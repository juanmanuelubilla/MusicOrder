"""Session management: engine creation, WAL pragmas and a session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from musicfusion.db.models import Base, all_models


def create_database_engine(path: Path, *, journal_mode: str = "WAL") -> Engine:
    """Create a SQLite engine with performance-oriented PRAGMAs.

    Args:
        path: absolute path of the SQLite file.
        journal_mode: SQLite journal mode (WAL by default).

    Returns:
        A configured :class:`sqlalchemy.Engine`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA journal_mode={journal_mode}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def create_tables(engine: Engine) -> None:
    """Create all missing tables."""
    from typing import cast

    from sqlalchemy import Table

    tables: list[Table] = [cast(Table, m.__table__) for m in all_models()]
    Base.metadata.create_all(engine, tables=tables)


def _ddl_type(column, engine: Engine) -> str:
    """SQL name for a mapped column, usable in ``ALTER TABLE`` statements."""
    try:
        return str(column.type.compile(dialect=engine.dialect))
    except Exception:  # noqa: BLE001 - JSON/LargeBinary etc. have no SQLite DDL
        type_class = type(column.type).__name__
        mapping = {
            "Boolean": "BOOLEAN",
            "JSON": "TEXT",
            "LargeBinary": "BLOB",
            "DateTime": "DATETIME",
            "BigInteger": "BIGINT",
            "Integer": "INTEGER",
            "Float": "FLOAT",
            "String": "VARCHAR",
            "Text": "TEXT",
        }
        return mapping.get(type_class, "TEXT")


def migrate_schema(engine: Engine) -> None:
    """Additively migrate older databases to the current schema.

    ``create_all`` only creates *missing* tables; tables that already exist keep
    whatever columns they had. This inspects every model table and emits
    ``ALTER TABLE ... ADD COLUMN`` for any column the ORM expects but the stored
    table lacks, so databases created by earlier versions (e.g. ``match_records``
    without the ``same_song`` flags) keep working.
    """
    from typing import cast

    from sqlalchemy import Table, inspect

    inspector = inspect(engine)
    available = {name for name in inspector.get_table_names()}

    for model in all_models():
        table = cast(Table, model.__table__)
        if table.name not in available:
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column for column in table.columns if column.name not in existing]
        if not missing:
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column for column in table.columns if column.name not in existing]
        if not missing:
            continue
        statements = [
            f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
            f"{_ddl_type(column, engine)}"
            for column in missing
        ]
        with engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)


class Database:
    """Thread-safe wrapper around an engine and a session factory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_database_engine(path)
        self.SessionFactory = sessionmaker(bind=self.engine, autoflush=False,
                                           expire_on_commit=False)

    def create_schema(self) -> None:
        """Create all missing tables and migrate older ones."""
        create_tables(self.engine)
        migrate_schema(self.engine)

    def session(self) -> Session:
        """Return a new :class:`sqlalchemy.orm.Session`."""
        return self.SessionFactory()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Provide a session that commits on success and rolls back on error."""
        session = self.SessionFactory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Release all pooled connections."""
        self.engine.dispose()

    def reset_database(self) -> None:
        """Completely purge all database data, recreate clean tables and VACUUM."""
        self.engine.dispose()
        try:
            if self.path.exists():
                self.path.unlink()
            for ext in ("-wal", "-shm", ".wal", ".shm"):
                sidecar = self.path.parent / f"{self.path.name}{ext}"
                if sidecar.exists():
                    sidecar.unlink()
        except OSError:
            # If file unlinking fails (e.g. open handles), drop tables
            with self.engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                for m in reversed(all_models()):
                    tbl = getattr(m, "__tablename__", None)
                    if tbl:
                        conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{tbl}"')
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        self.engine = create_database_engine(self.path)
        self.SessionFactory.configure(bind=self.engine)
        self.create_schema()
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("VACUUM")
        except Exception:
            pass

    def vacuum(self) -> None:
        """Run SQLite VACUUM to reclaim space and defragment."""
        with self.engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")

    def get_database_size_bytes(self) -> int:
        """Return total size of the database file and WAL/SHM sidecars in bytes."""
        total = 0
        if self.path.exists():
            total += self.path.stat().st_size
        for ext in ("-wal", "-shm", ".wal", ".shm"):
            sidecar = self.path.parent / f"{self.path.name}{ext}"
            if sidecar.exists():
                try:
                    total += sidecar.stat().st_size
                except OSError:
                    pass
        return total

    def remove_library_records(self, library_path: str) -> int:
        """Remove all tracks and associated records belonging to a library path."""
        from sqlalchemy import delete, select
        from musicfusion.db.models import Album, Artist, Library, Track, TrackFingerprint, TrackHash

        count_removed = 0
        with self.session_scope() as session:
            lib = session.scalar(select(Library).where(Library.path == str(library_path)))
            if lib:
                tracks = session.scalars(select(Track).where(Track.library_id == lib.id)).all()
                count_removed = len(tracks)
                track_ids = [t.id for t in tracks]
                if track_ids:
                    session.execute(delete(TrackHash).where(TrackHash.track_id.in_(track_ids)))
                    session.execute(delete(TrackFingerprint).where(TrackFingerprint.track_id.in_(track_ids)))
                    session.execute(delete(Track).where(Track.id.in_(track_ids)))
                session.delete(lib)
        return count_removed

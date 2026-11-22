"""Fusion executor.

Executes a :class:`FusionPlan` by copying files into the destination, creating
a restorable history entry before any mutation, and supporting full undo of
the last operation.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from musicfusion.audio.hashing import hash_file
from musicfusion.core.paths import unique_path
from musicfusion.db.models import HistoryEntry
from musicfusion.db.session import Database
from musicfusion.merge.planner import FusionPlan

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]

#: Operations that produced a physical file and can be undone.
UNDOABLE_KINDS = ("copy", "convert")


@dataclass
class ExecutionResult:
    """Outcome of executing a fusion plan."""

    copied: int = 0
    skipped: int = 0
    conflicts: int = 0
    errors: int = 0
    total_bytes: int = 0
    history_id: int | None = None

    def summary(self) -> str:
        return (
            f"copied={self.copied} skipped={self.skipped} "
            f"conflicts={self.conflicts} errors={self.errors}"
        )


class FusionExecutor:
    """Executes plans into a destination while recording undo data."""

    def __init__(self, db: Database | None = None) -> None:
        self._database = db

    def _ensure_db(self) -> Database:
        if self._database is None:
            self._database = Database(Path("/tmp/musicfusion-undo.db"))
            self._database.create_schema()
        return self._database

    def execute(
        self,
        plan: FusionPlan,
        *,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> ExecutionResult:
        """Run the planned actions. Sources are never written to."""
        if self._database is None:
            self._database = Database(plan.destination.parent / "fusion.db")
            self._database.create_schema()

        result = ExecutionResult()
        copied_entries: list[dict[str, str]] = []
        total = max(1, len(plan.actions))
        for index, action in enumerate(plan.actions, start=1):
            if on_progress:
                on_progress(index, total, f"Procesando {action.source.name}…")
            try:
                if action.kind in ("skip", "keep"):
                    result.skipped += 1
                    continue
                target = action.target
                if not action.source.is_file():
                    logger.warning("Source missing: %s", action.source)
                    result.errors += 1
                    continue
                if target.exists():
                    if target.resolve() == action.source.resolve():
                        result.skipped += 1
                        continue
                    # Si target ya contiene exactamente el mismo archivo (mismo SHA-256), omitir para evitar duplicar
                    src_hash = hash_file(action.source, "sha256")
                    dst_hash = hash_file(target, "sha256")
                    if src_hash is not None and src_hash == dst_hash:
                        result.skipped += 1
                        continue
                    target = unique_path(target)
                    result.conflicts += 1
                if dry_run:
                    result.copied += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_target = target.parent / f".{target.name}.tmp.{action.source.stat().st_size}"
                shutil.copy2(action.source, temp_target)
                os.replace(temp_target, target)
                copied_entries.append(
                    {"source": str(action.source), "target": str(target)}
                )
                result.copied += 1
                result.total_bytes += action.source.stat().st_size
            except OSError as exc:
                logger.warning("Copy failed %s -> %s: %s", action.source, action.target, exc)
                result.errors += 1

        if not dry_run and copied_entries:
            result.history_id = self._record_history(
                plan, copied_entries, result
            )
        logger.info("Fusion executed: %s", result.summary())
        return result

    def _record_history(
        self, plan: FusionPlan, copied: list[dict[str, str]], result: ExecutionResult
    ) -> int:
        with self._ensure_db().session_scope() as session:
            entry = HistoryEntry(
                kind="library_fusion",
                description=(
                    f"Fusion {plan.lib_a.name} + {plan.lib_b.name} "
                    f"-> {plan.destination.name or plan.destination} ({result.copied} files)"
                ),
                data={
                    "destination": str(plan.destination),
                    "copied": copied,
                },
                undoable=True,
            )
            session.add(entry)
            session.flush()
            return int(entry.id)

    def undo_last(self, history_id: int | None = None) -> bool:
        """Undo the latest undoable history entry (removes copied files)."""
        with self._ensure_db().session_scope() as session:
            query = select(HistoryEntry).where(
                HistoryEntry.undoable.is_(True),
                HistoryEntry.undone.is_(False),
            ).order_by(HistoryEntry.id.desc())
            if history_id:
                query = query.where(HistoryEntry.id == history_id)
            entry = session.scalar(query)
            if entry is None or entry.undone:
                return False
            data = entry.data or {}
            copied = data.get("copied", [])
            for item in copied:
                target = Path(item["target"])
                if target.exists():
                    try:
                        if target.is_file():
                            target.unlink()
                            logger.info("Undo removed %s", target)
                    except OSError as exc:
                        logger.warning("Undo failed for %s: %s", target, exc)
            setattr(entry, "undone", True)
        return True

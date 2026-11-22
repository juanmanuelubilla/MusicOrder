"""ORM models for the normalized MusicFusion database.

The schema keeps audio files, their metadata, acoustic fingerprints and every
audit object (scans, jobs, reports, history) in clearly separated tables with
appropriate indexes and foreign keys. Attributes use SQLAlchemy 2.0 ``Mapped``
annotations so the static type checker sees mapped value types, not columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TimestampMixin:
    """Provide created/updated timestamp columns."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class Library(Base, TimestampMixin):
    """A configured music library root directory."""

    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    watch: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tracks: Mapped[list[Track]] = relationship("Track", back_populates="library")
    scans: Mapped[list[Scan]] = relationship("Scan", back_populates="library")


class Artist(Base, TimestampMixin):
    """A musical artist (album/compilation artist resolution)."""

    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    sort_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    musicbrainz_id: Mapped[str | None] = mapped_column(String(36), index=True)
    discogs_id: Mapped[str | None] = mapped_column(String(32), index=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    albums: Mapped[list[Album]] = relationship("Album", back_populates="artist")
    tracks: Mapped[list[Track]] = relationship(
        "Track", back_populates="artist", foreign_keys="Track.artist_id"
    )


class Album(Base, TimestampMixin):
    """A release (album) associated with an artist or album-artist name."""

    __tablename__ = "albums"
    __table_args__ = (
        Index("ix_albums_year", "year"),
        UniqueConstraint("title_key", "artist_name", name="uq_album_title_artist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id"), nullable=True)
    artist_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title_key: Mapped[str] = mapped_column(String(512), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genres: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    total_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compilation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    album_artist: Mapped[str | None] = mapped_column(String(512), nullable=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)
    discogs_id: Mapped[str | None] = mapped_column(String(32), index=True)
    catalog_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    artist: Mapped[Artist | None] = relationship("Artist", back_populates="albums")
    tracks: Mapped[list[Track]] = relationship("Track", back_populates="album")
    artwork: Mapped[AlbumArt | None] = relationship("AlbumArt", back_populates="album", uselist=False)


class Track(Base, TimestampMixin):
    """A single audio file tracked in the library."""

    __tablename__ = "tracks"
    __table_args__ = (
        Index("ix_tracks_artist", "artist_name"),
        Index("ix_tracks_album_id", "album_id"),
        Index("ix_tracks_title_key", "title_key"),
        Index("ix_tracks_mtime", "mtime_ns"),
        Index("ix_tracks_duration", "duration_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id"), nullable=True)
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id"), nullable=True)
    album_id: Mapped[int | None] = mapped_column(ForeignKey("albums.id"), nullable=True)

    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    rel_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ctime_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    artist_name: Mapped[str] = mapped_column(String(512), nullable=False)
    album_artist: Mapped[str | None] = mapped_column(String(512), nullable=True)
    album_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    title_key: Mapped[str] = mapped_column(String(512), nullable=False)
    track_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    composer: Mapped[str | None] = mapped_column(String(512), nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    acoustic_id: Mapped[str | None] = mapped_column(String(40), index=True)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(36), index=True)
    isrc: Mapped[str | None] = mapped_column(String(32), index=True)
    has_embedded_art: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Metadata richness: count of recognized tags present at scan time.
    metadata_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    library: Mapped[Library | None] = relationship("Library", back_populates="tracks")
    artist: Mapped[Artist | None] = relationship(
        "Artist", back_populates="tracks", foreign_keys=[artist_id]
    )
    album: Mapped[Album | None] = relationship("Album", back_populates="tracks")
    hashes: Mapped[list[TrackHash]] = relationship(
        "TrackHash", back_populates="track", cascade="all, delete-orphan"
    )
    fingerprints: Mapped[list[TrackFingerprint]] = relationship(
        "TrackFingerprint", back_populates="track", cascade="all, delete-orphan"
    )
    duplicates: Mapped[list[DuplicateEntry]] = relationship(
        "DuplicateEntry", back_populates="track", cascade="all, delete-orphan"
    )
    match_records: Mapped[list[MatchRecord]] = relationship(
        "MatchRecord",
        foreign_keys="MatchRecord.source_track_id",
        back_populates="source_track",
        cascade="all, delete-orphan",
    )


class TrackHash(Base):
    """Hash entries per file (sha256, md5, ...)."""

    __tablename__ = "hashes"
    __table_args__ = (UniqueConstraint("track_id", "algorithm", name="uq_hash_track_algo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)

    track: Mapped[Track] = relationship("Track", back_populates="hashes")


class TrackFingerprint(Base):
    """Acoustic fingerprints per file (chromaprint, acoustid, spectral)."""

    __tablename__ = "fingerprints"
    __table_args__ = (UniqueConstraint("track_id", "algorithm", name="uq_fp_track_algo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    track: Mapped[Track] = relationship("Track", back_populates="fingerprints")


class AlbumArt(Base):
    """Artwork associated with an album (embedded or sidecar file)."""

    __tablename__ = "album_art"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id"), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="front")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    album: Mapped[Album] = relationship("Album", back_populates="artwork")


class Scan(Base):
    """A single library indexing run."""

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_added: Mapped[int] = mapped_column(Integer, default=0)
    files_modified: Mapped[int] = mapped_column(Integer, default=0)
    files_removed: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    full: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    library: Mapped[Library | None] = relationship("Library", back_populates="scans")


class Job(Base):
    """Anasynchronous task tracked by the job queue."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Report(Base):
    """A generated report file (html/pdf/csv/xlsx)."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Statistic(Base):
    """Named statistics (key/value) recomputed by the dashboard."""

    __tablename__ = "statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class Setting(Base):
    """Persistent key/value settings stored inside the database."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    value: Mapped[object | None] = mapped_column(JSON, nullable=True)


class HistoryEntry(Base):
    """Audit trail of destructive or significant operations."""

    __tablename__ = "history"
    __table_args__ = (Index("ix_history_ts", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undoable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    undone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DuplicateGroup(Base):
    """A group of tracks considered duplicates of one another."""

    __tablename__ = "duplicate_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    entries: Mapped[list[DuplicateEntry]] = relationship(
        "DuplicateEntry", back_populates="group", cascade="all, delete-orphan"
    )


class DuplicateEntry(Base):
    """Membership of a track inside a duplicate group."""

    __tablename__ = "duplicates"
    __table_args__ = (UniqueConstraint("group_id", "track_id", name="uq_dup_group_track"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("duplicate_groups.id"), nullable=False)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), nullable=False)

    group: Mapped[DuplicateGroup] = relationship("DuplicateGroup", back_populates="entries")
    track: Mapped[Track] = relationship("Track", back_populates="duplicates")


class Playlist(Base, TimestampMixin):
    """A playlist with absolute-path-aware entries."""

    __tablename__ = "playlists"
    __table_args__ = (Index("ix_playlists_kind", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    entries: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class MatchRecord(Base, TimestampMixin):
    """A persisted track-to-track matching result.

    Rows keep full history: every (re)analysis stores a new record, and human
    decisions (approve/reject/invalidate) are written back to ``decision`` so
    the Matching Engine and future Decision Engine can consult them.
    """

    __tablename__ = "match_records"
    __table_args__ = (
        Index("ix_matches_cache", "cache_key"),
        Index("ix_matches_classification", "classification"),
        Index("ix_matches_source_track", "source_track_id"),
        Index("ix_matches_candidate_track", "candidate_track_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"), nullable=True)
    candidate_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"), nullable=True)

    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)

    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    matching_method: Mapped[str] = mapped_column(String(64), default="weighted", nullable=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    artist_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    album_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    title_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    track_number_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    disc_number_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    year_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fingerprint_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    album_context_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    reasons: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    same_song: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    same_version: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    edition_different: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version_different: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version_tag: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # three-level identity + candidate-generation provenance (spec §32):
    # lets stored results be invalidated when the algorithm version changes
    same_track: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    same_album: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    same_release: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    candidate_phase: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    matching_version: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    invalidated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    source_track: Mapped[Track | None] = relationship(
        "Track", foreign_keys=[source_track_id], back_populates="match_records"
    )
    candidate_track: Mapped[Track | None] = relationship(
        "Track", foreign_keys=[candidate_track_id]
    )


class DecisionRecord(Base, TimestampMixin):
    """A persisted Decision Engine result for one candidate group.

    Stores every candidate score, the chosen winner and the explanation so the
    Fusion Engine (and later review tooling) can reconstruct *why* a specific
    copy was kept. Rows are grouped by ``group_key`` and linked to the
    ``match_records`` that produced the group when available.
    """

    __tablename__ = "decision_records"
    __table_args__ = (
        Index("ix_decision_group", "group_key"),
        Index("ix_decision_winner", "winner_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)

    winner_path: Mapped[str] = mapped_column(Text, nullable=False)
    winner_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    runner_up_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    runner_up_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    scores: Mapped[dict[str, float] | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    weights: Mapped[dict[str, float] | None] = mapped_column(JSON, nullable=True)

    match_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    winner_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id"), nullable=True
    )
    winner_track: Mapped[Track | None] = relationship("Track", foreign_keys=[winner_track_id])


def all_models() -> list[type[Base]]:
    """Return the model classes known to the mapper for create_all()."""
    return [
        Library, Artist, Album, Track, TrackHash, TrackFingerprint,
        AlbumArt, Scan, Job, Report, Statistic, Setting, HistoryEntry,
        DuplicateGroup, DuplicateEntry, Playlist, MatchRecord, DecisionRecord,
    ]

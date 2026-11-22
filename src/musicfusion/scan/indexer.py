"""Incremental library indexer.

Walks configured library roots, compares file identity (file size + mtime)
against stored rows, re-reads metadata only for new or modified files and
removes indexed rows whose files disappeared.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from musicfusion.audio.hashing import compute_hashes
from musicfusion.audio.metadata import AUDIO_EXTENSIONS, TrackInfo, read_metadata
from musicfusion.audio.quality import AudioQuality, probe_quality
from musicfusion.config.settings import LibraryConfig, Settings
from musicfusion.core.text import normalize_title
from musicfusion.db.models import Album, Artist, Library, Scan, Track
from musicfusion.db.session import Database

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ScanStats:
    """Aggregated counters of a scanning run."""

    files_found: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    errors: int = 0


class LibraryIndexer:
    """Incremental indexer backed by an open :class:`Database`."""

    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        *,
        default_hashes: bool = True,
    ) -> None:
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.db.create_schema()
        self.default_hashes = default_hashes

    def libraries(self) -> list[LibraryConfig]:
        """Return the configured library roots."""
        return self.settings.libraries()

    def walk_audio_files(self, root: Path) -> Iterator[Path]:
        """Yield supported audio files under ``root`` recursively."""
        extensions = set(self.settings.get("scan.include_extensions", list(AUDIO_EXTENSIONS)))
        exclude_names = set(self.settings.get("scan.exclude_names", []))
        follow_symlinks = bool(self.settings.get("scan.follows_symlinks", False))
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            dirnames[:] = [name for name in dirnames if name not in exclude_names]
            for filename in filenames:
                if filename in exclude_names:
                    continue
                if Path(filename).suffix.lower() not in extensions:
                    continue
                yield Path(dirpath) / filename

    def run(
        self,
        *,
        full: bool = False,
        force_hashes: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, int]:
        """Scan all libraries and return an aggregate stats dictionary."""
        total_stat: dict[str, int] = {
            "files_found": 0, "files_added": 0, "files_modified": 0,
            "files_removed": 0, "files_skipped": 0, "errors": 0,
        }
        for lib in self.libraries():
            stats = self.scan_library(lib, full=full, force_hashes=force_hashes,
                                      on_progress=on_progress)
            for key, value in stats.items():
                total_stat[key] += value
        return total_stat

    def scan_library(
        self,
        library_cfg: LibraryConfig,
        *,
        full: bool = False,
        force_hashes: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, int]:
        """Scan a single library root incrementally."""
        stats = ScanStats()
        root = Path(library_cfg.path).expanduser()
        if not root.is_dir():
            logger.warning("Library root does not exist: %s", root)
            return self._as_dict(stats)

        with self.db.session_scope() as session:
            library = session.scalar(select(Library).where(Library.path == str(root.resolve())))
            if library is None:
                library = Library(name=library_cfg.name, path=str(root.resolve()),
                                  enabled=True, watch=library_cfg.watch)
                session.add(library)
                session.flush()

            scan = Scan(status="running", library_id=library.id, full=full)
            session.add(scan)
            session.flush()

            known: dict[str, tuple[int, int]] = {}
            for row in session.scalars(
                select(Track).where(Track.library_id == library.id)
            ):
                known[row.path] = (row.file_size, row.mtime_ns)

            files = list(self.walk_audio_files(root))
            total = max(1, len(files))
            for index, path in enumerate(files, start=1):
                stats.files_found += 1
                summary = f"{library.name}: {path.name}"
                if on_progress:
                    on_progress(index, total, summary)
                try:
                    stat = path.stat()
                except OSError as exc:
                    logger.warning("Cannot stat %s: %s", path, exc)
                    stats.errors += 1
                    continue
                if (
                    not full
                    and not force_hashes
                    and str(path) in known
                    and known[str(path)] == (stat.st_size, stat.st_mtime_ns)
                ):
                    stats.files_skipped += 1
                    continue
                self._process_one(library, path, stat, stats, session, force_hashes)

            removed = 0
            for db_path in known:
                if not Path(db_path).exists():
                    track = session.scalar(select(Track).where(Track.path == db_path))
                    if track:
                        session.delete(track)
                        removed += 1
            stats.files_removed = removed

            scan.finished_at = datetime.utcnow()
            scan.status = "ok"
            scan.files_found = stats.files_found
            scan.files_added = stats.files_added
            scan.files_modified = stats.files_modified
            scan.files_removed = stats.files_removed
            scan.files_skipped = stats.files_skipped
            scan.errors = stats.errors
            library.last_scanned_at = datetime.utcnow()

        return self._as_dict(stats)

    def _process_one(
        self,
        library: Library,
        path: Path,
        stat: os.stat_result,
        stats: ScanStats,
        session: Session,
        force_hashes: bool,
    ) -> None:
        """Read metadata, hash and quality for a single file and upsert it."""
        metadata = read_metadata(path)
        if metadata is None:
            stats.errors += 1
            return
        digest = compute_hashes(path).get("sha256") if (self.default_hashes or force_hashes) else None
        quality: AudioQuality | None
        try:
            quality = probe_quality(path)
        except FileNotFoundError:
            quality = None
        if quality is not None:
            metadata.bitrate_kbps = quality.bitrate_kbps or metadata.bitrate_kbps
            metadata.sample_rate_hz = quality.sample_rate_hz or metadata.sample_rate_hz
            metadata.bit_depth = quality.bit_depth or metadata.bit_depth
            metadata.channels = quality.channels or metadata.channels

        track = _upsert_track(session, path, library, stat, metadata, digest, quality)
        if track.id:
            stats.files_added += 1
        else:
            stats.files_modified += 1

    def _as_dict(self, stats: ScanStats) -> dict[str, int]:
        return {
            "files_found": stats.files_found,
            "files_added": stats.files_added,
            "files_modified": stats.files_modified,
            "files_removed": stats.files_removed,
            "files_skipped": stats.files_skipped,
            "errors": stats.errors,
        }


def _upsert_artist(session: Session, name: str) -> Artist:
    artist = session.scalar(select(Artist).where(Artist.name == name))
    if artist is None:
        artist = Artist(name=name)
        session.add(artist)
        session.flush()
    return artist


def _upsert_album(
    session: Session, title: str, artist_name: str, year: int | None, artist: Artist
) -> Album:
    title_key = normalize_title(title)
    album = session.scalar(
        select(Album).where(Album.title_key == title_key, Album.artist_name == artist_name)
    )
    if album is None:
        album = Album(title=title, title_key=title_key, artist_name=artist_name,
                      artist_id=artist.id, year=year)
        session.add(album)
        session.flush()
    elif year and album.year in (None,):
        album.year = year
    return album


def _upsert_track(
    session: Session,
    path: Path,
    library: Library,
    stat: os.stat_result,
    metadata: TrackInfo,
    digest: str | None,
    quality: AudioQuality | None = None,
) -> Track:
    title = metadata.title or path.stem
    artist_name = metadata.artist or "Unknown Artist"
    album_title = metadata.album or "Unknown Album"
    artist = _upsert_artist(session, artist_name)
    album = _upsert_album(session, album_title, artist_name, metadata.year, artist)

    track = session.scalar(select(Track).where(Track.path == str(path)))
    if track is None:
        track = Track(path=str(path))
        session.add(track)

    track.library_id = library.id
    s = str(path).replace(str(library.path), "").lstrip("/")
    track.rel_path = s
    track.file_size = stat.st_size
    track.mtime_ns = stat.st_mtime_ns
    track.artist_name = artist_name
    track.album_artist = metadata.album_artist
    track.artist_id = artist.id
    track.album_id = album.id
    track.album_name = metadata.album
    track.title = title
    track.title_key = normalize_title(metadata.title)
    track.track_no = metadata.track_no
    track.disc_no = metadata.disc_no
    track.year = metadata.year
    track.genre = metadata.genre
    track.comment = metadata.comment
    track.composer = metadata.composer
    track.duration_ms = metadata.duration_ms
    track.format = path.suffix.lower().lstrip(".")
    track.bitrate_kbps = metadata.bitrate_kbps
    track.sample_rate_hz = metadata.sample_rate_hz
    track.bit_depth = metadata.bit_depth
    track.channels = metadata.channels
    track.replay_gain = metadata.replay_gain
    track.isrc = metadata.isrc
    track.musicbrainz_recording_id = metadata.recording_id
    track.metadata_score = metadata.metadata_score()
    track.has_embedded_art = metadata.has_embedded_art
    if quality is not None:
        track.quality_score = quality.score()
        track.codec = quality.codec or None
    if digest:
        track.sha256 = digest
    session.flush()
    return track


def normalize_title_for_key(text: str) -> str:
    """Small helper delegating to core normalization for album keys."""
    from musicfusion.core.text import normalize_title

    return normalize_title(text or "")

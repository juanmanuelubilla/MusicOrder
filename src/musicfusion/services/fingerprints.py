"""Persistent acoustic fingerprint store (database facade).

Fingerprints are computed once per file (typically by the ``fingerprint`` CLI
command or on demand) and stored in the ``fingerprints`` table. Every other
consumer -- the matching engine, the broad comparison and the metadata
suggestions -- reads through :func:`fingerprint_map` so audio is never decoded
twice.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import or_, select

from musicfusion.db.models import Track, TrackFingerprint

logger = logging.getLogger(__name__)

ALGORITHM = "chromaprint"


def fingerprint_map(db, roots: list[Path] | None = None) -> dict[str, str]:
    """Return ``path -> fingerprint`` for stored chromaprint rows.

    When ``roots`` is given, only files living under those roots are returned.
    """
    suffixes = [str(root.resolve()) + "%" for root in (roots or [])]
    result: dict[str, str] = {}
    with db.session() as session:
        path_by_id: dict[int, str] = {}
        tracks = session.query(Track)
        if suffixes:
            tracks = tracks.filter(or_(*(Track.path.like(s) for s in suffixes)))
        for track in tracks.yield_per(500):
            path_by_id[track.id] = track.path
        for fingerprint in session.query(TrackFingerprint).filter_by(algorithm=ALGORITHM):
            if fingerprint.track_id in path_by_id and fingerprint.value:
                result[path_by_id[fingerprint.track_id]] = fingerprint.value
    return result


def find_fingerprint(db, path: str) -> str:
    """Return the stored fingerprint for an exact path, or ``""``."""
    return fingerprint_map(db, roots=[Path(path)]).get(str(path), "")


def _find_row(db, path: str) -> tuple[int | None, TrackFingerprint | None]:
    with db.session() as session:
        track = session.scalar(select(Track).where(Track.path == str(path)))
        if track is None:
            return None, None
        row = session.scalar(
            select(TrackFingerprint).where(
                TrackFingerprint.track_id == track.id,
                TrackFingerprint.algorithm == ALGORITHM,
            )
        )
        return track.id, row


def has_fingerprint(db, path: str) -> bool:
    """Return True when a fingerprint row already exists for ``path``."""
    return _find_row(db, path)[1] is not None


def store(db, path: str, result) -> bool:
    """Upsert a fingerprint row (mirrored into ``tracks.acoustic_id``)."""
    track_id, row = _find_row(db, path)
    if track_id is None:
        return False
    with db.session_scope() as session:
        if row is None:
            row = session.scalar(
                select(TrackFingerprint).where(
                    TrackFingerprint.track_id == track_id,
                    TrackFingerprint.algorithm == ALGORITHM,
                )
            ) or TrackFingerprint(track_id=track_id, algorithm=ALGORITHM)
            if row.id is None:
                session.add(row)
        row.value = result.fingerprint
        row.duration_seconds = result.duration_seconds
        row.source = result.backend
        track = session.get(Track, track_id)
        if track is not None:
            track.acoustic_id = result.fingerprint[:40]
    return True


def fingerprint_track(db, path: str, *, force: bool = False):
    """Compute and store a fingerprint for ``path`` (skips stored ones).

    Returns the :class:`~musicfusion.audio.fingerprint.FingerprintResult` or
    ``None`` when the backend cannot process the file.
    """
    if not force:
        stored = find_fingerprint(db, path)
        if stored:
            from musicfusion.audio.fingerprint import FingerprintResult

            return FingerprintResult(fingerprint=stored, duration_seconds=0.0, backend="stored")
    from musicfusion.audio.fingerprint import fingerprint_file

    result = fingerprint_file(str(path))
    if result is None:
        return None
    store(db, str(path), result)
    return result


__all__ = ["find_fingerprint", "fingerprint_map", "fingerprint_track", "has_fingerprint", "store"]

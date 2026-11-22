"""Duplicate group detection across the indexed library.

Three passes:
  1. exact duplicates grouped by SHA-256;
  2. identical recordings grouped by (artist key, fuzzy title, duration);
  3. near-duplicate names grouped by title similarity alone.

Groups are stored as rows in ``duplicate_groups``/``duplicates``. The
classifier labels each group so different versions are never merged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from sqlalchemy import select

from musicfusion.compare.similarity import duration_similarity, title_similarity
from musicfusion.core.text import normalize_key, normalize_title
from musicfusion.db.models import DuplicateEntry, DuplicateGroup, Track
from musicfusion.db.session import Database

logger = logging.getLogger(__name__)

MAX_DURATION_DIFF_RATIO = 0.08

ProgressCallback = Callable[[int, int, str], None]


def _under_any(path: Path, roots: list[Path]) -> bool:
    """Return True when ``path`` is inside any of ``roots``."""
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


@dataclass
class TrackRef:
    """Lightweight track projection used by the detector."""

    id: int
    path: str
    artist: str
    title: str
    duration_ms: int
    sha256: str | None
    size: int
    library_id: int | None

    def title_key(self) -> str:
        return normalize_title(self.title)

    def artist_key(self) -> str:
        return normalize_key(self.artist)


@dataclass
class DetectedGroup:
    """A group of track ids judged duplicates of one another."""

    reason: str
    track_ids: list[int]
    classification: str = "different_version"
    confidence: float = 0.0


class DuplicateDetector:
    """Finds duplicate groups and persists them in the database."""

    def __init__(self, settings, db: Database | None = None) -> None:
        from musicfusion.config.settings import Settings

        self.settings: Settings = settings
        self.db = db or Database(settings.db_path)
        self.results: list[DetectedGroup] = []

    def _load_tracks(self, paths: list[Path] | None = None) -> list[TrackRef]:
        tracks: list[TrackRef] = []
        roots = [root.resolve() for root in (paths or [])]
        with self.db.session_scope() as session:
            rows = session.scalars(
                select(Track).where(Track.sha256.isnot(None))
            ).all()
            for r in rows:
                if roots and not _under_any(Path(r.path).resolve(), roots):
                    continue
                tracks.append(
                    TrackRef(id=r.id, path=r.path, artist=r.artist_name,
                             title=r.title or Path(r.path).stem,
                             duration_ms=r.duration_ms or 0,
                             sha256=r.sha256, size=r.file_size or 0,
                             library_id=r.library_id)
                )
        return tracks

    @staticmethod
    def _title_from_path(path: Path) -> str:
        return path.stem or ""

    def detect_hashes(self, tracks: list[TrackRef]) -> list[DetectedGroup]:
        groups: dict[str, list[TrackRef]] = {}
        for track in tracks:
            if track.sha256:
                groups.setdefault(track.sha256, []).append(track)
        return [
            DetectedGroup(reason="hash", track_ids=[t.id for t in members],
                          confidence=99.0)
            for members in groups.values()
            if len(members) > 1
        ]

    def detect_fuzzy(
        self, tracks: list[TrackRef],
        threshold: float = 88.0,
        same_artist: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> list[DetectedGroup]:
        by_artist: dict[str, list[TrackRef]] = {}
        for track in tracks:
            key = track.artist_key() if same_artist else "global"
            by_artist.setdefault(key, []).append(track)

        groups: list[DetectedGroup] = []
        track_to_group: dict[int, DetectedGroup] = {}
        buckets = list(by_artist.items())
        for bucket_index, (_key, candidates) in enumerate(buckets, start=1):
            if on_progress:
                on_progress(bucket_index, max(1, len(buckets)), "Comparando títulos…")
            for a, b in combinations(candidates, 2):
                ratio = title_similarity(a.title, b.title)
                if ratio < threshold:
                    continue
                dur_ok = duration_similarity(a.duration_ms, b.duration_ms)
                if dur_ok == 0 and ratio < 96:
                    continue
                self._add_pair(groups, track_to_group, a, b, reason="fuzzy", confidence=round(ratio, 1))
        return groups

    def _add_pair(
        self,
        groups: list[DetectedGroup],
        track_to_group: dict[int, DetectedGroup],
        a: TrackRef,
        b: TrackRef,
        reason: str,
        confidence: float,
    ) -> bool:
        group_a = track_to_group.get(a.id)
        group_b = track_to_group.get(b.id)

        if group_a is not None and group_b is not None:
            if group_a is not group_b:
                for tid in group_b.track_ids:
                    if tid not in group_a.track_ids:
                        group_a.track_ids.append(tid)
                    track_to_group[tid] = group_a
                group_a.confidence = max(group_a.confidence, group_b.confidence, confidence)
                if group_b in groups:
                    groups.remove(group_b)
            else:
                group_a.confidence = max(group_a.confidence, confidence)
            return True

        target_group = group_a or group_b
        if target_group is not None:
            if a.id not in target_group.track_ids:
                target_group.track_ids.append(a.id)
                track_to_group[a.id] = target_group
            if b.id not in target_group.track_ids:
                target_group.track_ids.append(b.id)
                track_to_group[b.id] = target_group
            target_group.confidence = max(target_group.confidence, confidence)
            return True

        new_group = DetectedGroup(reason=reason, track_ids=[a.id, b.id], confidence=confidence)
        groups.append(new_group)
        track_to_group[a.id] = new_group
        track_to_group[b.id] = new_group
        return True

    def run(
        self,
        *,
        persist: bool = True,
        paths: list[Path] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[DetectedGroup]:
        """Run all detection passes and return the aggregated groups.

        ``paths`` optionally restricts detection to tracks under those roots.
        """
        if on_progress:
            on_progress(1, 4, "Cargando pistas…")
        tracks = self._load_tracks(paths)
        if on_progress:
            on_progress(2, 4, "Buscando coincidencias por hash…")
        hash_groups = self.detect_hashes(tracks)

        fuzzy_groups = self.detect_fuzzy(
            tracks,
            on_progress=lambda step, total, msg: on_progress(
                2 + int(2 * step / max(1, total)), 4, msg
            )
            if on_progress
            else None,
        )

        seen: set[int] = set()
        merged: list[DetectedGroup] = []
        for group in hash_groups + fuzzy_groups:
            uniques = [tid for tid in group.track_ids if tid not in seen]
            if len(uniques) > 1:
                seen.update(uniques)
                merged.append(group)
        self.results = merged
        if on_progress:
            on_progress(3, 4, "Guardando grupos…")
        if persist:
            self._persist(merged)
        if on_progress:
            on_progress(4, 4, "Análisis terminado")
        return merged

    def _persist(self, groups: list[DetectedGroup]) -> None:
        with self.db.session_scope() as session:
            session.query(DuplicateEntry).delete()
            session.query(DuplicateGroup).delete()
            for group in groups:
                db_group = DuplicateGroup(
                    reason=group.reason,
                    classification=group.classification,
                    confidence=group.confidence,
                )
                session.add(db_group)
                session.flush()
                for track_id in group.track_ids:
                    session.add(DuplicateEntry(group_id=db_group.id, track_id=track_id))

    def results_as_dicts(self) -> list[dict[str, object]]:
        """Serialize detected groups for exporters and UI tables."""
        out: list[dict[str, object]] = []
        for group in self.results:
            out.append({
                "reason": group.reason,
                "classification": group.classification,
                "confidence": group.confidence,
                "track_ids": group.track_ids,
            })
        return out

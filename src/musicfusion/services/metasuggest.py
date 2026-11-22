"""Metadata centralization suggestions.

When several files in the library represent the *same song* (same bytes, same
acoustic fingerprint, same MusicBrainz recording, or the same title/artist),
the file with the richest metadata is picked as the *donor* and the others are
proposed as targets whose missing tags can be filled from the donor.

The suggestions are pure data -- applying them is an explicit, reversible
action that only writes tags (never deletes anything, never touches artwork).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from musicfusion.audio.metadata import TrackInfo, read_metadata, write_metadata_tags
from musicfusion.db.models import Track
from musicfusion.matching.normalize import primary_artist, text_profile

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.config.settings import Settings
    from musicfusion.db.session import Database

logger = logging.getLogger(__name__)

#: TrackInfo fields considered for centralization (``Track`` columns in parens).
_FIELDS = ("title", "artist", "album", "album_artist", "track_no", "year", "genre", "composer")
_FUZZY_TITLE_THRESHOLD = 0.80
MAX_COMPONENT_SIZE = 200


@dataclass(frozen=True)
class Suggestion:
    """A proposal to copy missing fields from ``donor_path`` into ``target_path``."""

    target_path: str
    target_score: float
    donor_path: str
    donor_score: float
    gain: float
    missing: tuple[str, ...]
    matched_by: str


@dataclass
class ApplyResult:
    """Outcome of applying a batch of suggestions."""

    applied: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class _UnionFind:
    """Tiny union-find used to connect tracks that share a song identity."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, node: int) -> int:
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


# ---------------------------------------------------------------------------
# suggestion discovery
# ---------------------------------------------------------------------------


def build_suggestions(
    settings: Settings,
    db: Database,
    *,
    roots: list[Path] | None = None,
    min_gain: float = 8.0,
    on_progress=None,
) -> list[Suggestion]:
    """Find metadata-centralization suggestions across the indexed tracks.

    ``roots`` optionally limits the search to files under those folders.
    """
    tracks = _load_tracks(db, roots)
    if not tracks:
        return []
    if on_progress:
        on_progress(0, len(tracks), "Cargando pistas indexadas…")

    fp_by_index = _load_fingerprints(db, tracks, roots)
    uf = _UnionFind(len(tracks))

    by_sha: dict[str, list[int]] = defaultdict(list)
    by_fp: dict[str, list[int]] = defaultdict(list)
    by_mb: dict[str, list[int]] = defaultdict(list)
    by_title: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_artist: dict[str, list[int]] = defaultdict(list)

    for index, track in enumerate(tracks):
        if track.sha256:
            by_sha[track.sha256].append(index)
        fingerprint = fp_by_index.get(index, "")
        if fingerprint:
            by_fp[fingerprint].append(index)
        if track.musicbrainz_recording_id:
            by_mb[track.musicbrainz_recording_id].append(index)
        artist_key = text_profile(primary_artist(track.artist_name)).key
        title_key = _title_key(track)
        if title_key:
            by_title[(artist_key, title_key)].append(index)
        if track.title:
            by_artist[artist_key].append(index)

    for bucket in (by_sha, by_fp, by_mb):
        for members in bucket.values():
            _union_bucket(uf, members)

    for members in by_title.values():
        _union_bucket(uf, members)

    for members in by_artist.values():
        if len(members) < 2:
            continue
        _union_fuzzy(uf, members, tracks)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(tracks)):
        components[uf.find(index)].append(index)

    suggestions: list[Suggestion] = []
    if on_progress:
        on_progress(len(tracks), len(tracks), "Analizando <misma canción>…")
    for members in components.values():
        if len(members) < 2 or len(members) > MAX_COMPONENT_SIZE:
            continue
        donor = max(members, key=lambda i: _score(tracks[i]))
        for target in members:
            if target == donor:
                continue
            gain = _score(tracks[donor]) - _score(tracks[target])
            if gain < min_gain:
                continue
            missing = _missing_fields(tracks[target], tracks[donor])
            if not missing:
                continue
            suggestions.append(
                Suggestion(
                    target_path=tracks[target].path,
                    target_score=_score(tracks[target]),
                    donor_path=tracks[donor].path,
                    donor_score=_score(tracks[donor]),
                    gain=round(gain, 1),
                    missing=missing,
                    matched_by=_matched_by(
                        tracks[target],
                        tracks[donor],
                        fp_by_index.get(target, ""),
                        fp_by_index.get(donor, ""),
                    ),
                )
            )
    suggestions.sort(key=lambda item: item.gain, reverse=True)
    return suggestions


def _union_bucket(uf: _UnionFind, members: list[int]) -> None:
    total = len(members)
    if total < 2:
        return
    for i in range(1, total):
        uf.union(members[0], members[i])


def _union_fuzzy(uf: _UnionFind, members: list[int], tracks: list[Track]) -> None:
    """Union titles within one artist bucket that fuzzy-match the same song."""
    placed: list[list[int]] = []
    for member in members:
        key = _title_key(tracks[member])
        bucket = None
        for candidate in placed:
            if _similar(key, _title_key(tracks[candidate[0]])) >= _FUZZY_TITLE_THRESHOLD:
                bucket = candidate
                break
        if bucket is None:
            placed.append([member])
        else:
            bucket.append(member)
    for bucket in placed:
        _union_bucket(uf, bucket)


def _load_tracks(db: Database, roots: list[Path] | None) -> list[Track]:
    from sqlalchemy import or_

    with db.session() as session:
        query = session.query(Track)
        if roots:
            suffixes = [str(root.resolve()) + "%" for root in roots]
            query = query.filter(or_(*(Track.path.like(s) for s in suffixes)))
        return list(query.all())


def _load_fingerprints(
    db: Database, tracks: list[Track], roots: list[Path] | None
) -> dict[int, str]:
    from musicfusion.services.fingerprints import fingerprint_map

    by_index: dict[int, str] = {}
    if not tracks:
        return by_index
    path_map = fingerprint_map(db, roots=roots) if roots else fingerprint_map(db)
    if not path_map:
        return by_index
    for index, track in enumerate(tracks):
        value = path_map.get(track.path)
        if value:
            by_index[index] = value
    return by_index


def _title_key(track: Track) -> str:
    return text_profile(track.title or "").base_key


def _similar(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return float(fuzz.token_set_ratio(left, right)) / 100.0


def _score(track: Track) -> float:
    value = track.metadata_score
    return round(float(value or 0.0), 1)


def _missing_fields(target: Track, donor: Track) -> tuple[str, ...]:
    missing: list[str] = []
    for name in _FIELDS:
        if _get(track=target, name=name) in ("", None) and _get(track=donor, name=name) not in (
            "",
            None,
        ):
            missing.append(name)
    return tuple(missing)


def _get(track: Track, name: str):
    mapping = {
        "title": track.title,
        "artist": track.artist_name,
        "album": track.album_name,
        "album_artist": track.album_artist,
        "track_no": track.track_no,
        "year": track.year,
        "genre": track.genre,
        "composer": track.composer,
    }
    return mapping[name]


def _matched_by(
    target: Track,
    donor: Track,
    fp_target: str,
    fp_donor: str,
) -> str:
    if target.sha256 and donor.sha256 and target.sha256 == donor.sha256:
        return "sha256"
    if (
        target.musicbrainz_recording_id
        and donor.musicbrainz_recording_id
        and target.musicbrainz_recording_id == donor.musicbrainz_recording_id
    ):
        return "musicbrainz_recording"
    if fp_target and fp_donor and fp_target == fp_donor:
        return "fingerprint"
    return "metadatos"


# ---------------------------------------------------------------------------
# applying suggestions
# ---------------------------------------------------------------------------


def apply_suggestions(
    db: Database, suggestions: list[Suggestion], *, dry_run: bool = False
) -> ApplyResult:
    """Write the merged tags for every suggestion (respecting ``dry_run``)."""
    result = ApplyResult()
    for suggestion in suggestions:
        errors = apply_one(db, suggestion, dry_run=dry_run)
        if errors:
            result.errors.extend(errors)
            result.skipped += 1
        else:
            result.applied += 1
    return result


def apply_one(db: Database, suggestion: Suggestion, *, dry_run: bool = False) -> list[str]:
    """Merge donor metadata into the target file and write its tags.

    Returns a list of error messages (empty on success).
    """
    donor_info = read_metadata(suggestion.donor_path)
    target_info = read_metadata(suggestion.target_path)
    if donor_info is None or target_info is None:
        return [f"no se pudo leer {suggestion.target_path} o su donante"]
    merged = _merge(target_info, donor_info)
    if dry_run:
        return []
    errors = write_metadata_tags(suggestion.target_path, merged)
    if not errors:
        _refresh_track(db, suggestion.target_path)
    return errors


def apply_one_with_art(db: Database, suggestion: Suggestion, *, dry_run: bool = False) -> list[str]:
    """Merge donor metadata and, when the target has no artwork, copy it too.

    Covers are only ever *added*: if the target already has embedded or sidecar
    art it is left untouched. Returns a list of error messages (empty on
    success).
    """
    from musicfusion.audio.artwork import discover_art, embed_art

    errors = apply_one(db, suggestion, dry_run=dry_run)
    if errors or dry_run:
        return errors
    target_path = Path(suggestion.target_path)
    if discover_art(target_path) is not None:
        return []
    donor_art = discover_art(Path(suggestion.donor_path))
    if donor_art is None:
        return []
    if embed_art(target_path, donor_art):
        refresh_track(db, suggestion.target_path)
        return []
    return ["no se pudo incrustar la portada del donante"]


def build_donor_map(
    settings: Settings,
    db: Database,
    *,
    roots: list[Path] | None = None,
) -> dict[str, Suggestion]:
    """Map every tracked file to its best metadata donor sibling.

    Purely a memory-friendly lookup built on :func:`build_suggestions`: each
    target path points to the :class:`Suggestion` that would fill it.
    """
    return {
        item.target_path: item
        for item in build_suggestions(settings, db, roots=roots, min_gain=0.0)
    }


def refresh_track(db: Database, path: str) -> None:
    """Re-read a file from disk and update its ``tracks`` row (public API)."""
    _refresh_track(db, path)


def _merge(target: TrackInfo, donor: TrackInfo) -> TrackInfo:
    """A copy of ``target`` with every empty field filled from ``donor``."""
    merged = TrackInfo(**{field: getattr(target, field) for field in target.__dataclass_fields__})
    for name in _FIELDS:
        if getattr(merged, name) in ("", None):
            setattr(merged, name, getattr(donor, name))
    return merged


def _refresh_track(db: Database, path: str) -> None:
    """Re-read a file after writing tags and update its ``tracks`` row."""
    info = read_metadata(path)
    if info is None:
        return
    from sqlalchemy import select

    with db.session_scope() as session:
        row = session.scalar(select(Track).where(Track.path == str(path)))
        if row is None:
            return
        row.title = (info.title or "") or Path(path).stem
        row.title_key = text_profile(info.title or "").key
        row.artist_name = info.artist or row.artist_name
        row.album_artist = info.album_artist or row.album_artist
        row.album_name = info.album or row.album_name
        row.track_no = info.track_no if info.track_no is not None else row.track_no
        row.year = info.year if info.year is not None else row.year
        row.genre = info.genre or row.genre
        row.composer = info.composer or row.composer
        row.metadata_score = info.metadata_score()
        row.has_embedded_art = bool(info.has_embedded_art)


__all__ = [
    "ApplyResult",
    "Suggestion",
    "apply_one",
    "apply_one_with_art",
    "apply_suggestions",
    "build_donor_map",
    "build_suggestions",
    "refresh_track",
]

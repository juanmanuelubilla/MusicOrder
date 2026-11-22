"""Candidate generation for the existing Matching Engine.

This module does NOT score or decide identity - it only proposes pairs of
tracks that the Matching Engine should analyse. The pipeline is:

    SOURCE FILES -> NORMALIZATION -> CANDIDATE GENERATION (here)
                 -> MATCHING ENGINE -> DECISION ENGINE

Phases (spec §22):

    1. exact normalized artist + album keys
    2. fuzzy artist similarity   (>= candidates.artist_threshold, default .75)
    3. fuzzy album similarity    (>= candidates.album_threshold,  default .80)
       - the YEAR is parsed apart and only used as a later signal; a year
         difference never blocks a candidate (reissues/remasters exist)
    4. tracklist similarity between candidate albums (strong context evidence)
    5. track pairing inside matched albums by title/duration/track-no -
       track numbers are a SIGNAL, never a requirement

The route on disk is just one more signal here; two files whose relative
paths differ can still become candidates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from musicfusion.matching.normalize import (
    normalize_music_text,
    primary_artist,
    text_profile,
)

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.matching.config import MatchConfig
    from musicfusion.matching.engine import MatchTrack

logger = logging.getLogger(__name__)

_YEAR_PREFIX = re.compile(r"^\s*((?:19|20)\d{2})\s*[-–]?\s*(.*)$")

#: title-similarity floor for pairing two tracks inside candidate albums
TRACK_PAIR_THRESHOLD = 62.0


@dataclass(frozen=True)
class TrackEntry:
    """One indexed audio file with precomputed canonical keys."""

    path: Path
    side: str
    artist_key: str
    album_key: str
    title_base: str
    version_tokens: tuple[str, ...]
    track_no: int | None
    disc_no: int | None
    duration_ms: int
    year: int | None
    match_track: MatchTrack

    @property
    def title_compact(self) -> str:
        return self.title_base.replace(" ", "")


@dataclass
class AlbumFolder:
    """All the tracks that live under one ``Año - Álbum`` folder."""

    side: str
    folder: Path
    raw_artist: str
    raw_album: str
    artist_key: str
    album_key: str
    year: int | None
    entries: list[TrackEntry] = field(default_factory=list)


@dataclass
class AlbumPairing:
    """Two album folders proposed as candidates for each other."""

    a: AlbumFolder
    b: AlbumFolder
    artist_score: float
    album_score: float
    year_score: float
    phase: str = ""
    tracklist_similarity: float = 0.0


def split_album_name(raw: str) -> tuple[int | None, str]:
    """Split ``"2001 - Jessico"`` / ``"2001 Jessico"`` / ``"Jessico"``.

    Returns ``(year, album_title)``; the year is metadata, never part of the
    identity string, so ``2001 - Jessico`` and ``2010 - Jessico`` share the
    same album_key (reissue candidates).
    """
    name = (raw or "").strip()
    match = _YEAR_PREFIX.match(name)
    if not match:
        return None, normalize_music_text(name)
    year = int(match.group(1))
    rest = match.group(2).strip()
    return year, normalize_music_text(rest)


def make_track_entry(path: Path, side: str, match_track: MatchTrack) -> TrackEntry:
    """Project one :class:`MatchTrack` into a candidate-generation entry."""
    from musicfusion.core.text import (
        album_from_folder,
        title_from_filename,
        track_no_from_filename,
        year_from_folder,
    )

    artist_raw = primary_artist(match_track.artist or match_track.album_artist or "")
    if not artist_raw:
        # derive from the folder layout: Artista/Año - Álbum/NN - Tema.ext
        grandparent = path.parent.parent.name
        artist_raw = grandparent.strip() if grandparent else ""
    album_raw = match_track.album or ""
    year = match_track.year
    if not album_raw:
        parent = path.parent.name
        album_raw = album_from_folder(parent) or ""
        year = year or year_from_folder(parent)
    title = match_track.title or ""
    track_no = match_track.track_no
    if not title:
        title = title_from_filename(path.stem) or path.stem.lower()
        if track_no is None:
            track_no = track_no_from_filename(path.stem)

    base, versions = _title_parts(title)
    folder_year, folder_album = split_album_name(path.parent.name)
    if folder_album:
        # trust the folder when tags disagree: it is the user's organization
        album_key = folder_album if not album_raw else normalize_music_text(album_raw)
    else:
        album_key = normalize_music_text(album_raw)
    entry_year = year or folder_year
    return TrackEntry(
        path=path,
        side=side,
        artist_key=normalize_music_text(artist_raw),
        album_key=album_key,
        title_base=base,
        version_tokens=versions,
        track_no=track_no if track_no is not None else match_track.track_no,
        disc_no=match_track.disc_no,
        duration_ms=match_track.duration_ms or 0,
        year=entry_year,
        match_track=match_track,
    )


def _title_parts(title: str) -> tuple[str, tuple[str, ...]]:
    profile = text_profile(title or "")
    if profile.base_tokens:
        return profile.base_key, profile.version_tokens
    from musicfusion.matching.normalize import split_base_and_version

    return split_base_and_version(title)


def build_album_folders(entries: list[TrackEntry]) -> list[AlbumFolder]:
    """Group entries by their containing folder (one AlbumFolder per folder)."""
    folders: dict[Path, AlbumFolder] = {}
    for entry in entries:
        folder = entry.path.parent
        book = folders.get(folder)
        if book is None:
            year, album_key = split_album_name(folder.name)
            album_key = entry.album_key or album_key
            book = AlbumFolder(
                side=entry.side,
                folder=folder,
                raw_artist=folder.parent.name,
                raw_album=folder.name,
                artist_key=entry.artist_key,
                album_key=album_key,
                year=entry.year or year,
                entries=[],
            )
            folders[folder] = book
        else:
            if not book.artist_key:
                book.artist_key = entry.artist_key
            if not book.year:
                book.year = entry.year
        book.entries.append(entry)
    return list(folders.values())


# ---------------------------------------------------------------------------
# phases 1-3: album-level candidates
# ---------------------------------------------------------------------------

def pair_album_folders(
    folders_a: list[AlbumFolder],
    folders_b: list[AlbumFolder],
    config: MatchConfig,
) -> list[AlbumPairing]:
    """Propose album pairs by normalized/fuzzy artist and album similarity."""
    artist_threshold = float(getattr(config, "artist_candidate_threshold", 0.75))
    album_threshold = float(getattr(config, "album_candidate_threshold", 0.80))
    pairings: list[AlbumPairing] = []

    for left in folders_a:
        if not left.artist_key:
            continue
        for right in folders_b:
            if not right.artist_key:
                continue
            artist_score = _text_similarity(left.artist_key, right.artist_key)
            if artist_score < artist_threshold:
                _log_reject("artist", left, right, artist_score, artist_threshold)
                continue
            album_score = _album_similarity(left, right)
            if album_score < album_threshold:
                _log_reject("album", left, right, album_score, album_threshold)
                continue
            pairings.append(
                AlbumPairing(
                    a=left,
                    b=right,
                    artist_score=artist_score,
                    album_score=album_score,
                    year_score=_year_score(left.year, right.year),
                    phase="exact_keys"
                    if artist_score >= 0.999 and album_score >= 0.999
                    else ("fuzzy_artist" if artist_score < 0.999 else "fuzzy_album"),
                )
            )
    return pairings


def _album_similarity(left: AlbumFolder, right: AlbumFolder) -> float:
    """Album-title similarity ignoring the leading year."""
    if not left.album_key or not right.album_key:
        return 0.0
    if left.album_key == right.album_key:
        return 1.0
    compact_l, compact_r = left.album_key.replace(" ", ""), right.album_key.replace(" ", "")
    if compact_l == compact_r:
        return 1.0
    return max(
        float(fuzz.token_set_ratio(left.album_key, right.album_key)) / 100.0,
        float(fuzz.token_sort_ratio(compact_l, compact_r)) / 100.0,
    )


def _text_similarity(a: str, b: str) -> float:
    """Similarity that also accepts glued spellings (``acdc`` vs ``ac dc``)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    compact_a, compact_b = a.replace(" ", ""), b.replace(" ", "")
    if compact_a == compact_b:
        return 1.0
    return max(
        float(fuzz.token_set_ratio(a, b)) / 100.0,
        float(fuzz.ratio(compact_a, compact_b)) / 100.0,
    )


def _year_score(a: int | None, b: int | None) -> float:
    """Year agreement as a SIGNAL only - never blocks candidacy (spec §8)."""
    if a is None or b is None:
        return 0.5
    if a == b:
        return 1.0
    if abs(a - b) <= 2:
        return 0.8
    return 0.4


def _log_reject(phase: str, left: AlbumFolder, right: AlbumFolder, score: float, threshold: float) -> None:
    logger.debug(
        "candidates[%s] rejected %s/%s vs %s/%s: %.2f < %.2f",
        phase, left.raw_artist, left.raw_album, right.raw_artist, right.raw_album,
        score, threshold,
    )


# ---------------------------------------------------------------------------
# phases 4-5: tracklist evidence + track pairing
# ---------------------------------------------------------------------------

def tracklists_similarity(titles_a: list[str], titles_b: list[str]) -> float:
    """Greedy fuzzy alignment of two title lists (0..1).

    Generic helper shared by candidate generation and the sync analyzer.
    """
    if not titles_a or not titles_b:
        return 0.0
    scored = sorted(
        (
            (float(fuzz.token_set_ratio(x, y)) / 100.0, x, y)
            for x in titles_a
            for y in titles_b
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    used_a: set[str] = set()
    used_b: set[str] = set()
    total = 0.0
    matched = 0
    for score, x, y in scored:
        if x in used_a or y in used_b or score < TRACK_PAIR_THRESHOLD / 100.0:
            continue
        used_a.add(x)
        used_b.add(y)
        total += score
        matched += 1
    if not matched:
        return 0.0
    coverage = matched / max(len(titles_a), len(titles_b))
    return round(min(1.0, (total / matched) * (0.5 + 0.5 * coverage)), 3)


def compute_tracklist_similarity(pairing: AlbumPairing) -> float:
    """Greedy fuzzy alignment of both tracklists (0..1).

    Strong contextual evidence: 10/12 fuzzy-matching titles mean the same
    release even when every file name differs in accents/punctuation/numbers.
    """
    titles_a = [e.title_base for e in pairing.a.entries]
    titles_b = [e.title_base for e in pairing.b.entries]
    return tracklists_similarity(titles_a, titles_b)


def pair_tracks_in_albums(
    pairing: AlbumPairing,
) -> list[tuple[TrackEntry, TrackEntry]]:
    """Best-first greedy track pairing inside two candidate albums.

    Track numbers are a bonus signal, never a requirement: ``01 - X`` pairs
    with plain ``X``, and different numbers do NOT block a candidate.
    """
    candidates: list[tuple[float, TrackEntry, TrackEntry]] = []
    for left in pairing.a.entries:
        for right in pairing.b.entries:
            score = _track_pair_score(left, right)
            if score >= TRACK_PAIR_THRESHOLD / 100.0:
                candidates.append((score, left, right))
    candidates.sort(key=lambda item: item[0], reverse=True)
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[TrackEntry, TrackEntry]] = []
    for score, left, right in candidates:
        if id(left) in used_a or id(right) in used_b:
            continue
        used_a.add(id(left))
        used_b.add(id(right))
        logger.debug(
            "candidates[track] paired %s <-> %s (%.2f)",
            left.path.name, right.path.name, score,
        )
        pairs.append((left, right))
    for leftover in pairing.a.entries:
        if id(leftover) not in used_a:
            logger.debug(
                "candidates[track] unpaired-in-album %s (no B candidate above %.2f)",
                leftover.path.name, TRACK_PAIR_THRESHOLD / 100.0,
            )
    return pairs


def _track_pair_score(left: TrackEntry, right: TrackEntry) -> float:
    """Title-first score with duration and track-number as reinforcement."""
    base = max(
        float(fuzz.token_set_ratio(left.title_base, right.title_base)) / 100.0,
        float(fuzz.ratio(left.title_compact, right.title_compact)) / 100.0,
    )
    if base <= 0.0:
        return 0.0
    score = base
    # duration agreement reinforces; disagreement must not veto here because
    # the Matching Engine weighs duration properly (and may use real audio)
    if left.duration_ms and right.duration_ms:
        gap = abs(left.duration_ms - right.duration_ms) / max(left.duration_ms, right.duration_ms)
        if gap <= 0.03:
            score = min(1.0, score + 0.05)
        elif gap >= 0.35:
            score -= 0.10
    if left.track_no is not None and right.track_no is not None and left.track_no == right.track_no:
        score = min(1.0, score + 0.03)
    return max(0.0, min(1.0, score))


__all__ = [
    "AlbumFolder",
    "AlbumPairing",
    "TrackEntry",
    "build_album_folders",
    "compute_tracklist_similarity",
    "make_track_entry",
    "pair_album_folders",
    "pair_tracks_in_albums",
    "split_album_name",
    "tracklists_similarity",
]

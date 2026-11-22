"""Structured similarity scoring between two tracks (or release signatures).

The comparer produces a 100-point *identity* match score from configurable
weights. It deliberately excludes quality/encoding signals: how "good" a file
sounds is the Decision Engine's concern, not whether two files represent the
same recording (see docs/COMANDOS.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import rapidfuzz

from musicfusion.core.text import edition_family, normalize_key, normalize_title

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "artist": 20.0,
    "album": 18.0,
    "title": 20.0,
    "year": 8.0,
    "duration": 14.0,
    "tracks": 8.0,
    "fingerprint": 6.0,
    "isrc": 6.0,
}

DURATION_TOLERANCE = 0.07  # 7% difference tolerated for "same recording"


@dataclass
class TrackShape:
    """Comparable snapshot of a track or release."""

    artist: str = ""
    album: str = ""
    title: str = ""
    year: int | None = None
    duration_ms: int = 0
    track_no: int | None = None
    total_tracks: int | None = None
    isrc: str = ""
    recording_id: str = ""
    release_id: str = ""
    acoustic_id: str = ""
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    bitrate_kbps: int | None = None
    codec: str = ""
    metadata_score: float = 0.0


@dataclass
class MatchBreakdown:
    """Per-dimension scores contributing to an overall match."""

    dims: dict[str, float] = field(default_factory=dict)
    total_possible: float = 0.0
    score: float = 0.0
    matched: bool = False

    def ratio(self) -> float:
        """Return the score normalized to 0..100."""
        if self.total_possible <= 0:
            return 0.0
        return round(100.0 * self.score / self.total_possible, 1)


def title_similarity(a: str, b: str) -> float:
    """RapidFuzz similarity between two normalized titles (0..100)."""
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return round(rapidfuzz.fuzz.ratio(na, nb), 1)


def duration_similarity(ms_a: int, ms_b: int) -> float:
    """1.0 when durations differ by less than the tolerance, else 0."""
    if ms_a <= 0 or ms_b <= 0:
        return 0.0
    diff = abs(ms_a - ms_b) / max(ms_a, ms_b, 1)
    return 1.0 if diff <= DURATION_TOLERANCE else 0.0


def identifier_match(a: str, b: str) -> bool:
    """Exact match for MusicBrainz/ISRC identifiers."""
    return bool(a) and a == b


class TrackComparer:
    """Computes a weighted similarity score between two tracks."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.total_w = sum(self.weights.values())

    def compare(self, left: TrackShape, right: TrackShape) -> MatchBreakdown:
        """Score ``left`` against ``right`` and return a breakdown.

        Identity dimensions (artist, album, title, year, duration, track count,
        fingerprint, ISRC) carry their configured weights; all are exclusive of
        audio quality, which is the Decision Engine's concern.
        """
        breakdown = MatchBreakdown(total_possible=self.total_w)

        artist_key_a = normalize_key(left.artist)
        artist_key_b = normalize_key(right.artist)
        dims = {}
        dims["artist"] = _weight(
            self.weights["artist"],
            100.0 if artist_key_a and artist_key_b and artist_key_a == artist_key_b else
            0.0,
        )
        album_a = edition_family(left.album)
        album_b = edition_family(right.album)
        dims["album"] = _weight(
            self.weights["album"],
            _ratio_match(album_a, album_b, left.track_no, right.track_no),
        )
        dims["title"] = _weight(self.weights["title"], title_similarity(left.title, right.title))
        dims["year"] = _weight(
            self.weights["year"],
            100.0 if left.year and right.year and left.year == right.year else 0.0,
        )
        dims["duration"] = _weight(
            self.weights["duration"], duration_similarity(left.duration_ms, right.duration_ms) * 100.0
        )
        dims["tracks"] = _weight(
            self.weights["tracks"],
            100.0 if left.total_tracks and right.total_tracks and
            left.total_tracks == right.total_tracks else 0.0,
        )
        dims["fingerprint"] = _weight(
            self.weights["fingerprint"],
            100.0 if identifier_match(left.acoustic_id, right.acoustic_id) else 0.0,
        )
        isrc_match = 100.0 if identifier_match(str(left.isrc), str(right.isrc)) else 0.0
        dims["isrc"] = _weight(self.weights["isrc"], isrc_match)

        dims = {k: v for k, v in dims.items() if v > 0}
        breakdown = MatchBreakdown(
            dims=dims, total_possible=self.total_w, score=sum(dims.values())
        )
        breakdown.matched = breakdown.score >= 0.5 * self.total_w
        return breakdown


def _weight(weight: float, contribution: float) -> float:
    """Return the weighted gain of a dimension (0..weight)."""
    return round(weight * max(0.0, min(100.0, contribution)) / 100.0, 2)


def _ratio_match(a: str, b: str, a_no: int | None, b_no: int | None) -> float:
    """Similarity for album keys reinforced when the disc track position matches."""
    ratio = title_similarity(a, b)
    if ratio >= 100.0 and a_no is not None and b_no is not None and a_no == b_no:
        ratio = min(100.0, ratio + 5.0)
    return ratio

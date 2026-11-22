"""Album-level similarity, compilation handling and dominant-album selection.

This module supplies the "album composition" signal described in spec #20-22:

* :func:`album_similarity` compares two release signatures track-by-track.
* :func:`is_compilation` labels "Various Artists" style releases.
* :func:`select_dominant_album` chooses which ambiguous "Greatest Hits" folder
  best represents the detected track set.

Results are scored 0..100 so they can plug into the engine's ``album_context``
signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from musicfusion.matching.normalize import is_compilation_artist, text_profile

#: generic compilation titles that say nothing about a release's *contents*
#: ("Greatest Hits", "Best Of", "The Essential", ...)
GENERIC_COMP_TITLES: frozenset[str] = frozenset(
    {
        "greatest hits", "greatest hit", "best of", "best", "the best",
        "hits", "hit", "the essential", "essential", "collection",
        "anthology", "complete", "from the vault", "the very best",
        "very best", "golden hits",
    }
)

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.matching.engine import AlbumContext


@dataclass
class TrackMember:
    """A lightweight, normalized member of an album."""

    title: str
    title_key: str
    artist: str = ""
    artist_key: str = ""
    disc_no: int | None = None
    track_no: int | None = None
    duration_ms: int = 0
    path: str = ""

    def sort_key(self) -> tuple[int, int]:
        return (self.disc_no or 0, self.track_no or 0)


@dataclass
class AlbumSignature:
    """Normalized identity of an album release."""

    title: str
    artist: str = ""
    title_key: str = ""
    artist_key: str = ""
    compilation: bool = False
    year: int | None = None
    members: tuple[TrackMember, ...] = ()
    total_tracks: int | None = None

    def sorted_members(self) -> list[TrackMember]:
        return sorted(self.members, key=TrackMember.sort_key)


@dataclass
class AlbumSimilarity:
    """Scored relationship between two album signatures."""

    score: float
    title_similarity: float
    position_coverage: float
    artist_similarity: float
    matched_positions: int
    compared_positions: int


def album_signature(
    *,
    title: str,
    artist: str = "",
    year: int | None = None,
    total_tracks: int | None = None,
    members: list[TrackMember] | None = None,
) -> AlbumSignature:
    """Build a normalized signature for an album release."""
    prof_title = text_profile(title)
    title_key = " ".join(t for t in prof_title.significant if not _is_year(t)) or prof_title.key
    artist_prof = text_profile(artist or "")
    return AlbumSignature(
        title=title,
        artist=artist,
        title_key=title_key,
        artist_key=artist_prof.key,
        compilation=is_compilation_artist(artist),
        year=year,
        members=tuple(members or ()),
        total_tracks=total_tracks,
    )


def member_from_track(
    *,
    title: str,
    artist: str = "",
    disc_no: int | None = None,
    track_no: int | None = None,
    duration_ms: int = 0,
    path: str = "",
) -> TrackMember:
    """Build a :class:`TrackMember` from a single track's metadata."""
    prof = text_profile(title)
    return TrackMember(
        title=title,
        title_key=prof.key,
        artist=artist,
        artist_key=text_profile(artist or "").key,
        disc_no=disc_no,
        track_no=track_no,
        duration_ms=duration_ms,
        path=path,
    )


def album_similarity(a: AlbumSignature, b: AlbumSignature) -> AlbumSimilarity:
    """Score how strongly two album signatures represent the same release."""
    title_sim = _token_sim(a.title_key, b.title_key)

    matched, compared = _track_overlap(a, b)
    coverage = matched / compared if compared else 0.0

    artist_agree = _artist_similarity(a, b)
    size_sim = _size_similarity(a, b)

    # A generic compilation title ("Greatest Hits") is weak evidence when the
    # tracklists barely overlap: two different "Greatest Hits" editions with
    # totally different songs should not get a large title credit (spec #21).
    if (a.title_key in GENERIC_COMP_TITLES or b.title_key in GENERIC_COMP_TITLES):
        title_credit = title_sim * min(1.0, 0.4 + 0.6 * coverage)
    else:
        title_credit = title_sim

    base = 0.35 * title_credit + 0.35 * coverage + 0.20 * artist_agree + 0.10 * size_sim
    return AlbumSimilarity(
        score=round(100.0 * base, 1),
        title_similarity=round(100.0 * title_sim, 1),
        position_coverage=round(100.0 * coverage, 1),
        artist_similarity=round(100.0 * artist_agree, 1),
        matched_positions=matched,
        compared_positions=compared,
    )


def album_context(a: AlbumSignature, b: AlbumSignature) -> AlbumContext:
    """Build an engine :class:`AlbumContext` from two album signatures."""
    from musicfusion.matching.engine import AlbumContext

    sim = album_similarity(a, b)
    return AlbumContext(
        similarity=sim.score,
        matched_tracks=sim.matched_positions,
        total_tracks=sim.compared_positions,
        model="album_composition",
    )


def select_dominant_album(signatures: list[AlbumSignature]) -> AlbumSignature | None:
    """Choose which release signature best represents a found album group.

    Implements the "dominant album" rule (#21): the winner must contain the
    other folders' content (composition agreement), not merely have the most
    files. An inflated 40+ track folder is penalized.
    """
    if not signatures:
        return None
    if len(signatures) == 1:
        return signatures[0]

    best: AlbumSignature | None = None
    best_score = -1.0
    for i, cand in enumerate(signatures):
        others = signatures[:i] + signatures[i + 1:]
        score = sum(album_similarity(cand, other).score for other in others)
        score += float(_reliable_track_count(cand))
        if _reliable_track_count(cand) > 40:
            score *= 0.95
        if score > best_score:
            best_score, best = score, cand
    return best


def _reliable_track_count(sig: AlbumSignature) -> int:
    """Track count dampened for inflated folders (extras, covers, etc.)."""
    n = sig.total_tracks or len(sig.members)
    return n if n <= 25 else int(25 + (n - 25) * 0.5)


def _is_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4


def _token_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(
        float(fuzz.token_sort_ratio(a, b)) / 100.0,
        float(fuzz.token_set_ratio(a, b)) / 100.0,
    )


def _track_overlap(a: AlbumSignature, b: AlbumSignature) -> tuple[int, int]:
    """Matched/compared track count, position-aware."""
    a_members = a.sorted_members()
    b_members = b.sorted_members()
    compared = max(len(a_members), len(b_members))
    if not a_members or not b_members:
        return 0, compared

    used: set[int] = set()
    matched = 0
    for x in a_members:
        best_idx: int | None = None
        best_score = -1.0
        for idx, y in enumerate(b_members):
            if idx in used:
                continue
            if x.disc_no is not None and y.disc_no is not None and x.disc_no != y.disc_no:
                continue
            score = _member_match(x, y)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is not None and best_score >= 0.7:
            used.add(best_idx)
            matched += 1
    return matched, compared


def _member_match(x: TrackMember, y: TrackMember) -> float:
    title_score = _token_sim(x.title_key, y.title_key)
    artist_score = 1.0 if (x.artist_key and x.artist_key == y.artist_key) else 0.0
    if not x.artist_key or not y.artist_key:
        artist_score = 0.5
    return 0.75 * title_score + 0.25 * artist_score


def _artist_similarity(a: AlbumSignature, b: AlbumSignature) -> float:
    if a.compilation or b.compilation:
        return 0.5
    if not a.artist_key or not b.artist_key:
        return 0.5
    return _token_sim(a.artist_key, b.artist_key)


def _size_similarity(a: AlbumSignature, b: AlbumSignature) -> float:
    n = a.total_tracks or len(a.members)
    m = b.total_tracks or len(b.members)
    if not n or not m:
        return 0.5
    return min(n, m) / max(n, m)


__all__ = [
    "AlbumSignature",
    "AlbumSimilarity",
    "TrackMember",
    "album_signature",
    "member_from_track",
    "album_similarity",
    "album_context",
    "select_dominant_album",
]

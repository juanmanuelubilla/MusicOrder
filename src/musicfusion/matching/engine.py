"""Core matching engine.

Computes how likely two audio files represent the same recording (song) and
returns an explainable :class:`MatchOutcome`. The engine never decides which
file to keep -> that belongs to the Decision/Fusion engines.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from rapidfuzz import fuzz as _fuzz

from musicfusion.matching.config import MatchConfig
from musicfusion.matching.normalize import (
    TextProfile,
    is_compilation_artist,
    is_year_token,
    primary_artist,
    text_profile,
)

logger = logging.getLogger(__name__)

#: fingerprints shorter than this are degenerate (ultra-short clips collapse
#: to a tiny shared value) and must not be treated as a positive match signal
MIN_FINGERPRINT_LEN = 16


class MatchRank(StrEnum):
    """Semantic classification of a match score."""

    CONFIRMED = "MATCH_CONFIRMED"
    VERY_HIGH = "MATCH_VERY_HIGH"
    HIGH = "MATCH_HIGH"
    POSSIBLE = "MATCH_POSSIBLE"
    LOW = "MATCH_LOW"
    NO_MATCH = "NO_MATCH"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class MatchTrack:
    """A comparable snapshot of a single audio file."""

    path: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    title: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    total_tracks: int | None = None
    year: int | None = None
    duration_ms: int = 0
    fingerprint: str = ""
    fingerprint_algorithm: str = ""
    isrc: str = ""
    recording_id: str = ""
    id: int | None = None


@dataclass
class AlbumContext:
    """Precomputed album-context evidence for a track pair."""

    similarity: float = 0.0
    matched_tracks: int = 0
    total_tracks: int = 0
    model: str = "album_composition"


@dataclass
class MatchOutcome:
    """Structured result of matching two files (see project spec #27)."""

    source_file: str
    candidate_file: str
    score: float = 0.0
    classification: str = MatchRank.NO_MATCH.value

    artist_score: float = 0.0
    album_score: float = 0.0
    title_score: float = 0.0
    track_number_score: float = 0.0
    disc_number_score: float = 0.0
    duration_score: float = 0.0
    year_score: float = 0.0
    fingerprint_score: float = 0.0
    album_context_score: float = 0.0

    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_review: bool = False
    matching_method: str = "weighted"

    # Semantic identity split (spec §13): both files may be the SAME SONG while
    # being a different EDITION (remaster) or a different VERSION (live/remix).
    # same_version is True only when no edition/version marker and no
    # meaningful gap set them apart.
    same_song: bool = False
    same_version: bool | None = None
    edition_different: bool = False
    version_different: bool = False
    version_tag: str = ""

    # Three-level identity (spec §18) + context evidence (spec §15):
    #   same_track   -> mergeable duplicate of the same recording/version
    #   same_album   -> both files belong to the same album title
    #   same_release -> same album AND compatible edition/year
    # context_score condenses artist/album/tracklist/duration/track evidence.
    same_track: bool = False
    same_album: bool = False
    same_release: bool = False
    context_score: float = 0.0
    candidate_phase: str = ""

    created_at: datetime = field(default_factory=datetime.utcnow)
    cache_key: str = ""

    def as_dict(self) -> dict[str, object]:
        """Serialize for storage or JSON endpoints."""
        return {
            "source_file": self.source_file,
            "candidate_file": self.candidate_file,
            "score": self.score,
            "classification": self.classification,
            "artist_score": self.artist_score,
            "album_score": self.album_score,
            "title_score": self.title_score,
            "track_number_score": self.track_number_score,
            "disc_number_score": self.disc_number_score,
            "duration_score": self.duration_score,
            "year_score": self.year_score,
            "fingerprint_score": self.fingerprint_score,
            "album_context_score": self.album_context_score,
            "context_score": self.context_score,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "requires_review": self.requires_review,
            "same_song": self.same_song,
            "same_version": self.same_version,
            "same_track": self.same_track,
            "same_album": self.same_album,
            "same_release": self.same_release,
            "edition_different": self.edition_different,
            "version_different": self.version_different,
            "version_tag": self.version_tag,
            "candidate_phase": self.candidate_phase,
            "matching_method": self.matching_method,
            "created_at": self.created_at.isoformat(),
            "cache_key": self.cache_key,
        }


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------


def _char_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(
        float(_fuzz.ratio(a, b)) / 100.0,
        float(_fuzz.partial_ratio(a, b)) / 100.0,
    )


def _token_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return max(
        float(_fuzz.token_sort_ratio(a, b)) / 100.0,
        float(_fuzz.token_set_ratio(a, b)) / 100.0,
    )


def edition_free_key(profile: TextProfile) -> str:
    """Significant token key with release years removed."""
    return " ".join(t for t in profile.significant if not is_year_token(t))


def number_fraction(left: int | None, right: int | None) -> float:
    """Track/disc fraction, neutral (0.5) when either side is missing."""
    if left is None or right is None:
        return 0.5
    if left == right:
        return 1.0
    if abs(left - right) <= 1:
        return 0.35
    return 0.0


def duration_fraction(
    l_ms: int,
    r_ms: int,
    *,
    relative_tolerance: float = 0.03,
    absolute_ms: float = 1500.0,
) -> float:
    """Duration similarity with adaptive tolerance for short/long songs."""
    if l_ms <= 0 or r_ms <= 0:
        return 0.5
    diff = abs(l_ms - r_ms)
    allowed = max(absolute_ms, relative_tolerance * max(l_ms, r_ms))
    if diff <= allowed:
        return 1.0
    ratio = diff / max(l_ms, r_ms)
    return max(0.0, 1.0 - 2.5 * ratio)


def year_fraction(left: int | None, right: int | None) -> float:
    if left is None or right is None:
        return 0.5
    if left == right:
        return 1.0
    if abs(left - right) <= 5:
        return 0.9
    return 0.4


def total_tracks_fraction(left: int | None, right: int | None) -> float:
    """Agreement on the album's total track count (neutral when unknown)."""
    if left is None or right is None:
        return 0.5
    if left == right:
        return 1.0
    if abs(left - right) <= 1:
        return 0.6
    return 0.0


def folder_fraction(left: MatchTrack, right: MatchTrack) -> float:
    """Similarity of the folder layout that contains each file.

    Combines three pieces of path evidence for ``Artista/Año - Álbum/``
    layouts: the album-folder name (weight 0.6), the artist-folder name
    (0.35) and a small year agreement bonus. Accent/case differences fold
    away, so ``Babasonicos`` ≡ ``Babasónicos`` and ``A Proposito`` ≡
    ``A Propósito``; genuinely different names score low.

    It is weak evidence only: it can add confidence but never blocks a match.
    """
    album_a = _folder_name_key(left.path)
    album_b = _folder_name_key(right.path)
    artist_a = _ancestor_folder_key(left.path)
    artist_b = _ancestor_folder_key(right.path)

    album_sim = 1.0 if album_a == album_b and album_a else _token_ratio(album_a, album_b)
    artist_sim = 1.0 if artist_a == artist_b and artist_a else _token_ratio(artist_a, artist_b)
    if not album_a and not album_b and not artist_a and not artist_b:
        return 0.0

    score = 0.65 * album_sim + 0.35 * artist_sim

    year_l, year_r = _folder_year(left.path), _folder_year(right.path)
    if year_l is not None and year_r is not None:
        if year_l == year_r:
            score = min(1.0, score + 0.05)
        elif abs(year_l - year_r) > 1:
            score *= 0.8
    return float(score)


def _folder_name_key(path: str) -> str:
    """Normalize the parent-folder name of a path for comparison."""
    parent = _ancestor_folder(path, 1)
    if not parent:
        return ""
    return text_profile(parent).base_key


def _ancestor_folder_key(path: str) -> str:
    """Normalize the grandparent (artist-level) folder name of a path."""
    ancestor = _ancestor_folder(path, 2)
    if not ancestor:
        return ""
    return text_profile(ancestor).base_key


def _folder_year(path: str) -> int | None:
    """Release year found as a YYYY token in the parent-folder name."""
    import re

    parent = _ancestor_folder(path, 1)
    match = re.search(r"\b((?:19|20)\d{2})\b", parent or "")
    return int(match.group(1)) if match else None


def _ancestor_folder(path: str, levels: int) -> str:
    import os

    current = os.path.dirname(path or "")
    for _ in range(levels - 1):
        current = os.path.dirname(current)
    return os.path.basename(current) if current else ""


def _parent_folder(path: str) -> str:
    import os

    parent = os.path.dirname(path or "")
    return os.path.basename(parent) if parent else ""


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


class MatchEngine:
    """Weighted, explainable track-to-track matching."""

    def __init__(self, config: MatchConfig) -> None:
        self.config = config
        self.weights = config.weighted()

    # ------------------------------------------------------------------ artist
    def _artist_fraction(self, left: MatchTrack, right: MatchTrack) -> tuple[float, str]:
        a = primary_artist(left.artist or left.album_artist)
        b = primary_artist(right.artist or right.album_artist)
        pa, pb = text_profile(a), text_profile(b)
        if not pa.key and not pb.key:
            return 0.5, "artist-unknown"
        if not pa.key or not pb.key:
            return 0.5, "artist-partial"
        if pa.key == pb.key or pa.compact == pb.compact:
            return 1.0, "artist-exact"
        score = max(float(_token_ratio(pa.key, pb.key)), float(_char_ratio(pa.folded, pb.folded)))
        return 0.25 + 0.75 * score, "artist-fuzzy"

    # ------------------------------------------------------------------- album
    def _album_fraction(self, left: MatchTrack, right: MatchTrack) -> tuple[float, str]:
        pa, pb = text_profile(left.album), text_profile(right.album)
        if not pa.key and not pb.key:
            return 0.5, "album-unknown"
        if not pa.key or not pb.key:
            return 0.5, "album-partial"
        base_a, base_b = edition_free_key(pa), edition_free_key(pb)
        if base_a == base_b or (pa.compact and pa.compact == pb.compact):
            return 1.0, "album-exact"
        ratio = float(_token_ratio(base_a, base_b))
        return 0.25 + 0.75 * ratio, "album-fuzzy"

    # ------------------------------------------------------------------- title
    def _title_result(
        self, left: MatchTrack, right: MatchTrack,
    ) -> tuple[float, bool, bool, bool]:
        """Return ``(fraction, edition_diff, version_diff, generic)``."""
        pa, pb = text_profile(left.title), text_profile(right.title)
        generic = pa.is_generic or pb.is_generic
        if not pa.words or not pb.words:
            return 0.5, False, False, generic
        same_song = pa.base_tokens == pb.base_tokens and bool(pa.base_tokens)
        edition_diff = pa.has_edition_marker != pb.has_edition_marker
        version_diff = pa.has_version_marker != pb.has_version_marker
        if same_song:
            if version_diff:
                return 0.90, edition_diff, True, generic
            if edition_diff:
                return 0.98, True, False, generic
            return 1.0, False, False, generic
        ratio = max(
            float(_token_ratio(pa.base_key, pb.base_key)),
            float(_char_ratio(pa.folded, pb.folded)),
        )
        return 0.25 + 0.75 * ratio, edition_diff, version_diff, generic

    # -------------------------------------------------------------- fingerprint
    def _fingerprint_state(self, left: MatchTrack, right: MatchTrack) -> tuple[str, float]:
        a, b = left.fingerprint, right.fingerprint
        if not a and not b:
            return "unknown", 0.5
        if a and b and a == b:
            if len(a) < MIN_FINGERPRINT_LEN:
                # degenerate fingerprint shared by unrelated ultra-short clips
                return "unknown", 0.5
            return "matched", 1.0
        return "mismatched", 0.0

    # ---------------------------------------------------------------- matching
    def match(
        self,
        left: MatchTrack,
        right: MatchTrack,
        context: AlbumContext | None = None,
    ) -> MatchOutcome:
        """Compute the structured match outcome for a track pair."""
        cfg = self.config
        out = MatchOutcome(source_file=left.path, candidate_file=right.path)

        art_f, art_kind = self._artist_fraction(left, right)
        alb_f, alb_kind = self._album_fraction(left, right)
        tit_f, edition_diff, version_diff, generic = self._title_result(left, right)
        trk_f = number_fraction(left.track_no, right.track_no)
        dis_f = number_fraction(left.disc_no, right.disc_no)
        dur_f = duration_fraction(
            left.duration_ms,
            right.duration_ms,
            relative_tolerance=self.config.duration_relative_tolerance,
            absolute_ms=self.config.duration_absolute_ms,
        )
        yr_f = year_fraction(left.year, right.year)
        fp_state, fp_f = self._fingerprint_state(left, right)

        # A large duration gap is hard evidence of a different VERSION even
        # when the titles carry no version marker (untagged live takes, radio
        # edits...): the same composition recorded live/edit runs measurably
        # longer/shorter than the studio master. Only relevant when both
        # titles point at the same song identity - between unrelated songs a
        # duration gap is just more NO_MATCH evidence.
        dur_gap = _duration_gap(left.duration_ms, right.duration_ms)
        titles_align = _same_song(left, right) or tit_f >= 0.85
        duration_version_diff = dur_gap >= cfg.version_duration_gap and titles_align
        album_version_tag = _album_version_gap(left, right) if titles_align else ""
        album_version_diff = bool(album_version_tag)

        # total-track-count evidence (#10) and folder-path evidence (#14):
        # additive confidence boosters, never allowed to make a match by themselves
        tt_f = total_tracks_fraction(left.total_tracks, right.total_tracks)
        folder_f = folder_fraction(left, right)

        dims = {
            "artist": art_f,
            "album": alb_f,
            "title": tit_f,
            "track_number": trk_f,
            "disc_number": dis_f,
            "duration": dur_f,
            "year": yr_f,
        }
        score = sum(self.weights[key] * value for key, value in dims.items())

        out.artist_score = round(art_f * 100.0, 1)
        out.album_score = round(alb_f * 100.0, 1)
        out.title_score = round(tit_f * 100.0, 1)
        out.track_number_score = round(trk_f * 100.0, 1)
        out.disc_number_score = round(dis_f * 100.0, 1)
        out.duration_score = round(dur_f * 100.0, 1)
        out.year_score = round(yr_f * 100.0, 1)
        out.fingerprint_score = round(fp_f * 100.0, 1)

        if fp_state == "matched":
            score = min(100.0, score + cfg.fingerprint_boost)
            out.reasons.append("Fingerprint compatible (mismo contenido de audio)")
        elif fp_state == "mismatched":
            score = max(0.0, score - cfg.fingerprint_penalty)
            out.warnings.append(
                "Los fingerprints no coinciden; puede ser una versión diferente"
            )

        # A "Greatest Hits"-style album that matches by name only must not be
        # treated as strong evidence when the ARTISTS are clearly different
        # (spec #7): address the risk of two distinct song/album pairs sharing
        # one generic compilation title.
        if (alb_kind == "album-exact"
                and art_kind in {"artist-partial", "artist-fuzzy"}
                and art_f < 0.5
                and not is_compilation_artist(left.artist)
                and not is_compilation_artist(right.artist)):
            alb_f_discounted = alb_f * 0.45
            score = score - (20.0 * (alb_f - alb_f_discounted))
            alb_f = alb_f_discounted
            out.album_score = round(alb_f * 100.0, 1)
            out.reasons.append(
                "Álbum con nombre genérico ('Greatest Hits' similar) y artistas "
                "distintos: señal de álbum atenuada"
            )

        context_sim = 0.0
        if context is not None:
            context_sim = float(context.similarity)
            out.album_context_score = round(context_sim, 1)
            if context_sim > 0.6:
                score = min(100.0, score + cfg.album_context_weight * context_sim / 100.0)
                out.reasons.append(
                    f"Contexto de álbum consistente ({context.matched_tracks}/"
                    f"{context.total_tracks} pistas coincidentes)"
                )
            elif context_sim > 0.0:
                out.warnings.append(f"El contexto de álbum difiere (similitud {context_sim:.0f}%)")

        elif context_sim > 0.0:
                out.warnings.append(f"El contexto de álbum difiere (similitud {context_sim:.0f}%)")

        # additive track-count and folder-path evidence: raise confidence when
        # they reinforce the rest, but never let them create a match alone.
        if left.total_tracks is not None and right.total_tracks is not None:
            if tt_f >= 1.0:
                score = min(100.0, score + cfg.total_tracks_bonus)
                out.reasons.append("Número total de pistas coherente")
            elif tt_f <= 0.0:
                score = max(0.0, score - cfg.total_tracks_penalty)
                out.warnings.append("El total de pistas difiere mucho (posibles ediciones distintas)")
        if folder_f >= 0.55:
            score = min(100.0, score + cfg.folder_bonus * folder_f)
            out.reasons.append(f"Estructura de carpeta comparable ({folder_f * 100.0:.0f}%)")

        # caps to avoid false confidence on editions/versions
        version_any = bool(version_diff) or duration_version_diff or album_version_diff
        if fp_state != "matched":
            if version_any:
                score = min(score, cfg.version_cap)
                out.requires_review = True
                if album_version_diff and not version_diff:
                    out.warnings.append(
                        "El álbum indica una versión distinta "
                        f"('{album_version_tag}'): directo/en vivo vs estudio"
                    )
                elif duration_version_diff and not version_diff:
                    out.warnings.append(
                        "Duración muy distinta "
                        f"({dur_gap * 100:.0f}%): probable versión diferente "
                        "(directo/edit) sin marcador en el título"
                    )
                else:
                    out.reasons.append("Versión diferente (live/remix/...) -> requiere revisión")
            elif edition_diff:
                score = min(score, cfg.edition_cap)
                out.warnings.append("Posible edición diferente (remaster/edición)")
            elif _edition_present(left, right):
                score = min(score, cfg.edition_edit_cap)
                out.warnings.append("Posible diferencia de edición sutil")

        score = max(0.0, min(100.0, score))
        out.score = round(score, 1)
        out.classification = rank(score, cfg)

        if generic:
            out.requires_review = True
            out.reasons.append("Título genérico o sin nombre: no confirmar sin contexto")
            if context_sim <= 0.0:
                out.classification = MatchRank.NEEDS_REVIEW.value
                out.warnings.append(
                    "Metadata o título insuficientes para confirmar; archivos como "
                    "'Intro'/'Untitled' pueden pertenecer a álbumes distintos."
                )

        out.reasons.append(f"Señales: {art_kind}, {alb_kind}")

        # --- semantic identity split (spec §13) --------------------------------
        # Same song = exact normalized identity OR near-identical titles backed
        # by matching artist AND album ("Muñeco" vs untagged typo "Mueco"):
        # the context carries the identity when the string cannot (spec §28).
        exact_same = _same_song(left, right)
        contextual_same = tit_f >= 0.88 and art_f >= 0.95 and alb_f >= 0.95
        out.same_song = bool(exact_same or contextual_same)
        if contextual_same and not exact_same:
            out.reasons.append(
                "Identidad contextual: título casi igual con artista y álbum coincidentes"
            )
        out.edition_different = bool(edition_diff) or _edition_present(left, right)
        out.version_different = bool(version_diff) or duration_version_diff or album_version_diff
        if out.version_different:
            out.version_tag = (
                _version_tag(left, right)
                or (album_version_tag if album_version_diff else "")
                or ("duración" if duration_version_diff else "")
            )
        out.same_version = (
            out.same_song
            and not out.edition_different
            and not out.version_different
            and not generic
        )

        # --- three-level identity + context evidence -----------------------
        out.context_score = round(
            100.0
            * (
                0.30 * art_f
                + 0.25 * alb_f
                + 0.20 * context_sim
                + 0.10 * dur_f
                + 0.10 * trk_f
                + 0.05 * yr_f
            ),
            1,
        )
        out.same_track = out.same_song and not out.version_different
        # compilation guard: identical generic album names ("Greatest Hits")
        # only count when the artists also agree strongly
        out.same_album = (
            art_f >= 0.85 and alb_f >= 0.85
        ) and not (
            is_compilation_artist(left.artist) ^ is_compilation_artist(right.artist)
        )
        out.same_release = out.same_album and not out.version_different and year_fraction(
            left.year, right.year
        ) >= 0.5
        from musicfusion.matching.storage import cache_key, config_fingerprint

        out.cache_key = cache_key(left, right, config_fingerprint(cfg))
        return out


def _same_song(left: MatchTrack, right: MatchTrack) -> bool:
    """True when both titles reduce to the same base (song) identity."""
    pa = text_profile(left.title)
    pb = text_profile(right.title)
    return bool(pa.base_tokens) and pa.base_tokens == pb.base_tokens


def _duration_gap(l_ms: int, r_ms: int) -> float:
    """Relative duration difference (0..1); 0.0 when either side is unknown."""
    if l_ms <= 0 or r_ms <= 0:
        return 0.0
    return abs(l_ms - r_ms) / max(l_ms, r_ms)


def _album_version_gap(left: MatchTrack, right: MatchTrack) -> str:
    """Version token present in ONE album name but not the other, or ''.

    A live/unplugged/directo marker on a single side of the album field is
    strong evidence that the two files are different performances. Plain album
    differences (original vs compilation) do NOT count - only version tokens.
    """
    la = frozenset(text_profile(left.album).version_tokens)
    lb = frozenset(text_profile(right.album).version_tokens)
    extra = lb - la or la - lb
    return sorted(extra)[0] if extra else ""


def _version_tag(left: MatchTrack, right: MatchTrack) -> str:
    """Return the version token that separates the two titles, or ''."""
    pa = text_profile(left.title)
    pb = text_profile(right.title)
    for tok in pb.version_tokens:
        if tok not in pa.version_tokens:
            return tok
    for tok in pa.version_tokens:
        if tok not in pb.version_tokens:
            return tok
    return ""


def _edition_present(left: MatchTrack, right: MatchTrack) -> bool:
    """True when at least one title carries an edition marker."""
    return text_profile(left.title).has_edition_marker or text_profile(right.title).has_edition_marker


# ---------------------------------------------------------------------------
# classification + explanation helpers
# ---------------------------------------------------------------------------


def rank(score: float, config: MatchConfig) -> str:
    if score >= config.threshold("confirmed_threshold"):
        return MatchRank.CONFIRMED.value
    if score >= config.threshold("very_high_threshold"):
        return MatchRank.VERY_HIGH.value
    if score >= config.threshold("high_threshold"):
        return MatchRank.HIGH.value
    if score >= config.threshold("possible_threshold"):
        return MatchRank.POSSIBLE.value
    if score >= config.threshold("low_threshold"):
        return MatchRank.LOW.value
    return MatchRank.NO_MATCH.value


def explain(outcome: MatchOutcome) -> str:
    """Human-readable explanation of a match outcome."""
    lines = [
        f"MATCH {outcome.score:.1f}% ({outcome.classification})",
        f"  Artista: {outcome.artist_score:.0f}%",
        f"  Álbum:   {outcome.album_score:.0f}%",
        f"  Título:  {outcome.title_score:.0f}%",
        f"  Pista:   {outcome.track_number_score:.0f}%",
        f"  Disco:   {outcome.disc_number_score:.0f}%",
        f"  Duración:{outcome.duration_score:.0f}%",
        f"  Año:     {outcome.year_score:.0f}%",
        f"  Fingerprint: {outcome.fingerprint_score:.0f}%",
    ]
    if outcome.album_context_score:
        lines.append(f"  Contexto de álbum: {outcome.album_context_score:.0f}%")
    if outcome.context_score:
        lines.append(f"  Contexto total: {outcome.context_score:.0f}%")
    identity = (
        f"same_track={outcome.same_track} same_album={outcome.same_album} "
        f"same_release={outcome.same_release}"
    )
    lines.append(f"  {identity}")
    if outcome.reasons:
        lines.append("Razones:")
        lines.extend(f"  - {r}" for r in outcome.reasons)
    if outcome.warnings:
        lines.append("Advertencias:")
        lines.extend(f"  - {w}" for w in outcome.warnings)
    if outcome.requires_review:
        lines.append("=> Requiere revisión manual.")
    return "\n".join(lines)


def explain_rejection(outcome: MatchOutcome) -> str:
    """Explain WHY a candidate was rejected (never just "No match").

    Format (spec §25): per-signal percentages plus the final verdict so the
    user can see which signal blocked the match.
    """
    blocking: list[str] = []
    if outcome.artist_score < 75.0:
        blocking.append(f"artista bajo ({outcome.artist_score:.0f}%)")
    if outcome.album_score < 70.0:
        blocking.append(f"álbum bajo ({outcome.album_score:.0f}%)")
    if outcome.title_score < 62.0:
        blocking.append(f"título bajo ({outcome.title_score:.0f}%)")
    if outcome.version_different:
        blocking.append(f"versión distinta ('{outcome.version_tag or 'sí'}')")
    fingerprint_note = (
        "no disponible" if outcome.fingerprint_score == 50.0
        else ("compatible" if outcome.fingerprint_score >= 100 else "no coincide")
    )
    header = "Candidato rechazado porque: " + ("; ".join(blocking) if blocking else "score insuficiente")
    lines = [
        header,
        f"  Artista: {outcome.artist_score:.0f}%  Álbum: {outcome.album_score:.0f}%  "
        f"Título: {outcome.title_score:.0f}%  Duración: {outcome.duration_score:.0f}%",
        f"  Fingerprint: {fingerprint_note}",
        f"  Final: {outcome.score:.1f}% -> {outcome.classification}",
    ]
    return "\n".join(lines)


def has_version_diff(left: MatchTrack, right: MatchTrack) -> bool:
    """True when the same base song has a different version marker."""
    pa, pb = text_profile(left.title), text_profile(right.title)
    same_base = bool(pa.base_tokens) and pa.base_tokens == pb.base_tokens
    return same_base and pa.has_version_marker != pb.has_version_marker


def edition_paint_match(left: MatchTrack, right: MatchTrack) -> bool:
    """True when only edition markers separate the two titles."""
    pa, pb = text_profile(left.title), text_profile(right.title)
    same_base = bool(pa.base_tokens) and pa.base_tokens == pb.base_tokens
    return same_base and pa.has_edition_marker != pb.has_edition_marker


def generic_source(track: MatchTrack) -> bool:
    """True when a track carries a generic/anonymous title."""
    return text_profile(track.title).is_generic

"""Decision Engine implementation.

The engine decides "which file to keep" for a *group of matching candidates*.
It consumes a lightweight projection of each candidate (see :class:`Candidate`)
so it stays decoupled from the database layer, and it combines configurable
dimensions: audio quality, metadata richness, embedded artwork, source, codec
preferences and library priority.

Design contract (see docs/COMANDOS.md, sección Comparar):

* Matching Engine answers ``same_content``; this engine never changes that.
* This engine answers ``winner``; the Fusion Engine executes the filesystem
  action afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_WEIGHTS: dict[str, float] = {
    "quality": 40.0,
    "metadata": 20.0,
    "artwork": 15.0,
    "codec": 15.0,
    "preference": 10.0,
}

#: Codecs ordered from most to least preferred when nothing configurable says
#: otherwise (lossless > lossy-by-bit-depth > legacy formats).
CODEC_PREFERENCE: tuple[str, ...] = (
    "truehd", "dts", "alac", "flac", "ape", "wavpack",
    "pcm_s24le", "pcm_s32le", "pcm_s16le",
    "opus", "aac", "mp3", "vorbis", "wma", "wav",
)


@dataclass
class Candidate:
    """A normalized snapshot of one file in a match group."""

    path: str
    source: str = ""
    library_priority: float = 0.0
    preference: float = 0.0
    quality_score: float = 0.0
    metadata_score: float = 0.0
    has_embedded_art: bool = False
    codec: str = ""
    bit_depth: int | None = None
    sample_rate_hz: int | None = None
    bitrate_kbps: int | None = None
    replay_gain: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_track(cls, track: Any) -> Candidate:
        """Build a candidate from a DB ``Track`` row.

        Reads only scalar columns so it also works on detached instances (the
        call may run outside the ORM session that loaded the row); the lazy
        ``track.library`` relationship is never touched.
        """
        return cls(
            path=track.path,
            source=str(track.library_id) if track.library_id else "",
            quality_score=track.quality_score or 0.0,
            metadata_score=track.metadata_score or 0.0,
            has_embedded_art=bool(track.has_embedded_art),
            codec=track.codec or "",
            bit_depth=track.bit_depth,
            sample_rate_hz=track.sample_rate_hz,
            bitrate_kbps=track.bitrate_kbps,
            replay_gain=track.replay_gain,
        )


@dataclass
class DecisionResult:
    """Per-candidate scoring and the chosen winner."""

    winner: Candidate
    runner_up: Candidate | None
    scores: dict[str, float]
    reasons: list[str]
    warnings: list[str]

    @property
    def winner_path(self) -> str:
        return self.winner.path

    def as_dict(self) -> dict[str, object]:
        """Serialize the decision for storage, JSON endpoints and the GUI."""
        return {
            "winner_path": self.winner.path,
            "winner_score": self.scores.get(self.winner.path, 0.0),
            "runner_up_path": self.runner_up.path if self.runner_up else None,
            "runner_up_score": (
                self.scores.get(self.runner_up.path, 0.0) if self.runner_up else 0.0
            ),
            "scores": dict(self.scores),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "requires_review": bool(self.warnings),
        }


def _codec_score(codec: str, weight: float) -> float:
    codec = (codec or "").lower()
    if not codec:
        return weight * 0.5
    try:
        idx = CODEC_PREFERENCE.index(codec)
    except ValueError:
        idx = len(CODEC_PREFERENCE)
    return weight * (1.0 - (idx / max(1, len(CODEC_PREFERENCE))))


class DecisionEngine:
    """Picks the best copy among the members of a match group."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.total_w = sum(self.weights.values()) or 1.0

    @staticmethod
    def from_settings(settings: Any) -> DecisionEngine:
        """Build the engine from the ``decision`` section of the settings."""
        data = dict(settings.get("decision.weights", {}) or DEFAULT_WEIGHTS)
        merged = dict(DEFAULT_WEIGHTS)
        merged.update(data)
        return DecisionEngine(weights=merged)

    def decide(self, candidates: list[Candidate]) -> DecisionResult:
        """Return the winning candidate plus a decision breakdown."""
        if not candidates:
            raise ValueError("decision group cannot be empty")
        if len(candidates) == 1:
            return DecisionResult(
                winner=candidates[0],
                runner_up=None,
                scores={candidates[0].path: 0.0},
                reasons=["Único candidato: no hay alternativa que elegir"],
                warnings=[],
            )

        scored: dict[str, float] = {}
        breakdown: dict[str, dict[str, float]] = {}
        for cand in candidates:
            dims = self._score(cand)
            total = round(sum(dims.values()), 2)
            scored[cand.path] = total
            breakdown[cand.path] = dims

        ordered = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
        winner_path = ordered[0][0]
        runner_path = ordered[1][0] if len(ordered) > 1 else None

        winner = next(c for c in candidates if c.path == winner_path)
        runner = next((c for c in candidates if c.path == runner_path), None)
        return DecisionResult(
            winner=winner,
            runner_up=runner,
            scores={k: v for k, v in scored.items()},
            reasons=_explain(breakdown[winner_path]),
            warnings=_warnings(scored, winner.path, runner.path if runner else None),
        )

    def _score(self, cand: Candidate) -> dict[str, float]:
        dims = {
            "quality": (cand.quality_score / 100.0) * self.weights["quality"],
            "metadata": (cand.metadata_score / 100.0) * self.weights["metadata"],
            "artwork": self.weights["artwork"] if cand.has_embedded_art else 0.0,
            "codec": _codec_score(cand.codec, self.weights["codec"]),
            "preference": (cand.preference / 100.0) * self.weights["preference"],
        }
        return {k: round(v, 2) for k, v in dims.items()}


def _explain(dims: dict[str, float]) -> list[str]:
    return [f"{name}: {value:.1f}" for name, value in dims.items() if value > 0] or [
        "sin señales positivas"
    ]


def _warnings(scores: dict[str, float], winner: str, runner: str | None) -> list[str]:
    if runner is None:
        return []
    gap = scores[winner] - scores[runner]
    if gap < 1.0:
        return ["Los candidatos están muy igualados; considerar revisión humana"]
    return []

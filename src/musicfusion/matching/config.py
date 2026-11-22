"""Configuration for the matching engine.

All parameters come from the ``matching`` section of the YAML settings so the
engine never hard-codes magic numbers. ``MatchConfig`` is a plain dataclass
plus a ``.from_settings()`` builder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from musicfusion.config.settings import Settings

# --------------------------------------------------------------------------
# default weights (percent of the "base" 100-point budget)
# --------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "artist": 20.0,
    "album": 20.0,
    "title": 30.0,
    "track_number": 10.0,
    "disc_number": 5.0,
    "duration": 10.0,
    "year": 5.0,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "confirmed_threshold": 95.0,
    "very_high_threshold": 90.0,
    "high_threshold": 80.0,
    "possible_threshold": 70.0,
    "low_threshold": 50.0,
}


@dataclass
class MatchConfig:
    """Tunable matching parameters loaded from settings."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    fingerprint_boost: float = 35.0
    fingerprint_penalty: float = 15.0
    album_context_weight: float = 10.0

    # additive evidence: how strongly a coherent total-track-count or a
    # comparable folder path raises confidence (0..100 budget points)
    total_tracks_bonus: float = 5.0
    total_tracks_penalty: float = 5.0
    folder_bonus: float = 5.0

    duration_relative_tolerance: float = 0.03
    duration_absolute_ms: float = 1500.0

    # relative duration gap that by itself signals a DIFFERENT VERSION of the
    # same song (live takes are typically far longer than studio recordings)
    version_duration_gap: float = 0.12

    # candidate generation (NOT matching thresholds): how similar two artists /
    # two albums must be before their tracks even reach the Matching Engine
    artist_candidate_threshold: float = 0.75
    album_candidate_threshold: float = 0.80

    # caps applied when the *same song* is recognised but a different edition
    # or a different version might be at play
    edition_edit_cap: float = 96.0
    edition_cap: float = 94.0
    version_cap: float = 93.0

    # title scoring parameters
    title_exact: float = 100.0

    @property
    def total_weight(self) -> float:
        return float(sum(self.weights.values()) or 1.0)

    def weighted(self) -> dict[str, float]:
        """Return dimensions normalized so they sum to 100."""
        total = self.total_weight
        return {key: 100.0 * value / total for key, value in self.weights.items()}

    def threshold(self, name: str) -> float:
        return float(self.thresholds.get(name, 50.0))

    @classmethod
    def from_settings(cls, settings: Settings | dict[str, object]) -> MatchConfig:
        """Build a config from a :class:`Settings` (or a mapping-like object).

        Falls back to defaults for any missing key, so a minimal YAML works.
        """
        if isinstance(settings, dict):
            get = _make_dict_get(settings)
        else:
            get = settings.get

        def _num(dotted: str, default: float) -> float:
            try:
                return float(get(dotted, default))
            except (TypeError, ValueError):
                return default

        weights = DEFAULT_WEIGHTS.copy()
        for name in weights:
            weights[name] = _num(f"matching.{name}_weight", weights[name])

        thresholds = DEFAULT_THRESHOLDS.copy()
        for name in thresholds:
            thresholds[name] = _num(f"matching.{name}", thresholds[name])

        return cls(
            weights=weights,
            thresholds=thresholds,
            fingerprint_boost=_num("matching.fingerprint_weight", 35.0),
            fingerprint_penalty=_num("matching.fingerprint_penalty", 15.0),
            album_context_weight=_num("matching.album_context_weight", 10.0),
            total_tracks_bonus=_num("matching.total_tracks_faith", 5.0),
            total_tracks_penalty=_num("matching.total_tracks_inconsistency", 5.0),
            folder_bonus=_num("matching.folder_weight", 5.0),
            duration_relative_tolerance=_num("matching.duration_relative_tolerance", 0.03),
            duration_absolute_ms=_num("matching.duration_absolute_ms", 1500.0),
            version_duration_gap=_num("matching.version_duration_gap", 0.12),
            artist_candidate_threshold=_num("candidates.artist_threshold", 0.75),
            album_candidate_threshold=_num("candidates.album_threshold", 0.80),
            edition_edit_cap=_num("matching.edition_edit_cap", 96.0),
            edition_cap=_num("matching.edition_cap", 94.0),
            version_cap=_num("matching.version_cap", 93.0),
        )


def _dict_get(data: dict[str, object], dotted: str, default: object = None) -> object:
    """Dotted lookup on a plain mapping (used when settings is a dict)."""
    parts = dotted.split(".")
    node: object = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _make_dict_get(data: dict[str, object]) -> Any:
    def getter(dotted: str, default: object = None) -> object:
        return _dict_get(data, dotted, default)

    return getter


__all__ = ["MatchConfig", "DEFAULT_WEIGHTS", "DEFAULT_THRESHOLDS"]

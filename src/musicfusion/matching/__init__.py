"""Matching Engine.

Determines how likely two audio files represent the same recording using a
configurable, explainable, multi-signal scoring flow:

* normalization ("Don't Stop Me Now" vs "DONT STOP ME NOW")
* fuzzy artist/album/title matching (RapidFuzz)
* track/disc number, duration and year signals
* optional audio-fingerprint evidence and album-context reinforcement
* hashed cache keyed by the full comparison input

The engine only answers *"are these the same content?"*; deciding which file
to keep is the Decision Engine's responsibility.
"""

from __future__ import annotations

from musicfusion.matching.album import (
    AlbumSignature,
    AlbumSimilarity,
    TrackMember,
    album_context,
    album_signature,
    album_similarity,
    member_from_track,
    select_dominant_album,
)
from musicfusion.matching.config import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    MatchConfig,
)
from musicfusion.matching.engine import (
    AlbumContext,
    MatchEngine,
    MatchOutcome,
    MatchRank,
    MatchTrack,
    duration_fraction,
    explain,
    folder_fraction,
    number_fraction,
    rank,
    total_tracks_fraction,
    year_fraction,
)
from musicfusion.matching.normalize import (
    TextProfile,
    is_compilation_artist,
    primary_artist,
    text_profile,
)
from musicfusion.matching.storage import MatchStore, cache_key, config_fingerprint

__all__ = [
    "AlbumContext",
    "AlbumSignature",
    "AlbumSimilarity",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_WEIGHTS",
    "MatchConfig",
    "MatchEngine",
    "MatchOutcome",
    "MatchRank",
    "MatchStore",
    "MatchTrack",
    "TextProfile",
    "TrackMember",
    "album_context",
    "album_signature",
    "album_similarity",
    "cache_key",
    "config_fingerprint",
    "duration_fraction",
    "explain",
    "folder_fraction",
    "is_compilation_artist",
    "member_from_track",
    "number_fraction",
    "primary_artist",
    "rank",
    "select_dominant_album",
    "text_profile",
    "total_tracks_fraction",
    "year_fraction",
]

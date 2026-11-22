"""Tests for the Decision Engine (spec §13-14).

Covers: winner selection by audio quality / artwork / codec, the
"single match group" passthrough, the separation contract (quality must NOT
influence the matching result) and the fusion-plan projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from musicfusion.compare.similarity import duration_similarity
from musicfusion.decision.engine import Candidate, DecisionEngine


@dataclass
class _FakeLibrary:
    path: str


@dataclass
class _FakeTrack:
    path: str
    quality_score: float = 0.0
    metadata_score: float = 0.0
    has_embedded_art: bool = False
    codec: str = ""
    bit_depth: int | None = None
    sample_rate_hz: int | None = None
    bitrate_kbps: int | None = None
    replay_gain: float | None = None
    library: _FakeLibrary | None = None
    library_id: int | None = None

    def quality(self) -> float:
        return self.quality_score

    def metadata(self) -> float:
        return self.metadata_score


def _candidate(path: str, **kw) -> Candidate:
    return Candidate.from_track(_FakeTrack(path=path, **kw))


def test_decision_prefers_best_quality() -> None:
    dec = DecisionEngine()
    low = _candidate("/a.flac", quality_score=60.0, metadata_score=80.0)
    hi = _candidate("/b.flac", quality_score=95.0, metadata_score=75.0)
    out = dec.decide([low, hi])
    assert out.winner is hi
    assert hi.path in out.scores
    assert "quality" in "".join(out.reasons).lower()


def test_decision_uses_artwork_to_break_ties() -> None:
    dec = DecisionEngine()
    no_art = _candidate("/a.flac", quality_score=90.0, has_embedded_art=False)
    with_art = _candidate("/b.flac", quality_score=90.0, has_embedded_art=True)
    out = dec.decide([no_art, with_art])
    assert out.winner is with_art


def test_decision_single_candidate_is_passthrough() -> None:
    dec = DecisionEngine()
    only = _candidate("/only.flac")
    out = dec.decide([only])
    assert out.winner is only
    assert out.runner_up is None


def test_decision_empty_group_raises() -> None:
    with pytest.raises(ValueError):
        DecisionEngine().decide([])


def test_decision_pick_better_example() -> None:
    """Running-from-spec #14 test 2: FLAC16/44.1 vs FLAC24/96 -> winner=B."""
    dec = DecisionEngine()
    a = _candidate("/a.flac", quality_score=70.0, codec="flac",
                   bit_depth=16, sample_rate_hz=44100, metadata_score=82.0)
    b = _candidate("/b.flac", quality_score=95.0, codec="flac",
                   bit_depth=24, sample_rate_hz=96000, metadata_score=98.0)
    out = dec.decide([a, b])
    assert out.winner is b


def test_quality_split_across_engines() -> None:
    """Identity matching must ignore quality; the decision engine must use it."""
    from musicfusion.compare.similarity import DEFAULT_WEIGHTS as MATCH_WEIGHTS
    from musicfusion.decision.engine import DEFAULT_WEIGHTS as DECISION_WEIGHTS

    assert "quality" not in MATCH_WEIGHTS          # matching == same_content
    assert "metadata" not in MATCH_WEIGHTS          # metadata richness too
    assert "quality" in DECISION_WEIGHTS            # decision == which to keep


def test_matching_not_contaminated_by_quality() -> None:
    """SP1 spec §14 (test 1): same song with different quality -> still MATCH."""
    from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack

    engine = MatchEngine(MatchConfig())
    a = MatchTrack(artist="Queen", album="Greatest Hits", title="We Will Rock You",
                   track_no=1, disc_no=1, duration_ms=122_000)
    b = MatchTrack(artist="Queen", album="Greatest Hits", title="We Will Rock You",
                   track_no=1, disc_no=1, duration_ms=122_000,
                   recording_id="fp-abc")
    out = engine.match(a, b)
    assert out.classification in {"MATCH_CONFIRMED", "MATCH_VERY_HIGH"}
    # quality is not a matching signal: identical names + same song
    assert out.artist_score > 0 and out.title_score > 0


def test_fusion_winner_uses_decision_engine() -> None:
    """SP1 spec: #14 test 4: live vs studio -> decision does not force merge."""
    from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack

    engine = MatchEngine(MatchConfig())
    studio = MatchTrack(artist="Depeche Mode", album="Violator",
                        title="Enjoy the Silence", track_no=5, duration_ms=198_000)
    live = MatchTrack(artist="Depeche Mode", album="Violator",
                      title="Enjoy the Silence (Live)", track_no=5, duration_ms=202_000)
    out = engine.match(studio, live)
    assert out.requires_review is True
    assert out.score <= engine.config.version_cap


def test_decision_from_track_detached_no_lazy_load() -> None:
    """Regression: Candidate.from_track must not lazy-load the ORM relation."""
    track = _FakeTrack(path="/detached.flac", quality_score=88.0, codec="flac")
    track.library_id = 7  # scalar column; no relationship to touch
    cand = Candidate.from_track(track)
    assert cand.path == "/detached.flac"
    assert cand.quality_score == 88.0
    assert cand.source == "7"


def test_duration_similarity_outage_fraction() -> None:
    # spec: 122s vs 247s strongly reduce confidence
    assert duration_similarity(122_000, 247_000) == 0.0
    assert duration_similarity(122_000, 123_000) == 1.0


def test_different_songs_never_reach_decision() -> None:
    """SP1 spec §14 (test 3): distinct songs -> same_content=False, no winner chosen."""
    from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack

    out = MatchEngine(MatchConfig()).match(
        MatchTrack(artist="Queen", album="Greatest Hits",
                   title="We Will Rock You", track_no=1, duration_ms=122_000),
        MatchTrack(artist="Queen", album="Greatest Hits",
                   title="We Are The Champions", track_no=7, duration_ms=180_000),
    )
    # Different songs: never reaches the "possible duplicate" tier, so no
    # decision is required -> requires_review stays off.
    assert out.score < MatchConfig().threshold("possible_threshold")
    assert out.score >= MatchConfig().threshold("low_threshold") or out.classification == "NO_MATCH"
    assert not out.requires_review


def test_decision_store_persists_result(tmp_path) -> None:
    """Decision engines results are persisted so they can be audited later."""
    from musicfusion.db.session import Database
    from musicfusion.decision.storage import DecisionStore, group_key

    engine = DecisionEngine()
    result = engine.decide([
        Candidate(path="/a.mp3", quality_score=92.0, codec="mp3"),
        Candidate(path="/b.flac", quality_score=99.0, codec="flac"),
    ])

    db = Database(Path(str(tmp_path)) / "decisions.db")
    store = DecisionStore(db)
    record = store.save(
        result,
        weights=engine.weights,
        match_ids=[7, 8],
        group_key=group_key("/a.mp3", "/b.flac"),
    )
    assert record.winner_path == "/b.flac"
    assert record.winner_score > record.runner_up_score
    assert record.reasons
    assert record.weights == engine.weights
    assert record.match_ids == [7, 8]
    assert store.list_recent()[0].id == record.id

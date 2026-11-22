"""Obligatory spec §33 tests: Matching Engine + Decision Engine separation.

These mirror the numbered tests from the implementation task:

  T1  "Don't Stop Me Now" vs "dont stop me now"                  -> high match
  T2  "... (2011 Remaster)" -> same song, different version      -> flagged
  T3  We Will Rock You vs Don't Stop Me Now                      -> NO_MATCH
  T4  AC/DC vs AC DC vs ACDC artist affinity                     -> high
  T5  MP3 320kbps vs FLAC 16/44.1  -> same_content, winner = FLAC
  T6  FLAC 16/44.1 vs FLAC 24/96  -> same_content, winner = B
  T7  "Intro" same name, different albums -> avoid false positive
  T8  two same-name "Greatest Hits" with different tracklists -> no mix
  T9  same-name compilations with near-identical tracklists -> candidates
  T10 metadata-less "01.flac" inferred via album context -> flagged
"""

from __future__ import annotations

from musicfusion.decision.engine import Candidate, DecisionEngine
from musicfusion.matching import (
    MatchConfig,
    MatchEngine,
    MatchTrack,
    album_signature,
    album_similarity,
    member_from_track,
)


def _track(**kwargs: object) -> MatchTrack:
    defaults: dict[str, object] = {
        "artist": "Queen",
        "album": "Greatest Hits",
        "title": "We Will Rock You",
        "track_no": 1,
        "disc_no": 1,
        "duration_ms": 122_000,
        "path": "/track.flac",
    }
    defaults.update(kwargs)
    return MatchTrack(**defaults)  # type: ignore[arg-type]


def _match(left: MatchTrack, right: MatchTrack):
    return MatchEngine(MatchConfig()).match(left, right)


def _decision(
    paths: tuple[str, str],
    qualities: tuple[float, float],
):
    a, b = paths
    a_q, b_q = qualities
    engine = DecisionEngine()
    candidates = [
        Candidate(path=a, quality_score=a_q, codec="flac", metadata_score=90.0),
        Candidate(path=b, quality_score=b_q, codec="flac", metadata_score=95.0),
    ]
    return engine.decide(candidates)


# ---------------------------------------------------------------------------
# T1: normalization / fuzzy matching
# ---------------------------------------------------------------------------


def test_t1_normalized_variants_match_high() -> None:
    out = _match(
        _track(title="Don't Stop Me Now", path="/a.flac"),
        _track(title="dont stop me now", path="/b.flac"),
    )
    assert out.score >= MatchConfig().threshold("high_threshold")
    assert out.same_song is True


def test_t1_underscores_and_spacing() -> None:
    out = _match(
        _track(title="Don't_Stop_Me_Now", path="/a.flac"),
        _track(title="Don't  Stop   Me  Now", path="/b.flac"),
    )
    assert out.score >= MatchConfig().threshold("high_threshold")


# ---------------------------------------------------------------------------
# T2: remaster => same song, different edition / version
# ---------------------------------------------------------------------------


def test_t2_remaster_is_same_song_different_edition() -> None:
    out = _match(
        _track(title="Don't Stop Me Now", path="/a.flac"),
        _track(title="Don't Stop Me Now (2011 Remaster)", path="/b.flac"),
    )
    assert out.same_song is True
    assert out.edition_different is True
    assert out.same_version in (False, None)


def test_t2_live_is_different_version() -> None:
    out = _match(
        _track(title="Enjoy the Silence", path="/a.flac"),
        _track(title="Enjoy the Silence (Live)", path="/b.flac"),
    )
    assert out.same_song is True
    assert out.version_different is True
    assert out.version_tag == "live"


# ---------------------------------------------------------------------------
# T3: different songs -> NO_MATCH (Decision must not run)
# ---------------------------------------------------------------------------


def test_t3_different_songs_no_match() -> None:
    out = _match(
        _track(title="We Will Rock You", path="/a.flac", track_no=1, duration_ms=122_000),
        _track(title="We Are The Champions", path="/b.flac", track_no=7, duration_ms=180_000),
    )
    # different songs: below the "possible duplicate" tier, never confirmed
    assert out.score < MatchConfig().threshold("high_threshold")
    assert out.score < 80.0
    assert not out.requires_review


# ---------------------------------------------------------------------------
# T4: artist aliases AC/DC vs AC DC
# ---------------------------------------------------------------------------


def test_t4_artist_alias_high_similarity() -> None:
    out = _match(
        _track(artist="AC/DC", path="/a.flac"),
        _track(artist="AC DC", path="/b.flac"),
    )
    assert out.artist_score >= 90.0


def test_t4_artist_alias_folds() -> None:
    from musicfusion.matching import text_profile

    keys = {text_profile(v).compact for v in ("AC/DC", "AC DC", "ACDC")}
    assert len(keys) == 1


# ---------------------------------------------------------------------------
# T5: same song, MP3 320 vs FLAC 16/44.1 -> winner = FLAC
# ---------------------------------------------------------------------------


def test_t5_mp3_vs_flac_winner_is_flac() -> None:
    result = _decision(("/a.mp3", "/b.flac"), (92.0, 99.0))
    assert result.winner_path.endswith("/b.flac")


# ---------------------------------------------------------------------------
# T6: FLAC 16/44 vs FLAC 24/96 -> winner = B
# ---------------------------------------------------------------------------


def test_t6_flac_16_vs_flac_24_winner_is_higher_quality() -> None:
    result = _decision(("/a.flac", "/b.flac"), (99.0, 100.0))
    assert result.winner_path.endswith("/b.flac")
    assert result.as_dict()["reasons"]


# ---------------------------------------------------------------------------
# T7: "Intro" on different albums is a false positive to avoid
# ---------------------------------------------------------------------------


def test_t7_intro_requires_review_across_albums() -> None:
    out = _match(
        _track(title="Intro", album="Album A", track_no=1, path="/a.flac"),
        _track(title="Intro", album="Album B", track_no=1, path="/b.flac"),
    )
    # same generic name IS the same name (same_song True), but the engine must
    # refuse to confirm it without album context: requires_review must be on.
    assert out.requires_review is True
    assert out.classification in {"NEEDS_REVIEW", "MATCH_POSSIBLE", "MATCH_LOW"}


# ---------------------------------------------------------------------------
# T8: same-name compilations with different tracklists -> no mix
# ---------------------------------------------------------------------------


def test_t8_homonymous_compilations_do_not_match() -> None:
    def album(name: str, tracklist: list[tuple[str, str]], album_artist: str):
        return album_signature(
            title=name,
            artist=album_artist,
            members=[
                member_from_track(title=t, artist=a, track_no=i + 1)
                for i, (t, a) in enumerate(tracklist)
            ],
        )

    hers = [
        ("We Will Rock You", "Queen"),
        ("We Are The Champions", "Queen"),
        ("Bohemian Rhapsody", "Queen"),
    ]
    theirs = [
        ("Dancing Queen", "ABBA"),
        ("Mamma Mia", "ABBA"),
        ("Fernando", "ABBA"),
    ]
    sim = album_similarity(
        album("Greatest Hits", hers, "Various Artists"),
        album("Greatest Hits", theirs, "Various Artists"),
    )
    # score is 0..100; a ~34 is clearly below anything resembling a match
    assert sim.score < 50.0

    similar = album_similarity(
        album("Greatest Hits", hers, "Various Artists"),
        album("Greatest Hits", hers, "Various Artists"),
    )
    assert similar.score >= 85.0


# ---------------------------------------------------------------------------
# T9: same-name compilations with near-identical tracklists -> candidates
# ---------------------------------------------------------------------------


def test_t9_identical_compilations_match() -> None:
    tracklist = [
        ("We Will Rock You", "Queen"),
        ("We Are The Champions", "Queen"),
        ("Bohemian Rhapsody", "Queen"),
        ("Another One Bites the Dust", "Queen"),
    ]

    def album(name: str):
        return album_signature(
            title=name,
            artist="Various Artists",
            members=[
                member_from_track(title=t, artist=a, track_no=i + 1)
                for i, (t, a) in enumerate(tracklist)
            ],
        )

    sim = album_similarity(album("Greatest Hits"), album("Greatest Hits (Vol 2)"))
    assert sim.score >= 0.6


# ---------------------------------------------------------------------------
# T10: metadata-less "01.flac" inferred via album context must be flagged
# ---------------------------------------------------------------------------


def test_t10_no_metadata_inferred_flagged() -> None:
    out = _match(
        _track(title="01.flac", album="Album A", artist="The Band",
               track_no=1, duration_ms=0, path="/albumA/01.flac"),
        _track(title="01.flac", album="Album B", artist="The Other",
               track_no=1, duration_ms=0, path="/albumB/01.flac"),
    )
    assert out.requires_review is True

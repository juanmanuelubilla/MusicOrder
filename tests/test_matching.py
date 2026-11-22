"""Tests for the Matching Engine (spec §30-31).

Covers normalization, fuzzy matching, artist/album/title, track/disc numbers,
durations, years, fingerprint behaviour, compilation and homonymous albums,
false-positive/negative prevention, album similarity, contextual matching,
final score composition, classification and database persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicfusion.db.session import Database
from musicfusion.matching import (
    AlbumContext,
    AlbumSignature,
    MatchConfig,
    MatchEngine,
    MatchRank,
    MatchStore,
    MatchTrack,
    album_context,
    album_signature,
    album_similarity,
    cache_key,
    config_fingerprint,
    duration_fraction,
    explain,
    folder_fraction,
    member_from_track,
    number_fraction,
    rank,
    select_dominant_album,
    total_tracks_fraction,
    year_fraction,
)
from musicfusion.matching.normalize import (
    is_compilation_artist,
    primary_artist,
    text_profile,
)

# ---------------------------------------------------------------------------
# 1. normalization
# ---------------------------------------------------------------------------


def test_normalize_title_variants() -> None:
    variants = [
        "Don't Stop Me Now",
        "Don't_Stop_Me_Now",
        "  Don't  Stop  Me  Now  ",
    ]
    keys = {text_profile(v).key for v in variants}
    assert len(keys) == 1, keys


def test_apostrophe_missing_is_fuzzy() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, 1, 1981, 238_000)
    b = _t("Queen", "Greatest Hits", "DONT STOP ME NOW", 1, 1, 1981, 238_000)
    out = engine.match(a, b)
    assert out.title_score >= 80.0
    assert out.classification not in {MatchRank.NO_MATCH.value}


def test_normalize_accented_equivalents() -> None:
    assert text_profile("Déjà Vu").key == text_profile("deja vu").key
    assert text_profile("Mötley Crüe").key == text_profile("motley crue").key


def test_edition_markers_are_not_destructive() -> None:
    base = text_profile("Don't Stop Me Now")
    edition = text_profile("Don't Stop Me Now (2011 Remaster)")
    assert edition.original == "Don't Stop Me Now (2011 Remaster)"  # untouched
    assert edition.has_edition_marker
    assert edition.base_tokens == base.base_tokens


def test_version_markers_survive_normalization() -> None:
    live = text_profile("Enjoy the Silence (Live)")
    base = text_profile("Enjoy the Silence")
    assert live.base_tokens == base.base_tokens
    assert live.has_version_marker and not base.has_version_marker


def test_feat_splits_primary_artist() -> None:
    assert primary_artist("Michael Jackson feat. Snoop Dogg") == "Michael Jackson"
    assert primary_artist("Anastacia featuring Lil' Jon") == "Anastacia"
    assert primary_artist("Queen") == "Queen"


def test_compilation_artist_detection() -> None:
    assert is_compilation_artist("Various Artists")
    assert is_compilation_artist("V.A.")
    assert is_compilation_artist("various")
    assert not is_compilation_artist("Queen")


# ---------------------------------------------------------------------------
# 2. scoring dimensions
# ---------------------------------------------------------------------------


def test_track_number_handles_01_vs_1() -> None:
    assert number_fraction(1, 1) == 1.0
    assert number_fraction(3, 3) == 1.0
    assert number_fraction(None, 3) == 0.5
    assert number_fraction(3, 4) < 1.0
    assert number_fraction(3, 9) == 0.0


def test_duration_tolerance_short_and_long() -> None:
    cfg = MatchConfig()
    # long songs: 238 vs 240 seconds are accepted
    assert duration_fraction(238_000, 240_000,
                             relative_tolerance=cfg.duration_relative_tolerance,
                             absolute_ms=cfg.duration_absolute_ms) == 1.0
    # extreme differences are heavily penalized
    slow = duration_fraction(238_000, 340_000,
                             relative_tolerance=cfg.duration_relative_tolerance,
                             absolute_ms=cfg.duration_absolute_ms)
    assert slow < 0.4


def test_year_fraction() -> None:
    assert year_fraction(1981, 1981) == 1.0
    assert year_fraction(1981, 2011) < 1.0
    assert year_fraction(None, 2011) == 0.5


def test_total_tracks_fraction() -> None:
    assert total_tracks_fraction(10, 10) == 1.0
    assert total_tracks_fraction(10, 11) < 1.0
    assert total_tracks_fraction(10, 20) == 0.0
    assert total_tracks_fraction(None, 10) == 0.5


def test_folder_fraction() -> None:
    a = _t("Queen", "Greatest Hits", "We Will Rock You", path="/Musica/Queen/1981 - Greatest Hits/01")
    b = _t("Queen", "Greatest Hits", "We Will Rock You", path="/Backup/Queen/Greatest Hits/01")
    c = _t("Queen", "Greatest Hits", "We Will Rock You", path="/Music/Other/01")
    assert folder_fraction(a, b) >= 0.55
    assert folder_fraction(a, c) < 0.55


def test_folder_fraction_folds_accents_and_uses_year() -> None:
    base = _t("Babasónicos", "Jessico", "Soy Rock", path="/m/Babasónicos/2001 - Jessico/04.ogg")
    twin = _t("Babasonicos", "Jessico", "Soy Rock", path="/n/Babasonicos/2001 - Jessico/04.ogg")
    other_year = _t("Band", "Album", "Song", path="/n/Band/1999 - Album/01.ogg")
    assert folder_fraction(base, twin) == 1.0
    assert folder_fraction(base, other_year) < 0.55


def test_total_tracks_evidence_in_score() -> None:
    engine = MatchEngine(MatchConfig())
    tt10 = _t("A", "B", "Song", 1, 1, None, 200_000, total_tracks=10)
    tt10b = _t("A", "B", "Song", 1, 1, None, 200_000, total_tracks=10)
    tt99 = _t("A", "B", "Song", 1, 1, None, 200_000, total_tracks=99)
    out_agree = engine.match(tt10, tt10b)
    out_conflict = engine.match(tt10, tt99)
    assert any("total de pistas" in r.lower() for r in out_agree.reasons)
    assert any("total de pistas" in w.lower() for w in out_conflict.warnings)


def test_folder_evidence_in_score() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "We Will Rock You", track_no=1, duration_ms=200_000,
           path="/Musica/Queen/Greatest Hits/01 - We Will Rock You.flac")
    b = _t("Queen", "Greatest Hits", "We will rock you", track_no=1, duration_ms=199_000,
           path="/Backup/Queen/Greatest Hits/01 We Will Rock You.flac")
    c = _t("Queen", "Greatest Hits", "We will rock you", track_no=1, duration_ms=199_000,
           path="/Backup/Bar/01 We Will Rock You.flac")
    out_same = engine.match(a, b)
    out_diff = engine.match(a, c)
    assert out_same.score > out_diff.score


# ---------------------------------------------------------------------------
# 3. spec example + CASO 1..10
# ---------------------------------------------------------------------------


def _t(
    artist: str = "",
    album: str = "",
    title: str = "",
    track_no: int | None = None,
    disc_no: int | None = None,
    year: int | None = None,
    duration_ms: int = 0,
    fingerprint: str = "",
    path: str = "",
    total_tracks: int | None = None,
) -> MatchTrack:
    return MatchTrack(
        path=path or "/f/" + (title or "x"),
        artist=artist,
        album_artist=artist,
        album=album,
        title=title,
        track_no=track_no,
        disc_no=disc_no,
        year=year,
        duration_ms=duration_ms,
        fingerprint=fingerprint,
        total_tracks=total_tracks,
    )


def test_caso1_titulo_identico() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, 1, 1981, 238_000)
    b = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, 1, 1981, 238_000)
    out = engine.match(a, b)
    assert out.classification == MatchRank.CONFIRMED.value
    assert out.score >= 95.0


def test_caso2_thin_variant_confirmed() -> None:
    engine = MatchEngine(MatchConfig())
    # "don t" (typed with a space) is a fuzzy variant of "don't"; still the
    # same song and confirmed, though normalization no longer makes it exact.
    a = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, None, 1981, 238_000)
    b = _t("Queen", "Greatest Hits", "don t stop me now", 1, None, 1981, 237_500)
    out = engine.match(a, b)
    assert out.title_score >= 90.0
    assert out.classification == MatchRank.CONFIRMED.value


def test_caso3_edition_variant_flagged() -> None:
    engine = MatchEngine(MatchConfig())
    base = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, 1, 1981, 238_000)
    rem = _t("Queen", "Greatest Hits (2011 Remaster)", "Don't Stop Me Now (2011 Remaster)",
             1, 1, 2011, 238_300)
    out = engine.match(base, rem)
    assert out.classification in {MatchRank.HIGH.value, MatchRank.VERY_HIGH.value}
    assert out.score < 95.0
    msg = "".join(out.warnings) + "".join(out.reasons)
    assert "edición" in msg.lower()


def test_caso4_live_is_different_version() -> None:
    engine = MatchEngine(MatchConfig())
    studio = _t("Depeche Mode", "Violator", "Enjoy the Silence", 5, 1, None, 198_000)
    live = _t("Depeche Mode", "Violator", "Enjoy the Silence (Live)", 5, 1, None, 202_000)
    out = engine.match(studio, live)
    assert out.classification != MatchRank.CONFIRMED.value
    assert out.requires_review is True


def test_caso5_artist_aliases_equivalent() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("AC/DC", "Back in Black", "Hells Bells", 5, 1, None, 312_000)
    b = _t("AC DC", "Back in Black", "Hells Bells", 5, 1, None, 311_400)
    assert engine.match(a, b).artist_score == 100.0
    assert engine.match(a, b).classification != MatchRank.NO_MATCH.value


def test_caso6_album_casing_equivalent() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Pink Floyd", "The Dark Side Of The Moon", "Time", 6, 1, 1973, 421_000)
    b = _t("Pink Floyd", "The Dark Side of the Moon", "Time", 6, 1, 1973, 421_200)
    assert engine.match(a, b).album_score == 100.0


def test_caso7_homonymous_different_artists_no_match() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "We Will Rock You", 1, 1, 1981, 122_000)
    b = _t("ABBA", "Greatest Hits", "Dancing Queen", 2, 1, 1979, 174_000)
    out = engine.match(a, b)
    assert out.classification == MatchRank.NO_MATCH.value


def test_caso8_near_identical_compilations_match() -> None:
    engine = MatchEngine(MatchConfig())
    a_tracks = [("We Will Rock You", 1), ("Billie Jean", 2), ("Dancing Queen", 3)]
    b_tracks = [("We Will Rock You", 1), ("Billie Jean", 2), ("Dancing Queen", 3)]
    sig_a = album_signature(
        title="Greatest Hits", artist="Various Artists",
        members=[member_from_track(title=title, track_no=n) for title, n in a_tracks],
    )
    sig_b = album_signature(
        title="Greatest Hits (2011)", artist="Various Artists",
        members=[member_from_track(title=title, track_no=n) for title, n in b_tracks],
    )
    sim = album_similarity(sig_a, sig_b)
    assert sim.score > 80.0

    track_a = MatchTrack(artist="Various artists", title="Billie Jean", track_no=2,
                         duration_ms=1_200_000)
    track_b = MatchTrack(artist="V.A.", title="Billie Jean", track_no=2,
                        duration_ms=1_202_000)
    ctx = album_context(sig_a, sig_b)
    out = engine.match(track_a, track_b, context=ctx)
    assert out.classification in {
        MatchRank.HIGH.value, MatchRank.VERY_HIGH.value, MatchRank.CONFIRMED.value,
    }


def test_caso9_intro_same_track_other_album_needs_review() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Alice", "Album X", "01 - Intro", 1, 1, None, 90_000)
    b = _t("Bob", "Album Y", "01 - Intro", 1, 1, None, 90_000)
    out = engine.match(a, b)
    assert out.classification == MatchRank.NEEDS_REVIEW.value
    assert out.requires_review is True


def test_caso10_context_permits_inference_but_flagged() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "01.flac", 1, 1, None, 200_000)
    b = _t("Queen", "Greatest Hits", "01.flac", 1, 1, None, 200_000)
    ctx = AlbumContext(similarity=92.0, matched_tracks=9, total_tracks=10)
    out = engine.match(a, b, context=ctx)
    assert out.album_context_score >= 90.0
    assert out.classification in {
        MatchRank.POSSIBLE.value, MatchRank.HIGH.value,
        MatchRank.VERY_HIGH.value, MatchRank.CONFIRMED.value,
    }
    assert out.requires_review is True


# ---------------------------------------------------------------------------
# fingerprint evidence
# ---------------------------------------------------------------------------


def test_fingerprint_boost_and_cap() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("A", "B", "Same", 1, 1, None, 200_000, fingerprint="AgAAEDGKAggTRInABFOwQYnABBky")
    b = _t("A", "B", "Same (Live)", 1, 1, None, 200_000, fingerprint="AgAAEDGKAggTRInABFOwQYnABBky")
    out = engine.match(a, b)
    assert out.fingerprint_score == 100.0
    assert out.score >= 95.0  # strong boost, even past the version cap
    assert out.score <= 100.0


def test_fingerprint_mismatch_penalty_and_warning() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("A", "B", "Same", None, None, None, 120_000, fingerprint="AgAAEDGKAggTRInABFOwQYn")
    b = _t("A", "B", "Same", None, None, None, 120_000, fingerprint="AgAAEDGKAggTRInABFOwZy8")
    out = engine.match(a, b)
    assert out.fingerprint_score == 0.0
    assert any("fingerprint" in w.lower() for w in out.warnings)


# ---------------------------------------------------------------------------
# false positives / negatives
# ---------------------------------------------------------------------------


def test_untitled_needs_review() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Unknown", "Misc", "Untitled", 0, None, None, 60_000)
    b = _t("Unknown", "Misc", "untitled", 0, None, None, 60_000)
    assert engine.match(a, b).classification == MatchRank.NEEDS_REVIEW.value


def test_track_01_needs_review() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("A", "AA", "Track 01", 1, None, None, 60_000)
    b = _t("B", "BB", "Track 01", 1, None, None, 60_000)
    assert engine.match(a, b).classification == MatchRank.NEEDS_REVIEW.value


def test_typos_are_tolerated() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("The Beatles", "Abbey Road", "Here Comes the Sun", 15, None, 1969, 190_000)
    b = _t("The Beatles", "Abbey Road", "Here Come the Sun", 15, None, 1969, 189_400)
    assert engine.match(a, b).classification not in {MatchRank.NO_MATCH.value}


def test_year_is_not_an_impediment() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "Love of My Life", 1, None, 1981, 220_000)
    b = _t("Queen", "Greatest Hits", "Love of My Life", 1, None, 2011, 220_000)
    out = engine.match(a, b)
    assert out.year_score > 0.0
    assert out.classification != MatchRank.NO_MATCH.value


def test_duration_gap_reduces_confidence() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Q", "A", "Same Song", None, None, None, 238_000)
    b = _t("Q", "A", "Same Song", None, None, None, 340_000)
    out = engine.match(a, b)
    assert out.duration_score < 40.0
    assert out.classification not in {MatchRank.CONFIRMED.value, MatchRank.VERY_HIGH.value}


def test_big_duration_gap_marks_different_version() -> None:
    """Untagged live take vs studio master: same song, NOT mergeable."""
    engine = MatchEngine(MatchConfig())
    studio = _t("Band", "Album", "Same Song", None, None, None, 200_000)
    live = _t("Band", "Album", "Same Song", None, None, None, 260_000)
    out = engine.match(studio, live)
    assert out.same_song is True
    assert out.version_different is True
    assert "duración" in out.version_tag
    assert out.requires_review is True
    assert any("Duración muy distinta" in w for w in out.warnings)

    from musicfusion.compare.broad import _is_duplicate

    assert _is_duplicate(out) is False


def test_small_duration_gap_is_not_a_version_difference() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Band", "Album", "Same Song", None, None, None, 200_000)
    b = _t("Band", "Album", "Same Song", None, None, None, 204_000)
    out = engine.match(a, b)
    assert out.version_different is False

    from musicfusion.compare.broad import _is_duplicate

    assert _is_duplicate(out) is True


def test_live_marker_blocks_duplicates_even_with_high_score() -> None:
    from musicfusion.compare.broad import _is_duplicate

    engine = MatchEngine(MatchConfig())
    base = _t("Depeche Mode", "Album", "Enjoy the Silence", 3, 1, 1990, 240_000)
    live = _t("Depeche Mode", "Live Album", "Enjoy the Silence (Live)", 3, 1, 1991, 300_000)
    out = engine.match(base, live)
    assert out.version_different is True
    assert _is_duplicate(out) is False


def test_album_version_marker_blocks_duplicates() -> None:
    """Same title, studio album vs live album (marker in the ALBUM field)."""
    from musicfusion.compare.broad import _is_duplicate

    engine = MatchEngine(MatchConfig())
    studio = _t("Band", "Greatest Hits", "My Song", None, None, None, 200_000)
    live = _t("Band", "Live in Paris", "My Song", None, None, None, 200_000)
    out = engine.match(studio, live)
    assert out.version_different is True
    assert out.version_tag == "live"
    assert any("álbum indica una versión distinta" in w for w in out.warnings)
    assert _is_duplicate(out) is False


def test_spanish_album_marker_directo_blocks_duplicates() -> None:
    engine = MatchEngine(MatchConfig())
    estudio = _t("Soda Stereo", "Canción Animal", "De Música Ligera", 3, 1, 1990, 210_000)
    directo = _t("Soda Stereo", "Directo en Buenos Aires", "De Música Ligera", 3, 1, 1993, 260_000)
    out = engine.match(estudio, directo)
    assert out.version_different is True
    assert out.version_tag == "directo"


def test_plain_album_difference_still_allows_duplicates() -> None:
    """Same recording on the original album AND a compilation stays a duplicate."""
    from musicfusion.compare.broad import _is_duplicate

    engine = MatchEngine(MatchConfig())
    original = _t("Band", "Nightwork", "My Song", 4, 1, 2011, 200_000)
    compilation = _t("Band", "The Essential Band", "My Song", 7, 2, 2015, 200_000)
    out = engine.match(original, compilation)
    assert out.version_different is False
    assert _is_duplicate(out) is True


def test_disc_number_disambiguates() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Me", "Double", "Track 03", 3, 1, None, 200_000)
    b = _t("Me", "Double", "Track 03", 3, 2, None, 200_000)
    out = engine.match(a, b)
    assert out.disc_number_score < 100.0


# ---------------------------------------------------------------------------
# album similarity + dominant album
# ---------------------------------------------------------------------------


def _sig(title: str, artist: str, tracks: list[tuple[str, int | None]]) -> AlbumSignature:
    return album_signature(
        title=title,
        artist=artist,
        total_tracks=len(tracks),
        members=[
            member_from_track(title=t, track_no=n, artist=artist) for t, n in tracks
        ],
    )


def test_album_similarity_identical() -> None:
    a = _sig("Greatest Hits", "Queen", [("Bohemian", 1), ("We Will", 2)])
    b = _sig("GREATEST HITS", "Queen", [("Bohemian", 1), ("We Will", 2)])
    assert album_similarity(a, b).score > 80.0


def test_album_similarity_distinct_artists() -> None:
    a = _sig("Greatest Hits", "Queen", [("We Will", 1), ("Another", 2)])
    b = _sig("Greatest Hits", "ABBA", [("Dancing Queen", 1), ("Other", 2)])
    assert album_similarity(a, b).score < 50.0


def test_compilations_compare_members() -> None:
    a = _sig("Greatest Hits", "Various Artists",
             [("We Will", 1), ("Billie Jean", 2), ("Dancing Queen", 3)])
    b = _sig("Greatest Hits", "Various Artists",
             [("We Will", 1), ("Billie Jean", 2), ("Dancing Queen", 3)])
    assert album_similarity(a, b).score > 80.0


def test_homonymous_compilations_do_not_mix() -> None:
    q = _sig("Greatest Hits", "Various Artists", [("We Will", 1), ("Bohemian", 2)])
    mj = _sig("Greatest Hits", "Various Artists", [("Thriller", 1), ("Billie", 2)])
    assert album_similarity(q, mj).score < 50.0


def test_select_dominant_album_prefers_coherent_folder() -> None:
    small = _sig("Greatest Hits", "Queen", [("We Will", 1)])
    full = _sig("Greatest Hits", "Queen", [("We Will", 1), ("Radio", 2), ("Bohemian", 3)])
    dominant = select_dominant_album([small, full])
    assert dominant is full


# ---------------------------------------------------------------------------
# classification, selection, explain
# ---------------------------------------------------------------------------


def test_rank_thresholds() -> None:
    cfg = MatchConfig()
    assert rank(100.0, cfg) == MatchRank.CONFIRMED.value
    assert rank(92.0, cfg) == MatchRank.VERY_HIGH.value
    assert rank(85.0, cfg) == MatchRank.HIGH.value
    assert rank(72.0, cfg) == MatchRank.POSSIBLE.value
    assert rank(55.0, cfg) == MatchRank.LOW.value
    assert rank(30.0, cfg) == MatchRank.NO_MATCH.value


def test_weights_sum_to_100() -> None:
    assert sum(MatchConfig().weighted().values()) == pytest.approx(100.0)


def test_explain_includes_breakdown() -> None:
    engine = MatchEngine(MatchConfig())
    a = _t("Queen", "Greatest Hits", "Don't Stop Me Now", 1, 1, 1981, 238_000)
    b = _t("Queen", "Greatest Hits", "Don t Stop Me Now", 1, 1, 1981, 238_500)
    text = explain(engine.match(a, b))
    assert "MATCH" in text and "Artista" in text and "Título" in text


# ---------------------------------------------------------------------------
# cache + persistence
# ---------------------------------------------------------------------------


def test_cache_key_stable() -> None:
    a = _t("Queen", "A", "Don't Stop", 1, None, None, 200_000)
    b = _t("Queen", "A", "Don't Stop", 1, None, None, 200_000)
    fp = config_fingerprint(MatchConfig())
    assert cache_key(a, b, fp) == cache_key(a, b, fp)
    c = _t("Other", "B", "Different", 2, None, None, 200_000)
    assert cache_key(a, b, fp) != cache_key(a, c, fp)


def test_cache_key_1_includes_config() -> None:
    a = _t("A", "B", "X", None, None, 1981, 200_000)
    b = _t("A", "B", "X", None, None, 1981, 200_000)
    cfg_a = config_fingerprint(MatchConfig())
    cfg_b = config_fingerprint(MatchConfig(thresholds={"confirmed_threshold": 90.0}))
    assert cache_key(a, b, cfg_a) != cache_key(a, b, cfg_b)


def test_persistence_flow(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.create_schema()
    store = MatchStore(db)

    engine = MatchEngine(MatchConfig())
    a = _t("Artist", "Album", "Song", 1, None, None, 100_000)
    b = _t("Artist", "Album", "Song", 1, None, None, 101_000)
    out = engine.match(a, b)
    out.cache_key = cache_key(a, b, config_fingerprint(MatchConfig()))

    stored = store.save(out)
    assert stored.id is not None
    assert stored.score == out.score
    assert stored.classification == out.classification

    assert len(store.list_recent(10)) == 1

    store.decide(stored.id, "approved")
    rec = store.get(stored.id)
    assert rec is not None and rec.decision == "approved"

    store.decide(stored.id, "rejected")
    rec = store.get(stored.id)
    assert rec is not None and rec.decision == "rejected"

    store.decide(stored.id, "invalidated")
    rec = store.get(stored.id)
    assert rec is not None and rec.invalidated is True

    assert store.clear() >= 1


def test_save_reuses_cached_result(tmp_path: Path) -> None:
    db = Database(tmp_path / "db2.sqlite")
    db.create_schema()
    store = MatchStore(db)
    a = _t("X", "Y", "Z", None, None, None, 100_000)
    b = _t("X", "Y", "Z", None, None, None, 101_000)
    fp = config_fingerprint(MatchConfig())
    assert store.find_cached(a, b, fp) is None

    out = MatchEngine(MatchConfig()).match(a, b)
    out.cache_key = cache_key(a, b, fp)
    store.save(out)
    assert store.find_cached(a, b, fp) is not None

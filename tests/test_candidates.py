"""Spec-driven tests: normalization, candidate generation and identity levels.

Covers the mandatory cases of the "corregir matching y generación de
candidatos" specification WITHOUT creating any parallel engine: everything
goes through musicfusion.matching (the single Matching Engine).
"""

from __future__ import annotations

from pathlib import Path

from musicfusion.compare.candidates import (
    TrackEntry,
    build_album_folders,
    compute_tracklist_similarity,
    make_track_entry,
    pair_album_folders,
    pair_tracks_in_albums,
    split_album_name,
)
from musicfusion.config.settings import Settings
from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack
from musicfusion.matching.normalize import normalize_music_text, split_base_and_version
from musicfusion.matching.storage import MATCHING_VERSION

# ---------------------------------------------------------------------------
# 1. normalización musical robusta
# ---------------------------------------------------------------------------

def test_normalize_music_text_canonical_form() -> None:
    assert normalize_music_text("Babasónicos") == normalize_music_text("Babasonicos")
    assert normalize_music_text("Babasónicos ") == normalize_music_text("BABASONICOS")
    assert normalize_music_text("Canción") == "cancion"
    assert normalize_music_text("Muñeco") == "muneco"
    assert normalize_music_text("Poder Ñandú") == normalize_music_text("Poder Nandu")
    assert normalize_music_text("Don't Stop Me Now") == normalize_music_text("Dont Stop Me Now")
    assert normalize_music_text("Los Calientes") == normalize_music_text("los calientes")
    # separators collapse into tokens; compact tolerates glued spellings
    assert normalize_music_text("D-Generación") == "d generacion"
    assert normalize_music_text("D_Generacion") == "d generacion"


def test_normalize_separators_keep_tokens() -> None:
    """Tokens survive so 'D Generación' != 'Generación D' if order matters,
    while compact forms allow 'DGeneracion' == 'D-Generación' style matches."""
    a = normalize_music_text("The Dark Side Of The Moon")
    b = normalize_music_text("the dark side of the moon")
    assert a == b


def test_split_base_and_version() -> None:
    base, versions = split_base_and_version("Los Calientes (Remix)")
    assert base == "los calientes"
    assert versions == ("remix",)
    base2, versions2 = split_base_and_version("Los Calientes")
    assert base2 == "los calientes" and versions2 == ()


def test_split_album_name_extracts_year() -> None:
    assert split_album_name("2001 - Jessico") == (2001, "jessico")
    assert split_album_name("2001 Jessico") == (2001, "jessico")
    assert split_album_name("Jessico") == (None, "jessico")


# ---------------------------------------------------------------------------
# helpers to build entries/engine tracks without touching disk audio
# ---------------------------------------------------------------------------

def _entry(
    root: str,
    folder: str,
    name: str,
    *,
    artist: str = "",
    album: str = "",
    title: str = "",
    duration_ms: int = 200_000,
    track_no: int | None = None,
    year: int | None = None,
    side: str = "a",
) -> TrackEntry:
    path = Path(root) / folder / name
    mt = MatchTrack(
        path=str(path),
        artist=artist,
        album=album,
        title=title,
        duration_ms=duration_ms,
        track_no=track_no,
        year=year,
    )
    return make_track_entry(path, side, mt)


def _config() -> MatchConfig:
    return MatchConfig()


# ---------------------------------------------------------------------------
# 6-8. candidatos por artista/álbum; año distinto NO bloquea
# ---------------------------------------------------------------------------

def test_candidate_exact_artist_and_album_keys() -> None:
    a = [_entry("/A", "1992 - Pasto", "01 - Intro.flac", artist="Babasónicos", album="Pasto")]
    b = [_entry("/B", "1992 - Pasto", "01 - Intro.flac", artist="Babasonicos", album="Pasto")]
    fa, fb = build_album_folders(a), build_album_folders(b)
    pairings = pair_album_folders(fa, fb, _config())
    assert len(pairings) == 1
    assert pairings[0].artist_score >= 0.999
    assert pairings[0].phase == "exact_keys"


def test_candidate_different_year_does_not_block() -> None:
    """2001 - Jessico vs 2010 - Jessico must STILL become candidates."""
    a = [_entry("/A", "2001 - Jessico", "01 - Los Calientes.flac",
                artist="Babasónicos", album="Jessico", year=2001)]
    b = [_entry("/B", "2010 - Jessico", "01 - Los Calientes.flac",
                artist="Babasonicos", album="Jessico", year=2010)]
    pairings = pair_album_folders(build_album_folders(a), build_album_folders(b), _config())
    assert len(pairings) == 1
    assert pairings[0].year_score < 1.0  # signal, not blocker


def test_candidate_fuzzy_artist_babasonicos_variants() -> None:
    a = [_entry("/A", "1999 - Miami", "11 - Gustavo Show.flac",
                artist="Babasónicos (Argentina)", album="Miami")]
    b = [_entry("/B", "1999 - Miami", "11 - Gustavo Show.flac",
                artist="babasonicos", album="Miami")]
    pairings = pair_album_folders(build_album_folders(a), build_album_folders(b), _config())
    assert len(pairings) == 1


def test_unrelated_artists_do_not_pair() -> None:
    a = [_entry("/A", "2001 - Jessico", "01 - X.flac", artist="Soda Stereo", album="Jessico")]
    b = [_entry("/B", "2001 - Jessico", "01 - X.flac", artist="Babasónicos", album="Jessico")]
    assert pair_album_folders(build_album_folders(a), build_album_folders(b), _config()) == []


def test_compilation_guard_in_engine() -> None:
    """same_album must NOT trigger between homonymous VA compilations."""
    engine = MatchEngine(_config())
    va = MatchTrack(artist="Various Artists", album="Greatest Hits", title="Song X",
                    duration_ms=200_000)
    band = MatchTrack(artist="Babasónicos", album="Greatest Hits", title="Song X",
                      duration_ms=200_000)
    out = engine.match(va, band)
    assert out.same_album is False


# ---------------------------------------------------------------------------
# 4/10/11. tracklist como evidencia; número de pista NO es requisito
# ---------------------------------------------------------------------------

def _album_entries(side_root: str, titles: list[str], prefix_numbers: bool = True) -> list:
    entries = []
    for index, title in enumerate(titles, start=1):
        name = f"{index:02d} - {title}.flac" if prefix_numbers else f"{title}.flac"
        entries.append(_entry(side_root, "1992 - Pasto", name,
                              artist="Babasónicos", album="Pasto", title=title))
    return entries


def test_track_number_not_required_for_candidates() -> None:
    left = _album_entries("/A", ["Los Calientes"], prefix_numbers=True)
    right = _album_entries("/B", ["Los Calientes"], prefix_numbers=False)
    pairing = pair_album_folders(
        build_album_folders(left), build_album_folders(right), _config()
    )[0]
    pairs = pair_tracks_in_albums(pairing)
    assert len(pairs) == 1
    assert pairs[0][0].title_base == "los calientes"


def test_tracklist_similarity_strong_evidence() -> None:
    titles_a = ["Zumba", "El Medium", "Cybernecia", "Safari Vixen", "Viva Satana",
                "Perfume Casino", "Calmatica", "Coyarama", "Su Majestad",
                "Pesadilla Bionica del Perro Bionico"]
    titles_b = ["Zumba", "El Medium", "Cybernecia", "Safari Vixen", "Viva Satana",
                "Perfume Casino", "Calmática", "Coyarama", "Su Majestad",
                "Pesadilla Biónica del Perro Biónico", "Su Ciervo"]  # reissue +1
    left = _album_entries("/A", titles_a)
    right = _album_entries("/B", titles_b)
    pairing = pair_album_folders(
        build_album_folders(left), build_album_folders(right), _config()
    )[0]
    similarity = compute_tracklist_similarity(pairing)
    assert similarity >= 0.85


def test_typos_pair_inside_candidate_albums() -> None:
    """Muñeco/Mueco: normalization can't make them equal, context does."""
    left = _album_entries("/A", ["Mueco"])
    right = _album_entries("/B", ["Muñeco"])
    pairing = pair_album_folders(
        build_album_folders(left), build_album_folders(right), _config()
    )[0]
    pairs = pair_tracks_in_albums(pairing)
    assert len(pairs) == 1  # token ratio muneco/mueco is high enough


def test_intro_same_title_other_album_not_duplicate() -> None:
    engine = MatchEngine(_config())
    a = MatchTrack(artist="Babasónicos", album="Pasto", title="Intro",
                   track_no=1, duration_ms=120_000, path="/A/Pasto/01 - Intro.flac")
    b = MatchTrack(artist="Charly García", album="Yendo a la Casa de Damián",
                   title="Intro", track_no=1, duration_ms=180_000,
                   path="/B/Damian/01 - Intro.flac")
    out = engine.match(a, b)
    from musicfusion.compare.broad import _is_duplicate

    assert _is_duplicate(out) is False
    assert out.same_album is False


def test_same_title_same_artist_different_album_is_not_release() -> None:
    engine = MatchEngine(_config())
    studio = MatchTrack(artist="Babasónicos", album="Jessico", title="Soy Rock",
                        duration_ms=200_000, year=2001, path="/A/Jessico/04.flac")
    comp = MatchTrack(artist="Babasónicos", album="Obras Cumbres", title="Soy Rock",
                      duration_ms=200_000, year=2018, path="/A/Obras Cumbres/09.flac")
    out = engine.match(studio, comp)
    assert out.same_track is True       # same recording...
    assert out.same_album is False      # ...different albums
    assert out.same_release is False


# ---------------------------------------------------------------------------
# 24/25/26. outcome fields + rejection explanation
# ---------------------------------------------------------------------------

def test_outcome_exposes_new_identity_fields() -> None:
    engine = MatchEngine(_config())
    a = MatchTrack(artist="Queen", album="Greatest Hits", title="We Will Rock You",
                   track_no=1, duration_ms=122_000, year=1981,
                   path="/Q/1981 - Greatest Hits/01.flac")
    b = MatchTrack(artist="QUEEN", album="Greatest Hits", title="We Will Rock You",
                   track_no=1, duration_ms=121_500, year=1981,
                   path="/Backup/Queen/Greatest Hits/01.flac")
    out = engine.match(a, b)
    assert out.same_track is True
    assert out.same_album is True
    assert out.same_release is True
    assert out.context_score >= 80.0
    assert hasattr(out, "candidate_phase")


def test_explain_rejection_format() -> None:
    from musicfusion.matching.engine import explain_rejection

    engine = MatchEngine(_config())
    a = MatchTrack(artist="Queen", album="Jazz", title="Fat Bottomed Girls",
                   duration_ms=257_000, path="/Q/Jazz/05.flac")
    b = MatchTrack(artist="Queen", album="Jazz", title="Bicycle Race",
                   duration_ms=190_000, path="/Q/Jazz/06.flac")
    out = engine.match(a, b)
    text = explain_rejection(out)
    assert text.startswith("Candidato rechazado porque:")
    assert "Final:" in text and "Fingerprint:" in text


def test_matching_version_recorded() -> None:
    assert MATCHING_VERSION == "2"


# ---------------------------------------------------------------------------
# settings wiring
# ---------------------------------------------------------------------------

def test_candidate_thresholds_configurable(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path / "cfg")
    settings.set("candidates.artist_threshold", 0.60)
    settings.set("candidates.album_threshold", 0.70)
    config = MatchConfig.from_settings(settings)
    assert config.artist_candidate_threshold == 0.60
    assert config.album_candidate_threshold == 0.70

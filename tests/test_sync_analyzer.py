"""Tests for the read-only sync analyzer (MusicOrder <-> Hermes contract).

The analyzer must NEVER touch the collections: tests build throwaway trees
with empty files and assert on the structured plan only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicfusion.sync.analyzer import analyze_sync


def _touch_album(root: Path, artist: str, album: str, titles: list[str]) -> Path:
    folder = root / artist / album
    folder.mkdir(parents=True, exist_ok=True)
    for index, title in enumerate(titles, start=1):
        (folder / f"{index:02d} - {title}.flac").write_bytes(b"")
    return folder


@pytest.fixture()
def master(tmp_path: Path) -> Path:
    base = tmp_path / "master"
    _touch_album(base, "Babasónicos", "1992 - Pasto",
                 ["Intro", "D-Generación", "Tripeando", "Natural"])
    _touch_album(base, "Babasónicos", "1996 - Dopádromo",
                 ["Zumba", "El Medium", "Cybernecia", "Su Ciervo"])
    _touch_album(base, "Soda Stereo", "1988 - Doble Vida",
                 ["Picnic en el 4 B", "En la Ciudad de la Furia"])
    return base


def test_identical_artist_and_album(master: Path, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasónicos", "1992 - Pasto",
                 ["Intro", "D-Generación", "Tripeando", "Natural"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "ARTIST_MATCHED"
    assert artist.master_name == "Babasónicos"
    album = artist.albums[0]
    assert album.decision == "EXISTS"
    assert album.confidence >= 95
    assert album.destination == "Babasónicos/1992 - Pasto"


def test_artist_with_accent_difference(master: Path, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "Pasto",
                 ["Intro", "D-Generacion", "Tripeando", "Natural"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "ARTIST_MATCHED"
    assert artist.master_name == "Babasónicos"   # MASTER naming preserved
    album = artist.albums[0]
    assert album.decision == "EXISTS"
    assert album.master_folder == "1992 - Pasto"
    assert album.destination == "Babasónicos/1992 - Pasto"


def test_artist_case_difference(master: Path, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "SODA STEREO", "Doble Vida",
                 ["Picnic en el 4 B", "En la Ciudad de la Furia"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "ARTIST_MATCHED"
    assert artist.master_name == "Soda Stereo"


def test_artist_space_and_separator_difference(tmp_path: Path) -> None:
    master = tmp_path / "master"
    _touch_album(master, "AC-DC", "1980 - Back in Black", ["Hells Bells"])
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "AC_DC", "Back in Black", ["Hells Bells"])
    plan = analyze_sync(master, incoming)
    assert plan.artists[0].decision == "ARTIST_MATCHED"
    assert plan.artists[0].master_name == "AC-DC"
    # glued spelling too (ACDC vs AC-DC)
    incoming2 = tmp_path / "incoming2"
    _touch_album(incoming2, "ACDC", "Back in Black", ["Hells Bells"])
    plan2 = analyze_sync(master, incoming2)
    assert plan2.artists[0].decision == "ARTIST_MATCHED"


def test_existing_artist_existing_and_new_albums(master: Path, tmp_path: Path) -> None:
    """The fundamental spec example: Pasto/Dopadromo exist; Miami is new."""
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "Pasto",
                 ["Intro", "D-Generacion", "Tripeando", "Natural"])
    _touch_album(incoming, "Babasonicos", "Dopadromo",
                 ["Zumba", "El Medium", "Cybernecia", "Su Ciervo"])
    _touch_album(incoming, "Babasonicos", "1999 - Miami",
                 ["El Ringo", "Gustavo Show", "Casualidad"])

    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "ARTIST_MATCHED"
    decisions = {a.incoming_folder: a for a in artist.albums}

    pasto = decisions["Pasto"]
    assert pasto.decision == "EXISTS"
    dopa = decisions["Dopadromo"]
    assert dopa.decision == "EXISTS"
    assert dopa.master_folder == "1996 - Dopádromo"

    miami = decisions["1999 - Miami"]
    assert miami.decision == "NEW_ALBUM"
    assert miami.destination == "Babasónicos/1999 - Miami"
    assert miami.review_required is False

    # nothing to copy from existing albums
    summary = plan.summary()
    assert summary["new_albums_to_copy"] == ["Babasonicos/1999 - Miami"]


def test_nonexistent_artist_is_ignored(master: Path, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Divididos", "1993 - La Era de la Boludez", ["Jijiji"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    assert artist.decision == "NEW_ARTIST"
    assert artist.action == "IGNORE"
    assert artist.albums == []          # never analyzed for copying
    assert all("Divididos" not in (x or "") for x in [artist.master_name])


def test_album_slightly_different_name_matches_by_tracklist(
    master: Path, tmp_path: Path
) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "Dopadromo (Edicion Argentina)",
                 ["Zumba", "El Medium", "Cybernecia", "Su Ciervo"])
    plan = analyze_sync(master, incoming)
    album = plan.artists[0].albums[0]
    assert album.decision == "EXISTS"
    assert album.master_folder == "1996 - Dopádromo"


def test_album_different_year_same_release(master: Path, tmp_path: Path) -> None:
    """A reissue year must not turn the same album into a new one."""
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "2005 - Dopadromo Remaster",
                 ["Zumba", "El Medium", "Cybernecia", "Su Ciervo"])
    plan = analyze_sync(master, incoming)
    album = plan.artists[0].albums[0]
    assert album.decision == "EXISTS"


def test_same_album_key_but_different_tracklist_is_flagged(
    master: Path, tmp_path: Path
) -> None:
    """Same normalized name but totally different songs -> suspicious."""
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "1992 - Pasto",
                 ["Otra Cosa", "Cancion Distinta", "Tema Raro", "Final Alternativo"])
    plan = analyze_sync(master, incoming)
    album = plan.artists[0].albums[0]
    assert album.evidence["tracklist_similarity"] < 0.45
    assert album.decision in {"AMBIGUOUS_ALBUM", "NEW_ALBUM"}


def test_two_editions_of_same_album_are_not_duplicated(
    master: Path, tmp_path: Path
) -> None:
    """Deluxe edition of an existing album still EXISTS (master is truth)."""
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "1992 - Pasto (Deluxe)",
                 ["Intro", "D-Generación", "Tripeando", "Natural",
                  "Demo Tema", "Sesion Extra"])
    plan = analyze_sync(master, incoming)
    album = plan.artists[0].albums[0]
    assert album.decision == "EXISTS"
    assert album.master_folder == "1992 - Pasto"


def test_ambiguous_case_and_hermes_resolution(master: Path, tmp_path: Path) -> None:
    """Ambiguity is surfaced with review_required and can be resolved later."""
    incoming = tmp_path / "incoming"
    # borderline name similarity + partial tracklist -> ambiguous band
    _touch_album(incoming, "Babasonikos", "Pasto Sessions",
                 ["Intro", "D-Generacion", "Tema Ajeno"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    if artist.decision == "AMBIGUOUS_ARTIST":
        assert artist.review_required is True
        result = plan.resolve({artist.id: "MATCH"})
        assert artist.id in result["applied"]
        assert artist.decision == "ARTIST_MATCHED"
        assert artist.action == "SYNC_ALBUMS"
    else:
        # if the name matched confidently, the album may still be ambiguous
        album = artist.albums[0]
        assert album.decision in {"EXISTS", "NEW_ALBUM", "AMBIGUOUS_ALBUM"}
        if album.review_required:
            result = plan.resolve({album.id: "NEW_ALBUM"})
            assert album.id in result["applied"]
            assert album.decision == "NEW_ALBUM"
            assert album.destination is not None
            assert album.destination.endswith(album.incoming_folder)


def test_resolve_rejects_invalid_decisions(master: Path, tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Desconocido Total", "2020 - X", ["Uno"])
    plan = analyze_sync(master, incoming)
    artist = plan.artists[0]
    result = plan.resolve({artist.id: "NO-EXISTE"})
    assert result["applied"] == []
    assert result["rejected"][0]["id"] == artist.id


def test_analyzer_never_writes_to_collections(master: Path, tmp_path: Path) -> None:
    """Paranoid guard: mtimes/sizes unchanged after a full analysis."""

    def snapshot(base: Path) -> dict[str, tuple]:
        data = {}
        for path in sorted(base.rglob("*")):
            stat = path.stat()
            data[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return data

    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "Pasto", ["Intro", "D-Generacion"])
    before_m, before_i = snapshot(master), snapshot(incoming)

    analyze_sync(master, incoming)

    assert snapshot(master) == before_m
    assert snapshot(incoming) == before_i


def test_json_serializable_plan(master: Path, tmp_path: Path) -> None:
    import json

    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "Pasto", ["Intro", "D-Generacion"])
    plan = analyze_sync(master, incoming)
    payload = json.dumps(plan.as_dict(), ensure_ascii=False)  # must not raise
    assert "ARTIST_MATCHED" in payload


# ---------------------------------------------------------------------------
# Hermes bridge sobre planes SERIALIZADOS (flujo por archivos, sin Python)
# ---------------------------------------------------------------------------

def test_apply_decisions_on_serialized_plan(master: Path, tmp_path: Path) -> None:
    import json

    from musicfusion.sync.analyzer import apply_decisions

    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonikos", "Pasto Sessions",
                 ["Intro", "D-Generacion", "Tema Ajeno"])
    plan = analyze_sync(master, incoming)
    serialized = json.loads(json.dumps(plan.as_dict()))  # simulate file round-trip

    artist_id = serialized["artists"][0]["id"]
    album_id = serialized["artists"][0]["albums"][0]["id"]

    result = apply_decisions(serialized, {
        artist_id: "MATCH",          # confirmar artista ambiguo
        album_id: "NEW_ALBUM",       # decidir que es un álbum nuevo
        "artist:Inventado": "MATCH", # id desconocido -> rejected
    })
    assert sorted(result["applied"]) == sorted([artist_id, album_id])
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["id"] == "artist:Inventado"

    invalid = apply_decisions(serialized, {album_id: "FOO"})
    assert invalid["applied"] == [] and len(invalid["rejected"]) == 1
    assert serialized["artists"][0]["decision"] == "ARTIST_MATCHED"
    assert serialized["artists"][0]["action"] == "SYNC_ALBUMS"
    assert serialized["artists"][0]["review_required"] is False

    # summaries refreshed so the file stays self-consistent
    assert serialized["new_albums_to_copy"] == ["Babasonikos/Pasto Sessions"]
    assert serialized["needs_review"] == []


def test_apply_decisions_ignore_artist_blocks_everything(master: Path, tmp_path: Path) -> None:
    import json

    from musicfusion.sync.analyzer import apply_decisions

    incoming = tmp_path / "incoming"
    _touch_album(incoming, "Babasonicos", "2020 - Cosa Nueva", ["Tema Uno"])
    plan = analyze_sync(master, incoming)
    serialized = json.loads(json.dumps(plan.as_dict()))

    artist_id = serialized["artists"][0]["id"]
    apply_decisions(serialized, {artist_id: "IGNORE"})
    assert serialized["artists"][0]["decision"] == "NEW_ARTIST"
    assert serialized["artists"][0]["action"] == "IGNORE"
    assert serialized["new_albums_to_copy"] == []   # nada se copiará


# ---------------------------------------------------------------------------
# opción "B solo con artistas presentes en A" (Comparar)
# ---------------------------------------------------------------------------

def test_pair_folders_restricts_b_to_master_artists(tmp_path: Path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from musicfusion.config.settings import Settings
    from musicfusion.core.app import AppContext
    from musicfusion.ui.pages.compare import ComparePage

    _ = QApplication.instance() or QApplication([])
    settings = Settings(config_dir=tmp_path / "cfg")
    page = ComparePage(AppContext(settings))

    def add(root: Path, rel: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")

    a, b = tmp_path / "A", tmp_path / "B"
    for rel in [
        "Babasónicos/1992 - Pasto/01 - Intro.flac",
        "Babasónicos/2001 - Jessico/04 - Soy Rock.flac",
    ]:
        add(a, rel)
    # B: mismos artistas + uno que SOLO existe en B (debe excluirse)
    for rel in [
        "Babasonicos/1992 - Pasto/01 - Intro.flac",          # acento distinto: entra
        "Divididos/1993 - La Era de la Boludez/01 - Jijiji.flac",  # fuera
        "Charly García/1982 - Damián/01 - No Me Dejen.flac",       # fuera
    ]:
        add(b, rel)

    pairs, only_a, only_b = page._pair_folders(a, b)
    assert page._filtered_out_b == 2                     # 2 archivos excluidos
    all_b = {p.name for _, p in pairs} | {p.name for p in only_b}
    assert "01 - Jijiji.flac" not in all_b
    assert "01 - No Me Dejen.flac" not in all_b
    assert any(p.name == "01 - Intro.flac" for _, p in pairs)

    # sin la opción: el artista extra SÍ entra al análisis
    page.limit_artists_check.setChecked(False)
    pairs2, _, only_b2 = page._pair_folders(a, b)
    assert page._filtered_out_b == 0
    assert any("Jijiji" in p.name for p in only_b2)

"""Unit tests for configuration, text helpers and metadata reading."""

from __future__ import annotations

from pathlib import Path

from musicfusion.audio.metadata import read_metadata
from musicfusion.config.settings import Settings
from musicfusion.core.text import normalize_key, normalize_title


def test_settings_round_trip(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path / "cfg")
    settings.set("app.theme", "light")
    settings.set("app.max_threads", 8)

    reloaded = Settings(config_dir=tmp_path / "cfg")
    assert reloaded.get("app.theme") == "light"
    assert reloaded.get("app.max_threads") == 8


def test_add_library_persists(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path / "cfg")
    root = tmp_path / "music"
    root.mkdir()
    lib = settings.add_library(str(root), name="Mi música")
    assert lib.name == "Mi música"

    reloaded = Settings(config_dir=tmp_path / "cfg")
    libs = reloaded.libraries()
    assert len(libs) == 1
    assert libs[0].path == str(root)


def test_normalize_key_handles_accents_and_case() -> None:
    assert normalize_key("Led Zeppelin") == normalize_key("led zeppelin")
    assert normalize_key("Álvaro") == "alvaro"
    assert normalize_key("Mötley Crüe") == "motley crue"
    assert normalize_title("  BoRhAp  ") == "borhap"
    assert normalize_title("The Song") == "the song"


def test_read_metadata_missing_file(tmp_path: Path) -> None:
    result = read_metadata(tmp_path / "nope.mp3")
    assert result is None


def test_structure_key_ignores_accents_and_punctuation() -> None:
    from musicfusion.core.text import structure_key

    a = structure_key(("Babasonicos", "1992 - Pasto", "01 - Intro.ogg"))
    b = structure_key(("Babasónicos", "1992 - Pasto", "01 - Intro.ogg"))
    assert a == b
    # punctuation-only differences collapse too
    assert structure_key(("X", "2009 - Mucho ", "12 - Guarda D.P..ogg")) == structure_key(
        ("X", "2009 - Mucho +", "12 - ¡Guarda D.P.!.ogg")
    )
    assert structure_key(("A", "2000 - X", "La Era del Amor Parte 1.ogg")) == structure_key(
        ("A", "2000 - X", "La Era del Amor (Parte 1).ogg")
    )
    # genuinely different names still differ
    assert a != structure_key(("Babasonicos", "1994 - Trance Zomba", "01 - Desarmate.ogg"))


def test_folder_layout_derivation() -> None:
    from musicfusion.core.text import (
        album_from_folder,
        title_from_filename,
        track_no_from_filename,
        year_from_folder,
    )

    assert track_no_from_filename("07 - Cancion de la Bandera") == 7
    assert title_from_filename("07 - Cancion de la Bandera") == "cancion de la bandera"
    assert title_from_filename("La Era del Amor (Parte 1)") == "la era del amor (parte 1)"
    assert album_from_folder("1992 - Pasto") == "pasto"
    assert year_from_folder("2016 - Desde Adentro - Impuesto de Fe En Vivo") == 2016

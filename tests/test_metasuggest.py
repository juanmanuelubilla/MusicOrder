"""Tests for metadata centralization suggestions (Phase: Metadatos).

A library where the same song exists twice -- once richly tagged, once poorly
tagged -- must produce a suggestion to fill the missing fields, and applying it
must actually write the tags into the file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _av_available() -> bool:
    try:
        return (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode == 0
        )
    except OSError:
        return False


def _write_audio(path: Path, flac: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = "flac" if flac else "libmp3lame"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            codec,
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _tag_flac(path: Path, **tags: str) -> None:
    import mutagen

    audio = mutagen.File(path)
    audio.update(tags)
    audio.save()


def _index(settings, db, folder: Path) -> None:
    from musicfusion.config.settings import LibraryConfig
    from musicfusion.scan.indexer import LibraryIndexer

    settings.set("scan.include_extensions", [".flac", ".mp3"])
    LibraryIndexer(settings, db).scan_library(LibraryConfig(path=str(folder)))


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_suggestion_fills_missing_tags(tmp_path: Path) -> None:
    from musicfusion.audio.metadata import read_metadata
    from musicfusion.config.settings import Settings
    from musicfusion.db.session import Database
    from musicfusion.services.metasuggest import apply_one, build_suggestions

    rich = tmp_path / "rich" / "song.flac"
    poor = tmp_path / "poor" / "song.flac"
    _write_audio(rich)
    _write_audio(poor)
    _tag_flac(
        rich,
        TITLE="Don't Stop Me Now",
        ARTIST="Queen",
        ALBUM="Gold",
        ALBUMARTIST="Queen",
        DATE="1981",
        GENRE="Rock",
    )
    _tag_flac(poor, TITLE="Don't Stop Me Now", ARTIST="Queen")

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    db = Database(settings.db_path)
    db.create_schema()
    _index(settings, db, tmp_path / "rich")
    _index(settings, db, tmp_path / "poor")

    suggestions = build_suggestions(settings, db, roots=[tmp_path / "rich", tmp_path / "poor"])
    assert suggestions, "a suggestion should be found"

    suggestion = next(
        s for s in suggestions if str(s.target_path) == str(poor) and str(s.donor_path) == str(rich)
    )
    assert sorted(suggestion.missing) == ["album", "album_artist", "genre", "year"]
    assert suggestion.matched_by == "metadatos"

    errors = apply_one(db, suggestion)
    assert not errors

    refreshed = read_metadata(poor)
    assert refreshed is not None
    assert refreshed.album == "Gold"
    assert refreshed.year == 1981
    assert refreshed.genre == "Rock"


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_no_suggestion_when_metadata_already_equal(tmp_path: Path) -> None:
    from musicfusion.config.settings import Settings
    from musicfusion.db.session import Database
    from musicfusion.services.metasuggest import build_suggestions

    first = tmp_path / "first" / "song.flac"
    second = tmp_path / "second" / "song.flac"
    _write_audio(first)
    _write_audio(second)
    _tag_flac(first, TITLE="Same Song", ARTIST="Band", ALBUM="Album")
    _tag_flac(second, TITLE="Same Song", ARTIST="Band", ALBUM="Album")

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    db = Database(settings.db_path)
    db.create_schema()
    _index(settings, db, tmp_path / "first")
    _index(settings, db, tmp_path / "second")

    suggestions = build_suggestions(settings, db, roots=[tmp_path / "first", tmp_path / "second"])
    assert suggestions == []


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_apply_one_with_art_copies_donor_cover(tmp_path: Path) -> None:
    import base64

    from musicfusion.audio.artwork import discover_art
    from musicfusion.config.settings import Settings
    from musicfusion.db.session import Database
    from musicfusion.services.metasuggest import apply_one_with_art, build_donor_map

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )

    rich = tmp_path / "rich" / "song.flac"
    poor = tmp_path / "poor" / "song.flac"
    _write_audio(rich)
    _write_audio(poor)
    _tag_flac(rich, TITLE="Don't Stop Me Now", ARTIST="Queen", ALBUM="Gold")
    _tag_flac(poor, TITLE="Don't Stop Me Now", ARTIST="Queen")

    from mutagen.flac import Picture

    audio = __import__("mutagen").File(str(rich))
    assert audio is not None
    picture = Picture()
    picture.type = 3
    picture.mime = "image/png"
    picture.desc = "front cover"
    picture.width = 1
    picture.height = 1
    picture.data = png
    audio.add_picture(picture)
    audio.save()

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    db = Database(settings.db_path)
    db.create_schema()
    _index(settings, db, tmp_path / "rich")
    _index(settings, db, tmp_path / "poor")

    donor_map = build_donor_map(settings, db)
    suggestion = donor_map.get(str(poor))
    assert suggestion is not None
    assert str(suggestion.donor_path) == str(rich)

    errors = apply_one_with_art(db, suggestion)
    assert not errors
    assert discover_art(poor) is not None

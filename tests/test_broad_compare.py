"""Tests for the broad ("compare everything") content comparison.

Covers the ``Amplio`` mode of the Comparar page: same song found across codecs
by acoustic content or metadata, "solo en A/B" leftovers, and full persistence
into the shared database (tracks, match records, duplicate groups).
"""

from __future__ import annotations

import shutil
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


def _write_audio(path: Path, flac: bool, frequency: int = 440, duration: float = 6.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = "flac" if flac else "libmp3lame"
    extra = [] if flac else ["-b:a", "96k"]
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={duration}",
            "-c:a",
            codec,
            *extra,
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _tag(path: Path, artist: str, title: str, album: str) -> None:
    import mutagen
    from mutagen.id3 import ID3, TALB, TIT2, TPE1

    if path.suffix == ".flac":
        tags = mutagen.File(path)
        tags["TITLE"] = title
        tags["ARTIST"] = artist
        tags["ALBUM"] = album
        tags.save()
    else:
        id3 = ID3(path)
        id3.add(TIT2(encoding=3, text=title))
        id3.add(TPE1(encoding=3, text=artist))
        id3.add(TALB(encoding=3, text=album))
        id3.save()


def _make_case(tmp_path: Path) -> tuple[Path, Path]:
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    _write_audio(root_a / "same.flac", flac=True)
    _write_audio(root_b / "same.mp3", flac=False)
    _tag(root_a / "same.flac", "Queen", "Don't Stop Me Now", "Gold")
    _tag(root_b / "same.mp3", "Queen", "Don't Stop Me Now", "Gold")
    _write_audio(root_a / "extra-a.flac", flac=True, frequency=523)
    _tag(root_a / "extra-a.flac", "Queen", "Bohemian Rhapsody", "Opera")
    _write_audio(root_b / "extra-b.flac", flac=True, frequency=659)
    _tag(root_b / "extra-b.flac", "Queen", "We Will Rock You", "News")
    return root_a, root_b


def _settings(db_path: Path, tmp_path: Path):
    from musicfusion.config.settings import Settings

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(db_path))
    settings.set("watch.enabled", False)
    return settings


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_broad_compare_matches_across_codecs_and_reports_rest(tmp_path: Path) -> None:
    from musicfusion.compare.broad import compare_all
    from musicfusion.db.session import Database

    root_a, root_b = _make_case(tmp_path)
    settings = _settings(tmp_path / "db.sqlite", tmp_path)
    db = Database(settings.db_path)
    db.create_schema()

    result = compare_all(settings, db, root_a, root_b, use_fingerprints=False)
    assert result.pairs_count == 1
    assert result.matched == 1
    assert result.by_metadata == 1
    assert [p.name for p in result.only_a] == ["extra-a.flac"]
    assert [p.name for p in result.only_b] == ["extra-b.flac"]
    assert result.saved["duplicates"] >= 1
    assert result.saved["decisions"] >= 1


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_broad_compare_persists_and_is_idempotent(tmp_path: Path) -> None:
    from musicfusion.compare.broad import compare_all
    from musicfusion.db.session import Database

    root_a, root_b = _make_case(tmp_path)
    settings = _settings(tmp_path / "db.sqlite", tmp_path)
    db = Database(settings.db_path)
    db.create_schema()

    compare_all(settings, db, root_a, root_b, use_fingerprints=False)
    compare_all(settings, db, root_a, root_b, use_fingerprints=False)

    from musicfusion.db.models import DuplicateGroup

    with db.session_scope() as session:
        assert session.query(DuplicateGroup).count() == 1


def _fingerprint_available() -> bool:
    from musicfusion.audio.fingerprint import fingerprint_available

    return fingerprint_available()


@pytest.mark.skipif(
    not _av_available() or not _fingerprint_available(),
    reason="ffmpeg + fingerprint backend required",
)
def test_broad_compare_uses_acoustic_fingerprints(tmp_path: Path) -> None:
    from musicfusion.compare.broad import compare_all
    from musicfusion.db.session import Database

    root_a, root_b = _make_case(tmp_path)
    settings = _settings(tmp_path / "db.sqlite", tmp_path)
    db = Database(settings.db_path)
    db.create_schema()

    result = compare_all(settings, db, root_a, root_b, use_fingerprints=True)
    # same audio decoded from flac+mp3 groups by fingerprint (different bytes)
    assert result.by_fingerprint >= 1
    assert result.matched >= 1


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_broad_compare_same_folder_detects_internal_duplicates(tmp_path: Path) -> None:
    from musicfusion.compare.broad import compare_all
    from musicfusion.db.session import Database

    root = tmp_path / "library"
    source = tmp_path / "src.flac"
    _write_audio(source, flac=True)

    copy_one = root / "copy-one.flac"
    copy_two = root / "copy-two.flac"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, copy_one)
    shutil.copy(source, copy_two)
    _tag(copy_one, "Band", "Same", "Album")
    _tag(copy_two, "Band", "Same", "Album")

    version_a = root / "retake-a.flac"
    version_b = root / "retake-b.flac"
    _write_audio(version_a, flac=True, frequency=329)
    _write_audio(version_b, flac=True, frequency=392)
    _tag(version_a, "Band", "Retake", "Album")
    _tag(version_b, "Band", "Retake", "Album")

    unique = root / "unique.flac"
    _write_audio(unique, flac=True, frequency=440)
    _tag(unique, "Band", "Unique Song", "Album")

    settings = _settings(tmp_path / "db.sqlite", tmp_path)
    db = Database(settings.db_path)
    db.create_schema()

    result = compare_all(settings, db, root, root, use_fingerprints=False)
    # a file must never be compared with itself
    assert all(p.path_a != p.path_b for p in result.pairs)
    # sha256 group: the two identical-byte copies
    assert result.by_sha256 == 1
    # metadata cluster: Retake versions detected although bytes differ
    assert result.by_metadata == 1
    assert result.pairs_count == 2
    assert result.matched == 2
    # no "solo en A/B" leftovers for a single-folder comparison
    assert not result.only_a and not result.only_b
    assert result.saved["duplicates"] >= 1


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_broad_compare_identical_bytes_grouped_by_sha256(tmp_path: Path) -> None:
    from musicfusion.compare.broad import compare_all
    from musicfusion.db.session import Database

    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    source = tmp_path / "src.flac"
    _write_audio(source, flac=True)
    copy_a = root_a / "same.flac"
    copy_b = root_b / "same.flac"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, copy_a)
    shutil.copy(source, copy_b)
    _tag(copy_a, "Band", "Same", "Album")
    _tag(copy_b, "Band", "Same", "Album")

    settings = _settings(tmp_path / "db.sqlite", tmp_path)
    db = Database(settings.db_path)
    db.create_schema()

    result = compare_all(settings, db, root_a, root_b, use_fingerprints=False)
    assert result.by_sha256 == 1
    assert result.matched == 1
    assert not result.only_a and not result.only_b

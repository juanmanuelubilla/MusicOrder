"""End-to-end tests for the library indexer over generated audio files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musicfusion.config.settings import Settings
from musicfusion.db.models import Library, Track
from musicfusion.db.session import Database
from musicfusion.scan.indexer import LibraryIndexer


def _av_available() -> bool:
    try:
        return (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode == 0
        )
    except OSError:
        return False


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_indexer_scan_and_skip(tmp_path: Path) -> None:
    lib = _make_library(tmp_path / "music", count=3)
    settings = _make_settings(tmp_path, lib)

    stats = LibraryIndexer(settings).run()
    assert stats["files_found"] == 3
    assert stats["files_added"] == 3

    stats2 = LibraryIndexer(settings).run()
    assert stats2["files_added"] == 0
    assert stats2["files_skipped"] == 3

    with Database(settings.db_path).session_scope() as session:
        assert session.query(Track).count() == 3
        assert session.query(Library).count() == 1


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_indexer_detects_new_file(tmp_path: Path) -> None:
    lib = _make_library(tmp_path / "music", count=2)
    settings = _make_settings(tmp_path, lib)
    LibraryIndexer(settings).run()

    _write_mp3(lib / "third.mp3")
    stats = LibraryIndexer(settings).run()
    assert stats["files_added"] == 1


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_indexer_stores_quality_for_decision(tmp_path: Path) -> None:
    """Regression: scanner must persist quality_score/codec for the Decision Engine."""
    lib = _make_library(tmp_path / "music", count=1)
    settings = _make_settings(tmp_path, lib)
    LibraryIndexer(settings).run()

    with Database(settings.db_path).session_scope() as session:
        track = session.query(Track).one()
        assert track.quality_score is not None and track.quality_score > 0
        assert track.codec


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_indexer_reports_real_progress(tmp_path: Path) -> None:
    """Progress must carry a real total so the ring can show a percentage."""
    lib = _make_library(tmp_path / "music", count=3)
    settings = _make_settings(tmp_path, lib)

    events: list[tuple[int, int]] = []
    LibraryIndexer(settings).run(
        on_progress=lambda step, total, msg: events.append((step, total))
    )
    assert events
    assert all(total == 3 for _step, total in events)
    assert max(step for step, _total in events) == 3


def _make_library(root: Path, count: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        _write_mp3(root / f"track{index + 1}.mp3")
    return root


def _write_mp3(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-codec:a", "libmp3lame", "-q:a", "9", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_settings(tmp_path: Path, lib: Path) -> Settings:
    settings = Settings(config_dir=tmp_path / "config")
    settings.add_library(str(lib))
    settings.set("watch.enabled", False)
    settings.set("scan.compute_fingerprints", False)
    return settings

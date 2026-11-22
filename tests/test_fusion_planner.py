"""Tests for the fusion planner honouring duplicate-group verdicts.

After a folder comparison (Comparar) persists duplicate groups and Decision
records, a fusion of the same folder (or of two folders sharing duplicates)
must copy only the winning copy of every duplicate song and skip the losers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musicfusion.decision.storage import group_key


def _av_available() -> bool:
    try:
        return (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode == 0
        )
    except OSError:
        return False


def _write_audio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "flac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _index(settings, db, folder: Path) -> None:
    from musicfusion.config.settings import LibraryConfig
    from musicfusion.scan.indexer import LibraryIndexer

    LibraryIndexer(settings, db).scan_library(LibraryConfig(path=str(folder)))


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_plan_skips_duplicate_loser_and_keeps_decision_winner(tmp_path: Path) -> None:
    import mutagen

    from musicfusion.config.settings import Settings
    from musicfusion.db.models import DecisionRecord, DuplicateEntry, DuplicateGroup, Track
    from musicfusion.db.session import Database
    from musicfusion.merge.planner import FusionPlanner

    folder = tmp_path / "library"
    copy_one = folder / "copy-one.flac"
    copy_two = folder / "copy-two.flac"
    _write_audio(copy_one)
    _write_audio(copy_two)
    for path in (copy_one, copy_two):
        audio = mutagen.File(str(path))
        assert audio is not None
        audio.update({"TITLE": "Same Song", "ARTIST": "Band", "ALBUM": "Album"})
        audio.save()

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    settings.set("scan.include_extensions", [".flac"])
    db = Database(settings.db_path)
    db.create_schema()
    _index(settings, db, folder)

    with db.session_scope() as session:
        rows = dict(session.query(Track.path, Track.id).all())
        assert str(copy_one) in rows and str(copy_two) in rows
        group = DuplicateGroup(reason="compare")
        session.add(group)
        session.flush()
        session.add(DuplicateEntry(group_id=group.id, track_id=rows[str(copy_one)]))
        session.add(DuplicateEntry(group_id=group.id, track_id=rows[str(copy_two)]))
        session.add(
            DecisionRecord(
                group_key=group_key(str(copy_one), str(copy_two)),
                winner_path=str(copy_one),
                winner_score=90.0,
                runner_up_path=str(copy_two),
                runner_up_score=10.0,
                winner_track_id=rows[str(copy_one)],
            )
        )

    destination = tmp_path / "dest"
    planner = FusionPlanner(settings, db)
    plan = planner.plan(folder, folder, destination)

    copies = [action for action in plan.actions if action.kind == "copy"]
    losers = [action for action in plan.actions if action.kind == "skip"]

    assert len(copies) == 1
    assert copies[0].source == copy_one.resolve()
    assert not any(action.source == copy_two for action in plan.actions if action.kind == "copy")
    assert any(action.kind == "skip" and action.source == copy_two for action in losers)


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_single_folder_plan_copies_only_winners_and_uniques(tmp_path: Path) -> None:
    """Fusing a folder with itself must copy only non-duplicates (winner + unique)."""
    import mutagen

    from musicfusion.config.settings import Settings
    from musicfusion.db.models import DecisionRecord, DuplicateEntry, DuplicateGroup, Track
    from musicfusion.db.session import Database
    from musicfusion.merge.planner import FusionPlanner

    folder = tmp_path / "library"
    copy_one = folder / "copy-one.flac"
    copy_two = folder / "copy-two.flac"
    unique = folder / "unique.flac"
    _write_audio(copy_one)
    _write_audio(copy_two)
    _write_audio(unique)
    for path in (copy_one, copy_two, unique):
        audio = mutagen.File(str(path))
        assert audio is not None
        audio.update({"TITLE": "Same Song", "ARTIST": "Band", "ALBUM": "Album"})
        audio.save()

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    settings.set("scan.include_extensions", [".flac"])
    db = Database(settings.db_path)
    db.create_schema()
    _index(settings, db, folder)

    with db.session_scope() as session:
        rows = dict(session.query(Track.path, Track.id).all())
        group = DuplicateGroup(reason="compare")
        session.add(group)
        session.flush()
        session.add(DuplicateEntry(group_id=group.id, track_id=rows[str(copy_one)]))
        session.add(DuplicateEntry(group_id=group.id, track_id=rows[str(copy_two)]))
        session.add(
            DecisionRecord(
                group_key=group_key(str(copy_one), str(copy_two)),
                winner_path=str(copy_one),
                winner_score=90.0,
                runner_up_path=str(copy_two),
                runner_up_score=10.0,
                winner_track_id=rows[str(copy_one)],
            )
        )

    destination = tmp_path / "dest"
    planner = FusionPlanner(settings, db)
    plan = planner.plan(folder, folder, destination)

    copies = sorted(action.source for action in plan.actions if action.kind == "copy")
    losers = [action for action in plan.actions if action.kind == "skip"]

    assert copies == [copy_one.resolve(), unique.resolve()]
    assert not any(action.source == copy_two for action in plan.actions if action.kind == "copy")
    assert any(action.kind == "skip" and action.source == copy_two for action in losers)
    assert plan.lib_a == plan.lib_b == folder

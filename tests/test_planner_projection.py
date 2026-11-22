"""Regression tests for the fusion planner's DB-track projection.

Covers the two-library planning path that crashed in production when
``track_to_match_track`` guessed wrong column names on the Track model.
"""

from __future__ import annotations

from pathlib import Path

from musicfusion.config.settings import Settings
from musicfusion.db.models import Album, Artist, Library, Track
from musicfusion.db.session import Database


def _make_track(db: Database, tmp_path: Path) -> Track:
    with db.session_scope() as session:
        lib = Library(path=str(tmp_path / "lib"), name="L")
        artist = Artist(name="Babasónicos")
        session.add(lib)
        session.add(artist)
        session.flush()
        album = Album(
            title="Pasto", year=1992, artist=artist,
            artist_name="Babasónicos", title_key="pasto",
        )
        session.add(album)
        session.flush()
        track = Track(
            path=str(tmp_path / "x.flac"),
            title="D-Generación",
            title_key="d generacion",
            artist_name="Babasónicos",
            album_artist="Babasónicos",
            album_name="Pasto",
            track_no=2,
            disc_no=None,
            year=1992,
            duration_ms=200_000,
            file_size=1234,
            mtime_ns=1,
            ctime_ns=1,
            library_id=lib.id,
            album_id=album.id,
        )
        session.add(track)
        session.flush()
        return track

def test_track_to_match_track_maps_real_columns(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path / "cfg")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    db = Database(settings.db_path)
    db.create_schema()

    track = _make_track(db, tmp_path)
    from musicfusion.merge.planner import track_to_match_track

    mt = track_to_match_track(track, {track.id: "fp123"})
    assert mt.artist == "Babasónicos"
    assert mt.album_artist == "Babasónicos"
    assert mt.album == "Pasto"
    assert mt.title == "D-Generación"
    assert mt.track_no == 2
    assert mt.year == 1992
    assert mt.duration_ms == 200_000
    assert mt.fingerprint == "fp123"   # indexer fingerprint travels along
    assert mt.id == track.id


def test_two_library_plan_runs_end_to_end(tmp_path: Path) -> None:
    """plan(A, B) with distinct folders must not crash on column mapping."""
    import subprocess

    try:
        ok = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
    except OSError:
        ok = False
    if not ok:
        import pytest

        pytest.skip("ffmpeg required")

    import mutagen

    from musicfusion.merge.planner import FusionPlanner

    lib_a = tmp_path / "A"
    lib_b = tmp_path / "B"

    def write(root: Path, name: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(path)],
            check=True, capture_output=True,
        )
        audio = mutagen.File(str(path))
        audio.update({"TITLE": "Tema X", "ARTIST": "Banda", "ALBUM": "Album"})
        audio.save()

    write(lib_a, "tema.flac")
    write(lib_b, "tema.flac")  # same song in both libraries

    settings = Settings(config_dir=tmp_path / "cfg")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    settings.set("scan.include_extensions", [".flac"])
    db = Database(settings.db_path)
    db.create_schema()

    from musicfusion.config.settings import LibraryConfig
    from musicfusion.scan.indexer import LibraryIndexer

    indexer = LibraryIndexer(settings, db)
    for folder in (lib_a, lib_b):
        indexer.scan_library(LibraryConfig(path=str(folder)))

    planner = FusionPlanner(settings, db)
    plan = planner.plan(lib_a, lib_b, tmp_path / "dest")

    copies = [a for a in plan.actions if a.kind == "copy"]
    # both files are the same recording: exactly one copy planned
    assert len(copies) == 1

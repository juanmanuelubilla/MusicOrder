"""Tests for duplicate detection, playlists and report exporters."""

from __future__ import annotations

from pathlib import Path

from musicfusion.db.models import Album, Artist, Library, Track
from musicfusion.db.session import Database
from musicfusion.duplicates.detect import DuplicateDetector
from musicfusion.playlists.manager import Playlist, PlaylistEntry, PlaylistManager
from musicfusion.reports.exporter import export_data, export_duplicates


def test_playlist_round_trip(tmp_path: Path) -> None:
    playlist = Playlist(
        name="mixes",
        entries=[
            PlaylistEntry(path="/tmp/a.mp3", title="Alpha", duration=300),
            PlaylistEntry(path="/tmp/b.mp3", title="Beta"),
        ],
    )
    manager = PlaylistManager()
    m3u8 = manager.export(playlist, tmp_path / "mix.m3u8")
    parsed = manager.import_file(m3u8)
    assert [entry.title for entry in parsed.entries] == ["Alpha", "Beta"]
    assert parsed.extended is True


def test_playlist_pls_round_trip(tmp_path: Path) -> None:
    playlist = Playlist(
        name="radio",
        entries=[PlaylistEntry(path="/tmp/only.mp3", title="Only", duration=200)],
    )
    manager = PlaylistManager()
    pls = manager.export(playlist, tmp_path / "radio.pls")
    parsed = manager.import_file(pls)
    assert parsed.name == "radio"
    assert len(parsed.entries) == 1
    assert parsed.entries[0].title == "Only"


def test_export_csv_and_html(tmp_path: Path) -> None:
    rows = [{"title": "Song", "artist": "Artist"}]
    csv_path = export_data(rows, tmp_path / "out.csv")
    html_path = export_data(rows, tmp_path / "out.html", title="Informe")
    assert csv_path.exists()
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "Song" in content


def test_export_duplicates_dispatch(tmp_path: Path) -> None:
    rows = [{"group": 1, "reason": "hash", "title": "Same", "path": "/tmp/x.mp3"}]
    target = export_duplicates(rows, tmp_path / "dupes.csv")
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "Same" in text


def _seed_detector(db: Database, root: Path) -> None:
    with db.session_scope() as session:
        lib = Library(name="root", path=str(root.resolve()), enabled=True)
        session.add(lib)
        session.flush()
        artist = Artist(name="Artist")
        session.add(artist)
        session.flush()
        album = Album(title="Album", title_key="album", artist_name="Artist", artist_id=artist.id)
        session.add(album)
        session.flush()
        rows = [
            (root / "x.mp3", "shaA"),
            (root / "y.mp3", "shaA"),
            (root / "sub" / "z.mp3", "shaB"),
        ]
        for path, sha in rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            session.add(
                Track(
                    path=str(path.resolve()), library_id=lib.id, artist_name="Artist",
                    title=path.stem, title_key=path.stem.lower(), sha256=sha,
                    file_size=10, mtime_ns=1, ctime_ns=1, duration_ms=120_000,
                )
            )
        session.flush()


def test_detector_scopes_by_path(tmp_path: Path) -> None:
    db = Database(tmp_path / "bulk.db")
    db.create_schema()
    _seed_detector(db, tmp_path)

    # Only files under the root's own branch count: the sub/ file is excluded.
    groups = DuplicateDetector(None, db).run(paths=[tmp_path / "sub"])
    assert groups == []

    groups = DuplicateDetector(None, db).run(paths=[tmp_path])
    assert any(group.reason == "hash" and len(group.track_ids) == 2 for group in groups)


def test_detector_scopes_two_paths(tmp_path: Path) -> None:
    db = Database(tmp_path / "two.db")
    db.create_schema()
    _seed_detector(db, tmp_path)

    folder_a = tmp_path
    folder_b = tmp_path / "sub"
    groups = DuplicateDetector(None, db).run(paths=[folder_a, folder_b])
    assert any(group.reason == "hash" and len(group.track_ids) == 2 for group in groups)

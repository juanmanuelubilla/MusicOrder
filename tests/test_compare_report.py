"""Tests for the folder-comparison persistence layer and schema migration.

These are the tests that make the comparison the *base* of the application:
after comparing two folders, the analysis must be visible to Duplicados /
Fusion via the shared database (``tracks``, ``match_records``,
``duplicate_groups`` and ``decision_records``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from musicfusion.config.settings import Settings
from musicfusion.db.models import (
    DecisionRecord,
    DuplicateEntry,
    DuplicateGroup,
    MatchRecord,
    Track,
)
from musicfusion.db.session import Database

# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migrate_adds_missing_columns(tmp_path: Path) -> None:
    """An older ``match_records`` table must be upgraded in place."""
    old = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{old}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE match_records (
                id INTEGER PRIMARY KEY,
                source_file TEXT NOT NULL,
                candidate_file TEXT NOT NULL,
                cache_key VARCHAR(64) NOT NULL,
                score FLOAT NOT NULL DEFAULT 0,
                classification VARCHAR(32) NOT NULL,
                matching_method VARCHAR(64) NOT NULL DEFAULT 'weighted'
            )
            """
        )
    Database(old).create_schema()
    columns = {column["name"] for column in inspect(engine).get_columns("match_records")}
    for expected in ("same_song", "same_version", "edition_different",
                     "version_different", "version_tag", "decision", "invalidated"):
        assert expected in columns


def test_create_schema_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    db.create_schema()
    db.create_schema()  # second call must not raise


# ---------------------------------------------------------------------------
# folder comparison persistence (requires ffmpeg)
# ---------------------------------------------------------------------------


def _av_available() -> bool:
    try:
        return (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False).returncode == 0
        )
    except OSError:
        return False


def _write_audio(path: Path, flac: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = "flac" if flac else "libmp3lame"
    extra = [] if flac else ["-b:a", "128k"]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", codec, *extra, str(path)],
        check=True, capture_output=True,
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


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_folder_compare_persists_analysis(tmp_path: Path) -> None:
    from musicfusion.audio.metadata import read_metadata
    from musicfusion.audio.quality import probe_quality
    from musicfusion.compare.report import PairRecord, persist_folder_compare
    from musicfusion.decision.engine import Candidate, DecisionEngine
    from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack

    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    song_a = root_a / "x" / "song.flac"
    song_b = root_b / "x" / "song.mp3"
    _write_audio(song_a, flac=True)
    _write_audio(song_b, flac=False)
    _tag(song_a, "Queen", "Don't Stop Me Now", "Gold")
    _tag(song_b, "Queen", "Don't Stop Me Now", "Gold")

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    settings.set("watch.enabled", False)
    db = Database(settings.db_path)
    db.create_schema()

    config = MatchConfig.from_settings(settings)
    engine = MatchEngine(config)
    info_a = read_metadata(song_a)
    info_b = read_metadata(song_b)

    def to_track(path: Path, info) -> MatchTrack:
        return MatchTrack(
            path=str(path), artist=info.artist or "", album=info.album or "",
            title=(info.title or "") or path.stem.lower(),
            track_no=info.track_no, disc_no=info.disc_no, year=info.year,
            duration_ms=info.duration_ms or 0, fingerprint="", recording_id="",
        )

    track_a = to_track(song_a, info_a)
    track_b = to_track(song_b, info_b)
    outcome = engine.match(track_a, track_b)
    qual_a = probe_quality(song_a)
    qual_b = probe_quality(song_b)

    def score(quality) -> float:
        return quality.score() if quality else 0.0

    decision = DecisionEngine.from_settings(settings).decide(
        [
            Candidate(path=str(song_a), quality_score=score(qual_a),
                      metadata_score=90.0, codec="flac"),
            Candidate(path=str(song_b), quality_score=score(qual_b),
                      metadata_score=90.0, codec="mp3"),
        ]
    )
    decision.weights = {}  # type: ignore[attr-defined]

    stats = persist_folder_compare(
        settings,
        db,
        [root_a, root_b],
        [PairRecord(
            path_a=song_a, path_b=song_b, outcome=outcome,
            track_a=track_a, track_b=track_b, qual_a=qual_a, qual_b=qual_b,
            decision=decision, same=True,
        )],
    )
    assert stats["pairs"] == 1
    assert stats["matches"] == 1
    assert stats["duplicates"] == 1
    assert stats["decisions"] == 1

    with db.session_scope() as session:
        assert session.query(Track).count() >= 2
        match = session.query(MatchRecord).first()
        assert match is not None
        assert match.cache_key
        assert match.source_track_id is not None
        assert match.candidate_track_id is not None
        assert session.query(DuplicateGroup).count() == 1
        assert session.query(DuplicateEntry).count() == 2
        decision_row = session.query(DecisionRecord).first()
        assert decision_row is not None
        assert decision_row.winner_track_id is not None
        assert decision_row.match_ids == [match.id]


@pytest.mark.skipif(not _av_available(), reason="ffmpeg required")
def test_folder_compare_persistence_is_idempotent(tmp_path: Path) -> None:
    """Running the same comparison twice must not duplicate groups."""
    from musicfusion.audio.quality import probe_quality
    from musicfusion.compare.report import PairRecord, persist_folder_compare
    from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack

    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    song_a = root_a / "same.flac"
    song_b = root_b / "same.mp3"
    _write_audio(song_a, flac=True)
    _write_audio(song_b, flac=False)
    _tag(song_a, "ABBA", "Dancing Queen", "Gold")
    _tag(song_b, "ABBA", "Dancing Queen", "Gold")

    settings = Settings(config_dir=tmp_path / "config")
    settings.set("db.path", str(tmp_path / "db.sqlite"))
    db = Database(settings.db_path)
    db.create_schema()

    def pair() -> PairRecord:
        track_a = MatchTrack(path=str(song_a), artist="ABBA", title="Dancing Queen",
                             album="Gold", track_no=1, disc_no=1, duration_ms=2000)
        track_b = MatchTrack(path=str(song_b), artist="ABBA", title="Dancing Queen",
                             album="Gold", track_no=1, disc_no=1, duration_ms=2000)
        outcome = MatchEngine(MatchConfig.from_settings(settings)).match(track_a, track_b)
        return PairRecord(path_a=song_a, path_b=song_b, outcome=outcome,
                          track_a=track_a, track_b=track_b,
                          qual_a=probe_quality(song_a), qual_b=probe_quality(song_b),
                          decision=None, same=True)

    persist_folder_compare(settings, db, [root_a, root_b], [pair()])
    persist_folder_compare(settings, db, [root_a, root_b], [pair()])
    with db.session_scope() as session:
        assert session.query(DuplicateGroup).count() == 1
        assert session.query(DuplicateEntry).count() == 2

"""Comprehensive test suite for the MusicFusion deduplication & merge engine.

Validates all 10 required test scenarios from the specification §11:
  1. Duplicates with identical file names
  2. Duplicates with different file names
  3. Files with identical names but different content (incremental renaming conflict resolution)
  4. Files already existing in the destination (idempotency, repeated execution)
  5. Nested / deeply hierarchical directories
  6. Empty files (0 bytes)
  7. Large files (block reading verification)
  8. Unicode file names (accents, Japanese, emoji, Cyrillic, special chars)
  9. Permission / I/O errors handling
 10. Cooperative cancellation during processing
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from musicfusion.audio.hashing import hash_file
from musicfusion.merge.engine import (
    FusionEngine,
    FusionOptions,
    FusionReport,
    ProgressData,
)


def _create_audio_file(path: Path, content: bytes) -> Path:
    """Helper to create a dummy audio file with specific byte content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# 1. Duplicates with identical file names
# ---------------------------------------------------------------------------
def test_duplicates_with_identical_names(tmp_path: Path) -> None:
    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    dest = tmp_path / "destination"

    content = b"AUDIO_SAMPLE_CONTENT_TEST_SAME_NAME" * 50
    _create_audio_file(src_a / "song.mp3", content)
    _create_audio_file(src_b / "song.mp3", content)

    engine = FusionEngine(FusionOptions(sources=[src_a, src_b], destination=dest))
    report = engine.run()

    # Exactly 1 file copied, 1 duplicate detected and omitted
    assert report.total_found == 2
    assert report.unique_files == 1
    assert report.files_copied == 1
    assert report.duplicates_detected == 1
    assert report.errors_count == 0

    dest_files = list(dest.glob("*.mp3"))
    assert len(dest_files) == 1
    assert dest_files[0].name == "song.mp3"
    assert dest_files[0].read_bytes() == content


def test_babasonicos_accent_unification_and_deduplication(tmp_path: Path) -> None:
    """Exact scenario from user request:
    Folder 'a' has 'Babasonicos' and 'Babasónicos' with tracks having differing accents.
    Folder 'b' has 'Babasónicos'.
    Destination MUST have only ONE 'Babasónicos' folder, and identical tracks must be
    copied exactly once.
    """
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    dest = tmp_path / "dest"

    track1_content = b"INTRO_AUDIO_CONTENT_HASH_1"
    track2_content = b"D_GENERACION_AUDIO_CONTENT_HASH_2"
    track3_content = b"MIAMI_AUDIO_CONTENT_HASH_3"

    # In folder a (without accents in folder and some track names)
    _create_audio_file(src_a / "Babasonicos" / "1992 - Pasto" / "01 - Intro.ogg", track1_content)
    _create_audio_file(src_a / "Babasonicos" / "1992 - Pasto" / "02 - D-Generacion.ogg", track2_content)
    # Also in folder a with accent
    _create_audio_file(src_a / "Babasónicos" / "1999 - Miami" / "01 - 4 A.M..ogg", track3_content)

    # In folder b (with accents in folder and track names)
    _create_audio_file(src_b / "Babasónicos" / "1992 - Pasto" / "01 - Intro.ogg", track1_content)
    _create_audio_file(src_b / "Babasónicos" / "1992 - Pasto" / "02 - D-Generación.ogg", track2_content)
    _create_audio_file(src_b / "Babasónicos" / "1999 - Miami" / "01 - 4 A.M..ogg", track3_content)

    engine = FusionEngine(FusionOptions(sources=[src_a, src_b], destination=dest, preserve_structure=True))
    report = engine.run()

    # Total found = 6 files. Exactly 3 unique copied, 3 omitted as duplicates
    assert report.total_found == 6
    assert report.unique_files == 3
    assert report.files_copied == 3
    assert report.duplicates_detected == 3

    # CRITICAL CHECK: In destination, ONLY 'Babasónicos' must exist (no 'Babasonicos'!)
    top_level_dest_dirs = [d.name for d in dest.iterdir() if d.is_dir()]
    assert top_level_dest_dirs == ["Babasónicos"]

    # Check album folders under Babasónicos
    album_dirs = sorted([d.name for d in (dest / "Babasónicos").iterdir() if d.is_dir()])
    assert album_dirs == ["1992 - Pasto", "1999 - Miami"]

    # Check files in 1992 - Pasto: exactly 2 files (Intro and D-Generación)
    pasto_files = sorted([f.name for f in (dest / "Babasónicos" / "1992 - Pasto").glob("*.ogg")])
    assert len(pasto_files) == 2
    assert "01 - Intro.ogg" in pasto_files
    # Preferred name has accent: D-Generación.ogg
    assert any("D-Generaci" in name for name in pasto_files)



# ---------------------------------------------------------------------------
# 2. Duplicates with different file names
# ---------------------------------------------------------------------------
def test_duplicates_with_different_names(tmp_path: Path) -> None:
    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    dest = tmp_path / "destination"

    content = b"AUDIO_CONTENT_DIFFERENT_NAMES_SAME_BYTES" * 80
    _create_audio_file(src_a / "track01.flac", content)
    _create_audio_file(src_b / "Artist - Song Title (Remastered).flac", content)

    engine = FusionEngine(FusionOptions(sources=[src_a, src_b], destination=dest))
    report = engine.run()

    # One is copied, the second is recognized as duplicate despite the totally different name
    assert report.total_found == 2
    assert report.unique_files == 1
    assert report.files_copied == 1
    assert report.duplicates_detected == 1

    dest_files = list(dest.glob("*.flac"))
    assert len(dest_files) == 1
    assert dest_files[0].read_bytes() == content


# ---------------------------------------------------------------------------
# 3. Files with identical names but different content (conflict resolution)
# ---------------------------------------------------------------------------
def test_same_name_different_content_increments_suffix(tmp_path: Path) -> None:
    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    src_c = tmp_path / "source_c"
    dest = tmp_path / "destination"

    content_a = b"SONG_CONTENT_VERSION_A"
    content_b = b"SONG_CONTENT_VERSION_B"
    content_c = b"SONG_CONTENT_VERSION_C"

    _create_audio_file(src_a / "cancion.mp3", content_a)
    _create_audio_file(src_b / "cancion.mp3", content_b)
    _create_audio_file(src_c / "cancion.mp3", content_c)

    engine = FusionEngine(FusionOptions(sources=[src_a, src_b, src_c], destination=dest))
    report = engine.run()

    # All three files must be kept, 2 conflicts resolved with incremental suffix
    assert report.total_found == 3
    assert report.unique_files == 3
    assert report.files_copied == 3
    assert report.duplicates_detected == 0
    assert report.conflicts_resolved == 2

    dest_files = sorted([f.name for f in dest.glob("*.mp3")])
    assert dest_files == ["cancion (2).mp3", "cancion (3).mp3", "cancion.mp3"]

    # Verify each contains the respective content and none was overwritten
    contents = {f.read_bytes() for f in dest.glob("*.mp3")}
    assert contents == {content_a, content_b, content_c}


# ---------------------------------------------------------------------------
# 4. Files already existing in the destination (idempotency, repeated execution)
# ---------------------------------------------------------------------------
def test_destination_existing_files_prevent_duplicates(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    content1 = b"CONTENT_1_ALREADY_IN_DEST"
    content2 = b"CONTENT_2_NEW"

    # Pre-populate destination with content1
    _create_audio_file(dest / "existing.mp3", content1)

    # Source has content1 (should be skipped) and content2 (should be copied)
    _create_audio_file(src / "copy_of_existing.mp3", content1)
    _create_audio_file(src / "brand_new.mp3", content2)

    engine = FusionEngine(FusionOptions(sources=[src], destination=dest))
    report = engine.run()

    assert report.total_found == 2
    assert report.duplicates_detected == 1  # copy_of_existing skipped!
    assert report.files_copied == 1         # brand_new copied
    assert (dest / "brand_new.mp3").exists()

    # Run the exact same engine execution again: NOTHING should be copied, NO duplicates created!
    engine2 = FusionEngine(FusionOptions(sources=[src], destination=dest))
    report2 = engine2.run()

    assert report2.duplicates_detected == 2
    assert report2.files_copied == 0
    assert len(list(dest.glob("*.mp3"))) == 2  # existing.mp3 and brand_new.mp3 only!


# ---------------------------------------------------------------------------
# 5. Nested / deeply hierarchical directories
# ---------------------------------------------------------------------------
def test_nested_directories_recursion(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    content_a = b"TRACK_SUBDIR_LEVEL_1"
    content_b = b"TRACK_SUBDIR_LEVEL_3"

    _create_audio_file(src / "Artist" / "Album" / "track1.ogg", content_a)
    _create_audio_file(src / "Deep" / "Level1" / "Level2" / "Level3" / "track2.ogg", content_b)

    # With include_subdirectories=True
    engine = FusionEngine(FusionOptions(sources=[src], destination=dest, include_subdirectories=True))
    report = engine.run()

    assert report.total_found == 2
    assert report.files_copied == 2
    assert (dest / "track1.ogg").exists()
    assert (dest / "track2.ogg").exists()


def test_nested_directories_with_preserve_structure(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    content = b"TRACK_STRUCTURE"
    _create_audio_file(src / "Rock" / "Queen" / "01.flac", content)

    engine = FusionEngine(
        FusionOptions(sources=[src], destination=dest, preserve_structure=True)
    )
    report = engine.run()

    assert report.files_copied == 1
    expected_path = dest / "Rock" / "Queen" / "01.flac"
    assert expected_path.exists()
    assert expected_path.read_bytes() == content


# ---------------------------------------------------------------------------
# 6. Empty files (0 bytes)
# ---------------------------------------------------------------------------
def test_empty_files(tmp_path: Path) -> None:
    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    dest = tmp_path / "destination"

    # Two 0-byte audio files
    _create_audio_file(src_a / "empty1.mp3", b"")
    _create_audio_file(src_b / "empty2.mp3", b"")

    engine = FusionEngine(FusionOptions(sources=[src_a, src_b], destination=dest))
    report = engine.run()

    # First empty file is copied, second empty file is recognized as duplicate (same SHA-256)
    assert report.total_found == 2
    assert report.unique_files == 1
    assert report.files_copied == 1
    assert report.duplicates_detected == 1

    dest_files = list(dest.glob("*.mp3"))
    assert len(dest_files) == 1
    assert dest_files[0].stat().st_size == 0


# ---------------------------------------------------------------------------
# 7. Large files (block reading verification)
# ---------------------------------------------------------------------------
def test_large_files_chunked_reading(tmp_path: Path) -> None:
    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    dest = tmp_path / "destination"

    # Create a 4 MiB file (larger than 1 MiB chunk)
    pattern = os.urandom(1024 * 1024)  # 1 MiB random block
    large_data = pattern * 4           # 4 MiB

    _create_audio_file(src_a / "symphony.wav", large_data)
    _create_audio_file(src_b / "symphony_backup.wav", large_data)

    engine = FusionEngine(
        FusionOptions(sources=[src_a, src_b], destination=dest, verify_sha256=True)
    )
    report = engine.run()

    assert report.total_found == 2
    assert report.unique_files == 1
    assert report.files_copied == 1
    assert report.duplicates_detected == 1
    assert report.total_bytes_copied == 4 * 1024 * 1024

    dest_wav = dest / "symphony.wav"
    assert dest_wav.exists()
    assert dest_wav.stat().st_size == 4 * 1024 * 1024
    assert hash_file(dest_wav) == hash_file(src_a / "symphony.wav")


# ---------------------------------------------------------------------------
# 8. Unicode file names (accents, Japanese, emoji, Cyrillic)
# ---------------------------------------------------------------------------
def test_unicode_filenames(tmp_path: Path) -> None:
    src = tmp_path / "source_unicode"
    dest = tmp_path / "destination_unicode"

    files_content = {
        "canción_ñandú_acento_áéíóú.mp3": b"UNICODE_SPANISH",
        "ベートーヴェン_交響曲第9番.flac": b"UNICODE_JAPANESE",
        "Чайковский_Лебединое_озеро.ogg": b"UNICODE_CYRILLIC",
        "🎵_Music_Emoji_🎶.m4a": b"UNICODE_EMOJI",
    }

    for name, content in files_content.items():
        _create_audio_file(src / name, content)

    engine = FusionEngine(FusionOptions(sources=[src], destination=dest))
    report = engine.run()

    assert report.total_found == len(files_content)
    assert report.files_copied == len(files_content)
    assert report.errors_count == 0

    for name, content in files_content.items():
        dest_file = dest / name
        assert dest_file.exists()
        assert dest_file.read_bytes() == content


# ---------------------------------------------------------------------------
# 9. Permission / I/O errors handling
# ---------------------------------------------------------------------------
def test_permission_error_handling(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    good_file = _create_audio_file(src / "good.mp3", b"GOOD_CONTENT")
    unreadable_file = _create_audio_file(src / "unreadable.mp3", b"NO_READ_PERMISSION")

    try:
        # Remove read permissions
        os.chmod(unreadable_file, 0)

        engine = FusionEngine(FusionOptions(sources=[src], destination=dest))
        report = engine.run()

        # good.mp3 copied, unreadable.mp3 recorded as error without aborting
        assert report.files_copied == 1
        assert report.errors_count >= 1
        assert (dest / "good.mp3").exists()
    finally:
        # Restore permissions for cleanup
        os.chmod(unreadable_file, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# 10. Cooperative cancellation during processing
# ---------------------------------------------------------------------------
def test_cancellation_during_processing(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    # Create multiple files
    for i in range(15):
        _create_audio_file(src / f"song_{i:02d}.mp3", f"CONTENT_{i}".encode())

    engine = FusionEngine(FusionOptions(sources=[src], destination=dest))

    def on_progress(p: ProgressData) -> None:
        if p.files_analyzed >= 3:
            engine.cancel()

    report = engine.run(on_progress=on_progress)

    assert engine.is_cancelled
    # Should have stopped early
    assert report.files_copied < 15


# ---------------------------------------------------------------------------
# Reporting (TXT and CSV)
# ---------------------------------------------------------------------------
def test_report_export(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "destination"

    _create_audio_file(src / "a.mp3", b"AAA")
    _create_audio_file(src / "dup_a.mp3", b"AAA")

    engine = FusionEngine(FusionOptions(sources=[src], destination=dest))
    report = engine.run()

    txt_path = tmp_path / "report.txt"
    csv_path = tmp_path / "report.csv"

    report.export_txt(txt_path)
    report.export_csv(csv_path)

    assert txt_path.exists()
    assert csv_path.exists()

    txt_content = txt_path.read_text(encoding="utf-8")
    assert "INFORME DE FUSIÓN MUSICFUSION" in txt_content
    assert "DETALLE DE DUPLICADOS OMITIDOS" in txt_content

    csv_content = csv_path.read_text(encoding="utf-8")
    assert "DUPLICADO_OMITIDO" in csv_content
    assert "COPIADO" in csv_content


# ---------------------------------------------------------------------------
# Acoustic fingerprinting & best bitrate winner selection
# ---------------------------------------------------------------------------
def test_acoustic_fingerprint_best_bitrate_wins(tmp_path: Path) -> None:
    """Validate that two audio files with identical sound but different bitrates
    are recognized as the same song via acoustic fingerprinting (Chromaprint),
    and the file with higher bitrate/quality is kept in the destination.
    """
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available on host system")

    src_a = tmp_path / "source_a"
    src_b = tmp_path / "source_b"
    dest = tmp_path / "destination"
    src_a.mkdir(parents=True)
    src_b.mkdir(parents=True)

    low_mp3 = src_a / "song_low.mp3"
    high_mp3 = src_b / "song_high.mp3"

    # Generate 4 seconds of identical musical tone (440Hz sine wave)
    # Low quality: 64k MP3
    cmd_low = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-ac", "2", "-b:a", "64k", str(low_mp3)
    ]
    # High quality: 320k MP3
    cmd_high = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-ac", "2", "-b:a", "320k", str(high_mp3)
    ]
    subprocess.run(cmd_low, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(cmd_high, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    engine = FusionEngine(
        FusionOptions(
            sources=[src_a, src_b],
            destination=dest,
            acoustic_detection=True,
        )
    )
    report = engine.run()

    # Total 2 files found: exactly 1 unique in destination, 1 duplicate detected
    assert report.total_found == 2
    assert report.duplicates_detected == 1
    assert report.files_copied == 1

    dest_files = list(dest.glob("*.mp3"))
    assert len(dest_files) == 1
    # The file in destination should be the higher bitrate version (larger file size)
    assert dest_files[0].stat().st_size > low_mp3.stat().st_size
    assert dest_files[0].stat().st_size == high_mp3.stat().st_size


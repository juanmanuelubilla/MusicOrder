"""Tests for the acoustic fingerprint module (Phase: huella acústica)."""

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


def _backend_available() -> bool:
    from musicfusion.audio.fingerprint import fingerprint_available

    return fingerprint_available()


def _write_sine(path: Path, frequency: int = 440, duration: float = 2.0) -> None:
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
            "flac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _write_noise(path: Path, duration: float = 2.0) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=white:duration={duration}",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(
    not _av_available() or not _backend_available(),
    reason="ffmpeg and a fingerprint backend required",
)
def test_backend_reports_available() -> None:
    from musicfusion.audio.fingerprint import backend_name, fingerprint_available

    assert fingerprint_available()
    assert backend_name() in {"chromaprint", "fpcalc"}


@pytest.mark.skipif(
    not _av_available() or not _backend_available(),
    reason="ffmpeg and a fingerprint backend required",
)
def test_same_content_yields_same_fingerprint(tmp_path: Path) -> None:
    from musicfusion.audio.fingerprint import fingerprint_file

    source = tmp_path / "src.flac"
    _write_sine(source, duration=6.0)
    copy_path = tmp_path / "copy.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-c:a", "flac", str(copy_path)],
        check=True,
        capture_output=True,
    )

    first = fingerprint_file(str(source))
    second = fingerprint_file(str(copy_path))
    third = fingerprint_file(str(source))
    assert first is not None and second is not None and third is not None
    assert first.fingerprint
    assert first.fingerprint == third.fingerprint
    assert first.fingerprint == second.fingerprint


@pytest.mark.skipif(
    not _av_available() or not _backend_available(),
    reason="ffmpeg and a fingerprint backend required",
)
def test_different_content_yields_different_fingerprint(tmp_path: Path) -> None:
    from musicfusion.audio.fingerprint import fingerprint_file

    a = tmp_path / "a.flac"
    b = tmp_path / "b.flac"
    _write_sine(a, frequency=440, duration=6.0)
    _write_noise(b, duration=6.0)

    fp_a = fingerprint_file(str(a))
    fp_b = fingerprint_file(str(b))
    assert fp_a is not None and fp_b is not None
    assert fp_a.fingerprint != fp_b.fingerprint


def test_no_file_returns_none() -> None:
    from musicfusion.audio.fingerprint import fingerprint_file

    assert fingerprint_file("/definitely/not/here.flac") is None

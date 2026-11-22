"""Tests for cover art helpers (bytes -> candidate, embed round-trip)."""

from __future__ import annotations

import base64

from musicfusion.audio.artwork import candidate_from_bytes

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


def test_candidate_from_bytes_detects_png_dimensions_and_mime() -> None:
    candidate = candidate_from_bytes(PNG_1X1, source="test")
    assert candidate.width == 1
    assert candidate.height == 1
    assert candidate.mime == "image/png"
    assert candidate.data == PNG_1X1
    assert candidate.source == "test"
    assert candidate.embedded is False


def test_candidate_from_bytes_detects_jpeg_mime() -> None:
    candidate = candidate_from_bytes(JPEG_HEADER + b"\x00" * 32, source="album.jpg")
    assert candidate.mime == "image/jpeg"

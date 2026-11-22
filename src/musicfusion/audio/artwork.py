"""Cover art discovery, selection and embedding.

Detects embedded art (Mutagen) and sidecar files (cover.jpg, folder.jpg,
front.jpg, album.jpg) and picks the highest-resolution candidate using PIL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.id3 import APIC, ID3FileType
from mutagen.mp4 import MP4, MP4Cover

logger = logging.getLogger(__name__)

SIDECAR_PRIORITY = (
    ("cover", ("jpg", "png", "jpeg", "webp")),
    ("folder", ("jpg", "png", "jpeg")),
    ("front", ("jpg", "png", "jpeg")),
    ("album", ("jpg", "png", "jpeg")),
    ("albumart", ("jpg", "png")),
    ("front", ("gif",)),
)

MAX_ART_BYTES = 20 * 1024 * 1024


@dataclass
class ArtCandidate:
    """A discovered artwork candidate."""

    data: bytes
    source: str = ""
    embedded: bool = False
    width: int = 0
    height: int = 0
    mime: str = ""

    @property
    def score(self) -> float:
        """A 0..100 score preferring large, non-embedded images."""
        if self.width <= 0 or self.height <= 0:
            return 0.0
        import math

        pixels = math.log(1 + self.width * self.height)
        return round((80.0 * pixels / math.log(1 + 3000 * 3000)
                      + (10.0 if not self.embedded else 0.0)), 1)


def _probe(data: bytes) -> tuple[int, int]:
    """Return (width, height) for a JPEG/PNG/WebP image, (0, 0) on failure."""
    try:
        from PIL import Image

        with Image.open(__import__("io").BytesIO(data)) as img:
            return img.size
    except Exception:
        return 0, 0


def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/unknown"


def _extract_embedded(path: Path) -> ArtCandidate | None:
    """Extract the first embedded picture from an audio file."""
    try:
        audio = mutagen.File(str(path))
        if audio is None:
            return None
        pictures: list[bytes] = []
        if hasattr(audio, "pictures"):
            pictures = [p.data for p in audio.pictures if getattr(p, "data", None)]
        elif isinstance(audio, ID3FileType) and audio.tags:
            pictures = [f.data for f in audio.tags.values()
                        if getattr(f, "FrameID", "") == "APIC"]
        for data in pictures:
            data = bytes(data)
            if not data or len(data) > MAX_ART_BYTES:
                continue
            width, height = _probe(data)
            if width and height:
                return ArtCandidate(data=data, source=str(path), embedded=True,
                                    width=width, height=height, mime=_mime(data))
    except Exception as exc:
        logger.debug("Embedded art extraction failed for %s: %s", path, exc)
    return None


def _find_sidecar(dir_path: Path) -> Path | None:
    """Return the best sidecar art file inside dir_path."""
    best: Path | None = None
    best_pixels = 0
    for name in list(dict.fromkeys(name for name, _ in SIDECAR_PRIORITY)):
        for ext in ("jpg", "jpeg", "png", "webp"):
            candidate = dir_path / f"{name}.{ext}"
            if not candidate.is_file():
                continue
            try:
                size = candidate.stat().st_size
                if not 100 < size < MAX_ART_BYTES:
                    continue
                width, height = _probe(candidate.read_bytes())
            except OSError:
                continue
            pixels = width * height
            if pixels > best_pixels:
                best = candidate
                best_pixels = pixels
    return best


def candidate_from_bytes(data: bytes, source: str = "", embedded: bool = False) -> ArtCandidate:
    """Build an :class:`ArtCandidate` from raw image bytes (probes + detects mime)."""
    width, height = _probe(data)
    return ArtCandidate(data=data, source=source, embedded=embedded,
                        width=width, height=height, mime=_mime(data))


def discover_art(track_path: Path) -> ArtCandidate | None:
    """Discover the best available artwork for a track file.

    Embedded pictures are preferred, then sidecar files in the same folder.
    """
    embedded = _extract_embedded(track_path)
    if embedded:
        return embedded
    sidecar = _find_sidecar(track_path.parent)
    if sidecar:
        try:
            data = sidecar.read_bytes()
            width, height = _probe(data)
            return ArtCandidate(data=data, source=str(sidecar), width=width,
                                height=height, mime=_mime(data))
        except OSError:
            logger.debug("Cannot read sidecar art %s", sidecar)
    return None


def embed_art(track_path: Path, art: ArtCandidate) -> bool:
    """Embed ``art`` into the audio file, preserving all other tags."""
    try:
        audio = mutagen.File(str(track_path))
        if audio is None:
            return False
        from mutagen.flac import Picture as FlacPicture

        if hasattr(audio, "add_picture"):
            picture = FlacPicture()
            picture.type = 3
            picture.mime = art.mime
            picture.desc = "front cover"
            picture.data = art.data
            audio.add_picture(picture)
        elif isinstance(audio, ID3FileType):
            if audio.tags is None:
                audio.add_tags()
            assert audio.tags is not None
            audio.tags.add(APIC(encoding=3, mime=art.mime, type=3,
                                desc="front cover", data=art.data))
        elif isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            assert audio.tags is not None
            cover_format = MP4Cover.FORMAT_JPEG if art.mime == "image/jpeg" else MP4Cover.FORMAT_PNG
            audio.tags["covr"] = [MP4Cover(art.data, imageformat=cover_format)]
        audio.save()
        return True
    except Exception as exc:
        logger.warning("Embed art failed for %s: %s", track_path, exc)
        return False


__all__ = ["ArtCandidate", "candidate_from_bytes", "discover_art", "embed_art", "_find_sidecar"]

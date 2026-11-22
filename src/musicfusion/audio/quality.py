"""Audio quality probing using ffprobe.

`ffprobe` provides authoritative technical information: container, codec,
bitrate, sample rate, bit depth and channel layout. Results are normalized
into a :class:`AudioQuality` object and a 0..100 quality score.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Multiply chain: FLAC/PCM > lossless codecs > high-bitrate MP3/AAC/Opus.
_LOSSLESS_CODECS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "ape", "wavpack"}


@dataclass
class AudioQuality:
    """Technical descriptors of an audio stream."""

    container: str = ""
    codec: str = ""
    bitrate_kbps: int | None = None
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    channel_layout: str = ""
    duration_ms: int = 0
    lossless: bool = False
    has_stream: bool = False
    tags: dict[str, str] = field(default_factory=dict)

    def score(self) -> float:
        """Compute a 0..100 audio-quality score.

        Lossless content scores highly; lossy codecs score by effective
        compressed bitrate. Files that fail to probe score 0.
        """
        if not self.has_stream:
            return 0.0
        base = 40.0
        codec = (self.codec or "").lower()
        if codec in _LOSSLESS_CODECS or self.lossless:
            base += 55.0
            if self.sample_rate_hz and self.sample_rate_hz >= 96000:
                base += 5.0
            elif self.sample_rate_hz and self.sample_rate_hz >= 44100:
                base += 4.0
        else:
            rate = self.bitrate_kbps or 128
            if rate >= 320:
                base += 52.0
            elif rate >= 256:
                base += 45.0
            elif rate >= 192:
                base += 40.0
            elif rate >= 128:
                base += 32.0
            elif rate >= 96:
                base += 24.0
            else:
                base += 12.0
        return round(min(100.0, base), 1)


def find_ffprobe() -> str:
    """Return the path of ffprobe or raise FileNotFoundError."""
    probe = shutil.which("ffprobe")
    if not probe:
        raise FileNotFoundError(
            "ffprobe not found. Install FFmpeg (apt/dnf/pacman/zypper install ffmpeg)."
        )
    return probe


def probe_quality(path: str | Path) -> AudioQuality | None:
    """Probe an audio file and return its quality descriptor.

    Returns ``None`` when ffprobe is unavailable or the stream is empty.
    """
    file_path = Path(path)
    try:
        probe = find_ffprobe()
    except FileNotFoundError:
        logger.warning("ffprobe unavailable; skipping quality probe for %s", file_path)
        return None

    cmd = [
        probe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode != 0:
            logger.debug("ffprobe failed for %s: %s", file_path, result.stderr.strip())
            return None
        payload = json.loads(result.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.debug("ffprobe error for %s: %s", file_path, exc)
        return None

    audio_stream = None
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break
    if not audio_stream:
        return None

    fmt = payload.get("format", {}) or {}
    quality = AudioQuality()
    quality.container = str(fmt.get("format_name") or "")
    quality.codec = audio_stream.get("codec_name", "")
    quality.sample_rate_hz = _int(audio_stream.get("sample_rate"))
    quality.channels = _int(audio_stream.get("channels"))
    quality.channel_layout = audio_stream.get("channel_layout", "")
    quality.bit_depth = _int(audio_stream.get("bits_per_sample"))
    quality.duration_ms = int(float(audio_stream.get("duration") or fmt.get("duration") or 0) * 1000)

    bit_rate = _int(audio_stream.get("bit_rate"))
    if not bit_rate:
        bit_rate = _int(fmt.get("bit_rate"))
    if bit_rate:
        quality.bitrate_kbps = bit_rate // 1000

    format_tags = fmt.get("tags", {}) or {}
    if isinstance(format_tags, dict):
        quality.tags = {str(k): str(v) for k, v in format_tags.items()}

    code = quality.codec.lower()
    quality.lossless = code in _LOSSLESS_CODECS or quality.bit_depth in (16, 24, 32)
    quality.has_stream = True
    return quality


def _int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None

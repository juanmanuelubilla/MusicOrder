"""Acoustic fingerprinting with pluggable backends (chromaprint).

Backends, in order of preference:

* ``chromaprint`` — Python bindings from the ``pyacoustid`` package using the
  system ``libchromaprint`` (classic fpcalc algorithm). Decodes audio through
  ffmpeg to raw mono PCM and feeds it to the fingerprinter.
* ``fpcalc`` — the standalone ``fpcalc`` binary (``libchromaprint-tools``),
  invoked with ``-json``.

When neither backend nor ffmpeg is available the module reports ``none`` and
every call returns ``None``; the rest of the application degrades gracefully
(matching and content grouping simply skip the acoustic signal).

Generated fingerprints are stable: the same audio content produces the same
encoded string regardless of the container/codec it is stored in, which is what
the matching engine compares for exact equality.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_FPCALC = "fpcalc"
_FFMPEG = "ffmpeg"
_ALGORITHM = 2  # chromaprint algorithm used for encoding (matches fpcalc)
_RATE = 11025
_CHANNELS = 1

_backend: str | None = None


@dataclass(frozen=True)
class FingerprintResult:
    """A single acoustic fingerprint fingerprint value."""

    fingerprint: str
    duration_seconds: float
    backend: str = "chromaprint"


def backend_name() -> str:
    """Return the active backend: ``chromaprint``, ``fpcalc`` or ``none``."""
    global _backend
    if _backend is None:
        try:
            from acoustid import chromaprint  # noqa: F401

            _backend = "chromaprint"
        except ImportError:
            _backend = "fpcalc" if shutil.which(_FPCALC) else "none"
    return _backend


def fingerprint_available() -> bool:
    """Return True when an acoustic fingerprint backend is usable."""
    return backend_name() != "none"


def fingerprint_file(
    path: str,
    *,
    max_duration: float = 120.0,
) -> FingerprintResult | None:
    """Compute a chromaprint fingerprint for ``path``.

    Returns ``None`` when no backend is available or the file cannot be
    decoded. May raise for unexpected errors, but generally swallows them and
    returns ``None`` (a missing fingerprint must never break a scan).
    """
    backend = backend_name()
    if backend == "none":
        return None
    try:
        if backend == "fpcalc":
            return _fpcalc_fingerprint(path)
        return _chromaprint_fingerprint(path, max_duration=max_duration)
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the run
        logger.warning("fingerprint failed for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# chromaprint backend (pyacoustid + ffmpeg decode)
# ---------------------------------------------------------------------------


def _decode_raw(path: str, *, max_duration: float) -> bytes:
    """Decode audio to raw mono S16LE PCM at 11025 Hz via ffmpeg."""
    ffmpeg = shutil.which(_FFMPEG)
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; required for chromaprint backend")
    cmd = [ffmpeg, "-v", "error", "-i", path]
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd += ["-f", "s16le", "-ac", str(_CHANNELS), "-ar", str(_RATE), "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = proc.stdout
    if out is None:
        proc.wait()
        raise RuntimeError("cannot capture ffmpeg output")
    try:
        data = out.read()
    finally:
        if proc.poll() is None:
            proc.kill()
        out.close()
        proc.wait()
    if not data:
        raise RuntimeError("ffmpeg produced no audio output")
    return data


def _chromaprint_fingerprint(path: str, *, max_duration: float) -> FingerprintResult:
    from acoustid import chromaprint

    raw = _decode_raw(path, max_duration=max_duration)
    fingerprinter = chromaprint.Fingerprinter()
    fingerprinter.start(_RATE, _CHANNELS)
    for offset in range(0, len(raw), 1 << 20):
        fingerprinter.feed(raw[offset : offset + (1 << 20)])
    bits = fingerprinter.finish()
    if not bits:
        raise RuntimeError("fingerprint is empty (too short?)")
    encoded = chromaprint.encode_fingerprint(bits, algorithm=_ALGORITHM)
    return FingerprintResult(
        fingerprint=encoded.decode("ascii"),
        duration_seconds=round(len(raw) / (_RATE * _CHANNELS * 2), 2),
        backend="chromaprint",
    )


# ---------------------------------------------------------------------------
# fpcalc backend
# ---------------------------------------------------------------------------


def _fpcalc_fingerprint(path: str) -> FingerprintResult:
    fpcalc = shutil.which(_FPCALC)
    if not fpcalc:
        raise RuntimeError("fpcalc binary not found")
    proc = subprocess.run([fpcalc, "-json", str(path)], capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("fpcalc failed to fingerprint the file")
    payload = json.loads(proc.stdout)
    return FingerprintResult(
        fingerprint=payload.get("fingerprint", ""),
        duration_seconds=float(payload.get("duration", 0.0) or 0.0),
        backend="fpcalc",
    )


__all__ = [
    "FingerprintResult",
    "backend_name",
    "fingerprint_available",
    "fingerprint_file",
]

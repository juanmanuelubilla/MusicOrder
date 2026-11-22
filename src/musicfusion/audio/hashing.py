"""File hashing utilities (SHA-256 and quick metadata-independent hashes)."""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB

ALGORITHMS = ("sha256", "md5")


def hash_file(path: str | Path, algorithm: str = "sha256") -> str | None:
    """Return the hex digest of a file, reading it in chunks.

    Returns ``None`` if the file cannot be read.
    """
    hasher = hashlib.new(algorithm)
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


def hash_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """Return the hex digest of an in-memory byte string."""
    return hashlib.new(algorithm, data).hexdigest()


def quick_hash(path: str | Path, sample_bytes: int = 65536) -> str | None:
    """Return a cheap hash of only the first ``sample_bytes`` of a file.

    Useful for fast pre-filtering before a full SHA-256 pass.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(sample_bytes)
        return hashlib.md5(head).hexdigest()
    except OSError:
        return None


def compute_hashes(path: str | Path, algorithms: tuple[str, ...] = ("sha256",)) -> dict[str, str]:
    """Compute the requested hash digests for a file in a single pass."""
    digests = {algorithm: hashlib.new(algorithm) for algorithm in algorithms}
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                for hasher in digests.values():
                    hasher.update(chunk)
    except OSError:
        return {}
    return {name: hasher.hexdigest() for name, hasher in digests.items()}

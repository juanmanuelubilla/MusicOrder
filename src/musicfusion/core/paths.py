"""Filesystem helpers: user directories and safe path handling."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Characters considered problematic in file names across Linux filesystems.
_INVALID = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MULTI_SPACE = re.compile(r"[ \t]+")


def user_config_dir(app: str = "musicfusion") -> Path:
    """Return the XDG config directory for the application."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = str(Path.home() / ".config")
    return Path(base) / app


def user_data_dir(app: str = "musicfusion") -> Path:
    """Return the XDG data directory for the application."""
    base = os.environ.get("XDG_DATA_HOME")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / app


def user_cache_dir(app: str = "musicfusion") -> Path:
    """Return the XDG cache directory for the application."""
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / app


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """Return a filesystem-safe version of ``name``.

    Args:
        name: string to sanitize.
        fallback: name used when the result is empty or ``.``-only.

    Returns:
        A safe file name without any path separator.
    """
    cleaned = _INVALID.sub("_", name)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip(" .")
    return cleaned or fallback


def unique_path(target: Path) -> Path:
    """Return ``target`` or, if it exists, a numbered variant (name (2).ext)."""
    if not target.exists():
        return target
    parent = target.parent
    stem, suffix = target.stem, target.suffix
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_within(child: Path, parent: Path) -> bool:
    """Check whether ``child`` is strictly inside the directory ``parent``."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False

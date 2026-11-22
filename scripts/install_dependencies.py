#!/usr/bin/env python3
"""Install MusicFusion Pro dependencies on major Linux distributions.

Creates or reuses a virtualenv inside the project, then installs the runtime
and development dependencies via pip. System packages (ffmpeg, portaudio,
basic media codecs, Qt multimedia backends) are installed with the native
package manager when possible:

  * Debian/Ubuntu/Raspbian  -> apt
  * Fedora/RHEL             -> dnf
  * Arch/Manjaro            -> pacman
  * openSUSE                -> zypper

Run from the project root::

    python3 scripts/install_dependencies.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
VENV = PROJECT / ".venv"
PIP = VENV / "bin" / "pip"
PYTHON = VENV / "bin" / "python"

DEBIAN_PACKAGES = [
    "ffmpeg",
    "libchromaprint1",
    "libchromaprint-tools",
    "libasound2-plugins",
    "libportaudio2",
    "libgl1",
    "libegl1",
    "libgstreamer1.0-0",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "libpulse0",
]
FEDORA_PACKAGES = [
    "ffmpeg",  # from rpmfusion
    "python3",
    "alsa-lib",
    "pulseaudio-libs",
    "gstreamer1-plugins-base",
    "gstreamer1-plugins-good",
    "gstreamer1-plugins-bad-free",
]
ARCH_PACKAGES = [
    "ffmpeg",
    "portaudio",
    "pipewire",
    "pulseaudio",
]
ZYPPER_PACKAGES = [
    "ffmpeg",
    "pulseaudio",
    "gstreamer-plugins-base",
    "gstreamer-plugins-good",
    "alsa-plugins",
]


def _run(command: list[str], *, check: bool = True) -> None:
    """Execute a command, streaming output through the terminal."""
    print("  $", " ".join(command))
    subprocess.run(command, check=check)


def detect_manager() -> str | None:
    """Return the package manager available on this system."""
    for manager in ("apt", "dnf", "pacman", "zypper"):
        if (Path("/usr/bin") / manager).exists() or _which(manager):
            return manager
    return None


def _which(binary: str) -> bool:
    try:
        subprocess.run([binary, "--version"], capture_output=True, check=False)
        return True
    except OSError:
        return False


def manager_packages(manager: str) -> list[str]:
    """Return the system packages for the detected manager."""
    return {
        "apt": DEBIAN_PACKAGES,
        "dnf": FEDORA_PACKAGES,
        "pacman": ARCH_PACKAGES,
        "zypper": ZYPPER_PACKAGES,
    }.get(manager, [])


def install_system(manager: str, packages: list[str]) -> None:
    """Install system packages via the detected package manager."""
    if not packages:
        return
    if manager == "apt":
        _run(["sudo", "apt-get", "update"], check=False)
        _run(["sudo", "apt-get", "install", "-y", *packages])
    elif manager == "dnf":
        _run(["sudo", "dnf", "install", "-y", *packages])
    elif manager == "pacman":
        _run(["sudo", "pacman", "-Sy", "--noconfirm", *packages])
    elif manager == "zypper":
        _run(["sudo", "zypper", "install", "-y", *packages])


def ensure_venv() -> None:
    """Create the project virtualenv if missing and upgrade tooling."""
    if not VENV.exists():
        _run([sys.executable, "-m", "venv", str(VENV)])
    _run([str(PIP), "install", "--upgrade", "pip", "wheel"])


def install_python() -> None:
    """Install the project (editable) with development extras."""
    _run([str(PIP), "install", "-e", ".[test]"])


def patch_qt_plugins() -> None:
    """Hint Qt Media to use the system media backend on some distros."""
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")


def main() -> int:
    print("== MusicFusion Pro - dependency installer ==")

    manager = detect_manager()
    packages = manager_packages(manager) if manager else []
    print(f"Distro package manager: {manager}")

    if manager and packages:
        print("Installing system packages…")
        install_system(manager, packages)
    else:
        print("No supported package manager detected; only Python deps will be installed.")

    print("Setting up virtual environment…")
    ensure_venv()
    block = input("Install Python dependencies now? [y/N] ").strip().lower()
    if block in ("y", "yes"):
        install_python()
    else:
        print("Skip. Run .venv/bin/pip install -e '.[dev]' manually.")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

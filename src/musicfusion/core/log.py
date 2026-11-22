"""Logging setup: rotating console + file logger."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Configure the root logger with a console handler and a rotating file handler.

    Args:
        log_dir: directory where log files will be written.
        level: minimum logging level.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile
        log_dir = Path(tempfile.gettempdir()) / "musicfusion-logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(_FORMAT, _DATE)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "musicfusion.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

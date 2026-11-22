"""GUI bootstrap and main function."""

from __future__ import annotations

import logging

from musicfusion.config.settings import Settings
from musicfusion.core.app import build_context

logger = logging.getLogger(__name__)

_active: dict[str, object] = {"palette": None}


def active_palette() -> object:
    """Return the currently selected theme palette (used by widgets)."""
    from musicfusion.ui.theme import PALETTES

    palette = _active["palette"]
    return palette if palette is not None else PALETTES["dark"]


def run(settings: Settings) -> int:
    """Launch the Qt application with the main window, returning the exit code."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from musicfusion.ui.main_window import MainWindow
    from musicfusion.ui.theme import PALETTES

    qapp = QApplication.instance() or QApplication([])
    qapp.setApplicationName("MusicFusion Pro")
    qapp.setOrganizationName("MusicFusion")

    context = build_context(settings)
    window = MainWindow(context)
    theme = settings.get("app.theme", "dark")
    _active["palette"] = PALETTES.get("light" if theme == "light" else "dark", PALETTES["dark"])
    window.apply_theme()
    window.show()

    QTimer.singleShot(0, _log_startup)
    return qapp.exec()


def _log_startup() -> None:
    logger.info("GUI initialized")

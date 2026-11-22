"""Plugin system for MusicFusion Pro."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """Declarative metadata for a plugin."""

    id: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    hooks: list[str] = field(default_factory=list)


class Plugin:
    """Base class every MusicFusion plugin extends."""

    info: PluginInfo | None = None

    def __init__(self, context: dict[str, Any] | None = None) -> None:
        self.context: dict[str, Any] = context or {}
        self.enabled: bool = True

    def enable(self) -> None:
        """Called when the plugin is activated."""
        self.enabled = True

    def disable(self) -> None:
        """Called when the plugin is disabled."""
        self.enabled = False

    def setup(self) -> None:
        """Optional one-time initialization at startup."""

    def teardown(self) -> None:
        """Optional cleanup when the application shuts down."""

    def on_track_scanned(self, track: Any) -> None:
        """Hook: a track was just indexed."""

    def on_track_merged(self, source: Any, target: Any) -> None:
        """Hook: a source track was merged into a target track."""

    def on_job_finished(self, job: Any) -> None:
        """Hook: a background job completed."""


def _plugin_info(cls: type) -> PluginInfo | None:
    return getattr(cls, "info", None)


def load_plugin(package: str) -> Plugin | None:
    """Import a plugin by dotted module path and instantiate its Plugin subclass."""
    try:
        module = importlib.import_module(package)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to import plugin %s: %s", package, exc)
        return None
    plugin_cls: type[Plugin] | None = None
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
            plugin_cls = attr
            break
    if plugin_cls is None:
        logger.warning("No Plugin subclass found in %s", package)
        return None
    return plugin_cls()

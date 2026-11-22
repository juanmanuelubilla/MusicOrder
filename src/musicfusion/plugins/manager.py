"""Plugin registry and loader for MusicFusion Pro."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from musicfusion.plugins.base import Plugin, load_plugin

logger = logging.getLogger(__name__)


@dataclass
class PluginRegistry:
    """Discovers, loads and manages plugins listed in configuration."""

    package_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.plugins: list[Plugin] = []
        self._by_id: dict[str, Plugin] = {}

    def load_all(self) -> int:
        """Instantiate every configured plugin; return count loaded."""
        for name in self.package_names:
            plugin = load_plugin(name)
            if plugin is not None:
                try:
                    plugin.setup()
                except Exception as exc:  # noqa: BLE001
                    logger.error("setup() failed for %s: %s", name, exc)
                self.plugins.append(plugin)
                self._by_id[getattr(plugin.info, "id", name)] = plugin
                logger.info("Loaded plugin %s", name)
        return len(self.plugins)

    def disable(self, plugin_id: str) -> None:
        plugin = self._by_id.get(plugin_id)
        if plugin:
            plugin.disable()

    def enable(self, plugin_id: str) -> None:
        plugin = self._by_id.get(plugin_id)
        if plugin:
            plugin.enable()

    def emit(self, hook: str, *args, **kwargs) -> None:
        """Dispatch an event to every enabled plugin implementing the hook."""
        for plugin in self.plugins:
            if not plugin.enabled:
                continue
            handler = getattr(plugin, hook, None)
            if handler is None:
                continue
            try:
                handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.error("Plugin %s hook %s failed: %s", type(plugin).__name__, hook, exc)

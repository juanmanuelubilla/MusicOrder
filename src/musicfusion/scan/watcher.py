# mypy: disable-error-code="attr-defined,arg-type"
"""Real-time filesystem monitoring using watchdog.

The watcher observes the configured library roots and reports new/modified/
removed audio files back to a callback. The indexer can re-index incrementally
without a full walk. watchdog ships no type stubs, hence the module pragma.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WATCHDOG_AVAILABLE = False

    class FileSystemEventHandler:  # type: ignore[no-redef]
        """Fallback stub used when watchdog is not installed."""

    Observer = object  # type: ignore[assignment,misc]

EventCallback = Callable[[str, str], None]  # (kind, path); kind in new/modified/deleted


class _AudioHandler(FileSystemEventHandler):
    """Watchdog handler filtering events to supported audio extensions."""

    def __init__(self, extensions: set[str], callback: EventCallback) -> None:
        self.extensions = extensions
        self.callback = callback

    def _relevant(self, path: str) -> bool:
        import os

        name = os.path.basename(path)
        return name.lower().endswith(tuple(self.extensions))

    def on_created(self, event: object) -> None:
        if not getattr(event, "is_directory", True) and self._relevant(event.src_path):
            self.callback("new", event.src_path)

    def on_modified(self, event: object) -> None:
        if not getattr(event, "is_directory", True) and self._relevant(event.src_path):
            self.callback("modified", event.src_path)

    def on_deleted(self, event: object) -> None:
        if not getattr(event, "is_directory", True) and self._relevant(event.src_path):
            self.callback("deleted", event.src_path)


class LibraryWatcher:
    """Observes library roots in background threads."""

    def __init__(self, extensions: set[str], callback: EventCallback, interval: float = 1.2) -> None:
        self.extensions = extensions
        self.callback = callback
        self.interval = interval
        self._observers: list[object] = []
        self._paths: set[str] = set()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """True when watchdog is importable."""
        return _WATCHDOG_AVAILABLE

    def start(self, root_paths: list[str]) -> None:
        """Start watching every path in ``root_paths``."""
        if not self.available:
            logger.warning("watchdog unavailable; monitoring disabled")
            return
        for root in root_paths:
            self.add_path(root)

    def add_path(self, root: str) -> None:
        """Add one root to the set of observed directories."""
        with self._lock:
            if root in self._paths:
                return
            observer = Observer(timeout=self.interval)
            handler = _AudioHandler(self.extensions, self.callback)
            observer.schedule(handler, root, recursive=True)
            observer.daemon = True
            observer.start()
            self._observers.append(observer)
            self._paths.add(root)
            logger.info("Watching library root: %s", root)

    def stop(self) -> None:
        """Stop all observers."""
        for observer in self._observers:
            observer.stop()
        for observer in self._observers:
            observer.join(timeout=2)
        self._observers.clear()

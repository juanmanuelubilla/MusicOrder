"""QThread-based worker that runs blocking jobs off the GUI thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


class JobWorker(QObject):
    """Runs a callable in a background thread and reports progress."""

    started = Signal()
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False

    @Slot()
    def run(self) -> None:
        """Execute the wrapped callable inside the worker thread."""
        self.started.emit()
        try:
            if self._fn and self._cancelled:
                raise RuntimeError("Cancelled")
            kwargs = dict(self._kwargs)
            if "on_progress" in _PARAM_NAMES(self._fn):
                kwargs["on_progress"] = self._report_progress
            result = self._fn(*self._args, **kwargs)
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Background job failed")
            self.failed.emit(str(exc))

    def _report_progress(self, step: int, total: int, message: str) -> None:
        self.progress.emit(step, total, message)

    def cancel(self) -> None:
        """Request a cooperative cancel (checked before start)."""
        self._cancelled = True


def _PARAM_NAMES(fn: Callable[..., Any]) -> set[str]:
    import inspect

    try:
        return set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return set()


class JobThread(QThread):
    """Owns the thread and the worker object, cleaning up on completion."""

    def __init__(self, worker: JobWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.worker = worker
        self.worker.moveToThread(self)
        self.started.connect(self.worker.run)

    def start_job(self) -> None:
        """Start the thread."""
        self.start()

    def stop(self) -> None:
        """Cancel the running job and wait for the thread to finish."""
        self.worker.cancel()
        self.quit()
        self.wait(3000)

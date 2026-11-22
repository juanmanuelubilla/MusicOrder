"""Shared helpers for running background jobs from the UI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from musicfusion.core.app import AppContext
from musicfusion.worker.qt import JobThread, JobWorker


class JobRunner(QObject):
    """Runs blocking operations in a worker thread and relays signals."""

    started = Signal()
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: JobThread | None = None

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Start ``fn`` in the background, forwarding progress to the UI."""
        worker = JobWorker(fn, *args, **kwargs)
        thread = JobThread(worker)
        self._thread = thread
        worker.progress.connect(self.progress)
        worker.finished.connect(self.finished)
        worker.failed.connect(self.failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def cancel(self) -> None:
        if self._thread is not None:
            self._thread.stop()


class JobProgress:
    """Ties a JobRunner to a ProgressRow so pages stay concise."""

    def __init__(self, parent: QObject, context: AppContext) -> None:
        self.context = context
        self.runner = JobRunner(parent)
        self.finished = self.runner.finished
        self.failed = self.runner.failed
        self.progress = self.runner.progress

    def _bind(self, on_progress) -> None:
        if on_progress is not None:
            self.runner.progress.connect(on_progress)

    def run_indexer(self, *, full: bool = False, on_progress=None) -> None:
        from musicfusion.scan.indexer import LibraryIndexer

        indexer = LibraryIndexer(self.context.settings, self.context.db)
        self._bind(on_progress)
        self.runner.run(indexer.run, full=full)

    def run_duplicates(self, *, on_progress=None, paths: list[Path] | None = None) -> None:
        from musicfusion.duplicates.detect import DuplicateDetector

        detector = DuplicateDetector(self.context.settings, self.context.db)
        self._bind(on_progress)
        self.runner.run(detector.run, paths=paths)

    def run_fusion(
        self,
        lib_a,
        lib_b,
        dest,
        *,
        dry_run: bool = False,
        on_progress=None,
        plan=None,
    ) -> None:
        from musicfusion.merge.executor import FusionExecutor
        from musicfusion.merge.planner import FusionPlanner

        if plan is None:
            plan = FusionPlanner(self.context.settings, self.context.db).plan(lib_a, lib_b, dest)
        executor = FusionExecutor(self.context.db)
        self._bind(on_progress)
        self.runner.run(executor.execute, plan, dry_run=dry_run)

    def run_reports(self, *, on_progress=None) -> None:
        from musicfusion.reports.exporter import export_all

        self._bind(on_progress)
        self.runner.run(export_all, self.context.settings)

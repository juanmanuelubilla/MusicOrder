"""Asynchronous job queue.

A :class:`JobQueue` accepts callable jobs, runs them on a thread pool and
persists progress into the ``jobs`` table. The GUI can subscribe to progress
events without blocking the event loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any

from sqlalchemy import select

from musicfusion.db.models import Job
from musicfusion.db.session import Database

logger = logging.getLogger(__name__)

JobFn = Callable[..., Any]
Listener = Callable[[int, float, str], None]  # (job_id, progress, message)


class JobQueue:
    """Queue + thread pool that tracks jobs in the database."""

    def __init__(self, db: Database, max_workers: int = 2) -> None:
        self.db = db
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mf-job")
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: Listener) -> None:
        """Register a progress listener invoked from the worker thread."""
        self._listeners.append(listener)

    def _emit(self, job_id: int, progress: float, message: str) -> None:
        for listener in self._listeners:
            try:
                listener(job_id, progress, message)
            except Exception:  # pragma: no cover
                logger.exception("Job listener error")

    def submit(self, kind: str, fn: JobFn, *args: Any, **kwargs: Any) -> int:
        """Enqueue a callable and return the new job id."""
        with self.db.session_scope() as session:
            job = Job(kind=kind, status="queued", params={"args": [], "kwargs": {}})
            session.add(job)
            session.flush()
            job_id = int(job.id)

        future = self.pool.submit(self._run, job_id, fn, args, kwargs)
        future.add_done_callback(lambda f: self._finalize(job_id, f))
        return job_id

    def _run(self, job_id: int, fn: JobFn, arg_tuple: tuple, kw: dict) -> dict[str, Any]:
        with self.db.session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.status = "running"
            job.started_at = datetime.utcnow()

        def progress(current: float, total: float | None, message: str) -> None:
            ratio = current / total if total else 0.0
            with self.db.session_scope() as session:
                row = session.get(Job, job_id)
                assert row is not None
                row.progress = ratio
                row.message = message[:500]
            self._emit(job_id, ratio*100 if total else 0.0, message)

        def update(message: str) -> None:
            with self.db.session_scope() as session:
                row = session.get(Job, job_id)
                assert row is not None
                row.message = message[:500]
            self._emit(job_id, 0.0, message)

        try:
            result = fn(progress=progress, update=update, **kw)
            self._emit(job_id, 100.0, "done")
            return {"ok": True, "result": _serializable(result)}
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            self._emit(job_id, 0.0, f"error: {exc}")
            return {"ok": False, "error": str(exc)}

    def _finalize(self, job_id: int, future: Future) -> None:
        with self.db.session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.finished_at = datetime.utcnow()
            if future.cancelled():
                job.status = "cancelled"
                job.error = "cancelled"
            else:
                try:
                    payload = future.result()
                except Exception as exc:  # pragma: no cover
                    job.status = "failed"
                    job.error = str(exc)
                else:
                    if payload.get("ok"):
                        job.status = "done"
                        job.result = payload.get("result")
                    else:
                        job.status = "failed"
                        job.error = payload.get("error")

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent job rows as dictionaries."""
        with self.db.session_scope() as session:
            rows = session.scalars(
                select(Job).order_by(Job.id.desc()).limit(limit)
            ).all()
            return [
                {
                    "id": row.id, "kind": row.kind, "status": row.status,
                    "progress": row.progress, "message": row.message,
                    "error": row.error, "created_at": row.created_at,
                }
                for row in rows
            ]

    def shutdown(self) -> None:
        """Stop accepting work and drain the pool."""
        self.pool.shutdown(wait=False)


def _serializable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serializable(v) for v in value]
    try:
        import json

        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)[:1000]

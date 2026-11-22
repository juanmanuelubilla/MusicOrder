"""Persistence for Decision Engine results (spec §31).

``DecisionStore`` records every decision outcome so the Fusion Engine and any
review tooling can later audit *why* a specific copy was chosen: scores per
candidate, weights used, reasons and warnings, plus (when available) the ids of
the ``match_records`` that produced the candidate group.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import select

from musicfusion.db.models import DecisionRecord
from musicfusion.db.session import Database
from musicfusion.decision.engine import DecisionResult

logger = logging.getLogger(__name__)


def group_key(*paths: str) -> str:
    """Stable identifier for a candidate group (independent of ordering)."""
    encoded = json.dumps(sorted(str(p) for p in paths), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _group_key_of(paths: list[str]) -> str:
    return group_key(*(paths or ["empty"]))


class DecisionStore:
    """Database access point for decision records."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.create_schema()

    def save(
        self,
        result: DecisionResult,
        *,
        group_key: str | None = None,
        weights: dict[str, float] | None = None,
        match_ids: list[int] | None = None,
        winner_track_id: int | None = None,
    ) -> DecisionRecord:
        """Persist a decision outcome and return the created record."""
        paths = []
        if result.winner:
            paths.append(result.winner.path)
        if result.runner_up:
            paths.append(result.runner_up.path)
        key = group_key or _group_key_of(paths)
        with self.db.session_scope() as session:
            record = DecisionRecord(
                group_key=key,
                winner_path=result.winner.path,
                winner_score=float(result.scores.get(result.winner.path, 0.0)),
                runner_up_path=result.runner_up.path if result.runner_up else None,
                runner_up_score=float(
                    result.scores.get(result.runner_up.path, 0.0) if result.runner_up else 0.0
                ),
                scores=result.scores or {},
                reasons=result.reasons or [],
                warnings=result.warnings or [],
                weights=dict(weights or {}),
                match_ids=list(match_ids or []),
                requires_review=bool(result.warnings),
                winner_track_id=winner_track_id,
            )
            session.add(record)
            session.flush()
            record_id = int(record.id)
        return self.get(record_id) or record

    def get(self, record_id: int) -> DecisionRecord | None:
        with self.db.session() as session:
            return session.get(DecisionRecord, record_id)

    def list_recent(self, limit: int = 100) -> list[DecisionRecord]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(DecisionRecord)
                    .order_by(DecisionRecord.created_at.desc())
                    .limit(limit)
                )
            )

    def clear(self) -> int:
        with self.db.session_scope() as session:
            count = session.query(DecisionRecord).count()
            session.query(DecisionRecord).delete()
            return int(count)


__all__ = ["DecisionStore", "group_key"]

"""Persist folder-comparison results into the audit tables.

This is what makes the folder comparison the *base* of the application:
after comparing two folders, the analysed files are indexed into ``tracks``
(like the fusion planner does), every compared pair is stored as a
``match_record``, same-content pairs become ``duplicate_groups``/``duplicates``
rows (so the Duplicados page lists them) and the Decision Engine winner is
stored in ``decision_records`` with its ``match_ids`` and ``winner_track_id``.

Nothing here mutates the source files: it only records what was compared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from musicfusion.config.settings import LibraryConfig
from musicfusion.db.models import DuplicateEntry, DuplicateGroup, Track
from musicfusion.decision.storage import group_key
from musicfusion.matching.storage import MatchStore, cache_key, config_fingerprint
from musicfusion.scan.indexer import LibraryIndexer

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.db.session import Database
    from musicfusion.decision.engine import DecisionResult
    from musicfusion.matching import MatchOutcome, MatchTrack

logger = logging.getLogger(__name__)


@dataclass
class PairRecord:
    """Everything needed to persist one analysed folder pair."""

    path_a: Path
    path_b: Path
    outcome: MatchOutcome
    track_a: MatchTrack
    track_b: MatchTrack
    qual_a: object
    qual_b: object
    decision: DecisionResult | None
    same: bool


def _ensure_indexed(settings, db: Database, roots: list[Path]) -> None:
    """Index the compared roots so their files live in ``tracks``."""
    indexer = LibraryIndexer(settings, db)
    for root in roots:
        try:
            indexer.scan_library(LibraryConfig(path=str(root)))
        except Exception as exc:  # noqa: BLE001 - keep going with the other root
            logger.warning("index failed for %s: %s", root, exc)


def _track_id(db: Database, path: str) -> int | None:
    with db.session() as session:
        row = session.scalar(select(Track).where(Track.path == str(path)))
        return row.id if row else None


def persist_folder_compare(
    settings,
    db: Database,
    roots: list[Path],
    pairs: list[PairRecord],
) -> dict[str, int]:
    """Index both folders and store every pair/match/decision in the database."""
    _ensure_indexed(settings, db, roots)
    from musicfusion.matching.config import MatchConfig

    cfg = MatchConfig.from_settings(settings)
    fingerprint = config_fingerprint(cfg)
    store = MatchStore(db)
    stats = {"pairs": 0, "matches": 0, "duplicates": 0, "decisions": 0}

    for pair in pairs:
        try:
            pair.outcome.cache_key = cache_key(pair.track_a, pair.track_b, fingerprint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache key failed for %s vs %s: %s", pair.path_a, pair.path_b, exc)
        source_id = _track_id(db, str(pair.path_a))
        candidate_id = _track_id(db, str(pair.path_b))
        match = store.save(
            pair.outcome,
            source_track_id=source_id,
            candidate_track_id=candidate_id,
        )
        stats["pairs"] += 1
        stats["matches"] += 1

        if pair.same and source_id and candidate_id:
            _upsert_duplicate_group(db, source_id, candidate_id, pair.outcome)
            stats["duplicates"] += 1

        if pair.decision is not None:
            _save_decision(
                db,
                pair.decision,
                winner_track_id=candidate_id if pair.decision.winner_path == str(pair.path_b) else source_id,
                match_id=match.id,
            )
            stats["decisions"] += 1
    return stats


def _upsert_duplicate_group(
    db: Database, a_track_id: int, b_track_id: int, outcome: object
) -> None:
    """Record a same-content pair as a duplicate group if not already present."""
    with db.session_scope() as session:
        existing = session.scalar(
            select(DuplicateGroup)
            .join(DuplicateEntry)
            .where(
                DuplicateEntry.track_id.in_([a_track_id, b_track_id])
            )
            .limit(1)
        )
        if existing is not None:
            members = session.scalars(
                select(DuplicateEntry).where(DuplicateEntry.group_id == existing.id)
            ).all()
            member_ids = {entry.track_id for entry in members}
            if {a_track_id, b_track_id}.issubset(member_ids):
                return
        group = DuplicateGroup(
            reason="compare",
            classification=getattr(outcome, "classification", "") or "different_version",
            confidence=float(getattr(outcome, "score", 0.0)),
        )
        session.add(group)
        session.flush()
        session.add(DuplicateEntry(group_id=group.id, track_id=a_track_id))
        session.add(DuplicateEntry(group_id=group.id, track_id=b_track_id))


def _save_decision(
    db: Database,
    decision,
    *,
    winner_track_id: int | None,
    match_id: int,
) -> None:
    """Persist a Decision Engine result linked to its match record."""
    from musicfusion.db.models import DecisionRecord

    winner = decision.winner
    runner_up = decision.runner_up
    paths = [winner.path]
    if runner_up:
        paths.append(runner_up.path)
    with db.session_scope() as session:
        record = DecisionRecord(
            group_key=group_key(*paths),
            winner_path=winner.path,
            winner_score=float(decision.scores.get(winner.path, 0.0)),
            runner_up_path=runner_up.path if runner_up else None,
            runner_up_score=float(
                decision.scores.get(runner_up.path, 0.0) if runner_up else 0.0
            ),
            scores=dict(decision.scores or {}),
            reasons=list(decision.reasons or []),
            warnings=list(decision.warnings or []),
            weights=dict(getattr(decision, "weights", {}) or {}),
            match_ids=[match_id],
            requires_review=bool(decision.warnings),
            winner_track_id=winner_track_id,
        )
        session.add(record)


__all__ = ["PairRecord", "persist_folder_compare"]

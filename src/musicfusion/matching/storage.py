"""Persistence and caching for match outcomes.

``MatchStore`` wraps the database so results can be stored, retrieved by cache
key, invalidated, approved or rejected, and re-analysed later. Cache keys are
derived from the full comparison context (both file signatures plus the active
matching configuration), so results are only reused when nothing relevant has
changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select

from musicfusion.db.models import MatchRecord
from musicfusion.db.session import Database
from musicfusion.matching.engine import MatchOutcome, MatchTrack

logger = logging.getLogger(__name__)

#: bumped whenever matching/candidate semantics change, so stored records can
#: be invalidated in bulk when the algorithm evolves (spec §32)
MATCHING_VERSION = "2"


def cache_key(
    source: MatchTrack,
    candidate: MatchTrack,
    config_fingerprint: str,
) -> str:
    """Stable hash over the comparison inputs and the active configuration."""
    payload = {
        "source": _track_sig(source),
        "candidate": _track_sig(candidate),
        "config": config_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def config_fingerprint(config: Any) -> str:
    """Serialize the tunable matching parameters into a short fingerprint."""
    values = {
        "weights": config.weights,
        "thresholds": config.thresholds,
        "fingerprint_boost": config.fingerprint_boost,
        "album_context_weight": config.album_context_weight,
        "duration_rel": config.duration_relative_tolerance,
        "duration_abs": config.duration_absolute_ms,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def _track_sig(track: MatchTrack) -> dict[str, object]:
    return {
        "path": track.path,
        "artist": track.artist,
        "album_artist": track.album_artist,
        "album": track.album,
        "title": track.title,
        "track_no": track.track_no,
        "disc_no": track.disc_no,
        "year": track.year,
        "duration_ms": track.duration_ms,
        "fingerprint": track.fingerprint,
        "isrc": track.isrc,
        "recording_id": track.recording_id,
    }


class MatchStore:
    """Database access point for match records (persistence + cache)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.create_schema()

    # ---------------------------------------------------------- cache lookups
    def find_cached(
        self,
        source: MatchTrack,
        candidate: MatchTrack,
        config_fingerprint: str,
    ) -> MatchRecord | None:
        """Return the latest non-invalidated record for this comparison."""
        key = cache_key(source, candidate, config_fingerprint)
        with self.db.session() as session:
            return session.scalar(
                select(MatchRecord)
                .where(MatchRecord.cache_key == key, MatchRecord.invalidated.is_(False))
                .order_by(MatchRecord.created_at.desc())
            )

    # --------------------------------------------------------------- store
    def save(
        self,
        outcome: MatchOutcome,
        *,
        source_track_id: int | None = None,
        candidate_track_id: int | None = None,
    ) -> MatchRecord:
        """Persist a match outcome and return the created record."""
        with self.db.session_scope() as session:
            record = MatchRecord(
                source_file=outcome.source_file,
                candidate_file=outcome.candidate_file,
                source_track_id=source_track_id,
                candidate_track_id=candidate_track_id,
                cache_key=outcome.cache_key,
                score=outcome.score,
                classification=outcome.classification,
                matching_method=outcome.matching_method,
                requires_review=outcome.requires_review,
                artist_score=outcome.artist_score,
                album_score=outcome.album_score,
                title_score=outcome.title_score,
                track_number_score=outcome.track_number_score,
                disc_number_score=outcome.disc_number_score,
                duration_score=outcome.duration_score,
                year_score=outcome.year_score,
                fingerprint_score=outcome.fingerprint_score,
                album_context_score=outcome.album_context_score,
                reasons=outcome.reasons,
                warnings=outcome.warnings,
                same_song=outcome.same_song,
                same_version=outcome.same_version,
                edition_different=outcome.edition_different,
                version_different=outcome.version_different,
                version_tag=outcome.version_tag,
                same_track=getattr(outcome, "same_track", False),
                same_album=getattr(outcome, "same_album", False),
                same_release=getattr(outcome, "same_release", False),
                context_score=getattr(outcome, "context_score", 0.0),
                candidate_phase=getattr(outcome, "candidate_phase", ""),
                matching_version=MATCHING_VERSION,
                decision="pending",
                invalidated=False,
            )
            session.add(record)
            session.flush()
            record_id = record.id
        return self.get(record_id) or record

    def get(self, record_id: int) -> MatchRecord | None:
        with self.db.session() as session:
            return session.get(MatchRecord, record_id)

# ---------------------------------------------------------------- decisions
    def decide(self, record_id: int, decision: str) -> MatchRecord | None:
        """Approve, reject or invalidate a stored match."""
        valid = {"approved", "rejected", "invalidated", "pending"}
        if decision not in valid:
            raise ValueError(f"invalid decision: {decision}")
        with self.db.session_scope() as session:
            record = session.get(MatchRecord, record_id)
            if record is None:
                return None
            if decision in ("approved", "rejected"):
                record.decision = decision
            elif decision == "invalidated":
                record.invalidated = True
            else:
                record.decision = "pending"
                record.invalidated = False
        return self.get(record_id)

    def list_recent(self, limit: int = 100) -> list[MatchRecord]:
        """List the latest match records for review."""
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(MatchRecord).order_by(MatchRecord.created_at.desc()).limit(limit)
                )
            )

    def clear(self) -> int:
        """Delete every match record (re-analysis reset)."""
        with self.db.session_scope() as session:
            count = session.query(MatchRecord).count()
            session.query(MatchRecord).delete()
            return int(count)


__all__ = ["MatchStore", "cache_key", "config_fingerprint"]

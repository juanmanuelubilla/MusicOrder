"""Fusion planner.

The planner indexes the source libraries on demand, compares every source
track against the other library using the intelligent comparer, and produces a
non-destructive :class:`FusionPlan` of copy / keep / conflict actions. Source
libraries are never modified.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from musicfusion.db.models import Track
from musicfusion.db.session import Database
from musicfusion.decision.engine import Candidate, DecisionEngine
from musicfusion.matching import MatchConfig, MatchEngine
from musicfusion.matching.normalize import normalize_music_text
from musicfusion.scan.indexer import LibraryIndexer

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.matching.engine import MatchTrack

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 70.0  # Matching Engine score to consider "same recording"


def _is_under(path: str, roots: list[Path]) -> bool:
    """Whether ``path`` is inside any of the given root folders."""
    try:
        candidate = Path(path).resolve()
    except OSError:
        return False
    return any(candidate.is_relative_to(root) for root in roots)


@dataclass
class PlanAction:
    """A single planned file operation."""

    source: Path
    target: Path
    kind: str
    score: float = 0.0
    note: str = ""

    def body(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "kind": self.kind,
            "score": self.score,
            "note": self.note,
        }


@dataclass
class FusionPlan:
    """A complete, non-destructive fusion plan."""

    lib_a: Path
    lib_b: Path
    destination: Path
    actions: list[PlanAction] = field(default_factory=list)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        lines = [
            f"Fusion plan: {self.lib_a} + {self.lib_b} -> {self.destination}",
            f"  counts: {counts}",
        ]
        for action in self.actions[:15]:
            lines.append(f"  [{action.kind:<8}] {action.source.name} -> {action.target}")
        if len(self.actions) > 15:
            lines.append(f"  ... and {len(self.actions) - 15} more actions")
        return "\n".join(lines)


class _Verdicts:
    """Duplicate-group verdicts persisted by a previous folder comparison.

    Lets the planner honour the Match/Decision engines already run in the
    Comparar page: each duplicate loser is mapped to the Decision winner, so a
    fusion only copies the winning copy of every duplicate song.
    """

    def __init__(self) -> None:
        self.skip: set[str] = set()
        self.dedup: dict[str, str] = {}
        self._groups: dict[str, set[int]] = defaultdict(set)
        self._winner_by_group: dict[int, str] = {}

    def shared_winner(self, a: str, b: str) -> str | None:
        common = self._groups.get(a, set()) & self._groups.get(b, set())
        for group_id in common:
            winner = self._winner_by_group.get(group_id)
            if winner:
                return winner
        return None


def track_to_match_track(track: Track, fingerprints: dict[int, str]) -> MatchTrack:
    """Project a DB track row into a Matching Engine track.

    The acoustic fingerprint computed by the Indexer travels with the track so
    identity decisions can use it (fixes the old ``acoustic_id=""`` gap).
    """
    from musicfusion.matching.engine import MatchTrack

    return MatchTrack(
        path=track.path or "",
        artist=track.artist_name,
        album_artist=track.album_artist or "",
        album=track.album_name or "",
        title=track.title,
        track_no=track.track_no,
        disc_no=track.disc_no,
        year=track.year,
        duration_ms=track.duration_ms or 0,
        fingerprint=fingerprints.get(track.id, ""),
        recording_id=track.musicbrainz_recording_id or "",
        isrc=track.isrc or "",
        id=track.id,
    )


class FusionPlanner:
    """Analyzes source libraries and produces a fusion plan."""

    def __init__(
        self,
        settings,
        db: Database | None = None,
        *,
        threshold: float = MATCH_THRESHOLD,
    ) -> None:
        from musicfusion.config.settings import Settings

        self.settings: Settings = settings
        self.db = db or Database(settings.db_path)
        self.db.create_schema()
        self.threshold = threshold
        self.engine = MatchEngine(MatchConfig.from_settings(settings))

    def _fingerprint_map(self) -> dict[int, str]:
        """track_id -> chromaprint fingerprint already computed by the Indexer."""
        from musicfusion.db.models import TrackFingerprint

        with self.db.session() as session:
            rows = session.query(TrackFingerprint).filter_by(algorithm="chromaprint").all()
            return {row.track_id: row.value for row in rows if row.value}

    def _index_source(
        self, root: Path, on_progress=None
    ) -> None:
        """Ensure a source root is indexed (read-only on the source)."""
        from musicfusion.config.settings import LibraryConfig

        indexer = LibraryIndexer(self.settings, self.db)
        indexer.scan_library(
            LibraryConfig(path=str(root)),
            on_progress=on_progress,
        )

    def _load_tracks(self, root: Path) -> list[Track]:
        resolved = str(root.resolve())
        with self.db.session_scope() as session:
            query = select(Track).join(Track.library).where(
                Track.library.has(path=resolved)
            )
            return list(session.scalars(query))

    def _load_verdicts(self, roots: list[Path]) -> _Verdicts:
        """Load duplicate-group verdicts persisted by a past folder comparison."""
        from musicfusion.db.models import DecisionRecord, DuplicateEntry, Track

        verdicts = _Verdicts()
        sources = [root.resolve() for root in roots]
        with self.db.session_scope() as session:
            path_by_id = dict(session.query(Track.id, Track.path).all())
        with self.db.session_scope() as session:
            entries = session.query(DuplicateEntry.group_id, DuplicateEntry.track_id).all()
        members_by_group: dict[int, set[str]] = defaultdict(set)
        for group_id, track_id in entries:
            path = path_by_id.get(track_id)
            if path:
                members_by_group[group_id].add(path)
                verdicts._groups[path].add(group_id)
        with self.db.session_scope() as session:
            decisions = (
                session.query(DecisionRecord)
                .filter(DecisionRecord.runner_up_path.isnot(None))
                .order_by(DecisionRecord.id)
                .all()
            )
        for record in decisions:
            winner, runner_up = record.winner_path, record.runner_up_path
            if not _is_under(winner, sources) or not _is_under(runner_up, sources):
                continue
            common = verdicts._groups.get(winner, set()) & verdicts._groups.get(runner_up, set())
            if not common:
                continue
            verdicts._winner_by_group[next(iter(common))] = winner
        for group_id, members in members_by_group.items():
            winner = verdicts._winner_by_group.get(group_id)
            if not winner or winner not in members:
                continue
            for member in members:
                if member != winner:
                    verdicts.dedup[member] = winner
        verdicts.skip = set(verdicts.dedup)
        return verdicts

    def _destination_for(self, destination: Path, track: Track) -> Path:
        # Normaliza el nombre del artista para que variantes con tilde/distinta
        # capitalizacion queden en la misma carpeta.
        artist_normalized = normalize_music_text(track.artist_name or "")
        # Build path: artist/album/title (using rel_path if available, else title)
        if track.rel_path:
            rel = Path(track.rel_path)
            # Prepend normalized artist name as first folder
            artist_part = artist_normalized if artist_normalized else Path(track.artist_name or "unknown")
            full_path = Path(artist_part) / rel
        else:
            # Fall back to title-based path with normalized artist
            full_path = Path(artist_normalized) / track.title if artist_normalized else Path(track.title)
        return destination / full_path

    def _plan_single(
        self,
        root: Path,
        destination: Path,
        on_progress=None,
    ) -> FusionPlan:
        """Plan a single-folder dedup copy into ``destination``.

        With only one source folder there is nothing to match against another
        library: the planner trusts the duplicate-group verdicts persisted by a
        past folder comparison (which use fingerprints + metadata) and copies
        every winner or unique track, skipping the duplicate losers.
        """
        self._index_source(root, on_progress=on_progress)
        tracks = self._load_tracks(root)
        plan = FusionPlan(lib_a=root, lib_b=root, destination=destination)
        verdicts = self._load_verdicts([root])
        winners = set(verdicts.dedup.values())
        for track in tracks:
            target = self._destination_for(destination, track)
            source = Path(track.path)
            if track.path in verdicts.skip:
                plan.actions.append(
                    PlanAction(
                        source=source,
                        target=target,
                        kind="skip",
                        note=f"duplicado; se conserva {Path(verdicts.dedup[track.path]).name}",
                    )
                )
            elif track.path in winners:
                plan.actions.append(
                    PlanAction(
                        source=source,
                        target=target,
                        kind="copy",
                        note="duplicado; se conserva la copia ganadora de la comparación",
                    )
                )
            else:
                plan.actions.append(
                    PlanAction(
                        source=source,
                        target=target,
                        kind="copy",
                        note="único en la carpeta",
                    )
                )
        return plan

    def plan(
        self,
        lib_a: Path,
        lib_b: Path,
        destination: Path,
        on_progress=None,
    ) -> FusionPlan:
        """Index sources and compute the action list, never touching sources."""
        lib_a, lib_b, destination = (
            Path(lib_a).expanduser(),
            Path(lib_b).expanduser(),
            Path(destination).expanduser(),
        )
        if lib_a.resolve() == lib_b.resolve():
            return self._plan_single(lib_a, destination, on_progress=on_progress)
        self._index_source(lib_a, on_progress=on_progress)
        self._index_source(lib_b, on_progress=on_progress)

        tracks_a = self._load_tracks(lib_a)
        tracks_b = self._load_tracks(lib_b)
        plan = FusionPlan(lib_a=lib_a, lib_b=lib_b, destination=destination)
        verdicts = self._load_verdicts([lib_a, lib_b])

        # Match B against A; unmatched B tracks are copied.
        matched_b: set[int] = set()
        for track_a in tracks_a:
            target = self._destination_for(destination, track_a)
            if track_a.path in verdicts.skip:
                plan.actions.append(
                    PlanAction(
                        source=Path(track_a.path),
                        target=target,
                        kind="skip",
                        note=f"duplicado; se conserva {Path(verdicts.dedup[track_a.path]).name}",
                    )
                )
                continue
            best_b, best_score = self._best_match(track_a, tracks_b)
            if best_b and best_score >= self.threshold:
                matched_b.add(best_b.id)
                if track_a.path and best_b.path and verdicts.shared_winner(
                    track_a.path, best_b.path
                ):
                    plan.actions.append(
                        PlanAction(
                            source=Path(track_a.path),
                            target=target,
                            kind="copy",
                            score=best_score,
                            note="duplicado; se conserva la copia ganadora de la comparación",
                        )
                    )
                else:
                    winner = self._pick_better(track_a, best_b)
                    if winner == track_a:
                        plan.actions.append(
                            PlanAction(
                                source=Path(track_a.path),
                                target=target,
                                kind="copy",
                                score=best_score,
                                note="same recording in both libraries (better copy)",
                            )
                        )
                    else:
                        plan.actions.append(
                            PlanAction(
                                source=Path(best_b.path),
                                target=target,
                                kind="copy",
                                score=best_score,
                                note="same recording; better copy found in B",
                            )
                        )
            else:
                plan.actions.append(
                    PlanAction(
                        source=Path(track_a.path),
                        target=target,
                        kind="copy",
                        score=best_score,
                        note="unique to library A",
                    )
                )

        for track_b in tracks_b:
            if track_b.id in matched_b:
                continue
            if track_b.path in verdicts.skip:
                plan.actions.append(
                    PlanAction(
                        source=Path(track_b.path),
                        target=self._destination_for(destination, track_b),
                        kind="skip",
                        note=f"duplicado; se conserva {Path(verdicts.dedup[track_b.path]).name}",
                    )
                )
                continue
            already = any(
                action.target == self._destination_for(destination, track_b)
                for action in plan.actions
            )
            plan.actions.append(
                PlanAction(
                    source=Path(track_b.path),
                    target=self._destination_for(destination, track_b),
                    kind="skip" if already else "copy",
                    note="already planned" if already else "unique to library B",
                )
            )
        return plan

    def _best_match(
        self, track: Track, candidates: list[Track]
    ) -> tuple[Track | None, float]:
        """Best candidate according to the shared Matching Engine.

        Identity is decided by the SAME engine used everywhere else (spec §31):
        weighted metadata signals + acoustic fingerprints, and a different
        version (live/remix/duration gap) never counts as the same recording.
        """
        if not hasattr(self, "_fp_map"):
            self._fp_map = self._fingerprint_map()
        left = track_to_match_track(track, self._fp_map)
        best: Track | None = None
        best_score = 0.0
        for candidate in candidates:
            if candidate.id == track.id:
                continue
            right = track_to_match_track(candidate, self._fp_map)
            try:
                outcome = self.engine.match(left, right)
            except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
                logger.warning("planner match failed: %s vs %s: %s", left.path, right.path, exc)
                continue
            if outcome.version_different:
                continue
            if outcome.score > best_score:
                best_score = outcome.score
                best = candidate
        if best_score < self.threshold:
            return None, best_score
        return best, best_score

    def _pick_better(self, a: Track, b: Track) -> Track:
        """Pick the copy the Decision Engine prefers (quality+metadata+artwork).

        Uses the ``decision.weights`` configured in the YAML settings.
        """
        decision = DecisionEngine.from_settings(self.settings).decide(
            [Candidate.from_track(a), Candidate.from_track(b)]
        )
        return a if decision.winner_path == a.path else b

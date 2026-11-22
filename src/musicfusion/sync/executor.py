"""Conservative merge executor for collection incorporation.

Turns a :class:`~musicfusion.sync.analyzer.SyncPlan` into concrete actions and
- only with explicit confirmation - copies what is MISSING from the master:

* NEW_ARTIST   -> copy the whole artist folder (PATH 2 name, no equivalent).
* NEW_ALBUM    -> copy the album folder INTO the master artist's folder
                  (destination uses the MASTER artist name).
* EXISTS / REVIEW / ambiguous -> never touched.

Hard safety rules: the master is never modified beyond *adding* new folders;
an existing destination is NEVER overwritten (re-checked at copy time);
nothing is ever deleted.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MergeAction:
    """One planned filesystem action (or an explicit no-op)."""

    kind: str                     # COPY_ARTIST | COPY_ALBUM | NONE | REVIEW
    status: str                   # EXISTE | FALTA | POSIBLE_EQUIVALENTE
    action_label: str             # NO_HACER_NADA | COPIAR | REVIEW
    source: Path | None
    destination: Path | None
    reason: str
    confidence: float = 0.0
    review_required: bool = False
    artist_incoming: str = ""
    artist_master: str | None = None
    item: str = ""                # incoming album folder or artist name

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "action": self.action_label,
            "source": str(self.source) if self.source else None,
            "destination": str(self.destination) if self.destination else None,
            "reason": self.reason,
            "confidence": self.confidence,
            "review_required": self.review_required,
            "artist_incoming": self.artist_incoming,
            "artist_master": self.artist_master,
            "item": self.item,
        }


@dataclass
class MergeResult:
    """Outcome of executing (or dry-running) the actions."""

    copied: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_review: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    files_copied: int = 0
    bytes_copied: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "skipped_existing": self.skipped_existing,
            "skipped_review": self.skipped_review,
            "errors": self.errors,
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
        }


def build_actions(plan) -> list[MergeAction]:
    """Translate a SyncPlan into conservative actions."""
    actions: list[MergeAction] = []
    for artist in plan.artists:
        if artist.decision == "NEW_ARTIST":
            src = plan.incoming / artist.incoming_name
            dst = plan.master / artist.incoming_name
            actions.append(MergeAction(
                kind="COPY_ARTIST",
                status=artist.status,
                action_label="COPIAR",
                source=src,
                destination=dst,
                reason=f"artista nuevo: incorporar completo ({artist.reason})",
                confidence=artist.confidence,
                review_required=False,
                artist_incoming=artist.incoming_name,
                item=artist.incoming_name,
            ))
            continue
        if artist.decision != "ARTIST_MATCHED":
            actions.append(MergeAction(
                kind="REVIEW",
                status=artist.status,
                action_label="REVIEW",
                source=None,
                destination=None,
                reason=f"equivalencia de artista dudosa ({artist.reason})",
                confidence=artist.confidence,
                review_required=True,
                artist_incoming=artist.incoming_name,
                artist_master=artist.master_name,
                item=artist.incoming_name,
            ))
            continue

        for album in artist.albums:
            common = dict(
                confidence=album.confidence,
                artist_incoming=artist.incoming_name,
                artist_master=artist.master_name,
                item=album.incoming_folder,
            )
            if album.decision == "EXISTS":
                actions.append(MergeAction(
                    kind="NONE",
                    status=album.status,
                    action_label="NO_HACER_NADA",
                    source=None,
                    destination=None,
                    reason=f"ya está en el maestro como '{album.master_folder}'",
                    **common,
                ))
            elif album.decision == "NEW_ALBUM":
                assert artist.master_name is not None
                src = plan.incoming / artist.incoming_name / album.incoming_folder
                dst = plan.master / artist.master_name / album.incoming_folder
                actions.append(MergeAction(
                    kind="COPY_ALBUM",
                    status=album.status,
                    action_label="COPIAR",
                    source=src,
                    destination=dst,
                    reason=f"álbum ausente en el maestro ({album.reason})",
                    **common,
                ))
            else:  # AMBIGUOUS_ALBUM
                actions.append(MergeAction(
                    kind="REVIEW",
                    status=album.status,
                    action_label="REVIEW",
                    source=plan.incoming / artist.incoming_name / album.incoming_folder,
                    destination=None,
                    reason=f"duda de equivalencia con '{album.master_folder}' ({album.reason})",
                    review_required=True,
                    **common,
                ))
    return actions


def _tree_size(path: Path) -> tuple[int, int]:
    files = bytes_ = 0
    for p in path.rglob("*"):
        if p.is_file():
            files += 1
            bytes_ += p.stat().st_size
    return files, bytes_


def execute_merge(actions: list[MergeAction], *, assume_yes: bool = False) -> MergeResult:
    """Perform COPIAR actions. Copies happen ONLY with ``assume_yes=True``.

    Without explicit confirmation this is a safe no-op (dry-run friendly):
    nothing is ever written. Safety re-checks at copy time: if the destination
    appeared meanwhile it is SKIPPED, never overwritten. REVIEW/NONE items are
    always untouched.
    """
    result = MergeResult()
    pending = [a for a in actions if a.action_label == "COPIAR"]
    if not pending:
        logger.info("merge: nada para copiar")
        return result
    if not assume_yes:
        # conservative: without explicit confirmation this call does NOTHING
        logger.warning(
            "merge: %s ítems pendientes NO copiados (falta confirmación explícita)",
            len(pending),
        )
        return result

    for action in pending:
        assert action.source is not None and action.destination is not None
        if not action.source.is_dir():
            result.errors.append(f"origen inexistente: {action.source}")
            continue
        if action.destination.exists():
            result.skipped_existing.append(str(action.destination))
            continue
        try:
            shutil.copytree(action.source, action.destination)
            files, size = _tree_size(action.destination)
            result.copied.append(str(action.destination))
            result.files_copied += files
            result.bytes_copied += size
        except OSError as exc:
            result.errors.append(f"fallo copiando {action.source} -> {action.destination}: {exc}")
    return result


__all__ = [
    "MergeAction",
    "MergeResult",
    "build_actions",
    "execute_merge",
]

"""Collection CONSOLIDATION stage: two sources -> ONE clean collection.

Built ON TOP of the existing pipeline (nothing re-implemented):

    LibraryIndexer (via FusionPlanner)          -> indexed tracks + fingerprints
    compare.candidates (phases 1-5)             -> artist/album/track candidates
    matching.album.album_similarity/context     -> album composition evidence
    MatchEngine.match                           -> same-recording identity
    DecisionEngine.decide                       -> best-copy winner
    FusionExecutor                              -> non-destructive execution + undo

Hierarchy respected at every step:

    ARTISTA -> ÁLBUM -> PISTA -> ARCHIVO -> CALIDAD/METADATA/CONTENIDO

Safety model: analyze -> plan -> validate -> execute -> verify -> (cleanup).
Sources are never modified; conflicting files go to ``DEST/_REVISION``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz as _fuzz

from musicfusion.audio.hashing import hash_file
from musicfusion.compare.candidates import (
    AlbumFolder,
    AlbumPairing,
    TrackEntry,
    build_album_folders,
    compute_tracklist_similarity,
    make_track_entry,
    pair_album_folders,
    pair_tracks_in_albums,
)
from musicfusion.db.session import Database
from musicfusion.decision.engine import Candidate, DecisionEngine
from musicfusion.matching import MatchConfig, MatchEngine, album_context, album_signature
from musicfusion.matching.album import member_from_track
from musicfusion.merge.executor import FusionExecutor
from musicfusion.merge.planner import (
    MATCH_THRESHOLD,
    FusionPlan,
    FusionPlanner,
    track_to_match_track,
)

logger = logging.getLogger(__name__)

ARTIST_MATCH_THRESHOLD = 0.90
TRACKLIST_STRONG = 0.75
TRACKLIST_WEAK = 0.45
COUNT_RATIO_MIN = 0.60
CONFLICT_GAP = 3.0
REVIEW_DIR = "_REVISION"

_SAME_OK = {"MATCH_VERY_HIGH", "MATCH_HIGH", "MATCH_CONFIRMED"}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass
class ConsolidationAction:
    """One planned filesystem operation with its full justification."""

    source: Path
    target: Path
    kind: str  # copy | review | skip
    reason: str = ""
    decision: str = ""  # SELECT_A | SELECT_B | COPY_FROM_A | COPY_FROM_B | KEEP_BOTH | REVIEW_REQUIRED
    match_score: float = 0.0
    quality_score: float = 0.0
    discarded: Path | None = None
    artist: str = ""
    album: str = ""
    track: str = ""
    source_a: Path | None = None
    source_b: Path | None = None
    selected: str = ""  # "A" | "B" | ""
    confidence: float = 0.0  # 0..1
    operation: str = "COPY"

    def body(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.target),
            "album": self.album,
            "track": self.track,
            "kind": self.kind,
            "source_a": str(self.source_a) if self.source_a else None,
            "source_b": str(self.source_b) if self.source_b else None,
            "selected_source": self.selected or None,
            "matching_score": round(self.match_score, 1),
            "quality_score": round(self.quality_score, 1),
            "decision": self.decision,
            "reason": self.reason,
            "operation": self.operation,
            "confidence": round(self.confidence, 3),
            "discarded_source": str(self.discarded) if self.discarded else None,
            "originals_touched": False,
        }


@dataclass
class TrackVerdict:
    """Outcome of comparing one track slot between both sources."""

    title: str
    entry_a: TrackEntry | None = None
    entry_b: TrackEntry | None = None
    result: str = "UNIQUE"  # EXACT | DUPLICATE | MATCH | UNIQUE | CONFLICT
    decision: str = ""  # SELECT_A | SELECT_B | KEEP_BOTH | COPY_FROM_A | COPY_FROM_B | REVIEW_REQUIRED
    reason: str = ""
    match_score: float = 0.0


@dataclass
class AlbumVerdict:
    """Outcome of comparing two album folders (or an exclusive one)."""

    folder_a: AlbumFolder | None = None
    folder_b: AlbumFolder | None = None
    status: str = "EXCLUSIVE"  # MERGED | EXCLUSIVE_A | EXCLUSIVE_B | REVIEW
    match_score: float = 0.0
    reason: str = ""
    tracks: list[TrackVerdict] = field(default_factory=list)

    @property
    def label(self) -> str:
        folder = self.folder_a or self.folder_b
        return folder.raw_album if folder else "?"


@dataclass
class ArtistVerdict:
    """Matched/exclusive artist group and all its album verdicts."""

    name_a: str = ""
    name_b: str = ""
    display: str = ""
    match_score: float = 0.0
    albums: list[AlbumVerdict] = field(default_factory=list)


@dataclass
class PlanStats:
    albums_only_a: int = 0
    albums_only_b: int = 0
    albums_merged: int = 0
    albums_review: int = 0
    tracks_only_a: int = 0
    tracks_only_b: int = 0
    duplicates_removed: int = 0
    duplicates_exact: int = 0
    conflicts: int = 0
    auto_decisions: int = 0
    pending_decisions: int = 0
    versions_kept_both: int = 0
    selected_a: int = 0
    selected_b: int = 0
    files_to_destination: int = 0
    source_files_modified: int = 0
    source_files_deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))


@dataclass
class ConsolidationPlan:
    """Complete, non-destructive consolidation plan for A + B -> destination."""

    lib_a: Path
    lib_b: Path
    destination: Path
    artists: list[ArtistVerdict] = field(default_factory=list)
    actions: list[ConsolidationAction] = field(default_factory=list)
    stats: PlanStats = field(default_factory=PlanStats)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def summary(self) -> str:
        counts: dict[str, int] = defaultdict(int)
        for action in self.actions:
            counts[action.kind] += 1
        lines = [
            f"Consolidation plan: {self.lib_a} + {self.lib_b} -> {self.destination}",
            f"  acciones: {dict(counts)}",
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lib_a": str(self.lib_a),
            "lib_b": str(self.lib_b),
            "destination": str(self.destination),
            "created_at": self.created_at.isoformat(),
            "stats": self.stats.as_dict(),
            "artists": [
                {
                    "name_a": artist.name_a,
                    "name_b": artist.name_b,
                    "display": artist.display,
                    "match_score": round(artist.match_score, 1),
                    "albums": [
                        {
                            "label": album.label,
                            "status": album.status,
                            "match_score": round(album.match_score, 1),
                            "reason": album.reason,
                            "tracks": [
                                {
                                    "title": tv.title,
                                    "result": tv.result,
                                    "decision": tv.decision,
                                    "reason": tv.reason,
                                    "match_score": round(tv.match_score, 1),
                                    "a": tv.entry_a.path.name if tv.entry_a else None,
                                    "b": tv.entry_b.path.name if tv.entry_b else None,
                                }
                                for tv in album.tracks
                            ],
                        }
                        for album in artist.albums
                    ],
                }
                for artist in self.artists
            ],
            "actions": [action.body() for action in self.actions],
        }

    # ------------------------------------------------------------- preview --
    def preview_text(self) -> str:
        """Human-readable DRY RUN preview (MUSICFUSION CONSOLIDATION PREVIEW)."""
        bar = "=" * 60
        sep = "-" * 60
        lines: list[str] = [
            bar,
            "MUSICFUSION CONSOLIDATION PREVIEW",
            bar,
            "",
            f"SOURCE A:\n{self.lib_a}/",
            "",
            f"SOURCE B:\n{self.lib_b}/",
            "",
            f"DESTINATION:\n{self.destination}/",
            "",
        ]
        for artist in self.artists:
            if artist.name_a and artist.name_b:
                lines += [f"ARTISTA: {artist.display}  (MATCH {artist.match_score:.1f}%)", ""]
            elif artist.name_a:
                lines += [f"ARTISTA: {artist.name_a}  (solo en A)", ""]
            else:
                lines += [f"ARTISTA: {artist.name_b}  (solo en B)", ""]
            for album in artist.albums:
                lines.append(f"ÁLBUM:\n{album.label}")
                status_label = {
                    "MERGED": "MERGED",
                    "EXCLUSIVE_A": "A ONLY",
                    "EXCLUSIVE_B": "B ONLY",
                    "REVIEW": "REVIEW",
                }.get(album.status, album.status)
                match_line = f"MATCH:\n{album.match_score:.1f}%" if album.match_score else ""
                header = [status_label]
                if match_line:
                    header.append(match_line)
                lines += ["  " + line for line in header]
                lines.append("")
                for number, tv in enumerate(album.tracks, start=1):
                    lines.append(f"TRACK {number:02d}:")
                    if tv.entry_a is not None:
                        lines.append(f"A → {tv.entry_a.path.name}")
                    if tv.entry_b is not None:
                        lines.append(f"B → {tv.entry_b.path.name}")
                    lines.append("")
                    result_label = {
                        "EXACT": "EXACT DUPLICATE",
                        "DUPLICATE": "DUPLICATE",
                        "MATCH": "MATCH",
                        "DIFFERENT": "DIFFERENT VERSIONS",
                        "UNIQUE": "UNIQUE",
                        "CONFLICT": "CONFLICT / REVIEW REQUIRED",
                    }.get(tv.result, tv.result)
                    decision_label = {
                        "SELECT_A": "COPY A TO DESTINATION",
                        "SELECT_B": "COPY B TO DESTINATION",
                        "COPY_FROM_A": "COPY A TO DESTINATION",
                        "COPY_FROM_B": "COPY B TO DESTINATION",
                        "KEEP_BOTH": "COPY BOTH VERSIONS TO DESTINATION",
                        "REVIEW_REQUIRED": "REVIEW_REQUIRED (BOTH COPIES PRESERVED)",
                    }.get(tv.decision, tv.decision or "-")
                    lines.append(f"RESULT:\n{result_label}")
                    lines.append("")
                    lines.append(f"DECISION:\n{decision_label}")
                    if tv.reason:
                        lines.append("")
                        lines.append(f"RAZÓN: {tv.reason}")
                    lines.append("")
                    lines.append("SOURCE A:\nUNCHANGED" if tv.entry_a else "SOURCE A:\n-")
                    lines.append("")
                    lines.append("SOURCE B:\nUNCHANGED" if tv.entry_b else "SOURCE B:\n-")
                    lines.append(sep)
                lines.append("")
        stats = self.stats
        lines += [
            bar,
            "RESUMEN",
            "",
            "Albums:",
            f"    A only: {stats.albums_only_a}",
            f"    B only: {stats.albums_only_b}",
            f"    merged: {stats.albums_merged}",
            "",
            "Tracks:",
            f"    A only: {stats.tracks_only_a}",
            f"    B only: {stats.tracks_only_b}",
            f"    duplicates: {stats.duplicates_removed}",
            f"    conflicts: {stats.conflicts}",
            f"    selected A: {stats.selected_a}",
            f"    selected B: {stats.selected_b}",
            "",
            "Files to destination:",
            f"    {stats.files_to_destination}",
            "",
            "Source files modified:",
            "    0",
            "",
            "Source files deleted:",
            "    0",
            bar,
        ]
        return "\n".join(lines)


@dataclass
class VerificationResult:
    """Post-execution integrity report."""

    expected: int = 0
    verified: int = 0
    missing: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    accidental_duplicates: list[str] = field(default_factory=list)
    sources_checked: int = 0
    sources_modified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing or self.collisions or self.unreadable
            or self.accidental_duplicates or self.sources_modified
        )

    def summary(self) -> str:
        state = "OK" if self.ok else "FALLOS"
        parts = [
            f"integridad={state}",
            f"{self.verified}/{self.expected} archivos verificados",
            f"{len(self.missing)} faltantes",
            f"{len(self.collisions)} colisiones",
            f"{len(self.unreadable)} ilegibles",
            f"{len(self.accidental_duplicates)} duplicados accidentales",
            f"fuentes verificadas={self.sources_checked} "
            f"modificadas={len(self.sources_modified)}",
        ]
        return ": ".join(parts[0:1]) + " " + ", ".join(parts[1:])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _richness(name: str) -> tuple[int, int]:
    """Display-quality score of a folder/name variant: (accents, mixed-case)."""
    accents = sum(1 for ch in name if ord(ch) > 127 and ch.isalpha())
    upper = sum(1 for ch in name if ch.isupper())
    return accents, upper


def _display_choice(a: str | None, b: str | None) -> str:
    """Best-looking variant of the same name: accents > capitals > B."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    ra, rb = _richness(a), _richness(b)
    if ra != rb:
        return a if ra > rb else b
    return b if len(b) >= len(a) else a


def _sidecars(folder: Path, audio_exts: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() not in audio_exts
    )


def _image_area(path: Path) -> int:
    try:
        from musicfusion.audio.artwork import _probe

        width, height = _probe(path.read_bytes())
        return width * height
    except Exception:  # noqa: BLE001 - any decoder failure falls back to size
        return 0


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------


class ConsolidationPlanner:
    """Plans the intelligent consolidation of two source collections."""

    def __init__(self, settings, db: Database | None = None) -> None:
        self.settings = settings
        self.fusion = FusionPlanner(settings, db)
        self.engine = MatchEngine(MatchConfig.from_settings(settings))
        self.threshold = MATCH_THRESHOLD

    # ------------------------------------------------------------ public ---
    def plan(
        self,
        lib_a: Path,
        lib_b: Path,
        destination: Path,
        on_progress=None,
        *,
        overwrite: bool = False,
    ) -> ConsolidationPlan:
        """Analyze both sources and produce the full consolidation plan."""
        lib_a = Path(lib_a).expanduser().resolve()
        lib_b = Path(lib_b).expanduser().resolve()
        destination = Path(destination).expanduser().resolve()
        if destination == lib_a or destination == lib_b:
            raise ValueError("El destino no puede ser igual a una fuente")
        if destination.is_relative_to(lib_a) or destination.is_relative_to(lib_b):
            raise ValueError("El destino no puede estar dentro de una fuente")
        if lib_a.is_relative_to(destination) or lib_b.is_relative_to(destination):
            raise ValueError("Una fuente no puede estar dentro del destino")
        if destination.exists() and not overwrite:
            raise ValueError(
                f"El destino ya existe: {destination}. "
                "Elegí otro destino o repetí con overwrite=True (--overwrite) "
                "para fusionar sin borrar nada."
            )

        progress = on_progress or (lambda done, total, msg: None)
        fusion = self.fusion
        progress(0, 100, f"Indexando {lib_a.name}…")
        fusion._index_source(lib_a)
        progress(10, 100, f"Indexando {lib_b.name}…")
        fusion._index_source(lib_b)

        tracks_a = fusion._load_tracks(lib_a)
        tracks_b = fusion._load_tracks(lib_b)
        fingerprints = fusion._fingerprint_map()

        folders_a = build_album_folders(
            [make_track_entry(Path(t.path), "a", track_to_match_track(t, fingerprints)) for t in tracks_a]
        )
        folders_b = build_album_folders(
            [make_track_entry(Path(t.path), "b", track_to_match_track(t, fingerprints)) for t in tracks_b]
        )

        plan = ConsolidationPlan(lib_a=lib_a, lib_b=lib_b, destination=destination)
        artists_a = _group_by_artist(folders_a)
        artists_b = _group_by_artist(folders_b)
        used_b: set[str] = set()
        step = max(1, (len(artists_a) + len(artists_b)) // 80 or 1)
        done = 15

        for key_a, group_a in sorted(artists_a.items()):
            best_key, best_score = self._best_artist_group(key_a, group_a, artists_b)
            artist = ArtistVerdict(name_a=group_a.raw_artist, display=group_a.raw_artist)
            if best_key is not None and best_score >= ARTIST_MATCH_THRESHOLD:
                used_b.add(best_key)
                group_b = artists_b[best_key]
                artist.name_b = group_b.raw_artist
                artist.display = _display_choice(group_a.raw_artist, group_b.raw_artist)
                artist.match_score = best_score * 100.0
                self._plan_matched_artist(plan, artist, group_a, group_b, destination, progress)
            else:
                self._plan_exclusive_side(plan, artist, group_a, None, destination, "A")
            plan.artists.append(artist)
            done += step
            progress(done, 100, f"Analizando {group_a.raw_artist}…")

        for key_b, group_b in sorted(artists_b.items()):
            if key_b in used_b:
                continue
            artist = ArtistVerdict(name_b=group_b.raw_artist, display=group_b.raw_artist)
            self._plan_exclusive_side(plan, artist, None, group_b, destination, "B")
            plan.artists.append(artist)
            done += step
            progress(done, 100, f"Analizando {group_b.raw_artist}…")

        _disambiguate_targets(plan)
        _fill_stats(plan)
        return plan

    # ----------------------------------------------------------- artists ---
    def _best_artist_group(self, key_a, group_a, artists_b) -> tuple[str | None, float]:
        best_key: str | None = None
        best = 0.0
        for key_b, group_b in artists_b.items():
            if key_a == key_b:
                return key_b, 1.0
            score = _fuzzy(key_a, key_b)
            if score > best:
                best, best_key = score, key_b
        return best_key, best

    # ------------------------------------------------------- album level ---
    def _pair_albums(
        self, group_a: _ArtistGroup, group_b: _ArtistGroup
    ) -> list[tuple[AlbumFolder, AlbumFolder, float, str]]:
        config = MatchConfig.from_settings(self.settings)
        pairings = pair_album_folders(group_a.folders, group_b.folders, config)
        results: list[tuple[AlbumFolder, AlbumFolder, float, str]] = []
        for pairing in pairings:
            status, score, reason = self._album_verdict(pairing)
            if status == "MERGED":
                results.append((pairing.a, pairing.b, score, reason))
        return results

    def _album_verdict(self, pairing: AlbumPairing) -> tuple[str, float, str]:
        """Hierarchical album gate: never merge on the name alone.

        Equal normalized keys still need corroboration (year / tracklist /
        track count). Any real token difference in the title separates the
        albums: ``Trinchera`` and ``Trinchera Avanzada`` are DIFFERENT
        releases no matter how many tracks they share.
        """
        left, right = pairing.a, pairing.b
        keys_equal = left.album_key == right.album_key
        tracklist = compute_tracklist_similarity(pairing)
        n_a, n_b = len(left.entries), len(right.entries)
        count_ratio = min(n_a, n_b) / max(n_a, n_b) if max(n_a, n_b) else 0.0
        years_agree = (
            left.year is not None
            and right.year is not None
            and abs((left.year or 0) - (right.year or 0)) <= 1
        )
        score = round(100.0 * (0.45 * pairing.album_score + 0.40 * tracklist + 0.15 * count_ratio), 1)

        if keys_equal:
            if years_agree or tracklist >= TRACKLIST_STRONG or count_ratio >= COUNT_RATIO_MIN:
                return "MERGED", score, "mismo álbum (clave normalizada + evidencia)"
            return "REVIEW", score, "mismo nombre pero sin evidencia de año/tracklist"

        if _version_split(left, right):
            return "SEPARATE", score, "marcador de versión distinto (live/remix/EP...)"

        return "SEPARATE", score, "títulos de álbum distintos"

    # ------------------------------------------------------ track level ----
    def _plan_matched_artist(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        group_a: _ArtistGroup,
        group_b: _ArtistGroup,
        destination: Path,
        progress,
    ) -> None:
        merged = self._pair_albums(group_a, group_b)
        paired_a: set[Path] = {a.folder for a, _, _, _ in merged}
        paired_b: set[Path] = {b.folder for _, b, _, _ in merged}

        for folder_a, folder_b, score, reason in merged:
            album = AlbumVerdict(folder_a=folder_a, folder_b=folder_b, status="MERGED",
                                 match_score=score, reason=reason)
            self._plan_track_pairs(plan, artist, album, destination)
            artist.albums.append(album)
            self._plan_sidecars(plan, artist, folder_a.folder, folder_b.folder, destination)
        for album_v in self._exclusive_albums(group_a, group_b, paired_a, paired_b):
            artist.albums.append(album_v)
            if album_v.folder_a is not None:
                self._plan_exclusive_tracks(plan, artist, album_v, album_v.folder_a, destination)
                self._plan_sidecars(plan, artist, album_v.folder_a.folder, None, destination)
            elif album_v.folder_b is not None:
                self._plan_exclusive_tracks(plan, artist, album_v, album_v.folder_b, destination)
                self._plan_sidecars(plan, artist, None, album_v.folder_b.folder, destination)
        progress(0, 0, "")

    def _exclusive_albums(
        self,
        group_a: _ArtistGroup,
        group_b: _ArtistGroup,
        paired_a: set[Path],
        paired_b: set[Path],
    ):
        for folder in group_a.folders:
            if folder.folder not in paired_a:
                yield AlbumVerdict(folder_a=folder, status="EXCLUSIVE_A",
                                   reason="álbum exclusivo de A")
        for folder in group_b.folders:
            if folder.folder not in paired_b:
                yield AlbumVerdict(folder_b=folder, status="EXCLUSIVE_B",
                                   reason="álbum exclusivo de B")

    def _plan_track_pairs(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        album: AlbumVerdict,
        destination: Path,
    ) -> None:
        assert album.folder_a and album.folder_b
        config = MatchConfig.from_settings(self.settings)
        pairing = AlbumPairing(
            a=album.folder_a,
            b=album.folder_b,
            artist_score=1.0,
            album_score=album.match_score / 100.0,
            year_score=1.0,
            phase="consolidation",
        )
        context_sig_a = _signature_of(album.folder_a)
        context_sig_b = _signature_of(album.folder_b)
        ctx = album_context(context_sig_a, context_sig_b)
        pairs = pair_tracks_in_albums(pairing)
        used_a: set[int] = {id(p[0]) for p in pairs}
        used_b: set[int] = {id(p[1]) for p in pairs}
        display_album = _display_choice(
            album.folder_a.raw_album, album.folder_b.raw_album
        )

        for left, right in pairs:
            verdict = self._compare_pair(left, right, ctx, config)
            album.tracks.append(verdict)
            if verdict.result == "DIFFERENT":
                if verdict.entry_a is not None:
                    self._plan_unique_track(plan, artist, display_album, verdict.entry_a, "A",
                                            reason="versión distinta: se conservan ambas")
                if verdict.entry_b is not None:
                    self._plan_unique_track(plan, artist, display_album, verdict.entry_b, "B",
                                            reason="versión distinta: se conservan ambas")
                continue
            if verdict.result == "CONFLICT":
                self._action_review(plan, artist.display, display_album, verdict)
                continue
            keep_a = verdict.decision == "SELECT_A" or (
                verdict.entry_b is None and verdict.entry_a is not None
            )
            winner = verdict.entry_a if keep_a else verdict.entry_b
            loser = verdict.entry_b if keep_a else verdict.entry_a
            if winner is None:
                self._action_review(plan, artist.display, display_album, verdict)
                continue
            target = destination / artist.display / display_album / winner.path.name
            plan.actions.append(
                ConsolidationAction(
                    source=winner.path,
                    target=target,
                    kind="copy",
                    reason=verdict.reason or ("pista presente en ambas fuentes"),
                    decision="SELECT_B" if winner is verdict.entry_b else "SELECT_A",
                    match_score=verdict.match_score,
                    quality_score=_entry_quality(winner),
                    discarded=loser.path if loser else None,
                    artist=artist.display,
                    album=display_album,
                    track=verdict.title,
                    source_a=verdict.entry_a.path if verdict.entry_a else None,
                    source_b=verdict.entry_b.path if verdict.entry_b else None,
                    selected="B" if winner is verdict.entry_b else "A",
                    confidence=max(0.0, min(1.0, verdict.match_score / 100.0)),
                )
            )
        for leftover in album.folder_a.entries:
            if id(leftover) not in used_a:
                self._plan_unique_track(plan, artist, display_album, leftover, "A")
                album.tracks.append(_unique_verdict(leftover, "A"))
        for leftover in album.folder_b.entries:
            if id(leftover) not in used_b:
                self._plan_unique_track(plan, artist, display_album, leftover, "B")
                album.tracks.append(_unique_verdict(leftover, "B"))

    def _compare_pair(
        self,
        left: TrackEntry,
        right: TrackEntry,
        ctx,
        config: MatchConfig,
    ) -> TrackVerdict:
        """Identity via the shared Matching Engine; never by filename alone."""
        outcome = self.engine.match(left.match_track, right.match_track, context=ctx)
        score = outcome.score
        same = (outcome.same_song or outcome.classification in _SAME_OK) and not outcome.version_different
        if outcome.version_different:
            return TrackVerdict(
                title=left.title_base or left.path.stem,
                entry_a=left,
                entry_b=right,
                result="DIFFERENT",
                decision="KEEP_BOTH",
                reason=(
                    "versiones distintas "
                    f"({outcome.version_tag or 'live/remix'}): no son duplicados"
                ),
                match_score=score,
            )
        if not same or score < self.threshold:
            clearly_different = (
                outcome.classification in {"NO_MATCH", "MATCH_LOW"}
                and not outcome.requires_review
            )
            if clearly_different:
                return TrackVerdict(
                    title=left.title_base or left.path.stem,
                    entry_a=left,
                    entry_b=right,
                    result="DIFFERENT",
                    decision="KEEP_BOTH",
                    reason="pistas distintas dentro del mismo álbum",
                    match_score=score,
                )
            return TrackVerdict(
                title=left.title_base or left.path.stem,
                entry_a=left,
                entry_b=right,
                result="CONFLICT",
                reason=(
                    f"sin identidad confirmada ({score:.0f}% {outcome.classification})"
                ),
                match_score=score,
            )
        sha_a = _sha_of(left.path)
        sha_b = _sha_of(right.path)
        exact = bool(sha_a and sha_a == sha_b) or outcome.fingerprint_score >= 100.0
        # identity is the Matching Engine's job; once confirmed, a winner is
        # always chosen. Conflicts are only for DOUBTFUL identity.
        strong_identity = exact or outcome.classification == "MATCH_CONFIRMED"
        decision_engine = DecisionEngine.from_settings(self.settings)
        result = decision_engine.decide(
            [_candidate_of(left), _candidate_of(right)]
        )
        keep_a = result.winner_path == str(left.path)
        gap = abs(result.scores[str(left.path)] - result.scores[str(right.path)])
        conflicted = not strong_identity and (
            gap < CONFLICT_GAP or outcome.requires_review
        )
        if not conflicted and gap < 0.5 and not exact:
            keep_a = False
            tie_note = "calidad y metadata equivalentes; se conserva la fuente B"
            return TrackVerdict(
                title=left.title_base or left.path.stem,
                entry_a=left,
                entry_b=right,
                result="DUPLICATE",
                decision="SELECT_B",
                reason=tie_note,
                match_score=score,
            )
        if exact:
            return TrackVerdict(
                title=left.title_base or left.path.stem,
                entry_a=left,
                entry_b=right,
                result="EXACT",
                decision=f"SELECT_{'A' if keep_a else 'B'}",
                reason="audio idéntico (hash/huella); se conserva una sola copia",
                match_score=score,
            )
        if conflicted:
            return TrackVerdict(
                title=left.title_base or left.path.stem,
                entry_a=left,
                entry_b=right,
                result="CONFLICT",
                decision="REVIEW_REQUIRED",
                reason=(
                    "identidad dudosa o metadata insuficiente "
                    f"({score:.0f}% {outcome.classification})"
                ),
                match_score=score,
            )
        quality_note = _quality_reason(left, right, keep_a)
        return TrackVerdict(
            title=left.title_base or left.path.stem,
            entry_a=left,
            entry_b=right,
            result="DUPLICATE" if exact else "MATCH",
            decision=f"SELECT_{'A' if keep_a else 'B'}",
            reason=quality_note,
            match_score=score,
        )

    def _plan_unique_track(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        display_album: str,
        entry: TrackEntry,
        side: str,
        reason: str = "",
    ) -> None:
        target = plan.destination / artist.display / display_album / entry.path.name
        plan.actions.append(
            ConsolidationAction(
                source=entry.path,
                target=target,
                kind="copy",
                reason=reason or f"única en la fuente {side}",
                decision=f"COPY_FROM_{side}",
                quality_score=_entry_quality(entry),
                artist=artist.display,
                album=display_album,
                track=entry.title_base or entry.path.stem,
                source_a=entry.path if side == "A" else None,
                source_b=entry.path if side == "B" else None,
                selected=side,
            )
        )

    def _action_review(
        self,
        plan: ConsolidationPlan,
        artist_display: str,
        display_album: str,
        verdict: TrackVerdict,
    ) -> None:
        base = destination_of(plan) / REVIEW_DIR / safe(artist_display) / safe(display_album)
        for side, entry in (("A", verdict.entry_a), ("B", verdict.entry_b)):
            if entry is None:
                continue
            plan.actions.append(
                ConsolidationAction(
                    source=entry.path,
                    target=base / f"{side}--{entry.path.name}",
                    kind="review",
                    reason=verdict.reason or "conflicto sin decisión automática",
                    decision="REVIEW_REQUIRED",
                    match_score=verdict.match_score,
                    quality_score=_entry_quality(entry),
                    artist=artist_display,
                    album=display_album,
                    track=verdict.title,
                    source_a=verdict.entry_a.path if verdict.entry_a else None,
                    source_b=verdict.entry_b.path if verdict.entry_b else None,
                    selected="",
                    confidence=max(0.0, min(1.0, verdict.match_score / 100.0)),
                )
            )

    # ---------------------------------------------------- exclusive side ---
    def _plan_exclusive_side(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        group_a: _ArtistGroup | None,
        group_b: _ArtistGroup | None,
        destination: Path,
        side: str,
    ) -> None:
        groups = [g for g in (group_a, group_b) if g]
        for group in groups:
            for folder in group.folders:
                album = AlbumVerdict(
                    folder_a=folder if side == "A" else None,
                    folder_b=folder if side == "B" else None,
                    status=f"EXCLUSIVE_{side}",
                    reason=f"artista/álbum exclusivo de {side}",
                )
                artist.albums.append(album)
                self._plan_exclusive_tracks(plan, artist, album, folder, destination)
                self._plan_sidecars(
                    plan, artist, folder.folder if side == "A" else None,
                    folder.folder if side == "B" else None, destination,
                )

    def _plan_exclusive_tracks(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        album: AlbumVerdict,
        folder: AlbumFolder,
        destination: Path,
    ) -> None:
        side = "A" if album.folder_a else "B"
        for entry in folder.entries:
            album.tracks.append(_unique_verdict(entry, side))
            self._plan_unique_track(plan, artist, folder.raw_album, entry, side)

    # ---------------------------------------------------------- sidecars ---
    def _plan_sidecars(
        self,
        plan: ConsolidationPlan,
        artist: ArtistVerdict,
        folder_a: Path | None,
        folder_b: Path | None,
        destination: Path,
    ) -> None:
        """Covers and any non-audio companions consolidated by content."""
        audio_exts = {
            ext.lower() for ext in self.settings.get("scan.include_extensions", []) or []
        } or None
        from musicfusion.audio.metadata import AUDIO_EXTENSIONS

        exts = audio_exts or AUDIO_EXTENSIONS
        sidecars_a = _sidecars(folder_a, exts) if folder_a else []
        sidecars_b = _sidecars(folder_b, exts) if folder_b else []
        album_display = _display_choice(
            folder_a.name if folder_a else None, folder_b.name if folder_b else None
        )
        target_dir = destination / artist.display / album_display
        by_key_a = {_key_name(p): p for p in sidecars_a}
        by_key_b = {_key_name(p): p for p in sidecars_b}
        for key in sorted(set(by_key_a) | set(by_key_b)):
            pa, pb = by_key_a.get(key), by_key_b.get(key)
            if pa and pb:
                ha, hb = _sha_of(pa), _sha_of(pb)
                if ha and ha == hb:
                    winner, loser, why = (_display_choice_file(pa, pb), None, "archivo idéntico en A y B")
                else:
                    winner, loser, why = _better_asset(pa, pb)
                selected = "B" if (pb is not None and winner is pb) else "A"
                plan.actions.append(
                    ConsolidationAction(
                        source=winner,
                        target=target_dir / winner.name,
                        kind="copy",
                        reason=why,
                        decision=f"SELECT_{selected}",
                        discarded=loser,
                        artist=artist.display,
                        album=album_display,
                        track=winner.name,
                        source_a=pa,
                        source_b=pb,
                        selected=selected,
                    )
                )
                continue
            solo = pa or pb
            if solo is None:
                continue
            side = "A" if pa else "B"
            plan.actions.append(
                ConsolidationAction(
                    source=solo,
                    target=target_dir / solo.name,
                    kind="copy",
                    reason=f"archivo exclusivo de {side}",
                    decision=f"COPY_FROM_{side}",
                    artist=artist.display,
                    album=album_display,
                    track=solo.name,
                    source_a=pa,
                    source_b=pb,
                    selected=side,
                )
            )

    # ------------------------------------------------------------ execute --
    def execute(
        self,
        plan: ConsolidationPlan,
        *,
        dry_run: bool = True,
        assume_yes: bool = False,
        overwrite: bool = False,
        on_progress=None,
    ) -> tuple[Any, VerificationResult]:
        """Validate -> execute (copy only) -> verify. Sources stay untouched."""
        errors = _validate_plan(plan)
        if errors:
            raise ValueError("Plan inválido: " + "; ".join(errors))
        if plan.destination.exists() and not overwrite:
            raise ValueError(
                f"El destino ya existe: {plan.destination}. "
                "Usa overwrite=True (--overwrite) para continuar sin borrar nada."
            )
        if dry_run:
            return None, VerificationResult()
        if not assume_yes:
            answer = input(f"¿Consolidar en {plan.destination}? (si/no): ")
            if answer.strip().lower() not in {"si", "sí", "s", "y", "yes"}:
                raise KeyboardInterrupt("Cancelado por el usuario")
        before = _source_manifest((plan.lib_a, plan.lib_b))
        executor = FusionExecutor(self.fusion.db)
        result = executor.execute(
            _as_fusion_plan(plan), dry_run=False, on_progress=on_progress
        )
        verification = verify_execution(plan)
        after = _source_manifest((plan.lib_a, plan.lib_b))
        verification.sources_checked = len(after)
        for path, stamp in before.items():
            if after.get(path) != stamp:
                verification.sources_modified.append(path)
        for path in after.keys() - before.keys():
            verification.sources_modified.append(f"NUEVO en fuente: {path}")
        _write_log(plan, result, verification)
        return result, verification


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


@dataclass
class _ArtistGroup:
    raw_artist: str
    artist_key: str
    folders: list[AlbumFolder]


def _group_by_artist(folders: list[AlbumFolder]) -> dict[str, _ArtistGroup]:
    from musicfusion.matching.normalize import normalize_music_text

    groups: dict[str, _ArtistGroup] = {}
    for folder in folders:
        key = normalize_music_text(folder.artist_key or folder.raw_artist)
        group = groups.get(key)
        if group is None:
            group = _ArtistGroup(raw_artist=folder.raw_artist, artist_key=key, folders=[])
            groups[key] = group
        group.folders.append(folder)
    return groups


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return float(_fuzz.token_set_ratio(a, b)) / 100.0


def _version_split(left: AlbumFolder, right: AlbumFolder) -> bool:
    from musicfusion.matching.normalize import text_profile

    va = frozenset(text_profile(left.raw_album).version_tokens)
    vb = frozenset(text_profile(right.raw_album).version_tokens)
    return bool(va ^ vb)


def _signature_of(folder: AlbumFolder):
    members = [
        member_from_track(
            title=e.title_base or e.path.stem,
            artist=folder.artist_key,
            disc_no=e.disc_no,
            track_no=e.track_no,
            duration_ms=e.duration_ms,
            path=str(e.path),
        )
        for e in folder.entries
    ]
    return album_signature(
        title=folder.raw_album,
        artist=folder.raw_artist,
        year=folder.year,
        total_tracks=len(folder.entries),
        members=members,
    )


_sha_cache: dict[str, str] = {}


def _sha_of(path: Path) -> str:
    spath = str(path)
    cached = _sha_cache.get(spath)
    if cached is not None:
        return cached
    digest = ""
    try:
        digest = hash_file(path) or ""
    except OSError as exc:
        logger.debug("hash failed for %s: %s", path, exc)
    _sha_cache[spath] = digest
    return digest


def _candidate_of(entry: TrackEntry) -> Candidate:
    from musicfusion.audio.metadata import read_metadata
    from musicfusion.audio.quality import probe_quality

    info = read_metadata(entry.path)
    quality = probe_quality(entry.path)
    fields = [info.artist, info.album, info.title, info.year] if info else []
    richness = 100.0 * sum(1 for v in fields if v not in ("", None)) / max(1, len(fields))
    codec = ""
    if quality and quality.codec:
        codec = quality.codec
    elif entry.path.suffix:
        ext = entry.path.suffix.lower().lstrip(".")
        codec = {"m4a": "aac", "wav": "pcm_s16le"}.get(ext, ext)
    return Candidate(
        path=str(entry.path),
        quality_score=quality.score() if quality else 0.0,
        metadata_score=round(richness, 1),
        has_embedded_art=bool(info.has_embedded_art) if info else False,
        codec=codec,
        bit_depth=quality.bit_depth if quality else None,
        sample_rate_hz=quality.sample_rate_hz if quality else None,
        bitrate_kbps=quality.bitrate_kbps if quality else None,
    )


def _entry_quality(entry: TrackEntry) -> float:
    from musicfusion.audio.quality import probe_quality

    quality = probe_quality(entry.path)
    return quality.score() if quality else 0.0


def _quality_reason(left: TrackEntry, right: TrackEntry, keep_a: bool) -> str:
    qa, qb = _candidate_of(left), _candidate_of(right)
    winner, runner = (qa, qb) if keep_a else (qb, qa)
    edges: list[str] = []
    if winner.quality_score > runner.quality_score:
        edges.append("mejor calidad")
    if winner.metadata_score > runner.metadata_score:
        edges.append("mejor metadata")
    if winner.has_embedded_art and not runner.has_embedded_art:
        edges.append("artwork embebido")
    if winner.codec and winner.codec != runner.codec:
        edges.append(f"códec {winner.codec}")
    return "; ".join(edges) or "decisión del Decision Engine"


def _unique_verdict(entry: TrackEntry, side: str) -> TrackVerdict:
    return TrackVerdict(
        title=entry.title_base or entry.path.stem,
        entry_a=entry if side == "A" else None,
        entry_b=entry if side == "B" else None,
        result="UNIQUE",
        decision=f"COPY_FROM_{side}",
        reason=f"solo existe en la fuente {side}",
    )


def _key_name(path: Path) -> str:
    from musicfusion.core.text import structure_key

    return structure_key([path.stem])


def _display_choice_file(pa: Path, pb: Path) -> Path:
    if pa.suffix.lower() in IMAGE_EXTENSIONS:
        area_a, area_b = _image_area(pa), _image_area(pb)
        if area_a != area_b:
            return pa if area_a > area_b else pb
    return pa if pa.stat().st_size >= pb.stat().st_size else pb


def _better_asset(pa: Path, pb: Path) -> tuple[Path, Path, str]:
    winner = _display_choice_file(pa, pb)
    loser = pb if winner is pa else pa
    why = "se conserva el cover/mejor imagen (mayor resolución o tamaño)"
    if pa.suffix.lower() not in IMAGE_EXTENSIONS:
        why = "contenido distinto: se conserva la versión más completa"
    return winner, loser, why


def safe(name: str) -> str:
    from musicfusion.core.paths import sanitize_filename

    return sanitize_filename(name or "desconocido")


def _as_fusion_plan(plan: ConsolidationPlan) -> FusionPlan:
    """Adapt the consolidation plan to the shared FusionExecutor format."""
    from musicfusion.merge.planner import PlanAction

    actions = [
        PlanAction(
            source=action.source,
            target=action.target,
            kind=action.kind,
            score=action.match_score,
            note=f"{action.decision}: {action.reason}",
        )
        for action in plan.actions
    ]
    return FusionPlan(
        lib_a=plan.lib_a,
        lib_b=plan.lib_b,
        destination=plan.destination,
        actions=actions,
    )


def destination_of(plan: ConsolidationPlan) -> Path:
    return plan.destination


def _validate_plan(plan: ConsolidationPlan) -> list[str]:
    errors: list[str] = []
    targets: dict[str, str] = {}
    for action in plan.actions:
        if action.kind == "skip":
            continue
        try:
            target = action.target.resolve()
            source = action.source.resolve()
        except OSError as exc:
            errors.append(f"ruta inválida {action.target}: {exc}")
            continue
        if target == source:
            continue
        owner = str(target)
        if owner in targets:
            errors.append(f"destino duplicado para {owner}: {targets[owner]} y {action.source}")
        targets[owner] = str(action.source)
        if plan.destination.resolve() not in target.parents:
            errors.append(f"destino fuera de la colección final: {target}")
    return errors


def _disambiguate_targets(plan: ConsolidationPlan) -> None:
    """Never plan two copies onto one target: rename the later one, no '(1)'."""
    seen: dict[str, str] = {}
    for action in plan.actions:
        if action.kind not in ("copy", "review"):
            continue
        key = str(action.target)
        if key not in seen:
            seen[key] = str(action.source)
            continue
        logger.warning(
            "destino duplicado %s (%s y %s); se renombra el segundo",
            key, seen[key], action.source,
        )
        side = "B" if _under_root(action.source, (plan.lib_b,)) else "A"
        target = action.target
        renamed = target.with_name(f"{target.stem} [{side}]{target.suffix}")
        counter = 2
        while str(renamed) in seen:
            renamed = target.with_name(f"{target.stem} [{side} {counter}]{target.suffix}")
            counter += 1
        action.target = renamed
        seen[str(renamed)] = str(action.source)


def _under_root(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root) for root in roots)


def _fill_stats(plan: ConsolidationPlan) -> None:
    stats = plan.stats
    for artist in plan.artists:
        for album in artist.albums:
            if album.status == "MERGED":
                stats.albums_merged += 1
            elif album.status == "EXCLUSIVE_A":
                stats.albums_only_a += 1
            elif album.status == "EXCLUSIVE_B":
                stats.albums_only_b += 1
            elif album.status == "REVIEW":
                stats.albums_review += 1
            for tv in album.tracks:
                if tv.result == "UNIQUE":
                    if tv.entry_a:
                        stats.tracks_only_a += 1
                    if tv.entry_b:
                        stats.tracks_only_b += 1
                elif tv.result == "DIFFERENT":
                    stats.versions_kept_both += 1
                elif tv.result in ("DUPLICATE", "MATCH"):
                    stats.duplicates_removed += 1
                    stats.auto_decisions += 1
                elif tv.result == "EXACT":
                    stats.duplicates_exact += 1
                    stats.duplicates_removed += 1
                    stats.auto_decisions += 1
                elif tv.result == "CONFLICT":
                    stats.conflicts += 1
                    stats.pending_decisions += 1
    reviewed = sum(1 for a in plan.actions if a.kind == "review")
    stats.pending_decisions = max(stats.pending_decisions, reviewed)
    for action in plan.actions:
        if action.kind != "copy":
            continue
        stats.files_to_destination += 1
        if action.selected == "A":
            stats.selected_a += 1
        elif action.selected == "B":
            stats.selected_b += 1


def _source_manifest(roots: tuple[Path, ...]) -> dict[str, tuple[int, int]]:
    """Read-only snapshot ``path -> (size, mtime_ns)`` of every file under roots."""
    import os

    manifest: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                    manifest[str(path)] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
    return manifest


def verify_execution(plan: ConsolidationPlan) -> VerificationResult:
    """Every planned file must exist, be readable, and be unique in its album."""
    verification = VerificationResult(expected=sum(1 for a in plan.actions if a.kind in ("copy", "review")))
    seen_targets: set[str] = set()
    seen_sources: set[str] = set()
    album_files: dict[str, list[Path]] = defaultdict(list)
    for action in plan.actions:
        if action.kind not in ("copy", "review"):
            continue
        key = str(action.target)
        if key in seen_targets:
            verification.collisions.append(key)
        seen_targets.add(key)
        if action.source.exists():
            seen_sources.add(str(action.source))
        if action.target.is_file():
            verification.verified += 1
            try:
                with action.target.open("rb") as handle:
                    handle.read(64)
            except OSError:
                verification.unreadable.append(str(action.target))
            if action.target.suffix.lower() not in IMAGE_EXTENSIONS:
                album_files[str(action.target.parent)].append(action.target)
        else:
            verification.missing.append(str(action.target))
    for folder, files in album_files.items():
        by_size: dict[int, list[Path]] = defaultdict(list)
        for path in files:
            try:
                by_size[path.stat().st_size].append(path)
            except OSError:
                continue
        for size, group in by_size.items():
            if len(group) < 2:
                continue
            digests: dict[str, Path] = {}
            for path in group:
                digest = _sha_of(path)
                if digest and digest in digests:
                    verification.accidental_duplicates.append(
                        f"{folder}: {digests[digest].name} == {path.name}"
                    )
                elif digest:
                    digests[digest] = path
    unique_expected = {str(a.source) for a in plan.actions if a.kind == "copy"}
    lost = unique_expected - seen_sources
    for source in sorted(lost):
        verification.missing.append(f"fuente desaparecida antes de copiar: {source}")
    return verification


def _write_log(plan: ConsolidationPlan, result, verification: VerificationResult) -> None:
    from musicfusion.core.paths import user_config_dir

    log_dir = user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    payload = {
        "created_at": plan.created_at.isoformat(),
        "lib_a": str(plan.lib_a),
        "lib_b": str(plan.lib_b),
        "destination": str(plan.destination),
        "execution": getattr(result, "summary", lambda: "")(),
        "verification": {
            "summary": verification.summary(),
            "ok": verification.ok,
            "missing": verification.missing,
            "collisions": verification.collisions,
            "unreadable": verification.unreadable,
            "accidental_duplicates": verification.accidental_duplicates,
            "sources_checked": verification.sources_checked,
            "sources_modified": verification.sources_modified,
        },
        "stats": plan.stats.as_dict(),
        "actions": [action.body() for action in plan.actions],
    }
    log_path = log_dir / f"consolidation-{stamp}.json"
    log_path.write_text(json_dumps(payload), encoding="utf-8")
    logger.info("Log de consolidación escrito en %s", log_path)


def json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "AlbumVerdict",
    "ArtistVerdict",
    "ConsolidationAction",
    "ConsolidationPlan",
    "ConsolidationPlanner",
    "PlanStats",
    "TrackVerdict",
    "VerificationResult",
    "verify_execution",
]

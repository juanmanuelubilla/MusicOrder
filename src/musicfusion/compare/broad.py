"""Broad ("compare everything") content comparison of two folders.

This is the *whole-content* mode of the Comparison page. While the paired
folder comparison aligns tracks by relative path, :func:`compare_all` indexes
every audio file under both roots and groups them by real content signals:

* exact **SHA-256**       -> literally the same bytes;
* **acoustic fingerprint** -> same audio content decoded from audio;
* **metadata clusters**    -> same song identity via fuzzy artist/title.

Every cross-side pair inside a group is analysed with the Matching Engine and
persisted with :func:`musicfusion.compare.report.persist_folder_compare`, so
matched pairs surface in Duplicados / Fusión through the shared database,
exactly like the paired mode. Files that never pair are reported as "solo en
A/B".
"""

from __future__ import annotations

import itertools
import logging
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from musicfusion.audio.hashing import hash_file
from musicfusion.audio.metadata import AUDIO_EXTENSIONS, TrackInfo, read_metadata
from musicfusion.audio.quality import probe_quality
from musicfusion.compare.candidates import (
    build_album_folders,
    compute_tracklist_similarity,
    make_track_entry,
    pair_album_folders,
    pair_tracks_in_albums,
)
from musicfusion.compare.report import PairRecord, persist_folder_compare
from musicfusion.config.settings import Settings
from musicfusion.db.models import Track, TrackFingerprint
from musicfusion.db.session import Database
from musicfusion.matching import MatchConfig, MatchEngine, MatchTrack
from musicfusion.matching.engine import MIN_FINGERPRINT_LEN, AlbumContext
from musicfusion.matching.normalize import primary_artist, text_profile

logger = logging.getLogger(__name__)

_SAME_OK = {"MATCH_VERY_HIGH", "MATCH_HIGH", "MATCH_CONFIRMED"}
_METADATA_CLUSTER_THRESHOLD = 0.78
_GROUP_MAX_CROSS = 24


@dataclass
class BroadEntry:
    """One indexed audio file from either side of the comparison."""

    side: str
    path: Path
    info: TrackInfo | None = None
    track: MatchTrack | None = None
    sha256: str = ""
    fingerprint: str = ""
    used: bool = False


@dataclass
class BroadResult:
    """Aggregated outcome of a broad comparison."""

    pairs: list[PairRecord] = field(default_factory=list)
    only_a: list[Path] = field(default_factory=list)
    only_b: list[Path] = field(default_factory=list)
    by_sha256: int = 0
    by_fingerprint: int = 0
    by_metadata: int = 0
    pairs_count: int = 0
    matched: int = 0
    errors: int = 0
    saved: dict[str, int] = field(default_factory=dict)
    #: files of B excluded because their artist does not exist in A (master)
    filtered_out_b: int = 0


ProgressCallback = Callable[[int, int, str], None]


def compare_all(
    settings: Settings,
    db: Database,
    root_a: Path,
    root_b: Path,
    *,
    use_fingerprints: bool = True,
    on_progress: ProgressCallback | None = None,
    restrict_b_to_master_artists: bool = False,
) -> BroadResult:
    """Compare every audio file under ``root_a`` against ``root_b``.

    Files are grouped by content and all cross-side pairs inside a group are
    analysed and persisted. When both roots point to the same folder, the
    comparison runs *inside* that single folder instead, pairing only distinct
    files so internal duplicates are detected. Returns a :class:`BroadResult`.

    With ``restrict_b_to_master_artists`` (A = master), entries of B whose
    top-level artist folder does not exist in A are excluded entirely:
    extra artists in B never enter the analysis nor any copy plan.
    """
    if root_a.resolve() == root_b.resolve():
        return _compare_internal(
            settings,
            db,
            root_a,
            use_fingerprints=use_fingerprints,
            on_progress=on_progress,
        )

    config = MatchConfig.from_settings(settings)
    engine = MatchEngine(config)
    extensions = _audio_extensions(settings)
    sha_map, fp_map = _existing_signatures(db)

    entries: list[BroadEntry] = []
    entries += _index_entries(root_a, "a", extensions, sha_map, fp_map, use_fingerprints)
    entries_b = _index_entries(root_b, "b", extensions, sha_map, fp_map, use_fingerprints)
    filtered_out_b = 0
    if restrict_b_to_master_artists:
        from musicfusion.matching.normalize import normalize_music_text

        def artist_of(entry: BroadEntry) -> str:
            try:
                return normalize_music_text(entry.path.relative_to(root_b).parts[0])
            except (ValueError, IndexError):
                return ""

        artists_a = {
            normalize_music_text(e.path.relative_to(root_a).parts[0]) for e in entries
        }
        kept_b = [e for e in entries_b if artist_of(e) in artists_a]
        filtered_out_b = len(entries_b) - len(kept_b)
        entries_b = kept_b
    entries += entries_b

    total = max(1, len(entries))
    if on_progress:
        on_progress(0, total, f"Indexando {len(entries)} archivos de audio…")

    result = BroadResult()
    result.filtered_out_b = filtered_out_b
    groups = _content_groups(entries)
    analysed: set[frozenset[str]] = set()

    processed = 0
    for index, (kind, members) in enumerate(groups, start=1):
        if on_progress:
            on_progress(processed, total, f"Grupo {index}/{len(groups)} ({kind})…")
        left = [entry for entry in members if entry.side == "a"]
        right = [entry for entry in members if entry.side == "b"]
        if not left or not right:
            continue
        for entry_a, entry_b in _pick_cross_pairs(left, right):
            entry_a.used = True
            entry_b.used = True
            processed += 1
            analysed.add(frozenset((str(entry_a.path), str(entry_b.path))))
            track_a, track_b = entry_a.track, entry_b.track
            if track_a is None or track_b is None:
                continue
            try:
                outcome = engine.match(track_a, track_b)
            except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
                result.errors += 1
                logger.warning(
                    "broad pair failed: %s vs %s: %s", entry_a.path, entry_b.path, exc, exc_info=exc
                )
                continue
            same = _is_duplicate(outcome)

            result.pairs_count += 1
            if same:
                result.matched += 1
                _bump_category(result, kind)
            result.pairs.append(_make_pair_record(settings, entry_a, entry_b, outcome, same))

    # album-context candidate generation (phases 1-5): pairs the content
    # groups missed - fuzzy artists/albums, moved track numbers, retagged files
    entries_a = [e for e in entries if e.side == "a"]
    entries_b = [e for e in entries if e.side == "b"]
    for entry_a, entry_b, context in _candidate_track_pairs(
        config, entries_a, entries_b, exclude=analysed
    ):
        entry_a.used = True
        entry_b.used = True
        track_a, track_b = entry_a.track, entry_b.track
        if track_a is None or track_b is None:
            continue
        try:
            outcome = engine.match(track_a, track_b, context=context)
        except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
            result.errors += 1
            logger.warning(
                "broad pair failed: %s vs %s: %s", entry_a.path, entry_b.path, exc, exc_info=exc
            )
            continue
        same = _is_duplicate(outcome)
        outcome.candidate_phase = getattr(context, "phase", "") or "album_context"
        result.pairs_count += 1
        if same:
            result.matched += 1
            _bump_category(result, "metadata")
        result.pairs.append(_make_pair_record(settings, entry_a, entry_b, outcome, same))

    result.only_a = sorted(entry.path for entry in entries if entry.side == "a" and not entry.used)
    result.only_b = sorted(entry.path for entry in entries if entry.side == "b" and not entry.used)

    if result.pairs:
        if on_progress:
            on_progress(total, total, "Guardando resultados…")
        result.saved = persist_folder_compare(settings, db, [root_a, root_b], result.pairs)
    return result


def _compare_internal(
    settings: Settings,
    db: Database,
    root: Path,
    *,
    use_fingerprints: bool,
    on_progress: ProgressCallback | None = None,
) -> BroadResult:
    """Detect duplicates *inside* a single folder.

    Files are grouped by real content signals (SHA-256, acoustic fingerprint
    and metadata clusters) and every pair of *distinct* files inside a group is
    analysed and persisted - a file is never paired with itself.
    """
    config = MatchConfig.from_settings(settings)
    engine = MatchEngine(config)
    extensions = _audio_extensions(settings)
    sha_map, fp_map = _existing_signatures(db)

    entries = _index_entries(root, "a", extensions, sha_map, fp_map, use_fingerprints)

    total = max(1, len(entries))
    if on_progress:
        on_progress(0, total, f"Indexando {len(entries)} archivos de audio…")

    result = BroadResult()
    analysed: set[frozenset[str]] = set()
    groups = [
        (kind, _pick_internal_pairs(members))
        for kind, members in _content_groups(entries)
        if len(members) >= 2
    ]
    for kind, pairs in groups:
        for entry_a, entry_b in pairs:
            analysed.add(frozenset((str(entry_a.path), str(entry_b.path))))
    _analyse_pairs(settings, db, engine, result, groups, on_progress, total)

    # album-context candidates INSIDE one tree: e.g. Babasonicos/Pasto vs
    # Babasónicos/Pasto living side by side - different folders, same music
    for entry_a, entry_b, context in _candidate_track_pairs(config, entries, None, exclude=analysed):
        entry_a.used = True
        entry_b.used = True
        track_a, track_b = entry_a.track, entry_b.track
        if track_a is None or track_b is None:
            continue
        try:
            outcome = engine.match(track_a, track_b, context=context)
        except Exception as exc:  # noqa: BLE001
            result.errors += 1
            logger.warning("broad pair failed: %s vs %s: %s", entry_a.path, entry_b.path, exc)
            continue
        same = _is_duplicate(outcome)
        outcome.candidate_phase = getattr(context, "phase", "") or "album_context"
        result.pairs_count += 1
        if same:
            result.matched += 1
            _bump_category(result, "metadata")
        result.pairs.append(_make_pair_record(settings, entry_a, entry_b, outcome, same))

    if on_progress:
        on_progress(total, total, "Guardando resultados…")
    if result.pairs:
        result.saved = persist_folder_compare(settings, db, [root], result.pairs)
    return result


def _analyse_pairs(
    settings: Settings,
    db: Database,
    engine: MatchEngine,
    result: BroadResult,
    groups: list[tuple[str, list[tuple[BroadEntry, BroadEntry]]]],
    on_progress: ProgressCallback | None,
    total: int,
) -> None:
    """Analyse every pair and fill ``result``."""
    processed = 0
    for index, (kind, pairs) in enumerate(groups, start=1):
        if on_progress:
            on_progress(processed, total, f"Grupo {index}/{len(groups)} ({kind})…")
        for entry_a, entry_b in pairs:
            entry_a.used = True
            entry_b.used = True
            processed += 1
            track_a, track_b = entry_a.track, entry_b.track
            if track_a is None or track_b is None:
                continue
            try:
                outcome = engine.match(track_a, track_b)
            except Exception as exc:  # noqa: BLE001 - one bad pair must not abort
                result.errors += 1
                logger.warning(
                    "broad pair failed: %s vs %s: %s", entry_a.path, entry_b.path, exc, exc_info=exc
                )
                continue
            same = _is_duplicate(outcome)
            result.pairs_count += 1
            if same:
                result.matched += 1
                _bump_category(result, kind)
            result.pairs.append(_make_pair_record(settings, entry_a, entry_b, outcome, same))


def _is_duplicate(outcome) -> bool:
    """A pair is a mergeable duplicate unless a version difference separates it.

    ``version_different`` covers both explicit markers ("Live", "Remix"...) and
    the duration-gap detection, so a live take is never merged with its studio
    master even when the tags are identical.
    """
    if getattr(outcome, "version_different", False):
        return False
    return bool(outcome.same_song) or outcome.classification in _SAME_OK


def _candidate_track_pairs(config, entries_a, entries_b, *, exclude):
    """Album-context candidate pairs (phases 1-5) not analysed before.

    ``entries_b=None`` means "inside one tree": candidate albums are searched
    among DIFFERENT folders of the same list (never a folder with itself).
    Yields ``(BroadEntry, BroadEntry, AlbumContext-with-phase)``.
    """
    from musicfusion.compare.candidates import TrackEntry  # noqa: F401 (typing clarity)

    def to_entry(broad_entry):
        if broad_entry.track is None:
            return None
        return make_track_entry(broad_entry.path, broad_entry.side, broad_entry.track)

    entries_left = [e for e in (to_entry(x) for x in entries_a) if e]
    if entries_b is None:
        folders_a = build_album_folders(entries_left)
        folders_b = folders_a
    else:
        entries_right = [e for e in (to_entry(x) for x in entries_b) if e]
        folders_a = build_album_folders(entries_left)
        folders_b = build_album_folders(entries_right)

    entry_by_path: dict[str, BroadEntry] = {}
    for source in list(entries_a) + ([] if entries_b is None else list(entries_b)):
        if source.track is not None:
            entry_by_path[str(source.path)] = source

    for pairing in pair_album_folders(folders_a, folders_b, config):
        if entries_b is None and pairing.a.folder == pairing.b.folder:
            continue  # never pair an album folder with itself
        pairing.tracklist_similarity = compute_tracklist_similarity(pairing)
        context = AlbumContext(
            similarity=pairing.tracklist_similarity,
            matched_tracks=round(pairing.tracklist_similarity * max(
                len(pairing.a.entries), len(pairing.b.entries), 1)),
            total_tracks=max(len(pairing.a.entries), len(pairing.b.entries), 1),
        )
        context.phase = pairing.phase  # type: ignore[attr-defined]
        for left, right in pair_tracks_in_albums(pairing):
            broad_left = entry_by_path.get(str(left.path))
            broad_right = entry_by_path.get(str(right.path))
            if broad_left is None or broad_right is None:
                continue
            key = frozenset((str(broad_left.path), str(broad_right.path)))
            if key in exclude or str(broad_left.path) == str(broad_right.path):
                continue
            exclude.add(key)
            yield broad_left, broad_right, context


def _bump_category(result: BroadResult, kind: str) -> None:
    if kind == "sha256":
        result.by_sha256 += 1
    elif kind == "fingerprint":
        result.by_fingerprint += 1
    else:
        result.by_metadata += 1


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------


def _audio_extensions(settings: Settings) -> set[str]:
    configured = settings.get("scan.include_extensions", None)
    if configured:
        return {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in configured}
    return set(AUDIO_EXTENSIONS)


def _walk(root: Path, extensions: set[str]) -> list[Path]:
    paths: list[Path] = []
    if not root.is_dir():
        return paths
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if Path(filename).suffix.lower() in extensions:
                paths.append(Path(dirpath) / filename)
    return sorted(paths)


def _existing_signatures(db: Database) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(path -> sha256, path -> fingerprint)`` already stored."""
    sha_map: dict[str, str] = {}
    fp_map: dict[str, str] = {}
    with db.session() as session:
        path_by_id: dict[int, str] = {}
        for track in session.query(Track).yield_per(1000):
            path_by_id[track.id] = track.path
            if track.sha256:
                sha_map[track.path] = track.sha256
        for fp_row in (
            session.query(TrackFingerprint).filter_by(algorithm="chromaprint").yield_per(1000)
        ):
            if fp_row.track_id in path_by_id and fp_row.value:
                fp_map[path_by_id[fp_row.track_id]] = fp_row.value
    return sha_map, fp_map


def _index_entries(
    root: Path,
    side: str,
    extensions: set[str],
    sha_map: dict[str, str],
    fp_map: dict[str, str],
    use_fingerprints: bool,
) -> list[BroadEntry]:
    entries: list[BroadEntry] = []
    for path in _walk(root, extensions):
        info = read_metadata(path)
        if info is None:
            continue
        if info.duration_ms <= 0:
            # tags lie or are missing: prefer the REAL audio duration (spec §21)
            quality = probe_quality(path)
            if quality and quality.duration_ms > 0:
                info.duration_ms = quality.duration_ms
        spath = str(path)
        digest = sha_map.get(spath) or hash_file(path) or ""
        fingerprint = fp_map.get(spath, "")
        if not fingerprint and use_fingerprints:
            from musicfusion.audio.fingerprint import fingerprint_available, fingerprint_file

            if fingerprint_available():
                result = fingerprint_file(spath)
                if result is not None:
                    fingerprint = result.fingerprint
        entries.append(
            BroadEntry(
                side=side,
                path=path,
                info=info,
                track=_make_track(path, info, fingerprint),
                sha256=digest,
                fingerprint=fingerprint,
            )
        )
    return entries


def _make_track(path: Path, info: TrackInfo, fingerprint: str = "") -> MatchTrack:
    """Build a MatchTrack, deriving missing tags from the folder layout."""
    from musicfusion.core.text import (
        album_from_folder,
        artist_from_folder,
        title_from_filename,
        track_no_from_filename,
        year_from_folder,
    )

    artist = (info.artist or "") or (info.album_artist or "")
    album = info.album or ""
    title = info.title or ""
    track_no = info.track_no
    year = info.year
    parent = path.parent.name
    grandparent = path.parent.parent.name
    if not artist and grandparent:
        artist = artist_from_folder(grandparent)
    if not album and parent:
        album = album_from_folder(parent)
        year = year or year_from_folder(parent)
    if not title:
        title = title_from_filename(path.stem) or path.stem.lower()
        if track_no is None:
            track_no = track_no_from_filename(path.stem)
    return MatchTrack(
        path=str(path),
        artist=artist,
        album_artist=info.album_artist or "",
        album=album,
        title=title,
        track_no=track_no,
        disc_no=info.disc_no,
        total_tracks=info.total_tracks,
        year=year,
        duration_ms=info.duration_ms or 0,
        fingerprint=fingerprint,
        isrc=info.isrc or "",
        recording_id=info.recording_id or "",
    )


# ---------------------------------------------------------------------------
# content grouping
# ---------------------------------------------------------------------------

_Group = tuple[str, list[BroadEntry]]


def _content_groups(entries: list[BroadEntry]) -> list[_Group]:
    """Return content groups as ``(kind, members)`` with kinds sha256/fingerprint/metadata."""
    groups: list[_Group] = []
    assigned: set[int] = set()

    by_sha: dict[str, list[BroadEntry]] = defaultdict(list)
    for entry in entries:
        if entry.sha256:
            by_sha[entry.sha256].append(entry)
    for digest, members in by_sha.items():
        if len(members) >= 2:
            groups.append(("sha256", members))
            assigned.update(id(m) for m in members)

    by_fp: dict[str, list[BroadEntry]] = defaultdict(list)
    for entry in entries:
        if id(entry) not in assigned and entry.fingerprint:
            by_fp[entry.fingerprint].append(entry)
    for fingerprint, members in by_fp.items():
        if len(members) >= 2 and len(fingerprint) >= MIN_FINGERPRINT_LEN:
            groups.append(("fingerprint", members))
            assigned.update(id(m) for m in members)

    remaining = [entry for entry in entries if id(entry) not in assigned]
    for cluster in _metadata_clusters(remaining):
        if len(cluster) >= 2:
            groups.append(("metadata", cluster))
    return groups


def _metadata_clusters(entries: list[BroadEntry]) -> list[list[BroadEntry]]:
    """Split entries into same-song clusters by artist and fuzzy title."""
    buckets: dict[str, list[BroadEntry]] = defaultdict(list)
    for entry in entries:
        artist = ""
        if entry.info:
            artist = primary_artist(entry.info.artist or entry.info.album_artist or "")
        buckets[text_profile(artist).key].append(entry)

    clusters: list[list[BroadEntry]] = []
    for members in buckets.values():
        placed: list[list[BroadEntry]] = []
        for member in members:
            title_key = _title_base_key(member)
            target = next(
                (
                    bucket
                    for bucket in placed
                    if _sim(_title_base_key(bucket[0]), title_key) >= _METADATA_CLUSTER_THRESHOLD
                ),
                None,
            )
            if target is None:
                placed.append([member])
            else:
                target.append(member)
        clusters.extend(placed)
    return clusters


def _title_base_key(entry: BroadEntry) -> str:
    title = entry.info.title if entry.info else ""
    return text_profile((title or "") or entry.path.stem).base_key


def _sim(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return float(fuzz.token_set_ratio(left, right)) / 100.0


def _pick_cross_pairs(
    left: list[BroadEntry], right: list[BroadEntry]
) -> list[tuple[BroadEntry, BroadEntry]]:
    """All cross pairs for small groups; best-first greedy match for big ones."""
    if not left or not right:
        return []
    if len(left) * len(right) <= _GROUP_MAX_CROSS:
        return [(a, b) for a in left for b in right]
    scored = sorted(
        ((_sim(_title_base_key(a), _title_base_key(b)), a, b) for a in left for b in right),
        key=lambda item: item[0],
        reverse=True,
    )
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[BroadEntry, BroadEntry]] = []
    for _score, entry_a, entry_b in scored:
        if id(entry_a) in used_a or id(entry_b) in used_b:
            continue
        used_a.add(id(entry_a))
        used_b.add(id(entry_b))
        pairs.append((entry_a, entry_b))
    return pairs


def _pick_internal_pairs(members: list[BroadEntry]) -> list[tuple[BroadEntry, BroadEntry]]:
    """All pairs of *distinct* files inside a single folder.

    ``itertools.combinations`` guarantees a file is never compared with itself
    and each unordered pair appears once. Big groups fall back to a best-first
    greedy match to stay bounded.
    """
    if len(members) < 2:
        return []
    combos = list(itertools.combinations(members, 2))
    if len(combos) <= _GROUP_MAX_CROSS:
        return combos
    scored = sorted(
        ((_sim(_title_base_key(a), _title_base_key(b)), a, b) for a, b in combos),
        key=lambda item: item[0],
        reverse=True,
    )
    used: set[int] = set()
    pairs: list[tuple[BroadEntry, BroadEntry]] = []
    for _score, entry_a, entry_b in scored:
        if id(entry_a) in used or id(entry_b) in used:
            continue
        used.add(id(entry_a))
        used.add(id(entry_b))
        pairs.append((entry_a, entry_b))
    return pairs


# ---------------------------------------------------------------------------
# analysis + decision
# ---------------------------------------------------------------------------


def _make_pair_record(
    settings: Settings,
    entry_a: BroadEntry,
    entry_b: BroadEntry,
    outcome,
    same: bool,
) -> PairRecord:
    qual_a = qual_b = None
    decision = None
    if same:
        if entry_a.track is None or entry_b.track is None:
            return PairRecord(
                path_a=entry_a.path,
                path_b=entry_b.path,
                outcome=outcome,
                track_a=_make_track(entry_a.path, entry_a.info or TrackInfo()),
                track_b=_make_track(entry_b.path, entry_b.info or TrackInfo()),
                qual_a=None,
                qual_b=None,
                decision=None,
                same=same,
            )
        qual_a = probe_quality(entry_a.path)
        qual_b = probe_quality(entry_b.path)
        decision = _decide(settings, entry_a, entry_b, qual_a, qual_b)
    return PairRecord(
        path_a=entry_a.path,
        path_b=entry_b.path,
        outcome=outcome,
        track_a=entry_a.track
        if entry_a.track is not None
        else _make_track(entry_a.path, entry_a.info or TrackInfo()),
        track_b=entry_b.track
        if entry_b.track is not None
        else _make_track(entry_b.path, entry_b.info or TrackInfo()),
        qual_a=qual_a,
        qual_b=qual_b,
        decision=decision,
        same=same,
    )


def _decide(settings: Settings, entry_a: BroadEntry, entry_b: BroadEntry, qual_a, qual_b):
    from musicfusion.decision.engine import Candidate, DecisionEngine

    engine = DecisionEngine.from_settings(settings)
    result = engine.decide(
        [
            Candidate(
                path=str(entry_a.path),
                quality_score=qual_a.score() if qual_a else 0.0,
                metadata_score=_richness(entry_a.info),
                codec=_codec_of(entry_a.path, qual_a),
            ),
            Candidate(
                path=str(entry_b.path),
                quality_score=qual_b.score() if qual_b else 0.0,
                metadata_score=_richness(entry_b.info),
                codec=_codec_of(entry_b.path, qual_b),
            ),
        ]
    )
    result.weights = dict(engine.weights)  # type: ignore[attr-defined]
    return result


def _richness(info: TrackInfo | None) -> float:
    fields = [
        getattr(info, "artist", "") or "",
        getattr(info, "album", "") or "",
        getattr(info, "title", "") or "",
        getattr(info, "year", None),
    ]
    present = sum(1 for value in fields if value not in ("", None))
    return round(100.0 * present / len(fields), 1)


def _codec_of(path: Path, quality) -> str:
    if quality and getattr(quality, "codec", ""):
        return quality.codec
    ext = path.suffix.lower().lstrip(".")
    return {"m4a": "aac", "wav": "pcm_s16le"}.get(ext, ext)


__all__ = [
    "BroadEntry",
    "BroadResult",
    "compare_all",
]

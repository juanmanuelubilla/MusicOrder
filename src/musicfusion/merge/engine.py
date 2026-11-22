"""Core deduplication and directory fusion engine for MusicFusion.

Implements byte-level SHA-256 deduplication and acoustic signature fingerprinting
(Chromaprint / AcoustID) across multiple source directories and the destination folder,
canonical folder/file name unification (e.g. Babasonicos and Babasónicos merge into
a single canonical 'Babasónicos' folder with accents), winner selection by best audio
bitrate / quality, incremental name conflict resolution, atomic copying, and reporting.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import acoustid

from musicfusion.audio.fingerprint import fingerprint_available
from musicfusion.audio.hashing import hash_file
from musicfusion.audio.metadata import (
    AUDIO_EXTENSIONS,
    TrackInfo,
    read_metadata,
    write_metadata_tags,
)
from musicfusion.audio.quality import probe_quality
from musicfusion.core.paths import unique_path
from musicfusion.core.text import strip_accents

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1024 * 1024  # 1 MiB chunk for streaming hash reads

MEDIA_EXTENSIONS = frozenset(
    AUDIO_EXTENSIONS | {".jpg", ".jpeg", ".png", ".webp"}
)


def _richness(text: str) -> tuple[int, int, int]:
    """Score representing name quality: (accents count, upper count, length)."""
    accents = sum(1 for ch in text if ord(ch) > 127 and ch.isalpha())
    upper = sum(1 for ch in text if ch.isupper())
    return accents, upper, len(text)


def _best_display_name(a: str, b: str) -> str:
    """Pick the visually richer name (e.g. 'Babasónicos' over 'Babasonicos')."""
    if not a:
        return b
    if not b:
        return a
    if _richness(a) >= _richness(b):
        return a
    return b


def _canonical_key(name: str) -> str:
    """Folding key that treats 'Babasonicos' and 'Babasónicos' identically."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    ext = f".{name.rsplit('.', 1)[1].lower()}" if "." in name else ""
    folded = strip_accents(stem.lower()).strip()
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    return f"{folded}{ext}"


@dataclass
class FusionOptions:
    """Configuration options for a fusion run."""

    sources: list[Path] = field(default_factory=list)
    destination: Path = field(default_factory=Path)
    simulation: bool = False
    include_subdirectories: bool = True
    verify_sha256: bool = True
    preserve_structure: bool = False
    normalize_structure: bool = False
    update_tags: bool = False
    include_sidecars: bool = True
    acoustic_detection: bool = True
    acoustic_threshold: float = 0.85
    supported_extensions: set[str] | None = None  # None means MEDIA_EXTENSIONS


@dataclass
class ProgressData:
    """Real-time progress metrics emitted during processing."""

    stage: str = "init"  # "scanning" | "analyzing" | "copying" | "done" | "cancelled"
    files_found: int = 0
    files_analyzed: int = 0
    files_copied: int = 0
    duplicates_omitted: int = 0
    conflicts_resolved: int = 0
    quality_upgrades: int = 0
    errors: int = 0
    percent: float = 0.0
    speed_files_sec: float = 0.0
    speed_bytes_sec: float = 0.0
    elapsed_seconds: float = 0.0
    current_file: str = ""
    status_message: str = ""


ProgressCallback = Callable[[ProgressData], None]


@dataclass
class CopiedItem:
    """Record of a successfully planned or executed file copy."""

    source: Path
    target: Path
    sha256: str
    size: int
    bitrate_kbps: int = 0
    conflict_renamed: bool = False
    verified: bool = False


@dataclass
class DuplicateItem:
    """Record of an omitted duplicate file."""

    source: Path
    matched_target: Path
    sha256: str
    size: int
    reason: str = "mismo SHA-256"
    bitrate_kbps: int = 0


@dataclass
class ErrorItem:
    """Record of an error encountered during processing."""

    source: Path
    target: Path | None
    error_message: str


@dataclass
class FusionReport:
    """Final report summarizing the fusion execution."""

    sources: list[Path] = field(default_factory=list)
    destination: Path = field(default_factory=Path)
    simulation: bool = False
    total_found: int = 0
    unique_files: int = 0
    duplicates_detected: int = 0
    files_copied: int = 0
    conflicts_resolved: int = 0
    quality_upgrades: int = 0
    errors_count: int = 0
    total_bytes_copied: int = 0
    bytes_saved: int = 0
    elapsed_seconds: float = 0.0
    copied_items: list[CopiedItem] = field(default_factory=list)
    duplicate_items: list[DuplicateItem] = field(default_factory=list)
    error_items: list[ErrorItem] = field(default_factory=list)

    def summary(self) -> str:
        speed = (
            (self.total_bytes_copied / (1024 * 1024)) / self.elapsed_seconds
            if self.elapsed_seconds > 0
            else 0.0
        )
        mode_str = "[SIMULACIÓN] " if self.simulation else ""
        return (
            f"=== {mode_str}INFORME DE FUSIÓN MUSICFUSION ===\n"
            f"Total de archivos encontrados:    {self.total_found}\n"
            f"Archivos únicos procesados:        {self.unique_files}\n"
            f"Duplicados omitidos:               {self.duplicates_detected}\n"
            f"Archivos copiados al destino:      {self.files_copied}\n"
            f"Conflictos de nombres resueltos:   {self.conflicts_resolved}\n"
            f"Mejoras de calidad (mayor bitrate):{self.quality_upgrades}\n"
            f"Errores encontrados:               {self.errors_count}\n"
            f"Espacio total copiado:             {self.total_bytes_copied / (1024*1024):.2f} MB\n"
            f"Espacio ahorrado por duplicados:   {self.bytes_saved / (1024*1024):.2f} MB\n"
            f"Tiempo transcurrido:               {self.elapsed_seconds:.2f} s ({speed:.2f} MB/s)\n"
        )

    def to_txt(self) -> str:
        lines = [self.summary(), ""]
        if self.duplicate_items:
            lines.append("--- DETALLE DE DUPLICADOS OMITIDOS ---")
            for dup in self.duplicate_items:
                lines.append(f"ORIGEN:    {dup.source}")
                lines.append(f"CONSERVA:  {dup.matched_target}")
                br_info = f" | {dup.bitrate_kbps} kbps" if dup.bitrate_kbps else ""
                lines.append(f"MOTIVO:    {dup.reason}{br_info} ({dup.size} bytes)\n")
        if self.copied_items:
            lines.append("--- ARCHIVOS COPIADOS ---")
            for item in self.copied_items:
                renamed_str = " [RENOMBRADO POR CONFLICTO]" if item.conflict_renamed else ""
                br_info = f" [{item.bitrate_kbps} kbps]" if item.bitrate_kbps else ""
                lines.append(f"{item.source} -> {item.target}{renamed_str}{br_info}")
        if self.error_items:
            lines.append("\n--- ERRORES ---")
            for err in self.error_items:
                lines.append(f"ARCHIVO: {err.source} | ERROR: {err.error_message}")
        return "\n".join(lines)

    def export_txt(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_txt(), encoding="utf-8")

    def export_csv(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["TIPO", "ORIGEN", "DESTINO_O_CONSERVADO", "SHA256", "BYTES", "BITRATE_KBPS", "NOTA"])
            for dup in self.duplicate_items:
                writer.writerow(
                    [
                        "DUPLICADO_OMITIDO",
                        str(dup.source),
                        str(dup.matched_target),
                        dup.sha256,
                        dup.size,
                        dup.bitrate_kbps,
                        dup.reason,
                    ]
                )
            for item in self.copied_items:
                note = "Conflicto resuelto" if item.conflict_renamed else "Copia normal"
                writer.writerow(
                    [
                        "COPIADO",
                        str(item.source),
                        str(item.target),
                        item.sha256,
                        item.size,
                        item.bitrate_kbps,
                        note,
                    ]
                )
            for err in self.error_items:
                writer.writerow(
                    [
                        "ERROR",
                        str(err.source),
                        str(err.target or ""),
                        "",
                        0,
                        0,
                        err.error_message,
                    ]
                )


class FusionEngine:
    """Core processor for deduplicating, fingerprinting and merging multiple libraries."""

    def __init__(self, options: FusionOptions) -> None:
        self.options = options
        self._cancel_requested = False
        self._hash_cache: dict[Path, str] = {}
        self._destination_hashes: dict[str, Path] = {}
        self._allocated_targets: set[Path] = set()
        self._canonical_names: dict[str, str] = {}
        self._fp_cache: dict[Path, Any] = {}
        self._quality_cache: dict[Path, tuple[float, int]] = {}
        self._metadata_cache: dict[Path, TrackInfo] = {}
        # Stores: list of dicts: {"fp": (duration, fp_bits), "quality": score, "bitrate": br, "target": Path, "sha256": str, "source": Path}
        self._acoustic_registry: list[dict[str, Any]] = []

    def cancel(self) -> None:
        """Signal cooperative cancellation."""
        self._cancel_requested = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def register_canonical_name(self, name: str) -> str:
        """Register a folder/file name and return the canonical best-looking variant."""
        key = _canonical_key(name)
        if not key:
            return name
        if key not in self._canonical_names:
            self._canonical_names[key] = name
        else:
            self._canonical_names[key] = _best_display_name(self._canonical_names[key], name)
        return self._canonical_names[key]

    def get_canonical_name(self, name: str) -> str:
        """Get the preferred canonical display name for a given folder or file."""
        key = _canonical_key(name)
        return self._canonical_names.get(key, name)

    def compute_sha256(self, path: Path) -> str | None:
        """Calculate SHA-256 digest in chunks, with caching to avoid repeated reads."""
        path_resolved = path.resolve()
        if path_resolved in self._hash_cache:
            return self._hash_cache[path_resolved]
        digest = hash_file(path_resolved, algorithm="sha256")
        if digest is not None:
            self._hash_cache[path_resolved] = digest
        return digest

    def compute_fingerprint(self, path: Path) -> Any | None:
        """Calculate acoustic fingerprint pair (duration, fp) via acoustid."""
        path_resolved = path.resolve()
        if path_resolved in self._fp_cache:
            return self._fp_cache[path_resolved]
        try:
            fp_pair = acoustid.fingerprint_file(str(path_resolved))
            self._fp_cache[path_resolved] = fp_pair
            return fp_pair
        except Exception as exc:
            logger.debug("Fingerprint not generated for %s: %s", path, exc)
            self._fp_cache[path_resolved] = None
            return None

    def probe_file_quality(self, path: Path) -> tuple[float, int]:
        """Return (quality_score, bitrate_kbps). Higher quality score is better."""
        path_resolved = path.resolve()
        if path_resolved in self._quality_cache:
            return self._quality_cache[path_resolved]
        score, bitrate = 0.0, 0
        try:
            q = probe_quality(path_resolved)
            if q:
                score = q.score()
                bitrate = q.bitrate_kbps or 0
        except Exception:
            pass

        # Fallback to mutagen
        if bitrate == 0:
            try:
                import mutagen

                audio = mutagen.File(str(path_resolved))
                if audio and hasattr(audio, "info") and hasattr(audio.info, "bitrate"):
                    raw_br = getattr(audio.info, "bitrate", 0)
                    if raw_br:
                        bitrate = raw_br // 1000
                        score = float(bitrate)
            except Exception:
                pass

        result = (score, bitrate)
        self._quality_cache[path_resolved] = result
        return result

    def _is_compatible(self, path: Path) -> bool:
        """Check if file matches supported audio or sidecar media extensions."""
        ext = path.suffix.lower()
        if self.options.supported_extensions is not None:
            return ext in self.options.supported_extensions
        if self.options.include_sidecars:
            return ext in MEDIA_EXTENSIONS
        return ext in AUDIO_EXTENSIONS

    def _is_audio(self, path: Path) -> bool:
        return path.suffix.lower() in AUDIO_EXTENSIONS

    def _discover_files(
        self,
        root: Path,
        include_subdirs: bool,
        on_progress: ProgressCallback | None = None,
        exclude_dir: Path | None = None,
    ) -> list[Path]:
        """Scan directory for compatible files using os.walk with live progress and directory pruning."""
        if not root.is_dir():
            return []
        found: list[Path] = []
        exclude_resolved = exclude_dir.resolve() if exclude_dir and exclude_dir.exists() else None
        ignored_names = {
            ".git", ".trash", ".trash-1000", "$recycle.bin", "system volume information",
            "lost+found", ".cache", "node_modules", ".venv", "venv", "__pycache__",
        }

        last_notify = time.monotonic()
        try:
            if include_subdirs:
                for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                    if self._cancel_requested:
                        break
                    try:
                        dir_path = Path(dirpath).resolve()
                    except OSError:
                        continue

                    # Prune destination directory if located inside the source
                    if exclude_resolved and (dir_path == exclude_resolved or exclude_resolved in dir_path.parents):
                        dirnames.clear()
                        continue

                    # Prune system/hidden directories
                    dirnames[:] = [d for d in dirnames if d.lower() not in ignored_names and not d.startswith(".")]

                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        fp = Path(dirpath) / fn
                        if self._is_compatible(fp):
                            found.append(fp)
                            now = time.monotonic()
                            if on_progress and (len(found) % 25 == 0 or now - last_notify >= 0.25):
                                last_notify = now
                                on_progress(
                                    ProgressData(
                                        stage="scanning",
                                        files_found=len(found),
                                        status_message=f"Buscando música en {root.name}: {len(found)} archivos encontrados…",
                                        current_file=f"Escaneando: {fn}",
                                    )
                                )
            else:
                try:
                    with os.scandir(root) as it:
                        for entry in it:
                            if entry.is_file() and not entry.name.startswith("."):
                                p = Path(entry.path)
                                if self._is_compatible(p):
                                    found.append(p)
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("Error traversing directory %s: %s", root, exc)

        return found

    def _index_destination(self, on_progress: ProgressCallback | None = None) -> None:
        """Index all existing files in the destination by SHA-256 and register canonical names."""
        dest = self.options.destination
        if not dest.exists():
            return

        dest_files = self._discover_files(dest, include_subdirs=True, on_progress=on_progress)
        total = len(dest_files)
        for idx, file_path in enumerate(dest_files, start=1):
            if self._cancel_requested:
                break
            try:
                rel = file_path.relative_to(dest)
                for part in rel.parts:
                    self.register_canonical_name(part)
            except ValueError:
                self.register_canonical_name(file_path.name)

            h = self.compute_sha256(file_path)
            if h:
                self._destination_hashes[h] = file_path
                self._allocated_targets.add(file_path.resolve())

                # If acoustic detection is enabled, index existing acoustic fingerprints
                if self.options.acoustic_detection and self._is_audio(file_path):
                    fp = self.compute_fingerprint(file_path)
                    if fp:
                        q_score, br = self.probe_file_quality(file_path)
                        self._acoustic_registry.append(
                            {
                                "fp": fp,
                                "quality": q_score,
                                "bitrate": br,
                                "target": file_path,
                                "sha256": h,
                                "source": file_path,
                                "is_preexisting": True,
                            }
                        )

            if on_progress and (idx % 10 == 0 or idx == total or total < 50):
                pct = (idx / total) * 100.0 if total > 0 else 0.0
                on_progress(
                    ProgressData(
                        stage="indexing_destination",
                        files_found=total,
                        files_analyzed=idx,
                        percent=pct,
                        status_message=f"Indexando destino ({idx}/{total}): {file_path.name}",
                        current_file=file_path.name,
                    )
                )

    def _extract_normalized_metadata(
        self,
        source: Path,
        source_root: Path,
    ) -> TrackInfo:
        """Extract tags from audio file, completing missing fields from folder/filename clues."""
        meta = read_metadata(source) if self._is_audio(source) else None
        if meta is not None:
            info = TrackInfo(**{k: getattr(meta, k) for k in meta.__dataclass_fields__})
        else:
            info = TrackInfo()

        try:
            rel = source.relative_to(source_root)
            parts = rel.parts
        except ValueError:
            parts = (source.name,)

        # Extract folder clues
        artist_folder = None
        album_folder = None
        if len(parts) >= 3:
            artist_folder = parts[-3]
            album_folder = parts[-2]
        elif len(parts) == 2:
            first = parts[-2]
            if re.match(r"^[\(\[]?\d{4}[\)\]]?\s*[-_]", first):
                album_folder = first
            else:
                artist_folder = first

        # Parse album folder clue for year & album name
        year_clue = None
        album_clue = None
        if album_folder:
            m_alb = re.match(r"^[\(\[]?(\d{4})[\)\]]?\s*[-_.\s]\s*(.+)$", album_folder)
            if m_alb:
                year_clue = int(m_alb.group(1))
                album_clue = m_alb.group(2).strip()
            else:
                album_clue = album_folder.strip()

        # Parse filename clue for track number & title
        stem = source.stem
        m_trk = re.match(r"^(?:(?:track|pista)\s*)?(\d{1,3})\s*[-._\s]\s*(.+)$", stem, re.IGNORECASE)
        if m_trk:
            track_clue = int(m_trk.group(1))
            title_clue = m_trk.group(2).strip()
        else:
            track_clue = None
            title_clue = stem.strip()

        # Reconcile artist
        artist = (info.artist or info.album_artist or "").strip()
        if not artist and artist_folder:
            artist = artist_folder.strip()
        if not artist:
            artist = "Artista Desconocido"
        self.register_canonical_name(artist)
        artist = self.get_canonical_name(artist)
        info.artist = artist
        if not info.album_artist:
            info.album_artist = artist

        # Reconcile year
        if info.year is None and year_clue is not None:
            info.year = year_clue

        # Reconcile album
        album = (info.album or "").strip()
        if not album and album_clue:
            album = album_clue.strip()
        if not album:
            album = "Álbum Desconocido"
        self.register_canonical_name(album)
        album = self.get_canonical_name(album)
        info.album = album

        # Reconcile track_no
        if info.track_no is None and track_clue is not None:
            info.track_no = track_clue

        # Reconcile title
        title = (info.title or "").strip()
        if not title and title_clue:
            title = title_clue.strip()
        if not title:
            title = stem.strip()
        info.title = title

        return info

    def _resolve_target_path(
        self,
        source: Path,
        source_root: Path,
        sha256: str,
    ) -> tuple[Path, bool]:
        """Determine destination path with optional normalization (Artista/Año - Álbum/00 - Título.ext)."""
        dest = self.options.destination
        if self.options.normalize_structure:
            info = self._extract_normalized_metadata(source, source_root)
            self._metadata_cache[source.resolve()] = info

            def _clean(val: str) -> str:
                c = re.sub(r'[/\\:*?"<>|]', '-', val)
                c = re.sub(r'\s+', ' ', c).strip('. ')
                return c or "unknown"

            artist_dir = _clean(info.artist or "Artista Desconocido")

            album_str = info.album or "Álbum Desconocido"
            m_pre = re.match(r"^[\(\[]?(\d{4})[\)\]]?\s*[-_.\s]\s*(.+)$", album_str)
            if m_pre:
                if not info.year:
                    info.year = int(m_pre.group(1))
                clean_album = m_pre.group(2).strip()
            else:
                clean_album = album_str.strip()

            if info.year:
                album_dir = _clean(f"{info.year} - {clean_album}")
            else:
                album_dir = _clean(clean_album)

            if not self._is_audio(source):
                file_name = self.get_canonical_name(source.name)
            else:
                ext = source.suffix
                title = info.title or source.stem
                if info.track_no is not None:
                    file_name = _clean(f"{info.track_no:02d} - {title}") + ext
                else:
                    file_name = _clean(title) + ext

            initial_target = dest / artist_dir / album_dir / file_name
        elif self.options.preserve_structure:
            try:
                rel = source.relative_to(source_root)
                canon_dirs = [self.get_canonical_name(part) for part in rel.parts[:-1]]
                canon_file = self.get_canonical_name(rel.parts[-1])
                initial_target = dest / Path(*canon_dirs) / canon_file
            except ValueError:
                initial_target = dest / self.get_canonical_name(source.name)
        else:
            initial_target = dest / self.get_canonical_name(source.name)

        target = initial_target
        conflict_renamed = False

        while True:
            target_resolved = target.resolve()
            if not target.exists() and target_resolved not in self._allocated_targets:
                break

            existing_sha = None
            if target.exists():
                existing_sha = self.compute_sha256(target)

            if existing_sha == sha256:
                return target, False

            conflict_renamed = True
            parent = target.parent
            stem, suffix = initial_target.stem, initial_target.suffix
            counter = 2
            while True:
                candidate = parent / f"{stem} ({counter}){suffix}"
                cand_res = candidate.resolve()
                if not candidate.exists() and cand_res not in self._allocated_targets:
                    target = candidate
                    break
                if candidate.exists() and self.compute_sha256(candidate) == sha256:
                    return candidate, False
                counter += 1
            break

        return target, conflict_renamed

    def _find_acoustic_match(self, fp_pair: Any) -> dict[str, Any] | None:
        """Find an existing audio entry that matches the acoustic fingerprint."""
        if not fp_pair:
            return None
        threshold = self.options.acoustic_threshold
        source_dur = fp_pair[0] if isinstance(fp_pair, (tuple, list)) else None

        for entry in self._acoustic_registry:
            existing_fp = entry.get("fp")
            if not existing_fp:
                continue
            # Duration pre-filter: identical recordings cannot differ by more than 5.0 seconds
            if source_dur is not None and isinstance(existing_fp, (tuple, list)):
                if abs(source_dur - existing_fp[0]) > 5.0:
                    continue
            try:
                similarity = acoustid.compare_fingerprints(fp_pair, existing_fp)
                if similarity >= threshold:
                    return entry
            except Exception:
                continue
        return None

    def run(self, on_progress: ProgressCallback | None = None) -> FusionReport:
        """Execute the entire deduplication, acoustic matching and fusion pipeline."""
        start_time = time.monotonic()
        report = FusionReport(
            sources=list(self.options.sources),
            destination=self.options.destination,
            simulation=self.options.simulation,
        )

        # 1. Index destination
        if on_progress:
            on_progress(ProgressData(stage="scanning", status_message="Indexando carpeta de destino…"))
        self._index_destination(on_progress=on_progress)

        if self._cancel_requested:
            report.elapsed_seconds = time.monotonic() - start_time
            return report

        # 2. Discover candidates and pre-register canonical names
        all_candidates: list[tuple[Path, Path]] = []
        for src in self.options.sources:
            if not src.exists():
                logger.warning("Source path does not exist: %s", src)
                report.error_items.append(
                    ErrorItem(source=src, target=None, error_message="Directorio de origen no existe")
                )
                continue
            if on_progress:
                on_progress(
                    ProgressData(
                        stage="scanning",
                        status_message=f"Buscando música en: {src}…",
                        current_file=f"Escaneando {src.name}",
                    )
                )
            files = self._discover_files(
                src,
                self.options.include_subdirectories,
                on_progress=on_progress,
                exclude_dir=self.options.destination,
            )
            for f in files:
                all_candidates.append((f, src))
                try:
                    rel = f.relative_to(src)
                    for part in rel.parts:
                        self.register_canonical_name(part)
                except ValueError:
                    self.register_canonical_name(f.name)

        report.total_found = len(all_candidates)
        total_files = len(all_candidates)

        if total_files == 0:
            report.elapsed_seconds = time.monotonic() - start_time
            if on_progress:
                on_progress(
                    ProgressData(
                        stage="done",
                        files_found=0,
                        percent=100.0,
                        status_message="No se encontraron archivos compatibles para procesar.",
                    )
                )
            return report

        # 3. Process each file
        bytes_copied_so_far = 0
        last_progress_time = time.monotonic()

        def _notify(current_idx: int, current_name: str, msg: str) -> None:
            nonlocal last_progress_time
            now = time.monotonic()
            elapsed = now - start_time
            if on_progress and (now - last_progress_time >= 0.05 or total_files < 50 or current_idx == total_files):
                last_progress_time = now
                speed_files = current_idx / elapsed if elapsed > 0 else 0.0
                speed_bytes = bytes_copied_so_far / elapsed if elapsed > 0 else 0.0
                percent = (current_idx / total_files) * 100.0

                on_progress(
                    ProgressData(
                        stage="processing",
                        files_found=total_files,
                        files_analyzed=current_idx,
                        files_copied=report.files_copied,
                        duplicates_omitted=report.duplicates_detected,
                        conflicts_resolved=report.conflicts_resolved,
                        quality_upgrades=report.quality_upgrades,
                        errors=report.errors_count,
                        percent=percent,
                        speed_files_sec=speed_files,
                        speed_bytes_sec=speed_bytes,
                        elapsed_seconds=elapsed,
                        current_file=current_name,
                        status_message=msg,
                    )
                )

        for idx, (source_file, source_root) in enumerate(all_candidates, start=1):
            if self._cancel_requested:
                if on_progress:
                    on_progress(
                        ProgressData(
                            stage="cancelled",
                            files_found=total_files,
                            files_analyzed=idx - 1,
                            files_copied=report.files_copied,
                            duplicates_omitted=report.duplicates_detected,
                            conflicts_resolved=report.conflicts_resolved,
                            quality_upgrades=report.quality_upgrades,
                            errors=report.errors_count,
                            status_message="Operación cancelada por el usuario.",
                        )
                    )
                break

            try:
                file_size = source_file.stat().st_size
            except OSError as exc:
                report.errors_count += 1
                report.error_items.append(
                    ErrorItem(source=source_file, target=None, error_message=f"Error leyendo archivo: {exc}")
                )
                _notify(idx, source_file.name, f"❌ Error leyendo archivo: {source_file.name}")
                continue

            # Calculate SHA-256
            sha256 = self.compute_sha256(source_file)
            if sha256 is None:
                report.errors_count += 1
                report.error_items.append(
                    ErrorItem(
                        source=source_file,
                        target=None,
                        error_message="No se pudo calcular el hash SHA-256",
                    )
                )
                _notify(idx, source_file.name, f"❌ Error calculando SHA-256: {source_file.name}")
                continue

            # Compute quality/bitrate for audio
            source_q_score, source_bitrate = (0.0, 0)
            is_audio_file = self._is_audio(source_file)
            if is_audio_file:
                source_q_score, source_bitrate = self.probe_file_quality(source_file)

            # Check Level 1: Exact SHA-256 Duplicate
            if sha256 in self._destination_hashes:
                existing_target = self._destination_hashes[sha256]
                report.duplicates_detected += 1
                report.bytes_saved += file_size
                report.duplicate_items.append(
                    DuplicateItem(
                        source=source_file,
                        matched_target=existing_target,
                        sha256=sha256,
                        size=file_size,
                        reason="mismo SHA-256 (duplicado exacto)",
                        bitrate_kbps=source_bitrate,
                    )
                )
                _notify(idx, source_file.name, f"⏭️ Omitido duplicado (SHA-256): {source_file.name}")
                continue

            # Check Level 2: Acoustic Fingerprint Duplicate (different compression / bitrate / tags)
            acoustic_match = None
            source_fp = None
            if is_audio_file and self.options.acoustic_detection:
                source_fp = self.compute_fingerprint(source_file)
                if source_fp:
                    acoustic_match = self._find_acoustic_match(source_fp)

            if acoustic_match is not None:
                # SAME SONG DETECTED BY ACOUSTIC SIGNATURE!
                existing_target = acoustic_match["target"]
                existing_quality = acoustic_match["quality"]
                existing_bitrate = acoustic_match["bitrate"]

                if source_q_score > existing_quality or (
                    source_q_score == existing_quality and source_bitrate > existing_bitrate
                ):
                    # NEW FILE HAS BETTER BITRATE / QUALITY!
                    # Replace existing lower-quality target with this higher-quality source
                    report.quality_upgrades += 1
                    upgrade_msg = (
                        f"Mejora de calidad: {source_file.name} ({source_bitrate} kbps) "
                        f"reemplaza versión anterior ({existing_bitrate} kbps)"
                    )
                    logger.info(upgrade_msg)

                    if not self.options.simulation:
                        # Re-point target path with source's extension if format differs
                        new_target = existing_target.with_suffix(source_file.suffix)
                        success = self._copy_file_safe(
                            source=source_file,
                            target=new_target,
                            expected_sha256=sha256,
                            file_size=file_size,
                            report=report,
                            conflict_renamed=False,
                        )
                        if success:
                            # If extension changed and old file exists, remove lower quality old format
                            if new_target != existing_target and existing_target.exists():
                                try:
                                    existing_target.unlink()
                                except OSError:
                                    pass

                            # Update registers
                            old_source = acoustic_match.get("source", existing_target)
                            old_sha = acoustic_match.get("sha256", "")
                            is_pre = acoustic_match.get("is_preexisting", False)
                            acoustic_match["fp"] = source_fp
                            acoustic_match["quality"] = source_q_score
                            acoustic_match["bitrate"] = source_bitrate
                            acoustic_match["target"] = new_target
                            acoustic_match["sha256"] = sha256
                            acoustic_match["source"] = source_file
                            acoustic_match["is_preexisting"] = False
                            self._destination_hashes[sha256] = new_target
                            report.duplicates_detected += 1
                            if is_pre:
                                report.files_copied += 1
                                report.unique_files += 1
                            report.duplicate_items.append(
                                DuplicateItem(
                                    source=old_source,
                                    matched_target=new_target,
                                    sha256=old_sha,
                                    size=0,
                                    reason=f"huella acústica similar: sustituido por mejor bitrate ({source_bitrate} kbps vs {existing_bitrate} kbps)",
                                    bitrate_kbps=existing_bitrate,
                                )
                            )
                    else:
                        report.duplicates_detected += 1
                        if acoustic_match.get("is_preexisting", False):
                            report.files_copied += 1
                            report.unique_files += 1
                            acoustic_match["is_preexisting"] = False
                    _notify(idx, source_file.name, f"⭐ Calidad mejorada: {source_file.name} ({source_bitrate} kbps)")
                else:
                    # EXISTING TARGET HAS BETTER OR EQUAL BITRATE -> Omit new source as lower-quality duplicate
                    report.duplicates_detected += 1
                    report.bytes_saved += file_size
                    reason_msg = (
                        f"misma huella acústica (omitido: menor/igual bitrate {source_bitrate} kbps "
                        f"vs conservado {existing_bitrate} kbps)"
                    )
                    report.duplicate_items.append(
                        DuplicateItem(
                            source=source_file,
                            matched_target=existing_target,
                            sha256=sha256,
                            size=file_size,
                            reason=reason_msg,
                            bitrate_kbps=source_bitrate,
                        )
                    )
                    _notify(idx, source_file.name, f"⏭️ Omitido duplicado acústico: {source_file.name}")
                continue

            # Level 3: Unique file -> Plan and copy
            report.unique_files += 1
            target_path, conflict_renamed = self._resolve_target_path(
                source_file, source_root, sha256
            )
            if conflict_renamed:
                report.conflicts_resolved += 1

            self._allocated_targets.add(target_path.resolve())

            if self.options.simulation:
                self._destination_hashes[sha256] = target_path
                report.files_copied += 1
                report.total_bytes_copied += file_size
                report.copied_items.append(
                    CopiedItem(
                        source=source_file,
                        target=target_path,
                        sha256=sha256,
                        size=file_size,
                        bitrate_kbps=source_bitrate,
                        conflict_renamed=conflict_renamed,
                        verified=True,
                    )
                )
                if is_audio_file and source_fp:
                    self._acoustic_registry.append(
                        {
                            "fp": source_fp,
                            "quality": source_q_score,
                            "bitrate": source_bitrate,
                            "target": target_path,
                            "sha256": sha256,
                            "source": source_file,
                        }
                    )
            else:
                success = self._copy_file_safe(
                    source=source_file,
                    target=target_path,
                    expected_sha256=sha256,
                    file_size=file_size,
                    report=report,
                    conflict_renamed=conflict_renamed,
                )
                if success:
                    self._destination_hashes[sha256] = target_path
                    report.files_copied += 1
                    report.total_bytes_copied += file_size
                    bytes_copied_so_far += file_size

                    if is_audio_file and source_fp:
                        self._acoustic_registry.append(
                            {
                                "fp": source_fp,
                                "quality": source_q_score,
                                "bitrate": source_bitrate,
                                "target": target_path,
                                "sha256": sha256,
                                "source": source_file,
                                "is_preexisting": False,
                            }
                        )

            _notify(idx, source_file.name, f"⚡ Copiando [{source_bitrate} kbps]: {source_file.name}")

            if self._cancel_requested:
                if on_progress:
                    on_progress(
                        ProgressData(
                            stage="cancelled",
                            files_found=total_files,
                            files_analyzed=idx,
                            files_copied=report.files_copied,
                            duplicates_omitted=report.duplicates_detected,
                            conflicts_resolved=report.conflicts_resolved,
                            quality_upgrades=report.quality_upgrades,
                            errors=report.errors_count,
                            status_message="Operación cancelada por el usuario.",
                        )
                    )
                break

        report.elapsed_seconds = time.monotonic() - start_time
        if on_progress:
            final_stage = "cancelled" if self._cancel_requested else "done"
            status = "Operación cancelada" if self._cancel_requested else "Fusión completada con éxito."
            on_progress(
                ProgressData(
                    stage=final_stage,
                    files_found=total_files,
                    files_analyzed=report.unique_files + report.duplicates_detected + report.errors_count,
                    files_copied=report.files_copied,
                    duplicates_omitted=report.duplicates_detected,
                    conflicts_resolved=report.conflicts_resolved,
                    quality_upgrades=report.quality_upgrades,
                    errors=report.errors_count,
                    percent=100.0,
                    elapsed_seconds=report.elapsed_seconds,
                    status_message=status,
                )
            )

        return report

    def _copy_file_safe(
        self,
        source: Path,
        target: Path,
        expected_sha256: str,
        file_size: int,
        report: FusionReport,
        conflict_renamed: bool,
    ) -> bool:
        """Perform copy using temporary file and atomic replace, with SHA-256 verification."""
        temp_target = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_name = f".{target.name}.tmp.{uuid4().hex[:8]}"
            temp_target = target.parent / temp_name

            # High-performance streaming copy with single-pass memory SHA-256 verification
            chunk_size = 2 * 1024 * 1024  # 2 MiB buffer optimal for high-throughput HDD I/O
            hasher = hashlib.sha256() if self.options.verify_sha256 else None
            with open(source, "rb") as fsrc, open(temp_target, "wb") as fdst:
                while chunk := fsrc.read(chunk_size):
                    if hasher is not None:
                        hasher.update(chunk)
                    fdst.write(chunk)

            try:
                shutil.copystat(source, temp_target)
            except OSError:
                pass

            verified = False
            if hasher is not None:
                written_sha = hasher.hexdigest()
                if written_sha != expected_sha256:
                    if temp_target.exists():
                        temp_target.unlink()
                    report.errors_count += 1
                    report.error_items.append(
                        ErrorItem(
                            source=source,
                            target=target,
                            error_message=(
                                f"Error de verificación SHA-256: esperado {expected_sha256}, "
                                f"obtenido {written_sha}"
                            ),
                        )
                    )
                    return False
                verified = True

            os.replace(temp_target, target)

            # Update tags in destination file if requested
            if self.options.update_tags and self._is_audio(target):
                info = self._metadata_cache.get(source.resolve())
                if info is not None:
                    try:
                        write_metadata_tags(target, info)
                    except Exception as err:
                        logger.warning("No se pudieron escribir tags en %s: %s", target, err)

            self._metadata_cache.pop(source.resolve(), None)

            report.copied_items.append(
                CopiedItem(
                    source=source,
                    target=target,
                    sha256=expected_sha256,
                    size=file_size,
                    conflict_renamed=conflict_renamed,
                    verified=verified,
                )
            )
            return True

        except OSError as exc:
            logger.error("Failed to copy %s to %s: %s", source, target, exc)
            if temp_target and temp_target.exists():
                try:
                    temp_target.unlink()
                except OSError:
                    pass
            report.errors_count += 1
            report.error_items.append(
                ErrorItem(
                    source=source,
                    target=target,
                    error_message=f"Error durante el copiado: {exc}",
                )
            )
            return False

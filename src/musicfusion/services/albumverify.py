"""Album verification against MusicBrainz.

Given a well-organized tree (``Artista/Año - Álbum/NN - Tema.ext``), this
module checks every album folder against the MusicBrainz release-group
database and reports:

* whether an official release with that artist+album name exists;
* its official title/year;
* its official track count, compared with the number of audio files on disk
  (so homonymous editions like ``Mucho`` vs ``Mucho +`` are told apart by
  their real track lists instead of just by name similarity).

Results are cached as JSON in the config directory (30-day TTL) to respect
MusicBrainz rate limits: repeated runs are offline and instant.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from musicfusion.audio.metadata import AUDIO_EXTENSIONS
from musicfusion.core.text import album_from_folder, year_from_folder
from musicfusion.matching.normalize import normalize_music_text

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 30.0
NAME_MATCH_THRESHOLD = 75.0


@dataclass
class AlbumVerification:
    """Outcome of checking one local album folder against MusicBrainz."""

    folder: Path
    artist: str
    album_name: str
    year: int | None
    local_tracks: int = 0
    local_titles: list[str] = field(default_factory=list)
    matched: bool = False
    mb_title: str = ""
    mb_artist: str = ""
    mb_year: int | None = None
    mb_tracks: int | None = None
    rg_id: str = ""
    official_titles: list[str] = field(default_factory=list)
    missing_tracks: list[dict[str, Any]] = field(default_factory=list)
    extra_files: list[str] = field(default_factory=list)
    complete_pct: float | None = None
    status: str = "not_found"  # ok | count_mismatch | not_found | error | no_artist
    detail: str = ""
    name_similarity: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder": str(self.folder),
            "artist": self.artist,
            "album": self.album_name,
            "year": self.year,
            "local_tracks": self.local_tracks,
            "matched": self.matched,
            "mb_title": self.mb_title,
            "mb_artist": self.mb_artist,
            "mb_year": self.mb_year,
            "mb_tracks": self.mb_tracks,
            "status": self.status,
            "detail": self.detail,
        }


class AlbumCache:
    """Tiny JSON cache for MusicBrainz album lookups (TTL in days)."""

    def __init__(self, path: Path, ttl_days: float = CACHE_TTL_DAYS) -> None:
        self.path = path
        self.ttl_seconds = ttl_days * 86400.0
        self._data: dict[str, Any] = {}
        try:
            if path.exists():
                self._data = dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("album cache unreadable (%s): %s", path, exc)
            self._data = {}

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if not isinstance(entry, dict):
            return None
        saved_at = float(entry.get("cached_at", 0) or 0)
        if time.time() - saved_at > self.ttl_seconds:
            return None
        return dict(entry.get("payload") or {})

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self._data[key] = {"cached_at": time.time(), "payload": payload}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("album cache write failed (%s): %s", self.path, exc)


def track_alignment(
    local_files: list[Path],
    official_titles: list[str],
    *,
    min_similarity: float = 62.0,
) -> dict[str, Any]:
    """Align local files against the OFFICIAL tracklist, track by track.

    The idea: MusicBrainz knows the canonical order and names; each local file
    is matched to its official counterpart by fuzzy title (accent/case-proof),
    then classified:

      OK          -> matched at the same position
      RENUMERADA  -> matched but sits in another slot
      MAL_TITULADA-> matched with low title similarity (tag/name wrong-ish)
      FALTANTE    -> official track with no local file
      EXTRA       -> local file that matches nothing (bonus/live/etc.)

    Read-only; never suggests writes on its own.
    """
    from musicfusion.core.text import (
        title_from_filename,
        track_no_from_filename,
    )
    from musicfusion.matching.normalize import (
        normalize_music_text as _norm_title,
    )

    officials = [
        {"no": index + 1, "key": _norm_title(title), "title": title}
        for index, title in enumerate(official_titles)
        if _norm_title(title)
    ]
    locals_ = []
    for path in local_files:
        raw_title = title_from_filename(path.stem) or path.stem.lower()
        locals_.append({
            "file": path.name,
            "no": track_no_from_filename(path.stem),
            "key": _norm_title(raw_title),
        })

    scored = sorted(
        (
            (float(fuzz.token_set_ratio(loc["key"], off["key"])), loc, off)
            for loc in locals_
            for off in officials
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    used_loc: set[str] = set()
    used_off: set[int] = set()
    pairs: list[dict[str, Any]] = []
    for score, loc, off in scored:
        if loc["file"] in used_loc or off["no"] in used_off:
            continue
        if score < min_similarity:
            continue
        used_loc.add(loc["file"])
        used_off.add(off["no"])
        same_slot = loc["no"] is not None and loc["no"] == off["no"]
        if same_slot:
            status = "OK"
        elif score >= 85.0:
            status = "RENUMERADA"
        else:
            status = "MAL_TITULADA"
        pairs.append({
            "status": status,
            "local": loc["file"],
            "local_no": loc["no"],
            "official_no": off["no"],
            "official_title": off["title"],
            "similarity": round(score, 1),
        })

    missing = [off for off in officials if off["no"] not in used_off]
    extra = [loc for loc in locals_ if loc["file"] not in used_loc]
    complete = round(100.0 * len(pairs) / max(len(officials), 1), 1)
    return {
        "pairs": sorted(pairs, key=lambda p: p["official_no"]),
        "missing": [{"no": m["no"], "title": m["title"]} for m in missing],
        "extra": [e["file"] for e in extra],
        "complete_pct": complete,
        "renumbered": sum(1 for p in pairs if p["status"] == "RENUMERADA"),
        "mislabeled": sum(1 for p in pairs if p["status"] == "MAL_TITULADA"),
    }


def verify_album_folder(
    folder: Path,
    client,
    cache: AlbumCache | None = None,
    extensions: set[str] | None = None,
) -> AlbumVerification:
    """Check one ``Año - Álbum`` folder against MusicBrainz."""
    from musicfusion.core.text import artist_from_folder

    extensions = {e.lower() for e in (extensions or AUDIO_EXTENSIONS)}
    artist = artist_from_folder(folder.parent.name)
    raw_name = folder.name
    album_name = album_from_folder(raw_name)
    year = year_from_folder(raw_name)

    audio = [
        p for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix.lower() in extensions
    ]
    from musicfusion.core.text import title_from_filename

    result = AlbumVerification(
        folder=folder, artist=artist, album_name=album_name or raw_name,
        year=year, local_tracks=len(audio),
        local_titles=[title_from_filename(p.stem) for p in audio],
    )
    if not artist:
        result.status = "no_artist"
        result.detail = "La carpeta madre no da un artista reconocible"
        return result

    cache_key = f"{artist.lower()}|{(album_name or raw_name).lower()}|{year or ''}"
    payload = cache.get(cache_key) if cache else None
    if payload is None:
        payload = _query_musicbrainz(client, artist, album_name or raw_name)
        if cache is not None and not payload.get("error"):
            cache.put(cache_key, payload)
    if payload.get("error"):
        result.status = "error"
        result.detail = "MusicBrainz no respondió; reintenta más tarde"
        return result
    return _finish(result, payload, exts=extensions)


def verify_album_tree(
    root: Path,
    client=None,
    cache: AlbumCache | None = None,
    extensions: set[str] | None = None,
) -> list[AlbumVerification]:
    """Verify every ``root/Artista/Álbum`` folder of a whole library tree."""
    results: list[AlbumVerification] = []
    if not root.is_dir():
        return results
    for artist_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        album_dirs = [p for p in sorted(artist_dir.iterdir()) if p.is_dir()]
        if album_dirs:
            for album_dir in album_dirs:
                results.append(verify_album_folder(album_dir, client, cache, extensions))
        else:
            # flat layout: root/Artista-Album files -> treat artist dir as album
            results.append(verify_album_folder(artist_dir, client, cache, extensions))
    return results


# ---------------------------------------------------------------------------
# shared pair/verdict helpers (used by the CLI and the GUI)
# ---------------------------------------------------------------------------

def find_similar_album_pairs(results_a, results_b=None, threshold: float = 70.0):
    """Similar-name album pairs within one tree or across two trees."""
    from rapidfuzz import fuzz

    from musicfusion.core.text import normalize_title

    def eligible(result) -> bool:
        return result.status in {"ok", "count_mismatch"}

    def similarity(left, right) -> float:
        return float(fuzz.token_set_ratio(
            normalize_title(left.album_name), normalize_title(right.album_name)
        ))

    lefts = [r for r in results_a if eligible(r)]
    pairs: list[tuple[Any, Any]] = []
    if results_b is None:
        for index, left in enumerate(lefts):
            for right in lefts[index + 1:]:
                if similarity(left, right) >= threshold:
                    pairs.append((left, right))
        return pairs
    rights = [r for r in results_b if eligible(r)]
    for left in lefts:
        for right in rights:
            if similarity(left, right) >= threshold:
                pairs.append((left, right))
    return pairs


def album_descriptor(result) -> dict[str, Any]:
    """Compact album description for AI prompts and cache keys."""
    return {
        "artist": result.artist,
        "title": result.album_name,
        "year": result.year,
        "track_count": result.local_tracks,
        "tracks": list(getattr(result, "local_titles", None) or []),
    }


def album_evidence(mb_client, wiki_client, result) -> dict[str, Any] | None:
    """Retrieve grounding facts for one album (MusicBrainz first, then Wikipedia)."""
    evidence: dict[str, Any] = {}
    if getattr(result, "matched", False) and getattr(result, "rg_id", ""):
        listing = mb_client.release_group_tracklist(result.rg_id)
        if listing:
            evidence["musicbrainz"] = {
                "title": result.mb_title,
                "year": result.mb_year,
                "count": listing.get("count"),
                "titles": listing.get("titles") or [],
            }
    if not evidence:
        summary = wiki_client.album_summary(
            result.artist, result.mb_title or result.album_name
        )
        if summary:
            evidence["wikipedia"] = summary
    return evidence or None


def ai_pair_verdicts(pairs, *, ai_client, mb_client, wiki_client, verdict_cache):
    """Grounded AI verdicts for album pairs; returns ``(l, r, verdict, cached)``."""
    from musicfusion.services.ollamalocal import (
        ai_album_relationship,
        load_verdict,
        save_verdict,
        verdict_cache_key,
    )

    output: list[tuple[Any, Any, Any, bool]] = []
    skipped_unhealthy = 0
    for left, right in pairs:
        if not ai_client.is_healthy:
            skipped_unhealthy += 1
            continue
        descriptor_left = album_descriptor(left)
        descriptor_right = album_descriptor(right)
        key = verdict_cache_key(descriptor_left, descriptor_right)
        verdict = load_verdict(verdict_cache, key)
        cached = verdict is not None
        if verdict is None:
            verdict = ai_album_relationship(
                ai_client, descriptor_left, descriptor_right,
                evidence_a=album_evidence(mb_client, wiki_client, left),
                evidence_b=album_evidence(mb_client, wiki_client, right),
            )
            save_verdict(verdict_cache, key, verdict)
            if verdict is None and not ai_client.is_healthy:
                # breaker just tripped: stop burning time on this endpoint
                logger.warning("IA desactivada por respuestas inservibles repetidas")
        output.append((left, right, verdict, cached))
    if skipped_unhealthy:
        logger.warning(
            "IA: %s pares omitidos porque Ollama dejó de responder útilmente",
            skipped_unhealthy,
        )
    return output


# ---------------------------------------------------------------------------

def _query_musicbrainz(client, artist: str, album: str) -> dict[str, Any]:
    """Remote lookup; returns a JSON-able payload for the cache."""
    groups = client.search_releasegroup(artist=artist, album=album)
    if groups is None:
        return {"error": True}
    best_score = max((g.score for g in groups), default=0)
    candidates = [g for g in groups if g.score >= min(best_score, 60)]
    ranked: list[tuple[float, Any]] = []
    for group in candidates:
        sim = max(
            fuzz.token_set_ratio(album.lower(), group.title.lower()),
            fuzz.token_set_ratio(_compact(album), _compact(group.title)),
        )
        ranked.append((sim, group))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < NAME_MATCH_THRESHOLD:
        return {"found": False}
    best = ranked[0][1]
    listing = client.release_group_tracklist(best.release_id)
    return {
        "found": True,
        "title": best.title,
        "artist": best.artist,
        "year": best.year,
        "tracks": listing.get("count") if listing else None,
        "official_titles": (listing or {}).get("titles") or [],
        "rg_id": best.release_id,
        "similarity": round(ranked[0][0], 1),
    }


def _finish(
    result: AlbumVerification,
    payload: dict[str, Any],
    *,
    exts: set[str] | None = None,
) -> AlbumVerification:
    if not payload.get("found"):
        result.detail = "Sin coincidencia en MusicBrainz"
        return result
    result.matched = True
    result.mb_title = payload.get("title", "")
    result.mb_artist = payload.get("artist", "")
    result.mb_year = payload.get("year")
    result.mb_tracks = payload.get("tracks")
    result.rg_id = payload.get("rg_id", "")
    result.official_titles = list(payload.get("official_titles") or [])
    result.name_similarity = float(payload.get("similarity", 0.0))

    # TRACK-BY-TRACK alignment against the official tracklist (the core of
    # the "compare against what that album should have" idea): tells exactly
    # which tracks are missing, extra or misnumbered - read-only.
    if result.official_titles:
        audio_files = [
            p for p in sorted(result.folder.rglob("*"))
            if p.is_file() and p.suffix.lower() in (exts or set())
        ]
        alignment = track_alignment(audio_files, result.official_titles)
        result.missing_tracks = alignment["missing"]
        result.extra_files = alignment["extra"]
        result.complete_pct = alignment["complete_pct"]

    if result.mb_tracks is None:
        result.status = "ok"
        result.detail = "Coincide el álbum; recuento oficial no disponible"
    elif result.mb_tracks == result.local_tracks and not result.missing_tracks:
        result.status = "ok"
        detail = f"{result.local_tracks}/{result.mb_tracks} temas"
        if result.complete_pct is not None:
            detail += f" · completo ({result.complete_pct:.0f}%)"
        result.detail = detail
    else:
        result.status = "count_mismatch"
        missing_note = ""
        if result.missing_tracks:
            names = ", ".join(f"'{m['title']}'" for m in result.missing_tracks[:3])
            more = f" y {len(result.missing_tracks) - 3} más" if len(result.missing_tracks) > 3 else ""
            missing_note = f" → faltan: {names}{more}"
        elif result.extra_files:
            missing_note = f" → {len(result.extra_files)} tema(s) extra (bonus/edición?)"
        result.detail = (
            f"Recuento distinto: {result.local_tracks} locales vs "
            f"{result.mb_tracks} oficiales (¿otra edición?)"
        ) + missing_note
    return result




def _compact(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


__all__ = [
    "AlbumCache",
    "AlbumVerification",
    "ai_pair_verdicts",
    "album_descriptor",
    "album_evidence",
    "compare_local_trees",
    "find_similar_album_pairs",
    "verify_album_folder",
    "verify_album_tree",
]


# ---------------------------------------------------------------------------
# OFFLINE mode: local-only album equivalence (fast, no network)
# ---------------------------------------------------------------------------

def compare_local_trees(
    root_a: Path,
    root_b: Path | None = None,
    extensions: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fast LOCAL album comparison - zero network, zero MusicBrainz.

    Groups every ``Artista/Año - Álbum`` folder of the given tree(s) by
    normalized artist + normalized album name (year stripped) plus tracklist
    similarity. Detects duplicated releases, probable editions and suspicious
    same-name/different-content folders. Less precise than MusicBrainz (no
    official tracklists) but instant and fully offline.
    """
    import re

    from musicfusion.compare.candidates import tracklists_similarity
    from musicfusion.core.text import title_from_filename

    def audio_titles(folder: Path) -> tuple[int, list[str]]:
        files = [
            p for p in sorted(folder.rglob("*"))
            if p.is_file() and p.suffix.lower() in exts
        ]
        titles = [normalize_music_text(title_from_filename(p.stem)) for p in files]
        return len(files), [t for t in titles if t]

    exts = {e.lower() for e in (extensions or AUDIO_EXTENSIONS)}
    sources: list[tuple[str, Path]] = [("A", root_a)]
    if root_b is not None:
        sources.append(("B", root_b))

    # artist_key -> list of (tree_label, raw_folder_name, Path, n_tracks, titles)
    per_artist: dict[str, list[dict[str, Any]]] = {}
    for label, root in sources:
        if not root.is_dir():
            continue
        for artist_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            artist_key = normalize_music_text(artist_dir.name)
            for album_dir in sorted(p for p in artist_dir.iterdir() if p.is_dir()):
                raw = album_dir.name
                year = None
                match = re.match(r"^\s*((?:19|20)\d{2})\s*[-–]?\s*(.*)$", raw)
                if match:
                    year = int(match.group(1))
                    stripped = match.group(2)
                else:
                    stripped = raw
                count, titles = audio_titles(album_dir)
                per_artist.setdefault(artist_key, []).append({
                    "tree": label,
                    "raw": raw,
                    "key": normalize_music_text(stripped),
                    "year": year,
                    "path": album_dir,
                    "tracks": count,
                    "titles": titles,
                })

    results: list[dict[str, Any]] = []
    for artist_key, albums in sorted(per_artist.items()):
        display_artist = albums[0]["path"].parent.name
        for index, left in enumerate(albums):
            for right in albums[index + 1:]:
                if left["key"] == right["key"]:
                    key_score = 1.0
                elif left["key"].replace(" ", "") == right["key"].replace(" ", ""):
                    key_score = 1.0
                else:
                    key_score = max(
                        float(fuzz.token_set_ratio(left["key"], right["key"])) / 100.0,
                        float(fuzz.ratio(
                            left["key"].replace(" ", ""),
                            right["key"].replace(" ", ""),
                        )) / 100.0,
                    )
                    if key_score < 0.80:
                        continue  # plainly different albums: not our business here
                similarity = tracklists_similarity(left["titles"], right["titles"])
                if key_score >= 0.999:
                    if similarity >= 0.75:
                        kind = "DUPLICADO_PROBABLE"
                        confidence = 95.0
                        detail = "mismo nombre normalizado y tracklist coincidente"
                    elif similarity < 0.45:
                        kind = "REVISAR_MISMO_NOMBRE"
                        confidence = 55.0
                        detail = "mismo nombre pero contenidos muy distintos"
                    else:
                        kind = "POSIBLE_EDICION"
                        confidence = 75.0
                        detail = "mismo álbum con contenidos parcialmente distintos (¿deluxe/live?)"
                else:
                    kind = "POSIBLE_EQUIVALENTE"
                    confidence = round(key_score * 70.0 + similarity * 30.0, 1)
                    detail = (
                        f"nombres similares ({key_score * 100:.0f}%) y "
                        f"tracklists que coinciden ({similarity * 100:.0f}%)"
                    )
                year_note = ""
                if left["year"] and right["year"] and abs(left["year"] - right["year"]) > 1:
                    year_note = f" · años {left['year']} vs {right['year']}"
                results.append({
                    "artist": display_artist,
                    "folder_a": f"[{left['tree']}] {left['raw']}",
                    "folder_b": f"[{right['tree']}] {right['raw']}",
                    "kind": kind,
                    "confidence": confidence,
                    "detail": detail + year_note,
                    "tracks": f"{left['tracks']} vs {right['tracks']} temas",
                })
    return results

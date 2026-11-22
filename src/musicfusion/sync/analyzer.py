"""Intelligent sync ANALYSIS between two music collections (read-only).

``PATH 1`` (master) is the source of truth: its names and structure are
always preserved. ``PATH 2`` (incoming) may contain new albums for artists
that already exist in the master, mixed with unknown artists.

This module ONLY analyzes and plans - it never copies, moves, renames,
deletes or retags anything. It produces a structured plan that an external
agent (e.g. Hermes) can read, reason about ambiguous cases, and feed
decisions back into :meth:`SyncPlan.resolve`.

Matching rules (deterministic first; ambiguous cases are surfaced, not guessed):

1. artist of PATH 2 vs artists of PATH 1:
   exact normalized -> ARTIST_MATCHED (~99%)
   fuzzy >= match threshold -> ARTIST_MATCHED (score-based confidence)
   fuzzy in [ambiguous_band, match) -> AMBIGUOUS_ARTIST (for Hermes)
   below -> NEW_ARTIST (action: IGNORE - nothing from it gets copied)
2. albums of a matched incoming artist vs master artist's album folders:
   normalized album key equal (year stripped) -> EXISTS
   fuzzy key + strong tracklist similarity -> EXISTS
   fuzzy key + weak tracklist -> AMBIGUOUS_ALBUM (for Hermes)
   otherwise -> NEW_ALBUM (candidate to copy under MASTER naming)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from musicfusion.audio.metadata import AUDIO_EXTENSIONS
from musicfusion.core.text import title_from_filename
from musicfusion.matching.normalize import normalize_music_text

logger = logging.getLogger(__name__)

#: confidence bands
MATCH_THRESHOLD = 0.90       # >= this fuzzy score -> confident match
AMBIGUOUS_THRESHOLD = 0.72   # >= this but < MATCH_THRESHOLD -> send to Hermes
TRACKLIST_STRONG = 0.80      # tracklist evidence that confirms an album pair
TRACKLIST_WEAK = 0.45

#: user-facing states/actions (spec: merge-collection dry-run vocabulary)
ALBUM_STATUS: dict[str, str] = {
    "EXISTS": "EXISTE",
    "NEW_ALBUM": "FALTA",
    "AMBIGUOUS_ALBUM": "POSIBLE_EQUIVALENTE",
    "IGNORE": "IGNORADO",
}
ALBUM_ACTION: dict[str, str] = {
    "EXISTS": "NO_HACER_NADA",
    "NEW_ALBUM": "COPIAR",
    "AMBIGUOUS_ALBUM": "REVIEW",
    "IGNORE": "NO_HACER_NADA",
}
ARTIST_STATUS: dict[str, str] = {
    "ARTIST_MATCHED": "EXISTE",
    "AMBIGUOUS_ARTIST": "POSIBLE_EQUIVALENTE",
    "NEW_ARTIST": "FALTA",
}

_DECISION_RE = re.compile(r"^(MATCH|EXISTS|NEW_ALBUM|NEW_ARTIST|IGNORE)$")

#: how many audio files are sampled per folder for tag-based identity
TAG_SAMPLE_LIMIT = 6


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class AlbumMatch:
    """One incoming album folder and how it relates to the master."""

    id: str
    incoming_folder: str
    master_folder: str | None
    decision: str                      # EXISTS | NEW_ALBUM | AMBIGUOUS_ALBUM
    confidence: float                  # 0..100
    reason: str
    destination: str | None            # proposed path under MASTER (POSIX)
    review_required: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """User-facing state (EXISTE / FALTA / POSIBLE_EQUIVALENTE)."""
        return ALBUM_STATUS.get(self.decision, self.decision)

    @property
    def action(self) -> str:
        """User-facing action (NO_HACER_NADA / COPIAR / REVIEW)."""
        return ALBUM_ACTION.get(self.decision, "REVIEW")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incoming_folder": self.incoming_folder,
            "master_folder": self.master_folder,
            "decision": self.decision,
            "status": self.status,
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
            "destination": self.destination,
            "review_required": self.review_required,
            "evidence": self.evidence,
        }


@dataclass
class ArtistMatch:
    """One incoming artist folder and how it relates to the master."""

    id: str
    incoming_name: str
    master_name: str | None
    decision: str                      # ARTIST_MATCHED | AMBIGUOUS_ARTIST | NEW_ARTIST
    confidence: float
    reason: str
    review_required: bool = False
    action: str = ""                   # e.g. "SYNC_ALBUMS" / "IGNORE"
    albums: list[AlbumMatch] = field(default_factory=list)
    tag_artist: str | None = None      # albumartist/artist from tags (if any)
    conflicts: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """User-facing state (EXISTE / FALTA / POSIBLE_EQUIVALENTE)."""
        return ARTIST_STATUS.get(self.decision, self.decision)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "incoming_name": self.incoming_name,
            "master_name": self.master_name,
            "decision": self.decision,
            "status": self.status,
            "confidence": self.confidence,
            "reason": self.reason,
            "review_required": self.review_required,
            "action": self.action,
            "tag_artist": self.tag_artist,
            "conflicts": list(self.conflicts),
            "albums": [a.as_dict() for a in self.albums],
        }


@dataclass
class SyncPlan:
    """Complete analysis result for ``incoming`` against ``master``."""

    master: Path
    incoming: Path
    artists: list[ArtistMatch] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        album_counts: dict[str, int] = {}
        for artist in self.artists:
            counts[artist.decision] = counts.get(artist.decision, 0) + 1
            for album in artist.albums:
                album_counts[album.decision] = album_counts.get(album.decision, 0) + 1
        return {
            "master": str(self.master),
            "incoming": str(self.incoming),
            "artists_total": len(self.artists),
            "artists_by_decision": counts,
            "albums_by_decision": album_counts,
            "new_albums_to_copy": [
                f"{artist.incoming_name}/{album.incoming_folder}"
                for artist in self.artists
                if artist.decision == "ARTIST_MATCHED"
                for album in artist.albums
                if album.decision == "NEW_ALBUM"
            ],
            "needs_review": [
                m["id"]
                for artist in self.artists
                for m in ([artist.as_dict()] if artist.review_required else [])
                + [a.as_dict() for a in artist.albums if a.review_required]
            ],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "artists": [a.as_dict() for a in self.artists],
        }

    # ------------------------------------------------------- Hermes feedback
    def resolve(self, decisions: dict[str, str]) -> dict[str, Any]:
        """Apply agent decisions for ambiguous ids (spec: Hermes layer).

        ``decisions`` maps item ids ("artist:<name>" / "album:<artist>/<folder>")
        to verdicts: MATCH/EXISTS, NEW_ALBUM, IGNORE... Unknown or invalid
        values are reported back and left untouched.
        """
        applied: list[str] = []
        rejected: list[dict[str, str]] = []
        for artist in self.artists:
            value = decisions.get(artist.id)
            if value:
                if _DECISION_RE.match(value):
                    _apply_artist_decision(artist, value)
                    applied.append(artist.id)
                else:
                    rejected.append({"id": artist.id, "value": value})
            for album in artist.albums:
                value = decisions.get(album.id)
                if value:
                    if _DECISION_RE.match(value):
                        _apply_album_decision(artist, album, value)
                        applied.append(album.id)
                    else:
                        rejected.append({"id": album.id, "value": value})
        return {"applied": applied, "rejected": rejected}


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def analyze_sync(
    master: Path | str,
    incoming: Path | str,
    *,
    settings=None,
) -> SyncPlan:
    """Compare ``incoming`` against ``master`` and produce a :class:`SyncPlan`.

    Read-only: both trees are only walked and their folder/file NAMES parsed;
    audio content is never opened and nothing is ever written.
    """
    master_path = Path(master).expanduser().resolve()
    incoming_path = Path(incoming).expanduser().resolve()

    match_threshold = 0.90
    ambiguous_threshold = 0.72
    if settings is not None:
        try:
            match_threshold = float(settings.get("sync.match_threshold", match_threshold))
            ambiguous_threshold = float(
                settings.get("sync.ambiguous_threshold", ambiguous_threshold)
            )
        except (TypeError, ValueError):
            pass

    master_artists = _list_artist_folders(master_path)
    incoming_artists = _list_artist_folders(incoming_path)
    plan = SyncPlan(master=master_path, incoming=incoming_path)

    for inc_name in sorted(incoming_artists):
        inc_dir = incoming_path / inc_name
        inc_key = normalize_music_text(inc_name)
        tag_key, conflicts = artist_tag_key(inc_dir)
        # identity priority: albumartist/artist tags first, folder name second
        effective_key = tag_key or inc_key
        if tag_key and tag_key != inc_key:
            conflicts.append(
                f"identidad por tags '{tag_key}' usada para comparar contra "
                f"carpeta '{inc_key}'"
            )

        best_name, best_score = _best_artist(effective_key, master_artists)

        artist_id = f"artist:{inc_name}"
        evidence_note = (
            f"identidad desde tags ('{tag_key}')" if tag_key else "identidad desde nombre de carpeta"
        )
        if best_score >= match_threshold:
            assert best_name is not None  # guaranteed by best_score >= threshold
            artist = ArtistMatch(
                id=artist_id,
                incoming_name=inc_name,
                master_name=best_name,
                decision="ARTIST_MATCHED",
                confidence=round(min(99.0, best_score * 100.0), 1),
                reason=(
                    "coincidencia exacta normalizada"
                    if best_score >= 0.999
                    else f"similitud de nombre {best_score * 100:.0f}%"
                )
                + f" · {evidence_note}",
                action="SYNC_ALBUMS",
                tag_artist=tag_key,
                conflicts=conflicts,
            )
            artist.albums = _analyze_albums(
                master_path / best_name, inc_dir
            )
        elif best_score >= ambiguous_threshold:
            artist = ArtistMatch(
                id=artist_id,
                incoming_name=inc_name,
                master_name=best_name,
                decision="AMBIGUOUS_ARTIST",
                confidence=round(best_score * 100.0, 1),
                reason=(
                    f"posible equivalente de '{best_name}' "
                    f"({best_score * 100:.0f}%) - requiere revisión"
                )
                + f" · {evidence_note}",
                review_required=True,
                action="ASK_HERMES",
                tag_artist=tag_key,
                conflicts=conflicts,
            )
        else:
            artist = ArtistMatch(
                id=artist_id,
                incoming_name=inc_name,
                master_name=None,
                decision="NEW_ARTIST",
                confidence=round((1.0 - best_score) * 100.0, 1),
                reason=(
                    f"sin equivalente en el maestro "
                    f"(mejor candidato '{best_name}' {best_score * 100:.0f}% < "
                    f"{ambiguous_threshold * 100:.0f}%)"
                    if best_name
                    else "el maestro no tiene artistas"
                ),
                action="IGNORE",
                tag_artist=tag_key,
                conflicts=conflicts,
            )
        plan.artists.append(artist)
    return plan


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _list_artist_folders(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]


def _sample_tags(files: list[Path], limit: int = TAG_SAMPLE_LIMIT):
    """Read tags of a few files (read-only, mutagen via read_metadata)."""
    infos = []
    from musicfusion.audio.metadata import read_metadata

    for path in files[:limit]:
        try:
            info = read_metadata(path)
        except Exception as exc:  # noqa: BLE001 - unreadable file must not abort
            logger.debug("tag read failed for %s: %s", path, exc)
            continue
        if info is not None:
            infos.append(info)
    return infos


def artist_tag_key(artist_dir: Path) -> tuple[str | None, list[str]]:
    """Tag-based artist identity of an artist tree (albumartist > artist).

    Returns ``(normalized_key_or_None, conflicts)``. A conflict is recorded
    when tags clearly contradict the folder name - reported, never acted on.
    """
    samples: list[Path] = []
    albums = _list_artist_folders(artist_dir)
    if albums:
        for album in albums[:TAG_SAMPLE_LIMIT]:
            files = _audio_files(artist_dir / album)
            if files:
                samples.append(files[0])
    else:
        samples = _audio_files(artist_dir)

    folder_key = normalize_music_text(artist_dir.name)
    tag_key: str | None = None
    conflicts: list[str] = []
    seen: set[str] = set()
    for info in _sample_tags(samples):
        raw = (info.album_artist or "").strip() or (info.artist or "").strip()
        key = normalize_music_text(raw)
        if not key:
            continue
        seen.add(key)
        if tag_key is None:
            tag_key = key
    if len(seen) > 1:
        conflicts.append(
            "tags de artista inconsistentes entre archivos: " + ", ".join(sorted(seen))
        )
    if tag_key and folder_key and _album_key_similarity(tag_key, folder_key) < 0.5:
        conflicts.append(
            f"los tags dicen '{tag_key}' pero la carpeta se llama "
            f"'{folder_key}' - revisar antes de decidir"
        )
    return tag_key, conflicts


def album_tag_identity(album_dir: Path) -> dict[str, Any]:
    """Tag evidence of one album folder (album name, year, track numbers)."""
    files = _audio_files(album_dir)
    infos = _sample_tags(files)
    album_keys = {normalize_music_text(i.album or "") for i in infos}
    album_keys.discard("")
    years = {i.year for i in infos if i.year}
    track_nos = [i.track_no for i in infos if i.track_no]
    return {
        "has_tags": bool(infos),
        "tag_album": sorted(album_keys)[0] if len(album_keys) == 1 else None,
        "tag_year": int(sorted(years)[0]) if len(years) == 1 else None,
        "track_count": len(files),
        "track_nos": sorted(track_nos),
        "titles": _audio_titles(album_dir),
    }


def _audio_titles(folder: Path) -> list[str]:
    """Normalized titles of one album folder, derived from file names."""
    titles: list[str] = []
    if not folder.is_dir():
        return titles
    for child in sorted(folder.iterdir()):
        if child.is_file() and child.suffix.lower() in AUDIO_EXTENSIONS:
            titles.append(normalize_music_text(title_from_filename(child.stem)))
    return [t for t in titles if t]


def _best_artist(incoming_key: str, master_names: list[str]) -> tuple[str | None, float]:
    best_name: str | None = None
    best_score = 0.0
    for name in master_names:
        master_key = normalize_music_text(name)
        score = _text_similarity(incoming_key, master_key)
        if score > best_score:
            best_name, best_score = name, score
    return best_name, best_score


def _text_similarity(a: str, b: str) -> float:
    """Name similarity tolerant to accents/case/separators/glued spellings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    compact_a, compact_b = a.replace(" ", ""), b.replace(" ", "")
    if compact_a == compact_b:
        return 1.0
    return max(
        float(fuzz.token_set_ratio(a, b)) / 100.0,
        float(fuzz.ratio(compact_a, compact_b)) / 100.0,
    )


def _split_year(raw: str) -> tuple[int | None, str]:
    import re

    match = re.match(r"^\s*((?:19|20)\d{2})\s*[-–]?\s*(.*)$", raw.strip())
    if not match:
        return None, raw.strip()
    return int(match.group(1)), match.group(2).strip()


def _analyze_albums(master_artist_dir: Path, incoming_artist_dir: Path) -> list[AlbumMatch]:
    """Album-level comparison for one matched artist pair."""
    master_albums = _list_artist_folders(master_artist_dir)
    master_index = [(raw, *_split_year(raw), normalize_music_text(_strip_year(raw)))
                    for raw in master_albums]
    results: list[AlbumMatch] = []

    for inc_raw in sorted(_list_artist_folders(incoming_artist_dir)):
        inc_year, _inc_rest = _split_year(inc_raw)
        folder_key = normalize_music_text(_strip_year(inc_raw))
        inc_tags = album_tag_identity(incoming_artist_dir / inc_raw)
        tag_album = inc_tags.get("tag_album")
        # album identity priority: metadata album > folder name
        inc_key = normalize_music_text(tag_album) if tag_album else folder_key
        identity_source = "tags" if tag_album else "carpeta"
        if tag_album and inc_key != folder_key:
            identity_source += f" (tag '{tag_album}' vs carpeta '{folder_key}')"
        if inc_tags.get("tag_year") and not inc_year:
            inc_year = int(inc_tags["tag_year"])
        album_id = f"album:{_parent_name(incoming_artist_dir)}/{inc_raw}"
        evidence: dict[str, Any] = {
            "year_incoming": inc_year,
            "tracks_incoming": inc_tags["track_count"],
            "identity_source": identity_source,
            "track_nos_incoming": inc_tags["track_nos"],
        }

        best: tuple[str, int | None, str, float] | None = None
        for raw, year, rest, key in master_index:
            if not inc_key or not key:
                continue
            score = _album_key_similarity(inc_key, key)
            if best is None or score > best[3]:
                best = (raw, year, key, score)

        dest_base = f"{_parent_name(master_artist_dir)}"
        if best is not None and best[3] >= 0.999:
            raw, year, _key, _score = best
            same_year = inc_year is None or year is None or abs(inc_year - year) <= 1
            similarity = tracklist_ratio(
                incoming_artist_dir / inc_raw, master_artist_dir / raw
            )
            evidence.update({
                "master_folder": raw,
                "year_master": year,
                "tracks_master": len(_audio_files(master_artist_dir / raw)),
                "tracklist_similarity": round(similarity, 3),
            })
            if similarity < TRACKLIST_WEAK and evidence["tracks_master"]:
                # identical name is NOT enough on its own (spec: never decide
                # by name alone) - contradictory tracklists mean mislabeled
                # folder or a genuinely different release -> let Hermes review
                results.append(AlbumMatch(
                    id=album_id,
                    incoming_folder=inc_raw,
                    master_folder=raw,
                    decision="AMBIGUOUS_ALBUM",
                    confidence=55.0,
                    reason=(
                        f"nombre idéntico pero tracklists muy distintas "
                        f"({similarity * 100:.0f}%): carpeta mal etiquetada u "
                        f"otra publicación - requiere revisión"
                    ),
                    destination=f"{dest_base}/{raw}",
                    review_required=True,
                    evidence=evidence,
                ))
                continue
            results.append(AlbumMatch(
                id=album_id,
                incoming_folder=inc_raw,
                master_folder=raw,
                decision="EXISTS",
                confidence=98.0 if same_year else 92.0,
                reason=(
                    "mismo álbum normalizado en el maestro"
                    if same_year
                    else "mismo álbum con año distinto (posible reedición)"
                ),
                destination=f"{dest_base}/{raw}",
                evidence=evidence,
            ))
            continue

        if best is not None and best[3] >= MATCH_THRESHOLD:
            raw, year, _key, score = best
            similarity = tracklist_ratio(
                incoming_artist_dir / inc_raw, master_artist_dir / raw
            )
            evidence.update({
                "master_folder": raw,
                "year_master": year,
                "tracks_master": len(_audio_files(master_artist_dir / raw)),
                "tracklist_similarity": round(similarity, 3),
            })
            if similarity >= TRACKLIST_STRONG:
                results.append(AlbumMatch(
                    id=album_id,
                    incoming_folder=inc_raw,
                    master_folder=raw,
                    decision="EXISTS",
                    confidence=round(min(97.0, score * 90.0 + similarity * 10.0), 1),
                    reason=f"nombre similar ({score * 100:.0f}%) y tracklist coincidente ({similarity * 100:.0f}%)",
                    destination=f"{dest_base}/{raw}",
                    evidence=evidence,
                ))
                continue
            weak = similarity < TRACKLIST_WEAK
            results.append(AlbumMatch(
                id=album_id,
                incoming_folder=inc_raw,
                master_folder=raw,
                decision="AMBIGUOUS_ALBUM" if not weak else "NEW_ALBUM",
                confidence=round(score * 70.0 + similarity * 30.0, 1),
                reason=(
                    f"nombre similar ({score * 100:.0f}%) pero tracklists muy distintas "
                    f"({similarity * 100:.0f}%): probablemente álbumes diferentes"
                    if weak
                    else f"nombre similar ({score * 100:.0f}%), tracklist intermedia "
                         f"({similarity * 100:.0f}%) - requiere revisión"
                ),
                destination=f"{dest_base}/{raw}" if not weak else f"{dest_base}/{inc_raw}",
                review_required=not weak,
                evidence=evidence,
            ))
            continue

        results.append(AlbumMatch(
            id=album_id,
            incoming_folder=inc_raw,
            master_folder=None,
            decision="NEW_ALBUM",
            confidence=99.0 if not best or best[3] < 0.5 else 85.0,
            reason=(
                "no existe un álbum equivalente en el maestro"
                if not best or best[3] < 0.5
                else f"mejor candidato '{best[0]}' solo {best[3] * 100:.0f}%"
            ),
            destination=f"{dest_base}/{inc_raw}",
            evidence={**evidence, "best_master_candidate": best[0] if best else None},
        ))
    return results


def tracklist_ratio(incoming_album: Path, master_album: Path) -> float:
    """Tracklist similarity between two album folders via shared helper."""
    from musicfusion.compare.candidates import tracklists_similarity

    return tracklists_similarity(
        _audio_titles(incoming_album), _audio_titles(master_album)
    )


def _album_key_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    compact_a, compact_b = a.replace(" ", ""), b.replace(" ", "")
    if compact_a == compact_b:
        return 1.0
    return max(
        float(fuzz.token_set_ratio(a, b)) / 100.0,
        float(fuzz.ratio(compact_a, compact_b)) / 100.0,
    )


def _strip_year(raw: str) -> str:
    import re

    return re.sub(r"^\s*(?:19|20)\d{2}\s*[-–]?\s*", "", raw.strip())


def _parent_name(path: Path) -> str:
    return path.name


def _apply_artist_decision(artist: ArtistMatch, value: str) -> None:
    if value == "MATCH":
        artist.decision = "ARTIST_MATCHED"
        artist.action = "SYNC_ALBUMS"
        artist.review_required = False
        artist.reason += " | confirmado por agente externo"
    elif value == "IGNORE":
        artist.decision = "NEW_ARTIST"
        artist.action = "IGNORE"
        artist.review_required = False
        artist.reason += " | descartado por agente externo"


def _apply_album_decision(artist: ArtistMatch, album: AlbumMatch, value: str) -> None:
    if value == "EXISTS":
        album.decision = "EXISTS"
        album.review_required = False
        album.reason += " | confirmado por agente externo"
    elif value == "NEW_ALBUM":
        album.decision = "NEW_ALBUM"
        album.master_folder = None
        album.destination = f"{artist.master_name or ''}/{album.incoming_folder}".lstrip("/")
        album.review_required = False
        album.reason += " | confirmado como nuevo por agente externo"
    elif value == "IGNORE":
        album.decision = "IGNORE"
        album.review_required = False
        album.reason += " | descartado por agente externo"


# ---------------------------------------------------------------------------
# Hermes bridge: operate on SERIALIZED plans (pure JSON in/out)
# ---------------------------------------------------------------------------

def apply_decisions(plan_dict: dict[str, Any], decisions: dict[str, str]) -> dict[str, Any]:
    """Apply agent verdicts to an already-serialized plan (as_dict()).

    This is the Hermes entry point when working through files instead of the
    Python API: read ``plan.json``, write ``decisions.json`` mapping item ids
    ("artist:X" / "album:X/Y") to verdicts, then call this (or the
    ``sync-resolve`` CLI command) to obtain the final actionable plan.

    Accepted verdicts: artist -> MATCH | IGNORE ; album -> EXISTS |
    NEW_ALBUM | IGNORE. Unknown ids or invalid values come back in
    ``rejected`` and leave the plan untouched.
    """
    applied: list[str] = []
    rejected: list[dict[str, str]] = []
    consumed: set[str] = set()

    def _valid(value: str, *, artist_level: bool) -> bool:
        allowed = ("MATCH", "IGNORE") if artist_level else ("EXISTS", "NEW_ALBUM", "IGNORE")
        return value in allowed

    for artist in plan_dict.get("artists", []):
        value = decisions.get(artist.get("id", ""))
        if value:
            consumed.add(artist.get("id", ""))
            if _valid(value, artist_level=True):
                if value == "MATCH":
                    artist["decision"] = "ARTIST_MATCHED"
                    artist["action"] = "SYNC_ALBUMS"
                else:
                    artist["decision"] = "NEW_ARTIST"
                    artist["action"] = "IGNORE"
                artist["review_required"] = False
                artist["reason"] = artist.get("reason", "") + " | decidido por agente externo"
                applied.append(artist["id"])
            else:
                rejected.append({"id": artist["id"], "value": value})
        for album in artist.get("albums", []):
            value = decisions.get(album.get("id", ""))
            if not value:
                continue
            consumed.add(album.get("id", ""))
            if _valid(value, artist_level=False):
                if value == "EXISTS":
                    album["decision"] = "EXISTS"
                elif value == "NEW_ALBUM":
                    album["decision"] = "NEW_ALBUM"
                    master_name = artist.get("master_name") or ""
                    album["master_folder"] = None
                    album["destination"] = (
                        f"{master_name}/{album['incoming_folder']}".lstrip("/")
                        if master_name else album["incoming_folder"]
                    )
                else:  # IGNORE
                    album["decision"] = "IGNORE"
                album["review_required"] = False
                album["action_label"] = {
                    "EXISTS": "NO_HACER_NADA",
                    "NEW_ALBUM": "COPIAR",
                    "IGNORE": "NO_HACER_NADA",
                }.get(album["decision"], album.get("action_label"))
                album["reason"] = album.get("reason", "") + " | decidido por agente externo"
                applied.append(album["id"])
            else:
                rejected.append({"id": album["id"], "value": value})

    # refresh top-level summaries so consumers trust the file again
    artist_counts: dict[str, int] = {}
    album_counts: dict[str, int] = {}
    new_to_copy: list[str] = []
    needs_review: list[str] = []
    for artist in plan_dict.get("artists", []):
        artist_counts[artist["decision"]] = artist_counts.get(artist["decision"], 0) + 1
        if artist.get("review_required"):
            needs_review.append(artist["id"])
        for album in artist.get("albums", []):
            decision = album["decision"]
            album_counts[decision] = album_counts.get(decision, 0) + 1
            if artist["decision"] == "ARTIST_MATCHED" and decision == "NEW_ALBUM":
                new_to_copy.append(f"{artist['incoming_name']}/{album['incoming_folder']}")
            if album.get("review_required"):
                needs_review.append(album["id"])
    for key, value in decisions.items():
        if key not in consumed:
            rejected.append({"id": key, "value": str(value), "reason": "id desconocido"})

    plan_dict["artists_by_decision"] = artist_counts
    plan_dict["albums_by_decision"] = album_counts
    plan_dict["new_albums_to_copy"] = new_to_copy
    plan_dict["needs_review"] = needs_review
    return {"applied": applied, "rejected": rejected}


__all__ = [
    "AlbumMatch",
    "ArtistMatch",
    "SyncPlan",
    "analyze_sync",
    "apply_decisions",
]

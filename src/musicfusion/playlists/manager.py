"""Playlist import and export for M3U, M3U8 and PLS.

Playlists may store relative or absolute paths. The manager resolves each entry
against the playlist directory and, when ``relative_to`` is given, rewrites the
stored paths so playlists stay portable after a library fusion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PlaylistEntry:
    """A single playlist entry."""

    path: str
    title: str = ""
    duration: int = -1


@dataclass
class Playlist:
    """Parsed playlist contents."""

    name: str
    entries: list[PlaylistEntry] = field(default_factory=list)
    extended: bool = False

    def entry_paths(self) -> list[str]:
        """Absolute path for every entry."""
        return [entry.path for entry in self.entries]


def _resolve(base: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (base.parent / candidate).resolve()


class PlaylistManager:
    """Reads and writes M3U/M3U8/PLS playlists."""

    def import_file(self, path: str | Path) -> Playlist:
        """Parse a playlist file and return a normalized :class:`Playlist`."""
        file_path = Path(path).expanduser()
        suffix = file_path.suffix.lower()
        if suffix == ".pls":
            return self._read_pls(file_path)
        return self._read_m3u(file_path)

    def _read_m3u(self, path: Path) -> Playlist:
        entries: list[PlaylistEntry] = []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        title = ""
        duration = -1
        extended = bool(lines) and lines[0].strip().upper() == "#EXTM3U"
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                payload = line[len("#EXTINF:") :].strip()
                if "," in payload:
                    duration_text, title = payload.split(",", 1)
                    try:
                        duration = int(duration_text.strip())
                    except ValueError:
                        duration = -1
                elif payload:
                    try:
                        duration = int(payload)
                    except ValueError:
                        duration = -1
                    title = ""
                continue
            if line.startswith("#"):
                continue
            absolute = _resolve(path, line)
            entries.append(
                PlaylistEntry(path=str(absolute), title=title, duration=duration)
            )
            title = ""
        return Playlist(name=path.stem, entries=entries, extended=extended)

    def _read_pls(self, path: Path) -> Playlist:
        entries: list[PlaylistEntry] = []
        count = 0
        for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
            line = line.strip()
            if line.lower().startswith("numberofentries"):
                try:
                    count = int(line.split("=", 1)[1])
                except (IndexError, ValueError):
                    count = 0
                continue
            if not line or "=" not in line or line.lower().startswith("[playlist]"):
                continue
            key, value = line.split("=", 1)
            if key.lower().startswith("file"):
                entries.append(PlaylistEntry(path=str(_resolve(path, value))))
            elif key.lower().startswith("title"):
                if entries and not entries[-1].title:
                    entries[-1].title = value
            elif key.lower().startswith("length"):
                if entries:
                    try:
                        entries[-1].duration = int(value)
                    except ValueError:
                        pass
        entries = entries[:count] if count else entries
        return Playlist(name=path.stem, entries=entries)

    def export(
        self,
        playlist: Playlist,
        path: str | Path,
        *,
        relative_to: Path | None = None,
        kind: str | None = None,
    ) -> Path:
        """Write ``playlist`` to ``path`` resolving paths portably."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        chosen = (kind or target.suffix.lower().lstrip(".")).lower()
        if chosen == "pls":
            self._write_pls(target, playlist, relative_to)
        else:
            self._write_m3u(target, playlist, relative_to, kind="m3u8" if chosen == "m3u8" else "m3u")
        logger.info("Playlist written: %s (%d entries)", target, len(playlist.entries))
        return target

    def _render_path(self, entry_path: str, relative_to: Path | None) -> str:
        path = Path(entry_path)
        if relative_to:
            try:
                return str(path.relative_to(relative_to))
            except ValueError:
                return str(path)
        return str(path)

    def _write_m3u(
        self,
        target: Path,
        playlist: Playlist,
        relative_to: Path | None,
        kind: str,
    ) -> None:
        lines: list[str] = []
        if kind == "m3u8":
            lines.append("#EXTM3U")
        for entry in playlist.entries:
            if kind == "m3u8" and (entry.title or entry.duration != -1):
                lines.append(f"#EXTINF:{entry.duration},{entry.title}")
            lines.append(self._render_path(entry.path, relative_to))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_pls(
        self, target: Path, playlist: Playlist, relative_to: Path | None
    ) -> None:
        lines = ["[playlist]"]
        for index, entry in enumerate(playlist.entries, start=1):
            lines.append(f"File{index}={self._render_path(entry.path, relative_to)}")
            if entry.title:
                lines.append(f"Title{index}={entry.title}")
            if entry.duration >= 0:
                lines.append(f"Length{index}={entry.duration}")
        lines.append(f"NumberOfEntries={len(playlist.entries)}")
        lines.append("Version=2")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_path(base: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (base.parent / candidate).resolve()

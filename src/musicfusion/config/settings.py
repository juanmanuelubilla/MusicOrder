"""Application settings backed by YAML, with profile import/export support."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Default configuration values; used when a key is missing in the YAML file.
DEFAULTS: dict[str, Any] = {
    "app": {
        "version": "0.1.0",
        "theme": "dark",
        "language": "es",
        "confirm_destructive": True,
        "max_threads": 4,
    },
    "db": {
        "path": "musicfusion.db",
        "journal_mode": "WAL",
        "synchronous": "NORMAL",
    },
    "scan": {
        "hash_algorithm": "sha256",
        "compute_fingerprints": False,
        "include_extensions": [
            ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav",
            ".wma", ".ape", ".aiff", ".aif", ".mp4", ".oga", ".webm",
        ],
        "exclude_names": ["@eaDir"],
        "follow_symlinks": False,
        "store_pictures": False,
    },
    "watch": {
        "enabled": True,
        "interval_seconds": 1.2,
    },
    "compare": {
        "weights": {
            "artist": 20,
            "album": 18,
            "title": 20,
            "year": 8,
            "duration": 14,
            "tracks": 8,
            "fingerprint": 6,
            "isrc": 6,
        },
        "title_min_ratio": 62.0,
    },
    "duplicates": {
        "hash_group": True,
        "fuzzy": True,
        "fuzzy_threshold": 90.0,
        "classify": True,
    },
    "services": {
        "musicbrainz": {"enabled": True, "user_agent": "MusicFusionPro/0.1"},
        "acoustid": {"enabled": False, "key": ""},
        "discogs": {"enabled": False, "token": ""},
        "lastfm": {"enabled": False, "key": ""},
    },
    "libraries": {},
    "renaming": {
        "pattern": "{artist}/{year} - {album}/{track:02d} - {title}",
    },
    "reports": {
        "directory": "reports",
    },
    "matching": {
        "artist_weight": 20,
        "album_weight": 20,
        "title_weight": 30,
        "track_number_weight": 10,
        "disc_number_weight": 5,
        "duration_weight": 10,
        "year_weight": 5,
        "confirmed_threshold": 95,
        "very_high_threshold": 90,
        "high_threshold": 80,
        "possible_threshold": 70,
        "low_threshold": 50,
        "fingerprint_weight": 35,
        "fingerprint_penalty": 15,
        "album_context_weight": 10,
        "edition_edit_cap": 96,
        "edition_cap": 94,
        "version_cap": 93,
        "duration_relative_tolerance": 0.03,
        "duration_absolute_ms": 1500,
    },
    "decision": {
        "weights": {
            "quality": 40,
            "metadata": 20,
            "artwork": 15,
            "codec": 15,
            "preference": 10,
        },
        "min_gap_for_auto": 1.0,
    },
    "log_level": "INFO",
}


@dataclass
class LibraryConfig:
    """Configuration describing a single music library root."""

    path: str
    name: str = ""
    enabled: bool = True
    watch: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            self.name = Path(self.path).name or self.path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LibraryConfig:
        """Build a LibraryConfig from a YAML mapping."""
        known = {"path", "name", "enabled", "watch"}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a mapping usable in YAML."""
        return {"path": self.path, "name": self.name, "enabled": self.enabled, "watch": self.watch}


@dataclass
class Settings:
    """Central settings object.

    The object is populated from a YAML document lazily and can persist
    changes back to disk. Profiles can be exported and re-imported.
    """

    config_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "musicfusion")
    _config_file: Path | None = None
    data: dict[str, Any] = field(default_factory=lambda: _deepcopy(DEFAULTS))

    def __post_init__(self) -> None:
        self.config_dir = Path(self.config_dir).expanduser()
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            import tempfile
            self.config_dir = Path(tempfile.gettempdir()) / "musicfusion"
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        if self._config_file is None:
            self._config_file = self.config_dir / "config.yaml"
        self.load()

    # ------------------------------------------------------------------ paths
    @property
    def config_file(self) -> Path:
        """Absolute path of the YAML config file."""
        return self._config_file or self.config_dir / "config.yaml"

    @property
    def db_path(self) -> Path:
        """Resolved path of the SQLite database file."""
        raw = self.data["db"]["path"]
        path = Path(raw)
        if not path.is_absolute():
            path = self.config_dir / path
        return path

    @property
    def log_level(self) -> int:
        import logging

        level = logging.getLevelName(self.data.get("log_level", "INFO"))
        return level if isinstance(level, int) else logging.INFO

    # ------------------------------------------------------------- libraries
    def libraries(self) -> list[LibraryConfig]:
        """Return the configured, enabled libraries as LibraryConfig objects."""
        raw = self.data.get("libraries", {})
        libs: list[LibraryConfig] = []
        for name, spec in raw.items():
            if isinstance(spec, str):
                spec = {"path": spec}
            if not isinstance(spec, dict):
                continue
            spec.setdefault("name", name)
            lib = LibraryConfig.from_dict(spec)
            if lib.enabled:
                libs.append(lib)
        return libs

    def add_library(self, path: str, name: str = "", watch: bool = True) -> LibraryConfig:
        """Register a new library root and persist the configuration."""
        lib = LibraryConfig(path=path, name=name, watch=watch)
        key = hashlib.sha1(lib.path.encode()).hexdigest()[:12]
        self.data.setdefault("libraries", {})[key] = lib.to_dict()
        self.save()
        return lib

    def get(self, dotted: str, default: Any = None) -> Any:
        """Return a nested setting using dot notation, e.g. ``app.theme``."""
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """Set a nested setting using dot notation and persist it."""
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    # ------------------------------------------------------------- persist
    def save(self) -> None:
        """Write the current settings to the YAML config file."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with self.config_file.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(
                    self.data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False
                )
        except OSError:  # pragma: no cover
            logger.warning("Could not write config file %s", self.config_file)

    def load(self) -> None:
        """Merge the on-disk YAML config over the defaults."""
        if not self.config_file.exists():
            self.save()
            return
        try:
            with self.config_file.open(encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            self.data = _deep_merge(_deepcopy(DEFAULTS), loaded)
        except yaml.YAMLError as exc:  # pragma: no cover
            logger.warning("Malformed config file %s: %s", self.config_file, exc)
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not read config file %s: %s", self.config_file, exc)

    # ------------------------------------------------------------- profiles
    def export_profile(self, target: Path) -> None:
        """Export only the user-tunable settings into ``target`` as YAML."""
        safe = {k: v for k, v in self.data.items() if k != "libraries"}
        safe.update(
            {
                "libraries": {k: v for k, v in self.data.get("libraries", {}).items()},
            }
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(safe, fh, allow_unicode=True, sort_keys=False)

    def import_profile(self, source: Path | str) -> None:
        """Merge a profile YAML onto the current settings and persist."""
        path = Path(source).expanduser()
        with path.open(encoding="utf-8") as fh:
            profile = yaml.safe_load(fh) or {}
        self.data = _deep_merge(self.data, profile)
        self.save()


def _deepcopy(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively copy a nested mapping/dict structure."""
    if isinstance(data, dict):
        return {k: _deepcopy(v) for k, v in data.items()}
    if isinstance(data, list):
        return [ _deepcopy(v) for v in data]
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries, favouring the ``overlay`` value."""
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(config_file: str | Path | None = None) -> Settings:
    """Load settings, optionally pinning an explicit config file path."""
    if config_file is None:
        return Settings()
    cfg = Path(config_file).expanduser()
    return Settings(config_dir=cfg.parent, _config_file=cfg)

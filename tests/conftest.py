"""Shared pytest fixtures for a sandboxed MusicFusion test environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicfusion.config.settings import Settings


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh, writable directory shared across the session."""
    return tmp_path_factory.mktemp("musicfusion")


@pytest.fixture()
def settings_factory(tmp_path: Path):
    """Build isolated Settings pointing into a per-test temp config dir."""

    def make() -> Settings:
        cfg = Settings(config_dir=tmp_path / "config")
        cfg.set("watch.enabled", False)
        return cfg

    return make


@pytest.fixture()
def settings(settings_factory) -> Settings:
    return settings_factory()

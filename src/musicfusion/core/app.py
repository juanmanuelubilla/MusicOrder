"""Application context: wires settings, database and job queue together."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from musicfusion.config.settings import Settings
from musicfusion.db.session import Database

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Central wiring object injected into services and UI pages.

    The context owns the mutable application-wide state (settings, database,
    job queue) so every module shares a single source of truth.
    """

    settings: Settings
    db: Database = field(init=False)

    def __post_init__(self) -> None:
        self.db = Database(self.settings.db_path)
        self.db.create_schema()


def build_context(settings: Settings | None = None) -> AppContext:
    """Create a fully wired application context.

    Args:
        settings: optional settings override; a default one is used otherwise.

    Returns:
        A ready-to-use :class:`AppContext` with an open database.
    """
    if settings is None:
        settings = Settings()
    context = AppContext(settings=settings)
    logger.info("App context ready (db=%s)", settings.db_path)
    return context

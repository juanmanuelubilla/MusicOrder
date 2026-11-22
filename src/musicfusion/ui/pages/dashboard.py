"""Task-oriented home page: plain-language launcher + library statistics."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget
from sqlalchemy import func

from musicfusion.core.app import AppContext
from musicfusion.db.models import Album, Artist, Library, Track
from musicfusion.ui.widgets import StatCard, palette


class TaskCard(QFrame):
    """A big, clickable card describing one common task in plain words."""

    ICONS = {
        "incorporar": "➕",
        "duplicados": "🔍",
        "comparar": "⚖️",
        "escanear": "📂",
        "reproducir": "▶",
        "informes": "📊",
    }

    def __init__(
        self,
        icon: str,
        title: str,
        description: str,
        on_click: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        icon_label = QLabel(icon)
        icon_label.setStyleSheet("font-size: 26px;")
        layout.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(title_label)

        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        layout.addWidget(desc_label)

        self.mouseReleaseEvent = lambda event: on_click()  # type: ignore[method-assign]


class DashboardPage(QWidget):
    """Shows what you can do, in plain language, plus aggregate stats."""

    _TASKS = [
        ("incorporar", "Incorporar música nueva",
         "Traé solo lo que falte desde otra carpeta o disco.",
         lambda nav: nav("Incorporar")),
        ("duplicados", "Buscar duplicados",
         "Encontrá canciones repetidas en tus bibliotecas.",
         lambda nav: nav("Duplicados")),
        ("comparar", "Comparar dos cosas",
         "¿Son el mismo tema? Compará archivos o carpetas.",
         lambda nav: nav("Comparar")),
        ("escanear", "Escanear bibliotecas",
         "Indexá tu música para que todo lo demás funcione.",
         "_scan_all_libraries"),
        ("reproducir", "Reproducir",
         "Escuchá tu música fusionada.",
         lambda nav: nav("Reproductor")),
        ("informes", "Ver informes",
         "Listados en HTML, Excel y PDF.",
         lambda nav: nav("Informes")),
    ]

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.navigator: Callable[..., None] | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel("¿Qué querés hacer?")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (icon, name, description, target) in enumerate(self._TASKS):
            card = TaskCard(icon, name, description, lambda t=target: self._run_task(t))
            grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(grid)

        stats_title = QLabel("Tu colección")
        stats_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(stats_title)

        stat_grid = QGridLayout()
        stat_grid.setSpacing(12)
        self.cards: dict[str, StatCard] = {}
        for column, (label, key) in enumerate(
            [("Temas", "tracks"), ("Álbumes", "albums"),
             ("Artistas", "artists"), ("Bibliotecas", "libraries")]
        ):
            card = StatCard(label, "0")
            self.cards[key] = card
            stat_grid.addWidget(card, 0, column)
        layout.addLayout(stat_grid)
        layout.addStretch(1)

        self.refresh()

    # ------------------------------------------------------------- wiring
    def set_navigator(self, navigator: Callable[..., None]) -> None:
        """MainWindow injects navigation so cards can jump between pages."""
        self.navigator = navigator

    def _run_task(self, target) -> None:
        if target == "_scan_all_libraries":
            # jump to Bibliotecas and start the scan there
            if self.navigator is not None:
                self.navigator("Bibliotecas", scan=True)
            return
        if self.navigator is not None and callable(target):
            target(self.navigator)

    def refresh(self) -> None:
        """Recompute the statistics shown on the page."""
        with self.context.db.session_scope() as session:
            counts = {
                "tracks": session.query(func.count(Track.id)).scalar() or 0,
                "albums": session.query(func.count(Album.id)).scalar() or 0,
                "artists": session.query(func.count(Artist.id)).scalar() or 0,
                "libraries": session.query(func.count(Library.id)).scalar() or 0,
            }
        for key, card in self.cards.items():
            card.set_value(f"{counts.get(key, 0):,}")

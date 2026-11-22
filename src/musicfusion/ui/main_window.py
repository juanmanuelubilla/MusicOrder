"""Main window with a sidebar navigation and page stack."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QWidget,
)

from musicfusion.core.app import AppContext
from musicfusion.ui.pages.compare import ComparePage
from musicfusion.ui.pages.dashboard import DashboardPage
from musicfusion.ui.pages.duplicates import DuplicatesPage
from musicfusion.ui.pages.fusion import FusionPage
from musicfusion.ui.pages.incorporate import IncorporatePage
from musicfusion.ui.pages.libraries import LibrariesPage
from musicfusion.ui.pages.metadata import MetadataPage
from musicfusion.ui.pages.player import PlayerPage
from musicfusion.ui.pages.reports import ReportsPage
from musicfusion.ui.pages.settings_page import SettingsPage

# Main tasks first (plain language), tools after.
_NAV = [
    ("Panel", DashboardPage),
    ("Incorporar", IncorporatePage),
    ("Comparar", ComparePage),
    ("Duplicados", DuplicatesPage),
    ("Bibliotecas", LibrariesPage),
    ("Metadatos", MetadataPage),
    ("Fusión", FusionPage),
    ("Reproductor", PlayerPage),
    ("Informes", ReportsPage),
    ("Ajustes", SettingsPage),
]

_SIDEBAR_TIPS = {
    "Panel": "Inicio: elegí qué querés hacer",
    "Incorporar": "Agregar a tu colección solo la música que falte",
    "Comparar": "¿Estos dos archivos o carpetas son lo mismo?",
    "Duplicados": "Encontrá y revisa canciones repetidas",
    "Bibliotecas": "Registrá y escaneá tus carpetas de música",
    "Metadatos": "Corregir etiquetas y carátulas",
    "Fusión": "Unir dos carpetas conservando la mejor copia (avanzado)",
    "Reproductor": "Escuchar tu música",
    "Informes": "Listados en HTML/Excel/PDF",
    "Ajustes": "Preferencias, tema e IA local",
}


class MainWindow(QMainWindow):
    """Application shell: sidebar, page stack, status bar and menus."""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle("MusicFusion Pro")
        self.resize(1080, 720)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(170)
        self.sidebar.setObjectName("Sidebar")
        for name, _ in _NAV:
            item = QListWidgetItem(name)
            if name in _SIDEBAR_TIPS:
                item.setToolTip(_SIDEBAR_TIPS[name])
            self.sidebar.addItem(item)

        self.stack = QStackedWidget()
        self.pages: list[QWidget] = []
        for _name, page_cls in _NAV:
            page = page_cls(self.context)
            self.pages.append(page)
            self.stack.addWidget(page)

        compare_page = cast(ComparePage, self.pages[self._index_of("Comparar")])
        compare_page.set_navigator(self._open_page)
        duplicates_page = cast(DuplicatesPage, self.pages[self._index_of("Duplicados")])
        duplicates_page.set_navigator(self._open_page)
        dashboard = cast(DashboardPage, self.pages[self._index_of("Panel")])
        dashboard.set_navigator(self._navigate_task)

        root.addWidget(self.sidebar)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Listo")

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._on_page_changed)
        self.sidebar.setCurrentRow(0)

        self._build_menus()
        self._build_toolbar()
        self.apply_theme()

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&Archivo")
        quit_action = QAction("Salir", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("&Ver")
        theme_action = QAction("Cambiar tema", self)
        theme_action.triggered.connect(self._toggle_theme)
        view_menu.addAction(theme_action)

        help_menu = menubar.addMenu("&Ayuda")
        about_action = QAction("Acerca de…", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Principal")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        actions = [
            ("Escanear", self._scan_library, "ctrl+E"),
            ("Duplicados", self._go_duplicates, "ctrl+d"),
            ("Comparar", self._go_compare, "ctrl+r"),
        ]
        for _label, slot, _shortcut in actions:
            action = QAction(_label, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)

    def _toggle_theme(self) -> None:
        setting = self.context.settings
        current = setting.get("app.theme", "dark")
        setting.set("app.theme", "light" if current == "light" else "dark")
        self.apply_theme()

    def apply_theme(self) -> None:
        from musicfusion.ui.theme import PALETTES

        theme = self.context.settings.get("app.theme", "dark")
        palette = PALETTES.get("light" if theme == "light" else "dark", PALETTES["dark"])
        self.setStyleSheet(palette.qss())

    def _index_of(self, label: str) -> int:
        for index, (name, _page_cls) in enumerate(_NAV):
            if name == label:
                return index
        raise KeyError(label)

    def _navigate_task(self, label: str, **kwargs) -> None:
        """Navigate from the Panel task cards, optionally triggering an action."""
        self._open_page(label)
        if kwargs.get("scan"):
            page = cast(LibrariesPage, self.pages[self._index_of("Bibliotecas")])
            page._scan_all()

    def _scan_library(self) -> None:
        self.sidebar.setCurrentRow(self._index_of("Bibliotecas"))
        page: LibrariesPage = cast(LibrariesPage, self.pages[self._index_of("Bibliotecas")])
        page._scan_all()

    def _go_duplicates(self) -> None:
        self.sidebar.setCurrentRow(self._index_of("Duplicados"))
        page: DuplicatesPage = cast(DuplicatesPage, self.pages[self._index_of("Duplicados")])
        page._analyze()

    def _go_compare(self) -> None:
        self.sidebar.setCurrentRow(self._index_of("Comparar"))

    def _open_page(self, name: str, payload: tuple[object, ...] = (), **kwargs) -> None:
        """Jump to a page by its sidebar label, e.g. 'Duplicados'.

        Extra kwargs: auto_plan=True makes the Fusión page generate its plan
        immediately from the comparison the user just ran (no second click).
        """
        try:
            index = self._index_of(name)
        except KeyError:
            return
        self.sidebar.setCurrentRow(index)
        if name == "Fusión":
            fusion_page = cast(FusionPage, self.pages[index])
            if payload:
                sources = [Path(p) for p in payload if p and isinstance(p, (str, Path))]
                if sources:
                    fusion_page.preset_sources(sources)
            if "destination" in kwargs and kwargs["destination"]:
                fusion_page.preset_destination(Path(kwargs["destination"]))
            if kwargs.get("auto_run_prompt"):
                fusion_page.prompt_start_fusion()
            elif kwargs.get("auto_plan"):
                fusion_page.auto_plan_from_comparison()

    def _on_page_changed(self, index: int) -> None:
        """Keep read-only pages fresh with the data already stored in the DB."""
        if _NAV[index][0] == "Duplicados":
            cast(DuplicatesPage, self.pages[index]).refresh()

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "Acerca de MusicFusion Pro",
            "MusicFusion Pro\nGestor y fusionador de bibliotecas musicales.\n\n"
            "Realizado por WARP Software\nhttps://www.warp.com.ar",
        )

    def set_status(self, message: str) -> None:
        self.status_bar.showMessage(message, 5000)

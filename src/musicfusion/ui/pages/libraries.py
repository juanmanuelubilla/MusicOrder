"""Library management page: add/remove roots and run scans."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicfusion.config.settings import LibraryConfig
from musicfusion.core.app import AppContext
from musicfusion.ui.pages._common import JobProgress
from musicfusion.ui.widgets import ActionBar, BusyRow


class LibrariesPage(QWidget):
    """Configure music roots and trigger incremental/full scans."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.jobs = JobProgress(self, context)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Bibliotecas musicales")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        bar = ActionBar()
        bar.add_button("Añadir carpeta…", primary=True, clicked=self._add_library)
        bar.add_button("Quitar seleccionada", clicked=self._remove_library)
        bar.add_button("Escanear", clicked=self._scan_all)
        bar.add_button("Escaneo completo", clicked=lambda: self._scan_all(full=True))
        bar.add_stretch()
        layout.addWidget(bar)

        self.list_widget = QListWidget()
        self.list_widget.itemActivated.connect(self._open_folder)
        layout.addWidget(self.list_widget, 1)

        self.progress = BusyRow()
        self.progress.update_message("Escaneando biblioteca…")
        layout.addWidget(self.progress)

        self.jobs.finished.connect(self._on_scan_done)
        self.refresh()

    def refresh(self) -> None:
        """Reload the list of configured libraries."""
        self.list_widget.clear()
        for lib in self.context.settings.libraries():
            item = QListWidgetItem(f"{lib.name}  —  {lib.path}")
            item.setData(Qt.UserRole, lib)
            self.list_widget.addItem(item)

    def _add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Elegir carpeta de música")
        if not folder:
            return
        self.context.settings.add_library(folder)
        # Make sure the root exists for the indexer.
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.refresh()

    def _remove_library(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        lib: LibraryConfig = item.data(Qt.UserRole)
        data = self.context.settings.data.get("libraries", {})
        for key, spec in list(data.items()):
            path = spec["path"] if isinstance(spec, dict) else spec
            if path == lib.path:
                del data[key]
        self.context.settings.save()
        self.refresh()

    def _open_folder(self, item: QListWidgetItem) -> None:
        lib: LibraryConfig = item.data(Qt.UserRole)
        Path(lib.path).mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["xdg-open", lib.path], check=False)

    def _scan_all(self, full: bool = False) -> None:
        self.progress.start("Escaneando biblioteca…")
        self.jobs.run_indexer(full=full, on_progress=self.progress.record_progress)

    def _on_scan_done(self, stats: dict) -> None:
        self.progress.set_done(
            f"Escaneo terminado: {stats.get('files_found', 0)} ficheros "
            f"({stats.get('files_added', 0)} nuevos)."
        )
        self.refresh()

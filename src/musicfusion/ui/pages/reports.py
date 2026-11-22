"""Reports page: generate the report bundle and open files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtWidgets import QLabel, QListWidget, QVBoxLayout, QWidget

from musicfusion.core.app import AppContext
from musicfusion.ui.pages._common import JobProgress
from musicfusion.ui.widgets import ActionBar, BusyRow


class ReportsPage(QWidget):
    """Generate and open the standard report bundle."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.jobs = JobProgress(self, context)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Informes")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        bar = ActionBar()
        bar.add_button("Generar informe", primary=True, clicked=self._generate)
        bar.add_button("Abrir carpeta", clicked=self._open_folder)
        bar.add_stretch()
        layout.addWidget(bar)

        self.report_list = QListWidget()
        layout.addWidget(self.report_list, 1)

        self.progress = BusyRow()
        self.progress.update_message("Generando informes…")
        layout.addWidget(self.progress)

        self.jobs.finished.connect(self._on_done)
        self.refresh()

    def _generate(self) -> None:
        self.progress.start("Generando informes…")
        self.jobs.run_reports(on_progress=self.progress.record_progress)

    def _on_done(self, paths: list[str]) -> None:
        self.progress.set_done("Informes generados.")
        self.refresh(list_paths=paths)

    def refresh(self, list_paths: list[str] | None = None) -> None:
        """Re-list generated report files."""
        self.report_list.clear()
        if list_paths:
            names = list_paths
        else:
            directory = Path(self.context.settings.get("reports.directory", "reports"))
            names = [str(path) for path in directory.glob("*.html")] + \
                    [str(path) for path in directory.glob("*.csv")] + \
                    [str(path) for path in directory.glob("*.xlsx")] + \
                    [str(path) for path in directory.glob("*.pdf")]
        for name in sorted(set(names)):
            self.report_list.addItem(name)

    def _open_folder(self) -> None:
        directory = Path(self.context.settings.get("reports.directory", "reports")).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(["xdg-open", str(directory)], check=False)

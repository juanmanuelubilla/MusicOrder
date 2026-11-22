"""Duplicate detection page: run the analyzer and review groups."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from musicfusion.core.app import AppContext
from musicfusion.db.models import DuplicateEntry, DuplicateGroup, Library, Track
from musicfusion.ui.pages._common import JobProgress
from musicfusion.ui.widgets import ActionBar, BusyRow


class DuplicatesPage(QWidget):
    """Detects duplicate groups across the library and exports them."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.db = context.db
        self.jobs = JobProgress(self, context)
        self.last_analyzed_paths: list[Path] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Duplicados")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        bar = ActionBar()
        bar.add_button("Analizar duplicados", primary=True, clicked=self._analyze)
        bar.add_button("En carpeta…", clicked=self._analyze_folder)
        bar.add_button("Entre dos carpetas…", clicked=self._analyze_two)
        bar.add_button("Exportar informe…", clicked=self._export)
        bar.add_button("🚀 Unir sin duplicados (Fusión)…", clicked=self._go_fusion)
        bar.add_stretch()
        layout.addWidget(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Grupo", "Razón", "Clasificación", "Título", "Ruta"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

        self.progress = BusyRow()
        self.progress.update_message("Buscando duplicados…")
        layout.addWidget(self.progress)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.jobs.finished.connect(self._on_done)
        self.jobs.failed.connect(self._on_failed)

    def _analyze(self) -> None:
        self._run()

    def _analyze_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Elegir carpeta con duplicados")
        if not folder:
            return
        self._run(paths=[Path(folder)])

    def _analyze_two(self) -> None:
        folder_a = QFileDialog.getExistingDirectory(self, "Elegir primera carpeta")
        if not folder_a:
            return
        folder_b = QFileDialog.getExistingDirectory(self, "Elegir segunda carpeta")
        if not folder_b:
            return
        self._run(paths=[Path(folder_a), Path(folder_b)])

    def _run(self, paths: list[Path] | None = None) -> None:
        if paths:
            self.last_analyzed_paths = [Path(p).resolve() for p in paths]
            self.context.settings.set("app.last_compared_sources", [str(p) for p in self.last_analyzed_paths])
        else:
            self.last_analyzed_paths = [Path(lib.path).resolve() for lib in self.context.settings.libraries()]
            if self.last_analyzed_paths:
                self.context.settings.set("app.last_compared_sources", [str(p) for p in self.last_analyzed_paths])
        self.progress.start("Buscando duplicados…")
        self.jobs.run_duplicates(on_progress=self.progress.record_progress, paths=paths)

    def _on_done(self, _result) -> None:
        self.progress.set_done("Análisis terminado.")
        self.refresh()

    def _on_failed(self, message: str) -> None:
        self.progress.set_done("")
        self.status.setText(f"Error: {message}")

    def refresh(self) -> None:
        """Re-read duplicate groups and populate the table."""
        rows: list[list[str]] = []
        with self.context.db.session_scope() as session:
            groups = session.scalars(select(DuplicateGroup).order_by(DuplicateGroup.id)).all()
            for group in groups:
                members = session.scalars(
                    select(Track).join(DuplicateEntry)
                    .where(DuplicateEntry.group_id == group.id).order_by(Track.id)
                ).all()
                for member in members:
                    rows.append([
                        str(group.id),
                        group.reason,
                        group.classification or "",
                        member.title,
                        member.path,
                    ])
        self.table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (0, 1, 2):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, col, item)
        self.status.setText(f"{len(rows)} entradas en {len(set(r[0] for r in rows))} grupos.")

    def set_navigator(self, navigator: object) -> None:
        self.navigator = navigator

    def _get_active_sources(self) -> list[Path]:
        """Collect source directories from current analysis, settings or DB libraries."""
        broad_roots = {Path("/"), Path("/mnt"), Path("/media"), Path("/Volumes"), Path("/home"), Path("/tmp")}

        # 1. From last analyzed paths in this session
        if hasattr(self, "last_analyzed_paths") and self.last_analyzed_paths:
            valid = [p for p in self.last_analyzed_paths if p.is_dir() and p not in broad_roots]
            if valid:
                return valid

        # 2. From saved compared sources in settings
        saved = self.context.settings.get("app.last_compared_sources", [])
        if saved and isinstance(saved, list):
            valid = [Path(p).resolve() for p in saved if p and Path(p).is_dir() and Path(p).resolve() not in broad_roots]
            if valid:
                return valid

        # 3. From configured libraries in settings
        libs = [Path(l.path).resolve() for l in self.context.settings.libraries() if Path(l.path).is_dir() and Path(l.path).resolve() not in broad_roots]
        if libs:
            return libs

        # 4. From Library records in DB
        with self.context.db.session_scope() as session:
            db_libs = session.scalars(select(Library.path)).all()
            valid = [Path(p).resolve() for p in db_libs if p and Path(p).is_dir() and Path(p).resolve() not in broad_roots]
            if valid:
                return valid

        return []

    def _go_fusion(self) -> None:
        if not hasattr(self, "navigator") or self.navigator is None:
            return

        sources = self._get_active_sources()
        if not sources:
            self.navigator("Fusión")
            return

        dest = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar carpeta de DESTINO único (donde consolidar la música sin duplicados)",
        )
        if not dest:
            return

        self.navigator("Fusión", tuple(sources), destination=dest, auto_run_prompt=True)

    def _export(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, "Guardar informe", "duplicates.html", "HTML (*.html);;CSV (*.csv);;Excel (*.xlsx);;PDF (*.pdf)"
        )
        if not target:
            return
        from musicfusion.reports.exporter import export_duplicates

        path = Path(target)
        groups_rows: list[dict[str, object]] = []
        with self.db.session_scope() as session:
            groups = session.scalars(select(DuplicateGroup).order_by(DuplicateGroup.id)).all()
            for group in groups:
                members = session.scalars(
                    select(Track).join(DuplicateEntry)
                    .where(DuplicateEntry.group_id == group.id)
                ).all()
                for member in members:
                    groups_rows.append({
                        "group": group.id,
                        "reason": group.reason,
                        "classification": group.classification,
                        "title": member.title,
                        "artist": member.artist_name,
                        "path": member.path,
                    })
        export_duplicates(groups_rows, path)
        self.status.setText(f"Informe guardado en {path}")

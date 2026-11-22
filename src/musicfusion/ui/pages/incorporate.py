"""Simple 3-step wizard to incorporate new music into the MASTER collection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicfusion.core.app import AppContext
from musicfusion.ui.pages._common import JobRunner
from musicfusion.ui.widgets import ActionBar

_STATUS_COLORS = {
    "EXISTE": "#8a8f98",
    "FALTA": "#69db7c",
    "POSIBLE_EQUIVALENTE": "#e8b34b",
}

_WELCOME = (
    "Paso 1: elegí tu colección ordenada (MAESTRO).\n"
    "Paso 2: elegí la carpeta con música nueva.\n"
    "Paso 3: presioná Analizar. MusicFusion detecta qué ya tenés "
    "(aunque cambien tildes o mayúsculas) y qué falta incorporar."
)


class IncorporatePage(QWidget):
    """Guided merge: MASTER collection + new folder -> copy only what's missing."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.jobs = JobRunner(self)
        self.jobs.finished.connect(self._on_finished)
        self.jobs.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Incorporar música nueva")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        welcome = QLabel(_WELCOME)
        welcome.setWordWrap(True)
        welcome.setStyleSheet(f"color: {self._muted()};")
        layout.addWidget(welcome)

        form_row = QHBoxLayout()
        self.master_edit = self._path_field()
        self.source_edit = self._path_field()
        form_row.addWidget(QLabel("MAESTRO (tu colección):"))
        form_row.addWidget(self.master_edit, 1)
        browse_m = QPushButton("Examinar…")
        browse_m.clicked.connect(lambda: self._browse(self.master_edit))
        form_row.addWidget(browse_m)
        layout.addLayout(form_row)

        second_row = QHBoxLayout()
        second_row.addWidget(QLabel("MÚSICA NUEVA:       "))
        second_row.addWidget(self.source_edit, 1)
        browse_s = QPushButton("Examinar…")
        browse_s.clicked.connect(lambda: self._browse(self.source_edit))
        second_row.addWidget(browse_s)
        layout.addLayout(second_row)

        actions = ActionBar()
        self.analyze_btn = actions.add_button(
            "Analizar qué falta", primary=True, clicked=self._analyze
        )
        self.filter_box = QComboBox()
        self.filter_box.addItems(["Mostrar todo", "Solo lo que falta", "Solo para revisar"])
        self.filter_box.currentIndexChanged.connect(self._apply_filter)
        actions.add_widget(QLabel("Filtro:"))
        actions.add_widget(self.filter_box)
        actions.add_stretch()
        layout.addWidget(actions)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Estado", "Artista (nueva)", "Álbum", "Confianza", "Acción", "Destino en el maestro"]
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 160)
        self.tree.setColumnWidth(2, 220)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 130)
        layout.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        self.summary_label = QLabel("")
        self.apply_btn = QPushButton("Incorporar elementos marcados COPIAR")
        self.apply_btn.setObjectName("Primary")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        bottom.addStretch(1)
        bottom.addWidget(self.summary_label)
        bottom.addWidget(self.apply_btn)
        layout.addLayout(bottom)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._muted()};")
        layout.addWidget(self.status)

        self.master_edit.setText(str(self.context.settings.get("ui.master_path", "") or ""))
        self.source_edit.setText(str(self.context.settings.get("ui.source_path", "") or ""))
        self._actions: list = []

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _muted() -> str:
        from musicfusion.ui.widgets import palette

        return str(palette().text_muted)

    def _path_field(self) -> QLabel:
        label = QLabel("(sin elegir)")
        label.setStyleSheet("font-weight: 600;")
        return label

    def _browse(self, field: QLabel) -> None:
        current = field.text() if field.text() != "(sin elegir)" else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Elegir carpeta", current)
        if not path:
            return
        field.setText(path)
        key = "ui.master_path" if field is self.master_edit else "ui.source_path"
        self.context.settings.set(key, path)

    # ------------------------------------------------------------- analyze
    def _analyze(self) -> None:
        master = Path(self.master_edit.text()) if self.master_edit.text() != "(sin elegir)" else None
        source = Path(self.source_edit.text()) if self.source_edit.text() != "(sin elegir)" else None
        if not master or not source or not master.is_dir():
            self.status.setText("Elegí primero las dos carpetas (y que existan).")
            return
        self.status.setText("Analizando… puede tardar un poco en colecciones grandes.")
        self.jobs.run(self._analyze_job, master, source)

    def _analyze_job(self, master: Path, source: Path):
        from musicfusion.sync.analyzer import analyze_sync
        from musicfusion.sync.executor import build_actions

        plan = analyze_sync(master, source, settings=self.context.settings)
        return build_actions(plan)

    def _on_finished(self, result) -> None:
        if isinstance(result, list) and result and hasattr(result[0], "status"):
            self._render_actions(result)
            return
        if isinstance(result, dict) and "copied" in result:
            copied = len(result.get("copied", []))
            files = result.get("files_copied", 0)
            skipped = result.get("skipped_existing", [])
            msg = f"Incorporación lista: {copied} carpetas copiadas ({files} archivos)."
            if skipped:
                msg += f" Omitidas por existir: {len(skipped)}."
            if result.get("errors"):
                msg += f" Errores: {len(result['errors'])}."
            self.status.setText(msg)
            QMessageBox.information(self, "Incorporar música", msg)
            self._analyze()  # refresh table after copying
            return
        self.status.setText("")

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Error: {message}")

    def _render_actions(self, actions: list) -> None:
        self._actions = actions
        self.tree.clear()
        pending = 0
        review = 0
        for action in actions:
            color = QColor(_STATUS_COLORS.get(action.status, "#cfd3d9"))
            item = QTreeWidgetItem([
                action.status,
                action.artist_incoming,
                action.item if action.item != action.artist_incoming else "",
                f"{action.confidence:.0f}%",
                action.action_label.replace("_", " ").capitalize(),
                str(action.destination) if action.destination else "",
            ])
            item.setForeground(0, color)
            item.setForeground(4, color)
            if action.action_label == "REVIEW":
                item.setForeground(5, QColor("#e8b34b"))
                review += 1
            if action.action_label == "COPIAR":
                pending += 1
            self.tree.addTopLevelItem(item)
        self._apply_filter()
        self.summary_label.setText(f"{pending} para incorporar · {review} para revisar")
        self.apply_btn.setEnabled(pending > 0)
        self.apply_btn.setText(f"Incorporar {pending} elemento(s)")
        self.status.setText("Análisis completo.")

    def _apply_filter(self) -> None:
        mode = self.filter_box.currentIndex()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            action_label = item.text(4).lower()
            visible = (
                mode == 0
                or (mode == 1 and "copiar" in action_label)
                or (mode == 2 and "review" in action_label)
            )
            item.setHidden(not visible)

    # --------------------------------------------------------------- apply
    def _apply(self) -> None:
        pending = [a for a in self._actions if a.action_label == "COPIAR"]
        if not pending:
            return
        lines = "\n".join(
            f"• {a.item}  →  {a.destination}" if a.destination else f"• {a.item}"
            for a in pending[:12]
        )
        more = "" if len(pending) <= 12 else f"\n… y {len(pending) - 12} más"
        answer = QMessageBox.question(
            self,
            "Confirmar incorporación",
            f"Se van a copiar {len(pending)} elementos al MAESTRO:\n\n{lines}{more}\n\n¿Continuar?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status.setText("Copiando…")
        self.jobs.run(lambda: __import__("musicfusion.sync.executor", fromlist=["execute_merge"]).execute_merge(pending, assume_yes=True))

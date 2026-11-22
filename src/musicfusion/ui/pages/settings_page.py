"""Preferences page: theme, profile, scanning and local-AI settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from musicfusion.core.app import AppContext
from musicfusion.ui.widgets import ActionBar


class SettingsPage(QWidget):
    """Edit and persist user preferences."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Ajustes")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light"])
        form.addRow("Tema:", self.theme_combo)

        self.language_combo = QComboBox()
        self.language_combo.addItems(["es", "en"])
        form.addRow("Idioma:", self.language_combo)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 32)
        form.addRow("Hilos máximos:", self.threads_spin)

        self.reports_dir = QLabel()
        form.addRow("Directorio de informes:", self.reports_dir)
        layout.addLayout(form)

        # ------------------------------------------------ IA local (Ollama)
        ai_label = QLabel("IA local (Ollama)")
        ai_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(ai_label)

        ai_form = QFormLayout()
        ai_form.setSpacing(10)

        self.ai_check = QCheckBox("Usar IA local como señal extra en comparaciones")
        ai_form.addRow("", self.ai_check)

        self.ai_host_edit = QLineEdit()
        self.ai_host_edit.setPlaceholderText("http://localhost:11434")
        ai_form.addRow("Servidor Ollama:", self.ai_host_edit)

        model_row = QHBoxLayout()
        self.ai_model_combo = QComboBox()
        self.ai_model_combo.setMinimumWidth(280)
        refresh_btn = QPushButton("Actualizar modelos")
        refresh_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self.ai_model_combo, 1)
        model_row.addWidget(refresh_btn)
        ai_form.addRow("Modelo:", model_row)
        layout.addLayout(ai_form)

        bar = ActionBar()
        bar.add_button("Guardar", primary=True, clicked=self._save)
        bar.add_button("Exportar perfil…", clicked=self._export_profile)
        bar.add_button("Importar perfil…", clicked=self._import_profile)
        bar.add_stretch()
        layout.addWidget(bar)
        layout.addStretch(1)

        self._apply_ui()

    def _apply_ui(self) -> None:
        setting = self.context.settings
        theme = setting.get("app.theme", "dark")
        self.theme_combo.setCurrentText("light" if theme == "light" else "dark")
        lang = setting.get("app.language", "es")
        self.language_combo.setCurrentText("en" if lang == "en" else "es")
        self.threads_spin.setValue(int(setting.get("app.max_threads", 4)))
        reports_dir = setting.get("reports.directory", "reports")
        self.reports_dir.setText(str(reports_dir))
        self.ai_check.setChecked(bool(setting.get("ollama.enabled", False)))
        self.ai_host_edit.setText(str(setting.get("ollama.host", "http://localhost:11434")))
        current_model = str(setting.get("ollama.model", "") or "")
        if not self.ai_model_combo.count():
            self._refresh_models(select=current_model)
        elif current_model:
            self.ai_model_combo.setCurrentText(current_model)

    def _refresh_models(self, select: str | None = None) -> None:
        from musicfusion.services.ollamalocal import OllamaClient

        host = self.ai_host_edit.text().strip() or "http://localhost:11434"
        models = OllamaClient(host=host).list_models()
        self.ai_model_combo.clear()
        if models:
            self.ai_model_combo.addItems(models)
            chosen = select or str(self.context.settings.get("ollama.model", "") or "")
            if chosen:
                index = self.ai_model_combo.findText(chosen)
                if index >= 0:
                    self.ai_model_combo.setCurrentIndex(index)
            self.ai_host_edit.setStyleSheet("")
        else:
            self.ai_model_combo.addItem("(Ollama no disponible)")
            self.ai_host_edit.setStyleSheet("color: #e05555;")

    def _save(self) -> None:
        setting = self.context.settings
        setting.set("app.theme", self.theme_combo.currentText())
        setting.set("app.language", self.language_combo.currentText())
        setting.set("app.max_threads", self.threads_spin.value())
        model = self.ai_model_combo.currentText()
        if "(" in model and ")" in model:
            model = ""
        setting.set("ollama.enabled", self.ai_check.isChecked())
        setting.set("ollama.host", self.ai_host_edit.text().strip() or "http://localhost:11434")
        setting.set("ollama.model", model)
        QMessageBox.information(self, "Ajustes", "Preferencias guardadas.")
        self._apply_ui()
        if hasattr(self.parent(), "apply_theme"):
            self.parent().apply_theme()

    def _export_profile(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, "Exportar perfil", "musicfusion-profile.yaml", "YAML (*.yaml)"
        )
        if not target:
            return
        self.context.settings.export_profile(Path(target))
        QMessageBox.information(self, "Perfil", f"Perfil exportado a {target}")

    def _import_profile(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "Importar perfil", str(Path.home()), "YAML (*.yaml)"
        )
        if not source:
            return
        self.context.settings.import_profile(source)
        self._apply_ui()
        QMessageBox.information(self, "Perfil", "Perfil importado.")

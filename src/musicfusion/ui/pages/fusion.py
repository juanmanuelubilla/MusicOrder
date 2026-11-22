"""Interactive, user-friendly GUI for merging music libraries without duplicates."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from musicfusion.core.app import AppContext
from musicfusion.db.models import Library
from musicfusion.merge.engine import (
    FusionEngine,
    FusionOptions,
    FusionReport,
    ProgressData,
)
from musicfusion.ui.widgets import ActionBar, StatCard, palette
from sqlalchemy import select


_BROAD_ROOTS = {Path("/"), Path("/mnt"), Path("/media"), Path("/Volumes"), Path("/home"), Path("/tmp")}


def _fmt_size(bytes_val: int) -> str:
    val = float(bytes_val)
    units = ["B", "KB", "MB", "GB", "TB"]
    for u in units:
        if val < 1024 or u == "TB":
            return f"{val:.1f} {u}" if u != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} GB"


class FusionWorker(QThread):
    """Background worker thread running FusionEngine without freezing the UI."""

    progress_signal = Signal(ProgressData)
    finished_signal = Signal(FusionReport)
    failed_signal = Signal(str)

    def __init__(self, options: FusionOptions, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.options = options
        self.engine = FusionEngine(options)

    def cancel(self) -> None:
        self.engine.cancel()

    def run(self) -> None:
        try:
            report = self.engine.run(on_progress=self._on_progress)
            self.finished_signal.emit(report)
        except Exception as exc:
            self.failed_signal.emit(str(exc))

    def _on_progress(self, data: ProgressData) -> None:
        self.progress_signal.emit(data)


class FusionPage(QWidget):
    """User-friendly, modern page to merge multiple music directories safely."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.sources: list[Path] = []
        self.destination: Path | None = None
        self._worker: FusionWorker | None = None
        self._last_report: FusionReport | None = None
        self._last_logged_msg: str = ""
        self._last_log_time: float = 0.0

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        # -------------------------------------------------------------
        # 1. HEADER MODERNO
        # -------------------------------------------------------------
        header_card = QFrame()
        header_card.setObjectName("Card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 16, 18, 16)
        header_layout.setSpacing(6)

        title_row = QHBoxLayout()
        title_icon = QLabel("🎵")
        title_icon.setStyleSheet("font-size: 26px;")
        title_text = QLabel("MusicFusion — Fusión y Consolidación de Bibliotecas")
        title_text.setStyleSheet("font-size: 20px; font-weight: 700; letter-spacing: -0.3px;")
        title_row.addWidget(title_icon)
        title_row.addWidget(title_text)
        title_row.addStretch()
        header_layout.addLayout(title_row)

        desc = QLabel(
            "Consolida múltiples carpetas musicales en una única colección ordenada sin duplicados. "
            "La detección analiza el contenido binario real (hash SHA-256): si un tema se repite varias veces "
            "en una carpeta o entre distintas carpetas, se copiará una sola copia perfecta al destino. "
            "Tus archivos originales permanecen siempre intactos."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {palette().text_muted}; font-size: 13px; line-height: 1.4;")
        header_layout.addWidget(desc)
        layout.addWidget(header_card)

        # -------------------------------------------------------------
        # 2. SECCIONES ORÍGENES Y DESTINO
        # -------------------------------------------------------------
        dirs_layout = QHBoxLayout()
        dirs_layout.setSpacing(14)

        # Panel Orígenes
        orig_frame = QFrame()
        orig_frame.setObjectName("Card")
        orig_box = QVBoxLayout(orig_frame)
        orig_box.setContentsMargins(16, 14, 16, 14)
        orig_box.setSpacing(10)

        orig_header = QHBoxLayout()
        orig_title = QLabel("📂 Carpetas de Origen")
        orig_title.setStyleSheet("font-weight: 700; font-size: 14px;")
        self.lbl_src_count = QLabel("(0 seleccionadas)")
        self.lbl_src_count.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        orig_header.addWidget(orig_title)
        orig_header.addWidget(self.lbl_src_count)
        orig_header.addStretch()
        orig_box.addLayout(orig_header)

        self.sources_list = QListWidget()
        self.sources_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.sources_list.setMinimumHeight(120)
        self.sources_list.setToolTip("Carpetas que serán analizadas en busca de música")
        orig_box.addWidget(self.sources_list)

        orig_buttons = QHBoxLayout()
        orig_buttons.setSpacing(8)
        btn_add_src = QPushButton("➕ Agregar carpeta…")
        btn_add_src.setToolTip("Agregar una carpeta de música a la lista de orígenes")
        btn_add_src.clicked.connect(self._add_source)
        orig_buttons.addWidget(btn_add_src)

        btn_del_src = QPushButton("🗑️ Quitar seleccionada")
        btn_del_src.setToolTip("Quitar la carpeta seleccionada de la lista")
        btn_del_src.clicked.connect(self._remove_selected_source)
        orig_buttons.addWidget(btn_del_src)

        btn_clear_src = QPushButton("🧹 Limpiar lista")
        btn_clear_src.setToolTip("Vaciar todas las carpetas de origen")
        btn_clear_src.clicked.connect(self._clear_sources)
        orig_buttons.addWidget(btn_clear_src)

        btn_load_compared = QPushButton("🔄 Cargar de Comparación")
        btn_load_compared.setToolTip("Cargar automáticamente las carpetas analizadas en la sección Comparar o Duplicados")
        btn_load_compared.clicked.connect(lambda: self.load_last_compared_sources(silent=False))
        orig_buttons.addWidget(btn_load_compared)
        orig_box.addLayout(orig_buttons)

        dirs_layout.addWidget(orig_frame, 1)

        # Panel Destino y Opciones
        right_frame = QFrame()
        right_frame.setObjectName("Card")
        right_box = QVBoxLayout(right_frame)
        right_box.setContentsMargins(16, 14, 16, 14)
        right_box.setSpacing(10)

        dest_title = QLabel("🎯 Carpeta de Destino")
        dest_title.setStyleSheet("font-weight: 700; font-size: 14px;")
        right_box.addWidget(dest_title)

        dest_row = QHBoxLayout()
        self.dest_path_label = QLabel("⚠️ Ninguna carpeta seleccionada")
        self.dest_path_label.setStyleSheet(
            "font-size: 12px; font-weight: 500; padding: 8px 12px; "
            f"background-color: {palette().bg}; border: 1px dashed {palette().border}; "
            "border-radius: 6px;"
        )
        self.dest_path_label.setWordWrap(True)
        dest_row.addWidget(self.dest_path_label, 1)

        btn_pick_dest = QPushButton("Elegir destino…")
        btn_pick_dest.setToolTip("Seleccionar la carpeta donde se fusionará la colección")
        btn_pick_dest.clicked.connect(self._pick_dest)
        dest_row.addWidget(btn_pick_dest)
        right_box.addLayout(dest_row)

        opts_title = QLabel("⚙️ Opciones de Fusión")
        opts_title.setStyleSheet("font-weight: 700; font-size: 13px; margin-top: 4px;")
        right_box.addWidget(opts_title)

        opts_grid = QGridLayout()
        opts_grid.setSpacing(8)

        self.chk_subdirs = QCheckBox("Incluir subdirectorios (recursivo)")
        self.chk_subdirs.setChecked(True)
        self.chk_subdirs.setToolTip("Explorar todas las subcarpetas dentro de los orígenes")
        opts_grid.addWidget(self.chk_subdirs, 0, 0)

        self.chk_verify = QCheckBox("Verificar integridad (SHA-256)")
        self.chk_verify.setChecked(True)
        self.chk_verify.setToolTip("Comprueba que cada archivo copiado sea idéntico en bytes al original")
        opts_grid.addWidget(self.chk_verify, 0, 1)

        self.chk_simulation = QCheckBox("Modo simulación (sin copiar)")
        self.chk_simulation.setChecked(False)
        self.chk_simulation.setToolTip("Analiza y calcula duplicados sin escribir ningún archivo en el disco")
        opts_grid.addWidget(self.chk_simulation, 1, 0)

        self.chk_preserve_structure = QCheckBox("Conservar y unificar estructura (Artista/Álbum)")
        self.chk_preserve_structure.setChecked(True)
        self.chk_preserve_structure.setToolTip("Mantiene y unifica las carpetas de Artista y Álbum, colapsando variantes como Babasonicos y Babasónicos")
        opts_grid.addWidget(self.chk_preserve_structure, 1, 1)

        self.chk_report = QCheckBox("Generar informe detallado")
        self.chk_report.setChecked(True)
        self.chk_report.setToolTip("Guarda lista completa de duplicados y archivos copiados")
        opts_grid.addWidget(self.chk_report, 2, 0)

        self.chk_acoustic = QCheckBox("Firma acústica (dejar mejor bitrate)")
        self.chk_acoustic.setChecked(True)
        self.chk_acoustic.setToolTip("Compara la huella sonora real de la música (Chromaprint/AcoustID) y, si dos temas son la misma canción pero con distinta compresión, conserva automáticamente la copia con mayor bitrate.")
        opts_grid.addWidget(self.chk_acoustic, 2, 1)

        self.chk_normalize = QCheckBox("🏷️ Normalizar nombres y tags (Artista / Año - Álbum / 00 - Título.ext)")
        self.chk_normalize.setChecked(True)
        self.chk_normalize.setToolTip("Organiza el destino en 'Artista/Año - Álbum/00 - Título.ext', completando y actualizando las etiquetas de audio sin alterar los archivos de origen.")
        opts_grid.addWidget(self.chk_normalize, 3, 0, 1, 2)

        right_box.addLayout(opts_grid)
        dirs_layout.addWidget(right_frame, 1)
        layout.addLayout(dirs_layout)

        # -------------------------------------------------------------
        # 3. BARRA DE ACCIÓN PRINCIPAL
        # -------------------------------------------------------------
        action_bar = ActionBar()
        self.btn_run = action_bar.add_button("🚀 Iniciar Fusión", primary=True, clicked=self._start_fusion)
        self.btn_run.setToolTip("Comenzar el análisis y la copia deduplicada")
        self.btn_cancel = action_bar.add_button("⏹️ Cancelar proceso", danger=True, clicked=self._cancel_fusion)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setToolTip("Detener el proceso de forma segura")

        self.btn_export_txt = action_bar.add_button("📄 Exportar TXT…", clicked=self._export_txt)
        self.btn_export_txt.setEnabled(False)
        self.btn_export_csv = action_bar.add_button("📊 Exportar CSV…", clicked=self._export_csv)
        self.btn_export_csv.setEnabled(False)
        action_bar.add_stretch()
        layout.addWidget(action_bar)

        # -------------------------------------------------------------
        # 4. TARJETAS DE MÉTRICAS CON COLORES DISTINTIVOS
        # -------------------------------------------------------------
        stats_grid = QHBoxLayout()
        stats_grid.setSpacing(10)
        self.card_found = StatCard("Encontrados", "0", "Archivos de audio", accent_color="#60a5fa")
        self.card_analyzed = StatCard("Analizados", "0", "Hashes SHA-256", accent_color="#a78bfa")
        self.card_copied = StatCard("Copiados", "0", "Nuevos al destino", accent_color="#34d399")
        self.card_duplicates = StatCard("Duplicados", "0", "Omitidos (mismo hash)", accent_color="#fbbf24")
        self.card_conflicts = StatCard("Conflictos", "0", "Nombres incrementados", accent_color="#38bdf8")
        self.card_errors = StatCard("Errores", "0", "Problemas de lectura", accent_color="#f87171")

        for card in (
            self.card_found,
            self.card_analyzed,
            self.card_copied,
            self.card_duplicates,
            self.card_conflicts,
            self.card_errors,
        ):
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            stats_grid.addWidget(card, 1)
        layout.addLayout(stats_grid)

        # -------------------------------------------------------------
        # 5. BARRA DE PROGRESO Y MONITOR EN TIEMPO REAL
        # -------------------------------------------------------------
        prog_frame = QFrame()
        prog_frame.setObjectName("Card")
        prog_box = QVBoxLayout(prog_frame)
        prog_box.setContentsMargins(16, 12, 16, 12)
        prog_box.setSpacing(8)

        status_row = QHBoxLayout()
        self.lbl_status = QLabel("Listo. Agrega tus carpetas y pulsa 'Iniciar Fusión'.")
        self.lbl_status.setStyleSheet("font-weight: 600; font-size: 13px;")
        status_row.addWidget(self.lbl_status)
        status_row.addStretch()

        self.lbl_percent = QLabel("0%")
        self.lbl_percent.setStyleSheet(f"font-weight: 700; font-size: 13px; color: {palette().accent};")
        status_row.addWidget(self.lbl_percent)
        prog_box.addLayout(status_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        prog_box.addWidget(self.progress_bar)

        metrics_layout = QHBoxLayout()
        self.lbl_speed = QLabel("Velocidad: 0.0 arch/s (0.00 MB/s)")
        self.lbl_time = QLabel("Tiempo transcurrido: 0.0 s")
        self.lbl_speed.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        self.lbl_time.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        metrics_layout.addWidget(self.lbl_speed)
        metrics_layout.addStretch()
        metrics_layout.addWidget(self.lbl_time)
        prog_box.addLayout(metrics_layout)
        layout.addWidget(prog_frame)

        # -------------------------------------------------------------
        # 6. CONSOLA DE ACTIVIDAD
        # -------------------------------------------------------------
        console_header = QHBoxLayout()
        log_label = QLabel("📋 Consola de Actividad")
        log_label.setStyleSheet("font-weight: 700; font-size: 13px;")
        console_header.addWidget(log_label)
        console_header.addStretch()
        btn_clear_log = QPushButton("Limpiar consola")
        btn_clear_log.setStyleSheet("font-size: 11px; padding: 3px 8px;")
        btn_clear_log.clicked.connect(self._clear_console)
        console_header.addWidget(btn_clear_log)
        layout.addLayout(console_header)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(130)
        self.console.setStyleSheet(
            f"background-color: {palette().bg}; color: #cbd5e1; "
            "font-family: 'JetBrains Mono', 'Fira Code', 'DejaVu Sans Mono', monospace; "
            "font-size: 12px; line-height: 1.4; border-radius: 8px;"
        )
        layout.addWidget(self.console)

        # Cargar última carpeta de destino recordada
        last_dest = str(self.context.settings.get("ui.fusion_dest_path", "") or "")
        if last_dest and Path(last_dest).is_dir():
            self.destination = Path(last_dest)
            self.dest_path_label.setText(str(self.destination))
            self.dest_path_label.setStyleSheet(
                f"font-size: 12px; font-weight: 600; padding: 8px 12px; "
                f"background-color: {palette().bg}; border: 1px solid {palette().border}; "
                "border-radius: 6px; color: #60a5fa;"
            )

    def _clear_console(self) -> None:
        self.console.clear()

    def _update_sources_count(self) -> None:
        count = len(self.sources)
        suffix = "carpeta" if count == 1 else "carpetas"
        self.lbl_src_count.setText(f"({count} {suffix})")

    def _add_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de origen")
        if not folder:
            return
        p = Path(folder).resolve()
        if p not in self.sources:
            self.sources.append(p)
            self.sources_list.addItem(f"📁 {p}")
            self._update_sources_count()
            self.log(f"➕ Origen agregado: {p}")
        else:
            self.log(f"ℹ️ La carpeta ya estaba en la lista: {p}")

    def _remove_selected_source(self) -> None:
        selected_items = self.sources_list.selectedItems()
        if not selected_items:
            return
        for item in selected_items:
            row = self.sources_list.row(item)
            text_path = item.text().replace("📁 ", "").strip()
            p = Path(text_path).resolve()
            if p in self.sources:
                self.sources.remove(p)
            self.sources_list.takeItem(row)
            self.log(f"🗑️ Origen eliminado: {p}")
        self._update_sources_count()

    def _clear_sources(self) -> None:
        self.sources.clear()
        self.sources_list.clear()
        self._update_sources_count()
        self.log("🧹 Lista de orígenes vaciada.")

    def _pick_dest(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de destino")
        if not folder:
            return
        p = Path(folder).resolve()
        self.destination = p
        self.dest_path_label.setText(str(p))
        self.dest_path_label.setStyleSheet(
            f"font-size: 12px; font-weight: 600; padding: 8px 12px; "
            f"background-color: {palette().bg}; border: 1px solid {palette().border}; "
            "border-radius: 6px; color: #60a5fa;"
        )
        self.context.settings.set("ui.fusion_dest_path", str(p))
        self.log(f"🎯 Carpeta de destino establecida: {p}")

    def log(self, message: str) -> None:
        self.console.appendPlainText(message)
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def _start_fusion(self) -> None:
        if not self.sources:
            QMessageBox.warning(
                self,
                "Faltan orígenes",
                "Por favor agrega al menos una carpeta de música en 'Carpetas de Origen'.",
            )
            return
        if not self.destination:
            QMessageBox.warning(
                self,
                "Falta destino",
                "Por favor selecciona la 'Carpeta de Destino' donde se consolidará la música.",
            )
            return

        broad_roots = {Path("/"), Path("/mnt"), Path("/media"), Path("/Volumes")}
        for src in self.sources:
            if src == self.destination:
                QMessageBox.warning(
                    self,
                    "Ruta no permitida",
                    f"El destino no puede ser la misma carpeta de origen:\n{src}",
                )
                return
            if src in broad_roots:
                reply = QMessageBox.question(
                    self,
                    "Atención: Directorio de sistema seleccionado",
                    f"Has seleccionado '{src}' como origen.\n\n"
                    "Escanear una ruta raíz o de montaje puede demorar varios minutos porque "
                    "incluye todos los discos montados.\n\n"
                    "¿Deseas continuar de todas formas?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        opts = FusionOptions(
            sources=list(self.sources),
            destination=self.destination,
            simulation=self.chk_simulation.isChecked(),
            include_subdirectories=self.chk_subdirs.isChecked(),
            verify_sha256=self.chk_verify.isChecked(),
            preserve_structure=self.chk_preserve_structure.isChecked(),
            normalize_structure=self.chk_normalize.isChecked(),
            update_tags=self.chk_normalize.isChecked(),
            acoustic_detection=self.chk_acoustic.isChecked(),
        )

        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_export_txt.setEnabled(False)
        self.btn_export_csv.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_percent.setText("0%")
        self.console.clear()
        self._last_logged_msg = ""
        self._last_log_time = 0.0

        sim_str = " [MODO SIMULACIÓN - SIN COPIAR]" if opts.simulation else ""
        self.log(f"🚀 INICIANDO FUSIÓN DE BIBLIOTECAS{sim_str}")
        self.log(f"   Carpetas de origen ({len(opts.sources)}):")
        for s in opts.sources:
            self.log(f"     • {s}")
        self.log(f"   Carpeta de destino: {opts.destination}")

        for s in opts.sources:
            try:
                if self.destination.is_relative_to(s):
                    self.log(f"   ℹ️ Nota: El destino está dentro de '{s}'. Se excluirá automáticamente del escaneo para evitar recursión.")
            except AttributeError:
                pass

        self.log("------------------------------------------------------------------")

        self._worker = FusionWorker(opts, self)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.failed_signal.connect(self._on_failed)
        self._worker.start()

    def _cancel_fusion(self) -> None:
        if self._worker and self._worker.isRunning():
            self.lbl_status.setText("Cancelando de forma segura…")
            self.log("⚠️ Solicitud de cancelación recibida. Deteniendo operaciones…")
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)

    def _on_progress(self, data: ProgressData) -> None:
        self.card_found.set_value(str(data.files_found))
        self.card_analyzed.set_value(str(data.files_analyzed))
        self.card_copied.set_value(str(data.files_copied))
        self.card_duplicates.set_value(str(data.duplicates_omitted))
        self.card_conflicts.set_value(str(data.conflicts_resolved))
        self.card_errors.set_value(str(data.errors))

        pct = int(data.percent)
        self.progress_bar.setValue(pct)
        self.lbl_percent.setText(f"{pct}%")
        self.lbl_status.setText(data.status_message)
        speed_mb = data.speed_bytes_sec / (1024 * 1024)
        self.lbl_speed.setText(f"Velocidad: {data.speed_files_sec:.1f} arch/s ({speed_mb:.2f} MB/s)")
        self.lbl_time.setText(f"Tiempo: {data.elapsed_seconds:.1f} s")

        msg = data.status_message
        now = time.time()
        # Log to console if message changed and is a notable action OR if > 0.4s elapsed
        is_action = any(sym in msg for sym in ("🔍", "📚", "⏭️", "⭐", "⚡", "❌", "⚠️", "Copiando", "Omitido", "Mejorada"))
        if msg and (msg != self._last_logged_msg):
            if is_action or (now - self._last_log_time >= 0.4):
                self.log(f"[{data.percent:5.1f}%] {msg}")
                self._last_logged_msg = msg
                self._last_log_time = now

    def _on_finished(self, report: FusionReport) -> None:
        self._last_report = report
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_export_txt.setEnabled(True)
        self.btn_export_csv.setEnabled(True)
        self.progress_bar.setValue(100)
        self.lbl_percent.setText("100%")

        self.card_found.set_value(str(report.total_found))
        self.card_analyzed.set_value(
            str(report.unique_files + report.duplicates_detected + report.errors_count)
        )
        self.card_copied.set_value(str(report.files_copied))
        self.card_duplicates.set_value(str(report.duplicates_detected))
        self.card_conflicts.set_value(str(report.conflicts_resolved))
        self.card_errors.set_value(str(report.errors_count))

        self.log("\n" + report.summary())

        info_box = QMessageBox(self)
        info_box.setWindowTitle("Fusión completada con éxito")
        info_box.setIcon(QMessageBox.Icon.Information)
        info_box.setText("El proceso de fusión ha finalizado.")
        info_box.setInformativeText(report.summary())
        info_box.exec()

    def _on_failed(self, error_msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText(f"Error: {error_msg}")
        self.log(f"❌ ERROR: {error_msg}")
        QMessageBox.critical(self, "Error durante la fusión", f"Se produjo un error:\n{error_msg}")

    def _export_txt(self) -> None:
        if not self._last_report:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar informe TXT", "informe_fusion.txt", "Archivos de texto (*.txt)"
        )
        if filename:
            self._last_report.export_txt(Path(filename))
            self.log(f"💾 Informe de texto guardado en: {filename}")
            QMessageBox.information(self, "Exportación exitosa", f"Informe guardado en:\n{filename}")

    def _export_csv(self) -> None:
        if not self._last_report:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar informe CSV", "informe_fusion.csv", "Archivos CSV (*.csv)"
        )
        if filename:
            self._last_report.export_csv(Path(filename))
            self.log(f"📊 Informe CSV guardado en: {filename}")
            QMessageBox.information(self, "Exportación exitosa", f"Informe CSV guardado en:\n{filename}")

    def preset_sources(self, sources: list[Path]) -> None:
        """Set source folders programmatically from previous analysis."""
        self._clear_sources()
        for src in sources:
            p = src.resolve()
            if p in _BROAD_ROOTS:
                continue
            if p not in self.sources and p.is_dir():
                self.sources.append(p)
                self.sources_list.addItem(f"📁 {p}")
        self._update_sources_count()
        if self.sources:
            self.log(f"📥 {len(self.sources)} carpetas de origen cargadas automáticamente.")

    def load_last_compared_sources(self, silent: bool = False) -> None:
        """Load source directories from the last comparison or duplicate scan."""
        saved = self.context.settings.get("app.last_compared_sources", [])
        valid: list[Path] = []
        if saved and isinstance(saved, list):
            valid = [Path(p).resolve() for p in saved if p and Path(p).is_dir() and Path(p).resolve() not in _BROAD_ROOTS]

        if not valid:
            with self.context.db.session_scope() as session:
                db_libs = session.scalars(select(Library.path)).all()
                valid = [Path(p).resolve() for p in db_libs if p and Path(p).is_dir() and Path(p).resolve() not in _BROAD_ROOTS]

        if not valid:
            valid = [Path(l.path).resolve() for l in self.context.settings.libraries() if Path(l.path).is_dir() and Path(l.path).resolve() not in _BROAD_ROOTS]

        if not valid:
            if not silent:
                QMessageBox.information(
                    self,
                    "Sin carpetas de comparación",
                    "No se encontraron carpetas previas de comparación guardadas.\n\n"
                    "Puedes agregarlas manualmente con el botón '➕ Agregar carpeta…' o seleccionarlas en la sección 'Comparar'.",
                )
            return

        self.preset_sources(valid)
        self.log(f"🔄 Se cargaron {len(valid)} carpetas desde la comparación previa.")

    def on_page_shown(self) -> None:
        """Sanitize sources and auto-load if empty when the user switches to this page."""
        # Purge broad roots like /mnt if previously present
        clean_sources = [p for p in self.sources if p not in _BROAD_ROOTS and p.is_dir()]
        if len(clean_sources) != len(self.sources):
            self.preset_sources(clean_sources)
        if not self.sources:
            self.load_last_compared_sources(silent=True)

    def preset_destination(self, dest: Path) -> None:
        """Set destination folder programmatically."""
        p = dest.resolve()
        self.destination = p
        self.dest_path_label.setText(str(p))
        self.dest_path_label.setStyleSheet(
            f"font-size: 12px; font-weight: 600; padding: 8px 12px; "
            f"background-color: {palette().bg}; border: 1px solid {palette().border}; "
            "border-radius: 6px; color: #34d399;"
        )
        self.context.settings.set("ui.fusion_dest_path", str(p))
        self.log(f"🎯 Carpeta de destino configurada: {p}")

    def prompt_start_fusion(self) -> None:
        """Offer to launch fusion directly when sources and destination are ready."""
        if not self.sources or not self.destination:
            return
        reply = QMessageBox.question(
            self,
            "Iniciar Fusión",
            f"Se han configurado automáticamente {len(self.sources)} carpetas de origen y el destino:\n\n"
            f"Destino: {self.destination}\n\n"
            "¿Deseas iniciar la consolidación y copia sin duplicados ahora mismo?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._start_fusion()

    def preset_libraries(self, lib_a: Path, lib_b: Path) -> None:
        """Integration helper when navigating from the Compare page."""
        self.preset_sources([lib_a, lib_b])

    def auto_plan_from_comparison(self) -> None:
        if self.sources and self.destination:
            self._start_fusion()

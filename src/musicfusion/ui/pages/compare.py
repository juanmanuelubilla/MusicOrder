"""File/folder comparison page: run Matching + Decision engines.

Two modes:
  * files   - pick two audio files and compare them in detail;
  * folders - align two directories: pair tracks by relative path first,
    then by song identity, and report matches, differences and the tracks
    that exist only on one side.

Both reuse the Matching Engine verdict and, for same-content pairs, the
Decision Engine winner with a persisted decision record.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicfusion.audio.metadata import AUDIO_EXTENSIONS
from musicfusion.core.app import AppContext
from musicfusion.core.text import (
    album_from_folder,
    artist_from_folder,
    normalize_title,
    structure_key,
    title_from_filename,
    track_no_from_filename,
    year_from_folder,
)
from musicfusion.ui.pages._common import JobRunner
from musicfusion.ui.widgets import ActionBar, BusyRow, palette

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.compare.report import PairRecord
    from musicfusion.decision.engine import DecisionResult
    from musicfusion.matching import MatchOutcome, MatchTrack

_SAME_OK = {"MATCH_VERY_HIGH", "MATCH_HIGH", "MATCH_CONFIRMED"}
_MODE_FILES = "files"
_MODE_FOLDERS = "folders"
_MODE_BROAD = "broad"
_VERIFY_MARKS = {"ok": "=", "count_mismatch": "!", "not_found": "?", "no_artist": "?", "error": "x"}


class ComparePage(QWidget):
    """Compares two audio files or two folders of audio files."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.file_a: Path | None = None
        self.file_b: Path | None = None
        self.folder_a: Path | None = None
        self.folder_b: Path | None = None
        self._mode = _MODE_FILES
        self._pending = ""
        self._job_started: float | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Comparar")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        bar = ActionBar()
        self.btn_file_a = bar.add_button("Archivo A…", clicked=self._pick_a)
        self.btn_file_b = bar.add_button("Archivo B…", clicked=self._pick_b)
        self.btn_folder_a = bar.add_button("Carpeta A…", clicked=self._pick_folder_a)
        self.btn_folder_b = bar.add_button("Carpeta B…", clicked=self._pick_folder_b)
        self.btn_same_folder = bar.add_button("Misma carpeta…", clicked=self._pick_same_folder)
        self.btn_compare = bar.add_button("Comparar", primary=True, clicked=self._compare)
        self.btn_verify = bar.add_button("Verificar álbumes", clicked=self._verify_albums)
        self.btn_verify.setToolTip(
            "Compara tus álbumes contra MusicBrainz (y tu IA local si está activada)"
        )
        self.verify_mode = QComboBox()
        self.verify_mode.addItems(["Online (MusicBrainz)", "Rápido (solo local)"])
        self.verify_mode.setToolTip(
            "Rápido: compara tus álbumes entre sí sin internet (menos preciso).\n"
            "Online: verifica contra las tracklists oficiales de MusicBrainz."
        )
        bar.add_widget(self.verify_mode)
        bar.add_widget(QLabel(""))
        self.mode_box = QComboBox()
        self.mode_box.addItems(["Dos archivos", "Dos carpetas", "Una carpeta (duplicados)"])
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        bar.add_widget(QLabel("Qué querés comparar:"))
        bar.add_widget(self.mode_box)
        self.fp_check = QCheckBox("Usar huella de audio (más preciso)")
        self.fp_check.setChecked(True)
        bar.add_widget(self.fp_check)
        self.limit_artists_check = QCheckBox("B solo con artistas presentes en A")
        self.limit_artists_check.setChecked(True)
        self.limit_artists_check.setToolTip(
            "A es el maestro: aunque B tenga muchos otros artistas,\n"
            "solo se comparan los que ya existen en A. Lo demás de B\n"
            "queda fuera del análisis y NO se copia."
        )
        bar.add_widget(self.limit_artists_check)
        bar.add_stretch()
        layout.addWidget(bar)

        row = QHBoxLayout()
        self.card_a = _FileCard("Archivo A")
        self.card_b = _FileCard("Archivo B")
        row.addWidget(self.card_a, 1)
        row.addWidget(self.card_b, 1)
        layout.addLayout(row)

        # next-step bar sits ABOVE the results so it is never lost under a
        # long list of comparisons
        self.nav_bar = ActionBar()
        self.btn_dups = self.nav_bar.add_button(
            "Ver duplicados", clicked=lambda: self._navigate_to("Duplicados")
        )
        self.btn_fusion = self.nav_bar.add_button(
            "Fusionar esto →", primary=True, clicked=self._go_fusion
        )
        self.btn_fusion.setToolTip(
            "Lleva estas carpetas a la página Fusión y genera el plan "
            "automáticamente (solo falta elegir el DESTINO)"
        )
        self.btn_incorporate = self.nav_bar.add_button(
            "", clicked=self._apply_selected_copies
        )
        self.btn_incorporate.setToolTip(
            "Copia hacia el maestro A únicamente los archivos 'SOLO EN B', "
            "respetando la estructura de carpetas.\n"
            "Nunca sobreescribe ni borra nada existente."
        )
        self.btn_dups.setEnabled(False)
        self.btn_fusion.setEnabled(False)
        self.btn_incorporate.setEnabled(False)
        self.nav_bar.add_stretch()
        self.nav_bar.setVisible(False)
        layout.addWidget(self.nav_bar)

        self.result_box = _ResultBox()
        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.result_scroll.setWidget(self.result_box)
        self.result_scroll.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self.result_scroll, 1)

        # ---- verification results: grouped/filterable table -----------------
        verify_bar = ActionBar()
        verify_bar.add_widget(QLabel("Verificación — Agrupar por:"))
        self.verify_group_box = QComboBox()
        self.verify_group_box.addItems(["Estado", "Artista", "Árbol", "Sin grupos"])
        self.verify_group_box.currentIndexChanged.connect(self._render_verify_rows)
        verify_bar.add_widget(self.verify_group_box)
        verify_bar.add_widget(QLabel("Filtro:"))
        self.verify_filter_box = QComboBox()
        self.verify_filter_box.addItems(
            ["Todo", "OK [=]", "Discrepancia [!]", "Sin match [?]", "Solo IA"]
        )
        self.verify_filter_box.currentIndexChanged.connect(self._render_verify_rows)
        verify_bar.add_widget(self.verify_filter_box)
        self.clear_cache_btn = verify_bar.add_button(
            'Limpiar caché', clicked=self._clear_cache
        )
        self.clear_cache_btn.setToolTip(
            'Borra las caches de MusicBrainz y IA. El próximo análisis irá más lento '
            'pero considerará datos frescos.'
        )
        verify_bar.add_stretch()
        self.apply_copy_btn = verify_bar.add_button(
            "Incorporar tildadas en A…", clicked=self._apply_selected_copies
        )
        self.apply_copy_btn.setToolTip(
            "Copia SOLO las carpetas tildadas hacia el maestro A.\n"
            "Nunca sobreescribe: si el destino ya existe, se omite."
        )
        self.apply_copy_btn.setEnabled(False)
        verify_bar.add_stretch()
        self.verify_bar = verify_bar
        self.verify_bar.setVisible(False)
        layout.addWidget(self.verify_bar)

        self.verify_tree = QTreeWidget()
        self.verify_tree.setHeaderLabels(
            ["Copiar", "Estado", "Artista", "Álbum / Par", "Temas", "MusicBrainz", "Detalle"]
        )
        self.verify_tree.setRootIsDecorated(False)
        self.verify_tree.setColumnWidth(0, 60)
        self.verify_tree.setColumnWidth(1, 120)
        self.verify_tree.setColumnWidth(2, 140)
        self.verify_tree.setColumnWidth(3, 240)
        self.verify_tree.setColumnWidth(4, 80)
        self.verify_tree.setColumnWidth(5, 190)
        self.verify_bar.setVisible(False)
        self.verify_tree.setVisible(False)
        layout.addWidget(self.verify_tree, 1)

        self.busy_row = BusyRow()
        self.busy_row.message.setText("Comparando…")
        layout.addWidget(self.busy_row)

        self.status = QLabel("")
        layout.addWidget(self.status)

        self.jobs = JobRunner(self)
        self.jobs.finished.connect(self._on_job_finished)
        self.jobs.failed.connect(self._on_job_failed)
        self.jobs.progress.connect(self._on_job_progress)

        self.navigator: Callable[..., None] | None = None

        self._sync_mode()

    # ----------------------------------------------------------------- modes
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self.mode_box.blockSignals(True)
        self.mode_box.setCurrentIndex(
            {_MODE_FOLDERS: 1, _MODE_BROAD: 2}.get(mode, 0)
        )
        self.mode_box.blockSignals(False)
        self._sync_mode()

    def _on_mode_changed(self, index: int) -> None:
        self._mode = {1: _MODE_FOLDERS, 2: _MODE_BROAD}.get(index, _MODE_FILES)
        self._sync_mode()

    def _sync_mode(self) -> None:
        files_mode = self._mode == _MODE_FILES
        # only the relevant pickers stay visible -> much less visual noise
        self.btn_file_a.setVisible(files_mode)
        self.btn_file_b.setVisible(files_mode)
        self.btn_folder_a.setVisible(not files_mode)
        self.btn_folder_b.setVisible(not files_mode)
        self.btn_same_folder.setVisible(not files_mode)
        self.btn_verify.setVisible(not files_mode)
        self.limit_artists_check.setVisible(not files_mode)
        for button in (self.btn_file_a, self.btn_file_b,
                       self.btn_folder_a, self.btn_folder_b, self.btn_same_folder):
            button.setEnabled(True)
        captions = (
            ("Archivo A", "Archivo B") if files_mode else ("Carpeta A (PRINCIPAL)", "Carpeta B")
        )
        self.card_a.set_caption(captions[0])
        self.card_b.set_caption(captions[1])
        hints = {
            _MODE_FILES: (
                "Elegí los dos archivos de audio y presioná Comparar: te dice si "
                "son el mismo tema y cuál tiene mejor calidad."
            ),
            _MODE_FOLDERS: (
                "Elegí las dos carpetas y presioná Comparar: empareja sus temas "
                "(aunque cambien tildes o mayúsculas) y marca duplicados."
            ),
            _MODE_BROAD: (
                "Elegí una carpeta y MusicFusion detectará los duplicados "
                "internos comparando cada tema contra todos los demás."
            ),
        }
        self.hint.setText(hints.get(self._mode, ""))

    def _audio_extensions(self) -> set[str]:
        configured = self.context.settings.get("scan.include_extensions", None)
        if configured:
            return {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in configured}
        return set(AUDIO_EXTENSIONS)

    # ----------------------------------------------------------------- selects
    def _pick_a(self) -> None:
        self._pick_into_file("a")

    def _pick_b(self) -> None:
        self._pick_into_file("b")

    def _pick_into_file(self, which: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Elegir archivo de audio",
            filter="Audio (*.flac *.mp3 *.ogg *.opus *.m4a *.aac *.wav *.wma *.aiff)",
        )
        if not path:
            return
        chosen = Path(path)
        self._set_mode(_MODE_FILES)
        if which == "a":
            self.file_a = chosen
            self.card_a.set_file(chosen)
        else:
            self.file_b = chosen
            self.card_b.set_file(chosen)

    def _pick_folder_a(self) -> None:
        self._pick_into_folder("a")

    def _pick_folder_b(self) -> None:
        self._pick_into_folder("b")

    def _pick_same_folder(self) -> None:
        """Choose ONE folder used as both A and B -> detects internal duplicates."""
        path = QFileDialog.getExistingDirectory(
            self, "Elegir carpeta para detectar duplicados internos"
        )
        if not path:
            return
        chosen = Path(path)
        self.folder_a = chosen
        self.folder_b = chosen
        self.card_a.set_file(chosen)
        self.card_b.set_file(chosen)
        self.status.setText("Misma carpeta en A y B: se detectarán los duplicados internos.")

    def _pick_into_folder(self, which: str) -> None:
        path = QFileDialog.getExistingDirectory(self, "Elegir carpeta de música")
        if not path:
            return
        chosen = Path(path)
        self._set_mode(_MODE_FOLDERS)
        if which == "a":
            self.folder_a = chosen
            self.card_a.set_file(chosen)
        else:
            self.folder_b = chosen
            self.card_b.set_file(chosen)

    # ----------------------------------------------------------------- compare
    def set_navigator(self, navigator: Callable[..., None]) -> None:
        """Allow the page to jump to other sections (Duplicados / Fusión)."""
        self.navigator = navigator

    def _navigate_to(self, page: str) -> None:
        if self.navigator is not None:
            self.navigator(page)

    def _go_fusion(self) -> None:
        """Send both compared folders to Fusión and auto-generate the plan."""
        if self.navigator is None:
            return
        self.navigator("Fusión", (self.folder_a, self.folder_b), auto_plan=True)

    def _compare(self) -> None:
        if self._mode == _MODE_FILES and self.file_a and self.file_b:
            self._pending = _MODE_FILES
            self._start_job()
            self.jobs.run(self._files_job, self.file_a, self.file_b)
            return
        if self._mode in (_MODE_FOLDERS, _MODE_BROAD) and self.folder_a and self.folder_b:
            self._pending = self._mode
            self._start_job()
            limit = self.limit_artists_check.isChecked()
            if self._mode == _MODE_BROAD or self.folder_a.resolve() == self.folder_b.resolve():
                self.jobs.run(self._broad_job, self.folder_a, self.folder_b, limit)
            else:
                self.jobs.run(self._folders_job, self.folder_a, self.folder_b, limit)
            return
        if self._mode == _MODE_FILES:
            self.status.setText("Selecciona el archivo A y el archivo B primero.")
        else:
            self.status.setText("Selecciona la carpeta A y la carpeta B primero.")

    def _show_nav(self, *, fusion: bool, dups: bool, why_disabled: str = "") -> None:
        """Central control of the next-step bar (always ABOVE the results)."""
        self.nav_bar.setVisible(True)
        self.btn_fusion.setEnabled(fusion)
        self.btn_dups.setEnabled(dups)
        tip_fusion = (
            "Lleva estas carpetas a la página Fusión y genera el plan "
            "automáticamente (solo falta elegir el DESTINO)"
            if fusion else why_disabled or "Todavía no hay nada que fusionar"
        )
        tip_dups = "" if dups else why_disabled or "Todavía no hay duplicados guardados"
        self.btn_fusion.setToolTip(tip_fusion)
        self.btn_dups.setToolTip(tip_dups)

    def _start_job(self) -> None:
        self._job_started = time.monotonic()
        self._set_busy(True)
        self.status.setText("")
        self.nav_bar.setVisible(False)

    def _set_busy(self, busy: bool, message: str = "Comparando…") -> None:
        self.btn_compare.setEnabled(not busy)
        self.btn_verify.setEnabled(not busy)
        if busy:
            self.busy_row.start(message)
        else:
            self.busy_row.stop()

    def _on_job_finished(self, result) -> None:
        self._set_busy(False)
        if self._pending == _MODE_FILES:
            self._render_files(result)
        elif self._pending in (_MODE_FOLDERS, _MODE_BROAD):
            self._render_folders(result)
            # folder comparisons keep using the plain text box
            self.verify_bar.setVisible(False)
            self.verify_tree.setVisible(False)
            self.result_box.setVisible(True)
            self.result_scroll.setVisible(True)
        elif self._pending == self._PENDING_INCORPORATE:
            from PySide6.QtWidgets import QMessageBox as _QMB

            msg = (
                f"Incorporados {result['files_copied']} archivos "
                f"({result['bytes_copied'] / (1024*1024):.1f} MiB) en A."
            )
            if result.get("skipped_existing"):
                msg += f" Omitidos por existir: {len(result['skipped_existing'])}."
            if result.get("errors"):
                msg += f" Errores: {len(result['errors'])}."
            self.status.setText(msg)
            self.btn_incorporate.setEnabled(False)
            _QMB.information(self, "Incorporar faltantes", msg)
        elif self._pending == self._PENDING_VERIFY:
            rows = result[0] if isinstance(result, tuple) else []
            saved = bool(result[1]) if isinstance(result, tuple) else False
            if isinstance(result, dict) and "copied" in result:
                # terminó la copia de las tildadas: resumir sin limpiar tabla
                msg = (
                    f"Incorporadas {len(result['copied'])} carpetas "
                    f"({result['files_copied']} archivos)."
                )
                if result.get("skipped_existing"):
                    msg += f" Omitidas por existir: {len(result['skipped_existing'])}."
                if result.get("errors"):
                    msg += f" Errores: {len(result['errors'])}."
                self.status.setText(msg)
                from PySide6.QtWidgets import QMessageBox as _QMB

                _QMB.information(self, "Incorporar selección", msg)
                return
            self._render_verify(rows, saved)
            self.status.setText(
                "Verificación completa. "
                f"{sum(1 for r in rows if r['type'] == 'album')} álbumes, "
                f"{sum(1 for r in rows if r['type'] == 'ai')} veredictos IA."
            )
        self._pending = ""

    def _on_job_progress(self, step: int, total: int, message: str) -> None:
        if total > 0:
            percent = 100.0 * step / total
            eta = None
            if step > 0 and self._job_started is not None:
                elapsed = max(0.0, time.monotonic() - self._job_started)
                eta = elapsed * max(0, total - step) / step
            self.busy_row.set_progress(percent, eta)
            if message:
                self.busy_row.update_message(message)

    def _on_job_failed(self, message: str) -> None:
        self._set_busy(False)
        self._pending = ""
        self.status.setText(f"Error: {message}")

    def _clear_cache(self) -> None:

        from PySide6.QtWidgets import QMessageBox as _QMB

        settings = self.context.settings
        cache_a = settings.config_dir / 'mb_albums_cache.json'
        cache_ai = settings.config_dir / 'mb_ai_verdicts.json'
        deleted = []
        if cache_a.exists():
            cache_a.unlink()
            deleted.append('cache de MusicBrainz')
        if cache_ai.exists():
            cache_ai.unlink()
            deleted.append('cache de IA')
        msg = 'Cachés borrados: ' + ', '.join(deleted) if deleted else 'Ningún cache encontrado'
        _QMB.information(self, 'Cache borrado', msg)
        # If we are on a verification/comparison page, re-trigger analysis
        pending = getattr(self, "_pending", "")
        if pending == self._PENDING_VERIFY:
            self._verify_albums()
        elif pending in (_MODE_FOLDERS, _MODE_BROAD):
            self._compare()

    # ------------------------------------------------------- single file mode
    def _files_job(self, file_a: Path, file_b: Path, on_progress=None) -> _PairResult:
        """Runs in the worker thread; returns the analysis result."""
        if callable(on_progress):
            on_progress(0, 2, "Analizando archivo A…")
        result = self._analyze_pair(file_a, file_b)
        if callable(on_progress):
            on_progress(2, 2, "Comparación completa")
        return result

    def _render_files(self, result: _PairResult) -> None:
        self.card_a.set_details(result.info_a, result.qual_a)
        self.card_b.set_details(result.info_b, result.qual_b)
        self.result_box.show_result(
            result.outcome,
            quality_of(result.qual_a),
            quality_of(result.qual_b),
            result.decision_text,
        )
        self.status.setText(
            f"Score {result.outcome.score:.1f} · {result.outcome.classification} · "
            f"same_song={result.outcome.same_song} · same_version={result.outcome.same_version}"
        )

    # ------------------------------------------------------- folder mode
    def _broad_job(
        self, root_a: Path, root_b: Path,
        restrict_artists: bool = False, on_progress=None,
    ) -> _FoldersResult:
        """Whole-content comparison: index both folders and compare every pair."""
        from musicfusion.compare.broad import compare_all

        result = compare_all(
            self.context.settings,
            self.context.db,
            root_a,
            root_b,
            use_fingerprints=self._use_fingerprints(),
            on_progress=on_progress,
            restrict_b_to_master_artists=restrict_artists,
        )
        categorized: list[tuple[str, str]] = []
        if result.filtered_out_b:
            categorized.append((
                "DISTINTAS",
                f"(filtro maestro: {result.filtered_out_b} archivos de B "
                "excluidos por artista inexistente en A)",
            ))
        for record in result.pairs:
            same = record.same
            mark = "==" if same else "!="
            winner = self._winner_of_report(record)
            line = (
                f"{self._rel(record.path_a, root_a)}  {mark}  "
                f"{self._rel(record.path_b, root_b)}  |  "
                f"{record.outcome.score:.1f}% · {record.outcome.classification}"
                f"{' · ' + winner if winner else ''}"
            )
            category = self._categorize(
                record.outcome, record.decision, record.path_a, record.path_b
            )
            categorized.append((category, line))

        grouped = self._grouped_lines(
            categorized, root_a, root_b, list(result.only_a), list(result.only_b)
        )

        saved = ""
        if result.saved:
            stats = result.saved
            saved = (
                f"guardado: {stats['pairs']} pares, {stats['duplicates']} duplicados, "
                f"{stats['decisions']} decisiones · por sha {result.by_sha256} · "
                f"por huella {result.by_fingerprint} · por metadatos {result.by_metadata}"
            )
        rows = self._build_folder_rows(
            categorized, root_a, root_b,
            [(r.path_a, r.path_b) for r in result.pairs],
            list(result.only_a), list(result.only_b),
        )
        return _FoldersResult(
            rows=rows,
            lines="\n".join(
                grouped if categorized or result.only_a or result.only_b
                else ["No hay coincidencias de contenido."]
            ),
            pairs=result.pairs_count,
            matched=result.matched,
            errors=result.errors,
            only_a=len(result.only_a),
            only_b=len(result.only_b),
            saved=saved,
            only_a_paths=list(result.only_a),
            only_b_paths=list(result.only_b),
        )

    @staticmethod
    def _winner_of_report(record) -> str:
        decision = getattr(record, "decision", None)
        if decision is None:
            return ""
        return Path(getattr(decision.winner, "path", "")).stem

    def _use_fingerprints(self) -> bool:
        if not self.fp_check.isChecked():
            return False
        from musicfusion.audio.fingerprint import fingerprint_available

        return fingerprint_available()

    # ------------------------------------------------------- grouped output
    _GROUP_ORDER = (
        "IDENTICOS", "GANA_A", "GANA_B", "VERSIONES", "DISTINTAS",
    )
    _GROUP_HEADERS = {
        "IDENTICOS": "idénticos — nada que hacer",
        "GANA_A": "gana A (principal) — se conserva",
        "GANA_B": "gana B — REVISAR: conviene reemplazar en A",
        "VERSIONES": "misma canción, versión distinta — revisar",
        "DISTINTAS": "temas distintos",
    }

    @classmethod
    def _categorize(cls, outcome, decision, path_a, path_b) -> str:
        """Group key for one compared pair (A = colección PRINCIPAL)."""
        same_song = bool(outcome.same_song) or outcome.classification in _SAME_OK
        if outcome.version_different or not same_song:
            return "VERSIONES" if outcome.same_song else "DISTINTAS"
        if outcome.score >= 99.5 and outcome.fingerprint_score >= 100:
            return "IDENTICOS"
        winner_path = getattr(getattr(decision, "winner", None), "path", "")
        if winner_path:
            return "GANA_B" if winner_path == str(path_b) else "GANA_A"
        return "IDENTICOS" if outcome.score >= 95 else "GANA_A"

    def _grouped_lines(
        self,
        categorized: list[tuple[str, str]],
        root_a: Path,
        root_b: Path,
        only_a: list[Path],
        only_b: list[Path],
    ) -> list[str]:
        """Render results GROUPED by match type, A = principal (spec usuario)."""
        sections: dict[str, list[str]] = {key: [] for key in self._GROUP_ORDER}
        for category, line in categorized:
            sections.setdefault(category, []).append(line)

        counts = {key: len(items) for key, items in sections.items()}
        summary = " · ".join(
            f"{key.lower()}={counts.get(key, 0)}" for key in self._GROUP_ORDER
        )
        lines = [f"== RESUMEN (A = PRINCIPAL) == {summary}"]

        for key in self._GROUP_ORDER:
            items = sections.get(key) or []
            if not items:
                continue
            lines.append("")
            lines.append(f"── {self._GROUP_HEADERS[key].upper()} ({len(items)}) ──")
            lines.extend(f"  {text}" for text in items)

        if only_a:
            lines.append("")
            lines.append(f"── SOLO EN A (PRINCIPAL) ({len(only_a)}) — OK ──")
            lines.extend(f"  {self._rel(path, root_a)}" for path in only_a)
        if only_b:
            lines.append("")
            lines.append(
                f"── SOLO EN B → COPIAR HACIA A ({len(only_b)}) ──"
            )
            lines.extend(f"  {self._rel(path, root_b)}" for path in only_b)
        return lines

    def _folders_job(
        self, root_a: Path, root_b: Path,
        restrict_artists: bool = False, on_progress=None,
    ) -> _FoldersResult:
        """Runs in the worker thread; pairs, analyzes and persists everything."""
        pairs, only_a, only_b = self._pair_folders(root_a, root_b)
        records: list[PairRecord] = []
        categorized: list[tuple[str, str]] = []
        matched = 0
        errors = 0
        if callable(on_progress):
            on_progress(0, max(1, len(pairs)), "Comparando pares…")
        for index, (path_a, path_b) in enumerate(pairs, start=1):
            if callable(on_progress):
                on_progress(index, max(1, len(pairs)), f"Comparando par {index}/{len(pairs)}…")
            try:
                result = self._analyze_pair(path_a, path_b)
            except Exception as exc:  # noqa: BLE001 - report one broken file pair
                errors += 1
                logging.getLogger(__name__).warning(
                    "pair failed: %s vs %s", path_a, path_b, exc_info=exc
                )
                categorized.append((
                    "DISTINTAS",
                    f"!! {self._rel(path_a, root_a)} vs "
                    f"{self._rel(path_b, root_b)} -> ERROR {exc}",
                ))
                continue
            same = (
                result.outcome.same_song or result.outcome.classification in _SAME_OK
            ) and not result.outcome.version_different
            if same:
                matched += 1
            mark = "==" if same else "!="
            winner = self._winner_of(result.decision_text)
            line = (
                f"{self._rel(path_a, root_a)}  {mark}  {self._rel(path_b, root_b)}  |  "
                f"{result.outcome.score:.1f}% · {result.outcome.classification}"
                f"{' · ' + winner if winner else ''}"
            )
            category = self._categorize(
                result.outcome, result.decision, path_a, path_b
            )
            categorized.append((category, line))
            if result.track_a and result.track_b:
                from musicfusion.compare.report import PairRecord

                records.append(
                    PairRecord(
                        path_a=path_a,
                        path_b=path_b,
                        outcome=result.outcome,
                        track_a=result.track_a,
                        track_b=result.track_b,
                        qual_a=result.qual_a,
                        qual_b=result.qual_b,
                        decision=result.decision if same else None,
                        same=same,
                    )
                )

        saved = ""
        if records:
            if callable(on_progress):
                on_progress(len(pairs), len(pairs), "Guardando resultados…")
            from musicfusion.compare.report import persist_folder_compare

            stats = persist_folder_compare(
                self.context.settings, self.context.db, [root_a, root_b], records
            )
            saved = (
                f"guardado: {stats['pairs']} pares, "
                f"{stats['duplicates']} duplicados, {stats['decisions']} decisiones"
            )
        if getattr(self, "_filtered_out_b", 0):
            categorized.insert(0, (
                "DISTINTAS",
                f"(filtro maestro: {self._filtered_out_b} archivos de B "
                "excluidos por artista inexistente en A)",
            ))
        grouped = self._grouped_lines(categorized, root_a, root_b, only_a, only_b)
        rows = self._build_folder_rows(
            categorized, root_a, root_b, pairs, only_a, only_b
        )
        return _FoldersResult(
            rows=rows,
            lines="\n".join(grouped if categorized or only_a or only_b
                            else ["No se encontraron pares."]),
            pairs=len(pairs),
            matched=matched,
            errors=errors,
            only_a=len(only_a),
            only_b=len(only_b),
            saved=saved,
            only_a_paths=list(only_a),
            only_b_paths=list(only_b),
        )

    def _render_folders(self, result: _FoldersResult) -> None:
        self._filter_kind = "folders"
        self._render_folder_result(result, saved=bool(result.saved))
        parts = [
            f"{result.pairs} pares · {result.matched} coinciden · "
            f"{result.pairs - result.matched} difieren · "
            f"{result.only_a} solo en A · {result.only_b} solo en B"
        ]
        if result.errors:
            parts.append(f"errores {result.errors}")
        if result.saved:
            parts.append(result.saved)
        self.status.setText(" · ".join(parts))
        self._last_only_b = list(getattr(result, "only_b_paths", []) or [])

    @staticmethod
    def _winner_of(decision_text: str) -> str:
        for line in decision_text.splitlines():
            if line.startswith("WINNER:"):
                return Path(line.split("WINNER:", 1)[1].strip()).stem
        return ""

    @staticmethod
    def _rel(path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    # ---------------------------------------------------------- pairing logic
    def _index_folder(self, root: Path) -> dict[str, list[Path]]:
        """Index audio files by a structure key of their relative path.

        The key folds accents, case and punctuation on every component
        (``Babasónicos/1992 - Pasto/01 - Intro.ogg`` ==
        ``Babasonicos/1992 - Pasto/01 - Intro.ogg``) so parallel trees that
        only differ in diacritics still pair 1:1.
        """
        index: dict[str, list[Path]] = defaultdict(list)
        extensions = self._audio_extensions()
        if not root.is_dir():
            return {key: list(value) for key, value in index.items()}
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if Path(filename).suffix.lower() not in extensions:
                    continue
                full = Path(dirpath) / filename
                rel = full.relative_to(root)
                index[structure_key(rel.parts)].append(full)
        return {key: list(value) for key, value in index.items()}

    def _track_key(self, path: Path) -> str:
        from musicfusion.audio.metadata import read_metadata

        info = read_metadata(path)
        if info and (info.title or info.artist):
            return "|".join([normalize_title(info.artist or ""), normalize_title(info.title or "")])
        return "|~|" + normalize_title(path.stem)

    def _pair_folders(self, root_a: Path, root_b: Path):
        index_a = self._index_folder(root_a)
        index_b = self._index_folder(root_b)
        if self.limit_artists_check.isChecked():
            index_b, self._filtered_out_b = self._restrict_to_master_artists(
                index_a, index_b
            )
        else:
            self._filtered_out_b = 0
        pairs: list[tuple[Path, Path]] = []
        lonely_a: list[Path] = []
        lonely_b: list[Path] = []

        shared = set(index_a) & set(index_b)
        for rel in shared:
            list_a, list_b = index_a[rel], index_b[rel]
            many = min(len(list_a), len(list_b))
            pairs.extend(zip(list_a[:many], list_b[:many]))
            lonely_a.extend(list_a[many:])
            lonely_b.extend(list_b[many:])
        for rel in index_a:
            if rel not in index_b:
                lonely_a.extend(index_a[rel])
        for rel in index_b:
            if rel not in index_a:
                lonely_b.extend(index_b[rel])

        remaining_a = {id(path): path for path in lonely_a}
        remaining_b = {id(path): path for path in lonely_b}
        keys_a: dict[str, list[int]] = defaultdict(list)
        keys_b: dict[str, list[int]] = defaultdict(list)
        for _id, path in remaining_a.items():
            keys_a[self._track_key(path)].append(_id)
        for _id, path in remaining_b.items():
            keys_b[self._track_key(path)].append(_id)
        for key in sorted(set(keys_a) & set(keys_b)):
            matched_a = keys_a[key]
            matched_b = keys_b[key]
            many = min(len(matched_a), len(matched_b))
            for _id_a, _id_b in zip(matched_a[:many], matched_b[:many]):
                pairs.append((remaining_a.pop(_id_a), remaining_b.pop(_id_b)))

        only_a = sorted(remaining_a.values(), key=str)
        only_b = sorted(remaining_b.values(), key=str)
        return pairs, only_a, only_b

    @staticmethod
    def _restrict_to_master_artists(
        index_a: dict[str, list[Path]],
        index_b: dict[str, list[Path]],
    ) -> tuple[dict[str, list[Path]], int]:
        """Keep only B entries whose ARTIST folder exists in A (normalized).

        A is the master: extra artists living only in B are excluded from the
        whole analysis so nothing from them can ever be copied.
        """
        from musicfusion.matching.normalize import normalize_music_text

        artists_a = {
            normalize_music_text(key.split("/", 1)[0]) for key in index_a
        }
        kept: dict[str, list[Path]] = {}
        dropped = 0
        for key, paths in index_b.items():
            artist_key = normalize_music_text(key.split("/", 1)[0])
            if artist_key in artists_a:
                kept[key] = paths
            else:
                dropped += len(paths)
        return kept, dropped

    # ------------------------------------------- incorporate missing B -> A
    _PENDING_INCORPORATE = "incorporate_b_a"

    def _incorporate_b_into_a(self) -> None:
        """Copy ONLY the files marked 'SOLO EN B' into master A (additive)."""
        files = list(getattr(self, "_last_only_b", []) or [])
        if not files:
            self.status.setText("No hay archivos 'Solo en B' para incorporar.")
            return
        root_b = self.folder_b
        if not root_b or not self.folder_a:
            return
        sample = "\n".join(
            f"• {p.relative_to(root_b)}" for p in files[:12]
        )
        more = "" if len(files) <= 12 else f"\n… y {len(files) - 12} más"
        answer = QMessageBox.question(
            self, "Incorporar faltantes de B en A",
            f"Se copiarán {len(files)} archivos hacia:\n{self.folder_a}\n\n"
            + sample + more
            + "\n\nSe respeta la estructura de carpetas. Nada existente se "
              "toca. ¿Continuar?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._pending = self._PENDING_INCORPORATE
        self._set_busy(True, "Incorporando faltantes…")
        self.jobs.run(self._incorporate_job, self.folder_a, root_b, files)

    @staticmethod
    def _incorporate_job(root_a: Path, root_b: Path, files: list) -> dict:
        import shutil

        copied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        n_files = n_bytes = 0
        for path in files:
            rel = path.relative_to(root_b)
            dest = root_a / rel
            if dest.exists():
                skipped.append(str(dest))
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                copied.append(str(dest))
                n_files += 1
                n_bytes += path.stat().st_size
            except OSError as exc:
                errors.append(f"{path} -> {dest}: {exc}")
        return {
            "copied": copied, "skipped_existing": skipped, "errors": errors,
            "files_copied": n_files, "bytes_copied": n_bytes,
        }

    def _build_folder_rows(
        self,
        categorized: list[tuple[str, str]],
        root_a: Path,
        root_b: Path,
        pairs: list,
        only_a: list[Path],
        only_b: list[Path],
    ) -> list[dict]:
        """Structured rows for the interactive grouped table (A = principal).

        Only 'SOLO EN B' rows are copiable (checkbox); everything else is
        informational.
        """
        rows: list[dict] = []
        pair_by_b = {str(pb): pa for pa, pb in pairs}
        for category, line in categorized:
            if category in {"IDENTICOS", "GANA_A", "GANA_B", "VERSIONES", "DISTINTAS"}:
                # recover the B path from the line text is fragile; instead we
                # attach identity from the pairing map below via solo entries.
                rows.append({
                    "type": "pair", "status": category, "text": line,
                    "artist": "", "album": "",
                })
        for path in only_a:
            parts = path.relative_to(root_a).parts
            rows.append({
                "type": "album", "tree": "A", "status": "SOLO_A",
                "artist": parts[0] if len(parts) > 1 else "",
                "album": "/".join(parts[1:]) or path.name,
                "path": str(path), "master_root": str(root_a),
                "rel": str(path.relative_to(root_a)),
                "copiable": False,
            })
        for path in only_b:
            rel_parts = path.relative_to(root_b).parts
            artist = rel_parts[0] if len(rel_parts) > 1 else ""
            album_rel = "/".join(rel_parts[1:]) or path.name
            rows.append({
                "type": "album", "tree": "B", "status": "SOLO_B",
                "artist": artist, "album": album_rel,
                "path": str(path), "master_root": str(root_a),
                "rel": str(path.relative_to(root_b)),
                "copiable": True,
            })
        return rows

    def _render_folder_result(self, result: _FoldersResult, saved: bool) -> None:
        """Folder comparisons use the same interactive table as verification."""
        self._verify_rows = list(result.rows)
        self._filter_kind = "folders"
        self.verify_filter_box.blockSignals(True)
        self.verify_filter_box.clear()
        self.verify_filter_box.addItems([
            "Todo", "Idénticos", "Gana A", "Gana B (revisar)",
            "Versión/distinta", "Solo en B (copiar)", "Solo en A",
        ])
        self.verify_filter_box.blockSignals(False)
        self.result_box.setVisible(False)
        self.result_scroll.setVisible(False)
        self.verify_bar.setVisible(True)
        self.verify_tree.setVisible(True)
        self._show_nav(fusion=True, dups=saved)
        self.btn_incorporate.setText(
            f"Incorporar tildadas en A ({len(result.only_b_paths)})"
        )
        self.btn_incorporate.setEnabled(bool(result.only_b_paths))
        self._render_verify_rows()

    # ------------------------------------------------- album verification
    _PENDING_VERIFY = "verify"

    def _verify_albums(self) -> None:
        """Verify the selected tree(s) against MusicBrainz (+ optional AI)."""
        if not self.folder_a:
            self.status.setText("Selecciona la Carpeta A (raíz Artista/Álbum) primero.")
            return
        root_b = None
        if self.folder_b and self.folder_b.resolve() != self.folder_a.resolve():
            root_b = self.folder_b
        offline = self.verify_mode.currentIndex() == 1
        self._pending = self._PENDING_VERIFY
        self._start_job()
        self.status.setText("")
        self.jobs.run(self._verify_job, self.folder_a, root_b, offline)

    def _verify_job(
        self, root_a: Path, root_b: Path | None, offline: bool = False, on_progress=None
    ) -> tuple[list[dict], bool]:
        """Runs in the worker thread; returns structured ROWS for the table.

        Row shapes:
          album rows: {type:"album", tree, artist, album, status, local, mb,
                       matched, detail}
          ai rows:    {type:"ai", artist, pair, verdict, conf, reason}
        """
        from musicfusion.services.albumverify import (
            AlbumCache,
            ai_pair_verdicts,
            compare_local_trees,
            find_similar_album_pairs,
            verify_album_tree,
        )
        from musicfusion.services.musicbrainz import MusicBrainzClient
        from musicfusion.services.ollamalocal import configured_client
        from musicfusion.services.wikipedia import WikipediaClient

        settings = self.context.settings
        rows: list[dict] = []

        if offline:
            groups = compare_local_trees(root_a, root_b)
            for group in groups:
                rows.append({
                    "type": "album",
                    "tree": "-",
                    "artist": group["artist"],
                    "album": f"{group['folder_a']}  <->  {group['folder_b']}",
                    "status": group["kind"],
                    "local": group.get("tracks", ""),
                    "mb": "",
                    "matched": f"{group['confidence']:.0f}%",
                    "detail": group["detail"],
                })
            if callable(on_progress):
                on_progress(1, 1, "Comparación local completa")
            return rows, False

        cache = AlbumCache(settings.config_dir / "mb_albums_cache.json")
        mb_client = MusicBrainzClient()
        wiki_client = WikipediaClient()

        if callable(on_progress):
            on_progress(0, 3, "Verificando árbol A contra MusicBrainz…")
        results_a = verify_album_tree(root_a, client=mb_client, cache=cache)
        results_b = []
        if root_b is not None:
            if callable(on_progress):
                on_progress(1, 3, "Verificando árbol B contra MusicBrainz…")
            results_b = verify_album_tree(root_b, client=mb_client, cache=cache)

        for label, results in (("A", results_a), ("B", results_b)):
            if root_b is None and label == "B":
                continue
            for result in results:
                official = "-" if result.mb_tracks is None else str(result.mb_tracks)
                matched = (
                    f"'{result.mb_title}' ({result.mb_year or '?'})"
                    if result.matched else ""
                )
                rows.append({
                    "type": "album",
                    "tree": label,
                    "artist": result.artist,
                    "album": result.album_name,
                    "status": _VERIFY_MARKS.get(result.status, "?"),
                    "local": str(result.local_tracks),
                    "mb": official,
                    "matched": matched,
                    "detail": "" if result.status == "ok" else result.detail,
                    "path": str(result.folder),
                    "master_root": str(root_a),
                    # álbumes del árbol ENTRANTE son candidatos a incorporar
                    "copiable": label != "A",
                })

        pairs = find_similar_album_pairs(results_a, results_b if root_b else None)
        if pairs and settings.get("ollama.enabled", False):
            ai_client = configured_client(settings)
            if ai_client.available():
                if callable(on_progress):
                    on_progress(2, 3, f"IA ({ai_client.model}): {len(pairs)} pares…")
                verdicts = ai_pair_verdicts(
                    pairs,
                    ai_client=ai_client,
                    mb_client=mb_client,
                    wiki_client=wiki_client,
                    verdict_cache=settings.config_dir / "mb_ai_verdicts.json",
                )
                for left, right, verdict, cached in verdicts:
                    rows.append({
                        "type": "ai",
                        "tree": "IA",
                        "artist": left.artist,
                        "album": f"'{left.album_name}' vs '{right.album_name}'",
                        "status": "IA",
                        "local": "",
                        "mb": "",
                        "matched": (
                            f"{verdict.relationship} ({verdict.confidence:.0f}%)"
                            + (" cache" if cached else "")
                            if verdict else "sin veredicto"
                        ),
                        "detail": verdict.reason if verdict else "",
                    })
        saved_any = any(
            bool(getattr(r, "saved", None))
            for r in [*results_a, *results_b]
        )
        if callable(on_progress):
            on_progress(3, 3, "Verificación completa")
        return rows, saved_any

    # ------------------------------------------------- verify table rendering
    _VERIFY_STATUS_LABELS = {
        "=": "OK [=]",
        "!": "DISCREPANCIA [!]",
        "?": "SIN MATCH [?]",
        "x": "ERROR [x]",
        "IA": "VEREDICTOS IA",
        "DUPLICADO_PROBABLE": "DUPLICADO PROBABLE",
        "POSIBLE_EDICION": "POSIBLE EDICIÓN",
        "REVISAR_MISMO_NOMBRE": "MISMO NOMBRE, CONTENIDO DISTINTO",
        "POSIBLE_EQUIVALENTE": "POSIBLE EQUIVALENTE",
        # comparación de carpetas
        "IDENTICOS": "IDÉNTICOS — nada que hacer",
        "GANA_A": "GANA A (principal) — se conserva",
        "GANA_B": "GANA B — REVISAR reemplazo en A",
        "VERSIONES": "MISMA CANCIÓN, VERSIÓN DISTINTA",
        "DISTINTAS": "TEMAS DISTINTOS / ERRORES",
        "SOLO_A": "SOLO EN A (PRINCIPAL) — OK",
        "SOLO_B": "SOLO EN B → COPIAR HACIA A",
    }

    def _render_verify(self, rows: list[dict], saved: bool) -> None:
        self._verify_rows = rows
        self.result_box.setVisible(False)
        self.result_scroll.setVisible(False)
        self.verify_bar.setVisible(True)
        self.verify_tree.setVisible(True)
        self._show_nav(fusion=True, dups=saved)
        self._render_verify_rows()

    def _render_verify_rows(self) -> None:
        import collections

        rows = getattr(self, "_verify_rows", [])
        group_mode = max(0, self.verify_group_box.currentIndex())   # estado/artista/árbol/plano
        filter_idx = max(0, self.verify_filter_box.currentIndex())

        folders_mode = getattr(self, "_filter_kind", "verify") == "folders"
        folder_map = {
            1: {"IDENTICOS"}, 2: {"GANA_A"}, 3: {"GANA_B"},
            4: {"VERSIONES", "DISTINTAS"}, 5: {"SOLO_B"}, 6: {"SOLO_A"},
        }
        def passes(row: dict) -> bool:
            if folders_mode:
                if filter_idx == 0:
                    return True
                return row.get("status") in folder_map.get(filter_idx, set())
            if row["type"] == "ai":
                return filter_idx in (0, 4)
            mark = row["status"]
            if filter_idx == 0:
                return True
            if filter_idx == 1:
                return mark == "="
            if filter_idx == 2:
                return mark == "!"
            if filter_idx == 3:
                return mark == "?"
            return False

        visible = [r for r in rows if passes(r)]

        def group_key(row: dict) -> tuple[str, str]:
            if group_mode == 0:      # estado
                if row["type"] == "ai":
                    return ("1", self._VERIFY_STATUS_LABELS.get("IA", "IA"))
                order = {"=": "0", "!": "1", "?": "2", "x": "3"}
                label = self._VERIFY_STATUS_LABELS.get(row["status"], row["status"])
                return (order.get(row["status"], "4"), str(label))
            if group_mode == 1:      # artista
                return ("0", row.get("artist") or "(sin artista)")
            if group_mode == 2:      # árbol
                if row["type"] == "ai":
                    return ("1", "IA")
                return ("0", f"Árbol {row.get('tree') or '-'}")
            return ("0", "")         # sin grupos

        self.verify_tree.clear()
        self._verify_items: list[tuple[QTreeWidgetItem, dict | None]] = []
        grouped: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
        for row in visible:
            grouped[group_key(row)].append(row)

        color_map = {"=": "#69db7c", "!": "#e8b34b", "?": "#8a8f98", "x": "#ff6b6b"}
        in_master = {
            self._identity_key(r.get("artist", ""), r.get("album", ""))
            for r in rows
            if r["type"] == "album" and r.get("tree") == "A"
        }
        for key in sorted(grouped):
            items = grouped[key]
            header = QTreeWidgetItem([
                f"{key[1]} ({len(items)})" if key[1] else f"Total ({len(items)})",
                "", "", "", "", "", "",
            ])
            header.setForeground(0, QColor("#5aa9e6"))
            header.setFlags(header.flags()
                            & ~Qt.ItemFlags.ItemIsUserCheckable
                            & ~Qt.ItemFlags.ItemIsSelectable)
            self.verify_tree.addTopLevelItem(header)
            self._verify_items.append((header, None))
            self.verify_tree.blockSignals(True)
            for row in sorted(items, key=lambda r: r.get("album", "")):
                is_album = row["type"] == "album"
                item = QTreeWidgetItem([
                    "",
                    row["status"] if is_album else "IA",
                    row.get("artist", ""),
                    row.get("album", ""),
                    f"{row.get('local', '')} | {row.get('mb', '')}".strip(" |"),
                    row.get("matched", ""),
                    row.get("detail", ""),
                ])
                mark_color = color_map.get(row["status"], "#cfd3d9")
                item.setForeground(1, QColor(mark_color))
                if row["type"] == "ai":
                    item.setForeground(1, QColor("#b085f5"))
                copiable = is_album and bool(row.get("copiable")) and row.get("path")
                if copiable:
                    already = False
                    item.setCheckState(0, Qt.CheckState.Unchecked if already
                                       else Qt.CheckState.Checked)
                    if already:
                        item.setToolTip(0, "Ya existe un álbum equivalente en A")
                else:
                    # filas de A y de IA no son copiables
                    item.setFlags(item.flags() & ~Qt.ItemFlags.ItemIsUserCheckable)
                self.verify_tree.addTopLevelItem(item)
                self._verify_items.append((item, row))
            self.verify_tree.blockSignals(False)
        self._on_verify_item_changed(None, 0)

    # --------------------------------------------------- selected incorporation
    @staticmethod
    def _identity_key(artist: str, album: str) -> tuple[str, str]:
        """Normalized identity ignoring accents/case AND leading years."""
        import re

        from musicfusion.matching.normalize import normalize_music_text

        def clean(value):
            value = normalize_music_text(value)
            return re.sub(r"^(?:19|20)\d{2}\s*[-–]?\s*", "", value).strip()

        return clean(artist), clean(album)

    def _selected_copy_rows(self) -> list[dict]:
        """Checked album rows (from non-A trees) that carry a real path."""
        selected = []
        for item, row in getattr(self, "_verify_items", []):
            if row is None or not row.get("copiable"):
                continue
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            selected.append(row)
        return selected

    def _on_verify_item_changed(self, item, column) -> None:
        if column == 0:
            count = len(self._selected_copy_rows())
            self.apply_copy_btn.setEnabled(count > 0)
            self.apply_copy_btn.setText(f"Incorporar {count} tildada(s) en A…")

    def _apply_selected_copies(self) -> None:
        selected = self._selected_copy_rows()
        if not selected:
            return
        lines = []
        for row in selected[:12]:
            src = Path(row["path"])
            dest = self._copy_destination_for(row)
            lines.append(f"• {src.parent.parent.name}/{src.name} → {dest}")
        more = "" if len(selected) <= 12 else f"\n… y {len(selected) - 12} más"
        answer = QMessageBox.question(
            self, "Confirmar incorporación",
            f"Se copiarán {len(selected)} carpetas hacia el maestro A:\n\n"
            + "\n".join(lines) + more
            + "\n\n¿Continuar? (nunca se sobreescribe nada existente)",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.jobs.run(lambda: self._perform_copy_plan(selected))

    def _copy_destination_for(self, row: dict) -> Path | None:
        src = Path(row["path"]) if row.get("path") else None
        master_root = row.get("master_root")
        if src is None or not master_root:
            return None
        if row.get("rel"):                # comparación de carpetas: misma ruta relativa
            return Path(master_root) / row["rel"]
        artist_dir = src.parent.name      # verificación: nombre de carpeta del ORIGEN
        return Path(master_root) / artist_dir / src.name

    def _perform_copy_plan(self, rows: list[dict]) -> dict:
        import shutil

        copied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        files = bytes_total = 0
        for row in rows:
            src = Path(row["path"])
            dest = self._copy_destination_for(row)
            if src is None or dest is None or not src.exists():
                errors.append(f"origen inválido: {row.get('path')}")
                continue
            if dest.exists():
                skipped.append(str(dest))
                continue
            try:
                if src.is_dir():
                    shutil.copytree(src, dest)
                    for p in dest.rglob("*"):
                        if p.is_file():
                            files += 1
                            bytes_total += p.stat().st_size
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    files += 1
                    bytes_total += src.stat().st_size
                copied.append(str(dest))
            except OSError as exc:
                errors.append(f"{src} -> {dest}: {exc}")
        return {
            "copied": copied, "skipped_existing": skipped, "errors": errors,
            "files_copied": files, "bytes_copied": bytes_total,
        }

    # ------------------------------------------------------------- analysis
    def _analyze_pair(self, path_a: Path, path_b: Path) -> _PairResult:
        from musicfusion.audio.metadata import read_metadata
        from musicfusion.audio.quality import probe_quality
        from musicfusion.matching import MatchConfig, MatchEngine

        info_a = read_metadata(path_a)
        info_b = read_metadata(path_b)
        fp_a = fp_b = ""
        if info_a is not None and info_b is not None and self._use_fingerprints():
            from musicfusion.audio.fingerprint import fingerprint_file

            result_a = fingerprint_file(str(path_a))
            result_b = fingerprint_file(str(path_b))
            fp_a = result_a.fingerprint if result_a else ""
            fp_b = result_b.fingerprint if result_b else ""
        qual_a = probe_quality(path_a)
        qual_b = probe_quality(path_b)
        track_a = self._to_track(path_a, info_a, fp_a)
        track_b = self._to_track(path_b, info_b, fp_b)

        config = MatchConfig.from_settings(self.context.settings)
        outcome = MatchEngine(config).match(track_a, track_b)
        decision_text, decision_result = self._decide(
            outcome, qual_a, qual_b, track_a, track_b, path_a, path_b
        )
        return _PairResult(
            outcome, qual_a, qual_b, decision_text, info_a, info_b,
            decision_result, track_a, track_b,
        )

    def _decide(self, outcome: MatchOutcome, qual_a, qual_b, track_a, track_b,
                path_a: Path, path_b: Path) -> tuple[str, DecisionResult | None]:
        """Run the Decision Engine for same-content pairs.

        Returns ``(render text, DecisionResult | None)``. Persistence is
        handled by :mod:`musicfusion.compare.report` once the results are known.
        """
        if not (outcome.same_song or outcome.classification in _SAME_OK):
            return "", None
        if outcome.version_different:
            return "", None
        from musicfusion.decision.engine import Candidate, DecisionEngine

        engine = DecisionEngine.from_settings(self.context.settings)
        result = engine.decide(
            [
                Candidate(
                    path=str(path_a),
                    quality_score=quality_of(qual_a),
                    metadata_score=_metadata_richness(track_a),
                    codec=_codec_of(path_a, qual_a),
                ),
                Candidate(
                    path=str(path_b),
                    quality_score=quality_of(qual_b),
                    metadata_score=_metadata_richness(track_b),
                    codec=_codec_of(path_b, qual_b),
                ),
            ]
        )
        result.weights = dict(engine.weights)  # type: ignore[attr-defined]
        lines = [f"WINNER: {result.winner.path}", f"Score: {result.scores[result.winner.path]}"]
        lines.append("Motivos:")
        lines.extend(f"  + {reason}" for reason in result.reasons)
        if result.runner_up:
            lines.append("Otros candidatos:")
            lines.append(f"  {result.runner_up.path} -> {result.scores[result.runner_up.path]}")
        return "\n".join(lines), result

    @staticmethod
    def _to_track(path: Path, info, fingerprint: str = ""):
        """Build a MatchTrack, deriving missing tags from the folder layout.

        Well-organized trees (``Artista/Año - Álbum/NN - Tema.ext``) carry the
        song identity even when the audio tags are empty or inconsistent.
        """
        from musicfusion.matching import MatchTrack

        artist = (info.artist if info else "") or (info.album_artist if info else "")
        album = info.album if info else ""
        title = (info.title if info else "") or ""
        track_no = info.track_no if info else None
        year = info.year if info else None
        parent = path.parent.name
        grandparent = path.parent.parent.name
        if not artist and grandparent:
            artist = artist_from_folder(grandparent)
        if not album and parent:
            album = album_from_folder(parent)
            year = year or year_from_folder(parent)
        if not title:
            title = title_from_filename(path.stem) or path.stem.lower()
            if track_no is None:
                track_no = track_no_from_filename(path.stem)
        return MatchTrack(
            path=str(path),
            artist=artist,
            album_artist=info.album_artist if info else "",
            album=album,
            title=title,
            track_no=track_no,
            disc_no=info.disc_no if info else None,
            year=year,
            duration_ms=info.duration_ms if info else 0,
            fingerprint=fingerprint,
            recording_id=(info.recording_id if info else "") or "",
        )


@dataclass
class _PairResult:
    outcome: MatchOutcome
    qual_a: object
    qual_b: object
    decision_text: str
    info_a: object
    info_b: object
    decision: DecisionResult | None
    track_a: MatchTrack
    track_b: MatchTrack


@dataclass
@dataclass
class _FoldersResult:
    lines: str
    pairs: int
    matched: int
    errors: int
    only_a: int
    only_b: int
    saved: str
    only_a_paths: list = field(default_factory=list)
    only_b_paths: list = field(default_factory=list)
    rows: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def quality_of(quality) -> float:
    return quality.score() if quality else 0.0


def _metadata_richness(track) -> float:
    fields = [
        getattr(track, "artist", "") or "",
        getattr(track, "album", "") or "",
        getattr(track, "title", "") or "",
        getattr(track, "year", None),
    ]
    present = sum(1 for value in fields if value not in ("", None, False))
    return round(100.0 * present / len(fields), 1)


def _codec_of(path: Path | None, quality) -> str:
    if quality and getattr(quality, "codec", ""):
        return quality.codec
    source = path if path else Path("?")
    ext = source.suffix.lower().lstrip(".")
    return {"m4a": "aac", "wav": "pcm_s16le"}.get(ext, ext)


# ---------------------------------------------------------------------------
# UI widgets
# ---------------------------------------------------------------------------


class _FileCard(QFrame):
    """A bordered card describing one selected audio file or folder."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        self.caption = QLabel(caption.upper())
        self.caption.setStyleSheet(
            f"color: {palette().text_muted}; font-size: 11px; letter-spacing: 1px;"
        )
        self.body = QLabel("Sin selección.")
        self.body.setWordWrap(True)
        layout.addWidget(self.caption)
        layout.addWidget(self.body)
        layout.addStretch(1)
        self._path: Path | None = None

    def set_caption(self, caption: str) -> None:
        self.caption.setText(caption.upper())

    def set_file(self, path: Path) -> None:
        self._path = path
        self.body.setText(f"{path}\n(usa 'Comparar' para leer el contenido)")

    def set_details(self, info, quality) -> None:
        lines: list[str] = [str(self._path) if self._path else "?"]
        if info:
            lines.append(f"{info.title or '—'} — {info.artist or '—'}")
            lines.append(f"Álbum: {info.album or '—'} · Año: {info.year or '—'}")
        if quality:
            lines.append(
                f"{quality.codec} · {quality.sample_rate_hz or '?'} Hz · "
                f"{quality.bit_depth or '?'} bit · {quality.bitrate_kbps or '?'} kbps"
            )
            lines.append(f"Calidad: {quality.score():.1f}/100")
        self.body.setText("\n".join(lines) if lines else "Sin datos legibles.")


class _ResultBox(QLabel):
    """Displays the MatchOutcome breakdown plus the decision explanation.

    Served directly to a QScrollArea so long results scroll.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Ejecuta 'Comparar' para ver el resultado.", parent)
        self.setObjectName("CardBody")
        self.setWordWrap(True)
        self.setTextInteractionFlags(self.textInteractionFlags().TextSelectableByMouse)

    def set_text(self, text: str) -> None:
        self.setText(text)

    def show_result(self, outcome: MatchOutcome, qual_a: float, qual_b: float,
                    decision: str) -> None:
        lines = [
            f"MATCH = {outcome.score:.1f}%  ({outcome.classification})",
            f"same_song = {outcome.same_song}  ·  same_version = {outcome.same_version}",
            f"requires_review = {outcome.requires_review}",
            "",
            "Desglose:",
            f"  artist       = {outcome.artist_score:.0f}",
            f"  album        = {outcome.album_score:.0f}",
            f"  title        = {outcome.title_score:.0f}",
            f"  track_number = {outcome.track_number_score:.0f}",
            f"  disc_number  = {outcome.disc_number_score:.0f}",
            f"  duration     = {outcome.duration_score:.0f}",
            f"  year         = {outcome.year_score:.0f}",
            f"  fingerprint  = {outcome.fingerprint_score:.0f}",
        ]
        if outcome.warnings:
            lines += ["", "Avisos:"] + [f"  ! {w}" for w in outcome.warnings]
        if outcome.reasons:
            lines += ["", "Razones:"] + [f"  - {r}" for r in outcome.reasons]
        lines += [
            "",
            "Calidad (probe):",
            f"  A = {qual_a:.1f}/100",
            f"  B = {qual_b:.1f}/100",
        ]
        if decision:
            lines += ["", "DECISIÓN:"] + decision.splitlines()
        self.setText("\n".join(lines))

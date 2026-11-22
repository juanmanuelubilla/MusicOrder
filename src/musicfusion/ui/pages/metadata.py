"""Metadata editor page: list files, inspect and fix tags and covers.

All indexed library files are listed with an editable metadata grid. Missing
fields and missing covers are highlighted. The user can either edit cells by
hand and save, or let the app autocomplete the gaps from a richer sibling file
(the metadata *donor*) and/or from MusicBrainz cover art. Covers can also be
picked manually from any image file.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicfusion.audio.metadata import TrackInfo, read_metadata
from musicfusion.core.app import AppContext
from musicfusion.ui.pages._common import JobRunner
from musicfusion.ui.widgets import ActionBar, BusyRow, palette

if TYPE_CHECKING:  # pragma: no cover
    from musicfusion.services.metasuggest import Suggestion

#: Field labels used in the "faltan" column (tags plus cover).
_FIELDS_ES = {
    "title": "título",
    "artist": "artista",
    "album": "álbum",
    "album_artist": "artista álbum",
    "track_no": "nº pista",
    "year": "año",
    "genre": "género",
    "composer": "compositor",
}
_FIELDS = tuple(_FIELDS_ES)

#: Editable columns -> (row key, value getter from TrackInfo).
_EDIT_COLUMNS = {
    2: ("title", lambda info: info.title or ""),
    3: ("artist", lambda info: info.artist or ""),
    4: ("album", lambda info: info.album or ""),
    5: ("year", lambda info: "" if info.year is None else str(info.year)),
    6: ("track_no", lambda info: "" if info.track_no is None else str(info.track_no)),
    7: ("genre", lambda info: info.genre or ""),
}
_COL_CHK = 0
_COL_FILE = 1
_COL_COVER = 8
_COL_MISSING = 9
_COL_DONOR = 10

FILTER_ALL = "Todos"
FILTER_META = "Faltan metadatos"
FILTER_COVER = "Faltan portada"


def _missing(info: TrackInfo | None) -> set[str]:
    if info is None:
        return set(_FIELDS)
    return {name for name in _FIELDS if getattr(info, name) in ("", None)}


def _cover_status(path: str, info: TrackInfo | None) -> str:
    if info is not None and info.has_embedded_art:
        return "embebida"
    from musicfusion.audio.artwork import discover_art

    if discover_art(Path(path)) is not None:
        return "archivo"
    return "sin portada"


class MetadataPage(QWidget):
    """List, edit and autocomplete metadata and covers for library files."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._rows: list[dict] = []
        self._visible: list[int] = []
        self._donor_map: dict[str, Suggestion] = {}
        self._pending = ""
        self._job_started: float | None = None
        self._populating = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Metadatos")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        bar = ActionBar()
        self.filter_box = QComboBox()
        self.filter_box.addItems([FILTER_ALL, FILTER_META, FILTER_COVER])
        self.filter_box.currentIndexChanged.connect(self._on_filter_changed)
        bar.add_widget(self.filter_box)
        self.btn_load = bar.add_button("Cargar archivos", primary=True, clicked=self._load)
        self.btn_save = bar.add_button("Guardar cambios", clicked=self._save)
        self.btn_save.setEnabled(False)
        self.btn_autofill = bar.add_button("Autocompletar selección", clicked=self._autofill)
        self.btn_autofill.setEnabled(False)
        bar.add_stretch()
        layout.addWidget(bar)

        self.status = QLabel("Selecciona una biblioteca escaneada y pulsa 'Cargar archivos'.")
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            ["", "Archivo", "Título", "Artista", "Álbum", "Año", "Nº pista",
             "Género", "Portada", "Faltan", "Donante"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)

        self.cover_panel = self._build_cover_panel()
        layout.addWidget(self.cover_panel)

        self.busy_row = BusyRow()
        self.busy_row.message.setText("Cargando archivos…")
        layout.addWidget(self.busy_row)

        self.jobs = JobRunner(self)
        self.jobs.finished.connect(self._on_finished)
        self.jobs.failed.connect(self._on_job_failed)
        self.jobs.progress.connect(self._on_progress)

    # ------------------------------------------------------------------- UI
    def _build_cover_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        self.cover_preview = QLabel("Sin selección")
        self.cover_preview.setFixedSize(160, 160)
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview.setStyleSheet("border: 1px dashed #555; border-radius: 4px;")
        layout.addWidget(self.cover_preview)

        info = QVBoxLayout()
        info.setSpacing(6)
        self.cover_note = QLabel("")
        self.cover_note.setWordWrap(True)
        self.cover_note.setStyleSheet(f"color: {palette().text_muted};")
        info.addWidget(self.cover_note, 1)
        cover_bar = QHBoxLayout()
        cover_bar.setSpacing(8)
        self.btn_cover_pick = _bar_button("Seleccionar portada…", primary=False,
                                          clicked=self._pick_cover)
        self.btn_cover_auto = _bar_button("Autocompletar portada", primary=False,
                                          clicked=self._autofill_cover)
        cover_bar.addWidget(self.btn_cover_pick)
        cover_bar.addWidget(self.btn_cover_auto)
        cover_bar.addStretch()
        info.addLayout(cover_bar)
        layout.addLayout(info, 1)
        return panel

    def _set_busy(self, busy: bool, message: str = "Trabajando…") -> None:
        self.btn_load.setEnabled(not busy)
        self.btn_save.setEnabled(not busy and any(row["dirty"] for row in self._rows))
        self.btn_autofill.setEnabled(not busy and bool(self._rows))
        self.btn_cover_pick.setEnabled(not busy)
        self.btn_cover_auto.setEnabled(not busy)
        if busy:
            self._job_started = time.monotonic()
            self.busy_row.start(message)
        else:
            self.busy_row.stop()

    # --------------------------------------------------------------- actions
    def _load(self) -> None:
        self._pending = "load"
        self.status.setText("")
        self._set_busy(True, message="Indexando biblioteca…")
        self.jobs.run(self._load_job)

    def _load_job(self, on_progress=None) -> tuple[list[dict], dict[str, Suggestion]]:
        from musicfusion.db.models import Track
        from musicfusion.services.metasuggest import build_donor_map

        self.context.db.create_schema()
        with self.context.db.session() as session:
            paths = [track.path for track in session.query(Track).order_by(Track.path)]
        if on_progress:
            on_progress(0, max(1, len(paths)), "Construyendo mapas de donantes…")
        donor_map = build_donor_map(self.context.settings, self.context.db)

        rows: list[dict] = []
        for index, path in enumerate(paths, start=1):
            if on_progress:
                on_progress(index, max(1, len(paths)), os.path.basename(path))
            info = read_metadata(path)
            if info is None:
                continue
            missing = _missing(info)
            rows.append({
                "path": path,
                "info": info,
                "missing": missing,
                "cover_status": _cover_status(path, info),
                "donor": donor_map.get(path),
                "dirty": False,
                "review": bool(missing) or not info.has_embedded_art,
            })
        return rows, donor_map

    def _on_finished(self, result) -> None:
        if self._pending == "load":
            self._on_loaded(result)
        elif self._pending == "save":
            self._on_saved(result)
        elif self._pending == "autofill":
            self._on_autofilled(result)
        self._pending = ""

    def _on_loaded(self, result) -> None:
        rows, donor_map = result
        self._set_busy(False)
        self._rows = rows
        self._donor_map = donor_map
        self._visible = []
        self._apply_filter()
        self.status.setText(
            f"{len(self._rows)} archivos cargados. Edita las celdas y pulsa "
            "'Guardar cambios', o usa 'Autocompletar selección'."
        )
        if not self._rows:
            self.status.setText("No hay archivos indexados. Escanea una biblioteca primero.")

    def _apply_filter(self) -> None:
        mode = self.filter_box.currentText()
        if mode == FILTER_META:
            indices = [i for i, row in enumerate(self._rows) if row["missing"]]
        elif mode == FILTER_COVER:
            indices = [i for i, row in enumerate(self._rows)
                       if row["cover_status"] == "sin portada"]
        else:
            indices = list(range(len(self._rows)))
        self._visible = indices
        self._render()

    def _on_filter_changed(self) -> None:
        if self._rows:
            self._apply_filter()

    # --------------------------------------------------------------- render
    def _render(self) -> None:
        self._populating = True
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible))
        for view_row, row_index in enumerate(self._visible):
            row = self._rows[row_index]
            info = row["info"]
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(
                Qt.CheckState.Checked if row["review"] else Qt.CheckState.Unchecked
            )
            self.table.setItem(view_row, _COL_CHK, check)

            file_item = QTableWidgetItem(Path(row["path"]).name)
            file_item.setToolTip(row["path"])
            file_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(view_row, _COL_FILE, file_item)

            for column, (name, getter) in _EDIT_COLUMNS.items():
                item = _editable_item(getter(info))
                if getattr(info, name) in ("", None):
                    item.setForeground(QColor(palette().danger))
                self.table.setItem(view_row, column, item)

            cover_item = _read_only_item(row["cover_status"])
            if row["cover_status"] == "sin portada":
                cover_item.setForeground(QColor(palette().danger))
            self.table.setItem(view_row, _COL_COVER, cover_item)

            missing_text = ", ".join(
                _FIELDS_ES[name] for name in _FIELDS if name in row["missing"]
            )
            missing_item = _read_only_item(missing_text)
            if missing_text:
                missing_item.setForeground(QColor(palette().danger))
            self.table.setItem(view_row, _COL_MISSING, missing_item)

            donor = row["donor"]
            donor_text = ""
            if donor is not None:
                donor_text = Path(donor.donor_path).name
            donor_item = _read_only_item(donor_text)
            donor_item.setToolTip(donor.donor_path if donor is not None else "")
            self.table.setItem(view_row, _COL_DONOR, donor_item)

        self.table.blockSignals(False)
        self._populating = False
        self._refresh_cover_panel()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating or item.column() not in _EDIT_COLUMNS:
            return
        view_row = self.table.row(item)
        if view_row < 0 or view_row >= len(self._visible):
            return
        row = self._rows[self._visible[view_row]]
        row["dirty"] = True
        item.setForeground(QColor(palette().accent))
        self.btn_save.setEnabled(True)

    # -------------------------------------------------------------- saving
    def _save(self) -> None:
        payload: list[tuple[str, TrackInfo]] = []
        saved_indexes: list[int] = []
        for view_row, row_index in enumerate(self._visible):
            row = self._rows[row_index]
            if not row["dirty"]:
                continue
            partial = self._collect_changes(self.table, view_row, row["info"])
            if partial is not None:
                payload.append((row["path"], partial))
                saved_indexes.append(row_index)
        if not payload:
            self.status.setText("No hay cambios pendientes.")
            return
        self._pending = "save"
        self._set_busy(True, message="Guardando metadatos…")
        self.jobs.run(self._save_job, payload, saved_indexes)

    @staticmethod
    def _collect_changes(table, view_row: int, info: TrackInfo) -> TrackInfo | None:
        partial = TrackInfo()
        changed = False
        for column, (name, _getter) in _EDIT_COLUMNS.items():
            item = table.item(view_row, column)
            text = item.text().strip() if item is not None else ""
            empty, original = _original_value(info, name)
            if not empty and text == original:
                continue
            if not text:
                continue
            if name == "year":
                try:
                    partial.year = int(text)
                except ValueError:
                    continue
            elif name == "track_no":
                try:
                    partial.track_no = int(text)
                except ValueError:
                    continue
            else:
                setattr(partial, name, text)
            changed = True
        return partial if changed else None

    def _save_job(self, payload, dirty_rows, on_progress=None):
        from musicfusion.audio.metadata import write_metadata_tags
        from musicfusion.services.metasuggest import refresh_track

        saved = 0
        errors: list[str] = []
        total = len(payload)
        for index, (path, partial) in enumerate(payload, start=1):
            if on_progress:
                on_progress(index, max(1, total), os.path.basename(path))
            problems = write_metadata_tags(path, partial)
            if problems:
                errors.extend(problems)
            else:
                refresh_track(self.context.db, path)
                saved += 1
        return saved, errors, dirty_rows

    def _on_saved(self, result) -> None:
        saved, errors, dirty_rows = result
        for row_index in dirty_rows:
            self._resnapshot(self._rows[row_index])
        self._set_busy(False)
        if errors:
            self.status.setText(
                f"Guardados: {saved} · con errores: {len(errors)}  —  {errors[0]}"
            )
        else:
            self.status.setText(f"Guardados {saved} archivo(s).")
        self._apply_filter()

    # ------------------------------------------------------------ autofill
    def _autofill(self) -> None:
        paths = []
        for view_row, row_index in enumerate(self._visible):
            item = self.table.item(view_row, _COL_CHK)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                paths.append(self._rows[row_index]["path"])
        if not paths:
            self.status.setText("Marca alguna fila (casilla) para autocompletar.")
            return
        self._pending = "autofill"
        self._set_busy(True, message="Autocompletando metadatos…")
        self.jobs.run(self._autofill_job, paths)

    def _autofill_cover(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            self.status.setText("Selecciona primero un archivo de la lista.")
            return
        self._pending = "autofill"
        self._set_busy(True, message="Autocompletando portada…")
        self.jobs.run(self._autofill_job, [self._rows[row_index]["path"]])

    def _autofill_job(self, paths, on_progress=None):
        from musicfusion.audio.artwork import candidate_from_bytes, discover_art, embed_art
        from musicfusion.services.metasuggest import apply_one_with_art, refresh_track
        from musicfusion.services.musicbrainz import fetch_front_cover

        tags_filled = 0
        covers_filled = 0
        errors: list[str] = []
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            if on_progress:
                on_progress(index, max(1, total), os.path.basename(path))
            donor = self._donor_map.get(path)
            if donor is not None:
                problems = apply_one_with_art(self.context.db, donor)
                if problems:
                    errors.extend(problems)
                else:
                    tags_filled += 1
            if discover_art(Path(path)) is None:
                info = read_metadata(path)
                artist = (info.artist or info.album_artist or "") if info else ""
                album = info.album if info else ""
                if album:
                    try:
                        data = fetch_front_cover(artist, album)
                    except Exception as exc:  # noqa: BLE001 - network must not abort
                        errors.append(str(exc))
                        data = None
                    if data:
                        if embed_art(Path(path), candidate_from_bytes(data, source="musicbrainz")):
                            covers_filled += 1
                            refresh_track(self.context.db, path)
        return tags_filled, covers_filled, errors, paths

    def _on_autofilled(self, result) -> None:
        tags_filled, covers_filled, errors, paths = result
        touched = set(paths)
        for row in self._rows:
            if row["path"] in touched:
                self._resnapshot(row)
        self._set_busy(False)
        message = (f"Autocompletados: {tags_filled} archivos · {covers_filled} portadas.")
        if errors:
            message += f"  Errores: {len(errors)} ({errors[0]})"
        self.status.setText(message + " Vuelve a 'Cargar archivos' para ver los cambios.")
        self._apply_filter()

    # ---------------------------------------------------------------- cover
    def _pick_cover(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            self.status.setText("Selecciona primero un archivo de la lista.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Elegir portada", filter="Imágenes (*.jpg *.jpeg *.png *.webp)"
        )
        if not path:
            return
        from musicfusion.audio.artwork import candidate_from_bytes, embed_art
        from musicfusion.services.metasuggest import refresh_track

        data = Path(path).read_bytes()
        if embed_art(Path(self._rows[row_index]["path"]),
                     candidate_from_bytes(data, source=path)):
            refresh_track(self.context.db, self._rows[row_index]["path"])
            self._resnapshot(self._rows[row_index])
            self._render()
            self.status.setText("Portada incrustada.")
        else:
            self.status.setText("No se pudo incrustar la portada.")

    def _refresh_cover_panel(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            self.cover_preview.setText("Sin selección")
            self.cover_preview.setPixmap(QPixmap())
            self.cover_note.setText("")
            return
        row = self._rows[row_index]
        self.cover_note.setText(
            f"{row['path']}\nPortada: {row['cover_status']}"
            f"{(' · faltan: ' + ', '.join(row['missing'])) if row['missing'] else ''}"
        )
        if row["cover_status"] != "sin portada":
            from musicfusion.audio.artwork import discover_art

            art = discover_art(Path(row["path"]))
            pix = QPixmap()
            if art is not None and pix.loadFromData(art.data):
                self.cover_preview.setText("")
                self.cover_preview.setPixmap(
                    pix.scaled(158, 158, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )
                return
        self.cover_preview.setText("Sin portada")
        self.cover_preview.setPixmap(QPixmap())

    def _on_selection_changed(self) -> None:
        self._refresh_cover_panel()

    # ---------------------------------------------------------------- misc
    def _current_row_index(self) -> int | None:
        view_row = self.table.currentRow()
        if view_row < 0 or view_row >= len(self._visible):
            return None
        return self._visible[view_row]

    def _resnapshot(self, row: dict) -> None:
        info = read_metadata(row["path"])
        row["info"] = info
        row["missing"] = _missing(info)
        row["cover_status"] = _cover_status(row["path"], info)
        row["dirty"] = False

    def _on_job_failed(self, message: str) -> None:
        self._set_busy(False)
        self._pending = ""
        self.status.setText(f"Error: {message}")

    def _on_progress(self, step: int, total: int, message: str) -> None:
        if total > 0:
            percent = 100.0 * step / total
            eta = None
            if step > 0 and self._job_started is not None:
                elapsed = max(0.0, time.monotonic() - self._job_started)
                eta = elapsed * max(0, total - step) / step
            self.busy_row.set_progress(percent, eta)
            if message:
                self.busy_row.update_message(message)


def _original_value(info: TrackInfo, name: str) -> tuple[bool, str]:
    if name in ("year", "track_no"):
        value = getattr(info, name)
        return value is None, "" if value is None else str(value)
    value = getattr(info, name)
    return value in ("", None), str(value or "")


def _editable_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(
        Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsEditable
        | Qt.ItemFlag.ItemIsSelectable
    )
    return item


def _read_only_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    return item


def _bar_button(text: str, *, primary: bool, clicked) -> QPushButton:
    button = QPushButton(text)
    if primary:
        button.setObjectName("Primary")
    button.clicked.connect(clicked)
    return button

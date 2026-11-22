"""Audio player page using Qt Multimedia."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from musicfusion.core.app import AppContext
from musicfusion.ui.pages._common import JobRunner
from musicfusion.ui.widgets import ActionBar, BusyRow


def _format_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


class PlayerPage(QWidget):
    """Browse indexed tracks and play back audio files."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._queue: list[tuple[str, str]] = []

        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.media.setAudioOutput(self.audio)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("Reproductor")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        bar = ActionBar()
        bar.add_button("Cargar pistas", primary=True, clicked=self._load_tracks)
        bar.add_stretch()
        layout.addWidget(bar)

        self.playlist_widget = QListWidget()
        layout.addWidget(self.playlist_widget, 1)

        controls = QHBoxLayout()
        self.pos_label = QLabel("0:00")
        self.prev_button = QPushButton("⏮")
        self.play_button = QPushButton("▶")
        self.next_button = QPushButton("⏭")
        self.slider = QSlider(Qt.Horizontal)
        self.duration_label = QLabel("0:00")
        controls.addWidget(self.pos_label)
        controls.addWidget(self.prev_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.duration_label)
        layout.addLayout(controls)

        self.now_label = QLabel("Reproducción: —")
        self.now_label.setStyleSheet("color: #8a93a3;")
        layout.addWidget(self.now_label)

        self.progress = BusyRow()
        self.progress.update_message("Cargando pistas…")
        layout.addWidget(self.progress)

        self.playlist_widget.itemDoubleClicked.connect(self._play_item)
        self.prev_button.clicked.connect(lambda: self._skip(-1))
        self.next_button.clicked.connect(lambda: self._skip(1))
        self.play_button.clicked.connect(self._toggle)
        self.slider.sliderReleased.connect(self._seek)
        self.media.positionChanged.connect(self._on_position)
        self.media.durationChanged.connect(self._on_duration)
        self.media.mediaStatusChanged.connect(self._on_status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(200)

        self.jobs = JobRunner(self)
        self.jobs.finished.connect(self._on_tracks_loaded)
        self.jobs.failed.connect(self._on_tracks_failed)

    @property
    def db(self):
        return self.context.db

    def _load_tracks(self) -> None:
        self.progress.start("Cargando pistas…")
        self.jobs.run(self._load_tracks_job)

    def _load_tracks_job(self, on_progress=None):
        """Runs in the worker thread; returns (title, artist, path) tuples."""
        from sqlalchemy import select

        from musicfusion.db.models import Track

        triples: list[tuple[str, str, str]] = []
        with self.db.session_scope() as session:
            tracks = session.scalars(
                select(Track).order_by(Track.artist_name, Track.title)
            ).all()
            total = max(1, len(tracks))
            for index, track in enumerate(tracks, start=1):
                if on_progress:
                    on_progress(index, total, f"Cargando {track.title}…")
                triples.append((track.title, track.artist_name, track.path))
        return triples

    def _on_tracks_loaded(self, result) -> None:
        self._queue = [(title, path) for title, _artist, path in (result or [])]
        self.playlist_widget.clear()
        for title, artist, _path in (result or []):
            self.playlist_widget.addItem(QListWidgetItem(f"{artist} — {title}"))
        self.progress.set_done(f"{len(self._queue)} pistas cargadas.")

    def _on_tracks_failed(self, message: str) -> None:
        self.progress.stop("")
        self.now_label.setText(f"Error: {message}")

    def _play_item(self, item: QListWidgetItem) -> None:
        row = self.playlist_widget.currentRow()
        self._play_row(row)

    def _play_row(self, row: int) -> None:
        if 0 <= row < len(self._queue):
            title, path = self._queue[row]
            self.media.setSource(QUrl.fromLocalFile(path))
            self.media.play()
            self.now_label.setText(f"Reproduciendo: {title}")

    def _toggle(self) -> None:
        if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media.pause()
            self.play_button.setText("▶")
        else:
            self.media.play()
            self.play_button.setText("⏸")

    def _skip(self, direction: int) -> None:
        row = self.playlist_widget.currentRow() + direction
        if 0 <= row < len(self._queue):
            self.playlist_widget.setCurrentRow(row)
            self._play_row(row)

    def _seek(self) -> None:
        self.media.setPosition(self.slider.value())

    def _on_position(self, position: int) -> None:
        self.slider.setValue(position)
        self.pos_label.setText(_format_ms(position))

    def _on_duration(self, duration: int) -> None:
        self.slider.setRange(0, duration)
        self.duration_label.setText(_format_ms(duration))

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._queue:
            self._skip(1)

    def _sync(self) -> None:
        if self.media.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.play_button.setText("▶")

"""Shared widgets used across the MusicFusion pages."""

from __future__ import annotations

import time

from PySide6.QtCore import QRectF, QTimer, Signal
from PySide6.QtGui import QPainter, QPen, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from musicfusion.ui.theme import Palette


def section_title(text: str) -> QLabel:
    """A bold, muted title used to head a page section."""
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    label.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {palette().text_muted};")
    return label


def palette() -> Palette:
    from musicfusion.ui.theme import PALETTES

    return PALETTES["dark"]


class StatCard(QFrame):
    """A compact, elegant metric card used across the application."""

    def __init__(
        self,
        title: str,
        value: str,
        subtitle: str = "",
        accent_color: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self._title_label = QLabel(title.upper())
        self._title_label.setObjectName("CardTitle")

        self._value_label = QLabel(value)
        self._value_label.setObjectName("CardValue")
        if accent_color:
            self._value_label.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {accent_color};")

        self._subtitle_label = QLabel(subtitle)
        self._subtitle_label.setObjectName("CardSubtitle")

        layout.addWidget(self._title_label)
        layout.addWidget(self._value_label)
        layout.addWidget(self._subtitle_label)

    def set_value(self, value: str) -> None:
        self._value_label.setText(value)

    def set_subtitle(self, text: str) -> None:
        self._subtitle_label.setText(text)


class ActionBar(QFrame):
    """A horizontal strip of actions with an optional stretch spacer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bar_layout = QHBoxLayout(self)
        self._bar_layout.setContentsMargins(0, 0, 0, 8)
        self._bar_layout.setSpacing(8)

    def add_button(self, text: str, *, primary: bool = False,
                   danger: bool = False, clicked=None) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("Primary")
        elif danger:
            button.setObjectName("Danger")
        if clicked is not None:
            button.clicked.connect(clicked)
        self._bar_layout.addWidget(button)
        return button

    def add_widget(self, widget: QWidget) -> None:
        self._bar_layout.addWidget(widget)

    def add_stretch(self) -> None:
        self._bar_layout.addStretch()


class ProgressRow(QFrame):
    """Label, progress bar and message for a running job."""

    changed = Signal(str)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.label = QLabel(title)
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setRange(0, 1000)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(self.message)
        self.setVisible(False)

    def show(self) -> None:
        """Override to always reset to zero when made visible."""
        super().show()
        self.bar.reset()

    def update_progress(self, step: int, total: int, message: str = "") -> None:
        self.record_progress(step, total, message)

    def record_progress(self, step: int, total: int, message: str = "") -> None:
        if total:
            self.bar.setValue(int(1000 * step / total))
        else:
            self.bar.setRange(0, 0)
        if message:
            self.message.setText(message)
        self.changed.emit(message)

    def set_done(self, message: str) -> None:
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        self.message.setText(message)
        self.changed.emit(message)


class Spinner(QFrame):
    """A lightweight rotating-arc wait indicator (no external assets)."""

    def __init__(self, size: int = 24, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._size = size
        self._angle = 0
        self._progress: float | None = None  # None -> indeterminate rotation
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._running = False

    def _tick(self) -> None:
        if self._progress is None:
            self._angle = (self._angle - 12) % 360
        self.update()

    def set_progress(self, value: float) -> None:
        """Fill a fraction of the circle (0-100); 100 closes the ring."""
        self._progress = max(0.0, min(100.0, value))
        self.update()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._progress = None
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._running:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 3
        side = self._size - 2 * margin
        rect = QRectF(margin, margin, side, side)
        pen = QPen(palette().accent)
        pen.setWidth(max(2, side // 6))
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self._progress is None:
            span = int(360 * 0.22)  # 22% arc, rotating
            start = self._angle
        else:
            span = int(max(4.0, 360.0 * self._progress / 100.0))  # fills up to full ring
            start = -90  # grow clockwise from the top
        painter.drawArc(rect, start * 16, span * 16)
        painter.end()


class BusyRow(QFrame):
    """Shows a spinner next to a message while a job is running."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.spinner = Spinner(size=20)
        self._started: float | None = None
        self.message = QLabel("")
        self.message.setStyleSheet(f"color: {palette().text_muted};")
        self.percent = QLabel("")
        self.percent.setStyleSheet(f"color: {palette().text_muted}; font-size: 12px;")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.spinner)
        layout.addWidget(self.message, 1)
        layout.addWidget(self.percent)
        self.setVisible(False)

    def start(self, message: str = "Trabajando…") -> None:
        self.message.setText(message)
        self.percent.setText("")
        self._started = time.monotonic()
        self.spinner.start()
        self.setVisible(True)

    def update_message(self, message: str) -> None:
        self.message.setText(message)

    def set_progress(self, percent: float, eta_seconds: float | None = None) -> None:
        """Fill the spinner ring, show the percentage and an estimated ETA."""
        self.spinner.set_progress(percent)
        label = f"{int(round(percent))}%"
        if eta_seconds is not None and eta_seconds > 0:
            label += f" · {_format_eta(eta_seconds)}"
        self.percent.setText(label)

    def record_progress(self, step: int, total: int, message: str = "") -> None:
        """Direct ProgressRow-style callback: compute % and ETA for the ring."""
        if total and total > 0:
            percent = 100.0 * step / total
            eta = None
            if step > 0 and self._started is not None:
                elapsed = max(0.0, time.monotonic() - self._started)
                eta = elapsed * max(0, total - step) / step
            self.set_progress(percent, eta)
        else:
            self.set_progress(0.0, None)
        if message:
            self.message.setText(message)

    def set_done(self, message: str) -> None:
        """Finish the job, keep the summary visible and close the ring."""
        self._started = None
        self.spinner.set_progress(100)
        self.message.setText(message)
        self.percent.setText("")
        self.spinner.stop()
        self.setVisible(message != "")

    def stop(self, message: str = "") -> None:
        if message:
            self.message.setText(message)
        self.percent.setText("")
        self.spinner.stop()
        self.setVisible(message != "")


def _format_eta(seconds: float) -> str:
    """Render a remaining-time estimate like '~1 min 05 s'."""
    total = max(1, int(round(seconds)))
    if total < 60:
        return f"~{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"~{minutes} min {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours}h {minutes:02d}m"


def grid_row(*widgets: QWidget, spacing: int = 10) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget, 1)
    return layout

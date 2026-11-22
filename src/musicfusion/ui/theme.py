"""Modern, clean, user-friendly light and dark themes for MusicFusion Pro."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Palette:
    """Color tokens used by the stylesheet templates."""

    bg: str = "#0f1117"
    surface: str = "#181b24"
    surface_alt: str = "#222734"
    surface_hover: str = "#2a3142"
    border: str = "#2f374a"
    border_focus: str = "#3b82f6"
    text: str = "#f1f5f9"
    text_muted: str = "#94a3b8"
    accent: str = "#3b82f6"
    accent_hover: str = "#60a5fa"
    accent_pressed: str = "#2563eb"
    danger: str = "#ef4444"
    danger_hover: str = "#f87171"
    success: str = "#10b981"
    warn: str = "#f59e0b"
    sidebar_bg: str = "#13161f"

    def qss(self) -> str:
        return _TEMPLATE.format(**self._values())

    def _values(self) -> dict[str, str]:
        return {
            "bg": self.bg,
            "surface": self.surface,
            "surface_alt": self.surface_alt,
            "surface_hover": self.surface_hover,
            "border": self.border,
            "border_focus": self.border_focus,
            "text": self.text,
            "text_muted": self.text_muted,
            "accent": self.accent,
            "accent_hover": self.accent_hover,
            "accent_pressed": self.accent_pressed,
            "danger": self.danger,
            "danger_hover": self.danger_hover,
            "success": self.success,
            "warn": self.warn,
            "sidebar_bg": self.sidebar_bg,
        }


def dark_palette() -> Palette:
    """Refined, modern dark theme palette (Linear/Spotify-inspired)."""
    return Palette()


def light_palette() -> Palette:
    """Crisp, modern light theme palette."""
    return Palette(
        bg="#f8fafc",
        surface="#ffffff",
        surface_alt="#f1f5f9",
        surface_hover="#e2e8f0",
        border="#cbd5e1",
        border_focus="#2563eb",
        text="#0f172a",
        text_muted="#64748b",
        accent="#2563eb",
        accent_hover="#3b82f6",
        accent_pressed="#1d4ed8",
        danger="#dc2626",
        danger_hover="#ef4444",
        success="#059669",
        warn="#d97706",
        sidebar_bg="#f1f5f9",
    )


PALETTES: dict[str, Palette] = {
    "dark": dark_palette(),
    "light": light_palette(),
}


_TEMPLATE = """
/* === BASE APPLICATION STYLING === */
QWidget {{
    background-color: {bg};
    color: {text};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background-color: {bg};
}}

/* === CARDS & CONTAINERS === */
QFrame#Card {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 10px;
}}

QFrame#Card:hover {{
    border-color: {surface_hover};
}}

QLabel#CardTitle {{
    color: {text_muted};
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

QLabel#CardValue {{
    color: {text};
    font-size: 24px;
    font-weight: 700;
}}

QLabel#CardSubtitle {{
    color: {text_muted};
    font-size: 11px;
}}

/* === SIDEBAR NAVIGATION === */
QListWidget#Sidebar {{
    background-color: {sidebar_bg};
    border: none;
    border-right: 1px solid {border};
    padding: 10px 4px;
    outline: none;
}}

QListWidget#Sidebar::item {{
    padding: 10px 14px;
    margin: 2px 6px;
    border-radius: 8px;
    color: {text_muted};
    font-weight: 500;
    font-size: 13px;
}}

QListWidget#Sidebar::item:hover {{
    background-color: {surface_hover};
    color: {text};
}}

QListWidget#Sidebar::item:selected {{
    background-color: {accent};
    color: #ffffff;
    font-weight: 600;
}}

/* === BUTTONS === */
QPushButton {{
    background-color: {surface_alt};
    color: {text};
    border: 1px solid {border};
    border-radius: 7px;
    padding: 7px 16px;
    font-weight: 500;
    font-size: 13px;
    min-height: 18px;
}}

QPushButton:hover {{
    background-color: {surface_hover};
    border-color: {border_focus};
    color: #ffffff;
}}

QPushButton:pressed {{
    background-color: {accent_pressed};
}}

QPushButton:disabled {{
    background-color: {surface};
    color: {text_muted};
    border-color: {border};
    opacity: 0.5;
}}

QPushButton#Primary {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
    font-weight: 600;
}}

QPushButton#Primary:hover {{
    background-color: {accent_hover};
    border-color: {accent_hover};
}}

QPushButton#Primary:pressed {{
    background-color: {accent_pressed};
}}

QPushButton#Danger {{
    background-color: {danger};
    color: #ffffff;
    border: 1px solid {danger};
    font-weight: 600;
}}

QPushButton#Danger:hover {{
    background-color: {danger_hover};
    border-color: {danger_hover};
}}

/* === INPUT FIELDS & LISTS === */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {bg};
    color: {text};
    border: 1px solid {border};
    border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: {accent};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border-color: {border_focus};
    background-color: {surface};
}}

QComboBox {{
    background-color: {surface_alt};
    color: {text};
    border: 1px solid {border};
    border-radius: 7px;
    padding: 6px 12px;
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

QComboBox QAbstractItemView {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 6px;
    selection-background-color: {accent};
    selection-color: #ffffff;
    padding: 4px;
}}

QListWidget, QTreeWidget, QTableWidget {{
    background-color: {bg};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}

QListWidget::item, QTreeWidget::item {{
    padding: 6px 10px;
    border-radius: 6px;
}}

QListWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {surface_alt};
}}

QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {accent};
    color: #ffffff;
}}

/* === PROGRESS BARS === */
QProgressBar {{
    background-color: {surface_alt};
    border: 1px solid {border};
    border-radius: 6px;
    text-align: center;
    color: {text};
    font-weight: 600;
    font-size: 11px;
    min-height: 14px;
    max-height: 14px;
}}

QProgressBar::chunk {{
    background-color: {accent};
    border-radius: 5px;
}}

/* === CHECKBOXES === */
QCheckBox {{
    spacing: 8px;
    color: {text};
    font-size: 13px;
}}

QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 1px solid {border};
    border-radius: 4px;
    background-color: {bg};
}}

QCheckBox::indicator:hover {{
    border-color: {border_focus};
}}

QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
    image: none;
}}

/* === SCROLLBARS === */
QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    margin: 2px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background-color: {border};
    min-height: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {text_muted};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background-color: transparent;
    height: 8px;
    margin: 2px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal {{
    background-color: {border};
    min-width: 24px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {text_muted};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* === TOOLBAR & STATUSBAR & MENUS === */
QToolBar {{
    background-color: {surface};
    border-bottom: 1px solid {border};
    spacing: 6px;
    padding: 4px 8px;
}}

QStatusBar {{
    background-color: {surface};
    color: {text_muted};
    border-top: 1px solid {border};
    font-size: 12px;
    padding: 3px 8px;
}}

QMenuBar {{
    background-color: {surface};
    border-bottom: 1px solid {border};
    padding: 2px 4px;
}}

QMenuBar::item {{
    padding: 4px 8px;
    border-radius: 4px;
}}

QMenuBar::item:selected {{
    background-color: {surface_alt};
}}

QMenu {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 20px 6px 12px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {accent};
    color: #ffffff;
}}

QToolTip {{
    background-color: {surface_alt};
    color: {text};
    border: 1px solid {border};
    border-radius: 5px;
    padding: 5px 8px;
}}
"""

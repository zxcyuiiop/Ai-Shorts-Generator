"""QSS theme for the desktop GUI.

One dark palette, no gradients-as-decoration, measured on Linear/Stripe:
near-black surfaces, one restrained accent, generous rounding on inputs and
cards, compact type. Everything visual lives here so screens stay declarative.
"""
from __future__ import annotations

# -- palette ---------------------------------------------------------------
BG = "#0E0F13"          # window background
SURFACE = "#171923"     # cards / panels
SURFACE_2 = "#1E2230"   # raised surfaces (hovered cards, inputs)
BORDER = "#262A38"      # hairlines
TEXT = "#E8EAF0"
TEXT_DIM = "#9AA0AE"
ACCENT = "#4C8DFF"
ACCENT_HOVER = "#63A0FF"
ACCENT_PRESS = "#3C7BE8"
DANGER = "#E5534B"
SUCCESS = "#3FB27F"
FOCUS = ACCENT

FONT_FAMILY = '"Segoe UI", "Inter", "Manrope", system-ui, sans-serif'


def app_stylesheet() -> str:
    return f"""
* {{
  font-family: {FONT_FAMILY};
  color: {TEXT};
  outline: none;
}}

QMainWindow, QWidget#root {{ background: {BG}; }}
QDialog {{ background: {BG}; }}

/* ---------- generic text ---------- */
QLabel {{ background: transparent; }}
QLabel#h1      {{ font-size: 22px; font-weight: 700; }}
QLabel#h2      {{ font-size: 15px; font-weight: 600; }}
QLabel#dim     {{ color: {TEXT_DIM}; }}
QLabel#mono    {{ font-family: "Cascadia Mono", "Consolas", monospace; }}

/* ---------- cards / group boxes ---------- */
QFrame#card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 10px;
}}
QFrame#card:hover {{ border-color: #333949; }}
QFrame#card QLabel {{ background: transparent; }}

QGroupBox {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 10px;
  margin-top: 18px;
  padding: 14px 12px 12px 12px;
  font-weight: 600;
}}
QGroupBox::title {{
  subcontrol-origin: margin;
  left: 12px;
  top: 2px;
  padding: 0 4px;
  color: {TEXT_DIM};
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}}

/* ---------- inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
  background: {SURFACE_2};
  border: 1px solid {BORDER};
  border-radius: 8px;
  padding: 7px 10px;
  selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
  border: 1px solid {FOCUS};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
  background: {SURFACE_2};
  border: 1px solid {BORDER};
  selection-background-color: {ACCENT};
  outline: none;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
  width: 16px; height: 16px; border-radius: 4px;
  border: 1px solid {BORDER}; background: {SURFACE_2};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

/* ---------- buttons ---------- */
QPushButton {{
  background: {SURFACE_2};
  border: 1px solid {BORDER};
  border-radius: 8px;
  padding: 8px 14px;
  font-weight: 600;
}}
QPushButton:hover  {{ background: #262B3A; border-color: #333949; }}
QPushButton:pressed {{ background: #20242F; }}
QPushButton:disabled {{ color: #565B66; background: #1A1D26; border-color: {BORDER}; }}

QPushButton#primary {{
  background: {ACCENT}; border-color: {ACCENT}; color: #0B1020;
}}
QPushButton#primary:hover  {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESS}; border-color: {ACCENT_PRESS}; }}
QPushButton#primary:disabled {{ background: #2A3550; border-color: #2A3550; color: #6B7896; }}

QPushButton#danger {{ background: transparent; border-color: {DANGER}; color: {DANGER}; }}
QPushButton#danger:hover {{ background: rgba(229,83,75,0.12); }}

/* ---------- progress ---------- */
QProgressBar {{
  background: {SURFACE_2}; border: 1px solid {BORDER};
  border-radius: 6px; height: 10px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

/* ---------- list / nav ---------- */
QListWidget#nav {{
  background: {SURFACE}; border: none; border-radius: 10px; padding: 8px;
  font-size: 14px;
}}
QListWidget#nav::item {{ padding: 10px 12px; border-radius: 8px; color: {TEXT_DIM}; }}
QListWidget#nav::item:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {SURFACE_2}; color: {TEXT}; font-weight: 600; }}

QListWidget, QTableWidget, QTreeWidget {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
}}
QListWidget::item, QTableWidget::item {{ padding: 6px; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: {SURFACE_2}; }}

/* ---------- scroll bars (slim, dark) ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: #2E3344; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3A4054; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: #2E3344; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #3A4054; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---------- tooltip / statusbar ---------- */
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px 8px; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{
  background: transparent; color: {TEXT_DIM}; padding: 8px 14px;
  border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {SURFACE}; color: {TEXT}; }}
"""


def accent() -> str:
    return ACCENT

import sys
from .constants import BG, TEXT_PRI, TEXT_SEC, BORDER, ACCENT, ACCENT_HI, SPLITTER_COLOR


def qt_argv() -> list:
    args = sys.argv[:]
    if sys.platform == "win32":
        args += ["-platform", "windows:darkmode=1"]
    return args


styleSheet = f"""
/* ── Base ─────────────────────────────────────────────────────── */
QWidget {{
    background-color: {BG};
    color: {TEXT_PRI};
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 12px;
}}

/* ── Menu bar ─────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {BORDER};
    color: {TEXT_PRI};
    padding: 2px 0;
    border-bottom: 1px solid {SPLITTER_COLOR};
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QMenuBar::item:pressed {{
    background-color: {ACCENT_HI};
    border-radius: 3px;
}}

/* ── Drop-down menus ─────────────────────────────────────────── */
QMenu {{
    background-color: #1e1e1e;
    border: 1px solid {SPLITTER_COLOR};
    padding: 4px 0;
}}
QMenu::item {{
    padding: 5px 28px 5px 20px;
    background: transparent;
}}
QMenu::item:selected {{
    background-color: {ACCENT_HI};
    color: {TEXT_PRI};
}}
QMenu::item:disabled {{
    color: #555;
}}
QMenu::separator {{
    height: 1px;
    background: {SPLITTER_COLOR};
    margin: 3px 0;
}}
QMenu::indicator {{
    width: 14px;
    height: 14px;
    margin-left: 4px;
    border: 1px solid {SPLITTER_COLOR};
    border-radius: 3px;
    background: transparent;
}}
QMenu::indicator:checked {{
    background-color: {ACCENT_HI};
    border-color: {ACCENT_HI};
    image: url();
}}
QMenu::right-arrow {{
    width: 8px;
    height: 8px;
}}

/* ── Keyboard shortcut text inside menus ────────────────────── */
QMenu::item {{
    padding-right: 60px;
}}

/* ── Scrollbars ──────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {BG};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {SPLITTER_COLOR};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT_HI};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* ── Tool buttons (left bar) ─────────────────────────────────── */
QToolButton {{
    border: none;
    border-radius: 4px;
    color: {TEXT_SEC};
    font-size: 13px;
    background: transparent;
}}
QToolButton:hover {{
    background: {ACCENT};
    color: {TEXT_PRI};
}}
QToolButton:checked {{
    background: {ACCENT_HI};
    color: {TEXT_PRI};
}}

/* ── Push buttons (transport) ────────────────────────────────── */
QPushButton {{
    border: none;
    border-radius: 4px;
    background: transparent;
    color: {TEXT_SEC};
}}
QPushButton:hover {{
    background: {ACCENT};
    color: {TEXT_PRI};
}}
QPushButton:pressed {{
    background: {ACCENT_HI};
    color: {TEXT_PRI};
}}

/* ── Sliders ─────────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    background: {SPLITTER_COLOR};
    height: 4px;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT_HI};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT_PRI};
    width: 10px;
    height: 10px;
    border-radius: 5px;
    margin: -3px 0;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_HI};
}}

/* ── Tooltips ────────────────────────────────────────────────── */
QToolTip {{
    background-color: {BORDER};
    color: {TEXT_PRI};
    border: 1px solid {SPLITTER_COLOR};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
}}
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QSlider, QFrame, QColorDialog, QDialog, QApplication,
)
from PyQt5.QtCore import Qt, QSize, QPoint, pyqtSignal
from PyQt5.QtGui import QFont, QColor

from core import constants
from core.annotation_layer import AnnotationLayer
from ui import icon


class AnnotToolBtn(QPushButton):
    """Tool button that emits right_clicked on right mouse press."""

    right_clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.right_clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class AnnotationPanel(QWidget):
    """Compact icon-only vertical annotation sidebar."""

    changed         = pyqtSignal()
    undo_req        = pyqtSignal()
    redo_req        = pyqtSignal()
    copy_req        = pyqtSignal()
    paste_req       = pyqtSignal()
    clear_frame_req = pyqtSignal()
    clear_all_req   = pyqtSignal()

    _TOOLS = [
        ("pen",     "pen.png",         "Freehand Pen"),
        ("line",    "remove.png",      "Straight Line"),
        ("arrow",   "right-arrow.png", "Arrow"),
        ("ellipse", "rec.png",         "Ellipse"),
        ("rect",    "stop.png",        "Rectangle"),
        ("eraser",  "eraser.png",      "Eraser"),
    ]

    def __init__(self, ann: AnnotationLayer, parent=None):
        super().__init__(parent)
        self._ann       = ann
        self._tool_btns: dict[str, AnnotToolBtn] = {}
        self.create_widgets()
        self.create_layout()
        self.create_connections()

    # -- Setup ----------------------------------------------------------- #

    def create_widgets(self):
        self.setStyleSheet(f"background:{constants.BORDER};")

        # Tool buttons
        for tool, icon_file, tip in self._TOOLS:
            btn = AnnotToolBtn()
            btn.setIcon(icon(icon_file))
            btn.setIconSize(QSize(14, 14))
            btn.setCheckable(True)
            btn.setChecked(tool == "pen")
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setFixedHeight(26)
            btn.setToolTip(f"{tip}  (right-click to set size)")
            btn.setStyleSheet(self._tool_style(tool == "pen"))
            self._tool_btns[tool] = btn

        # Color swatch
        self._color_btn = QPushButton()
        self._color_btn.setFixedHeight(20)
        self._color_btn.setFocusPolicy(Qt.NoFocus)
        self._color_btn.setToolTip("Pen color — click to change")
        self._refresh_color_btn()

        # Action buttons
        self._undo_btn  = self._make_action_btn("undo.png",   "Undo last stroke")
        self._redo_btn  = self._make_action_btn("redo.png",   "Redo last stroke")
        self._copy_btn  = self._make_action_btn("copy.png",   "Copy frame annotations")
        self._paste_btn = self._make_action_btn("paste.png",  "Paste annotations here")
        self._clear_btn = self._make_action_btn("cancel.png", "Clear this frame")
        self._bin_btn   = self._make_action_btn("bin.png",    "Clear all frames")

        # Visibility / playback / ghost toggles
        self._vis_btn = self._make_toggle_btn("show.png",    "Show / Hide annotations",       True)
        self._pb_btn  = self._make_toggle_btn("forward.png", "Show annotations during playback", True)
        self._ghost_btn = self._make_toggle_btn("ghost.png", "Ghost other frames",             False)

    def create_layout(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(3, 5, 3, 5)
        root.setSpacing(3)

        for btn in self._tool_btns.values():
            root.addWidget(btn)
        root.addWidget(self._sep())

        root.addWidget(self._color_btn)
        root.addWidget(self._sep())

        for btn in (self._undo_btn, self._redo_btn,
                    self._copy_btn, self._paste_btn,
                    self._clear_btn, self._bin_btn):
            root.addWidget(btn)
        root.addWidget(self._sep())

        root.addWidget(self._vis_btn)
        root.addWidget(self._pb_btn)
        root.addWidget(self._ghost_btn)
        root.addStretch()

    def create_connections(self):
        self._color_btn.clicked.connect(self._pick_color)

        for tool, btn in self._tool_btns.items():
            btn.toggled.connect(
                lambda checked, t=tool, b=btn: self._on_tool_toggled(t, b, checked))
            btn.right_clicked.connect(
                lambda t=tool, b=btn: self._show_size_popup(t, b))

        self._undo_btn.clicked.connect(self.undo_req)
        self._redo_btn.clicked.connect(self.redo_req)
        self._copy_btn.clicked.connect(self.copy_req)
        self._paste_btn.clicked.connect(self.paste_req)
        self._clear_btn.clicked.connect(self.clear_frame_req)
        self._bin_btn.clicked.connect(self.clear_all_req)

        self._vis_btn.toggled.connect(self._on_vis_toggled)
        self._pb_btn.toggled.connect(self._on_pb_toggled)
        self._ghost_btn.toggled.connect(self._on_ghost_toggled)

    # -- Widget factories ------------------------------------------------ #

    def _make_action_btn(self, icon_file: str, tip: str) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(icon(icon_file))
        btn.setIconSize(QSize(14, 14))
        btn.setFixedHeight(26)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(tip)
        btn.setStyleSheet(self._action_style())
        return btn

    def _make_toggle_btn(self, icon_file: str, tip: str, checked: bool) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(icon(icon_file))
        btn.setIconSize(QSize(14, 14))
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setFixedHeight(26)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(tip)
        btn.setStyleSheet(self._toggle_style(checked))
        return btn

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setFixedHeight(1)
        f.setStyleSheet("background:#333;border:none;")
        return f

    # -- Styles ---------------------------------------------------------- #

    @staticmethod
    def _tool_style(active: bool) -> str:
        bg     = "#4a4a6a" if active else "#2a2a2a"
        border = f"2px solid {constants.ACCENT_HI}" if active else "2px solid #3a3a3a"
        return (
            f"QPushButton{{background:{bg};color:{constants.TEXT_PRI};"
            f"border:{border};border-radius:6px;font-size:16px;}}"
            f"QPushButton:hover{{background:#3a3a5a;border:2px solid {constants.ACCENT_HI};}}"
            f"QPushButton:checked{{background:#4a4a6a;border:2px solid {constants.ACCENT_HI};}}"
        )

    @staticmethod
    def _action_style() -> str:
        return (
            f"QPushButton{{background:#2a2a2a;color:{constants.TEXT_SEC};"
            f"border:2px solid #3a3a3a;border-radius:6px;font-size:14px;}}"
            f"QPushButton:hover{{background:#3a3a3a;color:{constants.TEXT_PRI};"
            f"border:2px solid #555;}}"
        )

    @staticmethod
    def _toggle_style(active: bool) -> str:
        bg     = "#2a4a2a" if active else "#2a2a2a"
        border = "2px solid #4a8a4a" if active else "2px solid #3a3a3a"
        return (
            f"QPushButton{{background:{bg};color:{constants.TEXT_PRI};"
            f"border:{border};border-radius:6px;font-size:14px;}}"
            f"QPushButton:hover{{background:#3a5a3a;border:2px solid #5aaa5a;}}"
            f"QPushButton:checked{{background:#2a4a2a;border:2px solid #4a8a4a;}}"
        )

    @staticmethod
    def _slider_style() -> str:
        return (
            f"QSlider::groove:horizontal{{background:#333;height:4px;border-radius:2px;}}"
            f"QSlider::handle:horizontal{{background:{constants.TEXT_PRI};"
            f"width:10px;height:10px;border-radius:0px;margin:-3px 0;}}"
            f"QSlider::handle:horizontal:hover{{background:{constants.ACCENT_HI};}}"
            f"QSlider::sub-page:horizontal{{background:{constants.ACCENT_HI};border-radius:2px;}}"
        )

    # -- Color ----------------------------------------------------------- #

    def _refresh_color_btn(self) -> None:
        c = self._ann.pen_color
        self._color_btn.setStyleSheet(
            f"QPushButton{{background:{c};border:2px solid #555;border-radius:6px;}}"
            f"QPushButton:hover{{border:2px solid {constants.TEXT_PRI};}}"
        )

    def _pick_color(self) -> None:
        dlg = QColorDialog(QColor(self._ann.pen_color), self)
        dlg.setWindowTitle("Pen Color")
        dlg.setOptions(QColorDialog.DontUseNativeDialog)
        dlg.setStyleSheet(f"""
            QColorDialog, QColorDialog > QWidget {{
                background: #1c1c1c; color: {constants.TEXT_PRI};
            }}
            QLabel {{
                color: {constants.TEXT_PRI}; background: transparent;
            }}
            QPushButton {{
                background: #2a2a2a; color: {constants.TEXT_PRI};
                border: 1px solid #3a3a3a; border-radius: 4px;
                padding: 4px 14px; min-width: 64px;
            }}
            QPushButton:hover {{ background: #363636; border-color: #555; }}
            QLineEdit, QSpinBox {{
                background: #2a2a2a; color: {constants.TEXT_PRI};
                border: 1px solid #3a3a3a; border-radius: 3px; padding: 2px 4px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: #333; border: none; width: 14px;
            }}
            QSpinBox::up-arrow {{ border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 5px solid {constants.TEXT_SEC}; }}
            QSpinBox::down-arrow {{ border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {constants.TEXT_SEC}; }}
            QAbstractItemView {{
                background: #1c1c1c; color: {constants.TEXT_PRI};
                selection-background-color: {constants.ACCENT_HI};
            }}
        """)
        _ok_style = (
            f"QPushButton{{background:{constants.ACCENT_HI};color:{constants.TEXT_PRI};"
            f"border:1px solid {constants.ACCENT_HI};border-radius:4px;"
            f"padding:4px 14px;min-width:64px;}}"
            f"QPushButton:hover{{background:#1a9cf0;border-color:#1a9cf0;}}"
        )
        _cancel_style = (
            f"QPushButton{{background:#2a2a2a;color:{constants.TEXT_PRI};"
            f"border:1px solid #3a3a3a;border-radius:4px;"
            f"padding:4px 14px;min-width:64px;}}"
            f"QPushButton:hover{{background:#363636;border-color:#555;}}"
        )
        for btn in dlg.findChildren(QPushButton):
            txt = btn.text().replace("&", "")
            if txt == "OK":
                btn.setStyleSheet(_ok_style)
            elif txt == "Cancel":
                btn.setStyleSheet(_cancel_style)
        if dlg.exec_() == QDialog.Accepted:
            color = dlg.selectedColor()
            if color.isValid():
                self._ann.pen_color = color.name()
                self._refresh_color_btn()
                if self._ann.active_tool not in ("pen", "line", "arrow", "rect", "ellipse"):
                    self._tool_btns["pen"].setChecked(True)
            self.changed.emit()

    # -- Tool toggle ----------------------------------------------------- #

    def _on_tool_toggled(self, tool: str, btn: QPushButton, checked: bool) -> None:
        if checked:
            self._ann.active_tool = tool
            btn.setStyleSheet(self._tool_style(True))
            for t, b in self._tool_btns.items():
                if t != tool and b.isChecked():
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.setStyleSheet(self._tool_style(False))
                    b.blockSignals(False)
        else:
            any_checked = any(b.isChecked() for b in self._tool_btns.values())
            if not any_checked:
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)
            btn.setStyleSheet(self._tool_style(btn.isChecked()))

    # -- Size popup ------------------------------------------------------ #

    def _show_size_popup(self, tool: str, btn: AnnotToolBtn) -> None:
        lo, hi  = (4, 100) if tool == "eraser" else (1, 50)
        current = self._ann.get_thickness(tool)

        popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        self._size_popup = popup   # keep alive until closed
        popup.setStyleSheet(
            f"QWidget{{background:#232323;border:none;border-radius:6px;}}"
            f"QLabel{{color:{constants.TEXT_PRI};font-size:10px;"
            f"background:transparent;border:none;}}")

        layout = QVBoxLayout(popup)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        lbl = QLabel(f"Size: {current}")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(current)
        slider.setFixedWidth(150)
        slider.setFocusPolicy(Qt.StrongFocus)
        slider.setStyleSheet(self._slider_style())
        layout.addWidget(slider)

        def _on_change(v: int) -> None:
            self._ann.set_thickness(tool, v)
            lbl.setText(f"Size: {v}")
            self.changed.emit()

        slider.valueChanged.connect(_on_change)

        popup.adjustSize()
        pw, ph     = popup.width(), popup.height()
        btn_global = btn.mapToGlobal(QPoint(0, 0))
        x = btn_global.x() - pw - 4
        y = btn_global.y()
        screen = (btn.screen().availableGeometry()
                  if hasattr(btn, 'screen')
                  else QApplication.primaryScreen().availableGeometry())
        x = max(screen.left(), min(x, screen.right()  - pw))
        y = max(screen.top(),  min(y, screen.bottom() - ph))
        popup.move(x, y)
        popup.show()
        slider.setFocus()

    # -- Visibility slots ------------------------------------------------ #

    def _on_vis_toggled(self, checked: bool) -> None:
        self._ann.visible = checked
        self._vis_btn.setIcon(icon("show.png" if checked else "hidden.png"))
        self._vis_btn.setStyleSheet(self._toggle_style(checked))
        self.changed.emit()

    def _on_pb_toggled(self, checked: bool) -> None:
        self._ann.show_in_playback = checked
        self._pb_btn.setStyleSheet(self._toggle_style(checked))
        self.changed.emit()

    def _on_ghost_toggled(self, checked: bool) -> None:
        self._ann.ghost_enabled = checked
        self._ghost_btn.setStyleSheet(self._toggle_style(checked))
        self.changed.emit()

"""
BlastPlayer — player_window.py  (v3, RV-style)

Layout
------
  BlastPlayerWindow
  ├── menu bar
  └── QStackedWidget
      ├── WelcomeWidget
      └── PlayerWidget
          ├── VideoCanvas   (fills all space — pure black, video scaled to fit)
          └── BottomBar     (dark strip)
              ├── info row  "N frames"  |  FRAME#  |  fps
              ├── scrubber  (full width)
              └── transport row  [tools]  [nav]  [volume]
"""

import sys
import subprocess
from pathlib import Path

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QSizePolicy,
    QAction, QFileDialog, QMessageBox,
    QFrame, QToolButton, QActionGroup, QComboBox, QShortcut,
)
from PyQt5.QtCore import Qt, QTimer, QSize, pyqtSignal, QRect, QPoint
from PyQt5.QtGui import QKeySequence
from PyQt5.QtGui import QImage, QPixmap, QFont, QDragEnterEvent, QDropEvent, QIcon, QPainter

from core import constants


# ── helper ────────────────────────────────────────────────────────────────── #

def _icon(name: str) -> QIcon:
    p = constants.ICONS_DIR / name
    return QIcon(str(p)) if p.exists() else QIcon()


def _nav_btn(text: str = "", icon_name: str = "",
             tooltip: str = "", w: int = 32, h: int = 32) -> QPushButton:
    btn = QPushButton()
    if icon_name:
        ic = _icon(icon_name)
        if not ic.isNull():
            btn.setIcon(ic)
            btn.setIconSize(QSize(14, 14))
        else:
            btn.setText(text)
    else:
        btn.setText(text)
    btn.setFixedSize(w, h)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent;
            color: {constants.TEXT_SEC};
            border: none; border-radius: 4px;
            font-size: 13px;
        }}
        QPushButton:hover   {{ background: {constants.ACCENT};    color: {constants.TEXT_PRI}; }}
        QPushButton:pressed {{ background: {constants.ACCENT_HI}; color: white; }}
    """)
    return btn


# ══════════════════════════════════════════════════════════════════════════════
#  Welcome screen
# ══════════════════════════════════════════════════════════════════════════════

class WelcomeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: black;")

        logo = QLabel()
        pix = QPixmap(str(constants.ICONS_DIR / "clapperboard.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            logo.setStyleSheet("background: transparent;")
        else:
            logo.setText("🎬")
            logo.setStyleSheet("font-size: 52px; background: transparent;")
        logo.setAlignment(Qt.AlignCenter)

        title = QLabel("BlastPlayer")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: 26px; font-weight: bold;"
            f"color: {constants.TEXT_PRI}; background: transparent;"
        )

        hint = QLabel("Drop a video file here  ·  or  File → Open")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"font-size: 13px; color: {constants.TEXT_SEC}; background: transparent;"
        )

        layout = QVBoxLayout(self)
        layout.addStretch(2)
        layout.addWidget(logo)
        layout.addSpacing(8)
        layout.addWidget(title)
        layout.addSpacing(12)
        layout.addWidget(hint)
        layout.addStretch(3)


# ══════════════════════════════════════════════════════════════════════════════
#  Video canvas
# ══════════════════════════════════════════════════════════════════════════════

class VideoCanvas(QWidget):
    """Pure black canvas — video QLabel fills it, wheel/drag signals for zoom & pan."""

    zoom_scrolled = pyqtSignal(int)      # +1 = in, -1 = out
    pan_dragged   = pyqtSignal(int, int) # dx, dy

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: black;")
        self._drag_pos: QPoint | None = None

        self._display = QLabel(self)
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setStyleSheet("background: black;")
        self._display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def display(self) -> QLabel:
        return self._display

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._display.setGeometry(self.rect())

    def wheelEvent(self, event):
        self.zoom_scrolled.emit(1 if event.angleDelta().y() > 0 else -1)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._drag_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MiddleButton and self._drag_pos is not None:
            d = event.pos() - self._drag_pos
            self._drag_pos = event.pos()
            self.pan_dragged.emit(d.x(), d.y())
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._drag_pos = None
            self.setCursor(Qt.ArrowCursor)
        else:
            super().mouseReleaseEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
#  Player widget
# ══════════════════════════════════════════════════════════════════════════════

class PlayerWidget(QWidget):
    """
    Frame-accurate player.

    Shortcuts:  Space/K  play-pause   L  play-fwd   J  play-bwd
                ←/→  step frame       Home/End  first/last
    """

    _SPEEDS       = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
    _SPEED_LABELS = ("0.25×", "0.5×", "0.75×", "1×", "1.25×", "1.5×", "1.75×", "2×")

    # emitted so the window can update its title
    frame_changed = None   # set up in __init__

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._path            = ""
        self._cap             = None
        self._fps             = 24.0
        self._total_frames    = 0
        self._current_frame   = 0
        self._is_playing      = False
        self._play_reverse    = False
        self._speed           = 1.0
        self._volume          = 100
        self._audio_proc      = None
        self._scrubber_moving      = False
        self._loop                 = False
        self._current_pixmap       = None
        self._audio_scrub_enabled  = False
        self._scrub_proc           = None
        self._loop_on_step         = False
        self._loop_on_scrub        = False

        # Debounce timer for scrub audio (fires 80 ms after last scrub move)
        self._scrub_debounce = QTimer(self)
        self._scrub_debounce.setSingleShot(True)
        self._scrub_debounce.timeout.connect(self._play_scrub_audio)

        # Transform state
        self._zoom     = 1.0   # 1.0 = fit; > 1.0 = zoomed in
        self._pan_x    = 0
        self._pan_y    = 0
        self._rotation = 0     # 0 | 90 | 180 | 270
        self._flip_h   = False
        self._flip_v   = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self._build_ui()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def load_video(self, path: str) -> bool:
        self._cleanup()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return False

        self._path          = path
        self._cap           = cap
        self._fps           = cap.get(cv2.CAP_PROP_FPS) or 24.0
        self._total_frames  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._current_frame = 0

        self._scrubber.blockSignals(True)
        self._scrubber.setRange(0, max(self._total_frames - 1, 0))
        self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)

        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._show_frame(0)
        self._update_info()
        return True

    def stop(self):
        self._cleanup()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Video canvas
        self._canvas = VideoCanvas()
        self._canvas.zoom_scrolled.connect(self._on_zoom_scroll)
        self._canvas.pan_dragged.connect(self._on_pan_drag)
        root.addWidget(self._canvas, stretch=1)

        # ── Timeline strip (own widget, black background) ─────────────────
        # Row 1: frame number centred above scrubber
        # Row 2: [N frames]  [── scrubber ──]  [fps]
        timeline = QWidget()
        timeline.setFixedHeight(42)
        timeline.setStyleSheet("background: black;")
        tl = QVBoxLayout(timeline)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(0)

        # Frame number row (centred)
        fn_row = QHBoxLayout()
        fn_row.setContentsMargins(0, 0, 0, 0)
        fn_row.addStretch()
        frame_font = QFont("Courier New", 12)
        frame_font.setBold(True)
        self._frame_num_lbl = QLabel("0")
        self._frame_num_lbl.setFont(frame_font)
        self._frame_num_lbl.setAlignment(Qt.AlignCenter)
        self._frame_num_lbl.setStyleSheet(
            f"color: {constants.TEXT_PRI}; background: transparent;"
        )
        fn_row.addWidget(self._frame_num_lbl)
        fn_row.addStretch()
        tl.addLayout(fn_row)

        # Scrubber row: [N frames] [slider] [fps]
        sc_row = QHBoxLayout()
        sc_row.setContentsMargins(10, 0, 10, 0)
        sc_row.setSpacing(8)

        self._frames_lbl = QLabel("0 frames")
        self._frames_lbl.setStyleSheet(
            f"color: {constants.TEXT_SEC}; font-size: 10px; background: transparent;"
        )
        sc_row.addWidget(self._frames_lbl)

        self._scrubber = QSlider(Qt.Horizontal)
        self._scrubber.setRange(0, 0)
        self._scrubber.setStyleSheet(self._scrubber_style())
        self._scrubber.sliderPressed.connect(self._on_scrubber_pressed)
        self._scrubber.sliderReleased.connect(self._on_scrubber_released)
        self._scrubber.sliderMoved.connect(self._on_scrubber_moved)
        sc_row.addWidget(self._scrubber, stretch=1)

        self._fps_lbl = QLabel("24.00 fps")
        self._fps_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._fps_lbl.setStyleSheet(
            f"color: {constants.TEXT_SEC}; font-size: 10px; background: transparent;"
        )
        sc_row.addWidget(self._fps_lbl)
        tl.addLayout(sc_row)

        root.addWidget(timeline)

        # ── Transport row ─────────────────────────────────────────────────
        transport = QWidget()
        transport.setFixedHeight(40)
        transport.setStyleSheet(f"background: {constants.BORDER};")
        tr = QHBoxLayout(transport)
        tr.setContentsMargins(8, 0, 8, 0)
        tr.setSpacing(2)

        # ── Left side (stretch=1) keeps nav centred ──────────────────────
        tr.addStretch(1)

        # ── Nav buttons — centred:  ⏮  ◀  J  ▶  ▶  ⏭  ↺ ──────────────
        self._first_btn = _nav_btn("",  "backward.png",              tooltip="First frame  (Home)",  w=28, h=28)
        self._prev_btn  = _nav_btn("",  "left-arrow.png",            tooltip="Step back  (←)",       w=28, h=28)
        self._back_btn  = _nav_btn("",  "left.png",                  tooltip="Play backward  (J)",   w=28, h=28)

        self._play_btn  = _nav_btn("",  "play-button-arrowhead.png", tooltip="Play / Pause  (Space)",w=42, h=32)

        self._next_btn  = _nav_btn("",  "right-arrow (2).png",       tooltip="Step forward  (→)",    w=28, h=28)
        self._last_btn  = _nav_btn("",  "skip-button.png",           tooltip="Last frame  (End)",    w=28, h=28)

        self._loop_btn = QPushButton()
        self._loop_btn.setIcon(_icon("loop.png"))
        self._loop_btn.setIconSize(QSize(16, 16))
        self._loop_btn.setCheckable(True)
        self._loop_btn.setFixedSize(28, 28)
        self._loop_btn.setToolTip("Loop")
        self._loop_btn.setStyleSheet(self._loop_style(False))
        self._loop_btn.toggled.connect(self._on_loop_toggled)

        self._play_btn.setStyleSheet(f"""
            QPushButton {{
                background: {constants.ACCENT};
                color: {constants.TEXT_PRI};
                border: none; border-radius: 5px; font-size: 14px;
            }}
            QPushButton:hover   {{ background: {constants.ACCENT_HI}; color: white; }}
            QPushButton:pressed {{ background: {constants.ACCENT_HI}; color: white; }}
        """)

        for btn in (self._first_btn, self._prev_btn, self._back_btn,
                    self._play_btn,
                    self._next_btn, self._last_btn, self._loop_btn):
            tr.addWidget(btn)
            if btn is self._back_btn:
                tr.addSpacing(6)
            if btn is self._play_btn:
                tr.addSpacing(6)

        # ── Right side (stretch=1) — speed + volume, right-aligned ───────
        right = QWidget()
        right.setStyleSheet("background: transparent;")
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addStretch()

        # Speed combo
        self._speed_combo = QComboBox()
        self._speed_combo.addItems(self._SPEED_LABELS)
        self._speed_combo.setCurrentIndex(3)
        self._speed_combo.setFixedWidth(38)
        self._speed_combo.setFixedHeight(22)
        self._speed_combo.setToolTip("Playback speed")
        self._speed_combo.setStyleSheet(f"""
            QComboBox {{
                background: {constants.ACCENT};
                color: {constants.TEXT_PRI};
                border: none; border-radius: 4px;
                padding: 1px 6px; font-size: 11px;
            }}
            QComboBox QAbstractItemView {{
                background: {constants.BORDER};
                color: {constants.TEXT_PRI};
                selection-background-color: {constants.ACCENT_HI};
            }}
            QComboBox::drop-down {{ border: none; width: 0px; }}
        """)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        right_layout.addWidget(self._speed_combo)

        # Volume
        vol_ic = _nav_btn("", "volume-up.png", tooltip="Volume", w=24, h=28)
        right_layout.addWidget(vol_ic)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(self._volume)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.setToolTip("Volume")
        self._vol_slider.setStyleSheet(self._scrubber_style())
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        right_layout.addWidget(self._vol_slider)

        self._vol_lbl = QLabel(f"{self._volume}%")
        self._vol_lbl.setFixedWidth(36)
        self._vol_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._vol_lbl.setStyleSheet(
            f"color: {constants.TEXT_SEC}; font-size: 10px; background: transparent;"
        )
        right_layout.addWidget(self._vol_lbl)

        tr.addWidget(right, stretch=1)

        root.addWidget(transport)

        # ── Wire buttons ──────────────────────────────────────────────────
        self._first_btn.clicked.connect(self._go_first)
        self._prev_btn.clicked.connect(self._step_back)
        self._back_btn.clicked.connect(self._play_backward)
        self._play_btn.clicked.connect(self._toggle_play)
        self._next_btn.clicked.connect(self._step_forward)
        self._last_btn.clicked.connect(self._go_last)

    # ------------------------------------------------------------------ #
    #  Playback core                                                       #
    # ------------------------------------------------------------------ #

    def _toggle_play(self):
        self._pause() if self._is_playing else self._play()

    def _play_backward(self):
        if self._is_playing and self._play_reverse:
            self._pause()
        else:
            self._play(reverse=True)

    def _play(self, reverse: bool = False):
        if not self._cap:
            return
        if not reverse and self._current_frame >= self._total_frames - 1:
            self._seek(0)
        if reverse and self._current_frame <= 0:
            self._seek(self._total_frames - 1)
        self._is_playing   = True
        self._play_reverse = reverse
        self._play_btn.setIcon(_icon("pause.png"))
        self._play_btn.setText("")
        self._timer.start(max(1, int(1000 / (self._fps * self._speed))))
        self._start_audio()

    def _pause(self):
        self._is_playing   = False
        self._play_reverse = False
        self._timer.stop()
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._stop_audio()

    def _on_tick(self):
        if not self._cap:
            return
        if self._play_reverse:
            if self._current_frame <= 0:
                if self._loop:
                    self._seek(self._total_frames - 1)
                    self._start_audio()
                else:
                    self._pause()
                return
            target = self._current_frame - 1
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ret, frame = self._cap.read()
            if not ret:
                self._pause()
                return
            self._current_frame = target
            self._render(frame)
        else:
            ret, frame = self._cap.read()
            if not ret:
                if self._loop:
                    self._seek(0)
                    self._start_audio()
                    return
                self._pause()
                return
            self._current_frame = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            self._render(frame)

        if not self._scrubber_moving:
            self._scrubber.blockSignals(True)
            self._scrubber.setValue(self._current_frame)
            self._scrubber.blockSignals(False)
        self._update_info()

    # ------------------------------------------------------------------ #
    #  Navigation                                                          #
    # ------------------------------------------------------------------ #

    def _step_forward(self):
        self._pause()
        if not self._cap:
            return
        if self._current_frame < self._total_frames - 1:
            self._seek(self._current_frame + 1)
        elif self._loop_on_step:
            self._seek(0)

    def _step_back(self):
        self._pause()
        if not self._cap:
            return
        if self._current_frame > 0:
            self._seek(self._current_frame - 1)
        elif self._loop_on_step:
            self._seek(self._total_frames - 1)

    def _go_first(self):
        self._pause()
        if self._cap:
            self._seek(0)

    def _go_last(self):
        self._pause()
        if self._cap:
            self._seek(self._total_frames - 1)

    def _seek(self, frame_num: int):
        if not self._cap:
            return
        frame_num = max(0, min(frame_num, self._total_frames - 1))
        self._current_frame = frame_num
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        self._show_frame(frame_num)
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(frame_num)
        self._scrubber.blockSignals(False)
        self._update_info()

    def _show_frame(self, frame_num: int):
        if not self._cap:
            return
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = self._cap.read()
        if ret:
            self._render(frame)

    # ------------------------------------------------------------------ #
    #  Rendering                                                           #
    # ------------------------------------------------------------------ #

    def _render(self, frame):
        # Apply flip
        if self._flip_h and self._flip_v:
            frame = cv2.flip(frame, -1)
        elif self._flip_h:
            frame = cv2.flip(frame, 1)
        elif self._flip_v:
            frame = cv2.flip(frame, 0)
        # Apply rotation
        if self._rotation == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif self._rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif self._rotation == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888)
        self._current_pixmap = QPixmap.fromImage(qimg)
        self._refresh_display()

    def _refresh_display(self):
        if not self._current_pixmap or self._current_pixmap.isNull():
            return
        disp = self._canvas.display()
        dw, dh = disp.width(), disp.height()
        if dw == 0 or dh == 0:
            return

        if self._zoom <= 1.0:
            scaled = self._current_pixmap.scaled(
                disp.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self._pan_x = 0
            self._pan_y = 0
            disp.setPixmap(scaled)
        else:
            pw = int(self._current_pixmap.width()  * self._zoom)
            ph = int(self._current_pixmap.height() * self._zoom)
            scaled_full = self._current_pixmap.scaled(
                pw, ph, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
            # Clamp pan so we never go out of bounds
            self._pan_x = max(0, min(self._pan_x, max(0, pw - dw)))
            self._pan_y = max(0, min(self._pan_y, max(0, ph - dh)))
            vw = min(dw, pw)
            vh = min(dh, ph)
            crop = scaled_full.copy(QRect(self._pan_x, self._pan_y, vw, vh))
            # Centre the crop on black if smaller than display
            if vw < dw or vh < dh:
                canvas = QPixmap(dw, dh)
                canvas.fill(Qt.black)
                p = QPainter(canvas)
                p.drawPixmap((dw - vw) // 2, (dh - vh) // 2, crop)
                p.end()
                disp.setPixmap(canvas)
            else:
                disp.setPixmap(crop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_display()

    # ------------------------------------------------------------------ #
    #  Scrubber slots                                                      #
    # ------------------------------------------------------------------ #

    def _on_scrubber_pressed(self):
        self._scrubber_moving = True
        if self._is_playing:
            self._timer.stop()
            self._stop_audio()

    def _on_scrubber_released(self):
        self._scrubber_moving = False
        self._current_frame   = self._scrubber.value()
        if self._cap:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, self._current_frame)
        if self._is_playing:
            self._timer.start(max(1, int(1000 / (self._fps * self._speed))))
            self._start_audio()

    def _on_scrubber_moved(self, value: int):
        if not self._cap:
            return
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, value)
        ret, frame = self._cap.read()
        if ret:
            self._render(frame)
            self._current_frame = value
        self._update_info()
        if self._audio_scrub_enabled:
            self._scrub_debounce.start(80)

    # ------------------------------------------------------------------ #
    #  Speed / loop                                                        #
    # ------------------------------------------------------------------ #

    def _on_speed_changed(self, index: int):
        self._speed = self._SPEEDS[index]
        if self._is_playing:
            self._timer.setInterval(max(1, int(1000 / (self._fps * self._speed))))

    def _on_loop_toggled(self, checked: bool):
        self._loop = checked
        self._loop_btn.setStyleSheet(self._loop_style(checked))

    # ------------------------------------------------------------------ #
    #  Audio                                                               #
    # ------------------------------------------------------------------ #

    def _ffplay(self) -> str:
        exe = "ffplay.exe" if sys.platform == "win32" else "ffplay"
        return str(Path(constants.FFMPEG_PATH).parent / exe)

    def _start_audio(self):
        self._stop_audio()
        ffplay = self._ffplay()
        if not Path(ffplay).exists():
            return
        seek = self._current_frame / self._fps
        try:
            self._audio_proc = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit",
                 "-ss", f"{seek:.4f}",
                 "-volume", str(self._volume),
                 self._path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"[BlastPlayer] audio: {exc}")

    def _stop_audio(self):
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None

    def _play_scrub_audio(self):
        """Play a short audio snippet at the current frame for scrub feedback."""
        if not self._path or not self._cap:
            return
        ffplay = self._ffplay()
        if not Path(ffplay).exists():
            return
        # Kill previous scrub snippet
        if self._scrub_proc and self._scrub_proc.poll() is None:
            self._scrub_proc.terminate()
        seek = self._current_frame / self._fps
        try:
            self._scrub_proc = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit",
                 "-ss", f"{seek:.4f}",
                 "-t", "0.15",
                 "-volume", str(self._volume),
                 self._path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _on_volume_changed(self, value: int):
        self._volume = value
        self._vol_lbl.setText(f"{value}%")
        if self._is_playing:
            self._stop_audio()
            self._start_audio()

    # ------------------------------------------------------------------ #
    #  Info update                                                         #
    # ------------------------------------------------------------------ #

    def _update_info(self):
        f = self._current_frame
        self._frame_num_lbl.setText(str(f))
        self._frames_lbl.setText(f"{self._total_frames} frames")
        self._fps_lbl.setText(f"{self._fps:.2f} fps")
        # Notify window to update title
        if hasattr(self, '_on_frame_changed'):
            self._on_frame_changed(f)

    def set_frame_callback(self, fn):
        self._on_frame_changed = fn

    # ------------------------------------------------------------------ #
    #  Zoom / pan                                                          #
    # ------------------------------------------------------------------ #

    _ZOOM_STEP = 1.25

    def zoom_in(self):
        self._zoom = min(self._zoom * self._ZOOM_STEP, 16.0)
        self._refresh_display()

    def zoom_out(self):
        self._zoom = max(self._zoom / self._ZOOM_STEP, 1.0)
        self._pan_x = 0
        self._pan_y = 0
        self._refresh_display()

    def zoom_reset(self):
        self._zoom  = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._refresh_display()

    def _on_zoom_scroll(self, direction: int):
        if direction > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def _on_pan_drag(self, dx: int, dy: int):
        if self._zoom > 1.0:
            self._pan_x -= dx
            self._pan_y -= dy
            self._refresh_display()

    # ------------------------------------------------------------------ #
    #  Rotation / flip                                                     #
    # ------------------------------------------------------------------ #

    def rotate_cw(self):
        self._rotation = (self._rotation + 90) % 360
        self._rerender()

    def rotate_ccw(self):
        self._rotation = (self._rotation - 90) % 360
        self._rerender()

    def flip_horizontal(self):
        self._flip_h = not self._flip_h
        self._rerender()

    def flip_vertical(self):
        self._flip_v = not self._flip_v
        self._rerender()

    def _rerender(self):
        """Re-apply transforms to the current frame without seeking."""
        if self._cap and self._current_frame >= 0:
            self._show_frame(self._current_frame)


    # ------------------------------------------------------------------ #
    #  Keyboard                                                            #
    # ------------------------------------------------------------------ #

    def keyPressEvent(self, event):
        k    = event.key()
        mods = event.modifiers()
        ctrl = mods & Qt.ControlModifier

        if   k == Qt.Key_Space:                       self._toggle_play()
        elif k == Qt.Key_L:                           self._play(reverse=False)
        elif k == Qt.Key_J:                           self._play(reverse=True)
        elif k == Qt.Key_K:                           self._pause()
        elif k == Qt.Key_Left  and not ctrl:          self._step_back()
        elif k == Qt.Key_Right and not ctrl:          self._step_forward()
        elif k == Qt.Key_Home:                        self._go_first()
        elif k == Qt.Key_End:                         self._go_last()
        elif k == Qt.Key_Equal and ctrl:              self.zoom_in()
        elif k == Qt.Key_Minus and ctrl:              self.zoom_out()
        elif k == Qt.Key_0     and ctrl:              self.zoom_reset()
        elif k == Qt.Key_Backslash and not ctrl:      self.zoom_reset()
        else: super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Cleanup                                                             #
    # ------------------------------------------------------------------ #

    def _cleanup(self):
        self._timer.stop()
        self._stop_audio()
        if self._cap:
            self._cap.release()
            self._cap = None
        self._current_frame  = 0
        self._is_playing     = False
        self._current_pixmap = None
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._canvas.display().clear()
        self._canvas.display().setStyleSheet("background: black;")

    # ------------------------------------------------------------------ #
    #  Style helpers                                                       #
    # ------------------------------------------------------------------ #

    def _scrubber_style(self) -> str:
        return f"""
            QSlider::groove:horizontal {{
                background: {constants.SPLITTER_COLOR};
                height: 4px; border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {constants.ACCENT_HI};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {constants.TEXT_PRI};
                width: 10px; height: 10px;
                border-radius: 5px; margin: -3px 0;
            }}
            QSlider::handle:horizontal:hover {{
                background: {constants.ACCENT_HI};
            }}
        """

    def _loop_style(self, active: bool) -> str:
        bg    = constants.ACCENT_HI if active else constants.ACCENT
        color = "white"             if active else constants.TEXT_SEC
        return f"""
            QPushButton {{
                background: {bg}; color: {color};
                border: none; border-radius: 4px; font-size: 15px;
            }}
            QPushButton:hover {{ background: {constants.ACCENT_HI}; color: white; }}
        """


# ══════════════════════════════════════════════════════════════════════════════
#  Main window
# ══════════════════════════════════════════════════════════════════════════════

class BlastPlayerWindow(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BlastPlayer")
        self.setMinimumSize(900, 560)
        self.setAcceptDrops(True)

        self._player  = PlayerWidget(self)
        self._welcome = WelcomeWidget(self)
        self._current_filename = ""

        self._player.set_frame_callback(self._update_title)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome)
        self._stack.addWidget(self._player)
        self.setCentralWidget(self._stack)

        self._build_menu()
        self._setup_shortcuts()

    # ------------------------------------------------------------------ #
    #  Title                                                               #
    # ------------------------------------------------------------------ #

    def _update_title(self, frame: int = 0):
        if self._current_filename:
            self.setWindowTitle(f"{self._current_filename} — Frame {frame}")

    # ------------------------------------------------------------------ #
    #  Menu                                                                #
    # ------------------------------------------------------------------ #

    def _build_menu(self):
        mb = self.menuBar()

        # File
        fm = mb.addMenu("File")
        oa = fm.addAction("Open…"); oa.setShortcut("Ctrl+O"); oa.triggered.connect(self._on_open)
        fm.addSeparator()
        qa = fm.addAction("Quit");  qa.setShortcut("Ctrl+Q"); qa.setMenuRole(QAction.QuitRole); qa.triggered.connect(self.close)

        # Edit
        em = mb.addMenu("Edit")
        em.addAction("Undo").setShortcut("Ctrl+Z")
        em.addAction("Redo").setShortcut("Ctrl+Y")
        em.addSeparator()
        em.addAction("Copy Frame").setShortcut("Ctrl+C")

        # Playback
        pb = mb.addMenu("Playback")
        sm = pb.addMenu("Speed")
        sg = QActionGroup(self); sg.setExclusive(True)
        for i, (spd, lbl) in enumerate(zip(PlayerWidget._SPEEDS, PlayerWidget._SPEED_LABELS)):
            a = sm.addAction(lbl); a.setCheckable(True); a.setChecked(spd == 1.0)
            sg.addAction(a)
            _idx = i
            a.triggered.connect(lambda _, _i=_idx: self._player._speed_combo.setCurrentIndex(_i))
        pb.addSeparator()
        _a = pb.addAction("Play / Pause");   _a.setShortcut("Space"); _a.triggered.connect(self._player._toggle_play)
        _b = pb.addAction("Play Forwards");  _b.setShortcut("L");     _b.triggered.connect(lambda: self._player._play(reverse=False))
        _c = pb.addAction("Play Backwards"); _c.setShortcut("J");     _c.triggered.connect(lambda: self._player._play(reverse=True))
        _d = pb.addAction("Pause");          _d.setShortcut("K");     _d.triggered.connect(self._player._pause)
        pb.addSeparator()
        _e = pb.addAction("Go to Start"); _e.setShortcut("Home"); _e.triggered.connect(self._player._go_first)
        _f = pb.addAction("Go to End");   _f.setShortcut("End");  _f.triggered.connect(self._player._go_last)
        pb.addSeparator()
        la = pb.addAction("Loop"); la.setCheckable(True)
        la.triggered.connect(self._player._loop_btn.setChecked)
        pb.addSeparator()

        ss = pb.addMenu("Stepping and Scrubbing")
        los = ss.addAction("Loop on Step");   los.setShortcut("Ctrl+Shift+."); los.setCheckable(True)
        los.triggered.connect(lambda c: setattr(self._player, "_loop_on_step", c))
        loc = ss.addAction("Loop on Scrub");  loc.setShortcut("Ctrl+Alt+.");   loc.setCheckable(True)
        loc.triggered.connect(lambda c: setattr(self._player, "_loop_on_scrub", c))

        # Audio
        am = mb.addMenu("Audio")
        am.addAction("Volume Up").triggered.connect(
            lambda: self._player._vol_slider.setValue(min(100, self._player._vol_slider.value() + 5)))
        am.addAction("Volume Down").triggered.connect(
            lambda: self._player._vol_slider.setValue(max(0, self._player._vol_slider.value() - 5)))
        am.addSeparator()
        scrub_act = am.addAction("Audio Scrubbing")
        scrub_act.setCheckable(True)
        scrub_act.setChecked(False)
        scrub_act.triggered.connect(
            lambda checked: setattr(self._player, "_audio_scrub_enabled", checked)
        )

        # Video
        vm = mb.addMenu("Video")

        fs_act = vm.addAction("Fullscreen"); fs_act.setShortcut("F11")
        fs_act.triggered.connect(self._toggle_fullscreen)
        vm.addSeparator()

        pz_menu = vm.addMenu("Pan / Zoom")
        _zi = pz_menu.addAction("Zoom In");       _zi.setShortcut("Ctrl+="); _zi.triggered.connect(self._player.zoom_in)
        _zo = pz_menu.addAction("Zoom Out");      _zo.setShortcut("Ctrl+-"); _zo.triggered.connect(self._player.zoom_out)
        _zr = pz_menu.addAction("Reset Pan/Zoom");_zr.setShortcut("Ctrl+0"); _zr.triggered.connect(self._player.zoom_reset)
        vm.addSeparator()

        _rcw = vm.addAction("Rotate CW");  _rcw.setShortcut("Ctrl+Shift+M"); _rcw.triggered.connect(self._player.rotate_cw)
        _rcc = vm.addAction("Rotate CCW"); _rcc.setShortcut("Ctrl+Shift+N"); _rcc.triggered.connect(self._player.rotate_ccw)
        vm.addSeparator()

        _fh = vm.addAction("Flip Horizontal"); _fh.setShortcut("Ctrl+X");       _fh.triggered.connect(self._player.flip_horizontal)
        _fv = vm.addAction("Flip Vertical");   _fv.setShortcut("Ctrl+Shift+X"); _fv.triggered.connect(self._player.flip_vertical)

        # Bookmarks
        bm = mb.addMenu("Bookmarks")
        bm.addAction("Add Bookmark").setShortcut("Ctrl+B")
        bm.addAction("Next Bookmark").setShortcut("Shift+Right")
        bm.addAction("Previous Bookmark").setShortcut("Shift+Left")

        # Tools
        tm = mb.addMenu("Tools")
        tm.addAction("Preferences…")

        # Help
        hm = mb.addMenu("Help")
        hm.addAction("Keyboard Shortcuts")
        hm.addSeparator()
        about_act = hm.addAction("About BlastPlayer")
        about_act.triggered.connect(self._on_about)

    # ------------------------------------------------------------------ #
    #  Open video                                                          #
    # ------------------------------------------------------------------ #

    def open_video(self, path: str):
        if not _CV2_AVAILABLE:
            QMessageBox.critical(self, "Missing dependency",
                "opencv-python is required.\npip install opencv-python")
            return
        if not self._player.load_video(path):
            QMessageBox.warning(self, "Cannot open", f"Could not open:\n{path}")
            return
        self._current_filename = Path(path).name
        self._stack.setCurrentIndex(1)
        self._player.setFocus()
        self._update_title(0)

    def _setup_shortcuts(self):
        pass  # shortcuts handled via keyPressEvent + NoFocus on buttons

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            self._toggle_fullscreen()
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)

    def _on_about(self):
        from dialogs.about_dialog import AboutDialog
        AboutDialog(self).exec_()

    def _on_open(self):
        exts = " ".join(f"*{e}" for e in sorted(constants.VIDEO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", "", f"Video files ({exts});;All files (*)")
        if path:
            self.open_video(path)

    # ------------------------------------------------------------------ #
    #  Drag & drop                                                         #
    # ------------------------------------------------------------------ #

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).suffix.lower() in constants.VIDEO_EXTS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).suffix.lower() in constants.VIDEO_EXTS:
                self.open_video(path)
                break

    def closeEvent(self, event):
        self._player.stop()
        super().closeEvent(event)

"""
BlastPlayer — player_window.py  (v4, FFmpeg backend)

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

FFmpeg backend notes
--------------------
  • ffprobe  — metadata (fps, dimensions, frame count)
  • ffmpeg pipe — sequential frame decode during playback (_open_pipe)
  • ffmpeg single-frame — accurate seek for stepping/scrubbing (_fetch_frame)
  • ffplay   — audio (unchanged from v3)

No OpenCV dependency.
"""

import sys
import json
import time
import array
import queue
import threading
import subprocess
from pathlib import Path

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QSizePolicy,
    QAction, QFileDialog, QMessageBox,
    QFrame, QToolButton, QActionGroup, QComboBox, QShortcut,
    QOpenGLWidget,
)
from PyQt5.QtCore import Qt, QTimer, QSize, pyqtSignal, QSettings
from PyQt5.QtGui import (
    QKeySequence, QPainter, QPen, QColor,
    QPixmap, QFont, QDragEnterEvent, QDropEvent, QIcon,
    QOpenGLShaderProgram, QOpenGLShader, QOpenGLBuffer,
    QSurfaceFormat, QOpenGLVertexArrayObject,
)
try:
    from OpenGL.GL import (
        glClearColor, glClear, glViewport, glDrawArrays,
        glGenTextures, glBindTexture, glTexImage2D, glTexSubImage2D,
        glTexParameteri, glActiveTexture,
        GL_COLOR_BUFFER_BIT, GL_TRIANGLES, GL_FLOAT,
        GL_TEXTURE_2D, GL_RGB, GL_UNSIGNED_BYTE, GL_TEXTURE0,
        GL_LINEAR, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
        GL_CLAMP_TO_EDGE, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    )
except ImportError:
    raise SystemExit(
        "[BlastPlayer] PyOpenGL is required for GPU rendering.\n"
        "Install it with:  pip install PyOpenGL"
    )
from PyQt5.QtWidgets import QStyle, QStyleOptionSlider

from core import constants


# ── helpers ───────────────────────────────────────────────────────────────── #

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
    btn.setFocusPolicy(Qt.NoFocus)
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


def _ffmpeg_exe() -> str:
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidate = Path(constants.FFMPEG_PATH).parent / exe
    return str(candidate) if candidate.exists() else exe   # fall back to $PATH


def _ffprobe_exe() -> str:
    exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    candidate = Path(constants.FFMPEG_PATH).parent / exe
    return str(candidate) if candidate.exists() else exe


def _ffplay_exe() -> str:
    exe = "ffplay.exe" if sys.platform == "win32" else "ffplay"
    candidate = Path(constants.FFMPEG_PATH).parent / exe
    return str(candidate) if candidate.exists() else exe


# ── ffprobe metadata ──────────────────────────────────────────────────────── #

def probe_video(path: str) -> dict:
    """
    Returns a dict with keys:
        fps          (float)
        total_frames (int)
        width        (int)
        height       (int)
    Raises RuntimeError if ffprobe fails or the file has no video stream.
    """
    cmd = [
        _ffprobe_exe(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",
        path,
    ]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"ffprobe failed: {exc}") from exc

    data = json.loads(raw)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found.")
    s = streams[0]

    # fps
    def _ratio(field: str) -> float:
        val = s.get(field, "0/1")
        parts = val.split("/")
        if len(parts) == 2 and int(parts[1]):
            return int(parts[0]) / int(parts[1])
        return float(parts[0])

    fps = _ratio("r_frame_rate") or _ratio("avg_frame_rate") or 24.0

    # frame count  (nb_frames is absent in some containers)
    if "nb_frames" in s and s["nb_frames"].isdigit():
        total_frames = int(s["nb_frames"])
    elif "duration" in s:
        total_frames = max(1, int(float(s["duration"]) * fps))
    elif "tags" in s and "DURATION" in s["tags"]:
        h, m, sec = s["tags"]["DURATION"].split(":")
        dur = int(h) * 3600 + int(m) * 60 + float(sec)
        total_frames = max(1, int(dur * fps))
    else:
        total_frames = 0   # unknown — scrubber will show 0

    return {
        "fps":          fps,
        "total_frames": total_frames,
        "width":        int(s.get("width", 0)),
        "height":       int(s.get("height", 0)),
    }


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
#  Scrubber slider — clicks jump to exact position
# ══════════════════════════════════════════════════════════════════════════════

class ScrubberSlider(QSlider):
    """
    QSlider that jumps to the exact clicked position on the groove.

    Qt's default behaviour moves by a page step when clicking the groove, and
    calling super() afterwards overrides our setValue with that page step.
    We intercept groove clicks entirely: bypass super() for press/move/release,
    manually emit the standard signals, and let super() handle handle-drag as
    normal so no existing behaviour is regressed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._groove_pressed = False
        self._in_frame  = None   # int | None
        self._out_frame = None   # int | None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle_rect = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle_rect.contains(event.pos()):
                # Groove click: jump directly, don't let super() page-step on top.
                self._groove_pressed = True
                self.setValue(self._value_from_pos(event.pos()))
                self.sliderPressed.emit()
                event.accept()
                return
        self._groove_pressed = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._groove_pressed:
            val = max(self.minimum(),
                      min(self._value_from_pos(event.pos()), self.maximum()))
            self.setValue(val)
            self.sliderMoved.emit(val)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._groove_pressed:
            self._groove_pressed = False
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _value_from_pos(self, pos) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        if self.orientation() == Qt.Horizontal:
            span    = groove.width() - handle.width()
            rel_pos = pos.x() - groove.x() - handle.width() // 2
        else:
            span    = groove.height() - handle.height()
            rel_pos = pos.y() - groove.y() - handle.height() // 2
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), rel_pos, span,
            self.invertedAppearance())

    def set_in_out(self, in_frame, out_frame):
        self._in_frame  = in_frame
        self._out_frame = out_frame
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._in_frame is None and self._out_frame is None:
            return

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        span   = groove.width() - handle.width()
        offset = groove.x() + handle.width() // 2
        gy     = groove.center().y()

        def x_for(val):
            return offset + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), val, span)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        # Tinted range between in and out
        x_in  = x_for(self._in_frame  if self._in_frame  is not None else self.minimum())
        x_out = x_for(self._out_frame if self._out_frame is not None else self.maximum())
        if x_out > x_in:
            painter.fillRect(x_in, gy - 3, x_out - x_in, 6, QColor(255, 170, 0, 90))

        # In-point marker (green)
        if self._in_frame is not None:
            painter.setPen(QPen(QColor("#4CAF50"), 2))
            x = x_for(self._in_frame)
            painter.drawLine(x, gy - 7, x, gy + 7)

        # Out-point marker (orange-red)
        if self._out_frame is not None:
            painter.setPen(QPen(QColor("#FF6B35"), 2))
            x = x_for(self._out_frame)
            painter.drawLine(x, gy - 7, x, gy + 7)

        painter.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Video canvas
# ══════════════════════════════════════════════════════════════════════════════

class VideoCanvas(QOpenGLWidget):
    """
    GPU-accelerated video display.
    Raw RGB24 bytes are uploaded as a GL texture each frame; the GPU
    handles all scaling and letterboxing — zero CPU scaling per frame.
    """

    zoom_scrolled = pyqtSignal(int)       # +1 = in, -1 = out
    pan_dragged   = pyqtSignal(int, int)  # dx, dy

    # GLSL 1.20 — works on both compatibility and core profiles
    _VERT = """
        #version 120
        attribute vec2 a_pos;
        attribute vec2 a_tex;
        varying   vec2 v_tex;
        void main() {
            gl_Position = vec4(a_pos, 0.0, 1.0);
            v_tex = a_tex;
        }
    """
    _FRAG = """
        #version 120
        uniform sampler2D u_frame;
        varying vec2 v_tex;
        void main() {
            gl_FragColor = texture2D(u_frame, v_tex);
        }
    """

    def __init__(self, parent=None):
        fmt = QSurfaceFormat()
        fmt.setSwapInterval(0)                      # no vsync — timer drives frame rate
        fmt.setVersion(2, 1)
        fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(parent)

        self._frame_raw = None          # bytes | None
        self._vid_w     = 0
        self._vid_h     = 0
        self._zoom      = 1.0
        self._pan_x     = 0
        self._pan_y     = 0
        self._dirty     = False         # True → new frame waiting to upload
        self._drag_pos  = None

        # GL objects — initialised in initializeGL
        self._vao       = None          # QOpenGLVertexArrayObject
        self._prog      = None          # QOpenGLShaderProgram
        self._vbo       = None          # QOpenGLBuffer
        self._tex_id    = None          # raw GL texture name (int)
        self._tex_w     = 0             # dimensions of currently allocated texture
        self._tex_h     = 0

    # ── Public API ───────────────────────────────────────────────────── #

    def set_frame(self, raw: bytes, vid_w: int, vid_h: int):
        self._frame_raw = raw
        self._vid_w     = vid_w
        self._vid_h     = vid_h
        self._dirty     = True
        self.update()

    def clear_frame(self):
        self._frame_raw = None
        self.update()

    def set_transform(self, zoom: float, pan_x: int, pan_y: int):
        self._zoom  = zoom
        self._pan_x = pan_x
        self._pan_y = pan_y
        self.update()

    # ── OpenGL callbacks ─────────────────────────────────────────────── #

    def initializeGL(self):
        # VAO — required in core profile; harmless in compatibility profile
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()

        self._prog = QOpenGLShaderProgram(self)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Vertex,   self._VERT)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Fragment, self._FRAG)
        self._prog.bindAttributeLocation("a_pos", 0)
        self._prog.bindAttributeLocation("a_tex", 1)
        if not self._prog.link():
            print(f"[BlastPlayer] GL link error: {self._prog.log()}")

        self._vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._vbo.create()
        self._vbo.setUsagePattern(QOpenGLBuffer.DynamicDraw)

        # Raw GL texture (glTexImage2D/glTexSubImage2D for clean size-change handling)
        self._tex_id = int(glGenTextures(1))
        glBindTexture(GL_TEXTURE_2D, self._tex_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)

        glClearColor(0, 0, 0, 1)

    def resizeGL(self, w: int, h: int):
        glViewport(0, 0, w, h)

    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT)

        if not self._frame_raw or not self._vid_w or self._prog is None:
            return

        # Upload new frame bytes to the GPU texture
        if self._dirty:
            w, h = self._vid_w, self._vid_h
            glBindTexture(GL_TEXTURE_2D, self._tex_id)
            if w != self._tex_w or h != self._tex_h:
                # First frame or resolution change: allocate new storage
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, w, h, 0,
                             GL_RGB, GL_UNSIGNED_BYTE, self._frame_raw)
                self._tex_w, self._tex_h = w, h
            else:
                # Same size: update in-place (much faster than reallocating)
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h,
                                GL_RGB, GL_UNSIGNED_BYTE, self._frame_raw)
            glBindTexture(GL_TEXTURE_2D, 0)
            self._dirty = False

        verts = self._quad_vertices()
        if not verts:
            return

        buf = array.array('f', verts)
        raw = buf.tobytes()

        self._vao.bind()
        self._vbo.bind()
        self._vbo.allocate(raw, len(raw))

        self._prog.bind()
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self._tex_id)
        self._prog.setUniformValue("u_frame", 0)

        stride = 4 * 4                  # 4 floats × 4 bytes
        self._prog.enableAttributeArray(0)
        self._prog.enableAttributeArray(1)
        self._prog.setAttributeBuffer(0, GL_FLOAT, 0,     2, stride)
        self._prog.setAttributeBuffer(1, GL_FLOAT, 2 * 4, 2, stride)

        glDrawArrays(GL_TRIANGLES, 0, 6)

        self._prog.disableAttributeArray(0)
        self._prog.disableAttributeArray(1)
        glBindTexture(GL_TEXTURE_2D, 0)
        self._prog.release()
        self._vbo.release()
        self._vao.release()

    # ── Quad geometry ────────────────────────────────────────────────── #

    def _quad_vertices(self):
        """24 floats: 6 × (x, y, u, v) describing the letterboxed video quad."""
        vw, vh = self.width(), self.height()
        if vw == 0 or vh == 0 or self._vid_w == 0 or self._vid_h == 0:
            return []

        vid_ar  = self._vid_w / self._vid_h
        view_ar = vw / vh

        if self._zoom <= 1.0:
            if vid_ar >= view_ar:
                sx, sy = 1.0, view_ar / vid_ar
            else:
                sx, sy = vid_ar / view_ar, 1.0
            x0, x1 = -sx, sx
            y0, y1 = -sy, sy            # NDC: y0 = bottom, y1 = top
            u0, u1, v0, v1 = 0.0, 1.0, 0.0, 1.0
        else:
            x0, x1, y0, y1 = -1.0, 1.0, -1.0, 1.0
            fit = (vw / self._vid_w) if vid_ar >= view_ar else (vh / self._vid_h)
            total  = fit * self._zoom
            disp_w = self._vid_w * total
            disp_h = self._vid_h * total
            px = max(0.0, min(float(self._pan_x), max(0.0, disp_w - vw)))
            py = max(0.0, min(float(self._pan_y), max(0.0, disp_h - vh)))
            u0 = px / disp_w;           u1 = min(1.0, (px + vw) / disp_w)
            v0 = py / disp_h;           v1 = min(1.0, (py + vh) / disp_h)

        # Two triangles; v0 = image-top → NDC-top (y1), v1 = image-bottom → NDC-bottom (y0)
        return [
            x0, y1, u0, v0,   # top-left
            x1, y1, u1, v0,   # top-right
            x1, y0, u1, v1,   # bottom-right
            x0, y1, u0, v0,   # top-left
            x1, y0, u1, v1,   # bottom-right
            x0, y0, u0, v1,   # bottom-left
        ]

    # ── Mouse / wheel ────────────────────────────────────────────────── #

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
    Frame-accurate player backed by FFmpeg pipes.

    Playback strategy
    -----------------
      Forward play  : persistent ffmpeg pipe opened at the seek point;
                      _on_tick reads one frame (width*height*3 bytes) per tick.
                      Seeking re-opens the pipe at the new position.
      Reverse play  : no pipe — _on_tick calls _fetch_frame(n-1) each tick
                      (single-frame accurate seek).  Acceptable because reverse
                      is inherently slow; a frame cache could be added later.
      Step / scrub  : _fetch_frame() — single ffmpeg invocation, post-input -ss
                      for frame-accurate positioning.

    Shortcuts:  Space/K  play-pause   L  play-fwd   J  play-bwd
                ←/→  step frame       Home/End  first/last
    """

    _SPEEDS       = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
    _SPEED_LABELS = ("0.25×", "0.5×", "0.75×", "1×", "1.25×", "1.5×", "1.75×", "2×")
    _CACHE_MAX_MB = 2048   # skip RAM cache if decoded frames exceed this

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

        # File / probe info
        self._path            = ""
        self._fps             = 24.0
        self._total_frames    = 0
        self._vid_w           = 0
        self._vid_h           = 0        # native dimensions (pre-transform)

        # Playback state
        self._current_frame   = 0
        self._is_playing      = False
        self._play_reverse    = False
        self._speed           = 1.0
        self._loop            = False

        # Pipe (forward playback)
        self._pipe_proc       = None     # subprocess.Popen | None
        self._pipe_frame      = 0        # frame index pipe is currently at
        self._loop_frame0     = None     # raw bytes of frame 0, pre-decoded into memory

        # Wall-clock sync — used by both cache and pipe paths
        self._play_clock_start = 0.0    # time.monotonic() when playback (re)started
        self._play_frame_start = 0      # _current_frame when playback (re)started

        # Pipe reader thread — keeps pipe reads off the main thread
        self._frame_queue   = queue.Queue(maxsize=4)
        self._reader_thread = None
        self._reader_stop   = threading.Event()

        # Pre-warmed loop pipe — opened N frames before EOF for seamless looping
        self._loop_pipe_proc   = None
        self._loop_pipe_queue  = None
        self._loop_pipe_thread = None
        self._loop_reader_stop = threading.Event()

        # RAM frame cache — all frames decoded into memory for zero-latency playback
        self._frame_cache   = None   # list[bytes] once ready, None while not cached
        self._cache_loading = False  # True while background decode is running

        # Reverse frame cache
        self._reverse_cache   = []      # list of (frame_num, raw_bytes), pop() = backward
        self._cache_building  = False   # True while background thread is filling cache

        # In / out points
        self._in_frame  = None   # int | None
        self._out_frame = None   # int | None

        # Audio
        self._volume          = 100
        self._pre_mute_volume = 100   # volume restored when un-muting
        self._audio_proc      = None
        self._audio_scrub_enabled = False
        self._scrub_proc      = None

        # Scrub debounce
        self._scrub_debounce = QTimer(self)
        self._scrub_debounce.setSingleShot(True)
        self._scrub_debounce.timeout.connect(self._play_scrub_audio)

        # Misc state
        self._scrubber_moving = False
        self._loop_on_step    = False
        self._loop_on_scrub   = False

        # Transform state
        self._zoom     = 1.0
        self._pan_x    = 0
        self._pan_y    = 0
        self._rotation = 0      # 0 | 90 | 180 | 270
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
        try:
            info = probe_video(path)
        except RuntimeError as exc:
            print(f"[BlastPlayer] probe: {exc}")
            return False

        self._path         = path
        self._fps          = info["fps"]
        self._total_frames = info["total_frames"]
        self._vid_w        = info["width"]
        self._vid_h        = info["height"]
        self._current_frame = 0

        self._scrubber.blockSignals(True)
        self._scrubber.setRange(0, max(self._total_frames - 1, 0))
        self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)

        self._in_frame  = None
        self._out_frame = None
        self._scrubber.set_in_out(None, None)
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._show_frame(0)
        self._update_info()
        self._start_cache_build()
        return True

    def stop(self):
        self._cleanup()

    # ------------------------------------------------------------------ #
    #  In / Out points                                                     #
    # ------------------------------------------------------------------ #

    def _effective_in(self) -> int:
        return self._in_frame if self._in_frame is not None else 0

    def _effective_out(self) -> int:
        return self._out_frame if self._out_frame is not None else max(0, self._total_frames - 1)

    def set_in_frame(self):
        if not self._path:
            return
        f = self._current_frame
        if self._in_frame is not None and f == self._in_frame:
            # Toggle off: clear in point
            self._in_frame = None
        elif self._out_frame is not None and f >= self._out_frame:
            return  # invalid: in must be before out
        else:
            self._in_frame = f
        self._scrubber.set_in_out(self._in_frame, self._out_frame)

    def set_out_frame(self):
        if not self._path:
            return
        f = self._current_frame
        if self._out_frame is not None and f == self._out_frame:
            # Toggle off: clear out point
            self._out_frame = None
        elif self._in_frame is not None and f <= self._in_frame:
            return  # invalid: out must be after in
        else:
            self._out_frame = f
        self._scrubber.set_in_out(self._in_frame, self._out_frame)

    def clear_in_out(self):
        self._in_frame  = None
        self._out_frame = None
        self._scrubber.set_in_out(None, None)

    # ------------------------------------------------------------------ #
    #  FFmpeg frame helpers                                                #
    # ------------------------------------------------------------------ #

    def _effective_size(self) -> tuple[int, int]:
        """Return (w, h) after applying current rotation."""
        if self._rotation in (90, 270):
            return self._vid_h, self._vid_w
        return self._vid_w, self._vid_h

    def _build_vf(self) -> str:
        """Build a libavfilter -vf string for the current flip/rotation."""
        filters = []
        if self._flip_h:
            filters.append("hflip")
        if self._flip_v:
            filters.append("vflip")
        if self._rotation == 90:
            filters.append("transpose=1")
        elif self._rotation == 180:
            filters.append("transpose=1,transpose=1")
        elif self._rotation == 270:
            filters.append("transpose=2")
        return ",".join(filters) if filters else "null"

    def _fetch_frame(self, frame_num: int) -> bytes | None:
        """
        Accurate single-frame decode via ffmpeg (post-input -ss).
        Returns raw RGB24 bytes or None on failure.
        Uses post-input seek so every frame is reachable, not just keyframes.
        This is slower than the pipe but only used for stepping/scrubbing/reverse.
        """
        if not self._path or not self._vid_w:
            return None
        seek = frame_num / self._fps
        w, h = self._effective_size()
        cmd = [
            _ffmpeg_exe(),
            "-i",        self._path,
            "-ss",       f"{seek:.6f}",
            "-frames:v", "1",
            "-f",        "rawvideo",
            "-pix_fmt",  "rgb24",
            "-vf",       self._build_vf(),
            "pipe:1",
        ]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            raw = proc.stdout
            expected = w * h * 3
            return raw if len(raw) == expected else None
        except Exception as exc:
            print(f"[BlastPlayer] fetch_frame: {exc}")
            return None

    # ── Pipe (forward playback) ──────────────────────────────────────── #

    _LOOKAHEAD_FRAMES    = 30    # open the loop-back pipe this many frames before the end
    _AUDIO_SYNC_OFFSET   = 0.10  # seconds — shifts video clock forward to wait for ffplay startup

    def _open_pipe(self, start_frame: int = 0):
        """
        Open a persistent ffmpeg pipe starting at start_frame.

        Uses a double-seek for accuracy:
          1. Pre-input -ss snaps quickly to the nearest keyframe up to 4 s before.
          2. Post-input -ss fine-seeks within the decoded stream to the exact frame.
        This gives frame-accurate positioning without the cost of a full post-input
        seek from the beginning.
        """
        self._close_pipe()
        self._close_loop_pipe()
        self._close_lookahead()
        if not self._path or not self._vid_w:
            return

        pre_offset_frames = min(start_frame, int(self._fps * 4))
        pre_frame         = start_frame - pre_offset_frames
        pre_ts            = pre_frame / self._fps
        fine_ts           = pre_offset_frames / self._fps

        cmd = [_ffmpeg_exe()]
        if pre_ts > 0:
            cmd += ["-ss", f"{pre_ts:.6f}"]
        cmd += [
            "-i",       self._path,
            "-ss",      f"{fine_ts:.6f}",
            "-f",       "rawvideo",
            "-pix_fmt", "rgb24",
            "-vf",      self._build_vf(),
            "pipe:1",
        ]
        try:
            self._pipe_proc  = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self._pipe_frame = start_frame
            self._start_reader_thread()
        except Exception as exc:
            print(f"[BlastPlayer] pipe open: {exc}")
            self._pipe_proc = None

    def _close_pipe(self):
        self._stop_reader_thread()
        if self._pipe_proc is not None:
            try:
                self._pipe_proc.stdout.close()
                self._pipe_proc.terminate()
            except Exception:
                pass
            self._pipe_proc = None

    def _open_lookahead(self):
        """Pre-decode frame 0 into memory so it can be shown instantly at loop time."""
        if self._loop_frame0 is not None or not self._path or not self._vid_w:
            return
        def _fill():
            self._loop_frame0 = self._fetch_frame(0)
        threading.Thread(target=_fill, daemon=True).start()

    def _close_lookahead(self):
        self._loop_frame0 = None

    # ── RAM frame cache ──────────────────────────────────────────────── #

    def _start_cache_build(self):
        """
        Decode all frames into a Python list in a background thread.
        Once complete, self._frame_cache is a list[bytes]; playback and scrubbing
        switch to serving directly from memory — zero subprocess latency.
        Skipped silently if the video is too large for _CACHE_MAX_MB.
        """
        if not self._path or not self._vid_w or self._total_frames <= 0:
            return
        w, h       = self._effective_size()
        frame_size = w * h * 3
        total_mb   = (self._total_frames * frame_size) / 1_048_576
        if total_mb > self._CACHE_MAX_MB:
            return

        self._frame_cache   = None
        self._cache_loading = True
        path   = self._path
        vf     = self._build_vf()
        nf     = self._total_frames
        nbytes = frame_size

        def _fill():
            cmd = [
                _ffmpeg_exe(),
                "-i",       path,
                "-f",       "rawvideo",
                "-pix_fmt", "rgb24",
                "-vf",      vf,
                "pipe:1",
            ]
            frames = []
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                for _ in range(nf):
                    chunk = proc.stdout.read(nbytes)
                    if len(chunk) < nbytes:
                        break
                    frames.append(bytes(chunk))
                try:
                    proc.stdout.close()
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception as exc:
                print(f"[BlastPlayer] cache build: {exc}")

            if frames:
                self._frame_cache = frames
            self._cache_loading = False

        threading.Thread(target=_fill, daemon=True).start()

    def _open_loop_pipe(self):
        """Pre-warm a pipe from frame 0 so the loop swap is instantaneous."""
        if self._loop_pipe_proc is not None or not self._path or not self._vid_w:
            return
        self._loop_reader_stop.clear()
        self._loop_pipe_queue = queue.Queue(maxsize=4)
        cmd = [
            _ffmpeg_exe(),
            "-i",       self._path,
            "-f",       "rawvideo",
            "-pix_fmt", "rgb24",
            "-vf",      self._build_vf(),
            "pipe:1",
        ]
        try:
            self._loop_pipe_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            proc = self._loop_pipe_proc
            q    = self._loop_pipe_queue
            stop = self._loop_reader_stop
            self._loop_pipe_thread = threading.Thread(
                target=self._pipe_reader_loop,
                args=(proc, q, stop),
                daemon=True)
            self._loop_pipe_thread.start()
        except Exception as exc:
            print(f"[BlastPlayer] loop pipe: {exc}")
            self._loop_pipe_proc = None

    def _close_loop_pipe(self):
        self._loop_reader_stop.set()
        self._loop_pipe_thread = None
        if self._loop_pipe_proc is not None:
            try:
                self._loop_pipe_proc.stdout.close()
                self._loop_pipe_proc.terminate()
            except Exception:
                pass
            self._loop_pipe_proc = None
        if self._loop_pipe_queue is not None:
            while True:
                try:
                    self._loop_pipe_queue.get_nowait()
                except queue.Empty:
                    break
            self._loop_pipe_queue = None
        self._loop_reader_stop.clear()

    def _start_reader_thread(self):
        self._reader_stop.clear()
        self._frame_queue = queue.Queue(maxsize=4)
        proc = self._pipe_proc
        q    = self._frame_queue
        stop = self._reader_stop
        self._reader_thread = threading.Thread(
            target=self._pipe_reader_loop,
            args=(proc, q, stop),
            daemon=True)
        self._reader_thread.start()

    def _stop_reader_thread(self):
        """Signal the reader to exit and drain the queue so it can unblock."""
        self._reader_stop.set()
        self._reader_thread = None
        while True:                          # drain so a blocked put() can complete
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _pipe_reader_loop(self, proc, q, stop):
        """Background: reads frames from proc and puts them into q."""
        w, h   = self._effective_size()
        nbytes = w * h * 3
        while not stop.is_set():
            try:
                raw = proc.stdout.read(nbytes)
            except Exception:
                break
            if len(raw) < nbytes:
                try:
                    q.put(None, timeout=1.0)    # EOF sentinel
                except queue.Full:
                    pass
                break
            q.put(bytes(raw))   # blocks if queue full — fine for background thread

    # ── Reverse frame cache ──────────────────────────────────────────── #

    _REVERSE_BATCH = 60   # frames decoded per cache fill

    def _build_reverse_cache(self, up_to_frame: int):
        """
        Decode a batch of frames ending at *up_to_frame* in a background
        thread so the UI doesn't freeze.  The playback timer is stopped
        first and restarted via _resume_reverse() when the cache is ready.
        """
        if self._cache_building:
            return
        self._cache_building = True
        self._timer.stop()          # pause ticking while we fill

        start_frame = max(0, up_to_frame - self._REVERSE_BATCH + 1)
        w, h        = self._effective_size()
        nbytes      = w * h * 3
        n_frames    = up_to_frame - start_frame + 1
        path        = self._path
        vf          = self._build_vf()

        def _fill():
            pre_offset = min(start_frame, int(self._fps * 4))
            pre_frame  = start_frame - pre_offset
            pre_ts     = pre_frame  / self._fps
            fine_ts    = pre_offset / self._fps

            cmd = [_ffmpeg_exe()]
            if pre_ts > 0:
                cmd += ["-ss", f"{pre_ts:.6f}"]
            cmd += [
                "-i",        path,
                "-ss",       f"{fine_ts:.6f}",
                "-frames:v", str(n_frames),
                "-f",        "rawvideo",
                "-pix_fmt",  "rgb24",
                "-vf",       vf,
                "pipe:1",
            ]
            frames = []
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                for i in range(n_frames):
                    chunk = proc.stdout.read(nbytes)
                    if len(chunk) < nbytes:
                        break
                    frames.append((start_frame + i, bytes(chunk)))
                proc.stdout.close()
                proc.terminate()
                proc.wait(timeout=2)
            except Exception as exc:
                print(f"[BlastPlayer] reverse cache: {exc}")

            # Ascending order — pop() removes from the end, giving descending frame numbers
            self._reverse_cache  = frames
            self._cache_building = False
            # Re-enter the Qt main thread to restart the timer
            QTimer.singleShot(0, self._resume_reverse)

        threading.Thread(target=_fill, daemon=True).start()

    def _resume_reverse(self):
        """Called on the main thread after the reverse cache has been filled."""
        if self._is_playing and self._play_reverse and self._reverse_cache:
            interval = max(1, int(1000 / (self._fps * self._speed)))
            self._timer.start(interval)

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

        # ── Timeline strip ────────────────────────────────────────────────
        timeline = QWidget()
        timeline.setFixedHeight(42)
        timeline.setStyleSheet("background: black;")
        tl = QVBoxLayout(timeline)
        tl.setContentsMargins(0, 2, 0, 2)
        tl.setSpacing(0)

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

        sc_row = QHBoxLayout()
        sc_row.setContentsMargins(10, 0, 10, 0)
        sc_row.setSpacing(8)

        self._frames_lbl = QLabel("0 frames")
        self._frames_lbl.setStyleSheet(
            f"color: {constants.TEXT_SEC}; font-size: 10px; background: transparent;"
        )
        sc_row.addWidget(self._frames_lbl)

        self._scrubber = ScrubberSlider(Qt.Horizontal)
        self._scrubber.setRange(0, 0)
        self._scrubber.setFocusPolicy(Qt.NoFocus)
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

        tr.addStretch(1)

        self._first_btn = _nav_btn("", "backward.png",              tooltip="First frame  (Home)", w=28, h=28)
        self._prev_btn  = _nav_btn("", "left-arrow.png",            tooltip="Step back  (←)",      w=28, h=28)
        self._back_btn  = _nav_btn("", "left.png",                  tooltip="Play backward  (J)",  w=28, h=28)
        self._play_btn  = _nav_btn("", "play-button-arrowhead.png", tooltip="Play / Pause  (Space)", w=42, h=32)
        self._next_btn  = _nav_btn("", "right-arrow (2).png",       tooltip="Step forward  (→)",   w=28, h=28)
        self._last_btn  = _nav_btn("", "skip-button.png",           tooltip="Last frame  (End)",   w=28, h=28)

        self._loop_btn = QPushButton()
        self._loop_btn.setIcon(_icon("loop.png"))
        self._loop_btn.setIconSize(QSize(16, 16))
        self._loop_btn.setCheckable(True)
        self._loop_btn.setFixedSize(28, 28)
        self._loop_btn.setFocusPolicy(Qt.NoFocus)
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

        # Right side: speed + volume
        right = QWidget()
        right.setStyleSheet("background: transparent;")
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addStretch()

        self._speed_combo = QComboBox()
        self._speed_combo.addItems(self._SPEED_LABELS)
        self._speed_combo.setCurrentIndex(3)
        self._speed_combo.setFixedWidth(38)
        self._speed_combo.setFixedHeight(22)
        self._speed_combo.setFocusPolicy(Qt.NoFocus)
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

        self._vol_btn = _nav_btn("", "volume-up.png", tooltip="Mute / Unmute", w=24, h=28)
        self._vol_btn.clicked.connect(self._toggle_mute)
        right_layout.addWidget(self._vol_btn)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(self._volume)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.setFocusPolicy(Qt.NoFocus)
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

        # Wire buttons
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
        if not self._path:
            return
        if not reverse and self._current_frame >= self._effective_out():
            self._seek_no_render(self._effective_in())
        if reverse and self._current_frame <= self._effective_in():
            self._seek_no_render(self._effective_out())

        self._is_playing   = True
        self._play_reverse = reverse
        self._play_btn.setIcon(_icon("pause.png"))
        self._play_btn.setText("")

        if reverse:
            self._reverse_cache = []
        elif self._frame_cache is not None:
            self._pipe_frame = self._current_frame
        else:
            self._open_pipe(self._current_frame)

        self._play_frame_start = self._current_frame
        self._play_clock_start = time.monotonic()
        if not reverse:
            self._start_audio()

        interval = max(1, int(1000 / (self._fps * self._speed)))
        self._timer.start(interval)

    def _pause(self):
        self._is_playing   = False
        self._play_reverse = False
        self._timer.stop()
        self._close_pipe()
        self._close_loop_pipe()
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._stop_audio()
        # Re-render current frame at full SmoothTransformation quality now that we're paused
        self._refresh_display()

    def _on_tick(self):
        if not self._path:
            return

        if self._play_reverse:
            # ── Reverse ───────────────────────────────────────────────── #
            if self._frame_cache is not None:
                # Fast path: serve directly from RAM cache — no batch decode
                in_f       = self._effective_in()
                next_frame = self._current_frame - 1
                if next_frame < in_f:
                    if self._loop:
                        last = min(self._effective_out(), len(self._frame_cache) - 1)
                        self._current_frame = last
                        self._render_raw(self._frame_cache[last])
                        self._scrubber.blockSignals(True)
                        self._scrubber.setValue(last)
                        self._scrubber.blockSignals(False)
                        self._update_info()
                    else:
                        self._pause()
                    return
                self._render_raw(self._frame_cache[next_frame])
                self._current_frame = next_frame

            else:
                # Batch-decode path (used when RAM cache isn't available)
                if not self._reverse_cache and not self._cache_building:
                    in_f = self._effective_in()
                    if self._current_frame <= in_f:
                        if self._loop:
                            self._seek_no_render(self._effective_out())
                            self._reverse_cache = []
                            self._scrubber.blockSignals(True)
                            self._scrubber.setValue(self._current_frame)
                            self._scrubber.blockSignals(False)
                            self._update_info()
                        else:
                            self._pause()
                        return
                    # Pass current_frame - 1 so the first pop is the frame before us
                    self._build_reverse_cache(self._current_frame - 1)
                    return   # timer restarted by _resume_reverse when cache ready

                if not self._reverse_cache:
                    return   # still building

                frame_num, raw      = self._reverse_cache.pop()
                self._current_frame = frame_num
                self._render_raw(raw)

        else:
            # ── Forward play (wall-clock sync) ────────────────────────── #
            elapsed      = time.monotonic() - self._play_clock_start
            target_frame = int(self._play_frame_start + elapsed * self._fps * self._speed)

            # Upgrade from pipe to cache the moment the cache becomes ready
            if self._frame_cache is not None and self._pipe_proc is not None:
                self._close_pipe()
                self._close_loop_pipe()

            if self._frame_cache is not None:
                # ── Cache mode ──────────────────────────────────────── #
                out  = min(self._effective_out(), len(self._frame_cache) - 1)
                in_f = self._effective_in()
                if target_frame > out:
                    if self._loop:
                        self._stop_audio()
                        self._play_frame_start = in_f
                        self._play_clock_start = time.monotonic()
                        self._current_frame    = in_f
                        self._pipe_frame       = in_f + 1
                        self._render_raw(self._frame_cache[in_f])
                        self._start_audio()
                        self._scrubber.blockSignals(True)
                        self._scrubber.setValue(in_f)
                        self._scrubber.blockSignals(False)
                        self._update_info()
                    else:
                        self._pause()
                    return

                if target_frame <= self._current_frame:
                    return  # not yet time for the next frame

                self._render_raw(self._frame_cache[target_frame])
                self._current_frame = target_frame
                self._pipe_frame    = target_frame + 1

            else:
                # ── Pipe mode ───────────────────────────────────────── #
                out         = self._effective_out()
                raw         = None
                eof         = False
                out_reached = False
                while self._pipe_frame <= target_frame:
                    try:
                        item = self._frame_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        eof = True
                        break
                    raw = item
                    self._pipe_frame += 1
                    if self._out_frame is not None and self._pipe_frame - 1 >= out:
                        out_reached = True
                        break

                if eof or out_reached:
                    if raw is not None:
                        self._render_raw(raw)
                        self._current_frame = self._pipe_frame - 1
                    in_f = self._effective_in()
                    if self._loop:
                        self._stop_audio()
                        if out_reached or in_f > 0:
                            # Range loop or non-zero in point: simple re-seek
                            self._open_pipe(in_f)
                            self._current_frame    = in_f
                            self._play_frame_start = in_f
                            self._play_clock_start = time.monotonic()
                            self._start_audio()
                            self._scrubber.blockSignals(True)
                            self._scrubber.setValue(in_f)
                            self._scrubber.blockSignals(False)
                            self._update_info()
                        else:
                            # True EOF, in_f == 0: seamless loop-pipe swap
                            if self._loop_pipe_proc is not None:
                                self._reader_stop.set()
                                self._reader_thread = None
                                while True:
                                    try:
                                        self._frame_queue.get_nowait()
                                    except queue.Empty:
                                        break
                                if self._pipe_proc is not None:
                                    try:
                                        self._pipe_proc.stdout.close()
                                        self._pipe_proc.terminate()
                                    except Exception:
                                        pass
                                    self._pipe_proc = None
                                self._pipe_proc     = self._loop_pipe_proc
                                self._frame_queue   = self._loop_pipe_queue
                                self._reader_thread = self._loop_pipe_thread
                                self._reader_stop   = self._loop_reader_stop
                                self._pipe_frame    = 0
                                self._loop_pipe_proc   = None
                                self._loop_pipe_queue  = None
                                self._loop_pipe_thread = None
                                self._loop_reader_stop = threading.Event()
                                if self._loop_frame0 is not None:
                                    self._render_raw(self._loop_frame0)
                                    self._loop_frame0 = None
                            else:
                                raw0 = self._loop_frame0
                                self._open_pipe(0)
                                if raw0 is not None:
                                    self._render_raw(raw0)
                            self._current_frame    = 0
                            self._play_frame_start = 0
                            self._play_clock_start = time.monotonic()
                            self._start_audio()
                            self._scrubber.blockSignals(True)
                            self._scrubber.setValue(0)
                            self._scrubber.blockSignals(False)
                            self._update_info()
                    else:
                        self._pause()
                    return

                if raw is not None:
                    self._render_raw(raw)
                    self._current_frame = self._pipe_frame - 1
                    # Only pre-warm loop pipe when playing to actual end (in_f == 0)
                    if self._loop and self._total_frames > 0 and self._effective_in() == 0:
                        remaining = self._effective_out() - self._current_frame
                        if remaining <= self._LOOKAHEAD_FRAMES:
                            if self._loop_frame0 is None:
                                self._open_lookahead()
                            if self._loop_pipe_proc is None:
                                self._open_loop_pipe()

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
        if not self._path:
            return
        if self._current_frame < self._total_frames - 1:
            self._seek(self._current_frame + 1)
        elif self._loop_on_step:
            self._seek(0)

    def _step_back(self):
        self._pause()
        if not self._path:
            return
        if self._current_frame > 0:
            self._seek(self._current_frame - 1)
        elif self._loop_on_step:
            self._seek(self._total_frames - 1)

    def _go_first(self):
        self._pause()
        if self._path:
            self._seek(0)

    def _go_last(self):
        self._pause()
        if self._path:
            self._seek(self._total_frames - 1)

    def _seek(self, frame_num: int):
        """Accurate seek + display update."""
        if not self._path:
            return
        frame_num = max(0, min(frame_num, self._total_frames - 1))
        self._current_frame = frame_num
        self._pipe_frame    = frame_num   # keep cache position in sync
        self._show_frame(frame_num)
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(frame_num)
        self._scrubber.blockSignals(False)
        self._update_info()

    def _seek_no_render(self, frame_num: int):
        """Set current_frame without fetching (used before opening a pipe)."""
        self._current_frame = max(0, min(frame_num, self._total_frames - 1))

    def _show_frame(self, frame_num: int):
        if self._frame_cache and frame_num < len(self._frame_cache):
            self._render_raw(self._frame_cache[frame_num])
            return
        raw = self._fetch_frame(frame_num)
        if raw:
            self._render_raw(raw)

    # ------------------------------------------------------------------ #
    #  Rendering                                                           #
    # ------------------------------------------------------------------ #

    def _render_raw(self, raw: bytes):
        """Upload raw RGB24 bytes to the GL canvas."""
        w, h = self._effective_size()
        if w == 0 or h == 0:
            return
        self._canvas.set_frame(raw, w, h)

    def _refresh_display(self):
        """Push current zoom/pan state to the GL canvas."""
        self._canvas.set_transform(self._zoom, self._pan_x, self._pan_y)

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
            self._close_pipe()
            self._stop_audio()

    def _on_scrubber_released(self):
        self._scrubber_moving = False
        self._current_frame   = self._scrubber.value()
        if self._is_playing:
            self._stop_audio()
            if self._play_reverse:
                self._reverse_cache = []
            elif self._frame_cache is not None:
                self._pipe_frame = self._current_frame
            else:
                self._open_pipe(self._current_frame)
            self._play_frame_start = self._current_frame
            self._play_clock_start = time.monotonic()
            self._start_audio()
            self._timer.start(max(1, int(1000 / (self._fps * self._speed))))
        else:
            self._show_frame(self._current_frame)
            self._update_info()

    def _on_scrubber_moved(self, value: int):
        if not self._path:
            return
        if self._frame_cache and value < len(self._frame_cache):
            self._render_raw(self._frame_cache[value])
        else:
            raw = self._fetch_frame(value)
            if raw:
                self._render_raw(raw)
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
        if not checked:
            self._close_lookahead()
            self._close_loop_pipe()

    # ------------------------------------------------------------------ #
    #  Audio                                                               #
    # ------------------------------------------------------------------ #

    def _start_audio(self):
        self._stop_audio()
        ffplay = _ffplay_exe()
        if not Path(ffplay).exists():
            return
        seek = self._current_frame / self._fps
        cmd = [ffplay, "-nodisp", "-autoexit",
               "-ss",     f"{seek:.4f}",
               "-volume", str(self._volume),
               self._path]
        try:
            self._audio_proc = subprocess.Popen(
                cmd,
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
        """Short audio snippet at current frame for scrub feedback."""
        if not self._path:
            return
        ffplay = _ffplay_exe()
        if not Path(ffplay).exists():
            return
        if self._scrub_proc and self._scrub_proc.poll() is None:
            self._scrub_proc.terminate()
        seek = self._current_frame / self._fps
        try:
            self._scrub_proc = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit",
                 "-ss",     f"{seek:.4f}",
                 "-t",      "0.15",
                 "-volume", str(self._volume),
                 self._path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _toggle_mute(self):
        if self._volume > 0:
            self._pre_mute_volume = self._volume
            self._vol_slider.setValue(0)
        else:
            self._vol_slider.setValue(self._pre_mute_volume)

    def _on_volume_changed(self, value: int):
        self._volume = value
        self._vol_lbl.setText(f"{value}%")
        icon = "mute.png" if value == 0 else "volume-up.png"
        self._vol_btn.setIcon(_icon(icon))
        if self._is_playing:
            self._stop_audio()
            self._start_audio()

    # ------------------------------------------------------------------ #
    #  Info update / frame callback                                        #
    # ------------------------------------------------------------------ #

    def _update_info(self):
        f = self._current_frame
        self._frame_num_lbl.setText(str(f + 1))
        self._frames_lbl.setText(f"{self._total_frames} frames")
        self._fps_lbl.setText(f"{self._fps:.2f} fps")
        if hasattr(self, '_on_frame_changed'):
            self._on_frame_changed(f + 1)

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
        self.zoom_in() if direction > 0 else self.zoom_out()

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
        """
        Re-fetch the current frame with updated -vf filters.
        Cache, lookahead, and loop pipe are all invalidated — they were built
        with the old filter chain.
        """
        self._close_lookahead()
        self._close_loop_pipe()
        self._frame_cache   = None
        self._cache_loading = False
        if self._is_playing and not self._play_reverse:
            self._open_pipe(self._current_frame)
        elif self._path and self._current_frame >= 0:
            self._show_frame(self._current_frame)
        self._start_cache_build()

    # ------------------------------------------------------------------ #
    #  Keyboard                                                            #
    # ------------------------------------------------------------------ #

    def keyPressEvent(self, event):
        k    = event.key()
        mods = event.modifiers()
        ctrl = mods & Qt.ControlModifier

        if   k == Qt.Key_Space:                  self._toggle_play()
        elif k == Qt.Key_L:                      self._play(reverse=False)
        elif k == Qt.Key_J:                      self._play(reverse=True)
        elif k == Qt.Key_K:                      self._pause()
        elif k == Qt.Key_Left  and not ctrl:     self._step_back()
        elif k == Qt.Key_Right and not ctrl:     self._step_forward()
        elif k == Qt.Key_Home:                   self._go_first()
        elif k == Qt.Key_End:                    self._go_last()
        elif k == Qt.Key_BracketLeft:            self.set_in_frame()
        elif k == Qt.Key_BracketRight:           self.set_out_frame()
        elif k == Qt.Key_Backslash and ctrl:     self.clear_in_out()
        elif k == Qt.Key_Equal and ctrl:         self.zoom_in()
        elif k == Qt.Key_Minus and ctrl:         self.zoom_out()
        elif k == Qt.Key_0     and ctrl:         self.zoom_reset()
        elif k == Qt.Key_Backslash and not ctrl: self.zoom_reset()
        else: super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    #  Cleanup                                                             #
    # ------------------------------------------------------------------ #

    def _cleanup(self):
        self._timer.stop()
        self._close_pipe()
        self._close_loop_pipe()
        self._close_lookahead()
        self._stop_audio()
        self._path             = ""
        self._current_frame    = 0
        self._is_playing       = False
        self._reverse_cache    = []
        self._cache_building   = False
        self._loop_frame0      = None
        self._frame_cache      = None
        self._cache_loading    = False
        self._play_clock_start = 0.0
        self._play_frame_start = 0
        self._in_frame  = None
        self._out_frame = None
        self._scrubber.set_in_out(None, None)
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._canvas.clear_frame()

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

    _SETTINGS_ORG  = "BlastPlayer"
    _SETTINGS_APP  = "BlastPlayer"

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
        self._restore_geometry()
        self._restore_settings()

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
        qa = fm.addAction("Quit"); qa.setShortcut("Ctrl+Q")
        qa.setMenuRole(QAction.QuitRole); qa.triggered.connect(self.close)

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
            a.triggered.connect(lambda _, _i=i: self._player._speed_combo.setCurrentIndex(_i))
        pb.addSeparator()
        pb.addAction("Play / Pause").setShortcut("Space")
        pb.actions()[-1].triggered.connect(self._player._toggle_play)
        pb.addAction("Play Forwards").setShortcut("L")
        pb.actions()[-1].triggered.connect(lambda: self._player._play(reverse=False))
        pb.addAction("Play Backwards").setShortcut("J")
        pb.actions()[-1].triggered.connect(lambda: self._player._play(reverse=True))
        pb.addAction("Pause").setShortcut("K")
        pb.actions()[-1].triggered.connect(self._player._pause)
        pb.addSeparator()
        pb.addAction("Go to Start").setShortcut("Home")
        pb.actions()[-1].triggered.connect(self._player._go_first)
        pb.addAction("Go to End").setShortcut("End")
        pb.actions()[-1].triggered.connect(self._player._go_last)
        pb.addSeparator()
        self._loop_act = pb.addAction("Loop"); self._loop_act.setCheckable(True)
        self._loop_act.triggered.connect(self._player._loop_btn.setChecked)
        self._player._loop_btn.toggled.connect(self._loop_act.setChecked)
        pb.addSeparator()
        ss = pb.addMenu("Stepping and Scrubbing")
        self._loop_on_step_act = ss.addAction("Loop on Step")
        self._loop_on_step_act.setShortcut("Ctrl+Shift+.")
        self._loop_on_step_act.setCheckable(True)
        self._loop_on_step_act.triggered.connect(
            lambda c: setattr(self._player, "_loop_on_step", c))
        self._loop_on_scrub_act = ss.addAction("Loop on Scrub")
        self._loop_on_scrub_act.setShortcut("Ctrl+Alt+.")
        self._loop_on_scrub_act.setCheckable(True)
        self._loop_on_scrub_act.triggered.connect(
            lambda c: setattr(self._player, "_loop_on_scrub", c))
        pb.addSeparator()
        io_menu = pb.addMenu("In / Out")
        set_in_act = io_menu.addAction("Set In Point")
        set_in_act.setShortcut("[")
        set_in_act.triggered.connect(self._player.set_in_frame)
        set_out_act = io_menu.addAction("Set Out Point")
        set_out_act.setShortcut("]")
        set_out_act.triggered.connect(self._player.set_out_frame)
        io_menu.addSeparator()
        clear_io_act = io_menu.addAction("Clear In / Out")
        clear_io_act.setShortcut("Ctrl+\\")
        clear_io_act.triggered.connect(self._player.clear_in_out)

        # Audio
        am = mb.addMenu("Audio")
        vol_up_act = am.addAction("Volume Up")
        vol_up_act.setShortcut("Shift+Up")
        vol_up_act.triggered.connect(
            lambda: self._player._vol_slider.setValue(
                min(100, self._player._vol_slider.value() + 5)))
        vol_dn_act = am.addAction("Volume Down")
        vol_dn_act.setShortcut("Shift+Down")
        vol_dn_act.triggered.connect(
            lambda: self._player._vol_slider.setValue(
                max(0, self._player._vol_slider.value() - 5)))
        am.addSeparator()
        self._mute_act = am.addAction("Mute")
        self._mute_act.setCheckable(True)
        self._mute_act.setShortcut("Ctrl+M")
        self._mute_act.triggered.connect(self._player._toggle_mute)
        self._player._vol_slider.valueChanged.connect(
            lambda v: self._mute_act.setChecked(v == 0))
        am.addSeparator()
        self._scrub_act = am.addAction("Audio Scrubbing")
        self._scrub_act.setCheckable(True)
        self._scrub_act.triggered.connect(
            lambda checked: setattr(self._player, "_audio_scrub_enabled", checked))

        # Video
        vm = mb.addMenu("Video")
        fs_act = vm.addAction("Fullscreen"); fs_act.setShortcut("F11")
        fs_act.triggered.connect(self._toggle_fullscreen)
        vm.addSeparator()
        pz = vm.addMenu("Pan / Zoom")
        pz.addAction("Zoom In").setShortcut("Ctrl+=")
        pz.actions()[-1].triggered.connect(self._player.zoom_in)
        pz.addAction("Zoom Out").setShortcut("Ctrl+-")
        pz.actions()[-1].triggered.connect(self._player.zoom_out)
        pz.addAction("Reset Pan/Zoom").setShortcut("Ctrl+0")
        pz.actions()[-1].triggered.connect(self._player.zoom_reset)
        vm.addSeparator()
        vm.addAction("Rotate CW").setShortcut("Ctrl+Shift+M")
        vm.actions()[-1].triggered.connect(self._player.rotate_cw)
        vm.addAction("Rotate CCW").setShortcut("Ctrl+Shift+N")
        vm.actions()[-1].triggered.connect(self._player.rotate_ccw)
        vm.addSeparator()
        vm.addAction("Flip Horizontal").setShortcut("Ctrl+X")
        vm.actions()[-1].triggered.connect(self._player.flip_horizontal)
        vm.addAction("Flip Vertical").setShortcut("Ctrl+Shift+X")
        vm.actions()[-1].triggered.connect(self._player.flip_vertical)

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
        about_act.setMenuRole(QAction.NoRole)   # prevent macOS from auto-moving to app menu
        about_act.triggered.connect(self._on_about)

    # ------------------------------------------------------------------ #
    #  Open video                                                          #
    # ------------------------------------------------------------------ #

    def open_video(self, path: str):
        if not self._player.load_video(path):
            QMessageBox.warning(self, "Cannot open", f"Could not open:\n{path}")
            return
        self._current_filename = Path(path).name
        self._stack.setCurrentIndex(1)
        self._player.setFocus()
        self._update_title(0)
        # PlayerWidget was hidden during load_video so _display had no size yet;
        # defer one tick so the layout has settled before we scale the pixmap.
        QTimer.singleShot(0, self._player._refresh_display)

    def _setup_shortcuts(self):
        pass  # handled via keyPressEvent

    def _restore_geometry(self):
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
        geom = s.value("windowGeometry")
        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(1280, 720)

    def _save_geometry(self):
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
        s.setValue("windowGeometry", self.saveGeometry())

    def _restore_settings(self):
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)

        # Volume / mute — set slider which propagates to _volume and mute button icon
        pre_mute = int(s.value("preMuteVolume", 100))
        muted    = s.value("muted", False, type=bool)
        volume   = int(s.value("volume", 100))
        self._player._pre_mute_volume = pre_mute
        self._player._vol_slider.setValue(0 if muted else volume)

        # Audio scrubbing
        scrub = s.value("audioScrubbing", False, type=bool)
        self._player._audio_scrub_enabled = scrub
        self._scrub_act.setChecked(scrub)

        # Loop — setChecked triggers _on_loop_toggled which syncs _loop and the menu action
        loop = s.value("loop", False, type=bool)
        self._player._loop_btn.setChecked(loop)

        # Loop on step / scrub
        loop_step  = s.value("loopOnStep",  False, type=bool)
        loop_scrub = s.value("loopOnScrub", False, type=bool)
        self._player._loop_on_step  = loop_step
        self._player._loop_on_scrub = loop_scrub
        self._loop_on_step_act.setChecked(loop_step)
        self._loop_on_scrub_act.setChecked(loop_scrub)

        # Playback speed
        speed_idx = int(s.value("speedIndex", 3))
        speed_idx = max(0, min(speed_idx, len(PlayerWidget._SPEEDS) - 1))
        self._player._speed_combo.setCurrentIndex(speed_idx)

    def _save_settings(self):
        s = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
        s.setValue("volume",         self._player._volume)
        s.setValue("preMuteVolume",  self._player._pre_mute_volume)
        s.setValue("muted",          self._player._volume == 0)
        s.setValue("audioScrubbing", self._player._audio_scrub_enabled)
        s.setValue("loop",           self._player._loop)
        s.setValue("loopOnStep",     self._player._loop_on_step)
        s.setValue("loopOnScrub",    self._player._loop_on_scrub)
        s.setValue("speedIndex",     self._player._speed_combo.currentIndex())

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            self._toggle_fullscreen()
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)

    def _on_about(self):
        try:
            from dialogs.about_dialog import AboutDialog
            AboutDialog(self).exec_()
        except ImportError:
            QMessageBox.about(self, "BlastPlayer",
                "BlastPlayer\nFFmpeg-powered frame-accurate video player")

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
        self._save_geometry()
        self._save_settings()
        self._player.stop()
        super().closeEvent(event)
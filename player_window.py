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
import os
import re
import json
import time
import array
import queue
import threading
import subprocess
import tempfile
from pathlib import Path

from core.audio_engine import AudioEngine

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QSizePolicy,
    QAction, QFileDialog, QMessageBox,
    QFrame, QToolButton, QActionGroup, QComboBox, QShortcut,
    QOpenGLWidget, QListWidget, QListWidgetItem, QMenu, QSplitter,
    QApplication, QLineEdit, QInputDialog, QProgressBar,
    QStyledItemDelegate, QAbstractItemView, QStyle,
)
from PyQt5.QtCore import Qt, QTimer, QSize, QRect, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import (
    QKeySequence, QPainter, QPen, QColor,
    QPixmap, QFont, QFontMetrics, QDragEnterEvent, QDropEvent, QIcon,
    QOpenGLShaderProgram, QOpenGLShader, QOpenGLBuffer,
    QSurfaceFormat, QOpenGLVertexArrayObject,
)
try:
    from OpenGL.GL import (
        glClearColor, glClear, glViewport, glDrawArrays,
        glGenTextures, glDeleteTextures, glBindTexture,
        glTexImage2D, glTexSubImage2D, glTexImage3D,
        glTexParameteri, glActiveTexture, glUniform1i,
        GL_COLOR_BUFFER_BIT, GL_TRIANGLES, GL_FLOAT,
        GL_TEXTURE_2D, GL_TEXTURE_3D,
        GL_RGB, GL_RGBA, GL_RGB8, GL_RGB16, GL_RGBA32F,
        GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
        GL_TEXTURE0, GL_TEXTURE1,
        GL_LINEAR, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
        GL_CLAMP_TO_EDGE, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
        GL_TEXTURE_WRAP_R,
    )
except ImportError:
    raise SystemExit(
        "[BlastPlayer] PyOpenGL is required for GPU rendering.\n"
        "Install it with:  pip install PyOpenGL"
    )
from PyQt5.QtWidgets import QStyle, QStyleOptionSlider

from core import constants
from core.ocio_manager import OCIOManager, OCIO_AVAILABLE


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
#  Playlist sidebar — helpers
# ══════════════════════════════════════════════════════════════════════════════

_THUMB_W = 80    # thumbnail width  (px)
_THUMB_H = 45    # thumbnail height (px, ~16:9)
_ROW_H   = 68    # list-row height  (px)


def _fmt_duration(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    if seconds <= 0:
        return "--:--"
    s = int(round(seconds))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _ThumbnailLoader(QThread):
    """Extracts the first video frame as a QPixmap in a background thread."""
    done = pyqtSignal(str, object)   # (path, QPixmap)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        ext = Path(self._path).suffix.lower()
        is_image_seq = ext in {".exr", ".dpx"}

        if is_image_seq:
            # Open the literal file path — don't let ffmpeg glob a sequence pattern
            cmd = [_ffmpeg_exe(), "-y",
                   "-pattern_type", "none", "-i", self._path,
                   "-vframes", "1",
                   "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
        else:
            cmd = [_ffmpeg_exe(), "-y", "-i", self._path,
                   "-vf", "select=eq(n\\,0)", "-vframes", "1",
                   "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            data = proc.stdout.read()
            proc.wait()
        except Exception:
            return
        if not data:
            return
        pix = QPixmap()
        pix.loadFromData(bytes(data))
        if pix.isNull():
            return
        pix = pix.scaled(_THUMB_W, _THUMB_H,
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.done.emit(self._path, pix)


class _PlaylistDelegate(QStyledItemDelegate):
    """Renders each row: thumbnail | label + filename + duration/frame info."""

    _loop_pix: QPixmap | None = None   # cached once, shared across all instances

    def __init__(self, sidebar: "PlaylistSidebar", parent=None) -> None:
        super().__init__(parent)
        self._sb = sidebar
        if _PlaylistDelegate._loop_pix is None:
            raw = QPixmap(str(constants.ICONS_DIR / "loop.png"))
            _PlaylistDelegate._loop_pix = (
                raw.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                if not raw.isNull() else QPixmap()
            )

    def sizeHint(self, _option, _index) -> QSize:
        return QSize(0, _ROW_H)

    def paint(self, painter, option, index) -> None:
        painter.save()

        path  = index.data(Qt.UserRole)     or ""
        meta  = index.data(Qt.UserRole + 1) or {}
        thumb = index.data(Qt.UserRole + 2)

        selected = bool(option.state & QStyle.State_Selected)
        playing  = (path == self._sb._current_playing)
        r        = option.rect

        # ── Background ──────────────────────────────────────────────────
        if selected:
            painter.fillRect(r, QColor(constants.ACCENT_HI))
        elif playing:
            painter.fillRect(r, QColor("#111d2a"))
        else:
            painter.fillRect(r, QColor("#1c1c1c"))

        # Left accent bar for the currently playing clip
        if playing:
            painter.fillRect(r.x(), r.y(), 3, r.height(),
                             QColor(constants.ACCENT_HI))

        # Bottom row separator
        painter.setPen(QPen(QColor(constants.BORDER)))
        painter.drawLine(r.bottomLeft(), r.bottomRight())

        # ── Thumbnail ───────────────────────────────────────────────────
        tx = r.x() + 8
        ty = r.y() + (r.height() - _THUMB_H) // 2
        if thumb and not thumb.isNull():
            ox = (_THUMB_W - thumb.width())  // 2
            oy = (_THUMB_H - thumb.height()) // 2
            painter.drawPixmap(tx + ox, ty + oy, thumb)
        else:
            slot = QRect(tx, ty, _THUMB_W, _THUMB_H)
            painter.fillRect(slot, QColor("#252525"))
            painter.setPen(QPen(QColor("#3c3c3c")))
            painter.drawRect(slot.adjusted(0, 0, -1, -1))

        # ── Text block ──────────────────────────────────────────────────
        lx = tx + _THUMB_W + 8
        lw = r.right() - lx - 6

        t_col = QColor("white")   if selected else QColor(constants.TEXT_PRI)
        s_col = QColor("#c0d8f8") if selected else QColor(constants.TEXT_SEC)

        label    = meta.get("label",        Path(path).name if path else "")
        filename = Path(path).name          if path else ""
        duration = meta.get("duration",     0.0)
        frames   = meta.get("total_frames", 0)
        fps      = meta.get("fps",          0.0)
        loop_on  = meta.get("loop_this",    False)

        # Row 1 — label (bold 9 pt)
        f = painter.font()
        f.setPointSize(9)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(t_col)
        badge_w  = 22 if loop_on else 0
        elided   = QFontMetrics(f).elidedText(label, Qt.ElideRight, lw - badge_w)
        painter.drawText(QRect(lx, r.y() + 8, lw - badge_w, 17),
                         Qt.AlignLeft | Qt.AlignVCenter, elided)

        # Loop badge (top-right corner of text block)
        if loop_on and self._loop_pix and not self._loop_pix.isNull():
            painter.drawPixmap(r.right() - 22, r.y() + 8, self._loop_pix)

        # Row 2 — filename in grey (only shown when the label was renamed)
        f.setBold(False)
        f.setPointSize(8)
        painter.setFont(f)
        painter.setPen(s_col)
        if label != filename:
            el2 = QFontMetrics(f).elidedText(filename, Qt.ElideRight, lw)
            painter.drawText(QRect(lx, r.y() + 27, lw, 14),
                             Qt.AlignLeft | Qt.AlignVCenter, el2)

        # Row 3 — duration · frame count · fps  (bottom-aligned)
        parts: list[str] = []
        if duration > 0:
            parts.append(_fmt_duration(duration))
        if frames:
            parts.append(f"{frames} fr")
        if fps:
            parts.append(f"{fps:.4g} fps")
        info = "  ·  ".join(parts)
        painter.drawText(QRect(lx, r.bottom() - 20, lw, 16),
                         Qt.AlignLeft | Qt.AlignVCenter, info)

        painter.restore()


# ══════════════════════════════════════════════════════════════════════════════
#  Playlist sidebar
# ══════════════════════════════════════════════════════════════════════════════

class PlaylistSidebar(QWidget):
    video_selected           = pyqtSignal(str)
    selection_play_requested = pyqtSignal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(220)
        self.setAcceptDrops(True)

        self._paths:            list = []
        self._active_selection: list = []
        self._current_playing:  str  = ""
        self._thumb_threads:    dict = {}   # path → _ThumbnailLoader

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────── #

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header bar
        hdr = QWidget()
        hdr.setFixedHeight(34)
        hdr.setStyleSheet(f"background:{constants.BORDER};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(10, 0, 4, 0)
        hl.setSpacing(3)

        title = QLabel("Playlist")
        title.setStyleSheet(
            f"color:{constants.TEXT_PRI};font-size:12px;"
            f"font-weight:bold;background:transparent;")
        hl.addWidget(title)
        hl.addStretch()

        self._up_btn   = self._mk_hdr_btn("caret-arrow-up.png", "Move up")
        self._down_btn = self._mk_hdr_btn("down.png", "Move down")
        self._up_btn.clicked.connect(self._move_up)
        self._down_btn.clicked.connect(self._move_down)
        hl.addWidget(self._up_btn)
        hl.addWidget(self._down_btn)
        hl.addSpacing(4)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedSize(40, 22)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.setStyleSheet(f"""
            QPushButton{{background:{constants.ACCENT};color:{constants.TEXT_SEC};
                border:none;border-radius:3px;font-size:10px;}}
            QPushButton:hover{{background:{constants.ACCENT_HI};color:white;}}
        """)
        clear_btn.clicked.connect(self.clear)
        hl.addWidget(clear_btn)
        root.addWidget(hdr)

        # Search / filter bar
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(26)
        self._search.setStyleSheet(f"""
            QLineEdit{{background:#141414;color:{constants.TEXT_PRI};
                border:none;border-bottom:1px solid {constants.BORDER};
                font-size:11px;padding:0 8px;}}
            QLineEdit:focus{{border-bottom:1px solid {constants.ACCENT_HI};}}
        """)
        self._search.textChanged.connect(self._apply_filter)
        root.addWidget(self._search)

        # Audio-loading indicator — thin indeterminate bar, hidden by default
        self._audio_bar = QProgressBar()
        self._audio_bar.setRange(0, 0)          # indeterminate (pulsing)
        self._audio_bar.setFixedHeight(3)
        self._audio_bar.setTextVisible(False)
        self._audio_bar.setStyleSheet(f"""
            QProgressBar{{background:{constants.BORDER};border:none;}}
            QProgressBar::chunk{{background:{constants.ACCENT_HI};}}
        """)
        self._audio_bar.hide()
        root.addWidget(self._audio_bar)

        # Clip list
        self._list = QListWidget()
        self._list.setItemDelegate(_PlaylistDelegate(self, self._list))
        self._list.setStyleSheet(
            "QListWidget{background:#1c1c1c;border:none;outline:none;}"
            "QListWidget::item{border:none;padding:0;}")
        self._list.verticalScrollBar().setStyleSheet(f"""
            QScrollBar:vertical{{background:#1c1c1c;width:5px;border:none;margin:0;}}
            QScrollBar::handle:vertical{{background:{constants.ACCENT};
                border-radius:2px;min-height:20px;}}
            QScrollBar::handle:vertical:hover{{background:{constants.ACCENT_HI};}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
        """)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.setDragDropMode(QListWidget.InternalMove)
        self._list.setDefaultDropAction(Qt.MoveAction)
        self._list.model().rowsMoved.connect(self._sync_paths)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemClicked.connect(self._on_item_clicked)
        root.addWidget(self._list, stretch=1)

    def _mk_hdr_btn(self, icon_name: str, tip: str) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(_icon(icon_name))
        btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(22, 22)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(tip)
        btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:none;}}
            QPushButton:hover{{background:{constants.ACCENT};border-radius:3px;}}
        """)
        return btn

    # ── Public API ───────────────────────────────────────────────────────── #

    def add_video(self, path: str) -> None:
        if path in self._paths:
            return

        # Probe for duration / frame metadata (ffprobe, fast)
        try:
            info     = probe_video(path)
            fps      = float(info.get("fps", 0))
            frames   = int(info.get("total_frames", 0))
            duration = frames / fps if fps else 0.0
        except Exception:
            fps = frames = duration = 0

        self._paths.append(path)

        meta = {
            "label":        Path(path).name,
            "fps":          fps,
            "total_frames": frames,
            "duration":     duration,
            "loop_this":    False,
        }
        item = QListWidgetItem()
        item.setData(Qt.UserRole,     path)
        item.setData(Qt.UserRole + 1, meta)
        item.setData(Qt.UserRole + 2, None)   # thumbnail slot (filled async)
        item.setToolTip(path)
        item.setSizeHint(QSize(0, _ROW_H))
        self._list.addItem(item)

        self._start_thumb(path)

    def set_current(self, path: str) -> None:
        """Highlight *path* as the currently playing clip and scroll to it."""
        self._current_playing = path
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == path:
                self._list.setCurrentRow(i)
                self._list.scrollToItem(
                    self._list.item(i), QAbstractItemView.EnsureVisible)
                break
        else:
            self._list.clearSelection()
        self._list.viewport().update()

    def next_path(self, current_path: str):
        """Return the path after *current_path*, respecting loop-this-clip."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.UserRole) == current_path:
                if (item.data(Qt.UserRole + 1) or {}).get("loop_this"):
                    return current_path   # replay same clip
                break

        source = self._active_selection if self._active_selection else self._paths
        try:
            idx = source.index(current_path)
            if idx + 1 < len(source):
                return source[idx + 1]
        except ValueError:
            pass
        return None

    def clear(self) -> None:
        self._cleanup_threads()
        self._list.clear()
        self._paths.clear()
        self._active_selection.clear()
        self._current_playing = ""

    def set_audio_loading(self, loading: bool) -> None:
        """Show or hide the indeterminate loading bar while PCM is decoding."""
        if loading:
            self._audio_bar.show()
        else:
            self._audio_bar.hide()

    # ── Drag-and-drop (OS → sidebar) ─────────────────────────────────────── #

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if Path(p).suffix.lower() in constants.VIDEO_EXTS:
                self.add_video(p)

    # ── Thumbnail loading ─────────────────────────────────────────────────── #

    def _start_thumb(self, path: str) -> None:
        if path in self._thumb_threads:
            return
        t = _ThumbnailLoader(path, self)
        t.done.connect(self._on_thumb_ready)
        t.finished.connect(lambda p=path: self._thumb_threads.pop(p, None))
        self._thumb_threads[path] = t
        t.start()

    def _on_thumb_ready(self, path: str, pix: object) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.UserRole) == path:
                item.setData(Qt.UserRole + 2, pix)
                self._list.viewport().update()
                break

    def _cleanup_threads(self) -> None:
        for t in list(self._thumb_threads.values()):
            t.quit()
            t.wait(300)
        self._thumb_threads.clear()

    # ── List interaction ─────────────────────────────────────────────────── #

    def _on_item_clicked(self, item) -> None:
        if QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return   # modifier held — update selection only, don't play
        self._active_selection.clear()
        self.video_selected.emit(item.data(Qt.UserRole))

    def _play_selected(self) -> None:
        items = sorted(self._list.selectedItems(),
                       key=lambda it: self._list.row(it))
        if not items:
            return
        clip_infos = [
            {
                "path":      it.data(Qt.UserRole),
                "loop_this": (it.data(Qt.UserRole + 1) or {}).get("loop_this", False),
            }
            for it in items
        ]
        self._active_selection = [c["path"] for c in clip_infos]
        self.selection_play_requested.emit(clip_infos)

    def _apply_filter(self, text: str) -> None:
        text = text.lower().strip()
        for i in range(self._list.count()):
            item = self._list.item(i)
            path  = item.data(Qt.UserRole) or ""
            label = (item.data(Qt.UserRole + 1) or {}).get("label", "").lower()
            match = not text or text in label or text in path.lower()
            item.setHidden(not match)

    def _move_up(self) -> None:
        row = self._list.currentRow()
        if row <= 0:
            return
        item = self._list.takeItem(row)
        self._list.insertItem(row - 1, item)
        self._list.setCurrentRow(row - 1)
        self._sync_paths()

    def _move_down(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= self._list.count() - 1:
            return
        item = self._list.takeItem(row)
        self._list.insertItem(row + 1, item)
        self._list.setCurrentRow(row + 1)
        self._sync_paths()

    def _sync_paths(self) -> None:
        self._paths = [
            self._list.item(i).data(Qt.UserRole)
            for i in range(self._list.count())
        ]

    def _remove_item(self, item) -> None:
        path = item.data(Qt.UserRole)
        self._list.takeItem(self._list.row(item))
        self._paths = [p for p in self._paths if p != path]
        if path in self._active_selection:
            self._active_selection.remove(path)

    def _rename_item(self, item) -> None:
        meta = item.data(Qt.UserRole + 1) or {}
        cur  = meta.get("label", Path(item.data(Qt.UserRole)).name)
        text, ok = QInputDialog.getText(self, "Rename clip", "Label:", text=cur)
        if ok and text.strip():
            meta["label"] = text.strip()
            item.setData(Qt.UserRole + 1, meta)
            self._list.viewport().update()

    def _toggle_loop_item(self, item) -> None:
        meta = item.data(Qt.UserRole + 1) or {}
        meta["loop_this"] = not meta.get("loop_this", False)
        item.setData(Qt.UserRole + 1, meta)
        self._list.viewport().update()

    # ── Context menu ─────────────────────────────────────────────────────── #

    _MENU_STYLE = f"""
        QMenu{{background:{constants.BORDER};color:{constants.TEXT_PRI};
            border:1px solid {constants.SPLITTER_COLOR};font-size:11px;}}
        QMenu::item{{padding:6px 18px;}}
        QMenu::item:selected{{background:{constants.ACCENT_HI};color:white;}}
        QMenu::separator{{height:1px;background:{constants.SPLITTER_COLOR};margin:2px 0;}}
    """

    def _show_context_menu(self, pos) -> None:
        menu     = QMenu(self)
        menu.setStyleSheet(self._MENU_STYLE)
        item     = self._list.itemAt(pos)
        selected = self._list.selectedItems()

        add_act = menu.addAction("Add Videos…")
        add_act.triggered.connect(self._on_add_clicked)

        if len(selected) >= 2:
            menu.addSeparator()
            act = menu.addAction(f"Play Selected  ({len(selected)})")
            act.triggered.connect(self._play_selected)

        if item is not None:
            menu.addSeparator()
            meta    = item.data(Qt.UserRole + 1) or {}
            loop_on = meta.get("loop_this", False)
            loop_act = menu.addAction("Loop this clip")
            if loop_on:
                loop_act.setIcon(_icon("check.png"))
            loop_act.triggered.connect(lambda: self._toggle_loop_item(item))

            ren_act = menu.addAction("Rename…")
            ren_act.triggered.connect(lambda: self._rename_item(item))

            menu.addSeparator()
            rem_act = menu.addAction("Remove")
            rem_act.triggered.connect(lambda: self._remove_item(item))

        menu.exec_(self._list.mapToGlobal(pos))

    def _on_add_clicked(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(constants.VIDEO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add videos", "",
            f"Video files ({exts});;All files (*)")
        for p in paths:
            self.add_video(p)


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
        self._mc_clips  = []     # list of clip dicts for multi-clip band painting

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

    def set_mc_clips(self, clips: list):
        self._mc_clips = clips
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._in_frame is None and self._out_frame is None and not self._mc_clips:
            return

        opt    = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        span   = groove.width() - handle.width()
        offset = groove.x() + handle.width() // 2
        gy     = groove.center().y()
        gh     = groove.height()

        def x_for(val):
            return offset + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), val, span)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        # Multi-clip bands: alternate a light tint on odd-indexed clips
        if len(self._mc_clips) > 1:
            band_color = QColor(255, 255, 255, 70)
            div_color  = QColor(255, 255, 255, 100)
            bh = max(gh + 4, 8)   # band taller than the groove for visibility
            clips = self._mc_clips
            for i, clip in enumerate(clips):
                x_start = x_for(clip['offset'])
                # Extend band to the pixel just before the next clip starts so
                # there is no single-frame gap between adjacent bands.
                if i < len(clips) - 1:
                    x_end = x_for(clips[i + 1]['offset'])
                else:
                    x_end = x_for(self.maximum()) + 1
                if i % 2 == 1 and x_end > x_start:
                    painter.fillRect(x_start, gy - bh // 2,
                                     x_end - x_start, bh, band_color)
                # Divider at each clip boundary except the first
                if i > 0:
                    painter.setPen(QPen(div_color, 1))
                    painter.drawLine(x_start, gy - bh // 2, x_start, gy + bh // 2)

        # Tinted range between in and out
        x_in  = x_for(self._in_frame  if self._in_frame  is not None else self.minimum())
        x_out = x_for(self._out_frame if self._out_frame is not None else self.maximum())
        if self._in_frame is not None or self._out_frame is not None:
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

    Supports:
    - 8-bit RGB24 and 16-bit rgb48le (EXR/DPX) frames
    - Pre-allocated GPU texture pool (gpu cache) for zero-upload seeks
    - OCIO colour-management via injected GLSL function + LUT textures
    """

    zoom_scrolled = pyqtSignal(int)       # +1 = in, -1 = out
    pan_dragged   = pyqtSignal(int, int)  # dx, dy

    # GLSL 1.50 core — OpenGL 3.2 Core Profile (required by macOS; supported
    # on all modern Windows/Linux drivers too).
    _VERT_SRC = """
#version 150 core
in  vec2 a_pos;
in  vec2 a_tex;
out vec2 v_tex;
void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
    v_tex = a_tex;
}
"""
    # Base fragment shader; OCIO function + call are injected at markers.
    _FRAG_BASE = """
#version 150 core
uniform sampler2D u_frame;
// OCIO_INJECT
in  vec2 v_tex;
out vec4 fragColor;
void main() {
    vec4 c = texture(u_frame, v_tex);
// OCIO_CALL
    fragColor = c;
}
"""

    def __init__(self, parent=None):
        fmt = QSurfaceFormat()
        fmt.setSwapInterval(0)
        # OpenGL 3.2 Core Profile — the minimum that supports GLSL 1.50.
        # macOS requires Core Profile for anything above 2.1.
        # All modern Windows/Linux drivers support 3.2 Core.
        fmt.setVersion(3, 2)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(parent)

        self._frame_raw  = None   # bytes | None  — current SDR/HDR frame
        self._vid_w      = 0
        self._vid_h      = 0
        self._is_hdr     = False  # True → rgb48le (GL_UNSIGNED_SHORT)
        self._zoom       = 1.0
        self._pan_x      = 0
        self._pan_y      = 0
        self._dirty      = False
        self._drag_pos   = None

        # GL objects — initialised in initializeGL
        self._vao        = None
        self._prog       = None
        self._vbo        = None
        self._tex_id     = None   # streaming texture for set_frame()
        self._tex_w      = 0
        self._tex_h      = 0

        # GPU texture cache
        self._tex_pool      = []   # list[int] — pre-allocated texture IDs
        self._tex_cache     = {}   # frame_num → tex_pool index
        self._cache_w       = 0
        self._cache_h       = 0
        self._cache_is_hdr  = False
        self._draw_tex_id   = None  # if set, paintGL binds this instead of uploading

        # OCIO
        self._ocio_enabled  = False
        self._ocio_func_src = ""
        self._ocio_lut_info = []   # list[(sampler_name, tex_id, is_3d)]

    # ── Public API ───────────────────────────────────────────────────── #

    def set_frame(self, raw: bytes, vid_w: int, vid_h: int, is_hdr: bool = False):
        self._frame_raw = raw
        self._vid_w     = vid_w
        self._vid_h     = vid_h
        self._is_hdr    = is_hdr
        self._dirty     = True
        self._draw_tex_id = None
        self.update()

    def set_cached_frame(self, tex_id: int):
        """Render a previously cached texture (skips CPU→GPU upload)."""
        self._draw_tex_id = tex_id
        self.update()

    def clear_frame(self):
        self._frame_raw   = None
        self._draw_tex_id = None
        self.update()

    def set_transform(self, zoom: float, pan_x: int, pan_y: int):
        self._zoom  = zoom
        self._pan_x = pan_x
        self._pan_y = pan_y
        self.update()

    # ── GPU Texture Cache API ─────────────────────────────────────────── #

    def begin_gpu_cache(self, n_frames: int, w: int, h: int, is_hdr: bool):
        """Pre-allocate *n_frames* textures for the GPU cache (main thread only)."""
        self.clear_gpu_cache()
        self.makeCurrent()
        ids = glGenTextures(n_frames)
        if isinstance(ids, int):      # glGenTextures(1) returns a scalar
            ids = [ids]
        for tid in ids:
            self._alloc_texture(int(tid), w, h, is_hdr)
        self._tex_pool     = [int(t) for t in ids]
        self._cache_w      = w
        self._cache_h      = h
        self._cache_is_hdr = is_hdr

    def upload_gpu_frame(self, frame_num: int, raw: bytes):
        """Upload *raw* into the pool slot for *frame_num* (main thread only)."""
        if frame_num >= len(self._tex_pool):
            return
        tid = self._tex_pool[frame_num]
        self.makeCurrent()
        glBindTexture(GL_TEXTURE_2D, tid)
        if self._cache_is_hdr:
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0,
                            self._cache_w, self._cache_h,
                            GL_RGB, GL_UNSIGNED_SHORT, raw)
        else:
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0,
                            self._cache_w, self._cache_h,
                            GL_RGB, GL_UNSIGNED_BYTE, raw)
        glBindTexture(GL_TEXTURE_2D, 0)
        self._tex_cache[frame_num] = tid

    def get_cached_tex(self, frame_num: int):
        """Return cached GL texture ID for *frame_num*, or None."""
        return self._tex_cache.get(frame_num)

    def clear_gpu_cache(self):
        """Release all pooled GPU textures."""
        if not self._tex_pool:
            return
        self.makeCurrent()
        glDeleteTextures(len(self._tex_pool), self._tex_pool)
        self._tex_pool    = []
        self._tex_cache   = {}
        self._draw_tex_id = None   # don't reference deleted textures

    # ── OCIO API ─────────────────────────────────────────────────────── #

    def set_ocio(self, func_src: str, lut_list: list):
        """
        Install an OCIO colour transform.
        *lut_list* items: (sampler_name, width, height, data_bytes, is_3d)
        """
        self.disable_ocio()
        self.makeCurrent()

        lut_info = []
        for i, (sampler, w, h, data, is_3d) in enumerate(lut_list):
            tid = int(glGenTextures(1))
            if is_3d:
                glBindTexture(GL_TEXTURE_3D, tid)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE)
                glTexImage3D(GL_TEXTURE_3D, 0, GL_RGBA32F, w, h, h, 0,
                             GL_RGBA, GL_FLOAT, data)
                glBindTexture(GL_TEXTURE_3D, 0)
            else:
                glBindTexture(GL_TEXTURE_2D, tid)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA32F, w, h, 0,
                             GL_RGBA, GL_FLOAT, data)
                glBindTexture(GL_TEXTURE_2D, 0)
            lut_info.append((sampler, tid, is_3d))

        self._ocio_lut_info = lut_info
        self._ocio_func_src = func_src
        self._ocio_enabled  = True
        self._rebuild_shader()

    def disable_ocio(self):
        if not self._ocio_enabled and not self._ocio_lut_info:
            return
        self.makeCurrent()
        for _sampler, tid, _is_3d in self._ocio_lut_info:
            glDeleteTextures(1, [tid])
        self._ocio_lut_info = []
        self._ocio_func_src = ""
        self._ocio_enabled  = False
        self._rebuild_shader()

    # ── OpenGL callbacks ─────────────────────────────────────────────── #

    def initializeGL(self):
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()

        self._vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._vbo.create()
        self._vbo.setUsagePattern(QOpenGLBuffer.DynamicDraw)

        self._tex_id = int(glGenTextures(1))
        self._alloc_texture(self._tex_id, 0, 0, False)

        self._rebuild_shader()
        glClearColor(0, 0, 0, 1)

    def resizeGL(self, w: int, h: int):
        glViewport(0, 0, w, h)

    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT)

        have_cached = self._draw_tex_id is not None
        if not have_cached and (not self._frame_raw or not self._vid_w):
            return
        if self._prog is None:
            return

        # ── Determine which texture to render ────────────────────────── #
        if have_cached:
            render_tex = self._draw_tex_id
            # Keep _draw_tex_id set — subsequent repaints (e.g. scrubber update)
            # must re-render the same cached frame, not fall back to the stale
            # scratch texture (_tex_id) which may hold a frame from a different clip.
        else:
            render_tex = self._tex_id
            if self._dirty:
                w, h = self._vid_w, self._vid_h
                glBindTexture(GL_TEXTURE_2D, render_tex)
                if self._is_hdr:
                    int_fmt, gl_type = GL_RGB16, GL_UNSIGNED_SHORT
                else:
                    int_fmt, gl_type = GL_RGB8, GL_UNSIGNED_BYTE
                if w != self._tex_w or h != self._tex_h or self._is_hdr != getattr(self, '_tex_hdr', False):
                    glTexImage2D(GL_TEXTURE_2D, 0, int_fmt, w, h, 0,
                                 GL_RGB, gl_type, self._frame_raw)
                    self._tex_w   = w
                    self._tex_h   = h
                    self._tex_hdr = self._is_hdr
                else:
                    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h,
                                    GL_RGB, gl_type, self._frame_raw)
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

        # Frame texture → unit 0
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, render_tex)
        self._prog.setUniformValue("u_frame", 0)

        # OCIO LUT textures → units 1, 2, …
        if self._ocio_enabled:
            for i, (sampler, tid, is_3d) in enumerate(self._ocio_lut_info):
                unit = GL_TEXTURE1 + i
                glActiveTexture(unit)
                target = GL_TEXTURE_3D if is_3d else GL_TEXTURE_2D
                glBindTexture(target, tid)
                loc = self._prog.uniformLocation(sampler)
                if loc >= 0:
                    glUniform1i(loc, i + 1)

        stride = 4 * 4          # 4 floats × 4 bytes
        self._prog.enableAttributeArray(0)
        self._prog.enableAttributeArray(1)
        self._prog.setAttributeBuffer(0, GL_FLOAT, 0,     2, stride)
        self._prog.setAttributeBuffer(1, GL_FLOAT, 2 * 4, 2, stride)

        glDrawArrays(GL_TRIANGLES, 0, 6)

        self._prog.disableAttributeArray(0)
        self._prog.disableAttributeArray(1)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, 0)
        self._prog.release()
        self._vbo.release()
        self._vao.release()

    # ── Shader compilation ────────────────────────────────────────────── #

    def _rebuild_shader(self):
        """Compile/relink the shader program with optional OCIO injection."""
        if self._ocio_enabled and self._ocio_func_src:
            frag = self._FRAG_BASE.replace(
                "// OCIO_INJECT", self._ocio_func_src
            ).replace(
                "// OCIO_CALL", "    c = OCIODisplay(c);"
            )
            # Add LUT sampler uniforms after the injection point
            extra_uniforms = "\n".join(
                f"uniform {'sampler3D' if is_3d else 'sampler2D'} {sampler};"
                for sampler, _tid, is_3d in self._ocio_lut_info
            )
            frag = frag.replace("// OCIO_INJECT", extra_uniforms + "\n// OCIO_INJECT", 1)
        else:
            frag = self._FRAG_BASE.replace(
                "// OCIO_INJECT", ""
            ).replace(
                "// OCIO_CALL", ""
            )

        prog = QOpenGLShaderProgram(self)
        prog.addShaderFromSourceCode(QOpenGLShader.Vertex,   self._VERT_SRC)
        prog.addShaderFromSourceCode(QOpenGLShader.Fragment, frag)
        prog.bindAttributeLocation("a_pos", 0)
        prog.bindAttributeLocation("a_tex", 1)
        if not prog.link():
            print(f"[BlastPlayer] shader link error: {prog.log()}")
            return
        self._prog = prog

    # ── Texture helpers ───────────────────────────────────────────────── #

    @staticmethod
    def _alloc_texture(tid: int, w: int, h: int, is_hdr: bool):
        glBindTexture(GL_TEXTURE_2D, tid)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        if w > 0 and h > 0:
            if is_hdr:
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB16, w, h, 0,
                             GL_RGB, GL_UNSIGNED_SHORT, None)
            else:
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB8, w, h, 0,
                             GL_RGB, GL_UNSIGNED_BYTE, None)
        glBindTexture(GL_TEXTURE_2D, 0)

    # ── Quad geometry ────────────────────────────────────────────────── #

    def _quad_vertices(self):
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
            y0, y1 = -sy, sy
            u0, u1, v0, v1 = 0.0, 1.0, 0.0, 1.0
        else:
            x0, x1, y0, y1 = -1.0, 1.0, -1.0, 1.0
            fit = (vw / self._vid_w) if vid_ar >= view_ar else (vh / self._vid_h)
            total  = fit * self._zoom
            disp_w = self._vid_w * total
            disp_h = self._vid_h * total
            px = max(0.0, min(float(self._pan_x), max(0.0, disp_w - vw)))
            py = max(0.0, min(float(self._pan_y), max(0.0, disp_h - vh)))
            u0 = px / disp_w;  u1 = min(1.0, (px + vw) / disp_w)
            v0 = py / disp_h;  v1 = min(1.0, (py + vh) / disp_h)

        return [
            x0, y1, u0, v0,
            x1, y1, u1, v0,
            x1, y0, u1, v1,
            x0, y1, u0, v0,
            x1, y0, u1, v1,
            x0, y0, u0, v1,
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

    _SPEEDS             = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
    _SPEED_LABELS       = ("0.25×", "0.5×", "0.75×", "1×", "1.25×", "1.5×", "1.75×", "2×")
    _CACHE_MAX_MB       = 2048   # skip CPU RAM cache if decoded frames exceed this
    _PREFETCH_QUEUE_SIZE = 16    # frames buffered ahead in the pipe reader thread

    video_ended   = pyqtSignal()        # emitted when video reaches end without looping
    audio_loading = pyqtSignal(bool)   # True = PCM decode started, False = finished

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

        # File / probe info
        self._path            = ""
        self._fps             = 24.0
        self._total_frames    = 0
        self._vid_w           = 0
        self._vid_h           = 0        # native dimensions (pre-transform)

        # HDR / pixel format
        self._is_hdr          = False    # True for EXR / DPX
        self._pix_fmt         = "rgb24"  # ffmpeg -pix_fmt value
        self._bytes_per_pixel = 3        # 3 for rgb24, 6 for rgb48le

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
        self._frame_queue   = queue.Queue(maxsize=self._PREFETCH_QUEUE_SIZE)
        self._reader_thread = None
        self._reader_stop   = threading.Event()

        # Scrub-ahead pipe — pre-warmed while user drags scrubber
        self._scrub_pipe_proc   = None
        self._scrub_pipe_queue  = None
        self._scrub_pipe_thread = None
        self._scrub_pipe_stop   = threading.Event()
        self._scrub_pipe_frame  = -1   # target frame the scrub pipe is primed for

        # Pre-warmed loop pipe — opened N frames before EOF for seamless looping
        self._loop_pipe_proc   = None
        self._loop_pipe_queue  = None
        self._loop_pipe_thread = None
        self._loop_reader_stop = threading.Event()

        # RAM frame cache — all frames decoded into memory for zero-latency playback
        self._frame_cache   = None   # list[bytes] once ready, None while not cached
        self._cache_loading = False  # True while background decode is running
        # Per-clip cache for multi-clip mode (keyed by path)
        self._mc_caches: dict = {}   # path → list[bytes]

        # GPU texture cache — frames uploaded to VideoCanvas texture pool
        self._gpu_cache_ready   = False
        self._gpu_upload_idx    = 0
        self._gpu_build_gen     = 0   # incremented on each new build; cancels stale batches

        # Reverse frame cache
        self._reverse_cache   = []      # list of (frame_num, raw_bytes), pop() = backward
        self._cache_building  = False   # True while background thread is filling cache

        # Autoplay
        self._autoplay = True

        # Multi-clip timeline
        self._mc_clips           = []   # list of {path, fps, total_frames, width, height, offset}
        self._mc_idx             = -1   # -1 = single-clip mode
        self._mc_next_proc   = None           # pre-warmed pipe for the next clip
        self._mc_next_queue  = None
        self._mc_next_thread = None
        self._mc_next_stop   = threading.Event()

        # In / out points
        self._in_frame  = None   # int | None
        self._out_frame = None   # int | None

        # Audio
        self._volume          = 100
        self._pre_mute_volume = 100
        self._audio = AudioEngine(self)
        self._audio.set_volume(self._volume)
        self._audio.load_finished.connect(
            lambda *_: self.audio_loading.emit(False))

        # Scrub-ahead seek debounce — pre-warms ffmpeg pipe at drag target
        self._scrub_seek_debounce = QTimer(self)
        self._scrub_seek_debounce.setSingleShot(True)
        self._scrub_seek_debounce.timeout.connect(
            lambda: self._prime_scrub_pipe(self._scrubber.value()))

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

        self._is_hdr          = self._detect_hdr(path)
        self._pix_fmt         = "rgb48le" if self._is_hdr else "rgb24"
        self._bytes_per_pixel = 6        if self._is_hdr else 3

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
        self.audio_loading.emit(True)
        self._audio.load(path, self._fps)
        self._start_cache_build()
        if self._autoplay:
            self._play()
        return True

    def stop(self):
        self._cleanup()

    # ------------------------------------------------------------------ #
    #  EXR / HDR helpers                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_hdr(path: str) -> bool:
        return Path(path).suffix.lower() in constants.EXR_EXTS

    @staticmethod
    def _exr_seq_pattern(path: str) -> str:
        """Convert frame_0001.exr → frame_%04d.exr for ffmpeg -i."""
        m = re.search(r'(\d+)(\.[^.]+)$', path)
        if m:
            return path[:m.start(1)] + f"%0{len(m.group(1))}d" + m.group(2)
        return path

    def _frame_nbytes(self) -> int:
        w, h = self._effective_size()
        return w * h * self._bytes_per_pixel

    # ------------------------------------------------------------------ #
    #  In / Out points                                                     #
    # ------------------------------------------------------------------ #

    def _effective_in(self) -> int:
        """Local frame at the in-point for the currently active clip."""
        if self._in_frame is None:
            return 0
        if self._mc_idx >= 0 and self._mc_clips:
            return max(0, self._in_frame - self._mc_offset())
        return self._in_frame

    def _effective_out(self) -> int:
        """Local frame at the out-point for the currently active clip."""
        if self._out_frame is None:
            return max(0, self._total_frames - 1)
        if self._mc_idx >= 0 and self._mc_clips:
            local = self._out_frame - self._mc_offset()
            if local < 0:
                return 0                          # out-point is before this clip
            if local >= self._total_frames:
                return max(0, self._total_frames - 1)   # out-point is after this clip
            return local
        return self._out_frame

    def set_in_frame(self):
        if not self._path:
            return
        f = self._mc_offset() + self._current_frame   # always global
        if self._in_frame is not None and f == self._in_frame:
            self._in_frame = None
        elif self._out_frame is not None and f >= self._out_frame:
            return  # invalid: in must be before out
        else:
            self._in_frame = f
        self._scrubber.set_in_out(self._in_frame, self._out_frame)

    def set_out_frame(self):
        if not self._path:
            return
        f = self._mc_offset() + self._current_frame   # always global
        if self._out_frame is not None and f == self._out_frame:
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
    #  Multi-clip timeline                                                 #
    # ------------------------------------------------------------------ #

    def _mc_offset(self) -> int:
        """Global frame offset of the currently active clip."""
        if self._mc_idx >= 0 and self._mc_clips:
            return self._mc_clips[self._mc_idx]['offset']
        return 0

    def _global_to_local(self, global_frame: int):
        """Return (local_frame, clip_index) for a global scrubber position."""
        for i, clip in enumerate(self._mc_clips):
            if global_frame < clip['offset'] + clip['total_frames']:
                return global_frame - clip['offset'], i
        last = self._mc_clips[-1]
        return last['total_frames'] - 1, len(self._mc_clips) - 1

    def load_multi_clips(self, paths: list):
        """Probe all paths (strings or {path, loop_this} dicts), build mc timeline."""
        clips  = []
        offset = 0
        for entry in paths:
            # Accept both plain strings and dicts from PlaylistSidebar
            if isinstance(entry, dict):
                path      = entry["path"]
                loop_this = entry.get("loop_this", False)
            else:
                path      = entry
                loop_this = False
            try:
                info = probe_video(path)
            except RuntimeError:
                continue
            clips.append({
                'path':         path,
                'fps':          info['fps'],
                'total_frames': info['total_frames'],
                'width':        info['width'],
                'height':       info['height'],
                'offset':       offset,
                'loop_this':    loop_this,
            })
            offset += info['total_frames']
        if not clips:
            return

        # load_video clears _mc_clips/_mc_idx via _cleanup — set them back after
        if not self.load_video(clips[0]['path']):
            return
        self._mc_clips = clips
        self._mc_idx   = 0
        total = offset
        self._scrubber.blockSignals(True)
        self._scrubber.setRange(0, max(total - 1, 0))
        self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)
        self._scrubber.set_mc_clips(clips)
        self._frames_lbl.setText(f"{total} frames")
        # Reload audio engine with the full multi-clip sequence
        self.audio_loading.emit(True)
        self._audio.load(clips[0]['path'], clips[0]['fps'], mc_clips=clips)
        # Build frame caches for all clips except clip 0 (clip 0 is handled by
        # _start_cache_build which load_video already triggered above)
        if len(clips) > 1:
            self._start_mc_cache_builds(clips[1:])
        # Pre-warm clip 1's video pipe while clip 0 plays
        if len(clips) > 1:
            self._open_mc_next_pipe(clips[1])

    def _open_mc_next_pipe(self, clip: dict):
        """Start decoding the next clip's frames in the background so adoption is instant."""
        self._close_mc_next_pipe()
        w, h     = clip['width'], clip['height']
        is_hdr   = self._detect_hdr(clip['path'])
        pix_fmt  = "rgb48le" if is_hdr else "rgb24"
        bpp      = 6 if is_hdr else 3
        nbytes   = w * h * bpp
        src      = self._exr_seq_pattern(clip['path']) if is_hdr else clip['path']
        cmd = [
            _ffmpeg_exe(),
            "-i",       src,
            "-f",       "rawvideo",
            "-pix_fmt", pix_fmt,
            "-vf",      "null",
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._mc_next_proc  = proc
            self._mc_next_stop.clear()
            self._mc_next_queue = queue.Queue(maxsize=self._PREFETCH_QUEUE_SIZE)
            q    = self._mc_next_queue
            stop = self._mc_next_stop
            def _read():
                while not stop.is_set():
                    try:
                        raw = proc.stdout.read(nbytes)
                    except Exception:
                        break
                    if len(raw) < nbytes:
                        try: q.put(None, timeout=1.0)
                        except queue.Full: pass
                        break
                    q.put(bytes(raw))
            self._mc_next_thread = threading.Thread(target=_read, daemon=True)
            self._mc_next_thread.start()
        except Exception as exc:
            print(f"[BlastPlayer] mc_next_pipe: {exc}")
            self._mc_next_proc = None

    def _close_mc_next_pipe(self):
        self._mc_next_stop.set()
        self._mc_next_thread = None
        if self._mc_next_proc is not None:
            try:
                self._mc_next_proc.stdout.close()
                self._mc_next_proc.terminate()
            except Exception:
                pass
            self._mc_next_proc = None
        if self._mc_next_queue is not None:
            while True:
                try: self._mc_next_queue.get_nowait()
                except queue.Empty: break
            self._mc_next_queue = None
        self._mc_next_stop.clear()

    def _advance_to_clip(self, clip: dict):
        """
        Transition to the next clip.
        If a pre-warmed pipe exists for this clip, adopts it instantly (zero ffmpeg
        startup gap).  Otherwise falls back to opening a fresh pipe.
        """
        self._timer.stop()
        self._close_pipe()          # stops old reader thread, drains old queue
        self._close_loop_pipe()
        self._close_lookahead()
        # Audio is NOT stopped here — the concat stream plays continuously across clips

        self._path             = clip['path']
        self._fps              = clip['fps']
        self._total_frames     = clip['total_frames']
        self._vid_w            = clip['width']
        self._vid_h            = clip['height']
        self._current_frame    = 0
        self._pipe_frame       = 0
        self._is_playing       = True
        self._play_reverse     = False
        self._reverse_cache    = []
        self._cache_building   = False
        self._loop_frame0      = None
        self._frame_cache      = self._mc_caches.get(clip['path'])  # instant if cached
        self._cache_loading    = False
        self._gpu_cache_ready  = False
        self._gpu_upload_idx   = 0
        self._gpu_build_gen   += 1   # cancel any in-flight upload batch for the old clip
        self._is_hdr           = self._detect_hdr(clip['path'])
        self._pix_fmt          = "rgb48le" if self._is_hdr else "rgb24"
        self._bytes_per_pixel  = 6         if self._is_hdr else 3
        self._play_frame_start = 0
        self._play_clock_start = time.monotonic()
        self._play_btn.setIcon(_icon("pause.png"))
        self._play_btn.setText("")

        self._scrubber.blockSignals(True)
        if self._mc_clips:
            # Keep the full multi-clip range; just jump to this clip's offset
            mc_total = sum(c['total_frames'] for c in self._mc_clips)
            self._scrubber.setRange(0, max(mc_total - 1, 0))
            self._scrubber.setValue(clip.get('offset', 0))
        else:
            self._scrubber.setRange(0, max(self._total_frames - 1, 0))
            self._scrubber.setValue(0)
        self._scrubber.blockSignals(False)

        if self._mc_next_proc is not None:
            # Adopt the pre-warmed pipe — frames already buffered, no startup wait
            self._pipe_proc     = self._mc_next_proc
            self._frame_queue   = self._mc_next_queue
            self._reader_thread = self._mc_next_thread
            self._reader_stop   = self._mc_next_stop
            self._mc_next_proc   = None
            self._mc_next_queue  = None
            self._mc_next_thread = None
            self._mc_next_stop   = threading.Event()
            # Paint frame 0 immediately — closes the visual gap at the cut point.
            # Set play_frame_start=1 so the next tick targets frame 1, not frame 0.
            # Without this, floor(elapsed * fps) ≈ 0.98 truncates to 0, pipe_frame=1
            # wins the comparison and nothing renders for a full extra interval.
            try:
                first_raw = self._frame_queue.get_nowait()
                if first_raw is not None:
                    self._render_raw(first_raw)
                    self._pipe_frame       = 1
                    self._play_frame_start = 1   # frame 0 already consumed
                    self._play_clock_start = time.monotonic()
            except queue.Empty:
                pass  # pre-warm not ready; normal tick loop catches frame 0
        else:
            self._open_pipe(0)

        if self._frame_cache is None:    # not yet in _mc_caches — start building
            self._start_cache_build()
        self._timer.start(max(1, int(1000 / (self._fps * self._speed))))

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
        Returns raw bytes (RGB24 or rgb48le) or None on failure.
        """
        if not self._path or not self._vid_w:
            return None
        seek = frame_num / self._fps
        w, h = self._effective_size()
        src  = self._exr_seq_pattern(self._path) if self._is_hdr else self._path
        cmd = [
            _ffmpeg_exe(),
            "-i",        src,
            "-ss",       f"{seek:.6f}",
            "-frames:v", "1",
            "-f",        "rawvideo",
            "-pix_fmt",  self._pix_fmt,
            "-vf",       self._build_vf(),
            "pipe:1",
        ]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            raw      = proc.stdout
            expected = w * h * self._bytes_per_pixel
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

        src = self._exr_seq_pattern(self._path) if self._is_hdr else self._path
        cmd = [_ffmpeg_exe()]
        if pre_ts > 0:
            cmd += ["-ss", f"{pre_ts:.6f}"]
        cmd += [
            "-i",       src,
            "-ss",      f"{fine_ts:.6f}",
            "-f",       "rawvideo",
            "-pix_fmt", self._pix_fmt,
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
        frame_size = self._frame_nbytes()
        total_mb   = (self._total_frames * frame_size) / 1_048_576
        if total_mb > self._CACHE_MAX_MB:
            return

        self._frame_cache   = None
        self._cache_loading = True
        path    = self._path
        pix_fmt = self._pix_fmt
        src     = self._exr_seq_pattern(path) if self._is_hdr else path
        vf      = self._build_vf()
        nf      = self._total_frames
        nbytes  = frame_size

        def _fill():
            cmd = [
                _ffmpeg_exe(),
                "-i",       src,
                "-f",       "rawvideo",
                "-pix_fmt", pix_fmt,
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
                self._mc_caches[path] = frames   # available for cross-clip scrubs
            self._cache_loading = False
            QTimer.singleShot(0, self._start_gpu_cache_build)

        threading.Thread(target=_fill, daemon=True).start()

    def _start_mc_cache_builds(self, clips: list) -> None:
        """Pre-build frame caches for all mc clips (except the active one).

        Each clip gets its own background thread. Results land in
        _mc_caches[path] so cross-clip scrubbing is served from memory.
        """
        vf = self._build_vf()
        for clip in clips:
            path = clip['path']
            if path in self._mc_caches:
                continue
            is_hdr     = self._detect_hdr(path)
            pix_fmt    = "rgb48le" if is_hdr else "rgb24"
            bpp        = 6 if is_hdr else 3
            rot        = self._rotation
            fw = clip['height'] if rot in (90, 270) else clip['width']
            fh = clip['width']  if rot in (90, 270) else clip['height']
            frame_size = fw * fh * bpp
            total_mb   = (clip['total_frames'] * frame_size) / 1_048_576
            if total_mb > self._CACHE_MAX_MB:
                continue  # too large — skip, fall back to _fetch_frame
            src = self._exr_seq_pattern(path) if is_hdr else path
            nf  = clip['total_frames']

            def _fill(p=path, s=src, pf=pix_fmt, nb=frame_size, n=nf, v=vf):
                cmd = [_ffmpeg_exe(), "-i", s,
                       "-f", "rawvideo", "-pix_fmt", pf,
                       "-vf", v, "pipe:1"]
                frames = []
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    for _ in range(n):
                        chunk = proc.stdout.read(nb)
                        if len(chunk) < nb:
                            break
                        frames.append(bytes(chunk))
                    try:
                        proc.stdout.close()
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                except Exception as exc:
                    print(f"[BlastPlayer] mc cache ({p}): {exc}")
                if frames:
                    self._mc_caches[p] = frames

            threading.Thread(target=_fill, daemon=True).start()

    def _open_loop_pipe(self):
        """Pre-warm a pipe from frame 0 so the loop swap is instantaneous."""
        if self._loop_pipe_proc is not None or not self._path or not self._vid_w:
            return
        self._loop_reader_stop.clear()
        self._loop_pipe_queue = queue.Queue(maxsize=self._PREFETCH_QUEUE_SIZE)
        src = self._exr_seq_pattern(self._path) if self._is_hdr else self._path
        cmd = [
            _ffmpeg_exe(),
            "-i",       src,
            "-f",       "rawvideo",
            "-pix_fmt", self._pix_fmt,
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
        self._frame_queue = queue.Queue(maxsize=self._PREFETCH_QUEUE_SIZE)
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
        nbytes = self._frame_nbytes()
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
        nbytes      = self._frame_nbytes()
        n_frames    = up_to_frame - start_frame + 1
        path        = self._path
        pix_fmt     = self._pix_fmt
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
                "-pix_fmt",  pix_fmt,
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
    #  Scrub-ahead pipe                                                    #
    # ------------------------------------------------------------------ #

    def _prime_scrub_pipe(self, frame_num: int):
        """Open a background ffmpeg pipe at *frame_num* for instant scrub-release."""
        if not self._path or not self._vid_w:
            return
        self._close_scrub_pipe()
        nbytes = self._frame_nbytes()
        src    = self._exr_seq_pattern(self._path) if self._is_hdr else self._path

        pre_offset_frames = min(frame_num, int(self._fps * 4))
        pre_frame = frame_num - pre_offset_frames
        pre_ts    = pre_frame / self._fps
        fine_ts   = pre_offset_frames / self._fps

        cmd = [_ffmpeg_exe()]
        if pre_ts > 0:
            cmd += ["-ss", f"{pre_ts:.6f}"]
        cmd += [
            "-i",       src,
            "-ss",      f"{fine_ts:.6f}",
            "-f",       "rawvideo",
            "-pix_fmt", self._pix_fmt,
            "-vf",      self._build_vf(),
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            self._scrub_pipe_proc   = proc
            self._scrub_pipe_frame  = frame_num
            self._scrub_pipe_stop.clear()
            self._scrub_pipe_queue  = queue.Queue(maxsize=8)
            q    = self._scrub_pipe_queue
            stop = self._scrub_pipe_stop

            def _read():
                while not stop.is_set():
                    try:
                        raw = proc.stdout.read(nbytes)
                    except Exception:
                        break
                    if len(raw) < nbytes:
                        try: q.put(None, timeout=1.0)
                        except queue.Full: pass
                        break
                    try:
                        q.put(bytes(raw))
                    except Exception:
                        break

            self._scrub_pipe_thread = threading.Thread(target=_read, daemon=True)
            self._scrub_pipe_thread.start()
        except Exception as exc:
            print(f"[BlastPlayer] scrub pipe: {exc}")
            self._scrub_pipe_proc = None

    def _close_scrub_pipe(self):
        self._scrub_pipe_stop.set()
        self._scrub_pipe_thread = None
        if self._scrub_pipe_proc is not None:
            try:
                self._scrub_pipe_proc.stdout.close()
                self._scrub_pipe_proc.terminate()
            except Exception:
                pass
            self._scrub_pipe_proc = None
        if self._scrub_pipe_queue is not None:
            while True:
                try: self._scrub_pipe_queue.get_nowait()
                except queue.Empty: break
            self._scrub_pipe_queue = None
        self._scrub_pipe_stop.clear()
        self._scrub_pipe_frame = -1

    # ------------------------------------------------------------------ #
    #  GPU Texture Cache                                                   #
    # ------------------------------------------------------------------ #

    def _start_gpu_cache_build(self):
        """Upload CPU frame cache to GPU textures in batches (main thread only)."""
        if not self._frame_cache or not self._vid_w:
            return
        w, h = self._effective_size()
        bpp  = self._bytes_per_pixel
        max_frames = min(
            len(self._frame_cache),
            (constants.GPU_CACHE_MAX_MB * 1_048_576) // max(1, w * h * bpp),
        )
        if max_frames <= 0:
            return
        self._gpu_cache_ready = False
        self._gpu_upload_idx  = 0
        self._gpu_build_gen  += 1
        self._canvas.begin_gpu_cache(max_frames, w, h, self._is_hdr)
        gen = self._gpu_build_gen
        QTimer.singleShot(0, lambda: self._gpu_upload_batch(gen))

    def _gpu_upload_batch(self, gen: int):
        if gen != self._gpu_build_gen:
            return   # a newer build started — this batch is stale, discard
        if not self._frame_cache:
            return
        pool_size = len(self._canvas._tex_pool)
        for _ in range(8):
            idx = self._gpu_upload_idx
            if idx >= len(self._frame_cache) or idx >= pool_size:
                self._gpu_cache_ready = True
                return
            self._canvas.upload_gpu_frame(idx, self._frame_cache[idx])
            self._gpu_upload_idx += 1
        QTimer.singleShot(0, lambda: self._gpu_upload_batch(gen))

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Content row: playlist sidebar + video canvas ──────────────────
        self._content_splitter = QSplitter(Qt.Horizontal)
        self._content_splitter.setHandleWidth(2)
        self._content_splitter.setStyleSheet("""
            QSplitter::handle:horizontal { background: #2a2a2a; }
            QSplitter::handle:horizontal:hover { background: #444; }
        """)

        self._playlist_sidebar = PlaylistSidebar()
        self._playlist_sidebar.setVisible(False)
        self._playlist_sidebar.video_selected.connect(self._on_playlist_select)
        self._playlist_sidebar.selection_play_requested.connect(self.load_multi_clips)
        self._content_splitter.addWidget(self._playlist_sidebar)

        self._canvas = VideoCanvas()
        self._canvas.zoom_scrolled.connect(self._on_zoom_scroll)
        self._canvas.pan_dragged.connect(self._on_pan_drag)
        self._content_splitter.addWidget(self._canvas)

        self._content_splitter.setSizes([240, 10000])
        self._content_splitter.setCollapsible(0, False)
        self._content_splitter.setCollapsible(1, False)

        root.addWidget(self._content_splitter, stretch=1)

        # ── Timeline strip ────────────────────────────────────────────────
        timeline = QWidget()
        timeline.setFixedHeight(64)
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
        self._scrubber.setStyleSheet(self._playback_scrubber_style())
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

        # Playlist toggle — far left
        self._playlist_btn = QPushButton()
        ic = _icon("list.png")
        if not ic.isNull():
            self._playlist_btn.setIcon(ic)
            self._playlist_btn.setIconSize(QSize(14, 14))
        else:
            self._playlist_btn.setText("≡")
        self._playlist_btn.setCheckable(True)
        self._playlist_btn.setFixedSize(28, 28)
        self._playlist_btn.setFocusPolicy(Qt.NoFocus)
        self._playlist_btn.setToolTip("Show / Hide Playlist")
        self._playlist_btn.setStyleSheet(self._loop_style(False))
        self._playlist_btn.toggled.connect(self._toggle_playlist)
        tr.addWidget(self._playlist_btn)
        tr.addSpacing(6)

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
        if self._mc_idx >= 0 and self._mc_clips:
            global_pos = self._mc_offset() + self._current_frame
            if not reverse and self._out_frame is not None and global_pos >= self._out_frame:
                self._jump_to_global(self._in_frame if self._in_frame is not None else 0)
            if reverse and self._in_frame is not None and global_pos <= self._in_frame:
                self._jump_to_global(self._out_frame if self._out_frame is not None
                                     else sum(c['total_frames'] for c in self._mc_clips) - 1)
        else:
            if not reverse and self._current_frame >= self._effective_out():
                self._seek_no_render(self._effective_in())
            if reverse and self._current_frame <= self._effective_in():
                self._seek_no_render(self._effective_out())

        self._is_playing   = True
        self._play_reverse = reverse
        self._play_btn.setIcon(_icon("pause.png"))
        self._play_btn.setText("")

        self._audio.set_speed(self._speed)

        if reverse:
            self._reverse_cache = []
        elif self._frame_cache is not None:
            self._pipe_frame = self._current_frame
        else:
            self._open_pipe(self._current_frame)

        self._play_frame_start = self._current_frame
        self._play_clock_start = time.monotonic()
        if self._mc_idx >= 0 and self._mc_clips:
            global_frame = self._mc_offset() + self._current_frame
            sample = self._audio.global_frame_to_sample(global_frame, self._mc_clips)
            if reverse:
                self._audio._play_pos = min(sample + 1, self._audio.duration_samples())
                self._audio._mode = AudioEngine._PLAYING_REVERSE
            else:
                self._audio._play_pos = sample
                self._audio._mode = AudioEngine._PLAYING
        elif reverse:
            self._audio.play_reverse(self._current_frame)
        else:
            self._audio.play(self._current_frame)

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
        self._audio.stop()
        self._show_frame(self._current_frame)

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
                        self._audio.play_reverse(last)
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
                            self._audio.play_reverse(self._current_frame)
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
                    cache_out_reached = (
                        self._out_frame is not None
                        and out < len(self._frame_cache) - 1
                    )
                    if self._loop and self._mc_idx < 0:
                        # Single-clip loop
                        self._play_frame_start = in_f
                        self._play_clock_start = time.monotonic()
                        self._current_frame    = in_f
                        self._pipe_frame       = in_f + 1
                        self._render_raw(self._frame_cache[in_f])
                        self._audio.play(in_f)
                        self._scrubber.blockSignals(True)
                        self._scrubber.setValue(in_f)
                        self._scrubber.blockSignals(False)
                        self._update_info()
                    else:
                        self._end_of_video(out_reached=cache_out_reached)
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
                    if self._loop and self._mc_idx < 0:
                        if out_reached or in_f > 0:
                            # Range loop or non-zero in point: simple re-seek
                            self._open_pipe(in_f)
                            self._current_frame    = in_f
                            self._play_frame_start = in_f
                            self._play_clock_start = time.monotonic()
                            self._audio.play(in_f)
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
                            self._audio.play(0)
                            self._scrubber.blockSignals(True)
                            self._scrubber.setValue(0)
                            self._scrubber.blockSignals(False)
                            self._update_info()
                    else:
                        self._end_of_video(out_reached=out_reached)
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
            self._scrubber.setValue(self._mc_offset() + self._current_frame)
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

    def _jump_to_global(self, global_frame: int):
        """Switch to whichever clip owns global_frame and seek to its local position.
        Used to jump to in/out points that may live in a different clip."""
        if not (self._mc_idx >= 0 and self._mc_clips):
            self._seek_no_render(global_frame)
            return
        local, target_idx = self._global_to_local(global_frame)
        if target_idx != self._mc_idx:
            next_clip = self._mc_clips[target_idx]
            self._path            = next_clip['path']
            self._fps             = next_clip['fps']
            self._total_frames    = next_clip['total_frames']
            self._vid_w           = next_clip['width']
            self._vid_h           = next_clip['height']
            self._is_hdr          = self._detect_hdr(next_clip['path'])
            self._pix_fmt         = "rgb48le" if self._is_hdr else "rgb24"
            self._bytes_per_pixel = 6 if self._is_hdr else 3
            self._frame_cache     = self._mc_caches.get(next_clip['path'])
            self._cache_loading   = False
            self._gpu_cache_ready = False
            self._gpu_upload_idx  = 0
            self._gpu_build_gen  += 1
            self._reverse_cache   = []
            self._loop_frame0     = None
            self._mc_idx          = target_idx
            if self._frame_cache is None:
                self._start_cache_build()
        self._current_frame = local
        self._pipe_frame    = local

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
        w, h = self._effective_size()
        if not w or not h:
            return
        if self._gpu_cache_ready:
            tex = self._canvas.get_cached_tex(self._current_frame)
            if tex is not None:
                self._canvas.set_cached_frame(tex)
                return
        self._canvas.set_frame(raw, w, h, is_hdr=self._is_hdr)

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
        self._audio.stop()
        if self._is_playing:
            self._timer.stop()
            self._close_pipe()

    def _on_scrubber_released(self):
        self._scrubber_moving = False
        global_val = self._scrubber.value()

        if self._mc_idx >= 0 and self._mc_clips:
            local_frame, target_idx = self._global_to_local(global_val)
            was_playing = self._is_playing

            if target_idx != self._mc_idx:
                # ── Cross-clip seek: lightweight switch, no load_video ──────
                # load_video would reset the scrubber, trigger autoplay, and
                # re-decode audio — none of which we want here. The full mc
                # PCM buffer is already loaded; we only need to swap video state.
                next_clip = self._mc_clips[target_idx]
                self._timer.stop()
                self._close_pipe()
                self._close_loop_pipe()
                self._close_lookahead()
                self._close_scrub_pipe()

                self._path            = next_clip['path']
                self._fps             = next_clip['fps']
                self._total_frames    = next_clip['total_frames']
                self._vid_w           = next_clip['width']
                self._vid_h           = next_clip['height']
                self._is_hdr          = self._detect_hdr(next_clip['path'])
                self._pix_fmt         = "rgb48le" if self._is_hdr else "rgb24"
                self._bytes_per_pixel = 6 if self._is_hdr else 3
                # Adopt pre-built cache if available; otherwise start a build
                self._frame_cache     = self._mc_caches.get(next_clip['path'])
                self._cache_loading   = False
                self._gpu_cache_ready = False
                self._gpu_upload_idx  = 0
                self._gpu_build_gen  += 1
                self._reverse_cache   = []
                self._loop_frame0     = None
                self._mc_idx          = target_idx

                total = sum(c['total_frames'] for c in self._mc_clips)
                self._scrubber.blockSignals(True)
                self._scrubber.setRange(0, max(total - 1, 0))
                self._scrubber.setValue(global_val)
                self._scrubber.blockSignals(False)
                self._frames_lbl.setText(f"{total} frames")

                if not was_playing:
                    self._is_playing   = False
                    self._play_reverse = False
                    self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
                    self._play_btn.setText("")

                if self._frame_cache is None:
                    self._start_cache_build()

            # Seek to the local frame within the (now-active) clip
            self._current_frame = local_frame
            if self._is_playing:
                if self._play_reverse:
                    self._reverse_cache = []
                elif self._frame_cache is not None:
                    self._pipe_frame = local_frame
                else:
                    self._open_pipe(local_frame)
                self._play_frame_start = local_frame
                self._play_clock_start = time.monotonic()
                sample = self._audio.global_frame_to_sample(global_val, self._mc_clips)
                if self._play_reverse:
                    self._audio._play_pos = min(sample + 1, self._audio.duration_samples())
                    self._audio._mode = AudioEngine._PLAYING_REVERSE
                else:
                    self._audio._play_pos = sample
                    self._audio._mode = AudioEngine._PLAYING
                self._timer.start(max(1, int(1000 / (self._fps * self._speed))))
            else:
                self._show_frame(local_frame)
                self._scrubber.blockSignals(True)
                self._scrubber.setValue(global_val)
                self._scrubber.blockSignals(False)
                self._update_info()
            return

        # Single-clip mode
        self._scrub_seek_debounce.stop()
        self._current_frame = global_val
        if self._is_playing:
            if self._play_reverse:
                self._reverse_cache = []
                self._close_scrub_pipe()
            elif self._frame_cache is not None:
                self._pipe_frame = self._current_frame
                self._close_scrub_pipe()
            elif (self._scrub_pipe_proc is not None
                    and self._scrub_pipe_frame == global_val):
                # Adopt the pre-warmed scrub pipe — no ffmpeg startup wait
                self._close_pipe()
                self._pipe_proc     = self._scrub_pipe_proc
                self._frame_queue   = self._scrub_pipe_queue
                self._reader_thread = self._scrub_pipe_thread
                self._reader_stop   = self._scrub_pipe_stop
                self._pipe_frame    = global_val
                self._scrub_pipe_proc   = None
                self._scrub_pipe_queue  = None
                self._scrub_pipe_thread = None
                self._scrub_pipe_stop   = threading.Event()
                self._scrub_pipe_frame  = -1
            else:
                self._close_scrub_pipe()
                self._open_pipe(self._current_frame)
            self._play_frame_start = self._current_frame
            self._play_clock_start = time.monotonic()
            if self._play_reverse:
                self._audio.play_reverse(self._current_frame)
            else:
                self._audio.play(self._current_frame)
            self._timer.start(max(1, int(1000 / (self._fps * self._speed))))
        else:
            self._close_scrub_pipe()
            self._show_frame(self._current_frame)
            self._update_info()

    def _on_scrubber_moved(self, value: int):
        if not self._path:
            return
        if self._mc_idx >= 0 and self._mc_clips:
            local_frame, clip_idx = self._global_to_local(value)
            if clip_idx == self._mc_idx:
                # Same clip — use frame cache or fetch normally
                if self._frame_cache and local_frame < len(self._frame_cache):
                    self._render_raw(self._frame_cache[local_frame])
                else:
                    raw = self._fetch_frame(local_frame)
                    if raw:
                        self._render_raw(raw)
                self._current_frame = local_frame
            else:
                # Cross-clip scrub — serve from pre-built cache when available
                # (instant); otherwise temporarily adopt the target clip's metadata
                # and fetch via ffmpeg (slow path until the background cache lands).
                tc      = self._mc_clips[clip_idx]
                tc_path = tc['path']
                tc_cache = self._mc_caches.get(tc_path)
                if tc_cache and local_frame < len(tc_cache):
                    # Fast path: cache ready — render directly, bypassing GPU cache
                    tc_hdr = self._detect_hdr(tc_path)
                    rot    = self._rotation
                    tc_w   = tc['height'] if rot in (90, 270) else tc['width']
                    tc_h   = tc['width']  if rot in (90, 270) else tc['height']
                    self._canvas.set_frame(tc_cache[local_frame],
                                           tc_w, tc_h, is_hdr=tc_hdr)
                else:
                    # Slow path: ffmpeg single-frame fetch (cache still building)
                    tc_hdr = self._detect_hdr(tc_path)
                    saved  = (self._path, self._fps,
                              self._vid_w, self._vid_h,
                              self._is_hdr, self._pix_fmt, self._bytes_per_pixel)
                    self._path, self._fps    = tc_path, tc['fps']
                    self._vid_w, self._vid_h = tc['width'], tc['height']
                    self._is_hdr             = tc_hdr
                    self._pix_fmt            = "rgb48le" if tc_hdr else "rgb24"
                    self._bytes_per_pixel    = 6 if tc_hdr else 3
                    raw = self._fetch_frame(local_frame)
                    tc_w, tc_h = self._effective_size()
                    (self._path, self._fps,
                     self._vid_w, self._vid_h,
                     self._is_hdr, self._pix_fmt, self._bytes_per_pixel) = saved
                    if raw:
                        self._canvas.set_frame(raw, tc_w, tc_h, is_hdr=tc_hdr)
            self._update_info()
            # _update_info uses _mc_idx/_current_frame (active clip); for a
            # cross-clip scrub those haven't changed, so patch the counter directly.
            if clip_idx != self._mc_idx:
                self._frame_num_lbl.setText(str(value + 1))
            self._audio.scrub(value, mc_clips=self._mc_clips)
            return
        # Single-clip mode
        if self._frame_cache and value < len(self._frame_cache):
            self._render_raw(self._frame_cache[value])
        else:
            raw = self._fetch_frame(value)
            if raw:
                self._render_raw(raw)
        self._current_frame = value
        self._update_info()
        self._audio.scrub(value)
        if not self._play_reverse and not self._frame_cache:
            self._scrub_seek_debounce.start(120)

    # ------------------------------------------------------------------ #
    #  Speed / loop                                                        #
    # ------------------------------------------------------------------ #

    def _on_speed_changed(self, index: int):
        self._speed = self._SPEEDS[index]
        self._audio.set_speed(self._speed)
        if self._is_playing:
            self._timer.setInterval(max(1, int(1000 / (self._fps * self._speed))))

    def _on_loop_toggled(self, checked: bool):
        self._loop = checked
        self._loop_btn.setStyleSheet(self._loop_style(checked))
        if not checked:
            self._close_lookahead()
            self._close_loop_pipe()

    # ------------------------------------------------------------------ #
    #  Playlist                                                            #
    # ------------------------------------------------------------------ #

    def _toggle_playlist(self, checked: bool):
        self._playlist_sidebar.setVisible(checked)
        self._playlist_btn.setStyleSheet(self._loop_style(checked))

    def _on_playlist_select(self, path: str):
        from pathlib import Path as _P
        if _P(path).is_file():
            self.load_video(path)
            self._playlist_sidebar.set_current(path)
            QTimer.singleShot(0, self._refresh_display)

    def _end_of_video(self, out_reached: bool = False):
        """Called when playback hits the out-point or the natural end of a clip."""
        if self._mc_idx >= 0 and self._mc_clips:
            # Out-point explicitly hit: loop back to in-point or stop entirely.
            if out_reached:
                if self._loop:
                    self._close_mc_next_pipe()   # stale pre-warm no longer valid
                    target = self._in_frame if self._in_frame is not None else 0
                    self._jump_to_global(target)
                    self._audio.seek_to_clip(self._mc_idx)
                    if self._frame_cache is None:
                        self._open_pipe(self._current_frame)
                    else:
                        self._pipe_frame = self._current_frame
                    self._play_frame_start = self._current_frame
                    self._play_clock_start = time.monotonic()
                    total = sum(c['total_frames'] for c in self._mc_clips)
                    self._scrubber.blockSignals(True)
                    self._scrubber.setRange(0, max(total - 1, 0))
                    self._scrubber.setValue(self._mc_offset() + self._current_frame)
                    self._scrubber.blockSignals(False)
                    self._update_info()
                else:
                    self._audio.stop()
                    self._pause()
                    self.video_ended.emit()
                return

            # "Loop this clip" flag: replay the current clip instead of advancing
            if self._mc_clips[self._mc_idx].get('loop_this', False):
                next_idx = self._mc_idx
            else:
                next_idx = self._mc_idx + 1

            # Wrap around when loop is on, stop when it isn't
            if next_idx >= len(self._mc_clips):
                if self._loop:
                    next_idx = 0
                else:
                    self._audio.stop()
                    self._mc_clips = []
                    self._mc_idx   = -1
                    self._pause()
                    self.video_ended.emit()
                    return
            clips_backup = self._mc_clips
            next_clip    = clips_backup[next_idx]
            self._advance_to_clip(next_clip)
            self._mc_clips = clips_backup
            self._mc_idx   = next_idx
            # Re-anchor audio to the start of the new clip every transition.
            # Without this the PortAudio callback drains the buffer and stops.
            self._audio.seek_to_clip(next_idx)
            total = sum(c['total_frames'] for c in clips_backup)
            self._scrubber.blockSignals(True)
            self._scrubber.setRange(0, max(total - 1, 0))
            self._scrubber.setValue(next_clip['offset'])
            self._scrubber.blockSignals(False)
            self._frames_lbl.setText(f"{total} frames")
            # Pre-warm the clip after this one
            prewarm_idx = next_idx + 1
            if prewarm_idx < len(clips_backup):
                self._open_mc_next_pipe(clips_backup[prewarm_idx])
            elif self._loop and len(clips_backup) > 1:
                self._open_mc_next_pipe(clips_backup[0])
            return
        self._pause()
        self.video_ended.emit()

    # ------------------------------------------------------------------ #
    #  Audio                                                               #
    # ------------------------------------------------------------------ #

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
        self._audio.set_volume(value)

    # ------------------------------------------------------------------ #
    #  Info update / frame callback                                        #
    # ------------------------------------------------------------------ #

    def _update_info(self):
        f          = self._current_frame
        mc_off     = self._mc_offset()
        global_f   = mc_off + f
        self._frame_num_lbl.setText(str(global_f + 1))
        if self._mc_idx >= 0 and self._mc_clips:
            total = sum(c['total_frames'] for c in self._mc_clips)
            self._frames_lbl.setText(f"{total} frames")
        else:
            self._frames_lbl.setText(f"{self._total_frames} frames")
        self._fps_lbl.setText(f"{self._fps:.2f} fps")
        if hasattr(self, '_on_frame_changed'):
            self._on_frame_changed(global_f + 1)

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
        self._close_scrub_pipe()
        self._audio.stop()
        self._path             = ""
        self._current_frame    = 0
        self._is_playing       = False
        self._reverse_cache    = []
        self._cache_building   = False
        self._loop_frame0      = None
        self._frame_cache      = None
        self._cache_loading    = False
        self._gpu_cache_ready  = False
        self._gpu_upload_idx   = 0
        self._gpu_build_gen   += 1
        self._is_hdr           = False
        self._pix_fmt          = "rgb24"
        self._bytes_per_pixel  = 3
        self._play_clock_start = 0.0
        self._play_frame_start = 0
        self._close_mc_next_pipe()
        self._mc_clips = []
        self._mc_idx   = -1
        self._in_frame  = None
        self._out_frame = None
        self._scrubber.set_in_out(None, None)
        self._scrubber.set_mc_clips([])
        self._play_btn.setIcon(_icon("play-button-arrowhead.png"))
        self._play_btn.setText("")
        self._canvas.clear_gpu_cache()
        self._canvas.clear_frame()

    # ------------------------------------------------------------------ #
    #  Style helpers                                                       #
    # ------------------------------------------------------------------ #

    def _playback_scrubber_style(self) -> str:
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
                background: white;
                width: 3px; height: 18px;
                border-radius: 1px; margin: -7px 0;
            }}
            QSlider::handle:horizontal:hover {{
                background: {constants.ACCENT_HI};
            }}
        """

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
        self._ocio    = OCIOManager()

        self._player.set_frame_callback(self._update_title)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome)
        self._stack.addWidget(self._player)
        self.setCentralWidget(self._stack)

        self._build_menu()
        self._setup_shortcuts()
        self._restore_geometry()
        self._restore_settings()
        self._player.video_ended.connect(self._on_video_ended)
        self._player.audio_loading.connect(
            self._player._playlist_sidebar.set_audio_loading)

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
        self._autoplay_act = pb.addAction("Autoplay")
        self._autoplay_act.setCheckable(True)
        self._autoplay_act.setChecked(True)
        self._autoplay_act.setShortcut("Ctrl+A")
        self._autoplay_act.triggered.connect(
            lambda c: setattr(self._player, "_autoplay", c))
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
        self._scrub_act.setChecked(True)
        self._scrub_act.triggered.connect(
            lambda checked: setattr(self._player._audio, "_scrub_enabled", checked))

        # Color (OCIO)
        cm = mb.addMenu("Color")
        load_ocio_act = cm.addAction("Load OCIO Config…")
        load_ocio_act.triggered.connect(self._on_load_ocio_config)
        cm.addSeparator()
        self._ocio_src_menu  = cm.addMenu("Input Color Space")
        self._ocio_disp_menu = cm.addMenu("Display")
        self._ocio_view_menu = cm.addMenu("View")
        cm.addSeparator()
        self._ocio_enable_act = cm.addAction("Enable Color Management")
        self._ocio_enable_act.setCheckable(True)
        self._ocio_enable_act.setShortcut("Ctrl+Shift+C")
        self._ocio_enable_act.triggered.connect(self._on_ocio_toggled)
        if not OCIO_AVAILABLE:
            cm.setEnabled(False)
            cm.setTitle("Color  (install opencolorio)")

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
        self._player._playlist_sidebar.add_video(path)
        self._player._playlist_sidebar.set_current(path)
        QTimer.singleShot(0, self._player._refresh_display)

    def open_playlist(self, paths: list):
        """Load *paths* as a playlist: open the first video and queue the rest."""
        valid = [p for p in paths if Path(p).is_file()]
        if not valid:
            return
        self.open_video(valid[0])
        for path in valid[1:]:
            self._player._playlist_sidebar.add_video(path)
        # Show the playlist sidebar so the user can see the queue
        self._player._playlist_sidebar.setVisible(True)
        self._player._playlist_btn.setChecked(True)

    def _on_video_ended(self):
        next_path = self._player._playlist_sidebar.next_path(self._player._path)
        if next_path:
            self.open_video(next_path)

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

        # Autoplay
        autoplay = s.value("autoplay", True, type=bool)
        self._player._autoplay = autoplay
        self._autoplay_act.setChecked(autoplay)

        # Volume / mute — set slider which propagates to _volume and mute button icon
        pre_mute = int(s.value("preMuteVolume", 100))
        muted    = s.value("muted", False, type=bool)
        volume   = int(s.value("volume", 100))
        self._player._pre_mute_volume = pre_mute
        self._player._vol_slider.setValue(0 if muted else volume)

        # Audio scrubbing (always enabled by default with new engine)
        scrub = s.value("audioScrubbing", True, type=bool)
        self._scrub_act.setChecked(scrub)
        self._player._audio._scrub_enabled = scrub

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
        s.setValue("autoplay",       self._player._autoplay)
        s.setValue("volume",         self._player._volume)
        s.setValue("preMuteVolume",  self._player._pre_mute_volume)
        s.setValue("muted",          self._player._volume == 0)
        s.setValue("audioScrubbing", getattr(self._player._audio, "_scrub_enabled", True))
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
    #  OCIO                                                                #
    # ------------------------------------------------------------------ #

    def _on_load_ocio_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load OCIO Config", "", "OCIO Config (*.ocio);;All files (*)")
        if not path:
            return
        if not self._ocio.load_config(path):
            QMessageBox.warning(self, "OCIO", f"Failed to load config:\n{path}")
            return
        self._rebuild_ocio_menus()

    def _rebuild_ocio_menus(self):
        """Repopulate Input CS / Display / View submenus from the loaded config."""
        sg_src  = QActionGroup(self); sg_src.setExclusive(True)
        sg_disp = QActionGroup(self); sg_disp.setExclusive(True)
        sg_view = QActionGroup(self); sg_view.setExclusive(True)

        self._ocio_src_menu.clear()
        for cs in self._ocio.get_input_color_spaces():
            a = self._ocio_src_menu.addAction(cs)
            a.setCheckable(True)
            a.setChecked(cs == self._ocio.current_src_cs())
            sg_src.addAction(a)
            a.triggered.connect(lambda _, c=cs: self._on_ocio_src_changed(c))

        self._ocio_disp_menu.clear()
        for disp in self._ocio.get_displays():
            a = self._ocio_disp_menu.addAction(disp)
            a.setCheckable(True)
            a.setChecked(disp == self._ocio.current_display())
            sg_disp.addAction(a)
            a.triggered.connect(lambda _, d=disp: self._on_ocio_disp_changed(d))

        self._rebuild_ocio_view_menu()

    def _rebuild_ocio_view_menu(self):
        sg_view = QActionGroup(self); sg_view.setExclusive(True)
        self._ocio_view_menu.clear()
        for view in self._ocio.get_views(self._ocio.current_display()):
            a = self._ocio_view_menu.addAction(view)
            a.setCheckable(True)
            a.setChecked(view == self._ocio.current_view())
            sg_view.addAction(a)
            a.triggered.connect(lambda _, v=view: self._on_ocio_view_changed(v))

    def _on_ocio_src_changed(self, cs: str):
        self._ocio.set_transform(cs, self._ocio.current_display(),
                                 self._ocio.current_view())
        if self._ocio_enable_act.isChecked():
            self._apply_ocio()

    def _on_ocio_disp_changed(self, disp: str):
        self._ocio.set_transform(self._ocio.current_src_cs(), disp,
                                 self._ocio.current_view())
        self._rebuild_ocio_view_menu()
        if self._ocio_enable_act.isChecked():
            self._apply_ocio()

    def _on_ocio_view_changed(self, view: str):
        self._ocio.set_transform(self._ocio.current_src_cs(),
                                 self._ocio.current_display(), view)
        if self._ocio_enable_act.isChecked():
            self._apply_ocio()

    def _on_ocio_toggled(self, checked: bool):
        if checked:
            self._apply_ocio()
        else:
            self._player._canvas.disable_ocio()

    def _apply_ocio(self):
        func_src, luts = self._ocio.build_gpu_shader()
        if func_src:
            self._player._canvas.set_ocio(func_src, luts)
        else:
            self._ocio_enable_act.setChecked(False)
            QMessageBox.warning(self, "OCIO",
                "Could not build GPU shader — check config and transform selection.")

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
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if Path(url.toLocalFile()).suffix.lower() in constants.VIDEO_EXTS
        ]
        if not paths:
            return
        self.open_video(paths[0])          # play the first one immediately
        for path in paths[1:]:             # queue the rest
            self._player._playlist_sidebar.add_video(path)

    def closeEvent(self, event):
        self._save_geometry()
        self._save_settings()
        self._player.stop()
        self._player._audio.close()
        super().closeEvent(event)
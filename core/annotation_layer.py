"""
AnnotationLayer — per-frame freehand strokes, persisted as a JSON sidecar.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path


class AnnotationLayer:
    """Per-frame freehand strokes, persisted as a JSON sidecar next to the video."""

    COLORS     = ["#ff4444", "#ffdd00", "#ffffff", "#00ddff", "#44ff88"]
    THICKNESSES = [2, 5, 12]

    def __init__(self):
        self.strokes:          dict = {}    # int → [stroke_dict, ...]
        self._redo:            dict = {}    # int → [stroke_dict, ...] (transient)
        self._in_progress:     dict | None = None
        self.visible:          bool = True
        self.show_in_playback: bool = True
        self.active_tool:      str  = "pen"
        self.pen_color:        str  = "#ff4444"
        self._tool_thickness: dict = {
            "pen": 5, "line": 3, "arrow": 3,
            "rect": 3, "ellipse": 3, "eraser": 24,
        }
        self.ghost_enabled: bool = False
        self._path:         str  = ""
        self._dirty:        bool = False
        self._clipboard:    list = []

    # -- Drawing --------------------------------------------------------- #

    def begin_stroke(self, u: float, v: float) -> None:
        t = self._tool_thickness.get(self.active_tool, 5)
        self._in_progress = {
            "tool": self.active_tool, "color": self.pen_color,
            "thickness": t, "points": [(u, v)],
        }

    def extend_stroke(self, u: float, v: float) -> None:
        if not self._in_progress:
            return
        tool = self._in_progress["tool"]
        pts  = self._in_progress["points"]
        if tool in ("pen", "eraser"):
            pts.append((u, v))
        else:
            # Shape tools — keep only [start, current] so live preview is cheap
            if len(pts) < 2:
                pts.append((u, v))
            else:
                pts[1] = (u, v)

    def end_stroke(self, frame: int) -> None:
        s = self._in_progress
        if s and s["points"]:
            self.strokes.setdefault(frame, []).append(s)
            self._redo.pop(frame, None)   # new stroke invalidates redo history
            self._dirty = True
        self._in_progress = None

    def cancel_stroke(self) -> None:
        self._in_progress = None

    def live_stroke(self) -> dict | None:
        return self._in_progress

    # -- Edit ------------------------------------------------------------ #

    def undo_stroke(self, frame: int) -> None:
        strokes = self.strokes.get(frame)
        if strokes:
            self._redo.setdefault(frame, []).append(strokes.pop())
            if not strokes:
                self.strokes.pop(frame, None)
            self._dirty = True

    def redo_stroke(self, frame: int) -> None:
        redo = self._redo.get(frame)
        if redo:
            self.strokes.setdefault(frame, []).append(redo.pop())
            if not redo:
                self._redo.pop(frame, None)
            self._dirty = True

    def clear_frame(self, frame: int) -> None:
        self.strokes.pop(frame, None)
        self._redo.pop(frame, None)
        self._dirty = True

    def clear_all(self) -> None:
        self.strokes.clear()
        self._redo.clear()
        self._dirty = True

    def copy_frame(self, frame: int) -> None:
        self._clipboard = copy.deepcopy(self.strokes.get(frame, []))

    def paste_frame(self, frame: int) -> None:
        if not self._clipboard:
            return
        self.strokes[frame] = copy.deepcopy(self._clipboard)
        self._redo.pop(frame, None)
        self._dirty = True

    # -- Thickness ------------------------------------------------------- #

    def get_thickness(self, tool: str) -> int:
        return self._tool_thickness.get(tool, 5)

    def set_thickness(self, tool: str, value: int) -> None:
        self._tool_thickness[tool] = value

    # -- State ----------------------------------------------------------- #

    def has_clipboard(self) -> bool:
        return bool(self._clipboard)

    def is_dirty(self) -> bool:
        return self._dirty

    def save(self) -> None:
        self._save()
        self._dirty = False

    def discard(self) -> None:
        """Reload from disk, throwing away all in-memory changes."""
        self.strokes.clear()
        self._redo.clear()
        self._in_progress = None
        if self._path:
            self._load()
        self._dirty = False

    def set_video_path(self, path: str) -> None:
        self.strokes.clear()
        self._redo.clear()
        self._in_progress = None
        self._path = path
        self._dirty = False

    # -- Persistence ----------------------------------------------------- #

    def _sidecar(self) -> str:
        return (self._path + ".annotations.json") if self._path else ""

    def _save(self) -> None:
        p = self._sidecar()
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in self.strokes.items()}, f,
                          separators=(",", ":"))
        except Exception:
            pass

    def _load(self) -> None:
        p = self._sidecar()
        if not p or not Path(p).exists():
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            self.strokes = {int(k): v for k, v in data.items()}
        except Exception:
            pass

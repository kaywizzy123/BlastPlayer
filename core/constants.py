import sys as _sys
import shutil as _shutil
from pathlib import Path as _Path

# ── Colour palette ──────────────────────────────────────────────────────────
BG             = "#1A1A1A"
BORDER         = "#0a0a0a"
ACCENT         = "#343434"
ACCENT_HI      = "#1085d3"
TEXT_PRI       = "#ededed"
TEXT_SEC       = "#a1a1a1"
SPLITTER_COLOR = "#292929"

# ── Supported video extensions ──────────────────────────────────────────────
EXR_EXTS   = {".exr", ".dpx"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm"} | EXR_EXTS

# ── FFmpeg / ffplay path detection ──────────────────────────────────────────
if _sys.platform == "win32":
    _FFMPEG_CANDIDATES = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
elif _sys.platform == "darwin":
    _FFMPEG_CANDIDATES = [
        "/opt/homebrew/bin/ffmpeg",   # Homebrew Apple Silicon
        "/usr/local/bin/ffmpeg",      # Homebrew Intel / manual
        "/opt/local/bin/ffmpeg",      # MacPorts
    ]
else:
    _FFMPEG_CANDIDATES = [
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/snap/bin/ffmpeg",
    ]


def _find_ffmpeg() -> str:
    found = _shutil.which("ffmpeg")
    if found:
        return found
    for c in _FFMPEG_CANDIDATES:
        if _Path(c).exists():
            return c
    return _FFMPEG_CANDIDATES[0]


FFMPEG_PATH = _find_ffmpeg()

# ── GPU texture cache ───────────────────────────────────────────────────────
GPU_CACHE_MAX_MB = 1024   # maximum GPU texture pool size in MiB

# ── OpenColorIO ──────────────────────────────────────────────────────────────
OCIO_CONFIG_PATH = ""     # empty → use $OCIO env var or PyOpenColorIO built-in default

# ── Icons ───────────────────────────────────────────────────────────────────
ICONS_DIR: _Path = _Path(__file__).parent.parent / "icons"

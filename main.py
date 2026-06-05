"""
BlastPlayer — standalone animation studio video player
-------------------------------------------------------
Usage:
    python main.py                   # open with no video (welcome screen)
    python main.py path/to/video.mp4 # open a specific video immediately
"""

import sys
import tempfile
from pathlib import Path

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QFileSystemWatcher

from core.styles import qt_argv, styleSheet
from player_window import BlastPlayerWindow

# Temp file BlastVault writes when it wants this window to load a new video.
REQUEST_FILE = Path(tempfile.gettempdir()) / "blastvault_player_request.txt"


def _setup_file_watcher(window: "BlastPlayerWindow") -> QFileSystemWatcher:
    """Watch REQUEST_FILE; when BlastVault writes paths there, load them."""
    watcher = QFileSystemWatcher()
    # Watch the directory — the file may not exist yet on first launch.
    watcher.addPath(str(REQUEST_FILE.parent))

    def _on_dir_changed(_path: str):
        if not REQUEST_FILE.exists():
            return
        try:
            content = REQUEST_FILE.read_text(encoding="utf-8").strip()
            REQUEST_FILE.unlink(missing_ok=True)
        except OSError:
            return
        paths = [p for p in content.splitlines() if p.strip() and Path(p.strip()).is_file()]
        if not paths:
            return
        if len(paths) == 1:
            window.open_video(paths[0])
        else:
            window.open_playlist(paths)
        window.raise_()
        window.activateWindow()

    watcher.directoryChanged.connect(_on_dir_changed)
    return watcher


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(qt_argv())
    app.setStyle("Fusion")
    app.setStyleSheet(styleSheet)
    app.setApplicationName("BlastPlayer")

    window = BlastPlayerWindow()
    window.show()

    # Watch for BlastVault "load this video/playlist" requests — keep ref on window.
    window._file_watcher = _setup_file_watcher(window)

    # Open video(s) passed via command line
    if len(sys.argv) > 1:
        video_paths = [p for p in sys.argv[1:] if Path(p).is_file()]
        if len(video_paths) == 1:
            window.open_video(video_paths[0])
        elif len(video_paths) > 1:
            window.open_playlist(video_paths)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

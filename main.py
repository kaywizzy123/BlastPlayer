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
    """Watch REQUEST_FILE; when BlastVault writes a path there, load it."""
    watcher = QFileSystemWatcher()
    # Watch the directory — the file may not exist yet on first launch.
    watcher.addPath(str(REQUEST_FILE.parent))

    def _on_dir_changed(_path: str):
        if not REQUEST_FILE.exists():
            return
        try:
            video_path = REQUEST_FILE.read_text(encoding="utf-8").strip()
            REQUEST_FILE.unlink(missing_ok=True)
        except OSError:
            return
        if video_path and Path(video_path).is_file():
            window.open_video(video_path)
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

    # Watch for BlastVault "load this video" requests — keep ref on window.
    window._file_watcher = _setup_file_watcher(window)

    # Open a video passed via command line
    if len(sys.argv) > 1:
        video_path = sys.argv[1]
        if Path(video_path).is_file():
            window.open_video(video_path)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

"""
BlastPlayer — standalone animation studio video player
-------------------------------------------------------
Usage:
    python main.py                   # open with no video (welcome screen)
    python main.py path/to/video.mp4 # open a specific video immediately
"""

import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from core.styles import qt_argv, styleSheet
from player_window import BlastPlayerWindow


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(qt_argv())
    app.setStyle("Fusion")
    app.setStyleSheet(styleSheet)
    app.setApplicationName("BlastPlayer")

    window = BlastPlayerWindow()
    window.showMaximized()

    # Open a video passed via command line
    if len(sys.argv) > 1:
        video_path = sys.argv[1]
        if Path(video_path).is_file():
            window.open_video(video_path)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

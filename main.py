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
from PyQt5.QtNetwork import QLocalServer

from core.styles import qt_argv, styleSheet
from player_window import BlastPlayerWindow

_IPC_SERVER_NAME = "blastvault-player"


def _start_ipc_server(window: "BlastPlayerWindow") -> QLocalServer | None:
    """Try to claim the named local socket so BlastVault can reuse this window.

    Only the first running instance will succeed.  Extra windows (opened via
    right-click "Open in new window") simply run without a server — that is
    intentional; left-click in BlastVault always targets the primary window.
    """
    QLocalServer.removeServer(_IPC_SERVER_NAME)  # clean up any stale socket
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(_IPC_SERVER_NAME):
        return None  # another instance already owns the socket — fine

    def _on_connection():
        conn = server.nextPendingConnection()
        if not conn:
            return
        buf: list[bytes] = []

        def _on_ready():
            buf.append(conn.readAll().data())

        def _on_disconnected():
            path = b"".join(buf).decode(errors="replace").strip()
            if path and Path(path).is_file():
                window.open_video(path)
                window.raise_()
                window.activateWindow()
            conn.deleteLater()

        conn.readyRead.connect(_on_ready)
        conn.disconnected.connect(_on_disconnected)

    server.newConnection.connect(_on_connection)
    return server  # caller must keep this reference alive


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(qt_argv())
    app.setStyle("Fusion")
    app.setStyleSheet(styleSheet)
    app.setApplicationName("BlastPlayer")

    window = BlastPlayerWindow()
    window.show()

    # Claim the IPC socket — kept alive on the window so it isn't GC'd
    window._ipc_server = _start_ipc_server(window)

    # Open a video passed via command line
    if len(sys.argv) > 1:
        video_path = sys.argv[1]
        if Path(video_path).is_file():
            window.open_video(video_path)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap

from core import constants


class WelcomeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: black;")
        self.create_widgets()
        self.create_layout()
        self.create_connections()

    def create_widgets(self):
        self._logo = QLabel()
        pix = QPixmap(str(constants.ICONS_DIR / "clapperboard.png"))
        if not pix.isNull():
            self._logo.setPixmap(
                pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._logo.setStyleSheet("background: transparent;")
        else:
            self._logo.setText("🎬")
            self._logo.setStyleSheet("font-size: 52px; background: transparent;")
        self._logo.setAlignment(Qt.AlignCenter)

        self._title = QLabel("BlastPlayer")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setStyleSheet(
            f"font-size: 26px; font-weight: bold;"
            f"color: {constants.TEXT_PRI}; background: transparent;"
        )

        self._hint = QLabel("Drop a video file here  ·  or  File → Open")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet(
            f"font-size: 13px; color: {constants.TEXT_SEC}; background: transparent;"
        )

    def create_layout(self):
        layout = QVBoxLayout(self)
        layout.addStretch(2)
        layout.addWidget(self._logo)
        layout.addSpacing(8)
        layout.addWidget(self._title)
        layout.addSpacing(12)
        layout.addWidget(self._hint)
        layout.addStretch(3)

    def create_connections(self):
        pass

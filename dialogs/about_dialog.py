from PyQt5.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap

from core import constants


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About BlastPlayer")
        self.setFixedSize(500, 300)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.create_widgets()
        self.create_layout()
        self.create_connections()

    def create_widgets(self):
        self.logo_label = QLabel()
        pix = QPixmap(str(constants.ICONS_DIR / "clapperboard.png"))
        if not pix.isNull():
            self.logo_label.setPixmap(
                pix.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        self.logo_label.setAlignment(Qt.AlignCenter)
        self.logo_label.setStyleSheet("background: transparent;")

        self.title_label = QLabel("BlastPlayer v1.0")
        self.title_label.setStyleSheet(f"""
            font-size: 22px;
            font-weight: bold;
            color: {constants.TEXT_PRI};
            background: transparent;
        """)
        self.title_label.setAlignment(Qt.AlignCenter)

        self.desc_label = QLabel(
            "This app was designed and developed by\n"
            "Oluwakayode Ogunremi\n©2026"
        )
        self.desc_label.setStyleSheet(f"""
            font-size: 13px;
            color: {constants.TEXT_SEC};
            background: transparent;
        """)
        self.desc_label.setAlignment(Qt.AlignCenter)

        self.close_button = QPushButton("Close")
        self.close_button.setFixedWidth(100)
        self.close_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {constants.SPLITTER_COLOR};
                color: {constants.TEXT_PRI};
                border: none;
                padding: 6px 12px;
                border-radius: 5px;
            }}
            QPushButton:hover {{
                background-color: {constants.ACCENT};
            }}
        """)

    def create_layout(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(12)
        main_layout.addStretch()
        main_layout.addWidget(self.logo_label)
        main_layout.addWidget(self.title_label)
        main_layout.addWidget(self.desc_label)
        main_layout.addStretch()

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.close_button)
        button_layout.addStretch()
        main_layout.addLayout(button_layout)

    def create_connections(self):
        self.close_button.clicked.connect(self.close)

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PyQt5.QtCore import Qt

from core import constants


class AnnSaveDialog(QDialog):
    """
    Dark-themed Save / Discard / Cancel dialog for unsaved annotation changes.

    Returns
    -------
    2  Save
    1  Discard
    0  Cancel
    """

    def __init__(self, message: str, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Unsaved Annotations")
        self.setModal(True)
        self._message = message
        self.create_widgets()
        self.create_layout()
        self.create_connections()

    def create_widgets(self):
        self.setStyleSheet(
            f"QDialog{{background:#1c1c1c;color:{constants.TEXT_PRI};}}"
            f"QLabel{{color:{constants.TEXT_PRI};background:transparent;"
            f"font-size:13px;padding:0;}}"
            f"QPushButton{{background:#2a2a2a;color:{constants.TEXT_PRI};"
            f"border:1px solid #3a3a3a;border-radius:4px;"
            f"padding:5px 18px;font-size:12px;min-width:72px;}}"
            f"QPushButton:hover{{background:#363636;border-color:#555;}}"
            f"QPushButton#save_btn{{background:{constants.ACCENT_HI};"
            f"border-color:{constants.ACCENT_HI};}}"
            f"QPushButton#save_btn:hover{{background:#1a9cf0;"
            f"border-color:#1a9cf0;}}"
        )

        self._lbl        = QLabel(self._message)
        self._lbl.setWordWrap(True)
        self._cancel_btn = QPushButton("Cancel")
        self._discard_btn = QPushButton("Discard")
        self._save_btn   = QPushButton("Save")
        self._save_btn.setObjectName("save_btn")
        self._save_btn.setDefault(True)

    def create_layout(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(18)
        root.addWidget(self._lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._discard_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

    def create_connections(self):
        self._cancel_btn.clicked.connect(lambda: self.done(0))
        self._discard_btn.clicked.connect(lambda: self.done(1))
        self._save_btn.clicked.connect(lambda: self.done(2))

    # -- Convenience ----------------------------------------------------- #

    @staticmethod
    def ask(message: str, parent=None) -> int:
        """Create, run, and return the result in one call."""
        return AnnSaveDialog(message, parent).exec_()

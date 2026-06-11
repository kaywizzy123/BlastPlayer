from PyQt5.QtGui import QIcon

from core import constants


def icon(name: str) -> QIcon:
    p = constants.ICONS_DIR / name
    return QIcon(str(p)) if p.exists() else QIcon()

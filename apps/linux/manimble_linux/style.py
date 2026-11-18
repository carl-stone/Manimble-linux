"""Manimble's look (Theme.swift): quiet neutral surfaces, one orange accent.

Colours that follow light/dark are functions of the current colour scheme; call them when painting, not once.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication


def is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return app.palette().window().color().lightnessF() < 0.5


def _dyn(light: QColor, dark: QColor) -> QColor:
    return QColor(dark if is_dark() else light)


def _rgb(r: float, g: float, b: float, a: float = 1.0) -> QColor:
    return QColor.fromRgbF(r, g, b, a)


class Theme:
    accent = _rgb(1.0, 0.424, 0.047)                     # #FF6C0C
    accent_soft = _rgb(1.0, 0.424, 0.047, 0.18)
    warm = _rgb(0.047, 0.549, 1.0)                       # #0C8CFF, a blue foil to the accent, used sparingly

    @staticmethod
    def canvas_top() -> QColor:
        return _dyn(_rgb(0.965, 0.965, 0.965), _rgb(0.150, 0.150, 0.150))

    @staticmethod
    def canvas_bottom() -> QColor:
        """The stage around the slide, the rulers and the drawer."""
        return _dyn(_rgb(0.918, 0.918, 0.918), _rgb(0.080, 0.080, 0.080))

    @staticmethod
    def code_background() -> QColor:
        """The code card and the terminals."""
        return _dyn(_rgb(0.988, 0.988, 0.988), _rgb(0.105, 0.105, 0.105))

    @staticmethod
    def bar_background() -> QColor:
        """Header strips above the code and in the drawer."""
        return _dyn(_rgb(0.945, 0.945, 0.945), _rgb(0.132, 0.132, 0.132))

    @staticmethod
    def sidebar_background() -> QColor:
        return _dyn(_rgb(0.93, 0.93, 0.93), _rgb(0.17, 0.17, 0.17))

    @staticmethod
    def ink(alpha: float) -> QColor:
        """Foreground ink on the chrome: white at ``alpha`` in dark mode, black in light mode."""
        return _rgb(1, 1, 1, alpha) if is_dark() else _rgb(0, 0, 0, alpha)

    @staticmethod
    def font(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        f = QFont(QGuiApplication.font())
        f.setPointSizeF(size)
        f.setWeight(weight)
        return f

    @staticmethod
    def mono(size: float) -> QFont:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setPointSizeF(size)
        return f


def hex_color(c: QColor) -> str:
    """``#RRGGBB`` as the engine takes colours."""
    return "#{:02X}{:02X}{:02X}".format(c.red(), c.green(), c.blue())


def color_from_hex(s: str | None, default: str = "#FFFFFF") -> QColor:
    c = QColor(s or default)
    return c if c.isValid() else QColor(default)

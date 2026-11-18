"""Manimble's look (Theme.swift): quiet neutral surfaces, one orange accent.

Colours follow the current Qt appearance. Font sizes are logical screen sizes;
set MANIMBLE_TEXT_SCALE to a positive multiplier (e.g. 1.25) for larger text.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import math
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication


def watch_appearance(refresh: Callable[[], None]) -> None:
    """Connect a widget's bound refresh method to both native appearance signals."""
    app = QGuiApplication.instance()
    app.paletteChanged.connect(refresh)
    # The style hint is emitted before Qt settles its palette.
    app.styleHints().colorSchemeChanged.connect(refresh, Qt.ConnectionType.QueuedConnection)


def is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    palette = app.palette()
    surface = palette.window().color().lightnessF()
    label = palette.windowText().color().lightnessF()
    # An explicit Qt palette can differ from the desktop's scheme hint.
    if (surface < .5) != (label < .5):
        return surface < .5
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return surface < .5


def _dyn(light: QColor, dark: QColor) -> QColor:
    return QColor(dark if is_dark() else light)


def _rgb(r: float, g: float, b: float, a: float = 1.0) -> QColor:
    return QColor.fromRgbF(r, g, b, a)


@lru_cache(maxsize=1)
def _chrome_families() -> tuple[str, str]:
    installed = {family.casefold(): family for family in QFontDatabase.families()}

    def choose(preferred, system):
        for name in preferred:
            if name.casefold() in installed:
                return installed[name.casefold()]
        return QFontDatabase.systemFont(system).family()

    return (
        choose(("SF Pro Text", "Adwaita Sans", "Inter"), QFontDatabase.SystemFont.GeneralFont),
        choose(("SF Mono", "Adwaita Mono", "DejaVu Sans Mono"), QFontDatabase.SystemFont.FixedFont),
    )


@lru_cache(maxsize=1)
def _text_scale(value: str) -> float:
    try:
        scale = float(value)
    except ValueError:
        return 1.0
    return scale if math.isfinite(scale) and scale > 0 else 1.0


def _point_size(size: float) -> float:
    screen = QGuiApplication.primaryScreen()
    dpi = screen.logicalDotsPerInchY() if screen else 96.0
    # Qt applies the device-pixel scale itself.
    return Theme.logical_size(size) * 72 / dpi


def _font(family: str, points: float, weight: QFont.Weight) -> QFont:
    f = QFont(family)
    f.setPointSizeF(points)
    f.setWeight(weight)
    return f


@lru_cache(maxsize=32)
def _tabular_font(family: str, fixed: str, points: float, weight: QFont.Weight) -> QFont:
    f = _font(family, points, weight)
    f.setFeature(QFont.Tag.fromString("tnum"), 1)
    metrics = QFontMetricsF(f)
    advance = metrics.horizontalAdvance("0")
    if any(metrics.horizontalAdvance(digit) != advance for digit in "123456789"):
        f.setFamily(fixed)
    return f


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
    def secondary_ink() -> QColor:
        return Theme.ink(.65)

    @staticmethod
    def tertiary_ink() -> QColor:
        return Theme.ink(.45)

    @staticmethod
    def separator() -> QColor:
        return Theme.ink(.14)

    @staticmethod
    def error_ink() -> QColor:
        return _dyn(QColor("#d70015"), QColor("#ff453a"))

    @staticmethod
    def warning_ink() -> QColor:
        return _dyn(QColor("#ff9500"), QColor("#ff9f0a"))

    @staticmethod
    def logical_size(size: float) -> float:
        """Chrome size in logical pixels, including the explicit text scale."""
        return size * _text_scale(os.environ.get("MANIMBLE_TEXT_SCALE", "1"))

    @staticmethod
    def font(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        return _font(_chrome_families()[0], _point_size(size), weight)

    @staticmethod
    def mono(size: float) -> QFont:
        return _font(_chrome_families()[1], _point_size(size), QFont.Weight.Normal)

    @staticmethod
    def tabular(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        """Proportional UI text with stable digit widths, or fixed width if unavailable."""
        family, fixed = _chrome_families()
        return QFont(_tabular_font(family, fixed, _point_size(size), weight))


def hex_color(c: QColor) -> str:
    """``#RRGGBB`` as the engine takes colours."""
    return "#{:02X}{:02X}{:02X}".format(c.red(), c.green(), c.blue())


def color_from_hex(s: str | None, default: str = "#FFFFFF") -> QColor:
    c = QColor(s or default)
    return c if c.isValid() else QColor(default)

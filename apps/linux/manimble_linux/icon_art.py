"""Small, full-color illustrations used beside native editor controls."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPen
from PySide6.QtWidgets import QMenu, QProxyStyle, QStyle

from .icons import painted_icon
from .style import Theme, color_from_hex

LAYOUT_ICON_SIZE = QSize(44, 25)
THEME_SWATCH_SIZE = QSize(28, 18)
TEXT_COLOR_ICON_SIZE = QSize(20, 18)

# Miniature placeholders on the reference 13.333 × 7.5 inch slide (SlideTools.swift).
_LAYOUT_BOXES = {
    "title": ((1.667, 1.227, 10.0, 2.61), (1.667, 3.938, 10.0, 1.81)),
    "title_content": ((0.917, 0.399, 11.5, 1.45), (0.917, 1.996, 11.5, 4.76)),
    "two_content": ((0.917, 0.399, 11.5, 1.45), (0.917, 1.996, 5.667, 4.76), (6.75, 1.996, 5.667, 4.76)),
    "title_only": ((0.917, 0.399, 11.5, 1.45),),
    "blank": (),
}


def layout_icon(layout: str, background: QColor | str = "#FFFFFF") -> QIcon:
    """A miniature with the authored layout's placeholders, on the given slide background."""
    bg = QColor(background)
    if not bg.isValid():
        bg = QColor(Qt.GlobalColor.white)
    light = 0.2126 * bg.redF() + 0.7152 * bg.greenF() + 0.0722 * bg.blueF() > 0.5
    frame = 0.55 if light else 0.45
    boxes = _LAYOUT_BOXES.get(layout, ())

    def draw(painter, rect):
        slide = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillRect(slide, bg)
        painter.setPen(QPen(QColor.fromRgbF(frame, frame, frame), 1))
        painter.drawRect(slide)
        scale = slide.width() / 13.333
        dashed = QPen(QColor.fromRgbF(0.6, 0.6, 0.6), 0.8)
        dashed.setCapStyle(Qt.PenCapStyle.FlatCap)
        # Qt interprets dash lengths in multiples of the pen width.
        dashed.setDashPattern([1.6 / 0.8, 1.0 / 0.8])
        painter.setPen(dashed)
        for left, top, width, height in boxes:
            box = QRectF(slide.x() + left * scale, slide.y() + top * scale, width * scale, height * scale)
            painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))

    return painted_icon(LAYOUT_ICON_SIZE.width(), LAYOUT_ICON_SIZE.height(), draw)


def pen_color_icon(color: str) -> QIcon:
    dot = QColor(color)

    def draw(painter, rect):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(rect.adjusted(2, 2, -2, -2))

    return painted_icon(16, 16, draw)


def text_color_icon(color: QColor) -> QIcon:
    strip = QColor(color)

    def draw(painter, rect):
        painter.setFont(Theme.font(13, QFont.Weight.DemiBold))
        painter.setPen(Theme.ink(1))
        painter.drawText(rect.adjusted(0, 0, 0, -3), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, "A")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(strip)
        painter.drawRoundedRect(QRectF(rect.x() + 2, rect.bottom() - 4, 16, 3.5), 1, 1)

    return painted_icon(TEXT_COLOR_ICON_SIZE.width(), TEXT_COLOR_ICON_SIZE.height(), draw)


def theme_swatch(background: str, text: str) -> QIcon:
    bg = color_from_hex(background, "#000000")
    fg = color_from_hex(text)

    def draw(painter, rect):
        painter.setBrush(bg)
        painter.setPen(QPen(Theme.ink(0.16), 1))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        painter.setFont(Theme.font(10, QFont.Weight.DemiBold))
        painter.setPen(fg)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Aa")

    return painted_icon(THEME_SWATCH_SIZE.width(), THEME_SWATCH_SIZE.height(), draw)


class _MenuIconStyle(QProxyStyle):
    def __init__(self, width: int):
        super().__init__()
        self.width = width

    def pixelMetric(self, metric, option=None, widget=None):
        value = super().pixelMetric(metric, option, widget)
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return max(value, self.width)
        return value


def set_menu_icon_size(menu: QMenu, size: QSize) -> None:
    """Make room for wide art while keeping native rows, checkmarks and QAction dispatch."""
    style = _MenuIconStyle(size.width())
    style.setParent(menu)
    menu.setStyle(style)

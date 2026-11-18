"""Asset and template icons shared by the Linux editor's controls."""
from __future__ import annotations

from functools import cache, lru_cache
from weakref import ref

from PySide6.QtCore import QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .paths import RESOURCES, app_root

ASSETS = app_root() / 'apps/macos/assets'


@cache
def _source(name):
    if name == 'biocartoon':
        path = ASSETS / 'biocartoon-mono.png'
        image = QImage(str(path))
        if not image.isNull():
            return image
    else:
        path = RESOURCES / 'icons' / (name + '.svg')
        svg = QSvgRenderer(str(path))
        if svg.isValid():
            return svg
    raise RuntimeError(f'Cannot load Manimble icon: {path}')


@lru_cache(maxsize=512)
def _render(name, width, height, scale, rgba, mode, state):
    pixmap = QPixmap(max(1, round(width * scale)), max(1, round(height * scale)))
    pixmap.fill(Qt.GlobalColor.transparent)
    source = _source(name)
    bounds = QRectF(pixmap.rect())
    with QPainter(pixmap) as painter:
        if isinstance(source, QImage):
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(bounds, source)
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            source.render(painter, bounds)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(bounds, QColor.fromRgba(rgba))
    pixmap.setDevicePixelRatio(scale)
    return pixmap


class _TemplateIcon(QIconEngine):
    def __init__(self, name, control, tint):
        super().__init__()
        self.name = name
        self.control = control
        self.tint = tint

    def clone(self):
        return _TemplateIcon(self.name, self.control, self.tint)

    def iconName(self):
        return self.name

    def scaledPixmap(self, size, mode, state, scale):
        control = self.control() if self.control else None
        palette = control.palette() if control is not None else QGuiApplication.palette()
        role = QPalette.ColorRole.ButtonText
        if mode == QIcon.Mode.Disabled:
            color = palette.color(QPalette.ColorGroup.Disabled, role)
        elif mode == QIcon.Mode.Selected:
            color = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText)
        elif self.tint is not None:
            color = self.tint
        else:
            if state == QIcon.State.On:
                role = QPalette.ColorRole.Accent
            group = QPalette.ColorGroup.Active if mode == QIcon.Mode.Active else palette.currentColorGroup()
            color = palette.color(group, role)
        return _render(self.name, size.width(), size.height(), scale, color.rgba(), mode, state)

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def paint(self, painter, rect, mode, state):
        pixmap = self.scaledPixmap(rect.size(), mode, state, painter.device().devicePixelRatioF())
        painter.drawPixmap(rect, pixmap)


def template_icon(name, control=None, *, tint=None):
    """Tint a shipped symbol with the current control palette, not the desktop icon theme."""
    _source(name)
    return QIcon(_TemplateIcon(name, ref(control) if control is not None else None, tint))


class _PaintedIcon(QIconEngine):
    def __init__(self, width, height, draw):
        super().__init__()
        self.width, self.height, self.draw = width, height, draw

    def clone(self):
        return _PaintedIcon(self.width, self.height, self.draw)

    def actualSize(self, size, mode, state):
        natural = QSize(self.width, self.height)
        return natural.scaled(natural.boundedTo(size), Qt.AspectRatioMode.KeepAspectRatio)

    def paint(self, painter, rect, mode, state):
        natural = QSizeF(self.width, self.height)
        fitted = natural.scaled(natural.boundedTo(QSizeF(rect.size())), Qt.AspectRatioMode.KeepAspectRatio)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(rect.x() + (rect.width() - fitted.width()) / 2,
                              rect.y() + (rect.height() - fitted.height()) / 2)
            painter.scale(fitted.width() / natural.width(), fitted.height() / natural.height())
            self.draw(painter, QRectF(0, 0, self.width, self.height))
        finally:
            painter.restore()

    def scaledPixmap(self, size, mode, state, scale):
        fitted = self.actualSize(size, mode, state)
        pixmap = QPixmap(max(1, round(fitted.width() * scale)), max(1, round(fitted.height() * scale)))
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(scale)
        with QPainter(pixmap) as painter:
            self.paint(painter, QRect(0, 0, fitted.width(), fitted.height()), mode, state)
        return pixmap

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)


def painted_icon(width, height, draw):
    """Render full-color art at its logical size with the live appearance."""
    return QIcon(_PaintedIcon(width, height, draw))

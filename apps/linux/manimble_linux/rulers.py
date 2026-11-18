"""Manim-unit rulers, sharing the canvas's top-left frame coordinate mapping."""
from __future__ import annotations

from enum import Enum
from math import ceil, floor

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from .canvas import guide
from .style import Theme, is_dark, watch_appearance


class RulerView(QWidget):
    class Orientation(str, Enum):
        horizontal = "horizontal"
        vertical = "vertical"

    thickness = 20

    def __init__(self, orientation, canvas=None, parent=None):
        super().__init__(parent)
        self.orientation = self.Orientation(orientation)
        self.canvas = canvas
        self._cursor = None
        self._dragging = False
        self._drag_vertical = False
        self.setMouseTracking(True)
        if self.horizontal:
            self.setFixedHeight(self.thickness)
        else:
            self.setFixedWidth(self.thickness)
        self.setToolTip("Drag down onto the slide to add a horizontal guide" if self.horizontal else "Drag right onto the slide to add a vertical guide")
        watch_appearance(self.update)

    @property
    def horizontal(self):
        return self.orientation == self.Orientation.horizontal

    @property
    def cursor(self):
        return self._cursor

    @cursor.setter
    def cursor(self, value):
        self._cursor = value
        self.update()

    @property
    def pts_per_unit(self):
        return self.canvas.unit_px*self.canvas.effective_scale if self.canvas else 90

    def view_pos(self, frame_px):
        if self.canvas is None:
            return 0
        c = self.canvas; r = c.slide_rect
        point = QPointF(r.x()+frame_px*c.effective_scale,r.center().y()) if self.horizontal else QPointF(r.center().x(),r.y()+frame_px*c.effective_scale)
        point = self.mapFromGlobal(c.viewport().mapToGlobal(point))       # rulers and canvas are siblings
        return point.x() if self.horizontal else point.y()

    @property
    def center_pos(self):
        return self.view_pos(self.canvas.frame_size[0 if self.horizontal else 1]/2) if self.canvas else 0

    @property
    def step(self):
        ppu, min_spacing = self.pts_per_unit, Theme.logical_size(44)
        for size in (.1,.2,.25,.5,1,2,5,10,20):
            if size*ppu >= min_spacing:
                return size,5 if size in (.1,.5,5,10,20) else 4
        return 50,5

    def _along(self, lo, hi):
        return QRectF(lo,0,hi-lo,self.height()) if self.horizontal else QRectF(0,lo,self.width(),hi-lo)

    def _tick(self, pos, length, thick):
        pos = floor(pos + .5) - .5
        return QRectF(pos, thick - length, 1, length) if self.horizontal else QRectF(thick - length, pos, length, 1)

    def paintEvent(self, event):
        p = QPainter(self); p.fillRect(self.rect(),Theme.canvas_bottom())
        c = self.canvas
        if c is None:
            return
        length = self.width() if self.horizontal else self.height()
        thick = self.height() if self.horizontal else self.width()
        lo,hi = self.view_pos(0),self.view_pos(c.frame_size[0 if self.horizontal else 1])
        p.fillRect(self._along(min(lo,hi),max(lo,hi)),Theme.ink(.075))
        b = c.selected_box
        if b is not None:
            a,z = self.view_pos(b.x0 if self.horizontal else b.y0),self.view_pos(b.x1 if self.horizontal else b.y1)
            color = QColor(Theme.accent); color.setAlphaF(.28)
            p.fillRect(self._along(min(a,z),max(a,z)),color)
        major,minor = self.step; spacing = major/minor; ppu = self.pts_per_unit
        center = self.center_pos
        p.setFont(Theme.mono(8.5))
        metrics = QFontMetricsF(p.font(), p.device())
        major_ink, minor_ink, label_ink = Theme.ink(.6), Theme.ink(.28), Theme.ink(.72)
        first,last = floor(-center/ppu/spacing),ceil((length-center)/ppu/spacing)
        decimals = 0 if major >= 1 else 2 if major == .25 else 1
        for i in range(first,last+1):
            pos = center+i*spacing*ppu; is_major = i%minor == 0
            if not -1 <= pos <= length + 1:
                continue
            tick = thick if is_major else thick*(.42 if minor%2 == 0 and i%(minor//2) == 0 else .26)
            p.fillRect(self._tick(pos, tick, thick), major_ink if is_major else minor_ink)
            if is_major:
                value = i*spacing*(1 if self.horizontal else -1)
                if abs(value)<spacing/1000: value=0
                text = f"{value:.{decimals}f}"
                p.setPen(label_ink)
                if self.horizontal:
                    p.drawText(QPointF(pos + 3, 1 + metrics.ascent()), text)
                else:
                    p.save()
                    p.translate(thick - 1 - metrics.descent(), pos - 3)
                    p.rotate(-90)
                    p.drawText(QPointF(), text)
                    p.restore()
        guide_color = QColor.fromRgbF(.25, .85, 1.) if is_dark() else QColor.fromRgbF(0., .55, .75)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for g in c.user_guides:
            vertical,pos = guide(g)
            if vertical != self.horizontal:
                continue
            pos = self.view_pos(pos); path = QPainterPath()
            if self.horizontal:
                path.moveTo(pos-3,thick); path.lineTo(pos+3,thick); path.lineTo(pos,thick-6)
            else:
                path.moveTo(thick,pos-3); path.lineTo(thick,pos+3); path.lineTo(thick-6,pos)
            path.closeSubpath(); p.fillPath(path, guide_color)
        if self.cursor is not None:
            pos = self.view_pos(self.cursor.x() if self.horizontal else self.cursor.y())
            p.fillRect(self._tick(pos, thick, thick), Theme.ink(.85))
        edge = QRectF(0, thick - 1, length, 1) if self.horizontal else QRectF(thick - 1, 0, 1, length)
        p.fillRect(edge, Theme.separator())

    def guide_marker(self, point):
        if self.canvas is None:
            return None
        value = point.x() if self.horizontal else point.y()
        return next((i for i,g in enumerate(self.canvas.user_guides) if guide(g)[0] == self.horizontal and abs(self.view_pos(guide(g)[1])-value)<=5),None)

    def _frame_point(self, event):
        point = self.canvas.viewport().mapFromGlobal(event.globalPosition().toPoint())
        return self.canvas.frame_point(point)

    def mousePressEvent(self, event):
        if self.canvas is None or event.button() != Qt.MouseButton.LeftButton:
            return
        marker = self.guide_marker(event.position())
        point = self._frame_point(event)
        if marker is None:
            self._drag_vertical = not self.horizontal
            self.canvas.begin_guide_drag(self._drag_vertical,point.x() if self._drag_vertical else point.y())
        else:
            self._drag_vertical = guide(self.canvas.user_guides[marker])[0]
            self.canvas.continue_guide_drag(marker)
        self._dragging = True

    def mouseMoveEvent(self, event):
        if self._dragging:
            point = self._frame_point(event)
            self.canvas.update_guide_drag(point.x() if self._drag_vertical else point.y())
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if (self.guide_marker(event.position()) is not None) == self.horizontal else Qt.CursorShape.SizeVerCursor)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False; self.canvas.end_guide_drag()


class RulerMixin:
    @property
    def show_ruler(self):
        from . import prefs
        return prefs.get_bool("showRuler",True)

    def apply_ruler_visibility(self):
        for name in ("h_ruler","v_ruler","ruler_corner"):
            view = getattr(self,name,None)
            if view is not None:
                view.setVisible(self.show_ruler)

    def toggle_ruler(self):
        from . import prefs
        prefs.set("showRuler",not self.show_ruler)
        self.apply_ruler_visibility()

    def redraw_rulers(self):
        for name in ("h_ruler","v_ruler"):
            view = getattr(self,name,None)
            if view is not None:
                view.update()

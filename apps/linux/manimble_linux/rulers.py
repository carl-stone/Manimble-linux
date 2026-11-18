"""Manim-unit rulers, sharing the canvas's top-left frame coordinate mapping."""
from __future__ import annotations

from enum import Enum
from math import ceil, floor

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .canvas import guide
from .style import Theme


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
        for size in (.1,.2,.25,.5,1,2,5,10,20):
            if size*self.pts_per_unit >= 44:
                return size,5 if size in (.1,.5,5,10,20) else 4
        return 50,5

    def _along(self, lo, hi):
        return QRectF(lo,0,hi-lo,self.height()) if self.horizontal else QRectF(0,lo,self.width(),hi-lo)

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
        font = QFont("DejaVu Sans Mono"); font.setPixelSize(9); p.setFont(font)
        first,last = floor(-center/ppu/spacing),ceil((length-center)/ppu/spacing)
        decimals = 0 if major >= 1 else 2 if major == .25 else 1
        for i in range(first,last+1):
            pos = center+i*spacing*ppu; is_major = i%minor == 0
            tick = thick if is_major else thick*(.42 if minor%2 == 0 and i%(minor//2) == 0 else .26)
            p.setPen(QPen(Theme.ink(.6 if is_major else .28),1))
            if self.horizontal:
                p.drawLine(QPointF(pos,thick-tick),QPointF(pos,thick))
            else:
                p.drawLine(QPointF(thick-tick,pos),QPointF(thick,pos))
            if is_major:
                value = i*spacing*(1 if self.horizontal else -1)
                if abs(value)<spacing/1000: value=0
                text = f"{value:.{decimals}f}"
                p.setPen(Theme.ink(.72))
                if self.horizontal:
                    p.drawText(QPointF(pos+3,10),text)
                else:
                    p.save(); p.translate(10,pos-3); p.rotate(-90); p.drawText(QPointF(),text); p.restore()
        for g in c.user_guides:
            vertical,pos = guide(g)
            if vertical != self.horizontal:
                continue
            pos = self.view_pos(pos); path = QPainterPath()
            if self.horizontal:
                path.moveTo(pos-3,thick); path.lineTo(pos+3,thick); path.lineTo(pos,thick-6)
            else:
                path.moveTo(thick,pos-3); path.lineTo(thick,pos+3); path.lineTo(thick-6,pos)
            path.closeSubpath(); p.fillPath(path,QColor("#40D9FF"))
        if self.cursor is not None:
            pos = self.view_pos(self.cursor.x() if self.horizontal else self.cursor.y())
            p.setPen(QPen(Theme.ink(.85),1))
            p.drawLine(QPointF(pos,0),QPointF(pos,thick)) if self.horizontal else p.drawLine(QPointF(0,pos),QPointF(thick,pos))
        p.setPen(QPen(Theme.ink(.14),1))
        p.drawLine(0,self.height()-1,self.width(),self.height()-1) if self.horizontal else p.drawLine(self.width()-1,0,self.width()-1,self.height())

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

"""Frame-pixel canvas geometry and a small QGraphicsScene painted with QPainter.

The engine owns geometry and source. Qt owns only transient selection/typing and
optimistic rasters; no editable Qt object is serialized as slide source.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from math import atan2, cos, hypot, pi, sin, sqrt
from pathlib import Path
import logging

from .models import Box, CHAR_KINDS, frame_box as object_frame_box, glyph_boxes, stop_box
from .darkmode import ColorView, DarkMode


@dataclass
class Override:
    dx: float = 0
    dy: float = 0
    scale: float = 1
    replacement: object = None                 # (QImage, width, height)


class Handle(IntEnum):
    nw = 0
    ne = 1
    sw = 2
    se = 3
    w = 4
    e = 5

    @property
    def is_side(self):
        return self in (Handle.w, Handle.e)


@dataclass
class LineAnchor:
    oid: int
    at: str
    x: float
    y: float


@dataclass
class TextAnchor:
    x: float
    y: float
    top: bool
    align: str


def xy(p):
    return (p.x(), p.y()) if hasattr(p, "x") else (p[0], p[1])


def utf16(s, scalars):
    return len(s[:max(0, scalars)].encode("utf-16-le", errors="surrogatepass")) // 2


def code_points(s, offset):
    """Clamp an offset within a surrogate pair to the preceding code point."""
    count = units = 0
    for c in s:
        size = 2 if ord(c) > 0xffff else 1
        if units + size > offset:
            break
        count += 1
        units += size
    return count


def simplify(points, tolerance=1.2):
    """Swift's RDP distance is to the infinite endpoint line, not its segment."""
    if len(points) <= 2:
        return list(points)
    ax, ay = xy(points[0]); bx, by = xy(points[-1])
    dx, dy = bx - ax, by - ay
    length = hypot(dx, dy)
    worst, at = 0, 0
    for i in range(1, len(points) - 1):
        x, y = xy(points[i])
        d = hypot(x - ax, y - ay) if length < 1e-9 else abs(dy * (x - ax) - dx * (y - ay)) / length
        if d > worst:
            worst, at = d, i
    if worst <= tolerance:
        return [points[0], points[-1]]
    return simplify(points[:at + 1], tolerance)[:-1] + simplify(points[at:], tolerance)


def field(v, name, default=None):
    if isinstance(v, dict):
        return v.get(name, v.get("box", default) if name == "rect" else default)
    return getattr(v, name, default)


def as_box(v):
    if isinstance(v, Box):
        return v
    if isinstance(v, dict):
        return Box(v["x0"], v["y0"], v["x1"], v["y1"])
    return Box.of(v)


def guide(g):
    return (g.get("vertical", False), g.get("pos", 0)) if isinstance(g, dict) else (g[0], g[1])


class CanvasGeometry:
    """Qt-independent geometry used by CanvasView and portability tests."""
    frame_pad = 0.1
    line_tools = {"line", "arrow", "darrow"}
    bullet = "• "
    simplify = staticmethod(simplify)
    utf16 = staticmethod(utf16)
    code_points = staticmethod(code_points)

    def __init__(self):
        self.stop = None
        self.frame_size = (1280., 720.)
        self.unit_px = 90.
        self.scene_objects = {}
        self.overrides = {}
        self.line_overrides = {}
        self.selection = None
        self.others = set()
        self.user_guides = []
        self.show_grid = False
        self._drag_group = {}
        self._drag_oid = None
        self._drag_base = (0, 0)
        self._inline = None

    @property
    def px_per_point(self):
        return 1 / self.effective_scale

    @property
    def effective_scale(self):
        return 1.

    @property
    def selected_ids(self):
        return ([] if self.selection is None else [self.selection]) + sorted(self.others - {self.selection})

    @property
    def selected_box(self):
        if self.selection is None:
            return None
        e = self.line_ends(self.selection)
        return Box(min(e[0], e[2]), min(e[1], e[3]), max(e[0], e[2]), max(e[1], e[3])) if e else self.current_box(self.selection)

    @property
    def dragging(self):
        return self._drag_oid

    def is_dragging(self, oid):
        return oid == self._drag_oid or oid in self._drag_group

    def raw_box(self, oid):
        e = self._inline
        if e is not None and e.oid == oid and e.cell is None:
            return self.text_editor_box
        b = stop_box(self.stop, oid)
        o = self.overrides.get(oid)
        if b is not None and o is not None:
            if o.replacement is not None:
                return self.replacement_box(oid, o, b, o.replacement)
            return Box(b.cx + o.dx - b.w * o.scale / 2, b.cy + o.dy - b.h * o.scale / 2,
                       b.cx + o.dx + b.w * o.scale / 2, b.cy + o.dy + b.h * o.scale / 2)
        return b

    def frame_box(self, oid):
        b = object_frame_box(self.scene_objects.get(str(oid), {}))
        o = self.overrides.get(oid)
        if b is not None and o is not None:
            return Box(b.cx + o.dx - b.w * o.scale / 2, b.cy + o.dy - b.h * o.scale / 2,
                       b.cx + o.dx + b.w * o.scale / 2, b.cy + o.dy + b.h * o.scale / 2)
        return b

    def replacement_box(self, oid, o, ink, rep):
        w, h = (field(rep, "w"), field(rep, "h")) if isinstance(rep, dict) else rep[1:3]
        w *= o.scale; h *= o.scale
        f = self.frame_box(oid)
        if f is not None:
            fmt = self.scene_objects.get(str(oid), {}).get("format") or {}
            pad = self.frame_pad * self.unit_px
            x = f.cx - w / 2 if fmt.get("align") == "center" else f.x1 - pad - w if fmt.get("align") == "right" else f.x0 + pad
            y = f.cy - h / 2 if fmt.get("valign") == "mid" else f.y0 + pad
            return Box(x, y, x + w, y + h)
        return Box(ink.cx + o.dx - w / 2, ink.cy + o.dy - h / 2, ink.cx + o.dx + w / 2, ink.cy + o.dy + h / 2)

    def current_box(self, oid):
        b = self.frame_box(oid) or self.raw_box(oid)
        if b is None:
            return None
        w, h = max(b.w, 10 * self.px_per_point), max(b.h, 10 * self.px_per_point)
        return Box(b.cx - w / 2, b.cy - h / 2, b.cx + w / 2, b.cy + h / 2)

    def objects(self, within=None):
        z = {sp["oid"] for sp in (self.stop or {}).get("sprites", [])}
        inside = []
        for v in (self.stop or {}).get("visible", []):
            oid = int(v[0]); b = self.raw_box(oid)
            if b and (within is None or (b.x0 >= within.x0 and b.x1 <= within.x1 and b.y0 >= within.y0 and b.y1 <= within.y1)):
                inside.append((oid, b))
        return [oid for oid, b in inside if oid in z or not any(
            other != oid and ob.x0 <= b.x0 and ob.x1 >= b.x1 and ob.y0 <= b.y0 and ob.y1 >= b.y1 for other, ob in inside)]

    def hit(self, p):
        x, y = xy(p)
        z = {}
        for sp in (self.stop or {}).get("sprites", []):
            z[sp["oid"]] = max(z.get(sp["oid"], -float("inf")), sp["z"])
        hits = [(int(v[0]), b) for v in (self.stop or {}).get("visible", [])
                if (b := self.current_box(int(v[0]))) is not None and b.contains(x, y)]
        def rank(pair):
            oid, b = pair
            return z.get(oid, max((z[other] for other, ob in hits if other in z and ob.area > b.area), default=-float("inf"))), -b.area
        return max(hits, key=rank)[0] if hits else None

    def handle_at(self, p, b, sides=False):
        x, y = xy(p); tol = 9 * self.px_per_point
        candidates = [(Handle.nw, b.x0, b.y0), (Handle.ne, b.x1, b.y0), (Handle.sw, b.x0, b.y1), (Handle.se, b.x1, b.y1)]
        if sides:
            candidates += [(Handle.w, b.x0, b.cy), (Handle.e, b.x1, b.cy)]
        hits = [(hypot(x - px, y - py), h) for h, px, py in candidates if abs(x - px) <= tol and abs(y - py) <= tol]
        return min(hits, key=lambda v: v[0])[1] if hits else None

    def line_end_at(self, p, ends):
        x, y = xy(p)
        reach = 10 * self.px_per_point
        return next((i for i in range(2) if hypot(x - ends[2*i], y - ends[2*i+1]) <= reach), None)

    def snap_delta(self, oid, b, dx, dy):
        if self.stop is None:
            return dx, dy, []
        fw, fh = self.frame_size
        xs, ys = [0, fw / 2, fw], [0, fh / 2, fh]
        for v in self.stop.get("visible", []):
            other = int(v[0]); ob = self.raw_box(other)
            if other != oid and other not in self._drag_group and ob is not None:
                xs += [ob.x0, ob.cx, ob.x1]; ys += [ob.y0, ob.cy, ob.y1]
        for g in self.user_guides:
            vertical, pos = guide(g)
            (xs if vertical else ys).append(pos)
        if self.show_grid and self.unit_px > 4:
            for targets, extent in ((xs, fw), (ys, fh)):
                k = 1
                while extent / 2 + k * self.unit_px <= extent + .5:
                    targets += [extent / 2 + k * self.unit_px, extent / 2 - k * self.unit_px]; k += 1
        def best(candidates, targets):
            hits = [(t - c, t) for c in candidates for t in targets if abs(t - c) <= 6 * self.px_per_point]
            return min(hits, key=lambda v: abs(v[0])) if hits else None
        bx, by = best([b.x0 + dx, b.cx + dx, b.x1 + dx], xs), best([b.y0 + dy, b.cy + dy, b.y1 + dy], ys)
        guides = []
        if bx:
            dx += bx[0]; guides.append((True, bx[1]))
        if by:
            dy += by[0]; guides.append((False, by[1]))
        return dx, dy, guides

    def line_ends(self, oid):
        v = self.line_overrides.get(oid) or ((self.stop or {}).get("lines") or {}).get(str(oid))
        return list(v[:4]) if v and len(v) >= 4 else None

    def anchor_points(self, oid):
        b = self.raw_box(oid)
        if b is None:
            return []
        left, right, top, bottom = b.x0, b.x1, b.y0, b.y1
        if self.scene_objects.get(str(oid), {}).get("value") in ("Circle", "Ellipse", "Dot"):
            k = (1 - sqrt(.5)) / 2
            left += k * b.w; right -= k * b.w; top += k * b.h; bottom -= k * b.h
        return [("UL", left, top), ("UP", b.cx, b.y0), ("UR", right, top), ("LEFT", b.x0, b.cy),
                ("ORIGIN", b.cx, b.cy), ("RIGHT", b.x1, b.cy), ("DL", left, bottom), ("DOWN", b.cx, b.y1), ("DR", right, bottom)]

    def line_anchor(self, near, excluding=None):
        x, y = xy(near); reach = 10 * self.px_per_point
        host = best = None
        for v in (self.stop or {}).get("visible", []):
            oid = int(v[0]); obj = self.scene_objects.get(str(oid)); b = self.raw_box(oid)
            if oid == excluding or not obj or obj.get("kind") == "line" or b is None:
                continue
            if not (b.x0 - 3 * reach <= x <= b.x1 + 3 * reach and b.y0 - 3 * reach <= y <= b.y1 + 3 * reach):
                continue
            if host is None or b.area < host[1]:
                host = oid, b.area
            for at, ax, ay in self.anchor_points(oid):
                distance = hypot(ax - x, ay - y)
                if distance <= reach and (best is None or distance < best[1]):
                    best = LineAnchor(oid, at, ax, ay), distance
        return (best[0].oid, best[0]) if best else (host[0] if host else None, None)

    @staticmethod
    def anchor_fields(ends):
        return {("startAnchor" if i == 0 else "endAnchor"): {"oid": a.oid, "at": a.at} for i, a in enumerate(ends) if a is not None}

    def follow_xform(self, oid, stop):
        hosts = self.scene_objects.get(str(oid), {}).get("follows") or []
        ln = (stop.get("lines") or {}).get(str(oid))
        if not ln or len(ln) < 4:
            return None
        live = [(b, o) for h in hosts if (o := self.overrides.get(h)) is not None and o.replacement is None and (b := stop_box(stop, h)) is not None]
        if not live:
            return None
        moved = []
        for x, y in ((ln[0], ln[1]), (ln[2], ln[3])):
            def gap(pair):
                b = pair[0]
                return hypot(max(0, b.x0 - x, x - b.x1), max(0, b.y0 - y, y - b.y1))
            b, o = min(live, key=gap)
            moved.append((b.cx + (x - b.cx) * o.scale + o.dx, b.cy + (y - b.cy) * o.scale + o.dy) if gap((b, o)) <= .6 * self.unit_px else (x, y))
        ax, ay, bx, by = ln[:4]; (cx, cy), (dx, dy) = moved
        vx, vy, wx, wy = bx - ax, by - ay, dx - cx, dy - cy
        n2 = vx * vx + vy * vy
        a, b = ((wx * vx + wy * vy) / n2, (wy * vx - wx * vy) / n2) if n2 >= 1e-9 else (1, 0)
        s = [a, b, -b, a, cx - a * ax + b * ay, cy - b * ax - a * ay]
        x = (stop.get("xforms") or {}).get(str(oid))
        if not x or len(x) < 6:
            return s
        return [s[0]*x[0]+s[2]*x[1], s[1]*x[0]+s[3]*x[1], s[0]*x[2]+s[2]*x[3], s[1]*x[2]+s[3]*x[3],
                s[0]*x[4]+s[2]*x[5]+s[4], s[1]*x[4]+s[3]*x[5]+s[5]]

    def group_mates(self, oid):
        group = self.scene_objects.get(str(oid), {}).get("group")
        if group is None:
            return [oid]
        return [oid] + [int(v[0]) for v in (self.stop or {}).get("visible", []) if int(v[0]) != oid and self.scene_objects.get(str(int(v[0])), {}).get("group") == group]

    def wrappable(self, oid):
        obj = self.scene_objects.get(str(oid), {})
        return obj.get("kind") == "text" and obj.get("textOk") is not False and (self._inline is None or self._inline.oid != oid)


# Pure geometry remains importable without Qt (e.g. the macOS portability checks).
try:
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QGradient, QImage, QLinearGradient, QPainter,
                              QPainterPath, QPalette, QPen, QRadialGradient, QTransform, QTextCursor, QTextFormat, QTextCharFormat, QTextBlockFormat)
    from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QTextEdit, QPushButton, QFrame
except ImportError:
    QGraphicsView = None


if QGraphicsView is not None:
    from .fonts import display_family
    from .icons import template_icon
    from .style import Theme, is_dark, watch_appearance
    from .tasks import when_done

    def _smoothstep(t):
        return t * t * (3 - 2 * t)


    @lru_cache(maxsize=2)
    def _stage_paints(dark):
        """CanvasView.swift's normalized stage and shadow gradients, in view units."""
        top, bottom = ((.205, .210, .255), (.085, .088, .112)) if dark else ((.955, .955, .963), (.900, .900, .912))
        base = QLinearGradient(0, 0, 0, 1)
        base.setCoordinateMode(QGradient.CoordinateMode.ObjectMode)
        base.setColorAt(0, QColor.fromRgbF(*top))
        base.setColorAt(1, QColor.fromRgbF(*bottom))

        vignette = QRadialGradient(.5, .575, .85)
        strength = .38 if dark else .05
        for i in range(17):
            t = i / 16
            vignette.setColorAt((.15 + .70 * t) / .85, QColor.fromRgbF(0, 0, 0, strength * _smoothstep(t)))

        alpha = .62 if dark else .16
        fade = [(i / 16, QColor.fromRgbF(0, 0, 0, alpha * (1 - _smoothstep(i / 16)) ** 2)) for i in range(17)]
        shadow = []
        for row in range(3):
            for col in range(3):
                x, y = int(col == 0), int(row == 0)
                if row == col == 1:
                    shadow.append(QBrush(fade[0][1]))
                    continue
                if row == 1:
                    grad = QLinearGradient(x, 0, 1 - x, 0)
                elif col == 1:
                    grad = QLinearGradient(0, y, 0, 1 - y)
                else:
                    grad = QRadialGradient(x, y, 1)
                grad.setCoordinateMode(QGradient.CoordinateMode.ObjectMode)
                grad.setStops(fade)
                shadow.append(QBrush(grad))
        outline = QPen(QColor.fromRgbF(1, 1, 1, .16) if dark else QColor.fromRgbF(0, 0, 0, .10), 1)
        return QBrush(base), QBrush(vignette), shadow, outline

    @dataclass
    class InlineEdit:
        oid: int
        view: object
        center: object
        font_px: float
        family: str
        bold: bool
        italic: bool
        original: str
        done: object
        changed: object
        touched: set
        cell: object = None
        placeholder: object = None
        anchor: object = None
        align: str = "left"
        line_spacing: float | None = None


    class InlineTextView(QTextEdit):
        def __init__(self, canvas):
            super().__init__(canvas.viewport())
            self.canvas = canvas
            self.script_mode = None
            self.bullets = False
            self.on_delete_empty = None
            self.on_tab = None
            self.setAcceptRichText(False)
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setStyleSheet("QTextEdit { background: transparent; border: none; padding: 0; }")
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            self.document().setDocumentMargin(0)

        def keyPressEvent(self, ev):
            key = ev.key()
            # The previous editor may remain visible while its queued edit renders.
            if self.isReadOnly():
                ev.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.canvas.finish_text_edit(False); return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    self.canvas.finish_text_edit(True)
                else:
                    from .formatbar import TextTransform
                    cursor = self.textCursor()
                    prefix = TextTransform.newline_prefix(cursor.block().text()[:cursor.positionInBlock()], body=self.bullets)
                    super().keyPressEvent(ev)
                    if prefix:
                        self.insertPlainText(prefix)
                return
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.on_delete_empty and self.toPlainText() in ("", CanvasGeometry.bullet):
                self.on_delete_empty(); return
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                if self.on_tab:
                    self.on_tab()
                else:
                    from .formatbar import TextTransform
                    cursor = self.textCursor()
                    if cursor.hasSelection() or key == Qt.Key.Key_Backtab:
                        start, end = TextTransform.line_range(self.toPlainText(), (cursor.selectionStart(), cursor.selectionEnd()-cursor.selectionStart()))
                        blocks = [self.document().findBlockByNumber(i) for i in range(start,end+1)]
                        cursor.beginEditBlock()
                        for block in reversed(blocks):
                            edit = QTextCursor(block)
                            if key == Qt.Key.Key_Backtab:
                                count = min(4,len(block.text())-len(block.text().lstrip(" ")))
                                edit.setPosition(block.position()+count,QTextCursor.MoveMode.KeepAnchor)
                                edit.removeSelectedText()
                            else:
                                edit.insertText("    ")
                        cursor.endEditBlock()
                    else:
                        self.insertPlainText("    ")
                return
            if self.script_mode is not None and ev.text() and not ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                from .formatbar import TextTransform
                self.insertPlainText(TextTransform.raise_text(ev.text(), superscript=self.script_mode)); return
            super().keyPressEvent(ev)

        def focusOutEvent(self, ev):
            super().focusOutEvent(ev)
            # Formatting buttons may temporarily take focus. A click on the canvas
            # commits explicitly; tabbing to another editing area commits here.
            if ev.reason() not in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
                QTimer.singleShot(0, self._blur)
        def _blur(self):
            if self.canvas._inline and self.canvas._inline.view is self and not self.hasFocus():
                self.canvas.finish_text_edit(True)


    class _CanvasItem(QGraphicsItem):
        def __init__(self, canvas):
            super().__init__()
            self.canvas = canvas
            self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        def boundingRect(self):
            # sceneRect() infers its bounds from items until explicitly set;
            # asking it here would recurse during addItem().
            return QRectF(self.canvas.viewport().rect())

        def paint(self, painter, option, widget=None):
            self.canvas._paint(painter)


    class CanvasView(QGraphicsView, CanvasGeometry):
        _watched = {"stop", "frame_size", "unit_px", "scene_objects", "placeholders", "optimistic", "selection", "others",
                    "overrides", "line_overrides", "ghost", "color_view", "dark_images", "show_grid", "user_guides",
                    "draw_tool", "pen_color", "pen_width", "pending_strokes", "zoom", "pan", "show_objects", "find_marks", "hover"}
        _geometry = {"stop", "frame_size", "unit_px", "selection", "overrides", "user_guides", "zoom", "pan"}

        def __init__(self, parent=None):
            QGraphicsView.__init__(self, parent)
            self._ready = False
            CanvasGeometry.__init__(self)
            for name in ("select", "move", "resize", "wrap", "endpoints", "edit", "nudge", "delete", "many_edits", "draw", "pen",
                         "placeholder", "placeholder_delete", "context_menu", "zoom_change", "geometry_change", "cursor_move",
                         "inline_selection_style", "guides_change", "draw_tool_change", "icon_drop", "corner_action", "corner_edit", "cell_tab"):
                setattr(self, "on_" + name, None)
            self.has_corner_action = None
            self.placeholders = []
            self.optimistic = False
            self.ghost = None
            self.color_view = ColorView.normal
            self.dark_images = True
            self.snap_enabled = True
            self.draw_tool = None
            self.pen_color = QColor("#FF3B30")
            self.pen_width = 6.
            self.pending_strokes = []
            self.zoom = None
            self.pan = QPointF()
            self.show_objects = False
            self.find_marks = None
            self.hover = None
            self.feedback_layer = None
            self.script_layer = None
            self.committed_styles = None
            self._keep_others = False
            self._cache = OrderedDict()
            self._cache_bytes = 0
            self._cache_limit = 192 * 1024 * 1024
            self._failed_textures = set()
            self._guides = []
            self._drag_guide = None
            self._draw_start = None
            self._draw_band = None
            self._draw_anchors = [None, None]
            self._pen_points = []
            self._drag_start = None
            self._drag_handle = None
            self._drag_box = None
            self._drag_end = None
            self._wrap_band = None
            self._marquee_start = None
            self._marquee = None
            self._marquee_adds = False
            self._anchor_host = None
            self._snap_anchor = None
            self._drag_anchor = None
            self._pan_start = None
            self._space_pan = False
            self._layout_busy = False
            self._corner_oid = None
            self._corner_button = QPushButton(self.viewport())
            self._corner_button.setAccessibleName("Run again")
            self._corner_button.setIcon(template_icon("play.fill", tint=QColor("white")))
            self._python_button = QPushButton("py", self.viewport())
            self._python_button.setAccessibleName("Open Python code")
            self._python_button.setFont(Theme.font(11, QFont.Weight.Black))
            for button, cb in ((self._corner_button, "on_corner_action"), (self._python_button, "on_corner_edit")):
                button.setFixedSize(26, 26)
                button.setStyleSheet("QPushButton {border: none; border-radius: 13px; color: white; background: rgba(255, 108, 12, 235);}")
                button.clicked.connect(lambda checked=False, key=cb: self._emit(key, self._corner_oid) if self._corner_oid is not None else None)
                button.hide()
            self._corner_button.setToolTip("Run the plot's code again and show what it draws now")
            self._python_button.setToolTip("Open the plot's Python code")
            self.setScene(QGraphicsScene(self))
            self._item = _CanvasItem(self)
            self.scene().addItem(self._item)
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
            self.setMouseTracking(True)
            self.setAcceptDrops(True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._ready = True
            watch_appearance(self._repaint_stage)

        def _repaint_stage(self):
            self.viewport().update()

        def __setattr__(self, name, value):
            ready = getattr(self, "_ready", False)
            if ready and name == "selection" and value != self.selection:
                if self._inline is not None and value != self.editing_text:
                    self.finish_text_edit(True)
                if not self._keep_others:
                    object.__setattr__(self, "others", set())
            if name == "frame_size" and hasattr(value, "width"):
                value = (value.width(), value.height())
            if name == "color_view":
                value = ColorView(value)
            object.__setattr__(self, name, value)
            if ready and name in self._watched:
                if name == "zoom" and value is None:
                    object.__setattr__(self, "pan", QPointF())
                if name == "stop" and self._inline is not None and self._inline.placeholder is None and stop_box(value, self._inline.oid) is None:
                    self.finish_text_edit(True)
                self._changed()
                if name in self._geometry:
                    self._emit("on_geometry_change")
                if name == "zoom":
                    self._emit("on_zoom_change")
                if name == "draw_tool":
                    if value is None:
                        self._anchor_host = self._snap_anchor = None
                        self.viewport().unsetCursor()
                    else:
                        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
                    self._emit("on_draw_tool_change")

        def _emit(self, name, *args):
            cb = getattr(self, name, None)
            return cb(*args) if cb is not None else None

        def _changed(self):
            if not getattr(self, "_ready", False):
                return
            self.layout_text_editor()
            self._layout_corner_button()
            self.viewport().update()
            for layer in (self.feedback_layer, self.script_layer):
                if layer is not None:
                    layer.setGeometry(self.viewport().rect())
                    if hasattr(layer, "relayout"):
                        layer.relayout()
                    layer.update()

        @property
        def fit_rect(self):
            width, height = self.frame_size
            available = self.viewport().rect().adjusted(24, 24, -24, -24)
            k = max(.001, min(available.width() / width, available.height() / height))
            return QRectF((self.viewport().width() - width*k)/2, (self.viewport().height() - height*k)/2, width*k, height*k)

        @property
        def fit_scale(self):
            return self.fit_rect.width() / self.frame_size[0]

        @property
        def effective_scale(self):
            return self.fit_scale if self.zoom is None else self.zoom

        @property
        def slide_rect(self):
            if self.zoom is None:
                return self.fit_rect
            width, height = self.frame_size
            px, py = xy(self.pan)
            return QRectF((self.viewport().width() - width*self.zoom)/2 + px,
                          (self.viewport().height() - height*self.zoom)/2 + py, width*self.zoom, height*self.zoom)

        def view_rect(self, b):
            b = as_box(b); r = self.slide_rect; s = self.effective_scale
            return QRectF(r.x()+b.x0*s, r.y()+b.y0*s, b.w*s, b.h*s)

        def frame_point(self, p=None, *, window=None):
            if window is not None:
                p = QPointF(self.viewport().mapFrom(self.window(), window.toPoint() if hasattr(window, "toPoint") else window))
            x, y = xy(p); r = self.slide_rect; s = self.effective_scale
            return QPointF((x-r.x())/s, (y-r.y())/s)

        def zoom_by(self, factor):
            self.zoom = min(8., max(.1, self.effective_scale * factor))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if getattr(self, "_ready", False):
                self._item.prepareGeometryChange()
                self.setSceneRect(QRectF(self.viewport().rect()))
                self._changed()
                self._emit("on_geometry_change")

        @staticmethod
        def texture_key(path):
            name = Path(path).name
            return name if name.startswith("sp_") else str(path)

        def _remember(self, key, image):
            if key in self._cache:
                self._cache_bytes -= self._cache.pop(key).sizeInBytes()
            self._cache[key] = image
            self._cache_bytes += image.sizeInBytes()
            while self._cache_bytes > self._cache_limit and len(self._cache) > 1:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= old.sizeInBytes()
            return image

        def texture(self, path):
            if not path:
                return None
            key = (self.texture_key(path), ColorView.normal)
            image = self._cache.get(key)
            if image is not None:
                self._cache.move_to_end(key)
                return image
            image = QImage(str(path))
            if image.isNull():
                if str(path) not in self._failed_textures:
                    self._failed_textures.add(str(path))
                    logging.error("Canvas image could not be read: %s", path)
                return None
            return self._remember(key, image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))

        def _mapped(self, path, image, map_color=True):
            mode = self.color_view if map_color else ColorView.normal
            if mode == ColorView.normal:
                return image
            # In-memory replacements use QImage's content generation key.
            key = (self.texture_key(path) if path else ("image", image.cacheKey()), mode)
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return self._remember(key, DarkMode.apply(image, mode))

        def drop_textures(self):
            for key in list(self._cache):
                if not isinstance(key[0], str) or not key[0].startswith("sp_"):
                    self._cache_bytes -= self._cache.pop(key).sizeInBytes()
            self._failed_textures.clear()
            self._changed()

        def renumber(self, selection, to):
            if self._inline is not None and self._inline.placeholder is None:
                oid = to(self._inline.oid)
                if oid is not None:
                    self._inline.oid = oid
                else:
                    e = self._inline
                    self.finish_text_edit(False)
                    # A pending replacement may defer the normal release callback.
                    # A vanished object cannot own a live or read-only overlay.
                    if self._inline is e:
                        self._inline = None
                        e.view.hide()
                        e.view.deleteLater()
            self.others = {n for oid in self.others if (n := to(oid)) is not None}
            self.overrides = {n: o for oid, o in self.overrides.items() if (n := to(oid)) is not None}
            self.line_overrides = {n: o for oid, o in self.line_overrides.items() if (n := to(oid)) is not None}
            self._keep_others = True
            self.selection = selection
            self._keep_others = False

        def settle(self, oid, shown_dx, shown_dy):
            o = self.overrides.get(oid)
            if o is None:
                return
            o.dx -= shown_dx; o.dy -= shown_dy
            if self._drag_oid == oid:
                self._drag_base = (self._drag_base[0]-shown_dx, self._drag_base[1]-shown_dy)
            if oid in self._drag_group:
                x, y = self._drag_group[oid]; self._drag_group[oid] = (x-shown_dx, y-shown_dy)
            self._changed()

        def select_group(self, ids):
            ids = list(dict.fromkeys(ids))
            self._keep_others = True
            self.selection = ids[0] if ids else None
            self.others = set(ids[1:])
            self._keep_others = False
            self._emit("on_select", self.selection)

        def select_all_objects(self):
            self.select_group(self.objects())

        def select_with_group(self, oid):
            self.select_group(self.group_mates(oid))

        def _together(self, count, label, body):
            if count > 1 and self.on_many_edits:
                self.on_many_edits(label, body)
            else:
                body()

        def delete_selection(self):
            ids = self.selected_ids
            self._together(len(ids), f"delete {len(ids)} objects", lambda: [self._emit("on_delete", oid) for oid in sorted(ids, reverse=True)])
            self.others = set()

        def attach_feedback_layer(self):
            if self.feedback_layer is None:
                from .feedback import FeedbackLayer
                self.feedback_layer = FeedbackLayer(self)
                self.feedback_layer.setGeometry(self.viewport().rect())
                self.feedback_layer.show()
            return self.feedback_layer

        def _layout_corner_button(self):
            oid = self.hover
            b = self.current_box(oid) if oid is not None else None
            show = b is not None and self._inline is None and self.draw_tool is None and self.has_corner_action and self.has_corner_action(oid)
            self._corner_oid = oid if show else None
            if show:
                r = self.view_rect(b)
                self._corner_button.move(round(r.right()-32), round(r.top()+6))
                self._python_button.move(round(r.right()-64), round(r.top()+6))
                self._corner_button.setVisible(r.width() >= 38 and r.height() >= 38)
                self._python_button.setVisible(r.width() >= 70 and r.height() >= 38)
            else:
                self._corner_button.hide(); self._python_button.hide()

        def find_marks_changed(self):
            self._changed()

        def begin_guide_drag(self, vertical, pos):
            self._drag_guide = len(self.user_guides)
            self.user_guides = [*self.user_guides, (vertical, pos)]

        def continue_guide_drag(self, index):
            if 0 <= index < len(self.user_guides):
                self._drag_guide = index
                self._changed()

        def update_guide_drag(self, pos):
            if self._drag_guide is not None:
                guides = list(self.user_guides)
                guides[self._drag_guide] = (guide(guides[self._drag_guide])[0], pos)
                self.user_guides = guides

        def end_guide_drag(self):
            if self._drag_guide is None:
                return
            guides = list(self.user_guides)
            vertical, pos = guide(guides[self._drag_guide])
            if pos < 0 or pos > self.frame_size[0 if vertical else 1]:
                guides.pop(self._drag_guide)
            self._drag_guide = None
            self.user_guides = guides
            self._emit("on_guides_change")

        def guide_at(self, p):
            x, y = xy(p)
            return next((i for i, g in enumerate(self.user_guides) if abs((x if guide(g)[0] else y)-guide(g)[1]) <= 5*self.px_per_point), None)

        def _paint_stage(self, p, r):
            base, vignette, shadow, outline = _stage_paints(is_dark())
            view = self.sceneRect()
            p.fillRect(view, base)
            p.save()
            p.scale(view.width(), view.height() / 1.15)
            p.fillRect(QRectF(0, 0, 1, 1.15), vignette)
            p.restore()

            p.save()
            if self.stop is not None:
                # The backdrop and editor depth are not part of the rendered slide.
                outside = QPainterPath()
                outside.addRect(view)
                outside.addRect(r)
                p.setClipPath(outside, Qt.ClipOperation.IntersectClip)
            drop = r.translated(0, 14)
            margin = 46
            xs = (drop.left() - margin, drop.left(), drop.right(), drop.right() + margin)
            ys = (drop.top() - margin, drop.top(), drop.bottom(), drop.bottom() + margin)
            for i, brush in enumerate(shadow):
                row, col = divmod(i, 3)
                p.fillRect(QRectF(xs[col], ys[row], xs[col + 1] - xs[col], ys[row + 1] - ys[row]), brush)
            p.restore()
            p.setPen(outline)
            p.drawRect(r.adjusted(-.5, -.5, .5, .5))

        def _paint(self, p):
            r = self.slide_rect
            self._paint_stage(p, r)
            p.save()
            p.translate(r.topLeft()); p.scale(self.effective_scale, self.effective_scale)
            bounds = QRectF(0, 0, *self.frame_size)
            p.save(); p.setClipRect(bounds)
            self._paint_content(p, background=True)
            p.restore()
            # Engine rasters may extend onto the pasteboard; exact-frame pixels may not.
            outside = QPainterPath(); outside.addRect(QRectF(-r.x()/self.effective_scale, -r.y()/self.effective_scale,
                                                           self.viewport().width()/self.effective_scale, self.viewport().height()/self.effective_scale))
            inside = QPainterPath(); inside.addRect(bounds)
            p.save(); p.setClipPath(outside.subtracted(inside))
            self._paint_content(p, background=False)
            p.restore()
            self._paint_overlays(p)
            p.restore()

        def _paint_content(self, p, background=True):
            stop = self.stop
            if stop is None:
                return
            fw, fh = self.frame_size
            idle = not self.overrides and not self.line_overrides and self.color_view == ColorView.normal and self._inline is None and not self.optimistic
            if background and idle:
                image = self.texture(stop.get("png"))
                if image is not None:
                    p.drawImage(QRectF(0, 0, fw, fh), image)
            else:
                if background:
                    path = stop.get("bg"); image = self.texture(path)
                    if image is not None:
                        p.drawImage(QRectF(0, 0, fw, fh), self._mapped(path, image))
                for sp in sorted(stop.get("sprites", []), key=lambda s: s["z"]):
                    oid = sp["oid"]; e = self._inline; o = self.overrides.get(oid)
                    if oid in self.line_overrides or (e is not None and oid == e.oid and e.cell is None):
                        continue
                    map_color = self.dark_images or self.scene_objects.get(str(oid), {}).get("kind") != "image"
                    if o is not None and o.replacement is not None:
                        rep = o.replacement
                        image = rep["tex"] if isinstance(rep, dict) else rep[0]
                        b = stop_box(stop, oid)
                        if b is not None:
                            b = self.replacement_box(oid, o, b, rep)
                            p.drawImage(QRectF(b.x0, b.y0, b.w, b.h), self._mapped(None, image))
                        continue
                    path = sp["file"]; image = self.texture(path)
                    if image is None:
                        continue
                    image = self._mapped(path, image, map_color)
                    p.save()
                    if e is not None and oid == e.oid and e.cell is not None:
                        whole = QPainterPath(); whole.addRect(QRectF(-1e6, -1e6, 2e6, 2e6))
                        hole = QPainterPath(); b = e.cell[2]; hole.addRect(QRectF(b.x0, b.y0, b.w, b.h))
                        p.setClipPath(whole.subtracted(hole), Qt.ClipOperation.IntersectClip)
                    x, y, w, h = sp["x"], sp["y"], sp["w"], sp["h"]
                    if o is not None:
                        b = stop_box(stop, oid) or Box(x, y, x+w, y+h)
                        x = b.cx+o.dx+(x-b.cx)*o.scale; y = b.cy+o.dy+(y-b.cy)*o.scale
                        w *= o.scale; h *= o.scale
                    else:
                        xf = self.follow_xform(oid, stop) or (stop.get("xforms") or {}).get(str(oid))
                        if xf and len(xf) >= 6:
                            p.setTransform(QTransform(*xf[:6]), True)
                    p.drawImage(QRectF(x, y, w, h), image)
                    p.restore()
            if self.ghost is not None:
                g = self.ghost
                image, cx, cy, w, h = (g["tex"], g["cx"], g["cy"], g["w"], g["h"]) if isinstance(g, dict) else g
                p.drawImage(QRectF(cx-w/2, cy-h/2, w, h), self._mapped(None, image))
            for oid, ends in self.line_overrides.items():
                color = QColor(self.scene_objects.get(str(oid), {}).get("color") or "#FFFFFF")
                color = DarkMode.map(color, m=self.color_view)
                stroke = ((stop.get("lines") or {}).get(str(oid)) or [0, 0, 0, 0, 3])
                p.setPen(QPen(color, max(2., stroke[4] if len(stroke) > 4 else 3), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(*ends[:2]), QPointF(*ends[2:4]))

        def _paint_overlays(self, p):
            if self.stop is None:
                return
            k = self.px_per_point; fw, fh = self.frame_size
            accent = Theme.accent
            faint = QColor(accent); faint.setAlphaF(.35)
            glow = QColor(accent); glow.setAlphaF(.95)
            white = QColor(Qt.GlobalColor.white)
            th = max(1., 1.4*k)

            def outline(b, color=accent, width=th):
                if b is None:
                    return
                # Keep all four edges inside the reference box.
                tw, lh = min(width, b.w), min(width, b.h)
                p.fillRect(QRectF(b.x0, b.y0, b.w, lh), color)
                p.fillRect(QRectF(b.x0, b.y1-lh, b.w, lh), color)
                p.fillRect(QRectF(b.x0, b.y0, tw, b.h), color)
                p.fillRect(QRectF(b.x1-tw, b.y0, tw, b.h), color)

            def rule(vertical, pos, color, width):
                rect = QRectF(pos-width/2, 0, width, fh) if vertical else QRectF(0, pos-width/2, fw, width)
                p.fillRect(rect, color)

            def disc(x, y, radius, fill, ring, fraction):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ring)
                p.drawEllipse(QPointF(x,y),radius,radius)
                p.setBrush(fill)
                inner = radius*(1-fraction)
                p.drawEllipse(QPointF(x,y),inner,inner)

            p.setBrush(Qt.BrushStyle.NoBrush)
            if self.show_grid and self.unit_px > 4:
                axis = QColor(accent); axis.setAlphaF(.55)
                minor = QColor.fromRgbF(1., 1., 1., .13)
                width = max(1., .8*k)
                for vertical, extent in ((True, fw), (False, fh)):
                    n = int(extent/self.unit_px/2)+1
                    for i in range(-n, n+1):
                        pos = extent/2+i*self.unit_px
                        if 0 <= pos <= extent:
                            rule(vertical, pos, axis if i == 0 else minor, width)
            for i, g in enumerate(self.user_guides):
                vertical, pos = guide(g); active = i == self._drag_guide
                rule(vertical, pos, QColor.fromRgbF(.25, .85, 1., 1. if active else .8), 1.5*th if active else th)
            for vertical, pos in self._guides:
                rule(vertical, pos, glow, th)
            if self.pending_strokes or self._pen_points:
                color = DarkMode.map(QColor(self.pen_color), m=self.color_view)
                width = max(1.5, self.pen_width*.01*self.unit_px)
                p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                for points in (*self.pending_strokes, self._pen_points):
                    if not points:
                        continue
                    start = QPointF(*xy(points[0]))
                    if len(points) == 1:
                        p.drawPoint(start)
                        continue
                    path = QPainterPath(start)
                    for i in range(1, len(points)):
                        path.lineTo(QPointF(*xy(points[i])))
                    p.drawPath(path)
            if self._draw_band:
                x0, y0, x1, y1 = self._draw_band
                if self.draw_tool in self.line_tools:
                    p.setPen(QPen(accent, max(2., 3*k), Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                    p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
                else:
                    outline(Box(min(x0,x1), min(y0,y1), max(x0,x1), max(y0,y1)), width=1.4*th)
            if self._anchor_host is not None:
                for at, x, y in self.anchor_points(self._anchor_host):
                    on = self._snap_anchor is not None and self._snap_anchor.oid == self._anchor_host and self._snap_anchor.at == at
                    disc(x, y, (5.5 if on else 3.5)*k, accent if on else white, white if on else accent, .35)
            for ph in self.placeholders:
                if self._inline is not None and self._inline.placeholder == field(ph, "line"):
                    continue
                b = as_box(field(ph, "rect")); light = self.shown_light(b)
                grey = .55 if light else .6
                border = QColor.fromRgbF(grey, grey, grey, .9)
                dash, interval, width = 5*k, 8.5*k, max(1., 1.2*k)
                x = b.x0
                while x < b.x1:
                    length = min(dash, b.x1-x)
                    p.fillRect(QRectF(x,b.y0,length,width), border)
                    p.fillRect(QRectF(x,b.y1-width,length,width), border)
                    x += interval
                y = b.y0
                while y < b.y1:
                    length = min(dash, b.y1-y)
                    p.fillRect(QRectF(b.x0,y,width,length), border)
                    p.fillRect(QRectF(b.x1-width,y,width,length), border)
                    y += interval
                # Prompt uses the UI face but its size comes from the Manim layout.
                font = Theme.font(1); font.setPixelSize(max(1, round(field(ph,"size",24)/72*self.unit_px)))
                grey = .45 if light else .62
                p.setPen(QColor.fromRgbF(grey, grey, grey))
                p.setFont(font)
                anchor = self.placeholder_anchor(ph)
                metrics = QFontMetricsF(font); height = metrics.height()
                p.drawText(QPointF(anchor.x, anchor.y + metrics.ascent() - (0 if anchor.top else height/2)), field(ph,"prompt","Click to add text"))
            outline(self._wrap_band, width=1.4*th)
            if self.ghost:
                g = self.ghost
                cx, cy, w, h = (g["cx"],g["cy"],g["w"],g["h"]) if isinstance(g,dict) else g[1:]
                outline(Box(cx-w/2,cy-h/2,cx+w/2,cy+h/2), width=1.6*th)
            marks = self.find_marks() if callable(self.find_marks) else self.find_marks or []
            for mark in marks:
                oid = field(mark,"oid")
                if self._inline is not None and oid == self._inline.oid:
                    continue
                ranges = field(mark,"glyphs")
                boxes = glyph_boxes(self.stop, oid, *ranges) if ranges and oid not in self.overrides else None
                exact = boxes is not None
                boxes = boxes or ([b] if (b := self.current_box(oid)) is not None else [])
                for b in boxes:
                    pad = max(1., b.h*.12) if exact else 0
                    rect = QRectF(b.x0-pad,b.y0-pad,b.w+2*pad,b.h+2*pad)
                    current = field(mark,"current",False)
                    p.fillRect(rect, QColor(255,158,10,107) if current else QColor(255,214,25,77))
                    if current:
                        outline(Box(rect.left(),rect.top(),rect.right(),rect.bottom()), QColor.fromRgbF(.96, .5, 0., .95))
            selected = set(self.selected_ids)
            if self.show_objects:
                for v in self.stop.get("visible", []):
                    oid = int(v[0])
                    if oid not in selected:
                        outline(self.current_box(oid), glow if oid == self.hover else faint, th*(1.4 if oid == self.hover else .7))
            elif self.hover is not None and self.hover not in selected:
                outline(self.current_box(self.hover), glow, 1.4*th)
            for oid in selected:
                outline(self.current_box(oid), width=1.6*th)
            if self.selection is not None and (b := self.current_box(self.selection)) is not None:
                ends = self.line_ends(self.selection)
                points = [(ends[0], ends[1]), (ends[2], ends[3])] if ends else [(b.x0,b.y0),(b.x1,b.y0),(b.x0,b.y1),(b.x1,b.y1)]
                if self.wrappable(self.selection):
                    side = self._wrap_band or b
                    points += [(side.x0,b.cy),(side.x1,b.cy)]
                for x, y in points:
                    disc(x, y, 6.5*k, white, accent, .28)
            if self._marquee is not None:
                b = self._marquee
                fill = QColor(accent); fill.setAlphaF(.12)
                edge = QColor(accent); edge.setAlphaF(.9)
                p.fillRect(QRectF(b.x0,b.y0,b.w,b.h), fill)
                outline(b, edge, max(1., k))

        def snapshot(self):
            image = QImage(round(self.frame_size[0]), round(self.frame_size[1]), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHints(self.renderHints())
            self._paint_content(painter)
            painter.end()
            return image

        def snapshot_view(self):
            return self.grab().toImage()

        def backdrop_color(self, b):
            image = self.texture((self.stop or {}).get("bg"))
            if image is None:
                return None
            return image.pixelColor(min(image.width()-1,max(0,int(b.cx*image.width()/self.frame_size[0]))),
                                    min(image.height()-1,max(0,int(b.cy*image.height()/self.frame_size[1]))))

        @staticmethod
        def luma(color):
            return .2126*color.redF()+.7152*color.greenF()+.0722*color.blueF()

        def backdrop_is_light(self, b):
            color = self.backdrop_color(b)
            return color is not None and self.luma(color) > .5

        def shown_light(self, b):
            color = self.backdrop_color(b)
            return color is not None and self.luma(DarkMode.map(color, m=self.color_view)) > .5

        @property
        def shown_background(self):
            color = self.backdrop_color(Box(0,0,*self.frame_size))
            return DarkMode.map(color, m=self.color_view) if color is not None else None

        def mousePressEvent(self, ev):
            p = self.frame_point(ev.position())
            if ev.button() == Qt.MouseButton.MiddleButton or (self._space_pan and ev.button() == Qt.MouseButton.LeftButton):
                if self.zoom is None:
                    self.zoom = self.fit_scale
                self._pan_start = (ev.position(), QPointF(*xy(self.pan)))
                return
            if ev.button() != Qt.MouseButton.LeftButton:
                return super().mousePressEvent(ev)
            if self._inline is not None:
                self.finish_text_edit(True)
            self.setFocus()
            control = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if self.draw_tool == "pen":
                self._pen_points = [p]; self._changed(); return
            if self.draw_tool is not None:
                self._draw_anchors = [None,None]
                if self.draw_tool in self.line_tools and not control:
                    _, anchor = self.line_anchor(p)
                    if anchor:
                        p = QPointF(anchor.x,anchor.y); self._draw_anchors[0] = anchor
                self._draw_start = p
                self._draw_band = (*xy(p),*xy(p)); self._changed(); return
            i = self.guide_at(p)
            if i is not None:
                self.continue_guide_drag(i); return
            s = self.selection
            if s is not None:
                ends = self.line_ends(s); box = self.current_box(s)
                if ends:
                    if (end := self.line_end_at(p, ends)) is not None:
                        self._drag_end = end; self._drag_oid = s; self._drag_start = p
                        self.line_overrides[s] = ends; self._drag_anchor = None; return
                elif box is not None:
                    h = self.handle_at(p,box,self.wrappable(s))
                    if h is not None:
                        self._drag_start = p; self._drag_oid = s; self._drag_handle = h; self._drag_box = box
                        if h.is_side:
                            self._wrap_band = box
                        return
            oid = self.hit(p)
            shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if oid is None:
                for ph in self.placeholders:
                    if as_box(field(ph,"rect")).contains(*xy(p)):
                        self._emit("on_placeholder",ph); return
                if not shift:
                    self.select_group([])
                self._marquee_start = p; self._marquee_adds = shift and self.selection is not None
                return
            if shift:
                mates = self.group_mates(oid)
                self.select_group([i for i in self.selected_ids if i not in mates] if oid in self.selected_ids else mates+self.selected_ids)
                return
            if oid not in self.selected_ids:
                self.select_with_group(oid)
            self._drag_start = p; self._drag_oid = oid; self._drag_handle = None; self._drag_box = self.current_box(oid)
            o = self.overrides.get(oid,Override()); self._drag_base = (o.dx,o.dy)
            self._drag_group = {i:(self.overrides.get(i,Override()).dx,self.overrides.get(i,Override()).dy) for i in self.selected_ids if i != oid}

        def mouseDoubleClickEvent(self, ev):
            if ev.button() != Qt.MouseButton.LeftButton:
                return super().mouseDoubleClickEvent(ev)
            if self.draw_tool is not None or self._space_pan:
                self.mousePressEvent(ev)
                return
            oid = self.hit(self.frame_point(ev.position()))
            if oid is not None:
                self.select_group([oid]); self._emit("on_edit",oid,ev.position())

        @staticmethod
        def _angle_snap(origin, point):
            ox,oy = xy(origin); x,y = xy(point)
            angle = round(atan2(y-oy,x-ox)/(pi/12))*(pi/12); length = hypot(x-ox,y-oy)
            return QPointF(ox+length*cos(angle),oy+length*sin(angle))

        def mouseMoveEvent(self, ev):
            p = self.frame_point(ev.position()); self._emit("on_cursor_move",p)
            if self._pan_start is not None:
                start, base = self._pan_start
                self.pan = base+ev.position()-start; return
            shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            control = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if self._pen_points and self.draw_tool == "pen":
                if hypot(p.x()-self._pen_points[-1].x(),p.y()-self._pen_points[-1].y()) >= 1.5:
                    self._pen_points.append(p); self._changed()
                return
            if self._draw_start is not None and self.draw_tool is not None:
                point = p
                if shift:
                    if self.draw_tool in self.line_tools:
                        point = self._angle_snap(self._draw_start,p)
                    else:
                        dx,dy = p.x()-self._draw_start.x(),p.y()-self._draw_start.y()
                        side = max(abs(dx),abs(dy))
                        point = self._draw_start+QPointF(side if dx >= 0 else -side,side if dy >= 0 else -side)
                if self.draw_tool in self.line_tools:
                    self._anchor_host,self._snap_anchor = (None,None) if control else self.line_anchor(p)
                    self._draw_anchors[1] = self._snap_anchor
                    if self._snap_anchor:
                        point = QPointF(self._snap_anchor.x,self._snap_anchor.y)
                self._draw_band = (*xy(self._draw_start),*xy(point)); self._changed(); return
            if self._marquee_start is not None:
                sx,sy = xy(self._marquee_start); x,y = xy(p)
                self._marquee = Box(min(sx,x),min(sy,y),max(sx,x),max(sy,y)); self._changed(); return
            if self._drag_guide is not None:
                vertical,_ = guide(self.user_guides[self._drag_guide])
                self.update_guide_drag(p.x() if vertical else p.y()); return
            if self._drag_end is not None and self._drag_oid is not None:
                oid = self._drag_oid; end = self._drag_end; ends = self.line_overrides[oid]
                point = self._angle_snap(ends[2:4] if end == 0 else ends[:2],p) if shift else p
                self._anchor_host,self._snap_anchor = (None,None) if control else self.line_anchor(p,excluding=oid)
                self._drag_anchor = self._snap_anchor
                if self._snap_anchor:
                    point = QPointF(self._snap_anchor.x,self._snap_anchor.y)
                ends[2*end:2*end+2] = xy(point); self._changed(); return
            if self._drag_start is not None and self._drag_oid is not None and self._drag_box is not None:
                oid = self._drag_oid; b = self._drag_box; h = self._drag_handle
                if h is not None and h.is_side:
                    self._wrap_band = (Box(b.x0,b.y0,max(b.x0+24*self.px_per_point,p.x()),b.y1) if h == Handle.e
                                       else Box(min(b.x1-24*self.px_per_point,p.x()),b.y0,b.x1,b.y1))
                    self._changed(); return
                o = self.overrides.get(oid,Override())
                if h is not None:
                    o.scale = max(.05,hypot(p.x()-b.cx,p.y()-b.cy)/max(1,hypot(b.w/2,b.h/2)))
                else:
                    dx,dy = p.x()-self._drag_start.x(),p.y()-self._drag_start.y()
                    if shift:
                        if abs(dx)>abs(dy): dy=0
                        else: dx=0
                    if abs(dx)+abs(dy)<2 and oid not in self.overrides:
                        return
                    if self.snap_enabled and not ev.modifiers() & Qt.KeyboardModifier.AltModifier:
                        dx,dy,self._guides = self.snap_delta(oid,b,dx,dy)
                    else:
                        self._guides = []
                    o.dx,o.dy = self._drag_base[0]+dx,self._drag_base[1]+dy
                    for other,base in self._drag_group.items():
                        g = self.overrides.get(other,Override()); g.dx,g.dy = base[0]+dx,base[1]+dy
                        self.overrides[other] = g
                self.overrides[oid] = o; self._changed(); self._emit("on_geometry_change"); return
            if self.draw_tool is not None:
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
                if self.draw_tool in self.line_tools:
                    self._anchor_host,self._snap_anchor = (None,None) if control else self.line_anchor(p)
                    self._changed()
                return
            i = self.guide_at(p)
            if i is not None:
                self.viewport().setCursor(Qt.CursorShape.SizeHorCursor if guide(self.user_guides[i])[0] else Qt.CursorShape.SizeVerCursor)
                return
            self.hover = self.hit(p)
            cursor = Qt.CursorShape.OpenHandCursor if self.hover is not None else Qt.CursorShape.ArrowCursor
            if self.selection is not None:
                oid = self.selection
                ends = self.line_ends(oid)
                if ends:
                    if self.line_end_at(p, ends) is not None:
                        cursor = Qt.CursorShape.CrossCursor
                elif (box := self.current_box(oid)) is not None:
                    handle = self.handle_at(p, box, self.wrappable(oid))
                    if handle is not None:
                        cursor = Qt.CursorShape.SizeHorCursor if handle.is_side else Qt.CursorShape.CrossCursor
            self.viewport().setCursor(cursor)

        def mouseReleaseEvent(self, ev):
            if self._pan_start is not None:
                self._pan_start = None; return
            try:
                if self._drag_guide is not None:
                    self.end_guide_drag(); return
                if self._marquee_start is not None:
                    self._marquee_start = None; band = self._marquee; self._marquee = None
                    if band is not None and max(band.w,band.h) >= 3*self.px_per_point:
                        ids = self.selected_ids if self._marquee_adds else []
                        for oid in self.objects(within=band):
                            ids += self.group_mates(oid)
                        self.select_group(ids)
                    return
                if self.draw_tool == "pen":
                    points = simplify(self._pen_points,1.2); self._pen_points = []
                    length = sum(hypot(xy(b)[0]-xy(a)[0],xy(b)[1]-xy(a)[1]) for a,b in zip(points,points[1:]))
                    if len(points)>=2 and length>=4*self.px_per_point:
                        self.pending_strokes = [*self.pending_strokes, points]
                        def settled(_future=None):
                            self.pending_strokes = [stroke for stroke in self.pending_strokes if stroke is not points]
                        future = None
                        try:
                            future = self._emit("on_pen",points)
                        finally:
                            if future is None:
                                settled()
                            else:
                                when_done(future, settled)
                    return
                if self._draw_start is not None and self.draw_tool is not None:
                    tool,band = self.draw_tool,self._draw_band; self._draw_start = self._draw_band = None
                    if band and hypot(band[2]-band[0],band[3]-band[1])>=6*self.px_per_point:
                        self._emit("on_draw",tool,*band,self.anchor_fields(self._draw_anchors) if tool in self.line_tools else {})
                    self.draw_tool = None; return
                oid = self._drag_oid
                if self._drag_end is not None and oid is not None:
                    ends = self.line_overrides[oid]; original = (self.stop.get("lines") or {}).get(str(oid),ends)
                    if hypot(ends[0]-original[0],ends[1]-original[1])+hypot(ends[2]-original[2],ends[3]-original[3])>1 or self._drag_anchor is not None:
                        self._emit("on_endpoints",oid,ends,self.anchor_fields([self._drag_anchor,None] if self._drag_end == 0 else [None,self._drag_anchor]))
                    else:
                        self.line_overrides.pop(oid,None)
                    return
                h = self._drag_handle
                if h is not None and h.is_side and oid is not None and self._wrap_band:
                    if abs(self._wrap_band.w-self._drag_box.w)>=2:
                        self._emit("on_wrap",oid,self._wrap_band.w,"right" if h == Handle.e else "left")
                    return
                o = self.overrides.get(oid)
                if oid is None or o is None:
                    return
                if h is not None:
                    if abs(o.scale-1)>.002:
                        self._emit("on_resize",oid,o.scale)
                    elif o.dx == 0 and o.dy == 0 and o.replacement is None:
                        self.overrides.pop(oid,None)
                    return
                dx,dy = o.dx-self._drag_base[0],o.dy-self._drag_base[1]
                moved = abs(dx)+abs(dy)>=1
                group = sorted(self._drag_group)
                def move():
                    for member in [oid]+group:
                        if moved:
                            self._emit("on_move",member,dx,dy)
                        else:
                            base = self._drag_base if member == oid else self._drag_group[member]
                            override = self.overrides.get(member)
                            if override is not None:
                                if base == (0,0) and override.scale == 1 and override.replacement is None:
                                    self.overrides.pop(member,None)
                                else:
                                    override.dx,override.dy = base
                self._together(len(group)+1 if moved else 0,f"move {len(group)+1} objects",move)
            finally:
                self._drag_start = self._drag_oid = self._drag_handle = self._drag_box = self._drag_end = None
                self._wrap_band = self._anchor_host = self._snap_anchor = self._drag_anchor = None
                self._drag_group = {}; self._guides = []; self._changed()

        def contextMenuEvent(self, ev):
            if self.draw_tool is None:
                oid = self.hit(self.frame_point(ev.pos()))
                if oid not in self.selected_ids:
                    self.select_with_group(oid) if oid is not None else self.select_group([])
            self._emit("on_context_menu", ev.globalPos())
            ev.accept()

        def leaveEvent(self, ev):
            self._emit("on_cursor_move",None); self.hover = None
            super().leaveEvent(ev)

        def wheelEvent(self, ev):
            if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.zoom_by(1.25 if ev.angleDelta().y()>0 else .8)
            elif self.zoom is not None:
                delta = ev.pixelDelta() if not ev.pixelDelta().isNull() else ev.angleDelta()/3
                self.pan = QPointF(*xy(self.pan))+QPointF(delta)
            ev.accept()

        def keyPressEvent(self, ev):
            key = ev.key()
            if key == Qt.Key.Key_Space:
                self._space_pan = True; return
            if key == Qt.Key.Key_Escape and self.draw_tool is not None:
                self._draw_start = self._draw_band = None
                self._draw_anchors = [None, None]
                self._pen_points = []
                self.draw_tool = None
                return
            if key == Qt.Key.Key_A and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.select_all_objects(); return
            if self.selection is not None:
                delta = {Qt.Key.Key_Left:(-1,0),Qt.Key.Key_Right:(1,0),Qt.Key.Key_Up:(0,-1),Qt.Key.Key_Down:(0,1)}.get(key)
                if delta:
                    n = 10 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
                    for oid in self.selected_ids:
                        self._emit("on_nudge",oid,delta[0]*n,delta[1]*n)
                    return
                if key in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
                    self._emit("on_edit",self.selection,None); return
                if key in (Qt.Key.Key_Delete,Qt.Key.Key_Backspace):
                    self.delete_selection(); return
                if key == Qt.Key.Key_Escape:
                    self.select_group([]); return
            super().keyPressEvent(ev)

        def keyReleaseEvent(self, ev):
            if ev.key() == Qt.Key.Key_Space:
                self._space_pan = False
            super().keyReleaseEvent(ev)

        def dragEnterEvent(self, ev):
            if ev.mimeData().hasFormat("com.manimble.biocartoon-icon"):
                ev.acceptProposedAction()

        def dragMoveEvent(self, ev):
            self._emit("on_cursor_move",self.frame_point(ev.position()))
            if ev.mimeData().hasFormat("com.manimble.biocartoon-icon"):
                ev.acceptProposedAction()

        def dragLeaveEvent(self, ev):
            self._emit("on_cursor_move",None)

        def dropEvent(self, ev):
            if ev.mimeData().hasFormat("com.manimble.biocartoon-icon") and self._emit("on_icon_drop",bytes(ev.mimeData().data("com.manimble.biocartoon-icon")),self.frame_point(ev.position())):
                ev.acceptProposedAction()

        @property
        def editing_text(self):
            return self._inline.oid if self._inline is not None and self._inline.placeholder is None else None

        @property
        def editing_cell(self):
            e = self._inline
            return (e.oid,e.cell[0],e.cell[1]) if e is not None and e.cell is not None else None

        @property
        def text_script_mode(self):
            return self._inline.view.script_mode if self._inline is not None else None

        @text_script_mode.setter
        def text_script_mode(self, mode):
            if self._inline is not None:
                self._inline.view.script_mode = mode

        @property
        def text_editor_box(self):
            if self._inline is None:
                return None
            rect = self._inline.view.geometry()
            a = self.frame_point(rect.topLeft()); b = self.frame_point(rect.bottomRight())
            return Box(a.x(),a.y(),b.x(),b.y())

        def placeholder_anchor(self, ph):
            b = as_box(field(ph,"rect")); pad = self.frame_pad*self.unit_px
            return TextAnchor(b.x0+pad,b.cy,False,"left") if field(ph,"role") == "title" else TextAnchor(b.x0+pad,b.y0+pad,True,"left")

        @staticmethod
        def _mark_property(kind):
            return int(QTextFormat.Property.UserProperty)+CHAR_KINDS.index(kind)

        def _style_format(self, kind, on=True, color=None):
            fmt = QTextCharFormat()
            fmt.setProperty(self._mark_property(kind),(color or ("#FFD400" if kind == "highlight" else "#FFFFFF")) if on and kind in ("color","highlight") else bool(on))
            if kind == "bold":
                fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
            elif kind == "italic":
                fmt.setFontItalic(on)
            elif kind == "underline":
                fmt.setFontUnderline(on)
            elif kind == "strike":
                fmt.setFontStrikeOut(on)
            elif kind == "color":
                fmt.setForeground(QColor(color or "#FFFFFF") if on else self._editor_text_color)
            elif kind == "highlight":
                c = QColor(color or "#FFD400"); c.setAlphaF(.6)
                fmt.setBackground(c if on else QBrush(Qt.BrushStyle.NoBrush))
            return fmt

        def begin_text_edit(self, oid, text, family=None, bold=False, italic=False, styles=None, color=None,
                            at=None, cell=None, sizing=None, placeholder=None, align="left", valign="top",
                            line_spacing=None, frame=None, changed=None, done=None):
            if self._inline is not None:
                self._inline.view.hide(); self._inline.view.deleteLater(); self._inline = None
            # Table cells are (row, col, area, ink), matching models.table_cell_at.
            cell_ink = as_box(cell[3]) if cell is not None else None
            b = as_box(field(placeholder,"rect")) if placeholder is not None else cell_ink or self.raw_box(oid)
            if b is None or b.w <= 0 or b.h <= 0:
                return False
            lines = (sizing if sizing is not None else text).split("\n")
            family = display_family(family) if family else Theme.font(1).family()
            font = QFont(family); font.setPixelSize(100)
            font.setBold(bold); font.setItalic(italic)
            metrics = QFontMetricsF(font)
            widest = max((metrics.horizontalAdvance(line) for line in lines),default=0)
            by_height = 100*b.h/(len(lines)*metrics.height()*.8)
            font_px = field(placeholder,"size",24)/72*self.unit_px if placeholder is not None else min(max(100*b.w/widest,by_height*.5),by_height*2) if widest>1 else by_height
            tv = InlineTextView(self)
            color = DarkMode.map(QColor(color or "#FFFFFF"),m=self.color_view)
            self._editor_text_color = color
            font.setPixelSize(max(1,round(font_px*self.effective_scale)))
            palette = tv.palette()
            palette.setColor(QPalette.ColorRole.Text, color)
            tv.setPalette(palette)
            tv.setFont(font); tv.setTextColor(color); tv.setPlainText(text)
            given = dict(styles or {})
            given.setdefault("bold",[[0,len(text)]] if bold else [])
            given.setdefault("italic",[[0,len(text)]] if italic else [])
            cursor = QTextCursor(tv.document())
            cursor.select(QTextCursor.SelectionType.Document)
            # Marks, not inherited base font traits, determine every run's face.
            base = QTextCharFormat(); base.setFontWeight(QFont.Weight.Normal); base.setFontItalic(False)
            cursor.mergeCharFormat(base)
            for kind,runs in given.items():
                if kind not in CHAR_KINDS:
                    continue
                for run in runs:
                    a,bp = (run["start"],run["end"]) if isinstance(run,dict) else run[:2]
                    c = run.get("color") if isinstance(run,dict) else run[2] if len(run)>2 else None
                    cursor.setPosition(utf16(text,a)); cursor.setPosition(utf16(text,bp),QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(self._style_format(kind,True,c))
            area = None
            if cell is not None:
                cb = as_box(cell[2]); inset = max(3,.08*min(cb.w,cb.h))
                area = Box(cb.x0+inset,cb.y0+inset,cb.x1-inset,cb.y1-inset)
            anchor = self.placeholder_anchor(placeholder) if placeholder is not None else None
            if frame is not None and cell is None and placeholder is None:
                f = as_box(frame); pad = self.frame_pad*self.unit_px
                anchor = TextAnchor(f.cx if align == "center" else f.x1-pad if align == "right" else f.x0+pad,
                                    f.cy if valign == "mid" else f.y0+pad,valign != "mid",align)
            self._inline = InlineEdit(oid,tv,QPointF(area.cx if area else b.cx,area.cy if area else b.cy),
                                      font_px,family,bold,italic,text,done,changed,set(),
                                      (cell[0],cell[1],area) if cell else None,field(placeholder,"line") if placeholder is not None else None,
                                      anchor,align if cell is None else "left",line_spacing if cell is None else None)
            tv.bullets = field(placeholder,"role") == "body" or bool(text) and all(line.startswith(self.bullet) for line in text.split("\n"))
            if placeholder is not None:
                line = field(placeholder,"line")
                def delete_empty():
                    self.finish_text_edit(False); self._emit("on_placeholder_delete",line)
                tv.on_delete_empty = delete_empty
            if cell:
                def tab_cell():
                    self.finish_text_edit(True)
                    self._emit("on_cell_tab")
                tv.on_tab = tab_cell
            tv.textChanged.connect(self._text_changed)
            tv.selectionChanged.connect(self._selection_changed)
            self.layout_text_editor(); tv.show(); tv.setFocus()
            if placeholder is not None:
                cursor = tv.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End); tv.setTextCursor(cursor)
            elif at is not None:
                point = QPointF(*xy(at))-QPointF(tv.pos())
                tv.setTextCursor(tv.cursorForPosition(point.toPoint()))
            else:
                tv.selectAll()
            self._changed()
            return True

        def finish_text_edit(self, commit=True):
            e = self._inline
            if e is None or e.done is None:
                return
            done = e.done; e.done = None
            e.view.setReadOnly(True)
            cursor = e.view.textCursor(); cursor.clearSelection(); e.view.setTextCursor(cursor)
            if e.view.hasFocus():
                self.setFocus()
            self.committed_styles = {kind:self.char_runs(e.view,kind) for kind in e.touched} if e.touched else None
            text = e.view.toPlainText()
            def release():
                if self._inline is e:
                    self._inline = None; e.view.hide(); e.view.deleteLater(); self._changed()
            done(text if commit and (text != e.original or self.committed_styles is not None) else None,release)

        def char_runs(self, tv, kind):
            text = tv.toPlainText(); out = []
            block = tv.document().begin()
            prop = self._mark_property(kind)
            while block.isValid():
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid():
                        value = fragment.charFormat().property(prop)
                        if value:
                            a,b = code_points(text,fragment.position()),code_points(text,fragment.position()+fragment.length())
                            color = value if isinstance(value,str) else None
                            if out and out[-1][1] == a and (out[-1][2] if len(out[-1])>2 else None) == color:
                                out[-1][1] = b
                            else:
                                out.append([a,b]+([color] if color else []))
                    iterator += 1
                block = block.next()
            return out

        def selection_kinds(self, tv):
            cursor = tv.textCursor()
            if not cursor.hasSelection():
                return {kind for kind in CHAR_KINDS if tv.currentCharFormat().property(self._mark_property(kind))}
            start,end = cursor.selectionStart(),cursor.selectionEnd()
            result = set(CHAR_KINDS)
            for pos in range(start,end):
                c = QTextCursor(tv.document()); c.setPosition(pos); c.setPosition(pos+1,QTextCursor.MoveMode.KeepAnchor)
                result = {kind for kind in result if c.charFormat().property(self._mark_property(kind))}
                if not result:
                    break
            return result

        def toggle_inline_style(self, kind):
            e = self._inline
            if e is None or e.done is None or e.placeholder is not None or e.cell is not None:
                return None
            on = kind not in self.selection_kinds(e.view)
            return on if self.set_inline_style(kind,on=on,hex=None) else None

        def set_inline_style(self, kind, on=True, hex=None):
            e = self._inline
            if e is None or e.done is None or e.placeholder is not None or e.cell is not None or kind not in CHAR_KINDS:
                return False
            cursor = e.view.textCursor()
            if cursor.hasSelection():
                cursor.beginEditBlock(); cursor.mergeCharFormat(self._style_format(kind,on,hex)); cursor.endEditBlock()
            else:
                e.view.mergeCurrentCharFormat(self._style_format(kind,on,hex))
            e.touched.add(kind)
            self._changed(); self._selection_changed()
            return True

        def script_selection(self, superscript):
            e = self._inline
            if e is None or e.view.isReadOnly():
                return False
            cursor = e.view.textCursor()
            if not cursor.hasSelection():
                return False
            from .formatbar import TextTransform
            value = TextTransform.script(cursor.selectedText().replace("\u2029","\n"),superscript=superscript)
            if value is None:
                return False
            start = cursor.selectionStart(); cursor.insertText(value)
            cursor.setPosition(start); cursor.setPosition(start+utf16(value,len(value)),QTextCursor.MoveMode.KeepAnchor)
            e.view.setTextCursor(cursor); e.view.script_mode = None
            return True

        def _selection_changed(self):
            if self._inline is not None and self._inline.done is not None:
                self._emit("on_inline_selection_style",self.selection_kinds(self._inline.view))

        def _text_changed(self):
            if self._inline is not None and not self._layout_busy:
                self.layout_text_editor(); self.viewport().update()
                if self._inline.changed:
                    self._inline.changed(self._inline.view.toPlainText())

        def layout_text_editor(self):
            e = self._inline
            if e is None or self._layout_busy:
                return
            self._layout_busy = True
            try:
                tv = e.view; doc = tv.document(); scale = self.effective_scale
                font = QFont(e.family); font.setPixelSize(max(1,round(e.font_px*scale)))
                # Default font changes preserve per-run bold/italic marks and undo history.
                doc.setDefaultFont(font)
                block = QTextBlockFormat()
                block.setAlignment(Qt.AlignmentFlag.AlignHCenter if e.align == "center" else Qt.AlignmentFlag.AlignRight if e.align == "right" else Qt.AlignmentFlag.AlignLeft)
                pitch = e.font_px*scale*(1+(e.line_spacing if e.line_spacing is not None else .3))
                line_height = QFontMetricsF(font).lineSpacing()
                block.setLineHeight(max(pitch, line_height), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
                cursor = QTextCursor(doc); cursor.select(QTextCursor.SelectionType.Document)
                if cursor.blockFormat().alignment() != block.alignment() or abs(cursor.blockFormat().lineHeight()-block.lineHeight())>.01:
                    cursor.mergeBlockFormat(block)
                doc.setTextWidth(-1)
                width = max(2.,doc.idealWidth())+2
                doc.setTextWidth(width)
                height = max(doc.size().height(),QFontMetricsF(font).height())
                r = self.slide_rect
                x,y = r.x()+e.center.x()*scale-width/2,r.y()+e.center.y()*scale-height/2
                if e.anchor is not None:
                    a = e.anchor
                    x = r.x()+a.x*scale-(width/2 if a.align == "center" else width if a.align == "right" else 0)
                    y = r.y()+a.y*scale-(0 if a.top else height/2)
                tv.setGeometry(round(x),round(y),max(2,round(width)),max(2,round(height)))
            finally:
                self._layout_busy = False

else:
    class CanvasView:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("CanvasView requires PySide6. Install the Linux frontend requirements.")

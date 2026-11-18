"""Deck-wide engine search, replacement, and glyph highlights."""
from __future__ import annotations

from dataclasses import dataclass, fields
from html import escape

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QTextCursor
from PySide6.QtWidgets import (QApplication, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QLineEdit, QSizePolicy, QTextEdit, QToolButton, QWidget)

from .icons import painted_icon, template_icon
from .style import Theme, watch_appearance
from .tasks import spawn


@dataclass
class FindMatch:
    scene: str
    oid: int
    stop: int
    index: int
    kind: str
    value: str
    match: str
    editable: bool
    start: int | None = None
    end: int | None = None
    glyphs: list[int] | None = None

    @classmethod
    def from_json(cls, data):
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    @property
    def key(self):
        return f"{self.scene}:{self.kind}:{self.oid}:{self.stop}:{self.index}"

    @property
    def in_notes(self):
        return self.kind in ("notes", "buildNotes")


@dataclass
class FindMark:
    oid: int
    glyphs: tuple[int, int] | None
    current: bool


class LiveTextField(QLineEdit):
    on_next = None
    on_previous = None
    on_escape = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            action = self.on_previous if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else self.on_next
        elif event.key() == Qt.Key.Key_Escape:
            action = self.on_escape
        else:
            return super().keyPressEvent(event)
        if action:
            action()
        event.accept()


def _replace_icon(all_matches):
    width = 20 if all_matches else 16

    def draw(painter, _rect):
        ink = Theme.ink(1)
        painter.setFont(Theme.font(7.5, QFont.Weight.DemiBold))
        metrics = painter.fontMetrics()
        old, new = ("ab", "ac") if all_matches else ("b", "c")
        painter.setPen(ink)
        painter.drawText(QPointF(width - metrics.horizontalAdvance(old), metrics.ascent() - 1.5), old)
        arrow = QPainterPath(QPointF(7, 3))
        arrow.lineTo(4.5, 3)
        arrow.cubicTo(3.6, 3, 3, 3.6, 3, 4.5)
        arrow.lineTo(3, 7)
        arrow.moveTo(1.4, 5.6)
        arrow.lineTo(3, 7.4)
        arrow.lineTo(4.6, 5.6)
        painter.drawPath(arrow)
        box = QRectF(.5, 8.5, 11 if all_matches else 8, 7)
        painter.setBrush(ink)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(box, 1.5, 1.5)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.setPen(ink)
        painter.drawText(QPointF(box.center().x() - metrics.horizontalAdvance(new) / 2,
                                  box.top() - 2 + metrics.ascent()), new)

    return painted_icon(width, 16, draw)


class FindButton(QToolButton):
    def __init__(self, tip, *, title="", symbol=None, icon=None, toggle=False, option=False,
                 underline=False, width=24, icon_width=0):
        super().__init__()
        self.option = option
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCheckable(toggle)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if title:
            self.setText(title)
            font = Theme.font(12, QFont.Weight.Medium)
            font.setUnderline(underline)
            self.setFont(font)
        size = 11 if option else 12
        if symbol:
            self.setIcon(template_icon(symbol, self))
        if icon is not None:
            self.setIcon(icon)
            self.setIconSize(QSize(icon_width, 16))
        else:
            self.setIconSize(QSize(size, size))
        self.setFixedSize(max(22, self.fontMetrics().horizontalAdvance(title) + 4) if title else width,
                          max(18, self.fontMetrics().height() + 2) if title else 18 if option else 22)

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
            if not self.isEnabled():
                painter.setOpacity(.35)
            if self.option and self.isChecked():
                fill = QColor(Theme.accent)
                fill.setAlphaF(.3)
                painter.setBrush(fill)
                painter.setPen(QPen(Theme.accent))
                painter.drawRoundedRect(rect, 3, 3)
            elif self.isDown() or self.hasFocus():
                painter.setBrush(Theme.ink(.12) if self.isDown() else Qt.BrushStyle.NoBrush)
                painter.setPen(Theme.accent if self.hasFocus() else Qt.PenStyle.NoPen)
                painter.drawRoundedRect(rect, 3, 3)
            if self.text():
                painter.setFont(self.font())
                painter.setPen(Theme.ink(1))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
            else:
                # Render into a pixmap first: the replace art knocks out its new letters.
                pixmap = self.icon().pixmap(self.iconSize(), self.devicePixelRatioF(), QIcon.Mode.Normal)
                size = pixmap.deviceIndependentSize()
                painter.drawPixmap(QPointF((self.width() - size.width()) / 2,
                                          (self.height() - size.height()) / 2), pixmap)


class FindInput(QWidget):
    def __init__(self, field, buttons=()):
        super().__init__()
        self.field = field
        field.setFont(Theme.font(13))
        field.setFrame(False)
        field.setMinimumWidth(0)
        field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        field.setStyleSheet("QLineEdit { border: none; padding: 0; background: transparent; }")
        field.installEventFilter(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 0, 3, 0)
        row.setSpacing(1)
        row.addWidget(field, 1)
        if buttons:
            row.addSpacing(3)
        for button in buttons:
            row.addWidget(button)
        self.setFixedHeight(max(24, field.sizeHint().height() + 2,
                                max((b.height() + 4 for b in buttons), default=0)))
        self.compact_width = max(135, row.minimumSize().width() +
                                 field.fontMetrics().horizontalAdvance(field.placeholderText()) + 10)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return super().eventFilter(watched, event)

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Theme.code_background())
            painter.setPen(Theme.accent if self.field.hasFocus() else Theme.separator())
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5, 5)


class _ReplaceRow(QWidget):
    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            square_top = QPainterPath()
            square_top.addRect(rect.x(), rect.y(), rect.width(), 6)
            painter.setBrush(Theme.bar_background())
            painter.setPen(Theme.separator())
            painter.drawPath(path.united(square_top))


class FindBar(QWidget):
    preferred_field_width = 250
    field_help = "Find text in every slide{shortcut}; Return next match, Shift+Return previous"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.field = LiveTextField()
        self.field.setPlaceholderText("Find")
        self.field.setAccessibleName("Find")
        self.replace_field = LiveTextField()
        self.replace_field.setPlaceholderText("Replace")
        self.replace_field.setAccessibleName("Replace")
        self.case_button = FindButton("Match case", title="Aa", toggle=True, option=True)
        self.word_button = FindButton("Whole word", title="ab", toggle=True, option=True, underline=True)
        self.regex_button = FindButton(r"Regular expression (Python syntax; $1 or \1 in the replacement names a group)",
                                       title=".*", toggle=True, option=True)
        self.notes_button = FindButton("Check notes: search the speaker notes too (slide and build notes)",
                                       symbol="note.text", width=22, toggle=True, option=True)
        self.prev = FindButton("Previous match (Shift+Return)", symbol="arrow.up")
        self.next = FindButton("Next match (Return)", symbol="arrow.down")
        self.close = FindButton("Clear the search (Esc)", symbol="xmark")
        self.replace_button = FindButton("Toggle replace", symbol="chevron.right", toggle=True, width=18)
        self.replace_one = FindButton("Replace (Return): the selected match, then go to the next one",
                                      icon=_replace_icon(False), icon_width=16)
        self.replace_all = FindButton("Replace All: every match on every slide, and in the notes when they are checked (one undo step)",
                                      icon=_replace_icon(True), icon_width=20)
        self.count = QLabel()
        self.count.setTextFormat(Qt.TextFormat.PlainText)
        self.count.setFont(Theme.font(12))
        self.count.setFixedWidth(max(68, round(Theme.logical_size(68))))
        self.find_box = FindInput(self.field, [self.case_button, self.word_button, self.regex_button, self.notes_button])
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(self.replace_button)
        row.addSpacing(2)
        row.addWidget(self.find_box)
        row.addSpacing(4)
        for w in (self.count, self.prev, self.next, self.close):
            row.addWidget(w)
        self.find_box.setFixedWidth(self.preferred_field_width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.home = None
        self.replace_row = _ReplaceRow(self)
        replace = QHBoxLayout(self.replace_row)
        replace.setSpacing(0)
        self.replace_box = FindInput(self.replace_field)
        replace.addWidget(self.replace_box)
        replace.addSpacing(6)
        replace.addWidget(self.replace_one)
        replace.addSpacing(4)
        replace.addWidget(self.replace_all)
        replace.addStretch()
        shadow = QGraphicsDropShadowEffect(self.replace_row)
        shadow.setBlurRadius(8)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(0, 0, 0, 76))
        self.replace_row.setGraphicsEffect(shadow)
        self.replace_row.hide()
        self.matches = []
        self.current = None
        self.shown_key = None
        self.generation = 0
        self.on_update = None
        self._error = None
        self._field_tip = self.field_help.format(shortcut="")
        watch_appearance(self.refresh_appearance)
        self.update_results()

    def bind_shortcut_help(self, actions):
        self._field_tip = self.field_help.format(shortcut=f" ({actions.shortcut_text('show_find')})")
        self.replace_button.setToolTip(f"Toggle replace ({actions.shortcut_text('show_find_replace')})")
        self.refresh_appearance()

    @property
    def minimum_field_width(self):
        return min(self.preferred_field_width, self.find_box.compact_width)

    def width_for(self, field_width):
        return self.sizeHint().width() - self.find_box.minimumWidth() + field_width

    def set_field_width(self, width):
        if self.find_box.minimumWidth() != width:
            self.find_box.setFixedWidth(width)

    def attach_replace_row(self, root, home):
        self.home = home
        self.replace_row.setParent(root)
        for w in (root, home, self, self.find_box, self.replace_button, self.close):
            w.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.replace_shown and event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show):
            self.position_replace_row()
        return super().eventFilter(watched, event)

    def position_replace_row(self):
        if self.home is None:
            return
        root = self.replace_row.parentWidget()
        left = self.replace_button.mapTo(root, QPoint()).x() - 6
        right = self.close.mapTo(root, QPoint(self.close.width(), 0)).x() + 6
        top = self.home.mapTo(root, QPoint(0, self.home.height() - 1)).y()
        box_offset = self.find_box.mapTo(root, QPoint()).x() - left
        self.replace_row.layout().setContentsMargins(box_offset, 5, 6, 6)
        self.replace_box.setFixedWidth(self.find_box.width())
        self.replace_row.setGeometry(left, top, right - left, self.replace_box.height() + 11)
        self.replace_row.raise_()

    @property
    def options(self):
        return {"query": self.field.text(), "case": self.case_button.isChecked(), "word": self.word_button.isChecked(),
                "regex": self.regex_button.isChecked(), "notes": self.notes_button.isChecked()}

    @property
    def replace_shown(self):
        return not self.replace_row.isHidden()

    def set_replace_shown(self, on):
        self.replace_button.setChecked(on)
        self.replace_button.setIcon(template_icon("chevron.down" if on else "chevron.right", self.replace_button))
        if on:
            self.position_replace_row()
        self.replace_row.setVisible(on)

    def refresh_appearance(self):
        self.replace_row.update()
        self.find_box.update()
        self.replace_box.update()
        error = f'<div style="white-space: pre-wrap">{escape(self._error)}</div>' if self._error else ""
        self.count.setToolTip(error)
        self.field.setToolTip(error or self._field_tip)
        color = (Theme.warning_ink() if self._error else Theme.secondary_ink() if not self.field.text()
                 else Theme.ink(1) if self.matches else Theme.error_ink())
        palette = self.count.palette()
        palette.setColor(self.count.foregroundRole(), color)
        self.count.setPalette(palette)
        # The knockout icons use the live Theme instead of a native palette role.
        self.replace_one.update()
        self.replace_all.update()

    def update_results(self, error=None):
        self._error = error
        has = bool(self.matches)
        for button in (self.prev, self.next, self.replace_one, self.replace_all):
            button.setEnabled(has)
        if error:
            self.count.setText("⚠︎ Error")
        elif has:
            current = self.current + 1 if self.current is not None else "?"
            self.count.setText(f"{current} of {len(self.matches)}")
        else:
            self.count.setText("No results")
        self.refresh_appearance()
        if self.on_update:
            self.on_update()


class FindMixin:
    def init_find(self):
        self.find_bar = FindBar()
        self.format_bar.add_find(self.find_bar)
        self.find_bar.attach_replace_row(self.window.centralWidget(), self.format_bar)
        self.wire_find_bar(self.find_bar)

    def wire_find_bar(self, f):
        self.find_bar = f
        timer = QTimer(f)
        timer.setSingleShot(True)
        timer.setInterval(150)
        timer.timeout.connect(lambda: spawn(self.run_find(False)))
        f._search_timer = timer
        f.field.textChanged.connect(self.find_field_changed)
        for button in [f.case_button, f.word_button, f.regex_button, f.notes_button]:
            button.toggled.connect(self.find_option_changed)
        f.prev.clicked.connect(self.find_previous)
        f.next.clicked.connect(self.find_next)
        f.close.clicked.connect(self.close_find)
        f.replace_button.clicked.connect(self.toggle_replace_row)
        f.replace_one.clicked.connect(self.replace_current)
        f.replace_all.clicked.connect(self.replace_everything)
        f.field.on_next = self.find_next
        f.field.on_previous = self.find_previous
        f.field.on_escape = self.close_find
        f.replace_field.on_next = self.replace_current
        f.replace_field.on_escape = self.close_find
        f.on_update = self._find_updated
        self.canvas.find_marks = self.find_marks_on_slide

    def _find_updated(self):
        self.canvas.viewport().update()
        self.mark_notes_match()

    def find_marks_on_slide(self):
        f = self.find_bar
        if self.scene is None:
            return []
        stops = self.scene.get("stops", [])
        if not 0 <= self.stop_index < len(stops):
            return []
        visible = {int(v[0]) for v in stops[self.stop_index].get("visible", [])}
        objects = self.scene.get("objects", {})
        return [FindMark(m.oid, tuple(m.glyphs) if m.glyphs and len(m.glyphs) == 2 else None, i == f.current)
                for i, m in enumerate(f.matches) if m.scene == self.scene_name and not m.in_notes and m.oid in visible
                and objects.get(str(m.oid), {}).get("value", "").replace(" \n", " ") == m.value]

    def mark_notes_match(self):
        f = self.find_bar
        match = f.matches[f.current] if f.current is not None and f.current < len(f.matches) else None
        for name, kind in [("notes_box", "notes"), ("build_notes_box", "buildNotes")]:
            box = getattr(self, name, None)
            if box is None:
                continue
            if hasattr(box, "highlight"):
                good = match is not None and match.kind == kind and self.shown_match(match)
                box.highlight(match.value if good else None, match.start or 0 if good else 0, match.end or 0 if good else 0)
            elif isinstance(box, QTextEdit):
                selections = []
                if match and match.kind == kind and self.shown_match(match) and box.toPlainText() == match.value:
                    s = QTextEdit.ExtraSelection()
                    s.cursor = box.textCursor()
                    s.cursor.setPosition(len(match.value[:match.start].encode("utf-16-le")) // 2)
                    s.cursor.setPosition(len(match.value[:match.end].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
                    s.format.setBackground(QColor("#f6c344"))
                    selections.append(s)
                box.setExtraSelections(selections)

    def show_find(self, *_):
        self.find_bar.show()
        self.find_bar.field.setFocus()
        self.find_bar.field.selectAll()

    def show_find_replace(self, *_):
        self.find_bar.show()
        self.find_bar.set_replace_shown(True)
        (self.find_bar.replace_field if self.find_bar.field.text() else self.find_bar.field).setFocus()

    def toggle_replace_row(self, *_):
        if self.find_bar.replace_shown:
            bar = self.find_bar
            if bar.replace_field.hasFocus():
                bar.field.setFocus()
            bar.set_replace_shown(False)
        else:
            self.show_find_replace()

    def close_find(self, *_):
        f = self.find_bar
        with QSignalBlocker(f.field):
            f.field.clear()
        f._search_timer.stop()
        self._reset_find_results(f)
        f.set_replace_shown(False)
        self.canvas.setFocus()

    def find_field_changed(self, *_):
        f = self.find_bar
        self._reset_find_results(f)
        if f.field.text():
            f._search_timer.start()
        else:
            f._search_timer.stop()

    def find_option_changed(self, *_):
        self._reset_find_results(self.find_bar)
        spawn(self.run_find(False))

    @staticmethod
    def _reset_find_results(f):
        f.generation += 1
        f.matches, f.current, f.shown_key = [], None, None
        f.update_results()

    def run_find(self, keep=False):
        f = self.find_bar
        f._search_timer.stop()
        f.generation += 1
        generation = f.generation
        before = f.matches[f.current] if f.current is not None and f.current < len(f.matches) else None
        if not f.field.text() or self.workspace is None:
            f.matches, f.current = [], None
            f.update_results()
            return True
        try:
            reply = yield self.call("find", f.options)
            if generation != f.generation:
                return False
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "Search failed"))
            f.matches = [FindMatch.from_json(m) for m in reply.get("matches", [])]
            f.current = None
            if keep and before:
                f.current = next((i for i, m in enumerate(f.matches) if m.key == before.key), None)
                if f.current is None:
                    f.current = self.first_match_at_or_after(before)
            f.update_results()
            return True
        except Exception as exc:
            if generation == f.generation:
                f.matches, f.current = [], None
                f.update_results(str(exc))
                return True
            return False

    def show_index(self, scene):
        return next((i for i, s in enumerate((self.state or {}).get("scenes", [])) if s["name"] == scene), 2**31)

    def first_match_at_or_after(self, before):
        return next((i for i, m in enumerate(self.find_bar.matches)
                     if (self.show_index(m.scene), m.oid, m.index) >= (self.show_index(before.scene), before.oid, before.index)), None)

    def shown_match(self, m):
        if self.scene_name != m.scene:
            return False
        if not m.in_notes:
            return self.selection == m.oid
        return (self.find_bar.shown_key == m.key and self.stop_index == m.stop
                and self.drawer_open and self.drawer.page == ("build_notes" if m.kind == "buildNotes" else "notes"))

    def find_next(self, *_):
        spawn(self.go_to_match(True))

    def find_previous(self, *_):
        spawn(self.go_to_match(False))

    def go_to_match(self, forward=True):
        f = self.find_bar
        if not f.matches and not (yield from self.run_find(False)):
            return
        if not f.matches:
            if f.field.text():
                QApplication.beep()
            return
        if f.current is not None and self.shown_match(f.matches[f.current]):
            index = (f.current + (1 if forward else -1)) % len(f.matches)
        else:
            indices = [i for i, m in enumerate(f.matches) if (self.show_index(m.scene) >= self.show_index(self.scene_name)
                       if forward else self.show_index(m.scene) <= self.show_index(self.scene_name))]
            index = (indices[0] if indices else 0) if forward else (indices[-1] if indices else len(f.matches) - 1)
        yield from self.show_match(index)

    def show_match(self, index):
        f = self.find_bar
        if not 0 <= index < len(f.matches):
            return
        m = f.matches[index]
        generation = f.generation
        if self.scene_name != m.scene:
            yield from self.select_scene(m.scene)
        if self.scene_name != m.scene or f.generation != generation:
            return
        self.go_to_stop(m.stop)
        if m.in_notes:
            self.selection = None
            self.drawer.show_page("notes" if m.kind == "notes" else "build_notes")
            if not self.drawer_open:
                self.set_drawer_open(True)
        else:
            self.selection = m.oid
            self.canvas.select_group([m.oid])
        f.current, f.shown_key = index, m.key
        f.update_results()

    def replace_request(self, match=None, all=False):
        request = dict(self.find_bar.options, replacement=self.find_bar.replace_field.text())
        if all:
            request["all"] = True
        elif match:
            request.update(scene=match.scene, oid=match.oid, index=match.index, stop=match.stop, kind=match.kind)
        return request

    def _call_replace(self, request, *, all_matches=False):
        old_text = self.busy_text
        if all_matches:
            self.busy_text = "Replacing on every slide…"
        self.busy += 1
        try:
            return (yield self.call("replace", request))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self.busy -= 1
            if all_matches:
                self.busy_text = old_text

    def replace_current(self, *_):
        if not self.find_bar.field.text():
            return
        def work():
            if not (yield from self.run_find(True)):
                return
            f = self.find_bar
            if f.current is None or not self.shown_match(f.matches[f.current]):
                yield from self.go_to_match(True)
                return
            m = f.matches[f.current]
            if not m.editable:
                self.toast("This text is made by code, so it cannot be replaced here; edit it in the Python source", error=True)
                yield from self.go_to_match(True)
                return
            request = self.replace_request(m)
            reply = yield from self._call_replace(request)
            ok = reply.get("ok")
            if ok:
                self.last_changed = reply.get("changed")
                self.toast(f'Replaced · {m.match} → {request["replacement"]}')
            else:
                self.toast(reply.get("error") or "replace failed", error=True)
            if m.scene == self.scene_name:
                yield from self.load_scene(m.scene, True)
            yield from self.reload_state()
            if (yield from self.run_find(True)) and ok and f.current is not None:
                index = f.current
                if f.matches[index].key == m.key:
                    index = (index + 1) % len(f.matches)
                yield from self.show_match(index)
        return self.submit(work)

    def replace_everything(self, *_):
        if not self.find_bar.field.text():
            return
        request = self.replace_request(all=True)
        def work():
            reply = yield from self._call_replace(request, all_matches=True)
            if reply.get("ok"):
                self.last_changed = reply.get("changed")
                self.toast(f'Saved · {reply.get("message") or "replaced"}')
            else:
                self.toast(reply.get("error") or "replace failed", error=True)
            if self.scene_name:
                yield from self.load_scene(self.scene_name, True)
            yield from self.reload_state()
            yield from self.run_find(False)
        return self.submit(work)

"""Private editor/presenter feedback, never painted into audience or exported frames."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPalette, QPen, QRegion,
                          QTextCursor, QTextLayout, QTextOption)
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                              QScrollArea, QSizePolicy, QStyle, QStyleOptionFocusRect, QTextEdit,
                              QToolButton, QVBoxLayout, QWidget)

from . import workers
from .icons import template_icon
from .style import Theme, watch_appearance
from .tasks import resolved, spawn, when_done
from .widgets import hairline


_NOTE_RED = QColor.fromRgbF(1.0, .18, .16)


def _note_red(alpha):
    color = QColor(_NOTE_RED)
    color.setAlphaF(alpha)
    return color


def _symbol_button(symbol, name, tip, parent=None, *, tint=None, size=12):
    button = QToolButton(parent)
    button.setAccessibleName(name)
    button.setToolTip(tip)
    button.setIcon(template_icon(symbol, button, tint=tint))
    button.setIconSize(QSize(size, size))
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return button


def _confirm_removal(parent, *, all_notes=False):
    title = "Delete all feedback?" if all_notes else "Delete this feedback?"
    text = ("Remove every private feedback note from this deck?" if all_notes
            else "Remove this private feedback note from this deck?")
    box = QMessageBox(QMessageBox.Icon.Question, title, title, parent=parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setInformativeText(text)
    remove = box.addButton("Delete All" if all_notes else "Delete", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is remove


@dataclass
class FeedbackNote:
    id: str
    scene: str
    build: int
    x: float
    y: float
    w: float
    text: str
    created: str
    default_width = 0.32

    @classmethod
    def from_json(cls, value):
        return cls(id=str(value["id"]), scene=str(value["scene"]), build=int(value["build"]),
                   x=float(value["x"]), y=float(value["y"]), w=float(value["w"]),
                   text=str(value["text"]), created=str(value["created"]))


class FeedbackStore:
    def __init__(self, url, original_has_file=False, *, load=True):
        self.url = Path(url)
        self.keep_empty_file = original_has_file
        self.notes = []
        self.on_change = None
        self.on_error = None
        self._lock = threading.Lock()
        self._generation = 0
        self._saved_generation = 0
        self._pending = resolved()
        if load:
            self.load()

    def load(self):
        if not self.url.exists():
            self.notes = []
            return
        data = json.loads(self.url.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("notes"), list):
            raise ValueError(f"Unsupported feedback format in {self.url}")
        self.notes = [FeedbackNote.from_json(n) for n in data["notes"]]

    def _write(self, generation, payload):
        with self._lock:
            if generation < self._saved_generation:
                return
            if payload is None:
                self.url.unlink(missing_ok=True)
            else:
                fd, temporary = tempfile.mkstemp(prefix=".feedback-", dir=self.url.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.url)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            self._saved_generation = generation

    def save(self):
        self._generation += 1
        payload = json.dumps({"version": 1, "notes": [asdict(n) for n in self.notes]}, indent=2, sort_keys=True, ensure_ascii=False)
        if not self.notes and not self.keep_empty_file:
            payload = None
        self._pending = workers.run_blocking(self._write, self._generation, payload)
        def completed(future):
            try:
                future.result()
            except Exception as exc:
                if self.on_error:
                    self.on_error(str(exc))
        when_done(self._pending, completed)
        if self.on_change:
            self.on_change()
        return self._pending

    def flush(self):
        # Resave the latest snapshot: even an earlier failed write cannot masquerade as a successful flush.
        return self.save()

    @property
    def is_empty(self):
        return not self.notes

    def notes_for(self, scene):
        return [n for n in self.notes if n.scene == scene]

    def note(self, id):
        return next((n for n in self.notes if n.id == id), None)

    @property
    def scenes(self):
        return list(dict.fromkeys(n.scene for n in self.notes))

    def add(self, scene, build, x, y, text=""):
        note = FeedbackNote(str(uuid.uuid4()), scene, build, float(x), float(y), FeedbackNote.default_width,
                            text, datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        self.notes.append(note)
        self.save()
        return note

    def set_text(self, id, text):
        note = self.note(id)
        if note and note.text != text:
            note.text = text
            self.save()

    def move(self, id, x, y):
        note = self.note(id)
        if note:
            note.x, note.y = x, y
            self.save()

    def set_width(self, id, width, *, x=None):
        note = self.note(id)
        if note:
            note.w = max(0.05, min(1, width))
            if x is not None:
                note.x = x
            self.save()

    def remove(self, id):
        if self.note(id):
            self.notes = [n for n in self.notes if n.id != id]
            self.save()

    def remove_all(self):
        if self.notes:
            self.notes = []
            self.save()


class FeedbackTextView(QTextEdit):
    on_end = None
    on_delete_empty = None
    on_focus_change = None

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self.on_focus_change:
            self.on_focus_change(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self.on_focus_change:
            self.on_focus_change(False, event.reason())

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape or (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
            if self.on_end:
                self.on_end()
            event.accept()
        elif key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not self.toPlainText() and self.on_delete_empty:
            self.on_delete_empty()
            event.accept()
        else:
            super().keyPressEvent(event)


class FeedbackBox(QFrame):
    def __init__(self, id, parent=None):
        super().__init__(parent)
        self.id = id
        self.on_delete = self.on_geometry_commit = self.on_text_change = self.on_select = None
        self._drag = self._layout_key = self._flash = None
        self._selected = False
        self.text = FeedbackTextView(self)
        self.text.setAcceptRichText(False)
        self.text.setFrameStyle(QFrame.Shape.NoFrame)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text.setStyleSheet(
            f"QTextEdit {{ color: {_NOTE_RED.name()}; background: transparent; border: none; padding: 0; }}"
        )
        self.text.document().setDocumentMargin(0)
        self.text.textChanged.connect(lambda: self.on_text_change() if self.on_text_change else None)
        self.close = _symbol_button("xmark.circle.fill", "Delete this feedback", "Delete this feedback",
                                    self, tint=_note_red(.8), size=11)
        self.close.setAutoRaise(True)
        self.close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close.setStyleSheet("QToolButton { border: none; padding: 0; background: transparent; }")
        self.close.clicked.connect(lambda: self.on_delete() if self.on_delete else None)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def select(self, selected):
        if selected != self._selected:
            self._selected = selected
            self.update()

    def flash(self):
        if self._flash is None:
            self._flash = QVariantAnimation(self)
            self._flash.setStartValue(4.0)
            self._flash.setEndValue(2.0)
            self._flash.setDuration(600)
            self._flash.valueChanged.connect(lambda _: self.update())
        self._flash.stop()
        self._flash.start()

    def set_note_text(self, text):
        with QSignalBlocker(self.text):
            self.text.setPlainText(text)
        self._layout_key = None

    def layout_note(self, width, font_size):
        document = self.text.document()
        width = round(width)
        key = (width, font_size, document.revision())
        if key == self._layout_key:
            return
        font = Theme.font(font_size, QFont.Weight.Medium)
        if self.text.font() != font:
            with QSignalBlocker(self.text):
                self.text.setFont(font)
        inner = max(10, width - 12)
        document.setTextWidth(inner)
        height = round(max(QFontMetrics(font).lineSpacing(), document.size().height())) + 12
        self.resize(width, height)
        self.text.setGeometry(6, 6, inner, height - 12)
        close_size = max(12, min(18, round(font_size * .8)))
        self.close.setGeometry(width - close_size - 1, height - close_size - 1, close_size, close_size)
        self.close.raise_()
        self._layout_key = (width, font_size, document.revision())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = 2 if self._selected else 1
        if self._selected and self._flash is not None:
            border = self._flash.currentValue()
        color = _NOTE_RED if self._selected else _note_red(.75)
        painter.setPen(QPen(color, border))
        painter.setBrush(_note_red(.07))
        inset = border / 2
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), 4, 4)
        if self._selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for x in (1, self.width() - 5):
                painter.drawRoundedRect(QRectF(x, (self.height() - 5) / 2, 4, 5), 1, 1)

    def _resize_edge(self, x):
        return -1 if x < 6 else 1 if x >= self.width() - 6 else 0

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if self.on_select:
            self.on_select()
        self._drag = event.globalPosition(), self.pos(), self.width(), self._resize_edge(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag:
            start, origin, width, edge = self._drag
            delta = (event.globalPosition() - start).toPoint()
            if edge:
                layer = self.parentWidget()
                slide = layer.slide_rect()
                font_size = layer.font_size(slide)
                if edge < 0:
                    delta_x = max(round(slide.left()) - origin.x(), min(width - 60, delta.x()))
                    self.move(origin.x() + delta_x, origin.y())
                    self.layout_note(width - delta_x, font_size)
                else:
                    available = max(60, round(slide.right()) - origin.x())
                    self.layout_note(max(60, min(available, width + delta.x())), font_size)
            else:
                self.move(origin + delta)
            self.parentWidget()._update_mask()
            event.accept()
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._resize_edge(event.position().x())
                           else Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        if self._drag and event.button() == Qt.MouseButton.LeftButton:
            _, origin, width, edge = self._drag
            self._drag = None
            if self.on_geometry_commit and (self.pos() != origin or self.width() != width):
                self.on_geometry_commit(self.pos(), self.width(), edge)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class FeedbackLayer(QWidget):
    def __init__(self, canvas=None, parent=None):
        if parent is None and canvas is not None:
            parent = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        super().__init__(parent)
        self.canvas = canvas
        self._store = None
        self._scene = None
        self.build = 0
        self._placing = False
        self.slide_rect = self._canvas_rect
        self.fallback_responder = canvas
        self.on_add = self.on_select = None
        self.selected = None
        self.boxes = {}
        if parent:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())
        self.show()
        self._update_mask()

    def _canvas_rect(self):
        rect = getattr(self.canvas, "slide_rect", QRectF())
        return rect() if callable(rect) else rect

    @property
    def store(self):
        return self._store

    @store.setter
    def store(self, store):
        if self._store is store:
            return
        self.select(None)
        for id in tuple(self.boxes):
            self._discard_box(id)
        self._store = store
        self.rebuild()

    @property
    def scene(self):
        return self._scene

    @scene.setter
    def scene(self, scene):
        if scene != self._scene:
            self.end_editing()
            self.select(None)
            self._scene = scene
            self.rebuild()

    @property
    def placing(self):
        return self._placing

    @placing.setter
    def placing(self, on):
        self._placing = bool(on)
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self._update_mask()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.setGeometry(watched.rect())
            self.relayout()
        return False

    @staticmethod
    def font_size(rect):
        return max(8, round(rect.height()*0.033))

    def _update_mask(self):
        region = QRegion()
        if self.placing:
            region |= QRegion(self.slide_rect().toAlignedRect())
        for box in self.boxes.values():
            region |= QRegion(box.geometry())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, region.isEmpty())
        self.setMask(region)
        self.raise_()

    def rebuild(self):
        wanted = self.store.notes_for(self.scene) if self.store and self.scene else []
        ids = {n.id for n in wanted}
        for id in list(self.boxes):
            if id not in ids:
                self._discard_box(id)
        for note in wanted:
            box = self.boxes.get(note.id)
            if box is None:
                box = self._make_box(note)
                self.boxes[note.id] = box
            elif not box.text.hasFocus() and box.text.toPlainText() != note.text:
                box.set_note_text(note.text)
        self.relayout()

    def _discard_box(self, id):
        if self.selected == id:
            self.select(None)
        box = self.boxes.pop(id)
        box.text.on_focus_change = None
        if box.text.hasFocus():
            self._release_focus(box)
        box.hide()
        box.deleteLater()

    def _make_box(self, note):
        id, store = note.id, self.store
        box = FeedbackBox(id, self)
        box.set_note_text(note.text)
        box.on_delete = lambda: self._request_remove(id)
        box.on_select = lambda: self.select(id)
        box.on_text_change = lambda: self._text_changed(id)
        box.text.on_end = self.end_editing
        box.text.on_delete_empty = lambda: self.remove(id)

        def focus_change(focused, reason=None):
            if focused:
                self.select(id)
                return
            if reason in (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason):
                return
            # Do not remove a draft until the responder change is complete. A
            # modal prompt, deck switch, or renewed focus must not delete it.
            if not box.text.toPlainText():
                QTimer.singleShot(0, self, lambda: self._discard_empty_draft(store, id))

        def commit(point, width, edge):
            rect = self.slide_rect()
            if self.store is store and rect.width() > 0 and rect.height() > 0:
                x = min(.98, max(0, (point.x() - rect.x()) / rect.width()))
                if edge:
                    store.set_width(id, width / rect.width(), x=x if edge < 0 else None)
                else:
                    y = min(.98, max(0, (point.y() - rect.y()) / rect.height()))
                    store.move(id, x, y)
            self.relayout()

        box.text.on_focus_change = focus_change
        box.on_geometry_commit = commit
        box.show()
        return box

    def _discard_empty_draft(self, store, id):
        if self.store is not store:
            return
        note, box = store.note(id), self.boxes.get(id)
        if note is not None and not note.text and (box is None or not box.text.hasFocus()):
            self.remove(id)

    def _text_changed(self, id):
        box = self.boxes.get(id)
        if box and self.store:
            # Keep the memory snapshot current even before focus leaves, for Save/close.
            self.store.set_text(id, box.text.toPlainText())
            self.relayout(id)

    def relayout(self, only=None):
        rect = self.slide_rect()
        if rect.width() > 0 and rect.height() > 0 and self.store:
            font_size = self.font_size(rect)
            for id, box in self.boxes.items():
                if only is not None and only != id:
                    continue
                note = self.store.note(id)
                if note:
                    box.layout_note(max(60, rect.width() * note.w), font_size)
                    box.move(round(rect.x() + rect.width() * note.x), round(rect.y() + rect.height() * note.y))
        self._update_mask()

    def _request_remove(self, id):
        store = self.store
        note = store.note(id) if store else None
        if note and (not note.text or _confirm_removal(self)) and self.store is store:
            self.remove(id)

    def remove(self, id):
        if self.store:
            self.store.remove(id)
        if id in self.boxes:
            self._discard_box(id)
            self._update_mask()

    def _release_focus(self, box):
        if self.fallback_responder:
            self.fallback_responder.setFocus()
        else:
            box.text.clearFocus()

    def end_editing(self):
        for box in self.boxes.values():
            if box.text.hasFocus():
                self._release_focus(box)
                break

    def select(self, id, *, notify=True):
        box = self.boxes.get(id)
        if box is None:
            id = None
        if id == self.selected:
            return
        previous = self.boxes.get(self.selected)
        if previous is not None:
            previous.select(False)
        self.selected = id
        if box is not None:
            box.select(True)
            box.raise_()
            if notify and self.on_select:
                self.on_select(id)

    @property
    def is_editing(self):
        return any(box.text.hasFocus() for box in self.boxes.values())

    def focus(self, id):
        box = self.boxes.get(id)
        if box:
            self.select(id)
            box.text.setFocus()
            cursor = box.text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            box.text.setTextCursor(cursor)
            box.flash()

    def mousePressEvent(self, event):
        rect = self.slide_rect()
        if (event.button() == Qt.MouseButton.LeftButton and self.placing and self.store
                and self.scene and rect.contains(event.position())):
            self.end_editing()
            note = self.store.add(self.scene, self.build,
                                  min(.96, max(0, (event.position().x()-rect.x())/rect.width())),
                                  min(.96, max(0, (event.position().y()-rect.y())/rect.height())))
            self.rebuild()
            self.focus(note.id)
            if self.on_add:
                self.on_add()
            event.accept()
        else:
            event.ignore()


class _FeedbackExcerpt(QAbstractButton):
    """A keyboard-accessible row target with at most three lines of user text."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setFont(Theme.font(12))
        self._build_font = Theme.tabular(10)
        self._excerpt = ""
        self._build = ""
        self._empty = True
        self._wrap_width = None
        self._lines = ()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_note(self, note):
        excerpt, build = note.text or "(empty)", f"build {note.build + 1}"
        empty = not note.text
        if self._excerpt == excerpt and self._build == build and self._empty == empty:
            return
        self.setAccessibleName(excerpt)
        self.setAccessibleDescription(build)
        self._excerpt, self._build, self._empty = excerpt, build, empty
        self._wrap_width = None
        self.updateGeometry()
        self.update()

    def _wrapped(self, width):
        width = max(1, width)
        if width == self._wrap_width:
            return self._lines
        self._wrap_width = width
        text = self._excerpt.replace("\n", "\u2028")
        utf16 = text.encode("utf-16-le")
        layout = QTextLayout(text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        lines = []
        for i in range(3):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            start = 2 * line.textStart()
            end = start + 2 * line.textLength()
            caption = utf16[start:end].decode("utf-16-le").removesuffix("\u2028")
            if i == 2 and end < len(utf16):
                caption = self.fontMetrics().elidedText(caption.rstrip() + "…", Qt.TextElideMode.ElideRight, width)
            lines.append(caption)
        layout.endLayout()
        self._lines = lines
        return lines

    def heightForWidth(self, width):
        return self.fontMetrics().lineSpacing() * len(self._wrapped(width)) + 2 + QFontMetrics(self._build_font).height()

    def sizeHint(self):
        return QSize(0, self.heightForWidth(240))

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height() + 2 + QFontMetrics(self._build_font).height())

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.hasFocus():
            option = QStyleOptionFocusRect()
            option.initFrom(self)
            self.style().drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, option, painter, self)
        metrics = self.fontMetrics()
        baseline = metrics.ascent()
        painter.setPen(Theme.tertiary_ink() if self._empty else self.palette().windowText().color())
        for line in self._wrapped(self.width()):
            painter.drawText(0, baseline, line)
            baseline += metrics.lineSpacing()
        painter.setFont(self._build_font)
        painter.setPen(Theme.tertiary_ink())
        painter.drawText(0, baseline - metrics.ascent() + 2 + QFontMetrics(self._build_font).ascent(), self._build)


class _FeedbackRow(QWidget):
    def __init__(self, note, go, remove, parent):
        super().__init__(parent)
        self.selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 10, 6)
        layout.setSpacing(8)
        self.excerpt = _FeedbackExcerpt(self)
        self.excerpt.set_note(note)
        self.excerpt.clicked.connect(lambda: go(note.id))
        layout.addWidget(self.excerpt, 1)
        delete = _symbol_button("trash", "Delete", "Delete this feedback", self)
        delete.setFixedWidth(22)
        delete.setAutoRaise(True)
        delete.setCursor(Qt.CursorShape.ArrowCursor)
        delete.clicked.connect(lambda: remove(note.id))
        layout.addWidget(delete, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_selected(self, selected):
        if self.selected != selected:
            self.selected = selected
            self.update()

    def paintEvent(self, event):
        if self.selected:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Theme.accent_soft)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(6, 1, -6, -1), 5, 5)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.excerpt.click()
            event.accept()
        else:
            super().mousePressEvent(event)


class FeedbackListView(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.slide_label = lambda scene: scene
        self.on_go = self.on_close = None
        self.current = None
        self._rows, self._headings = {}, []
        self._structure = None
        self._ids = []
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 6)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(14, 0, 14, 0)
        header.setSpacing(9)
        icon = _symbol_button("text.bubble", "", "", self, tint=Theme.accent, size=15)
        icon.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        icon.setFixedSize(15, 15)
        icon.setStyleSheet("QToolButton { border: none; padding: 0; background: transparent; }")
        header.addWidget(icon)
        title = QVBoxLayout()
        title.setSpacing(0)
        title_label = QLabel("Feedback")
        title_label.setFont(Theme.font(14, QFont.Weight.DemiBold))
        title.addWidget(title_label)
        self._subtitle = QLabel("red notes from Presenter View")
        self._subtitle.setFont(Theme.mono(10))
        self._subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._subtitle.setWordWrap(True)
        title.addWidget(self._subtitle)
        header.addLayout(title, 1)
        done = QPushButton("Done")
        done.setFont(Theme.font(11))
        done.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        done.setToolTip("Back to the edit panel")
        done.clicked.connect(lambda: self.on_close() if self.on_close else None)
        header.addWidget(done)
        layout.addLayout(header)
        layout.addSpacing(10)
        rule_layout = QHBoxLayout()
        rule_layout.setContentsMargins(14, 0, 14, 0)
        self._rule = hairline(self)
        self._rule.setFrameShape(QFrame.Shape.NoFrame)
        rule_layout.addWidget(self._rule)
        layout.addLayout(rule_layout)
        layout.addSpacing(10)

        controls = QHBoxLayout()
        controls.setContentsMargins(10, 0, 10, 0)
        controls.setSpacing(6)
        self._previous = _symbol_button("chevron.left", "Previous", "Previous feedback", self)
        self._next = _symbol_button("chevron.right", "Next", "Next feedback", self)
        self._clear = QPushButton("Delete All")
        self._clear.setFont(Theme.font(11))
        self._clear.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._previous.clicked.connect(lambda: self.step(-1))
        self._next.clicked.connect(lambda: self.step(1))
        self._clear.clicked.connect(self.delete_all)
        self.counter = QLabel()
        self.counter.setFont(Theme.tabular(12, QFont.Weight.DemiBold))
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addWidget(self._previous)
        controls.addWidget(self.counter, 1)
        controls.addWidget(self._next)
        controls.addWidget(self._clear)
        layout.addLayout(controls)
        layout.addSpacing(8)

        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidgetResizable(True)
        self._scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        self._column = QVBoxLayout(content)
        self._column.setSpacing(2)
        self._scroll.setWidget(content)
        content.setAutoFillBackground(False)
        layout.addWidget(self._scroll, 1)
        self.reload()
        watch_appearance(self._refresh_ink)

    @staticmethod
    def _natural_key(value):
        return [int(part) if i % 2 else part.casefold()
                for i, part in enumerate(re.split(r"(\d+)", value))]

    def _groups(self):
        groups = {}
        for note in self.store.notes:
            groups.setdefault(note.scene, []).append(note)
        labels = {scene: self.slide_label(scene) for scene in groups}
        return [(labels[scene], sorted(groups[scene], key=lambda note: note.created))
                for scene in sorted(groups, key=lambda scene: self._natural_key(labels[scene]))]

    @staticmethod
    def _label_ink(label, ink):
        palette = label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, ink)
        label.setPalette(palette)

    def _refresh_ink(self):
        self._label_ink(self._subtitle, Theme.tertiary_ink())
        ink = Theme.secondary_ink()
        for label in self._headings:
            self._label_ink(label, ink)
        stylesheet = f"background: {Theme.separator().name(QColor.NameFormat.HexArgb)};"
        if self._rule.styleSheet() != stylesheet:
            self._rule.setStyleSheet(stylesheet)
        for row in self._rows.values():
            row.excerpt.update()

    def _rebuild_rows(self, groups):
        while self._column.count():
            widget = self._column.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._rows.clear()
        self._headings.clear()
        self._column.setContentsMargins(0, 0, 0, 6)
        parent = self._scroll.widget()
        if not groups:
            empty = QLabel("No feedback", parent)
            empty.setFont(Theme.font(12))
            empty.setContentsMargins(12, 10, 12, 10)
            self._column.addWidget(empty)
            self._headings.append(empty)
        for title, notes in groups:
            heading = QLabel(title.upper(), parent)
            heading.setTextFormat(Qt.TextFormat.PlainText)
            heading.setFont(Theme.font(10, QFont.Weight.DemiBold))
            heading.setContentsMargins(12, 8, 12, 2)
            heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self._column.addWidget(heading)
            self._headings.append(heading)
            for note in notes:
                row = _FeedbackRow(note, self.go, self.delete_row, parent)
                self._rows[note.id] = row
                self._column.addWidget(row)
        self._column.addStretch(1)
        self._refresh_ink()

    def reload(self):
        groups = self._groups()
        structure = tuple((title, tuple(note.id for note in notes)) for title, notes in groups)
        if structure != self._structure:
            ids = [id for _, group in structure for id in group]
            if self.current is not None and self.current not in ids:
                index = self._ids.index(self.current) if self.current in self._ids else 0
                self.current = ids[min(index, len(ids) - 1)] if ids else None
            self._ids = ids
            self._structure = structure
            self._rebuild_rows(groups)
        else:
            for _, notes in groups:
                for note in notes:
                    self._rows[note.id].excerpt.set_note(note)
        self.update_counter()

    def delete_row(self, id):
        note = self.store.note(id)
        if note and (not note.text or _confirm_removal(self)):
            self.store.remove(id)
            self.reload()

    def delete_all(self):
        if not self.store.is_empty and _confirm_removal(self, all_notes=True):
            self.store.remove_all()
            self.reload()

    def step(self, delta):
        if not self._ids:
            return
        if self.current in self._rows:
            index = (self._ids.index(self.current) + delta) % len(self._ids)
        else:
            index = 0 if delta > 0 else -1
        self.go(self._ids[index])

    def go_next(self):
        self.step(1)

    def go(self, id):
        note = self.store.note(id)
        if note:
            self.select(id)
            if self.on_go:
                self.on_go(note)

    def select(self, id):
        row = self._rows.get(id)
        if row is not None:
            self.current = id
            self.update_counter()
            self._scroll.ensureWidgetVisible(row, 0, 0)

    def update_counter(self):
        count = len(self._ids)
        for id, row in self._rows.items():
            row.set_selected(id == self.current)
        if self.current in self._rows:
            text = f"{self._ids.index(self.current) + 1} of {count}"
        else:
            text = "No feedback" if not count else f"{count} note" + ("" if count == 1 else "s")
        self.counter.setText(text)
        for button in (self._previous, self._next, self._clear):
            button.setEnabled(count > 0)


class FeedbackMixin:
    def _feedback_pane_is_current(self):
        return self.feedback_pane is not None and self.inspector_host.currentWidget() is self.feedback_pane

    def _reset_feedback(self):
        self.hide_feedback_pane()
        if self.feedback_pane is not None:
            pane, self.feedback_pane = self.feedback_pane, None
            self.inspector_host.removeWidget(pane)
            pane.deleteLater()
        if self.feedback is not None:
            self.feedback.on_change = self.feedback.on_error = None
            self.feedback = None
        layer = getattr(self.canvas, "feedback_layer", None)
        if layer is not None:
            layer.placing = False
            layer.store = None
        self.sync_feedback_button()

    def open_feedback(self, ws):
        if self.workspace is not ws:
            return
        self._reset_feedback()
        store = FeedbackStore(Path(ws.work)/"feedback.json", load=False)
        def load():
            store.keep_empty_file = (Path(ws.original)/"feedback.json").exists()
            store.load()
        yield workers.run_blocking(load)
        if self.workspace is not ws:
            return
        self.feedback = store
        store.on_change = self.feedback_changed
        store.on_error = lambda message: self.toast(f"Could not save feedback: {message}", error=True)
        layer = self.canvas.attach_feedback_layer()
        layer.store, layer.scene = store, self.scene_name
        layer.build = self.stop_index
        layer.on_select = self._select_feedback_box
        self.sync_feedback_button()

    def feedback_changed(self):
        layer = getattr(self.canvas, "feedback_layer", None)
        if layer:
            layer.rebuild()
        pane = self.feedback_pane
        if pane and pane.store is self.feedback:
            pane.reload()
            if layer and self._feedback_pane_is_current():
                layer.select(pane.current, notify=False)
        presenter = self.presenter
        if presenter and hasattr(presenter, "feedback_changed"):
            presenter.feedback_changed()
        self.sync_feedback_button()
        if self.feedback is None or self.feedback.is_empty:
            self.hide_feedback_pane()
        self.actions.refresh()

    def _select_feedback_box(self, id):
        pane = self.feedback_pane
        if pane is not None and pane.store is self.feedback:
            pane.select(id)

    def sync_feedback_button(self):
        button = self.format_bar.feedback
        count = len(self.feedback.notes) if self.feedback else 0
        with QSignalBlocker(button):
            button.setEnabled(count > 0)
            button.setText(f"View Feedback ({count})" if count else "View Feedback")
            button.setChecked(bool(count) and self._feedback_pane_is_current())

    def feedback_slide_label(self, scene):
        state = self.state or {}
        for i, slide in enumerate(state.get("scenes", [])):
            if slide["name"] == scene:
                return f"Slide {i+1} · {scene}" + (" (hidden)" if slide.get("hidden") else "")
        if any(s["name"] == scene for s in state.get("unusedScenes", [])):
            return f"Unused slide · {scene}"
        return f"Missing slide · {scene}"

    def view_feedback(self, *_):
        if self._feedback_pane_is_current():
            self.hide_feedback_pane()
            return
        store = self.feedback
        if not store or store.is_empty:
            return
        pane = self.feedback_pane
        if pane is None:
            pane = self.feedback_pane = FeedbackListView(store, self.inspector_host)
            pane.slide_label = self.feedback_slide_label
            pane.on_close = self.hide_feedback_pane
            pane.on_go = lambda note: self._watch(spawn(self._go_feedback(note)).future)
        pane.reload()
        self.show_inspector_page(pane)

    def _go_feedback(self, note):
        pane = self.feedback_pane
        if not self._feedback_pane_is_current() or pane.current != note.id:
            return
        all_scenes = (self.state or {}).get("scenes", []) + (self.state or {}).get("unusedScenes", [])
        if not any(s["name"] == note.scene for s in all_scenes):
            self.toast("That slide no longer exists", error=True)
            return
        if self.scene_name != note.scene or self._shown_scene != note.scene:
            if not (yield from self.select_scene(note.scene)):
                return
        if (not self._feedback_pane_is_current() or self.feedback_pane is not pane
                or pane.current != note.id or self.scene_name != note.scene or self._shown_scene != note.scene):
            return
        stops = (self.scene or {}).get("stops", [])
        if 0 <= note.build < len(stops) and self.stop_index != note.build:
            self.go_to_stop(note.build)
        layer = self.canvas.feedback_layer
        if layer and layer.scene == note.scene:
            layer.focus(note.id)

    def hide_feedback_pane(self):
        if self._feedback_pane_is_current():
            self.show_inspector_page(None)

    def toggle_feedback_mode(self, *_):
        layer = self.canvas.attach_feedback_layer()
        layer.placing = not layer.placing
        self.toast("Click the slide to add private feedback" if layer.placing else "Feedback placement off")

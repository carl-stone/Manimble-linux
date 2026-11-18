"""Private editor/presenter feedback, never painted into audience or exported frames."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QRegion, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget

from . import workers
from .tasks import resolved, spawn, when_done


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

    def set_width(self, id, width):
        note = self.note(id)
        if note:
            note.w = max(0.05, min(1, width))
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
            self.on_focus_change(False)

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
        self.on_delete = self.on_moved = self.on_text_change = self.on_resized = None
        self._drag = None
        self.text = FeedbackTextView(self)
        self.text.setAcceptRichText(False)
        self.text.setFrameStyle(QFrame.Shape.NoFrame)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text.setStyleSheet("QTextEdit { color:#ff2e29; background:transparent; border:none; }")
        self.text.document().setDocumentMargin(0)
        self.text.textChanged.connect(lambda: self.on_text_change() if self.on_text_change else None)
        self.close = QToolButton(self)
        self.close.setText("×")
        self.close.setToolTip("Delete this feedback")
        self.close.setAutoRaise(True)
        self.close.clicked.connect(lambda: self.on_delete() if self.on_delete else None)
        self.setStyleSheet("FeedbackBox { background:rgba(255,46,41,18); border:1px solid #ff2e29; border-radius:4px; }")
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def layout_note(self, width, font_size):
        font = QFont()
        font.setPixelSize(max(8, round(font_size)))
        font.setWeight(QFont.Weight.Medium)
        blocked = self.text.blockSignals(True)
        self.text.setFont(font)
        self.text.blockSignals(blocked)
        inner = max(10, width - 12)
        self.text.document().setTextWidth(inner)
        height = max(font_size * 1.3, self.text.document().size().height()) + 12
        self.resize(round(width), round(height))
        self.text.setGeometry(6, 6, round(inner), max(1, round(height-12)))
        self.close.setGeometry(max(0, round(width)-17), max(0, round(height)-17), 16, 16)
        self.close.raise_()
        return height

    def mousePressEvent(self, event):
        self._drag = (event.globalPosition(), self.pos(), self.width(), event.position().x() >= self.width()-7)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag:
            start, origin, width, resizing = self._drag
            delta = event.globalPosition()-start
            if resizing:
                self.resize(max(60, width+round(delta.x())), self.height())
            else:
                self.move(origin + delta.toPoint())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag:
            resizing = self._drag[3]
            self._drag = None
            if resizing and self.on_resized:
                self.on_resized(self.width())
            elif self.on_moved:
                self.on_moved(QPointF(self.pos()))
            event.accept()


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
        self.on_add = None
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
        self._store = store
        self.rebuild()

    @property
    def scene(self):
        return self._scene

    @scene.setter
    def scene(self, scene):
        if scene != self._scene:
            self.end_editing()
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
                box = self.boxes.pop(id)
                box.hide()
                box.deleteLater()
        for note in wanted:
            box = self.boxes.get(note.id)
            if box is None:
                box = self._make_box(note)
                self.boxes[note.id] = box
            elif not box.text.hasFocus() and box.text.toPlainText() != note.text:
                box.text.setPlainText(note.text)
        self.relayout()

    def _make_box(self, note):
        box = FeedbackBox(note.id, self)
        box.text.setPlainText(note.text)
        box.on_delete = lambda: self.remove(note.id)
        box.on_text_change = lambda: self._text_changed(note.id)
        box.text.on_end = self.end_editing
        box.text.on_delete_empty = lambda: self.remove(note.id)
        def focus_change(focused):
            if not focused and self.store:
                self.store.set_text(note.id, box.text.toPlainText())
                if not box.text.toPlainText():
                    QTimer.singleShot(0, lambda: self.remove(note.id) if not box.text.hasFocus() and not box.text.toPlainText() else None)
        box.text.on_focus_change = focus_change
        def moved(point):
            rect = self.slide_rect()
            if self.store and rect.width() > 0 and rect.height() > 0:
                self.store.move(note.id, min(.98, max(0, (point.x()-rect.x())/rect.width())),
                                min(.98, max(0, (point.y()-rect.y())/rect.height())))
            self.relayout()
        box.on_moved = moved
        def resized(width):
            rect = self.slide_rect()
            if self.store and rect.width() > 0:
                self.store.set_width(note.id, width/rect.width())
            self.relayout()
        box.on_resized = resized
        box.show()
        return box

    def _text_changed(self, id):
        box = self.boxes.get(id)
        if box and self.store:
            # Keep the memory snapshot current even before focus leaves, for Save/close.
            self.store.set_text(id, box.text.toPlainText())
            self.relayout(id)

    def relayout(self, only=None):
        rect = self.slide_rect()
        if rect.width() > 0 and rect.height() > 0 and self.store:
            for id, box in self.boxes.items():
                if only is not None and only != id:
                    continue
                note = self.store.note(id)
                if note:
                    box.layout_note(max(60, rect.width()*note.w), self.font_size(rect))
                    box.move(round(rect.x()+rect.width()*note.x), round(rect.y()+rect.height()*note.y))
        self._update_mask()

    def remove(self, id):
        box = self.boxes.pop(id, None)
        if box:
            box.text.on_focus_change = None
            box.hide()
            box.deleteLater()
        if self.store:
            self.store.remove(id)
        self._update_mask()

    def end_editing(self):
        for box in list(self.boxes.values()):
            if box.text.hasFocus():
                box.text.clearFocus()
        if self.fallback_responder:
            self.fallback_responder.setFocus()

    @property
    def is_editing(self):
        return any(box.text.hasFocus() for box in self.boxes.values())

    def focus(self, id):
        box = self.boxes.get(id)
        if box:
            box.text.setFocus()
            cursor = box.text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            box.text.setTextCursor(cursor)

    def mousePressEvent(self, event):
        rect = self.slide_rect()
        if self.placing and self.store and self.scene and rect.contains(event.position()):
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


class FeedbackListView(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.slide_label = lambda scene: scene
        self.on_go = self.on_close = None
        self.current = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Feedback — private red notes"), 1)
        done = QPushButton("Done")
        done.clicked.connect(lambda: self.on_close() if self.on_close else None)
        row.addWidget(done)
        layout.addLayout(row)
        row = QHBoxLayout()
        previous, next_, clear = QPushButton("Previous"), QPushButton("Next"), QPushButton("Delete All")
        previous.clicked.connect(lambda: self.step(-1))
        next_.clicked.connect(lambda: self.step(1))
        clear.clicked.connect(self.delete_all)
        self.counter = QLabel()
        for widget in (previous, self.counter, next_, clear):
            row.addWidget(widget)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.itemClicked.connect(lambda item: self.go(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.list)
        self.reload()

    @property
    def ordered(self):
        def natural(value):
            return [int(p) if p.isdigit() else p.casefold() for p in __import__("re").split(r"(\d+)", value)]
        return sorted(self.store.notes, key=lambda n: (natural(self.slide_label(n.scene)), n.created))

    def reload(self):
        self.list.clear()
        for note in self.ordered:
            item = QListWidgetItem(self.list)
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            row = QWidget()
            layout = QHBoxLayout(row)
            button = QPushButton(f"{self.slide_label(note.scene)} · build {note.build+1}\n{note.text or '(empty)'}")
            button.clicked.connect(lambda checked=False, id=note.id: self.go(id))
            button.setFlat(True)
            layout.addWidget(button, 1)
            delete = QToolButton()
            delete.setText("×")
            delete.setToolTip("Delete this feedback")
            delete.clicked.connect(lambda checked=False, id=note.id: self.delete_row(id))
            layout.addWidget(delete)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        if not self.store.note(self.current):
            self.current = None
        self.update_counter()

    def delete_row(self, id):
        self.store.remove(id)
        self.reload()

    def delete_all(self):
        result = QMessageBox.question(self, "Delete all feedback?", "Remove every private feedback note from this deck?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if result == QMessageBox.StandardButton.Yes:
            self.store.remove_all()
            self.reload()

    def step(self, delta):
        notes = self.ordered
        if not notes:
            return
        index = next((i for i, n in enumerate(notes) if n.id == self.current), None)
        self.go(notes[((index+delta) % len(notes)) if index is not None else (0 if delta > 0 else len(notes)-1)].id)

    def go_next(self):
        self.step(1)

    def go(self, id):
        note = self.store.note(id)
        if note:
            self.current = id
            self.update_counter()
            if self.on_go:
                self.on_go(note)
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == id:
                    self.list.setCurrentRow(i)
                    self.list.scrollToItem(self.list.item(i))
                    break

    def update_counter(self):
        notes = self.ordered
        index = next((i for i, n in enumerate(notes) if n.id == self.current), None)
        self.counter.setText(f"{index+1} of {len(notes)}" if index is not None else f"{len(notes)} notes" if notes else "No feedback")


class FeedbackMixin:
    def open_feedback(self, ws):
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
        self.sync_feedback_button()

    def feedback_changed(self):
        layer = getattr(self.canvas, "feedback_layer", None)
        if layer:
            layer.rebuild()
        pane = self.feedback_pane
        if pane:
            pane.reload()
        presenter = self.presenter
        if presenter and hasattr(presenter, "feedback_changed"):
            presenter.feedback_changed()
        self.sync_feedback_button()
        if self.feedback.is_empty:
            self.hide_feedback_pane()
        self.actions.refresh()

    def sync_feedback_button(self):
        button = self.format_bar.feedback
        count = len(self.feedback.notes) if self.feedback else 0
        button.setEnabled(count > 0)
        button.setText(f"View Feedback ({count})" if count else "View Feedback")

    def feedback_slide_label(self, scene):
        state = self.state or {}
        for i, slide in enumerate(state.get("scenes", [])):
            if slide["name"] == scene:
                return f"Slide {i+1} · {scene}" + (" (hidden)" if slide.get("hidden") else "")
        if any(s["name"] == scene for s in state.get("unusedScenes", [])):
            return f"Unused slide · {scene}"
        return f"Missing slide · {scene}"

    def view_feedback(self, *_):
        pane = self.feedback_pane
        if pane and not pane.isHidden():
            self.hide_feedback_pane()
            return
        store = self.feedback
        if not store or store.is_empty:
            return
        if pane is None or pane.store is not store:
            pane = self.feedback_pane = FeedbackListView(store)
        pane.slide_label = self.feedback_slide_label
        pane.on_close = self.hide_feedback_pane
        pane.on_go = lambda note: spawn(self._go_feedback(note))
        pane.reload()
        self.show_inspector_page(pane)

    def _go_feedback(self, note):
        all_scenes = (self.state or {}).get("scenes", []) + (self.state or {}).get("unusedScenes", [])
        if not any(s["name"] == note.scene for s in all_scenes):
            self.toast("That slide no longer exists", error=True)
            return
        if self.scene_name != note.scene:
            yield from self.select_scene(note.scene)
        self.go_to_stop(note.build)
        layer = self.canvas.attach_feedback_layer()
        layer.store, layer.scene, layer.build = self.feedback, self.scene_name, self.stop_index
        layer.focus(note.id)

    def hide_feedback_pane(self):
        pane = self.feedback_pane
        if pane:
            pane.hide()
            self.show_inspector_page(None)

    def toggle_feedback_mode(self, *_):
        layer = self.canvas.attach_feedback_layer()
        layer.placing = not layer.placing
        self.toast("Click the slide to add private feedback" if layer.placing else "Feedback placement off")

"""Deck-wide engine search, replacement, and glyph highlights."""
from __future__ import annotations

from dataclasses import dataclass, fields

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton, QVBoxLayout, QWidget, QTextEdit

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


class FindButton(QToolButton):
    def __init__(self, title, tip, toggle=False, parent=None):
        super().__init__(parent)
        self.setText(title)
        self.setToolTip(tip)
        self.setAutoRaise(True)
        self.setCheckable(toggle)


class FindInput(QFrame):
    def __init__(self, field, buttons=(), parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 2, 0)
        row.setSpacing(1)
        row.addWidget(field, 1)
        for button in buttons:
            row.addWidget(button)
        self.setFrameShape(QFrame.Shape.StyledPanel)


class FindBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.field = LiveTextField()
        self.field.setPlaceholderText("Find")
        self.replace_field = LiveTextField()
        self.replace_field.setPlaceholderText("Replace")
        self.case_button = FindButton("Aa", "Match case", True)
        self.word_button = FindButton("ab", "Whole word", True)
        self.regex_button = FindButton(".*", r"Python regular expression; $1 or \1 replaces a group", True)
        self.notes_button = FindButton("Notes", "Include slide and build notes", True)
        self.prev = FindButton("↑", "Previous match (Shift+Return)")
        self.next = FindButton("↓", "Next match (Return)")
        self.close = FindButton("×", "Clear search (Escape)")
        self.replace_button = FindButton("▸", "Toggle replace", True)
        self.replace_one = FindButton("Replace", "Replace the selected editable match")
        self.replace_all = FindButton("All", "Replace all editable matches in one undo step")
        self.count = QLabel()
        self.find_box = FindInput(self.field, [self.case_button, self.word_button, self.regex_button, self.notes_button])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for w in [self.replace_button, self.find_box, self.count, self.prev, self.next, self.close]:
            row.addWidget(w)
        layout.addLayout(row)
        self.replace_row = QWidget()
        replace = QHBoxLayout(self.replace_row)
        replace.setContentsMargins(20, 0, 0, 0)
        self.replace_box = FindInput(self.replace_field)
        replace.addWidget(self.replace_box, 1)
        replace.addWidget(self.replace_one)
        replace.addWidget(self.replace_all)
        layout.addWidget(self.replace_row)
        self.replace_row.hide()
        self.matches = []
        self.current = None
        self.shown_key = None
        self.generation = 0
        self.on_update = None
        self.update()

    @property
    def options(self):
        return {"query": self.field.text(), "case": self.case_button.isChecked(), "word": self.word_button.isChecked(),
                "regex": self.regex_button.isChecked(), "notes": self.notes_button.isChecked()}

    @property
    def replace_shown(self):
        return not self.replace_row.isHidden()

    def set_replace_shown(self, on):
        self.replace_row.setVisible(on)
        self.replace_button.setChecked(on)
        self.replace_button.setText("▾" if on else "▸")

    def update(self, error=None):
        has = bool(self.matches)
        for button in [self.prev, self.next, self.replace_one, self.replace_all]:
            button.setEnabled(has)
        self.count.setText("Error" if error else f"{self.current + 1 if self.current is not None else '?'} of {len(self.matches)}" if has else "No results")
        self.count.setToolTip(error or "")
        self.field.setToolTip(error or "Return: next; Shift+Return: previous; Escape: clear")
        if self.on_update:
            self.on_update()
        super().update()


class FindMixin:
    def init_find(self):
        self.find_bar = FindBar()
        self.wire_find_bar(self.find_bar)
        bar = self.format_bar
        if self.find_bar.parentWidget() is None and bar.layout() is not None:
            bar.layout().addWidget(self.find_bar)

    def wire_find_bar(self, f):
        self.find_bar = f
        timer = QTimer(f)
        timer.setSingleShot(True)
        timer.setInterval(150)
        timer.timeout.connect(lambda: spawn(self.run_find(False)))
        f._search_timer = timer
        f.field.textChanged.connect(lambda _: timer.start())
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
            self.find_bar.set_replace_shown(False)
        else:
            self.show_find_replace()

    def close_find(self, *_):
        f = self.find_bar
        f.generation += 1
        f.field.clear()
        f.matches = []
        f.current = None
        f.update()
        f.set_replace_shown(False)
        self.canvas.setFocus()

    def find_field_changed(self, *_):
        spawn(self.run_find(False))

    def find_option_changed(self, *_):
        spawn(self.run_find(False))

    def run_find(self, keep=False):
        f = self.find_bar
        f.generation += 1
        generation = f.generation
        before = f.matches[f.current] if f.current is not None and f.current < len(f.matches) else None
        if not f.field.text() or self.workspace is None:
            f.matches, f.current = [], None
            f.update()
            return
        try:
            reply = yield self.call("find", f.options)
            if generation != f.generation:
                return
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "Search failed"))
            f.matches = [FindMatch.from_json(m) for m in reply.get("matches", [])]
            f.current = None
            if keep and before:
                f.current = next((i for i, m in enumerate(f.matches) if m.key == before.key), None)
                if f.current is None:
                    f.current = self.first_match_at_or_after(before)
            f.update()
        except Exception as exc:
            if generation == f.generation:
                f.matches, f.current = [], None
                f.update(str(exc))

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
        drawer = self.drawer
        page = getattr(drawer, "page", None)
        return (self.find_bar.shown_key == m.key and self.stop_index == m.stop
                and not drawer.isHidden() and page in (m.kind, "build_notes" if m.kind == "buildNotes" else "notes"))

    def find_next(self, *_):
        spawn(self.go_to_match(True))

    def find_previous(self, *_):
        spawn(self.go_to_match(False))

    def go_to_match(self, forward=True):
        f = self.find_bar
        if not f.matches:
            yield from self.run_find(False)
        if not f.matches:
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
        if self.scene_name != m.scene:
            yield from self.select_scene(m.scene)
        if self.scene_name != m.scene:
            return
        self.go_to_stop(m.stop)
        if m.in_notes:
            self.selection = None
            self.drawer.show_page("notes" if m.kind == "notes" else "build_notes")
        else:
            self.selection = m.oid
            self.canvas.select_group([m.oid])
        f.current, f.shown_key = index, m.key
        f.update()

    def replace_request(self, match=None, all=False):
        request = dict(self.find_bar.options, replacement=self.find_bar.replace_field.text())
        if all:
            request["all"] = True
        elif match:
            request.update(scene=match.scene, oid=match.oid, index=match.index, stop=match.stop, kind=match.kind)
        return request

    def replace_current(self, *_):
        if not self.find_bar.field.text():
            return
        def work():
            yield from self.run_find(True)
            f = self.find_bar
            if f.current is None or not self.shown_match(f.matches[f.current]):
                yield from self.go_to_match(True)
                return
            m = f.matches[f.current]
            if not m.editable:
                self.toast("This text is made by code; edit it in the Python source", error=True)
                yield from self.go_to_match(True)
                return
            reply = yield self.call("replace", self.replace_request(m))
            if not reply.get("ok"):
                self.toast(reply.get("error", "Replace failed"), error=True)
                return
            yield from self.load_scene(m.scene, True)
            yield from self.reload_state()
            yield from self.run_find(True)
            if f.current is not None and f.matches[f.current].key == m.key:
                f.current = (f.current + 1) % len(f.matches)
            if f.current is not None:
                yield from self.show_match(f.current)
            self.toast(reply.get("message", "Replaced"))
        return self.submit(work)

    def replace_everything(self, *_):
        if not self.find_bar.field.text():
            return
        request = self.replace_request(all=True)
        def work():
            reply = yield self.call("replace", request)
            if not reply.get("ok"):
                self.toast(reply.get("error", "Replace failed"), error=True)
                return
            if self.scene_name:
                yield from self.load_scene(self.scene_name, True)
            yield from self.reload_state()
            yield from self.run_find(False)
            self.toast(reply.get("message", "Replaced"))
        return self.submit(work)

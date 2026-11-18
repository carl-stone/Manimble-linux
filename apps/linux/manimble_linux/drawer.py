"""The single bottom drawer: captured-target notes, terminal and script pages."""
from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget
from . import prefs
from .tasks import spawn
from .note_images import NoteImages


class NoteBox(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_begin = self.on_commit = None
        self.target = None
        self.image_base = None
        self._baseline = ''
        self._editing = False
        self.setAcceptRichText(False)

    @property
    def is_editing(self):
        return self._editing

    def set_text(self, text):
        if not self._editing:
            document = NoteImages.attributed(text, self.image_base)
            document.setParent(self)
            self.setDocument(document)
            self._baseline = text

    def focusInEvent(self, event):
        if not self._editing:
            self._editing = True
            self._baseline = NoteImages.markup(self)
            if self.on_begin:
                self.target = self.on_begin()
        super().focusInEvent(event)

    def commit(self):
        if not self._editing:
            return
        self._editing = False
        text = NoteImages.markup(self)
        if text != self._baseline and self.on_commit:
            self.on_commit(self.target, text)
        self._baseline = text

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.commit()

    def insertFromMimeData(self, mime):
        # Copy clipboard data before yielding; the owning clipboard can change meanwhile.
        from PySide6.QtCore import QMimeData
        snapshot = QMimeData()
        for fmt in mime.formats():
            snapshot.setData(fmt, mime.data(fmt))
        if mime.hasImage():
            snapshot.setImageData(mime.imageData())
        def paste():
            if self.image_base is not None:
                paths = yield from NoteImages.import_images(snapshot, self.image_base)
                if paths:
                    NoteImages.insert(paths, self, self.image_base)
                    return
            self.insertPlainText(snapshot.text())
        spawn(paste())


class BottomDrawer(QWidget):
    def __init__(self, notes, build_notes, alt_text, terminal=None, script=None, parent=None):
        super().__init__(parent)
        self.will_change_page = self.on_show_terminal = self.on_new_terminal = self.on_hide = self.on_insert_image = None
        self.tabs = QTabWidget()
        self.pages = {'notes': notes, 'build_notes': build_notes, 'alt_text': alt_text}
        for key, title in [('notes', 'Slide Notes'), ('build_notes', 'Build Notes'), ('alt_text', 'Alt Text')]:
            self.tabs.addTab(self.pages[key], title)
        if terminal is not None:
            self.add_page('terminal', terminal, 'Terminal')
        if script is not None:
            self.add_page('script', script, 'Script')
        self.build_label = QLabel()
        image = QPushButton('Insert Image…')
        image.clicked.connect(lambda: self.on_insert_image() if self.on_insert_image else None)
        hide = QPushButton('Hide')
        hide.clicked.connect(lambda: self.on_hide() if self.on_hide else self.hide())
        bar = QHBoxLayout()
        bar.addWidget(self.build_label)
        bar.addStretch()
        bar.addWidget(image)
        bar.addWidget(hide)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self._changed)

    def add_page(self, key, widget, title):
        self.pages[key] = widget
        self.tabs.addTab(widget, title)

    @property
    def page(self):
        widget = self.tabs.currentWidget()
        return next((k for k, v in self.pages.items() if v is widget), None)

    def show_page(self, page):
        if page not in self.pages:
            raise ValueError(f'Drawer page unavailable: {page}')
        self.tabs.setCurrentWidget(self.pages[page])
        self.show()

    def _changed(self, _):
        if self.will_change_page:
            self.will_change_page(self.page)
        if self.page == 'terminal' and self.on_show_terminal:
            self.on_show_terminal()


class NotesMixin:
    def build_notes_pane(self):
        self.notes_box = NoteBox()
        self.build_notes_box = NoteBox()
        self.alt_text_box = NoteBox()
        self.notes_pane, self.build_notes_view, self.alt_text_view = self.notes_box, self.build_notes_box, self.alt_text_box
        self.build_notes_for = None
        self.notes_box.on_begin = lambda: (self.scene_name, None)
        self.build_notes_box.on_begin = lambda: (self.scene_name, self.stop_index)
        self.alt_text_box.on_begin = lambda: (self.scene_name, None)
        self.notes_box.on_commit = lambda target, text: self.commit_notes(target, text)
        self.build_notes_box.on_commit = lambda target, text: self.commit_notes(target, text)
        self.alt_text_box.on_commit = lambda target, text: self.commit_notes(target, text, alt=True)

    @property
    def drawer_editing(self):
        return any(box.is_editing for box in (self.notes_box, self.build_notes_box, self.alt_text_box))

    def commit_drawer(self):
        for box in (self.notes_box, self.build_notes_box, self.alt_text_box):
            box.commit()

    def insert_notes_image(self):
        box = self.build_notes_box if self.drawer.page == 'build_notes' else self.notes_box
        if self.work_deck is None:
            return
        target = self.work_deck
        def choose():
            paths = yield from NoteImages.choose(target, self.window)
            if paths and self.work_deck == target:
                box.setFocus()
                NoteImages.insert(paths, box, target)
        spawn(choose())

    def commit_notes(self, target, text, alt=False):
        """Save speaker notes (stop None), one build's notes, or the alt text, for the target captured when editing
        began (SlideTools.swift commitNotes / commitBuildNotes / commitAltText)."""
        if not target or not target[0]:
            return
        name, stop = target
        args = {'op': 'set', 'scene': name, 'text': text}
        if stop is not None:
            args['stop'] = stop
        what = 'the alt text' if alt else ('the build notes' if stop is not None else 'the notes')
        saved = 'Alt text saved' if alt else (f'Notes saved for build {stop + 1}' if stop is not None else 'Speaker notes saved')
        def run():
            self.busy += 1
            try:
                result = yield self.call('alt_text' if alt else 'notes', args)
            finally:
                self.busy -= 1
            if not result.get('ok'):
                self.toast(result.get('error') or f'could not save {what}', error=True)
                if stop is not None and name == self.scene_name:      # back to what is saved
                    yield from self.load_scene(name, keep_stop=True, keep_overrides=True)
                return
            self.last_changed = result.get('changed')
            self.toast('No change' if result.get('message') == 'no change' else saved)
            if name == self.scene_name:
                yield from self.load_scene(name, keep_stop=True, keep_overrides=True)
            yield from self.reload_state()
        self.submit(run)

    @property
    def drawer_shows_notes(self):
        return self.drawer.isVisible() and self.drawer.page in ('notes', 'build_notes', 'alt_text')

    @property
    def drawer_shows_terminal(self):
        return self.drawer.isVisible() and self.drawer.page == 'terminal'

    def set_drawer_open(self, on, min_height=110):
        if not on:
            self.commit_drawer()
        self.drawer.setVisible(on)
        if on:
            self.center_split.setSizes([max(160, self.center_split.height() - min_height), min_height])
        prefs.set('showNotes', on)

    def toggle_notes_pane(self):
        if self.drawer_shows_notes:
            self.set_drawer_open(False)
        else:
            self.drawer.show_page('notes')
            self.set_drawer_open(True)
            self.notes_box.setFocus()

    def toggle_build_notes(self):
        self.drawer.show_page('build_notes')
        self.set_drawer_open(True)
        self.build_notes_box.setFocus()

    def toggle_alt_text(self):
        self.drawer.show_page('alt_text')
        self.set_drawer_open(True)
        self.alt_text_box.setFocus()

    def show_terminal_page(self):
        if self.workspace is None:
            return
        self.drawer.show_page('terminal')
        self.set_drawer_open(True, max(110, int(self.center_split.height() * .42)))
        if not self.terminal.has_started:
            self.terminal.start(self.work_deck, original=self.original_deck)
        self.terminal.focus()

    def toggle_terminal(self):
        if self.drawer_shows_terminal:
            self.set_drawer_open(False)
        else:
            self.show_terminal_page()

    def new_terminal(self):
        started = self.terminal.has_started
        self.show_terminal_page()
        if started:
            self.terminal.add_tab()

    def split_terminal(self):
        started = self.terminal.has_started
        self.show_terminal_page()
        if started:
            self.terminal.split_active()

"""One drawer for speaker notes, alt text and local shells."""
from __future__ import annotations
from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QScrollArea,
    QSizePolicy, QStackedWidget, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from . import prefs
from .icons import template_icon
from .style import Theme, is_dark, watch_appearance
from .tasks import spawn
from .note_images import NoteImages


class NoteBox(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_begin = self.on_commit = None
        self.target = None
        self.image_base = None
        self._baseline = ''
        self._rendered_target = self._rendered_base = None
        self._picture_width = None
        self._text_generation = 0
        self._editing = self._rendering = False
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportMargins(0, 2, 0, 2)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFont(Theme.font(13))
        self.document().setDocumentMargin(2)
        self.textChanged.connect(self._text_changed)
        watch_appearance(self._refresh_appearance)
        self._refresh_appearance()

    def _refresh_appearance(self):
        paper = QColor.fromRgbF(.07, .07, .07) if is_dark() else QColor('white')
        ink = Theme.ink(.92)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, paper)
        palette.setColor(QPalette.ColorRole.Text, ink)
        self.setPalette(palette)
        self.setStyleSheet(f'QTextEdit {{ background: {paper.name()}; border: 0; border-radius: 6px; }}')

    def _text(self):
        return NoteImages.markup(self) if self.image_base is not None else self.toPlainText()

    def _show_text(self, text):
        self._text_generation += 1
        self._rendering = True
        try:
            if self.image_base is not None and NoteImages.has(text):
                self._picture_width = NoteImages.editor_width(self)
                document = NoteImages.attributed(text, self.image_base, max_width=self._picture_width)
                document.setDefaultFont(self.font())
                document.setDocumentMargin(2)
                document.setParent(self)
                self.setDocument(document)
            else:
                self._picture_width = None
                self.setPlainText(text)
            self.document().clearUndoRedoStacks()
        finally:
            self._rendering = False
        self._rendered_base = self.image_base
        self._rendered_target = self._current_target()

    def showEvent(self, event):
        super().showEvent(event)
        # A hidden page's viewport may not yet have its real width.
        if (not self._editing and self._picture_width is not None and
                self._picture_width != NoteImages.editor_width(self)):
            self._show_text(self._baseline)

    @property
    def is_editing(self):
        return self._editing

    def set_text(self, text):
        if not self._editing and (text != self._baseline or self.image_base != self._rendered_base or
                                  self._current_target() != self._rendered_target):
            self._show_text(text)
            self._baseline = text

    def _current_target(self):
        return self.on_begin() if self.on_begin else None

    def _begin(self):
        if not self._editing:
            self._editing = True
            self.target = self._current_target()

    def _text_changed(self):
        # A build change can commit a focused editor and replace its document
        # without sending a focus event. Capture the new target on its first edit.
        if not self._rendering:
            self._begin()

    def focusInEvent(self, event):
        self._begin()
        super().focusInEvent(event)

    def begin_editing(self):
        self.setFocus()
        self.selectAll()

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods == Qt.KeyboardModifier.ControlModifier:
            self.commit(keep_editing=True)
            return
        if key == Qt.Key.Key_Escape:
            self._editing = False
            self._show_text(self._baseline)
            self.clearFocus()
            return
        super().keyPressEvent(event)

    def commit(self, *, keep_editing=False):
        if not self._editing:
            return
        self._editing = keep_editing
        text = self._text()
        if text == self._baseline:
            return
        self._baseline = text
        if self.on_commit:
            self.on_commit(self.target, text)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.commit()

    def picture_target(self):
        return self.image_base, self._text_generation, self._current_target()

    def insert_pictures(self, paths, target, cursor):
        if not paths or self.picture_target() != target or not self.isVisible():
            return
        self.setFocus()
        self.setTextCursor(cursor)
        NoteImages.insert(paths, self, target[0], max_width=NoteImages.editor_width(self))

    def canInsertFromMimeData(self, mime):
        return mime.hasText() or (self.image_base is not None and (
            mime.hasImage() or mime.hasUrls() or mime.hasFormat('image/png') or mime.hasFormat('image/tiff')))

    def dropEvent(self, event):
        if self.canInsertFromMimeData(event.mimeData()):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().dropEvent(event)

    def insertFromMimeData(self, mime):
        base = self.image_base
        try:
            pictures = NoteImages.pictures(mime) if base is not None else ()
        except ValueError as error:
            self._picture_error(error)
            return
        if not pictures:
            if mime.hasText():
                self.insertPlainText(mime.text())
            return
        target, cursor = self.picture_target(), self.textCursor()
        def paste():
            try:
                paths = yield from NoteImages.import_pictures(pictures, base)
                if self.hasFocus():
                    self.insert_pictures(paths, target, cursor)
            except (OSError, ValueError) as error:
                self._picture_error(error)
        spawn(paste(), name='note pictures')

    def _picture_error(self, error):
        from .widgets import alert
        alert('Could not insert note pictures', str(error), self)


class DrawerIconButton(QToolButton):
    def __init__(self, symbol, tip, parent, *, size=11, width=20, name=None):
        super().__init__(parent)
        self.setIcon(template_icon(symbol, self))
        self.setIconSize(QSize(size, size))
        self.setFixedSize(width, 24)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(tip)
        self.setAccessibleName(name or tip)
        watch_appearance(self._refresh_appearance)
        self._refresh_appearance()

    def _refresh_appearance(self):
        palette = self.palette()
        palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText, Theme.ink(.60))
        palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.ButtonText, Theme.ink(.60))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, Theme.ink(.25))
        self.setPalette(palette)


class _DrawerTab(QWidget):
    def __init__(self, activate, close=None):
        super().__init__()
        self.setFixedHeight(28)
        self._activate = activate
        self.selected = False
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 8 if close is not None else 6, 0)
        row.setSpacing(4)
        self.label = QToolButton(self)
        self.label.setFixedHeight(26)
        self.label.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.label.setAutoRaise(True)
        self.label.setStyleSheet('QToolButton { border: 0; padding: 0; }'
                                 'QToolButton:focus { border-bottom: 1px dotted palette(button-text); }')
        self.label.clicked.connect(lambda: activate())
        row.addWidget(self.label)
        self.close = None
        if close is not None:
            self.close = DrawerIconButton('xmark', 'Kill terminal', self, size=8, width=12, name='Close')
            self.close.clicked.connect(lambda: close())
            self._opacity = QGraphicsOpacityEffect(self.close)
            self._opacity.setOpacity(0)
            self.close.setGraphicsEffect(self._opacity)
            self.close.installEventFilter(self)
            row.addWidget(self.close)
        watch_appearance(self._refresh_appearance)
        self._refresh_appearance()

    def set_tab(self, title, tip, selected):
        if title != self.label.text():
            self.label.setText(title)
            self.label.setAccessibleName(title)
        if tip != self.label.toolTip():
            self.label.setToolTip(tip)
            self.setToolTip(tip)
        if self.selected != selected:
            self.selected = selected
            self._refresh_appearance()

    def _refresh_appearance(self):
        self.label.setFont(Theme.font(11, QFont.Weight.DemiBold if self.selected else QFont.Weight.Normal))
        palette = self.label.palette()
        palette.setColor(QPalette.ColorRole.ButtonText, Theme.ink(.9 if self.selected else .5))
        self.label.setPalette(palette)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._activate()
            event.accept()
        else:
            super().mousePressEvent(event)

    def enterEvent(self, event):
        if self.close is not None:
            self._opacity.setOpacity(1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.close is not None and not self.close.hasFocus():
            self._opacity.setOpacity(0)
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.close:
            if event.type() == QEvent.Type.FocusIn:
                self._opacity.setOpacity(1)
            elif event.type() == QEvent.Type.FocusOut and not self.underMouse():
                self._opacity.setOpacity(0)
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        if self.selected:
            with QPainter(self) as painter:
                painter.fillRect(4, 26, self.width() - 8, 2, Theme.accent)


class _DrawerStrip(QScrollArea):
    def __init__(self, parent):
        super().__init__(parent)
        self._active_tab = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.row = QHBoxLayout(content)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.setWidget(content)
        self.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(28)

    def sizeHint(self):
        return self.row.sizeHint()

    def minimumSizeHint(self):
        return QSize(0, 28)

    def reveal(self, tab):
        self._active_tab = tab
        if tab is not None:
            self.ensureWidgetVisible(tab, 0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._active_tab is not None:
            self.ensureWidgetVisible(self._active_tab, 0, 0)

    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        delta = event.pixelDelta().x() or event.pixelDelta().y()
        if not delta:
            delta = (event.angleDelta().x() or event.angleDelta().y()) // 2
        bar.setValue(bar.value() - delta)
        event.accept()


class _DrawerHint(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFont(Theme.font(10))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height())

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.setPen(Theme.ink(.4))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width()))


class BottomDrawer(QWidget):
    def __init__(self, notes, build_notes, alt_text, terminal, parent=None):
        super().__init__(parent)
        self.will_change_page = self.on_show_terminal = self.on_new_terminal = self.on_hide = self.on_insert_image = None
        self._page = 'notes'
        self._build_label = ''
        self._terminal_shortcut = 'Ctrl+Alt+T'
        self.terminal = terminal
        self.setMinimumHeight(90)
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QWidget(self)
        self.header.setFixedHeight(28)
        self.header.setAutoFillBackground(True)
        row = QHBoxLayout(self.header)
        row.setContentsMargins(4, 0, 12, 0)
        row.setSpacing(10)
        tabs = QWidget(self.header)
        tab_row = QHBoxLayout(tabs)
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(6)
        self.strip = _DrawerStrip(tabs)
        tab_row.addWidget(self.strip)
        self.plus = DrawerIconButton('plus', 'Add a terminal (Ctrl+Alt+Shift+T)', tabs)
        self.plus.clicked.connect(self._new_terminal)
        tab_row.addWidget(self.plus)
        row.addWidget(tabs)
        self.hint = _DrawerHint(self.header)
        row.addWidget(self.hint, 1)
        self.log_title = QLabel('Engine Log', self.header)
        self.log_title.setFont(Theme.font(11))
        row.addWidget(self.log_title)
        self.image = DrawerIconButton('photo.badge.plus', 'Put a picture in the notes (or paste or drop one in)', self.header)
        self.image.clicked.connect(lambda: self.on_insert_image() if self.on_insert_image else None)
        row.addWidget(self.image)
        terminal.actions.setParent(self.header)
        row.addWidget(terminal.actions)
        self.hide_button = DrawerIconButton('chevron.down', 'Hide (Ctrl+Alt+K notes, Ctrl+Alt+T terminal)', self.header)
        self.hide_button.clicked.connect(lambda: self.on_hide() if self.on_hide else self.hide())
        row.addWidget(self.hide_button)
        layout.addWidget(self.header)

        self.body = QStackedWidget(self)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._pages = {'terminal': terminal}
        specs = (
            ('notes', notes, 'Slide Notes', 'Speaker notes for every build of this slide (Ctrl+Alt+K)'),
            ('build_notes', build_notes, 'Build Notes', 'Speaker notes for the build on screen only'),
            ('alt_text', alt_text, 'Alt Text', 'A description of this slide for people who cannot see it'),
        )
        self._note_tabs = {}
        self._terminal_tabs = []
        self._terminal_state = None
        for key, editor, title, tip in specs:
            pane = QWidget(self.body)
            inset = QVBoxLayout(pane)
            inset.setContentsMargins(12, 8, 12, 8)
            inset.addWidget(editor)
            self.body.addWidget(pane)
            self._pages[key] = pane
            tab = _DrawerTab(lambda k=key: self.show_page(k))
            self._note_tabs[key] = (tab, title, tip)
            self.strip.row.addWidget(tab)
        self.body.addWidget(terminal)
        layout.addWidget(self.body, 1)
        terminal.tabs_changed.connect(self._sync_terminal_tabs)
        self._update_page()
        watch_appearance(self._refresh_appearance)
        self._refresh_appearance()
        QTimer.singleShot(0, self._refresh_shortcuts)

    def _refresh_appearance(self):
        for widget, color in ((self, Theme.canvas_bottom()), (self.header, Theme.bar_background()),
                              (self.strip.viewport(), Theme.bar_background())):
            palette = widget.palette()
            palette.setColor(QPalette.ColorRole.Window, color)
            widget.setPalette(palette)
        self.hint.update()

    def _refresh_shortcuts(self):
        controller = self.terminal.controller
        if not controller:
            return
        registry = controller.actions
        if 'toggle_terminal' not in registry:
            return
        self._terminal_shortcut = registry.shortcut_text('toggle_terminal')
        show_notes = registry.shortcut_text('toggle_notes_pane')
        add_terminal = registry.shortcut_text('new_terminal')
        for button, tip in ((self.plus, f'Add a terminal ({add_terminal})'),
                            (self.hide_button, f'Hide ({show_notes} notes, {self._terminal_shortcut} terminal)'),
                            (self.terminal.split_button, f'Split terminal ({registry.shortcut_text("split_terminal")})')):
            button.setToolTip(tip)
            button.setAccessibleName(tip)
        tab, title, _ = self._note_tabs['notes']
        self._note_tabs['notes'] = tab, title, f'Speaker notes for every build of this slide ({show_notes})'
        self._terminal_state = None
        self._update_page()

    def set_log_page(self, widget):
        self._pages['log'] = widget
        self.body.addWidget(widget)

    @property
    def page(self):
        return self._page

    def show_page(self, page):
        self._set_page(page)
        self.show()

    def _set_page(self, page):
        if page == self._page:
            return
        if page not in self._pages:
            raise ValueError(f'Drawer page unavailable: {page}')
        if self.will_change_page:
            self.will_change_page(page)
        self._page = page
        self._update_page()

    def _update_page(self):
        self.body.setCurrentWidget(self._pages[self._page])
        self.terminal.actions.setVisible(self._page == 'terminal')
        self.image.setVisible(self._page in ('notes', 'build_notes'))
        self.log_title.setVisible(self._page == 'log')
        self._update_hint()
        for key, (tab, title, tip) in self._note_tabs.items():
            tab.set_tab(title, tip, key == self._page)
        self._sync_terminal_tabs()
        if self._page in self._note_tabs:
            self.strip.reveal(self._note_tabs[self._page][0])
        elif self._page == 'log':
            self.strip.reveal(None)

    def set_build_label(self, label):
        self._build_label = label
        if self._page == 'build_notes':
            self._update_hint()

    def _update_hint(self):
        if self._page == 'build_notes':
            label = f' ({self._build_label})' if self._build_label else ''
            text = f'only for this build{label}, shown under the slide notes in Presenter View'
        elif self._page == 'alt_text':
            text = ('describes this slide for people who cannot see it; read by screen readers in PowerPoint exports, '
                    'never shown in Presenter View')
        elif self._page == 'notes':
            text = 'for every build of this slide, shown in Presenter View and by Manim Slides'
        else:
            text = ''
        self.hint.setText(text + '  ·  Ctrl+Return or click away to save' if text else '')
        self.hint.setToolTip(self.hint.text())

    def _sync_terminal_tabs(self):
        titles = self.terminal.tab_titles
        if not titles and self._page == 'terminal':
            self._set_page('notes')
            return
        active = self.terminal.active_tab_index if self._page == 'terminal' else None
        available = self.terminal.workspace_available
        state = (titles, active, available)
        if state == self._terminal_state:
            return
        had_shells = bool(self._terminal_tabs and self._terminal_tabs[0].close is not None)
        self._terminal_state = state
        self.plus.setEnabled(available)
        if bool(titles) != had_shells and self._terminal_tabs:
            self._remove_terminal_tab()
        specs = titles or [('Terminal 1', f"A shell in the deck's folder ({self._terminal_shortcut})")]
        while len(self._terminal_tabs) > len(specs):
            self._remove_terminal_tab()
        for i, (title, tip) in enumerate(specs):
            if i == len(self._terminal_tabs):
                close = (lambda index=i: self.terminal.close_tab(index)) if titles else None
                tab = _DrawerTab(lambda index=i: self._select_terminal(index), close)
                self._terminal_tabs.append(tab)
                self.strip.row.addWidget(tab)
            self._terminal_tabs[i].set_tab(title, tip, active == i)
            self._terminal_tabs[i].setEnabled(available)
        self.strip.row.activate()
        self.strip.updateGeometry()
        if active is not None:
            self.strip.reveal(self._terminal_tabs[active])

    def _remove_terminal_tab(self):
        tab = self._terminal_tabs.pop()
        self.strip.row.removeWidget(tab)
        tab.hide()
        tab.deleteLater()

    def _select_terminal(self, index):
        if self.on_show_terminal:
            self.on_show_terminal()
        if self._page == 'terminal' and index != self.terminal.active_tab_index:
            self.terminal.select_tab(index)

    def _new_terminal(self):
        if not self.terminal.has_started and self.on_show_terminal:
            self.on_show_terminal()
        if self.on_new_terminal:
            self.on_new_terminal()


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
        if self.drawer.page not in ('notes', 'build_notes') or not self.scene_name:
            return
        box = self.build_notes_box if self.drawer.page == 'build_notes' else self.notes_box
        target = box.picture_target()
        if target[0] is None:
            return
        cursor = box.textCursor()
        def choose():
            paths = yield from NoteImages.choose(target[0], self.window)
            box.insert_pictures(paths, target, cursor)
        self._watch(spawn(choose(), name='choose note pictures').future)

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
    def drawer_open(self):
        return not self.drawer.isHidden() and self.center_split.sizes()[1] > 0

    @property
    def drawer_shows_notes(self):
        return self.drawer_open and self.drawer.page in ('notes', 'build_notes', 'alt_text')

    @property
    def drawer_shows_terminal(self):
        return self.drawer_open and self.drawer.page == 'terminal'

    def drawer_split_moved(self):
        height = self.center_split.sizes()[1]
        if height:
            self._drawer_height = height
        elif prefs.get_bool('showNotes', True):
            self.commit_drawer()
            active = self.window.focusWidget()
            if active is not None and self.drawer.isAncestorOf(active):
                self.canvas.setFocus()
        prefs.set('showNotes', height > 0)

    def set_drawer_open(self, on, min_height=110):
        height = self.center_split.sizes()[1]
        if not on:
            if height:
                self._drawer_height = height
            self.commit_drawer()
            active = self.window.focusWidget()
            if active is not None and self.drawer.isAncestorOf(active):
                self.canvas.setFocus()
        self.drawer.setVisible(on)
        if on and height < min_height:
            span = self.center_split.height() - self.center_split.handleWidth()
            stage_minimum = self.center_split.widget(0).minimumSizeHint().height()
            preferred = max(min_height, self._drawer_height) if height == 0 else min_height
            wanted = min(preferred, max(0, span - stage_minimum))
            self.center_split.setSizes([span - wanted, wanted])
            prefs.set('manimble.drawer.sizes', self.center_split.sizes())
        prefs.set('showNotes', on)

    def toggle_notes_pane(self):
        if self.drawer_shows_notes:
            self.set_drawer_open(False)
        else:
            self.drawer.show_page('notes')
            self.set_drawer_open(True)
            self.notes_box.begin_editing()

    def toggle_build_notes(self):
        self.drawer.show_page('build_notes')
        self.set_drawer_open(True)
        self.build_notes_box.begin_editing()

    def toggle_alt_text(self):
        self.drawer.show_page('alt_text')
        self.set_drawer_open(True)
        self.alt_text_box.begin_editing()

    def toggle_engine_log(self):
        if self.drawer_open and self.drawer.page == 'log':
            self.set_drawer_open(False)
        else:
            self.drawer.show_page('log')
            self.set_drawer_open(True)
            self.engine_log.setFocus()

    def show_terminal_page(self):
        if self.workspace is None:
            return
        if not self.terminal.has_started:
            self.terminal.start(self.work_deck, original=self.original_deck)
        self.drawer.show_page('terminal')
        self.set_drawer_open(True, max(110, int(self.center_split.height() * .42)))
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

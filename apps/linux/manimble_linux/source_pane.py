"""Python source editing, with engine-owned conflict checking and source rewriting."""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QRegularExpression, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QStyle,
                               QVBoxLayout, QWidget)

from .style import Theme, is_dark, watch_appearance
from .widgets import SourceCodeView as BaseSourceCodeView, _rounded_mask


_TEXT = ((.13, .13, .15), (.88, .88, .88))
_COMMENT = ((.48, .48, .52), (.50, .50, .50))
_TOKENS = (
    (r'\b[A-Z][A-Z0-9_]{2,}\b', (0, .38, .62), (.55, .80, .95)),
    (r'\b[A-Za-z_][A-Za-z_0-9]*(?=\()', (0, .48, .42), (.45, .85, .75)),
    (r'\b(def|class|self|for|in|if|elif|else|return|import|from|as|with|while|and|or|not|'
     r'None|True|False|lambda|pass|yield|try|except|raise|is|assert|break|continue)\b',
     (.72, .08, .42), (1, .48, .70)),
    (r'(?<![\w.])\d+(\.\d+)?', (.55, .42, 0), (.85, .80, .50)),
    (r'''[rbf]?("[^"\n]*"|'[^'\n]*')''', (.70, .25, .08), (1, .58, .45)),
)


class PythonHighlighter(QSyntaxHighlighter):
    _patterns = tuple(QRegularExpression(pattern, QRegularExpression.PatternOption.UseUnicodePropertiesOption)
                      for pattern, _, _ in _TOKENS)
    _hash = QRegularExpression('#')

    def __init__(self, document):
        super().__init__(document)
        self._dark = None
        self.set_appearance(is_dark())

    def set_appearance(self, dark):
        if self._dark == dark:
            return
        self._dark = dark
        self._formats = []
        for colors in (*(token[1:] for token in _TOKENS), _COMMENT):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor.fromRgbF(*colors[dark]))
            self._formats.append(fmt)
        self.rehighlight()

    def highlightBlock(self, text):
        for pattern, fmt in zip(self._patterns, self._formats):
            matches = pattern.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        # Match Swift's first-hash heuristic, including '#' inside quoted strings.
        index = text.find('#')
        if index >= 0 and text.count('"', 0, index) % 2 == 0 and text.count("'", 0, index) % 2 == 0:
            # QRegularExpression and QTextLayout both use UTF-16 offsets.
            start = self._hash.match(text).capturedStart()
            self.setFormat(start, len(text[index:].encode('utf-16-le')) // 2, self._formats[-1])


class SourceCodeView(BaseSourceCodeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighter = PythonHighlighter(self.document())
        self._line_states = {}
        self.refresh_appearance()
        watch_appearance(self.refresh_appearance)

    def refresh_appearance(self):
        dark = is_dark()
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, Theme.code_background())
        palette.setColor(QPalette.ColorRole.Text, QColor.fromRgbF(*_TEXT[dark]))
        if palette != self.palette():
            self.setPalette(palette)
        self.comment_ink = QColor.fromRgbF(*_COMMENT[dark])
        self.highlighter.set_appearance(dark)
        self._paint_line_states(dark)

    def set_line_states(self, states):
        self._line_states = states
        self._paint_line_states(is_dark())

    def _paint_line_states(self, dark):
        changed = QColor('#32d74b' if dark else '#28cd41')
        current = QColor('#0a84ff' if dark else '#007aff')
        changed.setAlphaF(.28)
        current.setAlphaF(.25)
        self.set_line_backgrounds({index: changed if dirty else current
                                   for index, dirty in self._line_states.items()})

    def keyPressEvent(self, event):
        if self.isReadOnly():
            return super().keyPressEvent(event)
        if event.key() == Qt.Key.Key_Tab and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            cursor = self.textCursor()
            if cursor.hasSelection():
                start, end = cursor.selectionStart(), cursor.selectionEnd()
                cursor.setPosition(start)
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.beginEditBlock()
                while cursor.position() <= end:
                    cursor.insertText('    ')
                    end += 4
                    if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                        break
                cursor.endEditBlock()
            else:
                self.insertPlainText(' ' * (4 - cursor.positionInBlock() % 4))
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            cursor = self.textCursor()
            cursor.setPosition(cursor.selectionStart())
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
            before = cursor.selectedText()
            indent = before[:len(before) - len(before.lstrip(' \t'))]
            self.insertPlainText('\n' + indent + ('    ' if before.rstrip().endswith(':') else ''))
            return
        super().keyPressEvent(event)


class _SourceEdge(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.editing = False

    def paintEvent(self, event):
        width = 2 if self.editing else 1
        inset = width / 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Theme.accent if self.editing else Theme.ink(.12), width))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset),
                                10 - inset, 10 - inset)


class _SourceCard(QStackedWidget):
    def __init__(self, editor, on_details, parent):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(True)
        self.box = QWidget()
        layout = QVBoxLayout(self.box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.bar = QWidget()
        self.bar.setAutoFillBackground(True)
        self.bar.setFixedHeight(26)
        head = QHBoxLayout(self.bar)
        head.setContentsMargins(14, 0, 10, 0)
        head.setSpacing(4)
        self.header = QLabel('PYTHON')
        self.header.setTextFormat(Qt.TextFormat.PlainText)
        self.header.setFont(Theme.font(10, QFont.Weight.DemiBold))
        self.header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.header, 1)
        self.details = QPushButton('Error Details…')
        self.details.setFont(Theme.font(10, QFont.Weight.Medium))
        self.details.setFlat(True)
        self.details.clicked.connect(on_details)
        head.addWidget(self.details)
        layout.addWidget(self.bar)

        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setFont(Theme.mono(10))
        self.error.setContentsMargins(6, 3, 6, 3)
        self.error.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        policy = self.error.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.error.setSizePolicy(policy)
        self.error.setMaximumHeight(self.error.fontMetrics().lineSpacing() * 5 + 6)
        layout.addWidget(self.error)
        layout.addWidget(editor, 1)

        self.addWidget(self.box)
        self.edge = _SourceEdge(self)
        self.edge.raise_()
        self.currentChanged.connect(self.edge.raise_)
        self.refresh_appearance()
        watch_appearance(self.refresh_appearance)

    def refresh_appearance(self):
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, Theme.code_background())
        if palette != self.palette():
            self.setPalette(palette)
        palette = self.bar.palette()
        palette.setColor(QPalette.ColorRole.Window, Theme.bar_background())
        palette.setColor(QPalette.ColorRole.WindowText, Theme.ink(.5))
        if palette != self.bar.palette():
            self.bar.setPalette(palette)
        red = Theme.error_ink()
        background = QColor(red)
        background.setAlphaF(.1)
        error_style = f'color: {red.name()}; background-color: {background.name(QColor.NameFormat.HexArgb)};'
        detail_style = (f'QPushButton {{ color: {red.name()}; border: none; padding: 2px 0; }}'
                        f'QPushButton:pressed {{ background: {background.name(QColor.NameFormat.HexArgb)}; }}')
        for widget, style in ((self.error, error_style), (self.details, detail_style)):
            if style != widget.styleSheet():
                widget.setStyleSheet(style)
        self.edge.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMask(_rounded_mask(QRectF(self.rect()), 10))
        self.edge.setGeometry(self.rect())


def _show_diagnostic(parent, message):
    title = 'Python error'
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    layout = QVBoxLayout(dialog)
    heading = QHBoxLayout()
    icon = QLabel()
    icon.setPixmap(dialog.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical).pixmap(32, 32))
    heading.addWidget(icon)
    label = QLabel(title)
    label.setFont(Theme.font(14, QFont.Weight.DemiBold))
    heading.addWidget(label, 1)
    layout.addLayout(heading)
    if len(message) > 700 or message.count('\n') > 14:
        body = QPlainTextEdit(message)
        body.setReadOnly(True)
        dialog.resize(484, 330)
    else:
        body = QLabel(message)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setMaximumWidth(460)
    layout.addWidget(body, 1)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    copy = buttons.addButton('Copy', QDialogButtonBox.ButtonRole.ActionRole)
    copy.setToolTip('Copy this message to the clipboard')
    timer = QTimer(copy)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: copy.setText('Copy'))

    def copy_message():
        QApplication.clipboard().setText(f'{title}\n\n{message}')
        copy.setText('Copied')
        timer.start(1200)

    copy.clicked.connect(copy_message)
    layout.addWidget(buttons)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setFocus()
    dialog.open()


def _source_title(src):
    return f"PYTHON  ·  {src['file']}:{src['start']}" if src else 'PYTHON'


class SourceMixin:
    def make_source_pane(self):
        self.source_focused = False
        self.source_edit_base = self.source_edit_scene_name = self.source_draft = None
        self.source_error_message = None
        self.source_view = SourceCodeView()
        self.source_view.setReadOnly(True)
        self.source_view.on_focus = self.begin_source_editing
        self.source_view.on_blur = self.end_source_editing
        self.source_view.on_change = self.source_text_changed
        holder = QWidget()
        holder.setAutoFillBackground(True)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(10, 8, 10, 10)
        self.source_card = _SourceCard(self.source_view, self.show_source_error_details, holder)
        self.src_header = self.source_card.header
        self.source_error = self.source_card.error
        self.source_error_button = self.source_card.details
        self.source_box = self.source_card.box
        layout.addWidget(self.source_card)
        self.clear_source_error()
        return holder

    def begin_source_editing(self):
        if self.source_focused:
            return
        src = (self.scene or {}).get('source')
        if not src:
            return
        self.source_focused = True
        if self.source_edit_scene_name != self.scene_name or self.source_edit_base is None:
            self.source_edit_base = dict(src)
            self.source_edit_scene_name = self.scene_name
            self.source_draft = None
        self.source_view.set_numbering()
        self.source_view.set_line_states({})
        self.update_source_card_border()
        self.src_header.setText(_source_title(self.source_edit_base) + '  ·  EDITING')

    def source_text_changed(self):
        if not self.source_focused or not self.source_edit_base:
            return
        if self.animation_preview is not None:
            self.close_animation_preview()
        text = self.source_view.toPlainText()
        self.source_draft = text if text != self.source_edit_base['text'] else None
        self.src_header.setText(_source_title(self.source_edit_base)
                                + ('  ·  UNSAVED CODE' if self.source_draft is not None else '  ·  EDITING'))
        if self.source_draft is not None:
            self.clear_source_error()
        self.update_document_state()

    def end_source_editing(self):
        if not self.source_focused:
            return
        self.source_focused = False
        self.update_source_card_border()
        if self.source_draft is None:
            self.source_edit_base = None
            self.render_source()
        else:
            self.submit(self.commit_source_draft)

    def commit_source_draft(self):
        base, draft, name = self.source_edit_base, self.source_draft, self.source_edit_scene_name
        if base is None or draft is None or name is None:
            return
        self.busy += 1
        try:
            result = yield self.call('edit_source', {'scene': name, 'file': base['file'],
                                                    'expected': base['text'], 'text': draft})
            if not result.get('ok'):
                raise ValueError(result.get('error') or 'Could not update Python source')
        except Exception as exc:
            self.show_source_error(str(exc))
            self.toast(str(exc), error=True)
            self.update_document_state()
            return False
        finally:
            self.busy -= 1
        self.last_changed = result.get('changed')
        newer = self.source_draft != draft
        if not newer:
            self.source_draft = None
        if self.scene_name == name:
            yield from self.load_scene(name, keep_stop=True)
            self.source_edit_base = (self.scene or {}).get('source')
        yield from self.reload_state()
        if result.get('error'):
            self.show_source_error(result['error'])
        else:
            if self.source_focused or self.source_draft is not None:
                suffix = '  ·  UNSAVED CODE' if self.source_draft is not None else '  ·  EDITING'
                self.src_header.setText(_source_title(self.source_edit_base) + suffix)
            self.toast('Python source updated')
        self.update_document_state()
        if newer and not self.source_focused:
            self.submit(self.commit_source_draft)
        return True

    def clear_source_error(self):
        self.source_error_message = None
        self.source_error.clear()
        self.source_error.setToolTip('')
        self.source_error.hide()
        self.source_error_button.hide()

    def show_source_error(self, message):
        self.source_error_message = message
        lines = [line for line in message.split('\n') if line]
        self.source_error.setText('\n'.join(lines[-5:]))
        self.source_error.setToolTip(f'<pre>{escape(message)}</pre>')
        self.source_error.show()
        self.source_error_button.show()
        self.src_header.setText('PYTHON  ·  ERROR')

    def show_source_error_details(self):
        if self.source_error_message:
            _show_diagnostic(self.window, self.source_error_message)

    def render_source(self):
        if self.source_focused or self.source_draft is not None:
            return
        src = (self.scene or {}).get('source')
        had_read_only_focus = self.source_view.isReadOnly() and self.source_view.hasFocus()
        self.source_view.setReadOnly(not bool(src))
        self.clear_source_error()
        self.update_source_card_border()
        self.src_header.setText(_source_title(src))
        if not src:
            self.source_view.set_numbering()
            self.source_view.set_line_states({})
            self.source_view.clear()
            return
        self.source_view.set_numbering(src['start'])
        if self.source_view.toPlainText() != src['text']:
            self.source_view.setPlainText(src['text'])
        changed = self.last_changed if self.last_changed is not None else src.get('changed')
        ranges = (changed.get('ranges') or []) if changed and changed.get('file') == src['file'] else []
        loc = ((self.obj_info or {}).get('loc') or '').rsplit(':', 1)
        count = self.source_view.document().blockCount()
        start = src['start']
        selected = int(loc[1]) - start if len(loc) == 2 and loc[0] == src['file'] and loc[1].isdigit() else -1
        states = {}
        target = selected if 0 <= selected < count else None
        if target is not None:
            states[selected] = False
        first_changed = count
        for a, b in ranges:
            begin, end = max(0, a - start), min(count, max(a, b) - start + 1)
            if begin < end:
                first_changed = min(first_changed, begin)
                states.update((index, True) for index in range(begin, end))
        self.source_view.set_line_states(states)
        if first_changed < count:
            target = first_changed
        if target is not None:
            self.source_view.reveal_line(target)
        if (self.scene or {}).get('status') == 'error' and self.scene.get('error'):
            self.show_source_error(self.scene['error'])
        if had_read_only_focus:
            self.begin_source_editing()

    def update_source_card_border(self):
        self.source_card.edge.editing = self.source_focused
        self.source_card.edge.update()

    def highlight_source(self):
        self.render_source()

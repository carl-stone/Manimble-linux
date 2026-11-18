"""Python source editing, with engine-owned conflict checking and source rewriting."""
from __future__ import annotations

import keyword
import re
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout
from .widgets import SourceCodeView as BaseSourceCodeView


class PythonHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        rules = [(r'\b[A-Z][A-Z_0-9]{2,}\b', '#68b5d9'),
                 (r'\b\w+(?=\()', '#42a998'),
                 (r'\b(?:' + '|'.join(keyword.kwlist) + r')\b', '#cc5599'),
                 (r'(?<![\w.])\d+(?:\.\d+)?', '#ad9440'),
                 (r'''[rbfu]?(?:"[^"\n]*"|'[^'\n]*')''', '#c77b48'),
                 (r'#.*$', '#888888')]
        for pattern, color in rules:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            for match in re.finditer(pattern, text):
                # Qt positions are UTF-16, Python regex positions are code points.
                start = len(text[:match.start()].encode('utf-16-le')) // 2
                size = len(match.group().encode('utf-16-le')) // 2
                self.setFormat(start, size, fmt)


class SourceCodeView(BaseSourceCodeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighter = PythonHighlighter(self.document())

    def keyPressEvent(self, event):
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
            before = cursor.block().text()[:cursor.positionInBlock()]
            indent = re.match(r'\s*', before).group()
            self.insertPlainText('\n' + indent + ('    ' if before.rstrip().endswith(':') else ''))
            return
        super().keyPressEvent(event)


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
        self.src_header = QLabel('PYTHON')
        self.source_error = QLabel()
        self.source_error.setWordWrap(True)
        self.source_error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_error.setStyleSheet('color: #dd5050;')
        self.source_error_button = QPushButton('Error Details…')
        self.source_error_button.clicked.connect(self.show_source_error_details)
        self.source_card = QFrame()
        layout = QVBoxLayout(self.source_card)
        layout.setContentsMargins(8, 6, 8, 6)
        head = QHBoxLayout()
        head.addWidget(self.src_header)
        head.addWidget(self.source_error_button)
        layout.addLayout(head)
        layout.addWidget(self.source_error)
        layout.addWidget(self.source_view, 1)
        self.source_box = self.source_card
        self.clear_source_error()
        return self.source_card

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
        self.update_source_card_border()

    def source_text_changed(self):
        if not self.source_focused or not self.source_edit_base:
            return
        text = self.source_view.toPlainText()
        self.source_draft = text if text != self.source_edit_base['text'] else None
        self.src_header.setText('PYTHON · ' + ('UNSAVED CODE' if self.source_draft is not None else 'EDITING'))
        if self.source_draft is not None:
            self.clear_source_error()
        self.update_document_state()

    def end_source_editing(self):
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
                raise ValueError(result.get('error', 'Could not update Python source'))
        except Exception as exc:
            self.show_source_error(str(exc))
            self.toast(str(exc), error=True)
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
            self.toast('Python source updated')
        self.update_document_state()
        if newer and not self.source_focused:
            self.submit(self.commit_source_draft)
        return True

    def clear_source_error(self):
        self.source_error_message = None
        self.source_error.clear()
        self.source_error.hide()
        self.source_error_button.hide()

    def show_source_error(self, message):
        self.source_error_message = message
        self.source_error.setText('\n'.join(message.splitlines()[-5:]))
        self.source_error.setToolTip(message)
        self.source_error.show()
        self.source_error_button.show()
        self.src_header.setText('PYTHON · ERROR')

    def show_source_error_details(self):
        if self.source_error_message:
            self.alert('Python error', self.source_error_message)

    def render_source(self):
        if self.source_focused or self.source_draft is not None:
            return
        src = (self.scene or {}).get('source')
        self.source_view.setReadOnly(not bool(src))
        if not src:
            self.source_view.clear()
            return
        self.clear_source_error()
        self.src_header.setText(f"PYTHON · {src['file']}:{src['start']}")
        self.source_view.start_line = src['start']
        if self.source_view.toPlainText() != src['text']:
            self.source_view.setPlainText(src['text'])
        self.source_view._margin()
        changed = self.last_changed or src.get('changed') or {}
        ranges = changed.get('ranges', []) if changed.get('file') == src['file'] else []
        loc = (self.obj_info or {}).get('loc', '').rsplit(':', 1)
        selected = int(loc[1]) if len(loc) == 2 and loc[0] == src['file'] and loc[1].isdigit() else None
        highlights = []
        block = self.source_view.document().firstBlock()
        while block.isValid():
            line = src['start'] + block.blockNumber()
            dirty = any(a <= line <= max(a, b) for a, b in ranges)
            if dirty or line == selected:
                extra = QTextEdit.ExtraSelection()
                extra.cursor = QTextCursor(block)
                extra.format.setBackground(QColor('#344e38' if dirty else '#324c65'))
                extra.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                highlights.append(extra)
            block = block.next()
        self.source_view.setExtraSelections(highlights)
        if (self.scene or {}).get('status') == 'error':
            self.show_source_error(self.scene.get('error', 'Render failed'))

    def update_source_card_border(self):
        self.source_card.setStyleSheet('QFrame { border: 1px solid ' + ('#579cdd' if self.source_focused else '#555555') + '; }')

    def recolor_source(self):
        self.source_view.highlighter.rehighlight()

    def highlight_source(self):
        self.render_source()

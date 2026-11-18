"""Small Qt Widgets shared by the navigator, inspector, source pane and drawer."""
from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QSize, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QImage, QTextCursor, QTextCharFormat, QPalette
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QHBoxLayout, QPlainTextEdit, QTextEdit, QListWidget, QAbstractItemView,
    QSplitter, QSplitterHandle, QMessageBox, QSizePolicy, QStyledItemDelegate, QStyle, QStyleOptionViewItem)

from . import prefs
from .style import Theme


def section_label(text, parent=None):
    label = QLabel(text.upper(), parent)
    label.setFont(Theme.font(10, Theme.font(10).Weight.DemiBold))
    label.setStyleSheet(f"color: {Theme.ink(.45).name(QColor.NameFormat.HexArgb)}")
    return label


def field_label(text, parent=None):
    label = QLabel(text, parent)
    label.setFont(Theme.font(11))
    label.setStyleSheet(f"color: {Theme.ink(.65).name(QColor.NameFormat.HexArgb)}")
    return label


def hairline(parent=None):
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line


def add_copy_button(box):
    """Copy without dismissing a dialog; long messages use Qt's scrolling details."""
    text = "\n\n".join(filter(None, (box.windowTitle(), box.text(), box.informativeText(), box.detailedText())))
    if len(box.informativeText()) > 700 or box.informativeText().count("\n") > 14:
        box.setDetailedText(box.informativeText())
        box.setInformativeText("")
    button = QPushButton("Copy", box)
    box.layout().addWidget(button, box.layout().rowCount(), 0)
    def copy():
        QApplication.clipboard().setText(text)
        button.setText("Copied")
        QTimer.singleShot(1200, lambda: button.setText("Copy"))
    button.clicked.connect(copy)
    return button


def alert(title, text, parent=None):
    box = QMessageBox(QMessageBox.Icon.Warning, title, title, QMessageBox.StandardButton.Ok, parent)
    box.setInformativeText(str(text))
    add_copy_button(box)
    return box.exec()


def confirm(title, text, accept="Continue", parent=None):
    box = QMessageBox(QMessageBox.Icon.Question, title, title, QMessageBox.StandardButton.NoButton, parent)
    box.setInformativeText(text)
    yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(no)
    add_copy_button(box)
    box.exec()
    return box.clickedButton() is yes


class ProgressBar(QWidget):
    """Monotone, clock-driven estimate: 90% at deadline, then 99% after a tail."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setMinimumWidth(140)
        self.running = False
        self._current = 0.0
        self._from = 0.0
        self._to = 0.0
        self._start = 0.0
        self._ahead = .05
        self._tail = 4.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    @property
    def current(self):
        return self._current

    def reset(self):
        self._timer.stop()
        self.running = False
        self._current = 0.0
        self.update()

    def set(self, fraction):
        self._tick()
        self.running = False
        self._from = self._current
        self._to = min(1., max(0., float(fraction)))
        self._start = time.monotonic()
        self._timer.start()

    def run(self, remaining, tail=4.):
        if self._timer.isActive():
            self._tick()
        self.running = True
        self._from = self._current
        self._ahead = max(.05, float(remaining))
        self._tail = max(1., float(tail))
        self._start = time.monotonic()
        self._timer.start()

    def _tick(self):
        if not self._timer.isActive():
            return
        elapsed = time.monotonic() - self._start
        if self.running:
            if self._from < .9 and elapsed < self._ahead:
                value = self._from + (.9 - self._from) * elapsed / self._ahead
            else:
                u = min(1., max(0., elapsed - (self._ahead if self._from < .9 else 0.)) / self._tail)
                base = max(.9, self._from)
                value = base + (max(base, .99) - base) * (1 - (1 - u) ** 3)
                if u >= 1:
                    self._timer.stop()
            self._current = max(self._current, value)
        else:
            u = min(1., elapsed / .2)
            self._current = self._from + (self._to - self._from) * u
            if u >= 1:
                self._timer.stop()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Theme.ink(.14))
        painter.drawRoundedRect(QRectF(self.rect()), 1.5, 1.5)
        painter.setBrush(Theme.accent)
        painter.drawRoundedRect(QRectF(0, 0, self.width() * self._current, self.height()), 1.5, 1.5)


class Pill(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_cancel = None
        self.default_estimate = 3.0
        self.hovered = False
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setFont(Theme.font(12))
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.icon = QToolButton(self)
        self.icon.setText("ⓘ")
        self.icon.setToolTip("Click to copy this message")
        self.icon.clicked.connect(self.copy_message)
        self.cancel_button = QToolButton(self)
        self.cancel_button.setText("×")
        self.cancel_button.setAccessibleName("Stop rendering")
        self.cancel_button.setToolTip("Stop rendering")
        self.cancel_button.clicked.connect(self.cancel_pressed)
        self.cancel_button.hide()
        self.bar = ProgressBar(self)
        self.bar.hide()
        self.remaining_label = QLabel(self)
        self.remaining_label.setFont(Theme.font(10))
        self.remaining_label.hide()
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(self.icon)
        row.addWidget(self.label, 1)
        row.addWidget(self.remaining_label)
        row.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 14, 7)
        layout.setSpacing(6)
        layout.addLayout(row)
        layout.addWidget(self.bar)
        self.setMaximumWidth(640)
        self.setStyleSheet(f"Pill {{ background: {Theme.bar_background().name()}; border: 1px solid {Theme.ink(.14).name(QColor.NameFormat.HexArgb)}; border-radius: 15px; }} QToolButton {{ border: none; }}")
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(lambda: self.icon.setText(self._symbol))
        self._remaining_timer = QTimer(self)
        self._remaining_timer.setInterval(250)
        self._remaining_timer.timeout.connect(self._update_remaining)
        self._symbol = "ⓘ"
        self._deadline = None
        super().hide()

    def enterEvent(self, event):
        self.hovered = True
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        super().leaveEvent(event)

    def copy_message(self):
        QApplication.clipboard().setText(self.label.text())
        self.icon.setText("✓")
        self.icon.setToolTip("Copied")
        self._copy_timer.start(1200)

    def show(self, text=None, symbol="ⓘ", tint=None, spinning=False, progress=None, remaining=None, cancellable=True):
        if text is None:
            return super().show()
        was_hidden = self.isHidden()
        self.label.setText(str(text))
        self._copy_timer.stop()
        self._symbol = {"checkmark.circle.fill": "✓", "exclamationmark.triangle.fill": "⚠"}.get(symbol, "ⓘ")
        self.icon.setText(self._symbol)
        self.icon.setToolTip("Click to copy this message")
        self.icon.setVisible(symbol is not None and not spinning)
        if tint is not None:
            self.icon.setStyleSheet(f"color: {QColor(tint).name()}")
        self.cancel_button.setVisible(spinning and cancellable and self.on_cancel is not None)
        self.cancel_button.setEnabled(True)
        self.bar.setVisible(spinning)
        if spinning:
            if was_hidden:
                self.bar.reset()
            if progress is not None:
                self.bar.set(progress)
            elif remaining is not None:
                self.bar.run(remaining)
            elif not self.bar.running:
                self.bar.run(self.default_estimate)
        self._deadline = time.monotonic() + max(0., remaining) if spinning and remaining is not None else None
        self.remaining_label.setVisible(self._deadline is not None)
        self._remaining_timer.start() if self._deadline is not None else self._remaining_timer.stop()
        self._update_remaining()
        super().show()

    def _update_remaining(self):
        if self._deadline is not None:
            seconds = math.ceil(max(0., self._deadline - time.monotonic()))
            self.remaining_label.setText(f"{seconds}s remaining" if seconds else "Finishing…")

    def cancel_pressed(self):
        self.cancel_button.setEnabled(False)
        if self.on_cancel:
            self.on_cancel()

    def hide(self):
        self._remaining_timer.stop()
        self.bar.reset()
        super().hide()


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def paintEvent(self, event):
        editor = self.editor
        painter = QPainter(self)
        painter.fillRect(event.rect(), Theme.bar_background())
        painter.setPen(Theme.ink(.45))
        block = editor.firstVisibleBlock()
        top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            height = editor.blockBoundingRect(block).height()
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(0, round(top), self.width() - 6, round(height), Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + editor.start_line))
            top += height
            block = block.next()


class SourceCodeView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_focus = self.on_blur = self.on_change = None
        self.start_line = 1
        self.setFont(Theme.mono(12))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.gutter = _LineNumbers(self)
        self.blockCountChanged.connect(self._margin)
        self.updateRequest.connect(self._update_gutter)
        self.textChanged.connect(lambda: self.on_change() if self.on_change else None)
        self._margin()

    @property
    def string(self):
        return self.toPlainText()

    @string.setter
    def string(self, text):
        self.setPlainText(text)
        self.document().clearUndoRedoStacks()

    def _margin(self):
        width = 12 + self.fontMetrics().horizontalAdvance("9") * len(str(max(1, self.blockCount() + self.start_line - 1)))
        self.setViewportMargins(width, 0, 0, 0)
        self.gutter.setGeometry(0, 0, width, self.height())

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._margin()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self.on_focus:
            self.on_focus()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self.on_blur:
            self.on_blur()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Tab and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.insertPlainText("    ")
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            before = cursor.block().text()[:cursor.positionInBlock()]
            indentation = before[:len(before) - len(before.lstrip(" \t"))]
            self.insertPlainText("\n" + indentation + ("    " if before.rstrip().endswith(":") else ""))
            return
        super().keyPressEvent(event)


class MultilineTextBox(QTextEdit):
    def __init__(self, string="", max_lines=14, parent=None):
        super().__init__(parent)
        self.editor = self
        self.original = string
        self.max_lines = max_lines
        self.on_commit = None
        self.script_mode = None
        self._fills_container = False
        self._image_base = None
        self.setFont(Theme.font(13))
        self.setAcceptRichText(False)
        self.setPlainText(string)
        self.textChanged.connect(self.fit)
        self.fit()

    @property
    def fills_container(self):
        return self._fills_container

    @fills_container.setter
    def fills_container(self, value):
        self._fills_container = bool(value)
        self.setMinimumHeight(24)
        self.setMaximumHeight(16777215)
        self.fit()

    @property
    def image_base(self):
        return self._image_base

    @image_base.setter
    def image_base(self, base):
        value = self.string_value
        self._image_base = Path(base) if base else None
        self._show(value)

    @property
    def string_value(self):
        if self._image_base is not None:
            from .note_images import NoteImages
            return NoteImages.markup(self.document())
        return self.toPlainText()

    @property
    def selected_range(self):
        cursor = self.textCursor()
        return cursor.selectionStart(), cursor.selectionEnd() - cursor.selectionStart()

    @property
    def is_editing(self):
        return self.hasFocus()

    @property
    def image_count(self):
        count = 0
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.charFormat().isImageFormat():
                    count += 1
                iterator += 1
            block = block.next()
        return count

    def _show(self, value):
        if self._image_base is None:
            self.setPlainText(value)
        else:
            from .note_images import NoteImages
            document = NoteImages.attributed(value, self._image_base, max_width=min(360, max(80, self.viewport().width() - 12)))
            document.setParent(self)
            self.setDocument(document)

    def set_text(self, value):
        self._show(value)
        self.original = value
        self.document().clearUndoRedoStacks()
        self.fit()

    def begin_editing(self):
        self.setFocus()
        self.selectAll()

    def commit(self):
        value = self.string_value
        if value != self.original:
            self.original = value
            if self.on_commit:
                self.on_commit(value)

    def focusOutEvent(self, event):
        self.script_mode = None
        self.commit()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.commit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._show(self.original)
            self.clearFocus()
            return
        if self.script_mode is not None and event.text() and not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            from .formatbar import TextTransform
            self.insertPlainText(TextTransform.raise_text(event.text(), superscript=self.script_mode))
            return
        super().keyPressEvent(event)

    def script_selection(self, superscript):
        from .formatbar import TextTransform
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        result = TextTransform.script(cursor.selectedText(), superscript=superscript)
        if result is None:
            return False
        start = cursor.selectionStart()
        cursor.insertText(result)
        cursor.setPosition(start)
        cursor.setPosition(start + len(result.encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.script_mode = None
        return True

    def highlight(self, value=None, from_=0, to=0):
        self.setExtraSelections([])
        if value is None or not 0 <= from_ < to <= len(value):
            return
        match = value[from_:to]
        count = value[:from_].count(match)
        text = self.toPlainText()
        at = -1
        for _ in range(count + 1):
            at = text.find(match, at + 1)
            if at < 0:
                return
        cursor = self.textCursor()
        cursor.setPosition(len(text[:at].encode("utf-16-le")) // 2)
        cursor.setPosition(len(text[:at + len(match)].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(Theme.accent_soft)
        self.setExtraSelections([selection])
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def insert_images(self, paths):
        if self._image_base is None:
            return
        from .note_images import NoteImages
        NoteImages.insert(paths, self, self._image_base, max_width=min(360, max(80, self.viewport().width() - 12)))
        self.fit()

    def insertFromMimeData(self, mime):
        if self._image_base is not None and (mime.hasImage() or mime.hasUrls()):
            from .note_images import NoteImages
            from .tasks import spawn
            # QMimeData belongs to the clipboard; copy it before an asynchronous
            # asset import so a subsequent clipboard change cannot invalidate it.
            from PySide6.QtCore import QMimeData
            copied = QMimeData()
            for format in mime.formats():
                copied.setData(format, mime.data(format))
            if mime.hasImage():
                copied.setImageData(mime.imageData())
            document, base = self.document(), self._image_base
            position = self.textCursor()
            def paste():
                try:
                    paths = yield from NoteImages.import_images(copied, base)
                    if self.document() is not document:
                        return
                    self.setTextCursor(position)
                    if paths:
                        self.insert_images(paths)
                    else:
                        QTextEdit.insertFromMimeData(self, copied)
                except Exception as error:
                    alert("Could not insert note pictures", str(error), self)
            spawn(paste(), name="note_pictures")
            return
        super().insertFromMimeData(mime)

    def fit(self):
        if self._fills_container:
            return
        line = self.fontMetrics().lineSpacing()
        height = min(max(line, self.document().size().height()), line * self.max_lines) + 12
        self.setFixedHeight(round(height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class FlatRowView(QWidget):
    """A row intentionally has no system selection fill; thumbnails carry rings."""


class BuildPanel(QWidget):
    def __init__(self, image=None, index=0, active=False, handler=None, parent=None):
        super().__init__(parent)
        self.image = image if isinstance(image, QImage) else QImage(str(image)) if image else QImage()
        self.index, self.active, self.handler = index, active, handler
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Build {index + 1}")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(32, 18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(160, 90)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = round(self.width() * 9 / 16)
        if self.height() != height:
            self.setFixedHeight(height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if not self.image.isNull():
            target = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            rect = QRectF((self.width()-target.width())/2, (self.height()-target.height())/2, target.width(), target.height())
            painter.setOpacity(1. if self.active else .72)
            painter.drawImage(rect, self.image)
            painter.setOpacity(1.)
        painter.setPen(QPen(Theme.accent if self.active else Theme.ink(.22), 2 if self.active else 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)
        painter.fillRect(4, 3, 18, 14, QColor(0, 0, 0, 160))
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(Theme.font(9))
        painter.drawText(4, 3, 18, 14, Qt.AlignmentFlag.AlignCenter, str(self.index + 1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.handler:
            self.handler()
            event.accept()
        else:
            super().mousePressEvent(event)


class StepButton(BuildPanel):
    def __init__(self, path, index=0, active=False, handler=None, parent=None):
        super().__init__(_mapped_image(path), index, active, handler, parent)
        self.setFixedSize(80, 45)


def _mapped_image(path):
    if not path:
        return QImage()
    from .darkmode import DarkMode
    return DarkMode.image(path=str(path)) or QImage()


class SlideCell(FlatRowView):
    thumb_inset, build_indent, build_spacing, base_height = 42, 16, 6, 108

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_build = None
        self.on_click = None
        self.num = QLabel(self)
        self.num.setFont(Theme.font(11))
        self.thumb = QLabel(self)
        self.name = QLabel(self)
        self.name.setFont(Theme.font(10))
        self.badge = QLabel(self)
        self.badge.setFont(Theme.font(10))
        self._image = QImage()
        self._active = False
        self._hidden = False
        self._panels = []
        self._unused = False
        self._status = "pending"
        self._name = ""

    @staticmethod
    def row_height(column_width, builds=0):
        thumb_w = max(40, column_width - SlideCell.thumb_inset)
        thumb_h = round(thumb_w * 9 / 16)
        if builds <= 0:
            return max(108, 6 + thumb_h + 4 + 13 + 2)
        top = max(108, 6 + thumb_h + 4 + 13 + 6)
        return top + builds * (round(max(32, thumb_w - 16) * 9 / 16) + 6) + 2

    def configure(self, number, name, image=None, status="pending", stops=0, active=False, unused=False, hidden=False, expanded=None):
        self._name, self._hidden, self._unused, self._status = name, hidden, unused, status
        self._image = image if isinstance(image, QImage) else QImage()
        self.num.setText("–" if unused else str(number))
        font = self.num.font()
        font.setStrikeOut(hidden and not unused)
        self.num.setFont(font)
        self.name.setToolTip(name)
        chevron = "" if expanded is None else " ▾" if expanded else " ▸"
        self.badge.setText("unused" if unused else "hidden" if hidden else f"{stops} builds{chevron}" if stops > 1 else "…" if self._image.isNull() and status != "error" else "")
        self.set_active(active)
        self.set_builds([], -1)
        self._layout()

    def set_active(self, active):
        self._active = active
        self.num.setStyleSheet(f"color: {(Theme.accent if active else Theme.ink(.45)).name()}")
        self.name.setStyleSheet(f"color: {(QColor('#E54141') if self._status == 'error' else Theme.ink(.95 if active else .65)).name()}")
        self.update()

    def set_builds(self, images, active=-1):
        for panel in self._panels:
            panel.deleteLater()
        self._panels = []
        for i, image in enumerate(images):
            panel = BuildPanel(image, i, i == active, lambda index=i: self.on_build(index) if self.on_build else None, self)
            panel.show()
            self._panels.append(panel)
        self._layout()

    def _layout(self):
        width = max(40, self.width() - self.thumb_inset)
        height = round(width * 9 / 16)
        self.num.setGeometry(2, 8, 20, 18)
        self.thumb.setGeometry(30, 6, width, height)
        badge_w = self.badge.sizeHint().width()
        self.badge.setGeometry(30 + width - badge_w, 10 + height, badge_w, 15)
        name_width = max(0, width - badge_w - 4)
        text = self._name + (" — error" if self._status == "error" else "")
        self.name.setText(self.name.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, name_width))
        self.name.setGeometry(32, 10 + height, name_width, 15)
        y = max(108, 6 + height + 4 + 13 + 6)
        build_width = max(32, width - self.build_indent)
        for panel in self._panels:
            h = round(build_width * 9 / 16)
            panel.setGeometry(46, y, build_width, h)
            y += h + self.build_spacing
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.thumb.geometry())
        painter.fillRect(rect, Qt.GlobalColor.black)
        if not self._image.isNull():
            painter.setOpacity(.4 if self._hidden else 1.)
            size = self._image.size().scaled(rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(rect.center().x()-size.width()/2, rect.center().y()-size.height()/2, size.width(), size.height())
            painter.drawImage(target, self._image)
            painter.setOpacity(1.)
        if self._active:
            painter.setPen(QPen(Theme.accent, 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 7, 7)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.on_click:
            self.on_click()
            event.accept()
        else:
            super().mousePressEvent(event)


class _FlatRowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        plain = QStyleOptionViewItem(option)
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
        super().paint(painter, plain, index)


class SlideTable(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_delete = self.on_delete_build = self.on_menu = self.on_reclick = self.on_arrow = self.on_reorder = None
        self.build_focused = False
        self.on_resize = None
        self._pressed_item = None
        self._press_position = None
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setItemDelegate(_FlatRowDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setStyleSheet(f"QListWidget {{ background: {Theme.sidebar_background().name()}; border: none; }} QListWidget::item:selected {{ background: transparent; }}")

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.currentRow() >= 0:
            callback = self.on_delete_build if self.build_focused and self.on_delete_build else self.on_delete
            if callback:
                callback()
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and event.modifiers() == Qt.KeyboardModifier.NoModifier and self.on_arrow:
            self.on_arrow(1 if event.key() == Qt.Key.Key_Down else -1)
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        self._pressed_item = item if item is self.currentItem() else None
        self._press_position = event.position().toPoint()
        self.build_focused = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        reclick = (event.button() == Qt.MouseButton.LeftButton and item is not None
                   and item is self._pressed_item and self._press_position is not None
                   and (event.position().toPoint() - self._press_position).manhattanLength() < QApplication.startDragDistance())
        super().mouseReleaseEvent(event)
        self._pressed_item = None
        if reclick and self.on_reclick:
            self.on_reclick(self.row(item))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.on_resize:
            self.on_resize()

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        row = self.row(item) if item else -1
        if item:
            self.setCurrentItem(item)
        self.context_position = event.globalPos()
        menu = self.on_menu(row) if self.on_menu else None
        if menu:
            menu.exec(event.globalPos())
            menu.deleteLater()

    def dropEvent(self, event):
        if event.source() is not self or not self.on_reorder:
            event.ignore()
            return
        source = self.currentRow()
        target = self.indexAt(event.position().toPoint())
        index = target.row() if target.isValid() else self.count()
        if target.isValid() and event.position().y() > self.visualRect(target).center().y():
            index += 1
        if source >= 0 and index not in (source, source + 1) and self.on_reorder(source, index):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()


class _HairlineHandle(QSplitterHandle):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(Theme.ink(.16))
        if self.orientation() == Qt.Orientation.Horizontal:
            painter.drawLine(self.width()//2, 0, self.width()//2, self.height())
        else:
            painter.drawLine(0, self.height()//2, self.width(), self.height()//2)


class GrabSplitView(QSplitter):
    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None, settings_key=""):
        super().__init__(orientation, parent)
        self.settings_key = settings_key
        self.on_resize = None
        self._collapses_edge_panes = False
        self._collapses_last_pane = False
        self._restored = False
        self.setHandleWidth(9)
        self.setChildrenCollapsible(False)
        self.splitterMoved.connect(self._resized)

    def createHandle(self):
        return _HairlineHandle(self.orientation(), self)

    @property
    def collapses_edge_panes(self):
        return self._collapses_edge_panes

    @collapses_edge_panes.setter
    def collapses_edge_panes(self, value):
        self._collapses_edge_panes = bool(value)
        self._collapse_policy()

    @property
    def collapses_last_pane(self):
        return self._collapses_last_pane

    @collapses_last_pane.setter
    def collapses_last_pane(self, value):
        self._collapses_last_pane = bool(value)
        self._collapse_policy()

    def _collapse_policy(self):
        for i in range(self.count()):
            self.setCollapsible(i, self._collapses_edge_panes and i in (0, self.count()-1) or self._collapses_last_pane and i == self.count()-1)

    def showEvent(self, event):
        super().showEvent(event)
        self._collapse_policy()
        if not self._restored and self.settings_key:
            self._restored = True
            sizes = prefs.get_json(self.settings_key, [])
            if isinstance(sizes, list) and len(sizes) == self.count() and all(isinstance(s, int) and s >= 0 for s in sizes) and sum(sizes):
                self.setSizes(sizes)

    def _resized(self, *_):
        if self.settings_key:
            prefs.set(self.settings_key, self.sizes())
        if self.on_resize:
            self.on_resize()

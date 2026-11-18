"""Small Qt Widgets shared by the navigator, inspector, source pane and drawer."""
from __future__ import annotations

import time
from math import ceil
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QFontMetricsF, QImage, QPainter, QPainterPath,
                           QPalette, QPen, QRegion, QTextCharFormat, QTextCursor, QTextFormat)
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QHBoxLayout, QPlainTextEdit, QTextEdit, QListWidget, QAbstractItemView,
    QSplitter, QSplitterHandle, QMessageBox, QSizePolicy, QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QGraphicsOpacityEffect, QLayout)

from . import prefs
from .icons import template_icon
from .style import Theme, is_dark, watch_appearance


def _set_chrome_style(widget, style):
    # Repolishing a stylesheet can itself deliver a palette change.
    if widget.styleSheet() != style:
        widget.setStyleSheet(style)


class _InkLabel(QLabel):
    def __init__(self, text, ink, parent):
        super().__init__(text, parent)
        self._ink = ink
        self._recolor()
        watch_appearance(self._recolor)

    def _recolor(self):
        _set_chrome_style(self, f"color: {self._ink().name(QColor.NameFormat.HexArgb)}")


def section_label(text, parent=None):
    label = _InkLabel(text.upper(), Theme.tertiary_ink, parent)
    label.setFont(Theme.font(10, QFont.Weight.DemiBold))
    return label


def field_label(text, parent=None):
    label = _InkLabel(text, Theme.secondary_ink, parent)
    label.setFont(Theme.font(11))
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
        watch_appearance(self.update)

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


def _rounded_path(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _rounded_mask(rect, radius):
    return QRegion(_rounded_path(rect, radius).toFillPolygon().toPolygon())


class Pill(QWidget):
    contents_changed = Signal()
    _radius = 15
    _copy_tip = "Click to copy this message"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_cancel = None
        self.default_estimate = 3.0
        self.hovered = False
        self._shown = False
        self._cancelling = False
        self._symbol = self._tint = None

        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignTop)
        self.label.setFont(Theme.font(12, QFont.Weight.Medium))
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self.icon = self._icon_button(13, self._copy_tip)
        self.icon.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon.clicked.connect(self.copy_message)
        self.cancel_button = self._icon_button(14, "Stop rendering")
        self.cancel_button.clicked.connect(self.cancel_pressed)
        self.bar = ProgressBar(self)
        for widget in (self.icon, self.cancel_button, self.bar):
            widget.hide()

        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(self.icon)
        row.addWidget(self.label, 1)
        row.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(12, 7, 14, 7)
        layout.setSpacing(6)
        layout.addLayout(row)
        layout.addWidget(self.bar)
        self._row = row

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._update_copy_icon)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.)
        self.setGraphicsEffect(self._opacity)
        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._opacity.setOpacity)
        self._fade.finished.connect(self._finish_fade)
        self._recolor()
        watch_appearance(self._recolor)
        super().hide()

    def _icon_button(self, size, tip):
        button = QToolButton(self)
        button.setFixedSize(size, size)
        button.setIconSize(QSize(size, size))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setStyleSheet("QToolButton { background: transparent; border: none; padding: 0; }"
                             "QToolButton:pressed { background: palette(mid); border-radius: 6px; }")
        return button

    def _recolor(self):
        palette = self.label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, Theme.ink(.95))
        self.label.setPalette(palette)
        self.cancel_button.setIcon(template_icon("xmark.circle.fill", self.cancel_button, tint=Theme.secondary_ink()))
        self._update_copy_icon()
        self.bar.update()
        self.update()

    def _update_copy_icon(self):
        copied = self._copy_timer.isActive()
        if copied or self._symbol is not None:
            name = "doc.on.doc.fill" if copied else self._symbol
            if copied:
                tint = QColor("#30d158" if is_dark() else "#34c759")
            else:
                tint = self._tint() if callable(self._tint) else self._tint
            self.icon.setIcon(template_icon(name, self.icon, tint=tint))
        tip = "Copied" if copied else self._copy_tip
        self.icon.setToolTip(tip)
        self.icon.setAccessibleName(tip)

    def fit(self, max_width, max_height):
        """Size for the visible contents, without asking the stage layout for space."""
        margins = self.layout().contentsMargins()
        padding = margins.left() + margins.right()
        buttons = [button for button in (self.icon, self.cancel_button) if not button.isHidden()]
        inset = padding + sum(button.width() + self._row.spacing() for button in buttons)
        metrics = self.label.fontMetrics()
        measure = QFontMetricsF(self.label.font(), self.label)
        natural = ceil(max(measure.horizontalAdvance(line) for line in self.label.text().split("\n")))
        spinning = not self.bar.isHidden()
        width = min(max_width, max(natural + inset, self.bar.minimumWidth() + padding if spinning else 0))
        line = metrics.height()
        label_height = min(line + 2 * metrics.lineSpacing(), self.label.heightForWidth(max(1, width - inset)))
        extra = margins.top() + margins.bottom() + (self.layout().spacing() + self.bar.height() if spinning else 0)
        label_height = max(0, min(max_height - extra, max(line, label_height)))
        self.label.setFixedHeight(label_height)
        row_height = max(label_height, *(button.height() for button in buttons), 0)
        self.resize(width, min(max_height, row_height + extra))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Unlike painting transparent corners, this also clips the input region.
        self.setMask(_rounded_mask(QRectF(self.rect()), self._radius))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        material = Theme.bar_background()
        material.setAlphaF(.96)
        painter.fillPath(_rounded_path(QRectF(self.rect()), self._radius), material)

    def enterEvent(self, event):
        self.hovered = self._shown
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        super().leaveEvent(event)

    def copy_message(self):
        text = self.label.text()
        if text:
            QApplication.clipboard().setText(text)
            self._copy_timer.start(1200)
            self._update_copy_icon()

    def show(self, text=None, symbol="checkmark.circle.fill", tint=None, spinning=False, progress=None, remaining=None, cancellable=True):
        if text is not None:
            self._cancelling = spinning and ((self._shown and self._cancelling) or text == "Cancelling…")
            self.label.setText("Cancelling…" if self._cancelling else str(text))
            self._symbol = None if spinning else symbol
            self._tint = tint if callable(tint) else QColor(tint) if tint is not None else Theme.accent
            self._copy_timer.stop()
            self._update_copy_icon()
            self.icon.setVisible(self._symbol is not None)
            self.cancel_button.setVisible(spinning and cancellable and self.on_cancel is not None)
            self.cancel_button.setEnabled(not self._cancelling)
            self.bar.setVisible(spinning)
            if spinning:
                if not self._shown:
                    self.bar.reset()
                if progress is not None:
                    self.bar.set(progress)
                elif remaining is not None:
                    self.bar.run(remaining)
                elif not self.bar.running:
                    self.bar.run(self.default_estimate)
        if not self._shown:
            self._shown = True
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            super().show()
            self._animate_opacity(1., 180)
        self.contents_changed.emit()

    def cancel_pressed(self):
        if self._cancelling or self.on_cancel is None:
            return
        self._cancelling = True
        self.cancel_button.setEnabled(False)
        self.label.setText("Cancelling…")
        self.contents_changed.emit()
        self.on_cancel()

    def hide(self):
        if self._shown:
            self._shown = False
            self.hovered = False
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._animate_opacity(0., 250)

    def _animate_opacity(self, target, duration):
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(target)
        self._fade.setDuration(duration)
        self._fade.start()

    def _finish_fade(self):
        if not self._shown:
            self._copy_timer.stop()
            self.bar.reset()
            super().hide()


class _StageCard(QWidget):
    def __init__(self, center_split, parent):
        super().__init__(parent)
        self._split = center_split
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.addWidget(center_split)
        watch_appearance(self.update)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The splitter (including both panes and its divider) shares the card's clip.
        self._split.setMask(_rounded_mask(QRectF(0, 0, self.width() - 2, self.height() - 2), 11))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Theme.ink(.22), 1))
        painter.setBrush(Theme.canvas_bottom())
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 11.5, 11.5)


class StageHost(QWidget):
    """Keep the editor stage and both independently raised overlays in one column."""
    def __init__(self, center_split, busy_pill, toast_pill):
        super().__init__()
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(_StageCard(center_split, self))
        self._busy, self._toast = busy_pill, toast_pill
        for pill in (toast_pill, busy_pill):
            pill.setParent(self)
            pill.contents_changed.connect(self.place_pills)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.place_pills()

    def place_pills(self):
        width, height = self.width(), self.height()
        if not self._toast.isHidden():
            self._toast.fit(max(0, min(620, width - 20)), max(0, height - 100))
            self._toast.move((width - self._toast.width()) // 2, height - self._toast.height() - 90)
            self._toast.raise_()
        if not self._busy.isHidden():
            self._busy.fit(max(0, width - 34), max(0, height - 32))
            self._busy.move(width - self._busy.width() - 24, 22)
            self._busy.raise_()


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def paintEvent(self, event):
        editor = self.editor
        painter = QPainter(self)
        painter.fillRect(event.rect(), Theme.code_background())
        painter.setFont(editor.font())
        gap = editor.gutter_gap()
        block = editor.firstVisibleBlock()
        top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            height = editor.blockBoundingRect(block).height()
            if block.isVisible() and top + height >= event.rect().top():
                rect = QRectF(0, top, self.width(), height)
                color = editor.line_backgrounds.get(block.blockNumber())
                if color is not None:
                    painter.fillRect(rect, color)
                painter.setPen(editor.comment_ink)
                painter.drawText(rect.adjusted(0, 0, -gap, 0),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 str(block.blockNumber() + editor.start_line))
            top += height
            block = block.next()


class SourceCodeView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_focus = self.on_blur = self.on_change = None
        self.start_line = None
        self.line_backgrounds = {}
        self.comment_ink = Theme.ink(.5)
        self.setFont(Theme.mono(11.5))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.document().setDocumentMargin(6)
        self.gutter = _LineNumbers(self)
        self.blockCountChanged.connect(self._margin)
        self.updateRequest.connect(self._update_gutter)
        self.textChanged.connect(lambda: self.on_change() if self.on_change else None)
        self._margin()

    def set_numbering(self, start_line=None):
        self.start_line = start_line
        self._margin()
        self.gutter.update()

    def gutter_gap(self):
        advance = QFontMetricsF(self.font()).horizontalAdvance("9")
        return max(0, ceil(2 * advance - self.document().documentMargin()))

    def _margin(self):
        numbered = self.start_line is not None
        width = 0
        if numbered:
            digits = max(5, len(str(self.blockCount() + self.start_line - 1)))
            advance = QFontMetricsF(self.font()).horizontalAdvance("9")
            width = ceil(digits * advance + self.document().documentMargin()) + self.gutter_gap()
        margins = self.viewportMargins()
        if margins.left() != width:
            self.setViewportMargins(width, 0, 0, 0)
        viewport = self.viewport().geometry()
        self.gutter.setGeometry(viewport.x() - width, viewport.y(), width, viewport.height())
        self.gutter.setVisible(numbered)

    def set_line_backgrounds(self, backgrounds):
        self.line_backgrounds = backgrounds
        highlights = []
        for index, color in backgrounds.items():
            extra = QTextEdit.ExtraSelection()
            extra.cursor = QTextCursor(self.document().findBlockByNumber(index))
            extra.format.setBackground(color)
            extra.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            highlights.append(extra)
        self.setExtraSelections(highlights)
        self.gutter.update()

    def reveal_line(self, index):
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        while block.isValid() and top < self.viewport().height():
            height = self.blockBoundingRect(block).height()
            if block.blockNumber() == index and top >= 0 and top + height <= self.viewport().height():
                return
            top += height
            block = block.next()
        # In a no-wrap QPlainTextEdit the vertical scroll unit is a block, not a pixel.
        self.verticalScrollBar().setValue(index)

    def _update_gutter(self, rect, dy):
        if self.start_line is None:
            return
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


def _thumbnail_rect(rect, image):
    scale = min(rect.width() / image.width(), rect.height() / image.height())
    w, h = image.width() * scale, image.height() * scale
    return QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)


def _aspect_height(width):
    return (width * 9 + 8) // 16


class BuildPanel(QWidget):
    _strip = False

    def __init__(self, image, index, active, handler, parent=None):
        super().__init__(parent)
        self.image = image
        self.number, self.active, self.handler = str(index + 1), active, handler
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Build {self.number}")
        if not self._strip:
            self.setToolTip(self.accessibleName())
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        watch_appearance(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        radius = 6 if self._strip else 5
        clip = _rounded_path(rect, radius)
        painter.setClipPath(clip)
        if not self._strip:
            painter.fillPath(clip, Qt.GlobalColor.black)
        if not self.image.isNull():
            painter.setOpacity(1. if self.active else .78 if self._strip else .72)
            painter.drawImage(rect if self._strip else _thumbnail_rect(rect, self.image), self.image)
            painter.setOpacity(1.)
        border = Theme.accent if self.active else Theme.ink(.14 if self._strip else .35)
        width = 2 if self.active else 1
        painter.setPen(QPen(border, width))
        inset = width / 2
        painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), radius - inset, radius - inset)

        badge = QRectF(4, 3, 14, 13 if self._strip else 12)
        painter.fillPath(_rounded_path(badge, 4 if self._strip else 3), QColor.fromRgbF(0, 0, 0, .55))
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(Theme.font(10 if self._strip else 9, QFont.Weight.Bold))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.number)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.handler:
            self.handler()
            event.accept()
        else:
            super().mousePressEvent(event)


class StepButton(BuildPanel):
    _strip = True

    def __init__(self, path, index=0, active=False, handler=None, parent=None):
        super().__init__(_mapped_image(path), index, active, handler, parent)
        self.setFixedSize(80, 45)


def _mapped_image(path):
    if not path:
        return QImage()
    from .darkmode import DarkMode
    return DarkMode.image(path=str(path)) or QImage()


class SlideCell(QWidget):
    thumb_inset, build_indent, build_spacing, base_height = 42, 16, 6, 108

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_build = None
        self.on_click = None
        self._number_font = Theme.font(11, QFont.Weight.Medium)
        self._caption_font = Theme.font(10)
        self._badge_font = Theme.font(10, QFont.Weight.Medium)
        self._number = self._caption = self._badge = ""
        self._image = QImage()
        self._active = self._hidden = False
        self._panels = []
        watch_appearance(self.update)

    @staticmethod
    def _caption_height():
        return max(13, QFontMetrics(Theme.font(10, QFont.Weight.Medium)).height())

    @classmethod
    def row_height(cls, column_width, builds=0):
        width = max(40, column_width - cls.thumb_inset)
        caption_bottom = 6 + _aspect_height(width) + 4 + cls._caption_height()
        if builds <= 0:
            return max(cls.base_height, caption_bottom + 2)
        top = max(cls.base_height, caption_bottom + cls.build_spacing)
        return top + builds * (_aspect_height(max(32, width - cls.build_indent)) + cls.build_spacing) + 2

    def configure(self, number, name, image=None, status="pending", stops=0, active=False, unused=False, hidden=False, expanded=None):
        self._caption = f"{name} — error" if status == "error" else name
        self._hidden = hidden
        self._image = image if isinstance(image, QImage) else QImage()
        if hidden and self._image.hasAlphaChannel():
            black = QImage(self._image.size(), QImage.Format.Format_RGB32)
            black.fill(Qt.GlobalColor.black)
            with QPainter(black) as painter:
                painter.drawImage(0, 0, self._image)
            self._image = black
        self._number = "–" if unused else str(number)
        self._number_font.setStrikeOut(hidden and not unused)
        chevron = "" if expanded is None else " ▾" if expanded else " ▸"
        if unused:
            self._badge = "unused"
        elif hidden:
            self._badge = "hidden"
        elif stops > 1:
            self._badge = f"{stops} builds{chevron}"
        else:
            self._badge = "…" if self._image.isNull() and status != "error" else ""
        self.setAccessibleName(" ".join(filter(None, (self._number, self._caption, self._badge))))
        self.set_active(active)
        self.set_builds([], -1)

    def set_active(self, active):
        self._active = active
        self.update()

    def set_builds(self, images, active=-1):
        for panel in self._panels:
            panel.hide()
            panel.deleteLater()
        self._panels = []
        for i, image in enumerate(images):
            panel = BuildPanel(image, i, i == active, lambda index=i: self.on_build(index) if self.on_build else None, self)
            panel.show()
            self._panels.append(panel)
        self._layout_builds()

    def _layout_builds(self):
        thumb_width = max(40, self.width() - self.thumb_inset)
        build_width = max(32, thumb_width - self.build_indent)
        height = _aspect_height(build_width)
        top = max(self.base_height, 6 + _aspect_height(thumb_width) + 4 + self._caption_height() + self.build_spacing)
        for panel in self._panels:
            panel.setGeometry(30 + self.build_indent, top, build_width, height)
            top += height + self.build_spacing

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_builds()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        width = max(40, self.width() - self.thumb_inset)
        rect = QRectF(30, 6, width, _aspect_height(width))
        clip = _rounded_path(rect, 5)
        if self._active:
            ring = rect.adjusted(-2, -2, 2, 2)
            shadow = ring.translated(0, 1.5)
            for spread, alpha in ((4, .02), (3, .03), (2, .05), (1, .08), (0, .13)):
                path = _rounded_path(shadow.adjusted(-spread, -spread, spread, spread), 7 + spread)
                painter.fillPath(path.subtracted(clip), QColor.fromRgbF(0, 0, 0, alpha))
            painter.setPen(QPen(Theme.accent, 2.5))
            painter.drawRoundedRect(ring, 7, 7)
        if self._image.isNull():
            painter.setOpacity(.4 if self._hidden else 1.)
            painter.fillPath(clip, Qt.GlobalColor.black)
            painter.setOpacity(1.)
        else:
            art = _thumbnail_rect(rect, self._image)
            painter.save()
            painter.setOpacity(.4 if self._hidden else 1.)
            matte = clip
            if self._hidden:
                # The matte and opaque image are one layer, not two applications of .4 opacity.
                cutout = QPainterPath()
                cutout.addRect(art)
                matte = clip.subtracted(cutout)
            painter.fillPath(matte, Qt.GlobalColor.black)
            painter.setClipPath(clip)
            painter.drawImage(art, self._image)
            painter.restore()

        painter.setPen(Theme.accent if self._active else Theme.tertiary_ink())
        painter.setFont(self._number_font)
        align_right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(QRectF(2, 8, 20, QFontMetrics(self._number_font).height()), align_right, self._number)

        caption_y = rect.bottom() + 4
        caption_h = self._caption_height()
        badge_w = QFontMetrics(self._badge_font).horizontalAdvance(self._badge)
        badge_x = rect.right() - 2 - badge_w
        painter.setFont(self._badge_font)
        painter.setPen(Theme.tertiary_ink())
        painter.drawText(QRectF(badge_x, caption_y, badge_w, caption_h), align_right, self._badge)

        name_width = max(0, int(badge_x - 4 - 32))
        name = QFontMetrics(self._caption_font).elidedText(self._caption, Qt.TextElideMode.ElideRight, name_width)
        painter.setFont(self._caption_font)
        painter.setPen(Theme.ink(.95) if self._active else Theme.secondary_ink())
        painter.drawText(QRectF(32, caption_y, name_width, caption_h),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

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
        self.setSpacing(0)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setStyleSheet("QListWidget { background: transparent; border: none; outline: none; }"
                           "QListWidget::item:selected { background: transparent; }")

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
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        watch_appearance(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.orientation() == Qt.Orientation.Horizontal:
            line = QRectF(self.width() // 2, 0, 1, self.height())
        else:
            line = QRectF(0, self.height() // 2, self.width(), 1)
        painter.fillRect(line, Theme.separator())


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
                if self._collapses_edge_panes and self.orientation() == Qt.Orientation.Horizontal and len(sizes) == 3:
                    sizes = self._fit_saved_widths(sizes)
                self.setSizes(sizes)

    def _fit_saved_widths(self, widths):
        span = self.contentsRect().width() - (self.count() - 1) * self.handleWidth()
        widths = [max(w, self.widget(i).minimumWidth()) if w or i == 1 else 0 for i, w in enumerate(widths)]
        excess = sum(widths) - span
        if excess < 0:
            widths[1] -= excess
        else:
            for i in (1, 2, 0):
                reduction = min(excess, max(0, widths[i] - self.widget(i).minimumWidth()))
                widths[i] -= reduction
                excess -= reduction
        return widths

    def _resized(self, *_):
        if self.settings_key:
            prefs.set(self.settings_key, self.sizes())
        if self.on_resize:
            self.on_resize()

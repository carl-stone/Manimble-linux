"""Editable object inspector, table grid, and source-safe geometry controls."""
from __future__ import annotations

import json
from math import ceil, floor, isfinite

from PySide6.QtCore import QEvent, QLocale, QRectF, QSize, Qt, QSignalBlocker
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QFontDatabase, QPainter, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QStyle,
    QTextEdit, QVBoxLayout, QWidget)

from .formatbar import TextTransform
from .icons import template_icon
from .models import char_styles, stop_box
from .style import Theme, color_from_hex, watch_appearance
from .widgets import field_label, hairline, section_label


def table_rows(value):
    try:
        rows = json.loads(value) if isinstance(value, str) else value
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            return []
        return [[str(int(c)) if isinstance(c, float) and c.is_integer() else str(c) for c in row] for row in rows]
    except (TypeError, ValueError):
        return []


class InspectorLabel(QLabel):
    def __init__(self, text, font, *, ink=None, wrap=False):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFont(font)
        self.setWordWrap(wrap)
        if wrap:
            policy = self.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            self.setSizePolicy(policy)
        self.ink = ink
        if ink is not None:
            self._recolor()
            watch_appearance(self._recolor)

    def _recolor(self):
        stylesheet = f"color: {self.ink().name(QColor.NameFormat.HexArgb)}"
        if self.styleSheet() != stylesheet:
            self.setStyleSheet(stylesheet)


class InspectorStack(QWidget):
    def sizeHint(self):
        return QSize(292, super().sizeHint().height())


class InspectorSymbol(QWidget):
    def __init__(self, symbol):
        super().__init__()
        self.setFixedSize(15, 15)
        self.icon = template_icon(symbol, self, tint=Theme.accent)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            self.icon.paint(painter, self.rect())


def inspector_row(spacing):
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(spacing)
    return widget, row


class SharedScopeLabel(InspectorLabel):
    def __init__(self, title, checkbox):
        super().__init__(title, Theme.font(11), wrap=True)
        self.checkbox = checkbox

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.checkbox.click()
        else:
            super().mouseReleaseEvent(event)


class ColorWell(QPushButton):
    def __init__(self, color):
        super().__init__()
        self.color = color_from_hex(color)
        self.setAccessibleName("Color")
        self.setFixedSize(60, 26)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        watch_appearance(self.update)

    def paintEvent(self, event):
        super().paintEvent(event)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Theme.separator())
            painter.setBrush(self.color)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(5.5, 5.5, -5.5, -5.5), 3, 3)


class NumberField(QLineEdit):
    def __init__(self, name, text, on_commit, *, minimum=None):
        super().__init__(text)
        self.setAccessibleName(name)
        self.setFont(Theme.font(13))
        self.setFixedWidth(80)
        validator = QDoubleValidator(self)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.NumberOption.RejectGroupSeparator)
        validator.setLocale(locale)
        if minimum is not None:
            validator.setBottom(minimum)
        self.setValidator(validator)
        self.on_commit = on_commit
        self.editingFinished.connect(self._commit)

    def _commit(self):
        if self.isModified() and self.hasAcceptableInput():
            value = float(self.text())
            if isfinite(value):
                self.setModified(False)
                self.on_commit(value)


class SizeStepper(QDoubleSpinBox):
    def __init__(self, value):
        super().__init__()
        self.setAccessibleName("Font size")
        self.setRange(4, 300)
        self.setDecimals(1)
        self.setValue(value)
        self.setFixedWidth(18)
        self.setFrame(False)
        self.lineEdit().hide()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class TableCell(QLineEdit):
    def __init__(self, grid, row, col, text):
        super().__init__(text)
        self.grid = grid
        self.row, self.col = row, col
        self.setAccessibleName(f"Row {row + 1}, column {col + 1}")
        self.setFont(Theme.font(12))
        self.returnPressed.connect(grid.apply)

    def event(self, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            mods = event.modifiers()
            if mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                forward = event.key() == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ShiftModifier)
                self.grid.focus_adjacent(self.row, self.col, forward)
                return True
        return super().event(event)


class TableGrid(QScrollArea):
    def __init__(self, apply):
        super().__init__()
        self.apply = apply
        self.cells = []
        self.source_rows = None
        self.applying = None
        self.setAccessibleName("Cells")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMaximumWidth(292)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def cell(self, row, col):
        if 0 <= row < len(self.cells) and 0 <= col < len(self.cells[row]):
            return self.cells[row][col]

    def focus_cell(self, row, col, reason=Qt.FocusReason.OtherFocusReason):
        field = self.cell(row, col)
        if field is not None:
            field.setFocus(reason)
            field.selectAll()
            self.ensureWidgetVisible(field, 0, 0)

    def focus_adjacent(self, row, col, forward):
        cols = len(self.cells[0])
        index = (row * cols + col + (1 if forward else -1)) % (len(self.cells) * cols)
        reason = Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason
        row, col = divmod(index, cols)
        self.focus_cell(row, col, reason)

    def draft(self):
        return [[field.text() for field in row] for row in self.cells]

    def update_source(self, rows, *, replace=True):
        self.source_rows = rows
        if not replace:
            return
        padded = rows or [[""]]
        cols = max(1, max(map(len, padded)))
        if len(self.cells) != len(padded) or len(self.cells[0]) != cols:
            self.set_rows(rows)
        else:
            for cells, row in zip(self.cells, padded):
                for col, field in enumerate(cells):
                    value = row[col] if col < len(row) else ""
                    if field.text() != value:
                        field.setText(value)

    def set_rows(self, rows):
        rows = rows or [[""]]
        cols = max(1, max(map(len, rows)))
        document = QWidget()
        layout = QGridLayout(document)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.cells = [
            [TableCell(self, r, c, row[c] if c < len(row) else "") for c in range(cols)]
            for r, row in enumerate(rows)
        ]
        for r, row in enumerate(self.cells):
            for c, field in enumerate(row):
                layout.addWidget(field, r, c, Qt.AlignmentFlag.AlignTop)
        self.setWidget(document)
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        document = self.widget()
        if document is None or not self.cells:
            return
        rows, cols = len(self.cells), len(self.cells[0])
        cell_height = max(22, self.cells[0][0].fontMetrics().height() + 6)
        height = rows * (cell_height + 2) + (rows - 1) * 4
        scrollbar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self)
        available = min(292, self.width()) - (scrollbar if height > 220 else 0)
        width = max(56, (available - (cols - 1) * 4) // cols)
        document_width = cols * width + (cols - 1) * 4
        if document.width() != document_width or document.height() != height:
            for r, row in enumerate(self.cells):
                document.layout().setRowMinimumHeight(r, cell_height + 2)
                for field in row:
                    field.setFixedSize(width, cell_height)
            document.setFixedSize(document_width, height)
        view_height = min(height, 220) + (scrollbar if document_width > available else 0)
        if self.minimumHeight() != view_height:
            self.setFixedHeight(view_height)


class InspectorTextEdit(QTextEdit):
    """Qt's UTF-16 cursor ranges are converted to engine code-point spans at commit."""
    def __init__(self, text, committed, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFont(Theme.font(13))
        self.document().setDocumentMargin(4)
        frame = self.document().rootFrame().frameFormat()
        frame.setLeftMargin(2)
        frame.setRightMargin(2)
        self.document().rootFrame().setFrameFormat(frame)
        self.setPlainText(text)
        self.original = text
        self.original_styles = {}
        self.on_commit = committed
        self.text_script_mode = None
        self.touched_styles = set()
        self.body_bullets = False
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit(self.document().size())

    def _fit(self, size):
        height = ceil(min(size.height(), 14 * self.fontMetrics().lineSpacing() + 8)) + 2 * self.frameWidth()
        if self.minimumHeight() != height:
            self.setFixedHeight(height)

    def set_text(self, text, styles=None):
        if self.hasFocus() or self.touched_styles:
            return
        if text == self.toPlainText() and (styles is None or styles == self.original_styles):
            return
        self.setPlainText(text)
        self.original = text
        self.load_styles(styles or {})

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Escape:
            self.setPlainText(self.original)
            self.load_styles(self.original_styles)
            self.clearFocus()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.commit(); return
            cursor = self.textCursor()
            line = cursor.block().text()[:cursor.positionInBlock()]
            prefix = TextTransform.newline_prefix(line, self.body_bullets)
            match = TextTransform.marker.fullmatch(line)
            if match:
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText(); self.setTextCursor(cursor)
            else:
                cursor.insertText("\n" + prefix); self.setTextCursor(cursor)
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            cursor = self.textCursor()
            text = self.toPlainText()
            range_ = TextTransform.line_range(text, (cursor.selectionStart(), cursor.selectionEnd() - cursor.selectionStart()))
            new = TextTransform.indent(text, range_, -4 if key == Qt.Key.Key_Backtab or mods & Qt.KeyboardModifier.ShiftModifier else 4)
            cursor.beginEditBlock(); cursor.select(QTextCursor.SelectionType.Document); cursor.insertText(new); cursor.endEditBlock()
            self.setTextCursor(cursor); return
        if self.text_script_mode is not None and event.text() and not mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.textCursor().insertText(TextTransform.raise_text(event.text(), self.text_script_mode)); return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # Toolbar focus must retain the selection and typing marks until click-away commits it.
        if event.reason() not in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
            self.text_script_mode = None
            self.commit()

    def commit(self):
        text = self.toPlainText()
        if text == self.original and not self.touched_styles:
            return
        self.original_styles = self.style_runs(text, self.original_styles.keys() | self.touched_styles)
        styles = {kind: self.original_styles[kind] for kind in self.touched_styles}
        self.original = text
        self.touched_styles.clear()
        self.on_commit(text, styles)

    def script_selection(self, superscript):
        cursor = self.textCursor()
        if not cursor.hasSelection(): return False
        result = TextTransform.script(cursor.selectedText().replace("\u2029", "\n"), superscript)
        if result is None: return False
        cursor.insertText(result); self.setTextCursor(cursor); return True

    def set_inline_style(self, kind, on=True, hex=None):
        fmt = QTextCharFormat()
        if kind == "bold": fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        elif kind == "italic": fmt.setFontItalic(on)
        elif kind == "underline": fmt.setFontUnderline(on)
        elif kind == "strike": fmt.setFontStrikeOut(on)
        elif kind == "color": fmt.setForeground(QColor(hex)) if on else fmt.clearForeground()
        elif kind == "highlight": fmt.setBackground(QColor(hex)) if on else fmt.setBackground(QColor(Qt.GlobalColor.transparent))
        cursor = self.textCursor(); cursor.mergeCharFormat(fmt)
        if not cursor.hasSelection(): self.mergeCurrentCharFormat(fmt)
        self.touched_styles.add(kind)
        return on

    def toggle_inline_style(self, kind):
        fmt = self.textCursor().charFormat()
        on = {"bold": fmt.fontWeight() >= QFont.Weight.Bold, "italic": fmt.fontItalic(), "underline": fmt.fontUnderline(), "strike": fmt.fontStrikeOut()}.get(kind, False)
        return self.set_inline_style(kind, not on)

    def load_styles(self, styles):
        self.original_styles = dict(styles)
        if not styles:
            self.touched_styles.clear()
            return
        offsets = [0]
        for char in self.toPlainText():
            offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
        for kind, runs in styles.items():
            for run in runs:
                if isinstance(run, dict):
                    a, b, color = run.get("start", 0), run.get("end", 0), run.get("color")
                else:
                    a, b = run[:2]; color = run[2] if len(run) > 2 else None
                if 0 <= a < b < len(offsets):
                    cursor = self.textCursor()
                    cursor.setPosition(offsets[a])
                    cursor.setPosition(offsets[b], QTextCursor.MoveMode.KeepAnchor)
                    self.setTextCursor(cursor)
                    self.set_inline_style(kind, True, color)
        cursor = self.textCursor(); cursor.setPosition(0); self.setTextCursor(cursor)
        self.touched_styles.clear()
        self.document().clearUndoRedoStacks()

    def style_runs(self, text, kinds):
        if not kinds:
            return {}
        result = {kind: [] for kind in kinds}
        offset = 0
        for index, char in enumerate(text):
            cursor = QTextCursor(self.document()); cursor.setPosition(offset)
            units = 2 if ord(char) > 0xFFFF else 1
            cursor.setPosition(offset + units, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            values = {"bold": fmt.fontWeight() >= QFont.Weight.Bold, "italic": fmt.fontItalic(), "underline": fmt.fontUnderline(), "strike": fmt.fontStrikeOut(), "color": fmt.foreground().color().name().upper() if fmt.hasProperty(QTextCharFormat.Property.ForegroundBrush) else None, "highlight": fmt.background().color().name().upper() if fmt.background().color().alpha() and fmt.hasProperty(QTextCharFormat.Property.BackgroundBrush) else None}
            for kind in kinds:
                value = values[kind]
                if value:
                    run = [index, index + 1] + ([value] if kind in ("color", "highlight") else [])
                    runs = result[kind]
                    if runs and runs[-1][1] == index and runs[-1][2:] == run[2:]: runs[-1][1] = index + 1
                    else: runs.append(run)
            offset += units
        return result


class InspectorMixin:
    def rebuild_inspector(self):
        if not hasattr(self, "inspector") or self.inspector is None:
            self.inspector = QWidget()
        if not hasattr(self, "_inspector_fields"):
            self.inspector.setBackgroundRole(QPalette.ColorRole.Window)
            self.inspector.setAutoFillBackground(True)
            root = self.inspector.layout()
            if root is None:
                root = QVBoxLayout(self.inspector)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(0)
            stack = InspectorStack()
            stack.setMaximumWidth(292)
            layout = self._inspector_fields = QVBoxLayout(stack)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(9)
            row, horizontal = inspector_row(0)
            horizontal.addWidget(stack)
            horizontal.addStretch()
            root.addWidget(row)
            root.addStretch()
        layout = self._inspector_fields
        obj = self.obj_info or {}
        kind = obj.get("kind")
        target = (self._deck_epoch, self.scene_name, self.selection, obj.get("loc"))
        same_target = getattr(self, "_inspector_target", None) == target
        self._inspector_target = target
        self.text_field = self.text_field if same_target and kind == "text" and obj.get("textOk") else None
        self.table_grid = getattr(self, "table_grid", None) if same_target and kind == "table" else None
        self.size_field = None
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget is not self.text_field and widget is not self.table_grid:
                widget.hide()
                widget.deleteLater()

        if not obj:
            layout.addWidget(self.inspector_header("rectangle.on.rectangle", "Slide", self.scene_name))
            layout.addWidget(hairline())
            keys = [self.actions.shortcut_text(action) if action in self.actions else default
                    for action, default in (("undo", "Ctrl+Z"), ("prev_slide", "Ctrl+Up"), ("next_slide", "Ctrl+Down"),
                                            ("prev_stop", "Ctrl+["), ("next_stop", "Ctrl+]"))]
            for tip in (
                "Click an element to edit it",
                "Double-click text to retype, a formula for LaTeX",
                "Drag to move · drag a handle to resize",
                "Arrow keys nudge · Shift for bigger steps",
                f"{keys[0]} undo · {keys[1]} {keys[2]} slides · {keys[3]} {keys[4]} builds",
            ):
                layout.addWidget(InspectorLabel(tip, Theme.font(12), ink=Theme.secondary_ink, wrap=True))
        else:
            title, symbol = {
                "text": ("Text", "textformat"), "tex": ("Formula", "function"), "image": ("Image", "photo"),
                "line": (obj.get("shape") or "Line", "line.diagonal"),
                "shape": (obj.get("shape") or "Shape", "square.on.circle"),
                "table": ("Table", "tablecells"), "drawing": ("Drawing", "scribble.variable"),
            }.get(kind, (kind or "Object", "square"))
            layout.addWidget(self.inspector_header(symbol, title.title(), obj.get("loc")))
            layout.addWidget(hairline())
            self.inspector_object(layout, obj, kind)
            if box := stop_box(self.canvas.stop, self.selection):
                self.geometry_rows(layout, box, obj)
            if (obj.get("siblings") or 0) > 1:
                layout.addWidget(InspectorLabel(
                    f"Created in a loop/helper: {obj['siblings']} objects share this source line.",
                    Theme.font(13), ink=Theme.warning_ink, wrap=True,
                ))
        self.sync_format_bar()

    @staticmethod
    def inspector_header(symbol, title, detail):
        widget, row = inspector_row(9)
        row.addWidget(InspectorSymbol(symbol), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QWidget()
        labels = QVBoxLayout(text)
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(0)
        labels.addWidget(InspectorLabel(title, Theme.font(14, QFont.Weight.DemiBold)))
        labels.addWidget(InspectorLabel(detail or "", Theme.mono(10), ink=Theme.tertiary_ink, wrap=True))
        row.addWidget(text, 1)
        return widget

    def inspector_object(self, layout, obj, kind):
        note = obj.get("textNote")
        note = "computed" if note is None else note
        if kind == "text":
            if obj.get("textOk"):
                oid, target, seen = self.selection, self.scene_name, self.edit_context.seen
                source = obj.get("value") or ""
                value = self.pending_texts.get(f"{target}:{oid}", source)
                styles = char_styles(obj) if source == value else {}
                def commit(text, styles):
                    return self.commit_text(oid, text, shared=self.shared_choice, styles=styles, slide=target, seen=seen)
                if self.text_field is None:
                    self.text_field = InspectorTextEdit(value, commit)
                    self.text_field.load_styles(styles)
                    self.bind_inspector_text_signals(self.text_field)
                else:
                    self.text_field.set_text(value, styles)
                    if not self.text_field.hasFocus():
                        self.text_field.on_commit = commit
                title = field_label("Text")
                title.setBuddy(self.text_field)
                self.text_field.setAccessibleName("Text")
                layout.addWidget(title)
                layout.addWidget(self.text_field)
                layout.addWidget(InspectorLabel("↵ new line  ·  Ctrl+Return apply  ·  esc revert",
                                               Theme.font(10), ink=Theme.tertiary_ink, wrap=True))
                self.shared_row(layout, obj)
            else:
                layout.addWidget(InspectorLabel(f"Text is generated by code ({note}); edit it in the source.",
                                               Theme.font(13), ink=Theme.warning_ink, wrap=True))
            self.style_fields(layout, obj)
        elif kind == "tex":
            button = QPushButton("Edit LaTeX…")
            button.setFont(Theme.font(13))
            button.clicked.connect(self.edit_latex_pressed)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            formula = InspectorLabel(obj.get("value") or "", Theme.mono(11.5), wrap=True)
            formula.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(formula)
            if not obj.get("textOk"):
                layout.addWidget(InspectorLabel(f"Formula is generated by code ({note}).",
                                               Theme.font(13), ink=Theme.warning_ink, wrap=True))
            self.style_fields(layout, obj)
        elif kind == "table":
            self.table_editor(layout, obj)
        elif kind == "image":
            layout.addWidget(field_label("File"))
            layout.addWidget(InspectorLabel(obj.get("image") or "", Theme.font(13), wrap=True))
        elif kind == "drawing":
            layout.addWidget(InspectorLabel(
                f"{(obj.get('value') or '').title()} made by one line of code, kept together: drag to move it, "
                "drag a corner to resize it. Change anything else in the source.",
                Theme.font(13), ink=Theme.secondary_ink, wrap=True,
            ))
        else:
            self.add_color_well(layout, obj.get("color"))
            if kind == "line":
                layout.addWidget(InspectorLabel("Drag an end handle to change the line. Hold Shift to snap the angle.",
                                               Theme.font(13), ink=Theme.secondary_ink, wrap=True))

    def shared_row(self, layout, obj):
        if shared := obj.get("shared"):
            title = f"Change everywhere ({shared.get('name')} in {shared.get('rel')}, used {shared.get('uses')}×)"
            widget, row = inspector_row(3)
            box = QCheckBox()
            box.setAccessibleName(title)
            box.setChecked(self.shared_choice)
            box.toggled.connect(self.shared_toggled)
            label = SharedScopeLabel(title, box)
            row.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(label, 1)
            layout.addWidget(widget)

    def shared_toggled(self, on):
        self.shared_choice = on

    def style_fields(self, layout, obj):
        layout.addWidget(section_label("Appearance"))
        if obj.get("kind") == "text":
            family = QComboBox()
            family.setAccessibleName("Font")
            family.setFont(Theme.font(13))
            family.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            family.addItems(QFontDatabase.families())
            if original := obj.get("font"):
                if family.findText(original) < 0:
                    family.insertItem(0, original)
                family.setCurrentText(original)
            family.setToolTip("The font is usually a deck-wide constant, so this changes it for every slide.")
            family.textActivated.connect(self.family_changed)
            layout.addWidget(field_label("Font"))
            layout.addWidget(family)

            styles, row = inspector_row(6)
            for title, name, state, callback in (
                ("B", "Bold", (obj.get("weight") or "").upper() == "BOLD", self.bold_toggled),
                ("I", "Italic", (obj.get("slant") or "").upper() in ("ITALIC", "OBLIQUE"), self.italic_toggled),
            ):
                button = QPushButton(title)
                button.setAccessibleName(name)
                font = Theme.font(13, QFont.Weight.Bold if title == "B" else QFont.Weight.Normal)
                font.setItalic(title == "I")
                button.setFont(font)
                button.setFixedWidth(34)
                button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                button.setCheckable(True)
                button.setChecked(state)
                button.clicked.connect(lambda _, b=button, c=callback: c(b))
                row.addWidget(button)
            row.addStretch()
            layout.addWidget(field_label("Style"))
            layout.addWidget(styles)

        size = obj.get("fontSize")
        stepper = SizeStepper(30 if size is None else size)
        def size_changed(value):
            with QSignalBlocker(stepper):
                stepper.setValue(value)
            self.commit_font_size(value)
        self.size_field = NumberField("Size", f"{size or 0:.1f}", size_changed, minimum=4)
        field = self.size_field
        def size_stepped(value):
            field.setText(f"{value:.1f}")
            home = self.format_bar.size_field
            if not home.hasFocus():
                home.setText(home.validator().locale().toString(value, "g", 8))
            self.commit_font_size(value)
        stepper.valueChanged.connect(size_stepped)
        widget, row = inspector_row(6)
        row.addWidget(field)
        row.addWidget(stepper)
        row.addStretch()
        layout.addWidget(field_label("Size"))
        layout.addWidget(widget)
        self.add_color_well(layout, obj.get("color"))

    def add_color_well(self, layout, color):
        well = ColorWell(color)
        well.clicked.connect(lambda: self.open_color_panel())
        layout.addWidget(field_label("Color"))
        layout.addWidget(well, alignment=Qt.AlignmentFlag.AlignLeft)

    def geometry_rows(self, layout, box, obj):
        kind = obj.get("kind")
        layout.addWidget(hairline())
        layout.addWidget(section_label("Arrange"))
        def field(title, value, callback, *, decimals=0):
            widget = QWidget()
            col = QVBoxLayout(widget)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            col.addWidget(field_label(title))
            display = f"{value:.{decimals}f}" if decimals else str(floor(value + .5) if value >= 0 else ceil(value - .5))
            col.addWidget(NumberField(title, display, callback))
            return widget

        def add_row(*fields):
            widget, row = inspector_row(16)
            for child in fields:
                row.addWidget(child, 0, Qt.AlignmentFlag.AlignBottom)
            row.addStretch()
            layout.addWidget(widget)

        add_row(field("X (px)", box.cx, self.x_committed), field("Y (px)", box.cy, self.y_committed))
        if kind == "drawing":
            return
        width, height = obj.get("dims") or (box.w, box.h)
        add_row(field("Width", width, self.w_committed), field("Height", height, self.h_committed))
        if kind != "line":
            rotation = field("Rotation (°)", obj.get("rotation") or 0, self.rotation_committed, decimals=1)
            if kind in ("image", "shape"):
                aspect = QCheckBox("Keep aspect ratio")
                aspect.setFont(Theme.font(11))
                aspect.setChecked(self.aspect_locked)
                aspect.setToolTip("Off: width and height stretch the picture or shape independently")
                aspect.toggled.connect(self.aspect_toggled)
                add_row(rotation, aspect)
            else:
                add_row(rotation)

    def aspect_toggled(self, on):
        self.aspect_locked = on

    def current_dims(self, oid):
        dims = (self.obj_info or {}).get("dims")
        if dims and len(dims) == 2 and min(dims) > 0: return dims
        box = stop_box(getattr(self.canvas, "stop", None), oid)
        if box and box.w > 0 and box.h > 0: return box.w, box.h

    def x_committed(self, value):
        box = stop_box(self.canvas.stop, self.selection)
        if box:
            return self.commit_move(self.selection, value - box.cx, 0)

    def y_committed(self, value):
        box = stop_box(self.canvas.stop, self.selection)
        if box:
            return self.commit_move(self.selection, 0, value - box.cy)

    def w_committed(self, value):
        return self.commit_dimension(width=value)

    def h_committed(self, value):
        return self.commit_dimension(height=value)

    def commit_dimension(self, width=None, height=None):
        oid, obj = self.selection, self.obj_info or {}
        dims = self.current_dims(oid)
        if oid is None or not dims: return
        if self.aspect_locked or obj.get("kind") not in ("image", "shape"):
            if width is not None and width > 0: return self.commit_resize(oid, width / dims[0])
            if height is not None and height > 0: return self.commit_resize(oid, height / dims[1])
        req = {"op": "size", "oid": oid}
        if width is not None and width > 0: req["width"] = width
        if height is not None and height > 0: req["height"] = height
        if len(req) > 2: return self.submit(lambda: self.run_edit(req))

    def rotation_committed(self, value):
        oid = self.selection
        if oid is not None:
            req = {"op": "rotate", "oid": oid, "degrees": value}
            return self.submit(lambda: self.run_edit(req))

    def commit_font_size(self, value):
        oid, obj = self.selection, self.obj_info or {}
        if oid is not None and value >= 4 and obj.get("kind") in ("text", "tex"):
            def work():
                yield from self.preview_sprite(oid, obj["kind"], obj.get("value", ""), value,
                                              obj.get("color"), obj.get("font"), obj.get("weight"), obj.get("slant"))
                return (yield from self.run_edit({"op": "font", "oid": oid, "value": value}))
            return self.submit(work)
    def commit_style(self, font=None, weight=None, slant=None):
        oid, obj = self.selection, self.obj_info or {}
        if oid is None or obj.get("kind") != "text": return
        req = {"op": "style", "oid": oid, "shared": True}
        for key, value in (("font", font), ("weight", weight), ("slant", slant)):
            if value is not None: req[key] = value
        def work():
            yield from self.preview_sprite(oid, obj["kind"], obj.get("value", ""), obj.get("fontSize") or 30,
                                          obj.get("color"), font or obj.get("font"),
                                          weight or obj.get("weight"), slant or obj.get("slant"))
            return (yield from self.run_edit(req))
        return self.submit(work)
    def family_changed(self, family):
        return self.commit_style(font=family)

    def bold_toggled(self, button):
        on = self._inline_style("bold")
        if on is None:
            return self.commit_style(weight="BOLD" if button.isChecked() else "NORMAL")
        button.setChecked(on)

    def italic_toggled(self, button):
        on = self._inline_style("italic")
        if on is None:
            return self.commit_style(slant="ITALIC" if button.isChecked() else "NORMAL")
        button.setChecked(on)

    @staticmethod
    def table_rows(value):
        return table_rows(value)
    def table_editor(self, layout, obj):
        if self.table_grid is None:
            self.table_grid = TableGrid(self.apply_table)
        grid = self.table_grid
        rows = self.table_rows(obj.get("value"))
        if grid.applying is None and rows != grid.source_rows:
            grid.update_source(rows)
        controls, row = inspector_row(4)
        for text, tip, callback in (
            ("+ Row", "Add a row at the bottom", self.table_add_row),
            ("− Row", "Remove the bottom row", self.table_remove_row),
            ("+ Col", "Add a column on the right", self.table_add_col),
            ("− Col", "Remove the right column", self.table_remove_col),
        ):
            button = QPushButton(text)
            button.setFont(Theme.font(11))
            button.setToolTip(tip)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        apply = QPushButton("Apply Table")
        apply.setFont(Theme.font(13))
        apply.clicked.connect(self.apply_table)
        layout.addWidget(field_label("Cells"))
        layout.addWidget(grid)
        layout.addWidget(controls)
        layout.addWidget(apply, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(InspectorLabel("⇥ next cell  ·  ↵ apply", Theme.font(10), ink=Theme.tertiary_ink))

    def focus_first_table_cell(self):
        if self.table_grid is not None:
            self.table_grid.focus_cell(0, 0)

    def table_draft(self):
        return self.table_grid.draft()

    def reshape_table(self, change):
        rows = self.table_draft(); change(rows)
        self.table_grid.set_rows(rows)
    def table_add_row(self): self.reshape_table(lambda rows: rows.append([""] * len(rows[0])))
    def table_remove_row(self): self.reshape_table(lambda rows: rows.pop() if len(rows) > 1 else None)
    def table_add_col(self): self.reshape_table(lambda rows: [row.append("") for row in rows])
    def table_remove_col(self): self.reshape_table(lambda rows: [row.pop() for row in rows if len(row) > 1])
    def apply_table(self):
        oid, grid = self.selection, self.table_grid
        if oid is None or grid is None:
            return
        rows = grid.draft()
        grid.applying = rows

        def work():
            result = None
            try:
                result = yield from self.run_edit({"op": "table", "oid": oid, "rows": rows})
                return result
            finally:
                if grid is self.table_grid and grid.applying is rows:
                    grid.applying = None
                    if result and result.get("ok"):
                        # Apply backend normalization without throwing away edits typed during the render.
                        source = self.table_rows((self.obj_info or {}).get("value"))
                        grid.update_source(source, replace=grid.draft() == rows)

        return self.submit(work)

"""Editable object inspector, table grid, and source-safe geometry controls."""
from __future__ import annotations

import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFontComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)
from .formatbar import TextTransform, payload
from .models import CHAR_KINDS, char_styles, stop_box


def numeric(value):
    if hasattr(value, "value"): return value.value()
    if hasattr(value, "text"): return float(value.text())
    return float(value)


def table_rows(value):
    try:
        rows = json.loads(value) if isinstance(value, str) else value
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            return []
        return [[str(int(c)) if isinstance(c, float) and c.is_integer() else str(c) for c in row] for row in rows]
    except (TypeError, ValueError):
        return []


class InspectorTextEdit(QTextEdit):
    """Qt's UTF-16 cursor ranges are converted to engine code-point spans at commit."""
    def __init__(self, text, committed, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setPlainText(text)
        self.original = text
        self.on_commit = committed
        self.text_script_mode = None
        self.touched_styles = set()
        self.body_bullets = False
        self.setMinimumHeight(100)
        self.setMaximumHeight(240)

    def set_text(self, text):
        if text == self.toPlainText() or self.hasFocus() or self.touched_styles:
            return
        self.setPlainText(text)
        self.original = text

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key.Key_Escape:
            self.setPlainText(self.original); self.clearFocus(); return
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
            selected = cursor.hasSelection()
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
        if event.reason() not in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason): self.commit()

    def commit(self):
        text = self.toPlainText()
        styles = self.committed_styles
        if text != self.original or styles:
            self.on_commit(text, styles)
            self.original = text
            self.touched_styles.clear()

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
        for kind, runs in styles.items():
            for run in runs:
                if isinstance(run, dict):
                    a, b, color = run.get("start", 0), run.get("end", 0), run.get("color")
                else:
                    a, b = run[:2]; color = run[2] if len(run) > 2 else None
                text = self.toPlainText()
                cursor = self.textCursor()
                cursor.setPosition(len(text[:a].encode("utf-16-le")) // 2)
                cursor.setPosition(len(text[:b].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
                self.setTextCursor(cursor); self.set_inline_style(kind, True, color)
        cursor = self.textCursor(); cursor.setPosition(0); self.setTextCursor(cursor)
        self.touched_styles.clear()

    @property
    def committed_styles(self):
        result = {kind: [] for kind in self.touched_styles}
        text = self.toPlainText()
        offset = 0
        for index, char in enumerate(text):
            cursor = QTextCursor(self.document()); cursor.setPosition(offset)
            units = len(char.encode("utf-16-le")) // 2
            cursor.setPosition(offset + units, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            values = {"bold": fmt.fontWeight() >= QFont.Weight.Bold, "italic": fmt.fontItalic(), "underline": fmt.fontUnderline(), "strike": fmt.fontStrikeOut(), "color": fmt.foreground().color().name().upper() if fmt.hasProperty(QTextCharFormat.Property.ForegroundBrush) else None, "highlight": fmt.background().color().name().upper() if fmt.background().color().alpha() and fmt.hasProperty(QTextCharFormat.Property.BackgroundBrush) else None}
            for kind in self.touched_styles:
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
        layout = self.inspector.layout()
        if layout is None: layout = QVBoxLayout(self.inspector)
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()
        self.text_field = None
        self.size_field = None
        self.table_grid = None
        o = self.obj_info
        if not o:
            layout.addWidget(QLabel("Slide\n" + (self.scene_name or "")))
            tips = QLabel("Click an element to edit it.\nDouble-click text or a formula to retype.\nDrag to move; handles resize.\nArrow keys nudge; Shift for bigger steps.")
            tips.setWordWrap(True); layout.addWidget(tips); layout.addStretch(); self.sync_format_bar(); return
        layout.addWidget(QLabel(f"{o.get('kind', 'Object').capitalize()}\n{o.get('loc', '')}"))
        kind = o.get("kind")
        if kind == "text":
            if o.get("textOk"):
                oid, target, seen = self.selection, self.scene_name, self.edit_context.seen
                self.text_field = InspectorTextEdit(o.get("value", ""), lambda text, styles: self.commit_text(oid, text, shared=self.shared_choice, styles=styles, slide=target, seen=seen))
                styles = char_styles(o)
                self.text_field.load_styles(styles)
                layout.addWidget(self.text_field)
                layout.addWidget(QLabel("Return: newline · Ctrl+Return: apply · Escape: revert"))
                self.shared_row(o)
            else:
                note = QLabel(f"Text is generated by code ({o.get('textNote', 'computed')}); edit it in the source.")
                note.setWordWrap(True); layout.addWidget(note)
            self.style_fields(o)
        elif kind == "tex":
            button = QPushButton("Edit LaTeX…"); button.clicked.connect(self.edit_latex_pressed); layout.addWidget(button)
            formula = QLabel(o.get("value", "")); formula.setWordWrap(True); formula.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); layout.addWidget(formula)
            if not o.get("textOk"): layout.addWidget(QLabel("Formula is generated by code: " + o.get("textNote", "computed")))
            self.style_fields(o)
        elif kind == "table":
            for widget in self.table_editor(o): layout.addWidget(widget)
        elif kind == "image":
            filename = QLabel(o.get("image", "")); filename.setWordWrap(True); layout.addWidget(filename)
        else:
            color = QPushButton("Colour…"); color.clicked.connect(self.color_committed); layout.addWidget(color)
        box = stop_box(getattr(self.canvas, "stop", None), self.selection)
        if box: self.geometry_rows(box, o)
        if o.get("siblings", 0) > 1:
            info = QLabel(f"Created in a loop/helper: {o['siblings']} objects share this source line."); info.setWordWrap(True); layout.addWidget(info)
        layout.addStretch(); self.sync_format_bar()

    def shared_row(self, obj):
        shared = obj.get("shared")
        if not shared: return
        box = QCheckBox(f"Change everywhere ({shared.get('name')} in {shared.get('rel')}, used {shared.get('uses')}×)")
        box.setChecked(self.shared_choice); box.toggled.connect(self.shared_toggled)
        self.inspector.layout().addWidget(box)
    def shared_toggled(self, on): self.shared_choice = on.isChecked() if hasattr(on, "isChecked") else bool(on)

    def style_fields(self, obj, width=None):
        widget = QWidget(); form = QFormLayout(widget)
        if obj.get("kind") == "text":
            family = QFontComboBox(); family.setCurrentFont(QFont(obj.get("font", "")))
            family.currentFontChanged.connect(lambda font: self.family_changed(font.family())); form.addRow("Font", family)
            styles = QWidget(); row = QHBoxLayout(styles)
            for title, state, callback in [("B", obj.get("weight", "").upper() == "BOLD", self.bold_toggled), ("I", obj.get("slant", "").upper() in ("ITALIC", "OBLIQUE"), self.italic_toggled)]:
                button = QPushButton(title); button.setCheckable(True); button.setChecked(state); button.clicked.connect(lambda _, b=button, c=callback: c(b)); row.addWidget(button)
            form.addRow("Style", styles)
        size = QDoubleSpinBox(); size.setRange(4, 300); size.setDecimals(1); size.setValue(obj.get("fontSize", 30)); size.editingFinished.connect(lambda: self.size_committed(size)); self.size_field = size
        form.addRow("Size", size)
        color = QPushButton(obj.get("color", "#FFFFFF")); color.clicked.connect(self.color_committed); form.addRow("Colour", color)
        if obj.get("kind") == "text":
            fmt = obj.get("format") or {}
            align = QComboBox()
            align.addItems(["left", "center", "right", "justify"])
            align.setCurrentText(fmt.get("align") or "left")
            align.activated.connect(lambda _: self.align_justify() if align.currentText() == "justify" else self.commit_align(align.currentText()))
            form.addRow("Alignment", align)
            spacing = QDoubleSpinBox()
            spacing.setRange(.01, 100)
            spacing.setSingleStep(.1)
            line_spacing = fmt.get("lineSpacing")
            spacing.setValue((.3 if line_spacing is None else line_spacing) + .7)
            spacing.editingFinished.connect(lambda: self.spacing_chosen(spacing.value()))
            form.addRow("Line spacing", spacing)
            columns = QComboBox()
            columns.addItems(["1", "2", "3", "4"])
            columns.setCurrentIndex((fmt.get("columns") or 1) - 1)
            columns.activated.connect(lambda _: self.commit_columns(columns.currentIndex() + 1))
            form.addRow("Columns", columns)
        self.inspector.layout().addWidget(widget)

    def geometry_rows(self, box, obj):
        widget = QWidget(); form = QFormLayout(widget)
        dims = obj.get("dims") or [box.w, box.h]
        for name, value, callback in [("X (px)", box.cx, self.x_committed), ("Y (px)", box.cy, self.y_committed)] + ([] if obj.get("kind") == "drawing" else [("Width", dims[0], self.w_committed), ("Height", dims[1], self.h_committed)]):
            field = QDoubleSpinBox(); field.setRange(-1000000, 1000000); field.setDecimals(1); field.setValue(value); field.editingFinished.connect(lambda f=field, cb=callback: cb(f)); form.addRow(name, field)
        if obj.get("kind") not in ("line", "drawing"):
            rotation = QDoubleSpinBox(); rotation.setRange(-36000, 36000); rotation.setDecimals(1); rotation.setValue(obj.get("rotation", 0)); rotation.editingFinished.connect(lambda: self.rotation_committed(rotation)); form.addRow("Rotation (°)", rotation)
            if obj.get("kind") in ("image", "shape"):
                aspect = QCheckBox("Keep aspect ratio"); aspect.setChecked(self.aspect_locked); aspect.toggled.connect(self.aspect_toggled); form.addRow(aspect)
        self.inspector.layout().addWidget(widget)
    def aspect_toggled(self, on): self.aspect_locked = on.isChecked() if hasattr(on, "isChecked") else bool(on)
    def current_dims(self, oid):
        dims = (self.obj_info or {}).get("dims")
        if dims and len(dims) == 2 and min(dims) > 0: return dims
        box = stop_box(getattr(self.canvas, "stop", None), oid)
        if box and box.w > 0 and box.h > 0: return box.w, box.h
    def x_committed(self, field):
        box = stop_box(getattr(self.canvas, "stop", None), self.selection)
        if box: return self.commit_move(self.selection, numeric(field) - box.cx, 0)
    def y_committed(self, field):
        box = stop_box(getattr(self.canvas, "stop", None), self.selection)
        if box: return self.commit_move(self.selection, 0, numeric(field) - box.cy)
    def w_committed(self, field): return self.commit_dimension(width=numeric(field))
    def h_committed(self, field): return self.commit_dimension(height=numeric(field))
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
    def rotation_committed(self, field):
        oid = self.selection
        if oid is not None:
            req = {"op": "rotate", "oid": oid, "degrees": numeric(field)}
            return self.submit(lambda: self.run_edit(req))
    def size_committed(self, field): return self.commit_font_size(numeric(field))
    def size_stepped(self, field): return self.commit_font_size(numeric(field))
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
        return self.commit_style(font=family.currentText() if hasattr(family, "currentText") else family)
    def bold_toggled(self, button):
        if self._inline_style("bold") is None: return self.commit_style(weight="BOLD" if button.isChecked() else "NORMAL")
    def italic_toggled(self, button):
        if self._inline_style("italic") is None: return self.commit_style(slant="ITALIC" if button.isChecked() else "NORMAL")
    def color_committed(self, color=None):
        if isinstance(color, (str, QColor)): return self.commit_color(QColor(color).name().upper())
        return self.open_color_panel()

    @staticmethod
    def table_rows(value):
        return table_rows(value)
    def table_editor(self, obj, width=None):
        self.table_grid = QTableWidget(); self.table_grid.setMaximumHeight(240)
        self.layout_table_grid(self.table_rows(obj.get("value", "[]")))
        self.table_grid.cellActivated.connect(lambda *_: self.apply_table())
        controls = QWidget(); row = QHBoxLayout(controls)
        for text, callback in [("+ Row", self.table_add_row), ("− Row", self.table_remove_row), ("+ Col", self.table_add_col), ("− Col", self.table_remove_col), ("Apply", self.apply_table)]:
            button = QPushButton(text); button.clicked.connect(callback); row.addWidget(button)
        return [self.table_grid, controls]
    def table_draft(self):
        grid = self.table_grid
        return [[grid.item(r, c).text() if grid.item(r, c) else "" for c in range(grid.columnCount())] for r in range(grid.rowCount())]
    def layout_table_grid(self, rows):
        rows = rows or [[""]]; cols = max(1, max(map(len, rows)))
        self.table_grid.setRowCount(len(rows)); self.table_grid.setColumnCount(cols)
        for r, row in enumerate(rows):
            for c in range(cols): self.table_grid.setItem(r, c, QTableWidgetItem(row[c] if c < len(row) else ""))
    def reshape_table(self, change):
        rows = self.table_draft(); change(rows)
        if rows and rows[0]: self.layout_table_grid(rows)
    def table_add_row(self): self.reshape_table(lambda rows: rows.append([""] * len(rows[0])))
    def table_remove_row(self): self.reshape_table(lambda rows: rows.pop() if len(rows) > 1 else None)
    def table_add_col(self): self.reshape_table(lambda rows: [row.append("") for row in rows])
    def table_remove_col(self): self.reshape_table(lambda rows: [row.pop() for row in rows if len(row) > 1])
    def apply_table(self):
        oid = self.selection
        if oid is not None and self.table_grid:
            rows = self.table_draft()
            return self.submit(lambda: self.run_edit({"op": "table", "oid": oid, "rows": rows}))

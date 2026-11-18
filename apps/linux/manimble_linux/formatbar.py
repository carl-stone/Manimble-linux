"""Home bar and lossless plain-text/list and Unicode transformations."""
from __future__ import annotations

import re


class TextTransform:
    marker = re.compile(r"^(\s*)(?:[•\-–▪◦]|\d+[.)])\s+")
    sup = dict(zip("0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVW", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
    sub = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))

    @staticmethod
    def lines(text): return text.split("\n")
    @classmethod
    def strip(cls, line):
        match = cls.marker.match(line)
        if match: return match[1], line[match.end():]
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        return indent, line[len(indent):]
    @classmethod
    def has_marker(cls, line): return cls.marker.match(line) is not None
    @classmethod
    def is_bulleted(cls, text):
        lines = [line for line in cls.lines(text) if line.strip()]
        return bool(lines) and all(cls.has_marker(line) and not line.lstrip()[0].isdigit() for line in lines)
    @classmethod
    def is_numbered(cls, text):
        lines = [line for line in cls.lines(text) if line.strip()]
        return bool(lines) and all(cls.has_marker(line) and line.lstrip()[0].isdigit() for line in lines)
    @classmethod
    def toggle_bullets(cls, text):
        on = cls.is_bulleted(text)
        return "\n".join(line if not line.strip() else ind + ("" if on else "• ") + rest for line in cls.lines(text) for ind, rest in [cls.strip(line)])
    @classmethod
    def toggle_numbers(cls, text):
        on, number, result = cls.is_numbered(text), 0, []
        for line in cls.lines(text):
            if line.strip():
                number += 1
                ind, rest = cls.strip(line)
                line = ind + ("" if on else f"{number}. ") + rest
            result.append(line)
        return "\n".join(result)
    @classmethod
    def indent(cls, text, lines=None, by=4):
        return "\n".join(line if lines is not None and not lines[0] <= i <= lines[1] else "    " + line if by > 0 else line[min(4, len(line) - len(line.lstrip(' '))):] for i, line in enumerate(cls.lines(text)))
    @staticmethod
    def codepoint_offset(text, utf16):
        return len(text.encode("utf-16-le")[:max(0, utf16) * 2].decode("utf-16-le", errors="ignore"))
    @classmethod
    def line_range(cls, text, range_):
        start, length = range_
        a = text[:cls.codepoint_offset(text, start)].count("\n")
        b = text[:cls.codepoint_offset(text, max(start, start + length - (1 if length else 0)))].count("\n")
        return a, max(a, b)
    @classmethod
    def raise_text(cls, text, superscript):
        mapping = cls.sup if superscript else cls.sub
        return "".join(mapping.get(c, c) for c in text)
    @classmethod
    def script(cls, text, superscript):
        mapping = cls.sup if superscript else cls.sub
        back = {v: k for k, v in mapping.items()}
        convertible = [c for c in text if c in mapping or c in back]
        if not convertible: return None
        use = back if all(c in back for c in convertible) else mapping
        return "".join(use.get(c, c) for c in text)
    @classmethod
    def newline_prefix(cls, line, body=False):
        """Continue lists; empty markers terminate them. Body placeholders begin as bullets."""
        match = cls.marker.match(line)
        if match:
            indent, rest = cls.strip(line)
            if not rest.strip(): return ""
            number = re.match(r"\s*(\d+)([.)])", line)
            return indent + (f"{int(number[1]) + 1}{number[2]} " if number else "• ")
        return "• " if body else ""
    @staticmethod
    def clean_body(text):
        return "\n".join(line for line in text.split("\n") if line.strip("• \t"))


from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QPushButton, QScrollArea, QToolButton, QWidget)
from . import prefs
from .models import frame_size


def payload(value):
    if hasattr(value, "currentData"): return value.currentData()
    if hasattr(value, "data"): return value.data()
    return value


class FontMenuStyler:
    @staticmethod
    def apply(combo):
        for i in range(combo.count()):
            family = combo.itemText(i)
            combo.setItemData(i, QFont(family), Qt.ItemDataRole.FontRole)


class ColorGridView(QWidget):
    def __init__(self, current=None, chosen=None, parent=None):
        super().__init__(parent)
        self.on_choose = chosen
        layout = QGridLayout(self)
        colors = [[QColor.fromRgbF(v, v, v).name() for v in (0, .12, .25, .4, .55, .7, .82, .9, .96, 1)]]
        for hue in (0, .06, .12, .2, .33, .48, .58, .67, .76, .88):
            colors.append([QColor.fromHsvF(hue, s, v).name() for s, v in ((.15, 1), (.3, 1), (.5, 1), (.7, 1), (1, 1), (1, .85), (1, .7), (1, .55), (1, .4), (1, .25))])
        for row, values in enumerate(colors):
            for col, color in enumerate(values):
                button = QPushButton()
                button.setFixedSize(20, 20)
                button.setToolTip(color.upper())
                button.setStyleSheet(f"background:{color};border:{'2px solid #4D7CFF' if color.upper() == str(current).upper() else '1px solid #888'}")
                button.clicked.connect(lambda _, c=color: self.on_choose(c.upper()) if self.on_choose else None)
                layout.addWidget(button, row, col)


class FormatBar(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.row = QHBoxLayout(content)
        self.row.setContentsMargins(3, 2, 3, 2)
        self.setWidget(content)
        self.font_popup = QFontComboBox()
        self.font_popup.setMaximumWidth(180)
        self.row.addWidget(self.font_popup)
        self.size_field = QDoubleSpinBox()
        self.size_field.setRange(4, 300); self.size_field.setDecimals(1); self.size_field.setValue(30)
        self.row.addWidget(self.size_field)
        for name, title in [("bigger", "A↑"), ("smaller", "A↓"), ("bold", "B"), ("italic", "I"), ("underline", "U"), ("strike", "S̶"), ("superscript", "x²"), ("subscript_button", "x₂"), ("highlight", "Highlight"), ("color_button", "Colour"), ("clear_format", "Tx"), ("bullets", "•"), ("numbers", "1."), ("outdent", "⇤"), ("indent", "⇥"), ("align_left", "L"), ("align_center", "C"), ("align_right", "R"), ("align_justify", "J"), ("grid", "Grid"), ("snap", "Snap"), ("guide_v", "│"), ("guide_h", "─"), ("feedback", "Feedback"), ("script", "Script")]:
            button = QToolButton()
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setText(title); button.setToolTip(name.replace('_', ' ').capitalize())
            button.setCheckable(name in ("bold", "italic", "underline", "strike", "superscript", "subscript_button", "bullets", "numbers", "grid", "snap", "align_left", "align_center", "align_right", "align_justify"))
            setattr(self, name, button); self.row.addWidget(button)
        self.spacing = QComboBox()
        self.spacing.addItem("Spacing", None)
        for n in (1., 1.15, 1.5, 2., 2.5, 3.): self.spacing.addItem(f"{n:g}", n)
        self.spacing.addItem("Other…", "other")
        self.columns = QComboBox(); self.columns.addItem("Columns", None)
        for n in range(1, 5): self.columns.addItem(str(n), n)
        self.row.addWidget(self.spacing); self.row.addWidget(self.columns)
        self.row.addStretch()
        self.setMaximumHeight(66)

    def set_color(self, color):
        color = QColor(color)
        self.color_button.setStyleSheet(f"border-bottom: 4px solid {color.name()}")


class FormatBarMixin:
    highlight_swatches = [("Yellow", "#FFD400"), ("Green", "#7CFC00"), ("Cyan", "#40E0FF"), ("Pink", "#FF6EC7"), ("Blue", "#4D7CFF"), ("Red", "#FF4040"), ("Orange", "#FF9A2E"), ("Gray", "#8A8A8A"), ("No Highlight", None)]

    def make_format_bar(self):
        bar = FormatBar()
        self.format_bar = bar
        bar.font_popup.currentFontChanged.connect(lambda font: self.family_changed(font.family()))
        bar.size_field.editingFinished.connect(lambda: self.size_committed(bar.size_field))
        bindings = {"bigger": "format_bigger", "smaller": "format_smaller", "bold": "format_bold", "italic": "format_italic", "underline": "format_underline", "strike": "format_strikethrough", "superscript": "format_superscript", "subscript_button": "format_subscript", "highlight": "highlight_pressed", "color_button": "color_button_pressed", "clear_format": "clear_formatting", "bullets": "toggle_bullets", "numbers": "toggle_numbering", "outdent": "decrease_indent", "indent": "increase_indent", "align_left": "align_left", "align_center": "align_center", "align_right": "align_right", "align_justify": "align_justify", "grid": "toggle_grid", "snap": "toggle_snap", "guide_v": "add_vertical_guide", "guide_h": "add_horizontal_guide", "feedback": "view_feedback", "script": "view_script"}
        for name, method in bindings.items():
            getattr(bar, name).clicked.connect(lambda _, method=method: getattr(self, method)())
        bar.spacing.activated.connect(lambda _: self.spacing_chosen(bar.spacing))
        bar.columns.activated.connect(lambda _: self.columns_chosen(bar.columns))
        return bar

    def sync_format_bar(self):
        bar = self.format_bar
        o = self.obj_info or {}; fmt = o.get("format") or {}; text = o.get("kind") == "text"; texty = o.get("kind") in ("text", "tex")
        for field in (bar.font_popup, bar.size_field): field.blockSignals(True)
        bar.font_popup.setEnabled(text)
        if text: bar.font_popup.setCurrentFont(QFont(o.get("font") or ""))
        bar.size_field.setEnabled(texty); bar.size_field.setValue(o.get("fontSize") or 30)
        for field in (bar.font_popup, bar.size_field): field.blockSignals(False)
        for name in ("bigger", "smaller", "underline", "strike", "highlight", "clear_format"): getattr(bar, name).setEnabled(texty)
        for name in ("bold", "italic", "align_left", "align_center", "align_right", "align_justify", "spacing", "columns"): getattr(bar, name).setEnabled(text)
        for name in ("superscript", "subscript_button", "bullets", "numbers", "outdent", "indent"): getattr(bar, name).setEnabled(text and o.get("textOk", False))
        bar.color_button.setEnabled(bool(o) and o.get("kind") != "image")
        bar.set_color(o.get("color") or "#FFFFFF")
        checks = {"bold": (o.get("weight") or "").upper() == "BOLD", "italic": (o.get("slant") or "").upper() in ("ITALIC", "OBLIQUE"),
                  "underline": bool(fmt.get("underline")), "strike": bool(fmt.get("strike")),
                  "bullets": TextTransform.is_bulleted(o.get("value") or ""), "numbers": TextTransform.is_numbered(o.get("value") or ""),
                  "grid": getattr(self.canvas, "show_grid", False), "snap": getattr(self.canvas, "snap_enabled", True)}
        for name, on in checks.items():
            getattr(bar, name).setChecked(on)
        for key in ("left", "center", "right", "justify"): getattr(bar, "align_" + key).setChecked(text and fmt.get("align", "left") == key)

    def _inline_style(self, kind, on=None, hex=None):
        field = self.text_field
        if field is not None and field.hasFocus() and hasattr(field, "set_inline_style"):
            return field.toggle_inline_style(kind) if on is None else field.set_inline_style(kind, on, hex)
        method = getattr(self.canvas, "toggle_inline_style" if on is None else "set_inline_style", None)
        if method is None:
            return None
        if on is None:
            return method(kind)
        return True if method(kind, on=on, hex=hex) else None

    def commit_color(self, hex):
        if self._inline_style("color", True, hex) is not None: return
        oid, obj = self.selection, self.obj_info or {}
        if oid is not None:
            def work():
                if obj.get("kind") in ("text", "tex"):
                    yield from self.preview_sprite(oid, obj["kind"], obj.get("value", ""), obj.get("fontSize") or 30,
                                                  hex, obj.get("font"), obj.get("weight"), obj.get("slant"))
                return (yield from self.run_edit({"op": "color", "oid": oid, "value": hex}))
            return self.submit(work)
    def color_button_pressed(self, *_):
        popup = QFrame(self.window, Qt.WindowType.Popup)
        layout = QGridLayout(popup)
        def choose(hex): popup.close(); self.commit_color(hex)
        layout.addWidget(ColorGridView((self.obj_info or {}).get("color"), choose))
        more = QPushButton("More Colours…"); more.clicked.connect(lambda: (popup.close(), self.open_color_panel())); layout.addWidget(more)
        self._color_popup = popup; popup.move(self.window.mapToGlobal(self.window.rect().center())); popup.show()
    def open_color_panel(self):
        color = QColorDialog.getColor(QColor((self.obj_info or {}).get("color", "#FFFFFF")), self.window)
        if color.isValid(): self.commit_color(color.name().upper())
    def color_panel_changed(self, color): self.commit_color(QColor(color).name().upper())

    def commit_decoration(self, kind, on):
        oid = self.selection
        if oid is not None: return self.submit(lambda: self.run_edit({"op": "decorate", "oid": oid, "kind": kind, "on": bool(on)}))
    def _decoration(self, kind, on=None):
        result = self._inline_style(kind)
        if result is not None: return self.sync_format_bar()
        return self.commit_decoration(kind, not (self.obj_info or {}).get("format", {}).get(kind) if on is None else on)
    def underline_toggled(self, button): return self._decoration("underline", button.isChecked())
    def strike_toggled(self, button): return self._decoration("strike", button.isChecked())
    def format_underline(self): return self._decoration("underline")
    def format_strikethrough(self): return self._decoration("strike")
    def highlight_chosen(self, choice):
        color = payload(choice)
        if self._inline_style("highlight", color is not None, color) is not None: return
        oid = self.selection
        if oid is not None:
            req = {"op": "highlight", "oid": oid, "opacity": .6}
            if color: req["color"] = color
            return self.submit(lambda: self.run_edit(req))
    def highlight_pressed(self, *_):
        names = [title for title, _ in self.highlight_swatches]
        title, ok = QInputDialog.getItem(self.window, "Text highlight", "Colour", names, 0, False)
        if ok: return self.highlight_chosen(dict(self.highlight_swatches)[title])
    def clear_formatting(self):
        oid = self.selection
        if oid is not None: return self.submit(lambda: self.run_edit({"op": "clear_format", "oid": oid}))
    def format_bold(self):
        if self._inline_style("bold") is not None: return
        return self.commit_style(weight="NORMAL" if (self.obj_info or {}).get("weight", "").upper() == "BOLD" else "BOLD")
    def format_italic(self):
        if self._inline_style("italic") is not None: return
        return self.commit_style(slant="NORMAL" if (self.obj_info or {}).get("slant", "").upper() in ("ITALIC", "OBLIQUE") else "ITALIC")
    def format_bigger(self): return self.scale_font(1.1)
    def format_smaller(self): return self.scale_font(1 / 1.1)
    def scale_font(self, scale): return self.commit_font_size(max(4, round((self.obj_info or {}).get("fontSize", 30) * scale)))

    def apply_script(self, superscript):
        obj = self.obj_info or {}
        if obj.get("kind") != "text" or not obj.get("textOk"):
            self.toast("Select an editable text object first", error=True); return
        for editor in (self.canvas, self.text_field):
            if editor is None: continue
            active = getattr(editor, "editing_text", None) == self.selection or (hasattr(editor, "hasFocus") and editor.hasFocus())
            if active:
                if editor.script_selection(superscript): return
                editor.text_script_mode = None if getattr(editor, "text_script_mode", None) == superscript else superscript
                self.toast("Script typing mode changed; press again to stop")
                return
        converted = TextTransform.script(obj.get("value", ""), superscript)
        if converted is None: self.toast("No script form exists for these characters", error=True)
        else: self.commit_text(self.selection, converted, shared=self.shared_choice)
    def format_superscript(self): return self.apply_script(True)
    def format_subscript(self): return self.apply_script(False)
    def transform_text(self, transform):
        obj = self.obj_info or {}
        if obj.get("kind") != "text" or not obj.get("textOk"):
            self.toast("Select an editable text object first", error=True); return
        editor = self.text_field
        text = editor.toPlainText() if editor else obj.get("value", "")
        cursor = editor.textCursor() if editor else None
        range_ = (cursor.selectionStart(), cursor.selectionEnd() - cursor.selectionStart()) if cursor and cursor.hasSelection() else None
        new = transform(text, range_)
        if new != text:
            if editor: editor.setPlainText(new)
            return self.commit_text(self.selection, new, shared=self.shared_choice)
    def toggle_bullets(self): return self.transform_text(lambda s, r: TextTransform.toggle_bullets(s))
    def toggle_numbering(self): return self.transform_text(lambda s, r: TextTransform.toggle_numbers(s))
    def increase_indent(self): return self.transform_text(lambda s, r: TextTransform.indent(s, TextTransform.line_range(s, r) if r else None, 4))
    def decrease_indent(self): return self.transform_text(lambda s, r: TextTransform.indent(s, TextTransform.line_range(s, r) if r else None, -4))
    def commit_align(self, value):
        obj = self.obj_info or {}
        if obj.get("kind") != "text": return
        if "\n" not in obj.get("value", "") and obj.get("frame") is None:
            self.toast("Alignment requires several lines or a layout text box", error=True); return
        oid = self.selection
        return self.submit(lambda: self.run_edit({"op": "align", "oid": oid, "value": value}))
    def align_left(self): return self.commit_align("left")
    def align_center(self): return self.commit_align("center")
    def align_right(self): return self.commit_align("right")
    def align_justify(self):
        self.sync_format_bar(); self.toast("Manim cannot justify text with manual line breaks; use left, centre or right", error=True)
    def spacing_chosen(self, value):
        value = payload(value)
        if value == "other":
            current = ((self.obj_info or {}).get("format") or {}).get("lineSpacing")
            value, ok = QInputDialog.getDouble(self.window, "Line spacing", "Multiple of single spacing", (.3 if current is None else current) + .7, .01, 100, 2)
            if not ok: return
        if value is not None and self.selection is not None:
            oid = self.selection
            return self.submit(lambda: self.run_edit({"op": "spacing", "oid": oid, "value": max(-.6, float(value) - .7)}))
    def spacing_menu_chosen(self, value): return self.spacing_chosen(value)
    def columns_chosen(self, value):
        value = payload(value)
        if value is not None: return self.commit_columns(int(value))
    def columns_menu_chosen(self, value): return self.columns_chosen(value)
    def commit_columns(self, n):
        obj = self.obj_info or {}
        if obj.get("kind") != "text" or n not in range(1, 5): return
        if n > 1 and "\n" not in obj.get("value", "") and obj.get("format", {}).get("columns", 1) == 1:
            self.toast("Columns lay out text lines; add lines first", error=True); return
        oid = self.selection
        return self.submit(lambda: self.run_edit({"op": "columns", "oid": oid, "value": n}))
    def toggle_grid(self):
        self.canvas.show_grid = not getattr(self.canvas, "show_grid", False)
        prefs.set("showGrid", self.canvas.show_grid); self.canvas.update(); self.sync_format_bar()
    def toggle_snap(self):
        self.canvas.snap_enabled = not getattr(self.canvas, "snap_enabled", True)
        prefs.set("snapObjects", self.canvas.snap_enabled); self.sync_format_bar()
    @property
    def guides_key(self): return "guides:" + str(self.original_deck) if self.original_deck else None
    def add_guide(self, vertical):
        if not self.scene_name: return
        width, height = frame_size(self.scene)
        self.canvas.user_guides = list(getattr(self.canvas, "user_guides", [])) + [(vertical, width / 2 if vertical else height / 2)]
        self.save_guides(); self.canvas.update()
        self.toast("Guide added at the centre: drag to move, drag off the slide to remove")
    def add_vertical_guide(self): self.add_guide(True)
    def add_horizontal_guide(self): self.add_guide(False)
    def clear_guides(self): self.canvas.user_guides = []; self.save_guides(); self.canvas.update()
    def save_guides(self):
        if self.guides_key: prefs.set(self.guides_key, [[int(v), p] for v, p in self.canvas.user_guides])
    def load_guides(self):
        self.canvas.user_guides = [(v > .5, p) for v, p in prefs.get_json(self.guides_key, [])] if self.guides_key else []
    def set_draw_tool(self, kind):
        if self.scene_name:
            self.canvas.draw_tool = kind; self.canvas.setFocus()
            self.toast("Draw on the slide · Escape cancels" if kind else "Drawing off")
    def draw_tool_menu(self, kind): return self.set_draw_tool(payload(kind))
    def draw_finished(self, kind, sx, sy, ex, ey, anchors=None):
        return self.insert(kind, dict(anchors or {}, sx=sx, sy=sy, ex=ex, ey=ey))

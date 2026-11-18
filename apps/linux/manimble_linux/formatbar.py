"""Home bar and lossless plain-text/list and Unicode transformations."""
from __future__ import annotations

import re
from math import floor, isfinite

from PySide6.QtCore import QEvent, QPoint, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QDoubleValidator, QFont, QFontDatabase, QPainter, QPalette,
                           QPen, QRawFont, QTextCursor)
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from . import prefs
from .icon_art import TEXT_COLOR_ICON_SIZE, text_color_icon
from .icons import painted_icon, template_icon
from .models import frame_size
from .style import Theme, color_from_hex, hex_color, watch_appearance


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


def payload(value):
    if hasattr(value, "currentData"): return value.currentData()
    if hasattr(value, "data"): return value.data()
    return value


# Widget name, canonical command, symbol, accessible name, Swift help.
_HOME_COMMANDS = {
    "bigger": ("format_bigger", "textformat.size.larger", "Increase font size", "Increase font size ({key})"),
    "smaller": ("format_smaller", "textformat.size.smaller", "Decrease font size", "Decrease font size ({key})"),
    "bold": ("format_bold", "bold", "Bold", "Bold ({key})"),
    "italic": ("format_italic", "italic", "Italic", "Italic ({key})"),
    "underline": ("format_underline", "underline", "Underline", "Underline ({key}): a line attached to the text"),
    "strike": ("format_strikethrough", "strikethrough", "Strikethrough", "Strikethrough: a line attached to the text"),
    "superscript": ("format_superscript", "textformat.superscript", "Superscript",
                    "Superscript ({key}): the selected characters, or what you type next"),
    "subscript_button": ("format_subscript", "textformat.subscript", "Subscript",
                         "Subscript ({key}): the selected characters, or what you type next"),
    "highlight": (None, "highlighter", "Text highlight colour",
                  "Text highlight colour (a background rectangle behind the text)"),
    "color_button": (None, None, "Text colour", "Text colour"),
    "clear_format": ("clear_formatting", "eraser", "Clear Formatting",
                     "Clear all formatting: bold, italic, colour, highlight, underline, strikethrough, line spacing"),
    "bullets": ("toggle_bullets", "list.bullet", "Bulleted list", "Bulleted list ({key})"),
    "numbers": ("toggle_numbering", "list.number", "Numbered list", "Numbered list ({key})"),
    "outdent": ("decrease_indent", "decrease.indent", "Decrease indent", "Decrease indent ({key})"),
    "indent": ("increase_indent", "increase.indent", "Increase indent", "Increase indent ({key})"),
    "align_left": ("align_left", "text.alignleft", "Align left", "Align left ({key})"),
    "align_center": ("align_center", "text.aligncenter", "Center", "Center ({key})"),
    "align_right": ("align_right", "text.alignright", "Align right", "Align right ({key})"),
    "align_justify": ("align_justify", "text.justify", "Justify",
                      "Justify ({key}) — not available for Manim text with manual line breaks"),
    "spacing": (None, "arrow.up.and.down.text.horizontal", "Line spacing", "Line spacing"),
    "columns": (None, "rectangle.split.2x1", "Columns", "Columns: lay the lines out side by side"),
    "grid": ("toggle_grid", "grid", "Grid lines", "Grid lines, one manim unit apart ({key})"),
    "snap": ("toggle_snap", "square.on.square.dashed", "Snap to objects",
             "Snap to other objects, guides and the slide while dragging ({key}; hold Alt to drag freely)"),
    "guide_v": ("add_vertical_guide", "rectangle.split.2x1", "Add a vertical guide",
                "Add a vertical guide (drag it into place, drag it off the slide to remove)"),
    "guide_h": ("add_horizontal_guide", "rectangle.split.1x2", "Add a horizontal guide",
                "Add a horizontal guide (drag it into place, drag it off the slide to remove)"),
}
_CHECKABLE = {
    "bold", "italic", "underline", "strike", "superscript", "subscript_button",
    "bullets", "numbers", "align_left", "align_center", "align_right", "align_justify", "grid", "snap",
}
_COLORS = ("highlight", "color_button", "clear_format")
_CHARACTERS = ("bold", "italic", "underline", "strike", "superscript", "subscript_button")
_PARAGRAPHS = (
    ("bullets", "numbers", "outdent", "indent"),
    ("align_left", "align_center", "align_right", "align_justify", "spacing", "columns"),
    ("grid", "snap", "guide_v", "guide_h"),
)


class _FamilyPicker(QComboBox):
    def __init__(self):
        super().__init__()
        self.addItems(QFontDatabase.families())
        self.setCurrentIndex(-1)
        self.setFont(Theme.font(12))
        self.setToolTip("Font family (usually a deck-wide constant, so it changes every slide)")
        self.setAccessibleName("Font family")
        self._styled = False

    def showPopup(self):
        if not self._styled:
            for i in range(self.count()):
                family = self.itemText(i)
                font = Theme.font(13)
                font.setFamily(family)
                font.setStyleStrategy(QFont.StyleStrategy.NoFontMerging)
                face = QRawFont.fromFont(font)
                if face.isValid() and face.familyName().casefold() == family.casefold() and all(
                        face.supportsCharacter(letter) for letter in family):
                    self.setItemData(i, font, Qt.ItemDataRole.FontRole)
            self._styled = True
        super().showPopup()

    def select_family(self, name):
        i = self.findText(name) if name else -1
        if name and i == -1:
            self.insertItem(0, name)
            i = 0
        self.setCurrentIndex(i)


class _Separator(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(1, 18)

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), Theme.separator())


class _ColorChip(QPushButton):
    def __init__(self, color, current, parent=None):
        super().__init__(parent)
        self.color = color
        self.current = current
        self.setFixedSize(23, 23)
        self.setToolTip(hex_color(color))
        self.setAccessibleName(hex_color(color))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            chip = QRectF(1.5, 1.5, 20, 20)
            painter.setPen(QPen(Theme.separator(), .5))
            painter.setBrush(self.color)
            painter.drawRoundedRect(chip, 3, 3)
            if self.current or self.underMouse() or self.hasFocus():
                painter.setPen(QPen(Theme.ink(1) if self.current else Theme.accent, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(chip.adjusted(-.5, -.5, .5, .5), 4, 4)


class ColorGridView(QWidget):
    def __init__(self, current, chosen, parent=None):
        super().__init__(parent)
        layout = QGridLayout(self)
        # 20-unit swatches with three units between their painted edges.
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(0)
        greys = [QColor.fromRgbF(v / 9, v / 9, v / 9) for v in range(9, -1, -1)]
        tints = ((.12, 1), (.25, 1), (.4, 1), (.6, 1), (.8, 1), (1, 1),
                 (1, .85), (1, .7), (1, .55), (1, .4))
        browns = ((.3, .95), (.45, .86), (.55, .77), (.65, .68), (.75, .6),
                  (.8, .5), (.85, .42), (.9, .34), (.9, .27), (.9, .2))
        rows = [greys] + [[QColor.fromHsvF(h / 360, s, v) for s, v in tints]
                          for h in (0, 30, 54, 120, 185, 220, 275)]
        rows.append([QColor.fromHsvF(28 / 360, s, v) for s, v in browns])
        selected = (current or "").upper()
        for row, colors in enumerate(rows):
            for col, color in enumerate(colors):
                hex = hex_color(color)
                chip = _ColorChip(color, hex == selected)
                chip.clicked.connect(lambda _, c=hex: chosen(c))
                layout.addWidget(chip, row, col)


class _SizeStepper(QWidget):
    step_requested = Signal(int)

    def __init__(self):
        super().__init__()
        self.value = 30
        self.setToolTip("Font size")
        self.setAccessibleName("Font size")
        self.setFixedWidth(16)
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        for direction, symbol in ((1, "chevron.up"), (-1, "chevron.down")):
            button = QToolButton()
            button.setIcon(template_icon(symbol, button))
            button.setIconSize(QSize(8, 8))
            button.setFixedSize(16, 11)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAccessibleName("Increase font size" if direction > 0 else "Decrease font size")
            button.setToolTip("Font size")
            button.clicked.connect(lambda _, delta=direction: self.step_requested.emit(delta))
            row.addWidget(button)


class FormatBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.find = None
        self._shown_color = None
        self._highlight_color = None
        self.font_popup = _FamilyPicker()
        self.font_popup.setFixedWidth(150)
        self.size_field = QLineEdit()
        self.size_field.setFont(Theme.font(12))
        self.size_field.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.size_field.setFixedWidth(max(46, self.size_field.fontMetrics().horizontalAdvance("400.5") + 12))
        self.size_field.setValidator(QDoubleValidator(self.size_field))
        self.size_field.setAccessibleName("Font size")
        self.size_field.setToolTip("Font size (manim font_size)")
        self.size_stepper = _SizeStepper()

        for name, (_, symbol, title, tip) in _HOME_COMMANDS.items():
            button = self.make_button(title, title if "{key}" in tip else tip,
                                      symbol, checkable=name in _CHECKABLE,
                                      width=44 if name in ("spacing", "columns") else 30)
            setattr(self, name, button)
        for name in ("spacing", "columns"):
            button = getattr(self, name)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            button.setMenu(QMenu(button))
        self.color_button.setIconSize(TEXT_COLOR_ICON_SIZE)
        self.set_color("#FFFFFF")

        primary = self._group((self.font_popup, self.size_field, self.size_stepper, self.bigger, self.smaller))
        # The size field and the stepper sit closer together than the other controls.
        primary.layout().setSpacing(0)
        for i, gap in reversed(tuple(enumerate((4, 2, 4, 4), 1))):
            primary.layout().insertSpacing(i, gap)
        self.characters = self._command_group(_CHARACTERS)
        self.colors = self._command_group(_COLORS)
        self.optional = [self._command_group(names) for names in _PARAGRAPHS]
        self.overflow = self.make_button("Formatting", "Formatting", "chevron.down", width=30)
        self.overflow.clicked.connect(self.show_overflow)
        self.overflow.hide()
        self.script = self.make_button(
            "Script", "Script: words to read out and pointer moves for each build, played by Play ▸ Scripted Presentation",
            "scroll", checkable=True)
        self.feedback = self.make_button(
            "View Feedback", "Feedback notes typed in Presenter View (Feedback Mode), listed in the top-right panel: walk through them and delete them",
            "text.bubble", checkable=True)
        self.feedback.setAccessibleName("View feedback")
        self.feedback.setEnabled(False)
        self.script.setEnabled(False)
        self.primary = primary
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(10, 0, 10, 0)
        self.row.setSpacing(0)
        self.row.setSizeConstraint(QHBoxLayout.SizeConstraint.SetNoConstraint)
        self.row.addWidget(primary, alignment=Qt.AlignmentFlag.AlignVCenter)
        for group in (self.characters, self.colors, self.overflow, *self.optional):
            self.row.addWidget(group, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.row.addStretch()
        self.row.addSpacing(8)
        self.row.addWidget(self.script, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.row.addSpacing(6)
        self.row.addWidget(self.feedback, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.row.addSpacing(8)
        self.setFixedHeight(max(36, self.font_popup.sizeHint().height() + 4,
                                self.size_field.sizeHint().height() + 4, self.feedback.sizeHint().height() + 4))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.refresh_appearance()
        watch_appearance(self.refresh_appearance)

    @staticmethod
    def make_button(title, tip, symbol, *, checkable=False, width=None):
        button = QToolButton()
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAccessibleName(title)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setFont(Theme.font(12))
        size = 12 if width else 11
        button.setIconSize(QSize(size, size))
        if symbol:
            button.setIcon(template_icon(symbol, button))
        if width:
            button.setFixedSize(width, 24)
        else:
            button.setText(title)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        return button

    @staticmethod
    def _group(widgets):
        group = QWidget()
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for widget in widgets:
            row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)
        group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return group

    def _command_group(self, names):
        group = self._group((_Separator(), *(getattr(self, name) for name in names)))
        group.layout().setContentsMargins(4, 0, 0, 0)
        return group

    def add_find(self, find):
        self.find = find
        self.row.addWidget(find, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(max(self.height(), find.sizeHint().height() + 4))

    def event(self, event):
        if event.type() == QEvent.Type.LayoutRequest and self.find is not None:
            self.reflow()
        return super().event(event)

    def resizeEvent(self, event):
        if self.find is not None:
            self.reflow()
        super().resizeEvent(event)

    def reflow(self):
        # Detach view, alignment and lists in that order. Their commands
        # also live in the menubar. Keep the primary controls and search
        # reachable even on tiny screens or at large text scales.
        available = self.width() - 42 - self.script.sizeHint().width() - self.feedback.sizeHint().width()
        primary = self.primary.sizeHint().width() - self.font_popup.minimumWidth() + 150
        characters, colors = self.characters.sizeHint().width(), self.colors.sizeHint().width()
        field = self.find.preferred_field_width
        required = primary + characters + colors + self.find.width_for(field)
        span = required
        for group in self.optional:
            span += group.sizeHint().width()
            group.setVisible(span <= available)
        max_shrink = 54 + field - self.find.minimum_field_width
        show_characters = required - max_shrink <= available
        if not show_characters:
            required += self.overflow.width() - characters
        show_colors = required - max_shrink <= available
        if not show_colors:
            required -= colors
        short = max(0, required - available)
        shrink = min(short, field - self.find.minimum_field_width)
        field -= shrink
        font_width = 150 - min(short - shrink, 54)
        self.characters.setVisible(show_characters)
        self.colors.setVisible(show_colors)
        self.overflow.setVisible(not show_characters or not show_colors)
        if self.font_popup.minimumWidth() != font_width:
            self.font_popup.setFixedWidth(font_width)
        self.find.set_field_width(field)

    def show_overflow(self):
        menu = QMenu(self.overflow)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.setToolTipsVisible(True)
        for group, names in ((self.characters, _CHARACTERS), (self.colors, _COLORS)):
            if group.isHidden():
                if menu.actions():
                    menu.addSeparator()
                for name in names:
                    button = getattr(self, name)
                    action = menu.addAction(button.icon(), button.accessibleName())
                    action.setEnabled(button.isEnabled())
                    action.setCheckable(button.isCheckable())
                    action.setChecked(button.isChecked())
                    action.setToolTip(button.toolTip())
                    action.triggered.connect(lambda _, button=button: button.click())
        menu.popup(self.overflow.mapToGlobal(QPoint(0, self.overflow.height() + 4)))

    def refresh_appearance(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Accent, Theme.accent)
        self.setPalette(palette)
        self.update()

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), Theme.bar_background())
            painter.fillRect(0, self.height() - 1, self.width(), 1, Theme.separator())

    def set_color(self, color):
        color = color_from_hex(color)
        if color != self._shown_color:
            self._shown_color = color
            self.color_button.setIcon(text_color_icon(color))

    def set_highlight(self, color):
        if color == self._highlight_color:
            return
        self._highlight_color = color
        tint = color_from_hex(color, "#FFD400") if color else None
        self.highlight.setIcon(template_icon("highlighter", self.highlight, tint=tint))


class FormatBarMixin:
    highlight_swatches = [("Yellow", "#FFD400"), ("Green", "#7CFC00"), ("Cyan", "#40E0FF"), ("Pink", "#FF6EC7"), ("Blue", "#4D7CFF"), ("Red", "#FF4040"), ("Orange", "#FF9A2E"), ("Gray", "#8A8A8A"), ("No Highlight", None)]

    def make_format_bar(self):
        bar = FormatBar()
        self.format_bar = bar
        bar.font_popup.textActivated.connect(self.family_changed)
        bar.size_field.editingFinished.connect(self._home_size_committed)
        bar.size_stepper.step_requested.connect(self._home_size_step)
        bar.highlight.clicked.connect(self.highlight_pressed)
        bar.color_button.clicked.connect(self.color_button_pressed)
        bar.feedback.clicked.connect(self.view_feedback)
        bar.script.clicked.connect(self.view_script)
        self.canvas.on_inline_selection_style = self._sync_home_checks
        QApplication.instance().focusChanged.connect(self._home_focus_changed)
        return bar

    def bind_home_actions(self):
        """Bind after register_actions; the widgets never register accelerators."""
        bar, actions = self.format_bar, self.actions
        for name, (command, _, _, tip) in _HOME_COMMANDS.items():
            if command is None:
                continue
            button = getattr(bar, name)
            button.clicked.connect(lambda _, id=command: actions.trigger(id))
            if "{key}" in tip:
                button.setToolTip(tip.format(key=actions.shortcut_text(command)))
            self._bind_home_enabled(button, actions.action(command))
        bar.highlight_menu = QMenu(bar.highlight)
        highlight = bar.highlight_menu
        spacing, columns = bar.spacing.menu(), bar.columns.menu()
        titles = ("One column", "Two columns", "Three columns", "Four columns")
        for id, command in actions.commands.items():
            action = command.action
            handler, value = action.property("handler"), action.data()
            if handler == "highlight_chosen":
                if not highlight.actions():
                    self._bind_home_enabled(bar.highlight, action)
                icon = None
                if value:
                    color = QColor(value)

                    def swatch(painter, rect, color=color):
                        painter.setBrush(color)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

                    icon = painted_icon(16, 16, swatch)
                highlight.addAction(actions.present(id, highlight, icon=icon))
            elif handler == "spacing_menu_chosen":
                if not spacing.actions():
                    self._bind_home_enabled(bar.spacing, action)
                title = "Custom…" if value is None else str(float(value))
                spacing.addAction(actions.present(id, spacing, text=title))
            elif handler == "columns_menu_chosen":
                if not columns.actions():
                    self._bind_home_enabled(bar.columns, action)
                columns.addAction(actions.present(id, columns, text=titles[int(value) - 1], mirror_check=False))
        for menu in (highlight, spacing, columns):
            menu.aboutToShow.connect(actions.refresh)
        self.find_bar.bind_shortcut_help(actions)
        actions.refresh()

    @staticmethod
    def _bind_home_enabled(button, action):
        # Checked state can come from the active text run rather than the menu's
        # object-level state. Copy only enablement across the two presentations.
        action.changed.connect(lambda: button.setEnabled(action.isEnabled()))
        button.setEnabled(action.isEnabled())

    def _home_size_committed(self):
        field = self.format_bar.size_field
        if not field.isModified():
            return
        value, ok = field.validator().locale().toDouble(field.text())
        field.setModified(False)
        if ok and isfinite(value) and value >= 4 and value != (self.obj_info or {}).get("fontSize"):
            self.format_bar.size_stepper.value = min(400, value)
            self.commit_font_size(value)

    def _home_size_step(self, delta):
        field, stepper = self.format_bar.size_field, self.format_bar.size_stepper
        value = stepper.value
        if field.isModified():
            typed, ok = field.validator().locale().toDouble(field.text())
            if ok and isfinite(typed):
                value = min(400, max(4, typed))
        new = min(400, max(4, value + delta))
        if new != value:
            stepper.value = new
            field.setText(field.validator().locale().toString(new, "g", 8))
            self.commit_font_size(new)

    def bind_inspector_text_signals(self, field):
        field.currentCharFormatChanged.connect(self._inspector_caret_changed)
        field.selectionChanged.connect(self._inspector_caret_changed)
        field.textChanged.connect(self._inspector_caret_changed)

    def _home_focus_changed(self, old, new):
        field = self.text_field
        if field is not None and (old is field or new is field):
            self._sync_home_checks()

    def _inspector_caret_changed(self, *_):
        if self._active_inspector() is not None:
            self._sync_home_checks()

    def _active_inspector(self):
        field = self.text_field
        return field if field is not None and field.hasFocus() else None

    def _active_canvas_text(self):
        if self.selection is not None and self.canvas.editing_text == self.selection and self.canvas.editing_cell is None:
            inline = self.canvas._inline
            if inline is not None and not inline.view.isReadOnly():
                return inline.view
        return None

    def sync_format_bar(self):
        bar = self.format_bar
        o = self.obj_info or {}
        fmt = o.get("format") or {}
        is_text = o.get("kind") == "text"
        has_size = o.get("kind") in ("text", "tex")
        bar.font_popup.setEnabled(is_text)
        bar.font_popup.select_family(o.get("font") if is_text else None)
        with QSignalBlocker(bar.size_field):
            bar.size_field.setEnabled(has_size)
            bar.size_stepper.setEnabled(has_size)
            value = o.get("fontSize")
            bar.size_stepper.value = min(400, max(4, value if value is not None else 30))
            if not bar.size_field.hasFocus():
                value = floor((value or 0) * 10 + .5) / 10
                bar.size_field.setText(bar.size_field.validator().locale().toString(value, "g", 8) if has_size else "")
        bar.color_button.setEnabled(bool(o) and o.get("kind") != "image")
        bar.set_color(o.get("color") or "#FFFFFF")
        bar.set_highlight(fmt.get("highlight"))
        self._sync_home_checks()

    def _sync_home_checks(self, canvas_kinds=None):
        bar = self.format_bar
        o = self.obj_info or {}
        fmt = o.get("format") or {}
        is_text = o.get("kind") == "text"
        inspector, canvas_editor = self._active_inspector(), self._active_canvas_text()
        mode = None
        if inspector is not None:
            mode = inspector.text_script_mode
            char = inspector.currentCharFormat()
            checks = dict(bold=char.fontWeight() >= QFont.Weight.Bold, italic=char.fontItalic(),
                          underline=char.fontUnderline(), strike=char.fontStrikeOut())
        elif canvas_editor is not None:
            mode = self.canvas.text_script_mode
            kinds = canvas_kinds if canvas_kinds is not None else self.canvas.selection_kinds(canvas_editor)
            checks = {kind: kind in kinds for kind in ("bold", "italic", "underline", "strike")}
        else:
            checks = dict(bold=(o.get("weight") or "").upper() == "BOLD",
                          italic=(o.get("slant") or "").upper() in ("ITALIC", "OBLIQUE"),
                          underline=bool(fmt.get("underline")), strike=bool(fmt.get("strike")))
        editor = inspector if inspector is not None else canvas_editor
        text = editor.toPlainText() if is_text and editor is not None else o.get("value") or ""
        checks.update(superscript=mode is True, subscript_button=mode is False,
                      bullets=is_text and TextTransform.is_bulleted(text),
                      numbers=is_text and TextTransform.is_numbered(text),
                      grid=self.canvas.show_grid, snap=self.canvas.snap_enabled)
        for key in ("left", "center", "right", "justify"):
            checks["align_" + key] = is_text and (fmt.get("align") or "left") == key
        for name, on in checks.items():
            getattr(bar, name).setChecked(on)

    def _inline_style(self, kind, on=None, hex=None, editor=None):
        field = editor if editor is not None and editor is self.text_field else self._active_inspector()
        if field is not None:
            return field.toggle_inline_style(kind) if on is None else field.set_inline_style(kind, on, hex)
        method = getattr(self.canvas, "toggle_inline_style" if on is None else "set_inline_style", None)
        if method is None:
            return None
        if on is None:
            return method(kind)
        return True if method(kind, on=on, hex=hex) else None

    def commit_color(self, hex, *, editor=None):
        if self._inline_style("color", True, hex, editor) is not None:
            return
        oid, obj = self.selection, self.obj_info or {}
        if oid is not None and obj.get("kind") != "image" and (obj.get("color") or "").upper() != hex.upper():
            def work():
                if obj.get("kind") in ("text", "tex"):
                    yield from self.preview_sprite(oid, obj["kind"], obj.get("value", ""), obj.get("fontSize") or 30,
                                                  hex, obj.get("font"), obj.get("weight"), obj.get("slant"))
                return (yield from self.run_edit({"op": "color", "oid": oid, "value": hex}))
            return self.submit(work)
    def color_button_pressed(self, *_):
        obj = self.obj_info or {}
        if not obj or obj.get("kind") == "image":
            return
        bar = self.format_bar
        anchor = bar.color_button if bar.color_button.isVisible() else bar.overflow
        popup = QMenu(anchor)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        editor = self._active_inspector()

        def choose(hex):
            popup.close()
            bar.set_color(hex)
            self.commit_color(hex, editor=editor)

        grid = QWidgetAction(popup)
        grid.setDefaultWidget(ColorGridView(obj.get("color"), choose, popup))
        popup.addAction(grid)
        popup.addSeparator()
        popup.addAction("More Colours…", lambda: self.open_color_panel(anchor, editor))
        popup.popup(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))

    def open_color_panel(self, anchor=None, editor=None):
        obj = self.obj_info or {}
        if not obj or obj.get("kind") == "image":
            return
        if anchor is None:
            editor = editor if editor is not None else self._active_inspector()
            color = QColorDialog.getColor(color_from_hex(obj.get("color")), self.window)
            if color.isValid():
                return self.commit_color(hex_color(color), editor=editor)
            return
        panel = QColorDialog(color_from_hex(obj.get("color")), self.window)
        panel.setOption(QColorDialog.ColorDialogOption.NoButtons)
        panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        panel.setWindowFlag(Qt.WindowType.Tool)
        target = (self.workspace, self.scene_name, self.stop_index, self.selection)
        timer = QTimer(panel)
        timer.setInterval(350)
        timer.setSingleShot(True)

        def still_selected():
            return target == (self.workspace, self.scene_name, self.stop_index, self.selection)

        def apply():
            if still_selected():
                self.commit_color(hex_color(panel.currentColor()), editor=editor)

        def changed(color):
            if still_selected():
                self.format_bar.set_color(hex_color(color))
                timer.start()

        def finish(_result):
            if timer.isActive():
                timer.stop()
                apply()

        timer.timeout.connect(apply)
        panel.currentColorChanged.connect(changed)
        panel.finished.connect(finish)
        panel.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        panel.show()

    def commit_decoration(self, kind, on):
        oid = self.selection
        if oid is not None: return self.submit(lambda: self.run_edit({"op": "decorate", "oid": oid, "kind": kind, "on": bool(on)}))
    def _decoration(self, kind):
        result = self._inline_style(kind)
        if result is not None:
            getattr(self.format_bar, kind).setChecked(result)
            return
        return self.commit_decoration(kind, not ((self.obj_info or {}).get("format") or {}).get(kind))
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
        bar = self.format_bar
        anchor = bar.highlight if bar.highlight.isVisible() else bar.overflow
        bar.highlight_menu.popup(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
    def clear_formatting(self):
        oid = self.selection
        if oid is not None: return self.submit(lambda: self.run_edit({"op": "clear_format", "oid": oid}))
    def format_bold(self):
        result = self._inline_style("bold")
        if result is not None:
            self.format_bar.bold.setChecked(result)
            return
        return self.commit_style(weight="NORMAL" if ((self.obj_info or {}).get("weight") or "").upper() == "BOLD" else "BOLD")
    def format_italic(self):
        result = self._inline_style("italic")
        if result is not None:
            self.format_bar.italic.setChecked(result)
            return
        return self.commit_style(slant="NORMAL" if ((self.obj_info or {}).get("slant") or "").upper() in ("ITALIC", "OBLIQUE") else "ITALIC")
    def format_bigger(self): return self.scale_font(1.1)
    def format_smaller(self): return self.scale_font(1 / 1.1)
    def scale_font(self, scale):
        value = (self.obj_info or {}).get("fontSize")
        if value is not None:
            return self.commit_font_size(max(4, floor(value * scale + .5)))

    def apply_script(self, superscript):
        obj = self.obj_info or {}
        kind = "superscript" if superscript else "subscript"
        if obj.get("kind") != "text" or not obj.get("textOk"):
            self._sync_home_checks()
            self.toast(f"Select a text object first; {kind} applies to its text", error=True)
            return
        editor = self.canvas if self._active_canvas_text() is not None else self._active_inspector()
        if editor is not None:
            if not editor.script_selection(superscript):
                active = editor.text_script_mode is not superscript
                editor.text_script_mode = superscript if active else None
                self.toast(f"Characters you type now are {kind} · press again to stop" if active else "Back to normal characters")
            self._sync_home_checks()
            return
        field = self.text_field
        converted = TextTransform.script(field.toPlainText() if field is not None else obj.get("value", ""), superscript)
        self._sync_home_checks()
        if converted is None:
            self.toast(f"No {kind} form exists for these characters", error=True)
        else:
            if field is not None:
                field.set_text(converted)
            return self.commit_text(self.selection, converted, shared=self.shared_choice)
    def format_superscript(self): return self.apply_script(True)
    def format_subscript(self): return self.apply_script(False)
    def transform_text(self, transform):
        obj = self.obj_info or {}
        if obj.get("kind") != "text" or not obj.get("textOk"):
            self.toast("Select a text object first", error=True); return
        editor = self._active_canvas_text() or self._active_inspector()
        field = editor if editor is not None else self.text_field
        text = field.toPlainText() if field is not None else obj.get("value", "")
        cursor = field.textCursor() if field is not None else None
        range_ = (cursor.selectionStart(), cursor.selectionEnd() - cursor.selectionStart()) if cursor and cursor.hasSelection() else None
        new = transform(text, range_)
        if new == text:
            self._sync_home_checks()
            return
        if editor is None:
            if field is not None:
                field.set_text(new)
            return self.commit_text(self.selection, new, shared=self.shared_choice)
        # List and indent transformations only change line prefixes. Leave the
        # remaining characters' formatting intact and make this one local edit.
        doc = editor.document()
        edit = QTextCursor(doc)
        edit.beginEditBlock()
        for i, (old, updated) in enumerate(zip(text.split("\n"), new.split("\n"))):
            if old == updated:
                continue
            common = 0
            while common < min(len(old), len(updated)) and old[-1 - common] == updated[-1 - common]:
                common += 1
            prefix = old[:len(old) - common]
            start = doc.findBlockByNumber(i).position()
            edit.setPosition(start)
            edit.setPosition(start + len(prefix.encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
            edit.insertText(updated[:len(updated) - common])
        edit.endEditBlock()
        self._sync_home_checks()
    def toggle_bullets(self): return self.transform_text(lambda s, r: TextTransform.toggle_bullets(s))
    def toggle_numbering(self): return self.transform_text(lambda s, r: TextTransform.toggle_numbers(s))
    def increase_indent(self): return self.transform_text(lambda s, r: TextTransform.indent(s, TextTransform.line_range(s, r) if r else None, 4))
    def decrease_indent(self): return self.transform_text(lambda s, r: TextTransform.indent(s, TextTransform.line_range(s, r) if r else None, -4))
    def commit_align(self, value):
        obj = self.obj_info or {}
        if obj.get("kind") != "text": return
        if "\n" not in obj.get("value", "") and obj.get("frame") is None:
            self._sync_home_checks()
            self.toast("Alignment shows on text with several lines (↵ in the text box adds one), or in a text box of a slide layout", error=True)
            return
        oid = self.selection
        return self.submit(lambda: self.run_edit({"op": "align", "oid": oid, "value": value}))
    def align_left(self): return self.commit_align("left")
    def align_center(self): return self.commit_align("center")
    def align_right(self): return self.commit_align("right")
    def align_justify(self):
        self.sync_format_bar(); self.toast("Manim cannot justify text with manual line breaks; use left, centre or right", error=True)
    def spacing_chosen(self, value):
        value = payload(value)
        obj = self.obj_info or {}
        oid = self.selection
        if oid is None or obj.get("kind") != "text":
            return
        if value is None:
            current = (obj.get("format") or {}).get("lineSpacing")
            dialog = QDialog(self.window)
            dialog.setFont(Theme.font(13))
            dialog.setWindowTitle("Line spacing")
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Multiple of single spacing (1.0 = Manim's default):"))
            field = QLineEdit()
            field.setFont(Theme.font(13))
            validator = QDoubleValidator(field)
            field.setValidator(validator)
            field.setFixedWidth(max(80, field.fontMetrics().horizontalAdvance("100.00") + 12))
            layout.addWidget(field, alignment=Qt.AlignmentFlag.AlignLeft)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            apply = buttons.button(QDialogButtonBox.StandardButton.Ok)
            apply.setText("Apply")
            apply.setDefault(True)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            field.selectAll()
            field.setFocus()
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            value, ok = validator.locale().toDouble(field.text())
            if not ok or not isfinite(value) or value <= 0:
                return
        return self.submit(lambda: self.run_edit({"op": "spacing", "oid": oid, "value": max(-.6, float(value) - .7)}))
    def spacing_menu_chosen(self, value): return self.spacing_chosen(value)
    def columns_menu_chosen(self, value): return self.commit_columns(int(payload(value)))
    def commit_columns(self, n):
        obj = self.obj_info or {}
        if obj.get("kind") != "text" or n not in range(1, 5): return
        if n > 1 and "\n" not in obj.get("value", "") and (obj.get("format") or {}).get("columns", 1) == 1:
            self.toast("Columns lay out the lines of a text; add lines with ↵ in the text box first", error=True); return
        oid = self.selection
        return self.submit(lambda: self.run_edit({"op": "columns", "oid": oid, "value": n}))
    def toggle_grid(self):
        self.canvas.show_grid = not getattr(self.canvas, "show_grid", False)
        prefs.set("showGrid", self.canvas.show_grid); self.canvas.update(); self.sync_format_bar()
    def toggle_snap(self):
        self.canvas.snap_enabled = not getattr(self.canvas, "snap_enabled", True)
        prefs.set("snapObjects", self.canvas.snap_enabled); self.sync_format_bar()
        self.toast("Snapping on: hold Alt to drag freely" if self.canvas.snap_enabled else "Snapping off")
    @property
    def guides_key(self): return "guides:" + str(self.original_deck) if self.original_deck else None
    def add_guide(self, vertical):
        if not self.scene_name: return
        width, height = frame_size(self.scene)
        self.canvas.user_guides = list(getattr(self.canvas, "user_guides", [])) + [(vertical, width / 2 if vertical else height / 2)]
        self.save_guides(); self.canvas.update()
        self.toast("Guide added at the centre: drag it into place, drag it off the slide to remove it")
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

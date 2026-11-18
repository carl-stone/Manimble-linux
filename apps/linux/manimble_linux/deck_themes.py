"""Bundled theme metadata is parsed, never executed by the front end."""
from __future__ import annotations
import ast
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                              QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .formatbar import payload
from .icon_art import THEME_SWATCH_SIZE, theme_swatch
from .paths import engine_dir
from .style import Theme, watch_appearance
from .tasks import spawn
from .widgets import field_label, hairline


@dataclass(frozen=True)
class DeckTheme:
    id: str
    name: str
    source: str
    background: str
    text: str

    @classmethod
    def all(cls):
        result = []
        for path in (engine_dir() / "manimble" / "themes").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            values = {}
            for node in ast.parse(source).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name): values[target.id] = node.value.value
            if "THEME" in values:
                result.append(cls(path.stem, values["THEME"], source, values.get("BACKGROUND", "#000000"), values.get("TEXT_COLOR", "#FFFFFF")))
        return sorted(result, key=lambda theme: (theme.id != "midnight", theme.name))

    @property
    def swatch(self):
        return theme_swatch(self.background, self.text)


class _ThemePreview(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(480, 270)
        self.image = QImage()
        self.phase = 0
        self.spinner = QTimer(self)
        self.spinner.setInterval(65)
        self.spinner.timeout.connect(self._spin)
        self.status = field_label("", self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.addWidget(self.status)
        watch_appearance(self._repaint)

    def _repaint(self):
        self.update()

    def _spin(self):
        self.phase = (self.phase + 1) % 12
        self.update()

    def begin(self):
        self.status.clear()
        if not self.spinner.isActive():
            self.spinner.start()
        self.update()

    def show_result(self, image, status=""):
        self.spinner.stop()
        self.image = image
        self.status.setText(status)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        spinning = self.spinner.isActive()
        if not self.image.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(bounds, 6, 6)
            painter.save()
            painter.setClipPath(clip)
            painter.setOpacity(.35 if spinning else 1)
            scale = min(bounds.width() / self.image.width(), bounds.height() / self.image.height())
            rect = QRectF(0, 0, self.image.width() * scale, self.image.height() * scale)
            rect.moveCenter(bounds.center())
            painter.drawImage(rect, self.image)
            painter.restore()
        painter.setPen(QPen(Theme.separator(), 1))
        painter.drawRoundedRect(bounds.adjusted(.5, .5, -.5, -.5), 6, 6)
        if spinning:
            painter.translate(bounds.center())
            painter.rotate(self.phase * 30)
            for index in range(12):
                painter.setPen(QPen(Theme.ink(.15 + index / 17), 1.5,
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(0, -8, 0, -5)
                painter.rotate(30)


class ThemeSheet(QDialog):
    def __init__(self, theme, scene, engine, apply, parent=None, *, undo_shortcut="Ctrl+Z"):
        super().__init__(parent)
        self.scene, self.engine, self.apply_fn = scene, engine, apply
        self.undo_shortcut = undo_shortcut
        self.generation = 0
        self.dismissed = False
        self.previews = {}
        self.crosses = {}
        self.checking_crosses = set()
        self.themes = DeckTheme.all()
        self.setWindowTitle("Theme")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFont(Theme.font(13))
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("Apply Theme", self)
        heading.setFont(Theme.font(13, QFont.Weight.DemiBold))
        layout.addWidget(heading)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        theme_row.addWidget(field_label("Theme", self))
        self.picker = QComboBox()
        self.picker.setIconSize(THEME_SWATCH_SIZE)
        for item in self.themes: self.picker.addItem(item.swatch, item.name, item.id)
        self.picker.setCurrentIndex(max(0, self.picker.findData(theme.id)))
        self.picker.setAccessibleName("Theme")
        theme_row.addWidget(self.picker)
        theme_row.addStretch()
        layout.addLayout(theme_row)

        preview_column = QVBoxLayout()
        preview_column.setSpacing(6)
        caption = field_label(f"Preview of {scene}", self)
        caption.setTextFormat(Qt.TextFormat.PlainText)
        caption.setWordWrap(True)
        preview_column.addWidget(caption)
        self.preview = _ThemePreview(self)
        preview_column.addWidget(self.preview)
        layout.addLayout(preview_column)

        self.slide_only = QCheckBox("Apply to selected slide only", self)
        self.flip = QCheckBox("Also swap whites and blacks in the slides (for text that does not use TEXT_COLOR)", self)
        self.flip.setFont(Theme.font(11))
        self.flip.setChecked(True)
        self.detail = field_label("", self)
        self.detail.setWordWrap(True)
        layout.addWidget(self.slide_only)
        layout.addWidget(self.flip)
        layout.addWidget(self.detail)
        layout.addStretch()
        layout.addWidget(hairline(self))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        cancel_button = QPushButton("Cancel", self)
        cancel_button.setAutoDefault(False)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        apply_button = QPushButton("Apply", self)
        apply_button.setDefault(True)
        apply_button.clicked.connect(self.apply_pressed)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

        self.picker.currentIndexChanged.connect(self.changed)
        self.slide_only.toggled.connect(self.changed)
        self.flip.toggled.connect(self.load_preview)
        self.finished.connect(self.dismiss)
        self.changed()

    @property
    def chosen(self):
        return self.themes[self.picker.currentIndex()]

    @property
    def flipping(self):
        return not self.slide_only.isChecked() and self.crosses.get(self.chosen.id, False) and self.flip.isChecked()

    def dismiss(self, _):
        self.dismissed = True
        self.generation += 1
        self.preview.spinner.stop()

    def request(self, req):
        return self.engine.request("theme", req) if hasattr(self.engine, "request") else self.engine("theme", req)

    def changed(self, *_):
        one = self.slide_only.isChecked()
        theme_id = self.chosen.id
        undo = self.undo_shortcut
        if one:
            self.detail.setText(
                "This slide gets the theme's background, and its own code uses the theme's FONT, TEXT_COLOR, "
                "MUTED_COLOR and ACCENT; helpers shared with other slides keep the deck's. Choosing the deck's own "
                f"theme here takes the slide back to the deck's. Undo ({undo}) reverts it.")
        else:
            self.detail.setText(
                "The deck's font, text colour and accent constants (FONT, TEXT_COLOR, MUTED_COLOR, ACCENT) and "
                "its background are set to the theme's, and slides with a theme of their own take the deck's. "
                f"Every slide is re-rendered. Undo ({undo}) reverts it in one step.")
        self.flip.setVisible(not one and self.crosses.get(theme_id, False))
        if theme_id in self.crosses:
            self.load_preview()
            return
        self.generation += 1
        self.preview.begin()
        if theme_id in self.checking_crosses:
            return
        self.checking_crosses.add(theme_id)

        def work():
            if self.dismissed:
                return
            try:
                info = yield self.request({"op": "get", "theme": theme_id})
                crosses = bool(info.get("crosses"))
            except Exception:
                # Unknown crossing: try the preview without swapping, as the reference does.
                crosses = False
            self.checking_crosses.discard(theme_id)
            self.crosses[theme_id] = crosses
            if not self.dismissed and self.chosen.id == theme_id:
                self.flip.setVisible(not self.slide_only.isChecked() and crosses)
                self.load_preview()

        spawn(work())

    def load_preview(self, *_):
        theme_id, one, flip = self.chosen.id, self.slide_only.isChecked(), self.flipping
        self.generation += 1
        generation = self.generation
        key = (theme_id, one, flip)
        if key in self.previews:
            self.preview.show_result(self.previews[key])
            return
        self.preview.begin()

        def work():
            if generation != self.generation:
                return
            image = QImage()
            try:
                reply = yield self.request({"op": "preview", "theme": theme_id, "scene": self.scene,
                                            "slide_only": one, "flip": flip})
                if self.dismissed:
                    return
                path = reply.get("png")
                failure = reply.get("error")
                if path and not image.load(path) and failure is None:
                    failure = "Could not read theme preview"
            except Exception as exc:
                failure = str(exc)
            if not image.isNull():
                self.previews[key] = image
            if generation == self.generation:
                if image.isNull():
                    failure = failure if failure is not None else "the slide did not render"
                    self.preview.show_result(image, f"No preview: {failure}")
                else:
                    self.preview.show_result(image)

        spawn(work())

    def apply_pressed(self):
        theme, one, flip = self.chosen, self.slide_only.isChecked(), self.flipping
        apply = self.apply_fn
        self.accept()
        apply(theme, one, flip)


class DeckThemesMixin:
    def theme_chosen(self, item="midnight"):
        theme_id = payload(item)
        themes = DeckTheme.all()
        theme = next((t for t in themes if t.id == theme_id), None)
        if not theme or not self.scene_name or getattr(self, "theme_sheet", None) is not None:
            return
        target = self.scene_name
        sheet = ThemeSheet(theme, target, self.call,
                           lambda t, one, flip: self.run_apply_theme(t, flip=flip, slide_only=one, scene=target),
                           self.window, undo_shortcut=self.actions.shortcut_text("undo"))
        self.theme_sheet = sheet
        sheet.finished.connect(lambda _: setattr(self, "theme_sheet", None))
        sheet.open()

    def run_apply_theme(self, theme, flip=False, slide_only=False, scene=None):
        theme_id, name = (theme.id, theme.name) if isinstance(theme, DeckTheme) else (theme, theme)
        req = {"theme": theme_id, "flip": flip, "slide_only": slide_only, "current": scene or self.scene_name}

        def work():
            self.busy_text = f"Applying the {name} theme…"
            self.busy += 1
            try:
                result = yield self.call("theme", req)
            except Exception as exc:
                self.toast(str(exc), error=True)
                return
            finally:
                self.busy -= 1
                self.busy_text = "Rendering…"
            if not result.get("ok"):
                self.toast(result.get("error") or "theme change failed", error=True)
                return result
            self.last_changed = result.get("changed")
            message = result.get("message") or ""
            if message == "no change":
                self.toast("The slide already has this theme" if slide_only else "The deck already has this theme")
            else:
                self.toast(f"Saved · {message}")
            self.canvas.drop_textures()
            yield from self.reload_state()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
            return result

        return self.submit(work)

"""Bundled theme metadata is parsed, never executed by the front end."""
from __future__ import annotations
import ast
from dataclasses import dataclass
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout
from .paths import engine_dir
from .tasks import spawn
from .formatbar import payload


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
        pixmap = QPixmap(44, 25); pixmap.fill(QColor(self.background))
        painter = QPainter(pixmap); painter.setPen(QColor(self.text)); painter.setFont(QFont("DejaVu Sans", 12)); painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Aa"); painter.end()
        return QIcon(pixmap)


class ThemeSheet(QDialog):
    def __init__(self, theme, scene, engine, apply, parent=None):
        super().__init__(parent)
        self.scene, self.engine, self.apply_fn = scene, engine, apply
        self.generation = 0; self.previews = {}; self.crosses = {}
        self.themes = DeckTheme.all()
        self.setWindowTitle("Theme"); self.resize(520, 560)
        layout = QVBoxLayout(self)
        self.picker = QComboBox()
        for item in self.themes: self.picker.addItem(item.swatch, item.name, item.id)
        self.picker.setCurrentIndex(max(0, self.picker.findData(theme.id)))
        self.slide_only = QCheckBox("Apply to selected slide only")
        self.flip = QCheckBox("Also swap whites and blacks in slides that do not use TEXT_COLOR")
        self.flip.setChecked(True)
        self.image_view = QLabel(); self.image_view.setAlignment(Qt.AlignmentFlag.AlignCenter); self.image_view.setMinimumHeight(220)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setStyleSheet("color:#FC6255")
        self.detail = QLabel(); self.detail.setWordWrap(True)
        for widget in (self.picker, self.slide_only, self.flip, self.image_view, self.status, self.detail): layout.addWidget(widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply_pressed); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.picker.currentIndexChanged.connect(self.changed); self.slide_only.toggled.connect(self.changed); self.flip.toggled.connect(self.load_preview)
        self.finished.connect(lambda _: self.invalidate())
        self.changed()
    @property
    def chosen(self): return self.themes[self.picker.currentIndex()]
    @property
    def flipping(self): return not self.slide_only.isChecked() and self.crosses.get(self.chosen.id, False) and self.flip.isChecked()
    def invalidate(self): self.generation += 1
    def request(self, req):
        return self.engine.request("theme", req) if hasattr(self.engine, "request") else self.engine("theme", req)
    def changed(self, *_):
        theme = self.chosen
        self.detail.setText("This slide gets its own background and theme constants; shared helpers keep the deck's. Choosing the deck theme restores inheritance. Undo reverts it." if self.slide_only.isChecked() else "Changes the deck font, text, muted and accent constants and background, removing slide-specific themes. Every slide re-renders; Undo reverts it in one step.")
        self.generation += 1
        generation = self.generation
        self.flip.setVisible(not self.slide_only.isChecked() and self.crosses.get(theme.id, False))
        if theme.id in self.crosses:
            self.load_preview(); return
        def work():
            try:
                info = yield self.request({"op": "get", "theme": theme.id, "current": self.scene})
                if info.get("error"): raise RuntimeError(info["error"])
                self.crosses[theme.id] = bool(info.get("crosses"))
                if generation == self.generation:
                    self.flip.setVisible(not self.slide_only.isChecked() and self.crosses[theme.id]); self.load_preview()
            except Exception as exc:
                if generation == self.generation: self.status.setText(str(exc))
        spawn(work())
    def load_preview(self, *_):
        theme, one, flip = self.chosen, self.slide_only.isChecked(), self.flipping
        self.generation += 1; generation = self.generation
        key = (theme.id, one, flip)
        self.status.setText("Rendering preview…")
        if key in self.previews:
            self.show_preview(self.previews[key]); return
        def work():
            try:
                reply = yield self.request({"op": "preview", "theme": theme.id, "scene": self.scene, "slide_only": one, "flip": flip})
                if not reply.get("png"): raise RuntimeError(reply.get("error", "The slide did not render"))
                image = QPixmap(reply["png"])
                if image.isNull(): raise RuntimeError("Could not read theme preview")
                self.previews[key] = image
                if generation == self.generation: self.show_preview(image)
            except Exception as exc:
                if generation == self.generation: self.status.setText("No preview: " + str(exc))
        spawn(work())
    def show_preview(self, image):
        self.image_view.setPixmap(image.scaled(self.image_view.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)); self.status.clear()
    def apply_pressed(self):
        theme, one, flip = self.chosen, self.slide_only.isChecked(), self.flipping
        self.accept(); self.apply_fn(theme, one, flip)


class DeckThemesMixin:
    def theme_chosen(self, item="midnight"):
        theme_id = payload(item)
        themes = DeckTheme.all()
        theme = next((t for t in themes if t.id == theme_id), None)
        if not theme or not self.scene_name: return
        target = self.scene_name
        self.theme_sheet = ThemeSheet(theme, target, self.rpc, lambda t, one, flip: self.run_apply_theme(t, flip=flip, slide_only=one, scene=target), self.window)
        self.theme_sheet.open()
    def run_apply_theme(self, theme, flip=False, slide_only=False, scene=None):
        return self.tool_command("theme", {"theme": theme.id if isinstance(theme, DeckTheme) else theme, "flip": flip, "slide_only": slide_only, "current": scene or self.scene_name})

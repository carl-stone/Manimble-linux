"""Solid/picture backgrounds with a non-mutating local preview."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGridLayout, QLabel, QPushButton, QSlider, QWidget)
from .slide_tools import asset_result
from .tasks import spawn


@dataclass
class BackgroundInfo:
    mode: str = "none"
    color: str | None = None
    image: str | None = None
    transparency: float = 0
    default: str = "#000000"

    @classmethod
    def from_reply(cls, reply):
        return cls(**{key: value for key, value in reply.items() if key in cls.__dataclass_fields__})


@dataclass
class BackgroundChoice:
    mode: str
    color: str
    image: Path | None
    transparency: float


class _BackgroundPreview(QWidget):
    def __init__(self, owner):
        super().__init__(owner); self.owner = owner; self.setMinimumSize(360, 202)
    def paintEvent(self, event):
        choice = self.owner.choice
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.owner.info.default))
        painter.setOpacity(1 - choice.transparency)
        if choice.mode == "image" and self.owner.image_data and not self.owner.image_data.isNull():
            painter.drawImage(self.rect(), self.owner.image_data)
        else: painter.fillRect(self.rect(), QColor(choice.color))


class BackgroundSheet(QDialog):
    presets = ["#000000", "#1E1E1E", "#3C3C3C", "#808080", "#D9D9D9", "#FFFFFF", "#FDF6E3", "#E8E4D9", "#002B36", "#0C1B33", "#1F3A5F", "#2E5E8C", "#236B8E", "#58C4DD", "#83C167", "#FFFF00", "#FF862F", "#FC6255", "#9A72AC", "#7C5CFF"]
    def __init__(self, info, apply, reset, parent=None):
        super().__init__(parent)
        self.info = BackgroundInfo.from_reply(info) if isinstance(info, dict) else info
        self.apply_fn, self.reset_fn = apply, reset
        self.image = Path(self.info.image) if self.info.image else None
        self.image_data = QImage(str(self.image)) if self.image else None
        self.color = self.info.color or self.info.default
        self.setWindowTitle("Format Background"); self.resize(460, 500)
        form = QFormLayout(self)
        self.mode = QComboBox(); self.mode.addItem("Solid color", "color"); self.mode.addItem("Picture", "image"); self.mode.setCurrentIndex(1 if self.info.mode == "image" else 0)
        form.addRow("Fill", self.mode)
        self.well = QPushButton(self.color); self.well.clicked.connect(self.pick_color); form.addRow("Colour", self.well)
        swatches = QWidget(); grid = QGridLayout(swatches)
        for i, color in enumerate(self.presets):
            button = QPushButton(); button.setFixedSize(26, 22); button.setStyleSheet(f"background:{color}"); button.setToolTip(color); button.clicked.connect(lambda _, c=color: self.set_color(c)); grid.addWidget(button, i // 10, i % 10)
        form.addRow(swatches)
        self.choose_button = QPushButton("Choose Picture…"); self.choose_button.clicked.connect(self.choose_picture); form.addRow(self.choose_button)
        self.file_label = QLabel(str(self.image or "No picture chosen")); self.file_label.setWordWrap(True); form.addRow(self.file_label)
        self.slider = QSlider(Qt.Orientation.Horizontal); self.slider.setRange(0, 100); self.slider.setValue(round((self.info.transparency or 0) * 100))
        self.percent = QLabel(); form.addRow("Transparency", self.slider); form.addRow(self.percent)
        self.preview = _BackgroundPreview(self); form.addRow(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_all_button = buttons.addButton("Apply to All", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Reset", QDialogButtonBox.ButtonRole.ResetRole).clicked.connect(lambda: (self.accept(), self.reset_fn()))
        self.apply_button.clicked.connect(lambda: self.apply_pressed(False)); self.apply_all_button.clicked.connect(lambda: self.apply_pressed(True)); buttons.rejected.connect(self.reject); form.addRow(buttons)
        self.mode.currentIndexChanged.connect(self.changed); self.slider.valueChanged.connect(self.changed); self.changed()
    @property
    def choice(self): return BackgroundChoice(self.mode.currentData(), self.color, self.image, self.slider.value() / 100)
    def changed(self, *_):
        self.percent.setText(f"{self.slider.value()}%")
        picture = self.mode.currentData() == "image"
        self.choose_button.setEnabled(picture); self.well.setEnabled(not picture)
        for button in (self.apply_button, self.apply_all_button): button.setEnabled(not picture or self.image is not None)
        self.preview.update()
    def set_color(self, color):
        self.color = color; self.well.setText(color); self.mode.setCurrentIndex(0); self.changed()
    def pick_color(self):
        color = QColorDialog.getColor(QColor(self.color), self)
        if color.isValid(): self.set_color(color.name().upper())
    def choose_picture(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Background picture", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.webp *.heic *.heif)")
        if filename:
            self.image = Path(filename); self.image_data = QImage(filename); self.file_label.setText(filename); self.changed()
    def apply_pressed(self, all_slides):
        choice = self.choice; self.accept(); self.apply_fn(choice, all_slides)


class BackgroundMixin:
    def format_background(self):
        if not self.scene_name: return
        target = self.scene_name
        def work():
            try:
                info = yield self.call("background", {"op": "get", "scene": target})
                if info.get("error"): raise RuntimeError(info["error"])
                self.background_sheet = BackgroundSheet(info, lambda c, all: self.apply_background(c, all, scene=target), lambda: self.run_background({"op": "reset"}, scene=target), self.window)
                self.background_sheet.open()
            except Exception as exc: self.toast(str(exc), error=True)
        return spawn(work())
    def apply_background(self, choice, all=False, scene=None):
        target = scene or self.scene_name
        req = {"op": "set", "scene": target, "mode": choice.mode, "color": choice.color, "transparency": choice.transparency, "all": all}
        if choice.mode != "image": return self.run_background(req, scene=target)
        if choice.image is None: return
        def work():
            relative = yield from asset_result(self.save_asset(choice.image, prefix="background_"))
            req["file"] = str(relative)
            r = yield self.call("background", req)
            if not r.get("ok"): self.toast(r.get("error", "Background failed"), error=True); return r
            if self.scene_name == target: yield from self.load_scene(target, keep_stop=True)
            yield from self.reload_state()
            return r
        return self.submit(work)
    def run_background(self, req, scene=None):
        return self.tool_command("background", dict(req, scene=scene or req.get("scene") or self.scene_name))

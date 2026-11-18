"""Solid/picture backgrounds with a non-mutating local preview."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QColorDialog, QDialog, QFileDialog,
                              QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                              QRadioButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from .slide_tools import asset_result
from .style import Theme, color_from_hex, hex_color, watch_appearance
from .tasks import spawn
from .widgets import add_copy_button, field_label, hairline


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


def _read_picture(path: Path | None) -> QImage:
    if path is None:
        return QImage()
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if not image.isNull() or path.suffix.lower() not in (".heic", ".heif"):
        return image
    # The asset import path already supports these through the app's Pillow HEIF opener.
    from PIL import Image, ImageOps
    from PIL.ImageQt import ImageQt
    try:
        with Image.open(path) as source:
            return ImageQt(ImageOps.exif_transpose(source)).copy()
    except OSError:
        return image


class _ColorChip(QPushButton):
    def __init__(self, color, parent, *, well=False):
        super().__init__(parent)
        self.color = color
        self.well = well
        self.selected = False
        self.setFixedSize(56 if well else 22, 26 if well else 22)
        self.setAutoDefault(False)
        self.setAccessibleName("Color" if well else hex_color(color))
        if not well:
            self.setToolTip(hex_color(color))

    def paintEvent(self, event):
        if self.well:
            super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(.35)
        elif self.isDown() and not self.well:
            painter.setOpacity(.7)
        stroke = 2.5 if self.selected else 1
        inset_x, inset_y = (6, 5) if self.well else (stroke / 2, stroke / 2)
        bounds = QRectF(self.rect()).adjusted(inset_x, inset_y, -inset_x, -inset_y)
        painter.setPen(QPen(Theme.accent if self.selected else Theme.separator(), stroke))
        painter.setBrush(self.color)
        painter.drawRoundedRect(bounds, 4, 4)
        if not self.well and self.hasFocus():
            painter.setPen(QPen(Theme.ink(.85), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bounds.adjusted(2, 2, -2, -2), 2, 2)


class _PictureName(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(Theme.ink(.9) if self.isEnabled() else Theme.tertiary_ink())
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, self.width())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


class _BackgroundPreview(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setMinimumHeight(self.heightForWidth(420))
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def sizeHint(self):
        return QSize(420, self.heightForWidth(420))

    def heightForWidth(self, width):
        return round(width * 9 / 16)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        owner = self.owner
        width = min(self.width(), self.height() * 16 / 9)
        bounds = QRectF(0, 0, width, width * 9 / 16)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 6, 6)
        painter.setClipPath(clip)
        painter.fillRect(bounds, owner.base)
        painter.setOpacity(1 - owner.slider.value() / 100)
        if owner.is_picture:
            if not owner.image_data.isNull():
                painter.drawImage(bounds, owner.image_data)
        else:
            painter.fillRect(bounds, owner.color)
        painter.setOpacity(1)
        painter.setClipping(False)
        painter.setPen(QPen(Theme.separator(), 1))
        painter.drawRoundedRect(bounds.adjusted(.5, .5, -.5, -.5), 6, 6)


class BackgroundSheet(QDialog):
    presets = ["#000000", "#1E1E1E", "#3C3C3C", "#808080", "#D9D9D9", "#FFFFFF", "#FDF6E3", "#E8E4D9", "#002B36", "#0C1B33", "#1F3A5F", "#2E5E8C", "#236B8E", "#58C4DD", "#83C167", "#FFFF00", "#FF862F", "#FC6255", "#9A72AC", "#7C5CFF"]
    def __init__(self, info, apply, reset, parent=None):
        super().__init__(parent)
        self.info = BackgroundInfo.from_reply(info) if isinstance(info, dict) else info
        self.apply_fn, self.reset_fn = apply, reset
        self.image = Path(self.info.image) if self.info.image else None
        self.image_data = _read_picture(self.image)
        self.base = color_from_hex(self.info.default, "#000000")
        self.color = color_from_hex(self.info.color, self.base.name())
        self.setWindowTitle("Format Background")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFont(Theme.font(13))
        self.resize(460, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        heading = QLabel("Format Background", self)
        heading.setFont(Theme.font(13, QFont.Weight.DemiBold))
        layout.addWidget(heading)

        self.solid = QRadioButton("Solid color", self)
        self.picture = QRadioButton("Picture", self)
        mode = QButtonGroup(self)
        mode.addButton(self.solid)
        mode.addButton(self.picture)
        (self.picture if self.info.mode == "image" else self.solid).setChecked(True)
        fill_row = self._field_row("Fill", spacing=14)
        fill_row.addWidget(self.solid)
        fill_row.addWidget(self.picture)
        fill_row.addStretch()
        layout.addLayout(fill_row)

        color_row = self._field_row("Color")
        self.well = _ColorChip(self.color, self, well=True)
        self.well.clicked.connect(self.pick_color)
        color_row.addWidget(self.well)
        grid = QGridLayout()
        grid.setSpacing(5)
        self.swatches = []
        for index, color in enumerate(self.presets):
            chip = _ColorChip(QColor(color), self)
            chip.clicked.connect(lambda _, chip=chip: self.set_color(chip.color))
            grid.addWidget(chip, index // 10, index % 10)
            self.swatches.append(chip)
        color_row.addLayout(grid)
        color_row.addStretch()
        layout.addLayout(color_row)

        picture_row = self._field_row("Picture")
        self.choose_button = QPushButton("Choose Picture…", self)
        self.choose_button.setAutoDefault(False)
        self.choose_button.clicked.connect(self.choose_picture)
        picture_row.addWidget(self.choose_button)
        self.file_label = _PictureName(self)
        self.file_label.setText(self.image.name if self.image else "No picture chosen")
        picture_row.addWidget(self.file_label, 1)
        layout.addLayout(picture_row)

        self.preview = _BackgroundPreview(self)
        layout.addWidget(self.preview)
        transparency = self._field_row("Transparency")
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setValue(int((self.info.transparency or 0) * 100 + .5))
        self.slider.setAccessibleName("Transparency")
        transparency.addWidget(self.slider, 1)
        self.percent = QLabel(self)
        self.percent.setFont(Theme.tabular(12))
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.percent.setFixedWidth(max(40, self.percent.fontMetrics().horizontalAdvance("100%")))
        transparency.addWidget(self.percent)
        layout.addLayout(transparency)
        layout.addStretch()
        layout.addSpacing(6)
        layout.addWidget(hairline(self))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        reset_button = QPushButton("Reset Background", self)
        reset_button.clicked.connect(self.reset_pressed)
        self.apply_all_button = QPushButton("Apply to All", self)
        self.apply_all_button.clicked.connect(lambda: self.apply_pressed(True))
        cancel_button = QPushButton("Cancel", self)
        cancel_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("Apply", self)
        self.apply_button.clicked.connect(lambda: self.apply_pressed(False))
        self.apply_button.setDefault(True)
        for button in (reset_button, self.apply_all_button, cancel_button):
            button.setAutoDefault(False)
        buttons.addWidget(reset_button)
        buttons.addWidget(self.apply_all_button)
        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self.picture.toggled.connect(self.changed)
        self.slider.valueChanged.connect(self.changed)
        watch_appearance(self.changed)
        self.changed()

    def _field_row(self, title, *, spacing=10):
        row = QHBoxLayout()
        row.setSpacing(spacing)
        label = field_label(title, self)
        label.setFixedWidth(max(84, label.fontMetrics().horizontalAdvance(title)))
        row.addWidget(label)
        return row

    @property
    def is_picture(self):
        return self.picture.isChecked()

    @property
    def choice(self):
        picture = self.is_picture
        return BackgroundChoice("image" if picture else "color", hex_color(self.color),
                                self.image if picture else None, self.slider.value() / 100)

    def changed(self, *_):
        self.percent.setText(f"{self.slider.value()}%")
        picture = self.is_picture
        self.choose_button.setEnabled(picture)
        self.file_label.setEnabled(picture)
        self.file_label.update()
        self.well.setEnabled(not picture)
        self.well.update()
        for chip in self.swatches:
            chip.setEnabled(not picture)
            chip.selected = not picture and chip.color == self.color
            chip.update()
        for button in (self.apply_button, self.apply_all_button):
            button.setEnabled(not picture or self.image is not None)
        self.preview.update()

    def set_color(self, color):
        self.color = self.well.color = color
        self.changed()

    def pick_color(self):
        previous = self.color
        dialog = QColorDialog(previous, self)
        dialog.currentColorChanged.connect(self.set_color)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_color(dialog.selectedColor())
        else:
            self.set_color(previous)
        dialog.deleteLater()

    def choose_picture(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose a background picture", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.gif *.webp *.heic *.heif)")
        if filename:
            self.image = Path(filename)
            self.image_data = _read_picture(self.image)
            self.file_label.setText(self.image.name)
            self.changed()

    def apply_pressed(self, all_slides):
        choice, apply = self.choice, self.apply_fn
        if choice.mode == "image" and choice.image is None:
            return
        self.accept()
        apply(choice, all_slides)

    def reset_pressed(self):
        reset = self.reset_fn
        self.accept()
        reset()


class BackgroundMixin:
    def format_background(self):
        if not self.scene_name or self.player is not None:
            return
        target = self.scene_name

        def work():
            try:
                info = yield self.call("background", {"op": "get", "scene": target})
                if info.get("error"):
                    raise RuntimeError(info["error"])
            except Exception as exc:
                self.toast(f"Could not read the background: {exc}", error=True)
                return
            sheet = BackgroundSheet(
                info, lambda choice, all_slides: self.apply_background(choice, all_slides, scene=target),
                lambda: self.run_background({"op": "reset"}, scene=target), self.window)
            self.background_sheet = sheet
            sheet.finished.connect(lambda _: setattr(self, "background_sheet", None))
            sheet.open()

        return spawn(work())

    def apply_background(self, choice, all=False, scene=None):
        req = {"op": "set", "mode": choice.mode, "color": choice.color,
               "transparency": choice.transparency, "all": all}
        if choice.mode != "image" or choice.image is not None:
            return self.run_background(req, scene=scene, picture=choice.image)

    def run_background(self, req, scene=None, *, picture=None):
        target = scene or req.get("scene") or self.scene_name
        req = dict(req, scene=target)

        def work():
            if picture is not None:
                try:
                    req["file"] = str((yield from asset_result(self.save_asset(picture, prefix="background_"))))
                except Exception as exc:
                    title = "Could not copy the picture"
                    box = QMessageBox(QMessageBox.Icon.Warning, title, title,
                                      QMessageBox.StandardButton.Ok, self.window)
                    box.setTextFormat(Qt.TextFormat.PlainText)
                    box.setInformativeText(str(exc))
                    add_copy_button(box)
                    box.exec()
                    return
            self.busy += 1
            try:
                result = yield self.call("background", req)
            except Exception as exc:
                self.toast(str(exc), error=True)
                return
            finally:
                self.busy -= 1
            if not result.get("ok"):
                self.toast(result.get("error") or "background change failed", error=True)
                return result
            self.last_changed = result.get("changed")
            message = result.get("message") or ""
            self.toast("No change" if message == "no change" else f"Saved · {message}")
            yield from self.reload_state()
            if self.scene_name == target:
                yield from self.load_scene(target, keep_stop=True)
            return result

        return self.submit(work)

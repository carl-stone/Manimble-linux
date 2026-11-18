"""LaTeX sheet using the engine's real compiler and generation-checked previews."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                              QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .style import Theme
from .tasks import spawn
from .widgets import field_label


class _LatexPreview(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.image = QPixmap()
        self.error = QLabel(self)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.error.setFont(Theme.mono(11))
        self.error.setStyleSheet("color: #ff453a")  # legible red on the deliberately black preview
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 10)
        layout.addStretch()
        layout.addWidget(self.error)
        self.error.hide()

    def set_result(self, image=None, error=""):
        if image is not None:
            self.image = image
        self.error.setText(error)
        self.error.setVisible(bool(error))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.fillPath(clip, Qt.GlobalColor.black)
        if not self.image.isNull():
            available = bounds.adjusted(12, 12, -12, -12)
            size = self.image.deviceIndependentSize()
            if size.width() > available.width() or size.height() > available.height():
                size = size.scaled(available.size(), Qt.AspectRatioMode.KeepAspectRatio)
            rect = QRectF(available.topLeft(), size)
            rect.moveCenter(available.center())
            painter.setClipPath(clip)
            painter.drawPixmap(rect, self.image, QRectF(self.image.rect()))


class LatexSheet(QDialog):
    def __init__(self, initial, shared, render, apply, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit formula")
        self.resize(640, 470)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFont(Theme.font(13))
        self.render_fn, self.apply_fn = render, apply
        self.generation = 0
        self.text = QPlainTextEdit(initial)
        self.text.setFont(Theme.mono(14))
        self.text.setFixedHeight(round(Theme.logical_size(110)))
        self.preview = _LatexPreview(self)
        self.shared_box = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        hint = field_label("LaTeX  ·  Ctrl+Return to apply", self)
        hint.setFont(Theme.font(13))
        layout.addWidget(hint)
        layout.addWidget(self.text)
        layout.addWidget(self.preview, 1)
        if shared:
            scope = (f"Change everywhere ({shared.get('name')} in {shared.get('rel')}, "
                     f"used {shared.get('uses')}×). Untick for this slide only.")
            self.shared_box = QCheckBox(scope.replace("&", "&&"), self)
            self.shared_box.setChecked(True)
            layout.addWidget(self.shared_box)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        for title, action in (("Cancel", self.reject), ("Apply", self.apply_pressed)):
            button = QPushButton(title, self)
            button.setAutoDefault(False)
            button.clicked.connect(action)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.apply_pressed)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.load_preview)
        self.text.textChanged.connect(self.refresh)
        self.finished.connect(lambda _: self.cancel_preview())
        self.refresh()

    def cancel_preview(self):
        self.generation += 1
        self.timer.stop()

    def refresh(self):
        self.generation += 1
        self.timer.start()

    def load_preview(self):
        generation, text = self.generation, self.text.toPlainText()

        def work():
            if generation != self.generation:
                return
            try:
                result = yield self.render_fn(text)
                path, error = result.get("path"), result.get("error")
            except Exception as exc:
                path, error = None, str(exc)
            if generation != self.generation:
                return
            if path and error is None:
                image = QPixmap(path)
                self.preview.set_result(image, f"Cannot read preview: {path}" if image.isNull() else "")
            else:
                detail = "LaTeX error" if error is None else str(error)
                self.preview.set_result(error="\n".join(detail.split("\n")[-4:]))

        spawn(work())

    def apply_pressed(self):
        text = self.text.toPlainText()
        shared = self.shared_box is None or self.shared_box.isChecked()
        apply = self.apply_fn
        self.accept()
        apply(text, shared)


class FormulaMixin:
    def _formula_render(self, tex):
        return self.call("sprite", {"kind": "tex", "value": tex, "font_size": 40.0, "color": "#FFFFFF"})
    def open_latex(self, oid):
        obj = (self.scene or {}).get("objects", {}).get(str(oid), {})
        if obj.get("kind") != "tex":
            return
        if not obj.get("textOk"):
            self.toast(f"This formula is generated by code: {obj.get('textNote') or ''}", error=True)
            return
        context = self.edit_context
        sheet = LatexSheet(obj.get("value", ""), obj.get("shared"), self._formula_render,
                           lambda tex, shared: self.commit_text(oid, tex, shared=shared, slide=context.scene, seen=context.seen), self.window)
        self._show_latex_sheet(sheet)
    def edit_latex_pressed(self):
        if self.selection is not None: return self.open_latex(self.selection)
    def insert_formula_pressed(self):
        if not self.scene_name: return
        context = self.edit_context
        def apply(tex, _):
            if self.scene_name != context.scene or self.stop_index != context.stop:
                self.toast("The slide changed; choose Insert Formula again", error=True); return
            self.insert("formula", {"tex": tex})
        self._show_latex_sheet(LatexSheet("x^2 + y^2 = z^2", None, self._formula_render, apply, self.window))

    def _show_latex_sheet(self, sheet):
        self.latex_sheet = sheet
        sheet.finished.connect(lambda _: setattr(self, "latex_sheet", None))
        sheet.open()

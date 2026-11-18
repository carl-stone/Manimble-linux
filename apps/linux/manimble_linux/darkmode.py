"""Visual-only colour mapping, shared by canvas, thumbnails and presentation."""
from __future__ import annotations

from enum import Enum


class ColorView(str, Enum):
    normal = "normal"
    dark = "dark"
    invert = "invert"


class DarkMode:
    lo = 0.12
    hi = 0.28
    mode = ColorView.normal
    images = True

    @staticmethod
    def map(r, g=None, b=None, m=None):
        """Map straight (not premultiplied) sRGB; retain a QColor's alpha."""
        mode = ColorView(m if m is not None else DarkMode.mode)
        if g is None:
            from PySide6.QtGui import QColor
            c = r
            red, green, blue = DarkMode.map(c.redF(), c.greenF(), c.blueF(), mode)
            return QColor.fromRgbF(red, green, blue, c.alphaF())
        if mode == ColorView.normal:
            return r, g, b
        if mode == ColorView.invert:
            return 1 - r, 1 - g, 1 - b
        mx, mn = max(r, g, b), min(r, g, b)
        t = min(1, max(0, (mx - mn - DarkMode.lo) / (DarkMode.hi - DarkMode.lo)))
        k = (1 - t * t * (3 - 2 * t)) * (1 - mx - mn)
        return r + k, g + k, b + k

    @staticmethod
    def apply(img, mode=None):
        """Unpremultiply before the nonlinear map; return owned premultiplied pixels."""
        from PySide6.QtGui import QImage
        import numpy as np
        mode = ColorView(mode if mode is not None else DarkMode.mode)
        if mode == ColorView.normal:
            return img
        straight = img.convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = np.frombuffer(straight.constBits(), np.uint8).reshape(straight.height(), straight.bytesPerLine())
        rgba = pixels[:, :straight.width() * 4].reshape(straight.height(), straight.width(), 4).copy()
        rgb = rgba[:, :, :3].astype(np.float32) / 255
        if mode == ColorView.invert:
            rgb = 1 - rgb
        else:
            mx, mn = rgb.max(axis=2), rgb.min(axis=2)
            t = np.clip((mx - mn - DarkMode.lo) / (DarkMode.hi - DarkMode.lo), 0, 1)
            rgb += ((1 - t * t * (3 - 2 * t)) * (1 - mx - mn))[:, :, None]
        rgba[:, :, :3] = np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        out = QImage(rgba.data, straight.width(), straight.height(), rgba.strides[0], QImage.Format.Format_RGBA8888).copy()
        return out.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    @staticmethod
    def cg_image(path):
        from PySide6.QtGui import QImage
        image = QImage(str(path))
        return None if image.isNull() else DarkMode.apply(image)

    image = cg_image


class DarkModeMixin:
    def init_dark_mode(self):
        from . import prefs
        saved = prefs.get_str("colorView", "dark" if prefs.get_bool("darkMode") else "normal")
        DarkMode.mode = ColorView(saved) if saved in ColorView._value2member_map_ else ColorView.normal
        DarkMode.images = prefs.get_bool("darkModeImages", True)
        self.canvas.color_view = DarkMode.mode
        self.canvas.dark_images = DarkMode.images

    def toggle_dark_mode(self):
        self.set_color_view(ColorView.normal if DarkMode.mode == ColorView.dark else ColorView.dark)

    def toggle_invert_colors(self):
        self.set_color_view(ColorView.normal if DarkMode.mode == ColorView.invert else ColorView.invert)

    def toggle_dark_images(self):
        from . import prefs
        DarkMode.images = not DarkMode.images
        prefs.set("darkModeImages", DarkMode.images)
        self.canvas.dark_images = DarkMode.images

    def set_color_view(self, m):
        from . import prefs
        DarkMode.mode = ColorView(m)
        prefs.set("colorView", DarkMode.mode.value)
        self.canvas.color_view = DarkMode.mode
        # Navigator thumbnails use the same mapper, so invalidate their paint cache too.
        self.table.viewport().update()
        self.rebuild_steps()

    def apply_dark_mode(self):
        self.confirm_apply(ColorView.dark)

    def apply_invert_colors(self):
        self.confirm_apply(ColorView.invert)

    def confirm_apply(self, m):
        from PySide6.QtWidgets import QMessageBox
        title = "Apply dark mode to the deck?" if m == ColorView.dark else "Invert the deck's colours?"
        detail = ("Near-white and near-black colours are swapped; saturated colours and images are unchanged."
                  if m == ColorView.dark else "Every source colour becomes its complement. Images are unchanged.")
        box = QMessageBox(QMessageBox.Icon.Question, title,
                          detail + " Every slide is re-rendered. Undo reverts this in one step.", parent=self.window)
        apply = box.addButton("Apply", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() == apply:
            self.run_apply_colors(m)

    def run_apply_colors(self, m):
        def work():
            self.busy += 1
            self.busy_text = "Applying dark mode…" if m == ColorView.dark else "Inverting colours…"
            try:
                result = yield self.call("darkmode", {"current": self.scene_name or "", "mode": m.value})
                if not result.get("ok", False):
                    raise RuntimeError(result.get("error", "Colour change failed"))
                if DarkMode.mode == m:
                    self.set_color_view(ColorView.normal)
                self.toast("No colours to change" if result.get("message") == "no change" else "Saved · " + result.get("message", ""))
            except Exception as error:
                self.toast(str(error), error=True)
            finally:
                self.busy -= 1
                self.busy_text = "Rendering…"
            self.canvas.drop_textures()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
            yield from self.reload_state()
        self.submit(work)

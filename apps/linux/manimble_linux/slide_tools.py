"""Slide, insertion, arrangement and link tools; all mutations enter the controller queue."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog, QLineEdit,
    QMessageBox, QPushButton, QSpinBox)

from . import prefs
from .tasks import spawn


def asset_result(value):
    """Accept the core asset helper's asynchronous result without blocking Qt."""
    if inspect.isgenerator(value):
        return (yield from value)
    if hasattr(value, "add_done_callback"):
        return (yield value)
    return value


class SlideToolsMixin:
    slide_layouts = [("Title Slide", "title", ""), ("Title and Content", "title_content", "n"),
                     ("Two Content", "two_content", ""), ("Title Only", "title_only", ""), ("Blank", "blank", "")]
    layout_boxes = {"title": [(1.667, 1.227, 10, 2.61), (1.667, 3.938, 10, 1.81)],
                    "title_content": [(0.917, 0.399, 11.5, 1.45), (0.917, 1.996, 11.5, 4.76)],
                    "two_content": [(0.917, 0.399, 11.5, 1.45), (0.917, 1.996, 5.667, 4.76), (6.75, 1.996, 5.667, 4.76)],
                    "title_only": [(0.917, 0.399, 11.5, 1.45)], "blank": []}
    pen_swatches = [("Red", "#FF3B30"), ("Orange", "#FF9A2E"), ("Yellow", "#FFD400"),
                    ("Green", "#34C759"), ("Blue", "#4D7CFF"), ("Purple", "#7C5CFF"), ("White", "#FFFFFF"), ("Black", "#000000")]
    page_spread_key = "pageNumberSpread"
    page_spread_choices = [("Ask", ""), ("Apply to all slides", "all"), ("Only that slide", "one")]

    def tool_command(self, cmd, args, *, select=False):
        """Queue non-object mutations with an immutable target; return their real reply."""
        req = dict(args)
        target = req.get("scene", self.scene_name)
        def work():
            result = yield self.call(cmd, req)
            if result.get("ok") is False or result.get("error"):
                self.toast(result.get("error", f"{cmd} failed"), error=True)
                return result
            self.last_changed = result.get("changed")
            yield from self.reload_state()
            selected = result.get("select") if select else None
            names = [s["name"] for s in (self.state or {}).get("scenes", [])]
            if select:
                selected = selected if selected in names else (names[0] if names else None)
                if selected:
                    yield from self.select_scene(selected)
                else:
                    self.scene_name = None
                    self.scene = None
                    self.selection = None
                    self.canvas.stop = None
                    self.canvas.update()
                    self.rebuild_inspector()
            elif target and self.scene_name == target:
                if "stop" in result:
                    self.stop_index = result["stop"]
                yield from self.load_scene(target, keep_stop=True)
            self.toast(result.get("message", "Saved"))
            return result
        return self.submit(work)

    def insert(self, kind, extra=None, on_fail=None, *, context=None):
        context = context or self.edit_context
        if not context.scene:
            return None
        req = dict(extra or {}, kind=kind, scene=context.scene, stop=context.stop)
        if context.seen:
            req["seen"] = context.seen
        generation = getattr(self, "_insert_generation", 0) + 1
        self._insert_generation = generation
        epoch = (self._connection_epoch, self._deck_epoch)
        def preview():
            try:
                r = yield self.call("insert_sprite", req)
                current_epoch = (self._connection_epoch, self._deck_epoch)
                if epoch == current_epoch and self._insert_generation == generation and self.scene_name == context.scene and r.get("path"):
                    image = QImage(r["path"])
                    if not image.isNull():
                        width, height = (self.scene or {}).get("size", [1280, 720])
                        self.canvas.ghost = (image, width / 2 + r.get("cx", 0), height / 2 + r.get("cy", 0), r.get("w", image.width()), r.get("h", image.height()))
                        self.canvas.update()
            except Exception as exc:
                if self._insert_generation == generation:
                    self.toast(str(exc), error=True)
        if kind != "pen":
            spawn(preview())
        def work():
            try:
                r = yield self.call("insert", req)
                if not r.get("ok"):
                    if on_fail:
                        on_fail()
                    else:
                        self.toast(r.get("error", "Insert failed"), error=True)
                    return r
                yield from self.load_scene(context.scene, keep_stop=True)
                yield from self.reload_state()
                if self.scene_name == context.scene:
                    loc = f"{r.get('file', '')}:{r.get('line', 0)}"
                    for oid, obj in (self.scene or {}).get("objects", {}).items():
                        if obj.get("loc") == loc:
                            self.selection = int(oid)
                            if kind == "text" and "value" not in req:
                                self.begin_edit(int(oid))
                            break
                self.toast(f"Inserted {kind}")
                return r
            finally:
                if self._insert_generation == generation:
                    self._insert_generation += 1
                    self.canvas.ghost = None
                    self.canvas.update()
        return self.submit(work)

    def insert_text_pressed(self):
        return self.insert("text")

    def insert_shape_pressed(self, kind="rectangle"):
        return self.insert(kind if isinstance(kind, str) else kind.data())

    def insert_table_pressed(self, style="word"):
        if not isinstance(style, str):
            style = style.data() if hasattr(style, "data") else "word"
        dialog = QDialog(self.window)
        dialog.setWindowTitle(f"Insert {style} table")
        form = QFormLayout(dialog)
        rows, cols = QSpinBox(), QSpinBox()
        for field in (rows, cols):
            field.setRange(1, 999)
            field.setValue(3)
        form.addRow("Rows", rows)
        form.addRow("Columns", cols)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return self.insert("table", {"rows": rows.value(), "cols": cols.value(), "style": style})

    def insert_image_pressed(self):
        filename, _ = QFileDialog.getOpenFileName(self.window, "Insert picture", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.gif *.bmp *.webp *.heic *.heif *.svg)")
        if not filename:
            return
        context = self.edit_context
        def work():
            relative = yield from asset_result(self.save_asset(Path(filename)))
            return (yield self.insert("image", {"file": str(relative), "path": str(self.work_deck / relative)}, context=context))
        return spawn(work()).future

    def slide_op(self, op, index=None, scene_override=None, layout=None):
        name = scene_override or self.scene_name
        if not name:
            return
        req = {"op": op, "scene": name}
        if index is not None:
            req["index"] = index
        if layout:
            req["layout"] = layout
        return self.tool_command("slide", req, select=True)

    def new_slide(self): return self.slide_op("new", layout="title_content")
    def new_slide_layout(self, layout): return self.slide_op("new", layout=layout if isinstance(layout, str) else layout.data())
    def duplicate_slide(self): return self.slide_op("duplicate")
    def delete_slide(self): return self.slide_op("delete")
    def restore_slide(self): return self.slide_op("restore")
    def move_slide_up(self): return self.slide_op("up")
    def move_slide_down(self): return self.slide_op("down")

    @property
    def selected_slide_is_hidden(self):
        return any(s.get("name") == self.scene_name and s.get("hidden") for s in (self.state or {}).get("scenes", []))

    def toggle_hide_slide(self): return self.slide_op("unhide" if self.selected_slide_is_hidden else "hide")
    def new_build(self):
        if self.scene_name:
            return self.tool_command("new_build", {"scene": self.scene_name, "stop": self.stop_index})
    def delete_build(self):
        if self.scene_name and len((self.scene or {}).get("stops", [])) > 1:
            return self.tool_command("delete_build", {"scene": self.scene_name, "stop": self.stop_index})

    def set_placeholder(self, line, text, color=None):
        req = {"scene": self.scene_name, "line": line, "text": text}
        if color:
            req["color"] = color
        return self.tool_command("placeholder", req)

    def run_page_numbers(self, args, scene=None):
        return self.tool_command("page_numbers", dict(args, scene=scene or self.scene_name))

    def page_spread_picked(self, choice):
        value = choice.currentData() if hasattr(choice, "currentData") else choice
        prefs.set(self.page_spread_key, value) if value else prefs.remove(self.page_spread_key)

    def offer_page_number_spread(self, scene):
        choice = prefs.get_str(self.page_spread_key)
        if choice == "one":
            return
        if choice == "all":
            return self.run_page_numbers({"op": "spread"}, scene)
        box = QMessageBox(self.window)
        box.setWindowTitle("Apply to all slides?")
        box.setText("Give every slide's number the same position and style?")
        all_button = box.addButton("Apply to All Slides", QMessageBox.ButtonRole.AcceptRole)
        one_button = box.addButton("Only This Slide", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(one_button)
        remember = QCheckBox("Don't ask again")
        box.setCheckBox(remember)
        box.exec()
        all_slides = box.clickedButton() is all_button
        if remember.isChecked():
            prefs.set(self.page_spread_key, "all" if all_slides else "one")
        if all_slides:
            return self.run_page_numbers({"op": "spread"}, scene)

    def slide_numbers_pressed(self):
        if not self.scene_name:
            return
        target = self.scene_name
        def show():
            info = yield self.call("page_numbers", {"op": "get"})
            dialog = QDialog(self.window)
            dialog.setWindowTitle("Slide numbers")
            form = QFormLayout(dialog)
            pos = QComboBox()
            for title, key in [("Bottom right", "DR"), ("Bottom center", "DOWN"), ("Bottom left", "DL")]: pos.addItem(title, key)
            pos.setCurrentIndex(max(0, pos.findData(info.get("pos", "DR"))))
            start, size = QSpinBox(), QSpinBox()
            start.setRange(-999999, 999999); start.setValue(info.get("start", 1))
            size.setRange(6, 200); size.setValue(info.get("size", 24))
            form.addRow("Position", pos); form.addRow("Start at", start); form.addRow("Font size", size)
            checks = {}
            for key, title, default in [("skip", "Skip title slide", True), ("total", "Show total slides", True), ("sub", "Show sub-slide numbers", False)]:
                checks[key] = QCheckBox(title); checks[key].setChecked(info.get(key, default)); form.addRow(checks[key])
            spread = QComboBox()
            for title, value in self.page_spread_choices: spread.addItem(title, value)
            spread.setCurrentIndex(max(0, spread.findData(prefs.get_str(self.page_spread_key))))
            spread.currentIndexChanged.connect(lambda _: self.page_spread_picked(spread))
            form.addRow("When one is changed", spread)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Update Numbers" if info.get("on") else "Add Numbers")
            buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(lambda: (self.run_page_numbers({"op": "set", "pos": pos.currentData(), "start": start.value(), "size": size.value(), **{k: c.isChecked() for k, c in checks.items()}}, target), dialog.accept()))
            buttons.rejected.connect(dialog.reject)
            if info.get("on"):
                remove = buttons.addButton("Remove Numbers", QDialogButtonBox.ButtonRole.DestructiveRole)
                remove.clicked.connect(lambda: (self.run_page_numbers({"op": "remove"}, target), dialog.accept()))
            form.addRow(buttons)
            self._page_numbers_dialog = dialog
            dialog.open()
        return spawn(show())

    def set_pen_color(self, color):
        self.canvas.pen_color = QColor(color)
        prefs.set("penColor", self.canvas.pen_color.name().upper())
        self.set_draw_tool("pen")
    def pen_color_chosen(self, color): self.set_pen_color(color if isinstance(color, str) else color.data())
    def pen_color_panel(self):
        c = QColorDialog.getColor(getattr(self.canvas, "pen_color", QColor("#FF3B30")), self.window)
        if c.isValid(): self.set_pen_color(c)
    def pen_panel_changed(self, color): self.set_pen_color(color)
    def pen_finished(self, points):
        return self.insert("pen", {"points": [[p.x(), p.y()] if hasattr(p, "x") else list(p) for p in points], "color": self.canvas.pen_color.name().upper(), "width": getattr(self.canvas, "pen_width", 6)})

    def save_presenter_notes(self, scene, build, text, done):
        req = {"op": "set", "scene": scene, "text": text}
        if build is not None: req["stop"] = build
        future = self.tool_command("notes", req)
        from .tasks import when_done
        when_done(future, lambda f: done(not f.exception() and f.result().get("ok", False)))
        return future


class ArrangeMixin:
    @property
    def selection_is_grouped(self):
        return any((self.scene or {}).get("objects", {}).get(str(i), {}).get("group") is not None for i in self.canvas.selected_ids)

    def group_objects(self):
        ids = list(self.canvas.selected_ids)
        if len(ids) > 1: return self.submit(lambda: self.run_edit({"op": "group", "oid": ids[0], "oids": ids}))
    def ungroup_objects(self):
        ids = list(self.canvas.selected_ids)
        if ids: return self.submit(lambda: self.run_edit({"op": "ungroup", "oid": ids[0], "oids": ids}))
    def arrange(self, how):
        ids = list(self.canvas.selected_ids)
        if ids: return self.submit(lambda: self.run_edit({"op": "order", "oid": ids[0], "oids": ids, "value": how}))
    def bring_to_front(self): return self.arrange("front")
    def bring_forward(self): return self.arrange("forward")
    def send_backward(self): return self.arrange("backward")
    def send_to_back(self): return self.arrange("back")
    def delete_selected(self, oid=None):
        ids = [oid] if oid is not None else sorted(self.canvas.selected_ids, reverse=True)
        def body():
            for item in ids:
                self.submit(lambda item=item: self.run_edit({"op": "delete", "oid": item}))
        if len(ids) > 1: self.as_one_undo_step(f"delete {len(ids)} objects", body)
        else: body()


class HyperlinkMixin:
    @staticmethod
    def normalized_link(value):
        text = value.strip()
        if not text or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text): return text
        if text.startswith(("/", "~")): return Path(text).expanduser().absolute().as_uri()
        return "https://" + text

    def commit_link(self, value, oid=None):
        oid = self.selection if oid is None else oid
        if oid is not None:
            req = {"op": "link", "oid": oid, "url": self.normalized_link(value)}
            return self.submit(lambda: self.run_edit(req))

    def hyperlink_pressed(self):
        obj = self.obj_info or {}
        if obj.get("kind") not in ("text", "tex"): return
        oid = self.selection
        current = (obj.get("format") or {}).get("link", "")
        text, ok = QInputDialog.getText(self.window, "Edit Hyperlink" if current else "Add Hyperlink", "URL (empty removes the link)", text=current)
        if ok and self.normalized_link(text) != current: return self.commit_link(text, oid)

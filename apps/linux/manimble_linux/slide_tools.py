"""Slide, insertion, arrangement and link tools; all mutations enter the controller queue."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QSpinBox, QVBoxLayout)

from . import prefs
from .style import Theme
from .tasks import spawn, when_done
from .widgets import add_copy_button


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
        failure, success = {
            "slide": ("slide operation failed", req.get("op")),
            "new_build": ("could not add a build", "added a build"),
            "delete_build": ("could not delete the build", "deleted build"),
            "page_numbers": ("could not write the slide numbers", None),
        }.get(cmd, (f"{cmd} failed", None))
        show_busy = bool(success) or cmd == "page_numbers"

        def work():
            if show_busy:
                self.busy += 1
            try:
                result = yield self.call(cmd, req)
            finally:
                if show_busy:
                    self.busy -= 1
            if result.get("ok") is False or result.get("error"):
                self.toast(result.get("error") or failure, error=True)
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
                    self._clear_shown_scene()
                    self.render_source()
                    self.rebuild_steps()
                    self.rebuild_inspector()
            elif target and self.scene_name == target:
                if "stop" in result:
                    self.stop_index = result["stop"]
                yield from self.load_scene(target, keep_stop=True)
            text = result.get("message") or success or "Saved"
            if success:
                text = text.title()
                if cmd in ("new_build", "delete_build") or req.get("op") == "delete":
                    text += f" — {self.actions.shortcut_text('undo')} to undo"
            elif cmd == "page_numbers":
                message = result.get("message")
                text = "No change" if message == "no change" else f"Saved · {message or 'slide numbers'}"
            self.toast(text)
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
            except Exception:
                # The provisional sprite is optional; only the actual insert reports failure.
                return
        if kind != "pen":
            spawn(preview())
        def work():
            try:
                self.busy += 1
                try:
                    r = yield self.call("insert", req)
                finally:
                    self.busy -= 1
                if not r.get("ok"):
                    if on_fail:
                        on_fail()
                    else:
                        self.toast(r.get("error") or "insert failed", error=True)
                    return r
                self.last_changed = r.get("changed")
                yield from self.load_scene(context.scene, keep_stop=True)
                yield from self.reload_state()
                if kind != "pen" and self.scene_name == context.scene:
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
        context, epoch = self.edit_context, self._deck_epoch
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Insert math table" if style == "math" else "Insert word table")
        layout = QVBoxLayout(dialog)
        margins = layout.contentsMargins()
        dialog.setMinimumWidth(300 + margins.left() + margins.right())
        prompt = QLabel("Rows and columns:")
        prompt.setFont(Theme.font(13))
        layout.addWidget(prompt)
        fields = QHBoxLayout()
        fields.setSpacing(8)
        rows, cols = QSpinBox(), QSpinBox()
        for name, field in (("Rows", rows), ("Columns", cols)):
            field.setRange(1, 999)
            field.setValue(3)
            field.setFixedWidth(60)
            field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            label = QLabel(name)
            label.setBuddy(field)
            fields.addWidget(label)
            fields.addWidget(field)
        fields.addStretch()
        layout.addLayout(fields)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        insert = buttons.button(QDialogButtonBox.StandardButton.Ok)
        insert.setText("Insert")
        insert.setDefault(True)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted and self._deck_epoch == epoch:
            return self.insert("table", {"rows": rows.value(), "cols": cols.value(), "style": style}, context=context)

    def insert_image_pressed(self):
        if not self.workspace:
            return
        epoch = self._deck_epoch
        filename, _ = QFileDialog.getOpenFileName(
            self.window, "Choose an image to insert", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.gif *.bmp *.webp *.heic *.heif *.svg)",
        )
        if not filename or self._deck_epoch != epoch:
            return
        context = self.edit_context
        deck = self.work_deck

        def work():
            try:
                relative = yield from asset_result(self.save_asset(Path(filename)))
            except Exception as exc:
                if self._deck_epoch == epoch:
                    title = "Could not copy the image"
                    box = QMessageBox(QMessageBox.Icon.Warning, "", title, QMessageBox.StandardButton.Ok, self.window)
                    box.setTextFormat(Qt.TextFormat.PlainText)
                    box.setInformativeText(str(exc))
                    add_copy_button(box).setToolTip("Copy this message to the clipboard")
                    box.exec()
                return
            if self._deck_epoch == epoch:
                return (yield self.insert(
                    "image", {"file": str(relative), "path": str(deck / relative)}, context=context,
                ))
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
            self.commit_drawer()
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
        if not (scene or self.scene_name):
            return
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
        box.setText("You changed this slide's number. Give every slide's number the same position and style?")
        all_button = box.addButton("Apply to All Slides", QMessageBox.ButtonRole.AcceptRole)
        one_button = box.addButton("Only This Slide", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(one_button)
        remember = QCheckBox("Don't ask again")
        box.setCheckBox(remember)
        box.exec()
        all_slides = box.clickedButton() is all_button
        if remember.isChecked():
            prefs.set(self.page_spread_key, "all" if all_slides else "one")
            self.toast("Won't ask again · change this in Insert ▸ Slide Numbers…")
        if all_slides:
            return self.run_page_numbers({"op": "spread"}, scene)

    def slide_numbers_pressed(self):
        if not self.scene_name:
            return
        target = self.scene_name
        epoch = self._deck_epoch
        def show():
            info = yield self.call("page_numbers", {"op": "get"})
            if self._deck_epoch != epoch:
                return
            dialog = QDialog(self.window)
            dialog.setWindowTitle("Slide numbers")
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            layout = QVBoxLayout(dialog)
            prompt = QLabel(
                "Every slide of the show gets a number: a Text at the top of its construct(). "
                "The numbers follow along when slides are added, deleted or reordered."
            )
            prompt.setWordWrap(True)
            prompt.setFont(Theme.font(13))
            layout.addWidget(prompt)
            fields = QGridLayout()
            fields.setVerticalSpacing(7)
            fields.setHorizontalSpacing(10)
            fields.setColumnStretch(1, 1)
            dialog.setMinimumWidth(300 + layout.contentsMargins().left() + layout.contentsMargins().right())
            layout.addLayout(fields)

            def add_field(row, caption, field):
                label = QLabel(caption)
                label.setFont(Theme.font(11))
                label.setBuddy(field)
                fields.addWidget(label, row, 0, Qt.AlignmentFlag.AlignRight)
                fields.addWidget(field, row, 1, Qt.AlignmentFlag.AlignLeft)

            pos = QComboBox()
            for title, key in [("Bottom right", "DR"), ("Bottom center", "DOWN"), ("Bottom left", "DL")]: pos.addItem(title, key)
            pos.setCurrentIndex(max(0, pos.findData(info.get("pos", "DR"))))
            start, size = QSpinBox(), QSpinBox()
            start.setRange(-999999, 999999)
            start.setValue(info.get("start", 1))
            size.setRange(6, 200)
            size.setValue(info.get("size", 24))
            for field in (start, size):
                field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                field.setFixedWidth(64)
            add_field(0, "Position", pos)
            add_field(1, "Start at", start)
            add_field(2, "Font size", size)
            checks = {}
            for row, (key, title, default) in enumerate((
                ("skip", "Skip title slide", True),
                ("total", "Show total slides", True),
                ("sub", "Show sub-slide numbers", False),
            ), 3):
                checks[key] = QCheckBox(title)
                checks[key].setChecked(info.get(key, default))
                fields.addWidget(checks[key], row, 1)
            checks["sub"].setToolTip("Slides with several builds also count them: 29 / 47 · 1/3 rather than 29 / 47")
            spread = QComboBox()
            for title, value in self.page_spread_choices: spread.addItem(title, value)
            spread.setCurrentIndex(max(0, spread.findData(prefs.get_str(self.page_spread_key))))
            spread.currentIndexChanged.connect(lambda _: self.page_spread_picked(spread))
            add_field(6, "When one is changed", spread)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            apply = buttons.button(QDialogButtonBox.StandardButton.Ok)
            apply.setText("Update Numbers" if info.get("on") else "Add Numbers")
            apply.setDefault(True)

            def change(args):
                dialog.accept()
                if epoch == self._deck_epoch:
                    self.run_page_numbers(args, target)

            buttons.accepted.connect(lambda: change({
                "op": "set", "pos": pos.currentData(), "start": start.value(), "size": size.value(),
                **{key: box.isChecked() for key, box in checks.items()},
            }))
            buttons.rejected.connect(dialog.reject)
            if info.get("on"):
                remove = buttons.addButton("Remove Numbers", QDialogButtonBox.ButtonRole.DestructiveRole)
                remove.clicked.connect(lambda: change({"op": "remove"}))
            layout.addWidget(buttons)
            self._page_numbers_dialog = dialog
            dialog.finished.connect(lambda _: setattr(self, "_page_numbers_dialog", None))
            dialog.open()
        return self._watch(spawn(show()).future)

    def set_pen_color(self, color):
        self.canvas.pen_color = QColor(color)
        hex_color = self.canvas.pen_color.name().upper()
        prefs.set("penColor", hex_color)
        if self.canvas.draw_tool == "pen":
            self.toast(f"Pen colour {hex_color}")
        else:
            self.set_draw_tool("pen")
    def pen_color_chosen(self, color): self.set_pen_color(color if isinstance(color, str) else color.data())
    def pen_color_panel(self):
        c = QColorDialog.getColor(getattr(self.canvas, "pen_color", QColor("#FF3B30")), self.window)
        if c.isValid(): self.set_pen_color(c)
    def pen_panel_changed(self, color): self.set_pen_color(color)
    def pen_finished(self, points):
        return self.insert("pen", {
            "points": [[p.x(), p.y()] if hasattr(p, "x") else list(p) for p in points],
            "color": self.canvas.pen_color.name().upper(),
            "width": self.canvas.pen_width,
        })

    def save_presenter_notes(self, scene, build, text, done):
        req = {"op": "set", "scene": scene, "text": text}
        if build is not None: req["stop"] = build
        future = self.tool_command("notes", req)
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

    def hyperlink_pressed(self):
        obj = self.obj_info or {}
        if obj.get("kind") not in ("text", "tex"): return
        oid = self.selection
        context, epoch = self.edit_context, self._deck_epoch
        current = (obj.get("format") or {}).get("link")
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Edit Hyperlink" if current is not None else "Add Hyperlink")
        layout = QVBoxLayout(dialog)
        prompt = QLabel("Clicking this text in a slideshow or Presenter View opens the link.")
        prompt.setMaximumWidth(320)
        prompt.setWordWrap(True)
        prompt.setFont(Theme.font(13))
        layout.addWidget(prompt)
        field = QLineEdit(current or "")
        field.setAccessibleName("URL")
        field.setPlaceholderText("https://example.com")
        field.setFixedWidth(320)
        field.setMinimumHeight(24)
        layout.addWidget(field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        remove_result = 2
        if current is not None:
            remove = buttons.addButton("Remove Link", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: dialog.done(remove_result))
        layout.addWidget(buttons)
        field.setFocus()
        result = dialog.exec()
        if result == remove_result:
            value = ""
        elif result == QDialog.DialogCode.Accepted:
            value = self.normalized_link(field.text())
        else:
            return
        if value != (current or "") and epoch == self._deck_epoch:
            req = {"op": "link", "oid": oid, "url": value, "scene": context.scene, "stop": context.stop}
            if context.seen:
                req["seen"] = context.seen
            return self.submit(lambda: self.run_edit(req))

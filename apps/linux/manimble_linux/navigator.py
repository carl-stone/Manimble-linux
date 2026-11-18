"""Slide/build navigation, following MainController's nested navigator rows."""
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QListWidgetItem, QAbstractItemView, QMenu

from .icon_art import LAYOUT_ICON_SIZE, layout_icon, set_menu_icon_size
from .menus import LAYOUTS
from .tasks import spawn
from .widgets import SlideTable, SlideCell, StepButton, section_label, _mapped_image


class NavigatorMixin:
    def init_navigator(self, table=None):
        self.table = table if table is not None else SlideTable()
        self.collapsed_builds = set()
        self.builds_expanded_all = False
        self.all_build_pages = {}
        self.shown_builds_row = None
        self.show_unused_slides = False
        self.suppress_table = False
        self.thumb_cache = OrderedDict()
        self._thumb_bytes = 0
        self._navigator_fetch_generation = 0
        self._navigator_selection = None
        self._navigator_menu_request = 0
        self.table.on_delete = lambda: self.delete_slide()
        self.table.on_delete_build = lambda: self.delete_build()
        self.table.on_arrow = self.step_navigator
        self.table.on_reclick = lambda row: self.toggle_builds_list()
        self.table.on_menu = self._show_navigator_menu
        self.table.on_reorder = self._navigator_reorder
        self.table.on_resize = self.note_slide_row_heights
        self.table.currentRowChanged.connect(self.table_view_selection_did_change)
        return self.table

    @property
    def selected_slide_is_unused(self):
        return bool(self.scene_name and any(s["name"] == self.scene_name for s in (self.state or {}).get("unusedScenes", [])))

    @property
    def has_builds_list(self):
        return bool(self.scene_name and self.scene and self.scene.get("name") == self.scene_name and len(self.scene.get("stops", [])) > 1)

    @property
    def builds_row(self):
        return next((i for i, scene in enumerate(self.navigator_scenes) if scene["name"] == self.scene_name and self.build_paths(i)), None)

    def build_paths(self, row):
        scenes = self.navigator_scenes
        if not 0 <= row < len(scenes):
            return []
        name = scenes[row]["name"]
        if name in self.collapsed_builds:
            return []
        if name == self.scene_name and self.scene and self.scene.get("name") == name:
            pages = [s.get("png") for s in self.scene.get("stops", [])]
            return pages if len(pages) > 1 else []
        pages = self.all_build_pages.get(name, []) if self.builds_expanded_all else []
        return pages if len(pages) > 1 else []

    def _navigator_image(self, path):
        from .darkmode import DarkMode
        key = (str(DarkMode.mode), bool(DarkMode.images), path)
        if key in self.thumb_cache:
            self.thumb_cache.move_to_end(key)
            return self.thumb_cache[key]
        image = _mapped_image(path)
        if image is None:
            image = QImage()
        if image.width() > 512 or image.height() > 288:
            image = image.scaled(512, 288, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.thumb_cache[key] = image
        self._thumb_bytes += image.sizeInBytes()
        while self._thumb_bytes > 64 * 1024 * 1024 and len(self.thumb_cache) > 1:
            _, old = self.thumb_cache.popitem(last=False)
            self._thumb_bytes -= old.sizeInBytes()
        return image

    def build_images(self, paths):
        return [self._navigator_image(path) for path in paths]

    def reload_all_slide_rows(self):
        if not hasattr(self, "table"):
            return
        self.suppress_table = True
        scroll = self.table.verticalScrollBar().value()
        build_focus = self.table.build_focused
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        state_scenes = (self.state or {}).get("scenes", [])
        selected_row = -1
        for row, summary in enumerate(self.navigator_scenes):
            name = summary["name"]
            active = name == self.scene_name
            unused = row >= len(state_scenes)
            listable = (active and self.has_builds_list) or (self.builds_expanded_all and len(self.all_build_pages.get(name, [])) > 1)
            paths = self.build_paths(row)
            cell = SlideCell()
            cell.configure(number=0 if unused else row + 1, name=name,
                           image=self._navigator_image(summary.get("thumb")), status=summary.get("status", "pending"),
                           stops=summary.get("stops", 0), active=active, unused=unused,
                           hidden=bool(summary.get("hidden")), expanded=name not in self.collapsed_builds if listable else None)
            cell.on_build = lambda index, n=name: self._navigator_build_clicked(n, index)
            cell.set_builds(self.build_images(paths), self.stop_index if active else -1)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            if unused:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            item.setSizeHint(QSize(self.table.viewport().width(), round(SlideCell.row_height(self.table.viewport().width(), len(paths)))))
            self.table.addItem(item)
            self.table.setItemWidget(item, cell)
            if active:
                selected_row = row
        self.table.setCurrentRow(selected_row)
        self.table.build_focused = build_focus
        self.table.setDragEnabled(self.player is None)
        self.table.verticalScrollBar().setValue(scroll)
        self.table.setUpdatesEnabled(True)
        self.suppress_table = False
        self.shown_builds_row = self.builds_row

    def _navigator_build_clicked(self, name, index):
        self.table.setFocus()
        self.table.build_focused = True
        if name == self.scene_name:
            self.go_to_stop(index)
        else:
            def go():
                yield from self.select_scene(name)
                if self.scene_name == name:
                    self.table.build_focused = True
                    self.go_to_stop(index)
            spawn(go(), name="navigator_build")

    def toggle_expand_all_builds(self):
        self.builds_expanded_all = not self.builds_expanded_all
        self._navigator_fetch_generation += 1
        if self.builds_expanded_all:
            self.collapsed_builds.clear()
            spawn(self.fetch_all_build_pages(), name="navigator_pages")
        else:
            self.collapsed_builds = {self.scene_name} if self.scene_name else set()
        self.update_expand_all_item()
        self.reload_all_slide_rows()
        self._scroll_current_row()

    def fetch_all_build_pages(self):
        if not self.builds_expanded_all:
            return
        generation, workspace = self._navigator_fetch_generation, self.workspace
        try:
            reply = yield self.call("deck_pages", {"ahead": 0})
        except Exception as error:
            self.toast(str(error), error=True)
            return
        if not self.builds_expanded_all or generation != self._navigator_fetch_generation or workspace is not self.workspace:
            return
        pages = {s["scene"]: s["pages"] for s in reply.get("slides", []) if s.get("pages")}
        if pages != self.all_build_pages:
            self.all_build_pages = pages
            self.reload_all_slide_rows()

    def toggle_builds_list(self):
        if not self.has_builds_list:
            return
        if self.scene_name in self.collapsed_builds:
            self.collapsed_builds.remove(self.scene_name)
        else:
            self.collapsed_builds.add(self.scene_name)
        self.refresh_build_rows()

    def step_navigator(self, direction):
        scenes = self.navigator_scenes
        row = next((i for i, s in enumerate(scenes) if s["name"] == self.scene_name), None)
        if row is None:
            return
        count = len((self.scene or {}).get("stops", [])) if (self.scene or {}).get("name") == self.scene_name else 0
        if count > 1 and 0 <= self.stop_index + direction < count:
            self.collapsed_builds.discard(self.scene_name)
            self.table.build_focused = True
            self.go_to_stop(self.stop_index + direction)
            self.refresh_build_rows()
            return
        target_row = row + direction
        self.table.build_focused = False
        if not 0 <= target_row < len(scenes):
            return
        name = scenes[target_row]["name"]
        self.collapsed_builds.discard(name)
        def go():
            yield from self.select_scene(name)
            self.table.setFocus()
            if direction < 0 and self.scene_name == name:
                last = len((self.scene or {}).get("stops", [])) - 1
                if last > 0:
                    self.go_to_stop(last)
            self.refresh_build_rows()
        spawn(go(), name="navigator_step")

    def refresh_build_rows(self):
        self.reload_all_slide_rows()
        self._scroll_current_row()

    def _scroll_current_row(self):
        if self.table.currentItem():
            self.table.scrollToItem(self.table.currentItem(), QAbstractItemView.ScrollHint.EnsureVisible)

    def refresh_rings(self):
        for row, summary in enumerate(self.navigator_scenes):
            item = self.table.item(row)
            cell = self.table.itemWidget(item) if item else None
            if cell:
                cell.set_active(summary["name"] == self.scene_name)

    def fit_sidebar_column(self):
        self.note_slide_row_heights()

    def note_slide_row_heights(self):
        width = self.table.viewport().width()
        for row in range(self.table.count()):
            item = self.table.item(row)
            item.setSizeHint(QSize(width, round(SlideCell.row_height(width, len(self.build_paths(row))))))

    def table_view_selection_did_change(self, row):
        if self.suppress_table or not 0 <= row < len(self.navigator_scenes):
            return
        self._select_navigator_scene(self.navigator_scenes[row]["name"])

    def _select_navigator_scene(self, name):
        if self._navigator_selection is not None:
            workspace, pending_name, future = self._navigator_selection
            if workspace is self.workspace and name == pending_name and not future.done():
                return future
        if name == self.scene_name:
            return None
        self.collapsed_builds.discard(name)
        future = spawn(self.select_scene(name), name="navigator_select").future
        self._navigator_selection = (self.workspace, name, future)
        return future

    def select_row(self, row):
        self.suppress_table = True
        self.table.setCurrentRow(row)
        self.suppress_table = False
        self._scroll_current_row()

    def _navigator_reorder(self, source, index):
        scenes = (self.state or {}).get("scenes", [])
        if self.player is not None or not 0 <= source < len(scenes) or not 0 <= index <= len(scenes):
            return False
        self.slide_op("reorder", index=index, scene_override=scenes[source]["name"])
        return True

    def _show_navigator_menu(self, row):
        self._navigator_menu_request += 1
        request = self._navigator_menu_request
        scenes = self.navigator_scenes
        if self.player is not None or not scenes:
            return None
        if not 0 <= row < len(scenes):
            return self.navigator_menu(row)
        name = scenes[row]["name"]
        # The row-change signal may already have started this load. Reuse it so the
        # menu gets the clicked slide's build state and shown background together.
        selection = self._select_navigator_scene(name)
        if selection is None:
            return self.navigator_menu(row)
        position, workspace = self.table.context_position, self.workspace

        def show():
            if (yield selection) is False:
                return
            if self._navigator_menu_request != request or workspace is not self.workspace or self.scene_name != name:
                return
            current_row = next((i for i, s in enumerate(self.navigator_scenes) if s["name"] == name), -1)
            if current_row < 0:
                return
            menu = self.navigator_menu(current_row)
            if menu is not None:
                menu.aboutToHide.connect(menu.deleteLater)
                menu.popup(position)
        spawn(show(), name="navigator_context")
        return None

    def navigator_menu(self, row):
        scenes = self.navigator_scenes
        if self.player is not None or not scenes:
            return None
        clicked = scenes[row] if 0 <= row < len(scenes) else None
        if clicked is not None and clicked["name"] != self.scene_name:
            return None
        self.actions.refresh()
        menu = QMenu(self.table)

        def add(command):
            menu.addAction(self.actions.present(command, menu))

        if clicked is None:
            add("toggle_expand_all_builds")
            return menu

        layouts = menu.addMenu("New Slide")
        set_menu_icon_size(layouts, LAYOUT_ICON_SIZE)
        background = self.canvas.shown_background or "#FFFFFF"
        for _, layout in LAYOUTS:
            layouts.addAction(self.actions.present(f"new_slide_layout:{layout}", layouts,
                                                   icon=layout_icon(layout, background)))
        add("new_build")
        add("duplicate_slide")
        menu.addSeparator()
        unused = row >= len((self.state or {}).get("scenes", []))
        name = clicked["name"]
        # This action belongs to this menu instance, not the current deck selection.
        export = menu.addAction("Export Slide as Movie (.mp4)…")
        export.setData(name)
        export.setEnabled(not unused and not clicked.get("hidden") and self.animated_player is None)
        export.triggered.connect(lambda _=False: self.export_slide_movie(name))
        menu.addSeparator()
        add("restore_slide" if unused else "toggle_hide_slide")
        menu.addSeparator()
        add("toggle_expand_all_builds")
        menu.addSeparator()
        add("delete_slide")
        return menu

    def toggle_unused_slides(self):
        self.show_unused_slides = not self.show_unused_slides
        self.reload_all_slide_rows()
        if self.scene_name not in {s["name"] for s in self.navigator_scenes}:
            if self.navigator_scenes:
                spawn(self.select_scene(self.navigator_scenes[0]["name"]), name="navigator_first")
            else:
                self.table.clearSelection()

    def rebuild_steps(self):
        self.refresh_build_rows()
        layout = self.steps_bar.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        stops = (self.scene or {}).get("stops", [])
        if len(stops) > 1:
            layout.addWidget(section_label("BUILDS"))
            for i, stop in enumerate(stops):
                layout.addWidget(StepButton(stop.get("png", ""), i, i == self.stop_index, lambda index=i: self.go_to_stop(index)))
        layout.addStretch()

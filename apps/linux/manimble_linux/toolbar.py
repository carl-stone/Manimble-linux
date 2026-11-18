"""The deck toolbar; all commands reuse the window's ActionRegistry."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QToolBar, QToolButton, QMenu, QComboBox, QWidget, QSizePolicy, QStyle

from .menus import LAYOUTS, SHAPES, PEN_SWATCHES, register_actions

ZOOM_LEVELS = (("Fit", None), ("25%", .25), ("50%", .5), ("75%", .75), ("100%", 1.),
               ("150%", 1.5), ("200%", 2.), ("300%", 3.))


class ToolbarMixin:
    def make_toolbar(self):
        if not hasattr(self, "actions") or "new_slide" not in self.actions:
            register_actions(self)
        toolbar = QToolBar("Manimble", self.window)
        toolbar.setObjectName("manimble.toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.toolbar = toolbar
        self._toolbar_buttons = {}

        def button(id, label=None, menu_ids=None, icon="", popup_only=False, tip=None):
            action = self.actions.action(id)
            if action.icon().isNull():
                action.setIcon(QIcon.fromTheme(icon, self.window.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)))
            tool = QToolButton(toolbar)
            tool.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            tool.setDefaultAction(action)
            if label:
                tool.setText(label)
            if tip:
                tool.setToolTip(tip)
            if menu_ids:
                menu = QMenu(tool)
                for command in menu_ids:
                    if command == "-":
                        menu.addSeparator()
                    else:
                        menu.addAction(self.actions.action(command))
                menu.aboutToShow.connect(self.actions.refresh)
                tool.setMenu(menu)
                tool.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup if popup_only else QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            toolbar.addWidget(tool)
            self._toolbar_buttons[id] = tool
            return tool

        button("new_slide", "New Slide", [f"new_slide_layout:{layout}" for _, layout in LAYOUTS], "list-add",
               tip="Add a slide after this one (choose a layout)")
        button("new_build", "New Build", icon="view-list-tree", tip="Add a build after this one, starting from what it shows")
        button("toggle_expand_all_builds", "Expand All", icon="view-list-tree")
        toolbar.addSeparator()
        button("insert_table:word", "Table", ["insert_table:word", "insert_table:math"], "insert-table")
        button("insert_text_pressed", "Text", icon="insert-text")
        button("insert_formula_pressed", "Formula", icon="insert-math-expression")
        shape = button("insert_shape:rectangle", "Shape", [f"insert_shape:{kind}" for _, kind in SHAPES] + ["-"] +
                       [f"draw:{kind}" for kind in ("line", "arrow", "darrow", "rectangle", "ellipse")] + ["-", "draw:pen"], "draw-rectangle", popup_only=True)
        colors = shape.menu().addMenu("Pen Color")
        for title, _ in PEN_SWATCHES:
            colors.addAction(self.actions.action(f"pen_color:{title}"))
        colors.addSeparator()
        colors.addAction(self.actions.action("pen_color_panel"))
        button("show_bio_cartoon", "BioCartoon", icon="applications-science", tip="Search BioCartoon's science icons")
        button("insert_image_pressed", "Image", icon="insert-image")
        button("theme:midnight", "Theme", [id for id in self.actions.commands if id.startswith("theme:")], "color-management", popup_only=True,
               tip="Switch the deck to another theme (font, colours, background)")
        button("insert_python_plot_pressed", "Python Plot", icon="office-chart-line", tip="Draw a picture with Python code or a linked .py file")
        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.zoom_popup = QComboBox(toolbar)
        self.zoom_popup.setAccessibleName("Zoom")
        self.zoom_popup.setToolTip("Slide zoom (Fit resets panning)")
        for title, value in ZOOM_LEVELS:
            self.zoom_popup.addItem(title, value)
        self.zoom_popup.activated.connect(lambda _: self.zoom_chosen(self.zoom_popup))
        toolbar.addWidget(self.zoom_popup)
        button("toggle_dark_mode", "Dark Mode", icon="weather-clear-night", tip="Swap whites and blacks for viewing only; Format can apply this permanently")
        toolbar.addSeparator()
        button("toggle_terminal", "Terminal", icon="utilities-terminal", tip="Show or hide the terminal (Ctrl+Alt+T)")
        button("share_pdf:slides", "Share", ["share_pdf:slides", "share_pdf:builds", "-", "export_power_point", "export_beamer", "export_movie", "-", "export_pdf_pressed"],
               "document-send", popup_only=True)
        self.update_expand_all_item()
        return toolbar

    def update_expand_all_item(self):
        button = self._toolbar_buttons.get("toggle_expand_all_builds")
        if button:
            self.style_expand_all_item(button)

    def style_expand_all_item(self, item):
        expanded = self.builds_expanded_all
        item.setText("Collapse All" if expanded else "Expand All")
        item.setToolTip("Fold away the builds listed under every slide" if expanded else "List the builds under every slide in the navigator")
        item.setIcon(QIcon.fromTheme("view-list-tree" if not expanded else "view-list-details"))

    def zoom_chosen(self, popup):
        self.canvas.zoom = popup.currentData()
        self.sync_zoom_popup()

    def zoom_in(self):
        self.canvas.zoom_by(1.25)
        self.sync_zoom_popup()

    def zoom_out(self):
        self.canvas.zoom_by(.8)
        self.sync_zoom_popup()

    def zoom_fit(self):
        self.canvas.zoom = None
        self.sync_zoom_popup()

    def sync_zoom_popup(self):
        popup = self.zoom_popup
        popup.blockSignals(True)
        while popup.count() > len(ZOOM_LEVELS):
            popup.removeItem(len(ZOOM_LEVELS))
        zoom = self.canvas.zoom
        if zoom is None:
            popup.setItemText(0, f"Fit ({round(self.canvas.fit_scale * 100)}%)")
            popup.setCurrentIndex(0)
        else:
            title = f"{round(zoom * 100)}%"
            index = popup.findText(title)
            if index < 0:
                popup.addItem(title, zoom)
                index = popup.count() - 1
            popup.setCurrentIndex(index)
        popup.blockSignals(False)

    def share_chosen(self, sender):
        from .tasks import spawn
        builds = bool(sender.data())
        def share():
            try:
                path = yield from self.make_pdf(builds=builds)
                if path:
                    self.show_share_picker(path)
            except Exception as error:
                self.alert("Could not prepare PDF", str(error))
        spawn(share(), name="share_pdf")

    def show_share_picker(self, path):
        """Linux has no system-wide share sheet; expose the actual local PDF."""
        from pathlib import Path
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QApplication, QFileDialog
        from .tasks import spawn
        from .workers import run_blocking

        path = Path(path)
        menu = QMenu(self.window)
        self._share_menu = menu
        open_pdf = menu.addAction("Open PDF…")
        copy_file = menu.addAction("Copy PDF File")
        save_copy = menu.addAction("Save PDF Copy…")
        def open_document():
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self.alert("Could not open PDF", str(path))
        def copy_document():
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(path))])
            QApplication.clipboard().setMimeData(mime)
            self.toast("PDF copied — paste it into the application you want to share with")
        def save_document():
            destination, _ = QFileDialog.getSaveFileName(self.window, "Save PDF Copy", path.name, "PDF (*.pdf)")
            if not destination:
                return
            if not destination.lower().endswith(".pdf"):
                destination += ".pdf"
            def save():
                try:
                    yield run_blocking(_save_shared_copy, path, Path(destination))
                    self.toast("Saved " + destination)
                except Exception as error:
                    self.alert("Could not save PDF", str(error))
            spawn(save(), name="share_pdf_copy")
        open_pdf.triggered.connect(open_document)
        copy_file.triggered.connect(copy_document)
        save_copy.triggered.connect(save_document)
        button = self._toolbar_buttons.get("share_pdf:slides")
        anchor = button.mapToGlobal(button.rect().bottomLeft()) if button else self.window.mapToGlobal(self.window.rect().center())
        menu.popup(anchor)


def _save_shared_copy(source, destination):
    import os
    import shutil
    import tempfile
    from pathlib import Path
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)

"""The deck toolbar; all commands reuse the window's ActionRegistry."""
from __future__ import annotations

from math import floor

from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt
from PySide6.QtWidgets import (
    QComboBox, QLabel, QMenu, QSizePolicy, QToolBar, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from .darkmode import ColorView, DarkMode
from .icon_art import LAYOUT_ICON_SIZE, THEME_SWATCH_SIZE, layout_icon, pen_color_icon, set_menu_icon_size
from .icons import template_icon
from .menus import LAYOUTS, SHAPES, PEN_SWATCHES, register_actions
from .style import Theme

ZOOM_LEVELS = (("Fit", None), ("25%", .25), ("50%", .5), ("75%", .75), ("100%", 1.),
               ("150%", 1.5), ("200%", 2.), ("300%", 3.))


class _ToolbarMenu(QMenu):
    def __init__(self, parent):
        super().__init__(parent)
        self.setToolTipsVisible(True)

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            action = self.actionAt(event.pos())
            # QAction otherwise falls back to showing the row's label as a tooltip.
            if action is None or not action.statusTip():
                return True
        return super().event(event)


class _ZoomToolbarAction(QWidgetAction):
    """A combo in the toolbar and a regular submenu when it overflows."""

    def createWidget(self, parent):
        if not isinstance(parent, QToolBar):
            return None
        view = QWidget(parent)
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.popup = QComboBox(view)
        self.popup.setAccessibleName("Zoom")
        self.popup.setFont(Theme.font(11))
        self.popup.setFixedSize(round(Theme.logical_size(84)), round(Theme.logical_size(26)))
        for title, value in ZOOM_LEVELS:
            self.popup.addItem(title, value)
        layout.addWidget(self.popup)
        label = QLabel("Zoom", view)
        label.setFont(Theme.font(11))
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(label)
        return view


class ToolbarMixin:
    def make_toolbar(self):
        if not hasattr(self, "actions") or "new_slide" not in self.actions:
            register_actions(self)
        toolbar = QToolBar("Manimble", self.window)
        toolbar.setObjectName("manimble.toolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(22, 22))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.toolbar = toolbar
        self._toolbar_buttons = {}

        def popup():
            menu = _ToolbarMenu(toolbar)
            menu.aboutToShow.connect(self.actions.refresh)
            return menu

        def row(menu, id, text, *, icon=None, tip=""):
            action = self.actions.present(id, menu, text=text, icon=icon, tip=tip)
            menu.addAction(action)
            return action

        def button(id, label, symbol, tip, *, menu=None, popup_only=False, mirror_check=True):
            action = self.actions.present(id, toolbar, text=label, tip=tip, icon=template_icon(symbol, toolbar),
                                          mirror_check=mirror_check)
            # Use real QAction entries: Qt carries their menus into the overflow menu.
            # The default action already owns the menu. Installing it again on the
            # button removes that default action when Qt sees the same menu action.
            if menu is not None:
                action.setMenu(menu)
            toolbar.addAction(action)
            tool = toolbar.widgetForAction(action)
            tool.setFont(Theme.font(11))
            tool.setAccessibleName(label)
            if menu is not None:
                tool.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup if popup_only
                                  else QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            self._toolbar_buttons[id] = tool
            return tool

        def fixed_space():
            space = QWidget(toolbar)
            space.setFixedWidth(12)
            toolbar.addWidget(space)

        layouts = popup()
        layout_rows = [
            (layout, row(layouts, f"new_slide_layout:{layout}", title, icon=layout_icon(layout)))
            for title, layout in LAYOUTS
        ]

        def refresh_layout_art():
            background = self.canvas.shown_background or "#FFFFFF"
            for layout, action in layout_rows:
                action.setIcon(layout_icon(layout, background))

        layouts.aboutToShow.connect(refresh_layout_art)
        set_menu_icon_size(layouts, LAYOUT_ICON_SIZE)
        button("new_slide", "New Slide", "plus.rectangle.on.rectangle",
               "Add a slide after this one (choose a layout)", menu=layouts)
        button("new_build", "New Build", "square.stack.3d.up.badge.automatic",
               "Add a build after this one, starting from what it shows")
        button("toggle_expand_all_builds", *self._expand_all_presentation())
        fixed_space()

        tables = popup()
        row(tables, "insert_table:word", "Word Table", icon=template_icon("tablecells", tables),
            tip="Plain text cells (Table)")
        row(tables, "insert_table:math", "Math Table", icon=template_icon("function", tables),
            tip="LaTeX cells (MathTable)")
        button("insert_table:word", "Table", "tablecells", "Insert a word table (click) or choose word / math", menu=tables)
        button("insert_text_pressed", "Text", "textformat", "Insert text")
        button("insert_formula_pressed", "Formula", "function", "Insert formula")

        shape = popup()
        symbols = {"rectangle": "rectangle", "rounded": "rectangle.roundedtop", "circle": "circle",
                   "ellipse": "oval", "triangle": "triangle", "line": "line.diagonal",
                   "arrow": "arrow.right", "darrow": "arrow.left.and.right"}
        for title, kind in SHAPES:
            row(shape, f"insert_shape:{kind}", title, icon=template_icon(symbols[kind], shape))
        shape.addSeparator()
        for title, kind, symbol in (("Draw Line", "line", "pencil.line"),
                                    ("Draw Arrow", "arrow", "arrow.up.right"),
                                    ("Draw Double Arrow", "darrow", "arrow.left.and.right"),
                                    ("Draw Rectangle", "rectangle", "rectangle.dashed"),
                                    ("Draw Ellipse", "ellipse", "oval")):
            row(shape, f"draw:{kind}", title, icon=template_icon(symbol, shape),
                tip="Then drag on the slide (Shift snaps the angle or keeps it square)")
        shape.addSeparator()
        pen_shortcut = self.actions.shortcut_text("draw:pen")
        row(shape, "draw:pen", "Pen (freehand)", icon=template_icon("pencil.tip", shape),
            tip=f"Draw freehand on the slide ({pen_shortcut}); each stroke becomes a smooth curve in the source. Esc ends the pen.")
        colors = shape.addMenu("Pen Color")
        for title, color in PEN_SWATCHES:
            row(colors, f"pen_color:{title}", title, icon=pen_color_icon(color))
        button("insert_shape:rectangle", "Shape", "square.on.circle", "Insert shape", menu=shape, popup_only=True)
        button("show_bio_cartoon", "BioCartoon", "biocartoon",
               "Search BioCartoon's science icons and drag them onto the slide")
        button("insert_image_pressed", "Image", "photo", "Insert image")

        themes = popup()
        for id in self.actions.commands:
            if id.startswith("theme:"):
                themes.addAction(self.actions.action(id))
        set_menu_icon_size(themes, THEME_SWATCH_SIZE)
        button("theme:midnight", "Theme", "swatchpalette", "Switch the deck to another theme (font, colours, background)",
               menu=themes, popup_only=True, mirror_check=False)
        button("insert_python_plot_pressed", "Python Plot", "chart.xyaxis.line",
               "Insert a picture drawn by Python code (or a .py file); hover it and press ▶ to run the code again")

        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        zoom = _ZoomToolbarAction(toolbar)
        zoom.setText("Zoom")
        zoom_menu = QMenu(toolbar)
        zoom.setMenu(zoom_menu)
        zoom_menu.aboutToShow.connect(lambda: self._populate_zoom_menu(zoom_menu))
        toolbar.addAction(zoom)
        self.zoom_popup = zoom.popup
        self.zoom_popup.activated.connect(self.zoom_chosen)
        button("toggle_dark_mode", "Dark Mode", self._moon_symbol(),
               "Show slides with whites and blacks swapped (view only; Format ▸ Apply Dark Mode to Deck writes it)",
               mirror_check=False)
        fixed_space()
        terminal_shortcut = self.actions.shortcut_text("toggle_terminal")
        button("toggle_terminal", "Terminal", "terminal", f"Show or hide the terminal ({terminal_shortcut})")

        share = popup()
        row(share, "share_pdf:slides", "Share PDF (one page per slide)…")
        row(share, "share_pdf:builds", "Share PDF (every build)…")
        share.addSeparator()
        row(share, "export_power_point", "Export PowerPoint (.pptx)…")
        row(share, "export_beamer", "Export Beamer (.tex)…")
        row(share, "export_movie", "Export Movie (.mp4)…")
        share.addSeparator()
        row(share, "export_pdf_pressed", "Save PDF…")
        share_button = button("share_pdf:slides", "Share", "square.and.arrow.up", "Share the presentation",
                              menu=share, popup_only=True)
        share_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; height: 0px; }")

        self._populate_zoom_menu(zoom_menu)
        self.actions.refresh()
        return toolbar

    def _expand_all_presentation(self):
        if self.builds_expanded_all:
            return "Collapse All", "rectangle.compress.vertical", "Fold away the builds listed under every slide"
        return "Expand All", "rectangle.expand.vertical", "List the builds under every slide in the navigator"

    def update_expand_all_item(self):
        button = getattr(self, "_toolbar_buttons", {}).get("toggle_expand_all_builds")
        if button is None:
            return
        label, symbol, tip = self._expand_all_presentation()
        action = button.defaultAction()
        action.setText(label)
        action.setToolTip(tip)
        action.setStatusTip(tip)
        action.setIcon(template_icon(symbol, button))
        button.setAccessibleName(label)

    @staticmethod
    def _moon_symbol():
        return "moon.fill" if DarkMode.mode == ColorView.dark else "moon"

    def update_dark_mode_item(self):
        button = getattr(self, "_toolbar_buttons", {}).get("toggle_dark_mode")
        if button is not None:
            button.defaultAction().setIcon(template_icon(self._moon_symbol(), button))

    def zoom_chosen(self, index):
        self._set_zoom(self.zoom_popup.itemData(index))

    def _set_zoom(self, value):
        self.canvas.zoom = value
        self.sync_zoom_popup()

    def zoom_in(self):
        self.canvas.zoom_by(1.25)
        self.sync_zoom_popup()

    def zoom_out(self):
        self.canvas.zoom_by(.8)
        self.sync_zoom_popup()

    def zoom_fit(self):
        self._set_zoom(None)

    def sync_zoom_popup(self):
        popup = getattr(self, "zoom_popup", None)
        if popup is None:
            return
        fit = f"Fit ({floor(self.canvas.fit_scale * 100 + .5)}%)"
        zoom = self.canvas.zoom
        extra = len(ZOOM_LEVELS)
        index = 0
        with QSignalBlocker(popup):
            if popup.itemText(0) != fit:
                popup.setItemText(0, fit)
            if zoom is not None:
                title = f"{floor(zoom * 100 + .5)}%"
                index = popup.findText(title)
                if index < 0:
                    index = extra
                    if popup.count() == extra:
                        popup.addItem(title, zoom)
                    else:
                        popup.setItemText(extra, title)
                if index == extra:
                    popup.setItemData(extra, zoom)
            if index < extra and popup.count() > extra:
                popup.removeItem(extra)
            popup.setCurrentIndex(index)

    def _populate_zoom_menu(self, menu):
        self.sync_zoom_popup()
        popup = self.zoom_popup
        menu.clear()
        for index in range(popup.count()):
            action = menu.addAction(popup.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == popup.currentIndex())
            action.triggered.connect(lambda _, value=popup.itemData(index): self._set_zoom(value))

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

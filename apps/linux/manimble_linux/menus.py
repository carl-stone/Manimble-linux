"""The shared command surface, ported from AppDelegate.buildMenu and MenuRouter.

Command ids are the snake_case selectors; parameterized entries append a value. All
controller lookups happen at invocation time so an unwired feature is an explicit error.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from PySide6.QtCore import QFileInfo, QSize
from PySide6.QtWidgets import QApplication, QFileIconProvider, QLineEdit, QPlainTextEdit, QTextEdit, QMessageBox

from . import prefs
from .actions import ActionRegistry
from .icon_art import LAYOUT_ICON_SIZE, THEME_SWATCH_SIZE, layout_icon, set_menu_icon_size
from .paths import version
from .tasks import spawn

LAYOUTS = (("Title Slide", "title"), ("Title and Content", "title_content"),
           ("Two Content", "two_content"), ("Title Only", "title_only"), ("Blank", "blank"))
SHAPES = (("Rectangle", "rectangle"), ("Rounded Rectangle", "rounded"), ("Circle", "circle"),
          ("Ellipse", "ellipse"), ("Triangle", "triangle"), ("Line", "line"),
          ("Arrow", "arrow"), ("Double Arrow", "darrow"))
PEN_SWATCHES = (("Red", "#FF3B30"), ("Orange", "#FF9A2E"), ("Yellow", "#FFD400"),
                ("Green", "#34C759"), ("Blue", "#4D7CFF"), ("Purple", "#7C5CFF"),
                ("White", "#FFFFFF"), ("Black", "#000000"))
HIGHLIGHTS = (("Yellow", "#FFD400"), ("Green", "#7CFC00"), ("Cyan", "#40E0FF"),
              ("Pink", "#FF6EC7"), ("Blue", "#4D7CFF"), ("Red", "#FF4040"),
              ("Orange", "#FF9A2E"), ("Gray", "#8A8A8A"), ("No Highlight", None))
MENU_ORDER = ["Manimble", "File", "Edit", "Insert", "Slide", "Format", "Arrange", "View", "Play", "Window"]
GLOBAL = {"show_about", "install_dependencies", "choose_python", "hide", "terminate", "new_deck",
          "open_deck", "open_recent", "clear_recent", "perform_close", "perform_miniaturize", "perform_zoom",
          "arrange_in_front", "toggle_terminal", "new_terminal", "split_terminal", "toggle_terminal_in_work_copy",
          "toggle_navigator", "toggle_inspector", "toggle_pane_overlay"}
TEXT_ACTIONS = {"cut": "cut", "copy": "copy", "paste": "paste", "select_all": "selectAll",
                "undo": "undo", "redo": "redo"}


def _has_text_history(widget, name):
    history = widget if isinstance(widget, QLineEdit) else widget.document()
    return history.isUndoAvailable() if name == "undo" else history.isRedoAvailable()


class MenuRouter:
    def __init__(self, controller):
        self.controller = controller

    def local_target(self, name):
        if name not in TEXT_ACTIONS:
            return None
        widget = QApplication.focusWidget()
        while widget is not None:
            if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
                # After the source editor's local history runs out, the deck's
                # Undo/Redo is available again unless a draft still blocks it.
                if (widget is getattr(self.controller, "source_view", None) and name in {"undo", "redo"}
                        and not _has_text_history(widget, name)):
                    return None
                return widget
            widget = widget.parentWidget()
        return None

    def terminal(self):
        if self.controller.actions.terminal_focused:
            return getattr(self.controller, "terminal", None)
        return None

    def invoke(self, name, sender=None):
        local = self.local_target(name)
        if local is not None:
            return getattr(local, TEXT_ACTIONS[name])()
        controller = self.controller
        active = QApplication.activeWindow()
        if name in TEXT_ACTIONS and active is not None and active is not controller.window:
            # Never send Delete/Cut/Undo behind a presentation or a modal editor.
            return None
        terminal = self.terminal()
        if terminal is not None:
            if name in {"show_find", "show_find_replace"}:
                return terminal.show_find()
            if name in {"find_next", "find_previous"}:
                return terminal.find(previous=name == "find_previous")
            if name in TEXT_ACTIONS:
                session = terminal.active
                if session is not None and name in {"copy", "paste", "select_all"}:
                    if name == "paste":
                        session._emit("paste", text=QApplication.clipboard().text())
                    else:
                        session._emit(name)
                return None
        handler = getattr(controller, name, None)
        if not callable(handler):
            raise AttributeError(f"Manimble command {name!r} is not implemented by {type(controller).__name__}")
        result = handler(sender) if sender is not None else handler()
        if inspect.isgenerator(result):
            return spawn(result, name=name)
        return result


def _state(c):
    return getattr(c, "state", None) or {}


def _obj(c):
    return getattr(c, "obj_info", None) or {}


def validate_action(c, name, action):
    """Refresh title, checkmark and enablement from the current deck/focus state."""
    local = getattr(c, "_menu_router", None)
    target = local.local_target(name) if local else None
    if target is not None:
        if name in {"undo", "redo"}:
            return _has_text_history(target, name)
        if name in {"copy", "cut"}:
            return target.textCursor().hasSelection() if hasattr(target, "textCursor") else target.hasSelectedText()
        return True
    if local and local.terminal() is not None and name in TEXT_ACTIONS:
        return name in {"copy", "paste", "select_all"}
    active = QApplication.activeWindow()
    if name in TEXT_ACTIONS and active is not None and active is not c.window:
        return False
    state, obj = _state(c), _obj(c)
    canvas = getattr(c, "canvas", None)
    scene = getattr(c, "scene", None) or {}
    selected = list(getattr(canvas, "selected_ids", []))
    has_scene = getattr(c, "scene_name", None) is not None
    still = getattr(c, "player", None) is not None
    presenter = getattr(c, "presenter", None) is not None
    animated = getattr(c, "animated_player", None) is not None
    editing = has_scene and not still
    unused = bool(getattr(c, "selected_slide_is_unused", False))
    playing = still or presenter or animated
    scenes = bool(state.get("scenes"))
    kind, fmt = obj.get("kind"), obj.get("format") or {}
    data = action.data()
    if name in {"bring_to_front", "bring_forward", "send_backward", "send_to_back"}:
        return editing and bool(selected)
    if name == "group_objects":
        return editing and len(selected) > 1
    if name == "ungroup_objects":
        return editing and any((scene.get("objects", {}).get(str(i)) or {}).get("group") is not None for i in selected)
    if name in {"cut", "copy"}:
        return editing and bool(selected)
    if name == "paste":
        return bool(getattr(c, "can_paste_on_slide", editing))
    if name == "select_all":
        return editing
    if name == "spread_last_move":
        move = getattr(c, "last_build_move", None)
        return bool(move and move[0] == getattr(c, "scene_name", None) and move[2] == getattr(c, "stop_index", 0))
    if name in {"format_bold", "format_italic"}:
        return kind == "text"
    if name in {"format_underline", "format_strikethrough", "highlight_chosen"}:
        value = fmt.get({"format_underline": "underline", "format_strikethrough": "strike", "highlight_chosen": "highlight"}[name])
        if name == "highlight_chosen":
            action.setChecked((value.upper() if value is not None else None) == (data.upper() if data is not None else None))
        else:
            action.setChecked(bool(value))
        return kind in {"text", "tex"}
    if name in {"format_bigger", "format_smaller", "clear_formatting", "hyperlink_pressed"}:
        return kind in {"text", "tex"}
    if name in {"format_superscript", "format_subscript", "toggle_bullets", "toggle_numbering", "increase_indent", "decrease_indent"}:
        return kind == "text" and obj.get("textOk") is True
    if name in {"align_left", "align_center", "align_right", "align_justify", "spacing_menu_chosen", "columns_menu_chosen"}:
        if name in {"align_left", "align_center", "align_right"}:
            action.setChecked(fmt.get("align") == name[6:])
        if name == "columns_menu_chosen":
            action.setChecked(fmt.get("columns", 1) == data)
        return kind == "text"
    toggles = {"toggle_grid": (canvas, "show_grid"),
               "toggle_snap": (canvas, "snap_enabled"), "toggle_ruler": (c, "show_ruler"),
               "toggle_tips": (c, "show_tips"), "toggle_unused_slides": (c, "show_unused_slides"),
               "toggle_notes_pane": (c, "drawer_shows_notes")}
    if name in toggles:
        owner, attr = toggles[name]
        action.setChecked(bool(getattr(owner, attr, False)))
        return getattr(c, "workspace", None) is not None if name in {"toggle_unused_slides", "toggle_notes_pane"} else True
    if name in {"add_vertical_guide", "add_horizontal_guide"}:
        return has_scene
    if name == "clear_guides":
        return bool(getattr(canvas, "user_guides", []))
    if name == "draw_tool_menu":
        action.setChecked(getattr(canvas, "draw_tool", None) == data)
        return editing
    if name == "pen_color_chosen":
        color = getattr(canvas, "pen_color", None)
        current = color.name() if hasattr(color, "name") else str(color)
        action.setChecked(current.upper() == str(data).upper())
        return True
    if name in {"new_slide", "new_slide_layout", "duplicate_slide", "move_slide_up", "move_slide_down", "toggle_hide_slide"}:
        if name == "toggle_hide_slide":
            action.setText("Unhide Slide" if getattr(c, "selected_slide_is_hidden", False) else "Hide Slide")
        return editing and not unused
    if name == "new_build":
        return editing and scene.get("name") == getattr(c, "scene_name", None) and scene.get("stops") is not None
    if name == "delete_slide":
        return editing and (len(state.get("scenes", [])) > 1 or unused)
    if name == "restore_slide":
        return editing and unused
    if name == "toggle_expand_all_builds":
        action.setText("Collapse All Builds" if getattr(c, "builds_expanded_all", False) else "Expand All Builds")
        return bool(c.navigator_scenes) and not still
    if name == "extract_elements_pressed":
        return editing and kind == "image" and bool(obj.get("imagePath")) and not obj["imagePath"].lower().endswith(".svg")
    if name == "slide_numbers_pressed":
        return scenes and not still
    if name in {"apply_dark_mode", "apply_invert_colors"}:
        return scenes and not playing
    if name == "toggle_terminal":
        action.setText("Hide Terminal" if getattr(c, "drawer_shows_terminal", False) else "Show Terminal")
        return getattr(c, "workspace", None) is not None
    if name == "toggle_engine_log":
        action.setChecked(c.drawer_open and c.drawer.page == "log")
        return True
    if name in {"new_terminal", "split_terminal", "export_python_pressed", "save_to_original", "save_project_folder_pressed", "package_project_pressed", "revert_to_original"}:
        return getattr(c, "workspace", None) is not None
    if name == "toggle_terminal_in_work_copy":
        action.setChecked(prefs.get_bool("terminalInWorkCopy", True))
        return True
    if name in {"toggle_dark_mode", "toggle_invert_colors", "toggle_dark_images"}:
        from .darkmode import DarkMode
        mode = getattr(DarkMode.mode, "value", DarkMode.mode)
        action.setChecked(DarkMode.images if name == "toggle_dark_images" else mode == ("dark" if name == "toggle_dark_mode" else "invert"))
        return mode != "normal" if name == "toggle_dark_images" else True
    if name == "toggle_show_animations":
        action.setText("Hide Animations" if getattr(c, "show_animations", False) else "Show Animations")
        return True
    if name in {"play_menu_from_beginning", "play_menu_from_current", "play_menu_presenter"}:
        if getattr(c, "show_animations", False):
            return not playing and (scenes if name == "play_menu_from_beginning" else has_scene)
        if name == "play_menu_from_current":
            return editing
        return scenes and not still and not presenter
    if name == "auto_present_from_beginning":
        return scenes and not playing
    if name == "auto_present_from_current":
        return editing and not playing
    if name == "export_movie":
        return scenes and not still and not animated
    if name == "play_current_slide_animations":
        return editing and getattr(c, "source_draft", None) is None and getattr(c, "preview_scene", None) is None
    if name == "animation_resolution_chosen":
        action.setChecked(data == getattr(c, "animation_height", 720))
        return not playing and getattr(c, "preview_scene", None) is None
    if name == "theme_chosen":
        action.setChecked(scene.get("theme") == action.text())
        return has_scene
    if name in {"undo", "redo"}:
        return getattr(c, "source_draft", None) is None and bool(state.get("canUndo" if name == "undo" else "canRedo", True))
    if name.startswith("insert_") or name in {"format_background", "show_bio_cartoon"}:
        return editing
    if name == "clear_recent":
        return bool(data)
    return True


def register_actions(controller, registry=None):
    registry = registry or getattr(controller, "actions", None) or ActionRegistry(controller.window)
    controller.actions = registry
    router = MenuRouter(controller)
    controller._menu_router = router
    bound = set()
    placements = {}

    def ensure_menu(path):
        if path not in placements:
            placements[path] = []
            if "/" in path:
                parent = path.rsplit("/", 1)[0]
                ensure_menu(parent)
                placements[parent].append(("menu", path))

    def separator(menu):
        ensure_menu(menu)
        placements[menu].append(("separator", None))
        registry.separator(menu)

    def add(menu, title, name, key=None, value=..., id=None, check=False):
        command_id = id or (name if value is ... else f"{name}:{len(registry.commands)}")
        action = registry.add(command_id, title, shortcut=key, menu=menu,
                              scope="global" if name in GLOBAL else "deck", checkable=check)
        if value is not ...:
            action.setData(value)
        action.setProperty("handler", name)
        action.triggered.connect(lambda _=False, n=name, a=action, parameter=value is not ...: router.invoke(n, a if parameter else None))
        registry.commands[command_id].enabled = lambda n=name, a=action: validate_action(controller, n, a)
        bound.add(name)
        if menu:
            ensure_menu(menu)
            placements[menu].append(("action", command_id))
        return action

    def section(menu, rows):
        for row in rows:
            if row is None:
                separator(menu)
            else:
                title, name, *keys = row
                add(menu, title, name, keys[0] if keys else None)

    section("Manimble", [("About Manimble", "show_about"), None, ("Install Dependencies…", "install_dependencies"),
        ("Choose Python…", "choose_python"), None, ("Hide Manimble", "hide", "Ctrl+H"), ("Quit Manimble", "terminate", "Ctrl+Q")])
    section("File", [("New Deck…", "new_deck", "Ctrl+N"), ("Open Deck or Project…", "open_deck", "Ctrl+O")])
    add("File/Open Recent", "Clear Menu", "clear_recent")
    section("File/Import", [("PowerPoint (.pptx)…", "import_power_point"), ("Beamer (.tex)…", "import_beamer"), None,
                            ("Manim Slides Project…", "import_manim_slides")])
    separator("File")
    section("File", [("Save to Original", "save_to_original", "Ctrl+S"), ("Save Project Copy…", "save_project_folder_pressed"),
                     ("Package for Sharing…", "package_project_pressed"), ("Revert to Original…", "revert_to_original"), None])
    section("File/Export To", [("PowerPoint (.pptx)…", "export_power_point"), ("Beamer (.tex)…", "export_beamer"),
        ("PDF…", "export_pdf_pressed"), ("Movie (.mp4, with animations)…", "export_movie"), None,
        ("Python (whole deck in one file)…", "export_python_pressed")])
    section("File", [None, ("Close Window", "perform_close", "Ctrl+W")])
    section("Edit", [("Undo", "undo", "Ctrl+Z"), ("Redo", "redo", "Ctrl+Shift+Z"), None,
        ("Cut", "cut", "Ctrl+X"), ("Copy", "copy", "Ctrl+C"), ("Paste", "paste", "Ctrl+V"), ("Select All", "select_all", "Ctrl+A"), None,
        ("Find…", "show_find", "Ctrl+F"), ("Find and Replace…", "show_find_replace", "Ctrl+Alt+F"),
        ("Find Next", "find_next"), ("Find Previous", "find_previous")])
    section("Insert", [("Text", "insert_text_pressed", "Ctrl+Shift+T"), ("Formula…", "insert_formula_pressed", "Ctrl+Shift+E")])
    for title, kind in SHAPES:
        add("Insert/Shape", title, "insert_shape_pressed", value=kind, id=f"insert_shape:{kind}")
    for title, kind in (("Line", "line"), ("Arrow", "arrow"), ("Double Arrow", "darrow"), ("Rectangle", "rectangle"), ("Ellipse", "ellipse")):
        add("Insert/Draw (drag on the slide)", title, "draw_tool_menu", value=kind, id=f"draw:{kind}", check=True)
    separator("Insert/Draw (drag on the slide)")
    add("Insert/Draw (drag on the slide)", "Pen (freehand)", "draw_tool_menu", "Ctrl+Shift+P", value="pen", id="draw:pen", check=True)
    for title, color in PEN_SWATCHES:
        add("Insert/Draw (drag on the slide)/Pen Color", title, "pen_color_chosen", value=color, id=f"pen_color:{title}", check=True)
    separator("Insert/Draw (drag on the slide)/Pen Color")
    add("Insert/Draw (drag on the slide)/Pen Color", "Other…", "pen_color_panel")
    add("Insert", "BioCartoon Icons…", "show_bio_cartoon")
    for title, value in (("Word Table…", "word"), ("Math Table…", "math")):
        add("Insert", title, "insert_table_pressed", value=value, id=f"insert_table:{value}")
    section("Insert", [("Image…", "insert_image_pressed", "Ctrl+Shift+I"), ("Python Plot…", "insert_python_plot_pressed"),
        ("Extract Elements from Image…", "extract_elements_pressed"), None, ("Slide Numbers…", "slide_numbers_pressed")])
    for title, layout in LAYOUTS:
        action = add("Slide/New Slide", title, "new_slide_layout", "Ctrl+Shift+N" if layout == "title_content" else None,
                     value=layout, id=f"new_slide_layout:{layout}")
        action.setIcon(layout_icon(layout))
    section("Slide", [("New Build", "new_build"), ("Duplicate Slide", "duplicate_slide", "Ctrl+D"),
        ("Delete Slide", "delete_slide", "Ctrl+Shift+Backspace"), ("Hide Slide", "toggle_hide_slide", "Ctrl+Shift+H"),
        ("Restore Slide to Show", "restore_slide"), None, ("Move Slide Up", "move_slide_up", "Ctrl+Alt+Up"),
        ("Move Slide Down", "move_slide_down", "Ctrl+Alt+Down"), None, ("Previous Slide", "prev_slide", "Ctrl+Up"),
        ("Next Slide", "next_slide", "Ctrl+Down"), ("Previous Build", "prev_stop", "Ctrl+["), ("Next Build", "next_stop", "Ctrl+]")])
    for title, name, key in (("Bold", "format_bold", "Ctrl+B"), ("Italic", "format_italic", "Ctrl+I"),
                             ("Underline", "format_underline", "Ctrl+U"), ("Strikethrough", "format_strikethrough", None),
                             ("Superscript", "format_superscript", "Ctrl+Shift++"), ("Subscript", "format_subscript", "Ctrl+Shift+_")):
        add("Format", title, name, key, check=name in {"format_underline", "format_strikethrough"})
    section("Format", [None, ("Bigger", "format_bigger", "Ctrl+Alt+="), ("Smaller", "format_smaller", "Ctrl+Alt+-")])
    for title, color in HIGHLIGHTS:
        add("Format/Highlight", title, "highlight_chosen", value=color, id=f"highlight:{title}", check=True)
    section("Format", [("Hyperlink…", "hyperlink_pressed", "Ctrl+K"), ("Clear Formatting", "clear_formatting"), None,
        ("Bulleted List", "toggle_bullets", "Ctrl+Shift+8"), ("Numbered List", "toggle_numbering", "Ctrl+Shift+7"),
        ("Increase Indent", "increase_indent", "Ctrl+Shift+]"), ("Decrease Indent", "decrease_indent", "Ctrl+Shift+["), None])
    for title, name, key in (("Align Left", "align_left", "Ctrl+L"), ("Center", "align_center", "Ctrl+E"),
                             ("Align Right", "align_right", "Ctrl+R"), ("Justify", "align_justify", "Ctrl+J")):
        add("Format", title, name, key, check=name != "align_justify")
    for value in (1.0, 1.15, 1.5, 2.0, 2.5, 3.0, None):
        add("Format/Line Spacing", "Custom…" if value is None else str(value), "spacing_menu_chosen", value=value)
    for i, title in enumerate(("One", "Two", "Three", "Four"), 1):
        add("Format/Columns", title, "columns_menu_chosen", value=i, id=f"columns:{i}", check=True)
    section("Format", [None, ("Background…", "format_background")])
    from .deck_themes import DeckTheme
    for theme in DeckTheme.all():
        action = add("Format/Theme", theme.name, "theme_chosen", value=theme.id, id=f"theme:{theme.id}", check=True)
        action.setIcon(theme.swatch)
    section("Format", [None, ("Apply Dark Mode to Deck…", "apply_dark_mode"), ("Apply Inverted Colors to Deck…", "apply_invert_colors")])
    section("Arrange", [("Bring to Front", "bring_to_front", "Ctrl+Shift+F"), ("Bring Forward", "bring_forward", "Ctrl+Alt+Shift+F"),
        ("Send Backward", "send_backward", "Ctrl+Alt+Shift+B"), ("Send to Back", "send_to_back", "Ctrl+Shift+B"), None,
        ("Group", "group_objects", "Ctrl+G"), ("Ungroup", "ungroup_objects", "Ctrl+Shift+G"), None,
        ("Apply Move to All Builds", "spread_last_move", "Ctrl+Shift+A")])
    for title, name, key in (("Show Objects", "toggle_objects", "Ctrl+Alt+J"), ("Grid Lines", "toggle_grid", "Ctrl+Alt+G"),
                             ("Ruler", "toggle_ruler", "Ctrl+Alt+U"), ("Snap to Objects", "toggle_snap", "Ctrl+Alt+S")):
        add("View", title, name, key, check=name != "toggle_objects")
    section("View", [("Add Vertical Guide", "add_vertical_guide"), ("Add Horizontal Guide", "add_horizontal_guide"), ("Clear Guides", "clear_guides")])
    add("View", "Show Unused Slides", "toggle_unused_slides", check=True)
    add("View", "Speaker Notes", "toggle_notes_pane", "Ctrl+Alt+K", check=True)
    add("View", "Show Tips", "toggle_tips", check=True)
    separator("View")
    for title, name, key in (("Dark Mode", "toggle_dark_mode", "Ctrl+Alt+Shift+D"), ("Invert Colors", "toggle_invert_colors", "Ctrl+Alt+Shift+I"),
                             ("Recolor Images Too", "toggle_dark_images", None)):
        add("View", title, name, key, check=True)
    section("View", [None, ("Zoom In", "zoom_in", "Ctrl+="), ("Zoom Out", "zoom_out", "Ctrl+-"), ("Fit in Window", "zoom_fit", "Ctrl+0"), None,
        ("Show Terminal", "toggle_terminal", "Ctrl+Alt+T"), ("New Terminal", "new_terminal", "Ctrl+Alt+Shift+T"), ("Split Terminal", "split_terminal", "Ctrl+\\")])
    add("View", "Terminal Starts in Working Copy", "toggle_terminal_in_work_copy", check=True)
    add("View", "Engine Log", "toggle_engine_log", check=True)
    section("View", [None, ("Hide/Show Slide Navigator", "toggle_navigator", "Ctrl+Alt+N"),
        ("Hide/Show Inspector and Source", "toggle_inspector", "Ctrl+Alt+I"), ("Auxiliary Pane", "toggle_pane_overlay", "Ctrl+Alt+F12")])
    section("Play", [("Show Animations", "toggle_show_animations", "Ctrl+Alt+A"), None,
        ("Play from Beginning", "play_menu_from_beginning", "F5"), ("Play from Current Slide", "play_menu_from_current", "Shift+F5"),
        ("Presenter View", "play_menu_presenter", "Ctrl+F5"), ("Scripted Presentation", "auto_present_from_beginning"),
        ("Scripted Presentation from Current Slide", "auto_present_from_current"), None,
        ("Preview Current Slide’s Animations", "play_current_slide_animations", "Alt+F5")])
    for title, height in (("720p (fastest builds)", 720), ("1080p", 1080), ("1440p", 1440), ("4K (slowest builds)", 2160)):
        add("Play/Animation Resolution", title, "animation_resolution_chosen", value=height, id=f"animation_height:{height}", check=True)
    section("Window", [("Minimize", "perform_miniaturize", "Ctrl+M"), ("Zoom", "perform_zoom"), None, ("Bring All to Front", "arrange_in_front")])
    # Toolbar/context-only commands still share the same registry.
    add(None, "New Slide", "new_slide")
    add(None, "Expand All Builds", "toggle_expand_all_builds")
    add(None, "Share PDF (one page per slide)…", "share_chosen", value=0, id="share_pdf:slides")
    add(None, "Share PDF (every build)…", "share_chosen", value=1, id="share_pdf:builds")
    controller.bound_menu_names = tuple(sorted(bound | {"open_recent"}))
    registry._chrome_placements = placements
    return registry


def build_menus(controller, registry=None):
    registry = registry or getattr(controller, "actions", None)
    if registry is None or "show_about" not in registry:
        registry = register_actions(controller, registry)
    menus = registry.build_menus(controller.window.menuBar(), MENU_ORDER)
    # Registry owns the actions; preserve the source menu's interleaving of
    # direct actions and submenus rather than appending submenus at the bottom.
    for path, entries in registry._chrome_placements.items():
        menu = menus[path]
        for action in menu.actions():
            menu.removeAction(action)
            if action.isSeparator():
                action.deleteLater()
        for kind, value in entries:
            if kind == "menu":
                menu.addMenu(menus[value])
            elif kind == "action":
                menu.addAction(registry.action(value))
            else:
                menu.addSeparator()
    layouts = menus["Slide/New Slide"]
    set_menu_icon_size(layouts, LAYOUT_ICON_SIZE)

    def refresh_layouts():
        background = getattr(controller.canvas, "shown_background", None) or "#FFFFFF"
        for _, layout in LAYOUTS:
            registry.action(f"new_slide_layout:{layout}").setIcon(layout_icon(layout, background))

    layouts.aboutToShow.connect(refresh_layouts)
    if "Format/Theme" in menus:
        set_menu_icon_size(menus["Format/Theme"], THEME_SWATCH_SIZE)
    controller.menus = menus
    recent = menus["File/Open Recent"]
    recent.setToolTipsVisible(True)
    set_menu_icon_size(recent, QSize(16, 16))
    recent.aboutToShow.connect(lambda: _populate_recents(controller, recent))
    return menus


def _populate_recents(controller, menu):
    menu.clear()
    from .projects import RecentDecks
    paths = RecentDecks.paths(getattr(controller, "settings", None))
    provider = QFileIconProvider()
    for path in paths[:RecentDecks.limit]:
        p = Path(path)
        info = QFileInfo(str(p))
        if not info.exists():
            continue
        title = f"{p.parent.name} / {p.name}".replace("&", "&&")
        action = menu.addAction(provider.icon(info), title)
        action.setIconVisibleInMenu(True)
        action.setData(str(p))
        action.setToolTip(str(p))
        action.triggered.connect(lambda _=False, a=action: controller._menu_router.invoke("open_recent", a))
    has_paths = bool(menu.actions())
    if has_paths:
        menu.addSeparator()
    clear = controller.actions.action("clear_recent")
    clear.setData(has_paths)
    clear.setEnabled(has_paths)
    menu.addAction(clear)


class MenusMixin:
    def show_about(self):
        QMessageBox.about(self.window, "About Manimble", f"Manimble {version()}\n\nLicense: BSD-2\nDeveloped by Joseph Rich and Lior Pachter. 2026")

    def hide(self):
        self.window.hide()

    def terminate(self):
        self.app.quit()

    def perform_close(self):
        self.window.close()

    def perform_miniaturize(self):
        self.window.showMinimized()

    def perform_zoom(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def arrange_in_front(self):
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible():
                widget.raise_()
        self.window.activateWindow()

    def validate_menu_item(self, action):
        return validate_action(self, action.property("handler"), action)

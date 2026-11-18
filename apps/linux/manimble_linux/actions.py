"""One registry of commands for menus, toolbars, context menus, keyboard shortcuts and enablement.

Every command is registered once::

    reg.add("slide.new", "New Slide", self.new_slide, shortcut="Ctrl+Shift+N", menu="Slide",
            enabled=lambda: self.has_deck)

and then appears wherever it is placed: ``menu`` (a top-level menu title, with ``/`` for submenus, e.g.
``"Arrange/Align"``), ``reg.action(id)`` for toolbars and context menus. ``refresh()`` re-evaluates ``enabled`` /
``checked`` callables (the editor calls it when its state changes and before a menu opens).

``scope``: ``"deck"`` commands are deck-editing accelerators; while the terminal has focus they are disabled, so a
key the terminal needs (Ctrl+C, Ctrl+Z, Ctrl+D, ...) reaches the terminal instead. ``"global"`` commands keep their
shortcuts everywhere (show/new terminal, window commands).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QWidget


@dataclass
class Command:
    id: str
    action: QAction
    menu: str | None
    scope: str
    enabled: Callable[[], bool] | None
    checked: Callable[[], bool] | None
    shortcuts: list[QKeySequence] = field(default_factory=list)


class ActionRegistry(QObject):
    SEPARATOR = "-"

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.window = window
        self.commands: dict[str, Command] = {}
        self.menu_items: dict[str, list[str]] = {}       # menu path -> command ids / SEPARATOR, in order
        self.menu_order: list[str] = []
        self.terminal_focused = False

    def add(self, id: str, text: str, slot: Callable | None = None, *, shortcut: str | list[str] | None = None,
            menu: str | None = None, scope: str = "deck", enabled: Callable[[], bool] | None = None,
            checkable: bool = False, checked: Callable[[], bool] | None = None, tip: str | None = None,
            role: QAction.MenuRole | None = None) -> QAction:
        if id in self.commands:
            raise ValueError(f"command {id} registered twice")
        a = QAction(text, self.window)
        a.setObjectName(id)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        keys = [shortcut] if isinstance(shortcut, str) else list(shortcut or [])
        seqs = [QKeySequence(k) for k in keys]
        if seqs:
            a.setShortcuts(seqs)
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        if checkable or checked is not None:
            a.setCheckable(True)
        if role is not None:
            a.setMenuRole(role)
        if slot is not None:
            if a.isCheckable():
                a.triggered.connect(lambda on=False, s=slot: s())
            else:
                a.triggered.connect(lambda _=False, s=slot: s())
        self.window.addAction(a)                         # shortcuts work with the menu bar hidden too
        self.commands[id] = Command(id, a, menu, scope, enabled, checked, seqs)
        if menu:
            self._place(menu, id)
        return a

    def separator(self, menu: str) -> None:
        self._place(menu, self.SEPARATOR)

    def _place(self, menu: str, item: str) -> None:
        if menu not in self.menu_items:
            self.menu_items[menu] = []
            self.menu_order.append(menu)
        self.menu_items[menu].append(item)

    def action(self, id: str) -> QAction:
        return self.commands[id].action

    def __contains__(self, id: str) -> bool:
        return id in self.commands

    def trigger(self, id: str) -> None:
        a = self.action(id)
        if a.isEnabled():
            a.trigger()

    # ----------------------------------------------------------------- state

    def refresh(self) -> None:
        for c in self.commands.values():
            on = True
            if c.enabled is not None:
                try:
                    on = bool(c.enabled())
                except Exception:                      # noqa: BLE001  (state mid-change)
                    on = False
            c.action.setEnabled(on)
            if c.checked is not None:
                try:
                    c.action.setChecked(bool(c.checked()))
                except Exception:                      # noqa: BLE001
                    pass
            self._apply_scope(c)

    def set_terminal_focused(self, on: bool) -> None:
        if on == self.terminal_focused:
            return
        self.terminal_focused = on
        for c in self.commands.values():
            self._apply_scope(c)

    def _apply_scope(self, c: Command) -> None:
        # with the terminal focused, deck accelerators are taken off (the menu item still works by mouse)
        want = [] if (self.terminal_focused and c.scope == "deck") else c.shortcuts
        if list(c.action.shortcuts()) != want:
            c.action.setShortcuts(want)

    # ----------------------------------------------------------------- menus

    def build_menus(self, bar: QMenuBar, order: list[str] | None = None) -> dict[str, QMenu]:
        """Fill ``bar`` from the registered placements; top-level menus in ``order`` first."""
        menus: dict[str, QMenu] = {}
        tops = list(order or [])
        for path in self.menu_order:
            top = path.split("/")[0]
            if top not in tops:
                tops.append(top)
        for top in tops:
            m = bar.addMenu(top)
            m.aboutToShow.connect(self.refresh)
            menus[top] = m
        for path in self.menu_order:
            parts = path.split("/")
            parent = menus[parts[0]]
            for i in range(1, len(parts)):
                key = "/".join(parts[: i + 1])
                if key not in menus:
                    sub = parent.addMenu(parts[i])
                    sub.aboutToShow.connect(self.refresh)
                    menus[key] = sub
                parent = menus[key]
            for item in self.menu_items[path]:
                if item == self.SEPARATOR:
                    parent.addSeparator()
                else:
                    parent.addAction(self.action(item))
        return menus

    def context_menu(self, ids: list[str], parent: QWidget | None = None) -> QMenu:
        self.refresh()
        m = QMenu(parent or self.window)
        for i in ids:
            if i == self.SEPARATOR:
                m.addSeparator()
            elif i in self.commands:
                m.addAction(self.action(i))
        return m

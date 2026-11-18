"""The deck window's core members that feature modules (mixins and widgets) rely on.

``controller.MainController`` is the port of MainController.swift. Swift spreads it over many files with
``extension MainController { ... }``; the Linux port does the same with *mixins*: each feature module defines
``class FooMixin:`` whose methods use ``self`` as the controller, and ``MainController`` inherits them all. Names are
the Swift names converted mechanically to snake_case (``sceneName`` → ``scene_name``, ``@objc func newSlide`` →
``new_slide``), so a feature module and the core agree without coordinating.

This class documents the core members (implemented in controller.py) and is a base of MainController; it holds no
behaviour. Threading: everything runs on the GUI thread. Generators are *task steps* (tasks.py): ``r = yield from
self.run_edit(req)`` inside a task, or ``spawn(self.reload_state())``.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generator

if TYPE_CHECKING:                                   # pragma: no cover
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMainWindow, QWidget

    from .actions import ActionRegistry
    from .canvas import CanvasView
    from .paths import AppPaths
    from .rpc import RpcClient
    from .runtime import Runtime
    from .widgets import Pill
    from .workspace import Workspace


@dataclass(frozen=True)
class EditContext:
    """Where an edit was asked for: slide, build and the render (``seen``, its directory basename) its object ids
    come from. Captured when the user acts; queued work runs later with it."""
    scene: str | None
    stop: int
    seen: str | None


class DeckHost:
    # ----------------------------------------------------------------- services
    window: "QMainWindow"
    rpc: "RpcClient"                    # Engine.swift: ``yield self.call(cmd, args)`` for a reply dict
    runtime: "Runtime"
    paths: "AppPaths"
    settings: "QSettings"               # prefs.py helpers read/write it with the Swift UserDefaults keys
    actions: "ActionRegistry"
    canvas: "CanvasView"
    toast_pill: "Pill"
    busy_pill: "Pill"
    workspace: "Workspace | None"       # the working copy (workspace.py); ``workspace.work`` is the engine's deck

    # ----------------------------------------------------------------- state (as in MainController.swift)
    state: dict | None                  # last ``state`` reply
    scene: dict | None                  # last ``scene`` reply of the slide on screen
    scene_name: str | None
    stop_index: int
    selection: int | None               # primary selected object; ``canvas.selected_ids`` for all of them
    busy: int                           # > 0 shows the busy pill; ``busy_text`` its text
    busy_text: str
    shown_dir: str | None               # render directory of the frames on screen

    @property
    def edit_context(self) -> EditContext: ...

    @property
    def submit_context(self) -> EditContext | None: ...

    @property
    def obj_info(self) -> dict | None:
        """``scene["objects"][str(selection)]``."""

    # ----------------------------------------------------------------- engine
    def call(self, cmd: str, args: dict | None = None) -> Future: ...
    def submit(self, make: Callable[[], Generator]) -> Future:
        """Queue mutating work: runs strictly after everything submitted before, with ``submit_context`` set to the
        edit context captured now. ``make`` returns a generator (task)."""
    def run_edit(self, req: dict) -> Generator:
        """Task step: one ``edit``; fills scene/stop/seen from the context, toasts the outcome, reloads. -> reply"""
    def as_one_undo_step(self, label: str, body: Callable[[], None]) -> None: ...
    def finish_edits(self) -> Generator: ...
    def reload_state(self) -> Generator: ...
    def load_scene(self, name: str, keep_stop: bool, keep_overrides: bool = False) -> Generator: ...
    def select_scene(self, name: str) -> Generator: ...
    def go_to_stop(self, i: int) -> None: ...

    # ----------------------------------------------------------------- user feedback
    def toast(self, s: str, error: bool = False) -> None: ...
    def alert(self, title: str, text: str) -> None: ...

    # ----------------------------------------------------------------- window areas
    def show_inspector_page(self, widget: "QWidget | None") -> None:
        """``widget`` in the inspector's place (BioCartoon search, feedback list); None restores the inspector."""

    @property
    def work_deck(self) -> Path | None:
        """``workspace.work`` (the working deck folder) or None."""

    @property
    def original_deck(self) -> Path | None:
        """``workspace.original`` or None."""

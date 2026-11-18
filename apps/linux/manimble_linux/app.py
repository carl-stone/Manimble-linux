"""Application lifecycle; one engine and one isolated workspace per deck window."""
from __future__ import annotations
import os
from pathlib import Path
import sys
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from .paths import AppPaths, version
from . import prefs
from .tasks import spawn, when_done


class ManimbleApp(QApplication):
    def __init__(self, argv=None):
        for key in ('PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            os.environ.pop(key, None)
        os.environ['QT_API'] = 'pyside6'
        super().__init__(argv or [sys.argv[0]])
        self.setApplicationName('Manimble')
        self.setOrganizationName('org.pachterlab')
        self.setApplicationVersion(version())
        self.setDesktopFileName('manimble')
        self.setWindowIcon(QIcon.fromTheme('manimble'))
        self.setQuitOnLastWindowClosed(False)
        import pillow_heif
        pillow_heif.register_heif_opener()
        self.paths = AppPaths.from_environment()
        self.settings = prefs.open_settings(self.paths)
        self.controllers = []
        self._quitting = False
        self._last_main = None
        self.applicationStateChanged.connect(lambda *_: self._track_active())
        self.focusWindowChanged.connect(lambda *_: self._track_active())

    @property
    def controller(self):
        return self._last_main if self._last_main in self.controllers else (self.controllers[-1] if self.controllers else None)

    def _track_active(self):
        active = self.activeWindow()
        self._last_main = next((c for c in self.controllers if c.window is active), self._last_main)

    @property
    def recent_decks(self):
        from .projects import RecentDecks
        return RecentDecks.paths(self.settings)

    def clear_recent(self):
        from .projects import RecentDecks
        RecentDecks.clear(self.settings)

    def new_window(self):
        from .controller import MainController
        controller = MainController(paths=self.paths, settings=self.settings, app=self)
        previous = self.controller
        self.controllers.append(controller)
        if previous:
            controller.window.move(previous.window.pos().x() + 26, previous.window.pos().y() + 26)
        self._last_main = controller
        controller.show_window()
        controller.start()
        return controller

    def show(self, deck, from_controller=None):
        target = Path(deck).expanduser().resolve()
        for controller in self.controllers:
            if controller.original_deck == target or (controller.pending_deck and Path(controller.pending_deck).resolve() == target):
                controller.window.show()
                controller.window.raise_()
                controller.window.activateWindow()
                return controller
        controller = from_controller or self.controller
        if controller is None or controller.workspace is not None or controller.pending_deck is not None:
            controller = self.new_window()
        controller.open(str(target), confirmed=True)
        return controller

    def open_path(self, path):
        controller = self.controller or self.new_window()
        controller.open_project(str(Path(path).expanduser().absolute()))

    def event(self, event):
        if event.type() == QEvent.Type.FileOpen:
            self.open_path(event.file())
            return True
        return super().event(event)

    def window_closed(self, controller):
        if controller in self.controllers:
            self.controllers.remove(controller)
        if self._last_main is controller:
            self._last_main = None
        if not self.controllers and not self._quitting:
            # Linux has no persistent application menu once all windows close.
            super().quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        def confirm():
            try:
                for controller in list(self.controllers):
                    if not (yield from controller.confirm_close('quitting')):
                        return
                for controller in list(self.controllers):
                    controller._poll_timer.stop()
                    controller.stop_feature_workers()
                    if controller.terminal.has_started:
                        yield controller.terminal.stop_all()
                    if controller.rpc:
                        controller.rpc.stop()
                    if controller.workspace:
                        from .workers import run_blocking
                        yield run_blocking(controller.workspace.discard)
                        controller.workspace = None
                    controller.window._approved_close = True
                    controller.window.close()
                self.controllers.clear()
                super(ManimbleApp, self).quit()
            except Exception as exc:
                if self.controller:
                    self.controller.alert('Cannot quit yet', str(exc))
            finally:
                self._quitting = False
        spawn(confirm())

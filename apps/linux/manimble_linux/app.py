"""Application lifecycle; one engine and one isolated workspace per deck window."""
from __future__ import annotations
import os
from pathlib import Path
import sys
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout
from .paths import AppPaths, app_root, version
from .fonts import setup_deck_fonts
from .style import Theme
from . import prefs
from .tasks import spawn, when_done

def show_about(parent):
    app = QApplication.instance()
    dialog = getattr(app, "_about_dialog", None)
    if dialog is not None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return
    dialog = QDialog(parent)
    app._about_dialog = dialog
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.destroyed.connect(lambda: setattr(app, "_about_dialog", None))
    dialog.setWindowTitle("About Manimble")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 20, 24, 24)
    layout.setSpacing(8)
    logo = QLabel()
    logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon = app.windowIcon()
    if icon.isNull():
        icon = QIcon(str(app_root() / 'apps/macos/assets/Manimble.icns'))
    logo.setPixmap(icon.pixmap(QSize(96, 96), dialog.devicePixelRatioF()))
    layout.addWidget(logo)
    for text, font in (
        ("Manimble", Theme.font(20, QFont.Weight.Bold)),
        (app.applicationVersion() or version(), Theme.font(12)),
        ("License: BSD-2\nDeveloped by Joseph Rich and Lior Pachter. 2026", Theme.font(11)),
    ):
        label = QLabel(text)
        label.setFont(font)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
    dialog.show()


class ManimbleApp(QApplication):
    def __init__(self, argv=None):
        self._launch_paths = []
        for key in ('PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            os.environ.pop(key, None)
        os.environ['QT_API'] = 'pyside6'
        super().__init__(argv or [sys.argv[0]])
        self.setApplicationName('Manimble')
        self.setOrganizationName('org.pachterlab')
        self.setApplicationVersion(version())
        self.setDesktopFileName('manimble')
        self.setWindowIcon(QIcon(str(app_root() / 'apps/macos/assets/Manimble.icns')))
        self.setQuitOnLastWindowClosed(False)
        import pillow_heif
        pillow_heif.register_heif_opener()
        self.paths = AppPaths.from_environment()
        setup_deck_fonts(self.paths)
        self.setFont(Theme.font(13))
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
        controller._choose_on_start = True
        self.controllers.append(controller)
        if previous:
            controller.window.move(previous.window.pos().x() + 26, previous.window.pos().y() + 26)
        self._last_main = controller
        controller.show_window()
        for path in self._launch_paths:
            controller.open_project(path)
        self._launch_paths.clear()
        controller._app_startup = controller.start()
        return controller

    def show(self, deck, from_controller=None):
        if self._quitting:
            return None
        target = Path(deck).expanduser().resolve()
        for controller in self.controllers:
            pending = controller.pending_deck
            if controller.window._approved_close or controller._closing:
                continue
            if (controller.original_deck == target or getattr(controller, "_app_opening_deck", None) == target or
                    (pending and Path(pending).resolve() == target)):
                controller.window.show()
                controller.window.raise_()
                controller.window.activateWindow()
                return controller
        controller = from_controller if from_controller in self.controllers else self.controller
        if (controller is None or controller.window._approved_close or controller._closing or
                controller.workspace is not None or controller.pending_deck is not None or
                getattr(controller, "_app_opening_deck", None) is not None):
            controller = self.new_window()
        controller._choose_on_start = False
        self.open_deck_in(controller, str(target))
        return controller

    def open_deck_in(self, controller, deck):
        if controller._closing or controller.window._approved_close or self._quitting:
            return None
        future = controller.open(deck, confirmed=True)
        if controller.pending_deck is not None:
            return future
        controller._app_opening_deck = Path(deck)
        controller._app_opening_future = future

        def completed(_):
            if controller._app_opening_future is future:
                controller._app_opening_deck = controller._app_opening_future = None

        when_done(future, completed)
        return future

    def open_path(self, path):
        if not path or self._quitting:
            return
        controller = self.controller or self.new_window()
        controller.open_project(str(Path(path).expanduser().absolute()))

    def event(self, event):
        if event.type() == QEvent.Type.FileOpen:
            path = event.file() or event.url().toLocalFile()
            if path:
                if getattr(self, "controllers", None):
                    self.open_path(path)
                else:
                    self._launch_paths.append(path)
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
                    if controller.workspace or controller.source_draft is not None:
                        controller.window.show()
                        controller.window.raise_()
                        controller.window.activateWindow()
                    if not (yield from controller.confirm_close('quitting')):
                        return
                for controller in list(self.controllers):
                    controller._closing = True
                    controller.stop_feature_workers()
                    yield from controller.finish()
                    controller._connection_epoch += 1
                    controller.window._approved_close = True
                    controller.window.close()
                self.controllers.clear()
                super(ManimbleApp, self).quit()
            except Exception as exc:
                if self.controller:
                    from .project_controller import project_alert
                    project_alert(self.controller.window, 'Cannot quit yet', str(exc))
            finally:
                for controller in self.controllers:
                    controller._closing = False
                self._quitting = False
        spawn(confirm())

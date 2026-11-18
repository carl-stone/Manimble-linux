"""Local-only xterm widgets backed by separately spawned, single-threaded PTY owners.

Hiding a panel never stops a shell. The project owner must obtain stop consent,
then await ``stop_all()`` before deleting a working copy.
"""
from __future__ import annotations

import base64
from concurrent.futures import Future
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                    QWebEngineSettings, QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox,
                               QFontComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from . import prefs
from .drawer import DrawerIconButton
from .paths import RESOURCES
from .pty_helper import login_shell, terminal_directory
from .style import Theme, is_dark, watch_appearance
from .widgets import add_copy_button

_RESOURCE_DIR = (RESOURCES / "terminal").resolve()

# Swift specifies the default ink and paper; its terminal renderer supplies these ANSI roles.
_ANSI_NAMES = (
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue",
    "brightMagenta", "brightCyan", "brightWhite",
)
_ANSI_LIGHT = (
    "#242424", "#a12430", "#28610d", "#755900", "#17569c", "#873887", "#0b656b", "#dedede",
    "#5f5f5f", "#bf303f", "#367621", "#866800", "#1b65b5", "#9e439e", "#12757d", "#ffffff",
)
_ANSI_DARK = (
    "#242424", "#ef767d", "#91c776", "#d9b968", "#79b2f8", "#d79ed9", "#73c9cf", "#cccccc",
    "#808080", "#ff9aa1", "#ade38f", "#f3d58b", "#a2caff", "#f1b7f1", "#92e2e9", "#ffffff",
)


def _saved_font_family() -> str:
    return prefs.get_str("terminal.font", Theme.mono(12).family())


def _saved_font_size() -> int | None:
    return prefs.get_int("terminal.fontSize") or None


def _local_url(url: QUrl) -> bool:
    if url.scheme() == "qrc":
        return url.path() == "/qtwebchannel/qwebchannel.js"
    if not url.isLocalFile():
        return False
    try:
        return Path(url.toLocalFile()).resolve().is_relative_to(_RESOURCE_DIR)
    except (OSError, ValueError):
        return False


class _LocalRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        info.block(not _local_url(info.requestUrl()))


class _LocalPage(QWebEnginePage):
    diagnostic = Signal(str)

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        # Even local terminal output cannot navigate to a file or open a website.
        return url.isLocalFile() and Path(url.toLocalFile()).resolve() == _RESOURCE_DIR / "terminal.html"

    def createWindow(self, window_type):
        return None

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.diagnostic.emit(f"Terminal page: {message} ({source_id}:{line_number})")

    def certificateError(self, error):
        error.rejectCertificate()


class _Bridge(QObject):
    command = Signal(str)

    def __init__(self, session):
        super().__init__(session)
        self.session = session

    @Slot()
    def ready(self):
        session = self.session
        session.page_ready = True
        session._send_appearance()
        for data in session._pending_output:
            session._emit("write", data=data)
        session._pending_output.clear()
        if session._focus_pending and session.isVisible():
            session.focus()

    @Slot(str)
    def input(self, data):
        try:
            base64.b64decode(data, validate=True)
        except ValueError:
            return
        self.session._send({"op": "input", "data": data})

    @Slot(int, int)
    def resize_terminal(self, cols, rows):
        self.session.columns, self.session.rows = cols, rows
        self.session._send({"op": "resize", "cols": cols, "rows": rows})

    @Slot(bool)
    def focus_changed(self, focused):
        self.session.focused.emit(focused)

    @Slot(str)
    def title_changed(self, title):
        if title and title != self.session.shell_title:
            self.session.shell_title = title
            self.session._title_changed()

    @Slot(str)
    def snapshot(self, text):
        self.session._contents = text

    @Slot(str)
    def copy_selection(self, text):
        QApplication.clipboard().setText(text)

    @Slot(str)
    def key_action(self, action):
        session = self.session
        if action == "paste":
            session._emit("paste", text=QApplication.clipboard().text())
        elif action == "find":
            session.find_requested.emit()
        elif action == "interrupt":
            session.interrupt()
        elif action == "suspend":
            session.suspend()
        elif action == "zoom_in":
            session.set_font(size=min(40, session.font_size + 1))
        elif action == "zoom_out":
            session.set_font(size=max(7, session.font_size - 1))
        elif action == "zoom_reset":
            session.reset_font_size()


class TerminalSession(QWidget):
    """One shell and xterm view; raw ``output`` is available for diagnostics/tests."""
    output = Signal(bytes)
    diagnostic = Signal(str)
    focused = Signal(bool)
    find_requested = Signal()
    stopped = Signal()

    def __init__(self, parent=None, *, python: str | Path | None = None,
                 environment: dict[str, str] | None = None):
        super().__init__(parent)
        self.python = str(python or sys.executable)
        self.environment = dict(environment if environment is not None else os.environ)
        self.directory = Path.home()
        self.original_repository = False
        self.original: Path | None = None
        self.on_title = None
        self.shell_title = ""
        self.commands: list[str] = []
        self.columns, self.rows = 80, 24
        self.font_family = _saved_font_family()
        self._font_size = _saved_font_size()
        self.page_ready = False
        self._focus_pending = False
        self._pending_output: list[str] = []
        self._contents = ""
        self._buffer = bytearray()
        self._stderr = bytearray()
        self._process: QProcess | None = None
        self._stop_future: Future | None = None
        self._restart = None
        self._highlighted = False
        self.setMinimumWidth(160)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.marker = QWidget(self)
        self.marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.marker.setStyleSheet(f"background: {Theme.accent.name()}")
        self.marker.hide()
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setFont(Theme.mono(10))
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.view = QWebEngineView(self)
        layout.addWidget(self.view)
        # Off-the-record profile: no persistent browser data or permission grants.
        self.profile = QWebEngineProfile(self)
        self.interceptor = _LocalRequests(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.page = _LocalPage(self.profile, self.view)
        self.view.setPage(self.page)
        settings = self.page.settings()
        for attribute in (
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,
            QWebEngineSettings.WebAttribute.PluginsEnabled,
            QWebEngineSettings.WebAttribute.FullScreenSupportEnabled,
            QWebEngineSettings.WebAttribute.ScreenCaptureEnabled,
        ):
            settings.setAttribute(attribute, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.channel = QWebChannel(self.page)
        self.bridge = _Bridge(self)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.permissionRequested.connect(lambda permission: permission.deny())
        self.page.fileSystemAccessRequested.connect(lambda request: request.reject())
        self.page.diagnostic.connect(self._error)
        self.page.renderProcessTerminated.connect(self._renderer_stopped)
        self.view.loadFinished.connect(self._loaded)
        watch_appearance(self._send_appearance)
        self._send_appearance()
        self.view.load(QUrl.fromLocalFile(str(_RESOURCE_DIR / "terminal.html")))

    @property
    def highlighted(self):
        return self._highlighted

    @highlighted.setter
    def highlighted(self, value):
        self._highlighted = bool(value)
        self.marker.setVisible(self._highlighted)
        if self._highlighted:
            self.marker.setGeometry(0, 0, self.width(), 2)
            self.marker.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.marker.setGeometry(0, 0, event.size().width(), 2)

    @property
    def process(self):
        return self._process

    def _loaded(self, success):
        if not success:
            self._error("The local terminal page could not load. Check QtWebEngine sandbox and display diagnostics. "
                        "On Ubuntu, install the explicitly approved profile for the exact QtWebEngineProcess path; "
                        "do not disable Chromium sandboxing.")

    def _renderer_stopped(self, status, code):
        self.page_ready = False
        self._error(f"QtWebEngine terminal renderer stopped ({status.name}, code {code}). "
                    "Check display/GPU access and the QtWebEngine sandbox profile; sandboxing must remain enabled.")

    def _error(self, text):
        self.error_label.setText(text)
        self.error_label.show()
        self.diagnostic.emit(text)

    def _emit(self, op, **data):
        self.bridge.command.emit(json.dumps({"op": op, **data}, ensure_ascii=True))

    def _send(self, message):
        if self._process is not None and self._process.state() == QProcess.ProcessState.Running:
            self._process.write(json.dumps(message).encode() + b"\n")

    def start(self, directory: str | Path, original: str | Path | None = None):
        """Start or asynchronously restart, never overlapping old and new PTYs."""
        self.directory = Path(directory).absolute()
        self.original = Path(original).absolute() if original is not None else None
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self.terminate()
            self._restart = (self.directory, self.original)
            return
        self._restart = None
        self.commands = []
        self.shell_title = ""
        self._buffer.clear()
        self._stderr.clear()
        self._stop_future = None
        process = QProcess(self)
        self._process = process
        env = QProcessEnvironment()
        for key, value in self.environment.items():
            env.insert(key, str(value))
        process.setProcessEnvironment(env)
        # Launch a file rather than -m so the helper also works without inherited PYTHONPATH.
        args = ["-u", str(Path(__file__).with_name("pty_helper.py")), "--cwd", str(self.directory)]
        if self.original_repository:
            args.append("--repository")
        if self.original is not None:
            args += ["--original", str(self.original)]
        process.setProgram(self.python)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(lambda: self._read_output(process))
        process.readyReadStandardError.connect(lambda: self._read_errors(process))
        process.started.connect(lambda: self._send({"op": "resize", "cols": self.columns, "rows": self.rows}))
        process.finished.connect(lambda code, status: self._finished(process, code))
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        process.start()
        self._title_changed()

    def _process_error(self, process, error):
        if process is self._process:
            self._error(f"Terminal helper: {process.errorString()}")
            if error == QProcess.ProcessError.FailedToStart:
                self._finished(process, -1)

    def _read_errors(self, process):
        data = bytes(process.readAllStandardError())
        self._stderr.extend(data)
        del self._stderr[:-16384]
        if data:
            self._error(data.decode("utf-8", "replace").strip())

    def _read_output(self, process):
        self._buffer.extend(bytes(process.readAllStandardOutput()))
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer[:] = rest
            try:
                message = json.loads(line)
                event = message["event"]
                if event == "output":
                    data = base64.b64decode(message["data"], validate=True)
                    self.output.emit(data)
                    self._write_screen(message["data"])
                elif event == "status":
                    self.commands = message.get("commands", [])
                elif event == "error":
                    self._error(message["message"])
                elif event == "exit":
                    self._write_screen(base64.b64encode(
                        "\r\n[process finished — press ↻ to start a new shell]\r\n".encode()).decode())
            except (ValueError, KeyError, TypeError) as exc:
                self._error(f"Invalid terminal helper reply: {exc}")

    def _write_screen(self, data):
        if self.page_ready:
            self._emit("write", data=data)
        else:
            self._pending_output.append(data)
            # Keep bounded diagnostics even when the renderer fails.
            if len(self._pending_output) > 256:
                del self._pending_output[0]

    def _finished(self, process, code):
        if process is not self._process:
            return
        self._read_output(process)
        self._read_errors(process)
        self.commands = []
        self._process = None
        process.deleteLater()
        if self._stop_future is not None and not self._stop_future.done():
            self._stop_future.set_result(None)
        self.stopped.emit()
        self._title_changed()
        if self._restart:
            directory, original = self._restart
            self._restart = None
            self.start(directory, original)

    def terminate(self) -> Future:
        """Request session-wide process-group shutdown; resolves after helper exit."""
        self._restart = None
        if self._stop_future is not None:
            return self._stop_future
        self._stop_future = Future()
        if self._process is None or self._process.state() == QProcess.ProcessState.NotRunning:
            self._stop_future.set_result(None)
        else:
            self._send({"op": "stop"})
            # SIGTERM is handled by the helper, which still owns and reaps its PTY child.
            process = self._process
            QTimer.singleShot(1500, lambda: process.terminate()
                              if self._process is process and process.state() != QProcess.ProcessState.NotRunning else None)
        return self._stop_future

    def interrupt(self):
        self._send({"op": "signal", "name": "INT"})

    def suspend(self):
        self._send({"op": "signal", "name": "TSTP"})

    def type(self, text: str):
        self._send({"op": "input", "data": base64.b64encode(text.encode("utf-8")).decode("ascii")})

    def contents(self) -> str:
        return self._contents

    def focus(self):
        self._focus_pending = True
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)
        if self.page_ready:
            self._emit("focus")
            self._focus_pending = False

    def _title_changed(self):
        if self.on_title:
            self.on_title()

    @Slot()
    def _send_appearance(self):
        background = Theme.code_background()
        self.error_label.setStyleSheet(
            f"color: {Theme.error_ink().name()}; background: {background.name()}; padding: 5px")
        self.page.setBackgroundColor(background)
        if self.page_ready:
            dark = is_dark()
            foreground = "#e6e6e6" if dark else "#1f1f1f"
            paper = background.name()
            theme = dict(zip(_ANSI_NAMES, _ANSI_DARK if dark else _ANSI_LIGHT))
            theme.update(
                background=paper, foreground=foreground,
                cursor=foreground, cursorAccent=paper,
                selectionBackground=Theme.warm.name() + ("5c" if dark else "3d"),
                selectionInactiveBackground="#ffffff24" if dark else "#0000001f",
            )
            self._send_settings(theme=theme, scheme="dark" if dark else "light")

    @property
    def font_size(self) -> float:
        return self._font_size if self._font_size is not None else Theme.logical_size(12)

    def _send_settings(self, **appearance):
        if self.page_ready:
            self._emit("settings", family=self.font_family, size=self.font_size, **appearance)

    def set_font(self, family: str | None = None, size: int | None = None):
        if family is not None:
            self.font_family = family
            prefs.set("terminal.font", family)
        if size is not None:
            self._font_size = max(7, min(40, int(size)))
            prefs.set("terminal.fontSize", self._font_size)
        self._send_settings()

    def reset_font_size(self):
        self._font_size = None
        prefs.remove("terminal.fontSize")
        self._send_settings()


@dataclass
class AIAgent:
    title: str
    command: str
    install: str


AI_AGENTS = (
    AIAgent("Claude Code", "claude", "curl -fsSL https://claude.ai/install.sh | bash"),
    AIAgent("Codex (OpenAI)", "codex", "npm install -g @openai/codex"),
    AIAgent("Gemini CLI (Google)", "gemini", "npm install -g @google/gemini-cli"),
)


@dataclass
class _TerminalTab:
    panes: QSplitter
    number: int
    sessions: list[TerminalSession] = field(default_factory=list)
    last_active: TerminalSession | None = None


class TerminalPanel(QWidget):
    """Tabs of splittable shells; ``start(work, original)`` sets both deck directories.

    ``environment`` and ``python`` may be set by the controller before start.
    ``tabs_changed`` reports shell titles and changes to the active tab.
    """
    output = Signal(bytes)
    diagnostic = Signal(str)
    tabs_changed = Signal()

    def __init__(self, controller=None, parent=None, *, python=None, environment=None):
        super().__init__(parent)
        self.controller = controller
        self.python = str(python or sys.executable)
        self.environment = dict(environment if environment is not None else os.environ)
        self.directory = Path.home()
        self._has_workspace = False
        self.original: Path | None = None
        self.tabs: list[_TerminalTab] = []
        self.active_tab: _TerminalTab | None = None
        self.active: TerminalSession | None = None
        self.installed: set[str] | None = None
        self._probes: list[QProcess] = []
        self._closing: set[TerminalSession] = set()
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.actions = QWidget(self)      # BottomDrawer places this in its header.
        toolbar = QHBoxLayout(self.actions)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(10)
        run_group = QWidget(self.actions)
        run_row = QHBoxLayout(run_group)
        run_row.setContentsMargins(0, 0, 0, 0)
        run_row.setSpacing(2)
        self.run_button = QToolButton(run_group)
        self.run_button.setFont(Theme.font(11, QFont.Weight.Medium))
        self.run_button.setAutoRaise(True)
        self.run_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.run_button.clicked.connect(self.run_agent)
        run_row.addWidget(self.run_button)
        menu_button_style = "QToolButton::menu-indicator { image: none; width: 0; }"
        chooser = DrawerIconButton("chevron.down", "Choose the AI coding agent", run_group, size=9, width=16)
        self.agent_menu = QMenu(chooser)
        self.agent_menu.aboutToShow.connect(self._update_agent_menu)
        chooser.setMenu(self.agent_menu)
        chooser.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        chooser.setStyleSheet(menu_button_style)
        run_row.addWidget(chooser)
        toolbar.addWidget(run_group)
        self.split_button = DrawerIconButton("square.split.2x1", "Split terminal (Ctrl+\\)", self.actions)
        self.split_button.clicked.connect(self.split_active)
        toolbar.addWidget(self.split_button)
        restart = DrawerIconButton("arrow.clockwise", "Restart shell", self.actions)
        restart.clicked.connect(self.restart_pressed)
        toolbar.addWidget(restart)
        kill = DrawerIconButton("trash", "Kill terminal", self.actions)
        kill.clicked.connect(self.kill_pressed)
        toolbar.addWidget(kill)
        self.location = QToolButton(self.actions)
        self.location.setFont(Theme.font(11))
        self.location.setAutoRaise(True)
        self.location.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.location.setAccessibleName("Terminal working directory")
        self.location.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.location.setStyleSheet(menu_button_style)
        self.location_menu = QMenu(self.location)
        self.location_menu.aboutToShow.connect(self._update_location_menu)
        self.location.setMenu(self.location_menu)
        toolbar.addWidget(self.location)
        options = DrawerIconButton("ellipsis", "Terminal Options", self.actions)
        options_menu = QMenu(options)
        options_menu.addAction("Find…", self.show_find)
        options_menu.addAction("Font and Size…", self.edit_font)
        options.setMenu(options_menu)
        options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        options.setStyleSheet(menu_button_style)
        toolbar.addWidget(options)
        self.actions.hide()
        self.find_bar = QWidget(self)
        find_layout = QHBoxLayout(self.find_bar)
        find_layout.setContentsMargins(4, 0, 4, 0)
        self.find_field = QLineEdit(self.find_bar)
        self.find_field.setPlaceholderText("Find in terminal scrollback")
        self.find_case = QCheckBox("Case", self.find_bar)
        find_layout.addWidget(self.find_field)
        find_layout.addWidget(self.find_case)
        for label, slot in (("Previous", lambda: self.find(True)), ("Next", lambda: self.find()),
                            ("Close", self.hide_find)):
            button = QPushButton(label, self.find_bar)
            button.clicked.connect(slot)
            find_layout.addWidget(button)
        self.find_field.returnPressed.connect(self.find)
        self.find_field.textChanged.connect(lambda: self.find())
        self.find_case.toggled.connect(lambda: self.find())
        self.find_bar.hide()
        layout.addWidget(self.find_bar)
        self.content = QStackedWidget(self)
        layout.addWidget(self.content, 1)
        self.warning = QLabel(self)
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.setFont(Theme.font(10))
        layout.addWidget(self.warning)
        self.update_run_button()
        self._update_warning()
        watch_appearance(self._refresh_appearance)
        self._refresh_appearance()
        # WebEngine's actual text focus lives in its internal widget. JS focus/blur
        # alone does not cover switching to another Qt window or a find field.
        QApplication.instance().focusChanged.connect(self._qt_focus_changed)

    @property
    def all_sessions(self):
        return [session for tab in self.tabs for session in tab.sessions]

    @property
    def tab_titles(self):
        return [(f"Terminal {tab.number}" + (f" ×{len(tab.sessions)}" if len(tab.sessions) > 1 else ""),
                 "  ·  ".join(session.shell_title for session in tab.sessions if session.shell_title))
                for tab in self.tabs]

    @property
    def active_tab_index(self):
        return self.tabs.index(self.active_tab) if self.active_tab in self.tabs else None

    @property
    def has_started(self):
        return bool(self.tabs)

    @property
    def count(self):
        return len(self.all_sessions)

    @property
    def tab_count(self):
        return len(self.tabs)

    @property
    def agent(self):
        return prefs.get_str("terminal.agent", "claude")

    @agent.setter
    def agent(self, value):
        prefs.set("terminal.agent", value)
        self.update_run_button()

    def update_run_button(self):
        command = self.agent
        words = command.split(maxsplit=1)
        name = words[0] if words else "agent"
        title = f"Run {name}"
        self.run_button.setText(self.run_button.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 132))
        self.run_button.setAccessibleName(title)
        self.run_button.setToolTip(f"Type `{command}` into the active terminal")

    def _refresh_appearance(self):
        paper = self.palette()
        paper.setColor(QPalette.ColorRole.Window, Theme.code_background())
        self.setPalette(paper)
        palette = self.run_button.palette()
        palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText, Theme.accent)
        palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.ButtonText, Theme.accent)
        self.run_button.setPalette(palette)
        self._refresh_warning_ink()

    def _refresh_warning_ink(self):
        palette = self.warning.palette()
        palette.setColor(QPalette.ColorRole.WindowText,
                         Theme.error_ink() if self.uses_repository else Theme.secondary_ink())
        self.warning.setPalette(palette)
        self.warning.setContentsMargins(6, 3, 6, 3)

    @property
    def starts_in_work_copy(self):
        return prefs.get_bool("terminalInWorkCopy", True)

    @property
    def uses_repository(self):
        return not self.starts_in_work_copy and self.original is not None

    def _cwd(self):
        return self.original if self.uses_repository else self.directory

    def _session_environment(self):
        env = dict(self.environment)
        env["MANIMBLE_DECK"] = str(self.directory)
        if self.original:
            env["MANIMBLE_ORIGINAL_DECK"] = str(self.original)
        return env

    def configure(self, directory: str | Path, original: str | Path | None = None):
        """Set the next shell's workspace; earlier workspace shells must be stopped."""
        directory = Path(directory).absolute()
        original = Path(original).absolute() if original is not None else None
        if (directory, original) != (self.directory, self.original):
            if any(session.process is not None for session in self.all_sessions):
                raise RuntimeError("Stop the previous workspace's terminals before changing their directory")
            for tab in self.tabs:
                self.content.removeWidget(tab.panes)
                tab.panes.deleteLater()
            self.tabs.clear()
            self.active = self.active_tab = None
            self.find_bar.hide()
        self.directory, self.original = directory, original
        self._has_workspace = True
        self._update_warning()
        self.refresh()

    @property
    def workspace_available(self):
        return self._has_workspace

    def start(self, directory: str | Path, original: str | Path | None = None):
        self.configure(directory, original)
        if not self.tabs:
            self.add_tab()
            self.check_installed()

    def add_tab(self):
        used = {tab.number for tab in self.tabs}
        number = next(i for i in range(1, len(used) + 2) if i not in used)
        tab = _TerminalTab(QSplitter(Qt.Orientation.Horizontal, self), number)
        tab.panes.setChildrenCollapsible(False)
        self.tabs.append(tab)
        self.content.addWidget(tab.panes)
        self.add_session(tab)

    def split_active(self):
        if self.active_tab is None:
            self.add_tab()
        else:
            self.add_session(self.active_tab)

    def add_session(self, tab):
        session = TerminalSession(tab.panes, python=self.python, environment=self._session_environment())
        session.original_repository = self.uses_repository
        session.on_title = self.refresh
        session.output.connect(self.output)
        session.diagnostic.connect(self.diagnostic)
        session.focused.connect(lambda focused: self._session_focused(session, focused))
        session.find_requested.connect(self.show_find)
        at = tab.sessions.index(self.active) + 1 if self.active in tab.sessions else len(tab.sessions)
        tab.sessions.insert(at, session)
        tab.panes.insertWidget(at, session)
        session.start(self._cwd(), self.original)
        self.activate(session, focus=True)
        QTimer.singleShot(0, lambda: tab.panes.setSizes([1000] * len(tab.sessions)))

    def activate(self, session, focus=False):
        for other in self.all_sessions:
            if other is not session:
                other._focus_pending = False
        self.active = session
        self.active_tab = next((tab for tab in self.tabs if session in tab.sessions), None)
        if self.active_tab:
            self.active_tab.last_active = session
            self.content.setCurrentWidget(self.active_tab.panes)
        self.refresh()
        if focus:
            session.focus()

    def refresh(self):
        for tab in self.tabs:
            for session in tab.sessions:
                session.highlighted = len(tab.sessions) > 1 and session is self.active
        self.tabs_changed.emit()

    def select_tab(self, index):
        if 0 <= index < len(self.tabs):
            tab = self.tabs[index]
            self.activate(tab.last_active or tab.sessions[0], focus=True)

    def _set_terminal_focused(self, focused):
        if self.controller is not None:
            self.controller.actions.set_terminal_focused(focused)

    def _session_focused(self, session, focused):
        widget = QApplication.focusWidget()
        owns_focus = widget is session.view or (widget is not None and session.view.isAncestorOf(widget))
        # A WebChannel focus notification may arrive after another tab was chosen.
        # Native widget focus, not the delayed DOM event, decides the active pane.
        if focused and owns_focus and session.isVisible():
            self.activate(session)
        self._set_terminal_focused(self._owns_focus(widget))

    def _qt_focus_changed(self, old, new):
        focused_session = next((session for session in self.all_sessions
                                if new is session.view or (new is not None and session.view.isAncestorOf(new))), None)
        if focused_session is not None and focused_session is not self.active:
            self.activate(focused_session)
        self._set_terminal_focused(self._owns_focus(new))

    def _owns_focus(self, widget):
        return widget is not None and (widget is self or self.isAncestorOf(widget) or
                                       widget is self.actions or self.actions.isAncestorOf(widget))

    def running_commands(self) -> list[str]:
        """Includes stopped/background jobs, which also lose their working copy on close."""
        return [command for session in self.all_sessions for command in session.commands]

    def stop_all(self) -> Future:
        """Resolve only when every helper has completed session-wide group shutdown."""
        result = Future()
        sessions = list(dict.fromkeys(self.all_sessions + list(self._closing)))
        futures = [session.terminate() for session in sessions]
        if not futures:
            result.set_result(None)
            return result
        def completed(_):
            if all(future.done() for future in futures) and not result.done():
                try:
                    for future in futures:
                        future.result()
                    result.set_result(None)
                except BaseException as exc:
                    result.set_exception(exc)
        for future in futures:
            future.add_done_callback(completed)
        return result

    def terminate_all(self):
        return self.stop_all()

    def _confirm_stop(self, sessions, action):
        commands = [command for session in sessions for command in session.commands]
        if not commands:
            return True
        box = QMessageBox(QMessageBox.Icon.Warning, action,
                          "These running or suspended commands will stop:\n\n" + "\n".join(commands),
                          QMessageBox.StandardButton.Cancel, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        stop = box.addButton("Stop Commands", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is stop

    def kill(self, session, *, confirmed=False):
        if not confirmed and not self._confirm_stop([session], "Close terminal?"):
            return
        tab = next((tab for tab in self.tabs if session in tab.sessions), None)
        if tab is None:
            return
        index = self.tabs.index(tab)
        tab.sessions.remove(session)
        session.hide()
        session.setParent(self)
        self._closing.add(session)
        future = session.terminate()
        def finished(_):
            self._closing.discard(session)
            session.deleteLater()
        future.add_done_callback(finished)
        if not tab.sessions:
            self.content.removeWidget(tab.panes)
            tab.panes.deleteLater()
            self.tabs.remove(tab)
        elif tab.last_active is session:
            tab.last_active = tab.sessions[0]
        if self.active is session:
            if self.tabs:
                next_tab = self.tabs[min(index, len(self.tabs) - 1)]
                self.activate(next_tab.last_active or next_tab.sessions[0], focus=True)
            else:
                self.active = None
                self.active_tab = None
                self.find_bar.hide()
                self.refresh()
        else:
            self.refresh()

    def close_tab(self, index):
        if not 0 <= index < len(self.tabs):
            return
        sessions = list(self.tabs[index].sessions)
        if self._confirm_stop(sessions, "Close terminal tab?"):
            for session in reversed(sessions):
                self.kill(session, confirmed=True)

    def restart_pressed(self):
        if self.active and self._confirm_stop([self.active], "Restart shell?"):
            self.active.environment = self._session_environment()
            self.active.original_repository = self.uses_repository
            self.active.start(self._cwd(), self.original)
            self.active.focus()

    def kill_pressed(self):
        if self.active:
            self.kill(self.active)

    def type(self, text):
        if self.active:
            self.active.type(text)

    def contents(self):
        return self.active.contents() if self.active else ""

    def focus(self):
        if self.active:
            self.active.focus()

    def hideEvent(self, event):
        for session in self.all_sessions:
            session._focus_pending = False
        self._set_terminal_focused(False)
        super().hideEvent(event)

    def show_find(self):
        self.find_bar.show()
        self.find_field.setFocus()
        self.find_field.selectAll()

    def hide_find(self):
        self.find_bar.hide()
        if self.active:
            self.active._emit("search", text="")
            self.active.focus()

    def find(self, previous=False):
        if self.active:
            self.active._emit("search", text=self.find_field.text(), previous=bool(previous),
                              caseSensitive=self.find_case.isChecked())

    def edit_font(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Terminal Font")
        form = QFormLayout(dialog)
        font = QFontComboBox(dialog)
        font.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        family = self.active.font_family if self.active else _saved_font_family()
        font.setCurrentFont(QFont(family))
        original_family = font.currentFont().family()
        form.addRow("Font", font)
        size = QSpinBox(dialog)
        size.setRange(7, 40)
        saved = self.active._font_size if self.active else _saved_font_size()
        size.setValue(saved if saved is not None else round(Theme.logical_size(12)))
        form.addRow("Size", size)
        use_default = QCheckBox("Use default size", dialog)
        use_default.setChecked(saved is None)
        use_default.toggled.connect(size.setDisabled)
        size.setDisabled(use_default.isChecked())
        form.addRow(use_default)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = font.currentFont().family()
        new_family = chosen if chosen != original_family else None
        new_size = None if use_default.isChecked() else size.value()
        for session in self.all_sessions:
            if new_family is not None or new_size is not None:
                session.set_font(new_family, new_size)
            if new_size is None and session._font_size is not None:
                session.reset_font_size()
        self.focus()

    def _update_warning(self):
        repository = self.uses_repository
        name = "Original folder" if repository else "Working copy"
        directory = terminal_directory(self._cwd(), repository)
        self.location.setText(name)
        location = f"{name} — starts in {directory}" if self._has_workspace else name
        self.location.setToolTip(location)
        self.location.setAccessibleDescription(location)
        self._refresh_warning_ink()
        if repository:
            self.warning.setText("Original-folder shell: commands bypass Save isolation and can change original files immediately.")
        else:
            self.warning.setText("Working-copy shell. Authored symlinks and external assets are not sandboxed; commands can modify their targets.")

    def _update_location_menu(self):
        self.location_menu.clear()
        for title, work in (("Working copy", True), ("Original folder", False)):
            action = self.location_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(work != self.uses_repository)
            action.setEnabled(work or self.original is not None)
            action.triggered.connect(lambda _=False, enabled=work: self.set_start_in_work_copy(enabled))

    def set_start_in_work_copy(self, enabled: bool):
        """Do not switch directories or stop a shell without the existing consent."""
        if enabled == self.starts_in_work_copy:
            return
        if not enabled:
            if self.original is None:
                QMessageBox.warning(self, "No original folder", "Open a deck before using its original folder.")
                return
            directory = terminal_directory(self.original, True)
            answer = QMessageBox.warning(self, "Use original folder?",
                f"Commands will start in {directory} and bypass Save isolation. Changes are immediate and cannot be reverted by Manimble. All open shells will restart.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
        if not self._confirm_stop(self.all_sessions, "Restart terminals in another folder?"):
            return
        prefs.set("terminalInWorkCopy", enabled)
        self._update_warning()
        for session in self.all_sessions:
            session.environment = self._session_environment()
            session.original_repository = self.uses_repository
            session.start(self._cwd(), self.original)
        if self.controller:
            self.controller.toast("New terminals open in the working copy (what the slides show)" if enabled else
                                  "New terminals open in the project repository")
            self.controller.actions.refresh()

    def _update_agent_menu(self):
        self.agent_menu.clear()
        for agent in AI_AGENTS:
            suffix = "  (not installed)" if self.installed is not None and agent.command not in self.installed else ""
            action = self.agent_menu.addAction(agent.title + suffix)
            action.setCheckable(True)
            action.setChecked(self.agent == agent.command)
            action.triggered.connect(lambda _=False, command=agent.command: self.choose_agent(command))
        self.agent_menu.addSeparator()
        custom = all(agent.command != self.agent for agent in AI_AGENTS)
        title = f"Custom: {self.agent.replace('&', '&&')}…" if custom else "Custom Command…"
        action = self.agent_menu.addAction(title, self.custom_agent)
        action.setCheckable(True)
        action.setChecked(custom)

    def choose_agent(self, command):
        self.agent = command            # Run remains a separate, explicit action.

    def custom_agent(self):
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Custom agent command")
        dialog.setLabelText("Typed into the active terminal by the Run button, e.g. `aider` or `claude --model opus`.")
        dialog.setTextValue(self.agent)
        dialog.setOkButtonText("Use")
        if dialog.exec() == QDialog.DialogCode.Accepted and (text := dialog.textValue().strip()):
            self.agent = text
            self.check_installed()

    def check_installed(self, then=None):
        """Probe the interactive login shell, shell-quoting only executable names."""
        try:
            selected = shlex.split(self.agent)
        except ValueError as exc:
            self.diagnostic.emit(f"Agent command: {exc}")
            if then:
                then(None)
            return
        names = sorted({agent.command for agent in AI_AGENTS} | ({selected[0]} if selected else set()))
        # Custom command arguments are never interpolated into this availability probe.
        script = "\n".join(f"command -v {shlex.quote(name)} >/dev/null 2>&1 && printf '%s\\n' {shlex.quote('@@' + name)}"
                           for name in names) + "\n:"
        process = QProcess(self)
        self._probes.append(process)
        env = QProcessEnvironment()
        for key, value in self.environment.items():
            env.insert(key, str(value))
        process.setProcessEnvironment(env)
        process.setProgram(login_shell(self.environment))
        process.setArguments(["-i", "-l", "-c", script])
        timer = QTimer(process)
        timer.setSingleShot(True)
        output = bytearray()
        errors = bytearray()
        timed_out = False
        settled = False
        def timeout():
            nonlocal timed_out
            timed_out = True
            process.kill()
        timer.timeout.connect(timeout)
        process.readyReadStandardOutput.connect(lambda: output.extend(bytes(process.readAllStandardOutput())))
        process.readyReadStandardError.connect(lambda: errors.extend(bytes(process.readAllStandardError())))
        def done(code, status):
            nonlocal settled
            if settled:
                return
            settled = True
            timer.stop()
            if timed_out or code != 0 or status != QProcess.ExitStatus.NormalExit:
                reason = "timed out after 5 seconds" if timed_out else process.errorString() if code == -1 else errors.decode("utf-8", "replace").strip()
                self.diagnostic.emit(f"Agent availability probe failed: {reason}")
                found = None
            else:
                found = {line[2:] for line in output.decode("utf-8", "replace").splitlines() if line.startswith("@@")}
                self.installed = found
                self.update_run_button()
            self._probes.remove(process)
            process.deleteLater()
            if then:
                then(found)
        process.finished.connect(done)
        process.errorOccurred.connect(lambda error: done(-1, QProcess.ExitStatus.CrashExit)
                                     if error == QProcess.ProcessError.FailedToStart else None)
        process.start()
        process.closeWriteChannel()
        timer.start(5000)

    def run_agent(self):
        target = self.active
        if target is None or target.process is None:
            return
        process = target.process
        command = self.agent
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid agent command", str(exc))
            return
        if not parts:
            return
        name = parts[0]
        def go(found):
            if target not in self.all_sessions or target.process is not process or target._stop_future is not None:
                return
            if found is None:
                QMessageBox.warning(self, "Cannot check agent availability",
                                    "The interactive login-shell probe failed or timed out. No command was sent. Check your shell startup configuration.")
            elif name not in found:
                self.offer_install(name, target)
            else:
                target.type(command + "\r")
                target.focus()
        self.check_installed(then=go)

    def offer_install(self, name, target):
        process = target.process
        agent = next((agent for agent in AI_AGENTS if agent.command == name), None)
        title = f"{agent.title if agent else name} isn't installed"
        box = QMessageBox(QMessageBox.Icon.Information, title, title,
                          QMessageBox.StandardButton.Cancel if agent else QMessageBox.StandardButton.Ok, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        insert = None
        if agent:
            box.setInformativeText(f"Install it with:\n\n{agent.install}\n\nthen sign in the first time you run it.")
            insert = box.addButton("Type Install Command", QMessageBox.ButtonRole.ActionRole)
            insert.setToolTip("Types the command without pressing Return")
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        else:
            box.setInformativeText(f"`{name}` wasn't found on your shell's PATH.")
        copy = add_copy_button(box)
        copy.setToolTip("Copy this message to the clipboard")
        box.exec()
        if (agent is not None and box.clickedButton() is insert and target in self.all_sessions
                and target.process is process and target._stop_future is None):
            target.type(agent.install)
            target.focus()

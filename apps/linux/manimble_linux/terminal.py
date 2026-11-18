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
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                    QWebEngineSettings, QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFontComboBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QStackedWidget,
                               QTabBar, QVBoxLayout, QWidget)

from . import prefs
from .paths import RESOURCES
from .pty_helper import login_shell

_RESOURCE_DIR = (RESOURCES / "terminal").resolve()


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
        session._send_font()
        for data in session._pending_output:
            session._emit("write", data=data)
        session._pending_output.clear()
        if session._focus_pending:
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
            session.set_font(size=13)


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
        self.foreground_command: str | None = None
        self.commands: list[str] = []
        self.columns, self.rows = 80, 24
        self.font_family = prefs.get_str("terminal.font", "monospace")
        self.font_size = prefs.get_int("terminal.fontSize", 13)
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
        self.marker.setFixedHeight(2)
        self.marker.setStyleSheet("background: #70a5eb")
        self.marker.hide()
        layout.addWidget(self.marker)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.setStyleSheet("color: #ffb5ab; background: #282022; padding: 5px")
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
        self.view.load(QUrl.fromLocalFile(str(_RESOURCE_DIR / "terminal.html")))

    @property
    def highlighted(self):
        return self._highlighted

    @highlighted.setter
    def highlighted(self, value):
        self._highlighted = bool(value)
        self.marker.setVisible(self._highlighted)

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
        self.foreground_command = None
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
                    if self.page_ready:
                        self._emit("write", data=message["data"])
                    else:
                        self._pending_output.append(message["data"])
                        # The renderer may fail; retain bounded diagnostics, not an unbounded shell log.
                        if len(self._pending_output) > 256:
                            del self._pending_output[0]
                elif event == "status":
                    self.foreground_command = message.get("foreground")
                    self.commands = message.get("commands", [])
                    self._title_changed()
                elif event == "error":
                    self._error(message["message"])
                elif event == "exit":
                    self._emit("write", data=base64.b64encode(
                        f"\r\n[process finished ({message['code']}) — Restart starts a new shell]\r\n".encode()).decode())
            except (ValueError, KeyError, TypeError) as exc:
                self._error(f"Invalid terminal helper reply: {exc}")

    def _finished(self, process, code):
        if process is not self._process:
            return
        self._read_output(process)
        self._read_errors(process)
        self.commands = []
        self.foreground_command = None
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

    def _send_font(self):
        if self.page_ready:
            self._emit("font", family=self.font_family, size=self.font_size)

    def set_font(self, family: str | None = None, size: int | None = None):
        if family is not None:
            self.font_family = family
            prefs.set("terminal.font", family)
        if size is not None:
            self.font_size = max(7, min(40, int(size)))
            prefs.set("terminal.fontSize", self.font_size)
        self._send_font()


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
    ``on_hide``/``on_change`` are ordinary callable hooks, matching TerminalPanel.swift.
    """
    output = Signal(bytes)
    diagnostic = Signal(str)

    def __init__(self, controller=None, parent=None, *, python=None, environment=None):
        super().__init__(parent)
        self.controller = controller
        self.python = str(python or sys.executable)
        self.environment = dict(environment if environment is not None else os.environ)
        self.directory = Path.home()
        self.original: Path | None = None
        self.tabs: list[_TerminalTab] = []
        self.active_tab: _TerminalTab | None = None
        self.active: TerminalSession | None = None
        self.on_hide = None
        self.on_change = None
        self.installed: set[str] | None = None
        self._probes: list[QProcess] = []
        self._closing: set[TerminalSession] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.actions = QWidget(self)
        toolbar = QHBoxLayout(self.actions)
        toolbar.setContentsMargins(4, 2, 4, 2)
        self.agent_chooser = QComboBox(self.actions)
        for agent in AI_AGENTS:
            self.agent_chooser.addItem(agent.title, agent.command)
        self.agent_chooser.addItem("Custom command…", "custom")
        toolbar.addWidget(self.agent_chooser)
        self.run_button = QPushButton(self.actions)
        self.run_button.clicked.connect(self.run_agent)
        toolbar.addWidget(self.run_button)
        self.agent_chooser.activated.connect(self.choose_agent)
        for label, slot in (("+ Tab", self.add_tab), ("Split", self.split_active),
                            ("Restart", self.restart_pressed), ("Close", self.kill_pressed),
                            ("Find", self.show_find)):
            button = QPushButton(label, self.actions)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        self.location = QComboBox(self.actions)
        self.location.addItems(["Working copy", "Original folder"])
        self.location.setCurrentIndex(0 if prefs.get_bool("terminalInWorkCopy", True) else 1)
        self.location.activated.connect(self._change_location)
        toolbar.addWidget(self.location)
        self.font = QFontComboBox(self.actions)
        self.font.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.font.setMaximumWidth(140)
        self.font.setCurrentText(prefs.get_str("terminal.font", "monospace"))
        self.font.currentFontChanged.connect(lambda font: self.set_font(font.family()))
        toolbar.addWidget(self.font)
        self.size = QSpinBox(self.actions)
        self.size.setRange(7, 40)
        self.size.setValue(prefs.get_int("terminal.fontSize", 13))
        self.size.valueChanged.connect(lambda size: self.set_font(size=size))
        toolbar.addWidget(self.size)
        toolbar.addStretch(1)
        hide = QPushButton("Hide", self.actions)
        hide.clicked.connect(self.hide_terminal)
        toolbar.addWidget(hide)
        layout.addWidget(self.actions)
        self.tab_bar = QTabBar(self)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.currentChanged.connect(self.select_tab)
        self.tab_bar.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.tab_bar)
        self.find_bar = QWidget(self)
        find_layout = QHBoxLayout(self.find_bar)
        find_layout.setContentsMargins(4, 0, 4, 0)
        self.find_field = QLineEdit(self.find_bar)
        self.find_field.setPlaceholderText("Find in terminal scrollback")
        self.find_case = QCheckBox("Case", self.find_bar)
        find_layout.addWidget(self.find_field)
        find_layout.addWidget(self.find_case)
        for label, slot in (("Previous", lambda: self.find(True)), ("Next", self.find),
                            ("Close", self.hide_find)):
            button = QPushButton(label, self.find_bar)
            button.clicked.connect(slot)
            find_layout.addWidget(button)
        self.find_field.returnPressed.connect(self.find)
        self.find_field.textChanged.connect(lambda: self.find())
        self.find_bar.hide()
        layout.addWidget(self.find_bar)
        self.content = QStackedWidget(self)
        layout.addWidget(self.content, 1)
        self.warning = QLabel(self)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #e8bd70; padding: 3px")
        layout.addWidget(self.warning)
        self.update_run_button()
        self._update_warning()
        # WebEngine's actual text focus lives in its internal widget. JS focus/blur
        # alone does not cover switching to another Qt window or a find field.
        QApplication.instance().focusChanged.connect(self._qt_focus_changed)

    @property
    def all_sessions(self):
        return [session for tab in self.tabs for session in tab.sessions]

    @property
    def tab_titles(self):
        return [(f"Terminal {tab.number}" + (f" ×{len(tab.sessions)}" if len(tab.sessions) > 1 else ""),
                 " · ".join(session.foreground_command or session.shell_title for session in tab.sessions
                            if session.foreground_command or session.shell_title)) for tab in self.tabs]

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
        name = self.agent.split(maxsplit=1)[0] if self.agent else "agent"
        self.run_button.setText(f"Run {name}")
        self.run_button.setToolTip(f"Type `{self.agent}` and Enter into the active terminal")
        index = self.agent_chooser.findData(self.agent)
        self.agent_chooser.setCurrentIndex(index if index >= 0 else len(AI_AGENTS))
        for item, agent in enumerate(AI_AGENTS):
            suffix = " (not installed)" if self.installed is not None and agent.command not in self.installed else ""
            self.agent_chooser.setItemText(item, agent.title + suffix)
        custom = not any(agent.command == self.agent for agent in AI_AGENTS)
        self.agent_chooser.setItemText(len(AI_AGENTS), f"Custom: {self.agent}…" if custom else "Custom command…")

    def _cwd(self):
        return self.original if self.location.currentIndex() == 1 and self.original else self.directory

    def _session_environment(self):
        env = dict(self.environment)
        env["MANIMBLE_DECK"] = str(self.directory)
        if self.original:
            env["MANIMBLE_ORIGINAL_DECK"] = str(self.original)
        return env

    def start(self, directory: str | Path, original: str | Path | None = None):
        self.directory = Path(directory).absolute()
        self.original = Path(original).absolute() if original is not None else None
        if not self.tabs:
            self.add_tab()
            self.check_installed()
        else:
            for session in self.all_sessions:
                session.environment = self._session_environment()
                session.original_repository = self.location.currentIndex() == 1
                session.start(self._cwd(), self.original)
        self._update_warning()

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
        session.original_repository = self.location.currentIndex() == 1
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
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count() > len(self.tabs):
            self.tab_bar.removeTab(self.tab_bar.count() - 1)
        for i, (title, tip) in enumerate(self.tab_titles):
            if i >= self.tab_bar.count():
                self.tab_bar.addTab(title)
            self.tab_bar.setTabText(i, title)
            self.tab_bar.setTabToolTip(i, tip)
        if self.active_tab_index is not None:
            self.tab_bar.setCurrentIndex(self.active_tab_index)
        self.tab_bar.blockSignals(False)
        for tab in self.tabs:
            for session in tab.sessions:
                session.highlighted = len(tab.sessions) > 1 and session is self.active
        if self.on_change:
            self.on_change()

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
        self._set_terminal_focused(widget is not None and self.isAncestorOf(widget))

    def _qt_focus_changed(self, old, new):
        focused_session = next((session for session in self.all_sessions
                                if new is session.view or (new is not None and session.view.isAncestorOf(new))), None)
        if focused_session:
            self.activate(focused_session)
        self._set_terminal_focused(new is not None and (new is self or self.isAncestorOf(new)))

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
                self.refresh()
                self.hide_terminal()
        else:
            self.refresh()

    def close_tab(self, index):
        if not 0 <= index < len(self.tabs):
            return
        sessions = list(self.tabs[index].sessions)
        if self._confirm_stop(sessions, "Close terminal tab?"):
            for session in sessions:
                self.kill(session, confirmed=True)

    def restart_pressed(self):
        if self.active and self._confirm_stop([self.active], "Restart shell?"):
            self.active.environment = self._session_environment()
            self.active.original_repository = self.location.currentIndex() == 1
            self.active.start(self._cwd(), self.original)
            self.active.focus()

    def kill_pressed(self):
        if self.active:
            self.kill(self.active)

    def split_pressed(self):
        self.split_active()

    def type(self, text):
        if self.active:
            self.active.type(text)

    def contents(self):
        return self.active.contents() if self.active else ""

    def focus(self):
        if self.active:
            self.active.focus()

    def hide_terminal(self):
        self.hide()
        self._set_terminal_focused(False)
        if self.on_hide:
            self.on_hide()

    def hideEvent(self, event):
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

    def set_font(self, family=None, size=None):
        for session in self.all_sessions:
            session.set_font(family, size)

    def _update_warning(self):
        if self.location.currentIndex() == 1:
            self.warning.setText("Original-folder shell: commands bypass Save isolation and can change original files immediately.")
        else:
            self.warning.setText("Working-copy shell. Authored symlinks and external assets are not sandboxed; commands can modify their targets.")

    def set_start_in_work_copy(self, enabled: bool):
        """Change the shared preference through the same warning/stop-consent flow."""
        index = 0 if enabled else 1
        self.location.setCurrentIndex(index)
        self._change_location(index)

    def _change_location(self, index):
        old = 0 if prefs.get_bool("terminalInWorkCopy", True) else 1
        if index == old:
            return
        if index == 1:
            if self.original is None:
                QMessageBox.warning(self, "No original folder", "Open a deck before using its original folder.")
                self.location.setCurrentIndex(old)
                return
            answer = QMessageBox.warning(self, "Use original folder?",
                "Commands will run in the original deck and bypass Save isolation. Changes are immediate and cannot be reverted by Manimble. All open shells will restart.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                self.location.setCurrentIndex(old)
                return
        if not self._confirm_stop(self.all_sessions, "Restart terminals in another folder?"):
            self.location.setCurrentIndex(old)
            return
        prefs.set("terminalInWorkCopy", index == 0)
        self._update_warning()
        for session in self.all_sessions:
            session.environment = self._session_environment()
            session.original_repository = self.location.currentIndex() == 1
            session.start(self._cwd(), self.original)

    def choose_agent(self, index):
        command = self.agent_chooser.itemData(index)
        if command == "custom":
            self.custom_agent()
        else:
            self.agent = command
            self.check_installed()

    def custom_agent(self):
        text, accepted = QInputDialog.getText(self, "Custom agent command",
            "The Run button types this command and Enter into the active shell.", text=self.agent)
        if accepted and text.strip():
            self.agent = text.strip()
            self.check_installed()
        else:
            self.update_run_button()

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
        if self.active is None:
            self.add_tab()
        command = self.agent
        target = self.active
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid agent command", str(exc))
            return
        if not parts:
            return
        name = parts[0]
        def go(found):
            if target not in self.all_sessions:
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

    def offer_install(self, name, target=None):
        target = target or self.active
        agent = next((agent for agent in AI_AGENTS if agent.command == name), None)
        if agent is None:
            QMessageBox.information(self, "Agent not found", f"{name!r} was not found on your interactive login shell's PATH.")
            return
        box = QMessageBox(QMessageBox.Icon.Information, f"{agent.title} isn't installed",
            f"Install it with:\n\n{agent.install}\n\nThen sign in the first time you run it. Type Install Command inserts text only; it does not execute or install anything.",
            QMessageBox.StandardButton.Cancel, self)
        insert = box.addButton("Type Install Command", QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is insert and target in self.all_sessions:
            target.type(agent.install)
            target.focus()

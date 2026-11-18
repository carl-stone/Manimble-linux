"""Editable Python plots, immutable image revisions, and cancellable Qt processes.

Sidecars use the macOS PlotSpec format. Plot code is arbitrary user Python, not a
sandbox: it is only executed on Render, click-away commit, or explicit rerun.
Filesystem helpers are synchronous for worker use; UI callers yield run_blocking.
"""
from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import shutil
import tempfile
from concurrent.futures import CancelledError, Future
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QDir, QObject, QPoint, QProcess, QProcessEnvironment, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPalette, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QStyle, QStyleOptionFrame, QTextEdit, QVBoxLayout, QWidget,
)

from .models import frame_size, unit_px
from .paths import RESOURCES
from .style import Theme, watch_appearance
from .tasks import spawn
from .workers import run_blocking
from .widgets import add_copy_button, field_label


@dataclass(eq=True)
class PlotSpec:
    code: str | None = None
    file: str | None = None


class RunError(RuntimeError):
    def __init__(self, message: str, python: str = "", no_seaborn: bool = False):
        super().__init__(message)
        self.python = python
        self.no_seaborn = no_seaborn


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _environment(runtime) -> dict[str, str]:
    env = runtime.child_env()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(MPLBACKEND="Agg", PYTHONUNBUFFERED="1")
    return env


class _Command(QObject):
    """QProcess with bounded logs, timeout, and cancellation; no synchronous wait."""
    def __init__(self, python: str, args: list[str], env: dict, cwd: Path | None = None,
                 timeout: int = 300):
        super().__init__()
        self.python = python
        self.future = Future()
        self.process = QProcess(self)
        self.process.setProgram(python)
        self.process.setArguments(args)
        qenv = QProcessEnvironment()
        for key, value in env.items():
            qenv.insert(key, value)
        self.process.setProcessEnvironment(qenv)
        if cwd is not None:
            self.process.setWorkingDirectory(str(cwd))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.log = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.timed_out = False
        self.timeout = timeout
        self.cancelled = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(timeout * 1000)
        self.timer.timeout.connect(self._timeout)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)

    def start(self) -> Future:
        self.process.start()
        self.timer.start()
        return self.future

    def _read(self):
        self.log = (self.log + self.decoder.decode(bytes(self.process.readAllStandardOutput())))[-1_048_576:]

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart and not self.future.done():
            self.timer.stop()
            self.future.set_exception(RunError(f"Could not start {self.python}: {self.process.errorString()}", self.python))

    def _timeout(self):
        self.timed_out = True
        self.process.kill()

    def cancel(self):
        self.cancelled = True
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        elif not self.future.done():
            self.future.set_exception(CancelledError("Plot cancelled"))

    def _finished(self, code, status):
        self.timer.stop()
        self._read()
        self.log += self.decoder.decode(b"", final=True)
        if self.future.done():
            return
        if self.cancelled:
            self.future.set_exception(CancelledError("Plot cancelled"))
        elif code == 0 and status == QProcess.ExitStatus.NormalExit and not self.timed_out:
            self.future.set_result(self.log)
        else:
            text = self.log.strip()
            # Keep chained and long tracebacks intact, without repeating their tail.
            at = text.find("Traceback (most recent call last):")
            tail = text[at:] if at >= 0 else "\n".join(text.splitlines()[-14:])
            if self.timed_out:
                tail = (f"The code ran for {self.timeout // 60} minutes and was stopped.\n" if self.timeout >= 60
                        else f"The Python probe timed out after {self.timeout} seconds.\n") + tail
            no_seaborn = "No module named 'seaborn'" in self.log
            if not no_seaborn and "No module named" in self.log:
                tail += f"\n\nThis Python ({self.python}) is missing a package: install it there, or choose another Python below."
            self.future.set_exception(RunError(tail or f"The code failed (exit {code}).", self.python, no_seaborn))


class PythonPlot:
    template = '''import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(0, 2 * np.pi, 200)
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot(x, np.sin(x), label="sin x")
ax.plot(x, np.cos(x), label="cos x")
ax.legend()'''

    @staticmethod
    def plot(of_image: str | Path) -> tuple[Path, str] | None:
        path = Path(of_image)
        if path.suffix.lower() != ".png" or "@" not in path.stem:
            return None
        stem = path.stem.rsplit("@", 1)[0]
        return (path.parent, stem) if stem and PythonPlot.spec_url(path.parent, stem).is_file() else None

    @staticmethod
    def spec_url(dir: Path, stem: str) -> Path:
        return Path(dir) / (stem + ".plot.json")

    @staticmethod
    def load(dir: Path, stem: str) -> PlotSpec:
        data = json.loads(PythonPlot.spec_url(dir, stem).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(data.get(k) is not None and not isinstance(data[k], str) for k in ("code", "file")):
            raise ValueError("Invalid plot sidecar: code and file must be strings or null")
        return PlotSpec(data.get("code"), data.get("file"))

    @staticmethod
    def save(spec: PlotSpec, dir: Path, stem: str) -> None:
        _atomic_write(PythonPlot.spec_url(dir, stem),
                      json.dumps(asdict(spec), indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def free_stem(dir: Path) -> str:
        n = 1
        while PythonPlot.spec_url(dir, f"plot{n}").exists():
            n += 1
        return f"plot{n}"

    @staticmethod
    def store(png: bytes, stem: str, dir: Path) -> str:
        name = f"{stem}@{hashlib.sha256(png).hexdigest()[:8]}.png"
        path = Path(dir) / name
        if not path.exists():
            _atomic_write(path, png)
        elif path.read_bytes() != png:
            raise RunError(f"Plot image hash collision at {path}; the old revision was preserved.")
        return name

    @staticmethod
    def python(runtime, settings, owner=None):
        """Generator: select and probe interpreters asynchronously, retaining venv paths."""
        saved = str(settings.value("plotPython", "") or "")
        env = _environment(runtime)
        candidates = [saved] if saved else [runtime.python, str(runtime.paths.private_python)]
        if not saved:
            for directory in env.get("PATH", "").split(os.pathsep):
                for name in ("python3", "python"):
                    candidates.append(os.path.join(directory, name))
        seen = set()
        failures = []
        for candidate in candidates:
            candidate = os.path.abspath(os.path.expanduser(candidate))
            if candidate in seen:
                continue
            seen.add(candidate)
            if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                if saved:
                    raise RunError(f"The chosen plot Python is not executable: {candidate}. Choose another Python in the plot editor.", candidate)
                continue
            command = _Command(candidate, ["-u", "-c", "import matplotlib.pyplot"], env, timeout=20)
            if owner is not None:
                owner.command = command
                owner.check_cancelled()
            try:
                yield command.start()
                return candidate
            except RunError as exc:
                failures.append(f"{candidate}: {exc}")
                if saved:
                    raise RunError(f"The chosen plot Python cannot import matplotlib. Choose another Python or install matplotlib there.\n{exc}", candidate) from exc
        raise RunError("No Python with matplotlib was found. Choose one in the plot editor.\n" + "\n".join(failures))

    @staticmethod
    def run(spec: PlotSpec, deck: Path, runtime, settings):
        return PlotRun(spec, deck, runtime, settings)

    @staticmethod
    def install_seaborn(python: str, runtime) -> _Command:
        command = _Command(python, ["-u", "-m", "pip", "install", "--disable-pip-version-check", "seaborn"], _environment(runtime))
        command.start()
        return command


class PlotRun:
    """One cancellable plot operation. start() returns a Future[bytes]."""
    def __init__(self, spec: PlotSpec, deck: Path, runtime, settings):
        self.spec, self.deck = spec, Path(deck)
        self.runtime, self.settings = runtime, settings
        self.command = None
        self.cancelled = False
        self.task = None

    def start(self) -> Future:
        if self.task is None:
            self.task = spawn(self._run(), "Python plot")
        return self.task.future

    def check_cancelled(self):
        if self.cancelled:
            raise CancelledError("Plot cancelled")

    def cancel(self):
        self.cancelled = True
        if self.command is not None:
            self.command.cancel()

    def _prepare(self):
        tmp = Path(tempfile.mkdtemp(prefix="manimble-plot-"))
        try:
            if self.spec.file:
                path = Path(self.spec.file).expanduser()
                script = path if path.is_absolute() else self.deck / path
                script = script.absolute()
                if not script.is_file():
                    raise RunError(f"Can't find {script}")
                cwd = script.parent
            else:
                script, cwd = tmp / "plot.py", self.deck
                script.write_text(self.spec.code or "", encoding="utf-8")
            return tmp, script, cwd
        except BaseException:
            shutil.rmtree(tmp)
            raise

    @staticmethod
    def _read_png(path):
        from PIL import Image
        png = path.read_bytes()
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise RunError("The plot output is not a PNG image. Save PNG to MANIMBLE_PLOT_OUT.")
            image.verify()
        return png

    def _run(self):
        tmp = None
        try:
            self.check_cancelled()
            python = yield from PythonPlot.python(self.runtime, self.settings, self)
            self.check_cancelled()
            tmp, script, cwd = yield run_blocking(self._prepare)
            self.check_cancelled()
            output = tmp / "out.png"
            env = _environment(self.runtime)
            env["MANIMBLE_PLOT_OUT"] = str(output)
            self.command = _Command(python, ["-u", str(RESOURCES / "plot_runner.py"), str(script), str(output)], env, cwd)
            yield self.command.start()
            self.check_cancelled()
            png = yield run_blocking(self._read_png, output)
            self.check_cancelled()
            return png
        finally:
            if tmp is not None:
                yield run_blocking(shutil.rmtree, tmp)


class _PlotFileField(QLineEdit):
    """Keep the full editable path; when not editing, show its filename first."""
    def paintEvent(self, event):
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, self)
        margins = self.textMargins()
        rect.adjust(margins.left() + 2, margins.top(), -margins.right() - 2, -margins.bottom())
        metrics = self.fontMetrics()
        if self.hasFocus() or metrics.horizontalAdvance(self.text()) <= rect.width():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit, option, painter, self)
        painter.setPen(self.palette().text().color())
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(self.text(), Qt.TextElideMode.ElideLeft, rect.width()))


class _PlotPythonLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_path(self, path):
        self._full_text = "Python: " + (path or "automatic (one with matplotlib)")
        self.setAccessibleName(self._full_text)
        self.setToolTip(path)
        self.elide()

    def elide(self):
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, self.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.elide()


class PlotEditor(QFrame):
    """Semitransient plot popover: click away/Render commits; Escape/Cancel discards."""
    def __init__(self, spec: PlotSpec, error: str | None, deck: Path, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.deck, self.settings = Path(deck), settings
        self.on_done = None
        self.on_cancel = None
        self.render_requested = False
        self._cancelled = False
        self._delivered = False
        self._chooser = None
        self._anchor = None
        self.setWindowTitle("Python Plot")
        self.setAutoFillBackground(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFont(Theme.font(11))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("Python Plot")
        title.setFont(Theme.font(13, QFont.Weight.Bold))
        head.addWidget(title)
        head.addStretch()
        self.segments = QWidget()
        self.segments.setObjectName("plotModes")
        modes = QHBoxLayout(self.segments)
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(0)
        self.mode = QButtonGroup(self)
        for index, label in enumerate(("Code", "File")):
            segment = QPushButton(label)
            segment.setObjectName(f"plotMode{label}")
            segment.setCheckable(True)
            modes.addWidget(segment)
            self.mode.addButton(segment, index)
        self.mode.button(1 if spec.file is not None else 0).setChecked(True)
        head.addWidget(self.segments)
        layout.addLayout(head)

        self.code_view = QPlainTextEdit()
        self.code_view.setFont(Theme.mono(12))
        self.code_view.setPlainText(spec.code if spec.code is not None else PythonPlot.template)
        self.code_view.setFixedHeight(260)
        self.code_view.setViewportMargins(0, 2, 0, 2)
        self.code_view.setAccessibleName("Python code")
        layout.addWidget(self.code_view)

        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_field = _PlotFileField(str(self.file_path(spec.file)) if spec.file else "")
        self.file_field.setPlaceholderText("/path/to/plot.py")
        self.file_field.setAccessibleName("Python file")
        file_layout.addWidget(self.file_field, 1)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self.choose_file)
        file_layout.addWidget(choose)
        layout.addWidget(self.file_row)

        hint = field_label('Runs when you click away. The last matplotlib (or seaborn) figure becomes the picture '
                           '(or save one to os.environ["MANIMBLE_PLOT_OUT"]). Hover the plot and press ▶ to run it again.')
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error_view = None
        if error:
            self.error_view = QTextEdit()
            self.error_view.setAcceptRichText(False)
            self.error_view.setReadOnly(True)
            self.error_view.setFont(Theme.mono(10.5))
            self.error_view.setFrameShape(QFrame.Shape.NoFrame)
            self.error_view.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
            self.error_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.error_view.document().setDocumentMargin(0)
            self.error_view.setPlainText(error)
            self.error_view.setAccessibleName("Python plot error")
            layout.addWidget(self.error_view)

        foot = QHBoxLayout()
        self.python_label = _PlotPythonLabel()
        foot.addWidget(self.python_label, 1)
        change = QPushButton("Change…")
        change.clicked.connect(self.choose_python)
        foot.addWidget(change)
        self.automatic = QPushButton("Automatic")
        self.automatic.clicked.connect(self.automatic_python)
        foot.addWidget(self.automatic)
        foot.addSpacing(8)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_pressed)
        foot.addWidget(cancel)
        render = QPushButton("Render")
        render.setToolTip("Run the code now (Ctrl+Return)")
        render.clicked.connect(self.render_pressed)
        foot.addWidget(render)
        layout.addLayout(foot)
        self.mode.idClicked.connect(self.mode_changed)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.render_pressed)
        QShortcut(QKeySequence("Escape"), self, activated=self.cancel_pressed)
        self.mode_changed()
        self.update_python()
        self._recolor()
        watch_appearance(self._recolor)

    @property
    def spec(self):
        if self.mode.checkedId() == 1:
            return PlotSpec(file=self.file_field.text().strip())
        return PlotSpec(code=self.code_view.toPlainText())

    @property
    def python_path(self):
        return str(self.settings.value("plotPython", "") or "")

    def file_path(self, text):
        return self.deck / Path(text).expanduser()

    def update_python(self):
        value = self.python_path
        self.python_label.set_path(value)
        self.automatic.setVisible(bool(value))
        self._place()

    def _recolor(self):
        def color(ink):
            return ink.name(QColor.NameFormat.HexArgb)

        self.python_label.setStyleSheet(f"color: {color(Theme.secondary_ink())}")
        self.segments.setStyleSheet(f"""
            #plotModes QPushButton {{
                background: {color(Theme.ink(.06))};
                border: 1px solid {color(Theme.separator())};
                padding: 3px 10px;
            }}
            #plotModeCode {{ border-top-left-radius: 5px; border-bottom-left-radius: 5px; }}
            #plotModeFile {{ border-top-right-radius: 5px; border-bottom-right-radius: 5px; border-left: 0; }}
            #plotModes QPushButton:hover {{ background: {color(Theme.ink(.11))}; }}
            #plotModes QPushButton:checked {{ background: {color(Theme.accent_soft)}; }}
            #plotModes QPushButton:focus {{ border-color: {color(Theme.accent)}; }}
        """)
        if self.error_view is not None:
            palette = self.error_view.palette()
            palette.setColor(QPalette.ColorRole.Text, Theme.error_ink())
            palette.setColor(QPalette.ColorRole.Base, self.palette().window().color())
            self.error_view.setPalette(palette)

    def mode_changed(self):
        file_mode = self.mode.checkedId() == 1
        self.code_view.setVisible(not file_mode)
        self.file_row.setVisible(file_mode)
        self._place()

    def present(self, anchor: QRect, *, below=False):
        self._anchor = anchor
        self._below = below
        screen = QGuiApplication.screenAt(anchor.center()) or self.parentWidget().screen()
        self._available = screen.availableGeometry().adjusted(8, 8, -8, -8)
        self.setFixedWidth(max(1, min(560, self._available.width())))
        if self.error_view is not None:
            error = self.error_view
            gutter = error.verticalScrollBar().sizeHint().width()
            wrap_width = max(1, min(520, self.width() - 24 - gutter))
            error.setLineWrapColumnOrWidth(wrap_width)
            error.document().setTextWidth(wrap_width)
            error.setFixedHeight(min(140, round(error.document().size().height()) + 4))
        self._place()
        self.show()
        (self.file_field if self.mode.checkedId() == 1 else self.code_view).setFocus()

    def _place(self):
        if self._anchor is None:
            return
        available, anchor = self._available, self._anchor
        layout = self.layout()
        layout.activate()
        width = self.width()
        height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        self.resize(width, height)
        if self._below:
            x, y = anchor.center().x() - width // 2, anchor.bottom() + 8
            if y + height > available.bottom():
                y = anchor.top() - height - 8
        else:
            x, y = anchor.right() + 8, anchor.center().y() - height // 2
            if x + width > available.right() and anchor.left() - width - 8 >= available.left():
                x = anchor.left() - width - 8
        self.move(max(available.left(), min(x, available.right() - width + 1)),
                  max(available.top(), min(y, available.bottom() - height + 1)))

    def _choose(self, title, path, name_filter="All files (*)", *, hidden=False):
        start = path.parent if path is not None and path.parent.is_dir() else self.deck
        dialog = QFileDialog(self, title, str(start), name_filter)
        self._chooser = dialog
        dialog.setOption(QFileDialog.Option.DontResolveSymlinks)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setSupportedSchemes(["file"])
        if hidden:
            dialog.setFilter(dialog.filter() | QDir.Filter.Hidden)
        if path is not None and start == path.parent:
            dialog.selectFile(path.name)
        try:
            return dialog.selectedFiles()[0] if dialog.exec() else ""
        finally:
            dialog.deleteLater()
            self._chooser = None
            if self._cancelled:
                self._deliver()
            elif not self.isVisible():
                self.show()

    def choose_file(self):
        text = self.file_field.text().strip()
        path = self._choose("Choose a Python file that draws the plot",
                            self.file_path(text) if text else None, "Python (*.py)")
        if path and not self._delivered:
            self.file_field.setText(path)

    def choose_python(self):
        saved = self.python_path
        path = self._choose("Choose the Python that runs plots (a python3 with matplotlib)",
                            Path(saved).expanduser() if saved else None, hidden=True)
        if path and not self._delivered:
            self.settings.setValue("plotPython", path)
            self.update_python()

    def automatic_python(self):
        self.settings.remove("plotPython")
        self.update_python()

    def cancel_pressed(self):
        self._cancelled = True
        if self._chooser is not None:
            self._chooser.reject()
        self.close()
        self._deliver()

    def render_pressed(self):
        self.render_requested = True
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_pressed()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._chooser is None:
            self._deliver()

    def _deliver(self):
        if self._delivered or self._chooser is not None:
            return
        self._delivered = True
        done, cancel = self.on_done, self.on_cancel
        self.on_done = self.on_cancel = None
        if self._cancelled:
            if cancel:
                cancel()
        elif done:
            done(self.spec)
        self.deleteLater()


class PlotMixin:
    def plot_of(self, oid: int):
        obj = ((self.scene or {}).get("objects") or {}).get(str(oid), {})
        if obj.get("kind") != "image":
            return None
        path = obj.get("imagePath")
        if not path and obj.get("image") and self.work_deck:
            path = self.work_deck / obj["image"]
        return PythonPlot.plot(path) if path else None

    def insert_python_plot_pressed(self, anchor=None):
        if self.scene_name and self.workspace:
            if anchor is None:
                button = getattr(self, "_toolbar_buttons", {}).get("insert_python_plot_pressed")
                if button is not None and button.isVisible() and button.isEnabled():
                    anchor = button
            return self.open_plot_editor(PlotSpec(code=PythonPlot.template), anchor=anchor)

    def edit_plot(self, oid):
        return spawn(self._load_plot(oid, False), "Edit plot").future

    def rerun_plot(self, oid):
        return spawn(self._load_plot(oid, True), "Rerun plot").future

    def _load_plot(self, oid, rerun):
        ws, name = self.workspace, self.scene_name
        location = self.plot_of(oid)
        if location:
            try:
                spec = yield run_blocking(PythonPlot.load, *location)
                if self.workspace is not ws or self.scene_name != name or self.plot_of(oid) != location:
                    return
                if rerun:
                    return (yield self.commit_plot(spec, oid, True))
                return self.open_plot_editor(spec, oid, None)
            except Exception as exc:
                self.toast(str(exc), error=True)

    def _plot_anchor(self, oid, control):
        if isinstance(control, QWidget):
            return QRect(control.mapToGlobal(QPoint()), control.size()), True
        if isinstance(control, QRect) and not control.isEmpty():
            return control, True
        viewport = self.canvas.viewport()
        box = self.canvas.current_box(oid) if oid is not None else None
        if box is not None:
            visible = self.canvas.view_rect(box).intersected(QRectF(viewport.rect()))
            if not visible.isEmpty():
                rect = visible.toAlignedRect()
                return QRect(viewport.mapToGlobal(rect.topLeft()), rect.size()), False
        mid = viewport.mapToGlobal(viewport.rect().center())
        return QRect(mid.x() - 1, mid.y() - 1, 2, 2), False

    def open_plot_editor(self, spec, oid=None, error=None, *, anchor=None):
        ws, name = self.workspace, self.scene_name
        if not ws or not name:
            return None
        previous = getattr(self, "_plot_editor", None)
        if previous is not None:
            previous.cancel_pressed()
        editor = PlotEditor(spec, error, self.work_deck, self.settings, self.window)
        self._plot_editor = editor
        initial_spec, initial_python = editor.spec, editor.python_path

        def release():
            if self._plot_editor is editor:
                self._plot_editor = None

        def finish(draft):
            release()
            if self.workspace is not ws or self.scene_name != name or not self.window.isVisible():
                return
            if not (draft.file or (draft.code or "").strip()):
                return
            # Opening or changing Python in a diagnostic is not permission to retry its code.
            force = editor.render_requested and (error is not None or editor.python_path != initial_python)
            if draft != initial_spec or force or (oid is None and error is None):
                self.commit_plot(draft, oid, force)

        editor.on_cancel = release
        editor.on_done = finish
        rect, below = self._plot_anchor(oid, anchor)
        editor.present(rect, below=below)
        return editor

    def cancel_plot(self):
        """Cancel the running plot without losing its draft or the last good image."""
        run = getattr(self, "_plot_run", None)
        if run:
            run.cancel()
        install = getattr(self, "_plot_install", None)
        if install:
            install.cancel()

    def commit_plot(self, spec: PlotSpec, oid=None, force=False):
        if not self.workspace or not self.scene_name:
            return None
        ws, name, context = self.workspace, self.scene_name, self.edit_context
        old = self.plot_of(oid) if oid is not None else None
        old_obj = ((self.scene or {}).get("objects") or {}).get(str(oid), {})
        old_image = old_obj.get("imagePath") or (str(ws.work / old_obj["image"]) if old_obj.get("image") else None)
        source_scene = self.scene
        # Links inside either version of the deck remain portable and execute the work copy.
        if spec.file:
            path = Path(spec.file).expanduser()
            for root in (ws.work, ws.original):
                if path.is_absolute() and path.is_relative_to(root):
                    spec = PlotSpec(spec.code, path.relative_to(root).as_posix())
                    break
        return self.submit(lambda: self._commit_plot(spec, oid, force, ws, name, context, old, old_image, source_scene))

    def _commit_plot(self, spec, oid, force, ws, name, context, old, old_image, source_scene):
        if self.workspace is not ws:
            return
        if old and not force and (yield run_blocking(PythonPlot.load, *old)) == spec:
            return {"ok": True, "unchanged": True}
        stage = "run"
        self.busy_text = "Running the plot's code…"
        self.busy += 1
        self._plot_run = PythonPlot.run(spec, ws.work, self.runtime, self.settings)
        try:
            png = yield self._plot_run.start()
            if self.workspace is not ws:
                return
            self.busy_text = "Rendering…"
            stage = "save"
            dir, stem = old if old else (ws.work, (yield run_blocking(PythonPlot.free_stem, ws.work)))
            file = yield run_blocking(PythonPlot.store, png, stem, dir)
            self._plot_run.check_cancelled()
            if self.workspace is not ws:
                return
            yield run_blocking(PythonPlot.save, spec, dir, stem)
            if self.workspace is not ws:
                return
            path = dir / file
            relative = os.path.relpath(path, ws.work)
            if old_image and Path(old_image).absolute() == path.absolute():
                self.toast("The plot is up to date")
                return {"ok": True, "unchanged": True}
            stage = "update"
            req = {"scene": name, "stop": context.stop, "items": []}
            if context.seen:
                req["seen"] = context.seen
            if oid is not None:
                req["replace_image"] = {"oid": oid, "file": relative}
            else:
                width, height = frame_size(source_scene)
                from PIL import Image
                with Image.open(io.BytesIO(png)) as image:
                    w = min(6 * unit_px(source_scene), width * .8)
                    h = w * image.height / image.width
                if h > height * .8:
                    w *= height * .8 / h
                    h = height * .8
                req["items"] = [{"kind": "image", "file": relative,
                                 "sx": (width - w) / 2, "sy": (height - h) / 2,
                                 "ex": (width + w) / 2, "ey": (height + h) / 2}]
            result = yield self.call("insert_many", req)
            if not result.get("ok"):
                self.toast(result.get("error") or "could not update the plot", error=True)
                return result
            if self.workspace is ws:
                if self.scene_name == name:
                    yield from self.load_scene(name, keep_stop=True)
                yield from self.reload_state()
                self.toast("Inserted image" if oid is None else "Plot updated · Ctrl+Z goes back to the old one")
            return result
        except Exception as exc:
            failure = exc
        finally:
            self._plot_run = None
            self.busy_text = "Rendering…"
            self.busy -= 1
        if self.workspace is not ws:
            return
        same_scene = self.scene_name == name
        if isinstance(failure, CancelledError):
            if same_scene:
                self.open_plot_editor(spec, oid, "Plot cancelled; the previous picture is unchanged.")
        elif stage == "run":
            if isinstance(failure, RunError) and failure.no_seaborn and same_scene:
                self.offer_seaborn(failure, spec, oid)
            else:
                self.toast("The plot's code failed", error=True)
                if same_scene:
                    self.open_plot_editor(spec, oid, str(failure))
        elif stage == "save":
            self.alert("Could not save the plot", str(failure))
        else:
            self.toast(str(failure) or "could not update the plot", error=True)

    def offer_seaborn(self, error, spec, oid=None):
        ws, name = self.workspace, self.scene_name
        python = error.python
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle("Seaborn isn't installed")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText("Seaborn isn't installed")
        dialog.setInformativeText(f"This plot uses seaborn, which the Python that runs plots doesn't have:\n{python}\n\n"
                                  "Manimble can install it there with “pip install seaborn” and then run the plot again. "
                                  "This downloads packages and changes that Python environment.")
        install = dialog.addButton("Install Seaborn", QMessageBox.ButtonRole.AcceptRole)
        close = dialog.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(close)
        dialog.setEscapeButton(close)
        add_copy_button(dialog)
        dialog.exec()
        if self.workspace is not ws or self.scene_name != name:
            return
        if dialog.clickedButton() is not install:
            return self.open_plot_editor(spec, oid, f"{error}\n\nInstall seaborn into {python}, or choose another Python below.")
        return spawn(self._install_seaborn(python, spec, oid), "Install seaborn").future

    def _install_seaborn(self, python, spec, oid):
        self.busy_text = "Installing seaborn…"
        self.busy += 1
        ws, name = self.workspace, self.scene_name
        try:
            self._plot_install = PythonPlot.install_seaborn(python, self.runtime)
            yield self._plot_install.future
        except Exception as exc:
            failure = exc
        else:
            failure = None
        finally:
            self._plot_install = None
            self.busy_text = "Rendering…"
            self.busy -= 1
        if self.workspace is not ws or self.scene_name != name:
            return
        if isinstance(failure, CancelledError):
            self.open_plot_editor(spec, oid, "Seaborn installation cancelled.")
        elif failure is not None:
            self.toast("Could not install seaborn", error=True)
            self.open_plot_editor(spec, oid, "pip install seaborn failed:\n" + str(failure))
        else:
            self.toast("Seaborn installed")
            self.commit_plot(spec, oid, True)

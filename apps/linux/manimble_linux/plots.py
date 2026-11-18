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

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .models import frame_size, unit_px
from .paths import RESOURCES
from .tasks import spawn
from .workers import run_blocking


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
            tail = "\n".join(self.log.strip().splitlines()[-14:])
            # Long/chained tracebacks are evidence, not expendable console noise.
            at = self.log.find("Traceback (most recent call last):")
            if at >= 0 and self.log[at:].strip() not in tail:
                tail += "\n\n" + self.log[at:].strip()
            if self.timed_out:
                tail = (f"The code ran for {self.timeout // 60} minutes and was stopped.\n" if self.timeout >= 60
                        else f"The Python probe timed out after {self.timeout} seconds.\n") + tail
            if "No module named" in self.log:
                tail += f"\n\nThis Python ({self.python}) is missing a package: install it there, or choose another Python."
            self.future.set_exception(RunError(tail or f"The code failed (exit {code}).", self.python,
                                              "No module named 'seaborn'" in self.log))


class PythonPlot:
    template = '''import numpy as np
import matplotlib.pyplot as plt

x = np.linspace(0, 2 * np.pi, 200)
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot(x, np.sin(x), label="sin x")
ax.plot(x, np.cos(x), label="cos x")
ax.legend()
'''

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


class PlotEditor(QFrame):
    """Semitransient plot popover: click away/Render commits; Escape/Cancel discards."""
    def __init__(self, spec: PlotSpec, error: str | None, deck: Path, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.deck, self.settings = Path(deck), settings
        self.on_done = None
        self.on_cancel = None
        self.cancelled = False
        self._delivered = False
        self._choosing = False
        self.setWindowTitle("Python Plot")
        self.resize(600, 440)
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        head.addWidget(QLabel("Python Plot"))
        head.addStretch()
        self.mode = QComboBox()
        self.mode.addItems(["Code", "File"])
        self.mode.setCurrentIndex(1 if spec.file else 0)
        head.addWidget(self.mode)
        layout.addLayout(head)
        self.code_view = QPlainTextEdit()
        self.code_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.code_view.setPlainText(spec.code if spec.code is not None else PythonPlot.template)
        self.code_view.setMinimumHeight(260)
        layout.addWidget(self.code_view)
        self.file_row = QFrame()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_field = QLineEdit(spec.file or "")
        self.file_field.setPlaceholderText("/path/to/plot.py, or a deck-relative path")
        file_layout.addWidget(self.file_field)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self.choose_file)
        file_layout.addWidget(choose)
        layout.addWidget(self.file_row)
        hint = QLabel('Runs when you click away. The last matplotlib/seaborn figure becomes the picture, or save PNG to os.environ["MANIMBLE_PLOT_OUT"]. Hover the plot and press ▶ to rerun.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error_view = QPlainTextEdit(error or "")
        self.error_view.setReadOnly(True)
        self.error_view.setFont(self.code_view.font())
        self.error_view.setMaximumHeight(150)
        self.error_view.setVisible(bool(error))
        layout.addWidget(self.error_view)
        foot = QHBoxLayout()
        self.python_label = QLabel()
        self.python_label.setWordWrap(True)
        foot.addWidget(self.python_label, 1)
        change = QPushButton("Change Python…")
        change.clicked.connect(self.choose_python)
        foot.addWidget(change)
        automatic = QPushButton("Automatic")
        automatic.clicked.connect(self.automatic_python)
        foot.addWidget(automatic)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_pressed)
        foot.addWidget(cancel)
        render = QPushButton("Render")
        render.clicked.connect(self.render_pressed)
        foot.addWidget(render)
        layout.addLayout(foot)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut.activated.connect(self.render_pressed)
        self.mode_changed()
        self.update_python()

    @property
    def spec(self):
        return PlotSpec(file=self.file_field.text().strip()) if self.mode.currentIndex() else PlotSpec(code=self.code_view.toPlainText())

    def update_python(self):
        value = str(self.settings.value("plotPython", "") or "")
        self.python_label.setText("Python: " + (value or "automatic (one with matplotlib)"))
        self.python_label.setToolTip(value)

    def mode_changed(self):
        self.code_view.setVisible(self.mode.currentIndex() == 0)
        self.file_row.setVisible(self.mode.currentIndex() == 1)

    def _choose(self, title, filter="All files (*)"):
        # A child file dialog must not count as clicking away from this popup.
        self._choosing = True
        try:
            return QFileDialog.getOpenFileName(self, title, str(self.deck), filter)[0]
        finally:
            self._choosing = False
            self.show()

    def choose_file(self):
        path = self._choose("Choose a Python file that draws the plot", "Python (*.py)")
        if path:
            self.file_field.setText(path)

    def choose_python(self):
        path = self._choose("Choose Python with matplotlib")
        if path:
            self.settings.setValue("plotPython", path)
            self.update_python()

    def automatic_python(self):
        self.settings.remove("plotPython")
        self.update_python()

    def cancel_pressed(self):
        self.cancelled = True
        self.close()

    def render_pressed(self):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_pressed()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._choosing and not self._delivered:
            self._delivered = True
            callback = self.on_cancel if self.cancelled else self.on_done
            if callback:
                callback() if self.cancelled else callback(self.spec)


class PlotMixin:
    def plot_of(self, oid: int):
        obj = ((self.scene or {}).get("objects") or {}).get(str(oid), {})
        if obj.get("kind") != "image":
            return None
        path = obj.get("imagePath")
        if not path and obj.get("image") and self.work_deck:
            path = self.work_deck / obj["image"]
        return PythonPlot.plot(path) if path else None

    def insert_python_plot_pressed(self):
        if self.scene_name and self.workspace:
            return self.open_plot_editor(PlotSpec(code=PythonPlot.template), None, None)

    def edit_plot(self, oid):
        return spawn(self._load_plot(oid, False), "Edit plot").future

    def rerun_plot(self, oid):
        return spawn(self._load_plot(oid, True), "Rerun plot").future

    def _load_plot(self, oid, rerun):
        location = self.plot_of(oid)
        if location:
            try:
                spec = yield run_blocking(PythonPlot.load, *location)
                if rerun:
                    return (yield self.commit_plot(spec, oid, True))
                return self.open_plot_editor(spec, oid, None)
            except Exception as exc:
                self.toast(str(exc), error=True)

    def open_plot_editor(self, spec, oid=None, error=None):
        if not self.workspace:
            return None
        previous = getattr(self, "_plot_editor", None)
        if previous:
            previous.cancel_pressed()
        editor = PlotEditor(spec, error, self.work_deck, self.settings, self.window)
        self._plot_editor = editor
        editor.on_done = lambda draft: self.commit_plot(draft, oid, False) if draft.file or (draft.code or "").strip() else None
        center = self.canvas.mapToGlobal(self.canvas.rect().center())
        editor.move(center.x() - editor.width() // 2, center.y() - editor.height() // 2)
        editor.show()
        (editor.file_field if spec.file else editor.code_view).setFocus()
        return editor

    def cancel_plot(self):
        """Cancel the running plot without losing its draft or the last good image."""
        run = getattr(self, "_plot_run", None)
        if run:
            run.cancel()

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
        self.busy += 1
        self.busy_text = "Running the plot's code…"
        self._plot_run = PythonPlot.run(spec, ws.work, self.runtime, self.settings)
        try:
            png = yield self._plot_run.start()
            if self.workspace is not ws:
                return
            dir, stem = old if old else (ws.work, (yield run_blocking(PythonPlot.free_stem, ws.work)))
            file = yield run_blocking(PythonPlot.store, png, stem, dir)
            self._plot_run.check_cancelled()
            if self.workspace is not ws:
                return
            path = dir / file
            relative = os.path.relpath(path, ws.work)
            if old_image and Path(old_image).absolute() == path.absolute():
                yield run_blocking(PythonPlot.save, spec, dir, stem)
                self.toast("The plot is up to date")
                return {"ok": True, "unchanged": True}
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
                raise RunError(result.get("error", "Could not update the plot"))
            yield run_blocking(PythonPlot.save, spec, dir, stem)
            if self.workspace is ws:
                if self.scene_name == name:
                    yield from self.load_scene(name, keep_stop=True)
                yield from self.reload_state()
                self.toast("Plot updated · Ctrl+Z goes back to the old one")
            return result
        except CancelledError:
            if self.workspace is ws and self.scene_name == name:
                self.open_plot_editor(spec, oid, "Plot cancelled; the previous picture is unchanged.")
        except Exception as exc:
            self.toast("The plot's code failed", error=True)
            if self.workspace is ws and self.scene_name == name:
                if isinstance(exc, RunError) and exc.no_seaborn:
                    self.offer_seaborn(exc.python, spec, oid)
                else:
                    self.open_plot_editor(spec, oid, str(exc))
        finally:
            self._plot_run = None
            self.busy -= 1
            self.busy_text = "Rendering…"

    def offer_seaborn(self, python, spec, oid=None):
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle("Seaborn isn't installed")
        dialog.setText(f"This plot uses seaborn, missing from:\n{python}\n\nInstall it there using python -m pip install seaborn? This downloads packages and changes that Python environment.")
        install = dialog.addButton("Install Seaborn", QMessageBox.ButtonRole.AcceptRole)
        close = dialog.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(close)
        dialog.exec()
        if dialog.clickedButton() is not install:
            return self.open_plot_editor(spec, oid, f"ModuleNotFoundError: No module named 'seaborn'\n\nInstall seaborn into {python}, or choose another Python.")
        return spawn(self._install_seaborn(python, spec, oid), "Install seaborn").future

    def _install_seaborn(self, python, spec, oid):
        self.busy += 1
        self.busy_text = "Installing seaborn…"
        ws, name = self.workspace, self.scene_name
        command = PythonPlot.install_seaborn(python, self.runtime)
        try:
            yield command.future
        except Exception as exc:
            if self.workspace is ws and self.scene_name == name:
                self.open_plot_editor(spec, oid, "pip install seaborn failed:\n" + str(exc))
        else:
            if self.workspace is ws and self.scene_name == name:
                self.commit_plot(spec, oid, True)
        finally:
            self.busy -= 1
            self.busy_text = "Rendering…"

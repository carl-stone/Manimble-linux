"""Project lifecycle and import/export UI; filesystem work stays on the bounded worker pool."""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QMarginsF, QSizeF, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPageLayout, QPageSize, QPdfWriter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                              QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                              QPushButton, QVBoxLayout, QWidget)

from . import prefs
from .projects import (PortableProject, RecentDecks, create_new_deck, resolve_project,
                       selected_movie_scenes, unpack_archive)
from .tasks import spawn
from .widgets import add_copy_button
from .workers import run_blocking


def _plain_message(parent, icon, title, text):
    box = QMessageBox(icon, title, title, QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setInformativeText(str(text))
    for label in box.findChildren(QLabel):
        label.setTextFormat(Qt.TextFormat.PlainText)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return box


def project_alert(parent, title, text):
    box = _plain_message(parent, QMessageBox.Icon.Warning, title, text)
    box.addButton(QMessageBox.StandardButton.Ok)
    add_copy_button(box)
    box.exec()


def ask(parent, title, text, choices, default="Cancel", *, copy=False):
    box = _plain_message(parent, QMessageBox.Icon.Question, title, text)
    buttons = {name: box.addButton(name, QMessageBox.ButtonRole.AcceptRole if name != default else QMessageBox.ButtonRole.RejectRole)
               for name in choices}
    if default in buttons:
        box.setDefaultButton(buttons[default])
        box.setEscapeButton(buttons[default])
    if copy:
        add_copy_button(box)
    box.exec()
    return next((name for name, button in buttons.items() if box.clickedButton() == button), default)


def _path_with_suffix(path, suffix):
    return path if not suffix or path.suffix.lower() == suffix else path.with_name(path.name + suffix)

def _require_new_destination(path):
    if os.path.lexists(path):
        raise FileExistsError(f"A file or folder already exists at {path}. Choose another name.")


def _require_directory(path):
    if not path.is_dir():
        raise ValueError(f"Choose an existing folder.\n\n{path}")


def _require_output_file(path):
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"Choose a regular file destination.\n\n{path}")


def _beamer_outputs(path):
    # LaTeX writes these next to the .tex, and the figures writer reuses its companion directory.
    yield path
    yield from (path.with_suffix(suffix) for suffix in
                (".aux", ".log", ".nav", ".out", ".pdf", ".snm", ".toc", ".vrb", ".dvi"))
    yield path.with_name(path.stem + "-figures")


def _require_beamer_destination(path):
    *files, figures = _beamer_outputs(path)
    for file in files:
        _require_output_file(file)
    if os.path.lexists(figures):
        if figures.is_symlink() or not figures.is_dir() or any(
                item.is_symlink() or not item.is_file() for item in figures.iterdir()):
            raise ValueError(f"Choose a destination with a regular figures folder.\n\n{figures}")


def _require_empty_import_destination(path):
    if os.path.lexists(path) and (path.is_symlink() or not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"{path} already contains a file or project. Choose a new folder or an empty folder.")



def _label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class DestinationSheet(QDialog):
    """A single destination/options sheet, with a native picker for Browse."""

    def __init__(self, parent, title, message, path, action="Save", *, suffix="", directory=False,
                 validate=None, replacements=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._directory = directory
        self._initial = Path(path)
        self._suffix = suffix
        self._action = action
        self._validate = validate
        self._replacements = replacements
        self.destination = None
        layout = QVBoxLayout(self)
        layout.addWidget(_label(message))
        row = QHBoxLayout()
        self.path = QLineEdit(str(path))
        self.path.setAccessibleName("Destination")
        if directory:
            self.path.setCursorPosition(len(str(path)))
        else:
            self.path.setSelection(len(str(path)) - len(self._initial.name), len(self._initial.stem) if suffix else len(self._initial.name))
        row.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.setAutoDefault(False)
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        layout.addLayout(row)
        self.options = QVBoxLayout()
        layout.addLayout(self.options)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        confirm.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(540, self.sizeHint().height())

    def _path(self):
        text = self.path.text()
        if not text.strip():
            raise ValueError("Choose a destination.")
        path = Path(text).expanduser()
        if not path.is_absolute():
            start = self._initial if self._directory else self._initial.parent
            path = start / path
        return _path_with_suffix(path.absolute(), self._suffix)

    def browse(self):
        try:
            start = str(self._path())
        except (ValueError, RuntimeError):
            start = str(self._initial)
        if self._directory:
            picker = QFileDialog(self, self.windowTitle(), start)
            picker.setFileMode(QFileDialog.FileMode.Directory)
            picker.setOption(QFileDialog.Option.ShowDirsOnly)
            picker.setLabelText(QFileDialog.DialogLabel.Accept, self._action)
            selected = picker.selectedFiles()[0] if picker.exec() else ""
        else:
            formats = f"{self._suffix[1:].upper()} (*{self._suffix} *{self._suffix.upper()})" if self._suffix else ""
            picker = QFileDialog(self, self.windowTitle(), str(Path(start).parent), formats)
            picker.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            picker.setFileMode(QFileDialog.FileMode.AnyFile)
            picker.setOption(QFileDialog.Option.DontConfirmOverwrite)
            picker.selectFile(Path(start).name)
            picker.setLabelText(QFileDialog.DialogLabel.Accept, self._action)
            selected = picker.selectedFiles()[0] if picker.exec() else ""
        if selected:
            self.path.setText(selected)

    def accept(self):
        try:
            destination = self._path()
            if self._validate is not None:
                self._validate(destination)
            if self._replacements is not None:
                paths = [p for p in self._replacements(destination) if os.path.lexists(p)]
                if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths):
                    raise ValueError("Choose a destination without linked or special files.")
                if paths and ask(self, "Replace existing files?", "\n".join(map(str, paths)),
                                 ["Replace", "Cancel"], copy=True) != "Replace":
                    return
        except (OSError, ValueError, RuntimeError) as exc:
            project_alert(self, self.windowTitle(), str(exc))
            return
        self.destination = destination
        super().accept()

    def choose(self):
        return self.destination if self.exec() == QDialog.DialogCode.Accepted else None


def write_pdf(pages, destination):
    """Paint engine frames only; never export editor overlays. Publish only after the writer closes."""
    if not pages:
        raise ValueError("The deck has no pages to export")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".manimble-pdf-", suffix=".pdf", dir=destination.parent)
    os.close(fd)
    painter = None
    try:
        writer = QPdfWriter(temporary)
        writer.setResolution(144)
        writer.setCreator("Manimble")
        for index, path in enumerate(pages):
            image = QImage(str(path))
            if image.isNull():
                raise OSError(f"Cannot read export frame: {path}")
            size = QPageSize(QSizeF(image.width() * 25.4 / 144, image.height() * 25.4 / 144), QPageSize.Unit.Millimeter)
            writer.setPageLayout(QPageLayout(size, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
            if painter is None:
                painter = QPainter(writer)
                if not painter.isActive():
                    raise OSError("Cannot create PDF painter")
            elif not writer.newPage():
                raise OSError("Cannot append PDF page")
            painter.drawImage(writer.pageLayout().paintRectPixels(writer.resolution()), image)
        painter.end()
        painter = None
        del writer
        os.replace(temporary, destination)
    finally:
        if painter is not None:
            painter.end()
        Path(temporary).unlink(missing_ok=True)
    return destination


class MovieExportOptions(QWidget):
    heights = [720, 1080, 1440, 2160]
    rates = [24, 30, 60]
    plain_note = "Every build plays once at its playback rate, hidden slides are left out, and there is no audio. Slides not yet built at this resolution are built first."
    script_note = "Every build plays once, then its last frame stays up while its script is spoken and its pointer moves are drawn, as in Play ▸ Scripted Presentation; a build with no script stays up the script’s hold. Hidden slides are left out. Slides not yet built at this resolution are built first."

    def __init__(self, scenes, current_slide=None, height=720, has_script=False, parent=None):
        super().__init__(parent)
        self.scenes = scenes
        self.current_slide = current_slide
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.which = QComboBox()
        self.which.addItems(["All slides", "Current slide" + (f" ({current_slide})" if current_slide else ""), "Some slides…"])
        if current_slide is None:
            self.which.model().item(1).setEnabled(False)
        self.list = QLineEdit()
        self.list.setPlaceholderText("numbers or names, e.g. 1-3, 7, Summary")
        self.list.setMinimumWidth(240)
        self.list.setEnabled(False)
        self.resolution = QComboBox()
        self.resolution.addItems(["720p (1280 × 720)", "1080p (1920 × 1080)", "1440p (2560 × 1440)", "4K (3840 × 2160)"])
        self.resolution.setCurrentIndex(self.heights.index(height) if height in self.heights else 0)
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0, 600)
        self.hold.setValue(2)
        self.hold.setAlignment(Qt.AlignmentFlag.AlignRight)
        hold_row = QHBoxLayout()
        hold_row.setSpacing(6)
        hold_row.addWidget(self.hold)
        hold_row.addWidget(_label("seconds on each build’s last frame"))
        hold_row.addStretch()
        self.fps = QComboBox()
        self.fps.addItems([f"{rate} fps" for rate in self.rates])
        self.fps.setCurrentIndex(1)
        self.script = QCheckBox("Export with script")
        self.script.setEnabled(has_script)
        self.script.setToolTip(
            "Play the show as Play ▸ Scripted Presentation does: each build’s words spoken and its pointer moves drawn"
            if has_script else "This deck has no script yet (Script, top right, adds words and pointer moves)")
        self.script.setAccessibleDescription(self.script.toolTip())
        self.note = _label(self.plain_note)
        self.disclosure = _label("Cloud speech may transmit script text and incur costs.")
        self.disclosure.hide()
        for label, field in (("Slides:", self.which), ("", self.list), ("Resolution:", self.resolution),
                             ("Hold:", hold_row), ("Frame rate:", self.fps), ("", self.script)):
            layout.addRow(label, field)
        layout.addRow(self.note)
        layout.addRow(self.disclosure)
        self.which.currentIndexChanged.connect(self.which_changed)
        self.script.toggled.connect(self.script_changed)

    def which_changed(self, *_):
        self.list.setEnabled(self.which.currentIndex() == 2)
        if self.list.isEnabled():
            self.list.setFocus()

    def script_changed(self, *_):
        self.hold.setEnabled(not self.with_script)
        self.note.setText(self.script_note if self.with_script else self.plain_note)
        self.disclosure.setVisible(self.with_script)

    @property
    def with_script(self):
        return self.script.isEnabled() and self.script.isChecked()

    @property
    def height(self):
        return self.heights[self.resolution.currentIndex()]

    @property
    def frame_rate(self):
        return self.rates[self.fps.currentIndex()]

    @property
    def hold_seconds(self):
        return self.hold.value()

    def choose_current_slide(self):
        if self.current_slide:
            self.which.setCurrentIndex(1)

    def selected_scenes(self):
        if self.which.currentIndex() == 1:
            if not self.current_slide:
                raise ValueError("There is no current slide.\n\nChoose another option.")
            return [self.current_slide]
        if self.which.currentIndex() == 2:
            return selected_movie_scenes(self.scenes, self.list.text())
        return None



class ProjectMixin:

    def init_project(self):
        if getattr(self, "_project_rpc", None) is self.rpc:
            return
        self._project_rpc = self.rpc
        self.recent_decks = RecentDecks
        self._project_operation = None
        self.rpc.stopped.connect(self._project_engine_stopped)
        QTimer.singleShot(0, self._startup_project_actions)

    def _project_engine_stopped(self, code, text):
        operation = getattr(self, "_project_operation", None)
        if operation and not operation[1].done():
            operation[1].set_exception(RuntimeError(f"Engine stopped ({code}): {text}"))

    def _startup_project_actions(self):
        if self.window._approved_close or self._closing or getattr(self.app, "_quitting", False):
            return
        if not self.rpc or not self.rpc.running:
            return
        setup = getattr(self, "setup", None)
        if setup is not None and setup.isVisible():
            if not getattr(self, "_waiting_for_project_setup", False):
                self._waiting_for_project_setup = True
                setup.finished.connect(self._after_project_setup, Qt.ConnectionType.SingleShotConnection)
            return
        packages, self._pending_packages = getattr(self, "_pending_packages", []), []
        if packages:
            def open_packages():
                for package in packages:
                    yield self.open_project(package).future
            spawn(open_packages())
            return
        if getattr(self, "_choose_on_start", False) and not self.workspace and not self.pending_deck:
            self._choose_on_start = False
            self.choose_deck()

    def _after_project_setup(self, _):
        self._waiting_for_project_setup = False
        QTimer.singleShot(0, self._startup_project_actions)

    @contextmanager
    def _project_busy(self, title, *, cancellable=True):
        if not cancellable:
            self.busy_pill.show(title, spinning=True, cancellable=False)
            try:
                yield
            finally:
                self.update_busy_pill()
            return
        self.busy_text = title
        self.busy += 1
        try:
            yield
        finally:
            self.busy -= 1
            self.busy_text = "Rendering…"

    def handle_project_event(self, event):
        operation = getattr(self, "_project_operation", None)
        if not operation or event.get("event") != operation[0]:
            return False
        _, future = operation
        phase = event.get("phase")
        if phase in ("done", "error", "cancelled"):
            if not future.done():
                if phase == "error":
                    future.set_exception(RuntimeError(event.get("error") or {
                        "import": "import failed", "export": "export failed", "export_movie": "movie export failed",
                    }[operation[0]]))
                else:
                    future.set_result(event if phase == "done" else None)
        elif phase in ("building", "encoding"):
            done, total = event.get("done", 0), event.get("total", 0)
            if operation[0] == "export_movie":
                what = "Encoding movie" if phase == "encoding" else "Building animations"
                self.busy_text = f"{what} {done + 1}/{total}: {event.get('scene', '')}"
            else:
                what = "Importing" if operation[0] == "import" else "Exporting"
                self.busy_text = f"{what} slide {done + 1}/{total}…"
            self.busy_progress = done / total if total else None
            self.update_busy_pill()
        return True

    def _project_job(self, command, request):
        titles = {"import": "Importing…", "export": "Exporting…", "export_movie": "Exporting movie…"}
        errors = {"import": "could not start the import", "export": "could not start the export",
                  "export_movie": "could not start the movie export"}
        future = Future()
        self._project_operation = command, future
        try:
            with self._project_busy(titles[command]):
                reply = yield self.call(command, request)
                if reply.get("ok") is not True or reply.get("error"):
                    raise RuntimeError(reply.get("error") or errors[command])
                result = yield future
            if result is None:
                self.toast("cancelled")
            return result
        finally:
            self._project_operation = None

    def _project_active(self):
        return bool(getattr(self, "_project_session_active", False) or
                    getattr(getattr(self, "script", None), "movie_active", False))

    @contextmanager
    def _project_session(self):
        self._require_project_idle()
        self._project_session_active = True
        try:
            yield
        finally:
            self._project_session_active = False

    def _require_project_idle(self):
        if self._project_active():
            raise RuntimeError("Another import or export is still running")

    def _start_project_task(self, task, failure_title):
        def protected():
            try:
                with self._project_session():
                    return (yield from task())
            except Exception as exc:
                project_alert(self.window, failure_title, str(exc))
        return spawn(protected())

    def unpack_target(self, package):
        package = Path(package)
        target = package.with_suffix("")
        number = 2
        while os.path.lexists(target):
            target = package.parent / f"{package.stem} {number}"
            number += 1
        return DestinationSheet(
            self.window, "Unpack Package",
            f"Manimble unpacks {package.name} into a project folder. You edit and save that folder; the package stays as it is.",
            target, "Unpack", validate=_require_new_destination).choose()

    def open_project(self, path):
        self._choose_on_start = False
        def resolve():
            if self.window._approved_close or self._closing or getattr(self.app, "_quitting", False):
                return
            try:
                source = Path(path).expanduser().absolute()
                if source.suffix.lower() == ".manimble" and not source.is_dir():
                    if not source.is_file():
                        raise FileNotFoundError(source)
                    setup = getattr(self, "setup", None)
                    if not self.rpc or not self.rpc.running or (setup is not None and setup.isVisible()):
                        pending = getattr(self, "_pending_packages", [])
                        self._pending_packages = pending
                        pending.append(source)
                        if self.rpc and self.rpc.running:
                            self._startup_project_actions()
                        return
                    with self._project_session():
                        target = self.unpack_target(source)
                        if target is None:
                            return
                        with self._project_busy("Unpacking package…", cancellable=False):
                            deck = yield run_blocking(unpack_archive, source, target, self.runtime)
                else:
                    deck = yield run_blocking(resolve_project, source)
                if not (self.window._approved_close or self._closing or getattr(self.app, "_quitting", False)):
                    self.open_in_window(deck)
            except Exception as exc:
                project_alert(self.window, "Could not open project", str(exc))
        return spawn(resolve())

    def open_in_window(self, deck):
        app = self.app
        if hasattr(app, "show"):
            return app.show(str(deck), self)
        return self.open(str(deck))

    def open_deck(self):
        self._open_picker("Open Deck or Project", "Choose a deck folder, a Manimble project folder, or a .manimble package",
                          "Manimble projects (*.manimble *.MANIMBLE *.py *.PY)")

    def import_manim_slides(self):
        self._open_picker("Import Manim Slides Project",
                          "Choose a Manim Slides project: its folder, or the .py file with its Slide classes",
                          "Python files (*.py *.PY)", python_only=True)

    def _open_picker(self, title, instruction, file_filter, *, python_only=False):
        choice = ask(self.window, title, instruction, ["Open Folder…", "Open File…", "Cancel"])
        last = self.settings.value("lastDeck")
        start = str(Path(last).parent if last else Path.home())
        if choice == "Open Folder…":
            path = QFileDialog.getExistingDirectory(self.window, title, start)
        elif choice == "Open File…":
            path, _ = QFileDialog.getOpenFileName(self.window, title, start, file_filter)
            if path and python_only and Path(path).suffix.lower() != ".py":
                project_alert(self.window, "Could not open project", instruction)
                return
        else:
            return
        if path:
            return self.open_project(path)

    def open_recent(self, path):
        if hasattr(path, "data"):
            path = path.data()
        if not path:
            return
        if not Path(path).exists():
            RecentDecks.forget(path, self.settings)
            project_alert(self.window, "Could not open deck", f"{path} no longer exists.")
            return
        self.open_in_window(path)

    def clear_recent(self):
        RecentDecks.clear(self.settings)

    def new_deck_at(self, destination, title=None, theme=None):
        return (yield run_blocking(create_new_deck, destination, title, theme, paths=self.paths))

    def new_deck(self):
        self._choose_on_start = False
        from .deck_themes import DeckTheme
        from .icon_art import THEME_SWATCH_SIZE

        sheet = DestinationSheet(self.window, "New Deck",
                                 "Where should the new deck be created? A folder with this name is made.",
                                 Path.home() / "Untitled Deck", "Create Deck")
        picker = QComboBox(sheet)
        picker.setIconSize(THEME_SWATCH_SIZE)
        for theme in DeckTheme.all():
            picker.addItem(theme.swatch, theme.name, theme.id)
        if picker.count():
            layout = QFormLayout()
            layout.addRow("Theme:", picker)
            sheet.options.addLayout(layout)
        path = sheet.choose()
        if path is None:
            return
        theme = picker.currentData()
        def create():
            deck = yield from self.new_deck_at(path, theme=theme)
            self.open_in_window(deck)
        return self._start_project_task(create, "Could not create the deck")

    def _finish_project_edits(self, draft_message="Fix the Python code before continuing"):
        try:
            result = yield from self.finish_edits()
        except Exception as exc:
            if self.source_draft is None:
                project_alert(self.window, "Could not finish edits", str(exc))
                return False
            result = False
        if self.source_draft is not None:
            self.toast(draft_message, error=True)
            self.source_view.setFocus()
            return False
        return result is not False

    def update_document_state(self):
        ws = self.workspace
        if not ws:
            return
        def update():
            try:
                changed = yield run_blocking(ws.changed_files)
                if self.workspace is not ws:
                    return
                modified = bool(changed) or self.source_draft is not None
                self.window.setWindowModified(modified)
                self.window.setWindowTitle(f"{ws.original.parent.name} / {ws.original.name}[*] — Manimble")
                self.window.setToolTip("Edited — not saved to the original" if modified else str(ws.original))
            except Exception as exc:
                self.toast(str(exc), error=True)
        spawn(update())

    def save_workspace(self):
        ws = self.workspace
        if not ws:
            return True
        if not (yield from self._finish_project_edits("Fix the Python code before saving")):
            return False
        return (yield from self.save_finished())

    def save_finished(self, *, closing=False):
        ws = self.workspace
        if ws is None:
            return True
        if self.source_draft is not None:
            self.toast("Fix the Python code before saving", error=True)
            self.source_view.setFocus()
            return False
        try:
            if not (yield run_blocking(ws.changed_files)):
                if not closing:
                    self.toast("Nothing to save")
                return True
            if (yield run_blocking(lambda: ws.original_missing)):
                choice = ask(
                    self.window, "Could not save",
                    f"The deck folder is no longer at {ws.original}: it was moved, renamed or deleted since it was opened. "
                    "Your edits are still in the working copy. Use File ▸ Save Project Copy… to save them in a new folder.",
                    ["Save Project Copy…", "Cancel"], copy=True)
                if choice == "Save Project Copy…":
                    self.save_project_folder_pressed()
                return False
            conflicts = yield run_blocking(ws.conflict_files)
            overwrite = False
            if conflicts:
                explanation = (" was modified outside Manimble. Overwrite it?" if closing else
                               " was modified outside Manimble since this copy was made. Overwrite it with your edits?")
                overwrite = ask(self.window, "The original changed on disk", ", ".join(conflicts) + explanation,
                                ["Overwrite", "Cancel"], copy=True) == "Overwrite"
                if not overwrite:
                    return False
            yield run_blocking(ws.save, overwrite_conflicts=overwrite)
            self.update_document_state()
            if not closing:
                self.toast("Saved to " + str(ws.original))
            return True
        except Exception as exc:
            project_alert(self.window, "Could not save", str(exc))
            return False

    def save_to_original(self):
        return self._start_project_task(self.save_workspace, "Could not save")

    def save_to_original_after_source_edit(self):
        return (yield from self.save_finished())

    def revert_to_original(self):
        def confirm():
            workspace = self.workspace
            if workspace is None or (not self.source_draft and not (yield run_blocking(workspace.changed_files))):
                self.toast("No changes to revert")
                return False
            if ask(self.window, "Discard all unsaved edits?", "The working copy goes back to the last saved version of the deck.",
                   ["Revert", "Cancel"]) != "Revert":
                return False
            return (yield from self.revert_working_copy())
        return self._start_project_task(confirm, "Could not revert")

    def _discard_project_draft(self):
        self.source_draft = self.source_edit_base = self.source_edit_scene_name = None
        self.canvas.setFocus()
        self.clear_source_error()
        self.render_source()

    def revert_working_copy(self):
        if not self.workspace:
            return False
        if self.source_draft is not None:
            self._discard_project_draft()
        if not (yield from self._finish_project_edits()):
            return False
        try:
            yield run_blocking(self.workspace.revert)
            yield self.call("clear_history", {})
            self.hide_feedback_pane()
            yield from self.open_feedback(self.workspace)
            self.open_script(self.workspace)
            self.last_changed = None
            yield from self.reload_state()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
            self.update_document_state()
            self.toast("Reverted")
            return True
        except Exception as exc:
            project_alert(self.window, "Could not revert", str(exc))
            return False

    def confirm_close(self, what="closing this deck"):
        opening = getattr(self, "_app_opening_future", None)
        if opening is not None:
            yield opening
        if self._project_active():
            project_alert(self.window, f"Cannot continue {what}",
                          "Wait for the current project operation to finish, or cancel it first.")
            return False
        if self.workspace is None:
            return True
        try:
            yield from self.finish_edits()
        except Exception as exc:
            if self.source_draft is None and self.rpc and self.rpc.running:
                project_alert(self.window, "Could not finish edits", str(exc))
                return False
        running = self.terminal.running_commands()
        if running:
            names = ", ".join(sorted(set(running)))
            if ask(self.window, f"{names} is still running in the terminal",
                   f"It stops before {what}. What it changed until then stays in the working copy, so you can still save it; "
                   "anything it would do after that doesn't happen.",
                   ["Stop and Continue", "Cancel"]) != "Stop and Continue":
                return False
            yield self.terminal.stop_all()
        if self.source_draft is not None:
            if ask(self.window, "Python code has not been applied",
                   f"Fix the code error and click outside the editor, or discard this draft before {what}.",
                   ["Keep Editing", "Discard Draft"], default="Keep Editing") != "Discard Draft":
                self.source_view.setFocus()
                return False
            self._discard_project_draft()
        changed = yield run_blocking(self.workspace.changed_files)
        if not changed:
            return True
        choice = ask(self.window, f"Save your edits to {self.workspace.original.name}?",
                     f"You are editing a working copy. Before {what}, save it to the original or discard the changes.",
                     ["Save", "Discard Changes", "Cancel"])
        if choice == "Save":
            return (yield from self.save_finished(closing=True))
        return choice == "Discard Changes"

    def finish(self):
        startup = getattr(self, "_app_startup", None)
        if startup is not None and not startup.done():
            yield startup
        setup = getattr(self, "setup", None)
        if setup is not None:
            setup.close()
        if self.feedback is not None:
            yield self.feedback.flush()
        self.close_animation_preview()
        self._poll_timer.stop()
        yield self.terminal.stop_all()
        if self.rpc is not None:
            yield self.rpc.stop_async()
        if self.workspace is not None:
            workspace = self.workspace
            yield run_blocking(workspace.discard)
            self.workspace = None

    def reopen_window(self):
        self.window.show()
        if self.workspace is None:
            self._choose_on_start = True
            self._startup_project_actions()

    def choose_deck(self):
        choice = ask(self.window, "No deck is open", "Create a new deck, or open a deck folder, project folder or .manimble package.",
                     ["New Deck…", "Open…", "Cancel"])
        if choice == "New Deck…":
            self.new_deck()
        elif choice == "Open…":
            self.open_deck()

    def show_about(self):
        from .app import show_about
        show_about(self.window)

    def save_project_folder_pressed(self):
        if not self.workspace:
            return
        parent = DestinationSheet(
            self.window, "Save a Portable Project Folder", "Choose where Manimble should create the project folder.",
            self.workspace.original.parent.parent, "Save Project Here", directory=True, validate=_require_directory).choose()
        if parent is not None:
            return self.save_project_copy(parent)

    def save_project_copy(self, parent):
        if not self.workspace:
            return
        destination = Path(parent) / self.workspace.project_name
        number = 2
        while os.path.lexists(destination):
            destination = Path(parent) / f"{self.workspace.project_name} ({number})"
            number += 1
        workspace = self.workspace
        return self._start_project_task(lambda: self.save_portable_project(workspace, destination),
                                        "Could not save portable project")

    def package_project_pressed(self):
        if not self.workspace:
            return
        destination = DestinationSheet(
            self.window, "Package Project for Sharing",
            "Create one portable .manimble file that includes the deck and its checked dependencies.",
            self.workspace.original.parent.parent / (self.workspace.project_name + ".manimble"),
            suffix=".manimble", validate=_require_output_file).choose()
        if destination is not None:
            workspace = self.workspace
            return self._start_project_task(lambda: self.save_portable_project(workspace, destination, archive=True),
                                            "Could not save portable project")

    def save_portable_project(self, workspace, destination, archive=False):
        if not (yield from self._finish_project_edits("Fix the Python code before saving a copy")):
            return False
        with self._project_busy("Checking slide dependencies…"):
            report = yield self.call("dependency_report", {})
            if self._cancelling:
                self.toast("cancelled")
                return False
        if report.get("error"):
            raise RuntimeError(report["error"])
        scanned, total = report.get("scannedScenes", 0), report.get("totalScenes", 0)
        if "scannedScenes" not in report or scanned != total:
            project_alert(self.window, "Dependency scan incomplete",
                          f"Manimble checked {scanned} of {total} slides. Please try again.")
            return False
        failures = report.get("failures", [])
        if failures:
            details = "\n".join(f"{item['scene']}: {item['error']}" for item in failures[:5])
            message = (f"Manimble found dependencies in {scanned} slides, but the package may be incomplete.\n\n"
                       f"{details}\n\nContinue with the project copy?")
            if ask(self.window, "Some slides did not render during the dependency check", message,
                   ["Continue Anyway", "Cancel"], copy=True) != "Continue Anyway":
                return False
        warning = ("Dynamic dependency tracking cannot discover files code has not read. "
                   "Hardcoded absolute Python paths are not rewritten. Linked/external assets are not isolated by Save.")
        if ask(self.window, "Create portable project?", warning + f"\n\n{destination}",
               ["Create", "Cancel"]) != "Create":
            return False
        destination = Path(destination)
        if archive:
            _require_output_file(destination)
            overwrite = destination.exists()
            if overwrite and ask(self.window, "Replace existing archive?", str(destination),
                                 ["Replace", "Cancel"]) != "Replace":
                return False
            yield run_blocking(PortableProject.create_archive, workspace, report["dependencies"], destination, overwrite=overwrite)
        else:
            yield run_blocking(PortableProject.create_folder, workspace, report["dependencies"], destination)
        self.toast(("Packaged " if archive else "Saved project folder ") + destination.name)
        return True

    def import_power_point(self):
        self.run_import("PowerPoint")

    def import_beamer(self):
        self.run_import("Beamer (LaTeX)")

    def run_import(self, label):
        beamer = label == "Beamer (LaTeX)"
        formats = "Beamer (*.tex *.TEX *.ltx *.LTX)" if beamer else "PowerPoint (*.pptx *.PPTX);;All files (*)"
        instruction = f"Choose a {label} presentation to import"
        selected, _ = QFileDialog.getOpenFileName(self.window, instruction, str(Path.home()), formats)
        if not selected:
            return
        source = Path(selected)
        suffix = source.suffix.lower()
        if suffix in (".key", ".ppt"):
            project_alert(self.window, "Requires macOS",
                          "Keynote .key and legacy .ppt conversion require macOS. Export to .pptx first.")
            return
        if suffix not in ((".tex", ".ltx") if beamer else (".pptx",)):
            project_alert(self.window, f"Could not import {label}", instruction)
            return
        destination = DestinationSheet(
            self.window, f"Import {label}",
            "Where should the new Manim Slides deck be created? A folder with this name is made.",
            source.with_suffix(""), "Create Deck", validate=_require_empty_import_destination).choose()
        if destination is None:
            return

        def perform():
            if beamer and not (yield from self.offer_latex_packages(source, destination)):
                return
            _require_empty_import_destination(destination)
            result = yield from self._project_job("import", {"src": str(source), "dest": str(destination)})
            if result is None:
                return
            warnings = list(map(str, result.get("warnings", [])))
            suffix = " — " + "; ".join(warnings[:2]) if warnings else ""
            self.toast(f"Imported {result.get('slides', 0)} slides{suffix}", error=bool(warnings))
            if result.get("deck"):
                self.open_in_window(result["deck"])
        return self._start_project_task(perform, "Could not import")

    def _export_base(self):
        original = self.workspace.original
        return "presentation" if original.parent.name == "slides" else original.name

    def _export_sheet(self, title, message, suffix, *, name=None, directory=None, check=None, outputs=None):
        original = self.workspace.original

        def validate(path):
            _require_output_file(path)
            if check:
                check(path)

        return DestinationSheet(self.window, title, message,
                                (directory or original.parent) / (name or self._export_base() + suffix),
                                suffix=suffix, validate=validate, replacements=outputs or (lambda path: (path,)))

    def share_pdf_copy_destination(self, path):
        path = Path(path)
        return DestinationSheet(
            self.window, "Save PDF Copy", "Save a copy of the PDF.",
            self.workspace.original.parent / path.name if self.workspace else Path.home() / path.name,
            suffix=".pdf", validate=_require_output_file, replacements=lambda output: (output,)).choose()

    def export_power_point(self):
        self.run_export("pptx")

    def export_beamer(self):
        self.run_export("beamer")

    def run_export(self, kind):
        if not self.workspace:
            return
        if kind == "pptx":
            sheet = self._export_sheet("Export to PowerPoint", "Export to PowerPoint", ".pptx")
            editable_label = "Editable text boxes (recommended)"
        elif kind == "beamer":
            sheet = self._export_sheet(
                "Export to Beamer", "Export to Beamer (LaTeX; pictures go in a -figures folder beside it)", ".tex",
                check=_require_beamer_destination, outputs=_beamer_outputs)
            editable_label = "Editable text and LaTeX formulas (recommended)"
        else:
            project_alert(self.window, "Unsupported export",
                          "Use PowerPoint .pptx or Beamer .tex; Keynote conversion requires macOS.")
            return
        builds = QCheckBox("Every build as its own slide")
        builds.setChecked(True)
        mode = QComboBox()
        mode.addItems([editable_label, "Pictures only (exact look)"])
        sheet.options.addWidget(builds)
        sheet.options.addWidget(mode)
        path = sheet.choose()
        if path is None:
            return
        request = {"kind": kind, "path": str(path), "builds": builds.isChecked(), "editable": mode.currentIndex() == 0}

        def perform():
            if not (yield from self._finish_project_edits()):
                return
            result = yield from self._project_job("export", request)
            if result is None:
                return
            self.toast("Exported " + path.name)
            self._reveal_export(path)
            if result.get("pdfError"):
                message = f"{path.name} was saved, but LaTeX stopped with:\n\n{result['pdfError']}"
                log = path.with_suffix(".log")
                if log.is_file():
                    message += f"\n\nThe full log is in {log.name} next to it."
                project_alert(self.window, "The PDF could not be compiled", message)
        return self._start_project_task(perform, "Could not export")

    def export_python_pressed(self):
        if not self.workspace:
            return
        workspace = self.workspace

        def no_authored_source_overwrite(path):
            output = path.resolve()
            if output.exists() and any(output.is_relative_to(root) for root in (workspace.original, workspace.work)):
                raise ValueError("Choose a new filename or a destination outside the deck. "
                                 "Export creates a single-file copy; it cannot replace the deck’s editable source.")

        sheet = self._export_sheet(
            "Export Python",
            "Write the whole deck as one Python file (helper modules inlined, slides in show order). "
            "The deck's own .py files are already the editable source; this is a single-file copy.",
            ".py", name=workspace.original.parent.name + "_deck.py", directory=workspace.original,
            check=no_authored_source_overwrite)
        hidden = QCheckBox("Include hidden slides")
        sheet.options.addWidget(hidden)
        path = sheet.choose()
        if path is None:
            return
        include_hidden = hidden.isChecked()

        def perform():
            if not (yield from self._finish_project_edits()):
                return
            with self._project_busy("Exporting…", cancellable=False):
                result = yield self.call("export_python", {"path": str(path), "hidden": include_hidden})
            if result.get("ok") is not True or result.get("error"):
                raise RuntimeError(result.get("error") or "could not write the Python file")
            self.toast("Exported " + (result.get("message") or path.name))
            self._reveal_export(path)
        return self._start_project_task(perform, "Could not export")

    def make_pdf(self, builds=True, destination=None):
        with self._project_session():
            if not self.workspace or not (yield from self._finish_project_edits()):
                return None
            with self._project_busy("Exporting…"):
                result = yield self.call("pages", {"builds": builds})
                if self._cancelling:
                    self.toast("cancelled")
                    return None
                if result.get("error") or not result.get("pages"):
                    self.toast("Could not render all slides for export", error=True)
                    if result.get("error"):
                        project_alert(self.window, "Could not render all slides for export", result["error"])
                    return None
            with self._project_busy("Exporting…", cancellable=False):
                if destination is None:
                    destination = yield run_blocking(self._share_pdf_path, self.paths.cache, self._export_base() + ".pdf")
                return (yield run_blocking(write_pdf, result["pages"], destination))

    @staticmethod
    def _share_pdf_path(cache, name):
        # A later Share of a different deck or PDF variant must not replace an earlier result.
        directory = cache / "export"
        directory.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=directory)) / name

    def export_pdf_pressed(self):
        if not self.workspace:
            return
        sheet = self._export_sheet("Export to PDF", "Export to PDF", ".pdf")
        builds = QCheckBox("Every build as its own page")
        builds.setChecked(prefs.get_bool("pdfExportBuilds", True))
        sheet.options.addWidget(builds)
        path = sheet.choose()
        if path is None:
            return
        every_build = builds.isChecked()
        prefs.set("pdfExportBuilds", every_build)

        def perform():
            try:
                if (yield from self.make_pdf(every_build, path)):
                    self.toast("Saved " + path.name)
            except Exception as exc:
                project_alert(self.window, "Could not save", str(exc))
        return spawn(perform())

    def export_movie(self):
        self.run_movie_export()

    def export_slide_movie(self, scene):
        self.run_movie_export(scene)

    def run_movie_export(self, slide=None):
        scenes = (self.state or {}).get("scenes", [])
        if not self.workspace or not scenes:
            return
        store = self.script.store
        options = MovieExportOptions(scenes, slide or self.scene_name, self.animation_height, bool(store and not store.is_empty))
        if slide:
            options.choose_current_slide()
        sheet = self._export_sheet(
            "Export Movie", f"Export slide {slide} as a movie" if slide else "Export the animated show as a movie",
            ".mp4", name=f"{self._export_base()}_{slide}.mp4" if slide else None, check=lambda _: options.selected_scenes())
        sheet.options.addWidget(options)
        path = sheet.choose()
        if path is None:
            return
        names = options.selected_scenes()
        with_script = options.with_script
        request = {"path": str(path), "height": options.height, "hold": options.hold_seconds, "fps": options.frame_rate}
        if names is not None:
            request["scenes"] = names
        def perform():
            try:
                if not (yield from self._finish_project_edits()):
                    return
                if with_script:
                    chosen = names if names is not None else [s["name"] for s in scenes if not s.get("hidden")]
                    result = yield from self.export_scripted_movie(request, store=store, scenes=chosen, to=path)
                    if result is None:
                        return
                    count = len(chosen)
                else:
                    result = yield from self._project_job("export_movie", request)
                    if result is None:
                        return
                    count = result.get("slides", 0)
                slides = f" ({count} slide{'s' if count != 1 else ''})" if count else ""
                self.toast(f"Exported {path.name}{slides}")
                self._reveal_export(path)
            except Exception:
                if with_script and getattr(self.script, "movie_cancelled", False):
                    self.toast("cancelled")
                else:
                    raise
        return self._start_project_task(perform, "Could not export movie")

    @staticmethod
    def _reveal_export(path):
        # Native file managers that implement this desktop service select the result.
        from PySide6.QtCore import QMetaType, QUrl
        from PySide6.QtDBus import QDBusArgument, QDBusConnection, QDBusMessage

        bus = QDBusConnection.sessionBus()
        if bus.isConnected():
            message = QDBusMessage.createMethodCall("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                                                   "org.freedesktop.FileManager1", "ShowItems")
            urls = QDBusArgument()
            urls.beginArray(QMetaType.Type.QString)
            urls << QUrl.fromLocalFile(str(path)).toString()
            urls.endArray()
            message.setArguments([urls, ""])
            bus.asyncCall(message)

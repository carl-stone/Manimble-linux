"""Project lifecycle and import/export UI; filesystem work stays on the bounded worker pool."""
from __future__ import annotations

from concurrent.futures import Future
import inspect
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QMarginsF, QSizeF
from PySide6.QtGui import QImage, QPainter, QPageLayout, QPageSize, QPdfWriter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                              QFileDialog, QFormLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog,
                              QVBoxLayout)

from . import prefs
from .projects import (PortableProject, RecentDecks, create_new_deck, resolve_project,
                       selected_movie_scenes, unpack_archive)
from .tasks import spawn
from .workers import run_blocking


def ask(parent, title, text, choices, default="Cancel"):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    buttons = {name: box.addButton(name, QMessageBox.ButtonRole.AcceptRole if name != default else QMessageBox.ButtonRole.RejectRole)
               for name in choices}
    if default in buttons:
        box.setDefaultButton(buttons[default])
        box.setEscapeButton(buttons[default])
    box.exec()
    return next((name for name, button in buttons.items() if box.clickedButton() == button), default)


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


class MovieExportOptions(QDialog):
    heights = [720, 1080, 1440, 2160]
    rates = [24, 30, 60]

    def __init__(self, scenes, current_slide=None, height=720, has_script=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Movie Export Options")
        self.scenes = scenes
        self.current_slide = current_slide
        self.view = self
        layout = QFormLayout(self)
        self.which = QComboBox()
        self.which.addItems(["All slides", "Current slide" + (f" ({current_slide})" if current_slide else ""), "Some slides…"])
        if current_slide is None:
            self.which.model().item(1).setEnabled(False)
        self.list = QLineEdit()
        self.list.setPlaceholderText("numbers or names, e.g. 1-3, 7, Summary")
        self.list.setEnabled(False)
        self.resolution = QComboBox()
        self.resolution.addItems(["720p (1280 × 720)", "1080p (1920 × 1080)", "1440p (2560 × 1440)", "4K (3840 × 2160)"])
        self.resolution.setCurrentIndex(self.heights.index(height) if height in self.heights else 0)
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0, 600)
        self.hold.setValue(2)
        self.hold.setSuffix(" seconds")
        self.fps = QComboBox()
        self.fps.addItems([f"{rate} fps" for rate in self.rates])
        self.fps.setCurrentIndex(1)
        self.script = QCheckBox("Export with script")
        self.script.setEnabled(has_script)
        self.script.setToolTip("Narration and pointer moves use the deck's script; cloud speech may transmit text and incur costs." if has_script else "This deck has no script yet")
        self.note = QLabel("Every build plays once at its playback rate, with no audio. Hidden slides are left out. Unbuilt slides are built first.")
        self.note.setWordWrap(True)
        for label, widget in (("Slides:", self.which), ("", self.list), ("Resolution:", self.resolution), ("Hold:", self.hold), ("Frame rate:", self.fps), ("", self.script), ("", self.note)):
            layout.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.which.currentIndexChanged.connect(self.which_changed)
        self.script.toggled.connect(self.script_changed)

    def which_changed(self, *_):
        self.list.setEnabled(self.which.currentIndex() == 2)
        if self.list.isEnabled():
            self.list.setFocus()

    def script_changed(self, *_):
        self.hold.setEnabled(not self.with_script)
        self.note.setText("Every build plays once, then its script is spoken and pointer moves drawn; unscripted builds use the script hold. Hidden slides are left out. Unbuilt slides are built first." if self.with_script else
                          "Every build plays once at its playback rate, with no audio. Hidden slides are left out. Unbuilt slides are built first.")

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
                raise ValueError("There is no current slide")
            return [self.current_slide]
        if self.which.currentIndex() == 2:
            return selected_movie_scenes(self.scenes, self.list.text())
        return None

    def accept(self):
        try:
            self.selected_scenes()
        except ValueError as exc:
            QMessageBox.warning(self, "Choose slides", str(exc))
            return
        super().accept()


class ProjectMixin:

    def init_project(self):
        if getattr(self, "_project_rpc", None) is self.rpc:
            return
        self._project_rpc = self.rpc
        self.recent_decks = RecentDecks
        self._project_operation = None
        self.rpc.event.connect(self.handle_project_event)
        self.rpc.stopped.connect(self._project_engine_stopped)

    def _project_engine_stopped(self, code, text):
        operation = getattr(self, "_project_operation", None)
        if operation and not operation[1].done():
            operation[1].set_exception(RuntimeError(f"Engine stopped ({code}): {text}"))

    def handle_project_event(self, event):
        operation = getattr(self, "_project_operation", None)
        if not operation or event.get("event") != operation[0]:
            return False
        _, future, progress = operation
        phase = event.get("phase")
        if phase in ("done", "error", "cancelled"):
            if not future.done():
                if phase == "done":
                    future.set_result(event)
                else:
                    future.set_exception(RuntimeError(event.get("error") or f"Operation {phase}"))
        else:
            done, total = event.get("done", 0), event.get("total", 0)
            progress.setLabelText(f"{event.get('scene') or event.get('step') or phase or 'Working'} ({done}/{total})")
            progress.setRange(0, max(0, int(total)))
            progress.setValue(int(done))
        return True

    def _project_job(self, command, request):
        if getattr(self, "_project_operation", None):
            raise RuntimeError("Another import or export is still running")
        progress = QProgressDialog("Working…", "Cancel", 0, 0, self.window)
        progress.setWindowTitle({"import": "Import Presentation", "export": "Export Presentation", "export_movie": "Export Movie"}.get(command, command))
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.canceled.connect(lambda: self.call("cancel", {}))
        future = Future()
        self._project_operation = (command, future, progress)
        progress.show()
        try:
            reply = yield self.call(command, request)
            if reply.get("ok") is False or reply.get("error"):
                raise RuntimeError(reply.get("error", "Could not start operation"))
            return (yield future)
        finally:
            self._project_operation = None
            progress.close()
            progress.deleteLater()

    def unpack_target(self, package):
        package = Path(package)
        target = package.with_suffix("")
        number = 2
        while target.exists():
            target = package.parent / f"{package.stem} {number}"
            number += 1
        name, _ = QFileDialog.getSaveFileName(self.window, "Unpack Package into a new project folder (the archive stays unchanged)", str(target), options=QFileDialog.Option.DontConfirmOverwrite)
        return Path(name) if name else None

    def open_project(self, path):
        def resolve():
            try:
                source = Path(path).expanduser().absolute()
                if source.suffix.lower() == ".manimble" and not source.is_dir():
                    target = self.unpack_target(source)
                    if target is None:
                        return
                    deck = yield run_blocking(unpack_archive, source, target, self.runtime)
                else:
                    deck = yield run_blocking(resolve_project, source)
                self.open_in_window(deck)
            except Exception as exc:
                self.alert("Could not open project", str(exc))
        return spawn(resolve())

    def open_in_window(self, deck):
        app = self.app
        if app is not None:
            return app.show(str(deck), self)
        return self.open(str(deck))

    def open_deck(self):
        choice = ask(self.window, "Open Deck or Project", "Open a deck/project folder, Python file, or .manimble package.", ["Open Folder…", "Open File…", "Cancel"])
        start = str(Path(self.settings.value("lastDeck", str(Path.home()))).parent)
        if choice == "Open Folder…":
            path = QFileDialog.getExistingDirectory(self.window, "Open Deck or Project Folder", start)
        elif choice == "Open File…":
            path, _ = QFileDialog.getOpenFileName(self.window, "Open Project", start, "Manimble projects (*.manimble *.py);;All files (*)")
        else:
            return
        if path:
            self.open_project(path)

    def import_manim_slides(self):
        self.open_deck()

    def open_recent(self, path):
        if hasattr(path, "data"):
            path = path.data()
        if not path:
            return
        if not Path(path).exists():
            RecentDecks.forget(path, self.settings)
            self.alert("Could not open deck", f"{path} no longer exists.")
            return
        self.open_in_window(path)

    def clear_recent(self):
        RecentDecks.clear(self.settings)

    def new_deck_at(self, destination, title=None, theme=None):
        return (yield run_blocking(create_new_deck, destination, title, theme))

    def new_deck(self):
        path, _ = QFileDialog.getSaveFileName(self.window, "Create New Deck in a project folder", str(Path.home() / "Untitled Deck"), options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        dialog = QDialog(self.window)
        dialog.setWindowTitle("New Deck Theme")
        layout = QFormLayout(dialog)
        picker = QComboBox()
        picker.addItem("Plain", None)
        for theme in ("chalkboard", "ember", "midnight", "paper", "parchment", "slate", "terminal"):
            picker.addItem(theme.title(), theme)
        layout.addRow("Theme", picker)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        def create():
            try:
                deck = yield from self.new_deck_at(path, theme=picker.currentData())
                self.open_in_window(deck)
            except Exception as exc:
                self.alert("Could not create deck", str(exc))
        spawn(create())

    def _finish_project_edits(self):
        try:
            result = yield from self.finish_edits()
            if result is False or self.source_draft is not None:
                self.toast("Fix the Python code before continuing", error=True)
                return False
            if self.feedback is not None:
                yield self.feedback.flush()
            reply = yield self.call("settle", {})
            if reply.get("error") or reply.get("ok") is False:
                raise RuntimeError(reply.get("error", "Could not finish edits"))
            return True
        except Exception as exc:
            self.alert("Could not finish edits", str(exc))
            return False

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
        if not (yield from self._finish_project_edits()):
            return False
        return (yield from self.save_finished())

    def save_finished(self):
        ws = self.workspace
        if ws is None:
            return True
        if self.source_draft is not None:
            self.toast("Fix the Python code before saving", error=True)
            return False
        try:
            if (yield run_blocking(lambda: ws.original_missing)):
                choice = ask(self.window, "The original deck is missing", f"Your edits remain in the working copy. Save a project copy to a new folder?\n\n{ws.original}", ["Save Project Copy…", "Cancel"])
                if choice == "Save Project Copy…":
                    self.save_project_folder_pressed()
                return False
            conflicts = yield run_blocking(ws.conflict_files)
            overwrite = False
            if conflicts:
                overwrite = ask(self.window, "The original changed on disk", "Overwrite these externally changed files with your edits?\n\n" + "\n".join(conflicts), ["Overwrite", "Cancel"]) == "Overwrite"
                if not overwrite:
                    return False
            yield run_blocking(ws.save, overwrite_conflicts=overwrite)
            self.update_document_state()
            self.toast("Saved to " + str(ws.original))
            return True
        except Exception as exc:
            self.alert("Could not save", str(exc))
            return False

    def save_to_original(self):
        return spawn(self.save_workspace())

    def save_to_original_after_source_edit(self):
        return (yield from self.save_finished())

    def revert_to_original(self):
        if self.workspace and ask(self.window, "Discard all unsaved edits?", "The working copy goes back to the last saved baseline, not current external changes.", ["Revert", "Cancel"]) == "Revert":
            return spawn(self.revert_working_copy())

    def revert_working_copy(self):
        if not self.workspace:
            return False
        if not (yield from self._finish_project_edits()):
            return False
        try:
            yield run_blocking(self.workspace.revert)
            yield self.call("clear_history", {})
            yield from self.reload_state()
            if self.scene_name:
                yield from self.load_scene(self.scene_name, keep_stop=True)
            self.update_document_state()
            self.toast("Reverted")
            return True
        except Exception as exc:
            self.alert("Could not revert", str(exc))
            return False

    def confirm_close(self, what="closing this deck"):
        if self.workspace is None:
            return True
        if not (yield from self._finish_project_edits()):
            return False
        running = self.terminal.running_commands()
        if running:
            if ask(self.window, "Terminal commands are running", ", ".join(sorted(set(running))) + f" must stop before {what}. Their changes remain in the working copy.", ["Stop and Continue", "Cancel"]) != "Stop and Continue":
                return False
            yield self.terminal.stop_all()
        changed = yield run_blocking(self.workspace.changed_files)
        if not changed:
            return True
        choice = ask(self.window, "Save your edits?", f"You are editing a working copy. Before {what}, save to the original or discard changes.", ["Save", "Discard Changes", "Cancel"])
        if choice == "Save":
            return (yield from self.save_workspace())
        return choice == "Discard Changes"

    def finish(self):
        if self.feedback is not None:
            yield self.feedback.flush()
        self.close_animation_preview()
        self._poll_timer.stop()
        yield self.terminal.stop_all()
        yield self.rpc.stop_async()
        if self.workspace is not None:
            workspace = self.workspace
            yield run_blocking(workspace.discard)
            self.workspace = None

    def reopen_window(self):
        self.window.show()
        if self.workspace is None:
            self.choose_deck()

    def choose_deck(self):
        choice = ask(self.window, "No deck is open", "Create a new deck or open a deck folder, project or .manimble package.", ["New Deck…", "Open…", "Cancel"])
        if choice == "New Deck…":
            self.new_deck()
        elif choice == "Open…":
            self.open_deck()

    def save_project_folder_pressed(self):
        if not self.workspace:
            return
        parent = QFileDialog.getExistingDirectory(self.window, "Save Project Copy Here")
        if parent:
            self.save_project_copy(parent)

    def save_project_copy(self, parent):
        if not self.workspace:
            return
        destination = Path(parent) / self.workspace.project_name
        number = 2
        while destination.exists():
            destination = Path(parent) / f"{self.workspace.project_name} ({number})"
            number += 1
        return spawn(self.save_portable_project(self.workspace, destination, False))

    def package_project_pressed(self):
        if not self.workspace:
            return
        name, _ = QFileDialog.getSaveFileName(self.window, "Package Project for Sharing", self.workspace.project_name + ".manimble", "Manimble project (*.manimble)")
        if name:
            destination = Path(name)
            if destination.suffix.lower() != ".manimble":
                destination = Path(str(destination) + ".manimble")
            spawn(self.save_portable_project(self.workspace, destination, True))

    def save_portable_project(self, workspace, destination, archive=False):
        if not (yield from self._finish_project_edits()):
            return False
        try:
            report = yield self.call("dependency_report", {})
            if report.get("scannedScenes") != report.get("totalScenes") or "scannedScenes" not in report:
                raise RuntimeError(f"Dependency scan incomplete: {report.get('scannedScenes')} of {report.get('totalScenes')} slides checked")
            failures = report.get("failures", [])
            if failures and ask(self.window, "Some slides did not render", "The copy may be incomplete.\n\n" + "\n".join(f"{f['scene']}: {f['error']}" for f in failures) + "\n\nContinue?", ["Continue Anyway", "Cancel"]) != "Continue Anyway":
                return False
            warning = "Dynamic dependency tracking cannot discover files code has not read. Hardcoded absolute Python paths are not rewritten. Linked/external assets are not isolated by Save."
            if ask(self.window, "Create portable project?", warning, ["Create", "Cancel"]) != "Create":
                return False
            overwrite = False
            if archive and Path(destination).exists():
                overwrite = ask(self.window, "Replace existing archive?", str(destination), ["Replace", "Cancel"]) == "Replace"
                if not overwrite:
                    return False
            if archive:
                yield run_blocking(PortableProject.create_archive, workspace, report["dependencies"], destination, overwrite=overwrite)
            else:
                yield run_blocking(PortableProject.create_folder, workspace, report["dependencies"], destination)
            self.toast("Saved " + str(destination))
            return True
        except Exception as exc:
            self.alert("Could not save portable project", str(exc))
            return False

    def import_power_point(self):
        self.run_import("PowerPoint")

    def import_beamer(self):
        self.run_import("Beamer (LaTeX)")

    def run_import(self, label):
        source, _ = QFileDialog.getOpenFileName(self.window, "Import " + label, "", "Beamer (*.tex *.ltx)" if label.startswith("Beamer") else "PowerPoint (*.pptx);;All files (*)")
        if not source:
            return
        if Path(source).suffix.lower() in (".key", ".ppt"):
            self.alert("Requires macOS", "Keynote .key and legacy .ppt conversion require macOS. Export to .pptx first.")
            return
        destination, _ = QFileDialog.getSaveFileName(self.window, "Create imported project folder", str(Path(source).with_suffix("")), options=QFileDialog.Option.DontConfirmOverwrite)
        if not destination:
            return
        def perform():
            try:
                if Path(source).suffix.lower() in (".tex", ".ltx"):
                    if not (yield from self.offer_latex_packages(Path(source), Path(destination))):
                        return
                result = yield from self._project_job("import", {"src": source, "dest": destination})
                if result.get("warnings"):
                    self.alert("Import warnings", "\n".join(map(str, result["warnings"])))
                deck = result.get("deck") or str(Path(destination) / "deck")
                self.open_in_window(deck)
            except Exception as exc:
                self.alert("Could not import", str(exc))
        spawn(perform())

    def _export_options(self, title, *, editable=False, hidden=False, pdf=False):
        dialog = QDialog(self.window)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        builds = QCheckBox("Include hidden slides" if hidden else "Every build as its own page" if pdf else "Every build as its own slide")
        builds.setChecked(False if hidden else prefs.get_bool("pdfExportBuilds", True) if pdf else True)
        layout.addWidget(builds)
        mode = QComboBox()
        if editable:
            mode.addItems(["Editable text/formulas (recommended)", "Pictures only (exact look)"])
            layout.addWidget(mode)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        if pdf:
            prefs.set("pdfExportBuilds", builds.isChecked())
        return builds.isChecked(), mode.currentIndex() == 0

    def _export_filename(self, title, extension):
        if not self.workspace:
            return None
        name, _ = QFileDialog.getSaveFileName(self.window, title, self.workspace.original.name + "." + extension, f"{extension.upper()} (*.{extension})")
        if not name:
            return None
        return name if name.lower().endswith("." + extension) else name + "." + extension

    def export_power_point(self):
        self.run_export("pptx")

    def export_beamer(self):
        self.run_export("beamer")

    def run_export(self, kind):
        if kind not in ("pptx", "beamer"):
            self.alert("Unsupported export", "Use PowerPoint .pptx or Beamer .tex; Keynote conversion requires macOS.")
            return
        path = self._export_filename("Export " + kind + (" (companion -figures folder)" if kind == "beamer" else ""), "tex" if kind == "beamer" else "pptx")
        if not path:
            return
        options = self._export_options("Export Options", editable=True)
        if options is None:
            return
        def perform():
            try:
                if not (yield from self._finish_project_edits()):
                    return
                result = yield from self._project_job("export", {"kind": kind, "path": path, "builds": options[0], "editable": options[1]})
                if result.get("pdfError"):
                    self.alert("Export warning", str(result["pdfError"]))
                self.toast("Exported " + path)
            except Exception as exc:
                self.alert("Could not export", str(exc))
        spawn(perform())

    def export_python_pressed(self):
        path = self._export_filename("Export whole-deck Python", "py")
        if not path:
            return
        options = self._export_options("Python Export", hidden=True)
        if options is None:
            return
        def perform():
            try:
                if not (yield from self._finish_project_edits()):
                    return
                result = yield self.call("export_python", {"path": path, "hidden": options[0]})
                if result.get("error") or result.get("ok") is False:
                    raise RuntimeError(result.get("error", "Python export failed"))
                self.toast("Exported " + path)
            except Exception as exc:
                self.alert("Could not export", str(exc))
        spawn(perform())

    def make_pdf(self, builds=True, destination=None):
        if not (yield from self._finish_project_edits()):
            return None
        result = yield self.call("pages", {"builds": builds})
        if result.get("error") or not result.get("pages"):
            raise RuntimeError(result.get("error", "No rendered pages returned"))
        if destination is None:
            destination = self.paths.cache / "export" / (self.workspace.original.name + ".pdf")
        return (yield run_blocking(write_pdf, result["pages"], destination))

    def export_pdf_pressed(self):
        path = self._export_filename("Export PDF", "pdf")
        if not path:
            return
        options = self._export_options("PDF Export", pdf=True)
        if options is None:
            return
        def perform():
            try:
                result = yield from self.make_pdf(options[0], path)
                if result:
                    self.toast("Exported " + path)
            except Exception as exc:
                self.alert("Could not export PDF", str(exc))
        spawn(perform())

    def export_movie(self):
        self.run_movie_export()

    def export_slide_movie(self, scene):
        self.run_movie_export(scene)

    def run_movie_export(self, slide=None):
        scenes = (self.state or {}).get("scenes", [])
        if not self.workspace or not scenes:
            return
        store = self.script.store
        options = MovieExportOptions(scenes, slide or self.scene_name, self.animation_height, bool(store and not store.is_empty), self.window)
        if slide:
            options.choose_current_slide()
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        path = self._export_filename("Export Movie", "mp4")
        if not path:
            return
        names = options.selected_scenes()
        request = {"path": path, "height": options.height, "hold": options.hold_seconds, "fps": options.frame_rate}
        if names is not None:
            request["scenes"] = names
        def perform():
            try:
                if not (yield from self._finish_project_edits()):
                    return
                if options.with_script:
                    result = self.export_scripted_movie(request, store=store, scenes=names or [s["name"] for s in scenes if not s.get("hidden")], to=Path(path))
                    if inspect.isgenerator(result):
                        yield from result
                    elif isinstance(result, Future):
                        yield result
                else:
                    yield from self._project_job("export_movie", request)
                    self.toast("Exported " + path)
            except Exception as exc:
                self.alert("Could not export movie", str(exc))
        spawn(perform())

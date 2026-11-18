"""Explicit interpreter selection and dependency diagnostics; never installs on launch."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from PySide6.QtCore import QDir, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                              QLabel, QMessageBox, QPlainTextEdit, QVBoxLayout)

from . import prefs
from .runtime import Runtime, install_hint
from .style import Theme
from .tasks import spawn
from .widgets import add_copy_button, field_label
from .workers import run_blocking


@dataclass
class DependencyStatus:
    python: str = ''
    version: str = ''
    version_ok: bool = False
    imports: tuple[str, ...] = ()
    tools: dict[str, str] | None = None
    tex_packages: tuple[str, ...] | None = None   # missing base formula files, if checked
    error: str = ''

    @property
    def invalid(self):
        return not self.version_ok or bool(self.imports)

    def report(self):
        if self.error:
            return self.error
        lines = [f'Python: {self.python} ({self.version})']
        if not self.version_ok:
            lines.append('Choose Python 3.11–3.14.')
        lines.extend('Missing Python import: ' + module for module in self.imports)
        lines.extend(f'Missing {tool}: {why}' for tool, why in (self.tools or {}).items())
        lines.extend('Missing LaTeX package file: ' + name for name in self.tex_packages or ())
        if not self.invalid and not self.tools and not self.tex_packages:
            lines.append('All checked dependencies are available.')
        return '\n'.join(lines)


def _missing_tex_packages(runtime):
    """Check the same formula package files as the Mac setup script, without running TeX."""
    env = runtime.child_env()
    kpse = shutil.which('kpsewhich', path=env['PATH'])
    if not kpse:
        return None
    files = ('standalone.cls', 'preview.sty', 'english.ldf', 'dsfont.sty', 'relsize.sty')
    try:
        result = subprocess.run([kpse, *files], env=env, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode < 0 or result.stderr.strip():
        return None
    found = {Path(line).name for line in result.stdout.splitlines() if line}
    return tuple(name for name in files if name not in found)


def dependency_report(runtime, *, check_tex_packages=False):
    ok, version = runtime.version_ok()
    imports = runtime.missing_imports()
    tools = runtime.missing_tools()
    packages = (_missing_tex_packages(runtime)
                if check_tex_packages and not {'latex', 'dvisvgm'}.intersection(tools) else None)
    status = DependencyStatus(runtime.python, version, ok, tuple(imports), tools, packages)
    return status, status.invalid, tools


class Setup(QDialog):
    def __init__(self, controller):
        super().__init__(controller.window)
        self.setWindowTitle('Set Up Manimble')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.heading = QLabel()
        self.heading.setFont(Theme.font(15, QFont.Weight.Bold))
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        body = field_label(
            'Manimble draws slides with Manim, which runs in Python and uses LaTeX for formulas. '
            'Nothing is installed automatically.')
        body.setFont(Theme.font(13))
        body.setWordWrap(True)
        layout.addWidget(body)
        layout.addSpacing(6)

        self.python_row = self._row(layout, 'Python with Manim')
        self.tex_row = self._row(layout, 'LaTeX')
        self.tools_row = self._row(layout, 'Other tools')

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(Theme.mono(10))
        self.log.setFixedHeight(140)
        self.log.setAccessibleName('Dependency diagnostics')
        layout.addWidget(self.log)
        buttons = QDialogButtonBox()
        self.choose = buttons.addButton('Use Another Python…', QDialogButtonBox.ButtonRole.ActionRole)
        self.choose.clicked.connect(controller.choose_python)
        self.dismiss = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.dismiss.setDefault(True)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _row(layout, name):
        row = QVBoxLayout()
        row.setSpacing(2)
        box = QCheckBox(name)
        box.setFont(Theme.font(13))
        # These are status indicators, not a selection of items to install.
        box.setEnabled(False)
        row.addWidget(box)
        detail = field_label('')
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.setWordWrap(True)
        detail.setContentsMargins(20, 0, 0, 0)
        row.addWidget(detail)
        layout.addLayout(row)
        return box, detail

    @staticmethod
    def _show_status(row, found, detail):
        box, label = row
        box.setChecked(found)
        box.setAccessibleDescription(detail)
        label.setText(detail)

    def set_status(self, status):
        tools = status.tools
        packages = status.tex_packages
        needs_setup = status.invalid or bool(tools) or bool(packages)
        if needs_setup:
            heading = 'Manimble needs a few more things'
        elif packages is None:
            heading = 'All checked dependencies are available.'
        else:
            heading = 'Everything Manimble needs is installed'
        self.heading.setText(heading)
        self._show_status(
            self.python_row, not status.invalid,
            'Choose a Python environment with Manim and the engine requirements.'
            if status.invalid else f'Found: {status.python}')
        tex = [tool for tool in ('latex', 'dvisvgm') if tools is not None and tool in tools]
        self.tex_row[0].setText('LaTeX packages' if packages else 'LaTeX')
        if tools is None:
            self._show_status(self.tex_row, False, 'Not checked')
        elif tex or packages:
            self._show_status(self.tex_row, False, 'Missing: ' + ', '.join(tex or packages))
        else:
            found_tex = ('latex and dvisvgm; LaTeX packages were not checked' if packages is None
                         else 'a LaTeX installation with the packages Manim uses')
            self._show_status(self.tex_row, True, 'Found: ' + found_tex)
        other = [f'{tool} ({why})' for tool, why in (tools or {}).items() if tool not in tex]
        self._show_status(self.tools_row, tools is not None and not other,
                          'Not checked' if tools is None else
                          'Missing: ' + ', '.join(other) if other else 'All other checked tools are available.')

        if needs_setup:
            report = status.report()
            if tools or packages or tools is None:
                report += '\n\n' + install_hint()
            self.log.setPlainText(report)
        self.log.setVisible(needs_setup)
        self.choose.setVisible(status.invalid)
        self.dismiss.setText('Not Now' if (tools or packages) and not status.invalid else 'Close')
        self.layout().activate()
        self.resize(560, self.layout().totalHeightForWidth(560))


class SetupMixin:
    def choose_python(self):
        start = self.runtime.bin_dir if self.runtime else ''
        title = 'Choose the Python executable of the environment where manim is installed (e.g. …/venv/bin/python)'
        picker = QFileDialog(self.window, title, start, 'All files (*)')
        picker.setFileMode(QFileDialog.FileMode.ExistingFile)
        picker.setFilter(picker.filter() | QDir.Filter.Hidden)
        if not picker.exec():
            return
        path = picker.selectedFiles()[0]
        candidate = Runtime(path, 'saved', self.paths)
        def choose():
            status, invalid, _ = yield run_blocking(dependency_report, candidate)
            if invalid:
                box = QMessageBox(QMessageBox.Icon.Warning, 'Cannot use this Python', 'Cannot use this Python',
                                  QMessageBox.StandardButton.Ok, self.window)
                box.setTextFormat(Qt.TextFormat.PlainText)
                box.setInformativeText(status.report())
                box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                add_copy_button(box)
                box.exec()
                return
            if getattr(self, '_project_active', lambda: False)():
                self.alert('Cannot change Python yet', 'Wait for the current import or export to finish, or cancel it first.')
                return
            setup = getattr(self, 'setup', None)
            if setup is not None:
                setup.close()
            prefs.set('python', path)
            self.runtime = candidate
            if self.rpc is not None:
                yield self.rpc.stop_async()
            self.launch()
            if self.workspace:
                yield from self.reopen_working_copy()
        spawn(choose())

    def install_dependencies(self):
        def check():
            self.busy_pill.show('Checking for Python and LaTeX…', spinning=True, cancellable=False)
            try:
                if self.runtime is None:
                    runtime = Runtime.discover(self.paths, self.settings)
                else:
                    runtime = self.runtime
                status, _, _ = yield run_blocking(dependency_report, runtime, check_tex_packages=True)
            except Exception as exc:
                status = DependencyStatus(error=str(exc))
            finally:
                self.update_busy_pill()
            self.show_setup(status)
        spawn(check())

    def show_setup(self, status):
        if isinstance(status, str):
            status = DependencyStatus(error=status)
        setup = getattr(self, 'setup', None)
        if setup is None:
            self.setup = setup = Setup(self)
        setup.set_status(status)
        setup.show()
        setup.raise_()
        setup.activateWindow()

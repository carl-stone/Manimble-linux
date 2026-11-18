"""Explicit interpreter selection and dependency diagnostics; never installs on launch."""
from __future__ import annotations
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QLabel, QPushButton, QTextEdit, QVBoxLayout
from .runtime import Runtime, install_hint
from .workers import run_blocking
from .tasks import spawn
from . import prefs


class Setup(QDialog):
    def __init__(self, controller, report):
        super().__init__(controller.window)
        self.setWindowTitle('Manimble Dependencies')
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Missing dependencies affect the features listed below. Nothing is installed automatically.'))
        log = QTextEdit()
        log.setReadOnly(True)
        log.setPlainText(report + '\n\n' + install_hint())
        layout.addWidget(log)
        choose = QPushButton('Choose Python…')
        choose.clicked.connect(controller.choose_python)
        layout.addWidget(choose)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)


def dependency_report(runtime):
    ok, version = runtime.version_ok()
    imports = runtime.missing_imports()
    tools = runtime.missing_tools()
    lines = [f'Python: {runtime.python} ({version})']
    if not ok:
        lines.append('Choose Python 3.11–3.14.')
    lines += ['Missing Python import: ' + module for module in imports]
    lines += [f'Missing {tool}: {why}' for tool, why in tools.items()]
    if not imports and not tools and ok:
        lines.append('All checked dependencies are available.')
    return '\n'.join(lines), bool(imports) or not ok, tools


class SetupMixin:
    def choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self.window, 'Choose Python executable', self.runtime.bin_dir if self.runtime else '', 'All files (*)')
        if not path:
            return
        candidate = Runtime(path, 'saved', self.paths)
        def choose():
            report, invalid, _ = yield run_blocking(dependency_report, candidate)
            if invalid:
                self.alert('Cannot use this Python', report)
                return
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
            if self.runtime is None:
                report = install_hint()
            else:
                report, _, _ = yield run_blocking(dependency_report, self.runtime)
            self.show_setup(report)
        spawn(check())

    def show_setup(self, report):
        self.setup = Setup(self, report)
        self.setup.show()

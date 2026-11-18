"""The Python that runs the engine, and the environment child processes get.

Precedence: an explicit ``MANIMBLE_PYTHON``, then the interpreter saved in the settings (Choose Python…), then the
installed private venv. Run from a source checkout, the checkout's ``.venv`` and then the interpreter running this
front end take the private venv's place. A choice that was made explicitly and is not usable is an error to show,
never silently replaced by another interpreter.

A venv's ``bin/python`` is a symlink to the base interpreter; it is run by its own path, never the resolved one, or
the venv's site-packages would be lost.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths as _paths
from .paths import AppPaths

SUPPORTED = ((3, 11), (3, 14))         # inclusive
SETTINGS_KEY = "python"

# tools the engine runs, with what each is needed for
NATIVE_TOOLS = {
    "ffmpeg": "movie export and animations",
    "latex": "formulas (TeX Live)",
    "dvisvgm": "formulas (TeX Live)",
    "fc-match": "font matching (Fontconfig)",
    "pdftoppm": "PDF import and pictures (Poppler)",
    "gs": "PostScript pictures (Ghostscript)",
    "espeak-ng": "native narration voices",
}
ENGINE_IMPORTS = ("manim", "PIL", "av", "matplotlib", "pptx", "manim_slides")


class RuntimeError_(Exception):
    """An interpreter that was chosen explicitly cannot be used; the message says what to do."""


def _abs(p: str | os.PathLike) -> str:
    return os.path.abspath(os.path.expanduser(os.fspath(p)))


def _executable(p: str) -> bool:
    return os.path.isfile(p) and os.access(p, os.X_OK)


def dedupe(items) -> list[str]:
    out: list[str] = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


@dataclass
class Runtime:
    python: str                         # absolute path, symlinks kept
    source: str                         # "env", "saved", "private", "checkout", "self"
    paths: AppPaths
    engine: Path = field(default_factory=_paths.engine_dir)

    @classmethod
    def discover(cls, paths: AppPaths, settings=None, env: dict[str, str] | None = None) -> "Runtime":
        """The interpreter to run the engine with. Raises RuntimeError_ for an unusable explicit choice."""
        env = os.environ if env is None else env
        explicit = env.get("MANIMBLE_PYTHON")
        if explicit:
            p = _abs(explicit)
            if not _executable(p):
                raise RuntimeError_(f"MANIMBLE_PYTHON is set to {explicit}, which is not an executable Python. "
                                    "Unset it or point it at a Python that has Manimble's engine requirements.")
            return cls(p, "env", paths)
        saved = settings.value(SETTINGS_KEY, "") if settings is not None else ""
        if saved:
            p = _abs(str(saved))
            if not _executable(p):
                raise RuntimeError_(f"The Python chosen for Manimble, {saved}, no longer exists or cannot be run. "
                                    "Choose another one with Manimble ▸ Choose Python…")
            return cls(p, "saved", paths)
        checkout = _paths.checkout_root()
        if checkout is not None:
            venv = checkout / ".venv" / "bin" / "python"
            if _executable(str(venv)):
                return cls(str(venv), "checkout", paths)
            return cls(sys.executable, "self", paths)
        private = paths.private_python
        if _executable(str(private)):
            return cls(str(private), "private", paths)
        raise RuntimeError_(f"Manimble's Python environment is missing ({private}). "
                            "Run install_manimble.sh again, or choose a Python with Manimble ▸ Choose Python…")

    # ----------------------------------------------------------------- environment

    @property
    def bin_dir(self) -> str:
        return os.path.dirname(self.python)

    def search_path(self, base: str | None = None) -> str:
        """The chosen Python's bin first, then the user's and the system's usual folders, then the inherited PATH."""
        base = os.environ.get("PATH", "") if base is None else base
        home = str(self.paths.home)
        lead = [self.bin_dir, os.path.join(home, ".local", "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
        return ":".join(dedupe(lead + base.split(":")))

    def child_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for every process the app starts (engine, terminal, plots, workers)."""
        env = dict(os.environ if base is None else base)
        env["PATH"] = self.search_path(env.get("PATH", ""))
        env["QT_API"] = "pyside6"
        for k in ("PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            env.pop(k, None)
        env["MANIMBLE_PYTHON"] = self.python
        return env

    def engine_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = self.child_env(base)
        env["PYTHONPATH"] = str(self.engine)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def engine_command(self) -> list[str]:
        return [self.python, "-u", "-m", "manimble.rpc"]

    def manimbler_command(self, *args: str) -> list[str]:
        return [self.python, str(_paths.manimbler_path()), *args]

    # ----------------------------------------------------------------- diagnostics (blocking: run off the GUI thread)

    def missing_imports(self, modules=ENGINE_IMPORTS, timeout: float = 60) -> list[str]:
        """Modules the interpreter cannot import ([] when all import)."""
        code = ("import importlib.util,sys\n"
                "print('\\n'.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))")
        try:
            r = subprocess.run([self.python, "-c", code, *modules], capture_output=True, text=True, timeout=timeout,
                               env=self.child_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"(could not run {self.python}: {exc})"]
        if r.returncode != 0:
            return [f"(could not run {self.python}: {r.stderr.strip()[-300:]})"]
        return [m for m in r.stdout.split() if m]

    def missing_tools(self, tools=None) -> dict[str, str]:
        """Native tools not on the child PATH, with what each is needed for."""
        path = self.search_path()
        wanted = NATIVE_TOOLS if tools is None else {t: NATIVE_TOOLS.get(t, "") for t in tools}
        return {t: why for t, why in wanted.items() if shutil.which(t, path=path) is None}

    def version_ok(self) -> tuple[bool, str]:
        try:
            r = subprocess.run([self.python, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        v = r.stdout.strip()
        try:
            major, minor = (int(x) for x in v.split(".")[:2])
        except ValueError:
            return False, v or r.stderr.strip()
        return SUPPORTED[0] <= (major, minor) <= SUPPORTED[1], v


def install_hint() -> str:
    """How to get the native dependencies on this system."""
    root = _paths.app_root()
    script = root / "install_manimble.sh"
    if not script.exists():
        script = root / "apps" / "linux" / "install.sh"
    return f"Run {script} --system-deps to install the missing system packages."

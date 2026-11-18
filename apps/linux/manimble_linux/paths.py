"""Where the Linux front end keeps its files: XDG base directories, never the current directory.

Everything is resolved once, at start, into absolute paths. A relative or empty XDG variable is ignored, as the XDG
Base Directory specification requires, and the standard default under ``$HOME`` is used instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP = "manimble"
PACKAGE_DIR = Path(__file__).resolve().parent          # apps/linux/manimble_linux
LINUX_DIR = PACKAGE_DIR.parent                         # apps/linux
RESOURCES = LINUX_DIR / "resources"


def _xdg(name: str, default: str, env: dict[str, str]) -> Path:
    value = env.get(name, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(env.get("HOME") or os.path.expanduser("~")) / default


@dataclass(frozen=True)
class AppPaths:
    config: Path        # $XDG_CONFIG_HOME/manimble: settings.ini (no secrets)
    data: Path          # $XDG_DATA_HOME/manimble: app payload, private venvs
    cache: Path         # $XDG_CACHE_HOME/manimble: render caches, voices, model checkpoint, downloads
    state: Path         # $XDG_STATE_HOME/manimble: working copies and logs
    home: Path

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "AppPaths":
        env = dict(os.environ if env is None else env)
        home = Path(env.get("HOME") or os.path.expanduser("~"))
        return cls(
            config=_xdg("XDG_CONFIG_HOME", ".config", env) / APP,
            data=_xdg("XDG_DATA_HOME", ".local/share", env) / APP,
            cache=_xdg("XDG_CACHE_HOME", ".cache", env) / APP,
            state=_xdg("XDG_STATE_HOME", ".local/state", env) / APP,
            home=home,
        )

    # files and folders under the base directories
    @property
    def settings_file(self) -> Path:
        return self.config / "settings.ini"

    @property
    def payload(self) -> Path:
        """The installed copy of the app: VERSION, LICENSE, manimbler, engine/, apps/linux/."""
        return self.data / "app"

    @property
    def private_python(self) -> Path:
        return self.data / "python" / "bin" / "python"

    @property
    def extract_python(self) -> Path:
        return self.data / "extract" / "bin" / "python"

    @property
    def work(self) -> Path:
        """Working copies of open decks (Workspace)."""
        return self.state / "work"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def engine_log(self) -> Path:
        return self.logs / "engine.log"

    @property
    def render_cache(self) -> Path:
        return self.cache / "cache"

    @property
    def voice_cache(self) -> Path:
        return self.cache / "voice"

    @property
    def models(self) -> Path:
        return self.cache / "models"

    @property
    def downloads(self) -> Path:
        return self.cache / "downloads"

    def ensure(self) -> None:
        for d in (self.config, self.cache, self.state, self.work, self.logs):
            d.mkdir(parents=True, exist_ok=True)


def checkout_root() -> Path | None:
    """The source checkout this package runs from (repository root), or None for an installed payload."""
    root = LINUX_DIR.parent.parent
    if (root / ".git").exists() and (root / "engine" / "manimble").is_dir():
        return root
    return None


def app_root() -> Path:
    """The folder holding ``engine/``, ``manimbler`` and ``VERSION``: the checkout or the installed payload."""
    return LINUX_DIR.parent.parent


def engine_dir() -> Path:
    env = os.environ.get("MANIMBLE_ENGINE")
    if env and os.path.isabs(env):
        return Path(env)
    return app_root() / "engine"


def manimbler_path() -> Path:
    return app_root() / "manimbler"


def version() -> str:
    try:
        return (app_root() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"

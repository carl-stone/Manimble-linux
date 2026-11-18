"""Private Fontconfig policy and Qt face selection for the five bundled deck families."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from tempfile import mkstemp
from xml.sax.saxutils import escape

from . import paths as _paths

_THEME_FAMILIES = ("Helvetica Neue", "Avenir Next", "Chalkboard SE", "Palatino", "Menlo")
_LOCAL_FACES = (("Comic Neue", "comicneue"), ("Nunito Sans", "nunitosans"))
_SYSTEM_CONFIG_DIR = Path("/etc/fonts")
_WRAPPER_PREFIX = "manimble-fonts-"
_WRAPPER_HEADER = '<?xml version="1.0"?>\n<!-- Manimble private Fontconfig wrapper -->\n'
_display_families: dict[str, str] | None = None


def _base_config(env: dict[str, str]) -> Path:
    name = env.get("FONTCONFIG_FILE") or "fonts.conf"
    if os.path.isabs(name):
        return Path(name).resolve()
    if name.startswith("~"):
        return (Path(env.get("HOME") or Path.home()) / name[1:].lstrip("/")).resolve()
    # Fontconfig searches each FONTCONFIG_PATH entry, then its system config directory.
    search = env["FONTCONFIG_PATH"].split(os.pathsep) if env.get("FONTCONFIG_PATH") else ()
    for directory in (*search, _SYSTEM_CONFIG_DIR):
        candidate = Path(directory) / name
        if os.access(candidate, os.R_OK):
            return candidate.resolve()
    # Keep a missing explicit config visible to Fontconfig rather than swapping in fonts.conf.
    return (_SYSTEM_CONFIG_DIR / name).resolve()


def deck_font_env(paths: _paths.AppPaths, base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy the child environment and compose the app's font policy with its inherited config."""
    env = dict(os.environ if base is None else base)
    if sys.platform != "linux":
        return env
    if search := env.get("FONTCONFIG_PATH"):
        # Render workers change directory to the deck. Keep relative search roots tied to the launch directory.
        env["FONTCONFIG_PATH"] = os.pathsep.join(
            str(Path(directory or "/").resolve()) for directory in search.removesuffix(os.pathsep).split(os.pathsep)
        )
    inherited = _base_config(env)
    if inherited.name.startswith(_WRAPPER_PREFIX) and inherited.is_file():
        with inherited.open("rb") as config:
            header = _WRAPPER_HEADER.encode()
            if config.read(len(header)) == header:
                env["FONTCONFIG_FILE"] = str(inherited)
                return env
    fonts = (_paths.RESOURCES / "fonts").resolve()
    body = (_WRAPPER_HEADER + '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
            '<fontconfig>\n'
            f'  <include>{escape(str(inherited))}</include>\n'
            f'  <dir>{escape(str(fonts))}</dir>\n'
            f'  <include>{escape(str(fonts / "theme-aliases.conf"))}</include>\n'
            '</fontconfig>\n').encode()
    # Different inherited configs or install roots can share a cache without overwriting one another.
    target = paths.cache.resolve() / "fontconfig" / f"{_WRAPPER_PREFIX}{hashlib.sha256(body).hexdigest()}.conf"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = mkstemp(dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as config:
                config.write(body)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    env["FONTCONFIG_FILE"] = str(target)
    return env


def setup_deck_fonts(paths: _paths.AppPaths) -> None:
    """Register missing bundled faces and resolve the deck aliases once on the Qt GUI thread."""
    global _display_families
    if _display_families is not None:
        return
    from PySide6.QtGui import QFontDatabase
    from manimble.importer import resolve_font_family

    env = deck_font_env(paths)
    installed = {name.casefold() for name in QFontDatabase.families()}
    for family, subdir in _LOCAL_FACES:
        if family.casefold() not in installed:
            for font in sorted((_paths.RESOURCES / "fonts" / subdir).glob("*.ttf")):
                QFontDatabase.addApplicationFont(str(font))
    _display_families = {family.casefold(): resolve_font_family(family, env=env) for family in _THEME_FAMILIES}


def display_family(requested: str) -> str:
    """The face Qt should draw, without changing the authored family name."""
    if _display_families is None:
        return requested
    return _display_families.get(requested.casefold(), requested)

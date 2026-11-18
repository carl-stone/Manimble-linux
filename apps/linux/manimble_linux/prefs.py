"""Nonsecret preferences: QSettings in IniFormat at ``$XDG_CONFIG_HOME/manimble/settings.ini``.

Keys are the macOS app's UserDefaults keys wherever the behaviour is shared (``showGrid``, ``snapObjects``,
``penColor``, ``pageNumberSpread``, ``terminal.agent``...), so the meaning of each stays documented in one place.
QSettings stores everything as strings in INI files; these helpers read typed values back.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QSettings

from .paths import AppPaths

_settings: QSettings | None = None


def open_settings(paths: AppPaths) -> QSettings:
    """The one QSettings for the app (created on first use)."""
    global _settings
    if _settings is None:
        paths.config.mkdir(parents=True, exist_ok=True)
        _settings = QSettings(str(paths.settings_file), QSettings.Format.IniFormat)
    return _settings


def settings() -> QSettings:
    if _settings is None:
        return open_settings(AppPaths.from_environment())
    return _settings


def get_bool(key: str, default: bool = False) -> bool:
    v = settings().value(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_str(key: str, default: str = "") -> str:
    v = settings().value(key)
    return default if v is None else str(v)


def get_float(key: str, default: float = 0.0) -> float:
    try:
        v = settings().value(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def get_int(key: str, default: int = 0) -> int:
    try:
        v = settings().value(key)
        return default if v is None else int(float(v))
    except (TypeError, ValueError):
        return default


def get_json(key: str, default: Any = None) -> Any:
    v = settings().value(key)
    if v is None:
        return default
    try:
        return json.loads(str(v))
    except ValueError:
        return default


def has(key: str) -> bool:
    return settings().contains(key)


def set(key: str, value: Any) -> None:            # noqa: A001  (mirrors UserDefaults.set)
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    elif isinstance(value, bool):
        value = "true" if value else "false"
    settings().setValue(key, value)


def remove(key: str) -> None:
    settings().remove(key)

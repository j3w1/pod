"""Where Pod keeps its files. An explicit POD_*_HOME wins; otherwise the XDG location."""

import os
from pathlib import Path

from .errors import PodError


def _home(override: str, xdg: str, default: str) -> Path:
    value = os.environ.get(override)
    if value is not None:
        path = Path(value)
        if not value or not path.is_absolute():
            raise PodError("invalid_location", f"{override} must be an absolute directory path",
                           fix=f"set {override} to an absolute path or unset it")
        return path
    base = os.environ.get(xdg)
    return (Path(base) if base and Path(base).is_absolute() else Path.home() / default) / "pod"


def config_home() -> Path:
    return _home("POD_CONFIG_HOME", "XDG_CONFIG_HOME", ".config")


def state_home() -> Path:
    return _home("POD_STATE_HOME", "XDG_STATE_HOME", ".local/state")


def cache_home() -> Path:
    return _home("POD_CACHE_HOME", "XDG_CACHE_HOME", ".cache")


def route_file() -> Path:
    """The personal preference file: the only authority for routes, Preferred and Pin."""
    return config_home() / "config.yaml"


def objectives_home() -> Path:
    """Objective logs. Records written by 0.21 live beside this directory and are never read here."""
    return state_home() / "objectives"

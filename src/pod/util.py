"""Small bounded JSON and identity primitives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .errors import PodError

MAX_RECORD = 128 * 1024


def explicit_home(name: str) -> Path | None:
    """Return one absolute, process-scoped Pod home override when present."""
    if name not in os.environ:
        return None
    value = os.environ[name]
    if not value or len(value) > 4096 or "\x00" in value:
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path")
    try:
        path = Path(value)
    except (OSError, ValueError) as exc:
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path") from exc
    if not path.is_absolute():
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path")
    if path.exists() and not path.is_dir():
        raise PodError("invalid_location_override", f"{name} must identify a directory")
    return path


def native_home(name: str, *, default: Path | None = None) -> Path:
    """Resolve a native profile home without allowing cwd-relative authority."""
    if name not in os.environ:
        if default is None:
            raise PodError("native_home_unavailable", f"{name} is required")
        path = default
    else:
        value = os.environ[name]
        if not value or len(value) > 4096 or "\x00" in value:
            raise PodError("invalid_native_home", f"{name} must be an absolute directory path")
        try:
            path = Path(value)
        except (OSError, ValueError) as exc:
            raise PodError("invalid_native_home", f"{name} must be an absolute directory path") from exc
    if not path.is_absolute() or (path.exists() and not path.is_dir()):
        raise PodError("invalid_native_home", f"{name} must be an absolute directory path")
    return path


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def bounded_json(path: Path, *, limit: int = MAX_RECORD) -> Any:
    cursor = path.parent
    while cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise PodError("unsafe_record", "Record parent is redirected")
        cursor = cursor.parent
    if path.is_symlink() or not path.is_file():
        raise PodError("unsafe_record", "Record must be a regular, non-symlink file")
    if path.stat().st_size > limit:
        raise PodError("record_too_large", "Record exceeds its size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded JSON record") from exc


def atomic_json(path: Path, value: Any) -> None:
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"
    if len(data) > MAX_RECORD:
        raise PodError("record_too_large", "Record exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise PodError("unsafe_record", "Record parent is a symlink")
    fd, name = tempfile.mkstemp(prefix=".pod-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def exact(value: Any, fields: set[str], required: set[str] | None = None, *, name: str = "record") -> dict:
    if not isinstance(value, dict) or set(value) - fields or (required or set()) - set(value):
        raise PodError("invalid_" + name, f"{name} has missing or unsupported fields")
    return value


def bounded_text(value: Any, *, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise PodError("invalid_" + name, f"{name} must be bounded, nonempty text")
    return value

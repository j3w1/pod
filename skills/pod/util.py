"""Small bounded JSON and identity primitives."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
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


def _inside(candidate: Path, project: Path) -> bool:
    """Return whether a path is lexically or physically inside a project."""
    try:
        lexical_candidate = Path(os.path.abspath(candidate))
        lexical_project = Path(os.path.abspath(project))
        physical_candidate = candidate.resolve(strict=False)
        physical_project = project.resolve(strict=False)
        return (lexical_candidate == lexical_project
                or lexical_candidate.is_relative_to(lexical_project)
                or physical_candidate == physical_project
                or physical_candidate.is_relative_to(physical_project))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PodError("invalid_native_home", "Native home containment cannot be proven") from exc


def native_home(name: str, *, default: Path | None = None,
                project: Path | None = None) -> Path:
    """Resolve a native profile home outside the current project boundary."""
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
    boundary = (project if project is not None else Path.cwd()).resolve()
    boundaries = []
    from .github import repository_context
    context = repository_context(boundary)
    if context.get("repo_key") is not None:
        boundaries = [Path(value) for value in context["linked_worktrees"]]
    if any(_inside(path, candidate) for candidate in boundaries):
        raise PodError("project_contained_native_home",
                       f"{name} must remain outside every linked project worktree")
    return path


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def bounded_json(path: Path, *, limit: int = MAX_RECORD) -> Any:
    cursor = path.parent
    while cursor != cursor.parent:
        if cursor.is_symlink():
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


def bounded_stdin_json(*, limit: int = MAX_RECORD) -> Any:
    """Read one private JSON request from a pipe without treating it as a path."""
    try:
        raw = sys.stdin.buffer.read(limit + 1)
    except OSError as exc:
        raise PodError("invalid_record", "Cannot read bounded JSON input") from exc
    if len(raw) > limit:
        raise PodError("record_too_large", "Record exceeds its size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded JSON record") from exc


def atomic_json(path: Path, value: Any, *, limit: int = MAX_RECORD) -> None:
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"
    if len(data) > limit:
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

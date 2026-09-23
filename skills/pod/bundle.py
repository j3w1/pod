"""The one inventory of the Pod skill bundle: skill text, helpers and package modules."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import re

import yaml

from .errors import PodError

BUNDLE_TEXT = ("SKILL.md", "VERSION", "agents/openai.yaml", "scripts/pod.py",
               "references/execution-spec.md", "references/planning.md", "references/routing.md",
               "references/orca-boundary.md", "references/verification.md",
               "references/governor.md")
BUNDLE_MODULES = ("__init__.py", "__main__.py", "bundle.py", "cli.py", "config.py", "context.py",
                  "errors.py", "github.py", "governor.py", "internal.py", "ledger.py", "operations.py",
                  "orca.py", "quota.py", "records.py", "routing.py", "setup.py",
                  "skill_validation.py", "util.py")
BUNDLE_FILES = tuple(sorted(BUNDLE_TEXT + BUNDLE_MODULES))
IGNORED_DIRS = frozenset({"__pycache__"})
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_SKILL = 64 * 1024


def bundle_root() -> Path:
    """The installed bundle directory: the importable package and the skill are one tree."""
    return Path(str(files("pod")))


def version() -> str:
    from . import __version__

    return __version__


def canonical(root: Path | None = None) -> dict[str, bytes]:
    base = bundle_root() if root is None else root
    source = {}
    for name in BUNDLE_FILES:
        path = base.joinpath(*name.split("/"))
        try:
            source[name] = path.read_bytes()
        except OSError as exc:
            raise PodError("bundle_incomplete", f"Bundle file {name} is unreadable") from exc
        if len(source[name]) > MAX_SKILL and name in BUNDLE_TEXT:
            raise PodError("invalid_skill", f"{name} is oversized")
    return source


def frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        raise PodError("invalid_skill", "SKILL.md frontmatter is missing")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise PodError("invalid_skill", "SKILL.md frontmatter is not bounded")
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise PodError("invalid_skill", "SKILL.md frontmatter is invalid YAML") from exc
    if not isinstance(metadata, dict):
        raise PodError("invalid_skill", "SKILL.md frontmatter is not a mapping")
    return {"metadata": metadata, "body": parts[2]}


def _ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return bool(IGNORED_DIRS.intersection(relative.parts)) or path.suffix in IGNORED_SUFFIXES


def installed_files(target: Path) -> set[str]:
    """Relative names of a placed copy, ignoring interpreter caches."""
    names = set()
    for path in target.rglob("*"):
        if _ignored(path, target):
            continue
        if path.is_symlink() or path.is_file():
            names.add(path.relative_to(target).as_posix())
    return names


def installed_version(target: Path) -> str | None:
    """The VERSION a placed copy carries, or None when unreadable."""
    try:
        text = (target / "VERSION").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return text if re.fullmatch(r"\d+\.\d+\.\d+", text) else None

"""The one inventory of the Pod skill bundle: skill text, helpers and package modules."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import re

import yaml

from .errors import PodError

BUNDLE_TEXT = ("SKILL.md", "VERSION", "catalog.json", "agents/openai.yaml", "scripts/pod.py",
               "references/execution-spec.md", "references/planning.md", "references/models.md",
               "references/orca-boundary.md", "references/verification.md",
               "references/governor.md")
BUNDLE_MODULES = ("__init__.py", "__main__.py", "bundle.py", "catalog.py", "cli.py", "config.py", "context.py",
                  "errors.py", "github.py", "governor.py", "installer.py", "internal.py", "ledger.py",
                  "obligations.py", "operations.py", "orca.py", "placement.py", "records.py", "selection.py",
                  "skill_validation.py", "term.py", "tui.py", "tui_render.py",
                  "tui_state.py", "util.py")
BUNDLE_FILES = tuple(sorted(BUNDLE_TEXT + BUNDLE_MODULES))
IGNORED_DIRS = frozenset({"__pycache__"})
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_SKILL = 64 * 1024
RELOAD_ACTION = "reload the skill, re-read pod config --json, and write a fresh checkpoint"


def bundle_root() -> Path:
    """The installed bundle directory: the importable package and the skill are one tree."""
    return Path(str(files("pod")))


def version() -> str:
    from . import __version__

    return __version__


def running_identity() -> dict[str, str | None]:
    """Hash the running bundle using the installer's canonical, cache-free digest."""
    from .installer import optional_digest
    return {"version": version(), "bundle_digest": optional_digest(bundle_root())}


def checkpoint_identity(checkpoint: object) -> dict[str, str | None]:
    row = checkpoint if isinstance(checkpoint, dict) else {}
    return {"version": row.get("pod_version"), "bundle_digest": row.get("bundle_digest")}


def identity_matches(recorded: dict, running: dict | None = None) -> bool:
    current = running_identity() if running is None else running
    return (isinstance(current.get("bundle_digest"), str)
            and isinstance(recorded.get("bundle_digest"), str)
            and recorded.get("version") == current.get("version")
            and recorded["bundle_digest"] == current["bundle_digest"])


def identity_label(identity: dict | None) -> str:
    row = identity if isinstance(identity, dict) else {}
    reported = row.get("version")
    ver = reported if isinstance(reported, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", reported) else "missing"
    value = row.get("bundle_digest")
    short = value[:12] if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else "missing"
    return f"version {ver}, digest {short}"


def identity_drift_message(checkpoint: object, *, running: dict | None = None) -> str:
    current = running_identity() if running is None else running
    return (f"Checkpoint {identity_label(checkpoint_identity(checkpoint))}; "
            f"running {identity_label(current)}. Next: {RELOAD_ACTION}")


def require_current_identity(checkpoint: object) -> dict[str, str | None]:
    current = running_identity()
    if not identity_matches(checkpoint_identity(checkpoint), current):
        raise PodError("installed_version_changed", identity_drift_message(checkpoint, running=current))
    return current


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

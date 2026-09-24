"""Read-only discovery of installed skill placements and skills-CLI ownership."""

from __future__ import annotations

import os
from pathlib import Path

from .bundle import installed_version
from .errors import PodError
from .util import bounded_json, native_home


def skills_cli_entry() -> dict:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    path = state / "skills" / ".skill-lock.json"
    result = {"path": str(path), "entry": None, "readable": False}
    if path.is_symlink() or not path.is_file():
        return result
    try:
        raw = bounded_json(path, limit=4 * 1024 * 1024)
    except PodError:
        return result
    result["readable"] = True
    skills = raw.get("skills") if isinstance(raw, dict) else None
    entry = skills.get("pod") if isinstance(skills, dict) else None
    if isinstance(entry, dict):
        result["entry"] = {key: entry[key] for key in ("source", "ref", "subpath", "installedAt", "updatedAt")
                           if isinstance(entry.get(key), str)}
    return result


def inspect(project: Path | None = None) -> dict:
    canonical = Path.home() / ".agents" / "skills" / "pod"
    codex_home = native_home("CODEX_HOME", default=Path.home() / ".agents", project=project)
    claude_home = native_home("CLAUDE_CONFIG_DIR", default=Path.home() / ".claude", project=project)
    paths = {"canonical": canonical, "codex_configured": codex_home / "skills" / "pod",
             "claude": claude_home / "skills" / "pod"}
    report = {}
    for name, path in paths.items():
        if path.is_symlink():
            target = path.resolve(strict=False)
            status = "linked_to_canonical" if target == canonical.resolve(strict=False) else "other_link"
        elif path.is_dir():
            status = "present"
        elif path.exists():
            status = "conflict"
        else:
            status = "missing"
        if name == "codex_configured" and path != canonical and status == "missing" and canonical.is_dir():
            status = "present_elsewhere"
        if name == "claude" and status == "missing" and canonical.is_dir():
            status = "missing_claude_link"
        report[name] = {"status": status, "path": str(path),
                        "version": installed_version(path) if path.is_dir() else None}
    return report

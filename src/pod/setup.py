"""Explicit, owned skill integration; no machine installer or PATH repair."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path, PurePath
import hashlib
import os
import yaml

from .errors import PodError
from .util import atomic_json, bounded_json, native_home

SKILL_FILES = ("SKILL.md", "references/planning.md", "references/routing.md",
               "references/native-effects.md", "references/verification.md")


def canonical() -> dict[str, bytes]:
    root = files("pod").joinpath("skill")
    source = {name: root.joinpath(*name.split("/")).read_bytes() for name in SKILL_FILES}
    frontmatter = source["SKILL.md"].decode("utf-8").split("---", 2)[1]
    metadata = yaml.safe_load(frontmatter)
    source["agents/openai.yaml"] = yaml.safe_dump(
        {"interface": {"display_name": "Pod", "short_description": metadata["description"][:100]}},
        sort_keys=False).encode()
    return source


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_key(path: PurePath, root: PurePath) -> str:
    """Canonical manifest/content key; filesystem operations keep native Paths."""
    return path.relative_to(root).as_posix()


def _targets(project: Path, global_scope: bool) -> dict[str, Path]:
    if global_scope:
        codex = native_home("CODEX_HOME", default=Path.home() / ".agents") / "skills" / "pod"
        claude = native_home("CLAUDE_CONFIG_DIR", default=Path.home() / ".claude") / "skills" / "pod"
    else:
        codex = project / ".agents" / "skills" / "pod"
        claude = project / ".claude" / "skills" / "pod"
    return {"codex": codex, "claude": claude}


def inspect(project: Path, *, global_scope: bool = False) -> dict:
    source = canonical()
    targets = {}
    for host, target in _targets(project, global_scope).items():
        if target.is_symlink():
            status = "redirected"
        elif not target.exists():
            status = "missing"
        elif not target.is_dir():
            status = "conflict"
        else:
            states = []
            actual = {_relative_key(p, target) for p in target.rglob("*") if p.is_file() or p.is_symlink()}
            if actual - set(source):
                states.append("extra")
            for name, data in source.items():
                path = target / name
                if path.is_symlink():
                    states.append("redirected")
                elif not path.is_file():
                    states.append("missing")
                elif path.read_bytes() != data:
                    states.append("modified")
            status = "current" if not states else "modified_or_incomplete"
        targets[host] = {"status": status, "path": str(target)}
    return targets


def _install(target: Path, source: dict[str, bytes]) -> str:
    cursor = target
    while cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            return "preserved_conflict"
        cursor = cursor.parent
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return "preserved_conflict"
    if target.exists():
        actual = {_relative_key(p, target) for p in target.rglob("*") if p.is_file() or p.is_symlink()}
        if actual - set(source):
            return "preserved_modified"
    existing = [target / name for name in source if (target / name).exists() or (target / name).is_symlink()]
    if any(path.is_symlink() or not path.is_file() or path.read_bytes() != source[_relative_key(path, target)] for path in existing):
        return "preserved_modified"
    for name, data in source.items():
        path = target / name
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise PodError("unsafe_skill_target", "Skill directory is redirected")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    return "reused" if len(existing) == len(source) else "installed"


def _unredirected(path: Path) -> bool:
    cursor = path
    while cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            return False
        cursor = cursor.parent
    return True


def setup(project: Path, *, global_scope: bool = False) -> dict:
    if not global_scope and (project.is_symlink() or (hasattr(project, "is_junction") and project.is_junction())):
        raise PodError("unsafe_project", "Project root is redirected")
    source = canonical()
    if global_scope:
        outcomes = {host: _install(path, source) for host, path in _targets(project, True).items()}
    else:
        local = _targets(project, False)
        global_state = inspect(project, global_scope=True)
        manifest_path = project / ".pod" / "skills.json"
        skeleton = {"schema": "pod-skills/v1", "files": {name: _digest(data) for name, data in source.items()},
                    "targets": {host: str(path.relative_to(project)) for host, path in local.items()}}
        prior = bounded_json(manifest_path) if manifest_path.exists() else None
        if prior is not None and any(prior.get(key) != value for key, value in skeleton.items()):
            raise PodError("modified_enrollment", "Existing local enrollment differs; preserve it for review")
        owned = set(prior.get("owned_hosts", [])) if prior else set()
        outcomes = {}
        for host, path in local.items():
            compatible_global = global_state[host]["status"] == "current"
            if compatible_global:
                if host in owned and _unredirected(path) and path.is_dir() and all((path / name).is_file() and not (path / name).is_symlink()
                                                           and (path / name).read_bytes() == data
                                                           for name, data in source.items()):
                    actual = {_relative_key(p, path) for p in path.rglob("*") if p.is_file() or p.is_symlink()}
                    if actual == set(source):
                        for name in source:
                            (path / name).unlink()
                        (path / "references").rmdir()
                        (path / "agents").rmdir()
                        path.rmdir()
                        owned.remove(host)
                        outcomes[host] = "reused_global_removed_owned_local"
                        continue
                outcomes[host] = "reused_global_preserved_local" if path.exists() else "reused_global"
                continue
            existed = path.exists() or path.is_symlink()
            outcomes[host] = _install(path, source)
            if outcomes[host] == "installed" and not existed:
                owned.add(host)
        manifest = {**skeleton, "owned_hosts": sorted(owned)}
        if prior != manifest:
            atomic_json(manifest_path, manifest)
    return {"scope": "global" if global_scope else "project", "skills": outcomes,
            "installation": "not_performed", "hooks": "not_run"}

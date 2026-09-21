"""Explicit, owned skill integration; no machine installer or PATH repair."""

from __future__ import annotations

from pathlib import Path, PurePath
import hashlib
import os

from .bundle import (BUNDLE_FILES, bundle_root, canonical, installed_files, installed_version,
                     version)
from .errors import PodError
from .util import atomic_json, bounded_json, explicit_home, native_home

SKILL_FILES = BUNDLE_FILES
LOCK_NAME = ".skill-lock.json"
MAX_LOCK = 4 * 1024 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_key(path: PurePath, root: PurePath) -> str:
    """Canonical manifest/content key; filesystem operations keep native Paths."""
    return path.relative_to(root).as_posix()


def _home(project: Path) -> dict[str, Path]:
    return {"codex": native_home("CODEX_HOME", default=Path.home() / ".agents", project=project),
            "claude": native_home("CLAUDE_CONFIG_DIR", default=Path.home() / ".claude", project=project)}


def _targets(project: Path, global_scope: bool) -> dict[str, Path]:
    if global_scope:
        return {host: root / "skills" / "pod" for host, root in _home(project).items()}
    return {"codex": project / ".agents" / "skills" / "pod",
            "claude": project / ".claude" / "skills" / "pod"}


def _roots(project: Path, global_scope: bool) -> dict[str, Path]:
    """The boundary each ancestor-redirection walk stops at.

    A native profile home may itself be a symlink on an ordinary machine; only
    components Pod would create below it must be unredirected.
    """
    if global_scope:
        return _home(project)
    return {"codex": project, "claude": project}


def skills_cli_lock_path() -> Path:
    """Where the community skills CLI keeps its global installation lock."""
    state = explicit_home("XDG_STATE_HOME")
    if state is not None:
        return state / "skills" / LOCK_NAME
    return Path.home() / ".agents" / LOCK_NAME


def skills_cli_entry() -> dict:
    """The skills-CLI lock entry for Pod, read passively and bounded."""
    path = skills_cli_lock_path()
    record = {"lock_file": str(path), "readable": False, "entry": None}
    if not path.is_file() or path.is_symlink():
        return record
    try:
        lock = bounded_json(path, limit=MAX_LOCK)
    except PodError:
        return record
    record["readable"] = True
    skills = lock.get("skills") if isinstance(lock, dict) else None
    entry = skills.get("pod") if isinstance(skills, dict) else None
    if isinstance(entry, dict):
        record["entry"] = {key: entry[key] for key in ("source", "ref", "subpath", "installedAt", "updatedAt")
                           if isinstance(entry.get(key), str)}
    return record


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _managed_by_skills_cli(host: str, target: Path, targets: dict[str, Path], *,
                           global_scope: bool, entry: dict | None) -> bool:
    """Whether the community skills CLI, not Pod, owns this copy.

    That installer places one canonical copy under the agents home and links every
    other agent at it. Either side of that shape identifies it, so a source without
    a lock entry is still recognised; a lock entry is additional proof.
    """
    resolved = _resolved(target)
    if resolved is None:
        return False
    for other, peer in targets.items():
        if other == host:
            continue
        if peer.is_symlink() and _resolved(peer) == resolved:
            # This target is the canonical copy another agent is linked at.
            return True
    if target.is_symlink():
        for other, peer in targets.items():
            if other != host and _resolved(peer) == resolved:
                return True
        canonical_target = _resolved(targets["codex"])
        if canonical_target is not None and resolved == canonical_target:
            return True
    if global_scope and entry is not None and target.is_dir():
        return True
    return False


def _redirected_below(path: Path, root: Path) -> bool:
    """Whether anything Pod would create below a profile root is redirected.

    The root itself may legitimately be a symlink on an ordinary machine, so the
    walk stops there rather than refusing the whole installation.
    """
    cursor = path
    boundary = Path(os.path.abspath(root))
    while True:
        if Path(os.path.abspath(cursor)) == boundary:
            return False
        if cursor.is_symlink():
            return True
        parent = cursor.parent
        if parent == cursor:
            return False
        cursor = parent


def _owned_hosts(project: Path) -> set[str]:
    manifest = project / ".pod" / "skills.json"
    if not manifest.is_file():
        return set()
    try:
        prior = bounded_json(manifest)
    except PodError:
        return set()
    owned = prior.get("owned_hosts") if isinstance(prior, dict) else None
    return {host for host in owned if isinstance(host, str)} if isinstance(owned, list) else set()


def inspect(project: Path, *, global_scope: bool = False) -> dict:
    source = canonical()
    targets = _targets(project, global_scope)
    lock = skills_cli_entry()
    owned = set() if global_scope else _owned_hosts(project)
    report = {}
    for host, target in targets.items():
        declared = installed_version(target) if target.is_dir() else None
        manager = None
        if _managed_by_skills_cli(host, target, targets, global_scope=global_scope, entry=lock["entry"]):
            status, manager = "managed_by_skills_cli", "skills_cli"
        elif target.is_symlink():
            manager = "unowned"
            status = "redirected"
        elif not target.exists():
            status = "missing"
        elif not target.is_dir():
            status, manager = "conflict", "unowned"
        else:
            manager = "pod_setup" if host in owned else "unowned"
            states = []
            if installed_files(target) - set(source):
                states.append("extra")
            for name, data in source.items():
                path = target / name
                if path.is_symlink():
                    states.append("redirected")
                elif not path.is_file():
                    states.append("missing")
                elif path.read_bytes() != data:
                    states.append("modified")
            if not states:
                status = "current"
            elif declared is not None and declared != version():
                status = "other_version"
            else:
                status = "modified_or_incomplete"
        report[host] = {"status": status, "path": str(target), "version": declared, "manager": manager}
    return report


def _write(target: Path, source: dict[str, bytes], names: list[str]) -> None:
    for name in names:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise PodError("unsafe_skill_target", "Skill directory is redirected")
        if path.exists() or path.is_symlink():
            path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(source[name])
        if name == "scripts/pod.py":
            os.chmod(path, 0o700)


def _install(target: Path, source: dict[str, bytes], root: Path) -> str:
    if _redirected_below(target, root):
        return "preserved_conflict"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return "preserved_conflict"
    if target.exists():
        if installed_files(target) - set(source):
            return "preserved_modified"
    existing = [target / name for name in source if (target / name).exists() or (target / name).is_symlink()]
    if any(path.is_symlink() or not path.is_file() or path.read_bytes() != source[_relative_key(path, target)]
           for path in existing):
        declared = installed_version(target)
        if declared is not None and declared != version():
            return "preserved_other_version"
        return "preserved_modified"
    missing = [name for name in source if not (target / name).exists()]
    if not missing:
        return "reused"
    _write(target, source, missing)
    return "installed"


def _prune(target: Path, names: set[str]) -> None:
    for name in sorted(names, reverse=True):
        path = target / name
        if path.is_file() and not path.is_symlink():
            path.unlink()
    for directory in sorted({(target / name).parent for name in names}, reverse=True):
        try:
            if directory != target and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def _upgrade(target: Path, source: dict[str, bytes], prior_files: dict, root: Path) -> str | None:
    """Replace a copy this project owns and nobody edited, after a version change."""
    if _redirected_below(target, root) or target.is_symlink() or not target.is_dir():
        return None
    actual = installed_files(target)
    if actual != set(prior_files):
        return None
    for name, expected in prior_files.items():
        path = target / name
        if path.is_symlink() or not path.is_file() or _digest(path.read_bytes()) != expected:
            return None
    _prune(target, actual - set(source))
    _write(target, source, list(source))
    return "upgraded"


def _remove_owned(target: Path, source: dict[str, bytes]) -> bool:
    if installed_files(target) != set(source):
        return False
    for name, data in source.items():
        path = target / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            return False
    _prune(target, set(source))
    try:
        target.rmdir()
    except OSError:
        return False
    return True


def setup(project: Path, *, global_scope: bool = False) -> dict:
    if not global_scope and project.is_symlink():
        raise PodError("unsafe_project", "Project root is redirected")
    source = canonical()
    targets = _targets(project, global_scope)
    roots = _roots(project, global_scope)
    lock = skills_cli_entry()
    ownership = {}
    if global_scope:
        outcomes = {}
        for host, path in targets.items():
            if _managed_by_skills_cli(host, path, targets, global_scope=True, entry=lock["entry"]):
                outcomes[host] = "managed_by_skills_cli"
                ownership[host] = "skills_cli"
                continue
            outcomes[host] = _install(path, source, roots[host])
            ownership[host] = "pod_setup" if outcomes[host] in ("installed", "reused") else "unowned"
    else:
        local = targets
        global_state = inspect(project, global_scope=True)
        manifest_path = project / ".pod" / "skills.json"
        skeleton = {"schema": "pod-skills/v1", "version": version(),
                    "files": {name: _digest(data) for name, data in source.items()},
                    "targets": {host: str(path.relative_to(project)) for host, path in local.items()}}
        prior = bounded_json(manifest_path) if manifest_path.exists() else None
        if prior is not None and any(prior.get(key) != skeleton[key] for key in ("schema", "targets")):
            raise PodError("modified_enrollment", "Existing local enrollment differs; preserve it for review")
        prior_files = prior.get("files") if isinstance(prior, dict) else None
        owned = set(prior.get("owned_hosts", [])) if prior else set()
        outcomes = {}
        for host, path in local.items():
            if _managed_by_skills_cli(host, path, local, global_scope=False, entry=None):
                outcomes[host] = "managed_by_skills_cli"
                ownership[host] = "skills_cli"
                continue
            if global_state[host]["status"] in ("current", "managed_by_skills_cli"):
                if host in owned and not _redirected_below(path, roots[host]) and path.is_dir():
                    if _remove_owned(path, source):
                        owned.discard(host)
                        outcomes[host] = "reused_global_removed_owned_local"
                        ownership[host] = global_state[host]["manager"] or "pod_setup"
                        continue
                outcomes[host] = "reused_global_preserved_local" if path.exists() else "reused_global"
                ownership[host] = global_state[host]["manager"] or "pod_setup"
                continue
            existed = path.exists() or path.is_symlink()
            outcome = None
            if (existed and host in owned and isinstance(prior_files, dict)
                    and prior_files != skeleton["files"]):
                outcome = _upgrade(path, source, prior_files, roots[host])
            outcomes[host] = outcome if outcome else _install(path, source, roots[host])
            if outcomes[host] in ("installed", "upgraded") and (not existed or host in owned):
                owned.add(host)
            ownership[host] = "pod_setup" if host in owned else "unowned"
        manifest = {**skeleton, "owned_hosts": sorted(owned)}
        if prior != manifest:
            atomic_json(manifest_path, manifest)
    result = {"scope": "global" if global_scope else "project", "skills": outcomes,
              "ownership": ownership, "bundle": {"version": version(), "path": str(bundle_root())},
              "installation": "not_performed", "hooks": "not_run"}
    return result

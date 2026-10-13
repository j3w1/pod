"""Read-only list of the objectives recorded for one repository or worktree (R108).

Membership is proved by the state key, never guessed from paths. The list reads only Pod's own
state directory and Git identity: no Orca, preference or provider read, so it also works outside
an Orca terminal. It never writes, converts or locks a record.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import PodError
from .util import bounded_json

MAX_SCAN = 1024
MAX_ROWS = 128
MAX_DIAGNOSTICS = 32


def _location(selector: object) -> str | None:
    """The absolute path an Orca placement selector or worktree id names, else None."""
    if not isinstance(selector, str):
        return None
    value = selector.removeprefix("path:")
    if value.startswith("id:"):
        value = value[3:]
    if "::" in value:
        value = value.split("::", 1)[1]
    return value if value.startswith("/") else None


def _name(raw: dict) -> str | None:
    checkpoint = raw.get("checkpoint")
    name = checkpoint.get("objective") if isinstance(checkpoint, dict) else None
    if isinstance(name, str) and name:
        return name
    admissions = raw.get("admissions")
    names = {row.get("objective") for row in admissions.values() if isinstance(row, dict)} \
        if isinstance(admissions, dict) else set()
    return next(iter(names)) if len(names) == 1 and isinstance(next(iter(names)), str) else None


def _facts(raw: dict) -> dict:
    """Recorded locations and Runs, read from the documented fields only."""
    checkpoint = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else {}
    worktrees, runs, complete = set(), set(), True
    workspace = checkpoint.get("worktree")
    if isinstance(workspace, dict) and isinstance(workspace.get("path"), str):
        worktrees.add(workspace["path"])
    else:
        complete = False  # The coordinator's own location is unrecorded.
    refs = checkpoint.get("native_refs")
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, dict) and isinstance(ref.get("runId"), str):
            runs.add(ref["runId"])
    admissions = raw.get("admissions") if isinstance(raw.get("admissions"), dict) else {}
    for row in admissions.values():
        if not isinstance(row, dict):
            complete = False
            continue
        if isinstance(row.get("run_id"), str):
            runs.add(row["run_id"])
        binding = row.get("native_binding") if isinstance(row.get("native_binding"), dict) else {}
        place = _location(binding.get("worktreeId")) or _location(row.get("worktree"))
        if place is None:
            complete = False
        else:
            worktrees.add(place)
    return {"worktrees": sorted(worktrees), "runs": sorted(runs), "locations_complete": complete}


def _phase(raw: dict) -> str:
    if isinstance(raw.get("continued_by"), dict):
        return "continued"
    checkpoint = raw.get("checkpoint")
    return "closed" if isinstance(checkpoint, dict) and checkpoint.get("closure") else "open"


def _schema(raw: dict) -> str:
    from .installer import superseded_schemas
    from .ledger import _validate_context
    if superseded_schemas(raw):
        return "superseded"
    try:
        _validate_context(raw)
    except PodError:
        return "unsupported"
    return "current"


def objective_list(project: Path, *, repository: str | None = None, worktree: str | None = None) -> dict:
    """Every recorded objective of one repository, or of one worktree of it."""
    from .github import repository_context
    from .ledger import CONTEXT_LIMIT, record_name, state_root
    target = Path(worktree or repository or project).expanduser().resolve()
    if not target.is_dir():
        raise PodError("list_scope_unavailable", "The listed repository or worktree path is not a directory")
    context = repository_context(target)
    root = state_root(target)
    scope = {"repository": context["repository"], "repo_key": context["repo_key"],
             "worktree": context["worktree"] if worktree else None, "state_root": str(root)}
    result = {"schema": "pod-cli/v5", "status": "listed", "scope": scope, "objectives": [],
              "total": 0, "truncated": False, "complete": True,
              "scan": {"directories": 0, "scanned": 0},
              "unreadable_records": [], "unattributed_records": []}
    try:
        if root.is_symlink():
            raise OSError("redirected")
        with os.scandir(root) as frontier:
            directories = sorted((entry for entry in frontier), key=lambda entry: entry.name)
    except FileNotFoundError:
        return {**result, "status": "absent", "complete": True,
                "next_safe_action": "no Pod objective has been recorded under this state root"}
    except OSError:
        return {**result, "status": "unreadable", "complete": False,
                "next_safe_action": "the Pod state root cannot be enumerated; nothing can be proven absent"}
    rows, unreadable, unattributed = [], [], []
    directories = [entry for entry in directories if not entry.name.startswith(".")]
    result["scan"] = {"directories": len(directories), "scanned": min(len(directories), MAX_SCAN)}
    for entry in directories[:MAX_SCAN]:
        try:
            if entry.is_symlink():
                unreadable.append({"record": entry.name, "code": "unsafe_state"})
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            with os.scandir(entry.path) as contents:
                names = {item.name for item in contents}
        except OSError:
            unreadable.append({"record": entry.name, "code": "unreadable"})
            continue
        if "context.json" not in names:
            if names - {".lock"}:
                unattributed.append(entry.name)  # Pod files without the record that names them.
            continue
        try:
            raw = bounded_json(Path(entry.path) / "context.json", limit=CONTEXT_LIMIT)
        except PodError as exc:
            unreadable.append({"record": entry.name, "code": exc.code})
            continue
        if not isinstance(raw, dict):
            unreadable.append({"record": entry.name, "code": "state_unsupported"})
            continue
        name = _name(raw)
        if name is None:
            unattributed.append(entry.name)
            continue
        if record_name(target, context, name) != entry.name:
            continue  # Another repository's record, or another name under this key.
        facts = _facts(raw)
        row = {"objective": name, "record": entry.name, "phase": _phase(raw), "schema": _schema(raw),
               **facts}
        from .succession import links
        row.update(links(target, raw))
        if worktree is not None:
            if context["worktree"] in facts["worktrees"]:
                row["match"] = "worktree"
            elif not facts["locations_complete"]:
                row["match"] = "repository"  # Some recorded work has no location; it may touch it.
            else:
                continue
        rows.append(row)
    rows.sort(key=lambda row: (row["objective"], row["record"]))
    truncated = len(rows) > MAX_ROWS or len(directories) > MAX_SCAN
    result.update(objectives=rows[:MAX_ROWS], total=len(rows), truncated=truncated,
                  unreadable_records=unreadable[:MAX_DIAGNOSTICS],
                  unattributed_records=unattributed[:MAX_DIAGNOSTICS],
                  complete=not truncated and not unreadable and not unattributed)
    if len(unreadable) > MAX_DIAGNOSTICS or len(unattributed) > MAX_DIAGNOSTICS:
        result["diagnostics_omitted"] = (max(0, len(unreadable) - MAX_DIAGNOSTICS)
                                         + max(0, len(unattributed) - MAX_DIAGNOSTICS))
    result["next_safe_action"] = (
        "every record under the state root was read; this list is complete" if result["complete"]
        else "treat unreadable, unattributed or unscanned records as possibly touching this scope")
    return result


def render(result: dict) -> None:
    from .term import clean
    scope = result["scope"]
    where = scope.get("worktree") or scope.get("repository") or scope.get("repo_key") or "this directory"
    print(f"Pod objectives for {clean(str(where))}: {result['status']}; {result['total']} recorded"
          + ("; truncated" if result["truncated"] else "") + ("" if result["complete"] else "; incomplete"))
    for row in result["objectives"]:
        link = (f" -> {clean(row['continued_by'])}" if row.get("continued_by") else "")
        print(f"  {clean(row['objective'])[:64]} | {row['phase']}{link} | {row['schema']} | "
              f"runs {', '.join(clean(run) for run in row['runs'][:4]) or 'none'} | "
              f"worktrees {', '.join(clean(path) for path in row['worktrees'][:2]) or 'unrecorded'}")
    for item in result["unreadable_records"]:
        print(f"  Unreadable record {clean(item['record'])}: {clean(item['code'])}")
    for record in result["unattributed_records"]:
        print(f"  Unattributed record {clean(record)}")
    print(f"State root: {clean(scope['state_root'])}")
    print(f"Next: {result['next_safe_action']}")

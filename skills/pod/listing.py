"""Read-only list of the objectives recorded for one repository or worktree (R108).

Membership is proved by the state key, never guessed from paths. The list reads Pod's own state
directory and Git identity, so it also works outside an Orca terminal; inside one, open rows whose
recorded runtime differs from Orca's add a bounded window-continuation check (pod#110). It never
writes, converts or locks a record.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import PodError
from .history import load_context

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
    if isinstance(raw.get("historical_retirement"), dict):
        return "retired"  # Released by the owner; never closed or completed (R112).
    checkpoint = raw.get("checkpoint")
    return "closed" if isinstance(checkpoint, dict) and checkpoint.get("closure") else "open"


def _schema(raw: dict) -> str:
    from .installer import superseded_schemas
    from .ledger import _validate_context
    if superseded_schemas(raw):
        from .handoff import _older_schema
        return "superseded" if _older_schema(raw) else "unsupported"
    try:
        _validate_context(raw)
    except PodError:
        return "unsupported"
    return "current"


def objective_list(project: Path, *, repository: str | None = None, worktree: str | None = None,
                   delivered: bool = False) -> dict:
    """Every recorded objective of one repository, or of one worktree of it.

    `delivered` adds each open objective's read-only delivery proof (R148), which reads Orca and GitHub; the plain
    list stays record-only."""
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
            raw = load_context(Path(entry.path) / "context.json", validate=False)
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
        from .evidence_closure import block as closure_block, compact
        closure = closure_block(Path(entry.path))
        phase = _phase(raw)
        if closure is not None and phase == "open":
            phase = "closed"  # Closed by evidence, whatever the record's schema (R145, R146).
        row = {"objective": name, "record": entry.name, "phase": phase, "schema": _schema(raw),
               **facts}
        from .succession import links
        row.update(links(target, raw))
        from .readiness import label
        if closure is not None and phase == "closed":
            row["readiness"] = "closed"
        else:
            row["readiness"] = label(Path(entry.path), raw) if row["schema"] == "current" else "unavailable"
        row["closure_basis"] = "evidence" if closure is not None else "coordinator" if phase == "closed" else None
        row["evidence_closure"] = (None if closure is None else closure if closure["status"] == "unreadable"
                                   else compact(closure))
        if row["phase"] == "retired" and row["schema"] == "current":
            from .retirement import status_block
            row["retirement"] = status_block(raw)
        if worktree is not None:
            if context["worktree"] in facts["worktrees"]:
                row["match"] = "worktree"
            elif not facts["locations_complete"]:
                row["match"] = "repository"  # Some recorded work has no location; it may touch it.
            else:
                continue
        rows.append(row)
    rows.sort(key=lambda row: (row["objective"], row["record"]))
    if delivered:
        from .evidence_closure import prove
        for row in rows[:MAX_ROWS]:
            if row["phase"] == "open":
                proof = prove(target, row["objective"], readback=False)
                row["delivery_proof"] = {"status": proof["status"], "conditions": proof["conditions"]}
    # Open objectives whose coordinator a verified Orca window ended show their supported continuation step; this
    # reads Orca and the host verifier only for open rows, inside a live Orca terminal, within a bounded budget.
    from .window import discover
    discover(rows[:MAX_ROWS], project=target)
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
        if row.get("closure_basis") == "evidence":
            link += " (closed by evidence, coordinator gone)"
        print(f"  {clean(row['objective'])[:64]} | {row['phase']}{link} | {row['schema']} | "
              f"runs {', '.join(clean(run) for run in row['runs'][:4]) or 'none'} | "
              f"worktrees {', '.join(clean(path) for path in row['worktrees'][:2]) or 'unrecorded'} | "
              f"readiness {row['readiness']}")
        continuation = row.get("continuation")
        if continuation is not None:
            if continuation["status"] == "available":
                print(f"    Window continuation: available; in {clean(continuation['cwd'])} run "
                      f"{clean(continuation['command'])} (no owner decision)")
            else:
                print(f"    Window continuation: {continuation['status']} ({clean(str(continuation['condition']))})"
                      + (f"; {clean(continuation['next_action'])}" if continuation.get("next_action") else ""))
        proof = row.get("delivery_proof")
        if proof is not None:
            print(f"    Delivery proof: {proof['status'].replace('_', ' ')}"
                  + "".join(f"; {clean(item['code'])}" for item in proof["conditions"][:4]))
    for item in result["unreadable_records"]:
        print(f"  Unreadable record {clean(item['record'])}: {clean(item['code'])}")
    for record in result["unattributed_records"]:
        print(f"  Unattributed record {clean(record)}")
    print(f"State root: {clean(scope['state_root'])}")
    print(f"Next: {result['next_safe_action']}")

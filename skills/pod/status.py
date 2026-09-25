"""Read-only status projection and human rendering."""

from __future__ import annotations

from pathlib import Path

from .bundle import RELOAD_ACTION, checkpoint_identity, identity_drift_message, identity_label, identity_matches, running_identity
from .config import load as load_config
from .errors import PodError
from .ledger import context_root_for_run
from .orca import current_run, worker_rows
from .selection import active_constraints
from .term import clean


def status(project: Path, run: str | None, *, current_run_fn=current_run,
           worker_rows_fn=worker_rows) -> dict:
    preferences = load_config(project)
    running = running_identity()
    result = {"schema": "pod-cli/v4", "status": "selection_required", "run": run,
              "bundle_identity": {"running": running, "checkpoint": None, "drift": None},
              "preferences": {key: preferences[key] for key in ("path", "revision", "mode", "eligible", "not_set",
                                                               "max_active", "errors")},
              "constraints": [], "route_decisions": [], "route_mismatch": False,
              "active_constraints": [],
              "effective_unknown": False,
              "installed_version_drift": False, "blocker": None,
              "next_safe_action": "select a native Run", "usage": "unknown", "cost": "unknown"}
    if run is None:
        try:
            current = current_run_fn().get("run")
            run = current.get("id") if isinstance(current, dict) else None
        except PodError as exc:
            result.update({"status": "unavailable", "blocker": exc.code})
            return result
        if run is None:
            return result
    result["run"] = run
    native_error = None
    try:
        workers = worker_rows_fn(run)
    except PodError as exc:
        native_error = exc.code
        workers = {"workers": [], "scope": None, "complete": False}
    from .ledger import _read, kernel_view, superseded_for_run
    try:
        context_root = context_root_for_run(run)
        state = _read(context_root / "context.json") if context_root else None
        superseded = superseded_for_run(run) if context_root is None else []
    except PodError as exc:
        result.update({"status": "blocked", "blocker": exc.code,
                       "next_safe_action": "inspect the objective state"})
        return result
    if superseded:
        result.update({"status": "blocked", "blocker": "objective_superseded", "superseded": superseded,
                       "next_safe_action": ("settle its workers through Orca and start a new objective; "
                                            "the record is not converted")})
        return result
    checkpoint = state.get("checkpoint") if state else None
    recorded = checkpoint_identity(checkpoint) if checkpoint else None
    drift = bool(checkpoint and not identity_matches(recorded, running))
    admissions = list(state["admissions"].values()) if state else []
    checkpoint_refs = checkpoint.get("native_refs") if isinstance(checkpoint, dict) else None
    checkpoint_assignments = checkpoint.get("assignments") if isinstance(checkpoint, dict) else None
    checkpoint_refs = checkpoint_refs if isinstance(checkpoint_refs, list) else []
    checkpoint_assignments = checkpoint_assignments if isinstance(checkpoint_assignments, list) else []
    native_references = []
    for row in admissions[:64]:
        binding = row.get("native_binding") if isinstance(row.get("native_binding"), dict) else {}
        native_references.append({"admission_id": row["admission_id"], "run": row["run_id"],
                                  "task": row["task_id"], "dispatch": binding.get("dispatchId"),
                                  "worker": binding.get("workerId"),
                                  "terminal": binding.get("terminalHandle"),
                                  "worktree": binding.get("worktreeId"),
                                  "tab": None, "placement_readback": "unavailable",
                                  "surfaces": [], "warnings": [],
                                  "ui_visibility": "unverified", "ui_focus": "unverified",
                                  "state": row["state"], "runtime": row["runtime"]})
    result.update({"status": "unavailable" if native_error else "observed",
                   "objective": checkpoint.get("objective") if checkpoint else None,
                   "source": checkpoint.get("objective_source", "direct_objective") if checkpoint else None,
                   "selected_worktree": checkpoint.get("worktree") if checkpoint else None,
                   "constraints": state["constraints"] if state else [],
                   "active_constraints": active_constraints(state["constraints"], preferences) if state else [],
                   "route_decisions": [row["route_decision"] for row in admissions[:64]],
                   "native_references": native_references,
                   "native_references_truncated": len(admissions) > 64,
                   "checkpoint_join": {"native_refs": checkpoint_refs[:64],
                                       "assignments": checkpoint_assignments[:64],
                                       "plan_revision": checkpoint.get("plan_revision"),
                                       "candidate": checkpoint.get("candidate")}
                                      if checkpoint else None,
                   "route_mismatch": any(row["route_decision"].get("route_mismatch") for row in admissions),
                   "effective_unknown": any(row["route_decision"].get("effective_unknown") for row in admissions),
                   "installed_version_drift": drift,
                   "bundle_identity": {"checkpoint": recorded, "running": running, "drift": drift},
                   "installed_version_detail": identity_drift_message(checkpoint, running=running) if drift else None,
                   "native": {"worker_count": len(workers["workers"]), "scope": workers.get("scope"),
                              "complete": workers.get("complete")},
                   "pending_admissions": sum(row["state"] in ("reserved", "unresolved") for row in admissions),
                   "verification_gaps": checkpoint.get("verification_gaps", []) if checkpoint else [],
                   "remaining_gates": checkpoint.get("remaining_gates", []) if checkpoint else [],
                   "blocker": "route_mismatch" if any(row["route_decision"].get("route_mismatch") for row in admissions)
                              else "effective_unknown" if any(row["route_decision"].get("effective_unknown") for row in admissions)
                              else native_error or checkpoint.get("blocker") if checkpoint else native_error,
                   "next_safe_action": checkpoint.get("next_safe_action", "inspect native Run")
                                       if checkpoint else "inspect native Run"})
    objective = checkpoint.get("objective") if checkpoint else None
    if isinstance(objective, str) and checkpoint.get("obligations") is not None:
        try:
            view = kernel_view(project_for_state(checkpoint, project), objective, state=state)
            result["obligations"] = view["status"]["map"]
            result["obligation_lines"] = view["status"]["lines"]
            result["native_settlement"] = view["settlement"]
            for ref in native_references:
                ref.update(view.get("placements", {}).get(ref["admission_id"], {}))
            result["report"] = view["report"]
            if checkpoint.get("closure"):
                result["blocker"] = result["blocker"] or "objective_closed"
            elif (checkpoint.get("quiescence") or {}).get("state") == "quiescent":
                result["blocker"] = result["blocker"] or "quiescent"
                result["next_safe_action"] = checkpoint["quiescence"]["interim_report"]
        except PodError as exc:
            result["obligations"] = {"error": exc.code}
    if result["installed_version_drift"]:
        result["blocker"] = "installed_version_changed"
        result["next_safe_action"] = RELOAD_ACTION
    elif preferences["errors"] and result["blocker"] is None:
        result["blocker"] = "preferences_unavailable"
        result["next_safe_action"] = "inspect and correct the personal preference file"
    return result


def project_for_state(checkpoint: dict, project: Path) -> Path:
    """The objective's bound worktree when recorded, else the current directory."""
    worktree = checkpoint.get("worktree") if isinstance(checkpoint, dict) else None
    path = worktree.get("path") if isinstance(worktree, dict) else None
    return Path(path) if isinstance(path, str) and Path(path).is_dir() else project


def render(result: dict) -> None:
        print(f"Pod status: {result['status']}; objective {result.get('objective') or 'unknown'}")
        identity = result["bundle_identity"]
        if identity["checkpoint"] is not None:
            print(f"Checkpoint bundle: {identity_label(identity['checkpoint'])}; "
                  f"running: {identity_label(identity['running'])}")
        for ref in result.get("native_references", []):
            print(f"  Task {ref['task']} | Dispatch {ref['dispatch'] or 'pending'} | "
                  f"worker {ref['worker'] or 'unknown'} | terminal {ref['terminal'] or 'unavailable'} | "
                  f"tab {ref['tab'] or 'unavailable'} | worktree {ref['worktree'] or 'unverified'} | "
                  f"{ref['state']} | UI visibility/focus unverified")
            for surface in ref['surfaces']:
                print(f"    Orca placement surface: {clean(surface)}")
            for warning in ref['warnings']:
                print(f"    Orca placement warning: {clean(warning)}")
        for line in result.get("obligation_lines", []):
            print(line)
        for row in result.get("superseded", []):
            print(f"Superseded {row['objective'] or row['record']}: {', '.join(row['schemas'])} (blocked, not converted)")
        print(f"Blocker: {result.get('blocker') or 'none'}; next: {result['next_safe_action']}")

"""Read-only status projection and human rendering."""

from __future__ import annotations

from pathlib import Path

from .bundle import RELOAD_ACTION, checkpoint_identity, identity_drift_message, identity_label, identity_matches, running_identity
from .config import load as load_config
from .errors import PodError
from .ledger import context_root_for_run, contexts_for_run
from .orca import current_run, worker_rows
from .routes import project as project_routes
from .selection import active_constraints, failure_active
from .term import clean
from .util import route_summary

PREFERENCE_KEYS = ("path", "schema", "status", "revision", "eligible", "preferred", "pinned",
                   "max_active", "refresh", "errors", "setup")
# Objective status reads preferences only; observations stay with `pod models`.
NOT_READ = {"status": "not_read", "sources": {}}


def _attention(native: dict | None) -> dict | str:
    """Orca's own attention for one exact Dispatch; absent means unknown, never "not waiting"."""
    projection = native.get("projection") if isinstance(native, dict) else None
    attention = projection.get("attention") if isinstance(projection, dict) else None
    if not isinstance(attention, dict) or not isinstance(attention.get("categories"), list):
        return "unknown"
    return {"categories": [str(item) for item in attention["categories"]][:8],
            "requires_action": attention.get("requiresAction") is True}


def _attention_note(attention: dict | str) -> str:
    if attention == "unknown":
        return "; attention unknown"
    categories = attention["categories"]
    if {"input", "approval"} & set(categories):
        return f"; needs a human answer (Orca attention: {', '.join(categories)})"
    if "guidance" in categories:
        return "; needs the coordinator's guidance"
    return f"; needs attention: {', '.join(categories)}" if attention["requires_action"] else ""


def _orca_attention_label(attention: dict | str) -> str:
    """Orca's own attention for a row Pod cannot verify, labelled as Orca's rather than Pod's."""
    if attention == "unknown":
        return "Orca attention unknown"
    label = "Orca attention: " + (", ".join(clean(item)[:64] for item in attention["categories"]) or "none")
    return label + ("; Orca requires action" if attention["requires_action"] else "")


def settlement_failure(state: dict, view: dict | None, workers: dict) -> dict | None:
    """Name a failed exact native read: its code and the bound and current runtimes, or unknown."""
    if not isinstance(view, dict) or view.get("settlement") != "unverified":
        return None
    refs = (state.get("checkpoint") or {}).get("native_refs")
    runtimes = {row.get("runtime") for row in refs if isinstance(row, dict)} if isinstance(refs, list) else set()
    bound = next(iter(runtimes)) if len(runtimes) == 1 and isinstance(next(iter(runtimes)), str) else "unknown"
    current = workers.get("runtime")
    return {"code": view.get("settlement_failure") or "unknown", "bound_runtime": bound,
            "current_runtime": current if isinstance(current, str) and current else "unknown"}


def _objective_details(state: dict, view: dict | None, workers: dict, failure: dict | None = None) -> dict:
    checkpoint = state["checkpoint"]
    map_state = view["map"] if isinstance(view, dict) else None
    outstanding = (set((view.get("ctx") or {}).get("outstanding", [])) if isinstance(view, dict)
                   else {key for key, row in state["admissions"].items()
                         if row["state"] not in ("closed", "deferred")})
    active, settled, retained, unverified = [], [], [], []
    native_rows = {row.get("dispatchId"): row for row in workers.get("workers", [])
                   if isinstance(row, dict) and isinstance(row.get("dispatchId"), str)}
    for key, row in state["admissions"].items():
        binding = row.get("native_binding") or {}
        item = {"admission": key, "role": row["role"], "serves": list(row["serves"]),
                "route": row["route_decision"], "dispatch": binding.get("dispatchId"),
                "state": row["state"]}
        if failure is not None and row["state"] not in ("closed", "deferred"):
            # Presentation only: without exact native evidence an open row is neither active nor settled.
            item["orca_attention"] = _attention(native_rows.get(binding.get("dispatchId")))
            unverified.append(item)
            continue
        if key in outstanding:
            item["attention"] = _attention(native_rows.get(binding.get("dispatchId")))
        (active if key in outstanding else settled).append(item)
        facts = (view or {}).get("placements", {}).get(key, {}) if key not in outstanding else {}
        liveness = facts.get("native_liveness") or {}
        resource = facts.get("native_release") or {}
        owned = facts.get("terminal_ownership") or {}
        terminal_state = facts.get("native_terminal_state") or resource.get("state")
        if (liveness.get("verdict") == "live" and terminal_state != "released"
                and facts.get("terminal") == binding.get("terminalHandle")
                and binding.get("terminalHandle") is not None
                and (not owned or owned.get("ownerDispatchId") == binding["dispatchId"])):
            retained.append({"admission": key, "dispatch": binding["dispatchId"],
                             "terminal": binding["terminalHandle"], "state": terminal_state,
                             "reason": facts.get("retained_reason")})
        elif not any(facts.get(field) for field in ("native_liveness", "native_release", "native_terminal_state", "terminal_ownership")):
            # Older exact receipts omit resource facts; preserve their verified list readback.
            native = native_rows.get(binding.get("dispatchId")) if key not in outstanding else None
            projection = native.get("projection") if isinstance(native, dict) else None
            live = projection.get("liveness") if isinstance(projection, dict) else None
            if (isinstance(live, dict) and live.get("verdict") == "live"
                    and native.get("agentTerminalHandle") == binding.get("terminalHandle")
                    and binding.get("terminalHandle") is not None and native.get("terminalState") != "released"):
                retained.append({"admission": key, "dispatch": binding["dispatchId"],
                                 "terminal": binding["terminalHandle"], "state": native.get("terminalState")})
    obligations = map_state.get("obligations", []) if isinstance(map_state, dict) else []
    external = [{"obligation": ob["id"], **ob["external"]} for ob in obligations
                if ob["state"] == "blocked_external"]
    authority = [{"obligation": ob["id"], "referent": ob["wait"]["referent"]}
                 for ob in obligations if ob["state"] == "waiting" and ob["wait"]["class"] == "authority"]
    remaining = list(checkpoint.get("remaining_gates", []))
    progress = []
    by_admission = {row["admission"]: row for row in active}
    by_unverified = {row["admission"]: row for row in unverified}
    for ob in obligations:
        if ob["state"] == "active" and ob["executor"] == "coordinator":
            progress.append(f"coordinator: {ob['id']} ({ob.get('check') or ob.get('resolves') or 'current work'})")
        elif ob["state"] == "active" and ob["executor"] in by_unverified:
            attempt = by_unverified[ob["executor"]]
            progress.append(f"settlement unverified for {attempt['role']} (dispatch "
                            f"{attempt['dispatch'] or 'pending'}); "
                            + _orca_attention_label(attempt["orca_attention"]))
        elif ob["state"] == "active":
            attempt = by_admission.get(ob["executor"])
            if attempt:
                dispatch = attempt["dispatch"] or "pending"
                progress.append(f"waiting for {attempt['role']} delivery (dispatch {dispatch})"
                                + _attention_note(attempt["attention"]))
        elif ob["state"] == "blocked_external":
            progress.append(f"waiting for {ob['external']['party']}: {ob['external']['need']}")
    for gate in remaining:
        if isinstance(gate, dict) and gate.get("kind") == "hosted_ci":
            progress.append(f"waiting for hosted CI ({gate.get('workflow', 'workflow')} on "
                            f"{str(checkpoint.get('candidate') or 'unknown')[:12]})")
        else:
            progress.append("remaining gate: " + str(gate)[:160])
    if authority:
        next_action = "obtain authority for " + authority[0]["obligation"]
    elif failure is not None:
        next_action = (f"native settlement is unverified ({failure['code']}; bound runtime "
                       f"{failure['bound_runtime']}, current runtime {failure['current_runtime']}): "
                       f"{len(unverified)} open admission(s) stay unverified until an exact native read succeeds")
    elif active:
        next_action = "wait for the exact outstanding assignment result"
    elif remaining:
        next_action = "complete the next recorded gate"
    elif map_state and map_state.get("closure"):
        next_action = "objective closed"
    elif progress:
        next_action = progress[0]
    else:
        next_action = "read the objective map and choose the next ready obligation"
    assignments = {"active": active, "settled": settled}
    if failure is not None:
        assignments["unverified"] = unverified
    return {"assignments": assignments,
            "retained_terminals": retained,
            "gates": {"remaining": remaining, "blocked_external": external, "authority_waits": authority},
            "progress": progress, "recorded_next_action": checkpoint.get("next_safe_action"),
            "next_action": next_action}


def status(project: Path, run: str | None, *, objective: str | None = None, current_run_fn=current_run,
           worker_rows_fn=worker_rows) -> dict:
    preferences = load_config(project)
    running = running_identity()
    result = {"schema": "pod-cli/v5", "status": "selection_required", "run": run,
              "bundle_identity": {"running": running, "checkpoint": None, "drift": None},
              "preferences": {key: preferences[key] for key in PREFERENCE_KEYS},
              "routes": project_routes(project, preferences=preferences, observations=NOT_READ)["summary"],
              "constraints": [], "route_decisions": [], "route_mismatch": False,
              "active_constraints": [],
              "effective_unknown": False,
              "installed_version_drift": False, "blocker": None,
              "next_safe_action": "select a native Run", "usage": "unknown", "cost": "unknown"}
    if run is None and objective is None:
        try:
            current = current_run_fn().get("run")
            run = current.get("id") if isinstance(current, dict) else None
        except PodError as exc:
            result.update({"status": "unavailable", "blocker": exc.code})
            if exc.detail:
                result["detail"] = exc.detail
            if (exc.detail or {}).get("native_code") == "no_active_sender_terminal":
                result["next_safe_action"] = "This process is not in a live Orca terminal; Pod records need one. Observe read-only with pod status --objective ID"
            return result
        if run is None:
            return result
    from .ledger import _read, kernel_view, read, superseded_for_run
    try:
        if objective is None:
            matches = contexts_for_run(run)
            if len(matches) > 1:
                result.update({"run": run, "choices": sorted([
                    {"objective": (row.get("checkpoint") or {}).get("objective"),
                     "worktree": ((row.get("checkpoint") or {}).get("worktree") or {}).get("path"),
                     "seq": (row.get("checkpoint") or {}).get("seq"),
                     "state": "closed" if (row.get("checkpoint") or {}).get("closure") else "open"}
                    for _, row in matches], key=lambda row: str(row["objective"])),
                    "next_safe_action": "rerun with --objective ID"})
                return result
            context_root = matches[0][0] if matches else None
            state = matches[0][1] if matches else None
            superseded = superseded_for_run(run) if context_root is None else []
        else:
            state = read(project, objective)
            if state is None or not isinstance(state.get("checkpoint"), dict):
                from .ledger import state_root
                result.update({"status": "blocked", "blocker": "objective_unknown",
                               "next_safe_action": "name a recorded objective in this project",
                               "objective": objective, "repository_context": str(project),
                               "state_root_searched": str(state_root(project))})
                return result
            refs = {row.get("runId") for row in state["checkpoint"].get("native_refs", [])
                    if isinstance(row, dict)}
            refs.update(row.get("run_id") for row in state["admissions"].values())
            if run is not None and run not in refs:
                result.update({"status": "blocked", "blocker": "objective_run_mismatch",
                               "next_safe_action": "select a Run recorded by this objective"})
                return result
            if run is None:
                try:
                    current = current_run_fn().get("run")
                    current_id = current.get("id") if isinstance(current, dict) else None
                except PodError as exc:
                    current_id = None
                    if exc.detail:
                        result["detail"] = exc.detail
                run = current_id if current_id in refs else next(iter(refs)) if len(refs) == 1 else None
            superseded = []
    except PodError as exc:
        result.update({"status": "blocked", "blocker": exc.code,
                       "next_safe_action": "inspect the objective state"})
        return result
    result["run"] = run
    native_error = None
    if run is not None:
        try:
            workers = worker_rows_fn(run)
        except PodError as exc:
            native_error = exc.code
            if exc.detail:
                result["detail"] = exc.detail
            workers = {"workers": [], "scope": None, "complete": False}
    else:
        native_error = "run_unavailable"
        workers = {"workers": [], "scope": None, "complete": False}
    if superseded:
        result.update({"status": "blocked", "blocker": "objective_superseded", "superseded": superseded,
                       "next_safe_action": ("settle its workers through Orca and start a new objective; "
                                            "the record is not converted")})
        return result
    checkpoint = state.get("checkpoint") if state else None
    recorded = checkpoint_identity(checkpoint) if checkpoint else None
    drift = bool(checkpoint and not identity_matches(recorded, running))
    admissions = list(state["admissions"].values()) if state else []
    mismatch, unknown = route_summary(admissions)
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
                   "route_failures": [{"admission": key, **failure, "active": failure_active(failure)}
                                      for key, row in (state or {}).get("admissions", {}).items()
                                      for failure in row["failures"]],
                   "active_constraints": active_constraints(state["constraints"], preferences) if state else [],
                   "route_decisions": [row["route_decision"] for row in admissions[:64]],
                   "native_references": native_references,
                   "native_references_truncated": len(admissions) > 64,
                   "checkpoint_join": {"native_refs": checkpoint_refs[:64],
                                       "assignments": checkpoint_assignments[:64],
                                       "plan_revision": checkpoint.get("plan_revision"),
                                       "candidate": checkpoint.get("candidate")}
                                      if checkpoint else None,
                   "route_mismatch": mismatch,
                   "effective_unknown": unknown,
                   "installed_version_drift": drift,
                   "bundle_identity": {"checkpoint": recorded, "running": running, "drift": drift},
                   "installed_version_detail": identity_drift_message(checkpoint, running=running) if drift else None,
                   "native": {"worker_count": len(workers["workers"]), "scope": workers.get("scope"),
                              "complete": workers.get("complete")},
                   "pending_admissions": sum(row["state"] in ("reserved", "unresolved") for row in admissions),
                   "verification_gaps": checkpoint.get("verification_gaps", []) if checkpoint else [],
                   "remaining_gates": checkpoint.get("remaining_gates", []) if checkpoint else [],
                   "delivery": checkpoint.get("delivery") if checkpoint else None,
                   "blocker": "route_mismatch" if mismatch
                              else "effective_unknown" if unknown
                              else native_error or checkpoint.get("blocker") if checkpoint else native_error,
                   "next_safe_action": checkpoint.get("next_safe_action", "inspect native Run")
                                       if checkpoint else "inspect native Run"})
    objective = checkpoint.get("objective") if checkpoint else None
    view = None
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
    continuity = checkpoint.get("continuity") if isinstance(checkpoint, dict) else None
    if isinstance(continuity, dict) and continuity.get("history"):
        # The objective's runtime rebind history only, read-only; status never rebinds (A2).
        result["runtime_continuity"] = {"history": list(continuity["history"])}
    if state is not None and checkpoint is not None:
        failure = settlement_failure(state, view, workers)
        if failure is not None:
            result["native_settlement_failure"] = failure
        result.update(_objective_details(state, view, workers, failure))
    if result["installed_version_drift"]:
        result["blocker"] = "installed_version_changed"
        result["next_safe_action"] = RELOAD_ACTION
    elif preferences["errors"] and result["blocker"] is None:
        result["blocker"] = "route_setup_required" if preferences["setup"] else "preferences_unavailable"
        result["next_safe_action"] = (preferences["setup"]["action"] if preferences["setup"]
                                      else "inspect and correct the personal preference file")
    if result.get("blocker") in ("installed_version_changed", "route_mismatch", "effective_unknown",
                                 "preferences_unavailable", "route_setup_required"):
        result["next_action"] = result["next_safe_action"]
    elif native_error is not None and "native_settlement_failure" not in result:
        result["next_action"] = "inspect the native Run before deciding worker state"
    if (result.get("detail") or {}).get("native_code") == "no_active_sender_terminal":
        result["next_safe_action"] = "This process is not in a live Orca terminal; Pod records need one. Observe read-only with pod status --objective ID"
        result["next_action"] = result["next_safe_action"]
    if state is not None and checkpoint is not None:
        from .handoff import nonowner_detection
        handoff = nonowner_detection(project, objective, state, state["owner"])
        if handoff is not None:
            result.update(owner_handoff=handoff, blocker=handoff["status"],
                          next_safe_action=handoff["next_action"], next_action=handoff["next_action"])
    return result


def project_for_state(checkpoint: dict, project: Path) -> Path:
    """The objective's bound worktree when recorded, else the current directory."""
    worktree = checkpoint.get("worktree") if isinstance(checkpoint, dict) else None
    path = worktree.get("path") if isinstance(worktree, dict) else None
    return Path(path) if isinstance(path, str) and Path(path).is_dir() else project


def render(result: dict) -> None:
        print(f"Pod status: {result['status']}; objective {result.get('objective') or 'unknown'}")
        for choice in result.get("choices", []):
            print(f"  Choose {choice['objective']} | {choice['worktree'] or 'unbound'} | "
                  f"seq {choice['seq']} | {choice['state']}")
        identity = result.get("bundle_identity", {"checkpoint": None})
        if identity["checkpoint"] is not None:
            print(f"Checkpoint bundle: {identity_label(identity['checkpoint'])}; "
                  f"running: {identity_label(identity['running'])}")
        for ref in result.get("native_references", []):
            print(f"  Task {ref['task']} | Dispatch {ref['dispatch'] or 'pending'} | "
                  f"worker {ref['worker'] or 'unknown'} | terminal {ref['terminal'] or 'unavailable'} | "
                  f"tab {ref['tab'] or 'unavailable'} | worktree {ref['worktree'] or 'unverified'} | "
                  f"{ref['state']} | UI visibility/focus unverified")
            if ref.get("native_release") or ref.get("native_terminal_state") or (ref.get("native_stage") or {}).get("dispatch") == "failed":
                print(f"    Orca release: {ref.get('native_release')}; terminal state: {ref.get('native_terminal_state')}; Dispatch stage: {ref.get('native_stage')}; retained reason: {ref.get('retained_reason') or 'unknown'}")
            for surface in ref['surfaces']:
                print(f"    Orca placement surface: {clean(surface)}")
            for warning in ref['warnings']:
                print(f"    Orca start-time placement warning: {clean(warning)}")
        if result.get("state_root_searched"):
            print(f"Searched objective {result['objective']} in repository context {result['repository_context']}; state root {result['state_root_searched']}")
        for line in result.get("obligation_lines", []):
            print(line)
        if result.get("assignments"):
            assignments = result["assignments"]
            print(f"Assignments: {len(assignments['active'])} active, {len(assignments['settled'])} settled"
                  + (f", {len(assignments['unverified'])} unverified" if "unverified" in assignments else ""))
            failure = result.get("native_settlement_failure")
            if failure:
                print(f"Native settlement unverified: {failure['code']}; bound runtime "
                      f"{failure['bound_runtime']}, current runtime {failure['current_runtime']}")
            for item in assignments.get("unverified", []):
                print(f"  Unverified {item['role']} (dispatch {item['dispatch'] or 'pending'}): "
                      + _orca_attention_label(item["orca_attention"]))
        handoff = result.get("owner_handoff")
        if isinstance(handoff, dict):
            print(f"Owner handoff: {clean(handoff['condition'])}; {clean(handoff['message'])}")
        for entry in (result.get("runtime_continuity") or {}).get("history", []):
            label = "Owner handoff" if entry["provenance"] == "owner_handoff" else "Runtime rebind"
            print(f"{label}: {clean(entry['from_runtime'])} -> {clean(entry['to_runtime'])} at "
                  f"{clean(entry['at'])} ({entry['provenance']})")
        for terminal in result.get("retained_terminals", []):
            print(f"Retained terminal: {terminal['terminal']} (dispatch {terminal['dispatch']})"
                  + (f"; reason: {terminal.get('reason') or 'unknown'}" if "reason" in terminal else ""))
        for line in result.get("progress", []):
            print(f"Progress: {line}")
        if result.get("gates"):
            gates = result["gates"]
            print(f"Gates: {len(gates['remaining'])} remaining, "
                  f"{len(gates['blocked_external'])} external, {len(gates['authority_waits'])} authority")
        for row in result.get("superseded", []):
            print(f"Superseded {row['objective'] or row['record']}: {', '.join(row['schemas'])} (blocked, not converted)")
        print(f"Blocker: {result.get('blocker') or 'none'}; next: {result['next_safe_action']}")
        if result.get("next_action"):
            print(f"Next action: {result['next_action']}")

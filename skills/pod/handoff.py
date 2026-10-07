"""Read-only loss detection and the sole explicit coordinator-change transition."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from .errors import PodError
from .util import bounded_text, exact

FACTS = {"objective", "from_owner", "to_owner", "run", "recorded_generation",
         "recorded_runtime", "current_runtime", "ambiguity"}
DECISION = FACTS | {"provenance", "instruction"}
ENTRY = {"provenance", "state", "from_runtime", "to_runtime", "at", "verified",
         "decision", "abort_reason", "completion"}


def validate_entry(row: dict) -> None:
    decision = row.get("decision")
    if (set(row) != ENTRY or row.get("state") not in ("pending", "done", "aborted")
            or not isinstance(row.get("verified"), dict)
            or not isinstance(decision, dict) or set(decision) != DECISION
            or decision.get("provenance") != "user_direct"
            or any(not isinstance(decision.get(key), str) or not decision[key]
                   for key in DECISION - {"ambiguity", "recorded_generation"})
            or (decision["recorded_generation"] is not None and
                (type(decision["recorded_generation"]) is not int or decision["recorded_generation"] < 0))
            or not isinstance(decision.get("ambiguity"), list)
            or any(not isinstance(item, str) for item in decision["ambiguity"])
            or row.get("abort_reason") is not None and not isinstance(row["abort_reason"], str)):
        raise PodError("state_unsupported", "Owner handoff history is malformed")
    # Presentation and pane identities are never accepted as authority or stored evidence.
    allowed = {"run", "coordinator", "generation", "admissions", "absent", "owner_code", "caller_runtime"}
    completion = row["completion"]
    if (set(row["verified"]) - allowed or
            (row["state"] == "done") != isinstance(completion, dict) or
            completion is not None and (not isinstance(completion, dict) or set(completion) - allowed)):
        raise PodError("state_unsupported", "Owner handoff evidence is unsupported")


def unfinished(state: dict) -> dict | None:
    history = ((state.get("checkpoint") or {}).get("continuity") or {}).get("history", [])
    rows = [row for row in history if row.get("provenance") == "owner_handoff" and row["state"] != "done"]
    if len(rows) > 1:
        raise PodError("state_unsupported", "More than one unfinished owner handoff")
    return rows[0] if rows else None


def run_scope(project: Path, objective: str, run_id: str) -> dict:
    """Prove scope without the diagnostic reader's unsupported-record skip.

    A positive raw reference blocks unless a supported record proves closure.
    Unknown membership holds; no record is converted, locked or rewritten.
    """
    from .ledger import (CONTEXT_LIMIT, _validate_context,
                         _run_references, objective_root, state_root, kernel_context)
    from .util import bounded_json
    root = state_root(project)
    if root.is_symlink():
        return {"objectives": [], "unreadable": 1}
    own = objective_root(project, objective) / "context.json"
    others, unknown = [], 0
    # A complete frontier is part of the proof. scandir/stat propagate errors that
    # Path.glob suppresses; an unavailable directory is never apparent absence.
    paths = []
    try:
        with os.scandir(root) as frontier:
            directories = list(frontier)
    except FileNotFoundError:
        return {"objectives": [], "unreadable": 1}
    except OSError:
        return {"objectives": [], "unreadable": 1}
    for directory in directories:
        try:
            if directory.is_symlink():
                unknown += 1
                continue
            if not directory.is_dir(follow_symlinks=False):
                continue
            with os.scandir(directory.path) as contents:
                entries = list(contents)
            paths.extend(Path(entry.path) for entry in entries if entry.name == "context.json")
        except OSError:
            unknown += 1
    for path in paths:
        if path == own:
            continue
        try:
            raw = bounded_json(path, limit=CONTEXT_LIMIT)
        except PodError:
            unknown += 1
            continue
        if not isinstance(raw, dict):
            unknown += 1
            continue
        checkpoint = raw.get("checkpoint")
        admissions = raw.get("admissions")
        refs = checkpoint.get("native_refs") if isinstance(checkpoint, dict) else [] if checkpoint is None else None
        readable = (isinstance(refs, list) and isinstance(admissions, dict)
                    and all(isinstance(row, dict) and isinstance(row.get("runId"), str) and row["runId"] for row in refs)
                    and all(isinstance(row, dict) and isinstance(row.get("run_id"), str) and row["run_id"] for row in admissions.values()))
        referenced = (isinstance(refs, list) and any(isinstance(row, dict) and row.get("runId") == run_id for row in refs)
                      or isinstance(admissions, dict) and any(isinstance(row, dict) and row.get("run_id") == run_id for row in admissions.values()))
        supported = False
        try:
            _run_references(_validate_context(raw))
            supported = True
        except PodError:
            pass
        if referenced:
            closure = checkpoint.get("closure") if isinstance(checkpoint, dict) else None
            if supported and closure is not None:
                from .obligations import validate_closed_snapshot
                try:
                    # No peer settlement is inferred: absent native evidence leaves
                    # bound/reserved/unresolved admissions outstanding in this context.
                    workspace = checkpoint.get("worktree") or {}
                    peer_project = Path(workspace["path"]) if workspace.get("path") else (
                        project if objective_root(project, checkpoint["objective"]) == path.parent else None)
                    if peer_project is None:
                        raise PodError("closure_unverified", "Peer repository binding is unavailable")
                    if not peer_project.is_dir():
                        raise PodError("closure_unverified", "Peer worktree is unreadable")
                    ctx = kernel_context(peer_project, checkpoint["objective"], raw, None)
                    validate_closed_snapshot(checkpoint, ctx)
                except (PodError, KeyError, TypeError, ValueError, OSError):
                    pass
                else:
                    continue
            name = checkpoint.get("objective") if isinstance(checkpoint, dict) else None
            others.append(name if isinstance(name, str) and name and len(name) <= 256 else "unnamed objective")
        elif not readable or not supported:
            # Only documented reference layouts can prove non-membership.
            unknown += 1
    return {"objectives": sorted(set(others)), "unreadable": unknown}


def detect(project: Path, objective: str, state: dict, *, port, caller: str | None = None) -> dict:
    """Fresh native facts only. No writes, native mutations or presentation data."""
    from .ledger import _run_references, continuity_work
    from .orca import current_run, terminal_identity, worker_rows
    caller = os.environ.get("ORCA_TERMINAL_HANDLE") if caller is None else caller
    owner = state.get("owner")
    checkpoint = state.get("checkpoint") or {}
    binding = (checkpoint.get("continuity") or {}).get("binding")
    refs = _run_references(state)
    run_id = binding["run"] if binding else next(iter(refs)) if len(refs) == 1 else None
    generation = binding["generation"] if binding else None
    facts = {"objective": objective, "from_owner": owner, "to_owner": caller, "run": run_id,
             "recorded_generation": generation,
             "recorded_runtime": next(iter(set(refs.values()))) if len(set(refs.values())) == 1 else None,
             "current_runtime": None, "ambiguity": []}
    result = {"status": "handoff_unavailable", "condition": "caller_not_linked", "facts": facts,
              "definitive": False, "verified": {}}

    def fail(condition: str, message: str, *, definitive=False, action=None) -> dict:
        result.update(condition=condition, message=message, definitive=definitive,
                      next_action=action or "Continue from the original pane, or start a successor objective. Do not run orca orchestration run-use by hand.")
        return result

    if caller == owner:
        return fail("recorded_owner", "This caller is the recorded owner; use the ordinary authority and runtime-continuity checks.")
    if facts["recorded_runtime"] is None:
        return fail("recorded_runtime_unreadable", "The objective has no single readable recorded runtime.")
    if checkpoint.get("closure"):
        return fail("objective_closed", "The objective is closed.", definitive=True,
                    action="Start a successor objective; closure does not authorize a handoff.")
    if not caller:
        return fail("caller_not_live", "This process is not in a live Orca terminal; observe with pod status --objective ID.")
    own = terminal_identity(caller)
    if own["state"] != "live":
        return fail("caller_not_live", "Orca cannot prove this caller's handle live (" + (own["code"] or "successful terminal show did not prove liveness") + ").",
                    definitive=own["state"] == "lost")
    pending = unfinished(state)
    if pending and pending["state"] == "pending" and pending["decision"]["to_owner"] != caller:
        holder = terminal_identity(pending["decision"]["to_owner"])
        if holder["state"] != "lost":
            return fail("pending_other_caller", "The pending handoff belongs to another terminal whose handle Orca has not reported stale or gone.",
                        action="Complete it in that terminal, or wait until Orca reports its handle stale or gone; do not run orca orchestration run-use by hand.")
        # Detection cannot abort; the operation alone records and replaces this attempt.
        result["replace_pending"] = "pending caller handle stale or gone"
    try:
        first = current_run()
    except PodError as exc:
        return fail("binding_unreadable", "The caller's current Run binding could not be read (" + exc.code + ").")
    facts["current_runtime"] = first["runtime"]
    run = first["run"]
    if run is None or own["runtime"] != first["runtime"]:
        return fail("caller_not_linked", "The caller has no stable current Run binding at its live runtime.")
    if run_id is None or run["id"] != run_id:
        return fail("different_run", "The caller resolves a different Run from the recorded objective.", definitive=True)
    if run["coordinator_handle"] == owner:
        branch = "fresh"
        if generation is not None and run["consumer_generation"] != generation:
            return fail("generation_changed", "The recorded owner's consumer generation differs.", definitive=True,
                        action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    elif (run["coordinator_handle"] == caller and pending and pending["state"] == "pending"
          and all(pending["decision"][key] == facts[key] for key in FACTS - {"ambiguity", "current_runtime"})):
        branch = "resume"
        if generation is not None and run["consumer_generation"] <= generation:
            return fail("generation_changed", "The rebound Run has no generation above the recorded one.", definitive=True,
                        action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    else:
        return fail("lineage_unproven", "The Run was rebound outside a live matching pending handoff; lineage is unproven.", definitive=True,
                    action="Continue in a successor or recovery objective; do not run orca orchestration run-use by hand.")
    previous = terminal_identity(owner)
    if previous["state"] != "lost":
        return fail("owner_not_lost", "Orca has not reported the recorded owner's handle stale or gone (" + (previous["code"] or previous["state"]) + ").",
                    action="Use the recorded coordinator, or resolve the unreadable handle state; do not run orca orchestration run-use by hand.")
    if any((row.get("native_binding") or {}).get("terminalHandle") == caller
           for row in state["admissions"].values()):
        return fail("caller_is_worker", "The caller is a worker terminal of this objective.", definitive=True)
    try:
        fleet = worker_rows(run_id)
    except PodError as exc:
        return fail("workers_unreadable", "The Run's worker list could not be read (" + exc.code + ").")
    if (fleet["runtime"] != first["runtime"] or fleet.get("complete") is not True
            or any(not isinstance(row, dict) for row in fleet["workers"])):
        return fail("workers_unreadable", "The worker list does not prove complete current-runtime scope.")
    if any(part.get(key) == caller for row in fleet["workers"]
           for part in (row, row.get("worker"), row.get("projection")) if isinstance(part, dict)
           for key in ("terminalHandle", "agentTerminalHandle")):
        return fail("caller_is_worker", "The caller has a worker-only binding on this Run.", definitive=True)
    scope = run_scope(project, objective, run_id)
    if scope["objectives"]:
        result["other_objectives"] = scope["objectives"]
        return fail("run_shared", "Another open or unverified-closed objective records this Run: " + ", ".join(scope["objectives"]) + ".", definitive=True,
                    action="Resolve the shared objective scope before handoff; do not run orca orchestration run-use by hand.")
    if scope["unreadable"]:
        result["unreadable_objectives"] = scope["unreadable"]
        return fail("scope_unreadable", "Run scope is unverified: " + str(scope["unreadable"]) + " objective record(s) could not prove membership or closure.",
                    action="Resolve the unreadable objective records before handoff; do not run orca orchestration run-use by hand.")
    work = continuity_work(state, runtime=first["runtime"], port=port)
    if work["differs"]:
        result["differs"] = work["differs"]
        return fail("identity_differs", "A bound admission identity differs: " + ", ".join(work["differs"]) + ".", definitive=True,
                    action="Continue in a successor or recovery objective; differing identities cannot be overridden. Do not run orca orchestration run-use by hand.")
    try:
        after = current_run()
    except PodError:
        after = None
    if after != first:
        return fail("binding_unstable", "The caller's Run binding or runtime changed during detection.")
    facts["ambiguity"] = work["ambiguity"] + (["recorded consumer generation (none recorded)"] if binding is None else [])
    facts["ambiguity"] += [f"run {other}: not shown by run-current" for other in sorted(set(refs) - {run_id})]
    from .ledger import CONTINUITY_AMBIGUITY
    if len(facts["ambiguity"]) > CONTINUITY_AMBIGUITY:
        return fail("ambiguity_full", "Too many unreadable identities to bind an exact confirmation.")
    history = (checkpoint.get("continuity") or {}).get("history", [])
    if pending is None and len(history) >= 8:
        return fail("history_full", "The eight-entry continuity history is full.",
                    action="Continue in a successor objective; do not run orca orchestration run-use by hand.")
    question = (f"May Pod make this terminal the coordinator of objective {objective}? "
                "Orca reports the previous coordinator's handle stale or gone; the objective's owner will change and its work will be preserved.")
    result.update(status="handoff_available", condition=branch, question=question,
                  message="Caller is not the recorded owner; an Owner-authorized handoff is available."
                          + (" Ambiguity: " + "; ".join(facts["ambiguity"]) if facts["ambiguity"] else ""),
                  next_action="Ask the Owner: " + question + " Only their exact user_direct confirmation permits internal owner-handoff using these fresh facts; do not run orca orchestration run-use by hand.",
                  verified={**work["verified"], "run": run_id, "coordinator": run["coordinator_handle"],
                            "generation": run["consumer_generation"], "owner_code": previous["code"],
                            "caller_runtime": own["runtime"]})
    return result


def nonowner_detection(project: Path, objective: str, state: dict | None, owner: str, *, port=None, current=None) -> dict | None:
    """Use the authority port's actual caller; supplied owner strings grant nothing."""
    from .ledger import _run_references
    from .operations import OrcaPort
    if not state or not state.get("owner"):
        return None
    # Recorded-owner calls keep precisely their existing reads and R98 behavior.
    if current is None and os.environ.get("ORCA_TERMINAL_HANDLE") == state["owner"]:
        return None
    port = port or OrcaPort(project)
    if current is None:
        try:
            current = port.read_native(owner, authority_runs=tuple(sorted(_run_references(state))))
        except PodError:
            current = {"caller": os.environ.get("ORCA_TERMINAL_HANDLE")}
    if "caller" not in current or current["caller"] == state["owner"]:
        return None
    return detect(project, objective, state, port=port, caller=current["caller"])


def refuse_nonowner(project: Path, objective: str, state: dict | None, owner: str, *, code="native_authority_unverified", port=None, current=None) -> None:
    verdict = nonowner_detection(project, objective, state, owner, port=port, current=current)
    if verdict is not None:
        raise PodError(code, "Caller is not the recorded owner. " + verdict["message"],
                       {"owner_handoff": verdict, "next_action": verdict["next_action"]})


def owner_handoff(project: Path, objective: str, *, owner: str, decision: object, port) -> dict:
    from .ledger import (_lock, _path, _read, _write, _require_open, CONTINUITY_HISTORY)
    from .orca import current_run, mutate_command, terminal_identity
    value = exact(decision, DECISION, DECISION, name="handoff_decision")
    bounded_text(value["instruction"], name="instruction", limit=1024)
    if value["provenance"] != "user_direct":
        raise PodError("handoff_decision_mismatch", "Only an exact direct Owner confirmation permits this handoff")
    path = _path(project, objective)

    def refusal(verdict, code="owner_handoff_refused"):
        return PodError(code, verdict["message"], {"owner_handoff": verdict, "next_action": verdict["next_action"]})

    def abort(entry, reason, state):
        entry.update(state="aborted", abort_reason=reason)
        _write(path, state)

    # Keep the objective lock through the single native mutation and authoritative re-read.
    with _lock(path):
        state = _read(path)
        _require_open(state)
        if state.get("checkpoint") is None or owner != os.environ.get("ORCA_TERMINAL_HANDLE"):
            raise PodError("native_authority_unverified", "Handoff needs this live caller and a recorded objective")
        pending = unfinished(state)
        verdict = detect(project, objective, state, port=port)
        if any(value[key] != verdict["facts"][key] for key in FACTS):
            raise refusal({**verdict, "message": "The confirmation differs from fresh objective, owner, Run, generation, runtime or ambiguity facts. Nothing was written."}, "handoff_decision_mismatch")
        if pending and pending["state"] == "pending":
            holder = pending["decision"]["to_owner"]
            if holder != owner:
                if terminal_identity(holder)["state"] != "lost":
                    raise refusal(verdict)
                # Only the operation can terminate a definitely lost attempt.
                abort(pending, "pending caller handle stale or gone", state)
            elif verdict["status"] != "handoff_available":
                if verdict["definitive"]:
                    abort(pending, verdict["condition"], state)
                raise refusal(verdict)
        if verdict["status"] != "handoff_available":
            raise refusal(verdict, "runtime_continuity_full" if verdict["condition"] == "history_full" else "owner_handoff_refused")
        if pending and pending["state"] == "pending" and pending["decision"] != value:
            raise refusal({**verdict, "message": "The pending handoff requires its original exact confirmation."}, "handoff_decision_mismatch")
        record = state["checkpoint"].get("continuity") or {"binding": None, "history": []}
        if pending is None:
            if len(record["history"]) >= CONTINUITY_HISTORY:
                raise PodError("runtime_continuity_full", "Handoff history is full; continue in a successor objective", {"limit": CONTINUITY_HISTORY})
            pending = {"provenance": "owner_handoff", "state": "pending", "from_runtime": value["recorded_runtime"],
                       "to_runtime": value["current_runtime"], "at": datetime.now(timezone.utc).isoformat(),
                       "verified": verdict["verified"], "decision": dict(value), "abort_reason": None, "completion": None}
            record["history"].append(pending)
            state["checkpoint"]["continuity"] = record
            _write(path, state)
        elif pending["state"] == "aborted":
            pending.update(state="pending", decision=dict(value), verified=verdict["verified"],
                           from_runtime=value["recorded_runtime"], to_runtime=value["current_runtime"],
                           at=datetime.now(timezone.utc).isoformat())
            _write(path, state)
        failed = None
        if verdict["condition"] == "fresh":
            try:
                receipt = mutate_command(["orchestration", "run-use", "--id", value["run"], "--json"], accept_exit=(0, 1))
                if receipt.get("error") or receipt.get("exit") != 0:
                    failed = "run-use refused"
            except PodError:
                failed = "run-use outcome unknown"
            if failed:
                try:
                    after = current_run()
                except PodError:
                    after = None
                old = {"id": value["run"], "coordinator_handle": value["from_owner"],
                       "consumer_generation": value["recorded_generation"]}
                if after and after["runtime"] == value["current_runtime"] and after["run"] == old:
                    abort(pending, failed, state)
                raise PodError("owner_handoff_refused", failed + "; re-read status and rerun the same handoff confirmation; no work was changed",
                               {"handoff_state": pending["state"]})
        final = detect(project, objective, state, port=port)
        if final["status"] != "handoff_available" or final["condition"] != "resume":
            if final["definitive"]:
                abort(pending, final["condition"], state)
            raise refusal(final)
        if any(final["facts"][key] != value[key] for key in FACTS):
            raise refusal({**final, "message": "Handoff facts changed after the rebind; the record stays pending."})
        run = final["verified"]
        state["owner"] = owner
        for row in state["admissions"].values():
            row.update(owner=owner, runtime=value["current_runtime"])
        for ref in state["checkpoint"].get("native_refs", []):
            ref["runtime"] = value["current_runtime"]
        record["binding"] = {"run": value["run"], "coordinator": owner, "generation": run["generation"]}
        pending.update(state="done", completion=run)
        _write(path, state)
        return {"status": "handed_off", "objective": objective, "owner_handoff": pending}

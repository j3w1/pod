"""Derived coordinator continuation after a verified owner-authorized Orca window.

An owner-authorized maintenance window that stops or restarts Orca ends every live terminal. This module lets a new
coordinator in the objective's own worktree continue an open objective whose recorded coordinator that window ended,
with no per-objective owner decision. The host's authorization verifier (the existing relay verifier) proves the
window; Orca proves the loss, the Run and every admission's identity at the current runtime; Git proves the place.

The continuation is derived authority movement only. It records a `window_continuation` continuity row, never a
`user_direct` decision, and it changes no criterion, consent, standing authorization, Governor row, admission state or
evidence. Every refusal is a task for the project lead, never an owner question.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from .errors import PodError
from .util import digest

ACTION = "orca_window"
PROVENANCE = "window_continuation"
SCHEMA = "ce-orca-window/v1"
LOSS_BASIS = "live_at_final_prestop_read"
KINDS = ("stop", "restart")
HOST_CONDITIONS = ("window_not_found", "window_ambiguous", "window_not_authorized", "window_not_session_ending",
                   "window_authority_stale", "loss_unverified")
COMMAND = "pod continue --objective {} --json"
LEAD = "A task for the project lead, never an owner question: "
ROW = {"provenance", "state", "from_runtime", "to_runtime", "at", "from_owner", "to_owner", "run", "window",
       "admissions", "abort_reason"}
RUN_FIELDS = {"id", "before", "after"}
ADMISSION_FIELDS = {"id", "state", "verdict", "reason", "runtime", "status", "outcome"}
VERDICTS = ("settled", "unknown", "absent")
DISCOVERY_CALLS = 16
DISCOVERY_SECONDS = 20.0

# Each lead-task condition names what to resolve; none asks the owner.
NEXT = {
    "objective_unknown": "run the command in the objective's own checkout; records are found by this checkout's "
                         "Git directory, and open objectives cannot cross a repository move",
    "objective_continued": "read the successor objective; this record accepts no coordinator",
    "objective_retired": "read it with pod status --objective ID; it accepts no coordinator",
    "objective_closed": "read it with pod status --objective ID; it accepts no coordinator",
    "recorded_owner": "this terminal is the recorded coordinator; continue with ordinary Pod writes",
    "already_continued": "this terminal already continued the objective; continue with ordinary Pod writes",
    "handoff_pending": "finish or abort the pending owner handoff first; window continuation never overrides it",
    "recorded_runtime_unreadable": "the record has no single Run and runtime; inspect it with pod status --objective ID",
    "generation_unrecorded": "the record has no recorded Run generation; use an existing recovery path",
    "worktree_unrecorded": "the record names no worktree; use an existing recovery path",
    "caller_not_live": "run the command in a live Orca terminal in the objective's recorded worktree",
    "runtime_unchanged": "Orca's runtime has not changed since the record; this is not a window loss",
    "runtime_unreadable": "Orca's current runtime could not be read; retry in a live Orca terminal",
    "worktree_changed": "run the command in the objective's recorded worktree, on its recorded branch; a moved "
                        "repository or changed worktree cannot be continued",
    "owner_live": "the recorded coordinator is still live; continue there",
    "owner_unreadable": "Orca could not read the recorded coordinator's handle; retry the same command",
    "coordinator_lost_outside_window": "Orca shows the recorded coordinator closed after the window; use the "
                                       "existing paths (evidence closure, successor claim or owner handoff)",
    "run_missing": "Orca no longer has the objective's Run; Pod never creates a replacement Run; use an existing "
                   "recovery path",
    "run_unverified": "the objective's Run could not be read; retry the same command",
    "run_rebound_elsewhere": "the Run was rebound outside a Pod continuation; lineage is unproven; use an existing "
                             "recovery path and never run orca orchestration run-use by hand",
    "caller_bound_elsewhere": "this terminal coordinates another Run; start the continuation from a terminal bound "
                              "to no Run, or to this objective's Run",
    "pending_other_caller": "another terminal holds the pending continuation; finish it there, or wait until Orca "
                            "reports that terminal stale or gone",
    "caller_is_worker": "a worker terminal cannot coordinate; use another terminal in the recorded worktree",
    "workers_unreadable": "the Run's worker list could not be read completely; retry the same command",
    "run_shared": "another open objective records this Run; resolve the shared Run before continuing",
    "scope_unreadable": "another objective record could not prove whether it uses this Run; resolve it first",
    "peer_history_unverifiable": "a peer's retained history cannot prove its closure; resolve it first",
    "identity_differs": "a bound admission's native identity differs from its record; differing identities cannot be "
                        "continued; use an existing recovery path",
    "history_full": "the eight-entry continuity history is full; continue in a successor objective",
    "binding_unstable": "Orca's Run binding or runtime changed during the check; retry the same command",
    "window_verifier_unavailable": "configure the host's authorization_verifier; Pod cannot verify a window without it",
    "window_unverified": "the host verifier gave no usable answer; repair it and retry the same command",
    "window_not_found": "the host records no owner-authorized session-ending window that ended this runtime",
    "window_ambiguous": "the host records more than one window for this runtime; resolve it on the host",
    "window_not_authorized": "the host does not record this window as owner-authorized",
    "window_not_session_ending": "the host shows no session-ending stop or restart for this runtime (for example a "
                                 "drain or hold)",
    "window_authority_stale": "the window's owner authorization is no longer current on the host",
    "loss_unverified": "the host did not see this coordinator live when Orca stopped, so its loss is not attributed "
                       "to the window; use the existing paths",
    "window_changed": "the host's window answer differs from the pending continuation's; inspect the host record",
    "window_already_used": "this window's continuation receipt binds a different transition; inspect the record",
}


def _refusal(condition: str, message: str, *, verdict: dict | None = None) -> PodError:
    action = LEAD + NEXT[condition] + "."
    return PodError("window_continuation_refused", f"Window continuation refused ({condition}): {message}",
                    {"condition": condition, "next_action": action, **({"window_continuation": verdict} if verdict else {})})


# --------------------------------------------------------------------------- stored rows

def _text(value: object, limit: int = 512) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _validate_window(value: object) -> None:
    from .relay import validate_verdict
    verdict = validate_verdict(value)
    if verdict["action"] != ACTION or verdict["verified"] is not True:
        raise ValueError("window verdict")


def validate_entry(row: dict) -> None:
    """Stored window continuation rows; malformed or unknown rows are unsupported, never repaired."""
    try:
        if set(row) != ROW or row["provenance"] != PROVENANCE or row["state"] not in ("pending", "done", "aborted"):
            raise ValueError("row")
        for key in ("from_runtime", "to_runtime", "at", "from_owner", "to_owner"):
            if not _text(row[key]):
                raise ValueError(key)
        run = row["run"]
        if not isinstance(run, dict) or set(run) != RUN_FIELDS or not _text(run["id"]):
            raise ValueError("run")
        for key in ("before", "after"):
            part = run[key]
            if part is None and key == "after" and row["state"] != "done":
                continue
            if (not isinstance(part, dict) or set(part) != {"coordinator", "generation"}
                    or part["coordinator"] is not None and not _text(part["coordinator"], 256)
                    or type(part["generation"]) is not int or part["generation"] < 0):
                raise ValueError(key)
        _validate_window(row["window"])
        admissions = row["admissions"]
        if not isinstance(admissions, list) or len(admissions) > 256:
            raise ValueError("admissions")
        for item in admissions:
            if (not isinstance(item, dict) or set(item) != ADMISSION_FIELDS or not _text(item["id"], 128)
                    or item["verdict"] not in VERDICTS
                    or any(item[key] is not None and not _text(item[key]) for key in ("state", "reason", "runtime",
                                                                                     "status", "outcome"))):
                raise ValueError("admission")
        if row["abort_reason"] is not None and not _text(row["abort_reason"]):
            raise ValueError("abort")
        if (row["state"] == "aborted") != (row["abort_reason"] is not None):
            raise ValueError("abort state")
        if "decision" in row or "instruction" in row:
            raise ValueError("decision")
    except (KeyError, TypeError, ValueError, PodError):
        raise PodError("state_unsupported", "Window continuation history is malformed") from None


def _history(state: dict) -> list:
    return ((state.get("checkpoint") or {}).get("continuity") or {}).get("history") or []


def unfinished(state: dict) -> dict | None:
    rows = [row for row in _history(state) if row.get("provenance") == PROVENANCE and row.get("state") != "done"]
    if len(rows) > 1:
        raise PodError("state_unsupported", "More than one unfinished window continuation")
    return rows[0] if rows else None


def completed(state: dict) -> list[dict]:
    return [row for row in _history(state) if row.get("provenance") == PROVENANCE and row.get("state") == "done"]


# --------------------------------------------------------------------------- host window verdict

def _request(project: Path, objective: str, state: dict, facts: dict) -> dict:
    from .relay import SCHEMA as RELAY
    from .standing import _target
    return {"schema": RELAY, "reference": "orca-runtime:" + facts["recorded_runtime"], "action": ACTION,
            "objective": objective, "candidate": None, "target": _target(project, state), "pr": None,
            ACTION: {"old_runtime": facts["recorded_runtime"], "current_runtime": facts["current_runtime"],
                     "coordinator": facts["from_owner"]}}


def verify_window(project: Path, objective: str, state: dict, facts: dict) -> dict:
    """The host's verified window verdict, or a lead-task refusal naming why not. Read-only on both sides."""
    from . import relay
    from .config import effective
    command = effective(project)["preferences"].get("authorization_verifier")
    if not command:
        raise _refusal("window_verifier_unavailable", "no authorization_verifier is configured.")
    try:
        request = _request(project, objective, state, facts)
    except PodError as exc:
        raise _refusal("window_unverified", f"the repository target could not be derived ({exc.code}).") from None
    try:
        verdict = relay.verify(command, request)
    except PodError as exc:
        condition = (exc.detail or {}).get("condition")
        if exc.code == "relay_verifier_denied" and condition in HOST_CONDITIONS:
            raise _refusal(condition, "the host denied the window.") from None
        if exc.code == "relay_verifier_unconfigured":
            raise _refusal("window_verifier_unavailable", "no authorization_verifier is configured.") from None
        raise _refusal("window_unverified", f"the host verifier refused ({exc.code}).") from None
    window = verdict["window"]
    def moment(text):
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    if (window["old_runtime"] != facts["recorded_runtime"]
            or not moment(verdict["decided_at"]) <= moment(window["stopped_at"]) <= datetime.now(timezone.utc)):
        raise _refusal("window_unverified", "the host's window facts are inconsistent with this record.")
    return verdict


def stable(verdict: dict, facts: dict, repo_key: str) -> dict:
    """What a receipt binds: the window effect and the original transition, never the current runtime or caller."""
    return {"action": ACTION, "repo_key": repo_key, "objective": facts["objective"],
            "authorization": verdict["single_use"], "operation": verdict["window"]["operation"],
            "effect": verdict["window"]["effect"], "old_runtime": facts["recorded_runtime"],
            "coordinator": facts["from_owner"], "run": facts["run"], "generation": facts["recorded_generation"]}


def _receipt(project: Path, value: dict, *, consume: bool) -> None:
    """One non-evicting receipt per window effect, repository and objective, through the shared relay store."""
    from .relay import check_use
    name = "\n".join((ACTION, value["repo_key"], value["objective"], value["authorization"]))
    transition = digest({field: value[field] for field in ("objective", "repo_key", "coordinator", "run",
                                                           "generation", "old_runtime")})
    try:
        check_use(project, {"single_use": name, **value}, consume=consume, transition=transition)
    except PodError as exc:
        if exc.code == "relay_authorization_reused":
            raise _refusal("window_already_used", "the receipt for this window and objective binds another "
                                                  "transition.") from None
        raise


# --------------------------------------------------------------------------- native reads

def _admissions(state: dict, *, runtime: str, port) -> tuple[list[dict], list[str]]:
    """Fresh readback of every outstanding admission; identities compared exactly, nothing settled or stamped."""
    from .ledger import _native_assignment_settled
    from .orca import worker_rows
    rows, differs = [], []
    fleets: dict[str, object] = {}
    for key in sorted(state.get("admissions", {})):
        row = state["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        item = {"id": key, "state": row["state"], "verdict": "unknown", "reason": None, "runtime": None,
                "status": None, "outcome": None}
        if row["state"] == "unresolved":
            item["reason"] = "unresolved attempt"
        elif row["state"] == "reserved":
            run = row["run_id"]
            if run not in fleets:
                try:
                    fleets[run] = worker_rows(run)
                except PodError as exc:
                    fleets[run] = exc.code
            fleet = fleets[run]
            if isinstance(fleet, str):
                item["reason"] = f"exact Run/Task lookup unavailable ({fleet})"
            elif fleet["runtime"] != runtime or fleet.get("complete") is not True:
                item["reason"] = "exact Run/Task lookup incomplete at the current runtime"
            else:
                item["runtime"] = runtime
                found = [worker for worker in fleet["workers"] if isinstance(worker, dict) and (
                    worker.get("taskId") == row["task_id"] or isinstance(worker.get("projection"), dict)
                    and worker["projection"].get("taskId") == row["task_id"])]
                if found:
                    item["reason"] = "Dispatch found without a recorded native binding"
                else:
                    item.update(verdict="absent", reason="no Dispatch for the reserved Task")
        else:
            binding = row.get("native_binding") or {}
            try:
                shown = port.show_worker(binding["dispatchId"])
            except (PodError, KeyError) as exc:
                item["reason"] = "worker read unavailable (" + getattr(exc, "code", "unbound") + ")"
                rows.append(item)
                continue
            result = shown.get("result") if isinstance(shown, dict) else None
            parts = [result.get(name) if isinstance(result, dict) else None for name in ("dispatch", "projection", "worker")]
            if shown.get("runtime") != runtime or not all(isinstance(part, dict) for part in parts):
                item["reason"] = "worker read incomplete at the current runtime"
            else:
                dispatch, projection, worker = parts
                if (dispatch.get("id"), dispatch.get("runId"), dispatch.get("taskId"), projection.get("id"),
                        projection.get("dispatchId"), projection.get("runId"), projection.get("taskId"),
                        worker.get("dispatchId"), worker.get("worktreeId")) != (
                        binding["dispatchId"], binding["runId"], binding["taskId"], binding["workerId"],
                        binding["dispatchId"], binding["runId"], binding["taskId"], binding["dispatchId"],
                        binding["worktreeId"]):
                    differs.append(f"admission {key}")
                    continue
                status, outcome = dispatch.get("status"), projection.get("outcome")
                item.update(runtime=runtime, status=status if _text(status, 64) else None,
                            outcome=outcome if _text(outcome, 64) else None)
                if _native_assignment_settled(shown):
                    item.update(verdict="settled", reason="terminal Dispatch outcome read")
                else:
                    item["reason"] = "not settled at readback"
        rows.append(item)
    return rows, differs


def _cwd_toplevel() -> str | None:
    try:
        completed = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                                   timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


# --------------------------------------------------------------------------- detection

def detect(project: Path, objective: str, state: dict, *, port, caller: str | None = None,
           objective_only: bool = False, verify: bool = True) -> dict:
    """Fresh facts only; no writes and no native mutation.

    `objective_only` answers whether the objective is eligible, for listings, without caller checks. Otherwise the
    caller must be the live terminal in the recorded worktree that would run the continuation.
    """
    from .ledger import _run_references, CONTINUITY_HISTORY
    from .orca import current_run, run_show, terminal_identity, worker_rows
    from .handoff import run_scope, unfinished as handoff_unfinished
    caller = os.environ.get("ORCA_TERMINAL_HANDLE") if caller is None and not objective_only else caller
    checkpoint = state.get("checkpoint") or {}
    owner = state.get("owner")
    binding = (checkpoint.get("continuity") or {}).get("binding")
    facts = {"objective": objective, "from_owner": owner, "to_owner": caller, "run": None,
             "recorded_generation": None, "recorded_runtime": None, "current_runtime": None}
    result = {"status": "window_unavailable", "condition": None, "facts": facts, "command": None, "cwd": None,
              "definitive": False}

    def fail(condition: str, message: str, *, definitive: bool = False) -> dict:
        result.update(condition=condition, message=message, definitive=definitive,
                      next_action=LEAD + NEXT[condition] + ".")
        return result

    if isinstance(state.get("continued_by"), dict):
        return fail("objective_continued", "a successor objective continued this objective.", definitive=True)
    if isinstance(state.get("historical_retirement"), dict):
        return fail("objective_retired", "the owner's historical retirement ended this objective.", definitive=True)
    from .evidence_closure import present
    from .ledger import objective_root
    if present(objective_root(project, objective)):
        return fail("objective_closed", "the objective was closed by evidence.", definitive=True)
    if checkpoint.get("closure"):
        return fail("objective_closed", "the objective is closed.", definitive=True)
    try:
        refs = _run_references(state)
    except PodError:
        refs = {}
    runtimes = set(refs.values())
    if not isinstance(binding, dict) and len(refs) != 1 or len(runtimes) != 1 or not owner:
        return fail("recorded_runtime_unreadable", "the record has no single recorded Run, runtime and coordinator.")
    if not isinstance(binding, dict):
        return fail("generation_unrecorded", "the record has no recorded Run generation.")
    run_id, generation = binding["run"], binding["generation"]
    facts.update(run=run_id, recorded_generation=generation, recorded_runtime=next(iter(runtimes)))
    workspace = checkpoint.get("worktree")
    if not isinstance(workspace, dict) or not _text(workspace.get("path"), 4096):
        return fail("worktree_unrecorded", "the record names no worktree.")
    result.update(command=COMMAND.format(objective), cwd=workspace["path"])
    if not objective_only and caller == owner:
        return fail("recorded_owner", "this terminal is the recorded coordinator.")
    handoff = handoff_unfinished(state)
    if handoff is not None and handoff["state"] == "pending":
        return fail("handoff_pending", "an owner handoff is pending.")
    pending = unfinished(state)
    if not objective_only:
        if not caller:
            return fail("caller_not_live", "this process is not in a live Orca terminal.")
        own = terminal_identity(caller)
        if own["state"] != "live":
            return fail("caller_not_live", "Orca cannot prove this terminal live.")
        try:
            first = current_run()
        except PodError as exc:
            return fail("runtime_unreadable", f"the current Run binding could not be read ({exc.code}).")
        if own["runtime"] != first["runtime"]:
            return fail("binding_unstable", "the terminal and Run reads came from different runtimes.")
        runtime = first["runtime"]
    else:
        try:
            runtime = run_show(run_id)["runtime"]
        except PodError as exc:
            code = (exc.detail or {}).get("native_code")
            if code == "run_not_found":
                return fail("run_missing", "Orca reports the objective's Run not found.", definitive=True)
            return fail("run_unverified", f"the objective's Run could not be read ({exc.code}).")
        first = None
    facts["current_runtime"] = runtime
    if runtime in runtimes:
        return fail("runtime_unchanged", "Orca's runtime is the recorded one.")
    if not objective_only:
        from .operations import _check_worktree_binding
        try:
            _check_worktree_binding(project, workspace)
        except PodError:
            return fail("worktree_changed", "the repository, branch or worktree differs from the record.", definitive=True)
        top = _cwd_toplevel()
        if top is None or Path(top).resolve() != Path(workspace["path"]).resolve():
            return fail("worktree_changed", "this terminal does not run in the recorded worktree.", definitive=True)
    previous = terminal_identity(owner)
    if previous["state"] == "live":
        return fail("owner_live", "Orca reports the recorded coordinator live.", definitive=True)
    if previous.get("exited"):
        return fail("coordinator_lost_outside_window", "Orca shows the recorded coordinator's terminal closed at the "
                    "current runtime.", definitive=True)
    if previous["state"] != "lost":
        return fail("owner_unreadable", "Orca could not read the recorded coordinator's handle.")
    try:
        shown = run_show(run_id)
    except PodError as exc:
        if (exc.detail or {}).get("native_code") == "run_not_found":
            return fail("run_missing", "Orca reports the objective's Run not found.", definitive=True)
        return fail("run_unverified", f"the objective's Run could not be read ({exc.code}).")
    if shown["runtime"] != runtime or not shown["coordinator_known"]:
        return fail("run_unverified", "the Run read does not prove its coordinator at the current runtime.")
    holder, held = shown["coordinator_handle"], shown["consumer_generation"]
    holder_pending = pending is not None and pending["state"] == "pending"
    if pending is not None and holder_pending and pending["to_owner"] != caller:
        other = terminal_identity(pending["to_owner"])
        if other["state"] != "lost":
            return fail("pending_other_caller", "another terminal holds the pending continuation.")
    branch = None
    if holder in (owner, None) and held == generation:
        branch = "fresh"
    elif holder_pending and holder == pending["to_owner"] and held > generation:
        branch = "resume" if holder == caller else "replace"
    if branch is None:
        return fail("run_rebound_elsewhere", "the Run's coordinator or generation is outside a Pod continuation.",
                    definitive=True)
    if not objective_only:
        mine = first["run"]
        if branch == "resume":
            if not isinstance(mine, dict) or mine != {"id": run_id, "coordinator_handle": caller,
                                                     "consumer_generation": held}:
                return fail("caller_bound_elsewhere", "this terminal's current Run is not the continued Run.",
                            definitive=True)
        elif mine is not None and (mine.get("id") != run_id or mine.get("coordinator_handle") not in (owner, holder)):
            return fail("caller_bound_elsewhere", "this terminal coordinates another Run.", definitive=True)
        if any((row.get("native_binding") or {}).get("terminalHandle") == caller
               for row in state["admissions"].values()):
            return fail("caller_is_worker", "this terminal is a worker of this objective.", definitive=True)
    try:
        fleet = worker_rows(run_id)
    except PodError as exc:
        return fail("workers_unreadable", f"the Run's worker list could not be read ({exc.code}).")
    if fleet["runtime"] != runtime or fleet.get("complete") is not True:
        return fail("workers_unreadable", "the worker list does not prove complete current-runtime scope.")
    if caller and any(part.get(key) == caller for row in fleet["workers"] if isinstance(row, dict)
                      for part in (row, row.get("worker"), row.get("projection")) if isinstance(part, dict)
                      for key in ("terminalHandle", "agentTerminalHandle")):
        return fail("caller_is_worker", "this terminal has a worker binding on this Run.", definitive=True)
    scope = run_scope(project, objective, run_id)
    if scope["objectives"]:
        result["other_objectives"] = scope["objectives"][:8]
        return fail("run_shared", "another open objective records this Run: " + ", ".join(scope["objectives"][:8]) + ".",
                    definitive=True)
    if scope["unreadable"]:
        return fail("scope_unreadable", f"{scope['unreadable']} objective record(s) could not prove Run membership.")
    if scope.get("unverifiable"):
        return fail("peer_history_unverifiable", "peer history cannot prove closure: "
                    + ", ".join(scope["unverifiable"][:8]) + ".")
    admissions, differs = _admissions(state, runtime=runtime, port=port)
    if differs:
        result["differs"] = differs[:16]
        return fail("identity_differs", "a bound admission identity differs: " + ", ".join(differs[:4]) + ".",
                    definitive=True)
    if pending is None and len(_history(state)) >= CONTINUITY_HISTORY:
        return fail("history_full", "the continuity history is full.")
    result.update(branch=branch, admissions=admissions,
                  run={"id": run_id, "before": {"coordinator": holder, "generation": held}})
    if verify:
        try:
            result["window"] = verify_window(project, objective, state, facts)
        except PodError as exc:
            return fail(exc.detail["condition"], str(exc).split(": ", 1)[-1])
    if not objective_only:
        try:
            after = current_run()
        except PodError:
            after = None
        if after != first:
            return fail("binding_unstable", "the Run binding or runtime changed during the check.")
    result.update(status="window_available", condition=branch if not objective_only else "eligible",
                  message=("The recorded coordinator was lost to a verified owner-authorized Orca window; "
                           "a new coordinator in the recorded worktree continues the objective with derived authority."),
                  next_action=f"In the recorded worktree {workspace['path']}, run {COMMAND.format(objective)}; "
                              "no owner decision is needed.")
    return result


# --------------------------------------------------------------------------- the transition

def continue_objective(project: Path, objective: str, *, owner: str, port) -> dict:
    """Pending, then run-use, then done, under the objective lock; replays finish an interrupted attempt."""
    from .ledger import _lock, _path, _read, _write, _require_open
    from .orca import current_run, mutate_command
    from .history import prepare_context
    from .ledger import read
    if owner != os.environ.get("ORCA_TERMINAL_HANDLE"):
        raise _refusal("caller_not_live", "the request's terminal is not this process's terminal.")
    if read(project, objective) is None:
        # Checked before locking, so an unknown name leaves no state directory behind.
        raise _refusal("objective_unknown", "this checkout records no such objective.")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state.get("checkpoint") is None:
            raise _refusal("objective_unknown", "this checkout records no such objective.")
        # Continuation moves coordination only: a successor an incomplete claim reserves may change coordinator so
        # that the new one finishes the claim (R118).
        _require_open(state, reserved=False)
        from .evidence_closure import refuse_objective
        refuse_objective(project, objective)
        done = completed(state)
        if state.get("owner") == owner and done and done[-1]["to_owner"] == owner:
            return {"status": "already_continued", "objective": objective, "window_continuation": done[-1]}
        pending = unfinished(state)
        verdict = detect(project, objective, state, port=port)
        if verdict["status"] != "window_available":
            if pending is not None and pending["state"] == "pending" and verdict["definitive"] \
                    and verdict["condition"] not in ("pending_other_caller",):
                pending.update(state="aborted", abort_reason=verdict["condition"])
                _write(path, state)
            raise _refusal(verdict["condition"], verdict["message"], verdict=_public(verdict))
        facts = verdict["facts"]
        repo_key = state["checkpoint"]["worktree"]["repo_key"]
        bound = stable(verdict["window"], facts, repo_key)
        if pending is not None and stable(pending["window"], {**facts, "from_owner": pending["from_owner"]},
                                          repo_key) != bound:
            raise _refusal("window_changed", "the host's answer differs from the pending continuation.",
                           verdict=_public(verdict))
        if pending is not None and pending["state"] == "pending" and pending["to_owner"] != owner:
            # Detection proved that terminal stale or gone; only this operation ends its attempt.
            pending.update(state="aborted", abort_reason="pending caller handle stale or gone")
        now = datetime.now(timezone.utc).isoformat()
        record = state["checkpoint"].get("continuity") or {"binding": None, "history": []}
        if pending is None or pending["state"] == "aborted":
            entry = {"provenance": PROVENANCE, "state": "pending", "from_runtime": facts["recorded_runtime"],
                     "to_runtime": facts["current_runtime"], "at": now, "from_owner": facts["from_owner"],
                     "to_owner": owner, "run": {**verdict["run"], "after": None}, "window": verdict["window"],
                     "admissions": verdict["admissions"], "abort_reason": None}
            if pending is not None and pending["state"] == "aborted" and pending is unfinished(state):
                # A replacement attempt reuses the aborted row's position; its reason stays in the result.
                index = record["history"].index(pending)
                record["history"][index] = entry
            else:
                from .ledger import CONTINUITY_HISTORY
                if len(record["history"]) >= CONTINUITY_HISTORY:
                    raise _refusal("history_full", "the continuity history is full.")
                record["history"].append(entry)
            state["checkpoint"]["continuity"] = record
            # Both the pending and the done record must fit before the receipt is consumed.
            projected = _completed_state(deepcopy(state), entry=record["history"].index(entry), owner=owner,
                                         facts=facts, after={"coordinator": owner, "generation": 2 ** 31},
                                         admissions=verdict["admissions"])
            from .ledger import _validate_context
            _validate_context(projected)
            prepare_context(path, {**projected, "revision": projected["revision"] + 1})
            _receipt(project, bound, consume=True)
            _write(path, state)
            pending = entry
        else:
            _receipt(project, bound, consume=False)
        if verdict["branch"] in ("fresh", "replace"):
            failed = None
            try:
                receipt = mutate_command(["orchestration", "run-use", "--id", facts["run"], "--json"], accept_exit=(0, 1))
                if receipt.get("error") or receipt.get("exit") != 0:
                    failed = "run-use refused"
            except PodError:
                failed = "run-use outcome unknown"
            if failed:
                try:
                    after = current_run()
                except PodError:
                    after = None
                # Abort only on authoritative proof that the Run did not move; otherwise the row stays pending.
                if (after is not None and after["runtime"] == facts["current_runtime"] and
                        (after["run"] is None or after["run"]["coordinator_handle"] != owner)):
                    from .orca import run_show
                    try:
                        shown = run_show(facts["run"])
                    except PodError:
                        shown = None
                    if shown is not None and shown["coordinator_handle"] == verdict["run"]["before"]["coordinator"] \
                            and shown["consumer_generation"] == verdict["run"]["before"]["generation"]:
                        pending.update(state="aborted", abort_reason=failed)
                        _write(path, state)
                raise PodError("window_continuation_refused", failed + "; re-read status and rerun the same command; "
                               "no work was changed", {"condition": "run_use_failed", "continuation_state": pending["state"],
                                                       "next_action": LEAD + "rerun " + COMMAND.format(objective) + "."})
        final = detect(project, objective, state, port=port)
        if final["status"] != "window_available" or final["condition"] != "resume":
            if final["definitive"]:
                pending.update(state="aborted", abort_reason=final["condition"] or "unavailable")
                _write(path, state)
            raise _refusal(final["condition"], final["message"], verdict=_public(final))
        if stable(final["window"], {**final["facts"], "from_owner": pending["from_owner"]}, repo_key) != bound:
            raise _refusal("window_changed", "the host's answer changed during the continuation.")
        index = record["history"].index(pending)
        _completed_state(state, entry=index, owner=owner, facts=final["facts"],
                         after=final["run"]["before"], admissions=final["admissions"])
        _write(path, state)
        return {"status": "continued", "objective": objective, "window_continuation": record["history"][index]}


def _completed_state(state: dict, *, entry: int, owner: str, facts: dict, after: dict, admissions: list) -> dict:
    record = state["checkpoint"]["continuity"]
    row = record["history"][entry]
    state["owner"] = owner
    for admission in state["admissions"].values():
        admission.update(owner=owner, runtime=facts["current_runtime"])
    for ref in state["checkpoint"].get("native_refs", []):
        ref["runtime"] = facts["current_runtime"]
    record["binding"] = {"run": facts["run"], "coordinator": owner, "generation": after["generation"]}
    row.update(state="done", to_runtime=facts["current_runtime"], to_owner=owner,
               run={**row["run"], "after": dict(after)}, admissions=admissions)
    return state


def _public(verdict: dict) -> dict:
    keep = ("status", "condition", "message", "next_action", "facts", "command", "cwd", "branch", "definitive",
            "other_objectives", "differs")
    return {key: verdict[key] for key in keep if key in verdict}


# --------------------------------------------------------------------------- status

def status_block(project: Path, objective: str, state: dict, *, port=None) -> dict | None:
    """The block `pod status --objective` shows to a non-owner terminal; None when no window question arises."""
    from .operations import OrcaPort
    if not state or not state.get("owner") or os.environ.get("ORCA_TERMINAL_HANDLE") in (None, state["owner"]):
        return None
    verdict = detect(project, objective, state, port=port or OrcaPort(project))
    if verdict["condition"] in ("runtime_unchanged", "recorded_owner", "objective_continued", "objective_retired",
                                "objective_closed"):
        return None
    return _public(verdict)


def discover(rows: list[dict], *, project: Path) -> None:
    """Annotate open listing rows with objective-level eligibility, within a bounded budget. Read-only.

    One current-runtime read decides which rows need more: a row whose recorded runtime is Orca's current one has no
    window question and is left as it was. Eligibility is the objective's; the command still checks its caller.
    """
    import time
    from .ledger import read, _run_references
    from .operations import OrcaPort
    from .orca import current_run
    opened = [row for row in rows if row.get("phase") == "open"]
    if not opened:
        return
    unavailable = None
    if not os.environ.get("ORCA_TERMINAL_HANDLE"):
        unavailable = ("not_in_orca", "window continuation is checked only from a live Orca terminal")
    else:
        try:
            runtime = current_run()["runtime"]
        except PodError as exc:
            unavailable = ("orca_unreadable", f"Orca's current runtime could not be read ({exc.code})")
    if unavailable is not None:
        for row in opened:
            row["continuation"] = {"status": "unavailable", "condition": unavailable[0], "message": unavailable[1]}
        return
    deadline = time.monotonic() + DISCOVERY_SECONDS
    calls = 0
    for row in opened:
        try:
            state = read(project, row["objective"])
            runtimes = set(_run_references(state).values()) if state else set()
        except (PodError, KeyError, TypeError, OSError):
            continue
        if not state or not state.get("checkpoint") or runtimes == {runtime} or not runtimes:
            continue
        if calls >= DISCOVERY_CALLS or time.monotonic() >= deadline:
            row["continuation"] = {"status": "unchecked", "condition": "discovery_budget",
                                   "next_action": "pod status --objective " + row["objective"]}
            continue
        workspace = (state["checkpoint"].get("worktree") or {}).get("path")
        where = Path(workspace) if isinstance(workspace, str) and Path(workspace).is_dir() else project
        try:
            verdict = detect(where, row["objective"], state, port=OrcaPort(where), objective_only=True, verify=False)
            if verdict["status"] == "window_available":
                calls += 1
                verdict = detect(where, row["objective"], state, port=OrcaPort(where), objective_only=True)
        except PodError as exc:
            row["continuation"] = {"status": "unchecked", "condition": exc.code,
                                   "next_action": "pod status --objective " + row["objective"]}
            continue
        if verdict["condition"] in ("runtime_unchanged", "objective_continued", "objective_retired", "objective_closed"):
            continue
        row["continuation"] = {"status": "available" if verdict["status"] == "window_available" else "unavailable",
                               **{key: verdict[key] for key in ("condition", "message", "next_action", "command", "cwd")
                                  if key in verdict}}


# --------------------------------------------------------------------------- public command

def continue_command(project: Path, objective: str) -> dict:
    """`pod continue`: the supported step a new coordinator runs in the recorded worktree."""
    from .internal import run
    owner = os.environ.get("ORCA_TERMINAL_HANDLE")
    if not owner:
        error = _refusal("caller_not_live", "this process is not in a live Orca terminal.")
        return {"schema": "pod-cli/v5", "status": "refused", "objective": objective, **error.detail,
                "message": str(error)}
    try:
        result = run("window-continuation", {"project": str(project), "objective": objective, "owner": owner})
    except PodError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.code != "window_continuation_refused":
            raise
        return {"schema": "pod-cli/v5", "status": "refused", "objective": objective, "message": str(exc),
                **{key: detail[key] for key in ("condition", "next_action", "window_continuation",
                                                "continuation_state") if key in detail}}
    return {"schema": "pod-cli/v5", **result}


def render(result: dict) -> None:
    from .term import clean
    entry = result.get("window_continuation") if result["status"] in ("continued", "already_continued") else None
    if entry is not None:
        verb = "Continued" if result["status"] == "continued" else "Already continued"
        print(f"{verb} objective {clean(result['objective'])} after Orca window "
              f"{clean(entry['window']['window']['operation'])}: coordinator {clean(entry['from_owner'])} -> "
              f"{clean(entry['to_owner'])}, runtime {clean(entry['from_runtime'])} -> {clean(entry['to_runtime'])} "
              "(derived; no owner decision)")
        for item in entry["admissions"]:
            print(f"  Admission {clean(item['id'])}: {item['verdict']}"
                  + (f" ({clean(item['reason'])})" if item.get("reason") else ""))
        return
    print(f"Window continuation refused ({clean(str(result.get('condition')))}): {clean(result['message'])}")
    if result.get("next_action"):
        print(f"Next: {clean(result['next_action'])}")

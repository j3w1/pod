"""Compact objective policy/evidence state; Orca owns native lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterator

from .contracts import bound, fields, required, input_record, policy_mismatch
from .errors import PodError
from .history import load_context, prepare_context, publish_context
from . import gitio
from .installer import STATE_SCHEMAS, superseded_objectives, superseded_schemas
from .util import MAX_RECORD, bounded_text, digest, exact, explicit_home, native_home

ADMISSION_STATES = ("reserved", "bound", "unresolved", "closed", "deferred")
CONTEXT_SCHEMA = STATE_SCHEMAS["context"]
CHECKPOINT_SCHEMA = STATE_SCHEMAS["checkpoint"]
ADMISSION_SCHEMA = STATE_SCHEMAS["admission"]
# An obligation map lives in the checkpoint; the context record keeps its own bound.
CONTEXT_LIMIT = 4 * MAX_RECORD
_BINDING_FIELDS = {"runId", "taskId", "dispatchId", "workerId", "worktreeId", "terminalHandle"}
_CONTEXT_FIELDS = {"schema", "revision", "owner", "admissions", "checkpoint",
                   "interventions", "source_rejections", "constraints"}
# Optional succession links (R110) and historical retirement (R112); a record without them
# keeps its exact earlier shape.
_SUCCESSION_FIELDS = {"continued_by", "continues"}
_RETIREMENT_FIELD = "historical_retirement"

def state_root(project: Path | None = None) -> Path:
    override = explicit_home("POD_STATE_HOME")
    if override is not None:
        return override
    native_root = native_home("XDG_STATE_HOME", default=Path.home() / ".local" / "state",
                              project=project)
    # The validated native profile root may itself be a symlink. Anchor Pod's
    # owned state below its physical root so generic no-follow record checks can
    # still reject every redirect introduced beneath that boundary.
    return native_root.resolve(strict=False) / "pod"


def _record_exists(path: Path) -> bool:
    """Distinguish definite absence from an inaccessible state candidate."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PodError("unsafe_state", "State record availability cannot be proven") from exc


def objective_root(project: Path, objective: str) -> Path:
    from .github import repository_context
    return state_root(project) / record_name(project, repository_context(project), objective)


def record_name(project: Path, context: dict, objective: str) -> str:
    """The state directory name that binds one objective to its repository (or plain directory)."""
    if context["repo_key"] is None:
        return digest({"project": str(project.resolve()), "objective": objective})
    return digest({"repository": context["repo_key"], "objective": objective})


def _path(project: Path, objective: str) -> Path:
    return objective_root(project, objective) / "context.json"


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    cursor = path.parent
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise PodError("unsafe_state", "State ancestry is redirected")
        cursor = cursor.parent
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise PodError("unsafe_state", "State directory is redirected")
    fd = os.open(path.parent / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _empty() -> dict:
    return {"schema": CONTEXT_SCHEMA, "revision": 0, "owner": None,
            "admissions": {}, "checkpoint": None, "interventions": {},
            "source_rejections": {}, "constraints": []}


def binding_valid(binding: object) -> bool:
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
        return False
    for key in ("runId", "taskId", "dispatchId", "workerId", "worktreeId"):
        if not isinstance(binding.get(key), str) or not binding[key]:
            return False
    terminal = binding.get("terminalHandle")
    return terminal is None or isinstance(terminal, str) and bool(terminal)


def bound_assignments(state: dict | None, *, include_closed: bool = False) -> tuple[dict, ...]:
    if not isinstance(state, dict):
        return ()
    states = ("bound", "closed") if include_closed else ("bound",)
    return tuple(row for row in state.get("admissions", {}).values()
                 if isinstance(row, dict) and row.get("state") in states
                 and binding_valid(row.get("native_binding")))


_ADMISSION_FIELDS = {"schema", "state", "admission_id", "objective", "owner", "request",
                     "route_decision", "effective_evidence", "runtime", "request_uuid",
                     "run_id", "task_id", "plan_revision", "packet_id", "worktree", "reuse_of",
                     "native_binding", "recovery", "error", "failures", "created_at", "updated_at",
                     "serves", "role", "boundary", "map_revision", "candidate", "result",
                     "changed_paths", "boundary_exceeded", "disposition", "report", "binding",
                     "admitted_seq", "display", "retry_of", "retry_after"}


def _consumed_report(row: dict, runtimes: set[str]) -> dict | None:
    """Validate the immutable observation that already joined a settled Dispatch."""
    if row["report"] is None:
        return None
    from .records import report_record, _sha256
    fields = {"outcome", "status", "attempt", "observation", "observation_digest", "result_commit", "consumed_at"}
    value = exact(row["report"], fields, fields, name="consumed report")
    observation = report_record(value["observation"])
    bounded_text(value["consumed_at"], name="consumed_at")
    if value["result_commit"] is not None:
        bounded_text(value["result_commit"], name="result_commit")
    if (not binding_valid(row["native_binding"]) or not _sha256(value["observation_digest"])
            or value["status"] not in ("validated_observation", "reconciliation_required")
            or value["outcome"] != observation["outcome"] or value["attempt"] != observation["attempt"]
            or observation["attempt"] != row["native_binding"]["dispatchId"]
            or observation["assignment"] != row["packet_id"] or observation["candidate"] != row["candidate"]):
        raise PodError("state_unsupported", "Consumed report no longer joins its recorded assignment")
    if value["status"] == "validated_observation":
        binding = {key: row["native_binding"][key] for key in ("runId", "taskId", "dispatchId", "workerId")}
        # Rebinds preserve the observation and its original runtime in history.
        if not any(digest({"status": value["status"], "observation": observation,
                           "native_binding": {"runtime": runtime, **binding}}) == value["observation_digest"]
                   for runtime in runtimes | {row["runtime"]}):
            raise PodError("state_unsupported", "Consumed report observation identity changed")
    return value


def _validate_admission(key: str, row: object, *, runtimes: set[str] | None = None) -> None:
    if isinstance(row, dict) and row.get("schema") != ADMISSION_SCHEMA:
        raise PodError("state_unsupported",
                       f"Admission uses superseded schema {str(row.get('schema'))[:64]}; it is not converted")
    value = exact(row, _ADMISSION_FIELDS, _ADMISSION_FIELDS - {"admitted_seq", "display", "retry_of", "retry_after"}, name="admission")
    if value["admission_id"] != key:
        raise PodError("state_unsupported", "Admission identity or schema is unsupported")
    if "admitted_seq" in value and (type(value["admitted_seq"]) is not int or value["admitted_seq"] < 1):
        raise PodError("state_unsupported", "Admission sequence must be a positive integer")
    from .obligations import ROLES
    if (not isinstance(value["serves"], list) or not value["serves"] or value["role"] not in ROLES
            or not isinstance(value["boundary"], dict) or type(value["map_revision"]) is not int):
        raise PodError("state_unsupported", "Admission obligation binding is malformed")
    if value["changed_paths"] is not None and not isinstance(value["changed_paths"], list):
        raise PodError("state_unsupported", "Admission result is malformed")
    if value["state"] not in ADMISSION_STATES:
        raise PodError("state_unsupported", "Admission state is unsupported")
    for field in ("objective", "owner", "run_id", "task_id", "plan_revision", "packet_id", "worktree", "runtime"):
        if not isinstance(value[field], str) or not value[field]:
            raise PodError("state_unsupported", "Admission binding is incomplete")
    if not all(isinstance(value[key], dict) for key in ("request", "route_decision", "effective_evidence", "recovery")):
        raise PodError("state_unsupported", "Admission evidence is malformed")
    if not isinstance(value["failures"], list) or len(value["failures"]) > 16:
        raise PodError("state_unsupported", "Admission failures are malformed")
    if value["request_uuid"] is not None and not isinstance(value["request_uuid"], str):
        raise PodError("state_unsupported", "Request UUID is malformed")
    if value["reuse_of"] is not None and not isinstance(value["reuse_of"], str):
        raise PodError("state_unsupported", "Reuse identity is malformed")
    if "retry_of" in value:
        parent = value["recovery"].get("retry_of_dispatch_id")
        from .orca import _argument
        if (not isinstance(value["retry_of"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["retry_of"])
                or value["retry_of"] == key or value["reuse_of"] is not None or not _argument(parent)
                or admission_identity(objective=value["objective"], run_id=value["run_id"],
                                      task_id=value["task_id"], retry_of_dispatch_id=parent,
                                      retry_after=value.get("retry_after")) != key):
            raise PodError("state_unsupported", "Retry parent or attempt identity is malformed")
    elif "retry_of_dispatch_id" in value["recovery"]:
        raise PodError("state_unsupported", "Retry Dispatch has no parent admission")
    if "retry_after" in value and ("retry_of" not in value
            or not isinstance(value["retry_after"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["retry_after"])
            or value["retry_after"] in (key, value["retry_of"])):
        raise PodError("state_unsupported", "No-start predecessor is malformed")
    if value["state"] in ("bound", "closed") and not binding_valid(value["native_binding"]):
        raise PodError("state_unsupported", "Bound admission lacks exact native identity")
    if value["native_binding"] is not None and not binding_valid(value["native_binding"]):
        raise PodError("state_unsupported", "Admission native identity is malformed")
    if "display" in value:
        from .display import validate_facts
        validate_facts(value["display"])
    _consumed_report(value, runtimes or set())


def _validate_context(value: object) -> dict:
    superseded = superseded_schemas(value)
    if superseded:
        raise PodError("state_unsupported",
                       f"Objective state uses superseded schema {', '.join(superseded)}; it is listed, "
                       "blocked and never converted. Settle its workers through Orca and start a new objective")
    state = exact(value, _CONTEXT_FIELDS | _SUCCESSION_FIELDS | {_RETIREMENT_FIELD, "decision_channels", "pod_ticks", "standing_authorization"}, _CONTEXT_FIELDS, name="context")
    if "standing_authorization" in state:
        from .standing import validate_record
        validate_record(state["standing_authorization"])
    if "pod_ticks" in state:
        from .pod_ticks import validate
        validate(state["pod_ticks"])
    if "decision_channels" in state and (type(state["decision_channels"]) is not int or state["decision_channels"] not in (1, 2, 3, 4)):
        raise PodError("state_unsupported", "Decision channel representation is unsupported")
    from .succession import validate_link
    for name in _SUCCESSION_FIELDS & set(state):
        validate_link(name, state[name])
    if _RETIREMENT_FIELD in state:
        from .retirement import validate_entry
        validate_entry(state[_RETIREMENT_FIELD])
        if "continued_by" in state:
            raise PodError("state_unsupported", "A continued record cannot also carry a historical retirement")
    if type(state["revision"]) is not int or state["revision"] < 0:
        raise PodError("state_unsupported", "State revision is malformed")
    if state["owner"] is not None and (not isinstance(state["owner"], str) or not state["owner"]):
        raise PodError("state_unsupported", "Context owner is malformed")
    if not isinstance(state["admissions"], dict):
        raise PodError("state_unsupported", "Admissions are malformed")
    checkpoint_value = state.get("checkpoint")
    if isinstance(checkpoint_value, dict):
        if checkpoint_value.get("obligations") is not None:
            from .obligations import row_bounds
            from .lineage import Lineage
            row_bounds(checkpoint_value["obligations"])
            if any(isinstance(row, dict) and ("renews" in row or "renewal" in row or row.get("state") == "renewed")
                   for row in checkpoint_value["obligations"]):
                Lineage(checkpoint_value["obligations"])
        if "issue_revision_data" in checkpoint_value:
            raise PodError("state_unsupported", "Transient issue body DATA cannot be stored in a checkpoint")
        from .issue_revision import requested, validate_decision
        if not isinstance(checkpoint_value.get("revision_decisions", []), list):
            raise PodError("state_unsupported", "Revision decision history is malformed")
        revisions = [row for row in checkpoint_value.get("revision_decisions", []) if requested(row)]
        if revisions and state.get("decision_channels") != 4:
            raise PodError("state_unsupported", "Issue revision evidence requires its decision channel marker")
        if state.get("decision_channels") == 4 and not revisions:
            raise PodError("state_unsupported", "Issue revision history is missing")
        for row in revisions:
            validate_decision(row, checkpoint_value)
        if state.get("decision_channels") == 4:
            from .obligations import validate_map_record
            validate_map_record(checkpoint_value)
    continuity = checkpoint_value.get("continuity") if isinstance(checkpoint_value, dict) else None
    if continuity is not None:
        _validate_continuity(continuity)
    runtimes = {entry[field] for entry in (continuity or {}).get("history", []) for field in ("from_runtime", "to_runtime")}
    for key, row in state["admissions"].items():
        if not isinstance(key, str) or not key:
            raise PodError("state_unsupported", "Admission key is malformed")
        _validate_admission(key, row, runtimes=runtimes)
    if (not isinstance(state["interventions"], dict) or not isinstance(state["source_rejections"], dict)
            or not isinstance(state["constraints"], list) or len(state["constraints"]) > 64):
        raise PodError("state_unsupported", "Context evidence is malformed")
    from .selection import validate_constraint
    for row in state["constraints"]:
        validate_constraint(row)
    checkpoint_value = state.get("checkpoint")
    if isinstance(checkpoint_value, dict) and "governance_history" in checkpoint_value:
        _governance_history(checkpoint_value)
    if isinstance(checkpoint_value, dict) and "delivery" in checkpoint_value:
        from .governance import validate_delivery_record
        validate_delivery_record(checkpoint_value["delivery"])
    if isinstance(checkpoint_value, dict) and "continuity" in checkpoint_value:
        _validate_continuity(checkpoint_value["continuity"])
    if isinstance(checkpoint_value, dict) and "display" in checkpoint_value:
        from .display import validate_record
        validate_record(checkpoint_value["display"], state["admissions"])
    return state


def _read(path: Path) -> dict:
    if not _record_exists(path):
        return _empty()
    return load_context(path)


def read(project: Path, objective: str) -> dict | None:
    path = _path(project, objective)
    return _read(path) if _record_exists(path) else None


def _write(path: Path, value: dict, *, native: dict | None = None, protected=(), before_publish=None) -> None:
    from .succession import refuse_continued
    # Backstop: only its own transition writes a continued record or a historical retirement. A successor an
    # incomplete claim reserves is refused at each work entry point instead, so the claim itself can write it.
    refuse_continued(value, reserved=False)
    from .standing import observe_scope
    observe_scope(value)
    from .util import protect_decision_record
    protect_decision_record(value)
    value["revision"] += 1
    _validate_context(value)
    record, chunks = prepare_context(path, value, native=native, protected=protected)
    if before_publish is not None:
        before_publish()
    publish_context(path, record, chunks)


def _write_succession(path: Path, value: dict) -> None:
    """The claim's single write of a predecessor record, under that record's lock."""
    from .util import protect_decision_record
    protect_decision_record(value)
    value["revision"] += 1
    _validate_context(value)
    record, chunks = prepare_context(path, value)
    publish_context(path, record, chunks)


def _write_retirement(path: Path, value: dict) -> None:
    """The owner's historical retirement: its single write of the record, under that record's lock."""
    from .util import protect_decision_record
    protect_decision_record(value)
    value["revision"] += 1
    _validate_context(value)
    record, chunks = prepare_context(path, value, purpose="retirement")
    publish_context(path, record, chunks)


def _run_references(state: dict) -> dict[str, str]:
    refs: dict[str, str] = {}
    checkpoint_value = state.get("checkpoint")
    native_refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
    if not isinstance(native_refs, list) or len(native_refs) > 32:
        raise PodError("native_authority_unverified", "Objective Run references are malformed")
    for ref in native_refs:
        if (not isinstance(ref, dict) or not isinstance(ref.get("runId"), str)
                or not isinstance(ref.get("runtime"), str) or not ref["runId"] or not ref["runtime"]):
            raise PodError("native_authority_unverified", "Objective Run reference is incomplete")
        if ref["runId"] in refs and refs[ref["runId"]] != ref["runtime"]:
            raise PodError("native_authority_unverified", "Objective Run runtime is contradictory")
        refs[ref["runId"]] = ref["runtime"]
    for row in state.get("admissions", {}).values():
        run_id, runtime = row.get("run_id"), row.get("runtime")
        if not isinstance(run_id, str) or not isinstance(runtime, str) or not run_id or not runtime:
            raise PodError("native_authority_unverified", "Admission Run reference is incomplete")
        if run_id not in refs or refs[run_id] != runtime:
            raise PodError("native_authority_unverified", "Admission Run is outside checkpoint references")
    return refs


def require_authority(project: Path, objective: str, *, owner: str,
                      state: dict | None = None, run_id: str | None = None,
                      native_port=None, expected_runtime: str | None = None, claim: str | None = None,
                      persist_rebind: bool = True) -> dict:
    """Join a stable native current Run to this objective's persisted owner and refs.

    Every caller passing ``state`` holds the objective lock, so a proven runtime
    continuity is rebound before the existing checks repeat against the new
    runtime. Closing writes defer its persistence to their successful write.
    ``expected_runtime`` is the runtime the caller's own
    mutation already observed; continuity only rebinds to that runtime.
    """
    from .operations import OrcaPort
    from .orca import current_run
    locked = state is not None
    state = _read(_path(project, objective)) if state is None else state
    from .succession import refuse_continued
    refuse_continued(state, claim=claim)  # `claim`: the succession claim of this successor, the one write it allows.
    from .handoff import refuse_nonowner
    refuse_nonowner(project, objective, state, owner, port=native_port, claim=claim)
    if state["owner"] not in (None, owner):
        raise PodError("native_authority_unverified", "Caller is not the recorded owner; objective belongs to another coordinator")
    refs = _run_references(state)
    if len(set(refs.values())) > 1:
        raise PodError("native_authority_unverified", "Objective Run references span runtimes")
    if run_id is not None and run_id not in refs:
        raise PodError("native_authority_unverified", "Run is not an exact objective reference")
    port = native_port or OrcaPort(project)
    if not refs:
        if state.get("checkpoint") is not None or state.get("admissions"):
            raise PodError("native_authority_unverified", "Existing objective lacks a Run binding")
        first = current_run()
        current = first.get("run")
        if not isinstance(current, dict) or not isinstance(current.get("id"), str):
            raise PodError("native_authority_unverified", "Current coordinator Run is unavailable")
        refs = {current["id"]: first["runtime"]}
    selected = (run_id,) if run_id is not None else tuple(sorted(refs))
    assignments = bound_assignments(state)
    continuity = None
    try:
        native = port.read_native(owner, authority_runs=selected, assignments=assignments)
    except PodError:
        if not locked:
            raise
        continuity = _continuity_step(project, objective, state, owner=owner, port=port,
                                      expected_runtime=expected_runtime, persist=persist_rebind)
        if continuity is None:
            raise
        native = port.read_native(owner, authority_runs=selected, assignments=bound_assignments(state))
    else:
        if locked and native.get("runtime") not in set(refs.values()):
            continuity = _continuity_step(project, objective, state, owner=owner, port=port,
                                          current=native, expected_runtime=expected_runtime, persist=persist_rebind)
            if continuity is not None and bound_assignments(state):
                native = port.read_native(owner, authority_runs=selected,
                                          assignments=bound_assignments(state))
    if continuity is not None:
        refs = _run_references(state)
    if (native.get("authoritative") is not True or native.get("owner") != owner
            or native.get("scope") != "objective_assignments" or native.get("complete") is not True
            or native.get("runtime") not in set(refs.values())):
        raise PodError("native_authority_unverified", authority_message(state, native, owner))
    if any(refs[key] != native["runtime"] for key in selected):
        raise PodError("native_authority_unverified", "Objective Run runtime changed")
    return {"runtime": native["runtime"], "run_id": run_id or next(iter(refs)),
            "native": native, "references": refs, "continuity": continuity}


def authority_message(state: dict, native: dict, owner: str) -> str:
    """Diagnose the already observed authority; never grant a new route."""
    recorded_owner = state.get("owner")
    if native.get("caller") != recorded_owner or owner != recorded_owner:
        return "Caller is not the recorded owner; return to that coordinator; no runtime-continuity route is available to this caller"
    refs = _run_references(state)
    if native.get("runtime") not in refs.values():
        return "Objective runtime changed; reconcile the recorded runtime before authority can join"
    binding = native.get("binding")
    if isinstance(binding, dict) and binding.get("id") not in refs and len(refs) == 1:
        return "Current native Run does not own this objective; select its Run with orca orchestration run-use --id " + next(iter(refs))
    return "Current native Run does not own this objective; recorded coordinator binding is unavailable or differs"


# --------------------------------------------------------------------------- runtime continuity (A2)

# The checkpoint map's `rebind` names obligation evidence; runtime continuity uses its own names.
CONTINUITY_HISTORY = 8
CONTINUITY_AMBIGUITY = 64
_CONTINUITY_EVENTS: ContextVar[list | None] = ContextVar("pod_runtime_continuity", default=None)


def continuity_events() -> ContextVar:
    """Collector the helper entry uses to report rebinds in a mutation's result."""
    return _CONTINUITY_EVENTS


def _validate_continuity(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"binding", "history"}:
        raise PodError("state_unsupported", "Runtime continuity record is malformed")
    binding = value["binding"]
    if binding is not None and (not isinstance(binding, dict) or set(binding) != {"run", "coordinator", "generation"}
                                or not all(isinstance(binding[key], str) and binding[key]
                                           for key in ("run", "coordinator"))
                                or type(binding["generation"]) is not int or binding["generation"] < 0):
        raise PodError("state_unsupported", "Recorded runtime continuity binding is malformed")
    history = value["history"]
    if not isinstance(history, list) or len(history) > CONTINUITY_HISTORY:
        raise PodError("state_unsupported", "Runtime continuity history is malformed or over its bound")
    if sum(isinstance(row, dict) and row.get("provenance") == "owner_handoff"
           and row.get("state") != "done" for row in history) > 1:
        raise PodError("state_unsupported", "More than one unfinished owner handoff")
    for row in history:
        if not isinstance(row, dict) or not {"from_runtime", "to_runtime"} <= set(row):
            raise PodError("state_unsupported", "Runtime continuity history row is malformed")
        for field in ("from_runtime", "to_runtime"):
            bounded_text(row[field], name=field)
        if isinstance(row, dict) and row.get("provenance") == "owner_handoff":
            from .handoff import validate_entry
            validate_entry(row)
            continue
        if (not isinstance(row, dict)
                or not {"from_runtime", "to_runtime", "at", "provenance", "verified"} <= set(row)
                or set(row) - {"from_runtime", "to_runtime", "at", "provenance", "verified", "decision"}
                or row["provenance"] not in ("automatic", "owner")
                or (row["provenance"] == "owner") != isinstance(row.get("decision"), dict)
                or not isinstance(row["verified"], dict)):
            raise PodError("state_unsupported", "Runtime continuity history row is malformed")
        if isinstance(row.get("decision"), dict):
            from .util import owner_decision
            owner_decision(row["decision"], kind="continuity", stored=True)


def classify_continuity(state: dict, *, owner: str, current: dict, port) -> dict:
    """Classify continuity across a runtime change from trusted native reads only.

    Compared: the Run, coordinator handle and consumer generation recorded for the
    objective, and every bound admission's Run/Task/Dispatch/worker/worktree ids.
    Each read is taken at the current runtime. A difference disproves continuity;
    an unreadable identity or a bound record absent without a contradiction leaves
    it ambiguous; otherwise it is proven. Reserved admissions use the exact
    Run/Task lookup: no row proves nothing is in flight.
    """
    refs = _run_references(state)
    recorded_runtime = next(iter(set(refs.values())))
    runtime = current.get("runtime")
    checkpoint_value = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
    recorded = (checkpoint_value.get("continuity") or {}).get("binding")
    run = current.get("binding")
    differs: list[str] = []
    unreadable: list[str] = []
    verified: dict = {"admissions": [], "absent": []}
    if state.get("owner") not in (None, owner):
        differs.append("coordinator")
    if not isinstance(run, dict):
        unreadable.append("current Run binding (run-current returned none or it moved during the read)")
    else:
        expected = recorded["run"] if recorded else (next(iter(refs)) if len(refs) == 1 else None)
        if run.get("id") not in refs or (expected is not None and run.get("id") != expected):
            differs.append("run")
        else:
            verified["run"] = run["id"]
            unreadable.extend(f"run {other}: not shown by run-current" for other in sorted(set(refs) - {run["id"]}))
        coordinator = run.get("coordinator_handle")
        if coordinator != owner or (recorded is not None and recorded["coordinator"] != coordinator):
            differs.append("coordinator")
        else:
            verified["coordinator"] = coordinator
        if recorded is None:
            unreadable.append("recorded consumer generation (none recorded)")
        elif run.get("consumer_generation") != recorded["generation"]:
            differs.append("consumer generation")
        else:
            verified["generation"] = recorded["generation"]
    work = continuity_work(state, runtime=runtime, port=port)
    differs.extend(work["differs"])
    unreadable.extend(work["ambiguity"])
    verified.update(work["verified"])
    reads = work["reads"]
    if reads and not differs:
        # Bracket the identity reads: the current Run binding and runtime must not move meanwhile.
        try:
            after = port.read_native(owner, authority_runs=tuple(sorted(refs)))
        except PodError as exc:
            after = {"runtime": None, "error": exc.code}
        if after.get("runtime") != runtime or after.get("binding") != run:
            unreadable.append("current Run binding changed during the continuity check")
    if len(unreadable) > CONTINUITY_AMBIGUITY:
        unreadable = [*unreadable[:CONTINUITY_AMBIGUITY - 1],
                      f"{len(unreadable) - CONTINUITY_AMBIGUITY + 1} more unreadable identities"]
    return {"classification": "disproven" if differs else "ambiguous" if unreadable else "proven",
            "recorded_runtime": recorded_runtime, "current_runtime": runtime,
            "differs": differs, "ambiguity": unreadable, "verified": verified,
            "binding": run if isinstance(run, dict) else None,
            "caller_is_owner": current.get("caller") == state.get("owner") == owner}


def continuity_work(state: dict, *, runtime: str, port) -> dict:
    """R98 exact admission comparisons, shared with the owner handoff."""
    differs, unreadable = [], []
    verified = {"admissions": [], "absent": []}
    reads = 0
    for key in sorted(state.get("admissions", {})):
        row = state["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        if row["state"] == "unresolved":
            unreadable.append(f"admission {key}: unresolved attempt")
            continue
        reads += 1
        if row["state"] == "reserved":
            try:
                rows = port.find_worker(run=row["run_id"], task=row["task_id"])
            except PodError as exc:
                unreadable.append(f"admission {key}: exact Run/Task lookup unavailable ({exc.code})")
                continue
            if not isinstance(rows, list):
                unreadable.append(f"admission {key}: exact Run/Task lookup inconclusive")
            elif rows:
                unreadable.append(f"admission {key}: Dispatch found without a recorded native binding")
            else:
                verified["absent"].append(key)
            continue
        binding = row.get("native_binding")
        try:
            shown = port.show_worker(binding["dispatchId"])
        except PodError as exc:
            unreadable.append(f"admission {key}: worker read unavailable ({exc.code})")
            continue
        result = shown.get("result") if isinstance(shown, dict) else None
        parts = [result.get(name) if isinstance(result, dict) else None
                 for name in ("dispatch", "projection", "worker")]
        if shown.get("runtime") != runtime or not all(isinstance(part, dict) for part in parts):
            unreadable.append(f"admission {key}: worker read incomplete at the current runtime")
            continue
        dispatch, projection, worker = parts
        if (dispatch.get("id"), dispatch.get("runId"), dispatch.get("taskId"), projection.get("id"),
                projection.get("dispatchId"), projection.get("runId"), projection.get("taskId"),
                worker.get("dispatchId"), worker.get("worktreeId")) != (
                binding["dispatchId"], binding["runId"], binding["taskId"], binding["workerId"],
                binding["dispatchId"], binding["runId"], binding["taskId"], binding["dispatchId"],
                binding["worktreeId"]):
            differs.append(f"admission {key}")
        else:
            verified["admissions"].append(key)
    return {"differs": differs, "ambiguity": unreadable, "verified": verified, "reads": reads}


def _apply_continuity(state: dict, verdict: dict, *, provenance: str, decision: dict | None = None) -> dict:
    """Change only the recorded runtime and, where none was recorded, the generation."""
    checkpoint_value = state["checkpoint"]
    record = checkpoint_value.get("continuity") or {"binding": None, "history": []}
    if len(record["history"]) >= CONTINUITY_HISTORY:
        raise PodError("runtime_continuity_full",
                       f"Runtime continuity history holds {CONTINUITY_HISTORY} entries; nothing was written. "
                       "Settle this objective's workers through Orca and continue in a new objective",
                       {"limit": CONTINUITY_HISTORY})
    target = verdict["current_runtime"]
    for ref in checkpoint_value.get("native_refs", []):
        ref["runtime"] = target
    for row in state["admissions"].values():
        row["runtime"] = target
    binding = record["binding"]
    run = verdict["binding"]
    if binding is None and isinstance(run, dict):
        binding = {"run": run["id"], "coordinator": run["coordinator_handle"],
                   "generation": run["consumer_generation"]}
    entry = {"from_runtime": verdict["recorded_runtime"], "to_runtime": target,
             "at": datetime.now(timezone.utc).isoformat(), "provenance": provenance,
             "verified": verdict["verified"]}
    if decision is not None:
        entry["decision"] = decision
    checkpoint_value["continuity"] = {"binding": binding, "history": [*record["history"], entry]}
    return entry


def _ambiguous(objective: str, verdict: dict) -> PodError:
    if verdict.get("caller_is_owner") is False:
        return PodError("runtime_continuity_ambiguous", "Caller is not the recorded owner; continuity is unproven and this caller cannot use internal runtime-continuity",
                        {"objective": objective, "recorded_runtime": verdict["recorded_runtime"],
                         "current_runtime": verdict["current_runtime"], "ambiguity": verdict["ambiguity"],
                         "next_action": "return to the recorded coordinator"})
    named = "; ".join(verdict["ambiguity"][:3])
    more = len(verdict["ambiguity"]) - 3
    return PodError(
        "runtime_continuity_ambiguous",
        f"Orca runtime changed ({verdict['recorded_runtime']} -> {verdict['current_runtime']}) and continuity "
        f"is unproven: {named}" + (f"; and {more} more" if more > 0 else "") + ". Nothing was written. "
        "Ask the Owner; only their direct decision naming this objective, both runtimes and this "
        "ambiguity can rebind (internal runtime-continuity)",
        {"objective": objective, "recorded_runtime": verdict["recorded_runtime"],
         "current_runtime": verdict["current_runtime"], "ambiguity": verdict["ambiguity"],
         "next_action": "ask the Owner; only on their direct decision call internal runtime-continuity "
                        "with a decision of provenance user_direct, their instruction, and this "
                        "objective, recorded_runtime, current_runtime and ambiguity unchanged; "
                        "otherwise settle this objective's workers through Orca"})


def _continuity_step(project: Path, objective: str, state: dict, *, owner: str, port,
                     current: dict | None = None, expected_runtime: str | None = None,
                     persist: bool = True) -> dict | None:
    """Under the caller's objective lock: rebind a proven continuity and record it.

    Returns None when no continuity path applies or continuity is disproven, so the
    caller's existing check refuses with its own code; raises when it is ambiguous.
    """
    try:
        refs = _run_references(state)
    except PodError:
        return None
    if not refs or len(set(refs.values())) != 1:
        return None
    if current is None:
        try:
            current = port.read_native(owner, authority_runs=tuple(sorted(refs)))
        except PodError:
            return None
    runtime = current.get("runtime") if isinstance(current, dict) else None
    if (not isinstance(runtime, str) or runtime in refs.values() or "binding" not in current
            or (expected_runtime is not None and runtime != expected_runtime)):
        # No observed change, or a port that cannot show the current Run binding.
        return None
    if current.get("caller") is not None and current["caller"] != state.get("owner"):
        from .handoff import refuse_nonowner
        refuse_nonowner(project, objective, state, owner, port=port, current=current)
    verdict = classify_continuity(state, owner=owner, current=current, port=port)
    if verdict["classification"] == "ambiguous":
        raise _ambiguous(objective, verdict)
    if verdict["classification"] == "disproven" or current.get("authoritative") is not True:
        return None
    entry = _apply_continuity(state, verdict, provenance="automatic")
    if persist:
        _write(_path(project, objective), state)
        _record_continuity_event(entry)
    return entry


def _record_continuity_event(entry: dict | None) -> None:
    events = _CONTINUITY_EVENTS.get()
    if entry is not None and events is not None:
        events.append(entry)


def continuity_locked(project: Path, objective: str, *, owner: str, port,
                      current: dict | None = None) -> dict | None:
    """A mutating path outside the objective lock classifies under it before its runtime check."""
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        from .succession import refuse_continued
        refuse_continued(state)
        if state["owner"] != owner or state.get("checkpoint") is None:
            return None
        return _continuity_step(project, objective, state, owner=owner, port=port, current=current)


def owner_continuity(project: Path, objective: str, *, owner: str, decision: object, port) -> dict:
    """Rebind an ambiguous continuity only on the Owner's exact-scope direct decision."""
    from .util import owner_decision, CHANNEL_FIELDS, channel_fields
    value = exact(decision, {"provenance", "instruction", "objective", "recorded_runtime",
                             "current_runtime", "ambiguity"} | CHANNEL_FIELDS,
                  {"provenance", "instruction", "objective", "recorded_runtime",
                   "current_runtime", "ambiguity"}, name="continuity_decision")
    bounded_text(value["instruction"], name="instruction", limit=1024)

    def mismatch(reason: str) -> PodError:
        return PodError("runtime_continuity_decision_mismatch",
                        f"The continuity decision does not match the current facts: {reason}. Nothing was written",
                        {"reason": reason,
                         "next_action": "retry the mutation; if it reports ambiguity, ask the Owner again"})
    if value["provenance"] != "user_direct":
        raise mismatch("only a direct Owner instruction can rebind an ambiguous continuity")
    value = owner_decision(value, kind="continuity")
    if (not isinstance(value["ambiguity"], list) or not value["ambiguity"]
            or len(value["ambiguity"]) > CONTINUITY_AMBIGUITY
            or any(not isinstance(item, str) for item in value["ambiguity"])):
        raise mismatch("the decision must name the exact ambiguity Pod reported")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner or state.get("checkpoint") is None:
            raise PodError("native_authority_unverified", "Caller is not the recorded owner; objective belongs to another coordinator")
        # A rebind is no work: a successor an incomplete claim reserves may rebind so that it can finish the claim.
        _require_open(state, reserved=False)
        from .evidence_closure import refuse_objective
        refuse_objective(project, objective)
        refs = _run_references(state)
        if len(set(refs.values())) != 1:
            raise PodError("native_authority_unverified", "Objective Run references span runtimes")
        current = port.read_native(owner, authority_runs=tuple(sorted(refs)))
        if current.get("caller") != owner or "binding" not in current:
            raise PodError("native_authority_unverified", "Caller is not the recorded owner; the decision must come through the objective coordinator")
        if current.get("runtime") in refs.values():
            raise mismatch("the Orca runtime has not changed")
        verdict = classify_continuity(state, owner=owner, current=current, port=port)
        if verdict["classification"] == "disproven":
            raise PodError("native_authority_unverified",
                           "Runtime continuity is disproven (" + ", ".join(verdict["differs"][:4])
                           + "); no decision can rebind it")
        if verdict["classification"] == "proven":
            raise mismatch("continuity is now proven; the next mutation rebinds automatically")
        if (value["objective"] != objective or value["recorded_runtime"] != verdict["recorded_runtime"]
                or value["current_runtime"] != verdict["current_runtime"]
                or value["ambiguity"] != verdict["ambiguity"]):
            raise mismatch("objective, runtimes or reported ambiguity differ")
        entry = _apply_continuity(state, verdict, provenance="owner",
                                  decision={"provenance": "user_direct", "instruction": value["instruction"],
                                            **channel_fields(value),
                                            "ambiguity": verdict["ambiguity"]})
        _write(path, state)
        return {"status": "rebound", "objective": objective, "runtime_continuity": entry}


def contexts_for_run(run_id: str) -> list[tuple[Path, dict]]:
    """Read every objective bound to one Run without choosing among them."""
    root = state_root()
    if not root.exists():
        return []
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    matches = []
    for path in root.glob("*/context.json"):
        try:
            state = _read(path)
        except PodError as exc:
            if exc.code == "state_unsupported":
                continue
            raise
        checkpoint_value = state.get("checkpoint")
        refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
        admission_match = any(row.get("run_id") == run_id for row in state["admissions"].values())
        if admission_match or any(isinstance(ref, dict) and ref.get("runId") == run_id for ref in refs):
            matches.append((path.parent, state))
    return matches


def context_root_for_run(run_id: str) -> Path | None:
    matches = contexts_for_run(run_id)
    if len(matches) > 1:
        raise PodError("ambiguous_context", "Multiple local contexts bind this Run")
    return matches[0][0] if matches else None


def context_for_run(run_id: str) -> dict | None:
    root = context_root_for_run(run_id)
    return _read(root / "context.json") if root is not None else None


def check_bound_sources(project: Path, objective: str, *, owner: str,
                        assignment: str, sources: list[dict]) -> dict:
    bounded_text(assignment, name="assignment", limit=128)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, state, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Source check belongs to another coordinator")
        require_authority(project, objective, owner=owner, state=state)
        return _check_bound_sources_locked(project, path, state, assignment, sources)


def _check_bound_sources_locked(project: Path, path: Path, state: dict,
                                assignment: str, sources: list[dict]) -> dict:
    from .records import source_identity
    from .term import clean
    if state["source_rejections"].get(assignment):
        raise PodError("source_rejected", "Assignment has a durable definitive source rejection")
    for entry in sources:
        exact(entry, {"path", "state", "sha256"}, {"path", "state"}, name="source")
        if entry["state"] == "unavailable":
            raise PodError("source_unbound", "Source needs a fresh actual binding")
        try:
            observed = source_identity(project, entry["path"])
        except PodError as exc:
            if exc.code in ("unsafe_source", "secret_source"):
                state["source_rejections"][assignment] = {"reason": exc.code, "path": entry["path"]}
                _write(path, state)
            raise
        if observed["state"] == "unavailable":
            raise PodError("source_unavailable", "Source is temporarily unavailable")
        reason = "source_absent" if observed["state"] == "absent" else "source_changed" if observed != entry else None
        if reason:
            state["source_rejections"][assignment] = {"reason": reason, "path": entry["path"]}
            _write(path, state)
            named = repr(clean(entry["path"]))
            if reason == "source_absent":
                message = (f"Bound source {named} is absent. Sources are existing inputs the worker reads; "
                           "list files the worker will create in scope/actions, rebuild the packet and admit again")
            else:
                message = f"Bound source {named} changed; reread the input, rebuild the packet and admit again"
            raise PodError(reason, message)
    return {"status": "current", "assignment": assignment}


# --------------------------------------------------------------------------- kernel facts

# Governance policy and currency live in the dedicated module.
from .governance import (MAX_GOVERNANCE_HISTORY, _governance_history, _governance_target,
                         _independent_governance, _resolve_commit, _retain_governance_history,
                         governance_observation, require_governance_current, user_direct_revision)

_OBJECT = gitio.OBJECT_ID
_git = gitio.read


def git_result(worktree: str | None, base: str | None, commit: str | None = None) -> dict:
    """Changed paths of a worker result read from Git: commits since base, renames, dirt."""
    if not isinstance(worktree, str) or not Path(worktree).is_absolute() or base is None:
        return {"status": "unavailable", "reason": "no_git_base"}
    root = Path(worktree)
    head = _resolve_commit(root, commit or "HEAD")
    if head is None:
        return {"status": "unavailable", "reason": "result_unresolved"}
    diff = _git(root, ["diff", "--name-status", "-z", "-M", "--no-ext-diff", base, head])
    status = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if diff is None or diff.returncode != 0 or status is None or status.returncode != 0:
        return {"status": "unavailable", "reason": "git_unreadable"}
    changed = gitio.name_status_paths(diff.stdout)
    entries = status.stdout.split("\0")
    index = 0
    while index < len(entries) and entries[index]:
        entry = entries[index]
        changed.add(entry[3:])
        if entry[:1] in ("R", "C") or entry[1:2] in ("R", "C"):
            index += 1
            if index < len(entries) and entries[index]:
                changed.add(entries[index])
        index += 1
    paths = {path.rstrip("/") for path in changed if path}
    if len(paths) > 1024:
        return {"status": "over_limit", "reason": "changed_paths_limit", "base": base,
                "head": head, "count": len(paths)}
    return {"status": "observed", "base": base, "head": head,
            "changed_paths": sorted(paths)}


def git_head(worktree: str | None) -> str | None:
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        return None
    return _resolve_commit(Path(worktree), "HEAD")


def git_is_ancestor(project: Path, commit: str, candidate: str) -> bool | None:
    return gitio.is_ancestor(project, commit, candidate, resolve=_resolve_commit)


def git_delta(project: Path, source: str, target: str) -> list[str] | None:
    """Paths changed between two candidate commits; None when Git cannot prove it."""
    first, second = _resolve_commit(project, source), _resolve_commit(project, target)
    if first is None or second is None or not _OBJECT.fullmatch(source) or not _OBJECT.fullmatch(target):
        return None
    completed = _git(project, ["diff", "--name-status", "-z", "-M", "--no-ext-diff", first, second])
    if completed is None or completed.returncode != 0:
        return None
    return sorted(gitio.name_status_paths(completed.stdout))


def map_of(state: dict | None) -> dict | None:
    checkpoint_value = state.get("checkpoint") if isinstance(state, dict) else None
    if isinstance(checkpoint_value, dict) and checkpoint_value.get("obligations") is not None:
        return checkpoint_value
    return None


def _outstanding_ids(state: dict, native: dict | None) -> list[str]:
    """Objective-local outstanding admissions; without native evidence nothing is settled."""
    evidence: dict[str, list[dict]] = {}
    for row in (native or {}).get("assignments", []) if isinstance(native, dict) else []:
        if isinstance(row, dict) and isinstance(row.get("admission_id"), str):
            evidence.setdefault(row["admission_id"], []).append(row)
    outstanding = []
    for key, admission in state.get("admissions", {}).items():
        if admission["state"] in ("closed", "deferred"):
            continue
        binding = admission.get("native_binding")
        matches = evidence.get(key, [])
        settled = bool(admission["state"] == "bound" and binding_valid(binding) and len(matches) == 1
                       and isinstance(native, dict)
                       and matches[0].get("settled") is True
                       and matches[0].get("runtime") == admission["runtime"] == native.get("runtime")
                       and matches[0].get("run_id") == binding["runId"]
                       and matches[0].get("task_id") == binding["taskId"]
                       and matches[0].get("dispatch_id") == binding["dispatchId"]
                       and matches[0].get("worker_id") == binding["workerId"])
        if not settled:
            outstanding.append(key)
    return outstanding


def _governor_pending(project: Path, objective: str, excluded: frozenset = frozenset()) -> bool:
    """Whether an allowed Governor row is unsettled, apart from the rows a closure preserved (`excluded`, C11)."""
    from .governor import _read_journal, _record_path
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        return True
    return any(row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")
               and row["record_id"] not in excluded for row in journal["actions"])


def _authorization_granted(project: Path, objective: str, *, context: dict | None = None):
    def granted(scope: str, candidate: str) -> bool:
        from .governor import _read_journal, _record_path, applicable_authorization
        try:
            journal = _read_journal(_record_path(project, objective))
            current = context if context is not None else _read(_path(project, objective))
        except PodError:
            return False
        kinds = ("push", "pr_update") if scope == "publish" else (scope,)

        def valid(row: dict) -> bool:
            # The same tree and, for merge, the same target the unit binds now; a retarget needs fresh consent.
            unit = journal.get("units", {}).get(row["action"].get("unit")) or {}
            bound = unit.get("candidate") or {}
            tree = bound.get("tree") if bound.get("commit") == row.get("commit") else None
            return applicable_authorization(unit, row["action"].get("authorization"),
                                            candidate=row.get("commit") or row["action"]["candidate"],
                                            tree=tree, scope=scope, context=current)
        # A row carried from a lost coordinator records the predecessor's consent, never this objective's (R118).
        return any(row["decision"] == "ALLOW" and row["action"]["kind"] in kinds
                   and not isinstance(row.get("carried_from"), dict)
                   and candidate in (row["action"]["candidate"], row.get("commit"), row.get("candidate_id"))
                   and valid(row) for row in journal["actions"])
    return granted


@input_record("verification")
def validate_verification(value: object) -> dict:
    """The checkpoint's current verification context: dependencies and environment (R41)."""
    record = exact(value, {"dependencies", "environment"}, {"dependencies", "environment"},
                   name="verification")
    if (not isinstance(record["dependencies"], list) or len(record["dependencies"]) > bound("verification", "dependencies", "max_items")
            or len(set(map(str, record["dependencies"]))) != len(record["dependencies"])):
        raise PodError("invalid_checkpoint", "Verification dependencies are a bounded unique list")
    for item in record["dependencies"]:
        bounded_text(item, name="dependency", limit=bound("verification", "dependencies", "items")["max_length"])
    bounded_text(record["environment"], name="environment", limit=bound("verification", "environment"))
    return {"dependencies": list(record["dependencies"]), "environment": record["environment"]}


def kernel_context(project: Path, objective: str, state: dict, native: dict | None, *,
                   candidate: str | None = None, criteria: list | None = None,
                   governance: dict | None = None, delegation: str | None = None,
                   verification: dict | None = None) -> dict:
    """Objective-local facts the obligation kernel judges; read under the caller's lock."""
    from .config import effective, load
    from .records import source_identity
    from .selection import worker_ceiling
    from .governor import objective_delivery_reporting
    from .gitio import require_candidate
    checkpoint_value = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
    snapshot = load(project)
    ceiling = worker_ceiling(snapshot, state.get("constraints", [])) if not snapshot["errors"] else 0
    if ceiling == 0 or not snapshot["eligible"]:
        delegation = "unavailable"
    elif delegation not in ("available", "unavailable"):
        delegation = "unknown"

    def source_state(path: str) -> str:
        try:
            return source_identity(project, path)["state"]
        except PodError:
            return "rejected"

    def source_current(entry: dict) -> bool:
        """A bound evidence source still has exactly its recorded identity."""
        try:
            return entry.get("state") == "present" and source_identity(project, entry["path"]) == entry
        except (PodError, KeyError, TypeError):
            return False

    map_state = map_of(state)
    base_ref = (map_state or {}).get("governance", {}).get("base_ref")
    from .util import context_decisions
    from .governor import _read_journal, _record_path
    try:
        journal = _read_journal(_record_path(project, objective))
        decisions = context_decisions(state, journal)
    except PodError:
        journal = None
        decisions = context_decisions(state)
    from .worker_tabs import rows as tab_rows
    placements = {row["admission_id"]: row["placement"] for row in (native or {}).get("assignments", [])
                  if isinstance(row.get("placement"), dict)}
    outstanding = _outstanding_ids(state, native)
    return {"admissions": state.get("admissions", {}),
            "pod_mapping_source": checkpoint_value.get("objective_source"),
            "pod_mapping_facts": state.get("pod_ticks", {}).get("facts"),
            "pod_mapping_read": state.get("pod_ticks", {}).get("read", {}),
            "pod_mapping_ticks": state.get("pod_ticks", {}),
            "standing_journal": journal,
            **({"standing_context": state} if "standing_authorization" in state else {}),
            "worker_tabs": tab_rows(state, placements, outstanding=outstanding),
            # Tab intent is shown by status/acknowledgements, never added to the
            # immutable closure report. It can outlive objective closure.
            "owner_decisions": [row for row in decisions
                                if row.get("decision", {}).get("kind") != "keep_worker_tab"],
            "outstanding": outstanding,
            "ceiling": ceiling, "delegation": delegation,
            "constraints": state.get("constraints", []),
            "criteria": list(criteria if criteria is not None else checkpoint_value.get("criteria", [])),
            "candidate": candidate if candidate is not None else checkpoint_value.get("candidate"),
            "governance": governance,
            "bound_governance": lambda bound: governance_observation(
                project, bound["base_ref"], at=bound["base"], bound_ref=bound["base_ref"],
                selection=bound["selection"]),
            "governance_current": (governance or {}).get("current") if governance is not None
            else (_resolve_commit(project, base_ref) if base_ref else None),
            "source_state": source_state,
            "source_current": source_current,
            "policy_revision": effective(project)["revision"],
            "verification": verification if verification is not None else checkpoint_value.get("verification"),
            "authorization_granted": _authorization_granted(project, objective, context=state),
            "governor_pending": _governor_pending(project, objective),
            "governor_delivery": objective_delivery_reporting(project, objective),
            "git_delta": lambda source, target: git_delta(project, source, target),
            "candidate_check": lambda candidate: require_candidate(project, candidate, assurance=True),
            "is_ancestor": lambda commit, target: git_is_ancestor(project, commit, target)}


def kernel_view(project: Path, objective: str, *, native: dict | None = None,
                candidate: str | None = None, state: dict | None = None) -> dict:
    """Read-only map, facts and derived projections for status, acceptance and reports."""
    from .obligations import label_qualification, report_projection, status_projection
    state = read(project, objective) if state is None else state
    if state is None:
        return {"state": None, "map": None, "ctx": None, "status": status_projection(None, {}),
                "label": label_qualification(None, {}, candidate), "report": None, "settlement": "none"}
    settlement, failure, runtime_changed = "observed", None, False
    if native is None:
        from .operations import OrcaPort
        bound = tuple(row for row in state["admissions"].values()
                      if row["state"] == "bound" and binding_valid(row.get("native_binding")))
        try:
            native = OrcaPort(project).read_native(state.get("owner") or "", assignments=bound) if bound else None
        except PodError as exc:
            # The exact read is all-or-nothing; keep its code so status can name it (A1).
            native, settlement, failure = None, "unverified", exc.code
            runtime_changed = (exc.detail or {}).get("reason") == "runtime_changed"
    ctx = kernel_context(project, objective, state, native)
    map_state = map_of(state)
    view = {"state": state, "map": map_state, "ctx": ctx, "settlement": settlement,
            "settlement_failure": failure,
            "status": status_projection(map_state, ctx),
            "label": label_qualification(map_state, ctx, candidate if candidate is not None else ctx["candidate"])}
    # Read-side reports include normal mode while older stored closures retain their exact shape.
    view["report"] = report_projection(map_state, ctx, standing_normal=True) if map_state is not None else None
    view["placements"] = {row["admission_id"]: row["placement"]
                          for row in (native or {}).get("assignments", [])
                          if isinstance(row, dict) and isinstance(row.get("admission_id"), str)
                          and isinstance(row.get("placement"), dict)}
    from .worker_tabs import rows as tab_rows
    view["worker_tabs"] = tab_rows(state, view["placements"], outstanding=ctx["outstanding"], failure=failure,
                                   runtime_changed=runtime_changed)
    from .pod_ticks import projection as tick_projection
    view["pod_ticks"] = tick_projection(state, map_state, ctx, project=project, objective=objective)
    return view


def closed_snapshot_context(project: Path, objective: str, state: dict) -> dict:
    """Replay closure using complete, immutable consumed settlement observations.

    This reads past settlement only to validate an already recorded closure;
    it does not settle an admission or authorize ordinary work without readback.
    """
    ctx = kernel_context(project, objective, state, None)
    if (state.get("checkpoint") or {}).get("closure") is None:
        return ctx
    preserved = state["checkpoint"]["closure"].get("preserved") if isinstance(state["checkpoint"]["closure"], dict) \
        else None
    if isinstance(preserved, list) and preserved:
        # The rows this closure preserved stay unsettled in the journal; they never reopen it (C11).
        ctx["closure_preserved"] = preserved
        ctx["governor_pending"] = _governor_pending(project, objective, excluded=frozenset(
            row.get("record") for row in preserved if isinstance(row, dict)))
    ctx["outstanding"] = [key for key in ctx["outstanding"]
                          if state["admissions"][key]["state"] != "bound"
                          or (state["admissions"][key].get("report") or {}).get("status") != "validated_observation"]
    return ctx


def map_read(project: Path, objective: str) -> dict:
    """Read one restatable map and its exact objective-local attempt evidence."""
    from .assurance import evidence_valid, review_completed
    from .obligations import bound_governance_digest, restatable_rows, retained_history, undispositioned
    view = kernel_view(project, objective)
    state, map_state, ctx = view["state"], view["map"], view["ctx"]
    if state is None or map_state is None:
        raise PodError("map_unavailable", "This objective has no recorded obligation map")
    outstanding_ids = set(ctx["outstanding"])
    outstanding = []
    settled_attempts = {ob["id"]: [] for ob in map_state["obligations"]}
    for key, row in state["admissions"].items():
        binding = row.get("native_binding") or {}
        if key in outstanding_ids:
            outstanding.append({"admission": key, "role": row["role"], "serves": list(row["serves"]),
                                "state": row["state"], "dispatch": binding.get("dispatchId")})
        elif row["role"] == "review" and review_completed(row):
            for served in row["serves"]:
                if served in settled_attempts:
                    settled_attempts[served].append(key)
    pending = []
    for key, row in undispositioned(ctx).items():
        result = row.get("result") or {}
        pending.append({"admission": key, "serves": list(row.get("serves") or []),
                        "head": result.get("head"),
                        "paths_complete": (row.get("changed_paths") is not None
                                           and result.get("paths_status") != "over_limit")})
    gov = bound_governance_digest(map_state)
    eligibility = {}
    for ob in map_state["obligations"]:
        if ob["kind"] != "assurance":
            continue
        if map_state.get("closure") or ob["state"] in ("withdrawn", "renewed"):
            status = "terminal"
        elif ob["state"] == "satisfied" and evidence_valid(ob, ctx, gov):
            status = "bound"
        elif any(ob["id"] in row["serves"] for row in outstanding):
            status = "busy"
        else:
            status = "eligible"
        eligibility[ob["id"]] = status
    actions = [f"record a disposition for admission {row['admission']}" for row in pending]
    for ob in map_state["obligations"]:
        if ob["state"] == "active":
            actions.append(f"continue {ob['id']} with {ob['executor']}")
        elif ob["state"] == "waiting":
            actions.append(f"resolve {ob['id']} wait: {ob['wait']['class']} on {ob['wait']['referent']}")
        elif ob["state"] == "blocked_external":
            actions.append(f"wait for {ob['external']['party']}: {ob['external']['need']}")
    from .governor import _read_journal, _record_path, finalization
    try:
        remaining = finalization(state, _read_journal(_record_path(project, objective)))
    except PodError:
        remaining = None
    from .worker_tabs import finalization as tab_finalization
    remaining = tab_finalization(remaining, view.get("worker_tabs", []))
    return {"schema": "pod-map-view/v1", "seq": map_state["seq"], "next_seq": map_state["seq"] + 1,
            "revision": map_state["revision"], "candidate": state["checkpoint"].get("candidate"),
            **{key: state["checkpoint"].get(key) for key in
               ("criteria", "plan_revision", "objective_source", "worktree", "policy_revision")},
            "governance": map_state["governance"], "delivery": map_state.get("delivery"),
            **({"revision_decisions": map_state["revision_decisions"], "decision_channels": 4}
               if any(row.get("operation") == "issue_revision" for row in map_state.get("revision_decisions", [])) else {}),
            "closure": map_state.get("closure"), "obligations": restatable_rows(map_state),
            "retained_history": retained_history(map_state),
            "acceptances": {ob["id"]: ob["acceptances"] for ob in map_state["obligations"]
                            if ob.get("acceptances")},
            "outstanding": outstanding, "settled_attempts": settled_attempts,
            "undispositioned": pending, "review_eligibility": eligibility,
            "next_actions": actions[:MAX_RECORD // 128], "native_settlement": view["settlement"],
            "finalization": remaining}


def _require_open(state: dict, *, reserved: bool = True) -> None:
    from .succession import refuse_continued
    refuse_continued(state, reserved=reserved)
    map_state = map_of(state)
    if map_state is not None and map_state.get("closure"):
        from .obligations import refuse
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=map_state["closure"]["revision"])


def _map_refusal_context(exc: PodError, state: dict) -> PodError:
    prior = map_of(state) or {}
    detail = dict(exc.detail) if isinstance(exc.detail, dict) else {}
    detail.setdefault("current_seq", prior.get("seq", 0))
    detail.setdefault("revision", prior.get("revision", 0))
    exc.detail = detail
    return exc


@input_record("checkpoint")
def _checkpoint_input(project: Path, objective: str, owner: str, value: dict,
                      native: dict, previous: dict | None = None) -> tuple[dict, dict, list]:
    from .config import effective
    from .obligations import MAP_INPUT, MAP_STORED, refuse
    bounded_text(owner, name="owner")
    # A coordinator may round-trip the stored map; Pod recomputes every stamped field.
    value = {key: item for key, item in value.items()
             if key not in MAP_STORED - MAP_INPUT and key != "continuity"}
    if previous:
        carried = ("criteria", "plan_revision", "candidate", "assignments", "questions",
                   "verification_gaps", "next_safe_action", "worktree", "objective_source", "verification", "display")
        value = {**{key: previous[key] for key in carried if key in previous}, **value}
        value.setdefault("native_refs", [])
        value.setdefault("policy_revision", effective(project)["revision"])
    allowed = fields("checkpoint")
    required_fields = required("checkpoint")
    exact(value, allowed, required_fields, name="checkpoint")
    if value["schema"] != CHECKPOINT_SCHEMA or not isinstance(native.get("runtime"), str):
        raise PodError("invalid_checkpoint", f"Checkpoint needs {CHECKPOINT_SCHEMA} and current native runtime readback")
    if (not isinstance(value["criteria"], list) or len(value["criteria"]) > bound("checkpoint", "criteria", "max_items")
            or any(not isinstance(item, str) or not item for item in value["criteria"])
            or not isinstance(value["candidate"], str) or not value["candidate"]):
        raise PodError("invalid_checkpoint", "Checkpoint criteria and candidate are bounded text")
    from .gitio import require_candidate
    try:
        require_candidate(project, value["candidate"])
    except PodError as exc:
        if (previous and exc.code == "invalid_checkpoint" and isinstance(exc.detail, dict)
                and exc.detail.get("resolved") is None):
            exc.detail["detail"] = "candidate_identity_unavailable"
        raise
    value = {**value, "policy_revision": effective(project)["revision"]}
    if "objective" in value and value["objective"] != objective:
        raise PodError("invalid_checkpoint", "Checkpoint objective differs from its state key")
    if "objective_source" in value:
        from .github import validate_issue_binding
        validate_issue_binding(value["objective_source"])
    if "worktree" in value:
        from .records import worktree_binding
        worktree_binding(value["worktree"])
    if "verification" in value:
        value = {**value, "verification": validate_verification(value["verification"])}
    if "display" in value:
        from .display import validate_record
        validate_record(value["display"])
    proposed = {key: value[key] for key in MAP_INPUT if key in value}
    core = {key: item for key, item in value.items() if key not in MAP_INPUT}
    dispositions = proposed.pop("dispositions", [])
    if not isinstance(dispositions, list) or len(dispositions) > bound("map", "dispositions", "max_items"):
        raise refuse("obligation_unaccounted", "disposition_invalid", "dispositions are a bounded list", record='checkpoint')
    return core, proposed, dispositions


def validate_checkpoint_snapshot(project: Path, state: dict) -> None:
    """Parse stored checkpoint core through the ordinary checkpoint contract."""
    value = state.get("checkpoint")
    if not isinstance(value, dict):
        raise PodError("state_unsupported", "Persisted checkpoint is not a record")
    bounded_text(value.get("policy_revision"), name="policy_revision")
    # These are the two bundle stamps added after the ordinary core/map writer.
    stamps = exact({key: value[key] for key in ("pod_version", "bundle_digest") if key in value},
                   {"pod_version", "bundle_digest"}, {"pod_version", "bundle_digest"}, name="checkpoint stamps")
    bounded_text(stamps["pod_version"], name="pod_version", limit=64)
    if not isinstance(stamps["bundle_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", stamps["bundle_digest"]) is None:
        raise PodError("state_unsupported", "Persisted checkpoint bundle identity is malformed")
    refs = _run_references(state)
    core = {key: item for key, item in value.items() if key not in stamps}
    _checkpoint_input(project, value.get("objective"), state.get("owner"), core,
                      {"runtime": next(iter(refs.values()), None)})
    from .obligations import validate_map_record
    validate_map_record(value)


def _closure_unrecorded(project: Path, objective: str, state: dict, *, delivered: bool,
                        preserved: frozenset = frozenset()) -> dict | None:
    """What finalization still names before closure, for a write that closes (R117); rows this closure preserves
    are not among them (C11).

    An unreadable journal is left to closure's own settlement check, which refuses it.
    """
    from .governor import _read_journal, _record_path, finalization, finalization_step
    try:
        view = finalization(state, _read_journal(_record_path(project, objective)))
    except PodError:
        return None
    if preserved and "validation_readback" in view["remaining"]:
        kept = [entry for entry in view["records"]["validation_readback"] if entry["record"] not in preserved]
        view["records"]["validation_readback"] = kept
        if not kept:
            view["remaining"] = [name for name in view["remaining"] if name != "validation_readback"]
    remaining = [name for name in view["remaining"]
                 if name != "closure" and not (delivered and name == "delivery_record")]
    if not remaining:
        return None
    return {"remaining": remaining,
            "next_action": "record " + ", ".join(remaining) + " in that order before closure. Next: "
                           + finalization_step(remaining[0], view["records"][remaining[0]])}


def _closure_preservation(project: Path, objective: str, state: dict, ctx: dict) -> frozenset:
    """C11 at a closing write: a delivered objective's closure is not held by a publication-derived validation of
    an earlier head that the merged delivery superseded. Returns the record ids it preserves (also put in `ctx`)."""
    from .governor import _read_journal, _record_path
    from .evidence_closure import closure_preservation
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        return frozenset()
    found = closure_preservation(project, objective, state, journal)
    if found is None:
        return frozenset()
    if found["settles"]:
        row = found["settles"][0]
        from .obligations import refuse
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     f"Pod's readback now settles derived validation {row['record'][:12]} ({row['outcome']}); "
                     "record it first", record="closure",
                     next_action=f"run governor-reconcile for record {row['record']}, then write the closing "
                                 "checkpoint again")
    if found["proof"]["status"] != "proved" or found["held"] or not found["preserved"]:
        ctx["closure_preservation"] = {"proof": found["proof"], "held": found["held"],
                                       "preserved_at_closure": found["preserved"]}
        return frozenset()
    ids = frozenset(row["record"] for row in found["preserved"])
    ctx["closure_preserved"] = found["preserved"]
    ctx["governor_pending"] = _governor_pending(project, objective, excluded=ids)
    return ids


def _checkpoint_map_transition(project: Path, objective: str, state: dict, authority: dict,
                               core: dict, previous: dict, proposed: dict, dispositions: list,
                               native: dict, issue_revision=None) -> tuple[dict, dict | None]:
    from .obligations import accept_write, disposition, refuse
    prior_map = map_of(state)
    map_state: dict = {}
    ctx = None
    has_delivery = "delivery" in proposed
    derived_adoption = None
    adopted_observation = None
    delivery_request = proposed.pop("delivery", None)
    if proposed or prior_map is not None:
        refresh = proposed.get("governance_refresh") is True
        verified_delivery = None
        if has_delivery:
            from .governance import verify_delivery
            if prior_map is None:
                raise PodError("delivery_unverified", "A delivery needs a bound obligation map",
                               {"detail": "map_missing", "next_action": "checkpoint the objective map first"})
            record_id = delivery_request.get("record") if isinstance(delivery_request, dict) else None
            verified_delivery = verify_delivery(project, objective, prior_map, record_id,
                                                seq=prior_map["seq"] + 1)
        kept = None
        if prior_map is not None and not refresh:
            current_map = prior_map if verified_delivery is None else {**prior_map, "delivery": verified_delivery}
            from .governance import completion_governance
            from .obligations import restatable_rows
            closing = (proposed.get("close") is True and not prior_map.get("closure")
                       and (verified_delivery is None or prior_map.get("delivery") == verified_delivery))
            delivery_only = (verified_delivery is not None and set(proposed) <= {"seq", "obligations"}
                             and proposed.get("obligations", prior_map["obligations"]) in
                                 (prior_map["obligations"], restatable_rows(prior_map))
                             and all(core.get(key) == previous.get(key) for key in core
                                     if key not in {"schema", "native_refs", "policy_revision"}))
            if closing or delivery_only:
                kept, unchanged = completion_governance(project, current_map, checkpoint=True)
            else:
                unchanged = require_governance_current(project, current_map, checkpoint=True)
            if unchanged is not None:
                adopted_observation, derived_adoption = unchanged
        if prior_map is None:
            declared = proposed.get("governance")
            base_ref = declared.get("base_ref") if isinstance(declared, dict) else None
            observed = governance_observation(
                project, base_ref,
                user_direct=user_direct_revision(proposed.get("revision_authority")))
        else:
            base_ref = prior_map["governance"]["base_ref"]
            declared = proposed.get("governance")
            change = isinstance(declared, dict) and declared.get("base_ref", base_ref) != base_ref
            if change:
                # Normalize only an alias of the already trusted target. This
                # comparison grants no authority to select a different target;
                # keep the original declaration for exact-snapshot consent below.
                canonical, _ = _governance_target(project, declared["base_ref"],
                                                   user_direct=True, bound_ref=None)
                if canonical is not None and canonical == base_ref:
                    proposed = {**proposed, "governance": {**declared, "base_ref": base_ref}}
                    change = False
            if change and refresh:
                observed = governance_observation(
                        project, declared["base_ref"],
                        user_direct=user_direct_revision(proposed.get("revision_authority")))
            else:
                at = None if refresh else prior_map["governance"]["base"]
                observed = (governance_observation(project, base_ref, at=at, bound_ref=base_ref,
                                                   selection=prior_map["governance"].get("selection"))
                            if base_ref is not None
                        else governance_observation(project, None))
        if adopted_observation is not None:
            observed = adopted_observation
        history = _governance_history(prior_map)
        snapshot_authorized = (refresh
            and user_direct_revision(proposed.get("revision_authority"))
            and isinstance(declared, dict)
            and declared.get("base_ref") == observed.get("target_ref")
            and declared.get("base") == observed.get("commit"))
        if derived_adoption is None:
            observed = _independent_governance(
                project, observed, core["candidate"], state["admissions"],
                established=(prior_map or {}).get("governance"),
                previous_candidate=previous.get("candidate"),
                known_candidates=history["candidates"], snapshot_authorized=snapshot_authorized)
        ctx = kernel_context(project, objective, state, authority.get("native"),
                             candidate=core["candidate"], criteria=core["criteria"],
                             governance=observed, delegation=native.get("delegation"),
                             verification=core.get("verification"))
        if "standing_authorization" in state:
            ctx["standing_context"] = {**state, "checkpoint": {**(state.get("checkpoint") or {}), **core}}
            ctx["authorization_granted"] = _authorization_granted(project, objective, context=ctx["standing_context"])
        ctx["operation"] = "checkpoint"
        ctx["pod_mapping_source"] = core.get("objective_source")
        if "mapping_repair" in proposed:
            from .pod_mapping import observe as observe_mapping, prepare, request
            from .github import issue_recheck_live
            from .pod_ticks import _facts, _source_check, empty
            request(proposed["mapping_repair"])
            expected = (prior_map or {}).get("seq", 0) + 1
            if type(proposed.get("seq")) is not int or proposed["seq"] != expected:
                raise refuse("map_stale", "map_stale", "mapping repair requires the next map sequence",
                             expected=expected, current_seq=expected - 1)
            source = core.get("objective_source")
            if source is None:
                raise PodError("mapping_repair_refused", "Mapping repair requires a bound issue",
                               {"detail": "current_binding_unavailable"})
            checked, live = issue_recheck_live(project, source)
            _source_check(checked)
            if live is None:
                raise PodError("mapping_repair_refused", "Mapping repair requires a current issue read",
                               {"detail": "current_binding_unavailable"})
            ctx["pod_mapping_repairs"] = prepare(state, source, proposed["mapping_repair"], live["body"])
            ticks = state.setdefault("pod_ticks", empty())
            # This extra mapping-only read supplies no standing-consent observation.
            # The ordinary introducing/rebinding refresh keeps its existing hooks.
            observe_mapping(state, ticks, source, live["body"], mapping_check=checked)
            _facts(ticks, source, live["body"], mapping_check=checked)
            ctx["pod_mapping_facts"], ctx["pod_mapping_read"] = ticks["facts"], ticks["read"]
        ctx["governance_refresh"] = refresh
        if kept is not None:
            ctx["governance_kept"] = kept
        ctx["derived_adoption"] = derived_adoption
        ctx["recorded_questions"] = previous.get("questions", [])
        if issue_revision is not None:
            ctx["issue_revision"] = issue_revision
        if dispositions:
            if prior_map is None:
                raise refuse("obligation_unaccounted", "disposition_invalid",
                             "dispositions follow admissions under a map", record='checkpoint map transition')
            if prior_map.get("closure"):
                raise refuse("objective_closed", "objective_closed", "the objective is closed",
                             closure_revision=prior_map["closure"]["revision"])
            for request in dispositions:
                key = request.get("admission") if isinstance(request, dict) else None
                row = state["admissions"].get(key) if isinstance(key, str) else None
                if row is None:
                    raise refuse("obligation_unaccounted", "disposition_invalid",
                                 "a disposition names a recorded admission", record='checkpoint map transition')
                row["disposition"] = disposition(row, request, ctx, seq=prior_map["seq"] + 1)
        if proposed.get("close") is True and not (prior_map or {}).get("closure"):
            from .pod_ticks import closing_context, require_tabs
            closing_context(state, prior_map, ctx, project=project, objective=objective,
                            source=core.get("objective_source"))
            require_tabs(ctx["worker_tabs"], prior_map, proposed, ctx)
            from .succession import refuse_incomplete
            # A new closure proves that no incomplete claim, from any writer, names this objective as successor
            # (R118); a repeated one answers from the recorded closure as before (R117).
            refuse_incomplete(project, objective, state, full=True)
            kept = _closure_preservation(project, objective, state, ctx)
            # A delivery recorded in the closing write itself does not count: closure is the last record, and
            # its report is derived before that write adds the delivery.
            ctx["closure_unrecorded"] = _closure_unrecorded(
                project, objective, state, delivered=(prior_map or {}).get("delivery") is not None, preserved=kept)
        try:
            map_state = accept_write(prior_map, proposed, ctx)
        except PodError as exc:
            if ctx.get("closure_preservation") and isinstance(exc.detail, dict):
                # Today's refusal stands, naming why no row could be preserved (C11).
                exc.detail = {**exc.detail, "closure_preservation": ctx["closure_preservation"]}
            raise
        from .standing import checkpoint_times
        checkpoint_times(state, prior_map, map_state)
        if verified_delivery is not None:
            map_state["delivery"] = verified_delivery
        _retain_governance_history(project, map_state, prior_map, core["candidate"], proposed,
                                   snapshot_authorized=snapshot_authorized, derived_adoption=derived_adoption)
        if map_state.get("closure") is not None:
            # Include this checkpoint's decision in the final stored report as well.
            from .obligations import report_projection
            map_state["closure"]["report"] = report_projection(map_state, ctx)
    return map_state, ctx


@input_record("checkpoint")
def checkpoint(project: Path, objective: str, *, owner: str, value: dict, native: dict) -> dict:
    """Persist one validated checkpoint; while a map exists, every write is a map transition."""
    from .bundle import running_identity
    from .obligations import report_projection
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        raise PodError("invalid_checkpoint", f"Checkpoint needs {CHECKPOINT_SCHEMA} and current native runtime readback")
    from .issue_revision import requested, refuse as issue_refuse
    if "issue_revision_data" in value:
        if not requested(value.get("revision_authority")):
            raise issue_refuse("data_requires_issue_revision")
        if not isinstance(value["issue_revision_data"], dict):
            raise issue_refuse("malformed_body_data")
        try:
            data_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise issue_refuse("malformed_body_data") from None
        if data_size > MAX_RECORD:
            raise issue_refuse("data_input_too_large")
    if requested(value.get("revision_authority")) and "dispositions" in value:
        raise issue_refuse("combined_operation")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        previous = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
        try:
            core, proposed, dispositions = _checkpoint_input(project, objective, owner, value, native, previous)
            revision_data = core.pop("issue_revision_data", None)
            if "display" in core:
                from .display import validate_record
                validate_record(core["display"], state["admissions"])
        except PodError as exc:
            _map_refusal_context(exc, state)
            raise
        if state["owner"] not in (None, owner):
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, state, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Caller is not the recorded owner; another coordinator owns this objective")
        recorded_refs = {(row["runId"], row["runtime"]) for row in previous.get("native_refs", [])
                         if isinstance(row, dict)}
        from .issue_revision import Revision
        issue_requested = requested(proposed.get("revision_authority"))
        authority = require_authority(project, objective, owner=owner, state=state,
                                      expected_runtime=native["runtime"],
                                      persist_rebind=not (issue_requested or isinstance(proposed, dict) and proposed.get("close") is True))
        if issue_requested and authority.get("continuity") is not None:
            raise issue_refuse("native_continuity_requires_separate_checkpoint")
        if native["runtime"] != authority["runtime"]:
            raise PodError("native_authority_unverified", "Checkpoint runtime differs from current Run")
        supplied = core["native_refs"]
        if (not isinstance(supplied, list) or len(supplied) > bound("checkpoint", "native_refs", "max_items")
                or any(not isinstance(row, dict) or not isinstance(row.get("runId"), str)
                       or not isinstance(row.get("runtime"), str) for row in supplied)):
            raise PodError("native_authority_unverified", "Checkpoint Run references are malformed")
        current_refs = {(run, runtime) for run, runtime in authority["references"].items()}
        # A coordinator may restate the references it last saw, from before this call's rebind.
        if supplied and {(row["runId"], row["runtime"]) for row in supplied} not in (
                current_refs, recorded_refs if authority.get("continuity") else current_refs):
            raise PodError("native_authority_unverified", "Checkpoint cannot invent or drop Run references")
        core = {**core, "native_refs": [{"runId": run, "runtime": runtime}
                                         for run, runtime in sorted(authority["references"].items())]}
        if "verification" not in core and "verification" in previous:
            # Like the map, the verification context carries forward until it is restated.
            core["verification"] = previous["verification"]
        source = core.get("objective_source") or {}
        if source.get("role") == "SEAL" and source != previous.get("objective_source"):
            if not previous or previous.get("objective_source") is not None:
                raise PodError("seal_completion_binding_refused", "Bind a SEAL only to its existing direct objective",
                               {"next_action": "the project lead uses the direct objective that owns this SEAL's completion"})
            from .github import issue_recheck_live
            checked, _ = issue_recheck_live(project, source)
            if checked.get("body_changed") or checked.get("amendment_changed"):
                raise PodError("issue_reconciliation_required", "The SEAL completion binding changed during rebind")
        issue_revision = Revision(project, objective, owner, state, core, proposed, data=revision_data) if issue_requested else None
        if issue_revision is not None:
            proposed = {**proposed, "revision_authority": issue_revision.authority}
        from .pod_ticks import refresh as refresh_ticks
        prior_criteria = {row["id"] for row in (map_of(state) or {}).get("obligations", []) if row["kind"] == "criterion"}
        raw_obligations = proposed.get("obligations")
        introducing = isinstance(raw_obligations, list) and any(
            isinstance(row, dict) and row.get("kind") == "criterion" and row.get("id") not in prior_criteria
            for row in raw_obligations)
        rebound = core.get("objective_source") != previous.get("objective_source")
        if core.get("objective_source") is not None and (introducing or rebound or issue_requested):
            refresh_ticks(state, project, core["objective_source"])
        try:
            map_state, ctx = _checkpoint_map_transition(
                project, objective, state, authority, core, previous, proposed, dispositions, native, issue_revision)
        except PodError as exc:
            _map_refusal_context(exc, state)
            from .worker_tabs import rows as tab_rows
            tabs = tab_rows(state, {item["admission_id"]: item["placement"]
                            for item in authority.get("native", {}).get("assignments", [])
                            if isinstance(item.get("placement"), dict)},
                            outstanding=_outstanding_ids(state, authority.get("native")))
            if tabs:
                exc.detail = {**(exc.detail or {}), "worker_tabs": tabs}
            raise
        state["owner"] = owner
        identity = running_identity()
        if identity["bundle_digest"] is None:
            raise PodError("installed_version_changed", "Running bundle is incomplete; reload the skill and write a fresh checkpoint")
        continuity = _observed_continuity(state, authority, owner)
        state["checkpoint"] = {**core, **map_state, "objective": objective,
                               "pod_version": identity["version"],
                               "bundle_digest": identity["bundle_digest"],
                               **({"continuity": continuity} if continuity else {})}
        from .pod_ticks import bind_criterion_scopes
        bind_criterion_scopes(state, previous, verified=(issue_revision.scope_bindings if issue_revision else None))
        _write(path, state, native=authority.get("native"),
               before_publish=(lambda: issue_revision.consume(project, objective, owner, path, state)) if issue_revision else None)
        if isinstance(proposed, dict) and proposed.get("close") is True:
            _record_continuity_event(authority.get("continuity"))
        result = dict(state)
        # Acknowledgements use the accepted closure/dispositions with the same
        # native facts; the pre-write rows can still promise a future report.
        from .worker_tabs import rows as tab_rows
        placements = {item["admission_id"]: item["placement"]
                      for item in authority.get("native", {}).get("assignments", [])
                      if isinstance(item.get("placement"), dict)}
        result["worker_tabs"] = tab_rows(state, placements, outstanding=(ctx or {}).get("outstanding", []))
        from .pod_ticks import intake as tick_intake, projection as tick_projection
        result["pod_ticks"] = tick_projection(state, map_state, ctx or {}, project=project, objective=objective)
        if introducing or rebound or issue_requested:
            result["pod_tick_intake"] = tick_intake(state, map_state)
        if map_state:
            result["report"] = report_projection(state["checkpoint"], ctx, standing_normal=True)
        if (ctx or {}).get("closure_preserved"):
            # Named in the closing write's own answer, as its closure records them (C11).
            result["closure_preserved"] = [dict(row) for row in ctx["closure_preserved"]]
        if (ctx or {}).get("governance_kept"):
            # What the closing write did not adopt, said in its own answer (R147).
            result["governance_adoption"] = dict(ctx["governance_kept"])
        from .standing import projection as standing_projection
        from .governor import _read_journal, _record_path
        try:
            journal = _read_journal(_record_path(project, objective))
        except PodError:
            journal = None
        result["standing_authorization"] = standing_projection(state, journal)
        if map_state.get("closure"):
            from .evidence_closure import close_source_issues
            result.update(close_source_issues(project, objective, locked=True))
        return result


def _observed_continuity(state: dict, authority: dict, owner: str) -> dict | None:
    """An authority-joining checkpoint records the generation it observed for the coordinator's Run."""
    previous = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
    record = previous.get("continuity")
    observed = (authority.get("native") or {}).get("binding")
    if (isinstance(observed, dict) and observed.get("id") in authority["references"]
            and observed.get("coordinator_handle") == owner
            and type(observed.get("consumer_generation")) is int):
        record = {"binding": {"run": observed["id"], "coordinator": owner,
                              "generation": observed["consumer_generation"]},
                  "history": list((record or {}).get("history", []))}
    return record


def admission_identity(*, objective: str, run_id: str, task_id: str,
                       packet_id: str | None = None, plan_revision: str | None = None,
                       retry_of_dispatch_id: str | None = None,
                       retry_after: str | None = None) -> str:
    """Re-entry identity excludes mutable preference, Governor and version revisions."""
    identity = {"objective": objective, "run": run_id, "task": task_id}
    if retry_of_dispatch_id is not None:
        identity["retry_of"] = retry_of_dispatch_id
    if retry_after is not None:
        identity["retry_after"] = retry_after
    return digest(identity)


def retry_parent(state: dict, *, objective: str, owner: str, run_id: str,
                 task_id: str, retry_of: str) -> dict:
    """Resolve an owned admission, never a caller-supplied native Dispatch."""
    prior = state["admissions"].get(retry_of) if isinstance(retry_of, str) else None
    binding = prior.get("native_binding") if isinstance(prior, dict) else None
    if (not prior or prior["owner"] != owner or prior["objective"] != objective
            or prior["run_id"] != run_id or prior["task_id"] != task_id
            or prior["state"] not in ("bound", "closed") or not binding_valid(binding)
            or binding["runId"] != run_id or binding["taskId"] != task_id):
        raise PodError("retry_parent_unverified", "Retry needs an owned bound admission on this objective, Run and Task. Next: inspect the original admission and exact native Dispatch")
    from .orca import _argument
    if not _argument(binding["dispatchId"]):
        raise PodError("retry_parent_unverified", "Retry parent Dispatch cannot pass exactly to Orca")
    return prior


def logical_projection(project: Path, native: dict, *, objective: str,
                       tasks: list[str] | None = None) -> dict:
    """Project objective-local outstanding assignments from exact attempt settlement."""
    if (native.get("scope") != "objective_assignments" or native.get("complete") is not True
            or not isinstance(native.get("runtime"), str)
            or not isinstance(native.get("assignments"), list)):
        raise PodError("native_assignment_unverified", "Objective assignment evidence is incomplete")
    state = read(project, objective)
    admissions = state.get("admissions", {}) if isinstance(state, dict) else {}
    outstanding = []
    for admission_id in (_outstanding_ids(state, native) if isinstance(state, dict) else []):
        admission = admissions[admission_id]
        binding = admission.get("native_binding")
        task = binding.get("taskId") if isinstance(binding, dict) else admission["task_id"]
        if tasks is not None and task not in tasks:
            continue
        outstanding.append({**admission["request"], "objective": objective,
                            "admission_id": admission_id, "task": task,
                            "state": admission["state"], "role": admission["role"],
                            "candidate": admission["candidate"]})
    return {"schema": "pod-logical-projection/v1", "runtime": native["runtime"],
            "authoritative": native.get("authoritative") is True,
            "owner": native.get("owner") if native.get("authoritative") is True else None,
            "outstanding": outstanding,
            "outstanding_ids": [row["admission_id"] for row in outstanding],
            "physical_capacity": native.get("physical_capacity", "unavailable")}


_SETTLED_OUTCOME_STATUSES = {
    "succeeded": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "stopped": frozenset({"failed"}),
    "canceled": frozenset({"canceled", "cancelled"}),
    "cancelled": frozenset({"canceled", "cancelled"}),
}


def _native_assignment_settled(shown: dict) -> bool:
    """Require a terminal attempt, not one success-specific stage detail."""
    result = shown.get("result") if isinstance(shown, dict) else None
    projection = result.get("projection") if isinstance(result, dict) else None
    dispatch = result.get("dispatch") if isinstance(result, dict) else None
    if not isinstance(projection, dict) or not isinstance(dispatch, dict):
        return False
    stage = projection.get("stage")
    if not isinstance(stage, dict):
        return False
    outcome = projection.get("outcome")
    status = dispatch.get("status")
    return (isinstance(outcome, str) and outcome in _SETTLED_OUTCOME_STATUSES
            and isinstance(status, str) and status in _SETTLED_OUTCOME_STATUSES[outcome]
            and ("dispatch" not in stage or stage["dispatch"] == status)
            and isinstance(dispatch.get("id"), str)
            and projection.get("dispatchId") == dispatch["id"]
            and isinstance(dispatch.get("runId"), str)
            and projection.get("runId") == dispatch["runId"]
            and isinstance(dispatch.get("taskId"), str)
            and projection.get("taskId") == dispatch["taskId"])


def _reserve_route(project: Path, checkpoint_value: dict, body: dict, state: dict,
                   requested: dict, task_id: str, moment: datetime, projection: dict,
                   failures: list, reuse_of: str | None, native: dict) -> dict:
    from .config import load
    from .selection import validate_choice, worker_ceiling
    # The personal file is read after every other check under the objective lock.
    snapshot = load(project)
    if snapshot["policy_revision"] != checkpoint_value.get("policy_revision"):
        raise policy_mismatch("Governor policy changed since checkpoint",
                              checkpoint=checkpoint_value.get("policy_revision"),
                              packet=body.get("policy_revision"), effective=snapshot["policy_revision"])
    if body.get("policy_revision") != snapshot["policy_revision"]:
        raise policy_mismatch("Packet Governor policy changed",
                              checkpoint=checkpoint_value.get("policy_revision"),
                              packet=body.get("policy_revision"), effective=snapshot["policy_revision"])
    revision_changed = body.get("route", {}).get("preference_revision") != snapshot["revision"]
    if snapshot.get("setup"):
        # A kept 0.6.x file names its route setup rather than an empty worker ceiling.
        raise PodError("setup_required", snapshot["errors"][0]["message"])
    ceiling = worker_ceiling(snapshot, state["constraints"])
    if len(projection["outstanding"]) >= ceiling:
        raise PodError("logical_capacity_full", "Objective logical worker ceiling is occupied by outstanding attempts. Next: inspect exact native settlement or wait for those attempts")
    if "delegate" in body.get("actions", []) and not any(
            c["kind"] == "allow_delegation" and c["provenance"] == "user_direct" and c.get("active", True)
            for c in state["constraints"]):
        raise PodError("delegation_unauthorized", "Worker delegation lacks direct user intent")
    checked = validate_choice(snapshot, state["constraints"], failures, requested,
                              task=task_id, role=body.get("responsibility"), now=moment)
    if not checked["allowed"]:
        if revision_changed:
            raise PodError("preference_changed",
                           f"Preferences changed before dispatch and route is no longer allowed: {checked['code']}. "
                           "Next: reread pod config --json and rechoose at most twice before reporting the conflict")
        raise PodError(checked["code"],
                       f"Proposed route is not allowed at the current preference revision: {checked['code']}"
                       + (". Next: choose a supported exact effort; native_default effort is not a new route"
                          if checked["code"] == "effort_required" else "")
                       + (f"; reconsider at {checked['reconsider_at']} (no automatic start)"
                          if checked.get("reconsider_at") else ""))
    if revision_changed:
        raise PodError("preference_revision_stale",
                       "Preferences changed before dispatch. Next: reread pod config --json and rechoose at most twice before reporting the conflict")
    if reuse_of is not None:
        prior = state["admissions"].get(reuse_of)
        if (prior is None or prior["state"] not in ("bound", "closed")
                or not binding_valid(prior.get("native_binding"))
                or not prior["native_binding"]["terminalHandle"]):
            raise PodError("reuse_unavailable", "Terminal reuse needs a settled, known prior attempt. Next: read the prior exact native attempt and retain uncertainty until settlement")
        matches = [a for a in native["assignments"] if a.get("admission_id") == reuse_of]
        if len(matches) != 1 or matches[0].get("settled") is not True:
            raise PodError("reuse_unavailable", "Prior attempt has not settled natively. Next: read the exact Dispatch settlement before terminal reuse")
        effective_route = prior["route_decision"].get("effective", {})
        if any(effective_route.get(key) in (None, "unknown")
               or requested[key] != effective_route[key] for key in ("agent", "model", "effort")):
            raise PodError("reuse_route_changed", "Terminal reuse cannot switch the prior effective model")
    return snapshot


def _reserve_record(project: Path, objective: str, owner: str, admission_id: str,
                    requested: dict, snapshot: dict, state: dict, body: dict, checkpoint_value: dict,
                    map_state: dict,
                    ctx: dict, placement_binding: dict, moment: datetime, native: dict,
                    run_id: str, task_id: str, plan_revision: str, packet_id: str,
                    worktree: str, reuse_of: str | None, accompanying: dict | None,
                    placement_naming: dict | None = None, retry_of: str | None = None,
                    retry_after: str | None = None) -> tuple[dict, dict]:
    from . import __version__
    from .catalog import route_key
    from .obligations import admit, reserved_admission_shape
    stamp = moment.isoformat()
    pinned = snapshot.get("pinned")
    # Earlier rows keep `mode` and model-only `pinned_model`; new rows add the exact routes.
    decision = {"agent": requested["agent"], "model": requested["model"],
                "requested_effort": requested["effort"], "requested_context": requested["context"],
                "reason": requested["reason"] + ("; personal pin selected the route" if pinned else ""),
                "preference_revision": snapshot["revision"],
                "policy_revision": snapshot["policy_revision"],
                "pinned_model": pinned.split("/")[1] if pinned else None,
                "route": route_key(requested["agent"], requested["model"], requested["effort"]),
                "preferred_route": snapshot.get("preferred"), "pinned_route": pinned,
                "preference_schema": snapshot.get("schema"),
                "constraint_refs": [c.get("id") for c in state["constraints"] if c.get("active", True)],
                "pod_version": __version__, "effective": {"agent": "unknown", "model": "unknown",
                "effort": "unknown", "context": "unknown"},
                "route_mismatch": False, "effective_unknown": True}
    new_map = admit(map_state, body, ctx, admission_id=admission_id, accompanying=accompanying)
    shape = reserved_admission_shape(body, ctx, map_state)
    worktree_path = placement_binding.get("path") if isinstance(placement_binding, dict) else None
    result = ({"base": git_head(worktree_path), "head": None, "worktree": worktree_path}
              if body["role"] == "implement" else None)
    row = {"schema": ADMISSION_SCHEMA, "admission_id": admission_id,
           "objective": objective, "owner": owner, "request": requested,
           "route_decision": decision, "effective_evidence": {}, "runtime": native["runtime"],
           "request_uuid": None, "run_id": run_id, "task_id": task_id,
           "plan_revision": plan_revision, "packet_id": packet_id, "worktree": worktree,
           "reuse_of": reuse_of, "native_binding": None,
           "recovery": {"checkpoint_binding": {key: checkpoint_value.get(key) for key in
                                             ("candidate", "criteria", "plan_revision", "policy_revision",
                                              "objective_source", "worktree")},
                        "placement_binding": placement_binding},
           "error": None, "failures": [], "created_at": stamp, "updated_at": stamp,
           **shape, "map_revision": body["map_revision"], "result": result,
           "boundary_exceeded": [], "admitted_seq": new_map["seq"]}
    from .display import admission_facts
    row["display"] = admission_facts(state, body, worktree_path, placement_naming)
    if retry_of is not None:
        row["retry_of"] = retry_of
        row["recovery"]["retry_of_dispatch_id"] = state["admissions"][retry_of]["native_binding"]["dispatchId"]
    if retry_after is not None:
        row["retry_after"] = retry_after
    return row, new_map


def reserve(project: Path, objective: str, *, owner: str, admission_id: str,
            requested: dict, native_reader: Callable[[dict], dict], run_id: str,
            task_id: str, plan_revision: str, packet_id: str, worktree: str,
            frozen_packet: dict, expected_runtime: str, placement_binding: dict,
            reuse_of: str | None = None, accompanying: dict | None = None,
            now: datetime | None = None, continuity_port=None,
            placement_naming: dict | None = None, retry_of: str | None = None,
            retry_after: str | None = None) -> dict:
    """Final serialized admission: preference read is last, before writing the row.

    The served obligations become active in the same locked write that persists the
    admission row, so a map can never name an admission that was not recorded.
    """
    from .selection import failure_active
    from .issue_revision import refuse_alternate
    refuse_alternate(accompanying)
    moment = now or datetime.now(timezone.utc)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] not in (None, owner):
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, state, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Caller is not the recorded owner; another coordinator owns this objective")
        refs = _run_references(state)
        if run_id not in refs:
            raise PodError("native_authority_unverified", "Admission Run is not an exact objective reference")
        if retry_of is not None and reuse_of is not None:
            raise PodError("retry_reuse_conflict", "retry_of requires a fresh terminal; omit reuse_of")
        parent = (retry_parent(state, objective=objective, owner=owner, run_id=run_id,
                               task_id=task_id, retry_of=retry_of) if retry_of is not None else None)
        if retry_after is not None:
            from .operations import _retry_after
            _retry_after(project, state, parent, retry_after)
        if parent is not None and admission_id != admission_identity(
                objective=objective, run_id=run_id, task_id=task_id,
                retry_of_dispatch_id=parent["native_binding"]["dispatchId"], retry_after=retry_after):
            raise PodError("admission_conflict", "Retry identity does not name its exact native parent")
        existing = state["admissions"].get(admission_id)
        if existing is not None:
            if (existing["objective"] != objective or existing["run_id"] != run_id
                    or existing["task_id"] != task_id or existing["packet_id"] != packet_id
                    or existing["plan_revision"] != plan_revision or existing["worktree"] != worktree
                    or existing.get("retry_of") != retry_of or existing.get("retry_after") != retry_after):
                raise PodError("admission_conflict", "Admission identity was reused for different work")
            return {**existing, "existing": True}
        if any(row["task_id"] == task_id and row["state"] in ("reserved", "unresolved")
               for row in state["admissions"].values()):
            raise PodError("unresolved_prior_attempt", "An unsettled Task cannot be replaced")
        _require_open(state)
        from .evidence_closure import refuse_objective
        refuse_objective(project, objective)
        if any(row["phase"] != "completed" for row in state.get("pod_ticks", {}).get("requests", {}).values()):
            raise PodError("pod_tick_pending", "Recover the pending pod internal pod-tick request before admission")
        map_state = map_of(state)
        if map_state is None:
            from .obligations import refuse
            raise refuse("unbound_assignment", "no_map", "delegation needs an obligation map first", record='reserve')
        from .bundle import require_current_identity
        require_current_identity(state.get("checkpoint"))
        # New worker admission reads through its own native reader; a changed runtime is
        # classified here, under the lock, before the runtime checks below (A2).
        try:
            native = native_reader(state)
        except PodError:
            if (continuity_port is None
                    or _continuity_step(project, objective, state, owner=owner, port=continuity_port,
                                        expected_runtime=expected_runtime) is None):
                raise
            native = native_reader(state)
            refs = _run_references(state)
        else:
            if (continuity_port is not None and native.get("runtime") != refs[run_id]
                    and _continuity_step(project, objective, state, owner=owner, port=continuity_port,
                                         current=native, expected_runtime=expected_runtime) is not None):
                native = native_reader(state)
                refs = _run_references(state)
        if (native.get("authoritative") is not True or native.get("owner") != owner
                or native.get("scope") != "objective_assignments" or native.get("complete") is not True
                or not isinstance(native.get("runtime"), str)):
            raise PodError("native_authority_unverified", authority_message(state, native, owner))
        if native["runtime"] != expected_runtime:
            raise PodError("orca_runtime_changed", "Launch capability and native authority changed runtime")
        if native["runtime"] != refs[run_id]:
            raise PodError("native_authority_unverified", "Admission Run runtime differs from objective")
        checkpoint_value = state.get("checkpoint")
        body = frozen_packet.get("body") if isinstance(frozen_packet, dict) else None
        differing = []
        if not isinstance(body, dict):
            differing.append("packet_id")
        else:
            if frozen_packet.get("packet_id") != packet_id:
                differing.append("packet_id")
            differing.extend(key for key in ("candidate", "criteria", "objective_source", "worktree")
                             if body.get(key) != checkpoint_value.get(key))
            if body.get("plan_revision") != plan_revision:
                differing.append("plan_revision")
        if differing:
            reads = {key: ("pod internal packet: freeze the corrected body; use its packet_id" if key == "packet_id"
                           else "pod internal map: " + key) for key in differing}
            action = ("read the named fields from pod internal map in the objective's project; correct the packet body "
                      "and the admission plan_revision, then call pod internal packet to freeze it and retry this admission")
            raise PodError("packet_plan_mismatch", "Packet differs in fields: " + ", ".join(differing)
                           + ". Next: " + action,
                           {"fields": differing, "reads": reads, "next_action": action})
        bound_sources = [*body["sources"], *({"path": ref["path"], "state": "present",
                          "sha256": ref["sha256"]} for ref in body["context"]
                         if ref["kind"] in ("source", "instruction"))]
        _check_bound_sources_locked(project, path, state, packet_id, bound_sources)
        if parent is not None:
            matches = [item for item in native["assignments"] if item.get("admission_id") == retry_of]
            binding = parent["native_binding"]
            if (len(matches) != 1 or matches[0].get("runtime") != parent["runtime"]
                    or any(matches[0].get(field) != binding[key] for field, key in
                           (("run_id", "runId"), ("task_id", "taskId"),
                            ("dispatch_id", "dispatchId"), ("worker_id", "workerId")))
                    or matches[0].get("retry_eligible") is not True):
                raise PodError("retry_parent_unsettled", "Retry parent needs exact failed/stopped settlement, positively exited agent and known resources. Next: inspect its native worker and complete any uncertain stop or release through Orca")
            if any(failure.get("kind") == "auth_failed" and failure.get("task") == task_id
                   and failure_active(failure, now=moment)
                   for prior in state["admissions"].values() for failure in prior["failures"]):
                raise PodError("route_failed", "An active authentication refusal holds this Task's retry across routes. Next: resolve authentication through its existing user/runtime-change or retry-after path; do not reroute the refusal")
        projection = logical_projection(project, native, objective=objective)
        for prior in state["admissions"].values():
            if prior["task_id"] != task_id or prior["state"] != "bound":
                continue
            matches = [item for item in native["assignments"]
                       if item.get("admission_id") == prior["admission_id"]]
            if len(matches) != 1 or matches[0].get("settled") is not True:
                raise PodError("unresolved_prior_attempt", "A live bound Task cannot be replaced")
        failures = [failure for prior in state["admissions"].values() for failure in prior["failures"]]
        if any(f.get("kind") == "safety_refusal" and f.get("task") == task_id and not f.get("cleared_at")
               for f in failures):
            raise PodError("safety_refusal", "A safety refusal bars another model on this Task")
        for prior in state["admissions"].values():
            if prior["request"].get("model") == requested.get("model"):
                continue
            if not any(failure_active(f, now=moment) for f in prior["failures"]):
                continue
            if prior["state"] == "deferred":
                continue
            matches = [item for item in native["assignments"]
                       if item.get("admission_id") == prior["admission_id"]]
            if len(matches) != 1 or matches[0].get("settled") is not True:
                raise PodError("failed_attempt_unsettled", "Settle the failed attempt before an alternative route")
        from .obligations import admission_refusal
        # The caller verified launch-preference capability for this runtime before reserving.
        ctx = kernel_context(project, objective, state, native, candidate=checkpoint_value.get("candidate"),
                             delegation="available")
        ctx["operation"] = "admission"
        admission_refusal(map_state, body, ctx, admission_id=admission_id)
        require_governance_current(project, map_state)
        snapshot = _reserve_route(project, checkpoint_value, body, state, requested, task_id,
                                  moment, projection, failures, reuse_of, native)
        row, new_map = _reserve_record(
            project, objective, owner, admission_id, requested, snapshot, state, body,
            checkpoint_value, map_state,
            ctx, placement_binding, moment, native, run_id, task_id, plan_revision, packet_id,
            worktree, reuse_of, accompanying, placement_naming, retry_of=retry_of, retry_after=retry_after)
        state["owner"] = owner
        state["admissions"][admission_id] = row
        state["checkpoint"] = {**checkpoint_value, **new_map}
        from .pod_ticks import bind_criterion_scopes
        bind_criterion_scopes(state, checkpoint_value)
        _write(path, state, native=native)
        return {**row, "existing": False}


def update_admission(project: Path, objective: str, *, owner: str, admission_id: str,
                     update: Callable[[dict], None], open_required: bool = False,
                     expected_runtime: str | None = None) -> dict:
    """``expected_runtime`` is the runtime at which the update's native evidence was read.

    Such an update applies only when its own authority join ends on that runtime, so a
    rebind to another runtime, inside the join or by another call before it, refuses.
    """
    path = _path(project, objective)
    with _lock(path):
        return _update_admission_locked(project, objective, state=_read(path), owner=owner,
                                        admission_id=admission_id, update=update,
                                        open_required=open_required, expected_runtime=expected_runtime)


def _update_admission_locked(project: Path, objective: str, *, state: dict, owner: str,
                             admission_id: str, update: Callable[[dict], None],
                             open_required: bool = False, expected_runtime: str | None = None) -> dict:
    """The existing admission update, for a caller already holding the objective lock."""
    if state["owner"] != owner or admission_id not in state["admissions"]:
        from .handoff import refuse_nonowner
        refuse_nonowner(project, objective, state, owner, code="unknown_admission")
        raise PodError("unknown_admission", "No owned admission identity")
    if open_required:
        _require_open(state)
    authority = require_authority(project, objective, owner=owner, state=state,
                                  run_id=state["admissions"][admission_id]["run_id"],
                                  expected_runtime=expected_runtime)
    if expected_runtime is not None and authority["runtime"] != expected_runtime:
        raise PodError("native_authority_unverified", "Update evidence was read at another runtime")
    row = state["admissions"][admission_id]
    from copy import deepcopy
    display_before = ("display" in row, deepcopy(row.get("display")))
    admitted = ("admitted_seq" in row, row.get("admitted_seq"))
    update(row)
    if admitted != ("admitted_seq" in row, row.get("admitted_seq")):
        raise PodError("admission_conflict", "The admission's reservation sequence is immutable")
    if display_before != ("display" in row, row.get("display")):
        raise PodError("admission_conflict", "The admission display facts are immutable")
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(_path(project, objective), state)
    return row


def _stored_ingestion(row: dict) -> dict:
    if row["role"] != "implement":
        return {"status": "not_applicable"}
    result = row.get("result") or {}
    if result.get("paths_status") == "over_limit":
        return {"status": "over_limit", "reason": "changed_paths_limit", "count": result["path_count"]}
    return {"status": "recorded", "changed_paths": row["changed_paths"],
            "boundary_exceeded": row["boundary_exceeded"]}


def _ingest_result(row: dict, result_commit: str | None) -> dict:
    from .obligations import exceeded
    base = (row.get("result") or {}).get("base")
    observed = git_result((row.get("result") or {}).get("worktree"), base, result_commit)
    if observed["status"] == "observed":
        row["result"] = {**row["result"], "head": observed["head"], "paths_status": "observed"}
        row["changed_paths"] = observed["changed_paths"]
        row["boundary_exceeded"] = exceeded(observed["changed_paths"], row["boundary"])
        return {"status": "recorded", "changed_paths": row["changed_paths"],
                "boundary_exceeded": row["boundary_exceeded"], "head": observed["head"],
                "committed": observed["head"] != base}
    if observed["status"] == "over_limit":
        row["result"] = {**row["result"], "head": observed["head"],
                         "paths_status": "over_limit", "path_count": observed["count"]}
    return observed


def _report_written_ids(map_state: dict, admission_id: str) -> dict:
    """Project the accepted map's report identities, never reconstruct request policy."""
    findings = [finding for row in map_state["obligations"] for finding in row.get("findings", [])
                if finding.get("attempt") == admission_id]
    return {"findings": sorted({identity for finding in findings
                                for identity in finding.get("findings", [finding["finding"]])}),
            "corrections": sorted({finding["correction"] for finding in findings if "correction" in finding}),
            "proposals": [row["id"] for row in map_state["proposals"]
                          if row.get("origin_ref") == admission_id[:64]]}


def consume_report(project: Path, objective: str, *, owner: str, admission_id: str,
                   observation: dict | None, accompanying: dict | None = None, findings: list | None = None,
                   proposals: list | None = None, result_commit: str | None = None,
                   route_refresh: dict | None = None, route_mismatch: bool = False,
                   report_placement: dict | None = None, packet_body: dict | None = None,
                   prepare=None) -> dict:
    """Report consumption is a map write: Git result ingestion, triage and proposals, atomically.

    The worker report is an observation. Changed paths come from Git, never from the
    report; findings and discoveries become corrections or proposals by the coordinator's
    triage; nothing in the report changes a criterion, gate or authorization.
    """
    from .obligations import accept_write, refuse, report_projection, triage
    from .issue_revision import refuse_alternate
    refuse_alternate(accompanying)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        row = state["admissions"].get(admission_id)
        if state["owner"] != owner or row is None:
            raise PodError("unknown_admission", "No owned admission identity")
        prepared_continuity = None
        if prepare is not None:
            prepared = prepare(state, row)
            observation = prepared["observation"]
            route_refresh, route_mismatch = prepared["route_refresh"], prepared["route_mismatch"]
            report_placement = prepared["report_placement"]
            prepared_continuity = prepared["continuity"]
        if route_mismatch:
            _update_admission_locked(project, objective, state=state, owner=owner,
                                     admission_id=admission_id,
                                     update=lambda item: item["route_decision"].update(route_mismatch=True),
                                     expected_runtime=row["runtime"])
            raise PodError("route_mismatch", "Native effective route changed after start")
        # The report path read its fresh identity at the observation's runtime (A2).
        observed_runtime = (observation.get("native_binding") or {}).get("runtime")
        closing = isinstance(accompanying, dict) and accompanying.get("close") is True
        authority = require_authority(project, objective, owner=owner, state=state, run_id=row["run_id"],
                                      expected_runtime=observed_runtime, persist_rebind=not closing)
        if observed_runtime is not None and authority["runtime"] != observed_runtime:
            raise PodError("native_authority_unverified", "Report identity was read at another runtime")
        map_state = map_of(state)
        if map_state is None:
            raise refuse("unbound_assignment", "no_map", "a report joins an obligation map", record='consume report')
        ctx = kernel_context(project, objective, state, authority.get("native"))
        ctx["operation"] = "report"
        try:
            _require_open(state)
        except PodError as exc:
            if ctx["worker_tabs"]:
                exc.detail = {**(exc.detail or {}), "worker_tabs": ctx["worker_tabs"]}
            raise
        from .worker_tabs import project as tab_project, rows as tab_rows
        if closing:
            from .pod_ticks import closing_context, require_tabs
            closing_context(state, map_state, ctx, project=project, objective=objective)
            require_tabs(ctx["worker_tabs"], map_state, accompanying, ctx)
        if route_refresh:
            row["route_decision"].update(effective=route_refresh["effective"], effective_unknown=False)
            row["effective_evidence"]["effective"] = route_refresh["effective"]
            if route_refresh["inherited"] is not None:
                row["effective_evidence"]["inherited"] = route_refresh["inherited"]

        def tab_output():
            # Report acknowledgement uses the handler's original worker-show,
            # with the newly accepted correction records; it reads nothing more.
            placements = {item["admission_id"]: item["placement"]
                          for item in authority.get("native", {}).get("assignments", [])
                          if isinstance(item.get("placement"), dict)}
            if report_placement is not None:
                placements[admission_id] = report_placement
            tabs = tab_rows(state, placements, outstanding=ctx["outstanding"])
            worker = tab_project(state, row, placements.get(admission_id, {}), settled=True,
                                 packet_body=packet_body, acknowledgement=True)
            tabs = [worker if item["admission"] == admission_id else item for item in tabs]
            from .pod_ticks import projection as tick_projection
            return {"worker_tabs": tabs, "worker_tab": worker,
                    "pod_ticks": tick_projection(state, map_of(state), ctx, project=project, objective=objective)}
        settled = admission_id not in ctx["outstanding"]
        if not settled:
            raise PodError("report_attempt_unverified", "The exact Dispatch has not settled natively. Next: read its native settlement before consuming the report")
        report_value = observation.get("observation", {})
        report_identity = digest(observation)
        prior_report = row.get("report")
        if prior_report is not None and (prior_report.get("observation_digest") != report_identity
                                         or prior_report.get("result_commit") != result_commit):
            raise PodError("report_conflict", "A consumed Dispatch report or result cannot be replaced")
        if prior_report is not None and not accompanying and not findings and not proposals:
            ingestion = _stored_ingestion(row)
            return {"ingestion": ingestion, "settled": True,
                    "written": _report_written_ids(map_state, admission_id),
                    "map": {"seq": map_state["seq"], "revision": map_state["revision"],
                            "quiescence": map_state["quiescence"]},
                    "report": report_projection(map_state, ctx, standing_normal=True), **tab_output()}
        ingestion = {"status": "not_applicable"}
        if row["role"] == "implement":
            if prior_report is not None:
                ingestion = _stored_ingestion(row)
            elif row["changed_paths"] is not None:
                ingestion = _stored_ingestion(row)
            else:
                ingestion = _ingest_result(row, result_commit)
        if prior_report is None:
            row["report"] = {"outcome": report_value.get("outcome"), "status": observation.get("status"),
                             "attempt": report_value.get("attempt"),
                             "observation": report_value,
                             "observation_digest": report_identity, "result_commit": result_commit,
                             "consumed_at": datetime.now(timezone.utc).isoformat()}
            row["updated_at"] = row["report"]["consumed_at"]
        from .governance import completion_governance
        kept = None
        if closing:
            kept, _ = completion_governance(project, map_state)
        else:
            require_governance_current(project, map_state)
        if kept is not None:
            ctx["governance_kept"] = kept
        try:
            value, triaged = triage(map_state, dict(accompanying or {}), findings or [], proposals or [], ctx,
                                    admission_id=admission_id)
            if (prior_report is not None and not accompanying and value.get("obligations") == map_state["obligations"]
                    and value.get("proposals") == map_state["proposals"]):
                return {"ingestion": _stored_ingestion(row), "settled": True,
                        "written": _report_written_ids(map_state, admission_id),
                        "map": {"seq": map_state["seq"], "revision": map_state["revision"], "quiescence": map_state["quiescence"]},
                        "report": report_projection(map_state, ctx, standing_normal=True), **tab_output()}
            if value.get("close") is True and not map_state.get("closure"):
                from .succession import refuse_incomplete
                refuse_incomplete(project, objective, state, full=True)  # As a checkpoint's new closure (R118).
                ctx["closure_unrecorded"] = _closure_unrecorded(project, objective, state,
                                                                delivered=map_state.get("delivery") is not None)
            new_map = accept_write(map_state, value, ctx, triaged=triaged)
        except PodError as exc:
            _map_refusal_context(exc, state)
            if ctx["worker_tabs"]:
                exc.detail = {**(exc.detail or {}), "worker_tabs": ctx["worker_tabs"]}
            raise
        state["checkpoint"] = {**state["checkpoint"], **new_map}
        from .pod_ticks import bind_criterion_scopes
        bind_criterion_scopes(state, map_state)
        if observation.get("status") == "validated_observation" and report_value.get("outcome") == "succeeded":
            for prior in state["admissions"].values():
                if prior["admission_id"] == admission_id or prior["runtime"] != row["runtime"]:
                    continue
                for failure in prior["failures"]:
                    if (failure.get("kind") == "unavailable" and not failure.get("cleared_at")
                            and failure.get("model") == row["request"]["model"]
                            and failure.get("recorded_at", "") <= row["created_at"]):
                        failure["cleared_at"] = row["report"]["consumed_at"]
                        failure["cleared_by"] = admission_id
        _write(path, state, native=authority.get("native"), protected=ctx["outstanding"])
        if closing:
            _record_continuity_event(prepared_continuity or authority.get("continuity"))
        source_effects = {}
        if new_map.get("closure"):
            from .evidence_closure import close_source_issues
            source_effects = close_source_issues(project, objective, locked=True)
        return {"ingestion": ingestion, "settled": settled,
                "written": _report_written_ids(new_map, admission_id),
                "map": {"seq": new_map["seq"], "revision": new_map["revision"],
                        "quiescence": new_map["quiescence"]},
                "report": report_projection(state["checkpoint"], ctx, standing_normal=True),
                **({"governance_adoption": kept} if kept else {}), **source_effects, **tab_output()}


def state_inventory(project: Path) -> dict:
    """Read-only state inventory for doctor/status; it never rewrites a record."""
    root = state_root(project)
    result = {"current": 0, "unsupported": 0, "unreadable": 0, "blocked": False, "superseded": []}
    if not root.exists():
        return result
    for path in root.glob("*/context.json"):
        try:
            load_context(path)
            result["current"] += 1
        except PodError as exc:
            result["unsupported" if exc.code == "state_unsupported" else "unreadable"] += 1
        except OSError:
            result["unreadable"] += 1
    result["superseded"] = [{**row, "blocked": True, "converted": False}
                            for row in superseded_objectives(root)]
    result["blocked"] = bool(result["unsupported"] or result["unreadable"])
    return result


def superseded_for_run(run_id: str) -> list[dict]:
    """Superseded objective records that name this Run; read-only, for status."""
    root = state_root()
    rows = []
    for row in superseded_objectives(root):
        try:
            value = load_context(root / row["record"] / "context.json", validate=False)
        except (OSError, PodError):
            continue
        checkpoint_value = value.get("checkpoint") if isinstance(value, dict) else None
        refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
        admissions = value.get("admissions", {}) if isinstance(value, dict) else {}
        if (any(isinstance(ref, dict) and ref.get("runId") == run_id for ref in refs if isinstance(refs, list))
                or isinstance(admissions, dict) and any(isinstance(item, dict) and item.get("run_id") == run_id
                                                        for item in admissions.values())):
            rows.append({**row, "blocked": True, "converted": False})
    return rows


def intervention(project: Path, objective: str, *, owner: str, correction: dict,
                 task: str | None = None, diagnosis: dict | None = None,
                 unit: str | None = None) -> dict:
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, state, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Correction belongs to another coordinator")
        _require_open(state)
        require_authority(project, objective, owner=owner, state=state)
        result = _intervention_locked(project, objective, state, task=task, unit=unit,
                                      correction=correction, diagnosis=diagnosis)
        _write(path, state)
        return result


def _intervention_locked(project: Path, objective: str, state: dict, *, task: str | None,
                         unit: str | None, correction: dict, diagnosis: dict | None) -> dict:
    if (task is None) == (unit is None):
        raise PodError("invalid_intervention", "A correction names exactly one Task or delivery unit")
    exact(correction, {"criterion_id", "failure_id", "obligation", "failing_example", "hypothesis",
                       "last_meaningful_evidence", "next_discriminating_check", "correction_key"},
          {"criterion_id", "failure_id", "obligation", "failing_example", "hypothesis",
           "last_meaningful_evidence", "next_discriminating_check", "correction_key"}, name="correction")
    for item in correction.values():
        bounded_text(item, name="correction")
    if task is not None:
        bounded_text(task, name="task", limit=128)
        key = task
        if not any(row.get("state") in ("bound", "closed")
                   and isinstance(row.get("native_binding"), dict)
                   and row["native_binding"].get("taskId") == task
                   for row in state["admissions"].values()):
            raise PodError("task_unbound", "Correction Task lacks an exact native admission binding")
    else:
        bounded_text(unit, name="unit", limit=64)
        key = "unit:" + unit
        from .governor import unit_bound
        if not unit_bound(project, objective, state, unit):
            raise PodError("unit_unbound", "Correction unit has no prepared candidate")
    checkpoint_value = state.get("checkpoint")
    criteria = checkpoint_value.get("criteria") if isinstance(checkpoint_value, dict) else None
    if not isinstance(criteria, list) or correction["criterion_id"] not in criteria:
        raise PodError("criterion_unbound", "Correction criterion is absent from the accepted checkpoint")
    history = state["interventions"].setdefault(key, [])
    identity = digest(correction)
    if identity in [row["identity"] for row in history]:
        raise PodError("correction_replay", "Equivalent correction was already attempted")
    diagnosis_source = None
    if len(history) >= 2:
        if diagnosis is None:
            raise PodError("diagnosis_required", "Two equivalent failures require a discriminating diagnosis")
        exact(diagnosis, {"diagnosis_evidence"}, {"diagnosis_evidence"}, name="diagnosis")
        from .records import source_identity
        diagnosis_source = source_identity(project, diagnosis["diagnosis_evidence"])
        if diagnosis_source["state"] != "present":
            raise PodError("diagnosis_unproductive", "Diagnosis needs a present bounded evidence source")
        if diagnosis_source["sha256"] in [row.get("diagnosis_source_digest") for row in history]:
            raise PodError("diagnosis_replay", "Diagnosis evidence was already consumed")
    elif diagnosis is not None:
        raise PodError("diagnosis_unproductive", "A diagnosis is only recorded after the correction threshold")
    history.append({"identity": identity, "key": correction["correction_key"],
                    "criterion_id": correction["criterion_id"], "failure_id": correction["failure_id"],
                    "obligation": correction["obligation"], "failing_example": correction["failing_example"],
                    "hypothesis": correction["hypothesis"],
                    "next_discriminating_check": correction["next_discriminating_check"],
                    "evidence": correction["last_meaningful_evidence"],
                    "diagnosis": digest(diagnosis) if diagnosis else None,
                    "diagnosis_source": diagnosis_source,
                    "diagnosis_source_digest": diagnosis_source["sha256"] if diagnosis_source else None})
    return {"correction_identity": identity, "dispatch_authorized": False, "scope": key}
def constraints_update(project: Path, objective: str, *, owner: str,
                       action: str, value: dict | None = None, constraint_id: str | None = None) -> dict:
    from .config import load
    from .selection import validate_constraint
    path = _path(project, objective)
    if action == "list":
        state = read(project, objective)
        return {"constraints": state["constraints"] if state else []}
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, state, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Constraint belongs to another coordinator")
        tab_instruction = (action == "add" and isinstance(value, dict) and value.get("kind") == "keep_worker_tab")
        try:
            _require_open(state)
        except PodError as exc:
            # An owner's tab keep/revocation changes no map or closure report.
            # Continuation and historical retirement still refuse all writes.
            if not tab_instruction or exc.code != "objective_closed":
                raise
        require_authority(project, objective, owner=owner, state=state)
        if action == "add":
            snapshot = load(project)
            row = validate_constraint(value, snapshot)
            if row["kind"] == "keep_worker_tab" and row["value"]["objective"] != objective:
                raise PodError("invalid_constraint", "Worker-tab instruction must name this objective")
            if not isinstance(row.get("id"), str) or not row["id"] or len(row["id"]) > 128:
                raise PodError("invalid_constraint", "Constraint id is required")
            if any(c.get("id") == row["id"] for c in state["constraints"]):
                raise PodError("constraint_conflict", "Constraint id is already present")
            state["constraints"].append(row)
        elif action == "revoke":
            matches = [c for c in state["constraints"] if c.get("id") == constraint_id]
            if len(matches) != 1:
                raise PodError("unknown_constraint", "Constraint id is unavailable")
            if matches[0]["kind"] == "keep_worker_tab":
                raise PodError("constraint_authority", "Record revocation as a new user_direct keep_worker_tab instruction with keep false")
            matches[0]["active"] = False
        else:
            raise PodError("invalid_constraint_action", "Constraint action is unsupported")
        _write(path, state)
        return {"constraints": state["constraints"]}


def route_failure(project: Path, objective: str, *, owner: str, admission_id: str,
                  kind: str, source: str, retry_after: str | None = None,
                  clear: bool = False, cleared_by: str | None = None,
                  decision: dict | None = None,
                  native_reader: Callable[[dict], dict] | None = None) -> dict:
    from .selection import FAILURE_KINDS, TEMPORARY_RECONSIDERATION_SECONDS
    if kind not in FAILURE_KINDS or not isinstance(source, str) or not source or len(source) > 256:
        raise PodError("invalid_route_failure", "Failure kind and source are required")
    if kind == "safety_refusal" and retry_after is not None:
        raise PodError("invalid_route_failure", "Safety refusal has no timed retry")
    from .util import owner_decision, DECISION_FIELDS
    clear_decision = None
    if clear and cleared_by == "user":
        value = exact(decision if decision is not None else {}, DECISION_FIELDS, name="clear_decision")
        clear_decision = owner_decision(value, kind="route_failure_clear")
    elif decision is not None:
        raise PodError("invalid_route_failure", "An owner decision applies only to a user-cleared route failure")
    if retry_after is not None:
        from .util import normalize_timestamp
        retry_after = normalize_timestamp(retry_after, field="Retry-after", code="invalid_route_failure")
    def apply(row: dict) -> None:
        if clear:
            if cleared_by not in ("user", "runtime_change"):
                raise PodError("invalid_route_failure", "Clear needs user or runtime-change provenance")
            matched = False
            for failure in row["failures"]:
                if failure["kind"] == kind and not failure.get("cleared_at"):
                    failure["cleared_at"] = datetime.now(timezone.utc).isoformat()
                    failure["cleared_by"] = cleared_by
                    if clear_decision is not None:
                        failure["clear_decision"] = clear_decision
                    matched = True
            if not matched:
                raise PodError("unknown_route_failure", "No matching active failure to clear")
        else:
            if row["state"] not in ("closed", "deferred", "bound"):
                raise PodError("failure_attempt_unsettled", "Record actual failure on its own settled attempt")
            if row["state"] == "bound":
                try:
                    if native_reader is None:
                        from .operations import OrcaPort
                        native = OrcaPort(project).read_native(
                            owner, authority_runs=(row["run_id"],), assignments=(row,))
                    else:
                        native = native_reader(row)
                    projected = logical_projection(project, native, objective=objective)
                    if (native.get("authoritative") is not True or native.get("owner") != owner
                            or native.get("runtime") != row["runtime"]
                            or admission_id in projected["outstanding_ids"]):
                        raise PodError("failure_attempt_unsettled", "Native attempt is not settled")
                except PodError as exc:
                    raise PodError("failure_attempt_unsettled",
                                   "Record actual failure only after exact native settlement") from exc
            if len(row["failures"]) >= 16:
                raise PodError("route_failure_full", "Attempt failure record is full")
            observed = datetime.now(timezone.utc)
            reconsider_at = retry_after
            if kind == "unavailable" and (retry_after is None or
                    datetime.fromisoformat(retry_after.replace("Z", "+00:00")) <= observed):
                from datetime import timedelta
                reconsider_at = (observed + timedelta(seconds=TEMPORARY_RECONSIDERATION_SECONDS)).isoformat()
            row["failures"].append({"kind": kind, "source": source, "retry_after": retry_after,
                                    "reconsider_at": reconsider_at,
                                    "model": row["request"]["model"], "task": row["task_id"],
                                    "recorded_at": observed.isoformat(),
                                    "cleared_at": None, "cleared_by": None})
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply,
                            open_required=True)

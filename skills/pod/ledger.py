"""Compact objective policy/evidence state; Orca owns native lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
from typing import Callable, Iterator

from .errors import PodError
from . import gitio
from .installer import STATE_SCHEMAS, superseded_objectives, superseded_schemas
from .util import MAX_RECORD, atomic_json, bounded_json, bounded_text, digest, exact, explicit_home, native_home

ADMISSION_STATES = ("reserved", "bound", "unresolved", "closed", "deferred")
CONTEXT_SCHEMA = STATE_SCHEMAS["context"]
CHECKPOINT_SCHEMA = STATE_SCHEMAS["checkpoint"]
ADMISSION_SCHEMA = STATE_SCHEMAS["admission"]
# An obligation map lives in the checkpoint; the context record keeps its own bound.
CONTEXT_LIMIT = 4 * MAX_RECORD
_BINDING_FIELDS = {"runId", "taskId", "dispatchId", "workerId", "worktreeId", "terminalHandle"}
_CONTEXT_FIELDS = {"schema", "revision", "owner", "admissions", "checkpoint",
                   "interventions", "source_rejections", "constraints"}

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
    root = state_root(project)
    context = repository_context(project)
    if context["repo_key"] is None:
        return root / digest({"project": str(project.resolve()), "objective": objective})
    return root / digest({"repository": context["repo_key"], "objective": objective})


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


_ADMISSION_FIELDS = {"schema", "state", "admission_id", "objective", "owner", "request",
                     "route_decision", "effective_evidence", "runtime", "request_uuid",
                     "run_id", "task_id", "plan_revision", "packet_id", "worktree", "reuse_of",
                     "native_binding", "recovery", "error", "failures", "created_at", "updated_at",
                     "serves", "role", "boundary", "map_revision", "candidate", "result",
                     "changed_paths", "boundary_exceeded", "disposition", "report", "binding",
                     "admitted_seq"}


def _validate_admission(key: str, row: object) -> None:
    if isinstance(row, dict) and row.get("schema") != ADMISSION_SCHEMA:
        raise PodError("state_unsupported",
                       f"Admission uses superseded schema {str(row.get('schema'))[:64]}; it is not converted")
    value = exact(row, _ADMISSION_FIELDS, _ADMISSION_FIELDS - {"admitted_seq"}, name="admission")
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
    if value["state"] in ("bound", "closed") and not binding_valid(value["native_binding"]):
        raise PodError("state_unsupported", "Bound admission lacks exact native identity")
    if value["native_binding"] is not None and not binding_valid(value["native_binding"]):
        raise PodError("state_unsupported", "Admission native identity is malformed")


def _validate_context(value: object) -> dict:
    superseded = superseded_schemas(value)
    if superseded:
        raise PodError("state_unsupported",
                       f"Objective state uses superseded schema {', '.join(superseded)}; it is listed, "
                       "blocked and never converted. Settle its workers through Orca and start a new objective")
    state = exact(value, _CONTEXT_FIELDS, _CONTEXT_FIELDS, name="context")
    if type(state["revision"]) is not int or state["revision"] < 0:
        raise PodError("state_unsupported", "State revision is malformed")
    if state["owner"] is not None and (not isinstance(state["owner"], str) or not state["owner"]):
        raise PodError("state_unsupported", "Context owner is malformed")
    if not isinstance(state["admissions"], dict):
        raise PodError("state_unsupported", "Admissions are malformed")
    for key, row in state["admissions"].items():
        if not isinstance(key, str) or not key:
            raise PodError("state_unsupported", "Admission key is malformed")
        _validate_admission(key, row)
    if (not isinstance(state["interventions"], dict) or not isinstance(state["source_rejections"], dict)
            or not isinstance(state["constraints"], list) or len(state["constraints"]) > 64):
        raise PodError("state_unsupported", "Context evidence is malformed")
    from .selection import validate_constraint
    for row in state["constraints"]:
        validate_constraint(row)
    checkpoint_value = state.get("checkpoint")
    if isinstance(checkpoint_value, dict) and "governance_history" in checkpoint_value:
        _governance_history(checkpoint_value)
    return state


def _read(path: Path) -> dict:
    if not _record_exists(path):
        return _empty()
    return _validate_context(bounded_json(path, limit=CONTEXT_LIMIT))


def read(project: Path, objective: str) -> dict | None:
    path = _path(project, objective)
    return _read(path) if _record_exists(path) else None


def _write(path: Path, value: dict) -> None:
    value["revision"] += 1
    _validate_context(value)
    atomic_json(path, value, limit=CONTEXT_LIMIT)


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
                      native_port=None) -> dict:
    """Join a stable native current Run to this objective's persisted owner and refs."""
    from .operations import OrcaPort
    from .orca import current_run
    state = _read(_path(project, objective)) if state is None else state
    if state["owner"] not in (None, owner):
        raise PodError("native_authority_unverified", "Objective belongs to another coordinator")
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
    assignments = tuple(row for row in state.get("admissions", {}).values()
                        if row.get("state") == "bound" and binding_valid(row.get("native_binding")))
    native = port.read_native(owner, authority_runs=selected, assignments=assignments)
    if (native.get("authoritative") is not True or native.get("owner") != owner
            or native.get("scope") != "objective_assignments" or native.get("complete") is not True
            or native.get("runtime") not in set(refs.values())):
        raise PodError("native_authority_unverified", "Current native Run does not own this objective")
    if any(refs[key] != native["runtime"] for key in selected):
        raise PodError("native_authority_unverified", "Objective Run runtime changed")
    return {"runtime": native["runtime"], "run_id": run_id or next(iter(refs)),
            "native": native, "references": refs}


def context_root_for_run(run_id: str) -> Path | None:
    root = state_root()
    if not root.exists():
        return None
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
            matches.append(path.parent)
    if len(matches) > 1:
        raise PodError("ambiguous_context", "Multiple local contexts bind this Run")
    return matches[0] if matches else None


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
                         governance_observation, require_governance_current)

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
    return gitio.is_ancestor(project, commit, candidate)


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


def _governor_pending(project: Path, objective: str) -> bool:
    from .governor import _read_journal, _record_path
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        return True
    return any(row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")
               for row in journal["actions"])


def _authorization_granted(project: Path, objective: str):
    def granted(scope: str, candidate: str) -> bool:
        from .governor import _read_journal, _record_path
        try:
            journal = _read_journal(_record_path(project, objective))
        except PodError:
            return False
        return any(row["decision"] == "ALLOW" and row["action"]["kind"] == scope
                   and candidate in (row["action"]["candidate"], row.get("commit"), row.get("candidate_id"))
                   for row in journal["actions"])
    return granted


def validate_verification(value: object) -> dict:
    """The checkpoint's current verification context: dependencies and environment (R41)."""
    record = exact(value, {"dependencies", "environment"}, {"dependencies", "environment"},
                   name="verification")
    if (not isinstance(record["dependencies"], list) or len(record["dependencies"]) > 64
            or len(set(map(str, record["dependencies"]))) != len(record["dependencies"])):
        raise PodError("invalid_checkpoint", "Verification dependencies are a bounded unique list")
    for item in record["dependencies"]:
        bounded_text(item, name="dependency", limit=512)
    bounded_text(record["environment"], name="environment", limit=512)
    return {"dependencies": list(record["dependencies"]), "environment": record["environment"]}


def kernel_context(project: Path, objective: str, state: dict, native: dict | None, *,
                   candidate: str | None = None, criteria: list | None = None,
                   governance: dict | None = None, delegation: str | None = None,
                   verification: dict | None = None) -> dict:
    """Objective-local facts the obligation kernel judges; read under the caller's lock."""
    from .config import effective, load
    from .records import source_identity
    from .selection import worker_ceiling
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
    return {"admissions": state.get("admissions", {}),
            "outstanding": _outstanding_ids(state, native),
            "ceiling": ceiling, "delegation": delegation,
            "constraints": state.get("constraints", []),
            "criteria": list(criteria if criteria is not None else checkpoint_value.get("criteria", [])),
            "candidate": candidate if candidate is not None else checkpoint_value.get("candidate"),
            "governance": governance,
            "governance_current": (governance or {}).get("current") if governance is not None
            else (_resolve_commit(project, base_ref) if base_ref else None),
            "source_state": source_state,
            "source_current": source_current,
            "policy_revision": effective(project)["revision"],
            "verification": verification if verification is not None else checkpoint_value.get("verification"),
            "authorization_granted": _authorization_granted(project, objective),
            "governor_pending": _governor_pending(project, objective),
            "git_delta": lambda source, target: git_delta(project, source, target),
            "is_ancestor": lambda commit, target: git_is_ancestor(project, commit, target)}


def kernel_view(project: Path, objective: str, *, native: dict | None = None,
                candidate: str | None = None, state: dict | None = None) -> dict:
    """Read-only map, facts and derived projections for status, acceptance and reports."""
    from .obligations import label_qualification, report_projection, status_projection
    state = read(project, objective) if state is None else state
    if state is None:
        return {"state": None, "map": None, "ctx": None, "status": status_projection(None, {}),
                "label": label_qualification(None, {}, candidate), "report": None, "settlement": "none"}
    settlement = "observed"
    if native is None:
        from .operations import OrcaPort
        bound = tuple(row for row in state["admissions"].values()
                      if row["state"] == "bound" and binding_valid(row.get("native_binding")))
        try:
            native = OrcaPort(project).read_native(state.get("owner") or "", assignments=bound) if bound else None
        except PodError:
            native, settlement = None, "unverified"
    ctx = kernel_context(project, objective, state, native)
    map_state = map_of(state)
    view = {"state": state, "map": map_state, "ctx": ctx, "settlement": settlement,
            "status": status_projection(map_state, ctx),
            "label": label_qualification(map_state, ctx, candidate if candidate is not None else ctx["candidate"])}
    view["report"] = report_projection(map_state, ctx) if map_state is not None else None
    view["placements"] = {row["admission_id"]: row["placement"]
                          for row in (native or {}).get("assignments", [])
                          if isinstance(row, dict) and isinstance(row.get("admission_id"), str)
                          and isinstance(row.get("placement"), dict)}
    return view


def _require_open(state: dict) -> None:
    map_state = map_of(state)
    if map_state is not None and map_state.get("closure"):
        from .obligations import refuse
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=map_state["closure"]["revision"])


def _checkpoint_input(project: Path, objective: str, owner: str, value: dict,
                      native: dict) -> tuple[dict, dict, list]:
    from .config import effective
    from .obligations import MAP_INPUT, MAP_STORED, refuse
    bounded_text(owner, name="owner")
    # A coordinator may round-trip the stored map; Pod recomputes every stamped field.
    value = {key: item for key, item in value.items() if key not in MAP_STORED - MAP_INPUT}
    allowed = {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
               "assignments", "questions", "verification_gaps", "next_safe_action",
               "route_decisions", "objective", "objective_source", "worktree", "blocker",
               "remaining_gates", "verification"} | MAP_INPUT
    required = {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
                "assignments", "questions", "verification_gaps", "next_safe_action"}
    exact(value, allowed, required, name="checkpoint")
    if value["schema"] != CHECKPOINT_SCHEMA or not isinstance(native.get("runtime"), str):
        raise PodError("invalid_checkpoint", f"Checkpoint needs {CHECKPOINT_SCHEMA} and current native runtime readback")
    if (not isinstance(value["criteria"], list) or len(value["criteria"]) > 64
            or any(not isinstance(item, str) or not item for item in value["criteria"])
            or not isinstance(value["candidate"], str) or not value["candidate"]):
        raise PodError("invalid_checkpoint", "Checkpoint criteria and candidate are bounded text")
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
    proposed = {key: value[key] for key in MAP_INPUT if key in value}
    core = {key: item for key, item in value.items() if key not in MAP_INPUT}
    dispositions = proposed.pop("dispositions", [])
    if not isinstance(dispositions, list) or len(dispositions) > 16:
        raise refuse("obligation_unaccounted", "disposition_invalid", "dispositions are a bounded list")
    return core, proposed, dispositions


def _checkpoint_map_transition(project: Path, objective: str, state: dict, authority: dict,
                               core: dict, previous: dict, proposed: dict, dispositions: list,
                               native: dict) -> tuple[dict, dict | None]:
    from .obligations import accept_write, disposition, refuse
    prior_map = map_of(state)
    map_state: dict = {}
    ctx = None
    if proposed or prior_map is not None:
        refresh = proposed.get("governance_refresh") is True
        if prior_map is not None and not refresh:
            require_governance_current(project, prior_map)
        if prior_map is None:
            declared = proposed.get("governance")
            base_ref = declared.get("base_ref") if isinstance(declared, dict) else None
            observed = governance_observation(
                project, base_ref,
                user_direct=isinstance(proposed.get("revision_authority"), dict)
                and proposed["revision_authority"].get("provenance") == "user_direct")
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
                    project, declared["base_ref"], user_direct=isinstance(proposed.get("revision_authority"), dict)
                    and proposed["revision_authority"].get("provenance") == "user_direct")
            else:
                at = None if refresh else prior_map["governance"]["base"]
                observed = (governance_observation(project, base_ref, at=at, bound_ref=base_ref,
                                                   selection=prior_map["governance"].get("selection"))
                            if base_ref is not None
                        else governance_observation(project, None))
        history = _governance_history(prior_map)
        snapshot_authorized = (refresh
            and isinstance(proposed.get("revision_authority"), dict)
            and proposed["revision_authority"].get("provenance") == "user_direct"
            and isinstance(declared, dict)
            and declared.get("base_ref") == observed.get("target_ref")
            and declared.get("base") == observed.get("commit"))
        observed = _independent_governance(
            project, observed, core["candidate"], state["admissions"],
            established=(prior_map or {}).get("governance"),
            previous_candidate=previous.get("candidate"),
            known_candidates=history["candidates"], snapshot_authorized=snapshot_authorized)
        ctx = kernel_context(project, objective, state, authority.get("native"),
                             candidate=core["candidate"], criteria=core["criteria"],
                             governance=observed, delegation=native.get("delegation"),
                             verification=core.get("verification"))
        if dispositions:
            if prior_map is None:
                raise refuse("obligation_unaccounted", "disposition_invalid",
                             "dispositions follow admissions under a map")
            if prior_map.get("closure"):
                raise refuse("objective_closed", "objective_closed", "the objective is closed",
                             closure_revision=prior_map["closure"]["revision"])
            for request in dispositions:
                key = request.get("admission") if isinstance(request, dict) else None
                row = state["admissions"].get(key) if isinstance(key, str) else None
                if row is None:
                    raise refuse("obligation_unaccounted", "disposition_invalid",
                                 "a disposition names a recorded admission")
                row["disposition"] = disposition(row, request, ctx, seq=prior_map["seq"] + 1)
        map_state = accept_write(prior_map, proposed, ctx)
        _retain_governance_history(project, map_state, prior_map, core["candidate"], proposed,
                                   snapshot_authorized=snapshot_authorized)
    return map_state, ctx


def checkpoint(project: Path, objective: str, *, owner: str, value: dict, native: dict) -> dict:
    """Persist one validated checkpoint; while a map exists, every write is a map transition."""
    from .bundle import running_identity
    from .obligations import report_projection
    core, proposed, dispositions = _checkpoint_input(project, objective, owner, value, native)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        authority = require_authority(project, objective, owner=owner, state=state)
        if native["runtime"] != authority["runtime"]:
            raise PodError("native_authority_unverified", "Checkpoint runtime differs from current Run")
        supplied = core["native_refs"]
        if (not isinstance(supplied, list) or len(supplied) > 32
                or any(not isinstance(row, dict) or not isinstance(row.get("runId"), str)
                       or not isinstance(row.get("runtime"), str) for row in supplied)):
            raise PodError("native_authority_unverified", "Checkpoint Run references are malformed")
        if supplied and {(row["runId"], row["runtime"]) for row in supplied} != {
                (run, runtime) for run, runtime in authority["references"].items()}:
            raise PodError("native_authority_unverified", "Checkpoint cannot invent or drop Run references")
        core = {**core, "native_refs": [{"runId": run, "runtime": runtime}
                                         for run, runtime in sorted(authority["references"].items())]}
        previous = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
        if "verification" not in core and "verification" in previous:
            # Like the map, the verification context carries forward until it is restated.
            core["verification"] = previous["verification"]
        map_state, ctx = _checkpoint_map_transition(
            project, objective, state, authority, core, previous, proposed, dispositions, native)
        state["owner"] = owner
        identity = running_identity()
        if identity["bundle_digest"] is None:
            raise PodError("installed_version_changed", "Running bundle is incomplete; reload the skill and write a fresh checkpoint")
        state["checkpoint"] = {**core, **map_state, "objective": objective,
                               "pod_version": identity["version"],
                               "bundle_digest": identity["bundle_digest"]}
        _write(path, state)
        result = dict(state)
        if map_state:
            result["report"] = report_projection(state["checkpoint"], ctx)
        return result


def admission_identity(*, objective: str, run_id: str, task_id: str,
                       packet_id: str | None = None, plan_revision: str | None = None) -> str:
    """Re-entry identity excludes mutable preference, Governor and version revisions."""
    return digest({"objective": objective, "run": run_id, "task": task_id})


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
                            "state": admission["state"]})
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
        raise PodError("policy_revision_mismatch", "Governor policy changed since checkpoint")
    if body.get("policy_revision") != snapshot["policy_revision"]:
        raise PodError("policy_revision_mismatch", "Packet Governor policy changed")
    revision_changed = body.get("route", {}).get("preference_revision") != snapshot["revision"]
    ceiling = worker_ceiling(snapshot, state["constraints"])
    if len(projection["outstanding"]) >= ceiling:
        raise PodError("logical_capacity_full", "Objective logical worker ceiling is occupied")
    if "delegate" in body.get("actions", []) and not any(
            c["kind"] == "allow_delegation" and c["provenance"] == "user_direct" and c.get("active", True)
            for c in state["constraints"]):
        raise PodError("delegation_unauthorized", "Worker delegation lacks direct user intent")
    checked = validate_choice(snapshot, state["constraints"], failures, requested,
                              task=task_id, role=body.get("responsibility"), now=moment)
    if not checked["allowed"]:
        if revision_changed:
            raise PodError("preference_changed",
                           f"Preferences changed before dispatch and route is no longer allowed: {checked['code']}")
        raise PodError(checked["code"],
                       f"Proposed route is not allowed at the current preference revision: {checked['code']}")
    if revision_changed:
        raise PodError("preference_revision_stale",
                       "Preferences changed before dispatch; reread them and record a fresh decision")
    if reuse_of is not None:
        prior = state["admissions"].get(reuse_of)
        if (prior is None or prior["state"] not in ("bound", "closed")
                or not binding_valid(prior.get("native_binding"))
                or not prior["native_binding"]["terminalHandle"]):
            raise PodError("reuse_unavailable", "Terminal reuse needs a settled, known prior attempt")
        matches = [a for a in native["assignments"] if a.get("admission_id") == reuse_of]
        if len(matches) != 1 or matches[0].get("settled") is not True:
            raise PodError("reuse_unavailable", "Prior attempt has not settled natively")
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
                    worktree: str, reuse_of: str | None, accompanying: dict | None) -> tuple[dict, dict]:
    from . import __version__
    from .obligations import admit, reserved_admission_shape
    stamp = moment.isoformat()
    decision = {"agent": requested["agent"], "model": requested["model"],
                "requested_effort": requested["effort"], "requested_context": requested["context"],
                "reason": requested["reason"], "preference_revision": snapshot["revision"],
                "policy_revision": snapshot["policy_revision"], "mode": snapshot["mode"],
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
    return row, new_map


def reserve(project: Path, objective: str, *, owner: str, admission_id: str,
            requested: dict, native_reader: Callable[[dict], dict], run_id: str,
            task_id: str, plan_revision: str, packet_id: str, worktree: str,
            frozen_packet: dict, expected_runtime: str, placement_binding: dict,
            reuse_of: str | None = None, accompanying: dict | None = None,
            now: datetime | None = None) -> dict:
    """Final serialized admission: preference read is last, before writing the row.

    The served obligations become active in the same locked write that persists the
    admission row, so a map can never name an admission that was not recorded.
    """
    from .selection import failure_active
    moment = now or datetime.now(timezone.utc)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        refs = _run_references(state)
        if run_id not in refs:
            raise PodError("native_authority_unverified", "Admission Run is not an exact objective reference")
        existing = state["admissions"].get(admission_id)
        if existing is not None:
            if (existing["objective"] != objective or existing["run_id"] != run_id
                    or existing["task_id"] != task_id or existing["packet_id"] != packet_id
                    or existing["plan_revision"] != plan_revision or existing["worktree"] != worktree):
                raise PodError("admission_conflict", "Admission identity was reused for different work")
            return {**existing, "existing": True}
        if any(row["task_id"] == task_id and row["state"] in ("reserved", "unresolved")
               for row in state["admissions"].values()):
            raise PodError("unresolved_prior_attempt", "An unsettled Task cannot be replaced")
        _require_open(state)
        map_state = map_of(state)
        if map_state is None:
            from .obligations import refuse
            raise refuse("unbound_assignment", "no_map", "delegation needs an obligation map first")
        from .bundle import require_current_identity
        require_current_identity(state.get("checkpoint"))
        native = native_reader(state)
        if (native.get("authoritative") is not True or native.get("owner") != owner
                or native.get("scope") != "objective_assignments" or native.get("complete") is not True
                or not isinstance(native.get("runtime"), str)):
            raise PodError("native_authority_unverified", "Admission lacks stable native Run authority")
        if native["runtime"] != expected_runtime:
            raise PodError("orca_runtime_changed", "Launch capability and native authority changed runtime")
        if native["runtime"] != refs[run_id]:
            raise PodError("native_authority_unverified", "Admission Run runtime differs from objective")
        checkpoint_value = state.get("checkpoint")
        body = frozen_packet.get("body") if isinstance(frozen_packet, dict) else None
        if (not isinstance(body, dict) or frozen_packet.get("packet_id") != packet_id
                or body.get("candidate") != checkpoint_value.get("candidate")
                or body.get("criteria") != checkpoint_value.get("criteria")
                or body.get("plan_revision") != plan_revision
                or body.get("objective_source") != checkpoint_value.get("objective_source")
                or body.get("worktree") != checkpoint_value.get("worktree")):
            raise PodError("packet_plan_mismatch", "Packet differs from current checkpoint")
        bound_sources = [*body["sources"], *({"path": ref["path"], "state": "present",
                          "sha256": ref["sha256"]} for ref in body["context"]
                         if ref["kind"] in ("source", "instruction"))]
        _check_bound_sources_locked(project, path, state, packet_id, bound_sources)
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
        admission_refusal(map_state, body, ctx, admission_id=admission_id)
        require_governance_current(project, map_state)
        snapshot = _reserve_route(project, checkpoint_value, body, state, requested, task_id,
                                  moment, projection, failures, reuse_of, native)
        row, new_map = _reserve_record(
            project, objective, owner, admission_id, requested, snapshot, state, body,
            checkpoint_value, map_state,
            ctx, placement_binding, moment, native, run_id, task_id, plan_revision, packet_id,
            worktree, reuse_of, accompanying)
        state["owner"] = owner
        state["admissions"][admission_id] = row
        state["checkpoint"] = {**checkpoint_value, **new_map}
        _write(path, state)
        return {**row, "existing": False}


def update_admission(project: Path, objective: str, *, owner: str, admission_id: str,
                     update: Callable[[dict], None], open_required: bool = False) -> dict:
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner or admission_id not in state["admissions"]:
            raise PodError("unknown_admission", "No owned admission identity")
        if open_required:
            _require_open(state)
        require_authority(project, objective, owner=owner, state=state,
                          run_id=state["admissions"][admission_id]["run_id"])
        row = state["admissions"][admission_id]
        admitted = ("admitted_seq" in row, row.get("admitted_seq"))
        update(row)
        if admitted != ("admitted_seq" in row, row.get("admitted_seq")):
            raise PodError("admission_conflict", "The admission's reservation sequence is immutable")
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write(path, state)
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


def consume_report(project: Path, objective: str, *, owner: str, admission_id: str,
                   observation: dict, accompanying: dict | None = None, findings: list | None = None,
                   proposals: list | None = None, result_commit: str | None = None) -> dict:
    """Report consumption is a map write: Git result ingestion, triage and proposals, atomically.

    The worker report is an observation. Changed paths come from Git, never from the
    report; findings and discoveries become corrections or proposals by the coordinator's
    triage; nothing in the report changes a criterion, gate or authorization.
    """
    from .obligations import accept_write, refuse, report_projection, triage
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        row = state["admissions"].get(admission_id)
        if state["owner"] != owner or row is None:
            raise PodError("unknown_admission", "No owned admission identity")
        authority = require_authority(project, objective, owner=owner, state=state, run_id=row["run_id"])
        map_state = map_of(state)
        if map_state is None:
            raise refuse("unbound_assignment", "no_map", "a report joins an obligation map")
        _require_open(state)
        ctx = kernel_context(project, objective, state, authority.get("native"))
        settled = admission_id not in ctx["outstanding"]
        if not settled:
            raise PodError("report_attempt_unverified", "The exact Dispatch has not settled natively")
        report_value = observation.get("observation", {})
        report_identity = digest(observation)
        prior_report = row.get("report")
        if prior_report is not None and (prior_report.get("observation_digest") != report_identity
                                         or prior_report.get("result_commit") != result_commit):
            raise PodError("report_conflict", "A consumed Dispatch report or result cannot be replaced")
        if prior_report is not None and not accompanying and not findings and not proposals:
            ingestion = _stored_ingestion(row)
            return {"ingestion": ingestion, "settled": True,
                    "map": {"seq": map_state["seq"], "revision": map_state["revision"],
                            "quiescence": map_state["quiescence"]},
                    "report": report_projection(map_state, ctx)}
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
        value, triaged = triage(map_state, dict(accompanying or {}), findings or [], proposals or [], ctx,
                                admission_id=admission_id)
        new_map = accept_write(map_state, value, ctx, triaged=triaged)
        state["checkpoint"] = {**state["checkpoint"], **new_map}
        _write(path, state)
        return {"ingestion": ingestion, "settled": settled,
                "map": {"seq": new_map["seq"], "revision": new_map["revision"],
                        "quiescence": new_map["quiescence"]},
                "report": report_projection(state["checkpoint"], ctx)}


def state_inventory(project: Path) -> dict:
    """Read-only state inventory for doctor/status; it never rewrites a record."""
    root = state_root(project)
    result = {"current": 0, "unsupported": 0, "unreadable": 0, "blocked": False, "superseded": []}
    if not root.exists():
        return result
    for path in root.glob("*/context.json"):
        try:
            _validate_context(bounded_json(path, limit=CONTEXT_LIMIT))
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
    import json
    root = state_root()
    rows = []
    for row in superseded_objectives(root):
        try:
            value = json.loads((root / row["record"] / "context.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
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
            raise PodError("coordinator_conflict", "Constraint belongs to another coordinator")
        _require_open(state)
        require_authority(project, objective, owner=owner, state=state)
        if action == "add":
            snapshot = load(project)
            row = validate_constraint(value, snapshot)
            if not isinstance(row.get("id"), str) or not row["id"] or len(row["id"]) > 128:
                raise PodError("invalid_constraint", "Constraint id is required")
            if any(c.get("id") == row["id"] for c in state["constraints"]):
                raise PodError("constraint_conflict", "Constraint id is already present")
            state["constraints"].append(row)
        elif action == "revoke":
            matches = [c for c in state["constraints"] if c.get("id") == constraint_id]
            if len(matches) != 1:
                raise PodError("unknown_constraint", "Constraint id is unavailable")
            matches[0]["active"] = False
        else:
            raise PodError("invalid_constraint_action", "Constraint action is unsupported")
        _write(path, state)
        return {"constraints": state["constraints"]}


def route_failure(project: Path, objective: str, *, owner: str, admission_id: str,
                  kind: str, source: str, retry_after: str | None = None,
                  clear: bool = False, cleared_by: str | None = None,
                  native_reader: Callable[[dict], dict] | None = None) -> dict:
    from .selection import FAILURE_KINDS
    if kind not in FAILURE_KINDS or not isinstance(source, str) or not source or len(source) > 256:
        raise PodError("invalid_route_failure", "Failure kind and source are required")
    if kind == "safety_refusal" and retry_after is not None:
        raise PodError("invalid_route_failure", "Safety refusal has no timed retry")
    if retry_after is not None:
        try:
            parsed = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PodError("invalid_route_failure", "Retry-after must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise PodError("invalid_route_failure", "Retry-after needs a timezone")
    def apply(row: dict) -> None:
        if clear:
            if cleared_by not in ("user", "runtime_change"):
                raise PodError("invalid_route_failure", "Clear needs user or runtime-change provenance")
            matched = False
            for failure in row["failures"]:
                if failure["kind"] == kind and not failure.get("cleared_at"):
                    failure["cleared_at"] = datetime.now(timezone.utc).isoformat()
                    failure["cleared_by"] = cleared_by
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
            row["failures"].append({"kind": kind, "source": source, "retry_after": retry_after,
                                    "model": row["request"]["model"], "task": row["task_id"],
                                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                                    "cleared_at": None, "cleared_by": None})
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply,
                            open_required=True)

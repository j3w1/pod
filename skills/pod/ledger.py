"""Compact Pod policy/evidence state; Orca remains lifecycle authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
from typing import Callable, Iterator

from .errors import PodError
from .orca import require_route_establishment
from .quota import validate_snapshot
from .routing import quota_state
from .util import atomic_json, bounded_json, bounded_text, digest, exact, explicit_home, native_home


ADMISSION_STATES = ("reserved", "bound", "unresolved", "closed", "deferred")
CONTEXT_SCHEMA = "pod-context/v3"
_BINDING_FIELDS = {"runId", "taskId", "dispatchId", "workerId", "worktreeId", "terminalHandle"}
_CONTEXT_FIELDS = {"schema", "revision", "owner", "admissions", "checkpoint",
                   "interventions", "source_rejections"}


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
            "source_rejections": {}}


def binding_valid(binding: object) -> bool:
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
        return False
    for key in ("runId", "taskId", "dispatchId", "workerId", "worktreeId"):
        if not isinstance(binding.get(key), str) or not binding[key]:
            return False
    terminal = binding.get("terminalHandle")
    return terminal is None or isinstance(terminal, str) and bool(terminal)


def _validate_admission(key: str, row: object) -> None:
    required = {"schema", "state", "admission_id", "objective", "owner", "request",
                "route_decision", "effective_evidence", "runtime", "request_uuid",
                "run_id", "task_id", "plan_revision", "packet_id", "worktree", "bucket",
                "native_binding", "recovery", "error", "created_at", "updated_at"}
    value = exact(row, required, required, name="admission")
    if value["schema"] != "pod-admission/v2" or value["admission_id"] != key:
        raise PodError("state_unsupported", "Admission identity or schema is unsupported")
    if value["state"] not in ADMISSION_STATES:
        raise PodError("state_unsupported", "Admission state is unsupported")
    for field in ("objective", "owner", "run_id", "task_id", "plan_revision", "packet_id", "worktree"):
        if not isinstance(value[field], str) or not value[field]:
            raise PodError("state_unsupported", "Admission binding is incomplete")
    if not isinstance(value["request"], dict) or not isinstance(value["route_decision"], dict):
        raise PodError("state_unsupported", "Admission policy evidence is malformed")
    if not isinstance(value["effective_evidence"], dict):
        raise PodError("state_unsupported", "Admission effective evidence is malformed")
    if not isinstance(value["runtime"], str) or not value["runtime"]:
        raise PodError("state_unsupported", "Admission runtime is unavailable")
    request_uuid = value["request_uuid"]
    if request_uuid is not None and (not isinstance(request_uuid, str) or not request_uuid):
        raise PodError("state_unsupported", "Admission request UUID is malformed")
    binding = value["native_binding"]
    if value["state"] in ("bound", "closed") and not binding_valid(binding):
        raise PodError("state_unsupported", "Bound admission lacks exact native identity")
    if binding is not None and not binding_valid(binding):
        raise PodError("state_unsupported", "Admission native identity is malformed")


def _validate_context(value: object) -> dict:
    """Accept exactly the current context schema; any other record is unsupported, not converted."""
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_SCHEMA:
        raise PodError("state_unsupported",
                       f"State record is not {CONTEXT_SCHEMA}; archive or remove it before new admissions")
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
    if not isinstance(state["interventions"], dict) or not isinstance(state["source_rejections"], dict):
        raise PodError("state_unsupported", "Context evidence is malformed")
    return state


def _read(path: Path) -> dict:
    if not _record_exists(path):
        return _empty()
    return _validate_context(bounded_json(path))


def read(project: Path, objective: str) -> dict | None:
    path = _path(project, objective)
    return _read(path) if _record_exists(path) else None


def _write(path: Path, value: dict) -> None:
    value["revision"] += 1
    _validate_context(value)
    atomic_json(path, value)


def context_root_for_run(run_id: str) -> Path | None:
    root = state_root()
    if not root.exists():
        return None
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    matches = []
    unsupported_seen = False
    for path in root.glob("*/context.json"):
        try:
            state = _read(path)
        except PodError as exc:
            if exc.code == "state_unsupported":
                unsupported_seen = True
                continue
            raise
        checkpoint_value = state.get("checkpoint")
        refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
        admission_match = any(row.get("run_id") == run_id for row in state["admissions"].values())
        if admission_match or any(isinstance(ref, dict) and ref.get("runId") == run_id for ref in refs):
            matches.append(path.parent)
    if len(matches) > 1:
        raise PodError("ambiguous_context", "Multiple local contexts bind this Run")
    if not matches and unsupported_seen:
        raise PodError("state_unsupported", "An unsupported state record may bind this Run")
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
        return _check_bound_sources_locked(project, path, state, assignment, sources)


def _check_bound_sources_locked(project: Path, path: Path, state: dict,
                                assignment: str, sources: list[dict]) -> dict:
    from .records import source_identity
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
            raise PodError(reason, "Bound source is absent or changed")
    return {"status": "current", "assignment": assignment}


def checkpoint(project: Path, objective: str, *, owner: str, value: dict, native: dict) -> dict:
    bounded_text(owner, name="owner")
    exact(value, {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
                  "assignments", "questions", "verification_gaps", "next_safe_action",
                  "route_decisions", "quota_visibility", "objective", "objective_source",
                  "worktree", "blocker", "remaining_gates"},
          {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
           "assignments", "questions", "verification_gaps", "next_safe_action"}, name="checkpoint")
    if value["schema"] != "pod-checkpoint/v1" or not isinstance(native.get("runtime"), str):
        raise PodError("invalid_checkpoint", "Checkpoint needs current native runtime readback")
    if "objective" in value and value["objective"] != objective:
        raise PodError("invalid_checkpoint", "Checkpoint objective differs from its state key")
    if "objective_source" in value:
        from .github import validate_issue_binding
        validate_issue_binding(value["objective_source"])
    if "worktree" in value:
        from .records import packet
        probe = {"schema": "pod-packet/v1", "objective": objective, "criteria": [],
                 "responsibility": "checkpoint validation", "scope": [], "actions": [],
                 "candidate": value["candidate"], "context": [], "dependencies": [],
                 "route": {"agent": "direct"}, "policy_revision": value["policy_revision"],
                 "plan_revision": value["plan_revision"], "report_contract": "checkpoint",
                 "sources": [], "worktree": value["worktree"]}
        packet(probe)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        state["owner"] = owner
        state["checkpoint"] = {**value, "objective": objective}
        _write(path, state)
        return state


def admission_identity(*, objective: str, run_id: str, task_id: str, packet_id: str,
                       plan_revision: str) -> str:
    """Stable Pod identity for one policy decision, never an Orca request UUID."""
    return digest({"objective": objective, "run": run_id, "task": task_id,
                   "packet": packet_id, "plan_revision": plan_revision})


def _grant_matches(grant: dict | None, authorized: list, *, objective: str, run_id: str,
                   plan_revision: str, account: str, capacity: int, now: datetime) -> bool:
    if not isinstance(grant, dict) or grant not in authorized:
        return False
    try:
        expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return (grant.get("action") == "exceptional_capacity" and grant.get("objective") == objective
            and grant.get("run") == run_id and grant.get("plan_revision") == plan_revision
            and grant.get("account") == account and type(grant.get("limit")) is int
            and capacity <= grant["limit"] <= 8 and capacity >= 4
            and isinstance(grant.get("reason"), str) and bool(grant["reason"].strip())
            and expiry.tzinfo is not None and now.tzinfo is not None and now <= expiry)


def _spending_grant_binding(grants: list, supplied: object, *, requested: dict,
                            objective: str, now: datetime) -> tuple[dict, int]:
    if not isinstance(supplied, dict):
        raise PodError("spending_grant_required", "Paid launch lacks its exact spending grant")
    matches = [grant for grant in grants if isinstance(grant, dict) and grant.get("id") == supplied.get("id")]
    if len(matches) != 1:
        raise PodError("spending_grant_required", "Paid launch grant identity is unavailable")
    grant = matches[0]
    expected = {"id": grant["id"], "identity": digest(grant), "units": 1,
                "scope": digest({key: grant.get(key) for key in
                                 ("id", "action", "account", "model", "objective")})}
    try:
        expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise PodError("spending_grant_required", "Paid launch grant validity is invalid") from exc
    if (supplied != expected or grant.get("action") != "paid_usage"
            or grant.get("account") != requested.get("account")
            or grant.get("model") != requested.get("model")
            or grant.get("objective") != objective
            or type(grant.get("max_units")) is not int or grant["max_units"] < 1
            or expiry.tzinfo is None or now.tzinfo is None or now > expiry):
        raise PodError("spending_grant_required", "Paid launch grant scope or validity changed")
    return expected, grant["max_units"]


def _bucket_overlap(provider: str, account: str, bucket: str | None, other: dict) -> bool:
    if other.get("account") == account:
        return True
    other_provider = other.get("agent")
    if other_provider is None:
        return True
    if other_provider != provider:
        return False
    other_bucket = other.get("bucket")
    return bucket is None or other_bucket is None or bucket == other_bucket


def _all_admissions(project: Path) -> list[tuple[Path, dict]]:
    root = state_root(project)
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    rows = []
    if not root.exists():
        return rows
    for path in root.glob("*/context.json"):
        state = _read(path)
        rows.extend((path, row) for row in state["admissions"].values())
    return rows


def logical_projection(project: Path, native: dict, *, objective: str,
                       tasks: list[str] | None = None) -> dict:
    """Project objective-local outstanding assignments from exact attempt settlement."""
    if (native.get("scope") != "objective_assignments" or native.get("complete") is not True
            or not isinstance(native.get("runtime"), str)
            or not isinstance(native.get("assignments"), list)):
        raise PodError("native_assignment_unverified", "Objective assignment evidence is incomplete")
    state = read(project, objective)
    admissions = state.get("admissions", {}) if isinstance(state, dict) else {}
    evidence_by_admission: dict[str, list[dict]] = {}
    for row in native["assignments"]:
        if isinstance(row, dict) and isinstance(row.get("admission_id"), str):
            evidence_by_admission.setdefault(row["admission_id"], []).append(row)
    outstanding = []
    for admission_id, admission in admissions.items():
        if admission["state"] in ("closed", "deferred"):
            continue
        binding = admission.get("native_binding")
        settled = False
        matches = evidence_by_admission.get(admission_id, [])
        if admission["state"] == "bound" and binding_valid(binding) and len(matches) == 1:
            observed = matches[0]
            settled = (observed.get("settled") is True
                       and observed.get("runtime") == admission["runtime"] == native["runtime"]
                       and observed.get("run_id") == binding["runId"]
                       and observed.get("task_id") == binding["taskId"]
                       and observed.get("dispatch_id") == binding["dispatchId"]
                       and observed.get("worker_id") == binding["workerId"])
        if settled:
            continue
        task = binding.get("taskId") if isinstance(binding, dict) else admission["task_id"]
        if tasks is not None and task not in tasks:
            continue
        outstanding.append({**admission["request"], "objective": objective,
                            "admission_id": admission_id, "task": task,
                            "state": admission["state"], "bucket": admission.get("bucket")})
    return {"schema": "pod-logical-projection/v1", "runtime": native["runtime"],
            "authoritative": native.get("authoritative") is True,
            "owner": native.get("owner") if native.get("authoritative") is True else None,
            "outstanding": outstanding,
            "outstanding_ids": [row["admission_id"] for row in outstanding],
            "physical_capacity": native.get("physical_capacity", "unavailable")}


_SETTLED_ASSIGNMENT_OUTCOMES = frozenset(
    {"succeeded", "failed", "stopped", "canceled", "cancelled", "abandoned"})


def _native_assignment_settled(shown: dict) -> bool:
    """Read only Orca's assignment outcome and its settlement qualifier."""
    result = shown.get("result") if isinstance(shown, dict) else None
    projection = result.get("projection") if isinstance(result, dict) else None
    if not isinstance(projection, dict):
        return False
    stage = projection.get("stage")
    if not isinstance(stage, dict):
        return False
    outcome = projection.get("outcome")
    return (isinstance(outcome, str) and outcome in _SETTLED_ASSIGNMENT_OUTCOMES
            and stage.get("detail") == "settled")


def _quota_hold(provider: str, account: str, bucket: str | None,
                snapshot: dict | None, state: str, *, now: datetime,
                freshness: int, project: Path | None = None) -> bool:
    """Keep exhaustion monotonic until a fresh positive window clears it."""
    root = state_root(project)
    path = root / "quota-holds.json"
    key = digest({"provider": provider, "account": account, "bucket": bucket})
    with _lock(path):
        raw = bounded_json(path) if path.exists() else {"schema": "pod-quota-holds/v3", "holds": {}}
        exact(raw, {"schema", "holds"}, {"schema", "holds"}, name="quota_holds")
        if raw["schema"] != "pod-quota-holds/v3" or not isinstance(raw["holds"], dict):
            raise PodError("state_unsupported", "Quota hold schema is unsupported")
        hold = raw["holds"].get(key, {"windows": {}})
        if not isinstance(hold, dict) or not isinstance(hold.get("windows"), dict):
            raise PodError("state_unsupported", "Quota hold identity is malformed")
        windows = dict(hold["windows"])
        supported = None
        if state != "unknown" and snapshot is not None:
            try:
                candidate = validate_snapshot(snapshot)
                observed = datetime.fromisoformat(candidate["observed_at"].replace("Z", "+00:00"))
                if (candidate["provider"] == provider and candidate["account"] == account
                        and candidate["bucket"] == bucket
                        and candidate["source"] in ("supported", "supported_metadata")
                        and candidate["confidence"] == "observed"
                        and observed.tzinfo is not None and now.tzinfo is not None
                        and 0 <= (now - observed).total_seconds()):
                    supported = candidate, observed, (now - observed).total_seconds() <= freshness
            except (PodError, TypeError, ValueError, OverflowError):
                pass
        changed = False
        if supported is not None:
            candidate, observed, fresh = supported
            values = {row["name"]: (row.get("remaining_percent"), row.get("reset_at"))
                      for row in candidate["windows"]}
            if "remaining_percent" in candidate:
                values["aggregate"] = (candidate["remaining_percent"], None)
            for name, (remaining, reset_at) in values.items():
                previous = windows.get(name)
                previous_at = None
                if previous is not None:
                    if not isinstance(previous, dict) or not isinstance(previous.get("observed_at"), str):
                        raise PodError("state_unsupported", "Quota hold window is malformed")
                    try:
                        previous_at = datetime.fromisoformat(
                            previous["observed_at"].replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise PodError("state_unsupported",
                                       "Quota hold timestamp is malformed") from exc
                    if previous_at.tzinfo is None:
                        raise PodError("state_unsupported",
                                       "Quota hold timestamp has no timezone")
                if remaining == 0 and (previous_at is None or observed > previous_at):
                    windows[name] = {"observed_at": observed.isoformat(), "reset_at": reset_at}
                    changed = True
                elif (fresh and remaining is not None and remaining > 0
                      and previous_at is not None and observed > previous_at):
                    del windows[name]
                    changed = True
        if changed:
            if windows:
                raw["holds"][key] = {"windows": windows}
            else:
                raw["holds"].pop(key, None)
            atomic_json(path, raw)
        if state == "exhausted":
            return True
        return bool(windows)


def reserve(project: Path, objective: str, *, owner: str, admission_id: str,
            requested: dict, route_decision: dict, establishment: dict,
            native_reader: Callable[[], dict], capacity: int, run_id: str,
            task_id: str, plan_revision: str, packet_id: str, worktree: str,
            frozen_packet: dict,
            exceptional_grant: dict | None = None, capacity_reason: str | None = None,
            spending_grant: dict | None = None, task_policy: dict | None = None,
            now: datetime | None = None) -> dict:
    """Serialize policy and logical fan-out validation before the native start seam."""
    moment = now or datetime.now(timezone.utc)
    if not isinstance(admission_id, str) or not admission_id:
        raise PodError("invalid_admission", "Admission identity is required")
    if capacity < 1 or capacity > 8:
        raise PodError("invalid_capacity", "Worker capacity must be between one and eight")
    require_route_establishment(establishment, requested)
    if route_decision.get("status") != "usable" or route_decision.get("selected") != requested:
        raise PodError("route_unusable", "Admission needs the exact usable route decision")
    if not isinstance(route_decision.get("policy_revision"), str):
        raise PodError("route_unusable", "Route decision lacks a policy revision")
    path = _path(project, objective)
    with _lock(state_root(project) / "admission" / "state"), _lock(path):
        from .config import effective
        current_policy = effective(project, task=task_policy)
        if current_policy["revision"] != route_decision.get("policy_revision"):
            raise PodError("policy_revision_mismatch", "Admission policy changed after route selection")
        policy = current_policy["policy"]["policy"]
        route_model = current_policy["policy"]["models"].get(requested.get("alias"))
        if (not isinstance(route_model, dict) or not route_model.get("approved")
                or route_model.get("agent") != requested.get("agent")
                or route_model.get("model") != requested.get("model")
                or route_model.get("account") != requested.get("account")):
            raise PodError("route_unusable", "Requested route is no longer approved and exact")
        state = _read(path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        existing = state["admissions"].get(admission_id)
        if existing:
            immutable = (existing["objective"], existing["run_id"], existing["task_id"],
                         existing["packet_id"], existing["plan_revision"], existing["request"],
                         existing["worktree"])
            expected = (objective, run_id, task_id, packet_id, plan_revision, requested, worktree)
            if immutable != expected:
                raise PodError("admission_conflict", "Admission identity was reused for different work")
            return {**existing, "existing": True}
        if any(row.get("run_id") == run_id and row.get("task_id") == task_id
               and row.get("state") in ("reserved", "unresolved")
               for row in state["admissions"].values() if isinstance(row, dict)):
            raise PodError("unresolved_prior_attempt",
                           "A revised packet cannot replace an uncertain attempt for the same native Task")
        native = native_reader()
        if (native.get("authoritative") is not True or native.get("owner") != owner
                or native.get("runtime") != establishment.get("runtime")
                or native.get("scope") != "objective_assignments"
                or native.get("complete") is not True):
            raise PodError("native_authority_unverified",
                           "Admission lacks stable current-Run authority")
        checkpoint_value = state.get("checkpoint")
        body = frozen_packet.get("body") if isinstance(frozen_packet, dict) else None
        if (not isinstance(body, dict) or frozen_packet.get("packet_id") != packet_id
                or body.get("candidate") != (checkpoint_value or {}).get("candidate")
                or body.get("criteria") != (checkpoint_value or {}).get("criteria")
                or body.get("plan_revision") != plan_revision
                or body.get("policy_revision") != current_policy["revision"]
                or body.get("objective_source") != (checkpoint_value or {}).get("objective_source")
                or body.get("worktree") != (checkpoint_value or {}).get("worktree")):
            raise PodError("packet_plan_mismatch", "Packet differs from the current owned checkpoint")
        if "delegate" in body.get("actions", []) and not policy.get("child_delegation"):
            raise PodError("delegation_unauthorized", "Worker delegation is not authorized")
        bound_sources = [*body["sources"], *({"path": ref["path"], "state": "present",
                          "sha256": ref["sha256"]} for ref in body["context"]
                         if ref["kind"] in ("source", "instruction"))]
        _check_bound_sources_locked(project, path, state, packet_id, bound_sources)
        if capacity <= 3 and capacity > min(policy["max_workers"], policy["ordinary_max"]):
            raise PodError("capacity_ceiling", "Capacity exceeds the ordinary worker ceiling")
        if capacity == 3 and not (isinstance(capacity_reason, str) and capacity_reason.strip()):
            raise PodError("capacity_reason_required", "Three workers needs a reason")
        if capacity >= 4 and not _grant_matches(exceptional_grant, policy.get("exceptional_grants", []),
                                                 objective=objective, run_id=run_id,
                                                 plan_revision=plan_revision,
                                                 account=requested["account"], capacity=capacity,
                                                 now=moment):
            raise PodError("exceptional_capacity_grant_required", "Four to eight workers needs an exact grant")
        if (capacity >= 4
                and current_policy["provenance"].get("policy.max_workers") in ("project", "task")
                and capacity > policy["max_workers"]):
            raise PodError("capacity_ceiling", "Local hard capacity restriction remains effective")
        qstate, _ = quota_state(native.get("quota"), provider=requested["agent"],
                             account=requested["account"], bucket=requested.get("bucket"),
                             policy=policy, now=moment)
        if _quota_hold(requested["agent"], requested["account"], requested.get("bucket"),
                       native.get("quota"), qstate, now=moment,
                       freshness=policy.get("quota_fresh_seconds", 60), project=project):
            raise PodError("quota_exhausted", "Current applicable quota bucket is exhausted")
        projection = logical_projection(project, native, objective=objective)
        outstanding = projection["outstanding"]
        overlapping = [row for row in outstanding
                       if _bucket_overlap(requested["agent"], requested["account"],
                                          requested.get("bucket"), row)]
        if len(outstanding) >= capacity:
            raise PodError("logical_capacity_full", "Objective fan-out limit is occupied")
        if qstate == "unknown" and overlapping:
            raise PodError("unknown_quota_capacity",
                           "Unknown quota permits one outstanding logical assignment on this objective route")
        grant_binding = None
        if route_model.get("billing", "unknown") != "included":
            grant_binding, maximum = _spending_grant_binding(policy.get("spending_grants", []),
                                                              route_decision.get("spending_grant"), requested=requested,
                                                              objective=objective, now=moment)
            used = 0
            for _, prior in _all_admissions(project):
                prior_request = prior.get("request")
                prior_grant = prior.get("recovery", {}).get("spending_grant")
                if (isinstance(prior_request, dict)
                        and prior_request.get("account") == requested.get("account")
                        and prior_request.get("model") == requested.get("model")
                        and prior_grant is None):
                    raise PodError("spending_history_unverified",
                                   "Earlier matching admission lacks grant accounting")
                if prior_grant is None:
                    continue
                if (not isinstance(prior_grant, dict)
                        or type(prior_grant.get("units")) is not int
                        or prior_grant["units"] < 1
                        or not isinstance(prior_grant.get("scope"), str)):
                    raise PodError("state_unsupported",
                                   "Spending grant accounting is malformed")
                if prior_grant["scope"] == grant_binding["scope"]:
                    used += prior_grant["units"]
            if used >= maximum:
                raise PodError("spending_grant_exhausted", "Spending grant units are exhausted")
        stamp = moment.isoformat()
        row = {"schema": "pod-admission/v2", "state": "reserved",
               "admission_id": admission_id, "objective": objective, "owner": owner,
               "request": requested, "route_decision": route_decision,
               "effective_evidence": establishment, "runtime": native["runtime"],
               "request_uuid": None, "run_id": run_id, "task_id": task_id,
               "plan_revision": plan_revision, "packet_id": packet_id, "worktree": worktree,
               "bucket": requested.get("bucket"), "native_binding": None,
               "recovery": {
                   "checkpoint_binding": {key: checkpoint_value.get(key) for key in
                                          ("candidate", "criteria", "plan_revision",
                                           "policy_revision", "objective_source", "worktree")},
                   **({"placement_binding": body.get("placement", body.get("worktree"))}
                      if body.get("placement", body.get("worktree")) is not None else {}),
                   **({"spending_grant": grant_binding} if grant_binding else {})},
               "error": None, "created_at": stamp, "updated_at": stamp}
        state["owner"] = owner
        state["admissions"][admission_id] = row
        _write(path, state)
        return {**row, "existing": False}


def update_admission(project: Path, objective: str, *, owner: str, admission_id: str,
                     update: Callable[[dict], None]) -> dict:
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner or admission_id not in state["admissions"]:
            raise PodError("unknown_admission", "No owned admission identity")
        row = state["admissions"][admission_id]
        update(row)
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write(path, state)
        return row


def state_inventory(project: Path) -> dict:
    """Read-only state inventory for doctor/status; it never rewrites a record."""
    root = state_root(project)
    result = {"current": 0, "unsupported": 0, "unreadable": 0, "blocked": False}
    if not root.exists():
        return result
    for path in root.glob("*/context.json"):
        try:
            _validate_context(bounded_json(path))
            result["current"] += 1
        except PodError as exc:
            result["unsupported" if exc.code == "state_unsupported" else "unreadable"] += 1
        except OSError:
            result["unreadable"] += 1
    result["blocked"] = bool(result["unsupported"] or result["unreadable"])
    return result


def intervention(project: Path, objective: str, *, owner: str, correction: dict,
                 task: str | None = None, diagnosis: dict | None = None,
                 unit: str | None = None) -> dict:
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Correction belongs to another coordinator")
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

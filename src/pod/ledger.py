"""Compact private effect journal; native Orca remains the task authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import PodError
from .orca import effective_launch, require_prelaunch_assurance
from .routing import quota_state
from .util import atomic_json, bounded_json, bounded_text, digest, exact


def state_root() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise PodError("state_home_unavailable", "LOCALAPPDATA is required for Pod state")
        return Path(root) / "pod"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "pod"


def _path(project: Path, objective: str) -> Path:
    return state_root() / digest({"project": str(project.resolve()), "objective": objective}) / "context.json"


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
    fd = os.open(path.parent / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> dict:
    if not path.exists():
        return {"schema": "pod-context/v1", "revision": 0, "owner": None, "effects": {},
                "checkpoint": None, "deliveries": {}, "interventions": {},
                "source_rejections": {}, "cleanup": {}}
    value = bounded_json(path)
    exact(value, {"schema", "revision", "owner", "effects", "checkpoint", "deliveries", "interventions", "source_rejections", "cleanup"},
          {"schema", "revision", "owner", "effects", "checkpoint", "deliveries", "interventions", "source_rejections", "cleanup"}, name="context")
    if value["schema"] != "pod-context/v1":
        raise PodError("state_migration_required", "State schema requires explicit migration")
    return value


def read(project: Path, objective: str) -> dict | None:
    path = _path(project, objective)
    return _read(path) if path.exists() else None


def context_for_run(run_id: str) -> dict | None:
    """Read only a uniquely bound checkpoint; never create or migrate state."""
    root = state_root()
    if not root.exists():
        return None
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    matches = []
    for path in root.glob("*/context.json"):
        state = _read(path)
        checkpoint_value = state.get("checkpoint")
        refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
        if any(isinstance(ref, dict) and ref.get("runId") == run_id for ref in refs):
            matches.append(state)
    if len(matches) > 1:
        raise PodError("ambiguous_context", "Multiple local contexts bind this Run")
    return matches[0] if matches else None


def check_bound_sources(project: Path, objective: str, *, owner: str,
                        assignment: str, sources: list[dict]) -> dict:
    """Definitive source rejection sticks to the same assignment after restoration."""
    from .records import source_identity
    bounded_text(assignment, name="assignment", limit=128)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Source check belongs to another coordinator")
        prior = state["source_rejections"].get(assignment)
        if prior:
            raise PodError("source_rejected", "Assignment has a durable definitive source rejection")
        for entry in sources:
            exact(entry, {"path", "state", "sha256"}, {"path", "state"}, name="source")
            try:
                observed = source_identity(project, entry["path"])
            except PodError as exc:
                if exc.code in ("unsafe_source", "secret_source"):
                    state["source_rejections"][assignment] = {"reason": exc.code, "path": entry["path"]}
                    _write(path, state)
                raise
            if observed["state"] == "unavailable":
                raise PodError("source_unavailable", "Source is temporarily unavailable")
            if observed != entry:
                state["source_rejections"][assignment] = {"reason": "source_changed", "path": entry["path"]}
                _write(path, state)
                raise PodError("source_changed", "Bound source changed or disappeared")
        return {"status": "current", "assignment": assignment}


def _write(path: Path, value: dict) -> None:
    value["revision"] += 1
    atomic_json(path, value)


def checkpoint(project: Path, objective: str, *, owner: str, value: dict, native: dict) -> dict:
    """Store a bounded recovery pointer after an exact native read."""
    bounded_text(owner, name="owner")
    exact(value, {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
                  "assignments", "questions", "verification_gaps", "next_safe_action",
                  "route_decisions", "quota_visibility"},
          {"schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs",
           "assignments", "questions", "verification_gaps", "next_safe_action"}, name="checkpoint")
    if value["schema"] != "pod-checkpoint/v1" or not isinstance(native.get("runtime"), str):
        raise PodError("invalid_checkpoint", "Checkpoint needs current native runtime readback")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        state["owner"] = owner
        state["checkpoint"] = value
        _write(path, state)
        return state


def _grant_matches(grant: dict | None, *, objective: str, run_id: str, plan_revision: str, capacity: int) -> bool:
    return (isinstance(grant, dict) and grant.get("objective") == objective
            and grant.get("run") == run_id and grant.get("plan_revision") == plan_revision
            and type(grant.get("limit")) is int and grant["limit"] == capacity
            and isinstance(grant.get("reason"), str) and bool(grant["reason"].strip()))


def _pending_account(account: str, bucket: str | None = None) -> int:
    root = state_root()
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    count = 0
    if root.exists():
        for path in root.glob("*/context.json"):
            state = _read(path)
            count += sum(1 for effect in state["effects"].values()
                         if effect.get("state") in ("reserved", "uncertain")
                         and (effect.get("request", {}).get("account") == account
                              or bucket is not None and effect.get("bucket") == bucket))
    return count


def _quota_hold(account: str, snapshot: dict | None, state: str) -> bool:
    """Persist exhaustion until a later fresh positive supported observation."""
    path = state_root() / "quota-holds.json"
    raw = bounded_json(path) if path.exists() else {"schema": "pod-quota-holds/v1", "holds": {}}
    exact(raw, {"schema", "holds"}, {"schema", "holds"}, name="quota_holds")
    if raw["schema"] != "pod-quota-holds/v1" or not isinstance(raw["holds"], dict):
        raise PodError("state_migration_required", "Quota hold schema is unsupported")
    bucket = snapshot.get("bucket") if isinstance(snapshot, dict) else None
    key = digest({"bucket": bucket}) if isinstance(bucket, str) and bucket else None
    # Also retain an account-wide hold when the next observation is absent or
    # cannot name the old bucket.
    account_key = digest({"account": account})
    observed_at = snapshot.get("observed_at") if isinstance(snapshot, dict) else None
    existing = [x for x in (raw["holds"].get(account_key), raw["holds"].get(key) if key else None) if x]
    if state == "exhausted":
        raw["holds"][account_key] = observed_at or "unknown"
        if key:
            raw["holds"][key] = observed_at or "unknown"
        atomic_json(path, raw)
        return True
    if existing:
        def later(candidate: str, previous: str) -> bool:
            try:
                return datetime.fromisoformat(candidate.replace("Z", "+00:00")) > datetime.fromisoformat(previous.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return False
        if state in ("normal", "low", "critical") and isinstance(observed_at, str) and all(later(observed_at, past) for past in existing):
            raw["holds"].pop(account_key, None)
            if key:
                raw["holds"].pop(key, None)
            atomic_json(path, raw)
            return False
        return True
    if state == "unknown" and key is None and raw["holds"]:
        # Without a bucket identity, an observed exhausted shared bucket
        # cannot be ruled out for this route.
        return True
    return False


def reserve(project: Path, objective: str, *, owner: str, operation_id: str, requested: dict,
            route_decision: dict, capability_contract: dict, native_reader: Callable[[], dict],
            capacity: int, run_id: str, plan_revision: str,
            exceptional_grant: dict | None = None, quota_rules: dict | None = None,
            now: datetime | None = None) -> dict:
    """Read native state inside global admission lock, then reserve one effect."""
    bounded_text(operation_id, name="operation_id", limit=128)
    if route_decision.get("status") != "usable" or route_decision.get("selected") != requested:
        raise PodError("route_unusable", "Requested launch is not the approved route decision")
    require_prelaunch_assurance(capability_contract)
    if type(capacity) is not int or capacity < 1 or capacity > 8:
        raise PodError("invalid_capacity", "Capacity must be one through eight")
    if capacity > 3 and not _grant_matches(exceptional_grant, objective=objective, run_id=run_id,
                                           plan_revision=plan_revision, capacity=capacity):
        raise PodError("exceptional_grant_required", "Exceptional capacity requires an exact objective/Run/plan grant")
    path = _path(project, objective)
    # The global lock serializes all local objective/account reservations. It
    # does not fence other hosts; cross-host admission needs native atomicity.
    with _lock(state_root() / "admission"):
        with _lock(path):
            native = native_reader()
            if native.get("runtime") != capability_contract.get("runtime") or not native.get("authoritative"):
                raise PodError("native_authority_unverified", "Native runtime/ownership is not proven")
            if native.get("owner") != owner or native.get("scope") != "all" or native.get("complete") is not True:
                raise PodError("native_occupancy_unverified", "Native owner or complete fleet scope is unproven")
            if native.get("cross_host") and native.get("atomic_admission") is not True:
                raise PodError("distributed_admission_unverified", "Cross-host admission requires native atomic fencing")
            workers = native.get("workers")
            if not isinstance(workers, list):
                raise PodError("native_occupancy_unverified", "Native worker inventory is malformed")
            state = _read(path)
            if state["owner"] != owner:
                raise PodError("coordinator_conflict", "Coordinator ownership is absent or changed")
            prior = state["effects"].get(operation_id)
            if prior is not None:
                if prior["request"] != requested or prior.get("run_id") != run_id or prior.get("plan_revision") != plan_revision:
                    raise PodError("operation_conflict", "Operation identity was reused with a changed request")
                return {**prior, "existing": True}
            active = [w for w in workers if w.get("state") not in ("released",)]
            if any(not isinstance(w, dict) or not w.get("account") or not w.get("objective") for w in active):
                raise PodError("native_occupancy_unverified", "Active worker account/objective binding is unavailable")
            qstate, _ = quota_state(native.get("quota"), account=requested["account"],
                                    policy=quota_rules or {"quota_fresh_seconds": 60, "quota_critical": 5, "quota_low": 20},
                                    now=now or datetime.now(timezone.utc))
            if _quota_hold(requested["account"], native.get("quota"), qstate):
                raise PodError("quota_exhausted", "Current applicable quota bucket is exhausted")
            bucket = native.get("quota", {}).get("bucket") if isinstance(native.get("quota"), dict) and qstate != "unknown" else None
            if active and bucket is not None and any(not w.get("bucket") for w in active):
                raise PodError("bucket_occupancy_unverified", "Active worker bucket binding is unavailable")
            if qstate == "unknown" and any(w["account"] != requested["account"] for w in active):
                raise PodError("bucket_occupancy_unverified", "Shared bucket ownership is unavailable")
            objective_occupied = sum(1 for w in active if w["objective"] == objective)
            account_occupied = sum(1 for w in active if w["account"] == requested["account"]
                                   or bucket is not None and w.get("bucket") == bucket)
            local_objective = sum(1 for e in state["effects"].values() if e["state"] in ("reserved", "uncertain"))
            local_account = _pending_account(requested["account"], bucket)
            if objective_occupied + local_objective >= capacity:
                raise PodError("capacity_full", "Objective capacity is occupied")
            if qstate == "unknown" and account_occupied + local_account >= 1:
                raise PodError("unknown_quota_capacity", "Unknown account quota permits one active worker")
            effect = {"schema": "pod-effect/v1", "state": "reserved", "request": requested,
                      "runtime": native["runtime"], "operation_id": operation_id,
                      "route_revision": route_decision["policy_revision"], "native_binding": None,
                      "run_id": run_id, "plan_revision": plan_revision, "bucket": bucket,
                      "created_at": datetime.now(timezone.utc).isoformat()}
            state["effects"][operation_id] = effect
            _write(path, state)
            return {**effect, "existing": False}


def reconcile(project: Path, objective: str, *, owner: str, operation_id: str,
              observed: dict | None, definitive_absence: bool = False) -> dict:
    """Only exact positive proof settles or frees a reserved/uncertain effect."""
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner or operation_id not in state["effects"]:
            raise PodError("unknown_effect", "No owned effect identity to reconcile")
        effect = state["effects"][operation_id]
        if effect["state"] in ("confirmed", "absent"):
            return effect
        if observed is not None:
            if observed.get("runtime") != effect["runtime"] or observed.get("operation_id") != operation_id:
                raise PodError("effect_identity_mismatch", "Native readback does not join exact effect")
            launch_request = {key: effect["request"][key] for key in ("agent", "model", "effort")}
            effective_launch(launch_request, observed)
            worker = observed.get("worker_show")
            if not isinstance(worker, dict) or worker.get("dispatch", {}).get("id") != observed["dispatchId"]:
                raise PodError("native_identity_unverified", "Worker readback does not join Dispatch")
            projection = worker.get("projection")
            if (not isinstance(projection, dict) or not isinstance(projection.get("id"), str)
                    or projection.get("dispatchId") != observed["dispatchId"]
                    or projection.get("runId") != observed["runId"]
                    or projection.get("taskId") != observed["taskId"]):
                raise PodError("native_identity_unverified", "Worker identity is missing")
            start_options = worker.get("worker", {}).get("startOptions")
            if not isinstance(start_options, dict) or start_options.get("launch") != observed["launch"]:
                raise PodError("effective_launch_unverified", "Worker readback launch changed or is unavailable")
            effect["state"] = "confirmed"
            effect["native_binding"] = {"dispatchId": observed["dispatchId"], "workerId": projection["id"],
                                        "taskId": observed["taskId"], "runId": observed["runId"]}
        elif definitive_absence:
            # Only a native contract capable of proving operation-ID absence may
            # produce this flag; this API does not infer it from an empty list.
            raise PodError("absence_proof_unsupported", "Installed adapter has no definitive absence proof")
        else:
            effect["state"] = "uncertain"
        _write(path, state)
        return effect


def record_delivery(project: Path, objective: str, *, owner: str, delivery: dict,
                    reconciled_message_ids: set[str]) -> dict:
    exact(delivery, {"id", "messages", "runtime"}, {"id", "messages", "runtime"}, name="delivery")
    if not isinstance(delivery["messages"], list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in delivery["messages"]):
        raise PodError("invalid_delivery", "Delivery items are malformed")
    ids = {m["id"] for m in delivery["messages"]}
    if ids != reconciled_message_ids:
        raise PodError("delivery_unresolved", "All Delivery items must be reconciled before acknowledgment")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Delivery belongs to another coordinator")
        prior = state["deliveries"].get(delivery["id"])
        identity = digest(delivery)
        if prior and prior != identity:
            raise PodError("delivery_conflict", "Delivery identity changed")
        state["deliveries"][delivery["id"]] = identity
        _write(path, state)
        return {"ack_eligible": True, "delivery_id": delivery["id"]}


def intervention(project: Path, objective: str, *, owner: str, task: str, correction: dict,
                 diagnosis: dict | None = None) -> dict:
    exact(correction, {"obligation", "failing_example", "hypothesis", "last_meaningful_evidence",
                       "next_discriminating_check", "correction_key"},
          {"obligation", "failing_example", "hypothesis", "last_meaningful_evidence",
           "next_discriminating_check", "correction_key"}, name="correction")
    for item in correction.values():
        bounded_text(item, name="correction")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Correction belongs to another coordinator")
        history = state["interventions"].setdefault(task, [])
        identity = digest(correction)
        if identity in [row["identity"] for row in history]:
            raise PodError("correction_replay", "Equivalent correction was already attempted")
        # The evidence/obligation, rather than a caller-provided correction
        # label, is the bounded equivalence key. Rewording the label cannot
        # reset the no-progress threshold.
        obligation = " ".join(correction["obligation"].casefold().split())
        same = [row for row in history if row.get("obligation") == obligation
                and row["evidence"] == correction["last_meaningful_evidence"]]
        if len(same) >= 2:
            if diagnosis is None:
                raise PodError("diagnosis_required", "Two equivalent failures require a discriminating diagnosis")
            exact(diagnosis, {"diagnosis_evidence"}, {"diagnosis_evidence"}, name="diagnosis")
            bounded_text(diagnosis["diagnosis_evidence"], name="diagnosis_evidence")
            if diagnosis["diagnosis_evidence"] == correction["last_meaningful_evidence"]:
                raise PodError("diagnosis_unproductive", "Diagnosis must add discriminating evidence")
            if digest(diagnosis) in [row.get("diagnosis") for row in history]:
                raise PodError("diagnosis_replay", "Diagnosis evidence was already consumed")
        history.append({"identity": identity, "key": correction["correction_key"],
                        "obligation": obligation,
                        "evidence": correction["last_meaningful_evidence"],
                        "diagnosis": digest(diagnosis) if diagnosis else None})
        _write(path, state)
        return {"correction_identity": identity, "dispatch_authorized": False}

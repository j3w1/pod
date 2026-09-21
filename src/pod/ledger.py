"""Compact private effect journal; native Orca remains the task authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import PodError
from .orca import effective_launch, require_prelaunch_assurance
from .quota import validate_snapshot
from .routing import quota_state
from .util import atomic_json, bounded_json, bounded_text, digest, exact, explicit_home, native_home

_PREDECESSOR_BINDING_FIELDS = {"dispatchId", "workerId", "taskId", "runId"}
_CURRENT_BINDING_FIELDS = _PREDECESSOR_BINDING_FIELDS | {
    "worktreeId", "terminalHandle", "terminalResourceId",
}


def binding_version(binding: object) -> str | None:
    """Classify the immediate predecessor or current exact native binding."""
    if not isinstance(binding, dict):
        return None
    if set(binding) == _PREDECESSOR_BINDING_FIELDS:
        fields = _PREDECESSOR_BINDING_FIELDS
        version = "predecessor"
    elif set(binding) == _CURRENT_BINDING_FIELDS:
        fields = _PREDECESSOR_BINDING_FIELDS | {"worktreeId"}
        version = "current"
    else:
        return None
    if any(not isinstance(binding.get(field), str) or not binding[field] for field in fields):
        return None
    if version == "current":
        for field in ("terminalHandle", "terminalResourceId"):
            if binding[field] is not None and (not isinstance(binding[field], str) or not binding[field]):
                return None
    return version


def state_root(project: Path | None = None) -> Path:
    override = explicit_home("POD_STATE_HOME")
    if override is not None:
        return override
    if os.name == "nt":
        if "LOCALAPPDATA" not in os.environ:
            raise PodError("state_home_unavailable", "LOCALAPPDATA is required for Pod state")
        return native_home("LOCALAPPDATA", project=project) / "pod"
    return native_home("XDG_STATE_HOME", default=Path.home() / ".local" / "state",
                       project=project) / "pod"


def _path(project: Path, objective: str) -> Path:
    return state_root(project) / digest({"project": str(project.resolve()), "objective": objective}) / "context.json"


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
    bounded_text(assignment, name="assignment", limit=128)
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Source check belongs to another coordinator")
        return _check_bound_sources_locked(project, path, state, assignment, sources)


def _check_bound_sources_locked(project: Path, path: Path, state: dict,
                                assignment: str, sources: list[dict]) -> dict:
    """Check source bytes with the objective lock held, immediately before admission."""
    from .records import source_identity
    if state["source_rejections"].get(assignment):
        raise PodError("source_rejected", "Assignment has a durable definitive source rejection")
    for entry in sources:
        exact(entry, {"path", "state", "sha256"}, {"path", "state"}, name="source")
        if entry["state"] == "unavailable":
            # No bytes or absence were frozen. A later observation cannot
            # establish a historical change or bind this assignment.
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
        if observed["state"] == "absent":
            reason = "source_absent"
        elif observed != entry:
            reason = "source_changed"
        else:
            continue
        state["source_rejections"][assignment] = {"reason": reason, "path": entry["path"]}
        _write(path, state)
        raise PodError(reason, "Bound source is absent or changed")
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
    """Rejoin the previewed exact grant and return its durable unit bound."""
    if not isinstance(supplied, dict):
        raise PodError("spending_grant_required", "Paid launch lacks its exact spending grant")
    matches = [grant for grant in grants
               if isinstance(grant, dict) and grant.get("id") == supplied.get("id")]
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


def _spent_grant_units(binding: dict, requested: dict, project: Path) -> int:
    """Count durable reservations for one scoped grant across all local objectives."""
    root = state_root(project)
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    used = 0
    if not root.exists():
        return used
    for path in root.glob("*/context.json"):
        state = _read(path)
        for effect in state["effects"].values():
            if not isinstance(effect, dict) or effect.get("state") not in ("reserved", "uncertain", "confirmed"):
                continue
            authorization = effect.get("spending_grant")
            request = effect.get("request")
            if authorization is None:
                if (isinstance(request, dict) and request.get("account") == requested.get("account")
                        and request.get("model") == requested.get("model")):
                    raise PodError("spending_history_unverified", "Earlier matching launch lacks grant accounting")
                continue
            if not isinstance(authorization, dict):
                raise PodError("state_migration_required", "Spending grant accounting is malformed")
            units = authorization.get("units")
            if type(units) is not int or units < 1 or not isinstance(authorization.get("scope"), str):
                raise PodError("state_migration_required", "Spending grant accounting is malformed")
            if authorization["scope"] == binding["scope"]:
                used += units
    return used


def _bucket_overlap(provider: str, account: str, bucket: str | None, other: dict) -> bool:
    if other.get("account") == account:
        return True
    other_provider = other.get("agent")
    if other_provider is None:
        return True
    if other_provider is not None and other_provider != provider:
        return False
    other_bucket = other.get("bucket")
    return bucket is None or other_bucket is None or bucket == other_bucket


def _local_occupancy(project: Path) -> list[dict]:
    """Project unresolved launch and release evidence into worker identities."""
    root = state_root(project)
    if root.is_symlink():
        raise PodError("unsafe_state", "State root is redirected")
    occupied = []
    if root.exists():
        for path in root.glob("*/context.json"):
            state = _read(path)
            if not isinstance(state["effects"], dict) or not isinstance(state["cleanup"], dict):
                raise PodError("state_migration_required", "Effect or cleanup journal is malformed")
            confirmed = {}
            for operation_id, effect in state["effects"].items():
                if not isinstance(effect, dict):
                    raise PodError("state_migration_required", "Effect row is malformed")
                status = effect.get("state")
                if status not in ("reserved", "uncertain", "confirmed"):
                    # No installed adapter can prove an effect was absent. An
                    # unfamiliar disposition cannot silently free its slot.
                    raise PodError("effect_disposition_unverified", "Effect disposition needs exact proof")
                request = effect.get("request")
                if (not isinstance(request, dict) or not request.get("account") or not request.get("agent")
                        or effect.get("bucket") != request.get("bucket")):
                    raise PodError("state_migration_required", "Effect account binding is malformed")
                runtime = effect.get("runtime")
                if not isinstance(runtime, str) or not runtime:
                    raise PodError("state_migration_required", "Effect runtime binding is malformed")
                key = ("launch", str(path), operation_id)
                if status == "confirmed":
                    binding = effect.get("native_binding")
                    if binding_version(binding) is None:
                        raise PodError("effect_identity_unverified", "Confirmed launch lacks exact native binding")
                    key = ("dispatch", runtime, binding["dispatchId"])
                    confirmed.setdefault(binding["dispatchId"], []).append(effect)
                    if len(confirmed[binding["dispatchId"]]) != 1:
                        raise PodError("effect_identity_unverified", "Dispatch has multiple confirmed launch bindings")
                occupied.append({**request, "bucket": effect.get("bucket"),
                                 "_key": key, "_path": path, "_dispatch":
                                 effect["native_binding"]["dispatchId"] if status == "confirmed" else None})
            for dispatch, cleanup in state["cleanup"].items():
                if not isinstance(cleanup, dict):
                    raise PodError("state_migration_required", "Cleanup row is malformed")
                cleanup_state = cleanup.get("state")
                if cleanup_state not in ("reserved", "uncertain", "retained", "released",
                                         "already_released", "release_pending", "release_unknown"):
                    raise PodError("state_migration_required", "Cleanup state is unsupported")
                binding = cleanup.get("binding")
                matches = [effect for effect in confirmed.get(dispatch, [])
                           if effect.get("native_binding") == binding
                           and effect.get("runtime") == cleanup.get("runtime")]
                if len(matches) != 1:
                    raise PodError("cleanup_identity_unverified", "Cleanup has no unique exact launch binding")
                if cleanup_state in ("released", "already_released"):
                    occupied = [row for row in occupied
                                if not (row["_path"] == path and row["_dispatch"] == dispatch
                                        and row["_key"] == ("dispatch", cleanup["runtime"], dispatch))]
    return occupied


def _occupancy_projection(native: dict, objective_path: Path, objective: str,
                          requested: dict, project: Path) -> tuple[set[tuple], set[tuple], list[dict], list[dict]]:
    """Use one identity set for objective and overlapping-account admission."""
    workers = native["workers"]
    active = []
    seen = {}
    for index, worker in enumerate(workers):
        status = worker.get("state")
        if status == "released":
            continue
        if status not in ("occupied", "active", "launching", "reserved", "uncertain", "confirmed", "retained"):
            raise PodError("native_occupancy_unverified", "Native worker state is unsupported")
        if not worker.get("account") or not worker.get("objective"):
            raise PodError("native_occupancy_unverified", "Active worker account/objective binding is unavailable")
        dispatch = worker.get("dispatchId")
        key = (("dispatch", native["runtime"], dispatch)
               if isinstance(dispatch, str) and dispatch else ("native", index))
        identity = (worker["account"], worker["objective"], worker.get("agent"), worker.get("bucket"))
        if key in seen and seen[key] != identity:
            raise PodError("native_occupancy_unverified", "Native Dispatch has conflicting occupancy bindings")
        seen[key] = identity
        active.append({**worker, "_key": key})
    local = _local_occupancy(project)
    for row in local:
        if row["_key"] in seen and (row["account"] != seen[row["_key"]][0]
                                    or (seen[row["_key"]][2] is not None
                                        and row["agent"] != seen[row["_key"]][2])
                                    or (seen[row["_key"]][3] is not None
                                        and row.get("bucket") != seen[row["_key"]][3])):
            raise PodError("native_occupancy_unverified", "Native and local Dispatch bindings conflict")
    bucket = requested.get("bucket")
    overlap = [row for row in [*active, *local]
               if _bucket_overlap(requested["agent"], requested["account"], bucket, row)]
    objective_keys = {row["_key"] for row in active if row["objective"] == objective}
    objective_keys.update(row["_key"] for row in local if row["_path"] == objective_path)
    account_keys = {row["_key"] for row in overlap}
    return objective_keys, account_keys, active, overlap


def _quota_hold(provider: str, account: str, bucket: str | None,
                snapshot: dict | None, state: str, *, now: datetime, freshness: int,
                project: Path | None = None) -> bool:
    """Persist exhaustion until a later fresh positive supported observation."""
    path = state_root(project) / "quota-holds.json"
    raw = bounded_json(path) if path.exists() else {"schema": "pod-quota-holds/v3", "holds": {}}
    exact(raw, {"schema", "holds"}, {"schema", "holds"}, name="quota_holds")
    if raw["schema"] != "pod-quota-holds/v3" or not isinstance(raw["holds"], dict):
        raise PodError("state_migration_required", "Quota hold schema is unsupported")
    key = digest({"provider": provider, "account": account, "bucket": bucket})
    row = raw["holds"].get(key, {"provider": provider, "account": account,
                                  "bucket": bucket, "windows": {}})
    if not isinstance(row, dict) or any(row.get(field) != value for field, value in
                                        (("provider", provider), ("account", account), ("bucket", bucket))) or not isinstance(row.get("windows"), dict):
        raise PodError("state_migration_required", "Quota hold identity is malformed")
    windows = dict(row["windows"])
    supported = None
    if state != "unknown" and isinstance(snapshot, dict):
        try:
            candidate = validate_snapshot(snapshot)
            observed = datetime.fromisoformat(candidate["observed_at"].replace("Z", "+00:00"))
            if (candidate["provider"] == provider and candidate["account"] == account
                    and candidate["bucket"] == bucket and candidate["source"] in ("supported", "supported_metadata")
                    and candidate["confidence"] == "observed" and observed.tzinfo is not None
                    and now.tzinfo is not None and 0 <= (now - observed).total_seconds()):
                supported = (candidate, observed, (now - observed).total_seconds() <= freshness)
        except (PodError, TypeError, ValueError, OverflowError):
            pass
    if supported:
        candidate, observed, fresh = supported
        values = {digest({"window": window["name"]}): window.get("remaining_percent")
                  for window in candidate["windows"]}
        if "remaining_percent" in candidate:
            values[digest({"aggregate": True})] = candidate["remaining_percent"]
        for name, value in values.items():
            previous = windows.get(name)
            if previous is not None:
                try:
                    older = datetime.fromisoformat(previous.replace("Z", "+00:00"))
                except (AttributeError, ValueError) as exc:
                    raise PodError("state_migration_required", "Quota hold timestamp is malformed") from exc
                if older.tzinfo is None:
                    raise PodError("state_migration_required", "Quota hold timestamp has no timezone")
            if value == 0 and (previous is None or observed > older):
                windows[name] = observed.isoformat()
            elif fresh and value is not None and value > 0 and previous is not None and observed > older:
                windows.pop(name)
        if windows:
            raw["holds"][key] = {**row, "windows": windows}
        else:
            raw["holds"].pop(key, None)
        atomic_json(path, raw)
    if state == "exhausted":
        return True
    if windows:
        return True
    if bucket is None:
        return any(isinstance(other, dict) and other.get("provider") == provider
                   and other.get("account") == account and other.get("windows")
                   for other in raw["holds"].values())
    return False


def reserve(project: Path, objective: str, *, owner: str, operation_id: str, requested: dict,
            route_decision: dict, capability_contract: dict, native_reader: Callable[[], dict],
            capacity: int, run_id: str, plan_revision: str,
            exceptional_grant: dict | None = None, capacity_reason: str | None = None,
            frozen_packet: dict | None = None,
            now: datetime | None = None) -> dict:
    """Read native state inside global admission lock, then reserve one effect."""
    bounded_text(operation_id, name="operation_id", limit=128)
    if route_decision.get("status") != "usable" or route_decision.get("selected") != requested:
        raise PodError("route_unusable", "Requested launch is not the approved route decision")
    require_prelaunch_assurance(capability_contract, requested)
    if type(capacity) is not int or capacity < 1 or capacity > 8:
        raise PodError("invalid_capacity", "Capacity must be one through eight")
    packet_id = None
    if frozen_packet is not None:
        from .records import packet
        if (not isinstance(frozen_packet, dict) or not isinstance(frozen_packet.get("body"), dict)
                or packet(frozen_packet["body"]) != frozen_packet
                or frozen_packet["body"]["plan_revision"] != plan_revision
                or frozen_packet["body"]["objective"] != objective
                or frozen_packet["body"]["policy_revision"] != route_decision.get("policy_revision")
                or frozen_packet["body"]["route"] != requested):
            raise PodError("invalid_packet", "Admission packet is not frozen for this plan")
        packet_id = frozen_packet["packet_id"]
    from .config import effective
    effective_policy = effective(project)
    policy = effective_policy["policy"]["policy"]
    route_model = effective_policy["policy"]["models"].get(requested.get("alias"))
    if isinstance(route_model, dict) and (route_model.get("agent") != requested.get("agent")
            or route_model.get("model") != requested.get("model")
            or route_model.get("account") != requested.get("account")):
        raise PodError("route_unusable", "Requested route no longer matches effective policy")
    current_time = now or datetime.now(timezone.utc)
    spending_grant = None
    spending_limit = None
    if isinstance(route_model, dict) and route_model.get("billing", "unknown") != "included":
        spending_grant, spending_limit = _spending_grant_binding(
            policy["spending_grants"], route_decision.get("spending_grant"),
            requested=requested, objective=objective, now=current_time)
    elif route_decision.get("spending_grant") is not None:
        raise PodError("spending_grant_mismatch", "Included route cannot consume a paid grant")
    if capacity <= 3:
        if capacity > min(policy["max_workers"], policy["ordinary_max"]):
            raise PodError("capacity_ceiling", "Capacity exceeds effective personal/ordinary policy")
        if capacity == 3 and not (isinstance(capacity_reason, str) and capacity_reason.strip()):
            raise PodError("capacity_reason_required", "Three workers require a concrete reason")
    elif not _grant_matches(exceptional_grant, policy["exceptional_grants"], objective=objective,
                            run_id=run_id, plan_revision=plan_revision, account=requested["account"],
                            capacity=capacity, now=now or datetime.now(timezone.utc)):
        raise PodError("exceptional_grant_required", "Exceptional capacity requires a current effective personal grant")
    elif (effective_policy["provenance"].get("policy.max_workers") in ("project", "task")
          and capacity > policy["max_workers"]):
        raise PodError("capacity_ceiling", "Local hard capacity restriction remains effective")
    path = _path(project, objective)
    # The global lock serializes all local objective/account reservations. It
    # does not fence other hosts; cross-host admission needs native atomicity.
    with _lock(state_root(project) / "admission"):
        with _lock(path):
            current = effective(project)
            if current["revision"] != route_decision.get("policy_revision") or current["policy"]["policy"] != policy:
                raise PodError("policy_revision_mismatch", "Admission policy changed after route selection")
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
            if any(not isinstance(worker, dict) for worker in workers):
                raise PodError("native_occupancy_unverified", "Native worker row is malformed")
            state = _read(path)
            if state["owner"] != owner:
                raise PodError("coordinator_conflict", "Coordinator ownership is absent or changed")
            if frozen_packet is not None:
                checkpoint_value = state.get("checkpoint")
                if (not isinstance(checkpoint_value, dict)
                        or checkpoint_value.get("candidate") != frozen_packet["body"]["candidate"]
                        or checkpoint_value.get("criteria") != frozen_packet["body"]["criteria"]):
                    raise PodError("packet_plan_mismatch", "Packet differs from the owned checkpoint")
                body = frozen_packet["body"]
                bound = [*body["sources"], *({"path": ref["path"], "state": "present", "sha256": ref["sha256"]}
                                             for ref in body["context"] if ref["kind"] in ("source", "instruction"))]
                _check_bound_sources_locked(project, path, state, packet_id, bound)
            prior = state["effects"].get(operation_id)
            if prior is not None:
                if (prior["request"] != requested or prior.get("run_id") != run_id
                        or prior.get("plan_revision") != plan_revision or prior.get("packet_id") != packet_id
                        or prior.get("spending_grant") != spending_grant):
                    raise PodError("operation_conflict", "Operation identity was reused with a changed request")
                return {**prior, "existing": True}
            if (spending_grant is not None
                    and _spent_grant_units(spending_grant, requested, project) + 1 > spending_limit):
                raise PodError("spending_grant_exhausted", "Spending grant has no unreserved units")
            qstate, _ = quota_state(native.get("quota"), provider=requested["agent"],
                                    account=requested["account"], bucket=requested.get("bucket"),
                                    policy=policy,
                                    now=now or datetime.now(timezone.utc))
            if _quota_hold(requested["agent"], requested["account"], requested.get("bucket"),
                           native.get("quota"), qstate, now=now or datetime.now(timezone.utc),
                           freshness=policy["quota_fresh_seconds"], project=project):
                raise PodError("quota_exhausted", "Current applicable quota bucket is exhausted")
            bucket = requested.get("bucket")
            objective_keys, account_keys, active, overlapping = _occupancy_projection(
                native, path, objective, requested, project)
            if bucket is not None and any(not row.get("bucket") for row in overlapping):
                raise PodError("bucket_occupancy_unverified", "Occupied worker bucket binding is unavailable")
            if qstate == "unknown" and any(row["account"] != requested["account"]
                                           for row in active if row in overlapping):
                raise PodError("bucket_occupancy_unverified", "Shared bucket ownership is unavailable")
            if len(objective_keys) >= capacity:
                raise PodError("capacity_full", "Objective capacity is occupied")
            if qstate == "unknown" and account_keys:
                raise PodError("unknown_quota_capacity", "Unknown account quota permits one active worker")
            effect = {"schema": "pod-effect/v1", "state": "reserved", "request": requested,
                      "runtime": native["runtime"], "operation_id": operation_id,
                      "route_revision": route_decision["policy_revision"], "native_binding": None,
                      "run_id": run_id, "plan_revision": plan_revision, "bucket": bucket,
                      "packet_id": packet_id, "spending_grant": spending_grant,
                      "created_at": datetime.now(timezone.utc).isoformat()}
            state["effects"][operation_id] = effect
            _write(path, state)
            return {**effect, "existing": False}


def reconcile(project: Path, objective: str, *, owner: str, operation_id: str,
              observed: dict | None, definitive_absence: bool = False) -> dict:
    """Only exact positive proof settles or frees a reserved/uncertain effect."""
    path = _path(project, objective)
    with _lock(state_root(project) / "admission"), _lock(path):
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
            native_dispatch = worker.get("dispatch")
            projection = worker.get("projection")
            if (not isinstance(projection, dict) or not isinstance(projection.get("id"), str)
                    or projection.get("dispatchId") != observed["dispatchId"]
                    or projection.get("runId") != observed["runId"]
                    or projection.get("taskId") != observed["taskId"]):
                raise PodError("native_identity_unverified", "Worker identity is missing")
            native_worker = worker.get("worker")
            resource = worker.get("terminalResource")
            terminal = worker.get("terminal")
            terminal_handle = native_worker.get("agentTerminalHandle") if isinstance(native_worker, dict) else None
            if (not isinstance(native_dispatch, dict)
                    or native_dispatch.get("runId") != observed["runId"]
                    or native_dispatch.get("taskId") != observed["taskId"]
                    or not isinstance(native_worker, dict)
                    or native_worker.get("dispatchId") != observed["dispatchId"]
                    or not isinstance(native_worker.get("worktreeId"), str)
                    or not native_worker["worktreeId"]
                    or terminal_handle is not None and (not isinstance(terminal_handle, str)
                                                        or not terminal_handle)):
                raise PodError("native_identity_unverified", "Worker terminal resource identity is missing or contradictory")
            if resource is None:
                if (terminal_handle is None and terminal is not None
                        or terminal_handle is not None
                        and terminal is not None
                        and (not isinstance(terminal, dict)
                             or terminal.get("handle") != terminal_handle)):
                    raise PodError("native_identity_unverified", "Terminal-less worker identity is contradictory")
                resource_id = None
            else:
                if (not isinstance(resource, dict)
                        or not all(isinstance(resource.get(field), str) and resource[field]
                                   for field in ("id", "terminalHandle", "worktreeId"))
                        or resource.get("originDispatchId") != observed["dispatchId"]
                        or resource.get("ownerDispatchId") != observed["dispatchId"]
                        or resource.get("worktreeId") != native_worker["worktreeId"]
                        or terminal_handle != resource.get("terminalHandle")
                        or resource.get("ownershipState") != "owned"
                        or resource.get("releaseState") != "not_requested"
                        or resource.get("retainedReason") is not None
                        or resource.get("releaseRequestedAt") is not None
                        or resource.get("releaseCompletedAt") is not None
                        or resource.get("releaseError") is not None
                        or resource.get("archive") != {"source": None, "status": None}
                        or (terminal is not None and (not isinstance(terminal, dict)
                                                      or terminal.get("handle") != terminal_handle))):
                    raise PodError("native_identity_unverified", "Worker terminal resource identity is missing or contradictory")
                resource_id = resource["id"]
            start_options = native_worker.get("startOptions")
            if not isinstance(start_options, dict) or start_options.get("launch") != observed["launch"]:
                raise PodError("effective_launch_unverified", "Worker readback launch changed or is unavailable")
            effect["state"] = "confirmed"
            effect["native_binding"] = {"dispatchId": observed["dispatchId"], "workerId": projection["id"],
                                        "taskId": observed["taskId"], "runId": observed["runId"],
                                        "worktreeId": native_worker["worktreeId"],
                                        "terminalHandle": terminal_handle,
                                        "terminalResourceId": resource_id}
        elif definitive_absence:
            # Only a native contract capable of proving operation-ID absence may
            # produce this flag; this API does not infer it from an empty list.
            raise PodError("absence_proof_unsupported", "Installed adapter has no definitive absence proof")
        else:
            effect["state"] = "uncertain"
        _write(path, state)
        return effect


def record_delivery(project: Path, objective: str, *, owner: str, delivery: dict) -> dict:
    """Journal each immutable native item; eligibility reads only durable effects."""
    exact(delivery, {"id", "messages", "runtime", "runId"},
          {"id", "messages", "runtime", "runId"}, name="delivery")
    for key in ("id", "runtime", "runId"):
        bounded_text(delivery[key], name=key, limit=128)
    if not isinstance(delivery["messages"], list) or len(delivery["messages"]) > 64:
        raise PodError("invalid_delivery", "Delivery items are malformed")
    items = {}
    for message in delivery["messages"]:
        item = exact(message, {"id", "type", "runId", "taskId", "dispatchId"},
                     {"id", "type", "runId"}, name="delivery_item")
        if item["type"] not in ("worker_done", "question", "escalation", "heartbeat") or item["runId"] != delivery["runId"]:
            raise PodError("invalid_delivery", "Delivery item type or Run is unsupported")
        for key, value in item.items():
            bounded_text(value, name=key, limit=128)
        if item["type"] == "worker_done" and not all(item.get(key) for key in ("taskId", "dispatchId")):
            raise PodError("invalid_delivery", "Settlement needs exact Task and Dispatch")
        if item["id"] in items:
            raise PodError("invalid_delivery", "Duplicate message identity")
        items[item["id"]] = {"identity": digest({"runtime": delivery["runtime"], **item}),
                             "item": item, "effect": None}
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Delivery belongs to another coordinator")
        prior = state["deliveries"].get(delivery["id"])
        identity = digest(delivery)
        if prior and (not isinstance(prior, dict) or prior.get("identity") != identity):
            raise PodError("delivery_conflict", "Delivery identity changed")
        if not prior:
            state["deliveries"][delivery["id"]] = {"identity": identity, "runtime": delivery["runtime"],
                                                   "runId": delivery["runId"], "items": items}
            _write(path, state)
        unresolved = [key for key, row in state["deliveries"][delivery["id"]]["items"].items()
                      if row["effect"] is None]
        return {"ack_eligible": not unresolved, "delivery_id": delivery["id"], "unresolved": unresolved}


def reconcile_delivery_item(project: Path, objective: str, *, owner: str, delivery_id: str,
                            message_id: str, native_reader: Callable[[], dict]) -> dict:
    """Persist an effect from a trusted native readback seam before acknowledgment."""
    native_effect = native_reader()
    effect = exact(native_effect, {"runtime", "runId", "messageId", "taskId", "dispatchId",
                                   "kind", "status", "receiptId"},
                   {"runtime", "runId", "messageId", "kind", "status", "receiptId"}, name="delivery_effect")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner or delivery_id not in state["deliveries"]:
            raise PodError("unknown_delivery", "No owned Delivery")
        delivery = state["deliveries"][delivery_id]
        row = delivery["items"].get(message_id)
        if row is None:
            raise PodError("unknown_delivery_item", "Message is not in this Delivery")
        item = row["item"]
        if (effect["runtime"] != delivery["runtime"] or effect["runId"] != delivery["runId"]
                or effect["messageId"] != message_id or effect.get("taskId") != item.get("taskId")
                or effect.get("dispatchId") != item.get("dispatchId")):
            raise PodError("delivery_effect_mismatch", "Effect does not join exact native item")
        if item["type"] == "worker_done":
            bindings = [e for e in state["effects"].values() if e.get("state") == "confirmed"
                        and e.get("runtime") == effect["runtime"] and e.get("native_binding", {}).get("runId") == effect["runId"]
                        and e.get("native_binding", {}).get("taskId") == effect["taskId"]
                        and e.get("native_binding", {}).get("dispatchId") == effect["dispatchId"]]
            cleanup = state["cleanup"].get(effect["dispatchId"], {})
            if (effect["kind"] != "settlement" or effect["status"] not in ("completed", "failed")
                    or len(bindings) != 1 or cleanup.get("state") not in ("released", "already_released", "retained")):
                raise PodError("delivery_unresolved", "Settlement and terminal disposition are not durably reconciled")
        elif item["type"] == "question":
            if effect["kind"] != "reply" or effect["status"] != "replied":
                raise PodError("delivery_unresolved", "Question has no native reply receipt")
        elif item["type"] == "heartbeat":
            if effect["kind"] != "observation" or effect["status"] != "recorded":
                raise PodError("delivery_unresolved", "Heartbeat has no recorded observation")
        else:
            if effect["kind"] != "decision" or effect["status"] != "recorded":
                raise PodError("delivery_unresolved", "Escalation has no recorded decision")
        bounded_text(effect["receiptId"], name="receiptId", limit=128)
        if row["effect"] and row["effect"] != effect:
            raise PodError("delivery_effect_conflict", "A different effect was already recorded")
        if not row["effect"]:
            row["effect"] = effect
            _write(path, state)
        return {"status": "reconciled", "delivery_id": delivery_id, "message_id": message_id}


def intervention(project: Path, objective: str, *, owner: str, task: str, correction: dict,
                 diagnosis: dict | None = None) -> dict:
    bounded_text(task, name="task", limit=128)
    exact(correction, {"criterion_id", "failure_id", "obligation", "failing_example", "hypothesis", "last_meaningful_evidence",
                       "next_discriminating_check", "correction_key"},
          {"criterion_id", "failure_id", "obligation", "failing_example", "hypothesis", "last_meaningful_evidence",
           "next_discriminating_check", "correction_key"}, name="correction")
    for item in correction.values():
        bounded_text(item, name="correction")
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Correction belongs to another coordinator")
        if not any(effect.get("state") == "confirmed" and
                   isinstance(effect.get("native_binding"), dict) and
                   effect["native_binding"].get("taskId") == task
                   for effect in state["effects"].values() if isinstance(effect, dict)):
            raise PodError("task_unbound", "Correction Task lacks a confirmed native effect binding")
        checkpoint_value = state.get("checkpoint")
        criteria = checkpoint_value.get("criteria") if isinstance(checkpoint_value, dict) else None
        if not isinstance(criteria, list) or correction["criterion_id"] not in criteria:
            raise PodError("criterion_unbound", "Correction criterion is absent from the accepted checkpoint")
        history = state["interventions"].setdefault(task, [])
        identity = digest(correction)
        if identity in [row["identity"] for row in history]:
            raise PodError("correction_replay", "Equivalent correction was already attempted")
        # The caller supplies failure labels and evidence descriptions. Neither
        # changes the durable per-Task threshold. A later attempt needs a new
        # bounded source observation as explicit discriminating evidence.
        diagnosis_source = None
        if len(history) >= 2:
            if diagnosis is None:
                raise PodError("diagnosis_required", "Two equivalent failures require a discriminating diagnosis")
            exact(diagnosis, {"diagnosis_evidence"}, {"diagnosis_evidence"}, name="diagnosis")
            bounded_text(diagnosis["diagnosis_evidence"], name="diagnosis_evidence")
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
                        "obligation": correction["obligation"],
                        "failing_example": correction["failing_example"],
                        "hypothesis": correction["hypothesis"],
                        "next_discriminating_check": correction["next_discriminating_check"],
                        "evidence": correction["last_meaningful_evidence"],
                        "diagnosis": digest(diagnosis) if diagnosis else None,
                        "diagnosis_source": diagnosis_source,
                        "diagnosis_source_digest": diagnosis_source["sha256"] if diagnosis_source else None})
        _write(path, state)
        return {"correction_identity": identity, "dispatch_authorized": False}

"""Exact fenced-assignment accounting, without asserting that execution never began."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from .errors import PodError
from .ledger import _native_assignment_settled, objective_root, read, update_admission
from .orca import _mutation_envelope
from .util import bounded_json, bounded_text, digest, normalize_timestamp


def _refuse(detail: str) -> None:
    raise PodError("native_abandoned_unverified", detail + "; retain the original attempt and reconcile through Orca")


def validate(admission: dict) -> None:
    """The optional admission field also makes preceding readers refuse these semantics."""
    value = admission["abandoned_accounting"]
    fields = {"classification", "execution", "recorded_by", "reason", "proof", "fence",
              "native_abandoner", "native_abandon_reason", "previous_error"}
    if (not isinstance(value, dict) or set(value) != fields
            or value["classification"] != "abandoned_no_observed_start" or value["execution"] != "unknown"
            or value["native_abandoner"] != "unknown" or value["native_abandon_reason"] != "unknown"
            or not isinstance(value["proof"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["proof"])
            or value["proof"] not in admission["recovery"].get("start_observations", [])
            or admission["state"] not in ("bound", "closed") or admission["report"] is not None
            or admission["changed_paths"] is not None
            or admission.get("disposition") is not None and admission["disposition"].get("kind") != "discarded"
            or value["previous_error"] is not None and not isinstance(value["previous_error"], dict)
            or not isinstance(value["fence"], dict)
            or set(value["fence"]) != {"capabilityRevokedAt", "completedAt"}):
        raise PodError("state_unsupported", "Abandoned accounting record is malformed")
    bounded_text(value["reason"], name="abandoned accounting reason", limit=1024)
    bounded_text(value["recorded_by"], name="abandoned accounting actor", limit=4096)
    for key, timestamp in value["fence"].items():
        normalize_timestamp(timestamp, field=key, code="state_unsupported")


def _input_unobserved(value: dict, terminal: str) -> bool:
    effects = value.get("effects")
    if not isinstance(effects, list) or any(not isinstance(row, dict) for row in effects):
        return False
    inputs = [row for row in effects if row.get("kind") == "dispatch_input"]
    return (isinstance(terminal, str) and bool(terminal)
            and value.get("turnStart") in (None, "unobserved") and bool(inputs)
            and all(row.get("id") == terminal and row.get("role") == "agent"
                    and row.get("state") in ("accepted", "turn_unobserved") for row in inputs)
            and {row["state"] for row in inputs} == {"accepted", "turn_unobserved"})


def current_fence(shown: dict, admission: dict, binding: dict) -> dict:
    """Require current positive abandonment; neither terminal liveness nor silence is proof."""
    from .operations import _identity_from_show
    if _identity_from_show(shown, admission, binding["dispatchId"]) != binding:
        _refuse("Abandoned readback identity changed")
    result = shown["result"]
    dispatch, worker, projection = (result[key] for key in ("dispatch", "worker", "projection"))
    stage = projection.get("stage", {})
    if (worker.get("state") != "abandoned" or worker.get("stage") != "abandoned"
            or dispatch.get("status") != "failed" or dispatch.get("lastFailure") != "abandoned"
            or projection.get("outcome") != "failed" or not _native_assignment_settled(shown)
            or not isinstance(stage, dict) or stage.get("worker") != "abandoned"
            or stage.get("detail") != "abandoned" or stage.get("activity") not in (None, "unknown")
            or dispatch.get("lastHeartbeatAt") is not None
            or not _input_unobserved(worker, binding["terminalHandle"])):
        _refuse("Native abandonment or unobserved-start evidence is missing or conflicting")
    for key in ("capabilityRevokedAt", "completedAt"):
        try:
            normalize_timestamp(dispatch.get(key), field=key, code="native_abandoned_unverified")
        except PodError:
            _refuse("Native abandonment lacks its capability fence and completion time")
    if (worker.get("runtimeEpoch", admission["runtime"]) != admission["runtime"]
            or dispatch.get("assigneeHandle", binding["terminalHandle"]) != binding["terminalHandle"]
            or dispatch.get("task_id", admission["task_id"]) != admission["task_id"]):
        _refuse("Native abandonment carries contradictory identity")
    prompt = worker.get("prompt")
    if (prompt is not None and (not isinstance(prompt, dict)
                              or prompt.get("stages") != ["input_accepted"])
            or any(row.get("startedAt") is not None for row in (dispatch, worker))):
        _refuse("Current readback retains an observed or unclassified start")
    workspace, ownership = projection.get("workspace"), projection.get("resource")
    if (workspace is not None and (not isinstance(workspace, dict)
                                  or workspace.get("id") != binding["worktreeId"])
            or not isinstance(ownership, dict)
            or ownership.get("ownerDispatchId") not in _owners(ownership, binding)):
        _refuse("Current workspace or retained ownership contradicts the assignment")
    resource = result.get("terminalResource")
    if resource is not None and (not isinstance(resource, dict)
            or resource.get("ownerDispatchId") not in _owners(ownership, binding)
            or resource.get("terminalHandle") != binding["terminalHandle"]
            or resource.get("worktreeId") != binding["worktreeId"]
            or resource.get("releaseState") != ownership.get("releaseState")
            or resource.get("ownershipState") != ownership.get("state")):
        _refuse("Retained resource identity does not join the abandoned attempt")
    fence = {key: dispatch[key] for key in ("capabilityRevokedAt", "completedAt")}
    recorded = admission.get("abandoned_accounting")
    if recorded is not None and (not isinstance(recorded, dict) or recorded.get("fence") != fence):
        _refuse("Previously accounted native fence changed")
    return fence


def _owners(ownership: dict, binding: dict) -> tuple:
    """Only a released resource may have lost its named owner; a live one never joins another attempt."""
    released = ownership.get("releaseState") == "released"
    return (binding["dispatchId"], None) if released else (binding["dispatchId"],)


def release_evidence(shown: dict) -> str | None:
    """Native proof that nothing of this attempt can still run: the process exited or the resource was released.

    `abandoned` fences the worker without claiming its process stopped, so settlement alone proves neither.
    """
    projection = shown["result"].get("projection")
    projection = projection if isinstance(projection, dict) else {}
    liveness, resource = projection.get("liveness"), projection.get("resource")
    if isinstance(liveness, dict) and liveness.get("verdict") == "exited":
        return "process_exited"
    if (isinstance(resource, dict) and resource.get("state") == "released"
            and resource.get("releaseState") == "released"):
        return "resource_released"
    return None


def hold_scope(admission: dict) -> dict:
    """What a retained, possibly-live attempt can still affect: its worktree and branch."""
    placement = admission["recovery"].get("placement_binding")
    placement = placement if isinstance(placement, dict) else {}
    return {"worktree": admission["native_binding"]["worktreeId"], "branch": placement.get("branch"),
            "path": placement.get("path")}


def _unobserved_receipt(receipt: dict, admission: dict, request: str) -> str:
    from .operations import _check_retry_dispatch, _refusal_request_reference
    prompt = receipt.get("prompt")
    observed, valid = _refusal_request_reference(receipt)
    if (receipt.get("runtime") != admission["runtime"]
            or receipt.get("runId") != admission["run_id"]
            or receipt.get("taskId") != admission["task_id"]
            or not isinstance(receipt.get("dispatchId"), str)
            or receipt.get("state") != "outcome_unknown"
            or receipt.get("stage") != "turn_start_unobserved" or receipt.get("turnStart") != "unobserved"
            or receipt.get("error") is not None or receipt.get("_request_conflict")
            or receipt.get("_envelope_conflicts") or receipt.get("_result_error")
            or not valid or observed != request
            or not isinstance(prompt, dict) or prompt.get("requestId") != request
            or prompt.get("stages") != ["input_accepted"]):
        _refuse("Start receipt does not prove this exact request's unobserved start")
    if admission.get("retry_of") is not None and "retryOfDispatchId" in receipt:
        _check_retry_dispatch(receipt, admission)
    return receipt["dispatchId"]


def _observations(project: Path, objective: str, admission: dict, request: str) -> tuple[list[str], list[dict]]:
    from .operations import _start_observations, START_OBSERVATION_LIMIT
    refs = _start_observations(project, objective, admission)
    receipts = []
    for ref in refs:
        value = bounded_json(objective_root(project, objective) / "native-start" / (ref + ".json"),
                             limit=START_OBSERVATION_LIMIT)
        if any(value.get(key) != admission[field] for key, field in
               (("run", "run_id"), ("task", "task_id"), ("runtime", "runtime"))):
            _refuse("Start observation belongs to another native identity")
        observation = value.get("observation")
        if not isinstance(observation, dict):
            _refuse("Start observation is unreadable")
        if "abandoned_accounting" in observation:
            continue  # A prior interrupted accounting read is not a start observation.
        try:
            if "receipt" in observation:
                receipt = observation["receipt"]
            else:
                captured = observation["stdout"]
                raw = base64.b64decode(captured["base64"], validate=True)
                if (observation.get("transport") != "completed" or len(raw) != captured["bytes"]
                        or hashlib.sha256(raw).hexdigest() != captured["sha256"]):
                    raise ValueError("incomplete capture")
                envelope = _mutation_envelope(raw)
                receipt = {**envelope["result"], "runtime": envelope["runtime"],
                           "request_uuid": envelope["request_uuid"]}
            if not isinstance(receipt, dict) or value.get("retry_request") not in (None, request):
                raise ValueError("invalid observation")
        except (KeyError, TypeError, ValueError, PodError) as exc:
            raise PodError("native_abandoned_unverified", "An earlier start observation cannot be classified; retain it") from exc
        _unobserved_receipt(receipt, admission, request)
        receipts.append(receipt)
    if not receipts:
        _refuse("No immutable positive unobserved-start receipt exists")
    return refs, receipts


def recover(project: Path, objective: str, *, owner: str, admission: dict, reason: str, port) -> dict:
    """Account one existing request using reads only; never replay worker-start."""
    from .operations import (_check_native_placement, _check_retry_placement, _identity_from_show, _known_request_conflict,
                             _observe_start, _receipt_from_request_show, _retry_parent_link,
                             _reuse_effective_evidence, _valid_request_uuid)
    bounded_text(reason, name="abandoned_reason", limit=1024)
    previous = admission.get("abandoned_accounting")
    if previous is not None:
        if previous.get("reason") != reason:
            raise PodError("admission_conflict", "Abandoned accounting reason is immutable")
        return {"status": admission["state"], "admission": admission, "action": "reuse",
                **_current_hold(admission, port)}
    if (admission["state"] not in ("reserved", "unresolved") or _known_request_conflict(admission)
            or admission.get("report") is not None or admission.get("disposition") is not None
            or admission.get("changed_paths") is not None):
        _refuse("Only an unresolved original request without a known request conflict can be accounted")
    request = _valid_request_uuid(admission.get("request_uuid"))
    authority = port.read_native(owner, authority_runs=(admission["run_id"],))
    if (authority.get("authoritative") is not True or authority.get("owner") != owner
            or authority.get("runtime") != admission["runtime"]):
        raise PodError("native_authority_unverified", "Abandoned accounting needs current original Run authority and runtime")
    state = read(project, objective)
    _retry_parent_link(state, admission)
    refs, receipts = _observations(project, objective, admission, request)
    request_read = port.request_show(request)
    if request_read.get("runtime") != admission["runtime"]:
        _refuse("Request readback belongs to another runtime")
    status, receipt = _receipt_from_request_show(request_read, request)
    if status == "completed" and receipt is None:
        _refuse("Completed request lacks its receipt")
    if receipt is not None:
        receipts.append({"runtime": admission["runtime"], **receipt})
    dispatches = {_unobserved_receipt(row, admission, request) for row in receipts}
    if len(dispatches) != 1:
        _refuse("Request observations identify conflicting Dispatches")
    dispatch = next(iter(dispatches))
    shown = port.show_worker(dispatch)
    binding = _identity_from_show(shown, admission, dispatch)
    _check_retry_placement(port, binding, admission)
    placement = admission["recovery"].get("placement_binding")
    if not isinstance(placement, dict):
        _refuse("Original placement evidence is missing")
    observed = _check_native_placement(port, "id:" + binding["worktreeId"], placement)
    if observed["runtime"] != admission["runtime"]:
        _refuse("Actual abandoned worktree belongs to another runtime")
    fence = current_fence(shown, admission, binding)
    for row in receipts:
        if not _input_unobserved(row, binding["terminalHandle"]):
            _refuse("Start input effects do not join the current terminal or retain an observed start")
        for effect in row["effects"]:
            if effect.get("kind") == "worktree" and effect.get("id") != binding["worktreeId"]:
                _refuse("Start effects name another worktree")
    effective, mismatch, inherited = _reuse_effective_evidence(
        receipts[-1], shown, admission, state["admissions"].get(admission.get("reuse_of")))
    observation = {"abandoned_accounting": {"shown": shown, "request_show": request_read,
                    "recorded_by": owner, "reason": reason}}
    recorded = _observe_start(project, objective, owner=owner, admission_id=admission["admission_id"],
                              observation=observation, retry_request=None, reference={})
    proof = digest({"admission_id": admission["admission_id"], "run": admission["run_id"],
                    "task": admission["task_id"], "runtime": admission["runtime"],
                    "retry_request": None, "observation": observation})
    expected_refs = refs if proof in refs else [*refs, proof]
    if recorded["recovery"]["start_observations"] != expected_refs:
        _refuse("Start observations changed during accounting")

    def apply(row):
        if (row["state"] not in ("reserved", "unresolved") or row["request_uuid"] != request
                or row["recovery"].get("start_observations") != expected_refs
                or _known_request_conflict(row)):
            _refuse("Original request changed during accounting")
        row["abandoned_accounting"] = {
            "classification": "abandoned_no_observed_start", "execution": "unknown",
            "recorded_by": owner, "reason": reason, "proof": proof, "fence": fence,
            "native_abandoner": "unknown", "native_abandon_reason": "unknown",
            "previous_error": row["error"]}
        row["state"], row["native_binding"] = "bound", binding
        row["error"] = {"code": "route_mismatch"} if mismatch else None
        row["effective_evidence"] = {"requested": row["request"], "effective": effective}
        if inherited is not None:
            row["effective_evidence"]["inherited"] = inherited
        row["route_decision"].update(effective=effective, effective_unknown=any(
            effective[key] == "unknown" for key in ("agent", "model", "effort")))
        if mismatch:
            from .util import flag_route_mismatch
            flag_route_mismatch(row)

    row = update_admission(project, objective, owner=owner, admission_id=admission["admission_id"],
                           update=apply, expected_runtime=admission["runtime"])
    return {"status": row["state"], "admission": row, "action": "accounted_abandoned",
            **_hold_fields(row, release_evidence(shown))}


def _hold_fields(row: dict, release: str | None) -> dict:
    return {"release": release, "hold": None if release else hold_scope(row)}


def _current_hold(admission: dict, port) -> dict:
    """Repeating the accounting reads again; an unreadable or contradictory read keeps the hold."""
    binding = admission["native_binding"]
    try:
        shown = port.show_worker(binding["dispatchId"])
        current_fence(shown, admission, binding)
        release = release_evidence(shown)
    except PodError:
        release = None
    return _hold_fields(admission, release)

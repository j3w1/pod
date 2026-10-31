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


_MODES = {"abandoned_no_observed_start": "unknown", "abandoned_observed_start": "observed"}


def validate(admission: dict) -> None:
    """The optional admission field also makes preceding readers refuse these semantics."""
    value = admission["abandoned_accounting"]
    fields = {"classification", "execution", "recorded_by", "reason", "proof", "fence",
              "native_abandoner", "native_abandon_reason", "previous_error", "observed_start"}
    if (not isinstance(value, dict) or set(value) != fields
            or _MODES.get(value["classification"]) != value["execution"]
            or value["native_abandoner"] != "unknown" or value["native_abandon_reason"] != "unknown"
            or (value["observed_start"] is None) != (value["execution"] == "unknown")
            or value["observed_start"] is not None and not isinstance(value["observed_start"], dict)
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


def observed_start(shown: dict) -> dict:
    """Positive native evidence that execution began, kept as small bounded facts; empty means none seen."""
    result = shown["result"]
    worker, dispatch = result["worker"], result["dispatch"]
    facts: dict = {}
    if worker.get("turnStart") == "observed":
        facts["turnStart"] = "observed"
    for key, source, field in (("workerStartedAt", worker, "startedAt"), ("dispatchStartedAt", dispatch, "startedAt"),
                               ("lastHeartbeatAt", dispatch, "lastHeartbeatAt")):
        if source.get(field) is not None:
            facts[key] = source[field] if isinstance(source[field], str) and len(source[field]) <= 64 else "present"
    prompt = worker.get("prompt")
    if isinstance(prompt, dict) and any(stage != "input_accepted" for stage in prompt.get("stages") or []):
        facts["promptStages"] = [str(stage)[:64] for stage in prompt["stages"][:8]]
    effects = worker.get("effects")
    if isinstance(effects, list) and any(isinstance(row, dict) and row.get("kind") == "dispatch_input"
                                         and row.get("state") == "turn_observed" for row in effects):
        facts["turnObservedEffect"] = True
    return facts


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
    """Require current positive abandonment; neither terminal liveness nor silence is proof.

    A recorded accounting fixes the start classification: an unobserved start never later becomes
    observed here, and an observed one never regresses. Before any record the read itself decides.
    """
    from .operations import _identity_from_show
    if _identity_from_show(shown, admission, binding["dispatchId"]) != binding:
        _refuse("Abandoned readback identity changed")
    result = shown["result"]
    dispatch, worker, projection = (result[key] for key in ("dispatch", "worker", "projection"))
    stage = projection.get("stage", {})
    recorded = admission.get("abandoned_accounting")
    if recorded is not None and not isinstance(recorded, dict):
        _refuse("Previously accounted native fence changed")
    facts = observed_start(shown)
    observed = bool(facts) if recorded is None else recorded.get("classification") == "abandoned_observed_start"
    if observed and not facts:
        _refuse("Recorded observed start is missing from the current readback")
    if (worker.get("state") != "abandoned" or worker.get("stage") != "abandoned"
            or dispatch.get("status") != "failed" or dispatch.get("lastFailure") != "abandoned"
            or projection.get("outcome") != "failed" or not _native_assignment_settled(shown)
            or not isinstance(stage, dict) or stage.get("worker") != "abandoned"
            or stage.get("detail") != "abandoned"
            or not observed and (stage.get("activity") not in (None, "unknown")
                                 or dispatch.get("lastHeartbeatAt") is not None
                                 or not _input_unobserved(worker, binding["terminalHandle"]))
            or observed and not _inputs_join_terminal(worker, binding["terminalHandle"])):
        _refuse("Native abandonment or start evidence is missing or conflicting")
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
    if (not observed and (prompt is not None and (not isinstance(prompt, dict)
                                                  or prompt.get("stages") != ["input_accepted"])
                          or any(row.get("startedAt") is not None for row in (dispatch, worker)))):
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
    if recorded is not None and recorded.get("fence") != fence:
        _refuse("Previously accounted native fence changed")
    return fence


def _inputs_join_terminal(value: dict, terminal: str) -> bool:
    """With an observed start the input states vary, but every input still names this terminal."""
    effects = value.get("effects")
    if effects is None:
        return True
    if not isinstance(effects, list) or any(not isinstance(row, dict) for row in effects):
        return False
    return all(row.get("id") == terminal and row.get("role") == "agent"
               for row in effects if row.get("kind") == "dispatch_input")


def _owners(ownership: dict, binding: dict) -> tuple:
    """Only a released resource may have lost its named owner; a live one never joins another attempt."""
    released = ownership.get("releaseState") == "released"
    return (binding["dispatchId"], None) if released else (binding["dispatchId"],)


_BENIGN_EFFECTS = ("worktree", "dispatch_input")


def is_abandoned(shown: dict) -> bool:
    result = shown.get("result") if isinstance(shown, dict) else None
    worker = result.get("worker") if isinstance(result, dict) else None
    dispatch = result.get("dispatch") if isinstance(result, dict) else None
    return (isinstance(worker, dict) and worker.get("state") == "abandoned"
            or isinstance(dispatch, dict) and dispatch.get("lastFailure") == "abandoned")


MAX_PENDING_EFFECTS = 64


def _pending_effects(worker: dict) -> list[dict]:
    """Effects beyond the worktree and the accepted input, with full identities.

    Nothing is truncated: an identity is compared in full. Only a count past the bound is not listed;
    that overflow is recorded as one effect of unknown identity, which holds the whole repository.
    """
    effects = worker.get("effects")
    if not isinstance(effects, list):
        return [{"kind": "unknown", "id": None, "state": None}]
    pending = []
    for row in effects:
        if isinstance(row, dict) and row.get("kind") in _BENIGN_EFFECTS:
            continue
        row = row if isinstance(row, dict) else {}
        pending.append({"kind": row["kind"] if isinstance(row.get("kind"), str) and row["kind"] else "unknown",
                        "id": row["id"] if isinstance(row.get("id"), str) and row["id"] else None,
                        "state": row["state"] if isinstance(row.get("state"), str) and row["state"] else None})
    if len(pending) > MAX_PENDING_EFFECTS:
        pending = pending[:MAX_PENDING_EFFECTS] + [{"kind": "unknown", "id": None, "state": None}]
    return pending


def current_claim(shown: dict) -> dict | None:
    """The fresh claim of an abandoned attempt, or None when it is not abandoned or nothing can still run.

    `abandoned` fences the worker without claiming its process stopped, so settlement proves nothing
    about the claim. It is gone only when the native read positively reports the process exited and no
    effect beyond the worktree and accepted input is left unresolved. A live, unverifiable, missing or
    contradictory process verdict always keeps the claim, whatever the resource bookkeeping says: a
    released resource is not proof that the process stopped. This is the one decision every admission,
    readiness, retirement and succession boundary uses; it is derived from the current read, never from
    an accounting receipt.
    """
    if not is_abandoned(shown):
        return None
    result = shown["result"]
    worker = result.get("worker") if isinstance(result.get("worker"), dict) else {}
    projection = result.get("projection") if isinstance(result.get("projection"), dict) else {}
    liveness = projection.get("liveness")
    verdict = liveness.get("verdict") if isinstance(liveness, dict) else None
    effects = _pending_effects(worker)
    if verdict == "exited" and not effects:
        return None
    return {"process": verdict if isinstance(verdict, str) and verdict else "unknown", "effects": effects}


def release_evidence(shown: dict) -> str | None:
    """`process_exited` when the fresh claim is gone, else None."""
    return "process_exited" if is_abandoned(shown) and current_claim(shown) is None else None


def hold_scope(admission: dict, claim: dict | None = None) -> dict:
    """What a possibly-live attempt can still affect: worktree, branch, paths, surfaces and pending effects."""
    placement = admission["recovery"].get("placement_binding")
    placement = placement if isinstance(placement, dict) else {}
    boundary = admission.get("boundary") or {}
    claim = claim or {"process": "unknown", "effects": []}
    return {"worktree": admission["native_binding"]["worktreeId"], "repo_key": placement.get("repo_key"),
            "branch": placement.get("branch"), "path": placement.get("path"),
            "paths": sorted(set(boundary.get("paths") or []) | set(admission.get("changed_paths") or [])),
            "surfaces": sorted(boundary.get("surfaces") or []),
            "process": claim["process"], "effects": claim["effects"]}


def _effect_conflicts(hold: dict, placement: dict) -> bool:
    """A pending effect blocks an assignment in its repository that may touch its target.

    The repository is part of the key: the same target name elsewhere is independent, but an unknown
    repository on either side cannot establish independence. An effect of unknown identity holds the
    whole repository.
    """
    held_repo, repo = hold.get("repo_key"), placement.get("repo_key")
    if held_repo is not None and repo is not None and held_repo != repo:
        return False
    branch = placement.get("branch")
    for effect in hold.get("effects") or []:
        ident = effect.get("id")
        if effect.get("kind") == "unknown" or not isinstance(ident, str):
            return True
        if branch is not None and branch in (ident, ident.split("/", 1)[-1]):
            return True
    return False


def refuse_held_claim(projection: dict, placement: dict | None) -> None:
    """A new assignment that may touch a held attempt's worktree, branch or pending-effect target waits.

    An independent worktree, branch and target stays usable. Path and surface overlap is refused by the
    obligation kernel over the same outstanding rows, and Governor counts every held claim.
    """
    placement = placement if isinstance(placement, dict) else {}
    for row in projection.get("outstanding", []):
        hold = row.get("hold")
        if not isinstance(hold, dict):
            continue
        same_path = hold.get("path") is not None and hold.get("path") == placement.get("path")
        same_branch = (hold.get("branch") is not None and hold.get("branch") == placement.get("branch")
                       and hold.get("repo_key") == placement.get("repo_key"))
        if same_path or same_branch or _effect_conflicts(hold, placement):
            raise PodError("abandoned_claim_held",
                           "An abandoned attempt that may still run holds this worktree, branch or target. Next: read "
                           "its exact native terminal and wait for positive exit and resolved effects; an independent "
                           "worktree, branch and target is unaffected",
                           {"admission": row.get("admission_id"), "worktree": hold.get("worktree"),
                            "branch": hold.get("branch")})


def _receipt_dispatch(receipt: dict, admission: dict, request: str) -> tuple[str, bool]:
    """The Dispatch one start receipt names for this exact request, and whether it proves an observed start."""
    from .operations import STARTED_STATES, _check_retry_dispatch, _refusal_request_reference
    prompt = receipt.get("prompt")
    observed_ref, valid = _refusal_request_reference(receipt)
    if (receipt.get("runtime") != admission["runtime"]
            or receipt.get("runId") != admission["run_id"]
            or receipt.get("taskId") != admission["task_id"]
            or not isinstance(receipt.get("dispatchId"), str)
            or receipt.get("error") is not None or receipt.get("_request_conflict")
            or receipt.get("_envelope_conflicts") or receipt.get("_result_error")
            or not valid or observed_ref not in (None, request)
            or prompt is not None and (not isinstance(prompt, dict) or prompt.get("requestId") != request)):
        _refuse("Start receipt does not belong to this exact request")
    if admission["state"] == "bound":
        # Identity is already bound; the receipt only has to name the same Dispatch.
        return receipt["dispatchId"], False
    unobserved = (receipt.get("state") == "outcome_unknown" and receipt.get("stage") == "turn_start_unobserved"
                  and receipt.get("turnStart") == "unobserved" and isinstance(prompt, dict)
                  and prompt.get("stages") == ["input_accepted"])
    observed = receipt.get("turnStart") == "observed" or receipt.get("state") in STARTED_STATES
    if unobserved == observed or unobserved and observed_ref != request:
        _refuse("Start receipt proves neither an unobserved nor an observed start of this exact request")
    if admission.get("retry_of") is not None and "retryOfDispatchId" in receipt:
        _check_retry_dispatch(receipt, admission)
    return receipt["dispatchId"], observed


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
        _receipt_dispatch(receipt, admission, request)
        receipts.append(receipt)
    return refs, receipts


def recover(project: Path, objective: str, *, owner: str, admission: dict, reason: str, port) -> dict:
    """Account one existing request using reads only; never replay worker-start.

    Two cases share the shared identity adapter and the same non-success, no-evidence settlement.
    An unobserved start (unresolved or reserved) is recorded with execution unknown. An observed
    start (an unresolved start later proved by the readback, or an already bound admission) is
    recorded as observed, keeping every earlier fact; output and findings are neither read nor
    discarded here, and no report is manufactured.
    """
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
    if (admission["state"] not in ("reserved", "unresolved", "bound") or _known_request_conflict(admission)
            or admission.get("report") is not None or admission.get("disposition") is not None
            or admission.get("changed_paths") is not None):
        _refuse("Only an unresolved or bound original request without a report, disposition or known request "
                "conflict can be accounted")
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
    kinds = [_receipt_dispatch(row, admission, request) for row in receipts]
    bound = admission.get("native_binding")
    dispatches = {dispatch for dispatch, _ in kinds} | ({bound["dispatchId"]} if admission["state"] == "bound" else set())
    if len(dispatches) != 1:
        _refuse("Request observations do not identify exactly one Dispatch")
    dispatch = next(iter(dispatches))
    shown = port.show_worker(dispatch)
    binding = _identity_from_show(shown, admission, dispatch)
    if admission["state"] == "bound" and bound != binding:
        _refuse("Bound admission identity differs from the abandoned readback")
    _check_retry_placement(port, binding, admission)
    placement = admission["recovery"].get("placement_binding")
    if not isinstance(placement, dict):
        _refuse("Original placement evidence is missing")
    observed = _check_native_placement(port, "id:" + binding["worktreeId"], placement)
    if observed["runtime"] != admission["runtime"]:
        _refuse("Actual abandoned worktree belongs to another runtime")
    fence = current_fence(shown, admission, binding)
    facts = observed_start(shown)
    if admission["state"] == "bound" and not facts:
        _refuse("A bound admission is accounted only with an observed start")
    if any(seen for _, seen in kinds) and not facts:
        _refuse("An immutable observed start contradicts the unobserved readback")
    if not facts and not receipts:
        _refuse("No immutable positive unobserved-start receipt exists")
    for row in receipts:
        if not (_inputs_join_terminal(row, binding["terminalHandle"]) if facts
                else _input_unobserved(row, binding["terminalHandle"])):
            _refuse("Start input effects do not join the current terminal or retain an observed start")
        for effect in row.get("effects") or []:
            if effect.get("kind") == "worktree" and effect.get("id") != binding["worktreeId"]:
                _refuse("Start effects name another worktree")
    effective = mismatch = inherited = None
    if admission["state"] != "bound":
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
        if (row["state"] not in ("reserved", "unresolved", "bound") or row["request_uuid"] != request
                or row["recovery"].get("start_observations") != expected_refs
                or _known_request_conflict(row)
                or row["state"] == "bound" and row["native_binding"] != binding):
            _refuse("Original request changed during accounting")
        was_bound = row["state"] == "bound"
        row["abandoned_accounting"] = {
            "classification": "abandoned_observed_start" if facts else "abandoned_no_observed_start",
            "execution": "observed" if facts else "unknown",
            "recorded_by": owner, "reason": reason, "proof": proof, "fence": fence,
            "native_abandoner": "unknown", "native_abandon_reason": "unknown",
            "previous_error": row["error"], "observed_start": facts or None}
        row["state"], row["native_binding"] = "bound", binding
        if was_bound:
            return
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
            **_hold_fields(row, shown)}


def _hold_fields(row: dict, shown: dict) -> dict:
    claim = current_claim(shown)
    return {"release": None if claim else "process_exited", "hold": hold_scope(row, claim) if claim else None}


def _current_hold(admission: dict, port) -> dict:
    """Repeating the accounting reads again; an unreadable or contradictory read keeps the hold."""
    binding = admission["native_binding"]
    try:
        shown = port.show_worker(binding["dispatchId"])
        current_fence(shown, admission, binding)
        return _hold_fields(admission, shown)
    except PodError:
        return {"release": None, "hold": hold_scope(admission)}

"""Coordinator-proposed native worker starts with serialized admission and exact recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Protocol
from uuid import UUID

from .errors import PodError
from .ledger import (admission_identity, binding_valid, objective_root, read, reserve,
                     update_admission, _lock, _path, _read, _write, _native_assignment_settled)
from .orca import (MAX_OUTPUT, PREFLIGHT_REFUSALS, contract, current_run, mutate_command, read_command,
                   worker_rows, worker_show, worktree_identity, worktree_selector)
from .util import atomic_json, bounded_json, bounded_text, digest

STARTED_STATES = ("ready", "running", "succeeded", "failed", "stopped")
MAX_START_OBSERVATIONS = 16
START_OBSERVATION_LIMIT = 3 * MAX_OUTPUT


def _check_objective_source(project: Path, binding: dict | None, *, issue_port=None) -> None:
    if binding is None:
        return
    from .github import issue_recheck
    result = issue_recheck(project, binding, port=issue_port)
    if result["status"] != "current":
        raise PodError("issue_reconciliation_required",
                       "Execution Spec issue changed or closed; reconcile before another effect")


def _check_worktree_binding(project: Path, binding: dict | None) -> None:
    if binding is None:
        return
    from .github import repository_context
    current = repository_context(project)
    current_repository = current["repository"]
    expected_repository = binding.get("repository")
    repository_matches = (current_repository is None and expected_repository is None
                          or isinstance(current_repository, str)
                          and isinstance(expected_repository, str)
                          and current_repository.casefold() == expected_repository.casefold())
    if (not repository_matches
            or current["repo_key"] != binding.get("repo_key")
            or current["worktree"] != binding.get("path")
            or current["branch"] != binding.get("branch")):
        raise PodError("worktree_binding_changed",
                       "Current Git repository, branch, or worktree differs from the objective binding")


def _check_native_placement(native_port, selector: str, binding: dict | None) -> dict | None:
    """Join the selector Orca will use to the packet's explicit placement binding."""
    if binding is None:
        return None
    try:
        observed = native_port.resolve_worktree(selector)
    except (AttributeError, PodError) as exc:
        if isinstance(exc, PodError) and exc.code in (
                "worktree_resolution_unavailable", "worktree_resolution_ambiguous"):
            raise
        raise PodError("worktree_resolution_unavailable",
                       "Native worker placement could not be resolved") from exc
    if not isinstance(observed, dict):
        raise PodError("worktree_resolution_ambiguous", "Native worktree readback is malformed")
    current_repository = observed.get("repository")
    expected_repository = binding.get("repository")
    repository_matches = (current_repository is None and expected_repository is None
                          or isinstance(current_repository, str)
                          and isinstance(expected_repository, str)
                          and current_repository.casefold() == expected_repository.casefold())
    if (not repository_matches or observed.get("repo_key") != binding.get("repo_key")
            or observed.get("path") != binding.get("path")
            or observed.get("branch") != binding.get("branch")):
        raise PodError("worktree_binding_changed",
                       "Resolved native worker placement differs from the frozen assignment workspace")
    if not isinstance(observed.get("runtime"), str) or not observed["runtime"]:
        raise PodError("worktree_resolution_ambiguous", "Native placement lacks runtime identity")
    return observed


def _assignment_evidence(shown: dict, admission: dict) -> dict:
    """Project settlement for one already-bound assignment, never terminal ownership."""
    binding = admission.get("native_binding")
    if not binding_valid(binding) or shown.get("runtime") != admission.get("runtime"):
        raise PodError("native_assignment_unverified", "Exact assignment runtime or binding differs")
    result = shown.get("result") if isinstance(shown, dict) else None
    dispatch = result.get("dispatch") if isinstance(result, dict) else None
    projection = result.get("projection") if isinstance(result, dict) else None
    worker = result.get("worker") if isinstance(result, dict) else None
    if not all(isinstance(row, dict) for row in (dispatch, projection, worker)):
        raise PodError("native_assignment_unverified", "Exact assignment readback is incomplete")
    if (dispatch.get("id") != binding["dispatchId"]
            or dispatch.get("runId") != binding["runId"]
            or dispatch.get("taskId") != binding["taskId"]
            or projection.get("id") != binding["workerId"]
            or projection.get("dispatchId") != binding["dispatchId"]
            or projection.get("runId") != binding["runId"]
            or projection.get("taskId") != binding["taskId"]
            or worker.get("dispatchId") != binding["dispatchId"]
            or worker.get("worktreeId") != binding["worktreeId"]):
        raise PodError("native_assignment_unverified", "Exact assignment identity differs")
    return {"admission_id": admission["admission_id"], "runtime": shown["runtime"],
            "run_id": binding["runId"], "task_id": binding["taskId"],
            "dispatch_id": binding["dispatchId"], "worker_id": binding["workerId"],
            "settled": _native_assignment_settled(shown)}


class NativePort(Protocol):
    def capability(self) -> dict: ...
    def read_native(self, owner: str, *, authority_runs: tuple[str, ...] = (),
                    assignments: tuple[dict, ...] = ()) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str, retry_request: str | None = None,
                     terminal: str | None = None) -> dict: ...
    def request_show(self, request_uuid: str) -> dict: ...
    def find_worker(self, *, run: str, task: str) -> list[dict]: ...
    def show_worker(self, dispatch: str) -> dict: ...
    def resolve_worktree(self, selector: str) -> dict: ...


class OrcaPort:
    """Thin installed-Orca port: bounded reads and worker-start only."""

    def __init__(self, project: Path | None = None):
        self.project = project

    def capability(self) -> dict:
        snapshot = contract()
        if (snapshot.get("status") != "observed"
                or snapshot.get("capabilities", {}).get("launch_preferences_v1") is not True):
            raise PodError("launch_preferences_unavailable", "Orca cannot apply per-worker model preferences")
        return snapshot

    def resolve_worktree(self, selector: str) -> dict:
        from .github import repository_context
        resolved = worktree_identity(selector)
        context = repository_context(Path(resolved["path"]))
        return {**resolved, "repository": context["repository"], "repo_key": context["repo_key"]}

    def read_native(self, owner: str, *, authority_runs: tuple[str, ...] = (),
                    assignments: tuple[dict, ...] = ()) -> dict:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        if (not isinstance(authority_runs, tuple)
                or any(not isinstance(run_id, str) or not run_id for run_id in authority_runs)
                or len(set(authority_runs)) != len(authority_runs)):
            raise PodError("native_authority_unverified", "Native authority Run set is malformed")
        expected_runs = tuple(sorted(authority_runs))
        if not isinstance(assignments, tuple) or any(not isinstance(row, dict) for row in assignments):
            raise PodError("native_assignment_unverified", "Assignment evidence request is malformed")
        binding_before = current_run() if expected_runs else None
        evidence = [_assignment_evidence(self.show_worker(row["native_binding"]["dispatchId"]), row)
                    for row in assignments]
        binding_after = current_run() if expected_runs else None
        observed = binding_before or binding_after
        if observed is not None:
            runtime = observed["runtime"]
        elif evidence:
            runtime = evidence[0]["runtime"]
        else:
            snapshot = contract()
            runtime = snapshot.get("runtime")
            if snapshot.get("status") != "observed" or not isinstance(runtime, str):
                raise PodError("native_authority_unverified", "Orca runtime identity is unavailable")
        if any(row["runtime"] != runtime for row in evidence):
            raise PodError("orca_runtime_changed", "Exact assignment evidence changed Orca runtime")
        if expected_runs and (binding_before["runtime"] != runtime or binding_after["runtime"] != runtime):
            raise PodError("orca_runtime_changed", "Current Run binding changed Orca runtime")
        current = binding_before["run"] if binding_before is not None else None
        stable = bool(current is not None and current == binding_after["run"])
        authoritative = bool(expected_runs and handle and handle == owner and stable
                             and current.get("id") in expected_runs
                             and current.get("coordinator_handle") == owner)
        return {"runtime": runtime, "owner": owner if authoritative else None,
                "authoritative": authoritative, "scope": "objective_assignments",
                "complete": True, "assignments": evidence, "physical_capacity": "unavailable"}

    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str = "current", retry_request: str | None = None,
                     terminal: str | None = None) -> dict:
        selector = worktree_selector(worktree)
        if selector is None:
            raise PodError("invalid_worktree_selector", "Worker placement must name an existing worktree")
        argv = ["orchestration", "worker-start", "--task", task, "--run", run,
                "--worktree", selector]
        if terminal is not None:
            argv += ["--terminal", terminal]
        else:
            argv += ["--agent", route["agent"], "--model", route["model"]]
            if route["effort"] != "native_default":
                argv += ["--effort", route["effort"]]
        if retry_request is not None:
            _valid_request_uuid(retry_request)
            argv += ["--retry-request", retry_request]
        receipt = mutate_command(argv + ["--json"], accept_exit=(0, 1))
        return {"runtime": receipt["runtime"], "exit": receipt["exit"],
                "request_uuid": receipt.get("request_uuid"), **receipt["result"],
                "native_observation": receipt.get("native_observation")}

    def request_show(self, request_uuid: str) -> dict:
        _valid_request_uuid(request_uuid)
        return read_command(["orchestration", "request-show", "--request", request_uuid, "--json"])

    def find_worker(self, *, run: str, task: str) -> list[dict]:
        fleet = worker_rows(run)
        return [worker for worker in fleet["workers"]
                if worker.get("taskId") == task or isinstance(worker.get("projection"), dict)
                and worker["projection"].get("taskId") == task]

    def show_worker(self, dispatch: str) -> dict:
        return worker_show(dispatch)


def _valid_request_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise PodError("native_request_invalid", "Orca request UUID is unavailable")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise PodError("native_request_invalid", "Orca request UUID is invalid") from exc
    if str(parsed) != value.lower():
        raise PodError("native_request_invalid", "Orca request UUID is not canonical")
    return value


def _receipt_from_request_show(shown: dict, request_uuid: str) -> tuple[str, dict | None]:
    result = shown.get("result") if isinstance(shown, dict) else None
    if not isinstance(result, dict):
        raise PodError("native_request_unverified", "Orca request readback is malformed")
    if result.get("requestId") != request_uuid:
        raise PodError("native_request_mismatch", "Orca request readback names another request")
    status = result.get("status", result.get("state"))
    if status not in ("completed", "pending", "absent"):
        raise PodError("native_request_unverified", "Orca request state is unsupported")
    if status in ("completed", "pending") and result.get("method") != "orchestration.workerStart":
        raise PodError("native_request_mismatch", "Orca request readback names another mutation")
    receipt = result.get("receipt", result.get("result", result.get("response")))
    return status, receipt if isinstance(receipt, dict) else None


def _binding_from_show(shown: dict, admission: dict, dispatch: str) -> dict:
    if shown.get("runtime") != admission["runtime"]:
        raise PodError("native_identity_unverified", "Orca runtime changed")
    result = shown.get("result")
    native_dispatch = result.get("dispatch") if isinstance(result, dict) else None
    projection = result.get("projection") if isinstance(result, dict) else None
    worker = result.get("worker") if isinstance(result, dict) else None
    if not all(isinstance(row, dict) for row in (native_dispatch, projection, worker)):
        raise PodError("native_identity_unverified", "Worker readback is incomplete")
    if (native_dispatch.get("id") != dispatch
            or native_dispatch.get("runId") != admission["run_id"]
            or native_dispatch.get("taskId") != admission["task_id"]
            or projection.get("dispatchId") != dispatch
            or projection.get("runId") != admission["run_id"]
            or projection.get("taskId") != admission["task_id"]
            or worker.get("dispatchId") != dispatch):
        raise PodError("native_identity_unverified", "Worker readback does not join admission")
    if worker.get("state") not in STARTED_STATES:
        raise PodError("native_start_unsettled", "Worker has not proved that start occurred")
    binding = {"runId": admission["run_id"], "taskId": admission["task_id"],
               "dispatchId": dispatch, "workerId": projection.get("id"),
               "worktreeId": worker.get("worktreeId"),
               "terminalHandle": worker.get("agentTerminalHandle")}
    if not binding_valid(binding):
        raise PodError("native_identity_unverified", "Worker identity is incomplete")
    return binding


def _observed_value(value: object) -> str:
    return value if isinstance(value, str) and value.strip() and len(value) <= 256 else "unknown"


def _effective_evidence(receipt: dict, shown: dict, admission: dict) -> tuple[dict, bool]:
    result = shown["result"]
    worker = result["worker"]
    start_options = worker.get("startOptions")
    launch = start_options.get("launch") if isinstance(start_options, dict) else None
    receipt_launch = receipt.get("launch") if isinstance(receipt, dict) else None
    observed = launch.get("effective") if isinstance(launch, dict) else None
    claimed = receipt_launch.get("effective") if isinstance(receipt_launch, dict) else None
    observed_requested = launch.get("requested") if isinstance(launch, dict) else None
    claimed_requested = receipt_launch.get("requested") if isinstance(receipt_launch, dict) else None
    fields = ("agent", "model", "effort")
    def known_conflict(left: object, right: object) -> bool:
        return (isinstance(left, dict) and isinstance(right, dict)
                and any(_observed_value(left.get(key)) != "unknown"
                        and _observed_value(right.get(key)) != "unknown"
                        and _observed_value(left.get(key)) != _observed_value(right.get(key))
                        for key in fields))

    # Receipt and worker-show can represent the same unknown field as absent or null.
    # Only two known, relevant values can contradict one another.
    mismatch = (known_conflict(observed, claimed)
                or known_conflict(observed_requested, claimed_requested))
    # Orca reports null launch fields for a reused terminal, which sends no model or effort
    # flag. Absent, null or malformed effective values are unknown, never observed settings.
    effective = {key: _observed_value(observed.get(key)) if isinstance(observed, dict) else "unknown"
                 for key in fields}
    effective["context"] = "native_default"
    requested = admission["request"]
    for key in fields:
        expected = requested[key]
        if expected != "native_default" and (
                isinstance(observed_requested, dict)
                and _observed_value(observed_requested.get(key)) not in ("unknown", expected)
                or isinstance(claimed_requested, dict)
                and _observed_value(claimed_requested.get(key)) not in ("unknown", expected)):
            mismatch = True
        if effective[key] != "unknown" and requested[key] != "native_default" and effective[key] != requested[key]:
            mismatch = True
        if (isinstance(claimed, dict) and requested[key] != "native_default"
                and _observed_value(claimed.get(key)) not in ("unknown", requested[key])):
            mismatch = True
    return effective, mismatch


def _bind(project: Path, objective: str, *, owner: str, admission_id: str,
          receipt: dict, port: NativePort, request_uuid: str | None) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    if receipt.get("runtime") not in (None, admission["runtime"]):
        raise PodError("native_identity_unverified", "Start receipt changed runtime")
    dispatch = receipt.get("dispatchId")
    if (receipt.get("runId") != admission["run_id"] or receipt.get("taskId") != admission["task_id"]
            or not isinstance(dispatch, str)):
        raise PodError("native_identity_unverified", "Start receipt identity is incomplete")
    shown = port.show_worker(dispatch)
    binding = _binding_from_show(shown, admission, dispatch)
    effective, mismatch = _effective_evidence(receipt, shown, admission)
    def apply(row: dict) -> None:
        recovery = dict(row.get("recovery", {}))
        recovery.pop("preflight_refusal", None)
        recovery.pop("request_conflict", None)
        recovery["receipt"] = "recorded"
        row["state"] = "bound"
        row["native_binding"] = binding
        row["request_uuid"] = request_uuid
        row["error"] = {"code": "route_mismatch"} if mismatch else None
        row["recovery"] = recovery
        row["effective_evidence"] = {"requested": admission["request"], "effective": effective}
        row["route_decision"]["effective"] = effective
        row["route_decision"]["route_mismatch"] = mismatch
        row["route_decision"]["effective_unknown"] = any(
            effective[key] == "unknown" for key in ("agent", "model", "effort"))
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


def _hold(project: Path, objective: str, *, owner: str, admission_id: str,
          request_uuid: str | None, code: str, detail: object,
          request_conflict: bool = False) -> dict:
    def apply(row: dict) -> None:
        recovery = {**row.get("recovery", {}),
                    "next": "request-show" if request_uuid else "exact Run/Task/Dispatch read"}
        previous_error = (row.get("error") or {}).get("code")
        previous_refusal = recovery.get("preflight_refusal")
        if code == "native_refusal_unverified":
            recovery["preflight_refusal"] = "unverified"
        if (request_conflict or code == "native_request_conflict"
                or previous_error == "native_request_conflict"
                or previous_refusal == "unverified"):
            recovery["request_conflict"] = "unresolved"
        row["state"] = "unresolved"
        row["request_uuid"] = request_uuid
        row["error"] = {"code": code, "detail": detail}
        row["recovery"] = recovery
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


def _start_observations(project: Path, objective: str, admission: dict) -> list[str]:
    """Validate immutable native evidence before recovery or another same-request call."""
    refs = admission.get("recovery", {}).get("start_observations", [])
    try:
        if not isinstance(refs, list) or len(refs) > MAX_START_OBSERVATIONS:
            raise ValueError("invalid references")
        for ref in refs:
            if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref):
                raise ValueError("invalid reference")
            value = bounded_json(objective_root(project, objective) / "native-start" / (ref + ".json"),
                                 limit=START_OBSERVATION_LIMIT)
            if digest(value) != ref or value.get("admission_id") != admission["admission_id"]:
                raise ValueError("changed evidence")
    except (OSError, ValueError, PodError) as exc:
        raise PodError("native_evidence_unavailable", "Native start evidence is missing or corrupt") from exc
    return refs


def _observe_start(project: Path, objective: str, *, owner: str, admission_id: str,
                   observation: dict, retry_request: str | None, reference: dict) -> dict:
    # The reservation already authorized this call. Recording its returned facts must
    # survive native contact loss. This cannot change state, routes or effect authority;
    # classification and every subsequent native call still require current authority.
    journal = _path(project, objective)
    with _lock(journal):
        state = _read(journal)
        row = state["admissions"].get(admission_id)
        if state["owner"] != owner or row is None or row["owner"] != owner:
            raise PodError("unknown_admission", "No owned reservation for this native observation")
        refs = _start_observations(project, objective, row)
        value = {"admission_id": admission_id, "run": row["run_id"], "task": row["task_id"],
                 "runtime": row["runtime"], "retry_request": retry_request, "observation": observation}
        ref = digest(value)
        if ref in refs:
            return row
        if len(refs) >= MAX_START_OBSERVATIONS:
            raise PodError("native_evidence_full", "Native start evidence limit reached")
        path = objective_root(project, objective) / "native-start" / (ref + ".json")
        row["recovery"] = {**row.get("recovery", {}), "start_observations": [*refs, ref]}
        if reference.get("runtime") == row["runtime"]:
            request, valid = _refusal_request_reference(reference)
            if (not valid or reference.get("_request_conflict") or _known_request_conflict(row)
                    or (row["request_uuid"] is not None and request not in (None, row["request_uuid"]))):
                row["recovery"]["request_conflict"] = "unresolved"
            elif request is not None:
                row["request_uuid"] = request
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            if path.exists() or path.is_symlink():
                if bounded_json(path, limit=START_OBSERVATION_LIMIT) != value:
                    raise PodError("native_evidence_unavailable", "Native start evidence changed")
            else:
                atomic_json(path, value, limit=START_OBSERVATION_LIMIT)
        finally:
            # Even if the separate archive cannot be written, keep the observed UUID
            # and missing-evidence reference whenever the compact journal remains writable.
            _write(journal, state)
        return row


def _start_observed(project: Path, objective: str, *, admission_id: str,
                    port: NativePort, **kwargs) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    if len(_start_observations(project, objective, admission)) >= MAX_START_OBSERVATIONS:
        raise PodError("native_evidence_full", "Native start evidence limit reached before execution")
    owner, retry = kwargs["owner"], kwargs.get("retry_request")
    try:
        receipt = port.start_worker(**kwargs)
    except Exception as exc:
        observation = getattr(exc, "native_observation", {"transport": type(exc).__name__,
                                                         "response": "unavailable"})
        reference = getattr(exc, "native_reference", {})
        recorded = _observe_start(project, objective, owner=owner, admission_id=admission_id,
                                  observation=observation, retry_request=retry, reference=reference)
        _hold(project, objective, owner=owner, admission_id=admission_id,
              request_uuid=recorded["request_uuid"],
              code=exc.code if isinstance(exc, PodError) else type(exc).__name__,
              detail=str(exc), request_conflict=_known_request_conflict(recorded))
        raise
    observation = receipt.pop("native_observation", None)
    _observe_start(project, objective, owner=owner, admission_id=admission_id,
                   observation=observation if observation is not None else {"receipt": receipt},
                   retry_request=retry, reference=receipt)
    return receipt


def _request_conflict_result(project: Path, objective: str, *, owner: str,
                             admission_id: str, receipt: dict,
                             request_uuid: str | None) -> dict | None:
    """Hold a carried request contradiction without choosing a fresh UUID."""
    observed_request, request_valid = _refusal_request_reference(receipt)
    if (not receipt.get("_request_conflict") and request_valid
            and (request_uuid is None or observed_request is None
                 or observed_request == request_uuid)):
        return None
    error = receipt.get("error")
    refused = isinstance(error, dict) and error.get("code") in PREFLIGHT_REFUSALS
    return _hold(
        project, objective, owner=owner, admission_id=admission_id,
        request_uuid=request_uuid,
        code=("native_refusal_unverified" if refused else "native_request_conflict"),
        detail="native receipt carries contradictory or malformed request identity",
        request_conflict=True)


def _known_request_conflict(admission: dict) -> bool:
    recovery = admission.get("recovery", {})
    return (recovery.get("request_conflict") == "unresolved"
            or (admission.get("error") or {}).get("code") == "native_request_conflict"
            or recovery.get("preflight_refusal") == "unverified")


def _record_request(project: Path, objective: str, *, owner: str, admission_id: str,
                    request_uuid: str, receipt: dict) -> dict:
    """Persist Orca's UUID before any follow-up read can fail."""
    def apply(row: dict) -> None:
        row["request_uuid"] = request_uuid
        row["recovery"] = {**row.get("recovery", {}), "native_start_state": receipt.get("state"),
                           "failed_stage": receipt.get("failedStage"),
                           "residual_resources": receipt.get("residualResources")}
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


_EFFECT_IDENTITIES = ("dispatchId", "workerId")
_EFFECT_COLLECTIONS = ("residualResources", "effects")
_EFFECT_REFERENCES = ("terminal", "terminalHandle", "worktreeId", "terminalResourceId",
                      "resource", "failedStage")
# Fields Orca's refused-start guide documents as diagnostic detail, never as an effect.
_REFUSAL_DETAIL_FIELDS = frozenset({"taskId", "runId", "status", "unmetDependencies", "retryOf",
                                    "terminal", "reason", "nextSteps"})


def _possible_effect_evidence(row: object) -> bool:
    if not isinstance(row, dict):
        return True
    for name in _EFFECT_IDENTITIES:
        if row.get(name) is not None:
            return True
    for name in _EFFECT_COLLECTIONS:
        if name in row and (not isinstance(row[name], list) or row[name]):
            return True
    return any(name in row and row[name] is not None for name in _EFFECT_REFERENCES)


def _refusal_request_reference(receipt: dict) -> tuple[str | None, bool]:
    values = []
    if receipt.get("request_uuid") is not None:
        values.append(receipt["request_uuid"])
    if "mutation" in receipt:
        mutation = receipt["mutation"]
        if not isinstance(mutation, dict) or mutation.get("requestId") is None:
            return None, False
        values.append(mutation["requestId"])
    error = receipt.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    if isinstance(data, dict) and data.get("orchestrationRequestId") is not None:
        values.append(data["orchestrationRequestId"])
    if not values:
        return None, True
    try:
        validated = [_valid_request_uuid(value) for value in values]
    except PodError:
        return None, False
    if any(value != validated[0] for value in validated[1:]):
        return None, False
    return validated[0], True


def _preflight_refusal_classification(receipt: dict, admission: dict,
                                      *, request_uuid: str | None = None) -> str | None:
    """Classify one documented effect-free refusal without lifecycle inference.

    Orca's guide names `task_not_found`, `task_not_startable` and `inject_rejected` as
    preflight refusals that start nothing. Everything about the receipt must agree with
    that before Pod records a durable no-start decision.
    """
    error = receipt.get("error") if isinstance(receipt, dict) else None
    if not isinstance(error, dict) or error.get("code") not in PREFLIGHT_REFUSALS:
        return None
    if receipt.get("runtime") != admission.get("runtime"):
        return "unverified"
    if ("_result_error" in receipt or receipt.get("_envelope_conflicts")
            or receipt.get("_request_conflict")):
        return "unverified"
    if "exit" in receipt and (type(receipt["exit"]) is not int or receipt["exit"] == 0):
        return "unverified"
    observed_request, request_valid = _refusal_request_reference(receipt)
    if not request_valid or (request_uuid is not None and observed_request is not None
                             and observed_request != request_uuid):
        return "unverified"
    data = error.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return "unverified"
    detail_free = {key: value for key, value in data.items() if key not in _REFUSAL_DETAIL_FIELDS}
    if _possible_effect_evidence(receipt) or _possible_effect_evidence(detail_free):
        return "unverified"
    for name in ("runtime", "runtimeId"):
        if name in data and data[name] != admission["runtime"]:
            return "unverified"
    return "authoritative"


def _refusal_result(project: Path, objective: str, *, owner: str,
                    admission_id: str, admission: dict, receipt: dict,
                    request_uuid: str | None = None) -> dict | None:
    """Settle a decoded native refusal: defer, hold for readback, or hold as unverified."""
    error = receipt.get("error") if isinstance(receipt, dict) else None
    if not isinstance(error, dict):
        return None
    if receipt.get("runtime") != admission["runtime"]:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code="native_refusal_unverified",
                     detail="Native refusal runtime differs from admission", request_conflict=True)
    available_request = request_uuid
    if available_request is None:
        observed_request, request_valid = _refusal_request_reference(receipt)
        available_request = observed_request if request_valid else None
    if error.get("code") == "runtime_error":
        # Orca's catch-all is not proof that nothing started. Native readback of the
        # request, Dispatch and worker is the only path out of this hold.
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=available_request, code="native_runtime_error",
                     detail=_error_summary(error))
    classification = _preflight_refusal_classification(
        receipt, admission, request_uuid=request_uuid)
    if classification is None:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=available_request, code="native_effect_uncertain", detail=_error_summary(error))
    if classification == "authoritative":
        return _defer_refusal(project, objective, owner=owner, admission_id=admission_id,
                              receipt=receipt, request_uuid=available_request)
    return _hold(project, objective, owner=owner, admission_id=admission_id,
                 request_uuid=available_request, code="native_refusal_unverified",
                 detail=f"{error.get('code')} did not prove an admission-bound no-start result")


def _error_summary(error: dict) -> dict:
    """Small current diagnostic; complete native bytes belong to immutable evidence."""
    code, message = error.get("code"), error.get("message")
    return {"code": code if isinstance(code, str) and len(code) <= 256 else None,
            "message": message[:2048] if isinstance(message, str) else None}


def _defer_refusal(project: Path, objective: str, *, owner: str, admission_id: str,
                   receipt: dict, request_uuid: str | None) -> dict:
    error = receipt["error"]
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    next_step = data.get("nextSteps")
    if not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 2048:
        next_step = "explicit new policy admission after correcting the refused preflight condition"

    def apply(row: dict) -> None:
        recovery = dict(row.get("recovery", {}))
        recovery.pop("request_conflict", None)
        row["state"] = "deferred"
        row["request_uuid"] = request_uuid
        row["native_binding"] = None
        row["error"] = {"code": error.get("code"), "detail": _error_summary(error)}
        row["recovery"] = {**recovery,
                           "preflight_refusal": "authoritative",
                           "native_start_state": "refused",
                           "next": next_step}
    return update_admission(project, objective, owner=owner,
                            admission_id=admission_id, update=apply)


def _bound_assignments(state: dict | None, *, include_closed: bool = False) -> tuple[dict, ...]:
    if not isinstance(state, dict):
        return ()
    return tuple(row for row in state.get("admissions", {}).values()
                 if isinstance(row, dict) and row.get("state") in (("bound", "closed") if include_closed else ("bound",))
                 and binding_valid(row.get("native_binding")))


def _current_authority(project: Path, objective: str, admission: dict, *, issue_port=None) -> None:
    """Pending replay retains its original route; check only continuing authority/core."""
    state = read(project, objective)
    checkpoint_value = state.get("checkpoint") if isinstance(state, dict) else None
    expected = admission.get("recovery", {}).get("checkpoint_binding")
    keys = ("candidate", "criteria", "plan_revision", "policy_revision", "objective_source", "worktree")
    if not isinstance(expected, dict) or not isinstance(checkpoint_value, dict):
        raise PodError("checkpoint_binding_changed", "Checkpoint binding is unavailable")
    from .config import effective
    if effective(project)["revision"] != checkpoint_value.get("policy_revision"):
        raise PodError("policy_revision_mismatch", "Governor policy changed before replay")
    if any(expected.get(key) != checkpoint_value.get(key) for key in keys):
        raise PodError("checkpoint_binding_changed", "Checkpoint semantics changed before replay")
    _check_objective_source(project, expected.get("objective_source"), issue_port=issue_port)
    _check_worktree_binding(project, expected.get("worktree"))


def _adopt_unique(project: Path, objective: str, *, owner: str, admission_id: str,
                  port: NativePort, request_uuid: str | None) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    try:
        rows = port.find_worker(run=admission["run_id"], task=admission["task_id"])
    except PodError as exc:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code="native_readback_unavailable",
                     detail=f"Exact Run/Task readback failed: {exc.code}")
    candidates = []
    errors = []
    for row in rows:
        dispatch = row.get("dispatchId")
        if not isinstance(dispatch, str):
            continue
        try:
            _binding_from_show(port.show_worker(dispatch), admission, dispatch)
            candidates.append(dispatch)
        except PodError as exc:
            errors.append(exc.code)
    if len(candidates) != 1:
        reason = "native_attempt_absent" if not candidates else "native_attempt_ambiguous"
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code=reason,
                     detail={"matching": len(candidates), "rejected": errors})
    receipt = {"runtime": admission["runtime"], "runId": admission["run_id"],
               "taskId": admission["task_id"], "dispatchId": candidates[0]}
    return _bind(project, objective, owner=owner, admission_id=admission_id,
                 receipt=receipt, port=port, request_uuid=request_uuid)


def recover_admission(project: Path, objective: str, *, owner: str, admission_id: str,
                      worktree: str, port: NativePort | None = None, issue_port=None) -> dict:
    """Recover the same admission; pending replay never rechecks model preferences."""
    native_port = port or OrcaPort(project)
    state = read(project, objective)
    admission = state["admissions"].get(admission_id) if state else None
    if not isinstance(admission, dict) or admission.get("owner") != owner:
        raise PodError("unknown_admission", "No owned admission identity")
    if worktree != admission["worktree"]:
        raise PodError("admission_conflict", "Recovery changed original worktree")
    _start_observations(project, objective, admission)
    if admission["state"] in ("bound", "closed", "deferred"):
        action = "defer" if admission["state"] == "deferred" else "reuse"
        return {"status": admission["state"], "admission": admission, "action": action}
    request_uuid = admission.get("request_uuid")
    if request_uuid is None:
        if _known_request_conflict(admission):
            return {"status": admission["state"], "admission": admission, "action": "hold"}
        authority = native_port.read_native(owner, authority_runs=(admission["run_id"],))
        if (authority.get("authoritative") is not True or authority.get("owner") != owner
                or authority.get("runtime") != admission["runtime"]):
            raise PodError("native_authority_unverified", "No-UUID readback lost Run ownership")
        row = _adopt_unique(project, objective, owner=owner, admission_id=admission_id,
                            port=native_port, request_uuid=None)
        return {"status": row["state"], "admission": row, "action": "inspect_without_uuid"}
    try:
        request_uuid = _valid_request_uuid(request_uuid)
    except PodError as exc:
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=request_uuid, code=exc.code, detail="Invalid stored UUID")
        return {"status": row["state"], "admission": row, "action": "hold"}
    authority = native_port.read_native(owner, authority_runs=(admission["run_id"],))
    if (authority.get("authoritative") is not True or authority.get("owner") != owner
            or authority.get("runtime") != admission["runtime"]):
        raise PodError("native_authority_unverified", "Recovery caller/runtime authority is unproven")
    shown = native_port.request_show(request_uuid)
    if shown.get("runtime") != admission["runtime"]:
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=request_uuid, code="orca_runtime_changed", detail="request-show")
        return {"status": row["state"], "admission": row, "action": "hold"}
    status, receipt = _receipt_from_request_show(shown, request_uuid)
    if status == "completed":
        if receipt is None:
            row = _hold(project, objective, owner=owner, admission_id=admission_id,
                        request_uuid=request_uuid, code="native_receipt_missing", detail="completed request")
        else:
            completed = {"runtime": admission["runtime"], **receipt}
            row = _request_conflict_result(project, objective, owner=owner,
                                           admission_id=admission_id, receipt=completed,
                                           request_uuid=request_uuid)
            if row is None:
                row = _refusal_result(project, objective, owner=owner, admission_id=admission_id,
                                      admission=admission, receipt=completed, request_uuid=request_uuid)
            if row is None:
                row = _bind(project, objective, owner=owner, admission_id=admission_id,
                            receipt=completed, port=native_port, request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "action": "recorded_receipt"}
    if status == "pending":
        if _known_request_conflict(admission):
            return {"status": admission["state"], "admission": admission, "action": "hold"}
        _current_authority(project, objective, admission, issue_port=issue_port)
        capability = native_port.capability()
        if capability.get("runtime") != admission["runtime"]:
            raise PodError("orca_runtime_changed", "Pending replay capability changed runtime")
        placement = _check_native_placement(native_port, worktree,
                                            admission.get("recovery", {}).get("placement_binding"))
        if placement is not None and placement["runtime"] != admission["runtime"]:
            raise PodError("orca_runtime_changed", "Pending placement belongs to another runtime")
        pre_effect = native_port.read_native(owner, authority_runs=(admission["run_id"],))
        if (pre_effect.get("authoritative") is not True or pre_effect.get("owner") != owner
                or pre_effect.get("runtime") != admission["runtime"]):
            raise PodError("native_authority_unverified", "Pending replay lost Run ownership")
        terminal = None
        if admission["reuse_of"]:
            previous = state["admissions"].get(admission["reuse_of"])
            terminal = previous["native_binding"]["terminalHandle"] if previous else None
            if not isinstance(terminal, str) or not terminal:
                raise PodError("reuse_unavailable", "Pending terminal reuse lost its binding")
        receipt = _start_observed(project, objective, admission_id=admission_id, port=native_port,
                                           run=admission["run_id"], task=admission["task_id"],
                                           owner=owner, route=admission["request"], worktree=worktree,
                                           retry_request=request_uuid, terminal=terminal)
        row = _request_conflict_result(project, objective, owner=owner, admission_id=admission_id,
                                       receipt=receipt, request_uuid=request_uuid)
        if row is None:
            row = _refusal_result(project, objective, owner=owner, admission_id=admission_id,
                                  admission=admission, receipt=receipt, request_uuid=request_uuid)
        if row is None:
            returned = receipt.get("request_uuid")
            if returned is not None and returned != request_uuid:
                row = _hold(project, objective, owner=owner, admission_id=admission_id,
                            request_uuid=request_uuid, code="native_request_mismatch", detail=returned)
            else:
                row = _bind(project, objective, owner=owner, admission_id=admission_id,
                            receipt=receipt, port=native_port, request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "action": "joined_pending_request"}
    if _known_request_conflict(admission):
        return {"status": admission["state"], "admission": admission, "action": "hold"}
    row = _adopt_unique(project, objective, owner=owner, admission_id=admission_id,
                        port=native_port, request_uuid=request_uuid)
    return {"status": row["state"], "admission": row, "action": "inspect_after_absent"}


def guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                  plan_revision: str, frozen_packet: dict, worktree: str = "current",
                  reuse_of: str | None = None, port: NativePort | None = None,
                  issue_port=None, accompanying: dict | None = None) -> dict:
    """Packet, recovery, issue, placement, serialized boundary, then one native start."""
    from .records import packet
    bounded_text(owner, name="owner")
    native_port = port or OrcaPort(project)
    validated = packet(frozen_packet["body"] if "body" in frozen_packet else frozen_packet)
    if "packet_id" in frozen_packet and frozen_packet["packet_id"] != validated["packet_id"]:
        raise PodError("invalid_packet", "Frozen packet identity changed")
    body = validated["body"]
    if body["objective"] != objective:
        raise PodError("packet_mismatch", "Packet objective differs from admission")
    admission_id = admission_identity(objective=objective, run_id=run, task_id=task)
    existing_state = read(project, objective)
    from .ledger import _run_references
    if not isinstance(existing_state, dict) or existing_state.get("owner") != owner:
        raise PodError("native_authority_unverified", "Objective has no owned checkpoint")
    if run not in _run_references(existing_state):
        raise PodError("native_authority_unverified", "Admission Run is not an exact objective reference")
    if isinstance(existing_state, dict) and admission_id in existing_state["admissions"]:
        existing = existing_state["admissions"][admission_id]
        if (existing["packet_id"] != validated["packet_id"]
                or existing["plan_revision"] != plan_revision or existing["worktree"] != worktree):
            code = "unresolved_prior_attempt" if existing["state"] in ("reserved", "unresolved") else "admission_conflict"
            raise PodError(code, "A Task's admission cannot be replaced by another packet or placement")
        recovered = recover_admission(project, objective, owner=owner, admission_id=admission_id,
                                      worktree=worktree, port=native_port, issue_port=issue_port)
        return {**recovered, "decision": recovered["admission"]["route_decision"]}
    _check_objective_source(project, body.get("objective_source"), issue_port=issue_port)
    _check_worktree_binding(project, body.get("worktree"))
    from .github import repository_context
    current_worktree = repository_context(project)
    default_binding = {"repository": current_worktree["repository"],
                       "repo_key": current_worktree["repo_key"],
                       "path": current_worktree["worktree"],
                       "branch": current_worktree["branch"]}
    placement_binding = body.get("placement", body.get("worktree", default_binding))
    placement = _check_native_placement(native_port, worktree, placement_binding)
    capability = native_port.capability()
    if placement is not None and placement["runtime"] != capability.get("runtime"):
        raise PodError("orca_runtime_changed", "Placement and launch capability belong to different runtimes")
    proposed = {key: value for key, value in body["route"].items() if key != "preference_revision"}
    admission = reserve(project, objective, owner=owner, admission_id=admission_id,
                        requested=proposed,
                        native_reader=lambda state: native_port.read_native(
                            owner, authority_runs=(run,),
                            assignments=_bound_assignments(state, include_closed=bool(reuse_of))),
                        run_id=run, task_id=task, plan_revision=plan_revision,
                        packet_id=validated["packet_id"], worktree=worktree,
                        frozen_packet=validated, expected_runtime=capability["runtime"],
                        placement_binding=placement_binding, reuse_of=reuse_of,
                        accompanying=accompanying)
    if admission["existing"]:
        recovered = recover_admission(project, objective, owner=owner, admission_id=admission_id,
                                      worktree=worktree, port=native_port, issue_port=issue_port)
        return {**recovered, "decision": recovered["admission"]["route_decision"]}
    terminal = None
    if reuse_of:
        prior = read(project, objective)["admissions"][reuse_of]
        terminal = prior["native_binding"]["terminalHandle"]
    try:
        receipt = _start_observed(project, objective, admission_id=admission_id, port=native_port,
                                           run=run, task=task, owner=owner, route=proposed,
                                           worktree=worktree, terminal=terminal)
        row = _request_conflict_result(project, objective, owner=owner, admission_id=admission_id,
                                       receipt=receipt, request_uuid=None)
        if row is None:
            row = _refusal_result(project, objective, owner=owner, admission_id=admission_id,
                                  admission=admission, receipt=receipt)
        if row is not None:
            return {"status": row["state"], "admission": row, "decision": row["route_decision"]}
        request_uuid = _valid_request_uuid(receipt.get("request_uuid"))
        _record_request(project, objective, owner=owner, admission_id=admission_id,
                        request_uuid=request_uuid, receipt=receipt)
        row = _bind(project, objective, owner=owner, admission_id=admission_id,
                    receipt=receipt, port=native_port, request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "decision": row["route_decision"]}
    except Exception as exc:
        code = exc.code if isinstance(exc, PodError) else type(exc).__name__
        current = read(project, objective)["admissions"][admission_id]
        request_uuid = current.get("request_uuid")
        _hold(project, objective, owner=owner, admission_id=admission_id,
              request_uuid=request_uuid, code=code, detail=str(exc))
        raise

"""Serialized policy admission and exact Orca request recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .config import DEFAULT_WORKER_CAPACITY, effective
from .errors import PodError
from .ledger import (admission_identity, binding_valid, read, reserve,
                     update_admission, _native_assignment_settled,
                     _spending_grant_binding)
from .orca import (PREFLIGHT_REFUSALS, account_evidence_stops, account_metadata_raw,
                   agent_login_mode, bucket_for, contract, current_run, hosts,
                   mutate_command, read_command,
                   require_route_establishment, route_establishment,
                   selected_account_evidence, worker_rows, worker_show, worktree_identity,
                   worktree_selector)
from .routing import preview
from .util import bounded_text


STARTED_STATES = ("ready", "running", "succeeded", "failed", "stopped")


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
    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool) -> dict: ...
    def read_native(self, owner: str, *, route: dict | None = None,
                    establishment: dict | None = None,
                    authority_runs: tuple[str, ...] = (),
                    assignments: tuple[dict, ...] = ()) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str, retry_request: str | None = None) -> dict: ...
    def request_show(self, request_uuid: str) -> dict: ...
    def find_worker(self, *, run: str, task: str) -> list[dict]: ...
    def show_worker(self, dispatch: str) -> dict: ...
    def resolve_worktree(self, selector: str) -> dict: ...


class OrcaPort:
    """Thin installed-Orca port: bounded reads plus worker-start/replay only."""

    def __init__(self, project: Path | None = None):
        self.project = project

    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool = False) -> dict:
        return route_establishment(route, model_policy, snapshot=contract(),
                                   accounts=account_metadata_raw(),
                                   login=agent_login_mode(route["agent"]), fleet=hosts(),
                                   child_delegation=child_delegation)

    def resolve_worktree(self, selector: str) -> dict:
        from .github import repository_context
        resolved = worktree_identity(selector)
        context = repository_context(Path(resolved["path"]))
        return {**resolved, "repository": context["repository"],
                "repo_key": context["repo_key"]}

    def read_native(self, owner: str, *, route: dict | None = None,
                    establishment: dict | None = None,
                    authority_runs: tuple[str, ...] = (),
                    assignments: tuple[dict, ...] = ()) -> dict:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        if (not isinstance(authority_runs, tuple)
                or any(not isinstance(run_id, str) or not run_id for run_id in authority_runs)
                or len(set(authority_runs)) != len(authority_runs)):
            raise PodError("native_authority_unverified", "Native authority Run set is malformed")
        expected_runs = tuple(sorted(authority_runs))
        if (not isinstance(assignments, tuple)
                or any(not isinstance(row, dict) for row in assignments)):
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
        if expected_runs and (binding_before["runtime"] != runtime
                              or binding_after["runtime"] != runtime):
            raise PodError("orca_runtime_changed", "Current Run binding changed Orca runtime")
        current = binding_before["run"] if binding_before is not None else None
        stable_current = bool(current is not None and current == binding_after["run"])
        current_id = current.get("id") if isinstance(current, dict) else None
        authoritative = bool(
            expected_runs and handle and handle == owner and stable_current
            and current_id in expected_runs
            and current.get("coordinator_handle") == owner)
        if (route is None) != (establishment is None):
            raise PodError("account_binding_unverified",
                           "Fresh account evidence needs its established route")
        quota = (_quota_snapshot(route, establishment, runtime=runtime)
                 if route is not None else None)
        return {"runtime": runtime, "owner": owner if authoritative else None,
                "authoritative": authoritative,
                "scope": "objective_assignments", "complete": True,
                "assignments": evidence, "quota": quota,
                "physical_capacity": "unavailable"}

    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str = "current", retry_request: str | None = None) -> dict:
        selector = worktree_selector(worktree)
        if selector is None:
            raise PodError("invalid_worktree_selector",
                           "Worker placement must name an existing worktree")
        argv = ["orchestration", "worker-start", "--task", task, "--run", run,
                "--worktree", selector, "--agent", route["agent"], "--model", route["model"],
                "--effort", route["effort"]]
        if retry_request is not None:
            _valid_request_uuid(retry_request)
            argv += ["--retry-request", retry_request]
        argv += ["--json"]
        receipt = mutate_command(argv, accept_exit=(0, 1))
        result = receipt["result"]
        return {"runtime": receipt["runtime"], "exit": receipt["exit"],
                "request_uuid": receipt.get("request_uuid"), **result}

    def request_show(self, request_uuid: str) -> dict:
        _valid_request_uuid(request_uuid)
        return read_command(["orchestration", "request-show", "--request", request_uuid, "--json"])

    def find_worker(self, *, run: str, task: str) -> list[dict]:
        fleet = worker_rows(run)
        return [worker for worker in fleet["workers"]
                if (worker.get("taskId") == task
                    or isinstance(worker.get("projection"), dict)
                    and worker["projection"].get("taskId") == task)]

    def show_worker(self, dispatch: str) -> dict:
        return worker_show(dispatch)


def _quota_snapshot(route: dict, establishment: dict, *, runtime: str) -> dict | None:
    metadata = account_metadata_raw()
    if metadata.get("runtime") != runtime or establishment.get("runtime") != runtime:
        raise PodError("orca_runtime_changed", "Account evidence belongs to another Orca runtime")
    provider = metadata["providers"].get(route.get("agent"))
    if not isinstance(provider, dict):
        raise PodError("account_binding_unverified", "Selected native account is unavailable")
    evidence = selected_account_evidence(provider, agent_login_mode(route["agent"]))
    approved_billing = establishment.get("billing", {}).get("approved")
    stops = account_evidence_stops(evidence, expected_identity=route.get("account"),
                                   approved_billing=approved_billing)
    if stops:
        code = stops[0]
        raise PodError(code, "Fresh selected-account evidence no longer establishes the route")
    if not provider.get("windows"):
        return None
    bucket = route.get("bucket") or bucket_for(route.get("agent"), route.get("model", ""))
    windows, unknowns = [], []
    for name, window in provider["windows"].items():
        if name == "fableWeekly" and bucket != "fable" or name == "weekly" and bucket == "fable":
            continue
        used = window.get("usedPercent")
        if type(used) not in (int, float):
            unknowns.append(name)
            continue
        windows.append({"name": name, "remaining_percent": max(0.0, 100.0 - float(used)),
                        "reset_at": window.get("resetsAt")})
    updated = provider.get("updated_at_ms")
    if not windows or type(updated) not in (int, float):
        return None
    return {"schema": "pod-quota/v1", "provider": route["agent"],
            "account": evidence["identity_digest"],
            "bucket": bucket, "windows": windows,
            "observed_at": datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat(),
            "source": "supported_metadata", "confidence": "observed",
            "unknowns": sorted(unknowns)}


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
    if not isinstance(native_dispatch, dict) or not isinstance(projection, dict) or not isinstance(worker, dict):
        raise PodError("native_identity_unverified", "Worker readback is incomplete")
    if (native_dispatch.get("id") != dispatch
            or native_dispatch.get("runId") != admission["run_id"]
            or native_dispatch.get("taskId") != admission["task_id"]
            or projection.get("dispatchId") != dispatch
            or projection.get("runId") != admission["run_id"]
            or projection.get("taskId") != admission["task_id"]
            or worker.get("dispatchId") != dispatch):
        raise PodError("native_identity_unverified", "Worker readback does not join the admission")
    launch = worker.get("startOptions", {}).get("launch") if isinstance(worker.get("startOptions"), dict) else None
    # Orca 1.4.209 exposes only model and effort on the native launch wire.
    # Context proof remains in the separate Pod establishment/admission evidence.
    requested = {key: admission["request"][key] for key in ("agent", "model", "effort")}
    if not isinstance(launch, dict) or launch.get("requested") != requested or launch.get("effective") != requested:
        raise PodError("effective_launch_unverified", "Worker effective route differs from admission")
    if worker.get("state") not in STARTED_STATES:
        raise PodError("native_start_unsettled", "Worker has not proved that start occurred")
    binding = {"runId": admission["run_id"], "taskId": admission["task_id"],
               "dispatchId": dispatch, "workerId": projection.get("id"),
               "worktreeId": worker.get("worktreeId"),
               "terminalHandle": worker.get("agentTerminalHandle")}
    if not binding_valid(binding):
        raise PodError("native_identity_unverified", "Worker identity is incomplete")
    return binding


def _bind(project: Path, objective: str, *, owner: str, admission_id: str,
          receipt: dict, port: NativePort, request_uuid: str | None) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    if receipt.get("runtime") not in (None, admission["runtime"]):
        raise PodError("native_identity_unverified", "Start receipt changed runtime")
    run = receipt.get("runId")
    task = receipt.get("taskId")
    dispatch = receipt.get("dispatchId")
    if run != admission["run_id"] or task != admission["task_id"] or not isinstance(dispatch, str):
        raise PodError("native_identity_unverified", "Start receipt identity is incomplete")
    shown = port.show_worker(dispatch)
    binding = _binding_from_show(shown, admission, dispatch)
    def apply(row: dict) -> None:
        recovery = dict(row.get("recovery", {}))
        recovery.pop("preflight_refusal", None)
        recovery.pop("request_conflict", None)
        recovery["receipt"] = "recorded"
        row["state"] = "bound"
        row["native_binding"] = binding
        row["request_uuid"] = request_uuid
        row["error"] = None
        row["recovery"] = recovery
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
    available_request = request_uuid
    if available_request is None:
        observed_request, request_valid = _refusal_request_reference(receipt)
        available_request = observed_request if request_valid else None
    if error.get("code") == "runtime_error":
        # Orca's catch-all is not proof that nothing started. Native readback of the
        # request, Dispatch and worker is the only path out of this hold.
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=available_request, code="native_runtime_error",
                     detail=error)
    classification = _preflight_refusal_classification(
        receipt, admission, request_uuid=request_uuid)
    if classification is None:
        return None
    if classification == "authoritative":
        return _defer_refusal(project, objective, owner=owner, admission_id=admission_id,
                              receipt=receipt, request_uuid=available_request)
    return _hold(project, objective, owner=owner, admission_id=admission_id,
                 request_uuid=available_request, code="native_refusal_unverified",
                 detail=f"{error.get('code')} did not prove an admission-bound no-start result")


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
        row["error"] = {"code": error.get("code"), "detail": error}
        row["recovery"] = {**recovery,
                           "preflight_refusal": "authoritative",
                           "native_start_state": "refused",
                           "next": next_step}
    return update_admission(project, objective, owner=owner,
                            admission_id=admission_id, update=apply)


def _bound_assignments(state: dict | None) -> tuple[dict, ...]:
    if not isinstance(state, dict):
        return ()
    return tuple(row for row in state.get("admissions", {}).values()
                 if isinstance(row, dict) and row.get("state") == "bound"
                 and binding_valid(row.get("native_binding")))


def _current_authority(project: Path, objective: str, admission: dict,
                       *, task_policy: dict | None, issue_port=None) -> tuple[dict, dict]:
    """A pending replay is still a mutation, so current revocation/spending policy applies."""
    policy = effective(project, task=task_policy)
    if policy["revision"] != admission["route_decision"].get("policy_revision"):
        raise PodError("policy_revision_mismatch", "Policy changed before native request replay")
    state = read(project, objective)
    checkpoint_value = state.get("checkpoint") if isinstance(state, dict) else None
    expected_checkpoint = admission.get("recovery", {}).get("checkpoint_binding")
    core_checkpoint = {"candidate", "criteria", "plan_revision", "policy_revision"}
    optional_checkpoint = {"objective_source", "worktree"}
    allowed_checkpoint = core_checkpoint | optional_checkpoint
    binding_valid_shape = (isinstance(expected_checkpoint, dict)
                           and core_checkpoint <= set(expected_checkpoint) <= allowed_checkpoint)
    expected_normalized = ({key: expected_checkpoint.get(key) for key in allowed_checkpoint}
                           if binding_valid_shape else None)
    current_normalized = ({key: checkpoint_value.get(key) for key in allowed_checkpoint}
                          if isinstance(checkpoint_value, dict) else None)
    if expected_normalized is None or current_normalized != expected_normalized:
        raise PodError("checkpoint_binding_changed",
                       "Checkpoint semantics changed before native request replay")
    _check_objective_source(project, expected_normalized.get("objective_source"),
                            issue_port=issue_port)
    _check_worktree_binding(project, expected_normalized.get("worktree"))
    route = admission["request"]
    model = policy["policy"]["models"].get(route.get("alias"))
    if (not isinstance(model, dict) or not model.get("approved")
            or model.get("agent") != route.get("agent")
            or model.get("model") != route.get("model")
            or model.get("account") != route.get("account")):
        raise PodError("route_revoked", "Admission route is no longer approved")
    if model.get("billing", "unknown") != "included":
        binding = admission.get("recovery", {}).get("spending_grant")
        _spending_grant_binding(policy["policy"]["policy"].get("spending_grants", []),
                                binding, requested=route,
                                objective=admission["objective"],
                                now=datetime.now(timezone.utc))
    return model, policy["policy"]["policy"]


def _adopt_unique(project: Path, objective: str, *, owner: str, admission_id: str,
                  port: NativePort, request_uuid: str | None) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    rows = port.find_worker(run=admission["run_id"], task=admission["task_id"])
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
                      worktree: str, port: NativePort | None = None,
                      task_policy: dict | None = None, issue_port=None) -> dict:
    """Recover the same admission; never issue a fresh semantic start."""
    native_port = port or OrcaPort(project)
    state = read(project, objective)
    admission = state["admissions"].get(admission_id) if state else None
    if not isinstance(admission, dict) or admission.get("owner") != owner:
        raise PodError("unknown_admission", "No owned admission identity")
    if admission["state"] in ("bound", "closed", "deferred"):
        action = "defer" if admission["state"] == "deferred" else "reuse"
        return {"status": admission["state"], "admission": admission, "action": action}
    request_uuid = admission.get("request_uuid")
    if request_uuid is None:
        if _known_request_conflict(admission):
            return {"status": admission["state"], "admission": admission, "action": "hold"}
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=None, code="native_request_missing",
                    detail="no Orca-issued UUID can scope attempt discovery")
        return {"status": row["state"], "admission": row, "action": "hold"}
    try:
        request_uuid = _valid_request_uuid(request_uuid)
    except PodError as exc:
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=request_uuid, code=exc.code, detail="invalid stored UUID")
        return {"status": row["state"], "admission": row, "action": "hold"}
    if worktree != admission["worktree"]:
        raise PodError("admission_conflict", "Recovery changed the original worktree")
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
            completed_receipt = {"runtime": admission["runtime"], **receipt}
            row = _request_conflict_result(
                project, objective, owner=owner, admission_id=admission_id,
                receipt=completed_receipt, request_uuid=request_uuid)
            if row is None:
                row = _refusal_result(
                project, objective, owner=owner, admission_id=admission_id,
                admission=admission, receipt=completed_receipt,
                request_uuid=request_uuid)
            if row is None:
                row = _bind(project, objective, owner=owner, admission_id=admission_id,
                            receipt=completed_receipt, port=native_port,
                            request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "action": "recorded_receipt"}
    if status == "pending":
        if _known_request_conflict(admission):
            return {"status": admission["state"], "admission": admission, "action": "hold"}
        model, current_policy = _current_authority(
            project, objective, admission, task_policy=task_policy, issue_port=issue_port)
        placement = _check_native_placement(
            native_port, worktree, admission.get("recovery", {}).get("placement_binding"))
        if placement is not None and placement["runtime"] != admission["runtime"]:
            raise PodError("orca_runtime_changed", "Pending replay placement belongs to another runtime")
        fresh_establishment = native_port.establish(
            admission["request"], model,
            child_delegation=bool(current_policy.get("child_delegation")))
        require_route_establishment(fresh_establishment, admission["request"])
        pre_effect = native_port.read_native(
            owner, route=admission["request"], establishment=fresh_establishment,
            authority_runs=(admission["run_id"],))
        if (pre_effect.get("authoritative") is not True or pre_effect.get("owner") != owner
                or pre_effect.get("runtime") != admission["runtime"]):
            raise PodError("native_authority_unverified",
                           "Pending replay lost current Run ownership")
        receipt = native_port.start_worker(run=admission["run_id"], task=admission["task_id"],
                                           owner=owner, route=admission["request"],
                                           worktree=worktree, retry_request=request_uuid)
        row = _request_conflict_result(
            project, objective, owner=owner, admission_id=admission_id,
            receipt=receipt, request_uuid=request_uuid)
        if row is None:
            row = _refusal_result(
            project, objective, owner=owner, admission_id=admission_id,
            admission=admission, receipt=receipt, request_uuid=request_uuid)
        returned = receipt.get("request_uuid")
        if row is None:
            if returned is not None and returned != request_uuid:
                row = _hold(project, objective, owner=owner, admission_id=admission_id,
                            request_uuid=request_uuid, code="native_request_mismatch",
                            detail=returned)
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
                  assessment: dict, capabilities: dict, quotas: dict,
                  plan_revision: str, frozen_packet: dict, capacity: int = DEFAULT_WORKER_CAPACITY,
                  capacity_reason: str | None = None, exceptional_grant: dict | None = None,
                  worktree: str = "current", port: NativePort | None = None,
                  task_policy: dict | None = None, now: datetime | None = None,
                  issue_port=None) -> dict:
    """Policy check, durable reservation, then one native start or exact recovery."""
    from .records import packet
    from .ledger import check_bound_sources
    bounded_text(owner, name="owner")
    native_port = port or OrcaPort(project)
    validated = packet(frozen_packet["body"] if "body" in frozen_packet else frozen_packet)
    if "packet_id" in frozen_packet and frozen_packet["packet_id"] != validated["packet_id"]:
        raise PodError("invalid_packet", "Frozen packet identity changed")
    if validated["body"]["objective"] != objective:
        raise PodError("packet_mismatch", "Packet objective differs from admission")
    admission_id = admission_identity(objective=objective, run_id=run, task_id=task,
                                      packet_id=validated["packet_id"], plan_revision=plan_revision)
    existing_state = read(project, objective)
    if isinstance(existing_state, dict) and admission_id in existing_state["admissions"]:
        recovered = recover_admission(project, objective, owner=owner,
                                      admission_id=admission_id, worktree=worktree,
                                      port=native_port, task_policy=task_policy,
                                      issue_port=issue_port)
        return {**recovered,
                "decision": recovered["admission"]["route_decision"],
                "establishment": recovered["admission"]["effective_evidence"]}
    policy = effective(project, task=task_policy)
    _check_objective_source(project, validated["body"].get("objective_source"),
                            issue_port=issue_port)
    packet_worktree = validated["body"].get("worktree")
    _check_worktree_binding(project, packet_worktree)
    placement_binding = validated["body"].get("placement", packet_worktree)
    placement = _check_native_placement(native_port, worktree, placement_binding)
    decision = preview(assessment, policy, capabilities=capabilities, quotas=quotas,
                       objective=objective, now=now)
    if decision.get("status") != "usable":
        raise PodError("route_unusable", "Routing preview did not produce a usable route")
    route = decision["selected"]
    if validated["body"]["route"] != route:
        raise PodError("packet_mismatch", "Packet route differs from effective route")
    establishment = native_port.establish(route, policy["policy"]["models"][route["alias"]],
                                           child_delegation=bool(policy["policy"]["policy"].get("child_delegation")))
    if placement is not None and placement["runtime"] != establishment.get("runtime"):
        raise PodError("orca_runtime_changed", "Worker placement and route belong to different runtimes")
    check_bound_sources(project, objective, owner=owner, assignment=validated["packet_id"],
                        sources=validated["body"]["sources"])
    admission = reserve(project, objective, owner=owner, admission_id=admission_id,
                        requested=route, route_decision={**decision,
                                                        "policy": policy["policy"]["policy"],
                                                        "task_policy": task_policy},
                        establishment=establishment,
                        native_reader=lambda: native_port.read_native(
                            owner, route=route, establishment=establishment,
                            authority_runs=(run,),
                            assignments=_bound_assignments(existing_state)),
                        capacity=capacity, run_id=run, task_id=task,
                        plan_revision=plan_revision, packet_id=validated["packet_id"],
                        worktree=worktree, frozen_packet=validated,
                        exceptional_grant=exceptional_grant, capacity_reason=capacity_reason,
                        spending_grant=decision.get("spending_grant"), task_policy=task_policy,
                        now=now)
    if admission["existing"]:
        recovered = recover_admission(project, objective, owner=owner,
                                      admission_id=admission_id, worktree=worktree,
                                      port=native_port, task_policy=task_policy,
                                      issue_port=issue_port)
        return {**recovered, "decision": decision, "establishment": establishment}
    try:
        receipt = native_port.start_worker(run=run, task=task, owner=owner, route=route,
                                           worktree=worktree)
        row = _request_conflict_result(
            project, objective, owner=owner, admission_id=admission_id,
            receipt=receipt, request_uuid=None)
        if row is None:
            row = _refusal_result(
            project, objective, owner=owner, admission_id=admission_id,
            admission=admission, receipt=receipt)
        if row is not None:
            return {"status": row["state"], "admission": row, "decision": decision,
                    "establishment": establishment}
        request_uuid = _valid_request_uuid(receipt.get("request_uuid"))
        _record_request(project, objective, owner=owner, admission_id=admission_id,
                        request_uuid=request_uuid, receipt=receipt)
        row = _bind(project, objective, owner=owner, admission_id=admission_id,
                    receipt=receipt, port=native_port, request_uuid=request_uuid)
        return {"status": "bound", "admission": row, "decision": decision,
                "establishment": establishment}
    except Exception as exc:
        code = exc.code if isinstance(exc, PodError) else type(exc).__name__
        current = read(project, objective)["admissions"][admission_id]
        request_uuid = current.get("request_uuid")
        if isinstance(exc, PodError) and exc.code == "native_request_invalid":
            request_uuid = None
        _hold(project, objective, owner=owner, admission_id=admission_id,
              request_uuid=request_uuid, code=code, detail=str(exc))
        raise


"""One-shot guarded native admission. No autonomous queue or scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Protocol

from .config import DEFAULT_WORKER_CAPACITY, effective
from .errors import PodError
from .ledger import binding_version, reconcile, reserve
from .orca import contract, executable, worker_rows, worker_show
from .routing import preview
from .util import bounded_text


class NativePort(Protocol):
    def assurance(self, route: dict) -> dict: ...
    def read_native(self, owner: str) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict) -> dict: ...
    def show_worker(self, dispatch: str) -> dict: ...
    def release_worker(self, dispatch: str) -> dict: ...


class OrcaPort:
    """Installed Orca adapter. Unverified protections block before mutation."""

    def assurance(self, route: dict) -> dict:
        observed = contract()
        # No installed-version guarantee yet proves these for the account route.
        return {"runtime": None, "billing_preflight": False, "fanout_control": False,
                "account_binding": None,
                "reason": "installed runtime controls unverified", "version": observed.get("version")}

    def read_native(self, owner: str) -> dict:
        fleet = worker_rows()
        rows = []
        for worker in fleet["workers"]:
            state = "released" if worker.get("terminalState") == "released" else "occupied"
            rows.append({"state": state, "account": None, "objective": None,
                         "dispatchId": worker.get("dispatchId")})
        return {"runtime": fleet["runtime"], "owner": owner if os.environ.get("ORCA_TERMINAL_HANDLE") == owner else None,
                "authoritative": os.environ.get("ORCA_TERMINAL_HANDLE") == owner,
                "scope": fleet["scope"].get("source") if isinstance(fleet["scope"], dict) else None,
                "complete": fleet["complete"], "workers": rows,
                "cross_host": any(w.get("projection", {}).get("host") not in (None, "local") for w in fleet["workers"]),
                "atomic_admission": False}

    def start_worker(self, *, run: str, task: str, owner: str, route: dict) -> dict:
        argv = [str(executable()), "orchestration", "worker-start", "--task", task,
                "--run", run, "--from", owner, "--worktree", "current",
                "--agent", route["agent"], "--model", route["model"],
                "--effort", route["effort"], "--json"]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
            result = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise PodError("native_effect_uncertain", "Native start response is unavailable") from exc
        if completed.returncode or not result.get("ok") or not isinstance(result.get("result"), dict):
            raise PodError("native_effect_uncertain", "Native start failed or outcome is uncertain")
        return {"runtime": result.get("_meta", {}).get("runtimeId"), **result["result"]}

    def show_worker(self, dispatch: str) -> dict:
        return worker_show(dispatch)

    def release_worker(self, dispatch: str) -> dict:
        argv = [str(executable()), "orchestration", "worker-release",
                "--dispatch", dispatch, "--json"]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
            value = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise PodError("native_release_uncertain", "Native release response is unavailable") from exc
        if not value.get("ok") or not isinstance(value.get("result"), dict):
            raise PodError("native_release_uncertain", "Native release outcome is uncertain")
        result = {"runtime": value.get("_meta", {}).get("runtimeId"), **value["result"]}
        disposition = result.get("status", result.get("state"))
        if completed.returncode not in (0, 1) or (completed.returncode == 1
                                                  and disposition != "release_unknown"):
            raise PodError("native_release_uncertain", "Native release outcome is uncertain")
        return result


def _release_readback_matches(shown: dict, runtime: str, dispatch: str, binding: dict) -> bool:
    result = shown.get("result")
    if (not isinstance(result, dict) or shown.get("runtime") != runtime
            or binding_version(binding) != "current"):
        return False
    native_dispatch = result.get("dispatch")
    projection = result.get("projection")
    worker = result.get("worker")
    if not (isinstance(native_dispatch, dict) and isinstance(projection, dict)
            and isinstance(worker, dict)
            and native_dispatch.get("id") == dispatch
            and native_dispatch.get("runId") == binding["runId"]
            and native_dispatch.get("taskId") == binding["taskId"]
            and projection.get("id") == binding["workerId"]
            and projection.get("runId") == binding["runId"]
            and projection.get("taskId") == binding["taskId"]
            and projection.get("dispatchId") == dispatch
            and worker.get("dispatchId") == dispatch
            and worker.get("worktreeId") == binding["worktreeId"]
            and worker.get("agentTerminalHandle") == binding["terminalHandle"]):
        return False
    workspace = projection.get("workspace")
    if workspace is not None and (not isinstance(workspace, dict)
                                  or workspace.get("id") != binding["worktreeId"]):
        return False
    resource = result.get("terminalResource")
    if binding["terminalResourceId"] is None:
        if resource is not None:
            return False
    elif (not isinstance(resource, dict)
          or resource.get("id") != binding["terminalResourceId"]
          or resource.get("terminalHandle") != binding["terminalHandle"]
          or resource.get("worktreeId") != binding["worktreeId"]
          or resource.get("originDispatchId") != dispatch
          or resource.get("ownerDispatchId") != dispatch):
        return False
    terminal = result.get("terminal")
    if binding["terminalHandle"] is None:
        return terminal is None
    return terminal is None or (isinstance(terminal, dict)
                                and terminal.get("handle") == binding["terminalHandle"])


def _upgrade_predecessor_binding(shown: dict, effect: dict, dispatch: str) -> dict:
    """Rebind the immediate predecessor row from one exact native readback."""
    binding = effect.get("native_binding")
    result = shown.get("result")
    if (binding_version(binding) != "predecessor"
            or shown.get("runtime") != effect.get("runtime")
            or not isinstance(result, dict)):
        raise PodError("release_identity_unverified", "Predecessor binding cannot be safely upgraded")
    native_dispatch = result.get("dispatch")
    projection = result.get("projection")
    worker = result.get("worker")
    if (not isinstance(native_dispatch, dict) or not isinstance(projection, dict)
            or not isinstance(worker, dict) or native_dispatch.get("id") != dispatch
            or native_dispatch.get("runId") != binding["runId"]
            or native_dispatch.get("taskId") != binding["taskId"]
            or projection.get("id") != binding["workerId"]
            or projection.get("runId") != binding["runId"]
            or projection.get("taskId") != binding["taskId"]
            or projection.get("dispatchId") != dispatch
            or worker.get("dispatchId") != dispatch
            or not isinstance(worker.get("worktreeId"), str) or not worker["worktreeId"]):
        raise PodError("release_identity_unverified", "Predecessor native identities are contradictory")
    terminal_handle = worker.get("agentTerminalHandle")
    if terminal_handle is not None and (not isinstance(terminal_handle, str) or not terminal_handle):
        raise PodError("release_identity_unverified", "Predecessor terminal identity is malformed")
    resource = result.get("terminalResource")
    if resource is None:
        terminal = result.get("terminal")
        if (terminal_handle is None and terminal is not None
                or terminal_handle is not None
                and terminal is not None
                and (not isinstance(terminal, dict)
                     or terminal.get("handle") != terminal_handle)):
            raise PodError("release_identity_unverified", "Predecessor terminal identities conflict")
        resource_id = None
    elif (not isinstance(resource, dict)
          or not isinstance(resource.get("id"), str) or not resource["id"]
          or resource.get("terminalHandle") != terminal_handle
          or resource.get("worktreeId") != worker["worktreeId"]
          or resource.get("originDispatchId") != dispatch
          or resource.get("ownerDispatchId") != dispatch):
        raise PodError("release_identity_unverified", "Predecessor resource identity is contradictory")
    else:
        resource_id = resource["id"]
    upgraded = {**binding, "worktreeId": worker["worktreeId"],
                "terminalHandle": terminal_handle, "terminalResourceId": resource_id}
    if (not _release_readback_matches(shown, effect["runtime"], dispatch, upgraded)
            or not _release_launch_matches(shown, effect)):
        raise PodError("release_identity_unverified", "Predecessor launch readback changed")
    return upgraded


def _settled_execution(result: dict, binding: dict) -> bool:
    native_dispatch = result.get("dispatch")
    worker = result.get("worker")
    if not isinstance(native_dispatch, dict) or not isinstance(worker, dict):
        return False
    required_dispatch = {"status", "lastFailure"}
    required_worker = {"state", "stage", "lastError"}
    if not required_dispatch <= set(native_dispatch) or not required_worker <= set(worker):
        return False
    succeeded = (native_dispatch["status"], native_dispatch["lastFailure"],
                 worker["state"], worker["stage"], worker["lastError"]) == (
                     "completed", None, "succeeded", "settled", None)
    failure = native_dispatch["lastFailure"]
    structured_failure = failure if isinstance(failure, dict) else None
    if isinstance(failure, str) and failure != "worker_failed" and len(failure) <= 16 * 1024:
        try:
            structured_failure = json.loads(failure)
        except (TypeError, ValueError):
            structured_failure = None
    report_failure = (isinstance(structured_failure, dict)
                      and structured_failure.get("provenance") == "worker_report"
                      and structured_failure.get("outcome") == "failed"
                      and all(isinstance(structured_failure.get(field), str)
                              and structured_failure[field]
                              for field in ("messageId", "completedAt"))
                      and ((binding["terminalHandle"] is None
                            and structured_failure.get("reportedBy") is None)
                           or structured_failure.get("reportedBy") == binding["terminalHandle"]))
    failed_shape = (native_dispatch["status"] == "failed"
                    and worker["state"] == "failed" and worker["stage"] == "settled")
    failed = (failed_shape
              and ((failure == "worker_failed" and worker["lastError"] == "worker_failed")
                   or (report_failure and worker["lastError"] is None)))
    return (succeeded or failed) and worker.get("agentTerminalHandle") == binding["terminalHandle"]


def _resource_disposition(result: dict, binding: dict, disposition: str) -> bool:
    resource = result.get("terminalResource")
    if resource is None:
        projection = result.get("projection")
        projected_resource = projection.get("resource") if isinstance(projection, dict) else None
        expected_states = {"owned": {"none"}, "retained": {"none", "retained"}}
        return (disposition in expected_states
                and binding.get("terminalHandle") is None
                and binding.get("terminalResourceId") is None
                and result.get("terminal") is None
                and (projected_resource is None
                     or (isinstance(projected_resource, dict)
                         and projected_resource.get("state") in expected_states[disposition])))
    if not isinstance(resource, dict):
        return False
    required = {"ownershipState", "releaseState", "retainedReason", "releaseRequestedAt",
                "releaseCompletedAt", "releaseError", "archive"}
    if not required <= set(resource):
        return False
    if disposition == "owned":
        terminal = result.get("terminal")
        return (resource["ownershipState"] == "owned"
                and resource["releaseState"] == "not_requested"
                and resource["retainedReason"] is None
                and resource["releaseRequestedAt"] is None
                and resource["releaseCompletedAt"] is None
                and resource["releaseError"] is None
                and resource["archive"] == {"source": None, "status": None}
                and isinstance(terminal, dict)
                and terminal.get("handle") == binding["terminalHandle"]
                and terminal.get("connected") is True
                and terminal.get("writable") is True)
    archive = resource["archive"]
    if disposition == "released":
        terminal = result.get("terminal")
        observation = result.get("observation")
        projection = result.get("projection")
        projected_resource = projection.get("resource") if isinstance(projection, dict) else None
        terminal_ok = (terminal is None or (binding["terminalHandle"] is not None
                       and isinstance(terminal, dict)
                       and terminal.get("handle") == binding["terminalHandle"]
                       and terminal.get("connected") is False
                       and terminal.get("writable") is False
                       and isinstance(terminal.get("exitCause"), dict)
                       and terminal["exitCause"].get("kind") == "operator_close"))
        observation_ok = (observation is None
                          or (isinstance(observation, dict)
                              and ((observation.get("status") == "exited"
                                    and observation.get("exactWorker") is True)
                                   or (observation.get("status") in ("missing", "unverifiable")
                                       and observation.get("exactWorker") is False))))
        return (resource["ownershipState"] == "released"
                and resource["releaseState"] == "released"
                and resource["retainedReason"] is None
                and resource["releaseError"] is None
                and isinstance(resource["releaseRequestedAt"], str)
                and bool(resource["releaseRequestedAt"])
                and isinstance(resource["releaseCompletedAt"], str)
                and bool(resource["releaseCompletedAt"])
                and isinstance(archive, dict)
                and isinstance(archive.get("source"), str)
                and bool(archive["source"])
                and archive.get("status") == "captured"
                and isinstance(projected_resource, dict)
                and projected_resource.get("state") == "released"
                and projected_resource.get("id") == binding["terminalResourceId"]
                and projected_resource.get("ownerDispatchId") == binding["dispatchId"]
                and projected_resource.get("releaseState") == "released"
                and projected_resource.get("terminalState") == "released"
                and terminal_ok and observation_ok)
    if disposition == "retained":
        return (resource["ownershipState"] == "user_owned"
                and resource["releaseState"] == "retained"
                and resource["retainedReason"] == "user_takeover"
                and resource["releaseError"] is None
                and resource["releaseRequestedAt"] is None
                and resource["releaseCompletedAt"] is None
                and isinstance(archive, dict)
                and archive.get("source") is None
                and archive.get("status") is None)
    return False


def _release_launch_matches(shown: dict, effect: dict) -> bool:
    request = effect.get("request")
    result = shown.get("result")
    if not isinstance(request, dict) or not isinstance(result, dict):
        return False
    launch = {key: request.get(key) for key in ("agent", "model", "effort")}
    if any(not isinstance(value, str) or not value for value in launch.values()):
        return False
    worker = result.get("worker")
    start_options = worker.get("startOptions") if isinstance(worker, dict) else None
    return (isinstance(start_options, dict)
            and start_options.get("launch") == {"requested": launch, "effective": launch})


def _confirmed_release_effect(state: dict, dispatch: str) -> dict:
    bindings = [effect for effect in state["effects"].values()
                if isinstance(effect, dict) and effect.get("state") == "confirmed"
                and isinstance(effect.get("native_binding"), dict)
                and effect["native_binding"].get("dispatchId") == dispatch]
    if len(bindings) != 1:
        raise PodError("release_identity_unverified", "No unique confirmed Pod worker binding")
    return bindings[0]


def _uncertain_release_receipt(receipt: dict, disposition: str) -> dict:
    """Retain bounded native recovery metadata without inventing a retry."""
    if disposition not in ("release_pending", "release_unknown"):
        raise PodError("native_release_uncertain", "Unsupported uncertain release disposition")
    recovery = receipt.get("recovery", receipt.get("nextAction"))
    useful_recovery = (isinstance(recovery, str) and bool(recovery.strip())
                       or isinstance(recovery, list) and bool(recovery)
                       or isinstance(recovery, dict) and bool(recovery))
    if receipt.get("processAction") != "none" or not useful_recovery:
        raise PodError("native_release_uncertain", "Release recovery metadata is incomplete")
    try:
        encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PodError("native_release_uncertain", "Release recovery metadata is malformed") from exc
    if len(encoded.encode()) > 16 * 1024:
        raise PodError("native_release_uncertain", "Release recovery metadata is too large")
    return receipt


def guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                  operation_id: str, assessment: dict, capabilities: dict, quotas: dict,
                  occupancy: dict, plan_revision: str, capacity: int = DEFAULT_WORKER_CAPACITY,
                  capacity_reason: str | None = None, exceptional_grant: dict | None = None,
                  frozen_packet: dict | None = None,
                  port: NativePort | None = None, now: datetime | None = None) -> dict:
    """Execute at most one authorized native start, preserving uncertain effects."""
    bounded_text(owner, name="owner")
    bounded_text(run, name="run")
    bounded_text(task, name="task")
    bounded_text(objective, name="objective")
    if capacity == 3 and not (isinstance(capacity_reason, str) and capacity_reason.strip()):
        raise PodError("capacity_reason_required", "Three workers require a concrete reason")
    policy = effective(project)
    decision = preview(assessment, policy, capabilities=capabilities, quotas=quotas,
                       occupancy=occupancy, objective=objective, now=now or datetime.now(timezone.utc))
    if decision["status"] != "usable":
        raise PodError("route_unusable", decision["reason"])
    route = decision["selected"]
    native_port = port or OrcaPort()
    assurance = native_port.assurance(route)
    intent = reserve(project, objective, owner=owner, operation_id=operation_id, requested=route,
                     route_decision=decision, capability_contract=assurance,
                     native_reader=lambda: native_port.read_native(owner), capacity=capacity,
                     run_id=run, plan_revision=plan_revision, exceptional_grant=exceptional_grant,
                     capacity_reason=capacity_reason, frozen_packet=frozen_packet, now=now)
    if intent["existing"]:
        raise PodError("operation_already_recorded", "Recorded operation must be reconciled, never relaunched")
    try:
        receipt = native_port.start_worker(run=run, task=task, owner=owner, route=route)
        if receipt.get("runtime") != intent["runtime"] or receipt.get("runId") != run or receipt.get("taskId") != task:
            raise PodError("native_effect_uncertain", "Native start receipt identity is incomplete")
        shown = native_port.show_worker(receipt["dispatchId"])
        if shown.get("runtime") != intent["runtime"]:
            raise PodError("native_effect_uncertain", "Native worker readback changed runtime")
        observed = {**receipt, "operation_id": operation_id, "worker_show": shown["result"]}
        effect = reconcile(project, objective, owner=owner, operation_id=operation_id, observed=observed)
        return {"status": "confirmed", "effect": effect, "decision": decision}
    except Exception:
        reconcile(project, objective, owner=owner, operation_id=operation_id, observed=None)
        raise


def release_once(project: Path, objective: str, *, owner: str, dispatch: str,
                 port: NativePort | None = None) -> dict:
    """Release one exactly settled owned worker once; uncertain release is held."""
    from .ledger import _lock, _path, _read, _write
    native_port = port or OrcaPort()
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Release belongs to another coordinator")
        if dispatch in state["cleanup"]:
            raise PodError("release_already_recorded", "Reconcile the existing release identity without repeating it")
        effect = _confirmed_release_effect(state, dispatch)
        binding = effect["native_binding"]
        shown = native_port.show_worker(dispatch)
        result = shown.get("result", {})
        if (not _release_readback_matches(shown, effect["runtime"], dispatch, binding)
                or not _settled_execution(result, binding)
                or not _release_launch_matches(shown, effect)
                or not _resource_disposition(result, binding, "owned")):
            raise PodError("release_identity_unverified", "Native settlement or worker identity is unproven")
        state["cleanup"][dispatch] = {"schema": "pod-cleanup/v1", "state": "reserved",
                                      "binding": binding, "runtime": shown["runtime"],
                                      "repeat_allowed": False}
        _write(path, state)
    try:
        receipt = native_port.release_worker(dispatch)
        if receipt.get("runtime") != shown["runtime"] or receipt.get("dispatchId") != dispatch:
            raise PodError("native_release_uncertain", "Release runtime changed")
        disposition = receipt.get("status", receipt.get("state"))
        if disposition not in ("released", "already_released", "retained",
                               "release_pending", "release_unknown"):
            raise PodError("native_release_uncertain", "Release disposition needs exact reconciliation")
        after = native_port.show_worker(dispatch)
        if (not _release_readback_matches(after, shown["runtime"], dispatch, binding)
                or not _settled_execution(after.get("result", {}), binding)
                or not _release_launch_matches(after, effect)):
            raise PodError("native_release_uncertain", "Post-release readback identity changed")
        if disposition in ("release_pending", "release_unknown"):
            recovery = _uncertain_release_receipt(receipt, disposition)
            if _resource_disposition(after["result"], binding, "released"):
                disposition = "released"
            elif _resource_disposition(after["result"], binding, "retained"):
                disposition = "retained"
        else:
            expected_disposition = "released" if disposition in ("released", "already_released") else "retained"
            if not _resource_disposition(after["result"], binding, expected_disposition):
                raise PodError("native_release_uncertain", "Release resource proof is contradictory or incomplete")
        with _lock(path):
            state = _read(path)
            state["cleanup"][dispatch]["state"] = disposition
            if disposition in ("release_pending", "release_unknown"):
                state["cleanup"][dispatch]["recovery"] = recovery
            _write(path, state)
        return {"status": disposition, "dispatch": dispatch}
    except Exception:
        with _lock(path):
            state = _read(path)
            state["cleanup"][dispatch]["state"] = "uncertain"
            _write(path, state)
        raise


def reconcile_release(project: Path, objective: str, *, owner: str, dispatch: str,
                      port: NativePort | None = None) -> dict:
    """Read native release state only; never repeat the release effect."""
    from .ledger import _lock, _path, _read, _write
    native_port = port or OrcaPort()
    path = _path(project, objective)
    with _lock(path):
        state = _read(path)
        if state["owner"] != owner:
            raise PodError("unknown_release", "No owned release intention")
        if not isinstance(state["effects"], dict) or not isinstance(state["cleanup"], dict):
            raise PodError("state_migration_required", "Effect or cleanup journal is malformed")
        if dispatch not in state["cleanup"]:
            # A confirmed launch can outlive the fleet projection before any
            # release attempt was journaled. Inspect its exact native resource
            # without issuing a second effect or discarding launch evidence.
            effect = _confirmed_release_effect(state, dispatch)
            binding = effect["native_binding"]
            if not isinstance(effect.get("runtime"), str) or not effect["runtime"]:
                raise PodError("release_identity_unverified", "Confirmed worker binding is incomplete")
            shown = native_port.show_worker(dispatch)
            if binding_version(binding) == "predecessor":
                binding = _upgrade_predecessor_binding(shown, effect, dispatch)
                effect["native_binding"] = binding
                _write(path, state)
            if (not _release_readback_matches(shown, effect["runtime"], dispatch, binding)
                    or not _release_launch_matches(shown, effect)):
                raise PodError("release_identity_unverified", "Native release readback or launch changed")
            result = shown["result"]
            if (_settled_execution(result, binding)
                    and _resource_disposition(result, binding, "released")):
                state["cleanup"][dispatch] = {"schema": "pod-cleanup/v1", "state": "released",
                                              "binding": binding, "runtime": shown["runtime"],
                                              "repeat_allowed": False}
                _write(path, state)
                return {"status": "released", "dispatch": dispatch, "source": "native_readback"}
            return {"status": "confirmed", "dispatch": dispatch,
                    "next_safe_action": "inspect native recovery metadata"}
        cleanup = state["cleanup"][dispatch]
        if not isinstance(cleanup, dict):
            raise PodError("state_migration_required", "Cleanup row is malformed")
        if cleanup.get("state") not in ("reserved", "uncertain", "retained", "released",
                                        "already_released", "release_pending", "release_unknown"):
            raise PodError("state_migration_required", "Cleanup state is unsupported")
        if cleanup["state"] not in ("reserved", "uncertain", "retained",
                                    "release_pending", "release_unknown"):
            return {"status": cleanup["state"], "dispatch": dispatch}
        shown = native_port.show_worker(dispatch)
        result = shown.get("result", {})
        binding = cleanup["binding"]
        effect = _confirmed_release_effect(state, dispatch)
        if (effect.get("native_binding") != binding or effect.get("runtime") != cleanup["runtime"]
                or not _release_readback_matches(shown, cleanup["runtime"], dispatch, binding)
                or not _settled_execution(result, binding)
                or not _release_launch_matches(shown, effect)):
            raise PodError("release_identity_unverified", "Native release readback or launch changed")
        if _resource_disposition(result, binding, "released"):
            cleanup["state"] = "released"
            _write(path, state)
            return {"status": "released", "dispatch": dispatch, "source": "native_readback"}
        if (cleanup["state"] in ("reserved", "uncertain", "release_pending", "release_unknown")
                and _resource_disposition(result, binding, "retained")):
            cleanup["state"] = "retained"
            _write(path, state)
            return {"status": "retained", "dispatch": dispatch, "source": "native_readback"}
        return {"status": cleanup["state"], "dispatch": dispatch,
                "next_safe_action": "inspect native recovery metadata"}

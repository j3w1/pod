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
from .ledger import reconcile, reserve
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
        if completed.returncode or not value.get("ok") or not isinstance(value.get("result"), dict):
            raise PodError("native_release_uncertain", "Native release outcome is uncertain")
        return {"runtime": value.get("_meta", {}).get("runtimeId"), **value["result"]}


def _release_readback_matches(shown: dict, runtime: str, dispatch: str, binding: dict) -> bool:
    result = shown.get("result")
    if not isinstance(result, dict) or shown.get("runtime") != runtime:
        return False
    native_dispatch = result.get("dispatch")
    projection = result.get("projection")
    worker = result.get("worker")
    return (isinstance(native_dispatch, dict) and isinstance(projection, dict)
            and isinstance(worker, dict)
            and native_dispatch.get("id") == dispatch
            and native_dispatch.get("runId") == binding["runId"]
            and native_dispatch.get("taskId") == binding["taskId"]
            and projection.get("id") == binding["workerId"]
            and projection.get("runId") == binding["runId"]
            and projection.get("taskId") == binding["taskId"]
            and projection.get("dispatchId") == dispatch
            and worker.get("dispatchId") == dispatch)


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
        bindings = [e for e in state["effects"].values() if e.get("state") == "confirmed"
                    and e.get("native_binding", {}).get("dispatchId") == dispatch]
        if len(bindings) != 1:
            raise PodError("release_identity_unverified", "No unique confirmed Pod worker binding")
        binding = bindings[0]["native_binding"]
        requested_launch = {key: bindings[0]["request"][key] for key in ("agent", "model", "effort")}
        shown = native_port.show_worker(dispatch)
        result = shown.get("result", {})
        native_dispatch = result.get("dispatch", {})
        if (not _release_readback_matches(shown, bindings[0]["runtime"], dispatch, binding)
                or native_dispatch.get("status") not in ("completed", "failed")
                or result.get("worker", {}).get("startOptions", {}).get("launch") !=
                   {"requested": requested_launch, "effective": requested_launch}):
            raise PodError("release_identity_unverified", "Native settlement or worker identity is unproven")
        state["cleanup"][dispatch] = {"schema": "pod-cleanup/v1", "state": "reserved",
                                      "binding": binding, "runtime": shown["runtime"],
                                      "repeat_allowed": False}
        _write(path, state)
    try:
        receipt = native_port.release_worker(dispatch)
        if receipt.get("runtime") != shown["runtime"]:
            raise PodError("native_release_uncertain", "Release runtime changed")
        disposition = receipt.get("status", receipt.get("state"))
        if disposition not in ("released", "already_released", "retained"):
            raise PodError("native_release_uncertain", "Release disposition needs exact reconciliation")
        after = native_port.show_worker(dispatch)
        if not _release_readback_matches(after, shown["runtime"], dispatch, binding):
            raise PodError("native_release_uncertain", "Post-release readback identity changed")
        resource = after["result"].get("terminalResource")
        if disposition in ("released", "already_released") and (
                not isinstance(resource, dict) or resource.get("releaseState") not in ("released", "already_released")):
            raise PodError("native_release_uncertain", "Released resource readback is unavailable")
        with _lock(path):
            state = _read(path)
            state["cleanup"][dispatch]["state"] = disposition
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
            bindings = [effect for effect in state["effects"].values()
                        if isinstance(effect, dict) and effect.get("state") == "confirmed"
                        and isinstance(effect.get("native_binding"), dict)
                        and effect["native_binding"].get("dispatchId") == dispatch]
            if len(bindings) != 1:
                raise PodError("unknown_release", "No unique confirmed Pod worker binding")
            effect = bindings[0]
            binding = effect["native_binding"]
            if (not isinstance(effect.get("runtime"), str) or not effect["runtime"]
                    or any(not isinstance(binding.get(field), str) or not binding[field]
                           for field in ("dispatchId", "workerId", "runId", "taskId"))):
                raise PodError("release_identity_unverified", "Confirmed worker binding is incomplete")
            shown = native_port.show_worker(dispatch)
            if not _release_readback_matches(shown, effect["runtime"], dispatch, binding):
                raise PodError("release_identity_unverified", "Native release readback does not join binding")
            result = shown["result"]
            resource = result.get("terminalResource")
            release_state = resource.get("releaseState") if isinstance(resource, dict) else None
            if (result["dispatch"].get("status") in ("completed", "failed")
                    and release_state in ("released", "already_released")):
                state["cleanup"][dispatch] = {"schema": "pod-cleanup/v1", "state": release_state,
                                              "binding": binding, "runtime": shown["runtime"],
                                              "repeat_allowed": False}
                _write(path, state)
                return {"status": release_state, "dispatch": dispatch, "source": "native_readback"}
            return {"status": "confirmed", "dispatch": dispatch,
                    "next_safe_action": "inspect native recovery metadata"}
        cleanup = state["cleanup"][dispatch]
        if not isinstance(cleanup, dict):
            raise PodError("state_migration_required", "Cleanup row is malformed")
        if cleanup.get("state") not in ("reserved", "uncertain", "retained", "released", "already_released"):
            raise PodError("state_migration_required", "Cleanup state is unsupported")
        if cleanup["state"] not in ("reserved", "uncertain", "retained"):
            return {"status": cleanup["state"], "dispatch": dispatch}
        shown = native_port.show_worker(dispatch)
        result = shown.get("result", {})
        resource = result.get("terminalResource")
        binding = cleanup["binding"]
        if not _release_readback_matches(shown, cleanup["runtime"], dispatch, binding):
            raise PodError("release_identity_unverified", "Native release readback does not join binding")
        if isinstance(resource, dict) and resource.get("releaseState") in ("released", "already_released"):
            cleanup["state"] = "released"
            _write(path, state)
            return {"status": "released", "dispatch": dispatch, "source": "native_readback"}
        if (cleanup["state"] in ("reserved", "uncertain") and isinstance(resource, dict)
                and resource.get("releaseState") == "retained"):
            cleanup["state"] = "retained"
            _write(path, state)
            return {"status": "retained", "dispatch": dispatch, "source": "native_readback"}
        return {"status": cleanup["state"], "dispatch": dispatch,
                "next_safe_action": "inspect native recovery metadata"}

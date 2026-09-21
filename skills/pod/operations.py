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
from .orca import (account_metadata_raw, agent_login_mode, bucket_for, contract, hosts, identity,
                   mutate_command, route_establishment, worker_rows, worker_show, worktree_selector)
from .routing import preview
from .util import bounded_text


DELIVERY_TYPES = ("worker_done", "escalation", "question")
STRUCTURED_START_FAILURES = ("failed", "outcome_unknown")
# Native worker states that prove the launch happened, whatever became of it after.
STARTED_STATES = ("ready", "running", "succeeded", "failed", "stopped")


class NativePort(Protocol):
    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool) -> dict: ...
    def read_native(self, owner: str, *, run: str | None = None, route: dict | None = None,
                    runs: tuple[str, ...] = ()) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str) -> dict: ...
    def find_worker(self, *, run: str, task: str) -> list[dict]: ...
    def show_worker(self, dispatch: str) -> dict: ...
    def release_worker(self, dispatch: str) -> dict: ...
    def wait_delivery(self, *, run: str, timeout_ms: int | None = None,
                      ack: str | None = None, types: tuple[str, ...] = DELIVERY_TYPES) -> dict: ...


def _identifier(value: object) -> str | None:
    """An identity that a projection may give as a bare string or as an object."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("id", "dispatchId", "dispatch_id"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
    return None


def _terminal_state(row: dict) -> str:
    state = row.get("terminalState")
    if state == "released":
        return "released"
    if state == "retained":
        return "retained"
    if state in ("release_pending", "release_unknown"):
        return "uncertain"
    if state == "active":
        return "active"
    return "occupied"


class OrcaPort:
    """Installed Orca adapter: every native effect goes through the mutation allowlist."""

    def __init__(self, project: Path | None = None):
        # The project under admission, so a state-home containment check is judged against
        # it rather than against whatever directory the helper happened to run in.
        self.project = project

    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool = False) -> dict:
        return route_establishment(route, model_policy, snapshot=contract(),
                                   accounts=account_metadata_raw(),
                                   login=agent_login_mode(route["agent"]), fleet=hosts(),
                                   child_delegation=child_delegation)

    def read_native(self, owner: str, *, run: str | None = None, route: dict | None = None,
                    runs: tuple[str, ...] = ()) -> dict:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        authoritative = bool(handle) and handle == owner
        pages = [worker_rows(run)] if run else [worker_rows()]
        scope = "all"
        if run:
            extra_runs = sorted({extra for extra in runs if extra and extra != run})
            scope = "bound+ledger_runs" if extra_runs else "bound"
            for extra in extra_runs:
                pages.append(worker_rows(extra))
        runtime = pages[0]["runtime"]
        raw_scope = pages[0]["scope"]
        if not run:
            source = raw_scope.get("source") if isinstance(raw_scope, dict) else None
            scope = source if source == "all" else "bound+ledger_runs"
        bindings = _ledger_bindings(runtime, self.project)
        rows = []
        cross_host = False
        for page in pages:
            if page["runtime"] != runtime:
                raise PodError("orca_runtime_changed", "Runtime changed during fleet read")
            for worker in page["workers"]:
                dispatch = identity(worker, "dispatchId")
                projection = worker.get("projection") if isinstance(worker.get("projection"), dict) else {}
                host = projection.get("host")
                host_id = host.get("id") if isinstance(host, dict) else host
                if host_id not in (None, "local"):
                    cross_host = True
                parent = _identifier(projection.get("parent"))
                bound = bindings.get(dispatch)
                descendant = False
                if bound is None and parent is not None and parent in bindings:
                    bound, descendant = bindings[parent], True
                row = {"state": _terminal_state(worker), "dispatchId": dispatch,
                       "host": host_id or "local", "parent": parent}
                if bound is None:
                    row.update({"account": None, "objective": None, "foreign": True})
                else:
                    row.update({"account": bound["account"], "objective": bound["objective"],
                                "agent": bound["agent"], "bucket": bound["bucket"],
                                "descendant": descendant})
                rows.append(row)
        quota = _quota_snapshot(route) if route else None
        return {"runtime": runtime, "owner": owner if authoritative else None,
                "authoritative": authoritative, "scope": scope,
                "complete": all(page["complete"] for page in pages), "workers": rows,
                "quota": quota, "cross_host": cross_host, "atomic_admission": False,
                "descendants_allowed": False}

    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str = "current") -> dict:
        selector = worktree_selector(worktree)
        if selector is None:
            raise PodError("invalid_worktree_selector",
                           "Worker placement must name an existing worktree; creation modes are refused")
        argv = ["orchestration", "worker-start", "--task", task, "--run", run,
                "--worktree", selector, "--agent", route["agent"], "--model", route["model"],
                "--effort", route["effort"], "--json"]
        receipt = mutate_command(argv, accept_exit=(0, 1))
        result = receipt["result"]
        state = result.get("state")
        if receipt["exit"] == 1 and state not in STRUCTURED_START_FAILURES:
            raise PodError("native_effect_uncertain", "Native start failed without a structured outcome")
        return {"runtime": receipt["runtime"], "exit": receipt["exit"], **result}

    def find_worker(self, *, run: str, task: str) -> list[dict]:
        """Read back which workers this Run already holds for a Task."""
        fleet = worker_rows(run)
        return [worker for worker in fleet["workers"] if identity(worker, "taskId") == task]

    def show_worker(self, dispatch: str) -> dict:
        return worker_show(dispatch)

    def release_worker(self, dispatch: str) -> dict:
        receipt = mutate_command(["orchestration", "worker-release", "--dispatch", dispatch, "--json"],
                                 accept_exit=(0, 1))
        result = {"runtime": receipt["runtime"], **receipt["result"]}
        disposition = result.get("status", result.get("state"))
        if receipt["exit"] == 1 and disposition != "release_unknown":
            raise PodError("native_release_uncertain", "Native release outcome is uncertain")
        return result

    def wait_delivery(self, *, run: str, timeout_ms: int | None = None, ack: str | None = None,
                      types: tuple[str, ...] = DELIVERY_TYPES) -> dict:
        argv = ["orchestration", "check", "--run", run]
        if ack:
            argv += ["--ack", ack]
        if timeout_ms is not None:
            argv += ["--wait", "--types", ",".join(types), "--timeout-ms", str(int(timeout_ms))]
        argv += ["--json"]
        seconds = 30 if timeout_ms is None else int(timeout_ms) // 1000 + 30
        receipt = mutate_command(argv, timeout=seconds)
        return {"runtime": receipt["runtime"], **receipt["result"]}


def _ledger_bindings(runtime: str, project: Path | None = None) -> dict:
    """Which native Dispatches this machine's Pod records actually own."""
    from .ledger import _read, state_root

    bindings = {}
    root = state_root(project)
    if not root.exists():
        return bindings
    for path in root.glob("*/context.json"):
        state = _read(path)
        for effect in state.get("effects", {}).values():
            if not isinstance(effect, dict) or effect.get("runtime") != runtime:
                continue
            binding = effect.get("native_binding")
            request = effect.get("request")
            if not isinstance(binding, dict) or not isinstance(request, dict):
                continue
            dispatch = binding.get("dispatchId")
            if isinstance(dispatch, str) and dispatch:
                bindings[dispatch] = {"account": request.get("account"), "objective": str(path.parent.name),
                                      "agent": request.get("agent"), "bucket": effect.get("bucket")}
    return bindings


def _quota_snapshot(route: dict) -> dict | None:
    """Provider rate-limit windows as a Pod quota snapshot, or None when unavailable."""
    metadata = account_metadata_raw()
    provider = metadata["providers"].get(route.get("agent"))
    if not isinstance(provider, dict) or not provider.get("windows"):
        return None
    bucket = route.get("bucket") or bucket_for(route.get("agent"), route.get("model", ""))
    windows = []
    unknowns = []
    for name, window in provider["windows"].items():
        if name == "fableWeekly" and bucket != "fable":
            continue
        if name == "weekly" and bucket == "fable":
            continue
        used = window.get("usedPercent")
        if type(used) not in (int, float):
            unknowns.append(name)
            continue
        windows.append({"name": name, "remaining_percent": max(0.0, 100.0 - float(used)),
                        "reset_at": window.get("resetsAt")})
    if not windows:
        return None
    updated = provider.get("updated_at_ms")
    if type(updated) not in (int, float):
        # Without a timestamp there is no way to judge age, and an undated number must not
        # be treated as a current reading.
        return None
    observed = datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat()
    # `confidence` states where the numbers came from: the runtime reported them. How much
    # to trust them is a question of age, which the reader answers against
    # `quota_fresh_seconds` using `observed_at`. Orca caches this metadata and does not
    # refresh it on read, so a reading is often hours old and resolves to unknown quota,
    # which permits one active worker on that account rather than the usual two.
    return {"schema": "pod-quota/v1", "provider": route["agent"], "account": route["account"],
            "bucket": bucket, "windows": windows, "observed_at": observed,
            "source": "supported_metadata", "confidence": "observed",
            "unknowns": sorted(unknowns)}


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
    # A worker this coordinator fenced is settled too: the stop is the known outcome.
    # Its lastError carries the diagnostic that prompted the stop, so it is not constrained.
    stopped = (native_dispatch["status"], failure, worker["state"], worker["stage"]) == (
        "failed", "stopped", "stopped", "process_stopped")
    return ((succeeded or failed or stopped)
            and worker.get("agentTerminalHandle") == binding["terminalHandle"])


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
        # Ownership is about the resource, not the process. A worker this coordinator
        # fenced has a closed terminal and a still-owned resource: exactly the case that
        # needs releasing. Requiring a live, writable terminal here would leak it.
        terminal = result.get("terminal")
        return (resource["ownershipState"] == "owned"
                and resource["releaseState"] == "not_requested"
                and resource["retainedReason"] is None
                and resource["releaseRequestedAt"] is None
                and resource["releaseCompletedAt"] is None
                and resource["releaseError"] is None
                and resource["archive"] == {"source": None, "status": None}
                and (terminal is None
                     or (isinstance(terminal, dict)
                         and terminal.get("handle") == binding["terminalHandle"])))
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
                # The archive must be resolved, not necessarily captured. A worker whose
                # process was stopped before it wrote a transcript reports `unavailable`,
                # which is a complete native statement; an absent status is not.
                and isinstance(archive, dict)
                and ((archive.get("status") == "captured"
                      and isinstance(archive.get("source"), str) and bool(archive["source"]))
                     or (archive.get("status") == "unavailable"
                         and archive.get("source") is None))
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


def _structured_failure(receipt: dict) -> dict:
    """Bounded recovery metadata from a native start that failed with a known shape."""
    keep = {key: receipt.get(key) for key in
            ("state", "failedStage", "dispatchId", "taskId", "runId", "setup", "effects",
             "residualResources", "recovery", "mutation")
            if receipt.get(key) is not None}
    try:
        encoded = json.dumps(keep, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PodError("native_effect_uncertain", "Native failure metadata is malformed") from exc
    if len(encoded.encode()) > 16 * 1024:
        raise PodError("native_effect_uncertain", "Native failure metadata is too large")
    return keep


def guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                  operation_id: str, assessment: dict, capabilities: dict, quotas: dict,
                  occupancy: dict, plan_revision: str, capacity: int = DEFAULT_WORKER_CAPACITY,
                  capacity_reason: str | None = None, exceptional_grant: dict | None = None,
                  frozen_packet: dict | None = None, worktree: str = "current",
                  port: NativePort | None = None, now: datetime | None = None) -> dict:
    """Execute at most one authorized native start, preserving uncertain effects."""
    bounded_text(owner, name="owner")
    bounded_text(run, name="run")
    bounded_text(task, name="task")
    bounded_text(objective, name="objective")
    if capacity == 3 and not (isinstance(capacity_reason, str) and capacity_reason.strip()):
        raise PodError("capacity_reason_required", "Three workers require a concrete reason")
    if worktree_selector(worktree) is None:
        raise PodError("invalid_worktree_selector",
                       "Worker placement must name an existing worktree; creation modes are refused")
    policy = effective(project)
    decision = preview(assessment, policy, capabilities=capabilities, quotas=quotas,
                       occupancy=occupancy, objective=objective, now=now or datetime.now(timezone.utc))
    if decision["status"] != "usable":
        raise PodError("route_unusable", decision["reason"])
    route = decision["selected"]
    model_policy = policy["policy"]["models"].get(route.get("alias"), {})
    child_delegation = bool(policy["policy"]["policy"].get("child_delegation"))
    native_port = port or OrcaPort(project)
    establishment = native_port.establish(route, model_policy, child_delegation=child_delegation)
    # The established bucket is recorded as an observation. It never rewrites the route a
    # frozen packet was bound to, which the coordinator chose before anything was read.
    intent = reserve(project, objective, owner=owner, operation_id=operation_id, requested=route,
                     route_decision=decision, establishment=establishment,
                     native_reader=lambda: native_port.read_native(owner, run=run, route=route),
                     capacity=capacity,
                     run_id=run, plan_revision=plan_revision, exceptional_grant=exceptional_grant,
                     capacity_reason=capacity_reason, frozen_packet=frozen_packet, now=now)
    if intent["existing"]:
        raise PodError("operation_already_recorded", "Recorded operation must be reconciled, never relaunched")
    try:
        receipt = native_port.start_worker(run=run, task=task, owner=owner, route=route,
                                           worktree=worktree)
        if receipt.get("state") in STRUCTURED_START_FAILURES:
            # The Dispatch may already own resources. Keep the slot occupied and hand
            # back exact recovery metadata; never relaunch under this operation id.
            failure = _structured_failure(receipt)
            effect = reconcile(project, objective, owner=owner, operation_id=operation_id,
                               observed=None, native_failure=failure)
            return {"status": "uncertain", "effect": effect, "decision": decision,
                    "establishment": establishment, "native_failure": failure}
        if receipt.get("runtime") != intent["runtime"] or receipt.get("runId") != run or receipt.get("taskId") != task:
            raise PodError("native_effect_uncertain", "Native start receipt identity is incomplete")
        shown = native_port.show_worker(receipt["dispatchId"])
        if shown.get("runtime") != intent["runtime"]:
            raise PodError("native_effect_uncertain", "Native worker readback changed runtime")
        observed = {**receipt, "operation_id": operation_id, "worker_show": shown["result"]}
        effect = reconcile(project, objective, owner=owner, operation_id=operation_id, observed=observed)
        return {"status": "confirmed", "effect": effect, "decision": decision,
                "establishment": establishment}
    except Exception:
        reconcile(project, objective, owner=owner, operation_id=operation_id, observed=None)
        raise


def reconcile_launch(project: Path, objective: str, *, owner: str, operation_id: str,
                     run: str, task: str, port: NativePort | None = None) -> dict:
    """Recover one uncertain launch by reading native state, never by starting again."""
    from .ledger import _path, _read

    bounded_text(owner, name="owner")
    bounded_text(operation_id, name="operation_id", limit=128)
    native_port = port or OrcaPort(project)
    state = _read(_path(project, objective))
    effect = state["effects"].get(operation_id)
    if not isinstance(effect, dict):
        raise PodError("unknown_effect", "No owned effect identity to reconcile")
    if effect.get("state") == "confirmed":
        return {"status": "confirmed", "effect": effect, "action": "none"}
    # The Run is recorded on the effect. Adopting a worker from a Run this effect was never
    # reserved for would bind Pod to a Dispatch it never started, and a later release would
    # act on someone else's worker.
    if effect.get("run_id") != run:
        raise PodError("effect_identity_mismatch",
                       "Recovery names a Run this effect was not reserved for")
    rows = native_port.find_worker(run=run, task=task)
    if not rows:
        return {"status": "uncertain", "effect": effect, "action": "hold",
                "reason": "no native worker row joins this Run and Task"}
    if len(rows) > 1:
        raise PodError("native_occupancy_unverified",
                       "More than one native worker joins this Run and Task")
    dispatch = identity(rows[0], "dispatchId")
    if not isinstance(dispatch, str) or not dispatch:
        raise PodError("native_identity_unverified", "Native worker row has no Dispatch identity")
    shown = native_port.show_worker(dispatch)
    result = shown.get("result", {})
    worker = result.get("worker") if isinstance(result.get("worker"), dict) else {}
    start_options = worker.get("startOptions") if isinstance(worker.get("startOptions"), dict) else {}
    launch = start_options.get("launch") if isinstance(start_options.get("launch"), dict) else {}
    native_dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
    # A worker that reached any of these states demonstrably started, whatever it did
    # next. A worker still unobserved at start has proved nothing and stays uncertain.
    started = worker.get("state") in STARTED_STATES
    observed = {"runtime": shown.get("runtime"), "operation_id": operation_id,
                "dispatchId": dispatch, "runId": identity(native_dispatch, "runId"),
                "taskId": identity(native_dispatch, "taskId"),
                "state": "ready" if started else worker.get("state"),
                "launch": launch, "worker_show": result}
    if observed["runId"] != run or observed["taskId"] != task:
        raise PodError("native_identity_unverified",
                       "Native readback does not join the Run and Task being recovered")
    settled = reconcile(project, objective, owner=owner, operation_id=operation_id, observed=observed)
    return {"status": settled.get("state"), "effect": settled, "action": "adopted",
            "dispatchId": dispatch}


def settle_delivery(project: Path, objective: str, *, owner: str, run: str,
                    timeout_ms: int | None = None, port: NativePort | None = None) -> dict:
    """Read one Delivery batch, settle its worker_done items, and report acknowledgment state."""
    from .ledger import record_delivery, reconcile_delivery_item

    bounded_text(owner, name="owner")
    bounded_text(run, name="run")
    native_port = port or OrcaPort(project)
    receipt = native_port.wait_delivery(run=run, timeout_ms=timeout_ms)
    delivery = _delivery_record(receipt, run)
    if delivery is None:
        return {"status": "empty", "run": run, "delivery_id": None, "ack_eligible": False,
                "items": []}
    record_delivery(project, objective, owner=owner, delivery=delivery)
    settled = []
    for message in delivery["messages"]:
        if message["type"] != "worker_done":
            continue
        dispatch = message["dispatchId"]
        shown = native_port.show_worker(dispatch)
        result = shown.get("result", {})
        native_dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
        status = native_dispatch.get("status")
        if status not in ("completed", "failed"):
            raise PodError("delivery_unresolved", "Settlement readback does not show a settled Dispatch")
        # A repeated batch is normal: Orca replays an unacknowledged Delivery. Releasing is
        # done once, so a dispatch that already has a settled cleanup row is stepped over
        # rather than raising and stranding the items behind it.
        if _cleanup_state(project, objective, dispatch) is None:
            release_once(project, objective, owner=owner, dispatch=dispatch, port=native_port)
        completed_at = native_dispatch.get("completedAt") or native_dispatch.get("updatedAt") or status

        def reader(dispatch=dispatch, status=status, message=message, shown=shown, completed_at=completed_at):
            return {"runtime": shown["runtime"], "runId": message["runId"],
                    "messageId": message["id"], "taskId": message["taskId"],
                    "dispatchId": dispatch, "kind": "settlement", "status": status,
                    "receiptId": f"{dispatch}@{completed_at}"}

        reconcile_delivery_item(project, objective, owner=owner, delivery_id=delivery["id"],
                                message_id=message["id"], native_reader=reader)
        settled.append({"message_id": message["id"], "dispatchId": dispatch, "status": status})
    journal = record_delivery(project, objective, owner=owner, delivery=delivery)
    return {"status": "recorded", "run": run, "delivery_id": delivery["id"],
            "ack_eligible": journal["ack_eligible"], "unresolved": journal["unresolved"],
            "items": [dict(message) for message in delivery["messages"]], "settled": settled}


def acknowledge_delivery(project: Path, objective: str, *, owner: str, run: str,
                         delivery_id: str, port: NativePort | None = None) -> dict:
    """Acknowledge a Delivery only once every item has a durable effect."""
    from .ledger import _path, _read

    native_port = port or OrcaPort(project)
    state = _read(_path(project, objective))
    if state["owner"] != owner:
        raise PodError("coordinator_conflict", "Delivery belongs to another coordinator")
    delivery = state["deliveries"].get(delivery_id)
    if not isinstance(delivery, dict):
        raise PodError("unknown_delivery", "No owned Delivery")
    unresolved = [key for key, row in delivery["items"].items() if row.get("effect") is None]
    if unresolved:
        raise PodError("delivery_unresolved", "Every Delivery item needs a durable effect first")
    receipt = native_port.wait_delivery(run=run, ack=delivery_id)
    if receipt.get("runtime") != delivery.get("runtime"):
        raise PodError("delivery_ack_mismatch",
                       "Acknowledgment came from a different runtime than the Delivery")
    acknowledged = receipt.get("acknowledged") or receipt.get("acknowledgedDeliveryId")
    if acknowledged is not None and acknowledged != delivery_id:
        raise PodError("delivery_ack_mismatch", "Native acknowledgment names a different Delivery")
    following = _delivery_record(receipt, run)
    # A receipt that names no Delivery does not confirm one. The batch was sent for
    # acknowledgment and every item is durably resolved, but the claim stays honest.
    return {"status": "acknowledged" if acknowledged == delivery_id else "acknowledgment_unconfirmed",
            "delivery_id": delivery_id, "confirmed_by_runtime": acknowledged == delivery_id,
            "next_delivery": following["id"] if following else None}


def _cleanup_state(project: Path, objective: str, dispatch: str) -> str | None:
    """The recorded disposition of a release, or None when none was ever recorded."""
    from .ledger import _path, _read

    state = _read(_path(project, objective))
    row = state["cleanup"].get(dispatch)
    return row.get("state") if isinstance(row, dict) else None


def _delivery_record(receipt: dict, run: str) -> dict | None:
    """Normalise one native Delivery batch into the journal's exact record."""
    payload = receipt.get("delivery")
    if not isinstance(payload, dict):
        payload = receipt if isinstance(receipt.get("messages"), list) else None
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    delivery_id = payload.get("id") or payload.get("deliveryId")
    if not isinstance(messages, list) or not messages or not isinstance(delivery_id, str):
        return None
    rows = []
    for message in messages:
        if not isinstance(message, dict):
            raise PodError("invalid_delivery", "Delivery item is malformed")
        body = message.get("payload")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as exc:
                raise PodError("invalid_delivery", "Delivery payload is not decodable") from exc
        body = body if isinstance(body, dict) else {}
        row = {"id": message.get("id"), "type": message.get("type"),
               "runId": identity(message, "runId") or body.get("runId") or body.get("run_id") or run}
        task = identity(message, "taskId") or body.get("taskId") or body.get("task_id")
        dispatch = identity(message, "dispatchId") or body.get("dispatchId") or body.get("dispatch_id")
        if task is not None:
            row["taskId"] = task
        if dispatch is not None:
            row["dispatchId"] = dispatch
        rows.append(row)
    return {"id": delivery_id, "runtime": receipt["runtime"], "runId": run, "messages": rows}


def release_once(project: Path, objective: str, *, owner: str, dispatch: str,
                 port: NativePort | None = None) -> dict:
    """Release one exactly settled owned worker once; uncertain release is held."""
    from .ledger import _lock, _path, _read, _write
    native_port = port or OrcaPort(project)
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
    native_port = port or OrcaPort(project)
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
        effect = _confirmed_release_effect(state, dispatch)
        binding = cleanup.get("binding")
        if (effect.get("native_binding") != binding or effect.get("runtime") != cleanup.get("runtime")
                or binding_version(binding) is None):
            raise PodError("release_identity_unverified", "Cleanup and launch bindings do not match")
        predecessor = binding_version(binding) == "predecessor"
        if (not predecessor and cleanup["state"] not in
                ("reserved", "uncertain", "retained", "release_pending", "release_unknown")):
            return {"status": cleanup["state"], "dispatch": dispatch}
        shown = native_port.show_worker(dispatch)
        if predecessor:
            binding = _upgrade_predecessor_binding(shown, effect, dispatch)
        result = shown.get("result", {})
        if (effect.get("runtime") != cleanup["runtime"]
                or not _release_readback_matches(shown, cleanup["runtime"], dispatch, binding)
                or not _settled_execution(result, binding)
                or not _release_launch_matches(shown, effect)):
            raise PodError("release_identity_unverified", "Native release readback or launch changed")
        claimed_released = cleanup["state"] in ("released", "already_released")
        native_released = _resource_disposition(result, binding, "released")
        if claimed_released and not native_released:
            raise PodError("release_identity_unverified", "Recorded release disposition is unproven")
        may_settle_retained = cleanup["state"] in (
            "reserved", "uncertain", "release_pending", "release_unknown")
        native_retained = may_settle_retained and _resource_disposition(result, binding, "retained")
        if native_released:
            cleanup["state"] = "released" if not claimed_released else cleanup["state"]
        elif native_retained:
            cleanup["state"] = "retained"
        if predecessor:
            effect["native_binding"] = binding
            cleanup["binding"] = binding
        if predecessor or native_released or native_retained:
            _write(path, state)
        if native_released or native_retained:
            return {"status": cleanup["state"], "dispatch": dispatch, "source": "native_readback"}
        return {"status": cleanup["state"], "dispatch": dispatch,
                "next_safe_action": "inspect native recovery metadata"}

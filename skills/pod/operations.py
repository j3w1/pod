"""Serialized policy admission and exact Orca request recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .config import DEFAULT_WORKER_CAPACITY, effective
from .errors import PodError
from .ledger import (admission_identity, binding_valid, migrate_v1, read, reserve,
                     update_admission, _spending_grant_binding)
from .orca import (account_metadata_raw, agent_login_mode, bucket_for, contract, hosts, identity,
                   mutate_command, read_command, route_establishment, run_rows, worker_rows, worker_show,
                   worktree_selector)
from .routing import preview
from .util import bounded_text


STARTED_STATES = ("ready", "running", "succeeded", "failed", "stopped")


class NativePort(Protocol):
    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool) -> dict: ...
    def read_native(self, owner: str, *, route: dict | None = None) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str, retry_request: str | None = None) -> dict: ...
    def request_show(self, request_uuid: str) -> dict: ...
    def find_worker(self, *, run: str, task: str) -> list[dict]: ...
    def show_worker(self, dispatch: str) -> dict: ...


class OrcaPort:
    """Thin installed-Orca port: bounded reads plus worker-start/replay only."""

    def __init__(self, project: Path | None = None):
        self.project = project

    def establish(self, route: dict, model_policy: dict, *, child_delegation: bool = False) -> dict:
        return route_establishment(route, model_policy, snapshot=contract(),
                                   accounts=account_metadata_raw(),
                                   login=agent_login_mode(route["agent"]), fleet=hosts(),
                                   child_delegation=child_delegation)

    def read_native(self, owner: str, *, route: dict | None = None) -> dict:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        authoritative = bool(handle) and handle == owner
        before = run_rows()
        runtime = before["runtime"]
        run_ids = tuple(sorted(row["id"] for row in before["runs"]))
        pages = [(run_id, worker_rows(run_id)) for run_id in run_ids]
        after = run_rows()
        after_ids = tuple(sorted(row["id"] for row in after["runs"]))
        if (before.get("complete") is not True or after.get("complete") is not True
                or after["runtime"] != runtime or after_ids != run_ids):
            raise PodError("native_occupancy_unverified",
                           "Native Run inventory changed during the fleet read")
        rows = []
        cross_host = False
        for run_id, page in pages:
            if page["runtime"] != runtime or page["complete"] is not True:
                raise PodError("orca_runtime_changed", "Orca projection changed during admission read")
            scope = page.get("scope")
            if (not isinstance(scope, dict) or scope.get("source") != "flag"
                    or scope.get("run") != run_id):
                raise PodError("native_occupancy_unverified",
                               "Run-scoped worker projection does not prove its exact Run")
            for worker in page["workers"]:
                if not isinstance(worker, dict):
                    raise PodError("orca_contract", "Worker projection is malformed")
                projection = worker.get("projection") if isinstance(worker.get("projection"), dict) else {}
                host = projection.get("host")
                host_id = host.get("id") if isinstance(host, dict) else host
                if host_id not in (None, "local"):
                    cross_host = True
                rows.append(worker)
        return {"runtime": runtime, "owner": owner if authoritative else None,
                "authoritative": authoritative,
                "scope": "all_runs",
                "complete": True, "workers": rows,
                "quota": _quota_snapshot(route) if route else None,
                "cross_host": cross_host, "atomic_admission": False}

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
                if (identity(worker, "taskId") == task
                    or isinstance(worker.get("projection"), dict)
                    and identity(worker["projection"], "taskId") == task)]

    def show_worker(self, dispatch: str) -> dict:
        return worker_show(dispatch)


def _quota_snapshot(route: dict) -> dict | None:
    metadata = account_metadata_raw()
    provider = metadata["providers"].get(route.get("agent"))
    if not isinstance(provider, dict) or not provider.get("windows"):
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
    return {"schema": "pod-quota/v1", "provider": route["agent"], "account": route["account"],
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
    run = identity(receipt, "runId")
    task = identity(receipt, "taskId")
    dispatch = identity(receipt, "dispatchId")
    if run != admission["run_id"] or task != admission["task_id"] or not isinstance(dispatch, str):
        raise PodError("native_identity_unverified", "Start receipt identity is incomplete")
    shown = port.show_worker(dispatch)
    binding = _binding_from_show(shown, admission, dispatch)
    def apply(row: dict) -> None:
        row["state"] = "bound"
        row["native_binding"] = binding
        row["request_uuid"] = request_uuid
        row["error"] = None
        row["recovery"] = {**row.get("recovery", {}), "receipt": "recorded"}
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


def _hold(project: Path, objective: str, *, owner: str, admission_id: str,
          request_uuid: str | None, code: str, detail: object) -> dict:
    def apply(row: dict) -> None:
        row["state"] = "unresolved"
        row["request_uuid"] = request_uuid
        row["error"] = {"code": code, "detail": detail}
        row["recovery"] = {**row.get("recovery", {}),
                           "next": "request-show" if request_uuid else "exact Run/Task/Dispatch read"}
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


def _record_request(project: Path, objective: str, *, owner: str, admission_id: str,
                    request_uuid: str, receipt: dict) -> dict:
    """Persist Orca's UUID before any follow-up read can fail."""
    def apply(row: dict) -> None:
        row["request_uuid"] = request_uuid
        row["recovery"] = {**row.get("recovery", {}), "native_start_state": receipt.get("state"),
                           "failed_stage": receipt.get("failedStage"),
                           "residual_resources": receipt.get("residualResources")}
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply)


def _current_authority(project: Path, objective: str, admission: dict,
                       *, task_policy: dict | None) -> None:
    """A pending replay is still a mutation, so current revocation/spending policy applies."""
    policy = effective(project, task=task_policy)
    if policy["revision"] != admission["route_decision"].get("policy_revision"):
        raise PodError("policy_revision_mismatch", "Policy changed before native request replay")
    state = read(project, objective)
    checkpoint_value = state.get("checkpoint") if isinstance(state, dict) else None
    expected_checkpoint = admission.get("recovery", {}).get("checkpoint_binding")
    current_checkpoint = ({key: checkpoint_value.get(key) for key in
                           ("candidate", "criteria", "plan_revision", "policy_revision")}
                          if isinstance(checkpoint_value, dict) else None)
    if not isinstance(expected_checkpoint, dict) or current_checkpoint != expected_checkpoint:
        raise PodError("checkpoint_binding_changed",
                       "Checkpoint semantics changed before native request replay")
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


def _adopt_unique(project: Path, objective: str, *, owner: str, admission_id: str,
                  port: NativePort, request_uuid: str | None) -> dict:
    admission = read(project, objective)["admissions"][admission_id]
    rows = port.find_worker(run=admission["run_id"], task=admission["task_id"])
    candidates = []
    errors = []
    for row in rows:
        dispatch = identity(row, "dispatchId")
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
                      task_policy: dict | None = None) -> dict:
    """Recover the same admission; never issue a fresh semantic start."""
    native_port = port or OrcaPort(project)
    state = read(project, objective)
    admission = state["admissions"].get(admission_id) if state else None
    if not isinstance(admission, dict) or admission.get("owner") != owner:
        raise PodError("unknown_admission", "No owned admission identity")
    if admission["state"] in ("bound", "closed"):
        return {"status": admission["state"], "admission": admission, "action": "reuse"}
    request_uuid = admission.get("request_uuid")
    if request_uuid is None:
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
    authority = native_port.read_native(owner, route=admission["request"])
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
            row = _bind(project, objective, owner=owner, admission_id=admission_id,
                        receipt={"runtime": admission["runtime"], **receipt},
                        port=native_port, request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "action": "recorded_receipt"}
    if status == "pending":
        _current_authority(project, objective, admission, task_policy=task_policy)
        receipt = native_port.start_worker(run=admission["run_id"], task=admission["task_id"],
                                           owner=owner, route=admission["request"],
                                           worktree=worktree, retry_request=request_uuid)
        returned = receipt.get("request_uuid")
        if returned is not None and returned != request_uuid:
            row = _hold(project, objective, owner=owner, admission_id=admission_id,
                        request_uuid=request_uuid, code="native_request_mismatch", detail=returned)
        else:
            row = _bind(project, objective, owner=owner, admission_id=admission_id,
                        receipt=receipt, port=native_port, request_uuid=request_uuid)
        return {"status": row["state"], "admission": row, "action": "joined_pending_request"}
    row = _adopt_unique(project, objective, owner=owner, admission_id=admission_id,
                        port=native_port, request_uuid=request_uuid)
    return {"status": row["state"], "admission": row, "action": "inspect_after_absent"}


def guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                  assessment: dict, capabilities: dict, quotas: dict, occupancy: dict,
                  plan_revision: str, frozen_packet: dict, capacity: int = DEFAULT_WORKER_CAPACITY,
                  capacity_reason: str | None = None, exceptional_grant: dict | None = None,
                  worktree: str = "current", port: NativePort | None = None,
                  task_policy: dict | None = None, now: datetime | None = None) -> dict:
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
                                      port=native_port, task_policy=task_policy)
        return {**recovered,
                "decision": recovered["admission"]["route_decision"],
                "establishment": recovered["admission"]["effective_evidence"]}
    policy = effective(project, task=task_policy)
    decision = preview(assessment, policy, capabilities=capabilities, quotas=quotas,
                       occupancy=occupancy, objective=objective, now=now)
    if decision.get("status") != "usable":
        raise PodError("route_unusable", "Routing preview did not produce a usable route")
    route = decision["selected"]
    if validated["body"]["route"] != route:
        raise PodError("packet_mismatch", "Packet route differs from effective route")
    establishment = native_port.establish(route, policy["policy"]["models"][route["alias"]],
                                           child_delegation=bool(policy["policy"]["policy"].get("child_delegation")))
    check_bound_sources(project, objective, owner=owner, assignment=validated["packet_id"],
                        sources=validated["body"]["sources"])
    admission = reserve(project, objective, owner=owner, admission_id=admission_id,
                        requested=route, route_decision={**decision,
                                                        "policy": policy["policy"]["policy"],
                                                        "task_policy": task_policy},
                        establishment=establishment,
                        native_reader=lambda: native_port.read_native(owner, route=route),
                        capacity=capacity, run_id=run, task_id=task,
                        plan_revision=plan_revision, packet_id=validated["packet_id"],
                        worktree=worktree, frozen_packet=validated,
                        exceptional_grant=exceptional_grant, capacity_reason=capacity_reason,
                        spending_grant=decision.get("spending_grant"), task_policy=task_policy,
                        now=now)
    if admission["existing"]:
        recovered = recover_admission(project, objective, owner=owner,
                                      admission_id=admission_id, worktree=worktree,
                                      port=native_port, task_policy=task_policy)
        return {**recovered, "decision": decision, "establishment": establishment}
    try:
        receipt = native_port.start_worker(run=run, task=task, owner=owner, route=route,
                                           worktree=worktree)
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


def migrate_state(project: Path, objective: str, *, owner: str,
                  port: NativePort | None = None) -> dict:
    native_port = port or OrcaPort(project)
    return migrate_v1(project, objective, owner=owner, worker_reader=native_port.show_worker)

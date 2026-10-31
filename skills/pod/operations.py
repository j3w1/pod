"""Coordinator-proposed native worker starts with serialized admission and exact recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Protocol
from uuid import UUID

from .contracts import policy_mismatch
from .errors import PodError
from .ledger import (admission_identity, binding_valid, bound_assignments as _bound_assignments,
                     objective_root, read, reserve,
                     update_admission, retry_parent, _lock, _path, _read, _write, _native_assignment_settled,
                     _native_assignment_claim_free)
from .orca import (MAX_OUTPUT, PREFLIGHT_REFUSALS, _passable, contract, current_run, mutate_command, read_command,
                   worker_rows, worker_show, worktree_identity, worktree_selector)
from .util import atomic_json, bounded_json, bounded_text, digest

STARTED_STATES = ("ready", "running", "succeeded", "failed", "stopped")
MAX_START_OBSERVATIONS = 16
START_OBSERVATION_LIMIT = 3 * MAX_OUTPUT


def _check_objective_source(project: Path, binding: dict | None, *, issue_port=None,
                            allow_closed: bool = False) -> None:
    if binding is None:
        return
    from .github import issue_recheck
    result = issue_recheck(project, binding, port=issue_port)
    if result["status"] != "current":
        if allow_closed and result.get("reason") == "closed_issue_requires_intent_reconciliation":
            return
        raise PodError("issue_reconciliation_required",
                       "Execution Spec issue changed or closed; reconcile before another effect",
                       {"reason": result.get("reason")})


def _check_worktree_binding(project: Path, binding: dict | None, current: dict | None = None) -> None:
    """Bind the current repository, branch and worktree right before an effect, read fresh from Git."""
    if binding is None:
        return
    if current is None:
        from .github import repository_context
        current = repository_context(project, fresh=True)
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    if budget is not None:
        budget.check()
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
                       "Current Git repository, branch, or worktree differs from the objective binding; keep packet worktree on the objective and use placement for a separate assignment worktree")


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
                       "Resolved native worker placement differs from the frozen assignment workspace; set packet placement to the child-worktree binding and select that same worktree")
    if not isinstance(observed.get("runtime"), str) or not observed["runtime"]:
        raise PodError("worktree_resolution_ambiguous", "Native placement lacks runtime identity")
    return observed


def _assignment_evidence(shown: dict, admission: dict) -> dict:
    """Project settlement for one already-bound assignment, never terminal ownership."""
    binding = admission.get("native_binding")
    if not binding_valid(binding):
        raise PodError("native_assignment_unverified", "Exact assignment runtime or binding differs")
    if shown.get("runtime") != admission.get("runtime"):
        detail = ({"reason": "runtime_changed"} if isinstance(shown.get("runtime"), str)
                  and shown["runtime"] else None)
        raise PodError("native_assignment_unverified", "Exact assignment runtime or binding differs", detail)
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
    _check_retry_dispatch(dispatch, admission)
    # A fence is not an exit: an abandoned attempt, accounted or not, keeps its claim and its logical
    # slot until the fresh native read shows the process exited with nothing pending.
    from .abandoned import current_claim, current_fence
    if "abandoned_accounting" in admission:
        current_fence(shown, admission, binding)
    claim = current_claim(shown)
    settled = _native_assignment_claim_free(shown)
    return {"claim": claim, "admission_id": admission["admission_id"], "runtime": shown["runtime"],
            "run_id": binding["runId"], "task_id": binding["taskId"],
            "dispatch_id": binding["dispatchId"], "worker_id": binding["workerId"],
            "settled": settled,
            "retry_eligible": (_retry_eligible(shown)
                               and worker.get("agentTerminalHandle") == binding["terminalHandle"]),
            "placement": _placement_evidence(result, binding)}


def _retry_eligible(shown: dict) -> bool:
    """Assignment settlement alone says nothing about the old agent's exit."""
    result = shown["result"]
    projection, worker, dispatch = (result[name] for name in ("projection", "worker", "dispatch"))
    liveness, resource = projection.get("liveness"), projection.get("resource")
    if (not _native_assignment_settled(shown) or dispatch.get("status") != "failed"
            or projection.get("outcome") not in ("failed", "stopped")
            or worker.get("state") not in ("failed", "stopped")
            or not isinstance(liveness, dict) or liveness.get("verdict") != "exited"
            or not isinstance(resource, dict)
            or resource.get("state") not in ("owned", "retained", "released", "absent", "external", "user_owned")
            or resource.get("releaseState") not in ("none", "not_requested", "unreleased", "retained", "released")):
        return False
    if (resource.get("ownerDispatchId") not in (None, dispatch["id"])
            or (resource["state"] == "released") != (resource["releaseState"] == "released")):
        return False
    ownership = result.get("terminalResource")
    if ownership is not None:
        if (not isinstance(ownership, dict) or ownership.get("ownerDispatchId") != dispatch["id"]
                or ownership.get("worktreeId") != worker.get("worktreeId")
                or ownership.get("terminalHandle") != worker.get("agentTerminalHandle")
                or ownership.get("ownershipState") != resource["state"]
                or ownership.get("releaseState") != resource["releaseState"]):
            return False
    return True


def _check_retry_dispatch(dispatch: dict, admission: dict) -> None:
    if admission.get("retry_of") is None:
        return
    expected = admission.get("recovery", {}).get("retry_of_dispatch_id")
    observed = dispatch.get("retryOfDispatchId")
    if dispatch.get("id") == expected or observed is not None and observed != expected:
        raise PodError("native_retry_parent_mismatch", "Native retry names another parent; preserve the contradiction and inspect the exact Dispatch")
    if not expected or observed != expected:
        raise PodError("native_retry_parent_unverified", "Native retry lacks exact retryOfDispatchId; inspect the exact Dispatch without another start")


def _retry_conflict_code(admission: dict) -> str | None:
    for field, code in (("retry_parent_conflict", "native_retry_parent_mismatch"),
                        ("retry_placement_conflict", "native_retry_placement_mismatch")):
        if admission.get("recovery", {}).get(field) == "unresolved":
            return code
    return None


def _check_retry_placement(port: NativePort, binding: dict, admission: dict) -> None:
    if admission.get("retry_of") is None:
        return
    expected = admission.get("recovery", {}).get("placement_binding")
    if not isinstance(expected, dict):
        raise PodError("native_retry_placement_unverified", "Retry lacks its frozen placement; retain the attempt and inspect through Orca")
    try:
        observed = _check_native_placement(port, "id:" + binding["worktreeId"], expected)
    except PodError as exc:
        code = ("native_retry_placement_mismatch" if exc.code == "worktree_binding_changed"
                else "native_retry_placement_unverified")
        raise PodError(code, "Actual retry worker placement does not join the frozen admission; inspect the exact native worker/worktree without another start",
                       {"cause": exc.code, "worktree": binding["worktreeId"]}) from exc
    if observed["runtime"] != admission["runtime"]:
        raise PodError("native_retry_placement_unverified", "Retry worker placement belongs to another runtime; retain the attempt and inspect through Orca")


def _retry_parent_link(state: dict, admission: dict) -> str | None:
    if admission.get("retry_of") is None:
        return None
    parent = retry_parent(state, objective=admission["objective"], owner=admission["owner"],
                          run_id=admission["run_id"], task_id=admission["task_id"],
                          retry_of=admission["retry_of"])
    dispatch = parent["native_binding"]["dispatchId"]
    if dispatch != admission["recovery"].get("retry_of_dispatch_id"):
        raise PodError("native_retry_parent_mismatch", "Recorded retry parent changed; inspect the original admission")
    return dispatch


def _retry_parent_current(state: dict, admission: dict, port: NativePort) -> str | None:
    dispatch = _retry_parent_link(state, admission)
    if dispatch is None:
        return None
    parent = state["admissions"][admission["retry_of"]]
    shown = port.show_worker(dispatch)
    evidence = _assignment_evidence(shown, parent)
    if evidence["retry_eligible"] is not True:
        raise PodError("retry_parent_unsettled", "Retry parent no longer proves failed/stopped settlement, exited agent and known resources; inspect its exact native Dispatch",
                       {"worker_show": shown})
    return dispatch


def _placement_evidence(result: dict, binding: dict) -> dict:
    """Presentation facts from the same exact native read; none is UI rendering proof."""
    worker = result["worker"]
    handle = binding.get("terminalHandle")
    terminal = result.get("terminal")
    resource = result.get("terminalResource")
    # After terminal reuse Orca transfers ownership to the follow-up Dispatch; its tab
    # then belongs to that Dispatch. A resource without a named owner proves neither.
    owned = resource is None or (isinstance(resource, dict)
                                 and resource.get("ownerDispatchId") == binding["dispatchId"])
    terminal = terminal if (isinstance(terminal, dict) and handle is not None
                            and terminal.get("handle") == handle
                            and terminal.get("worktreeId") == binding["worktreeId"]
                            and owned) else {}
    facts = {"worktree": binding["worktreeId"], "tab": terminal.get("tabId"),
             "native_worker_state": worker.get("state"), "terminal": handle,
             "placement_readback": "observed", "surfaces": [], "warnings": [],
             "ui_visibility": "unverified", "ui_focus": "unverified"}
    projection = result.get("projection")
    projection = projection if isinstance(projection, dict) else {}
    release = projection.get("resource")
    release = release if isinstance(release, dict) else {}
    ownership = resource if isinstance(resource, dict) else {}
    observed_terminal = result.get("terminal")
    observed_terminal = observed_terminal if isinstance(observed_terminal, dict) else {}
    observation = result.get("observation")
    observation = observation if isinstance(observation, dict) and result.get("terminal") is None else {}
    # Only presentation facts: terminal previews and unrelated archive/endpoint data are not status.
    facts.update(
        native_release={key: release[key] for key in ("state", "releaseState", "terminalState", "ownerDispatchId", "id")
                        if key in release} or None,
        native_stage=projection.get("stage") if isinstance(projection.get("stage"), dict) else None,
        native_liveness=projection.get("liveness") if isinstance(projection.get("liveness"), dict) else None,
        native_terminal_state=release.get("terminalState") or worker.get("terminalState"),
        terminal_ownership={key: ownership[key] for key in ("ownerDispatchId", "originDispatchId", "ownershipState",
                            "releaseState", "retainedReason", "terminalHandle", "worktreeId") if key in ownership} or None,
        terminal_readback={key: observed_terminal[key] for key in ("handle", "tabId", "worktreeId", "connected",
                           "writable", "orphaned") if key in observed_terminal} or None,
        # Orca's own statement about the worker's terminal record, from the same exact read (R125).
        terminal_observation={key: observation[key] for key in ("status", "exactWorker") if key in observation}
                             or None,
        retained_reason=ownership.get("retainedReason") or worker.get("retainedReason") or worker.get("retainReason"))
    if "role" in projection:
        facts["native_role"] = projection["role"]
    if isinstance(projection.get("nextAction"), dict):
        facts["native_next_action"] = projection["nextAction"]
    if resource is not None or result.get("terminal") is not None or observation:
        # Orca's gone-record observation is judged against the worker's own terminal, so its identity is kept even
        # when the raw resource is omitted (R125, A475).
        facts["worker_terminal"] = worker.get("agentTerminalHandle")
    if worker.get("agentTerminalHandle") != handle:
        facts.update(tab=None, placement_readback="unavailable")
        return facts
    for name in ("effects", "residualResources"):
        effects = worker.get(name)
        for effect in effects if isinstance(effects, list) else []:
            if (not isinstance(effect, dict) or handle is None or effect.get("kind") != "terminal"
                    or effect.get("role") != "agent" or effect.get("id") != handle):
                continue
            for key, target in (("surface", "surfaces"), ("warning", "warnings")):
                value = effect.get(key)
                if isinstance(value, str) and value not in facts[target]:
                    facts[target].append(value)
    return facts


class NativePort(Protocol):
    def capability(self) -> dict: ...
    def read_native(self, owner: str, *, authority_runs: tuple[str, ...] = (),
                    assignments: tuple[dict, ...] = ()) -> dict: ...
    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str, retry_request: str | None = None,
                     terminal: str | None = None, retry_of: str | None = None) -> dict: ...
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
        evidence = []
        for row in assignments:
            try:
                shown = self.show_worker(row["native_binding"]["dispatchId"])
            except PodError as exc:
                # Preserve the native read's code, with the precise read to repeat.
                # This adds no observation and never promotes unknown settlement.
                from .worker_tabs import project as tab_project
                failed = tab_project({"owner": owner, "checkpoint": {"objective": row.get("objective")}},
                                     row, {}, settled=True, failure=exc.code)
                exc.detail = {**(exc.detail or {}), "worker_tabs": [failed], "next_action": failed["wording"]}
                raise
            # Identity/runtime disagreement is a continuity refusal, not an
            # unreadable terminal or a reason to repeat worker-show.
            evidence.append(_assignment_evidence(shown, row))
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
        observed = {"runtime": runtime, "owner": owner if authoritative else None,
                    "authoritative": authoritative, "scope": "objective_assignments",
                    "complete": True, "assignments": evidence, "physical_capacity": "unavailable"}
        if expected_runs:
            # The stable current-Run binding and caller identity runtime continuity compares (A2).
            observed.update(binding=current if stable else None, caller=handle or None)
        return observed

    def start_worker(self, *, run: str, task: str, owner: str, route: dict,
                     worktree: str = "current", retry_request: str | None = None,
                     terminal: str | None = None, retry_of: str | None = None) -> dict:
        if retry_of is not None and terminal is not None:
            raise PodError("retry_reuse_conflict", "A native retry requires a fresh terminal")
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
        if retry_of is not None:
            argv += ["--retry-of", retry_of]
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
        from .abandoned import attach_closure
        return attach_closure(worker_show(dispatch))

    def read_screen(self, handle: str) -> list[str] | None:
        from .orca import terminal_screen
        return terminal_screen(handle)


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


def _identity_from_show(shown: dict, admission: dict, dispatch: str) -> dict:
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
    _check_retry_dispatch(native_dispatch, admission)
    binding = {"runId": admission["run_id"], "taskId": admission["task_id"],
               "dispatchId": dispatch, "workerId": projection.get("id"),
               "worktreeId": worker.get("worktreeId"),
               "terminalHandle": worker.get("agentTerminalHandle")}
    # A record holds only identities that can be written and passed back to Orca exactly (R18).
    if not binding_valid(binding) or not all(value is None or _passable(value) for value in binding.values()):
        raise PodError("native_identity_unverified", "Worker identity is incomplete")
    return binding


def _binding_from_show(shown: dict, admission: dict, dispatch: str) -> dict:
    binding = _identity_from_show(shown, admission, dispatch)
    if shown["result"]["worker"].get("state") not in STARTED_STATES:
        raise PodError("native_start_unsettled", "Worker has not proved that start occurred")
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


def _reuse_effective_evidence(receipt: dict, shown: dict, admission: dict,
                              prior: dict | None) -> tuple[dict, bool, dict | None]:
    effective, mismatch = _effective_evidence(receipt, shown, admission)
    binding = prior.get("native_binding") if isinstance(prior, dict) else None
    decision = prior.get("route_decision") if isinstance(prior, dict) else None
    previous = decision.get("effective") if isinstance(decision, dict) else None
    worker = shown["result"]["worker"]
    if (not admission.get("reuse_of") or not isinstance(prior, dict)
            or prior.get("state") not in ("bound", "closed") or not binding_valid(binding)
            or not binding["terminalHandle"] or not isinstance(previous, dict)
            or any(_observed_value(previous.get(key)) == "unknown"
                   for key in ("agent", "model", "effort"))
            or worker.get("agentTerminalHandle") != binding["terminalHandle"]
            or worker.get("worktreeId") != binding["worktreeId"]):
        return effective, mismatch, None
    start_options = worker.get("startOptions")
    shown_launch = start_options.get("launch") if isinstance(start_options, dict) else None
    receipt_launch = receipt.get("launch") if isinstance(receipt, dict) else None
    observations = [values for launch in (shown_launch, receipt_launch) if isinstance(launch, dict)
                    for values in (launch.get("effective"), launch.get("requested"))
                    if isinstance(values, dict)]
    inherited = []
    for key in ("agent", "model", "effort"):
        if any(_observed_value(values.get(key)) not in ("unknown", previous[key])
               for values in observations):
            mismatch = True
        if effective[key] == "unknown":
            effective[key] = previous[key]
            inherited.append(key)
    provenance = {"admission": admission["reuse_of"], "dispatch": binding["dispatchId"],
                  "terminal": binding["terminalHandle"], "fields": inherited}
    return effective, mismatch, provenance


def _bind(project: Path, objective: str, *, owner: str, admission_id: str,
          receipt: dict, port: NativePort, request_uuid: str | None) -> dict:
    admissions = read(project, objective)["admissions"]
    admission = admissions[admission_id]
    conflict = _retry_conflict_code(admission)
    if conflict is not None:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code=conflict,
                     detail="Preserved native evidence contradicts this retry's admitted parent or placement")
    if receipt.get("runtime") not in (None, admission["runtime"]):
        raise PodError("native_identity_unverified", "Start receipt changed runtime")
    dispatch = receipt.get("dispatchId")
    if (receipt.get("runId") != admission["run_id"] or receipt.get("taskId") != admission["task_id"]
            or not isinstance(dispatch, str)):
        raise PodError("native_identity_unverified", "Start receipt identity is incomplete")
    try:
        _retry_parent_link({"admissions": admissions}, admission)
        if admission.get("retry_of") is not None and "retryOfDispatchId" in receipt:
            _check_retry_dispatch({"id": dispatch, "retryOfDispatchId": receipt["retryOfDispatchId"]}, admission)
        shown = port.show_worker(dispatch)
        binding = _binding_from_show(shown, admission, dispatch)
        _check_retry_placement(port, binding, admission)
        if (admission.get("retry_of") is not None and binding["terminalHandle"] is not None
                and binding["terminalHandle"] == admissions[admission["retry_of"]]["native_binding"]["terminalHandle"]):
            raise PodError("native_retry_terminal_reused", "Native retry reused the parent's terminal; retain both attempts and inspect through Orca")
    except PodError as exc:
        if admission.get("retry_of") is not None and exc.code in (
                "native_retry_parent_mismatch", "native_retry_parent_unverified", "retry_parent_unsettled",
                "native_retry_terminal_reused", "native_retry_placement_mismatch", "native_retry_placement_unverified"):
            return _hold(project, objective, owner=owner, admission_id=admission_id,
                         request_uuid=request_uuid, code=exc.code, detail=str(exc))
        raise
    effective, mismatch, inherited = _reuse_effective_evidence(
        receipt, shown, admission, admissions.get(admission.get("reuse_of")))
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
        if inherited is not None:
            row["effective_evidence"]["inherited"] = inherited
        row["route_decision"]["effective"] = effective
        if mismatch:
            from .util import flag_route_mismatch
            flag_route_mismatch(row)
        row["route_decision"]["effective_unknown"] = any(
            effective[key] == "unknown" for key in ("agent", "model", "effort"))
    # The receipt and worker read were checked at this runtime; a rebind inside the write's
    # own authority join would apply them at another one (A2).
    return update_admission(project, objective, owner=owner, admission_id=admission_id, update=apply,
                            expected_runtime=admission["runtime"])


def _hold(project: Path, objective: str, *, owner: str, admission_id: str,
          request_uuid: str | None, code: str, detail: object,
          request_conflict: bool = False) -> dict:
    def apply(row: dict) -> None:
        recovery = {**row.get("recovery", {}),
                    "next": "request-show" if request_uuid else "exact Run/Task/Dispatch read"}
        previous_error = (row.get("error") or {}).get("code")
        previous_refusal = recovery.get("preflight_refusal")
        if code == "native_retry_parent_mismatch":
            recovery["retry_parent_conflict"] = "unresolved"
        if code == "native_retry_placement_mismatch":
            recovery["retry_placement_conflict"] = "unresolved"
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
                   observation: dict, retry_request: str | None, reference: dict,
                   invocation: dict | None = None) -> dict:
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
        if invocation is not None:
            value["invocation"] = invocation
        ref = digest(value)
        if ref in refs:
            return row
        if len(refs) >= MAX_START_OBSERVATIONS:
            raise PodError("native_evidence_full", "Native start evidence limit reached")
        path = objective_root(project, objective) / "native-start" / (ref + ".json")
        row["recovery"] = {**row.get("recovery", {}), "start_observations": [*refs, ref]}
        if reference.get("runtime") == row["runtime"]:
            if (row.get("retry_of") is not None
                    and reference.get("runId") == row["run_id"] and reference.get("taskId") == row["task_id"]
                    and _passable(reference.get("dispatchId"))):
                observed = row["recovery"].get("observed_dispatch_id")
                if observed not in (None, reference["dispatchId"]):
                    row["recovery"]["request_conflict"] = "unresolved"
                else:
                    row["recovery"]["observed_dispatch_id"] = reference["dispatchId"]
            if row.get("retry_of") is not None:
                parent = row["recovery"].get("retry_of_dispatch_id")
                if (reference.get("dispatchId") == parent
                        or reference.get("retryOfDispatchId") is not None
                        and reference["retryOfDispatchId"] != parent):
                    row["recovery"]["retry_parent_conflict"] = "unresolved"
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
                atomic_json(path, value, limit=START_OBSERVATION_LIMIT, compact=True)
        finally:
            # Even if the separate archive cannot be written, keep the observed UUID
            # and missing-evidence reference whenever the compact journal remains writable.
            _write(journal, state)
        return row


def _start_observed(project: Path, objective: str, *, admission_id: str,
                    port: NativePort, **kwargs) -> dict:
    from .succession import refuse_continued
    state = read(project, objective)
    refuse_continued(state)  # Recheck right before the native effect (R110).
    admission = state["admissions"][admission_id]
    if len(_start_observations(project, objective, admission)) >= MAX_START_OBSERVATIONS:
        raise PodError("native_evidence_full", "Native start evidence limit reached before execution")
    owner, retry = kwargs["owner"], kwargs.get("retry_request")
    invocation = ({"run_id": kwargs["run"], "task_id": kwargs["task"],
                   "retry_of_dispatch_id": kwargs["retry_of"], "runtime": admission["runtime"],
                   "retry_request": retry} if kwargs.get("retry_of") is not None else None)
    try:
        receipt = port.start_worker(**kwargs)
    except Exception as exc:
        observation = getattr(exc, "native_observation", {"transport": type(exc).__name__,
                                                         "response": "unavailable"})
        reference = getattr(exc, "native_reference", {})
        recorded = _observe_start(project, objective, owner=owner, admission_id=admission_id,
                                  observation=observation, retry_request=retry, reference=reference,
                                  invocation=invocation)
        _hold(project, objective, owner=owner, admission_id=admission_id,
              request_uuid=recorded["request_uuid"],
              code=exc.code if isinstance(exc, PodError) else type(exc).__name__,
              detail=str(exc), request_conflict=_known_request_conflict(recorded))
        raise
    observation = receipt.pop("native_observation", None)
    _observe_start(project, objective, owner=owner, admission_id=admission_id,
                   observation=observation if observation is not None else {"receipt": receipt},
                   retry_request=retry, reference=receipt, invocation=invocation)
    return receipt


def _request_conflict_result(project: Path, objective: str, *, owner: str,
                             admission_id: str, receipt: dict,
                             request_uuid: str | None) -> dict | None:
    """Hold a carried request contradiction without choosing a fresh UUID."""
    admission = read(project, objective)["admissions"][admission_id]
    conflict = _retry_conflict_code(admission)
    if conflict is not None:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid or admission.get("request_uuid"),
                     code=conflict, detail="Preserved native evidence contradicts the retry parent or placement")
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


def _save_no_start(project: Path, objective: str, admission_id: str, **evidence) -> str:
    """Archive affirmative no-effect evidence; an empty start journal proves nothing."""
    row = read(project, objective)["admissions"][admission_id]
    value = {"admission_id": admission_id, "run_id": row["run_id"], "task_id": row["task_id"],
             "runtime": row["runtime"], "retry_of_dispatch_id": row["recovery"]["retry_of_dispatch_id"],
             "start_observations": _start_observations(project, objective, row), **evidence}
    ref = digest(value)
    path = objective_root(project, objective) / "no-start" / (ref + ".json")
    if path.exists() or path.is_symlink():
        if bounded_json(path, limit=START_OBSERVATION_LIMIT) != value:
            raise PodError("native_evidence_unavailable", "No-start evidence changed")
    else:
        atomic_json(path, value, limit=START_OBSERVATION_LIMIT, compact=True)
    return ref


def _no_start_proof(project: Path, state: dict, previous: dict, ref: str | None) -> dict:
    """Join immutable evidence to this exact admission and recorded R98 lineage."""
    refs = _start_observations(project, previous["objective"], previous)
    try:
        if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref):
            raise ValueError("missing no-start proof")
        proof = bounded_json(objective_root(project, previous["objective"]) / "no-start" / (ref + ".json"),
                             limit=START_OBSERVATION_LIMIT)
        if (not isinstance(proof, dict) or digest(proof) != ref
                or proof.get("admission_id") != previous["admission_id"]
                or any(proof.get(key) != previous[key] for key in ("run_id", "task_id"))
                or proof.get("retry_of_dispatch_id") != previous["recovery"].get("retry_of_dispatch_id")
                or proof.get("start_observations") != refs):
            raise ValueError("changed no-start proof")
        # R98 rewrites admission runtime references, never recorded raw observations.
        # Accept only the recorded continuity path from the observed runtime.
        observed_runtime = proof.get("runtime")
        reached = observed_runtime
        for entry in (state.get("checkpoint", {}).get("continuity") or {}).get("history", []):
            if (entry["from_runtime"] == reached
                    and (entry.get("provenance") not in ("owner_handoff", "window_continuation")
                         or entry.get("state") == "done")):
                reached = entry["to_runtime"]
        if not isinstance(observed_runtime, str) or reached != previous["runtime"]:
            raise ValueError("no recorded runtime continuity for no-start proof")
    except (OSError, ValueError, PodError) as exc:
        raise PodError("native_evidence_unavailable", "No-start proof is missing or corrupt; inspect the original admission evidence without another start") from exc
    return proof


def _pre_start_proven(proof: dict, admission: dict) -> bool:
    return (proof.get("kind") == "pre_start" and proof.get("stage") == "retry_parent_recheck"
            and proof.get("effect") == "not_called" and proof.get("start_observations") == []
            and admission["request_uuid"] is None
            and isinstance(proof.get("error"), dict) and proof["error"].get("code") in (
                "orca_unavailable", "retry_parent_unsettled", "native_assignment_unverified"))


def _retry_after(project: Path, state: dict, parent: dict | None, previous_id: str) -> dict:
    """A fresh intent succeeds only a proven no-start on this exact native chain."""
    previous = state["admissions"].get(previous_id) if isinstance(previous_id, str) else None
    if (parent is None or not isinstance(previous, dict) or previous["state"] != "deferred"
            or previous.get("retry_of") != parent["admission_id"]
            or previous["recovery"].get("retry_of_dispatch_id") != parent["native_binding"]["dispatchId"]
            or any(previous[key] != parent[key] for key in ("objective", "owner", "run_id", "task_id"))
            or previous.get("native_binding") is not None or _retry_conflict_code(previous)
            or _known_request_conflict(previous)):
        raise PodError("retry_no_start_unverified", "retry_after needs an owned deferred no-start retry with the same retry_of parent; unknown starts keep their reservation and exact request recovery")
    refs = _start_observations(project, previous["objective"], previous)
    proof = _no_start_proof(project, state, previous, previous["recovery"].get("no_start"))
    observed_runtime = proof["runtime"]
    allowed = False
    if proof.get("kind") == "native_refusal":
        receipt = proof.get("receipt")
        if isinstance(receipt, dict) and refs:
            error = receipt.get("error") or {}
            data = error.get("data") or {}
            invocation_bound = False
            for observed_ref in refs:
                value = bounded_json(objective_root(project, previous["objective"]) / "native-start" /
                                     (observed_ref + ".json"), limit=START_OBSERVATION_LIMIT)
                invoked = value.get("invocation")
                if (isinstance(invoked, dict)
                        and all(invoked.get(key) == proof[key] for key in
                                ("run_id", "task_id", "retry_of_dispatch_id", "runtime"))
                        and invoked.get("retry_request") in (None, previous["request_uuid"])):
                    invocation_bound = True
            allowed = (isinstance(data, dict) and error.get("code") == "task_not_startable"
                       and all(receipt.get(key) in (None, previous[field])
                               for key, field in (("runId", "run_id"), ("taskId", "task_id")))
                       and _preflight_refusal_classification(receipt, {**previous, "runtime": observed_runtime},
                               request_uuid=previous["request_uuid"]) == "authoritative"
                       and data.get("status") in ("blocked", "ready") and data.get("reason") in (None, "")
                       and all(data.get(key) in (None, proof[field]) for key, field in
                               (("runId", "run_id"), ("taskId", "task_id"), ("retryOf", "retry_of_dispatch_id")))
                       and (invocation_bound or all(data.get(key) == proof[field] for key, field in
                               (("runId", "run_id"), ("taskId", "task_id"), ("retryOf", "retry_of_dispatch_id")))))
    elif proof.get("kind") == "pre_start":
        allowed = _pre_start_proven(proof, previous)
    if not allowed:
        raise PodError("retry_no_start_unverified", "No recoverable no-start prerequisite is proved; circuit-breaker, permission, safety, authentication and unknown refusals stay held with their original next action")
    return previous


def _defer_pre_start(project: Path, objective: str, owner: str, admission_id: str,
                     runtime: str, exc: PodError) -> None:
    # This handler surrounds only the parent's read/recheck, before _start_observed.
    # A crash before this durable proof leaves the reservation unresolved as before.
    error = {"code": exc.code, "message": str(exc), "detail": exc.detail}
    proof = _save_no_start(project, objective, admission_id, kind="pre_start",
                           runtime=runtime, stage="retry_parent_recheck", effect="not_called", error=error,
                           observed_at=datetime.now(timezone.utc).isoformat())
    # Record-only R25 evidence survives contact loss without touching the reservation,
    # even if another call has rebound it. The immutable index makes later authorized
    # recovery possible without scanning evidence or inferring no-start from absence.
    path = objective_root(project, objective) / "no-start" / "pending" / (admission_id + ".json")
    value = {"no_start": proof}
    if path.exists() or path.is_symlink():
        if bounded_json(path, limit=START_OBSERVATION_LIMIT) != value:
            raise PodError("native_evidence_unavailable", "Pre-start evidence index changed")
    else:
        atomic_json(path, value, limit=START_OBSERVATION_LIMIT, compact=True)
    _recover_pre_start(project, objective, owner, admission_id, runtime)


def _recover_pre_start(project: Path, objective: str, owner: str, admission_id: str,
                        runtime: str) -> dict | None:
    """Classify persisted caught no-start proof through the normal authority fence."""
    path = objective_root(project, objective) / "no-start" / "pending" / (admission_id + ".json")
    if not path.exists() and not path.is_symlink():
        return None
    try:
        index = bounded_json(path, limit=START_OBSERVATION_LIMIT)
        if not isinstance(index, dict) or set(index) != {"no_start"}:
            raise ValueError("invalid pre-start index")
    except (OSError, ValueError, PodError) as exc:
        raise PodError("native_evidence_unavailable", "Pre-start evidence index is unreadable; retain the reservation") from exc
    state = read(project, objective)
    admission = state["admissions"][admission_id]
    _retry_parent_link(state, admission)
    proof = _no_start_proof(project, state, admission, index["no_start"])
    if (admission.get("retry_of") is None or not _pre_start_proven(proof, admission)
            or admission["state"] not in ("reserved", "unresolved")
            or admission["native_binding"] is not None or _retry_conflict_code(admission)
            or _known_request_conflict(admission)):
        raise PodError("retry_no_start_unverified", "Pre-start proof no longer joins an untouched recoverable retry; retain its exact attempt")

    def apply(row: dict) -> None:
        if row != admission:
            raise PodError("admission_conflict", "Pre-start reservation changed before classification")
        row["state"] = "deferred"
        row["error"] = {"code": proof["error"]["code"], "detail": _error_summary(proof["error"])}
        row["recovery"] = {**row["recovery"], "no_start": index["no_start"],
                           "next": "Inspect the parent worker-show and correct the read/exit prerequisite; then explicitly admit with the same retry_of and retry_after=" + admission_id}
    return update_admission(project, objective, owner=owner, admission_id=admission_id,
                            update=apply, expected_runtime=runtime)


def _defer_refusal(project: Path, objective: str, *, owner: str, admission_id: str,
                   receipt: dict, request_uuid: str | None) -> dict:
    error = receipt["error"]
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    next_step = data.get("nextSteps")
    if not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 2048:
        next_step = "explicit new policy admission after correcting the refused preflight condition"
    admission = read(project, objective)["admissions"][admission_id]
    proof = (_save_no_start(project, objective, admission_id, kind="native_refusal",
                            runtime=receipt["runtime"], receipt=receipt)
             if admission.get("retry_of") is not None else None)

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
        if proof is not None:
            row["recovery"]["no_start"] = proof
    return update_admission(project, objective, owner=owner,
                            admission_id=admission_id, update=apply, expected_runtime=receipt["runtime"])


def _current_authority(project: Path, objective: str, admission: dict, *, issue_port=None) -> None:
    """Pending replay retains its original route; check only continuing authority/core."""
    state = read(project, objective)
    checkpoint_value = state.get("checkpoint") if isinstance(state, dict) else None
    expected = admission.get("recovery", {}).get("checkpoint_binding")
    keys = ("candidate", "criteria", "plan_revision", "policy_revision", "objective_source", "worktree")
    if not isinstance(expected, dict) or not isinstance(checkpoint_value, dict):
        raise PodError("checkpoint_binding_changed", "Checkpoint binding is unavailable")
    from .config import effective
    effective_revision = effective(project)["revision"]
    if effective_revision != checkpoint_value.get("policy_revision"):
        raise policy_mismatch("Governor policy changed before replay",
                              checkpoint=checkpoint_value.get("policy_revision"),
                              packet=expected.get("policy_revision"), effective=effective_revision, replay=True)
    if any(expected.get(key) != checkpoint_value.get(key) for key in keys):
        raise PodError("checkpoint_binding_changed", "Checkpoint semantics changed before replay")
    _check_objective_source(project, expected.get("objective_source"), issue_port=issue_port)
    _check_worktree_binding(project, expected.get("worktree"))


def _adopt_unique(project: Path, objective: str, *, owner: str, admission_id: str,
                  port: NativePort, request_uuid: str | None) -> dict:
    state = read(project, objective)
    admission = state["admissions"][admission_id]
    try:
        _retry_parent_link(state, admission)
    except PodError as exc:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code=exc.code, detail=str(exc))
    try:
        rows = port.find_worker(run=admission["run_id"], task=admission["task_id"])
    except PodError as exc:
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code="native_readback_unavailable",
                     detail=f"Exact Run/Task readback failed: {exc.code}")
    if not isinstance(rows, list):
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code="native_readback_unavailable",
                     detail="Exact Run/Task inventory is incomplete")
    candidates = []
    errors = []
    own_errors = []
    observed_dispatch = admission["recovery"].get("observed_dispatch_id")
    known = {row["native_binding"]["dispatchId"] for key, row in state["admissions"].items()
             if key != admission_id and binding_valid(row.get("native_binding"))}
    for row in rows:
        dispatch = row.get("dispatchId") if isinstance(row, dict) else None
        if not isinstance(dispatch, str):
            if admission.get("retry_of") is not None:
                errors.append("native_identity_unverified")
            continue
        if admission.get("retry_of") is not None and dispatch in known:
            continue
        try:
            binding = _binding_from_show(port.show_worker(dispatch), admission, dispatch)
            _check_retry_placement(port, binding, admission)
            candidates.append(dispatch)
        except PodError as exc:
            errors.append(exc.code)
            if dispatch == observed_dispatch:
                own_errors.append(exc.code)
    if admission.get("retry_of") is not None and errors:
        code = next((code for code in ("native_retry_parent_mismatch", "native_retry_placement_mismatch",
                                      "native_retry_placement_unverified") if code in own_errors),
                    "native_attempt_ambiguous")
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code=code,
                     detail={"matching": len(candidates), "rejected": errors})
    if len(candidates) != 1:
        reason = "native_attempt_absent" if not candidates else "native_attempt_ambiguous"
        return _hold(project, objective, owner=owner, admission_id=admission_id,
                     request_uuid=request_uuid, code=reason,
                     detail={"matching": len(candidates), "rejected": errors})
    receipt = {"runtime": admission["runtime"], "runId": admission["run_id"],
               "taskId": admission["task_id"], "dispatchId": candidates[0]}
    return _bind(project, objective, owner=owner, admission_id=admission_id,
                 receipt=receipt, port=port, request_uuid=request_uuid)


def _recover_completed(project: Path, objective: str, owner: str, admission_id: str,
                       admission: dict, request_uuid: str, receipt: dict | None,
                       native_port: NativePort) -> dict:
    if receipt is None:
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=request_uuid, code="native_receipt_missing", detail="completed request")
    else:
        completed = {"runtime": admission["runtime"], **receipt}
        if admission.get("retry_of") is not None:
            _observe_start(project, objective, owner=owner, admission_id=admission_id,
                           observation={"request_show_receipt": receipt}, retry_request=request_uuid,
                           reference=completed)
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


def _recover_pending(project: Path, objective: str, owner: str, admission_id: str,
                     admission: dict, state: dict, request_uuid: str, worktree: str,
                     native_port: NativePort, issue_port) -> dict:
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
    try:
        parent = _retry_parent_current(state, admission, native_port)
    except PodError as exc:
        row = _hold(project, objective, owner=owner, admission_id=admission_id,
                    request_uuid=request_uuid, code=exc.code, detail=str(exc))
        return {"status": row["state"], "admission": row, "action": "hold"}
    if admission["reuse_of"]:
        previous = state["admissions"].get(admission["reuse_of"])
        terminal = previous["native_binding"]["terminalHandle"] if previous else None
        if not isinstance(terminal, str) or not terminal:
            raise PodError("reuse_unavailable", "Pending terminal reuse lost its binding")
    receipt = _start_observed(project, objective, admission_id=admission_id, port=native_port,
                                       run=admission["run_id"], task=admission["task_id"],
                                       owner=owner, route=admission["request"], worktree=worktree,
                                       retry_request=request_uuid, terminal=terminal,
                                       **({"retry_of": parent} if parent is not None else {}))
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


def _continuity_for_recovery(project: Path, objective: str, owner: str, admission: dict,
                             authority: dict, native_port: NativePort) -> dict:
    """Classify a runtime change before recovery's own runtime check; the rebind recovers nothing."""
    if authority.get("runtime") == admission["runtime"]:
        return admission
    from .ledger import continuity_locked
    if continuity_locked(project, objective, owner=owner, port=native_port, current=authority) is None:
        return admission
    return read(project, objective)["admissions"][admission["admission_id"]]


def recover_admission(project: Path, objective: str, *, owner: str, admission_id: str,
                      worktree: str, port: NativePort | None = None, issue_port=None,
                      abandoned_reason: str | None = None) -> dict:
    """Recover the same admission; pending replay never rechecks model preferences."""
    native_port = port or OrcaPort(project)
    state = read(project, objective)
    from .handoff import refuse_nonowner
    refuse_nonowner(project, objective, state, owner, port=native_port)
    admission = state["admissions"].get(admission_id) if state else None
    if not isinstance(admission, dict) or admission.get("owner") != owner:
        raise PodError("unknown_admission", "No owned admission identity")
    if worktree != admission["worktree"]:
        raise PodError("admission_conflict", "Recovery changed original worktree")
    _start_observations(project, objective, admission)
    if _retry_conflict_code(admission) is not None:
        return {"status": admission["state"], "admission": admission, "action": "hold"}
    if abandoned_reason is not None:
        from .abandoned import recover
        try:
            return recover(project, objective, owner=owner, admission=admission,
                           reason=abandoned_reason, port=native_port)
        except PodError as exc:
            if admission.get("retry_of") is not None and exc.code in (
                    "native_retry_parent_mismatch", "native_retry_parent_unverified",
                    "native_retry_placement_mismatch", "native_retry_placement_unverified"):
                row = _hold(project, objective, owner=owner, admission_id=admission_id,
                            request_uuid=admission["request_uuid"], code=exc.code, detail=str(exc))
                return {"status": row["state"], "admission": row, "action": "hold"}
            raise
    if admission["state"] in ("bound", "closed", "deferred"):
        action = "defer" if admission["state"] == "deferred" else "reuse"
        return {"status": admission["state"], "admission": admission, "action": action}
    request_uuid = admission.get("request_uuid")
    if request_uuid is None:
        if _known_request_conflict(admission):
            return {"status": admission["state"], "admission": admission, "action": "hold"}
        authority = native_port.read_native(owner, authority_runs=(admission["run_id"],))
        admission = _continuity_for_recovery(project, objective, owner, admission, authority, native_port)
        if (authority.get("authoritative") is not True or authority.get("owner") != owner
                or authority.get("runtime") != admission["runtime"]):
            raise PodError("native_authority_unverified", "No-UUID readback lost Run ownership")
        row = _recover_pre_start(project, objective, owner, admission_id, admission["runtime"])
        if row is not None:
            return {"status": row["state"], "admission": row, "action": "defer"}
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
    admission = _continuity_for_recovery(project, objective, owner, admission, authority, native_port)
    state = read(project, objective)
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
        return _recover_completed(project, objective, owner, admission_id, admission,
                                  request_uuid, receipt, native_port)
    if status == "pending":
        return _recover_pending(project, objective, owner, admission_id, admission, state,
                                request_uuid, worktree, native_port, issue_port)
    if _known_request_conflict(admission):
        return {"status": admission["state"], "admission": admission, "action": "hold"}
    row = _adopt_unique(project, objective, owner=owner, admission_id=admission_id,
                        port=native_port, request_uuid=request_uuid)
    return {"status": row["state"], "admission": row, "action": "inspect_after_absent"}


def _launch_reserved(project: Path, objective: str, owner: str, admission_id: str,
                     admission: dict, native_port: NativePort, run: str, task: str,
                     proposed: dict, worktree: str, reuse_of: str | None) -> dict:
    terminal = None
    if reuse_of:
        prior = read(project, objective)["admissions"][reuse_of]
        terminal = prior["native_binding"]["terminalHandle"]
    parent = None
    if admission.get("retry_of") is not None:
        try:
            parent = _retry_parent_current(read(project, objective), admission, native_port)
        except PodError as exc:
            _defer_pre_start(project, objective, owner, admission_id, admission["runtime"], exc)
            raise
    try:
        receipt = _start_observed(project, objective, admission_id=admission_id, port=native_port,
                                           run=run, task=task, owner=owner, route=proposed,
                                           worktree=worktree, terminal=terminal,
                                           **({"retry_of": parent} if parent is not None else {}))
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


def guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                  plan_revision: str, frozen_packet: dict, worktree: str = "current",
                  reuse_of: str | None = None, port: NativePort | None = None,
                  issue_port=None, accompanying: dict | None = None,
                  retry_of: str | None = None, retry_after: str | None = None,
                  abandoned_reason: str | None = None) -> dict:
    """One admission, then the delivery step: one safe automatic retry or the receiver's diagnosis (R2)."""
    native_port = port or OrcaPort(project)
    arguments = dict(project=project, objective=objective, owner=owner, run=run, task=task,
                     plan_revision=plan_revision, frozen_packet=frozen_packet, worktree=worktree,
                     port=native_port, issue_port=issue_port)
    result = _guarded_start(**arguments, reuse_of=reuse_of, accompanying=accompanying, retry_of=retry_of,
                            retry_after=retry_after, abandoned_reason=abandoned_reason)
    row = result.get("admission")
    if abandoned_reason is not None or not isinstance(row, dict) or row.get("state") != "bound":
        return result
    from .delivery import follow_up
    delivery = follow_up(project, objective, admission=row, port=native_port, state=read(project, objective),
                         relaunch=lambda parent: guarded_start(**arguments, retry_of=parent))
    return {**result, "delivery": delivery} if delivery is not None else result


def _guarded_start(project: Path, objective: str, *, owner: str, run: str, task: str,
                   plan_revision: str, frozen_packet: dict, worktree: str = "current",
                   reuse_of: str | None = None, port: NativePort | None = None,
                   issue_port=None, accompanying: dict | None = None,
                   retry_of: str | None = None, retry_after: str | None = None,
                   abandoned_reason: str | None = None) -> dict:
    """Packet, recovery, issue, placement, serialized boundary, then one native start."""
    from .issue_revision import refuse_alternate
    refuse_alternate(accompanying)
    from .records import packet
    bounded_text(owner, name="owner")
    native_port = port or OrcaPort(project)
    validated = packet(frozen_packet["body"] if "body" in frozen_packet else frozen_packet)
    if "packet_id" in frozen_packet and frozen_packet["packet_id"] != validated["packet_id"]:
        raise PodError("invalid_packet", "Frozen packet identity changed")
    body = validated["body"]
    if body["objective"] != objective:
        raise PodError("packet_mismatch", "Packet objective differs from admission")
    existing_state = read(project, objective)
    from .handoff import refuse_nonowner
    refuse_nonowner(project, objective, existing_state, owner, port=native_port)
    from .ledger import _run_references
    if not isinstance(existing_state, dict) or existing_state.get("owner") != owner:
        raise PodError("native_authority_unverified", "Objective has no owned checkpoint")
    if run not in _run_references(existing_state):
        raise PodError("native_authority_unverified", "Admission Run is not an exact objective reference")
    if retry_of is not None and reuse_of is not None:
        raise PodError("retry_reuse_conflict", "retry_of requires a fresh terminal; omit reuse_of")
    parent = (retry_parent(existing_state, objective=objective, owner=owner, run_id=run,
                           task_id=task, retry_of=retry_of) if retry_of is not None else None)
    if retry_after is not None:
        _retry_after(project, existing_state, parent, retry_after)
    admission_id = admission_identity(objective=objective, run_id=run, task_id=task,
                                      retry_of_dispatch_id=parent["native_binding"]["dispatchId"] if parent else None,
                                      retry_after=retry_after)
    if isinstance(existing_state, dict) and admission_id in existing_state["admissions"]:
        existing = existing_state["admissions"][admission_id]
        if (existing["packet_id"] != validated["packet_id"]
                or existing["plan_revision"] != plan_revision or existing["worktree"] != worktree
                or existing.get("retry_of") != retry_of or existing.get("retry_after") != retry_after):
            code = "unresolved_prior_attempt" if existing["state"] in ("reserved", "unresolved") else "admission_conflict"
            raise PodError(code, "A Task's admission cannot be replaced by another packet or placement")
        recovered = recover_admission(project, objective, owner=owner, admission_id=admission_id,
                                      worktree=worktree, port=native_port, issue_port=issue_port,
                                      abandoned_reason=abandoned_reason)
        return {**recovered, "decision": recovered["admission"]["route_decision"]}
    if abandoned_reason is not None:
        raise PodError("unknown_admission", "Abandoned accounting recovers an existing admission; it never starts work")
    # A tick rebind changes only objective_source. Name that stale packet before
    # rechecking its old source; every other pre-existing refusal keeps its order.
    if any(row["phase"] != "completed" for row in existing_state.get("pod_ticks", {}).get("requests", {}).values()):
        raise PodError("pod_tick_pending", "Recover the pending pod internal pod-tick request before admission")
    checkpoint_value = existing_state.get("checkpoint") or {}
    if (body.get("objective_source") != checkpoint_value.get("objective_source")
            and existing_state.get("pod_ticks", {}).get("last_rebind") == digest(checkpoint_value.get("objective_source"))
            and (body.get("objective_source") is None or all(
                body["objective_source"].get(key) == checkpoint_value["objective_source"].get(key)
                for key in ("repository", "number", "locator", "amendments")))):
        differing = [key for key in ("candidate", "criteria", "objective_source", "worktree")
                     if body.get(key) != checkpoint_value.get(key)]
        if body.get("plan_revision") != plan_revision:
            differing.append("plan_revision")
        action = "read the named fields from pod internal map; freeze the corrected packet with pod internal packet and retry"
        raise PodError("packet_plan_mismatch", "Packet differs in fields: " + ", ".join(differing),
                       {"fields": differing, "reads": {key: "pod internal map: " + key for key in differing},
                        "next_action": action})
    _check_objective_source(project, body.get("objective_source"), issue_port=issue_port)
    from .github import repository_context
    current_worktree = repository_context(project, fresh=True)  # Bound right before the start.
    _check_worktree_binding(project, body.get("worktree"), current_worktree)
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
                            assignments=_bound_assignments(state, include_closed=bool(reuse_of or retry_of))),
                        run_id=run, task_id=task, plan_revision=plan_revision,
                        packet_id=validated["packet_id"], worktree=worktree,
                        frozen_packet=validated, expected_runtime=capability["runtime"],
                        placement_binding=placement_binding, reuse_of=reuse_of, retry_of=retry_of,
                        retry_after=retry_after,
                        accompanying=accompanying, continuity_port=native_port,
                        placement_naming=None if placement is None else
                        {"mode": placement.get("display_name_mode"), "main": placement.get("main")})
    if admission["existing"]:
        recovered = recover_admission(project, objective, owner=owner, admission_id=admission_id,
                                      worktree=worktree, port=native_port, issue_port=issue_port)
        return {**recovered, "decision": recovered["admission"]["route_decision"]}
    return _launch_reserved(project, objective, owner, admission_id, admission, native_port,
                            run, task, proposed, worktree, reuse_of)

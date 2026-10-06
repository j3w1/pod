"""Private bounded helper entry. It installs no public command."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .errors import FieldRefusal, PodError
from .records import (acceptance, integration_observation, packet, report, source_identity,
                      verify_sources)
from .util import bounded_json, bounded_stdin_json, exact, route_flags
from .context import execution_brief


def _governor_projection(project: Path, objective: str, owner: str, *, mutating: bool,
                         version_exempt: bool = False) -> dict:
    """Join Governor work to stable current-Run authority and objective assignments."""
    from .ledger import continuity_locked, logical_projection, read
    from .operations import OrcaPort

    state = read(project, objective)
    if mutating and (state is None or state.get("owner") != owner):
        raise PodError("native_authority_unverified",
                       "Caller is not the recorded owner; Governor mutation does not own this Pod objective context")
    if mutating:
        from .ledger import _require_open
        _require_open(state)
    if mutating and not version_exempt:
        from .bundle import require_current_identity
        require_current_identity(state.get("checkpoint"))
    port = OrcaPort(project)
    authority_runs, runtimes, assignments = _governor_references(state)

    def observe() -> dict:
        return port.read_native(owner, authority_runs=tuple(sorted(authority_runs)) if mutating else (),
                                assignments=tuple(assignments))
    try:
        native = observe()
    except PodError:
        # A mutating Governor path classifies a runtime change before its own runtime check (A2).
        if not mutating or continuity_locked(project, objective, owner=owner, port=port) is None:
            raise
        state = read(project, objective)
        authority_runs, runtimes, assignments = _governor_references(state)
        native = observe()
    else:
        if (mutating and runtimes and native.get("runtime") not in runtimes
                and continuity_locked(project, objective, owner=owner, port=port, current=native) is not None):
            state = read(project, objective)
            authority_runs, runtimes, assignments = _governor_references(state)
            native = observe()
    projection = logical_projection(project, native, objective=objective)
    if not mutating:
        return projection
    if (native.get("authoritative") is not True or native.get("owner") != owner
            or projection.get("authoritative") is not True
            or projection.get("owner") != owner
            or projection.get("runtime") != native.get("runtime")):
        from .ledger import authority_message
        raise PodError("native_authority_unverified", authority_message(state, native, owner))
    if not authority_runs or any(runtime != native["runtime"] for runtime in runtimes):
        raise PodError("native_authority_unverified",
                       "Governor evidence belongs to another Orca Run or runtime")
    return projection


def _governor_references(state: dict | None) -> tuple[set[str], set[object], tuple[dict, ...]]:
    """Run references, recorded runtimes and bound assignments the Governor joins."""
    from .ledger import bound_assignments
    authority_runs: set[str] = set()
    runtimes: set[object] = set()
    if state is not None:
        for row in state.get("admissions", {}).values():
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("run_id"), str):
                authority_runs.add(row["run_id"])
            runtimes.add(row.get("runtime"))
        checkpoint_value = state.get("checkpoint")
        refs = checkpoint_value.get("native_refs", []) if isinstance(checkpoint_value, dict) else []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            run_id = ref.get("runId")
            ref_runtime = ref.get("runtime")
            if (isinstance(run_id, str) and run_id
                    and isinstance(ref_runtime, str) and ref_runtime):
                authority_runs.add(run_id)
                runtimes.add(ref_runtime)
    return authority_runs, runtimes, bound_assignments(state)


def _op_brief(request: dict) -> dict:
    exact(request, {"criteria", "coverage", "map", "project", "candidate", "verification"}, {"criteria", "coverage"}, name="request")
    brief = execution_brief(request["criteria"], request["coverage"])
    # An execution brief engages the kernel: it always carries a validated draft map.
    # Validation is read-only (Git reads at the declared base), so Plan Mode can use it.
    from .obligations import brief_map
    declared = request.get("map", {}).get("governance") if isinstance(request.get("map"), dict) else None
    base_ref = declared.get("base_ref") if isinstance(declared, dict) else None
    if "project" in request:
        from .ledger import governance_observation
        from .governance import user_direct_revision
        authority = request.get("map", {}).get("revision_authority") if isinstance(request.get("map"), dict) else None
        observed = governance_observation(
            Path(request["project"]), base_ref,
            user_direct=user_direct_revision(authority))
    else:
        observed = {"status": "no_repository"} if base_ref is None else {"status": "unavailable"}
    facts = {"governance": observed, "admissions": {}, "outstanding": [],
             "ceiling": 0, "delegation": "unknown", "constraints": [],
             "candidate": request.get("candidate"), "verification": request.get("verification")}
    if "project" in request:
        from .ledger import kernel_context, validate_verification
        from .gitio import require_candidate
        project = Path(request["project"])
        if "candidate" in request:
            require_candidate(project, request["candidate"])
        verification = validate_verification(request["verification"]) if "verification" in request else None
        facts = kernel_context(project, "brief", {"admissions": {}}, None,
                               candidate=request.get("candidate"), criteria=request["criteria"],
                               governance=observed, verification=verification)
    brief["map"] = brief_map(request["criteria"], request.get("map"), facts)
    return brief


def _op_packet(request: dict) -> dict:
    return packet(request)


def _op_report(request: dict) -> dict:
    exact(request, {"report", "packet", "project", "objective", "admission_id", "map", "triage",
                    "proposals", "result_commit"},
          {"report", "packet", "project", "objective", "admission_id"}, name="request")
    if not isinstance(request["packet"], dict):
        raise PodError("invalid_packet", "Report needs a frozen packet")
    from .ledger import read
    state = read(Path(request["project"]), request["objective"])
    admission = state.get("admissions", {}).get(request["admission_id"]) if state else None
    if (not admission or admission.get("state") != "bound" or not admission.get("native_binding")
            or admission.get("packet_id") != request["packet"].get("packet_id")):
        raise PodError("report_attempt_unverified", "No exact native admission binding")
    from .operations import OrcaPort, _binding_from_show, _reuse_effective_evidence
    port = OrcaPort(Path(request["project"]))
    binding = admission["native_binding"]
    shown = port.show_worker(binding["dispatchId"])
    if shown.get("runtime") != admission["runtime"]:
        # A report read classifies a runtime change before its own runtime check (A2).
        from .ledger import continuity_locked
        if continuity_locked(Path(request["project"]), request["objective"], owner=admission["owner"],
                             port=port) is not None:
            state = read(Path(request["project"]), request["objective"])
            admission = state["admissions"][request["admission_id"]]
            binding = admission["native_binding"]
            shown = port.show_worker(binding["dispatchId"])
    fresh = _binding_from_show(shown, admission, binding["dispatchId"])
    if fresh != binding:
        raise PodError("report_attempt_unverified", "Fresh native identity differs from admission")
    effective, mismatch, inherited = _reuse_effective_evidence(
        {}, shown, admission, state["admissions"].get(admission.get("reuse_of")))
    if (mismatch or any(admission["route_decision"].get("effective", {}).get(key) not in
                        (effective[key], "unknown") for key in ("agent", "model", "effort"))):
        from .ledger import update_admission
        update_admission(Path(request["project"]), request["objective"],
                         owner=admission["owner"], admission_id=admission["admission_id"],
                         update=lambda row: row["route_decision"].update(route_mismatch=True),
                         expected_runtime=admission["runtime"])
        raise PodError("route_mismatch", "Native effective route changed after start")
    if (admission["route_decision"].get("effective_unknown")
            and all(effective[key] != "unknown" for key in ("agent", "model", "effort"))):
        from .ledger import update_admission
        def refresh(row: dict) -> None:
            row["route_decision"]["effective"] = effective
            row["route_decision"]["effective_unknown"] = False
            row["effective_evidence"]["effective"] = effective
            if inherited is not None:
                row["effective_evidence"]["inherited"] = inherited
        update_admission(Path(request["project"]), request["objective"],
                         owner=admission["owner"], admission_id=admission["admission_id"],
                         update=refresh, expected_runtime=admission["runtime"])
    validated = report(request["report"], request["packet"],
                       {"runtime": admission["runtime"], **{key: binding[key] for key in
                        ("runId", "taskId", "dispatchId", "workerId")}})
    # Report consumption is a map write: Git reads the result, triage and proposals land
    # atomically, and nothing in the report itself changes an obligation.
    from .ledger import consume_report
    consumed = consume_report(Path(request["project"]), request["objective"], owner=admission["owner"],
                              admission_id=admission["admission_id"], observation=validated,
                              accompanying=request.get("map"), findings=request.get("triage"),
                              proposals=request.get("proposals"),
                              result_commit=request.get("result_commit"))
    return {**validated, **consumed}


def _op_source(request: dict) -> dict:
    exact(request, {"project", "path"}, {"project", "path"}, name="request")
    return source_identity(Path(request["project"]), request["path"])


def _op_verify_sources(request: dict) -> dict:
    exact(request, {"project", "sources"}, {"project", "sources"}, name="request")
    verify_sources(Path(request["project"]), request["sources"])
    return {"status": "current"}


def _op_issue_intake(request: dict) -> dict:
    exact(request, {"project", "locator", "amendments"}, {"project", "locator"}, name="request")
    from .github import issue_intake
    return issue_intake(Path(request["project"]), request["locator"],
                        amendments=request.get("amendments"))


def _op_issue_recheck(request: dict) -> dict:
    exact(request, {"project", "source"}, {"project", "source"}, name="request")
    from .github import issue_recheck
    return issue_recheck(Path(request["project"]), request["source"])


def _op_project_context(request: dict) -> dict:
    exact(request, {"project"}, {"project"}, name="request")
    from .github import repository_context
    return repository_context(Path(request["project"]))


def _op_acceptance(request: dict) -> dict:
    exact(request, {"criteria", "evidence_rows", "candidate", "policy_revision",
                    "sources", "dependencies", "environment", "review_required",
                    "hosted_required", "owner_acceptance", "project", "objective_source",
                    "objective"},
          {"criteria", "evidence_rows", "candidate", "policy_revision",
           "sources", "dependencies", "environment", "review_required",
           "hosted_required"}, name="request")
    arguments = {key: value for key, value in request.items() if key not in ("project", "objective")}
    view = None
    if "objective" in request:
        if "project" not in request:
            raise PodError("invalid_request", "Objective-bound acceptance requires the checkout")
        from .ledger import kernel_view
        view = kernel_view(Path(request["project"]), request["objective"], candidate=request["candidate"])
        arguments["label"] = view["label"]
        arguments["route_holds"] = [
            {"admission": key, "reason": "route_mismatch" if flags[0]
             else "effective_unknown"}
            for key, row in view["state"]["admissions"].items()
            if row.get("state") in ("bound", "closed")
            for flags in [route_flags(row)]
            if flags[0] or flags[1]
        ] if view["state"] is not None else []
    objective_source = arguments.pop("objective_source", None)
    if objective_source is not None:
        if "project" not in request:
            raise PodError("invalid_request", "Issue-bound acceptance requires the checkout")
        from .github import issue_recheck
        current = issue_recheck(Path(request["project"]), objective_source)
        if current["status"] != "current":
            raise PodError("issue_reconciliation_required",
                           "Execution Spec issue changed before final verification")
    # Whether a candidate is merged or released is read from Git here. A caller cannot
    # hand in that answer, because it is the one the final report rests on.
    if "project" in request:
        arguments["integration"] = integration_observation(Path(request["project"]),
                                                           request["candidate"])
    result = acceptance(**arguments)
    if view is not None:
        result["report"] = view["report"]
        result["native_settlement"] = view["settlement"]
    return result


def _op_integration_observe(request: dict) -> dict:
    exact(request, {"project", "candidate", "base_ref", "objective"}, {"project", "candidate"}, name="request")
    from .governance import canonical_bound_ref
    from .ledger import map_of, read
    project = Path(request["project"])
    state = read(project, request["objective"]) if "objective" in request else None
    bound = (map_of(state) or {}).get("governance", {}).get("base_ref") if state else None
    base_ref = canonical_bound_ref(project, bound, request.get("base_ref"))
    return integration_observation(project, request["candidate"], base_ref=base_ref)


def _op_checkpoint(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "value"},
          {"project", "objective", "owner", "value"}, name="request")
    from .ledger import checkpoint
    from .orca import contract
    snapshot = contract()
    if snapshot.get("status") != "observed":
        raise PodError("orca_unavailable", "A checkpoint needs a current native runtime read")
    delegation = ("available" if snapshot.get("capabilities", {}).get("launch_preferences_v1") is True
                  else "unavailable")
    return checkpoint(Path(request["project"]), request["objective"], owner=request["owner"],
                      value=request["value"], native={"runtime": snapshot["runtime"],
                                                      "delegation": delegation})


def _op_constraint(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "action", "value", "id"},
          {"project", "objective", "owner", "action"}, name="request")
    from .ledger import constraints_update
    return constraints_update(Path(request["project"]), request["objective"],
                              owner=request["owner"], action=request["action"],
                              value=request.get("value"), constraint_id=request.get("id"))


def _op_route_failure(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "admission_id", "kind",
                    "source", "retry_after", "clear", "cleared_by"},
          {"project", "objective", "owner", "admission_id", "kind", "source"}, name="request")
    from .ledger import route_failure
    return route_failure(Path(request["project"]), request["objective"],
                         owner=request["owner"], admission_id=request["admission_id"],
                         kind=request["kind"], source=request["source"],
                         retry_after=request.get("retry_after"), clear=request.get("clear", False),
                         cleared_by=request.get("cleared_by"))


def _op_governor(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "action", "exception", "pull_request"},
          {"project", "objective", "owner", "action"}, name="request")
    from .governor import decide
    project = Path(request["project"])
    projection = _governor_projection(project, request["objective"], request["owner"],
                                      mutating=True)
    return decide(project, request["objective"], owner=request["owner"],
                  action=request["action"], exception=request.get("exception"),
                  native_projection=projection, pull_request=request.get("pull_request"))


def _op_governor_outcome(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "record_id", "outcome", "provider", "evidence", "detail"},
          {"project", "objective", "owner", "record_id", "outcome"}, name="request")
    from .governor import record_outcome
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"],
                         mutating=True, version_exempt=True)
    return record_outcome(project, request["objective"], owner=request["owner"],
                          record_id=request["record_id"], outcome=request["outcome"],
                          provider=request.get("provider"), evidence=request.get("evidence"),
                          detail=request.get("detail"))


def _op_governor_prepare(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "unit", "branch", "tasks", "base_ref",
                    "workflows", "verification", "toolchain", "environment"},
          {"project", "objective", "owner", "unit"}, name="request")
    from .governor import observe_candidate, prepare_candidate
    from .governance import canonical_bound_ref
    from .ledger import map_of, read
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"], mutating=True)
    state = read(project, request["objective"])
    bound = (map_of(state) or {}).get("governance", {}).get("base_ref") if state else None
    base_ref = canonical_bound_ref(project, bound, request.get("base_ref"), branch=request.get("branch"))
    # Commit and tree are read from Git here. A caller cannot hand in the candidate it
    # wants validated, because that identity is what every later reuse rests on.
    observation = observe_candidate(project, base_ref=base_ref,
                                    workflows=request.get("workflows"),
                                    verification=request.get("verification"),
                                    toolchain=request.get("toolchain"),
                                    environment=request.get("environment"))
    return prepare_candidate(project, request["objective"], owner=request["owner"], unit=request["unit"],
                             observation=observation, branch=request.get("branch"),
                             tasks=request.get("tasks"))


def _op_governor_preflight(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "unit", "candidate", "check", "status", "report"},
          {"project", "objective", "owner", "unit", "candidate", "check", "status"}, name="request")
    from .governor import record_preflight
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"], mutating=True)
    return record_preflight(project, request["objective"], owner=request["owner"],
                            unit=request["unit"], candidate=request["candidate"], check=request["check"],
                            status=request["status"], report=request.get("report"))


def _op_governor_classify(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "record_id", "classification"},
          {"project", "objective", "owner", "record_id", "classification"}, name="request")
    from .governor import classify_failure
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"], mutating=True)
    return classify_failure(project, request["objective"], owner=request["owner"],
                            record_id=request["record_id"], classification=request["classification"])


def _op_governor_correct(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "unit", "correction", "diagnosis"},
          {"project", "objective", "owner", "unit", "correction"}, name="request")
    from .governor import record_correction
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"], mutating=True)
    return record_correction(project, request["objective"], owner=request["owner"],
                             unit=request["unit"], correction=request["correction"],
                             diagnosis=request.get("diagnosis"))


def _op_governor_execute(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "action", "exception", "pull_request"},
          {"project", "objective", "owner", "action"}, name="request")
    from .governor import execute
    project = Path(request["project"])
    projection = _governor_projection(project, request["objective"], request["owner"],
                                      mutating=True)
    # The production port is the installed gh and git; request JSON cannot supply one.
    return execute(project, request["objective"], owner=request["owner"],
                   action=request["action"], exception=request.get("exception"),
                   pull_request=request.get("pull_request"), native_projection=projection)


def _op_governor_reconcile(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "record_id"},
          {"project", "objective", "owner", "record_id"}, name="request")
    from .governor import reconcile
    project = Path(request["project"])
    _governor_projection(project, request["objective"], request["owner"],
                         mutating=True, version_exempt=True)
    return reconcile(project, request["objective"], owner=request["owner"],
                     record_id=request["record_id"])


def _op_governor_status(request: dict) -> dict:
    exact(request, {"project", "objective", "owner"}, {"project", "objective", "owner"}, name="request")
    from .governor import status
    project = Path(request["project"])
    projection = _governor_projection(project, request["objective"], request["owner"],
                                      mutating=False)
    return status(project, request["objective"], native_projection=projection)


def _op_admission(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "run", "task",
                    "plan_revision", "packet", "worktree", "reuse_of", "map"},
          {"project", "objective", "owner", "run", "task",
           "plan_revision", "packet"}, name="request")
    from .operations import guarded_start
    # The production port verifies installed controls itself; request JSON
    # can supply assessment snapshots but cannot assert native assurance.
    return guarded_start(Path(request["project"]), request["objective"],
                         owner=request["owner"], run=request["run"], task=request["task"],
                         plan_revision=request["plan_revision"],
                         frozen_packet=request["packet"],
                         worktree=request.get("worktree", "current"),
                         reuse_of=request.get("reuse_of"), accompanying=request.get("map"))


def _op_runtime_continuity(request: dict) -> dict:
    exact(request, {"project", "objective", "owner", "decision"},
          {"project", "objective", "owner", "decision"}, name="request")
    from .ledger import owner_continuity
    from .operations import OrcaPort
    project = Path(request["project"])
    return owner_continuity(project, request["objective"], owner=request["owner"],
                            decision=request["decision"], port=OrcaPort(project))


def _op_cleanup_plan(request: dict) -> dict:
    exact(request, {"project", "objective", "expect", "archives"}, {"project", "objective"}, name="request")
    from .cleanup import plan
    return plan(Path(request["project"]), request["objective"],
                expect=request.get("expect"), archives=request.get("archives"))


def _op_map(request: dict) -> dict:
    exact(request, {"project", "objective"}, {"project", "objective"}, name="request")
    from .ledger import map_read
    return map_read(Path(request["project"]), request["objective"])



_OPERATIONS = {
    'brief': _op_brief,
    'packet': _op_packet,
    'report': _op_report,
    'source': _op_source,
    'verify-sources': _op_verify_sources,
    'issue-intake': _op_issue_intake,
    'issue-recheck': _op_issue_recheck,
    'project-context': _op_project_context,
    'acceptance': _op_acceptance,
    'integration-observe': _op_integration_observe,
    'checkpoint': _op_checkpoint,
    'constraint': _op_constraint,
    'route-failure': _op_route_failure,
    'governor': _op_governor,
    'governor-outcome': _op_governor_outcome,
    'governor-prepare': _op_governor_prepare,
    'governor-preflight': _op_governor_preflight,
    'governor-classify': _op_governor_classify,
    'governor-correct': _op_governor_correct,
    'governor-execute': _op_governor_execute,
    'governor-reconcile': _op_governor_reconcile,
    'governor-status': _op_governor_status,
    'admission': _op_admission,
    'runtime-continuity': _op_runtime_continuity,
    'cleanup-plan': _op_cleanup_plan,
    'map': _op_map,
}


def run(operation: str, request: dict) -> dict:
    handler = _OPERATIONS.get(operation)
    if handler is None:
        raise PodError("unknown_operation", "Unsupported private helper operation")
    from .ledger import continuity_events
    events = continuity_events()
    token = events.set([])
    try:
        result = handler(request)
        rebinds = events.get()
    except FieldRefusal as exc:
        # exact() cannot know the operation; the entry names it beside the record and fields.
        exc.args = (f"internal {operation}: {exc}",)
        exc.detail = {**(exc.detail or {}), "operation": operation}
        raise
    except PodError as exc:
        if isinstance(exc.detail, dict) and isinstance(exc.detail.get("referent"), dict):
            exc.args = (f"internal {operation}: {exc}",)
            exc.detail = {**exc.detail, "operation": operation}
        if operation == "admission" and isinstance(request, dict) and request.get("project") and request.get("objective") and request.get("task"):
            from .ledger import read
            state = read(Path(request["project"]), request["objective"])
            if not any(row.get("task_id") == request["task"] for row in (state or {}).get("admissions", {}).values()):
                exc.args = (str(exc) + "; after fixing this refusal the same Task can be admitted again; settle unused Tasks through Orca",)
        # A rebind is recorded before the mutation's own checks; a later refusal still reports it.
        if events.get() and (exc.detail is None or isinstance(exc.detail, dict)):
            exc.detail = {**(exc.detail or {}), "runtime_continuity": events.get()}
        raise
    finally:
        events.reset(token)
    # A mutation that rebound a proven runtime continuity reports it (A2).
    return {**result, "runtime_continuity": rebinds} if rebinds and isinstance(result, dict) else result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.internal")
    parser.add_argument("operation", choices=tuple(_OPERATIONS))
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        value = bounded_stdin_json() if args.input == Path("-") else bounded_json(args.input)
        result = run(args.operation, value)
        print(json.dumps({"schema": "pod-cli/v4", "status": "ok", "result": result}, sort_keys=True))
        return 0
    except PodError as exc:
        error = {"code": exc.code, "message": str(exc)}
        if getattr(exc, "detail", None):
            error["detail"] = exc.detail
        print(json.dumps({"schema": "pod-cli/v4", "status": "blocked", "error": error},
                         sort_keys=True, default=str))
        return 1
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        error = {"code": "internal_error",
                 "message": f"Internal helper failed ({type(exc).__name__})"}
        print(json.dumps({"schema": "pod-cli/v4", "status": "blocked", "error": error},
                         sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

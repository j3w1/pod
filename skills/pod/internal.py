"""Private bounded helper entry. It installs no public command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import PodError
from .records import (acceptance, integration_observation, packet, report, source_identity,
                      verify_sources)
from .util import bounded_json, bounded_stdin_json, exact
from .context import execution_brief


def _governor_projection(project: Path, objective: str, owner: str, *, mutating: bool,
                         version_exempt: bool = False) -> dict:
    """Join Governor work to stable current-Run authority and objective assignments."""
    from .ledger import binding_valid, logical_projection, read
    from .operations import OrcaPort

    state = read(project, objective)
    if mutating and (state is None or state.get("owner") != owner):
        raise PodError("native_authority_unverified",
                       "Governor mutation does not own this Pod objective context")
    if mutating:
        from .ledger import _require_open
        _require_open(state)
    if mutating and not version_exempt:
        from .bundle import require_current_identity
        require_current_identity(state.get("checkpoint"))
    authority_runs: set[str] = set()
    runtimes: set[object] = set()
    assignments = []
    if state is not None:
        for row in state.get("admissions", {}).values():
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("run_id"), str):
                authority_runs.add(row["run_id"])
            runtimes.add(row.get("runtime"))
            if row.get("state") == "bound" and binding_valid(row.get("native_binding")):
                assignments.append(row)
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
    native = OrcaPort(project).read_native(
        owner, authority_runs=tuple(sorted(authority_runs)) if mutating else (),
        assignments=tuple(assignments))
    projection = logical_projection(project, native, objective=objective)
    if not mutating:
        return projection
    if (native.get("authoritative") is not True or native.get("owner") != owner
            or projection.get("authoritative") is not True
            or projection.get("owner") != owner
            or projection.get("runtime") != native.get("runtime")):
        raise PodError("native_authority_unverified",
                       "Governor mutation requires the current native objective owner")
    if not authority_runs or any(runtime != native["runtime"] for runtime in runtimes):
        raise PodError("native_authority_unverified",
                       "Governor evidence belongs to another Orca Run or runtime")
    return projection


def run(operation: str, request: dict) -> dict:
    if operation == "brief":
        exact(request, {"criteria", "coverage", "map", "project"}, {"criteria", "coverage"}, name="request")
        brief = execution_brief(request["criteria"], request["coverage"])
        # An execution brief engages the kernel: it always carries a validated draft map.
        # Validation is read-only (Git reads at the declared base), so Plan Mode can use it.
        from .obligations import brief_map
        declared = request.get("map", {}).get("governance") if isinstance(request.get("map"), dict) else None
        base_ref = declared.get("base_ref") if isinstance(declared, dict) else None
        if "project" in request:
            from .ledger import governance_observation
            observed = governance_observation(Path(request["project"]), base_ref)
        else:
            observed = {"status": "no_repository"} if base_ref is None else {"status": "unavailable"}
        brief["map"] = brief_map(request["criteria"], request.get("map"),
                                 {"governance": observed, "admissions": {}, "outstanding": [],
                                  "ceiling": 0, "delegation": "unknown", "constraints": []})
        return brief
    if operation == "packet":
        return packet(request)
    if operation == "report":
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
        from .operations import OrcaPort, _binding_from_show, _effective_evidence
        binding = admission["native_binding"]
        shown = OrcaPort(Path(request["project"])).show_worker(binding["dispatchId"])
        fresh = _binding_from_show(shown, admission, binding["dispatchId"])
        if fresh != binding:
            raise PodError("report_attempt_unverified", "Fresh native identity differs from admission")
        effective, mismatch = _effective_evidence({}, shown, admission)
        if (mismatch or any(admission["route_decision"].get("effective", {}).get(key) not in
                            (effective[key], "unknown") for key in ("agent", "model", "effort"))):
            from .ledger import update_admission
            update_admission(Path(request["project"]), request["objective"],
                             owner=admission["owner"], admission_id=admission["admission_id"],
                             update=lambda row: row["route_decision"].update(route_mismatch=True))
            raise PodError("route_mismatch", "Native effective route changed after start")
        if (admission["route_decision"].get("effective_unknown")
                and all(effective[key] != "unknown" for key in ("agent", "model", "effort"))):
            from .ledger import update_admission
            def refresh(row: dict) -> None:
                row["route_decision"]["effective"] = effective
                row["route_decision"]["effective_unknown"] = False
                row["effective_evidence"]["effective"] = effective
            update_admission(Path(request["project"]), request["objective"],
                             owner=admission["owner"], admission_id=admission["admission_id"],
                             update=refresh)
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
    if operation == "source":
        exact(request, {"project", "path"}, {"project", "path"}, name="request")
        return source_identity(Path(request["project"]), request["path"])
    if operation == "verify-sources":
        exact(request, {"project", "sources"}, {"project", "sources"}, name="request")
        verify_sources(Path(request["project"]), request["sources"])
        return {"status": "current"}
    if operation == "issue-intake":
        exact(request, {"project", "locator", "amendments"}, {"project", "locator"}, name="request")
        from .github import issue_intake
        return issue_intake(Path(request["project"]), request["locator"],
                            amendments=request.get("amendments"))
    if operation == "issue-recheck":
        exact(request, {"project", "source"}, {"project", "source"}, name="request")
        from .github import issue_recheck
        return issue_recheck(Path(request["project"]), request["source"])
    if operation == "project-context":
        exact(request, {"project"}, {"project"}, name="request")
        from .github import repository_context
        return repository_context(Path(request["project"]))
    if operation == "acceptance":
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
    if operation == "integration-observe":
        exact(request, {"project", "candidate", "base_ref"}, {"project", "candidate"}, name="request")
        return integration_observation(Path(request["project"]), request["candidate"],
                                       base_ref=request.get("base_ref", "origin/main"))
    if operation == "checkpoint":
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
    if operation == "constraint":
        exact(request, {"project", "objective", "owner", "action", "value", "id"},
              {"project", "objective", "owner", "action"}, name="request")
        from .ledger import constraints_update
        return constraints_update(Path(request["project"]), request["objective"],
                                  owner=request["owner"], action=request["action"],
                                  value=request.get("value"), constraint_id=request.get("id"))
    if operation == "route-failure":
        exact(request, {"project", "objective", "owner", "admission_id", "kind",
                        "source", "retry_after", "clear", "cleared_by"},
              {"project", "objective", "owner", "admission_id", "kind", "source"}, name="request")
        from .ledger import route_failure
        return route_failure(Path(request["project"]), request["objective"],
                             owner=request["owner"], admission_id=request["admission_id"],
                             kind=request["kind"], source=request["source"],
                             retry_after=request.get("retry_after"), clear=request.get("clear", False),
                             cleared_by=request.get("cleared_by"))
    if operation == "governor":
        exact(request, {"project", "objective", "owner", "action", "exception"},
              {"project", "objective", "owner", "action"}, name="request")
        from .governor import decide
        project = Path(request["project"])
        projection = _governor_projection(project, request["objective"], request["owner"],
                                          mutating=True)
        return decide(project, request["objective"], owner=request["owner"],
                      action=request["action"], exception=request.get("exception"),
                      native_projection=projection)
    if operation == "governor-outcome":
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
    if operation == "governor-prepare":
        exact(request, {"project", "objective", "owner", "unit", "branch", "tasks", "base_ref",
                        "workflows", "verification", "toolchain", "environment"},
              {"project", "objective", "owner", "unit"}, name="request")
        from .governor import observe_candidate, prepare_candidate
        project = Path(request["project"])
        _governor_projection(project, request["objective"], request["owner"], mutating=True)
        # Commit and tree are read from Git here. A caller cannot hand in the candidate it
        # wants validated, because that identity is what every later reuse rests on.
        observation = observe_candidate(project, base_ref=request.get("base_ref", "origin/main"),
                                        workflows=request.get("workflows"),
                                        verification=request.get("verification"),
                                        toolchain=request.get("toolchain"),
                                        environment=request.get("environment"))
        return prepare_candidate(project, request["objective"], owner=request["owner"], unit=request["unit"],
                                 observation=observation, branch=request.get("branch"),
                                 tasks=request.get("tasks"))
    if operation == "governor-preflight":
        exact(request, {"project", "objective", "owner", "unit", "candidate", "check", "status", "report"},
              {"project", "objective", "owner", "unit", "candidate", "check", "status"}, name="request")
        from .governor import record_preflight
        project = Path(request["project"])
        _governor_projection(project, request["objective"], request["owner"], mutating=True)
        return record_preflight(project, request["objective"], owner=request["owner"],
                                unit=request["unit"], candidate=request["candidate"], check=request["check"],
                                status=request["status"], report=request.get("report"))
    if operation == "governor-classify":
        exact(request, {"project", "objective", "owner", "record_id", "classification"},
              {"project", "objective", "owner", "record_id", "classification"}, name="request")
        from .governor import classify_failure
        project = Path(request["project"])
        _governor_projection(project, request["objective"], request["owner"], mutating=True)
        return classify_failure(project, request["objective"], owner=request["owner"],
                                record_id=request["record_id"], classification=request["classification"])
    if operation == "governor-correct":
        exact(request, {"project", "objective", "owner", "unit", "correction", "diagnosis"},
              {"project", "objective", "owner", "unit", "correction"}, name="request")
        from .governor import record_correction
        project = Path(request["project"])
        _governor_projection(project, request["objective"], request["owner"], mutating=True)
        return record_correction(project, request["objective"], owner=request["owner"],
                                 unit=request["unit"], correction=request["correction"],
                                 diagnosis=request.get("diagnosis"))
    if operation == "governor-execute":
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
    if operation == "governor-reconcile":
        exact(request, {"project", "objective", "owner", "record_id"},
              {"project", "objective", "owner", "record_id"}, name="request")
        from .governor import reconcile
        project = Path(request["project"])
        _governor_projection(project, request["objective"], request["owner"],
                             mutating=True, version_exempt=True)
        return reconcile(project, request["objective"], owner=request["owner"],
                         record_id=request["record_id"])
    if operation == "governor-status":
        exact(request, {"project", "objective", "owner"}, {"project", "objective", "owner"}, name="request")
        from .governor import status
        project = Path(request["project"])
        projection = _governor_projection(project, request["objective"], request["owner"],
                                          mutating=False)
        return status(project, request["objective"], native_projection=projection)
    if operation == "admission":
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
    raise PodError("unknown_operation", "Unsupported private helper operation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.internal")
    parser.add_argument("operation", choices=("brief", "packet", "report", "source",
                                               "verify-sources", "acceptance", "integration-observe",
                                               "issue-intake", "issue-recheck", "project-context",
                                               "checkpoint", "admission", "constraint", "route-failure",
                                               "governor", "governor-outcome", "governor-prepare",
                                               "governor-preflight", "governor-classify",
                                               "governor-correct", "governor-execute",
                                               "governor-reconcile", "governor-status"))
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


if __name__ == "__main__":
    raise SystemExit(main())

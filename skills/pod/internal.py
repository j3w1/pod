"""Private bounded helper entry. It installs no public command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import effective
from .errors import PodError
from .records import (acceptance, integration_observation, packet, report, source_identity,
                      verify_sources)
from .routing import preview, replay
from .release import release_gate
from .util import bounded_json, exact
from .context import execution_brief


def run(operation: str, request: dict) -> dict:
    if operation == "brief":
        exact(request, {"criteria", "coverage"}, {"criteria", "coverage"}, name="request")
        return execution_brief(request["criteria"], request["coverage"])
    if operation == "preview":
        exact(request, {"project", "assessment", "capabilities", "quotas", "occupancy", "strict_pin",
                        "safety_refusal", "objective", "task_policy"},
              {"project", "assessment"}, name="request")
        policy = effective(Path(request["project"]), task=request.get("task_policy"))
        return preview(request["assessment"], policy, capabilities=request.get("capabilities"),
                       quotas=request.get("quotas"), occupancy=request.get("occupancy"),
                       strict_pin=request.get("strict_pin"), safety_refusal=request.get("safety_refusal", False),
                       objective=request.get("objective"))
    if operation == "replay":
        return replay(request)
    if operation == "packet":
        return packet(request)
    if operation == "report":
        exact(request, {"report", "packet", "project", "objective", "admission_id"},
              {"report", "packet", "project", "objective", "admission_id"}, name="request")
        if not isinstance(request["packet"], dict):
            raise PodError("invalid_packet", "Report needs a frozen packet")
        from .ledger import read
        state = read(Path(request["project"]), request["objective"])
        admission = state.get("admissions", {}).get(request["admission_id"]) if state else None
        if (not admission or admission.get("state") != "bound" or not admission.get("native_binding")
                or admission.get("packet_id") != request["packet"].get("packet_id")):
            raise PodError("report_attempt_unverified", "No exact native admission binding")
        from .operations import OrcaPort, _binding_from_show
        binding = admission["native_binding"]
        shown = OrcaPort(Path(request["project"])).show_worker(binding["dispatchId"])
        fresh = _binding_from_show(shown, admission, binding["dispatchId"])
        if fresh != binding:
            raise PodError("report_attempt_unverified", "Fresh native identity differs from admission")
        return report(request["report"], request["packet"],
                      {"runtime": admission["runtime"], **{key: binding[key] for key in
                       ("runId", "taskId", "dispatchId", "workerId")}})
    if operation == "source":
        exact(request, {"project", "path"}, {"project", "path"}, name="request")
        return source_identity(Path(request["project"]), request["path"])
    if operation == "verify-sources":
        exact(request, {"project", "sources"}, {"project", "sources"}, name="request")
        verify_sources(Path(request["project"]), request["sources"])
        return {"status": "current"}
    if operation == "acceptance":
        exact(request, {"criteria", "evidence_rows", "candidate", "policy_revision",
                        "sources", "dependencies", "environment", "review_required",
                        "hosted_required", "owner_acceptance", "project"},
              {"criteria", "evidence_rows", "candidate", "policy_revision",
               "sources", "dependencies", "environment", "review_required",
               "hosted_required"}, name="request")
        arguments = {key: value for key, value in request.items() if key != "project"}
        # Whether a candidate is merged or released is read from Git here. A caller cannot
        # hand in that answer, because it is the one the final report rests on.
        if "project" in request:
            arguments["integration"] = integration_observation(Path(request["project"]),
                                                               request["candidate"])
        return acceptance(**arguments)
    if operation == "integration-observe":
        exact(request, {"project", "candidate", "base_ref"}, {"project", "candidate"}, name="request")
        return integration_observation(Path(request["project"]), request["candidate"],
                                       base_ref=request.get("base_ref", "origin/main"))
    if operation == "release-gate":
        exact(request, {"candidate", "tree", "records", "authorization"},
              {"candidate", "tree", "records"}, name="request")
        return release_gate(**request)
    if operation == "checkpoint":
        exact(request, {"project", "objective", "owner", "value"},
              {"project", "objective", "owner", "value"}, name="request")
        from .ledger import checkpoint
        from .orca import contract
        snapshot = contract()
        if snapshot.get("status") != "observed":
            raise PodError("orca_unavailable", "A checkpoint needs a current native runtime read")
        return checkpoint(Path(request["project"]), request["objective"], owner=request["owner"],
                          value=request["value"], native={"runtime": snapshot["runtime"]})
    if operation == "governor":
        exact(request, {"project", "objective", "owner", "action", "exception"},
              {"project", "objective", "owner", "action"}, name="request")
        from .governor import decide
        from .operations import OrcaPort
        from .ledger import fresh_projection
        native = OrcaPort(Path(request["project"])).read_native(request["owner"])
        projection = fresh_projection(Path(request["project"]), native,
                                      objective=request["objective"])
        return decide(Path(request["project"]), request["objective"], owner=request["owner"],
                      action=request["action"], exception=request.get("exception"),
                      native_projection=projection)
    if operation == "governor-outcome":
        exact(request, {"project", "objective", "owner", "record_id", "outcome", "provider", "evidence", "detail"},
              {"project", "objective", "owner", "record_id", "outcome"}, name="request")
        from .governor import record_outcome
        return record_outcome(Path(request["project"]), request["objective"], owner=request["owner"],
                              record_id=request["record_id"], outcome=request["outcome"],
                              provider=request.get("provider"), evidence=request.get("evidence"),
                              detail=request.get("detail"))
    if operation == "governor-prepare":
        exact(request, {"project", "objective", "owner", "unit", "branch", "tasks", "base_ref",
                        "workflows", "verification", "toolchain", "environment"},
              {"project", "objective", "owner", "unit"}, name="request")
        from .governor import observe_candidate, prepare_candidate
        project = Path(request["project"])
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
        return record_preflight(Path(request["project"]), request["objective"], owner=request["owner"],
                                unit=request["unit"], candidate=request["candidate"], check=request["check"],
                                status=request["status"], report=request.get("report"))
    if operation == "governor-classify":
        exact(request, {"project", "objective", "owner", "record_id", "classification"},
              {"project", "objective", "owner", "record_id", "classification"}, name="request")
        from .governor import classify_failure
        return classify_failure(Path(request["project"]), request["objective"], owner=request["owner"],
                                record_id=request["record_id"], classification=request["classification"])
    if operation == "governor-correct":
        exact(request, {"project", "objective", "owner", "unit", "correction", "diagnosis"},
              {"project", "objective", "owner", "unit", "correction"}, name="request")
        from .governor import record_correction
        return record_correction(Path(request["project"]), request["objective"], owner=request["owner"],
                                 unit=request["unit"], correction=request["correction"],
                                 diagnosis=request.get("diagnosis"))
    if operation == "governor-execute":
        exact(request, {"project", "objective", "owner", "action", "exception", "pull_request"},
              {"project", "objective", "owner", "action"}, name="request")
        from .governor import execute
        from .operations import OrcaPort
        from .ledger import fresh_projection
        native = OrcaPort(Path(request["project"])).read_native(request["owner"])
        projection = fresh_projection(Path(request["project"]), native,
                                      objective=request["objective"])
        # The production port is the installed gh and git; request JSON cannot supply one.
        return execute(Path(request["project"]), request["objective"], owner=request["owner"],
                       action=request["action"], exception=request.get("exception"),
                       pull_request=request.get("pull_request"), native_projection=projection)
    if operation == "governor-reconcile":
        exact(request, {"project", "objective", "owner", "record_id"},
              {"project", "objective", "owner", "record_id"}, name="request")
        from .governor import reconcile
        return reconcile(Path(request["project"]), request["objective"], owner=request["owner"],
                         record_id=request["record_id"])
    if operation == "governor-status":
        exact(request, {"project", "objective", "owner"}, {"project", "objective", "owner"}, name="request")
        from .governor import status
        from .operations import OrcaPort
        from .ledger import fresh_projection
        native = OrcaPort(Path(request["project"])).read_native(request["owner"])
        projection = fresh_projection(Path(request["project"]), native,
                                      objective=request["objective"])
        return status(Path(request["project"]), request["objective"], native_projection=projection)
    if operation == "admission":
        exact(request, {"project", "objective", "owner", "run", "task",
                        "assessment", "capabilities", "quotas", "occupancy", "plan_revision",
                        "capacity", "capacity_reason", "exceptional_grant", "packet", "worktree"},
              {"project", "objective", "owner", "run", "task",
               "assessment", "capabilities", "quotas", "occupancy", "plan_revision", "packet"}, name="request")
        from .operations import guarded_start
        # The production port verifies installed controls itself; request JSON
        # can supply assessment snapshots but cannot assert native assurance.
        return guarded_start(Path(request["project"]), request["objective"],
                             owner=request["owner"], run=request["run"], task=request["task"],
                             assessment=request["assessment"], capabilities=request["capabilities"], quotas=request["quotas"],
                             occupancy=request["occupancy"], plan_revision=request["plan_revision"],
                             capacity=request.get("capacity", 2),
                             capacity_reason=request.get("capacity_reason"),
                             exceptional_grant=request.get("exceptional_grant"),
                             frozen_packet=request["packet"],
                             worktree=request.get("worktree", "current"))
    if operation == "state-migrate":
        exact(request, {"project", "objective", "owner"},
              {"project", "objective", "owner"}, name="request")
        from .operations import migrate_state
        return migrate_state(Path(request["project"]), request["objective"], owner=request["owner"])
    raise PodError("unknown_operation", "Unsupported private helper operation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.internal")
    parser.add_argument("operation", choices=("brief", "preview", "replay", "packet", "report", "source",
                                               "verify-sources", "acceptance", "integration-observe",
                                               "checkpoint", "admission", "state-migrate", "release-gate",
                                               "governor", "governor-outcome", "governor-prepare",
                                               "governor-preflight", "governor-classify",
                                               "governor-correct", "governor-execute",
                                               "governor-reconcile", "governor-status"))
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        value = bounded_json(args.input)
        result = run(args.operation, value)
        print(json.dumps({"schema": "pod-helper/v2", "status": "ok", "result": result}, sort_keys=True))
        return 0
    except PodError as exc:
        print(json.dumps({"schema": "pod-helper/v2", "status": "blocked",
                          "error": {"code": exc.code, "message": str(exc)}}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

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
        exact(request, {"report", "packet", "project", "objective", "operation_id"},
              {"report", "packet", "project", "objective", "operation_id"}, name="request")
        if not isinstance(request["packet"], dict):
            raise PodError("invalid_packet", "Report needs a frozen packet")
        from .ledger import read
        state = read(Path(request["project"]), request["objective"])
        effect = state.get("effects", {}).get(request["operation_id"]) if state else None
        if (not effect or effect.get("state") != "confirmed" or not effect.get("native_binding")
                or effect.get("packet_id") != request["packet"].get("packet_id")):
            raise PodError("report_attempt_unverified", "No confirmed native attempt binding")
        binding = effect["native_binding"]
        return report(request["report"], request["packet"],
                      {"runtime": effect["runtime"], **{key: binding[key] for key in
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
                        "sources", "dependencies", "environment", "review_required", "hosted_required",
                        "owner_acceptance", "integration"},
              {"criteria", "evidence_rows", "candidate", "policy_revision",
               "sources", "dependencies", "environment", "review_required", "hosted_required"}, name="request")
        return acceptance(**request)
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
        exact(request, {"project", "objective", "owner", "action", "override"},
              {"project", "objective", "owner", "action"}, name="request")
        from .governor import decide
        return decide(Path(request["project"]), request["objective"], owner=request["owner"],
                      action=request["action"], override=request.get("override"))
    if operation == "governor-outcome":
        exact(request, {"project", "objective", "owner", "record_id", "outcome"},
              {"project", "objective", "owner", "record_id", "outcome"}, name="request")
        from .governor import record_outcome
        return record_outcome(Path(request["project"]), request["objective"], owner=request["owner"],
                              record_id=request["record_id"], outcome=request["outcome"])
    if operation == "admission":
        exact(request, {"project", "objective", "owner", "run", "task", "operation_id",
                        "assessment", "capabilities", "quotas", "occupancy", "plan_revision",
                        "capacity", "capacity_reason", "exceptional_grant", "packet", "worktree"},
              {"project", "objective", "owner", "run", "task", "operation_id",
               "assessment", "capabilities", "quotas", "occupancy", "plan_revision", "packet"}, name="request")
        from .operations import guarded_start
        # The production port verifies installed controls itself; request JSON
        # can supply assessment snapshots but cannot assert native assurance.
        return guarded_start(Path(request["project"]), request["objective"],
                             owner=request["owner"], run=request["run"], task=request["task"],
                             operation_id=request["operation_id"], assessment=request["assessment"],
                             capabilities=request["capabilities"], quotas=request["quotas"],
                             occupancy=request["occupancy"], plan_revision=request["plan_revision"],
                             capacity=request.get("capacity", 2),
                             capacity_reason=request.get("capacity_reason"),
                             exceptional_grant=request.get("exceptional_grant"),
                             frozen_packet=request["packet"],
                             worktree=request.get("worktree", "current"))
    if operation == "reconcile-launch":
        exact(request, {"project", "objective", "owner", "operation_id", "run", "task"},
              {"project", "objective", "owner", "operation_id", "run", "task"}, name="request")
        from .operations import reconcile_launch
        return reconcile_launch(Path(request["project"]), request["objective"], owner=request["owner"],
                                operation_id=request["operation_id"], run=request["run"],
                                task=request["task"])
    if operation == "delivery":
        exact(request, {"project", "objective", "owner", "run", "timeout_ms", "retain"},
              {"project", "objective", "owner", "run"}, name="request")
        from .operations import settle_delivery
        retain = request.get("retain") or []
        if not isinstance(retain, list) or any(not isinstance(item, str) for item in retain):
            raise PodError("invalid_request", "Retained dispatch identities must be strings")
        return settle_delivery(Path(request["project"]), request["objective"], owner=request["owner"],
                               run=request["run"], timeout_ms=request.get("timeout_ms"),
                               retain=tuple(retain))
    if operation == "delivery-ack":
        exact(request, {"project", "objective", "owner", "run", "delivery_id"},
              {"project", "objective", "owner", "run", "delivery_id"}, name="request")
        from .operations import acknowledge_delivery
        return acknowledge_delivery(Path(request["project"]), request["objective"],
                                    owner=request["owner"], run=request["run"],
                                    delivery_id=request["delivery_id"])
    if operation == "release":
        exact(request, {"project", "objective", "owner", "dispatch"},
              {"project", "objective", "owner", "dispatch"}, name="request")
        from .operations import release_once
        return release_once(Path(request["project"]), request["objective"],
                            owner=request["owner"], dispatch=request["dispatch"])
    if operation == "reconcile-release":
        exact(request, {"project", "objective", "owner", "dispatch"},
              {"project", "objective", "owner", "dispatch"}, name="request")
        from .operations import reconcile_release
        return reconcile_release(Path(request["project"]), request["objective"],
                                 owner=request["owner"], dispatch=request["dispatch"])
    raise PodError("unknown_operation", "Unsupported private helper operation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.internal")
    parser.add_argument("operation", choices=("brief", "preview", "replay", "packet", "report", "source",
                                               "verify-sources", "acceptance", "integration-observe",
                                               "checkpoint", "admission", "reconcile-launch",
                                               "delivery", "delivery-ack", "release",
                                               "reconcile-release", "release-gate",
                                               "governor", "governor-outcome"))
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        value = bounded_json(args.input)
        result = run(args.operation, value)
        print(json.dumps({"schema": "pod-helper/v1", "status": "ok", "result": result}, sort_keys=True))
        return 0
    except PodError as exc:
        print(json.dumps({"schema": "pod-helper/v1", "status": "blocked",
                          "error": {"code": exc.code, "message": str(exc)}}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

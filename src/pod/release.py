"""Candidate-bound release evidence projection. It never performs a release."""

from __future__ import annotations

from typing import Any

from .errors import PodError
from .util import bounded_text, exact

REQUIRED_GATES = (
    "unit_linux", "unit_windows", "incident_linux", "incident_windows",
    "compile_linux", "compile_windows", "frozen_wheel_linux", "frozen_wheel_windows",
    "isolated_install_linux", "isolated_install_windows", "hosted_ci",
    "skill_validation", "independent_review", "live_codex_linux", "live_claude_linux",
    "live_codex_windows", "live_claude_windows", "matched_evaluation",
    "project_acceptance",
)
LIVE_CHECKS = ("discovery", "in_session", "authorized_execution", "effective_route",
               "lifecycle", "verification", "adoption")
ROW_FIELDS = {"schema", "candidate", "tree", "host", "utc", "gate", "command",
              "outcome", "report", "checks"}


def release_gate(candidate: str, tree: str, records: list[dict]) -> dict:
    bounded_text(candidate, name="candidate", limit=64)
    bounded_text(tree, name="tree", limit=64)
    if not isinstance(records, list) or len(records) > 128:
        raise PodError("invalid_validation", "Validation records exceed limit")
    matching: dict[str, dict] = {}
    for raw in records:
        row = exact(raw, ROW_FIELDS, ROW_FIELDS - {"checks"}, name="validation")
        if row["schema"] != "pod-validation/v1" or row["gate"] not in REQUIRED_GATES:
            raise PodError("invalid_validation", "Unknown gate or validation schema")
        if row["candidate"] != candidate or row["tree"] != tree:
            continue
        if row["gate"] in matching:
            raise PodError("duplicate_validation", "Multiple records claim the same candidate gate")
        if row["outcome"] not in ("PASS", "FAILED", "NOT_RUN", "UNAVAILABLE"):
            raise PodError("invalid_validation", "Invalid gate outcome")
        for field in ("host", "utc", "command", "report"):
            bounded_text(row[field], name=field, limit=512)
        if row["gate"].startswith("live_"):
            checks = row.get("checks")
            if not isinstance(checks, dict) or set(checks) != set(LIVE_CHECKS) or any(v != "PASS" for v in checks.values()):
                row = {**row, "outcome": "UNAVAILABLE"}
        matching[row["gate"]] = row
    statuses = {gate: matching[gate]["outcome"] if gate in matching else "NOT_RUN" for gate in REQUIRED_GATES}
    complete = all(status == "PASS" for status in statuses.values())
    return {"schema": "pod-release-gate/v1", "candidate": candidate, "tree": tree,
            "status": "owner_decision_required" if complete else "blocked",
            "gates": statuses, "missing_or_failed": [gate for gate, status in statuses.items() if status != "PASS"],
            "release_authorized": False}

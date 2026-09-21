"""Candidate-bound release evidence projection. It never performs a release."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .errors import PodError
from .util import bounded_text, exact

REQUIRED_GATES = (
    "unit_linux", "unit_windows", "incident_linux", "incident_windows",
    "compile_linux", "compile_windows", "frozen_wheel_linux", "frozen_wheel_windows",
    "isolated_install_linux", "isolated_install_windows", "hosted_ci_linux", "hosted_ci_windows",
    "skill_validation", "independent_review", "live_codex_linux", "live_claude_linux",
    "live_codex_windows", "live_claude_windows", "matched_evaluation",
    "project_acceptance",
)
LIVE_CHECKS = ("discovery", "in_session", "authorized_execution", "effective_route",
               "lifecycle", "verification", "adoption")
ROW_FIELDS = {"schema", "candidate", "tree", "host", "utc", "gate", "command",
              "outcome", "report", "checks"}
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_REPORT_REFERENCE = re.compile(
    r"(?P<reference>[A-Za-z0-9][A-Za-z0-9._/-]{0,255}) sha256:(?P<digest>[0-9a-f]{64})\Z"
)


def _git_object_id(value: Any) -> bool:
    return isinstance(value, str) and _GIT_OBJECT_ID.fullmatch(value) is not None


def _git_object_pair(candidate: Any, tree: Any) -> bool:
    return (_git_object_id(candidate) and _git_object_id(tree)
            and len(candidate) == len(tree))


def _utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _sanitized_report(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = _REPORT_REFERENCE.fullmatch(value)
    if match is None:
        return False
    return all(part not in ("", ".", "..") for part in match.group("reference").split("/"))


def release_gate(candidate: str, tree: str, records: list[dict]) -> dict:
    if not _git_object_pair(candidate, tree):
        raise PodError("invalid_validation", "Candidate and tree must use one full Git object format")
    if not isinstance(records, list) or len(records) > 128:
        raise PodError("invalid_validation", "Validation records exceed limit")
    matching: dict[str, dict] = {}
    for raw in records:
        row = exact(raw, ROW_FIELDS, ROW_FIELDS - {"checks"}, name="validation")
        if row["schema"] != "pod-validation/v1" or row["gate"] not in REQUIRED_GATES:
            raise PodError("invalid_validation", "Unknown gate or validation schema")
        if row["outcome"] not in ("PASS", "FAILED", "NOT_RUN", "UNAVAILABLE"):
            raise PodError("invalid_validation", "Invalid gate outcome")
        for field in ("host", "utc", "command", "report"):
            bounded_text(row[field], name=field, limit=512)
        if not _git_object_pair(row["candidate"], row["tree"]):
            raise PodError("invalid_validation", "Validation candidate and tree must use one full Git object format")
        if not _utc_timestamp(row["utc"]):
            raise PodError("invalid_validation", "Validation timestamp must be canonical UTC")
        if not _sanitized_report(row["report"]):
            raise PodError("invalid_validation", "Validation report reference or digest is malformed")
        expected_host = ("Linux" if row["gate"].endswith("_linux") else
                         "Windows" if row["gate"].endswith("_windows") else None)
        if ((expected_host is not None and row["host"] != expected_host)
                or (expected_host is None and row["host"] not in ("Linux", "Windows"))):
            raise PodError("invalid_validation", "Validation gate host does not match its required OS")
        if row["candidate"] != candidate or row["tree"] != tree:
            continue
        if row["gate"] in matching:
            raise PodError("duplicate_validation", "Multiple records claim the same candidate gate")
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

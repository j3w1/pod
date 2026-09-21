"""Candidate-bound release evidence projection. It never performs a release."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .errors import PodError
from .util import bounded_text, exact

REQUIRED_GATES = (
    "unit_linux", "incident_linux", "compile_linux", "frozen_wheel_linux",
    "isolated_install_linux", "bundle_copy_form", "hosted_ci_linux",
    "skill_validation", "skill_bundle_parity", "skills_cli_install",
    "independent_review", "live_codex_linux", "live_claude_linux",
    "orca_delegation_codex", "orca_delegation_claude", "project_acceptance",
)
LIVE_CHECKS = ("discovery", "in_session", "authorized_execution", "effective_route",
               "lifecycle", "verification", "adoption")
DELEGATION_CHECKS = ("request_construction", "account_authentication", "launch_identity",
                     "effective_launch", "delivery", "settlement", "release")
AUTHORIZATION_FIELDS = {"schema", "candidate", "tree", "scope", "authorized_by", "utc", "reference"}
AUTHORIZATION_SCOPES = ("merge", "release", "deploy")
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


def validate_authorization(value: Any, *, candidate: str, tree: str) -> dict | None:
    """Accept only a complete owner authorization bound to this exact candidate.

    Authorization is an input the owner supplies. It is never derived from test
    results, and passing checks never produce it.
    """
    if value is None:
        return None
    record = exact(value, AUTHORIZATION_FIELDS, AUTHORIZATION_FIELDS, name="authorization")
    if record["schema"] != "pod-release-authorization/v1":
        raise PodError("invalid_authorization", "Authorization schema is unsupported")
    for field in ("authorized_by", "reference"):
        bounded_text(record[field], name=field, limit=512)
    if not _git_object_pair(record["candidate"], record["tree"]):
        raise PodError("invalid_authorization", "Authorization candidate and tree must be Git object ids")
    if not _utc_timestamp(record["utc"]):
        raise PodError("invalid_authorization", "Authorization timestamp must be canonical UTC")
    scope = record["scope"]
    if (not isinstance(scope, list) or not scope or len(scope) > 8
            or any(item not in AUTHORIZATION_SCOPES for item in scope)):
        raise PodError("invalid_authorization", "Authorization scope is unsupported")
    if "release" not in scope:
        return None
    if record["candidate"] != candidate or record["tree"] != tree:
        return None
    return record


def release_gate(candidate: str, tree: str, records: list[dict],
                 authorization: Any = None) -> dict:
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
        # Linux is the supported execution environment; there is no other host vocabulary.
        if row["host"] != "Linux":
            raise PodError("invalid_validation", "Validation gate host does not match its required OS")
        if row["candidate"] != candidate or row["tree"] != tree:
            continue
        if row["gate"] in matching:
            raise PodError("duplicate_validation", "Multiple records claim the same candidate gate")
        required_checks = (LIVE_CHECKS if row["gate"].startswith("live_")
                           else DELEGATION_CHECKS if row["gate"].startswith("orca_delegation_")
                           else None)
        if required_checks is not None:
            checks = row.get("checks")
            if (not isinstance(checks, dict) or set(checks) != set(required_checks)
                    or any(value != "PASS" for value in checks.values())):
                row = {**row, "outcome": "UNAVAILABLE"}
        matching[row["gate"]] = row
    statuses = {gate: matching[gate]["outcome"] if gate in matching else "NOT_RUN" for gate in REQUIRED_GATES}
    complete = all(status == "PASS" for status in statuses.values())
    granted = validate_authorization(authorization, candidate=candidate, tree=tree)
    status = "blocked"
    if complete:
        status = "authorized" if granted is not None else "owner_decision_required"
    return {"schema": "pod-release-gate/v1", "candidate": candidate, "tree": tree,
            "status": status, "gates": statuses,
            "missing_or_failed": [gate for gate, value in statuses.items() if value != "PASS"],
            "authorization": {"present": granted is not None,
                              "scope": granted["scope"] if granted else []},
            "release_authorized": status == "authorized"}

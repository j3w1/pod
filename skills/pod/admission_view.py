"""R250: one admission's packet and report-receipt facts, read from Pod's own record and nothing else.

`pod status --objective ID --admission ADMISSION --json` is the supported public proof that Pod accepted a
worker's report. It projects only identifiers, digests and receipt fields, under their recorded names, and never
the report body, packet text, transcript or source content. It reads one record without a lock through the
lockless scan read (R206): no Orca, preference, provider or GitHub read, and no write, conversion or repair.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import PodError

SCHEMA = "pod-admission-view/v1"
# Every projected value is an identifier, digest, state word or time; none needs more.
VALUE_LIMIT = 512
# The whole projection stays within this many encoded bytes; tests hold it there.
VIEW_BYTES = 8 * 1024
ADMISSION_ID = re.compile(r"[a-f0-9]{64}")
UNKNOWN = "unknown"
NO_REPORT = "no report has been consumed for this admission"


def _value(value):
    """A recorded identifier as recorded, or None when it is absent or not a bounded single-line text."""
    if isinstance(value, str) and value and len(value) <= VALUE_LIMIT and value.isprintable():
        return value
    return None


def _count(value):
    return len(value) if isinstance(value, list) else None


class _Facts:
    """Copy named facts, turning a missing or malformed one into `unknown` with its reason."""

    def __init__(self):
        self.unknown = []

    def text(self, field, value, *, nullable=False):
        if value is None and nullable:
            return None
        found = _value(value)
        if found is None:
            self.unknown.append({"field": field, "reason": "absent" if value is None else "unreadable"})
            return UNKNOWN
        return found


def _ingestion(row: dict, facts: _Facts) -> dict:
    """The recorded Git ingestion of an implementer's result, as counts and commits, never its paths."""
    if row.get("role") != "implement":
        return {"status": "not_applicable"}
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    base = facts.text("report.ingestion.base", result.get("base"), nullable=True)
    head = facts.text("report.ingestion.head", result.get("head"), nullable=True)
    if result.get("paths_status") == "over_limit":
        count = result.get("path_count")
        return {"status": "over_limit", "reason": "changed_paths_limit", "base": base, "head": head,
                "changed_path_count": count if type(count) is int else UNKNOWN}
    changed, exceeded = _count(row.get("changed_paths")), _count(row.get("boundary_exceeded"))
    if changed is None:
        facts.unknown.append({"field": "report.ingestion", "reason": "changed paths were not recorded"})
        return {"status": UNKNOWN, "base": base, "head": head}
    return {"status": "recorded", "base": base, "head": head, "changed_path_count": changed,
            "boundary_exceeded_count": exceeded if exceeded is not None else UNKNOWN}


def _report(row: dict, facts: _Facts) -> dict:
    report = row.get("report")
    if report is None:
        return {"consumed": False, "reason": NO_REPORT}
    observation = report.get("observation") if isinstance(report.get("observation"), dict) else {}
    binding = row.get("native_binding") if isinstance(row.get("native_binding"), dict) else {}
    return {
        "consumed": True,
        "status": facts.text("report.status", report.get("status")),
        # Pod consumes a report only after Orca settled its exact Dispatch, so a consumed one settled.
        "settled": True,
        "outcome": facts.text("report.outcome", report.get("outcome")),
        "assignment": facts.text("report.assignment", observation.get("assignment")),
        "attempt": facts.text("report.attempt", report.get("attempt")),
        "candidate": facts.text("report.candidate", observation.get("candidate")),
        "native_binding": {"runtime": facts.text("report.native_binding.runtime", row.get("runtime")),
                           **{key: facts.text("report.native_binding." + key, binding.get(key))
                              for key in ("runId", "taskId", "dispatchId", "workerId")}},
        "observation_digest": facts.text("report.observation_digest", report.get("observation_digest")),
        "result_commit": facts.text("report.result_commit", report.get("result_commit"), nullable=True),
        "consumed_at": facts.text("report.consumed_at", report.get("consumed_at")),
        "ingestion": _ingestion(row, facts),
    }


def _project(row: dict) -> dict:
    facts = _Facts()
    binding = row.get("native_binding")
    native = binding if isinstance(binding, dict) else {}
    recovery = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
    view = {
        "found": True,
        "state": facts.text("state", row.get("state")),
        "role": facts.text("role", row.get("role")),
        "packet_id": facts.text("packet_id", row.get("packet_id")),
        "candidate": facts.text("candidate", row.get("candidate")),
        # The digest of the frozen packet's assignment, recorded at admission as `recovery.assignment`.
        "assignment_digest": facts.text("assignment_digest", recovery.get("assignment")),
        "run_id": facts.text("run_id", row.get("run_id")),
        "task_id": facts.text("task_id", row.get("task_id")),
        # A reserved or deferred admission has no Dispatch yet; null says so, never unknown.
        "dispatch_id": facts.text("dispatch_id", native.get("dispatchId"), nullable=binding is None),
        "report": _report(row, facts),
    }
    view["unknown"] = facts.unknown
    return view


def _unread(reason: str, detail: str) -> dict:
    """Every fact reads unknown when the record itself cannot be read."""
    fields = ("found", "state", "role", "packet_id", "candidate", "assignment_digest", "run_id", "task_id",
              "dispatch_id", "report")
    return {**{field: UNKNOWN for field in fields},
            "unknown": [{"field": "record", "reason": reason, "detail": detail[:VALUE_LIMIT]}]}


def view(project: Path, objective: str, admission: str) -> dict:
    """The public projection; `status` is `observed`, `absent` (no such admission) or `unavailable`."""
    from .history import load_context, scan_read
    from .ledger import _record_exists, objective_root
    result = {"schema": "pod-cli/v5", "status": "observed"}
    head = {"schema": SCHEMA, "objective": objective, "admission": admission}
    if not ADMISSION_ID.fullmatch(admission):
        raise PodError("invalid_admission", "--admission names a Pod admission id: 64 lower-case hex characters",
                       {"next_action": "pass the admission id from pod status --objective ID --json"})
    try:
        path = objective_root(project, objective) / "context.json"
        if not _record_exists(path):
            raise PodError("objective_unknown", "This repository has no record for the objective")
        state = scan_read(path, lambda: load_context(path))
        admissions = state.get("admissions") if isinstance(state, dict) else None
        if not isinstance(admissions, dict):
            raise PodError("state_unsupported", "The record has no admission inventory")
        row = admissions.get(admission)
    except PodError as exc:
        if exc.code == "objective_unknown":
            return {**result, "status": "blocked", "blocker": exc.code,
                    "next_safe_action": "name a recorded objective in this repository",
                    "admission_view": {**head, "record": "absent", "found": False, "unknown": [],
                                       "reason": "this repository has no record for the objective"}}
        return {**result, "status": "unavailable",
                "admission_view": {**head, "record": UNKNOWN, **_unread(exc.code, str(exc))}}
    except OSError as exc:
        return {**result, "status": "unavailable",
                "admission_view": {**head, "record": UNKNOWN, **_unread("record_unreadable", type(exc).__name__)}}
    if row is None:
        return {**result, "status": "absent",
                "admission_view": {**head, "record": "read", "found": False, "unknown": [],
                                   "reason": "the objective's record holds no admission with this id"}}
    if not isinstance(row, dict):
        return {**result, "status": "unavailable",
                "admission_view": {**head, "record": "read", **_unread("admission_unreadable",
                                                                       "the admission is not a record")}}
    projected = {**head, "record": "read", **_project(row)}
    if len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) > VIEW_BYTES:
        # Unreachable with bounded values; kept so the documented bound can never be exceeded.
        projected = {**head, "record": "read", **_unread("view_limit", "the projection exceeded its bound")}
        result["status"] = "unavailable"
    return {**result, "admission_view": projected}


def render(result: dict) -> None:
    """Short human output; `--json` is the contract."""
    item = result.get("admission_view") or {}
    print(f"Admission {item.get('admission')} of {item.get('objective')}: {result.get('status')}")
    for field in ("state", "role", "packet_id", "candidate", "assignment_digest", "run_id", "task_id", "dispatch_id"):
        if field in item:
            print(f"  {field}: {item[field]}")
    report = item.get("report")
    if isinstance(report, dict):
        if report.get("consumed"):
            print(f"  report: {report['status']}, outcome {report['outcome']}, attempt {report['attempt']}, "
                  f"consumed {report['consumed_at']}")
        else:
            print(f"  report: {report.get('reason')}")
    for row in item.get("unknown") or []:
        print(f"  unknown {row['field']}: {row['reason']}")

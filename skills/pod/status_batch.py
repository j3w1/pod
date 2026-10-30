"""Bounded public full-status pages over one complete, revision-bound cohort.

Only common configuration/bundle/repository discovery is shared. Every named
record goes through the singular status implementation, including its native,
source, ownership and proof checks. No status or native evidence is cached.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from .errors import PodError
from . import listing
from .github import repository_context, repository_reads
from .ledger import state_root
from .orca import DEADLINE_SPENT, MAX_OUTPUT, _DEADLINE, current_run, read_deadline
from .status import StatusInputs, status
from .util import digest
from .window import Budget

DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0  # The existing Git read bound; native reads retain their own 10 s cap.
CURSOR_BYTES = 256
PAGE_METADATA_RESERVE = 256


class ReadBudget(Budget):
    """The existing reader budget, intersected with the original outer deadline.

    Inherit check(need=...) and the canonical deadline code: window and handoff
    readers reserve time and catch that protocol before a verifier can start.
    """

    def __init__(self, seconds: float):
        self._parent = _DEADLINE.get()
        super().__init__(seconds)

    def remaining(self) -> float:
        left = super().remaining()
        return min(left, self._parent.remaining()) if self._parent is not None else left


def _public_code(code: str) -> str:
    return "status_deadline" if code == DEADLINE_SPENT else code


def _bytes(value: dict) -> int:
    # The public CLI uses this exact JSON encoding and adds one newline.
    total = 1
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False)
    for chunk in encoder.iterencode(value):
        if len(chunk) > MAX_OUTPUT - total:
            return MAX_OUTPUT + 1
        total += len(chunk.encode("utf-8"))
        if total > MAX_OUTPUT:
            return MAX_OUTPUT + 1
    return total


def _cursor(cohort: str, offset: int) -> str:
    raw = json.dumps({"v": 1, "cohort": cohort, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _offset(value: str | None, cohort: str, total: int) -> int:
    if value is None:
        return 0
    try:
        if not isinstance(value, str) or not value or len(value.encode()) > CURSOR_BYTES:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(raw)
        if (not isinstance(cursor, dict) or set(cursor) != {"v", "cohort", "offset"}
                or type(cursor["v"]) is not int or cursor["v"] != 1
                or type(cursor["offset"]) is not int or not 0 < cursor["offset"] < listing.MAX_SCAN
                or not isinstance(cursor["cohort"], str)
                or value != _cursor(cursor["cohort"], cursor["offset"])):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PodError("status_cursor_invalid", "Use the exact next_cursor from the preceding page") from None
    if cursor["cohort"] != cohort:
        raise PodError("status_cohort_changed", "The cohort or its discovery inputs changed; discard earlier pages")
    if cursor["offset"] >= total:
        raise PodError("status_cursor_invalid", "The cursor offset lies outside its cohort")
    return cursor["offset"]


def _stamp(path: Path) -> list | None:
    try:
        row = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return ["unreadable"]
    return [row.st_dev, row.st_ino, row.st_mode, row.st_size, row.st_mtime_ns, row.st_ctime_ns]


def _record_stamp(path: Path) -> str:
    root = _stamp(path)
    # Do not traverse a redirected record directory, even for metadata.
    files = {} if root is None or len(root) < 3 or not stat.S_ISDIR(root[2]) else {
        name: _stamp(path / name) for name in
        ("context.json", "governor.json", "closure-evidence.json", "history")}
    return digest({"root": root, "files": files})


def _record_name(name: str) -> tuple[str, bool]:
    try:
        name.encode("utf-8")
        return name, True
    except UnicodeError:
        return "sha256:" + hashlib.sha256(os.fsencode(name)).hexdigest(), False


def _inventory(project: Path, context: dict, budget: ReadBudget) -> dict:
    """The existing membership reader, before any summary presentation limit.

    The scan retains the existing directory bound. An unreadable/unattributed
    record has unknown membership and is an explicit failure, never omitted.
    """
    root = state_root(project)
    entries = []
    try:
        if root.is_symlink():
            raise OSError("redirected")
        with os.scandir(root) as stream:
            for entry in stream:
                budget.check()
                if not entry.name.startswith("."):
                    entries.append(entry)
                    if len(entries) > listing.MAX_SCAN:
                        return {"records": [], "complete": False, "reason": "scan_limit",
                                "scanned": listing.MAX_SCAN, "total": None}
    except FileNotFoundError:
        return {"records": [], "complete": True, "reason": None, "scanned": 0, "total": 0}
    except OSError:
        return {"records": [], "complete": False, "reason": "unreadable_root", "scanned": 0, "total": None}
    records = []
    for entry in sorted(entries, key=lambda row: os.fsencode(row.name)):
        budget.check()
        name, encodable = _record_name(entry.name)
        before = _record_stamp(Path(entry.path))
        observed = listing.read_record(project, context, entry) if encodable else {"error": "path_encoding"}
        budget.check()
        after = _record_stamp(Path(entry.path))
        if observed is None:
            continue
        raw = observed.get("raw") or {}
        objective = observed.get("objective")
        problem = observed.get("error")
        if objective is not None and (len(objective) > 4096 or not _record_name(objective)[1]):
            objective, problem = None, "objective_identity_invalid"
        revision = raw.get("revision")
        records.append({"record": name, "objective": objective,
                        "revision": revision if type(revision) is int and revision >= 0 else None,
                        "fingerprint": after,
                        "error": "changed_during_read" if before != after else problem})
    records.sort(key=lambda row: (row["objective"] or "", row["record"]))
    return {"records": records, "complete": True, "reason": None,
            "scanned": len(entries), "total": len(records)}


def _binding() -> dict:
    try:
        return current_run()
    except PodError as exc:
        return {"unavailable": exc.code, "native_code": (exc.detail or {}).get("native_code")}


def _scope(context: dict) -> dict:
    return {key: context.get(key) for key in ("repository", "repo_key", "worktree", "branch")}


def _failure(entry: dict, code: str, reason: str, *, report=None, error_type=None) -> dict:
    code = _public_code(code)
    reason = reason.encode("utf-8", errors="replace").decode("utf-8")[:512]
    code = code if isinstance(code, str) and code.isascii() and len(code) <= 128 else "status_unavailable"
    return {"record": entry["record"], "objective": entry["objective"], "revision": entry["revision"],
            "outcome": "failure", "exit_code": 1, "report": report,
            "failure": {"code": code, "reason": reason, "error_type": error_type}}


def _read_one(project: Path, entry: dict, inputs: StatusInputs, budget: ReadBudget) -> dict:
    if entry["error"] is not None:
        return _failure(entry, entry["error"], "The cohort membership read could not read this record")
    try:
        budget.check()
        report = status(project, None, objective=entry["objective"], read_context=inputs)
        budget.check()  # A late complete JSON value is not a timely status result.
        _bytes(report)  # Invalid JSON/UTF-8 is a typed record failure, not a broken whole response.
        if not isinstance(report, dict) or report.get("schema") != "pod-cli/v5":
            return _failure(entry, "status_contract", "The singular status returned no full supported report")
        if report.get("status") != "observed":
            code = report.get("blocker") or (report.get("error") or {}).get("code") or "status_unavailable"
            return _failure(entry, code, "The singular status returned a nonzero full-status outcome", report=report)
        if report.get("objective") != entry["objective"] or not isinstance(report.get("readiness"), dict):
            return _failure(entry, "status_contract", "The singular status returned a partial or mismatched report",
                            report=report)
        return {"record": entry["record"], "objective": entry["objective"], "revision": entry["revision"],
                "outcome": "status", "exit_code": 0, "report": report, "failure": None}
    except PodError as exc:
        return _failure(entry, exc.code, str(exc)[:512])
    except Exception as exc:
        # One corrupt record or unexpected projection error cannot erase other
        # records. The error remains nonzero; process-control exceptions escape.
        return _failure(entry, "status_read_failed", "The singular status read failed", error_type=type(exc).__name__)


def _page(result: dict, offset: int, total: int, cohort: str, page_size: int) -> None:
    following = offset + len(result["records"])
    result["page"] = {"offset": offset, "count": len(result["records"]), "limit": page_size,
                      "next_cursor": _cursor(cohort, following) if following < total else None,
                      "end": following == total}


def _page_bytes(result: dict, payload_bytes: int) -> int:
    """Measure each report once; repeatedly encoding the growing page is quadratic."""
    return (_bytes({**result, "records": []}) + payload_bytes
            + 2 * max(0, len(result["records"]) - 1) + PAGE_METADATA_RESERVE)


def _full_status(project: Path, *, cursor: str | None, page_size: int, timeout: float) -> dict:
    """Return one bounded full-status page, or explicit incomplete/changed facts.

    A complete cohort is not a usable-admission verdict. Clients must consume
    every contiguous page and apply the unchanged checks to each full report.
    Native/process limits remain in their authoritative adapters.
    """
    if type(page_size) is not int or not 1 <= page_size <= listing.MAX_ROWS:
        raise PodError("status_page_size", f"Page size must be between 1 and {listing.MAX_ROWS}")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_TIMEOUT):
        raise PodError("status_timeout", f"A positive remaining deadline of at most {MAX_TIMEOUT:g} seconds is required")
    target = project.expanduser().resolve()
    if not target.is_dir():
        raise PodError("list_scope_unavailable", "The full-status repository path is not a directory")
    budget = ReadBudget(timeout)
    with read_deadline(budget), repository_reads(isolated=True):
        context = repository_context(target)
        if context.get("repo_key") is None:
            raise PodError("repository_identity_unavailable", "A full-status cohort needs a real repository identity")
        inventory = _inventory(target, context, budget)
        inputs, native = StatusInputs.read(target), _binding()
        budget.check()
        scope = _scope(context)
        cohort = digest({"scope": scope, "inventory": inventory, "inputs": inputs.identity(), "binding": native})
        total = len(inventory["records"])
        offset = _offset(cursor, cohort, total)
        result = {"schema": "pod-status-batch/v1", "status": "incomplete", "complete": False,
                  "scope": scope, "cohort": {"id": cohort, "total": inventory["total"],
                                             "enumeration_complete": inventory["complete"],
                                             "membership_complete": inventory["complete"] and
                                             all(row["objective"] is not None for row in inventory["records"]),
                                             "scanned": inventory["scanned"], "scan_limit": listing.MAX_SCAN},
                  "records": [], "page": {}, "reason": inventory["reason"],
                  "deadline_seconds": timeout, "output_limit_bytes": MAX_OUTPUT}
        payload_bytes = 0
        for entry in inventory["records"][offset:offset + page_size]:
            row = _read_one(target, entry, inputs, budget)
            result["records"].append(row)
            proposed_bytes = payload_bytes + _bytes(row) - 1
            _page(result, offset, total, cohort, page_size)
            if _page_bytes(result, proposed_bytes) > MAX_OUTPUT:
                result["records"].pop()
                if result["records"]:
                    break  # This record is still the next page's first record.
                result["records"].append(_failure(entry, "status_report_too_large",
                                                  "The full singular report cannot fit the existing output bound"))
                proposed_bytes = _bytes(result["records"][0]) - 1
                _page(result, offset, total, cohort, page_size)
                if _page_bytes(result, proposed_bytes) > MAX_OUTPUT:
                    raise PodError("status_output_limit", "Even bounded page metadata exceeds the existing output limit")
            payload_bytes = proposed_bytes
        _page(result, offset, total, cohort, page_size)
        failure = None
        try:
            budget.check()
            # An independent fresh scan/config/binding fence: no mixed revision
            # cohort, no silently continued page after a source/runtime change.
            fresh_context = repository_context(target, fresh=True)
            fresh_inventory = _inventory(target, fresh_context, budget)
            fresh_inputs, fresh_native = StatusInputs.read(target), _binding()
            budget.check()
            if (scope != _scope(fresh_context) or inventory != fresh_inventory
                    or inputs.identity() != fresh_inputs.identity() or native != fresh_native):
                failure = "status_cohort_changed"
        except PodError as exc:
            failure = _public_code(exc.code)
        result["page"]["valid"] = failure is None and inventory["complete"]
        if failure is not None:
            result["reason"] = failure
            result["page"]["next_cursor"] = None
            invalidated = []
            for row in result["records"]:
                invalid = _failure(row, failure, "The page's final currency/deadline fence failed",
                                   report=row["report"])
                if row["failure"] is not None:
                    invalid["failure"] = row["failure"]
                invalid["page_failure"] = {"code": failure,
                                           "reason": "The page's final currency/deadline fence failed"}
                invalidated.append(invalid)
            result["records"] = invalidated
        elif inventory["complete"]:
            failed = any(row["outcome"] == "failure" for row in result["records"])
            whole = offset == 0 and result["page"]["end"]
            result.update(status="incomplete" if failed else "observed" if whole else "page",
                          complete=whole and result["cohort"]["membership_complete"],
                          reason="record_failure" if failed else None)
        if _bytes(result) > MAX_OUTPUT:
            # Failure envelopes can be larger than successful row metadata.
            # Keep the typed failure and identity; omit the diagnostic payload.
            for row in result["records"]:
                row["report"] = None
            if _bytes(result) > MAX_OUTPUT:
                raise PodError("status_output_limit", "The typed page exceeds the existing output limit")
        return result


def full_status(project: Path, *, cursor: str | None = None, page_size: int = listing.MAX_ROWS,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Public deadline spelling belongs here; inner readers use their existing protocol."""
    try:
        return _full_status(project, cursor=cursor, page_size=page_size, timeout=timeout)
    except PodError as exc:
        if exc.code == DEADLINE_SPENT:
            raise PodError("status_deadline", str(exc), exc.detail) from None
        raise

"""Bounded packets, observations and candidate-bound evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import stat
from typing import Any

from .errors import PodError
from .util import bounded_text, digest, exact

PACKET_FIELDS = {"schema", "objective", "criteria", "responsibility", "scope", "actions", "candidate", "context", "dependencies", "route", "policy_revision", "plan_revision", "report_contract", "sources"}
REPORT_FIELDS = {"schema", "assignment", "attempt", "candidate", "outcome", "scope", "files", "checks", "failures", "evidence", "uncertainty", "questions"}
EVIDENCE_FIELDS = {"schema", "criterion", "candidate", "sources", "policy_revision", "dependencies", "environment", "check", "command", "result", "timestamp", "status", "reference", "reviewer_attempt"}


def _list(value: Any, name: str, limit: int = 64) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise PodError("invalid_" + name, f"{name} must be a bounded list")
    return value


def _safe_relative(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 512
            and not Path(value).is_absolute() and ".." not in Path(value).parts
            and not any(part.lower() in (".ssh", ".secrets", "secrets", "credentials")
                        or part.lower().startswith(".env")
                        or part.lower().endswith((".key", ".pem", ".p12"))
                        for part in Path(value).parts))


def _strings(value: Any, name: str) -> None:
    for item in _list(value, name):
        bounded_text(item, name=name, limit=512)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def packet(value: Any) -> dict:
    p = exact(value, PACKET_FIELDS, PACKET_FIELDS, name="packet")
    if p["schema"] != "pod-packet/v1":
        raise PodError("invalid_packet", "Unsupported packet schema")
    for key in ("objective", "responsibility", "candidate", "policy_revision", "plan_revision", "report_contract"):
        bounded_text(p[key], name=key)
    for key in ("criteria", "actions", "dependencies"):
        _strings(p[key], key)
    for item in _list(p["scope"], "scope"):
        if not _safe_relative(item):
            raise PodError("invalid_scope", "Packet scope must be a safe relative path")
    for item in _list(p["context"], "context"):
        ref = exact(item, {"kind", "path", "sha256", "id", "binding_digest"}, {"kind"}, name="context_reference")
        if ref["kind"] in ("source", "instruction"):
            if set(ref) != {"kind", "path", "sha256"} or not _safe_relative(ref["path"]):
                raise PodError("invalid_context", "Context source must be a bound safe path")
            if not _sha256(ref["sha256"]):
                raise PodError("invalid_context", "Context source digest is invalid")
        elif ref["kind"] == "summary":
            if set(ref) != {"kind", "id", "binding_digest"}:
                raise PodError("invalid_context", "Summary context must be a bound reference")
            bounded_text(ref["id"], name="summary id", limit=128)
            if not _sha256(ref["binding_digest"]):
                raise PodError("invalid_context", "Summary binding digest is invalid")
        else:
            raise PodError("invalid_context", "Unsupported context reference")
    for item in _list(p["sources"], "sources"):
        ref = exact(item, {"path", "state", "sha256"}, {"path", "state"}, name="source")
        if not _safe_relative(ref["path"]) or ref["state"] not in ("present", "absent", "unavailable"):
            raise PodError("invalid_source", "Packet source is not a safe bound path")
        if ref["state"] == "present" and not _sha256(ref.get("sha256")):
            raise PodError("invalid_source", "Present source needs a content digest")
    route = exact(p["route"], {"alias", "agent", "model", "account", "bucket", "effort"}, {"agent"}, name="packet_route")
    for key, value in route.items():
        if value is not None:
            bounded_text(value, name="route " + key, limit=256)
    if len(str(p)) > 65536:
        raise PodError("invalid_packet", "Packet is too large")
    return {"packet_id": digest(p), "body": p}


def report(value: Any, frozen: dict, native_binding: dict) -> dict:
    if not isinstance(frozen, dict) or not isinstance(frozen.get("body"), dict) or digest(frozen["body"]) != frozen.get("packet_id"):
        raise PodError("report_binding_mismatch", "Frozen packet content identity changed")
    packet(frozen["body"])
    r = exact(value, REPORT_FIELDS, REPORT_FIELDS, name="report")
    if r["schema"] != "pod-report/v1" or r["candidate"] != frozen["body"]["candidate"] or r["assignment"] != frozen["packet_id"]:
        raise PodError("report_binding_mismatch", "Report does not bind the frozen packet")
    for key in ("assignment", "attempt", "candidate"):
        bounded_text(r[key], name=key, limit=256)
    if r["outcome"] not in ("succeeded", "failed", "partial", "blocked", "uncertain"):
        raise PodError("invalid_report", "Report outcome is invalid")
    for key in ("scope", "files", "checks", "failures", "evidence", "uncertainty", "questions"):
        _strings(r[key], key)
    binding = exact(native_binding, {"runtime", "runId", "taskId", "dispatchId", "workerId"},
                    {"runtime", "runId", "taskId", "dispatchId", "workerId"}, name="native_binding")
    if any(not isinstance(value, str) or not value for value in binding.values()) or r["attempt"] != binding["dispatchId"]:
        raise PodError("report_attempt_mismatch", "Report attempt does not join the issued Dispatch")
    assigned = frozen["body"]["scope"]
    deviations = []
    if set(r["scope"]) != set(assigned):
        deviations.append("reported_scope_changed")
    for path in r["files"]:
        if not _safe_relative(path) or not any(path == base or path.startswith(base.rstrip("/") + "/") for base in assigned):
            deviations.append("file_outside_assignment")
    if deviations:
        return {"status": "reconciliation_required", "deviations": sorted(set(deviations)),
                "observation": r, "native_binding": binding}
    return {"status": "validated_observation", "observation": r, "native_binding": binding}


def evidence(value: Any, *, criterion: str, candidate: str, policy_revision: str,
             sources: list | None = None, dependencies: list | None = None,
             environment: str | None = None) -> dict:
    e = exact(value, EVIDENCE_FIELDS, EVIDENCE_FIELDS - {"reviewer_attempt"}, name="evidence")
    if e["schema"] != "pod-evidence/v1" or e["criterion"] != criterion or e["candidate"] != candidate or e["policy_revision"] != policy_revision:
        raise PodError("stale_evidence", "Evidence does not bind current criterion, candidate and policy")
    if (sources is not None and e["sources"] != sources or
            dependencies is not None and e["dependencies"] != dependencies or
            environment is not None and e["environment"] != environment):
        raise PodError("stale_evidence", "Evidence source, dependency or environment binding changed")
    if e["status"] not in ("PASS", "FAILED", "NOT_RUN", "UNAVAILABLE"):
        raise PodError("invalid_evidence", "Invalid evidence status")
    return e


def source_identity(root: Path, relative: str, *, max_bytes: int = 1_048_576) -> dict:
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise PodError("unsafe_source", "Project root is redirected")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise PodError("unsafe_source", "Source path must stay relative to project")
    if not _safe_relative(relative):
        raise PodError("secret_source", "Secret-bearing source is outside Pod context collection")
    cursor = root
    for component in Path(relative).parts[:-1]:
        cursor = cursor / component
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()) or not cursor.is_dir():
            raise PodError("unsafe_source", "Source parent is redirected or unavailable")
    path = root / relative
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise PodError("unsafe_source", "Source is redirected")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"path": relative, "state": "absent"}
    except OSError:
        return {"path": relative, "state": "unavailable"}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise PodError("unsafe_source", "Source must be a bounded regular file")
        data = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        if len(data) > max_bytes or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            return {"path": relative, "state": "unavailable"}
        return {"path": relative, "state": "present", "sha256": hashlib.sha256(data).hexdigest()}
    finally:
        os.close(fd)


def verify_sources(root: Path, bound: list[dict]) -> None:
    for entry in bound:
        exact(entry, {"path", "state", "sha256"}, {"path", "state"}, name="source")
        observed = source_identity(root, entry["path"])
        if observed["state"] == "unavailable":
            raise PodError("source_unavailable", "Source observation is unavailable")
        if observed != entry:
            raise PodError("source_changed", "Bound source has changed or disappeared")


def acceptance(criteria: list[str], evidence_rows: list[dict], *, candidate: str, policy_revision: str,
               sources: list, dependencies: list, environment: str,
               review_required: bool, hosted_required: bool) -> dict:
    """Project-criteria projection. Caller records never confer external acceptance."""
    checks = {}
    for criterion in criteria:
        rows = [e for e in evidence_rows if e.get("criterion") == criterion]
        checks[criterion] = [evidence(e, criterion=criterion, candidate=candidate,
                                      policy_revision=policy_revision, sources=sources,
                                      dependencies=dependencies, environment=environment)["status"] for e in rows]
    checks_pass = bool(criteria) and all("PASS" in checks[c] and "FAILED" not in checks[c] for c in criteria)
    review = "NOT_REQUIRED" if not review_required else "NOT_RUN"
    hosted = "NOT_REQUIRED" if not hosted_required else "NOT_RUN"
    for kind in ("review", "hosted"):
        row = [e for e in evidence_rows if e.get("criterion") == f"gate:{kind}"]
        if row:
            statuses = [evidence(e, criterion=f"gate:{kind}", candidate=candidate,
                                 policy_revision=policy_revision, sources=sources,
                                 dependencies=dependencies, environment=environment)["status"] for e in row]
            state = "RECORDED_PASS_UNVERIFIED" if "PASS" in statuses and "FAILED" not in statuses else "BLOCKED"
            if kind == "review" and review_required:
                review = state
            if kind == "hosted" and hosted_required:
                hosted = state
    return {"implemented": "unassessed", "required_checks_pass": checks_pass,
            "independently_reviewed": review, "hosted_proof_complete": hosted,
            "accepted": False, "merged": False, "released": False,
            "criteria": checks, "project_assessment_required": True}

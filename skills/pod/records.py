"""Bounded packets, observations and candidate-bound evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import ExitStack
import hashlib
import errno
import os
import stat
from typing import Any

from .errors import PodError
from .util import bounded_text, digest, exact

import re

_HEX40 = re.compile(r"[0-9a-f]{40}")

PACKET_SCHEMA = "pod-packet/v3"
PACKET_FIELDS = {"schema", "objective", "criteria", "responsibility", "scope", "actions",
                 "candidate", "context", "dependencies", "route", "policy_revision",
                 "plan_revision", "report_contract", "sources", "objective_source", "worktree",
                 "placement", "serves", "role", "boundary", "map_revision", "resolves",
                 "stop_condition", "delta_from"}
PACKET_REQUIRED = PACKET_FIELDS - {"objective_source", "worktree", "placement", "resolves",
                                   "stop_condition", "delta_from"}
REPORT_FIELDS = {"schema", "assignment", "attempt", "candidate", "outcome", "scope", "files", "checks", "failures", "evidence", "uncertainty", "questions"}
EVIDENCE_FIELDS = {"schema", "criterion", "candidate", "sources", "policy_revision", "dependencies", "environment", "check", "command", "result", "timestamp", "status", "reference", "reviewer_attempt", "definition"}


def _list(value: Any, name: str, limit: int = 64) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise PodError("invalid_" + name, f"{name} must be a bounded list")
    return value


_CREDENTIAL_COMPONENTS = frozenset({
    ".ssh", ".secrets", "secrets", "credentials", ".credentials",
    ".aws", ".azure", ".kube", ".docker", ".gnupg", ".password-store",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    ".authinfo", ".authinfo.gpg", ".pgpass", "pgpass.conf", ".my.cnf",
    ".dockercfg", "auth.json", "auth.yaml", "auth.yml",
    "credential.json", "credentials.json", "credentials.yaml", "credentials.yml",
    "token.json", "tokens.json",
})
_CREDENTIAL_DIRECTORIES = frozenset({(".config", "gcloud"), (".config", "gh"),
                                     (".local", "share", "keyrings")})
_PRIVATE_KEY_BASES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")


def _excluded_source_path(value: str) -> bool:
    """Conservative conventional credential names, not general secret detection."""
    parts = tuple(part.casefold() for part in value.replace("\\", "/").split("/") if part)
    for part in parts:
        if (part in _CREDENTIAL_COMPONENTS or part.startswith(".env")
                or part.endswith((".key", ".pem", ".p12", ".pfx"))):
            return True
        if not part.endswith(".pub") and any(
                part == base or part.startswith((base + "_", base + "-", base + "."))
                for base in _PRIVATE_KEY_BASES):
            return True
    return any(parts[index:index + len(directory)] == directory
               for directory in _CREDENTIAL_DIRECTORIES
               for index in range(len(parts) - len(directory) + 1))


def _safe_relative(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 512
            and not Path(value).is_absolute() and ".." not in Path(value).parts
            and not _excluded_source_path(value))


def _strings(value: Any, name: str) -> None:
    for item in _list(value, name):
        bounded_text(item, name=name, limit=512)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def packet(value: Any) -> dict:
    p = exact(value, PACKET_FIELDS, PACKET_REQUIRED, name="packet")
    if p["schema"] != PACKET_SCHEMA:
        raise PodError("invalid_packet", f"Packet schema must be {PACKET_SCHEMA}")
    from .obligations import packet_binding
    packet_binding(p)
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
    if "objective_source" in p:
        from .github import validate_issue_binding
        validate_issue_binding(p["objective_source"])
    for field in ("worktree", "placement"):
        if field in p:
            worktree_binding(p[field])
    route_value = p["route"]
    route = exact(route_value, {"agent", "model", "effort", "context", "reason", "preference_revision"},
                  {"agent"} if isinstance(route_value, dict) and route_value.get("agent") == "direct" else
                  {"agent", "model", "effort", "context", "reason", "preference_revision"},
                  name="packet_route")
    for key, value in route.items():
        if value is not None:
            bounded_text(value, name="route " + key, limit=256)
    if len(str(p)) > 65536:
        raise PodError("invalid_packet", "Packet is too large")
    return {"packet_id": digest(p), "body": p}


def worktree_binding(value: Any) -> dict:
    """Exact Git repository, key, worktree path and branch of an objective or placement."""
    worktree = exact(value, {"repository", "repo_key", "path", "branch"},
                     {"repository", "repo_key", "path", "branch"}, name="worktree_binding")
    if ((worktree["repository"] is not None
         and (not isinstance(worktree["repository"], str)
              or worktree["repository"].count("/") != 1))
            or not _sha256(worktree["repo_key"])
            or not isinstance(worktree["path"], str) or not Path(worktree["path"]).is_absolute()
            or len(worktree["path"]) > 4096
            or (worktree["branch"] is not None
                and (not isinstance(worktree["branch"], str) or not worktree["branch"]
                     or len(worktree["branch"]) > 256))):
        raise PodError("invalid_worktree_binding", "Packet worktree identity is malformed")
    return worktree


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


def evidence_record(value: Any) -> dict:
    """Canonical detailed receipt shape, shared by acceptance and obligation maps.

    Map receipts additionally require a definition fingerprint; standalone acceptance
    has no obligation definition to join. A reference identifies one immutable receipt
    within its obligation, so a new observation needs a new reference.
    """
    e = exact(value, EVIDENCE_FIELDS, EVIDENCE_FIELDS - {"reviewer_attempt", "definition"}, name="evidence")
    if e["schema"] != "pod-evidence/v1" or e["status"] not in ("PASS", "FAILED", "NOT_RUN", "UNAVAILABLE"):
        raise PodError("invalid_evidence", "Invalid evidence schema or status")
    if "definition" in e and not _sha256(e["definition"]):
        raise PodError("invalid_evidence", "Evidence definition is a SHA256 fingerprint")
    return e


def evidence(value: Any, *, criterion: str, candidate: str, policy_revision: str,
             sources: list | None = None, dependencies: list | None = None,
             environment: str | None = None) -> dict:
    e = evidence_record(value)
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
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise PodError("unsafe_source", "Source path must stay relative to project")
    if "\x00" in relative:
        raise PodError("unsafe_source", "Source path contains an invalid component")
    if not _safe_relative(relative):
        raise PodError("secret_source", "Secret-bearing source is outside Pod context collection")
    if type(max_bytes) is not int or max_bytes < 0 or max_bytes > 1_048_576:
        raise PodError("unsafe_source", "Source read limit is invalid")
    nofollow, directory, nonblock = (getattr(os, name, 0) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
    if not (nofollow and directory and nonblock):
        raise PodError("source_unavailable", "No-follow descriptor-relative source access is unsupported on this host")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    absolute = Path(os.path.abspath(root))
    with ExitStack() as stack:
        try:
            parent = os.open(absolute.anchor, flags | directory)
            stack.callback(os.close, parent)
            # Start at the filesystem anchor: the project root's ancestors are
            # also pinned, so a parent replacement cannot redirect descent.
            for part in absolute.parts[1:]:
                parent = os.open(part, flags | directory, dir_fd=parent)
                stack.callback(os.close, parent)
            for part in Path(relative).parts[:-1]:
                try:
                    parent = os.open(part, flags | directory, dir_fd=parent)
                except FileNotFoundError:
                    return {"path": relative, "state": "absent"}
                stack.callback(os.close, parent)
            try:
                fd = os.open(Path(relative).name, flags | nonblock, dir_fd=parent)
            except FileNotFoundError:
                return {"path": relative, "state": "absent"}
            stack.callback(os.close, fd)
        except (NotImplementedError, TypeError) as exc:
            raise PodError("source_unavailable", "Descriptor-relative source access is unsupported") from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PodError("unsafe_source", "Source path contains a redirect or non-directory") from exc
            return {"path": relative, "state": "unavailable"}
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise PodError("unsafe_source", "Source must be a bounded regular file")
            chunks, remaining = [], max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        except OSError:
            return {"path": relative, "state": "unavailable"}
        data = b"".join(chunks)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(data) > max_bytes or len(data) != before.st_size or identity(before) != identity(after):
            return {"path": relative, "state": "unavailable"}
        return {"path": relative, "state": "present", "sha256": hashlib.sha256(data).hexdigest()}


def verify_sources(root: Path, bound: list[dict]) -> None:
    for entry in bound:
        exact(entry, {"path", "state", "sha256"}, {"path", "state"}, name="source")
        if entry["state"] == "unavailable":
            raise PodError("source_unbound", "Source needs a fresh actual binding")
        observed = source_identity(root, entry["path"])
        if observed["state"] == "unavailable":
            raise PodError("source_unavailable", "Source observation is unavailable")
        if observed["state"] == "absent":
            raise PodError("source_absent", "Bound source is absent")
        if observed != entry:
            raise PodError("source_changed", "Bound source has changed or disappeared")


def integration_observation(project: Path, candidate: str, *, base_ref: str = "origin/main") -> dict:
    """Read Git for whether a candidate is merged and tagged. It changes nothing."""
    import subprocess

    record = {"schema": "pod-integration-observation/v1", "candidate": candidate,
              "base_ref": base_ref, "ancestor_of_base": None, "tags": [], "reason": None}
    if not isinstance(candidate, str) or not _HEX40.fullmatch(candidate or ""):
        raise PodError("invalid_candidate", "Candidate must be a full Git commit id")
    def run(argv):
        return subprocess.run(["git", "-C", str(project), *argv], capture_output=True,
                              text=True, timeout=30, check=False)
    try:
        ancestor = run(["merge-base", "--is-ancestor", candidate, base_ref])
        if ancestor.returncode == 0:
            record["ancestor_of_base"] = True
        elif ancestor.returncode == 1:
            record["ancestor_of_base"] = False
        else:
            record["reason"] = "base_ref_unavailable"
        tags = run(["tag", "--points-at", candidate])
        if tags.returncode == 0:
            record["tags"] = sorted(line.strip() for line in tags.stdout.splitlines() if line.strip())[:16]
    except (OSError, subprocess.SubprocessError):
        record["reason"] = "git_unavailable"
    return record


def acceptance(criteria: list[str], evidence_rows: list[dict], *, candidate: str, policy_revision: str,
               sources: list, dependencies: list, environment: str,
               review_required: bool, hosted_required: bool,
               owner_acceptance: dict | None = None, integration: dict | None = None,
               label: dict | None = None, route_holds: list[dict] | None = None) -> dict:
    """Project-criteria projection.

    Caller records never confer a check result. Three facts are deliberately not derived
    from them: whether the owner accepted the candidate, which arrives as an explicit
    authorization record; whether it is merged or released, which is read from Git by
    `integration_observation`; and whether it is independently reviewed, which is the
    obligation map's label qualification (R88) and never a caller's review row.
    """
    checks = {}
    for criterion in criteria:
        rows = [e for e in evidence_rows if e.get("criterion") == criterion]
        checks[criterion] = [evidence(e, criterion=criterion, candidate=candidate,
                                      policy_revision=policy_revision, sources=sources,
                                      dependencies=dependencies, environment=environment)["status"] for e in rows]
    checks_pass = bool(criteria) and all("PASS" in checks[c] and "FAILED" not in checks[c] for c in criteria)
    qualification = label if isinstance(label, dict) else {
        "label": "WITHHELD", "assurance_unbound": [{"obligation": None, "gap": "none_recorded"}], "withdrawn": []}
    # A caller may add a review requirement but never waive one the bound map records.
    required = bool(review_required or qualification.get("required"))
    if qualification.get("label") == "QUALIFIED":
        review = "QUALIFIED"
    else:
        review = "WITHHELD" if required else "NOT_REQUIRED"
    hosted = "NOT_REQUIRED" if not hosted_required else "NOT_RUN"
    row = [e for e in evidence_rows if e.get("criterion") == "gate:hosted"]
    if row:
        statuses = [evidence(e, criterion="gate:hosted", candidate=candidate,
                             policy_revision=policy_revision, sources=sources,
                             dependencies=dependencies, environment=environment)["status"] for e in row]
        if hosted_required:
            hosted = "RECORDED_PASS_UNVERIFIED" if "PASS" in statuses and "FAILED" not in statuses else "BLOCKED"
    granted = None
    if owner_acceptance is not None:
        granted = exact(owner_acceptance, {"schema", "candidate", "policy_revision", "utc", "accepted_by"},
                        {"schema", "candidate", "policy_revision", "utc", "accepted_by"},
                        name="acceptance_authorization")
        if granted["schema"] != "pod-acceptance-authorization/v1":
            raise PodError("invalid_acceptance", "Acceptance authorization schema is unsupported")
        for field in ("candidate", "policy_revision", "utc", "accepted_by"):
            bounded_text(granted[field], name=field, limit=512)
        if granted["candidate"] != candidate or granted["policy_revision"] != policy_revision:
            granted = None
    observed = None
    if integration is not None:
        observed = exact(integration, {"schema", "candidate", "base_ref", "ancestor_of_base", "tags", "reason"},
                         {"schema", "candidate"}, name="integration")
        if observed["schema"] != "pod-integration-observation/v1" or observed["candidate"] != candidate:
            raise PodError("invalid_integration", "Integration observation does not bind this candidate")
    accepted = bool(checks_pass and granted is not None
                    and review in ("NOT_REQUIRED", "QUALIFIED")
                    and hosted in ("NOT_REQUIRED", "RECORDED_PASS_UNVERIFIED")
                    and not route_holds)
    return {"implemented": "unassessed", "required_checks_pass": checks_pass,
            "independently_reviewed": review,
            "assurance_unbound": [] if review == "QUALIFIED" else qualification.get("assurance_unbound", []),
            "hosted_proof_complete": hosted,
            "accepted": accepted,
            "route_holds": list(route_holds or []),
            "merged": bool(observed and observed.get("ancestor_of_base") is True),
            "released": bool(observed and observed.get("tags")),
            "criteria": checks, "project_assessment_required": granted is None}

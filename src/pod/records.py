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

PACKET_FIELDS = {"schema", "objective", "criteria", "responsibility", "scope", "actions", "candidate", "context", "dependencies", "route", "policy_revision", "plan_revision", "report_contract", "sources"}
REPORT_FIELDS = {"schema", "assignment", "attempt", "candidate", "outcome", "scope", "files", "checks", "failures", "evidence", "uncertainty", "questions"}
EVIDENCE_FIELDS = {"schema", "criterion", "candidate", "sources", "policy_revision", "dependencies", "environment", "check", "command", "result", "timestamp", "status", "reference", "reviewer_attempt"}


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
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise PodError("unsafe_source", "Source path must stay relative to project")
    if "\x00" in relative:
        raise PodError("unsafe_source", "Source path contains an invalid component")
    if os.name == "nt":
        import ntpath
        if ntpath.splitdrive(relative)[0] or ntpath.isabs(relative) or ":" in relative:
            raise PodError("unsafe_source", "Windows source path must be a plain relative file path")
    if not _safe_relative(relative):
        raise PodError("secret_source", "Secret-bearing source is outside Pod context collection")
    if type(max_bytes) is not int or max_bytes < 0 or max_bytes > 1_048_576:
        raise PodError("unsafe_source", "Source read limit is invalid")
    if os.name == "nt":
        return _source_identity_windows(root, relative, max_bytes)
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


def _source_identity_windows(root: Path, relative: str, max_bytes: int) -> dict:
    """Retain each non-reparse ancestor without delete sharing through the read."""
    import ctypes
    from ctypes import wintypes
    import ntpath

    read_data, read_attributes = 0x0001, 0x0080
    share_read, open_existing = 0x0001, 3
    backup_semantics, open_reparse = 0x02000000, 0x00200000
    reparse_attribute, directory_attribute, disk_type = 0x0400, 0x0010, 1

    class TagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", FileTime), ("accessed", FileTime),
                    ("written", FileTime), ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        close = kernel.CloseHandle
        tag_info = kernel.GetFileInformationByHandleEx
        file_info = kernel.GetFileInformationByHandle
        file_type = kernel.GetFileType
        read = kernel.ReadFile
    except (AttributeError, OSError) as exc:
        raise PodError("source_unavailable", "Required Windows no-follow handle primitives are unavailable") from exc
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    tag_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    tag_info.restype = wintypes.BOOL
    file_info.argtypes, file_info.restype = (wintypes.HANDLE, ctypes.POINTER(FileInfo)), wintypes.BOOL
    file_type.argtypes, file_type.restype = (wintypes.HANDLE,), wintypes.DWORD
    read.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    read.restype = wintypes.BOOL

    absolute = ntpath.abspath(os.fspath(root))
    drive, tail = ntpath.splitdrive(absolute)
    if not drive or drive.startswith("\\\\") or not tail.startswith("\\"):
        raise PodError("source_unavailable", "Retained local Windows ancestry is unavailable for this source")
    root_components = [drive + "\\"] + [part for part in tail.split("\\") if part]
    components = root_components + list(Path(relative).parts)
    source_component_start = len(root_components)
    handles: list[int] = []
    current = components[0]
    invalid = ctypes.c_void_p(-1).value
    try:
        for index, part in enumerate(components):
            if index:
                current = ntpath.join(current, part)
            final = index == len(components) - 1
            handle = create(current, read_attributes | (read_data if final else 0), share_read,
                            None, open_existing, backup_semantics | open_reparse, None)
            if handle == invalid:
                if index >= source_component_start and ctypes.get_last_error() in (2, 3):
                    return {"path": relative, "state": "absent"}
                return {"path": relative, "state": "unavailable"}
            handles.append(handle)
            tag = TagInfo()
            if not tag_info(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
                return {"path": relative, "state": "unavailable"}
            if tag.attributes & reparse_attribute:
                raise PodError("unsafe_source", "Source path contains a reparse point")
            if not final and not tag.attributes & directory_attribute:
                raise PodError("unsafe_source", "Source parent is not a directory")
            if final:
                if tag.attributes & directory_attribute or file_type(handle) != disk_type:
                    raise PodError("unsafe_source", "Source must be a regular disk file")
                before = FileInfo()
                if not file_info(handle, ctypes.byref(before)):
                    return {"path": relative, "state": "unavailable"}
                size = before.size_high << 32 | before.size_low
                if size > max_bytes:
                    raise PodError("unsafe_source", "Source must be a bounded regular file")
                chunks = []
                remaining = max_bytes + 1
                while remaining:
                    amount = min(65536, remaining)
                    buffer = ctypes.create_string_buffer(amount)
                    received = wintypes.DWORD()
                    if not read(handle, buffer, amount, ctypes.byref(received), None):
                        return {"path": relative, "state": "unavailable"}
                    if not received.value:
                        break
                    chunks.append(buffer.raw[:received.value])
                    remaining -= received.value
                after = FileInfo()
                if not file_info(handle, ctypes.byref(after)):
                    return {"path": relative, "state": "unavailable"}
                data = b"".join(chunks)
                stable = lambda s: (s.volume, s.index_high, s.index_low, s.size_high,
                                    s.size_low, s.written.high, s.written.low)
                if len(data) > max_bytes or len(data) != size or stable(before) != stable(after):
                    return {"path": relative, "state": "unavailable"}
                return {"path": relative, "state": "present", "sha256": hashlib.sha256(data).hexdigest()}
    finally:
        for handle in reversed(handles):
            close(handle)


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

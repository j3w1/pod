"""Verified immutable history: physical placement, never lifecycle or policy state.

Every consumer receives the complete logical context. Only typed positions can
reference history; all chunks are verified before logical validation or projection.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from contextlib import ExitStack, contextmanager
import errno
import secrets

from .errors import PodError
from .util import MAX_RECORD, encoded, digest, atomic_json

CONTEXT_LIMIT = 4 * MAX_RECORD
WARN_AT = CONTEXT_LIMIT // 2
COMPACT_AT = 5 * CONTEXT_LIMIT // 8
MAINTENANCE_LIMIT = 16 * CONTEXT_LIMIT
CHUNK_LIMIT = MAX_RECORD
PART_BYTES = 48 * 1024
MARKER = "$pod_history"
FORMAT = {"schema": "pod-history-refs/v1"}
HEX = re.compile(r"[a-f0-9]{64}\Z")


def _bytes(value):
    return encoded(value, compact=True)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fail(code, message):
    raise PodError(code, message)


def _check_read_deadline():
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    if budget is not None:
        budget.check()


def is_reference(value) -> bool:
    return isinstance(value, dict) and set(value) == {MARKER}


def _review_consumed(state, key, row):
    serves = row.get("serves")
    if not isinstance(serves, list) or len(serves) != 1:
        return False
    return any(ob.get("id") == serves[0] and ob.get("kind") == "assurance"
               and any(item.get("evidence", {}).get("attempt") == key
                       and ("reported_seq" in item or "accepted_seq" in item)
                       for item in ob.get("receipts", []))
               for ob in (state.get("checkpoint") or {}).get("obligations", []))


def _review_settled(state, key, row):
    """Any-outcome review: its served assurance holds its findings, each required one resolved."""
    serves = row.get("serves")
    obligations = (state.get("checkpoint") or {}).get("obligations", [])
    if (not isinstance(serves, list) or len(serves) != 1 or not isinstance(obligations, list)
            or not any(isinstance(ob, dict) and ob.get("id") == serves[0] and ob.get("kind") == "assurance"
                       for ob in obligations)):
        return False
    from .lineage import Lineage
    try:
        findings = Lineage(obligations).findings(serves[0])
    except (PodError, AttributeError, KeyError, TypeError):
        return False  # An unreadable lineage cannot prove the corrections settled.
    return all(isinstance(finding.get("resolved_seq"), int) for finding in findings
               if finding.get("attempt") == key and finding.get("triage") == "required_correction")


OUTCOMES = ("succeeded", "partial", "failed", "blocked", "uncertain")


def admission_movable(state, key, row, protected=frozenset()) -> bool:
    """Conservative, role-aware predicate for consumed superseded results.

    A consumed report proves prior native settlement; archiving does not
    change the admission's bound state or current native outstanding predicate.
    Any freshly observed outstanding/native or Governor dependency vetoes a move.
    """
    from .util import route_hold
    candidate = (state.get("checkpoint") or {}).get("candidate")
    report = row.get("report") or {}
    if (key in protected or row.get("state") not in ("closed", "bound")
            or not row.get("candidate") or row.get("candidate") == candidate
            or report.get("status") not in ("validated_observation", "reconciliation_required")
            or report.get("outcome") not in OUTCOMES
            or row.get("route_decision", {}).get("effective_unknown") is True
            or route_hold(row)
            or any(not failure.get("cleared_at") for failure in row.get("failures", []))):
        return False
    role = row.get("role")
    if role == "implement":
        return row.get("disposition") is not None
    if role == "investigate":
        return report.get("status") == "reconciliation_required" or report.get("outcome") == "succeeded"
    if role != "review" or report.get("status") != "validated_observation":
        return False
    if report.get("outcome") == "succeeded":
        return _review_consumed(state, key, row)
    return _review_settled(state, key, row)


def _positions(state):
    """Whitelisted slots only: never expand a marker in arbitrary user evidence."""
    if not isinstance(state, dict):
        return
    admissions = state.get("admissions")
    if isinstance(admissions, dict):
        for key in admissions:
            yield admissions, key
    checkpoint = state.get("checkpoint")
    if isinstance(checkpoint, dict):
        obligations = checkpoint.get("obligations")
        for index, ob in enumerate(obligations if isinstance(obligations, list) else []):
            yield obligations, index
            ob = obligations[index]  # Hydration of the outer row precedes nested positions.
            receipts = ob.get("receipts") if isinstance(ob, dict) else None
            if isinstance(receipts, list):
                for index in range(len(receipts)):
                    yield receipts, index
            findings = ob.get("findings") if isinstance(ob, dict) else None
            if isinstance(findings, list):
                for index in range(len(findings)):
                    yield findings, index
        history = checkpoint.get("governance_history")
        for field in ("candidates", "decisions"):
            entries = history.get(field) if isinstance(history, dict) else None
            if isinstance(entries, list):
                for index in range(len(entries)):
                    yield entries, index
        entries = checkpoint.get("revision_decisions")
        if isinstance(entries, list):
            for index in range(len(entries)):
                yield entries, index
    if "historical_retirement" in state:
        yield state, "historical_retirement"


def compact(logical_state, *, protected_admissions=(), force=False,
            archive_retirement=False, trigger_bytes=COMPACT_AT, current_bytes=0,
            protect_receipts=False, protected_candidates=(), maintenance=(), require_fit=True):
    """Return (physical record, {file_digest: exact_file_bytes}); never mutate input.

    Once triggered, move every eligible slot (hysteresis without a second arbitrary
    target). The logical admission/receipt/governance count contracts never change.
    Retirement-only fallback may move the immutable entry, never unsettled data.
    """
    if "history" in logical_state:
        _fail("history_invalid", "compact requires hydrated logical state")
    record = deepcopy(logical_state)
    if not force and not maintenance and len(_bytes(record)) < trigger_bytes:
        return record, {}
    chunks = {}

    def reference(value):
        proposed = {}
        ref = _reference(value, proposed)
        if len(_bytes(ref)) >= len(_bytes(value)):
            return deepcopy(value)
        chunks.update(proposed)
        return ref

    protected = frozenset(protected_admissions)
    movable = set()
    for key, row in logical_state.get("admissions", {}).items():
        if admission_movable(logical_state, key, row, protected):
            movable.add(key)
            record["admissions"][key] = reference(row)
    checkpoint = logical_state.get("checkpoint") or {}
    current = checkpoint.get("candidate")
    target = record.get("checkpoint") or {}
    logical_rows = {row["id"]: row for row in checkpoint.get("obligations", [])
                    if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for index, (old_ob, new_ob) in enumerate(zip(checkpoint.get("obligations", []), target.get("obligations", []))):
        if old_ob.get("state") in ("withdrawn", "renewed"):
            target["obligations"][index] = reference(old_ob)
            continue
        if old_ob.get("kind") == "assurance" and old_ob.get("findings") and type(checkpoint.get("seq")) is int:
            from .assurance import validate_findings
            try:
                validate_findings(old_ob, logical_rows, seq=checkpoint["seq"])
            except PodError:
                pass  # Unvalidated observations cannot authorize a new history position.
            else:
                for finding_index, finding in enumerate(old_ob["findings"]):
                    new_ob["findings"][finding_index] = reference(finding)
        selected = {json.dumps(item, sort_keys=True) for item in old_ob.get("evidence", [])}
        for index, receipt in enumerate(old_ob.get("receipts", [])):
            evidence = receipt.get("evidence") or {}
            candidate = evidence.get("candidate")
            # Current/selected proof stays inline, as do failed/unknown tests.
            if (protect_receipts or candidate in protected_candidates or not candidate or candidate == current
                    or json.dumps(evidence, sort_keys=True) in selected):
                continue
            if old_ob.get("kind") == "assurance":
                eligible = (receipt.get("reuse") is None and evidence.get("attempt") in movable
                            and ("reported_seq" in receipt or "accepted_seq" in receipt))
            else:
                eligible = evidence.get("status") == "PASS" and "accepted_seq" in receipt
            if eligible:
                new_ob["receipts"][index] = reference(receipt)
    old_history = checkpoint.get("governance_history") or {}
    for field in ("candidates", "decisions"):
        entries = old_history.get(field, [])
        for index, value in enumerate(entries[:-1]):
            if field == "candidates" and value == current:
                continue
            target["governance_history"][field][index] = reference(value)
    for index, value in enumerate(checkpoint.get("revision_decisions", [])[:-1]):
        target["revision_decisions"][index] = reference(value)
    if archive_retirement and "historical_retirement" in record:
        record["historical_retirement"] = reference(logical_state["historical_retirement"])
    if chunks:
        record["history"] = dict(FORMAT)
    if maintenance:
        record.setdefault("history", dict(FORMAT))["maintenance"] = _reference(list(maintenance), chunks)
    if not maintenance and len(_bytes(record)) >= len(_bytes(logical_state)):
        record, chunks = deepcopy(logical_state), {}
    if require_fit:
        _require_size(record, current_bytes=current_bytes)
    return record, chunks


def _reference(value, chunks):
    payload = _bytes(value)
    parts = []
    for offset in range(0, len(payload), PART_BYTES):
        chunk = _bytes({"schema": "pod-history-chunk/v1",
                        "data": base64.b64encode(payload[offset:offset + PART_BYTES]).decode("ascii")})
        address = _sha(chunk)
        chunks[address] = chunk
        parts.append(address)
    return {MARKER: {"sha256": _sha(payload), "parts": parts, "bytes": len(payload)}}


def largest_sections(value):
    return sorted(({"section": key, "bytes": len(_bytes(item))}
                   for key, item in value.items()), key=lambda row: row["bytes"], reverse=True)[:8]


def _require_size(record, *, current_bytes):
    proposed = len(_bytes(record))
    if proposed > CONTEXT_LIMIT:
        sections = largest_sections(record)
        raise PodError("record_too_large", f"Record size {current_bytes} -> {proposed} bytes exceeds limit "
                       f"{CONTEXT_LIMIT}; largest sections: "
                       + ", ".join(f"{row['section']}={row['bytes']}" for row in sections),
                       {"current_bytes": current_bytes, "proposed_bytes": proposed,
                        "limit_bytes": CONTEXT_LIMIT, "largest_sections": sections,
                        "next_action": "On waiting rows, unselect stale selected evidence with evidence: [] so their "
                                       "retained receipts can move to immutable history. Mechanically renew large "
                                       "rows through the checkpoint renew route so terminal rows can archive. "
                                       "Consume or triage reports, record implementation dispositions and settle "
                                       "governed effects through their supported paths. If the record is still over "
                                       "limit, project lead or coordinator: run internal record-compaction detection "
                                       "for this exact project and objective, then submit its exact scoped maintenance "
                                       "request; no owner decision is required."})


def _expand(reference, loader, cache):
    if not is_reference(reference):
        _fail("history_invalid", "Malformed history digest reference")
    ref = reference[MARKER]
    if (not isinstance(ref, dict) or set(ref) != {"sha256", "parts", "bytes"}
            or not isinstance(ref["sha256"], str) or not HEX.fullmatch(ref["sha256"])
            or type(ref["bytes"]) is not int or ref["bytes"] < 1
            or not isinstance(ref["parts"], list) or not ref["parts"]
            or len(ref["parts"]) != (ref["bytes"] + PART_BYTES - 1) // PART_BYTES
            or any(not isinstance(part, str) or not HEX.fullmatch(part) for part in ref["parts"])):
        _fail("history_invalid", "Malformed history digest reference")
    pieces = []
    for index, address in enumerate(ref["parts"]):
        if address not in cache:
            try:
                raw = loader(address)
            except FileNotFoundError:
                _fail("history_missing", "Referenced immutable history chunk is missing")
            except OSError as exc:
                raise PodError("history_unavailable", "History storage is unreadable") from exc
            if not isinstance(raw, bytes) or len(raw) > CHUNK_LIMIT or _sha(raw) != address:
                _fail("history_integrity", "History chunk size or digest differs")
            try:
                chunk = json.loads(raw.decode("utf-8"))
                if (not isinstance(chunk, dict) or set(chunk) != {"schema", "data"}
                        or chunk["schema"] != "pod-history-chunk/v1" or _bytes(chunk) != raw):
                    raise ValueError("shape/canonical bytes")
                cache[address] = base64.b64decode(chunk["data"], validate=True)
            except (UnicodeError, ValueError, TypeError):
                _fail("history_integrity", "History chunk cannot be decoded exactly")
        part = cache[address]
        if len(part) != min(PART_BYTES, ref["bytes"] - index * PART_BYTES):
            _fail("history_integrity", "History chunk sequence length differs")
        pieces.append(part)
    payload = b"".join(pieces)
    if len(payload) != ref["bytes"] or _sha(payload) != ref["sha256"]:
        _fail("history_integrity", "Hydrated value size or digest differs")
    try:
        value = json.loads(payload.decode("utf-8"))
        canonical = _bytes(value)
    except (UnicodeError, ValueError, RecursionError):
        _fail("history_integrity", "Hydrated JSON value cannot be decoded")
    if canonical != payload or is_reference(value):
        _fail("history_integrity", "Hydrated history is not a canonical nonrecursive value")
    return value


def _compaction_request(value):
    """Validate declared maintenance scope; this never authenticates a caller."""
    from .util import bounded_text, exact
    common = {"schema", "requested_by", "reason", "repository", "objective", "facts"}
    value = exact(value, common | {"owner", "relay"}, common, name="compaction request")
    if value["schema"] != "pod-compaction-request/v1" or value["requested_by"] not in ("coordinator", "project_lead"):
        _fail("compaction_decision_mismatch", "Unsupported compaction request kind")
    field = "owner" if value["requested_by"] == "coordinator" else "relay"
    exact(value, common | {field}, common | {field}, name="compaction request")
    texts = [(value["reason"], "reason", 1024), (value["objective"], "objective", 256)]
    if field == "owner":
        texts.append((value["owner"], "owner", 256))
    else:
        relay = exact(value["relay"], {"reference"}, {"reference"}, name="compaction relay")
        texts.append((relay["reference"], "reference", 512))
    for text, name, limit in texts:
        bounded_text(text, name=name, limit=limit)
        text.encode("utf-8")
    facts = exact(value["facts"], {"record", "revision", "input_sha256", "logical_sha256"},
                  {"record", "revision", "input_sha256", "logical_sha256"}, name="compaction facts")
    if (type(facts["revision"]) is not int or facts["revision"] < 0
            or any(not isinstance(item, str) or not HEX.fullmatch(item)
                   for item in (value["repository"], facts["record"], facts["input_sha256"], facts["logical_sha256"]))):
        _fail("compaction_decision_mismatch", "Malformed compaction request scope or facts")
    return value


def _maintenance_valid(receipts):
    from .util import owner_decision, DECISION_FIELDS
    if not isinstance(receipts, list) or not receipts:
        _fail("history_invalid", "Maintenance receipts must be a nonempty list")
    for receipt in receipts:
        if (not isinstance(receipt, dict) or set(receipt) != {"authority", "decision", "input_sha256", "logical_sha256"}
                or any(not isinstance(receipt[field], str) or not HEX.fullmatch(receipt[field])
                       for field in ("decision", "input_sha256", "logical_sha256"))
                or not isinstance(receipt["authority"], dict)):
            _fail("history_invalid", "Malformed immutable maintenance receipt")
        try:
            authority = receipt["authority"]
            if authority.get("schema") == "pod-compaction-request/v1":
                _compaction_request(authority)
                if (digest(authority) != receipt["decision"]
                        or any(authority["facts"][key] != receipt[key] for key in ("input_sha256", "logical_sha256"))):
                    raise ValueError("request digest or facts differ")
            else:
                if set(authority) != DECISION_FIELDS - {"relay"} or authority.get("provenance") != "user_direct":
                    raise ValueError("session authority")
                owner_decision(authority, kind="compaction", stored=True)
        except (PodError, ValueError, UnicodeError) as exc:
            raise PodError("history_invalid", "Malformed maintenance authority") from exc
    return receipts


def _hydrate(record, loader):
    state, cache, maintenance = deepcopy(record), {}, []
    if not isinstance(state, dict):
        return state, maintenance
    metadata = state.pop("history", None)
    if metadata is not None:
        if (not isinstance(metadata, dict) or set(metadata) - {"schema", "maintenance"}
                or metadata.get("schema") != FORMAT["schema"]):
            _fail("history_invalid", "Unsupported history reference format")
        if "maintenance" in metadata:
            maintenance = _maintenance_valid(_expand(metadata["maintenance"], loader, cache))
    elif "history" in record:
        _fail("history_invalid", "Unsupported history reference format")
    for parent, key in _positions(state):
        value = parent[key]
        if not isinstance(value, dict) or MARKER not in value:
            continue
        if metadata is None:
            _fail("history_invalid", "References require the physical-format discriminator")
        expanded = _expand(value, loader, cache)
        if (isinstance(expanded, dict) and any(is_reference(item)
                for field in ("receipts", "findings") for item in expanded.get(field, []))):
            _fail("history_integrity", "Whole history rows cannot contain nested references")
        parent[key] = expanded
    # Do not recursively expand pointers returned by a chunk, including inside typed rows.
    if any(is_reference(parent[key]) for parent, key in _positions(state)):
        _fail("history_integrity", "Recursive history references are forbidden")
    return state, maintenance


def hydrate(record, loader):
    """Verify all references, restoring exact logical positions, values and order."""
    return _hydrate(record, loader)[0]


class ChunkStore:
    """Objective-local chunks, opened through stable no-follow directory descriptors."""
    def __init__(self, objective_directory):
        self.root = Path(objective_directory) / "history" / "sha256"

    @contextmanager
    def directory(self, *, create=False):
        _check_read_deadline()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in self.root.absolute().parts[1:]:
                _check_read_deadline()
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except FileNotFoundError as exc:
            raise PodError("history_missing", "Referenced immutable history storage is missing") from exc
        except OSError as exc:
            code = "history_unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR) else "history_unavailable"
            raise PodError(code, "History storage cannot be safely accessed") from exc
        finally:
            os.close(fd)

    def _name(self, address):
        if not isinstance(address, str) or not HEX.fullmatch(address):
            _fail("history_invalid", "Chunk address must be a SHA256 digest")
        return address + ".json"

    def _load(self, directory, address):
        try:
            fd = os.open(self._name(address), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError as exc:
            raise PodError("history_missing", "Referenced immutable history chunk is missing") from exc
        except OSError as exc:
            code = "history_unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR) else "history_unavailable"
            raise PodError(code, "History chunk cannot be safely opened") from exc
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                _fail("history_unsafe", "History chunk must be a regular file")
            data = file.read(CHUNK_LIMIT + 1)
        if len(data) > CHUNK_LIMIT or _sha(data) != address:
            _fail("history_integrity", "Stored history size or digest differs")
        return data

    def load(self, address):
        with self.directory() as directory:
            return self._load(directory, address)

    def put(self, chunks):
        if not chunks:
            return
        with self.directory(create=True) as directory:
            for address, data in chunks.items():
                name = self._name(address)
                if not isinstance(data, bytes) or len(data) > CHUNK_LIMIT or _sha(data) != address:
                    _fail("history_integrity", "Proposed immutable chunk bytes differ from address")
                try:
                    prior = self._load(directory, address)
                except PodError as exc:
                    if exc.code != "history_missing":
                        raise
                else:
                    if prior != data:
                        _fail("history_integrity", "Existing immutable chunk differs")
                    continue
                temporary = ".pending-" + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as file:
                        file.write(data)
                        file.flush()
                        os.fsync(file.fileno())
                    try:
                        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        if self._load(directory, address) != data:
                            _fail("history_integrity", "Concurrent immutable chunk differs")
                finally:
                    os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)


def _context_bytes(path, limit):
    """Bound the actual read, retaining exact physical bytes for decision binding."""
    _check_read_deadline()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.absolute().parent.parts[1:]:
            _check_read_deadline()
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        _check_read_deadline()
        record = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(record, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                _fail("unsafe_record", "Context must be a regular, non-symlink file")
            _check_read_deadline()
            data = file.read(limit + 1)
        if len(data) > limit or info.st_size > limit:
            maintenance = limit >= MAINTENANCE_LIMIT
            raise PodError("record_too_large", "Context exceeds its bounded read limit; "
                           + ("bounded compaction cannot read it" if maintenance else
                              "use internal record-compaction detection for a project-lead or coordinator request"),
                           {"current_bytes": max(info.st_size, len(data)), "limit_bytes": limit,
                            "next_action": ("Project lead: preserve this record and arrange supported recovery; "
                                            "bounded compaction cannot read it." if maintenance else
                                            "Project lead or coordinator: run internal record-compaction detection for "
                                            "this exact project and objective, then submit its exact scoped maintenance "
                                            "request; no owner decision is required.")})
        return data
    except OSError as exc:
        raise PodError("unsafe_record", "Context cannot be safely opened") from exc
    finally:
        os.close(fd)


def _load(path, *, limit=CONTEXT_LIMIT, validate=True):
    raw = _context_bytes(path, limit)
    try:
        physical = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded context JSON") from exc
    store = ChunkStore(path.parent)
    # Open history only if referenced, pinning ancestry for the complete verification.
    with ExitStack() as stack:
        directory = None
        def read_chunk(address):
            nonlocal directory
            _check_read_deadline()
            if directory is None:
                directory = stack.enter_context(store.directory())
            _check_read_deadline()
            return store._load(directory, address)
        state, receipts = _hydrate(physical, read_chunk)
    if validate:
        from .ledger import _validate_context
        _validate_context(state)
    return state, receipts, raw


def load_context(path, *, validate=True):
    """The ordinary bounded boundary, also usable by raw-schema diagnostics."""
    return _load(path, validate=validate)[0]


def protections(path, state, *, native=None, protected=(), journal=None):
    """Only recorded Governor joins and supplied fresh native evidence add vetoes."""
    admissions = state.get("admissions", {})
    keep = set(protected)
    checkpoint = state.get("checkpoint") or {}
    for ob in checkpoint.get("obligations", []):
        if ob.get("state") in ("active", "waiting"):
            for reference in (ob.get("executor"), (ob.get("wait") or {}).get("referent")):
                if isinstance(reference, str) and reference in admissions:
                    keep.add(reference)
    keep.update((state.get("continued_by") or {}).get("unreported", []))
    if native is not None:
        from .ledger import _outstanding_ids
        keep.update(_outstanding_ids(state, native))
        for row in native.get("assignments", []):
            # An explicit fresh live/unverified observation vetoes even an earlier closed row.
            if isinstance(row, dict) and row.get("admission_id") in admissions and row.get("settled") is not True:
                keep.add(row["admission_id"])
    try:
        from .governor import _read_journal
        journal = _read_journal(path.parent / "governor.json") if journal is None else journal
        rows = [row for row in journal["actions"] if row["decision"] == "ALLOW"
                and row["outcome"] in ("pending", "UNKNOWN", "FAILED")]
        # A claim in progress must not hide any dependency it has yet to join.
        if (state.get("continues") or {}).get("state") == "claiming":
            return set(admissions), None
        for row in rows:
            action = row["action"]
            identifiers = {action.get(key) for key in ("candidate", "task", "task_id", "packet", "packet_id", "dispatch")
                           if isinstance(action.get(key), str) and action[key]}
            if not identifiers:
                return set(admissions), None
            for key, admission in admissions.items():
                binding = admission.get("native_binding") or {}
                if identifiers & {key, admission.get("candidate"), admission.get("task_id"),
                                  admission.get("packet_id"), binding.get("dispatchId")}:
                    keep.add(key)
        return keep, {row["action"]["candidate"] for row in rows}
    except (PodError, OSError, KeyError, TypeError):
        # Unreadable/ambiguous joins cannot authorize relocation of proof.
        return set(admissions), None


def prepare_context(path, state, *, purpose="ordinary", native=None, protected=(), force=False, maintenance=None, journal=None,
                    require_fit=True):
    """Pure physical planner, including R118's all-context capacity preflight."""
    current = path.stat().st_size if path.exists() else 0
    receipts = maintenance
    if receipts is None:
        receipts = _load(path)[1] if path.exists() else []
    keep, hold_receipts = protections(path, state, native=native, protected=protected, journal=journal)
    kwargs = {"protected_admissions": keep, "protect_receipts": hold_receipts is None,
              "protected_candidates": hold_receipts or (), "maintenance": receipts,
              "force": force or purpose == "retirement", "current_bytes": current, "require_fit": require_fit}
    try:
        return compact(state, **kwargs)
    except PodError as exc:
        if purpose != "retirement" or exc.code != "record_too_large":
            raise
    return compact(state, archive_retirement=True, **kwargs)


def publish_context(path, record, chunks):
    """Publish chunks without replacement before the atomic context replacement."""
    from .evidence_closure import refuse_closed
    refuse_closed(path.parent)  # Backstop: a record closed by evidence is never written again (R145).
    _require_size(record, current_bytes=path.stat().st_size if path.exists() else 0)
    ChunkStore(path.parent).put(chunks)
    atomic_json(path, record, limit=CONTEXT_LIMIT, compact=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _moved(record):
    counts = {"admissions": 0, "receipts": 0, "governance": 0, "revision_decisions": 0, "retirement": 0}
    checkpoint = record.get("checkpoint") or {}
    counts["admissions"] = sum(is_reference(row) for row in record.get("admissions", {}).values())
    counts["receipts"] = sum(is_reference(row) for ob in checkpoint.get("obligations", []) for row in ob.get("receipts", []))
    counts["governance"] = sum(is_reference(row) for entries in checkpoint.get("governance_history", {}).values() for row in entries)
    counts["revision_decisions"] = sum(is_reference(row) for row in checkpoint.get("revision_decisions", []))
    counts["retirement"] = int(is_reference(record.get("historical_retirement")))
    counts["obligations"] = sum(is_reference(row) for row in checkpoint.get("obligations", []))
    counts["findings"] = sum(is_reference(item) for row in checkpoint.get("obligations", []) for item in row.get("findings", []))
    return counts


def _settlement_action(sections):
    standing = any(row["section"] == "standing_authorization" for row in sections[:3])
    return (("The standing_authorization section is bounded and needs no maintenance. " if standing else "")
            + "Consume or triage reports, record implementation dispositions, or settle governed "
            "effects through their supported paths; largest sections: "
            + ", ".join(row["section"] for row in sections[:3]))


def capacity(path, state):
    """Additive observation only: never changes readiness, phase or existing actions."""
    _, receipts, raw = _load(path)
    logical = len(_bytes(state))
    result = {"physical_bytes": len(raw), "logical_bytes": logical, "limit_bytes": CONTEXT_LIMIT,
              "warning_bytes": WARN_AT, "compaction_bytes": COMPACT_AT, "warning": len(raw) >= WARN_AT,
              "largest_sections": largest_sections(state), "maintenance": receipts}
    if result["warning"]:
        try:
            planned, _ = prepare_context(path, state, force=True, maintenance=receipts)
            useful = len(_bytes(planned)) < len(raw)
        except PodError as exc:
            if exc.code != "record_too_large":
                raise
            useful = False
        result["reclaimable_bytes"] = len(raw) - len(_bytes(planned)) if useful else 0
        result["next_action"] = (f"A permitted write compacts settled history automatically once its record reaches {COMPACT_AT} bytes; project lead "
                                 "or coordinator: run internal record-compaction detection, then submit its exact scoped "
                                 "maintenance request for explicit bounded maintenance now. The project lead handles capacity "
                                 "before closure." if useful else
                                 _settlement_action(result["largest_sections"]))
        if useful and any(row["section"] == "standing_authorization" for row in result["largest_sections"][:3]):
            result["next_action"] += " The standing_authorization section is bounded and needs no maintenance."
    else:
        result["next_action"] = f"Continue through the existing objective operations; settled history compacts once the record reaches {COMPACT_AT} bytes."
    return result


def record_compaction(project, objective, *, request=None, decision=None):
    """Declared project-lead or coordinator maintenance, never ordinary work or native authority."""
    from contextlib import nullcontext
    from .github import repository_context
    from .ledger import _path, _record_exists, _lock
    from .succession import refuse_continued
    from .util import bounded_text, exact, owner_decision, DECISION_FIELDS
    bounded_text(objective, name="objective", limit=256)
    context = repository_context(project, fresh=True)
    repository = context["repo_key"]
    if repository is None:
        _fail("compaction_scope_unavailable", "Record compaction needs the objective's Git repository")
    path = _path(project, objective)
    if not _record_exists(path):
        _fail("objective_unknown", "No record exists for this exact objective; nothing was created")
    from .evidence_closure import refuse_closed
    refuse_closed(path.parent)
    if request is not None and decision is not None:
        _fail("compaction_decision_mismatch", "Supply one scoped compaction request")
    if request is not None:
        try:
            request = _compaction_request(request)
        except (PodError, UnicodeError) as exc:
            raise PodError("compaction_decision_mismatch", "Compaction needs an exact coordinator or project-lead request") from exc
    if decision is not None:
        try:
            decision = exact(decision, DECISION_FIELDS | {"repository", "objective", "facts"},
                             {"provenance", "instruction", "repository", "objective", "facts"}, name="compaction decision")
            if decision["provenance"] != "user_direct":
                raise ValueError("provenance")
            bounded_text(decision["instruction"], name="instruction", limit=1024)
            decision = owner_decision(decision, kind="compaction")
        except (PodError, ValueError) as exc:
            raise PodError("compaction_decision_mismatch", "Compaction needs an exact session decision") from exc
    submitted = request if request is not None else decision
    with _lock(path) if submitted is not None else nullcontext():
        state, receipts, raw = _load(path, limit=MAINTENANCE_LIMIT)
        refuse_continued(state, reserved=False)
        checkpoint = state.get("checkpoint") or {}
        if checkpoint.get("objective") != objective:
            _fail("compaction_decision_mismatch", "The record does not name the requested objective")
        logical = digest(state)
        binding = {"record": path.parent.name, "revision": state["revision"], "input_sha256": _sha(raw),
                   "logical_sha256": logical}
        facts = {"repository": repository, "objective": objective, "owner": state["owner"], **binding}
        if submitted is not None:
            if request is not None and request["requested_by"] == "coordinator" and request["owner"] != state["owner"]:
                _fail("compaction_decision_mismatch", "The coordinator request does not name the recorded coordinator")
            same_scope = submitted["repository"] == repository and submitted["objective"] == objective
            prior = next((row for row in receipts if row["decision"] == digest(submitted)), None)
            if same_scope and prior is not None and prior["logical_sha256"] == logical:
                return {"status": "replayed", "facts": facts, "written": False}
            if not same_scope or submitted["facts"] != binding:
                _fail("compaction_decision_mismatch", "The request differs from the exact physical or logical record; detect again")
        # Reserve the largest complete request authority, including six-byte JSON
        # escapes in reason, coordinator identity and lead relay. Digests have
        # fixed width; receipt fragment references can grow at part boundaries.
        # The session-decision authority is smaller. A no-op needs no write capacity.
        authority = {"schema": "pod-compaction-request/v1", "reason": "\x01" * 1024,
                     "repository": repository, "objective": objective, "facts": binding}
        authorities = [{**authority, "requested_by": "coordinator", "owner": "\x01" * 256},
                       {**authority, "requested_by": "project_lead", "relay": {"reference": "\x01" * 512}}]
        allowance = {"authority": max(authorities, key=lambda value: len(_bytes(value))),
                     "decision": "0" * 64, "input_sha256": binding["input_sha256"], "logical_sha256": logical}
        record, chunks = prepare_context(path, state, force=True, maintenance=[*receipts, allowance], require_fit=False)
        useful = len(_bytes(record)) < len(raw)
        if useful:
            _require_size(record, current_bytes=len(raw))
        result = {"status": "compaction_available" if useful else "unchanged", "facts": facts, "current_bytes": len(raw),
                  "proposed_bytes": len(_bytes(record)), "limit_bytes": CONTEXT_LIMIT, "moved": _moved(record), "written": False,
                  "reclaimable_bytes": max(0, len(raw) - len(_bytes(record)))}
        if not useful:
            result["largest_sections"] = largest_sections(state)
            result["next_action"] = (_settlement_action(result["largest_sections"]) if len(raw) >= WARN_AT else
                                     "Continue through the existing objective operations; no settled history currently "
                                     "reduces this record after the maintenance receipt allowance.")
            return result
        if submitted is None:
            result["question"] = (f"Does the project lead or coordinator request lossless compaction for objective {objective}, "
                                  "preserving all recorded work and decisions and keeping unsettled items in its record?")
            result["next_action"] = {"operation": "record-compaction", "request": {"project": str(project), "objective": objective,
                "request": {"schema": "pod-compaction-request/v1", "requested_by": "coordinator",
                            "reason": "<reason for lossless compaction>", "owner": state["owner"],
                            "repository": repository, "objective": objective, "facts": binding}}}
            return result
        receipt = {"authority": request if request is not None else
                   {key: decision[key] for key in ("provenance", "instruction", "channel")},
                   "decision": digest(submitted), "input_sha256": binding["input_sha256"], "logical_sha256": logical}
        record, chunks = prepare_context(path, state, force=True, maintenance=[*receipts, receipt], require_fit=False)
        if len(_bytes(record)) >= len(raw):
            return {**result, "status": "unchanged", "proposed_bytes": len(_bytes(record)), "written": False,
                    "reclaimable_bytes": 0}
        publish_context(path, record, chunks)
        return {**result, "status": "compacted", "proposed_bytes": len(_bytes(record)), "written": True,
                "reclaimable_bytes": len(raw) - len(_bytes(record))}

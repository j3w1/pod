"""Verified immutable history: physical placement, never lifecycle or policy state.

Typed immutable pages preserve the logical context. Decision readers open only
relevant descendants; explicit audits verify the complete retained evidence.
"""
from __future__ import annotations

import base64
import fcntl
from functools import wraps
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
import time

from .errors import PodError
from .util import MAX_RECORD, encoded, digest, atomic_json

CONTEXT_LIMIT = 4 * MAX_RECORD
MAINTENANCE_LIMIT = 16 * CONTEXT_LIMIT
CHUNK_LIMIT = MAX_RECORD
PART_BYTES = 48 * 1024
MARKER = "$pod_history"
FORMAT = {"schema": "pod-history-refs/v1"}
HEX = re.compile(r"[a-f0-9]{64}\Z")
CLEANUP_TRIGGER_BYTES = 256 * 1024
CLEANUP_HINT_BYTES = 16 * 1024


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


def _reference(value, chunks):
    payload = _bytes(value)
    parts = []
    for offset in range(0, len(payload), PART_BYTES):
        part = payload[offset:offset + PART_BYTES]
        address = _sha(part)
        chunks[address] = part
        parts.append(address)
    return {MARKER: {"sha256": _sha(payload), "parts": parts, "bytes": len(payload), "encoding": "json"}}


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
                        "next_action": "run pod internal storage with action inspect for this project and objective, "
                                       "then action migrate with its exact revision and sha256; immutable revisions "
                                       "support growth without changing requirements or selected proof"})


def _reference_value(reference):
    if not is_reference(reference):
        _fail("history_invalid", "Malformed history digest reference")
    ref = reference[MARKER]
    if (not isinstance(ref, dict) or (set(ref) != {"sha256", "parts", "bytes"} and
            not (set(ref) == {"sha256", "parts", "bytes", "encoding"} and ref["encoding"] == "json"))
            or not isinstance(ref["sha256"], str) or not HEX.fullmatch(ref["sha256"])
            or type(ref["bytes"]) is not int or ref["bytes"] < 1
            or not isinstance(ref["parts"], list) or not ref["parts"]
            or len(ref["parts"]) != (ref["bytes"] + PART_BYTES - 1) // PART_BYTES
            or any(not isinstance(part, str) or not HEX.fullmatch(part) for part in ref["parts"])):
        _fail("history_invalid", "Malformed history digest reference")
    return ref


def _expand(reference, loader, cache):
    ref = _reference_value(reference)
    pieces = []
    for index, address in enumerate(ref["parts"]):
        _check_read_deadline()
        if address not in cache:
            try:
                raw = loader(address)
            except FileNotFoundError:
                _fail("history_missing", "Referenced immutable history chunk is missing")
            except OSError as exc:
                raise PodError("history_unavailable", "History storage is unreadable") from exc
            if not isinstance(raw, bytes) or len(raw) > CHUNK_LIMIT or _sha(raw) != address:
                _fail("history_integrity", "History chunk size or digest differs")
            if ref.get("encoding") == "json":
                cache[address] = raw
            else:
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
    from .util import owner_decision, DECISION_FIELDS, exact
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
                session_only = set(authority) == {"provenance", "instruction", "channel"} and authority.get("provenance") == "user_direct"
                if not session_only:
                    exact(authority, DECISION_FIELDS, {"provenance", "instruction", "channel"}, name="maintenance decision")
                    from .relay import validate_decision_record
                    validate_decision_record(authority, kind="compaction")
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
        if (not isinstance(metadata, dict) or set(metadata) - {"schema", "maintenance", "decision_channels"}
                or metadata.get("schema") != FORMAT["schema"]
                or "decision_channels" in metadata and metadata["decision_channels"] != 5):
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
    if isinstance(record, dict) and record.get("schema") == STORAGE_SCHEMA:
        from .history_tree import Reader, plain
        class InputStore:
            def load(self, address): return loader(address)
        return plain(Reader(InputStore()).value(record["root"]))
    return _hydrate(record, loader)[0]


class _ReadPin:
    """A shared lock on the stable objective directory, without creating a file.

    Publications may proceed while a reader holds old immutable references. Only
    reclamation needs the exclusive lock, so reads do not block semantic writes.
    """
    def __init__(self, store, *, create=False, collect=False):
        self.store, self.fd = store, None
        self.collect_on_close = collect
        self.scope = store.directory_path(store.root.parent.parent, create=create)
        self.fd = self.scope.__enter__()
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.scope.__exit__(None, None, None); self.fd = None
            raise PodError("history_busy", "Immutable storage is being reclaimed; retry a fresh read") from exc

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def close(self):
        if self.fd is None: return
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.scope.__exit__(None, None, None)
        self.fd = None
        if self.collect_on_close:
            self.store.maybe_collect()


def _pinned_read(function):
    @wraps(function)
    def call(path, *args, **kwargs):
        with ChunkStore(path.parent).pin():
            return function(path, *args, **kwargs)
    return call


class ChunkStore:
    """Objective-local chunks, opened through stable no-follow directory descriptors."""
    def __init__(self, objective_directory):
        self.root = Path(objective_directory) / "history" / "sha256"

    @contextmanager
    def directory_path(self, path, *, create=False):
        _check_read_deadline()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in Path(path).absolute().parts[1:]:
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
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise PodError("storage_full", "Immutable history publication has insufficient disk space; read back and retry") from exc
            code = "history_unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR) else "history_unavailable"
            raise PodError(code, "History storage cannot be safely accessed") from exc
        finally:
            os.close(fd)

    def directory(self, *, create=False):
        return self.directory_path(self.root, create=create)

    def pin(self, *, create=False, collect=False):
        return _ReadPin(self, create=create, collect=collect)

    def cleanup_info(self):
        """Bounded housekeeping observation; it is never evidence or a GC mark root."""
        path = self.root.parent.parent / ".history-gc"
        try:
            with self.directory_path(path.parent) as directory:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode): raise ValueError
                    raw = file.read(CLEANUP_HINT_BYTES + 1)
            value = json.loads(raw)
            if (len(raw) > CLEANUP_HINT_BYTES or not isinstance(value, dict)
                    or set(value) != {"queued_bytes", "roots", "untracked"}
                    or type(value["queued_bytes"]) is not int or value["queued_bytes"] < 0
                    or not isinstance(value["roots"], list) or len(value["roots"]) > 64
                    or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in value["roots"])
                    or type(value["untracked"]) is not bool): raise ValueError
            return {"pending": True, **value}
        except FileNotFoundError:
            return {"pending": False, "queued_bytes": 0, "roots": [], "untracked": False}
        except (OSError, PodError, ValueError, TypeError):
            pending = path.exists()
            return {"pending": pending, "queued_bytes": None if pending else 0, "roots": [], "untracked": pending}

    def request_collection(self, *, queued_bytes=None, replaced=()):
        # This is a bounded recovery hint, not evidence or a second authority.
        # A failed hint cannot roll back an already durable publication.
        try:
            info = self.cleanup_info()
            roots = list(dict.fromkeys([*info["roots"], *replaced]))
            value = {"queued_bytes": (info["queued_bytes"] or 0) + (queued_bytes or 0),
                     "roots": roots[-64:], "untracked": info["untracked"] or queued_bytes is None or len(roots) > 64}
            atomic_json(self.root.parent.parent / ".history-gc", value, limit=CLEANUP_HINT_BYTES, compact=True)
        except (OSError, PodError):
            return

    def maybe_collect(self):
        info = self.cleanup_info()
        if info["pending"] and (info["untracked"] or info["queued_bytes"] >= CLEANUP_TRIGGER_BYTES):
            return self.collect(seconds=5)

    def collect(self, *, seconds=300):
        """Mark both current authorities, then sweep only unreachable owned chunks.

        Live readers and publishers hold shared directory pins. If any remains,
        leave the hint for the writer's release or the next write. Read-only
        pin release never performs cleanup. A crash leaves the hint for the next publication. No primary changes.
        """
        if not (self.root.parent.parent / ".history-gc").exists():
            return {"status": "unchanged", "pending": False, "removed_bytes": 0}
        # Never scan inside a semantic writer's record lock. The objective pin
        # below serializes reclamation with readers and publishers instead.
        try:
            with self.directory_path(self.root.parent.parent) as directory:
                try: fd = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError: fd = None
                if fd is not None:
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally: os.close(fd)
        except (OSError, PodError, ValueError):
            return {"status": "deferred", "pending": True, "reason": "writer_active"}
        from .orca import _DEADLINE
        token = _DEADLINE.set(None)  # A separate cleanup pass has its own storage budget.
        try:
            return self._collect(seconds=seconds)
        except (OSError, PodError, ValueError, TypeError, KeyError, RecursionError) as exc:
            # Commit success is independent of best-effort post-commit cleanup.
            # Missing/corrupt required data preserves the hint and every chunk.
            return {"status": "deferred", "pending": True, "reason": getattr(exc, "code", "cleanup_interrupted")}
        finally:
            _DEADLINE.reset(token)

    def _collect(self, *, seconds=300):
        from .history_tree import Reader
        from .installer import STATE_SCHEMAS
        from .governor import SCHEMA as GOVERNOR_SCHEMA
        objective = self.root.parent.parent
        deadline = time.monotonic() + seconds
        def check():
            if time.monotonic() > deadline:
                _fail("history_deadline", "Cleanup exceeded its separate operation deadline; retry storage gc")
        with self.directory_path(objective) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "deferred", "pending": True, "reason": "readers_active"}
            # A prior publication may have reported an unconfirmed directory
            # sync. Make the current roots durable before reclaiming any older
            # bytes, including when this sweep resumes an earlier pending hint.
            os.fsync(lock)
            with self.directory() as directory:
                store = self
                class PinnedStore:
                    def load(self, address): return store._load(directory, address)
                reader = Reader(PinnedStore(), pin=False, seconds=seconds)
                marked = set()
                def mark(address):
                    check()
                    if address in marked: return False
                    reader.read_bytes(address)
                    marked.add(address)
                    return True
                def fragment(ref):
                    value = _reference_value(ref)
                    for address in value["parts"]: mark(address)
                def page(ref):
                    check()
                    address = ref["sha256"]
                    if address in marked: return
                    value = reader.page(ref); marked.add(address)
                    if value["level"]:
                        for child in value["entries"]: page(child)
                    else:
                        for row in value["entries"]:
                            descriptor(row[1] if value["kind"] == "dict" else row)
                def descriptor(ref):
                    if not isinstance(ref, dict) or len(ref) != 1:
                        _fail("history_invalid", "GC cannot prove a value descriptor")
                    if "ref" in ref: page(ref["ref"])
                    elif "value" in ref: fragment(ref["value"])
                    elif "inline" not in ref:
                        _fail("history_invalid", "GC cannot prove a value descriptor")
                for name in ("context.json", "governor.json"):
                    path = objective / name
                    if not path.exists(): continue
                    physical = json.loads(_context_bytes(path, MAINTENANCE_LIMIT))
                    if not isinstance(physical, dict):
                        _fail("history_invalid", "GC cannot prove an authority object")
                    if physical.get("schema") == STORAGE_SCHEMA:
                        if (set(physical) != _REVISION_FIELDS or physical["kind"] != name.removesuffix(".json")):
                            _fail("history_invalid", "GC cannot prove the current root")
                        descriptor(physical["root"])
                        for field in ("attribution", "facts", "maintenance"):
                            if physical[field] not in (None, []): descriptor(physical[field])
                    elif name == "context.json" and physical.get("schema") == STATE_SCHEMAS["context"]:
                        # Preceding typed slots remain roots while the other file
                        # switches format. User content is never searched for refs.
                        meta = physical.get("history")
                        if meta is not None and (not isinstance(meta, dict) or meta.get("schema") != FORMAT["schema"]
                                or set(meta) - {"schema", "maintenance", "decision_channels"}):
                            _fail("history_invalid", "GC cannot prove a preceding format")
                        for parent, key in _positions(physical):
                            if is_reference(parent[key]): fragment(parent[key])
                        maintenance = (physical.get("history") or {}).get("maintenance")
                        if maintenance is not None: fragment(maintenance)
                    elif name != "governor.json" or physical.get("schema") != GOVERNOR_SCHEMA:
                        _fail("history_invalid", "GC cannot prove an unsupported authority")
                # Complete marking precedes every unlink. Pins also cover the
                # put-before-switch window, so an in-flight proposal is not swept.
                removed_bytes = removed_files = 0
                for name in os.listdir(directory):
                    check()
                    reader.check()
                    if name.endswith(".json") and HEX.fullmatch(name[:-5]) and name[:-5] not in marked:
                        removed_bytes += os.stat(name, dir_fd=directory, follow_symlinks=False).st_size
                        os.unlink(name, dir_fd=directory)
                        removed_files += 1
                os.fsync(directory)
            os.unlink(".history-gc", dir_fd=lock)
            os.fsync(lock)
            return {"status": "collected", "pending": False, "removed_bytes": removed_bytes, "removed_files": removed_files}

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
                           + ("bounded storage conversion cannot read it" if maintenance else
                              "use internal storage inspect and migrate for explicit conversion"),
                           {"current_bytes": max(info.st_size, len(data)), "limit_bytes": limit,
                            "next_action": ("Project lead: preserve this record and arrange supported recovery; "
                                            "bounded storage conversion cannot read it." if maintenance else
                                            "Use internal storage inspect for this exact project and objective, then migrate "
                                            "with its source revision and SHA256; no owner decision is required.")})
        return data
    except OSError as exc:
        raise PodError("unsafe_record", "Context cannot be safely opened") from exc
    finally:
        os.close(fd)


from .installer import STORAGE_SCHEMA
_REVISION_FIELDS = {"schema", "kind", "revision", "root", "maintenance", "source", "attribution", "facts"}


def _maintenance_data(path, value):
    from .history_tree import Reader
    if value == []: return []
    values = Reader(ChunkStore(path.parent)).value(value)
    if not isinstance(values, list):
        _fail("history_invalid", "Maintenance revision is not a list")
    return values


def _revision_state(path, physical, raw):
    from .history_tree import SCHEMA, open_root
    if physical.get("schema") != SCHEMA:
        _fail("history_invalid", "Unsupported immutable publication format")
    if (set(physical) != _REVISION_FIELDS or physical["kind"] not in ("context", "governor")
            or type(physical["revision"]) is not int or physical["revision"] < 0
            or not isinstance(physical["maintenance"], (dict, list))
            or not isinstance(physical["attribution"], (dict, type(None)))):
        _fail("history_invalid", "Malformed revision envelope")
    source = physical["source"]
    if (not isinstance(source, dict) or set(source) != {"revision", "sha256"}
            or type(source["revision"]) is not int or source["revision"] < 0
            or not isinstance(source["sha256"], str) or not HEX.fullmatch(source["sha256"])):
        _fail("history_invalid", "Malformed migration source")
    if isinstance(physical["maintenance"], list) and physical["maintenance"]:
        _fail("history_invalid", "Nonempty maintenance history requires a verified revision reference")
    state = open_root(physical["root"], ChunkStore(path.parent))
    if state.get("revision") != physical["revision"]:
        _fail("history_integrity", "Logical revision differs from its envelope")
    state.origin = _sha(raw)
    state.storage = {key: physical[key] for key in ("kind", "maintenance", "source", "attribution", "facts")}
    state.storage["root"] = physical["root"]
    return state


@_pinned_read
def load_document(path, *, kind="governor", limit=CONTEXT_LIMIT):
    """Read a separate existing durable record through the same immutable store."""
    raw = _context_bytes(path, limit)
    try:
        physical = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded durable record") from exc
    if isinstance(physical, dict) and physical.get("schema") == STORAGE_SCHEMA:
        state = _revision_state(path, physical, raw)
        if state.storage["kind"] != kind:
            _fail("history_invalid", "Durable record kind differs")
        return state
    return physical


def _attribution(path, state, *, project=None):
    """One-time normalization uses existing frozen facts, never inferred ownership."""
    from .records import worktree_binding
    from .history_tree import plain
    checkpoint = state.get("checkpoint") or {}
    objective = checkpoint.get("objective")
    if not isinstance(objective, str) or not objective:
        return {"status": "unknown", "reason": "objective_unavailable"}
    if project is not None:
        from .github import repository_context
        from .ledger import record_name
        observed = repository_context(project)
        if observed["repository"] is not None and record_name(project, observed, objective) == path.parent.name:
            location = checkpoint.get("worktree")
            if location is not None:
                checked = worktree_binding(plain(location))
                if (checked["repository"], checked["repo_key"]) != (observed["repository"], observed["repo_key"]):
                    return {"status": "unknown", "reason": "recorded_repository_conflict", "objective": objective}
            # Worker placement can belong to another repository. The objective's
            # proven namespace is its attribution, not those resource locations.
            sources = ["repository_objective_record_key"]
            return {"status": "proved", "repository": observed["repository"], "repo_key": observed["repo_key"],
                    "objective": objective, "basis": {"record_key": path.parent.name, "sources": sources,
                    "sources_count": 1, "sources_sha256": digest(sources)}}
    locations, sources = [], []
    if checkpoint.get("worktree") is not None:
        locations.append(checkpoint["worktree"]); sources.append("checkpoint.worktree")
    else:
        for key, row in state.get("admissions", {}).items():
            recovery = row.get("recovery") or {}
            for field, location in (("checkpoint_binding.worktree", (recovery.get("checkpoint_binding") or {}).get("worktree")),
                                    ("placement_binding", recovery.get("placement_binding"))):
                if location is not None:
                    locations.append(location); sources.append("admissions/" + key + "/recovery/" + field)
    checked = []
    try:
        checked = [worktree_binding(plain(row)) for row in locations]
    except PodError:
        return {"status": "unknown", "reason": "recorded_location_malformed", "objective": objective}
    identities = {(row["repository"], row["repo_key"]) for row in checked if row["repository"] is not None}
    if project is not None:
        from .github import repository_context
        from .ledger import record_name
        observed = repository_context(project)
        if observed["repository"] is not None and record_name(project, observed, objective) == path.parent.name:
            identities.add((observed["repository"], observed["repo_key"])); sources.append("repository_objective_record_key")
    if len(identities) != 1:
        return {"status": "unknown", "reason": "recorded_repository_conflict" if identities else "repository_unavailable", "objective": objective}
    repository, key = next(iter(identities))
    return {"status": "proved", "repository": repository, "repo_key": key, "objective": objective,
            "basis": {"record_key": path.parent.name, "sources": sources, "sources_count": len(sources), "sources_sha256": digest(sources)}}



def _attribution_data(path, descriptor, root):
    from .history_tree import open_root
    if descriptor is None: return None
    data = open_root(descriptor, ChunkStore(path.parent))
    if data.get("root_sha256") != digest(root):
        _fail("history_integrity", "Attribution differs from its atomic revision")
    return data


def _public_attribution(data):
    from .history_tree import plain
    if data is None: return None
    result = {key: plain(data[key]) for key in ("status", "repository", "repo_key", "objective", "reason") if key in data}
    basis = data.get("basis")
    if isinstance(basis, dict):
        result["basis"] = {key: plain(basis[key]) for key in ("record_key", "sources_count", "sources_sha256") if key in basis}
    return result

@_pinned_read
def _load(path, *, limit=MAINTENANCE_LIMIT, validate=True):
    raw = _context_bytes(path, limit)
    try:
        physical = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded context JSON") from exc
    from .history_tree import SCHEMA
    if isinstance(physical, dict) and physical.get("schema") == SCHEMA:
        state = _revision_state(path, physical, raw)
        if state.storage["kind"] != "context":
            _fail("history_invalid", "Durable record kind differs")
        if validate:
            from .ledger import _validate_context
            _validate_context(state, full_history=False)
        return state, _maintenance_data(path, physical["maintenance"]), raw
    store = ChunkStore(path.parent)
    from .history_tree import Reader
    budget = Reader(store)
    # Open history only if referenced, pinning ancestry for the complete verification.
    with ExitStack() as stack:
        directory = None
        def read_chunk(address):
            nonlocal directory
            budget.check()
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


@_pinned_read
def load_admission_facts(path, *, include_admissions=True):
    """Versioned current-claims input for admission; never a complete proof/audit read."""
    from .history_tree import SCHEMA, open_root, plain
    from .installer import STATE_SCHEMAS
    from .records import worktree_binding
    raw = _context_bytes(path, MAINTENANCE_LIMIT)
    try:
        physical = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PodError("invalid_record", "Cannot decode admission attribution") from exc
    if not isinstance(physical, dict):
        _fail("history_invalid", "Admission attribution is not an object")
    if physical.get("schema") == SCHEMA:
        state = _revision_state(path, physical, raw)
        attribution = _public_attribution(_attribution_data(path, state.storage["attribution"], physical["root"]))
    else:
        state = physical
        metadata = physical.get("history")
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get("schema") != FORMAT["schema"]):
            _fail("history_invalid", "Unsupported earlier admission reference format")
        attribution = None
    if state.get("schema") != STATE_SCHEMAS["context"]:
        _fail("state_unsupported", "Admission attribution uses an unsupported context")
    checkpoint = state.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") != STATE_SCHEMAS["checkpoint"]:
        _fail("state_unsupported", "Admission attribution has no supported checkpoint")
    objective = checkpoint.get("objective")
    if not isinstance(objective, str) or not objective:
        _fail("history_invalid", "Admission attribution has no objective")
    location = checkpoint.get("worktree")
    if attribution is None:
        location = worktree_binding(plain(location))
        if (location.get("repository") is None
                or digest({"repository": location["repo_key"], "objective": objective}) != path.parent.name):
            _fail("history_invalid", "Admission attribution differs from its repository/objective record key")
        attribution = {"status": "proved", "repository": location["repository"], "repo_key": location["repo_key"],
                       "objective": objective, "basis": {"record_key": path.parent.name, "sources": ["checkpoint.worktree"]}}
    if (not isinstance(attribution, dict) or attribution.get("status") != "proved"
            or not isinstance(attribution.get("repository"), str) or attribution["repository"].count("/") != 1
            or not isinstance(attribution.get("repo_key"), str) or not HEX.fullmatch(attribution["repo_key"])
            or attribution.get("objective") != objective
            or not isinstance(attribution.get("basis"), dict) or attribution["basis"].get("record_key") != path.parent.name):
        _fail("attribution_unavailable", "Recorded facts do not prove repository attribution; retain a scoped unknown")
    if location is not None:
        location = worktree_binding(plain(location))
        if (location["repository"], location["repo_key"]) != (attribution["repository"], attribution["repo_key"]):
            _fail("history_integrity", "Current placement contradicts recorded attribution")
    revision = state.get("revision")
    if type(revision) is not int or revision < 0 or physical.get("revision") != revision:
        _fail("history_integrity", "Admission attribution revision differs")
    result = {"schema": "pod-admission-facts/v1", "generation": {"revision": revision, "sha256": _sha(raw)},
              "attribution_complete": True, "attribution": attribution, "complete": False, "admissions": None,
              "context": {"schema": state["schema"], "revision": revision, "owner": state.get("owner"),
                          "checkpoint": {key: plain(checkpoint.get(key)) for key in
                              ("schema", "objective", "objective_source", "worktree", "native_refs")}}}
    closed = checkpoint.get("closure")
    if closed is not None and not isinstance(closed, dict):
        _fail("history_invalid", "Admission phase has a malformed closure")
    result["context"]["checkpoint"]["closure"] = ({key: plain(closed.get(key)) for key in ("revision", "seq")}
                                                   if closed else None)
    result["phase"] = ("continued" if state.get("continued_by") else "retired"
                       if state.get("historical_retirement") else "closed" if closed else "open")
    if not include_admissions:
        return result
    for field in ("continues", "continued_by", "historical_retirement"):
        if field in state:
            value = state[field]
            if is_reference(value): value = _expand(value, ChunkStore(path.parent).load, {})
            result["context"][field] = plain(value)
    admissions = state.get("admissions")
    if not isinstance(admissions, dict):
        _fail("history_invalid", "Current admission inventory is unavailable")
    rows = {}
    fields = ("schema", "admission_id", "objective", "owner", "state", "role", "runtime", "run_id", "task_id",
              "worktree", "native_binding", "serves", "boundary", "candidate", "request_uuid")
    for key, value in admissions.items():
        if is_reference(value): value = _expand(value, ChunkStore(path.parent).load, {})
        if not isinstance(value, dict) or value.get("admission_id") != key or value.get("schema") != STATE_SCHEMAS["admission"]:
            _fail("history_invalid", "Current admission identity is unavailable")
        if any(field not in value for field in fields):
            _fail("history_invalid", "Current admission facts are incomplete")
        rows[key] = {field: plain(value[field]) for field in fields}
        recovery = value.get("recovery")
        if not isinstance(recovery, dict):
            _fail("history_invalid", "Current admission placement facts are unavailable")
        placement = recovery.get("placement_binding")
        rows[key]["placement_binding"] = plain(placement) if placement is not None else None
        if "abandoned_accounting" in value:
            accounting = value["abandoned_accounting"]
            if not isinstance(accounting, dict):
                _fail("history_invalid", "Current abandonment marker is malformed")
            rows[key]["abandoned_accounting"] = {field: plain(accounting[field])
                for field in ("classification", "execution") if field in accounting}
    result.update(admissions=rows, complete=location is not None)
    if location is None:
        result["errors"] = [{"code": "objective_placement_unknown"}]
    return result


def prepare_context(path, state, *, maintenance=None, project=None):
    """Pure planner for the one context writer, including first/earlier writes."""
    return _prepare_revisions(path, state, kind="context", maintenance=maintenance, project=project)


def publish_context(path, record, chunks):
    """Publish once, then reclaim unreachable chunks after readers drain."""
    store = ChunkStore(path.parent)
    with store.pin(create=True, collect=True):
        previous = json.loads(_context_bytes(path, MAINTENANCE_LIMIT)) if path.exists() else {}
        replaced = [previous[key]["ref"]["sha256"] for key in ("root", "facts", "attribution", "maintenance")
                    if isinstance(previous.get(key), dict) and "ref" in previous[key]]
        store.request_collection(queued_bytes=sum(map(len, chunks.values())) + len(_bytes(record)), replaced=replaced)
        _publish_context(path, record, chunks)
        state = getattr(record, "source_state", None)
        if hasattr(state, "origin"):
            # A multi-phase writer may publish this same object again. Advance
            # its CAS token only after durability; independent snapshots retain
            # their own old token and remain stale.
            state.origin = _sha(_bytes(record))
            if state.reader._lease is not None:
                state.reader._lease.collect_on_close = True
            state.storage = {key: record[key] for key in _REVISION_FIELDS - {"schema", "revision"}}


def _publish_context(path, record, chunks):
    """Publish chunks without replacement before the atomic context replacement."""
    from .evidence_closure import refuse_closed
    refuse_closed(path.parent)  # Backstop: a record closed by evidence is never written again (R145).
    expected = getattr(record, "expected", None)
    if getattr(record, "preview", False):
        _fail("map_stale", "A semantic write must advance the record revision; read the current record")
    observed = _sha(_context_bytes(path, MAINTENANCE_LIMIT)) if path.exists() else _sha(b"")
    if expected is not None and observed != expected:
        _fail("map_stale", "Context changed before revision publication; read it again")
    if record.get("schema") != STORAGE_SCHEMA:
        _fail("storage_format_invalid", "Publication requires the single supported storage envelope")
    _require_size(record, current_bytes=path.stat().st_size if path.exists() else 0)
    preflight_landing(path, sum(map(len, chunks.values())) + len(_bytes(record)))
    try:
        ChunkStore(path.parent).put(chunks)
        atomic_json(path, record, limit=CONTEXT_LIMIT, compact=True)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise PodError("storage_full", "Revision publication has insufficient disk space; prior context is intact",
                           {"next_action": "free task storage and retry the same operation after readback"}) from exc
        raise
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    except OSError as exc:
        raise PodError("storage_publication_unconfirmed", "Publication needs readback after its directory sync failed",
                       {"next_action": "read the authoritative record before retrying; do not repeat an external effect"}) from exc
    finally:
        os.close(fd)


class _Publication(dict):
    """A transient compare-and-swap token, excluded from persisted JSON."""


def _prepare_revisions(path, state, *, kind="context", maintenance=None, project=None):
    from .history_tree import SCHEMA, plan
    exists = path.exists()
    raw = _context_bytes(path, MAINTENANCE_LIMIT) if exists else b""
    previous = json.loads(raw) if raw else None
    source = getattr(getattr(getattr(state, "reader", None), "store", None), "root", None)
    same_store = source is not None and source.absolute() == ChunkStore(path.parent).root.absolute()
    expected = getattr(state, "origin", _sha(raw)) if same_store else _sha(raw)
    if expected != _sha(raw):
        _fail("map_stale", "Record changed before planning; read it again")
    current = previous is not None and previous.get("schema") == SCHEMA
    metadata = {key: previous[key] for key in ("kind", "maintenance", "source", "attribution", "facts")} if current else {
        "kind": kind, "maintenance": [], "source": {"revision": previous["revision"] if previous else 0,
        "sha256": _sha(raw)}, "attribution": None, "facts": None}
    if metadata["kind"] != kind:
        _fail("history_invalid", "Durable record kind differs")
    if maintenance is None and not current and previous is not None:
        reference = (previous.get("history") or {}).get("maintenance")
        maintenance = _maintenance_valid(_expand(reference, ChunkStore(path.parent).load, {})) if reference else []
    root, chunks = plan(state, store_root=ChunkStore(path.parent).root, force_root=True)
    if maintenance is not None:
        metadata["maintenance"] = []
        if maintenance:
            metadata["maintenance"], extra = plan(maintenance, force_root=True); chunks.update(extra)
    if kind == "context":
        old = _attribution_data(path, metadata["attribution"], previous["root"]) if current else None
        attribution = (_attribution(path, state, project=project)
                       if old is None or old.get("status") != "proved"
                       else deepcopy(old))
        attribution["root_sha256"] = digest(root)
        metadata["attribution"], extra = plan(attribution, force_root=True); chunks.update(extra)
    else:
        from .governor import _effect_facts
        facts = {**_effect_facts(state), "binding": {"revision": state["revision"], "root_sha256": digest(root)}}
        metadata["facts"], extra = plan(facts, force_root=True); chunks.update(extra)
    record = _Publication(schema=SCHEMA, revision=state["revision"], root=root, **metadata)
    preflight_landing(path, sum(map(len, chunks.values())) + len(_bytes(record)))
    record.expected = expected
    record.source_state = state if same_store else None
    record.preview = current and state["revision"] != previous["revision"] + 1 and root != previous["root"]
    return record, chunks


def prepare_document(path, state):
    return _prepare_revisions(path, state, kind="governor")


# Reserved for one maximum-size report, triage/corrections and atomic publication.
# This is an operation landing reserve, independent of retained history count.
REPORT_LANDING_BYTES = 4 * 1024 * 1024
DISK_HEADROOM_BYTES = 8 * 1024 * 1024


def preflight_landing(path, proposed_bytes=0, *, reports=0):
    """Admit only with room for pending reports and a fresh atomic replacement."""
    ancestor = path.parent
    while not ancestor.exists(): ancestor = ancestor.parent
    info = os.statvfs(ancestor)
    available = info.f_bavail * info.f_frsize
    required = proposed_bytes + reports * REPORT_LANDING_BYTES + DISK_HEADROOM_BYTES
    if available < required:
        raise PodError("storage_full", "The report's durable landing path has insufficient disk space",
                       {"available_bytes": available, "required_bytes": required,
                        "next_action": "free task storage, then retry admission; no worker was started"})


def preflight_reports(path, state, *, reports):
    """Prove a supported revision landing path before reserving native work.

    A preceding primary file is converted by its next accepted publication.
    Planning that operation here publishes nothing and ensures its
    immutable pages plus pending reports fit the durable destination.
    """
    from .history_tree import RevisionDict, plan
    migration_bytes = 0
    if not isinstance(state, RevisionDict) or not hasattr(state, "storage"):
        _, chunks = plan(state, store_root=ChunkStore(path.parent).root, force_root=True)
        migration_bytes = sum(map(len, chunks.values())) + CONTEXT_LIMIT
    preflight_landing(path, migration_bytes, reports=reports)


def cleanup_inventory(state_root, *, collect=False):
    """Inspect only owned bounded hints; never read logical records or infer authority."""
    root = Path(state_root)
    if not root.exists(): return []
    result = []
    with ChunkStore(root).directory_path(root) as directory:
        for name in sorted(os.listdir(directory)):
            if not HEX.fullmatch(name): continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode): continue
            store = ChunkStore(root / name)
            queued = store.cleanup_info()
            if not queued["pending"]: continue
            row = {"record": name, "queued_bytes": queued["queued_bytes"], "untracked": queued["untracked"]}
            if collect:
                row["cleanup"] = store.collect()
                row["queued_bytes"] = store.cleanup_info()["queued_bytes"]
            result.append(row)
    return result


def migrate_context(path, *, revision, sha256, project=None, kind="context"):
    result = _migrate_context(path, revision=revision, sha256=sha256, project=project, kind=kind)
    store = ChunkStore(path.parent)
    result["cleanup"] = store.collect() if result["written"] else store.cleanup_info()
    return result


def _migrate_context(path, *, revision, sha256, project=None, kind="context"):
    """Explicit, resumable, lossless placement change; one writable authority."""
    from .ledger import _lock, _validate_context
    from .history_tree import SCHEMA, Reader, plan, plain
    from .evidence_closure import refuse_closed
    refuse_closed(path.parent)
    with _lock(path):
        refuse_closed(path.parent)
        if kind == "governor":
            context_path = path.with_name("context.json")
            if context_path.exists():
                from .succession import refuse_continued
                refuse_continued(load_context(context_path))
        raw = _context_bytes(path, MAINTENANCE_LIMIT)
        try:
            physical = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PodError("invalid_record", "Cannot decode migration source") from exc
        if not isinstance(physical, dict): _fail("history_invalid", "Migration source is not an object")
        target_schema = SCHEMA
        if physical.get("schema") == target_schema:
            existing = _revision_state(path, physical, raw)
            if existing.storage["kind"] != kind:
                _fail("history_invalid", "Migration record kind differs")
            if kind == "context":
                from .succession import refuse_continued
                refuse_continued(existing)
            if ((physical["source"]["sha256"], physical["source"]["revision"]) == (sha256, revision)
                    or (_sha(raw), physical["revision"]) == (sha256, revision)):
                return {"status": "replayed", "written": False, "revision": physical["revision"], "attribution": _public_attribution(_attribution_data(path, physical["attribution"], physical["root"]))}
            _fail("storage_migration_stale", "Record already migrated from different source facts")
        if kind == "context":
            state, receipts, _ = _load(path, limit=MAINTENANCE_LIMIT)
            _validate_context(state)
            from .succession import refuse_continued
            refuse_continued(state)
            if "historical_retirement" in state:
                _fail("objective_retired", "An objective under historical retirement is frozen")
            attribution = _attribution(path, state, project=project)
            facts = None
        elif kind == "governor":
            from .governor import _validate_journal, _effect_facts
            state, receipts, attribution = _validate_journal(physical), [], None
            facts = None
        else:
            _fail("history_invalid", "Unsupported durable record kind")
        if state["revision"] != revision or _sha(raw) != sha256:
            _fail("storage_migration_stale", "Migration source changed; detect it again")
        root, chunks = plan(state, force_root=True)
        public_attribution = _public_attribution(attribution)
        if kind == "context":
            attribution["root_sha256"] = digest(root)
            attribution, attr_chunks = plan(attribution, force_root=True); chunks.update(attr_chunks)
        if kind == "governor":
            fact_value = {**_effect_facts(state), "binding": {"revision": revision, "root_sha256": digest(root)}}
            facts, fact_chunks = plan(fact_value, force_root=True); chunks.update(fact_chunks)
        maintenance = []
        if receipts:
            maintenance, receipt_chunks = plan(receipts, force_root=True); chunks.update(receipt_chunks)
        record = _Publication(schema=target_schema, kind=kind, revision=revision, root=root,
                              maintenance=maintenance, source={"revision": revision, "sha256": sha256},
                              attribution=attribution, facts=facts)
        record.expected = sha256
        class Proposed:
            def load(self, address): return chunks[address]
        if plain(Reader(Proposed()).value(root)) != state:
            _fail("history_integrity", "Migration changes logical state")
        publish_context(path, record, chunks)
        return {"status": "migrated", "written": True, "revision": revision, "attribution": public_attribution,
                "physical_bytes": len(_bytes(record)), "logical_bytes": len(_bytes(state)), "chunks": len(chunks)}


def audit_context(path, *, kind="context"):
    """Verify every currently retained value, never reconstruct discarded snapshots."""
    from .history_tree import plain, open_root
    if kind == "context":
        state, receipts, raw = _load(path)
    else:
        from .governor import _read_journal
        state, receipts, raw = _read_journal(path), [], _context_bytes(path, CONTEXT_LIMIT)
    if hasattr(state, "reader"):
        state.reader.seconds = 300  # Explicit full audit has its own deadline.
    logical = plain(state)
    if hasattr(state, "storage"):
        state.reader.check()
        metadata = state.storage
        maintenance = plain(_maintenance_data(path, metadata["maintenance"]))
        if maintenance: _maintenance_valid(maintenance)
        if kind == "context":
            plain(_attribution_data(path, metadata["attribution"], metadata["root"]))
        else:
            from .governor import _effect_facts
            expected = {**_effect_facts(state), "binding": {
                "revision": state["revision"], "root_sha256": digest(metadata["root"])}}
            if plain(open_root(metadata["facts"], ChunkStore(path.parent))) != expected:
                _fail("history_integrity", "Current effects differ from their atomic projection")
    if kind == "context":
        from .ledger import _validate_context
        _validate_context(logical)
    else:
        from .governor import _validate_journal
        _validate_journal(logical)
    return {"state": logical, "maintenance": plain(receipts), "physical_bytes": len(raw),
            "logical_bytes": len(_bytes(logical)), "complete": True}


def capacity(path, state):
    """Additive observation only: never changes readiness, phase or existing actions."""
    from .history_tree import RevisionDict, descriptor_size, SCHEMA
    physical = json.loads(_context_bytes(path, MAINTENANCE_LIMIT))
    if physical.get("schema") == SCHEMA:
        maintenance = physical["maintenance"]
        return {"format": SCHEMA, "physical_bytes": path.stat().st_size,
                "logical_bytes": descriptor_size(physical["root"]),
                "working_set_bytes": path.stat().st_size + getattr(state.reader, "bytes", 0),
                "pages_read": getattr(state.reader, "nodes", 0),
                "history_complete": False, "full_audit_available": True,
                "warning": False, "maintenance": [] if maintenance == [] else None,
                "maintenance_loaded": maintenance == [],
                "cleanup": {key: value for key, value in ChunkStore(path.parent).cleanup_info().items() if key != "roots"},
                "next_action": "continue ordinary operations; use explicit storage audit for full retained history"}
    return {"format": physical.get("schema"), "physical_bytes": path.stat().st_size,
            "warning": False, "conversion_pending": True, "logical_bytes": len(_bytes(state)), "full_audit_available": True,
            "cleanup": {key: value for key, value in ChunkStore(path.parent).cleanup_info().items() if key != "roots"},
            "next_action": "the next accepted write converts this supported physical input; use storage inspect and migrate for explicit conversion"}


def record_compaction(project, objective, *, request=None, decision=None):
    """Read-only compatibility notice; explicit conversion has one source-bound entry."""
    from .ledger import _path
    from .evidence_closure import refuse_closed
    from .records import storage
    path = _path(project, objective)
    refuse_closed(path.parent)
    state = load_context(path)
    from .succession import refuse_continued
    refuse_continued(state, reserved=False)
    if "historical_retirement" in state:
        _fail("objective_retired", "An objective under historical retirement is frozen")
    if request is not None or decision is not None:
        _fail("storage_action_required", "Use internal storage inspect then migrate with its exact source revision and SHA256; this entry is read-only")
    result = storage(project, objective)
    return {**result, "status": "unchanged" if result["migrated"] else "conversion_available",
            "current_bytes": result["physical_bytes"], "proposed_bytes": result["physical_bytes"], "reclaimable_bytes": 0}

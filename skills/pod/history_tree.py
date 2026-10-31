"""Copy-on-write revision pages over the existing immutable history chunk store.

Containers have ordinary dict/list semantics. Opening a container reads its index,
not its descendants. Unchanged descendants keep their verified references on write.
No projection drops rows, changes their status, or stands in for selected proof.
"""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import resource
import weakref
import time

from .errors import PodError
from .installer import STORAGE_SCHEMA as SCHEMA
NODE_SCHEMA = "pod-revision-page/v1"
INLINE_BYTES = 2048
PAGE_BYTES = 48 * 1024
READ_SECONDS = 30
PARSE_DEPTH = 64


def _raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def plain(value, depth=0):
    """Explicit full materialization for export, hashes and full audit only."""
    if depth >= PARSE_DEPTH:
        raise PodError("history_parse_depth", "JSON value exceeds the bounded parse depth")
    if isinstance(value, dict):
        return {key: plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item, depth + 1) for item in value]
    return value


def _memory():
    # Linux is the supported execution environment. Read current usage, not the
    # high-water mark left by a previous operation in a long-lived coordinator.
    virtual, resident = map(int, Path("/proc/self/statm").read_text().split()[:2])
    page = os.sysconf("SC_PAGE_SIZE")
    return virtual * page, resident * page


def _headroom():
    available = next(int(row.split()[1]) * 1024 for row in
                     Path("/proc/meminfo").read_text().splitlines() if row.startswith("MemAvailable:"))
    ceiling = Path("/sys/fs/cgroup/memory.max")
    if ceiling.exists() and ceiling.read_text().strip() != "max":
        available = min(available, max(0, int(ceiling.read_text()) -
                        int(ceiling.with_name("memory.current").read_text())))
    limit = resource.getrlimit(resource.RLIMIT_AS)[0]
    if limit != resource.RLIM_INFINITY:
        available = min(available, max(0, limit - _memory()[0]))
    # Leave the rest to the host and other operations. This bounds actual memory,
    # not accumulated logical evidence or bytes streamed through an operation.
    return available // 4


def _storage_read(function):
    """Charge only active storage work; external calls between lazy reads have their own budget."""
    @wraps(function)
    def call(self, *args, **kwargs):
        outer = self._active_started is None
        if outer:
            self._active_started = time.monotonic()
        try:
            result = function(self, *args, **kwargs)
            self.check()
            return result
        finally:
            if outer:
                self.elapsed += time.monotonic() - self._active_started
                self._active_started = None
    return call


class Reader:
    def __init__(self, store, *, memory_limit=None, seconds=READ_SECONDS, pin=True):
        self.store = store
        self._lease = store.pin() if pin and hasattr(store, "pin") else None
        self._release = weakref.finalize(self, self._lease.close) if self._lease is not None else None
        self.memory_limit = _headroom() if memory_limit is None else memory_limit
        self.resident = _memory()[1]
        self.seconds = seconds
        self.bytes = self.nodes = 0
        self.elapsed = 0.0
        self._active_started = None

    def close(self):
        if self._release is not None: self._release()

    def check(self):
        from .history import _check_read_deadline
        _check_read_deadline()
        elapsed = self.elapsed + (time.monotonic() - self._active_started if self._active_started is not None else 0)
        if elapsed > self.seconds:
            raise PodError("history_deadline", "History read exceeded its operation deadline")
        growth = max(0, _memory()[1] - self.resident)
        if growth >= self.memory_limit:
            raise PodError("history_memory", "History read exhausted its available process memory headroom",
                           {"resident_growth_bytes": growth, "memory_headroom_bytes": self.memory_limit,
                            "next_action": "retry a scoped read with available memory; preserve retained evidence"})

    @_storage_read
    def read_bytes(self, address):
        self.check()
        raw = self.store.load(address)
        from .history import CHUNK_LIMIT
        if not isinstance(raw, bytes) or len(raw) > CHUNK_LIMIT or hashlib.sha256(raw).hexdigest() != address:
            raise PodError("history_integrity", "History chunk bytes differ from their address")
        self.bytes += len(raw)
        self.nodes += 1
        return raw

    @_storage_read
    def page(self, reference):
        from .history import HEX, _check_read_deadline
        _check_read_deadline()
        if (not isinstance(reference, dict) or set(reference) != {"sha256", "bytes", "kind", "level", "logical_bytes"}
                or reference["kind"] not in ("dict", "list")
                or type(reference["level"]) is not int or not 0 <= reference["level"] < PARSE_DEPTH
                or type(reference["logical_bytes"]) is not int or reference["logical_bytes"] < 3
                or not isinstance(reference["sha256"], str) or not HEX.fullmatch(reference["sha256"])
                or type(reference["bytes"]) is not int or not 1 <= reference["bytes"] <= PAGE_BYTES):
            raise PodError("history_invalid", "Malformed revision page reference")
        self.check()
        raw = self.read_bytes(reference["sha256"])
        if len(raw) != reference["bytes"]:
            raise PodError("history_integrity", "Revision page length differs")
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {"schema", "kind", "level", "entries"}
                    or value["schema"] != NODE_SCHEMA or value["kind"] != reference["kind"]
                    or value["level"] != reference["level"]
                    or not isinstance(value["entries"], list) or _raw(value) != raw):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
            raise PodError("history_integrity", "Revision page cannot be decoded canonically") from exc
        try:
            logical = page_logical_size(value["kind"], value["entries"], value["level"])
        except (TypeError, KeyError, ValueError) as exc:
            raise PodError("history_integrity", "Revision page entries are malformed") from exc
        if logical != reference["logical_bytes"]:
            raise PodError("history_integrity", "Revision logical size differs from its index")
        return value

    def entries(self, reference, depth=0):
        if depth >= PARSE_DEPTH:
            raise PodError("history_parse_depth", "Revision index is too deeply nested")
        page = self.page(reference)
        if page["level"]:
            for child in page["entries"]:
                if (not isinstance(child, dict) or child.get("level") != page["level"] - 1
                        or child.get("kind") != page["kind"]):
                    raise PodError("history_integrity", "Revision index level differs")
                yield from self.entries(child, depth + 1)
        else:
            yield from page["entries"]

    @_storage_read
    def value(self, value, parent=None):
        if not isinstance(value, dict) or len(value) != 1:
            raise PodError("history_integrity", "Malformed revision value descriptor")
        if "inline" in value:
            result = tracked(value["inline"], parent)
            mark_persisted(result)
            return result
        if "value" in value:
            from .history import _expand
            ref = value["value"]
            if (not isinstance(ref, dict) or not isinstance(ref.get("$pod_history"), dict)
                    or type(ref["$pod_history"].get("bytes")) is not int
                    or ref["$pod_history"]["bytes"] > self.memory_limit):
                raise PodError("history_memory", "History scalar exceeds available parse memory headroom")
            return tracked(_expand(ref, self.read_bytes, {}), parent)
        reference = value.get("ref")
        if not isinstance(reference, dict) or reference.get("kind") not in ("dict", "list"):
            raise PodError("history_integrity", "Malformed revision container descriptor")
        cls = RevisionDict if reference["kind"] == "dict" else RevisionList
        result = cls(reader=self, reference=reference, parent=parent)
        result.persisted = True
        return result


class _Revision:
    @property
    def parent(self):
        return self._parent() if self._parent is not None else None

    @parent.setter
    def parent(self, value):
        self._parent = weakref.ref(value) if value is not None else None

    def _init(self, reader=None, reference=None, parent=None):
        self.reader, self.reference, self.parent = reader, reference, parent
        self.persisted = False
        self.depth = getattr(parent, "depth", -1) + 1
        if self.depth >= PARSE_DEPTH:
            raise PodError("history_parse_depth", "Revision value exceeds the bounded parse depth")
        self.loaded = reference is None
        self.dirty = reference is None

    def changed(self):
        self.dirty = True
        self.persisted = False
        parent = self.parent
        if parent is not None:
            parent.changed()

    def __deepcopy__(self, memo):
        result = type(self)(reader=self.reader, reference=deepcopy(self.reference))
        memo[id(self)] = result
        result.loaded, result.dirty, result.persisted = self.loaded, self.dirty, self.persisted
        if hasattr(self, "origin"): result.origin = self.origin
        if hasattr(self, "storage"): result.storage = deepcopy(self.storage)
        if self.loaded:
            if isinstance(self, dict):
                dict.clear(result)
                for key, item in dict.items(self):
                    item = deepcopy(item, memo)
                    if isinstance(item, _Revision): item.parent = result
                    dict.__setitem__(result, key, item)
            else:
                for item in list.__iter__(self):
                    item = deepcopy(item, memo)
                    if isinstance(item, _Revision): item.parent = result
                    list.append(result, item)
        return result

    def __eq__(self, other):
        if (isinstance(other, _Revision) and not self.dirty and not other.dirty
                and self.reference == other.reference):
            return True
        self.ensure()
        if isinstance(other, _Revision): other.ensure()
        return dict.__eq__(self, other) if isinstance(self, dict) else list.__eq__(self, other)

    def __ne__(self, other):
        return not self == other


def unchanged(old, new):
    if old is new:
        return True
    if isinstance(old, _Revision) and isinstance(new, _Revision):
        return not old.dirty and not new.dirty and old.reference == new.reference
    if not isinstance(old, (dict, list)) and not isinstance(new, (dict, list)):
        return type(old) is type(new) and (_raw(old) == _raw(new) if isinstance(old, float) else old == new)
    return False


class RevisionDict(_Revision, dict):
    def __init__(self, *, reader=None, reference=None, parent=None):
        dict.__init__(self)
        self._init(reader, reference, parent)
        # CPython's JSON encoder bypasses items() for an internally empty dict,
        # even on a subclass. A nonempty private slot makes it call our items()
        # and verify/load the actual object instead of silently emitting {}.
        if not self.loaded: dict.__setitem__(self, "$pod_unloaded", None)

    def ensure(self):
        if self.loaded: return
        entries = list(self.reader.entries(self.reference))
        seen = set()
        dict.clear(self)
        for row in entries:
            if (not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                    or row[0] in seen):
                raise PodError("history_integrity", "Revision object keys are malformed or duplicated")
            seen.add(row[0])
            dict.__setitem__(self, row[0], self.reader.value(row[1], self))
        self.loaded = True

    def __getitem__(self, key): self.ensure(); return dict.__getitem__(self, key)
    def __iter__(self): self.ensure(); return dict.__iter__(self)
    def __len__(self): self.ensure(); return dict.__len__(self)
    def __contains__(self, key): self.ensure(); return dict.__contains__(self, key)
    def keys(self): self.ensure(); return dict.keys(self)
    def items(self): self.ensure(); return dict.items(self)
    def values(self): self.ensure(); return dict.values(self)
    def get(self, key, default=None): self.ensure(); return dict.get(self, key, default)
    def copy(self): self.ensure(); return dict(self)
    def __or__(self, other):
        if not isinstance(other, dict): return NotImplemented
        self.ensure(); return {**self, **other}
    def __ror__(self, other):
        if not isinstance(other, dict): return NotImplemented
        self.ensure(); return {**other, **self}
    def __setitem__(self, key, value):
        # Keep ordinary Python alias semantics for newly assigned containers.
        # This parent stays dirty after insertion, so planning visits their
        # current values even when the caller mutates its original dict/list.
        self.ensure()
        if key in self and unchanged(dict.__getitem__(self, key), value):
            return
        dict.__setitem__(self, key, value); self.changed()
    def __delitem__(self, key): self.ensure(); dict.__delitem__(self, key); self.changed()
    def setdefault(self, key, default=None):
        self.ensure()
        if key not in self: self[key] = default
        return self[key]
    def pop(self, key, *default):
        self.ensure()
        if key in self:
            result = dict.pop(self, key); self.changed(); return result
        return dict.pop(self, key, *default)
    def popitem(self): self.ensure(); result = dict.popitem(self); self.changed(); return result
    def clear(self): self.ensure(); dict.clear(self); self.changed()
    def update(self, *args, **kwargs):
        for key, item in dict(*args, **kwargs).items(): self[key] = item
    def __ior__(self, other): self.update(other); return self


class RevisionList(_Revision, list):
    def __init__(self, *, reader=None, reference=None, parent=None):
        list.__init__(self)
        self._init(reader, reference, parent)

    def ensure(self):
        if self.loaded: return
        entries = list(self.reader.entries(self.reference))
        for row in entries: list.append(self, self.reader.value(row, self))
        self.loaded = True

    def __getitem__(self, key): self.ensure(); return list.__getitem__(self, key)
    def __iter__(self): self.ensure(); return list.__iter__(self)
    def __len__(self): self.ensure(); return list.__len__(self)
    def __contains__(self, value): self.ensure(); return list.__contains__(self, value)
    def __reversed__(self): self.ensure(); return list.__reversed__(self)
    def __add__(self, other): self.ensure(); return list.__add__(self, other)
    def __radd__(self, other): self.ensure(); return other + list(self)
    def copy(self): self.ensure(); return list(self)
    def count(self, value): self.ensure(); return list.count(self, value)
    def index(self, value, *bounds): self.ensure(); return list.index(self, value, *bounds)
    def __mul__(self, count): self.ensure(); return list.__mul__(self, count)
    def __rmul__(self, count): self.ensure(); return list.__mul__(self, count)
    def __setitem__(self, key, value):
        self.ensure()
        if not isinstance(key, slice) and unchanged(list.__getitem__(self, key), value):
            return
        list.__setitem__(self, key, list(value) if isinstance(key, slice) else value)
        self.changed()
    def __delitem__(self, key): self.ensure(); list.__delitem__(self, key); self.changed()
    def append(self, value): self.ensure(); list.append(self, value); self.changed()
    def extend(self, values): self.ensure(); list.extend(self, list(values)); self.changed()
    def insert(self, index, value): self.ensure(); list.insert(self, index, value); self.changed()
    def pop(self, index=-1): self.ensure(); result = list.pop(self, index); self.changed(); return result
    def remove(self, value): self.ensure(); list.remove(self, value); self.changed()
    def reverse(self): self.ensure(); list.reverse(self); self.changed()
    def sort(self, *args, **kwargs): self.ensure(); list.sort(self, *args, **kwargs); self.changed()
    def clear(self): self.ensure(); list.clear(self); self.changed()
    def __iadd__(self, values): self.extend(values); return self
    def __imul__(self, count): self.ensure(); list.__imul__(self, count); self.changed(); return self



def mark_persisted(value):
    if isinstance(value, _Revision):
        value.persisted = True
        children = dict.values(value) if isinstance(value, dict) else list.__iter__(value)
        for child in children: mark_persisted(child)


def from_revision(value):
    """Byte provenance for unchanged stored values; never accepted from JSON input."""
    return isinstance(value, _Revision) and value.persisted

def tracked(value, parent=None):
    if isinstance(value, _Revision):
        # Reparenting a shared prior node must not break its dirty propagation.
        value = deepcopy(value)
        value.parent = parent
        return value
    if isinstance(value, dict):
        result = RevisionDict(parent=parent)
        for key, item in value.items(): dict.__setitem__(result, key, tracked(item, result))
        return result
    if isinstance(value, list):
        result = RevisionList(parent=parent)
        for item in value: list.append(result, tracked(item, result))
        return result
    return value


def plan(value, *, store_root=None, force_root=False):
    """Build immutable pages; never open a clean subtree or publish any bytes."""
    chunks = {}
    budget = Reader(None)
    def has_revision(item):
        if isinstance(item, _Revision) and item.reference is not None: return True
        if isinstance(item, dict): return any(has_revision(child) for child in item.values())
        if isinstance(item, list): return any(has_revision(child) for child in item)
        return False
    def page(kind, entries, level):
        raw = _raw({"schema": NODE_SCHEMA, "kind": kind, "level": level, "entries": entries})
        if len(raw) > PAGE_BYTES:
            raise PodError("history_page_size", "One revision index entry exceeds the page bound")
        address = hashlib.sha256(raw).hexdigest()
        chunks[address] = raw
        logical = page_logical_size(kind, entries, level)
        return {"sha256": address, "bytes": len(raw), "kind": kind, "level": level, "logical_bytes": logical}

    def pages(kind, entries, level=0):
        batches, batch, size = [], [], 100
        for entry in entries:
            cost = len(_raw(entry))
            if batch and size + cost > PAGE_BYTES - 512:
                batches.append(page(kind, batch, level)); batch, size = [], 100
            batch.append(entry); size += cost
        if batch or not batches: batches.append(page(kind, batch, level))
        if len(batches) == 1: return batches[0]
        # Pages of references can themselves be paged; no lifetime index ceiling.
        return pages(kind, batches, level + 1)

    def encode(item, depth=0):
        budget.check()
        if depth >= PARSE_DEPTH:
            raise PodError("history_parse_depth", "Revision value is too deeply nested")
        if isinstance(item, _Revision) and not item.dirty and item.reference is not None:
            source = getattr(item.reader.store, "root", None)
            if store_root is None or source is not None and source.absolute() == store_root.absolute():
                return {"ref": item.reference}
            # A carried value belongs to another objective's store. Copy its
            # verified logical bytes; never publish a dangling cross-store ref.
            item = plain(item)
        if not isinstance(item, (dict, list)):
            if len(_raw(item)) > INLINE_BYTES:
                from .history import _reference
                return {"value": _reference(item, chunks)}
            return {"inline": item}
        # Small ordinary records stay together; large containers use typed pages.
        # Do not measure a lazy subtree by serializing all of its descendants.
        if not has_revision(item) and not (force_root and depth == 0):
            literal = plain(item)
            raw = _raw(literal)
            if len(raw) <= INLINE_BYTES: return {"inline": literal}
        if isinstance(item, dict):
            entries = [[key, encode(child, depth + 1)] for key, child in item.items()]
            kind = "dict"
        else:
            entries = [encode(child, depth + 1) for child in item]
            kind = "list"
        return {"ref": pages(kind, entries)}

    root = encode(value)
    return root, chunks


def descriptor_size(value):
    if "ref" in value: return value["ref"]["logical_bytes"]
    if "value" in value: return value["value"]["$pod_history"]["bytes"]
    return len(_raw(value["inline"]))


def page_logical_size(kind, entries, level):
    if level:
        interior = sum(child["logical_bytes"] - 3 for child in entries)
    elif kind == "dict":
        interior = sum(len(_raw(key)) + descriptor_size(child) - 1 for key, child in entries)
    else:
        interior = sum(descriptor_size(child) - 1 for child in entries)
    return 3 + interior + max(0, len(entries) - 1)


def open_root(root, store):
    reader = Reader(store)
    value = reader.value(root)
    if not isinstance(value, dict):
        raise PodError("history_integrity", "Context revision is not an object")
    return value


def state_digest(value):
    """Bind decision state to verified subtree identities without exporting history.

    Evidence digests continue using the exact logical JSON in util.digest. Only
    predecessor/state binding uses this representation; a changed dirty subtree
    gets a newly planned identity, and unchanged references require no reads.
    """
    from .util import digest
    def identity(item):
        if isinstance(item, _Revision):
            descriptor, _ = plan(item, force_root=True)
            return {"$pod_state": descriptor}
        if isinstance(item, dict):
            return {key: identity(child) for key, child in item.items()}
        if isinstance(item, list):
            return [identity(child) for child in item]
        return item
    return digest(identity(value))

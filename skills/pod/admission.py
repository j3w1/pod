"""Bounded, read-only admission facts. The host serializes and authorizes effects."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import contextvars
import json
import hashlib
import math
import os
import re
import stat
import threading
import time
from collections import Counter
from pathlib import Path

from .errors import PodError
from . import gitio, history, listing, process_exit
from .github import repository_context
from .ledger import binding_valid, record_name, state_root
from .orca import DEADLINE_SPENT, FULL_JSON, WORKER_TERMINAL_STATES as TERMINAL_STATES, _read, _passable, complete_list, read_deadline
from .util import digest as _digest, public_decisions
from .window import Budget

MAX_TIMEOUT = 30.0
MAX_CLAIM_BYTES = 8 * 1024 * 1024
MAX_ORCA_READS = 8  # Orca CLI subprocesses one call runs at once.
POOL_WORKERS = 12
GIT_WORKERS = 4  # Foreign worktree identity reads started from inventory rows, in their own pool.
WORKER_LIST = ["orchestration", "worker-list", "--include-remote", *FULL_JSON]
# The single query's input faults; a batch names the query that raised one.
INPUT_FAULTS = ("admission_candidate", "admission_target", "admission_timeout", "admission_scope_unavailable",
                "repository_identity_unavailable", "git_unavailable")


def digest(value):
    try:
        return _digest(value)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PodError("invalid_record", "Admission facts cannot be encoded canonically") from None


RECORD_CENSUS = "pod-admission-record-census/v1"


def _no_census() -> dict:
    return {"schema": RECORD_CENSUS, "complete": False, "scope_sha256": None,
            "cohort_sha256": None, "records_sha256": None, "binding_sha256": None}


def _finish_censuses(results, budget, *, stable=True) -> None:
    """Only retain a positive witness over the final projection, after every call-level phase.

    A changed projection invalidates its earlier proof; recomputing a hash cannot repair missing work.
    """
    try:
        budget.check()
        for result in results:
            budget.check()
            witness = result.get("record_census")
            valid = (stable and isinstance(witness, dict) and set(witness) == set(_no_census())
                     and witness.get("schema") == RECORD_CENSUS and witness.get("complete") is True
                     and all(isinstance(witness[key], str) and history.HEX.fullmatch(witness[key])
                             for key in ("scope_sha256", "cohort_sha256", "records_sha256", "binding_sha256")))
            if valid:
                public = {key: value for key, value in result.items() if key != "record_census"}
                valid = witness["binding_sha256"] == digest({
                    **{key: value for key, value in witness.items() if key != "binding_sha256"},
                    "result_sha256": digest(public)})
            if not valid:
                result["record_census"] = _no_census()
        budget.check()
    except PodError:
        for result in results:
            result["record_census"] = _no_census()


def _census_stat(path: Path) -> dict | None:
    """No-follow filesystem generation, including replacement and in-place changes; never a new query fault."""
    try:
        value = path.lstat()
        return {"device": value.st_dev, "inode": value.st_ino, "mode": value.st_mode,
                "size": value.st_size, "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}
    except FileNotFoundError:
        return None
    except OSError:
        return {"unavailable": True}


def _census_entry(entry) -> dict:
    identity = _census_stat(Path(entry.path))
    try:
        if identity is None or identity.get("inode") != entry.inode():
            identity = {"unavailable": True}
    except OSError:
        identity = {"unavailable": True}
    return {"name": entry.name, "identity": identity}


def _phase_closure(directory: Path):
    from .evidence_closure import FILE, block
    identity = _census_stat(directory / FILE)
    closure = block(directory)
    _shared(("census-closure", str(directory)), lambda: {"identity": identity, "value": closure})
    return closure


class _RecordCensus:
    """Positive record work, independent of uncertain native owner/process facts and of snapshot relevance.

    Private rows never leave the call. Every visible entry must acquire a proved classification; every
    consulted generation, including safe foreign exclusions, must survive the shared end recheck exactly.
    """

    def __init__(self, root: Path):
        self.root = root
        self.records = {}
        self.classified = set()
        self.cohort = None
        self.finished = False
        self.sound = True
        # Records whose evidence closure this query's own phase judgments consulted, directly or through a shared
        # foreign-owner judgment it used. The call reads each closure once, but only these bind this query's witness.
        self.phases = set()

    def attribution(self, directory, raw, generation):
        try:
            self.records[directory.name] = {"attribution_generation": generation,
                                            "attribution_sha256": digest(_attributed(raw))}
        except PodError:
            self.sound = False

    def classify(self, directory, raw, kind, rows=None):
        try:
            record = self.records[directory.name]
            record["classification"] = kind
            if rows is not None:
                generation = digest(raw["_facts_generation"]) if "_facts_generation" in raw else record["attribution_generation"]
                self.sound &= generation == record["attribution_generation"]
                record.update(claims_generation=generation, claims_sha256=_claim_identity(raw, rows))
            self.classified.add(directory.name)
        except (PodError, KeyError):
            self.sound = False

    def witness(self, result, budget, journals, consulted, policies) -> dict:
        discovery = _SHARED.get()
        if not self.finished or not self.sound or discovery is None:
            return _no_census()
        try:
            budget.check()
            first = discovery.done(("census-cohort", str(self.root)))
            final = _shared(("census-cohort", str(self.root)), lambda: None)
            root_before = discovery.done(("census-root", str(self.root)))
            root_after = _shared(("census-root", str(self.root)), lambda: {"unavailable": True})
            if (first is None or final is None or first != final or root_before != root_after
                    or root_before is not None and ("unavailable" in root_before
                                                    or not stat.S_ISDIR(root_before["mode"]))
                    or self.cohort != {row["name"] for row in first}
                    or self.classified != self.cohort):
                return _no_census()
            rows = []
            for entry in first:
                budget.check()
                name, identity = entry["name"], entry["identity"]
                if identity is None or "unavailable" in identity or not stat.S_ISDIR(identity["mode"]):
                    return _no_census()
                directory = self.root / name
                record = dict(self.records[name])
                if _after_context(directory / "context.json") != record["attribution_generation"]:
                    return _no_census()
                # A foreign record can be excluded by attribution alone, then consulted for a native placement.
                # Bind and check that claims read too, without opening otherwise irrelevant foreign history.
                if directory in consulted:
                    facts = discovery.done(("facts", str(directory / "context.json"), True))
                    if not isinstance(facts, dict) or facts.get("complete") is not True:
                        return _no_census()
                    generation = digest(_generation(facts.get("generation")))
                    if generation != record["attribution_generation"]:
                        return _no_census()
                    record["consulted_claims_generation"] = generation
                if name in journals:
                    record["journal_generation"] = journals[name]
                    if _after_journal(directory / "governor.json") != journals[name]:
                        return _no_census()
                # Only a closure this query's own judgments consulted; another query's read in the call is not its.
                closure = discovery.done(("census-closure", str(directory))) if name in self.phases else None
                if closure is not None:
                    if (closure["identity"] is not None and ("unavailable" in closure["identity"]
                            or not stat.S_ISREG(closure["identity"]["mode"]))
                            or isinstance(closure["value"], dict) and closure["value"].get("status") != "recorded"):
                        return _no_census()
                    _shared(("evidence-closure", str(directory)), lambda: _phase_closure(directory))
                    if _shared(("census-closure", str(directory)), lambda: None) != closure:
                        return _no_census()
                    record["phase_closure_sha256"] = digest(closure)
                rows.append({"name": name, **record})
            witness = {"schema": RECORD_CENSUS, "complete": True,
                       "scope_sha256": digest({"repo_key": result["scope"]["repo_key"],
                                               "path": result["scope"]["path"], "state_root": str(self.root)}),
                       "cohort_sha256": digest({"root": root_before, "entries": first}),
                       "records_sha256": digest({"records": rows, "effect_policies": policies})}
            witness["binding_sha256"] = digest({**witness, "result_sha256": digest(result)})
            budget.check()
            return witness
        except (PodError, OSError, ValueError, TypeError, KeyError):
            return _no_census()


# --------------------------------------------------------------------------- shared discovery

_SHARED: contextvars.ContextVar = contextvars.ContextVar("pod_admission_discovery", default=None)
_FENCING: contextvars.ContextVar = contextvars.ContextVar("pod_admission_fencing", default=False)


class _Discovery:
    """The reads one admission call shares between its queries; nothing outlives the call.

    Each fact is read once per call and stage: `first` for discovery, `recheck` for the final change
    recheck, which therefore never reuses a discovery read. A read runs once, in the caller's thread
    (`memo`) or in the bounded pool (`submit`), and every query receives its outcome, value or
    exception, at the point where it uses the fact, so faults stay where the single query raised them.
    Pool tasks run in a copy of the submitting context, which carries the shared read deadline.
    At most `MAX_ORCA_READS` Orca CLI subprocesses run at once. A foreign worktree's Git identity read
    starts from its inventory row in a separate pool of `GIT_WORKERS` threads. `close` waits for every thread.
    """

    def __init__(self):
        self.stage = "first"
        self.gate = threading.BoundedSemaphore(MAX_ORCA_READS)
        self.closed = False
        self.fleet_read = None
        self.orca_reads = 0
        self.terminal_start = None
        self.fence_started = False  # After this, only the fence's own end reads may read Orca.
        self.abandoned: set = set()  # Dispatches the queries' records account as abandoned.
        self.owner_runs: set = set()  # Runs whose current owner some query observes; changed under `_lock`.
        # R212: each query's candidate scope, registered at its record-pass yield; the Dispatches some query counts
        # exited, fixed before the end fence starts; and the call's own clock for the verifier's reserve.
        self.host_exit_slots: list = []
        self.host_exit_counted: tuple = ()
        self.host_exit_summary = {"requests": 0, "asked": 0, "counted": 0}
        self.clock = time.monotonic
        self.started = None
        self.finished_at: dict = {}
        # The fleet read discovery used, for the batch fence: {"page": its newest-first page, "rows": every row}.
        self.fleet_start = None
        self._lock = threading.Lock()
        self._memo: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="pod-admission")
        self._git = ThreadPoolExecutor(max_workers=GIT_WORKERS, thread_name_prefix="pod-admission-git")
        self._placed: set = set()  # Workspace paths whose identity read has started.

    def memo(self, key, fn, *, stage=None):
        full = (stage or self.stage, *key)
        with self._lock:
            future = self._memo.get(full)
            owner = future is None
            if owner:
                future = self._memo[full] = Future()
        if owner:
            try:
                future.set_result(fn())
            except BaseException as exc:  # Every query sees the same outcome, then raises it where it reads.
                future.set_exception(exc)
        return future.result()

    def submit(self, key, fn, *, stage=None) -> Future:
        full = (stage or self.stage, *key)
        with self._lock:
            future = self._memo.get(full)
            if future is not None:
                return future
            future = self._memo[full] = Future()
            if self.closed:
                future.set_exception(PodError(DEADLINE_SPENT, "The admission call has ended"))
                return future
            context = contextvars.copy_context()
            self._pool.submit(self._run, future, context, fn)
            return future

    @staticmethod
    def _run(future, context, fn):
        try:
            future.set_result(context.run(fn))
        except BaseException as exc:
            future.set_exception(exc)

    def finish(self, name, value):
        """Stamp when a discovery inventory read returned, on the call's clock (R212's reserve)."""
        self.finished_at[name] = self.clock()
        return value

    def done(self, key):
        """A completed first-stage value, or None; never waits and never reads."""
        future = self._memo.get(("first", *key))
        if future is None or not future.done() or future.exception() is not None:
            return None
        return future.result()

    def ordered(self):
        """Every native read a result uses happens before the fence's end reads start."""
        if self.fence_started and not _FENCING.get():
            raise PodError("native_read_after_fence", "A native read was requested after the end fence started")

    def read(self, argv):
        self.ordered()
        with self.gate:
            with self._lock:
                self.orca_reads += 1
            return _read(argv)

    def prefetch_claim(self, row, runtime):
        """Start an abandoned worker's current-claim read as soon as its inventory row is known."""
        key = row.get("dispatchId") if isinstance(row, dict) else None
        if _FENCING.get():
            return  # The fence re-reads exactly the claims discovery read, never new ones.
        if isinstance(key, str) and key and isinstance(runtime, str):
            self.submit(("claim", key, runtime), lambda: _native_claim(row, runtime))

    def prefetch_place(self, row):
        """Start a current row's workspace Git identity read (rule T 4b) as soon as its inventory row is listed.

        A released row reads exited and reaches the 4b proof only when abandoned. The read is the same per-call
        memo `elsewhere` reads, so each query still receives that one read's value or fault where it judges."""
        if _FENCING.get() or not isinstance(row, dict):
            return
        if row.get("terminalState") == "released" and row.get("workerState") != "abandoned":
            return
        path = _row_location(row)
        if not isinstance(path, str) or not path.startswith("/") or os.path.normpath(path) != path:
            return
        with self._lock:
            if self.closed or path in self._placed:
                return
            self._placed.add(path)
            self._git.submit(contextvars.copy_context().run, _prefetch_identity, self, path)

    def name_owners(self, runs):
        """A record pass names the Runs whose owner it observes; each read starts once the inventory is known."""
        with self._lock:
            self.owner_runs |= set(runs)
        self.start_owners()

    def start_owners(self):
        """Start each named Run owner's read once the terminal inventory is known; never waits. Called when a record
        pass names Runs and when the terminal inventory completes, so each read starts once both are known."""
        terminals = self.done(("terminals",))
        if not isinstance(terminals, dict) or not isinstance(terminals.get("runtime"), str):
            return
        with self._lock:
            runs = sorted(self.owner_runs)
        for run in runs:
            self.submit(("owner", run, terminals["runtime"]), lambda run=run: _gated(
                _owner_observation, run, terminals, terminals["runtime"]), stage="first")

    def close(self):
        with self._lock:
            self.closed = True
        self._git.shutdown(wait=True, cancel_futures=True)
        self._pool.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            futures = sorted(self._memo.items(), key=lambda item: repr(item[0]))
        for _, future in futures:
            if not future.done():
                future.set_exception(PodError(DEADLINE_SPENT, "The admission call has ended"))
        # A programming fault in a background read is never hidden, whether or not a query used it.
        for _, future in futures:
            exc = future.exception()
            if exc is not None and not isinstance(exc, (PodError, OSError)):
                raise exc


def _shared(key, fn):
    """Read once per call when a call shares discovery; otherwise read now."""
    discovery = _SHARED.get()
    return fn() if discovery is None else discovery.memo(key, fn)


def _row_location(row: dict):
    """The workspace path an inventory row names, as `_worker_fact` reads it; None when it names none."""
    resource = row.get("resource") if isinstance(row.get("resource"), dict) else {}
    projection = row.get("projection") if isinstance(row.get("projection"), dict) else {}
    workspace = projection.get("workspace") if isinstance(projection.get("workspace"), dict) else {}
    try:
        return listing._location(resource.get("worktreeId") or workspace.get("id"))
    except (PodError, TypeError, ValueError, AttributeError):
        return None


def _prefetch_identity(discovery, path: str) -> None:
    """Rule T 4b's Git identity of a listed workspace, read ahead under the same checks `elsewhere` makes first."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode) or str(Path(path).resolve(strict=True)) != path:
            return
        discovery.memo(("context", str(Path(path))), lambda: repository_context(Path(path), fresh=True),
                       stage="first")
    except (OSError, PodError, ValueError, RuntimeError):
        return  # `elsewhere` meets the same outcome where it judges the path.


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _resolved(path: str) -> Path:
    """A known repository path resolved once per call."""
    return _shared(("resolved", path), lambda: _resolve(path))


def _gated_read(argv):
    discovery = _SHARED.get()
    return _read(argv) if discovery is None else discovery.read(argv)


def _gated(fn, *args):
    discovery = _SHARED.get()
    if discovery is None:
        return fn(*args)
    discovery.ordered()
    with discovery.gate:
        with discovery._lock:
            discovery.orca_reads += 1
        return fn(*args)


def _claim_reference(reference: dict, directory: Path):
    value = reference.get(history.MARKER)
    if not isinstance(value, dict) or type(value.get("bytes")) is not int or value["bytes"] > MAX_CLAIM_BYTES:
        raise PodError("admission_claim_limit", "One archived claim exceeds the bounded query working set")
    store = history.ChunkStore(directory)
    def load(address):
        history._check_read_deadline()
        return store.load(address)
    return history._expand(reference, load, {})


# --------------------------------------------------------------------------- native inventory

def _fleet() -> dict | None:
    """One complete native inventory for CE's unbound caller; bound callers fall back.

    Orca decides the scope. Omitting --run must never turn a bound Run's empty
    list into proof that the repository has no other worker. Every page is read, however many workers the fleet
    has (R206); a missing or repeated cursor still refuses, and the read deadline still applies.

    Discovery reads only this newest-first cursor walk. Its cursor is a keyset position, so every Dispatch
    that exists for the whole walk is listed exactly once, whatever state it changes to. Inside a batch the
    walk is also the fence's start (`fleet_start`): total and counts from its first page, the newest rows
    from that page, and the unreleased rows, the released rows and the abandoned claims from the walk. The
    end fence reads every page of the walk again, in parallel, from the cursor Orca issued for it, and
    compares each page's Dispatches and each Dispatch's identity at both ends, so every race shows there: a
    Dispatch created during the walk changes the end's newest page; a deletion or a move changes a page's
    Dispatches; any identity change of any row, in any state, alters the end's digests. The end fence is a
    pure comparison, so a race during its own reads only makes it unequal, which fails safe.
    """
    discovery = _SHARED.get()
    fleet = _fleet_sequential(discovery)
    if discovery is not None:
        discovery.fleet_read = "sequential" if fleet is not None else "bound"
    return fleet


def _fleet_sequential(discovery=None) -> dict | None:
    prefix = [*WORKER_LIST, "--limit", "100"]
    cursor, seen, workers, runtime, pages = None, set(), {}, None, []
    while True:
        response = _gated_read(prefix + (["--cursor", cursor] if cursor else []))
        first = response if cursor is None else first
        result = response["result"]
        scope = result.get("scope")
        if isinstance(scope, dict) and scope.get("source") == "bound" and cursor is None:
            if discovery is not None:
                discovery.fleet_start = PodError("native_inventory_scope", "A bound caller cannot fence all-Run coverage")
            return None
        if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
            raise PodError("native_inventory_scope", "Native inventory does not prove all-Run coverage")
        if runtime is not None and runtime != response["runtime"]:
            raise PodError("orca_runtime_changed", "Native runtime changed between inventory pages")
        runtime = response["runtime"]
        rows, page = result.get("workers"), result.get("page")
        if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(page, dict)
                or type(page.get("hasMore")) is not bool or result.get("truncated", False) is not False):
            raise PodError("native_inventory_incomplete", "Native inventory page is incomplete")
        for row in rows:
            key = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in workers:
                raise PodError("orca_pagination", "Native inventory repeats or omits a Dispatch identity")
            workers[key] = row
            if discovery is not None and row.get("workerState") == "abandoned":
                discovery.prefetch_claim(row, runtime)
            if discovery is not None:
                discovery.prefetch_place(row)
        # The end fence reads this page again from the same cursor Orca issued for it (R207).
        pages.append({"cursor": cursor, "ids": [row["dispatchId"] for row in rows], "more": page["hasMore"]})
        if not page["hasMore"]:
            if discovery is not None:
                discovery.fleet_start = {"page": first, "rows": list(workers.values()), "pages": pages}
            return {"runtime": runtime, "workers": list(workers.values()), "complete": True}
        cursor = page.get("nextCursor")
        if not rows or not isinstance(cursor, str) or not cursor or cursor in seen:
            raise PodError("orca_pagination", "Native inventory cursor is missing or repeated")
        seen.add(cursor)


def _rejected(message: str) -> PodError:
    return PodError("native_inventory_partition", message)


def _counts(value) -> dict:
    if (not isinstance(value, dict) or not set(value) <= set(TERMINAL_STATES)
            or any(type(count) is not int or count < 0 for count in value.values())):
        raise _rejected("Inventory counts name an unknown terminal state or are malformed")
    return value


def _worker_fact(row: dict) -> dict:
    if not isinstance(row, dict):
        raise PodError("orca_contract", "Native worker row is malformed")
    projection = row.get("projection")
    if not isinstance(projection, dict):
        raise PodError("orca_contract", "Native worker projection is unavailable")
    resource = row.get("resource") or {}
    workspace = projection.get("workspace") or {}
    release = projection.get("resource") or {}
    live = projection.get("liveness") or {}
    if not all(isinstance(value, dict) for value in (resource, workspace, release, live)):
        raise PodError("orca_contract", "Native worker claim facts are malformed")
    dispatch, run, task = row.get("dispatchId"), row.get("runId"), row.get("taskId")
    if (not all(isinstance(value, str) and value and _passable(value) for value in (dispatch, run, task))
            or any(projection.get(field) != value for field, value in
                   (("dispatchId", dispatch), ("runId", run), ("taskId", task)))):
        raise PodError("orca_contract", "Native worker identity differs")
    path = listing._location(resource.get("worktreeId") or workspace.get("id"))
    if path is not None and not _passable(path):
        raise PodError("orca_contract", "Native worker location is not valid UTF-8")
    return {"dispatch": dispatch, "run": run, "task": task,
            "worker": projection.get("id"),
            "terminal": row.get("agentTerminalHandle", resource.get("terminalHandle")),
            "resource": {key: resource.get(key) for key in
                         ("id", "ownerDispatchId", "ownershipState", "releaseState", "endpointId", "endpointIncarnation")},
            "worktree": path,
            "liveness": live.get("verdict", "unverifiable"), "released": release.get("state") == "released"}


def _terminal_rows(response: dict) -> list[dict]:
    """The complete, well-formed terminal rows of one whole-inventory read (R113)."""
    from .retirement import _complete
    rows = _complete(response["result"], "terminals")
    if rows is None:
        raise PodError("native_resources_incomplete", "Native terminal ownership inventory is incomplete")
    for row in rows:
        if not isinstance(row.get("handle"), str) or not row["handle"] or not _passable(row["handle"]):
            raise PodError("native_resources_invalid", "Native terminal identity is malformed")
        for field in ("worktreePath", "worktreeId"):
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or not _passable(value)):
                raise PodError("native_resources_invalid", "Native terminal location is malformed")
    return rows


def _terminals() -> dict:
    """Current native ownership also covers terminals outside supervised workers."""
    response = complete_list("terminal", _gated_read)
    discovery = _SHARED.get()
    if discovery is not None:
        discovery.terminal_start = response  # The batch fence fingerprints this same read.
    rows = _terminal_rows(response)
    return {"runtime": response["runtime"], "complete": True, "terminals": [
        {key: row.get(key) for key in ("handle", "worktreePath", "worktreeId", "connected", "writable", "orphaned", "exitCause")}
        for row in rows]}  # Presentation/preview text never leaves this reader.


def _identity_row(row) -> dict:
    """The fields of one worker row that Pod's claims use, null when absent (the fleet fence's row identity)."""
    def part(value, *names):
        value = value if isinstance(value, dict) else {}
        return {name: value.get(name) for name in names}
    row = row if isinstance(row, dict) else {}
    projection = row.get("projection") if isinstance(row.get("projection"), dict) else {}
    return {**part(row, "dispatchId", "runId", "taskId", "terminalState", "workerState", "dispatchStatus",
                   "agentTerminalHandle"),
            "resource": part(row.get("resource"), "id", "ownerDispatchId", "ownershipState", "releaseState",
                             "terminalHandle", "worktreeId"),
            "projection": {"id": projection.get("id"), "liveness": part(projection.get("liveness"), "verdict"),
                           "workspace": part(projection.get("workspace"), "id"),
                           "resource": part(projection.get("resource"), "state")}}


def _rows_digest(runtime: str, rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"runtime": runtime, "rows": rows}, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim_fact(dispatch: str, claim) -> dict:
    """The claim Pod derived from one abandoned Dispatch's current read."""
    if claim is None:
        return {"dispatch": dispatch, "process": "exited", "effects": []}
    return {"dispatch": dispatch, "process": claim.get("process"), "effects": claim.get("effects")}


def _fleet_side(observed, claims, counted=()) -> dict:
    """One side of the fleet identity fence over one complete all-Run read, or the code that refused it.

    `observed` is {"page": the unfiltered newest-first page, "rows": every row read} or the fault;
    `claims` maps each abandoned Dispatch whose claim discovery read to its claim, or to the fault;
    `counted` names the Dispatches some query counted exited on the host's verdict (R212), whose extended identity
    this side digests too."""
    runtime = None
    try:
        # A spent budget anywhere this side depends on is its code, before a failed or invalid fleet read (R207).
        _raise_first([value for value in (observed, *claims.values())
                      if isinstance(value, PodError) and value.code in BUDGET_CODES])
        if isinstance(observed, BaseException):
            raise observed
        if observed is None:
            raise PodError("native_fence_unavailable", "Discovery made no fleet read to fence")
        page_read = observed["page"]
        runtime = page_read.get("runtime")
        result = page_read["result"]
        scope, page, rows = result.get("scope"), result.get("page"), result.get("workers")
        if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
            raise PodError("native_inventory_scope", "The fleet read does not prove all-Run coverage")
        counts = _counts(result.get("counts"))
        if (not isinstance(page, dict) or type(page.get("total")) is not int or page["total"] < 0
                or not isinstance(rows, list) or len(rows) > 100):
            raise PodError("native_inventory_incomplete", "The fleet page has no total, counts or rows")
        if not isinstance(runtime, str) or not _passable(runtime):
            raise PodError("orca_contract", "The fleet read has no runtime")
        unreleased = sorted((_identity_row(row) for row in observed["rows"] if row.get("terminalState") != "released"),
                            key=lambda row: str(row["dispatchId"]))
        released = sorted((_identity_row(row) for row in observed["rows"] if row.get("terminalState") == "released"),
                          key=lambda row: str(row["dispatchId"]))
        newest = sorted((_identity_row(row) for row in rows), key=lambda row: str(row["dispatchId"]))
        _raise_first([claims[dispatch] for dispatch in sorted(claims)])  # budget exhaustion first (R207)
        facts = [_claim_fact(dispatch, claims[dispatch]) for dispatch in sorted(claims)]
        pages = observed.get("pages")
        if not isinstance(pages, list) or not pages:
            raise PodError("native_inventory_incomplete", "The fleet read names no pages")
        by_dispatch = {row.get("dispatchId"): row for row in observed["rows"] if isinstance(row, dict)}
        exits = [_host_exit_identity(by_dispatch[dispatch]) if dispatch in by_dispatch
                 else {"dispatchId": dispatch, "missing": True} for dispatch in counted]
        side = {"runtime": runtime, "complete": True, "total": page["total"], "counts": dict(counts),
                "unreleased": len(unreleased), "unreleased_sha256": _rows_digest(runtime, unreleased),
                "released": len(released), "released_sha256": _rows_digest(runtime, released),
                "newest": len(newest), "newest_sha256": _rows_digest(runtime, newest),
                "pages": len(pages), "pages_sha256": _rows_digest(runtime, [[page["ids"], page["more"]] for page in pages]),
                "claims_read": len(facts), "claims_sha256": _rows_digest(runtime, facts),
                "host_exit": len(exits), "host_exit_sha256": _rows_digest(runtime, exits)}
    except PodError as exc:
        return {"runtime": runtime if isinstance(runtime, str) and _passable(runtime) else None,
                "complete": False, "code": exc.code}
    except OSError:
        return {"runtime": runtime if isinstance(runtime, str) and _passable(runtime) else None,
                "complete": False, "code": "native_inventory_unreadable"}
    except (KeyError, TypeError, AttributeError, UnicodeError, ValueError, RecursionError):
        return {"runtime": None, "complete": False, "code": "native_inventory_incomplete"}
    return side


BUDGET_CODES = (DEADLINE_SPENT, "admission_timeout")


def _raise_first(outcomes) -> None:
    """Raise one of the faults among `outcomes`, a spent budget before any other, so the fence's reason never hides
    that the call ran out of time behind a concurrent failure (R207)."""
    faults = [value for value in outcomes if isinstance(value, BaseException)]
    for fault in faults:
        if isinstance(fault, PodError) and fault.code in BUDGET_CODES:
            raise fault
    if faults:
        raise faults[0]


def _walk_page(response, runtime) -> list:
    """One end read of a walk page, checked as the walk checks its pages: all-Run scope, one runtime, a whole page
    of at most 100 rows each with a distinct Dispatch identity."""
    result = response["result"]
    scope = result.get("scope")
    if isinstance(scope, dict) and scope.get("source") == "bound":
        raise PodError("native_inventory_scope", "A bound caller cannot fence all-Run coverage")
    if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
        raise PodError("native_inventory_scope", "A fence page does not prove all-Run coverage")
    if response.get("runtime") != runtime:
        raise PodError("orca_runtime_changed", "Native runtime changed between fence reads")
    rows, page = result.get("workers"), result.get("page")
    if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(page, dict)
            or type(page.get("hasMore")) is not bool or result.get("truncated", False) is not False):
        raise PodError("native_inventory_incomplete", "A fence page is incomplete")
    ids = [row.get("dispatchId") if isinstance(row, dict) else None for row in rows]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise PodError("orca_pagination", "A fence page repeats or omits a Dispatch identity")
    return rows


def _fleet_end(discovery, pages) -> dict:
    """A fresh complete fleet read for the fence: every page of discovery's walk again, read in parallel, each from
    the cursor Orca issued for it during this call (R207). Membership is then each page's Dispatches, compared
    with the start's; every failure is kept, a spent budget first."""
    if not isinstance(pages, list) or not pages:
        raise PodError("native_fence_unavailable", "Discovery made no fleet walk to read again")
    futures = [discovery.submit(("walk", index), lambda page=page: discovery.read(
                   [*WORKER_LIST, "--limit", "100", *(["--cursor", page["cursor"]] if page["cursor"] else [])]),
                   stage="fence") for index, page in enumerate(pages)]
    responses = []
    for future in futures:
        try:
            responses.append(future.result())
        except (PodError, OSError) as exc:
            responses.append(exc)
    _raise_first(responses)
    runtime = responses[0].get("runtime")
    rows, read = [], []
    for response in responses:
        page_rows = _walk_page(response, runtime)
        rows.extend(page_rows)
        read.append({"ids": [row["dispatchId"] for row in page_rows], "more": response["result"]["page"]["hasMore"]})
    return {"page": responses[0], "rows": rows, "pages": read}


def _native_fence(discovery) -> dict:
    """The batch fence: discovery's terminal and fleet reads against fresh end reads. The call starts it
    once every native read any result uses is done, so its reads are the call's last native reads, and
    runs the remaining projection and the shared recheck beside it. Never cached."""
    # Every claim read discovery started is part of what it read; wait for each before fencing.
    claimed = {}
    for key, future in list(discovery._memo.items()):
        if key[:2] == ("first", "claim"):
            try:
                claimed[key[2:]] = future.result()
            except (PodError, OSError) as exc:
                claimed[key[2:]] = exc
    start = discovery.terminal_start
    if start is None:
        future = discovery._memo.get(("first", "terminals"))
        start = future.exception() if future is not None and future.done() else None
    fleet = discovery.done(("fleet",))
    fleet_start = discovery.fleet_start
    if fleet_start is None:
        future = discovery._memo.get(("first", "fleet"))
        fleet_start = future.exception() if future is not None and future.done() else None
    runtime = fleet["runtime"] if isinstance(fleet, dict) else None
    sources = {row.get("dispatchId"): row for row in (fleet or {}).get("workers", []) if isinstance(row, dict)}
    start_claims = {dispatch: value for (dispatch, observed), value in claimed.items() if observed == runtime}
    reads = {"end": discovery.submit(("terminals",), lambda: complete_list("terminal", discovery.read), stage="fence"),
             "fleet": discovery.submit(("fleet",), lambda: _fleet_end(
                 discovery, fleet_start.get("pages") if isinstance(fleet_start, dict) else None), stage="fence")}
    claim_reads = {dispatch: discovery.submit(("claim", dispatch, runtime), lambda dispatch=dispatch: _native_claim(
                       sources[dispatch], runtime), stage="fence") for dispatch in start_claims if dispatch in sources}
    observed = {}
    for name, future in [*reads.items(), *(("claim:" + key, value) for key, value in claim_reads.items())]:
        try:
            observed[name] = future.result()
        except (PodError, OSError) as exc:
            observed[name] = exc
    end_claims = {dispatch: observed.get("claim:" + dispatch, PodError("native_identity_unverified", "No source row"))
                  for dispatch in start_claims}
    counted = discovery.host_exit_counted  # fixed before the end fence started
    fence = {"start": _fence_side(start, rows=False), "end": _fence_side(observed["end"], rows=True),
             "fleet_start": _fleet_side(fleet_start, start_claims, counted),
             "fleet_end": _fleet_side(observed["fleet"], end_claims, counted)}
    sides = list(fence.values())
    compared = ("total", "counts", "unreleased", "unreleased_sha256", "released", "released_sha256",
                "newest", "newest_sha256", "pages", "pages_sha256", "claims_read",
                "claims_sha256", "host_exit", "host_exit_sha256")
    fence["stable"] = bool(all(side["complete"] for side in sides)
                           and len({side["runtime"] for side in sides}) == 1
                           and fence["start"]["sha256"] == fence["end"]["sha256"]
                           and all(fence["fleet_start"][key] == fence["fleet_end"][key] for key in compared))
    fence["reason"] = _fence_reason(fence)
    fence["host_exit_dispatches"] = list(counted)
    return fence


def _fence_reason(fence) -> str | None:
    """Why the fence is unstable, budget exhaustion first: null when it is stable (R207)."""
    if fence["stable"]:
        return None
    sides = [fence[name] for name in ("start", "end", "fleet_start", "fleet_end")]
    codes = [side.get("code") for side in sides if not side["complete"]]
    if any(code in BUDGET_CODES for code in codes):
        return "discovery_budget"
    if codes:
        return "incomplete"
    if len({side["runtime"] for side in sides}) != 1:
        return "runtime_changed"
    if fence["start"]["sha256"] != fence["end"]["sha256"]:
        return "terminals_changed"
    return "fleet_changed"


FENCE_FIELDS = ("handle", "incarnationId", "worktreePath", "worktreeId", "executionHostId", "agentIdentity",
                "connected", "writable", "orphaned")


def fence_fingerprint(runtime: str, rows: list[dict]) -> str:
    """The documented native_fence rule; a consumer computes the same value from its own complete read."""
    return hashlib.sha256(json.dumps({"runtime": runtime, "terminals": rows}, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _fence_side(response, *, rows: bool) -> dict:
    """One side of the batch fence: a fingerprint of a complete terminal read, or the code that refused it."""
    runtime = response.get("runtime") if isinstance(response, dict) else None
    runtime = runtime if isinstance(runtime, str) and _passable(runtime) else None
    try:
        if isinstance(response, BaseException):
            raise response
        if response is None:
            raise PodError("native_fence_unavailable", "Discovery made no fingerprintable terminal read")
        terminals = sorted(({field: row.get(field) for field in FENCE_FIELDS} for row in _terminal_rows(response)),
                           key=lambda row: row["handle"])
        if len({row["handle"] for row in terminals}) != len(terminals):
            raise PodError("native_resources_invalid", "Native terminal identities repeat")
        sha = fence_fingerprint(runtime, terminals)
    except PodError as exc:
        return {"runtime": runtime, "complete": False, "count": None, "code": exc.code}
    except OSError:
        return {"runtime": runtime, "complete": False, "count": None, "code": "native_resources_unreadable"}
    except (UnicodeError, ValueError, TypeError, RecursionError, KeyError, AttributeError):
        return {"runtime": runtime, "complete": False, "count": None, "code": "native_resources_invalid"}
    side = {"runtime": runtime, "complete": True, "count": len(terminals), "sha256": sha}
    if rows:
        side["terminals"] = terminals
    return side


def _native_claim(source: dict, runtime: str):
    """The W1-owned interpreter alone classifies an abandoned attempt's effects."""
    try:
        from .abandoned import current_claim, is_abandoned
    except ImportError:
        raise PodError("native_claim_reader_unavailable", "The shared current-claim reader is not installed") from None
    response = _gated_read(["orchestration", "worker-show", "--dispatch", source["dispatchId"], *FULL_JSON])
    result = response.get("result") or {}
    dispatch, worker, projection = (result.get(name) or {} for name in ("dispatch", "worker", "projection"))
    if (not all(isinstance(row, dict) for row in (dispatch, worker, projection))
            or response.get("runtime") != runtime or not is_abandoned(response)
            or any(dispatch.get(key) != source.get(field) for key, field in
                   (("id", "dispatchId"), ("runId", "runId"), ("taskId", "taskId")))
            or projection.get("id") != (source.get("projection") or {}).get("id")):
        raise PodError("native_identity_unverified", "Current claim observation differs from its inventory identity")
    return current_claim(response)


def _owner_observation(run: str, terminals: dict, runtime: str) -> dict:
    from .orca import run_show
    observed = run_show(run)
    if observed["runtime"] != runtime:
        raise PodError("orca_runtime_changed", "Coordinator ownership and native inventory runtimes differ")
    terminal = next((row for row in terminals["terminals"] if row.get("handle") == observed["coordinator_handle"]), None)
    return {"run": run, "runtime": runtime, "terminal": observed["coordinator_handle"],
            "consumer_generation": observed["consumer_generation"],
            "worktree": (terminal or {}).get("worktreePath"),
            "live": bool(terminal and terminal.get("connected") is True and terminal.get("writable") is True
                         and terminal.get("orphaned") is False), "source": "native_run_and_terminal"}


def _primary(path: Path) -> tuple[dict, str]:
    reader = getattr(history, "load_admission_facts", None)
    if reader is not None:
        # Read once per call, classified by revision: a record that keeps changing is never unreadable (R206).
        facts = _shared(("facts", str(path), False),
                        lambda: history.scan_read(path, lambda: reader(path, include_admissions=False)))
        raw = _facts_context(facts)
        return raw, digest(facts["generation"])
    # Reuse the existing no-follow, bounded physical reader; do not hydrate receipts.
    data = _shared(("bytes", str(path)), lambda: history._context_bytes(path, history.CONTEXT_LIMIT))
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise PodError("invalid_record", "Cannot decode bounded context JSON") from None
    if not isinstance(raw, dict):
        raise PodError("state_unsupported", "Context is not an object")
    return raw, hashlib.sha256(data).hexdigest()


def _generation(value):
    if (not isinstance(value, dict) or type(value.get("revision")) is not int or value["revision"] < 0
            or not isinstance(value.get("sha256"), str) or not history.HEX.fullmatch(value["sha256"])):
        raise PodError("admission_facts_unsupported", "Facts generation is unavailable")
    return value


def _facts_context(facts: dict) -> dict:
    if (not isinstance(facts, dict) or facts.get("schema") != "pod-admission-facts/v1"
            or facts.get("attribution_complete") is not True or not isinstance(facts.get("context"), dict)
            or facts.get("phase") not in ("open", "closed", "continued", "retired")):
        raise PodError("admission_facts_unsupported", "Current attribution contract is unavailable")
    return {**facts["context"], "_facts_phase": facts["phase"],
            "_facts_generation": _generation(facts.get("generation")),
            "_facts_attribution": facts.get("attribution")}


def _phase(raw: dict) -> str:
    return raw["_facts_phase"] if "_facts_phase" in raw else listing._phase(raw)


TERMINAL_PHASES = ("closed", "retired", "continued")
# A worker and Dispatch pair that ended: rule S settles only these, never running, ready, abandoned or unknown.
_SETTLED_WORKERS = (("succeeded", "completed"), ("failed", "failed"))


def _record_phase(raw: dict, directory: Path | None, consulted: set | None = None) -> str:
    """The phase `status --list` reads (R108): a readable R145 evidence closure closes an otherwise open record.
    An unreadable evidence closure leaves the record open here, so admission stays fail-closed. A record whose
    closure is consulted is added to `consulted`, the asking query's census dependencies (R213)."""
    phase = _phase(raw)
    if phase != "open" or directory is None:
        return phase
    if consulted is not None:
        consulted.add(directory.name)
    closure = _shared(("evidence-closure", str(directory)), lambda: _phase_closure(directory))
    return "closed" if isinstance(closure, dict) and closure.get("status") == "recorded" else phase


def _retirement_preserved(project: Path, directory: Path) -> set[str] | None:
    """The admissions a validated historical retirement preserved, or None when the entry is unproved.

    Retirement refused while any Dispatch was live or unsettled or any start of a reserved or unresolved admission
    was still pending (R112), and a `retired` record accepts no later write, so an admission it preserved can never
    start or bind afterwards. A current record carries its entry; an older one has it certified beside it (R205)."""
    from . import retirement
    from .ledger import _read
    try:
        state = _read(directory / "context.json")
        entry, older = (state.get(retirement.FIELD) if isinstance(state, dict) else None), False
    except (PodError, OSError, ValueError):
        entry = None
    try:
        if not isinstance(entry, dict):
            found = retirement.older_state(directory, project)
            entry, older = (found[0].get(retirement.FIELD) if found is not None else None), True
        if not isinstance(entry, dict):
            return None
        retirement.validate_entry(entry, older=older)
    except (PodError, OSError, ValueError):
        return None
    return {row["id"] for row in entry["preserved"] if row.get("kind") == "admission"}


def _run_coordinators(runtime: str, check=None) -> dict | None:
    """Every Run's current coordinator handle from Orca's whole paged Run list, or None unless the walk is provably
    complete (rule S, 4): every page from this runtime, an explicit end cursor, no truncated, partial or incomplete
    marker, no repeated cursor, no repeated or empty Run identity and no contradiction between a page's cursor and
    its `hasMore`. The walk has no page count bound (R206); the call's deadline (`check`) and each read's byte bound
    stay."""
    found, cursor, seen = {}, None, set()
    while True:
        if check is not None:
            check()
        response = _gated_read(["orchestration", "run-list", "--limit", "100", *FULL_JSON,
                                *(["--cursor", cursor] if cursor else [])])
        result = response.get("result") if isinstance(response, dict) else None
        if (not isinstance(result, dict) or response.get("runtime") != runtime
                or not isinstance(result.get("runs"), list) or "nextCursor" not in result
                or result.get("truncated", False) is not False or result.get("partial", False) is not False
                or result.get("complete", True) is not True
                or "hasMore" in result and type(result["hasMore"]) is not bool):
            return None
        for row in result["runs"]:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                    or row["id"] in found or "coordinator_handle" not in row):
                return None
            handle = row["coordinator_handle"]
            if handle is not None and (not isinstance(handle, str) or not handle):
                return None
            found[row["id"]] = handle
        cursor = result["nextCursor"]
        if cursor is None:
            return None if result.get("hasMore") is True else found
        if (not isinstance(cursor, str) or not cursor or cursor in seen or not result["runs"]
                or result.get("hasMore") is False):
            return None
        seen.add(cursor)


def _settled_gone(source: dict, shown: dict, rows: list, terminals: dict, owner) -> bool:
    """Rule S (R206): an ended Dispatch whose terminal, workspace and Run coordinator are all gone holds no current
    resource. Every condition is read from this call's complete inventories; anything unproved keeps the gap."""
    if (source.get("workerState"), source.get("dispatchStatus")) not in _SETTLED_WORKERS or shown["liveness"] == "live":
        return False
    handle, path = shown["terminal"], shown["worktree"]
    if not isinstance(handle, str) or not handle or not isinstance(path, str) or not path:
        return False
    if any(isinstance(row, dict) and row.get("handle") == handle for row in terminals["terminals"]):
        # The handle is in use again. Only a proven later Dispatch ends this one's hold: one that took over this
        # Dispatch's own terminal resource (its origin) as owner, ended and released it, with no other holder.
        if not _released_by_later(source, shown["dispatch"], handle, rows):
            return False
    try:
        os.lstat(path)
        return False  # The workspace still exists.
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return False  # An unreadable path proves nothing.
    try:
        coordinator = owner(shown["run"])
    except PodError:
        return False
    # Only explicit absence or recognized exit proves the coordinator gone; a disconnected, orphaned or otherwise
    # unverified coordinator keeps the gap.
    return coordinator.get("state") in ("absent", "exited")


def _released_by_later(source: dict, dispatch: str, handle: str, rows: list) -> bool:
    """Rule S, 2: exactly one other ended Dispatch owns the terminal resource this Dispatch originated, on the same
    handle, and released it; this Dispatch holds no unreleased resource of its own; nothing else holds the handle."""
    def resource(row):
        value = row.get("resource")
        return value if isinstance(value, dict) else None

    def ended_released(row):
        held = resource(row)
        return ((row.get("workerState"), row.get("dispatchStatus")) in _SETTLED_WORKERS and held is not None
                and held.get("releaseState") == "released" and held.get("ownershipState") == "released")
    own = resource(source)
    if own is not None and not ended_released(source):
        return False
    later = [row for row in rows if isinstance(row, dict) and row is not source and isinstance(row.get("dispatchId"), str)
             and row["dispatchId"] != dispatch and row.get("agentTerminalHandle") == handle and ended_released(row)
             and resource(row).get("terminalHandle") == handle and resource(row).get("originDispatchId") == dispatch
             and resource(row).get("ownerDispatchId") == row["dispatchId"]]
    others = [row for row in rows if isinstance(row, dict) and row is not source and row not in later
              and (row.get("agentTerminalHandle") == handle or (resource(row) or {}).get("terminalHandle") == handle)
              and not ended_released(row)]
    return len(later) == 1 and not others


def _physical(path) -> str:
    """A path resolved once per call, whichever query or reader asks first."""
    return _shared(("physical", str(path)), lambda: str(Path(path).resolve()))


def _overlap(left, right) -> bool:
    try:
        left, right = _physical(left), _physical(right)
        return _within(left, right) or _within(right, left)
    except (OSError, ValueError):
        return True  # An inaccessible path cannot establish independence.


def _workspace(project: Path, path, places) -> tuple:
    """The registered worktree of `project` holding `path`, and whether `path` is in that repository at all."""
    if not path:
        return None, False
    def locate():
        inside = _physical(path)
        roots = [root for root in places if _within(inside, _physical(root))]
        return (max(roots, key=len) if roots else None,
                bool(roots) or any(_overlap(path, root) for root in places))
    # The same path in the same repository's worktree registry is located once per call.
    return _shared(("workspace", str(project), str(path)), locate)


def _coordinator(run: str, terminals: dict, budget) -> dict:
    """The Run's current coordinator from one complete Run list per call, before the end fence (rule S, 4)."""
    coordinators = _shared(("run-coordinators", terminals["runtime"]),
                           lambda: _run_coordinators(terminals["runtime"], budget.check))
    if coordinators is None or run not in coordinators:
        raise PodError("orca_contract", "The Run's current coordinator is unproved")
    handle = coordinators[run]
    terminal = next((row for row in terminals["terminals"] if row.get("handle") == handle), None) if handle else None
    if terminal is None:
        return {"terminal": handle, "state": "absent"}  # none recorded, or absent from the complete inventory
    from .retirement import _exited
    if _exited(terminal):
        return {"terminal": handle, "state": "exited"}
    live = (terminal.get("connected") is True and terminal.get("writable") is True
            and terminal.get("orphaned") is False)
    return {"terminal": handle, "state": "live" if live else "unverified"}


class _Owners(tuple):
    """A shared foreign-owner judgment's `(directory, generation, outcome)` entries, and `closures`: the names of the
    owner records whose evidence closure its phase reads consulted. Every query that uses the judgment, whichever
    query first made it, takes those closures as its own census dependencies (R213)."""

    def __new__(cls, entries, closures=()):
        value = super().__new__(cls, entries)
        value.closures = frozenset(closures)
        return value


class _HostExitSlot:
    """One query's R212 scope, registered at its record-pass yield, and what the call decided for its own candidates.

    Candidates and decisions are the query's own: a verdict asked for because of another query never changes this
    query's result."""

    def __init__(self, project: Path, request: dict, runs: set, own_runs: set, gaps: set, abandoned: set, *,
                 scope: dict, owed: dict, owners, unreadable, placements: dict | None = None):
        # `runs`: every Run a record names (rule S scope); `own_runs`: those named by this repository's records whose
        # admissions the query reads, the only Runs on which its projection claims a row outside the repository.
        self.project, self.request, self.runs, self.own_runs = project, request, runs, own_runs
        self.gaps, self.abandoned = gaps, abandoned
        # What the records say about a row: the query's scope, this repository's owed-report holds by Dispatch, and
        # the shared readers of a Run's foreign owners and of whether a record naming a Run reads whole.
        self.scope, self.owed, self.owners, self.unreadable = scope, owed, owners, unreadable
        # The placements this repository's records bind to their Dispatches, complete at the record-pass yield and
        # never rewritten: the projection recovers a row's branch from them first (pod#243). None knows none, which
        # keeps every such row.
        self.placements = placements or {}
        # Every record the record clauses read for this query, by name: its recheck dependencies (R207), and the
        # records whose evidence closure a foreign-owner judgment they used consulted: its census dependencies (R213).
        self.dependencies: dict = {}  # record name -> (directory, generation)
        self.phases: set = set()
        self.candidates: dict = {}  # Dispatch -> {"host", "terminal", "run", "worktree"}
        self.decisions: dict = {}  # Dispatch -> {"counted", "code", "entry", "verdict"}


def _host_exit_host(source: dict, shown: dict, handles: set, abandoned: set) -> str | None:
    """The local execution-host id of a Dispatch R212 may ask about, or None. Settled and not abandoned, liveness
    neither live nor exited, its agent terminal absent from the complete terminal inventory, and every identity in the
    request alphabet. A remote, foreign, missing or malformed host is never asked (F4)."""
    if ((source.get("workerState"), source.get("dispatchStatus")) not in _SETTLED_WORKERS
            or source.get("workerState") == "abandoned" or shown["dispatch"] in abandoned
            or shown["liveness"] in ("live", "exited")
            or not process_exit.eligible_identity(shown["dispatch"])
            or not process_exit.eligible_identity(shown["terminal"]) or shown["terminal"] in handles):
        return None
    host = (source.get("projection") or {}).get("host")
    if not isinstance(host, dict) or host.get("kind") != "local" or not process_exit.eligible_identity(host.get("id")):
        return None
    return host["id"]


def _host_exit_scope(slot: _HostExitSlot, shown: dict, places) -> bool:
    """Rule S's own scope: a row this query would otherwise hold or read a record for."""
    path = shown["worktree"]
    return (path is None or shown["run"] in slot.runs or shown["dispatch"] in slot.gaps
            or _workspace(slot.project, path, places)[1])


def _host_exit_relevant(slot: _HostExitSlot, shown: dict, places) -> bool:
    """The relevance superset S(r, q): it contains every row the projection holds for this query, and every row whose
    counting can expose a record-side hold relevant to it. The native judgment comes first; only a row it leaves out
    is judged by the records, whose every read becomes this query's recheck dependency."""
    return _host_exit_native(slot, shown, places) or _host_exit_record_held(slot, shown, places)


def _host_exit_native(slot: _HostExitSlot, shown: dict, places) -> bool:
    """S's judgment from the row's native location and the record pass's own placements, before any other record is
    read for it.

    A record can give an unregistered row a branch and a repository key, which only narrow `_relevant` or match a
    requested branch; it never makes a path overlap. So a worktree-only request decides any located row by its path,
    and a row in this repository's registered worktree has a complete location that is judged exactly. So does a row
    outside the registry, such as one whose worktree was removed, when an admission of this repository's records binds
    its exact native identity: the projection takes that admission's placement first (`_recovered`), so its writer
    claim and process-liveness unknown carry exactly this location (pod#243). Any other such row keeps the
    conservative answer, since a record read later may still give it a branch."""
    request = slot.request
    if not any(request.values()) or shown["dispatch"] in slot.gaps:
        return True
    path = shown["worktree"]
    if path is None:
        return True  # Unlocated: `_relevant` keeps it for every request.
    registered, inside = _workspace(slot.project, path, places)
    if not inside and shown["run"] not in slot.own_runs:
        return False  # The projection makes no claim and no process-liveness unknown for it.
    if request["worktree"] and not request["branch"]:
        return _overlap(request["worktree"], path)
    if registered is None:
        placement = _recovered(shown, slot.placements)
        if placement is None:
            return True  # A branch, target or effects request facing a branch only a later record read may know.
        return _relevant({"repo_key": placement.get("repo_key"), "worktree": path, "branch": placement["branch"],
                          "locations_complete": True}, request, slot.scope["repo_key"], _overlap, "process_liveness")
    return bool(request["worktree"] and _overlap(request["worktree"], path)
                or request["branch"] and places.get(registered) == request["branch"])


def _host_exit_record_held(slot: _HostExitSlot, shown: dict, places) -> bool:
    """A row the native judgment leaves out that a record can still hold: counting it can expose an owed-report hold
    or an unreadable owner (`_host_exit_recorded`), or, outside the repository on another Run, a record naming its Run
    cannot be read whole (`_host_exit_unread`). Counting a row lets an owed-report hold of its admission stand in for
    its native writer, so such a row is judged by the placement its record names."""
    if _host_exit_recorded(slot, shown):
        return True
    path = shown["worktree"]
    if path is None:
        return False
    _, inside = _workspace(slot.project, path, places)
    return not inside and shown["run"] not in slot.own_runs and _host_exit_unread(slot, shown)


def _host_exit_placement_relevant(slot: _HostExitSlot, path) -> bool:
    """Whether the projection's `worker_placement_unknown` for a row at `path` is relevant to this query."""
    return _relevant({**slot.scope, "worktree": path}, slot.request, slot.scope["repo_key"], _overlap,
                     "worker_placement")


def _host_exit_recorded(slot: _HostExitSlot, shown: dict) -> bool:
    """Whether counting the row can expose a relevant record-side hold (R185): an admission binding its Dispatch that
    still owes its report, in this repository's records or in its Run's records of another repository, judged by the
    placement that admission recorded; or such an owner record that cannot be read, judged as the projection's unknown
    for it. Anything that cannot be judged keeps the row."""
    request, key = slot.request, slot.scope["repo_key"]
    try:
        if any(_relevant(held, request, key, _overlap) for held in slot.owed.get(shown["dispatch"], ())):
            return True  # this repository's records, which the query's own change recheck already covers
        owners = slot.owners(shown["dispatch"], shown["run"])
        slot.phases.update(getattr(owners, "closures", ()))
        for directory, generation, _ in owners:
            if generation is not None:
                slot.dependencies.setdefault(directory.name, (directory, generation))
        for _, _, outcome in owners:
            if (isinstance(outcome, PodError) and _host_exit_placement_relevant(slot, shown["worktree"])
                    or isinstance(outcome, tuple) and _relevant(outcome[0], request, key, _overlap)):
                return True
        return False
    except (PodError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return True


def _host_exit_unread(slot: _HostExitSlot, shown: dict) -> bool:
    """A row outside the repository on another Run: a candidate only when a record naming its Run cannot be read whole
    and the projection's unknown for the row is relevant. Every record read is a recheck dependency."""
    try:
        unreadable, reads = slot.unreadable(shown["run"])
        for directory, generation in reads:
            slot.dependencies.setdefault(directory.name, (directory, generation))
        return unreadable and _host_exit_placement_relevant(slot, shown["worktree"])
    except (PodError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return True


def _host_exit_requests(ids, hosts: dict) -> list:
    """Requests of at most K ids, grouped by host and sorted."""
    by_host = {}
    for dispatch in sorted(ids):
        by_host.setdefault(hosts[dispatch], []).append(dispatch)
    return [(host, group[index:index + process_exit.K]) for host, group in sorted(by_host.items())
            for index in range(0, len(group), process_exit.K)]


def _host_exit_round(reask: list, unsent: list, first: bool) -> list:
    """The next round's plan indexes: the first request alone (pod#219), then at most IN_FLIGHT (pod#227), taking the
    pending re-asks (pod#275) and then the unsent requests. Every re-ask comes from a request already sent, so this is
    plan order."""
    return (reask + unsent)[:1 if first else process_exit.IN_FLIGHT]


def _host_exit_prefetch(discovery, budget, project: Path) -> None:
    """R212: ask the host verifier, before the end fence, about each query's own candidates; decide per query.

    Runs last in the prefetch, after every other native read a result uses, so only local projection work, the
    recheck and a batch's end reads follow it. Unconfigured, nothing is asked and every result is as before."""
    slots = discovery.host_exit_slots
    fleet, terminals = discovery.done(("fleet",)), discovery.done(("terminals",))
    if (not slots or not isinstance(fleet, dict) or fleet.get("complete") is not True
            or not isinstance(fleet.get("workers"), list) or not isinstance(terminals, dict)
            or terminals.get("complete") is not True or terminals.get("runtime") != fleet.get("runtime")):
        return
    runtime = fleet["runtime"]
    handles = {row.get("handle") for row in terminals["terminals"] if isinstance(row, dict)}
    eligible = []  # every query's rows R212 may ask about, before any relevance judgment
    for slot in slots:
        try:
            places = _shared(("places", str(slot.project)), lambda slot=slot: _places(slot.project))
        except (PodError, OSError):
            continue  # The query raises the same fault where it reads its places.
        for source in fleet["workers"]:
            if not isinstance(source, dict) or source.get("workerState") == "unsupervised":
                continue
            try:
                shown = _worker_fact(source)
                host = _host_exit_host(source, shown, handles, slot.abandoned)
                if host is not None and _host_exit_scope(slot, shown, places):
                    eligible.append((slot, places, source, shown, host))
            except (PodError, OSError, ValueError):
                continue  # The projection meets the same fact and keeps its hold.
    if not eligible:
        return
    try:
        from .config import effective
        preferences = effective(project)["preferences"]
    except (PodError, OSError, ValueError):
        preferences = None
    # Only readable preferences can say no verifier is configured: a missing file, or a valid or kept earlier (v1)
    # file without one. An invalid or unreadable file holds every candidate as unavailable.
    if not isinstance(preferences, dict) or preferences.get("status") not in ("valid", "missing", "setup_required"):
        command = False
    else:
        command = preferences.get("authorization_verifier")
    if command is None:
        return  # Unconfigured: today's behaviour; no candidate is judged, no record read, nothing disclosed or asked.
    for slot, places, source, shown, host in eligible:
        try:
            if (not _host_exit_relevant(slot, shown, places)
                    or _shared(("settled-gone", shown["dispatch"]), lambda: _settled_gone(
                        source, shown, fleet["workers"], terminals, lambda run: _coordinator(run, terminals, budget)))):
                continue
        except (PodError, OSError, ValueError):
            continue  # The projection meets the same fact and keeps its hold.
        slot.candidates[shown["dispatch"]] = {"host": host, "terminal": shown["terminal"], "run": shown["run"],
                                              "worktree": shown["worktree"]}
    asking = [slot for slot in slots if slot.candidates]
    if not asking:
        return
    hosts = {dispatch: facts["host"] for slot in asking for dispatch, facts in slot.candidates.items()}
    union = set(hosts)
    if len(_host_exit_requests(union, hosts)) <= process_exit.R:
        asked = union
    else:
        # Over the bound: whole query sets, smallest first, then by their sorted ids; never by query order.
        asked = set()
        for wanted in sorted({frozenset(slot.candidates) for slot in asking}, key=lambda ids: (len(ids), sorted(ids))):
            if len(_host_exit_requests(asked | wanted, hosts)) <= process_exit.R:
                asked |= wanted
    plan = _host_exit_requests(asked, hosts) if command else []
    answers = {}
    stopped = None  # the code every candidate not asked holds with: the stopping round's first transport or contract
    # fault, or `host_exit_unverified` once UNVERIFIED_STOP unverified answers stand with none verified (pod#275)
    if not command:
        answers = {dispatch: (process_exit.UNAVAILABLE, None, None) for dispatch in asked}
    elif plan:
        try:
            # A single query reads rule S's Run list lazily; read it now so only local work follows the requests.
            _shared(("run-coordinators", terminals["runtime"]),
                    lambda: _run_coordinators(terminals["runtime"], budget.check))
        except (PodError, OSError):
            pass  # The projection meets the same outcome and keeps its gap.
        stamps = [discovery.finished_at.get(name) for name in ("fleet", "terminals")]
        native = (max(stamps) - discovery.started) if None not in stamps and discovery.started is not None else 0.0
        reserve = max(process_exit.RESERVE_S, process_exit.RESERVE_SCALE * native)
        sent, unsent, reask, reasked = [], list(range(len(plan))), [], set()
        unverified, verified = 0, False  # unverified answers that stand (after their one re-ask), any verified answer
        # The first request in plan order runs alone; the rest follow in rounds of at most IN_FLIGHT, each starting
        # only after every request of the previous round answered, under the same deadline test, and only while no
        # request has met a transport or contract fault (pod#219, pod#227) and UNVERIFIED_STOP unverified answers have
        # not stood without one verified answer (pod#275). A request answering unverified is asked once more, in plan
        # order in a later round, only while every unsent request still fits R beside it, so a re-ask never displaces a
        # planned request and the call never exceeds R; its answer replaces the first. A stop ends the call's requests
        # after its own round, whose other requests were already in flight and keep their own outcomes; a re-ask not
        # yet sent keeps its first answer.
        while unsent or reask:
            if budget.remaining() < process_exit.TIMEOUT_S + reserve:
                answers.update({dispatch: (process_exit.DEADLINE, None, None) for index in unsent
                                for dispatch in plan[index][1]})
                break
            group = _host_exit_round(reask, unsent, not sent)
            reask = [index for index in reask if index not in group]
            unsent = [index for index in unsent if index not in group]
            futures = [discovery.submit(("host-exit", len(sent) + offset),
                                        lambda ids=plan[index][1]: process_exit.ask(command, ids))
                       for offset, index in enumerate(group)]
            sent.extend(group)
            for index, future in zip(group, futures):
                host, ids = plan[index]
                try:
                    answer = future.result()
                    code = process_exit.judge(answer, host=host, runtime=runtime)
                except (PodError, OSError):
                    answer, code = {"verdict": None}, process_exit.FAILED
                verdict = answer["verdict"]
                entries = dict(zip(ids, verdict["dispatches"])) if code is None else {}
                facts = ({key: verdict[key] for key in process_exit.FACT_FIELDS} if code is None else None)
                for dispatch in ids:
                    answers[dispatch] = (code, entries.get(dispatch), facts)
                if stopped is None and code in process_exit.STOPPING:
                    stopped = code  # the first in plan order
                if code == process_exit.UNVERIFIED:
                    if index not in reasked and len(sent) + len(reask) + len(unsent) < process_exit.R:
                        reask = sorted({*reask, index})
                        reasked.add(index)
                    else:
                        unverified += 1
                elif code not in process_exit.STOPPING:
                    verified = True  # a `verified: true` answer, usable or not, keeps every later round
            if stopped is None and not verified and unverified >= process_exit.UNVERIFIED_STOP:
                stopped = process_exit.UNVERIFIED
            if stopped is not None:
                break
        discovery.host_exit_summary.update(requests=len(sent), asked=sum(len(plan[index][1]) for index in set(sent)))
        if len(sent) > len(set(sent)):  # re-asks sent, present only when any was, so the summary shape stays as before
            discovery.host_exit_summary["reasked"] = len(sent) - len(set(sent))
    counted, not_asked = set(), set()
    for slot in asking:
        covered = all(dispatch in answers and answers[dispatch][0] is None for dispatch in slot.candidates)
        # After a stop, in any round, a candidate not asked holds with the stopping code, as its single query meets a
        # fault or unverified answers that do not depend on the ids on its own first requests; only a set too large to
        # be asked alone keeps `host_exit_call_limit`, as its single query.
        unasked = (stopped if stopped is not None
                   and len(_host_exit_requests(slot.candidates, hosts)) <= process_exit.R else process_exit.CALL_LIMIT)
        for dispatch, facts in slot.candidates.items():
            code, entry, verdict = answers.get(dispatch, (unasked, None, None))
            if dispatch not in answers and code == stopped:
                not_asked.add(dispatch)
            if code is None:
                code = process_exit.entry_code(entry, facts["terminal"])
                if code is None and not covered:
                    code = process_exit.UNCOVERED
            slot.decisions[dispatch] = {"counted": code is None, "code": code, "entry": entry, "verdict": verdict}
            if code is None:
                counted.add(dispatch)
    discovery.host_exit_counted = tuple(sorted(counted))
    discovery.host_exit_summary["counted"] = len(counted)
    if stopped is not None:  # the call's own record of why candidates were not asked; never a per-query field
        discovery.host_exit_summary.update(not_asked_after=stopped, not_asked=len(not_asked))


def _host_exit_rows(slot: _HostExitSlot, used: set) -> list:
    """This query's own disclosure: every candidate it asked about, whether it counted, and the host's attribution."""
    rows = []
    for dispatch in sorted(slot.decisions):
        decision, facts = slot.decisions[dispatch], slot.candidates[dispatch]
        entry, verdict = decision["entry"] or {}, decision["verdict"] or {}
        rows.append({"dispatch": dispatch, "run": facts["run"], "worktree": facts["worktree"],
                     "counted": dispatch in used, "code": decision["code"],
                     **{key: verdict.get(key) for key in process_exit.FACT_FIELDS},
                     "state": entry.get("state"), "checked": entry.get("checked")})
    return rows


def _host_exit_identity(row: dict) -> dict:
    """A counted Dispatch's fence identity (R212): the claim identity plus every native field its exit depends on."""
    def part(value, *names):
        value = value if isinstance(value, dict) else {}
        return {name: value.get(name) for name in names}
    identity = _identity_row(row)
    projection = row.get("projection") if isinstance(row.get("projection"), dict) else {}
    identity["projection"].update(liveness=part(projection.get("liveness"), "verdict", "reason", "observedAt"),
                                  evidence=part(projection.get("evidence"), "lastObservedAt", "liveStatus"),
                                  host=part(projection.get("host"), "kind", "id"))
    identity["resource"].update(part(row.get("resource"), "endpointId", "endpointIncarnation"))
    return identity


def _placement(row: dict) -> dict:
    recovery = row.get("recovery")
    if recovery is not None and not isinstance(recovery, dict):
        raise PodError("admission_placement_invalid", "Accepted recovery placement facts are malformed")
    placement = row.get("placement_binding")
    if placement is None:
        placement = (recovery or {}).get("placement_binding")
    if placement is not None and not isinstance(placement, dict):
        raise PodError("admission_placement_invalid", "Accepted placement binding is malformed")
    if placement:
        path, branch = placement.get("path"), placement.get("branch")
        if (path is not None and (not isinstance(path, str) or len(path) > 4096 or not _passable(path) or not Path(path).is_absolute())
                or branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 256 or not _passable(branch))):
            raise PodError("admission_placement_invalid", "Accepted placement location fields are malformed")
        key = placement.get("repo_key")
        if key is not None and (not isinstance(key, str) or not history.HEX.fullmatch(key)):
            raise PodError("admission_placement_invalid", "Accepted placement repository identity is malformed")
    return placement or {}


def _membership(project: Path, context: dict, record: str, raw: dict, known=()) -> str | None:
    """A matching key proves membership; exclusion needs a second attributed key.

    A mismatched name alone could be corruption of this repository's record.
    Current worktree attribution is written in the same atomic context by the
    existing checkpoint writer. Unattributed records require normalization.
    """
    from .installer import STATE_SCHEMAS
    from .records import worktree_binding
    if raw.get("schema") != STATE_SCHEMAS["context"] or not isinstance(raw.get("checkpoint"), dict):
        raise PodError("state_unsupported", "Required attribution header format is unsupported")
    name = listing._name(raw)
    if name is None or len(name) > 256 or "\x00" in name:
        raise PodError("unattributed", "Objective identity is unavailable")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise PodError("unattributed", "Objective identity is not UTF-8") from None
    attribution = raw.get("_facts_attribution")
    if attribution is not None:
        if (not isinstance(attribution, dict) or attribution.get("status") != "proved"
                or attribution.get("objective") != name or not isinstance(attribution.get("repo_key"), str)
                or not history.HEX.fullmatch(attribution["repo_key"])
                or record_name(project, attribution, name) != record):
            raise PodError("record_attribution_unknown", "Normalized attribution does not bind the record key")
        return name if attribution["repo_key"] == context["repo_key"] else None
    place = (raw.get("checkpoint") or {}).get("worktree") if isinstance(raw.get("checkpoint"), dict) else None
    if record_name(project, context, name) == record:
        if isinstance(place, dict) and place.get("repo_key") != context["repo_key"]:
            raise PodError("record_identity_mismatch", "Worktree attribution differs from the record key")
        return name
    if (raw.get("schema") == STATE_SCHEMAS["context"] and isinstance(place, dict)
            and raw["checkpoint"].get("schema") == STATE_SCHEMAS["checkpoint"]):
        worktree_binding(place)
        if record_name(project, {"repo_key": place["repo_key"]}, name) == record:
            return None
    for source in known:
        if record_name(Path(source["worktree"]), source, name) == record:
            return name if source["repo_key"] == context["repo_key"] else None
    # Earlier records can retain attribution in their accepted placement facts
    # even when the coordinator worktree was not recorded. Read no history here.
    admissions = raw.get("admissions") or {}
    if not isinstance(admissions, dict):
        raise PodError("state_unsupported", "Admission attribution fields are malformed")
    for row in admissions.values():
        if not isinstance(row, dict):
            continue
        placement = _placement(row)
        if isinstance(placement, dict) and isinstance(placement.get("repo_key"), str):
            if record_name(project, placement, name) == record:
                return name if placement["repo_key"] == context["repo_key"] else None
    raise PodError("record_attribution_unknown", "Record cannot be attributed or safely excluded")


def _admissions(raw: dict, directory: Path, *, placement_unknown: bool = False) -> dict:
    """A record's validated admission inventory. `placement_unknown` (rule T only) also accepts an inventory whose
    one missing fact is the record's own placement."""
    if "_facts_generation" not in raw or _SHARED.get() is None:
        return _admission_rows(raw, directory, placement_unknown=placement_unknown)
    def load():
        working = dict(raw)
        return working, _admission_rows(working, directory, placement_unknown=placement_unknown)
    # One validation and archive expansion per record generation and call; each query gets its own copy.
    current, rows = _shared(("admissions", str(directory), digest(raw["_facts_generation"]), placement_unknown), load)
    raw.clear()
    raw.update(current)
    return dict(rows)


def _attributed(raw: dict) -> dict:
    """The facts a record pass takes from a record's attribution read: all but the bytes' generation and the proof
    basis of its attribution, which admission never reads (R207). Everything else is compared, so a fact a later
    reader adds to the attribution read is compared too."""
    view = {key: value for key, value in raw.items() if key != "_facts_generation"}
    if isinstance(view.get("_facts_attribution"), dict):
        view["_facts_attribution"] = {key: value for key, value in view["_facts_attribution"].items() if key != "basis"}
    return view


def _admission_rows(raw: dict, directory: Path, *, placement_unknown: bool = False) -> dict:
    from .installer import STATE_SCHEMAS
    from .ledger import _CONTEXT_FIELDS, _SUCCESSION_FIELDS
    projected = "_facts_generation" in raw
    if projected:
        path = directory / "context.json"
        facts = _shared(("facts", str(path), True), lambda: history.scan_read(
            path, lambda: history.load_admission_facts(path, include_admissions=True)))
        current = _facts_context(facts)
        closed_empty = (facts.get("phase") == "closed" and facts.get("admissions") == {}
                        and facts.get("errors") == [{"code": "objective_placement_unknown"}])
        unplaced = placement_unknown and facts.get("errors") == [{"code": "objective_placement_unknown"}]
        if (facts.get("complete") is not True and not closed_empty and not unplaced) or not isinstance(facts.get("admissions"), dict):
            raise PodError("admission_facts_incomplete", "Current claim inventory is incomplete")
        # Other bytes may still carry every fact the attribution read gave (a re-encoding). Then the attribution and
        # these claims are one read of the newer bytes, and the change recheck compares the record with them, so any
        # later change, a return to the attribution read's bytes included, is found (R207). Only a shared discovery
        # keeps the claims read for that recheck.
        if current["_facts_generation"] != raw["_facts_generation"]:
            attributed, claimed = _attributed(raw), _attributed(current)
            if _SHARED.get() is None or any(key not in claimed or claimed[key] != value for key, value in attributed.items()):
                raise PodError("record_changed", "Attribution changed before current claims were read")
        raw.clear()
        raw.update(current)
        raw["admissions"] = facts["admissions"]
    if not projected and (set(raw) - (_CONTEXT_FIELDS | _SUCCESSION_FIELDS | {"historical_retirement", "decision_channels",
            "decision_order", "pod_ticks", "standing_authorization", "history"})
            or not _CONTEXT_FIELDS <= set(raw) or type(raw.get("revision")) is not int or raw["revision"] < 0):
        raise PodError("state_unsupported", "Context claim representation is unsupported")
    if raw.get("schema") != STATE_SCHEMAS["context"]:
        raise PodError("state_unsupported", "Context schema is unsupported")
    checkpoint = raw.get("checkpoint")
    earlier_claims = isinstance(checkpoint, dict) and checkpoint.get("schema") == "pod-checkpoint/v2"
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") not in (STATE_SCHEMAS["checkpoint"], "pod-checkpoint/v2"):
        raise PodError("state_unsupported", "Checkpoint schema is unsupported")
    if checkpoint.get("worktree") is not None:
        from .records import worktree_binding
        worktree_binding(checkpoint["worktree"])
    admissions = raw.get("admissions")
    if not isinstance(admissions, dict):
        raise PodError("state_unsupported", "Admissions are unavailable")
    metadata = raw.get("history")
    if "history" in raw and (not isinstance(metadata, dict) or metadata.get("schema") != history.FORMAT["schema"]):
        raise PodError("history_invalid", "History format is unsupported")
    rows = {}
    for key, row in admissions.items():
        if history.is_reference(row):
            if (raw.get("history") or {}).get("schema") != history.FORMAT["schema"]:
                raise PodError("history_invalid", "History format is unsupported")
            # A settled attempt can retain a live resource. Read its binding, never
            # infer absence from archival, a report, or the objective's closed label.
            row = _claim_reference(row, directory)
        if (not isinstance(row, dict) or row.get("schema") not in
                ((STATE_SCHEMAS["admission"], "pod-admission/v3") if earlier_claims else (STATE_SCHEMAS["admission"],))
                or row.get("state") not in ("reserved", "bound", "unresolved", "closed", "deferred")
                or row.get("role") not in (("implement", "review", "investigate", None) if earlier_claims else ("implement", "review", "investigate"))
                or row.get("admission_id") != key):
            raise PodError("state_unsupported", "Admission claim is malformed")
        if row.get("native_binding") is not None and not isinstance(row.get("runtime"), str):
            raise PodError("state_unsupported", "Native claim runtime is unavailable")
        if row.get("native_binding") is not None and not binding_valid(row["native_binding"]):
            raise PodError("admission_binding_invalid", "Native claim binding is malformed")
        _placement(row)
        rows[key] = row
    if projected:
        return rows  # The versioned reader validates phase without loading closure.report.
    closure = checkpoint.get("closure")
    if closure is not None:
        from .obligations import _closure_record
        _closure_record(closure)
    if "continued_by" in raw:
        from .succession import validate_link
        validate_link("continued_by", raw["continued_by"])
    if "historical_retirement" in raw:
        from .retirement import validate_entry
        retirement = raw["historical_retirement"]
        if history.is_reference(retirement):
            retirement = _claim_reference(retirement, directory)
        validate_entry(retirement)
    return rows


def _journal(path: Path) -> tuple[dict, str]:
    # Read once per call, classified by the journal bytes actually read: a journal another writer republishes and
    # reclaims during the read is read again, and one that keeps changing is never unreadable (R206).
    journal, identity, _ = _shared(("journal", str(path)), lambda: history.scan_read(path, lambda: _journal_read(path)))
    return journal, identity


def _journal_read(path: Path) -> tuple[dict, str, dict | None]:
    from . import governor
    if path.is_symlink():
        raise PodError("unsafe_state", "Governor journal is redirected")
    reader = getattr(governor, "load_admission_effects", None)
    if reader is not None:
        facts = reader(path)
        if (not isinstance(facts, dict) or facts.get("schema") != "pod-admission-effects/v1"
                or facts.get("complete") is not True or not isinstance(facts.get("units"), dict)
                or not isinstance(facts.get("actions"), list)):
            raise PodError("admission_effects_incomplete", "Current effect inventory is unavailable")
        journal = {"units": facts["units"], "actions": facts["actions"]}
        _validate_effect_facts(journal)
        generation = _generation(facts.get("generation"))
        return journal, digest(generation), generation
    journal = governor._read_journal(path)
    _validate_effect_facts(journal)
    try:
        return journal, digest(journal), None
    except UnicodeError:
        raise PodError("invalid_record", "Journal identities must be UTF-8") from None


_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _after_context(path: Path) -> str:
    """The recheck identity of a record's context. Bytes equal to the discovery read's keep its identity
    without a decode; anything else is read again in full, in the recheck stage. Judged once per call."""
    return _shared(("after-context", str(path)), lambda: _after_context_read(path))


def _after_context_read(path: Path) -> str:
    discovery = _SHARED.get()
    facts = discovery.done(("facts", str(path), False)) if discovery is not None else None
    claims = discovery.done(("facts", str(path), True)) if discovery is not None else None
    if (isinstance(facts, dict) and isinstance(facts.get("generation"), dict) and isinstance(claims, dict)
            and isinstance(claims.get("generation"), dict) and claims["generation"] != facts["generation"]):
        # The claims came from later bytes than the attribution (R207): the record is unchanged only while it still
        # holds those bytes. Any other bytes, the attribution read's included, are a change, never its identity.
        data = _shared(("after-bytes", str(path)), lambda: history._context_bytes(path, history.MAINTENANCE_LIMIT))
        if hashlib.sha256(data).hexdigest() == claims["generation"].get("sha256"):
            return digest(facts["generation"])
        return "changed:" + _primary(path)[1]
    if isinstance(facts, dict) and isinstance(facts.get("generation"), dict):
        data = _shared(("after-bytes", str(path)), lambda: history._context_bytes(path, history.MAINTENANCE_LIMIT))
        if hashlib.sha256(data).hexdigest() == facts["generation"].get("sha256"):
            return digest(facts["generation"])
    return _primary(path)[1]


def _after_journal(path: Path) -> str:
    """The recheck identity of a Governor journal, compared the same way before any decode; judged once per call."""
    return _shared(("after-journal", str(path)), lambda: _after_journal_read(path))


def _after_journal_read(path: Path) -> str:
    from .ledger import _record_exists
    discovery = _SHARED.get()
    first = discovery.done(("journal", str(path))) if discovery is not None else None
    generation = first[2] if first is not None else None
    if isinstance(generation, dict):
        if path.is_symlink():
            raise PodError("unsafe_state", "Governor journal is redirected")
        if not _record_exists(path):
            if generation == {"revision": 0, "sha256": _EMPTY_SHA}:
                return first[1]
        else:
            data = _shared(("after-bytes", str(path)), lambda: history._context_bytes(path, history.MAINTENANCE_LIMIT))
            sha = hashlib.sha256(data).hexdigest()
            if sha != _EMPTY_SHA and sha == generation.get("sha256"):
                return first[1]
    return _journal(path)[1]


def _validate_effect_facts(journal: dict) -> None:
    from .config import validate_effects
    from .governor import KINDS, DECISIONS, OUTCOMES, _validate_branch
    for unit in journal["units"].values():
        if not isinstance(unit, dict):
            raise PodError("admission_effects_incomplete", "Unit branch facts are malformed")
        _validate_branch(unit.get("branch"))
    for row in journal["actions"]:
        action = row.get("action") if isinstance(row, dict) else None
        if (not isinstance(action, dict) or action.get("kind") not in KINDS
                or row.get("decision") not in DECISIONS or row.get("outcome") not in OUTCOMES
                or any(not isinstance(action.get(key), str) or not action[key] for key in ("candidate", "target", "unit"))
                or not isinstance(row.get("record_id"), str) or not row["record_id"]):
            raise PodError("admission_effects_incomplete", "Current action facts are malformed")
        if action.get("effects") is not None:
            validate_effects(action["effects"])
        recorded = row.get("effects")
        if recorded is not None and (not isinstance(recorded, list)
                or any(not isinstance(effect, str) or not effect for effect in recorded)):
            raise PodError("admission_effects_incomplete", "Admitted Governor effect identities are malformed")


def _action_effects(row: dict, project: Path | None, policies: dict) -> tuple[list[str] | None, str]:
    """Use the Governor's identities and preserve its durable admitted mapping."""
    from .governor import _effects
    recorded = row.get("effects") or []
    effects, source = _effects(row["action"], {})
    if effects is None and not recorded and project is not None:
        from .config import effective
        key = str(project)
        if key not in policies:
            policies[key] = _shared(("effective", key), lambda: effective(project))
        effects, source = _effects(row["action"], policies[key]["policy"]["waste_governor"])
    if effects is None and not recorded:
        return None, "unknown"
    return sorted(set(effects or []) | set(recorded)), source if effects is not None else "recorded"


def _effect_scopes(row: dict, identities: list[str] | None) -> tuple[list[str] | None, list[str]]:
    """Attach repository context to intrinsic names; declared/mapped effects stay global."""
    from .governor import _effects
    if identities is None:
        return None, []
    action = row["action"]
    intrinsic, _ = _effects({**action, "effects": None}, {})
    prefixes = {value.split(":", 1)[0] + ":" for value in intrinsic or []}
    prefixes -= {"deploy:", "release:"}
    if action["kind"] in ("push", "pr_update"):
        prefixes.add(action["kind"] + ":")
    declared = set(action.get("effects") or [])
    relative = [value for value in identities if value not in declared and value.startswith(tuple(prefixes))]
    return [value for value in identities if value not in relative], relative


def _effect_match(facts: dict, requested: list[str], repo_key: str | None) -> bool:
    shared = facts.get("global_effects", facts.get("effects")) or []
    relative = facts.get("repository_effects") or []
    return bool(set(shared) & set(requested) or
                facts.get("repo_key") in (None, repo_key) and set(relative) & set(requested))


def _relevant(facts: dict, request: dict, repo_key: str | None, overlap=None, fact=None) -> bool:
    """Intersect factual scopes, without deciding whether CE may perform an action."""
    if not any(request.values()):
        return True
    if "effects" in facts:
        identities = facts["effects"]
        return bool(request["effects"] and (identities is None or _effect_match(facts, request["effects"], repo_key))
                    or request["target"] and facts.get("repo_key") is None and facts.get("repository_effects")
                    or request["target"] and facts.get("target") == request["target"]
                       and facts.get("repo_key", repo_key) == repo_key)
    owners = facts.get("repo_keys") or ([facts["repo_key"]] if facts.get("repo_key") else [])
    if owners and repo_key not in owners:
        return False
    location_fact = facts.get("kind") in ("writer", "ownership", "assignment") or fact in ("process_liveness", "terminal_liveness")
    if location_fact and not request["worktree"] and not request["branch"] and facts.get("locations_complete") is True and not facts.get("native_effects"):
        return False
    if request["worktree"] or request["branch"]:
        paths = facts.get("worktrees", [facts.get("worktree")])
        branches = facts.get("branches", [facts.get("branch")])
        if (request["worktree"] and any(path and (overlap(request["worktree"], path) if overlap else request["worktree"] == path) for path in paths)
                or request["branch"] and request["branch"] in branches):
            return True
        if facts.get("locations_complete") is True:
            return False
        if request["worktree"] and not request["branch"] and any(paths):
            return False
    return True


def _within(inner: str, outer: str) -> bool:
    """`Path(inner).is_relative_to(outer)` for two resolved absolute paths, compared as text."""
    if inner.startswith("//") or outer.startswith("//"):
        return Path(inner).is_relative_to(outer)  # POSIX keeps a leading double slash distinct.
    return inner == outer or inner.startswith(outer if outer.endswith("/") else outer + "/")


def _owed_hold(objective: str | None, record: str, key: str, row: dict) -> tuple[dict, str | None]:
    """A bound admission's owed-report hold: the record-side claim an unfinished unbound admission makes, with the
    same keys (R185), and the repository key its accepted placement records. Its locations are complete only with
    both worktree and branch recorded."""
    placement = _placement(row)
    binding = row.get("native_binding") or {}
    location = (listing._location(binding.get("worktreeId"))
                or listing._location(row.get("worktree")) or placement.get("path"))
    return ({"objective": objective, "record": record,
             "kind": "writer" if row.get("role") == "implement" else "assignment",
             "admission": key, "worktree": location, "branch": placement.get("branch"),
             "locations_complete": bool(location) and bool(placement.get("branch")),
             "liveness": "exited", "boundary": row.get("boundary")}, placement.get("repo_key"))


def _record_locations(place: dict, rows: dict) -> dict:
    """The worktrees and branches a record's coordinator place and admissions name, and whether all are recorded."""
    placements = [_placement(row) for row in rows.values()]
    paths = [place.get("path"), *(listing._location((row.get("native_binding") or {}).get("worktreeId"))
              or listing._location(row.get("worktree")) or placement.get("path")
              for row, placement in zip(rows.values(), placements))]
    return {"worktrees": sorted({path for path in paths if path}),
            "branches": [place.get("branch"), *(placement.get("branch") for placement in placements)],
            "locations_complete": bool(paths) and all(paths)}


def _placement_matches(binding: dict, shown: dict) -> bool:
    return (binding_valid(binding) and all(binding.get(key) == shown.get(field) for key, field in
        (("dispatchId", "dispatch"), ("runId", "run"), ("taskId", "task"),
         ("workerId", "worker"), ("terminalHandle", "terminal")))
        and listing._location(binding["worktreeId"]) == shown["worktree"])


def _recovered(shown: dict, *tables) -> dict | None:
    """The placement that gives a row outside the worktree registry its branch (pod#243): the first table, in the
    order given, whose entry for its Dispatch is an admission binding that exact native identity. The record pass's
    table comes first and is never rewritten afterwards, so a row it recovers keeps that placement whatever order
    the worker loop reads other rows or other records in, and R212's superset can judge the row by it in advance."""
    for table in tables:
        binding, placement = table.get(shown["dispatch"], ({}, {}))
        if _placement_matches(binding, shown):
            return placement
    return None


def _canonical_target(project: Path | None, ref: str | None) -> str | None:
    if project is None or ref is None:
        return ref
    def read():
        observed = gitio.read(project, ["rev-parse", "--symbolic-full-name", "--verify", "--quiet", "--end-of-options", ref])
        value = observed.stdout.strip() if observed is not None and observed.returncode == 0 else ""
        return value if value.startswith("refs/") else ref
    return _shared(("canonical", str(project), ref), read)


def _claim_identity(raw: dict, rows: dict) -> str:
    generation = raw.get("_facts_generation")
    if generation is None or _SHARED.get() is None:
        return _claim_digest(raw, rows)
    # A record's claims are a function of its generation: computed once per generation and call.
    return _shared(("claim-identity", digest(generation)), lambda: _claim_digest(raw, rows))


def _claim_digest(raw: dict, rows: dict) -> str:
    from .governor import report_owed
    checkpoint = raw["checkpoint"]
    phase = _phase(raw)
    return digest({"objective": listing._name(raw), "phase": phase,
                   "worktree": checkpoint.get("worktree") if phase == "open" else None,
                   "runs": checkpoint.get("native_refs"),
                   "admissions": {key: {**{field: row.get(field) for field in
                       ("state", "role", "native_binding", "runtime", "worktree", "request_uuid", "run_id", "task_id", "boundary")},
                       # A report consumed, or an accepted placement changed, between the read and the recheck
                       # changes a hold (R185).
                       "report_owed": report_owed(row), "placement": _placement(row)}
                       for key, row in rows.items()}})


def _terminal_history(gap: dict, project: Path, fleet_facts: dict, unattributed_actor: bool, elsewhere=None) -> bool:
    """Rule T (R206): a terminal record's settled history is no current claim. The caller has proved complete,
    unbound fleet and terminal inventories; this proves the record unchanged, without an unsettled governed effect,
    and every admission settled. Anything unproved keeps the gap."""
    directory = gap["directory"]
    if not _shared(("terminal-validated", str(project), str(directory), gap["phase"]),
                   lambda: _terminal_validated(project, directory, gap["phase"])):
        return False
    if (_after_context(directory / "context.json") != gap["identity"]
            or _after_journal(directory / "governor.json") != gap["journal_id"]):
        return False
    try:
        if _effects_pending(_journal(directory / "governor.json")[0]):
            return False
    except PodError:
        return False
    preserved = None
    for key, row in gap["rows"].items():
        state = row.get("state")
        if state in ("closed", "deferred"):
            continue
        binding = row.get("native_binding")
        if binding and gap["phase"] != "retired":
            from .governor import report_owed
            if report_owed(row) is not None:
                return False  # Its report is still owed: the worker's placement is no settled history.
        if binding:
            # Absent from a complete all-Run inventory, or shown settled: no current Dispatch.
            if fleet_facts.get(binding.get("dispatchId")) in (None, "settled"):
                continue
            if elsewhere is not None and elsewhere(binding.get("dispatchId"), row):
                continue  # 4b: current only in another repository's registered worktree, as the worker loop skips
            return False
        if state not in ("reserved", "unresolved", "bound"):
            return False
        if gap["phase"] == "retired":
            if preserved is None:
                preserved = _retirement_preserved(project, directory) or set()
            if key in preserved:
                continue  # (i) retirement certified no pending start, and the record accepts no later write
        if unattributed_actor:
            return False  # (ii) a current actor in this repository could be this reservation's
    return True


def _terminal_validated(project: Path, directory: Path, phase: str) -> bool:
    """Rule T 1-2: the record's terminal status established from its own whole context through the validators the
    ordinary context path uses, never from the selective facts reader's phase alone. A closed record has a valid
    checkpoint closure or a readable R145 evidence closure; a continued one a valid R110 link; a `retired` one a
    valid R112 entry or a certified R205 entry beside it. Anything unproved is not terminal."""
    from . import retirement
    from .ledger import _read
    try:
        state = _read(directory / "context.json")
    except (PodError, OSError, ValueError):
        state = None
    state = state if isinstance(state, dict) else {}
    try:
        if phase == "continued":
            from .succession import validate_link
            link = state.get("continued_by")
            if history.is_reference(link):
                link = _claim_reference(link, directory)
            validate_link("continued_by", link)
            return isinstance(link, dict)
        if phase == "retired":
            entry = state.get(retirement.FIELD)
            if history.is_reference(entry):
                entry = _claim_reference(entry, directory)
            if isinstance(entry, dict):
                retirement.validate_entry(entry)
                return True
            return retirement.older_retirement(project, directory) == "retired"
        if phase == "closed":
            closure = (state.get("checkpoint") or {}).get("closure") if isinstance(state.get("checkpoint"), dict) else None
            if closure is not None:
                from .obligations import _closure_record
                _closure_record(closure)
                return True
            from .evidence_closure import block
            evidence = block(directory)
            return isinstance(evidence, dict) and evidence.get("status") == "recorded"
    except (PodError, KeyError, TypeError, ValueError, AttributeError):
        return False
    return False


def _effects_pending(journal: dict) -> bool:
    return any(row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN") for row in journal["actions"])


def _effect_identity(journal: dict, *, open_objective: bool) -> str:
    return digest({"actions": [row for row in journal["actions"]
                              if row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")],
                   "branches": {key: unit.get("branch") for key, unit in journal["units"].items()}
                               if open_objective else {}})


def _proof(project: Path, objective: str, candidate: str) -> dict:
    return _proof_view(project, objective, candidate)[0]


def _proof_view(project: Path, objective: str, candidate: str, *, state: dict | None = None,
                native: dict | None = None) -> tuple[dict, dict, dict | None]:
    """The exact-candidate proof, with the record and kernel view it was judged from (None on a candidate
    mismatch). The one proof implementation: `pod admission` and its selected read both return this proof.

    A recording write (R209) passes its own authority-joined `state` and the `native` read it already made, so the
    proof judges that snapshot rather than the bytes on disk; every other caller reads both as before."""
    from .ledger import kernel_view, read
    state = read(project, objective) if state is None else state
    if state is None:
        raise PodError("objective_unknown", "The selected objective has no record")
    checkpoint = state.get("checkpoint") or {}
    if checkpoint.get("candidate") != candidate:
        return {"status": "blocked", "candidate": candidate, "reason": "candidate_mismatch"}, state, None
    # A root checked out at the candidate keeps judging bound sources by its working files, which is what a review
    # binds, including bytes a clean checkout converts (eol, filters). Any other root, such as a canonical checkout on
    # the target branch, judges them against the candidate's stored blobs instead of its own unrelated files (pod#184).
    at_candidate = gitio.resolve_commit(project, "HEAD") == candidate
    view = kernel_view(project, objective, state=state, candidate=candidate, native=native,
                       sources_at=None if at_candidate else candidate)
    from .obligations import _persisted_rows, selected_unverifiable, TERMINAL
    gaps, unavailable = [], []
    if view["map"] is None:
        gaps.append("map_missing")
    else:
        try:
            history = {}
            persisted = _persisted_rows(view["map"], view["ctx"], unverifiable_receipts=history)
            # Only a row's selected evidence judges the candidate. An unselected receipt whose original scope cannot be
            # verified is nonqualifying history: every receipt was still validated above (pod#203).
            unavailable = selected_unverifiable(persisted, history)
            gaps.extend("evidence_unverified:" + key for key in unavailable)
        except PodError as exc:
            gaps.append(exc.code)
    verification_gaps = checkpoint.get("verification_gaps")
    if not isinstance(verification_gaps, list) or any(not isinstance(item, str) for item in verification_gaps):
        raise PodError("invalid_verification_gaps", "Recorded verification_gaps must be a list of strings")
    gaps.extend(verification_gaps)
    if view["ctx"]["outstanding"]:
        gaps.append("outstanding_assignments")
    if view["label"]["label"] != "QUALIFIED":
        gaps.append("assurance_unbound")
    return ({"status": "unknown" if view["settlement"] == "unverified" or unavailable else "blocked" if gaps else "ready",
             "candidate": candidate, "settlement": view["settlement"],
             "purpose": "candidate_proof", "gaps": gaps, "assurance": view["label"],
             "objective_unfinished": [row["id"] for row in (view["map"] or {}).get("obligations", [])
                                      if row["state"] not in TERMINAL],
             "objective_complete": bool(view["map"]) and all(row["state"] in TERMINAL
                                       for row in view["map"]["obligations"])}, state, view)


def _query(project: Path, *, repository: str | None = None, objective: str | None = None,
          candidate: str | None = None, worktree: str | None = None, branch: str | None = None,
          target: str | None = None, target_commit: str | None = None,
          effects: list[str] | None = None, expect: str | None = None, timeout: float = 10.0,
          known_repositories: list[str] | None = None) -> dict:
    """Inventory Pod claims and exact proof, with explicit uncertainty and recheck identity.

    No result is a permission. CE joins its native/authority inventory and repeats
    this read under its existing serialization immediately before the effect.

    One query's projection over its call's shared discovery, as a generator the call drives in four
    steps: it yields ("prepared", root, project) once its inputs are proved, "native" when its record pass
    is done, "recheck" when its first pass is done, and returns its result after the call's one shared
    change recheck.
    """
    if bool(objective) != bool(candidate) or candidate and not gitio.OBJECT_ID.fullmatch(candidate):
        raise PodError("admission_candidate", "Supply an objective and its full candidate object id together")
    if target_commit and (not target or not gitio.OBJECT_ID.fullmatch(target_commit)):
        raise PodError("admission_target", "A target commit needs a target ref and a full object id")
    project = Path(repository or project).expanduser().resolve()
    if not project.is_dir():
        raise PodError("admission_scope_unavailable", "The repository argument is not an existing directory")
    requested_target, target = target, _canonical_target(project, target)
    context = _context(project)
    if context["repo_key"] is None:
        raise PodError("admission_scope_unavailable", "The repository argument has no usable Git identity")
    known = [context, *(_context(_resolved(path)) for path in known_repositories or [])]
    root = _shared(("state-root", str(project)), lambda: state_root(project))  # One repository's root, once a call.
    yield "prepared", root, project
    census = _RecordCensus(root)
    scope = {"repository": context["repository"], "repo_key": context["repo_key"], "path": str(project),
             "inventory": "pod_objectives_and_recorded_runs"}
    result = {"schema": "pod-admission-query/v1", "status": "observed", "scope": scope,
              "complete": True, "claims": [], "conflicts": [], "unknowns": [],
              "native_resources_complete": False,
              "proof": None, "snapshot": None, "target": None,
              "authorization": False,
              "next_action": "Join CE authority/resources and recheck under effect serialization"}
    stamps, sources, runs, run_owners, phases, policies = {}, [], {}, {}, {}, {}
    effect_sources, run_repositories, run_sources = [], {}, {}
    record_scopes = {}
    own_records = set()  # this repository's records: the record pass judges their admissions itself
    owners, owner_reads = {}, {}
    # Placements bound to their Dispatches: this repository's records' (the record pass's alone, never rewritten
    # afterwards, which R212's superset reads), and those the worker loop reads from records naming a row's Run.
    placements_by_dispatch, run_placements = {}, {}
    scope_records_read, attribution_gaps = set(), []
    # Rule T (R206): gaps on another objective's terminal record, judged after the complete native inventories.
    terminal_gaps = []
    # What those inventories show: each Dispatch settled or current, and whether any current actor in this repository
    # is attributed to no open record.
    fleet_facts, fleet_paths, open_handles, unattributed = {}, {}, set(), {"actor": False}
    abandoned_dispatches = set()
    # Bound admissions whose report is still owed, in a record that is not `retired`: each is held by its record
    # whenever the native side makes no claim for its Dispatch.
    unfinished = []
    physical, overlap = _physical, _overlap
    host_slot, host_used = None, set()  # R212: this query's own candidates, and those its projection counted exited

    def relevant(facts, selection, key, fact=None):
        return _relevant(facts, selection, key, overlap, fact)

    def workspace(path, places):
        return _workspace(project, path, places)
    from .orca import _DEADLINE
    from .governor import report_owed
    budget = _DEADLINE.get() or Budget(float(timeout))

    def owner_claims():
        """The ownership claims whose current Run owner this query observes."""
        selection = {"worktree": physical(worktree) if worktree else None,
                     "branch": branch, "target": target, "effects": effects or []}
        for ownership in list(result["claims"]):
            if ownership["kind"] != "ownership" or "recorded_owner" not in ownership:
                continue
            if not worktree and ownership.get("objective") != objective or not relevant(ownership, selection, context["repo_key"]):
                continue
            yield ownership

    def unknown(record, code, **extra):
        if isinstance(record, str):
            try:
                record.encode("utf-8")
            except UnicodeError:
                record = "sha256:" + hashlib.sha256(os.fsencode(record)).hexdigest()
        fact_scope = extra.pop("fact_scope", record_scopes.get(record, scope))
        repair = {"action": "inspect_record", "record": record,
                  "condition": "Establish the named fact from its authoritative source; preserve the record and repeat the query"}
        if record is None and code.startswith("native_resources"):
            repair = {"command": ["orca", "terminal", "list", "--json"],
                      "condition": "Establish complete, well-formed current native terminal ownership"}
        elif record is None and code.startswith("orca_"):
            repair = {"command": ["orca", "orchestration", "worker-list", "--include-remote", "--limit", "100", *FULL_JSON],
                      "condition": "Reconcile the failed native inventory fact; incomplete coverage never proves absence"}
        elif record is None and code == "discovery_budget":
            repair = {"action": "inspect_read_budget", "condition": "Identify and reconcile the source read that exhausted CE's deadline before retrying"}
        elif code == history.RECORD_CHANGED:
            repair = {"action": "repeat_query", "record": record,
                      "condition": "The record changed while it was read; repeat the admission query"}
        result["unknowns"].append({"record": record, "code": code, "scope": fact_scope,
            "fact": extra.pop("fact", code), "reconciliation": extra.pop("reconciliation", repair),
            **extra})

    def unavailable(record, code):
        """One record Pod cannot read or attribute before any of its own resources is known holds only those
        resources (R206): it is named apart, never as a repository-wide unknown. A live writer it could own is still
        found by the complete native fleet and terminal inventories this query reads."""
        if record in record_scopes:
            unknown(record, code)  # Its own recorded scope is known; the hold stays on exactly that scope.
            return
        if code == history.RECORD_CHANGED:
            # Being written, not damaged: the same record-only hold, with a retry step instead of a repair.
            relevance = ("a record that changed while it was read holds only its own resources; current native "
                         "writers are inventoried independently")
            reconciliation = {"action": "repeat_query", "record": record,
                              "condition": "The record changed while it was read; repeat the admission query"}
        else:
            relevance = ("an unreadable record holds only its own resources; current native writers are inventoried "
                         "independently")
            reconciliation = {"action": "inspect_record", "record": record,
                              "condition": "Repair or remove the unreadable record; it holds only its own resources"}
        result.setdefault("unrelated_unknowns", []).append({
            "record": record, "code": code, "status": "unavailable", "fact": "record_readable",
            "scope": {"record": record}, "relevance": relevance, "reconciliation": reconciliation})

    def claim(row):
        try:
            encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise PodError("invalid_record", "Claim identities must be UTF-8") from None
        if len(encoded) > 16 * 1024:
            raise PodError("admission_claim_limit", "One claim exceeds the bounded projection")
        result["claims"].append(row)

    def effect_claims(directory, raw, journal, source):
        name = listing._name(raw)
        for row in journal["actions"]:
            if row["decision"] != "ALLOW" or row["outcome"] not in ("pending", "UNKNOWN"):
                continue
            action = row["action"]
            source_path = source.get("path", source.get("worktree")) if source else None
            if not source_path and source:
                source_path = next((item["worktree"] for item in known if item["repo_key"] == source.get("repo_key")), None)
            source_project = Path(source_path) if source_path and Path(source_path).is_dir() else None
            target_project = source_project or next((Path(item["worktree"]) for item in known
                if source and item["repo_key"] == source.get("repo_key")), None)
            identities, effect_source = _action_effects(row, source_project, policies)
            shared, relative = _effect_scopes(row, identities)
            action_target = _canonical_target(target_project, action["target"]) if action["kind"] in ("merge", "push", "pr_update") else action["target"]
            fact_scope = {"effects": identities, "target": action_target, "action": row["record_id"],
                          "global_effects": shared, "repository_effects": relative,
                          "repo_key": source.get("repo_key") if source else None}
            claim({"objective": name, "record": directory.name,
                "repo_key": source.get("repo_key") if source else None,
                "repository": source.get("repository") if source else None,
                "kind": "target_update" if action["kind"] in ("merge", "push", "pr_update") else "external_effect",
                "action": row["record_id"], "effects": identities, "effect_source": effect_source,
                "global_effects": shared, "repository_effects": relative,
                "target": action_target, "outcome": row["outcome"]})
            if identities is None:
                census.sound = False
                unknown(directory.name, "effects_unknown", objective=name, fact="external_effect_identity",
                        fact_scope=fact_scope, reconciliation={"action": "reconcile_governor",
                        "record": row["record_id"], "condition": "Read back the admitted action and establish its actual downstream effects"})
            elif relative and not (source or {}).get("repo_key"):
                census.sound = False
                unknown(directory.name, "effect_attribution_unknown", objective=name, fact="effect_repository",
                        fact_scope=fact_scope, reconciliation={"action": "establish_effect_repository",
                        "record": row["record_id"], "condition": "Establish the repository namespace of this admitted intrinsic effect"})

    consulted, proof_outcome = [], None
    selection_reads = []  # R212: records this query's candidate judgment read, beyond those it consulted
    try:
        budget.check()
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            raise PodError("unsafe_state", "State root is redirected")
        cohort = _shared(("cohort", str(root)), lambda: _cohort(root, budget))
        for entry in sorted(cohort, key=lambda row: row.name):
            budget.check()
            record = entry.name
            reading = True
            try:
                record.encode("utf-8")
            except UnicodeError:
                unavailable("sha256:" + hashlib.sha256(os.fsencode(record)).hexdigest(), "path_encoding")
                continue
            try:
                if entry.is_symlink():
                    raise PodError("unsafe_state", "Record directory is redirected")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                directory = Path(entry.path)
                has_context, has_other = _shared(("contents", str(directory)), lambda: _contents(directory, budget))
                if not has_context:
                    if has_other:
                        unavailable(record, "unattributed")
                    continue
                raw, identity = _primary(directory / "context.json")
                census.attribution(directory, raw, identity)
                reading = False  # The record reads; any later fault is about its own facts.
                try:
                    name = _membership(project, context, record, raw, known)
                except PodError as exc:
                    phase = _record_phase(raw, directory, census.phases)
                    if (exc.code == "record_attribution_unknown" and phase in TERMINAL_PHASES
                            and listing._name(raw) != objective):
                        try:
                            # Supported, terminal, and no hidden admission references; placement may be unrecorded.
                            terminal_rows = _admissions(raw, directory, placement_unknown=True)
                            journal, journal_id = _journal(directory / "governor.json")
                        except PodError:
                            raise exc from None
                        if not _effects_pending(journal):
                            if phase == "closed" and raw.get("admissions") == {}:
                                attribution_gaps.append((record, directory, identity, journal_id))
                            else:
                                terminal_gaps.append({"record": record, "directory": directory, "identity": identity,
                                    "journal_id": journal_id, "phase": phase, "rows": terminal_rows,
                                    "codes": [("record_attribution_unknown", {}, None)]})
                            continue
                    raise
                source = raw.get("_facts_attribution") or (raw.get("checkpoint") or {}).get("worktree")
                source = source if isinstance(source, dict) else next((item for item in known
                    if record_name(Path(item["worktree"]), item, listing._name(raw)) == record), None)
                references = (raw.get("checkpoint") or {}).get("native_refs")
                for ref in references if isinstance(references, list) else []:
                    if isinstance(ref, dict) and isinstance(ref.get("runId"), str) and source:
                        run_repositories.setdefault(ref["runId"], set()).add(source.get("repo_key"))
                        run_sources.setdefault(ref["runId"], []).append((directory, identity))
                if name is None:
                    if effects or target and source and source.get("repository") == context["repository"]:
                        journal, journal_id = _journal(directory / "governor.json")
                        effect_claims(directory, raw, journal, source)
                        effect_sources.append((directory, journal_id, _effect_identity(journal, open_objective=False)))
                    census.classify(directory, raw, "foreign")
                    continue
                own_records.add(record)
                phase = _record_phase(raw, directory, census.phases)
                try:
                    rows = _admissions(raw, directory)
                except PodError as exc:
                    if exc.code != "admission_facts_incomplete" or phase not in TERMINAL_PHASES or name == objective:
                        raise
                    try:
                        terminal_rows = _admissions(raw, directory, placement_unknown=True)
                        journal, journal_id = _journal(directory / "governor.json")
                    except PodError:
                        raise exc from None
                    if _effects_pending(journal):
                        raise exc from None
                    terminal_gaps.append({"record": record, "directory": directory, "identity": identity,
                        "journal_id": journal_id, "phase": phase, "rows": terminal_rows,
                        "codes": [("admission_facts_incomplete", {}, None)]})
                    continue
                if name == objective and "_facts_generation" in raw and digest(raw["_facts_generation"]) != identity:
                    # The selected record binds exact bytes: its claims must come from the bytes its attribution did.
                    raise PodError("record_changed", "The selected record changed between its attribution and claims reads")
                phases[record] = phase if raw["checkpoint"]["schema"] == "pod-checkpoint/v3" else "noncurrent"
                place = raw["checkpoint"].get("worktree") or {}
                for row in rows.values():
                    placement = _placement(row)
                    binding = row.get("native_binding") or {}
                    if row.get("abandoned_accounting") and binding.get("dispatchId"):
                        abandoned_dispatches.add(binding["dispatchId"])
                    if (placement.get("path") == listing._location(binding.get("worktreeId"))
                            and "branch" in placement and binding.get("dispatchId")):
                        placements_by_dispatch[binding["dispatchId"]] = (binding, placement)
                record_scopes[record] = {**scope, **_record_locations(place, rows)}
                journal, journal_id = _journal(directory / "governor.json")
                current_id = _claim_identity(raw, rows)
                effect_id = _effect_identity(journal, open_objective=phase == "open")
                selected = name == objective
                stamps[record] = [identity, journal_id] if selected else [current_id, effect_id]
                sources.append((directory, identity, journal_id, current_id, effect_id, selected))
                checkpoint = raw["checkpoint"]
                refs = checkpoint.get("native_refs")
                if not isinstance(refs, list):
                    raise PodError("state_unsupported", "Native Run references are unavailable")
                for ref in refs:
                    if (not isinstance(ref, dict) or not isinstance(ref.get("runId"), str)
                            or not ref["runId"] or not isinstance(ref.get("runtime"), str)):
                        raise PodError("state_unsupported", "Native Run reference is malformed")
                    runs.setdefault(ref["runId"], [])
                    run_owners.setdefault(ref["runId"], []).append((record, name, ref["runtime"]))
                place = checkpoint.get("worktree")
                open_objective = phase == "open"
                if open_objective:
                    if not isinstance(place, dict) or not isinstance(place.get("path"), str):
                        unknown(record, "ownership_unavailable", objective=name)
                    claim({"objective": name, "record": record, "kind": "ownership",
                        "source_schema": checkpoint["schema"], "current_contract": checkpoint["schema"] == "pod-checkpoint/v3",
                        "recorded_owner": raw.get("owner"), "recorded_native_refs": refs,
                        "owner_source": "recorded_context",
                        "worktree": place.get("path") if isinstance(place, dict) else None,
                        "branch": place.get("branch") if isinstance(place, dict) else None,
                        "locations_complete": isinstance(place, dict) and bool(place.get("path")) and "branch" in place})
                    from .governor import _validate_branch
                    for unit_name, unit in journal["units"].items():
                        remote_branch = _validate_branch(unit.get("branch"))
                        if remote_branch is not None:
                            claim({"objective": name, "record": record, "kind": "ownership", "unit": unit_name,
                                   "branch": remote_branch["branch"], "remote": remote_branch["remote"],
                                   "worktree": None, "locations_complete": True})
                unbound = []
                for key, row in rows.items():
                    binding = row.get("native_binding")
                    if binding is not None:
                        if not binding_valid(binding):
                            raise PodError("state_unsupported", "Native claim identity is malformed")
                        runs.setdefault(binding["runId"], []).append((record, name, key,
                            {"native_binding": binding, "runtime": row.get("runtime"), "boundary": row.get("boundary")}))
                        run_owners.setdefault(binding["runId"], []).append((record, name, row.get("runtime")))
                        owed = report_owed(row)
                        if owed is not None and phase != "retired":
                            # Its placement stays held until its report is consumed, whatever the native row
                            # shows; a `retired` record holds nothing (R112, R206).
                            unfinished.append((binding["dispatchId"], *_owed_hold(name, record, key, row)))
                    elif row["state"] not in ("closed", "deferred"):
                        held = {"objective": name, "record": record,
                            "kind": "writer" if row["role"] == "implement" else "assignment",
                            "admission": key, "worktree": listing._location(row.get("worktree"))
                                or (_placement(row)).get("path"),
                            "branch": (_placement(row)).get("branch"),
                            "locations_complete": bool((_placement(row)).get("path")),
                            "liveness": "unknown", "boundary": row.get("boundary")}
                        if phase in TERMINAL_PHASES and name != objective and not _effects_pending(journal):
                            unbound.append((key, row, held))  # Rule T judges it after the native inventories.
                            continue
                        claim(held)
                        unknown(record, "assignment_unbound", objective=name, admission=key)
                if unbound:
                    terminal_gaps.append({"record": record, "directory": directory, "identity": identity,
                        "journal_id": journal_id, "phase": phase, "rows": {key: row for key, row, _ in unbound},
                        "codes": [("assignment_unbound", {"objective": name, "admission": key}, held)
                                  for key, _, held in unbound]})
                effect_claims(directory, raw, journal, source or context)
                census.classify(directory, raw, "included", rows)
            except PodError as exc:
                # Only a record that cannot be read at all is named apart; a read deadline is no record's fault.
                (unavailable if reading and exc.code != "discovery_budget" else unknown)(record, exc.code)
            except OSError:
                (unavailable if reading else unknown)(record, "unreadable")
        if objective:
            # The proof is read before the change recheck and the end fence, so a change during its read is found.
            try:
                budget.check()
                proof_outcome = (_proof(project, objective, candidate), None)
            except PodError as exc:
                proof_outcome = ({"status": "unknown", "candidate": candidate, "reason": exc.code}, exc.code)
        def read_owner(directory):
            """One owning record's attribution read, generation and admissions; or the PodError that holds its
            Dispatch. Only its admissions' placements decide a hold, so its own coordinator place may be
            unrecorded. Read once per call."""
            try:
                owner_raw, current = _primary(directory / "context.json")
            except OSError:
                return PodError("unreadable", "The record cannot be read"), None
            except PodError as exc:
                return exc, None
            try:
                return (owner_raw, _admissions(owner_raw, directory, placement_unknown=True)), current
            except PodError as exc:
                return exc, current

        def foreign_owners(dispatch, run):
            """What the records owning `run` in other repositories say about `dispatch`, a Dispatch the native side
            saw without a claim: `(directory, generation, outcome)` per record read, where `outcome` is a hold, a
            PodError, or None. Each owing admission accounts for itself, whatever another record says of the same
            Dispatch. Which repository its placement belongs to is the record's to say, never the directory now at
            its path, so the owners are read whatever that path holds today. Judged once per call, as `_Owners`,
            which also names the owner records whose evidence closure this judgment consulted: a query that uses the
            judgment depends on those closures (R213)."""
            found, closures = [], set()
            for directory, generation in run_sources.get(run, []):
                if directory.name in own_records:
                    continue  # this repository's own record: the record pass and rule T judge its admissions
                read, current = _shared(("owner-read", str(directory)), lambda directory=directory: read_owner(directory))
                if current is not None and current != generation:
                    read = PodError("record_changed", "The record changed while it was read")
                if isinstance(read, PodError):
                    found.append((directory, generation if current is not None else None, read))
                    continue
                owner_raw, rows = read
                found.append((directory, generation, None))
                if _record_phase(owner_raw, directory, closures) == "retired":
                    continue
                for key, row in rows.items():
                    binding = row.get("native_binding") or {}
                    if binding.get("dispatchId") != dispatch or report_owed(row) is None:
                        continue
                    try:
                        found.append((directory, generation, _owed_hold(listing._name(owner_raw), directory.name, key, row)))
                    except (PodError, AttributeError, KeyError, TypeError, ValueError):
                        # An owing admission whose placement cannot be read holds its Dispatch as an unknown.
                        found.append((directory, generation, PodError("admission_placement_invalid",
                                                                      "Accepted placement facts cannot be read")))
            return _Owners(found, closures)

        def owners_of(dispatch, run):
            """The shared foreign-owner judgment of `dispatch` on `run`, read once per call (R185; R212 reads it too)."""
            return _shared(("foreign-owners", str(project), dispatch, run), lambda: foreign_owners(dispatch, run))

        def run_unreadable(run):
            """Whether a record naming `run` cannot be read whole as the worker loop's placement lookup reads it (its
            context changed since the record pass, or its admission inventory or a placement cannot be read), and the
            `(directory, generation)` of every record read. That lookup names a row outside the worktree registry as
            `worker_placement_unknown`. Judged once per call."""
            def judge():
                reads = []
                for directory, generation in run_sources.get(run, []):
                    try:
                        candidate_raw, current_generation = _primary(directory / "context.json")
                    except (PodError, OSError):
                        return True, tuple(reads)
                    reads.append((directory, generation))
                    if current_generation != generation:
                        return True, tuple(reads)
                    try:
                        _admissions(candidate_raw, directory)
                    except PodError:
                        return True, tuple(reads)
                return False, tuple(reads)
            return _shared(("run-unreadable", str(project), run), judge)

        # The Dispatches that rule T must find settled: those bound by a deferred terminal record's admissions.
        gap_dispatches = {(row.get("native_binding") or {}).get("dispatchId") for gap in terminal_gaps
                          for row in gap["rows"].values() if row.get("native_binding")}
        discovery = _SHARED.get()
        if discovery is not None:
            # The call reads these native facts once, for every query, before the end fence starts.
            discovery.abandoned |= abandoned_dispatches
            # Started while the other queries read their records, or as soon as the terminal inventory is known.
            discovery.name_owners({ref["runId"] for ownership in owner_claims() for ref in ownership["recorded_native_refs"]})
            # R212: this query's own candidate scope, which the call asks the host about before the end fence.
            owed = {}  # this repository's owed-report holds by Dispatch, as their records place them
            for dispatch, held, _ in unfinished:
                owed.setdefault(dispatch, []).append(held)
            host_slot = _HostExitSlot(project, {"worktree": physical(worktree) if worktree else None,
                                                "branch": branch, "target": target, "effects": effects or []},
                                      {*runs, *run_sources}, set(runs), gap_dispatches, set(abandoned_dispatches),
                                      scope=scope, owed=owed, owners=owners_of, unreadable=run_unreadable,
                                      placements=placements_by_dispatch)
            discovery.host_exit_slots.append(host_slot)
        yield "native"  # Every query's record pass runs while the native inventory is read.
        # Prefer the installed all-Run inventory for CE's unbound caller.
        # A bound caller retains exact-Run reads and the same outer deadline.
        fleet = _shared(("fleet",), _fleet)
        terminals = _shared(("terminals",), _terminals)
        if terminals.get("complete") is not True:
            raise PodError("native_resources_incomplete", "Current native resource coverage is incomplete")
        if fleet is not None and terminals["runtime"] != fleet["runtime"]:
            raise PodError("orca_runtime_changed", "Worker and terminal inventory runtimes differ")
        result["native_resources_complete"] = terminals.get("complete") is True
        native_groups = []
        if fleet is None:
            unknown(None, "native_inventory_scope", fact="all_run_coverage", reconciliation={
                "action": "use_ce_unbound_read_backend",
                "condition": "Read from CE's unbound backend while retaining the original caller in CE authority checks; do not mutate the native Run binding"})
        else:
            native_groups = [(None, fleet)]
        places = _shared(("places", str(project)), lambda: _places(project))

        def coordinator(run):
            return _coordinator(run, terminals, budget)

        def elsewhere(path):
            """Rule T 4b: `path` is proven a registered worktree of another repository. It is an existing directory
            reached without a symlink, outside this repository's worktrees, whose Git common directory differs from
            this repository's and whose own worktree registry lists it. Anything unproved is not elsewhere. Judged
            once per call for each repository and path."""
            return _shared(("elsewhere", str(project), str(path)), lambda: _elsewhere(path))

        def _elsewhere(path):
            if not isinstance(path, str) or not path.startswith("/") or os.path.normpath(path) != path:
                return False
            try:
                if not stat.S_ISDIR(os.lstat(path).st_mode) or str(Path(path).resolve(strict=True)) != path:
                    return False
                if workspace(path, places)[1]:
                    return False
                other = _context(Path(path))
                if other.get("repo_key") is None or other["repo_key"] == context["repo_key"]:
                    return False
                # The registry the identity read listed: one `git worktree list` of that repository, from that path.
                return any(physical(root) == path for root in other.get("linked_worktrees") or [])
            except (OSError, PodError, ValueError, RuntimeError):
                return False

        owners_consulted = set()

        def foreign_holds(dispatch, run, path):
            """Apply the shared judgment to this query: holds, unknowns, and every read record rechecked (R185)."""
            judged = owners_of(dispatch, run)
            census.phases.update(getattr(judged, "closures", ()))  # the owner phases this query now depends on
            for directory, generation, outcome in judged:
                if generation is not None and directory.name not in owners_consulted:
                    consulted.append((directory, generation))  # rechecked with every other consulted source
                    owners_consulted.add(directory.name)
                if isinstance(outcome, PodError):
                    census.sound = False
                    unknown(directory.name, "record_changed" if outcome.code == "record_changed" else "worker_placement_unknown",
                            fact_scope={**scope, "worktree": path}, fact="worker_placement", dispatch=dispatch,
                            cause=outcome.code)
                elif outcome is not None:
                    unfinished.append((dispatch, *outcome))

        def names_here(row):
            """Rule T 4b (3): the admission itself names this repository's workspace or the requested branch."""
            placement = _placement(row)
            location = listing._location(row.get("worktree")) or placement.get("path")
            return bool(location and workspace(location, places)[1]) or bool(branch and placement.get("branch") == branch)
        for ownership in owner_claims():
            observations = []
            for run in sorted({ref["runId"] for ref in ownership["recorded_native_refs"]}):
                if run not in owner_reads:
                    try:
                        owner_reads[run] = _shared(("owner", run, terminals["runtime"]), lambda: _gated(
                            _owner_observation, run, terminals, terminals["runtime"]))
                    except PodError as exc:
                        owner_reads[run] = {"run": run, "status": "unknown", "code": exc.code}
                        if exc.code == "native_read_after_fence":
                            # A read the fence would not cover is never made: this result is incomplete.
                            unknown(ownership["record"], exc.code, objective=ownership.get("objective"),
                                    fact="current_run_owner", run=run, reconciliation={
                                        "action": "repeat_admission_batch",
                                        "condition": "Repeat the batch; every Run owner read precedes the end fence"})
                observations.append(owner_reads[run])
            ownership["owner_observations"] = observations
            matches = bool(observations) and all(row.get("terminal") == ownership["recorded_owner"]
                and row.get("worktree") == ownership.get("worktree") and row.get("live") is True for row in observations)
            ownership["owner_identity_matches"] = matches
            owners[ownership["record"]] = matches
        workers, context_only, skipped = {}, {}, {}
        for requested_run, native in native_groups:
            budget.check()
            if native.get("complete") is not True:
                unknown(None, "native_inventory_incomplete")
                continue
            workers = {}
            discovery = _SHARED.get()
            if discovery is not None:
                for source in native["workers"]:
                    if isinstance(source, dict) and (source.get("workerState") == "abandoned"
                                                     or source.get("dispatchId") in abandoned_dispatches):
                        discovery.prefetch_claim(source, native["runtime"])
            for source in native["workers"]:
                from .ledger import _native_assignment_settled
                if (terminals.get("complete") is True and source.get("workerState") == "unsupervised"
                        and source.get("resource") is None and _native_assignment_settled({"result": {
                            "dispatch": {"id": source.get("dispatchId"), "runId": source.get("runId"),
                                         "taskId": source.get("taskId"), "status": source.get("dispatchStatus")},
                            "projection": source.get("projection")}})):
                    # A settled context-only Dispatch owns no supervised
                    # resource. Its terminal's current ownership is inventoried
                    # separately below; no process exit is inferred here.
                    fleet_facts[source.get("dispatchId")] = "settled"
                    context_only[source.get("dispatchId")] = source
                    continue
                shown = _worker_fact(source)
                path = shown["worktree"]
                run = shown["run"]
                if shown["liveness"] == "exited" and source.get("workerState") != "abandoned" and shown["dispatch"] not in abandoned_dispatches:
                    workers[shown["dispatch"]] = shown
                    fleet_facts[shown["dispatch"]] = "settled"
                    continue
                fleet_facts[shown["dispatch"]] = "current"
                fleet_paths[shown["dispatch"]] = path
                registered, in_repository = workspace(path, places)
                # Only a row this query would otherwise hold or read a record for is judged, so the Run coordinator
                # reads stay bounded by this repository's own native actors.
                judged = ((in_repository or path is None or run in runs or run in run_sources
                           or shown["dispatch"] in gap_dispatches)
                          and source.get("workerState") != "abandoned" and shown["dispatch"] not in abandoned_dispatches)
                host_decision = host_slot.decisions.get(shown["dispatch"]) if host_slot is not None else None
                if judged and _shared(("settled-gone", shown["dispatch"]), lambda: _settled_gone(
                        source, shown, native["workers"], terminals, coordinator)):
                    # Rule S (R206): an ended Dispatch whose terminal, workspace and Run coordinator are all gone.
                    workers[shown["dispatch"]] = shown
                    fleet_facts[shown["dispatch"]] = "settled"
                    result.setdefault("unrelated_unknowns", []).append({
                        "record": "run:" + str(run), "code": "native_liveness_unverified", "fact": "process_liveness",
                        "dispatch": shown["dispatch"], "scope": {"run": run, "worktree": path},
                        "relevance": "settled_dispatch_gone_resources",
                        "reconciliation": {"command": ["orca", "orchestration", "worker-show", "--dispatch",
                                                       shown["dispatch"], *FULL_JSON],
                                           "condition": "Settled with its terminal, workspace and Run coordinator gone; "
                                                        "it holds no current resource"}})
                    continue
                if judged and host_decision is not None and host_decision["counted"]:
                    # R212: this call's host scan found no process carrying this settled local Dispatch's identity.
                    shown["liveness"], shown["exit_source"] = "exited", "host_process_exit"
                    workers[shown["dispatch"]] = shown
                    fleet_facts[shown["dispatch"]] = "settled"
                    host_used.add(shown["dispatch"])
                    continue
                branch_fact, branch_known = places.get(registered), registered is not None
                recovered = _recovered(shown, placements_by_dispatch, run_placements)
                if not branch_known and recovered is not None:
                    branch_fact, branch_known = recovered["branch"], True
                if path and not branch_known and not elsewhere(path):
                    # Native work in this repository may have been admitted by a
                    # different repository's objective. Inspect only that actual
                    # possible conflict, never every foreign admission history.
                    # A workspace proven to be another repository's worktree is not this repository's work (T 4b).
                    for directory, generation in run_sources.get(run, []):
                        if directory.name in scope_records_read:
                            continue
                        scope_records_read.add(directory.name)
                        candidate_raw, current_generation = _primary(directory / "context.json")
                        consulted.append((directory, generation))  # Rechecked with every other source.
                        if current_generation != generation:
                            unknown(directory.name, "record_changed")
                            continue
                        try:
                            candidate_rows = _admissions(candidate_raw, directory)
                        except PodError as exc:
                            census.sound = False
                            # Its own fact, distinct from the record's own inventory gap (D2): this live or unverified
                            # worker's placement cannot be read from the record that owns its Run.
                            unknown(directory.name, "worker_placement_unknown", fact_scope={**scope, "worktree": path},
                                    fact="worker_placement", dispatch=shown["dispatch"], cause=exc.code)
                            continue
                        for candidate_row in candidate_rows.values():
                            binding = candidate_row.get("native_binding") or {}
                            placement = _placement(candidate_row)
                            if placement.get("path") == listing._location(binding.get("worktreeId")) and "branch" in placement:
                                run_placements[binding.get("dispatchId")] = (binding, placement)
                        recovered = _recovered(shown, run_placements)
                        if recovered is not None:
                            branch_fact, branch_known = recovered["branch"], True
                            break
                native_effects = []
                actor_repo = context["repo_key"] if registered is not None else (
                    recovered.get("repo_key") if recovered is not None else None)
                actor_scope = {"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                               "locations_complete": bool(path) and branch_known}
                if (source.get("workerState") == "abandoned" or shown["dispatch"] in abandoned_dispatches) and (effects or relevant(actor_scope,
                        {"worktree": physical(worktree) if worktree else None,
                         "branch": branch, "target": target, "effects": effects or []}, context["repo_key"])):
                    try:
                        observed_claim = _shared(("claim", shown["dispatch"], native["runtime"]),
                                                 lambda: _native_claim(source, native["runtime"]))
                        if observed_claim is None:
                            shown["liveness"] = "exited"
                        else:
                            shown["liveness"] = observed_claim["process"]
                            native_effects = observed_claim["effects"]
                            if native_effects and effects:
                                unknown(None, "native_effect_identity_unknown", dispatch=shown["dispatch"],
                                        fact_scope={"effects": None}, fact="native_effect_identity",
                                        reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], *FULL_JSON],
                                                        "condition": "Reconcile the W1-reported pending native effects and their targets"})
                            if native_effects and target:
                                from .abandoned import _effect_conflicts
                                target_branch = (target.removeprefix("refs/remotes/").partition("/")[2]
                                                 if target.startswith("refs/remotes/") else target.removeprefix("refs/heads/"))
                                if _effect_conflicts({"repo_key": actor_repo, "effects": native_effects},
                                                     {"repo_key": context["repo_key"], "branch": target_branch}):
                                    unknown(None, "native_effect_target_unknown", dispatch=shown["dispatch"],
                                        fact="native_effect_target", fact_scope={"repo_key": context["repo_key"], "target": target},
                                        reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], *FULL_JSON],
                                                       "condition": "Reconcile the shared-reader native effect that may update this target"})
                    except PodError as exc:
                        unknown(None, exc.code, dispatch=shown["dispatch"],
                                fact_scope={"effects": None} if effects else actor_scope,
                                fact="current_native_claim", reconciliation={"action": "read_shared_current_claim",
                                "condition": "Load the W1-owned claim reader and read the exact current worker observation"})
                if requested_run is None and run not in runs and not in_repository:
                    if path is None and shown["liveness"] != "exited":
                        run_repo_keys = run_repositories.get(run, set())  # Never rebinds the owners dict.
                        unknown(None, "native_location_unavailable", dispatch=shown["dispatch"],
                                fact_scope={"repo_keys": sorted(key for key in run_repo_keys if key), "run": run},
                                reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], *FULL_JSON],
                                                "condition": "Resolve the native resource's repository and worktree ownership"})
                    # No claim here, live or not, but another repository's owed admission may have placed it here:
                    # its owners are still read, so the record holds that placement (pod#237).
                    skipped[shown["dispatch"]] = shown
                    continue
                workers[shown["dispatch"]] = shown
                if shown["liveness"] == "exited" and not native_effects:
                    continue
                open_run = any(phases.get(owner) == "open" for owner, _, _ in run_owners.get(run, []))
                if open_run and shown["terminal"]:
                    open_handles.add(shown["terminal"])
                elif not open_run and (in_repository or path is None):
                    unattributed["actor"] = True  # A current actor no open record accounts for (rule T, 4)
                stamps["dispatch:" + shown["dispatch"]] = digest(shown)
                claim({"kind": "writer", "run": run,
                    "dispatch": shown["dispatch"], "worktree": path,
                    "native_terminal": shown["terminal"], "native_resource": shown["resource"],
                    "branch": branch_fact, "liveness": shown["liveness"],
                    "native_effects": native_effects,
                    "repo_key": actor_repo, "locations_complete": bool(path) and branch_known,
                    "boundaries": [row.get("boundary") for _, _, _, row in runs.get(run, [])
                                   if row["native_binding"]["dispatchId"] == shown["dispatch"]],
                    "objectives": sorted({name for _, name, _ in run_owners.get(run, [])})})
                if path is None:
                    unknown("run:" + str(run), "native_location_unavailable", dispatch=shown["dispatch"])
                if shown["liveness"] not in ("live", "exited"):
                    unknown("run:" + str(run), "native_liveness_unverified", dispatch=shown["dispatch"],
                            **({"host_exit": {"code": host_decision["code"]}} if host_decision is not None else {}),
                            fact="process_liveness", fact_scope={"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                                "locations_complete": bool(path) and branch_known},
                            reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], *FULL_JSON],
                                            "condition": "Read authoritative process liveness; resource release alone is not process exit"})
            checked_runs = [requested_run] if requested_run else list(runs)
            for run in checked_runs:
                for record, name, runtime in set(run_owners[run]):
                    if native["runtime"] != runtime and (requested_run is not None or phases[record] == "open"):
                        if owners.get(record) and requested_run is None:
                            result.setdefault("recorded_runtime_changes", []).append({"record": record, "objective": name,
                                "recorded_runtime": runtime, "observed_runtime": native["runtime"],
                                "current_owner_identity_matches": True})
                        else:
                            unknown(record, "native_runtime_changed", objective=name, fact="current_run_identity",
                                    reconciliation={"action": "read_current_run_owner", "run": run,
                                        "condition": "Establish the current coordinator/resource identity; rebind only an objective whose write authority is needed"})
                for record, name, key, row in runs[run]:
                    binding = row["native_binding"]
                    shown = workers.get(binding["dispatchId"])
                    if (not isinstance(shown, dict) or shown["run"] != run
                            or shown["task"] != binding["taskId"]
                            or shown["worker"] != binding["workerId"]
                            or shown["worktree"] != listing._location(binding["worktreeId"])
                            or shown["terminal"] != binding["terminalHandle"]):
                        unknown(record, "native_identity_unverified", objective=name, admission=key)
        # A bound admission whose report is still owed keeps its placement held by its record once the native side
        # settles, skips or leaves out its Dispatch (exited, rule S, R212, a settled context-only row, a row skipped
        # outside the repository, or a native writer this query's relevance filters out): consuming the report reads
        # the worker's result where it was placed. A Dispatch the native inventory lacks is already unknown. Only a
        # native writer claim in this query's own result already holds it, so the hold is never shown twice and
        # never hidden behind a claim the result leaves out (pod#237).
        selection = {"worktree": physical(worktree) if worktree else None,
                     "branch": branch, "target": target, "effects": effects or []}
        native_writers = [row for row in result["claims"] if "native_terminal" in row and row.get("dispatch")]
        natively_shown = {row["dispatch"] for row in native_writers}
        natively_claimed = {row["dispatch"] for row in native_writers if relevant(row, selection, context["repo_key"])}
        for dispatch in fleet_facts:
            # Each owning record accounts for its own admission: another record's consumed binding of the same
            # Dispatch never discharges it. The owners are read whatever the Dispatch's path holds today; which
            # repository an owed placement belongs to is the record's to say (`held_here`).
            if dispatch in natively_claimed:
                continue
            shown = workers.get(dispatch, skipped.get(dispatch))
            if shown is None and dispatch in context_only:
                try:
                    shown = _worker_fact(context_only[dispatch])
                except PodError:
                    shown = {"run": context_only[dispatch].get("runId"), "worktree": None}
            if shown is None or shown["run"] not in run_sources:
                continue
            foreign_holds(dispatch, shown["run"], shown["worktree"])
        def held_here(repo_key, location):
            """Whether an owed placement is this repository's, by the identity its admission recorded: its repository
            key decides, whatever path now nests it; without one, its location in this repository, and otherwise
            anything not proved another repository's registered worktree."""
            if repo_key is not None:
                return repo_key == context["repo_key"]
            return bool(location and workspace(location, places)[1]) or not (location and elsewhere(location))
        for dispatch, held, repo_key in unfinished:
            if dispatch in fleet_facts and dispatch not in natively_claimed and held_here(repo_key, held["worktree"]):
                if dispatch in natively_shown and not relevant(held, selection, context["repo_key"]):
                    continue  # its native writer, left out as well, already counts it among the unrelated claims
                claim(held)  # `exited`: the record holds the placement it still owes, whatever process state remains
        from .retirement import _exited
        for terminal in terminals["terminals"]:
            path = terminal.get("worktreePath") or listing._location(terminal.get("worktreeId"))
            if path is None and not _exited(terminal):
                unknown(None, "native_location_unavailable", fact="terminal_location",
                        fact_scope={"terminal": terminal.get("handle")},
                        reconciliation={"command": ["orca", "terminal", "show", "--terminal", terminal.get("handle"), "--json"],
                                        "condition": "Establish the current terminal's worktree and repository ownership"})
                continue
            registered, in_repository = workspace(path, places)
            if not in_repository or _exited(terminal):
                continue
            live = terminal.get("connected") is True and terminal.get("writable") is True and terminal.get("orphaned") is False
            if terminal.get("handle") not in open_handles and terminal.get("handle") not in {
                    row.get("recorded_owner") for row in result["claims"] if row.get("kind") == "ownership"}:
                unattributed["actor"] = True
            claim({"kind": "writer", "native_terminal": terminal.get("handle"), "worktree": path,
                   "branch": places.get(registered), "locations_complete": registered is not None, "repo_key": context["repo_key"],
                   "liveness": "live" if live else "unverifiable", "source": "native_terminal_inventory"})
            if not live:
                unknown(None, "native_liveness_unverified", fact="terminal_liveness",
                        terminal=terminal.get("handle"),
                        fact_scope={**scope, "worktree": path, "branch": places.get(registered), "locations_complete": registered is not None},
                        reconciliation={"command": ["orca", "terminal", "show", "--terminal", terminal.get("handle"), "--json"],
                                        "condition": "Establish the current terminal ownership and process state"})
        if target:
            observed = _shared(("commit", str(project), target), lambda: gitio.resolve_commit(project, target))
            result["target"] = {"ref": target, "requested_ref": requested_target, "commit": observed}
            if observed is None:
                unknown(None, "target_unavailable")
            elif target_commit and target_commit != observed:
                result["conflicts"].append({"kind": "target_changed", "expected": target_commit, "observed": observed})
        yield "recheck"
        for record, directory, generation, journal_id in attribution_gaps:
            unchanged = (_after_context(directory / "context.json") == generation
                         and _after_journal(directory / "governor.json") == journal_id)
            if fleet is not None and fleet.get("complete") is True and unchanged:
                result.setdefault("unrelated_unknowns", []).append({"record": record,
                    "code": "record_attribution_unknown", "fact": "repository_attribution", "scope": {"record": record},
                    "relevance": "closed_without_admissions_or_outstanding_effects; current native claims independently inventoried",
                    "reconciliation": {"action": "normalize_attribution", "owner": "storage",
                                       "condition": "Preserve source-proven repository attribution without inventing placement"}})
            else:
                unknown(record, "record_attribution_unknown")
        native_complete = (fleet is not None and fleet.get("complete") is True
                           and terminals.get("complete") is True)
        for gap in terminal_gaps:
            if native_complete and _terminal_history(gap, project, fleet_facts, unattributed["actor"],
                                                     lambda dispatch, row: (not names_here(row)
                                                                            and elsewhere(fleet_paths.get(dispatch)))):
                for code, extra, _ in gap["codes"]:
                    result.setdefault("unrelated_unknowns", []).append({
                        "record": gap["record"], "code": code, "fact": code, "scope": {"record": gap["record"]},
                        **extra, "relevance": "terminal_record_history",
                        "reconciliation": {"action": "inspect_record", "record": gap["record"],
                            "condition": "Settled history of a terminal record; current native actors are inventoried "
                                         "independently"}})
            else:
                for code, extra, held in gap["codes"]:
                    if held is not None:
                        claim(held)
                    unknown(gap["record"], code, **extra)
        if objective:
            result["proof"], code = proof_outcome
            if code is not None:
                unknown(None, code, objective=objective)
            elif result["proof"]["status"] == "unknown":
                unknown(None, "proof_unverified", objective=objective)
        for directory, journal_id, effect_id in effect_sources:
            budget.check()
            after_id = _after_journal(directory / "governor.json")
            if after_id != journal_id and _effect_identity(_journal(directory / "governor.json")[0],
                                                          open_objective=False) != effect_id:
                unknown(directory.name, "record_changed", fact="external_effects")
        for source_project, policy in policies.items():
            from .config import effective
            if _shared(("effective", source_project), lambda: effective(Path(source_project)))["revision"] != policy["revision"]:
                census.sound = False
                unknown(None, "effect_policy_changed", fact="effect_mapping")
        # R212: every record this query's candidate judgment read is rechecked with its consulted sources, so a change
        # after that read is this query's `record_changed`, never a hold its judgment silently dropped.
        selection_reads = [entry for entry in (host_slot.dependencies.values() if host_slot is not None else ())
                           if entry not in consulted]
        if host_slot is not None:
            census.phases |= host_slot.phases
        for directory, generation in [*consulted, *selection_reads]:
            budget.check()
            if _after_context(directory / "context.json") != generation:
                unknown(directory.name, "record_changed")
        for directory, identity, journal_id, current_id, effect_id, selected in sources:
            budget.check()
            after_id = _after_context(directory / "context.json")
            after_journal_id = _after_journal(directory / "governor.json")
            if after_id != identity or after_journal_id != journal_id:
                after_raw = _primary(directory / "context.json")[0]
                after_journal = _journal(directory / "governor.json")[0]
                after_rows = _admissions(after_raw, directory)
                if (selected or _claim_identity(after_raw, after_rows) != current_id
                        or _effect_identity(after_journal, open_objective=_record_phase(
                            after_raw, directory, census.phases) == "open") != effect_id):
                    # The change holds what the record named before and what it names now, so a placement moved to
                    # the requested worktree or branch is never read as unrelated.
                    before_scope = record_scopes.get(directory.name, scope)
                    now = _record_locations(after_raw["checkpoint"].get("worktree") or {}, after_rows)
                    unknown(directory.name, "record_changed", fact_scope={**before_scope,
                        "worktrees": sorted({*before_scope.get("worktrees", []), *now["worktrees"]}),
                        "branches": [*before_scope.get("branches", []), *now["branches"]],
                        "locations_complete": (before_scope.get("locations_complete") is True and now["locations_complete"]
                                               and all(before_scope.get("branches", [None])) and all(now["branches"]))})
        after = _shared(("cohort-after", str(root)), lambda: _after_cohort(root, budget))
        before = {entry.name for entry in cohort}
        for added in sorted(set(after) - before):
            budget.check()
            entry = after[added]
            try:
                raw, _ = _primary(Path(entry.path) / "context.json")
                if _membership(project, context, added, raw, known) is None and not effects:
                    continue
            except (PodError, OSError):
                pass
            unknown(added, "inventory_changed")
        known = {row[0].name for row in sources}
        if (before - set(after)) & known:
            unknown(None, "inventory_changed")
        census.cohort = before
        census.finished = before == set(after) and not attribution_gaps and not terminal_gaps
    except PodError as exc:
        unknown(None, exc.code)
    except OSError:
        unknown(None, "inventory_unreadable")
    if objective and result["proof"] is None:
        result["proof"] = {"status": "unknown", "candidate": candidate, "reason": "admission_incomplete"}
    request = {"worktree": physical(worktree) if worktree else None,
               "branch": branch, "target": target, "effects": effects or []}
    result["scope"]["selection"] = {**request, "target": requested_target}
    all_unknowns = result["unknowns"]
    unread = [{"record": row["record"], "code": row["code"]} for row in result.get("unrelated_unknowns", [])
              if row.get("status") == "unavailable"]
    history_gaps = any(row.get("relevance") in ("terminal_record_history", "settled_dispatch_gone_resources")
                       for row in result.get("unrelated_unknowns", []))
    result["repository_complete"] = not all_unknowns and not unread and not history_gaps
    result["unavailable_records"] = unread
    result["unknowns"] = [row for row in all_unknowns if row.get("objective") == objective and objective
                          or relevant(row["scope"], request, context["repo_key"], row["fact"])]
    relevant_unknowns = list(result["unknowns"])
    if not worktree and not branch and (target or effects):
        writers = [row for row in result["unknowns"] if row["fact"] in ("process_liveness", "terminal_liveness")]
        if len(writers) > 1:
            result["unknowns"] = [row for row in result["unknowns"] if row not in writers]
            result["unknowns"].append({"record": None, "code": "native_writer_scope_unknown", "fact": "writer_relevance",
                "scope": {"repo_key": context["repo_key"], "target": target}, "count": len(writers),
                "by_code": dict(Counter(row["code"] for row in writers)),
                "references": {"dispatches": sorted({row["dispatch"] for row in writers if row.get("dispatch")}),
                               "terminals": sorted({row["terminal"] for row in writers if row.get("terminal")})},
                "reconciliation": {"action": "supply_candidate_workspace",
                    "condition": "Supply the candidate worktree and branch, or reconcile the listed unlocated writer resources before deciding their relevance"}})
    result["unrelated_unknowns"] = [*result.get("unrelated_unknowns", []),
                                    *(row for row in all_unknowns if row not in relevant_unknowns)]
    unrelated = result["unrelated_unknowns"]
    result["unrelated_unknowns"] = {"count": len(unrelated), "by_code": dict(Counter(row["code"] for row in unrelated)),
        "references": {"records": sorted({row["record"] for row in unrelated
                                          if row.get("record") and not row["record"].startswith("run:")}),
                       "runs": sorted({row["record"][4:] for row in unrelated if str(row.get("record", "")).startswith("run:")}),
                       "dispatches": sorted({row["dispatch"] for row in unrelated if row.get("dispatch")}),
                       "terminals": sorted({row.get("terminal") or row["scope"]["terminal"] for row in unrelated
                                            if row.get("terminal") or row["scope"].get("terminal")})},
        "diagnostic": ["pod", "admission", "--repository", str(project), "--json"]}
    all_claims = result["claims"]
    result["claims"] = [row for row in all_claims if relevant(row, request, context["repo_key"])]
    result["claims"].sort(key=digest)
    result["unrelated_claims"] = len(all_claims) - len(result["claims"])
    for claim in result["claims"]:
        if claim.get("objective") == objective and claim["kind"] == "ownership":
            continue
        dimensions = []
        if worktree and claim.get("worktree") and overlap(claim["worktree"], physical(worktree)):
            dimensions.append("worktree")
        if branch and claim.get("branch") == branch:
            dimensions.append("branch")
        if target and claim.get("target") == target and claim.get("repo_key", context["repo_key"]) == context["repo_key"]:
            dimensions.append("target_update")
        if _effect_match(claim, effects or [], context["repo_key"]):
            dimensions.append("external_effect")
        if dimensions:
            result["conflicts"].append({"claim": claim, "dimensions": dimensions})
    result["complete"] = not result["unknowns"]
    active_records = {claim["record"] for claim in result["claims"] if claim.get("record")}
    for claim in result["claims"]:
        active_records.update(record_name(project, context, name) for name in claim.get("objectives", []))
    if objective:
        active_records.add(record_name(project, context, objective))
    # An inert finished record supplies no current exclusion dependency. Adding
    # retained finished history must not invalidate an otherwise identical read.
    stamps = {key: value for key, value in stamps.items()
              if key in active_records or key.startswith("dispatch:")}
    result["snapshot"] = digest({"scope": scope, "records": stamps, "claims": result["claims"], "target": result["target"],
        "request": {"objective": objective, "candidate": candidate, "worktree": worktree,
                    "branch": branch, "target": target, "effects": effects or []}})
    if expect and result["snapshot"] != expect:
        result["conflicts"].append({"kind": "snapshot_changed"})
    if host_slot is not None and host_slot.decisions:
        result["host_exits"] = _host_exit_rows(host_slot, host_used)  # this query's own candidates only (R212)
    result["status"] = "unknown" if not result["complete"] else "blocked" if (result["conflicts"] or
        result["proof"] is not None and result["proof"]["status"] != "ready") else "observed"
    result = public_decisions(result)
    journals = {directory.name: journal_id for directory, journal_id, _ in effect_sources}
    journals.update({directory.name: journal_id for directory, _, journal_id, *_ in sources})
    # The census qualifies those reads as strictly as consulted ones (R213): a claims read from a later generation than
    # the attribution read, or an incomplete one, leaves it false even where the query is complete under R207.
    result["record_census"] = census.witness(result, budget, journals,
                                             {directory for directory, _ in [*consulted, *selection_reads]},
                                             {path: policy["revision"] for path, policy in policies.items()})
    return result


def _empty_scan():
    from contextlib import nullcontext
    return nullcontext(())


def _context(path: Path) -> dict:
    return _shared(("context", str(path)), lambda: repository_context(path, fresh=True))


def _cohort(root: Path, budget) -> list:
    _shared(("census-root", str(root)), lambda: _census_stat(root))
    try:
        with os.scandir(root) as entries:
            cohort = []
            for entry in entries:
                budget.check()
                if not entry.name.startswith("."):
                    cohort.append(entry)
            identities = []
            for entry in sorted(cohort, key=lambda row: row.name):
                budget.check()
                identities.append(_census_entry(entry))
            _shared(("census-cohort", str(root)), lambda: identities)
            return cohort
    except FileNotFoundError:
        _shared(("census-cohort", str(root)), lambda: [])
        return []


def _after_cohort(root: Path, budget) -> dict:
    _shared(("census-root", str(root)), lambda: _census_stat(root))
    with os.scandir(root) if root.exists() else _empty_scan() as entries:
        after = {}
        for entry in entries:
            budget.check()
            if not entry.name.startswith("."):
                after[entry.name] = entry
        identities = []
        for entry in sorted(after.values(), key=lambda row: row.name):
            budget.check()
            identities.append(_census_entry(entry))
        _shared(("census-cohort", str(root)), lambda: identities)
        return after


def _contents(directory: Path, budget) -> tuple[bool, bool]:
    has_context = has_other = False
    with os.scandir(directory) as contents:
        for item in contents:
            budget.check()
            if item.name == "context.json":
                has_context = True
                break
            has_other = has_other or item.name != ".lock"
    return has_context, has_other


def _places(project: Path) -> dict:
    with gitio.worktree_inventory(project) as inventory:
        return {row["worktree"]: row.get("branch", "").removeprefix("refs/heads/") or None
                for row in gitio.worktree_rows(inventory)}


def _unknown_result(project: Path, kwargs: dict, code: str) -> dict:
    """The single query's answer when its inventory cannot be read at all."""
    scope = {"repository": None, "repo_key": None, "path": str(Path(kwargs.get("repository") or project).expanduser().absolute()),
             "inventory": "pod_objectives_and_recorded_runs", "selection": {
                "worktree": str(Path(kwargs["worktree"]).resolve()) if kwargs.get("worktree") else None,
                "branch": kwargs.get("branch"), "target": kwargs.get("target"), "effects": kwargs.get("effects") or []}}
    return {"schema": "pod-admission-query/v1", "status": "unknown", "scope": scope,
            "complete": False, "claims": [], "conflicts": [],
            "repository_complete": False, "native_resources_complete": False,
            "record_census": _no_census(),
            "unavailable_records": [],
            "unrelated_unknowns": {"count": 0, "by_code": {}, "references": {
                "records": [], "runs": [], "dispatches": [], "terminals": []}},
            "proof": {"status": "unknown", "candidate": kwargs["candidate"], "reason": code}
                     if kwargs.get("objective") else None,
            "snapshot": None, "target": None, "authorization": False,
            "unknowns": [{"record": None, "code": code, "scope": scope}],
            "next_action": "Restore repository inventory access, then repeat the admission query"}


def _bounded(result: dict, kwargs: dict) -> dict:
    """The single query's own projection limit, applied to each result."""
    from .orca import MAX_OUTPUT
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT:
        omitted = {"claims": len(result["claims"]), "conflicts": len(result["conflicts"]),
                   "unknowns": len(result["unknowns"])}
        result.update(status="unknown", complete=False, claims=[], conflicts=[], snapshot=None,
                      record_census=_no_census(),
                      proof={"status": "unknown", "reason": "admission_projection_limit"} if kwargs.get("objective") else None,
                      unknowns=[row for row in result["unknowns"] if row["record"] is not None
                                and not row["record"].startswith("run:") and "admission" not in row][:4096])
        result["unknowns"].append({"record": None, "code": "admission_projection_limit", "scope": result["scope"],
                                   "omitted": omitted})
    return result


def _prefetch_native(discovery, budget, *, fence: bool = False, project: Path | None = None) -> None:
    """Read, in parallel, every native fact the projections will use: the fleet and terminals already
    started, each abandoned or record-accounted abandoned Dispatch's claim, each observed Run owner and,
    before an end fence, rule S's Run-coordinator list under the call's deadline (`budget`). Last, R212's host
    verdicts for each query's own candidates, after every other read and before the end fence.
    Faults are kept for the queries, which raise them where they read."""
    def settled(key):
        future = discovery._memo.get(("first", *key))
        if future is None:
            return None
        try:
            return future.result()
        except (PodError, OSError):
            return None  # The queries raise it where they read it.
    # The reads that need only the terminal inventory start before the fleet is awaited.
    terminals = settled(("terminals",))
    if isinstance(terminals, dict) and isinstance(terminals.get("runtime"), str):
        discovery.start_owners()
        if fence:
            # The same key and walk as the projection's `coordinator`, so rule S never reads it after the end fence;
            # without a fence the projection still reads it only when rule S needs it.
            discovery.submit(("run-coordinators", terminals["runtime"]),
                             lambda: _run_coordinators(terminals["runtime"], budget.check))
    fleet = settled(("fleet",))
    if isinstance(fleet, dict) and isinstance(fleet.get("workers"), list) and fleet.get("complete") is True:
        for source in fleet["workers"]:
            if isinstance(source, dict) and (source.get("workerState") == "abandoned"
                                             or source.get("dispatchId") in discovery.abandoned):
                discovery.prefetch_claim(source, fleet["runtime"])
    for key, future in list(discovery._memo.items()):
        if key[:2] in (("first", "claim"), ("first", "owner"), ("first", "run-coordinators")):
            try:
                future.result()
            except (PodError, OSError):
                pass
    if project is not None:
        _host_exit_prefetch(discovery, budget, project)


def _after_terminals(discovery, budget, fence: bool) -> None:
    """Once the terminal inventory is known: start every Run owner a record pass has already named and, in a batch,
    R206 rule S's Run-coordinator list, beside the walk. No other read."""
    terminals = discovery.memo(("terminals",), _terminals, stage="first")  # The inventory read already started.
    runtime = terminals.get("runtime") if isinstance(terminals, dict) else None
    discovery.start_owners()
    if fence and isinstance(runtime, str):
        discovery.submit(("run-coordinators", runtime), lambda: _run_coordinators(runtime, budget.check),
                         stage="first")


def _fenced(discovery) -> dict:
    _FENCING.set(True)  # This task's context, and the reads it starts, are the fence's own.
    return _native_fence(discovery)


class _InputFault(Exception):
    def __init__(self, index: int, error: PodError):
        super().__init__(str(error))
        self.index, self.error = index, error


def _call(project: Path, queries: list[dict], budget, *, fence: bool = False) -> dict:
    """Answer queries over one shared discovery, in request order, within one deadline.

    Every query's inputs are proved first; then each record cohort is listed, the native fleet, the
    terminal inventory and each repository's worktrees are read in the pool while the queries read
    their records here, every query finishes its first pass, the one shared change recheck runs, and,
    with `fence`, a fresh terminal inventory is read last. Nothing is reused by a later call.
    """
    started = time.monotonic()
    discovery = _Discovery()
    discovery.clock = getattr(budget, "_clock", time.monotonic)
    discovery.started = discovery.clock()
    token = _SHARED.set(discovery)
    generators, results = [], [None] * len(queries)
    try:
        with read_deadline(budget):
            for path in dict.fromkeys(path for kwargs in queries  # Git identity for every scope at once, each path once;
                                      for path in (kwargs.get("repository") or project,  # each query still raises its own fault.
                                                   *(kwargs.get("known_repositories") or []))):
                resolved = Path(path).expanduser().resolve()
                if resolved.is_dir():
                    discovery.submit(("context", str(resolved)), lambda resolved=resolved: repository_context(resolved, fresh=True))
            roots, projects = {}, {}
            for index, kwargs in enumerate(queries):
                generator = _query(project, **kwargs)
                try:
                    _, root, resolved = next(generator)
                except PodError as exc:
                    if exc.code in INPUT_FAULTS:
                        raise _InputFault(index, exc) from None
                    results[index] = _unknown_result(project, kwargs, exc.code)
                    generators.append(None)
                    continue
                generators.append(generator)
                roots[str(root)], projects[str(resolved)] = root, resolved
            for root in roots.values():
                # Listed before any native read starts, so a record added while it runs is a change.
                if not (root.is_symlink() or any(parent.is_symlink() for parent in root.parents)):
                    try:
                        _shared(("cohort", str(root)), lambda root=root: _cohort(root, budget))
                    except (PodError, OSError):
                        pass  # The query raises the same outcome where it reads its cohort.
            discovery.submit(("fleet",), lambda: discovery.finish("fleet", _fleet()))
            discovery.submit(("terminals",), lambda: discovery.finish("terminals", _terminals()))
            discovery.submit(("terminals-known",), lambda: _after_terminals(discovery, budget, fence))
            for resolved in projects.values():
                discovery.submit(("places", str(resolved)), lambda resolved=resolved: _places(resolved))
            # Every query's record pass; then every native read a result uses, after which the end fence
            # starts; then, beside the fence, every query's native projection and the one shared recheck.
            fenced = None
            for step, stage in enumerate(("first", "first", "recheck")):
                discovery.stage = stage
                if step == 1:
                    _prefetch_native(discovery, budget, fence=fence, project=project)
                    if fence:
                        discovery.fence_started = True
                        fenced = discovery.submit(("native",), lambda: _fenced(discovery), stage="fence")
                for index, generator in enumerate(generators):
                    if generator is None or results[index] is not None:
                        continue
                    try:
                        next(generator)
                    except StopIteration as done:
                        results[index] = done.value
                    except PodError as exc:
                        if exc.code in INPUT_FAULTS:
                            raise _InputFault(index, exc) from None
                        results[index] = _unknown_result(project, queries[index], exc.code)
            results = [_bounded(result, kwargs) for result, kwargs in zip(results, queries)]
            fleet = discovery.done(("fleet",))
            answer = {"results": results, "discovery": {
                "elapsed_s": None, "native_workers": len(fleet["workers"]) if isinstance(fleet, dict) else None,
                "records": sum(len(discovery.done(("cohort", key)) or []) for key in roots),
                "fleet_read": discovery.fleet_read, "orca_reads": None,
                "host_exit": dict(discovery.host_exit_summary)}}
            if fenced is not None:
                answer["native_fence"] = fenced.result()
    finally:
        for generator in generators:
            if generator is not None:
                generator.close()
        _SHARED.reset(token)
        discovery.close()
    answer["discovery"]["orca_reads"] = discovery.orca_reads
    answer["discovery"]["elapsed_s"] = round(time.monotonic() - started, 3)
    _finish_censuses(answer["results"], budget, stable=answer.get("native_fence", {}).get("stable", True))
    return answer


def render(result: dict) -> None:
    from .term import clean
    print(f"Pod admission facts: {result['status']}; complete: {result['complete']}")
    print(f"Claims: {len(result['claims'])}; conflicts: {len(result['conflicts'])}; unknowns: {len(result['unknowns'])}")
    for row in result["unknowns"]:
        print(f"  {clean(str(row['record']))}: {clean(row['code'])}")
    print(result["next_action"])


def _timeout(timeout) -> float:
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
        raise PodError("admission_timeout", "Timeout must be greater than zero and at most 30 seconds")
    return float(timeout)


def query(project: Path, **kwargs) -> dict:
    """Read facts within one deadline; never grant authority to perform an effect.

    The single query is a one-query call over the same shared discovery as a batch.
    """
    timeout = _timeout(kwargs.pop("timeout", 10.0))
    try:
        return _call(project, [kwargs], Budget(timeout))["results"][0]
    except _InputFault as fault:
        raise fault.error from None


BATCH_REQUEST = "pod-admission-batch-request/v1"
MAX_BATCH_QUERIES = 512
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_KNOWN = 512
MAX_EFFECTS = 64
QUERY_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
QUERY_TEXT = ("worktree", "branch", "objective", "candidate", "target", "target_commit", "expect")


def _request_fault(message: str) -> PodError:
    return PodError("admission_batch_request", message)


def _text(value, name: str, *, path: bool = False) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096 or not _passable(value)
            or path and not value.startswith("/")):
        raise _request_fault(f"{name} must be {'an absolute path' if path else 'a nonempty string'} "
                             "of at most 4096 characters, UTF-8 without NUL")
    return value


def batch_request(data: bytes) -> dict:
    """Validate a whole batch request before any discovery; any fault refuses the whole batch."""
    if not isinstance(data, bytes) or not data:
        raise _request_fault("Supply one batch request object on stdin")
    if len(data) > MAX_REQUEST_BYTES:
        raise _request_fault("The batch request exceeds 1 MiB")
    def pairs(items):
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            raise _request_fault("A request object repeats a key")
        return dict(items)
    def constant(name):
        raise _request_fault("The batch request is not standard JSON")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError):
        raise _request_fault("The batch request is not UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise _request_fault("The batch request must be an object")
    if set(value) - {"schema", "known_repositories", "queries"}:
        raise _request_fault("The batch request has an unknown key")
    if value.get("schema") != BATCH_REQUEST:
        raise _request_fault("The batch request schema must be " + BATCH_REQUEST)
    known = value.get("known_repositories")
    if known is not None:
        if not isinstance(known, list) or len(known) > MAX_KNOWN:
            raise _request_fault(f"known_repositories must be a list of at most {MAX_KNOWN} paths")
        for path in known:
            _text(path, "Each known repository", path=True)
            if ".." in Path(path).parts:
                raise _request_fault("A known repository path must not contain a '..' component")
    queries = value.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_BATCH_QUERIES:
        raise _request_fault(f"queries must be a list of 1 to {MAX_BATCH_QUERIES} objects")
    seen, checked = set(), []
    for row in queries:
        if not isinstance(row, dict):
            raise _request_fault("Each query must be an object")
        if set(row) - {"id", "repository", "effects", *QUERY_TEXT}:
            raise _request_fault("A query has an unknown key")
        if not isinstance(row.get("id"), str) or not QUERY_ID.fullmatch(row["id"]):
            raise _request_fault("Each query id must be 1-128 characters from [A-Za-z0-9._:-]")
        if row["id"] in seen:
            raise _request_fault("Query id " + row["id"] + " is repeated")
        seen.add(row["id"])
        _text(row.get("repository"), "Query " + row["id"] + " repository", path=True)
        for name in QUERY_TEXT:
            if row.get(name) is not None:
                _text(row[name], "Query " + row["id"] + " " + name)
        effects = row.get("effects")
        if effects is not None:
            if not isinstance(effects, list) or len(effects) > MAX_EFFECTS:
                raise _request_fault(f"Query {row['id']} effects must be a list of at most {MAX_EFFECTS} strings")
            for effect in effects:
                _text(effect, "Query " + row["id"] + " effect")
        checked.append(row)
    return {"known_repositories": list(known or []), "queries": checked}


def batch(project: Path, request: dict, *, timeout=10.0) -> dict:
    """Answer a validated batch request over one shared discovery and one deadline (pod-admission-batch/v1)."""
    budget = Budget(_timeout(timeout))
    known = request["known_repositories"]
    queries = [{"repository": row["repository"], "objective": row.get("objective"), "candidate": row.get("candidate"),
                "worktree": row.get("worktree"), "branch": row.get("branch"), "target": row.get("target"),
                "target_commit": row.get("target_commit"), "effects": list(row.get("effects") or []),
                "expect": row.get("expect"), "known_repositories": list(known)} for row in request["queries"]]
    try:
        answer = _call(project, queries, budget, fence=True)
    except _InputFault as fault:
        raise PodError(fault.error.code, f"Query {request['queries'][fault.index]['id']}: {fault.error}") from None
    results, fence = answer["results"], answer["native_fence"]
    statuses = {result["status"] for result in results}
    response = {"schema": "pod-admission-batch/v1",
                "status": "unknown" if "unknown" in statuses or not fence["stable"]
                          else "blocked" if "blocked" in statuses else "observed",
                "complete": all(result["complete"] for result in results) and fence["stable"],
                "authorization": False,
                "responses": [{"id": row["id"], "result": result} for row, result in zip(request["queries"], results)],
                "native_fence": fence, "discovery": answer["discovery"]}
    if len(json.dumps(response, sort_keys=True, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise PodError("admission_batch_projection_limit", "The batch response would exceed 8 MiB; split the batch")
    _finish_censuses(results, budget, stable=fence["stable"])
    return response


def batch_command(args, project: Path) -> dict:
    """`pod admission --batch`: the whole request is refused before any discovery when any part is invalid."""
    import sys
    flags = [name for name in ("repository", "objective", "candidate", "worktree", "branch", "target",
                               "target_commit", "expect") if getattr(args, name) is not None]
    if flags or args.effect or args.known_repository:
        raise _request_fault("--batch reads every query from stdin; per-query flags are refused")
    if not args.json:
        raise _request_fault("--batch requires --json")
    timeout = _timeout(args.timeout)
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None or sys.stdin.isatty():
        raise _request_fault("Supply one batch request object on stdin")
    try:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    except OSError:
        raise _request_fault("Cannot read the batch request from stdin") from None
    return batch(project, batch_request(data), timeout=timeout)


# --------------------------------------------------------------------------- selected objective (R208)

SELECTED_SCHEMA = "pod-admission-selected/v1"
SELECTED_FLAGS = ("worktree", "branch", "target", "target_commit")
ROUTE_FIELDS = ("agent", "model", "effort", "context")


def _selected_fault(message: str) -> PodError:
    return PodError("admission_selected_request", message)


def _record_identity(directory: Path) -> str:
    """The selected record's change identity: its primary and journal bytes (history chunks are addressed by
    content, so the primary names them). Read without decoding; an absent journal is its own identity."""
    from .ledger import _record_exists
    parts = {}
    for name in ("context.json", "governor.json"):
        parts[name] = (hashlib.sha256(history._context_bytes(directory / name, history.MAINTENANCE_LIMIT)).hexdigest()
                       if _record_exists(directory / name) else None)
    return digest(parts)


def _route(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) for key in ROUTE_FIELDS}


def _selected_assurances(state: dict, proof: dict) -> list[dict]:
    """Every recorded assurance with its recorded scope; currency is read from the proof's own judgement."""
    label = proof.get("assurance") if isinstance(proof.get("assurance"), dict) else None
    gaps = {row.get("obligation"): row.get("gap") for row in (label or {}).get("assurance_unbound", [])
            if isinstance(row, dict) and row.get("obligation")}
    mismatch = proof.get("reason") == "candidate_mismatch"
    rows = []
    for ob in (state.get("checkpoint") or {}).get("obligations") or []:
        if not isinstance(ob, dict) or ob.get("kind") != "assurance":
            continue
        live = ob.get("state") not in ("withdrawn", "renewed")
        if not live:
            current, gap = None, None
        elif mismatch:
            current, gap = False, "candidate_mismatch"
        elif label is None:
            current, gap = None, None  # The proof could not judge it; the read's own unknown says why.
        else:
            gap = gaps.get(ob.get("id"))
            current = ob.get("state") == "satisfied" and gap is None
        evidence = [row for row in ob.get("evidence") or [] if isinstance(row, dict)]
        attempts = [{"admission": row["attempt"], "candidate": row.get("candidate"), "accepted_seq": None,
                     "source": "evidence"} for row in evidence if isinstance(row.get("attempt"), str)]
        for receipt in ob.get("receipts") or []:
            held = receipt.get("evidence") if isinstance(receipt, dict) else None
            if isinstance(held, dict) and isinstance(held.get("attempt"), str):
                attempts.append({"admission": held["attempt"], "candidate": held.get("candidate"),
                                 "accepted_seq": receipt.get("accepted_seq"), "source": "receipt"})
        reuse = ob.get("reuse") if isinstance(ob.get("reuse"), dict) else None
        scope = ob.get("scope") if isinstance(ob.get("scope"), dict) else {}
        rows.append({"id": ob.get("id"), "state": ob.get("state"), "audit_role": ob.get("audit_role"),
                     "provenance": ob.get("provenance"), "unit": ob.get("unit"), "parent": ob.get("parent"),
                     "definition": ob.get("definition"),
                     "scope": {"paths": scope.get("paths"), "surfaces": scope.get("surfaces")},
                     "evidence_current": current, "gap": gap,
                     "evidence_candidate": evidence[-1].get("candidate") if evidence else None,
                     "reuse": {key: reuse.get(key) for key in ("from", "to", "definition")} if reuse else None,
                     "accepted_attempts": attempts,
                     "findings": [{key: row.get(key) for key in ("finding", "severity", "triage", "correction",
                                                                 "resolved_seq")}
                                  for row in ob.get("findings") or [] if isinstance(row, dict)]})
    return rows


def _selected_attempts(project: Path, state: dict, candidate: str) -> list[dict]:
    """Every admission as recorded. Effective facts are the ones captured at bind time, never a live read."""
    ancestry = {}

    def in_candidate(commit):
        if commit not in ancestry:
            ancestry[commit] = gitio.is_ancestor(project, commit, candidate, resolve=lambda _root, value: value)
        return ancestry[commit]

    admissions = state.get("admissions") if isinstance(state.get("admissions"), dict) else {}
    ordered = sorted(admissions.items(), key=lambda item: (
        item[1].get("admitted_seq") if isinstance(item[1], dict) and type(item[1].get("admitted_seq")) is int else 0,
        item[0]))
    rows = []
    for key, row in ordered:
        if not isinstance(row, dict):
            continue
        unknowns = []
        binding = row.get("native_binding") if binding_valid(row.get("native_binding")) else None
        evidence = row.get("effective_evidence") if isinstance(row.get("effective_evidence"), dict) else {}
        if isinstance(evidence.get("effective"), dict):
            effective = {**_route(evidence["effective"]),
                         "source": "inherited" if evidence.get("inherited") is not None else "receipt"}
            if evidence.get("inherited") is not None:
                effective["inherited"] = evidence["inherited"]
            missing = [field for field in ("agent", "model", "effort") if effective[field] == "unknown"]
            if missing:
                unknowns.append({"code": "effective_unknown", "fact": "effective_route", "fields": missing})
        else:
            effective = {**_route(None), "source": "not_recorded"}
            unknowns.append({"code": "effective_not_recorded", "fact": "effective_route"})
        report = row.get("report") if isinstance(row.get("report"), dict) else None
        disposition = row.get("disposition") if isinstance(row.get("disposition"), dict) else None
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        attempt = {"admission": key, "role": row.get("role"), "serves": row.get("serves"), "state": row.get("state"),
                   "admitted_seq": row.get("admitted_seq"),
                   "native": None if binding is None else {
                       "run": binding["runId"], "task": binding["taskId"], "dispatch": binding["dispatchId"],
                       "worker": binding["workerId"], "terminal": binding["terminalHandle"],
                       "worktree_id": binding["worktreeId"]},
                   "worktree": row.get("worktree"), "candidate": row.get("candidate"),
                   "requested": _route(row.get("request")), "effective": effective,
                   "reuse_of": row.get("reuse_of"), "retry_of": row.get("retry_of"),
                   "report": None if report is None else {key: report.get(key)
                                                          for key in ("outcome", "status", "result_commit")},
                   "result_head": result.get("head"),
                   "disposition": None if disposition is None else {
                       "kind": disposition.get("kind"), "integrated_into": disposition.get("candidate"),
                       "validated": disposition.get("validated"), "seq": disposition.get("seq")},
                   "in_candidate": None}
        if row.get("role") == "implement":
            commit = (disposition.get("candidate") if disposition and disposition.get("kind") == "integrated_into"
                      else result.get("head"))
            if isinstance(commit, str) and commit:
                attempt["in_candidate"] = in_candidate(commit)
                if attempt["in_candidate"] is None:
                    unknowns.append({"code": "implementation_ancestry_unknown", "fact": "implementation_in_candidate",
                                     "commit": commit})
        attempt["unknowns"] = unknowns
        rows.append(attempt)
    return rows


def _selected_snapshot(state: dict, view: dict | None) -> dict:
    checkpoint = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
    ctx = (view or {}).get("ctx") or {}
    governance = checkpoint.get("governance") if isinstance(checkpoint.get("governance"), dict) else {}
    source = checkpoint.get("objective_source") if isinstance(checkpoint.get("objective_source"), dict) else None
    bound = None
    if view is not None and view.get("map") is not None:
        from .obligations import bound_governance_digest
        bound = bound_governance_digest(view["map"])
    current_base = ctx.get("governance_current")
    return {"record_revision": state.get("revision"), "seq": checkpoint.get("seq"),
            "checkpoint_revision": checkpoint.get("revision"), "plan_revision": checkpoint.get("plan_revision"),
            "recorded_candidate": checkpoint.get("candidate"), "pod_version": checkpoint.get("pod_version"),
            "source": None if source is None else {key: source.get(key) for key in
                ("repository", "number", "locator", "body_sha256", "amendments")},
            "policy": {"recorded": checkpoint.get("policy_revision"), "current": ctx.get("policy_revision"),
                       "current_equal": None if ctx.get("policy_revision") is None
                       else ctx["policy_revision"] == checkpoint.get("policy_revision")},
            "governance": {"base_ref": governance.get("base_ref"), "base": governance.get("base"),
                           "selection": governance.get("selection"), "bound_digest": bound,
                           "current_base": current_base,
                           "stale": None if current_base is None else current_base != governance.get("base")}}


def selected(project: Path, *, repository: str | None = None, objective: str | None = None,
             candidate: str | None = None, tree: str | None = None, expect: str | None = None,
             timeout: float = 10.0) -> dict:
    """R208: one objective's exact-candidate facts, with no record scan and no fleet discovery.

    Reads the repository identity, the candidate, the selected record and its own journal, and the existing proof
    path (whose only native reads are exact worker reads of still-bound admissions). It grants nothing.
    """
    if not objective or not candidate or not gitio.OBJECT_ID.fullmatch(candidate):
        raise PodError("admission_candidate", "Supply an objective and its full candidate object id together")
    if tree is not None and not gitio.OBJECT_ID.fullmatch(tree):
        raise _selected_fault("--tree must be a full object id")
    if expect is not None and not re.fullmatch(r"[0-9a-f]{64}", expect):
        raise _selected_fault("--expect must be a digest this read returned (64 lowercase hex characters)")
    budget = Budget(_timeout(timeout))
    project = Path(repository or project).expanduser().resolve()
    result = {"schema": SELECTED_SCHEMA, "status": "unknown", "authorization": False,
              "binding": {"repository": None, "repo_key": None, "path": str(project), "objective": objective,
                          "record": None, "candidate": candidate, "tree": None},
              "snapshot": None, "proof": None, "assurances": [], "attempts": [], "unknowns": [], "conflicts": []}
    unknowns, binding = result["unknowns"], result["binding"]
    reading = False  # True once the selected record exists and its read has started
    try:
        with read_deadline(budget):
            if not project.is_dir():
                raise PodError("admission_scope_unavailable", "The repository argument is not an existing directory")
            context = repository_context(project, fresh=True)
            if context["repo_key"] is None:
                raise PodError("admission_scope_unavailable", "The repository argument has no usable Git identity")
            binding.update(repository=context["repository"], repo_key=context["repo_key"])
            if gitio.resolve_commit(project, candidate) != candidate:
                raise PodError("admission_candidate", "The candidate is not a commit in this repository")
            read_tree = gitio.read(project, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                             candidate + "^{tree}"])
            observed_tree = read_tree.stdout.strip() if read_tree is not None and read_tree.returncode == 0 else ""
            if not gitio.OBJECT_ID.fullmatch(observed_tree):
                raise PodError("git_unavailable", "Git cannot read the candidate's tree")
            if tree is not None and tree != observed_tree:
                raise PodError("selected_tree_mismatch", "The candidate's tree differs from --tree")
            binding["tree"] = observed_tree
            root = state_root(project)
            if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
                raise PodError("unsafe_state", "State root is redirected")
            binding["record"] = name = record_name(project, context, objective)
            directory = root / name
            from .ledger import _record_exists
            if not _record_exists(directory / "context.json"):
                raise PodError("objective_unknown", "The selected objective has no record in this repository")
            reading = True
            before = _record_identity(directory)
            state = view = None
            try:
                proof, state, view = _proof_view(project, objective, candidate)
            except PodError as exc:
                if exc.code in (DEADLINE_SPENT, "objective_unknown"):
                    raise
                # The same outcome `pod admission` reports for a proof that raised.
                proof = {"status": "unknown", "candidate": candidate, "reason": exc.code}
                unknowns.append({"code": exc.code if exc.code in ("invalid_record", "state_unsupported")
                                 else "proof_unverified", "fact": "candidate_proof", "reason": exc.code})
            result["proof"] = proof
            if state is not None:
                recorded = (state.get("checkpoint") or {}).get("worktree")
                if (isinstance(recorded, dict) and isinstance(recorded.get("repo_key"), str)
                        and recorded["repo_key"] != context["repo_key"]):
                    raise PodError("selected_repository_mismatch", "The record names another repository")
                if proof["status"] == "unknown" and not unknowns:
                    unknowns.append({"code": "proof_unverified", "fact": "candidate_proof",
                                     "reason": proof.get("reason") or proof.get("settlement")})
                result["snapshot"] = _selected_snapshot(state, view)
                result["assurances"] = _selected_assurances(state, proof)
                result["attempts"] = _selected_attempts(project, state, candidate)
            after = _record_identity(directory)
            budget.check()  # after the last read: a deadline spent by any read is never ready
            if after != before:
                unknowns.append({"code": "record_changed", "fact": "selected_record"})
    except PodError as exc:
        if exc.code == DEADLINE_SPENT:
            unknowns.append({"code": DEADLINE_SPENT, "fact": "read_deadline"})
        elif not reading or exc.code in ("selected_repository_mismatch", "unsafe_state"):
            raise  # A binding refusal: no answer exists.
        elif exc.code == "objective_unknown":
            unknowns.append({"code": "record_changed", "fact": "selected_record"})
        else:
            unknowns.append({"code": "invalid_record", "fact": "selected_record", "reason": exc.code})
    except OSError:
        if not reading:
            raise PodError("admission_scope_unavailable", "The repository cannot be read") from None
        unknowns.append({"code": "invalid_record", "fact": "selected_record"})
    if result["proof"] is None:
        result["proof"] = {"status": "unknown", "candidate": candidate, "reason": "admission_incomplete"}
    answer = _selected_published(result, expect, candidate)
    # Publication is part of the deadline: an answer finished after it ran out is never ready.
    if budget.remaining() <= 0:
        if not any(row["code"] == DEADLINE_SPENT for row in result["unknowns"]):
            result["unknowns"].append({"code": DEADLINE_SPENT, "fact": "read_deadline"})
        answer = _selected_published(result, expect, candidate)
    return answer


def _selected_published(result: dict, expect: str | None, candidate: str) -> dict:
    """The finished answer, withheld when its final serialized form exceeds the projection bound."""
    answer = _selected_finish(result, expect)
    from .orca import MAX_OUTPUT
    if len(json.dumps(answer, sort_keys=True, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT:
        omitted = {"assurances": len(result["assurances"]), "attempts": len(result["attempts"])}
        result.update(snapshot=None, assurances=[], attempts=[],
                      proof={"status": "unknown", "candidate": candidate, "reason": "selected_projection_limit"},
                      unknowns=[{"code": "selected_projection_limit", "fact": "projection", "omitted": omitted},
                                *(row for row in result["unknowns"] if row["code"] == DEADLINE_SPENT)])
        answer = _selected_finish(result, expect)
    return answer


def selected_digest(answer: dict) -> str:
    """The digest of an answer's facts: everything but `digest`, `status`, `conflicts` and `next_action`. Status is
    derived from the proof and the unknowns, which it covers, so an `--expect` conflict never changes it."""
    return digest({key: value for key, value in answer.items()
                   if key not in ("digest", "status", "conflicts", "next_action")})


def _selected_finish(result: dict, expect: str | None) -> dict:
    answer = {**result, "unknowns": sorted(result["unknowns"], key=digest), "conflicts": []}
    withheld = any(row["code"] == "selected_projection_limit" for row in answer["unknowns"])
    answer["status"] = ("unknown" if answer["unknowns"] else "ready" if answer["proof"]["status"] == "ready"
                        else "blocked")
    answer["digest"] = None if withheld else selected_digest(answer)
    if expect is not None and answer["digest"] != expect:
        # R185's shape: a factual conflict, never a refusal; the answer is never ready.
        answer["conflicts"].append({"kind": "digest_changed", "expected": expect})
        if answer["status"] == "ready":
            answer["status"] = "blocked"
    answer["next_action"] = ("Join CE authority and repeat this read with --expect under effect serialization"
                             if answer["status"] == "ready" else
                             "Reconcile the named proof gaps or unknowns, then repeat the read")
    return answer


def selected_command(args, project: Path) -> dict:
    """`pod admission --selected`: every flag that needs repository or fleet discovery is refused before any read."""
    flags = [name for name in SELECTED_FLAGS if getattr(args, name) is not None]
    if args.batch or flags or args.effect or args.known_repository:
        raise _selected_fault("--selected reads one objective; --batch, --worktree, --branch, --target, "
                              "--target-commit, --effect and --known-repository are refused")
    if not args.json:
        raise _selected_fault("--selected requires --json")
    if not args.objective or not args.candidate:
        raise _selected_fault("--selected requires --objective and --candidate")
    return selected(project, repository=args.repository, objective=args.objective, candidate=args.candidate,
                    tree=args.tree, expect=args.expect, timeout=args.timeout)

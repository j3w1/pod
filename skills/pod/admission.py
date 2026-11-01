"""Bounded, read-only admission facts. The host serializes and authorizes effects."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import contextvars
import json
import hashlib
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .errors import PodError
from . import gitio, history, listing
from .github import repository_context
from .ledger import binding_valid, record_name, state_root
from .orca import DEADLINE_SPENT, WORKER_TERMINAL_STATES as TERMINAL_STATES, _read, _passable, complete_list, read_deadline
from .util import digest as _digest, public_decisions
from .window import Budget

MAX_TIMEOUT = 30.0
MAX_CLAIM_BYTES = 8 * 1024 * 1024
MAX_ORCA_READS = 8  # Orca CLI subprocesses one call runs at once.
POOL_WORKERS = 12
WORKER_LIST = ["orchestration", "worker-list", "--include-remote", "--json"]
# The single query's input faults; a batch names the query that raised one.
INPUT_FAULTS = ("admission_candidate", "admission_target", "admission_timeout", "admission_scope_unavailable",
                "repository_identity_unavailable", "git_unavailable")


def digest(value):
    try:
        return _digest(value)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PodError("invalid_record", "Admission facts cannot be encoded canonically") from None


# --------------------------------------------------------------------------- shared discovery

_SHARED: contextvars.ContextVar = contextvars.ContextVar("pod_admission_discovery", default=None)


class _Discovery:
    """The reads one admission call shares between its queries; nothing outlives the call.

    Each fact is read once per call and stage: `first` for discovery, `recheck` for the final change
    recheck, which therefore never reuses a discovery read. A read runs once, in the caller's thread
    (`memo`) or in the bounded pool (`submit`), and every query receives its outcome, value or
    exception, at the point where it uses the fact, so faults stay where the single query raised them.
    Pool tasks run in a copy of the submitting context, which carries the shared read deadline.
    At most `MAX_ORCA_READS` Orca CLI subprocesses run at once. `close` waits for every thread.
    """

    def __init__(self, *, partitioned: bool = True):
        self.partitioned = partitioned
        self.stage = "first"
        self.gate = threading.BoundedSemaphore(MAX_ORCA_READS)
        self.closed = False
        self.fleet_read = None
        self.fleet_fallback = None
        self.orca_reads = 0
        self.terminal_start = None
        self.fleet_start = None  # The read that closes fleet discovery, for the batch fence.
        self._lock = threading.Lock()
        self._memo: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="pod-admission")

    def memo(self, key, fn):
        full = (self.stage, *key)
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

    def submit(self, key, fn) -> Future:
        full = (self.stage, *key)
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

    def done(self, key):
        """A completed first-stage value, or None; never waits and never reads."""
        future = self._memo.get(("first", *key))
        if future is None or not future.done() or future.exception() is not None:
            return None
        return future.result()

    def read(self, argv):
        with self.gate:
            with self._lock:
                self.orca_reads += 1
            return _read(argv)

    def prefetch_claim(self, row, runtime):
        """Start an abandoned worker's current-claim read as soon as its inventory row is known."""
        key = row.get("dispatchId") if isinstance(row, dict) else None
        if isinstance(key, str) and key and isinstance(runtime, str):
            self.submit(("claim", key, runtime), lambda: _native_claim(row, runtime))

    def close(self):
        with self._lock:
            self.closed = True
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


def _gated_read(argv):
    discovery = _SHARED.get()
    return _read(argv) if discovery is None else discovery.read(argv)


def _gated(fn, *args):
    discovery = _SHARED.get()
    if discovery is None:
        return fn(*args)
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
    Inside a shared admission call the terminal-state partitions are read in parallel (`_fleet_partitioned`);
    when their union is not proved complete, the same call falls back to the sequential walk below, within the
    same deadline, and records which read it used.
    """
    discovery = _SHARED.get()
    if discovery is not None and discovery.partitioned:
        try:
            fleet = _fleet_partitioned(discovery)
        except PodError as exc:
            discovery.fleet_fallback = exc.code
        else:
            discovery.fleet_read = "partitioned" if fleet is not None else "bound"
            return fleet
    fleet = _fleet_sequential(discovery)
    if discovery is not None:
        discovery.fleet_read = "sequential" if fleet is not None else "bound"
    return fleet


def _fleet_sequential(discovery=None) -> dict | None:
    prefix = [*WORKER_LIST, "--limit", "100"]
    cursor, seen, workers, runtime = None, set(), {}, None
    while True:
        response = _gated_read(prefix + (["--cursor", cursor] if cursor else []))
        if discovery is not None and cursor is None:
            discovery.fleet_start = response  # On this read, its own first page closes discovery.
        result = response["result"]
        scope = result.get("scope")
        if isinstance(scope, dict) and scope.get("source") == "bound" and cursor is None:
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
        if not page["hasMore"]:
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


def _fleet_partition(state: str, discovery) -> dict:
    """Every page of one terminal-state partition, with the sequential walk's cursor and identity checks."""
    prefix = [*WORKER_LIST, "--limit", "100", "--terminal-state", state]
    cursor, seen, workers, runtime, total, totals = None, set(), {}, None, None, []
    while True:
        if discovery.closed:
            raise PodError(DEADLINE_SPENT, "The admission call has ended")
        response = discovery.read(prefix + (["--cursor", cursor] if cursor else []))
        result = response["result"]
        scope = result.get("scope")
        if isinstance(scope, dict) and scope.get("source") == "bound" and cursor is None:
            return {"state": state, "bound": True}
        if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
            raise _rejected("A partition page does not prove all-Run coverage")
        if runtime is not None and runtime != response["runtime"]:
            raise _rejected("Native runtime changed between partition pages")
        runtime = response["runtime"]
        rows, page = result.get("workers"), result.get("page")
        if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(page, dict)
                or type(page.get("hasMore")) is not bool or result.get("truncated", False) is not False
                or type(page.get("total")) is not int or page["total"] < 0):
            raise _rejected("A partition page is incomplete")
        if total is not None and page["total"] != total:
            raise _rejected("A partition total changed between its pages")
        total = page["total"]
        if "counts" in result:
            totals.append(sum(_counts(result["counts"]).values()))
        for row in rows:
            key = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in workers:
                raise _rejected("A partition repeats or omits a Dispatch identity")
            if row.get("terminalState") != state:
                raise _rejected("A Dispatch moved out of the partition it was listed in")
            workers[key] = row
            if row.get("workerState") == "abandoned":
                discovery.prefetch_claim(row, runtime)
        if not page["hasMore"]:
            if len(workers) != total:
                raise _rejected("A partition's rows differ from its total")
            return {"state": state, "bound": False, "runtime": runtime, "total": total,
                    "workers": list(workers.values()), "totals": totals}
        cursor = page.get("nextCursor")
        if not rows or not isinstance(cursor, str) or not cursor or cursor in seen:
            raise _rejected("A partition cursor is missing or repeated")
        seen.add(cursor)


def _fleet_partitioned(discovery) -> dict | None:
    """The all-Run inventory as the union of its six terminal-state partitions, read in parallel.

    Completeness argument. Each partition is walked to its end with the sequential walk's cursor, runtime,
    scope and identity checks, and it must hold exactly the rows its own `page.total` names, a total that
    never changes across its pages. After every partition is done, one unfiltered read gives the fleet's
    final `counts` and `page.total` T. The union is accepted only when every page of every partition and
    the final read report scope `all` with one runtime; each partition's total equals the final
    `counts[state]` (absent means 0); the final counts name no other state; no Dispatch appears in two
    partitions; no page's counts add up to more than T; and the union holds exactly T distinct Dispatches.

    Every Dispatch the partitions listed was read from the same runtime, and Orca keeps a Dispatch once
    it exists: a page whose counts exceed T would show one leaving during the read, and refuses. So the
    union is a subset of the T Dispatches that exist at the final read, and it has T members: it is all of
    them. A Dispatch that moved between partitions during the read appears in two (a duplicate), or in
    none, leaving its partition totals unequal to the final counts. A Dispatch created during the read
    raises T above the union. A new terminal state is named by the final counts. Each refuses, and the
    caller falls back to the sequential walk. Any partition's first page reporting a bound scope answers
    None, exactly as the sequential walk's first page does.

    The union is ordered by partition, in `TERMINAL_STATES` order, then in Orca's page order, so the
    same Orca state always gives the same order.
    """
    futures = [discovery.submit(("partition", state), lambda state=state: _fleet_partition(state, discovery))
               for state in TERMINAL_STATES]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except PodError as exc:
            outcomes.append(exc)
    if any(isinstance(row, dict) and row["bound"] for row in outcomes):
        return None
    for row in outcomes:
        if isinstance(row, PodError):
            raise row  # The first in partition order: deterministic, whichever thread failed first.
    runtimes = {row["runtime"] for row in outcomes}
    if len(runtimes) != 1:
        raise _rejected("Partitions report different native runtimes")
    final = discovery.read([*WORKER_LIST, "--limit", "1"])
    discovery.fleet_start = final
    result, runtime = final["result"], final["runtime"]
    scope, page = result.get("scope"), result.get("page")
    if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
        raise _rejected("The final count read does not prove all-Run coverage")
    if runtimes != {runtime}:
        raise _rejected("The final count read reports a different native runtime")
    counts = _counts(result.get("counts"))
    if not isinstance(page, dict) or type(page.get("total")) is not int or page["total"] < 0:
        raise _rejected("The final count read has no total")
    total = page["total"]
    workers, seen = [], set()
    for row in outcomes:
        if row["total"] != counts.get(row["state"], 0):
            raise _rejected("A partition total differs from the final counts")
        if any(observed > total for observed in row["totals"]):
            raise _rejected("A Dispatch left the inventory during the read")
        for worker in row["workers"]:
            if worker["dispatchId"] in seen:
                raise _rejected("A Dispatch appears in two partitions")
            seen.add(worker["dispatchId"])
            workers.append(worker)
    if len(workers) != total:
        raise _rejected("The partitions do not hold every Dispatch")
    return {"runtime": runtime, "workers": workers, "complete": True}


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


def _fleet_side(response) -> dict:
    """One side of the worker-inventory fence: the unfiltered all-Run read's runtime, total and counts."""
    runtime = response.get("runtime") if isinstance(response, dict) else None
    runtime = runtime if isinstance(runtime, str) and _passable(runtime) else None
    try:
        if isinstance(response, BaseException):
            raise response
        if response is None:
            raise PodError("native_fence_unavailable", "Discovery made no fleet read to fence")
        result = response["result"]
        scope, page, counts = result.get("scope"), result.get("page"), result.get("counts")
        if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
            raise PodError("native_inventory_scope", "The fleet read does not prove all-Run coverage")
        if (not isinstance(page, dict) or type(page.get("total")) is not int or page["total"] < 0
                or not isinstance(counts, dict) or any(not isinstance(key, str) or type(value) is not int or value < 0
                                                       for key, value in counts.items())):
            raise PodError("native_inventory_incomplete", "The fleet read has no total and counts")
    except PodError as exc:
        return {"runtime": runtime, "complete": False, "code": exc.code}
    except (KeyError, TypeError, AttributeError):
        return {"runtime": runtime, "complete": False, "code": "native_inventory_incomplete"}
    return {"runtime": runtime, "complete": True, "total": page["total"], "counts": dict(counts)}


def _native_fence(discovery) -> dict:
    """The batch fence: discovery's terminal and fleet reads against fresh end reads, made in parallel
    after every projection and the shared recheck, as the call's last native reads. Never cached."""
    discovery.stage = "fence"
    start = discovery.terminal_start
    if start is None:
        future = discovery._memo.get(("first", "terminals"))
        start = future.exception() if future is not None and future.done() else None
    reads = {"end": discovery.submit(("fence", "terminals"), lambda: complete_list("terminal", discovery.read)),
             "fleet_end": discovery.submit(("fence", "fleet"), lambda: discovery.read([*WORKER_LIST, "--limit", "1"]))}
    observed = {}
    for name, future in reads.items():
        try:
            observed[name] = future.result()
        except PodError as exc:
            observed[name] = exc
    fence = {"start": _fence_side(start, rows=False), "end": _fence_side(observed["end"], rows=True),
             "fleet_start": _fleet_side(discovery.fleet_start), "fleet_end": _fleet_side(observed["fleet_end"])}
    sides = list(fence.values())
    fence["stable"] = bool(all(side["complete"] for side in sides)
                           and len({side["runtime"] for side in sides}) == 1
                           and fence["start"]["sha256"] == fence["end"]["sha256"]
                           and all(fence["fleet_start"][key] == fence["fleet_end"][key] for key in ("total", "counts")))
    return fence


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
    response = _gated_read(["orchestration", "worker-show", "--dispatch", source["dispatchId"], "--json"])
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
        facts = _shared(("facts", str(path), False), lambda: reader(path, include_admissions=False))
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


def _admissions(raw: dict, directory: Path) -> dict:
    if "_facts_generation" not in raw or _SHARED.get() is None:
        return _admission_rows(raw, directory)
    def load():
        working = dict(raw)
        return working, _admission_rows(working, directory)
    # One validation and archive expansion per record generation and call; each query gets its own copy.
    current, rows = _shared(("admissions", str(directory), digest(raw["_facts_generation"])), load)
    raw.clear()
    raw.update(current)
    return dict(rows)


def _admission_rows(raw: dict, directory: Path) -> dict:
    from .installer import STATE_SCHEMAS
    from .ledger import _CONTEXT_FIELDS, _SUCCESSION_FIELDS
    projected = "_facts_generation" in raw
    if projected:
        path = directory / "context.json"
        facts = _shared(("facts", str(path), True), lambda: history.load_admission_facts(path, include_admissions=True))
        current = _facts_context(facts)
        closed_empty = (facts.get("phase") == "closed" and facts.get("admissions") == {}
                        and facts.get("errors") == [{"code": "objective_placement_unknown"}])
        if (facts.get("complete") is not True and not closed_empty) or not isinstance(facts.get("admissions"), dict):
            raise PodError("admission_facts_incomplete", "Current claim inventory is incomplete")
        if current["_facts_generation"] != raw["_facts_generation"]:
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
    journal, identity, _ = _shared(("journal", str(path)), lambda: _journal_read(path))
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
    without a decode; anything else is read again in full, in the recheck stage."""
    discovery = _SHARED.get()
    facts = discovery.done(("facts", str(path), False)) if discovery is not None else None
    if isinstance(facts, dict) and isinstance(facts.get("generation"), dict):
        data = _shared(("after-bytes", str(path)), lambda: history._context_bytes(path, history.MAINTENANCE_LIMIT))
        if hashlib.sha256(data).hexdigest() == facts["generation"].get("sha256"):
            return digest(facts["generation"])
    return _primary(path)[1]


def _after_journal(path: Path) -> str:
    """The recheck identity of a Governor journal, compared the same way before any decode."""
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


def _placement_matches(binding: dict, shown: dict) -> bool:
    return (binding_valid(binding) and all(binding.get(key) == shown.get(field) for key, field in
        (("dispatchId", "dispatch"), ("runId", "run"), ("taskId", "task"),
         ("workerId", "worker"), ("terminalHandle", "terminal")))
        and listing._location(binding["worktreeId"]) == shown["worktree"])


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
    checkpoint = raw["checkpoint"]
    phase = _phase(raw)
    return digest({"objective": listing._name(raw), "phase": phase,
                   "worktree": checkpoint.get("worktree") if phase == "open" else None,
                   "runs": checkpoint.get("native_refs"),
                   "admissions": {key: {field: row.get(field) for field in
                       ("state", "role", "native_binding", "runtime", "worktree", "request_uuid", "run_id", "task_id", "boundary")}
                       for key, row in rows.items()}})


def _effect_identity(journal: dict, *, open_objective: bool) -> str:
    return digest({"actions": [row for row in journal["actions"]
                              if row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")],
                   "branches": {key: unit.get("branch") for key, unit in journal["units"].items()}
                               if open_objective else {}})


def _proof(project: Path, objective: str, candidate: str) -> dict:
    from .ledger import kernel_view, read
    state = read(project, objective)
    if state is None:
        raise PodError("objective_unknown", "The selected objective has no record")
    checkpoint = state.get("checkpoint") or {}
    if checkpoint.get("candidate") != candidate:
        return {"status": "blocked", "candidate": candidate, "reason": "candidate_mismatch"}
    view = kernel_view(project, objective, state=state, candidate=candidate)
    from .obligations import _persisted_rows, TERMINAL
    gaps, unavailable = [], []
    if view["map"] is None:
        gaps.append("map_missing")
    else:
        try:
            _persisted_rows(view["map"], view["ctx"], unverifiable=unavailable)
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
    return {"status": "unknown" if view["settlement"] == "unverified" or unavailable else "blocked" if gaps else "ready",
            "candidate": candidate, "settlement": view["settlement"],
            "purpose": "candidate_proof", "gaps": gaps, "assurance": view["label"],
            "objective_unfinished": [row["id"] for row in (view["map"] or {}).get("obligations", [])
                                     if row["state"] not in TERMINAL],
            "objective_complete": bool(view["map"]) and all(row["state"] in TERMINAL
                                      for row in view["map"]["obligations"])}


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
    known = [context, *(_context(Path(path).expanduser().resolve()) for path in known_repositories or [])]
    root = state_root(project)
    yield "prepared", root, project
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
    owners, owner_reads = {}, {}
    placements_by_dispatch = {}
    scope_records_read, attribution_gaps = set(), []
    abandoned_dispatches = set()
    physical_paths = {}

    def physical(path):
        if path not in physical_paths:
            # Each path is resolved once per call, whichever query asks first.
            physical_paths[path] = _shared(("physical", str(path)), lambda: str(Path(path).resolve()))
        return physical_paths[path]

    def overlap(left, right):
        try:
            left, right = physical(left), physical(right)
            return _within(left, right) or _within(right, left)
        except (OSError, ValueError):
            return True  # An inaccessible path cannot establish independence.

    def relevant(facts, selection, key, fact=None):
        return _relevant(facts, selection, key, overlap, fact)

    def workspace(path, places):
        if not path:
            return None, False
        def locate():
            inside = physical(path)
            roots = [root for root in places if _within(inside, physical(root))]
            return (max(roots, key=len) if roots else None,
                    bool(roots) or any(overlap(path, root) for root in places))
        # The same path in the same repository's worktree registry is located once per call.
        return _shared(("workspace", str(project), str(path)), locate)
    from .orca import _DEADLINE
    budget = _DEADLINE.get() or Budget(float(timeout))

    def owner_claims():
        """The ownership claims whose current Run owner this query observes."""
        selection = {"worktree": str(Path(worktree).resolve()) if worktree else None,
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
            repair = {"command": ["orca", "orchestration", "worker-list", "--include-remote", "--limit", "100", "--json"],
                      "condition": "Reconcile the failed native inventory fact; incomplete coverage never proves absence"}
        elif record is None and code == "discovery_budget":
            repair = {"action": "inspect_read_budget", "condition": "Identify and reconcile the source read that exhausted CE's deadline before retrying"}
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
        result.setdefault("unrelated_unknowns", []).append({
            "record": record, "code": code, "status": "unavailable", "fact": "record_readable",
            "scope": {"record": record},
            "relevance": "an unreadable record holds only its own resources; current native writers are inventoried "
                         "independently",
            "reconciliation": {"action": "inspect_record", "record": record,
                               "condition": "Repair or remove the unreadable record; it holds only its own resources"}})

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
                unknown(directory.name, "effects_unknown", objective=name, fact="external_effect_identity",
                        fact_scope=fact_scope, reconciliation={"action": "reconcile_governor",
                        "record": row["record_id"], "condition": "Read back the admitted action and establish its actual downstream effects"})
            elif relative and not (source or {}).get("repo_key"):
                unknown(directory.name, "effect_attribution_unknown", objective=name, fact="effect_repository",
                        fact_scope=fact_scope, reconciliation={"action": "establish_effect_repository",
                        "record": row["record_id"], "condition": "Establish the repository namespace of this admitted intrinsic effect"})

    consulted, proof_outcome = [], None
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
                reading = False  # The record reads; any later fault is about its own facts.
                try:
                    name = _membership(project, context, record, raw, known)
                except PodError as exc:
                    if exc.code == "record_attribution_unknown" and _phase(raw) == "closed":
                        _admissions(raw, directory)  # Supported, structurally closed, and no hidden admission references.
                        if raw.get("admissions") != {}:
                            raise
                        journal, journal_id = _journal(directory / "governor.json")
                        if not any(row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN") for row in journal["actions"]):
                            attribution_gaps.append((record, directory, identity, journal_id))
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
                    continue
                rows = _admissions(raw, directory)
                phases[record] = _phase(raw) if raw["checkpoint"]["schema"] == "pod-checkpoint/v3" else "noncurrent"
                place = raw["checkpoint"].get("worktree") or {}
                for row in rows.values():
                    placement = _placement(row)
                    binding = row.get("native_binding") or {}
                    if row.get("abandoned_accounting") and binding.get("dispatchId"):
                        abandoned_dispatches.add(binding["dispatchId"])
                    if (placement.get("path") == listing._location(binding.get("worktreeId"))
                            and "branch" in placement and binding.get("dispatchId")):
                        placements_by_dispatch[binding["dispatchId"]] = (binding, placement)
                placements = [_placement(row) for row in rows.values()]
                paths = [place.get("path"), *(listing._location((row.get("native_binding") or {}).get("worktreeId"))
                          or listing._location(row.get("worktree")) or placement.get("path")
                          for row, placement in zip(rows.values(), placements))]
                record_scopes[record] = {**scope, "worktrees": sorted({path for path in paths if path}),
                                        "branches": [place.get("branch"), *(p.get("branch") for p in placements)],
                                        "locations_complete": bool(paths) and all(paths)}
                journal, journal_id = _journal(directory / "governor.json")
                current_id = _claim_identity(raw, rows)
                effect_id = _effect_identity(journal, open_objective=_phase(raw) == "open")
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
                open_objective = _phase(raw) == "open"
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
                for key, row in rows.items():
                    binding = row.get("native_binding")
                    if binding is not None:
                        if not binding_valid(binding):
                            raise PodError("state_unsupported", "Native claim identity is malformed")
                        runs.setdefault(binding["runId"], []).append((record, name, key,
                            {"native_binding": binding, "runtime": row.get("runtime"), "boundary": row.get("boundary")}))
                        run_owners.setdefault(binding["runId"], []).append((record, name, row.get("runtime")))
                    elif row["state"] not in ("closed", "deferred"):
                        claim({"objective": name, "record": record,
                            "kind": "writer" if row["role"] == "implement" else "assignment",
                            "admission": key, "worktree": listing._location(row.get("worktree"))
                                or (_placement(row)).get("path"),
                            "branch": (_placement(row)).get("branch"),
                            "locations_complete": bool((_placement(row)).get("path")),
                            "liveness": "unknown", "boundary": row.get("boundary")})
                        unknown(record, "assignment_unbound", objective=name, admission=key)
                effect_claims(directory, raw, journal, source or context)
            except PodError as exc:
                # Only a record that cannot be read at all is named apart; a read deadline is no record's fault.
                (unavailable if reading and exc.code != "discovery_budget" else unknown)(record, exc.code)
            except OSError:
                (unavailable if reading else unknown)(record, "unreadable")
        discovery = _SHARED.get()
        ready = discovery.done(("terminals",)) if discovery is not None else None
        if isinstance(ready, dict) and isinstance(ready.get("runtime"), str):
            # Start this query's Run owner reads now; the projection below uses the same shared reads.
            for ownership in owner_claims():
                for run in sorted({ref["runId"] for ref in ownership["recorded_native_refs"]}):
                    discovery.submit(("owner", run, ready["runtime"]), lambda run=run: _gated(
                        _owner_observation, run, ready, ready["runtime"]))
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
        for ownership in owner_claims():
            observations = []
            for run in sorted({ref["runId"] for ref in ownership["recorded_native_refs"]}):
                if run not in owner_reads:
                    try:
                        owner_reads[run] = _shared(("owner", run, terminals["runtime"]), lambda: _gated(
                            _owner_observation, run, terminals, terminals["runtime"]))
                    except PodError as exc:
                        owner_reads[run] = {"run": run, "status": "unknown", "code": exc.code}
                observations.append(owner_reads[run])
            ownership["owner_observations"] = observations
            matches = bool(observations) and all(row.get("terminal") == ownership["recorded_owner"]
                and row.get("worktree") == ownership.get("worktree") and row.get("live") is True for row in observations)
            ownership["owner_identity_matches"] = matches
            owners[ownership["record"]] = matches
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
                    continue
                shown = _worker_fact(source)
                path = shown["worktree"]
                run = shown["run"]
                if shown["liveness"] == "exited" and source.get("workerState") != "abandoned" and shown["dispatch"] not in abandoned_dispatches:
                    workers[shown["dispatch"]] = shown
                    continue
                registered, in_repository = workspace(path, places)
                branch_fact, branch_known = places.get(registered), registered is not None
                binding, placement = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                if not branch_known and _placement_matches(binding, shown):
                    branch_fact, branch_known = placement["branch"], True
                if path and not branch_known:
                    # Native work in this repository may have been admitted by a
                    # different repository's objective. Inspect only that actual
                    # possible conflict, never every foreign admission history.
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
                            unknown(directory.name, exc.code, fact_scope={**scope, "worktree": path},
                                    fact="worker_placement")
                            continue
                        for candidate_row in candidate_rows.values():
                            binding = candidate_row.get("native_binding") or {}
                            placement = _placement(candidate_row)
                            if placement.get("path") == listing._location(binding.get("worktreeId")) and "branch" in placement:
                                placements_by_dispatch[binding.get("dispatchId")] = (binding, placement)
                        bound, placed = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                        if _placement_matches(bound, shown):
                            branch_fact, branch_known = placed["branch"], True
                            break
                native_effects = []
                scope_binding, scope_placement = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                actor_repo = context["repo_key"] if registered is not None else (
                    scope_placement.get("repo_key") if _placement_matches(scope_binding, shown) else None)
                actor_scope = {"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                               "locations_complete": bool(path) and branch_known}
                if (source.get("workerState") == "abandoned" or shown["dispatch"] in abandoned_dispatches) and (effects or relevant(actor_scope,
                        {"worktree": str(Path(worktree).resolve()) if worktree else None,
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
                                        reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                        "condition": "Reconcile the W1-reported pending native effects and their targets"})
                            if native_effects and target:
                                from .abandoned import _effect_conflicts
                                target_branch = (target.removeprefix("refs/remotes/").partition("/")[2]
                                                 if target.startswith("refs/remotes/") else target.removeprefix("refs/heads/"))
                                if _effect_conflicts({"repo_key": actor_repo, "effects": native_effects},
                                                     {"repo_key": context["repo_key"], "branch": target_branch}):
                                    unknown(None, "native_effect_target_unknown", dispatch=shown["dispatch"],
                                        fact="native_effect_target", fact_scope={"repo_key": context["repo_key"], "target": target},
                                        reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                       "condition": "Reconcile the shared-reader native effect that may update this target"})
                    except PodError as exc:
                        unknown(None, exc.code, dispatch=shown["dispatch"],
                                fact_scope={"effects": None} if effects else actor_scope,
                                fact="current_native_claim", reconciliation={"action": "read_shared_current_claim",
                                "condition": "Load the W1-owned claim reader and read the exact current worker observation"})
                if requested_run is None and run not in runs and not in_repository:
                    if path is None and shown["liveness"] != "exited":
                        owners = run_repositories.get(run, set())
                        unknown(None, "native_location_unavailable", dispatch=shown["dispatch"],
                                fact_scope={"repo_keys": sorted(key for key in owners if key), "run": run},
                                reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                "condition": "Resolve the native resource's repository and worktree ownership"})
                    continue
                workers[shown["dispatch"]] = shown
                if shown["liveness"] == "exited" and not native_effects:
                    continue
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
                            fact="process_liveness", fact_scope={"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                                "locations_complete": bool(path) and branch_known},
                            reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
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
        if objective:
            # The proof is read before the change recheck, so a change during its read is still found.
            try:
                budget.check()
                proof_outcome = (_proof(project, objective, candidate), None)
            except PodError as exc:
                proof_outcome = ({"status": "unknown", "candidate": candidate, "reason": exc.code}, exc.code)
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
                unknown(None, "effect_policy_changed", fact="effect_mapping")
        for directory, generation in consulted:
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
                if (selected or _claim_identity(after_raw, _admissions(after_raw, directory)) != current_id
                        or _effect_identity(after_journal, open_objective=_phase(after_raw) == "open") != effect_id):
                    unknown(directory.name, "record_changed")
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
    except PodError as exc:
        unknown(None, exc.code)
    except OSError:
        unknown(None, "inventory_unreadable")
    if objective and result["proof"] is None:
        result["proof"] = {"status": "unknown", "candidate": candidate, "reason": "admission_incomplete"}
    request = {"worktree": str(Path(worktree).resolve()) if worktree else None,
               "branch": branch, "target": target, "effects": effects or []}
    result["scope"]["selection"] = {**request, "target": requested_target}
    all_unknowns = result["unknowns"]
    unread = [{"record": row["record"], "code": row["code"]} for row in result.get("unrelated_unknowns", [])
              if row.get("status") == "unavailable"]
    result["repository_complete"] = not all_unknowns and not unread
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
        if worktree and claim.get("worktree") and overlap(claim["worktree"], str(Path(worktree).resolve())):
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
    result["status"] = "unknown" if not result["complete"] else "blocked" if (result["conflicts"] or
        result["proof"] is not None and result["proof"]["status"] != "ready") else "observed"
    return public_decisions(result)


def _empty_scan():
    from contextlib import nullcontext
    return nullcontext(())


def _context(path: Path) -> dict:
    return _shared(("context", str(path)), lambda: repository_context(path, fresh=True))


def _cohort(root: Path, budget) -> list:
    try:
        with os.scandir(root) as entries:
            cohort = []
            for entry in entries:
                budget.check()
                if not entry.name.startswith("."):
                    cohort.append(entry)
            return cohort
    except FileNotFoundError:
        return []


def _after_cohort(root: Path, budget) -> dict:
    with os.scandir(root) if root.exists() else _empty_scan() as entries:
        after = {}
        for entry in entries:
            budget.check()
            if not entry.name.startswith("."):
                after[entry.name] = entry
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
                      proof={"status": "unknown", "reason": "admission_projection_limit"} if kwargs.get("objective") else None,
                      unknowns=[row for row in result["unknowns"] if row["record"] is not None
                                and not row["record"].startswith("run:") and "admission" not in row][:4096])
        result["unknowns"].append({"record": None, "code": "admission_projection_limit", "scope": result["scope"],
                                   "omitted": omitted})
    return result


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
    token = _SHARED.set(discovery)
    generators, results = [], [None] * len(queries)
    try:
        with read_deadline(budget):
            for kwargs in queries:  # Git identity for every scope at once; each query still raises its own fault.
                for path in (kwargs.get("repository") or project, *(kwargs.get("known_repositories") or [])):
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
            discovery.submit(("fleet",), _fleet)
            discovery.submit(("terminals",), _terminals)
            for resolved in projects.values():
                discovery.submit(("places", str(resolved)), lambda resolved=resolved: _places(resolved))
            # Every query's record pass, then every query's native projection, then the one shared recheck.
            for stage in ("first", "first", "recheck"):
                discovery.stage = stage
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
                "fleet_read": discovery.fleet_read, "orca_reads": None}}
            if fence:
                answer["native_fence"] = _native_fence(discovery)
    finally:
        for generator in generators:
            if generator is not None:
                generator.close()
        _SHARED.reset(token)
        discovery.close()
    answer["discovery"]["orca_reads"] = discovery.orca_reads
    answer["discovery"]["elapsed_s"] = round(time.monotonic() - started, 3)
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

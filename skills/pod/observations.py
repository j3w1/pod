"""Current and previous public-source observation snapshots, outside every authority.

Observations are dated facts from fixed public pages. They never write preferences, the
registry or the bundle, never start workers, and are read offline: only ``refresh`` uses
the network. Rows keep exact source names; routes are matched at read time.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterator

from . import catalog, sources
from .errors import PodError
from .util import atomic_json, explicit_home, native_home

SCHEMA = "pod-observations/v1"
REFRESH_SCHEMA = "pod-observations-refresh/v1"
BUNDLED_PATH = Path(__file__).with_name("observations.json")
MAX_SNAPSHOT = 1024 * 1024
MAX_BUNDLED = 64 * 1024
MAX_STATUS = 64 * 1024
FRESH_S = 24 * 3600          # automatic refresh is due from this age
STALE_S = 7 * 24 * 3600      # display stale from this age
FAILED_RESTRAINT_S = 6 * 3600  # no automatic retry this soon after a failed or refused attempt
BRIEF_RESTRAINT_S = 600      # after a cancelled attempt, or one still (or left) marked running
COLLAPSE_FLOOR = 8           # below this many rows a drop is too small to call a collapse
MAX_DIFF_NAMES = 20
MAX_DIAGNOSTICS = 10
OUTCOMES = ("running", "promoted", "checked", "failed", "refused", "cancelled", "superseded")
_SOURCE_FIELDS = {"url", "attribution", "required", "status", "retrieved_at", "published_at",
                  "methodology", "rows", "diagnostics"}
_METRIC_RANGES = {"intelligence": 100, "usd_per_task": 100000, "output_tps": 100000,
                  "first_response_s": 100000, "total_response_s": 100000,
                  "context_tokens": 100_000_000}
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


# ---------------------------------------------------------------- time and paths


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise PodError("invalid_time", "Observation time needs a timezone")
    return now.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def cache_root(project: Path | None = None) -> Path:
    """``${XDG_CACHE_HOME:-~/.cache}/pod/models``, or ``$POD_CACHE_HOME/models`` for validation."""
    override = explicit_home("POD_CACHE_HOME")
    if override is not None:
        root = override / "models"
    else:
        root = native_home("XDG_CACHE_HOME", default=Path.home() / ".cache",
                           project=project).resolve(strict=False) / "pod" / "models"
    if root.resolve(strict=False).is_relative_to(Path(__file__).resolve().parent):
        raise PodError("unsafe_cache", "Observation cache cannot live inside the installed bundle")
    return root


def _make_root(root: Path) -> None:
    if root.is_symlink():
        raise PodError("unsafe_cache", "Observation cache directory is redirected")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextmanager
def _lock(root: Path, *, shared: bool = False) -> Iterator[None]:
    """Short cache lock for file reads and swaps only; never held across network work."""
    path = root / ".lock"
    if shared:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            yield
            return
        except OSError as exc:
            raise PodError("unsafe_cache", "Observation cache lock cannot be opened") from exc
    else:
        _make_root(root)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PodError("observations_busy", "Another Pod process is updating observations")
                time.sleep(.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- validation


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PodError("invalid_observations", "Observation snapshot has a duplicate key")
        result[key] = value
    return result


def _invalid(message: str) -> PodError:
    return PodError("invalid_observations", message)


def _exact(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise _invalid(f"{name} has missing or unsupported fields")
    return value


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or sources.clean(value, limit) != value:
        raise _invalid(f"{name} must be bounded printable text")
    return value


def _utc(value: object, name: str) -> str:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise _invalid(f"{name} must be a UTC timestamp")
    try:
        _moment(value)
    except ValueError as exc:
        raise _invalid(f"{name} is not a real time") from exc
    return value


def _metric(key: str, value: object) -> None:
    if value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= _METRIC_RANGES[key]:
        raise _invalid(f"Metric {key} must be a finite number in range or null")
    if key == "context_tokens" and type(value) is not int:
        raise _invalid("Context tokens must be a whole number")


def _row(row: object) -> dict:
    _exact(row, {"name", "creator", "qualifiers", "metrics"}, "observation row")
    _text(row["name"], "row name", sources.MAX_NAME)
    if row["creator"] not in sources.CREATORS:
        raise _invalid("Rows are limited to Anthropic and OpenAI")
    qualifiers = row["qualifiers"]
    if (not isinstance(qualifiers, list) or len(qualifiers) != len(set(qualifiers))
            or any(item not in sources.QUALIFIERS for item in qualifiers)):
        raise _invalid("Row qualifiers are unsupported")
    metrics = _exact(row["metrics"], set(_METRIC_RANGES), "row metrics")
    for key, value in metrics.items():
        _metric(key, value)
    return row


def _block(block: object) -> dict:
    _exact(block, _SOURCE_FIELDS, "source block")
    _text(block["url"], "source URL", 300)
    if not block["url"].startswith("https://"):
        raise _invalid("Source URL must use HTTPS")
    _text(block["attribution"], "attribution", 80)
    if type(block["required"]) is not bool or block["status"] not in sources.STATUSES:
        raise _invalid("Source block has an invalid status")
    for key in ("retrieved_at", "published_at"):
        if block[key] is not None:
            _utc(block[key], key)
    if block["methodology"] is not None:
        _text(block["methodology"], "methodology", 120)
    rows = block["rows"]
    if not isinstance(rows, list) or len(rows) > sources.MAX_SCOPED_ROWS:
        raise _invalid("Source rows must be a bounded list")
    names = [_row(row)["name"] for row in rows]
    if len(names) != len(set(names)):
        raise _invalid("Source has duplicate rows for one exact name")
    if rows and block["retrieved_at"] is None:
        raise _invalid("Source rows need their retrieval time")
    diagnostics = block["diagnostics"]
    if not isinstance(diagnostics, list) or len(diagnostics) > MAX_DIAGNOSTICS:
        raise _invalid("Source diagnostics must be a bounded list")
    for item in diagnostics:
        _text(item, "diagnostic", sources.MAX_DIAGNOSTIC)
    return block


def validate(document: object) -> dict:
    """Strict ``pod-observations/v1`` validation; raises ``invalid_observations``."""
    _exact(document, {"schema", "generation", "created_at", "sources"}, "observation snapshot")
    if document["schema"] != SCHEMA:
        raise _invalid("Unsupported observation schema")
    generation = document["generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"0|[1-9]\d{0,17}", generation):
        raise _invalid("Generation must be a decimal counter")
    _utc(document["created_at"], "created_at")
    blocks = document["sources"]
    if not isinstance(blocks, dict) or len(blocks) > 8:
        raise _invalid("Sources must be a small object")
    for source_id, block in blocks.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", source_id):
            raise _invalid("Source id is invalid")
        _block(block)
    return document


def _bytes(path: Path, limit: int) -> bytes | None:
    """A regular file's bytes, or None when absent; PodError when redirected, special or too big.

    The file is opened without following a link or waiting on a FIFO, and its type and size
    are checked before anything is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PodError("unsafe_cache", f"{path.name} is redirected") from exc
        raise _invalid(f"{path.name} cannot be read") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(f"{path.name} is not a regular file")
        if info.st_size > limit:
            raise _invalid(f"{path.name} exceeds its size limit")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise _invalid(f"{path.name} cannot be read") from exc
    finally:
        os.close(fd)
    if size > limit:
        raise _invalid(f"{path.name} exceeds its size limit")
    return b"".join(chunks)


def _json(raw: bytes, name: str) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _invalid(f"{name} is not valid JSON") from exc


def _read(path: Path, limit: int) -> dict | None:
    """A validated snapshot, or None when absent; PodError when present but unusable."""
    raw = _bytes(path, limit)
    return None if raw is None else validate(_json(raw, path.name))


def _try(path: Path, limit: int, diagnostics: list[str]) -> dict | None:
    try:
        return _read(path, limit)
    except PodError as exc:
        diagnostics.append(f"{path.name} ignored: {exc}")
        return None


def bundled() -> dict | None:
    return _try(BUNDLED_PATH, MAX_BUNDLED, [])


# ---------------------------------------------------------------- reading


def _ages(snapshot: dict | None, moment: datetime) -> dict[str, int | None]:
    if snapshot is None:
        return {}
    return {source_id: (None if block["retrieved_at"] is None
                        else max(0, int((moment - _moment(block["retrieved_at"])).total_seconds())))
            for source_id, block in snapshot["sources"].items()}


def _data_age(snapshot: dict | None, moment: datetime) -> int | None:
    """Age of the oldest required source's data; None when a required source has none."""
    if snapshot is None:
        return None
    ages = _ages(snapshot, moment)
    required = [ages[source_id] for source_id, block in snapshot["sources"].items() if block["required"]]
    if not required or any(age is None for age in required):
        return None
    return max(required)


def _view(snapshot: dict | None, origin: str, moment: datetime, diagnostics: list[str]) -> dict:
    age = _data_age(snapshot, moment)
    return {"origin": origin, "snapshot": snapshot, "age_s": age,
            "stale": age is None or age >= STALE_S, "source_ages": _ages(snapshot, moment),
            "diagnostics": diagnostics}


def _cached(moment: datetime, *, pair: bool) -> tuple[dict, dict | None]:
    """The current view and, with ``pair``, the previous snapshot, read under one shared lock."""
    diagnostics, older = [], None
    try:
        root = cache_root()
        with _lock(root, shared=True):
            snapshot = _try(root / "current.json", MAX_SNAPSHOT, diagnostics)
            if pair:
                older = _try(root / "previous.json", MAX_SNAPSHOT, [])
    except PodError as exc:
        diagnostics.append(f"Observation cache unavailable: {exc}")
        snapshot = None
    if snapshot is not None:
        return _view(snapshot, "cache", moment, diagnostics), older
    snapshot = _try(BUNDLED_PATH, MAX_BUNDLED, diagnostics)
    return _view(snapshot, "bundled" if snapshot else "unknown", moment, diagnostics), older


def load(now: datetime | None = None) -> dict:
    """Offline read: local cache, then the bundled snapshot, then unknown. Never fetches."""
    return _cached(_now(now), pair=False)[0]


def load_pair(now: datetime | None = None) -> dict:
    """``load()`` and the previous snapshot from one shared-lock read, so the pair is coherent.

    A promotion swaps both files under the exclusive lock, so readers never see one swap
    without the other. A crash between the two swaps leaves previous and current both holding
    the snapshot being replaced: still a valid pair, but the older previous generation is lost.
    """
    current, older = _cached(_now(now), pair=True)
    return {"current": current, "previous": older}


def previous() -> dict | None:
    """The snapshot that the latest promotion replaced, or None."""
    return load_pair()["previous"]


def _status_record(root: Path) -> dict | None:
    """The bounded refresh record; unreadable or invalid records count as absent."""
    try:
        raw = _bytes(root / "refresh.json", MAX_STATUS)
        if raw is None:
            return None
        record = _json(raw, "refresh.json")
        _exact(record, {"schema", "attempt", "sources"}, "refresh record")
        attempt = _exact(record["attempt"], {"started_at", "finished_at", "outcome", "check"}, "attempt")
        _utc(attempt["started_at"], "started_at")
        if attempt["finished_at"] is not None:
            _utc(attempt["finished_at"], "finished_at")
        if record["schema"] != REFRESH_SCHEMA or attempt["outcome"] not in OUTCOMES or type(attempt["check"]) is not bool:
            return None
        if not isinstance(record["sources"], dict) or len(record["sources"]) > 8:
            return None
        for row in record["sources"].values():
            _exact(row, {"status", "attempted_at", "retry_after_until", "diagnostics"}, "source attempt")
            if row["status"] not in sources.STATUSES:
                return None
            for key in ("attempted_at", "retry_after_until"):
                if row[key] is not None:
                    _utc(row[key], key)
            if not isinstance(row["diagnostics"], list) or len(row["diagnostics"]) > MAX_DIAGNOSTICS:
                return None
            for item in row["diagnostics"]:
                _text(item, "diagnostic", sources.MAX_DIAGNOSTIC)
        return record
    except (OSError, ValueError, UnicodeError, PodError):
        return None


def _retry_until(record: dict | None, source_id: str, moment: datetime) -> datetime | None:
    row = (record or {}).get("sources", {}).get(source_id)
    if not row or row["retry_after_until"] is None:
        return None
    until = _moment(row["retry_after_until"])
    return until if until > moment else None


def _auto(setting: object, moment: datetime) -> tuple[bool, str]:
    if setting != "automatic":
        return False, "automatic refresh is off"
    try:
        root = cache_root()
        record = _status_record(root)
        with _lock(root, shared=True):
            current = _try(root / "current.json", MAX_SNAPSHOT, [])
    except PodError as exc:
        return False, f"cache unavailable: {exc}"
    for source in sources.SOURCES:
        until = _retry_until(record, source.id, moment) if source.required else None
        if until is not None:
            return False, f"{source.id} asked to retry after {_stamp(until)}"
    if record is not None:
        attempt = record["attempt"]
        since = (moment - _moment(attempt["started_at"])).total_seconds()
        if attempt["outcome"] == "running" and since < BRIEF_RESTRAINT_S:
            return False, "a refresh is already running"
        # A cancel, quit or kill is not a source failure: it restrains automatic starts briefly.
        restraint = {"cancelled": BRIEF_RESTRAINT_S, "failed": FAILED_RESTRAINT_S,
                     "refused": FAILED_RESTRAINT_S}.get(attempt["outcome"], 0)
        if since < restraint:
            return False, f"last attempt at {attempt['started_at']} was {attempt['outcome']}"
    if current is None:
        return True, "no local observations"
    age = _data_age(current, moment)
    if age is None or age >= FRESH_S:
        return True, "observations are at least 24 hours old"
    return False, "observations are fresh"


def auto_refresh_due(setting: object, now: datetime | None = None) -> bool:
    """Whether opening the workspace should start one asynchronous refresh. Offline."""
    return _auto(setting, _now(now))[0]


def _match(source_id: str, name: str) -> dict | None:
    match = getattr(catalog, "match", None)
    if match is None:
        return None
    try:
        return match(source_id, name)
    except PodError:
        return None


def _coverage(source_id: str, block: dict | None) -> dict:
    rows = block["rows"] if block else []
    return {"rows": len(rows), "mapped": sum(_match(source_id, row["name"]) is not None for row in rows)}


def status(now: datetime | None = None, setting: object = None) -> dict:
    """Offline source, cache and mapping diagnostics."""
    moment = _now(now)
    view = load(moment)
    snapshot, record = view["snapshot"], None
    try:
        root = str(cache_root())
        record = _status_record(Path(root))
    except PodError as exc:
        root = None
        view["diagnostics"].append(f"Observation cache unavailable: {exc}")
    rows = {}
    for source in sources.SOURCES:
        block = (snapshot or {}).get("sources", {}).get(source.id)
        attempt = (record or {}).get("sources", {}).get(source.id)
        age = view["source_ages"].get(source.id)
        rows[source.id] = {
            "url": source.url, "attribution": source.attribution, "required": source.required,
            "status": block["status"] if block else "not_attempted",
            "retrieved_at": block["retrieved_at"] if block else None,
            "published_at": block["published_at"] if block else None,
            "methodology": block["methodology"] if block else None,
            "age_s": age, "stale": age is None or age >= STALE_S,
            **_coverage(source.id, block),
            "diagnostics": (block["diagnostics"] if block else []),
            "last_attempt": attempt}
    due, reason = _auto(setting, moment)
    return {"cache": root, "origin": view["origin"],
            "generation": snapshot["generation"] if snapshot else None,
            "created_at": snapshot["created_at"] if snapshot else None,
            "age_s": view["age_s"], "stale": view["stale"], "sources": rows,
            "last_attempt": record["attempt"] if record else None,
            "auto_refresh": {"setting": setting, "due": due, "reason": reason},
            "diagnostics": view["diagnostics"]}


# ---------------------------------------------------------------- refresh


def _digest(path: Path) -> str | None:
    try:
        raw = _bytes(path, MAX_SNAPSHOT)
    except PodError as exc:
        return f"unusable: {exc}"
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _empty_block(source: sources.Source, status: str) -> dict:
    return {"url": source.url, "attribution": source.attribution, "required": source.required,
            "status": status, "retrieved_at": None, "published_at": None, "methodology": None,
            "rows": [], "diagnostics": []}


def _kept(source: sources.Source, prior: dict | None, status: str, message: str) -> dict:
    """A failed optional source keeps its prior rows with their own retrieval time."""
    if prior is None:
        block = _empty_block(source, status)
    else:
        block = {**prior, "status": status, "required": source.required}
        if prior["rows"]:
            message += f"; keeping rows retrieved at {prior['retrieved_at']}"
    block["diagnostics"] = [sources.diagnostic(message)]
    return block


def _collapsed(old: dict, new: dict) -> bool:
    return any(old[key] >= COLLAPSE_FLOOR and new[key] * 2 < old[key] for key in ("rows", "mapped"))


def _diff(old: dict | None, new: dict) -> dict:
    result = {}
    for source_id, block in new["sources"].items():
        before = {row["name"]: row for row in ((old or {}).get("sources", {}).get(source_id) or {"rows": []})["rows"]}
        after = {row["name"]: row for row in block["rows"]}
        groups = {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
                  "changed": sorted(name for name in set(after) & set(before) if after[name] != before[name])}
        result[source_id] = {key: names[:MAX_DIFF_NAMES] for key, names in groups.items()} | {
            "counts": {key: len(names) for key, names in groups.items()}}
    return result


def _write_record(root: Path, record: dict) -> None:
    atomic_json(root / "refresh.json", record, limit=MAX_STATUS)


def _attempt_record(previous_record: dict | None, started: str, outcome: str, check: bool,
                    finished: str | None, attempts: dict) -> dict:
    rows = dict((previous_record or {}).get("sources", {}))
    rows.update(attempts)
    return {"schema": REFRESH_SCHEMA, "sources": rows,
            "attempt": {"started_at": started, "finished_at": finished, "outcome": outcome, "check": check}}


def refresh(check: bool = False, now: datetime | None = None, cancel=None, fetch=None,
            *, policy: sources.Policy | None = None) -> dict:
    """Fetch every fixed source, validate, compare and atomically promote one snapshot.

    ``check`` and cancelled runs never promote. The cache lock is held only for short file
    reads and the final compare-and-swap, never across network work. ``fetch(url, *,
    deadline, cancel) -> sources.Page`` may be injected; it defaults to the bounded HTTPS
    reader. One wall-clock deadline of ``policy.timeout_s`` (``sources.DEFAULT``'s when no policy
    is given) covers every source's fetch and parse together. An unexpected error settles the
    attempt as failed instead of leaving it running. No preference, registry or bundle file is
    written and no worker is started.
    """
    moment = _now(now)
    started = _stamp(moment)
    fetcher = fetch or partial(sources.fetch, policy=policy or sources.DEFAULT)
    root = cache_root()
    diagnostics: list[str] = []
    with _lock(root):
        base_digest = _digest(root / "current.json")
        base = _try(root / "current.json", MAX_SNAPSHOT, diagnostics)
        record = _status_record(root)
        _write_record(root, _attempt_record(record, started, "running", check, None, {}))
    try:
        reference = base or bundled()
        deadline = time.monotonic() + (policy or sources.DEFAULT).timeout_s
        blocks, attempts = {}, {}
        cancelled = required_failed = collapsed = False
        for source in sources.SOURCES:
            prior = (reference or {}).get("sources", {}).get(source.id)
            prior = prior if prior and prior["url"] == source.url else None
            until = _retry_until(record, source.id, moment)
            retry_s = None
            if cancelled or sources.cancelled(cancel):
                cancelled = True
                status_, message = "not_attempted", "Refresh was cancelled"
            elif until is not None:
                status_, message = "not_attempted", f"Source asked to retry after {_stamp(until)}"
            elif required_failed:
                status_, message = "not_attempted", "Skipped because a required source failed"
            else:
                page = None
                try:
                    page = fetcher(source.url, deadline=deadline, cancel=cancel)
                    parsed = sources.PARSERS[source.id](page.text, deadline)
                    block = {"url": source.url, "attribution": source.attribution,
                             "required": source.required, "status": "ok",
                             "retrieved_at": _stamp(_now(now)), "published_at": parsed["published_at"],
                             "methodology": parsed["methodology"], "rows": parsed["rows"],
                             "diagnostics": [sources.diagnostic(item) for item in parsed["diagnostics"]][:MAX_DIAGNOSTICS]}
                    _block(block)
                    old, new = _coverage(source.id, prior), _coverage(source.id, block)
                    if _collapsed(old, new):
                        collapsed = collapsed or source.required
                        raise sources.ParseError(
                            f"Coverage collapsed from {old['rows']} rows ({old['mapped']} mapped) "
                            f"to {new['rows']} ({new['mapped']} mapped)")
                    blocks[source.id] = block
                    attempts[source.id] = {"status": "ok", "attempted_at": started,
                                           "retry_after_until": None, "diagnostics": block["diagnostics"]}
                    continue
                except sources.SourceError as exc:
                    status_, message, retry_s = exc.status, f"{source.id}: {exc}", exc.retry_after_s
                    cancelled = cancelled or (exc.status == "not_attempted" and sources.cancelled(cancel))
                except (sources.ParseError, PodError) as exc:
                    status_, message = "malformed", f"{source.id}: {exc}"
                except Exception as exc:  # A fetch or parser defect fails this source, never the record.
                    status_ = "unavailable" if page is None else "malformed"
                    message = f"{source.id}: unexpected {type(exc).__name__}: {exc}"
            blocks[source.id] = _kept(source, prior, status_, message)
            if until is not None:
                retry_at = _stamp(until)
            elif retry_s is not None:
                retry_at = _stamp(datetime.fromtimestamp(moment.timestamp() + retry_s, timezone.utc))
            else:
                retry_at = None
            attempts[source.id] = {"status": status_, "attempted_at": None if status_ == "not_attempted" else started,
                                   "retry_after_until": retry_at, "diagnostics": blocks[source.id]["diagnostics"]}
            if source.required and status_ != "ok":
                required_failed = True
                diagnostics.append(sources.diagnostic(message))

        candidate = None
        if cancelled:
            outcome = "cancelled"
        elif required_failed:
            outcome = "refused" if collapsed else "failed"
        else:
            candidate = validate({"schema": SCHEMA, "generation": "0", "created_at": started, "sources": blocks})
            outcome = "checked" if check else "promoted"
        generation = None
        with _lock(root):
            if outcome == "promoted":
                if sources.cancelled(cancel):
                    outcome = "cancelled"
                elif _digest(root / "current.json") != base_digest:
                    outcome = "superseded"
                    diagnostics.append("A concurrent refresh promoted newer observations first")
                else:
                    older = _try(root / "previous.json", MAX_SNAPSHOT, [])
                    counter = max([int(doc["generation"]) for doc in (base, older) if doc] + [0]) + 1
                    generation = candidate["generation"] = str(counter)
                    candidate["created_at"] = _stamp(_now(now))
                    if base is not None:
                        atomic_json(root / "previous.json", base, limit=MAX_SNAPSHOT)
                    atomic_json(root / "current.json", candidate, limit=MAX_SNAPSHOT)
            _write_record(root, _attempt_record(_status_record(root), started, outcome, check,
                                                _stamp(_now(now)), attempts))
        return {"outcome": outcome, "check": check, "promoted": outcome == "promoted",
                "generation": generation, "base_generation": base["generation"] if base else None,
                "sources": {source_id: {"status": block["status"], **_coverage(source_id, block),
                                        "retrieved_at": block["retrieved_at"],
                                        "retry_after_until": attempts[source_id]["retry_after_until"],
                                        "diagnostics": block["diagnostics"]}
                            for source_id, block in blocks.items()},
                "diff": _diff(reference, candidate) if candidate else None,
                "diagnostics": diagnostics[:MAX_DIAGNOSTICS]}
    except Exception as exc:
        # Never leave the record at "running": settle it as failed, then report a PodError.
        try:
            with _lock(root):
                _write_record(root, _attempt_record(_status_record(root), started, "failed", check,
                                                    _stamp(_now(now)), {}))
        except (PodError, OSError):
            pass
        if isinstance(exc, PodError):
            raise
        raise PodError("refresh_failed", sources.diagnostic(
            f"Refresh failed unexpectedly: {type(exc).__name__}: {exc}")) from exc

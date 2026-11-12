"""One append-only log per objective. Everything Pod knows about an objective is a fold over it.

An entry is one JSON object on one line. Entries are never edited or removed, there is no
limit on how many an objective may have, and no caller supplies a sequence number: an append
names the length it read, so a writer that worked from an older view is refused. A crash can
leave a partial last line; it was never an entry, so readers ignore it and the next append
clears it.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re

from . import paths
from .errors import PodError

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def path(repository: str, objective: str) -> Path:
    for value, what in ((repository, "repository"), (objective, "objective")):
        if not NAME.fullmatch(value):
            raise PodError("invalid_name", f"{what} id {value!r} must be lowercase letters, digits, dot, dash or underscore.",
                           fix="choose a short id such as issue-42")
    return paths.objectives_home() / repository / f"{objective}.jsonl"


def _entries(data: bytes) -> tuple[list[dict], int]:
    """Complete entries and the byte length they occupy; a partial last line is not an entry."""
    entries, good = [], 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            raise PodError("log_damaged", f"Entry {len(entries)} of the objective log is not readable.",
                           fix="restore the log file from a copy; Pod never rewrites entries")
        entries.append(entry)
        good += len(line)
    return entries, good


def read(target: Path) -> list[dict]:
    """Every entry, without taking the lock: the file only grows."""
    try:
        return _entries(target.read_bytes())[0]
    except FileNotFoundError:
        return []


class Writer:
    """The objective lock, held for one command. Appends are checked against the view read."""

    def __init__(self, handle: int, entries: list[dict]):
        self._handle = handle
        self.entries = entries

    def append(self, *new: dict) -> list[dict]:
        """Write entries durably before the caller acts on them."""
        size = os.fstat(self._handle).st_size
        data = os.pread(self._handle, size, 0)
        current, good = _entries(data)
        if len(current) != len(self.entries):
            raise PodError("log_moved", "Another writer appended to this objective.",
                           fix="run `pod status` and repeat the command if it is still needed")
        if good != size:
            os.ftruncate(self._handle, good)
        stamped = [{"n": len(self.entries) + offset, "at": now(), **entry} for offset, entry in enumerate(new)]
        payload = b"".join(json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
                           for entry in stamped)
        written = 0
        while written < len(payload):       # a short write is not an entry: write every byte or raise
            written += os.pwrite(self._handle, payload[written:], good + written)
        os.fsync(self._handle)
        self.entries.extend(stamped)
        return stamped


@contextmanager
def writer(target: Path, *, create: bool = False):
    """Hold the objective's lock. Without `create`, a missing log is a refusal."""
    if not create and not target.exists():
        raise PodError("objective_unknown", f"No objective log at {target}.", fix="run `pod status --all` to list objectives")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        entries, _ = _entries(os.pread(handle, os.fstat(handle).st_size, 0))
        if not entries and not create:
            raise PodError("objective_unknown", f"No objective log at {target}.", fix="run `pod status --all` to list objectives")
        yield Writer(handle, entries)
    finally:
        os.close(handle)               # an empty file left by a refused open is not an objective and is reused


def every() -> list[Path]:
    """Every objective log on this host."""
    home = paths.objectives_home()
    return sorted(home.glob("*/*.jsonl")) if home.is_dir() else []

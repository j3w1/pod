"""The last good public snapshot and the outcome of the last refresh attempt.

A refresh either replaces the snapshot with a complete new one or leaves it exactly as it was
and records why. Only one snapshot is kept, with the methodology its page stated, so values
measured under different methodologies are never set side by side. Nothing here reads or
writes the personal preference file.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .. import paths
from . import leaderboard

SCHEMA = "pod-model-snapshot/v1"


def home() -> Path:
    return paths.cache_home() / "models"


def _read(path: Path):
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load() -> dict | None:
    """The last good snapshot, or None. A damaged file reads as no snapshot."""
    value = _read(home() / "snapshot.json")
    if (value is None or value.get("schema") != SCHEMA or not isinstance(value.get("rows"), list)
            or not all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in value["rows"])):
        return None
    return value


def last_attempt() -> dict | None:
    value = _read(home() / "attempt.json")
    return value if value is not None and isinstance(value.get("ok"), bool) else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def refresh(fetch=None) -> dict:
    """One fetch and one parse. Returns the attempt record; the snapshot changes only on success."""
    attempt = {"attempted_at": now(), "source": leaderboard.URL, "ok": False, "failure": None}
    try:
        parsed = leaderboard.parse((fetch or leaderboard.fetch)())
    except leaderboard.RefreshError as exc:
        attempt["failure"] = leaderboard.clean(str(exc), 300)
    else:
        _write(home() / "snapshot.json", {
            "schema": SCHEMA, "source": leaderboard.URL, "attribution": leaderboard.ATTRIBUTION,
            "fetched_at": attempt["attempted_at"], "methodology": parsed["methodology"],
            "units": leaderboard.UNITS, "unknown_columns": parsed["unknown_columns"],
            "absent_metrics": parsed["absent_metrics"], "diagnostics": parsed["diagnostics"], "rows": parsed["rows"]})
        attempt.update(ok=True, rows=len(parsed["rows"]), methodology=parsed["methodology"],
                       diagnostics=parsed["diagnostics"])
    _write(home() / "attempt.json", attempt)
    return attempt


def age(stamp: str | None, *, at: datetime | None = None) -> str:
    """A short age such as `3 h ago`, or `unknown age` for an unreadable time."""
    try:
        then = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
        seconds = ((at or datetime.now(timezone.utc)) - then).total_seconds()
    except (ValueError, TypeError):
        return "unknown age"
    if seconds < 0:
        return "unknown age"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{int(seconds // size)} {unit} ago"
    return "just now"

"""R239: a shared index file of keyed rows binds a review to the rows its change edits and the file's schema.

An index file is a JSON object whose `schema` member names a known index format. Each entry of its keyed
collections is one row, keyed by the collection and the entry's key; every other top-level member except
`schema` is one row keyed by its name. The file's schema is its format name, its top-level member names and
which of them are keyed collections. Row order is not bound.

A binding entry for such a file keeps its whole-content identity and adds `index`: the schema digest and,
for each row the reviewed change adds, removes or edits against its merge base, that row's digest (null when
the change removes it). The entry stays current while the file parses, has no duplicate key, keeps the same
schema and every bound row is unchanged; another change's rows elsewhere do not matter. Anything Pod cannot
judge exactly falls back to today's whole-content binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# The keyed collections of each known index format. A list collection is keyed by its entries' `id`; an
# object collection by its member names.
KINDS: dict[str, dict[str, str]] = {
    "pod-scenario-coverage/v1": {"scenarios": "list", "withdrawn_scenarios": "list"},
    "suite-lanes/v1": {"durations": "object", "overrides": "object"},
}
MAX_ROWS = 256      # bound rows per entry; a larger change keeps whole-content binding
MAX_KEY = 512       # characters of one row key
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class _Duplicate(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    found: dict = {}
    for name, value in pairs:
        if name in found:
            raise _Duplicate(name)
        found[name] = value
    return found


def _constant(name: str):
    raise ValueError(name)  # NaN and the infinities are not JSON


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(*parts: str) -> str:
    return json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))


def parse(data: Any) -> tuple[str, dict[str, str]] | None:
    """The schema digest and every row's digest, or None for any file that is not exactly a known index:
    undecodable or invalid JSON, a duplicate member name at any depth, an unknown format, a collection of
    the wrong type, a list row without a text `id`, or two rows with one key."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        document = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("schema") not in KINDS:
        return None
    collections = KINDS[document["schema"]]
    rows: dict[str, str] = {}
    for name, value in document.items():
        if name == "schema":
            continue
        shape = collections.get(name)
        if shape is None:
            rows[_key(name)] = _digest(value)
            continue
        if shape == "list":
            if not isinstance(value, list):
                return None
            entries = []
            for row in value:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    return None
                entries.append((row["id"], row))
        else:
            if not isinstance(value, dict):
                return None
            entries = list(value.items())
        for key, row in entries:
            item = _key(name, key)
            if item in rows:
                return None
            rows[item] = _digest(row)
    schema = _digest({"schema": document["schema"], "members": sorted(document),
                      "collections": sorted(name for name in collections if name in document)})
    return schema, rows


def binding(entry: dict, current: Any, base: Any) -> dict:
    """`entry` with its `index`, or unchanged when Pod cannot bind rows exactly.

    `current` is the file's bytes now, which must hash to the entry's recorded identity; `base` is its bytes at
    the merge base of the reviewed candidate and the governed base, or None when it is absent or unreadable
    there. Unchanged entries keep today's whole-content binding."""
    if (not isinstance(entry, dict) or entry.get("state") != "present" or "index" in entry
            or not isinstance(current, (bytes, bytearray))
            or hashlib.sha256(bytes(current)).hexdigest() != entry.get("sha256")):
        return entry
    now, then = parse(current), parse(base)
    if now is None or then is None:
        return entry
    rows = {key: now[1].get(key) for key in sorted(set(now[1]) | set(then[1]))
            if now[1].get(key) != then[1].get(key)}
    if len(rows) > MAX_ROWS or any(len(key) > MAX_KEY for key in rows):
        return entry
    return {**entry, "index": {"schema": now[0], "rows": rows}}


def current(entry: dict, data: Any) -> bool:
    """Whether a row-bound entry still holds: the file parses as the same schema and every bound row is unchanged
    (a removed row is still absent). Fails closed on anything else."""
    index = entry.get("index") if isinstance(entry, dict) else None
    if not valid(index):
        return False
    parsed = parse(data)
    if parsed is None or parsed[0] != index["schema"]:
        return False
    return all(parsed[1].get(key) == digest for key, digest in index["rows"].items())


def valid(index: Any) -> bool:
    """The stored `index` shape: a schema digest and at most MAX_ROWS bounded keys mapped to a digest or null."""
    return (isinstance(index, dict) and set(index) == {"schema", "rows"}
            and isinstance(index["schema"], str) and bool(_DIGEST.match(index["schema"]))
            and isinstance(index["rows"], dict) and len(index["rows"]) <= MAX_ROWS
            and all(isinstance(key, str) and 0 < len(key) <= MAX_KEY
                    and (digest is None or isinstance(digest, str) and bool(_DIGEST.match(digest)))
                    for key, digest in index["rows"].items()))

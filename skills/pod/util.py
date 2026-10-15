"""Small bounded JSON and identity primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
from typing import Any
import unicodedata

from .errors import FieldRefusal, PodError

MAX_RECORD = 128 * 1024
DECISION_FIELDS = {"provenance", "instruction", "channel", "relay"}
CHANNEL_FIELDS = {"channel", "relay"}
RELAYED_DECISIONS = {"question", "governance", "withdrawal", "claim", "retirement"}


def owner_decision(value: dict, *, kind: str, stored: bool = False) -> dict:
    """Validate the shared owner-decision fields; stored reads never add or rewrite fields.

    The enclosing boundary validates its own facts and provenance. Older inputs without
    instruction/provenance keep their existing shape; those fields, when supplied, obey
    the same contract. A relay describes its verified owner source, not Pod verification
    of the person or where a keystroke originated.
    """
    if not isinstance(value, dict):
        raise PodError("invalid_decision", "An owner decision must be an object")
    if "provenance" in value and value["provenance"] != "user_direct":
        raise PodError("invalid_decision", "Owner decision provenance must be user_direct")
    if "instruction" in value:
        bounded_text(value["instruction"], name="instruction", limit=1024)
        try:
            value["instruction"].encode("utf-8")
        except UnicodeEncodeError:
            raise PodError("invalid_decision_text", "Decision instruction must be UTF-8 text") from None
    channel = value.get("channel", "session")
    if channel not in ("session", "relayed"):
        raise PodError("invalid_decision_channel", "Decision channel must be session or relayed")
    if channel == "session" and "relay" in value or channel == "relayed" and "relay" not in value:
        raise PodError("invalid_decision_relay", "A complete relay is required exactly for channel relayed",
                       {"next_action": "classify the channel honestly and provide relay reference and owner_source only for relayed decisions"})
    if channel == "relayed":
        relay = exact(value["relay"], {"reference", "owner_source"}, {"reference", "owner_source"}, name="decision_relay")
        for field in ("reference", "owner_source"):
            bounded_text(relay[field], name=field, limit=512)
            try:
                relay[field].encode("utf-8")
            except UnicodeEncodeError:
                raise PodError("invalid_decision_text", "Relay fields must be UTF-8 text") from None
        if kind not in RELAYED_DECISIONS:
            code = "relayed_authorization_refused" if kind == "authorization" else "relayed_decision_refused"
            action = "obtain the owner's decision typed in the coordinator's own session for this decision kind"
            raise PodError(code, f"A relayed {kind} decision is refused. Next: {action}",
                           {"kind": kind, "next_action": action})
    return dict(value) if stored else {**value, "channel": channel}


def decision_channel(value: dict) -> dict:
    """Additive public projection, with earlier provenance explicit and stored bytes untouched."""
    return {**value, "channel": value.get("channel", "session"),
            **({"channel_recorded": False} if "channel" not in value else {})}


def channel_fields(value: dict) -> dict:
    return {key: value[key] for key in CHANNEL_FIELDS | {"channel_recorded"} if key in value}


def derived_governance_summary(state: dict) -> dict | None:
    """Bounded projection; complete adoptions remain only at their history positions."""
    count, first, latest = 0, None, None
    for index, row in enumerate(state.get("governance_history", {}).get("decisions", [])):
        if row.get("provenance") == "derived":
            count += 1
            if first is None:
                first = (index, row)
            latest = (index, row)
    if first is None:
        return None
    row = latest[1]
    return {"count": count, "previous_base": first[1]["previous_base"],
            "base": row["base"], "base_ref": row["base_ref"],
            "sources": [dict(source) for source in row["sources"]],
            "history": {"reference": "governance_history/decisions", "provenance": "derived",
                        "first": first[0], "last": latest[0]}}


def map_decisions(state: dict) -> list[dict]:
    """Owner decision records on the map, with stable location references."""
    rows = []
    def add(kind, reference, value):
        rows.append({"kind": kind, "reference": reference, "decision": decision_channel(value)})
    for index, value in enumerate(state.get("revision_decisions", [])):
        add("revision", f"revision_decisions/{index}", value)
    for index, value in enumerate(state.get("governance_history", {}).get("decisions", [])):
        if value.get("provenance") != "derived":
            add("governance", f"governance_history/decisions/{index}", value)
    for index, value in enumerate(state.get("reopened", [])):
        add("reopen", f"reopened/{index}", value)
    for ob in state.get("obligations", []):
        if ob.get("provenance") == "user_direct":
            add("obligation", f"obligations/{ob['id']}/source", ob["source"])
        if ob.get("withdrawal", {}).get("by") == "user_direct":
            add("withdrawal", f"obligations/{ob['id']}/withdrawal", ob["withdrawal"])
    return rows


def context_decisions(state: dict, journal: dict | None = None) -> list[dict]:
    """Non-map owner decisions; derived continuity and Governor verdicts are not owner decisions."""
    rows = []
    def add(kind, reference, value):
        rows.append({"kind": kind, "reference": reference, "decision": decision_channel(value)})
    for index, value in enumerate(state.get("constraints", [])):
        if value.get("provenance") == "user_direct":
            add("constraint", f"constraints/{index}", value)
    for index, value in enumerate(((state.get("checkpoint") or {}).get("continuity") or {}).get("history", [])):
        if isinstance(value.get("decision"), dict):
            add("handoff" if value["provenance"] == "owner_handoff" else "continuity", f"continuity/history/{index}", value["decision"])
    if isinstance(state.get("continued_by"), dict):
        add("claim", "continued_by", state["continued_by"])
    if isinstance(state.get("historical_retirement"), dict):
        add("retirement", "historical_retirement", state["historical_retirement"])
    for identity, admission in state.get("admissions", {}).items():
        for index, failure in enumerate(admission.get("failures", [])):
            if failure.get("cleared_by") == "user":
                add("route_failure_clear", f"admissions/{identity}/failures/{index}/clear_decision", failure.get("clear_decision", {}))
    for value in (journal or {}).get("actions", []):
        for field, kind in (("authorization", "authorization"),):
            decision = value.get("action", {}).get(field)
            if isinstance(decision, dict) and "carried_from" not in value:
                add(kind, f"governor/{value['record_id']}/{field}", decision)
        if isinstance(value.get("exception"), dict) and "carried_from" not in value:
            add("governor_exception", f"governor/{value['record_id']}/exception", value["exception"])
    return rows


def protect_decision_record(value: dict) -> None:
    """Earlier readers must refuse channel fields and closure reports they cannot read faithfully."""
    def carries_channel(item):
        return (isinstance(item, dict) and (bool((CHANNEL_FIELDS | {"decision_channels"}) & item.keys()) or any(carries_channel(v) for v in item.values()))
                or isinstance(item, list) and any(carries_channel(v) for v in item))
    if carries_channel(value):
        value["decision_channels"] = 1
_ZONED_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")


def normalize_timestamp(value: object, *, field: str, code: str) -> str:
    """Accept a bounded zoned timestamp and store one UTC representation."""
    example = "2026-09-25T20:01:22Z"
    if not isinstance(value, str) or _ZONED_TIMESTAMP.fullmatch(value) is None:
        raise PodError(code, f"{field} needs a timezone, for example {example}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
    except ValueError as exc:
        raise PodError(code, f"{field} needs a valid timezone, for example {example}") from exc
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")


def explicit_home(name: str) -> Path | None:
    """Return one absolute, process-scoped Pod home override when present."""
    if name not in os.environ:
        return None
    value = os.environ[name]
    if not value or len(value) > 4096 or "\x00" in value:
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path")
    try:
        path = Path(value)
    except (OSError, ValueError) as exc:
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path") from exc
    if not path.is_absolute():
        raise PodError("invalid_location_override", f"{name} must be an absolute directory path")
    if path.exists() and not path.is_dir():
        raise PodError("invalid_location_override", f"{name} must identify a directory")
    return path


def _inside(candidate: Path, project: Path) -> bool:
    """Return whether a path is lexically or physically inside a project."""
    try:
        lexical_candidate = Path(os.path.abspath(candidate))
        lexical_project = Path(os.path.abspath(project))
        physical_candidate = candidate.resolve(strict=False)
        physical_project = project.resolve(strict=False)
        return (lexical_candidate == lexical_project
                or lexical_candidate.is_relative_to(lexical_project)
                or physical_candidate == physical_project
                or physical_candidate.is_relative_to(physical_project))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PodError("invalid_native_home", "Native home containment cannot be proven") from exc


def native_home(name: str, *, default: Path | None = None,
                project: Path | None = None) -> Path:
    """Resolve a native profile home outside the current project boundary."""
    if name not in os.environ:
        if default is None:
            raise PodError("native_home_unavailable", f"{name} is required")
        path = default
    else:
        value = os.environ[name]
        if not value or len(value) > 4096 or "\x00" in value:
            raise PodError("invalid_native_home", f"{name} must be an absolute directory path")
        try:
            path = Path(value)
        except (OSError, ValueError) as exc:
            raise PodError("invalid_native_home", f"{name} must be an absolute directory path") from exc
    if not path.is_absolute() or (path.exists() and not path.is_dir()):
        raise PodError("invalid_native_home", f"{name} must be an absolute directory path")
    boundary = (project if project is not None else Path.cwd()).resolve()
    boundaries = []
    from .github import repository_context
    context = repository_context(boundary)
    if context.get("repo_key") is not None:
        boundaries = [Path(value) for value in context["linked_worktrees"]]
    if any(_inside(path, candidate) for candidate in boundaries):
        raise PodError("project_contained_native_home",
                       f"{name} must remain outside every linked project worktree")
    return path


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def bounded_json(path: Path, *, limit: int = MAX_RECORD) -> Any:
    cursor = path.parent
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise PodError("unsafe_record", "Record parent is redirected")
        cursor = cursor.parent
    if path.is_symlink() or not path.is_file():
        raise PodError("unsafe_record", "Record must be a regular, non-symlink file")
    if path.stat().st_size > limit:
        raise PodError("record_too_large", "Record exceeds its size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded JSON record") from exc


def bounded_stdin_json(*, limit: int = MAX_RECORD) -> Any:
    """Read one private JSON request from a pipe without treating it as a path."""
    try:
        raw = sys.stdin.buffer.read(limit + 1)
    except OSError as exc:
        raise PodError("invalid_record", "Cannot read bounded JSON input") from exc
    if len(raw) > limit:
        raise PodError("record_too_large", "Record exceeds its size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PodError("invalid_record", "Cannot decode bounded JSON record") from exc


def encoded(value: Any, *, compact: bool = False) -> bytes:
    """The exact bytes `atomic_json` writes for a value."""
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    return json.dumps(value, sort_keys=True, ensure_ascii=False, **options).encode() + b"\n"


def atomic_json(path: Path, value: Any, *, limit: int = MAX_RECORD, compact: bool = False) -> None:
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    data = encoded(value, compact=compact)
    if len(data) > limit:
        sections = sorted(
            ({"section": key, "bytes": len(json.dumps(item, sort_keys=True, ensure_ascii=False,
                                                      **options).encode())}
             for key, item in value.items()), key=lambda item: item["bytes"], reverse=True
        )[:8] if isinstance(value, dict) else []
        current = path.stat().st_size if path.exists() and path.is_file() else 0
        raise PodError("record_too_large",
                       f"Record size {current} -> {len(data)} bytes exceeds limit {limit}; "
                       + "largest sections: " + ", ".join(f"{item['section']}={item['bytes']}" for item in sections),
                       {"current_bytes": current, "proposed_bytes": len(data), "limit_bytes": limit,
                        "largest_sections": sections})
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise PodError("unsafe_record", "Record parent is a symlink")
    fd, name = tempfile.mkstemp(prefix=".pod-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


# Field names echoed by an input refusal come from the caller's keys, so they are bounded.
MAX_NAMED_FIELDS = 8
MAX_FIELD_NAME = 64


def _field_names(names: set, *, complete: bool = False) -> tuple[list[str], int]:
    """Sorted, control-stripped names; only caller vocabulary has a count bound."""
    cleaned = sorted({"".join(character for character in str(name)
                              if unicodedata.category(character) not in ("Cc", "Cf", "Cs"))[:MAX_FIELD_NAME]
                      for name in names})
    return (cleaned, 0) if complete else (cleaned[:MAX_NAMED_FIELDS], max(0, len(cleaned) - MAX_NAMED_FIELDS))


def exact(value: Any, fields: set[str], required: set[str] | None = None, *,
          name: str = "record", error=None) -> dict:
    if not isinstance(value, dict) or set(value) - fields or (required or set()) - set(value):
        if error is not None:
            raise error(name)
        shape = {"accepted": _field_names(fields, complete=True)[0],
                 "optional": _field_names(fields - (required or set()), complete=True)[0]}
        if not isinstance(value, dict):
            raise FieldRefusal("invalid_" + name, f"{name} must be a JSON object",
                               {"record": name, "expected": "object", **shape})
        missing, missing_omitted = _field_names((required or set()) - set(value))
        unsupported, unsupported_omitted = _field_names(set(value) - fields)
        parts = [f"{label} {', '.join(names)}" + (f" (+{omitted} more)" if omitted else "")
                 for label, names, omitted in (("missing", missing, missing_omitted),
                                               ("unsupported", unsupported, unsupported_omitted))
                 if names]
        raise FieldRefusal("invalid_" + name,
                           f"{name} has missing or unsupported fields: {'; '.join(parts)}",
                           {"record": name, "missing": missing, "unsupported": unsupported,
                            "omitted": missing_omitted + unsupported_omitted, **shape})
    return value


def route_flags(row: dict) -> tuple[bool, bool]:
    decision = row.get("route_decision") or {}
    return bool(decision.get("route_mismatch")), bool(decision.get("effective_unknown"))


def route_summary(rows: list[dict], *, unknown_states: tuple[str, ...] | None = None) -> tuple[bool, bool]:
    flagged = [(row, route_flags(row)) for row in rows]
    return (any(flags[0] for _, flags in flagged),
            any(flags[1] and (unknown_states is None or row.get("state") in unknown_states)
                for row, flags in flagged))


def bounded_text(value: Any, *, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        code_name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
        length = len(value) if isinstance(value, str) else None
        raise PodError("invalid_" + code_name,
                       f"{name} must be bounded, nonempty text (limit {limit}, length {length})",
                       {"field": name, "limit": limit, "length": length})
    return value

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
from .contracts import bound, fields, required, input_record

MAX_RECORD = 128 * 1024
DECISION_EVIDENCE = {"actor", "decided_at", "recorded_at", "decision_binding", "decision_verification"}
DECISION_FIELDS = {"provenance", "instruction", "channel", "relay"} | DECISION_EVIDENCE
CHANNEL_FIELDS = {"channel", "relay"} | DECISION_EVIDENCE
RELAYED_DECISIONS = {"question", "governance", "withdrawal", "claim", "retirement"}


@input_record("decision_relay")
def _decision_relay(value):
    relay = exact(value, fields("decision_relay"), required("decision_relay"), name="decision_relay")
    for field in ("reference", "owner_source"):
        bounded_text(relay[field], name=field, limit=bound("decision_relay", field))
        try:
            relay[field].encode("utf-8")
        except UnicodeEncodeError:
            raise PodError("invalid_decision_text", "Relay fields must be UTF-8 text") from None
    return relay


def owner_decision(value: dict, *, kind: str, stored: bool = False,
                   verified_authorization: bool = False) -> dict:
    """Validate the shared owner-decision fields; stored reads never add or rewrite fields.

    The enclosing boundary validates its own facts and provenance. Older inputs without
    instruction/provenance keep their existing shape; those fields, when supplied, obey
    the same contract. A relay describes its verified owner source, not Pod verification
    of the person or where a keystroke originated.
    """
    if not isinstance(value, dict):
        raise PodError("invalid_decision", "An owner decision must be an object")
    from .relay import current_decision, decision_input, stored_or_capability
    capability = current_decision()
    if (decision_input(value) or DECISION_EVIDENCE & set(value)
            or not stored and capability is not None and capability.matches(value, kind)):
        return stored_or_capability(value, kind=kind, stored=stored)
    if "provenance" in value and value["provenance"] != "user_direct":
        raise PodError("invalid_decision", "Owner decision provenance must be user_direct")
    if "instruction" in value:
        bounded_text(value["instruction"], name="instruction", limit=bound("revision authority", "instruction"))
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
        if kind in ("authorization", "standing_authorization") and verified_authorization:
            relay = exact(value["relay"], fields("authorization relay"), required("authorization relay"),
                          name="authorization relay")
            for field in relay:
                bounded_text(relay[field], name=field, limit=bound("authorization relay", field))
                try:
                    relay[field].encode("utf-8")
                except UnicodeEncodeError:
                    raise PodError("invalid_decision_text", "Relay fields must be UTF-8 text") from None
        else:
            _decision_relay(value["relay"])
        if kind not in RELAYED_DECISIONS and not (kind in ("authorization", "standing_authorization") and verified_authorization):
            code = "relayed_authorization_refused" if kind == "authorization" else "relayed_decision_refused"
            action = "obtain the owner's decision typed in the coordinator's own session for this decision kind"
            raise PodError(code, f"A relayed {kind} decision is refused. Next: {action}",
                           {"kind": kind, "next_action": action})
    return dict(value) if stored else {**value, "channel": channel}


def decision_channel(value: dict) -> dict:
    """Full decision inventory, with earlier channel provenance explicit and bytes untouched."""
    return {**value, "channel": value.get("channel", "session"),
            **({"channel_recorded": False} if "channel" not in value else {})}


def public_decisions(value: Any, *, rule: bool = False) -> Any:
    """Copy public facts without expanding new private decision authority.

    Internal inventories and closure reports keep their full authenticated records.
    Only new kernel actor records lose authority fields here; earlier session and
    relay representations retain their documented semantics. Route rules also keep
    their existing original-word exclusion, including nested request records.
    """
    if isinstance(value, list):
        return [public_decisions(item, rule=rule) for item in value]
    if not isinstance(value, dict):
        return value
    rule = rule or value.get("kind") == "route_rule"
    authority = (isinstance(value.get("actor"), dict)
                 or bool({"decision_binding", "decision_verification"} & value.keys()))
    private = ((DECISION_FIELDS - {"provenance", "channel"}) | {"decision_effect"}) if authority else set()
    if authority or rule:
        private = private | {"request", "instruction"}
    result = {key: public_decisions(item, rule=rule) for key, item in value.items() if key not in private}
    if authority:
        actor = value.get("actor", (value.get("decision_verification") or {}).get("principal"))
        if isinstance(actor, dict):
            result["actor"] = {key: actor[key] for key in ("kind", "channel") if key in actor}
            if "channel" not in result["actor"] and "channel" in value:
                result["actor"]["channel"] = value["channel"]
        if (private - {"actor"}) & value.keys():
            result["decision_sha256"] = digest({key: value[key] for key in DECISION_FIELDS | {"decision_effect"} if key in value})
        if "instruction" in value:
            result["instruction_sha256"] = digest(value["instruction"])
    return result


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
        if ob.get("withdrawal", {}).get("by") in ("user_direct", "agent_decision"):
            add("withdrawal", f"obligations/{ob['id']}/withdrawal", ob["withdrawal"])
    return rows


def context_decisions(state: dict, journal: dict | None = None) -> list[dict]:
    """Non-map owner decisions; derived continuity and Governor verdicts are not owner decisions."""
    rows = []
    def add(kind, reference, value):
        rows.append({"kind": kind, "reference": reference, "decision": decision_channel(value)})
    for index, value in enumerate(state.get("constraints", [])):
        if value.get("provenance") in ("user_direct", "agent_decision"):
            add("constraint", f"constraints/{index}", value)
    for index, value in enumerate(((state.get("checkpoint") or {}).get("continuity") or {}).get("history", [])):
        if isinstance(value.get("decision"), dict):
            add("handoff" if value["provenance"] == "owner_handoff" else "continuity", f"continuity/history/{index}", value["decision"])
    if isinstance(state.get("continued_by"), dict):
        add("claim", "continued_by", state["continued_by"])
    if isinstance(state.get("historical_retirement"), dict):
        add("retirement", "historical_retirement", state["historical_retirement"])
    for index, value in enumerate(state.get("standing_authorization", {}).get("decisions", [])):
        add("standing_authorization", f"standing_authorization/decisions/{index}", value)
    from .contracts import TOOL_QUESTION_SCHEMA
    questions = (state.get("checkpoint") or {}).get("questions", [])
    for value in questions if isinstance(questions, list) else []:
        if isinstance(value, dict) and value.get("schema") == TOOL_QUESTION_SCHEMA and "decision" in value:
            from .relay import validate_decision_record
            add("tool_permission" if value["purpose"] == "memory" else "tool_operation",
                f"checkpoint/questions/{value['id']}/decision", validate_decision_record(value["decision"]))
    for identity, admission in state.get("admissions", {}).items():
        for index, failure in enumerate(admission.get("failures", [])):
            if failure.get("cleared_by") in ("user", "agent"):
                add("route_failure_clear", f"admissions/{identity}/failures/{index}/clear_decision", failure.get("clear_decision", {}))
    for value in (journal or {}).get("actions", []):
        for field, kind in (("authorization", "authorization"),):
            decision = value.get("action", {}).get(field)
            if isinstance(decision, dict) and "standing" not in decision and "carried_from" not in value:
                add(kind, f"governor/{value['record_id']}/{field}", decision)
        if isinstance(value.get("exception"), dict) and "carried_from" not in value:
            add("governor_exception", f"governor/{value['record_id']}/exception", value["exception"])
    return rows


def protect_decision_record(value: dict) -> None:
    """Protect supported decision positions without interpreting opaque user content."""
    from .standing import SCHEMA as STANDING_SCHEMA, STATUS_SCHEMA as STANDING_STATUS_SCHEMA
    from .contracts import TOOL_QUESTION_SCHEMA, STATE_SCHEMAS
    def representation(item):
        if isinstance(item, dict):
            return (5 if "decision_binding" in item or "decision_order" in item or item.get("decision_channels") == 5 or item.get("schema") == TOOL_QUESTION_SCHEMA
                   else 4 if item.get("operation") == "issue_revision" and "relay_verification" in item
                   or item.get("decision_channels") == 4
                   else 3 if item.get("schema") in (STANDING_SCHEMA, STANDING_STATUS_SCHEMA)
                   or item.get("schema") == "pod-authorization/v1" and "standing" in item
                   or item.get("decision_channels") == 3
                   else 2 if "relay_verification" in item or item.get("decision_channels") == 2
                   else 1 if (CHANNEL_FIELDS | {"decision_channels"}) & item.keys() else 0)
        return 0

    containers = [value]
    if value.get("schema") == STATE_SCHEMAS["context"] and isinstance(value.get("checkpoint"), dict):
        containers.append(value["checkpoint"])
    for container in tuple(containers):
        report = (container.get("closure") or {}).get("report")
        if isinstance(report, dict):
            containers.append(report)
    records = list(containers)
    for container in containers:
        # Inventories add a default session channel for old rows. That projection
        # does not mean a channel was stored, so it must not raise their reader floor.
        decisions = (context_decisions(container) if container.get("schema") == STATE_SCHEMAS["context"] else [])
        if container.get("schema") == STATE_SCHEMAS["checkpoint"]:
            decisions += map_decisions(container)
        for row in decisions:
            decision = row["decision"]
            if decision.get("channel_recorded") is False:
                decision = {key: item for key, item in decision.items() if key not in ("channel", "channel_recorded")}
            records.append(decision)
        records.extend((container.get("standing_authorization"), container.get("history")))
        records.extend(row.get("decision") for row in container.get("owner_decisions", []))
        continuity = container.get("continuity") or {}
        for row in continuity.get("history", []):
            records.extend((row.get("decision"), row.get("resolution")))
        questions = container.get("questions", [])
        if isinstance(questions, list):
            records.extend(row for row in questions if isinstance(row, dict) and row.get("schema") == TOOL_QUESTION_SCHEMA)
        # Carried journal decisions still require their reader floor even though
        # they are excluded from the current owner's decision inventory.
        for row in container.get("actions", []):
            records.extend(((row.get("action") or {}).get("authorization"), row.get("exception")))
    version = max(map(representation, records), default=0)
    if version:
        value["decision_channels"] = version
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
    from .history_tree import plain
    value = plain(value)
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
    from .history_tree import plain
    value = plain(value)
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
        from .contracts import TABLE, bounded_fields, input_action
        if name in TABLE:
            shape["bounded_fields"] = bounded_fields(name)
            shape["next_action"] = input_action(name)
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


LEAD_ROUTE_AUTHORITY = ("The coordinator relays the project lead's delegated decision; Pod does not authenticate "
                        "the lead. It supplies no review proof, receipts, REUSE or provider authority.")


def route_occurrence(row: dict) -> int:
    decision = row.get("route_decision") or {}
    return decision.get("mismatch_occurrence", 1 if route_flags(row)[0] else 0)


def validate_route_settlements(row: dict) -> None:
    """Validate stored settlement shapes; recorded flags alone never settle a hold."""
    decision = row.get("route_decision") or {}
    occurrence = route_occurrence(row)
    entries = decision.get("settlements", [])
    if (type(occurrence) is not int or occurrence < 0
            or "mismatch_occurrence" in decision and (occurrence < 1 or not route_flags(row)[0])
            or not isinstance(entries, list) or entries and "mismatch_occurrence" not in decision):
        raise PodError("state_unsupported", "Route mismatch occurrence or settlements are malformed")
    last = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise PodError("state_unsupported", "Route settlement must be a record")
        basis = entry.get("basis")
        common = {"basis", "occurrence", "utc"}
        specific = ({"report_digest"} if basis == "matching_report" else
                    {"replacement", "seq"} if basis == "redone" else
                    {"decision", "source", "reason", "seq"} if basis == "lead_decision" else None)
        if specific is None:
            raise PodError("state_unsupported", "Route settlement basis is unsupported")
        if (basis == "lead_decision" and row.get("role") in ("review", "investigate")
                and entry.get("decision") == "discard"):
            specific |= {"corrections"}
        allowed = common | specific | ({"seq"} if basis == "matching_report" else set())
        if (set(entry) - allowed or not (common | specific) <= set(entry)
                or type(entry.get("occurrence")) is not int or not last < entry["occurrence"] <= occurrence
                or "seq" in entry and (type(entry["seq"]) is not int or entry["seq"] < 1)):
            raise PodError("state_unsupported", "Route settlement fields or occurrence are malformed")
        normalize_timestamp(entry["utc"], field="Route settlement UTC", code="state_unsupported")
        last = entry["occurrence"]
        if basis == "matching_report":
            consumed = row.get("report") or {}
            if (not isinstance(entry["report_digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["report_digest"]) is None
                    or entry["report_digest"] != consumed.get("observation_digest")):
                raise PodError("state_unsupported", "Route settlement does not bind its consumed report")
        elif basis == "redone":
            if (row.get("role") != "implement" or not isinstance(entry["replacement"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["replacement"]) is None
                    or entry["replacement"] == row.get("admission_id")):
                raise PodError("state_unsupported", "Route replacement identity is malformed")
        else:
            if (entry["decision"] not in ("accept", "discard")
                    or not isinstance(entry["source"], dict) or set(entry["source"]) != {"reference"}
                    or row.get("role") in ("review", "investigate")
                       and entry["decision"] == "accept" and row.get("report") is None):
                raise PodError("state_unsupported", "Relayed project lead route decision is malformed")
            try:
                bounded_text(entry["source"]["reference"], name="reference", limit=bound("route settlement source", "reference"))
                bounded_text(entry["reason"], name="reason", limit=bound("route settlement", "reason"))
            except PodError:
                raise PodError("state_unsupported", "Relayed project lead source or reason is malformed") from None
            if "corrections" in entry:
                ids = entry["corrections"]
                if (not isinstance(ids, list) or len(ids) > bound("route settlement", "corrections", "max_items")
                        or any(not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key) is None for key in ids)
                        or len(ids) != len(set(ids)) or row.get("report") is not None):
                    raise PodError("state_unsupported", "Terminal route discard corrections are malformed")


def route_hold(row: dict) -> bool:
    """Raw mismatch history holds only its current, unsettled occurrence."""
    validate_route_settlements(row)
    return route_flags(row)[0] and not any(entry["occurrence"] == route_occurrence(row)
                                         for entry in (row.get("route_decision") or {}).get("settlements", []))


def flag_route_mismatch(row: dict) -> None:
    if route_discarded(row):
        raise PodError("route_settled_discarded", "Project lead: this admission was terminally discarded through route_settlements; use a fresh admission for further proof")
    decision = row["route_decision"]
    occurrence = route_occurrence(row)
    if not route_hold(row):
        occurrence += 1
    decision.update(route_mismatch=True, mismatch_occurrence=occurrence)


def route_discarded(row: dict, *, pending=()) -> bool:
    """Terminal review/investigation discard, including an uncommitted map intent.

    Pending input affects only validation inside that transaction. Every intent is
    fully validated against the completed map before any admission is changed.
    """
    if row.get("role") not in ("review", "investigate"):
        return False
    validate_route_settlements(row)
    if any(entry.get("basis") == "lead_decision" and entry.get("decision") == "discard"
           for entry in (row.get("route_decision") or {}).get("settlements", [])):
        return True
    return isinstance(pending, list) and any(
        isinstance(entry, dict) and entry.get("admission") == row.get("admission_id")
        and entry.get("basis") == "lead_decision" and entry.get("decision") == "discard" for entry in pending)


def matching_route_settlement(row: dict, *, seq: int | None = None) -> bool:
    if not route_hold(row):
        return False
    row["route_decision"].setdefault("mismatch_occurrence", route_occurrence(row))
    from .relay import decision_commit_time
    row["route_decision"].setdefault("settlements", []).append({
        "basis": "matching_report", "occurrence": route_occurrence(row),
        "utc": decision_commit_time(),
        **({"seq": seq} if seq is not None else {}), "report_digest": row["report"]["observation_digest"]})
    return True


def route_next_action(row: dict, *, ended: bool = False) -> str:
    key = row.get("admission_id", "<admission>")
    if ended:
        return (f"Project lead: for admission {key}, record any remaining tab follow-up "
                "in the successor or repository task; this ended record accepts no settlement write and supplies no close command.")
    implementation = row.get("role") == "implement"
    replacement = ("for an implementation, disposition it and record route_settlements basis redone "
                   "for a qualifying replacement; " if implementation else "")
    if implementation:
        decision = "use decision discard with ordinary disposition discarded"
        if row.get("report") is not None or (row.get("result") or {}).get("base") is None:
            decision = "use decision accept with ordinary disposition integrated_into, or decision discard with ordinary disposition discarded"
    elif row.get("report") is not None:
        decision = "use decision accept for the later route evidence while the consumed report stands as recorded"
    else:
        decision = ("use decision discard after native settlement, with corrections listing every known required finding as an ordinary correction id "
                    "(or [] with a reason explaining why none are known)")
    return (f"Project lead: for admission {key}, consume its report with internal report after a matching readback; "
            + replacement + "otherwise record route_settlements basis lead_decision with the lead's source and "
            "reason in internal checkpoint: " + decision + ". " + LEAD_ROUTE_AUTHORITY)


def route_settlement_projection(rows: list[dict]) -> dict:
    entries = [{"admission": row["admission_id"], **entry} for row in rows
               for entry in (row.get("route_decision") or {}).get("settlements", [])]
    return ({"route_settlements": entries[-64:], "route_settlements_truncated": len(entries) > 64,
             **({"route_settlement_authority": LEAD_ROUTE_AUTHORITY}
                if any(entry["basis"] == "lead_decision" for entry in entries) else {})} if entries else {})


def route_summary(rows: list[dict], *, unknown_states: tuple[str, ...] | None = None) -> tuple[bool, bool]:
    flagged = [(row, route_flags(row)) for row in rows]
    return (any(route_hold(row) for row, _ in flagged),
            any(flags[1] and (unknown_states is None or row.get("state") in unknown_states)
                for row, flags in flagged))


def bounded_text(value: Any, *, name: str, limit: int | None = None) -> str:
    if limit is None:
        from .contracts import bound
        limit = bound("packet", "objective")
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        code_name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
        length = len(value) if isinstance(value, str) else None
        raise PodError("invalid_" + code_name,
                       f"{name} must be bounded, nonempty text (limit {limit}, length {length})",
                       {"field": name, "limit": limit, "length": length})
    return value

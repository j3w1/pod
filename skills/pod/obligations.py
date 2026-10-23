"""The obligation map kernel: records every coordination decision must leave.

Work starts only for a recorded, authorized obligation; every unfinished obligation is
either advanced or waits on one named, checkable reason; an undispositioned settled
result with incomplete path evidence holds new implementation; `independently reviewed`
is derived from bound assurance records;
an objective closes only when its obligations are satisfied or withdrawn.

This module is deterministic over its inputs. It makes no model call, reads nothing and
writes nothing. The ledger supplies objective-local facts under the objective lock
(admission rows, outstanding reservations, the ceiling, governance text at the bound
base commit, source, authority and Git observations) and persists only what is accepted
here. The same predicates serve the checkpoint, admission, report, acceptance and
Governor boundaries, status, and the recorded-trace checks; there is no second copy.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Callable

from .contracts import (TABLE, UNIT, IDENTIFIER, SURFACE, SOURCE, LINES, GOVERNANCE_PATHS, KINDS, PROVENANCES, STATES,
                        WAIT_CLASSES, PARTIES, ROLES, VERDICTS, PROPOSAL_SOURCES, bound, fields, required,
                        SEVERITIES, TRIAGE, input_record, annotate, current)
from .errors import PodError
from .gitio import OBJECT_ID
from .util import (digest, exact, _field_names, owner_decision, decision_channel, channel_fields,
                   CHANNEL_FIELDS, DECISION_FIELDS, route_hold, route_flags, route_discarded,
                   route_next_action, route_occurrence, validate_route_settlements)
import unicodedata

from .lineage import Lineage, FROZEN, DEFINITION as RENEW_DEFINITION

TERMINAL = ("satisfied", *FROZEN)
EXTERNAL_WAITS = ("authority", "user_hold", "input_unavailable")
AUTHORIZED_SCOPES = ("publish", "merge", "release", "deploy")
MAX_OBLIGATIONS = bound("map", "obligations", "max_items")
MAX_PROPOSALS = bound("map", "proposals", "max_items")
MAX_SERVES = bound("packet", "serves", "max_items")
MAX_EVIDENCE = bound("obligation", "evidence", "max_items")
MAX_RECEIPTS = bound("obligation", "receipts", "max_items")
MAX_LIVE = bound("map", "obligations", "max_live")
MAX_FINDINGS = bound("obligation", "findings", "max_items")
MAX_REPORT_FINDINGS = bound("report request", "triage", "max_items")
MAX_ACCEPTANCES = bound("map", "acceptances", "max_items")
MAX_CITED_LINES = bound("policy citation", "lines", "max_lines")
MAX_GOVERNANCE_TEXT = 1_048_576
CHURN_THRESHOLD = 3

_ID = re.compile(IDENTIFIER)
_SURFACE = re.compile(SURFACE)
_LINES = re.compile(LINES)
_COMMIT = OBJECT_ID
_SOURCE = re.compile(SOURCE)
_ACCEPTANCE_FIELDS = fields("acceptance")

NEXT_ACTIONS = {
    "unbound_assignment": "admit only a packet that serves unsatisfied obligations of the current map revision",
    "ownership_conflict": "record an ownership wait on the overlapping holder, or narrow the boundary",
    "integration_pending": "record a disposition, or establish complete paths and a nonoverlapping boundary",
    "objective_closed": "only a direct user revision with reopen continues a closed objective",
    "obligation_invalid": "correct the obligation's provenance, parent, check or citation",
    "obligation_unaccounted": "restate every obligation with exactly one currently valid state",
    "wait_invalid": "restate the wait with one controlling class and a current referent",
    "map_stale": "re-read with pod internal map and write its next sequence number",
    "governance_changed": "write a checkpoint with governance_refresh to reconcile at the new base",
    "governance_unavailable": "restore the governance source read; new work holds until it is bound",
}

# Proposed map fields a caller may send; everything else in a stored map is stamped here.
MAP_INPUT = fields("map")
MAP_STORED = {"seq", "revision", "obligations", "proposals", "governance", "governance_sources",
              "coordinator_slot", "quiescence", "closure", "observations", "reopened",
              "governance_history", "delivery", "revision_decisions"}


def refuse(code: str, detail: str, message: str, *, next_action: str | None = None,
           accepted: set[str] | None = None, optional: set[str] | None = None, restate: list[str] | None = None,
           bounded_fields: dict | None = None,
           changed_fields: set[str] | None = None,
           **referent: Any) -> PodError:
    """One boundary refusal naming its detail, referent and next safe action."""
    actions = {
        "receipt_conflict": "preserve the immutable receipt; use a fresh reference or review attempt for fresh evidence",
        "finding_limit": "preserve every stored finding; consolidate only related new findings within the group/id bounds",
        "receipt_limit": "preserve receipt history; no receipt can be evicted to admit new proof",
        "row_limit": (f"keep every row; a map holds at most {MAX_LIVE} live and {MAX_OBLIGATIONS} total rows, "
                      "withdrawn and renewed rows leave the live count, so record fewer new rows in this write"),
        "malformed": "correct the named record or fields to the accepted shape and retry",
    }
    action = next_action or actions.get(detail) or NEXT_ACTIONS.get(code, "inspect the refused record")
    def safe(value):
        if isinstance(value, str):
            return "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))[:512]
        if isinstance(value, dict):
            return {safe(str(key))[:64]: safe(item) for key, item in list(value.items())[:8]}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value[:8]]
        return value
    referent = safe(referent)
    if changed_fields is not None:
        # These names are selected from the constant definition vocabulary.
        referent["fields"] = sorted(changed_fields)
    # Shape vocabulary comes from code, unlike the bounded caller referents above.
    if accepted is not None:
        referent.update(accepted=_field_names(accepted, complete=True)[0],
                        optional=_field_names(optional or set(), complete=True)[0])
    if bounded_fields is not None:
        referent["bounded_fields"] = bounded_fields
    if restate is not None:
        # Map ids have already passed the 96-row/64-character identifier bounds.
        referent["restate"] = list(restate)
    named = ", ".join(f"{key}={value}" for key, value in sorted(referent.items()))
    text = f"{detail}: {message}" + (f" ({named})" if named else "") + f". Next: {action}"
    return PodError(code, text, {"detail": detail, "referent": referent, "next_action": action})


def _text(value: Any, name: str, *, code: str = "obligation_invalid", detail: str = "malformed",
          limit: int | None = None, obligation: str | None = None) -> str:
    if limit is None:
        spec = TABLE.get(current(), {}).get("fields", {}).get(name, {})
        limit = spec.get("max_length", TABLE["obligation"]["fields"]["check"]["max_length"])
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        length = len(value) if isinstance(value, str) else None
        error = refuse(code, detail, f"{name} must be bounded, nonempty text (limit {limit}, length {length})",
                       field=name, limit=limit, length=length,
                       **({"obligation": obligation} if obligation is not None else {}))
        annotate(error, current())
        raise error
    return value


def _ident(value: Any, name: str, *, code: str = "obligation_invalid", obligation: str | None = None) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise refuse(code, "malformed", f"{name} must be a bounded identifier", field=name,
                     **({"obligation": obligation} if obligation is not None else {}))
    return value


def _exact(value: Any, fields: set[str], required: set[str], name: str, *,
           code: str = "obligation_invalid", obligation: str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - fields or required - set(value):
        from .contracts import bounded_fields
        contract = {"bounded_fields": bounded_fields(name)} if name in TABLE else {}
        if not isinstance(value, dict):
            raise refuse(code, "malformed", f"{name} must be a JSON object", record=name, expected="object", accepted=fields, optional=fields-required, **contract, **({"obligation": obligation} if obligation is not None else {}))
        missing, m = _field_names(required - set(value))
        unsupported, u = _field_names(set(value) - fields)
        known = obligation or value.get("id")
        referent = {"obligation": known} if isinstance(known, str) and _ID.fullmatch(known) else {}
        raise refuse(code, "malformed", f"{name} has missing or unsupported fields: missing {missing}; unsupported {unsupported}",
                     record=name, missing=missing, unsupported=unsupported, omitted=m+u,
                     accepted=fields, optional=fields-required, **contract, **referent)
    return value


# --------------------------------------------------------------------------- boundaries

def _path(value: Any, *, code: str, obligation: str | None = None) -> str:
    if (not isinstance(value, str) or not value or len(value) > bound("boundary", "paths", "items")["max_length"] or "\x00" in value
            or value.startswith("/") or "\\" in value):
        raise refuse(code, "malformed", "boundary paths are repo-relative", record="boundary path", **({"obligation": obligation} if obligation is not None else {}))
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise refuse(code, "malformed", "boundary paths stay inside the repository", record="boundary path", **({"obligation": obligation} if obligation is not None else {}))
    return "/".join(parts) or "."


@input_record("boundary")
def boundary(value: Any, *, code: str = "obligation_invalid", obligation: str | None = None) -> dict:
    """Declared editing boundary: repo-relative path prefixes plus named surfaces."""
    if value is None:
        return {"paths": [], "surfaces": []}
    record = _exact(value, fields("boundary"), required("boundary"), "boundary", code=code, obligation=obligation)
    paths, surfaces = record.get("paths", []), record.get("surfaces", [])
    if (not isinstance(paths, list) or len(paths) > bound("boundary", "paths", "max_items") or not isinstance(surfaces, list)
            or len(surfaces) > bound("boundary", "surfaces", "max_items")):
        raise refuse(code, "malformed", "boundary is a bounded list of paths and surfaces", record='boundary',
                     **({"obligation": obligation} if obligation is not None else {}))
    for surface in surfaces:
        if not isinstance(surface, str) or not _SURFACE.fullmatch(surface):
            raise refuse(code, "malformed", "boundary surfaces are bounded names", record='boundary',
                         **({"obligation": obligation} if obligation is not None else {}))
    return {"paths": sorted({_path(item, code=code, obligation=obligation) for item in paths}),
            "surfaces": sorted(set(surfaces))}


def _parts(path: str) -> tuple[str, ...]:
    return () if path == "." else tuple(path.split("/"))


def _prefix(a: str, b: str) -> bool:
    """Whether one path is a component-wise prefix of the other."""
    left, right = _parts(a), _parts(b)
    shorter = min(len(left), len(right))
    return left[:shorter] == right[:shorter]


def overlap(a: dict, b: dict) -> dict:
    """Deterministic overlap of two boundaries; empty lists mean none."""
    paths = sorted({x for x in a["paths"] for y in b["paths"] if _prefix(x, y)}
                   | {y for x in a["paths"] for y in b["paths"] if _prefix(x, y)})
    return {"paths": paths, "surfaces": sorted(set(a["surfaces"]) & set(b["surfaces"]))}


def overlaps(a: dict, b: dict) -> bool:
    found = overlap(a, b)
    return bool(found["paths"] or found["surfaces"])


def within(path: str, declared: dict) -> bool:
    parts = _parts(path)
    return any(_parts(item) == parts[:len(_parts(item))] for item in declared["paths"])


def exceeded(changed: list[str], declared: dict) -> list[str]:
    """Changed paths outside the declared path prefixes."""
    return sorted(path for path in changed if not within(path, declared))


# --------------------------------------------------------------------------- governance

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lines(text: str) -> list[str]:
    return text.split("\n")


def bind_governance(observed: Any, base_ref: str | None, exclude: list[str]) -> tuple[list[dict], str | None]:
    """The declared governance sources at one base commit: path, byte revision, base.

    A project outside Git has no base revision and therefore no governance source; a Git
    project names its target branch, and an unreadable base holds new work.
    """
    if base_ref is None:
        if not isinstance(observed, dict) or observed.get("status") != "no_repository":
            raise refuse("governance_unavailable", "governance_unavailable",
                         "a Git project binds governance at its selected target branch", record='bind governance')
        return [], None
    if (not isinstance(observed, dict) or observed.get("status") != "observed"
            or not isinstance(observed.get("commit"), str) or not _COMMIT.fullmatch(observed["commit"])):
        raise refuse("governance_unavailable", "governance_unavailable",
                     "governance sources could not be read at the base commit", base_ref=base_ref)
    texts = observed.get("texts", {})
    rows = []
    for path in GOVERNANCE_PATHS:
        if path in exclude:
            continue
        text = texts.get(path)
        revision = None if text is None else "sha256:" + _sha(text.encode())
        rows.append({"path": path, "revision": revision, "base": observed["commit"]})
    return rows, observed["commit"]


def governance_digest(sources: list[dict]) -> str:
    if not isinstance(sources, list) or len(sources) > len(GOVERNANCE_PATHS):
        raise refuse("obligation_invalid", "malformed", "governance sources are the bounded bound-source records", record="governance sources")
    paths = set()
    for raw in sources:
        row = _exact(raw, {"path", "revision", "base"}, {"path", "revision", "base"}, "governance source")
        if (not isinstance(row["path"], str) or row["path"] not in GOVERNANCE_PATHS or row["path"] in paths
                or not isinstance(row["base"], str) or _COMMIT.fullmatch(row["base"]) is None
                or row["revision"] is not None and (not isinstance(row["revision"], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", row["revision"]) is None)):
            raise refuse("obligation_invalid", "malformed", "governance source retains its unique path and bound revisions", record="governance source")
        paths.add(row["path"])
    return digest(sources)


def bound_governance_digest(state: dict) -> str:
    """A derived adoption keeps the byte-identical sources' original proof identity."""
    sources = state["governance_sources"]
    base = state["governance"].get("proof_base")
    return governance_digest([{**row, "base": base} for row in sources] if base is not None else sources)


@input_record("policy citation")
def _cite(source: Any, observed: dict | None, sources: list[dict], *, obligation: str | None = None) -> dict:
    record = _exact(source, fields("policy citation"), required("policy citation"), "policy citation", obligation=obligation)
    bound = {row["path"]: row for row in sources}
    if record["path"] not in bound or bound[record["path"]]["revision"] is None:
        raise refuse("obligation_invalid", "governance_source_unrecognized",
                     "project policy cites only a declared governance source bound at intake",
                     record="policy citation", field="path", obligation=obligation)
    texts = (observed or {}).get("texts", {})
    text = texts.get(record["path"])
    if (text is None or "sha256:" + _sha(text.encode()) != bound[record["path"]]["revision"]
            or (observed or {}).get("commit") != bound[record["path"]]["base"]):
        raise refuse("obligation_invalid", "cite_not_base",
                     "the cited source is not read at the objective's bound base revision",
                     path=record["path"], obligation=obligation)
    match = _LINES.fullmatch(record["lines"]) if isinstance(record["lines"], str) else None
    lines = _lines(text)
    if match is None:
        raise refuse("obligation_invalid", "cite_not_found", "cite a line range like 38-40", record='cite',
                     obligation=obligation)
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    if first > last or last > len(lines) or last - first >= MAX_CITED_LINES:
        raise refuse("obligation_invalid", "cite_not_found", "the cited lines are absent at the base",
                     path=record["path"], field="lines", obligation=obligation)
    quote = "\n".join(lines[first - 1:last])
    if not quote.strip():
        raise refuse("obligation_invalid", "cite_not_found", "the cited lines are blank at the base",
                     path=record["path"], field="lines", obligation=obligation)
    quote_sha = _sha(quote.encode())
    if record.get("revision") not in (None, bound[record["path"]]["revision"]):
        raise refuse("obligation_invalid", "cite_not_base", "the citation names another source revision",
                     path=record["path"], obligation=obligation)
    if record.get("quote_sha256") not in (None, quote_sha):
        raise refuse("obligation_invalid", "cite_not_base",
                     "the cited text differs from the base revision's text", path=record["path"], obligation=obligation)
    return {"path": record["path"], "lines": record["lines"],
            "revision": bound[record["path"]]["revision"], "base": bound[record["path"]]["base"],
            "quote_sha256": quote_sha, "gone": False}


def _relocate(source: dict, observed: dict, sources: list[dict]) -> dict:
    """Find the exact cited text at a new base. Moved text is not withdrawal."""
    bound = {row["path"]: row for row in sources}
    text = observed.get("texts", {}).get(source["path"])
    row = bound.get(source["path"])
    if text is None or row is None or row["revision"] is None:
        return {**source, "gone": True, "base": observed["commit"]}
    match = _LINES.fullmatch(source["lines"])
    count = int(match.group(2) or match.group(1)) - int(match.group(1)) + 1
    lines = _lines(text)
    for index in range(0, max(0, len(lines) - count + 1)):
        if _sha("\n".join(lines[index:index + count]).encode()) == source["quote_sha256"]:
            span = str(index + 1) if count == 1 else f"{index + 1}-{index + count}"
            return {**source, "lines": span, "revision": row["revision"], "base": row["base"],
                    "gone": False}
    return {**source, "gone": True, "revision": row["revision"], "base": row["base"]}


# --------------------------------------------------------------------------- records

_OBLIGATION_FIELDS = fields("obligation")
_DEFINITION = ("check", "resolves", "stop_condition", "scope", "question", "insufficiency",
               "existing_evidence", "uncovered_risk", "proof_scope")
_STATE_FIELDS = {"active": "executor", "waiting": "wait", "blocked_external": "external",
                 "withdrawn": "withdrawal", "renewed": "renewal"}


@input_record("obligation")
def _structure(raw: Any) -> dict:
    row = _exact(raw, _OBLIGATION_FIELDS, {"id", "kind", "provenance"}, "obligation")
    ob = dict(row)
    _ident(ob["id"], "obligation id")
    if ob["kind"] not in KINDS or ob["provenance"] not in PROVENANCES:
        raise refuse("obligation_invalid", "malformed", "kind allowed: " + ", ".join(KINDS) + "; provenance allowed: " + ", ".join(PROVENANCES), obligation=ob["id"])
    for key in ("check", "resolves", "stop_condition", "question", "insufficiency",
                "existing_evidence", "uncovered_risk"):
        if key in ob:
            _text(ob[key], key, obligation=ob["id"])
    if not ob.get("check") and not ob.get("resolves"):
        raise refuse("obligation_invalid", "no_check", "every obligation needs a check or the decision it resolves",
                     obligation=ob["id"])
    if ob.get("resolves") and not ob.get("stop_condition"):
        raise refuse("obligation_invalid", "no_check", "an investigation's resolves needs a stop_condition",
                     obligation=ob["id"])
    ob["boundary"] = boundary(ob.get("boundary"), obligation=ob["id"])
    if "proof_scope" in ob:
        scope = ob["proof_scope"]
        if ob["kind"] == "assurance" or not isinstance(scope, list) or len(scope) > bound("obligation", "proof_scope", "max_items"):
            raise refuse("obligation_invalid", "malformed", "proof_scope is a bounded path list on non-assurance obligations",
                         obligation=ob["id"], field="proof_scope")
        if not scope and (ob["kind"] != "subgoal" or ob["provenance"] != "coordinator"):
            raise refuse("obligation_invalid", "malformed", "empty proof_scope requires a coordinator subgoal",
                         obligation=ob["id"], field="proof_scope")
        ob["proof_scope"] = sorted({_path(path, code="obligation_invalid", obligation=ob["id"]) for path in scope})
    if "parent" in ob:
        _ident(ob["parent"], "parent", obligation=ob["id"])
        if ob["parent"] == ob["id"]:
            raise refuse("obligation_invalid", "no_parent", "an obligation cannot parent itself", obligation=ob["id"])
    if ob["kind"] in ("subgoal", "correction") or (ob["kind"] == "assurance" and ob["provenance"] == "coordinator"):
        if "parent" not in ob:
            raise refuse("obligation_invalid", "no_parent", "subgoal, correction and coordinator assurance name a parent",
                         obligation=ob["id"])
    if ob["kind"] == "assurance":
        for key in ("scope", "question", "candidate", "existing_evidence", "insufficiency"):
            if key not in ob:
                raise refuse("obligation_invalid", "assurance_incomplete",
                             "assurance records scope, question, candidate, existing evidence and insufficiency",
                             obligation=ob["id"])
        ob["scope"] = boundary(ob["scope"], obligation=ob["id"])
        _text(ob["candidate"], "candidate", limit=bound("obligation", "candidate"), obligation=ob["id"])
        if "unit" in ob and (not isinstance(ob["unit"], str) or not UNIT.fullmatch(ob["unit"])):
            raise refuse("obligation_invalid", "malformed", "unit is a Governor delivery-unit identifier",
                         field="unit", obligation=ob["id"])
    else:
        for key in ("scope", "question", "candidate", "existing_evidence", "insufficiency", "uncovered_risk",
                    "findings", "unit"):
            if key in ob:
                raise refuse("obligation_invalid", "malformed", f"only assurance records {key}", obligation=ob["id"])
    if "finding" in ob and ob["kind"] != "correction":
        raise refuse("obligation_invalid", "malformed", "only a correction records its finding", obligation=ob["id"])
    if "adopts" in ob:
        _ident(ob["adopts"], "adopted proposal", obligation=ob["id"])
    state = ob.get("state")
    if state is None:
        raise refuse("obligation_unaccounted", "missing_state", "every obligation has exactly one state",
                     obligation=ob["id"])
    if not isinstance(state, str) or state not in STATES:
        raise refuse("obligation_unaccounted", "multiple_states" if isinstance(state, list) else "missing_state",
                     "state allowed: " + ", ".join(STATES), obligation=ob["id"])
    for other, field in _STATE_FIELDS.items():
        if field in ob and other != state:
            raise refuse("obligation_unaccounted", "multiple_states",
                         f"{field} belongs to the {other} state, not {state}", obligation=ob["id"])
    if "reuse" in ob and state != "satisfied":
        raise refuse("obligation_unaccounted", "multiple_states", "reuse binds only a satisfied obligation",
                      obligation=ob["id"])
    if state in _STATE_FIELDS and _STATE_FIELDS[state] not in ob:
        raise refuse("obligation_unaccounted", "missing_state",
                     f"{state} needs its {_STATE_FIELDS[state]} record", obligation=ob["id"])
    evidence = ob.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise refuse("obligation_invalid", "malformed", "evidence is a bounded list", obligation=ob["id"])
    if "receipts" in ob and (not isinstance(ob["receipts"], list) or len(ob["receipts"]) > MAX_RECEIPTS):
        raise refuse("obligation_invalid", "malformed", "receipts is a bounded stored history", obligation=ob["id"], limit=MAX_RECEIPTS)
    if "acceptances" in ob:
        ob["acceptances"] = _acceptance_records(ob["acceptances"], obligation=ob["id"])
    if "pod_mapping" in ob:
        from .pod_mapping import validate
        validate(ob)
    return ob


@input_record("acceptance")
def _acceptance(raw: Any, fields: set[str], *, obligation: str | None) -> dict:
    """One coordinator judgement of an outside result; the source is the caller's opaque id."""
    row = _exact(raw, fields, fields, "acceptance", obligation=obligation)
    if not isinstance(row["source"], str) or not _SOURCE.fullmatch(row["source"]):
        raise refuse("obligation_invalid", "malformed",
                     "an acceptance source is 1-128 printable characters without spaces",
                     field="source", **({"obligation": obligation} if obligation is not None else {}))
    if row["verdict"] not in VERDICTS:
        raise refuse("obligation_invalid", "malformed", "verdict allowed: " + ", ".join(VERDICTS),
                     field="verdict", **({"obligation": obligation} if obligation is not None else {}))
    _text(row["candidate"], "candidate", limit=bound("acceptance", "candidate"), obligation=obligation)
    _text(row["evidence"], "evidence", limit=bound("acceptance", "evidence"), obligation=obligation)
    return row


def _acceptance_records(raw: Any, *, obligation: str) -> list[dict]:
    """The stored acceptance records of one obligation, each stamped with the sequence that wrote it."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ACCEPTANCES:
        raise refuse("obligation_invalid", "malformed", "acceptances are a bounded nonempty list",
                     obligation=obligation, field="acceptances")
    rows = []
    for item in raw:
        row = _acceptance(item, _ACCEPTANCE_FIELDS | {"seq"}, obligation=obligation)
        if type(row["seq"]) is not int or row["seq"] < 1:
            raise refuse("obligation_invalid", "malformed", "an acceptance retains its writing sequence",
                         obligation=obligation, field="seq")
        rows.append(row)
    if len({row["source"] for row in rows}) != len(rows):
        raise refuse("obligation_invalid", "malformed", "one acceptance per source", obligation=obligation)
    return rows


def _write_acceptances(rows: dict[str, dict], prior_rows: dict[str, dict], requests: Any, seq: int) -> None:
    """Carry recorded judgements, clear them on reopen, and append this write's new ones."""
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
        if ob["state"] in FROZEN:
            continue
        if earlier is None or not earlier.get("acceptances"):
            continue
        if (earlier["state"] == "satisfied" and ob["state"] != "satisfied"
                or earlier.get("definition") != ob.get("definition")):
            continue  # Reopened: its earlier judgements no longer describe it.
        ob["acceptances"] = deepcopy(earlier["acceptances"])
    if requests is None:
        return
    if not isinstance(requests, list) or len(requests) > MAX_ACCEPTANCES:
        raise refuse("obligation_invalid", "malformed", "acceptances are a bounded list of requests",
                     record="acceptances")
    for raw in requests:
        key = raw.get("obligation") if isinstance(raw, dict) else None
        if not isinstance(key, str) or key not in rows:
            raise refuse("obligation_invalid", "malformed", "an acceptance names an obligation in the map",
                         record="acceptances")
        ob = rows[key]
        if ob["state"] in FROZEN:
            raise refuse("obligation_invalid", "terminal_frozen", "terminal rows cannot receive acceptances", obligation=key)
        request = _acceptance({name: item for name, item in raw.items() if name != "obligation"},
                              _ACCEPTANCE_FIELDS, obligation=key)
        recorded = {row["source"]: row for row in ob.get("acceptances", [])}
        if request["source"] in recorded:
            earlier = {name: item for name, item in recorded[request["source"]].items() if name != "seq"}
            if earlier != request:
                raise refuse("obligation_invalid", "acceptance_conflict",
                             "a recorded acceptance is immutable; record a new result under a new source",
                             obligation=key, field="source")
            continue
        if len(recorded) >= MAX_ACCEPTANCES:
            raise refuse("obligation_invalid", "malformed", "an obligation keeps at most 16 acceptances",
                         obligation=key, field="acceptances")
        ob["acceptances"] = [*ob.get("acceptances", []), {**request, "seq": seq}]


@input_record("proposal")
def _proposal(raw: Any) -> dict:
    row = _exact(raw, fields("proposal"), required("proposal"), "proposal")
    _ident(row["id"], "proposal id")
    if row["source"] not in PROPOSAL_SOURCES or row["status"] not in TABLE["proposal"]["fields"]["status"]["allowed"]:
        raise refuse("obligation_invalid", "malformed", "proposal source allowed: " + ", ".join(PROPOSAL_SOURCES) + "; status allowed: open, adopted", proposal=row["id"])
    _text(row["origin_ref"], "origin_ref", limit=bound("proposal", "origin_ref"))
    _text(row["summary"], "summary")
    return dict(row)


@input_record("revision authority")
def _authority(value: Any, *, kind: str = "revision", stored: bool = False) -> dict | None:
    if value is None:
        return None
    record = _exact(value, DECISION_FIELDS | {"question"}, {"provenance", "instruction"}, "revision authority")
    if record["provenance"] != "user_direct":
        raise refuse("obligation_invalid", "provenance_unauthorized",
                     "a map revision is authorized only by a direct user instruction", record='authority')
    _text(record["instruction"], "instruction", limit=bound("revision authority", "instruction"))
    if "question" in record:
        _text(record["question"], "question", limit=bound("revision authority", "question"))
    return owner_decision(record, kind=kind, stored=stored)


# --------------------------------------------------------------------------- facts

def _admissions(ctx: dict) -> dict:
    return ctx.get("admissions", {})


def _outstanding(ctx: dict) -> list[str]:
    return list(ctx.get("outstanding", []))


def _admission_boundary(row: dict) -> dict:
    declared = row.get("boundary") or {"paths": [], "surfaces": []}
    changed = row.get("changed_paths") or []
    return {"paths": sorted(set(declared["paths"]) | set(changed)), "surfaces": declared["surfaces"]}


def undispositioned(ctx: dict) -> dict[str, dict]:
    """Settled implementation results without a disposition, by admission id."""
    outstanding = set(_outstanding(ctx))
    return {key: row for key, row in _admissions(ctx).items()
            if row.get("role") == "implement" and key not in outstanding
            and row.get("state") != "deferred" and row.get("disposition") is None}


def _serving(ctx: dict, obligation: str, *, outstanding_only: bool) -> list[str]:
    outstanding = set(_outstanding(ctx))
    return sorted(key for key, row in _admissions(ctx).items()
                  if obligation in (row.get("serves") or [])
                  and (key in outstanding) == outstanding_only)


def _settled_serving(ctx: dict, obligation: str) -> list[str]:
    return [key for key in _serving(ctx, obligation, outstanding_only=False)
            if _admissions(ctx)[key].get("state") != "deferred"]


# --------------------------------------------------------------------------- evidence

def definition_id(ob: dict) -> str:
    """Identity of what is verified, separate from state, candidate and test command.

    Revisions of other obligations and governance line relocation do not change this
    identity. Candidate, source, environment, dependency and policy checks remain separate.
    """
    value = {key: ob[key] for key in ("id", "kind", "provenance", "parent", *_DEFINITION)
             if key in ob}
    # Assurance scope is what the reviewer checks; its editing boundary is only ownership.
    if ob["kind"] != "assurance":
        value["boundary"] = boundary(ob.get("boundary"))
    if "scope" in value:
        value["scope"] = boundary(value["scope"])
    return digest(value)






















# --------------------------------------------------------------------------- write

def _first(value: dict, key: str, default: Any) -> Any:
    return value[key] if key in value else default


@input_record("governance")
def _write_governance(prior_map: dict | None, value: dict, ctx: dict,
                      user: bool, refresh: bool) -> tuple:
    # Governance: declared at intake, read at the base, refreshed only by an explicit revision.
    observed = ctx.get("governance")
    if prior_map is None:
        # A round-tripped stamped base is ignored: Pod binds the base it reads itself.
        declared = _exact(value.get("governance"), {"base_ref", "selection", "exclude", "base"},
                          {"base_ref"}, "governance")
        if declared["base_ref"] is not None:
            _text(declared["base_ref"], "base_ref", limit=bound("governance", "base_ref"))
        exclude = declared.get("exclude", [])
        if (not isinstance(exclude, list) or any(item not in GOVERNANCE_PATHS for item in exclude)
                or len(set(exclude)) != len(exclude)):
            raise refuse("obligation_invalid", "governance_source_unrecognized",
                         "governance exclusions allowed: " + ", ".join(GOVERNANCE_PATHS), record='governance')
        if exclude and not user:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "only a direct user constraint excludes a governance source", record='governance')
        target_ref = observed.get("target_ref") if isinstance(observed, dict) else None
        sources, base = bind_governance(observed, target_ref or declared["base_ref"], sorted(exclude))
        governance = {"base_ref": target_ref, "selection": observed.get("selection") if target_ref else None,
                      "exclude": sorted(exclude), "base": base}
    else:
        governance = prior_map["governance"]
        declared = value.get("governance")
        if declared is not None:
            if not isinstance(declared, dict) or set(declared) - {"base_ref", "selection", "exclude", "base", "proof_base"}:
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "governance sources are fixed at intake", record='governance')
            if declared.get("exclude", governance["exclude"]) != governance["exclude"]:
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "governance source paths are fixed at intake", record='governance')
            if declared.get("base_ref", governance["base_ref"]) != governance["base_ref"] and not (user and refresh):
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "a target change needs a direct user revision and governance refresh", record='governance')
        if refresh or ctx.get("derived_adoption"):
            target_ref = observed.get("target_ref") if isinstance(observed, dict) else None
            sources, base = bind_governance(observed, target_ref, governance["exclude"])
            previous_base = governance.get("proof_base", governance["base"])
            governance = {key: item for key, item in governance.items() if key != "proof_base"}
            governance = {**governance, "base_ref": target_ref,
                          "selection": observed.get("selection") if target_ref else None, "base": base}
            if ctx.get("derived_adoption"):
                governance["proof_base"] = previous_base
        else:
            sources = prior_map["governance_sources"]
    gov = bound_governance_digest({"governance": governance, "governance_sources": sources})
    rebind = value.get("rebind", [])
    if not isinstance(rebind, list) or len(rebind) > MAX_OBLIGATIONS:
        raise refuse("obligation_invalid", "malformed", "rebind is a bounded list of obligation ids", record='governance')
    if rebind and not refresh:
        raise refuse("obligation_invalid", "malformed", "evidence is rebound only by a governance refresh", record='governance')

    return observed, governance, sources, gov, rebind


def _write_proposals(prior_map: dict | None, value: dict) -> dict:
    # Proposals persist; only adoption changes their status.
    prior_proposals = {row["id"]: row for row in (prior_map or {}).get("proposals", [])}
    raw_proposals = _first(value, "proposals", list(prior_proposals.values()))
    if not isinstance(raw_proposals, list) or len(raw_proposals) > MAX_PROPOSALS:
        raise refuse("obligation_invalid", "malformed", "proposals are a bounded list", record='write proposals')
    proposals = {}
    for raw in raw_proposals:
        row = _proposal(raw)
        if row["id"] in proposals:
            raise refuse("obligation_invalid", "malformed", "proposal ids are unique", proposal=row["id"])
        earlier = prior_proposals.get(row["id"])
        if earlier is not None:
            if {k: v for k, v in row.items() if k != "status"} != {k: v for k, v in earlier.items() if k != "status"}:
                raise refuse("obligation_invalid", "malformed", "a proposal's record is immutable", proposal=row["id"])
            row["status"] = earlier["status"]
        elif row["status"] != "open":
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "a proposal is adopted only by a user or governance obligation", proposal=row["id"])
        proposals[row["id"]] = row
    for key in prior_proposals:
        if key not in proposals:
            raise refuse("obligation_invalid", "malformed", "a proposal cannot be dropped", proposal=key)

    return proposals


def _write_obligations(prior_map: dict | None, value: dict, ctx: dict, seq: int,
                       user: bool, triaged: frozenset, refresh: bool,
                       observed: dict | None, sources: list[dict], gov: str,
                       proposals: dict) -> tuple[list, dict, dict, list]:
    # Obligations: structure, stable identity, authority.
    raw_obligations = value["obligations"]
    if not isinstance(raw_obligations, list):
        row_bounds(raw_obligations)
    prior_rows = {row["id"]: row for row in (prior_map or {}).get("obligations", [])}
    # Omission preserves terminal history in its original position. Live rows cannot disappear.
    requested = {row.get("id"): row for row in raw_obligations if isinstance(row, dict) and isinstance(row.get("id"), str)}
    raw_obligations = list(raw_obligations)
    for index, (key, row) in enumerate(prior_rows.items()):
        if key not in requested and row["state"] in FROZEN:
            raw_obligations.insert(min(index, len(raw_obligations)), deepcopy(row))
    value["obligations"] = raw_obligations
    row_bounds(raw_obligations, request_entries=ctx.get("request_entries", 0))
    rows: dict[str, dict] = {}
    for raw in raw_obligations:
        earlier = prior_rows.get(raw.get("id")) if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
        if earlier is not None and earlier["state"] in FROZEN and raw != earlier:
            raise refuse("obligation_invalid", "terminal_frozen", "terminal rows may be omitted or restated byte-identically",
                         obligation=earlier["id"])
        for field in ("renews", "renewal"):
            if isinstance(raw, dict) and raw.get(field) != (earlier or {}).get(field) and raw.get("id") not in ctx.get("renewal_rows", set()):
                raise refuse("obligation_invalid", "renewal_kernel_field", "renewal links are stored-only fields", field=field)
        if isinstance(raw, dict) and "acceptances" in raw and isinstance(raw.get("id"), str) and (
                raw["id"] not in prior_rows or raw["acceptances"] != prior_rows[raw["id"]].get("acceptances")):
            raise refuse("obligation_invalid", "acceptance_kernel_field",
                         "acceptances are recorded through the map-level acceptances list",
                         field="acceptances", **({"obligation": raw["id"]} if isinstance(raw.get("id"), str) else {}))
        ob = _structure(raw)
        if ob["id"] in rows or ob["id"] in proposals:
            raise refuse("obligation_invalid", "malformed", "obligation ids are unique", obligation=ob["id"])
        rows[ob["id"]] = ob
    if ctx.get("brief") and ctx.get("candidate") is None:
        satisfied = [ob["id"] for ob in rows.values() if ob["state"] == "satisfied"]
        if satisfied:
            raise refuse("obligation_invalid", "malformed", "satisfied obligations need candidate for brief validation",
                         field="candidate", obligations=",".join(satisfied[:8]), remaining=max(0, len(satisfied) - 8))
    for key in prior_rows:
        if key not in rows:
            raise refuse("obligation_unaccounted", "missing_state", "an obligation cannot be dropped; withdraw it",
                         obligation=key)
    criteria = ctx.get("criteria", [])
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
        if earlier is not None and (earlier["state"] in FROZEN or ob["state"] == "renewed" and ob["id"] in ctx.get("renewal_rows", set())):
            if (refresh or ctx.get("derived_adoption")) and ob["provenance"] == "project_policy":
                ob["source"] = _relocate(earlier["source"], observed, sources)
            continue
        if ob["kind"] == "assurance" and ctx.get("candidate_check") is not None:
            ctx["candidate_check"](ob["candidate"])
        ob["definition"] = definition_id(ob)
        if earlier is not None:
            for key in ("kind", "provenance", "parent", "unit"):
                if ob.get(key) != earlier.get(key):
                    raise refuse("obligation_invalid", "redefinition_unauthorized",
                                 f"an obligation's {key} is immutable", obligation=ob["id"])
            redefined = (any(ob.get(key) != earlier.get(key) for key in _DEFINITION if key not in ("scope", "proof_scope"))
                         or ob["kind"] == "assurance" and ob["scope"] != earlier.get("scope"))
            if redefined and user:
                owner_decision(ctx["revision_authority"], kind="question" if ctx["authority_kind"] == "question" else "revision")
            if not user and redefined:
                raise refuse("obligation_invalid", "redefinition_unauthorized",
                             "only a direct user revision redefines an obligation's check", obligation=ob["id"])
            ob["introduced_seq"] = earlier["introduced_seq"]
            source = earlier.get("source")
            if (refresh or ctx.get("derived_adoption")) and ob["provenance"] == "project_policy" and isinstance(source, dict):
                source = _relocate(source, observed, sources)
            if source is not None:
                ob["source"] = source
            else:
                ob.pop("source", None)
            if earlier.get("adopts") != ob.get("adopts"):
                raise refuse("obligation_invalid", "redefinition_unauthorized", "adoption is recorded once",
                             obligation=ob["id"])
            if "acceptances" in ob and ob["acceptances"] != earlier.get("acceptances"):
                raise refuse("obligation_invalid", "acceptance_kernel_field",
                             "acceptances are recorded through the map-level acceptances list",
                             obligation=ob["id"], field="acceptances")
            ob.pop("acceptances", None)
            ob.pop("finding", None)
            if earlier.get("finding") is not None:
                ob["finding"] = earlier["finding"]
            if ob["id"] not in triaged:
                ob.pop("findings", None)
                if earlier.get("findings") is not None:
                    ob["findings"] = earlier["findings"]
            continue
        if ob["id"] not in triaged and ob["id"] not in ctx.get("renewal_new", set()) and ("finding" in ob or "findings" in ob):
            raise refuse("obligation_invalid", "malformed", "review findings are recorded only by triage",
                         obligation=ob["id"])
        if "acceptances" in ob:
            raise refuse("obligation_invalid", "acceptance_kernel_field",
                         "acceptances are recorded through the map-level acceptances list",
                         obligation=ob["id"], field="acceptances")
        if ob.get("introduced_seq") not in (None, seq):
            raise refuse("obligation_invalid", "malformed", "a new obligation is introduced at this write",
                         obligation=ob["id"], seq=seq)
        ob["introduced_seq"] = seq
        if ob["id"] in ctx.get("renewal_new", set()):
            if (refresh or ctx.get("derived_adoption")) and ob["provenance"] == "project_policy":
                ob["source"] = _relocate(ob["source"], observed, sources)
            continue
        if ob["provenance"] == "user_direct" and isinstance(ob.get("source"), dict):
            authority = ctx.get("revision_authority") or {}
            if "instruction" in ob["source"]:
                _user_instruction(ob["source"], ob["id"])
            if CHANNEL_FIELDS & set(ob["source"]):
                source_authority = owner_decision(ob["source"], kind="question" if "question" in authority else "revision")
                if channel_fields(source_authority) != channel_fields(authority):
                    raise refuse("obligation_invalid", "provenance_unauthorized", "obligation source channel must match its revision authority", obligation=ob["id"])
            source = {**ob["source"], **channel_fields(authority)}
            if "question" in authority:
                source["question"] = authority["question"]
            ob["source"] = owner_decision(source, kind="question" if "question" in authority else "revision")
        _introduce(ob, prior_map is None, user, observed, sources, criteria,
                   issue_revision=ctx.get("issue_revision") is not None)
    lineage = Lineage(rows)
    ctx["lineage"] = lineage
    for ob in rows.values():
        if "parent" in ob:
            parent = rows.get(ob["parent"])
            if parent is None:
                raise refuse("obligation_invalid", "no_parent", "the parent is not in the map", obligation=ob["id"])
            if ob["id"] not in prior_rows and ob["id"] not in ctx.get("renewal_new", set()) and lineage.effective(ob["parent"]) != ob["parent"]:
                raise refuse("obligation_invalid", "parent_renewed", "new obligations name the live parent tip",
                             obligation=ob["id"], parent=lineage.effective(ob["parent"]))
            if ob["kind"] == "correction" and parent["kind"] != "assurance":
                raise refuse("obligation_invalid", "no_parent", "a correction is parented to an assurance obligation",
                             obligation=ob["id"])
        if "adopts" in ob and ob["id"] not in prior_rows and ob["id"] not in ctx.get("renewal_new", set()):
            proposal = proposals.get(ob["adopts"])
            if ob["provenance"] not in ("user_direct", "project_policy"):
                raise refuse("obligation_invalid", "provenance_unauthorized",
                             "only a direct user revision or a governance citation adopts a proposal",
                             obligation=ob["id"])
            if proposal is None or proposal["status"] != "open":
                raise refuse("obligation_invalid", "malformed", "the adopted proposal is not open",
                             obligation=ob["id"], proposal=ob["adopts"])
            proposal["status"] = "adopted"
    _ancestry(rows, lineage=lineage)
    for ob in rows.values():
        if ob["kind"] == "assurance" and ob["id"] not in prior_rows and ob["id"] not in ctx.get("renewal_new", set()) and ob["state"] not in FROZEN:
            for other in rows.values():
                recorded = {**other, "receipts": prior_rows.get(other["id"], {}).get("receipts", [])}
                if (other is not ob and other["kind"] == "assurance"
                        and other["state"] != "renewed" and (other["state"] != "withdrawn"
                             or _accepted_assurance_current(recorded, ctx, gov))
                        and (other["candidate"] == ob["candidate"]
                             or _accepted_assurance_current(recorded, ctx, gov))
                        and overlaps(other["scope"], ob["scope"])
                        and not ob.get("uncovered_risk")):
                    raise refuse("obligation_invalid", "missing_uncovered_risk",
                                 "an overlapping assurance on the same candidate names the risk it adds",
                                 obligation=ob["id"], overlaps=other["id"])

    from .pod_mapping import write as write_mapping
    write_mapping(prior_map, value, rows, ctx, seq)
    return raw_obligations, prior_rows, rows, criteria


def _write_evidence(rows: dict, prior_rows: dict, rebind: list[str], triaged: frozenset,
                    ctx: dict, gov: str, user: bool, authority: dict | None, seq: int) -> dict:
    # Withdrawals and evidence stamping.
    reuse_failures = {}
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
        if earlier is not None and (earlier["state"] in FROZEN or ob["state"] == "renewed"):
            continue
        if ob["kind"] == "assurance":
            for finding in ob.get("findings", []):
                if finding.get("triage") == "required_correction" and "recorded_seq" not in finding:
                    child = rows.get(finding.get("correction"))
                    if child is not None and child.get("finding", {}).get("attempt") == finding.get("attempt"):
                        finding["recorded_seq"] = child["introduced_seq"]
        if earlier is not None and earlier["state"] == "withdrawn":
            if ob["state"] != "withdrawn" or ob["withdrawal"] != earlier["withdrawal"]:
                raise refuse("obligation_invalid", "withdrawal_unauthorized", "withdrawal is terminal",
                             obligation=ob["id"])
        elif ob["state"] == "withdrawn":
            revision = ctx.get("issue_revision")
            if (revision is not None and ob["id"] in revision.withdrawn_ids
                    and ob["withdrawal"].get("by") == "issue_revision"):
                record = _exact(ob["withdrawal"], {"by", "reason"}, {"by", "reason"}, "issue revision withdrawal")
                _text(record["reason"], "withdrawal reason", limit=1024)
                ob["withdrawal"] = {**record, "decision": seq}
            else:
                ob["withdrawal"] = _withdrawal(ob, user, authority)
        if ob["id"] in rebind and ob["provenance"] == "project_policy" and ob.get("source", {}).get("gone"):
            raise refuse("obligation_unaccounted", "evidence_invalidated",
                         "evidence for a policy whose cited text is gone cannot be rebound", obligation=ob["id"])
        reported = [item[1] for item in triaged
                    if isinstance(item, tuple) and len(item) == 2 and item[0] == "review_report"
                    and item[1] in _admissions(ctx)
                    and _admissions(ctx)[item[1]].get("role") == "review"
                    and _admissions(ctx)[item[1]].get("serves") == [ob["id"]]]
        if len(reported) > 1:
            raise refuse("obligation_invalid", "malformed", "one report consumes one review attempt", record='evidence')
        ob["evidence"] = _stamp_evidence(ob, earlier, ctx, gov, ob["id"] in rebind,
                                          reported_attempt=reported[0] if reported else None, seq=seq)
        try:
            reuse = _bind_reuse(ob, earlier, ctx)
        except PodError as exc:
            if (exc.detail or {}).get("detail") != "evidence_invalidated":
                raise
            reuse_failures[ob["id"]] = exc
            reuse = None
        if reuse is not None:
            ob["reuse"] = reuse
        if not ob["evidence"]:
            ob.pop("evidence")
    missing_rebind = [key for key in rebind if key not in rows]
    if missing_rebind:
        raise refuse("obligation_invalid", "malformed", "rebind names unknown obligations",
                     obligation=missing_rebind[0])

    return reuse_failures


def row_bounds(rows: Any, *, request: bool = False, request_entries: int | None = None) -> None:
    if not isinstance(rows, list):
        raise refuse("obligation_invalid", "malformed", "obligations are a bounded list", record="obligation map")
    live = sum(not isinstance(row, dict) or row.get("state") not in FROZEN for row in rows)
    total = len(rows)
    name = "request_entries" if request else "hydrated_total"
    if total > MAX_OBLIGATIONS or live > MAX_LIVE:
        bound_name = name if total > MAX_OBLIGATIONS else "live_rows"
        raise refuse("obligation_invalid", "row_limit", "the obligation row bound is full",
                     bound=bound_name, request_entries=total if request else request_entries,
                     hydrated_total=total, live=live,
                     proposed=total if total > MAX_OBLIGATIONS else live,
                     limit=MAX_OBLIGATIONS if total > MAX_OBLIGATIONS else MAX_LIVE)


def _renew(prior: dict | None, value: dict, ctx: dict, seq: int) -> dict:
    if "renew" not in value:
        return value
    if ctx.get("operation", "checkpoint") != "checkpoint" or prior is None:
        raise refuse("obligation_invalid", "renewal_checkpoint_only", "renewal uses an existing checkpoint map", record="renewal request")
    entries = value["renew"]
    if not isinstance(entries, list) or len(entries) > bound("map", "renew", "max_items"):
        raise refuse("obligation_invalid", "malformed", "renew is a bounded list", record="renewal request")
    before = {row["id"]: row for row in prior["obligations"]}
    raw = deepcopy(value.get("obligations", prior["obligations"]))
    if not isinstance(raw, list):
        row_bounds(raw)
    supplied = {row.get("id"): row for row in raw if isinstance(row, dict) and isinstance(row.get("id"), str)}
    replacements, ids = {}, set(before) | set(supplied)
    generated = []
    from .lineage import copied_identity
    for entry in entries:
        if not isinstance(entry, dict):
            raise refuse("obligation_invalid", "malformed", "renew entries are objects", record="renewal request")
        changed = set(entry) & set(RENEW_DEFINITION)
        if changed:
            raise refuse("obligation_invalid", "renewal_changed_definition", "renewal cannot supply definition fields",
                         changed_fields=changed, next_action="use the existing authorized revision path for changed definitions")
        _exact(entry, fields("renewal request"), required("renewal request"), "renewal request")
        old_id, new_id = _ident(entry["id"], "renewed id"), _ident(entry["as"], "replacement id")
        old = before.get(old_id)
        if old is None or old["state"] in FROZEN or old_id in replacements or old_id in replacements.values():
            raise refuse("obligation_invalid", "renewal_unavailable", "renew the current live row once per write", obligation=old_id)
        if new_id in ids or any(isinstance(item, dict) and item.get("id") == new_id for item in entries):
            raise refuse("obligation_invalid", "renewal_id_conflict", "renewal needs a fresh id and no same-write chains", obligation=new_id)
        restatable = restatable_rows({"obligations": [old]})[0]
        if old_id in supplied and supplied[old_id] not in (old, restatable):
            changed = [key for key in RENEW_DEFINITION if supplied[old_id].get(key) != old.get(key)]
            if supplied[old_id].get("source") == restatable.get("source") and "source" in changed:
                changed.remove("source")
            raise refuse("obligation_invalid", "renewal_changed_definition" if changed else "renewal_changed_row",
                         "renewal copies the recorded row unchanged", changed_fields=set(changed), obligation=old_id,
                         next_action=("use the existing authorized revision path for changed definitions" if changed else
                                      "omit the renewed row from obligations or update; put replacement state fields in its renew entry"))
        pending = [key for key, admission in _admissions(ctx).items() if old_id in admission.get("serves", [])
                   and not route_discarded(admission, pending=ctx.get("pending_route_settlements", ()))
                   and (key in _outstanding(ctx) or admission.get("state") != "deferred"
                        and (admission.get("report") is None and admission.get("disposition") is None
                             or admission.get("role") == "implement" and admission.get("disposition") is None))]
        if pending:
            admission = _admissions(ctx)[pending[0]]
            implementation = admission.get("role") == "implement"
            action = (f"record an ordinary disposition for implementation admission {pending[0]} before retrying renewal"
                      if implementation else f"settle and consume the report for admission {pending[0]} before retrying renewal")
            if route_hold(admission):
                action = (f"Project lead: {action}. " if implementation else "") + route_next_action(admission)
            raise refuse("obligation_invalid", "renewal_result_pending", "serving work must settle and its result be consumed or dispositioned",
                         obligation=old_id, admission=pending[0], next_action=action)
        state = entry.get("state", old["state"])
        if state in TERMINAL:
            raise refuse("obligation_invalid", "renewal_terminal_state", "the replacement needs an explicit non-terminal state", obligation=new_id)
        new = {key: deepcopy(old[key]) for key in RENEW_DEFINITION if key in old}
        new.update(id=new_id, renews=old_id, state=state)
        if new["kind"] == "assurance":
            new["candidate"] = ctx.get("candidate")
        for field in ("wait", "executor", "external"):
            if field in entry:
                new[field] = deepcopy(entry[field])
            elif _STATE_FIELDS.get(state) == field and field in old:
                new[field] = deepcopy(old[field])
        _structure(new)
        frozen = deepcopy(old)
        for field in (*_STATE_FIELDS.values(), "reuse"):
            frozen.pop(field, None)
        frozen.update(state="renewed", renewal={"by": new_id, "seq": seq, "definition": copied_identity(old),
                                                "was_satisfied": old["state"] == "satisfied"})
        if old_id in supplied:
            raw[raw.index(supplied[old_id])] = frozen
        else:
            raw.append(frozen)
        generated.append(new)
        replacements[old_id] = new_id
        ids.add(new_id)
    raw.extend(generated)
    for row in raw:
        if isinstance(row, dict) and row.get("state") == "waiting":
            wait = row.get("wait", {})
            if isinstance(wait, dict) and isinstance(wait.get("referent"), str) and wait["referent"] in replacements:
                wait["referent"] = replacements[wait["referent"]]
    ctx["renewal_rows"] = set(replacements) | set(replacements.values())
    ctx["renewal_new"] = set(replacements.values())
    return {**value, "obligations": raw}


def expand_update(prior: dict | None, value: dict) -> dict:
    """Apply a bounded patch to prior rows before the ordinary full-map validator."""
    if "update" not in value:
        return value
    if "obligations" in value:
        raise refuse("obligation_invalid", "malformed", "update and obligations are mutually exclusive", record='expand update')
    current_seq = prior.get("seq", 0) if isinstance(prior, dict) else 0
    revision = prior.get("revision", 0) if isinstance(prior, dict) else 0
    expected = current_seq + 1
    if type(value.get("seq")) is not int or value["seq"] != expected:
        raise refuse("map_stale", "map_stale", "re-read with pod internal map before an update",
                     current_seq=current_seq, expected=expected, revision=revision)
    changes = value["update"]
    if not isinstance(changes, dict) or len(changes) > bound("map", "update", "max_items"):
        raise refuse("obligation_invalid", "malformed", "update is a bounded mapping of obligation ids", record='expand update')
    rows = {row["id"]: deepcopy(row) for row in (prior or {}).get("obligations", [])}
    for key, patch in changes.items():
        _ident(key, "updated obligation")
        if not isinstance(patch, dict) or patch.get("id", key) != key:
            raise refuse("obligation_invalid", "malformed", "an update names its exact obligation id",
                         obligation=key)
        if key not in rows:
            rows[key] = {**patch, "id": key}
            continue
        row = rows[key]
        if "state" in patch and patch["state"] != row.get("state"):
            for field in (*_STATE_FIELDS.values(), "reuse"):
                row.pop(field, None)
        row.update(patch)
    return {**{key: item for key, item in value.items() if key != "update"},
            "obligations": list(rows.values())}


def restatable_rows(state: dict, *, include_terminal: bool = False) -> list[dict]:
    """Expose complete caller fields without receipt history or kernel stamps."""
    rows = []
    for stored in state["obligations"]:
        if stored["state"] in FROZEN and not include_terminal:
            continue
        row = {key: deepcopy(value) for key, value in stored.items()
               if key not in ("introduced_seq", "definition", "receipts", "findings", "finding", "acceptances")}
        if row.get("provenance") == "project_policy" and isinstance(row.get("source"), dict):
            row["source"] = {key: row["source"][key] for key in ("path", "lines") if key in row["source"]}
        if row.get("kind") == "assurance":
            row["evidence"] = [{"attempt": item["attempt"]} for item in row.get("evidence", [])]
        else:
            row["evidence"] = [{key: value for key, value in item.items() if key != "governance"}
                               for item in row.get("evidence", [])]
        if isinstance(row.get("reuse"), dict):
            row["reuse"] = {"from": row["reuse"]["from"]}
        rows.append(row)
    return rows


@input_record("map")
def _accept_write(prior: dict | None, value: dict, ctx: dict, *, triaged: frozenset = frozenset()) -> dict:
    """Validate one proposed map write against the prior accepted map and current facts.

    Returns the map fields to persist. Every refusal names its code, detail, referent and
    next safe action. `prior` is the previously accepted map (or None); `value` holds the
    caller's proposed fields from MAP_INPUT. Review findings are Pod's record: only the
    report boundary's triage (`triaged`) may add them.
    """
    prior_map = prior if isinstance(prior, dict) and prior.get("obligations") is not None else None
    request_entries = (len(value["obligations"]) if isinstance(value.get("obligations"), list)
                       else len(value["update"]) if isinstance(value.get("update"), dict) else 0)
    if (triaged or ctx.get("operation") == "report") and "report_request_entries" in ctx:
        request_entries = ctx["report_request_entries"]
    elif isinstance(value.get("obligations"), list) and len(value["obligations"]) > MAX_OBLIGATIONS:
        row_bounds(value["obligations"], request=True)
    value = expand_update(prior_map, value)
    # Dispositions change admission rows, so the ledger applies them before this write.
    unknown = set(value) - (MAP_INPUT - {"dispositions"})
    if unknown:
        raise refuse("obligation_invalid", "malformed", "unsupported map fields: " + ", ".join(_field_names(unknown)[0]), record='obligation map')
    closed = bool(prior_map and prior_map.get("closure"))
    authority_kind = "revision"
    raw_authority = value.get("revision_authority")
    from .issue_revision import requested, Revision, refuse as issue_refuse
    issue = requested(raw_authority)
    if issue:
        revision_capability = ctx.get("issue_revision")
        if (not isinstance(revision_capability, Revision) or ctx.get("operation") != "checkpoint"
                or raw_authority != revision_capability.authority):
            raise issue_refuse("checkpoint_only")
        authority_kind = "issue_revision"
    elif isinstance(raw_authority, dict) and "question" in raw_authority:
        reference = raw_authority["question"]
        questions = ctx.get("recorded_questions", [])
        if not isinstance(questions, list) or not any(
                question == reference or isinstance(question, dict) and reference in (question.get("id"), question.get("reference"))
                for question in questions):
            raise refuse("decision_question_unrecorded", "unknown_referent", "the owner answer must name a currently recorded question",
                         record="revision authority",
                         next_action="record the question first, or obtain the owner's instruction in the coordinator's session")
        authority_kind = "question"
    elif value.get("governance_refresh") is True:
        authority_kind = "governance"
    elif isinstance(raw_authority, dict) and prior_map is not None and not value.get("reopen"):
        prior_rows = {row["id"]: row for row in prior_map["obligations"]}
        changes = [row for row in value.get("obligations", []) if isinstance(row, dict)
                   and (not isinstance(row.get("id"), str) or row != prior_rows.get(row["id"]))]
        if changes and all(isinstance(row.get("id"), str) and row["id"] in prior_rows and row.get("state") == "withdrawn"
                           and definition_id(row) == definition_id(prior_rows[row["id"]]) for row in changes):
            authority_kind = "withdrawal"
    authority = deepcopy(raw_authority) if issue else _authority(raw_authority, kind=authority_kind)
    user = authority is not None and not issue
    reopen = value.get("reopen") is True
    if reopen and authority is not None:
        owner_decision(authority, kind="question" if authority_kind == "question" else "reopen")
    if closed and not (user and reopen):
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=prior_map["closure"]["revision"])
    if closed and "route_settlements" in value:
        raise refuse("objective_closed", "objective_closed", "a closed objective accepts no route settlement",
                     record="route settlements")
    if prior_map is not None and "obligations" not in value:
        # A checkpoint that leaves the map unchanged still revalidates it against current facts.
        value = {**value, "obligations": deepcopy(prior_map["obligations"])}
    if value.get("obligations") is None:
        if prior_map is not None:
            raise refuse("obligation_unaccounted", "missing_state", "an obligation map cannot be removed", record='obligation map')
        if set(value) - {"seq"}:
            raise refuse("obligation_invalid", "malformed", "map fields need an obligation list", record='obligation map')
        return {}
    if "reopen" in value and (value["reopen"] is not True or not closed):
        raise refuse("obligation_invalid", "malformed", "reopen applies only to a closed objective", record='obligation map')
    seq = prior_map["seq"] + 1 if prior_map else 1
    if "seq" in value and value["seq"] != seq:
        raise refuse("map_stale", "map_stale", "the proposed map is not the next write; "
                     "re-read with pod internal map", current_seq=(prior_map or {}).get("seq", 0),
                     expected=seq, revision=(prior_map or {}).get("revision", 0))
    refresh = value.get("governance_refresh") is True
    if "governance_refresh" in value and not refresh:
        raise refuse("obligation_invalid", "malformed", "governance_refresh is true when present", record='obligation map')
    if refresh and prior_map is None:
        raise refuse("obligation_invalid", "malformed", "governance is bound at intake, then refreshed", record='obligation map')
    ctx = {**ctx, "governance_refresh": refresh, "revision_authority": authority, "authority_kind": authority_kind,
           "request_entries": request_entries, "pending_route_settlements": value.get("route_settlements", ())}
    value = _renew(prior_map, value, ctx, seq)
    revision = (prior_map["revision"] + (1 if user or refresh or issue else 0)) if prior_map else 1

    observed, governance, sources, gov, rebind = _write_governance(prior_map, value, ctx, user, refresh)

    proposals = _write_proposals(prior_map, value)

    if "reuse" in value:
        reuse = _exact(value["reuse"], {"from", "ids"}, {"from", "ids"}, "map reuse")
        _text(reuse["from"], "reuse from", limit=128)
        ids = reuse["ids"]
        if not isinstance(ids, list) or not ids or len(ids) > MAX_OBLIGATIONS:
            raise refuse("obligation_invalid", "malformed", "reuse ids must name a nonempty bounded list",
                         field="reuse.ids")
        for key in ids:
            _ident(key, "reuse obligation")
        if len(set(ids)) != len(ids):
            raise refuse("obligation_invalid", "malformed", "reuse ids are unique", field="reuse.ids")
        known = {row.get("id") for row in value["obligations"] if isinstance(row, dict)}
        if set(ids) - known:
            raise refuse("obligation_invalid", "malformed", "reuse names unknown obligations", field="reuse.ids")
        value = {**value, "obligations": [dict(row, reuse={"from": reuse["from"]}) if row.get("id") in ids
                                        else row for row in value["obligations"]]}
    raw_obligations, prior_rows, rows, criteria = _write_obligations(
        prior_map, value, ctx, seq, user, triaged, refresh, observed, sources, gov, proposals)

    # Coverage: every original criterion has an objective obligation (R39).
    covered = _covered(rows, ctx)
    for criterion in criteria:
        if criterion not in covered:
            raise refuse("obligation_unaccounted", "criterion_uncovered",
                         "every original criterion needs an objective obligation", criterion=criterion)

    reuse_failures = _write_evidence(rows, prior_rows, rebind, triaged, ctx, gov, user, authority, seq)
    _write_acceptances(rows, prior_rows, value.get("acceptances"), seq)

    state = {"seq": seq, "revision": revision, "governance": governance, "governance_sources": sources,
             "obligations": [rows[key] for key in [row["id"] for row in raw_obligations]],
             "proposals": list(proposals.values())}
    if prior_map is not None and "governance_history" in prior_map:
        state["governance_history"] = deepcopy(prior_map["governance_history"])
    if ctx.get("derived_adoption") is not None:
        history = state.setdefault("governance_history", {"candidates": [], "decisions": []})
        history["decisions"].append({"seq": seq, **ctx["derived_adoption"]})
    if prior_map is not None and "delivery" in prior_map:
        state["delivery"] = deepcopy(prior_map["delivery"])
    if authority is not None or (prior_map or {}).get("revision_decisions"):
        decisions = deepcopy((prior_map or {}).get("revision_decisions", []))
        if authority is not None:
            decisions.append({"seq": seq, "kind": authority_kind, **authority})
        state["revision_decisions"] = decisions
    for ob in rows.values():
        if ob["state"] == "satisfied":
            for item in ob["receipts"]:
                if item["evidence"] in ob.get("evidence", []):
                    item.setdefault("accepted_seq", seq)
                    item["reuse"] = deepcopy(ob.get("reuse"))
    for ob in rows.values():
        if ob["kind"] != "assurance" or ob["state"] in FROZEN:
            continue
        for finding in ob.get("findings", []):
            if finding["triage"] != "required_correction":
                continue
            child = rows.get(ctx["lineage"].effective(finding.get("correction")))
            if child is None or child.get("finding", {}).get("attempt") != finding["attempt"]:
                finding.pop("resolved_seq", None)
                continue
            if child["state"] == "satisfied":
                accepted = [item["accepted_seq"] for item in child["receipts"]
                            if item["evidence"] in child.get("evidence", [])
                            and isinstance(item.get("accepted_seq"), int)]
                if accepted:
                    finding["resolved_seq"] = max(accepted)
                else:
                    finding.pop("resolved_seq", None)
            elif child["state"] == "withdrawn" and child["withdrawal"]["by"] == "user_direct":
                finding.setdefault("resolved_seq", seq)
            else:
                finding.pop("resolved_seq", None)
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
        continuing = (earlier is not None and earlier["state"] == ob["state"] == "active"
                      and earlier.get("executor") == ob.get("executor")
                      and ob.get("executor") in _outstanding(ctx))
        if ob["state"] not in TERMINAL and not continuing and _accepted_assurance_current(ob, ctx, gov):
            raise refuse("obligation_unaccounted", "assurance_still_bound",
                         "a still-valid accepted assurance cannot be reopened by removing its evidence",
                         obligation=ob["id"])
    _account(state, rows, seq, ctx, gov, reuse_failures=reuse_failures)
    _waits(state, rows, ctx)
    violations = properties(state, ctx)
    for name, found in violations.items():
        if found:
            raise refuse("obligation_unaccounted", "property_" + name, found[0], record='obligation map')
    slot = [ob["id"] for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    state["coordinator_slot"] = slot[0] if slot else None
    state["quiescence"] = quiescence(state)
    state["closure"] = None
    state["reopened"] = list((prior_map or {}).get("reopened", []))
    if closed:
        state["reopened"] = (state["reopened"] + [{"seq": seq, "revision": revision,
                                                   "instruction": authority["instruction"],
                                                   **channel_fields(authority),
                                                   **({"question": authority["question"]} if "question" in authority else {}),
                                                   "closed_revision": prior_map["closure"]["revision"]}])[-8:]
    state["observations"] = observations(prior_map, state, ctx)
    _persisted_rows(state, ctx)
    if value.get("close") is True:
        closure = _close(state, ctx)
        if callable(ctx.get("pod_tick_gate")):
            ctx["pod_tick_gate"](state)
        # Closure is the last record (R117). Only a new closure write checks this; a recorded closure is
        # re-validated by _close alone, so it stays valid whatever was recorded around it.
        unrecorded = ctx.get("closure_unrecorded")
        if unrecorded:
            raise refuse("obligation_unaccounted", "closure_unfinished",
                         "closure is the last record; " + ", ".join(unrecorded["remaining"]) + " come first",
                         record="closure", next_action=unrecorded["next_action"])
        state["closure"] = closure
    elif "close" in value:
        raise refuse("obligation_invalid", "malformed", "close is true when present", record='obligation map')
    if "route_settlements" in value:
        _route_settlements(value["route_settlements"], state, ctx)
    return state


def accept_write(prior: dict | None, value: dict, ctx: dict, *, triaged: frozenset = frozenset()) -> dict:
    """Expose refusal context alongside the same ordered map transition."""
    try:
        return _accept_write(prior, value, {**ctx, "accepted_map": prior}, triaged=triaged)
    except PodError as exc:
        current = prior if isinstance(prior, dict) else {}
        detail = dict(exc.detail) if isinstance(exc.detail, dict) else {}
        detail.setdefault("current_seq", current.get("seq", 0))
        detail.setdefault("revision", current.get("revision", 0))
        exc.detail = detail
        raise


def _route_later(original: dict, replacement: dict) -> bool:
    if type(original.get("admitted_seq")) is int and type(replacement.get("admitted_seq")) is int:
        return replacement["admitted_seq"] > original["admitted_seq"]
    from .util import normalize_timestamp
    try:
        old = normalize_timestamp(original.get("created_at"), field="admission time", code="state_unsupported")
        new = normalize_timestamp(replacement.get("created_at"), field="replacement time", code="state_unsupported")
        return datetime.fromisoformat(new.replace("Z", "+00:00")) > datetime.fromisoformat(old.replace("Z", "+00:00"))
    except PodError:
        return False


def _route_definitions(admission: dict, lineage: Lineage) -> bool:
    binding = admission.get("binding")
    definitions = binding.get("definitions") if isinstance(binding, dict) else None
    if not isinstance(definitions, dict):
        return False
    return all(key in lineage.rows and definitions.get(key) == definition_id({
        **lineage.rows[lineage.effective(key)], "id": key}) for key in admission["serves"])


@input_record("route settlement")
def _route_settlements(entries, state: dict, ctx: dict) -> None:
    """Validate every basis last, then append all records to the locked write's admissions."""
    if ctx.get("operation", "checkpoint") not in ("checkpoint", "report"):
        raise refuse("route_settlement_invalid", "checkpoint_only", "route settlements use internal checkpoint or report map",
                     record="route settlements")
    if not isinstance(entries, list) or len(entries) > bound("map", "route_settlements", "max_items"):
        raise refuse("route_settlement_invalid", "malformed", "route_settlements is a bounded list",
                     record="route settlements")
    admissions, seen, accepted = _admissions(ctx), set(), []
    lineage = Lineage(state["obligations"])
    for entry in entries:
        _exact(entry, fields("route settlement"), required("route settlement"), "route settlement",
               code="route_settlement_invalid")
        key = entry["admission"]
        row = admissions.get(key) if isinstance(key, str) else None
        if row is None or key in seen or not route_hold(row):
            raise refuse("route_settlement_invalid", "route_not_held", "name each recorded unsettled route once",
                         admission=key)
        seen.add(key)
        basis = entry["basis"]
        expected = {"admission", "basis", "replacement"} if basis == "redone" else {
            "admission", "basis", "decision", "source", "reason"}
        if (basis == "lead_decision" and row["role"] in ("review", "investigate")
                and entry.get("decision") == "discard"):
            expected.add("corrections")
        if basis not in ("redone", "lead_decision") or set(entry) != expected:
            raise refuse("route_settlement_invalid", "malformed", "use the exact fields for this route settlement basis",
                         admission=key, accepted=expected, optional=set())
        if basis == "redone":
            replacement = admissions.get(entry["replacement"]) if isinstance(entry["replacement"], str) else None
            consumed = (replacement or {}).get("report") or {}
            if (row["role"] != "implement" or row.get("disposition") is None or replacement is None
                    or not _route_later(row, replacement) or replacement.get("role") != "implement"
                    or route_hold(replacement) or route_flags(replacement)[1]
                    or consumed.get("status") != "validated_observation" or consumed.get("outcome") != "succeeded"
                    or (replacement.get("disposition") or {}).get("kind") != "integrated_into"
                    or any((replacement.get("route_decision") or {}).get("effective", {}).get(field) != row["request"][field]
                           for field in ("agent", "model", "effort"))):
                raise refuse("route_settlement_invalid", "replacement_unqualified", "redone needs a later successful integrated implementation on the admitted route",
                             admission=key, next_action=route_next_action(row))
            original_serves = {lineage.effective(ob) for ob in row["serves"]}
            replacement_serves = {lineage.effective(ob) for ob in replacement["serves"]}
            if (original_serves != replacement_serves or not original_serves <= set(lineage.rows)
                    or row["boundary"] != replacement["boundary"]
                    or not _route_definitions(row, lineage) or not _route_definitions(replacement, lineage)):
                raise refuse("route_settlement_invalid", "replacement_coverage", "replacement must serve exactly every original obligation at its current renewal tip",
                             admission=key, next_action=route_next_action(row))
        else:
            decision = entry["decision"]
            source = _exact(entry["source"], fields("route settlement source"), required("route settlement source"),
                            "route settlement source", code="route_settlement_invalid")
            _text(source["reference"], "reference", limit=bound("route settlement source", "reference"), code="route_settlement_invalid")
            _text(entry["reason"], "reason", limit=bound("route settlement", "reason"), code="route_settlement_invalid")
            if decision not in ("accept", "discard"):
                raise refuse("route_settlement_invalid", "malformed", "a lead decision accepts or discards the attempt", admission=key)
            if row["role"] == "implement":
                kind = "integrated_into" if decision == "accept" else "discarded"
                if (row.get("disposition") or {}).get("kind") != kind:
                    raise refuse("route_settlement_invalid", "disposition_required", "the lead decision needs its matching ordinary implementation disposition",
                                 admission=key, next_action=route_next_action(row))
            elif row["role"] in ("review", "investigate"):
                if decision == "accept":
                    if row.get("report") is None:
                        raise refuse("route_settlement_invalid", "accept_unavailable", "accepting later route evidence needs an already consumed review or investigation report",
                                     admission=key, next_action=route_next_action(row))
                elif key in _outstanding(ctx) or row.get("state") != "bound" or row.get("report") is not None:
                    raise refuse("route_settlement_invalid", "discard_unavailable", "only a natively settled review or investigation without a consumed report can be terminally discarded",
                                 admission=key, next_action=route_next_action(row))
                corrections = entry.get("corrections", [])
                if (not isinstance(corrections, list) or len(corrections) > bound("route settlement", "corrections", "max_items")
                        or any(not isinstance(identity, str) for identity in corrections)
                        or len(corrections) != len(set(corrections))
                        or any(lineage.rows.get(identity, {}).get("kind") != "correction" for identity in corrections)):
                    raise refuse("route_settlement_invalid", "corrections_required", "list ordinary correction ids for every known required finding; [] needs a reason explaining why none are known",
                                 admission=key, next_action=route_next_action(row))
            else:
                raise refuse("route_settlement_invalid", "role_invalid", "this admission has no supported settlement role", admission=key)
        record = {field: deepcopy(item) for field, item in entry.items() if field != "admission"}
        record.update(occurrence=route_occurrence(row), seq=state["seq"], utc=datetime.now(timezone.utc).isoformat())
        staged = {**row, "route_decision": {**row["route_decision"], "mismatch_occurrence": route_occurrence(row),
                                           "settlements": [*row["route_decision"].get("settlements", []), record]}}
        validate_route_settlements(staged)
        accepted.append((row, staged["route_decision"]))
    for row, decision in accepted:
        row["route_decision"] = decision


@input_record("objective source")
def _objective_source(value: Any, obligation: str) -> dict:
    source = _exact(value, fields("objective source"), required("objective source"), "objective source", obligation=obligation)
    _text(source["ref"], "source ref", limit=bound("objective source", "ref"), obligation=obligation)
    return source


@input_record("user source")
def _user_source(value: Any, obligation: str) -> dict:
    source = _exact(value, fields("user source"), required("user source"), "user source", obligation=obligation)
    owner_decision(source, kind="question" if "question" in source else "revision", stored=True)
    _user_instruction(source, obligation)
    if "ref" in source:
        _text(source["ref"], "source ref", limit=bound("user source", "ref"), obligation=obligation)
    return source


@input_record("user source")
def _user_instruction(source: dict, obligation: str) -> None:
    _text(source["instruction"], "instruction", limit=bound("user source", "instruction"), obligation=obligation)


def _introduce(ob: dict, intake: bool, user: bool, observed: dict | None, sources: list[dict],
               criteria: list[str], *, issue_revision: bool = False) -> None:
    provenance, kind = ob["provenance"], ob["kind"]
    if provenance == "objective":
        source = _objective_source(ob.get("source"), ob["id"])
        if kind not in ("criterion", "delivery") or not (intake or issue_revision and kind == "criterion"):
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "objective provenance enters criteria and delivery obligations at intake",
                         obligation=ob["id"])
    elif provenance == "user_direct":
        source = _user_source(ob.get("source"), ob["id"])
        if not user:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "a user_direct obligation enters only by a direct user revision", obligation=ob["id"])
    elif provenance == "project_policy":
        if not isinstance(ob.get("source"), dict):
            raise refuse("obligation_invalid", "cite_not_found", "project policy cites a governance source line range",
                         obligation=ob["id"])
        ob["source"] = _cite(ob["source"], observed, sources, obligation=ob["id"])
    else:
        if kind not in ("subgoal", "assurance", "correction"):
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "the coordinator introduces only subgoals, assurance and corrections", obligation=ob["id"])
        if "source" in ob:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "coordinator obligations derive from their parent, not a source", obligation=ob["id"])


def _ancestry(rows: dict[str, dict], *, lineage=None) -> None:
    lineage = lineage if lineage is not None else Lineage(rows)
    for ob in rows.values():
        cursor = rows[lineage.effective(ob["id"])]
        seen = {cursor["id"]}
        while "parent" in cursor:
            cursor = rows[lineage.effective(cursor["parent"])]
            if cursor["id"] in seen:
                raise refuse("obligation_invalid", "no_parent", "parent chains are acyclic", obligation=ob["id"])
            seen.add(cursor["id"])


@input_record("withdrawal")
def _withdrawal(ob: dict, user: bool, authority: dict | None) -> dict:
    record = _exact(ob["withdrawal"], fields("withdrawal"), required("withdrawal"), "withdrawal", obligation=ob["id"])
    if "decision" in record:
        raise refuse("obligation_invalid", "withdrawal_unauthorized", "issue revision decision references are kernel-owned", obligation=ob["id"])
    _text(record["reason"], "reason", limit=bound("withdrawal", "reason"), obligation=ob["id"])
    by = record["by"]
    allowed = (by == "user_direct" and user
               or by == "project_policy" and ob["provenance"] == "project_policy"
               and bool(ob.get("source", {}).get("gone"))
               or by == "coordinator" and ob["provenance"] == "coordinator" and ob["kind"] in ("subgoal", "assurance"))
    if not allowed:
        raise refuse("obligation_invalid", "withdrawal_unauthorized",
                     "withdrawal by allowed: user_direct, project_policy, coordinator; current authority does not permit withdrawal", obligation=ob["id"])
    result = {"by": by, "reason": record["reason"]}
    if by == "user_direct":
        result["instruction"] = authority["instruction"]
        result.update(channel_fields(owner_decision(authority, kind="withdrawal", stored="channel" not in authority)))
        if CHANNEL_FIELDS & set(record) and channel_fields(record) != channel_fields(result):
            raise refuse("obligation_invalid", "withdrawal_unauthorized", "withdrawal channel must match its revision authority", obligation=ob["id"])
    elif CHANNEL_FIELDS & set(record):
        raise refuse("obligation_invalid", "malformed", "only owner withdrawals carry an owner decision channel", obligation=ob["id"])
    return result


@input_record("external")
def _external_record(ob: dict) -> dict:
    external = _exact(ob["external"], fields("external"), required("external"), "external",
                      code="obligation_unaccounted", obligation=ob["id"])
    if external["party"] not in PARTIES:
        raise refuse("obligation_unaccounted", "external_invalid",
                     "an external block names owner, user, provider or third_party", obligation=ob["id"])
    return external


@input_record("external")
def _external_text(ob: dict, field: str) -> None:
    _text(ob["external"][field], field, code="obligation_unaccounted", detail="external_invalid",
          limit=bound("external", field), obligation=ob["id"])


def _restate_action(ctx: dict) -> str:
    operation = ctx.get("operation", "checkpoint")
    reading = "read pod internal map for next_seq, restatable obligations and current outstanding/settled attempts; "
    if operation == "admission":
        return reading + "put the full obligations restatement in map.obligations in this same admission request with map.seq = next_seq (map.update is not accepted)"
    if operation == "report":
        return reading + "put the restating map with its update in this same report request; use map.seq = next_seq"
    return reading + "restate every named obligation in this checkpoint's map, with seq = next_seq"


def _account(state: dict, rows: dict[str, dict], seq: int, ctx: dict, gov: str, *, reuse_failures: dict | None = None) -> None:
    """R83: exactly one currently valid state for every obligation."""
    outstanding = set(_outstanding(ctx))
    admissions = _admissions(ctx)
    coordinator = [ob for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    if len(coordinator) > 1:
        raise refuse("obligation_unaccounted", "coordinator_slot_exceeded",
                     "at most one obligation is coordinator-held active",
                     obligations=",".join(ob["id"] for ob in coordinator))
    pending = undispositioned(ctx)
    open_corrections = {
        ob["id"]: [child["id"] for child in rows.values()
                   if ctx["lineage"].effective(child.get("parent")) == ob["id"] and child["kind"] == "correction"
                   and child["state"] != "renewed"
                   and not (child["state"] == "satisfied"
                            or child["state"] == "withdrawn" and child["withdrawal"]["by"] == "user_direct")]
        for ob in rows.values() if ob["state"] == "satisfied" and ob["kind"] == "assurance"}
    open_corrections = {key: children for key, children in open_corrections.items() if children}
    for ob in rows.values():
        state = ob["state"]
        if state == "unassigned" and ob["introduced_seq"] != seq:
            raise refuse("obligation_unaccounted", "unassigned_expired",
                         "unassigned is valid only in the introducing checkpoint", obligation=ob["id"])
        if state == "active":
            executor = ob["executor"]
            if executor != "coordinator":
                row = admissions.get(executor) if isinstance(executor, str) else None
                if row is None or ob["id"] not in (row.get("serves") or []):
                    raise refuse("obligation_unaccounted", "executor_unknown",
                                 "a worker executor is an admission that serves the obligation", obligation=ob["id"])
                if executor not in outstanding:
                    raise refuse("obligation_unaccounted", "active_admission_settled",
                                 "the executing admission has settled; restate as waiting, blocked_external, "
                                 "satisfied with evidence, or withdrawn with authority",
                                 next_action=_restate_action(ctx),
                                 restate=[row["id"] for row in rows.values() if row["state"] == "active"
                                          and row.get("executor") != "coordinator"
                                          and row.get("executor") in admissions
                                          and row["id"] in (admissions[row["executor"]].get("serves") or [])
                                          and row["executor"] not in outstanding],
                                 obligation=ob["id"], admission=executor)
            else:
                for key in outstanding:
                    found = overlap(ob["boundary"], _admission_boundary(admissions.get(key, {})))
                    if found["paths"] or found["surfaces"]:
                        raise refuse("obligation_unaccounted", "coordinator_boundary_overlap",
                                     "the coordinator-held boundary overlaps an outstanding admission",
                                     obligation=ob["id"], admission=key)
        if state == "blocked_external":
            external = _external_record(ob)
            for field in ("need", "unblocks_when"):
                _external_text(ob, field)
                if external[field] in rows or external[field] in admissions:
                    raise refuse("obligation_unaccounted", "external_invalid",
                                 "an external block never names an internal referent", obligation=ob["id"])
        if state == "satisfied":
            served = [key for key in pending if ob["id"] in (pending[key].get("serves") or [])]
            if served:
                raise refuse("obligation_unaccounted", "undispositioned_result",
                             "a served result has no disposition", obligation=ob["id"], admission=served[0])
            if "evidence" not in ob:
                raise refuse("obligation_unaccounted", "satisfied_without_evidence",
                             "satisfied needs a settled attempt or short evidence with check, command, result "
                             "and reference", obligation=ob["id"],
                             next_action="use one settled attempt or add a short evidence receipt",
                             settled_attempts=",".join(_settled_serving(ctx, ob["id"])))
            if ob["id"] in open_corrections:
                # This guaranteed refusal keeps precedence over later state errors;
                # its diagnostic still accounts for every invalid satisfied row below.
                break
    from .assurance import invalidation
    invalid = [invalidation(ob, ctx, gov, corrections=open_corrections.get(ob["id"], [])) for ob in rows.values()
               if ob["state"] == "satisfied"
               and (ob["id"] in open_corrections or ob["id"] in (reuse_failures or {}) or not evidence_valid(ob, ctx, gov))]
    if invalid:
        eligible = [item["id"] for item in rows.values() if item.get("receipts")
                    and not item.get("source", {}).get("gone")] if ctx.get("governance_refresh") else []
        first = ({"obligation": next(iter(open_corrections)),
                  "correction": next(iter(open_corrections.values()))[0]} if open_corrections
                 else {"obligation": invalid[0]["obligation"]})
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "; ".join(f"{item['obligation']}: {item['classification']}, {item['eligibility']}; {item['correction']}"
                               for item in invalid[:8]) + (f"; +{len(invalid)-8} more" if len(invalid) > 8 else ""),
                     **first, invalidations=invalid[:8], remaining=max(0, len(invalid)-8),
                     eligible_rebind=",".join(eligible[:8]), next_action="re-read with pod internal map; "
                     + ("rebind eligible ids; " if eligible else "")
                     + "name eligible ids in reuse {from, ids}; record fresh evidence or restate the other rows")
    for key in outstanding:
        row = admissions.get(key)
        for served in (row or {}).get("serves") or []:
            ob = rows.get(served)
            if ob is None or ob["state"] != "active" or ob.get("executor") != key:
                raise refuse("obligation_unaccounted", "outstanding_unaccounted",
                             "an outstanding admission's obligation stays active under it",
                             obligation=served, admission=key)
    for key, row in pending.items():
        for served in row.get("serves") or []:
            ob = rows.get(served)
            held = (ob is not None and (ob["state"] == "active" and ob["executor"] == "coordinator"
                                        or ob["state"] == "waiting" and ob["wait"].get("class") == "sequenced"))
            if not held:
                raise refuse("obligation_unaccounted", "undispositioned_result",
                             "until its result has a disposition, the owner obligation is coordinator-held or sequenced",
                             obligation=served, admission=key)


@input_record("wait")
def _wait_record(ob: dict) -> dict:
    wait = ob["wait"]
    if not isinstance(wait, dict) or wait.get("class") not in WAIT_CLASSES:
        raise refuse("wait_invalid", "unclassified", "wait class allowed: " + ", ".join(WAIT_CLASSES), obligation=ob["id"])
    record = _exact(wait, fields("wait"), required("wait"),
                    "wait", code="wait_invalid", obligation=ob["id"])
    for key in ("rationale", "revisit_when"):
        if key in record:
            _text(record[key], key, code="wait_invalid", detail="malformed", obligation=ob["id"])
    if ("rationale" in record or "revisit_when" in record or "inputs" in record) and record["class"] != "sequenced":
        raise refuse("wait_invalid", "malformed", "only a sequenced wait carries a serialization rationale",
                     obligation=ob["id"])
    return record


@input_record("wait")
def _validate_wait(ob: dict, rows: dict[str, dict], ctx: dict, outstanding: set[str],
                   admissions: dict, held: list[dict], pending: dict) -> str | None:
    wait = _wait_record(ob)
    cls, referent = wait["class"], wait["referent"]
    name = ob["id"]
    if cls in ("dependency", "contract_unsettled"):
        target = rows.get(referent) if isinstance(referent, str) else None
        if target is None or target is ob or cls == "contract_unsettled" and target["kind"] != "subgoal":
            raise refuse("wait_invalid", "unknown_referent", f"{cls} names another obligation"
                         + (" that is a subgoal" if cls == "contract_unsettled" else ""), obligation=name)
        if target["state"] in TERMINAL:
            raise refuse("wait_invalid", "resolved_referent", "the waited-on obligation is settled",
                         obligation=name, referent=referent)
        return referent
    elif cls == "sequenced":
        if not held:
            raise refuse("wait_invalid", "sequenced_without_active",
                         "sequenced waits on the coordinator-held active obligation", obligation=name)
        if referent != held[0]["id"]:
            raise refuse("wait_invalid", "unknown_referent", "sequenced names the coordinator-held obligation",
                         obligation=name, expected=held[0]["id"])
    elif cls == "ownership":
        if isinstance(referent, str) and referent in admissions:
            if referent not in outstanding:
                raise refuse("wait_invalid", "resolved_referent", "the overlapping admission has settled",
                             obligation=name, referent=referent)
            other = _admission_boundary(admissions[referent])
        elif isinstance(referent, str) and referent in rows:
            if not (rows[referent]["state"] == "active" and rows[referent]["executor"] == "coordinator"):
                raise refuse("wait_invalid", "resolved_referent", "the obligation is no longer coordinator-held",
                             obligation=name, referent=referent)
            other = rows[referent]["boundary"]
        else:
            raise refuse("wait_invalid", "unknown_referent",
                         "ownership names an outstanding admission or the coordinator-held obligation",
                         obligation=name)
        if not overlaps(ob["boundary"], other):
            raise refuse("wait_invalid", "ownership_without_overlap", "the declared boundaries do not overlap",
                         obligation=name, referent=referent)
    elif cls == "capacity":
        ceiling = ctx.get("ceiling", 0)
        if (not isinstance(referent, list) or len(referent) > bound("wait", "referent", "capacity")["max_items"]
                or any(not isinstance(item, str) for item in referent)):
            raise refuse("wait_invalid", "unknown_referent", "capacity names the reservations holding the ceiling",
                         obligation=name)
        if len(outstanding) < ceiling:
            raise refuse("wait_invalid", "capacity_below_ceiling",
                         "a capacity wait is valid only at the ceiling", obligation=name,
                         reserved=len(outstanding), ceiling=ceiling)
        if set(referent) != outstanding:
            raise refuse("wait_invalid", "unknown_referent", "capacity names exactly the outstanding reservations",
                         obligation=name)
    elif cls == "integration_pending":
        if not isinstance(referent, str) or referent not in admissions:
            raise refuse("wait_invalid", "unknown_referent", "integration_pending names a settled result",
                         obligation=name)
        if referent not in pending:
            raise refuse("wait_invalid", "resolved_referent", "the result has a disposition or is not settled",
                         obligation=name, referent=referent)
        if not overlaps(ob["boundary"], _admission_boundary(admissions[referent])):
            raise refuse("wait_invalid", "ownership_without_overlap", "the result does not overlap this boundary",
                         obligation=name, referent=referent)
    elif cls == "input_unavailable":
        reader = ctx.get("source_state")
        if not isinstance(referent, str) or not referent:
            raise refuse("wait_invalid", "unknown_referent", "input_unavailable names a source binding path",
                         obligation=name)
        if reader is None or reader(referent) != "unavailable":
            raise refuse("wait_invalid", "resolved_referent", "the source binding is readable now",
                         obligation=name, referent=referent)
    elif cls == "authority":
        record = _exact(referent, {"kind", "scope", "candidate", "need"}, {"kind"}, "authority referent",
                        code="wait_invalid", obligation=ob["id"])
        if record["kind"] == "authorization":
            if record.get("scope") not in AUTHORIZED_SCOPES or not isinstance(record.get("candidate"), str):
                raise refuse("wait_invalid", "unknown_referent",
                             "a missing authorization names its scope and candidate", obligation=name)
            granted = ctx.get("authorization_granted")
            if granted is not None and granted(record["scope"], record["candidate"]):
                raise refuse("wait_invalid", "resolved_referent", "the authorization is recorded",
                             obligation=name)
        elif record["kind"] in ("native_authority", "permission"):
            _text(record.get("need"), "need", code="wait_invalid", detail="unknown_referent", obligation=ob["id"])
        else:
            raise refuse("wait_invalid", "unknown_referent", "authority names an authorization or blocked authority",
                         obligation=name)
    elif cls == "user_hold":
        constraints = [row for row in ctx.get("constraints", []) if row.get("id") == referent]
        if not constraints or constraints[0].get("provenance") != "user_direct":
            raise refuse("wait_invalid", "unknown_referent", "user_hold names a user_direct constraint",
                         obligation=name)
        if constraints[0].get("active", True) is False:
            raise refuse("wait_invalid", "resolved_referent", "the user constraint is no longer in force",
                         obligation=name, referent=referent)
    return None


def _check_wait_cycles(edges: dict[str, str]) -> None:
    for start in edges:
        seen = [start]
        cursor = start
        while cursor in edges:
            cursor = edges[cursor]
            if cursor in seen:
                raise refuse("wait_invalid", "cycle", "dependency and contract waits are acyclic",
                             obligations=",".join(seen))
            seen.append(cursor)




def _waits(state: dict, rows: dict[str, dict], ctx: dict) -> None:
    """R84: one controlling reason, valid for its class and current referent."""
    outstanding = set(_outstanding(ctx))
    admissions = _admissions(ctx)
    held = [ob for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    pending = undispositioned(ctx)
    edges: dict[str, str] = {}
    violations = []
    for ob in rows.values():
        if ob["state"] != "waiting":
            continue
        try:
            edge = _validate_wait(ob, rows, ctx, outstanding, admissions, held, pending)
        except PodError as exc:
            violations.append((ob["id"], exc))
            continue
        if edge is not None:
            edges[ob["id"]] = edge
    if violations:
        first = violations[0][1]
        first.detail["dependents"] = [key for key, _ in violations]
        first.detail["remaining"] = 0
        first.detail["next_action"] = _restate_action(ctx)
        first.detail["referent"]["restate"] = [key for key, _ in violations]
        first.args = (str(first).rsplit(". Next: ", 1)[0] + "; affected dependents: " + ", ".join(key for key, _ in violations)
                      + "; Next: " + _restate_action(ctx),)
        raise first
    try:
        _check_wait_cycles(edges)
    except PodError as exc:
        affected = []
        for name in edges:
            cursor, seen = name, set()
            while cursor in edges and cursor not in seen:
                seen.add(cursor)
                cursor = edges[cursor]
            if cursor in seen:
                affected.append(name)
        exc.detail["dependents"] = affected
        exc.detail["next_action"] = _restate_action(ctx)
        exc.detail["referent"]["restate"] = affected
        exc.detail["remaining"] = 0
        exc.args = (str(exc).rsplit(". Next: ", 1)[0] + "; affected dependents: " + ", ".join(affected)
                    + "; Next: " + _restate_action(ctx),)
        raise


# --------------------------------------------------------------------------- properties

def _chain_end(ob: dict, rows: dict[str, dict]) -> dict:
    cursor, seen = ob, set()
    while (cursor["state"] == "waiting" and cursor["wait"]["class"] in ("dependency", "contract_unsettled")
           and cursor["id"] not in seen):
        seen.add(cursor["id"])
        cursor = rows[cursor["wait"]["referent"]]
    return cursor


def p1_accounting(state: dict, ctx: dict) -> list[str]:
    """Structural obligation accounting, a property of accepted records, not liveness."""
    rows = {ob["id"]: ob for ob in state["obligations"]}
    active = any(ob["state"] == "active" for ob in rows.values())
    found = []
    if not active:
        for ob in rows.values():
            if ob["state"] == "unassigned":
                found.append(f"{ob['id']} is newly unassigned while no obligation is active")
            elif ob["state"] not in TERMINAL:
                end = _chain_end(ob, rows)
                if not (end["state"] == "blocked_external"
                        or end["state"] == "waiting" and end["wait"]["class"] in EXTERNAL_WAITS):
                    found.append(f"{ob['id']} waits without active work on a chain that does not end externally")
    return found


def p2_provenance(state: dict, ctx: dict) -> list[str]:
    rows = {ob["id"]: ob for ob in state["obligations"]}
    lineage = Lineage(rows)
    found = []
    for ob in rows.values():
        root = ob
        while root["provenance"] == "coordinator" and "parent" in root:
            root = rows[lineage.effective(root["parent"])]
        if root["provenance"] == "coordinator":
            found.append(f"{ob['id']} has no authorized provenance")
        elif root["provenance"] == "project_policy" and not isinstance(root.get("source", {}).get("base"), str):
            found.append(f"{ob['id']} lacks a governance citation at a base revision")
    return found


def p3_no_slot_filling(state: dict, ctx: dict) -> list[str]:
    rows = {ob["id"]: ob for ob in state["obligations"]}
    found, seen = [], {}
    for key in _outstanding(ctx):
        serves = (_admissions(ctx).get(key) or {}).get("serves") or []
        if not serves:
            found.append(f"outstanding admission {key[:12]} serves no obligation")
        for served in serves:
            ob = rows.get(served)
            if ob is None or ob["state"] in TERMINAL:
                found.append(f"outstanding admission {key[:12]} serves no unsatisfied obligation {served}")
            if served in seen:
                found.append(f"{served} has two outstanding admissions")
            seen[served] = key
    return found


def p4_no_integration_deadlock(state: dict, ctx: dict) -> list[str]:
    rows = {ob["id"]: ob for ob in state["obligations"]}
    found = []
    for key, row in undispositioned(ctx).items():
        for served in row.get("serves") or []:
            ob = rows.get(served)
            if not (ob is not None and (ob["state"] == "active" and ob["executor"] == "coordinator"
                                        or ob["state"] == "waiting" and ob["wait"]["class"] == "sequenced")):
                found.append(f"result {key[:12]} has no coordinator-held or sequenced owner")
    return found


def p5_assurance_binding(state: dict, ctx: dict) -> list[str]:
    """Assurance-binding integrity: every satisfied assurance is bound; not review truth."""
    gov = bound_governance_digest(state)
    found = [f"{ob['id']} is satisfied without a bound review attempt"
            for ob in state["obligations"]
            if ob["kind"] == "assurance" and ob["state"] == "satisfied" and not evidence_valid(ob, ctx, gov)]
    found += [f"{ob['id']} is reopened while its accepted review remains bound"
              for ob in state["obligations"]
              if ob["state"] not in TERMINAL and ob.get("executor") not in _outstanding(ctx)
              and _accepted_assurance_current(ob, ctx, gov)]
    return found


def p6_terminal_closure(state: dict, ctx: dict) -> list[str]:
    if not state.get("closure"):
        return []
    found = [f"{ob['id']} is open in a closed objective" for ob in state["obligations"]
             if ob["state"] not in TERMINAL]
    if _outstanding(ctx):
        found.append("a closed objective has outstanding admissions")
    return found


PROPERTIES = {"P1": p1_accounting, "P2": p2_provenance, "P3": p3_no_slot_filling,
              "P4": p4_no_integration_deadlock, "P5": p5_assurance_binding, "P6": p6_terminal_closure}


def properties(state: dict, ctx: dict) -> dict[str, list[str]]:
    """The P1–P6 predicates; every boundary write and the generative tests use these."""
    return {name: check(state, ctx) for name, check in PROPERTIES.items()}


# --------------------------------------------------------------------------- quiescence, closure

def quiescence(state: dict) -> dict | None:
    rows = {ob["id"]: ob for ob in state["obligations"]}
    if any(ob["state"] == "active" for ob in rows.values()):
        return None
    unfinished = [ob for ob in rows.values() if ob["state"] not in TERMINAL]
    if not unfinished:
        return {"state": "closable"}
    blocked = []
    for ob in unfinished:
        end = _chain_end(ob, rows)
        if end["state"] == "blocked_external":
            blocked.append({"obligation": ob["id"], "via": end["id"], **end["external"]})
        else:
            blocked.append({"obligation": ob["id"], "via": end["id"], "class": end["wait"]["class"],
                            "referent": end["wait"]["referent"]})
    parts = []
    for row in blocked:
        if row["via"] != row["obligation"]:
            continue
        if "party" in row:
            parts.append(f"{row['party']}: {row['need']} (unblocks when {row['unblocks_when']})")
        else:
            parts.append(f"{row['class']}: {row['referent']}")
    return {"state": "quiescent", "blocked": blocked,
            "interim_report": "incomplete — blocked on " + "; ".join(parts)}


def validate_map_record(state: dict) -> None:
    """The persisted map envelope used by ordinary writes and checkpoint reads."""
    value = _exact({key: value for key, value in state.items() if key in MAP_STORED},
                   MAP_STORED, MAP_STORED - {"governance_history", "delivery", "revision_decisions"}, "persisted map")
    for field in ("seq", "revision"):
        if type(value[field]) is not int or value[field] < 1:
            raise refuse("obligation_invalid", "malformed", "persisted map sequence and revision are positive integers", record="persisted map")
    if "revision_decisions" in value:
        decisions = value["revision_decisions"]
        if not isinstance(decisions, list):
            raise refuse("obligation_invalid", "malformed", "revision decisions are a list", record="revision decisions")
        for row in decisions:
            if row.get("operation") == "issue_revision":
                from .issue_revision import validate_decision
                validate_decision(row, state)
                continue
            _exact(row, DECISION_FIELDS | {"seq", "question", "kind"}, {"seq", "provenance", "instruction", "kind"}, "revision decision")
            if type(row["seq"]) is not int or not 1 <= row["seq"] <= value["seq"]:
                raise refuse("obligation_invalid", "malformed", "revision decision names its accepted sequence", record="revision decision")
            if row["kind"] not in ("question", "governance", "withdrawal", "revision"):
                raise refuse("obligation_invalid", "malformed", "revision decision kind is unsupported", record="revision decision")
            owner_decision(row, kind=row["kind"], stored=True)
    governance = _exact(value["governance"], {"base_ref", "selection", "exclude", "base", "proof_base"},
                        {"base_ref", "selection", "exclude", "base"}, "persisted governance")
    if governance["base_ref"] is not None:
        _text(governance["base_ref"], "base_ref", limit=256)
    if (governance["base"] is not None and (not isinstance(governance["base"], str) or _COMMIT.fullmatch(governance["base"]) is None)
            or governance["selection"] is not None and not isinstance(governance["selection"], str)
            or not isinstance(governance["exclude"], list) or len(governance["exclude"]) > len(GOVERNANCE_PATHS)
            or any(not isinstance(path, str) or path not in GOVERNANCE_PATHS for path in governance["exclude"])
            or len(set(governance["exclude"])) != len(governance["exclude"])):
        raise refuse("obligation_invalid", "malformed", "persisted governance retains its target and source selection", record="persisted governance")
    governance_digest(value["governance_sources"])
    from .governance import _governance_history
    history = _governance_history(value)["decisions"]
    if "proof_base" in governance:
        if (not isinstance(governance["proof_base"], str) or _COMMIT.fullmatch(governance["proof_base"]) is None):
            raise refuse("obligation_invalid", "malformed", "proof base retains the unchanged source identity", record="persisted governance")
        revisions = [{"path": row["path"], "revision": row["revision"]} for row in value["governance_sources"]]
        base, visited = governance["base"], set()
        while base != governance["proof_base"]:
            adoption = next((row for row in reversed(history) if row.get("provenance") == "derived"
                             and row["base"] == base and row["base_ref"] == governance["base_ref"]), None)
            if base in visited or adoption is None or adoption["sources"] != revisions:
                raise refuse("obligation_invalid", "malformed", "proof base requires the unchanged-source adoption chain", record="persisted governance")
            visited.add(base)
            base = adoption["previous_base"]
    sources = value["governance_sources"]
    if (governance["selection"] not in (None, "default", "user_direct")
            or governance["exclude"] != sorted(governance["exclude"])
            or governance["base"] is None and (governance["base_ref"] is not None or governance["selection"] is not None or sources)
            or governance["base"] is not None and (governance["base_ref"] is None or governance["selection"] is None
                or [row["path"] for row in sources] != [path for path in GOVERNANCE_PATHS if path not in governance["exclude"]]
                or any(row["base"] != governance["base"] for row in sources))):
        raise refuse("obligation_invalid", "malformed", "governance header and declared sources retain one bound snapshot", record="persisted governance")
    proposals = value["proposals"]
    if not isinstance(proposals, list) or len(proposals) > MAX_PROPOSALS:
        raise refuse("obligation_invalid", "malformed", "persisted proposals are a bounded list", record="persisted map")
    parsed = [_proposal(row) for row in proposals]
    if len({row["id"] for row in parsed}) != len(parsed):
        raise refuse("obligation_invalid", "malformed", "persisted proposal ids are unique", record="persisted map")
    if value["coordinator_slot"] is not None:
        _ident(value["coordinator_slot"], "coordinator slot")
    quiescent = value["quiescence"]
    if quiescent is not None:
        fields = {"state"} if isinstance(quiescent, dict) and quiescent.get("state") == "closable" else {"state", "blocked", "interim_report"}
        _exact(quiescent, fields, fields, "quiescence")
        if quiescent["state"] not in ("closable", "quiescent") or "blocked" in quiescent and not isinstance(quiescent["blocked"], list):
            raise refuse("obligation_invalid", "malformed", "persisted quiescence is a recorded projection", record="quiescence")
        if "interim_report" in quiescent:
            # This concatenates individually bounded inputs; the enclosing
            # record bounds its size, rather than one input's text limit.
            text = quiescent["interim_report"]
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise refuse("obligation_invalid", "malformed", "quiescence retains its derived report text", record="quiescence")
    if value["closure"] is not None:
        closed = _closure_record(value["closure"])
        if any(type(closed[field]) is not int or closed[field] != value[field] for field in ("seq", "revision")) or not isinstance(closed["report"], dict):
            raise refuse("obligation_invalid", "malformed", "closure retains its map sequence and report", record="closure")
    reopened = value["reopened"]
    if not isinstance(reopened, list) or len(reopened) > 8:
        raise refuse("obligation_invalid", "malformed", "reopen history is a bounded list", record="persisted map")
    for raw in reopened:
        row = _exact(raw, {"seq", "revision", "instruction", "closed_revision", "question", *CHANNEL_FIELDS}, {"seq", "revision", "instruction", "closed_revision"}, "reopen")
        owner_decision(row, kind="question" if "question" in row else "reopen", stored=True)
        _text(row["instruction"], "instruction", limit=1024)
        if (type(row["seq"]) is not int or not 1 <= row["seq"] <= value["seq"]
                or any(type(row[field]) is not int or not 1 <= row[field] <= value["revision"] for field in ("revision", "closed_revision"))):
            raise refuse("obligation_invalid", "malformed", "reopen history retains its recorded sequence", record="reopen")
    observation = _exact(value["observations"], {"ceiling", "outstanding", "delegation", "inputs", "governance_current", "governance_stale", "policy_gone", "serialization", "churn"},
                         {"ceiling", "outstanding", "delegation", "inputs", "governance_current", "governance_stale", "policy_gone", "serialization", "churn"}, "observations")
    if (type(observation["ceiling"]) is not int or observation["ceiling"] < 0
            or observation["delegation"] not in ("available", "unavailable", "unknown")
            or type(observation["governance_stale"]) is not bool):
        raise refuse("obligation_invalid", "malformed", "observations retain their measured input types", record="observations")
    _text(observation["inputs"], "inputs")
    if observation["governance_current"] is not None:
        _text(observation["governance_current"], "governance_current")
    for field in ("outstanding", "policy_gone", "serialization", "churn"):
        if not isinstance(observation[field], list):
            raise refuse("obligation_invalid", "malformed", "observation rows are recorded lists", record="observations")
    for field in ("policy_gone", "churn"):
        for identity in observation[field]: _ident(identity, field)
    for raw in observation["outstanding"]:
        row = _exact(raw, {"admission", "boundary"}, {"admission", "boundary"}, "outstanding observation")
        _text(row["admission"], "admission")
        boundary(row["boundary"])
    for raw in observation["serialization"]:
        row = _exact(raw, {"obligation", "since_seq", "holder"}, {"obligation", "since_seq", "holder"}, "serialization observation")
        for field in ("obligation", "holder"): _ident(row[field], field)
        if type(row["since_seq"]) is not int or not 1 <= row["since_seq"] <= value["seq"]:
            raise refuse("obligation_invalid", "malformed", "serialization observation retains its sequence", record="observations")


def _persisted_rows(state: dict, ctx: dict, *, unverifiable: list[str] | None = None) -> dict:
    """Canonical persisted rows, shared by completed writes and closure reads."""
    from .assurance import validate_receipts, validate_findings
    validate_map_record(state)
    raw = state.get("obligations")
    row_bounds(raw)
    parsed = [_structure(row) for row in raw]
    if parsed != raw:
        raise refuse("obligation_invalid", "malformed", "persisted rows retain their canonical structure", record="obligation map")
    rows = {row["id"]: row for row in parsed}
    if len(rows) != len(parsed):
        raise refuse("obligation_invalid", "malformed", "persisted obligation ids are unique", record="obligation map")
    ctx["lineage"] = Lineage(rows)
    observed = ctx.get("governance")
    if observed is None and callable(ctx.get("bound_governance")):
        observed = ctx["bound_governance"](state["governance"])
    if observed is not None:
        sources, base = bind_governance(observed, state["governance"]["base_ref"], state["governance"]["exclude"])
        if sources != state["governance_sources"] or base != state["governance"]["base"]:
            raise refuse("obligation_invalid", "cite_not_base", "persisted sources match their bound base snapshot", record="persisted governance")
    for ob in rows.values():
        if type(ob.get("introduced_seq")) is not int or not 1 <= ob["introduced_seq"] <= state["seq"] or ob.get("definition") != definition_id(ob):
            raise refuse("obligation_invalid", "malformed", "persisted obligations retain introduction and definition identities", obligation=ob["id"])
        from .pod_mapping import validate_epoch
        validate_epoch(ob, seq=state["seq"], lineage=ctx["lineage"])
        if "parent" in ob and (ob["parent"] not in rows or ob["kind"] == "correction" and rows[ob["parent"]]["kind"] != "assurance"):
            raise refuse("obligation_invalid", "no_parent", "persisted parent names its recorded obligation", obligation=ob["id"])
        if ob["provenance"] == "project_policy":
            source = _exact(ob.get("source"), {"path", "lines", "revision", "base", "quote_sha256", "gone"},
                            {"path", "lines", "revision", "base", "quote_sha256", "gone"}, "persisted citation", obligation=ob["id"])
            if (source["path"] not in GOVERNANCE_PATHS or not isinstance(source["lines"], str)
                    or _LINES.fullmatch(source["lines"]) is None or type(source["gone"]) is not bool
                    or not isinstance(source["base"], str) or _COMMIT.fullmatch(source["base"]) is None
                    or not isinstance(source["quote_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", source["quote_sha256"]) is None
                    or source["revision"] is not None and (not isinstance(source["revision"], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", source["revision"]) is None)):
                raise refuse("obligation_invalid", "malformed", "persisted citation retains its bound source identity", obligation=ob["id"])
            match = _LINES.fullmatch(source["lines"])
            first, last = int(match.group(1)), int(match.group(2) or match.group(1))
            if first > last or last - first >= MAX_CITED_LINES or source["path"] not in {row["path"] for row in state["governance_sources"]}:
                raise refuse("obligation_invalid", "cite_not_found", "persisted citation names declared bounded lines", obligation=ob["id"])
            if observed is None or observed.get("status") != "observed":
                raise refuse("governance_unavailable", "governance_unavailable", "persisted policy citation needs its bound source snapshot", obligation=ob["id"])
            cited = (_relocate(source, observed, state["governance_sources"]) if source["gone"]
                     else _cite(source, observed, state["governance_sources"], obligation=ob["id"]))
            if cited != source:
                raise refuse("obligation_invalid", "cite_not_base", "persisted citation and withdrawal match their bound source text", obligation=ob["id"])
        elif "renews" not in ob:
            from .issue_revision import introduced
            _introduce(ob, ob["introduced_seq"] == 1, ob["provenance"] == "user_direct", None, [], list(ctx.get("criteria", [])),
                       issue_revision=introduced(state, ob))
        if ob["state"] == "withdrawn":
            withdrawal = _exact(ob["withdrawal"], fields("withdrawal"), required("withdrawal"),
                                "withdrawal", obligation=ob["id"])
            if withdrawal.get("by") == "issue_revision":
                _exact(withdrawal, {"by", "reason", "decision"}, {"by", "reason", "decision"}, "issue revision withdrawal")
                _text(withdrawal["reason"], "withdrawal reason", limit=1024)
                if not any(row.get("operation") == "issue_revision" and row["seq"] == withdrawal["decision"]
                           and ob["id"] in row["retired"] for row in state.get("revision_decisions", [])):
                    raise refuse("obligation_invalid", "withdrawal_unauthorized", "withdrawal needs its exact issue revision", obligation=ob["id"])
            else:
                instruction = withdrawal.get("instruction") if isinstance(withdrawal, dict) else None
                authority = _authority({"provenance": "user_direct", "instruction": instruction, **channel_fields(withdrawal)}, kind="withdrawal", stored=True) if isinstance(withdrawal, dict) and withdrawal.get("by") == "user_direct" else None
                if _withdrawal(ob, authority is not None, authority) != withdrawal:
                    raise refuse("obligation_invalid", "withdrawal_unauthorized", "persisted withdrawal retains its complete authority record", obligation=ob["id"])
        unknown = validate_receipts(ob, ctx, seq=state["seq"])
        if unknown and unverifiable is not None:
            unverifiable.append(ob["id"])
        validate_findings(ob, rows, seq=state["seq"], lineage=ctx["lineage"])
    _ancestry(rows, lineage=ctx["lineage"])
    return rows


def _close(state: dict, ctx: dict) -> dict:
    _persisted_rows(state, ctx)
    open_rows = [ob["id"] for ob in state["obligations"] if ob["state"] not in TERMINAL]
    if open_rows:
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "an objective closes only when every obligation is satisfied or withdrawn",
                     obligation=open_rows[0])
    if _outstanding(ctx) or undispositioned(ctx):
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "closure waits for settled admissions and dispositions", record='closure')
    if ctx.get("governor_pending"):
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "closure waits for pending governed effects to settle", record='closure',
                     next_action="settle each pending or UNKNOWN Governor row from Pod's readback with "
                                 "governor-reconcile, or record its outcome with the evidence it needs, then close")
    closure = {"seq": state["seq"], "revision": state["revision"]}
    # The ledger supplies the clock only to a new write; replay retains the recorded instant verbatim.
    recorded = state.get("closure")
    stamp = recorded.get("at") if recorded is not None else ctx.get("closure_at")
    if stamp is not None:
        from .util import normalize_timestamp
        closure["at"] = normalize_timestamp(stamp, field="Closure time", code="obligation_invalid")
    if ctx.get("closure_preserved"):
        # Superseded derived validations a delivered objective's closure preserved UNKNOWN (C11); the journal
        # keeps them exactly as recorded.
        closure["preserved"] = [dict(row) for row in ctx["closure_preserved"]]
    return {**closure, "report": report_projection({**state, "closure": closure}, ctx)}


CLOSURE_FIELDS = {"seq", "revision", "report"}
PRESERVED_FIELDS = {"record", "kind", "recorded", "outcome", "reason", "pr", "merged_head"}


def _closure_record(value: object) -> dict:
    """A stored closure: its sequence, revision and report, and optionally the rows it preserved (C11)."""
    closed = _exact(value, CLOSURE_FIELDS | {"preserved", "at"}, CLOSURE_FIELDS, "closure")
    if "at" in closed:
        from .util import normalize_timestamp
        if normalize_timestamp(closed["at"], field="Closure time", code="obligation_invalid") != closed["at"]:
            raise refuse("obligation_invalid", "malformed", "closure retains its recorded UTC time", record="closure")
    rows = closed.get("preserved")
    if "preserved" in closed and (
            not isinstance(rows, list) or not 0 < len(rows) <= 64
            or any(not isinstance(row, dict) or set(row) != PRESERVED_FIELDS
                   or not isinstance(row["record"], str) or not 0 < len(row["record"]) <= 128
                   or row["kind"] != "workflow_dispatch" or row["recorded"] not in ("pending", "UNKNOWN")
                   or row["outcome"] != "UNKNOWN" or row["reason"] != "superseded_by_merged_head"
                   or type(row["pr"]) is not int or row["pr"] < 1
                   or not isinstance(row["merged_head"], str) or _COMMIT.fullmatch(row["merged_head"]) is None
                   for row in rows)
            or len({row["record"] for row in rows}) != len(rows)):
        raise refuse("obligation_invalid", "malformed", "closure preserves only superseded derived validations",
                     record="closure")
    return closed


def validate_closed_snapshot(state: dict, ctx: dict) -> tuple[str, ...]:
    """Recheck recorded closure with the same row, closure and report contracts."""
    closure = _closure_record(state.get("closure"))
    for key in ("seq", "revision"):
        if type(state.get(key)) is not int or state[key] < 1 or type(closure[key]) is not int or closure[key] != state[key]:
            raise refuse("obligation_invalid", "malformed", "closure sequence and revision match the map", record="closure")
    raw = state.get("obligations")
    if not isinstance(raw, list) or not raw:
        raise refuse("obligation_invalid", "malformed", "closure needs its complete obligation map", record="closure")
    unverifiable = []
    rows = _persisted_rows(state, ctx, unverifiable=unverifiable)
    criteria = set(ctx.get("criteria", []))
    from .pod_mapping import superseded_originals
    superseded = superseded_originals(state, rows, ctx)
    original = {ob["source"]["ref"] for ob in rows.values()
                if ob["provenance"] == "objective" and ob["id"] not in superseded}
    if not original <= criteria or not criteria <= _covered(rows, ctx):
        raise refuse("obligation_invalid", "malformed", "closure retains every original criterion and covers its recorded criteria", record="closure")
    _account(state, rows, state["seq"], ctx, bound_governance_digest(state))
    _waits(state, rows, ctx)
    if state["coordinator_slot"] is not None or state["quiescence"] != quiescence(state):
        raise refuse("obligation_invalid", "malformed", "closure retains its derived slot and quiescence", record="closure")
    if any(properties(state, ctx).values()):
        raise refuse("obligation_invalid", "malformed", "closure retains all map invariants", record="closure")
    legacy_report = "decision_channels" not in closure["report"]
    if legacy_report:
        from .util import map_decisions
        if ("proof_base" in state["governance"] or "revision_decisions" in state
                or any("channel" in row["decision"] and row["decision"].get("channel_recorded", True)
                       for row in map_decisions(state))):
            raise refuse("obligation_invalid", "malformed", "a channel-aware closure retains its decision report marker", record="closure")
    expected = _close(state, {**ctx, "legacy_decision_report": legacy_report})
    if closure != expected:
        raise refuse("obligation_invalid", "malformed", "closure report matches the validated map and evidence", record="closure")
    return tuple(sorted(set(unverifiable)))


def brief_map(criteria: list[str], value: Any, ctx: dict) -> dict:
    """R39/A43 at the execution brief: the draft intake map, validated without persisting it.

    An execution brief always carries its map. The draft runs through the same intake write
    the checkpoint boundary accepts, against read-only governance facts, so a Plan Mode
    brief is checked exactly as it would be bound and nothing is written.
    """
    if not isinstance(value, dict) or not isinstance(value.get("obligations"), list):
        raise refuse("obligation_unaccounted", "map_missing",
                     "an execution brief records its obligation map with every original criterion", record='brief map')
    draft = accept_write(None, value, {**ctx, "criteria": list(criteria), "brief": True})
    return {"draft": True, "persisted": False, "criteria_covered": list(criteria), **draft}


def _covered(rows: dict[str, dict], ctx: dict | None = None) -> set[str]:
    from .pod_mapping import effective_ref
    original = {ob["source"].get("ref") for ob in rows.values()
                if ob["provenance"] in ("objective", "user_direct") and isinstance(ob.get("source"), dict)}
    return original | {ref for ob in rows.values() if (ref := effective_ref(ob, ctx)) is not None}


# --------------------------------------------------------------------------- admission

def packet_binding(body: dict) -> dict:
    """Validate the pod-packet/v3 obligation binding fields."""
    serves = body.get("serves")
    if (not isinstance(serves, list) or not serves or len(serves) > MAX_SERVES
            or len(set(serves)) != len(serves)):
        raise refuse("unbound_assignment", "missing", "a packet serves one or more obligations", record='packet binding')
    for item in serves:
        _ident(item, "served obligation", code="unbound_assignment")
    if body.get("role") not in ROLES:
        raise refuse("unbound_assignment", "role_kind_mismatch", "role allowed: " + ", ".join(ROLES), record='packet binding')
    if type(body.get("map_revision")) is not int or body["map_revision"] < 1:
        raise refuse("unbound_assignment", "out_of_revision", "a packet binds the map revision it serves", record='packet binding')
    declared = boundary(body.get("boundary"), code="unbound_assignment")
    for key in ("resolves", "stop_condition", "delta_from"):
        if key in body:
            _text(body[key], key, code="unbound_assignment")
    return {"serves": list(serves), "role": body["role"], "boundary": declared,
            "map_revision": body["map_revision"]}


def admission_refusal(state: dict | None, body: dict, ctx: dict, *, admission_id: str) -> dict:
    """R85/R86/R89: refuse an assignment that serves nothing recorded, overlaps or is closed."""
    if not isinstance(state, dict) or state.get("obligations") is None:
        raise refuse("unbound_assignment", "no_map", "delegation needs an obligation map first", record='admission refusal')
    if state.get("closure"):
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=state["closure"]["revision"])
    binding = packet_binding(body)
    if binding["map_revision"] != state["revision"]:
        raise refuse("unbound_assignment", "out_of_revision", "the packet names another map revision",
                     packet=binding["map_revision"], current=state["revision"])
    rows = {ob["id"]: ob for ob in state["obligations"]}
    proposals = {row["id"] for row in state.get("proposals", [])}
    lineage = Lineage(rows)
    ctx = {**ctx, "lineage": lineage}
    for served in binding["serves"]:
        if lineage.effective(served) != served:
            raise refuse("unbound_assignment", "renewed", "new admissions serve the live renewal tip",
                         obligation=served, live_tip=lineage.effective(served))
        if served in proposals:
            raise refuse("unbound_assignment", "proposed", "a proposal is not an obligation", obligation=served)
        ob = rows.get(served)
        if ob is None:
            raise refuse("unbound_assignment", "missing", "the served obligation is not in the map", obligation=served)
        if ob["state"] in TERMINAL:
            raise refuse("unbound_assignment", ob["state"], f"the served obligation is {ob['state']}",
                         obligation=served)
        if _accepted_assurance_current(ob, ctx, bound_governance_digest(state)):
            raise refuse("unbound_assignment", "assurance_still_bound",
                         "an accepted assurance receipt is still valid", obligation=served)
        busy = _serving(ctx, served, outstanding_only=True)
        if busy:
            raise refuse("unbound_assignment", "obligation_busy", "the obligation already has an outstanding admission",
                         obligation=served, admission=busy[0])
    kinds = {rows[served]["kind"] for served in binding["serves"]}
    if binding["role"] == "review" and (kinds != {"assurance"} or len(binding["serves"]) != 1):
        raise refuse("unbound_assignment", "role_kind_mismatch", "a review attempt serves exactly one assurance obligation", record='admission refusal')
    if binding["role"] != "review" and "assurance" in kinds:
        raise refuse("unbound_assignment", "role_kind_mismatch", "only a review attempt serves an assurance obligation", record='admission refusal')
    from .assurance import reserve_capacity
    for served in binding["serves"]:
        if rows[served]["kind"] == "assurance":
            reserve_capacity(rows[served], {**ctx, "accepted_map": state}, seq=state["seq"] + 1)
    if binding["role"] == "investigate" and not (body.get("resolves") and body.get("stop_condition")):
        raise refuse("unbound_assignment", "missing_resolves", "an investigation names resolves and stop_condition", record='admission refusal')
    if binding["role"] == "implement":
        admissions = _admissions(ctx)
        for key in _outstanding(ctx):
            found = overlap(binding["boundary"], _admission_boundary(admissions.get(key, {})))
            if found["paths"] or found["surfaces"]:
                raise refuse("ownership_conflict", "ownership_conflict", "the boundary overlaps an outstanding admission",
                             admission=key, paths=",".join(found["paths"][:8]), surfaces=",".join(found["surfaces"]))
        for ob in rows.values():
            if (ob["state"] == "active" and ob["executor"] == "coordinator"
                    and ob["id"] not in binding["serves"]):
                found = overlap(binding["boundary"], ob["boundary"])
                if found["paths"] or found["surfaces"]:
                    raise refuse("ownership_conflict", "ownership_conflict",
                                 "the boundary overlaps the coordinator-held obligation", obligation=ob["id"],
                                 paths=",".join(found["paths"][:8]), surfaces=",".join(found["surfaces"]))
        for key, row in undispositioned(ctx).items():
            if (row.get("changed_paths") is None
                    or (row.get("result") or {}).get("paths_status") not in (None, "observed")):
                raise refuse("integration_pending", "integration_pending",
                             "the settled result has no complete changed-path evidence",
                             admission=key)
            found = overlap(binding["boundary"], _admission_boundary(row))
            if found["paths"] or found["surfaces"]:
                raise refuse("integration_pending", "integration_pending",
                             "a settled result without a disposition overlaps this boundary",
                             admission=key, paths=",".join(found["paths"][:8]))
    return binding


def admission_binding(body: dict, ctx: dict, state: dict) -> dict:
    """The R41 context an attempt runs under: policy, environment, dependencies, bound sources."""
    verification = ctx.get("verification") or {}
    sources = [*(body.get("sources") or []),
               *({"path": ref["path"], "state": "present", "sha256": ref["sha256"]}
                 for ref in (body.get("context") or []) if ref.get("kind") in ("source", "instruction"))]
    return {"policy_revision": ctx.get("policy_revision"), "environment": verification.get("environment"),
            "dependencies": list(verification.get("dependencies", [])), "sources": sources,
            "governance": bound_governance_digest(state),
            "definitions": {ob["id"]: definition_id(ob) for ob in state["obligations"]
                            if ob["id"] in body["serves"]}}


def reserved_admission_shape(body: dict, ctx: dict, state: dict) -> dict:
    """Fields shared by the map's admission preview and the persisted reservation."""
    binding = packet_binding(body)
    return {"serves": binding["serves"], "role": binding["role"],
            "boundary": binding["boundary"], "state": "reserved",
            "disposition": None, "changed_paths": None,
            "candidate": ctx.get("candidate"), "report": None,
            "binding": admission_binding(body, ctx, state)}


def admit(state: dict, body: dict, ctx: dict, *, admission_id: str, accompanying: dict | None = None) -> dict:
    """The map write that accompanies an admission row: served obligations become active."""
    binding = admission_refusal(state, body, ctx, admission_id=admission_id)
    base = dict(accompanying) if accompanying is not None else {}
    if set(base) - fields("admission map"):
        raise refuse("obligation_invalid", "malformed", "an admission carries only obligations and proposals; unsupported "
                     + ", ".join(_field_names(set(base) - {"seq", "obligations", "proposals"})[0]), record="admission map", accepted=fields("admission map"), optional=fields("admission map"))
    obligations = [dict(row) for row in base.get("obligations", state["obligations"])]
    for row in obligations:
        if row.get("id") in binding["serves"]:
            row["state"] = "active"
            row["executor"] = admission_id
            for key in ("wait", "external"):
                row.pop(key, None)
    value = {"obligations": obligations, "proposals": base.get("proposals", state.get("proposals", []))}
    if "seq" in base:
        value["seq"] = base["seq"]
    admissions = {**_admissions(ctx), admission_id: reserved_admission_shape(body, ctx, state)}
    outstanding = list(dict.fromkeys([*_outstanding(ctx), admission_id]))
    return accept_write(state, value, {**ctx, "admissions": admissions, "outstanding": outstanding})


# --------------------------------------------------------------------------- results and review

@input_record("disposition")
def disposition(row: dict, request: Any, ctx: dict, *, seq: int) -> dict:
    """One disposition for a settled implementation result; a record, never a deletion."""
    record = _exact(request, {"admission", "integrated_into", "discarded", "reason", "attestation"},
                    {"admission"}, "disposition", code="obligation_unaccounted")
    key = record["admission"]
    if row.get("role") != "implement" or key in _outstanding(ctx) or row.get("state") == "deferred":
        raise refuse("obligation_unaccounted", "disposition_invalid",
                     "only a settled implementation result takes a disposition", admission=key)
    if row.get("disposition") is not None:
        raise refuse("obligation_unaccounted", "disposition_invalid", "a result has exactly one disposition",
                     admission=key)
    if ("integrated_into" in record) == ("discarded" in record):
        raise refuse("obligation_unaccounted", "disposition_invalid",
                     "a disposition is integrated_into or discarded", admission=key)
    for field in ("reason", "attestation"):
        if field in record:
            _text(record[field], field, code="obligation_unaccounted", detail="disposition_invalid", limit=bound("disposition", field))
    if "discarded" in record:
        if record["discarded"] is not True or "reason" not in record:
            raise refuse("obligation_unaccounted", "disposition_invalid", "discarding a result needs a reason",
                         admission=key)
        return {"kind": "discarded", "reason": record["reason"], "seq": seq}
    candidate = _text(record["integrated_into"], "integrated_into", code="obligation_unaccounted",
                      detail="disposition_invalid", limit=bound("disposition", "integrated_into"))
    result = row.get("result") or {}
    if row.get("changed_paths") is None:
        if result.get("base") is not None or "attestation" not in record:
            raise refuse("obligation_unaccounted", "disposition_invalid",
                         "consume the report so Git records the result before integrating it; "
                         "only a result without a Git base integrates by attestation", admission=key)
        out = {"kind": "integrated_into", "candidate": candidate, "validated": "attestation",
               "attestation": record["attestation"], "boundary_check": "unavailable", "seq": seq}
        if "reason" in record:
            out["reason"] = record["reason"]
        return out
    if row.get("boundary_exceeded") and "reason" not in record:
        raise refuse("obligation_unaccounted", "disposition_invalid",
                     "a boundary-exceeded result needs reason; use dispositions "
                     f"[{{'admission':'{key}','integrated_into':'<candidate>','reason':'<why>'}}]",
                     next_action="add reason to this exact disposition and re-read with pod internal map",
                     admission=key, missing="reason")
    head = result.get("head")
    if isinstance(head, str) and head != result.get("base"):
        ancestor = ctx.get("is_ancestor")
        if ancestor is None or ancestor(head, candidate) is not True:
            raise refuse("obligation_unaccounted", "disposition_invalid",
                         "integrated_into is validated by Git ancestry of the result commit", admission=key,
                         candidate=candidate)
        validated = "ancestry"
    else:
        if "attestation" not in record:
            raise refuse("obligation_unaccounted", "disposition_invalid",
                         "a result without a commit needs a coordinator attestation", admission=key)
        validated = "attestation"
    out = {"kind": "integrated_into", "candidate": candidate, "validated": validated, "seq": seq}
    for field in ("reason", "attestation"):
        if field in record:
            out[field] = record[field]
    return out




# --------------------------------------------------------------------------- reporting



def report_projection(state: dict, ctx: dict, *, standing_normal: bool = False) -> dict:
    """R43 report content derived from the map: nothing here is asserted by a caller."""
    rows = state["obligations"]
    legacy_report = ctx.get("legacy_decision_report", False)
    admissions = _admissions(ctx)
    status = ("closed" if state.get("closure") else
              "quiescent" if (state.get("quiescence") or {}).get("state") == "quiescent" else "open")
    downgrades = [finding for ob in rows if ob["kind"] == "assurance" for finding in ob.get("findings", [])
                  if finding["severity"] in ("blocker", "major") and finding["triage"] == "advisory"]
    blockers = [{"obligation": ob["id"], **ob["external"]} for ob in rows if ob["state"] == "blocked_external"]
    blockers += [{"obligation": ob["id"], "class": ob["wait"]["class"], "referent": ob["wait"]["referent"]}
                 for ob in rows if ob["state"] == "waiting" and ob["wait"]["class"] in EXTERNAL_WAITS]
    result = {"status": status,
              "satisfied": [ob["id"] for ob in rows if ob["state"] == "satisfied"],
              "unfinished": [ob["id"] for ob in rows if ob["state"] not in TERMINAL],
              "blockers": blockers,
              "withdrawn": [{"obligation": ob["id"], "provenance": ob["provenance"],
                             **(decision_channel(ob["withdrawal"]) if not legacy_report and ob["withdrawal"]["by"] == "user_direct" else ob["withdrawal"])}
                            for ob in rows if ob["state"] == "withdrawn"],
              "triage_downgrades": downgrades,
              "boundary_exceeded": [{"admission": key, "paths": row["boundary_exceeded"],
                                     "disposition": row.get("disposition")}
                                    for key, row in sorted(admissions.items()) if row.get("boundary_exceeded")],
              "result_over_limit": [{"admission": key, "path_count": row["result"]["path_count"]}
                                    for key, row in sorted(admissions.items())
                                    if (row.get("result") or {}).get("paths_status") == "over_limit"],
              "proposals_out_of_scope": [{"id": row["id"], "source": row["source"], "summary": row["summary"]}
                                         for row in state.get("proposals", []) if row["status"] == "open"],
              "label": label_qualification(state, ctx, ctx.get("candidate"))}
    if any(ob["state"] == "renewed" for ob in rows):
        result["renewed"] = [{"obligation": ob["id"], **ob["renewal"]} for ob in rows if ob["state"] == "renewed"]
    if any(ob["kind"] == "delivery" for ob in rows):
        delivery_status = ctx.get("governor_delivery") or {}
        result["delivery_authorization"] = delivery_status.get("units") or {
            "unprepared": {"publish": "MISSING", "merge": "MISSING"}}
        result["hosted_checks"] = delivery_status.get("hosted_checks", "NOT_RUN")
    if state.get("governance_history", {}).get("decisions"):
        result["governance_decisions"] = [deepcopy(row) if legacy_report else decision_channel(row)
                                           for row in state["governance_history"]["decisions"]
                                           if row.get("provenance") != "derived"]
        from .util import derived_governance_summary
        derived = derived_governance_summary(state)
        if derived and not legacy_report:
            result["derived_governance_decisions"] = derived
    from .util import map_decisions
    decisions = ctx.get("owner_decisions", []) + map_decisions(state)
    if decisions and not legacy_report:
        if state.get("closure"):
            from .standing import decision_time
            decisions = [{**row, "decision": {**{key: row["decision"][key] for key in ("id", "operation", "channel")},
                                                 "decided_at": decision_time(row["decision"])}}
                         if row["kind"] == "standing_authorization" else row for row in decisions]
        result["owner_decisions"] = decisions
    if not legacy_report:
        result["decision_channels"] = 1
        context = ctx.get("standing_context", {})
        has_decisions = bool(context.get("standing_authorization", {}).get("decisions"))
        if has_decisions or standing_normal:
            from .standing import projection as standing_projection
            closure_utc = ((state.get("closure") or {}).get("report", {}).get(
                "standing_authorization", {}).get("at_closure", {}).get("utc")
                or ((context.get("checkpoint") or {}).get("closure") or {}).get(
                    "report", {}).get("standing_authorization", {}).get("at_closure", {}).get("utc"))
            result["standing_authorization"] = standing_projection(
                {**context, "checkpoint": {**(context.get("checkpoint") or {}), **state}}, ctx.get("standing_journal"),
                closure_utc=closure_utc, compact_answers=bool(state.get("closure")))
            if has_decisions:
                result["decision_channels"] = 3
    if state.get("delivery") is not None:
        result["delivery"] = deepcopy(state["delivery"])
    if status == "quiescent":
        result["interim_report"] = state["quiescence"]["interim_report"]
    elif status == "open" and result["unfinished"]:
        result["interim_report"] = "incomplete — " + str(len(result["unfinished"])) + " obligation(s) unfinished"
    if any(row.get("operation") == "issue_revision" for row in state.get("revision_decisions", [])):
        result["decision_channels"] = 4
    return result


def observations(prior: dict | None, state: dict, ctx: dict) -> dict:
    """Recorded observations, never gates: counts, flags and the inputs a rationale binds."""
    admissions = _admissions(ctx)
    outstanding = [{"admission": key, "boundary": _admission_boundary(admissions.get(key, {}))}
                   for key in sorted(_outstanding(ctx))]
    current = {"ceiling": ctx.get("ceiling", 0), "outstanding": outstanding,
               "delegation": ctx.get("delegation", "unknown"),
               "inputs": digest({"candidate": ctx.get("candidate"), "revision": state["revision"],
                                 "coordinator_slot": state.get("coordinator_slot")}),
               "governance_current": ctx.get("governance_current"),
               "governance_stale": (ctx.get("governance_current") is not None
                                    and ctx["governance_current"] !=
                                    (state.get("delivery") or {}).get("result", state["governance"]["base"])),
               "policy_gone": [ob["id"] for ob in state["obligations"]
                               if ob["provenance"] == "project_policy" and ob.get("source", {}).get("gone")]}
    snapshot = {**state, "observations": current}
    current["serialization"] = serialization_flags(prior, snapshot)
    current["churn"] = [ob["id"] for ob in state["obligations"]
                        if ob["state"] != "satisfied" and len(_settled_serving(ctx, ob["id"])) >= CHURN_THRESHOLD]
    return current


def serialization_flags(prior: dict | None, state: dict) -> list[dict]:
    """R91: an unexplained sequenced wait across two accepted writes on independent work.

    The flag is an observation for traces and status, never a refusal. A sequenced wait
    records sequencing as its one controlling reason, so the waiting obligation carries no
    recorded dependency; it is independent when its boundary overlaps neither the
    coordinator-held boundary nor an outstanding admission. The flag is suppressed when
    delegation is not positively available, the ceiling is zero or no capacity is free. A rationale and
    revisit condition bound to the current inputs explain the wait; that is a presence
    and binding check, not a judgment of the rationale's truth.
    """
    if not isinstance(prior, dict) or prior.get("obligations") is None:
        return []
    before, now = prior.get("observations") or {}, state.get("observations") or {}
    if (now.get("delegation") != "available" or before.get("delegation") != "available"
            or not now.get("ceiling") or not before.get("ceiling")):
        return []
    if (len(now.get("outstanding", [])) >= now["ceiling"]
            or len(before.get("outstanding", [])) >= before["ceiling"]):
        return []
    rows = {ob["id"]: ob for ob in state["obligations"]}
    earlier = {ob["id"]: ob for ob in prior["obligations"]}
    holder = rows.get(state.get("coordinator_slot") or "")
    flags = []
    for ob in rows.values():
        old = earlier.get(ob["id"])
        if not (ob["state"] == "waiting" and ob["wait"]["class"] == "sequenced"
                and old is not None and old["state"] == "waiting" and old["wait"]["class"] == "sequenced"):
            continue
        if holder is None:
            continue
        busy = [row["boundary"] for row in now.get("outstanding", [])] + [holder["boundary"]]
        if any(overlaps(ob["boundary"], other) for other in busy):
            continue
        wait = ob["wait"]
        if wait.get("rationale") and wait.get("revisit_when") and wait.get("inputs") == now.get("inputs"):
            continue
        flags.append({"obligation": ob["id"], "since_seq": prior["seq"], "holder": holder["id"]})
    return flags


def retained_history(state: dict) -> list[dict]:
    """Read-only counts of the fully hydrated history on frozen rows."""
    return [{"id": ob["id"], "state": ob["state"], "retained_receipts": len(ob.get("receipts", [])),
             "retained_findings": len(ob.get("findings", []))}
            for ob in state["obligations"] if ob["state"] in FROZEN]


def status_projection(state: dict | None, ctx: dict) -> dict:
    """Human and JSON status for the map: every state with its referent."""
    if not isinstance(state, dict) or state.get("obligations") is None:
        return {"map": None, "lines": ["Map        none (kernel inert)"]}
    rows = state["obligations"]
    retained = retained_history(state)
    history = {row["id"]: row for row in retained}
    counts = {name: sum(ob["state"] == name for ob in rows) for name in STATES}
    lines = [f"Objective  rev {state['revision']} · seq {state['seq']} · "
             + ("closed" if state.get("closure") else "open"),
             "Map        " + f"{len(rows)} obligations: " + " · ".join(
                 f"{count} {name}" for name, count in counts.items() if count)
             + f" · {sum(row['status'] == 'open' for row in state.get('proposals', []))} proposal(s)"]
    for ob in rows:
        if ob["state"] == "active":
            detail = "coordinator" if ob["executor"] == "coordinator" else "worker " + ob["executor"][:12]
        elif ob["state"] == "waiting":
            detail = f"{ob['wait']['class']} → {ob['wait']['referent']}"
        elif ob["state"] == "blocked_external":
            detail = f"{ob['external']['party']}: {ob['external']['need']}"
        elif ob["state"] == "renewed":
            detail = "renewed by " + ob["renewal"]["by"]
        elif ob["state"] == "withdrawn":
            detail = f"by {ob['withdrawal']['by']}: {ob['withdrawal']['reason']}"
            if ob["withdrawal"]["by"] == "user_direct":
                channel = decision_channel(ob["withdrawal"])
                detail += " (" + channel["channel"] + ("; earlier record" if channel.get("channel_recorded") is False else "") + ")"
        else:
            detail = ob.get("check") or ob.get("resolves") or ""
        if ob["id"] in history:
            counts_kept = history[ob["id"]]
            detail = f"{counts_kept['retained_receipts']} receipt(s), {counts_kept['retained_findings']} finding(s) retained; " + detail
        lines.append(f"  {ob['id']:<4} {ob['state']:<16} {detail}"[:160])
    for key, row in sorted(_admissions(ctx).items()):
        if row.get("role") == "implement" and row.get("disposition"):
            item = row["disposition"]
            lines.append(f"Results    {key[:12]} {item['kind']}"
                         + (f" {item['candidate'][:12]}" if item["kind"] == "integrated_into" else ""))
        elif row.get("role") == "implement" and key in undispositioned(ctx):
            lines.append(f"Results    {key[:12]} awaiting disposition")
    label = label_qualification(state, ctx, ctx.get("candidate"))
    for gap in label["assurance_unbound"]:
        lines.append(f"Assurance  {gap['obligation'] or 'none'} {gap['gap']}")
    if (state.get("quiescence") or {}).get("state") == "quiescent":
        lines.append("Interim    " + state["quiescence"]["interim_report"])
    observed = state.get("observations") or {}
    for flag in observed.get("serialization", []):
        lines.append(f"Flag       serialization: {flag['obligation']} sequenced behind {flag['holder']} "
                     f"since seq {flag['since_seq']}")
    for key in observed.get("churn", []):
        lines.append(f"Flag       churn: {key} has {CHURN_THRESHOLD}+ settled admissions without satisfaction")
    if observed.get("governance_stale"):
        lines.append("Flag       governance base moved; refresh before new admission")
    if state.get("delivery") is not None:
        lines.append(f"Delivery   {state['delivery']['method']} {state['delivery']['result'][:12]}")
    def referent(ob):
        value = ob.get("executor") or ob.get("wait") or ob.get("external") or ob.get("withdrawal") or ob.get("renewal")
        return decision_channel(value) if isinstance(value, dict) and value.get("by") == "user_direct" else value

    return {"map": {"seq": state["seq"], "revision": state["revision"], "counts": counts,
                    "coordinator_slot": state.get("coordinator_slot"),
                    "quiescence": state.get("quiescence"), "closure": state.get("closure"),
                    "delivery": state.get("delivery"),
                    "retained_history": retained,
                    "observations": state.get("observations"), "label": label,
                    "obligations": [{"id": ob["id"], "state": ob["state"],
                                     "referent": referent(ob),
                                     **({"acceptances": deepcopy(ob["acceptances"])}
                                        if ob.get("acceptances") else {})}
                                    for ob in rows]},
            "lines": lines}



from .assurance import (_source_entries, _dependencies, _evidence_record, _stamp_evidence,
                        binding_current, review_completed, _reuse_valid, _bind_reuse, evidence_valid,
                        _accepted_assurance_current, triage, label_qualification)
from .traces import evaluate_trace

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
import hashlib
import re
from typing import Any, Callable

from .errors import PodError
from .util import digest

KINDS = ("criterion", "delivery", "assurance", "correction", "steer", "subgoal")
PROVENANCES = ("objective", "user_direct", "project_policy", "coordinator")
STATES = ("unassigned", "active", "waiting", "blocked_external", "satisfied", "withdrawn")
TERMINAL = ("satisfied", "withdrawn")
WAIT_CLASSES = ("dependency", "contract_unsettled", "sequenced", "ownership", "capacity",
                "integration_pending", "input_unavailable", "authority", "user_hold")
EXTERNAL_WAITS = ("authority", "user_hold", "input_unavailable")
PARTIES = ("owner", "user", "provider", "third_party")
ROLES = ("implement", "investigate", "review")
SEVERITIES = ("blocker", "major", "minor", "info")
TRIAGE = ("required_correction", "advisory")
PROPOSAL_SOURCES = ("worker_report", "reviewer_advisory", "issue", "repository",
                    "fetched_documentation", "coordinator_caution")
GOVERNANCE_PATHS = (".pod/config.yaml", "AGENTS.md", "CLAUDE.md")
AUTHORIZED_SCOPES = ("merge", "release", "deploy")
MAX_OBLIGATIONS = 96
MAX_PROPOSALS = 64
MAX_SERVES = 16
MAX_EVIDENCE = 16
MAX_FINDINGS = 32
MAX_CITED_LINES = 64
MAX_GOVERNANCE_TEXT = 1_048_576
CHURN_THRESHOLD = 3

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SURFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_LINES = re.compile(r"([1-9][0-9]{0,6})(?:-([1-9][0-9]{0,6}))?\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

NEXT_ACTIONS = {
    "unbound_assignment": "admit only a packet that serves unsatisfied obligations of the current map revision",
    "ownership_conflict": "record an ownership wait on the overlapping holder, or narrow the boundary",
    "integration_pending": "record a disposition, or establish complete paths and a nonoverlapping boundary",
    "objective_closed": "only a direct user revision with reopen continues a closed objective",
    "obligation_invalid": "correct the obligation's provenance, parent, check or citation",
    "obligation_unaccounted": "restate every obligation with exactly one currently valid state",
    "wait_invalid": "restate the wait with one controlling class and a current referent",
    "map_stale": "re-read the checkpoint and write the next sequence number",
    "governance_changed": "write a checkpoint with governance_refresh to reconcile at the new base",
    "governance_unavailable": "restore the governance source read; new work holds until it is bound",
}

# Proposed map fields a caller may send; everything else in a stored map is stamped here.
MAP_INPUT = {"seq", "obligations", "proposals", "governance", "governance_refresh",
             "revision_authority", "reopen", "close", "rebind", "dispositions"}
MAP_STORED = {"seq", "revision", "obligations", "proposals", "governance", "governance_sources",
              "coordinator_slot", "quiescence", "closure", "observations", "reopened",
              "governance_history"}


def refuse(code: str, detail: str, message: str, **referent: Any) -> PodError:
    """One boundary refusal naming its detail, referent and next safe action."""
    action = NEXT_ACTIONS.get(code, "inspect the refused record")
    named = ", ".join(f"{key}={value}" for key, value in sorted(referent.items()))
    text = f"{detail}: {message}" + (f" ({named})" if named else "") + f". Next: {action}"
    return PodError(code, text, {"detail": detail, "referent": referent, "next_action": action})


def _text(value: Any, name: str, *, code: str = "obligation_invalid", detail: str = "malformed",
          limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise refuse(code, detail, f"{name} must be bounded, nonempty text")
    return value


def _ident(value: Any, name: str, *, code: str = "obligation_invalid") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise refuse(code, "malformed", f"{name} must be a bounded identifier")
    return value


def _exact(value: Any, fields: set[str], required: set[str], name: str, *,
           code: str = "obligation_invalid") -> dict:
    if not isinstance(value, dict) or set(value) - fields or required - set(value):
        raise refuse(code, "malformed", f"{name} has missing or unsupported fields")
    return value


# --------------------------------------------------------------------------- boundaries

def _path(value: Any, *, code: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value
            or value.startswith("/") or "\\" in value):
        raise refuse(code, "malformed", "boundary paths are repo-relative")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise refuse(code, "malformed", "boundary paths stay inside the repository")
    return "/".join(parts) or "."


def boundary(value: Any, *, code: str = "obligation_invalid") -> dict:
    """Declared editing boundary: repo-relative path prefixes plus named surfaces."""
    if value is None:
        return {"paths": [], "surfaces": []}
    record = _exact(value, {"paths", "surfaces"}, set(), "boundary", code=code)
    paths, surfaces = record.get("paths", []), record.get("surfaces", [])
    if (not isinstance(paths, list) or len(paths) > 64 or not isinstance(surfaces, list)
            or len(surfaces) > 32):
        raise refuse(code, "malformed", "boundary is a bounded list of paths and surfaces")
    for surface in surfaces:
        if not isinstance(surface, str) or not _SURFACE.fullmatch(surface):
            raise refuse(code, "malformed", "boundary surfaces are bounded names")
    return {"paths": sorted({_path(item, code=code) for item in paths}),
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
                         "a Git project binds governance at its selected target branch")
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
    return digest(sources)


def _cite(source: Any, observed: dict | None, sources: list[dict]) -> dict:
    record = _exact(source, {"path", "lines", "revision", "quote_sha256", "base", "gone"},
                    {"path", "lines"}, "policy citation")
    bound = {row["path"]: row for row in sources}
    if record["path"] not in bound or bound[record["path"]]["revision"] is None:
        raise refuse("obligation_invalid", "governance_source_unrecognized",
                     "project policy cites only a declared governance source bound at intake",
                     path=record["path"])
    texts = (observed or {}).get("texts", {})
    text = texts.get(record["path"])
    if (text is None or "sha256:" + _sha(text.encode()) != bound[record["path"]]["revision"]
            or (observed or {}).get("commit") != bound[record["path"]]["base"]):
        raise refuse("obligation_invalid", "cite_not_base",
                     "the cited source is not read at the objective's bound base revision",
                     path=record["path"])
    match = _LINES.fullmatch(record["lines"]) if isinstance(record["lines"], str) else None
    lines = _lines(text)
    if match is None:
        raise refuse("obligation_invalid", "cite_not_found", "cite a line range like 38-40")
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    if first > last or last > len(lines) or last - first >= MAX_CITED_LINES:
        raise refuse("obligation_invalid", "cite_not_found", "the cited lines are absent at the base",
                     path=record["path"], lines=record["lines"])
    quote = "\n".join(lines[first - 1:last])
    if not quote.strip():
        raise refuse("obligation_invalid", "cite_not_found", "the cited lines are blank at the base",
                     path=record["path"], lines=record["lines"])
    quote_sha = _sha(quote.encode())
    if record.get("revision") not in (None, bound[record["path"]]["revision"]):
        raise refuse("obligation_invalid", "cite_not_base", "the citation names another source revision",
                     path=record["path"])
    if record.get("quote_sha256") not in (None, quote_sha):
        raise refuse("obligation_invalid", "cite_not_base",
                     "the cited text differs from the base revision's text", path=record["path"])
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

_OBLIGATION_FIELDS = {"id", "kind", "provenance", "introduced_seq", "parent", "check", "resolves",
                      "stop_condition", "boundary", "source", "state", "executor", "wait", "external",
                      "evidence", "withdrawal", "adopts", "reuse", "scope", "question", "candidate",
                      "existing_evidence", "insufficiency", "uncovered_risk", "findings", "finding",
                      "definition", "receipts"}
_DEFINITION = ("check", "resolves", "stop_condition", "scope", "question", "insufficiency",
               "existing_evidence", "uncovered_risk")
_STATE_FIELDS = {"active": "executor", "waiting": "wait", "blocked_external": "external",
                 "withdrawn": "withdrawal"}


def _structure(raw: Any) -> dict:
    row = _exact(raw, _OBLIGATION_FIELDS, {"id", "kind", "provenance"}, "obligation")
    ob = dict(row)
    _ident(ob["id"], "obligation id")
    if ob["kind"] not in KINDS or ob["provenance"] not in PROVENANCES:
        raise refuse("obligation_invalid", "malformed", "kind or provenance is unsupported", obligation=ob["id"])
    for key in ("check", "resolves", "stop_condition", "question", "insufficiency",
                "existing_evidence", "uncovered_risk"):
        if key in ob:
            _text(ob[key], key)
    if not ob.get("check") and not ob.get("resolves"):
        raise refuse("obligation_invalid", "no_check", "every obligation needs a check or the decision it resolves",
                     obligation=ob["id"])
    if ob.get("resolves") and not ob.get("stop_condition"):
        raise refuse("obligation_invalid", "no_check", "an investigation's resolves needs a stop_condition",
                     obligation=ob["id"])
    ob["boundary"] = boundary(ob.get("boundary"))
    if "parent" in ob:
        _ident(ob["parent"], "parent")
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
        ob["scope"] = boundary(ob["scope"])
        _text(ob["candidate"], "candidate", limit=128)
    else:
        for key in ("scope", "question", "candidate", "existing_evidence", "insufficiency", "uncovered_risk",
                    "findings"):
            if key in ob:
                raise refuse("obligation_invalid", "malformed", f"only assurance records {key}", obligation=ob["id"])
    if "finding" in ob and ob["kind"] != "correction":
        raise refuse("obligation_invalid", "malformed", "only a correction records its finding", obligation=ob["id"])
    if "adopts" in ob:
        _ident(ob["adopts"], "adopted proposal")
    state = ob.get("state")
    if state is None:
        raise refuse("obligation_unaccounted", "missing_state", "every obligation has exactly one state",
                     obligation=ob["id"])
    if not isinstance(state, str) or state not in STATES:
        raise refuse("obligation_unaccounted", "multiple_states" if isinstance(state, list) else "missing_state",
                     "state must be exactly one supported state", obligation=ob["id"])
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
    return ob


def _proposal(raw: Any) -> dict:
    row = _exact(raw, {"id", "source", "origin_ref", "summary", "status"},
                 {"id", "source", "origin_ref", "summary", "status"}, "proposal")
    _ident(row["id"], "proposal id")
    if row["source"] not in PROPOSAL_SOURCES or row["status"] not in ("open", "adopted"):
        raise refuse("obligation_invalid", "malformed", "proposal source or status is unsupported", proposal=row["id"])
    _text(row["origin_ref"], "origin_ref", limit=256)
    _text(row["summary"], "summary")
    return dict(row)


def _authority(value: Any) -> dict | None:
    if value is None:
        return None
    record = _exact(value, {"provenance", "instruction"}, {"provenance", "instruction"}, "revision authority")
    if record["provenance"] != "user_direct":
        raise refuse("obligation_invalid", "provenance_unauthorized",
                     "a map revision is authorized only by a direct user instruction")
    _text(record["instruction"], "instruction", limit=1024)
    return dict(record)


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


def _source_entries(value: Any, name: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} sources are a bounded list")
    rows = []
    for item in value:
        entry = _exact(item, {"path", "state", "sha256"}, {"path", "state"}, "evidence source")
        _path(entry["path"], code="obligation_invalid")
        if entry["state"] not in ("present", "absent", "unavailable"):
            raise refuse("obligation_invalid", "malformed", "an evidence source state is unsupported")
        rows.append(dict(entry))
    return rows


def _dependencies(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} dependencies are a bounded list")
    return [_text(item, "dependency", limit=512) for item in value]


def _evidence_record(ob: dict, raw: Any) -> dict:
    """The detailed pod-evidence/v1 record, structurally joined to this obligation (R41).

    The record's `criterion` names the obligation id; its check text may differ from the
    obligation's outcome description, because Pod cannot judge what a test proves. What Pod
    keeps is the binding: candidate, sources, effective Governor policy, dependencies,
    environment and the command and result that produced the status.
    """
    from .records import evidence_record
    try:
        row = evidence_record({k: v for k, v in raw.items() if k != "governance"}
                              if isinstance(raw, dict) else raw)
    except PodError as exc:
        raise refuse("obligation_invalid", "malformed", str(exc), obligation=ob["id"]) from exc
    if row["criterion"] != ob["id"]:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "an evidence record names the obligation it serves as its criterion",
                     obligation=ob["id"], criterion=str(row["criterion"])[:64])
    for field in ("candidate", "policy_revision", "environment", "check", "command", "result", "timestamp",
                  "reference"):
        _text(row[field], field)
    if "definition" not in row:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "map evidence names the obligation definition it verified", obligation=ob["id"])
    record = dict(row)
    record["sources"] = _source_entries(row["sources"], "evidence")
    record["dependencies"] = _dependencies(row["dependencies"], "evidence")
    return record


def _stamp_evidence(ob: dict, prior: dict | None, ctx: dict, gov: str, rebind: bool,
                    *, reported_attempt: str | None = None, seq: int) -> list[dict]:
    """Keep immutable receipt identities even when a caller omits the selected evidence.

    History is inside the existing obligation record, bounded by MAX_EVIDENCE, and is
    never evicted or accepted from the caller. Full history refuses another receipt;
    it cannot silently make a forgotten receipt new again.
    """
    identity = "attempt" if ob["kind"] == "assurance" else "reference"
    receipts = {item["evidence"][identity]: deepcopy(item) for item in (prior or {}).get("receipts", [])}
    if reported_attempt is not None:
        admission = _admissions(ctx)[reported_attempt]
        if review_completed(admission):
            binding = admission.get("binding") or {}
            base = {"attempt": reported_attempt, "candidate": admission.get("candidate"),
                    "binding": deepcopy(admission.get("binding")),
                    "definition": binding.get("definitions", {}).get(ob["id"]),
                    "governance": binding.get("governance")}
            previous = receipts.get(reported_attempt)
            if previous is not None and previous["evidence"] != base:
                raise refuse("obligation_invalid", "receipt_conflict",
                             "a consumed review cannot change its bound receipt", obligation=ob["id"],
                             receipt=reported_attempt)
            receipts[reported_attempt] = {**(previous or {}), "evidence": base,
                                          "reported_seq": (previous or {}).get("reported_seq", seq)}
    stamped = []
    for raw in ob.get("evidence", []):
        if ob["kind"] == "assurance":
            row = _exact(raw, {"attempt", "candidate", "binding", "governance", "definition"},
                         {"attempt"}, "assurance evidence")
            _text(row["attempt"], "attempt", limit=128)
            admission = _admissions(ctx).get(row["attempt"])
            admission = admission if isinstance(admission, dict) else {}
            base = {"attempt": row["attempt"], "candidate": admission.get("candidate"),
                    "binding": deepcopy(admission.get("binding")),
                    "definition": (admission.get("binding") or {}).get("definitions", {}).get(ob["id"])}
        else:
            base = _evidence_record(ob, raw)
        key = base[identity]
        previous = receipts.get(key)
        if previous and base != {k: v for k, v in previous["evidence"].items() if k != "governance"}:
            raise refuse("obligation_invalid", "receipt_conflict",
                         "an observed receipt cannot change its definition, candidate or content",
                         obligation=ob["id"], receipt=key)
        stamp = (previous["evidence"]["governance"] if previous else
                 (base.get("binding") or {}).get("governance") if identity == "attempt" else gov)
        if rebind:
            stamp = gov
        evidence = {**base, "governance": stamp}
        receipts[key] = {**(previous or {}), "evidence": evidence}
        stamped.append(evidence)
    if len(receipts) > MAX_EVIDENCE:
        raise refuse("obligation_invalid", "receipt_limit", "the bounded receipt history is full",
                     obligation=ob["id"])
    ob["receipts"] = list(receipts.values())
    return stamped


def binding_current(binding: Any, ctx: dict) -> bool:
    """R41: effective Governor policy, environment, dependencies and sources still hold."""
    if not isinstance(binding, dict):
        return False
    verification = ctx.get("verification")
    current_source = ctx.get("source_current")
    if (not isinstance(verification, dict) or binding.get("policy_revision") is None
            or binding.get("policy_revision") != ctx.get("policy_revision")
            or binding.get("environment") != verification.get("environment")
            or not isinstance(binding.get("dependencies"), list)
            or not set(binding["dependencies"]) <= set(verification.get("dependencies", []))
            or not isinstance(binding.get("sources"), list)):
        return False
    for entry in binding["sources"]:
        if not isinstance(entry, dict) or current_source is None or current_source(entry) is not True:
            return False
    return True


def review_completed(admission: Any) -> bool:
    """A review attempt binds assurance only when its report is a completed, validated observation.

    Completion is not approval: a completed review may carry findings, which triage turns
    into corrections that the assurance obligation waits on.
    """
    report = admission.get("report") if isinstance(admission, dict) else None
    return (isinstance(report, dict) and report.get("outcome") == "succeeded"
            and report.get("status") == "validated_observation")


def _reuse_valid(ob: dict, ctx: dict, candidate: str) -> bool:
    reuse = ob.get("reuse")
    return (isinstance(reuse, dict) and reuse.get("to") == candidate
            and reuse.get("definition") == definition_id(ob) and isinstance(reuse.get("delta"), list))


def _bind_reuse(ob: dict, prior: dict | None, ctx: dict) -> dict | None:
    """Explicit REUSE: the Git delta between candidates must leave the obligation's scope unaffected."""
    if "reuse" not in ob:
        return None
    raw = _exact(ob["reuse"], {"from", "to", "delta", "definition"}, {"from"}, "reuse")
    source = _text(raw["from"], "reuse from", limit=128)
    current = ctx.get("candidate")
    scope = ob["scope"] if ob["kind"] == "assurance" else ob["boundary"]
    if not scope["paths"]:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "reuse needs a declared path scope to show it is unaffected", obligation=ob["id"])
    previous = (prior or {}).get("reuse")
    if (isinstance(previous, dict) and previous.get("from") == source and previous.get("to") == current
            and previous.get("definition") == definition_id(ob)):
        return previous
    delta_reader: Callable | None = ctx.get("git_delta")
    delta = delta_reader(source, current) if delta_reader is not None and isinstance(current, str) else None
    if delta is None:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "the Git delta between the candidates cannot be read, so reuse is unproven",
                     obligation=ob["id"], candidate=source)
    touched = overlap({"paths": [_path(path, code="obligation_unaccounted") for path in delta], "surfaces": []},
                      {"paths": scope["paths"], "surfaces": []})
    if touched["paths"]:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "the candidate delta touches the obligation's scope; it needs fresh evidence",
                     obligation=ob["id"], paths=",".join(touched["paths"][:8]))
    return {"from": source, "to": current, "delta": sorted(delta)[:256], "definition": definition_id(ob)}


def evidence_valid(ob: dict, ctx: dict, gov: str, candidate: str | None = None) -> bool:
    """Whether a satisfied obligation's evidence is passing and currently valid (R41)."""
    current = ctx.get("candidate") if candidate is None else candidate
    rows = ob.get("evidence", [])
    if not rows:
        return False
    allowed = {current}
    if _reuse_valid(ob, ctx, current):
        allowed.add(ob["reuse"]["from"])
    for row in rows:
        if (row.get("definition") != definition_id(ob)
                or row.get("governance") != gov or row.get("candidate") not in allowed):
            return False
        if ob["kind"] == "assurance":
            admission = _admissions(ctx).get(row["attempt"])
            # Both reservation and report consumption must follow correction
            # resolution. Wall-clock admission stamps provide neither ordering.
            # Older accepted receipts can use accepted_seq as their report bound.
            receipt = next((item for item in ob.get("receipts", [])
                            if item["evidence"].get("attempt") == row["attempt"]), None)
            proof_seq = (receipt or {}).get("reported_seq", (receipt or {}).get("accepted_seq"))
            admitted_seq = admission.get("admitted_seq") if isinstance(admission, dict) else None
            superseded = any(
                finding.get("triage") == "required_correction"
                and (not isinstance(proof_seq, int) or not isinstance(admitted_seq, int)
                     or not isinstance(finding.get("recorded_seq"), int)
                     or not isinstance(finding.get("resolved_seq"), int)
                     or finding["resolved_seq"] < finding["recorded_seq"]
                     or proof_seq <= finding["resolved_seq"]
                     or admitted_seq <= finding["resolved_seq"])
                for finding in ob.get("findings", []))
            if (not isinstance(admission, dict) or admission.get("role") != "review"
                    or admission.get("serves") != [ob["id"]] or row["attempt"] in _outstanding(ctx)
                    or not review_completed(admission) or admission.get("candidate") != row.get("candidate")
                    or (admission.get("binding") or {}).get("definitions", {}).get(ob["id"]) != row["definition"]
                    or row.get("binding") != admission.get("binding")
                    or not binding_current(admission.get("binding"), ctx) or superseded):
                return False
        elif row.get("status") != "PASS" or not binding_current(row, ctx):
            return False
    return True


def _accepted_assurance_current(ob: dict, ctx: dict, gov: str) -> bool:
    """An accepted proof survives omission and unsatisfied intermediate states."""
    return ob["kind"] == "assurance" and any(
        item.get("accepted_seq") is not None and evidence_valid(
            {**ob, "evidence": [item["evidence"]], "reuse": item.get("reuse")}, ctx, gov)
        for item in ob.get("receipts", []))


# --------------------------------------------------------------------------- write

def _first(value: dict, key: str, default: Any) -> Any:
    return value[key] if key in value else default


def accept_write(prior: dict | None, value: dict, ctx: dict, *, triaged: frozenset = frozenset()) -> dict:
    """Validate one proposed map write against the prior accepted map and current facts.

    Returns the map fields to persist. Every refusal names its code, detail, referent and
    next safe action. `prior` is the previously accepted map (or None); `value` holds the
    caller's proposed fields from MAP_INPUT. Review findings are Pod's record: only the
    report boundary's triage (`triaged`) may add them.
    """
    prior_map = prior if isinstance(prior, dict) and prior.get("obligations") is not None else None
    # Dispositions change admission rows, so the ledger applies them before this write.
    unknown = set(value) - (MAP_INPUT - {"dispositions"})
    if unknown:
        raise refuse("obligation_invalid", "malformed", "unsupported map fields: " + ", ".join(sorted(unknown)))
    closed = bool(prior_map and prior_map.get("closure"))
    authority = _authority(value.get("revision_authority"))
    user = authority is not None
    reopen = value.get("reopen") is True
    if closed and not (user and reopen):
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=prior_map["closure"]["revision"])
    if prior_map is not None and "obligations" not in value:
        # A checkpoint that leaves the map unchanged still revalidates it against current facts.
        value = {**value, "obligations": deepcopy(prior_map["obligations"])}
    if value.get("obligations") is None:
        if prior_map is not None:
            raise refuse("obligation_unaccounted", "missing_state", "an obligation map cannot be removed")
        if set(value) - {"seq"}:
            raise refuse("obligation_invalid", "malformed", "map fields need an obligation list")
        return {}
    if "reopen" in value and (value["reopen"] is not True or not closed):
        raise refuse("obligation_invalid", "malformed", "reopen applies only to a closed objective")
    seq = prior_map["seq"] + 1 if prior_map else 1
    if "seq" in value and value["seq"] != seq:
        raise refuse("map_stale", "map_stale", "the proposed map is not the next write", expected=seq)
    refresh = value.get("governance_refresh") is True
    if "governance_refresh" in value and not refresh:
        raise refuse("obligation_invalid", "malformed", "governance_refresh is true when present")
    if refresh and prior_map is None:
        raise refuse("obligation_invalid", "malformed", "governance is bound at intake, then refreshed")
    revision = (prior_map["revision"] + (1 if user or refresh else 0)) if prior_map else 1

    # Governance: declared at intake, read at the base, refreshed only by an explicit revision.
    observed = ctx.get("governance")
    if prior_map is None:
        # A round-tripped stamped base is ignored: Pod binds the base it reads itself.
        declared = _exact(value.get("governance"), {"base_ref", "selection", "exclude", "base"},
                          {"base_ref"}, "governance")
        if declared["base_ref"] is not None:
            _text(declared["base_ref"], "base_ref", limit=256)
        exclude = declared.get("exclude", [])
        if (not isinstance(exclude, list) or any(item not in GOVERNANCE_PATHS for item in exclude)
                or len(set(exclude)) != len(exclude)):
            raise refuse("obligation_invalid", "governance_source_unrecognized",
                         "only declared governance sources can be excluded")
        if exclude and not user:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "only a direct user constraint excludes a governance source")
        target_ref = observed.get("target_ref") if isinstance(observed, dict) else None
        sources, base = bind_governance(observed, target_ref or declared["base_ref"], sorted(exclude))
        governance = {"base_ref": target_ref, "selection": observed.get("selection") if target_ref else None,
                      "exclude": sorted(exclude), "base": base}
    else:
        governance = prior_map["governance"]
        declared = value.get("governance")
        if declared is not None:
            if not isinstance(declared, dict) or set(declared) - {"base_ref", "selection", "exclude", "base"}:
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "governance sources are fixed at intake")
            if declared.get("exclude", governance["exclude"]) != governance["exclude"]:
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "governance source paths are fixed at intake")
            if declared.get("base_ref", governance["base_ref"]) != governance["base_ref"] and not (user and refresh):
                raise refuse("obligation_invalid", "governance_source_unrecognized",
                             "a target change needs a direct user revision and governance refresh")
        if refresh:
            target_ref = observed.get("target_ref") if isinstance(observed, dict) else None
            sources, base = bind_governance(observed, target_ref, governance["exclude"])
            governance = {**governance, "base_ref": target_ref,
                          "selection": observed.get("selection") if target_ref else None, "base": base}
        else:
            sources = prior_map["governance_sources"]
    gov = governance_digest(sources)
    rebind = value.get("rebind", [])
    if not isinstance(rebind, list) or len(rebind) > MAX_OBLIGATIONS:
        raise refuse("obligation_invalid", "malformed", "rebind is a bounded list of obligation ids")
    if rebind and not refresh:
        raise refuse("obligation_invalid", "malformed", "evidence is rebound only by a governance refresh")

    # Proposals persist; only adoption changes their status.
    prior_proposals = {row["id"]: row for row in (prior_map or {}).get("proposals", [])}
    raw_proposals = _first(value, "proposals", list(prior_proposals.values()))
    if not isinstance(raw_proposals, list) or len(raw_proposals) > MAX_PROPOSALS:
        raise refuse("obligation_invalid", "malformed", "proposals are a bounded list")
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

    # Obligations: structure, stable identity, authority.
    raw_obligations = value["obligations"]
    if not isinstance(raw_obligations, list) or len(raw_obligations) > MAX_OBLIGATIONS:
        raise refuse("obligation_invalid", "malformed", "obligations are a bounded list")
    prior_rows = {row["id"]: row for row in (prior_map or {}).get("obligations", [])}
    rows: dict[str, dict] = {}
    for raw in raw_obligations:
        ob = _structure(raw)
        if ob["id"] in rows or ob["id"] in proposals:
            raise refuse("obligation_invalid", "malformed", "obligation ids are unique", obligation=ob["id"])
        rows[ob["id"]] = ob
    for key in prior_rows:
        if key not in rows:
            raise refuse("obligation_unaccounted", "missing_state", "an obligation cannot be dropped; withdraw it",
                         obligation=key)
    criteria = ctx.get("criteria", [])
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
        ob["definition"] = definition_id(ob)
        if earlier is not None:
            for key in ("kind", "provenance", "parent"):
                if ob.get(key) != earlier.get(key):
                    raise refuse("obligation_invalid", "redefinition_unauthorized",
                                 f"an obligation's {key} is immutable", obligation=ob["id"])
            if not user and any(ob.get(key) != earlier.get(key) for key in _DEFINITION if key != "scope") \
                    or not user and ob["kind"] == "assurance" and ob["scope"] != earlier.get("scope"):
                raise refuse("obligation_invalid", "redefinition_unauthorized",
                             "only a direct user revision redefines an obligation's check", obligation=ob["id"])
            ob["introduced_seq"] = earlier["introduced_seq"]
            source = earlier.get("source")
            if refresh and ob["provenance"] == "project_policy" and isinstance(source, dict):
                source = _relocate(source, observed, sources)
            if source is not None:
                ob["source"] = source
            else:
                ob.pop("source", None)
            if earlier.get("adopts") != ob.get("adopts"):
                raise refuse("obligation_invalid", "redefinition_unauthorized", "adoption is recorded once",
                             obligation=ob["id"])
            ob.pop("finding", None)
            if earlier.get("finding") is not None:
                ob["finding"] = earlier["finding"]
            if ob["id"] not in triaged:
                ob.pop("findings", None)
                if earlier.get("findings") is not None:
                    ob["findings"] = earlier["findings"]
            continue
        if ob["id"] not in triaged and ("finding" in ob or "findings" in ob):
            raise refuse("obligation_invalid", "malformed", "review findings are recorded only by triage",
                         obligation=ob["id"])
        if ob.get("introduced_seq") not in (None, seq):
            raise refuse("obligation_invalid", "malformed", "a new obligation is introduced at this write",
                         obligation=ob["id"], seq=seq)
        ob["introduced_seq"] = seq
        _introduce(ob, prior_map is None, user, observed, sources, criteria)
    for ob in rows.values():
        if "parent" in ob:
            parent = rows.get(ob["parent"])
            if parent is None:
                raise refuse("obligation_invalid", "no_parent", "the parent is not in the map", obligation=ob["id"])
            if ob["kind"] == "correction" and parent["kind"] != "assurance":
                raise refuse("obligation_invalid", "no_parent", "a correction is parented to an assurance obligation",
                             obligation=ob["id"])
        if "adopts" in ob and ob["id"] not in prior_rows:
            proposal = proposals.get(ob["adopts"])
            if ob["provenance"] not in ("user_direct", "project_policy"):
                raise refuse("obligation_invalid", "provenance_unauthorized",
                             "only a direct user revision or a governance citation adopts a proposal",
                             obligation=ob["id"])
            if proposal is None or proposal["status"] != "open":
                raise refuse("obligation_invalid", "malformed", "the adopted proposal is not open",
                             obligation=ob["id"], proposal=ob["adopts"])
            proposal["status"] = "adopted"
    _ancestry(rows)
    for ob in rows.values():
        if ob["kind"] == "assurance" and ob["id"] not in prior_rows and ob["state"] != "withdrawn":
            for other in rows.values():
                recorded = {**other, "receipts": prior_rows.get(other["id"], {}).get("receipts", [])}
                if (other is not ob and other["kind"] == "assurance"
                        and (other["state"] != "withdrawn"
                             or _accepted_assurance_current(recorded, ctx, gov))
                        and (other["candidate"] == ob["candidate"]
                             or _accepted_assurance_current(recorded, ctx, gov))
                        and overlaps(other["scope"], ob["scope"])
                        and not ob.get("uncovered_risk")):
                    raise refuse("obligation_invalid", "missing_uncovered_risk",
                                 "an overlapping assurance on the same candidate names the risk it adds",
                                 obligation=ob["id"], overlaps=other["id"])

    # Coverage: every original criterion has an objective obligation (R39).
    covered = _covered(rows)
    for criterion in criteria:
        if criterion not in covered:
            raise refuse("obligation_unaccounted", "criterion_uncovered",
                         "every original criterion needs an objective obligation", criterion=criterion)

    # Withdrawals and evidence stamping.
    for ob in rows.values():
        earlier = prior_rows.get(ob["id"])
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
            raise refuse("obligation_invalid", "malformed", "one report consumes one review attempt")
        ob["evidence"] = _stamp_evidence(ob, earlier, ctx, gov, ob["id"] in rebind,
                                          reported_attempt=reported[0] if reported else None, seq=seq)
        reuse = _bind_reuse(ob, earlier, ctx)
        if reuse is not None:
            ob["reuse"] = reuse
        if not ob["evidence"]:
            ob.pop("evidence")
    missing_rebind = [key for key in rebind if key not in rows]
    if missing_rebind:
        raise refuse("obligation_invalid", "malformed", "rebind names unknown obligations",
                     obligation=missing_rebind[0])

    state = {"seq": seq, "revision": revision, "governance": governance, "governance_sources": sources,
             "obligations": [rows[key] for key in [row["id"] for row in raw_obligations]],
             "proposals": list(proposals.values())}
    if prior_map is not None and "governance_history" in prior_map:
        state["governance_history"] = deepcopy(prior_map["governance_history"])
    for ob in rows.values():
        if ob["state"] == "satisfied":
            for item in ob["receipts"]:
                if item["evidence"] in ob.get("evidence", []):
                    item.setdefault("accepted_seq", seq)
                    item["reuse"] = deepcopy(ob.get("reuse"))
    for ob in rows.values():
        if ob["kind"] != "assurance":
            continue
        for finding in ob.get("findings", []):
            if finding["triage"] != "required_correction":
                continue
            child = rows.get(finding.get("correction"))
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
    _account(state, rows, seq, ctx, gov)
    _waits(state, rows, ctx)
    violations = properties(state, ctx)
    for name, found in violations.items():
        if found:
            raise refuse("obligation_unaccounted", "property_" + name, found[0])
    slot = [ob["id"] for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    state["coordinator_slot"] = slot[0] if slot else None
    state["quiescence"] = quiescence(state)
    state["closure"] = None
    state["reopened"] = list((prior_map or {}).get("reopened", []))
    if closed:
        state["reopened"] = (state["reopened"] + [{"seq": seq, "revision": revision,
                                                   "instruction": authority["instruction"],
                                                   "closed_revision": prior_map["closure"]["revision"]}])[-8:]
    state["observations"] = observations(prior_map, state, ctx)
    if value.get("close") is True:
        state["closure"] = _close(state, ctx)
    elif "close" in value:
        raise refuse("obligation_invalid", "malformed", "close is true when present")
    return state


def _introduce(ob: dict, intake: bool, user: bool, observed: dict | None, sources: list[dict],
               criteria: list[str]) -> None:
    provenance, kind = ob["provenance"], ob["kind"]
    if provenance == "objective":
        source = _exact(ob.get("source"), {"ref"}, {"ref"}, "objective source")
        _text(source["ref"], "source ref", limit=256)
        if kind not in ("criterion", "delivery") or not intake:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "objective provenance enters criteria and delivery obligations at intake",
                         obligation=ob["id"])
    elif provenance == "user_direct":
        source = _exact(ob.get("source"), {"instruction", "ref"}, {"instruction"}, "user source")
        _text(source["instruction"], "instruction", limit=1024)
        if "ref" in source:
            _text(source["ref"], "source ref", limit=256)
        if not user:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "a user_direct obligation enters only by a direct user revision", obligation=ob["id"])
    elif provenance == "project_policy":
        if not isinstance(ob.get("source"), dict):
            raise refuse("obligation_invalid", "cite_not_found", "project policy cites a governance source line range",
                         obligation=ob["id"])
        ob["source"] = _cite(ob["source"], observed, sources)
    else:
        if kind not in ("subgoal", "assurance", "correction"):
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "the coordinator introduces only subgoals, assurance and corrections", obligation=ob["id"])
        if "source" in ob:
            raise refuse("obligation_invalid", "provenance_unauthorized",
                         "coordinator obligations derive from their parent, not a source", obligation=ob["id"])


def _ancestry(rows: dict[str, dict]) -> None:
    for ob in rows.values():
        seen = {ob["id"]}
        cursor = ob
        while "parent" in cursor:
            cursor = rows[cursor["parent"]]
            if cursor["id"] in seen:
                raise refuse("obligation_invalid", "no_parent", "parent chains are acyclic", obligation=ob["id"])
            seen.add(cursor["id"])


def _withdrawal(ob: dict, user: bool, authority: dict | None) -> dict:
    record = _exact(ob["withdrawal"], {"by", "reason", "instruction"}, {"by", "reason"}, "withdrawal")
    _text(record["reason"], "reason", limit=1024)
    by = record["by"]
    allowed = (by == "user_direct" and user
               or by == "project_policy" and ob["provenance"] == "project_policy"
               and bool(ob.get("source", {}).get("gone"))
               or by == "coordinator" and ob["provenance"] == "coordinator" and ob["kind"] in ("subgoal", "assurance"))
    if not allowed:
        raise refuse("obligation_invalid", "withdrawal_unauthorized",
                     f"{by} cannot withdraw this {ob['provenance']} {ob['kind']}", obligation=ob["id"])
    result = {"by": by, "reason": record["reason"]}
    if by == "user_direct":
        result["instruction"] = authority["instruction"]
    return result


def _account(state: dict, rows: dict[str, dict], seq: int, ctx: dict, gov: str) -> None:
    """R83: exactly one currently valid state for every obligation."""
    outstanding = set(_outstanding(ctx))
    admissions = _admissions(ctx)
    coordinator = [ob for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    if len(coordinator) > 1:
        raise refuse("obligation_unaccounted", "coordinator_slot_exceeded",
                     "at most one obligation is coordinator-held active",
                     obligations=",".join(ob["id"] for ob in coordinator))
    pending = undispositioned(ctx)
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
                                 "the executing admission has settled; restate the obligation",
                                 obligation=ob["id"], admission=executor)
            else:
                for key in outstanding:
                    found = overlap(ob["boundary"], _admission_boundary(admissions.get(key, {})))
                    if found["paths"] or found["surfaces"]:
                        raise refuse("obligation_unaccounted", "coordinator_boundary_overlap",
                                     "the coordinator-held boundary overlaps an outstanding admission",
                                     obligation=ob["id"], admission=key)
        if state == "blocked_external":
            external = _exact(ob["external"], {"party", "need", "unblocks_when"},
                              {"party", "need", "unblocks_when"}, "external", code="obligation_unaccounted")
            if external["party"] not in PARTIES:
                raise refuse("obligation_unaccounted", "external_invalid",
                             "an external block names owner, user, provider or third_party", obligation=ob["id"])
            for field in ("need", "unblocks_when"):
                _text(external[field], field, code="obligation_unaccounted", detail="external_invalid")
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
                             "satisfied needs passing bound evidence", obligation=ob["id"])
            if not evidence_valid(ob, ctx, gov):
                raise refuse("obligation_unaccounted", "evidence_invalidated",
                             "its evidence is not passing and valid for the current candidate and governance",
                             obligation=ob["id"])
            if ob["kind"] == "assurance":
                for child in rows.values():
                    if (child.get("parent") == ob["id"] and child["kind"] == "correction"
                            and not (child["state"] == "satisfied"
                                     or child["state"] == "withdrawn" and child["withdrawal"]["by"] == "user_direct")):
                        raise refuse("obligation_unaccounted", "evidence_invalidated",
                                     "an assurance is satisfied only when its corrections are",
                                     obligation=ob["id"], correction=child["id"])
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


def _wait_record(ob: dict) -> dict:
    wait = ob["wait"]
    if not isinstance(wait, dict) or wait.get("class") not in WAIT_CLASSES:
        raise refuse("wait_invalid", "unclassified", "a wait has one controlling class", obligation=ob["id"])
    record = _exact(wait, {"class", "referent", "rationale", "revisit_when", "inputs"}, {"class", "referent"},
                    "wait", code="wait_invalid")
    for key in ("rationale", "revisit_when"):
        if key in record:
            _text(record[key], key, code="wait_invalid", detail="malformed")
    if ("rationale" in record or "revisit_when" in record or "inputs" in record) and record["class"] != "sequenced":
        raise refuse("wait_invalid", "malformed", "only a sequenced wait carries a serialization rationale",
                     obligation=ob["id"])
    return record


def _waits(state: dict, rows: dict[str, dict], ctx: dict) -> None:
    """R84: one controlling reason, valid for its class and current referent."""
    outstanding = set(_outstanding(ctx))
    admissions = _admissions(ctx)
    held = [ob for ob in rows.values() if ob["state"] == "active" and ob["executor"] == "coordinator"]
    pending = undispositioned(ctx)
    edges: dict[str, str] = {}
    for ob in rows.values():
        if ob["state"] != "waiting":
            continue
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
            edges[name] = referent
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
            if (not isinstance(referent, list) or len(referent) > 8
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
                            code="wait_invalid")
            if record["kind"] == "authorization":
                if record.get("scope") not in AUTHORIZED_SCOPES or not isinstance(record.get("candidate"), str):
                    raise refuse("wait_invalid", "unknown_referent",
                                 "a missing authorization names its scope and candidate", obligation=name)
                granted = ctx.get("authorization_granted")
                if granted is not None and granted(record["scope"], record["candidate"]):
                    raise refuse("wait_invalid", "resolved_referent", "the authorization is recorded",
                                 obligation=name)
            elif record["kind"] in ("native_authority", "permission"):
                _text(record.get("need"), "need", code="wait_invalid", detail="unknown_referent")
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
    for start in edges:
        seen = [start]
        cursor = start
        while cursor in edges:
            cursor = edges[cursor]
            if cursor in seen:
                raise refuse("wait_invalid", "cycle", "dependency and contract waits are acyclic",
                             obligations=",".join(seen))
            seen.append(cursor)


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
    found = []
    for ob in rows.values():
        root = ob
        while root["provenance"] == "coordinator" and "parent" in root:
            root = rows[root["parent"]]
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
    gov = governance_digest(state["governance_sources"])
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


def _close(state: dict, ctx: dict) -> dict:
    open_rows = [ob["id"] for ob in state["obligations"] if ob["state"] not in TERMINAL]
    if open_rows:
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "an objective closes only when every obligation is satisfied or withdrawn",
                     obligation=open_rows[0])
    if _outstanding(ctx) or undispositioned(ctx):
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "closure waits for settled admissions and dispositions")
    if ctx.get("governor_pending"):
        raise refuse("obligation_unaccounted", "closure_unfinished",
                     "closure waits for pending governed effects to settle")
    closure = {"seq": state["seq"], "revision": state["revision"]}
    return {**closure, "report": report_projection({**state, "closure": closure}, ctx)}


def brief_map(criteria: list[str], value: Any, ctx: dict) -> dict:
    """R39/A43 at the execution brief: the draft intake map, validated without persisting it.

    An execution brief always carries its map. The draft runs through the same intake write
    the checkpoint boundary accepts, against read-only governance facts, so a Plan Mode
    brief is checked exactly as it would be bound and nothing is written.
    """
    if not isinstance(value, dict) or not isinstance(value.get("obligations"), list):
        raise refuse("obligation_unaccounted", "map_missing",
                     "an execution brief records its obligation map with every original criterion")
    draft = accept_write(None, value, {**ctx, "criteria": list(criteria)})
    return {"draft": True, "persisted": False, "criteria_covered": list(criteria), **draft}


def _covered(rows: dict[str, dict]) -> set[str]:
    return {ob["source"].get("ref") for ob in rows.values()
            if ob["provenance"] in ("objective", "user_direct") and isinstance(ob.get("source"), dict)}


# --------------------------------------------------------------------------- admission

def packet_binding(body: dict) -> dict:
    """Validate the pod-packet/v3 obligation binding fields."""
    serves = body.get("serves")
    if (not isinstance(serves, list) or not serves or len(serves) > MAX_SERVES
            or len(set(serves)) != len(serves)):
        raise refuse("unbound_assignment", "missing", "a packet serves one or more obligations")
    for item in serves:
        _ident(item, "served obligation", code="unbound_assignment")
    if body.get("role") not in ROLES:
        raise refuse("unbound_assignment", "role_kind_mismatch", "role is implement, investigate or review")
    if type(body.get("map_revision")) is not int or body["map_revision"] < 1:
        raise refuse("unbound_assignment", "out_of_revision", "a packet binds the map revision it serves")
    declared = boundary(body.get("boundary"), code="unbound_assignment")
    for key in ("resolves", "stop_condition", "delta_from"):
        if key in body:
            _text(body[key], key, code="unbound_assignment")
    return {"serves": list(serves), "role": body["role"], "boundary": declared,
            "map_revision": body["map_revision"]}


def admission_refusal(state: dict | None, body: dict, ctx: dict, *, admission_id: str) -> dict:
    """R85/R86/R89: refuse an assignment that serves nothing recorded, overlaps or is closed."""
    if not isinstance(state, dict) or state.get("obligations") is None:
        raise refuse("unbound_assignment", "no_map", "delegation needs an obligation map first")
    if state.get("closure"):
        raise refuse("objective_closed", "objective_closed", "the objective is closed",
                     closure_revision=state["closure"]["revision"])
    binding = packet_binding(body)
    if binding["map_revision"] != state["revision"]:
        raise refuse("unbound_assignment", "out_of_revision", "the packet names another map revision",
                     packet=binding["map_revision"], current=state["revision"])
    rows = {ob["id"]: ob for ob in state["obligations"]}
    proposals = {row["id"] for row in state.get("proposals", [])}
    for served in binding["serves"]:
        if served in proposals:
            raise refuse("unbound_assignment", "proposed", "a proposal is not an obligation", obligation=served)
        ob = rows.get(served)
        if ob is None:
            raise refuse("unbound_assignment", "missing", "the served obligation is not in the map", obligation=served)
        if ob["state"] in TERMINAL:
            raise refuse("unbound_assignment", ob["state"], f"the served obligation is {ob['state']}",
                         obligation=served)
        if _accepted_assurance_current(ob, ctx, governance_digest(state["governance_sources"])):
            raise refuse("unbound_assignment", "assurance_still_bound",
                         "an accepted assurance receipt is still valid", obligation=served)
        busy = _serving(ctx, served, outstanding_only=True)
        if busy:
            raise refuse("unbound_assignment", "obligation_busy", "the obligation already has an outstanding admission",
                         obligation=served, admission=busy[0])
    kinds = {rows[served]["kind"] for served in binding["serves"]}
    if binding["role"] == "review" and (kinds != {"assurance"} or len(binding["serves"]) != 1):
        raise refuse("unbound_assignment", "role_kind_mismatch", "a review attempt serves exactly one assurance obligation")
    if binding["role"] != "review" and "assurance" in kinds:
        raise refuse("unbound_assignment", "role_kind_mismatch", "only a review attempt serves an assurance obligation")
    if binding["role"] == "investigate" and not (body.get("resolves") and body.get("stop_condition")):
        raise refuse("unbound_assignment", "missing_resolves", "an investigation names resolves and stop_condition")
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
            "governance": governance_digest(state["governance_sources"]),
            "definitions": {ob["id"]: definition_id(ob) for ob in state["obligations"]
                            if ob["id"] in body["serves"]}}


def admit(state: dict, body: dict, ctx: dict, *, admission_id: str, accompanying: dict | None = None) -> dict:
    """The map write that accompanies an admission row: served obligations become active."""
    binding = admission_refusal(state, body, ctx, admission_id=admission_id)
    base = dict(accompanying) if accompanying is not None else {}
    if set(base) - {"seq", "obligations", "proposals"}:
        raise refuse("obligation_invalid", "malformed", "an admission carries only obligations and proposals")
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
    admissions = {**_admissions(ctx), admission_id: {"serves": binding["serves"], "role": binding["role"],
                                                      "boundary": binding["boundary"], "state": "reserved",
                                                      "disposition": None, "changed_paths": None,
                                                      "candidate": ctx.get("candidate"), "report": None,
                                                      "binding": admission_binding(body, ctx, state)}}
    outstanding = list(dict.fromkeys([*_outstanding(ctx), admission_id]))
    return accept_write(state, value, {**ctx, "admissions": admissions, "outstanding": outstanding})


# --------------------------------------------------------------------------- results and review

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
            _text(record[field], field, code="obligation_unaccounted", detail="disposition_invalid", limit=1024)
    if "discarded" in record:
        if record["discarded"] is not True or "reason" not in record:
            raise refuse("obligation_unaccounted", "disposition_invalid", "discarding a result needs a reason",
                         admission=key)
        return {"kind": "discarded", "reason": record["reason"], "seq": seq}
    candidate = _text(record["integrated_into"], "integrated_into", code="obligation_unaccounted",
                      detail="disposition_invalid", limit=128)
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
                     "a result that exceeded its boundary needs a reason to integrate", admission=key)
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


def triage(state: dict, value: dict, findings: Any, proposals: Any, ctx: dict, *,
           admission_id: str) -> tuple[dict, frozenset]:
    """R82/R87: review findings become corrections or proposals; reports add only proposals.

    Returns the map value, finding-owned obligation ids, and the consumed review
    marker. Only this report boundary can supply those records to `accept_write`.
    """
    base = {key: value[key] for key in value if key in ("seq", "obligations", "proposals")}
    obligations = [dict(row) for row in base.get("obligations", state["obligations"])]
    listed = [dict(row) for row in base.get("proposals", state.get("proposals", []))]
    admission = _admissions(ctx).get(admission_id) or {}
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise refuse("obligation_invalid", "malformed", "triage is a bounded list")
    if findings and admission.get("role") != "review":
        raise refuse("obligation_invalid", "malformed", "only a review attempt is triaged", admission=admission_id)
    rows = {row.get("id"): row for row in obligations}
    assurance_id = (admission.get("serves") or [None])[0]
    prior_ids = {ob["id"] for ob in state["obligations"]}
    stored = next((ob.get("findings", []) for ob in state["obligations"] if ob["id"] == assurance_id), [])
    written: set[str | tuple[str, str]] = set()
    records = []
    for raw in findings:
        record = _exact(raw, {"finding", "severity", "triage", "summary", "reason", "correction"},
                        {"finding", "severity", "triage", "summary"}, "finding")
        _ident(record["finding"], "finding")
        _text(record["summary"], "summary")
        if record["severity"] not in SEVERITIES or record["triage"] not in TRIAGE:
            raise refuse("obligation_invalid", "malformed", "severity or triage is unsupported",
                         finding=record["finding"])
        if record["severity"] in ("blocker", "major") and record["triage"] == "advisory":
            if "reason" not in record:
                raise refuse("obligation_invalid", "downgrade_unreasoned",
                             "downgrading a reviewer's blocker or major finding needs a reason",
                             finding=record["finding"])
            _text(record["reason"], "reason", limit=1024)
        entry = {"attempt": admission_id, "finding": record["finding"], "severity": record["severity"],
                 "triage": record["triage"], "summary": record["summary"],
                 "recorded_seq": state["seq"] + 1}
        if any(item.get("attempt") == admission_id and item.get("finding") == record["finding"]
               for item in stored):
            continue
        if "reason" in record:
            entry["reason"] = record["reason"]
        known = {row.get("id") for row in listed}
        if record["triage"] == "required_correction":
            correction = rows.get(record.get("correction"))
            if (correction is None or record["correction"] in prior_ids or correction.get("kind") != "correction"
                    or correction.get("parent") != assurance_id or correction.get("provenance") != "coordinator"):
                raise refuse("obligation_invalid", "no_parent",
                             "a required correction is a new coordinator correction under the assurance obligation",
                             finding=record["finding"])
            correction["finding"] = {"attempt": admission_id, "finding": record["finding"],
                                     "severity": record["severity"]}
            written.add(correction["id"])
            entry["correction"] = record["correction"]
        else:
            if "correction" in record:
                raise refuse("obligation_invalid", "malformed", "an advisory finding becomes a proposal",
                             finding=record["finding"])
            key = "adv-" + digest({"attempt": admission_id, "finding": record["finding"]})[:12]
            if key not in known:
                listed.append({"id": key, "source": "reviewer_advisory", "origin_ref": admission_id[:64],
                               "summary": record["summary"], "status": "open"})
            entry["proposal"] = key
        records.append(entry)
    if records:
        assurance = rows.get(assurance_id)
        if assurance is None:
            raise refuse("obligation_invalid", "malformed", "the reviewed assurance obligation is absent")
        if len(stored) + len(records) > MAX_FINDINGS:
            raise refuse("obligation_invalid", "finding_limit",
                         "the bounded finding history is full", obligation=assurance_id)
        assurance["findings"] = list(stored) + records
        written.add(assurance_id)
    if admission.get("role") == "review":
        written.add(("review_report", admission_id))
    if not isinstance(proposals, list) or len(proposals) > 16:
        raise refuse("obligation_invalid", "malformed", "report proposals are a bounded list")
    for raw in proposals:
        record = _exact(raw, {"summary", "source"}, {"summary"}, "report proposal")
        _text(record["summary"], "summary")
        source = record.get("source", "worker_report")
        if source not in PROPOSAL_SOURCES:
            raise refuse("obligation_invalid", "malformed", "proposal source is unsupported")
        key = "rep-" + digest({"attempt": admission_id, "summary": record["summary"]})[:12]
        if key not in {row.get("id") for row in listed}:
            listed.append({"id": key, "source": source, "origin_ref": admission_id[:64],
                           "summary": record["summary"], "status": "open"})
    out = {**{k: v for k, v in value.items() if k not in ("obligations", "proposals")},
           "obligations": obligations, "proposals": listed}
    return out, frozenset(written)


# --------------------------------------------------------------------------- reporting

def label_qualification(state: dict | None, ctx: dict, candidate: str | None) -> dict:
    """R88: the `independently reviewed` label is derived, never asserted."""
    if not isinstance(state, dict) or state.get("obligations") is None:
        return {"label": "WITHHELD", "assurance_unbound": [{"obligation": None, "gap": "none_recorded"}],
                "withdrawn": [], "required": False}
    gov = governance_digest(state["governance_sources"])
    rows = [ob for ob in state["obligations"] if ob["kind"] == "assurance"]
    live = [ob for ob in rows if ob["state"] != "withdrawn"]
    gaps = []
    if not live:
        gaps.append({"obligation": None, "gap": "none_recorded"})
    for ob in live:
        if ob["state"] != "satisfied":
            gaps.append({"obligation": ob["id"], "gap": "unbound"})
        elif candidate is None or candidate != ctx.get("candidate") or not evidence_valid(ob, ctx, gov, candidate):
            gaps.append({"obligation": ob["id"], "gap": "invalidated"})
    withdrawn = [{"obligation": ob["id"], "provenance": ob["provenance"], **ob["withdrawal"]}
                 for ob in rows if ob["state"] == "withdrawn"]
    # Recorded, non-withdrawn assurance obligations are themselves a review requirement; a
    # caller cannot declare them not required.
    return {"label": "WITHHELD" if gaps else "QUALIFIED", "assurance_unbound": gaps, "withdrawn": withdrawn,
            "required": bool(live)}


def report_projection(state: dict, ctx: dict) -> dict:
    """R43 report content derived from the map: nothing here is asserted by a caller."""
    rows = state["obligations"]
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
              "withdrawn": [{"obligation": ob["id"], "provenance": ob["provenance"], **ob["withdrawal"]}
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
    if state.get("governance_history", {}).get("decisions"):
        result["governance_decisions"] = deepcopy(state["governance_history"]["decisions"])
    if status == "quiescent":
        result["interim_report"] = state["quiescence"]["interim_report"]
    elif status == "open" and result["unfinished"]:
        result["interim_report"] = "incomplete — " + str(len(result["unfinished"])) + " obligation(s) unfinished"
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
                                    and ctx["governance_current"] != state["governance"]["base"]),
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


def status_projection(state: dict | None, ctx: dict) -> dict:
    """Human and JSON status for the map: every state with its referent."""
    if not isinstance(state, dict) or state.get("obligations") is None:
        return {"map": None, "lines": ["Map        none (kernel inert)"]}
    rows = state["obligations"]
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
        elif ob["state"] == "withdrawn":
            detail = f"by {ob['withdrawal']['by']}: {ob['withdrawal']['reason']}"
        else:
            detail = ob.get("check") or ob.get("resolves") or ""
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
    return {"map": {"seq": state["seq"], "revision": state["revision"], "counts": counts,
                    "coordinator_slot": state.get("coordinator_slot"),
                    "quiescence": state.get("quiescence"), "closure": state.get("closure"),
                    "observations": state.get("observations"), "label": label,
                    "obligations": [{"id": ob["id"], "state": ob["state"],
                                     "referent": ob.get("executor") or ob.get("wait") or ob.get("external")
                                     or ob.get("withdrawal")} for ob in rows]},
            "lines": lines}


# --------------------------------------------------------------------------- recorded traces

TRACE_SCHEMA = "pod-trace/v1"


def evaluate_trace(trace: Any) -> dict:
    """Scripted record checks over a sanitized recorded trace (R91, A162).

    Slot filling, amplification and scope inflation are findings the boundary refuses, so
    any occurrence fails the trace. Serialization and churn are flags. Wall time and
    admission counts are observations only. A trace without obligation maps, such as a
    0.5 native-observation capture, is reported as not evaluable rather than as a pass.
    """
    if isinstance(trace, dict) and trace.get("schema") != TRACE_SCHEMA and isinstance(trace.get("observations"), list):
        rows = [row for row in trace["observations"] if isinstance(row, dict)]
        stamps = sorted(str(row.get("completed_at")) for row in rows if row.get("completed_at"))
        return {"schema": TRACE_SCHEMA, "label": "recorded_native_observations", "evaluable": False,
                "reason": "the trace has no obligation maps or boundary decisions",
                "checks": {name: "NOT_EVALUABLE" for name in
                           ("slot_filling", "serialization", "amplification", "churn", "scope_inflation")},
                "observations": {"native_dispatches": trace.get("native_dispatch_count", "unknown"),
                                 "starts": sum(row.get("start_observed") is True for row in rows),
                                 "settlements": sum(row.get("settlement_observed") is True for row in rows),
                                 "first_completion": stamps[0] if stamps else "unknown",
                                 "last_completion": stamps[-1] if stamps else "unknown",
                                 "admissions": "unknown", "wall_time": "unknown"},
                "failed": False}
    record = _exact(trace, {"schema", "label", "records", "observations"}, {"schema", "label", "records"},
                    "trace", code="invalid_trace")
    if record["label"] not in ("synthetic", "recorded") or not isinstance(record["records"], list):
        raise PodError("invalid_trace", "Trace label is synthetic or recorded with a record list")
    findings = {"slot_filling": [], "serialization": [], "amplification": [], "churn": [], "scope_inflation": []}
    previous = None
    settled: dict[str, int] = {}
    satisfied: set[str] = set()
    admissions = 0
    for index, row in enumerate(record["records"]):
        if not isinstance(row, dict) or row.get("kind") not in ("write", "admission", "settlement"):
            raise PodError("invalid_trace", f"Trace record {index} is malformed")
        if row["kind"] == "write":
            current = row["map"]
            rows = {ob["id"]: ob for ob in current["obligations"]}
            earlier = {ob["id"] for ob in (previous or {}).get("obligations", [])}
            proposals = {item["id"] for item in (previous or {}).get("proposals", [])}
            for ob in rows.values():
                if ob["id"] in earlier:
                    continue
                allowed = (ob["provenance"] == "objective" and previous is None
                           and ob["kind"] in ("criterion", "delivery")
                           or ob["provenance"] == "user_direct" and row.get("user_revision") is True
                           or ob["provenance"] == "project_policy" and isinstance(ob.get("source", {}).get("base"), str)
                           or ob["provenance"] == "coordinator" and ob["kind"] in ("subgoal", "assurance", "correction")
                           and ob.get("parent") in rows)
                if not allowed or ob.get("adopts") in proposals and ob["provenance"] not in ("user_direct",
                                                                                               "project_policy"):
                    findings["scope_inflation"].append({"record": index, "obligation": ob["id"]})
            findings["serialization"] += [{"record": index, **flag}
                                          for flag in serialization_flags(previous, current)]
            satisfied = {ob["id"] for ob in rows.values() if ob["state"] == "satisfied"}
            previous = current
        elif row["kind"] == "admission":
            admissions += 1
            rows = {ob["id"]: ob for ob in (previous or {}).get("obligations", [])}
            serves = row.get("serves") or []
            if row.get("accepted") is True:
                if not serves or any(rows.get(item) is None or rows[item]["state"] in TERMINAL for item in serves):
                    findings["slot_filling"].append({"record": index, "serves": serves})
                if row.get("role") == "review" and any(item in satisfied for item in serves):
                    findings["amplification"].append({"record": index, "serves": serves})
        else:
            for item in row.get("serves") or []:
                settled[item] = settled.get(item, 0) + 1
    findings["churn"] = [{"obligation": key, "settled": count} for key, count in sorted(settled.items())
                         if count >= CHURN_THRESHOLD and key not in satisfied]
    failed = bool(findings["slot_filling"] or findings["amplification"] or findings["scope_inflation"])
    return {"schema": TRACE_SCHEMA, "label": record["label"], "evaluable": True,
            "checks": {name: ("FLAGGED" if name in ("serialization", "churn") and rows else
                              "FAILED" if rows else "PASS") for name, rows in findings.items()},
            "findings": findings,
            "observations": {**(record.get("observations") or {}), "admissions": admissions},
            "failed": failed}

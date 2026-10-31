"""Code-owned input vocabulary and bounds, exposed as data by internal contract.

This is a table of the existing validators' contracts, not a second validator or
a generated schema. Validators select their bounds here and retain their own
ordering, codes and semantic checks. Discovery never includes request values.
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import re

from .errors import FieldRefusal, PodError
from .installer import STATE_SCHEMAS

IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z"
UNIT = re.compile(IDENTIFIER)
SURFACE = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z"
SOURCE = r"[!-~]{1,128}\Z"
LINES = r"([1-9][0-9]{0,6})(?:-([1-9][0-9]{0,6}))?\Z"
GOVERNANCE_PATHS = (".pod/config.yaml", "AGENTS.md", "CLAUDE.md")
KINDS = ("criterion", "delivery", "assurance", "correction", "steer", "subgoal")
PROVENANCES = ("objective", "user_direct", "project_policy", "coordinator")
STATES = ("unassigned", "active", "waiting", "blocked_external", "satisfied", "withdrawn", "renewed")
WAIT_CLASSES = ("dependency", "contract_unsettled", "sequenced", "ownership", "capacity",
                "integration_pending", "input_unavailable", "authority", "user_hold")
PARTIES = ("owner", "user", "provider", "third_party")
ROLES = ("implement", "investigate", "review")
VERDICTS = ("accepted", "rejected")
EVIDENCE_STATUSES = ("PASS", "FAILED", "NOT_RUN", "UNAVAILABLE")
PROPOSAL_SOURCES = ("worker_report", "reviewer_advisory", "issue", "repository",
                    "fetched_documentation", "coordinator_caution")
EFFECT_PREFIXES = ("workflow:", "deploy:", "release:")
ACTION_KINDS = ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic",
                "merge", "release", "deploy", "cancel_validation")
PURPOSES = ("validation", "diagnostic", "checkpoint", "release", "cancellation")
AUTHORIZATION_SCOPES = ("publish", "merge", "release", "deploy")
TOOL_QUESTION_SCHEMA = "pod-tool-question/v1"
PR_NUMBER = {"type": "integer", "min": 1, "max": 2147483647}
SEVERITIES = ("blocker", "major", "minor", "info")
TRIAGE = ("required_correction", "advisory")


def text(length=512):
    return {"type": "text", "max_length": length, "nonempty": True, "nul": False}


def choice(values):
    return {"type": "text", "allowed": list(values)}


def items(count, item=None):
    return {"type": "list", "max_items": count, **({"items": item} if item else {})}


def record(name):
    return {"type": "object", "record": name}


def ident(pattern=IDENTIFIER):
    maximum = 128 if pattern in (SURFACE, SOURCE) else 64
    return {"type": "text", "pattern": pattern, "max_length": maximum}


def definition(fields, required=(), **bounds):
    return {"fields": fields, "required": set(required), **bounds}


_OWNER_INSTRUCTION = text(1024)
_ANY = {"type": "any"}  # Existing opaque fields acquire no new validation.
_PATH = {"type": "text", "max_length": 512, "nonempty": True,
         "form": "safe repository-relative path, excluding credential paths"}
_GIT = {"type": "text", "form": "resolvable Git commit object id (40 or 64 lower-case hex)"}
_DIGEST = {"type": "text", "pattern": r"[0-9a-f]{64}\Z", "max_length": 64}
_MAP_FIELDS = {
    "seq": {"type": "integer", "minimum": 1}, "obligations": {**items(512, record("obligation")), "max_live": 128},
    "proposals": items(64, record("proposal")), "governance": record("governance"),
    "governance_refresh": {"type": "boolean"}, "revision_authority": record("revision authority"),
    "reopen": {"type": "boolean"}, "close": {"type": "boolean"}, "rebind": {"type": "list"},
    "dispositions": items(16, record("disposition")), "delivery": {"type": "object"},
    "route_settlements": {**items(16, record("route settlement")), "only": "checkpoint or report map"},
    "update": {"type": "object", "max_items": 128, "form": "obligation ids mapped to field updates; state changes clear conflicting state fields"},
    "renew": {**items(16, record("renewal request")), "only": "checkpoint"},
    "reuse": {"type": "object"}, "acceptances": items(16, record("acceptance request")),
    "mapping_repair": record("mapping repair"),
}

TABLE = {
    "route settlement": definition({
        "admission": _DIGEST, "basis": choice(("redone", "lead_decision")),
        "replacement": _DIGEST, "decision": choice(("accept", "discard")),
        "source": record("route settlement source"), "reason": text(1024),
        "corrections": items(512, ident()),
    }, ("admission", "basis"),
       form="redone requires only replacement; lead_decision requires decision, source and reason; review/investigation discard requires corrections and no consumed report, accept requires an already consumed report; delegated lead is relayed, not authenticated"),
    "route settlement source": definition({"reference": text(512)}, ("reference",)),
    "renewal request": definition({"id": ident(), "as": ident(), "state": choice(STATES),
        "wait": record("wait"), "executor": text(128), "external": record("external")}, ("id", "as")),
    "mapping repair": definition({
        "original_body": {"type": "text", "max_length": 524288, "max_bytes": 524288, "encoding": "UTF-8", "nul": False},
        "targets": {"type": "object", "min_items": 1, "max_items": 64,
                    "keys": ident(), "values": {"type": "text", "pattern": r"PoD#[0-9]{1,9}\Z"}},
    }, ("original_body", "targets"), only="ordinary checkpoint; body is DATA, never authority"),
    "pod mapping": definition({
        "ref": {"type": "text", "pattern": r"PoD#[0-9]{1,9}\Z"},
        **{key: {"type": "object", "form": "complete bound issue source"}
           for key in ("original_source", "accepted_source", "current_source")},
        "scope_sha256": _DIGEST, "accepted_seq": {"type": "integer", "minimum": 1},
        "invalidated": {"type": "boolean"},
    }, ("ref", "original_source", "accepted_source", "current_source", "scope_sha256", "accepted_seq", "invalidated"),
       stored_only=True),
    "pod-tick": definition({"project": text(4096), "objective": text(4096), "owner": text(4096),
                             "pod": {"type": "integer", "minimum": 0, "maximum": 999999999}},
                            ("project", "objective", "owner", "pod")),
    "packet": definition({
        "schema": choice(("pod-packet/v3",)),
        **{key: text(4096) for key in ("objective", "responsibility", "candidate", "policy_revision",
                                      "plan_revision", "report_contract")},
        **{key: items(64, text()) for key in ("criteria", "actions", "dependencies")},
        "scope": items(64, _PATH), "context": items(64, record("context_reference")),
        "sources": items(64, record("source")), "route": record("packet_route"),
        "objective_source": {"type": "object", "form": "bound issue source"},
        "worktree": record("worktree_binding"), "placement": record("worktree_binding"),
        "serves": {**items(16, ident()), "min_items": 1, "unique": True}, "role": choice(ROLES),
        "boundary": record("boundary"), "map_revision": {"type": "integer", "minimum": 1},
        "resolves": text(), "stop_condition": text(), "delta_from": text(),
        "role_label": text(48), "purpose": text(160),
        "routing_role": choice(("coordinator", "worker", "reviewer", "fixer", "auditor")),
        "fallback_of": _DIGEST,
        "decision_context": {"type": "object", "form": "bounded kernel-frozen policy and permission summary; grants no authority"},
    }, ("schema", "objective", "criteria", "responsibility", "scope", "actions", "candidate", "context",
        "dependencies", "route", "policy_revision", "plan_revision", "report_contract", "sources",
        "serves", "role", "boundary", "map_revision"), max_repr_length=65536),
    "report": definition({
        "schema": choice(("pod-report/v1",)),
        **{key: text(256) for key in ("assignment", "attempt", "candidate")},
        "outcome": choice(("succeeded", "failed", "partial", "blocked", "uncertain")),
        **{key: items(64, text()) for key in ("scope", "files", "checks", "failures", "evidence", "uncertainty", "questions")},
    }, ("schema", "assignment", "attempt", "candidate", "outcome", "scope", "files", "checks", "failures",
        "evidence", "uncertainty", "questions")),
    "checkpoint": definition({
        **_MAP_FIELDS, "schema": choice((STATE_SCHEMAS["checkpoint"],)),
        "criteria": items(64, {"type": "text", "nonempty": True}), "candidate": _GIT,
        **{key: _ANY for key in ("plan_revision", "policy_revision", "assignments", "questions",
                                  "verification_gaps", "next_safe_action", "route_decisions", "objective", "blocker", "remaining_gates")},
        "objective_source": {"type": "object", "form": "bound issue source"},
        "issue_revision_data": record("issue revision data"),
        "native_refs": items(32, {"type": "object", "form": "runId and runtime text"}),
        "worktree": record("worktree_binding"), "verification": record("verification"), "display": record("display"),
    }, ("schema", "criteria", "plan_revision", "candidate", "policy_revision", "native_refs", "assignments",
        "questions", "verification_gaps", "next_safe_action")),
    "display": definition({"task": text(48), "continued": items(32, text(256)),
                           "host_tab": {"type": "boolean"}}, ("task", "continued", "host_tab")),
    "map": definition(_MAP_FIELDS, form="seq is required for update; obligations are required at intake; otherwise omitted fields carry forward"),
    "obligation": definition({
        "id": ident(), "kind": choice(KINDS), "provenance": choice(PROVENANCES),
        "introduced_seq": {"type": "integer"}, "parent": ident(),
        **{key: text() for key in ("check", "resolves", "stop_condition", "question", "insufficiency", "existing_evidence", "uncovered_risk")},
        "boundary": record("boundary"), "scope": record("boundary"), "candidate": text(128),
        "unit": {**ident(), "only": "assurance; immutable once set"},
        "audit_role": {**choice(("reviewer", "auditor")), "only": "assurance; immutable after designation"},
        "source": {"type": "object", "records": ["objective source", "user source", "policy citation"],
                   "form": "selected by provenance"},
        "state": choice(STATES), "executor": {"type": "text", "form": "coordinator or serving admission"},
        "wait": record("wait"), "external": record("external"), "withdrawal": record("withdrawal"),
        "evidence": items(16, {"type": "object", "form": "attempt reference, short evidence or detailed evidence"}),
        "adopts": ident(), "reuse": _ANY, "findings": items(64, _ANY), "finding": _ANY,
        "definition": _ANY, "receipts": {**items(64, _ANY), "stored_only": True}, "proof_scope": items(64, _PATH),
        "renews": {**ident(), "stored_only": True},
        "renewal": {"type": "object", "stored_only": True, "form": "by: successor id, seq: renewal sequence, definition: copied identity, was_satisfied: prior state fact"},
        "acceptances": {**items(16, record("acceptance")), "stored_only": True},
        "pod_mapping": {**record("pod mapping"), "stored_only": True},
    }, ("id", "kind", "provenance")),
    "boundary": definition({"paths": items(64, {**text(), "form": "repo-relative prefix, no backslash or parent traversal"}),
                            "surfaces": items(32, ident(SURFACE))}),
    "wait": definition({"class": choice(WAIT_CLASSES),
                        "referent": {"type": "conditional", "form": "class-specific current obligation/admission, authority record or external fact",
                                     "capacity": items(8, {"type": "text"})},
                        "rationale": text(), "revisit_when": text(), "inputs": _ANY}, ("class", "referent")),
    "external": definition({"party": choice(PARTIES), "need": text(), "unblocks_when": text()},
                           ("party", "need", "unblocks_when")),
    "withdrawal": definition({"by": choice(("user_direct", "project_policy", "coordinator", "issue_revision")), "reason": text(1024),
                              "decision": {"type": "integer", "minimum": 1, "stored_only": True},
                              "instruction": _ANY, "channel": choice(("session", "relayed")), "relay": {"type": "object"}},
                             ("by", "reason")),
    "evidence": definition({"schema": choice(("pod-evidence/v1",)),
                            **{key: _ANY for key in ("criterion", "candidate", "sources", "policy_revision", "dependencies", "environment", "check", "command", "result", "reference", "reviewer_attempt")},
                            "definition": _DIGEST, "timestamp": {"type": "text", "form": "timezone-aware ISO timestamp"},
                            "status": choice(EVIDENCE_STATUSES)},
                           ("schema", "criterion", "candidate", "sources", "policy_revision", "dependencies", "environment", "check", "command", "result", "timestamp", "status", "reference")),
    "short evidence": definition({**{key: text() for key in ("check", "command", "result", "reference", "environment")},
                                  "sources": items(64, record("source")), "dependencies": items(64, text()),
                                  "timestamp": {"type": "text", "form": "timezone-aware ISO timestamp"},
                                  "status": choice(EVIDENCE_STATUSES)},
                                 ("check", "command", "result", "reference")),
    "acceptance": definition({"source": ident(SOURCE), "verdict": choice(VERDICTS),
                              "candidate": text(128), "evidence": text(1024)},
                             ("source", "verdict", "candidate", "evidence")),
    "admission": definition({**{key: {"type": "text"} for key in ("project", "objective", "run", "task")},
                             "owner": text(4096),
                             "plan_revision": _ANY, "packet": {"type": "object", "form": "frozen packet result"},
                             "worktree": _ANY, "reuse_of": _ANY,
                             "retry_of": {"type": "text", "form": "owned parent admission id; fresh terminal, excludes reuse_of"},
                             "retry_after": {"type": "text", "form": "deferred proven no-start admission id; explicit fresh intent with the same retry_of"},
                             "abandoned_reason": text(1024),
                             "map": record("admission map")},
                            ("project", "objective", "owner", "run", "task", "plan_revision", "packet")),
    "admission map": definition({"seq": _MAP_FIELDS["seq"], "obligations": _MAP_FIELDS["obligations"],
                                 "proposals": _MAP_FIELDS["proposals"]}),
    "action": definition({"kind": choice(ACTION_KINDS), **{key: text() for key in ("candidate", "target", "reason")},
                          "pr": PR_NUMBER,
                          "unit": ident(), "purpose": choice(PURPOSES), "effects": {**items(16, {"type": "text", "max_length": 256, "prefixes": list(EFFECT_PREFIXES), "nonempty_suffix": True}), "unique": True},
                          "authorization": record("authorization"), "diagnostic": record("diagnostic"),
                          "evidence": items(16, text()),
                          "inputs": {"type": "object", "max_items": 8,
                                     "keys": ident(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z"),
                                     "values": {"type": "text", "max_length": 512}}},
                         ("kind", "candidate", "target", "reason")),
    "diagnostic": definition({key: text() for key in ("question", "local_limitation", "check", "stopping_condition")},
                             ("question", "local_limitation", "check", "stopping_condition")),
    "proposal": definition({"id": ident(), "source": choice(PROPOSAL_SOURCES),
                            "origin_ref": text(256), "summary": text(), "status": choice(("open", "adopted"))},
                           ("id", "source", "origin_ref", "summary", "status")),
    "context_reference": definition({"kind": choice(("source", "instruction", "summary")), "path": _PATH,
                                     "sha256": _DIGEST, "id": text(128), "binding_digest": _DIGEST}, ("kind",)),
    "source": definition({"path": _PATH, "state": choice(("present", "absent", "unavailable")), "sha256": _DIGEST}, ("path", "state")),
    "packet_route": definition({key: {**text(256), "nullable": True} for key in
                                ("agent", "model", "effort", "context", "reason", "preference_revision")}, ("agent",)),
    "worktree_binding": definition({"repository": {"type": "text", "nullable": True, "form": "one slash"}, "repo_key": _DIGEST,
                                    "path": {"type": "text", "max_length": 4096, "form": "absolute path"},
                                    "branch": {"type": "text", "nonempty": True, "max_length": 256, "nullable": True}},
                                   ("repository", "repo_key", "path", "branch")),
}
TABLE["map evidence"] = definition({
    **TABLE["evidence"]["fields"],
    **{key: text() for key in ("candidate", "policy_revision", "environment", "check", "command", "result", "timestamp", "reference")},
    "sources": items(64, record("source")), "dependencies": items(64, text()),
}, required=TABLE["evidence"]["required"] | {"definition"})
TABLE["acceptance request"] = definition({"obligation": ident(), **TABLE["acceptance"]["fields"]},
                                         ("obligation", *TABLE["acceptance"]["required"]))
TABLE["verification"] = definition({"dependencies": {**items(64, text()), "unique": True},
                                     "environment": text()}, ("dependencies", "environment"))
TABLE["governance"] = definition({"base_ref": {**text(256), "nullable": True},
                                  "selection": {"type": "object", "form": "trusted target selection"},
                                  "exclude": {"type": "list", "unique": True,
                                              "items": choice(GOVERNANCE_PATHS)},
                                  "base": _ANY}, ("base_ref",))
TABLE["revision authority"] = definition({"provenance": choice(("user_direct",)), "instruction": _OWNER_INSTRUCTION,
                                          "operation": {**choice(("issue_revision",)), "form": "selects the checkpoint-only issue revision authority record"},
                                          "question": text(), "channel": choice(("session", "relayed")),
                                          "relay": {"type": "object"}}, ("provenance", "instruction"))
TABLE["disposition"] = definition({"admission": {"type": "text"}, "integrated_into": text(128),
                                   "discarded": {"type": "boolean"}, "reason": text(1024), "attestation": text(1024)},
                                  ("admission",))
TABLE["objective source"] = definition({"ref": text(256)}, ("ref",))
TABLE["user source"] = definition({"instruction": _OWNER_INSTRUCTION, "ref": text(256), "question": _ANY,
                                   "channel": choice(("session", "relayed")), "relay": {"type": "object"}},
                                  ("instruction",))
TABLE["policy citation"] = definition({"path": choice(GOVERNANCE_PATHS),
                                       "lines": {"type": "text", "pattern": LINES, "max_lines": 64},
                                       **{key: _ANY for key in ("revision", "quote_sha256", "base", "gone")}},
                                      ("path", "lines"))
TABLE["authorization"] = definition({
    "schema": choice(("pod-authorization/v1",)),
    **{key: {"type": "text", "pattern": r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", "max_length": 64}
       for key in ("candidate", "tree")},
    **{key: text(512) for key in ("authorized_by", "reference", "target")},
    "scope": {**items(8, choice(AUTHORIZATION_SCOPES)), "min_items": 1},
    "utc": {"type": "text", "form": "timezone-aware ISO timestamp"},
    "provenance": choice(("user_direct",)), "instruction": _OWNER_INSTRUCTION,
    "channel": {**choice(("session", "relayed")), "form": "relayed publish/merge requires the host verifier"},
    "relay": record("authorization relay"),
    "pr": {**PR_NUMBER, "form": "required for relayed merge only; equals action.pr"},
    "relay_verification": {"type": "object", "form": "stored verifier verdict; never caller authority"},
    "standing": {"type": "object", "stored_only": True, "form": "standing decision id, reference and channel; never caller authority"},
}, ("schema", "candidate", "tree", "scope", "authorized_by", "utc", "reference"))
TABLE["decision_relay"] = definition({key: text(512) for key in ("reference", "owner_source")},
                                     ("reference", "owner_source"))
TABLE["authorization relay"] = definition({key: text(512) for key in ("reference", "owner_source")},
                                          ("reference",))
TABLE["issue revision relay"] = definition({"reference": text(512)}, ("reference",))
TABLE["decision_action_relay"] = definition({"reference": text(512), "verification": choice(("decision",))},
                                             ("reference", "verification"))
TABLE["issue revision authority"] = definition({"operation": choice(("issue_revision",)),
    "provenance": choice(("user_direct", "agent_decision", "decision_request")), "instruction": _OWNER_INSTRUCTION,
    "channel": choice(("relayed", "agent_session")),
    "relay": {"type": "object", "records": ["issue revision relay", "decision_action_relay"]}},
    ("operation", "provenance", "instruction", "channel"),
    required_by_channel={"agent_session": ["operation", "provenance", "instruction", "channel"],
                         "relayed": ["operation", "provenance", "instruction", "channel", "relay"]})
STANDING_SCOPES = ("publish", "merge", "release", "install", "cleanup", "deploy")
STANDING_EXCLUSIONS = ("deploy", "paid_resources", "secrets", "unique_deletion", "scope_growth")
TABLE["standing decision"] = definition({
    "provenance": choice(("user_direct",)), "instruction": _OWNER_INSTRUCTION,
    "channel": choice(("session", "relayed")), "relay": record("authorization relay"),
    "scope": {**items(6, choice(STANDING_SCOPES)), "min_items": 1, "unique": True},
    "widening": {**items(5, choice(STANDING_EXCLUSIONS)), "unique": True},
    "expires_at": {"type": "text", "nullable": True, "form": "future timezone-aware ISO timestamp"},
    "scope_digest": _DIGEST,
    "spending": choice(("included_subscription", "new_commitment")),
}, ("provenance", "instruction"))
TABLE["responder answer"] = definition({
    "question": text(512), "text": text(4096),
    "relay": {"type": "object", "fields": {"reference": text(512)}},
}, ("question", "text", "relay"))
TABLE["issue revision data"] = definition({
    "historical_bodies": items(64, record("issue body data")),
}, ("historical_bodies",), max_bytes=128 * 1024)
TABLE["issue body data"] = definition({
    "body_sha256": _DIGEST,
    "body": {"type": "text", "max_length": 512 * 1024, "max_bytes": 512 * 1024,
             "form": "exact UTF-8 body DATA, never authority"},
}, ("body_sha256", "body"))
TABLE["standing decision"]["stored_max_bytes"] = 4 * 1024
TABLE["responder answer"]["stored_max_bytes"] = 8 * 1024
TABLE["standing authorization"] = definition({
    **{key: text(4096) for key in ("project", "objective", "owner")},
    "action": choice(("activate", "revoke", "answer", "show")),
    "decision": {**record("standing decision"), "fields": TABLE["standing decision"]["fields"]},
    "answer": {**record("responder answer"), "fields": TABLE["responder answer"]["fields"]},
}, ("project", "objective", "owner", "action"))
TABLE["finding"] = definition({
    "finding": ident(), "severity": choice(SEVERITIES), "triage": choice(TRIAGE),
    "summary": text(512),
    "reason": {**text(1024), "validated_when": "blocker or major finding downgraded to advisory"},
    "root_cause": text(512),
    "findings": {**items(8, record("group member")), "min_items": 1},
    "correction": {"type": "text", "form": "new correction obligation identifier"},
}, ("triage", "summary"),
    required_by_shape={"single": ["finding", "severity", "triage", "summary"],
                       "group": ["findings", "triage", "summary"]},
    form="finding and severity or findings, exclusively; an advisory group also needs root_cause")
TABLE["group member"] = definition({"finding": ident(), "severity": choice(SEVERITIES)}, ("finding", "severity"))
TABLE["worker_tab_instruction"] = definition({
    **{key: text(256) for key in ("objective", "dispatch", "terminal")}, "keep": {"type": "boolean"},
}, ("objective", "dispatch", "terminal", "keep"))
TABLE["objective tool"] = definition({
    "project": text(4096), "objective": text(4096), "owner": text(4096),
    "action": choice(("check", "review", "answer", "cancel")), "tool": text(128), "category": text(64),
    "operation": text(256), "spending": choice(("included_subscription", "new_commitment")),
    "invocation": text(128), "answer": text(64), "decision": {"type": "object"},
}, ("project", "objective", "action"),
    required_by_action={"check": ["tool", "category", "operation"], "review": ["tool", "category"],
                        "answer": ["invocation", "answer", "decision"], "cancel": ["invocation"]})
TABLE["report request"] = definition({
    **{key: {"type": "text"} for key in ("project", "objective", "admission_id")},
    "packet": {"type": "object", "form": "frozen packet result"}, "report": record("report"),
    "map": record("map"), "triage": items(32, record("finding")), "proposals": items(64, record("proposal")),
    "result_commit": _GIT,
}, ("report", "packet", "project", "objective", "admission_id"))
TABLE["context_reference"]["variants"] = {
    "source": ["kind", "path", "sha256"], "instruction": ["kind", "path", "sha256"],
    "summary": ["kind", "id", "binding_digest"],
}
TABLE["context_reference"]["form"] = "each kind accepts exactly its variant's fields"
TABLE["packet_route"]["required_by_agent"] = {
    "direct": ["agent"], "other": sorted(TABLE["packet_route"]["fields"]),
}

# Additive input channels use the same records. Evidence is stored-only and the
# mutation validator rejects it on public input before it can become authority.
for _name in ("revision authority", "user source", "authorization", "standing decision", "withdrawal"):
    _fields = TABLE[_name]["fields"]
    if "channel" in _fields:
        _fields["channel"] = choice(("session", "relayed", "agent_session"))
    if "provenance" in _fields:
        _fields["provenance"] = choice(("user_direct", "agent_decision", "decision_request"))
    _relay_record = _fields.get("relay", {}).get("record", "decision_relay")
    _fields["relay"] = {"type": "object", "records": [_relay_record, "decision_action_relay"],
                        "form": "decision_request uses decision_action_relay; session channels carry no relay"}
    _fields.update({key: {"type": "text" if key in ("decided_at", "recorded_at") else "object", "stored_only": True}
                    for key in ("actor", "decided_at", "recorded_at", "decision_binding", "decision_verification")})
TABLE["withdrawal"]["fields"]["by"] = choice(("user_direct", "agent_decision", "project_policy", "coordinator", "issue_revision"))
TABLE["withdrawal"]["fields"]["accepted_seq"] = {"type": "integer", "stored_only": True}


def fields(name):
    return set(TABLE[name]["fields"])


def required(name):
    return set(TABLE[name]["required"])


def bound(name, field, key="max_length"):
    return TABLE[name]["fields"][field][key]


def describe(name):
    row = TABLE[name]
    return {"accepted": sorted(row["fields"]), "optional": sorted(fields(name) - required(name)),
            "fields": deepcopy(row["fields"]),
            **{key: deepcopy(value) for key, value in row.items() if key not in ("fields", "required")}}


def bounded_fields(name):
    def bounded(spec):
        return any(key in spec for key in ("max_length", "max_items", "pattern", "allowed")) or any(
            bounded(value) for value in spec.values() if isinstance(value, dict))
    return {key: deepcopy(spec) for key, spec in TABLE[name]["fields"].items() if bounded(spec)}


_CURRENT = ContextVar("pod_input_record", default=None)


def current():
    return _CURRENT.get()


def input_action(name):
    return "correct the named " + name + " fields using its complete accepted/optional names and every listed field bound, then retry"


def annotate(exc, name, *, accepted=None, optional=None):
    """Add the whole constant contract, without replacing existing diagnostics."""
    if (not isinstance(exc, PodError) or name not in TABLE
            or exc.detail is not None and not isinstance(exc.detail, dict)):
        return
    detail = dict(exc.detail or {})
    target = detail["referent"] if isinstance(detail.get("referent"), dict) else detail
    if target.get("record") in TABLE:
        name = target["record"]
    detail.setdefault("next_action", input_action(name))
    exc.detail = detail
    if "bounded_fields" in target:
        return  # The innermost record owns its refusal.
    target.setdefault("record", name)
    target["bounded_fields"] = bounded_fields(name)
    target.setdefault("accepted", sorted(accepted if accepted is not None else fields(name)))
    target.setdefault("optional", sorted(optional if optional is not None else fields(name) - required(name)))
    exc.detail = detail


def input_record(name):
    """Preserve validator behavior, decorating only its input-shape/limit errors."""
    def decorate(fn):
        @wraps(fn)
        def checked(*args, **kwargs):
            token = _CURRENT.set(name)
            try:
                return fn(*args, **kwargs)
            except PodError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                if (isinstance(exc, FieldRefusal) or exc.code.startswith("invalid_")
                        or detail.get("detail") in ("malformed", "external_invalid", "assurance_incomplete",
                                                     "no_check", "missing_state", "multiple_states", "missing", "role_kind_mismatch", "evidence_unbound")
                        or name == "wait" and exc.code == "wait_invalid"
                        or name == "checkpoint" and "Run references are malformed" in str(exc)):
                    annotate(exc, name)
                raise
            finally:
                _CURRENT.reset(token)
        return checked
    return decorate


def policy_mismatch(message, *, checkpoint, effective, packet=None, replay=False):
    from .errors import PodError
    def known(value):
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
    digests = {"checkpoint": known(checkpoint), "effective": known(effective)}
    if packet is not None:
        digests["packet"] = known(packet)
    action = ("effective policy depends on the project: run pod config --json inside the objective's project "
              "and set every pod internal request's project to that project; read pod internal map for the "
              "checkpoint policy_revision; ")
    action += ("retry the same pending request unchanged in that project; a genuine policy/core change keeps replay blocked under its existing authority checks"
               if replay else "refresh the checkpoint there, then freeze the packet with that policy_revision")
    return PodError("policy_revision_mismatch", message + ". Policy digests: " + str(digests) + ". Next: " + action,
                    {"policy_digests": digests, "expected": known(effective),
                     "received": known(checkpoint if checkpoint != effective else packet), "next_action": action})

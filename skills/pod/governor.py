"""The waste governor: a deterministic decision kernel before an expensive Pod-mediated effect.

It evaluates at boundaries, not continuously: before a managed remote action, after a
validation result, when a candidate is superseded, and during recovery. Every decision
is a function of durable objective records plus the proposed action. There is no
background agent, no cost model, no scoring and no model call.

Three outcomes: ALLOW executes the exactly bound action, REUSE returns existing valid
evidence or attaches to an equivalent action already running, and DEFER names the
reason and the next useful action. A warning is an annotation, never a fourth state.
Authorization denial stays a separate class from "permitted but premature".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import platform
import re
import subprocess
from typing import Protocol

import yaml

from .config import GOVERNED_KINDS, effective, validate_effects
from .errors import PodError
from .ledger import _intervention_locked, _lock, _path, _read, _write, objective_root
from .util import MAX_RECORD, atomic_json, bounded_json, bounded_text, digest, exact

SCHEMA = "pod-governor/v2"
KINDS = GOVERNED_KINDS
AUTHORIZED_KINDS = ("merge", "release", "deploy")
VALIDATION_KINDS = ("workflow_dispatch", "validation_rerun")
PUBLICATION_KINDS = ("push", "pr_update")
DISPATCH_KINDS = ("workflow_dispatch", "remote_diagnostic")
EXECUTABLE_KINDS = ("push", "pr_update", "workflow_dispatch", "validation_rerun",
                    "remote_diagnostic", "cancel_validation")
PURPOSES = ("validation", "diagnostic", "checkpoint", "release", "cancellation")
DECISIONS = ("ALLOW", "REUSE", "DEFER")
OUTCOMES = ("pending", "PASS", "FAILED", "UNKNOWN", "CANCELED")
CLASSES = ("authorization", "correctness", "efficiency")
FAILURE_CLASSES = ("code_defect", "remote_only", "transient", "external")
PREFLIGHT_STATUSES = ("PASS", "FAILED", "UNAVAILABLE")
DIAGNOSTIC_FIELDS = {"question", "local_limitation", "check", "stopping_condition"}
DEFAULT_UNIT = "default"
# Mirrors the ledger's own threshold: a third correction needs a discriminating diagnosis.
CORRECTION_THRESHOLD = 2
# Rows are pretty-printed receipts of about a kilobyte; the journal keeps its own bound so
# the cap on rows and the cap on bytes agree, and compaction runs before either is hit.
MAX_ACTIONS = 96
MAX_JOURNAL = 2 * MAX_RECORD
MAX_UNITS = 32
MAX_HISTORY = 16
MAX_WORKFLOWS = 32
_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml\Z")
_GIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
NEXT_ACTIONS = {
    "authorization_missing": "supply the owner authorization record naming this candidate, tree and scope",
    "unit_unknown": "prepare the delivery unit's candidate with the governor-prepare helper",
    "unit_unbound": "prepare the delivery unit's candidate and branch with the governor-prepare helper",
    "candidate_unbound": "checkpoint a candidate, or prepare one for the delivery unit",
    "superseded_candidate": "re-issue the request against the unit's current candidate generation",
    "candidate_unfrozen": "commit or discard the working-tree changes, then prepare the candidate again",
    "candidate_unpublished": "push the candidate commit to the unit's branch first",
    "effects_unknown": "declare the action's downstream effects, or map waste_governor.triggers in project policy",
    "effect_unresolved": "reconcile the unresolved submission with the governor-reconcile helper before submitting again",
    "integration_unsettled": "settle the unit's native work and corrections first",
    "diagnosis_required": "record a discriminating diagnosis for the unit's repeated failure",
    "local_preflight_missing": "run the configured local preflight and record each result for this candidate",
    "blocking_findings": "fix the failing local check, then prepare a new candidate",
    "verification_gaps": "close the checkpoint's verification gaps",
    "failure_unclassified": "classify the previous failure with the governor-classify helper",
    "unchanged_rerun": "fix the defect locally and prepare a new candidate; nothing new would be learned from the same one",
    "remote_only_needs_diagnostic": "request a bounded remote diagnostic naming the question and its stopping condition",
    "transient_budget_exhausted": "the retry budget for this candidate is spent; inspect the failure or prepare a new candidate",
    "external_blocker": "report the external blocker; re-classify it once the dependency is restored",
    "repeat_diagnostic": "this probe already ran unchanged; change the check or record the answer",
    "publication_unsettled": "record or reconcile the pending publication; its triggered run is the validation",
    "exception_grant_required": "bind the exception to a current personal efficiency_exception grant",
}


class GitHubPort(Protocol):
    """The remote operations a managed executor may perform. Every call is bounded."""

    def branch_head(self, *, remote: str, branch: str) -> str | None: ...
    def push(self, *, remote: str, branch: str, commit: str, expected: str | None) -> dict: ...
    def pull_request(self, *, head: str, base: str) -> dict | None: ...
    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> dict: ...
    def dispatch(self, *, workflow: str, ref: str, inputs: dict) -> dict: ...
    def runs(self, *, workflow: str, commit: str) -> list[dict]: ...
    def run(self, *, run_id: str) -> dict: ...
    def rerun(self, *, run_id: str, failed_only: bool) -> dict: ...
    def cancel(self, *, run_id: str) -> dict: ...


# --------------------------------------------------------------------------- journal

def _record_path(project: Path, objective: str) -> Path:
    return objective_root(project, objective) / "governor.json"


def _empty_counters() -> dict:
    return {"decisions": {name: 0 for name in DECISIONS}, "deferrals": {}, "warnings": {},
            "outcomes": {name: 0 for name in OUTCOMES if name != "pending"},
            "attachments": 0, "evidence_reused": 0, "exceptions_applied": 0,
            "cancellations": 0, "failures_interrupted": 0, "observed_deferrals": 0}


def _empty_journal() -> dict:
    return {"schema": SCHEMA, "revision": 0, "units": {}, "actions": [], "counters": _empty_counters()}


def _malformed(detail: str) -> PodError:
    return PodError("state_unsupported", "Governor journal is malformed: " + detail)


def _validate_row(row: object) -> None:
    if (not isinstance(row, dict)
            or not isinstance(row.get("record_id"), str)
            or not isinstance(row.get("logical_key"), str)
            or type(row.get("attempt")) is not int
            or not isinstance(row.get("action"), dict)
            or row["action"].get("kind") not in KINDS
            or not isinstance(row["action"].get("candidate"), str)
            or not isinstance(row["action"].get("target"), str)
            or not isinstance(row["action"].get("unit"), str)
            or row.get("decision") not in DECISIONS
            or row.get("outcome") not in OUTCOMES
            or not isinstance(row.get("receipt"), dict)
            or not isinstance(row.get("warnings"), list)
            or not isinstance(row.get("reasons"), list)
            or not isinstance(row.get("classifications"), list)):
        raise _malformed("action row")


def _validate_unit(name: str, unit: object) -> None:
    if (not isinstance(unit, dict) or unit.get("name") != name
            or type(unit.get("generation")) is not int
            or not isinstance(unit.get("history"), list)
            or not isinstance(unit.get("preflight"), dict)
            or not isinstance(unit.get("published"), dict)
            or not isinstance(unit.get("tasks"), list)
            or (unit.get("candidate") is not None and not isinstance(unit["candidate"], dict))):
        raise _malformed("unit record " + name)


def _read_journal(path: Path) -> dict:
    if not path.exists():
        return _empty_journal()
    value = bounded_json(path, limit=MAX_JOURNAL)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PodError("state_unsupported",
                       f"Governor journal is not {SCHEMA}; archive or remove it before governed actions")
    exact(value, {"schema", "revision", "units", "actions", "counters"},
          {"schema", "revision", "units", "actions", "counters"}, name="governor")
    if (not isinstance(value["actions"], list) or len(value["actions"]) > MAX_ACTIONS
            or not isinstance(value["units"], dict) or len(value["units"]) > MAX_UNITS
            or not isinstance(value["counters"], dict)):
        raise _malformed("inventory")
    for row in value["actions"]:
        _validate_row(row)
    for name, unit in value["units"].items():
        _validate_unit(name, unit)
    return value


def _write_journal(path: Path, journal: dict) -> None:
    journal["revision"] += 1
    atomic_json(path, journal, limit=MAX_JOURNAL)


def _compact(journal: dict) -> None:
    """Bounded retention: settled rows of superseded candidates go first; nothing unresolved does."""
    if len(journal["actions"]) < MAX_ACTIONS:
        return
    current = {unit["candidate"]["id"] for unit in journal["units"].values() if unit.get("candidate")}
    referenced = {row.get("derived_from") for row in journal["actions"]
                  if row["outcome"] in ("pending", "UNKNOWN")}
    kept = []
    dropped = 0
    for row in journal["actions"]:
        settled = row["outcome"] in ("PASS", "FAILED", "CANCELED")
        stale = row.get("candidate_id") is not None and row["candidate_id"] not in current
        if (settled and stale and row["record_id"] not in referenced
                and len(journal["actions"]) - dropped >= MAX_ACTIONS):
            dropped += 1
            continue
        kept.append(row)
    journal["actions"] = kept
    if len(journal["actions"]) >= MAX_ACTIONS:
        raise PodError("governor_full", "Governor journal is full; start a new objective record")


# --------------------------------------------------------------------------- validation

def validate_authorization(value: object, *, candidate: str | None = None, tree: str | None = None,
                           kind: str | None = None) -> dict | None:
    """Accept only a complete owner authorization record bound to this candidate.

    The release gate owns the record's shape. Reusing its validator is what keeps a record
    from passing here and being refused there, or the reverse.
    """
    if value is None:
        return None
    from .release import validate_authorization as validate_release_authorization

    record = validate_release_authorization(
        value, candidate=value.get("candidate") if isinstance(value, dict) else None,
        tree=value.get("tree") if isinstance(value, dict) else None, require_release=False)
    if record is None:
        return None
    if candidate is not None and record["candidate"] != candidate:
        return None
    if tree is not None and record["tree"] != tree:
        return None
    if kind is not None and kind not in record["scope"]:
        return None
    return record


def _validate_diagnostic(value: object) -> dict:
    record = exact(value, DIAGNOSTIC_FIELDS, DIAGNOSTIC_FIELDS, name="diagnostic")
    for key in DIAGNOSTIC_FIELDS:
        bounded_text(record[key], name=key, limit=512)
    return record


def validate_action(value: object) -> dict:
    """Normalise one action request. Purpose is derived from what the action triggers."""
    action = exact(value, {"kind", "candidate", "target", "reason", "unit", "purpose", "effects",
                           "authorization", "diagnostic", "evidence", "inputs"},
                   {"kind", "candidate", "target", "reason"}, name="action")
    if action["kind"] not in KINDS:
        raise PodError("invalid_action", "Unsupported governed action kind")
    for key in ("candidate", "target", "reason"):
        bounded_text(action[key], name=key, limit=512)
    unit = action.get("unit", DEFAULT_UNIT)
    if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
        raise PodError("invalid_action", "Delivery unit names are bounded identifiers")
    effects = action.get("effects")
    if effects is not None:
        validate_effects(effects)
    diagnostic = action.get("diagnostic")
    if (action["kind"] == "remote_diagnostic") != (diagnostic is not None):
        raise PodError("invalid_action", "Exactly a remote diagnostic carries a diagnostic record")
    if diagnostic is not None:
        diagnostic = _validate_diagnostic(diagnostic)
    evidence = action.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 16:
        raise PodError("invalid_action", "Evidence references are a bounded list")
    for item in evidence:
        bounded_text(item, name="evidence", limit=512)
    inputs = action.get("inputs", {})
    if (not isinstance(inputs, dict) or len(inputs) > 8
            or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", k)
                   or not isinstance(v, str) or len(v) > 512 for k, v in inputs.items())):
        raise PodError("invalid_action", "Dispatch inputs are a bounded string mapping")
    if inputs and action["kind"] not in DISPATCH_KINDS:
        raise PodError("invalid_action", "Only a workflow dispatch or diagnostic carries inputs")
    if action.get("purpose") is not None and action["purpose"] not in PURPOSES:
        raise PodError("invalid_action", "Unsupported action purpose")
    return {"kind": action["kind"], "candidate": action["candidate"], "target": action["target"],
            "reason": action["reason"], "unit": unit, "effects": effects,
            "purpose": action.get("purpose"), "authorization": action.get("authorization"),
            "diagnostic": diagnostic, "evidence": evidence, "inputs": inputs}


def _purpose(kind: str, *, validates: bool, deploys: bool, releases: bool) -> str:
    if kind == "cancel_validation":
        return "cancellation"
    if kind == "remote_diagnostic":
        return "diagnostic"
    if kind in AUTHORIZED_KINDS or deploys or releases:
        return "release"
    if validates:
        return "validation"
    return "checkpoint"


def _effects(action: dict, governor_policy: dict) -> tuple[list[str] | None, str]:
    """What the action really triggers: intrinsic, declared, project-mapped, or unknown."""
    kind, target = action["kind"], action["target"]
    if kind in DISPATCH_KINDS or kind == "validation_rerun":
        intrinsic = ["workflow:" + target]
    elif kind in AUTHORIZED_KINDS or kind == "cancel_validation":
        intrinsic = [kind.split("_")[0] + ":" + target]
    else:
        intrinsic = None
    declared = action.get("effects")
    if intrinsic is not None:
        return sorted(set(intrinsic) | set(declared or [])), "intrinsic"
    if declared is not None:
        return sorted(declared), "declared"
    mapped = governor_policy.get("triggers", {}).get(kind)
    if mapped is not None:
        return sorted(mapped), "mapped"
    return None, "unknown"


def _logical_key(action: dict, candidate_id: str | None) -> str:
    diagnostic = action.get("diagnostic")
    return digest({"kind": action["kind"], "unit": action["unit"],
                   "candidate": candidate_id or action["candidate"], "target": action["target"],
                   "effects": action.get("effects"),
                   "check": diagnostic.get("check") if isinstance(diagnostic, dict) else None})


def _validate_exception(value: object) -> dict | None:
    if value is None:
        return None
    record = exact(value, {"grant", "reason", "by"}, {"grant", "reason", "by"}, name="exception")
    for key in ("grant", "reason", "by"):
        bounded_text(record[key], name=key, limit=512)
    return record


def _exception_grant(governor_policy: dict, supplied: dict | None, *, action: dict, objective: str,
                     candidate_ids: set[str], now: datetime) -> dict | None:
    """A scoped exception rejoins a personal grant; it is never a generic force flag."""
    if supplied is None:
        return None
    matches = [grant for grant in governor_policy.get("exceptions", [])
               if isinstance(grant, dict) and grant.get("id") == supplied["grant"]]
    if len(matches) != 1:
        raise PodError("exception_grant_required", "Exception grant is not a current personal grant")
    grant = matches[0]
    try:
        expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise PodError("exception_grant_required", "Exception grant validity is invalid") from exc
    if (grant.get("objective") != objective or action["kind"] not in grant.get("kinds", [])
            or (grant.get("unit") is not None and grant["unit"] != action["unit"])
            or (grant.get("candidate") is not None and grant["candidate"] not in candidate_ids)
            or expiry.tzinfo is None or now.tzinfo is None or now > expiry):
        raise PodError("exception_grant_required", "Exception grant does not bind this action, unit, candidate or time")
    return {**supplied, "grant_identity": digest(grant)}


# --------------------------------------------------------------------------- scoping

def _unit_tasks(unit: dict | None) -> list[str] | None:
    return None if unit is None else list(unit.get("tasks", []))


def _active(native_projection: dict | None, tasks: list[str] | None = None) -> list[str]:
    """Objective-local logical assignments supplied at the governor boundary."""
    if not isinstance(native_projection, dict):
        return []
    rows = native_projection.get("outstanding")
    if not isinstance(rows, list):
        raise PodError("native_assignment_unverified", "Governor logical projection is malformed")
    active = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PodError("native_assignment_unverified", "Governor logical projection is malformed")
        task = row.get("task")
        if tasks is not None and task is not None and task not in tasks:
            continue
        active.append(str(index) + ":" + str(task or "unbound"))
    return active


def _pending_interventions(state: dict, unit: str | None = None,
                           tasks: list[str] | None = None) -> list[str]:
    """Tasks or units that reached the correction threshold with no diagnosis recorded yet."""
    pending = []
    for key, history in state.get("interventions", {}).items():
        if not isinstance(history, list) or len(history) < CORRECTION_THRESHOLD:
            continue
        if any(isinstance(row, dict) and row.get("diagnosis") for row in history):
            continue
        if tasks is not None and key not in tasks and key != "unit:" + str(unit):
            continue
        pending.append(key)
    return sorted(pending)


def _checkpoint(state: dict) -> dict:
    value = state.get("checkpoint")
    return value if isinstance(value, dict) else {}


def _inputs(state: dict) -> str:
    """A digest of everything a rerun would consume; unchanged means nothing to learn."""
    checkpoint = _checkpoint(state)
    return digest({"candidate": checkpoint.get("candidate"),
                   "plan_revision": checkpoint.get("plan_revision"),
                   "policy_revision": checkpoint.get("policy_revision"),
                   "gaps": checkpoint.get("verification_gaps"),
                   "interventions": digest(state.get("interventions", {})),
                   "sources": state.get("source_rejections", {})})


def _phase(state: dict, actions: list[dict], unit: dict | None, candidate: str | None,
           native_projection: dict | None = None) -> str:
    checkpoint = _checkpoint(state)
    if native_projection is None and (state.get("admissions") or checkpoint.get("native_refs")):
        raise PodError("native_assignment_unverified",
                       "Governor needs exact objective assignment evidence for native-bound work")
    tasks = _unit_tasks(unit)
    if not candidate or _active(native_projection, tasks):
        return "working"
    name = unit["name"] if unit else None
    if _pending_interventions(state, name, tasks):
        return "converging"
    passed = {(row["action"]["kind"], row["action"]["candidate"], row.get("candidate_id"))
              for row in actions if row.get("outcome") == "PASS" and row.get("decision") == "ALLOW"}
    def done(kinds):
        return any(kind == k and (candidate in (c, cid)) for kind, c, cid in passed for k in kinds)
    if done(("merge", "release")):
        return "complete"
    if done(VALIDATION_KINDS):
        return "remotely_verified"
    return "candidate"


def unit_bound(project: Path, objective: str, state: dict, unit: str) -> bool:
    """Whether a unit has a prepared candidate, or is the default unit with a checkpoint."""
    journal = _read_journal(_record_path(project, objective))
    record = journal["units"].get(unit)
    if isinstance(record, dict) and record.get("candidate"):
        return True
    return unit == DEFAULT_UNIT and bool(_checkpoint(state).get("candidate"))


# --------------------------------------------------------------------------- evaluation

def _reason(reasons: list[dict], cls: str, code: str, detail: str) -> None:
    reasons.append({"class": cls, "code": code, "detail": detail})


def _warn(warnings: list[dict], code: str, detail: str) -> None:
    warnings.append({"code": code, "detail": detail})


def _evaluate(action: dict, state: dict, journal: dict, governor_policy: dict, *,
              managed: bool = False, native_projection: dict | None = None) -> dict:
    """Apply the decision order and return reasons, warnings, reuse and bindings."""
    reasons: list[dict] = []
    warnings: list[dict] = []
    kind = action["kind"]
    unit = journal["units"].get(action["unit"])
    binding = unit.get("candidate") if unit else None
    if managed and (unit is None or (kind != "cancel_validation"
                                     and (binding is None or unit.get("branch") is None))):
        _reason(reasons, "correctness", "unit_unbound",
                "managed execution needs the unit's prepared candidate and its remote, branch and base")
    checkpoint = _checkpoint(state)
    effects, source = _effects(action, governor_policy)
    validates = kind in VALIDATION_KINDS or bool(effects and any(e.startswith("workflow:") for e in effects))
    deploys = kind == "deploy" or bool(effects and any(e.startswith("deploy:") for e in effects))
    releases = kind == "release" or bool(effects and any(e.startswith("release:") for e in effects))
    purpose = _purpose(kind, validates=validates, deploys=deploys, releases=releases)
    if action.get("purpose") not in (None, purpose):
        raise PodError("invalid_action", f"This action's triggers make its purpose {purpose}")
    names_current = binding is not None and action["candidate"] in (binding["id"], binding["commit"])
    candidate_id = binding["id"] if names_current else None
    candidate_ids = {action["candidate"]} | ({binding["id"], binding["commit"]} if names_current else set())

    # 1. Authority and project requirements.
    scopes = set()
    if kind in AUTHORIZED_KINDS:
        scopes.add(kind)
    if deploys:
        scopes.add("deploy")
    if releases:
        scopes.add("release")
    for scope in sorted(scopes):
        authorized = validate_authorization(action.get("authorization"),
                                            candidate=binding["commit"] if binding else action["candidate"],
                                            tree=binding["tree"] if binding else None, kind=scope)
        if authorized is None:
            _reason(reasons, "authorization", "authorization_missing",
                    f"{scope} needs an owner authorization record naming this candidate, tree and scope")

    # 2. Bind the request to the current candidate and its context.
    if action["unit"] != DEFAULT_UNIT and unit is None:
        _reason(reasons, "correctness", "unit_unknown", f"no delivery unit named {action['unit']} is prepared")
    current = binding["id"] if binding else checkpoint.get("candidate")
    if binding is None and not current:
        _reason(reasons, "correctness", "candidate_unbound", "no candidate is bound for this unit or objective")
    superseded = (binding is not None and action["candidate"] not in (binding["id"], binding["commit"])) or (
        binding is None and bool(current) and action["candidate"] != current)
    if superseded:
        detail = (f"current candidate is {binding['id'][:12]} (generation {binding['generation']})"
                  if binding else f"checkpoint candidate is {current}")
        if kind == "remote_diagnostic":
            _warn(warnings, "superseded_candidate", "diagnostic runs against a superseded candidate; " + detail)
        else:
            _reason(reasons, "correctness", "superseded_candidate", detail)
    if binding and binding.get("dirty_paths") and (validates or purpose == "release" or kind in PUBLICATION_KINDS):
        _reason(reasons, "correctness", "candidate_unfrozen",
                f"{binding['dirty_paths']} tracked path(s) were modified when the candidate was observed")
    if effects is None:
        _reason(reasons, "correctness", "effects_unknown",
                f"what a {kind} triggers is neither declared on the request nor mapped by project policy")
    # A diagnostic runs at the branch as it stands; it asks a question, it does not
    # validate the candidate, so it is not held for the candidate to be published.
    starting = [row for row in journal["actions"]
                if row["action"]["unit"] == action["unit"] and row["decision"] == "ALLOW"
                and row["action"]["kind"] in PUBLICATION_KINDS and candidate_id is not None
                and row.get("candidate_id") == candidate_id and row["outcome"] in ("pending", "UNKNOWN")
                and "workflow:" + action["target"] in (row.get("effects") or [])]
    if (binding and validates and kind not in PUBLICATION_KINDS and kind != "remote_diagnostic"
            and unit.get("branch") and not starting):
        key = unit["branch"]["remote"] + "/" + unit["branch"]["branch"]
        if unit["published"].get(key) != binding["commit"]:
            _reason(reasons, "correctness", "candidate_unpublished",
                    f"{key} does not carry the candidate commit according to recorded pushes")

    logical_key = _logical_key(action, candidate_id)
    same = [row for row in journal["actions"] if row["logical_key"] == logical_key and row["decision"] == "ALLOW"]
    latest = same[-1] if same else None
    reuse = None

    def verdict(phase: str, stale_pending: list[str]) -> dict:
        return {"reasons": reasons, "warnings": warnings, "reuse": reuse, "phase": phase,
                "logical_key": logical_key, "candidate_id": candidate_id, "candidate_ids": candidate_ids,
                "commit": binding["commit"] if names_current else None,
                "effects": effects, "effect_source": source, "purpose": purpose,
                "cancel_safe": purpose in ("validation", "diagnostic") and not deploys and not releases,
                "attempt": (latest["attempt"] + 1) if latest is not None else 1,
                "unit": unit, "binding": binding, "stale_pending": stale_pending}

    if superseded and kind != "remote_diagnostic":
        # Nothing after the binding step is about this request; it names the wrong candidate.
        return verdict(_phase(state, journal["actions"], unit, current, native_projection), [])

    # 3. An equivalent action already running, or valid evidence already recorded.
    if latest is None and kind in VALIDATION_KINDS and candidate_id is not None:
        # A push or pull-request update that triggers this workflow journals a derived
        # validation row when it lands; the same key finds it. A publication still pending
        # is the run about to start, so a dispatch now would only double it.
        if starting:
            _reason(reasons, "efficiency", "publication_unsettled",
                    f"{starting[-1]['action']['kind']} {starting[-1]['record_id'][:12]} will start this workflow "
                    "once it lands; settle it first")
    if latest is not None and not superseded:
        if latest["outcome"] == "pending":
            reuse = {"record_id": latest["record_id"], "kind": "attach",
                     "detail": "an identical action is already running"}
        elif latest["outcome"] == "PASS":
            if kind == "remote_diagnostic":
                reuse = {"record_id": latest["record_id"], "kind": "evidence",
                         "detail": "this diagnostic already answered against the same candidate and target"}
            elif purpose == "validation" and kind not in PUBLICATION_KINDS:
                reuse = {"record_id": latest["record_id"], "kind": "evidence",
                         "detail": "a passing result already binds this candidate and context"}
            else:
                reuse = {"record_id": latest["record_id"], "kind": "already_done",
                         "detail": "this action already completed for the same candidate and target"}
            if binding is None:
                _warn(warnings, "context_unbound",
                      "the reused evidence binds a commit only; workflow, base and environment were not frozen")
        elif latest["outcome"] == "UNKNOWN":
            _reason(reasons, "correctness", "effect_unresolved",
                    f"attempt {latest['attempt']} of this action has no known outcome")

    # 4. Supersedence and unresolved prior effects in this unit.
    for row in journal["actions"]:
        if (row["action"]["unit"] == action["unit"] and row["decision"] == "ALLOW"
                and row["outcome"] == "UNKNOWN" and row["logical_key"] != logical_key
                and row["action"]["kind"] != "cancel_validation"):
            _reason(reasons, "correctness", "effect_unresolved",
                    f"{row['action']['kind']} {row['record_id'][:12]} has no known outcome")
            break
    stale_pending = [row["record_id"] for row in journal["actions"]
                     if row["action"]["unit"] == action["unit"] and row["decision"] == "ALLOW"
                     and row["outcome"] == "pending" and row.get("cancel_safe")
                     and candidate_id is not None and row.get("candidate_id") != candidate_id]
    if stale_pending:
        _warn(warnings, "superseded_validation_pending",
              f"{len(stale_pending)} validation run(s) for a superseded candidate are still pending")
    phase = _phase(state, journal["actions"], unit, current, native_projection)
    if (validates or purpose == "release") and kind != "remote_diagnostic" and phase in ("working", "converging"):
        pending = _pending_interventions(state, action["unit"], _unit_tasks(unit))
        if phase == "converging" and "unit:" + action["unit"] in pending:
            _reason(reasons, "correctness", "diagnosis_required",
                    "two remote validation failures were corrected without new evidence")
        else:
            _reason(reasons, "efficiency", "integration_unsettled",
                    "objective assignments are still outstanding" if phase == "working"
                    else "corrections are still unsettled")

    # 5. Readiness, or the diagnostic exception.
    if purpose == "validation" or purpose == "release":
        configured = list(governor_policy.get("preflight", []))
        if binding is not None:
            receipts = unit["preflight"].get(candidate_id, {})
            missing = [check for check in configured if check not in receipts]
            failed = sorted(check for check, receipt in receipts.items() if receipt.get("status") == "FAILED")
            unavailable = sorted(check for check, receipt in receipts.items() if receipt.get("status") == "UNAVAILABLE")
            if failed:
                _reason(reasons, "correctness", "blocking_findings",
                        "local checks failed on this candidate: " + ", ".join(failed))
            if missing:
                _reason(reasons, "efficiency", "local_preflight_missing", "missing: " + ", ".join(missing))
            if unavailable:
                _warn(warnings, "preflight_unavailable", "unavailable locally: " + ", ".join(unavailable))
            if not configured:
                _warn(warnings, "preflight_unconfigured", "no local preflight is configured in waste_governor.preflight")
        else:
            _warn(warnings, "context_unbound", "the candidate binds a commit only; prepare the unit for context binding")
    gaps = checkpoint.get("verification_gaps") or []
    if gaps:
        if purpose == "release":
            _reason(reasons, "correctness", "verification_gaps", f"{len(gaps)} verification gap(s) remain")
        else:
            _warn(warnings, "verification_gaps", f"{len(gaps)} verification gap(s) remain in the checkpoint")

    # 6. Repeated failure.
    if latest is not None and latest["outcome"] == "FAILED" and reuse is None and not superseded:
        classification = latest.get("classification")
        if latest.get("candidate_id") is None and latest.get("inputs") != _inputs(state):
            _warn(warnings, "necessary_rerun", "inputs changed since the previous attempt")
        elif classification is None:
            _reason(reasons, "correctness", "failure_unclassified",
                    f"attempt {latest['attempt']} failed and has not been classified")
        elif classification["class"] == "code_defect":
            _reason(reasons, "efficiency", "unchanged_rerun",
                    "the previous attempt failed on a code or configuration defect and the candidate is unchanged")
        elif classification["class"] == "remote_only":
            if kind == "remote_diagnostic":
                _reason(reasons, "efficiency", "repeat_diagnostic", "this diagnostic already ran unchanged")
            else:
                _reason(reasons, "efficiency", "remote_only_needs_diagnostic",
                        "the failure only reproduces remotely; a bounded diagnostic is the useful next step")
        elif classification["class"] == "transient":
            budget = int(governor_policy.get("transient_retries", 1))
            used = sum(1 for row in same if row["attempt"] > 1)
            if used < budget:
                _warn(warnings, "transient_retry", f"retry {used + 1} of {budget} after a transient failure")
            else:
                _reason(reasons, "efficiency", "transient_budget_exhausted",
                        f"{budget} transient retry(ies) were already spent on this candidate")
        else:
            _reason(reasons, "correctness", "external_blocker",
                    "the previous attempt was blocked by quota, authorization or an unavailable dependency")
    elif latest is not None and latest["outcome"] == "FAILED" and kind == "remote_diagnostic" and reuse is None and not superseded:
        pass
    elif kind == "remote_diagnostic" and latest is None:
        _warn(warnings, "early_diagnostic", "supplies information unavailable locally")

    return verdict(phase, stale_pending)


def _resolve(verdict: dict, exception: dict | None, mode: str) -> tuple[str, dict]:
    """DEFER on any reason. Only an efficiency deferral yields to a scoped exception or observe mode."""
    reasons = verdict["reasons"]
    applied = False
    ignored = None
    hard = [reason for reason in reasons if reason["class"] != "efficiency"]
    soft = [reason for reason in reasons if reason["class"] == "efficiency"]
    observed = False
    if soft and not hard:
        if exception is not None:
            applied = True
        elif mode == "observe":
            observed = True
        if applied or observed:
            for reason in soft:
                verdict["warnings"].append({"code": reason["code"], "detail": reason["detail"],
                                            "softened_by": "exception" if applied else "observe_mode"})
            verdict["reasons"] = []
            reasons = []
    elif hard and exception is not None:
        ignored = hard[0]["class"]
    decision = "DEFER" if reasons else ("REUSE" if verdict["reuse"] else "ALLOW")
    return decision, {"applied": applied, "ignored_because": ignored, "observed": observed}


def _explanation(decision: str, action: dict, verdict: dict, next_action: str | None) -> str:
    binding = verdict.get("binding")
    label = (f"{action['unit']}/g{binding['generation']} {binding['commit'][:12]}" if binding
             else f"{action['unit']} {action['candidate'][:12]}")
    lines = [f"{decision}: " + (", ".join(reason["code"] for reason in verdict["reasons"]) if verdict["reasons"]
                                 else (verdict["reuse"]["kind"] if verdict["reuse"] else "admitted")),
             f"Candidate: {label}", f"Requested: {action['kind']} {action['target']}"]
    if verdict["reasons"]:
        lines.append("Because:")
        lines.extend("  " + reason["detail"] for reason in verdict["reasons"])
    if verdict["warnings"]:
        lines.append("Noted: " + ", ".join(warning["code"] for warning in verdict["warnings"]))
    if next_action:
        lines.append("Next action: " + next_action)
    return "\n".join(lines)


def _count(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _admit(project: Path, objective: str, *, owner: str, action: dict, exception: dict | None,
           now: datetime, managed: bool = False, native_projection: dict | None = None) -> dict:
    """Evaluate one request and journal the decision, with the objective lock held."""
    bounded_text(owner, name="owner")
    proposal = validate_action(action)
    supplied = _validate_exception(exception)
    policy = effective(project)
    governor_policy = policy["policy"]["waste_governor"]
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = now.isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_journal(record_path)
        verdict = _evaluate(proposal, state, journal, governor_policy, managed=managed,
                            native_projection=native_projection)
        exception_record = _exception_grant(governor_policy, supplied, action=proposal, objective=objective,
                                            candidate_ids=verdict["candidate_ids"], now=now)
        decision, exception_result = _resolve(verdict, exception_record, governor_policy.get("mode", "enforce"))
        codes = [reason["code"] for reason in verdict["reasons"]]
        next_action = next((NEXT_ACTIONS[code] for code in codes if code in NEXT_ACTIONS), None)
        record_id = digest({"action": proposal, "at": moment, "revision": journal["revision"]})
        counters = journal["counters"]
        _count(counters["decisions"], decision)
        for code in codes:
            _count(counters["deferrals"], code)
        for warning in verdict["warnings"]:
            _count(counters["warnings"], warning["code"])
            if warning.get("softened_by") == "observe_mode":
                _count(counters, "observed_deferrals")
        if exception_result["applied"]:
            _count(counters, "exceptions_applied")
        executes = decision == "ALLOW"
        if executes:
            _compact(journal)
            row = {"record_id": record_id, "logical_key": verdict["logical_key"], "attempt": verdict["attempt"],
                   "action": {key: proposal[key] for key in ("kind", "candidate", "target", "reason", "unit",
                                                             "effects", "authorization", "diagnostic", "inputs")},
                   "candidate_id": verdict["candidate_id"], "commit": verdict["commit"],
                   "decision": decision, "phase": verdict["phase"],
                   "at": moment, "outcome": "pending", "inputs": _inputs(state),
                   "exception": exception_record,
                   "warnings": [warning["code"] for warning in verdict["warnings"]],
                   "reasons": [{key: reason[key] for key in ("class", "code", "detail")} for reason in verdict["reasons"]],
                   "purpose": verdict["purpose"], "cancel_safe": verdict["cancel_safe"],
                   "effects": verdict["effects"] or [],
                   "receipt": {"started_at": moment, "finished_at": None, "observed_elapsed_s": None,
                               "provider": None, "evidence": list(proposal["evidence"]), "detail": None},
                   "classification": None, "classifications": [], "derived_from": None}
            journal["actions"].append(row)
        elif decision == "REUSE":
            # Reuse is a decision about an existing row, not a new action; it is counted and
            # the reused row's identity is returned, and nothing is journaled that could be
            # mistaken later for a run that happened.
            record_id = verdict["reuse"]["record_id"]
            _count(counters, "attachments" if verdict["reuse"]["kind"] == "attach" else "evidence_reused")
        unit = journal["units"].get(proposal["unit"])
        if unit is not None:
            unit["last_decision"] = {"decision": decision, "kind": proposal["kind"], "target": proposal["target"],
                                     "codes": codes, "at": moment, "next_action": next_action,
                                     "record_id": record_id if executes else None}
        _write_journal(record_path, journal)
        result = {"schema": SCHEMA, "decision": decision, "mode": governor_policy.get("mode", "enforce"),
                  "phase": verdict["phase"], "unit": proposal["unit"], "purpose": verdict["purpose"],
                  "candidate": verdict["candidate_id"] or proposal["candidate"],
                  "generation": verdict["binding"]["generation"] if verdict["binding"] else None,
                  "effects": verdict["effects"], "effect_source": verdict["effect_source"],
                  "reasons": [{key: reason[key] for key in ("class", "code", "detail")} for reason in verdict["reasons"]],
                  "warnings": verdict["warnings"], "reuse": verdict["reuse"],
                  "next_action": next_action, "exception": exception_result,
                  "record_id": record_id, "recorded": executes, "executes": executes,
                  "superseded_validation": verdict["stale_pending"]}
        result["explanation"] = _explanation(decision, proposal, verdict, next_action)
        result["_binding"] = verdict["binding"]
        result["_unit"] = unit
        result["_policy"] = governor_policy
        result["_kind"] = proposal["kind"]
        return result


def decide(project: Path, objective: str, *, owner: str, action: dict, exception: object = None,
           now: datetime | None = None, native_projection: dict | None = None) -> dict:
    """Return ALLOW, REUSE or DEFER for one proposed expensive effect, and record it."""
    result = _admit(project, objective, owner=owner, action=action, exception=exception,
                    now=now or datetime.now(timezone.utc), native_projection=native_projection)
    return {key: value for key, value in result.items() if not key.startswith("_")}


# --------------------------------------------------------------------------- outcomes

def _find_row(journal: dict, record_id: str) -> dict:
    rows = [row for row in journal["actions"] if row["record_id"] == record_id]
    if not rows:
        raise PodError("unknown_governed_action", "No recorded action with that identity")
    return rows[-1]


def _validate_provider(value: object, *, nested: bool = True) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 16:
        raise PodError("invalid_provider", "Provider receipt is a small mapping")
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64:
            raise PodError("invalid_provider", "Provider receipt keys are short names")
        if isinstance(item, dict) and nested:
            _validate_provider(item, nested=key == "triggered")
        elif not (item is None or isinstance(item, (str, int, bool))) or (isinstance(item, str) and len(item) > 512):
            raise PodError("invalid_provider", "Provider receipt values are short scalars")
    return dict(value)


def _settle(journal: dict, row: dict, *, outcome: str, provider: dict | None, evidence: list[str],
            detail: str | None, moment: str) -> None:
    # pending and UNKNOWN are open states; PASS, FAILED and CANCELED are final and exact.
    if row["outcome"] not in ("pending", "UNKNOWN") and row["outcome"] != outcome:
        raise PodError("outcome_conflict", "A different outcome was already recorded")
    if row["outcome"] in ("pending", "UNKNOWN") and row["outcome"] != outcome:
        row["outcome"] = outcome
        receipt = row["receipt"]
        receipt["finished_at"] = moment
        try:
            started = datetime.fromisoformat(receipt["started_at"])
            finished = datetime.fromisoformat(moment)
            receipt["observed_elapsed_s"] = max(0.0, (finished - started).total_seconds())
        except (TypeError, ValueError):
            receipt["observed_elapsed_s"] = None
        _count(journal["counters"]["outcomes"], outcome)
    if provider is not None:
        row["receipt"]["provider"] = {**(row["receipt"].get("provider") or {}), **provider}
    if evidence:
        row["receipt"]["evidence"] = sorted(set(row["receipt"].get("evidence", [])) | set(evidence))[:16]
    if detail is not None:
        row["receipt"]["detail"] = detail
    if outcome == "PASS" and row["action"]["kind"] in PUBLICATION_KINDS:
        unit = journal["units"].get(row["action"]["unit"])
        if unit and unit.get("branch") and row.get("commit"):
            # What the branch carries is what the recorded pushes landed, whichever
            # generation they belonged to.
            unit["published"][unit["branch"]["remote"] + "/" + unit["branch"]["branch"]] = row["commit"]
        _derive_validation(journal, row, moment)


def _derive_validation(journal: dict, row: dict, moment: str) -> None:
    """Journal the validation a landed publication started, one row per triggered workflow.

    The run exists whether or not anyone dispatched it, so it must be visible to the same
    logical key a later dispatch would use; otherwise the dispatch doubles it. The executor
    supplies the run identities it read back; a manual outcome leaves them for readback.
    """
    if row.get("candidate_id") is None:
        return
    triggered = (row["receipt"].get("provider") or {}).get("triggered") or {}
    for effect in row.get("effects") or []:
        if not effect.startswith("workflow:"):
            continue
        workflow = effect[len("workflow:"):]
        action = {"kind": "workflow_dispatch", "candidate": row["candidate_id"], "target": workflow,
                  "reason": "started by " + row["action"]["kind"] + " " + row["record_id"][:12],
                  "unit": row["action"]["unit"], "effects": None, "authorization": None,
                  "diagnostic": None, "inputs": {}}
        key = _logical_key(action, row["candidate_id"])
        if any(other["logical_key"] == key and other["outcome"] in ("pending", "UNKNOWN", "PASS")
               for other in journal["actions"]):
            continue
        _compact(journal)
        run = triggered.get(workflow)
        journal["actions"].append({
            "record_id": digest({"derived": row["record_id"], "workflow": workflow}),
            "logical_key": key, "attempt": 1, "action": action, "candidate_id": row["candidate_id"],
            "commit": row.get("commit"), "decision": "ALLOW", "phase": row.get("phase"), "at": moment,
            "outcome": "pending", "inputs": row.get("inputs"), "exception": None,
            "warnings": ["derived_from_publication"], "reasons": [], "purpose": "validation",
            "cancel_safe": True, "effects": [effect],
            "receipt": {"started_at": row["receipt"]["started_at"], "finished_at": None,
                        "observed_elapsed_s": None, "provider": _provider_run(run) if run else None,
                        "evidence": [], "detail": None if run else "started by the publication; run not read back yet"},
            "classification": None, "classifications": [], "derived_from": row["record_id"]})


def record_outcome(project: Path, objective: str, *, owner: str, record_id: str, outcome: str,
                   provider: object = None, evidence: list[str] | None = None, detail: str | None = None,
                   now: datetime | None = None) -> dict:
    """Bind the real result of an allowed action so later decisions can reuse it."""
    bounded_text(owner, name="owner")
    bounded_text(record_id, name="record_id", limit=128)
    if outcome not in OUTCOMES or outcome == "pending":
        raise PodError("invalid_outcome", "Outcome must be PASS, FAILED, UNKNOWN or CANCELED")
    provider_record = _validate_provider(provider)
    references = list(evidence or [])
    if len(references) > 16:
        raise PodError("invalid_evidence", "Evidence references are a bounded list")
    for item in references:
        bounded_text(item, name="evidence", limit=512)
    if detail is not None:
        bounded_text(detail, name="detail", limit=1024)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        if row["decision"] != "ALLOW":
            raise PodError("invalid_outcome", "Only an admitted action carries an outcome")
        _settle(journal, row, outcome=outcome, provider=provider_record, evidence=references,
                detail=detail, moment=moment)
        _write_journal(record_path, journal)
        return {"status": "recorded", "record_id": record_id, "outcome": row["outcome"],
                "receipt": row["receipt"]}


def classify_failure(project: Path, objective: str, *, owner: str, record_id: str, classification: dict,
                     now: datetime | None = None) -> dict:
    """Say why a remote attempt failed before another expensive one is considered.

    A code or configuration defect is recorded as a correction through the ledger's own
    intervention rule, so a third equivalent correction needs a diagnosis exactly as a
    worker correction would.
    """
    bounded_text(owner, name="owner")
    bounded_text(record_id, name="record_id", limit=128)
    record = exact(classification, {"class", "reason", "correction", "diagnosis", "evidence"},
                   {"class", "reason"}, name="classification")
    if record["class"] not in FAILURE_CLASSES:
        raise PodError("invalid_classification", "Failure class must be code_defect, remote_only, transient or external")
    bounded_text(record["reason"], name="reason", limit=1024)
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 16:
        raise PodError("invalid_classification", "Evidence references are a bounded list")
    for item in evidence:
        bounded_text(item, name="evidence", limit=512)
    if record["class"] == "code_defect" and not isinstance(record.get("correction"), dict):
        raise PodError("invalid_classification", "A code defect classification carries a correction record")
    if record["class"] != "code_defect" and (record.get("correction") is not None or record.get("diagnosis") is not None):
        raise PodError("invalid_classification", "Only a code defect carries a correction or diagnosis")
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        if row["outcome"] != "FAILED":
            raise PodError("invalid_classification", "Only a failed attempt is classified")
        stored = {"class": record["class"], "reason": record["reason"], "evidence": list(evidence),
                  "at": moment, "correction_identity": None}
        if record["class"] == "code_defect":
            try:
                correction = _intervention_locked(project, objective, state, task=None,
                                                  unit=row["action"]["unit"],
                                                  correction=record["correction"],
                                                  diagnosis=record.get("diagnosis"))
            except PodError as exc:
                if exc.code == "diagnosis_required":
                    _count(journal["counters"], "failures_interrupted")
                    _write_journal(record_path, journal)
                raise
            stored["correction_identity"] = correction["correction_identity"]
            _write(context_path, state)
        row["classification"] = stored
        row["classifications"] = (row.get("classifications", []) + [stored])[-8:]
        _write_journal(record_path, journal)
        return {"status": "classified", "record_id": record_id, "classification": stored}


def record_correction(project: Path, objective: str, *, owner: str, unit: str, correction: dict,
                      diagnosis: dict | None = None) -> dict:
    """Record the correction behind a new candidate before it is validated.

    This is the third-correction path: once two remote failures were corrected without new
    evidence, the next candidate's correction is recorded here with the diagnosis that
    distinguishes it, and validation of that candidate is admitted again.
    """
    bounded_text(owner, name="owner")
    if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
        raise PodError("invalid_unit", "Delivery unit names are bounded identifiers")
    context_path = _path(project, objective)
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        result = _intervention_locked(project, objective, state, task=None, unit=unit,
                                      correction=correction, diagnosis=diagnosis)
        _write(context_path, state)
        return result


# --------------------------------------------------------------------------- candidates

def _git(project: Path, argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PodError("git_unavailable", "Git could not observe the candidate") from exc


def _bounded_mapping(value: object, *, name: str, limit: int = 16) -> dict:
    if value is None:
        return {}
    if (not isinstance(value, dict) or len(value) > limit
            or any(not isinstance(k, str) or not k or len(k) > 64 or not isinstance(v, str) or len(v) > 256
                   for k, v in value.items())):
        raise PodError("invalid_" + name, f"{name} is a bounded string mapping")
    return dict(value)


def observe_candidate(project: Path, *, base_ref: str | None = "origin/main",
                      workflows: list[str] | None = None, verification: list[str] | None = None,
                      toolchain: dict | None = None, environment: dict | None = None) -> dict:
    """Read the frozen source version and the context a validation of it would depend on.

    Commit and tree come from Git, never from the caller. Workflow files are digested by
    the same bounded source reader packets use, so a changed workflow changes the candidate
    even when the commit is unchanged in a different checkout.
    """
    from .records import source_identity

    head = _git(project, ["rev-parse", "--verify", "HEAD^{commit}"])
    tree = _git(project, ["rev-parse", "--verify", "HEAD^{tree}"])
    if head.returncode or tree.returncode or not _GIT_ID.fullmatch(head.stdout.strip()) \
            or not _GIT_ID.fullmatch(tree.stdout.strip()) or len(head.stdout.strip()) != len(tree.stdout.strip()):
        raise PodError("candidate_unobservable", "HEAD is not a committed Git object here")
    status = _git(project, ["status", "--porcelain", "--untracked-files=no"])
    if status.returncode:
        raise PodError("candidate_unobservable", "Git status is unavailable")
    dirty = len([line for line in status.stdout.splitlines() if line.strip()])
    base = None
    if base_ref is not None:
        bounded_text(base_ref, name="base_ref", limit=256)
        if not _REF.fullmatch(base_ref):
            raise PodError("invalid_base_ref", "Base reference is not a plain Git reference")
        resolved = _git(project, ["rev-parse", "--verify", base_ref + "^{commit}"])
        if resolved.returncode == 0 and _GIT_ID.fullmatch(resolved.stdout.strip()):
            merge_base = _git(project, ["merge-base", "HEAD", resolved.stdout.strip()])
            base = {"ref": base_ref, "commit": resolved.stdout.strip(),
                    "merge_base": merge_base.stdout.strip() if merge_base.returncode == 0 else None}
        else:
            base = {"ref": base_ref, "commit": None, "merge_base": None}
    paths = list(workflows or [])
    if len(paths) > MAX_WORKFLOWS:
        raise PodError("invalid_workflows", "Workflow paths are a bounded list")
    digests = {}
    for path in paths:
        bounded_text(path, name="workflow", limit=512)
        observed = source_identity(project, path)
        if observed["state"] != "present":
            raise PodError("workflow_missing", f"Workflow {path} is {observed['state']}")
        digests[path] = observed["sha256"]
    commands = list(verification or [])
    if len(commands) > 32:
        raise PodError("invalid_verification", "Verification commands are a bounded list")
    for command in commands:
        bounded_text(command, name="verification", limit=512)
    tools = {"python": platform.python_version()} | _bounded_mapping(toolchain, name="toolchain")
    return {"schema": "pod-candidate-observation/v1", "commit": head.stdout.strip(), "tree": tree.stdout.strip(),
            "dirty_paths": dirty, "base": base, "workflows": digests, "verification": commands,
            "toolchain": tools, "environment": _bounded_mapping(environment, name="environment"),
            "policy_revision": effective(project)["revision"]}


def _validate_observation(value: object) -> dict:
    fields = {"schema", "commit", "tree", "dirty_paths", "base", "workflows", "verification",
              "toolchain", "environment", "policy_revision"}
    record = exact(value, fields, fields, name="observation")
    if record["schema"] != "pod-candidate-observation/v1":
        raise PodError("invalid_observation", "Unsupported candidate observation schema")
    if (not isinstance(record["commit"], str) or not _GIT_ID.fullmatch(record["commit"])
            or not isinstance(record["tree"], str) or not _GIT_ID.fullmatch(record["tree"])
            or len(record["commit"]) != len(record["tree"])):
        raise PodError("invalid_observation", "Candidate commit and tree must be Git object ids of one format")
    if type(record["dirty_paths"]) is not int or record["dirty_paths"] < 0:
        raise PodError("invalid_observation", "Dirty path count must be a non-negative integer")
    base = record["base"]
    if base is not None:
        exact(base, {"ref", "commit", "merge_base"}, {"ref", "commit", "merge_base"}, name="base")
        bounded_text(base["ref"], name="base_ref", limit=256)
        for key in ("commit", "merge_base"):
            if base[key] is not None and (not isinstance(base[key], str) or not _GIT_ID.fullmatch(base[key])):
                raise PodError("invalid_observation", "Base identities must be Git object ids")
    workflows = record["workflows"]
    if (not isinstance(workflows, dict) or len(workflows) > MAX_WORKFLOWS
            or any(not isinstance(k, str) or not k or len(k) > 512 or not isinstance(v, str)
                   or len(v) != 64 or any(ch not in "0123456789abcdef" for ch in v) for k, v in workflows.items())):
        raise PodError("invalid_observation", "Workflow digests are a bounded path-to-sha256 mapping")
    if not isinstance(record["verification"], list) or len(record["verification"]) > 32:
        raise PodError("invalid_observation", "Verification commands are a bounded list")
    for command in record["verification"]:
        bounded_text(command, name="verification", limit=512)
    _bounded_mapping(record["toolchain"], name="toolchain")
    _bounded_mapping(record["environment"], name="environment")
    bounded_text(record["policy_revision"], name="policy_revision", limit=128)
    return record


def _validate_branch(value: object) -> dict | None:
    if value is None:
        return None
    record = exact(value, {"remote", "branch", "base"}, {"remote", "branch", "base"}, name="branch")
    for key in ("remote", "branch", "base"):
        if not isinstance(record[key], str) or not _REF.fullmatch(record[key]) or ".." in record[key]:
            raise PodError("invalid_branch", f"Branch {key} is not a plain Git name")
    return dict(record)


def candidate_identity(unit: str, observation: dict) -> str:
    return digest({"unit": unit, "commit": observation["commit"], "tree": observation["tree"],
                   "base": observation["base"]["commit"] if observation["base"] else None,
                   "workflows": observation["workflows"], "verification": observation["verification"],
                   "toolchain": observation["toolchain"], "environment": observation["environment"],
                   "policy_revision": observation["policy_revision"]})


def prepare_candidate(project: Path, objective: str, *, owner: str, unit: str, observation: dict,
                      branch: dict | None = None, tasks: list[str] | None = None,
                      now: datetime | None = None) -> dict:
    """Freeze one candidate generation for a delivery unit at an explicit boundary.

    Identical bound inputs return the existing generation; changed inputs open a new one
    and name the superseded validation that is still pending, so the executor can cancel
    it when policy allows. Every edit is not a generation; a prepared candidate is.
    """
    bounded_text(owner, name="owner")
    if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
        raise PodError("invalid_unit", "Delivery unit names are bounded identifiers")
    record = _validate_observation(observation)
    branch_record = _validate_branch(branch)
    task_list = list(tasks or [])
    if len(task_list) > 32:
        raise PodError("invalid_tasks", "Unit tasks are a bounded list")
    for task in task_list:
        bounded_text(task, name="task", limit=128)
    identity = candidate_identity(unit, record)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_journal(record_path)
        existing = journal["units"].get(unit)
        if existing is None:
            if len(journal["units"]) >= MAX_UNITS:
                raise PodError("governor_full", "Too many delivery units; start a new objective record")
            existing = {"name": unit, "branch": None, "tasks": [], "generation": 0, "candidate": None,
                        "history": [], "preflight": {}, "published": {}, "last_decision": None}
            journal["units"][unit] = existing
        if branch_record is not None:
            existing["branch"] = branch_record
        if tasks is not None:
            existing["tasks"] = task_list
        previous = existing.get("candidate")
        status = "unchanged"
        superseded = []
        if previous is not None and previous["id"] == identity and previous["dirty_paths"] != record["dirty_paths"]:
            # Same bound inputs, different working-tree state: the candidate did not change,
            # but whether it is frozen did. Only a fresh observation can say so.
            previous["dirty_paths"] = record["dirty_paths"]
            previous["prepared_at"] = moment
            status = "refreshed"
        if previous is None or previous["id"] != identity:
            status = "prepared"
            existing["generation"] += 1
            binding = {"schema": "pod-candidate/v1", "unit": unit, "generation": existing["generation"],
                       "id": identity, "commit": record["commit"], "tree": record["tree"],
                       "dirty_paths": record["dirty_paths"], "base": record["base"],
                       "workflows": record["workflows"], "verification": record["verification"],
                       "toolchain": record["toolchain"], "environment": record["environment"],
                       "policy_revision": record["policy_revision"], "prepared_at": moment,
                       "supersedes": previous["id"] if previous else None}
            existing["candidate"] = binding
            existing["history"] = (existing["history"] + [identity])[-MAX_HISTORY:]
            keep = set(existing["history"][-2:])
            existing["preflight"] = {cid: receipts for cid, receipts in existing["preflight"].items() if cid in keep}
            superseded = [row["record_id"] for row in journal["actions"]
                          if row["action"]["unit"] == unit and row["decision"] == "ALLOW"
                          and row["outcome"] == "pending" and row.get("cancel_safe")
                          and row.get("candidate_id") != identity]
        _write_journal(record_path, journal)
        return {"status": status, "unit": unit, "candidate": existing["candidate"],
                "superseded_validation": superseded, "branch": existing["branch"], "tasks": existing["tasks"]}


def record_preflight(project: Path, objective: str, *, owner: str, unit: str, candidate: str,
                     check: str, status: str, report: str | None = None,
                     now: datetime | None = None) -> dict:
    """Bind one local check result to the unit's current candidate generation."""
    bounded_text(owner, name="owner")
    bounded_text(check, name="check", limit=128)
    bounded_text(candidate, name="candidate", limit=128)
    if status not in PREFLIGHT_STATUSES:
        raise PodError("invalid_preflight", "Preflight status must be PASS, FAILED or UNAVAILABLE")
    if report is not None:
        bounded_text(report, name="report", limit=512)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_journal(record_path)
        record = journal["units"].get(unit)
        binding = record.get("candidate") if record else None
        if binding is None:
            raise PodError("unit_unbound", "Prepare the unit's candidate before recording preflight")
        if candidate not in (binding["id"], binding["commit"]):
            raise PodError("superseded_candidate", "Preflight names a candidate the unit has superseded")
        receipts = record["preflight"].setdefault(binding["id"], {})
        if len(receipts) >= 32 and check not in receipts:
            raise PodError("governor_full", "Too many preflight checks for one candidate")
        receipts[check] = {"status": status, "at": moment, "report": report}
        _write_journal(record_path, journal)
        return {"status": "recorded", "unit": unit, "candidate": binding["id"], "check": check,
                "result": status}


# --------------------------------------------------------------------------- execution

def _uncertain(exc: PodError) -> bool:
    return exc.code in ("remote_effect_uncertain", "remote_read_failed")


def _outcome_from_run(run: dict) -> tuple[str, str | None]:
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status != "completed":
        return "pending", None
    if conclusion == "success":
        return "PASS", None
    if conclusion == "cancelled":
        return "CANCELED", None
    if conclusion == "skipped":
        return "FAILED", "skipped: a skipped required check leaves the check pending, not passed"
    return "FAILED", str(conclusion) if conclusion is not None else "completed without a conclusion"


def _provider_run(run: dict) -> dict:
    return {"run_id": str(run.get("id")), "url": run.get("url"), "status": run.get("status"),
            "conclusion": run.get("conclusion"), "created_at": run.get("created_at")}


def _pick_run(runs: list[dict], *, since: str) -> dict | None:
    """The newest run of this workflow for this commit created at or after the submission."""
    try:
        # GitHub reports run creation to the second; the floor must not be finer.
        floor = datetime.fromisoformat(since).replace(microsecond=0)
    except (TypeError, ValueError):
        floor = None
    candidates = []
    for run in runs:
        created = run.get("created_at")
        try:
            stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
        except ValueError:
            stamp = None
        if floor is not None and stamp is not None and stamp.tzinfo is not None and floor.tzinfo is not None \
                and stamp < floor:
            continue
        candidates.append((stamp or datetime.min.replace(tzinfo=timezone.utc), run))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _perform(port: GitHubPort, row: dict, unit: dict, journal: dict,
             pull_request: dict | None) -> tuple[str, dict | None, str | None]:
    """Perform the admitted action against the row's own bound commit. Returns outcome, provider, detail."""
    kind = row["action"]["kind"]
    branch = unit.get("branch")
    commit = row.get("commit")
    if branch is None and kind != "cancel_validation":
        raise PodError("unit_branch_required", "Managed execution needs the unit's remote, branch and base")
    if commit is None and kind != "cancel_validation":
        raise PodError("candidate_unbound", "The admitted row carries no candidate commit")
    if kind in PUBLICATION_KINDS:
        target = branch["remote"] + "/" + branch["branch"]
        if row["action"]["target"] != target:
            raise PodError("target_mismatch", f"Publication target must be {target}")
        expected = unit["published"].get(target)
        try:
            pushed = port.push(remote=branch["remote"], branch=branch["branch"], commit=commit, expected=expected)
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", None, "push response was lost"
            raise
        if pushed.get("status") != "pushed":
            return "FAILED", {"remote_head": pushed.get("remote_head")}, "branch_moved: the remote branch was not at the expected commit"
        provider = {"remote_head": commit, "branch": target}
        provider["triggered"] = _triggered_runs(port, row, commit)
        if kind == "push":
            return "PASS", provider, None
        try:
            found = port.pull_request(head=branch["branch"], base=branch["base"])
            if found is None:
                title = (pull_request or {}).get("title") or row["action"]["reason"]
                body = (pull_request or {}).get("body") or ""
                bounded_text(title, name="title", limit=256)
                if len(body) > 16 * 1024:
                    raise PodError("invalid_pull_request", "Pull request body is too large")
                found = port.open_pull_request(head=branch["branch"], base=branch["base"], title=title, body=body)
                provider["pr_reused"] = False
            else:
                provider["pr_reused"] = True
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", provider, "pull request state is unknown after the push"
            raise
        provider.update({"pr": found.get("number"), "pr_url": found.get("url")})
        return "PASS", provider, None
    if kind in DISPATCH_KINDS:
        workflow = row["action"]["target"]
        if not _WORKFLOW.fullmatch(workflow):
            raise PodError("invalid_workflow", "Dispatch target must be a workflow file name")
        try:
            port.dispatch(workflow=workflow, ref=branch["branch"], inputs=row["action"].get("inputs") or {})
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", None, "dispatch response was lost"
            raise
        # A validation run must sit on the candidate commit; a diagnostic ran at whatever the
        # branch carried, which the recorded pushes know. The commit looked up is recorded so
        # a later readback looks in the same place.
        if kind == "remote_diagnostic":
            commit = unit["published"].get(branch["remote"] + "/" + branch["branch"]) or commit
        lookup = {"ref_commit": commit}
        try:
            run = _pick_run(port.runs(workflow=workflow, commit=commit), since=row["receipt"]["started_at"])
        except PodError as exc:
            if _uncertain(exc):
                return "pending", lookup, "dispatched; run identity not read back yet"
            raise
        if run is None:
            return "pending", lookup, "dispatched; no run visible yet"
        return "pending", {**lookup, **_provider_run(run)}, None
    if kind == "validation_rerun":
        failed = [other for other in journal["actions"]
                  if other["action"]["unit"] == unit["name"] and other["decision"] == "ALLOW"
                  and other["outcome"] == "FAILED" and other["action"]["target"] == row["action"]["target"]
                  and other.get("candidate_id") == row.get("candidate_id")
                  and (other["receipt"].get("provider") or {}).get("run_id")]
        if not failed:
            raise PodError("rerun_unbound", "No failed run with a provider identity binds this candidate and workflow")
        run_id = str(failed[-1]["receipt"]["provider"]["run_id"])
        try:
            port.rerun(run_id=run_id, failed_only=True)
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", {"run_id": run_id}, "rerun response was lost"
            raise
        return "pending", {"run_id": run_id, "rerun": True}, None
    if kind == "cancel_validation":
        target = _find_row(journal, row["action"]["target"])
        run_id = (target["receipt"].get("provider") or {}).get("run_id")
        if target["outcome"] != "pending" or not target.get("cancel_safe") or not run_id:
            raise PodError("cancel_unsafe", "Only a pending, cancel-safe validation with a run identity is canceled")
        try:
            port.cancel(run_id=str(run_id))
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", {"run_id": str(run_id)}, "cancel response was lost"
            raise
        return "PASS", {"run_id": str(run_id), "canceled_record": target["record_id"]}, None
    raise PodError("invalid_action", f"{kind} is decided by the governor but performed by project governance")


def _triggered_runs(port: GitHubPort, row: dict, commit: str) -> dict:
    """The runs a landed publication started, by workflow, as far as a readback can see them."""
    triggered = {}
    for effect in row.get("effects") or []:
        if not effect.startswith("workflow:"):
            continue
        workflow = effect[len("workflow:"):]
        try:
            run = _pick_run(port.runs(workflow=workflow, commit=commit), since=row["receipt"]["started_at"])
        except PodError as exc:
            if _uncertain(exc):
                continue
            raise
        if run is not None:
            triggered[workflow] = run
    return triggered


def _record_execution(project: Path, objective: str, *, owner: str, record_id: str, outcome: str,
                      provider: dict | None, detail: str | None, now: datetime) -> dict:
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = now.isoformat()
    with _lock(context_path):
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        if outcome == "pending":
            # A submission whose response was lost and whose run is now visible is running,
            # not unknown; the readback settles the identity, never a resubmission.
            if row["outcome"] == "UNKNOWN":
                row["outcome"] = "pending"
            if provider is not None:
                row["receipt"]["provider"] = {**(row["receipt"].get("provider") or {}), **provider}
            if detail is not None:
                row["receipt"]["detail"] = detail
        else:
            _settle(journal, row, outcome=outcome, provider=provider, evidence=[], detail=detail, moment=moment)
            if outcome == "PASS" and row["action"]["kind"] == "cancel_validation":
                target = _find_row(journal, row["action"]["target"])
                if target["outcome"] == "pending":
                    _settle(journal, target, outcome="CANCELED", provider=None, evidence=[],
                            detail="superseded by candidate " + str(row.get("candidate_id"))[:12], moment=moment)
                    _count(journal["counters"], "cancellations")
        _write_journal(record_path, journal)
        return dict(row)


def execute(project: Path, objective: str, *, owner: str, action: dict, exception: object = None,
            port: GitHubPort | None = None, pull_request: dict | None = None,
            now: datetime | None = None, native_projection: dict | None = None) -> dict:
    """Admit and perform one action as a single managed step.

    The decision and the execution are one path: the journal records the admitted
    intention before the remote call, the call targets the bound candidate commit rather
    than whatever HEAD points to, and a lost response leaves an UNKNOWN row that only a
    provider readback can settle. The lock is never held across the remote call.
    """
    moment = now or datetime.now(timezone.utc)
    if validate_action(action)["kind"] not in EXECUTABLE_KINDS:
        raise PodError("invalid_action", "Merge, release and deployment are decided here and performed by project governance")
    admitted = _admit(project, objective, owner=owner, action=action, exception=exception, now=moment,
                      managed=True, native_projection=native_projection)
    result = {key: value for key, value in admitted.items() if not key.startswith("_")}
    result["cancellations"] = []
    if admitted["decision"] != "ALLOW":
        return result
    unit = admitted["_unit"]
    binding = admitted["_binding"]
    remote = port or GhPort(project)
    policy = admitted["_policy"]
    later = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
    kind = admitted["_kind"]
    # A cancellation never cancels on its own behalf; supersedence is handled once, by the
    # action that follows it.
    if (kind != "cancel_validation" and policy.get("cancel_superseded_validation")
            and admitted["superseded_validation"]):
        journal = _read_journal(_record_path(project, objective))
        for stale in admitted["superseded_validation"]:
            stale_row = _find_row(journal, stale)
            if not (stale_row["receipt"].get("provider") or {}).get("run_id"):
                # Nothing to cancel remotely; the row settles by readback or stays superseded.
                result["cancellations"].append({"target": stale, "record_id": None, "decision": "DEFER",
                                                "outcome": None, "detail": "no run identity to cancel"})
                continue
            try:
                canceled = execute(
                    project, objective, owner=owner, port=remote, now=moment,
                    native_projection=native_projection,
                    action={"kind": "cancel_validation", "unit": unit["name"], "candidate": binding["id"],
                            "target": stale, "reason": "superseded by a newer candidate generation"})
                result["cancellations"].append({**canceled, "target": stale})
            except PodError as exc:
                # The cancel's own row is settled by its execute; the primary action proceeds.
                result["cancellations"].append({"target": stale, "record_id": None, "decision": "ALLOW",
                                                "outcome": "FAILED", "detail": exc.code})
            except BaseException:
                _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                                  outcome="FAILED", provider=None, detail="not_attempted", now=later())
                raise
    journal = _read_journal(_record_path(project, objective))
    row = _find_row(journal, admitted["record_id"])
    try:
        outcome, provider, detail = _perform(remote, row, unit, journal, pull_request)
    except PodError as exc:
        _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                          outcome="UNKNOWN" if _uncertain(exc) else "FAILED", provider=None,
                          detail=exc.code, now=later())
        raise
    except BaseException as exc:
        # A port that answered in an unexpected shape, or a defect here, leaves the remote
        # state unknown; the row must say so rather than stay pending forever.
        _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                          outcome="UNKNOWN", provider=None, detail=type(exc).__name__, now=later())
        raise
    settled = _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                                outcome=outcome, provider=provider, detail=detail, now=later())
    result.update({"outcome": settled["outcome"], "receipt": settled["receipt"]})
    return result


def reconcile(project: Path, objective: str, *, owner: str, record_id: str, port: GitHubPort | None = None,
              now: datetime | None = None) -> dict:
    """Settle an UNKNOWN or pending row from provider readback only; never resubmit."""
    bounded_text(owner, name="owner")
    bounded_text(record_id, name="record_id", limit=128)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    state = _read(context_path)
    if state["owner"] not in (None, owner):
        raise PodError("coordinator_conflict", "Another coordinator owns this objective")
    journal = _read_journal(record_path)
    row = _find_row(journal, record_id)
    if row["outcome"] not in ("UNKNOWN", "pending"):
        return {"status": row["outcome"], "record_id": record_id, "action": "none", "receipt": row["receipt"]}
    unit = journal["units"].get(row["action"]["unit"])
    if unit is None:
        raise PodError("unit_unbound", "No delivery unit binds this row")
    # The row carries the commit it was admitted for. A superseded generation is looked up
    # by that commit, never by whatever the unit's current candidate happens to be.
    commit = row.get("commit")
    remote = port or GhPort(project)
    kind = row["action"]["kind"]
    provider = row["receipt"].get("provider") or {}
    moment = now or datetime.now(timezone.utc)
    if kind in PUBLICATION_KINDS:
        branch = unit["branch"]
        head = remote.branch_head(remote=branch["remote"], branch=branch["branch"])
        if commit is not None and head == commit:
            outcome, detail = "PASS", "remote branch carries the candidate commit"
            provider = {**provider, "remote_head": head, "branch": branch["remote"] + "/" + branch["branch"],
                        "triggered": _triggered_runs(remote, row, commit)}
        elif head is not None and head == unit["published"].get(branch["remote"] + "/" + branch["branch"]):
            outcome, detail = "FAILED", "not_pushed: the remote branch is still at the previously recorded commit"
        else:
            outcome, detail = "UNKNOWN", "remote branch head is " + (head[:12] if head else "absent")
        settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                    provider=provider, detail=detail, now=moment) if outcome != "UNKNOWN" else row
        return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
    if kind in DISPATCH_KINDS or kind == "validation_rerun":
        run_id = provider.get("run_id")
        lookup = provider.get("ref_commit") or commit
        if run_id:
            run = remote.run(run_id=str(run_id))
        elif lookup is None:
            return {"status": row["outcome"], "record_id": record_id, "action": "hold",
                    "reason": "the row carries neither a run identity nor a commit to look up",
                    "receipt": row["receipt"]}
        else:
            run = _pick_run(remote.runs(workflow=row["action"]["target"], commit=lookup),
                            since=row["receipt"]["started_at"])
        if run is None:
            return {"status": "UNKNOWN", "record_id": record_id, "action": "hold",
                    "reason": "no run for this workflow and commit is visible since the submission",
                    "receipt": row["receipt"]}
        outcome, detail = _outcome_from_run(run)
        settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                    provider={**({"ref_commit": lookup} if lookup else {}), **_provider_run(run)},
                                    detail=detail, now=moment)
        return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
    if kind == "cancel_validation":
        run_id = provider.get("run_id")
        if not run_id:
            return {"status": "UNKNOWN", "record_id": record_id, "action": "hold", "receipt": row["receipt"]}
        run = remote.run(run_id=str(run_id))
        status = run.get("status")
        if status == "completed":
            outcome = "PASS" if run.get("conclusion") == "cancelled" else "FAILED"
            settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                        provider=_provider_run(run), detail=None, now=moment)
            return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
        return {"status": "UNKNOWN", "record_id": record_id, "action": "hold", "receipt": row["receipt"]}
    raise PodError("invalid_action", f"{kind} has no provider readback")


# --------------------------------------------------------------------------- projection

def enforcement(policy: dict | None = None, project: Path | None = None) -> dict:
    """What the governor's controls actually are on this host. It never claims a sandbox.

    A skill cannot restrict a shell that holds the same credentials. Enforcement beyond
    advisory exists only where the host restricts the mutation routes, which is an owner
    declaration Pod records at the owner-configuration tier, never a control it proves.
    """
    effective_policy = policy if policy is not None else effective(project)
    governor_policy = effective_policy["policy"]["waste_governor"]
    declared = governor_policy.get("host_control")
    mode = governor_policy.get("mode", "enforce")
    level = "declared" if declared and mode == "enforce" else "advisory"
    return {"level": level, "mode": mode,
            "tier": "owner_route_config" if declared else "unavailable",
            "host_control": declared,
            "ungoverned_routes": "unknown",
            "detail": ("the owner declares that this host restricts remote mutation to the governed path; "
                       "Pod records that declaration and cannot prove it" if declared else
                       "workers and shells with the same credentials can bypass the governed path; "
                       "decisions are advisory until the host restricts those routes")}


def discover_triggers(project: Path) -> dict:
    """Propose a trigger mapping from the repository's workflow files. Read-only, never policy."""
    root = project / ".github" / "workflows"
    proposal = {"schema": "pod-trigger-proposal/v1", "proposal": True, "triggers": {}, "workflows": {},
                "reason": None}
    if not root.is_dir():
        proposal["reason"] = "no_workflows"
        return proposal
    paths = sorted(path for path in root.iterdir() if path.suffix in (".yml", ".yaml") and path.is_file()
                   and not path.is_symlink())[:MAX_WORKFLOWS]
    triggers: dict[str, set[str]] = {"push": set(), "pr_update": set()}
    for path in paths:
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError("oversized")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError):
            proposal["workflows"][path.name] = {"error": "unreadable"}
            continue
        if not isinstance(document, dict):
            proposal["workflows"][path.name] = {"error": "not_a_workflow"}
            continue
        events = document.get("on", document.get(True))
        if isinstance(events, str):
            events = {events: None}
        elif isinstance(events, list):
            events = {name: None for name in events if isinstance(name, str)}
        elif not isinstance(events, dict):
            events = {}
        record = {"events": sorted(str(name) for name in events),
                  "dispatchable": "workflow_dispatch" in events,
                  "merge_queue": "merge_group" in events,
                  "concurrency": isinstance(document.get("concurrency"), (dict, str)),
                  "cancel_in_progress": isinstance(document.get("concurrency"), dict)
                  and bool(document["concurrency"].get("cancel-in-progress")),
                  "path_filtered": any(isinstance(spec, dict) and ("paths" in spec or "paths-ignore" in spec)
                                       for spec in events.values())}
        if "push" in events:
            triggers["push"].add("workflow:" + path.name)
            spec = events["push"]
            if isinstance(spec, dict) and spec.get("branches") is not None:
                record["push_branches"] = spec["branches"] if isinstance(spec["branches"], list) else [spec["branches"]]
        if "pull_request" in events or "pull_request_target" in events:
            triggers["pr_update"].add("workflow:" + path.name)
        proposal["workflows"][path.name] = record
    proposal["triggers"] = {kind: sorted(values) for kind, values in triggers.items() if values}
    if not proposal["workflows"]:
        proposal["reason"] = "no_workflows"
    return proposal


def _unit_projection(unit: dict, actions: list[dict]) -> dict:
    binding = unit.get("candidate")
    rows = [row for row in actions if row["action"]["unit"] == unit["name"] and row["decision"] == "ALLOW"]
    active = [{"record_id": row["record_id"], "kind": row["action"]["kind"], "target": row["action"]["target"],
               "provider": row["receipt"].get("provider")} for row in rows if row["outcome"] == "pending"]
    unresolved = [row["record_id"] for row in rows if row["outcome"] == "UNKNOWN"]
    last = unit.get("last_decision")
    return {"generation": unit["generation"], "branch": unit.get("branch"), "tasks": unit.get("tasks", []),
            "candidate": ({key: binding[key] for key in ("id", "commit", "tree", "dirty_paths", "prepared_at",
                                                          "policy_revision")}
                          | {"base": binding["base"]["commit"] if binding.get("base") else None,
                             "workflows": sorted(binding["workflows"])}) if binding else None,
            "preflight": {check: receipt["status"] for check, receipt in
                          unit["preflight"].get(binding["id"], {}).items()} if binding else {},
            "published": unit.get("published", {}),
            "last_decision": last,
            "blocker": (last.get("codes") or None) if last and last.get("decision") == "DEFER" else None,
            "next_action": last.get("next_action") if last else None,
            "active_validation": active, "unresolved": unresolved}


def status_at(root: Path, *, project: Path | None = None,
              native_projection: dict | None = None) -> dict:
    """Read-only projection of one objective's governor state from its private directory."""
    context_path = root / "context.json"
    state = _read(context_path) if context_path.exists() else {
        "admissions": {}, "interventions": {}, "checkpoint": None}
    journal = _read_journal(root / "governor.json")
    policy = effective(project) if project is not None else None
    units = {name: _unit_projection(unit, journal["actions"]) for name, unit in journal["units"].items()}
    result = {"schema": SCHEMA,
              "phase": _phase(state, journal["actions"], None, _checkpoint(state).get("candidate"),
                              native_projection),
              "active_admissions": _active(native_projection),
              "pending_diagnosis": _pending_interventions(state), "units": units,
              "counters": journal["counters"],
              "actions": [{key: row[key] for key in ("record_id", "decision", "outcome", "at", "attempt")}
                          | {"kind": row["action"]["kind"], "candidate": row.get("candidate_id") or row["action"]["candidate"],
                             "unit": row["action"]["unit"], "target": row["action"]["target"],
                             "commit": row.get("commit"), "derived_from": row.get("derived_from"),
                             "provider": row["receipt"].get("provider")}
                          for row in journal["actions"][-16:]]}
    if policy is not None:
        result["enforcement"] = enforcement(policy)
        result["mode"] = policy["policy"]["waste_governor"].get("mode", "enforce")
        result["configured"] = {"preflight": policy["policy"]["waste_governor"].get("preflight", []),
                                "triggers": policy["policy"]["waste_governor"].get("triggers", {})}
        if project is not None and not result["configured"]["triggers"]:
            result["trigger_proposal"] = discover_triggers(project)
    return result


def status(project: Path, objective: str, *, native_projection: dict | None = None) -> dict:
    """Read-only projection for status and reporting."""
    return status_at(objective_root(project, objective), project=project,
                     native_projection=native_projection)


# --------------------------------------------------------------------------- GitHub port

from .github import GhPort  # noqa: E402  (the port is thin and imports nothing from here)

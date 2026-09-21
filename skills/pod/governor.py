"""A bounded decision check before an expensive Pod-mediated effect.

It reuses the records the objective already keeps. There is no background model,
no cost model, no daemon and no scoring: every decision is a deterministic
function of durable state plus the action being proposed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .errors import PodError
from .ledger import _lock, _path, _read, objective_root
from .util import atomic_json, bounded_json, bounded_text, digest, exact

KINDS = ("push", "pr_update", "workflow_dispatch", "merge", "release", "deploy",
         "remote_diagnostic", "validation_rerun")
AUTHORIZED_KINDS = ("merge", "release", "deploy")
VALIDATION_KINDS = ("workflow_dispatch", "validation_rerun")
LOCAL_KINDS = ("push", "pr_update")
OUTCOMES = ("pending", "PASS", "FAILED", "UNKNOWN")
CLASSES = ("authorization", "correctness", "efficiency")
SETTLED_CLEANUP = ("released", "already_released", "retained")
MAX_ACTIONS = 256
AUTHORIZATION_FIELDS = {"schema", "candidate", "tree", "scope", "authorized_by", "utc", "reference"}


def _record_path(project: Path, objective: str) -> Path:
    return objective_root(project, objective) / "governor.json"


def _read_actions(path: Path) -> dict:
    if not path.exists():
        return {"schema": "pod-governor/v1", "revision": 0, "actions": []}
    value = bounded_json(path)
    exact(value, {"schema", "revision", "actions"}, {"schema", "revision", "actions"}, name="governor")
    if value["schema"] != "pod-governor/v1":
        raise PodError("state_migration_required", "Governor schema requires explicit migration")
    return value


def validate_authorization(value: object, *, candidate: str | None = None,
                           kind: str | None = None) -> dict | None:
    """Accept only a complete owner authorization record bound to this candidate."""
    if value is None:
        return None
    record = exact(value, AUTHORIZATION_FIELDS, AUTHORIZATION_FIELDS, name="authorization")
    if record["schema"] != "pod-release-authorization/v1":
        raise PodError("invalid_authorization", "Authorization schema is unsupported")
    for key in ("candidate", "tree", "authorized_by", "utc", "reference"):
        bounded_text(record[key], name=key, limit=256)
    scope = record["scope"]
    if (not isinstance(scope, list) or not scope or len(scope) > 16
            or any(item not in KINDS for item in scope)):
        raise PodError("invalid_authorization", "Authorization scope is unsupported")
    if candidate is not None and record["candidate"] != candidate:
        return None
    if kind is not None and kind not in scope:
        return None
    return record


def validate_action(value: object) -> dict:
    action = exact(value, {"kind", "candidate", "target", "reason", "diagnostic_value", "authorization"},
                   {"kind", "candidate", "target", "reason"}, name="action")
    if action["kind"] not in KINDS:
        raise PodError("invalid_action", "Unsupported governed action kind")
    for key in ("candidate", "target", "reason"):
        bounded_text(action[key], name=key, limit=512)
    value_text = action.get("diagnostic_value")
    if value_text is not None:
        bounded_text(value_text, name="diagnostic_value", limit=512)
    if action["kind"] != "remote_diagnostic" and value_text:
        raise PodError("invalid_action", "Only a remote diagnostic carries diagnostic value")
    return action


def _validate_override(value: object) -> dict | None:
    if value is None:
        return None
    override = exact(value, {"reason", "by"}, {"reason", "by"}, name="override")
    for key in ("reason", "by"):
        bounded_text(override[key], name=key, limit=512)
    return override


def _active(state: dict) -> list[str]:
    active = []
    for key, effect in state.get("effects", {}).items():
        if not isinstance(effect, dict) or effect.get("state") not in ("reserved", "uncertain", "confirmed"):
            continue
        dispatch = (effect.get("native_binding") or {}).get("dispatchId")
        cleanup = state.get("cleanup", {}).get(dispatch, {}) if dispatch else {}
        if cleanup.get("state") in SETTLED_CLEANUP:
            continue
        active.append(key)
    return sorted(active)


def _unresolved_deliveries(state: dict) -> list[str]:
    unresolved = []
    for key, delivery in state.get("deliveries", {}).items():
        if not isinstance(delivery, dict):
            continue
        if any(row.get("effect") is None for row in delivery.get("items", {}).values()):
            unresolved.append(key)
    return sorted(unresolved)


def _pending_interventions(state: dict) -> list[str]:
    pending = []
    for task, record in state.get("interventions", {}).items():
        if isinstance(record, dict) and record.get("dispatch_authorized") is False:
            pending.append(task)
    return sorted(pending)


def _intervention_identity(state: dict) -> str:
    return digest(state.get("interventions", {}))


def _checkpoint(state: dict) -> dict:
    value = state.get("checkpoint")
    return value if isinstance(value, dict) else {}


def _phase(state: dict, actions: list[dict]) -> str:
    checkpoint = _checkpoint(state)
    candidate = checkpoint.get("candidate")
    if not candidate or _active(state):
        return "working"
    if _unresolved_deliveries(state) or _pending_interventions(state):
        return "converging"
    passed = {(row["action"]["kind"], row["action"]["candidate"]) for row in actions
              if row.get("outcome") == "PASS" and row.get("decision") in ("ALLOW", "WARN")}
    if any((kind, candidate) in passed for kind in ("merge", "release")):
        return "complete"
    if any((kind, candidate) in passed for kind in VALIDATION_KINDS):
        return "remotely_verified"
    return "candidate"


def _same(row: dict, action: dict) -> bool:
    prior = row["action"]
    return (prior["kind"] == action["kind"] and prior["candidate"] == action["candidate"]
            and prior["target"] == action["target"])


def _evaluate(action: dict, state: dict, actions: list[dict], phase: str) -> list[dict]:
    reasons = []
    checkpoint = _checkpoint(state)
    candidate = checkpoint.get("candidate")
    kind = action["kind"]
    if kind in AUTHORIZED_KINDS:
        authorization = validate_authorization(action.get("authorization"),
                                               candidate=action["candidate"], kind=kind)
        if authorization is None:
            reasons.append({"class": "authorization", "code": "authorization_missing", "decision": "DEFER",
                            "detail": f"{kind} needs an owner authorization record naming this candidate and scope"})
    if candidate and action["candidate"] != candidate:
        if kind == "remote_diagnostic" and action.get("diagnostic_value"):
            reasons.append({"class": "correctness", "code": "superseded_candidate", "decision": "WARN",
                            "detail": "diagnostic runs against a candidate the checkpoint has superseded"})
        else:
            reasons.append({"class": "correctness", "code": "superseded_candidate", "decision": "DEFER",
                            "detail": f"checkpoint candidate is {candidate}"})
    gaps = checkpoint.get("verification_gaps") or []
    if gaps:
        decision = "DEFER" if kind in AUTHORIZED_KINDS else ("WARN" if kind in LOCAL_KINDS else "DEFER")
        reasons.append({"class": "correctness", "code": "verification_gaps", "decision": decision,
                        "detail": f"{len(gaps)} verification gap(s) remain"})
    if kind in VALIDATION_KINDS + AUTHORIZED_KINDS and phase in ("working", "converging"):
        detail = ("active or uncertain native effects remain" if phase == "working"
                  else "deliveries or corrections are still unsettled")
        reasons.append({"class": "efficiency", "code": "integration_unsettled", "decision": "DEFER",
                        "detail": detail})
    identical = [row for row in actions if _same(row, action) and row.get("decision") in ("ALLOW", "WARN")]
    if identical:
        latest = identical[-1]
        if latest.get("outcome") in ("pending", "PASS"):
            reasons.append({"class": "efficiency", "code": "duplicate", "decision": "DEFER",
                            "detail": f"an identical action is already {latest.get('outcome')}"})
        elif latest.get("outcome") in ("FAILED", "UNKNOWN"):
            if latest.get("inputs") == _inputs(state):
                reasons.append({"class": "efficiency", "code": "unchanged_rerun", "decision": "DEFER",
                                "detail": "the previous attempt failed and no input has changed since"})
            else:
                reasons.append({"class": "efficiency", "code": "necessary_rerun", "decision": "ALLOW",
                                "detail": "inputs changed since the previous attempt"})
        if kind == "remote_diagnostic" and action.get("diagnostic_value"):
            reasons.append({"class": "efficiency", "code": "repeat_diagnostic", "decision": "WARN",
                            "detail": "this diagnostic already ran against the same candidate and target"})
    elif kind == "remote_diagnostic" and action.get("diagnostic_value"):
        reasons.append({"class": "efficiency", "code": "early_diagnostic", "decision": "ALLOW",
                        "detail": "supplies information unavailable locally"})
    return reasons


def _inputs(state: dict) -> str:
    """A digest of everything a rerun would consume; unchanged means nothing to learn."""
    checkpoint = _checkpoint(state)
    return digest({"candidate": checkpoint.get("candidate"),
                   "plan_revision": checkpoint.get("plan_revision"),
                   "policy_revision": checkpoint.get("policy_revision"),
                   "gaps": checkpoint.get("verification_gaps"),
                   "interventions": _intervention_identity(state),
                   "sources": state.get("source_rejections", {})})


def _resolve(reasons: list[dict], override: dict | None) -> tuple[str, dict]:
    applied = False
    ignored = None
    worst = "ALLOW"
    blocking = [reason for reason in reasons if reason["decision"] == "DEFER"]
    if blocking:
        hard = [reason for reason in blocking if reason["class"] != "efficiency"]
        if override is not None and not hard:
            applied = True
            for reason in blocking:
                reason["decision"] = "WARN"
            worst = "WARN"
        else:
            if override is not None:
                ignored = hard[0]["class"]
            worst = "DEFER"
    elif any(reason["decision"] == "WARN" for reason in reasons):
        worst = "WARN"
    return worst, {"applied": applied, "ignored_because": ignored}


def decide(project: Path, objective: str, *, owner: str, action: dict,
           override: object = None, now: datetime | None = None) -> dict:
    """Return ALLOW, WARN or DEFER for one proposed expensive effect, and record it."""
    bounded_text(owner, name="owner")
    proposal = validate_action(action)
    override_record = _validate_override(override)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_actions(record_path)
        actions = journal["actions"]
        phase = _phase(state, actions)
        reasons = _evaluate(proposal, state, actions, phase)
        decision, override_result = _resolve(reasons, override_record)
        record_id = digest({"action": proposal, "at": moment, "revision": journal["revision"]})
        result = {"schema": "pod-governor/v1", "decision": decision, "phase": phase,
                  "reasons": [{key: reason[key] for key in ("class", "code", "detail")} for reason in reasons],
                  "override": override_result, "record_id": record_id, "recorded": decision != "DEFER"}
        if decision != "DEFER":
            if len(actions) >= MAX_ACTIONS:
                raise PodError("governor_full", "Governor journal is full; start a new objective record")
            actions.append({"record_id": record_id, "action": proposal, "decision": decision,
                            "phase": phase, "at": moment, "outcome": "pending",
                            "inputs": _inputs(state),
                            "override": override_record, "reasons": result["reasons"]})
            journal["revision"] += 1
            atomic_json(record_path, journal)
        return result


def record_outcome(project: Path, objective: str, *, owner: str, record_id: str, outcome: str) -> dict:
    """Bind the real result of an allowed action so later decisions can reuse it."""
    bounded_text(owner, name="owner")
    bounded_text(record_id, name="record_id", limit=128)
    if outcome not in OUTCOMES or outcome == "pending":
        raise PodError("invalid_outcome", "Outcome must be PASS, FAILED or UNKNOWN")
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        state = _read(context_path)
        if state["owner"] not in (None, owner):
            raise PodError("coordinator_conflict", "Another coordinator owns this objective")
        journal = _read_actions(record_path)
        rows = [row for row in journal["actions"] if row["record_id"] == record_id]
        if not rows:
            raise PodError("unknown_governed_action", "No recorded action with that identity")
        row = rows[-1]
        if row["outcome"] != "pending" and row["outcome"] != outcome:
            raise PodError("outcome_conflict", "A different outcome was already recorded")
        if row["outcome"] == "pending":
            row["outcome"] = outcome
            journal["revision"] += 1
            atomic_json(record_path, journal)
        return {"status": "recorded", "record_id": record_id, "outcome": outcome}


def status(project: Path, objective: str) -> dict:
    """Read-only projection for status and reporting."""
    context_path = _path(project, objective)
    state = _read(context_path) if context_path.exists() else {
        "effects": {}, "cleanup": {}, "deliveries": {}, "interventions": {}, "checkpoint": None}
    journal = _read_actions(_record_path(project, objective))
    return {"schema": "pod-governor/v1", "phase": _phase(state, journal["actions"]),
            "active_effects": _active(state), "unresolved_deliveries": _unresolved_deliveries(state),
            "pending_diagnosis": _pending_interventions(state),
            "actions": [{key: row[key] for key in ("record_id", "decision", "outcome", "at")}
                        | {"kind": row["action"]["kind"], "candidate": row["action"]["candidate"]}
                        for row in journal["actions"][-16:]]}

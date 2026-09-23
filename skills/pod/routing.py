"""Deterministic assessment-bound route preview and replay; never calls a model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import COMPLEXITIES, EFFORTS, route_identity, validate
from .errors import PodError
from .util import bounded_text, digest, exact
from .quota import validate_snapshot

ASSESSMENT_FIELDS = {"method", "responsibility", "complexity", "risk", "size", "uncertainty", "verifiability", "capabilities", "context", "reason", "bounded", "data_location"}


def assess(raw: Any) -> dict:
    a = exact(raw, ASSESSMENT_FIELDS, ASSESSMENT_FIELDS - {"bounded", "data_location"}, name="assessment")
    for key in ("method", "responsibility", "risk", "size", "uncertainty", "verifiability", "reason"):
        bounded_text(a[key], name=key)
    if a["complexity"] not in COMPLEXITIES:
        raise PodError("invalid_assessment", "Unknown complexity")
    for key in ("capabilities", "context"):
        if not isinstance(a[key], list) or len(a[key]) > 32 or any(not isinstance(x, str) or len(x) > 128 for x in a[key]):
            raise PodError("invalid_assessment", f"{key} must be a bounded list")
    if "bounded" in a and not isinstance(a["bounded"], bool):
        raise PodError("invalid_assessment", "bounded must be boolean")
    if "data_location" in a:
        bounded_text(a["data_location"], name="data_location", limit=128)
    return a


def quota_state(snapshot: dict | None, *, provider: str | None, account: str | None,
                bucket: str | None, policy: dict, now: datetime) -> tuple[str, str]:
    if snapshot is None or not all(isinstance(x, str) and x for x in (provider, account, bucket)):
        return "unknown", "no supported quota snapshot"
    try:
        s = validate_snapshot(snapshot)
    except PodError:
        return "unknown", "quota snapshot is invalid"
    if s["provider"] != provider or s["account"] != account or s["bucket"] != bucket:
        return "unknown", "quota snapshot does not bind provider/account/bucket"
    if s["source"] not in ("supported", "supported_metadata") or s["confidence"] != "observed":
        return "unknown", "unsupported quota source"
    try:
        observed = datetime.fromisoformat(s["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or now.tzinfo is None:
            raise ValueError("timezone required")
        age = (now - observed).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return "unknown", "invalid quota timestamp"
    if age < 0:
        return "unknown", "quota observation is from the future"
    windows = s["windows"]
    known = [window["remaining_percent"] for window in windows if "remaining_percent" in window]
    if "remaining_percent" in s:
        known.append(s["remaining_percent"])
    if 0 in known:
        return "exhausted", "applicable bucket is exhausted"
    if s["unknowns"]:
        return "unknown", "quota windows have unresolved values"
    if not windows or any("remaining_percent" not in window for window in windows):
        return "unknown", "applicable quota windows are unknown"
    remaining = min(known)
    if age > policy["quota_fresh_seconds"]:
        return "unknown", "quota observation is stale"
    if remaining <= policy["quota_critical"]:
        return "critical", "applicable bucket is critical"
    if remaining <= policy["quota_low"]:
        return "low", "applicable bucket is low"
    return "normal", "supported quota observation"


def _grant(grants: list, *, action: str, route: dict, objective: str | None,
           units: int, now: datetime) -> dict | None:
    for grant in grants:
        if grant.get("action") != action or grant.get("account") != route.get("account"):
            continue
        if grant.get("model") != route.get("model"):
            continue
        if objective is None or grant.get("objective") != objective:
            continue
        if type(grant.get("max_units")) is not int or units > grant["max_units"] or units < 0:
            continue
        try:
            expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if expiry.tzinfo is not None and now.tzinfo is not None and now <= expiry:
            return {"id": grant["id"], "identity": digest(grant), "units": units,
                    "scope": digest({key: grant.get(key) for key in
                                     ("id", "action", "account", "model", "objective")})}
    return None


def preview(assessment: dict, effective: dict, *, capabilities: dict | None = None,
            quotas: dict | None = None, strict_pin: str | None = None, safety_refusal: bool = False,
            objective: str | None = None, now: datetime | None = None) -> dict:
    a = assess(assessment)
    policy = effective["policy"]
    models, rows, rules = policy["models"], policy["routing"], policy["policy"]
    caps, quota_map = capabilities or {}, quotas or {}
    now = now or datetime.now(timezone.utc)
    preferred = rows[a["complexity"]]["model"]
    preferred_effort = rows[a["complexity"]]["effort"]
    pin = strict_pin or (preferred if rows[a["complexity"]].get("strict") else None)
    reasons: dict[str, list[str]] = {}
    feasible: list[tuple[str, dict, str, str, dict | None]] = []
    if safety_refusal:
        return {"schema": "pod-route/v1", "status": "blocked", "preferred": preferred,
                "selected": None, "reason": "provider safety refusal; rerouting prohibited",
                "rejections": {}, "policy_revision": effective["revision"]}
    for alias, model in models.items():
        reject = []
        if pin and alias != pin:
            reject.append("strict pin excludes substitution")
        if not model.get("approved") or not model.get("approval_ref") or model.get("approval_route") != route_identity(model):
            reject.append("resolved route lacks personal approval")
        if not model.get("account"):
            reject.append("account route is unresolved")
        if model.get("agent") not in ("codex", "claude"):
            reject.append("agent route is unresolved")
        if rules.get("allowed_agents") is not None and model.get("agent") not in rules["allowed_agents"]:
            reject.append("agent is restricted")
        if rules.get("allowed_accounts") is not None and model.get("account") not in rules["allowed_accounts"]:
            reject.append("account is restricted")
        if model.get("capabilities") is not None and not set(a["capabilities"]) <= set(model["capabilities"]):
            reject.append("assignment exceeds approved model capabilities")
        if model.get("locations") is not None and a.get("data_location") not in model["locations"]:
            reject.append("data location is outside approved route")
        if rules.get("allowed_locations") is not None and a.get("data_location") not in rules["allowed_locations"]:
            reject.append("data location is restricted")
        effort = preferred_effort
        advertised = caps.get(alias)
        if not isinstance(advertised, dict) or advertised.get("agent") != model.get("agent") or advertised.get("model") != model.get("model") or advertised.get("account") != model.get("account"):
            reject.append("installed route capability is unverified")
        else:
            # A request cannot assert a native protection. Routing decides policy
            # feasibility only; the installed runtime establishes the route at admission.
            if "bucket" in advertised and (not isinstance(advertised["bucket"], str)
                                            or not advertised["bucket"] or len(advertised["bucket"]) > 128):
                reject.append("route quota bucket identity is invalid")
            if alias != preferred and a["complexity"] not in advertised.get("suitable_for", []):
                reject.append("alternative suitability for this assignment is unverified")
            approved_efforts = model.get("efforts", [preferred_effort])
            if effort not in approved_efforts or effort not in advertised.get("efforts", []):
                alternatives = [e for e in approved_efforts if e in advertised.get("efforts", [])]
                if alias != preferred and not pin and alternatives:
                    effort = alternatives[0]
                else:
                    reject.append("requested effort is outside approved or installed route")
            if effort not in advertised.get("efforts", []):
                reject.append("requested effort is unsupported")
            if not set(a["capabilities"]) <= set(advertised.get("capabilities", [])):
                reject.append("required capability is unsupported")
        billing = model.get("billing", "unknown")
        grant = None
        if billing != "included":
            grant = _grant(rules["spending_grants"], action="paid_usage", route=model, objective=objective, units=1, now=now)
            if grant is None:
                reject.append("paid or uncertain billing lacks an exact spending grant")
        route_bucket = advertised.get("bucket") if isinstance(advertised, dict) and isinstance(advertised.get("bucket"), str) else None
        qstate, qreason = quota_state(quota_map.get(model.get("account")), provider=model.get("agent"),
                                     account=model.get("account"), bucket=route_bucket, policy=rules, now=now)
        if qstate == "exhausted":
            reject.append(qreason)
        elif qstate == "critical" and (not a.get("bounded") or a["uncertainty"] not in ("low", "bounded")):
            reject.append("critical quota cannot support this uncertain assignment")
        elif qstate == "low" and not a.get("bounded"):
            reject.append("low quota requires bounded work or a later observation")
        if reject:
            reasons[alias] = reject
        else:
            feasible.append((alias, model, effort, qstate, grant))
    order = [preferred] + [x for x in models if x != preferred]
    chosen = next((x for name in order for x in feasible if x[0] == name), None)
    if chosen is None:
        return {"schema": "pod-route/v1", "status": "blocked", "preferred": preferred,
                "selected": None, "reason": "no approved, usable route", "rejections": reasons,
                "policy_revision": effective["revision"]}
    alias, model, effort, qstate, grant = chosen
    route = {"alias": alias, "agent": model["agent"], "model": model["model"], "account": model["account"],
             "bucket": caps[alias].get("bucket"), "effort": effort}
    return {"schema": "pod-route/v1", "status": "usable", "preferred": preferred,
            "selected": route, "reason": "preferred" if alias == preferred else "preferred infeasible: " + "; ".join(reasons.get(preferred, [])),
            "rejections": reasons, "quota_state": qstate, "approval_ref": model["approval_ref"],
            "spending_grant": grant,
            "policy_revision": effective["revision"], "assessment_digest": digest(a),
            "catalog_revision": digest(caps)}


def replay(captured: dict) -> dict:
    exact(captured, {"assessment", "effective", "capabilities", "quotas", "strict_pin", "safety_refusal", "objective", "at"},
          {"assessment", "effective", "capabilities", "quotas", "at"}, name="replay")
    try:
        when = datetime.fromisoformat(captured["at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PodError("invalid_replay", "Replay needs an explicit timestamp") from exc
    policy = captured["effective"]
    if not isinstance(policy, dict) or not isinstance(policy.get("policy"), dict):
        raise PodError("invalid_replay", "Replay needs a captured effective policy")
    validate(policy["policy"])
    if policy.get("revision") != digest(policy["policy"]):
        raise PodError("policy_revision_mismatch", "Captured policy revision does not match its content")
    return preview(captured["assessment"], captured["effective"], capabilities=captured["capabilities"],
                   quotas=captured["quotas"],
                   strict_pin=captured.get("strict_pin"), safety_refusal=captured.get("safety_refusal", False),
                   objective=captured.get("objective"), now=when)

"""Deterministic enforcement of a coordinator-proposed worker route."""

from __future__ import annotations

from datetime import datetime, timezone

from .catalog import by_id
from .errors import PodError

CONSTRAINT_KINDS = ("only_agents", "only_models", "exclude_models", "role_model",
                    "max_workers", "allow_disabled", "allow_delegation")
PROVENANCE = ("user_direct", "issue", "repository", "worker")
FAILURE_KINDS = ("rate_limited", "unavailable", "auth_failed", "safety_refusal")


def validate_constraint(value: object, snapshot: dict | None = None) -> dict:
    if not isinstance(value, dict):
        raise PodError("invalid_constraint", "Constraint must be a record")
    required = {"kind", "provenance", "value"}
    if set(value) - (required | {"id", "role", "saved_state", "mode", "file_stamp", "active"}) or not required <= set(value):
        raise PodError("invalid_constraint", "Constraint has missing or unsupported fields")
    kind, provenance, item = value["kind"], value["provenance"], value["value"]
    if kind not in CONSTRAINT_KINDS or provenance not in PROVENANCE:
        raise PodError("invalid_constraint", "Constraint kind or provenance is unsupported")
    if kind in ("allow_disabled", "allow_delegation") and provenance != "user_direct":
        raise PodError("constraint_authority", "Only direct user intent can widen delegation")
    if kind in ("only_agents", "only_models", "exclude_models"):
        allowed = {"codex", "claude"} if kind == "only_agents" else set(by_id())
        if (not isinstance(item, list) or not item or len(item) > 6
                or any(not isinstance(member, str) for member in item)
                or len(set(item)) != len(item) or not set(item) <= allowed):
            raise PodError("invalid_constraint", "Constraint list is invalid")
    elif kind == "role_model":
        if not isinstance(item, str) or item not in by_id() or not isinstance(value.get("role"), str) or not value["role"]:
            raise PodError("invalid_constraint", "Role model needs a role and supported id")
    elif kind == "max_workers":
        if type(item) is not int or not 0 <= item <= 8:
            raise PodError("invalid_constraint", "Maximum workers must be 0 through 8")
        if snapshot is not None and item > snapshot["max_active"]:
            raise PodError("constraint_authority", "Objective maximum cannot widen personal ceiling")
    elif kind == "allow_disabled":
        if not isinstance(item, str) or item not in by_id():
            raise PodError("invalid_constraint", "Disabled exception needs a supported model id")
        if snapshot is not None:
            if snapshot["saved"].get(item) != "disabled":
                raise PodError("invalid_constraint", "Scoped exception needs a Disabled saved model")
            if not snapshot.get("file_stamp"):
                raise PodError("constraint_authority", "Personal preference file identity is unavailable")
            value = {**value, "saved_state": snapshot["saved"][item], "mode": snapshot["mode"],
                     "file_stamp": snapshot["file_stamp"]}
    elif item is not True:
        raise PodError("invalid_constraint", "Delegation exception must be true")
    return value


def active_constraints(constraints: list[dict], snapshot: dict) -> list[dict]:
    result = []
    for row in constraints:
        checked = validate_constraint(row)
        if checked.get("active", True) is False:
            continue
        # A file stamp is conservative: it also lapses after unrelated edits, but a
        # toggle away and back cannot silently reactivate an old direct-user exception.
        if checked["kind"] == "allow_disabled" and (
                checked.get("saved_state") != snapshot["saved"].get(checked["value"])
                or checked.get("mode") != snapshot["mode"]
                or checked.get("file_stamp") != snapshot["file_stamp"]):
            continue
        result.append(checked)
    return result


def worker_ceiling(snapshot: dict, constraints: list[dict]) -> int:
    ceiling = snapshot["max_active"]
    for row in active_constraints(constraints, snapshot):
        if row["kind"] == "max_workers":
            ceiling = min(ceiling, row["value"])
    return ceiling


def failure_active(failure: dict, *, now: datetime | None = None) -> bool:
    if failure.get("cleared_at"):
        return False
    retry_after = failure.get("retry_after")
    if retry_after is None:
        return True
    try:
        due = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PodError("invalid_route_failure", "Retry-after is not an ISO timestamp") from exc
    if due.tzinfo is None:
        raise PodError("invalid_route_failure", "Retry-after needs a timezone")
    return due > (now or datetime.now(timezone.utc))


def validate_choice(snapshot: dict, constraints: list[dict], failures: list[dict],
                    choice: object, *, task: str | None = None, role: str | None = None,
                    now: datetime | None = None) -> dict:
    """Validate only current eligibility and native shape; never judge suitability."""
    if not isinstance(choice, dict) or set(choice) != {"agent", "model", "effort", "context", "reason"}:
        return {"allowed": False, "code": "invalid_choice"}
    models = by_id()
    model_id = choice["model"]
    if not isinstance(model_id, str) or model_id not in models:
        return {"allowed": False, "code": "unsupported_model"}
    if snapshot["errors"]:
        return {"allowed": False, "code": "preferences_unavailable"}
    if choice["agent"] != models[model_id]["agent"]:
        return {"allowed": False, "code": "agent_mismatch"}
    if choice["context"] != "native_default":
        return {"allowed": False, "code": "context_unsupported"}
    effort = choice["effort"]
    if effort != "native_default" and (effort not in models[model_id]["efforts"] or effort == "ultra"):
        return {"allowed": False, "code": "effort_unsupported"}
    if not isinstance(choice["reason"], str) or not choice["reason"].strip() or len(choice["reason"]) > 512:
        return {"allowed": False, "code": "reason_missing"}
    rows = active_constraints(constraints, snapshot)
    exceptions = {row["value"] for row in rows if row["kind"] == "allow_disabled"}
    if model_id not in snapshot["eligible"] and not (
            snapshot["saved"].get(model_id) == "disabled" and model_id in exceptions):
        return {"allowed": False, "code": "model_ineligible" if snapshot["eligible"] else "empty_pool"}
    for row in rows:
        kind, value = row["kind"], row["value"]
        if (kind == "only_agents" and choice["agent"] not in value
                or kind == "only_models" and model_id not in value
                or kind == "exclude_models" and model_id in value
                or kind == "role_model" and row.get("role") == role and model_id != value):
            return {"allowed": False, "code": "constraint_excluded"}
    for failure in failures:
        if (failure.get("kind") == "safety_refusal" and task is not None
                and failure.get("task") == task and not failure.get("cleared_at")):
            return {"allowed": False, "code": "safety_refusal"}
        if not failure_active(failure, now=now):
            continue
        if failure.get("model") == model_id and failure.get("kind") in FAILURE_KINDS:
            return {"allowed": False, "code": "route_failed"}
    return {"allowed": True, "code": "allowed", "model": model_id, "agent": choice["agent"],
            "effort": effort, "context": "native_default"}

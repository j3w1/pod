"""Deterministic enforcement of a coordinator-proposed exact worker route."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .catalog import AGENTS, EFFORTS, by_id, known, parse_route_key, route_key, route_keys, supported_route
from .contracts import bound, fields, required, input_record
from .errors import PodError
from .util import DECISION_FIELDS, owner_decision

CONSTRAINT_KINDS = ("only_agents", "only_models", "exclude_models", "role_model", "route_rule",
                    "max_workers", "allow_disabled", "allow_delegation", "keep_worker_tab")
PROVENANCE = ("user_direct", "issue", "repository", "worker")
TEMPORARY_RECONSIDERATION_SECONDS = 60
FAILURE_KINDS = ("rate_limited", "unavailable", "auth_failed", "safety_refusal")
PREFERENCE_ERRORS = ("setup_required", "invalid_pin", "pin_ineligible", "invalid_preferred",
                     "preferred_ineligible")
SEMANTIC_ROLES = ("coordinator", "worker", "reviewer", "fixer", "auditor")
RULE_CONSTRAINTS = ("only_agents", "only_models", "exclude_models", "role_model")
RULE_DECISIONS = ("user_direct", "agent_decision", "decision_request")


def _rule_list(value: object, allowed, name: str) -> list:
    if (not isinstance(value, list) or not value or len(value) > len(allowed)
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value) or not set(value) <= set(allowed)):
        raise PodError("invalid_route_rule", f"Route rule {name} needs a nonempty unique supported list")
    return value


def _rule_routes(value: object, *, stored: bool) -> list:
    # Stored registry tuples remain readable, but filtering uses supported routes only.
    if not isinstance(value, list) or not value or len(value) > len(known()) * len(EFFORTS):
        raise PodError("invalid_route_rule", "Route rule routes need a bounded nonempty exact route list")
    for key in value:
        if supported_route(key) is not None:
            continue
        try:
            shape = parse_route_key(key)
        except PodError:
            raise PodError("invalid_route_rule", "Route rule needs supported exact routes") from None
        if not stored or shape["model"] not in known() or known()[shape["model"]]["agent"] != shape["agent"]:
            raise PodError("invalid_route_rule", "Route rule needs supported exact routes")
    if len(set(value)) != len(value):
        raise PodError("invalid_route_rule", "Route rule routes must be unique")
    return value


def validate_rule(value: object, *, stored: bool = False) -> dict:
    """Validate a composite's structure only; approval belongs to its mutation boundary."""
    if not isinstance(value, dict) or set(value) != {"constraints", "efforts", "roles", "fallback"}:
        raise PodError("invalid_route_rule", "Route rule needs constraints, efforts, roles and fallback")
    nested = value["constraints"]
    if not isinstance(nested, list) or len(nested) > 64:
        raise PodError("invalid_route_rule", "Route rule constraints need a bounded list")
    for row in nested:
        if (not isinstance(row, dict) or not {"kind", "value"} <= set(row)
                or set(row) - {"kind", "value", "role"} or row["kind"] not in RULE_CONSTRAINTS
                or ("role" in row and row["kind"] != "role_model")):
            raise PodError("invalid_route_rule", "Route rule nested constraint shape is invalid")
        validate_constraint({**row, "provenance": "issue"})
        if row["kind"] == "role_model" and row.get("role") not in SEMANTIC_ROLES:
            raise PodError("invalid_route_rule", "Route rule role_model needs a semantic role")
    if value["efforts"] is not None:
        _rule_list(value["efforts"], EFFORTS, "efforts")
    roles = value["roles"]
    if not isinstance(roles, dict) or set(roles) - set(SEMANTIC_ROLES):
        raise PodError("invalid_route_rule", "Route rule roles need canonical semantic roles")
    for predicates in roles.values():
        if isinstance(predicates, list):
            _rule_routes(predicates, stored=stored)
        elif isinstance(predicates, dict) and predicates and not set(predicates) - {
                "only_agents", "only_models", "exclude_models", "efforts"}:
            for kind, items in predicates.items():
                vocabulary = EFFORTS if kind == "efforts" else AGENTS if kind == "only_agents" else known()
                _rule_list(items, vocabulary, kind)
        else:
            raise PodError("invalid_route_rule", "Route rule role needs exact routes or nonempty filters")
    fallback = value["fallback"]
    if fallback is not None:
        if not isinstance(fallback, list) or len(fallback) > 5:
            raise PodError("invalid_route_rule", "Route rule fallback needs at most five entries")
        covered = set()
        for entry in fallback:
            if not isinstance(entry, dict) or set(entry) != {"roles", "on", "routes"}:
                raise PodError("invalid_route_rule", "Route rule fallback entry shape is invalid")
            _rule_list(entry["roles"], SEMANTIC_ROLES, "fallback roles")
            _rule_list(entry["on"], ("unavailable", "rate_limited"), "fallback failures")
            _rule_routes(entry["routes"], stored=stored)
            if covered.intersection(entry["roles"]):
                raise PodError("invalid_route_rule", "Route rule fallback role groups must be disjoint")
            covered.update(entry["roles"])
    return value


def validate_constraint(value: object, snapshot: dict | None = None, *, new: bool = False) -> dict:
    """Validate constraint shape and shared input or stored approval evidence.

    Model constraints accept any registry id, including ids that are no longer supported, so
    earlier objective records stay readable. A new Disabled exception names one exact Disabled
    route; an earlier model-id exception remains readable but never matches a route.
    """
    if not isinstance(value, dict):
        raise PodError("invalid_constraint", "Constraint must be a record")
    required = {"kind", "provenance", "value"}
    if set(value) - (required | {"id", "role", "saved_state", "mode", "file_stamp", "active"} | DECISION_FIELDS) or not required <= set(value):
        raise PodError("invalid_constraint", "Constraint has missing or unsupported fields")
    kind, provenance, item = value["kind"], value["provenance"], value["value"]
    model_decision = kind in (*RULE_CONSTRAINTS, "route_rule") and provenance in RULE_DECISIONS
    if (kind not in CONSTRAINT_KINDS or provenance not in PROVENANCE
            and not model_decision):
        raise PodError("invalid_constraint", "Constraint kind or provenance is unsupported")
    if provenance == "user_direct" or model_decision:
        value = owner_decision(value, kind="constraint", stored=not (new or snapshot is not None))
    elif (DECISION_FIELDS - {"provenance"}) & set(value):
        raise PodError("invalid_constraint", "Only direct user constraints carry owner decision fields")
    if kind in ("allow_disabled", "allow_delegation") and provenance != "user_direct":
        raise PodError("constraint_authority", "Only direct user intent can widen delegation")
    if kind == "route_rule":
        validate_rule(item, stored=not (new or snapshot is not None))
    elif kind in ("only_agents", "only_models", "exclude_models"):
        allowed = set(AGENTS) if kind == "only_agents" else set(known())
        if (not isinstance(item, list) or not item or len(item) > len(allowed)
                or any(not isinstance(member, str) for member in item)
                or len(set(item)) != len(item) or not set(item) <= allowed):
            raise PodError("invalid_constraint", "Constraint list is invalid")
    elif kind == "role_model":
        if not isinstance(item, str) or item not in known() or not isinstance(value.get("role"), str) or not value["role"]:
            raise PodError("invalid_constraint", "Role model needs a role and a registry id")
    elif kind == "max_workers":
        if type(item) is not int or not 0 <= item <= 8:
            raise PodError("invalid_constraint", "Maximum workers must be 0 through 8")
        if snapshot is not None and item > snapshot["max_active"]:
            raise PodError("constraint_authority", "Objective maximum cannot widen personal ceiling")
    elif kind == "keep_worker_tab":
        if provenance != "user_direct" or not value.get("instruction"):
            raise PodError("constraint_authority", "A worker-tab keep or revocation needs a recorded user_direct instruction")
        _worker_tab_instruction(item)
    elif kind == "allow_disabled":
        if not isinstance(item, str) or (supported_route(item) is None and item not in known()):
            raise PodError("invalid_constraint", "Disabled exception needs an exact supported route")
        if snapshot is not None:
            if supported_route(item) is None or snapshot["routes"].get(item) != "disabled":
                raise PodError("invalid_constraint", "Scoped exception needs an exact Disabled saved route")
            if not snapshot.get("file_stamp"):
                raise PodError("constraint_authority", "Personal preference file identity is unavailable")
            value = {key: field for key, field in value.items() if key != "mode"}
            value.update(saved_state=snapshot["routes"][item], file_stamp=snapshot["file_stamp"])
    elif item is not True:
        raise PodError("invalid_constraint", "Delegation exception must be true")
    return value


@input_record("worker_tab_instruction")
def _worker_tab_instruction(item):
    from .util import bounded_text, exact
    exact(item, fields("worker_tab_instruction"), required("worker_tab_instruction"), name="worker_tab_instruction")
    for field in ("objective", "dispatch", "terminal"):
        bounded_text(item[field], name=field, limit=bound("worker_tab_instruction", field))
    if type(item["keep"]) is not bool:
        raise PodError("invalid_constraint", "Worker-tab keep is a boolean; false records revocation")


def active_constraints(constraints: list[dict], snapshot: dict) -> list[dict]:
    result = []
    routes = snapshot.get("routes") or {}
    for row in constraints:
        checked = validate_constraint(row)
        if checked.get("active", True) is False:
            continue
        # A file stamp is conservative: it also lapses after unrelated edits, but a
        # toggle away and back cannot silently reactivate an old direct-user exception.
        if checked["kind"] == "allow_disabled" and (
                checked.get("saved_state") != routes.get(checked["value"])
                or "mode" in checked
                or checked.get("file_stamp") != snapshot.get("file_stamp")):
            continue
        result.append(checked)
    return result


def semantic_role(packet_role: str, served_rows: list[dict], *, requested: str | None = None,
                  composite: bool = False) -> str:
    """Derive a routing role from authoritative rows, never titles or responsibility prose.

    The adapter supplies resolved correction ancestors as additional correction rows.
    Calls without a composite keep their role semantics and need no structural facts.
    """
    defaults = {"implement": "worker", "investigate": "worker", "review": "reviewer"}
    if not composite:
        return requested or defaults.get(packet_role, packet_role)
    if (not isinstance(packet_role, str) or packet_role not in defaults
            or requested is not None and requested not in SEMANTIC_ROLES
            or not isinstance(served_rows, list) or not served_rows
            or any(not isinstance(row, dict) for row in served_rows)):
        raise PodError("routing_role_invalid", "Composite starts need authoritative structural role facts")
    if packet_role == "review":
        if any(row.get("kind") != "assurance" or row.get("audit_role") not in ("reviewer", "auditor")
               for row in served_rows):
            raise PodError("routing_role_required", "Review assurances need one agreeing typed audit role")
        designations = {row["audit_role"] for row in served_rows}
        if len(designations) != 1:
            raise PodError("routing_role_required", "Review assurances need one agreeing typed audit role")
        result = next(iter(designations))
        if requested != result:
            raise PodError("routing_role_mismatch", "Review routing role must match its served assurances")
        return result
    result = ("fixer" if packet_role == "implement"
              and any(row.get("kind") == "correction" for row in served_rows) else "worker")
    if (result == "fixer" and requested != "fixer"
            or requested is not None and requested != result):
        raise PodError("routing_role_mismatch", "Routing role must match the structural work role")
    return result


def _unmet_predicate(kind: str, value, route: dict, *, row_role=None, role=None) -> str | None:
    if kind == "only_agents" and route["agent"] not in value:
        return "agent"
    if (kind == "only_models" and route["model"] not in value
            or kind == "exclude_models" and route["model"] in value
            or kind == "role_model" and row_role == role and route["model"] != value):
        return "model"
    if kind == "efforts" and route["effort"] not in value:
        return "effort"
    return None


def _primary_unmet(value: dict, route: dict, routing_role: str) -> str | None:
    for row in value["constraints"]:
        predicate = _unmet_predicate(row["kind"], row["value"], route,
                                     row_role=row.get("role"), role=routing_role)
        if predicate:
            return predicate
    if value["efforts"] is not None and route["effort"] not in value["efforts"]:
        return "effort"
    per_role = value["roles"].get(routing_role, {})
    if isinstance(per_role, list):
        return "role" if route["key"] not in per_role else None
    for kind, items in per_role.items():
        predicate = _unmet_predicate(kind, items, route)
        if predicate:
            return predicate
    return None


def _approved_rule(row: dict) -> bool:
    """Use shared stored-approval validation, never an actor label as authority.

    The installed utility rejects new actor forms until integration supplies their
    actual stored-evidence validation. This seam then admits truthful agent approvals
    without rewriting them as user_direct or changing the route selector again.
    """
    if row["provenance"] not in RULE_DECISIONS:
        return False
    owner_decision(row, kind="constraint", stored=True)
    return True


def allowed_routes(snapshot: dict, constraints: list[dict], *, routing_role: str | None,
                   fallback: dict | None = None, role: str | None = None) -> dict:
    """Project the existing selector's hard filters without ranking suitability.

    The optional role is original responsibility prose for standalone
    role_model rows; nested composite rows use only the semantic routing_role.
    Fallback context is an internal adapter port. Its shape checks are not native or
    approval proof: the admission adapter must verify task, lineage, settlement and
    current failure facts before constructing it, and must reject raw public context.
    """
    rows = active_constraints(constraints, snapshot)
    groups = [row for row in rows if row["kind"] == "route_rule"]
    if len(groups) > 1:
        raise PodError("route_rule_conflict", "Only one requested route rule may be active")
    group = groups[0] if groups else None
    rule_id = group.get("id") if group else None
    projection = {"routes": [], "stage": "primary", "pinned_route": snapshot.get("pinned"),
                  "preferred_route": None, "pin_inapplicable": False, "unmet": []}

    def unmet(predicate):
        row = {"rule_id": rule_id, "predicate": predicate}
        if row not in projection["unmet"]:
            projection["unmet"].append(row)

    if snapshot["errors"]:
        unmet("preferences")
        projection["pin_inapplicable"] = snapshot.get("pinned") is not None
        return projection
    fallback_routes = None
    if fallback is not None:
        projection["stage"] = "fallback"
        if (not group or not isinstance(fallback, dict)
                or set(fallback) != {"admission", "kind", "rule_id", "routing_role", "settlement"}
                or not isinstance(fallback["admission"], str) or not fallback["admission"].strip()
                or not rule_id or fallback["rule_id"] != rule_id
                or fallback["routing_role"] != routing_role
                or fallback["kind"] not in ("unavailable", "rate_limited")
                or fallback["settlement"] not in ("settled", "no_start")):
            unmet("stage")
        else:
            fallback_routes = next((entry["routes"] for entry in group["value"]["fallback"] or []
                                    if routing_role in entry["roles"] and fallback["kind"] in entry["on"]), None)
            if fallback_routes is None:
                unmet("stage")
    if group and routing_role not in SEMANTIC_ROLES:
        unmet("role")
    pin = snapshot.get("pinned")
    if projection["unmet"]:
        projection["pin_inapplicable"] = pin is not None
        return projection

    candidates = set(snapshot["eligible"])
    candidates.update(row["value"] for row in rows if row["kind"] == "allow_disabled"
                      and snapshot["routes"].get(row["value"]) == "disabled")
    if group and _approved_rule(group):
        explicit = fallback_routes if fallback_routes is not None else group["value"]["roles"].get(routing_role, [])
        if isinstance(explicit, list):
            candidates.update(explicit)
    rejected = []
    for key in route_keys():
        route = parse_route_key(key) | {"key": key}
        predicate = None
        if group:
            if fallback_routes is not None:
                predicate = "role" if key not in fallback_routes else None
            else:
                predicate = _primary_unmet(group["value"], route, routing_role)
        if predicate is None and key not in candidates:
            predicate = "pool"
        if predicate is None:
            for row in rows:
                kind = row["kind"]
                if (not group and pin is not None and row["provenance"] == "repository"
                        and kind in ("only_models", "exclude_models", "role_model")):
                    continue
                # Standalone role_model retains its caller's responsibility meaning.
                predicate = _unmet_predicate(kind, row["value"], route,
                                             row_role=row.get("role"), role=role)
                if predicate:
                    break
        if predicate is None:
            projection["routes"].append(key)
        elif predicate not in rejected:
            rejected.append(predicate)
    if not projection["routes"]:
        for predicate in rejected or ["pool"]:
            unmet(predicate)
    projection["pin_inapplicable"] = pin is not None and pin not in projection["routes"]
    preferred = snapshot.get("preferred")
    projection["preferred_route"] = preferred if preferred in projection["routes"] else None
    return projection


def worker_ceiling(snapshot: dict, constraints: list[dict]) -> int:
    ceiling = snapshot["max_active"]
    for row in active_constraints(constraints, snapshot):
        if row["kind"] == "max_workers":
            ceiling = min(ceiling, row["value"])
    return ceiling


def failure_active(failure: dict, *, now: datetime | None = None) -> bool:
    if failure.get("cleared_at"):
        return False
    retry_after = failure.get("reconsider_at") or failure.get("retry_after")
    if retry_after is None:
        if failure.get("kind") != "unavailable":
            return True
        recorded = failure.get("recorded_at")
        # Unsupported old records without observation time remain diagnostic holds.
        if not isinstance(recorded, str):
            return True
        try:
            observed = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PodError("invalid_route_failure", "Observation time is not an ISO timestamp") from exc
        if observed.tzinfo is None:
            raise PodError("invalid_route_failure", "Observation time needs a timezone")
        return observed + timedelta(seconds=TEMPORARY_RECONSIDERATION_SECONDS) > (now or datetime.now(timezone.utc))
    try:
        due = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PodError("invalid_route_failure", "Retry-after is not an ISO timestamp") from exc
    if due.tzinfo is None:
        raise PodError("invalid_route_failure", "Retry-after needs a timezone")
    return due > (now or datetime.now(timezone.utc))


def validate_choice(snapshot: dict, constraints: list[dict], failures: list[dict],
                    choice: object, *, task: str | None = None, role: str | None = None,
                    now: datetime | None = None, routing_role: str | None = None,
                    fallback: dict | None = None) -> dict:
    """Validate only current eligibility, the exact pin and native shape; never judge suitability."""
    if not isinstance(choice, dict) or set(choice) != {"agent", "model", "effort", "context", "reason"}:
        return {"allowed": False, "code": "invalid_choice"}
    models = by_id()
    model_id = choice["model"]
    if not isinstance(model_id, str) or model_id not in models:
        return {"allowed": False, "code": "unsupported_model"}
    if snapshot["errors"]:
        return {"allowed": False, "code": next((row["code"] for row in snapshot["errors"]
                      if row["code"] in PREFERENCE_ERRORS), "preferences_unavailable")}
    if choice["agent"] != models[model_id]["agent"]:
        return {"allowed": False, "code": "agent_mismatch"}
    if choice["context"] != "native_default":
        return {"allowed": False, "code": "context_unsupported"}
    effort = choice["effort"]
    if effort == "native_default":
        return {"allowed": False, "code": "effort_required"}
    if not isinstance(effort, str) or effort not in models[model_id]["efforts"]:
        return {"allowed": False, "code": "effort_unsupported"}
    if not isinstance(choice["reason"], str) or not choice["reason"].strip() or len(choice["reason"]) > 512:
        return {"allowed": False, "code": "reason_missing"}
    key = route_key(choice["agent"], model_id, effort)
    pin = snapshot.get("pinned")
    has_composite = any(isinstance(row, dict) and row.get("kind") == "route_rule"
                        and row.get("active", True) is not False for row in constraints)
    if not has_composite and fallback is None and pin is not None and key != pin:
        return {"allowed": False, "code": "pin_mismatch", "pinned_route": pin}
    rows = active_constraints(constraints, snapshot)
    group = next((row for row in rows if row["kind"] == "route_rule"), None)
    rule_id = group.get("id") if group else None
    projection = None
    if group or fallback is not None:
        projection = allowed_routes(snapshot, constraints, routing_role=routing_role,
                                    fallback=fallback, role=role)
        pin = None if projection["pin_inapplicable"] else projection["pinned_route"]
        if key not in projection["routes"]:
            predicate = next((row["predicate"] for row in projection["unmet"]
                              if row["predicate"] == "stage"
                              or row["predicate"] == "role" and routing_role not in SEMANTIC_ROLES), None)
            if predicate is None and group:
                predicate = (_primary_unmet(group["value"], {"key": key, **choice}, routing_role)
                             if projection["stage"] == "primary" else None)
                if projection["stage"] == "fallback":
                    alternatives = next((entry["routes"] for entry in group["value"]["fallback"] or []
                                         if routing_role in entry["roles"] and fallback["kind"] in entry["on"]), [])
                    predicate = "role" if key not in alternatives else None
            if predicate is None:
                predicate = next((part for row in rows if (part := _unmet_predicate(
                    row["kind"], row["value"], choice, row_role=row.get("role"), role=role))), "pool")
            return {"allowed": False, "code": "route_rule_excluded", "rule_id": rule_id,
                    "predicate": predicate, "stage": projection["stage"],
                    "pinned_route": projection["pinned_route"], "pin_inapplicable": projection["pin_inapplicable"]}
        if pin is not None and key != pin:
            return {"allowed": False, "code": "pin_mismatch", "pinned_route": pin,
                    "rule_id": rule_id,
                    "predicate": "pin", "stage": projection["stage"]}
    else:
        # Preserve standalone pin/repository precedence and exact refusal codes.
        exceptions = {row["value"] for row in rows if row["kind"] == "allow_disabled"}
        if key not in snapshot["eligible"] and not (
                snapshot["routes"].get(key) == "disabled" and key in exceptions):
            return {"allowed": False, "code": "route_ineligible" if snapshot["eligible"] else "empty_pool"}
        for row in rows:
            kind, value = row["kind"], row["value"]
            if pin is not None and row["provenance"] == "repository" and kind in (
                    "only_models", "exclude_models", "role_model"):
                continue
            if _unmet_predicate(kind, value, choice, row_role=row.get("role"), role=role):
                return {"allowed": False, "code": "constraint_excluded"}
    for failure in failures:
        if (failure.get("kind") == "safety_refusal" and task is not None
                and failure.get("task") == task and not failure.get("cleared_at")):
            result = {"allowed": False, "code": "safety_refusal"}
            if projection:
                result.update(rule_id=rule_id,
                              predicate="stage", stage=projection["stage"])
            return result
        if not failure_active(failure, now=now):
            continue
        if failure.get("model") == model_id and failure.get("kind") in FAILURE_KINDS:
            result = {"allowed": False, "code": "route_failed",
                      "reconsider_at": failure.get("reconsider_at") or failure.get("retry_after"),
                      "source": failure.get("source"), "recorded_at": failure.get("recorded_at"),
                      "pinned_route": projection["pinned_route"] if projection else pin}
            if projection:
                result.update(rule_id=rule_id,
                              predicate="stage", stage=projection["stage"],
                              pin_inapplicable=projection["pin_inapplicable"])
            return result
    result = {"allowed": True, "code": "allowed", "route": key, "model": model_id, "agent": choice["agent"],
            "effort": effort, "context": "native_default",
            "pinned_route": projection["pinned_route"] if projection else pin,
            "preferred_route": projection["preferred_route"] if projection else snapshot.get("preferred")}
    if projection:
        result.update(stage=projection["stage"], pin_inapplicable=projection["pin_inapplicable"],
                      rule_id=rule_id)
    return result

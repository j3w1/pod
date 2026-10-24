"""Pure machine decision policy; evaluated principals are never credentials.

Mutation adapters supply complete effects, including removals, as ``{kind, effect}``
envelopes in ``effects``. Questions/exceptions wrap one such envelope in ``effect``.
Constraints use ``added`` and ``removed`` lists of complete constraint rows;
standing decisions use ``scopes`` of action names or envelopes. Opaque effects
remain unclassified. These helpers cannot prove an adapter supplied every effect.
"""

from __future__ import annotations

import copy
import json

from .errors import PodError
from .util import bounded_text, exact

CATEGORIES = (
    "merge_publish", "scope_changes", "secrets", "speaking_for_person",
    "production_deployment", "irreversible_deletion", "model_choices",
    "coordinator_changes", "continuity", "routine", "money", "policy_changes",
    "human_login",
)
DECIDERS = ("any_agent", "trusted_agents", "person")
PERSON_ONLY = frozenset({"money", "policy_changes", "human_login"})
SENSITIVE = frozenset({"secrets", "money", "speaking_for_person",
                       "production_deployment", "irreversible_deletion"})
_KINDS = {
    "governance": "scope_changes", "withdrawal": "scope_changes",
    "retirement": "scope_changes", "revision": "scope_changes",
    "issue_revision": "scope_changes", "reopen": "scope_changes",
    "route_rule": "model_choices", "route_failure_clear": "model_choices",
    "only_agents": "model_choices", "only_models": "model_choices",
    "exclude_models": "model_choices", "role_model": "model_choices",
    "continuity": "continuity", "compaction": "routine", "closure": "routine",
    "pod_tick": "routine", "policy": "policy_changes",
    "policy_change": "policy_changes", "policy_setup": "policy_changes",
    "human_login": "human_login", "publish": "merge_publish",
    "merge": "merge_publish", "release": "merge_publish",
    "deployment": "production_deployment", "deploy": "production_deployment",
    "install": "routine", "cleanup": "routine",
    "unique_deletion": "irreversible_deletion", "scope_growth": "scope_changes",
    "secrets": "secrets", "speaking_for_person": "speaking_for_person",
    "irreversible_deletion": "irreversible_deletion", "routine": "routine",
}


def _bounded_object(value, *, name, limit=4096):
    """Copy bounded JSON metadata without treating its shape as authority."""
    if not isinstance(value, dict) or not value:
        raise PodError("invalid_" + name, f"{name} must be a nonempty object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PodError("invalid_" + name, f"{name} must be bounded JSON") from None
    if len(encoded) > limit:
        raise PodError("invalid_" + name, f"{name} exceeds its encoded byte limit")
    try:
        return copy.deepcopy(value)
    except RecursionError:
        raise PodError("invalid_" + name, f"{name} exceeds bounded JSON depth") from None


def preset(name, categories=None):
    """Return the complete decider map, never a persisted policy decision."""
    if name not in ("careful", "delegate", "hands_off", "custom"):
        raise PodError("invalid_decision_policy", "Unknown decision policy preset")
    if name == "custom":
        result = dict(exact(categories, set(CATEGORIES), set(CATEGORIES), name="policy_categories"))
    else:
        if categories is not None:
            raise PodError("invalid_decision_policy", "Only Custom accepts a category map")
        default = {"careful": "person", "delegate": "trusted_agents", "hands_off": "any_agent"}[name]
        result = dict.fromkeys(CATEGORIES, default)
        if name == "careful":
            result.update(dict.fromkeys(("model_choices", "continuity", "routine"), "any_agent"))
        result.update(dict.fromkeys(PERSON_ONLY, "person"))
    if any(value not in DECIDERS for value in result.values()):
        raise PodError("invalid_decision_policy", "Unknown category decider")
    if any(result[key] != "person" for key in PERSON_ONLY):
        raise PodError("invalid_decision_policy", "Money, policy changes and human login require person")
    return result


def validate(value):
    """Validate a stored policy; integration validates the decision's actor/evidence."""
    row = exact(value, {"preset", "categories", "setup", "decision"},
                {"preset", "categories", "setup"}, name="decision_policy")
    expected = preset("custom", row["categories"])
    if row["preset"] != "custom" and preset(row["preset"]) != expected:
        raise PodError("invalid_decision_policy", "Preset and category map disagree")
    if row["setup"] not in ("answered", "skipped"):
        raise PodError("invalid_decision_policy", "Setup must be answered or skipped")
    if row["setup"] == "skipped":
        if row["preset"] != "careful" or "decision" in row:
            raise PodError("invalid_decision_policy", "Skipped setup records only Careful without a decision")
    elif "decision" not in row:
        raise PodError("invalid_decision_policy", "Answered setup needs its decision record")
    result = {**row, "categories": expected}
    if "decision" in row:
        result["decision"] = _bounded_object(row["decision"], name="policy_decision")
    return result


def effective(value_or_none):
    """Missing policy is read-only implicit Careful; it is not a skipped latch."""
    if value_or_none is None:
        return {"preset": "careful", "categories": preset("careful"), "setup": "unasked"}
    # Accept our own effective projection for composition, never as stored input.
    if isinstance(value_or_none, dict) and value_or_none.get("setup") == "unasked":
        expected = {"preset": "careful", "categories": preset("careful"), "setup": "unasked"}
        if value_or_none == expected:
            return expected
    return validate(value_or_none)


def _category_list(value):
    if (not isinstance(value, list) or not value or len(value) > len(CATEGORIES) + 1
            or any(not isinstance(x, str) or x not in (*CATEGORIES, "unclassified") for x in value)
            or len(set(value)) != len(value)):
        raise PodError("invalid_effect_categories", "Expected nonempty unique known effect categories")
    return sorted(value)


def categories(kind, complete_effect):
    """Derive the full union; labels may assert agreement but never replace it.

    Money requires ``spending`` = included_subscription or new_commitment. Tool
    effects require a sensitive ``category``. Authorization uses ``action``;
    standing uses every ``scopes`` and ``widening`` entry. Wrappers and added/removed constraints
    recurse through the same classifier. At most 64 effects and depth 8 are accepted.
    """
    remaining = [64]

    def envelope(value, depth):
        row = exact(value, {"kind", "effect"}, {"kind", "effect"}, name="effect")
        return derive(row["kind"], row["effect"], depth)

    def derive(effect_kind, effect, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            raise PodError("invalid_effect", "Complete effect exceeds category traversal bounds")
        if not isinstance(effect_kind, str) or not isinstance(effect, dict):
            raise PodError("invalid_effect", "Effect needs a kind and object")
        result = set()
        if effect_kind in _KINDS:
            result.add(_KINDS[effect_kind])
        elif effect_kind in ("claim", "handoff"):
            result.update(("scope_changes", "coordinator_changes"))
        elif effect_kind in ("question", "governor_exception"):
            result.update(envelope(effect["effect"], depth + 1) if "effect" in effect else ["unclassified"])
        elif effect_kind in ("constraint", "constraints_update"):
            rows = []
            for key in ("added", "removed"):
                values = effect.get(key, [])
                if not isinstance(values, list) or len(values) > 64:
                    raise PodError("invalid_effect", "Constraint effects need bounded added/removed lists")
                rows.extend(values)
            if not rows:
                result.add("unclassified")
            for row in rows:
                if not isinstance(row, dict):
                    raise PodError("invalid_effect", "Complete constraint rows must be objects")
                result.update(derive(row.get("kind", "unclassified"), row.get("effect", {}), depth + 1))
        elif effect_kind == "authorization":
            result.update(derive(effect.get("action", "unclassified"), effect.get("effect", {}), depth + 1))
        elif effect_kind == "standing_authorization":
            scopes = effect.get("scopes")
            if not isinstance(scopes, list) or not scopes or len(scopes) > 64:
                raise PodError("invalid_effect", "Standing authorization needs every affected scope")
            widening = effect.get("widening", [])
            if not isinstance(widening, list) or len(widening) > 64:
                raise PodError("invalid_effect", "Standing widening must be a bounded list")
            for scope in scopes + widening:
                result.update(derive(scope, {}, depth + 1) if isinstance(scope, str) else envelope(scope, depth + 1))
        elif effect_kind in ("tool_permission", "tool_operation"):
            category = effect.get("category")
            if not isinstance(category, str) or category not in SENSITIVE:
                raise PodError("invalid_effect", "Tool effects need their sensitive category")
            result.update(derive("money", {"spending": effect.get("spending")}, depth + 1)
                          if category == "money" else [category])
        elif effect_kind in ("money", "paid_resources"):
            if effect.get("spending") not in ("included_subscription", "new_commitment"):
                raise PodError("invalid_spending", "Money requires explicit spending classification")
            result.add("routine" if effect["spending"] == "included_subscription" else "money")
        elif effect_kind == "completed_cleanup":
            result.add("routine" if effect.get("already_authorized") is True and effect.get("completed") is True
                       else "unclassified")
        else:
            result.add("unclassified")
        if effect_kind == "retirement" and effect.get("irreversible_deletion") is True:
            result.add("irreversible_deletion")
        extra = effect.get("effects", [])
        if not isinstance(extra, list) or len(extra) > 64:
            raise PodError("invalid_effect", "Additional effects must be bounded envelopes")
        for nested in extra:
            result.update(envelope(nested, depth + 1))
        if "categories" in effect and set(_category_list(effect["categories"])) != result:
            raise PodError("effect_category_mismatch", "Declared categories disagree with complete effect")
        if effect_kind not in ("tool_permission", "tool_operation") and "category" in effect:
            if [effect["category"]] != sorted(result):
                raise PodError("effect_category_mismatch", "Declared category disagrees with complete effect")
        return result

    return sorted(derive(kind, complete_effect, 0))


def required(policy, categories):
    """Strictest complete category decider; unclassified is always person."""
    current = effective(policy)
    values = _category_list(categories)
    return max(("person" if key == "unclassified" else current["categories"][key]
                for key in values), key=DECIDERS.index)


def permits(policy, categories, normalized_principal):
    """Evaluate a kernel-normalized principal, never authenticate a public claim.

    Person: {kind: person, channel: session|relayed}. Local cooperative agent:
    {kind: agent, channel: agent_session}. Verified agent: {kind: agent,
    channel: relayed, identity: bounded text, scope: {categories: [...]}}.
    Extra scope kind/resource predicates are integration-owned and must already
    match the effect. The host adapter alone establishes relayed identity/scope.
    """
    decider = required(policy, categories)
    actor = normalized_principal
    if not isinstance(actor, dict):
        return False
    if actor.get("kind") == "person":
        return actor.get("channel") in ("session", "relayed")
    if actor.get("kind") != "agent" or decider == "person":
        return False
    if actor.get("channel") == "agent_session":
        return decider == "any_agent" and set(actor) == {"kind", "channel"}
    if actor.get("channel") != "relayed":
        return False
    identity, scope = actor.get("identity"), actor.get("scope")
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 128 or not isinstance(scope, dict):
        return False
    try:
        allowed = _category_list(scope.get("categories"))
    except PodError:
        return False
    return set(categories) <= set(allowed)


def projection(policy, permissions, pending):
    """Bounded summaries without decision text, principal evidence or rule words.

    Pending summaries use {id, categories, state}; only unresolved states
    pending/waiting are projected. Missing categories conservatively need person.
    """
    from .tool_permissions import validate_memory

    current = effective(policy)
    memory = validate_memory(permissions)
    if not isinstance(pending, list) or len(pending) > 8:
        raise PodError("invalid_pending_decisions", "At most eight pending machine decisions")
    waits = []
    seen = set()
    for row in pending:
        if not isinstance(row, dict):
            raise PodError("invalid_pending_decisions", "Pending decisions must be objects")
        identity = bounded_text(row.get("id"), name="pending_id", limit=128)
        if identity in seen:
            raise PodError("invalid_pending_decisions", "Duplicate pending identity")
        seen.add(identity)
        if row.get("state") in ("answered", "consumed", "refused", "cancelled"):
            continue
        if row.get("state") not in ("pending", "waiting"):
            raise PodError("invalid_pending_decisions", "Unknown pending state")
        effect_categories = _category_list(row.get("categories", [row.get("category", "unclassified")]))
        decider = required(current, effect_categories)
        waits.append({"id": identity, "categories": effect_categories, "required": decider,
                      "waiting_on": "person" if decider == "person" else "agent"})
    setup = current["setup"] == "unasked"
    return {"preset": current["preset"], "categories": dict(current["categories"]),
            "setup": current["setup"], "setup_question": (
                {"text": "Who should decide? Choose Careful, Delegate, Hands-off or Custom.",
                 "choices": ["careful", "delegate", "hands_off", "custom", "skip"], "required": "person"}
                if setup else None),
            "permissions": [{key: row[key] for key in ("tool", "category", "mode")} for row in memory],
            "pending": waits, "waiting_on_person": setup or any(x["waiting_on"] == "person" for x in waits),
            "waiting_on_agent": any(x["waiting_on"] == "agent" for x in waits),
            "every_sensitive_use": "Check current policy and tool permission before every sensitive use."}

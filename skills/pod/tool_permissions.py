"""Pure sensitive-tool memory and invocation evaluation, with no use grants.

An ``allowed`` result is a policy evaluation. The integration must independently
establish decision authority, recheck current bindings, consume any one-use receipt
and commit under its locks before returning permission to execute. Passing JSON to
this module cannot authenticate a principal or consume a pending invocation.
"""

from __future__ import annotations

from . import policy as decision_policy
from .errors import PodError
from .util import bounded_text, exact

MODES = ("hands_off", "ask_each_time")


def _tool(value):
    value = bounded_text(value, name="tool", limit=128)
    if any(char in value for char in "*?[]"):
        raise PodError("invalid_tool", "Tool identities must be exact, without wildcards")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PodError("invalid_tool", "Tool identity must be UTF-8") from None
    return value


def _category(value):
    if not isinstance(value, str) or value not in decision_policy.SENSITIVE:
        raise PodError("invalid_tool_category", "Choose a sensitive tool category")
    return value


def validate_memory(value):
    """Validate stored metadata, not decision authority; return independent copies."""
    if not isinstance(value, list) or len(value) > 32:
        raise PodError("invalid_tool_permissions", "Tool permission memory allows at most 32 entries")
    result, seen = [], set()
    for item in value:
        row = exact(item, {"tool", "category", "mode", "decision"},
                    {"tool", "category", "mode", "decision"}, name="tool_permission")
        tool, category = _tool(row["tool"]), _category(row["category"])
        if row["mode"] not in MODES or category == "money" and row["mode"] == "hands_off":
            raise PodError("invalid_tool_permission", "Money has no hands-off permission")
        if (tool, category) in seen:
            raise PodError("invalid_tool_permissions", "Duplicate tool/category permission")
        seen.add((tool, category))
        result.append({**row, "decision": decision_policy._bounded_object(row["decision"], name="tool_decision")})
    return result


def remember(memory, tool, category, mode, decision):
    """Propose one exact memory replacement; integration establishes authority/CAS."""
    rows = validate_memory(memory)
    new = validate_memory([{"tool": tool, "category": category, "mode": mode, "decision": decision}])[0]
    for index, row in enumerate(rows):
        if (row["tool"], row["category"]) == (tool, category):
            rows[index] = new
            break
    else:
        rows.append(new)
    return validate_memory(rows)


def _revisions(value):
    names = {"policy", "memory", "decision_generation", "invocation"}
    row = exact(value, names, names, name="tool_revisions")
    if type(row["decision_generation"]) is not int or row["decision_generation"] < 0:
        raise PodError("invalid_tool_revisions", "Decision generation must be a nonnegative integer")
    for key in ("policy", "memory", "invocation"):
        bounded_text(row[key], name="tool_" + key, limit=128)
    return dict(row)


def check(policy, memory, tool, category, operation, *, spending=None, pending=None, revisions=None):
    """Evaluate memory then an independently attested exact operation approval.

    ``revisions`` is {policy, memory, decision_generation, invocation}: opaque current
    revisions plus a kernel-assigned invocation identity. Approved ``pending`` is
    {id, purpose: operation, tool, category, operation, spending, revisions,
    state: approved, principal}. Its id must equal revisions.invocation; spending is
    null outside money. States consumed/refused never allow execution, including
    when the current policy would otherwise allow hands-off. A memory-purpose answer
    is not an operation answer. Optional fields on pending are not accepted here;
    integration adapts its bounded durable question to this evaluated value shape.

    This function checks category scope only. The integration must attest the actor
    and match decision kind, resources, payload, current generation and receipt.
    """
    current = decision_policy.effective(policy)
    rows = validate_memory(memory)
    tool, category = _tool(tool), _category(category)
    bounded_text(operation, name="tool_operation", limit=256)
    try:
        operation.encode("utf-8")
    except UnicodeError:
        raise PodError("invalid_tool_operation", "Operation must be UTF-8") from None
    if category != "money" and spending is not None:
        raise PodError("invalid_spending", "Spending classification is only for money operations")
    effect_categories = decision_policy.categories("tool_operation", {"category": category, "spending": spending})
    decider = decision_policy.required(current, effect_categories)
    base = {"tool": tool, "category": category, "categories": effect_categories, "required": decider}

    def result(status, **extra):
        return {**base, "status": status, **extra}

    live = _revisions(revisions) if revisions is not None else None
    approved = False
    if pending is not None:
        # Metadata from an answered first-use question must never act as a grant.
        if isinstance(pending, dict) and pending.get("purpose") == "memory":
            return result("refused", reason="memory_choice_is_not_operation_approval")
        names = {"id", "purpose", "tool", "category", "operation", "spending", "revisions", "state", "principal"}
        row = exact(pending, names, names, name="tool_pending")
        if row["purpose"] != "operation" or row["state"] not in ("approved", "pending", "refused", "consumed"):
            return result("refused", reason="invalid_operation_approval")
        if row["state"] in ("refused", "consumed"):
            return result("refused", reason="operation_" + row["state"])
        if live is None:
            return result("refused", reason="operation_revisions_required")
        bound = _revisions(row["revisions"])
        expected = (live["invocation"], tool, category, operation, spending, live)
        actual = (row["id"], row["tool"], row["category"], row["operation"], row["spending"], bound)
        if actual != expected:
            return result("refused", reason="operation_binding_mismatch")
        if row["state"] == "approved":
            if not decision_policy.permits(current, effect_categories, row["principal"]):
                return result("refused", reason="operation_principal_not_permitted")
            approved = True

    if category == "money" and spending == "included_subscription":
        return result("allowed", mode="included_subscription", consume_required=False)
    entry = next((row for row in rows if (row["tool"], row["category"]) == (tool, category)), None)
    if entry is None:
        return result("permission_question", purpose="memory",
                      text=(f"Remember that each new spending commitment with {tool} needs the person?"
                            if category == "money" else
                            f"May agents use {tool} for {category} hands-off, or must they ask each time?"),
                      choices=["ask_each_time"] if category == "money" else list(MODES))
    if approved:
        return result("allowed", mode="one_operation", invocation=live["invocation"], consume_required=True)
    if entry["mode"] == "hands_off" and decider == "any_agent":
        return result("allowed", mode="hands_off", consume_required=False)
    return result("operation_question", purpose="operation", mode=entry["mode"],
                  text=f"Authorize this pending {category} operation with {tool}?",
                  binding={"tool": tool, "category": category, "operation": operation,
                           "spending": spending, "revisions": live},
                  choices=["approve", "refuse"])

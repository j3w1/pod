"""Revision-bound context reuse, steering and non-mutating feedback."""

from __future__ import annotations

from typing import Any

from .errors import PodError
from .util import digest, exact


def execution_brief(criteria: list[str], coverage: list[dict]) -> dict:
    """Keep each original criterion tied to a check or explicit dependency."""
    if (not isinstance(criteria, list) or not criteria or len(criteria) > 64
            or any(not isinstance(c, str) or not c or len(c) > 256 for c in criteria)
            or len(set(criteria)) != len(criteria) or not isinstance(coverage, list)
            or len(coverage) != len(criteria)):
        raise PodError("invalid_brief", "Original criteria must be bounded and unique")
    rows = {}
    for raw in coverage:
        row = exact(raw, {"criterion", "check", "dependency"}, {"criterion"}, name="coverage")
        if row["criterion"] not in criteria or row["criterion"] in rows:
            raise PodError("invalid_brief", "Coverage must bind one original criterion")
        for field in ("check", "dependency"):
            if field in row and (not isinstance(row[field], str) or not row[field].strip()
                                 or len(row[field]) > 512):
                raise PodError("invalid_brief", "Coverage description is invalid")
        if not row.get("check") and not row.get("dependency"):
            raise PodError("invalid_brief", "Criterion needs a check or explicit dependency")
        rows[row["criterion"]] = row
    if set(rows) != set(criteria):
        raise PodError("invalid_brief", "Original criterion is missing")
    return {"schema": "pod-execution-brief/v1", "criteria": list(criteria),
            "coverage": [rows[c] for c in criteria], "digest": digest([rows[c] for c in criteria])}


def bind_context(*, candidate: str, instructions: list[dict], requirements: str,
                 sources: list[dict], policy_revision: str, summary: str) -> dict:
    if not isinstance(summary, str) or len(summary) > 8192:
        raise PodError("invalid_context", "Context summary is too large")
    binding = {"candidate": candidate, "instructions": instructions,
               "requirements": requirements, "sources": sources,
               "policy_revision": policy_revision}
    return {"schema": "pod-context-summary/v1", "binding": binding,
            "binding_digest": digest(binding), "summary": summary}


def context_valid(record: dict, current_binding: dict) -> bool:
    exact(record, {"schema", "binding", "binding_digest", "summary"},
          {"schema", "binding", "binding_digest", "summary"}, name="context")
    return (record["schema"] == "pod-context-summary/v1"
            and digest(record["binding"]) == record["binding_digest"]
            and record["binding"] == current_binding)


def steer(plan: dict, *, changes: dict, authorized_acceptance_change: bool) -> dict:
    exact(plan, {"schema", "revision", "criteria", "assignments", "candidate"},
          {"schema", "revision", "criteria", "assignments", "candidate"}, name="plan")
    if plan["schema"] != "pod-plan/v1" or type(plan["revision"]) is not int:
        raise PodError("invalid_plan", "Plan revision is invalid")
    exact(changes, {"criteria", "assignments", "candidate", "reason"},
          {"reason"}, name="steering")
    if "criteria" in changes and changes["criteria"] != plan["criteria"] and not authorized_acceptance_change:
        raise PodError("acceptance_change_requires_authorization", "Changed acceptance criteria require authorization")
    result = {**plan, **{key: value for key, value in changes.items() if key in ("criteria", "assignments", "candidate")}}
    result["revision"] = plan["revision"] + 1
    affected = [a for a in plan["assignments"] if a not in result["assignments"]]
    return {"plan": result, "affected": affected, "proof_state": "revalidate", "reason": changes["reason"]}


def feedback(observations: list[dict]) -> dict:
    if len(observations) < 3 or any(not isinstance(o, dict) or "outcome" not in o or "route" not in o for o in observations):
        return {"suggestion": None, "reason": "insufficient comparable observed outcomes"}
    routes = {o["route"] for o in observations}
    if len(routes) != 1:
        return {"suggestion": None, "reason": "mixed routes are not a meaningful pattern"}
    if all(o["outcome"] == "blocked" for o in observations):
        return {"suggestion": f"Review the saved preference for {next(iter(routes))}", "writes": False}
    return {"suggestion": None, "reason": "no repeated actionable pattern"}

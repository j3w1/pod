"""Deterministic evaluation of recorded coordination traces (R91)."""

from __future__ import annotations

from typing import Any

from .errors import PodError


# --------------------------------------------------------------------------- recorded traces

TRACE_SCHEMA = "pod-trace/v1"


def evaluate_trace(trace: Any) -> dict:
    """Scripted record checks over a sanitized recorded trace (R91, A162).

    Slot filling, amplification and scope inflation are findings the boundary refuses, so
    any occurrence fails the trace. Serialization and churn are flags. Wall time and
    admission counts are observations only. A trace without obligation maps, such as a
    0.5 native-observation capture, is reported as not evaluable rather than as a pass.
    """
    from .obligations import _exact, serialization_flags, TERMINAL, CHURN_THRESHOLD

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

"""Deterministic large logical contexts; synthetic fixtures, never native evidence."""
from copy import deepcopy
import json

from pod.ledger import _empty, _validate_context
from pod.util import digest, encoded


def size(value):
    return len(encoded(value, compact=True))


def synthetic_state(count=60):
    state = _empty()
    current = "f" * 40
    candidates = [f"{i + 1:040x}" for i in range(20)] + [current]
    obligations = [{"id": f"A{i}", "kind": "assurance", "state": "active",
                    "executor": "coordinator", "evidence": [], "receipts": [], "findings": []}
                   for i in range(7)]
    state["owner"] = "synthetic-owner"
    state["checkpoint"] = {"schema": "pod-checkpoint/v3", "objective": "synthetic-capacity",
                           "candidate": current, "seq": 80, "revision": 3,
                           "native_refs": [{"runId": "synthetic-run", "runtime": "synthetic-runtime"}],
                           "obligations": obligations, "proposals": [], "closure": None, "quiescence": None,
                           "governance_history": {"candidates": candidates,
                               "decisions": [{"seq": i + 1, "base_ref": "origin/main", "base": candidate,
                                              "kind": "snapshot", "provenance": "user_direct", "channel": "session",
                                              "instruction": "synthetic-owner-decision" * 20}
                                             for i, candidate in enumerate(candidates)]}}
    for i in range(count):
        key, candidate = f"attempt-{i:03}", candidates[i % 20]
        role = "review" if i < 42 else "implement" if i < 48 else "investigate"
        served = f"A{i % 7}"
        binding = {"policy_revision": "synthetic-policy", "governance": "1" * 64,
                   "definitions": {served: "2" * 64}, "dependencies": ["fixture"],
                   "sources": [{"path": "synthetic/" + ("s" * 70) + str(n), "state": "present",
                                "digest": "3" * 64} for n in range(30)], "environment": "synthetic"}
        native = {"runId": "synthetic-run", "taskId": f"synthetic-task-{i}",
                  "dispatchId": f"synthetic-dispatch-{i}", "workerId": f"synthetic-worker-{i}",
                  "worktreeId": "synthetic-repo::/synthetic/project", "terminalHandle": f"synthetic-terminal-{i}"}
        observation = {"schema": "pod-report/v1", "assignment": f"synthetic-packet-{i}",
                       "attempt": native["dispatchId"], "candidate": candidate, "outcome": "succeeded",
                       "scope": ["synthetic"], "files": [], "checks": ["fixture"], "failures": [],
                       "evidence": [], "uncertainty": [], "questions": []}
        report = {"outcome": "succeeded", "status": "validated_observation", "attempt": native["dispatchId"],
                  "observation": observation, "result_commit": None, "consumed_at": "synthetic-time"}
        report["observation_digest"] = digest({"status": report["status"], "observation": observation,
            "native_binding": {"runtime": "synthetic-runtime", **{k: native[k] for k in ("runId", "taskId", "dispatchId", "workerId")}}})
        row = {"schema": "pod-admission/v4", "admission_id": key, "objective": "synthetic-capacity",
               "state": "bound", "owner": "synthetic-owner", "runtime": "synthetic-runtime",
               "candidate": candidate, "role": role, "serves": [served], "binding": binding,
               "native_binding": native, "run_id": "synthetic-run", "task_id": native["taskId"],
               "plan_revision": "synthetic-plan", "packet_id": observation["assignment"],
               "worktree": "path:/synthetic/project", "reuse_of": None, "request_uuid": None,
               "request": {"model": "synthetic-model"},
               "route_decision": {"reason": "synthetic-route-" * 60, "effective_unknown": False, "route_mismatch": False},
               "effective_evidence": {"synthetic": "effective" * 160},
               "recovery": {"synthetic": "frozen-source" * 450}, "report": report,
               "disposition": {"kind": "discarded", "reason": "synthetic-result-obsolete"} if role == "implement" else None,
               "error": None, "failures": [], "created_at": "synthetic-time", "updated_at": "synthetic-time",
               "boundary": {"paths": ["synthetic"], "surfaces": []}, "map_revision": 1,
               "result": None, "changed_paths": [], "boundary_exceeded": [], "admitted_seq": i + 1}
        state["admissions"][key] = row
        if role == "review":
            obligations[i % 7]["receipts"].append({"reported_seq": i + 2,
                "evidence": {"attempt": key, "candidate": candidate, "binding": deepcopy(binding),
                             "definition": "2" * 64, "governance": "1" * 64}})
    return _validate_context(state)


def inline_copies(physical, logical):
    """Pod 0.21.16's physical shape: every satisfied row's receipt copy of current proof inline."""
    from pod.history import is_reference
    shaped = deepcopy(physical)
    current = logical["checkpoint"]["candidate"]
    for old, new in zip(logical["checkpoint"]["obligations"], shaped["checkpoint"]["obligations"]):
        if is_reference(new) or old.get("state") != "satisfied":
            continue
        selected = [json.dumps(item, sort_keys=True) for item in old.get("evidence", [])]
        for index, receipt in enumerate(old.get("receipts", [])):
            evidence = receipt.get("evidence") or {}
            if evidence.get("candidate") == current or json.dumps(evidence, sort_keys=True) in selected:
                new["receipts"][index] = deepcopy(receipt)
    return shaped

"""Deterministic large logical contexts; synthetic fixtures, never native evidence."""
from copy import deepcopy
import json

from pod.errors import PodError
from pod.ledger import _empty, _validate_context
from pod.util import digest, encoded


# Preceding encoder thresholds are fixture vocabulary only.
WARN_AT = 262144
COMPACT_AT = 327680

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


# Preceding-format fixture construction only. No production path imports this.
from pod.history import CONTEXT_LIMIT, FORMAT, MARKER, PART_BYTES, _bytes, _sha, _fail, _require_size
import base64

def _review_consumed(state, key, row):
    serves = row.get("serves")
    if not isinstance(serves, list) or len(serves) != 1:
        return False
    return any(ob.get("id") == serves[0] and ob.get("kind") == "assurance"
               and any(item.get("evidence", {}).get("attempt") == key
                       and ("reported_seq" in item or "accepted_seq" in item)
                       for item in ob.get("receipts", []))
               for ob in (state.get("checkpoint") or {}).get("obligations", []))


def _review_settled(state, key, row):
    """Any-outcome review: its served assurance holds its findings, each required one resolved."""
    serves = row.get("serves")
    obligations = (state.get("checkpoint") or {}).get("obligations", [])
    if (not isinstance(serves, list) or len(serves) != 1 or not isinstance(obligations, list)
            or not any(isinstance(ob, dict) and ob.get("id") == serves[0] and ob.get("kind") == "assurance"
                       for ob in obligations)):
        return False
    from pod.lineage import Lineage
    try:
        lineage = Lineage(obligations)
        findings = lineage.findings(serves[0])
        for finding in findings:
            if finding.get("attempt") != key or finding.get("triage") != "required_correction":
                continue
            # A frozen finding keeps its bytes; the effective correction decides resolution.
            child = lineage.rows.get(lineage.effective(finding.get("correction")))
            owner_withdrawn = (isinstance(child, dict) and child.get("state") == "withdrawn"
                               and (child.get("withdrawal") or {}).get("by") == "user_direct")
            if not owner_withdrawn and not isinstance(finding.get("resolved_seq"), int):
                return False
    except (PodError, AttributeError, KeyError, TypeError):
        return False  # An unreadable lineage cannot prove the corrections settled.
    return True


def _proposals_settled(state, key):
    """A reconciled investigation leaves no unadopted proposal from its report."""
    proposals = (state.get("checkpoint") or {}).get("proposals", [])
    return isinstance(proposals, list) and not any(
        not isinstance(row, dict) or row.get("origin_ref") == key[:64] and row.get("status") != "adopted"
        for row in proposals)


OUTCOMES = ("succeeded", "partial", "failed", "blocked", "uncertain")


def admission_movable(state, key, row, protected=frozenset()) -> bool:
    """Conservative, role-aware predicate for consumed results.

    A consumed report proves prior native settlement; archiving does not
    change the admission's bound state or current native outstanding predicate.
    Any freshly observed outstanding/native or Governor dependency vetoes a move.
    """
    from pod.util import route_hold
    candidate = (state.get("checkpoint") or {}).get("candidate")
    report = row.get("report") or {}
    if (key in protected or row.get("state") not in ("closed", "bound")
            or not row.get("candidate")
            or report.get("status") not in ("validated_observation", "reconciliation_required")
            or report.get("outcome") not in OUTCOMES
            or row.get("route_decision", {}).get("effective_unknown") is True
            or route_hold(row)
            or any(not failure.get("cleared_at") for failure in row.get("failures", []))):
        return False
    role = row.get("role")
    if row.get("candidate") == candidate and (role != "review" or not _review_consumed(state, key, row)):
        return False
    if role == "implement":
        return row.get("disposition") is not None
    if role == "investigate":
        if report.get("status") == "reconciliation_required":
            return _proposals_settled(state, key)
        return report.get("outcome") == "succeeded"
    if role != "review" or report.get("status") != "validated_observation":
        return False
    if report.get("outcome") == "succeeded":
        return _review_consumed(state, key, row)
    return _review_settled(state, key, row)


def compact(logical_state, *, protected_admissions=(), force=False,
            archive_retirement=False, trigger_bytes=COMPACT_AT, current_bytes=0,
            protect_receipts=False, protected_candidates=(), maintenance=(), require_fit=True):
    """Return (physical record, {file_digest: exact_file_bytes}); never mutate input.

    Once triggered, move every eligible slot (hysteresis without a second arbitrary
    target). The logical admission/receipt/governance count contracts never change.
    Retirement-only fallback may move the immutable entry, never unsettled data.
    """
    if "history" in logical_state:
        _fail("history_invalid", "compact requires hydrated logical state")
    record = deepcopy(logical_state)
    if not force and not maintenance and len(_bytes(record)) < trigger_bytes:
        return record, {}
    chunks = {}

    def reference(value):
        proposed = {}
        ref = _reference(value, proposed)
        if len(_bytes(ref)) >= len(_bytes(value)):
            return deepcopy(value)
        chunks.update(proposed)
        return ref

    protected = frozenset(protected_admissions)
    movable = set()
    for key, row in logical_state.get("admissions", {}).items():
        if admission_movable(logical_state, key, row, protected):
            movable.add(key)
            record["admissions"][key] = reference(row)
    checkpoint = logical_state.get("checkpoint") or {}
    current = checkpoint.get("candidate")
    target = record.get("checkpoint") or {}
    logical_rows = {row["id"]: row for row in checkpoint.get("obligations", [])
                    if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for index, (old_ob, new_ob) in enumerate(zip(checkpoint.get("obligations", []), target.get("obligations", []))):
        if old_ob.get("state") in ("withdrawn", "renewed"):
            target["obligations"][index] = reference(old_ob)
            continue
        if old_ob.get("kind") == "assurance" and old_ob.get("findings") and type(checkpoint.get("seq")) is int:
            from pod.assurance import validate_findings
            try:
                validate_findings(old_ob, logical_rows, seq=checkpoint["seq"])
            except PodError:
                pass  # Unvalidated observations cannot authorize a new history position.
            else:
                for finding_index, finding in enumerate(old_ob["findings"]):
                    new_ob["findings"][finding_index] = reference(finding)
        selected = {json.dumps(item, sort_keys=True) for item in old_ob.get("evidence", [])}
        satisfied = old_ob.get("state") == "satisfied"
        for index, receipt in enumerate(old_ob.get("receipts", [])):
            evidence = receipt.get("evidence") or {}
            candidate = evidence.get("candidate")
            copy = json.dumps(evidence, sort_keys=True) in selected
            if protect_receipts or candidate in protected_candidates or not candidate:
                continue
            # Current/selected proof stays inline, except a satisfied row's receipt copy:
            # the row itself keeps that evidence and reuse inline for every reader.
            if not satisfied and (candidate == current or copy):
                continue
            consumed = "reported_seq" in receipt or "accepted_seq" in receipt
            if old_ob.get("kind") == "assurance" and satisfied and copy:
                attempt = evidence.get("attempt")
                eligible = (consumed and isinstance(attempt, str) and attempt in logical_state.get("admissions", {})
                            and attempt not in protected
                            and receipt.get("reuse") in (None, old_ob.get("reuse")))
            elif old_ob.get("kind") == "assurance":
                eligible = (candidate != current and receipt.get("reuse") is None
                            and evidence.get("attempt") in movable and consumed)
            else:
                # Failed or unknown tests stay inline.
                eligible = evidence.get("status") == "PASS" and "accepted_seq" in receipt
            if eligible:
                new_ob["receipts"][index] = reference(receipt)
    old_history = checkpoint.get("governance_history") or {}
    for field in ("candidates", "decisions"):
        entries = old_history.get(field, [])
        for index, value in enumerate(entries[:-1]):
            if field == "candidates" and value == current:
                continue
            target["governance_history"][field][index] = reference(value)
    for index, value in enumerate(checkpoint.get("revision_decisions", [])[:-1]):
        target["revision_decisions"][index] = reference(value)
    if archive_retirement and "historical_retirement" in record:
        record["historical_retirement"] = reference(logical_state["historical_retirement"])
    if chunks:
        record["history"] = dict(FORMAT)
    if maintenance:
        record.setdefault("history", dict(FORMAT))["maintenance"] = _reference(list(maintenance), chunks)
        if any("decision_binding" in row.get("authority", {}) for row in maintenance):
            record["history"]["decision_channels"] = 5
    if not maintenance and len(_bytes(record)) >= len(_bytes(logical_state)):
        record, chunks = deepcopy(logical_state), {}
    if require_fit:
        _require_size(record, current_bytes=current_bytes)
    return record, chunks


def _reference(value, chunks):
    payload = _bytes(value)
    parts = []
    for offset in range(0, len(payload), PART_BYTES):
        part = payload[offset:offset + PART_BYTES]
        chunk = _bytes({"schema": "pod-history-chunk/v1",
                        "data": base64.b64encode(part).decode("ascii")})
        address = _sha(chunk)
        chunks[address] = chunk
        parts.append(address)
    return {MARKER: {"sha256": _sha(payload), "parts": parts, "bytes": len(payload)}}




def publish_preceding(path, record, chunks):
    """Install preceding-format bytes in a disposable compatibility fixture."""
    from pod.history import ChunkStore
    from pod.util import atomic_json
    ChunkStore(path.parent).put(chunks)
    atomic_json(path, record, limit=CONTEXT_LIMIT, compact=True)

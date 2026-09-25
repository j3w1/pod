"""Evidence receipts, review bindings, triage and assurance qualification."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .errors import PodError
from .util import digest


def _source_entries(value: Any, name: str) -> list[dict]:
    from .obligations import _exact, _path, refuse
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} sources are a bounded list")
    rows = []
    for item in value:
        entry = _exact(item, {"path", "state", "sha256"}, {"path", "state"}, "evidence source")
        _path(entry["path"], code="obligation_invalid")
        if entry["state"] not in ("present", "absent", "unavailable"):
            raise refuse("obligation_invalid", "malformed", "an evidence source state is unsupported")
        rows.append(dict(entry))
    return rows


def _dependencies(value: Any, name: str) -> list[str]:
    from .obligations import _text, refuse
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} dependencies are a bounded list")
    return [_text(item, "dependency", limit=512) for item in value]


def _evidence_record(ob: dict, raw: Any) -> dict:
    """The detailed pod-evidence/v1 record, structurally joined to this obligation (R41).

    The record's `criterion` names the obligation id; its check text may differ from the
    obligation's outcome description, because Pod cannot judge what a test proves. What Pod
    keeps is the binding: candidate, sources, effective Governor policy, dependencies,
    environment and the command and result that produced the status.
    """
    from .obligations import _text, refuse
    from .records import evidence_record
    try:
        row = evidence_record({k: v for k, v in raw.items() if k != "governance"}
                              if isinstance(raw, dict) else raw)
    except PodError as exc:
        raise refuse("obligation_invalid", "malformed", str(exc), obligation=ob["id"]) from exc
    if row["criterion"] != ob["id"]:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "an evidence record names the obligation it serves as its criterion",
                     obligation=ob["id"], criterion=str(row["criterion"])[:64])
    for field in ("candidate", "policy_revision", "environment", "check", "command", "result", "timestamp",
                  "reference"):
        _text(row[field], field)
    if "definition" not in row:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "map evidence names the obligation definition it verified", obligation=ob["id"])
    record = dict(row)
    record["sources"] = _source_entries(row["sources"], "evidence")
    record["dependencies"] = _dependencies(row["dependencies"], "evidence")
    return record


def _stamp_evidence(ob: dict, prior: dict | None, ctx: dict, gov: str, rebind: bool,
                    *, reported_attempt: str | None = None, seq: int) -> list[dict]:
    """Keep immutable receipt identities even when a caller omits the selected evidence.

    History is inside the existing obligation record, bounded by MAX_EVIDENCE, and is
    never evicted or accepted from the caller. Full history refuses another receipt;
    it cannot silently make a forgotten receipt new again.
    """
    from .obligations import MAX_EVIDENCE, _admissions, _exact, _text, refuse
    identity = "attempt" if ob["kind"] == "assurance" else "reference"
    receipts = {item["evidence"][identity]: deepcopy(item) for item in (prior or {}).get("receipts", [])}
    if reported_attempt is not None:
        admission = _admissions(ctx)[reported_attempt]
        if review_completed(admission):
            binding = admission.get("binding") or {}
            base = {"attempt": reported_attempt, "candidate": admission.get("candidate"),
                    "binding": deepcopy(admission.get("binding")),
                    "definition": binding.get("definitions", {}).get(ob["id"]),
                    "governance": binding.get("governance")}
            previous = receipts.get(reported_attempt)
            if previous is not None and previous["evidence"] != base:
                raise refuse("obligation_invalid", "receipt_conflict",
                             "a consumed review cannot change its bound receipt", obligation=ob["id"],
                             receipt=reported_attempt)
            receipts[reported_attempt] = {**(previous or {}), "evidence": base,
                                          "reported_seq": (previous or {}).get("reported_seq", seq)}
    stamped = []
    for raw in ob.get("evidence", []):
        if ob["kind"] == "assurance":
            row = _exact(raw, {"attempt", "candidate", "binding", "governance", "definition"},
                         {"attempt"}, "assurance evidence")
            _text(row["attempt"], "attempt", limit=128)
            admission = _admissions(ctx).get(row["attempt"])
            admission = admission if isinstance(admission, dict) else {}
            base = {"attempt": row["attempt"], "candidate": admission.get("candidate"),
                    "binding": deepcopy(admission.get("binding")),
                    "definition": (admission.get("binding") or {}).get("definitions", {}).get(ob["id"])}
        else:
            base = _evidence_record(ob, raw)
        key = base[identity]
        previous = receipts.get(key)
        if previous and base != {k: v for k, v in previous["evidence"].items() if k != "governance"}:
            raise refuse("obligation_invalid", "receipt_conflict",
                         "an observed receipt cannot change its definition, candidate or content",
                         obligation=ob["id"], receipt=key)
        stamp = (previous["evidence"]["governance"] if previous else
                 (base.get("binding") or {}).get("governance") if identity == "attempt" else gov)
        if rebind:
            stamp = gov
        evidence = {**base, "governance": stamp}
        receipts[key] = {**(previous or {}), "evidence": evidence}
        stamped.append(evidence)
    if len(receipts) > MAX_EVIDENCE:
        raise refuse("obligation_invalid", "receipt_limit", "the bounded receipt history is full",
                     obligation=ob["id"])
    ob["receipts"] = list(receipts.values())
    return stamped


def binding_current(binding: Any, ctx: dict) -> bool:
    """R41: effective Governor policy, environment, dependencies and sources still hold."""
    if not isinstance(binding, dict):
        return False
    verification = ctx.get("verification")
    current_source = ctx.get("source_current")
    if (not isinstance(verification, dict) or binding.get("policy_revision") is None
            or binding.get("policy_revision") != ctx.get("policy_revision")
            or binding.get("environment") != verification.get("environment")
            or not isinstance(binding.get("dependencies"), list)
            or not set(binding["dependencies"]) <= set(verification.get("dependencies", []))
            or not isinstance(binding.get("sources"), list)):
        return False
    for entry in binding["sources"]:
        if not isinstance(entry, dict) or current_source is None or current_source(entry) is not True:
            return False
    return True


def review_completed(admission: Any) -> bool:
    """A review attempt binds assurance only when its report is a completed, validated observation.

    Completion is not approval: a completed review may carry findings, which triage turns
    into corrections that the assurance obligation waits on.
    """
    report = admission.get("report") if isinstance(admission, dict) else None
    return (isinstance(report, dict) and report.get("outcome") == "succeeded"
            and report.get("status") == "validated_observation")


def _reuse_valid(ob: dict, ctx: dict, candidate: str) -> bool:
    from .obligations import definition_id
    reuse = ob.get("reuse")
    return (isinstance(reuse, dict) and reuse.get("to") == candidate
            and reuse.get("definition") == definition_id(ob) and isinstance(reuse.get("delta"), list))


def _bind_reuse(ob: dict, prior: dict | None, ctx: dict) -> dict | None:
    """Explicit REUSE: the Git delta between candidates must leave the obligation's scope unaffected."""
    from .obligations import _exact, _path, _text, definition_id, overlap, refuse
    if "reuse" not in ob:
        return None
    raw = _exact(ob["reuse"], {"from", "to", "delta", "definition"}, {"from"}, "reuse")
    source = _text(raw["from"], "reuse from", limit=128)
    current = ctx.get("candidate")
    scope = ob["scope"] if ob["kind"] == "assurance" else ob["boundary"]
    if not scope["paths"]:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "reuse needs a declared path scope to show it is unaffected", obligation=ob["id"])
    previous = (prior or {}).get("reuse")
    if (isinstance(previous, dict) and previous.get("from") == source and previous.get("to") == current
            and previous.get("definition") == definition_id(ob)):
        return previous
    delta_reader: Callable | None = ctx.get("git_delta")
    delta = delta_reader(source, current) if delta_reader is not None and isinstance(current, str) else None
    if delta is None:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "the Git delta between the candidates cannot be read, so reuse is unproven",
                     obligation=ob["id"], candidate=source)
    touched = overlap({"paths": [_path(path, code="obligation_unaccounted") for path in delta], "surfaces": []},
                      {"paths": scope["paths"], "surfaces": []})
    if touched["paths"]:
        raise refuse("obligation_unaccounted", "evidence_invalidated",
                     "the candidate delta touches the obligation's scope; it needs fresh evidence",
                     obligation=ob["id"], paths=",".join(touched["paths"][:8]))
    return {"from": source, "to": current, "delta": sorted(delta)[:256], "definition": definition_id(ob)}


def evidence_valid(ob: dict, ctx: dict, gov: str, candidate: str | None = None) -> bool:
    """Whether a satisfied obligation's evidence is passing and currently valid (R41)."""
    from .obligations import _admissions, _outstanding, definition_id
    current = ctx.get("candidate") if candidate is None else candidate
    rows = ob.get("evidence", [])
    if not rows:
        return False
    allowed = {current}
    if _reuse_valid(ob, ctx, current):
        allowed.add(ob["reuse"]["from"])
    for row in rows:
        if (row.get("definition") != definition_id(ob)
                or row.get("governance") != gov or row.get("candidate") not in allowed):
            return False
        if ob["kind"] == "assurance":
            admission = _admissions(ctx).get(row["attempt"])
            # Both reservation and report consumption must follow correction
            # resolution. Wall-clock admission stamps provide neither ordering.
            # Older accepted receipts can use accepted_seq as their report bound.
            receipt = next((item for item in ob.get("receipts", [])
                            if item["evidence"].get("attempt") == row["attempt"]), None)
            proof_seq = (receipt or {}).get("reported_seq", (receipt or {}).get("accepted_seq"))
            admitted_seq = admission.get("admitted_seq") if isinstance(admission, dict) else None
            superseded = any(
                finding.get("triage") == "required_correction"
                and (not isinstance(proof_seq, int) or not isinstance(admitted_seq, int)
                     or not isinstance(finding.get("recorded_seq"), int)
                     or not isinstance(finding.get("resolved_seq"), int)
                     or finding["resolved_seq"] < finding["recorded_seq"]
                     or proof_seq <= finding["resolved_seq"]
                     or admitted_seq <= finding["resolved_seq"])
                for finding in ob.get("findings", []))
            if (not isinstance(admission, dict) or admission.get("role") != "review"
                    or admission.get("serves") != [ob["id"]] or row["attempt"] in _outstanding(ctx)
                    or not review_completed(admission) or admission.get("candidate") != row.get("candidate")
                    or (admission.get("binding") or {}).get("definitions", {}).get(ob["id"]) != row["definition"]
                    or row.get("binding") != admission.get("binding")
                    or not binding_current(admission.get("binding"), ctx) or superseded):
                return False
        elif row.get("status") != "PASS" or not binding_current(row, ctx):
            return False
    return True


def _accepted_assurance_current(ob: dict, ctx: dict, gov: str) -> bool:
    """An accepted proof survives omission and unsatisfied intermediate states."""
    return ob["kind"] == "assurance" and any(
        item.get("accepted_seq") is not None and evidence_valid(
            {**ob, "evidence": [item["evidence"]], "reuse": item.get("reuse")}, ctx, gov)
        for item in ob.get("receipts", []))


def triage(state: dict, value: dict, findings: Any, proposals: Any, ctx: dict, *,
           admission_id: str) -> tuple[dict, frozenset]:
    """R82/R87: review findings become corrections or proposals; reports add only proposals.

    Returns the map value, finding-owned obligation ids, and the consumed review
    marker. Only this report boundary can supply those records to `accept_write`.
    """
    from .obligations import MAX_FINDINGS, PROPOSAL_SOURCES, SEVERITIES, TRIAGE, _admissions, _exact, _ident, _text, refuse
    base = {key: value[key] for key in value if key in ("seq", "obligations", "proposals")}
    obligations = [dict(row) for row in base.get("obligations", state["obligations"])]
    listed = [dict(row) for row in base.get("proposals", state.get("proposals", []))]
    admission = _admissions(ctx).get(admission_id) or {}
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise refuse("obligation_invalid", "malformed", "triage is a bounded list")
    if findings and admission.get("role") != "review":
        raise refuse("obligation_invalid", "malformed", "only a review attempt is triaged", admission=admission_id)
    rows = {row.get("id"): row for row in obligations}
    assurance_id = (admission.get("serves") or [None])[0]
    prior_ids = {ob["id"] for ob in state["obligations"]}
    stored = next((ob.get("findings", []) for ob in state["obligations"] if ob["id"] == assurance_id), [])
    written: set[str | tuple[str, str]] = set()
    records = []
    for raw in findings:
        record = _exact(raw, {"finding", "severity", "triage", "summary", "reason", "correction"},
                        {"finding", "severity", "triage", "summary"}, "finding")
        _ident(record["finding"], "finding")
        _text(record["summary"], "summary")
        if record["severity"] not in SEVERITIES or record["triage"] not in TRIAGE:
            raise refuse("obligation_invalid", "malformed", "severity or triage is unsupported",
                         finding=record["finding"])
        if record["severity"] in ("blocker", "major") and record["triage"] == "advisory":
            if "reason" not in record:
                raise refuse("obligation_invalid", "downgrade_unreasoned",
                             "downgrading a reviewer's blocker or major finding needs a reason",
                             finding=record["finding"])
            _text(record["reason"], "reason", limit=1024)
        entry = {"attempt": admission_id, "finding": record["finding"], "severity": record["severity"],
                 "triage": record["triage"], "summary": record["summary"],
                 "recorded_seq": state["seq"] + 1}
        if any(item.get("attempt") == admission_id and item.get("finding") == record["finding"]
               for item in stored):
            continue
        if "reason" in record:
            entry["reason"] = record["reason"]
        known = {row.get("id") for row in listed}
        if record["triage"] == "required_correction":
            correction = rows.get(record.get("correction"))
            if (correction is None or record["correction"] in prior_ids or correction.get("kind") != "correction"
                    or correction.get("parent") != assurance_id or correction.get("provenance") != "coordinator"):
                raise refuse("obligation_invalid", "no_parent",
                             "a required correction is a new coordinator correction under the assurance obligation",
                             finding=record["finding"])
            correction["finding"] = {"attempt": admission_id, "finding": record["finding"],
                                     "severity": record["severity"]}
            written.add(correction["id"])
            entry["correction"] = record["correction"]
        else:
            if "correction" in record:
                raise refuse("obligation_invalid", "malformed", "an advisory finding becomes a proposal",
                             finding=record["finding"])
            key = "adv-" + digest({"attempt": admission_id, "finding": record["finding"]})[:12]
            if key not in known:
                listed.append({"id": key, "source": "reviewer_advisory", "origin_ref": admission_id[:64],
                               "summary": record["summary"], "status": "open"})
            entry["proposal"] = key
        records.append(entry)
    if records:
        assurance = rows.get(assurance_id)
        if assurance is None:
            raise refuse("obligation_invalid", "malformed", "the reviewed assurance obligation is absent")
        if len(stored) + len(records) > MAX_FINDINGS:
            raise refuse("obligation_invalid", "finding_limit",
                         "the bounded finding history is full", obligation=assurance_id)
        assurance["findings"] = list(stored) + records
        written.add(assurance_id)
    if admission.get("role") == "review":
        written.add(("review_report", admission_id))
    if not isinstance(proposals, list) or len(proposals) > 16:
        raise refuse("obligation_invalid", "malformed", "report proposals are a bounded list")
    for raw in proposals:
        record = _exact(raw, {"summary", "source"}, {"summary"}, "report proposal")
        _text(record["summary"], "summary")
        source = record.get("source", "worker_report")
        if source not in PROPOSAL_SOURCES:
            raise refuse("obligation_invalid", "malformed", "proposal source is unsupported")
        key = "rep-" + digest({"attempt": admission_id, "summary": record["summary"]})[:12]
        if key not in {row.get("id") for row in listed}:
            listed.append({"id": key, "source": source, "origin_ref": admission_id[:64],
                           "summary": record["summary"], "status": "open"})
    out = {**{k: v for k, v in value.items() if k not in ("obligations", "proposals")},
           "obligations": obligations, "proposals": listed}
    return out, frozenset(written)


def label_qualification(state: dict | None, ctx: dict, candidate: str | None) -> dict:
    """R88: the `independently reviewed` label is derived, never asserted."""
    from .obligations import governance_digest
    if not isinstance(state, dict) or state.get("obligations") is None:
        return {"label": "WITHHELD", "assurance_unbound": [{"obligation": None, "gap": "none_recorded"}],
                "withdrawn": [], "required": False}
    gov = governance_digest(state["governance_sources"])
    rows = [ob for ob in state["obligations"] if ob["kind"] == "assurance"]
    live = [ob for ob in rows if ob["state"] != "withdrawn"]
    gaps = []
    if not live:
        gaps.append({"obligation": None, "gap": "none_recorded"})
    for ob in live:
        if ob["state"] != "satisfied":
            gaps.append({"obligation": ob["id"], "gap": "unbound"})
        elif candidate is None or candidate != ctx.get("candidate") or not evidence_valid(ob, ctx, gov, candidate):
            gaps.append({"obligation": ob["id"], "gap": "invalidated"})
    withdrawn = [{"obligation": ob["id"], "provenance": ob["provenance"], **ob["withdrawal"]}
                 for ob in rows if ob["state"] == "withdrawn"]
    # Recorded, non-withdrawn assurance obligations are themselves a review requirement; a
    # caller cannot declare them not required.
    return {"label": "WITHHELD" if gaps else "QUALIFIED", "assurance_unbound": gaps, "withdrawn": withdrawn,
            "required": bool(live)}

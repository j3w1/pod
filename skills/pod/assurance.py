"""Evidence receipts, review bindings, triage and assurance qualification."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .errors import PodError
from .util import digest


def _source_entries(value: Any, name: str, *, obligation: str | None = None) -> list[dict]:
    from .obligations import _exact, _path, refuse
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} sources are a bounded list", record='source entries',
                     **({"obligation": obligation} if obligation is not None else {}))
    rows = []
    for item in value:
        entry = _exact(item, {"path", "state", "sha256"}, {"path", "state"}, "evidence source", obligation=obligation)
        _path(entry["path"], code="obligation_invalid", obligation=obligation)
        if entry["state"] not in ("present", "absent", "unavailable"):
            raise refuse("obligation_invalid", "malformed", "evidence source state allowed: present, absent, unavailable", record='source entries',
                         **({"obligation": obligation} if obligation is not None else {}))
        rows.append(dict(entry))
    return rows


def _dependencies(value: Any, name: str, *, obligation: str | None = None) -> list[str]:
    from .obligations import _text, refuse
    if not isinstance(value, list) or len(value) > 64:
        raise refuse("obligation_invalid", "malformed", f"{name} dependencies are a bounded list", record='dependencies',
                     **({"obligation": obligation} if obligation is not None else {}))
    return [_text(item, "dependency", limit=512, obligation=obligation) for item in value]


def _evidence_record(ob: dict, raw: Any, ctx: dict) -> dict:
    """The detailed pod-evidence/v1 record, structurally joined to this obligation (R41).

    The record's `criterion` names the obligation id; its check text may differ from the
    obligation's outcome description, because Pod cannot judge what a test proves. What Pod
    keeps is the binding: candidate, sources, effective Governor policy, dependencies,
    environment and the command and result that produced the status.
    """
    from datetime import datetime, timezone
    from .obligations import _exact, _text, definition_id, refuse
    from .records import evidence_record
    if isinstance(raw, dict) and "schema" not in raw:
        fields = {"check", "command", "result", "reference", "status", "sources",
                  "dependencies", "environment", "timestamp"}
        required = {"check", "command", "result", "reference"}
        _exact(raw, fields, required, "short evidence", obligation=ob["id"])
        verification = ctx.get("verification") or {}
        raw = {"schema": "pod-evidence/v1", "criterion": ob["id"],
               "candidate": ctx.get("candidate"), "policy_revision": ctx.get("policy_revision"),
               "sources": [], "dependencies": list(verification.get("dependencies", [])),
               "environment": verification.get("environment"), "timestamp": datetime.now(timezone.utc).isoformat(),
               "status": "PASS", "definition": definition_id(ob), **raw}
    try:
        row = evidence_record({k: v for k, v in raw.items() if k != "governance"}
                              if isinstance(raw, dict) else raw)
    except PodError as exc:
        raise refuse("obligation_invalid", "malformed", str(exc), obligation=ob["id"],
                     **{"record": "evidence", **(exc.detail or {})}) from exc
    if row["criterion"] != ob["id"]:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "an evidence record names the obligation it serves as its criterion",
                     obligation=ob["id"], field="criterion")
    for field in ("candidate", "policy_revision", "environment", "check", "command", "result", "timestamp",
                  "reference"):
        _text(row[field], field, obligation=ob["id"])
    if "definition" not in row:
        raise refuse("obligation_invalid", "evidence_unbound",
                     "map evidence names the obligation definition it verified", obligation=ob["id"])
    record = dict(row)
    record["sources"] = _source_entries(row["sources"], "evidence", obligation=ob["id"])
    record["dependencies"] = _dependencies(row["dependencies"], "evidence", obligation=ob["id"])
    return record


def _persisted_review_binding(value: Any, ob: dict) -> dict | None:
    from .obligations import _exact, _text, refuse
    from .records import _sha256
    if value is None:
        return None
    fields = {"policy_revision", "environment", "dependencies", "sources", "governance", "definitions"}
    binding = _exact(value, fields, fields, "assurance binding", obligation=ob["id"])
    for field in ("policy_revision", "environment", "governance"):
        if binding[field] is not None:
            _text(binding[field], field, obligation=ob["id"])
    _source_entries(binding["sources"], "binding", obligation=ob["id"])
    _dependencies(binding["dependencies"], "binding", obligation=ob["id"])
    if not isinstance(binding["definitions"], dict) or not all(isinstance(key, str) and _sha256(value) for key, value in binding["definitions"].items()):
        raise refuse("obligation_invalid", "malformed", "receipt definitions are bound identities", obligation=ob["id"])
    return binding


def _persisted_evidence(ob: dict, raw: Any, ctx: dict) -> dict:
    """Parse a stamped receipt, including retained unselected evidence."""
    from .obligations import _exact, _text, refuse
    from .records import _sha256
    if ob["kind"] != "assurance":
        if not isinstance(raw, dict) or raw.get("schema") != "pod-evidence/v1":
            raise refuse("obligation_invalid", "malformed", "persisted evidence is a complete detailed record", obligation=ob["id"])
        record = _evidence_record(ob, raw, ctx)
        governance = _text(raw.get("governance"), "governance", obligation=ob["id"])
        if not _sha256(governance):
            raise refuse("obligation_invalid", "malformed", "receipt governance is a bound fingerprint", obligation=ob["id"])
        record = {**record, "governance": governance}
    else:
        fields = {"attempt", "candidate", "binding", "governance", "definition"}
        record = _exact(raw, fields, fields, "assurance evidence", obligation=ob["id"])
        _text(record["attempt"], "attempt", limit=128, obligation=ob["id"])
        # Unknown unconsumed attempts can be recorded, but never qualify as proof.
        for field in ("candidate", "governance"):
            if record[field] is not None:
                _text(record[field], field, obligation=ob["id"])
        if record["definition"] is not None and not _sha256(record["definition"]):
            raise refuse("obligation_invalid", "malformed", "receipt definition is a SHA256 identity", obligation=ob["id"])
        if record["governance"] is not None and not _sha256(record["governance"]):
            raise refuse("obligation_invalid", "malformed", "receipt governance is a bound fingerprint", obligation=ob["id"])
        _persisted_review_binding(record["binding"], ob)
    if record != raw:
        raise refuse("obligation_invalid", "malformed", "persisted evidence retains its canonical record", obligation=ob["id"])
    if ob["kind"] == "assurance":
        from .obligations import _admissions
        admission = _admissions(ctx).get(record["attempt"]) or {}
        binding = _persisted_review_binding(admission.get("binding"), ob) or {}
        expected = {"candidate": admission.get("candidate"), "binding": admission.get("binding"),
                    "definition": binding.get("definitions", {}).get(ob["id"])}
        if any(record[field] != expected[field] for field in expected):
            raise refuse("obligation_invalid", "receipt_conflict", "retained review evidence matches its immutable admission", obligation=ob["id"])
    return record


def validate_receipts(ob: dict, ctx: dict, *, seq: int) -> None:
    """One persisted evidence/history contract for ordinary writes and closure reads."""
    from .obligations import MAX_EVIDENCE, _exact, _text, _path, refuse
    from .records import _sha256
    history = ob.get("receipts")
    if not isinstance(history, list) or len(history) > MAX_EVIDENCE:
        raise refuse("obligation_invalid", "malformed", "receipt history is a bounded recorded list", obligation=ob["id"])
    identity = "attempt" if ob["kind"] == "assurance" else "reference"
    receipts = {}
    for raw in history:
        receipt = _exact(raw, {"evidence", "accepted_seq", "reported_seq", "reuse"}, {"evidence"}, "receipt", obligation=ob["id"])
        evidence = _persisted_evidence(ob, receipt["evidence"], ctx)
        key = evidence[identity]
        if key in receipts:
            raise refuse("obligation_invalid", "receipt_conflict", "receipt identities are unique", obligation=ob["id"])
        receipts[key] = receipt
        for field in ("accepted_seq", "reported_seq"):
            if field in receipt and (type(receipt[field]) is not int or not ob["introduced_seq"] <= receipt[field] <= seq):
                raise refuse("obligation_invalid", "malformed", "receipt sequence belongs to the recorded map history", obligation=ob["id"])
        reuse = receipt.get("reuse")
        if reuse is not None:
            reuse = _exact(reuse, {"from", "to", "delta", "definition"}, {"from", "to", "delta", "definition"}, "receipt reuse", obligation=ob["id"])
            for field in ("from", "to"):
                _text(reuse[field], field, limit=128, obligation=ob["id"])
            if not _sha256(reuse["definition"]) or not isinstance(reuse["delta"], list) or len(reuse["delta"]) > 256:
                raise refuse("obligation_invalid", "malformed", "receipt reuse retains its definition and bounded delta", obligation=ob["id"])
            for path in reuse["delta"]:
                _path(path, code="obligation_invalid", obligation=ob["id"])
            if (reuse["from"] != evidence["candidate"] or reuse["definition"] != evidence["definition"]
                    or reuse["delta"] != sorted(set(reuse["delta"]))):
                raise refuse("obligation_invalid", "receipt_conflict", "receipt reuse retains its source proof identity and canonical delta", obligation=ob["id"])
        if ob["kind"] == "assurance" and "accepted_seq" in receipt:
            from .obligations import _admissions
            if not _sha256(evidence["governance"]) or not _sha256(evidence["definition"]) or evidence["binding"] is None:
                raise refuse("obligation_invalid", "receipt_conflict", "accepted review history retains complete proof bindings", obligation=ob["id"])
            for field in ("policy_revision", "environment"):
                _text(evidence["binding"][field], field, obligation=ob["id"])
            if not _sha256(evidence["binding"]["governance"]):
                raise refuse("obligation_invalid", "receipt_conflict", "accepted review binding retains its governance identity", obligation=ob["id"])
            admission = _admissions(ctx).get(evidence["attempt"]) or {}
            reported = receipt.get("reported_seq", receipt["accepted_seq"])
            admitted = admission.get("admitted_seq")
            if (reported > receipt["accepted_seq"]
                    or admitted is not None and (type(admitted) is not int or not ob["introduced_seq"] <= admitted <= reported)
                    or not review_completed(admission)):
                raise refuse("obligation_invalid", "receipt_conflict", "accepted review receipt retains admission and report ordering", obligation=ob["id"])
    for raw in ob.get("evidence", []):
        evidence = _persisted_evidence(ob, raw, ctx)
        receipt = receipts.get(evidence[identity])
        if receipt is None or receipt["evidence"] != evidence:
            raise refuse("obligation_invalid", "receipt_conflict", "selected evidence matches its immutable receipt", obligation=ob["id"])
        if ob["state"] == "satisfied" and ("accepted_seq" not in receipt or receipt.get("reuse") != ob.get("reuse")):
            raise refuse("obligation_invalid", "receipt_conflict", "satisfied evidence retains its acceptance and reuse binding", obligation=ob["id"])


def validate_findings(ob: dict, rows: dict, *, seq: int) -> None:
    """Parse immutable triage records on ordinary writes and closure reads."""
    from .obligations import MAX_FINDINGS, SEVERITIES, TRIAGE, _exact, _text, _ident, refuse
    findings = ob.get("findings", [])
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise refuse("obligation_invalid", "malformed", "finding history is a bounded list", obligation=ob["id"])
    seen = set()
    for raw in findings:
        required = {"attempt", "finding", "severity", "triage", "summary", "recorded_seq"}
        row = _exact(raw, required | {"reason", "root_cause", "findings", "correction", "proposal", "resolved_seq"}, required, "persisted finding", obligation=ob["id"])
        _text(row["attempt"], "attempt", limit=128, obligation=ob["id"])
        _ident(row["finding"], "finding", obligation=ob["id"])
        _text(row["summary"], "summary", obligation=ob["id"])
        if row["severity"] not in SEVERITIES or row["triage"] not in TRIAGE:
            raise refuse("obligation_invalid", "malformed", "finding retains severity and triage", obligation=ob["id"])
        ids = row.get("findings", [row["finding"]])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or ids[0] != row["finding"]:
            raise refuse("obligation_invalid", "malformed", "finding groups retain their identities", obligation=ob["id"])
        for identity in ids:
            _ident(identity, "finding", obligation=ob["id"])
            key = (row["attempt"], identity)
            if key in seen:
                raise refuse("obligation_invalid", "receipt_conflict", "finding identities are immutable and unique", obligation=ob["id"])
            seen.add(key)
        for field in ("recorded_seq", "resolved_seq"):
            if field in row and (type(row[field]) is not int or not 1 <= row[field] <= seq):
                raise refuse("obligation_invalid", "malformed", "finding retains its map sequence", obligation=ob["id"])
        for field in ("reason", "root_cause"):
            if field in row: _text(row[field], field, limit=1024, obligation=ob["id"])
        if row["triage"] == "required_correction":
            correction = rows.get(row.get("correction")) if isinstance(row.get("correction"), str) else None
            if correction is None or correction["kind"] != "correction" or correction.get("parent") != ob["id"] or "proposal" in row:
                raise refuse("obligation_invalid", "no_parent", "required finding retains its correction", obligation=ob["id"])
        elif "correction" in row or row["severity"] in ("blocker", "major") and not row.get("reason"):
            raise refuse("obligation_invalid", "malformed", "advisory finding retains its reason and proposal", obligation=ob["id"])
        else:
            _ident(row.get("proposal"), "proposal", obligation=ob["id"])
    if "finding" in ob:
        raw = _exact(ob["finding"], {"attempt", "finding", "findings", "severity"}, {"attempt", "finding", "severity"}, "correction finding", obligation=ob["id"])
        _text(raw["attempt"], "attempt", limit=128, obligation=ob["id"])
        ids = raw.get("findings", [raw["finding"]])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or ids[0] != raw["finding"] or raw["severity"] not in SEVERITIES:
            raise refuse("obligation_invalid", "malformed", "correction retains its finding group", obligation=ob["id"])
        for identity in ids: _ident(identity, "finding", obligation=ob["id"])


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
                         {"attempt"}, "assurance evidence", obligation=ob["id"])
            _text(row["attempt"], "attempt", limit=128, obligation=ob["id"])
            admission = _admissions(ctx).get(row["attempt"])
            admission = admission if isinstance(admission, dict) else {}
            base = {"attempt": row["attempt"], "candidate": admission.get("candidate"),
                    "binding": deepcopy(admission.get("binding")),
                    "definition": (admission.get("binding") or {}).get("definitions", {}).get(ob["id"])}
        else:
            earlier = receipts.get(raw.get("reference")) if isinstance(raw, dict) else None
            if (earlier is not None and "schema" not in raw and "timestamp" not in raw
                    and isinstance(earlier["evidence"].get("timestamp"), str)):
                # A restated short receipt keeps the time it was first observed, so replay is idempotent.
                raw = {**raw, "timestamp": earlier["evidence"]["timestamp"]}
            base = _evidence_record(ob, raw, ctx)
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


def reuse_eligibility(ob: dict, source: str, ctx: dict) -> tuple[str, list[str], list[str] | None]:
    """One read-only scope/delta check shared by binding and complete refusals."""
    from .obligations import _path, overlap
    paths = ob.get("proof_scope", ob["scope"]["paths"] if ob["kind"] == "assurance" else ob["boundary"]["paths"])
    if not paths and "proof_scope" not in ob:
        return "no declared scope", [], None
    reader = ctx.get("git_delta")
    current = ctx.get("candidate")
    delta = reader(source, current) if reader is not None and isinstance(current, str) else None
    if delta is None:
        return "delta unreadable", [], None
    touched = overlap({"paths": [_path(path, code="obligation_unaccounted", obligation=ob["id"]) for path in delta], "surfaces": []},
                      {"paths": paths, "surfaces": []})["paths"]
    return ("touched" if touched else "eligible"), touched, delta


def _bind_reuse(ob: dict, prior: dict | None, ctx: dict) -> dict | None:
    from .obligations import _exact, _text, definition_id, refuse
    if "reuse" not in ob:
        return None
    raw = _exact(ob["reuse"], {"from", "to", "delta", "definition"}, {"from"}, "reuse", obligation=ob["id"])
    source = _text(raw["from"], "reuse from", limit=128, obligation=ob["id"])
    current = ctx.get("candidate")
    previous = (prior or {}).get("reuse")
    if (isinstance(previous, dict) and previous.get("from") == source and previous.get("to") == current
            and previous.get("definition") == definition_id(ob)):
        return previous
    label, touched, delta = reuse_eligibility(ob, source, ctx)
    if label != "eligible":
        raise refuse("obligation_unaccounted", "evidence_invalidated", "REUSE is unproven: " + label,
                     obligation=ob["id"], paths=",".join(touched[:8]))
    return {"from": source, "to": current, "delta": sorted(delta)[:256], "definition": definition_id(ob)}


def invalidation(ob: dict, ctx: dict, gov: str, *, corrections: list[str] | None = None) -> dict:
    """Classify all invalid proof without changing a receipt or declaring REUSE."""
    from .obligations import definition_id
    rows = ob.get("evidence", [])
    if any(row.get("definition") != definition_id(ob) for row in rows):
        classification = "definition change"
    elif any(row.get("governance") != gov for row in rows):
        classification = "governance change"
    elif any(not binding_current(row.get("binding") if ob["kind"] == "assurance" else row, ctx) for row in rows):
        classification = "binding change"
    elif (any(row.get("candidate") != ctx.get("candidate") for row in rows)
          and evidence_valid({key: value for key, value in ob.items() if key != "reuse"},
                             {**ctx, "candidate": rows[0].get("candidate")}, gov)):
        classification = "candidate-only"
    else:
        classification = "binding change"
    source = ob.get("reuse", {}).get("from") or (rows[0].get("candidate") if rows else None)
    label, touched, _ = reuse_eligibility(ob, source, ctx) if classification == "candidate-only" else ("ineligible", [], None)
    correction = (f"add reuse {{from: {source}}} to {ob['id']}" if label == "eligible"
                  else f"record fresh evidence for {ob['id']}" + (" after governance refresh/rebind" if classification == "governance change" else ""))
    if corrections:
        label = "ineligible"
        correction = ("an assurance is satisfied only when its corrections are; resolve "
                      + ",".join(corrections[:8]) + f"; record fresh evidence for {ob['id']}")
    return {"obligation": ob["id"], "classification": classification, "eligibility": label,
            "paths": touched[:8], "correction": correction,
            **({"corrections": corrections[:8], "remaining_corrections": max(0, len(corrections) - 8)} if corrections else {})}


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
    from .obligations import (MAX_FINDINGS, PROPOSAL_SOURCES, SEVERITIES, TRIAGE, _admissions,
                              _exact, _ident, _text, expand_update, refuse)
    value = expand_update(state, value)
    base = {key: value[key] for key in value if key in ("seq", "obligations", "proposals")}
    obligations = [dict(row) for row in base.get("obligations", state["obligations"])]
    listed = [dict(row) for row in base.get("proposals", state.get("proposals", []))]
    admission = _admissions(ctx).get(admission_id) or {}
    assurance_id = (admission.get("serves") or [None])[0] if admission.get("role") == "review" else None
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise refuse("obligation_invalid", "malformed", "triage is a bounded list", record='review triage',
                     obligation=assurance_id)
    if findings and admission.get("role") != "review":
        raise refuse("obligation_invalid", "malformed", "only a review attempt is triaged", admission=admission_id)
    rows = {row.get("id"): row for row in obligations}
    if (admission.get("role") == "review" and admission.get("serves") == [assurance_id]
            and review_completed(admission)):
        served = rows.get(assurance_id)
        if (isinstance(served, dict) and served.get("kind") == "assurance"
                and served.get("state") == "satisfied" and not served.get("evidence")):
            served["evidence"] = [{"attempt": admission_id}]
    prior_ids = {ob["id"] for ob in state["obligations"]}
    stored = next((ob.get("findings", []) for ob in state["obligations"] if ob["id"] == assurance_id), [])
    written: set[str | tuple[str, str]] = set()
    records = []
    correction_findings = {}
    for raw in findings:
        record = _exact(raw, {"finding", "severity", "findings", "triage", "summary", "reason", "correction", "root_cause"},
                        {"triage", "summary"} | ({"findings"} if isinstance(raw, dict) and "findings" in raw else {"finding", "severity"}),
                        "finding", obligation=assurance_id)
        if "findings" in record:
            grouped = record["findings"]
            if "finding" in record or "severity" in record or not isinstance(grouped, list) or not 1 <= len(grouped) <= 8:
                raise refuse("obligation_invalid", "malformed", "a group contains 1..8 finding/severity records",
                             record="finding group", obligation=assurance_id)
            for item in grouped:
                _exact(item, {"finding", "severity"}, {"finding", "severity"}, "group member", obligation=assurance_id)
                _ident(item["finding"], "finding", obligation=assurance_id)
                if item["severity"] not in SEVERITIES:
                    raise refuse("obligation_invalid", "malformed", "severity allowed: " + ", ".join(SEVERITIES),
                                 record="group member", obligation=assurance_id)
            if record.get("triage") != "required_correction":
                _text(record.get("root_cause"), "root_cause", obligation=assurance_id)
            ids = [item["finding"] for item in grouped]
            if len(set(ids)) != len(ids):
                raise refuse("obligation_invalid", "malformed", "group ids are unique", record="finding group",
                             obligation=assurance_id)
            record = {**record, "finding": ids[0], "severity": min((item["severity"] for item in grouped), key=SEVERITIES.index)}
        else:
            if "finding" not in record or "severity" not in record:
                raise refuse("obligation_invalid", "malformed", "finding needs finding and severity", record="finding",
                             obligation=assurance_id)
            ids = [record["finding"]]
        _ident(record["finding"], "finding", obligation=assurance_id)
        _text(record["summary"], "summary", obligation=assurance_id)
        if record["severity"] not in SEVERITIES or record["triage"] not in TRIAGE:
            raise refuse("obligation_invalid", "malformed", "severity allowed: " + ", ".join(SEVERITIES) + "; triage allowed: " + ", ".join(TRIAGE),
                         finding=record["finding"], obligation=assurance_id)
        if record["severity"] in ("blocker", "major") and record["triage"] == "advisory":
            if "reason" not in record:
                raise refuse("obligation_invalid", "downgrade_unreasoned",
                             "downgrading a reviewer's blocker or major finding needs a reason",
                             finding=record["finding"], obligation=assurance_id)
            _text(record["reason"], "reason", limit=1024, obligation=assurance_id)
        entry = {"attempt": admission_id, "finding": record["finding"], "severity": record["severity"],
                 "triage": record["triage"], "summary": record["summary"],
                 "recorded_seq": state["seq"] + 1}
        if "root_cause" in record:
            _text(record["root_cause"], "root_cause", obligation=assurance_id)
            entry["root_cause"] = record["root_cause"]
        if "findings" in record:
            entry["findings"] = ids
        same_attempt = [item for item in [*stored, *records] if item.get("attempt") == admission_id]
        matching = [item for item in same_attempt if set(item.get("findings", [item["finding"]])) & set(ids)]
        if matching:
            prior = matching[0]
            replay = {key: prior.get(key) for key in ("finding", "severity", "triage", "summary", "reason", "correction", "findings", "root_cause")}
            proposed = {key: record.get(key) for key in replay}
            proposed["findings"] = ids if "findings" in record else None
            if replay != proposed:
                raise refuse("obligation_invalid", "receipt_conflict", "stored finding ids cannot be regrouped or changed",
                             finding=record["finding"], record="finding", obligation=assurance_id)
            continue
        if "reason" in record:
            entry["reason"] = record["reason"]
        known = {row.get("id") for row in listed}
        if record["triage"] == "required_correction":
            correction = rows.get(record.get("correction"))
            if (correction is None or record["correction"] in prior_ids or correction.get("kind") != "correction"
                    or correction.get("parent") != assurance_id or correction.get("provenance") != "coordinator"):
                raise refuse("obligation_invalid", "no_parent",
                             "several required findings of one review may share one new coordinator correction under the assurance obligation",
                             finding=record["finding"], obligation=assurance_id)
            previous = correction_findings.get(correction["id"], {})
            all_ids = list(dict.fromkeys([*previous.get("findings", []), *ids]))
            if len(all_ids) > 8:
                raise refuse("obligation_invalid", "finding_limit", "correction finding ids exceed the bound of 8",
                             obligation=correction["id"], record="correction finding")
            correction["finding"] = {"attempt": admission_id, "finding": all_ids[0], "findings": all_ids,
                                     "severity": min((previous.get("severity", record["severity"]), record["severity"]),
                                                     key=SEVERITIES.index)}
            correction_findings[correction["id"]] = correction["finding"]
            written.add(correction["id"])
            entry["correction"] = record["correction"]
        else:
            if "correction" in record:
                raise refuse("obligation_invalid", "malformed", "an advisory finding becomes a proposal",
                             finding=record["finding"], obligation=assurance_id)
            key = "adv-" + digest({"attempt": admission_id, "finding": record["finding"]})[:12]
            if key not in known:
                listed.append({"id": key, "source": "reviewer_advisory", "origin_ref": admission_id[:64],
                               "summary": record["summary"], "status": "open"})
            entry["proposal"] = key
        records.append(entry)
    if records:
        assurance = rows.get(assurance_id)
        if assurance is None:
            raise refuse("obligation_invalid", "malformed", "the reviewed assurance obligation is absent", record='review triage',
                         obligation=assurance_id)
        if len(stored) + len(records) > MAX_FINDINGS:
            raise refuse("obligation_invalid", "finding_limit",
                         "the bounded finding history is full", obligation=assurance_id)
        assurance["findings"] = list(stored) + records
        written.add(assurance_id)
    if admission.get("role") == "review":
        written.add(("review_report", admission_id))
    if not isinstance(proposals, list) or len(proposals) > 16:
        raise refuse("obligation_invalid", "malformed", "report proposals are a bounded list", record='review triage',
                     obligation=assurance_id)
    for raw in proposals:
        record = _exact(raw, {"summary", "source"}, {"summary"}, "report proposal", obligation=assurance_id)
        _text(record["summary"], "summary", obligation=assurance_id)
        source = record.get("source", "worker_report")
        if source not in PROPOSAL_SOURCES:
            raise refuse("obligation_invalid", "malformed", "proposal source allowed: " + ", ".join(PROPOSAL_SOURCES), record='review triage',
                         obligation=assurance_id)
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

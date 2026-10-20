"""Bound PES checkbox proof, durable write intents and read-only projections.

GitHub owns the body. Only its digest and compact checkbox observations live here;
an intended digest or a matching readback never establishes write authorship.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from time import time
import uuid

from .errors import PodError
from .util import digest, exact

SCHEMA = "pod-ticks/v1"
STEPS = "Record the same proof without closing, run each due pod internal pod-tick request, confirm its readback, then close."
INTENT_MARGIN_S = 5
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
SCOPE_FIELDS = {"definition", "source_digest", "amendments_digest", "block_sha256"}


def validate_scope(value: object) -> dict:
    scope = exact(value, SCOPE_FIELDS, SCOPE_FIELDS, name="criterion source scope")
    if any(not isinstance(item, str) or not _SHA.fullmatch(item) for item in scope.values()):
        raise PodError("state_unsupported", "PoD criterion source identity is malformed")
    return scope


def requirement_scope(entry: dict) -> dict | None:
    """A definition-only entry records unknown scope, unlike an absent earlier cache."""
    return {key: entry[key] for key in SCOPE_FIELDS} if SCOPE_FIELDS <= entry.keys() else None


def selected_pass_receipts(row: dict) -> list[dict]:
    return [receipt for receipt in row.get("receipts", [])
            if receipt["evidence"] in row.get("evidence", [])
            and receipt["evidence"].get("status") == "PASS"
            and receipt["evidence"].get("definition") == row["definition"]]


def proof_identity(receipt: dict) -> str:
    # Governance can be explicitly rebound; receipt content and its first
    # acceptance sequence cannot. A reused reference retains this identity.
    return digest({"accepted_seq": receipt.get("accepted_seq"),
                   "evidence": {key: value for key, value in receipt["evidence"].items() if key != "governance"}})


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def empty() -> dict:
    return {"schema": SCHEMA, "facts": None, "read": {"status": "not_read", "reason": "never_read"},
            "authors": {}, "requests": {}, "closure_blocked": [], "last_rebind": None}


def _intent_window(request: dict) -> dict:
    from .github import MUTATION_TIMEOUT_S
    if request["phase"] != "intent" or "intent_at" not in request:
        return {}
    return {"intent_at": request["intent_at"],
            "retry_after": request["intent_at"] + MUTATION_TIMEOUT_S + INTENT_MARGIN_S}


def _pending_error(request: dict | None, message: str) -> PodError:
    window = _intent_window(request) if request is not None else {}
    if window:
        message += (f"; intent_at={window['intent_at']}, in flight until "
                    f"retry_after={window['retry_after']} (Unix seconds)")
    return PodError("pod_tick_pending", message, {**({"pod": request["pod"]} if request else {}), **window})


def validate(value: object) -> None:
    """Optional representation: older contexts are read without conversion."""
    row = exact(value, set(empty()) | {"criterion_scopes"}, set(empty()), name="pod_ticks")
    if row["schema"] != SCHEMA:
        raise PodError("state_unsupported", "PoD tick representation is unsupported")
    if row["last_rebind"] is not None and (not isinstance(row["last_rebind"], str) or not _SHA.fullmatch(row["last_rebind"])):
        raise PodError("state_unsupported", "PoD tick rebind identity is malformed")
    if (not isinstance(row["authors"], dict) or not isinstance(row["requests"], dict)
            or not isinstance(row["read"], dict) or not isinstance(row["closure_blocked"], list)):
        raise PodError("state_unsupported", "PoD tick records are malformed")
    exact(row["read"], {"status", "reason"}, {"status", "reason"}, name="PoD read")
    if (row["read"]["status"] not in ("read", "not_read")
            or row["read"]["reason"] is not None and not _text(row["read"]["reason"])):
        raise PodError("state_unsupported", "PoD read state is malformed")
    for key, author in row["authors"].items():
        if not _NUMBER.fullmatch(key) or author not in ("own", "ambiguous"):
            raise PodError("state_unsupported", "PoD tick authorship is malformed")
    facts = row["facts"]
    if facts is not None:
        exact(facts, {"source", "body_sha256", "lines", "blocks"}, {"source", "body_sha256", "lines"}, name="PoD line facts")
        if (not isinstance(facts["source"], str) or not _SHA.fullmatch(facts["source"])
                or not isinstance(facts["body_sha256"], str) or not _SHA.fullmatch(facts["body_sha256"])
                or not isinstance(facts["lines"], dict)):
            raise PodError("state_unsupported", "PoD line facts are malformed")
        for key, states in facts["lines"].items():
            if (not _NUMBER.fullmatch(key) or not isinstance(states, list) or not states
                    or any(type(state) is not bool for state in states)):
                raise PodError("state_unsupported", "PoD checkbox observations are malformed")
        blocks = facts.get("blocks", {})
        if (not isinstance(blocks, dict) or set(blocks) - set(facts["lines"])
                or any(not isinstance(hashes, list) or len(hashes) != len(facts["lines"][key])
                       or any(not isinstance(item, str) or not _SHA.fullmatch(item) for item in hashes)
                       for key, hashes in blocks.items())):
            raise PodError("state_unsupported", "PoD source block observations are malformed")
    scopes = row.get("criterion_scopes", {})
    if not isinstance(scopes, dict) or len(scopes) > 96:
        raise PodError("state_unsupported", "PoD criterion source identities are malformed")
    for key, scope in scopes.items():
        from .obligations import _ident, MAX_EVIDENCE
        _ident(key, "criterion")
        exact(scope, SCOPE_FIELDS | {"proofs"}, {"definition"}, name="criterion source facts")
        if not isinstance(scope["definition"], str) or not _SHA.fullmatch(scope["definition"]):
            raise PodError("state_unsupported", "PoD criterion definition is malformed")
        if set(scope) & (SCOPE_FIELDS - {"definition"}):
            validate_scope({key: value for key, value in scope.items() if key != "proofs"})
        proofs = scope.get("proofs", {})
        if (not isinstance(proofs, dict) or len(proofs) > MAX_EVIDENCE
                or any(not isinstance(key, str) or not _SHA.fullmatch(key) for key in proofs)):
            raise PodError("state_unsupported", "PoD proof source identities are malformed")
        for proof in proofs.values():
            validate_scope(proof)
    for key, request in row["requests"].items():
        required = {"id", "pod", "seq", "source", "direction", "before", "intended", "phase", "result", "readback", "block"}
        exact(request, required | {"intent_at"}, required, name="PoD tick request")
        from .github import validate_issue_binding
        validate_issue_binding(request["source"])
        if (not _NUMBER.fullmatch(key) or type(request["pod"]) is not int or str(request["pod"]) != key
                or type(request["seq"]) is not int or request["seq"] < 0
                or not isinstance(request["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", request["id"])
                or request["direction"] not in ("tick", "untick")
                or request["phase"] not in ("intent", "result", "completed")
                or any(not isinstance(request[field], str) or not _SHA.fullmatch(request[field])
                       for field in ("before", "intended"))
                or any(request[field] is not None and not isinstance(request[field], dict)
                       for field in ("result", "readback", "block"))):
            raise PodError("state_unsupported", "PoD tick request is malformed")
        if "intent_at" in request and (type(request["intent_at"]) not in (int, float)
                                       or not math.isfinite(request["intent_at"]) or request["intent_at"] < 0):
            raise PodError("state_unsupported", "PoD intent time is malformed")
        if request["before"] != request["source"]["body_sha256"]:
            raise PodError("state_unsupported", "PoD intent differs from its bound source")
        result = request["result"]
        if result is not None:
            kind = result.get("result")
            if kind == "accepted":
                valid = (set(result) == {"result", "status"} and type(result.get("status")) is int
                         and 200 <= result["status"] < 300)
            elif kind == "refused":
                valid = (set(result) == {"result", "status", "message"} and type(result.get("status")) is int
                         and 400 <= result["status"] < 500 and _text(result.get("message"), empty=True))
            else:
                valid = set(result) == {"result", "reason"} and kind == "unconfirmed" and _text(result.get("reason"))
            if not valid:
                raise PodError("state_unsupported", "PoD held response is malformed")
        back = request["readback"]
        if back is not None:
            exact(back, {"body_sha256", "state", "ticked"}, {"body_sha256", "state", "ticked"}, name="PoD readback")
            if (back["body_sha256"] is not None and (not isinstance(back["body_sha256"], str) or not _SHA.fullmatch(back["body_sha256"]))
                    or back["state"] not in (None, "OPEN", "CLOSED")
                    or back["ticked"] is not None and type(back["ticked"]) is not bool):
                raise PodError("state_unsupported", "PoD readback is malformed")
        if request["block"] is not None:
            _validate_block(request["block"])
        if (request["phase"] == "intent" and (result is not None or request["before"] == request["intended"])
                or request["phase"] == "result" and result is None):
            raise PodError("state_unsupported", "PoD request phase contradicts its result")
    for closed in row["closure_blocked"]:
        keys = {"pod", "direction", "block", "ticked", "authorship"}
        exact(closed, keys, keys, name="PoD blocked at closure")
        if (type(closed["pod"]) is not int or not 0 <= closed["pod"] <= 999_999_999
                or closed["direction"] not in ("tick", "untick")
                or closed["authorship"] not in ("own", "ambiguous", "not_pod")
                or closed["ticked"] is not None and type(closed["ticked"]) is not bool):
            raise PodError("state_unsupported", "PoD closing limitation is malformed")
        _validate_block(closed["block"])


def _text(value: object, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= 512 and "\0" not in value


def _validate_block(block: dict) -> None:
    exact(block, {"code", "reason", "row", "status", "message"}, {"code", "reason", "row"}, name="PoD block")
    if (not _text(block["code"]) or not _text(block["reason"])
            or block["row"] not in ("A3", "B2", "B3", "B5", "C2", "C3", "D1", "E2")
            or "status" in block and (type(block["status"]) is not int or not 400 <= block["status"] < 500)
            or "message" in block and not _text(block["message"], empty=True)):
        raise PodError("state_unsupported", "PoD block is malformed")


def _facts(ledger: dict, source: dict, body: str, *, context: dict | None = None) -> None:
    from .github import pod_lines, pod_blocks
    lines = {str(number): [line["ticked"] for line in rows] for number, rows in pod_lines(body).items()}
    ledger["facts"] = {"source": digest(source), "body_sha256": _hash(body), "lines": lines,
                       "blocks": {str(number): [block["sha256"] for block in blocks]
                                  for number, blocks in pod_blocks(body).items()}}
    ledger["read"] = {"status": "read", "reason": None}
    if context is not None:
        from .standing import observe
        observe(context, source, body)
    # Once an unticked or missing line is observed, earlier success
    # no longer establishes continuous authorship of a later tick.
    ledger["authors"] = {key: author for key, author in ledger["authors"].items()
                         if lines.get(key) and all(lines[key])}


def _bind_proof_scopes(entry: dict, row: dict, earlier: dict, observed: dict | None, seq: int) -> None:
    """Retain actual scope only at first acceptance, never on reference reuse."""
    if observed is None:
        return
    old = {receipt["evidence"]["reference"]: receipt for receipt in earlier.get("receipts", [])}
    for receipt in selected_pass_receipts(row):
        if (receipt.get("accepted_seq") == seq
                and old.get(receipt["evidence"]["reference"], {}).get("accepted_seq") is None):
            entry.setdefault("proofs", {}).setdefault(proof_identity(receipt), deepcopy(observed))


def _validate_reselected_proof(entry: dict, row: dict, earlier: dict) -> None:
    """A returning logical hash cannot make an earlier receipt new proof."""
    from .obligations import refuse
    selected = ({proof_identity(receipt) for receipt in selected_pass_receipts(earlier)}
                if earlier.get("state") == "satisfied" else set())
    old = {proof_identity(receipt) for receipt in earlier.get("receipts", [])
           if receipt.get("accepted_seq") is not None}
    for receipt in selected_pass_receipts(row):
        key = proof_identity(receipt)
        if row["state"] != "satisfied" or key not in old or key in selected:
            continue
        scope = requirement_scope(entry)
        proof = entry.get("proofs", {}).get(key)
        if scope is None or proof is None or any(scope[field] != proof[field]
                                                for field in ("definition", "block_sha256", "amendments_digest")):
            raise refuse("obligation_unaccounted", "evidence_invalidated",
                         "the reselected receipt lacks unchanged accepted requirement and proof-time scope",
                         obligation=row["id"], next_action="record fresh proof for the accepted requirement scope")


def bind_criterion_scopes(state: dict, previous: dict, *, verified: dict | None = None) -> None:
    """Keep requirement acceptance and first proof acceptance as separate facts.

    Metadata-only definition changes carry prior physical scope. A validated
    session-owner check revision may accept new scope; ordinary observation may
    not. An unknown new definition cannot resurrect an older hash's source.
    """
    from .github import pod_ref_number
    checkpoint = state["checkpoint"]
    source = checkpoint.get("objective_source")
    ledger = state.get("pod_ticks", {})
    facts = ledger.get("facts") or {}
    if source is None:
        return
    before = {row["id"]: row for row in previous.get("obligations", [])}
    decisions = checkpoint.get("revision_decisions", [])
    decision = decisions[-1] if decisions else {}
    owner_revision = (decision.get("seq") == checkpoint["seq"]
                      and decision.get("kind") in ("revision", "question")
                      and decision.get("channel", "session") == "session")
    for row in checkpoint.get("obligations", []):
        if row["kind"] != "criterion" or row["provenance"] != "objective" or row["state"] == "withdrawn":
            continue
        earlier = before.get(row["id"], {})
        entry = ledger.get("criterion_scopes", {}).get(row["id"], {})
        number = pod_ref_number(row.get("source", {}).get("ref"))
        blocks = facts.get("blocks", {}).get(str(number), [])
        observed = None
        if (facts.get("source") == digest(source) and len(blocks) == 1
                and row.get("source", {}).get("ref") in checkpoint.get("criteria", [])):
            observed = {"definition": row["definition"], "source_digest": digest(source),
                "amendments_digest": digest(source["amendments"]), "block_sha256": blocks[0]}
        approved = (verified or {}).get(row["id"])
        changed = earlier.get("definition") != row["definition"]
        if approved is not None and entry.get("definition") != row["definition"]:
            entry = {**deepcopy(approved), **({"proofs": entry["proofs"]} if "proofs" in entry else {})}
        elif changed:
            semantic = owner_revision and row.get("check") != earlier.get("check")
            initial = not previous.get("obligations")
            old_scope = requirement_scope(entry) if entry.get("definition") == earlier.get("definition") else None
            scope = observed if initial or semantic else old_scope
            entry = {**(deepcopy(scope) if scope is not None else {}), "definition": row["definition"],
                     **({"proofs": entry["proofs"]} if "proofs" in entry else {})}
        if entry:
            _validate_reselected_proof(entry, row, earlier)
            _bind_proof_scopes(entry, row, earlier, observed, checkpoint["seq"])
            ledger.setdefault("criterion_scopes", {})[row["id"]] = entry


def refresh(state: dict, project: Path, source: dict) -> None:
    """Introducing/rebinding checkpoint: one best-effort read, never a refusal."""
    from .github import issue_recheck_live
    ledger = state.setdefault("pod_ticks", empty())
    if ledger["last_rebind"] != digest(source):
        ledger["last_rebind"] = None
    try:
        result, live = issue_recheck_live(project, source)
        if live is None or _hash(live["body"]) != source["body_sha256"]:
            ledger["read"] = {"status": "not_read", "reason": result.get("reason") or "body_digest_mismatch"}
        else:
            _facts(ledger, source, live["body"], context=state)
    except PodError as exc:
        ledger["read"] = {"status": "not_read", "reason": exc.code}


def mapping(map_state: dict | None) -> dict[int, list[dict]]:
    from .github import pod_ref_number
    revisions = [row for row in (map_state or {}).get("revision_decisions", [])
                 if row.get("operation") == "issue_revision"]
    approved = {row["id"] for row in revisions[-1]["criteria"]} if revisions else None
    result = {}
    for criterion in (map_state or {}).get("obligations", []):
        if (criterion.get("kind") == "criterion" and criterion.get("provenance") == "objective"
                and criterion.get("state") == "withdrawn" and approved is not None
                and criterion["id"] not in approved):
            continue  # An approved current mapping supersedes history without rewriting its withdrawal.
        number = pod_ref_number(criterion.get("source", {}).get("ref")) if criterion["kind"] == "criterion" else None
        if number is not None:
            result.setdefault(number, []).append(criterion)
    return result


def intake(state: dict, map_state: dict | None = None) -> dict:
    source = (state.get("checkpoint") or {}).get("objective_source")
    if source is None:
        return {"status": "not_applicable"}
    ledger = state.get("pod_ticks", empty())
    facts = ledger["facts"]
    if facts is None or facts["source"] != digest(source) or ledger["read"]["status"] != "read":
        return {"status": "not_read", "reason": ledger["read"]["reason"]}
    mapped = mapping(map_state or state["checkpoint"])
    lines = {int(key): rows for key, rows in facts["lines"].items()}
    return {"status": "read", "unmapped": sorted(set(lines) - set(mapped)),
            "multiply_mapped": sorted(number for number, rows in mapped.items() if number in lines and len(rows) > 1),
            "duplicated": sorted(number for number, rows in lines.items() if len(rows) > 1),
            "missing": [{"pod": number, "criteria": [row["id"] for row in mapped[number]]}
                        for number in sorted(set(mapped) - set(lines))]}


def projection(state: dict | None, map_state: dict | None, ctx: dict, *, project: Path | None = None,
               objective: str | None = None, owner: str | None = None, source: dict | None = None) -> dict:
    """R9/R10 decision table from recorded observations and current proof; no fetch."""
    from .assurance import evidence_valid
    from .obligations import bound_governance_digest
    state = state or {}
    source = source if source is not None else (state.get("checkpoint") or {}).get("objective_source")
    if source is None:
        return {"status": "not_applicable", "lines": []}
    ledger = state.get("pod_ticks", empty())
    facts = ledger["facts"]
    fresh = facts is not None and facts["source"] == digest(source)
    lines = (facts or {}).get("lines", {})
    mapped = mapping(map_state)
    terminal = bool(state.get("continued_by") or state.get("historical_retirement") or (map_state or {}).get("closure"))
    closed_blocks = {row["pod"]: row for row in ledger["closure_blocked"]} if terminal else {}
    gov = bound_governance_digest(map_state) if map_state and map_state.get("obligations") is not None else ""
    output = []
    for number in sorted(set(mapped) | {int(key) for key in lines} | {int(key) for key in ledger["authors"]}):
        key = str(number)
        criteria = [{"id": row["id"], "state": row["state"],
                     "current": row["state"] == "satisfied" and evidence_valid(row, ctx, gov)}
                    for row in mapped.get(number, [])]
        proven = bool(criteria) and all(row["current"] for row in criteria)
        observed = lines.get(key, [])
        ticked = observed[0] if len(observed) == 1 else None
        author = ledger["authors"].get(key, "not_pod")
        direction, block, label = None, None, "not_proven"
        if not fresh:
            # Never let absent/stale facts hide a potentially writable proven
            # line, or the invalidation of a tick whose authorship we hold.
            direction = "tick" if proven else "untick" if author == "own" else None
            label = "due" if direction else "not_proven"
        elif not observed:
            label, block = "blocked", {"code": "line_missing", "reason": "line_missing"}
        elif len(observed) != 1:
            direction = "tick" if proven and not all(observed) else "untick" if author == "own" and not proven else None
            label, block = "blocked", {"code": "line_duplicated", "reason": "line_duplicated"}
        elif ticked and author == "ambiguous":
            label = "ambiguous"
        elif ticked:
            direction = "untick" if author == "own" and not proven else None
            label = "due" if direction else "current" if proven else "ticked_without_pod_proof"
        elif proven:
            direction, label = "tick", "due"
        elif not criteria:
            label, block = "blocked", {"code": "unmapped", "reason": "unmapped"}
        request = ledger["requests"].get(key)
        if request is not None and request["phase"] != "completed" and direction is None:
            direction, label = request["direction"], "due"
        # Only a request after the latest checkpoint/report can justify leaving
        # a due tick blocked at closure. seq is unchanged by tick rebinds.
        recent = request is not None and request["seq"] == (map_state or {}).get("seq", 0)
        if direction and recent and request["block"] is not None:
            label, block = "blocked", request["block"]
        if terminal:
            direction = None
            if number in closed_blocks:
                prior = closed_blocks[number]
                label, block = "blocked", prior["block"]
        row = {"pod": number, "ticked": ticked, "checkboxes": list(observed),
               "occurrences": len(observed) if fresh else None,
               "authorship": author, "criteria": criteria, "proven": proven,
               "status": label, "direction": direction, "due": bool(direction) and not terminal,
               "blocked": block, "facts_current": fresh}
        if (recent and request["phase"] == "completed" and request["block"] is not None
                and request["block"].get("row") in ("B2", "B3", "B5")
                and (request["result"] or {}).get("result") in ("accepted", "unconfirmed")
                and (request["readback"] or {}).get("body_sha256") == request["intended"]
                and source == {**request["source"], "body_sha256": request["intended"]}
                and ledger["last_rebind"] == digest(source)):
            row["hold"] = deepcopy(request["block"])
        if number in closed_blocks:
            row["blocked_at_closure"] = closed_blocks[number]
        if direction:
            request_body = {"project": str(project), "objective": objective, "owner": owner or state.get("owner"), "pod": number}
            row["request"] = {"operation": "pod-tick", "input": request_body}
            window = _intent_window(request) if request is not None else {}
            if window:
                row["request"].update(window, status="in_flight" if time() < window["retry_after"] else "recovery_ready")
            row["command"] = "pod internal pod-tick --input - <<'JSON'\n" + json.dumps(request_body, sort_keys=True) + "\nJSON"
            waiting = [key for key, admission in state.get("admissions", {}).items()
                       if admission["state"] in ("reserved", "unresolved")]
            if waiting:
                row["waiting_on_admissions"] = waiting
        if request is not None:
            row["latest_request"] = deepcopy(request)
        output.append(row)
    return {"status": "closed" if terminal else "observed", "facts_current": fresh,
            "read": deepcopy(ledger["read"]), "lines": output}


def require_closing(state: dict, post_map: dict, ctx: dict, *, project: Path, objective: str,
                    source: dict | None = None) -> None:
    view = projection(state, {**post_map, "closure": None, "seq": ctx["pod_tick_prior_seq"]}, ctx,
                      project=project, objective=objective, source=source)
    due = [row for row in view["lines"] if row["due"]]
    def blocked(row):
        request = row.get("latest_request")
        return (request is not None and request["seq"] == ctx["pod_tick_prior_seq"]
                and request["block"] is not None and request["block"].get("row") in ("B2", "B3", "B5", "C3", "E2"))
    writable = [row for row in due if not blocked(row)]
    if writable:
        raise PodError("pod_ticks_due", STEPS,
                       {"pod_ticks": view, "codes": ["pod_ticks_due"], "steps": STEPS,
                        "next_action": STEPS})
    if due or "pod_ticks" in state:
        state.setdefault("pod_ticks", empty())["closure_blocked"] = [
            {"pod": row["pod"], "direction": row["direction"], "block": deepcopy(row["blocked"]),
             "ticked": row["ticked"], "authorship": row["authorship"]} for row in due]


def closing_context(state: dict, prior_map: dict | None, ctx: dict, *, project: Path, objective: str,
                    source: dict | None = None) -> None:
    """Install a gate only for a new closure; old snapshots never acquire it."""
    ctx["pod_tick_prior_seq"] = (prior_map or {}).get("seq", 0)
    ctx["pod_tick_gate"] = lambda post: require_closing(state, post, ctx, project=project, objective=objective, source=source)


def require_tabs(tabs: list[dict], prior: dict | None, proposed: dict, ctx: dict) -> None:
    """Keep the existing tab refusal's precedence, while naming both gates."""
    from .worker_tabs import require_closed_tabs
    try:
        require_closed_tabs(tabs)
    except PodError as exc:
        if callable(ctx.get("pod_tick_gate")):
            from .obligations import accept_write
            try:
                post = accept_write(prior, {key: value for key, value in proposed.items() if key != "close"}, ctx)
                ctx["pod_tick_gate"](post)
            except PodError as tick_exc:
                if tick_exc.code == "pod_ticks_due":
                    exc.detail = {**(exc.detail or {}), **tick_exc.detail,
                                  "worker_tabs": tabs, "codes": [exc.code, "pod_ticks_due"],
                                  "steps": STEPS + " Settle the worker tabs before closing."}
        raise


def _block(exc: PodError, *, row: str = "B2") -> dict:
    return {"code": exc.code, "reason": (exc.detail or {}).get("reason") or exc.code, "row": row}


def _source_check(result: dict) -> None:
    # CLOSED takes precedence for ticks even when the ordinary recheck reports
    # body drift first. There is no allow_closed path for this operation.
    reason = ("closed_issue_requires_intent_reconciliation" if result.get("state") == "CLOSED"
              else result.get("reason"))
    if result.get("state") == "CLOSED" or result["status"] != "current":
        raise PodError("issue_reconciliation_required", "Reconcile the bound PES source before ticking",
                       {"reason": reason})


class _ReadbackPort:
    """Keep the issue observation even when the recheck refuses its role or amendment."""
    def __init__(self, port):
        self.port, self.live = port, None

    def issue(self, **arguments):
        self.live = self.port.issue(**arguments)
        return self.live

    def amendment(self, **arguments):
        return self.port.amendment(**arguments)


def tick(project: Path, objective: str, *, owner: str, pod: int) -> dict:
    """One request: locked intent, unlocked send, held result, readback, atomic receipt/rebind."""
    from . import ledger as store
    from .github import GhPort, flip_pod_checkbox, issue_recheck_live, pod_lines
    if type(pod) is not int or not 0 <= pod <= 999_999_999:
        raise PodError("invalid_request", "pod must be a PoD number from 0 to 999999999")
    path, key = store._path(project, objective), str(pod)
    port = GhPort(project)
    with store._lock(path):
        state = store._read(path)
        store._require_open(state)
        store.require_authority(project, objective, owner=owner, state=state, persist_rebind=False)
        source = (state.get("checkpoint") or {}).get("objective_source")
        if source is None:
            return {"status": "not_applicable"}
        _require_admissions_settled(state)
        ledger = state.setdefault("pod_ticks", empty())
        pending = [(number, row) for number, row in ledger["requests"].items() if row["phase"] != "completed"]
        if pending and pending[0][0] != key:
            raise _pending_error(pending[0][1], "Recover the pending PoD request before another tick")
        recovering = bool(pending)
        request = deepcopy(pending[0][1]) if pending else None
        window = _intent_window(request) if request is not None else {}
        if window and time() < window["retry_after"]:
            raise _pending_error(request, "The PoD request is still in flight; recover after its send timeout")
        prior_request = deepcopy(ledger["requests"].get(key))
        seq = (store.map_of(state) or {}).get("seq", 0)
        source = deepcopy(source)
    # No objective lock spans a provider call. The durable intent serializes
    # sends, and a concurrent caller seeing it can only recover, never resend.
    if recovering:
        result = request["result"] or {"result": "unconfirmed", "reason": "interrupted"}
        return _readback(project, objective, owner, key, request, result, port, recovering=True)
    request = {"id": uuid.uuid4().hex, "pod": pod, "seq": seq, "source": source,
               "direction": "tick", "before": source["body_sha256"], "intended": source["body_sha256"],
               "phase": "completed", "result": None, "readback": None, "block": None}
    ctx = store.kernel_context(project, objective, state, None)
    current = projection(state, store.map_of(state), ctx, project=project, objective=objective)
    row = next((row for row in current["lines"] if row["pod"] == pod), None)
    request["direction"] = (row or {}).get("direction") or "tick"
    live = None
    try:
        checked, live = issue_recheck_live(project, source, port=port)
        _source_check(checked)
    except PodError as exc:
        request["block"] = _block(exc, row="A3" if exc.code == "repository_mismatch" else
                                  "B5" if exc.code == "issue_access_unavailable" else "B3"
                                  if (exc.detail or {}).get("reason") == "closed_issue_requires_intent_reconciliation" else "B2")
        if live is not None:
            lines = pod_lines(live["body"]).get(pod, [])
            request["readback"] = {"body_sha256": _hash(live["body"]), "state": live["state"],
                                   "ticked": lines[0]["ticked"] if len(lines) == 1 else None}
        # This is a new, pre-send refusal, not a result for an existing intent.
        # It may replace only the request observed before the immediate read.
        with store._lock(path):
            recorded = store._read(path)
            store._require_open(recorded)
            if recorded["owner"] != owner:
                raise PodError("coordinator_conflict", "Tick belongs to the recorded coordinator")
            ticks = recorded.setdefault("pod_ticks", empty())
            if ticks["requests"].get(key) != prior_request:
                raise _pending_error(ticks["requests"].get(key), "PoD request changed during the immediate read")
            if row is not None or live is not None and pod in pod_lines(live["body"]):
                ticks["requests"][key] = deepcopy(request)
                if live is not None:
                    _facts(ticks, {**source, "body_sha256": _hash(live["body"])}, live["body"],
                           context=recorded if _hash(live["body"]) == source["body_sha256"] else None)
                store._write(path, recorded)
        ctx = store.kernel_context(project, objective, recorded, None)
        exc.detail = {**(exc.detail or {}), "pod": pod, "direction": request["direction"],
                      "blocked": request["block"],
                      "pod_ticks": projection(recorded, store.map_of(recorded), ctx,
                                              project=project, objective=objective)}
        raise
    body = live["body"]
    with store._lock(path):
        state = store._read(path)
        store._require_open(state)
        _require_admissions_settled(state)
        if state["checkpoint"].get("objective_source") != source or (store.map_of(state) or {}).get("seq", 0) != seq:
            raise PodError("checkpoint_binding_changed", "Checkpoint changed during the immediate tick read; request again")
        ledger = state.setdefault("pod_ticks", empty())
        pending = next((row for row in ledger["requests"].values() if row["phase"] != "completed"), None)
        if pending is not None:
            raise _pending_error(pending, "Recover the pending PoD request before another tick")
        lines = pod_lines(body).get(pod, [])
        if not lines and pod not in mapping(store.map_of(state)) and key not in ledger["authors"]:
            raise PodError("line_missing", "No PoD line, mapping or authorship for this number",
                           {"pod": pod, "direction": request["direction"],
                            "code": "line_missing", "reason": "line_missing", "row": "C2",
                            "readback": {"body_sha256": _hash(body), "state": live["state"], "ticked": None},
                            "pod_ticks": projection(state, store.map_of(state), ctx, project=project, objective=objective)})
        _facts(ledger, source, body, context=state)
        ctx = store.kernel_context(project, objective, state, None)
        view = projection(state, store.map_of(state), ctx, project=project, objective=objective)
        row = next((row for row in view["lines"] if row["pod"] == pod), None)
        request["direction"] = (row or {}).get("direction") or "tick"
        request["readback"] = {"body_sha256": _hash(body), "state": live["state"],
                               "ticked": lines[0]["ticked"] if len(lines) == 1 else None}
        if len(lines) != 1:
            code = "line_missing" if not lines else "line_duplicated"
            request["block"] = {"code": code, "reason": code, "row": "C2" if not lines else "C3"}
        elif row is None or not row["due"]:
            if row is None or not row["criteria"] and not lines[0]["ticked"]:
                request["block"] = {"code": "unmapped", "reason": "unmapped", "row": "D1"}
        else:
            intended = flip_pod_checkbox(body, lines[0]["offset"], row["direction"] == "tick")
            request.update(intended=_hash(intended), phase="intent", readback=None, intent_at=time())
        ledger["requests"][key] = request
        store._write(path, state)
        if request["phase"] != "intent":
            view = projection(state, store.map_of(state), ctx, project=project, objective=objective)
    if request["phase"] != "intent":
        if request["block"]:
            raise PodError(request["block"]["code"], "No single writable proven PoD line",
                           {**request["block"], "pod": pod, "direction": request["direction"],
                            "readback": request["readback"], "pod_ticks": view})
        row = next((row for row in view["lines"] if row["pod"] == pod), None)
        return {"status": (row or {}).get("status", "not_due"), "pod": pod, "pod_ticks": view}
    try:
        result = port.issue_body_write(source=source, body=intended)
    except PodError as exc:
        if exc.code != "remote_effect_uncertain":
            raise
        result = {"result": "unconfirmed", "reason": exc.code}
    # BaseException deliberately escapes, preserving the intent for E4.
    request.update(phase="result", result=result)
    _record(project, objective, owner, key, request)
    return _readback(project, objective, owner, key, request, result, port)


def _require_admissions_settled(state: dict) -> None:
    pending = [key for key, row in state["admissions"].items() if row["state"] in ("reserved", "unresolved")]
    if pending:
        raise PodError("pod_tick_admission_pending", "Settle the admitted request through its existing admission replay before ticking",
                       {"admissions": pending,
                        "next_action": "Replay the existing admission with its unchanged frozen packet; settle or reconcile its native request, then request pod-tick again"})


def _record(project: Path, objective: str, owner: str, key: str, request: dict) -> dict:
    from . import ledger as store
    path = store._path(project, objective)
    with store._lock(path):
        state = store._read(path)
        store._require_open(state)
        if state["owner"] != owner:
            raise PodError("coordinator_conflict", "Tick belongs to the recorded coordinator")
        ticks = state.setdefault("pod_ticks", empty())
        current = ticks["requests"].get(key)
        if current is None or current["id"] != request["id"]:
            raise _pending_error(current, "Tick result no longer names the latest intent")
        ticks["requests"][key] = deepcopy(request)
        store._write(path, state)
        return state


def _readback(project: Path, objective: str, owner: str, key: str, request: dict, result: dict,
              port, *, recovering: bool = False) -> dict:
    from . import ledger as store
    from .github import issue_recheck_live, pod_lines
    source = request["source"]
    held_refusal = result["result"] == "refused"
    accepted = result["result"] == "accepted"
    expected = {**source, "body_sha256": request["intended"]} if not held_refusal else source
    failure, checked = None, None
    reader = _ReadbackPort(port)
    try:
        checked, _ = issue_recheck_live(project, expected, port=reader)
    except PodError as exc:
        failure = exc
    live = reader.live
    now = _hash(live["body"]) if live is not None else None
    path = store._path(project, objective)
    with store._lock(path):
        state = store._read(path)
        store._require_open(state)
        ledger = state.setdefault("pod_ticks", empty())
        current = ledger["requests"].get(key)
        if current is None or current["id"] != request["id"]:
            raise _pending_error(current, "Tick receipt no longer names the latest intent")
        # A recovery that started without a response must retain any held
        # response recorded by the sender while this read was in flight.
        if result["result"] == "unconfirmed" and (current["result"] or {}).get("result") in ("accepted", "refused"):
            result = deepcopy(current["result"])
        held_refusal = result["result"] == "refused"
        accepted = result["result"] == "accepted"
        rebind = not held_refusal and now == request["intended"]
        if accepted and live is not None and now != request["intended"]:
            failure = PodError("issue_reconciliation_required", "Tick readback differs from the intended body",
                               {"reason": "tick_readback_mismatch"})
        if failure is None:
            try:
                # The earlier body is valid after a held refusal or an
                # unconfirmed send; source holds still apply to either.
                if not accepted and now == request["before"]:
                    previous_check = {**checked, "status": "current", "reason": None}
                    if checked.get("amendment_changed"):
                        previous_check.update(status="reconciliation_required", reason="issue_amendment_changed")
                    elif checked.get("reason") not in (None, "issue_body_changed"):
                        previous_check = checked
                    _source_check(previous_check)
                else:
                    _source_check(checked)
            except PodError as exc:
                failure = exc
        if held_refusal and now != request["before"] and failure is None:
            failure = PodError("issue_reconciliation_required", "Refused tick read back an outside edit",
                               {"reason": "issue_body_changed"})
        if failure is not None:
            request["block"] = _block(failure, row="A3" if failure.code == "repository_mismatch" else "B5"
                                      if failure.code == "issue_access_unavailable" else "B3"
                                      if (failure.detail or {}).get("reason") == "closed_issue_requires_intent_reconciliation" else "B2")
        elif held_refusal:
            request["block"] = {"code": "github_write_refused", "reason": "github_write_refused", "row": "E2",
                                "status": result["status"], "message": result.get("message", "")}
        else:
            request["block"] = None
        states = pod_lines(live["body"]).get(int(key), []) if live else []
        request.update(phase="result" if live is None else "completed", result=result,
                       readback={"body_sha256": now, "state": live["state"] if live else None,
                                 "ticked": states[0]["ticked"] if len(states) == 1 else None})
        current_source = state["checkpoint"].get("objective_source")
        already_rebound = (rebind and current_source == {**source, "body_sha256": request["intended"]}
                           and ledger["last_rebind"] == digest(current_source))
        if current_source != source and not already_rebound:
            rebind = False
            failure = PodError("issue_reconciliation_required", "Source rebound while tick was in flight",
                               {"reason": "checkpoint_binding_changed"})
            request["block"] = _block(failure)
        if rebind:
            state["checkpoint"]["objective_source"] = {**source, "body_sha256": now}
            ledger["last_rebind"] = digest(state["checkpoint"]["objective_source"])
            # Pod's own confirmed tick flips one checkbox: no material scope moves.
            from .standing import carry_tick
            carry_tick(state, source)
        if live is not None:
            _facts(ledger, state["checkpoint"]["objective_source"], live["body"], context=state)
            # Drift observations are stale facts: never pretend that their body
            # is the checkpoint binding, even though status retains the readback.
            if now != state["checkpoint"]["objective_source"]["body_sha256"]:
                ledger["facts"]["source"] = digest({**source, "body_sha256": now})
        if rebind and request["direction"] == "tick":
            ledger["authors"][key] = "own" if accepted else "ambiguous"
        ledger["requests"][key] = request
        store._write(path, state)
        ctx = store.kernel_context(project, objective, state, None)
        view = projection(state, store.map_of(state), ctx, project=project, objective=objective)
    receipt = {"pod": int(key), "direction": request["direction"], "before": request["before"],
               "intended": request["intended"], "result": result,
               "readback": request["readback"], "rebind": rebind,
               "authorship": ledger["authors"].get(key, "not_pod"), "recovered": recovering}
    if failure is not None:
        failure.detail = {**(failure.detail or {}), "receipt": receipt, "pod_ticks": view}
        raise failure
    return {"status": "refused" if held_refusal else "confirmed" if accepted and rebind else "unconfirmed",
            "receipt": receipt, "readback": request["readback"],
            "objective_source": state["checkpoint"]["objective_source"], "pod_ticks": view,
            "next_action": "Confirm the readback; record its result in the coordinator report."}

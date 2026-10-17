"""Objective-local standing owner consent and verified responder observations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from .contracts import STANDING_EXCLUSIONS, STANDING_SCOPES, TABLE, fields, required, input_record
from .errors import FieldRefusal, PodError
from .util import bounded_text, digest, encoded, exact, normalize_timestamp, owner_decision

SCHEMA = "pod-standing-authorization/v1"
STATUS_SCHEMA = "pod-standing-authorization-status/v1"
DEFAULT_SCOPE = STANDING_SCOPES[:-1]
MAX_DECISIONS = 32
MAX_ANSWERS = 16
MAX_DECISION_BYTES = TABLE["standing decision"]["stored_max_bytes"]
MAX_ANSWER_BYTES = TABLE["responder answer"]["stored_max_bytes"]
_STORED_DECISION = {"id", "operation", "utc", "seq", "binding", "exclusions", "relay_verification"}


def _clock(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _timestamp(now: datetime) -> str:
    return normalize_timestamp(now.isoformat(), field="standing time", code="invalid_standing_authorization")


def scope_digest(context: dict) -> str:
    checkpoint = context.get("checkpoint") or {}
    binding = {"criteria": checkpoint.get("criteria", [])}
    source = checkpoint.get("objective_source")
    if source is not None:
        binding.update({key: source[key] for key in ("repository", "number", "locator", "amendments")})
        binding["body_scope_sha256"] = context.get("standing_authorization", {}).get("observed", {}).get("body_scope_sha256")
    return digest(binding)


def observe(context: dict, source: dict, body: str) -> None:
    """Record a bound body already read by Pod; checkbox marks carry no scope."""
    if "standing_authorization" not in context or hashlib.sha256(body.encode()).hexdigest() != source["body_sha256"]:
        return
    normalized = re.sub(r"(?m)^([ \t]*(?:[-+*]|[0-9]+[.)])[ \t]+)\[[xX]\](?=[ \t\r]|$)", r"\1[ ]", body)
    context["standing_authorization"]["observed"] = {
        "source": digest(source), "body_scope_sha256": hashlib.sha256(normalized.encode()).hexdigest()}


def suspension_reason(context: dict) -> str | None:
    source = (context.get("checkpoint") or {}).get("objective_source")
    if source is not None and context.get("standing_authorization", {}).get("observed", {}).get("source") != digest(source):
        return "scope_unobserved"
    decisions = context.get("standing_authorization", {}).get("decisions", [])
    if decisions and decisions[-1]["operation"] == "activate" and decisions[-1]["binding"]["scope_digest"] != scope_digest(context):
        return "scope_changed"
    return None


def state(context: dict, *, now: datetime | None = None) -> str:
    """The sole mode calculation; no I/O, mutation, verifier or native observation."""
    decisions = context.get("standing_authorization", {}).get("decisions", [])
    if not decisions:
        return "none"
    if ((context.get("checkpoint") or {}).get("closure") or context.get("continued_by")
            or context.get("historical_retirement")):
        return "ended"
    latest = decisions[-1]
    if any(row["seq"] > latest["seq"] for row in (context.get("checkpoint") or {}).get("reopened", [])):
        return "ended"
    if latest["operation"] == "revoke":
        return "revoked"
    if latest.get("expires_at") is not None:
        expires = datetime.fromisoformat(latest["expires_at"].replace("Z", "+00:00"))
        if expires <= _clock(now):
            return "expired"
    if suspension_reason(context) is not None:
        return "suspended"
    return "active"


def _names(value: object, allowed: tuple, *, nonempty: bool = False) -> list:
    if (not isinstance(value, list) or len(value) > len(allowed) or nonempty and not value
            or any(not isinstance(item, str) or item not in allowed for item in value)
            or len(set(value)) != len(value)):
        raise PodError("invalid_standing_authorization", "Standing scope and widening are bounded unique lists")
    return list(value)


def _decision(value: object, operation: str, now: datetime, *, stored: bool = False) -> dict:
    allowed = fields("standing decision") if operation == "activate" else fields("standing decision") - {"scope", "widening", "expires_at"}
    row = exact(value, allowed, required("standing decision"), name="standing decision")
    row = owner_decision(row, kind="standing_authorization", stored=stored, verified_authorization=True)
    if operation == "activate":
        scope = _names(row.get("scope", list(DEFAULT_SCOPE)), STANDING_SCOPES, nonempty=True)
        widening = _names(row.get("widening", []), STANDING_EXCLUSIONS)
        if "deploy" in scope and "deploy" not in widening:
            raise PodError("invalid_standing_authorization", "Deploy scope requires an explicit deploy widening")
        if "deploy" in widening and "deploy" not in scope:
            scope.append("deploy")
        expiry = row.get("expires_at")
        if expiry is not None:
            expiry = normalize_timestamp(expiry, field="Standing expiry", code="invalid_standing_authorization")
            if not stored and datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= now:
                raise PodError("invalid_standing_authorization", "Standing expiry must be later than now")
        row = {**row, "scope": scope, "widening": widening, "expires_at": expiry}
    return row


def _answer(value: object) -> dict:
    row = exact(value, fields("responder answer"), required("responder answer"), name="responder answer")
    for key, limit in (("question", 512), ("text", 4096)):
        bounded_text(row[key], name=key, limit=limit).encode("utf-8")
    reference = exact(row["relay"], {"reference"}, {"reference"}, name="responder relay")
    bounded_text(reference["reference"], name="reference", limit=512).encode("utf-8")
    return row


def _request_fields(operation: str, decision: dict) -> dict:
    return {"operation": operation, **{key: decision.get(key) for key in ("scope", "widening", "expires_at")}}


def _row_fit(row: dict, *, answer: bool = False) -> None:
    limit = MAX_ANSWER_BYTES if answer else MAX_DECISION_BYTES
    size = len(encoded(row, compact=True))
    if size > limit:
        raise PodError("invalid_standing_authorization", "Standing row exceeds its encoded byte limit",
                       {"row_bytes": size, "limit_bytes": limit,
                        "next_action": "shorten the decision or answer and its relay metadata"})


def validate_record(value: object) -> None:
    """Validate stored facts without treating them as a fresh authorization or answer."""
    from .relay import validate_verdict
    try:
        record = exact(value, {"schema", "decisions", "answers", "observed"}, {"schema", "decisions", "answers"}, name="standing record")
        if record["schema"] != SCHEMA or not isinstance(record["decisions"], list) or not isinstance(record["answers"], list):
            raise ValueError("record")
        if len(record["decisions"]) > MAX_DECISIONS or len(record["answers"]) > MAX_ANSWERS:
            raise ValueError("capacity")
        if "observed" in record:
            observed = exact(record["observed"], {"source", "body_scope_sha256"}, {"source", "body_scope_sha256"}, name="standing observation")
            if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in observed.values()):
                raise ValueError("observation")
        activations = set()
        for index, raw in enumerate(record["decisions"], 1):
            _row_fit(raw)
            row = exact(raw, fields("standing decision") | _STORED_DECISION,
                        {"id", "operation", "utc", "seq", *required("standing decision")}, name="standing decision")
            if row["id"] != f"Y{index}" or row["operation"] not in ("activate", "revoke"):
                raise ValueError("identity")
            if type(row["seq"]) is not int or row["seq"] < 0:
                raise ValueError("seq")
            normalize_timestamp(row["utc"], field="Standing time", code="state_unsupported")
            decision = _decision({key: item for key, item in row.items() if key not in _STORED_DECISION},
                                 row["operation"], _clock(None), stored=True)
            if "channel" not in decision:
                raise ValueError("channel")
            if row["operation"] == "activate":
                if any(key not in row for key in ("scope", "widening", "expires_at", "binding", "exclusions")):
                    raise ValueError("activation")
                if row["scope"] != decision["scope"] or row["exclusions"] != [item for item in STANDING_EXCLUSIONS if item not in row["widening"]]:
                    raise ValueError("exclusions")
                binding = exact(row["binding"], {"scope_digest"}, {"scope_digest"}, name="standing binding")
                if not isinstance(binding["scope_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", binding["scope_digest"]):
                    raise ValueError("binding")
                activations.add(row["id"])
            elif {"scope", "widening", "expires_at", "binding", "exclusions"} & row.keys():
                raise ValueError("revocation")
            if row["channel"] == "relayed":
                verdict = validate_verdict(row.get("relay_verification"))
                if (not verdict["verified"] or verdict["action"] != "standing"
                        or verdict["standing"] != _request_fields(row["operation"], decision)
                        or row["relay"] != {"reference": verdict["reference"], "owner_source": verdict["owner_source"]}):
                    raise ValueError("verdict")
            elif "relay_verification" in row:
                raise ValueError("session verdict")
        for index, raw in enumerate(record["answers"], 1):
            _row_fit(raw, answer=True)
            row = exact(raw, {"id", "question", "text", "decided_by", "responder_source", "relay", "relay_verification", "utc", "under"},
                        {"id", "question", "text", "decided_by", "responder_source", "relay", "relay_verification", "utc", "under"}, name="responder answer")
            _answer({key: row[key] for key in ("question", "text", "relay")})
            normalize_timestamp(row["utc"], field="Answer time", code="state_unsupported")
            verdict = validate_verdict(row["relay_verification"])
            if (row["id"] != f"A{index}" or row["decided_by"] != "responder" or row["under"] not in activations
                    or not verdict["verified"] or verdict["action"] != "answer"
                    or verdict["answer"] != {key: row[key] for key in ("question", "text")}
                    or row["relay"]["reference"] != verdict["reference"] or row["responder_source"] != verdict["responder_source"]):
                raise ValueError("answer verdict")
    except (PodError, ValueError, TypeError, KeyError, UnicodeError):
        raise PodError("state_unsupported", "Standing authorization record is malformed") from None


def _target(project: Path, context: dict) -> dict:
    from . import gitio, relay
    from .github import _github_repository
    base_ref = (context.get("checkpoint") or {}).get("governance", {}).get("base_ref")
    parts = base_ref.removeprefix("refs/remotes/").split("/", 1) if isinstance(base_ref, str) else []
    if len(parts) != 2 or not all(parts):
        raise relay.refusal("relay_verifier_mismatch", fields=["target.repository"])
    remote = gitio.read(project, ["remote", "get-url", "--all", parts[0]])
    urls = remote.stdout.splitlines() if remote is not None and remote.returncode == 0 else []
    repository = _github_repository(urls[0]) if len(urls) == 1 else None
    if repository is None:
        raise relay.refusal("relay_verifier_mismatch", fields=["target.repository"])
    return {"repository": repository, "remote": parts[0], "base": parts[1]}


def _verify(project: Path, objective: str, context: dict, reference: str, action: str, binding: dict) -> dict:
    from . import relay
    from .config import effective
    command = effective(project)["preferences"].get("authorization_verifier")
    if not command:
        raise relay.refusal("relay_verifier_unconfigured")
    request = {"schema": relay.SCHEMA, "reference": reference, "action": action, "objective": objective,
               "candidate": None, "target": _target(project, context), "pr": None, action: binding}
    verdict = relay.verify(command, request)
    record = context.get("standing_authorization", {})
    if any(row.get("relay_verification", {}).get("single_use") == verdict["single_use"]
           for row in [*record.get("decisions", []), *record.get("answers", [])]):
        raise relay.refusal("relay_authorization_reused")
    # An identical receipt without its row is an interrupted write, which may finish once.
    relay.check_use(project, verdict)
    return verdict


@input_record("standing authorization")
def operate(request: dict, *, now: datetime | None = None) -> dict:
    from .ledger import _lock, _path, _read, _require_open, _write, require_authority
    from .governor import _read_journal, _record_path
    try:
        exact(request, fields("standing authorization"), required("standing authorization"), name="standing authorization")
        for key in ("project", "objective", "owner"):
            bounded_text(request[key], name=key, limit=4096)
        if not Path(request["project"]).is_absolute():
            raise ValueError("project")
        action = request["action"]
        expected = "decision" if action in ("activate", "revoke") else "answer" if action == "answer" else None
        if (action not in ("activate", "revoke", "answer", "show")
                or set(request) != required("standing authorization") | ({expected} if expected else set())):
            raise ValueError("action fields")
        moment = _clock(now)
        decision = _decision(request["decision"], action, moment) if expected == "decision" else None
        answer = _answer(request["answer"]) if expected == "answer" else None
    except FieldRefusal as exc:
        exc.code = "invalid_standing_authorization"
        raise
    except (PodError, ValueError, TypeError, UnicodeError) as exc:
        raise PodError("invalid_standing_authorization", "Malformed standing authorization input") from exc
    project, objective, owner = Path(request["project"]), request["objective"], request["owner"]
    path = _path(project, objective)
    with _lock(path):
        context = _read(path)
        try:
            journal = _read_journal(_record_path(project, objective))
        except PodError:
            journal = None
        if action == "show":
            return {"standing_authorization": projection(context, journal, now=moment)}
        if context["owner"] != owner:
            from .handoff import refuse_nonowner
            refuse_nonowner(project, objective, context, owner, code="coordinator_conflict")
            raise PodError("coordinator_conflict", "Standing authorization belongs to another coordinator")
        _require_open(context)
        require_authority(project, objective, owner=owner, state=context)
        current = state(context, now=moment)
        record = context.get("standing_authorization", {"schema": SCHEMA, "decisions": [], "answers": []})
        verdict = None
        if decision is not None and decision["channel"] == "relayed":
            verdict = _verify(project, objective, context, decision["relay"]["reference"], "standing", _request_fields(action, decision))
        elif action == "answer" and record["answers"]:
            verdict = _verify(project, objective, context, answer["relay"]["reference"], "answer",
                              {key: answer[key] for key in ("question", "text")})
        if action == "revoke" and current not in ("active", "suspended", "expired"):
            return {"state": current, "changed": False}
        context["standing_authorization"] = record
        if action == "answer":
            if current != "active":
                raise PodError("responder_answer_requires_yolo", "Responder answers require active yolo",
                               {"next_action": "ask the owner as before; the responder's pick may be recorded in the question's choices as a recommendation"})
            questions = (context.get("checkpoint") or {}).get("questions", [])
            if not isinstance(questions, list) or not any(question == answer["question"] or isinstance(question, dict)
                    and answer["question"] in (question.get("id"), question.get("reference")) for question in questions):
                raise PodError("decision_question_unrecorded", "Responder answer must name a currently recorded question",
                               {"next_action": "record the question first"})
            if verdict is None:
                verdict = _verify(project, objective, context, answer["relay"]["reference"], "answer",
                                  {key: answer[key] for key in ("question", "text")})
            if len(record["answers"]) >= MAX_ANSWERS:
                raise PodError("responder_answers_full", "Responder answer history is full")
            row = {**answer, "id": f"A{len(record['answers']) + 1}", "decided_by": "responder",
                   "responder_source": verdict["responder_source"], "relay_verification": verdict,
                   "utc": _timestamp(moment), "under": record["decisions"][-1]["id"]}
            _row_fit(row, answer=True)
            record["answers"].append(row)
        else:
            if action == "activate" and len(record["decisions"]) >= MAX_DECISIONS - 1:
                raise PodError("standing_authorization_full", "Standing owner-decision history is full")
            if action == "activate" and (context.get("checkpoint") or {}).get("objective_source") is not None:
                from .github import issue_recheck_live
                source = context["checkpoint"]["objective_source"]
                checked, live = issue_recheck_live(project, source)
                if checked["status"] != "current":
                    raise PodError("issue_reconciliation_required", "Execution Spec changed or closed; reconcile before activating yolo",
                                   {"reason": checked.get("reason")})
                observe(context, source, live["body"])
            row = {**decision, "id": f"Y{len(record['decisions']) + 1}", "operation": action,
                   "utc": _timestamp(moment), "seq": (context.get("checkpoint") or {}).get("seq", 0)}
            if action == "activate":
                row.update(binding={"scope_digest": scope_digest(context)},
                           exclusions=[item for item in STANDING_EXCLUSIONS if item not in row["widening"]])
            if decision["channel"] == "relayed":
                row.update(relay={"reference": verdict["reference"], "owner_source": verdict["owner_source"]}, relay_verification=verdict)
            _row_fit(row)
            record["decisions"].append(row)
        # Validate fit before consuming an otherwise valid receipt. The context writer is atomic.
        validate_record(record)
        from .history import prepare_context
        from .util import protect_decision_record
        proposed = {**context, "revision": context["revision"] + 1}
        protect_decision_record(proposed)
        prepare_context(path, proposed)
        if verdict is not None:
            from .relay import check_use
            check_use(project, verdict, consume=True)
        _write(path, context)
        return {"standing_authorization": projection(context, journal, now=moment),
                "answer" if action == "answer" else "decision": row, "context_revision": context["revision"]}


def projection(context: dict, journal: dict | None = None, *, now: datetime | None = None,
               closure_utc: str | None = None) -> dict:
    record = context.get("standing_authorization", {})
    decisions = record.get("decisions", [])
    latest = next((row for row in reversed(decisions) if row["operation"] == "activate"), None)
    decision = None
    if latest is not None:
        decision = {key: latest[key] for key in ("id", "scope", "exclusions", "widening", "expires_at", "channel")}
        decision["since"] = latest["utc"]
        if latest["channel"] == "relayed":
            decision.update(relay_reference=latest["relay"]["reference"], owner_source=latest["relay"]["owner_source"])
    effects = []
    for row in (journal or {}).get("actions", []):
        action = row.get("action", {})
        consent = action.get("authorization") or {}
        if "standing" not in consent:
            continue
        effects.append({"record_id": row["record_id"], "kind": action["kind"], "scope": consent["scope"],
                        "candidate": consent["candidate"], "tree": consent["tree"], "target": consent.get("target"),
                        "pr": consent.get("pr"), "decision": row["decision"], "outcome": row["outcome"],
                        "at": row["at"], "standing": dict(consent["standing"])})
    answers = [{**{key: row[key] for key in ("id", "question", "text", "responder_source", "under")}, "at": row["utc"]}
               for row in record.get("answers", [])]
    current = state(context, now=now)
    result = {"schema": STATUS_SCHEMA, "mode": "yolo" if current == "active" else "normal", "state": current,
            "reason": suspension_reason(context) if current == "suspended" else None,
            "decision": decision,
            "effects": {"total": len(effects), "truncated": len(effects) > 64, "items": effects[-64:]},
            "responder_decisions": {"total": len(answers), "truncated": len(answers) > 64, "items": answers[-64:]},
            "history": {"total": len(decisions), "items": [{**{key: row[key] for key in ("id", "operation", "channel")},
                                                           "at": row["utc"]} for row in decisions]}}
    checkpoint = context.get("checkpoint") or {}
    closure = checkpoint.get("closure")
    if closure and record:
        stored = closure.get("report", {}).get("standing_authorization", {}).get("at_closure", {})
        stamp = normalize_timestamp(stored.get("utc") or closure_utc or _timestamp(_clock(now)),
                                    field="Standing closure time", code="state_unsupported")
        before = {**context, "checkpoint": {**checkpoint, "closure": None}}
        active = state(before, now=datetime.fromisoformat(stamp.replace("Z", "+00:00"))) == "active"
        result["at_closure"] = {"utc": stamp, "mode": "yolo" if active else "normal",
                                "ran_in_yolo": latest is not None,
                                "decision": latest["id"] if latest else None, "active_until_closure": active}
    return result


def synthesize(context: dict, binding: dict | None, unit: dict | None, scopes: set[str], *,
               pr: int | None = None, now: datetime | None = None) -> tuple[dict | None, str]:
    from .governor import merge_target_name
    current = state(context, now=now)
    if current != "active":
        return None, f"yolo is {current}"
    record = context["standing_authorization"]["decisions"][-1]
    missing = scopes - set(record["scope"])
    if missing:
        return None, "yolo does not cover " + ", ".join(sorted(missing))
    target = merge_target_name(unit)
    if not binding or target is None:
        return None, "yolo needs a prepared candidate and target"
    if "merge" in scopes and pr is None:
        return None, "yolo merge needs action.pr"
    index = len(context["standing_authorization"]["decisions"]) - 1
    return {"schema": "pod-authorization/v1", "candidate": binding["commit"], "tree": binding["tree"],
            "scope": sorted(scopes), "utc": _timestamp(_clock(now)), "target": target,
            **({"pr": pr} if "merge" in scopes else {}),
            "standing": {"id": record["id"], "reference": f"standing_authorization/decisions/{index}",
                         "channel": record["channel"]}}, ""


def cleanup_projection(context: dict, resources: list[dict]) -> dict:
    summary = projection(context)
    decision = summary["decision"]
    active = summary["mode"] == "yolo" and "cleanup" in decision["scope"]
    covers, asks = [], []
    for row in resources:
        covered = (active and row["class"] != "protected" and (row["class"] in ("integrated", "merged_remote_head")
                   or row["class"] == "unique" and "unique_deletion" in decision["widening"]))
        (covers if covered else asks).append(row["id"])
    return {"mode": summary["mode"], "decision": decision, "covers": covers, "asks": asks}


def mode_line(summary: dict) -> str:
    if summary["mode"] == "yolo":
        decision = summary["decision"]
        return f"Mode: yolo ({', '.join(decision['scope'])}; {decision['channel']} since {decision['since']})"
    reason = {"suspended": "scope unobserved" if summary.get("reason") == "scope_unobserved" else "scope changed",
              "expired": "expiry reached", "revoked": "owner revoked", "ended": "objective ended"}.get(summary["state"])
    return "Mode: normal" + (f" (yolo {summary['state']}: {reason})" if reason else "")

"""One checkpoint-only issue-source reconciliation, never general owner authority."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .errors import PodError
from .contracts import bound, fields, input_record
from .util import MAX_RECORD, bounded_text, digest, exact

OPERATION = "issue_revision"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_INPUT = fields("issue revision authority")
_STORED = _INPUT | {"seq", "kind", "relay_verification", "previous", "source", "criteria", "retired"}


def requested(value: object) -> bool:
    return isinstance(value, dict) and value.get("operation") == OPERATION


def refuse_alternate(value: object) -> None:
    if isinstance(value, dict) and (requested(value.get("revision_authority")) or "issue_revision_data" in value):
        raise refuse("checkpoint_only")


def refuse(reason: str, **detail) -> PodError:
    return PodError("issue_revision_refused", "Issue revision refused: " + reason,
                    {"reason": reason, "next_action": "use a checkpoint with exact source-derived criteria and a host-verified issue_revision reference; otherwise obtain a coordinator-session revision", **detail})


@input_record("issue revision authority")
def input_authority(value: object) -> dict:
    row = exact(value, _INPUT, _INPUT, name="issue revision authority")
    if (row["operation"] != OPERATION or row["provenance"] != "user_direct"
            or row["channel"] != "relayed"):
        raise refuse("honest_relay_required")
    bounded_text(row["instruction"], name="instruction", limit=bound("revision authority", "instruction"))
    reference = exact(row["relay"], {"reference"}, {"reference"}, name="issue revision relay")
    bounded_text(reference["reference"], name="reference", limit=bound("issue revision relay", "reference"))
    try:
        row["instruction"].encode("utf-8")
        reference["reference"].encode("utf-8")
    except UnicodeError:
        raise refuse("utf8_required") from None
    return deepcopy(row)


def validate_binding(value: object, target: dict) -> dict:
    """The exact agreed wire binding; equal source digests have distinct semantic roles."""
    row = exact(value, {"source", "map", "coordinator"}, {"source", "map", "coordinator"}, name="issue revision binding")
    target = exact(target, {"repository", "remote", "base"}, {"repository", "remote", "base"}, name="issue revision target")
    source = exact(row["source"], {"repository", "number", "locator", "previous_digest", "proposed_digest"},
                   {"repository", "number", "locator", "previous_digest", "proposed_digest"}, name="issue revision source")
    from .github import parse_issue_locator
    parsed = parse_issue_locator(source["locator"])
    if (type(source["number"]) is not int or source["number"] < 1
            or parsed != {key: source[key] for key in ("repository", "number", "locator")}
            or source["repository"] != target.get("repository")
            or any(not isinstance(source[key], str) or not _SHA.fullmatch(source[key])
                   for key in ("previous_digest", "proposed_digest"))):
        raise refuse("source_identity")
    mapping = exact(row["map"], {"seq", "revision", "policy_revision"}, {"seq", "revision", "policy_revision"}, name="issue revision map")
    if any(type(mapping[key]) is not int or mapping[key] < 1 for key in ("seq", "revision")):
        raise refuse("map_identity")
    bounded_text(mapping["policy_revision"], name="policy_revision", limit=256)
    native = exact(row["coordinator"], {"handle", "incarnation", "run", "runtime", "generation"},
                   {"handle", "incarnation", "run", "runtime", "generation"}, name="issue revision coordinator")
    for key in ("handle", "incarnation", "run", "runtime"):
        bounded_text(native[key], name=key, limit=256).encode("utf-8")
    if type(native["generation"]) is not int or native["generation"] < 0:
        raise refuse("native_generation")
    return row


def _binding(project: Path, objective: str, owner: str, state: dict, source: dict, reference: str) -> dict:
    from . import ledger, orca, relay
    from .config import effective
    from .github import validate_issue_binding
    from .standing import _target
    previous = state.get("checkpoint") or {}
    old = validate_issue_binding(previous.get("objective_source"))
    validate_issue_binding(source)
    identity = ("repository", "number", "locator")
    if (type(old["number"]) is not int or type(source["number"]) is not int
            or any(source[key] != old[key] for key in identity)):
        raise refuse("source_retarget")
    target = _target(project, state)
    authority = ledger.require_authority(project, objective, owner=owner, state=state, persist_rebind=False)
    if authority.get("continuity") is not None:
        raise refuse("native_continuity_requires_separate_checkpoint")
    before = orca.current_run()
    shown = orca.read_command(["terminal", "show", "--terminal", owner, "--json"])
    terminal = shown["result"].get("terminal")
    after = orca.current_run()
    run = before.get("run") or {}
    if (before != after or state.get("owner") != owner or run.get("coordinator_handle") != owner
            or authority["references"].get(run.get("id")) != before.get("runtime")
            or before.get("runtime") != authority["runtime"] or shown["runtime"] != authority["runtime"]
            or not isinstance(terminal, dict) or terminal.get("handle") != owner
            or terminal.get("connected") is not True or terminal.get("writable") is not True
            or terminal.get("orphaned") is not False):
        raise refuse("native_binding_unverified")
    binding = {"source": {**{key: source[key] for key in identity}, "previous_digest": digest(old), "proposed_digest": digest(source)},
               "map": {"seq": previous.get("seq"), "revision": previous.get("revision"),
                       "policy_revision": effective(project)["revision"]},
               "coordinator": {"handle": owner, "incarnation": terminal.get("incarnationId"), "run": run.get("id"),
                               "runtime": authority["runtime"], "generation": run.get("consumer_generation")}}
    validate_binding(binding, target)
    return {"schema": relay.SCHEMA, "reference": reference, "action": OPERATION, "objective": objective,
            "candidate": None, "target": target, "pr": None, OPERATION: binding}


def _lines(project: Path, source: dict) -> dict[str, str]:
    from .github import issue_recheck_live, parse_issue_locator
    checked, live = issue_recheck_live(project, source)
    if checked["status"] != "current" or live is None:
        raise refuse("source_not_current")
    identity = parse_issue_locator(live.get("url"))
    if (type(live.get("number")) is not int or live["number"] != source["number"]
            or identity != {key: source[key] for key in ("repository", "number", "locator")}):
        raise refuse("live_source_identity")
    return {ref: block["text"] for ref, block in _source_blocks(live["body"]).items()}


def _source_blocks(body: str) -> dict:
    from .github import pod_blocks
    lines = pod_blocks(body)
    if not lines or len(lines) > 64 or any(len(rows) != 1 for rows in lines.values()):
        raise refuse("source_pod_mapping")
    return {f"PoD#{number}": rows[0]
            for number, rows in lines.items()}


def source_check(ref: str, text: str) -> str:
    """One strict bounded check representation, including every byte of long blocks."""
    if len(text) <= bound("obligation", "check"):
        return text
    return f"{ref} complete source block sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def criteria_view(body: str) -> dict:
    """Bounded current-body DATA on the existing intake read, never approval."""
    try:
        blocks = _source_blocks(body)
    except PodError:
        return {"status": "unavailable", "reason": "source_pod_mapping", "criteria": []}
    return {"status": "ready", "reason": None, "criteria": [
        {"ref": ref, "check": source_check(ref, block["text"]), "block_sha256": block["sha256"]}
        for ref, block in blocks.items()]}


@input_record("issue revision data")
def body_data(value: object) -> dict[str, str]:
    if value is None:
        return {}
    data = exact(value, {"historical_bodies"}, {"historical_bodies"}, name="issue revision data")
    rows = data["historical_bodies"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise refuse("malformed_body_data")
    bodies = {}
    total = 0
    for row in rows:
        row = exact(row, {"body_sha256", "body"}, {"body_sha256", "body"}, name="issue body data")
        key, body = row["body_sha256"], row["body"]
        if (not isinstance(key, str) or not _SHA.fullmatch(key) or key in bodies
                or not isinstance(body, str)):
            raise refuse("malformed_body_data")
        try:
            raw = body.encode("utf-8")
        except UnicodeError:
            raise refuse("utf8_required") from None
        total += len(raw)
        if len(raw) > 512 * 1024 or total > MAX_RECORD:
            raise refuse("body_data_too_large")
        if hashlib.sha256(raw).hexdigest() != key:
            raise refuse("body_data_digest_mismatch")
        bodies[key] = body
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RECORD:
        raise refuse("body_data_too_large")
    return bodies


def _prior_scopes(state: dict, bodies: dict, proposed_source: dict) -> dict:
    """Resolve each selected proof at its actual map sequence, or genuine introduction."""
    from .github import pod_blocks, pod_ref_number, validate_issue_binding
    from .pod_ticks import proof_identity, requirement_scope, selected_pass_receipts
    prior = state["checkpoint"]
    rows = [row for row in prior["obligations"] if row["kind"] == "criterion"
            and row["provenance"] == "objective" and row["state"] != "withdrawn"]
    associations = {}
    requirements = {}
    for row in rows:
        selected = selected_pass_receipts(row)
        associations[row["id"]] = [{"seq": receipt.get("accepted_seq"), "receipt": receipt, "sources": {}}
                                   for receipt in selected] or [{"seq": row["introduced_seq"], "receipt": None, "sources": {}}]
        local = state.get("pod_ticks", {}).get("criterion_scopes", {}).get(row["id"], {})
        if local.get("definition") != row["definition"] or requirement_scope(local) is not None:
            continue
        accepted = local.get("acceptance")
        # A pre-extension marker can recover only a positively retained initial
        # conjunction. A later owner decision may have accepted another epoch,
        # even if the logical hash has since returned to its original value.
        if accepted is not None or not any(
                decision.get("kind") in ("revision", "question")
                and decision.get("seq", 0) > row["introduced_seq"]
                for decision in prior.get("revision_decisions", [])):
            requirements[row["id"]] = {"seq": accepted["seq"] if accepted else row["introduced_seq"],
                                       "receipt": None, "sources": {}, "acceptance": accepted}
    def slots(row):
        requirement = requirements.get(row["id"])
        return associations[row["id"]] + ([requirement] if requirement is not None else [])
    def add(slot, source):
        if source is None:
            return
        validate_issue_binding(source)
        accepted = slot.get("acceptance")
        if accepted is not None and digest(source) != accepted["source_digest"]:
            return
        if all(source[key] == proposed_source[key] for key in ("repository", "number", "locator")):
            slot["sources"][digest(source)] = source
    def snapshot(value, seq):
        old = {row["id"]: row for row in value.get("obligations", [])}
        for row in rows:
            recorded = old.get(row["id"], {})
            if (recorded.get("definition") != row["definition"] or recorded.get("source") != row.get("source")
                    or row.get("source", {}).get("ref") not in value.get("criteria", [])):
                continue
            for slot in slots(row):
                if slot["seq"] != seq:
                    continue
                receipt = slot["receipt"]
                if receipt is not None and not any(
                        item.get("accepted_seq") == seq
                        and all(item["evidence"].get(key) == receipt["evidence"].get(key)
                                for key in ("reference", "criterion", "definition", "status"))
                        for item in recorded.get("receipts", [])):
                    continue
                add(slot, value["source"])
    snapshot({**prior, "source": prior["objective_source"]}, prior["seq"])
    for decision in prior.get("revision_decisions", []):
        if decision.get("operation") != OPERATION:
            continue
        snapshot(decision["previous"], decision["relay_verification"][OPERATION]["map"]["seq"])
    for admission in state.get("admissions", {}).values():
        checkpoint = admission.get("recovery", {}).get("checkpoint_binding", {})
        definitions = (admission.get("binding") or {}).get("definitions", {})
        for row in rows:
            if (definitions.get(row["id"]) == row["definition"]
                    and row["id"] in admission.get("serves", [])
                    and row.get("source", {}).get("ref") in checkpoint.get("criteria", [])):
                for slot in slots(row):
                    if type(slot["seq"]) is int and admission.get("admitted_seq") == slot["seq"] + 1:
                        add(slot, checkpoint.get("objective_source"))
    for request in state.get("pod_ticks", {}).get("requests", {}).values():
        for row in rows:
            for slot in associations[row["id"]]:
                # The receipt proves this definition belonged to the same map.
                # source.ref/kind/provenance are immutable; checkbox rebinds do
                # not advance seq or alter scope. A later observation does.
                if (slot["receipt"] is not None and type(slot["seq"]) is int
                        and row["introduced_seq"] <= slot["seq"] == request["seq"]
                        and slot["receipt"]["evidence"].get("criterion") == row["id"]):
                    add(slot, request["source"])
    referenced = {source["body_sha256"] for slots in associations.values()
                  for slot in slots for source in slot["sources"].values()}
    referenced.update(source["body_sha256"] for slot in requirements.values()
                      for source in slot["sources"].values())
    referenced.update(slot["acceptance"]["body_sha256"] for slot in requirements.values()
                      if slot["acceptance"] is not None)
    if set(bodies) - referenced:
        raise refuse("body_data_unreferenced")
    parsed = {key: pod_blocks(body) for key, body in bodies.items()}
    resolved = {}
    for row in rows:
        fact = {"id": row["id"], "definition": row["definition"], "status": "unknown",
                "reason": "prior_source_association_unavailable", "scope": None}
        local = state.get("pod_ticks", {}).get("criterion_scopes", {}).get(row["id"], {})
        candidates = []
        if local.get("definition") == row["definition"]:
            scope = requirement_scope(local)
            if scope is None:
                requirement = requirements.get(row["id"], {})
                accepted = requirement.get("acceptance")
                bindings = ([accepted] if accepted is not None else [
                    {"source_digest": key, "body_sha256": source["body_sha256"],
                     "amendments_digest": digest(source["amendments"])}
                    for key, source in requirement.get("sources", {}).items()])
                recovered = []
                for binding in bindings:
                    blocks = parsed.get(binding["body_sha256"], {}).get(
                        pod_ref_number(row.get("source", {}).get("ref")), [])
                    if len(blocks) != 1:
                        break
                    recovered.append({"definition": row["definition"], "source_digest": binding["source_digest"],
                                      "amendments_digest": binding["amendments_digest"], "block_sha256": blocks[0]["sha256"]})
                else:
                    if len({(item["block_sha256"], item["amendments_digest"]) for item in recovered}) == 1:
                        scope = recovered[0]
                if scope is None:
                    resolved[row["id"]] = fact
                    continue  # No late-source or original-hash resurrection.
            candidates.append(scope)
            if associations[row["id"]][0]["receipt"] is None:
                resolved[row["id"]] = {**fact, "status": "known", "reason": None, "scope": deepcopy(scope)}
                continue
        reason = fact["reason"]
        for slot in associations[row["id"]]:
            proof = local.get("proofs", {}).get(proof_identity(slot["receipt"])) if slot["receipt"] else None
            if proof is not None:
                candidates.append(proof)
            sources = slot["sources"]
            if not sources and proof is None:
                break
            if sources and len({digest(source["amendments"]) for source in sources.values()}) != 1:
                reason = "prior_source_scope_ambiguous"
                break
            available = [(key, source) for key, source in sorted(sources.items()) if source["body_sha256"] in parsed]
            if not available and proof is None:
                reason = "historical_body_unavailable"
                break
            for source_digest, source in available:
                blocks = parsed[source["body_sha256"]].get(pod_ref_number(row.get("source", {}).get("ref")), [])
                if len(blocks) != 1:
                    reason = "prior_pod_scope_unavailable"
                    break
                candidates.append({"definition": row["definition"], "source_digest": source_digest,
                                   "amendments_digest": digest(source["amendments"]), "block_sha256": blocks[0]["sha256"]})
            else:
                continue
            break
        else:
            identities = {(scope["block_sha256"], scope["amendments_digest"]) for scope in candidates}
            if len(identities) == 1:
                fact.update(status="known", reason=None, scope=candidates[0])
            elif len(identities) > 1:
                reason = "prior_source_scope_ambiguous"
        if fact["status"] == "unknown":
            fact["reason"] = reason
        resolved[row["id"]] = fact
    return resolved


class Revision:
    """Kernel-owned capability scoped to one validated transition under the objective lock."""

    def __init__(self, project, objective, owner, state, core, proposed, *, data=None):
        from . import obligations, relay
        from .config import effective
        prior = state.get("checkpoint") or {}
        if not prior.get("obligations") or prior.get("closure"):
            raise refuse("existing_open_issue_map_required")
        self.original = deepcopy(state)
        self.source = deepcopy(core.get("objective_source"))
        self.input = input_authority(proposed.get("revision_authority"))
        if set(proposed) - {"seq", "obligations", "revision_authority", "update"}:
            raise refuse("combined_operation")
        if any(core.get(key) != prior.get(key) for key in set(core) | set(prior)
               if key not in obligations.MAP_STORED | {"criteria", "objective_source", "plan_revision", "next_safe_action", "objective", "pod_version", "bundle_digest", "continuity"}):
            raise refuse("unrelated_checkpoint_change")
        if core["policy_revision"] != prior["policy_revision"]:
            raise refuse("policy_changed")
        expanded = obligations.expand_update(prior, proposed)
        if type(expanded.get("seq")) is not int or expanded["seq"] != prior["seq"] + 1:
            raise obligations.refuse("map_stale", "map_stale", "issue revision needs the next sequence", expected=prior["seq"] + 1)
        self.lines = _lines(project, self.source)
        if core["criteria"] != list(self.lines):
            raise refuse("criteria_not_exact_source")
        rows = expanded.get("obligations", prior["obligations"])
        if not isinstance(rows, list) or len(rows) > obligations.MAX_OBLIGATIONS:
            raise refuse("malformed_obligations")
        previous_rows = {row["id"]: row for row in prior["obligations"]}
        data = body_data(data)
        scope_facts = _prior_scopes(state, data, self.source)
        def unchanged_scope(row):
            scope = scope_facts[row["id"]]["scope"]
            text = self.lines.get(row.get("source", {}).get("ref"))
            if text is None:
                return False
            if scope is None:
                return None
            return (scope["block_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
                    and scope["amendments_digest"] == digest(self.source["amendments"]))
        current = {}
        self.withdrawn_ids = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise refuse("malformed_obligation")
            obligations._structure(row)
            if row.get("kind") == "criterion" and not isinstance(row.get("source"), dict):
                raise refuse("malformed_criterion_source")
            if row.get("state") == "withdrawn" and not isinstance(row.get("withdrawal"), dict):
                raise refuse("malformed_withdrawal")
            earlier = previous_rows.get(row["id"])
            if earlier is not None:
                if any(key in row and row[key] != earlier.get(key)
                       for key in ("introduced_seq", "definition", "receipts", "findings", "finding", "acceptances")):
                    raise refuse("kernel_history_change")
                normalized = obligations.restatable_rows({"obligations": [obligations._structure(row)]})[0]
                old_normalized = obligations.restatable_rows({"obligations": [earlier]})[0]
            if earlier is not None and normalized != old_normalized:
                expected = {key: item for key, item in old_normalized.items()
                            if key not in {"state", "executor", "wait", "external", "evidence", "reuse", "withdrawal"}}
                actual = {key: item for key, item in normalized.items()
                          if key not in {"state", "executor", "wait", "external", "evidence", "reuse", "withdrawal"}}
                if (earlier["kind"] != "criterion" or earlier["provenance"] != "objective"
                        or row.get("state") != "withdrawn" or actual != expected
                        or row.get("withdrawal", {}).get("by") != OPERATION
                        or earlier["state"] == "withdrawn"):
                    raise refuse("unrelated_obligation_change")
                self.withdrawn_ids.append(row["id"])
                if unchanged_scope(earlier):
                    raise refuse("unchanged_criterion_retirement")
            if (row.get("kind") == "criterion" and row.get("provenance") == "objective"
                    and row.get("state") != "withdrawn"):
                ref = row.get("source", {}).get("ref")
                if (not isinstance(ref, str) or ref not in self.lines or ref in current
                        or row.get("provenance") != "objective" or set(row.get("source", {})) != {"ref"}
                        or any(key in row for key in ("resolves", "stop_condition", "adopts"))):
                    raise refuse("criterion_not_exact_source")
                if earlier is not None:
                    same = unchanged_scope(earlier)
                    if same is None:
                        raise refuse("prior_criterion_source_unavailable", criterion=earlier["id"],
                            definition=earlier["definition"], missing_fact="complete prior physical source block bound to this criterion definition",
                            fact_reason=scope_facts[earlier["id"]]["reason"],
                            next_action="supply hash-matching body DATA for a genuine retained association, or explicitly remap the unknown criterion with fresh proof pending")
                    if not same:
                        raise refuse("changed_criterion_requires_replacement")
                elif row.get("check") != source_check(ref, self.lines[ref]):
                    raise refuse("criterion_not_exact_source")
                current[ref] = {"id": row["id"], "ref": ref, "check": row["check"],
                                "definition": obligations.definition_id(row)}
            if earlier is None:
                if (row.get("kind"), row.get("provenance")) not in (("criterion", "objective"), ("subgoal", "coordinator"), ("assurance", "coordinator")):
                    raise refuse("unrelated_obligation_injection")
                if row.get("state") in ("satisfied", "withdrawn") or any(key in row for key in ("evidence", "receipts", "acceptances", "reuse", "adopts")):
                    raise refuse("new_proof_forbidden")
        previous_criteria = {row.get("source", {}).get("ref", ""): row
                             for row in prior["obligations"] if row["kind"] == "criterion"
                             and row["provenance"] == "objective" and row["state"] != "withdrawn"}
        material = (set(previous_criteria) != set(current)
                    or any(not unchanged_scope(row) for row in previous_criteria.values()))
        if set(current) != set(self.lines) or not material:
            raise refuse("material_complete_reconciliation_required")
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            if row["id"] in previous_rows or row.get("kind") == "criterion":
                continue
            seen = set()
            cursor = row
            while cursor.get("parent") in by_id and cursor["id"] not in seen:
                seen.add(cursor["id"])
                cursor = by_id[cursor["parent"]]
            if cursor["id"] not in {item["id"] for item in current.values()}:
                raise refuse("decomposition_outside_live_criteria")
        self.criteria = [current[key] for key in self.lines]
        from .pod_ticks import mapping
        from .github import pod_ref_number
        mapped = mapping({"obligations": rows, "revision_decisions": [{"operation": OPERATION, "criteria": self.criteria}]})
        expected = {pod_ref_number(item["ref"]): item["id"] for item in self.criteria}
        if (set(mapped) != set(expected) or any(len(mapped[number]) != 1
                or mapped[number][0]["id"] != key for number, key in expected.items())):
            raise refuse("current_source_mapping_ambiguous")
        self.current_scopes = {item["id"]: {"definition": item["definition"], "source_digest": digest(self.source),
            "amendments_digest": digest(self.source["amendments"]),
            "block_sha256": hashlib.sha256(self.lines[item["ref"]].encode("utf-8")).hexdigest()}
            for item in self.criteria}
        self.request = _binding(project, objective, owner, state, self.source, self.input["relay"]["reference"])
        self.command = effective(project)["preferences"].get("authorization_verifier")
        self.verdict = relay.verify(self.command, self.request)
        if any(row.get("relay_verification", {}).get("single_use") == self.verdict["single_use"]
               for row in prior.get("revision_decisions", [])):
            raise relay.refusal("relay_authorization_reused")
        self.authority = {**self.input, "relay": {"reference": self.verdict["reference"], "owner_source": self.verdict["owner_source"]},
                          "relay_verification": self.verdict,
                          "previous": {"source": deepcopy(prior["objective_source"]), "criteria": deepcopy(prior["criteria"]),
                                       "obligations": deepcopy(prior["obligations"]), "pod_ticks": deepcopy(state.get("pod_ticks"))},
                          "source": self.source, "criteria": self.criteria, "retired": self.withdrawn_ids,
                          "scope_facts": {"previous": list(scope_facts.values()), "current": self.current_scopes,
                                          "data_digests": sorted(data)}}

    def consume(self, project, objective, owner, path, state):
        from . import ledger, relay
        from .config import effective
        transition = digest({"previous": self.original, "checkpoint": state["checkpoint"], "pod_ticks": state.get("pod_ticks")})
        if digest(ledger._read(path)) != digest(self.original):
            raise refuse("checkpoint_changed")
        request = _binding(project, objective, owner, deepcopy(self.original), self.source, self.request["reference"])
        if (request != self.request or effective(project)["preferences"].get("authorization_verifier") != self.command
                or _lines(project, self.source) != self.lines):
            raise refuse("binding_changed_before_consumption")
        if relay.verify(self.command, request) != self.verdict:
            raise relay.refusal("relay_verifier_mismatch")
        if (_binding(project, objective, owner, deepcopy(self.original), self.source, self.request["reference"]) != request
                or _lines(project, self.source) != self.lines
                or effective(project)["revision"] != request[OPERATION]["map"]["policy_revision"]
                or effective(project)["preferences"].get("authorization_verifier") != self.command):
            raise refuse("binding_changed_before_consumption")
        relay.check_use(project, self.verdict, consume=True, transition=transition)


def validate_decision(row: dict, checkpoint: dict) -> None:
    try:
        _validate_decision(row, checkpoint)
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError):
        raise PodError("state_unsupported", "Stored issue revision evidence is malformed") from None


def _validate_decision(row: dict, checkpoint: dict) -> None:
    """Stored-only evidence is fully retained; old helpers fail at decision_channels=4."""
    from . import obligations, relay
    from .github import validate_issue_binding
    exact(row, _STORED | {"scope_facts"}, _STORED, name="stored issue revision")
    input_authority({**{key: row[key] for key in _INPUT}, "relay": {"reference": row["relay"]["reference"]}})
    verdict = relay.validate_verdict(row["relay_verification"])
    if (row["kind"] != OPERATION or verdict["action"] != OPERATION or verdict["verified"] is not True
            or checkpoint.get("objective") is not None and verdict["objective"] != checkpoint["objective"]
            or row["relay"] != {"reference": verdict["reference"], "owner_source": verdict["owner_source"]}
            or type(row["seq"]) is not int or row["seq"] != verdict[OPERATION]["map"]["seq"] + 1
            or row["seq"] > checkpoint["seq"]):
        raise refuse("stored_verification_binding")
    previous = exact(row["previous"], {"source", "criteria", "obligations", "pod_ticks"},
                     {"source", "criteria", "obligations", "pod_ticks"}, name="previous issue revision")
    for key, source in (("previous_digest", previous["source"]), ("proposed_digest", row["source"])):
        validate_issue_binding(source)
        if (digest(source) != verdict[OPERATION]["source"][key]
                or any(source[field] != verdict[OPERATION]["source"][field] for field in ("repository", "number", "locator"))):
            raise refuse("stored_source_binding")
    if (not isinstance(previous["obligations"], list) or not previous["obligations"] or len(previous["obligations"]) > 96
            or not isinstance(previous["criteria"], list) or not previous["criteria"] or len(previous["criteria"]) > 64
            or any(not isinstance(ref, str) or not ref for ref in previous["criteria"])):
        raise refuse("stored_previous_map")
    for ob in previous["obligations"]:
        parsed = obligations._structure(ob)
        if parsed.get("definition") != obligations.definition_id(parsed):
            raise refuse("stored_previous_definition")
    if previous["pod_ticks"] is not None:
        from .pod_ticks import validate
        validate(previous["pod_ticks"])
    if not isinstance(row["criteria"], list) or not row["criteria"] or len(row["criteria"]) > 64:
        raise refuse("stored_criteria_mapping")
    ids, refs = set(), set()
    for criterion in row["criteria"]:
        exact(criterion, {"id", "ref", "check", "definition"}, {"id", "ref", "check"}, name="issue criterion mapping")
        if criterion["id"] in ids or criterion["ref"] in refs or not re.fullmatch(r"PoD#(?:0|[1-9][0-9]{0,8})", criterion["ref"]):
            raise refuse("stored_criteria_mapping")
        obligations._ident(criterion["id"], "criterion")
        bounded_text(criterion["check"], name="criterion check", limit=512)
        if "definition" in criterion and (not isinstance(criterion["definition"], str) or not _SHA.fullmatch(criterion["definition"])):
            raise refuse("stored_criteria_mapping")
        ids.add(criterion["id"]); refs.add(criterion["ref"])
    prior_ids = {ob["id"] for ob in previous["obligations"]}
    if (not isinstance(row["retired"], list) or len(set(row["retired"])) != len(row["retired"])
            or not set(row["retired"]) <= prior_ids or set(row["retired"]) & ids):
        raise refuse("stored_retirements")
    if "scope_facts" in row:
        _validate_scope_facts(row)


def _validate_scope_facts(row: dict) -> None:
    from .pod_ticks import validate_scope
    facts = exact(row["scope_facts"], {"previous", "current", "data_digests"},
                  {"previous", "current", "data_digests"}, name="issue scope facts")
    previous = {ob["id"]: ob for ob in row["previous"]["obligations"] if ob["kind"] == "criterion"
                and ob["provenance"] == "objective" and ob["state"] != "withdrawn"}
    if not isinstance(facts["previous"], list) or len(facts["previous"]) != len(previous):
        raise refuse("stored_scope_facts")
    prior_facts = {}
    for fact in facts["previous"]:
        exact(fact, {"id", "definition", "status", "reason", "scope"},
              {"id", "definition", "status", "reason", "scope"}, name="prior issue scope fact")
        if fact["id"] in prior_facts or fact["id"] not in previous or fact["definition"] != previous[fact["id"]]["definition"]:
            raise refuse("stored_scope_facts")
        if fact["status"] == "known":
            scope = validate_scope(fact["scope"])
            if scope["definition"] != fact["definition"] or fact["reason"] is not None:
                raise refuse("stored_scope_facts")
        elif fact["status"] == "unknown":
            if (fact["scope"] is not None or fact["id"] not in row["retired"]
                    or fact["reason"] not in {"prior_source_association_unavailable", "historical_body_unavailable",
                                              "prior_pod_scope_unavailable", "prior_source_scope_ambiguous"}):
                raise refuse("stored_scope_facts")
        else:
            raise refuse("stored_scope_facts")
        prior_facts[fact["id"]] = fact
    current = facts["current"]
    if not isinstance(current, dict) or set(current) != {item["id"] for item in row["criteria"]}:
        raise refuse("stored_scope_facts")
    for criterion in row["criteria"]:
        scope = validate_scope(current[criterion["id"]])
        if (scope["definition"] != criterion.get("definition") or scope["source_digest"] != digest(row["source"])
                or scope["amendments_digest"] != digest(row["source"]["amendments"])):
            raise refuse("stored_scope_facts")
        old = prior_facts.get(criterion["id"])
        if old is None:
            descriptor = f"{criterion['ref']} complete source block sha256:{scope['block_sha256']}"
            if (criterion["check"] != descriptor
                    and scope["block_sha256"] != hashlib.sha256(criterion["check"].encode("utf-8")).hexdigest()):
                raise refuse("stored_scope_facts")
        elif (old["status"] != "known" or any(scope[key] != old["scope"][key]
                                              for key in ("definition", "amendments_digest", "block_sha256"))):
            raise refuse("stored_scope_facts")
    digests = facts["data_digests"]
    if (not isinstance(digests, list) or len(digests) > 64
            or any(not isinstance(item, str) or not _SHA.fullmatch(item) for item in digests)
            or digests != sorted(set(digests))):
        raise refuse("stored_scope_facts")


def introduced(checkpoint: dict, ob: dict) -> bool:
    return any(row.get("kind") == OPERATION and row["seq"] == ob.get("introduced_seq")
               and any(item["id"] == ob["id"] and item["ref"] == ob.get("source", {}).get("ref")
                       for item in row["criteria"])
               for row in checkpoint.get("revision_decisions", []))

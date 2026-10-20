"""Bounded spelling repair inside the existing obligation map.

Caller body text is DATA. Only retained kernel bindings identify its source;
neither an association nor its scope fingerprint establishes passing evidence.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re

from .contracts import IDENTIFIER, bound, fields, input_record, required
from .errors import PodError
from .util import digest, exact

_TARGET = re.compile(r"PoD#([0-9]{1,9})\Z")
_LEGACY_REF = re.compile(r"PoD ([0-9]{1,9}):")
_LEGACY_LINE = re.compile(r"^- \[([ xX])\] \*\*PoD ([0-9]{1,9}):", re.M)
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _hold(detail: str, obligation: str | None = None) -> PodError:
    return PodError("mapping_repair_refused", "PoD mapping repair held: " + detail,
                    {"detail": detail, **({"obligation": obligation} if obligation else {}),
                     "next_action": "preserve the original map and receipts; restore genuinely available "
                                    "source facts through the existing checkpoint route; unavailable original "
                                    "or proof-time bindings cannot be supplied by caller assertion"})


@input_record("mapping repair")
def request(value: object) -> dict:
    row = exact(value, fields("mapping repair"), required("mapping repair"), name="mapping repair")
    body, targets = row["original_body"], row["targets"]
    try:
        valid_body = isinstance(body, str) and "\0" not in body and len(body.encode("utf-8")) <= bound(
            "mapping repair", "original_body", "max_bytes")
    except UnicodeEncodeError:
        valid_body = False
    if not valid_body:
        raise _hold("original_body_invalid")
    if (not isinstance(targets, dict) or not 1 <= len(targets) <= bound("mapping repair", "targets", "max_items")
            or any(not isinstance(key, str) or not re.fullmatch(IDENTIFIER, key)
                   or not isinstance(ref, str) or not _TARGET.fullmatch(ref) for key, ref in targets.items())):
        raise _hold("targets_invalid")
    if len({int(_TARGET.fullmatch(ref)[1]) for ref in targets.values()}) != len(targets):
        raise _hold("target_conflict")
    return row


def _identity(source: dict) -> dict:
    return {key: value for key, value in source.items() if key != "body_sha256"}


@input_record("pod mapping")
def validate(row: dict) -> None:
    from .github import validate_issue_binding
    association = exact(row.get("pod_mapping"), fields("pod mapping"), required("pod mapping"), name="pod mapping")
    source = row.get("source")
    ref = source.get("ref") if isinstance(source, dict) else None
    original = _LEGACY_REF.match(ref) if isinstance(ref, str) else None
    target = _TARGET.fullmatch(association["ref"]) if isinstance(association["ref"], str) else None
    if (row.get("kind") != "criterion" or row.get("provenance") not in ("objective", "user_direct")
            or original is None or target is None or original[1] != target[1]
            or type(association["accepted_seq"]) is not int or association["accepted_seq"] < 1
            or type(association["invalidated"]) is not bool
            or not isinstance(association["scope_sha256"], str) or not _SHA.fullmatch(association["scope_sha256"])):
        raise _hold("association_invalid", row.get("id"))
    for key in ("original_source", "accepted_source", "current_source"):
        validate_issue_binding(association[key])
    if any(_identity(association[key]) != _identity(association["original_source"])
           for key in ("accepted_source", "current_source")):
        raise _hold("association_invalid", row.get("id"))


def scope_hash(body: str) -> str:
    """Whole body, masking only checkbox bytes recognized by the existing parser."""
    from .github import pod_lines
    masked = list(body)
    for lines in pod_lines(body).values():
        for line in lines:
            masked[line["offset"]] = " "
    return _hash("".join(masked))


def read_current(source: dict, body: str, check: dict | None) -> bool:
    # Recovery may have checked the intended body but read back the unchanged
    # earlier body. Compare raw bytes to the actual checkpoint binding here;
    # Amendment currency comes from that real check, or an empty bound list
    # needing no amendment read. CLOSED/role holds retain their separate authority.
    amendments_current = (check.get("amendment_changed") is False if check is not None else not source["amendments"])
    return bool(amendments_current and _hash(body) == source["body_sha256"])


def current(row: dict, ctx: dict) -> bool:
    association = row.get("pod_mapping")
    if association is None:
        return True
    facts, source = ctx.get("pod_mapping_facts"), ctx.get("pod_mapping_source")
    return bool(not association["invalidated"] and source == association["current_source"]
                and facts is not None and facts["source"] == digest(source)
                and facts["body_sha256"] == source["body_sha256"]
                and facts.get("mapping_scope_sha256") == association["scope_sha256"]
                and facts.get("mapping_source_current") is True
                and ctx.get("pod_mapping_read", {}).get("status") == "read")


def effective_ref(row: dict, ctx: dict | None = None) -> str | None:
    """One live association predicate; withdrawn history remains in the map."""
    if row.get("kind") != "criterion" or row.get("state") == "withdrawn":
        return None
    if "pod_mapping" in row:
        return row["pod_mapping"]["ref"] if current(row, ctx or {}) else None
    return row.get("source", {}).get("ref")


def _tick_transition(association: dict, source: dict, ledger: dict) -> dict | None:
    # Only the existing protocol's exact intended rebind can be held pending.
    # An unknown response is not material divergence, nor confirmed currency.
    if ledger.get("last_rebind") != digest(source):
        return None
    return next((request for request in ledger.get("requests", {}).values()
                 if request.get("source") == association["current_source"]
                 and request.get("intended") == source["body_sha256"]
                 and (request.get("result") or {}).get("result") != "refused"), None)


def observe(state: dict, ledger: dict, source: dict, body: str, *, mapping_check: dict | None = None,
            apply: bool = True) -> bool:
    """Narrow _facts adapter: keep source/proof/standing/authorship hooks intact.

    An upcoming checkpoint rebind is judged by the map writer. Tick observations
    can retire an association here, including an earlier unbound drift read whose
    next observation restores the exact bytes. Retirement is sticky. Checkpoint
    refresh defers mutation until the writer validates exact prior metadata.
    """
    checkpoint = state.get("checkpoint") or {}
    if source != checkpoint.get("objective_source"):
        return False
    previous = ledger.get("facts")
    observed_hash, observed_scope = _hash(body), scope_hash(body)
    diverged = False
    for row in checkpoint.get("obligations", []):
        association = row.get("pod_mapping")
        if association is None or association["invalidated"]:
            continue
        transition = _tick_transition(association, source, ledger)
        prior_current = (previous is None or previous["source"] == digest(association["current_source"])
                         and previous["body_sha256"] == association["current_source"]["body_sha256"]
                         or transition is not None and previous["source"] == digest(source)
                         and previous.get("mapping_scope_sha256") == association["scope_sha256"])
        unchanged = (_identity(source) == _identity(association["accepted_source"])
                     and observed_hash == source["body_sha256"] and observed_scope == association["scope_sha256"])
        if not prior_current or not unchanged or (mapping_check or {}).get("amendment_changed") is True:
            diverged = True
            if apply:
                association["invalidated"] = True
        elif source != association["current_source"]:
            if transition is not None and (transition.get("result") or {}).get("result") == "accepted":
                if apply:
                    association["current_source"] = deepcopy(source)
            elif transition is None:
                diverged = True
                if apply:
                    association["invalidated"] = True
    return diverged


def _historical(state: dict, row: dict, source: dict) -> dict:
    """Choose original source facts before DATA; never hash-select a packet."""
    from .ledger import binding_valid
    checkpoint = state.get("checkpoint") or {}
    original_ref = row["source"]["ref"]
    original_criteria = {criterion["source"]["ref"] for criterion in checkpoint.get("obligations", [])
                         if criterion["kind"] == "criterion" and criterion["provenance"] == "objective"}
    candidates = []
    for admission in state.get("admissions", {}).values():
        native = admission.get("native_binding")
        seq = admission.get("admitted_seq")
        binding = admission.get("recovery", {}).get("checkpoint_binding", {})
        if (admission.get("state") not in ("bound", "closed") or not binding_valid(native)
                or admission.get("objective") != checkpoint.get("objective")
                or native["runId"] != admission.get("run_id") or native["taskId"] != admission.get("task_id")
                or native["runId"] not in {ref.get("runId") for ref in checkpoint.get("native_refs", [])}
                or type(seq) is not int or not row["introduced_seq"] <= seq <= checkpoint["seq"]
                or type(admission.get("map_revision")) is not int
                or not 1 <= admission["map_revision"] <= checkpoint["revision"]
                or not isinstance(binding, dict) or not isinstance(binding.get("criteria"), list)
                or any(not isinstance(criterion, str) for criterion in binding["criteria"])
                or original_ref not in binding["criteria"] or not original_criteria <= set(binding["criteria"])
                or not isinstance(binding.get("objective_source"), dict)):
            continue
        if row["id"] in admission.get("serves", []):
            definition = admission.get("binding", {}).get("definitions", {}).get(row["id"])
        else:
            definition = row.get("definition") if checkpoint["revision"] == admission["map_revision"] == 1 else None
        candidates.append((seq, binding["objective_source"], definition))
    # The actual preceding checkpoint is also a baseline when the same write
    # changes the labels. A late canonical observation alone supplies no old body.
    if (checkpoint.get("objective_source") is not None and checkpoint["objective_source"] != source
            and original_ref in checkpoint.get("criteria", [])):
        candidates.append((checkpoint["seq"], checkpoint["objective_source"], row.get("definition")))
    if not candidates:
        raise _hold("historical_binding_unavailable", row["id"])
    earliest = min(seq for seq, _, _ in candidates)
    baselines = {digest(bound): bound for seq, bound, _ in candidates if seq == earliest}
    if len(baselines) != 1:
        raise _hold("historical_binding_ambiguous", row["id"])
    baseline = next(iter(baselines.values()))
    from .github import validate_issue_binding
    for _, bound, definition in candidates:
        validate_issue_binding(bound)
        if _identity(bound) != _identity(source) or bound not in (baseline, source):
            raise _hold("historical_binding_conflict", row["id"])
        if definition is None:
            raise _hold("historical_binding_unavailable", row["id"])
        if definition != row.get("definition"):
            raise _hold("historical_definition_conflict", row["id"])
    return baseline


def prepare(state: dict, source: dict | None, value: object, body: str) -> dict:
    """Build kernel associations from a real current read and retained history."""
    from .github import pod_lines
    requested = request(value)
    if source is None or _hash(body) != source["body_sha256"]:
        raise _hold("current_binding_unavailable")
    original = requested["original_body"]
    # Hypothetical replacement has the same offsets. The existing canonical
    # parser, unchanged, decides which candidates are outside fences/column zero.
    hypothetical = _LEGACY_LINE.sub(lambda match: match[0].replace("**PoD ", "**PoD#", 1), original)
    recognized = {line["offset"] for lines in pod_lines(hypothetical).values() for line in lines}
    original_lines = [match for match in _LEGACY_LINE.finditer(original) if match.start() + 3 in recognized]
    amended = list(original)
    for match in original_lines:
        offset = match.start(2) - 1
        if len(body) == len(original) and body[offset] == "#":
            amended[offset] = "#"
    if "".join(amended) != body:
        raise _hold("body_not_label_only")
    rows = {row["id"]: row for row in (state.get("checkpoint") or {}).get("obligations", [])}
    original_hash, accepted_scope, current_lines = _hash(original), scope_hash(body), pod_lines(body)
    result = {}
    for key, target in requested["targets"].items():
        row = rows.get(key)
        old_ref = _LEGACY_REF.match((row or {}).get("source", {}).get("ref", ""))
        number_text = _TARGET.fullmatch(target)[1]
        number = int(number_text)
        if (row is None or row["kind"] != "criterion" or row["state"] == "withdrawn"
                or old_ref is None or old_ref[1] != number_text):
            raise _hold("target_not_original_criterion", key)
        if row.get("pod_mapping") is not None:
            raise _hold("already_repaired", key)
        matching = [match for match in original_lines if int(match[2]) == number]
        if (len(matching) != 1 or matching[0][2] != number_text
                or len(current_lines.get(number, [])) != 1
                or body[matching[0].start(2) - 1] != "#"):
            raise _hold("target_line_not_unique", key)
        baseline = _historical(state, row, source)
        if original_hash != baseline["body_sha256"]:
            raise _hold("original_body_mismatch", key)
        if _identity(baseline) != _identity(source):
            raise _hold("source_identity_changed", key)
        # No ordinary PASS in the preceding mechanism records its
        # proof-time issue source. Omission cannot turn that unknown into proof.
        if any(receipt.get("evidence", {}).get("status") == "PASS" for receipt in row.get("receipts", [])):
            raise _hold("proof_binding_unavailable", key)
        result[key] = {"ref": target, "original_source": deepcopy(baseline),
                       "accepted_source": deepcopy(source), "current_source": deepcopy(source),
                       "scope_sha256": accepted_scope, "accepted_seq": state["checkpoint"]["seq"] + 1,
                       "invalidated": False}
    return result


def superseded_originals(prior: dict | None, rows: dict, ctx: dict, *, seq: int | None = None) -> set[str]:
    """Only a genuine current R149 mapping can supersede withdrawn originals."""
    from .github import pod_ref_number
    from .issue_revision import Revision, validate_decision
    from .obligations import _authority, definition_id
    capability = ctx.get("issue_revision")
    same_write = isinstance(capability, Revision)
    if same_write:
        if (ctx.get("operation") != "checkpoint" or ctx.get("authority_kind") != "issue_revision"
                or ctx.get("revision_authority") != capability.authority
                or capability.original.get("checkpoint") != prior or seq != prior["seq"] + 1):
            return set()
        decision = {"seq": seq, "kind": "issue_revision", **capability.authority}
        checkpoint = {**prior, "seq": seq}
    else:
        decisions = [row for row in (prior or {}).get("revision_decisions", [])
                     if row.get("operation") == "issue_revision"]
        if not decisions:
            return set()
        decision, checkpoint = decisions[-1], prior
    validate_decision(decision, checkpoint)
    source = ctx.get("pod_mapping_source")
    approved = decision["criteria"]
    if (source is None or any(source[key] != decision["source"][key]
                              for key in ("repository", "number", "locator"))
            or ctx.get("criteria") != [item["ref"] for item in approved]):
        return set()
    selected = {item["id"] for item in approved}
    accepted = {row["id"]: row for row in (prior or {}).get("obligations", [])}
    authority = ctx.get("revision_authority")
    owner_write = (not same_write and ctx.get("operation") == "checkpoint"
                     and seq == prior["seq"] + 1 and ctx.get("authority_kind") in ("revision", "question")
                     and isinstance(authority, dict)
                     and (ctx["authority_kind"] == "question" or authority.get("channel") == "session")
                     and _authority(authority, kind=ctx["authority_kind"]) == authority)
    owner_revision = next((row for row in reversed((prior or {}).get("revision_decisions", []))
                           if row.get("kind") in ("revision", "question")
                           and (row["kind"] == "question" or row.get("channel", "session") == "session")
                           and decision["seq"] < row["seq"] <= prior["seq"]), None)
    if owner_revision is not None:
        from .util import owner_decision
        owner_decision(owner_revision, kind=owner_revision["kind"], stored=True)
    # R120 question authority retains its validated channel, as in ordinary intake.
    # Historical retirement follows the actual accepted definition and its
    # separate owner authority. It grants neither a new check nor passing proof.
    for item in approved:
        row = rows.get(item["id"])
        if (row is None or (row["kind"], row["provenance"]) != ("criterion", "objective")
                or row["source"] != {"ref": item["ref"]}
                or (same_write and (row["check"] != item["check"]
                                    or row.get("definition") != item.get("definition")))
                or row["definition"] != definition_id(row)):
            return set()
        if not same_write:
            earlier = accepted.get(item["id"])
            if (earlier is None or (earlier["kind"], earlier["provenance"], earlier["source"])
                    != ("criterion", "objective", {"ref": item["ref"]})
                    or earlier["definition"] != definition_id(earlier)
                    or earlier["check"] != item["check"] and owner_revision is None
                    or row["check"] != earlier["check"] and not owner_write):
                return set()
    mapped = {}
    for row in rows.values():
        number = pod_ref_number(effective_ref(row, ctx))
        if number is not None:
            mapped.setdefault(number, []).append(row["id"])
    expected = {pod_ref_number(item["ref"]): [item["id"]] for item in approved
                if rows[item["id"]]["state"] != "withdrawn"}
    if (mapped != expected or any(row["id"] not in selected for row in rows.values()
                                  if (row["kind"], row["provenance"]) == ("criterion", "objective")
                                  and row["state"] != "withdrawn")):
        return set()
    previous = {row["id"]: row for row in decision["previous"]["obligations"]}
    result = set()
    for key, row in rows.items():
        old = previous.get(key)
        if (key in selected or old is None or (row["kind"], row["provenance"], row["state"])
                != ("criterion", "objective", "withdrawn") or row["source"] != old.get("source")
                or row.get("definition") != old.get("definition") or row["definition"] != definition_id(row)):
            continue
        withdrawal = row.get("withdrawal", {})
        if (old["state"] == "withdrawn" and withdrawal == old.get("withdrawal")
                or key in decision["retired"] and withdrawal.get("by") == "issue_revision"
                and (same_write or withdrawal.get("decision") == decision["seq"])):
            result.add(key)
    return result


def write(prior: dict | None, value: dict, rows: dict, ctx: dict, seq: int) -> None:
    """Preserve caller-immutable metadata; apply only verified checkpoint facts."""
    from .obligations import definition_id
    prior_rows = {row["id"]: row for row in (prior or {}).get("obligations", [])}
    raw_rows = {row["id"]: row for row in value["obligations"]}
    repairs = ctx.get("pod_mapping_repairs", {})
    if "mapping_repair" in value and (ctx.get("operation") != "checkpoint" or not repairs):
        raise _hold("checkpoint_only")
    for key, row in rows.items():
        earlier = prior_rows.get(key, {})
        stored = earlier.get("pod_mapping")
        raw = raw_rows[key]
        if "pod_mapping" in raw and raw["pod_mapping"] != stored:
            raise _hold("association_kernel_field", key)
        row.pop("pod_mapping", None)
        if stored is not None:
            row["pod_mapping"] = deepcopy(stored)
            source, facts = ctx.get("pod_mapping_source"), ctx.get("pod_mapping_facts")
            pending_tick = (source is not None and _identity(source) == _identity(stored["accepted_source"])
                            and _tick_transition(stored, source, ctx.get("pod_mapping_ticks", {})) is not None
                            and facts is not None and facts["source"] == digest(source)
                            and facts.get("mapping_scope_sha256") == stored["scope_sha256"])
            observed_divergence = ctx.get("pod_mapping_read", {}).get("reason") in (
                "issue_body_changed", "issue_amendment_changed")
            if observed_divergence or definition_id(row) != earlier.get("definition") or not pending_tick and (
                    source is not None and source != stored["current_source"] or (
                    facts is not None and (facts["source"] != digest(stored["current_source"])
                                           or facts.get("mapping_scope_sha256") is not None
                                           and facts["mapping_scope_sha256"] != stored["scope_sha256"]))):
                row["pod_mapping"]["invalidated"] = True
        if key in repairs:
            if (raw.get("source") != earlier.get("source") or definition_id(row) != earlier.get("definition")
                    or row["state"] != earlier.get("state") or row.get("evidence", []) != earlier.get("evidence", [])):
                raise _hold("repair_changes_obligation", key)
            row["pod_mapping"] = deepcopy(repairs[key])
        if "pod_mapping" in row:
            validate(row)
            if not row["introduced_seq"] <= row["pod_mapping"]["accepted_seq"] <= seq:
                raise _hold("association_invalid", key)
            if row["state"] == "satisfied":
                if not current(row, ctx):
                    raise _hold("mapping_invalidated" if row["pod_mapping"]["invalidated"] else "mapping_binding_unavailable", key)
                old = {receipt["evidence"].get("reference"): receipt for receipt in earlier.get("receipts", [])}
                if any(evidence.get("reference") in old and (
                        old[evidence["reference"]].get("accepted_seq", 0) <= row["pod_mapping"]["accepted_seq"])
                       for evidence in row.get("evidence", [])):
                    raise _hold("proof_binding_unavailable", key)
    if any("pod_mapping" in row for row in rows.values()):
        superseded = superseded_originals(prior, rows, ctx, seq=seq)
        original = {row["source"]["ref"] for row in rows.values()
                    if (row["provenance"] == "objective" or "pod_mapping" in row)
                    and row["id"] not in superseded}
        if not original <= set(ctx.get("criteria", [])):
            raise _hold("original_criteria_required")
    live = {}
    for row in rows.values():
        from .github import pod_ref_number
        number = pod_ref_number(effective_ref(row, ctx))
        if number is not None:
            live.setdefault(number, []).append(row)
    if any(len(group) > 1 and any("pod_mapping" in row for row in group) for group in live.values()):
        raise _hold("target_conflict")

"""A successor objective's claim on the objective of a lost coordinator (R110).

The claim ends the predecessor's write authority and gives it to no one: its owner, admissions,
Runs, map and evidence stay exactly as recorded, and its old coordinator is refused by name. It
never steals: Orca must report the predecessor's coordinator gone and its work settled. Orca has
no worktree claim or conditional send, so the claim binds none (`orca_claim: unavailable`).

It carries the predecessor's unsettled Governor rows that Pod can read back into the successor's own
journal (R118); they settle only by that readback. The claim is a cooperative Pod record, not an
operating-system fence: a process of the old coordinator that is still running is not stopped by it.
"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path

from .errors import PodError
from .util import bounded_text, digest, exact, owner_decision, channel_fields, decision_channel, CHANNEL_FIELDS

DECISION = {"provenance", "instruction", "predecessor", "predecessor_owner", "successor",
            "successor_owner", "runs", "ambiguity"}
FACTS = DECISION - {"provenance", "instruction"}
# The carried Governor rows a decision names (R118); a decision without them names none.
CARRIED = "carried"
CONTINUED = {"objective", "record", "owner", "runs", "at", "instruction", "unfinished", "unreported",
             "ambiguity", "orca_claim"}
CONTINUES = {"objective", "record", "at"}
# A claim reserves its successor before it links the predecessor: the successor's link names the predecessor with
# this state, and the digest of the decision that made the claim, until the claim completes; the successor accepts no
# other write meanwhile.
CLAIMING = "claiming"
MAX_CHAIN = 8
MAX_LISTED = 64
ACTION = "Read the successor objective with pod status --objective {}; this record accepts no writes."


def _texts(value: object, *, limit: int) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_LISTED
            and all(isinstance(item, str) and 0 < len(item) <= limit for item in value))


def validate_link(name: str, value: object) -> None:
    """The stored shape of either side of a claim; anything else makes the record unsupported."""
    fields = CONTINUED if name == "continued_by" else CONTINUES
    if name == "continued_by" and isinstance(value, dict) and "decision_binding" in value and "provenance" in value:
        fields = fields | {"provenance"}
    if name == "continues" and isinstance(value, dict) and value.get("state") == CLAIMING:
        fields = CONTINUES | {"state", "decision"}
    if name == "continues" and isinstance(value, dict) and "approval" in value:
        fields = fields | {"approval"}
    if (not isinstance(value, dict) or set(value) - CHANNEL_FIELDS != fields
            or any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024
                   for key in fields - {"runs", "unfinished", "unreported", "ambiguity", "approval"})
            or name == "continued_by" and (value["orca_claim"] != "unavailable"
                                           or not all(_texts(value[key], limit=256)
                                                      for key in ("runs", "unfinished", "unreported"))
                                           or not _texts(value["ambiguity"], limit=512))):
        raise PodError("state_unsupported", f"Objective {name.replace('_', ' ')} record is malformed")
    if name == "continued_by":
        owner_decision(value, kind="claim", stored=True)
    if name == "continues" and isinstance(value, dict) and "approval" in value:
        from .relay import validate_decision_record
        validate_decision_record(value["approval"], kind="claim")


def reserved_for(state: dict | None) -> str | None:
    """The predecessor an incomplete claim of this successor names, if one is in progress."""
    back = state.get("continues") if isinstance(state, dict) else None
    return back["objective"] if isinstance(back, dict) and back.get("state") == CLAIMING else None


def refuse_continued(state: dict | None, *, claim: str | None = None, reserved: bool = True) -> None:
    """The one refusal a continued objective, or one under historical retirement (R112), gives every write.

    A successor that an incomplete claim reserves refuses every write but that claim's own (`claim` names its
    predecessor) until the claim completes (R118). `reserved=False` skips that check where no work is done: the
    record's write backstop, so the claim itself can write, and an owner handoff or runtime rebind, which move
    authority so that a new coordinator can finish the claim.
    """
    link = state.get("continued_by") if isinstance(state, dict) else None
    if isinstance(link, dict):
        raise PodError("objective_continued",
                       f"This objective was continued by successor objective {link['objective']}; "
                       "its record is kept and accepts no writes",
                       {"successor": link["objective"], "next_action": ACTION.format(link["objective"])})
    pending = reserved_for(state) if reserved else None
    if pending is not None and pending != claim:
        raise _claim_incomplete(pending)
    from .retirement import refuse_retired
    refuse_retired(state)


def _claim_incomplete(predecessor: str) -> PodError:
    return PodError("claim_incomplete",
                    f"This objective's claim on predecessor objective {predecessor} is incomplete; it accepts no "
                    "other write until that claim completes",
                    {"predecessor": predecessor,
                     "next_action": "rerun the same internal succession claim to complete it, after an owner handoff "
                                    "or runtime rebind if its coordinator is gone"})


def incoming_claim(project: Path, objective: str, state: dict | None, *, full: bool = False) -> dict | None:
    """The incomplete claim that names this objective as its successor, from any writer, or None (R110, R118).

    The one predicate for every shape an interrupted claim leaves. This version reserves the successor first, so its
    own `continues` names the predecessor with `state: claiming`. Pod 0.10.0 to 0.13.2 linked the predecessor and
    then wrote the successor's `continues`, and the earlier PR-B candidate copied rows in between; a claim of theirs
    interrupted after the link shows only as a predecessor's `continued_by` naming this objective while its own
    `continues` is missing, which retirement's claimant scan of the repository's records finds.

    Rows or units the earlier candidate copied before stopping name their predecessor in the objective's own
    journal. Earlier writers never checked for an incoming claim, so two of their claims can name one successor:
    every claimant counts, and a predecessor other than the one this objective's own records name makes the claims
    ambiguous, even after one of them completed. No supported route merges two claims, so an ambiguous successor
    stays held rather than closing over the other predecessor's effects.

    The scan therefore always runs for an objective whose own records name a predecessor. Otherwise it runs only
    where they cannot prove there is no claim: work it already started proves it, since every writer refuses to
    begin a claim of a started successor; an objective with no checkpoint yet is named by no claim, since every
    writer claims only a recorded successor. An unreadable journal of its own proves nothing. `full` scans anyway,
    for closure and for continuing it as a predecessor, which also cover a claim an earlier Pod left before the
    objective started. Returns `{"predecessor": name}` for one readable incomplete claim, which an unreadable record
    never hides; `{"predecessor": None, "ambiguous": names}` for more than one predecessor; or
    `{"predecessor": None, "unreadable": paths}` when no readable incomplete claim is found and a record that could
    hold one is unreadable, which a completed claim of its own answers only where `full` asks for proof.
    """
    if not isinstance(state, dict) or state.get("checkpoint") is None:
        return None
    back = state.get("continues")
    named = back["objective"] if isinstance(back, dict) else _copied_from(project, objective)
    if named is None and not full and _started(project, objective, state, unreadable=None):
        return None
    from .retirement import _claimants
    claimants, unreadable = _claimants(project, objective)
    found = sorted({*claimants, *([named] if named is not None else [])})
    if len(found) > 1:
        return {"predecessor": None, "ambiguous": found[:MAX_LISTED]}
    if isinstance(back, dict) and back.get("state") != CLAIMING:
        return {"predecessor": None, "unreadable": unreadable[:MAX_LISTED]} if full and unreadable else None
    if found:
        return {"predecessor": found[0]}
    return {"predecessor": None, "unreadable": unreadable[:MAX_LISTED]} if unreadable else None


def _copied_from(project: Path, objective: str) -> str | None:
    """The predecessor a claim copied rows or units from into this objective's journal, if any."""
    from .governor import _read_journal, _record_path
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        return None
    for item in [*journal["actions"], *journal["units"].values()]:
        name = (item.get("carried_from") or {}).get("objective")
        if isinstance(name, str):
            return name
    return None


def refuse_incomplete(project: Path, objective: str, state: dict | None, *, claim: str | None = None,
                      full: bool = False) -> None:
    """Refuse the successor of an incomplete claim every write but that claim's own (R118).

    While a record that could hold such a claim is unreadable, an objective whose own records cannot prove it has
    none refuses every write: work would start it, and a started successor can no longer complete the claim.
    Closure and continuing it as a predecessor (`full`) ask that proof even of a started objective. Claims of more
    than one predecessor refuse every write, their replays included. An objective whose closure is recorded and
    valid is answered from that closure before any of this (R117): a repeated close refuses `objective_closed`
    with its finished finalization, whatever an incoming claim would say.
    """
    if _closure_recorded(project, objective, state):
        return
    found = incoming_claim(project, objective, state, full=full)
    if found is None:
        return
    if "ambiguous" in found:
        raise _claim_ambiguous(found["ambiguous"])
    if claim is not None and found["predecessor"] == claim:
        return
    if found["predecessor"] is None:
        named = found["unreadable"]
        raise PodError("claim_unverified", "A Pod record that could name this objective as the successor of an "
                       "incomplete claim is unreadable: " + ", ".join(named[:3])
                       + (f"; and {len(named) - 3} more" if len(named) > 3 else ""),
                       {"unreadable": named,
                        "next_action": "repair or remove the unreadable record, then retry"})
    raise _claim_incomplete(found["predecessor"])


def _closure_recorded(project: Path, objective: str, state: dict | None) -> bool:
    """Whether this objective's closure is recorded and still validates, so it answers its own replay (R117)."""
    from .ledger import closed_snapshot_context, map_of
    from .obligations import validate_closed_snapshot
    closed = map_of(state)
    if closed is None or not closed.get("closure"):
        return False
    try:
        return validate_closed_snapshot(closed, closed_snapshot_context(project, objective, state)) == ()
    except (PodError, KeyError, TypeError, ValueError, OSError):
        return False


AMBIGUOUS_ACTION = ("no supported route merges claims of two predecessors: the owner decides outside Pod how each "
                    "predecessor's unsettled effects settle; historical retirement does not apply to a claimed "
                    "successor or a continued predecessor")


def _claim_ambiguous(predecessors: list[str]) -> PodError:
    return PodError("claim_ambiguous",
                    "More than one predecessor objective names this objective as its successor ("
                    + ", ".join(predecessors) + "); it accepts no write, and nothing was written",
                    {"predecessors": predecessors, "next_action": AMBIGUOUS_ACTION})


def _read(project: Path, objective: str) -> dict | None:
    from .ledger import read
    try:
        return read(project, objective)
    except PodError:
        return None


def chain_state(project: Path, state: dict) -> dict:
    """Follow successors, at most eight, to where the work now stands; never writes."""
    seen = set()
    current, name = state, None
    for _ in range(MAX_CHAIN):
        link = current.get("continued_by")
        if not isinstance(link, dict):
            break
        name = link["objective"]
        if name in seen:
            return {"state": "unknown", "objective": name, "reason": "successor chain loops"}
        seen.add(name)
        successor = _read(project, name)
        back = (successor or {}).get("continues")
        if successor is None:
            return {"state": "unknown", "objective": name, "reason": "successor record unreadable"}
        if (not isinstance(back, dict) or back.get("objective") != (current.get("checkpoint") or {}).get("objective")
                or back.get("state") == CLAIMING):
            return {"state": "claim_incomplete", "objective": name,
                    "reason": "the successor does not yet record the claim; rerun the same claim"}
        current = successor
    else:
        if isinstance(current.get("continued_by"), dict):
            return {"state": "unknown", "objective": name, "reason": "successor chain exceeds eight objectives"}
    closed = bool(((current.get("checkpoint") or {}).get("closure")))
    if not closed and name is not None:
        # A successor closed by evidence is a closed chain end (R146).
        from .evidence_closure import present
        from .ledger import objective_root
        closed = present(objective_root(project, name))
    return {"state": "successor_closed" if closed else "successor_open", "objective": name}


def links(project: Path, raw: dict) -> dict:
    """Public succession fields of one record, for status and the objective list."""
    out = {}
    link = raw.get("continued_by")
    if isinstance(link, dict) and isinstance(link.get("objective"), str):
        out["continued_by"] = link["objective"]
        out["succession_state"] = chain_state(project, raw)["state"]
    back = raw.get("continues")
    if isinstance(back, dict) and isinstance(back.get("objective"), str):
        out["continues"] = back["objective"]
    return out


def _carried_settlement(project: Path, state: dict, link: dict) -> dict | None:
    """How the rows this predecessor's claim carried stand now (R118).

    Each row is read from the successor's journal and, while it is still unsettled there and that successor was
    itself continued, from the later successor that carried it again, at most eight objectives along. A settled
    result is current wherever it was recorded; a row the chain cannot follow (unreadable, looping, too long, or a
    later claim that has not yet copied it) reads unavailable rather than a frozen earlier outcome. None when the
    claim carried nothing.
    """
    from .governor import _read_journal, _record_path
    name = (state.get("checkpoint") or {}).get("objective")
    try:
        journal = _read_journal(_record_path(project, link["objective"]))
    except PodError as exc:
        return {"rows": None, "reason": f"the successor's Governor journal is unreadable ({exc.code})"}
    rows = [row for row in journal["actions"] if (row.get("carried_from") or {}).get("objective") == name]
    if not rows:
        return None
    current = [_current_carried(project, link["objective"], row) for row in rows]
    # An unavailable row (outcome None) is not known settled, so it counts as unsettled.
    return {"rows": current[:MAX_LISTED],
            "unsettled": sum(row["outcome"] in ("pending", "UNKNOWN", None) for row in current)}


def _current_carried(project: Path, holder: str, row: dict) -> dict:
    """One carried row's current outcome, following it through later claims; `held_by` names where it was read."""
    from .governor import _read_journal, _record_path
    entry = {"record": row["record_id"], "kind": row["action"]["kind"]}
    seen = {holder}
    for _ in range(MAX_CHAIN):
        if row["outcome"] not in ("pending", "UNKNOWN"):
            break
        record = _read(project, holder)
        if record is None:
            return {**entry, "outcome": None, "reason": f"objective {holder} is unreadable"}
        link = record.get("continued_by")
        if not isinstance(link, dict):
            break
        later = link["objective"]
        if later in seen:
            return {**entry, "outcome": None, "reason": "successor chain loops"}
        seen.add(later)
        try:
            journal = _read_journal(_record_path(project, later))
        except PodError as exc:
            return {**entry, "outcome": None,
                    "reason": f"objective {later}'s Governor journal is unreadable ({exc.code})"}
        copy = next((other for other in journal["actions"] if other["record_id"] == row["record_id"]
                     and (other.get("carried_from") or {}).get("objective") == holder), None)
        if copy is None:
            return {**entry, "outcome": None, "reason": f"objective {later} has not recorded this row; its claim "
                                                        "may be incomplete"}
        holder, row = later, copy
    else:
        if row["outcome"] in ("pending", "UNKNOWN") and isinstance(((_read(project, holder) or {})
                                                                    .get("continued_by")), dict):
            return {**entry, "outcome": None, "reason": "successor chain exceeds eight objectives"}
    return {**entry, "outcome": row["outcome"], "held_by": holder}


def status_block(project: Path, state: dict) -> dict | None:
    link = state.get("continued_by")
    back = state.get("continues")
    if not isinstance(link, dict) and not isinstance(back, dict):
        return None
    block = {}
    if isinstance(link, dict):
        chain = chain_state(project, state)
        block.update(continued_by=link["objective"], at=link["at"], state=chain["state"],
                     chain_end=chain.get("objective"), unfinished_at_claim=list(link["unfinished"]),
                     unreported=list(link["unreported"]), ambiguity=list(link["ambiguity"]),
                     orca_claim=link["orca_claim"])
        block.update(channel_fields(decision_channel(link)))
        if chain.get("reason"):
            block["reason"] = chain["reason"]
        carried = _carried_settlement(project, state, link)
        if carried is not None:
            block["carried"] = carried
    if isinstance(back, dict):
        block["continues"] = back["objective"]
        block["continues_at"] = back["at"]
    return block


def _started(project: Path, objective: str, state: dict, *, predecessor: str | None = None,
             carried: frozenset = frozenset(),
             unreadable: str | None = "the successor's Governor journal is unreadable") -> str | None:
    """Why the successor has already begun work, if it has.

    Rows and units this claim of `predecessor` already copied (record ids in `carried`) are the claim's own
    partial write, not the successor's work. An unreadable journal answers `unreadable`: started for a claim, which
    must not copy into it, and unproven (None) for the incoming-claim scan.
    """
    from .governor import _read_journal, _record_path
    from .obligations import TERMINAL
    checkpoint = state.get("checkpoint") or {}
    if state.get("admissions"):
        return "the successor already has admissions"
    if checkpoint.get("closure") or checkpoint.get("delivery"):
        return "the successor is closed or delivered"
    if any(isinstance(row, dict) and row.get("state") in TERMINAL for row in checkpoint.get("obligations") or []):
        return "the successor already settled an obligation"
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        return unreadable
    def mine(item: dict) -> bool:
        return predecessor is not None and (item.get("carried_from") or {}).get("objective") == predecessor
    if (any(not (mine(row) and row["record_id"] in carried) for row in journal["actions"])
            or any(not mine(unit) for unit in journal["units"].values())):
        return "the successor already has Governor work"
    return None


def _predecessor_checks(project: Path, predecessor: str, prior: dict, facts: dict, fail, *, port,
                        previous: dict | None = None) -> tuple[dict | None, list[str], list[str]]:
    """The claim's checks of the predecessor's own record and native work, in the claim's order; read-only.

    Returns the first refusal `fail` builds, else None with the unfinished obligations and unreported
    settled admissions. `previous` is a read of the predecessor's coordinator terminal this call already made.
    """
    from .handoff import unfinished
    from .ledger import _native_assignment_claim_free as _native_assignment_settled, _run_references, map_of
    from .orca import run_show, terminal_identity, worker_rows
    from .obligations import TERMINAL

    def refused(*args) -> tuple[dict, list, list]:
        return fail(*args), [], []

    map_state = map_of(prior)
    if map_state is not None and map_state.get("closure"):
        return refused("predecessor_closed", "The predecessor objective is closed; a closed objective needs no successor.")
    from .evidence_closure import present
    from .ledger import objective_root
    if present(objective_root(project, predecessor)):
        return refused("predecessor_closed", "The predecessor objective was closed by evidence, coordinator gone; a "
                       "closed objective needs no successor.")
    if prior.get("checkpoint") is None or not prior.get("owner"):
        return refused("predecessor_unclaimable", "The predecessor has no recorded coordinator to end.")
    facts["predecessor_owner"] = prior["owner"]
    if unfinished(prior) is not None:
        return refused("predecessor_handoff_pending", "The predecessor has an unfinished owner handoff.")
    from .window import unfinished as window_unfinished
    if (window_unfinished(prior) or {}).get("state") == "pending":
        # A claimant may hold a pending window continuation; a claim never ends it (pod#110).
        return refused("predecessor_continuation_pending", "The predecessor has a pending window continuation; "
                       "finish it with pod continue first.")
    incomplete = _incomplete_claim(project, predecessor, prior)
    if incomplete is not None:
        return refused(*incomplete)
    try:
        runs = sorted(_run_references(prior))
    except PodError:
        return refused("predecessor_unverified", "The predecessor's Run references are unreadable.")
    facts["runs"] = runs
    if not runs:
        return refused("predecessor_unverified", "The predecessor records no Run to prove settled.")
    previous = terminal_identity(prior["owner"]) if previous is None else previous
    if previous["state"] == "live":
        return refused("predecessor_live", "Orca reports the predecessor's coordinator live; the claim never steals.",
                       "Continue in the predecessor's own terminal, or close it first.")
    if previous["state"] != "lost" and not previous.get("exited"):
        return refused("predecessor_unverified", "Orca cannot prove the predecessor's coordinator gone ("
                       + (previous["code"] or previous["state"]) + ").")
    dispatches = {}
    for run in runs:
        try:
            shown = run_show(run)
        except PodError as exc:
            return refused("predecessor_unverified", f"A predecessor Run could not be read ({exc.code}).")
        holder = shown["coordinator_handle"]
        if holder is not None and holder != prior["owner"]:
            other = terminal_identity(holder)
            if other["state"] == "live":
                return refused("predecessor_run_held", "Another live terminal coordinates a predecessor Run.",
                               "Continue on the successor's own Run; same-Run continuation is an owner handoff.")
            if other["state"] != "lost" and not other.get("exited"):
                return refused("predecessor_unverified", "A predecessor Run's coordinator is unverified.")
        try:
            fleet = worker_rows(run)
        except PodError as exc:
            return refused("predecessor_unverified", f"A predecessor Run's workers could not be read ({exc.code}).")
        if fleet.get("complete") is not True:
            return refused("predecessor_unverified", "A predecessor Run's worker list is incomplete.")
        for row in fleet["workers"]:
            dispatch = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(dispatch, str) or not dispatch:
                return refused("predecessor_unverified", "A predecessor worker has no Dispatch identity.")
            dispatches[dispatch] = run
    carried, refusal = _carried(project, predecessor, prior)
    if refusal is not None:
        return refused(*refusal)
    facts["carried"] = [row["record_id"] for row in carried]
    unreported = []
    bound = {}
    for key in sorted(prior["admissions"]):
        row = prior["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        if row["state"] == "unresolved":
            return refused("predecessor_work_unsettled", f"Predecessor admission {key[:12]} is unresolved.")
        if row["state"] == "reserved":
            try:
                found = port.find_worker(run=row["run_id"], task=row["task_id"])
            except PodError as exc:
                return refused("predecessor_unverified", f"A reserved predecessor start could not be read ({exc.code}).")
            if found:
                return refused("predecessor_work_unsettled", f"Predecessor admission {key[:12]} has an unbound Dispatch.")
            facts["ambiguity"].append(f"reserved admission {key}: no Dispatch found")
            continue
        bound[row["native_binding"]["dispatchId"]] = (key, row)
    for dispatch in sorted(set(dispatches) | set(bound)):
        try:
            shown = port.show_worker(dispatch)
        except PodError as exc:
            return refused("predecessor_unverified", f"A predecessor worker could not be read ({exc.code}).")
        result = shown.get("result") if isinstance(shown, dict) else None
        if dispatch in bound:
            key, row = bound[dispatch]
            binding = row["native_binding"]
            parts = [result.get(name) if isinstance(result, dict) else None for name in ("dispatch", "projection", "worker")]
            if not all(isinstance(part, dict) for part in parts) or (
                    parts[0].get("id"), parts[0].get("runId"), parts[0].get("taskId"), parts[1].get("id"),
                    parts[2].get("worktreeId")) != (binding["dispatchId"], binding["runId"], binding["taskId"],
                                                    binding["workerId"], binding["worktreeId"]):
                return refused("predecessor_unverified", f"Predecessor admission {key[:12]} has a differing native identity.")
            if (row.get("report") or {}).get("status") != "validated_observation":
                unreported.append(key)
        if not _native_assignment_settled(shown):
            return refused("predecessor_work_live", "A predecessor worker is live or unsettled; the claim waits for it.",
                           "Settle the predecessor's workers through Orca, then rerun the claim.")
    unfinished_rows = [ob["id"] for ob in (map_state or {}).get("obligations", []) if ob["state"] not in TERMINAL]
    return None, unfinished_rows, unreported


def _incomplete_claim(project: Path, objective: str, state: dict) -> tuple | None:
    """The refusal for continuing an objective whose own claim of its predecessor is incomplete, or None (R118).

    It is never a predecessor: claiming it would freeze the only record that can complete that claim, and an empty
    successor could close while the original effects stay unsettled.
    """
    found = incoming_claim(project, objective, state, full=True)
    if found is None:
        return None
    if "ambiguous" in found:
        return ("claim_ambiguous", "More than one predecessor objective names the predecessor as its successor ("
                + ", ".join(found["ambiguous"]) + "); it is not continued.", AMBIGUOUS_ACTION[0].upper()
                + AMBIGUOUS_ACTION[1:] + ".")
    if found["predecessor"] is None:
        return ("predecessor_unverified", "A Pod record that could name the predecessor as a claimed successor is "
                "unreadable: " + ", ".join(found["unreadable"][:3]) + ".",
                "Repair or remove the unreadable record, then rerun the claim.")
    own = found["predecessor"]
    return ("predecessor_claim_incomplete",
            f"The predecessor's own claim on objective {own} is incomplete; it is not continued until that claim "
            "completes.",
            f"Complete that claim first: rerun internal succession for objective {objective} with predecessor {own} "
            "and the decision that made it, from its recorded coordinator, after an owner handoff or runtime rebind "
            "if that coordinator is gone.")


def _scope(decision: dict) -> str:
    """The digest of a claim decision's exact facts, kept by the reservation it makes."""
    return digest({**{key: decision[key] for key in FACTS}, CARRIED: decision.get(CARRIED, [])})


def _carried(project: Path, predecessor: str, prior: dict) -> tuple[list[dict], tuple | None]:
    """The predecessor's unsettled Governor rows a claim carries, or the refusal of one it cannot (R118)."""
    from .governor import _read_journal, _record_path, carried_rows, carry_refusal
    from .ledger import map_of
    try:
        journal = _read_journal(_record_path(project, predecessor))
    except PodError as exc:
        return [], ("predecessor_unverified", f"The predecessor's Governor journal is unreadable ({exc.code}).")
    rows = carried_rows(journal)
    governance = (map_of(prior) or {}).get("governance")
    for row in rows:
        reason = carry_refusal(row, journal, governance)
        if reason is not None:
            return [], ("predecessor_effect_pending",
                        f"The predecessor's {row['outcome']} {row['action']['kind']} {row['record_id'][:12]} cannot "
                        f"be carried: {reason}.",
                        "Settle it in the predecessor first; a successor carries only effects Pod can read back.")
    return rows, None


def _predecessor_facts(project: Path, objective: str, predecessor: str, state: dict,
                       prior: dict | None, owner: str, *, port) -> dict:
    """Fresh facts, read-only; an unproven fact holds the claim."""
    from .ledger import record_name
    from .github import repository_context
    facts = {"predecessor": predecessor, "predecessor_owner": None, "successor": objective,
             "successor_owner": owner, "runs": [], "ambiguity": [], CARRIED: []}
    verdict = {"status": "claim_unavailable", "condition": None, "facts": facts}

    def fail(condition: str, message: str, action: str | None = None) -> dict:
        verdict.update(condition=condition, message=message,
                       next_action=action or "Resolve the named condition; nothing was written.")
        return verdict

    if state.get("continued_by"):
        return fail("objective_continued", "The successor objective is itself continued.")
    if predecessor == objective:
        return fail("predecessor_unknown", "An objective cannot continue itself.")
    if prior is None:
        return fail("predecessor_unknown", "No readable objective of that name is recorded in this repository.")
    record = record_name(project, repository_context(project), predecessor)
    link = prior.get("continued_by")
    if isinstance(link, dict):
        if link["objective"] == objective:
            # The facts of the recorded claim, so its own decision can complete an interrupted link.
            facts.update(predecessor_owner=prior.get("owner"), successor_owner=link["owner"],
                         runs=list(link["runs"]), ambiguity=list(link["ambiguity"]),
                         carried=[row["record_id"] for row in _carried(project, predecessor, prior)[0]])
            back = state.get("continues")
            verdict.update(status="claimed", condition="claimed_by_this_successor", record=record,
                           complete=(isinstance(back, dict) and back.get("objective") == predecessor
                                     and back.get("state") != CLAIMING),
                           message="This successor already holds the claim.")
            return verdict
        return fail("predecessor_claimed", f"Successor objective {link['objective']} already continues it.",
                    ACTION.format(link["objective"]))
    if isinstance(prior.get("historical_retirement"), dict):
        return fail("predecessor_retired", "The owner's historical retirement covers the predecessor; it is not "
                    "continued.")
    back = state.get("continues")
    if isinstance(back, dict) and reserved_for(state) != predecessor:
        return fail("successor_started", f"This successor already continues objective {back['objective']}.")
    started = _started(project, objective, state)
    if started:
        return fail("successor_started", "Claim before the successor starts work: " + started + ".")
    failed, unfinished_rows, unreported = _predecessor_checks(project, predecessor, prior, facts, fail, port=port)
    if failed is not None:
        return failed
    question = (f"May Pod record objective {objective} as the successor of objective {predecessor}? "
                "Orca reports the predecessor's coordinator gone and its work settled; the predecessor's record is "
                "kept, and it will accept no further writes.")
    if facts[CARRIED]:
        question += (f" Its {len(facts[CARRIED])} unsettled Governor effect(s) move to the successor, which settles "
                     "them only by reading them back and never repeats them.")
    verdict.update(status="claim_available", condition="claimable", record=record, question=question,
                   unfinished=unfinished_rows[:MAX_LISTED], unreported=unreported[:MAX_LISTED],
                   message="The predecessor's coordinator is gone and its work settled.",
                   next_action="Confirm with the owner's direct instruction to continue the predecessor: "
                               + question + " Then rerun internal succession with that decision.")
    return verdict


def claim_view(project: Path, objective: str, state: dict, *, port, previous: dict | None = None) -> dict:
    """What a successor's claim would check of this objective now, with no successor: never writes (R114).

    The predecessor's own checks in the claim's order, for a reader that is not the recorded coordinator;
    the successor's checks (recorded, open, not started) wait for an actual claim.
    """
    facts = {"predecessor": objective, "predecessor_owner": None, "runs": [], "ambiguity": [], CARRIED: []}
    view = {"status": "claim_unavailable", "condition": None, "facts": facts}

    def fail(condition: str, message: str, action: str | None = None) -> dict:
        view.update(condition=condition, message=message,
                    next_action=action or "Resolve the named condition before a successor claims it.")
        return view

    link = state.get("continued_by")
    if isinstance(link, dict):
        return fail("predecessor_claimed", f"Successor objective {link['objective']} already continues it.",
                    ACTION.format(link["objective"]))
    if isinstance(state.get("historical_retirement"), dict):
        return fail("predecessor_retired", "The owner's historical retirement covers this objective; it is not "
                    "continued.")
    failed, unfinished_rows, unreported = _predecessor_checks(project, objective, state, facts, fail, port=port,
                                                              previous=previous)
    if failed is not None:
        return failed
    view.update(status="claim_available", condition="claimable", unfinished=unfinished_rows[:MAX_LISTED],
                unreported=unreported[:MAX_LISTED],
                message="The coordinator is gone and its work settled.",
                next_action="A successor objective, recorded and not yet started, may claim it through internal "
                            "succession with the owner's direct instruction.")
    return view


def succession(project: Path, objective: str, *, owner: str, predecessor: str,
               decision: object | None = None, port=None) -> dict:
    """Detect without a decision; claim with an exact one. Locks are taken in path order.

    Detection of a delivered predecessor names the evidence closure as its next action; that read-only proof runs
    after the locks are released, as it reads Orca and GitHub (R147)."""
    result = _succession(project, objective, owner=owner, predecessor=predecessor, decision=decision, port=port)
    return _delivered_route(project, predecessor, result, port=port) if decision is None else result


def _succession(project: Path, objective: str, *, owner: str, predecessor: str,
                decision: object | None = None, port=None) -> dict:
    from .ledger import _lock, _path, _read, _write, _write_succession, require_authority
    from .operations import OrcaPort
    bounded_text(predecessor, name="predecessor", limit=256)
    from .relay import Decision, decision_input, _raw_decision
    generic = decision_input(decision)
    requested_decision = dict(decision) if isinstance(decision, dict) else None
    if decision is not None:
        decision = exact(decision, DECISION | {CARRIED} | CHANNEL_FIELDS, DECISION, name="succession_decision")
        bounded_text(decision["instruction"], name="instruction", limit=1024)
        if decision["provenance"] != "user_direct" and not generic:
            raise PodError("succession_decision_mismatch", "Only the owner's direct instruction permits a claim")
        if generic:
            _raw_decision(decision)
        else:
            decision = owner_decision(decision, kind="claim")
    if owner != os.environ.get("ORCA_TERMINAL_HANDLE"):
        raise PodError("native_authority_unverified", "A claim needs the successor's own live coordinator")
    if predecessor == objective:
        raise PodError("predecessor_unknown", "An objective cannot continue itself",
                       {"next_action": "name the interrupted objective as the predecessor"})
    port = port or OrcaPort(project)
    own, other = _path(project, objective), _path(project, predecessor)
    # Nothing is created for a name that has no record: the locks below would make its directory.
    if not own.exists():
        raise PodError("successor_unrecorded", "Write the successor objective's first checkpoint before the claim",
                       {"next_action": "checkpoint the successor objective, then rerun internal succession"})
    if not other.exists():
        raise PodError("predecessor_unknown", "No objective of that name is recorded in this repository",
                       {"next_action": "name the predecessor objective exactly as pod status --list shows it"})
    with ExitStack() as stack:
        for path in sorted((own, other), key=str):
            stack.enter_context(_lock(path))
        state = _read(own)
        if state.get("checkpoint") is None:
            raise PodError("successor_unrecorded", "Write the successor objective's first checkpoint before the claim",
                           {"next_action": "checkpoint the successor objective, then rerun internal succession"})
        require_authority(project, objective, owner=owner, state=state, native_port=port, claim=predecessor)
        from .evidence_closure import refuse_closed
        refuse_closed(other.parent)  # The predecessor's record accepts no claim once closed by evidence (R145).
        try:
            prior = _read(other)
        except PodError as exc:
            raise PodError("predecessor_unverified", "The predecessor record is unreadable or on a superseded schema",
                           {"code": exc.code, "next_action": "only a readable current record can be continued"}) from exc
        verdict = _predecessor_facts(project, objective, predecessor, state, prior, owner, port=port)
        capability = None
        def authorize(recorded=None, *, completion=False):
            nonlocal capability, decision
            carried, failure = _carried(project, predecessor, prior)
            if failure is not None:
                raise PodError(failure[0], failure[1])
            effect = {"decision": requested_decision, "carried": carried}
            states = {"successor": deepcopy(state), "predecessor": deepcopy(prior)}
            if recorded is None:
                capability = Decision(project, objective, kind="claim", effect=effect, state=states,
                                      decision=requested_decision, resources={"predecessor": predecessor, "successor": objective})
            else:
                prepare = Decision.complete if completion else Decision.resume
                capability = prepare(project, objective, recorded, effect=effect)
            capability.keys = ["ownership:" + digest(name) for name in (predecessor, objective)]
            capability.phases({"successor": own, "predecessor": other}, states)
            stack.enter_context(capability.scope())
            decision = owner_decision(decision, kind="claim")
            capability.finalize(decision)
        if verdict["status"] == "claimed":
            back = state.get("continues")
            if isinstance(back, dict) and back.get("objective") != predecessor:
                raise PodError("successor_started", f"This successor already continues objective {back['objective']}",
                               {"next_action": "a successor continues exactly one predecessor"})
            link = prior["continued_by"]
            recorded = {"predecessor": predecessor, "predecessor_owner": prior["owner"], "successor": objective,
                        "successor_owner": link["owner"], "runs": link["runs"], "ambiguity": link["ambiguity"],
                        CARRIED: verdict["facts"][CARRIED]}
            if decision is not None and _differs(decision, recorded):
                raise PodError("succession_decision_mismatch",
                               "The decision differs from the recorded claim. Nothing was written.",
                               {"next_action": "rerun with the decision that made the claim"})
            if not verdict["complete"]:
                if decision is None:  # Detection never writes, even to finish an interrupted claim.
                    return {**verdict, "next_action": "rerun internal succession with the claim's decision "
                                                      "to record the successor's link"}
                if generic:
                    authorize(link, completion=True)
                # Before copying rows, the successor must still be open with no work of its own: only rows and
                # units this claim already copied may be there (R118).
                blocker = _started(project, objective, state, predecessor=predecessor,
                                   carried=frozenset(recorded[CARRIED])) if recorded[CARRIED] else None
                if blocker:
                    raise PodError("successor_started", "The successor changed while the claim was incomplete: "
                                   + blocker + "; no carried row is copied. Nothing was written.",
                                   {"next_action": "the owner decides how the predecessor's unsettled effects "
                                                   "settle, for example by historical retirement"})
                completed = {**state, "continues": {"objective": predecessor, "record": verdict["record"],
                                                    "at": prior["continued_by"]["at"],
                                                    **({"approval": dict(capability.record)} if capability else {})}}
                _require_fit(project, objective, predecessor, prior, recorded[CARRIED], at=link["at"],
                             records={"successor's record": completed}, linked=True)
                _carry(project, objective, predecessor, prior, recorded[CARRIED], at=link["at"])
                state["continues"] = completed["continues"]
                _write(own, state)
            return {"status": "claimed", "replayed": True, "predecessor": predecessor, "successor": objective,
                    "continued_by": prior["continued_by"], CARRIED: recorded[CARRIED]}
        reserved = reserved_for(state) == predecessor
        if verdict["status"] != "claim_available" and reserved:
            # The claim reserved this successor and stopped before linking the predecessor, which cannot be
            # claimed now: the owner's decision releases the reservation, and detection says so.
            if decision is None:
                return {**verdict, "reserved": True,
                        "next_action": "rerun internal succession with the claim's decision to release this "
                                       "successor's reservation, since the claim cannot complete"}
            if _scope(decision) != state["continues"]["decision"]:
                raise PodError("succession_decision_mismatch",
                               "The decision differs from the one that reserved this successor. Nothing was written.",
                               {"succession": verdict, "next_action": "rerun with the decision that made the claim"})
            if generic:
                authorize(state["continues"].get("approval"), completion=True)
            state.pop("continues")
            _write(own, state)
            raise PodError(verdict["condition"], verdict["message"] + " The successor's reservation was released.",
                           {"succession": verdict, "next_action": verdict["next_action"]})
        if decision is None:
            return verdict
        if verdict["status"] != "claim_available":
            raise PodError(verdict["condition"], verdict["message"],
                           {"succession": verdict, "next_action": verdict["next_action"]})
        facts = verdict["facts"]
        if (reserved and _scope(decision) == state["continues"]["decision"]
                and _handed_over(state, decision["successor_owner"], owner, since=state["continues"]["at"])):
            # The decision that reserved this successor names its owner then; a completed owner handoff since then
            # moved the successor to this caller, so the authorized owner stands and every other fact is fresh.
            facts = {**facts, "successor_owner": decision["successor_owner"]}
        if _differs(decision, facts):
            raise PodError("succession_decision_mismatch",
                           "The decision differs from fresh predecessor, successor, owner, Run, ambiguity or carried "
                           "Governor facts. Nothing was written.",
                           {"succession": verdict, "next_action": verdict["next_action"]})
        if generic:
            authorize(state["continues"].get("approval") if reserved else None)
            fresh = _predecessor_facts(project, objective, predecessor, state, prior, owner, port=port)
            if (fresh["status"] != "claim_available" or fresh["facts"] != verdict["facts"]
                    or digest(_carried(project, predecessor, prior)[0]) != digest(capability.effect["carried"])):
                raise PodError("succession_decision_mismatch", "Claim eligibility or native identities changed during verification")
        at = capability.commit_clock() if capability else datetime.now(timezone.utc).isoformat()
        link = {"objective": objective, "record": own.parent.name, "owner": facts["successor_owner"],
                "runs": list(verdict["facts"]["runs"]), "at": at, "instruction": decision["instruction"],
                **channel_fields(decision),
                "unfinished": verdict["unfinished"], "unreported": verdict["unreported"],
                "ambiguity": list(verdict["facts"]["ambiguity"]), "orca_claim": "unavailable"}
        if capability is not None:
            link["provenance"] = decision["provenance"]
        reservation = {"objective": predecessor, "record": other.parent.name, "at": at, "state": CLAIMING,
                       "decision": _scope(decision)}
        completed = {"objective": predecessor, "record": other.parent.name, "at": at}
        if capability is not None:
            reservation["approval"] = dict(capability.record)
            completed["approval"] = dict(capability.record)
        # Every record the claim writes must fit before the first is written, so a claim too large for the
        # successor never freezes the predecessor or reserves the successor.
        try:
            _require_fit(project, objective, predecessor, prior, verdict["facts"][CARRIED], at=at, linked=False,
                         records={"predecessor's record": {**prior, "continued_by": link},
                                  "successor's reserved record": {**state, "continues": reservation},
                                  "successor's record": {**state, "continues": completed}})
        except PodError as exc:
            if not (reserved and _scope(decision) == state["continues"]["decision"]):
                raise
            # An earlier reservation of this same claim cannot complete either: its decision releases it.
            state.pop("continues")
            _write(own, state)
            raise PodError(exc.code, str(exc) + " The successor's reservation was released.", exc.detail) from exc
        prior["continued_by"] = link
        # Order: the successor's reservation first, so it accepts no other write until the claim completes; then
        # the predecessor's link, which freezes its record and Governor journal; then the carried rows, read from
        # that frozen journal; then the successor's link without the reservation, which marks the claim complete.
        # A replay after any interruption recomputes the same rows and completes what is missing.
        if not reserved:
            state["continues"] = reservation
            _write(own, state)
        _write_succession(other, prior)
        if capability is not None:
            capability.completion_only = True
        _carry(project, objective, predecessor, prior, verdict["facts"][CARRIED], at=at)
        state["continues"] = completed
        _write(own, state)
        return {"status": "claimed", "replayed": False, "predecessor": predecessor, "successor": objective,
                "continued_by": prior["continued_by"], CARRIED: list(verdict["facts"][CARRIED])}


def _delivered_route(project: Path, predecessor: str, verdict: dict, *, port) -> dict:
    """A claim stays available with its own decision, but a delivered predecessor needs none: its next action points
    to the evidence closure instead of asking the owner (R147)."""
    if verdict["status"] != "claim_available":
        return verdict
    from .evidence_closure import SWEEP_ACTION, prove
    if prove(project, predecessor, port=port, readback=False)["status"] != "proved":
        return verdict
    shown = {key: value for key, value in verdict.items() if key != "question"}
    return {**shown, "delivered": True,
            "next_action": SWEEP_ACTION[0].upper() + SWEEP_ACTION[1:] + "; a claim stays optional and moves "
                           "authority only."}


def _handed_over(state: dict, original: str, current: str, *, since: str) -> bool:
    """Whether completed owner handoffs or window continuations recorded after `since` moved this objective from
    `original` to `current`. A window continuation moves authority only; the reserving decision keeps its owner."""
    if original == current or state.get("owner") != current:
        return False
    history = ((state.get("checkpoint") or {}).get("continuity") or {}).get("history") or []
    owner = original
    for entry in history:
        decision = entry.get("decision") if isinstance(entry, dict) else None
        if (entry.get("provenance") == "owner_handoff" and entry.get("state") == "done" and isinstance(decision, dict)
                and str(entry.get("at")) >= since and decision.get("from_owner") == owner):
            owner = decision.get("to_owner")
        elif (entry.get("provenance") == "window_continuation" and entry.get("state") == "done"
              and str(entry.get("at")) >= since and entry.get("from_owner") == owner):
            owner = entry.get("to_owner")
    return owner == current


def _differs(decision: dict, facts: dict) -> bool:
    return (any(decision[key] != facts[key] for key in FACTS)
            or decision.get(CARRIED, []) != facts.get(CARRIED, []))


def _carried_journal(project: Path, objective: str, predecessor: str, prior: dict, record_ids: list[str], *,
                     at: str) -> tuple[dict, bool]:
    """The successor's own journal with the carried rows copied in, and whether that changed it; writes nothing."""
    from .governor import _read_journal, _record_path, carry_into
    from .ledger import map_of
    journal = _read_journal(_record_path(project, objective))
    changed = bool(record_ids) and carry_into(
        journal, _read_journal(_record_path(project, predecessor)), record_ids, objective=predecessor,
        governance=(map_of(prior) or {}).get("governance"), at=at)
    return journal, changed


def _carry(project: Path, objective: str, predecessor: str, prior: dict, record_ids: list[str], *, at: str) -> None:
    """Write the carried rows into the successor's own journal; the caller holds both objective locks."""
    from .governor import _record_path, _write_journal
    journal, changed = _carried_journal(project, objective, predecessor, prior, record_ids, at=at)
    if changed:
        _write_journal(_record_path(project, objective), journal)


def _require_fit(project: Path, objective: str, predecessor: str, prior: dict, record_ids: list[str], *, at: str,
                 records: dict[str, dict], linked: bool) -> None:
    """Refuse, before any write, a claim one of whose records would exceed its byte limit (R118).

    `records` are the context records the claim still writes, as each will stand; the successor's journal is built
    with every carried row and marker, exactly as the claim copies it. The limits never change.
    """
    from .governor import MAX_JOURNAL
    from .ledger import CONTEXT_LIMIT, _path
    from .history import prepare_context, prepare_document, preflight_landing
    from .util import encoded, protect_decision_record
    journal, _ = _carried_journal(project, objective, predecessor, prior, record_ids, at=at)
    protect_decision_record(journal)
    from .governor import _record_path
    journal_path = _record_path(project, objective)
    journal_record, journal_chunks = prepare_document(journal_path, {**journal, "revision": journal["revision"] + 1})
    preflight_landing(journal_path, sum(map(len, journal_chunks.values())))
    planned = [("successor's Governor journal", journal_record, MAX_JOURNAL)]
    for name, value in records.items():
        candidate = {**value, "revision": value["revision"] + 2}
        from .relay import stamp_decision
        stamp_decision(candidate)
        protect_decision_record(candidate)
        path = _path(project, predecessor if name.startswith("predecessor") else objective)
        try:
            physical, _ = prepare_context(path, candidate)
        except PodError as exc:
            if exc.code != "record_too_large":
                raise
            # Keep the established claim refusal and remedy; nothing has been published.
            physical = None
            size = exc.detail["proposed_bytes"]
        else:
            size = len(encoded(physical, compact=True))
        planned.append((name, physical, CONTEXT_LIMIT, size))
    for entry in planned:
        name, value, limit = entry[:3]
        size = entry[3] if len(entry) == 4 else len(encoded(value, compact=True))
        if size <= limit:
            continue
        route = ("no supported route completes a claim linked before this check: only unreleased candidates of it "
                 "could link one" if linked else
                 "settle the predecessor's unsettled rows by readback first, through an owner handoff of the "
                 "predecessor (R105) to a live coordinator, or end delivered work by historical retirement (R112)")
        raise PodError("claim_capacity",
                       f"The claim cannot fit: the {name} would be {size} bytes, over its {limit}-byte limit. "
                       "Nothing was written.",
                       {"record": name, "proposed_bytes": size, "limit_bytes": limit, "next_action": route})

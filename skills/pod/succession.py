"""A successor objective's claim on the objective of a lost coordinator (R110).

The claim ends the predecessor's write authority and gives it to no one: its owner, admissions,
Runs, map and evidence stay exactly as recorded, and its old coordinator is refused by name. It
never steals: Orca must report the predecessor's coordinator gone and its work settled. Orca has
no worktree claim or conditional send, so the claim binds none (`orca_claim: unavailable`).
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path

from .errors import PodError
from .util import bounded_text, exact

DECISION = {"provenance", "instruction", "predecessor", "predecessor_owner", "successor",
            "successor_owner", "runs", "ambiguity"}
FACTS = DECISION - {"provenance", "instruction"}
CONTINUED = {"objective", "record", "owner", "runs", "at", "instruction", "unfinished", "unreported",
             "ambiguity", "orca_claim"}
CONTINUES = {"objective", "record", "at"}
MAX_CHAIN = 8
MAX_LISTED = 64
ACTION = "Read the successor objective with pod status --objective {}; this record accepts no writes."


def _texts(value: object, *, limit: int) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_LISTED
            and all(isinstance(item, str) and 0 < len(item) <= limit for item in value))


def validate_link(name: str, value: object) -> None:
    """The stored shape of either side of a claim; anything else makes the record unsupported."""
    fields = CONTINUED if name == "continued_by" else CONTINUES
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024
                   for key in fields - {"runs", "unfinished", "unreported", "ambiguity"})
            or name == "continued_by" and (value["orca_claim"] != "unavailable"
                                           or not all(_texts(value[key], limit=256)
                                                      for key in ("runs", "unfinished", "unreported"))
                                           or not _texts(value["ambiguity"], limit=512))):
        raise PodError("state_unsupported", f"Objective {name.replace('_', ' ')} record is malformed")


def refuse_continued(state: dict | None) -> None:
    """The one refusal a continued objective gives every write, including its own coordinator's."""
    link = state.get("continued_by") if isinstance(state, dict) else None
    if isinstance(link, dict):
        raise PodError("objective_continued",
                       f"This objective was continued by successor objective {link['objective']}; "
                       "its record is kept and accepts no writes",
                       {"successor": link["objective"], "next_action": ACTION.format(link["objective"])})


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
        if not isinstance(back, dict) or back.get("objective") != (current.get("checkpoint") or {}).get("objective"):
            return {"state": "claim_incomplete", "objective": name,
                    "reason": "the successor does not yet record the claim; rerun the same claim"}
        current = successor
    else:
        if isinstance(current.get("continued_by"), dict):
            return {"state": "unknown", "objective": name, "reason": "successor chain exceeds eight objectives"}
    closed = bool(((current.get("checkpoint") or {}).get("closure")))
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
                     unreported=list(link["unreported"]), orca_claim=link["orca_claim"])
        if chain.get("reason"):
            block["reason"] = chain["reason"]
    if isinstance(back, dict):
        block["continues"] = back["objective"]
        block["continues_at"] = back["at"]
    return block


def _started(project: Path, objective: str, state: dict) -> str | None:
    """Why the successor has already begun work, if it has."""
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
        return "the successor's Governor journal is unreadable"
    if journal["actions"] or journal["units"]:
        return "the successor already has Governor work"
    return None


def _predecessor_facts(project: Path, objective: str, predecessor: str, state: dict,
                       prior: dict | None, owner: str, *, port) -> dict:
    """Fresh facts, read-only; an unproven fact holds the claim."""
    from .handoff import unfinished
    from .ledger import _governor_pending, _native_assignment_settled, _run_references, map_of, record_name
    from .github import repository_context
    from .orca import run_show, terminal_identity, worker_rows
    from .obligations import TERMINAL
    facts = {"predecessor": predecessor, "predecessor_owner": None, "successor": objective,
             "successor_owner": owner, "runs": [], "ambiguity": []}
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
            back = state.get("continues")
            verdict.update(status="claimed", condition="claimed_by_this_successor", record=record,
                           complete=isinstance(back, dict) and back.get("objective") == predecessor,
                           message="This successor already holds the claim.")
            return verdict
        return fail("predecessor_claimed", f"Successor objective {link['objective']} already continues it.",
                    ACTION.format(link["objective"]))
    back = state.get("continues")
    if isinstance(back, dict):
        return fail("successor_started", f"This successor already continues objective {back['objective']}.")
    started = _started(project, objective, state)
    if started:
        return fail("successor_started", "Claim before the successor starts work: " + started + ".")
    map_state = map_of(prior)
    if map_state is not None and map_state.get("closure"):
        return fail("predecessor_closed", "The predecessor objective is closed; a closed objective needs no successor.")
    if prior.get("checkpoint") is None or not prior.get("owner"):
        return fail("predecessor_unclaimable", "The predecessor has no recorded coordinator to end.")
    facts["predecessor_owner"] = prior["owner"]
    if unfinished(prior) is not None:
        return fail("predecessor_handoff_pending", "The predecessor has an unfinished owner handoff.")
    try:
        runs = sorted(_run_references(prior))
    except PodError:
        return fail("predecessor_unverified", "The predecessor's Run references are unreadable.")
    facts["runs"] = runs
    if not runs:
        return fail("predecessor_unverified", "The predecessor records no Run to prove settled.")
    previous = terminal_identity(prior["owner"])
    if previous["state"] == "live":
        return fail("predecessor_live", "Orca reports the predecessor's coordinator live; the claim never steals.",
                    "Continue in the predecessor's own terminal, or close it first.")
    if previous["state"] != "lost":
        return fail("predecessor_unverified", "Orca cannot prove the predecessor's coordinator gone ("
                    + (previous["code"] or previous["state"]) + ").")
    dispatches = {}
    for run in runs:
        try:
            shown = run_show(run)
        except PodError as exc:
            return fail("predecessor_unverified", f"A predecessor Run could not be read ({exc.code}).")
        holder = shown["coordinator_handle"]
        if holder is not None and holder != prior["owner"]:
            other = terminal_identity(holder)
            if other["state"] == "live":
                return fail("predecessor_run_held", "Another live terminal coordinates a predecessor Run.",
                            "Continue on the successor's own Run; same-Run continuation is an owner handoff.")
            if other["state"] != "lost":
                return fail("predecessor_unverified", "A predecessor Run's coordinator is unverified.")
        try:
            fleet = worker_rows(run)
        except PodError as exc:
            return fail("predecessor_unverified", f"A predecessor Run's workers could not be read ({exc.code}).")
        if fleet.get("complete") is not True:
            return fail("predecessor_unverified", "A predecessor Run's worker list is incomplete.")
        for row in fleet["workers"]:
            dispatch = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(dispatch, str) or not dispatch:
                return fail("predecessor_unverified", "A predecessor worker has no Dispatch identity.")
            dispatches[dispatch] = run
    if _governor_pending(project, predecessor):
        return fail("predecessor_effect_pending", "The predecessor has a Governor effect whose outcome is pending.")
    unreported = []
    bound = {}
    for key in sorted(prior["admissions"]):
        row = prior["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        if row["state"] == "unresolved":
            return fail("predecessor_work_unsettled", f"Predecessor admission {key[:12]} is unresolved.")
        if row["state"] == "reserved":
            try:
                found = port.find_worker(run=row["run_id"], task=row["task_id"])
            except PodError as exc:
                return fail("predecessor_unverified", f"A reserved predecessor start could not be read ({exc.code}).")
            if found:
                return fail("predecessor_work_unsettled", f"Predecessor admission {key[:12]} has an unbound Dispatch.")
            facts["ambiguity"].append(f"reserved admission {key}: no Dispatch found")
            continue
        bound[row["native_binding"]["dispatchId"]] = (key, row)
    for dispatch in sorted(set(dispatches) | set(bound)):
        try:
            shown = port.show_worker(dispatch)
        except PodError as exc:
            return fail("predecessor_unverified", f"A predecessor worker could not be read ({exc.code}).")
        result = shown.get("result") if isinstance(shown, dict) else None
        if dispatch in bound:
            key, row = bound[dispatch]
            binding = row["native_binding"]
            parts = [result.get(name) if isinstance(result, dict) else None for name in ("dispatch", "projection", "worker")]
            if not all(isinstance(part, dict) for part in parts) or (
                    parts[0].get("id"), parts[0].get("runId"), parts[0].get("taskId"), parts[1].get("id"),
                    parts[2].get("worktreeId")) != (binding["dispatchId"], binding["runId"], binding["taskId"],
                                                    binding["workerId"], binding["worktreeId"]):
                return fail("predecessor_unverified", f"Predecessor admission {key[:12]} has a differing native identity.")
            if (row.get("report") or {}).get("status") != "validated_observation":
                unreported.append(key)
        if not _native_assignment_settled(shown):
            return fail("predecessor_work_live", "A predecessor worker is live or unsettled; the claim waits for it.",
                        "Settle the predecessor's workers through Orca, then rerun the claim.")
    unfinished_rows = [ob["id"] for ob in (map_state or {}).get("obligations", []) if ob["state"] not in TERMINAL]
    question = (f"May Pod record objective {objective} as the successor of objective {predecessor}? "
                "Orca reports the predecessor's coordinator gone and its work settled; the predecessor's record is "
                "kept, and it will accept no further writes.")
    verdict.update(status="claim_available", condition="claimable", record=record, question=question,
                   unfinished=unfinished_rows[:MAX_LISTED], unreported=unreported[:MAX_LISTED],
                   message="The predecessor's coordinator is gone and its work settled.",
                   next_action="Confirm with the owner's direct instruction to continue the predecessor: "
                               + question + " Then rerun internal succession with that decision.")
    return verdict


def succession(project: Path, objective: str, *, owner: str, predecessor: str,
               decision: object | None = None, port=None) -> dict:
    """Detect without a decision; claim with an exact one. Locks are taken in path order."""
    from .ledger import _lock, _path, _read, _write, _write_succession, require_authority
    from .operations import OrcaPort
    bounded_text(predecessor, name="predecessor", limit=256)
    if decision is not None:
        decision = exact(decision, DECISION, DECISION, name="succession_decision")
        bounded_text(decision["instruction"], name="instruction", limit=1024)
        if decision["provenance"] != "user_direct":
            raise PodError("succession_decision_mismatch", "Only the owner's direct instruction permits a claim")
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
        require_authority(project, objective, owner=owner, state=state, native_port=port)
        try:
            prior = _read(other)
        except PodError as exc:
            raise PodError("predecessor_unverified", "The predecessor record is unreadable or on a superseded schema",
                           {"code": exc.code, "next_action": "only a readable current record can be continued"}) from exc
        verdict = _predecessor_facts(project, objective, predecessor, state, prior, owner, port=port)
        if verdict["status"] == "claimed":
            back = state.get("continues")
            if isinstance(back, dict) and back.get("objective") != predecessor:
                raise PodError("successor_started", f"This successor already continues objective {back['objective']}",
                               {"next_action": "a successor continues exactly one predecessor"})
            if not verdict["complete"]:
                if decision is None:  # Detection never writes, even to finish an interrupted claim.
                    return {**verdict, "next_action": "rerun internal succession with the claim's decision "
                                                      "to record the successor's link"}
                state["continues"] = {"objective": predecessor, "record": verdict["record"],
                                      "at": prior["continued_by"]["at"]}
                _write(own, state)
            return {"status": "claimed", "replayed": True, "predecessor": predecessor, "successor": objective,
                    "continued_by": prior["continued_by"]}
        if decision is None:
            return verdict
        if verdict["status"] != "claim_available":
            raise PodError(verdict["condition"], verdict["message"],
                           {"succession": verdict, "next_action": verdict["next_action"]})
        if any(decision[key] != verdict["facts"][key] for key in FACTS):
            raise PodError("succession_decision_mismatch",
                           "The decision differs from fresh predecessor, successor, owner, Run or ambiguity facts. "
                           "Nothing was written.", {"succession": verdict, "next_action": verdict["next_action"]})
        at = datetime.now(timezone.utc).isoformat()
        prior["continued_by"] = {"objective": objective, "record": own.parent.name, "owner": owner,
                                 "runs": list(verdict["facts"]["runs"]), "at": at,
                                 "instruction": decision["instruction"], "unfinished": verdict["unfinished"],
                                 "unreported": verdict["unreported"],
                                 "ambiguity": list(verdict["facts"]["ambiguity"]), "orca_claim": "unavailable"}
        _write_succession(other, prior)
        state["continues"] = {"objective": predecessor, "record": other.parent.name, "at": at}
        _write(own, state)
        return {"status": "claimed", "replayed": False, "predecessor": predecessor, "successor": objective,
                "continued_by": prior["continued_by"]}

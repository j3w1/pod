"""Readiness: whether an objective is finished and whether Pod still needs its coordinator (R114).

One plain answer for every reader, owner or not, projected from the records Pod already keeps (the
objective's state and map, its Governor journal and admissions) and the native reads status already
makes. It stores nothing and changes no value Pod stores or later re-derives. An idle prompt, a merged
pull request or a finished worker never reads closed: only a recorded closure with none of Pod's own
records left unfinished does.
"""

from __future__ import annotations

from pathlib import Path

from .errors import PodError
from .term import clean

CONTRACT = "pod-readiness/v1"
STATES = ("working", "waiting", "finalizing", "closed", "closed_external", "coordinator_lost",
          "retired", "continued", "unavailable")
# Who an external wait class names when its row carries no party of its own.
WAIT_PARTIES = {"authority": "owner", "user_hold": "user"}
MAX_LISTED = 32


def unavailable(reason: str, message: str) -> dict:
    """Status cannot answer for this objective; nothing is inferred from what it could not read."""
    return {"contract": CONTRACT, "state": "unavailable", "summary": f"Unavailable: {message}.",
            "acts_next": "unknown", "coordinator_needed": None, "coordinator": None, "finalization": None,
            "reason": {"code": reason, "message": message}}


def journal(root: Path) -> dict:
    """One objective's Governor journal through its own reader; an unreadable journal raises."""
    from .governor import _read_journal
    return _read_journal(root / "governor.json")


def _waiting(map_state: dict | None) -> list[dict]:
    """The named decisions and parties a quiescent objective waits on, from its derived quiescence."""
    quiescence = (map_state or {}).get("quiescence") or {}
    if quiescence.get("state") != "quiescent":
        return []
    rows = []
    for row in quiescence.get("blocked", []):
        if row.get("via") != row.get("obligation"):
            continue  # A dependency chain is named once, at the obligation it ends on.
        if "party" in row:
            rows.append({"obligation": row["obligation"], "party": row["party"], "need": row["need"]})
        else:
            rows.append({"obligation": row["obligation"], "party": WAIT_PARTIES.get(row["class"]),
                         "class": row["class"], "referent": row["referent"]})
    return rows[:MAX_LISTED]


def _gates(checkpoint: dict) -> list[dict]:
    """Work the coordinator left outside a closed objective, with who acts when the gate names it."""
    rows = []
    for gate in checkpoint.get("remaining_gates") or []:
        party = gate.get("party") if isinstance(gate, dict) else None
        if isinstance(gate, dict) and gate.get("kind") == "hosted_ci" and party is None:
            party = "provider"
        rows.append({"gate": gate, "party": party if isinstance(party, str) else None})
    return rows[:MAX_LISTED]


def base(state: dict, journal: dict, *, outstanding: set[str]) -> dict:
    """The record-derived readiness, before any coordinator read: never coordinator_lost.

    What remains to record is the Governor's one finalization projection; this only decides the state.
    Delivery is under way once a merge is decided or recorded, or every obligation is satisfied or
    withdrawn: until closure the objective is then finalizing, however idle its coordinator looks.
    """
    from .governor import finalization
    from .ledger import map_of
    from .obligations import TERMINAL
    map_state = map_of(state)
    pending = finalization(state, journal)
    names = pending["remaining"]
    obligations = (map_state or {}).get("obligations") or []
    delivered = ((map_state or {}).get("delivery") is not None or "merge_outcome" in names
                 or "delivery_record" in names
                 or bool(obligations) and all(ob["state"] in TERMINAL for ob in obligations))
    block = {"contract": CONTRACT, "state": "working", "acts_next": "coordinator",
             "coordinator_needed": True, "coordinator": None, "finalization": pending}
    waiting = _waiting(map_state)
    if waiting:
        block["waiting_on"] = waiting
    if (map_state or {}).get("closure"):
        gates = _gates(state["checkpoint"])
        parties = {row["party"] for row in gates}
        # One named party acts on every gate; otherwise the owner decides who does the outside work.
        acts = "none" if not gates else parties.pop() if len(parties) == 1 and None not in parties else "owner"
        block.update(state="closed_external" if gates else "closed", coordinator_needed=False, acts_next=acts)
        if gates:
            block["external"] = gates
    elif delivered:
        block["state"] = "finalizing"
        # Delivered with only closure left: the coordinator records it now, whoever a recorded wait names (R144).
    elif waiting:
        block.update(state="waiting", acts_next=waiting[0]["party"] or "unknown")
    elif outstanding and not any(ob["state"] == "active" and ob.get("executor") == "coordinator"
                                 for ob in obligations):
        block["acts_next"] = "worker"
    return block


def label(root: Path, state: dict) -> str:
    """The list's compact readiness from Pod's records alone (R108): no Orca read, so never coordinator_lost.

    Without a native read no open admission is proven settled.
    """
    if isinstance(state.get("continued_by"), dict):
        return "continued"
    if isinstance(state.get("historical_retirement"), dict):
        return "retired"
    if not isinstance(state.get("checkpoint"), dict):
        return "unavailable"
    try:
        recorded = journal(root)
    except PodError:
        return "unavailable"
    outstanding = {key for key, row in state["admissions"].items() if row["state"] not in ("closed", "deferred")}
    return base(state, recorded, outstanding=outstanding)["state"]


def coordinator(state: dict) -> dict:
    """The recorded coordinator: handle, Run and generation as Pod recorded them."""
    from .ledger import _run_references
    checkpoint = state.get("checkpoint") or {}
    binding = (checkpoint.get("continuity") or {}).get("binding")
    try:
        refs = _run_references(state)
    except PodError:
        refs = {}
    runs = sorted(refs)
    runtimes = set(refs.values())
    return {"handle": state.get("owner"),
            "run": binding["run"] if isinstance(binding, dict) else runs[0] if len(runs) == 1 else None,
            "runs": runs[:MAX_LISTED],
            "generation": binding["generation"] if isinstance(binding, dict) else None,
            "runtime": next(iter(runtimes)) if len(runtimes) == 1 else None,
            "liveness": "not_read", "code": None}


def _liveness(identity: dict) -> tuple[str, str | None]:
    if identity["state"] == "live":
        return "live", None
    if identity["state"] == "lost":
        return "lost", identity.get("code")
    return ("exited", None) if identity.get("exited") else ("unknown", identity.get("code") or "unreadable")


def _handoff_route(state: dict, facts: dict) -> dict:
    """What every caller's owner handoff (R105) would meet first; the acting terminal checks the rest."""
    from .handoff import unfinished
    from .orca import run_show
    route = {"route": "owner_handoff", "status": "blocked"}
    if (state.get("checkpoint") or {}).get("closure"):
        return {**route, "condition": "objective_closed", "message": "the objective is closed; closure does not "
                                                                     "authorize a handoff"}
    if facts["liveness"] == "exited":
        return {**route, "condition": "owner_not_lost", "message": "Orca shows the coordinator's terminal closed; "
                                                                   "a handoff needs a stale or gone handle"}
    if facts["runtime"] is None or facts["run"] is None:
        return {**route, "condition": "recorded_runtime_unreadable",
                "message": "the objective has no single recorded Run and runtime"}
    try:
        pending = unfinished(state)
        shown = run_show(facts["run"])
    except PodError as exc:
        return {**route, "status": "unverified", "condition": "run_unreadable",
                "message": f"the recorded Run could not be read ({exc.code})"}
    holder = shown["coordinator_handle"]
    if holder == facts["handle"]:
        if facts["generation"] is not None and shown["consumer_generation"] != facts["generation"]:
            return {**route, "condition": "generation_changed",
                    "message": "the Run's consumer generation differs from the recorded one"}
        condition = "fresh"
    elif pending and pending["state"] == "pending" and pending["decision"]["to_owner"] == holder:
        condition = "resume"
    else:
        return {**route, "condition": "lineage_unproven",
                "message": "the Run was rebound outside a pending handoff"}
    return {**route, "status": "available", "condition": condition,
            "message": f"from a live terminal on Run {facts['run']} with the owner's direct confirmation; "
                       "that terminal's status runs the remaining checks"}


def _evidence_route(project: Path | None, objective: str, port) -> dict:
    """Closure by evidence (R145): first and needing no owner decision. Its proof reads Orca and GitHub."""
    route = {"route": "evidence_closure"}
    if project is None:
        return {**route, "status": "unverified", "condition": "record_elsewhere",
                "message": "this record is not one of this repository's; read it from its own worktree"}
    from .evidence_closure import prove
    proof = prove(project, objective, port=port, readback=False)
    if proof["status"] == "proved":
        return {**route, "status": "available", "condition": "proved",
                "message": "delivery is proved; pod sweep closes it by evidence, with no owner decision"}
    first = next((row for row in proof["conditions"] if row["status"] == proof["status"]), proof["conditions"][0])
    return {**route, "status": "unverified" if proof["status"] == "unverified" else "blocked",
            "condition": first["code"], "message": first["message"],
            "conditions": [{key: row[key] for key in ("code", "status", "message")} for row in proof["conditions"]]}


def _claim_route(claim: dict | None) -> dict:
    route = {"route": "successor_claim"}
    if claim is None:
        return {**route, "status": "unverified", "condition": "not_read", "message": "no successor claim facts"}
    if claim["status"] == "claim_available":
        return {**route, "status": "available", "condition": "claimable",
                "message": "a new objective, recorded and not yet started, may claim it with the owner's "
                           "direct instruction"}
    return {**route, "status": "blocked", "condition": claim["condition"], "message": claim["message"]}


# Plain names of the Governor kinds a readback settles; release and deploy record their own outcome.
_READBACKS = {"push": "push", "pr_update": "pull request update", "workflow_dispatch": "workflow dispatch",
              "validation_rerun": "validation rerun", "remote_diagnostic": "remote diagnostic",
              "cancel_validation": "validation cancellation"}


def _record(name: str, entry: dict) -> str:
    """One remaining record in plain words, by its kind and what it derives from."""
    referent, kind = clean(entry["record"])[:12], clean(entry["kind"])
    if name == "merge_outcome":
        return f"the merge outcome ({referent})"
    if name == "delivery_record":
        return f"the delivery record of merge {referent}"
    if name == "report_consumption":
        return (f"the disposition of implementation result {referent}" if entry["state"] == "undispositioned"
                else f"the {kind} worker's report ({referent})")
    if entry.get("derived_from"):
        return f"the validation readback of publication {clean(entry['derived_from'])[:12]} ({referent})"
    if kind in ("release", "deploy"):
        return f"the {kind} outcome ({referent})"
    return f"the {_READBACKS.get(kind, kind)} readback ({referent})"


def _phrase(pending: dict) -> str:
    parts = [part for name in pending["remaining"]
             for part in (["closure"] if name == "closure" else
                          [_record(name, entry) for entry in pending["records"][name]])]
    if len(parts) > 4:
        parts = parts[:3] + [f"{len(parts) - 4} more record(s)", parts[-1]]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _summary(block: dict) -> str:
    state = block["state"]
    pending = block["finalization"]
    if state == "closed":
        return "Closed: Pod needs nothing more; its coordinator may close."
    if state == "closed_external":
        return (f"Closed with external work: {len(block['external'])} remaining gate(s) outside the objective, "
                f"for the {block['acts_next']}; Pod needs nothing more and its coordinator may close.")
    derived = (pending or {}).get("records", {}).get("validation_readback") or []
    if (state == "finalizing" and pending["remaining"] == ["validation_readback", "closure"]
            and all(entry.get("derived_from") for entry in derived)):
        # Never claims a row preservable before the closing write checks it (C11, Amendment 3).
        return (f"Delivered, not closed: settle each of the {len(derived)} publication-derived validation(s) with "
                "governor-reconcile, or write the closing checkpoint (close: true), which reads each back and "
                "preserves only an earlier-head validation the merged delivery superseded; no owner decision is "
                "needed, and it is not safe to close the coordinator.")
    if state == "finalizing" and pending["remaining"] == ["closure"]:
        return ("Delivered, not closed: the coordinator records closure now with a checkpoint close: true; no owner "
                "decision is needed, and it is not safe to close the coordinator.")
    if state == "finalizing":
        return f"Delivered but finalizing: Pod still needs {_phrase(pending)}; not safe to close the coordinator."
    if state == "waiting":
        row = block["waiting_on"][0]
        need = row.get("need") or f"{row['class']} {row['referent']}"
        who = f"on the {row['party']}" if row["party"] else "for"
        more = f" and {len(block['waiting_on']) - 1} more" if len(block["waiting_on"]) > 1 else ""
        return f"Waiting {who}: {clean(need)[:120]} ({row['obligation']}){more}; not finished."
    if state == "coordinator_lost":
        route = next(row for row in block["recovery"]["routes"] if row["route"] == "evidence_closure")
        if route["status"] == "available":
            return "Coordinator lost; delivery proved: pod sweep closes it by evidence, with no owner decision."
        return f"Coordinator lost; delivery not proved ({route['condition']}): a task for the project lead."
    if state == "working" and block.get("unverified"):
        return (f"Working: {block['unverified']['admissions']} assignment(s) cannot be verified "
                f"({block['unverified']['code']}); not finished.")
    if state == "working" and pending is not None and "closure" not in pending["remaining"]:
        return ("Working: no obligation map is recorded, so Pod tracks no closure; the coordinator decides when "
                "it is finished.")
    if state == "working" and block["acts_next"] == "worker":
        return "Working: worker assignments are in progress; not finished."
    return "Working: the coordinator acts next; not finished."


def assess(project: Path, objective: str, state: dict, root: Path, *, view: dict | None,
           map_error: str | None, failure: dict | None, owner_reader: bool, port=None,
           worker_tabs: list | None = None) -> dict:
    """Status's readiness block and, for a non-owner reader, the facts a successor claim would check (R114).

    `root` is the record's state directory. Only a reader other than the recorded coordinator reads that
    coordinator's terminal: the recorded coordinator's own terminal is live while it reads.
    """
    out = {}
    identity = None
    if isinstance(state.get("continued_by"), dict):
        from .succession import chain_state
        link = state["continued_by"]
        chain = chain_state(project, state)
        block = {"contract": CONTRACT, "state": "continued", "acts_next": "successor",
                 "coordinator_needed": False, "coordinator": coordinator(state), "finalization": None,
                 "reason": {"successor": link["objective"], "succession_state": chain["state"]},
                 "summary": f"Continued by successor objective {clean(link['objective'])[:64]} "
                            f"({chain['state'].replace('_', ' ')}); read that objective, as this record "
                            "accepts no writes."}
    elif isinstance(state.get("historical_retirement"), dict):
        entry = state["historical_retirement"]
        block = {"contract": CONTRACT, "state": "retired", "acts_next": "none", "coordinator_needed": False,
                 "coordinator": coordinator(state), "finalization": None,
                 "reason": {"retired_at": entry["at"], "reason": entry["reason"]},
                 "summary": "Ended by the owner's historical retirement: not completed or closed, and its "
                            "record accepts no writes."}
    elif map_error is not None:
        block = unavailable(map_error, f"the objective map cannot be projected ({map_error})")
    else:
        try:
            recorded = journal(root)
        except PodError as exc:
            recorded = None
            block = unavailable("governor_unreadable", f"the Governor journal cannot be read ({exc.code}); "
                                "a pending effect cannot be ruled out")
        if recorded is not None:
            outstanding = (set(view["ctx"]["outstanding"]) if isinstance(view, dict)
                           else {key for key, row in state["admissions"].items()
                                 if row["state"] not in ("closed", "deferred")})
            block = base(state, recorded, outstanding=outstanding)
            block["coordinator"] = facts = coordinator(state)
            if failure is not None and block["state"] == "working":
                block["unverified"] = {"code": failure["code"], "admissions": len(outstanding)}
            if block["coordinator_needed"] and facts["handle"] is not None:
                if owner_reader:
                    facts["liveness"] = "live"  # The reader is the recorded coordinator's own terminal.
                else:
                    from .orca import terminal_identity
                    identity = terminal_identity(facts["handle"])
                    facts["liveness"], facts["code"] = _liveness(identity)
    out["readiness"] = block
    if not owner_reader and state.get("owner"):
        from .ledger import objective_root
        from .operations import OrcaPort
        from .status import project_for_state
        from .succession import claim_view
        bound = project_for_state(state["checkpoint"], project)
        local = objective_root(bound, objective) == root
        if local:
            claim = claim_view(bound, objective, state, port=port or OrcaPort(bound), previous=identity)
        else:  # The claim reads its predecessor's journal through this repository's records only.
            claim = {"status": "claim_unavailable", "condition": "predecessor_unverified",
                     "facts": {"predecessor": objective, "predecessor_owner": None, "runs": [], "ambiguity": []},
                     "message": "This record is not one of this repository's; read it from its own worktree.",
                     "next_action": "Rerun pod status --objective ID from the objective's repository."}
        out["successor_claim"] = claim
        facts = block.get("coordinator") or {}
        if facts.get("liveness") in ("lost", "exited") and block["state"] in ("working", "waiting", "finalizing"):
            # The evidence closure comes first and needs no owner; handoff and claim stay listed as optional.
            routes = [_evidence_route(bound if local else None, objective, port or OrcaPort(bound)),
                      {"route": "same_run_resume", "status": "blocked", "condition": "owner_not_live",
                       "message": "Orca reports the recorded coordinator's terminal "
                                  + ("closed" if facts["liveness"] == "exited" else "stale or gone")
                                  + "; a conversation resumed in another terminal does not hold its Run"},
                      _handoff_route(state, facts), _claim_route(claim)]
            block.update(state="coordinator_lost", acts_next="lead",
                         recovery={"route": next((row["route"] for row in routes if row["status"] == "available"),
                                                 None), "routes": routes})
            if routes[0]["status"] == "available":
                # Delivered: the optional claim's next action points to the evidence closure, not the owner (R147).
                from .evidence_closure import SWEEP_ACTION
                out["successor_claim"] = {**claim, "next_action": SWEEP_ACTION[0].upper() + SWEEP_ACTION[1:]
                                          + "; a claim stays optional and moves authority only."}
            else:
                from .evidence_closure import LEAD_ACTION
                out["successor_claim"] = {**claim, "next_action": LEAD_ACTION
                    + " A claim stays optional and moves authority only, with its own recorded decision."}
    if "summary" not in block:
        block["summary"] = _summary(block)
    tabs = worker_tabs if worker_tabs is not None else (view or {}).get("worker_tabs", [])
    if tabs:
        from .worker_tabs import finalization, wording
        if block["state"] == "closed":
            block["summary"] = "Closed: " + wording(tabs)
        elif block["state"] == "closed_external":
            block["summary"] = block["summary"].replace(
                "Pod needs nothing more and its coordinator may close.", wording(tabs))
        else:
            block["summary"] += " " + wording(tabs)
        block["finalization"] = finalization(block.get("finalization"), tabs)
    return out

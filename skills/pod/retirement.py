"""The owner's historical retirement of delivered objectives whose coordinator is lost (R112).

Retirement releases named worktrees of delivered history and nothing else. The objective is not
closed, continued, QUALIFIED or successful: its Governor journal, admissions, obligations and
evidence stay exactly as recorded, and the record accepts no later write. No coordinator is
revived, and no successor, agent session or worker is created. CE stays the resource authority.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path

from .errors import PodError
from .util import bounded_text, digest, exact

FIELD = "historical_retirement"
REQUEST = {"objective", "release"}
DECISION = {"provenance", "instruction", "repository", "reason", "objectives"}
DECIDED = {"objective", "owner", "runs", "release", "records", "delivery"}
FACTS = DECIDED - {"delivery"}
ENTRY = {"at", "decision", "instruction", "reason", "repository", "objectives", "owner", "runs",
         "released", "preserved", "unfinished", "delivery", "native"}
GOVERNOR_ROW = {"kind", "id", "action", "candidate", "outcome"}
ADMISSION_ROW = {"kind", "id", "role", "state", "dispatch"}
NATIVE = {"runtime", "owner", "runs", "dispatches", "exited_terminals"}
PRESERVED_OUTCOMES = ("pending", "UNKNOWN", "FAILED")
MAX_OBJECTIVES = 16
MAX_PATHS = 8
MAX_RECORDS = 64
MAX_LISTED = 64
MAX_DELIVERY = 16
ACTION = ("Read it with pod status --objective {}; the owner's historical retirement released its worktrees: "
          "it is not completed, and its record accepts no writes.")


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _texts(value: object, bound: int, limit: int) -> bool:
    return isinstance(value, list) and len(value) <= bound and all(_text(item, limit) for item in value)


def _path_text(value: object) -> bool:
    return (_text(value, 4096) and value.startswith("/") and os.path.normpath(value) == value
            and ".." not in Path(value).parts)


def _record_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("kind") == "governor":
        return (set(row) == GOVERNOR_ROW and all(_text(row[key], 256) for key in ("id", "action", "candidate"))
                and row["outcome"] in PRESERVED_OUTCOMES)
    if row.get("kind") == "admission":
        return (set(row) == ADMISSION_ROW and all(_text(row[key], 256) for key in ("id", "role", "state"))
                and row["state"] in ("reserved", "unresolved", "bound")
                and (row["dispatch"] is None or _text(row["dispatch"], 256)))
    return False


def _runs(value: object) -> bool:
    return (isinstance(value, list) and 0 < len(value) <= 32
            and all(isinstance(row, dict) and set(row) == {"run", "generation"} and _text(row["run"], 256)
                    and type(row["generation"]) is int and row["generation"] >= 0 for row in value))


def _facts_valid(value: dict) -> bool:
    return (_text(value["objective"], 256) and _text(value["owner"], 256) and _runs(value["runs"])
            and isinstance(value["release"], list) and 0 < len(value["release"]) <= MAX_PATHS
            and all(_path_text(path) for path in value["release"])
            and len(set(value["release"])) == len(value["release"])
            and isinstance(value["records"], list) and len(value["records"]) <= MAX_RECORDS
            and all(_record_row(row) for row in value["records"]))


def validate_entry(value: object) -> None:
    """The stored shape of a retirement; anything else makes the record unsupported."""
    native = value.get("native") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != ENTRY
            or not all(_text(value[key], 1024) for key in ("at", "decision", "instruction", "reason", "repository"))
            or not _texts(value["objectives"], MAX_OBJECTIVES, 256) or not value["objectives"]
            or not _text(value["owner"], 256) or not _runs(value["runs"])
            or not isinstance(value["released"], list) or not 0 < len(value["released"]) <= MAX_PATHS
            or not all(_path_text(path) for path in value["released"])
            or not isinstance(value["preserved"], list) or len(value["preserved"]) > MAX_RECORDS
            or not all(_record_row(row) for row in value["preserved"])
            or not _texts(value["unfinished"], MAX_LISTED, 256)
            or not _texts(value["delivery"], MAX_DELIVERY, 512) or not value["delivery"]
            or not isinstance(native, dict) or set(native) != NATIVE
            or not all(_text(native[key], 256) for key in ("runtime", "owner"))
            or any(type(native[key]) is not int or native[key] < 0 for key in ("runs", "dispatches", "exited_terminals"))):
        raise PodError("state_unsupported", "Objective historical retirement record is malformed")


def refuse_retired(state: dict | None) -> None:
    """The one refusal an objective under historical retirement gives every write, its coordinator's included."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if isinstance(entry, dict):
        name = ((state.get("checkpoint") or {}).get("objective")) or "this objective"
        raise PodError("objective_retired",
                       f"The owner's historical retirement of this objective at {entry['at']} released its worktrees; "
                       "it is not completed, and its record accepts no writes",
                       {"retired_at": entry["at"], "next_action": ACTION.format(name)})


def counts(rows: list[dict]) -> dict:
    tally = {"pending": 0, "unknown": 0, "failed": 0, "unreported": 0}
    for row in rows:
        if row["kind"] == "governor":
            tally[{"pending": "pending", "UNKNOWN": "unknown", "FAILED": "failed"}[row["outcome"]]] += 1
        else:
            tally["unreported" if row["state"] == "bound" else "unknown"] += 1
    return tally


def status_block(state: dict) -> dict | None:
    """Public meaning of a retirement for status and the objective list; never closure or success."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if not isinstance(entry, dict):
        return None
    return {"retired_at": entry["at"], "completed": False, "released": list(entry["released"]),
            "preserved": [dict(row) for row in entry["preserved"]], "counts": counts(entry["preserved"]),
            "unfinished": list(entry["unfinished"]), "delivery": list(entry["delivery"]),
            "reason": entry["reason"], "decided_with": list(entry["objectives"])}


def _governor_rows(project: Path, objective: str) -> list[dict]:
    from .governor import _read_journal, _record_path
    journal = _read_journal(_record_path(project, objective))
    return [{"kind": "governor", "id": row["record_id"], "action": row["action"]["kind"],
             "candidate": row["action"]["candidate"], "outcome": row["outcome"]}
            for row in journal["actions"]
            if row["decision"] == "ALLOW" and row["outcome"] in PRESERVED_OUTCOMES]


def _paths(state: dict) -> set[str]:
    from .listing import _facts
    return {os.path.normpath(path) for path in _facts(state)["worktrees"]}


def _detect(project: Path, objective: str, release: list[str], state: dict | None, *, port) -> dict:
    """Fresh facts for one objective, read-only; an unproven fact refuses it alone."""
    from .handoff import unfinished
    from .ledger import _native_assignment_settled, _run_references, map_of
    from .obligations import TERMINAL
    from .orca import EXIT_KINDS, read_command, run_show, terminal_identity, worker_rows
    verdict = {"objective": objective, "status": "refused", "condition": None,
               "facts": {"objective": objective, "owner": None, "runs": [], "release": list(release), "records": []}}

    def fail(condition: str, message: str, action: str | None = None) -> dict:
        verdict.update(condition=condition, message=message,
                       next_action=action or "Resolve the named condition; nothing was written.")
        return verdict

    if state is None or not isinstance(state.get("checkpoint"), dict):
        return fail("objective_unknown", "No readable objective of that name is recorded in this repository.")
    if isinstance(state.get("continued_by"), dict):
        return fail("objective_continued", "A successor continues this objective; historical retirement does not apply to it separately.",
                    "Read the successor with pod status --objective " + state["continued_by"]["objective"] + ".")
    if isinstance(state.get(FIELD), dict):
        verdict.update(status="retired", condition="objective_retired", facts={
            "objective": objective, "owner": state[FIELD]["owner"], "runs": list(state[FIELD]["runs"]),
            "release": list(state[FIELD]["released"]), "records": list(state[FIELD]["preserved"])},
            message="The owner's historical retirement already covers this objective.", next_action=ACTION.format(objective))
        return verdict
    map_state = map_of(state)
    if map_state is not None and map_state.get("closure"):
        return fail("objective_closed", "The objective is closed; a closed objective needs no retirement.")
    owner = state.get("owner")
    if not owner:
        return fail("owner_unrecorded", "The objective records no coordinator.")
    verdict["facts"]["owner"] = owner
    try:
        if unfinished(state) is not None:
            return fail("handoff_pending", "The objective has an unfinished owner handoff.")
        refs = sorted(_run_references(state))
        recorded = _paths(state)
        records = _governor_rows(project, objective)
    except PodError as exc:
        return fail("scope_unreadable", f"The objective's record or Governor journal is unreadable ({exc.code}).")
    for path in release:
        if path not in recorded:
            return fail("path_not_recorded", "A path to release is not one of the objective's recorded worktrees.",
                        "Name only worktrees pod status --list records for this objective.")
    if not refs:
        return fail("scope_unreadable", "The objective records no Run whose work could be proven settled.")
    for key in sorted(state["admissions"]):
        row = state["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        records.append({"kind": "admission", "id": key, "role": row["role"], "state": row["state"],
                        "dispatch": (row.get("native_binding") or {}).get("dispatchId")})
    if len(records) > MAX_RECORDS:
        return fail("scope_unreadable", f"More than {MAX_RECORDS} preserved records cannot be bound to one decision.")
    verdict["facts"]["records"] = records
    previous = terminal_identity(owner)
    if previous["state"] == "live":
        return fail("owner_not_lost", "Orca reports the objective's coordinator live; retirement never takes "
                    "over live work.", "Continue in the coordinator's own terminal, or close it first.")
    if previous["state"] != "lost" and not previous.get("exited"):
        return fail("owner_not_lost", "Orca cannot prove the objective's coordinator gone ("
                    + (previous["code"] or previous["state"]) + ").")
    binding = ((state["checkpoint"].get("continuity") or {}).get("binding")) or {}
    runs, dispatches, runtime, settled = [], {}, None, 0
    for run in refs:
        try:
            shown = run_show(run)
            fleet = worker_rows(run)
        except PodError as exc:
            return fail("workers_unverified", f"A Run or its worker list could not be read ({exc.code}).")
        if shown["coordinator_handle"] not in (None, owner):
            return fail("generation_changed", "A recorded Run is now bound to another coordinator.")
        if binding.get("run") == run and binding.get("generation") != shown["consumer_generation"]:
            return fail("generation_changed", "A recorded Run's consumer generation changed.")
        if fleet.get("complete") is not True:
            return fail("workers_unverified", "A Run's worker list is incomplete.")
        runtime = runtime or fleet["runtime"]
        runs.append({"run": run, "generation": shown["consumer_generation"]})
        for row in fleet["workers"]:
            dispatch = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(dispatch, str) or not dispatch:
                return fail("workers_unverified", "A worker has no Dispatch identity.")
            dispatches[dispatch] = None
    verdict["facts"]["runs"] = runs
    for key, row in state["admissions"].items():
        if row["state"] == "bound":
            dispatches[row["native_binding"]["dispatchId"]] = (key, row["native_binding"])
    for dispatch in sorted(dispatches):
        try:
            shown = port.show_worker(dispatch)
        except PodError as exc:
            return fail("workers_unverified", f"A recorded Dispatch could not be read ({exc.code}).")
        result = shown.get("result") if isinstance(shown, dict) else None
        if dispatches[dispatch] is not None:
            key, bound = dispatches[dispatch]
            parts = [result.get(name) if isinstance(result, dict) else None for name in ("dispatch", "projection", "worker")]
            if not all(isinstance(part, dict) for part in parts) or (
                    parts[0].get("id"), parts[0].get("runId"), parts[0].get("taskId"), parts[1].get("id"),
                    parts[2].get("worktreeId")) != (bound["dispatchId"], bound["runId"], bound["taskId"],
                                                    bound["workerId"], bound["worktreeId"]):
                return fail("workers_unverified", f"Admission {key[:12]} has a differing native identity.")
        if not _native_assignment_settled(shown):
            return fail("work_live", "A recorded Dispatch is live or unsettled; retirement waits for it.",
                        "Settle the work through Orca, then detect again.")
        settled += 1
    terminals = 0
    for path in release:
        try:
            listed = read_command(["terminal", "list", "--worktree", "path:" + path, "--json"])
            rows = listed["result"]["terminals"]
            scope = listed["result"].get("hostScope")
            if (not isinstance(rows, list) or listed["result"].get("truncated") is not False
                    or isinstance(scope, dict) and scope.get("omittedHostIds")):
                raise PodError("orca_contract", "terminal list is partial")
        except (PodError, KeyError, TypeError) as exc:
            return fail("path_unverified", "Orca could not list the terminals of a path to release ("
                        + (exc.code if isinstance(exc, PodError) else "orca_contract") + ").")
        for row in rows:
            cause = row.get("exitCause") if isinstance(row, dict) else None
            exited = (isinstance(row, dict) and row.get("connected") is False and row.get("writable") is False
                      and isinstance(cause, dict) and cause.get("kind") in EXIT_KINDS)
            if not exited:
                return fail("path_in_use", "An Orca terminal that is not proven exited is in a path to release.",
                            "Close that terminal, or name only paths no terminal uses.")
            terminals += 1
    obligations = (map_state or {}).get("obligations", [])
    verdict.update(status="eligible", condition="retirable",
                   unfinished=[ob["id"] for ob in obligations if ob["state"] not in TERMINAL][:MAX_LISTED],
                   native={"runtime": runtime or "unknown",
                           "owner": "exited" if previous.get("exited") else (previous["code"] or "lost"),
                           "runs": len(runs), "dispatches": settled, "exited_terminals": terminals},
                   message="The coordinator is gone, every recorded Dispatch is settled and no live terminal "
                           "uses the paths to release.")
    return verdict


def _requested(value: object) -> list[dict]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_OBJECTIVES:
        raise PodError("invalid_request", f"objectives is a list of 1 to {MAX_OBJECTIVES} entries")
    rows = []
    for item in value:
        row = exact(item, REQUEST, REQUEST, name="retirement_objective")
        bounded_text(row["objective"], name="objective", limit=256)
        if (not isinstance(row["release"], list) or not 0 < len(row["release"]) <= MAX_PATHS
                or not all(_path_text(path) for path in row["release"])
                or len(set(row["release"])) != len(row["release"])):
            raise PodError("invalid_request", f"release names 1 to {MAX_PATHS} distinct absolute normalized paths")
        rows.append({"objective": row["objective"], "release": list(row["release"])})
    if len({row["objective"] for row in rows}) != len(rows):
        raise PodError("invalid_request", "Each objective is named once")
    return rows


def _decision(value: object, requested: list[dict], repository: str) -> dict:
    decision = exact(value, DECISION, DECISION, name="retirement_decision")
    bounded_text(decision["instruction"], name="instruction", limit=1024)
    bounded_text(decision["reason"], name="reason", limit=1024)
    mismatch = PodError("retirement_decision_mismatch", "Only the owner's direct instruction for exactly the "
                        "requested objectives and paths permits retirement. Nothing was written.",
                        {"next_action": "detect again without a decision and confirm its facts with the owner"})
    if decision["provenance"] != "user_direct" or decision["repository"] != repository:
        raise mismatch
    blocks = decision["objectives"]
    if not isinstance(blocks, list) or len(blocks) != len(requested):
        raise mismatch
    for block, row in zip(blocks, requested):
        block = exact(block, DECIDED, DECIDED, name="retirement_decision_objective")
        if (not _facts_valid(block) or not _texts(block["delivery"], MAX_DELIVERY, 512) or not block["delivery"]
                or block["objective"] != row["objective"] or block["release"] != row["release"]):
            raise mismatch
    return decision


def _question(eligible: list[dict], tally: dict) -> str:
    names = [row["objective"] for row in eligible]
    paths = sorted({path for row in eligible for path in row["facts"]["release"]})
    held = sum(tally.values())
    return (f"May Pod retire {'objective' if len(names) == 1 else 'objectives'} {', '.join(names)} as delivered "
            f"history whose coordinator is gone, releasing {', '.join(paths)} for cleanup? Retirement does not "
            f"close or complete {'it' if len(names) == 1 else 'them'}: {held} pending, unknown, failed or unreported "
            "record(s) stay exactly as recorded, and the record will accept no further writes.")


def historical_retirement(project: Path, objectives: object, *, decision: object | None = None,
                          port=None) -> dict:
    """Detect without a decision; retire with an exact one. Each objective succeeds or refuses alone."""
    from .github import repository_context
    from .ledger import _lock, _path, _read, _write_retirement, record_name
    from .operations import OrcaPort
    requested = _requested(objectives)
    context = repository_context(project, fresh=True)
    repository = context["repo_key"]
    if repository is None:
        raise PodError("retirement_scope_unavailable", "Historical retirement needs a Git repository",
                       {"next_action": "run it from the repository's canonical checkout"})
    if decision is not None:
        decision = _decision(decision, requested, repository)
    port = port or OrcaPort(project)
    paths = {row["objective"]: _path(project, row["objective"]) for row in requested}
    results = []
    with ExitStack() as stack:
        # Nothing is created for a name that has no record: a lock would make its directory.
        for path in sorted((path for path in paths.values() if path.exists()), key=str):
            stack.enter_context(_lock(path))
        for index, row in enumerate(requested):
            name, path = row["objective"], paths[row["objective"]]
            try:
                state = _read(path) if path.exists() else None
            except PodError as exc:
                verdict = {"objective": name, "status": "refused", "condition": "scope_unreadable",
                           "facts": {"objective": name, "owner": None, "runs": [], "release": row["release"],
                                     "records": []},
                           "message": f"The objective's record is unreadable or unsupported ({exc.code}).",
                           "next_action": "Historical retirement needs a readable current record."}
            else:
                verdict = _detect(project, name, row["release"], state, port=port)
            verdict["record"] = record_name(project, context, name)
            if decision is None:
                results.append(verdict)
                continue
            block = decision["objectives"][index]
            facts = {key: block[key] for key in FACTS}
            if verdict["status"] == "retired":
                if state[FIELD]["decision"] == digest(decision):
                    results.append({**verdict, "status": "replayed",
                                    "message": "This decision's historical retirement is already recorded; "
                                               "nothing was written."})
                else:
                    results.append({**verdict, "status": "refused"})
                continue
            if verdict["status"] != "eligible":
                results.append(verdict)
                continue
            if facts != verdict["facts"]:
                results.append({**verdict, "status": "refused", "condition": "retirement_decision_mismatch",
                                "message": "The decision differs from fresh owner, Run, path or record facts. "
                                           "Nothing was written.",
                                "next_action": "detect again and confirm the changed facts with the owner"})
                continue
            entry = {"at": datetime.now(timezone.utc).isoformat(), "decision": digest(decision),
                     "instruction": decision["instruction"], "reason": decision["reason"],
                     "repository": repository, "objectives": [item["objective"] for item in requested],
                     "owner": facts["owner"], "runs": facts["runs"], "released": facts["release"],
                     "preserved": facts["records"], "unfinished": verdict["unfinished"],
                     "delivery": list(block["delivery"]), "native": verdict["native"]}
            state[FIELD] = entry
            _write_retirement(path, state)
            results.append({**verdict, "status": "retired", "retirement": status_block(state),
                            "message": "Retired: the record is kept, not completed, and accepts no writes.",
                            "next_action": ACTION.format(name)})
    done = [row for row in results if row["status"] in ("retired", "replayed")]
    if decision is None:
        eligible = [row for row in results if row["status"] == "eligible"]
        tally = {"pending": 0, "unknown": 0, "failed": 0, "unreported": 0}
        for row in eligible:
            for key, value in counts(row["facts"]["records"]).items():
                tally[key] += value
        out = {"status": "retirement_available" if eligible else "retirement_unavailable",
               "repository": repository, "objectives": results}
        if eligible:
            out["question"] = _question(eligible, tally)
            out["next_action"] = ("Ask the owner the question. Then rerun with a user_direct decision of exactly "
                                  "{provenance, instruction, repository, reason, objectives}, each objective "
                                  "{objective, owner, runs, release, records, delivery} with these facts and the "
                                  "delivery references.")
        else:
            out["next_action"] = "Historical retirement fits no requested objective now; resolve each named condition."
        return out
    return {"status": "retired" if len(done) == len(results) else "partially_retired" if done else "refused",
            "repository": repository, "objectives": results}

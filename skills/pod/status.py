"""Read-only status projection and human rendering."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .bundle import RELOAD_ACTION, checkpoint_identity, identity_drift_message, identity_label, identity_matches, running_identity
from .config import load as load_config
from .errors import PodError
from .ledger import context_root_for_run, contexts_for_run
from .orca import current_run, worker_rows
from .routes import project as project_routes
from .selection import active_constraints, failure_active
from .term import clean
from .util import route_summary, route_hold, route_next_action, route_settlement_projection

PREFERENCE_KEYS = ("path", "schema", "status", "revision", "eligible", "preferred", "pinned",
                   "max_active", "refresh", "errors", "setup")
# Objective status reads preferences only; observations stay with `pod models`.
NOT_READ = {"status": "not_read", "sources": {}}
QUESTION_LIMIT = 64
QUESTION_BYTES = 32 * 1024
QUESTION_FACT_BYTES = 4096


@dataclass(frozen=True)
class StatusInputs:
    """Common discovery for one bounded read, never objective or native proof.

    A batch checks these inputs again before publishing. Each status still reads
    its own record, Governor, source, exact native assignments and ownership.
    Nothing survives the command or supplies a cached status on a later page.
    """

    project: Path
    preferences: dict
    running: dict
    routes: dict

    @classmethod
    def read(cls, project: Path) -> StatusInputs:
        preferences = load_config(project)
        running = running_identity()
        routes = project_routes(project, preferences=preferences, observations=NOT_READ)["summary"]
        return cls(project.resolve(), preferences, running, routes)

    def identity(self) -> str:
        from .util import digest
        return digest({"project": str(self.project), "preferences": self.preferences,
                       "running": self.running, "routes": self.routes})


def decision_status(preferences: dict, checkpoint: object = None, *, objective: bool = False) -> dict:
    """Join raw config facts with the shared pure visibility projection."""
    from .readiness import decision_view
    if preferences.get("status") == "invalid":
        return decision_view(None)
    return decision_view({"policy": preferences.get("decision_policy"),
                          "permissions": preferences.get("tool_permissions", []),
                          "pending": preferences.get("pending_decisions", []),
                          "generation": preferences.get("decision_generation", 0)},
                         checkpoint, objective=objective)


def public_status(value, *, rule=False):
    """Public boundary only; private helpers retain the complete authority inventory."""
    from .util import public_decisions
    return public_decisions(value, rule=rule)


def recorded_questions(checkpoint: object, *, reason: str = "checkpoint_unavailable", _exclude=()) -> dict:
    """Only recorded facts, including explicit gaps in the earlier opaque question forms."""
    result = {"schema": "pod-recorded-questions/v1", "status": "unavailable",
              "checkpoint": {"seq": None, "revision": None}, "items": [], "total": None,
              "truncated": False, "missing": [], "reason": reason}
    if not isinstance(checkpoint, dict):
        result["missing"] = ["checkpoint"]
        return result
    for field in ("seq", "revision"):
        value = checkpoint.get(field)
        if type(value) is int and value >= 0:
            result["checkpoint"][field] = value
        else:
            result["missing"].append("checkpoint." + field)
    questions = checkpoint.get("questions")
    if not isinstance(questions, list):
        result.update(reason="questions_missing" if "questions" not in checkpoint else "questions_unsupported")
        result["missing"].append("questions")
        return result

    def text(value, limit):
        if isinstance(value, str) and value.strip() and len(value) <= limit:
            try:
                value.encode("utf-8")
                return value
            except UnicodeError:
                pass
        return None

    def fact(value):
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return (json.loads(encoded), True) if len(encoded) <= QUESTION_FACT_BYTES else (None, False)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None, False

    eligible = [(index, raw) for index, raw in enumerate(questions) if index not in _exclude]
    result.update(total=len(eligible), status="complete", reason=None)
    for index, raw in eligible[:QUESTION_LIMIT]:
        source = raw if isinstance(raw, dict) else {"reference": raw, "text": raw} if isinstance(raw, str) else {}
        row = {"index": index, "id": text(source.get("id"), 512), "reference": text(source.get("reference"), 512),
               "text": text(source.get("text", source.get("question")), 4096),
               "choices": None, "resolution": None, "missing": []}
        if type(source.get("owner_only")) is bool:
            row["owner_only"] = source["owner_only"]
        if source.get("recipient") == "repository_lead":
            row["recipient"] = source["recipient"]
        if row["id"] is None and row["reference"] is None:
            row["missing"].append("identity")
        if row["text"] is None:
            row["missing"].append("text")
        choices, valid = fact(source.get("choices"))
        if "choices" in source and valid:
            row["choices"] = choices
        elif "choices" in source or row.get("owner_only") is not True:
            row["missing"].append("choices")
        resolution = {key: source[key] for key in ("resolution", "resolved", "answer", "status") if key in source}
        resolution, valid = fact(resolution)
        if resolution and valid:
            row["resolution"] = resolution
        else:
            row["missing"].append("resolution")
        projected = {**result, "items": [*result["items"], row]}
        if len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) > QUESTION_BYTES - 512:
            break
        result["items"].append(row)
    result["truncated"] = len(result["items"]) < len(eligible)
    if result["truncated"]:
        result["missing"].append("items")
    if result["missing"] or any(row["missing"] for row in result["items"]):
        result.update(status="partial", reason="projection_limit" if result["truncated"] else "question_facts_missing")
    return result


def _question_status(state: dict | None) -> dict:
    """The CE owner feed excludes only proven kernel-owned unanswered lead questions.

    Keep the full recorded projection for decision/attention accounting. No marker
    supplied on an opaque question confers this exception, even a matching id/text.
    """
    from .assurance import REVIEW_ORIGIN, owned_allowance_question
    from .ledger import map_of
    from .obligations import validate_map_record
    checkpoint = (state or {}).get("checkpoint")
    excluded, lead = [], None
    mapped = map_of(state)
    if mapped is not None and state.get("question_channels") == 1:
        try:
            validate_map_record(mapped)
            questions = checkpoint.get("questions")
            if isinstance(questions, list):
                excluded = [index for index, row in enumerate(questions)
                            if isinstance(row, dict) and row.get("resolved") is False
                            and owned_allowance_question(row, mapped, state)]
                if excluded:
                    lead = {key: value for key, value in questions[excluded[-1]].items() if key != REVIEW_ORIGIN}
        except (PodError, KeyError, TypeError, ValueError):
            pass  # Unproved provenance never removes an owner question.
    return {"recorded_questions": recorded_questions(checkpoint, _exclude=excluded),
            **({"review_allowance_question": lead} if lead is not None else {})}


def _attention(native: dict | None) -> dict | str:
    """Orca's own attention for one exact Dispatch; absent means unknown, never "not waiting"."""
    projection = native.get("projection") if isinstance(native, dict) else None
    attention = projection.get("attention") if isinstance(projection, dict) else None
    if not isinstance(attention, dict) or not isinstance(attention.get("categories"), list):
        return "unknown"
    return {"categories": [str(item) for item in attention["categories"]][:8],
            "requires_action": attention.get("requiresAction") is True}


def _attention_note(attention: dict | str) -> str:
    if attention == "unknown":
        return "; attention unknown"
    categories = attention["categories"]
    if {"input", "approval"} & set(categories):
        return f"; needs a human answer (Orca attention: {', '.join(categories)})"
    if "guidance" in categories:
        return "; needs the coordinator's guidance"
    return f"; needs attention: {', '.join(categories)}" if attention["requires_action"] else ""


def _orca_attention_label(attention: dict | str) -> str:
    """Orca's own attention for a row Pod cannot verify, labelled as Orca's rather than Pod's."""
    if attention == "unknown":
        return "Orca attention unknown"
    label = "Orca attention: " + (", ".join(clean(item)[:64] for item in attention["categories"]) or "none")
    return label + ("; Orca requires action" if attention["requires_action"] else "")


def settlement_failure(state: dict, view: dict | None, workers: dict) -> dict | None:
    """Name a failed exact native read: its code and the bound and current runtimes, or unknown."""
    if not isinstance(view, dict) or view.get("settlement") != "unverified":
        return None
    refs = (state.get("checkpoint") or {}).get("native_refs")
    runtimes = {row.get("runtime") for row in refs if isinstance(row, dict)} if isinstance(refs, list) else set()
    bound = next(iter(runtimes)) if len(runtimes) == 1 and isinstance(next(iter(runtimes)), str) else "unknown"
    current = workers.get("runtime")
    return {"code": view.get("settlement_failure") or "unknown", "bound_runtime": bound,
            "current_runtime": current if isinstance(current, str) and current else "unknown"}


def _objective_details(state: dict, view: dict | None, workers: dict, failure: dict | None = None) -> dict:
    checkpoint = state["checkpoint"]
    map_state = view["map"] if isinstance(view, dict) else None
    outstanding = (set((view.get("ctx") or {}).get("outstanding", [])) if isinstance(view, dict)
                   else {key for key, row in state["admissions"].items()
                         if row["state"] not in ("closed", "deferred")})
    active, settled, retained, unverified = [], [], [], []
    native_rows = {row.get("dispatchId"): row for row in workers.get("workers", [])
                   if isinstance(row, dict) and isinstance(row.get("dispatchId"), str)}
    closure = map_state.get("closure") if isinstance(map_state, dict) else None
    continued = state.get("continued_by") if isinstance(state.get("continued_by"), dict) else None
    retirement = state.get("historical_retirement") if isinstance(state.get("historical_retirement"), dict) else None
    preserved = []
    for key, row in state["admissions"].items():
        binding = row.get("native_binding") or {}
        item = {"admission": key, "role": row["role"], "serves": list(row["serves"]),
                "route": row["route_decision"], "dispatch": binding.get("dispatchId"),
                "state": row["state"]}
        if retirement is not None and row["state"] not in ("closed", "deferred"):
            # Retirement read every recorded Dispatch settled; a start without one keeps its unknown
            # outcome, never settled, unverified or successful (R112).
            if row["state"] == "bound":
                settled.append({**item, "settlement": "retirement", "retired_at": retirement["at"]})
            else:
                preserved.append({**item, "outcome": "unknown", "retired_at": retirement["at"]})
            continue
        if failure is not None and closure is not None and row["state"] not in ("closed", "deferred"):
            # Closure was accepted only with no outstanding assignment, judged from an exact read,
            # so its record proves these settled even when today's read cannot repeat it.
            settled.append({**item, "settlement": "closure", "closure_seq": closure.get("seq")})
            continue
        if continued is not None and row["state"] not in ("closed", "deferred"):
            # The claim read every bound Dispatch settled, and a reserved row without a Dispatch
            # was named in the owner's decision; the record accepts no later write (R110).
            settled.append({**item, "settlement": "claim" if row["state"] == "bound" else "claim_no_dispatch",
                            "claimed_at": continued["at"],
                            **({} if row["state"] == "bound" else
                               {"basis": "no Dispatch was found at the claim; the owner's decision accepted it"})})
            continue
        if failure is not None and row["state"] not in ("closed", "deferred"):
            # Presentation only: without exact native evidence an open row is neither active nor settled.
            item["orca_attention"] = _attention(native_rows.get(binding.get("dispatchId")))
            unverified.append(item)
            continue
        if key in outstanding:
            item["attention"] = _attention(native_rows.get(binding.get("dispatchId")))
        (active if key in outstanding else settled).append(item)
        facts = (view or {}).get("placements", {}).get(key, {}) if key not in outstanding else {}
        liveness = facts.get("native_liveness") or {}
        resource = facts.get("native_release") or {}
        owned = facts.get("terminal_ownership") or {}
        terminal_state = facts.get("native_terminal_state") or resource.get("state")
        if (liveness.get("verdict") == "live" and terminal_state != "released"
                and facts.get("terminal") == binding.get("terminalHandle")
                and binding.get("terminalHandle") is not None
                and (not owned or owned.get("ownerDispatchId") == binding["dispatchId"])):
            retained.append({"admission": key, "dispatch": binding["dispatchId"],
                             "terminal": binding["terminalHandle"], "state": terminal_state,
                             "reason": facts.get("retained_reason")})
        elif not any(facts.get(field) for field in ("native_liveness", "native_release", "native_terminal_state", "terminal_ownership")):
            # Older exact receipts omit resource facts; preserve their verified list readback.
            native = native_rows.get(binding.get("dispatchId")) if key not in outstanding else None
            projection = native.get("projection") if isinstance(native, dict) else None
            live = projection.get("liveness") if isinstance(projection, dict) else None
            if (isinstance(live, dict) and live.get("verdict") == "live"
                    and native.get("agentTerminalHandle") == binding.get("terminalHandle")
                    and binding.get("terminalHandle") is not None and native.get("terminalState") != "released"):
                retained.append({"admission": key, "dispatch": binding["dispatchId"],
                                 "terminal": binding["terminalHandle"], "state": native.get("terminalState")})
    obligations = map_state.get("obligations", []) if isinstance(map_state, dict) else []
    external = [{"obligation": ob["id"], **ob["external"]} for ob in obligations
                if ob["state"] == "blocked_external"]
    authority = [{"obligation": ob["id"], "referent": ob["wait"]["referent"]}
                 for ob in obligations if ob["state"] == "waiting" and ob["wait"]["class"] == "authority"]
    remaining = list(checkpoint.get("remaining_gates", []))
    progress = []
    by_admission = {row["admission"]: row for row in active}
    by_unverified = {row["admission"]: row for row in unverified}
    for ob in obligations:
        if ob["state"] == "active" and ob["executor"] == "coordinator":
            progress.append(f"coordinator: {ob['id']} ({ob.get('check') or ob.get('resolves') or 'current work'})")
        elif ob["state"] == "active" and ob["executor"] in by_unverified:
            attempt = by_unverified[ob["executor"]]
            progress.append(f"settlement unverified for {attempt['role']} (dispatch "
                            f"{attempt['dispatch'] or 'pending'}); "
                            + _orca_attention_label(attempt["orca_attention"]))
        elif ob["state"] == "active":
            attempt = by_admission.get(ob["executor"])
            if attempt:
                dispatch = attempt["dispatch"] or "pending"
                progress.append(f"waiting for {attempt['role']} delivery (dispatch {dispatch})"
                                + _attention_note(attempt["attention"]))
        elif ob["state"] == "blocked_external":
            progress.append(f"waiting for {ob['external']['party']}: {ob['external']['need']}")
    for gate in remaining:
        if isinstance(gate, dict) and gate.get("kind") == "hosted_ci":
            progress.append(f"waiting for hosted CI ({gate.get('workflow', 'workflow')} on "
                            f"{str(checkpoint.get('candidate') or 'unknown')[:12]})")
        else:
            progress.append("remaining gate: " + str(gate)[:160])
    if retirement is not None:
        next_action = "historical retirement by the owner: not completed, and its record accepts no writes"
    elif authority:
        next_action = "obtain authority for " + authority[0]["obligation"]
    elif failure is not None and unverified:
        next_action = (f"native settlement is unverified ({failure['code']}; bound runtime "
                       f"{failure['bound_runtime']}, current runtime {failure['current_runtime']}): "
                       f"{len(unverified)} open admission(s) stay unverified until an exact native read succeeds")
    elif active:
        next_action = "wait for the exact outstanding assignment result"
    elif remaining:
        next_action = "complete the next recorded gate"
    elif map_state and map_state.get("closure"):
        next_action = "objective closed"
    elif progress:
        next_action = progress[0]
    else:
        next_action = "read the objective map and choose the next ready obligation"
    assignments = {"active": active, "settled": settled}
    if failure is not None:
        assignments["unverified"] = unverified
    if retirement is not None:
        assignments["preserved"] = preserved
    return {"assignments": assignments,
            "retained_terminals": retained,
            "gates": {"remaining": remaining, "blocked_external": external, "authority_waits": authority},
            "progress": progress, "recorded_next_action": checkpoint.get("next_safe_action"),
            "next_action": next_action}


def status(project: Path, run: str | None, *, objective: str | None = None, current_run_fn=current_run,
           worker_rows_fn=worker_rows, read_context: StatusInputs | None = None) -> dict:
    inputs = StatusInputs.read(project) if read_context is None else read_context
    if inputs.project != project.resolve():
        raise PodError("status_context_mismatch", "Shared status discovery belongs to another project")
    preferences, running = deepcopy(inputs.preferences), dict(inputs.running)
    result = {"schema": "pod-cli/v5", "status": "selection_required", "run": run,
              **decision_status(preferences),
              "bundle_identity": {"running": running, "checkpoint": None, "drift": None},
              "preferences": {key: preferences[key] for key in PREFERENCE_KEYS},
              "routes": deepcopy(inputs.routes),
              "constraints": [], "route_decisions": [], "route_mismatch": False,
              "active_constraints": [],
              "effective_unknown": False,
              "installed_version_drift": False, "blocker": None,
              "next_safe_action": "select a native Run", "usage": "unknown", "cost": "unknown"}
    if run is None and objective is None:
        try:
            current = current_run_fn().get("run")
            run = current.get("id") if isinstance(current, dict) else None
        except PodError as exc:
            result.update({"status": "unavailable", "blocker": exc.code})
            if exc.detail:
                result["detail"] = exc.detail
            if (exc.detail or {}).get("native_code") == "no_active_sender_terminal":
                result["next_safe_action"] = "This process is not in a live Orca terminal; Pod records need one. Observe read-only with pod status --objective ID"
            return result
        if run is None:
            return result
    from .ledger import _read, kernel_view, map_of, read, superseded_for_run
    from .readiness import assess, unavailable
    record_root = None
    try:
        if objective is None:
            matches = contexts_for_run(run)
            if len(matches) > 1:
                result.update({"run": run, "choices": sorted([
                    {"objective": (row.get("checkpoint") or {}).get("objective"),
                     "worktree": ((row.get("checkpoint") or {}).get("worktree") or {}).get("path"),
                     "seq": (row.get("checkpoint") or {}).get("seq"),
                     "state": "closed" if (row.get("checkpoint") or {}).get("closure") else "open"}
                    for _, row in matches], key=lambda row: str(row["objective"])),
                    "next_safe_action": "rerun with --objective ID"})
                return result
            context_root = record_root = matches[0][0] if matches else None
            state = matches[0][1] if matches else None
            from .evidence_closure import status_fields, tail_closed
            name = ((state or {}).get("checkpoint") or {}).get("objective")
            if record_root is not None and isinstance(name, str) and tail_closed(record_root):
                # Closed by evidence: read from its closure record, never written or converted (R145, R146).
                result.update(run=run, **status_fields(project, name, record_root))
                return result
            superseded = superseded_for_run(run) if context_root is None else []
        else:
            from .evidence_closure import older_predecessor_fields, status_fields, tail_closed
            from .ledger import objective_root
            closed = objective_root(project, objective)
            if tail_closed(closed):
                # Any reader, current or older record: the closure record and documented fields only (C4).
                result.update(status_fields(project, objective, closed))
                return result
            continued = older_predecessor_fields(project, objective, closed)
            if continued is not None:
                # An older predecessor whose chain closed by evidence reads continued through the same read-only
                # path; a current one reads below as before (R146).
                result.update(continued)
                return result
            state = read(project, objective)
            result.update(_question_status(state))
            result.update(decision_status(preferences, state.get("checkpoint") if state else None, objective=True))
            if state is None or not isinstance(state.get("checkpoint"), dict):
                from .ledger import state_root
                result.update({"status": "blocked", "blocker": "objective_unknown",
                               "next_safe_action": "name a recorded objective in this project",
                               "objective": objective, "repository_context": str(project),
                               "state_root_searched": str(state_root(project)),
                               "readiness": unavailable("objective_unknown", "this objective has no readable "
                                                        "record in this repository")})
                return result
            from .ledger import objective_root
            record_root = objective_root(project, objective)
            refs = {row.get("runId") for row in state["checkpoint"].get("native_refs", [])
                    if isinstance(row, dict)}
            refs.update(row.get("run_id") for row in state["admissions"].values())
            if run is not None and run not in refs:
                result.update({"status": "blocked", "blocker": "objective_run_mismatch",
                               "next_safe_action": "select a Run recorded by this objective",
                               "readiness": unavailable("objective_run_mismatch", "the named Run is not "
                                                        "recorded by this objective")})
                return result
            if run is None:
                try:
                    current = current_run_fn().get("run")
                    current_id = current.get("id") if isinstance(current, dict) else None
                except PodError as exc:
                    current_id = None
                    if exc.detail:
                        result["detail"] = exc.detail
                run = current_id if current_id in refs else next(iter(refs)) if len(refs) == 1 else None
            superseded = []
    except PodError as exc:
        result.update({"status": "blocked", "blocker": exc.code,
                       "next_safe_action": (((exc.detail or {}).get("next_action") or "Project lead or coordinator: run internal record-compaction "
                                             "detection for this exact project and objective, then submit its exact scoped "
                                             "maintenance request; no owner decision is required.")
                                            if exc.code == "record_too_large"
                                            else "inspect the objective state")})
        if objective is not None:
            result["readiness"] = unavailable(exc.code, f"the objective record cannot be read ({exc.code})")
            result["recorded_questions"] = recorded_questions(None, reason=exc.code)
            result.update(decision_status(preferences, None, objective=True))
        if exc.code == "record_too_large":
            from .history import CONTEXT_LIMIT, WARN_AT, COMPACT_AT
            result["record_capacity"] = {"status": "unavailable", "physical_bytes": (exc.detail or {}).get("current_bytes"),
                                         "logical_bytes": None, "limit_bytes": CONTEXT_LIMIT,
                                         "warning_bytes": WARN_AT, "compaction_bytes": COMPACT_AT, "warning": True,
                                         "next_action": result["next_safe_action"]}
        return result
    result["run"] = run
    native_error = None
    if run is not None:
        try:
            workers = worker_rows_fn(run)
        except PodError as exc:
            native_error = exc.code
            if exc.detail:
                result["detail"] = exc.detail
            workers = {"workers": [], "scope": None, "complete": False}
    else:
        native_error = "run_unavailable"
        workers = {"workers": [], "scope": None, "complete": False}
    if superseded:
        result.update({"status": "blocked", "blocker": "objective_superseded", "superseded": superseded,
                       "next_safe_action": ("settle its workers through Orca and start a new objective; "
                                            "the record is not converted")})
        return result
    checkpoint = state.get("checkpoint") if state else None
    if state is not None:
        result.update(_question_status(state))
        result.update(decision_status(preferences, checkpoint, objective=True))
        from .standing import projection as standing_projection
        result["standing_authorization"] = standing_projection(state)
    recorded = checkpoint_identity(checkpoint) if checkpoint else None
    drift = bool(checkpoint and not identity_matches(recorded, running))
    admissions = list(state["admissions"].values()) if state else []
    mismatch, unknown = route_summary(admissions)
    route_holds = [row for row in admissions if route_hold(row)]
    checkpoint_refs = checkpoint.get("native_refs") if isinstance(checkpoint, dict) else None
    checkpoint_assignments = checkpoint.get("assignments") if isinstance(checkpoint, dict) else None
    checkpoint_refs = checkpoint_refs if isinstance(checkpoint_refs, list) else []
    checkpoint_assignments = checkpoint_assignments if isinstance(checkpoint_assignments, list) else []
    native_references = []
    for row in admissions[:64]:
        binding = row.get("native_binding") if isinstance(row.get("native_binding"), dict) else {}
        native_references.append({"admission_id": row["admission_id"], "run": row["run_id"],
                                  "task": row["task_id"], "dispatch": binding.get("dispatchId"),
                                  "worker": binding.get("workerId"),
                                  "terminal": binding.get("terminalHandle"),
                                  "worktree": binding.get("worktreeId"),
                                  "tab": None, "placement_readback": "unavailable",
                                  "surfaces": [], "warnings": [],
                                  "ui_visibility": "unverified", "ui_focus": "unverified",
                                  "state": row["state"], "runtime": row["runtime"]})
    result.update({"status": "unavailable" if native_error else "observed",
                   "objective": checkpoint.get("objective") if checkpoint else None,
                   "source": checkpoint.get("objective_source", "direct_objective") if checkpoint else None,
                   "selected_worktree": checkpoint.get("worktree") if checkpoint else None,
                   "constraints": state["constraints"] if state else [],
                   "route_failures": [{"admission": key, **failure, "active": failure_active(failure)}
                                      for key, row in (state or {}).get("admissions", {}).items()
                                      for failure in row["failures"]],
                   "active_constraints": active_constraints(state["constraints"], preferences) if state else [],
                   "route_decisions": [row["route_decision"] for row in admissions[:64]],
                   "route_holds": [row["admission_id"] for row in route_holds[:64]],
                   "route_holds_truncated": len(route_holds) > 64,
                   **route_settlement_projection(admissions),
                   "native_references": native_references,
                   "native_references_truncated": len(admissions) > 64,
                   "checkpoint_join": {"native_refs": checkpoint_refs[:64],
                                       "assignments": checkpoint_assignments[:64],
                                       "plan_revision": checkpoint.get("plan_revision"),
                                       "candidate": checkpoint.get("candidate")}
                                      if checkpoint else None,
                   "route_mismatch": mismatch,
                   "effective_unknown": unknown,
                   "installed_version_drift": drift,
                   "bundle_identity": {"checkpoint": recorded, "running": running, "drift": drift},
                   "installed_version_detail": identity_drift_message(checkpoint, running=running) if drift else None,
                   "native": {"worker_count": len(workers["workers"]), "scope": workers.get("scope"),
                              "complete": workers.get("complete")},
                   "pending_admissions": sum(row["state"] in ("reserved", "unresolved") for row in admissions),
                   "verification_gaps": checkpoint.get("verification_gaps", []) if checkpoint else [],
                   "remaining_gates": checkpoint.get("remaining_gates", []) if checkpoint else [],
                   "delivery": checkpoint.get("delivery") if checkpoint else None,
                   "blocker": "route_mismatch" if mismatch
                              else "effective_unknown" if unknown
                              else native_error or checkpoint.get("blocker") if checkpoint else native_error,
                   "next_safe_action": (" ".join(route_next_action(row) for row in route_holds[:64]) if mismatch else
                                        checkpoint.get("next_safe_action", "inspect native Run")
                                        if checkpoint else "inspect native Run")})
    objective = checkpoint.get("objective") if checkpoint else None
    view = None
    if isinstance(objective, str) and checkpoint.get("obligations") is not None:
        try:
            view = kernel_view(project_for_state(checkpoint, project), objective, state=state)
            result["obligations"] = view["status"]["map"]
            result["obligation_lines"] = view["status"]["lines"]
            result["native_settlement"] = view["settlement"]
            for ref in native_references:
                ref.update(view.get("placements", {}).get(ref["admission_id"], {}))
            result["report"] = view["report"]
            if checkpoint.get("closure"):
                result["blocker"] = result["blocker"] or "objective_closed"
                if checkpoint["closure"].get("preserved"):
                    # Results the closure preserved UNKNOWN, as recorded (C11).
                    result["closure_preserved"] = [dict(row) for row in checkpoint["closure"]["preserved"]]
            elif (checkpoint.get("quiescence") or {}).get("state") == "quiescent":
                result["blocker"] = result["blocker"] or "quiescent"
                result["next_safe_action"] = checkpoint["quiescence"]["interim_report"]
        except PodError as exc:
            result["obligations"] = {"error": exc.code}
            # Recorded judgements stay readable when the map cannot be projected (R109).
            recorded = {ob.get("id"): ob["acceptances"]
                        for ob in checkpoint.get("obligations") or []
                        if isinstance(ob, dict) and isinstance(ob.get("acceptances"), list)}
            if recorded:
                result["obligations"]["acceptances"] = recorded
    continuity = checkpoint.get("continuity") if isinstance(checkpoint, dict) else None
    if isinstance(continuity, dict) and continuity.get("history"):
        # The objective's runtime rebind history only, read-only; status never rebinds (A2).
        from .util import decision_channel
        result["runtime_continuity"] = {"history": [
            {**row, "decision": decision_channel(row["decision"])} if isinstance(row.get("decision"), dict) else dict(row)
            for row in continuity["history"]]}
    if state is not None:
        from .util import context_decisions, map_decisions, derived_governance_summary
        from .governor import _read_journal, _record_path
        try:
            journal = _read_journal(record_root / "governor.json") if record_root is not None else None
        except PodError:
            journal = None
        from .standing import projection as standing_projection
        result["standing_authorization"] = standing_projection(state, journal)
        decisions = context_decisions(state, journal) + map_decisions(checkpoint or {})
        if decisions:
            result["owner_decisions"] = decisions
        derived = derived_governance_summary(checkpoint or {})
        if derived:
            result["derived_governance_decisions"] = derived
    if state is not None and checkpoint is not None:
        failure = settlement_failure(state, view, workers)
        if failure is not None:
            result["native_settlement_failure"] = failure
        result.update(_objective_details(state, view, workers, failure))
        from .worker_tabs import rows as tab_rows
        result["worker_tabs"] = ((view or {}).get("worker_tabs") if view is not None else None)
        if result["worker_tabs"] is None:
            result["worker_tabs"] = tab_rows(state, {}, failure=(failure or {}).get("code") or "worker_terminal_unverified")
        from .pod_ticks import projection as tick_projection
        result["pod_ticks"] = ((view or {}).get("pod_ticks") if view else None)
        if result["pod_ticks"] is None:
            tick_error = result.get("obligations", {}).get("error")
            result["pod_ticks"] = ({"status": "unavailable", "error": tick_error, "lines": []}
                                   if tick_error and checkpoint.get("objective_source") else
                                   tick_projection(state, map_of(state), {}, project=project, objective=objective))
    if result["installed_version_drift"]:
        result["blocker"] = "installed_version_changed"
        result["next_safe_action"] = RELOAD_ACTION
    elif preferences["errors"] and result["blocker"] is None:
        result["blocker"] = "route_setup_required" if preferences["setup"] else "preferences_unavailable"
        result["next_safe_action"] = (preferences["setup"]["action"] if preferences["setup"]
                                      else "inspect and correct the personal preference file")
    if result.get("blocker") in ("installed_version_changed", "route_mismatch", "effective_unknown",
                                 "preferences_unavailable", "route_setup_required"):
        result["next_action"] = result["next_safe_action"]
    elif native_error is not None and "native_settlement_failure" not in result:
        result["next_action"] = "inspect the native Run before deciding worker state"
    if (result.get("detail") or {}).get("native_code") == "no_active_sender_terminal":
        result["next_safe_action"] = "This process is not in a live Orca terminal; Pod records need one. Observe read-only with pod status --objective ID"
        result["next_action"] = result["next_safe_action"]
    handoff = None
    if state is not None and checkpoint is not None:
        from .handoff import nonowner_detection
        handoff = nonowner_detection(project, objective, state, state["owner"])
        if handoff is not None:
            result.update(owner_handoff=handoff, blocker=handoff["status"],
                          next_safe_action=handoff["next_action"], next_action=handoff["next_action"])
            # A coordinator lost to a verified owner-authorized Orca window continues with derived authority and no
            # owner decision; the owner handoff block stays unchanged beside it (pod#110).
            from .window import primary_action, status_block as window_block
            window = window_block(project, objective, state)
            if window is not None:
                # The window step, or its lead task, comes first; owner handoff stays optional (pod#110).
                result["window_continuation"] = window
                action = primary_action(window)
                result.update(next_safe_action=action, next_action=action,
                              **({"blocker": "window_available"} if window["status"] == "window_available" else {}))
    if state is not None:
        from .retirement import ACTION as RETIREMENT, status_block as retirement_block
        retirement = retirement_block(state)
        if retirement is not None:
            # Under historical retirement a record is read, never written; it is not closed, continued or
            # successful (R112).
            # Other objectives keep their exact earlier projection, without a phase field.
            result.update(phase="retired", retirement=retirement, blocker="objective_retired",
                          next_safe_action=RETIREMENT.format(objective), next_action=RETIREMENT.format(objective))
        from .succession import ACTION, reserved_for, status_block
        block = status_block(project, state)
        if block is not None:
            result["succession"] = block
        if block is not None and "continued_by" in block:
            # A continued record is read, never written: its successor holds the remaining work.
            result.update(blocker="objective_continued", next_safe_action=ACTION.format(block["continued_by"]),
                          next_action=ACTION.format(block["continued_by"]))
        elif block is not None and block.get("continues") and (not _claim_recorded(project, state)
                                                               or reserved_for(state) is not None):
            result["succession"]["state"] = "claim_incomplete"
            # Completing the claim needs coordinator authority. A window block appears only to a reader that is not
            # the recorded coordinator, so its step, or its lead task when unavailable, stays first; once authority
            # has moved, completing the original claim comes next (pod#110).
            if not result.get("window_continuation"):
                result.update(next_safe_action="rerun the same internal succession claim to complete it",
                              next_action="rerun the same internal succession claim to complete it")
        elif block is not None:
            result["succession"]["state"] = "claimed"
        if record_root is not None:
            # A continued predecessor closed with its chain keeps reading continued, with its own entry (C4).
            from .evidence_closure import block as closure_block
            entry = closure_block(record_root)
            if entry is not None:
                result["evidence_closure"] = entry
        if mismatch and ((checkpoint or {}).get("closure") or state.get("continued_by")
                         or state.get("historical_retirement") or result.get("evidence_closure")):
            action = " ".join(route_next_action(row, ended=True) for row in route_holds[:64])
            result.update(next_safe_action=action, next_action=action)
    if state is not None and checkpoint is not None and isinstance(objective, str) and record_root is not None:
        # New fields only: every earlier field above keeps its exact value (R114).
        error = result["obligations"].get("error") if isinstance(result.get("obligations"), dict) else None
        result.update(assess(project, objective, state, record_root, view=view, map_error=error,
                             failure=result.get("native_settlement_failure"), owner_reader=handoff is None,
                             worker_tabs=result.get("worker_tabs")))
        recovery = (result.get("readiness") or {}).get("recovery") or {}
        if recovery.get("route") == "evidence_closure":
            # A delivered objective of a lost coordinator closes by evidence; nothing here asks the owner, and an
            # owner handoff stays optional (R145, R147).
            from .evidence_closure import SWEEP_ACTION
            action = SWEEP_ACTION[0].upper() + SWEEP_ACTION[1:]
            if isinstance(result.get("owner_handoff"), dict):
                result["owner_handoff"] = {**{key: value for key, value in result["owner_handoff"].items()
                                              if key != "question"},
                                           "next_action": action + "; a handoff stays optional and moves authority only."}
            result.update(next_safe_action=action, next_action=action)
        elif result.get("window_continuation"):
            pass  # The window route above, or its lead task, is the next action; nothing asks the owner.
        elif (result.get("readiness") or {}).get("state") == "coordinator_lost":
            # Lost and not proved delivered: a task for the project lead, never a question for the owner (R145).
            # The owner_handoff block keeps its R105 facts unchanged: handoff and successor claim stay optional
            # recovery routes the lead may take, each with its own recorded decision.
            evidence = next((row for row in recovery.get("routes") or [] if row.get("route") == "evidence_closure"),
                            None)
            code = (evidence or {}).get("condition") or "unverified"
            action = (f"Coordinator lost; delivery not proved ({code}): a task for the project lead. Resolve the "
                      "named conditions, then pod sweep closes it by evidence; continuing the work through a successor "
                      "claim or an owner handoff stays optional, each with its own recorded decision; never run "
                      "orca orchestration run-use by hand.")
            result.update(next_safe_action=action, next_action=action)
    if state is not None and record_root is not None:
        from .history import capacity
        try:
            result["record_capacity"] = capacity(record_root / "context.json", state)
        except PodError as exc:
            result["record_capacity"] = {"status": "unavailable", "code": exc.code}
            result["readiness"] = unavailable(exc.code, f"the objective history cannot be read ({exc.code})")
    if "readiness" in result:
        from .readiness import decision_readiness
        result["readiness"] = decision_readiness(result["readiness"], {
            key: result[key] for key in ("decision_policy", "tool_permissions", "waiting_decisions", "needs_you",
                                        "setup_question")}, worker_tabs=result.get("worker_tabs"))
    return public_status(result)


def _claim_recorded(project: Path, state: dict) -> bool:
    """The predecessor names this objective as its successor."""
    from .ledger import read
    objective = (state.get("checkpoint") or {}).get("objective")
    try:
        prior = read(project, state["continues"]["objective"])
    except PodError:
        return False
    return ((prior or {}).get("continued_by") or {}).get("objective") == objective


def project_for_state(checkpoint: dict, project: Path) -> Path:
    """The objective's bound worktree when recorded, else the current directory."""
    worktree = checkpoint.get("worktree") if isinstance(checkpoint, dict) else None
    path = worktree.get("path") if isinstance(worktree, dict) else None
    return Path(path) if isinstance(path, str) and Path(path).is_dir() else project


def render(result: dict) -> None:
        result = public_status(result)
        if result.get("closure_basis") == "evidence" or (result.get("record_class") == "older"
                                                         and result.get("evidence_closure")):
            from .evidence_closure import render as render_closed
            render_closed(result)
            return
        if isinstance(result.get("readiness"), dict):
            print(clean(result["readiness"]["summary"]))
        capacity = result.get("record_capacity") or {}
        if capacity.get("warning"):
            print(f"Record capacity: {capacity['physical_bytes']}/{capacity['limit_bytes']} bytes. "
                  + clean(capacity["next_action"]))
        print(f"Pod status: {result['status']}; objective {result.get('objective') or 'unknown'}")
        decisions = result.get("decision_policy", {})
        if decisions.get("status") == "available":
            print(f"Decision policy: {decisions['preset']}; setup {decisions['setup']}")
            if result.get("setup_question"):
                print("Decision setup offered: " + clean(result["setup_question"]["text"]))
            for permission in result.get("tool_permissions") or []:
                print(f"Tool permission: {clean(permission['tool'])} | {permission['category']} | {permission['mode']}")
        elif decisions:
            print("Decision policy: unavailable")
        waits = result.get("waiting_decisions", {})
        for wait in waits.get("items", []):
            print(f"Decision wait: {wait['waiting_on']}; requires {wait['required'] or 'unknown'}")
        if waits.get("status") in ("partial", "unavailable"):
            print("Decision waits: " + waits["status"] + "; some facts are unknown")
        if result.get("needs_you") is True:
            print("Needs you: a recorded decision needs your answer")
        if result.get("review_allowance_question"):
            print("Repository lead: " + clean(result["review_allowance_question"]["text"]))
        if result.get("route_settlement_authority"):
            print("Route settlement: " + clean(result["route_settlement_authority"]))
        if "standing_authorization" in result:
            from .standing import mode_line
            print(clean(mode_line(result["standing_authorization"])))
        for choice in result.get("choices", []):
            print(f"  Choose {choice['objective']} | {choice['worktree'] or 'unbound'} | "
                  f"seq {choice['seq']} | {choice['state']}")
        identity = result.get("bundle_identity", {"checkpoint": None})
        if identity["checkpoint"] is not None:
            print(f"Checkpoint bundle: {identity_label(identity['checkpoint'])}; "
                  f"running: {identity_label(identity['running'])}")
        for ref in result.get("native_references", []):
            print(f"  Task {ref['task']} | Dispatch {ref['dispatch'] or 'pending'} | "
                  f"worker {ref['worker'] or 'unknown'} | terminal {ref['terminal'] or 'unavailable'} | "
                  f"tab {ref['tab'] or 'unavailable'} | worktree {ref['worktree'] or 'unverified'} | "
                  f"{ref['state']} | UI visibility/focus unverified")
            if ref.get("native_release") or ref.get("native_terminal_state") or (ref.get("native_stage") or {}).get("dispatch") == "failed":
                print(f"    Orca release: {ref.get('native_release')}; terminal state: {ref.get('native_terminal_state')}; Dispatch stage: {ref.get('native_stage')}; retained reason: {ref.get('retained_reason') or 'unknown'}")
            for surface in ref['surfaces']:
                print(f"    Orca placement surface: {clean(surface)}")
            for warning in ref['warnings']:
                print(f"    Orca start-time placement warning: {clean(warning)}")
        if result.get("state_root_searched"):
            print(f"Searched objective {result['objective']} in repository context {result['repository_context']}; state root {result['state_root_searched']}")
        for line in result.get("obligation_lines", []):
            print(line)
        if result.get("assignments"):
            assignments = result["assignments"]
            print(f"Assignments: {len(assignments['active'])} active, {len(assignments['settled'])} settled"
                  + (f", {len(assignments['unverified'])} unverified" if "unverified" in assignments else ""))
            failure = result.get("native_settlement_failure")
            by_closure = [item for item in assignments["settled"] if item.get("settlement") in
                          ("closure", "claim", "claim_no_dispatch")]
            unproven = [item for item in assignments["settled"] if item.get("settlement") == "claim_no_dispatch"]
            if unproven:
                print(f"Claimed without a Dispatch: {len(unproven)} reserved assignment(s) the owner accepted "
                      "at the claim; no native settlement proves them")
            if failure and assignments.get("unverified"):
                print(f"Native settlement unverified: {failure['code']}; bound runtime "
                      f"{failure['bound_runtime']}, current runtime {failure['current_runtime']}")
            elif failure and by_closure:
                print(f"Settled by {'closure' if by_closure[0]['settlement'] == 'closure' else 'the successor claim'}: "
                      f"{len(by_closure)} assignment(s); the exact native read is "
                      f"unavailable ({failure['code']}; bound runtime {failure['bound_runtime']}, "
                      f"current runtime {failure['current_runtime']})")
            for item in assignments.get("unverified", []):
                print(f"  Unverified {item['role']} (dispatch {item['dispatch'] or 'pending'}): "
                      + _orca_attention_label(item["orca_attention"]))
        retirement = result.get("retirement")
        if isinstance(retirement, dict):
            tally = retirement["counts"]
            print(f"Historical retirement by the owner at {clean(retirement['retired_at'])}: not completed or closed; released "
                  + ", ".join(clean(path) for path in retirement["released"])
                  + f"; preserved {tally['pending']} pending, {tally['unknown']} unknown, {tally['failed']} failed and "
                  f"{tally['unreported']} unreported record(s); {len(retirement['unfinished'])} unfinished "
                  "obligation(s); this record accepts no writes")
            for row in retirement["locations"] or []:
                print(f"  Released {clean(row['path'])}: "
                      + ("absent from this host, Git and Orca when the owner decided" if row["disposition"] == "absent"
                         else "an existing worktree of this repository when the owner decided"))
            for path in retirement["retained"]:
                print(f"  Still held: {clean(path)}, a recorded worktree this retirement did not release")
            if retirement["unrecorded_locations"] == "hold_ends":
                print("  Its record does not list every location its work used; retirement ended cleanup's hold on "
                      "the locations it never listed")
            for reference in retirement["delivery"]:
                print(f"  Delivery reference: {clean(reference)}")
            preserved = result.get("assignments", {}).get("preserved", [])
            if preserved:
                print(f"Preserved without settlement: {len(preserved)} start(s) whose outcome stays unknown")
        succession = result.get("succession")
        if isinstance(succession, dict) and succession.get("continued_by"):
            print(f"Continued by successor objective {clean(succession['continued_by'])} "
                  f"({succession['state'].replace('_', ' ')}); this record accepts no writes")
        if isinstance(succession, dict) and succession.get("continues"):
            print(f"Continues objective {clean(succession['continues'])}"
                  + ("; the claim is incomplete" if succession.get("state") == "claim_incomplete" else ""))
        handoff = result.get("owner_handoff")
        if isinstance(handoff, dict):
            print(f"Owner handoff: {clean(handoff['condition'])}; {clean(handoff['message'])}")
        window = result.get("window_continuation")
        if isinstance(window, dict):
            print(("Window continuation: available; " if window["status"] == "window_available"
                   else f"Window continuation: unavailable ({clean(window['condition'])}); ")
                  + clean(window["next_action"]))
        claim = result.get("successor_claim")
        if isinstance(claim, dict):
            print("Successor claim: " + ("available; " if claim["status"] == "claim_available"
                                         else f"refused ({clean(claim['condition'])}); ") + clean(claim["message"]))
        for entry in (result.get("runtime_continuity") or {}).get("history", []):
            label = {"owner_handoff": "Owner handoff", "window_continuation": "Window continuation"}.get(
                entry["provenance"], "Runtime rebind")
            print(f"{label}: {clean(entry['from_runtime'])} -> {clean(entry['to_runtime'])} at "
                  f"{clean(entry['at'])} ({entry['provenance']})")
        for row in result.get("owner_decisions", []):
            channel = row["decision"]["channel"] + (" (earlier record)" if row["decision"].get("channel_recorded") is False else "")
            print(f"Decision {clean(row['kind'])}: {clean(channel)} ({clean(row['reference'])})")
        derived = result.get("derived_governance_decisions")
        if derived:
            print(f"Derived governance adoptions ({derived['count']}): {clean(derived['previous_base'])} -> "
                  f"{clean(derived['base'])}; unchanged sources; {clean(derived['history']['reference'])}")
        for terminal in result.get("retained_terminals", []):
            print(f"Retained terminal: {terminal['terminal']} (dispatch {terminal['dispatch']})"
                  + (f"; reason: {terminal.get('reason') or 'unknown'}" if "reason" in terminal else ""))
        for terminal in result.get("worker_tabs", []):
            print(f"Worker tab: {clean(terminal['terminal'] or terminal['dispatch'])} "
                  f"({clean(terminal['state'])}, dispatch {clean(terminal['dispatch'])}); {clean(terminal['wording'])}")
        for line in result.get("progress", []):
            print(f"Progress: {line}")
        for row in result.get("pod_ticks", {}).get("lines", []):
            block = row.get("blocked")
            reason = (f"; {clean(block['code'])}: {clean(block['reason'])}" if block else "")
            if block and "status" in block:
                reason += f" (GitHub {block['status']}: {clean(block.get('message', ''))})"
            hold = row.get("hold")
            if hold:
                reason += f"; hold {clean(hold['code'])}: {clean(hold['reason'])}"
            request = row.get("request", {})
            if "retry_after" in request:
                wait = "in flight until" if request["status"] == "in_flight" else "recovery ready since"
                reason += (f"; intent_at={request['intent_at']}, {wait} "
                           f"retry_after={request['retry_after']} (Unix seconds)")
            direction = row.get("direction") or row.get("blocked_at_closure", {}).get("direction")
            checkbox = (f"checkboxes {row['checkboxes']}" if len(row.get("checkboxes", [])) > 1
                        else f"checkbox {row['ticked']}")
            print(f"PoD#{row['pod']}: {checkbox}; {clean(row['authorship'])}; "
                  f"{clean(row['status'])}" + (f" ({direction})" if direction else "") + f"{reason}; criteria "
                  + ", ".join(f"{clean(item['id'])}={clean(item['state'])}"
                              + (" (stale)" if item['state'] == 'satisfied' and not item['current'] else "")
                              for item in row['criteria']))
        if result.get("gates"):
            gates = result["gates"]
            print(f"Gates: {len(gates['remaining'])} remaining, "
                  f"{len(gates['blocked_external'])} external, {len(gates['authority_waits'])} authority")
        for row in result.get("superseded", []):
            print(f"Superseded {row['objective'] or row['record']}: {', '.join(row['schemas'])} (blocked, not converted)")
        print(f"Blocker: {result.get('blocker') or 'none'}; next: {result['next_safe_action']}")
        if result.get("next_action"):
            print(f"Next action: {result['next_action']}")

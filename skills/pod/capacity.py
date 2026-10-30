"""Healthy, same-coordinator capacity continuity; never an Orca lifecycle action.

Public descendants have separate records. Immutable work identities remain rooted
in their first objective, so an already admitted native attempt or provider effect
is neither re-created nor renamed. Only verified ancestry makes an inherited
attempt part of descendant work; fresh authority keeps its public objective scope.
"""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .errors import PodError
from .limits import HARD_LIMITS, current as current_limits
from .util import digest

SCHEMA = "pod-capacity-lineage/v1"
CHILD = "capacity_continuity"
PENDING = "capacity_rollover_pending"
CONTINUED = "capacity_continued_by"
_IDENTITY = ("admission_id", "objective", "task_id", "run_id", "role", "candidate",
             "packet_id", "serves", "binding", "request")


def _error(code, text, **detail):
    return PodError(code, text, detail or None)


def direct_root(project, objective):
    from .github import repository_context
    from .ledger import record_name, state_root
    return state_root(project) / record_name(project, repository_context(project), objective)


def public_name(state, default=None):
    return ((state or {}).get(CHILD) or {}).get("plan", {}).get("successor", default)


def same_work(state, objective):
    current = (state.get("checkpoint") or {}).get("objective")
    return objective == current or objective in state.get(CHILD, {}).get("work_objectives", [])


def _plan(value):
    wanted = {"root", "predecessor", "successor", "depth", "source_revision", "source_sha256", "at"}
    if (not isinstance(value, dict) or set(value) != wanted
            or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 4096
                   for k in ("root", "predecessor"))
            or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 256
                   for k in ("successor", "at"))
            or type(value["depth"]) is not int or not 1 <= value["depth"] <= HARD_LIMITS["successor_chain"]
            or type(value["source_revision"]) is not int or value["source_revision"] < 0
            or not isinstance(value["source_sha256"], str) or len(value["source_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["source_sha256"])):
        raise _error("state_unsupported", "Capacity lineage plan is malformed")
    if value["successor"] in (value["root"], value["predecessor"]):
        raise _error("state_unsupported", "Capacity successor must have a distinct public identity")
    return value


def validate(state):
    """Shape only; a selected descendant additionally verifies its actual parent."""
    for key in (PENDING, CONTINUED):
        if key in state:
            _plan(state[key])
    if PENDING in state and CONTINUED in state:
        raise _error("state_unsupported", "Capacity reservation and predecessor fence conflict")
    if CHILD in state:
        row = state[CHILD]
        if (not isinstance(row, dict) or set(row) != {"schema", "phase", "plan", "standing_floor", "work_objectives"}
                or row["schema"] != SCHEMA or row["phase"] not in ("preparing", "active")
                or type(row["standing_floor"]) is not int or row["standing_floor"] < 0):
            raise _error("state_unsupported", "Capacity descendant representation is malformed")
        _plan(row["plan"])
        names = row["work_objectives"]
        if (not isinstance(names, list) or len(names) != row["plan"]["depth"]
                or any(not isinstance(name, str) or not name or len(name) > 4096 for name in names)
                or len(set(names)) != len(names) or names[0] != row["plan"]["root"]
                or names[-1] != row["plan"]["predecessor"]):
            raise _error("state_unsupported", "Capacity work ancestry is malformed")


def refuse_ended(state):
    if not isinstance(state, dict):
        return
    if CONTINUED in state:
        child = state[CONTINUED]["successor"]
        raise _error("objective_continued", "This public objective continues in its capacity successor; it is not closed",
                     successor=child, next_action="select objective " + child + " through the normal helper")
    if PENDING in state or state.get(CHILD, {}).get("phase") == "preparing":
        plan = state.get(PENDING) or state[CHILD]["plan"]
        raise _error("capacity_continuity_pending", "Finish the recorded capacity transition before ordinary work",
                     predecessor=plan["predecessor"], successor=plan["successor"],
                     next_action="use internal capacity-continuity action resume for the recorded predecessor")


def _read(project, objective):
    from .history import load_context
    path = direct_root(project, objective) / "context.json"
    return load_context(path) if path.exists() else None


def _source_snapshot(parent, plan):
    snapshot = deepcopy(parent)
    marker = snapshot.pop(CONTINUED, snapshot.pop(PENDING, None))
    if marker != plan:
        raise _error("capacity_lineage_unverified", "The predecessor has no matching durable capacity reservation")
    snapshot["revision"] = plan["source_revision"]
    if digest(snapshot) != plan["source_sha256"]:
        raise _error("capacity_lineage_unverified", "The retained predecessor differs from its frozen capacity snapshot")
    return snapshot


def _admission_identity(row):
    return digest({key: row.get(key) for key in _IDENTITY})


def verify(project, objective, state):
    """Verify a bounded root lineage and the inherited immutable native identities."""
    validate(state)
    child = state.get(CHILD)
    if child is None:
        return objective
    cursor, name, seen = state, objective, set()
    for _ in range(HARD_LIMITS["successor_chain"]):
        row = cursor.get(CHILD)
        if row is None:
            if name != child["plan"]["root"]:
                raise _error("capacity_lineage_unverified", "Capacity lineage does not end at its recorded root")
            return name
        plan = row["plan"]
        if name != plan["successor"] or name in seen or plan["root"] != child["plan"]["root"]:
            raise _error("capacity_lineage_unverified", "Capacity lineage identity is ambiguous or loops")
        seen.add(name)
        parent = _read(project, plan["predecessor"])
        if parent is None:
            raise _error("capacity_lineage_unverified", "Capacity predecessor is unavailable")
        source = _source_snapshot(parent, plan)
        for key, admission in source["admissions"].items():
            inherited = cursor["admissions"].get(key)
            if inherited is None or _admission_identity(inherited) != _admission_identity(admission):
                raise _error("capacity_lineage_unverified", "An inherited admission identity is missing or changed", admission=key)
        expected_names = source.get(CHILD, {}).get("work_objectives", []) + [plan["predecessor"]]
        from .succession import _handed_over
        owner_preserved = (cursor.get("owner") == source.get("owner")
                           or _handed_over(cursor, source.get("owner"), cursor.get("owner"), since=plan["at"]))
        if (not owner_preserved or row["work_objectives"] != expected_names
                or (cursor.get("checkpoint") or {}).get("objective") != plan["successor"]):
            raise _error("capacity_lineage_unverified", "Capacity continuity cannot infer another owner or logical objective")
        cursor, name = source, plan["predecessor"]
    raise _error("capacity_lineage_unverified", "Capacity lineage exceeds its hard safety ceiling")


@contextmanager
def selected(project, objective):
    """An explicit public selector, never a caller-supplied root or transplanted row."""
    state = _read(project, objective)
    if state is None or CHILD not in state:
        yield objective
        return
    refuse_ended(state)
    root = verify(project, objective, state)
    yield root


def _native_guard(project, state, owner):
    from .ledger import _run_references, bound_assignments
    from .operations import OrcaPort, _check_objective_source
    from .orca import current_run
    if not owner or state.get("owner") != owner:
        raise _error("native_authority_unverified", "Capacity continuity needs its actual recorded coordinator")
    if ((state.get("checkpoint") or {}).get("closure") or state.get("continued_by")
            or state.get("historical_retirement")):
        raise _error("capacity_ineligible", "Ended work cannot enter healthy capacity continuity")
    refs = _run_references(state)
    if len(refs) != 1:
        raise _error("native_authority_unverified", "Capacity continuity needs one stable native Run")
    snapshot = current_run()
    run = snapshot["run"]
    expected = ((state.get("checkpoint") or {}).get("continuity") or {}).get("binding") or {}
    if (run.get("id") != expected.get("run") or run.get("coordinator_handle") != owner
            or run.get("consumer_generation") != expected.get("generation")
            or snapshot["runtime"] != next(iter(refs.values()))):
        raise _error("native_authority_unverified", "Capacity continuity cannot change the native Run, coordinator or generation")
    native = OrcaPort(project).read_native(owner, authority_runs=tuple(refs), assignments=bound_assignments(state))
    if not native.get("authoritative") or not native.get("complete"):
        raise _error("native_authority_unverified", "Current capacity ownership is unavailable")
    for key, row in state["admissions"].items():
        if row.get("state") in ("reserved", "unresolved") or (not row.get("native_binding") and row.get("state") != "deferred"):
            raise _error("capacity_native_pending", "Resolve the recorded native start before capacity continuity", admission=key)
        if row.get("route_decision", {}).get("effective_unknown") is True:
            raise _error("native_authority_unverified", "An inherited effective native identity is unavailable", admission=key)
    _check_objective_source(project, (state.get("checkpoint") or {}).get("objective_source"))
    questions = (state.get("checkpoint") or {}).get("questions", [])
    settled_states = ("answered", "resolved", "canceled", "expired", "consumed")
    if any(not isinstance(row, dict) or not (row.get("resolved") is True
           or row.get("state") in settled_states or row.get("status") in settled_states) for row in questions):
        raise _error("capacity_decision_pending", "Resolve the recorded decision through its current objective before capacity continuity")
    return native


def _publish(path, state, *, native=None):
    """Product-owned transition publication after guards; no external lifecycle call."""
    from .ledger import _validate_context
    from .history import prepare_context, publish_context
    from .relay import stamp_decision
    from .util import protect_decision_record
    state["revision"] += 1
    stamp_decision(state)
    protect_decision_record(state)
    _validate_context(state)
    physical, chunks = prepare_context(path, state, native=native, bulk_history=True)
    publish_context(path, physical, chunks)


def _resume_locked(project, plan, owner):
    from .governor import _read_journal
    from .history import publish_journal
    parent_dir, child_dir = (direct_root(project, plan[name]) for name in ("predecessor", "successor"))
    parent = _read(project, plan["predecessor"])
    if parent is None:
        raise _error("capacity_lineage_unverified", "Recorded capacity predecessor is unavailable")
    source = _source_snapshot(parent, plan)
    native = _native_guard(project, source, owner)
    child = _read(project, plan["successor"])
    if child is not None and (child.get(CHILD, {}).get("plan") != plan or child.get("owner") != owner):
        raise _error("capacity_child_conflict", "The successor id already belongs to different work")
    if child is None:
        child = deepcopy(source)
        child["checkpoint"]["objective"] = plan["successor"]
        child[CHILD] = {"schema": SCHEMA, "phase": "preparing", "plan": plan,
                        "standing_floor": len(source.get("standing_authorization", {}).get("decisions", [])),
                        "work_objectives": source.get(CHILD, {}).get("work_objectives", []) + [plan["predecessor"]]}
        _publish(child_dir / "context.json", child, native=native)
    if child[CHILD]["phase"] == "preparing":
        # A prior attempt may already have copied the exact journal. No provider
        # submission is performed, and counters/rows retain all original ids.
        journal = _read_journal(parent_dir / "governor.json")
        target = child_dir / "governor.json"
        if target.exists() and _read_journal(target) != journal:
            raise _error("capacity_child_conflict", "Prepared successor journal differs from its predecessor")
        publish_journal(target, journal, limit=2 * current_limits().record_bytes)
    if PENDING in parent:
        native = _native_guard(project, source, owner)
        parent.pop(PENDING)
        parent[CONTINUED] = plan
        _publish(parent_dir / "context.json", parent, native=native)
    if child[CHILD]["phase"] == "preparing":
        native = _native_guard(project, source, owner)
        child[CHILD]["phase"] = "active"
        _publish(child_dir / "context.json", child, native=native)
    verify(project, plan["successor"], child)
    return {"status": "continued", "kind": "healthy_capacity", "predecessor": plan["predecessor"],
            "successor": plan["successor"], "logical_root": plan["root"], "depth": plan["depth"],
            "native_effects": [], "next_action": "select objective " + plan["successor"]}


def resume(project, objective, owner):
    from .ledger import _lock
    prior = _read(project, objective)
    if prior is None:
        raise _error("capacity_ineligible", "No recorded capacity transition exists")
    plan = prior.get(PENDING) or prior.get(CONTINUED)
    if plan is None:
        raise _error("capacity_ineligible", "No recorded capacity transition exists")
    paths = sorted({direct_root(project, name) / "context.json"
                    for name in (plan["root"], plan["predecessor"], plan["successor"])})
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(_lock(path))
        return _resume_locked(project, plan, owner)


def maybe(project, objective, owner, *, request_bytes=0):
    """At most one transition at a successful ordinary checkpoint/report boundary."""
    from .history import context_limit
    from .ledger import _lock
    parent_dir = direct_root(project, objective)
    prior = _read(project, objective)
    if prior is None:
        return {"status": "not_due"}
    if ((prior.get("checkpoint") or {}).get("closure") or prior.get("continued_by")
            or prior.get("historical_retirement")):
        return {"status": "not_due"}
    if prior.get(PENDING) or prior.get(CONTINUED):
        return resume(project, objective, owner)
    physical = (parent_dir / "context.json").stat().st_size
    from .assurance import live_finding_count
    checkpoint = prior.get("checkpoint") or {}
    finding_pressure = any(live_finding_count(row.get("findings", []), {"map_state": checkpoint})
                           >= max(1, 7 * current_limits().findings // 8)
                           for row in checkpoint.get("obligations", []) if row.get("kind") == "assurance")
    if (physical < 7 * context_limit() // 8
            and request_bytes < 7 * current_limits().record_bytes // 8 and not finding_pressure):
        return {"status": "not_due"}
    if CHILD in prior:
        plan = prior[CHILD]["plan"]
        parent = _read(project, plan["predecessor"])
        if parent is None:
            raise _error("capacity_lineage_unverified", "Capacity predecessor is unavailable")
        inherited = _source_snapshot(parent, plan)
        def work_identity(state):
            checkpoint = state.get("checkpoint") or {}
            return digest({"admissions": state["admissions"], "constraints": state["constraints"],
                           "candidate": checkpoint.get("candidate"), "criteria": checkpoint.get("criteria"),
                           "obligations": checkpoint.get("obligations"), "proposals": checkpoint.get("proposals"),
                           "source": checkpoint.get("objective_source")})
        if work_identity(prior) == work_identity(inherited):
            return {"status": "held", "code": "capacity_no_material_relief",
                    "next_action": "preserve the current successor; repeated metadata alone creates no further descendant"}
    root = verify(project, objective, prior)
    depth = prior.get(CHILD, {}).get("plan", {}).get("depth", 0) + 1
    if depth > current_limits().successor_chain:
        return {"status": "held", "code": "capacity_chain_limit", "limit": current_limits().successor_chain}
    successor = root[:192] + "-capacity-" + str(depth) + "-" + digest({"objective": objective, "state": prior})[:12]
    paths = sorted({direct_root(project, name) / "context.json" for name in (root, objective, successor)})
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(_lock(path))
        current_state = _read(project, objective)
        if current_state != prior:
            raise _error("capacity_state_changed", "Capacity state changed; read the accepted current record")
        native = _native_guard(project, prior, owner)
        if _read(project, successor) is not None:
            raise _error("capacity_child_conflict", "The deterministic successor is already recorded")
        plan = {"root": root, "predecessor": objective, "successor": successor, "depth": depth,
                "source_revision": prior["revision"], "source_sha256": digest(prior),
                "at": datetime.now(timezone.utc).isoformat()}
        # Capacity recovery must itself fit before publishing its reservation.
        from .history import prepare_context, compact_journal
        from .governor import _read_journal
        from .util import encoded
        child = deepcopy(prior)
        child["checkpoint"]["objective"] = successor
        child[CHILD] = {"schema": SCHEMA, "phase": "preparing", "plan": plan,
                        "standing_floor": len(prior.get("standing_authorization", {}).get("decisions", [])),
                        "work_objectives": prior.get(CHILD, {}).get("work_objectives", []) + [objective]}
        reserved = {**deepcopy(prior), PENDING: plan}
        fenced = {**deepcopy(prior), CONTINUED: plan}
        for candidate_path, candidate in ((parent_dir / "context.json", reserved),
                                           (parent_dir / "context.json", fenced),
                                           (direct_root(project, successor) / "context.json", child)):
            candidate["revision"] += 4  # Enough digits for every metadata phase.
            prepare_context(candidate_path, candidate, native=native, bulk_history=True)
        journal = _read_journal(parent_dir / "governor.json")
        if len(encoded(compact_journal(journal)[0], compact=True)) > 2 * current_limits().record_bytes:
            raise _error("capacity_journal_limit", "The complete successor journal does not fit; no reservation was written")
        prior[PENDING] = plan
        _publish(parent_dir / "context.json", prior, native=native)
        return _resume_locked(project, plan, owner)


def status_block(state):
    block = {"continuation_kind": "healthy_capacity", "completed": False}
    if CONTINUED in state:
        plan = state[CONTINUED]
        return {**block, "continued_by": plan["successor"], "logical_root": plan["root"],
                "state": "capacity_successor", "at": plan["at"]}
    if PENDING in state:
        plan = state[PENDING]
        return {**block, "state": "capacity_pending", "successor": plan["successor"],
                "logical_root": plan["root"], "next_action": "use internal capacity-continuity action resume for " + plan["predecessor"]}
    if CHILD in state:
        row = state[CHILD]
        return {**block, "continues": row["plan"]["predecessor"], "logical_root": row["plan"]["root"],
                "state": "capacity_successor_" + row["phase"]}
    return None


def tail_state(project, state):
    """A predecessor never claims completion; only its verified tail can end duty."""
    cursor = state
    seen = set()
    for _ in range(HARD_LIMITS["successor_chain"]):
        plan = cursor.get(CONTINUED)
        if plan is None:
            if CHILD in cursor:
                verify(project, cursor[CHILD]["plan"]["successor"], cursor)
            if (cursor.get("checkpoint") or {}).get("closure"):
                return "closed"
            if cursor.get("historical_retirement"):
                return "retired"
            return "open"
        name = plan["successor"]
        if name in seen:
            return "unknown"
        seen.add(name)
        cursor = _read(project, name)
        if cursor is None or cursor.get(CHILD, {}).get("phase") != "active":
            return "unknown"
    return "unknown"

"""Bounded, read-only admission facts. The host serializes and authorizes effects."""
from __future__ import annotations

import json
import hashlib
import math
import os
from collections import Counter
from pathlib import Path

from .errors import PodError
from . import gitio, history, listing
from .github import repository_context
from .ledger import binding_valid, record_name, state_root
from .orca import _read, _passable, read_deadline
from .util import digest as _digest, public_decisions
from .window import Budget

MAX_TIMEOUT = 30.0
MAX_CLAIM_BYTES = 8 * 1024 * 1024
MAX_CLAIMS = 4096


def digest(value):
    try:
        return _digest(value)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PodError("invalid_record", "Admission facts cannot be encoded canonically") from None


def _claim_reference(reference: dict, directory: Path):
    value = reference.get(history.MARKER)
    if not isinstance(value, dict) or type(value.get("bytes")) is not int or value["bytes"] > MAX_CLAIM_BYTES:
        raise PodError("admission_claim_limit", "One archived claim exceeds the bounded query working set")
    store = history.ChunkStore(directory)
    def load(address):
        history._check_read_deadline()
        return store.load(address)
    return history._expand(reference, load, {})


def _fleet() -> dict | None:
    """One complete native inventory for CE's unbound caller; bound callers fall back.

    Orca decides the scope. Omitting --run must never turn a bound Run's empty
    list into proof that the repository has no other worker.
    """
    prefix = ["orchestration", "worker-list", "--include-remote", "--json", "--limit", "100"]
    cursor, seen, workers, runtime = None, set(), {}, None
    for _ in range(100):
        response = _read(prefix + (["--cursor", cursor] if cursor else []))
        result = response["result"]
        scope = result.get("scope")
        if isinstance(scope, dict) and scope.get("source") == "bound" and cursor is None:
            return None
        if not isinstance(scope, dict) or scope.get("source") != "all" or scope.get("run") is not None:
            raise PodError("native_inventory_scope", "Native inventory does not prove all-Run coverage")
        if runtime is not None and runtime != response["runtime"]:
            raise PodError("orca_runtime_changed", "Native runtime changed between inventory pages")
        runtime = response["runtime"]
        rows, page = result.get("workers"), result.get("page")
        if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(page, dict)
                or type(page.get("hasMore")) is not bool or result.get("truncated", False) is not False):
            raise PodError("native_inventory_incomplete", "Native inventory page is incomplete")
        for row in rows:
            key = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in workers:
                raise PodError("orca_pagination", "Native inventory repeats or omits a Dispatch identity")
            workers[key] = row
        if not page["hasMore"]:
            return {"runtime": runtime, "workers": list(workers.values()), "complete": True}
        cursor = page.get("nextCursor")
        if not rows or not isinstance(cursor, str) or not cursor or cursor in seen:
            raise PodError("orca_pagination", "Native inventory cursor is missing or repeated")
        seen.add(cursor)
    raise PodError("admission_inventory_limit", "Native inventory exceeds bounded pages")


def _worker_fact(row: dict) -> dict:
    if not isinstance(row, dict):
        raise PodError("orca_contract", "Native worker row is malformed")
    projection = row.get("projection")
    if not isinstance(projection, dict):
        raise PodError("orca_contract", "Native worker projection is unavailable")
    resource = row.get("resource") or {}
    workspace = projection.get("workspace") or {}
    release = projection.get("resource") or {}
    live = projection.get("liveness") or {}
    if not all(isinstance(value, dict) for value in (resource, workspace, release, live)):
        raise PodError("orca_contract", "Native worker claim facts are malformed")
    dispatch, run, task = row.get("dispatchId"), row.get("runId"), row.get("taskId")
    if (not all(isinstance(value, str) and value and _passable(value) for value in (dispatch, run, task))
            or any(projection.get(field) != value for field, value in
                   (("dispatchId", dispatch), ("runId", run), ("taskId", task)))):
        raise PodError("orca_contract", "Native worker identity differs")
    path = listing._location(resource.get("worktreeId") or workspace.get("id"))
    if path is not None and not _passable(path):
        raise PodError("orca_contract", "Native worker location is not valid UTF-8")
    return {"dispatch": dispatch, "run": run, "task": task,
            "worker": projection.get("id"),
            "terminal": row.get("agentTerminalHandle", resource.get("terminalHandle")),
            "resource": {key: resource.get(key) for key in
                         ("id", "ownerDispatchId", "ownershipState", "releaseState", "endpointId", "endpointIncarnation")},
            "worktree": path,
            "liveness": live.get("verdict", "unverifiable"), "released": release.get("state") == "released"}


def _terminals() -> dict:
    """Current native ownership also covers terminals outside supervised workers."""
    from .retirement import _complete
    response = _read(["terminal", "list", "--json"])
    rows = _complete(response["result"], "terminals")
    if rows is None:
        raise PodError("native_resources_incomplete", "Native terminal ownership inventory is incomplete")
    for row in rows:
        if not isinstance(row.get("handle"), str) or not row["handle"] or not _passable(row["handle"]):
            raise PodError("native_resources_invalid", "Native terminal identity is malformed")
        for field in ("worktreePath", "worktreeId"):
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or not _passable(value)):
                raise PodError("native_resources_invalid", "Native terminal location is malformed")
    return {"runtime": response["runtime"], "complete": True, "terminals": [
        {key: row.get(key) for key in ("handle", "worktreePath", "worktreeId", "connected", "writable", "orphaned", "exitCause")}
        for row in rows]}  # Presentation/preview text never leaves this reader.


def _native_claim(source: dict, runtime: str):
    """The W1-owned interpreter alone classifies an abandoned attempt's effects."""
    try:
        from .abandoned import current_claim, is_abandoned
    except ImportError:
        raise PodError("native_claim_reader_unavailable", "The shared current-claim reader is not installed") from None
    response = _read(["orchestration", "worker-show", "--dispatch", source["dispatchId"], "--json"])
    result = response.get("result") or {}
    dispatch, worker, projection = (result.get(name) or {} for name in ("dispatch", "worker", "projection"))
    if (not all(isinstance(row, dict) for row in (dispatch, worker, projection))
            or response.get("runtime") != runtime or not is_abandoned(response)
            or any(dispatch.get(key) != source.get(field) for key, field in
                   (("id", "dispatchId"), ("runId", "runId"), ("taskId", "taskId")))
            or projection.get("id") != (source.get("projection") or {}).get("id")):
        raise PodError("native_identity_unverified", "Current claim observation differs from its inventory identity")
    return current_claim(response)


def _owner_observation(run: str, terminals: dict, runtime: str) -> dict:
    from .orca import run_show
    observed = run_show(run)
    if observed["runtime"] != runtime:
        raise PodError("orca_runtime_changed", "Coordinator ownership and native inventory runtimes differ")
    terminal = next((row for row in terminals["terminals"] if row.get("handle") == observed["coordinator_handle"]), None)
    return {"run": run, "runtime": runtime, "terminal": observed["coordinator_handle"],
            "consumer_generation": observed["consumer_generation"],
            "worktree": (terminal or {}).get("worktreePath"),
            "live": bool(terminal and terminal.get("connected") is True and terminal.get("writable") is True
                         and terminal.get("orphaned") is False), "source": "native_run_and_terminal"}


def _primary(path: Path) -> tuple[dict, str]:
    reader = getattr(history, "load_admission_facts", None)
    if reader is not None:
        facts = reader(path, include_admissions=False)
        raw = _facts_context(facts)
        return raw, digest(facts["generation"])
    # Reuse the existing no-follow, bounded physical reader; do not hydrate receipts.
    data = history._context_bytes(path, history.CONTEXT_LIMIT)
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise PodError("invalid_record", "Cannot decode bounded context JSON") from None
    if not isinstance(raw, dict):
        raise PodError("state_unsupported", "Context is not an object")
    return raw, hashlib.sha256(data).hexdigest()


def _generation(value):
    if (not isinstance(value, dict) or type(value.get("revision")) is not int or value["revision"] < 0
            or not isinstance(value.get("sha256"), str) or not history.HEX.fullmatch(value["sha256"])):
        raise PodError("admission_facts_unsupported", "Facts generation is unavailable")
    return value


def _facts_context(facts: dict) -> dict:
    if (not isinstance(facts, dict) or facts.get("schema") != "pod-admission-facts/v1"
            or facts.get("attribution_complete") is not True or not isinstance(facts.get("context"), dict)
            or facts.get("phase") not in ("open", "closed", "continued", "retired")):
        raise PodError("admission_facts_unsupported", "Current attribution contract is unavailable")
    return {**facts["context"], "_facts_phase": facts["phase"],
            "_facts_generation": _generation(facts.get("generation")),
            "_facts_attribution": facts.get("attribution")}


def _phase(raw: dict) -> str:
    return raw["_facts_phase"] if "_facts_phase" in raw else listing._phase(raw)


def _placement(row: dict) -> dict:
    recovery = row.get("recovery")
    if recovery is not None and not isinstance(recovery, dict):
        raise PodError("admission_placement_invalid", "Accepted recovery placement facts are malformed")
    placement = row.get("placement_binding")
    if placement is None:
        placement = (recovery or {}).get("placement_binding")
    if placement is not None and not isinstance(placement, dict):
        raise PodError("admission_placement_invalid", "Accepted placement binding is malformed")
    if placement:
        path, branch = placement.get("path"), placement.get("branch")
        if (path is not None and (not isinstance(path, str) or len(path) > 4096 or not _passable(path) or not Path(path).is_absolute())
                or branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 256 or not _passable(branch))):
            raise PodError("admission_placement_invalid", "Accepted placement location fields are malformed")
        key = placement.get("repo_key")
        if key is not None and (not isinstance(key, str) or not history.HEX.fullmatch(key)):
            raise PodError("admission_placement_invalid", "Accepted placement repository identity is malformed")
    return placement or {}


def _membership(project: Path, context: dict, record: str, raw: dict, known=()) -> str | None:
    """A matching key proves membership; exclusion needs a second attributed key.

    A mismatched name alone could be corruption of this repository's record.
    Current worktree attribution is written in the same atomic context by the
    existing checkpoint writer. Unattributed records require normalization.
    """
    from .installer import STATE_SCHEMAS
    from .records import worktree_binding
    if raw.get("schema") != STATE_SCHEMAS["context"] or not isinstance(raw.get("checkpoint"), dict):
        raise PodError("state_unsupported", "Required attribution header format is unsupported")
    name = listing._name(raw)
    if name is None or len(name) > 256 or "\x00" in name:
        raise PodError("unattributed", "Objective identity is unavailable")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise PodError("unattributed", "Objective identity is not UTF-8") from None
    attribution = raw.get("_facts_attribution")
    if attribution is not None:
        if (not isinstance(attribution, dict) or attribution.get("status") != "proved"
                or attribution.get("objective") != name or not isinstance(attribution.get("repo_key"), str)
                or not history.HEX.fullmatch(attribution["repo_key"])
                or record_name(project, attribution, name) != record):
            raise PodError("record_attribution_unknown", "Normalized attribution does not bind the record key")
        return name if attribution["repo_key"] == context["repo_key"] else None
    place = (raw.get("checkpoint") or {}).get("worktree") if isinstance(raw.get("checkpoint"), dict) else None
    if record_name(project, context, name) == record:
        if isinstance(place, dict) and place.get("repo_key") != context["repo_key"]:
            raise PodError("record_identity_mismatch", "Worktree attribution differs from the record key")
        return name
    if (raw.get("schema") == STATE_SCHEMAS["context"] and isinstance(place, dict)
            and raw["checkpoint"].get("schema") == STATE_SCHEMAS["checkpoint"]):
        worktree_binding(place)
        if record_name(project, {"repo_key": place["repo_key"]}, name) == record:
            return None
    for source in known:
        if record_name(Path(source["worktree"]), source, name) == record:
            return name if source["repo_key"] == context["repo_key"] else None
    # Earlier records can retain attribution in their accepted placement facts
    # even when the coordinator worktree was not recorded. Read no history here.
    admissions = raw.get("admissions") or {}
    if not isinstance(admissions, dict):
        raise PodError("state_unsupported", "Admission attribution fields are malformed")
    for row in admissions.values():
        if not isinstance(row, dict):
            continue
        placement = _placement(row)
        if isinstance(placement, dict) and isinstance(placement.get("repo_key"), str):
            if record_name(project, placement, name) == record:
                return name if placement["repo_key"] == context["repo_key"] else None
    raise PodError("record_attribution_unknown", "Record cannot be attributed or safely excluded")


def _admissions(raw: dict, directory: Path) -> dict:
    from .installer import STATE_SCHEMAS
    from .ledger import _CONTEXT_FIELDS, _SUCCESSION_FIELDS
    projected = "_facts_generation" in raw
    if projected:
        facts = history.load_admission_facts(directory / "context.json", include_admissions=True)
        current = _facts_context(facts)
        if facts.get("complete") is not True or not isinstance(facts.get("admissions"), dict):
            raise PodError("admission_facts_incomplete", "Current claim inventory is incomplete")
        if current["_facts_generation"] != raw["_facts_generation"]:
            raise PodError("record_changed", "Attribution changed before current claims were read")
        raw.clear()
        raw.update(current)
        raw["admissions"] = facts["admissions"]
    if not projected and (set(raw) - (_CONTEXT_FIELDS | _SUCCESSION_FIELDS | {"historical_retirement", "decision_channels",
            "decision_order", "pod_ticks", "standing_authorization", "history"})
            or not _CONTEXT_FIELDS <= set(raw) or type(raw.get("revision")) is not int or raw["revision"] < 0):
        raise PodError("state_unsupported", "Context claim representation is unsupported")
    if raw.get("schema") != STATE_SCHEMAS["context"]:
        raise PodError("state_unsupported", "Context schema is unsupported")
    checkpoint = raw.get("checkpoint")
    earlier_claims = isinstance(checkpoint, dict) and checkpoint.get("schema") == "pod-checkpoint/v2"
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") not in (STATE_SCHEMAS["checkpoint"], "pod-checkpoint/v2"):
        raise PodError("state_unsupported", "Checkpoint schema is unsupported")
    if checkpoint.get("worktree") is not None:
        from .records import worktree_binding
        worktree_binding(checkpoint["worktree"])
    admissions = raw.get("admissions")
    if not isinstance(admissions, dict):
        raise PodError("state_unsupported", "Admissions are unavailable")
    metadata = raw.get("history")
    if "history" in raw and (not isinstance(metadata, dict) or metadata.get("schema") != history.FORMAT["schema"]):
        raise PodError("history_invalid", "History format is unsupported")
    rows = {}
    for key, row in admissions.items():
        if history.is_reference(row):
            if (raw.get("history") or {}).get("schema") != history.FORMAT["schema"]:
                raise PodError("history_invalid", "History format is unsupported")
            # A settled attempt can retain a live resource. Read its binding, never
            # infer absence from archival, a report, or the objective's closed label.
            row = _claim_reference(row, directory)
        if (not isinstance(row, dict) or row.get("schema") not in
                ((STATE_SCHEMAS["admission"], "pod-admission/v3") if earlier_claims else (STATE_SCHEMAS["admission"],))
                or row.get("state") not in ("reserved", "bound", "unresolved", "closed", "deferred")
                or row.get("role") not in (("implement", "review", "investigate", None) if earlier_claims else ("implement", "review", "investigate"))
                or row.get("admission_id") != key):
            raise PodError("state_unsupported", "Admission claim is malformed")
        if row.get("native_binding") is not None and not isinstance(row.get("runtime"), str):
            raise PodError("state_unsupported", "Native claim runtime is unavailable")
        if row.get("native_binding") is not None and not binding_valid(row["native_binding"]):
            raise PodError("admission_binding_invalid", "Native claim binding is malformed")
        _placement(row)
        rows[key] = row
    if projected:
        return rows  # The versioned reader validates phase without loading closure.report.
    closure = checkpoint.get("closure")
    if closure is not None:
        from .obligations import _closure_record
        _closure_record(closure)
    if "continued_by" in raw:
        from .succession import validate_link
        validate_link("continued_by", raw["continued_by"])
    if "historical_retirement" in raw:
        from .retirement import validate_entry
        retirement = raw["historical_retirement"]
        if history.is_reference(retirement):
            retirement = _claim_reference(retirement, directory)
        validate_entry(retirement)
    return rows


def _journal(path: Path) -> tuple[dict, str]:
    from . import governor
    if path.is_symlink():
        raise PodError("unsafe_state", "Governor journal is redirected")
    reader = getattr(governor, "load_admission_effects", None)
    if reader is not None:
        facts = reader(path)
        if (not isinstance(facts, dict) or facts.get("schema") != "pod-admission-effects/v1"
                or facts.get("complete") is not True or not isinstance(facts.get("units"), dict)
                or not isinstance(facts.get("actions"), list)):
            raise PodError("admission_effects_incomplete", "Current effect inventory is unavailable")
        journal = {"units": facts["units"], "actions": facts["actions"]}
        _validate_effect_facts(journal)
        return journal, digest(_generation(facts.get("generation")))
    journal = governor._read_journal(path)
    _validate_effect_facts(journal)
    try:
        return journal, digest(journal)
    except UnicodeError:
        raise PodError("invalid_record", "Journal identities must be UTF-8") from None


def _validate_effect_facts(journal: dict) -> None:
    from .config import validate_effects
    from .governor import KINDS, DECISIONS, OUTCOMES, _validate_branch
    for unit in journal["units"].values():
        if not isinstance(unit, dict):
            raise PodError("admission_effects_incomplete", "Unit branch facts are malformed")
        _validate_branch(unit.get("branch"))
    for row in journal["actions"]:
        action = row.get("action") if isinstance(row, dict) else None
        if (not isinstance(action, dict) or action.get("kind") not in KINDS
                or row.get("decision") not in DECISIONS or row.get("outcome") not in OUTCOMES
                or any(not isinstance(action.get(key), str) or not action[key] for key in ("candidate", "target", "unit"))
                or not isinstance(row.get("record_id"), str) or not row["record_id"]):
            raise PodError("admission_effects_incomplete", "Current action facts are malformed")
        if action.get("effects") is not None:
            validate_effects(action["effects"])
        recorded = row.get("effects")
        if recorded is not None and (not isinstance(recorded, list)
                or any(not isinstance(effect, str) or not effect for effect in recorded)):
            raise PodError("admission_effects_incomplete", "Admitted Governor effect identities are malformed")


def _action_effects(row: dict, project: Path | None, policies: dict) -> tuple[list[str] | None, str]:
    """Use the Governor's identities and preserve its durable admitted mapping."""
    from .governor import _effects
    recorded = row.get("effects") or []
    effects, source = _effects(row["action"], {})
    if effects is None and not recorded and project is not None:
        from .config import effective
        key = str(project)
        if key not in policies:
            policies[key] = effective(project)
        effects, source = _effects(row["action"], policies[key]["policy"]["waste_governor"])
    if effects is None and not recorded:
        return None, "unknown"
    return sorted(set(effects or []) | set(recorded)), source if effects is not None else "recorded"


def _relevant(facts: dict, request: dict, repo_key: str | None, overlap=None) -> bool:
    """Intersect factual scopes, without deciding whether CE may perform an action."""
    if not any(request.values()):
        return True
    if "effects" in facts:
        identities = facts["effects"]
        return bool(request["effects"] and (identities is None or set(identities) & set(request["effects"]))
                    or request["target"] and facts.get("target") == request["target"]
                       and facts.get("repo_key", repo_key) == repo_key)
    owners = facts.get("repo_keys") or ([facts["repo_key"]] if facts.get("repo_key") else [])
    if owners and repo_key not in owners:
        return False
    if request["worktree"] or request["branch"]:
        paths = facts.get("worktrees", [facts.get("worktree")])
        branches = facts.get("branches", [facts.get("branch")])
        if (request["worktree"] and any(path and (overlap(request["worktree"], path) if overlap else request["worktree"] == path) for path in paths)
                or request["branch"] and request["branch"] in branches):
            return True
        if facts.get("locations_complete") is True:
            return False
        if request["worktree"] and not request["branch"] and any(paths):
            return False
    return True


def _placement_matches(binding: dict, shown: dict) -> bool:
    return (binding_valid(binding) and all(binding.get(key) == shown.get(field) for key, field in
        (("dispatchId", "dispatch"), ("runId", "run"), ("taskId", "task"),
         ("workerId", "worker"), ("terminalHandle", "terminal")))
        and listing._location(binding["worktreeId"]) == shown["worktree"])


def _canonical_target(project: Path | None, ref: str | None) -> str | None:
    if project is None or ref is None:
        return ref
    observed = gitio.read(project, ["rev-parse", "--symbolic-full-name", "--verify", "--quiet", "--end-of-options", ref])
    value = observed.stdout.strip() if observed is not None and observed.returncode == 0 else ""
    return value if value.startswith("refs/") else ref


def _claim_identity(raw: dict, rows: dict) -> str:
    checkpoint = raw["checkpoint"]
    phase = _phase(raw)
    return digest({"objective": listing._name(raw), "phase": phase,
                   "worktree": checkpoint.get("worktree") if phase == "open" else None,
                   "runs": checkpoint.get("native_refs"),
                   "admissions": {key: {field: row.get(field) for field in
                       ("state", "role", "native_binding", "runtime", "worktree", "request_uuid", "run_id", "task_id", "boundary")}
                       for key, row in rows.items()}})


def _effect_identity(journal: dict, *, open_objective: bool) -> str:
    return digest({"actions": [row for row in journal["actions"]
                              if row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")],
                   "branches": {key: unit.get("branch") for key, unit in journal["units"].items()}
                               if open_objective else {}})


def _proof(project: Path, objective: str, candidate: str) -> dict:
    from .ledger import kernel_view, read
    state = read(project, objective)
    if state is None:
        raise PodError("objective_unknown", "The selected objective has no record")
    checkpoint = state.get("checkpoint") or {}
    if checkpoint.get("candidate") != candidate:
        return {"status": "blocked", "candidate": candidate, "reason": "candidate_mismatch"}
    view = kernel_view(project, objective, state=state, candidate=candidate)
    from .obligations import _persisted_rows, TERMINAL
    gaps, unavailable = [], []
    if view["map"] is None:
        gaps.append("map_missing")
    else:
        try:
            _persisted_rows(view["map"], view["ctx"], unverifiable=unavailable)
            gaps.extend("evidence_unverified:" + key for key in unavailable)
        except PodError as exc:
            gaps.append(exc.code)
    verification_gaps = checkpoint.get("verification_gaps")
    if not isinstance(verification_gaps, list) or any(not isinstance(item, str) for item in verification_gaps):
        raise PodError("invalid_verification_gaps", "Recorded verification_gaps must be a list of strings")
    gaps.extend(verification_gaps)
    if view["ctx"]["outstanding"]:
        gaps.append("outstanding_assignments")
    if view["label"]["label"] != "QUALIFIED":
        gaps.append("assurance_unbound")
    return {"status": "unknown" if view["settlement"] == "unverified" or unavailable else "blocked" if gaps else "ready",
            "candidate": candidate, "settlement": view["settlement"],
            "purpose": "candidate_proof", "gaps": gaps, "assurance": view["label"],
            "objective_unfinished": [row["id"] for row in (view["map"] or {}).get("obligations", [])
                                     if row["state"] not in TERMINAL],
            "objective_complete": bool(view["map"]) and all(row["state"] in TERMINAL
                                      for row in view["map"]["obligations"])}


def _query(project: Path, *, repository: str | None = None, objective: str | None = None,
          candidate: str | None = None, worktree: str | None = None, branch: str | None = None,
          target: str | None = None, target_commit: str | None = None,
          effects: list[str] | None = None, expect: str | None = None, timeout: float = 10.0,
          known_repositories: list[str] | None = None) -> dict:
    """Inventory Pod claims and exact proof, with explicit uncertainty and recheck identity.

    No result is a permission. CE joins its native/authority inventory and repeats
    this read under its existing serialization immediately before the effect.
    """
    if bool(objective) != bool(candidate) or candidate and not gitio.OBJECT_ID.fullmatch(candidate):
        raise PodError("admission_candidate", "Supply an objective and its full candidate object id together")
    if target_commit and (not target or not gitio.OBJECT_ID.fullmatch(target_commit)):
        raise PodError("admission_target", "A target commit needs a target ref and a full object id")
    project = Path(repository or project).expanduser().resolve()
    requested_target, target = target, _canonical_target(project, target)
    context = repository_context(project, fresh=True)
    known = [context, *(repository_context(Path(path).expanduser().resolve(), fresh=True)
                        for path in known_repositories or [])]
    root = state_root(project)
    scope = {"repository": context["repository"], "repo_key": context["repo_key"], "path": str(project),
             "inventory": "pod_objectives_and_recorded_runs"}
    result = {"schema": "pod-admission-query/v1", "status": "observed", "scope": scope,
              "complete": True, "claims": [], "conflicts": [], "unknowns": [],
              "native_resources_complete": False,
              "proof": None, "snapshot": None, "target": None,
              "authorization": False,
              "next_action": "Join CE authority/resources and recheck under effect serialization"}
    stamps, sources, runs, run_owners, phases, policies = {}, [], {}, {}, {}, {}
    effect_sources, run_repositories, run_sources = [], {}, {}
    record_scopes = {}
    owners, owner_reads = {}, {}
    placements_by_dispatch = {}
    scope_records_read, attribution_gaps = set(), []
    abandoned_dispatches = set()
    physical_paths = {}

    def physical(path):
        if path not in physical_paths:
            physical_paths[path] = Path(path).resolve()
        return physical_paths[path]

    def overlap(left, right):
        try:
            left, right = physical(left), physical(right)
            return left.is_relative_to(right) or right.is_relative_to(left)
        except (OSError, ValueError):
            return True  # An inaccessible path cannot establish independence.

    def relevant(facts, selection, key):
        return _relevant(facts, selection, key, overlap)

    def workspace(path, places):
        if not path:
            return None, False
        roots = [root for root in places if physical(path).is_relative_to(physical(root))]
        return (max(roots, key=len) if roots else None,
                bool(roots) or any(overlap(path, root) for root in places))
    from .orca import _DEADLINE
    budget = _DEADLINE.get() or Budget(float(timeout))

    def unknown(record, code, **extra):
        if isinstance(record, str):
            try:
                record.encode("utf-8")
            except UnicodeError:
                record = "sha256:" + hashlib.sha256(os.fsencode(record)).hexdigest()
        fact_scope = extra.pop("fact_scope", record_scopes.get(record, scope))
        repair = {"action": "inspect_record", "record": record,
                  "condition": "Establish the named fact from its authoritative source; preserve the record and repeat the query"}
        if record is None and code.startswith("native_resources"):
            repair = {"command": ["orca", "terminal", "list", "--json"],
                      "condition": "Establish complete, well-formed current native terminal ownership"}
        elif record is None and code.startswith("orca_"):
            repair = {"command": ["orca", "orchestration", "worker-list", "--include-remote", "--limit", "100", "--json"],
                      "condition": "Reconcile the failed native inventory fact; incomplete coverage never proves absence"}
        elif record is None and code == "discovery_budget":
            repair = {"action": "inspect_read_budget", "condition": "Identify and reconcile the source read that exhausted CE's deadline before retrying"}
        result["unknowns"].append({"record": record, "code": code, "scope": fact_scope,
            "fact": extra.pop("fact", code), "reconciliation": extra.pop("reconciliation", repair),
            **extra})

    def claim(row):
        if len(result["claims"]) >= MAX_CLAIMS:
            raise PodError("admission_claim_limit", "Current claims exceed the bounded projection")
        try:
            encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise PodError("invalid_record", "Claim identities must be UTF-8") from None
        if len(encoded) > 16 * 1024:
            raise PodError("admission_claim_limit", "One claim exceeds the bounded projection")
        result["claims"].append(row)

    def effect_claims(directory, raw, journal, source):
        name = listing._name(raw)
        for row in journal["actions"]:
            if row["decision"] != "ALLOW" or row["outcome"] not in ("pending", "UNKNOWN"):
                continue
            action = row["action"]
            source_path = source.get("path", source.get("worktree")) if source else None
            if not source_path and source:
                source_path = next((item["worktree"] for item in known if item["repo_key"] == source.get("repo_key")), None)
            source_project = Path(source_path) if source_path and Path(source_path).is_dir() else None
            target_project = source_project or next((Path(item["worktree"]) for item in known
                if source and item["repo_key"] == source.get("repo_key")), None)
            identities, effect_source = _action_effects(row, source_project, policies)
            action_target = _canonical_target(target_project, action["target"]) if action["kind"] in ("merge", "push", "pr_update") else action["target"]
            fact_scope = {"effects": identities, "target": action_target, "action": row["record_id"],
                          "repo_key": source.get("repo_key") if source else None}
            claim({"objective": name, "record": directory.name,
                "repo_key": source.get("repo_key") if source else None,
                "repository": source.get("repository") if source else None,
                "kind": "target_update" if action["kind"] in ("merge", "push", "pr_update") else "external_effect",
                "action": row["record_id"], "effects": identities, "effect_source": effect_source,
                "target": action_target, "outcome": row["outcome"]})
            if identities is None:
                unknown(directory.name, "effects_unknown", objective=name, fact="external_effect_identity",
                        fact_scope=fact_scope, reconciliation={"action": "reconcile_governor",
                        "record": row["record_id"], "condition": "Read back the admitted action and establish its actual downstream effects"})

    with read_deadline(budget):
        try:
            budget.check()
            if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
                raise PodError("unsafe_state", "State root is redirected")
            try:
                with os.scandir(root) as entries:
                    cohort = []
                    for entry in entries:
                        budget.check()
                        if not entry.name.startswith("."):
                            cohort.append(entry)
            except FileNotFoundError:
                cohort = []
            for entry in sorted(cohort, key=lambda row: row.name):
                budget.check()
                record = entry.name
                try:
                    record.encode("utf-8")
                except UnicodeError:
                    unknown("sha256:" + hashlib.sha256(os.fsencode(record)).hexdigest(), "path_encoding")
                    continue
                try:
                    if entry.is_symlink():
                        raise PodError("unsafe_state", "Record directory is redirected")
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    directory = Path(entry.path)
                    has_context = has_other = False
                    with os.scandir(directory) as contents:
                        for item in contents:
                            budget.check()
                            if item.name == "context.json":
                                has_context = True
                                break
                            has_other = has_other or item.name != ".lock"
                    if not has_context:
                        if has_other:
                            unknown(record, "unattributed")
                        continue
                    raw, identity = _primary(directory / "context.json")
                    try:
                        name = _membership(project, context, record, raw, known)
                    except PodError as exc:
                        if exc.code == "record_attribution_unknown" and _phase(raw) == "closed" and raw.get("admissions") == {}:
                            _admissions(raw, directory)  # Supported, structurally closed, and no hidden admission references.
                            journal, journal_id = _journal(directory / "governor.json")
                            if not any(row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN") for row in journal["actions"]):
                                attribution_gaps.append((record, directory, identity, journal_id))
                                continue
                        raise
                    source = raw.get("_facts_attribution") or (raw.get("checkpoint") or {}).get("worktree")
                    source = source if isinstance(source, dict) else next((item for item in known
                        if record_name(Path(item["worktree"]), item, listing._name(raw)) == record), None)
                    references = (raw.get("checkpoint") or {}).get("native_refs")
                    for ref in references if isinstance(references, list) else []:
                        if isinstance(ref, dict) and isinstance(ref.get("runId"), str) and source:
                            run_repositories.setdefault(ref["runId"], set()).add(source.get("repo_key"))
                            run_sources.setdefault(ref["runId"], []).append((directory, identity))
                    if name is None:
                        if effects or target and source and source.get("repository") == context["repository"]:
                            journal, journal_id = _journal(directory / "governor.json")
                            effect_claims(directory, raw, journal, source)
                            effect_sources.append((directory, journal_id, _effect_identity(journal, open_objective=False)))
                        continue
                    rows = _admissions(raw, directory)
                    phases[record] = _phase(raw) if raw["checkpoint"]["schema"] == "pod-checkpoint/v3" else "noncurrent"
                    place = raw["checkpoint"].get("worktree") or {}
                    for row in rows.values():
                        placement = _placement(row)
                        binding = row.get("native_binding") or {}
                        if row.get("abandoned_accounting") and binding.get("dispatchId"):
                            abandoned_dispatches.add(binding["dispatchId"])
                        if (placement.get("path") == listing._location(binding.get("worktreeId"))
                                and "branch" in placement and binding.get("dispatchId")):
                            placements_by_dispatch[binding["dispatchId"]] = (binding, placement)
                    placements = [_placement(row) for row in rows.values()]
                    paths = [place.get("path"), *(listing._location((row.get("native_binding") or {}).get("worktreeId"))
                              or listing._location(row.get("worktree")) or placement.get("path")
                              for row, placement in zip(rows.values(), placements))]
                    record_scopes[record] = {**scope, "worktrees": sorted({path for path in paths if path}),
                                            "branches": [place.get("branch"), *(p.get("branch") for p in placements)],
                                            "locations_complete": bool(paths) and all(paths)}
                    journal, journal_id = _journal(directory / "governor.json")
                    current_id = _claim_identity(raw, rows)
                    effect_id = _effect_identity(journal, open_objective=_phase(raw) == "open")
                    selected = name == objective
                    stamps[record] = [identity, journal_id] if selected else [current_id, effect_id]
                    sources.append((directory, identity, journal_id, current_id, effect_id, selected))
                    checkpoint = raw["checkpoint"]
                    refs = checkpoint.get("native_refs")
                    if not isinstance(refs, list):
                        raise PodError("state_unsupported", "Native Run references are unavailable")
                    for ref in refs:
                        if (not isinstance(ref, dict) or not isinstance(ref.get("runId"), str)
                                or not ref["runId"] or not isinstance(ref.get("runtime"), str)):
                            raise PodError("state_unsupported", "Native Run reference is malformed")
                        runs.setdefault(ref["runId"], [])
                        run_owners.setdefault(ref["runId"], []).append((record, name, ref["runtime"]))
                    place = checkpoint.get("worktree")
                    open_objective = _phase(raw) == "open"
                    if open_objective:
                        if not isinstance(place, dict) or not isinstance(place.get("path"), str):
                            unknown(record, "ownership_unavailable", objective=name)
                        claim({"objective": name, "record": record, "kind": "ownership",
                            "source_schema": checkpoint["schema"], "current_contract": checkpoint["schema"] == "pod-checkpoint/v3",
                            "recorded_owner": raw.get("owner"), "recorded_native_refs": refs,
                            "owner_source": "recorded_context",
                            "worktree": place.get("path") if isinstance(place, dict) else None,
                            "branch": place.get("branch") if isinstance(place, dict) else None,
                            "locations_complete": isinstance(place, dict) and bool(place.get("path")) and "branch" in place})
                        from .governor import _validate_branch
                        for unit_name, unit in journal["units"].items():
                            remote_branch = _validate_branch(unit.get("branch"))
                            if remote_branch is not None:
                                claim({"objective": name, "record": record, "kind": "ownership", "unit": unit_name,
                                       "branch": remote_branch["branch"], "remote": remote_branch["remote"],
                                       "worktree": None, "locations_complete": True})
                    for key, row in rows.items():
                        binding = row.get("native_binding")
                        if binding is not None:
                            if not binding_valid(binding):
                                raise PodError("state_unsupported", "Native claim identity is malformed")
                            runs.setdefault(binding["runId"], []).append((record, name, key,
                                {"native_binding": binding, "runtime": row.get("runtime"), "boundary": row.get("boundary")}))
                            run_owners.setdefault(binding["runId"], []).append((record, name, row.get("runtime")))
                        elif row["state"] not in ("closed", "deferred"):
                            claim({"objective": name, "record": record,
                                "kind": "writer" if row["role"] == "implement" else "assignment",
                                "admission": key, "worktree": listing._location(row.get("worktree"))
                                    or (_placement(row)).get("path"),
                                "branch": (_placement(row)).get("branch"),
                                "locations_complete": bool((_placement(row)).get("path")),
                                "liveness": "unknown", "boundary": row.get("boundary")})
                            unknown(record, "assignment_unbound", objective=name, admission=key)
                    effect_claims(directory, raw, journal, source or context)
                except PodError as exc:
                    unknown(record, exc.code)
                except OSError:
                    unknown(record, "unreadable")
            # Prefer the installed all-Run inventory for CE's unbound caller.
            # A bound caller retains exact-Run reads and the same outer deadline.
            fleet = _fleet()
            terminals = _terminals()
            if terminals.get("complete") is not True:
                raise PodError("native_resources_incomplete", "Current native resource coverage is incomplete")
            if fleet is not None and terminals["runtime"] != fleet["runtime"]:
                raise PodError("orca_runtime_changed", "Worker and terminal inventory runtimes differ")
            result["native_resources_complete"] = terminals.get("complete") is True
            native_groups = []
            if fleet is None:
                unknown(None, "native_inventory_scope", fact="all_run_coverage", reconciliation={
                    "action": "use_ce_unbound_read_backend",
                    "condition": "Read from CE's unbound backend while retaining the original caller in CE authority checks; do not mutate the native Run binding"})
            else:
                native_groups = [(None, fleet)]
            with gitio.worktree_inventory(project) as inventory:
                places = {row["worktree"]: row.get("branch", "").removeprefix("refs/heads/") or None
                          for row in gitio.worktree_rows(inventory)}
            for ownership in result["claims"]:
                if ownership["kind"] != "ownership" or "recorded_owner" not in ownership:
                    continue
                selection = {"worktree": str(Path(worktree).resolve()) if worktree else None,
                             "branch": branch, "target": target, "effects": effects or []}
                if not worktree and ownership.get("objective") != objective or not relevant(ownership, selection, context["repo_key"]):
                    continue
                observations = []
                for run in sorted({ref["runId"] for ref in ownership["recorded_native_refs"]}):
                    if run not in owner_reads:
                        try:
                            owner_reads[run] = _owner_observation(run, terminals, terminals["runtime"])
                        except PodError as exc:
                            owner_reads[run] = {"run": run, "status": "unknown", "code": exc.code}
                    observations.append(owner_reads[run])
                ownership["owner_observations"] = observations
                matches = bool(observations) and all(row.get("terminal") == ownership["recorded_owner"]
                    and row.get("worktree") == ownership.get("worktree") and row.get("live") is True for row in observations)
                ownership["owner_identity_matches"] = matches
                owners[ownership["record"]] = matches
            for requested_run, native in native_groups:
                budget.check()
                if native.get("complete") is not True:
                    unknown(None, "native_inventory_incomplete")
                    continue
                workers = {}
                for source in native["workers"]:
                    from .ledger import _native_assignment_settled
                    if (terminals.get("complete") is True and source.get("workerState") == "unsupervised"
                            and source.get("resource") is None and _native_assignment_settled({"result": {
                                "dispatch": {"id": source.get("dispatchId"), "runId": source.get("runId"),
                                             "taskId": source.get("taskId"), "status": source.get("dispatchStatus")},
                                "projection": source.get("projection")}})):
                        # A settled context-only Dispatch owns no supervised
                        # resource. Its terminal's current ownership is inventoried
                        # separately below; no process exit is inferred here.
                        continue
                    shown = _worker_fact(source)
                    path = shown["worktree"]
                    run = shown["run"]
                    if shown["liveness"] == "exited" and source.get("workerState") != "abandoned" and shown["dispatch"] not in abandoned_dispatches:
                        workers[shown["dispatch"]] = shown
                        continue
                    registered, in_repository = workspace(path, places)
                    branch_fact, branch_known = places.get(registered), registered is not None
                    binding, placement = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                    if not branch_known and _placement_matches(binding, shown):
                        branch_fact, branch_known = placement["branch"], True
                    if path and not branch_known:
                        # Native work in this repository may have been admitted by a
                        # different repository's objective. Inspect only that actual
                        # possible conflict, never every foreign admission history.
                        for directory, generation in run_sources.get(run, []):
                            if directory.name in scope_records_read:
                                continue
                            scope_records_read.add(directory.name)
                            candidate_raw, current_generation = _primary(directory / "context.json")
                            if current_generation != generation:
                                unknown(directory.name, "record_changed")
                                continue
                            try:
                                candidate_rows = _admissions(candidate_raw, directory)
                            except PodError as exc:
                                unknown(directory.name, exc.code, fact_scope={**scope, "worktree": path},
                                        fact="worker_placement")
                                continue
                            for candidate_row in candidate_rows.values():
                                binding = candidate_row.get("native_binding") or {}
                                placement = _placement(candidate_row)
                                if placement.get("path") == listing._location(binding.get("worktreeId")) and "branch" in placement:
                                    placements_by_dispatch[binding.get("dispatchId")] = (binding, placement)
                            bound, placed = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                            if _placement_matches(bound, shown):
                                branch_fact, branch_known = placed["branch"], True
                                break
                    native_effects = []
                    scope_binding, scope_placement = placements_by_dispatch.get(shown["dispatch"], ({}, {}))
                    actor_repo = context["repo_key"] if registered is not None else (
                        scope_placement.get("repo_key") if _placement_matches(scope_binding, shown) else None)
                    actor_scope = {"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                                   "locations_complete": bool(path) and branch_known}
                    if (source.get("workerState") == "abandoned" or shown["dispatch"] in abandoned_dispatches) and (effects or relevant(actor_scope,
                            {"worktree": str(Path(worktree).resolve()) if worktree else None,
                             "branch": branch, "target": target, "effects": effects or []}, context["repo_key"])):
                        try:
                            observed_claim = _native_claim(source, native["runtime"])
                            if observed_claim is None:
                                shown["liveness"] = "exited"
                            else:
                                shown["liveness"] = observed_claim["process"]
                                native_effects = observed_claim["effects"]
                                if native_effects and effects:
                                    unknown(None, "native_effect_identity_unknown", dispatch=shown["dispatch"],
                                            fact_scope={"effects": None}, fact="native_effect_identity",
                                            reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                            "condition": "Reconcile the W1-reported pending native effects and their targets"})
                        except PodError as exc:
                            unknown(None, exc.code, dispatch=shown["dispatch"],
                                    fact_scope={"effects": None} if effects else actor_scope,
                                    fact="current_native_claim", reconciliation={"action": "read_shared_current_claim",
                                    "condition": "Load the W1-owned claim reader and read the exact current worker observation"})
                    if requested_run is None and run not in runs and not in_repository:
                        if path is None and shown["liveness"] != "exited":
                            owners = run_repositories.get(run, set())
                            unknown(None, "native_location_unavailable", dispatch=shown["dispatch"],
                                    fact_scope={"repo_keys": sorted(key for key in owners if key), "run": run},
                                    reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                    "condition": "Resolve the native resource's repository and worktree ownership"})
                        continue
                    workers[shown["dispatch"]] = shown
                    if shown["liveness"] == "exited" and not native_effects:
                        continue
                    stamps["dispatch:" + shown["dispatch"]] = digest(shown)
                    claim({"kind": "writer", "run": run,
                        "dispatch": shown["dispatch"], "worktree": path,
                        "native_terminal": shown["terminal"], "native_resource": shown["resource"],
                        "branch": branch_fact, "liveness": shown["liveness"],
                        "native_effects": native_effects,
                        "repo_key": actor_repo, "locations_complete": bool(path) and branch_known,
                        "boundaries": [row.get("boundary") for _, _, _, row in runs.get(run, [])
                                       if row["native_binding"]["dispatchId"] == shown["dispatch"]],
                        "objectives": sorted({name for _, name, _ in run_owners.get(run, [])})})
                    if path is None:
                        unknown("run:" + str(run), "native_location_unavailable", dispatch=shown["dispatch"])
                    if shown["liveness"] not in ("live", "exited"):
                        unknown("run:" + str(run), "native_liveness_unverified", dispatch=shown["dispatch"],
                                fact="process_liveness", fact_scope={"repo_key": actor_repo, "worktree": path, "branch": branch_fact,
                                    "locations_complete": bool(path) and branch_known},
                                reconciliation={"command": ["orca", "orchestration", "worker-show", "--dispatch", shown["dispatch"], "--json"],
                                                "condition": "Read authoritative process liveness; resource release alone is not process exit"})
                checked_runs = [requested_run] if requested_run else list(runs)
                for run in checked_runs:
                    for record, name, runtime in set(run_owners[run]):
                        if native["runtime"] != runtime and (requested_run is not None or phases[record] == "open"):
                            if owners.get(record) and requested_run is None:
                                result.setdefault("recorded_runtime_changes", []).append({"record": record, "objective": name,
                                    "recorded_runtime": runtime, "observed_runtime": native["runtime"],
                                    "current_owner_identity_matches": True})
                            else:
                                unknown(record, "native_runtime_changed", objective=name, fact="current_run_identity",
                                        reconciliation={"action": "read_current_run_owner", "run": run,
                                            "condition": "Establish the current coordinator/resource identity; rebind only an objective whose write authority is needed"})
                    for record, name, key, row in runs[run]:
                        binding = row["native_binding"]
                        shown = workers.get(binding["dispatchId"])
                        if (not isinstance(shown, dict) or shown["run"] != run
                                or shown["task"] != binding["taskId"]
                                or shown["worker"] != binding["workerId"]
                                or shown["worktree"] != listing._location(binding["worktreeId"])
                                or shown["terminal"] != binding["terminalHandle"]):
                            unknown(record, "native_identity_unverified", objective=name, admission=key)
            from .retirement import _exited
            for terminal in terminals["terminals"]:
                path = terminal.get("worktreePath") or listing._location(terminal.get("worktreeId"))
                if path is None and not _exited(terminal):
                    unknown(None, "native_location_unavailable", fact="terminal_location",
                            fact_scope={"terminal": terminal.get("handle")},
                            reconciliation={"command": ["orca", "terminal", "show", "--terminal", terminal.get("handle"), "--json"],
                                            "condition": "Establish the current terminal's worktree and repository ownership"})
                    continue
                registered, in_repository = workspace(path, places)
                if not in_repository or _exited(terminal):
                    continue
                live = terminal.get("connected") is True and terminal.get("writable") is True and terminal.get("orphaned") is False
                claim({"kind": "writer", "native_terminal": terminal.get("handle"), "worktree": path,
                       "branch": places.get(registered), "locations_complete": registered is not None, "repo_key": context["repo_key"],
                       "liveness": "live" if live else "unverifiable", "source": "native_terminal_inventory"})
                if not live:
                    unknown(None, "native_liveness_unverified", fact="terminal_liveness",
                            terminal=terminal.get("handle"),
                            fact_scope={**scope, "worktree": path, "branch": places.get(registered), "locations_complete": registered is not None},
                            reconciliation={"command": ["orca", "terminal", "show", "--terminal", terminal.get("handle"), "--json"],
                                            "condition": "Establish the current terminal ownership and process state"})
            if target:
                observed = gitio.resolve_commit(project, target)
                result["target"] = {"ref": target, "requested_ref": requested_target, "commit": observed}
                if observed is None:
                    unknown(None, "target_unavailable")
                elif target_commit and target_commit != observed:
                    result["conflicts"].append({"kind": "target_changed", "expected": target_commit, "observed": observed})
            for record, directory, generation, journal_id in attribution_gaps:
                unchanged = (_primary(directory / "context.json")[1] == generation
                             and _journal(directory / "governor.json")[1] == journal_id)
                if fleet is not None and fleet.get("complete") is True and unchanged:
                    result.setdefault("unrelated_unknowns", []).append({"record": record,
                        "code": "record_attribution_unknown", "fact": "repository_attribution", "scope": {"record": record},
                        "relevance": "closed_without_admissions_or_outstanding_effects; current native claims independently inventoried",
                        "reconciliation": {"action": "normalize_attribution", "owner": "storage",
                                           "condition": "Preserve source-proven repository attribution without inventing placement"}})
                else:
                    unknown(record, "record_attribution_unknown")
            if objective:
                try:
                    budget.check()
                    result["proof"] = _proof(project, objective, candidate)
                    if result["proof"]["status"] == "unknown":
                        unknown(None, "proof_unverified", objective=objective)
                except PodError as exc:
                    result["proof"] = {"status": "unknown", "candidate": candidate, "reason": exc.code}
                    unknown(None, exc.code, objective=objective)
            for directory, journal_id, effect_id in effect_sources:
                budget.check()
                after_journal, after_id = _journal(directory / "governor.json")
                if after_id != journal_id and _effect_identity(after_journal, open_objective=False) != effect_id:
                    unknown(directory.name, "record_changed", fact="external_effects")
            for source_project, policy in policies.items():
                from .config import effective
                if effective(Path(source_project))["revision"] != policy["revision"]:
                    unknown(None, "effect_policy_changed", fact="effect_mapping")
            for directory, identity, journal_id, current_id, effect_id, selected in sources:
                budget.check()
                after_raw, after_id = _primary(directory / "context.json")
                after_journal, after_journal_id = _journal(directory / "governor.json")
                if after_id != identity or after_journal_id != journal_id:
                    if (selected or _claim_identity(after_raw, _admissions(after_raw, directory)) != current_id
                            or _effect_identity(after_journal, open_objective=_phase(after_raw) == "open") != effect_id):
                        unknown(directory.name, "record_changed")
            with os.scandir(root) if root.exists() else _empty_scan() as entries:
                after = {}
                for entry in entries:
                    budget.check()
                    if not entry.name.startswith("."):
                        after[entry.name] = entry
            before = {entry.name for entry in cohort}
            for added in sorted(set(after) - before):
                budget.check()
                entry = after[added]
                try:
                    raw, _ = _primary(Path(entry.path) / "context.json")
                    if _membership(project, context, added, raw, known) is None and not effects:
                        continue
                except (PodError, OSError):
                    pass
                unknown(added, "inventory_changed")
            known = {row[0].name for row in sources}
            if (before - set(after)) & known:
                unknown(None, "inventory_changed")
        except PodError as exc:
            unknown(None, exc.code)
        except OSError:
            unknown(None, "inventory_unreadable")
    if objective and result["proof"] is None:
        result["proof"] = {"status": "unknown", "candidate": candidate, "reason": "admission_incomplete"}
    request = {"worktree": str(Path(worktree).resolve()) if worktree else None,
               "branch": branch, "target": target, "effects": effects or []}
    result["scope"]["selection"] = {**request, "target": requested_target}
    all_unknowns = result["unknowns"]
    result["repository_complete"] = not all_unknowns
    result["unknowns"] = [row for row in all_unknowns if row.get("objective") == objective and objective
                          or relevant(row["scope"], request, context["repo_key"])]
    result["unrelated_unknowns"] = [*result.get("unrelated_unknowns", []),
                                    *(row for row in all_unknowns if row not in result["unknowns"])]
    unrelated = result["unrelated_unknowns"]
    result["unrelated_unknowns"] = {"count": len(unrelated), "by_code": dict(Counter(row["code"] for row in unrelated)),
        "references": {"records": sorted({row["record"] for row in unrelated
                                          if row.get("record") and not row["record"].startswith("run:")}),
                       "runs": sorted({row["record"][4:] for row in unrelated if str(row.get("record", "")).startswith("run:")}),
                       "dispatches": sorted({row["dispatch"] for row in unrelated if row.get("dispatch")}),
                       "terminals": sorted({row.get("terminal") or row["scope"]["terminal"] for row in unrelated
                                            if row.get("terminal") or row["scope"].get("terminal")})},
        "diagnostic": ["pod", "admission", "--repository", str(project), "--json"]}
    all_claims = result["claims"]
    result["claims"] = [row for row in all_claims if relevant(row, request, context["repo_key"])]
    result["claims"].sort(key=digest)
    result["unrelated_claims"] = len(all_claims) - len(result["claims"])
    for claim in result["claims"]:
        if claim.get("objective") == objective and claim["kind"] == "ownership":
            continue
        dimensions = []
        if worktree and claim.get("worktree") and overlap(claim["worktree"], str(Path(worktree).resolve())):
            dimensions.append("worktree")
        if branch and claim.get("branch") == branch:
            dimensions.append("branch")
        if target and claim.get("target") == target and claim.get("repo_key", context["repo_key"]) == context["repo_key"]:
            dimensions.append("target_update")
        if set(claim.get("effects") or []) & set(effects or []):
            dimensions.append("external_effect")
        if dimensions:
            result["conflicts"].append({"claim": claim, "dimensions": dimensions})
    result["complete"] = not result["unknowns"]
    active_records = {claim["record"] for claim in result["claims"] if claim.get("record")}
    for claim in result["claims"]:
        active_records.update(record_name(project, context, name) for name in claim.get("objectives", []))
    if objective:
        active_records.add(record_name(project, context, objective))
    # An inert finished record supplies no current exclusion dependency. Adding
    # retained finished history must not invalidate an otherwise identical read.
    stamps = {key: value for key, value in stamps.items()
              if key in active_records or key.startswith("dispatch:")}
    result["snapshot"] = digest({"scope": scope, "records": stamps, "claims": result["claims"], "target": result["target"],
        "request": {"objective": objective, "candidate": candidate, "worktree": worktree,
                    "branch": branch, "target": target, "effects": effects or []}})
    if expect and result["snapshot"] != expect:
        result["conflicts"].append({"kind": "snapshot_changed"})
    result["status"] = "unknown" if not result["complete"] else "blocked" if (result["conflicts"] or
        result["proof"] is not None and result["proof"]["status"] != "ready") else "observed"
    return public_decisions(result)


def _empty_scan():
    from contextlib import nullcontext
    return nullcontext(())


def render(result: dict) -> None:
    from .term import clean
    print(f"Pod admission facts: {result['status']}; complete: {result['complete']}")
    print(f"Claims: {len(result['claims'])}; conflicts: {len(result['conflicts'])}; unknowns: {len(result['unknowns'])}")
    for row in result["unknowns"]:
        print(f"  {clean(str(row['record']))}: {clean(row['code'])}")
    print(result["next_action"])


def query(project: Path, **kwargs) -> dict:
    """Read facts within one deadline; never grant authority to perform an effect."""
    timeout = kwargs.get("timeout", 10.0)
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
        raise PodError("admission_timeout", "Timeout must be greater than zero and at most 30 seconds")
    with read_deadline(Budget(float(timeout))):
        try:
            result = _query(project, **kwargs)
        except PodError as exc:
            if exc.code in ("admission_candidate", "admission_target", "admission_timeout"):
                raise
            scope = {"repository": None, "repo_key": None, "path": str(Path(kwargs.get("repository") or project).expanduser().absolute()),
                     "inventory": "pod_objectives_and_recorded_runs", "selection": {
                        "worktree": str(Path(kwargs["worktree"]).resolve()) if kwargs.get("worktree") else None,
                        "branch": kwargs.get("branch"), "target": kwargs.get("target"), "effects": kwargs.get("effects") or []}}
            result = {"schema": "pod-admission-query/v1", "status": "unknown", "scope": scope,
                      "complete": False, "claims": [], "conflicts": [],
                      "repository_complete": False, "native_resources_complete": False,
                      "unrelated_unknowns": {"count": 0, "by_code": {}, "references": {
                        "records": [], "runs": [], "dispatches": [], "terminals": []}},
                      "proof": {"status": "unknown", "candidate": kwargs["candidate"], "reason": exc.code}
                               if kwargs.get("objective") else None,
                      "snapshot": None, "target": None, "authorization": False,
                      "unknowns": [{"record": None, "code": exc.code, "scope": scope}],
                      "next_action": "Restore repository inventory access, then repeat the admission query"}
    from .orca import MAX_OUTPUT
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT:
        omitted = {"claims": len(result["claims"]), "conflicts": len(result["conflicts"]),
                   "unknowns": len(result["unknowns"])}
        result.update(status="unknown", complete=False, claims=[], conflicts=[], snapshot=None,
                      proof={"status": "unknown", "reason": "admission_projection_limit"} if kwargs.get("objective") else None,
                      unknowns=[row for row in result["unknowns"] if row["record"] is not None
                                and not row["record"].startswith("run:") and "admission" not in row][:MAX_CLAIMS])
        result["unknowns"].append({"record": None, "code": "admission_projection_limit", "scope": result["scope"],
                                   "omitted": omitted})
    return result

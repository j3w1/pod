"""Governance target selection, observations, retained evidence and currency."""

from __future__ import annotations

from pathlib import Path
import re

from . import gitio
from .errors import PodError
from .util import bounded_text


def _git(root: Path, argv: list[str], *, binary: bool = False):
    # Keep the ledger read hook used by existing governance boundary tests.
    from . import ledger
    return ledger._git(root, argv, binary=binary)


def git_is_ancestor(project: Path, commit: str, candidate: str) -> bool | None:
    from . import ledger
    return ledger.git_is_ancestor(project, commit, candidate)


_BASE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_OBJECT = gitio.OBJECT_ID


def user_direct_revision(value: object) -> bool:
    return isinstance(value, dict) and value.get("provenance") == "user_direct"


def canonical_bound_ref(project: Path, bound_ref: str | None, proposed: str | None,
                        *, branch: dict | None = None) -> str:
    """Accept an alias only when Git or the configured remote proves the same ref identity."""
    if bound_ref is None:
        return proposed or "origin/main"
    if isinstance(branch, dict):
        remote, base = branch.get("remote"), branch.get("base")
        if not isinstance(remote, str) or not isinstance(base, str):
            raise PodError("target_mismatch", "The prepared remote/base is not the bound governance target")
        target = f"refs/remotes/{remote}/{base}"
        configured = _git(project, ["config", "--get", f"remote.{remote}.url"])
        if (target != bound_ref or configured is None or configured.returncode != 0 or not configured.stdout.strip()
                or _resolve_commit(project, target) is None):
            raise PodError("target_mismatch", "The prepared remote/base is not the bound governance target")
    if proposed is None or proposed == bound_ref:
        return bound_ref
    if not isinstance(proposed, str) or not _BASE_REF.fullmatch(proposed) or ".." in proposed:
        raise PodError("target_mismatch", "Target alias is not a plain Git ref")
    named = _git(project, ["rev-parse", "--symbolic-full-name", "--verify", "--quiet",
                           "--end-of-options", proposed])
    if named is None or named.returncode != 0 or named.stdout.strip() != bound_ref:
        raise PodError("target_mismatch", "Target alias does not name the bound governance ref")
    return bound_ref


DELIVERY_FIELDS = {"seq", "record", "target_ref", "base", "result", "candidate", "tree",
                   "method", "authorization_reference", "provider"}


def _delivery_refusal(subcode: str, message: str, next_action: str, **referent) -> PodError:
    return PodError("delivery_unverified", f"{message}. Next: {next_action}",
                    {"detail": subcode, "next_action": next_action, "referent": referent})


def validate_delivery_record(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != DELIVERY_FIELDS
            or type(value.get("seq")) is not int or value["seq"] < 1
            or any(not isinstance(value.get(key), str) or not value[key]
                   for key in DELIVERY_FIELDS - {"seq", "provider"})
            or not isinstance(value.get("provider"), dict)):
        raise PodError("state_unsupported", "Verified delivery record is malformed")
    return value


def _delivery_object(project: Path, argv: list[str], *, subcode: str) -> str:
    found = _git(project, argv)
    value = found.stdout.strip() if found is not None and found.returncode == 0 else ""
    if not _OBJECT.fullmatch(value):
        raise _delivery_refusal(subcode, "The delivery Git object cannot be read",
                                "restore the target and candidate commits, then retry the delivery record")
    return value


def verify_delivery(project: Path, objective: str, map_state: dict, record_id: str, *, seq: int) -> dict:
    """Bind a Governor merge PASS to one exact target result without moving policy authority."""
    from .governor import _read_journal, _record_path, validate_authorization
    if not isinstance(record_id, str) or not 1 <= len(record_id) <= 128:
        raise _delivery_refusal("record_missing", "A bounded Governor record id is required",
                                "name the allowed merge PASS record from this objective")
    prior = map_state.get("delivery")
    if prior is not None:
        validate_delivery_record(prior)
        if prior["record"] != record_id:
            raise PodError("delivery_recorded", "This objective already has a different verified delivery record",
                           {"detail": "delivery_recorded", "record": prior["record"],
                            "next_action": "keep the existing delivery or obtain a new objective decision"})
        return dict(prior)
    journal = _read_journal(_record_path(project, objective))
    matches = [row for row in journal["actions"] if row.get("record_id") == record_id]
    if len(matches) != 1:
        raise _delivery_refusal("record_missing", "The merge record is absent from this objective",
                                "record the exact Governor merge outcome first", record=record_id)
    row = matches[0]
    if (row.get("decision") != "ALLOW" or row.get("outcome") != "PASS"
            or (row.get("action") or {}).get("kind") != "merge"):
        raise _delivery_refusal("record_not_merge_pass", "The record is not an allowed merge PASS",
                                "settle the exact governed merge before recording delivery", record=record_id)
    unit = journal["units"].get(row["action"].get("unit"))
    candidate = unit.get("candidate") if isinstance(unit, dict) else None
    if (not isinstance(candidate, dict) or not isinstance(candidate.get("commit"), str)
            or not isinstance(candidate.get("tree"), str)
            or not _OBJECT.fullmatch(candidate["commit"]) or not _OBJECT.fullmatch(candidate["tree"])
            or len(candidate["commit"]) != len(candidate["tree"])
            or row.get("candidate_id") != candidate.get("id")
            or row.get("commit") != candidate.get("commit")
            or row["action"].get("candidate") not in (candidate.get("id"), candidate.get("commit"))):
        raise _delivery_refusal("candidate_mismatch", "The merge row does not bind the prepared candidate",
                                "prepare and authorize the exact current candidate", record=record_id)
    try:
        authorization = validate_authorization(row["action"].get("authorization"),
                                               candidate=candidate["commit"], tree=candidate["tree"], kind="merge")
    except PodError:
        authorization = None
    if authorization is None:
        raise _delivery_refusal("authorization_invalid", "The merge authorization does not bind commit and tree",
                                "obtain the owner's merge decision for this exact candidate", record=record_id)
    candidate_tree = _delivery_object(project, ["rev-parse", "--verify", "--quiet",
                                                candidate["commit"] + "^{tree}"], subcode="candidate_unavailable")
    if candidate_tree != candidate["tree"]:
        raise _delivery_refusal("candidate_mismatch", "The prepared candidate tree differs from Git",
                                "prepare the candidate from a current Git observation", record=record_id)
    branch = unit.get("branch")
    governance = map_state["governance"]
    if not isinstance(branch, dict) or any(not isinstance(branch.get(key), str) for key in ("remote", "base")):
        raise _delivery_refusal("target_identity", "The delivery unit has no exact remote and base",
                                "prepare the unit on the bound governance target")
    target_ref = f"refs/remotes/{branch['remote']}/{branch['base']}"
    if target_ref != governance.get("base_ref"):
        raise _delivery_refusal("target_identity", "The merge target differs from bound governance",
                                "obtain a direct user decision for the intended target", target=target_ref)
    configured = _git(project, ["config", "--get", f"remote.{branch['remote']}.url"])
    current = _resolve_commit(project, target_ref)
    if (configured is None or configured.returncode != 0 or not configured.stdout.strip()
            or current is None):
        raise _delivery_refusal("target_unavailable", "The configured remote target cannot be observed",
                                "restore the remote URL and tracking ref, then retry delivery", target=target_ref)
    provider = row["receipt"].get("provider") or {}
    if not isinstance(provider, dict):
        raise _delivery_refusal("readback_mismatch", "Provider merge readback is malformed",
                                "reconcile the exact merge provider receipt")
    readback = provider.get("merge_commit")
    if readback is not None and (not isinstance(readback, str) or not _OBJECT.fullmatch(readback)):
        raise _delivery_refusal("readback_mismatch", "Provider merge readback is not a full commit id",
                                "record the exact provider merge commit")
    result = readback or current
    resolved = _resolve_commit(project, result)
    if resolved is None or resolved != result:
        raise _delivery_refusal("result_unavailable", "The delivered result commit is unavailable",
                                "restore the provider result commit and retry delivery", result=result)
    if readback is not None and current != result:
        from . import gitio
        if gitio.is_ancestor(project, result, current, resolve=_resolve_commit) is not True:
            raise _delivery_refusal("readback_mismatch", "The observed target does not contain provider readback",
                                    "inspect the merge readback and target ref", result=result, current=current)
    parents_read = _git(project, ["rev-list", "--parents", "-n", "1", result])
    words = parents_read.stdout.split() if parents_read is not None and parents_read.returncode == 0 else []
    if not words or words[0] != result or any(not _OBJECT.fullmatch(item) for item in words):
        raise _delivery_refusal("result_unavailable", "The result parents cannot be observed",
                                "restore the exact result commit and retry delivery", result=result)
    base, head = governance["base"], candidate["commit"]
    parents = words[1:]
    if result == head:
        from . import gitio
        method = "fast-forward" if gitio.is_ancestor(project, base, head, resolve=_resolve_commit) is True else None
    elif parents == [base, head]:
        method = "merge"
    elif parents == [base]:
        method = "squash"
    else:
        method = None
    if method is None:
        raise _delivery_refusal("result_not_exact", "The result is not an exact merge, squash or fast-forward",
                                "obtain a direct user decision for the exact new target snapshot", result=result)
    tree = _delivery_object(project, ["rev-parse", "--verify", "--quiet", result + "^{tree}"],
                            subcode="tree_unavailable")
    if tree != candidate["tree"]:
        raise _delivery_refusal("tree_mismatch", "The delivered tree differs from the authorized tree",
                                "inspect the merge result and authorize its exact tree", result=result)
    if provider.get("method") is not None and provider["method"] != method:
        raise _delivery_refusal("method_mismatch", "Provider merge method differs from the verified result",
                                "reconcile the provider merge readback", method=method)
    return {"seq": seq, "record": record_id, "target_ref": target_ref, "base": base,
            "result": result, "candidate": head, "tree": tree, "method": method,
            "authorization_reference": authorization["reference"], "provider": dict(provider)}


def _resolve_commit(root: Path, ref: str) -> str | None:
    if not isinstance(ref, str) or not _BASE_REF.fullmatch(ref) or ".." in ref:
        return None
    return gitio.resolve_commit(root, ref)


def _symbolic_ref(root: Path, ref: str) -> str | None:
    found = _git(root, ["symbolic-ref", "-q", ref])
    value = found.stdout.strip() if found is not None and found.returncode == 0 else ""
    return value if value.startswith(("refs/heads/", "refs/remotes/")) else None


def _governance_target(project: Path, proposed: str | None, *, user_direct: bool,
                       bound_ref: str | None) -> tuple[str | None, str | None]:
    """Select a branch identity independently of the proposed policy citation."""
    if bound_ref is not None:
        return bound_ref, None
    default = _symbolic_ref(project, "refs/remotes/origin/HEAD")
    if default is not None and (not default.startswith("refs/remotes/origin/")
                                or default == "refs/remotes/origin/HEAD"):
        default = None
    if proposed is None:
        return (default, None) if default is not None else (None, "target_selection_required")
    if not isinstance(proposed, str) or not _BASE_REF.fullmatch(proposed) or ".." in proposed:
        return None, "target_ref_invalid"
    named = _git(project, ["rev-parse", "--symbolic-full-name", "--verify", "--quiet",
                           "--end-of-options", proposed])
    selected = named.stdout.strip() if named is not None and named.returncode == 0 else ""
    if (proposed == "HEAD" or selected not in (default,)
            and not selected.startswith(("refs/heads/", "refs/remotes/"))):
        return None, "candidate_or_nonbranch_ref"
    if default is not None and (selected == default or
                                selected == "refs/heads/" + default.removeprefix("refs/remotes/origin/")
                                and _resolve_commit(project, selected) == _resolve_commit(project, default)):
        return default, None
    if not user_direct:
        return None, "target_selection_required"
    return selected, None


def governance_observation(project: Path, base_ref: str | None, *, at: str | None = None,
                           user_direct: bool = False, bound_ref: str | None = None,
                           selection: str | None = None) -> dict:
    """Read governance from the independently selected target, never the candidate."""
    from .github import repository_context
    from .obligations import GOVERNANCE_PATHS, MAX_GOVERNANCE_TEXT
    try:
        context = repository_context(project)
    except PodError:
        return {"status": "unavailable", "reason": "repository_unreadable"}
    if context["repo_key"] is None:
        return {"status": "no_repository"}
    target, reason = _governance_target(project, base_ref, user_direct=user_direct, bound_ref=bound_ref)
    if target is None:
        return {"status": "unavailable", "reason": reason}
    default = _symbolic_ref(project, "refs/remotes/origin/HEAD")
    chosen_by = selection or ("default" if target == default else "user_direct")
    if chosen_by == "default" and default != target:
        return {"status": "unavailable", "reason": "default_target_changed", "target_ref": target}
    current = _resolve_commit(project, target)
    commit = at if at is not None else current
    if commit is None or not _OBJECT.fullmatch(commit):
        return {"status": "unavailable", "reason": "base_ref_unresolved", "target_ref": target,
                "current": current}
    texts: dict[str, str | None] = {}
    for path in GOVERNANCE_PATHS:
        listed = _git(project, ["ls-tree", "-z", commit, "--", path])
        if listed is None or listed.returncode != 0:
            return {"status": "unavailable", "reason": "tree_unreadable", "current": current}
        entry = listed.stdout.split("\0")[0]
        if not entry:
            texts[path] = None
            continue
        meta, _, _name = entry.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            return {"status": "unavailable", "reason": "source_not_blob", "current": current}
        size = _git(project, ["cat-file", "-s", parts[2]])
        if size is None or size.returncode != 0 or not size.stdout.strip().isdigit() \
                or int(size.stdout.strip()) > MAX_GOVERNANCE_TEXT:
            return {"status": "unavailable", "reason": "source_oversized", "current": current}
        blob = _git(project, ["cat-file", "blob", parts[2]], binary=True)
        if blob is None or blob.returncode != 0:
            return {"status": "unavailable", "reason": "source_unreadable", "current": current}
        try:
            texts[path] = blob.stdout.decode("utf-8")
        except UnicodeError:
            return {"status": "unavailable", "reason": "source_not_text", "current": current}
    return {"status": "observed", "target_ref": target, "selection": chosen_by, "commit": commit,
            "current": current, "texts": texts}


MAX_GOVERNANCE_HISTORY = 64


def _history_limit() -> int:
    from . import ledger
    return ledger.MAX_GOVERNANCE_HISTORY


def _governance_history(prior: dict | None) -> dict:
    """Kernel-owned bounded history; never accept a caller's replacement."""
    history = (prior or {}).get("governance_history", {"candidates": [], "decisions": []})
    if (not isinstance(history, dict) or set(history) != {"candidates", "decisions"}
            or any(not isinstance(history[key], list) or len(history[key]) > _history_limit()
                   for key in ("candidates", "decisions"))
            or any(not isinstance(value, str) or not value or len(value) > 512
                   for value in history["candidates"])):
        raise PodError("state_unsupported", "Governance history is malformed")
    for row in history["decisions"]:
        if (not isinstance(row, dict)
                or set(row) != {"seq", "base_ref", "base", "provenance", "instruction", "kind"}
                or type(row["seq"]) is not int or row["seq"] < 1
                or not isinstance(row["base_ref"], str) or not _BASE_REF.fullmatch(row["base_ref"])
                or not isinstance(row["base"], str) or not _OBJECT.fullmatch(row["base"])
                or row["provenance"] != "user_direct" or row["kind"] not in ("initial", "snapshot", "retarget")
                or not isinstance(row["instruction"], str) or not 1 <= len(row["instruction"]) <= 1024):
            raise PodError("state_unsupported", "Governance decision history is malformed")
    return {"candidates": list(history["candidates"]),
            "decisions": [dict(row) for row in history["decisions"]]}


def _checkpoint_candidate(project: Path, candidate: str | None) -> str:
    identity = _resolve_commit(project, candidate)
    if identity is None:
        raise PodError("invalid_checkpoint",
                       "A mapped Git checkpoint candidate must resolve to a commit. "
                       "Next: name the current available candidate commit before writing the checkpoint",
                       {"detail": "candidate_identity_unavailable", "candidate": candidate})
    return identity


def _retain_governance_history(project: Path, map_state: dict, prior: dict | None,
                               candidate: str, proposed: dict, *, snapshot_authorized: bool) -> None:
    if map_state["governance"].get("base") is None:
        return
    history = _governance_history(prior)
    # Resolve aliases now, before a later checkout or ref movement changes their meaning.
    identity = _checkpoint_candidate(project, candidate)
    bounded_text(identity, name="candidate history", limit=512)
    if identity not in history["candidates"]:
        history["candidates"].append(identity)
    authority = proposed.get("revision_authority")
    old = (prior or {}).get("governance", {})
    current = map_state["governance"]
    if (isinstance(authority, dict) and authority.get("provenance") == "user_direct"
            and (prior is None or snapshot_authorized or old.get("base_ref") != current["base_ref"])):
        decision = {"base_ref": current["base_ref"], "base": current["base"],
                    "provenance": "user_direct", "instruction": authority["instruction"],
                    "kind": "initial" if prior is None else "snapshot" if snapshot_authorized else "retarget"}
        if not any({key: value for key, value in row.items() if key != "seq"} == decision
                   for row in history["decisions"]):
            history["decisions"].append({"seq": map_state["seq"], **decision})
    if any(len(history[key]) > _history_limit() for key in ("candidates", "decisions")):
        raise PodError("governance_unavailable", "Governance history is full; retained evidence cannot be evicted",
                       {"detail": "governance_history_full",
                        "next_action": "preserve the objective and report its governance history capacity",
                        "referent": {"limit": _history_limit()}})
    map_state["governance_history"] = history


def _independent_governance(project: Path, observed: dict, candidate: str | None,
                            admissions: dict, *, established: dict | None,
                            previous_candidate: str | None = None,
                            known_candidates: list[str] | None = None,
                            snapshot_authorized: bool = False) -> dict:
    """Preserve the selected bound target; reject adoption of known objective work.

    Initial target selection is the trusted authority input, including candidate
    equality. Git cannot prove pre-intake authorship. Later target observations
    are checked against that baseline, regardless of ref reuse or policy bytes.
    Adopting known objective work requires direct authority for the exact new
    snapshot; it does not prove that the project merged or authored that work.
    """
    if observed.get("status") != "observed" or established is None:
        return observed
    current_candidate = _checkpoint_candidate(project, candidate)
    base = established.get("base")
    if observed["commit"] == base:
        return observed

    # Exact user adoption establishes this snapshot even when older objects are
    # unavailable. It does not erase history or authorize a different later base.
    if snapshot_authorized:
        return observed

    def unavailable(reason: str, identity: str | None = None) -> dict:
        action = ("restore the missing Git evidence or obtain a fresh direct user decision for "
                  "this exact target snapshot, then write governance_refresh with "
                  "revision_authority and governance.base_ref/base")
        raise PodError("governance_unavailable", f"Governance comparison is unavailable: {reason}. Next: {action}",
                       {"detail": "governance_unavailable", "next_action": action,
                        "referent": {"reason": reason, "identity": identity,
                                     "base_ref": observed["target_ref"], "base": observed["commit"]}})

    if base is None or _resolve_commit(project, base) is None:
        return unavailable("bound_base_unavailable", base)
    values = [current_candidate, *(known_candidates or [])]
    if previous_candidate is not None:
        values.append(previous_candidate)
    for row in admissions.values():
        result = row.get("result") or {}
        values.extend(value for value in (row.get("candidate"), result.get("base"), result.get("head"),
                                          (row.get("report") or {}).get("result_commit"))
                      if value is not None)
    identities = set()
    for value in values:
        resolved = _resolve_commit(project, value)
        if resolved is None:
            return unavailable("objective_identity_unavailable", value)
        identities.add(resolved)
    target = observed["commit"]
    contains_work = False
    for identity in sorted(identities):
        baseline = git_is_ancestor(project, identity, base)
        if baseline is None:
            return unavailable("target_relation_unavailable", identity)
        if baseline:
            continue
        common = _git(project, ["merge-base", "--all", identity, target])
        if common is None or common.returncode not in (0, 1):
            return unavailable("target_relation_unavailable", identity)
        shared_commits = common.stdout.splitlines()
        if bool(shared_commits) != (common.returncode == 0):
            return unavailable("target_relation_unavailable", identity)
        for shared in shared_commits:
            if not _OBJECT.fullmatch(shared):
                return unavailable("target_relation_unavailable", identity)
            in_baseline = git_is_ancestor(project, shared, base)
            if in_baseline is None:
                return unavailable("target_relation_unavailable", shared)
            if not in_baseline:
                contains_work = True
    if contains_work:
        action = ("obtain a fresh direct user decision for this exact target snapshot, then write "
                  "governance_refresh with revision_authority and governance.base_ref/base")
        raise PodError("governance_unavailable",
                       "The changed governance target contains known objective work beyond its bound base. "
                       f"Next: {action}",
                       {"detail": "governance_unavailable", "next_action": action,
                        "referent": {"reason": "candidate_policy_target", "base_ref": observed["target_ref"],
                                     "base": target, "previous_base": base}})
    return observed


def require_governance_current(project: Path, map_state: dict | None) -> None:
    """Before new admission or a governed effect: the bound base is still the target's base."""
    if not isinstance(map_state, dict) or map_state.get("obligations") is None:
        return
    from .obligations import refuse
    governance = map_state["governance"]
    if governance.get("base_ref") is None:
        return
    if (governance.get("selection") == "default"
            and _symbolic_ref(project, "refs/remotes/origin/HEAD") != governance["base_ref"]):
        raise refuse("governance_changed", "governance_changed",
                     "the default target branch changed; a direct user revision must select a new target")
    current = _resolve_commit(project, governance["base_ref"])
    if current is None:
        raise refuse("governance_unavailable", "governance_unavailable",
                     "the governance base cannot be resolved; new work holds", base_ref=governance["base_ref"])
    delivery = map_state.get("delivery")
    if delivery is not None:
        validate_delivery_record(delivery)
        if current != delivery["result"]:
            raise refuse("governance_changed", "governance_changed",
                         "the target moved after the recorded delivery; obtain a direct user decision "
                         "for the new snapshot", recorded=delivery["result"][:12], current=current[:12])
        return
    if current != governance["base"]:
        raise refuse("governance_changed", "governance_changed",
                     "the target branch moved since governance was bound", bound=governance["base"][:12],
                     current=current[:12])

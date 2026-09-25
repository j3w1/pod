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
    if current != governance["base"]:
        raise refuse("governance_changed", "governance_changed",
                     "the target branch moved since governance was bound", bound=governance["base"][:12],
                     current=current[:12])

"""Read-only, objective-scoped cleanup evidence and guarded command plan."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
import re

from .errors import PodError
from . import cleanup_archives
from .gitio import OBJECT_ID, is_ancestor, read as git_read, resolve_commit, worktree_inventory, worktree_rows
from .governor import _read_journal, _record_path
from .ledger import read
from .orca import read_command
from .util import bounded_text, digest, exact

MAX_RESOURCES = 96
MAX_GIT_OUTPUT = 2_000_000
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")


def _git(project: Path, argv: list[str]) -> str | None:
    result = git_read(project, argv)
    if result is None or result.returncode != 0 or len(result.stdout) > MAX_GIT_OUTPUT:
        return None
    return result.stdout


def _worktrees(project: Path) -> list[dict]:
    """Keep the bounded full-list contract used by R113's registry observations."""
    try:
        with worktree_inventory(project) as inventory:
            output = inventory.read(MAX_GIT_OUTPUT + 1)
            if len(output) > MAX_GIT_OUTPUT:
                raise PodError("cleanup_unavailable", "Git worktree inventory cannot be read")
            rows = list(worktree_rows(StringIO(output)))
    except PodError as exc:
        raise PodError("cleanup_unavailable", "Git worktree inventory cannot be read") from exc
    if len(rows) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Git worktree inventory exceeds the cleanup bound")
    return rows


def _objective_worktrees(project: Path, paths: set[str], branches: set[str]) -> tuple[dict, dict, str | None]:
    """Keep objective rows and all checkouts of their refs, without a repository-size bound.

    Spool the complete read to disk: the first pass discovers current refs at the bound paths;
    the second sees every checkout of those refs, even before those paths in Git's output.
    """
    try:
        with worktree_inventory(project) as inventory:
            by_path, refs, main_path = {}, set(branches), None
            for row in worktree_rows(inventory):
                path = row.get("worktree")
                if main_path is None:
                    main_path = path
                if path in paths:
                    by_path[path] = row
                    if isinstance(row.get("branch"), str):
                        refs.add(row["branch"])
            inventory.seek(0)
            checkouts: dict[str, list[str]] = {}
            for row in worktree_rows(inventory):
                if row.get("branch") in refs and isinstance(row.get("worktree"), str):
                    checkouts.setdefault(row["branch"], []).append(row["worktree"])
            return by_path, checkouts, main_path
    except PodError as exc:
        raise PodError("cleanup_unavailable", "Git worktree inventory cannot be read") from exc


def _branch_refs(journal: dict, checkout_refs: set[str], selected_branches: set[str], *,
                 worker: bool) -> tuple[set[str], set[tuple[str, str]]]:
    local = {ref for ref in checkout_refs if ref.startswith("refs/heads/")}
    remotes = set()
    for unit in journal["units"].values():
        branch = unit.get("branch")
        if not isinstance(branch, dict):
            continue
        if isinstance(branch.get("branch"), str):
            ref = "refs/heads/" + branch["branch"]
            if not worker or ref in selected_branches:
                local.add(ref)
        remote = branch.get("remote")
        if not isinstance(remote, str) or not _BRANCH.fullmatch(remote):
            continue
        # A prepared name alone proves no remote ownership; only Governor publications count.
        for key in unit.get("published", {}):
            if isinstance(key, str) and key.startswith(remote + "/"):
                name = key[len(remote) + 1:]
                if not worker or "refs/heads/" + name in selected_branches:
                    remotes.add((remote, name))
    return local, remotes


def _selected_worker(state: dict, worker: object) -> tuple[dict | None, str | None]:
    if worker is None:
        return None, None
    selector = exact(worker, {"admission", "dispatch", "reuse_reason"}, set(), name="cleanup_worker")
    if ("admission" in selector) == ("dispatch" in selector):
        raise PodError("invalid_cleanup_worker", "Select exactly one admission or Dispatch")
    key = "admission" if "admission" in selector else "dispatch"
    identity = bounded_text(selector[key], name="cleanup_worker", limit=256)
    reason = (bounded_text(selector["reuse_reason"], name="cleanup_reuse_reason", limit=512)
              if "reuse_reason" in selector else None)
    matches = [row for row in state["admissions"].values()
               if (row.get("admission_id") if key == "admission" else
                   (row.get("native_binding") or {}).get("dispatchId")) == identity]
    if len(matches) != 1:
        raise PodError("cleanup_worker_unavailable", "Selector does not name one exact objective admission")
    return matches[0], reason


def _worker_facts(admission: dict, state: dict, port, reuse_reason: str | None, *,
                  check_reuse: bool = True, expected_owner: str | None = None) -> tuple[dict, list[str]]:
    """Fresh exact Dispatch release evidence; a missing terminal alone proves no release."""
    binding = admission.get("native_binding") or {}
    report = admission.get("report")
    facts = {"admission": admission["admission_id"], "dispatch": binding.get("dispatchId"),
             "report": report.get("observation_digest") if isinstance(report, dict) else None,
             "reuse_reason": reuse_reason}
    reasons = []
    if not isinstance(report, dict) or report.get("attempt") != binding.get("dispatchId"):
        reasons.append("worker report has not been recorded for this Dispatch")
    if reuse_reason is not None:
        reasons.append("reused next round: " + reuse_reason)
    if check_reuse:
        for other in state["admissions"].values():
            if other.get("reuse_of") == admission["admission_id"]:
                reasons.append("reused next round by admission " + other["admission_id"])
    expected_owner = expected_owner or binding.get("dispatchId")
    try:
        from .operations import _assignment_evidence
        shown = port.show_worker(binding.get("dispatchId"))
        evidence = _assignment_evidence(shown, admission)
        placement = evidence["placement"]
        facts.update(settled=evidence["settled"],
                     release=placement.get("native_release"),
                     ownership=placement.get("terminal_ownership"),
                     terminal=placement.get("terminal_readback"),
                     liveness=placement.get("native_liveness"))
        ownership = placement.get("terminal_ownership") or {}
        release = placement.get("native_release") or {}
        terminal = placement.get("terminal_readback") or {}
        native = shown["result"]
        resource = native.get("terminalResource")
        facts["worker_terminal"] = native["worker"].get("agentTerminalHandle")
        if (not isinstance(binding.get("terminalHandle"), str)
                or native["worker"].get("agentTerminalHandle") != binding["terminalHandle"]):
            reasons.append("worker terminal identity is unavailable or differs from its recorded binding")
        observed_terminal = native.get("terminal")
        if observed_terminal is not None and (not isinstance(observed_terminal, dict)
                or observed_terminal.get("handle") != binding.get("terminalHandle")
                or observed_terminal.get("worktreeId") != binding.get("worktreeId")
                or observed_terminal.get("connected") is not False):
            reasons.append("worker terminal readback is connected, incomplete or belongs to another binding")
        owner = ownership.get("ownerDispatchId") or release.get("ownerDispatchId")
        retained = placement.get("retained_reason")
        if retained:
            facts["retained_reason"] = retained
            reasons.append("native retention: " + str(retained))
        if not evidence["settled"]:
            reasons.append("worker Dispatch is active or unsettled")
        if placement.get("native_role") in ("coordinator", "setup"):
            reasons.append("native resource belongs to a coordinator or setup worker")
        if (resource is not None and (not isinstance(resource, dict)
                or resource.get("ownerDispatchId") != expected_owner
                or resource.get("worktreeId") != binding.get("worktreeId")
                or resource.get("terminalHandle") != binding.get("terminalHandle"))
                or owner not in (None, expected_owner)
                or release.get("ownerDispatchId") not in (None, expected_owner)):
            reasons.append("worker resource is reused or belongs to another native binding")
        if expected_owner != binding.get("dispatchId") and not isinstance(resource, dict):
            reasons.append("earlier worker resource transfer has not been proved")
        # Older supported worker-show replies omit terminalResource after release. Their exact-bound
        # projection.resource must positively say released twice, with exited liveness and the exact
        # worker terminal; an absent resource, ownership field or terminal never supplies that proof.
        released = (resource.get("ownershipState") == resource.get("releaseState") == "released"
                    if isinstance(resource, dict) else resource is None
                    and release.get("state") == release.get("releaseState") == "released")
        if (not released or release.get("state") not in (None, "released")
                or release.get("releaseState") not in (None, "released")):
            reasons.append("worker resource is not natively released")
        if ((placement.get("native_liveness") or {}).get("verdict") != "exited"
                or terminal.get("connected") is True):
            reasons.append("worker process is live or its exit is unverified")
    except (PodError, AttributeError, KeyError, TypeError):
        reasons.append("worker native settlement or release is unavailable")
    return facts, reasons


def _reuse_predecessors(state: dict, selected: dict) -> set[str]:
    """Only a recorded same-runtime, same-placement and same-terminal reuse chain."""
    current, seen = selected, set()
    binding = selected.get("native_binding") or {}
    placement = (selected.get("recovery") or {}).get("placement_binding")
    for _ in range(len(state["admissions"])):
        previous = current.get("reuse_of")
        if previous is None:
            return seen
        prior = state["admissions"].get(previous)
        other_binding = (prior or {}).get("native_binding") or {}
        if (previous in seen or previous == selected["admission_id"] or prior is None
                or prior.get("runtime") != selected.get("runtime")
                or (prior.get("recovery") or {}).get("placement_binding") != placement
                or not binding.get("terminalHandle")
                or any(other_binding.get(key) != binding.get(key) for key in ("worktreeId", "terminalHandle"))):
            return set()
        seen.add(previous)
        current = prior
    return set()


def _placement_history(state: dict, selected: dict, paths: set[str], port, *,
                       selected_released: bool) -> tuple[list[dict], list[str]]:
    """Earlier placement use is safe only with fresh release or exact transfer proof."""
    predecessors = _reuse_predecessors(state, selected) if selected_released else set()
    history, holds = [], []
    for admission in state["admissions"].values():
        placement = (admission.get("recovery") or {}).get("placement_binding") or {}
        if admission is selected or placement.get("path") not in paths:
            continue
        facts, reasons = _worker_facts(admission, state, port, None, check_reuse=False)
        proof = "released"
        if reasons and admission["admission_id"] in predecessors:
            facts, reasons = _worker_facts(admission, state, port, None, check_reuse=False,
                                           expected_owner=selected["native_binding"]["dispatchId"])
            proof = "transferred_to_selected"
        history.append({**facts, "proof": proof if not reasons else "unverified", "holds": sorted(set(reasons))})
        if reasons:
            holds.append("worktree is also bound to admission " + admission["admission_id"])
            holds.extend("other admission " + admission["admission_id"] + ": " + reason for reason in reasons)
    return history, holds


def _disposable(path: str) -> bool:
    parts = Path(path).parts
    return ("__pycache__" in parts or path.endswith(".pyc")
            or any(part in (".pytest_cache", ".mypy_cache", ".ruff_cache") for part in parts))


def _dirt(path: Path) -> tuple[list[str], bool]:
    output = _git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"])
    if output is None:
        return ["worktree status unavailable"], True
    data, index, changed = output.split("\0"), 0, []
    while index < len(data) and data[index]:
        entry = data[index]
        name = entry[3:]
        if entry[:2] != "!!" or not _disposable(name):
            changed.append(name)
        if entry[:1] in ("R", "C") or entry[1:2] in ("R", "C"):
            index += 1
            if index < len(data) and data[index]:
                changed.append(data[index])
        index += 1
    return sorted(set(changed))[:64], False


def _terminal_facts(path: Path, admissions: dict, *, orca_reader, native_port) -> tuple[list[dict], bool]:
    selector = "path:" + str(path)
    try:
        shown = orca_reader(["worktree", "show", "--worktree", selector, "--json"])
        worktree = shown["result"]["worktree"]
        listed = orca_reader(["terminal", "list", "--worktree", selector, "--json"])
        terminals = listed["result"]["terminals"]
        if (not isinstance(worktree, dict) or worktree.get("path") != str(path)
                or not isinstance(terminals, list) or listed["result"].get("truncated")):
            return [], True
    except (PodError, KeyError, TypeError):
        return [], True
    facts, unknown = [], False
    by_handle: dict[str, list[dict]] = {}
    for row in admissions.values():
        binding = row.get("native_binding") if isinstance(row, dict) else None
        handle = binding.get("terminalHandle") if isinstance(binding, dict) else None
        if isinstance(handle, str):
            by_handle.setdefault(handle, []).append(row)
    for terminal in terminals:
        if not isinstance(terminal, dict) or terminal.get("worktreePath") != str(path):
            unknown = True
            continue
        handle = terminal.get("handle")
        matches = by_handle.get(handle, [])
        if len(matches) != 1:
            facts.append({"handle": handle, "state": "foreign"})
            unknown = True
            continue
        admission = matches[0]
        dispatch = admission["native_binding"]["dispatchId"]
        resource = None
        try:
            from .operations import _assignment_evidence
            shown = native_port.show_worker(dispatch)
            settled = _assignment_evidence(shown, admission)["settled"] is True
            resource = shown["result"].get("terminalResource")
        except (PodError, AttributeError, KeyError, TypeError):
            settled = False
        # Settlement of the old Dispatch says nothing about who holds the terminal now: reuse moves it to
        # another Dispatch, and a user takeover or an external terminal is never reclaimable.
        resource = resource if isinstance(resource, dict) else {}
        ownership = {"state": resource.get("ownershipState"), "release": resource.get("releaseState"),
                     "owner": resource.get("ownerDispatchId")}
        owned = (resource.get("terminalHandle") == handle and ownership["owner"] == dispatch
                 and ownership["state"] == "owned" and ownership["release"] != "released")
        facts.append({"handle": handle, "admission": admission["admission_id"], "ownership": ownership,
                      "state": ("retained_reclaimable" if settled and owned
                                else "active_or_unknown" if not settled else "not_owned")})
        if not (settled and owned):
            unknown = True
    return facts, unknown or worktree.get("isMainWorktree") is True


def _target_branches(target_ref: object) -> set[str]:
    """The bound target's local branch, or any local branch named like a suffix of its remote-tracking ref."""
    if not isinstance(target_ref, str) or not target_ref.startswith(("refs/heads/", "refs/remotes/")):
        return set()
    if target_ref.startswith("refs/heads/"):
        return {target_ref}
    parts = target_ref.removeprefix("refs/remotes/").split("/")
    return {"refs/heads/" + "/".join(parts[index:]) for index in range(1, len(parts))}


def _resource(kind: str, name: str, identity: dict, classification: str, reasons: list[str],
              *, delete_argv: list[str] | None = None, archive_argv: list[str] | None = None,
              archive_verified: bool = False, archive_observations: list[dict] | None = None) -> dict:
    return {"id": kind + ":" + name, "kind": kind, "class": classification,
            "reasons": sorted(set(reasons)), "identity": identity,
            "delete_argv": delete_argv, "archive_argv": archive_argv,
            "archive_verified": archive_verified, "archives": archive_observations or []}


def _expect_change(expect: object, resources: list[dict], target: dict) -> tuple[str, dict]:
    fingerprints = {row["id"]: digest({key: value for key, value in row.items() if key != "fingerprint"})
                    for row in resources}
    plan_hash = digest([sorted(fingerprints.items()), target])
    if expect is not None:
        provided = exact(expect, {"plan", "resources"}, {"plan", "resources"}, name="cleanup_expect")
        older = provided["resources"]
        if (not isinstance(older, dict) or len(older) > MAX_RESOURCES
                or not isinstance(provided["plan"], str) or len(provided["plan"]) != 64
                or any(not isinstance(key, str) or not isinstance(value, str) or len(value) != 64
                       for key, value in older.items())):
            raise PodError("invalid_cleanup_expect", "Cleanup expect needs bounded resource fingerprints")
        if provided["plan"] != plan_hash or older != fingerprints:
            changed = sorted(key for key in older.keys() & fingerprints.keys()
                             if older[key] != fingerprints[key])
            added = sorted(fingerprints.keys() - older.keys())
            removed = sorted(older.keys() - fingerprints.keys())
            raise PodError("cleanup_changed", "Cleanup plan changed; reread the plan before deleting",
                           {"detail": "cleanup_changed", "changed": changed,
                            "added": added, "removed": removed,
                            "next_action": "rerun cleanup-plan and review the changed resources"})
    return plan_hash, fingerprints


def _successor_paths(project: Path, state: dict) -> tuple[str | None, set[str] | None]:
    """Worktrees the successors of a continued objective record (at most eight); None when unreadable."""
    link = state.get("continued_by")
    if not isinstance(link, dict):
        return None, set()
    paths, seen, current = set(), set(), state
    for _ in range(8):
        link = current.get("continued_by")
        if not isinstance(link, dict):
            return state["continued_by"]["objective"], paths
        if link["objective"] in seen:
            return state["continued_by"]["objective"], None
        seen.add(link["objective"])
        try:
            current = read(project, link["objective"])
        except PodError:
            current = None
        if current is None:
            return state["continued_by"]["objective"], None
        bound = (current.get("checkpoint") or {}).get("worktree")
        if isinstance(bound, dict) and isinstance(bound.get("path"), str):
            paths.add(bound["path"])
        else:
            return state["continued_by"]["objective"], None  # An unplaced successor may work anywhere.
        for admission in current["admissions"].values():
            placement = (admission.get("recovery") or {}).get("placement_binding")
            if isinstance(placement, dict) and isinstance(placement.get("path"), str):
                paths.add(placement["path"])
    return state["continued_by"]["objective"], None


def plan(project: Path, objective: str, *, expect: object = None, archives: object = None, worker: object = None,
         orca_reader=read_command, native_port=None) -> dict:
    """Observe only resources named by this objective; return guarded argv, never execute it.

    An older record closed by evidence is read through the evidence path's documented fields before the validating
    reader (R94, R146); what it does not document reads as unknown, so its resources stay protected. An older record
    without that closure keeps the ordinary reader's refusal."""
    from .evidence_closure import closed_older_state
    from .operations import OrcaPort
    older = closed_older_state(project, objective)
    state = older if older is not None else read(project, objective)
    if state is None or not isinstance(state.get("checkpoint"), dict):
        raise PodError("cleanup_unavailable", "Objective checkpoint is unavailable")
    checkpoint = state["checkpoint"]
    if older is not None and worker is not None:
        raise PodError("cleanup_worker_unavailable",
                       "Older evidence-closed records do not document worker report and release evidence")
    selected, reuse_reason = _selected_worker(state, worker)
    port = native_port or OrcaPort(project)
    worker_facts, worker_holds = (_worker_facts(selected, state, port, reuse_reason)
                                  if selected else (None, []))
    try:
        journal = _read_journal(_record_path(project, objective))
    except PodError:
        if older is None:
            raise
        journal = {"actions": [], "units": {}}  # An older journal this Pod cannot validate names no resource.
    archive_rows = cleanup_archives.archive_rows(archives)
    paths = set()
    expected_branches: dict[str, set[str]] = {}
    bound = checkpoint.get("worktree")
    if selected is None and isinstance(bound, dict) and isinstance(bound.get("path"), str):
        paths.add(bound["path"])
        if isinstance(bound.get("branch"), str):
            expected_branches.setdefault(bound["path"], set()).add(bound["branch"])
    for admission in state["admissions"].values():
        if selected is not None and admission is not selected:
            continue
        placement = (admission.get("recovery") or {}).get("placement_binding")
        if isinstance(placement, dict) and isinstance(placement.get("path"), str):
            paths.add(placement["path"])
            if isinstance(placement.get("branch"), str):
                expected_branches.setdefault(placement["path"], set()).add(placement["branch"])
    if selected is not None:
        if len(paths) != 1:
            raise PodError("cleanup_worker_unavailable", "Worker has no exact recorded placement binding")
        if not expected_branches.get(next(iter(paths))):
            worker_holds.append("worker placement branch identity is unavailable")
        if not isinstance(bound, dict) or not isinstance(bound.get("path"), str):
            worker_holds.append("objective integration worktree binding is unavailable")
        elif bound["path"] in paths:
            worker_holds.append("selected worker uses the objective integration worktree")
        history, history_holds = _placement_history(state, selected, paths, port,
                                                    selected_released=not worker_holds)
        worker_facts["placement_history"] = history
        worker_holds.extend(history_holds)
    if len(paths) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Objective cleanup resource bound is exceeded")
    selected_branches = {"refs/heads/" + branch for branches in expected_branches.values() for branch in branches}
    journal_branches = {"refs/heads/" + branch["branch"] for unit in journal["units"].values()
                        if isinstance(branch := unit.get("branch"), dict) and isinstance(branch.get("branch"), str)}
    by_path, checkouts, main_path = _objective_worktrees(project, paths, selected_branches | journal_branches)
    checkout_refs = {row["branch"] for row in by_path.values() if isinstance(row.get("branch"), str)}
    local_branches, remote_names = _branch_refs(journal, checkout_refs, selected_branches,
                                               worker=selected is not None)
    if len(paths) + len(local_branches) + len(remote_names) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Objective cleanup resource bound is exceeded")
    repository = cleanup_archives.repository_observation(project)
    git_resources = {}
    for path_text in sorted(paths):
        row = by_path.get(path_text) or {}
        ref = row.get("branch")
        tip = resolve_commit(project, ref) if isinstance(ref, str) else None
        git_resources[path_text] = cleanup_archives.resource_observation(Path(path_text), ref, tip, repository)
    # Keep recovery evidence outside every selected checkout/admin, including a shared admin root.
    # This scope does not purport to bound an external consumer's cache or other native effects.
    excluded = [Path(path) for path in paths]
    if "common_dir" in repository:
        excluded.append(Path(repository["common_dir"]["path"]))
    excluded += [Path(facts["git_dir"]["path"]) for facts in git_resources.values() if "git_dir" in facts]
    archive_by_basis = {}

    def archive_facts(ref, facts):
        # A worktree and its branch share a ref, not a preservation obligation.
        key = ref, digest(facts)
        if key not in archive_by_basis:
            archive_by_basis[key] = [cleanup_archives.verify(project, row, facts, excluded)
                                     for row in archive_rows if row["ref"] == ref]
        return archive_by_basis[key]
    governance = checkpoint.get("governance") or {}
    target_ref = governance.get("base_ref")
    target = resolve_commit(project, target_ref) if isinstance(target_ref, str) else None
    target_branches = _target_branches(target_ref)
    delivered = checkpoint.get("delivery") or {}
    delivered_result = delivered.get("result")
    # Later merges advance the target; it moved after delivery only when it no longer contains the delivered result.
    moved = bool(delivered) and (target is None or not isinstance(delivered_result, str)
                                 or is_ancestor(project, delivered_result, target) is not True)
    default = _git(project, ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"])
    default_branch = default.strip().removeprefix("refs/remotes/origin/") if default else None
    stashes = _git(project, ["stash", "list", "--format=%gd:%H"])
    stash_unknown = stashes is None
    stash_present = bool(stashes and stashes.strip())
    merged = {row.get("commit") for row in journal["actions"]
              if row.get("decision") == "ALLOW" and row.get("outcome") == "PASS"
              and row.get("action", {}).get("kind") == "merge"}
    if delivered.get("candidate"):
        merged.add(delivered["candidate"])
    resources = []
    successor, successor_paths = _successor_paths(project, state)
    for path_text in sorted(paths):
        path = Path(path_text)
        terminals = []
        dirty = []
        row = by_path.get(path_text)
        branch_ref = row.get("branch") if row else None
        branch = branch_ref.removeprefix("refs/heads/") if isinstance(branch_ref, str) else None
        tip = resolve_commit(project, branch_ref) if isinstance(branch_ref, str) else None
        shared = sorted(other for other in checkouts.get(branch_ref, []) if other != path_text)
        git_facts = git_resources[path_text]
        reasons, classification = [], "integrated"
        if not path.is_absolute() or path.is_symlink() or row is None or not path.is_dir():
            classification, reasons = "protected", ["worktree binding is unavailable or foreign"]
        elif successor is not None and (successor_paths is None or path_text in successor_paths):
            classification, reasons = "protected", [f"recorded by successor objective {successor}"]
        elif path_text == main_path or branch in ("main", default_branch) or branch_ref in target_branches:
            classification, reasons = "protected", ["main checkout, or the default or target branch"]
        elif expected_branches.get(path_text) and expected_branches[path_text] != {branch}:
            classification, reasons = "protected", ["worktree branch changed from its objective binding"]
        elif shared:
            classification, reasons = "protected", ["the branch is also checked out in another worktree"]
        elif moved or target is None or tip is None:
            classification, reasons = "protected", ["target or branch identity is unavailable"]
        elif not git_facts["complete"]:
            classification, reasons = "protected", git_facts["holds"]
        else:
            terminals, unknown_terminal = _terminal_facts(path, state["admissions"],
                                                            orca_reader=orca_reader, native_port=port)
            dirty, status_unknown = _dirt(path)
            if unknown_terminal:
                classification, reasons = "protected", ["a terminal here is active, user-owned, external, "
                                                        "held by another Dispatch or of unknown ownership"]
            elif status_unknown or stash_unknown:
                classification, reasons = "protected", ["worktree status or stash read is uncertain"]
            elif dirty or stash_present:
                classification, reasons = "unique", (["worktree has data: " + ", ".join(dirty[:8])] if dirty else [])
                if stash_present:
                    reasons.append("repository stash is present")
            elif is_ancestor(project, tip, target) is not True and tip not in merged:
                classification, reasons = "unique", ["branch has commits outside the observed target"]
            elif git_facts["additional_roots"]:
                classification, reasons = "unique", ["worktree has additional local recovery roots"]
        if worker_holds:
            classification, reasons = "protected", reasons + worker_holds
        ref = branch_ref if isinstance(branch_ref, str) else None
        observed_archives = archive_facts(ref, git_facts)
        archive_complete = any(row["verified"] and row["covers_resource"] for row in observed_archives)
        archive_argv = (["git", "bundle", "create", "<dir>/" + (branch or "worktree").replace("/", "_") +
                         ".bundle", ref] if classification == "unique" and ref else None)
        deletable = (classification == "integrated"
                     or classification == "unique" and archive_complete and not dirty and not stash_present)
        resources.append(_resource("worktree", path_text,
                                   {"path": path_text, "branch": branch_ref, "tip": tip,
                                    "terminals": terminals, "shared_with": shared, "git": git_facts,
                                    **({"worker": worker_facts} if selected is not None else {})}, classification, reasons,
                                   delete_argv=["orca", "worktree", "rm", "--worktree", "path:" + path_text,
                                                "--json"] if deletable else None,
                                   archive_argv=archive_argv, archive_verified=archive_complete,
                                   archive_observations=observed_archives))
    for ref in sorted(local_branches):
        branch = ref.removeprefix("refs/heads/")
        tip = resolve_commit(project, ref)
        associated = [row for row in resources if row["kind"] == "worktree" and row["identity"]["branch"] == ref]
        checked_out = sorted(checkouts.get(ref, []))
        reasons, classification = [], "integrated"
        if branch in ("main", default_branch) or ref == target_ref or ref in target_branches:
            classification, reasons = "protected", ["main, default or target branch"]
        elif not associated:
            classification, reasons = "protected", ["creation by this objective is unproven"]
        elif any(other not in paths for other in checked_out):
            classification, reasons = "protected", ["the branch is checked out in a worktree outside this objective"]
        elif any(row["class"] == "protected" for row in associated):
            classification, reasons = "protected", ["associated worktree is protected"]
        elif tip is None or target is None or moved:
            classification, reasons = "protected", ["branch or target identity is unavailable"]
        elif stash_present or associated and any(row["class"] == "unique" for row in associated):
            classification, reasons = "unique", ["branch has unique work or a repository stash"]
        elif is_ancestor(project, tip, target) is not True and tip not in merged:
            classification, reasons = "unique", ["branch has commits outside the observed target"]
        if selected is not None:
            reasons += sorted({reason for row in associated if row["delete_argv"] is None
                               for reason in row["reasons"]})
        git_facts = cleanup_archives.reference_observation(project, ref, tip, repository)
        observed_archives = archive_facts(ref, git_facts)
        verified = any(row["verified"] and row["covers_resource"] for row in observed_archives)
        removed_with_worktree = bool(associated and any(row["delete_argv"] for row in associated))
        deletable = (classification == "integrated" or classification == "unique" and verified
                     and not associated and not stash_present)
        resources.append(_resource("local_branch", ref, {"ref": ref, "tip": tip, "checked_out": checked_out,
                                                        "git": git_facts},
                                   classification,
                                   reasons + (["Orca also removes the merged local branch"]
                                              if removed_with_worktree else []),
                                   delete_argv=(["git", "update-ref", "-d", ref, tip]
                                                if deletable and not removed_with_worktree and tip else None),
                                   archive_argv=(["git", "bundle", "create", "<dir>/" +
                                                  branch.replace("/", "_") + ".bundle", ref]
                                                 if classification == "unique" else None),
                                   archive_verified=verified, archive_observations=observed_archives))
    for remote, branch in sorted(remote_names):
        ref = "refs/heads/" + branch
        output = _git(project, ["ls-remote", remote, ref]) if _BRANCH.fullmatch(branch) else None
        rows = [line.split() for line in output.splitlines()] if output is not None else []
        tip = next((parts[0] for parts in rows if len(parts) == 2 and parts[1] == ref
                    and OBJECT_ID.fullmatch(parts[0])), None)
        classification, reasons = "merged_remote_head", []
        if branch in ("main", default_branch) or target_ref == f"refs/remotes/{remote}/{branch}":
            classification, reasons = "protected", ["default or target remote branch"]
        elif output is None or len(rows) != 1 or tip is None or target is None or moved:
            classification, reasons = "protected", ["remote tip or target is uncertain"]
        elif tip not in merged and is_ancestor(project, tip, target) is not True:
            classification, reasons = "unique", ["remote tip has unique commits"]
        archive_ref = "refs/remotes/" + remote + "/" + branch
        git_facts = cleanup_archives.reference_observation(project, archive_ref, tip, repository)
        observed_archives = archive_facts(archive_ref, git_facts)
        verified = any(row["verified"] and row["covers_resource"] for row in observed_archives)
        if selected is not None:
            held = [row for row in resources if row["kind"] == "worktree" and row["delete_argv"] is None]
            if held:
                classification = "protected"
                reasons += sorted({reason for row in held for reason in row["reasons"]})
        resources.append(_resource("remote_branch", remote + "/" + branch,
                                   {"remote": remote, "ref": ref, "tip": tip, "git": git_facts}, classification, reasons,
                                   delete_argv=(["git", "push", "--force-with-lease=" + ref + ":" + tip,
                                                remote, ":" + ref]
                                                if classification == "merged_remote_head"
                                                or classification == "unique" and verified else None),
                                   archive_argv=(["git", "bundle", "create", "<dir>/" +
                                                  remote + "_" + branch.replace("/", "_") + ".bundle",
                                                  archive_ref]
                                                 if classification == "unique" else None),
                                   archive_verified=verified, archive_observations=observed_archives))
    if len(resources) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Objective cleanup resource bound is exceeded")
    resources.sort(key=lambda row: row["id"])
    observed_target = {"ref": target_ref, "commit": target, "moved_after_delivery": moved}
    plan_hash, fingerprints = _expect_change(expect, resources, observed_target)
    for row in resources:
        row["fingerprint"] = fingerprints[row["id"]]
    from .standing import cleanup_projection
    from .evidence_closure import present
    from .ledger import objective_root
    # An objective closed by evidence reads as closed, like one its coordinator closed (R145).
    basis = ("evidence" if present(objective_root(project, objective))
             else "coordinator" if checkpoint.get("closure") else None)
    notes = ["Read-only plan; owner consent precedes commands.",
             "Orca worktree removal also removes a merged local branch.",
             "Verify each unique-work archive before removing its original ref.",
             "archive_argv creates branch-only evidence; additional recovery roots require a complete "
             "recovery bundle and expanded archive input."]
    if older is not None:
        notes.append("Older record closed by evidence, read through its documented fields only: its target and "
                     "delivery are not documented, so resources that depend on them stay protected.")
    return {"schema": "pod-cleanup-plan/v1", "objective": objective, "closure_basis": basis,
            **({"worker": worker_facts} if selected is not None else {}),
            "record_class": "older" if older is not None else "current",
            "standing_authorization": cleanup_projection(state, resources),
            "target": observed_target,
            "resources": resources, "digest": plan_hash,
            "expect": {"plan": plan_hash, "resources": fingerprints},
            "notes": notes}

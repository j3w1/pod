"""Read-only, objective-scoped cleanup evidence and guarded command plan."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile

from .errors import PodError
from .gitio import OBJECT_ID, is_ancestor, read as git_read, resolve_commit
from .governor import _read_journal, _record_path
from .ledger import read
from .orca import read_command
from .util import digest, exact

MAX_RESOURCES = 96
MAX_GIT_OUTPUT = 2_000_000
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")


def _git(project: Path, argv: list[str]) -> str | None:
    result = git_read(project, argv)
    if result is None or result.returncode != 0 or len(result.stdout) > MAX_GIT_OUTPUT:
        return None
    return result.stdout


def _worktrees(project: Path) -> list[dict]:
    output = _git(project, ["worktree", "list", "--porcelain", "-z"])
    if output is None:
        raise PodError("cleanup_unavailable", "Git worktree inventory cannot be read")
    rows, current = [], {}
    for field in output.split("\0"):
        if not field:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        rows.append(current)
    if len(rows) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Git worktree inventory exceeds the cleanup bound")
    return rows


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


def _archive_verified(project: Path, value: dict) -> bool:
    path = Path(value["path"])
    try:
        readable = (path.is_absolute() and not path.is_symlink() and path.is_file()
                    and path.stat().st_size <= 128 * 1024 * 1024)
    except OSError:
        readable = False
    if not readable:
        return False
    ref, tip = value["ref"], value["tip"]
    named = (ref.removeprefix("refs/heads/") if isinstance(ref, str) and ref.startswith("refs/heads/")
             else ref.removeprefix("refs/remotes/") if isinstance(ref, str) and ref.startswith("refs/remotes/")
             else "")
    if (not named or not _BRANCH.fullmatch(named)
            or not isinstance(tip, str) or not OBJECT_ID.fullmatch(tip)):
        return False
    def run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "--no-optional-locks", *argv], cwd=cwd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
    verified = run(["bundle", "verify", str(path)], cwd=project)
    listed = run(["bundle", "list-heads", str(path)], cwd=project)
    if (verified is None or verified.returncode or listed is None or listed.returncode
            or len(listed.stdout) > MAX_GIT_OUTPUT
            or not any(line.split() == [tip, ref] for line in listed.stdout.splitlines())):
        return False
    with tempfile.TemporaryDirectory(prefix="pod-cleanup-") as temporary:
        restored = Path(temporary) / "restored.git"
        cloned = run(["clone", "--bare", "-q", str(path), str(restored)], cwd=project)
        if cloned is None or cloned.returncode:
            return False
        found = run(["-C", str(restored), "rev-parse", "--verify", ref + "^{commit}"], cwd=project)
        return found is not None and found.returncode == 0 and found.stdout.strip() == tip


def _resource(kind: str, name: str, identity: dict, classification: str, reasons: list[str],
              *, delete_argv: list[str] | None = None, archive_argv: list[str] | None = None,
              archive_verified: bool = False) -> dict:
    return {"id": kind + ":" + name, "kind": kind, "class": classification,
            "reasons": sorted(set(reasons)), "identity": identity,
            "delete_argv": delete_argv, "archive_argv": archive_argv,
            "archive_verified": archive_verified}


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


def plan(project: Path, objective: str, *, expect: object = None, archives: object = None,
         orca_reader=read_command, native_port=None) -> dict:
    """Observe only resources named by this objective; return guarded argv, never execute it."""
    from .operations import OrcaPort
    state = read(project, objective)
    if state is None or not isinstance(state.get("checkpoint"), dict):
        raise PodError("cleanup_unavailable", "Objective checkpoint is unavailable")
    checkpoint = state["checkpoint"]
    journal = _read_journal(_record_path(project, objective))
    archive_rows = [] if archives is None else archives
    if not isinstance(archive_rows, list) or len(archive_rows) > MAX_RESOURCES:
        raise PodError("invalid_cleanup_archives", "Archives are a bounded list")
    archive_by_ref = {}
    for item in archive_rows:
        row = exact(item, {"path", "ref", "tip"}, {"path", "ref", "tip"}, name="cleanup_archive")
        if not all(isinstance(row[key], str) and len(row[key]) <= 4096 for key in row):
            raise PodError("invalid_cleanup_archives", "Archive path, ref and tip are bounded text")
        archive_by_ref[(row["ref"], row["tip"])] = _archive_verified(project, row)
    worktrees = _worktrees(project)
    by_path = {row.get("worktree"): row for row in worktrees if isinstance(row.get("worktree"), str)}
    checkouts: dict[str, list[str]] = {}
    for row in worktrees:
        if isinstance(row.get("branch"), str) and isinstance(row.get("worktree"), str):
            checkouts.setdefault(row["branch"], []).append(row["worktree"])
    main_path = next((row.get("worktree") for row in worktrees if row.get("worktree")), None)
    paths = set()
    expected_branches: dict[str, set[str]] = {}
    bound = checkpoint.get("worktree")
    if isinstance(bound, dict) and isinstance(bound.get("path"), str):
        paths.add(bound["path"])
        if isinstance(bound.get("branch"), str):
            expected_branches.setdefault(bound["path"], set()).add(bound["branch"])
    for admission in state["admissions"].values():
        placement = (admission.get("recovery") or {}).get("placement_binding")
        if isinstance(placement, dict) and isinstance(placement.get("path"), str):
            paths.add(placement["path"])
            if isinstance(placement.get("branch"), str):
                expected_branches.setdefault(placement["path"], set()).add(placement["branch"])
    governance = checkpoint.get("governance") or {}
    target_ref = governance.get("base_ref")
    target = resolve_commit(project, target_ref) if isinstance(target_ref, str) else None
    target_branches = _target_branches(target_ref)
    delivered = checkpoint.get("delivery") or {}
    moved = bool(delivered and target != delivered.get("result"))
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
    resources, covered_branches = [], set()
    port = native_port or OrcaPort(project)
    for path_text in sorted(paths):
        path = Path(path_text)
        terminals = []
        dirty = []
        row = by_path.get(path_text)
        branch_ref = row.get("branch") if row else None
        branch = branch_ref.removeprefix("refs/heads/") if isinstance(branch_ref, str) else None
        tip = resolve_commit(project, branch_ref) if isinstance(branch_ref, str) else None
        shared = sorted(other for other in checkouts.get(branch_ref, []) if other != path_text)
        reasons, classification = [], "integrated"
        if not path.is_absolute() or path.is_symlink() or row is None or not path.is_dir():
            classification, reasons = "protected", ["worktree binding is unavailable or foreign"]
        elif path_text == main_path or branch in ("main", default_branch) or branch_ref in target_branches:
            classification, reasons = "protected", ["main checkout, or the default or target branch"]
        elif expected_branches.get(path_text) and expected_branches[path_text] != {branch}:
            classification, reasons = "protected", ["worktree branch changed from its objective binding"]
        elif shared:
            classification, reasons = "protected", ["the branch is also checked out in another worktree"]
        elif moved or target is None or tip is None:
            classification, reasons = "protected", ["target or branch identity is unavailable"]
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
        ref = branch_ref if isinstance(branch_ref, str) else None
        archive_verified = bool(ref and tip and archive_by_ref.get((ref, tip)))
        archive_argv = (["git", "bundle", "create", "<dir>/" + (branch or "worktree").replace("/", "_") +
                         ".bundle", ref] if classification == "unique" and ref else None)
        deletable = (classification == "integrated"
                     or classification == "unique" and archive_verified and not dirty and not stash_present)
        resources.append(_resource("worktree", path_text,
                                   {"path": path_text, "branch": branch_ref, "tip": tip,
                                    "terminals": terminals, "shared_with": shared}, classification, reasons,
                                   delete_argv=["orca", "worktree", "rm", "--worktree", "path:" + path_text,
                                                "--json"] if deletable else None,
                                   archive_argv=archive_argv, archive_verified=archive_verified))
        if branch_ref:
            covered_branches.add(branch_ref)
    local_branches = {ref for ref in covered_branches if ref.startswith("refs/heads/")}
    for unit in journal["units"].values():
        branch = unit.get("branch")
        if isinstance(branch, dict) and isinstance(branch.get("branch"), str):
            local_branches.add("refs/heads/" + branch["branch"])
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
        verified = bool(tip and archive_by_ref.get((ref, tip)))
        removed_with_worktree = bool(associated and any(row["delete_argv"] for row in associated))
        deletable = (classification == "integrated" or classification == "unique" and verified
                     and not associated and not stash_present)
        resources.append(_resource("local_branch", ref, {"ref": ref, "tip": tip, "checked_out": checked_out},
                                   classification,
                                   reasons + (["Orca also removes the merged local branch"]
                                              if removed_with_worktree else []),
                                   delete_argv=(["git", "update-ref", "-d", ref, tip]
                                                if deletable and not removed_with_worktree and tip else None),
                                   archive_argv=(["git", "bundle", "create", "<dir>/" +
                                                  branch.replace("/", "_") + ".bundle", ref]
                                                 if classification == "unique" else None),
                                   archive_verified=verified))
    remote_names = set()
    for unit in journal["units"].values():
        branch = unit.get("branch")
        if not isinstance(branch, dict):
            continue
        remote = branch.get("remote")
        if not isinstance(remote, str) or not _BRANCH.fullmatch(remote):
            continue
        # Only branches this objective's Governor published are its remote resources; a prepared branch
        # name alone proves nothing about who created the remote branch.
        for key in unit.get("published", {}):
            if isinstance(key, str) and key.startswith(remote + "/"):
                remote_names.add((remote, key[len(remote) + 1:]))
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
        verified = bool(tip and archive_by_ref.get((archive_ref, tip)))
        resources.append(_resource("remote_branch", remote + "/" + branch,
                                   {"remote": remote, "ref": ref, "tip": tip}, classification, reasons,
                                   delete_argv=(["git", "push", "--force-with-lease=" + ref + ":" + tip,
                                                remote, ":" + ref]
                                                if classification == "merged_remote_head"
                                                or classification == "unique" and verified else None),
                                   archive_argv=(["git", "bundle", "create", "<dir>/" +
                                                  remote + "_" + branch.replace("/", "_") + ".bundle",
                                                  archive_ref]
                                                 if classification == "unique" else None),
                                   archive_verified=verified))
    if len(resources) > MAX_RESOURCES:
        raise PodError("cleanup_unavailable", "Objective cleanup resource bound is exceeded")
    resources.sort(key=lambda row: row["id"])
    observed_target = {"ref": target_ref, "commit": target, "moved_after_delivery": moved}
    plan_hash, fingerprints = _expect_change(expect, resources, observed_target)
    for row in resources:
        row["fingerprint"] = fingerprints[row["id"]]
    return {"schema": "pod-cleanup-plan/v1", "objective": objective,
            "target": observed_target,
            "resources": resources, "digest": plan_hash,
            "expect": {"plan": plan_hash, "resources": fingerprints},
            "notes": ["Read-only plan; owner consent precedes commands.",
                      "Orca worktree removal also removes a merged local branch.",
                      "Verify each unique-work archive before removing its original ref."]}

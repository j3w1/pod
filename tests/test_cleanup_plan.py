"""Read-only cleanup decisions over disposable Git worktrees and a local remote."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.cleanup import plan
from pod import cleanup_archives
from pod.errors import PodError
from pod.github import repository_context
from pod.github import repository_reads
from pod.governor import _empty_journal, _record_path, _write_journal
from pod.ledger import objective_root
from pod.ledger import read
from pod.operations import guarded_start, OrcaPort
from tests.kernel_support import KernelCase, git
from tests.common import receipt


@contextmanager
def current_resources():
    """Inspect the actual current rows passed to the unchanged expect validator."""
    resources = []
    original = plan.__globals__["_expect_change"]
    def inspect(expect, rows, target):
        resources[:] = rows
        return original(expect, rows, target)
    with patch("pod.cleanup._expect_change", side_effect=inspect):
        yield resources


class CleanupPlanTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.child = self.root / "objective-worktree"
        git(self.project, "worktree", "add", "-qb", "objective", str(self.child))
        context = repository_context(self.child)
        self.intake(worktree={"repository": context["repository"], "repo_key": context["repo_key"],
                              "path": context["worktree"], "branch": context["branch"]})
        self.terminals = []
        self.orca_reads = []

    def orca_read(self, argv):
        self.orca_reads.append(list(argv))
        if argv[:2] == ["worktree", "show"]:
            path = argv[argv.index("--worktree") + 1].removeprefix("path:")
            return {"result": {"worktree": {"path": path, "isMainWorktree": False}}}
        if argv[:2] == ["terminal", "list"]:
            return {"result": {"terminals": list(self.terminals), "truncated": False}}
        raise AssertionError(argv)

    def observed(self, **kwargs):
        with repository_reads():
            return plan(self.project, "objective", orca_reader=self.orca_read, native_port=self.port, **kwargs)

    def resource(self, result, kind):
        return next(row for row in result["resources"] if row["kind"] == kind)

    def changed(self, expect, **kwargs):
        with current_resources() as resources, self.assertRaises(PodError) as error:
            self.observed(expect=expect, **kwargs)
        self.assertEqual(error.exception.code, "cleanup_changed")
        return {"resources": resources}

    def test_clean_integrated_worktree_and_branch_have_guarded_commands_without_writes(self):
        state_path = objective_root(self.project, "objective") / "context.json"
        before_state = state_path.read_bytes()
        before_refs = git(self.project, "show-ref")
        before_files = {path.name: path.read_bytes() for path in self.child.iterdir() if path.is_file()}
        result = self.observed()
        worktree = self.resource(result, "worktree")
        branch = self.resource(result, "local_branch")
        self.assertEqual(worktree["class"], "integrated")
        self.assertEqual(worktree["delete_argv"],
                         ["orca", "worktree", "rm", "--worktree", "path:" + str(self.child), "--json"])
        self.assertEqual(branch["class"], "integrated")
        self.assertIsNone(branch["delete_argv"])
        self.assertIn("Orca also removes", " ".join(branch["reasons"]))
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(git(self.project, "show-ref"), before_refs)
        self.assertEqual({path.name: path.read_bytes() for path in self.child.iterdir() if path.is_file()},
                         before_files)
        self.assertFalse((_record_path(self.project, "objective")).exists())
        self.assertEqual([argv[:2] for argv in self.orca_reads], [["worktree", "show"], ["terminal", "list"]])

    def test_untracked_data_and_unknown_terminal_are_never_silently_deleted(self):
        (self.child / "valuable.txt").write_text("private work\n")
        unique = self.resource(self.observed(), "worktree")
        self.assertEqual(unique["class"], "unique")
        self.assertIsNone(unique["delete_argv"])
        self.assertEqual(unique["archive_argv"][-1], "refs/heads/objective")
        self.terminals = [{"handle": "foreign", "worktreePath": str(self.child)}]
        protected = self.resource(self.observed(), "worktree")
        self.assertEqual(protected["class"], "protected")
        self.assertIsNone(protected["delete_argv"])

    def test_ignored_data_and_stash_are_unique_even_when_worktree_looks_clean(self):
        (self.project / ".git" / "info" / "exclude").write_text("*.scratch\n")
        (self.child / "notes.scratch").write_text("retained data\n")
        ignored = self.resource(self.observed(), "worktree")
        self.assertEqual(ignored["class"], "unique")
        self.assertIn("notes.scratch", " ".join(ignored["reasons"]))
        (self.child / "notes.scratch").unlink()
        (self.child / "README.md").write_text("uncommitted tracked data\n")
        git(self.child, "stash", "push", "-qm", "keep this work")
        stashed = self.resource(self.observed(), "worktree")
        self.assertEqual(stashed["class"], "unique")
        self.assertIn("stash", " ".join(stashed["reasons"]))
        self.assertIsNone(stashed["delete_argv"])

    def test_expect_reports_changed_resource_ids_without_saved_state(self):
        first = self.observed()
        self.assertEqual(self.observed(expect=first["expect"])["digest"], first["digest"])
        (self.child / "valuable.txt").write_text("new data\n")
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")
        self.assertIn("worktree:" + str(self.child), changed.exception.detail["changed"])

    def independent_root(self):
        return git(self.project, "commit-tree", self.base + "^{tree}", "-m", "independent retained history")

    def test_fetched_root_on_surviving_remote_ref_does_not_make_integrated_work_unique(self):
        oid = self.independent_root()
        git(self.child, "fetch", "-q", str(self.project), oid + ":refs/remotes/peer/other")
        result = self.observed()
        worktree = self.resource(result, "worktree")
        facts = worktree["identity"]["git"]
        self.assertEqual(worktree["class"], "integrated")
        self.assertIsNotNone(worktree["delete_argv"])
        self.assertEqual(facts["additional_roots"], [])
        self.assertIn(oid, facts["observed_roots"].values())
        self.assertEqual(facts["surviving_refs"]["refs/remotes/peer/other"], oid)
        self.assertNotIn(oid, facts["required_objects"])
        bundle = self.root / "branch.bundle"
        git(self.project, "bundle", "create", str(bundle), "refs/heads/objective")
        archived = self.resource(self.observed(archives=[{
            "path": str(bundle), "ref": "refs/heads/objective", "tip": self.base}]), "worktree")
        self.assertTrue(archived["archive_verified"], archived["archives"])

    def test_head_reflog_root_on_surviving_ref_needs_no_extra_preservation(self):
        oid = self.independent_root()
        git(self.child, "checkout", "-q", "--detach", oid)
        git(self.child, "checkout", "-q", "objective")
        git(self.project, "update-ref", "refs/remotes/peer/other", oid)
        result = self.observed()
        worktree = self.resource(result, "worktree")
        self.assertEqual(worktree["class"], "integrated")
        self.assertIsNotNone(worktree["delete_argv"])
        self.assertNotIn(oid, worktree["identity"]["git"]["required_objects"])
        git(self.project, "update-ref", "-d", "refs/remotes/peer/other")
        current = self.resource(self.changed(result["expect"]), "worktree")
        self.assertEqual(current["class"], "unique")
        self.assertIn(oid, current["identity"]["git"]["required_objects"])
        self.assertIsNone(current["delete_argv"])

    def test_removed_private_names_require_preservation_even_if_the_bytes_survive(self):
        oid = self.independent_root()
        git(self.child, "update-ref", "refs/worktree/private", oid)
        git(self.project, "update-ref", "refs/remotes/peer/other", oid)
        worktree = self.resource(self.observed(), "worktree")
        facts = worktree["identity"]["git"]
        self.assertEqual(worktree["class"], "unique")
        self.assertIsNone(worktree["delete_argv"])
        self.assertIn("refs/worktree/private", facts["additional_roots"])
        self.assertIn(oid, facts["required_objects"])
        self.assertNotIn("refs/worktree/private", facts["surviving_refs"])
        self.assertNotIn("refs/heads/objective", facts["surviving_refs"])

    def test_removed_private_refs_do_not_prove_survival_of_reflog_only_material(self):
        oid = self.independent_root()
        git(self.child, "checkout", "-q", "--detach", oid)
        git(self.child, "checkout", "-q", "objective")
        git(self.child, "update-ref", "refs/worktree/only-holder", oid)
        facts = self.resource(self.observed(), "worktree")["identity"]["git"]
        self.assertIn("reflog:" + oid, facts["additional_roots"])
        self.assertIn(oid, facts["required_objects"])

    def test_unavailable_surviving_reference_inventory_holds_the_resource(self):
        original = cleanup_archives._run
        def run(root, argv, **kwargs):
            if argv == ["for-each-ref", "--format=%(refname) %(objectname) %(symref)"]:
                raise cleanup_archives.Unavailable("Surviving reference observation is incomplete")
            return original(root, argv, **kwargs)
        with patch.object(cleanup_archives, "_run", side_effect=run):
            worktree = self.resource(self.observed(), "worktree")
        self.assertEqual(worktree["class"], "protected")
        self.assertIsNone(worktree["delete_argv"])
        self.assertIn("Surviving reference", " ".join(worktree["reasons"]))

    def test_verified_archive_allows_a_guarded_unique_worktree_command(self):
        (self.child / "README.md").write_text("unique commit\n")
        git(self.child, "add", "README.md")
        git(self.child, "commit", "-qm", "unique")
        tip = git(self.child, "rev-parse", "HEAD")
        ref = "refs/heads/objective"
        bundle = self.root / "objective.bundle"
        git(self.project, "bundle", "create", str(bundle), ref)
        verified = self.observed(archives=[{"path": str(bundle), "ref": ref, "tip": tip}])
        worktree = self.resource(verified, "worktree")
        self.assertEqual(worktree["class"], "unique")
        self.assertTrue(worktree["archive_verified"])
        self.assertIsNotNone(worktree["delete_argv"])
        bundle.write_text("corrupt\n")
        rejected = self.resource(self.observed(archives=[{"path": str(bundle), "ref": ref, "tip": tip}]),
                                 "worktree")
        self.assertFalse(rejected["archive_verified"])
        self.assertIsNone(rejected["delete_argv"])

    def test_merged_remote_tip_has_an_exact_lease(self):
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "refs/heads/objective:refs/heads/objective")
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": [],
                                         "preflight": {}, "published": {"origin/objective": self.base},
                                         "tasks": [], "candidate": None,
                                         "branch": {"remote": "origin", "base": "target",
                                                    "branch": "objective"}}
        _write_journal(_record_path(self.project, "objective"), journal)
        remote = self.resource(self.observed(), "remote_branch")
        self.assertEqual(remote["class"], "merged_remote_head")
        self.assertEqual(remote["delete_argv"],
                         ["git", "push", "--force-with-lease=refs/heads/objective:" + self.base,
                          "origin", ":refs/heads/objective"])

    def test_remote_tip_change_invalidates_expect_and_main_checkout_is_protected(self):
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "refs/heads/objective:refs/heads/objective")
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": [],
                                         "preflight": {}, "published": {"origin/objective": self.base},
                                         "tasks": [], "candidate": None,
                                         "branch": {"remote": "origin", "base": "target", "branch": "objective"}}
        _write_journal(_record_path(self.project, "objective"), journal)
        first = self.observed()
        (self.child / "README.md").write_text("new remote tip\n")
        git(self.child, "add", "README.md")
        git(self.child, "commit", "-qm", "new remote tip")
        git(self.child, "push", "-q", "origin", "refs/heads/objective:refs/heads/objective")
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertIn("remote_branch:origin/objective", changed.exception.detail["changed"])

    def test_main_checkout_is_protected(self):
        main_context = repository_context(self.project)
        self.write(self.stored(), worktree={"repository": main_context["repository"],
                                            "repo_key": main_context["repo_key"],
                                            "path": main_context["worktree"],
                                            "branch": main_context["branch"]})
        main = next(row for row in self.observed()["resources"]
                    if row["id"] == "worktree:" + str(self.project))
        self.assertEqual(main["class"], "protected")
        self.assertIsNone(main["delete_argv"])

    def test_a_worktree_on_the_bound_target_branch_is_protected(self):
        git(self.project, "branch", "maintained")
        maintained = self.root / "maintained-worktree"
        git(self.project, "worktree", "add", "-q", str(maintained), "maintained")
        context = repository_context(maintained)
        git(self.project, "remote", "add", "team/upstream", str(self.project))
        git(self.project, "update-ref", "refs/remotes/team/upstream/maintained", self.base)
        for objective, base_ref in (("local-target", "refs/heads/maintained"),
                                    ("slash-remote-target", "refs/remotes/team/upstream/maintained")):
            self.write([self.criterion()], objective=objective, governance={"base_ref": base_ref},
                       revision_authority={"provenance": "user_direct", "instruction": "Deliver onto maintained"},
                       worktree={"repository": context["repository"], "repo_key": context["repo_key"],
                                 "path": context["worktree"], "branch": context["branch"]})
            result = plan(self.project, objective, orca_reader=self.orca_read, native_port=self.port)
            for kind in ("worktree", "local_branch"):
                with self.subTest(target=base_ref, kind=kind):
                    row = self.resource(result, kind)
                    self.assertEqual(row["class"], "protected")
                    self.assertIsNone(row["delete_argv"])

    def test_a_branch_shared_with_another_worktree_is_protected_and_changes_expect(self):
        first = self.observed()
        git(self.project, "worktree", "add", "-q", "--force", str(self.root / "foreign-worktree"), "objective")
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")
        again = self.observed()
        worktree, branch = self.resource(again, "worktree"), self.resource(again, "local_branch")
        self.assertEqual((worktree["class"], worktree["delete_argv"]), ("protected", None))
        self.assertEqual(worktree["identity"]["shared_with"], [str(self.root / "foreign-worktree")])
        self.assertEqual((branch["class"], branch["delete_argv"]), ("protected", None))

    def test_expect_refuses_when_the_target_moved(self):
        first = self.observed()
        moved = git(self.project, "commit-tree", self.base + "^{tree}", "-p", self.base, "-m", "target moved")
        git(self.project, "update-ref", "refs/remotes/origin/target", moved)
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_a_retargeted_publication_never_claims_a_foreign_remote_branch(self):
        from tests.kernel_support import GovernorFakePort, action, authorization
        from pod.governor import execute, observe_candidate, prepare_candidate, reconcile, _read_journal
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "refs/heads/objective:refs/heads/foreign")
        observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "objective"})
        port = GovernorFakePort(lost=("push",))
        admitted = execute(self.project, "objective", owner="owner", port=port,
                           native_projection={"outstanding": []},
                           action=action(unit="delivery", candidate=observed["commit"], target="origin/objective",
                                         effects=[], authorization=authorization(
                                             candidate=observed["commit"], tree=observed["tree"],
                                             scope=("publish",))))
        self.assertEqual(admitted["outcome"], "UNKNOWN")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "foreign"})
        port.heads["origin/foreign"] = observed["commit"]
        reconcile(self.project, "objective", owner="owner", record_id=admitted["record_id"], port=port)
        unit = _read_journal(_record_path(self.project, "objective"))["units"]["delivery"]
        self.assertNotIn("origin/foreign", unit["published"])
        self.assertNotIn("remote_branch:origin/foreign", [row["id"] for row in self.observed()["resources"]])

    def test_an_older_publication_row_never_settles_on_a_retargeted_branch(self):
        from tests.kernel_support import GovernorFakePort, action, authorization
        from pod.governor import (execute, observe_candidate, prepare_candidate, reconcile, _read_journal,
                                  _write_journal)
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "refs/heads/objective:refs/heads/foreign")
        observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "objective"})
        port = GovernorFakePort(lost=("push",))
        admitted = execute(self.project, "objective", owner="owner", port=port,
                           native_projection={"outstanding": []},
                           action=action(unit="delivery", candidate=observed["commit"], target="origin/objective",
                                         effects=[], authorization=authorization(
                                             candidate=observed["commit"], tree=observed["tree"],
                                             scope=("publish",))))
        # A row admitted before rows recorded their pushed branch.
        journal_path = _record_path(self.project, "objective")
        journal = _read_journal(journal_path)
        next(row for row in journal["actions"] if row["record_id"] == admitted["record_id"]).pop("branch")
        _write_journal(journal_path, journal)
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "foreign"})
        port.heads["origin/foreign"] = observed["commit"]
        found = reconcile(self.project, "objective", owner="owner", record_id=admitted["record_id"], port=port)
        self.assertEqual(found["status"], "UNKNOWN")
        self.assertNotIn("origin/foreign", _read_journal(journal_path)["units"]["delivery"]["published"])
        self.assertNotIn("remote_branch:origin/foreign", [row["id"] for row in self.observed()["resources"]])

    def test_a_prepared_but_unpublished_remote_branch_is_not_offered(self):
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "refs/heads/objective:refs/heads/objective")
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": [],
                                         "preflight": {}, "published": {}, "tasks": [], "candidate": None,
                                         "branch": {"remote": "origin", "base": "target", "branch": "objective"}}
        _write_journal(_record_path(self.project, "objective"), journal)
        self.assertEqual([row for row in self.observed()["resources"] if row["kind"] == "remote_branch"], [])

    def test_terminal_ownership_not_old_settlement_decides_reclaimability(self):
        from pod.cleanup import _terminal_facts
        binding = {"runId": "run", "taskId": "task", "dispatchId": "dispatch-1", "workerId": "worker-1",
                   "worktreeId": "wt", "terminalHandle": "term-1"}
        admission = {"admission_id": "adm-1", "runtime": "runtime", "native_binding": binding}
        self.terminals = [{"handle": "term-1", "worktreePath": str(self.child)}]

        class Shown:
            resource = None

            def show_worker(self, dispatch):
                return {"runtime": "runtime", "result": {
                    "dispatch": {"id": dispatch, "runId": "run", "taskId": "task", "status": "completed"},
                    "projection": {"id": "worker-1", "dispatchId": dispatch, "runId": "run", "taskId": "task",
                                   "outcome": "succeeded", "stage": {"dispatch": "completed"}},
                    "worker": {"dispatchId": dispatch, "worktreeId": "wt", "agentTerminalHandle": "term-1"},
                    "terminalResource": self.resource}}

        port = Shown()
        owned = {"terminalHandle": "term-1", "ownerDispatchId": "dispatch-1", "ownershipState": "owned",
                 "releaseState": "not_requested"}
        cases = ((owned, "retained_reclaimable", False),
                 ({**owned, "ownerDispatchId": "foreign-dispatch"}, "not_owned", True),
                 ({**owned, "ownershipState": "user_owned", "releaseState": "retained"}, "not_owned", True),
                 ({**owned, "ownershipState": "external"}, "not_owned", True),
                 ({**owned, "releaseState": "released"}, "not_owned", True),
                 (None, "not_owned", True))
        for resource, state, protected in cases:
            port.resource = resource
            with self.subTest(resource=resource):
                facts, unknown = _terminal_facts(self.child, {"adm-1": admission}, orca_reader=self.orca_read,
                                                 native_port=port)
                self.assertEqual((facts[0]["state"], unknown), (state, protected))


class FixtureCleanupPort:
    """Execute the listed commands only in an allowlisted disposable Git fixture.

    The Orca port models worktree rm's local effects with real non-force Git commands;
    no command reaches the native Orca host or a network remote.
    """
    def __init__(self, project, paths):
        self.project, self.paths, self.commands = project, set(map(str, paths)), []

    def apply(self, result):
        order = {"worktree": 0, "local_branch": 1, "remote_branch": 2}
        for row in sorted(result["resources"], key=lambda row: order[row["kind"]]):
            argv = row["delete_argv"]
            if argv is None:
                continue
            if argv[:3] == ["orca", "worktree", "rm"]:
                path = argv[argv.index("--worktree") + 1].removeprefix("path:")
                if path not in self.paths:
                    raise AssertionError("fixture port rejected foreign path")
                commands = [["git", "worktree", "remove", path],
                            ["git", "branch", "-d", row["identity"]["branch"].removeprefix("refs/heads/")]]
            elif argv[0] == "git":
                commands = [argv]
            else:
                raise AssertionError(argv)
            for command in commands:
                self.commands.append(command)
                subprocess.run(command, cwd=self.project, capture_output=True, text=True, check=True)


class CleanupScaleTests(unittest.TestCase):
    """One expensive repository fixture, shared by these short, independently restored tests."""
    @classmethod
    def setUpClass(cls):
        cls.case = CleanupPlanTests()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        cls.unrelated = []
        for index in range(150):
            path = cls.case.root / f"unrelated-{index:03}"
            git(cls.case.project, "worktree", "add", "-qb", f"unrelated-{index:03}", str(path))
            # Real porcelain over the old byte bound, without enormous paths or another host worktree.
            git(cls.case.project, "worktree", "lock", "--reason", "x" * 14000, str(path))
            cls.unrelated.append(path)
        bare = cls.case.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(cls.case.project, "remote", "add", "origin", str(bare))
        git(cls.case.project, "push", "-q", "origin", "objective")
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": [],
                                         "preflight": {}, "published": {"origin/objective": cls.case.base},
                                         "tasks": [], "candidate": None,
                                         "branch": {"remote": "origin", "base": "target", "branch": "objective"}}
        _write_journal(_record_path(cls.case.project, "objective"), journal)

    def test_large_inventory_plan_expect_apply_only_objective_resources(self):
        case = self.case
        before = git(case.project, "worktree", "list", "--porcelain", "-z")
        self.assertEqual(len(before.split("worktree ")) - 1, 152)
        self.assertGreater(len(before), 2_000_000)
        unrelated_before = [row for row in before.split("\0\0") if str(case.child) not in row]
        refs_before = git(case.project, "for-each-ref", "--format=%(refname) %(objectname)")
        context = (objective_root(case.project, "objective") / "context.json").read_bytes()
        first = case.observed()
        self.assertEqual({row["id"] for row in first["resources"]},
                         {"worktree:" + str(case.child), "local_branch:refs/heads/objective",
                          "remote_branch:origin/objective"})
        again = case.observed(expect=first["expect"])
        port = FixtureCleanupPort(case.project, [case.child])
        self.addCleanup(git, case.project, "push", "-q", "origin", "objective")
        self.addCleanup(git, case.project, "worktree", "add", "-qb", "objective", str(case.child), case.base)
        port.apply(again)
        self.assertFalse(case.child.exists())
        self.assertEqual(git(case.project, "ls-remote", "origin", "refs/heads/objective"), "")
        self.assertNotIn("refs/heads/objective", git(case.project, "show-ref"))
        after = git(case.project, "worktree", "list", "--porcelain", "-z")
        self.assertEqual(after.split("\0\0"), unrelated_before)
        own_refs = ("refs/heads/objective ", "refs/remotes/origin/objective ")
        self.assertEqual([line for line in refs_before.splitlines() if not line.startswith(own_refs)],
                         git(case.project, "for-each-ref", "--format=%(refname) %(objectname)").splitlines())
        self.assertEqual((objective_root(case.project, "objective") / "context.json").read_bytes(), context)
        self.assertEqual(port.commands[0], ["git", "worktree", "remove", str(case.child)])
        self.assertEqual(port.commands[1], ["git", "branch", "-d", "objective"])
        self.assertIn("--force-with-lease=refs/heads/objective:" + case.base, port.commands[2])

    def test_shared_branch_beyond_the_old_inventory_bound_remains_protected(self):
        case, foreign = self.case, self.unrelated[-1]
        first = case.observed()
        self.addCleanup(git, foreign, "checkout", "-q", "unrelated-149")
        git(foreign, "checkout", "-q", "--ignore-other-worktrees", "objective")
        result = case.changed(first["expect"])
        worktree = case.resource(result, "worktree")
        self.assertEqual(worktree["identity"]["shared_with"], [str(foreign)])
        self.assertEqual((worktree["class"], worktree["delete_argv"]), ("protected", None))
        branch = case.resource(result, "local_branch")
        self.assertEqual(branch["identity"]["checked_out"], [str(case.child), str(foreign)])
        self.assertIsNone(branch["delete_argv"])

    def test_one_plan_still_has_a_resource_bound(self):
        # Integration plus 47 placements: 48 worktrees, 48 local branches and one published remote.
        # These are actual fixture worktrees, without altering the production bound of 96.
        state = read(self.case.project, "objective")
        for index, path in enumerate(self.unrelated[:47]):
            state["admissions"][f"scope-{index}"] = {"recovery": {"placement_binding": {
                "path": str(path), "branch": f"unrelated-{index:03}"}}}
        with patch("pod.cleanup.read", return_value=state), \
                patch("pod.cleanup._terminal_facts", side_effect=AssertionError("over-bound native read")), \
                patch("pod.cleanup._dirt", side_effect=AssertionError("over-bound status read")), \
                self.assertRaises(PodError) as refused:
            self.case.observed()
        self.assertEqual(refused.exception.code, "cleanup_unavailable")
        self.assertEqual(str(refused.exception), "Objective cleanup resource bound is exceeded")

    def test_full_registry_consumer_bound_does_not_bound_objective_plan(self):
        from pod.cleanup import _worktrees
        with self.assertRaises(PodError) as refused:
            _worktrees(self.case.project)
        self.assertEqual(refused.exception.code, "cleanup_unavailable")
        result = self.case.observed()
        self.assertEqual({row["id"] for row in result["resources"]},
                         {"worktree:" + str(self.case.child), "local_branch:refs/heads/objective",
                          "remote_branch:origin/objective"})


class CleanupWorkerTests(KernelCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_case = cls()
        cls.addClassCleanup(cls.fixture_case.doCleanups)
        cls.fixture_case._build_fixture()
        cls.context_path = objective_root(cls.fixture_case.project, "objective") / "context.json"
        cls.context_bytes = cls.context_path.read_bytes()
        cls.journal_path = _record_path(cls.fixture_case.project, "objective")
        cls.journal_bytes = cls.journal_path.read_bytes()
        cls.native_workers = deepcopy(cls.fixture_case.port.workers)
        admin = Path(git(cls.fixture_case.child, "rev-parse", "--absolute-git-dir"))
        cls.head_reflog = (admin / "logs" / "HEAD").read_bytes()

    def setUp(self):
        fixture = self.fixture_case
        for key in ("root", "project", "child", "base", "candidate", "main_binding", "frozen",
                    "admission", "dispatch", "port", "native_show"):
            setattr(self, key, getattr(fixture, key))
        self.port.workers = deepcopy(self.native_workers)
        self.port.show_worker = self.shown
        self.resource_case, self.retained_reason = "released", None
        self.resource_owners = {}
        self.terminals, self.orca_reads = [], []
        self.context_path.write_bytes(self.context_bytes)
        self.journal_path.write_bytes(self.journal_bytes)
        git(self.project, "update-ref", "refs/heads/worker", self.base)
        if self.child.is_dir():
            git(self.child, "reset", "--hard", self.base)
            git(self.child, "checkout", "-q", "worker")
            for name in ("valuable.txt", "notes.scratch"):
                (self.child / name).unlink(missing_ok=True)
        else:
            git(self.project, "worktree", "add", "-q", str(self.child), "worker")
        # A reset does not remove independent older roots from a previous method's private reflog.
        admin = Path(git(self.child, "rev-parse", "--absolute-git-dir"))
        (admin / "logs" / "HEAD").write_bytes(self.head_reflog)
        (admin / "ORIG_HEAD").unlink(missing_ok=True)
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        git(self.root / "origin.git", "update-ref", "refs/heads/worker", self.base)
        git(self.project, "update-ref", "refs/remotes/origin/worker", self.base)
        git(self.project, "stash", "clear")
        (self.project / ".git" / "info" / "exclude").write_text("")

    def _build_fixture(self):
        super().setUp()
        self.child = self.root / "worker-worktree"
        git(self.project, "worktree", "add", "-qb", "worker", str(self.child))
        main = repository_context(self.project)
        self.main_binding = {"repository": main["repository"], "repo_key": main["repo_key"],
                             "path": main["worktree"], "branch": main["branch"]}
        self.intake(self.sub("S", boundary={"paths": ["src"]}), worktree=self.main_binding)
        context = repository_context(self.child)
        placement = {"repository": context["repository"], "repo_key": context["repo_key"],
                     "path": context["worktree"], "branch": context["branch"]}
        self.port.placement = {**placement, "runtime": self.port.runtime}
        self.frozen = self.packet(["S"], worktree=self.main_binding, placement=placement,
                                  boundary={"paths": ["src"]})
        self.admission = guarded_start(self.project, "objective", owner="owner", run="run", task="task",
                                       plan_revision="plan", frozen_packet=self.frozen,
                                       worktree="path:" + str(self.child), port=self.port)["admission"]
        self.settle(self.admission)
        rows = self.stored()
        child = next(row for row in rows if row["id"] == "S")
        child.pop("executor")
        child.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(self.admission, self.frozen, result_commit=self.base, map={"obligations": rows})
        self.dispatch = self.admission["native_binding"]["dispatchId"]
        self.native_show = self.port.show_worker
        self.resource_case = "released"
        self.retained_reason = None
        self.port.show_worker = self.shown
        self.terminals = []
        self.orca_reads = []
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", str(bare))
        git(self.project, "push", "-q", "origin", "worker")
        journal = _empty_journal()
        for branch in ("worker", "unrelated"):
            journal["units"][branch] = {"name": branch, "generation": 1, "history": [],
                                        "preflight": {}, "published": {"origin/" + branch: self.base},
                                        "tasks": [], "candidate": None,
                                        "branch": {"remote": "origin", "base": "target", "branch": branch}}
        _write_journal(_record_path(self.project, "objective"), journal)

    def shown(self, dispatch):
        shown = self.native_show(dispatch)
        result = shown["result"]
        binding = self.admission["native_binding"]
        case = self.resource_case
        owner = getattr(self, "resource_owners", {}).get(dispatch, dispatch)
        result["projection"].update(resource={"state": "released", "releaseState": "released",
                                              "ownerDispatchId": owner},
                                    liveness={"verdict": "exited"})
        if case != "projection_only":
            result["terminalResource"] = {
                "worktreeId": binding["worktreeId"], "terminalHandle": binding["terminalHandle"],
                "ownerDispatchId": dispatch, "ownershipState": "released", "releaseState": "released"}
            result["terminalResource"]["ownerDispatchId"] = owner
        if case in ("unreleased", "retained", "reused", "foreign", "user_owned", "live"):
            result["terminalResource"].update(ownershipState="owned", releaseState="not_requested")
            result["projection"]["resource"] = {"state": "owned", "releaseState": "not_requested"}
        if case == "retained":
            result["terminalResource"].update(releaseState="retained", retainedReason=self.retained_reason)
        if case == "reused":
            result["terminalResource"]["ownerDispatchId"] = "dispatch-next-round"
            result["projection"]["resource"]["ownerDispatchId"] = "dispatch-next-round"
        if case == "foreign":
            result["terminalResource"]["worktreeId"] = "foreign-worktree"
        if case == "user_owned":
            result["terminalResource"]["ownershipState"] = "user_owned"
        if case == "live":
            result["projection"]["liveness"]["verdict"] = "live"
        if case == "unknown_release":
            result.pop("terminalResource")
            result["projection"].pop("resource")
        if case == "wrong_runtime":
            shown["runtime"] = "another-runtime"
        return shown

    orca_read = CleanupPlanTests.orca_read
    resource = CleanupPlanTests.resource
    changed = CleanupPlanTests.changed

    def observed(self, worker=None, **kwargs):
        with repository_reads():
            return plan(self.project, "objective", orca_reader=self.orca_read, native_port=self.port,
                        worker=worker or {"admission": self.admission["admission_id"]}, **kwargs)

    def assert_held(self, reason, result=None):
        result = self.observed() if result is None else result
        self.assertEqual(len(result["resources"]), 3)
        for row in result["resources"]:
            self.assertIsNone(row["delete_argv"], row)
            self.assertIn(reason, " ".join(row["reasons"]), row)

    def test_exact_selector_removes_released_worker_and_its_local_and_remote_branches_while_open(self):
        context = (objective_root(self.project, "objective") / "context.json").read_bytes()
        first = self.observed()
        self.assertIsNone(first["closure_basis"])
        self.assertEqual({row["id"] for row in first["resources"]},
                         {"worktree:" + str(self.child), "local_branch:refs/heads/worker",
                          "remote_branch:origin/worker"})
        again = self.observed(worker={"dispatch": self.dispatch}, expect=first["expect"])
        self.assertEqual(first["digest"], again["digest"])
        port = FixtureCleanupPort(self.project, [self.child])
        port.apply(again)
        self.assertFalse(self.child.exists())
        self.assertEqual(git(self.project, "ls-remote", "origin", "refs/heads/worker"), "")
        self.assertNotIn("refs/heads/worker", git(self.project, "show-ref"))
        self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), context)
        self.assertTrue(self.project.is_dir())

    def test_internal_entry_point_passes_the_exact_dispatch_selector(self):
        from pod.internal import run
        native_run = subprocess.run
        def execute(argv, **kwargs):
            if argv[0] != "/fixture/orca":
                return native_run(argv, **kwargs)
            result = self.orca_read(argv[1:])
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"ok": True, "result": result["result"], "_meta": {"runtimeId": self.port.runtime}}), "")
        with patch.object(OrcaPort, "show_worker", side_effect=self.shown), \
                patch("pod.orca.executable", return_value="/fixture/orca"), \
                patch("pod.orca.subprocess.run", side_effect=execute):
            result = run("cleanup-plan", {"project": str(self.project), "objective": "objective",
                                           "worker": {"dispatch": self.dispatch}})
        self.assertEqual(result["worker"]["dispatch"], self.dispatch)

    def test_recorded_reuse_reason_is_exact_and_changes_expect(self):
        first = self.observed()
        reason = "reused next round after the exact candidate review"
        worker = {"admission": self.admission["admission_id"], "reuse_reason": reason}
        retained = self.observed(worker=worker)
        self.assertEqual(retained["worker"]["reuse_reason"], reason)
        self.assert_held("reused next round: " + reason, retained)
        self.changed(first["expect"], worker=worker)

    def test_different_recorded_reuse_reason_changes_expect(self):
        worker = {"admission": self.admission["admission_id"], "reuse_reason": "initial next round"}
        retained = self.observed(worker=worker)
        self.changed(retained["expect"], worker={**worker, "reuse_reason": "a different next round"})

    def test_native_retention_and_reuse_keep_the_exact_reason(self):
        self.resource_case = "retained"
        self.retained_reason = "reused next round for delta review of the correction"
        result = self.observed()
        self.assertEqual(result["worker"]["retained_reason"], self.retained_reason)
        self.assert_held("native retention: " + self.retained_reason, result)
        self.resource_case = "reused"
        self.assert_held("reused or belongs to another native binding")

    def test_active_unreleased_live_or_unknown_worker_resources_stay_held(self):
        self.port.workers[self.dispatch]["outcome"] = "in_progress"
        self.assert_held("worker Dispatch is active or unsettled")
        self.port.workers[self.dispatch]["outcome"] = "succeeded"
        for case, reason in (("unreleased", "not natively released"), ("live", "exit is unverified")):
            with self.subTest(case=case):
                self.resource_case = case
                self.assert_held(reason)

    def test_unknown_release_and_changed_runtime_keep_worker_resources_held(self):
        for case, reason in (("unknown_release", "not natively released"),
                             ("wrong_runtime", "native settlement or release is unavailable")):
            with self.subTest(case=case):
                self.resource_case = case
                self.assert_held(reason)

    def test_foreign_user_owned_and_current_integration_resources_stay_held(self):
        for case, reason in (("foreign", "another native binding"), ("user_owned", "not natively released")):
            with self.subTest(case=case):
                self.resource_case = case
                self.assert_held(reason)
        state = read(self.project, "objective")
        state["checkpoint"]["worktree"]["path"] = str(self.child)
        with patch("pod.cleanup.read", return_value=state):
            self.assert_held("objective integration worktree")

    def test_unreported_and_other_assignment_placements_are_held(self):
        state = read(self.project, "objective")
        state["admissions"][self.admission["admission_id"]]["report"] = None
        with patch("pod.cleanup.read", return_value=state):
            self.assert_held("worker report has not been recorded")
        state = read(self.project, "objective")
        other = deepcopy(state["admissions"][self.admission["admission_id"]])
        other.update(admission_id="another-admission", reuse_of=self.admission["admission_id"])
        other["native_binding"]["dispatchId"] = "dispatch-next-round"
        state["admissions"][other["admission_id"]] = other
        with patch("pod.cleanup.read", return_value=state):
            result = self.observed()
        self.assert_held("worktree is also bound to admission another-admission", result)
        self.assert_held("reused next round by admission another-admission", result)

    def test_dirty_ignored_stash_and_foreign_terminal_keep_all_worker_resources(self):
        for filename in ("valuable.txt", "notes.scratch"):
            with self.subTest(path=filename):
                (self.project / ".git" / "info" / "exclude").write_text("*.scratch\n")
                (self.child / filename).write_text("keep this data\n")
                self.assert_held(filename)
                (self.child / filename).unlink()

    def test_stash_and_foreign_terminal_keep_all_worker_resources(self):
        (self.child / "README.md").write_text("stashed data\n")
        git(self.child, "stash", "push", "-qm", "retain")
        self.assert_held("stash")
        git(self.project, "stash", "clear")
        self.terminals = [{"handle": "foreign", "worktreePath": str(self.child)}]
        self.assert_held("terminal here is active")

    def test_unmerged_and_corrupt_archive_hold_worker_refs(self):
        (self.child / "README.md").write_text("unique work\n")
        git(self.child, "add", "README.md")
        git(self.child, "commit", "-qm", "unique work")
        self.assert_held("outside the observed target")
        tip = git(self.child, "rev-parse", "HEAD")
        archive = self.root / "worker.bundle"
        archive.write_text("corrupt archive\n")
        self.assert_held("outside the observed target", self.observed(
            archives=[{"path": str(archive), "ref": "refs/heads/worker", "tip": tip}]))

    def test_expanded_archive_does_not_clear_worker_release_or_report_holds(self):
        ref = "refs/heads/worker"
        archive = self.root / "worker-expanded.bundle"
        git(self.project, "bundle", "create", str(archive), ref)
        rows = [{"path": str(archive), "ref": ref, "tip": self.base,
                 "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                 "recovery_refs": {ref: self.base}}]
        self.resource_case = "unreleased"
        result = self.observed(archives=rows)
        self.assertTrue(self.resource(result, "worktree")["archive_verified"])
        self.assert_held("not natively released", result)
        self.resource_case = "released"
        state = read(self.project, "objective")
        state["admissions"][self.admission["admission_id"]]["report"] = None
        with patch("pod.cleanup.read", return_value=state):
            result = self.observed(archives=rows)
        self.assertTrue(self.resource(result, "worktree")["archive_verified"])
        self.assert_held("worker report has not been recorded", result)

    def test_changed_release_report_or_remote_tip_invalidates_expect(self):
        first = self.observed()
        self.resource_case = "unreleased"
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")
        self.resource_case = "released"
        moved = git(self.project, "commit-tree", self.base + "^{tree}", "-p", self.base, "-m", "remote moved")
        git(self.project, "push", "-q", "origin", moved + ":refs/heads/worker")
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertIn("remote_branch:origin/worker", changed.exception.detail["changed"])
        argv = self.resource(first, "remote_branch")["delete_argv"]
        result = subprocess.run(argv, cwd=self.project, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.project, "ls-remote", "origin", "refs/heads/worker").split()[0], moved)

    def test_default_target_and_changed_branch_bindings_remain_protected(self):
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/worker")
        self.assert_held("default")
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        git(self.child, "checkout", "-qb", "changed-worker")
        result = self.observed()
        self.assertTrue(all(row["delete_argv"] is None for row in result["resources"]))
        self.assertIn("branch changed", " ".join(self.resource(result, "worktree")["reasons"]))

    def test_moved_after_delivery_hold_is_unchanged(self):
        state = read(self.project, "objective")
        state["checkpoint"]["delivery"] = {"candidate": self.base, "result": "f" * 40}
        with patch("pod.cleanup.read", return_value=state):
            result = self.observed()
        self.assertTrue(result["target"]["moved_after_delivery"])
        self.assertTrue(all(row["delete_argv"] is None for row in result["resources"]))

    def test_projection_release_without_a_terminal_resource_is_supported(self):
        # Release/liveness fields from the sanitized native capture; identity stays fixture-owned.
        self.resource_case = "projection_only"
        shown = self.shown(self.dispatch)
        captured = receipt("stopped-worker-show")["result"]["projection"]
        shown["result"]["projection"].update(resource=captured["resource"], liveness=captured["liveness"])
        with patch.object(self.port, "show_worker", return_value=shown):
            self.assertIsNotNone(self.resource(self.observed(), "worktree")["delete_argv"])

    def test_incomplete_resource_and_wrong_worker_terminal_never_prove_release(self):
        shown = self.shown(self.dispatch)
        for key in ("ownerDispatchId", "worktreeId", "terminalHandle"):
            incomplete = deepcopy(shown)
            incomplete["result"]["terminalResource"].pop(key)
            with self.subTest(missing=key), patch.object(self.port, "show_worker", return_value=incomplete):
                self.assertTrue(all(row["delete_argv"] is None for row in self.observed()["resources"]))

    def test_incomplete_release_fields_and_wrong_worker_terminal_never_prove_release(self):
        shown = self.shown(self.dispatch)
        for key in ("ownershipState", "releaseState"):
            incomplete = deepcopy(shown)
            incomplete["result"]["terminalResource"].pop(key)
            with self.subTest(missing=key), patch.object(self.port, "show_worker", return_value=incomplete):
                self.assertTrue(all(row["delete_argv"] is None for row in self.observed()["resources"]))
        shown["result"]["worker"]["agentTerminalHandle"] = "wrong-terminal"
        with patch.object(self.port, "show_worker", return_value=shown):
            self.assert_held("worker terminal identity is unavailable or differs")

    def test_incomplete_connected_or_foreign_terminal_readbacks_hold_release(self):
        shown = self.shown(self.dispatch)
        binding = self.admission["native_binding"]
        terminal = {"handle": binding["terminalHandle"], "worktreeId": binding["worktreeId"], "connected": False}
        for fields in ({"connected": True}, {"connected": None}):
            shown["result"]["terminal"] = {**terminal, **fields}
            with self.subTest(fields=fields), patch.object(self.port, "show_worker", return_value=shown):
                self.assert_held("terminal readback is connected, incomplete or belongs to another binding")

    def test_foreign_terminal_handle_or_worktree_holds_release(self):
        shown = self.shown(self.dispatch)
        binding = self.admission["native_binding"]
        terminal = {"handle": binding["terminalHandle"], "worktreeId": binding["worktreeId"], "connected": False}
        for fields in ({"handle": "other-terminal"}, {"worktreeId": "other-worktree"}):
            shown["result"]["terminal"] = {**terminal, **fields}
            with self.subTest(fields=fields), patch.object(self.port, "show_worker", return_value=shown):
                self.assert_held("terminal readback is connected, incomplete or belongs to another binding")

    def test_contradictory_projection_ownership_and_unknown_integration_stay_held(self):
        shown = self.shown(self.dispatch)
        shown["result"]["projection"]["resource"]["ownerDispatchId"] = "foreign-dispatch"
        with patch.object(self.port, "show_worker", return_value=shown):
            self.assert_held("reused or belongs to another native binding")
        state = read(self.project, "objective")
        state["checkpoint"]["worktree"] = None
        with patch("pod.cleanup.read", return_value=state):
            self.assert_held("integration worktree binding is unavailable")
        state = read(self.project, "objective")
        state["admissions"][self.admission["admission_id"]]["recovery"]["placement_binding"]["branch"] = None
        with patch("pod.cleanup.read", return_value=state):
            result = self.observed()
        self.assertEqual({row["kind"] for row in result["resources"]}, {"worktree", "local_branch"})
        for row in result["resources"]:
            self.assertIsNone(row["delete_argv"])
            self.assertIn("worker placement branch identity is unavailable", " ".join(row["reasons"]))

    def final_reused_round(self):
        """Record both rounds through admission/report entry points; native reuse keeps one resource."""
        with repository_reads():
            self.write(self.stored(), worktree=self.main_binding, dispositions=[{
                "admission": self.admission["admission_id"], "discarded": True,
                "reason": "the unchanged first-round result is set aside before the next frozen assignment"}])
            self.resource_case = "unreleased"
            placement = self.admission["recovery"]["placement_binding"]
            frozen = self.packet(["S"], worktree=self.main_binding, placement=placement,
                                  boundary={"paths": ["src"]})
            final = guarded_start(self.project, "objective", owner="owner", run="run", task="task-next",
                                  plan_revision="plan", frozen_packet=frozen, worktree="path:" + str(self.child),
                                  reuse_of=self.admission["admission_id"], port=self.port)["admission"]
            self.resource_owners[self.dispatch] = final["native_binding"]["dispatchId"]
            self.settle(final)
            rows = self.stored()
            child = next(row for row in rows if row["id"] == "S")
            child.pop("executor")
            child.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
            self.report(final, frozen, result_commit=self.base, map={"obligations": rows})
        return final

    def test_final_reused_round_can_remove_the_reported_released_placement(self):
        reason = "immediate next round on the correction's frozen candidate"
        final = self.final_reused_round()
        selector = {"admission": final["admission_id"]}
        self.resource_case = "released"
        first = self.observed(worker=selector)
        self.assertEqual(first["worker"]["placement_history"][0]["proof"], "transferred_to_selected")
        self.assert_held("reused next round: " + reason, self.observed(worker={
            "admission": self.admission["admission_id"], "reuse_reason": reason}))
        again = self.observed(worker=selector, expect=first["expect"])
        FixtureCleanupPort(self.project, [self.child]).apply(again)
        self.assertFalse(self.child.exists())
        self.assertEqual(git(self.project, "ls-remote", "origin", "refs/heads/worker"), "")
        self.assertIsNone(read(self.project, "objective")["checkpoint"].get("closure"))

    def test_reuse_reason_and_unreleased_final_round_stay_held(self):
        reason = "immediate next round on the correction's frozen candidate"
        self.assert_held("reused next round: " + reason, self.observed(worker={
            "admission": self.admission["admission_id"], "reuse_reason": reason}))
        final = self.final_reused_round()
        self.assert_held("worker resource is not natively released", self.observed(worker={"admission": final["admission_id"]}))

    def test_reuse_history_active_unknown_or_foreign_ownership_holds_and_changes_expect(self):
        final = self.final_reused_round()
        self.resource_case = "released"
        selector = {"dispatch": final["native_binding"]["dispatchId"]}
        first = self.observed(worker=selector)
        self.port.workers[self.dispatch]["outcome"] = "in_progress"
        self.assert_held("other admission", self.observed(worker=selector))
        with self.assertRaises(PodError) as changed:
            self.observed(worker=selector, expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_reuse_history_foreign_ownership_holds(self):
        final = self.final_reused_round()
        self.resource_case = "released"
        selector = {"dispatch": final["native_binding"]["dispatchId"]}
        self.port.workers[self.dispatch]["outcome"] = "succeeded"
        self.resource_owners[self.dispatch] = "foreign-dispatch"
        self.assert_held("other admission", self.observed(worker=selector))

    def test_reuse_requires_a_complete_exact_chain_or_positive_independent_historical_release(self):
        final = self.final_reused_round()
        self.resource_case = "released"
        selector = {"admission": final["admission_id"]}
        state = read(self.project, "objective")
        state["admissions"][final["admission_id"]]["reuse_of"] = None
        with patch("pod.cleanup.read", return_value=state):
            self.assert_held("other admission", self.observed(worker=selector))
        # Without chain proof, only the old Dispatch's own positive released identity can clear it.
        self.resource_owners[self.dispatch] = self.dispatch
        with patch("pod.cleanup.read", return_value=state):
            result = self.observed(worker=selector)
        self.assertEqual(result["worker"]["placement_history"][0]["proof"], "released")
        self.assertIsNotNone(self.resource(result, "worktree")["delete_argv"])

    def test_missing_earlier_native_resource_after_reuse_stays_held(self):
        final = self.final_reused_round()
        self.resource_case = "released"
        selector = {"admission": final["admission_id"]}
        first = self.observed(worker=selector)
        show = self.port.show_worker

        def unavailable(dispatch):
            shown = show(dispatch)
            if dispatch == self.dispatch:
                # Observed Orca response after reuse/release; identities remain fixture-owned.
                shown["result"]["projection"].update(
                    resource={"state": "absent", "reason": "not_materialized"},
                    liveness={"verdict": "unverifiable", "reason": "missing_status"})
                shown["result"].pop("terminalResource", None)
                shown["result"]["terminal"] = None
            return shown

        with patch.object(self.port, "show_worker", side_effect=unavailable):
            result = self.observed(worker=selector)
            self.assert_held("other admission", result)
            self.assert_held("earlier worker resource transfer has not been proved", result)
            with self.assertRaises(PodError) as changed:
                self.observed(worker=selector, expect=first["expect"])
            self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_selector_shape_and_unknown_identity_are_refused_without_writes(self):
        before = (objective_root(self.project, "objective") / "context.json").read_bytes()
        invalid = (({}, "invalid_cleanup_worker"),
                   ({"admission": "a", "dispatch": "b"}, "invalid_cleanup_worker"),
                   ({"dispatch": "unknown"}, "cleanup_worker_unavailable"),
                   ({"admission": self.admission["admission_id"], "reuse_reason": ""},
                    "invalid_cleanup_reuse_reason"),
                   ({"admission": self.admission["admission_id"], "reuse_reason": "x" * 513},
                    "invalid_cleanup_reuse_reason"))
        for worker, code in invalid:
            with self.subTest(worker=worker), self.assertRaises(PodError) as refused:
                plan(self.project, "objective", worker=worker, orca_reader=self.orca_read, native_port=self.port)
            self.assertEqual(refused.exception.code, code)
        self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

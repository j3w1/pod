"""Read-only cleanup decisions over disposable Git worktrees and a local remote."""

from __future__ import annotations

import subprocess

from pod.cleanup import plan
from pod.errors import PodError
from pod.github import repository_context
from pod.governor import _empty_journal, _record_path, _write_journal
from pod.ledger import objective_root
from tests.kernel_support import KernelCase, git


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
        return plan(self.project, "objective", orca_reader=self.orca_read, native_port=self.port, **kwargs)

    def resource(self, result, kind):
        return next(row for row in result["resources"] if row["kind"] == kind)

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


if __name__ == "__main__":
    import unittest
    unittest.main()

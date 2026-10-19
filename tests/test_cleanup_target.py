"""Cleanup after a real Governor delivery: a later merge that advances the target is normal; a target that no
longer contains the delivery result, or whose ancestry cannot be read, keeps every dependent resource protected."""

from __future__ import annotations

from unittest.mock import patch

import pod.cleanup as cleanup
from pod.errors import PodError
from pod.github import repository_context
from pod.governor import observe_candidate, prepare_candidate
from pod.internal import acknowledge, run as internal_run
from tests.kernel_support import KernelCase, GovernorFakePort, action, authorization, git
from tests.test_finalization import CONTRACT


class DeliveredTargetTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.child = self.root / "objective-worktree"
        git(self.project, "worktree", "add", "-qb", "objective", str(self.child))
        (self.child / "README.md").write_text("delivered\n")
        git(self.child, "commit", "-qam", "delivered change")
        self.candidate = git(self.child, "rev-parse", "HEAD")
        context = repository_context(self.child)
        self.intake(worktree={"repository": context["repository"], "repo_key": context["repo_key"],
                              "path": context["worktree"], "branch": context["branch"]})
        git(self.project, "remote", "add", "origin", str(self.project))
        for target, value in (("pod.internal._governor_projection", {"outstanding": []}),
                              ("pod.orca.contract", CONTRACT)):
            started = patch(target, return_value=value)
            started.start(); self.addCleanup(started.stop)
        remote = patch("pod.governor_effects.GhPort", side_effect=lambda _project: GovernorFakePort())
        remote.start(); self.addCleanup(remote.stop)
        self.result = self.deliver()

    def op(self, operation, **fields):
        request = {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}
        return acknowledge(operation, request, internal_run(operation, request))

    def deliver(self):
        """The coordinator's own merge through the Governor, its PASS outcome and the verified delivery record."""
        observed = observe_candidate(self.child, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "objective"})
        consent = authorization(candidate=observed["commit"], tree=observed["tree"], scope=("merge",),
                                target="origin/target")
        decided = self.op("governor", action=action(kind="merge", candidate=observed["commit"], target="origin/target",
                                                    unit="delivery", effects=[], authorization=consent))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "objective")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": result})
        self.op("checkpoint", value=self.core(obligations=self.stored(), delivery={"record": decided["record_id"]}))
        return result

    def orca_read(self, argv):
        if argv[:2] == ["worktree", "show"]:
            path = argv[argv.index("--worktree") + 1].removeprefix("path:")
            return {"result": {"worktree": {"path": path, "isMainWorktree": False}}}
        if argv[:2] == ["terminal", "list"]:
            return {"result": {"terminals": [], "truncated": False}}
        raise AssertionError(argv)

    def observed(self, **kwargs):
        return cleanup.plan(self.project, "objective", orca_reader=self.orca_read, native_port=self.port, **kwargs)

    def move_target(self, commit):
        git(self.project, "update-ref", "refs/heads/target", commit)
        git(self.project, "update-ref", "refs/remotes/origin/target", commit)

    def rows(self, result):
        return {row["kind"]: row for row in result["resources"] if row["kind"] in ("worktree", "local_branch")}

    def test_the_delivered_target_classifies_the_delivered_worktree_integrated(self):
        result = self.observed()
        rows = self.rows(result)
        self.assertEqual((result["target"]["commit"], result["target"]["moved_after_delivery"]), (self.result, False))
        self.assertEqual(rows["worktree"]["class"], "integrated")
        self.assertEqual(rows["worktree"]["delete_argv"],
                         ["orca", "worktree", "rm", "--worktree", "path:" + str(self.child), "--json"])

    def test_a_later_merge_that_advances_the_target_keeps_the_delivery_and_its_guarded_commands(self):
        before = self.observed()
        later = git(self.project, "commit-tree", self.result + "^{tree}", "-p", self.result, "-m", "a later merge")
        self.move_target(later)
        with self.assertRaises(PodError) as changed:
            self.observed(expect=before["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")
        result = self.observed()
        rows = self.rows(result)
        self.assertEqual((result["target"]["commit"], result["target"]["moved_after_delivery"]), (later, False))
        self.assertEqual((rows["worktree"]["class"], rows["worktree"]["reasons"]), ("integrated", []))
        self.assertEqual(rows["worktree"]["delete_argv"][:3], ["orca", "worktree", "rm"])
        self.assertEqual(rows["local_branch"]["class"], "integrated")

    def test_a_target_that_no_longer_contains_the_delivery_result_protects_every_dependent_resource(self):
        rewritten = git(self.project, "commit-tree", self.result + "^{tree}", "-m", "rewritten history")
        self.move_target(rewritten)
        result = self.observed()
        rows = self.rows(result)
        self.assertEqual((result["target"]["commit"], result["target"]["moved_after_delivery"]), (rewritten, True))
        for kind in ("worktree", "local_branch"):
            self.assertEqual((rows[kind]["class"], rows[kind]["delete_argv"]), ("protected", None), kind)

    def test_unreadable_ancestry_of_the_delivery_result_reads_moved_and_protects(self):
        later = git(self.project, "commit-tree", self.result + "^{tree}", "-p", self.result, "-m", "a later merge")
        self.move_target(later)
        real = cleanup.is_ancestor
        unread = lambda project, older, newer: None if older == self.result else real(project, older, newer)
        with patch.object(cleanup, "is_ancestor", side_effect=unread):
            result = self.observed()
        rows = self.rows(result)
        self.assertTrue(result["target"]["moved_after_delivery"])
        for kind in ("worktree", "local_branch"):
            self.assertEqual((rows[kind]["class"], rows[kind]["delete_argv"]), ("protected", None), kind)

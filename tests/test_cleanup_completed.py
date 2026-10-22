"""Completed-task cleanup through production entries and disposable Git/recording ports."""

from copy import deepcopy
import subprocess
from pathlib import Path
from unittest.mock import patch

from pod import cleanup, cleanup_stashes
from pod.errors import PodError
from pod.github import issue_intake, repository_context
from pod.internal import run
from pod.ledger import read
from tests.kernel_support import git
from tests.pod_tick_support import TickCase


class _CompletedFixture(TickCase):
    def setUp(self):
        super().setUp()
        self.child = self.root / "task-checkout"
        git(self.project, "worktree", "add", "-qb", "task-branch", str(self.child))
        self.terminals = []
        self.role = None

    def closed(self, *, body="- [x] **PoD#1 - Completed task.**"):
        self.issue.body = body
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        context = repository_context(self.child)
        self.intake(objective_source=source, worktree={key: context[key] for key in ("repository", "repo_key")}
                    | {"path": context["worktree"], "branch": context["branch"]})
        self.prove(close=True)

    def native(self, argv, **kwargs):
        if argv[:2] == ["worktree", "show"]:
            path = argv[3].removeprefix("path:")
            branch = git(Path(path), "symbolic-ref", "--short", "HEAD")
            return {"runtime": "runtime", "result": {"worktree": {
                "id": "fixture-" + Path(path).name, "path": path, "branch": branch,
                "isMainWorktree": Path(path) == self.project, "isBare": False, "role": self.role,
                "git": {"path": path, "branch": branch, "isBare": False}}}}
        if argv[:2] == ["terminal", "list"]:
            return {"runtime": "runtime", "result": {"terminals": list(self.terminals), "truncated": False}}
        raise AssertionError(argv)

    def observed(self, **options):
        # No remote Git/provider process or native mutation can escape these ports.
        original = cleanup.plan
        def observe(project, objective=None, **fields):
            return original(project, objective, orca_reader=self.native, native_port=self.port, **fields)
        with patch("pod.cleanup.plan", side_effect=observe):
            return run("cleanup-plan", {"project": str(self.project), "objective": "objective", **options})

    def row(self, result, kind="worktree"):
        return next(row for row in result["resources"] if row["kind"] == kind)

    def unique(self):
        (self.child / "README.md").write_text("unmerged task history\n")
        git(self.child, "commit", "-qam", "task leftover")
        (self.child / "README.md").write_text("uncommitted leftover\n")
        (self.child / "untracked.txt").write_text("untracked leftover\n")

    def assert_held(self, result):
        self.assertIsNone(self.row(result)["delete_argv"])


class CompletedCleanupTests(_CompletedFixture):
    def test_closed_current_proven_pods_allow_unmerged_dirty_git_phase_without_archives(self):
        self.unique()
        self.closed()
        before = read(self.project, "objective")
        result = self.observed()
        row = self.row(result)
        self.assertEqual(result["closure_basis"], "coordinator")
        self.assertEqual(row["class"], "unique")
        self.assertFalse(row["archive_verified"])
        self.assertEqual(row["archives"], [])
        self.assertIsNone(row["archive_argv"])
        self.assertIn(row["id"], result["standing_authorization"]["covers"])
        self.assertNotIn(row["id"], result["standing_authorization"]["asks"])
        self.assertEqual(row["delete_argv"], cleanup_stashes.worktree_argv(self.project, str(self.child)))
        self.assertIsNone(self.row(result, "local_branch")["delete_argv"])
        self.assertIn("reobserve", " ".join(self.row(result, "local_branch")["reasons"]))
        self.assertEqual(read(self.project, "objective"), before)
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        self.assertFalse(self.child.exists())
        self.assertTrue(git(self.project, "rev-parse", "refs/heads/task-branch"))

    def test_unrelated_real_stash_survives_the_actual_listed_git_effect(self):
        self.unique()
        sibling = self.root / "sibling"
        git(self.project, "worktree", "add", "-qb", "sibling", str(sibling))
        (sibling / "README.md").write_text("unrelated stash material\n")
        git(sibling, "stash", "push", "-qm", "fixture stash")
        root = git(self.project, "rev-parse", "refs/stash")
        common = self.project / ".git"
        retained = {name: (common / name).read_bytes() for name in ("refs/stash", "logs/refs/stash")}
        before = subprocess.run(["git", "-C", str(sibling), "cat-file", "--batch"],
                                input=(root + "\n" + root + "^{tree}\n").encode(), capture_output=True, check=True).stdout
        self.closed()
        row = self.row(self.observed())
        self.assertEqual({item["relation"] for item in row["stash_observation"]["relations"]}, {"unrelated"}, row)
        self.assertIsNotNone(row["delete_argv"])
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        self.assertEqual({name: (common / name).read_bytes() for name in retained}, retained)
        self.assertEqual(git(self.project, "rev-parse", "refs/stash"), root)
        after = subprocess.run(["git", "-C", str(sibling), "cat-file", "--batch"],
                               input=(root + "\n" + root + "^{tree}\n").encode(), capture_output=True, check=True).stdout
        self.assertEqual(after, before)

    def test_current_dirty_bytes_bind_expect_even_with_unchanged_status_and_tip(self):
        self.unique()
        self.closed()
        first = self.observed()
        (self.child / "untracked.txt").write_text("changed untracked bytes\n")
        with self.assertRaises(PodError) as caught:
            self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_unchanged_completion_and_current_facts_accept_the_same_expect(self):
        self.unique()
        self.closed()
        first = self.observed()
        self.assertEqual(self.observed(expect=first["expect"])["digest"], first["digest"])

    def completion_controls(self, changes):
        from pod.status import status
        self.closed()
        state = read(self.project, "objective")
        current = status(self.project, None, objective="objective")
        self.assertTrue(cleanup._completion(self.project, "objective", state, older=False)[0])
        for path, value in changes:
            view = deepcopy(current)
            parent = view
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            with self.subTest(path=path, value=value), patch("pod.status.status", return_value=view):
                self.assertFalse(cleanup._completion(self.project, "objective", state, older=False)[0])

    def test_complete_ticks_do_not_replace_current_quiet_coordinator_closure(self):
        self.completion_controls([
            (("status",), "unavailable"), (("report", "status"), "open"),
            (("readiness", "state"), "closed_external"), (("native", "complete"), False),
            (("pending_admissions",), 1), (("assignments", "active"), [{"dispatch": "open"}]),
            (("evidence_closure",), {"status": "closed"}), (("phase",), "retired"),
        ])

    def test_closure_does_not_replace_nonempty_current_mapped_pod_proof(self):
        self.completion_controls([
            (("pod_ticks", "lines"), []), (("pod_ticks", "facts_current"), False),
            (("pod_ticks", "lines", 0, "proven"), False),
            (("pod_ticks", "lines", 0, "criteria"), []),
            (("pod_ticks", "lines", 0, "authorship"), "ambiguous"),
            (("pod_ticks", "lines", 0, "status"), "ticked_without_pod_proof"),
            (("pod_ticks", "lines", 0, "blocked"), {"code": "line_duplicated"}),
            (("pod_ticks", "lines", 0, "occurrences"), 2),
        ])

    def test_native_changed_branch_identity_holds_completed_default(self):
        self.unique()
        self.closed()
        original = self.native
        def changed(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ["worktree", "show"]:
                result["result"]["worktree"]["branch"] = "different"
            return result
        with patch.object(self, "native", side_effect=changed):
            self.assert_held(self.observed())

    def test_changed_completion_during_current_observation_refuses(self):
        self.unique()
        self.closed()
        original = cleanup._completion
        count = 0
        def changing(*args, **kwargs):
            nonlocal count
            eligible, facts = original(*args, **kwargs)
            count += 1
            return (False, facts) if count > 1 else (eligible, facts)
        with patch.object(cleanup, "_completion", side_effect=changing), self.assertRaises(PodError) as caught:
            self.observed()
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_changed_live_source_blocks_default_without_promoting_cached_ticks(self):
        self.unique()
        self.closed()
        self.issue.body += "\n- [x] **PoD#2 - New unchecked proof.**"
        self.assert_held(self.observed())

    def test_unavailable_live_source_blocks_default(self):
        self.unique()
        self.closed()
        self.issue.unreadable = True
        self.assert_held(self.observed())

    def test_attached_terminal_still_holds_completed_dirty_worktree(self):
        self.unique()
        self.closed()
        self.terminals = [{"handle": "attached", "worktreePath": str(self.child)}]
        self.assert_held(self.observed())

    def test_role_worktree_still_holds_completed_task(self):
        self.unique()
        self.closed()
        self.role = "director"
        self.assert_held(self.observed())

    def test_locked_worktree_still_holds_completed_task(self):
        self.unique()
        self.closed()
        git(self.project, "worktree", "lock", str(self.child))
        self.assert_held(self.observed())

    def test_absent_worktree_branch_none_supplies_no_ownership_for_remaining_ref(self):
        self.closed()
        git(self.project, "worktree", "remove", str(self.child))
        result = self.observed()
        self.assertIsNone(self.row(result)["identity"]["branch"])
        branch = self.row(result, "local_branch")
        self.assertIsNone(branch["delete_argv"])
        self.assertIn("unproven", " ".join(branch["reasons"]))

    def test_private_stash_alias_stays_held_after_genuine_completion(self):
        (self.child / "README.md").write_text("retained stash\n")
        git(self.child, "stash", "push", "-qm", "fixture")
        git(self.child, "symbolic-ref", "refs/worktree/needed-stash", "refs/stash")
        self.closed()
        self.assert_held(self.observed())

    def test_default_and_shared_worktrees_stay_protected_after_completion(self):
        self.closed()
        git(self.project, "worktree", "add", "--force", "-q", str(self.root / "shared"), "task-branch")
        row = self.row(self.observed())
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])

    def test_default_branch_stays_protected_after_completion(self):
        self.closed()
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/task-branch")
        self.assert_held(self.observed())

    def test_missing_stash_log_stays_held_after_genuine_completion(self):
        (self.child / "README.md").write_text("retained stash\n")
        git(self.child, "stash", "push", "-qm", "fixture")
        (self.project / ".git/logs/refs/stash").unlink()
        self.closed()
        self.assert_held(self.observed())

    def test_opaque_native_and_scratch_selectors_never_invent_association_or_commands(self):
        self.closed()
        for kind, name in (("native", "unknown-dispatch"), ("cache", str(self.root))):
            with self.subTest(kind=kind):
                row = self.observed(observe={"kind": kind, "name": name})["resources"][0]
                self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
                self.assertIsNone(row["delete_argv"])

class CompletedRemoteTests(_CompletedFixture):
    def remote_plan(self):
        from pod.governor import _empty_journal, _record_path, _write_journal
        self.unique()
        self.closed()
        self.tip = git(self.child, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/task-branch", self.tip)
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": [], "preflight": {},
            "published": {"origin/task-branch": self.tip}, "tasks": [], "candidate": None,
            "branch": {"remote": "origin", "base": "target", "branch": "task-branch"}}
        _write_journal(_record_path(self.project, "objective"), journal)
        original = cleanup._git
        def observed(project, argv):
            if argv[:1] == ["ls-remote"]:
                return self.tip + "\trefs/heads/task-branch\n"
            return original(project, argv)
        return patch.object(cleanup, "_git", side_effect=observed)

    def test_completed_unmerged_remote_has_exact_lease_and_no_archive_or_question(self):
        with self.remote_plan():
            result = self.observed()
        row = self.row(result, "remote_branch")
        self.assertEqual(row["class"], "unique")
        self.assertFalse(row["archive_verified"])
        self.assertEqual(row["archives"], [])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["delete_argv"], ["git", "push", "--force-with-lease=refs/heads/task-branch:" + self.tip,
                                             "origin", ":refs/heads/task-branch"])
        self.assertIn(row["id"], result["standing_authorization"]["covers"])
        self.assertNotIn(row["id"], result["standing_authorization"]["asks"])

    def test_changed_remote_oid_refuses_the_completed_plan_expect(self):
        with self.remote_plan():
            first = self.observed()
            self.tip = git(self.project, "commit-tree", self.base + "^{tree}", "-p", self.tip, "-m", "remote changed")
            with self.assertRaises(PodError) as caught:
                self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

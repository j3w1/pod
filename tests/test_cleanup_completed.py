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
        self.path_terminals = {}
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
            # A quiet fixture native entry can persist after the actual Git phase.
            # Its null branch is never the source of task/ref association.
            branch = git(Path(path), "symbolic-ref", "--short", "HEAD") if Path(path).exists() else None
            return {"runtime": "runtime", "result": {"worktree": {
                "id": "fixture-" + Path(path).name, "path": path, "branch": branch,
                "isMainWorktree": Path(path) == self.project, "isBare": False, "role": self.role,
                "git": {"path": path, "branch": branch, "isBare": False}}}}
        if argv[:2] == ["terminal", "list"]:
            path = argv[3].removeprefix("path:")
            return {"runtime": "runtime", "result": {
                "terminals": list(self.terminals) + list(self.path_terminals.get(path, [])), "truncated": False}}
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
        self.assertEqual(row["delete_argv"], cleanup_stashes.worktree_argv(self.project, str(self.child), completed=True))
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

    def test_unrelated_stash_with_terminals_elsewhere_keeps_the_completed_force_phase(self):
        self.unique()
        sibling = self.root / "sibling"
        git(self.project, "worktree", "add", "-qb", "sibling", str(sibling))
        (sibling / "README.md").write_text("unrelated stash material\n")
        git(sibling, "stash", "push", "-qm", "fixture stash")
        (sibling / "README.md").write_text("second unrelated stash material\n")
        git(sibling, "stash", "push", "-qm", "second fixture stash")
        detached = self.root / "detached-review"
        git(self.project, "worktree", "add", "-q", "--detach", str(detached))
        common = self.project / ".git"
        retained = {name: (common / name).read_bytes() for name in ("refs/stash", "logs/refs/stash")}
        self.closed()
        self.path_terminals = {str(sibling): [{"handle": "exited-reviewer", "worktreePath": str(sibling)}],
                               str(self.project): [{"handle": "coordinator", "worktreePath": str(self.project)}]}
        reads = []
        original = self.native
        def native(argv, **kwargs):
            reads.append(list(argv))
            return original(argv, **kwargs)
        self.native = native
        row = self.row(self.observed())
        self.assertEqual({item["relation"] for item in row["stash_observation"]["relations"]}, {"unrelated"}, row)
        self.assertEqual(row["delete_argv"], cleanup_stashes.worktree_argv(self.project, str(self.child), completed=True))
        self.assertNotIn(["terminal", "show"], [argv[:2] for argv in reads])
        self.assertFalse({"path:" + str(sibling), "path:" + str(detached), "path:" + str(self.project)}
                         & {argv[3] for argv in reads if len(argv) > 3})
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        self.assertEqual({name: (common / name).read_bytes() for name in retained}, retained)

    def test_completed_force_argv_starts_no_configured_fsmonitor(self):
        self.unique()
        self.closed()
        marker, hook = self.root / "fsmonitor-marker", self.root / "fsmonitor-hook"
        hook.write_text('#!/bin/sh\necho ran >> "%s"\nexit 1\n' % marker)
        hook.chmod(0o755)
        git(self.project, "config", "core.fsmonitor", str(hook))
        row = self.row(self.observed())
        argv = row["delete_argv"]
        position = argv.index("core.hooksPath=/dev/null")
        self.assertEqual(argv[position + 1:position + 3], ["-c", "core.fsmonitor=false"])
        self.assertEqual(argv.count("--force"), 1)
        subprocess.run(argv, capture_output=True, check=True)
        self.assertFalse(self.child.exists())
        self.assertFalse(marker.exists())

    def test_applicable_filter_keeps_the_completed_force_form_whose_effect_starts_no_helper(self):
        self.unique()
        self.closed()
        marker = self.root / "filter-marker"
        git(self.project, "config", "filter.probe.clean", f"sh -c 'echo ran >> {marker}; cat'")
        (self.project / ".git" / "info" / "attributes").write_text("* filter=probe\n")
        row = self.row(self.observed())
        self.assertEqual(row["delete_argv"], cleanup_stashes.worktree_argv(self.project, str(self.child), completed=True))
        # Pod's unchanged general status inspection may itself run the helper for the
        # dirty file (pre-existing advisory); the forced effect runs no clean check.
        inspected = marker.read_text() if marker.exists() else ""
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        self.assertFalse(self.child.exists())
        self.assertEqual(marker.read_text() if marker.exists() else "", inspected)

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

    def test_absent_worktree_without_recorded_branch_binding_holds_remaining_ref(self):
        self.closed()
        git(self.project, "worktree", "remove", str(self.child))
        state = read(self.project, "objective")
        state["checkpoint"]["worktree"]["branch"] = None
        with patch("pod.cleanup.read", return_value=state):
            branch = self.observed(observe={"kind": "local_branch", "name": "refs/heads/task-branch"})["resources"][0]
        self.assertEqual(branch["identity"]["current"]["status"], "present")
        self.assertEqual(branch["identity"]["provenance"]["status"], "unavailable")
        self.assertIsNone(branch["delete_argv"])

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

class CompletedRemainingRefTests(_CompletedFixture):
    ref = "refs/heads/task-branch"

    def remaining(self):
        self.unique()
        self.closed()
        self.tip = git(self.project, "rev-parse", self.ref)
        subprocess.run(cleanup_stashes.worktree_argv(self.project, str(self.child), completed=True), capture_output=True, check=True)
        self.assertFalse(self.child.exists())

    def selected(self, **options):
        return self.observed(observe={"kind": "local_branch", "name": self.ref}, **options)

    def assert_remaining(self, row):
        self.assertEqual(row["class"], "unique")
        self.assertEqual(row["delete_argv"], cleanup_stashes.local_argv(self.project, self.ref, self.tip))
        self.assertEqual(row["archives"], [])
        self.assertFalse(row["archive_verified"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["identity"]["checked_out"], [])
        self.assertEqual(row["identity"]["current"], {"status": "present", "path": None, "ref": self.ref, "oid": self.tip})
        self.assertEqual(row["identity"]["provenance"], {
            "status": "observed", "source": "objective_checkpoint", "reference": "objective"})
        self.assertEqual(row["identity"]["ref_storage"]["current"]["oid"], self.tip)
        self.assertTrue(row["identity"]["ref_storage"]["freshness"]["stable"])
        self.assertEqual(row["stash_observation"]["executor"]["profile"], cleanup_stashes.PROFILE)
        self.assertEqual(row["stash_observation"]["footprint"]["status"], "observed")

    def test_genuine_completed_recorded_ref_after_actual_worktree_removal_has_guarded_command(self):
        self.remaining()
        result = self.observed()
        row = self.row(result, "local_branch")
        self.assert_remaining(row)
        self.assertIn(row["id"], result["standing_authorization"]["covers"])
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        absent = subprocess.run(["git", "-C", str(self.project), "show-ref", "--verify", self.ref], capture_output=True)
        self.assertNotEqual(absent.returncode, 0)

    def test_selected_remaining_ref_preserves_current_provenance_storage_stash_and_expect(self):
        self.remaining()
        result = self.selected()
        row = result["resources"][0]
        self.assert_remaining(row)
        self.assertEqual(result["expect"]["resources"], {row["id"]: row["fingerprint"]})

    def test_missing_objective_record_cannot_supply_current_ref_eligibility(self):
        self.remaining()
        with patch("pod.cleanup.read", return_value=None):
            row = self.selected()["resources"][0]
        self.assertEqual(row["identity"]["current"]["status"], "present")
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])

    def test_wrong_recorded_branch_cannot_borrow_the_actual_remaining_ref(self):
        self.remaining()
        state = read(self.project, "objective")
        state["checkpoint"]["worktree"]["branch"] = "different"
        with patch("pod.cleanup.read", return_value=state):
            row = self.selected()["resources"][0]
        self.assertIsNone(row["delete_argv"])
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")

    def ambiguous(self, path, branch):
        state = read(self.project, "objective")
        # Negative record-port disagreement only; no private or accepted record is written.
        state["admissions"]["conflict"] = {"admission_id": "conflict", "state": "closed", "recovery": {
            "placement_binding": {"path": str(path), "branch": branch}}}
        with patch("pod.cleanup.read", return_value=state):
            row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("ambiguous", " ".join(row["reasons"]))

    def test_disagreeing_bindings_on_the_same_recorded_path_hold(self):
        self.remaining()
        self.ambiguous(self.child, "different")

    def test_two_recorded_paths_for_the_same_ref_are_ambiguous(self):
        self.remaining()
        self.ambiguous(self.root / "other-placement", "task-branch")

    def test_admission_binding_is_current_selection_data_without_creation_history(self):
        self.remaining()
        state = read(self.project, "objective")
        placement = state["checkpoint"].pop("worktree")
        state["admissions"]["recorded"] = {"admission_id": "recorded", "recovery": {"placement_binding": placement}}
        from pod.cleanup_archives import repository_observation
        facts = cleanup._remaining_local_ref(self.project, "objective", state, self.ref, {},
                                             repository_observation(self.project), self.native)
        self.assertEqual(facts["status"], "observed")
        self.assertEqual(facts["provenance"], {"status": "observed", "source": "admission", "reference": "recorded"})
        self.assertNotIn("creation", facts)

    def test_current_checkout_elsewhere_still_holds_completed_ref(self):
        self.remaining()
        git(self.project, "worktree", "add", "-q", str(self.root / "shared"), "task-branch")
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertTrue(row["identity"]["checked_out"])

    def test_recreated_unregistered_path_is_not_completed_resource_absence(self):
        self.remaining()
        self.child.mkdir()
        (self.child / "valuable.txt").write_text("unrelated current path\n")
        self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_current_attached_native_terminal_holds_after_git_worktree_absence(self):
        self.remaining()
        self.terminals = [{"handle": "attached", "worktreePath": str(self.child)}]
        self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_current_role_entry_holds_after_git_worktree_absence(self):
        self.remaining()
        self.role = "director"
        self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_incomplete_native_terminal_read_stays_held(self):
        self.remaining()
        original = self.native
        def incomplete(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ["terminal", "list"]:
                result["result"]["truncated"] = True
            return result
        with patch.object(self, "native", side_effect=incomplete):
            self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_unavailable_native_observation_is_not_quiet_absence(self):
        self.remaining()
        with patch.object(self, "native", side_effect=PodError("orca_unavailable", "unavailable")):
            self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_held_native_role_change_still_changes_expect(self):
        self.remaining()
        self.role = "director"
        first = self.observed()
        self.role = "lead"
        with self.assertRaises(PodError) as caught:
            self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_missing_live_source_completion_still_holds_the_remaining_ref(self):
        self.remaining()
        self.issue.unreadable = True
        self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_changed_ref_oid_refuses_old_expect_and_the_actual_old_oid_command(self):
        self.remaining()
        first = self.observed()
        old_argv = self.row(first, "local_branch")["delete_argv"]
        new = git(self.project, "commit-tree", self.base + "^{tree}", "-p", self.tip, "-m", "changed current ref")
        git(self.project, "update-ref", self.ref, new)
        stale = subprocess.run(old_argv, capture_output=True)
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(git(self.project, "rev-parse", self.ref), new)
        with self.assertRaises(PodError) as caught:
            self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_equal_oid_ref_file_replacement_refuses_old_expect(self):
        self.remaining()
        first = self.observed()
        path = self.project / ".git" / self.ref
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with self.assertRaises(PodError) as caught:
            self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_unchanged_current_remaining_ref_accepts_its_own_expect(self):
        self.remaining()
        first = self.observed()
        self.assertEqual(self.observed(expect=first["expect"])["digest"], first["digest"])

    def packed_identity(self):
        path = self.project / ".git" / "packed-refs"
        info = path.stat()
        return path.read_bytes(), (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)

    def test_unrelated_packed_refs_file_keeps_the_loose_remaining_ref_command_and_bytes(self):
        self.remaining()
        git(self.project, "tag", "unrelated-packed")
        git(self.project, "pack-refs")
        packed, identity = self.packed_identity()
        self.assertNotIn(self.ref.encode(), packed)
        self.assertTrue((self.project / ".git" / self.ref).is_file())
        row = self.row(self.observed(), "local_branch")
        self.assert_remaining(row)
        self.assertEqual(row["identity"]["ref_storage"]["packed"]["status"], "present")
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        absent = subprocess.run(["git", "-C", str(self.project), "show-ref", "--verify", self.ref], capture_output=True)
        self.assertNotEqual(absent.returncode, 0)
        self.assertEqual(self.packed_identity(), (packed, identity))
        self.assertFalse((self.project / ".git" / "packed-refs.lock").exists())

    def test_changed_packed_refs_file_refuses_the_old_remaining_ref_expect(self):
        self.remaining()
        git(self.project, "tag", "unrelated-packed")
        git(self.project, "pack-refs")
        first = self.observed()
        git(self.project, "tag", "another-packed")
        git(self.project, "pack-refs")
        with self.assertRaises(PodError) as caught:
            self.observed(expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_packed_only_selected_entry_holds_the_remaining_ref(self):
        self.remaining()
        git(self.project, "pack-refs", "--all")
        self.assertFalse((self.project / ".git" / self.ref).exists())
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])

    def test_alias_of_the_remaining_ref_holds_even_with_an_empty_stash(self):
        self.remaining()
        git(self.project, "symbolic-ref", "refs/heads/task-alias", self.ref)
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("primitive is unavailable", " ".join(row["reasons"]))
        self.assertIn("ref_alias_overlap", row["stash_observation"]["footprint"]["reasons"])
        git(self.project, "symbolic-ref", "--delete", "refs/heads/task-alias")
        other = self.root / "alias-holder"
        git(self.project, "worktree", "add", "-q", "--detach", str(other))
        git(other, "symbolic-ref", "refs/worktree/needs-task", self.ref)
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("ref_alias_overlap", row["stash_observation"]["footprint"]["reasons"])

    def test_unreadable_registered_worktree_holds_the_remaining_ref_even_with_an_empty_stash(self):
        self.remaining()
        other = self.root / "unreadable-sibling"
        git(self.project, "worktree", "add", "-q", "--detach", str(other))
        other.rename(self.root / "parked-sibling")
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertEqual(row["stash_observation"]["footprint"]["status"], "unavailable")

    def stash(self):
        (self.project / "README.md").write_text("unrelated retained stash\n")
        git(self.project, "stash", "push", "-qm", "fixture stash")

    def test_unrelated_retained_stash_stays_byte_identical_through_actual_ref_deletion(self):
        self.remaining()
        self.stash()
        paths = [self.project / ".git" / name for name in ("refs/stash", "logs/refs/stash")]
        before = [path.read_bytes() for path in paths]
        row = self.row(self.observed(), "local_branch")
        self.assert_remaining(row)
        self.assertEqual({value["relation"] for value in row["stash_observation"]["relations"]}, {"unrelated"})
        subprocess.run(row["delete_argv"], capture_output=True, check=True)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_actual_symbolic_private_dependency_keeps_remaining_ref_held(self):
        self.remaining()
        self.stash()
        git(self.project, "symbolic-ref", "refs/worktree/needed-name", self.ref)
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertEqual({value["relation"] for value in row["stash_observation"]["relations"]}, {"related"})

    def test_missing_stash_log_is_not_empty_after_actual_worktree_removal(self):
        self.remaining()
        self.stash()
        (self.project / ".git/logs/refs/stash").unlink()
        self.assertIsNone(self.row(self.observed(), "local_branch")["delete_argv"])

    def test_packed_storage_keeps_the_unprofiled_effect_held_even_with_empty_stash(self):
        self.remaining()
        git(self.project, "pack-refs", "--all", "--no-prune")
        row = self.row(self.observed(), "local_branch")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("primitive is unavailable", " ".join(row["reasons"]))

    def test_unknown_footprint_keeps_empty_stash_remaining_ref_held(self):
        self.remaining()
        original = cleanup_stashes._footprint
        def unknown(*args, **kwargs):
            value = original(*args, **kwargs)
            value.update(status="unavailable", opaque=["effect_footprint_unavailable"], reasons=["effect_footprint_unavailable"])
            return value
        with patch.object(cleanup_stashes, "_footprint", side_effect=unknown):
            row = self.row(self.observed(), "local_branch")
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

"""pod#292: a new terminal in the recorded worktree takes the owner handoff, binding the Run inside it.

Orca alone is replaced. A new terminal has no current Run, so its handoff reads the Run's coordinator by run show,
must run in the recorded worktree, and keeps every other R105 check; status names only a route a caller can take.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import repository_context
from pod.ledger import objective_root
from tests.kernel_support import git
from tests.test_owner_handoff import HandoffCase, SENTINEL


class UnlinkedCase(HandoffCase):
    def binding(self):
        context = repository_context(self.project)
        return {"repository": context["repository"], "repo_key": context["repo_key"],
                "path": context["worktree"], "branch": context["branch"]}

    def core(self, **extra):
        return super().core(**{"worktree": self.binding(), **extra})

    def packet(self, serves, **extra):
        return super().packet(serves, **{"worktree": self.binding(), **extra})

    def mutate(self, argv, **kwargs):
        if self.current is not None:
            return super().mutate(argv, **kwargs)
        # The unlinked caller's run-use: the Run now names this terminal, its current Run.
        self.mutations.append(list(argv))
        state = self.state()
        self.phase1.append(deepcopy(state))
        self.assertEqual(state["checkpoint"]["continuity"]["history"][-1]["state"], "pending")
        self.assertEqual(argv, ["orchestration", "run-use", "--id", "run", "--json", "--full"])
        if self.mode not in ("refused", "error_unchanged"):
            self.current = {"id": "run", "coordinator_handle": os.environ["ORCA_TERMINAL_HANDLE"],
                            "consumer_generation": 2}
        if self.mode in ("lost", "error_unchanged"):
            raise PodError("native_effect_uncertain", SENTINEL)
        return {"runtime": self.port.runtime, "exit": 1 if self.mode == "refused" else 0,
                "error": {"code": "refused"} if self.mode == "refused" else None, "result": {}}

    def native_read(self, argv, **kwargs):
        if argv[:2] == ["orchestration", "run-show"] and self.current is None and getattr(self, "shown", None):
            if self.shown == "fail":
                raise PodError("orca_unavailable", SENTINEL)
            return {"runtime": self.port.runtime, "result": {"run": {"id": argv[3], **self.shown}}}
        return super().native_read(argv, **kwargs)

    def unlink(self, caller="next"):
        """The recorded coordinator is lost and a brand-new terminal, bound to no Run, reads."""
        self.establish(); self.lose(caller); self.current = None

    @contextmanager
    def inside(self, where=None):
        old = Path.cwd(); os.chdir(where or self.project)
        try:
            yield
        finally:
            os.chdir(old)

    def elsewhere(self):
        other = self.root / "other"
        if not other.exists():
            git(self.project, "worktree", "add", "-q", "-b", "elsewhere", str(other))
        return other

    def take(self, decision=None):
        with self.inside():
            return self.handoff(decision)

    def verdict(self):
        return self.status()["owner_handoff"]


class UnlinkedHandoffTests(UnlinkedCase):
    def test_a_new_terminal_in_the_recorded_worktree_completes_the_handoff(self):
        self.unlink(); before = self.state(); journal = (objective_root(self.project, "objective") / "governor.json").read_bytes()
        verdict = self.verdict()
        self.assertEqual((verdict["status"], verdict["condition"]), ("handoff_available", "fresh"))
        self.assertEqual(verdict["facts"]["to_owner"], "next")
        result = self.take()
        self.assertEqual(result["status"], "handed_off")
        self.assertEqual(self.entry()["state"], "done")
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.state()["owner"], "next")
        self.assertEqual(self.state()["checkpoint"]["continuity"]["binding"],
                         {"run": "run", "coordinator": "next", "generation": 2})
        self.assert_only_handoff_changes(deepcopy(before), deepcopy(self.phase1[0]), completed=False)
        self.assert_only_handoff_changes(deepcopy(before), self.state(), completed=True)
        self.assertEqual((objective_root(self.project, "objective") / "governor.json").read_bytes(), journal)
        self.assertEqual(len(self.port.starts), 1)
        self.assertNotIn(SENTINEL, json.dumps(self.state()))

    def test_a_live_old_coordinator_still_refuses(self):
        self.unlink(); self.terminals["owner"] = "live"; before = self.snapshot()
        self.assertEqual(self.verdict()["condition"], "owner_not_lost")
        error = self.refused(lambda: self.take(self.decision()), code="owner_handoff_refused")
        self.assertEqual(error.detail["owner_handoff"]["condition"], "owner_not_lost")
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])

    def test_a_caller_outside_the_recorded_worktree_refuses_and_is_told_where(self):
        self.unlink()
        other = self.elsewhere()
        decision = self.decision(); before = self.snapshot()
        with self.inside(other):
            error = self.refused(lambda: self.handoff(decision), code="owner_handoff_refused")
        verdict = error.detail["owner_handoff"]
        self.assertEqual((verdict["condition"], verdict["definitive"]), ("worktree_changed", False))
        self.assertIn("recorded worktree " + self.binding()["path"], verdict["next_action"])
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])

    def assert_run_row(self, shown, condition):
        self.unlink(); self.shown = shown; before = self.snapshot()
        self.assertEqual(self.verdict()["condition"], condition)
        facts = {**self.verdict()["facts"], "provenance": "user_direct", "instruction": "Make this terminal the coordinator"}
        self.refused(lambda: self.take(facts))
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])

    # Every R105 check still applies to an unlinked caller, one Run shape per method.
    def test_a_run_rebound_to_another_refuses(self):
        self.assert_run_row({"coordinator_handle": "third", "consumer_generation": 2}, "lineage_unproven")

    def test_an_advanced_generation_refuses(self):
        self.assert_run_row({"coordinator_handle": "owner", "consumer_generation": 8}, "generation_changed")

    def test_an_unreadable_run_refuses(self):
        self.assert_run_row("fail", "run_unreadable")

    def test_a_run_without_a_shown_coordinator_refuses(self):
        self.assert_run_row({"consumer_generation": 1}, "run_unreadable")

    def test_a_run_already_naming_the_caller_refuses(self):
        self.assert_run_row({"coordinator_handle": "next", "consumer_generation": 2}, "binding_unstable")

    def test_a_worker_terminal_cannot_take_the_handoff_unlinked(self):
        self.unlink(); self.extra_workers = [{"dispatchId": "other-worker", "terminalHandle": "next"}]
        self.assertEqual(self.verdict()["condition"], "caller_is_worker")


class NoRecordedWorktreeTests(HandoffCase):
    def test_no_recorded_worktree_names_the_successor_claim(self):
        self.establish(); self.lose(); self.current = None
        verdict = self.status()["owner_handoff"]
        self.assertEqual(verdict["condition"], "caller_not_linked")
        self.assertIn("records no worktree", verdict["message"])
        self.assertIn("successor objective", verdict["next_action"])


class UnlinkedTransitionTests(UnlinkedCase):
    def assert_outcome(self, mode, expected):
        self.unlink(); self.mode = mode; decision = self.decision()
        self.refused(lambda: self.take(decision))
        self.assertEqual(self.entry()["state"], expected)
        self.assertEqual(len(self.mutations), 1)
        return decision

    def test_a_refused_run_use_aborts_on_the_run_readback(self):
        self.assert_outcome("refused", "aborted")

    def test_an_unknown_run_use_with_an_unchanged_run_aborts(self):
        self.assert_outcome("error_unchanged", "aborted")

    def test_a_landed_run_use_stays_pending_then_completes_once(self):
        decision = self.assert_outcome("lost", "pending")
        # The landed rebind completes as Branch 2, with no second run-use.
        self.mode = "success"; self.take(decision)
        self.assertEqual((self.entry()["state"], len(self.mutations)), ("done", 1))

    def test_a_landed_unlinked_handoff_never_completes_outside_the_recorded_worktree(self):
        decision = self.assert_outcome("lost", "pending")
        other = self.elsewhere(); before = self.snapshot()
        self.mode = "success"
        with self.inside(other):
            error = self.refused(lambda: self.handoff(decision), code="owner_handoff_refused")
        self.assertEqual(error.detail["owner_handoff"]["condition"], "worktree_changed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.entry()["state"], len(self.mutations), self.state()["owner"]), ("pending", 1, "owner"))
        self.assertTrue(self.entry()["verified"]["unlinked"])
        self.take(decision)  # Back in the recorded worktree it completes, still with one run-use.
        self.assertEqual((self.entry()["state"], len(self.mutations)), ("done", 1))

    def test_a_wrong_place_caller_never_ends_a_lost_holders_attempt(self):
        self.assert_outcome("lost", "pending")
        self.current = None  # The replacing terminal is bound to no Run; the lost holder's run-use never landed.
        self.terminals["next"] = "stale"; os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        other = self.elsewhere(); decision = self.decision(); before = self.snapshot()
        with self.inside(other):
            error = self.refused(lambda: self.handoff(decision), code="owner_handoff_refused")
        self.assertEqual((error.detail["owner_handoff"]["condition"], error.detail["owner_handoff"]["definitive"]),
                         ("worktree_changed", False))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.entry()["state"], self.entry()["decision"]["to_owner"]), ("pending", "next"))
        # An eligible caller in the recorded worktree may still replace it.
        self.mode = "success"; self.take(decision)
        self.assertEqual((self.state()["owner"], self.entry()["state"]), ("third", "done"))
        self.assertEqual(self.entry()["abort_reason"], "pending caller handle stale or gone")

    def test_an_unreadable_run_after_a_refused_run_use_stays_pending(self):
        self.unlink(); self.mode = "refused"; decision = self.decision()
        original = self.native_read

        def failing(argv, **kwargs):
            if argv[:2] == ["orchestration", "run-show"] and self.mutations:
                raise PodError("orca_unavailable", SENTINEL)
            return original(argv, **kwargs)
        with patch("pod.orca.read_command", side_effect=failing):
            self.refused(lambda: self.take(decision))
        self.assertEqual(self.entry()["state"], "pending")


class RecoveryRouteTests(UnlinkedCase):
    """The status recovery list offers owner handoff only where a new terminal can take it."""

    def route(self):
        from pod.readiness import _handoff_route
        facts = {"handle": "owner", "run": "run", "generation": 1, "runtime": self.port.runtime, "liveness": "lost"}
        return _handoff_route(self.state(), facts)

    def test_available_names_the_recorded_worktree(self):
        self.unlink()
        route = self.route()
        self.assertEqual((route["status"], route["condition"]), ("available", "fresh"))
        self.assertIn("recorded worktree " + self.binding()["path"], route["message"])
        self.assertNotIn("on Run", route["message"])

    def test_a_changed_branch_in_the_recorded_worktree_reads_blocked(self):
        self.unlink()
        git(self.project, "checkout", "-q", "-b", "moved")
        route = self.route()
        self.assertEqual((route["status"], route["condition"]), ("blocked", "worktree_changed"))
        self.assertIn("successor claim", route["message"])
        with self.inside():  # The handoff itself agrees: no terminal there can take it.
            self.assertEqual(self.verdict()["condition"], "worktree_changed")

    def test_a_missing_recorded_worktree_reads_blocked(self):
        self.unlink()
        with patch("pod.window._worktree_directory", return_value=False):
            route = self.route()
        self.assertEqual((route["status"], route["condition"]), ("blocked", "worktree_changed"))
        self.assertIn("successor claim", route["message"])

    def test_an_unreadable_recorded_worktree_reads_unverified(self):
        self.unlink()
        with patch("pod.window._worktree_directory", side_effect=PodError("worktree_unreadable", SENTINEL)):
            route = self.route()
        self.assertEqual((route["status"], route["condition"]), ("unverified", "worktree_unreadable"))

    def test_a_pending_resume_stays_available_to_its_holder(self):
        self.unlink(); self.mode = "lost"; self.refused(lambda: self.take(self.decision()))
        route = self.route()
        self.assertEqual((route["status"], route["condition"]), ("available", "resume"))
        self.assertIn("pending handoff", route["message"])


class NoRecordedWorktreeRouteTests(HandoffCase):
    def test_an_unrecorded_worktree_reads_blocked(self):
        from pod.readiness import _handoff_route
        self.establish(); self.lose(); self.current = None
        route = _handoff_route(self.state(), {"handle": "owner", "run": "run", "generation": 1,
                                              "runtime": self.port.runtime, "liveness": "lost"})
        self.assertEqual((route["status"], route["condition"]), ("blocked", "caller_not_linked"))
        self.assertIn("unrecorded", route["message"]); self.assertIn("successor claim", route["message"])


if __name__ == "__main__":
    unittest.main()

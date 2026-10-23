"""Discovery's shared deadline bounds local Git reads without changing other callers."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod import github, gitio, orca
from pod.errors import PodError
from pod.window import Budget


class SyntheticBudget(Budget):
    def __init__(self, seconds):
        self.now = 0.0
        self._clock = lambda: self.now
        self.deadline = seconds


class GitBudgetChecks:
    original_timeout = 30

    def setUp(self):
        self.process = self.enterContext(patch("subprocess.run", return_value=
            subprocess.CompletedProcess([], 0, stdout="answer", stderr="")))
        self.enterContext(patch("pod.github.git_executable", return_value=Path("git")))

    def test_each_read_uses_the_remaining_budget(self):
        budget = SyntheticBudget(5)
        with orca.read_deadline(budget):
            self.invoke()
            self.assertEqual(self.process.call_args.kwargs["timeout"], 5)
            budget.now = 4.75
            self.invoke()
            self.assertEqual(self.process.call_args.kwargs["timeout"], 0.25)

    def test_a_large_budget_keeps_the_original_timeout(self):
        with orca.read_deadline(SyntheticBudget(100)):
            self.invoke()
        self.assertEqual(self.process.call_args.kwargs["timeout"], self.original_timeout)

    def test_exhaustion_starts_no_process(self):
        for left in (0, -1):
            with self.subTest(left=left), orca.read_deadline(SyntheticBudget(left)):
                with self.assertRaises(PodError) as refused:
                    self.invoke()
                self.assertEqual(refused.exception.code, orca.DEADLINE_SPENT)
        self.process.assert_not_called()

    def test_exhaustion_after_a_read_starts_no_next_process(self):
        budget = SyntheticBudget(1)
        with orca.read_deadline(budget):
            self.invoke()
            budget.now = 1
            with self.assertRaises(PodError) as refused:
                self.invoke()
        self.assertEqual(refused.exception.code, orca.DEADLINE_SPENT)
        self.process.assert_called_once()

    def test_a_deadline_timeout_reports_budget_exhaustion(self):
        budget = SyntheticBudget(1)

        def expire(*args, **kwargs):
            budget.now = 1
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        self.process.side_effect = expire
        with orca.read_deadline(budget), self.assertRaises(PodError) as refused:
            self.invoke()
        self.assertEqual(refused.exception.code, orca.DEADLINE_SPENT)

    def test_without_a_deadline_the_original_timeout_is_unchanged(self):
        self.invoke()
        self.assertEqual(self.process.call_args.kwargs["timeout"], self.original_timeout)

    def test_leaving_the_deadline_restores_the_original_timeout(self):
        with orca.read_deadline(SyntheticBudget(1)):
            self.invoke()
        self.invoke()
        self.assertEqual(self.process.call_args.kwargs["timeout"], self.original_timeout)

    def test_an_early_timeout_keeps_the_original_failure(self):
        self.process.side_effect = subprocess.TimeoutExpired("git", self.original_timeout)
        with orca.read_deadline(SyntheticBudget(100)):
            self.assert_unavailable()

    def test_without_a_deadline_errors_are_unchanged(self):
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("git", self.original_timeout)):
            with self.subTest(error=type(error).__name__):
                self.process.side_effect = error
                self.assert_unavailable()


class ReadTests(GitBudgetChecks, unittest.TestCase):
    def invoke(self, **kwargs):
        return gitio.read(Path("."), ["rev-parse", "HEAD"], **kwargs)

    def assert_unavailable(self):
        self.assertIsNone(self.invoke())

    def test_an_explicit_timeout_stays_below_both_limits(self):
        for left, expected in ((100, 7), (0.5, 0.5)):
            with self.subTest(left=left), orca.read_deadline(SyntheticBudget(left)):
                self.invoke(timeout=7, binary=True)
                self.assertEqual(self.process.call_args.kwargs["timeout"], expected)
                self.assertFalse(self.process.call_args.kwargs["text"])
        self.invoke(timeout=7)
        self.assertEqual(self.process.call_args.kwargs["timeout"], 7)


class InventoryTests(GitBudgetChecks, unittest.TestCase):
    def invoke(self):
        with gitio.worktree_inventory(Path(".")) as inventory:
            return inventory.read()

    def assert_unavailable(self):
        with self.assertRaises(PodError) as refused:
            self.invoke()
        self.assertEqual(refused.exception.code, "git_unavailable")


class LocalGitTests(GitBudgetChecks, unittest.TestCase):
    original_timeout = 20

    def invoke(self, **kwargs):
        return github._local_git(Path("."), ["remote", "get-url", "origin"], **kwargs)

    def assert_unavailable(self):
        with self.assertRaises(PodError) as refused:
            self.invoke()
        self.assertEqual(refused.exception.code, "git_unavailable")

    def test_an_explicit_timeout_stays_below_both_limits(self):
        for left, expected in ((100, 7), (0.5, 0.5)):
            with self.subTest(left=left), orca.read_deadline(SyntheticBudget(left)):
                self.invoke(timeout=7)
                self.assertEqual(self.process.call_args.kwargs["timeout"], expected)
        self.invoke(timeout=7)
        self.assertEqual(self.process.call_args.kwargs["timeout"], 7)

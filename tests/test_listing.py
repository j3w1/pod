"""R108: the read-only list of objectives that touch a repository or worktree."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod import cli, listing
from pod.ledger import objective_root, state_root
from tests.kernel_support import KernelCase, git


def offline(*_args, **_kwargs):
    raise AssertionError("the objective list reads no Orca state")


class ListingCase(KernelCase):
    def listed(self, **scope):
        with patch("pod.orca.read_command", side_effect=offline):
            return listing.objective_list(self.project, **scope)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        old = Path.cwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(out), redirect_stderr(err), patch("pod.orca.read_command", side_effect=offline):
                code = cli.main(["status", *args])
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def other_repository(self) -> Path:
        other = self.root / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        return other

    def raw(self, path: Path, value) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "context.json").write_text(value if isinstance(value, str) else json.dumps(value))
        return path


class ListingTests(ListingCase):
    def test_absent_state_root_is_distinct_from_an_empty_list(self):
        self.assertFalse(state_root(self.project).exists())
        result = self.listed()
        self.assertEqual((result["status"], result["objectives"], result["complete"]), ("absent", [], True))
        self.intake()
        result = self.listed()
        self.assertEqual(result["status"], "listed")
        self.assertEqual([row["objective"] for row in result["objectives"]], ["objective"])

    def test_complete_list_names_phase_runs_and_locations_for_this_repository_only(self):
        self.intake()
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="closed-one",
                   governance={"base_ref": "target"}, close=True)
        other = self.other_repository()
        self.raw(objective_root(other, "foreign"), {"schema": "pod-context/v4", "revision": 1, "owner": "x",
                                                     "checkpoint": {"objective": "foreign"}, "admissions": {},
                                                     "interventions": {}, "source_rejections": {}, "constraints": []})
        result = self.listed()
        self.assertEqual(result["status"], "listed")
        self.assertTrue(result["complete"]); self.assertFalse(result["truncated"])
        rows = {row["objective"]: row for row in result["objectives"]}
        self.assertEqual(set(rows), {"objective", "closed-one"})
        self.assertEqual((rows["objective"]["phase"], rows["closed-one"]["phase"]), ("open", "closed"))
        self.assertEqual(rows["objective"]["runs"], ["run"])
        self.assertEqual(rows["objective"]["schema"], "current")
        self.assertEqual(rows["objective"]["record"], objective_root(self.project, "objective").name)
        self.assertFalse(rows["objective"]["locations_complete"])  # No worktree binding was recorded.
        self.assertEqual(result["scope"]["state_root"], str(state_root(self.project)))
        foreign = listing.objective_list(other)
        self.assertEqual([row["objective"] for row in foreign["objectives"]], ["foreign"])

    def test_unreadable_unattributed_and_superseded_records_are_reported_not_hidden(self):
        self.intake()
        root = state_root(self.project)
        self.raw(root / "broken", "truncated{")
        (root / "journal-only").mkdir(); (root / "journal-only" / "governor.json").write_text("{}")
        (root / "lock-only").mkdir(); (root / "lock-only" / ".lock").write_text("")
        self.raw(objective_root(self.project, "earlier"), {
            "schema": "pod-context/v4", "revision": 1, "owner": "former", "admissions": {},
            "checkpoint": {"schema": "pod-checkpoint/v2", "objective": "earlier",
                           "native_refs": [{"runId": "run05", "runtime": "rt"}]},
            "interventions": {}, "source_rejections": {}, "constraints": []})
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        result = self.listed()
        self.assertFalse(result["complete"])
        self.assertEqual([row["record"] for row in result["unreadable_records"]], ["broken"])
        self.assertEqual(result["unattributed_records"], ["journal-only"])
        rows = {row["objective"]: row for row in result["objectives"]}
        self.assertEqual((rows["earlier"]["schema"], rows["earlier"]["phase"], rows["earlier"]["runs"]),
                         ("superseded", "open", ["run05"]))
        self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_unenumerable_root_and_truncation_are_explicit(self):
        self.intake()
        self.write([self.criterion()], objective="second", governance={"base_ref": "target"})
        with patch.object(listing, "MAX_ROWS", 1):
            result = self.listed()
        self.assertEqual((result["truncated"], result["total"], len(result["objectives"]), result["complete"]),
                         (True, 2, 1, False))
        root = state_root(self.project)
        moved = root.with_name("moved")
        root.rename(moved); root.symlink_to(moved)
        result = self.listed()
        self.assertEqual((result["status"], result["complete"]), ("unreadable", False))

    def test_worktree_scope_matches_recorded_locations_and_holds_unplaced_work(self):
        from pod.github import repository_context
        context = repository_context(self.project)
        binding = {"repository": None, "repo_key": context["repo_key"], "path": context["worktree"],
                   "branch": "main"}
        self.intake(worktree=binding)
        self.write([self.criterion()], objective="unplaced", governance={"base_ref": "target"})
        sibling = self.root / "sibling"
        git(self.project, "worktree", "add", "-q", str(sibling), "-b", "sibling")
        self.write([self.criterion()], objective="elsewhere", governance={"base_ref": "target"},
                   worktree={**binding, "path": str(sibling.resolve()), "branch": "sibling"})
        rows = {row["objective"]: row["match"] for row in self.listed(worktree=str(self.project))["objectives"]}
        self.assertEqual(rows, {"objective": "worktree", "unplaced": "repository"})
        rows = {row["objective"]: row["match"] for row in self.listed(worktree=str(sibling))["objectives"]}
        self.assertEqual(rows, {"elsewhere": "worktree", "unplaced": "repository"})

    def test_cli_lists_without_orca_and_refuses_mixed_selectors(self):
        self.intake()
        code, out, _ = self.cli("--list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["objective"] for row in json.loads(out)["objectives"]], ["objective"])
        code, out, _ = self.cli("--list", "--repository", str(self.project))
        self.assertEqual(code, 0)
        self.assertIn("objective | open | current | runs run", out)
        for args in (("--list", "--objective", "objective"), ("--repository", str(self.project)),
                     ("--list", "--repository", ".", "--worktree", ".")):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.cli(*args)


if __name__ == "__main__":
    unittest.main()

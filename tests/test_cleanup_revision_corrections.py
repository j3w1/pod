"""Ordinary branch-bundle placement guards, independent of file recovery."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pod import cleanup
from pod.errors import PodError
from tests import test_cleanup_plan as plans
from tests.kernel_support import git


class SelectedPlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from types import SimpleNamespace
        cls.case = plans.CleanupPlanTests()
        cls.case.setUp()
        cls.addClassCleanup(cls.case.doCleanups)
        case = cls.case
        artifacts = case.root / "artifacts"
        artifacts.mkdir()
        target = case.root / "known-sibling"
        git(case.project, "worktree", "add", "-qb", "target-sibling", str(target))
        cls.fixture = fixture = SimpleNamespace(project=case.project, root=case.root, live=case.child,
                                                target=target, artifacts=artifacts, tip=case.base)
        cls.selected_path = fixture.root / "outside-selected"
        git(fixture.project, "worktree", "add", "--quiet", "-b", cls.selected_path.name, str(cls.selected_path))
        inside = fixture.target / "ignored/artifacts"
        inside.mkdir(mode=0o700, parents=True)
        cls.rows, cls.ordinary_rows = {}, {}
        for path, rows in ((cls.selected_path, cls.rows), (fixture.live, cls.ordinary_rows)):
            ref = "refs/heads/" + git(path, "symbolic-ref", "--short", "HEAD")
            outside = fixture.artifacts / (path.name + ".bundle")
            git(path, "bundle", "create", str(outside), ref)
            outside.chmod(0o600)
            nested = inside / outside.name
            nested.write_bytes(outside.read_bytes())
            nested.chmod(0o600)
            for label, bundle in (("inside", nested), ("outside", outside)):
                rows[label] = {"path": str(bundle), "ref": ref, "tip": fixture.tip}
        cls.state = {"checkpoint": {"worktree": {"path": str(fixture.live), "branch": "objective"},
                                    "governance": {"base_ref": "refs/heads/main"}},
                     "admissions": {"known": {"admission_id": "known", "recovery": {
                         "placement_binding": {"path": str(fixture.target), "branch": "target-sibling"}}}}}

    def setUp(self):
        pass

    def entry(self, *, kind="worktree", name=None, objective=None, archives_input=None, **options):
        from pod.internal import run
        request = {"project": str(self.fixture.project), **options}
        if objective is not None:
            request["objective"] = objective
        if kind is not None:
            request["observe"] = {"kind": kind, "name": name or str(self.selected_path)}
        if archives_input is not None:
            request["archives"] = archives_input
        original = cleanup.plan
        def observe(project, objective=None, **fields):
            return original(project, objective, orca_reader=self.case.orca_read, native_port=self.case.port, **fields)
        with patch.object(cleanup, "plan", side_effect=observe):
            return run("cleanup-plan", request)

    def observed(self, *, label="inside", include_target=True, ordinary=False, **options):
        state = deepcopy(self.state)
        if not include_target:
            state["admissions"] = {}
        original = cleanup._objective_plan
        def objective(*args, **kwargs):
            result = original(*args, **kwargs)
            self.ordinary_ids = {row["id"] for row in result["resources"]}
            return result
        with patch.object(cleanup, "read", return_value=state), \
                patch.object(cleanup, "_read_journal", return_value={"actions": [], "units": {}}), \
                patch("pod.evidence_closure.closed_older_state", return_value=None), \
                patch.object(cleanup, "_objective_plan", side_effect=objective):
            rows = self.ordinary_rows if ordinary else self.rows
            return self.entry(kind=None if ordinary else "worktree", name=str(self.selected_path),
                              objective="fixture-objective", archives_input=[rows[label]], **options)

    def selected(self, **options):
        return self.observed(**options)

    def selected_row(self, result):
        self.assertEqual(len(result["resources"]), 1)
        row = result["resources"][0]
        self.assertEqual(row["id"], "worktree:" + str(self.selected_path))
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["archives"][0]["mode"], "branch_only")
        return row

    def test_ordinary_selected_outside_ordinary_list_holds_bundle_inside_known_objective(self):
        row = self.selected_row(self.selected())
        self.assertNotIn(row["id"], self.ordinary_ids)
        self.assertIn("worktree:" + str(self.fixture.target), self.ordinary_ids)
        self.assertFalse(row["archive_verified"])
        self.assertIn("Archive is inside a removable resource or administrative directory", row["archives"][0]["holds"])

    def test_ordinary_selected_outside_known_scope_keeps_original_narrow_coverage_and_shape(self):
        row = self.selected_row(self.selected(label="outside"))
        self.assertTrue(row["archive_verified"], row)
        self.assertTrue(row["archives"][0]["verified"])
        self.assertTrue(row["archives"][0]["covers_resource"])
        self.assertEqual(set(row), {"id", "kind", "class", "reasons", "identity", "delete_argv", "archive_argv",
                                   "archive_verified", "archives", "stash_observation", "fingerprint"})

    def test_ordinary_selected_known_scope_does_not_infer_unlisted_worktree_boundaries(self):
        before = self.selected(include_target=False)
        row = self.selected_row(before)
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertEqual(row["identity"]["observation_scope"]["mode"], "objective")

    def test_new_known_objective_boundary_invalidates_same_selected_ordinary_expect(self):
        before = self.selected(include_target=False)
        with self.assertRaises(PodError) as changed:
            self.selected(expect=before["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_ordinary_bundle_inside_known_sibling_holds(self):
        result = self.observed(ordinary=True)
        row = next(row for row in result["resources"] if row["id"] == "worktree:" + str(self.fixture.live))
        self.assertFalse(row["archive_verified"])
        self.assertIn("Archive is inside a removable resource or administrative directory", row["archives"][0]["holds"])

    def test_ordinary_bundle_outside_known_scope_keeps_positive_git_evidence(self):
        result = self.observed(ordinary=True, label="outside")
        row = next(row for row in result["resources"] if row["id"] == "worktree:" + str(self.fixture.live))
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["archives"][0]["mode"], "branch_only")

    def test_project_only_ordinary_placement_keeps_only_its_observed_boundary(self):
        row = self.selected_row(self.entry(name=str(self.selected_path), archives_input=[self.rows["inside"]]))
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["identity"]["observation_scope"]["mode"], "project_only")
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")

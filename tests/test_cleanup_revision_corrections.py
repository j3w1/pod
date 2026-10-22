"""Recovery correction boundaries, using disposable real Git and production plans."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

from pod import cleanup, cleanup_archives as archives, cleanup_files as files
from pod.errors import PodError
from tests.test_cleanup_files import FileFixture, git
from tests.test_cleanup_reversible import EntryFixture
from tests import test_cleanup_plan as plans


def rebuild_bundle(fixture):
    fixture.bundle.unlink()
    git(fixture.target, "bundle", "create", str(fixture.bundle), fixture.ref,
        "refs/bisect/saved", "refs/worktree/saved", "refs/rewritten/saved")
    fixture.bundle.chmod(0o600)
    fixture.recovery_refs = {name: oid for oid, name in (line.split() for line in
        git(fixture.project, "bundle", "list-heads", str(fixture.bundle)).splitlines())}


class NestedControlFixture(EntryFixture):
    pointer = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.fixture
        cls.marker = fixture.root / "filter-cwd"
        source = fixture.root / "nested-source"
        source.mkdir()
        git(source, "init", "-q", "--template=", "-b", "main")
        (source / "data").write_bytes(b"nested data\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "nested")
        oid = git(source, "rev-parse", "HEAD")
        git(fixture.target, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub")
        git(fixture.target, "commit", "-qm", "gitlink")
        sub = fixture.target / "sub"
        sub.mkdir()
        args = (["--separate-git-dir=" + str(fixture.root / "external.git")]
                if cls.pointer else [])
        git(sub, "init", "-q", "--template=", *args)
        git(sub, "fetch", "-q", str(source), "main")
        git(sub, "checkout", "-q", oid)
        git(sub, "config", "filter.podprobe.clean", "sh -c " + shlex.quote(
            "pwd >> " + shlex.quote(str(cls.marker)) + "; cat"))
        admin = Path(git(sub, "rev-parse", "--absolute-git-dir"))
        (admin / "info").mkdir(exist_ok=True)
        (admin / "info/attributes").write_text("* filter=podprobe\n")
        fixture.tip = git(fixture.target, "rev-parse", "HEAD")
        rebuild_bundle(fixture)
        fixture.capture()

    def assert_no_recovery_execution(self, record):
        self.assertEqual(record["status"], "held", record)
        self.assertIn("unsupported_admin_state", record["reasons"])
        self.assertFalse(any("/pod-files-" in line for line in self.marker.read_text().splitlines()))


class EmbeddedControlEntryTests(NestedControlFixture):
    def test_embedded_archived_config_never_executes_at_production_entry(self):
        self.marker.write_text("")
        row = self.entry()["resources"][0]
        self.assert_no_recovery_execution(row["reversible_eligibility"])
        self.assertIsNone(row["delete_argv"])


class PointerControlEntryTests(NestedControlFixture):
    pointer = True

    def test_archived_gitfile_never_loads_external_config_at_production_entry(self):
        self.marker.write_text("")
        row = self.entry()["resources"][0]
        self.assert_no_recovery_execution(row["reversible_eligibility"])
        self.assertIsNone(row["delete_argv"])


class GitlinkEntryTests(EntryFixture):
    def test_tip_gitlink_without_nested_controls_holds_before_recovery_workfiles(self):
        fixture = self.fixture
        git(fixture.target, "update-index", "--add", "--cacheinfo", f"160000,{fixture.tip},sub")
        git(fixture.target, "commit", "-qm", "gitlink")
        fixture.tip = git(fixture.target, "rev-parse", "HEAD")
        rebuild_bundle(fixture)
        fixture.capture()
        record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held", record)
        self.assertIn("unsupported_admin_state", record["reasons"])

    def test_index_only_gitlink_remains_held_with_a_regenerated_index(self):
        fixture = self.fixture
        git(fixture.target, "update-index", "--add", "--cacheinfo", f"160000,{fixture.tip},staged-sub")
        self.addCleanup(git, fixture.target, "reset", "--quiet", "HEAD", "staged-sub")
        fixture.capture()
        record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held", record)
        self.assertFalse(record["file_recovery"]["restore_verified"])
        self.assertIn("file_restore_failed", record["reasons"])


class CurrentComparisonEntryTests(EntryFixture):
    def test_deep_live_tree_keeps_verified_artifacts_with_unavailable_current_data(self):
        fixture = self.fixture
        row = fixture.row()
        directories = []
        def remove_deep_tree():
            # Iterative scratch-only cleanup stays safe beyond Python's recursion limit.
            for path in reversed(directories):
                path.rmdir()
        self.addCleanup(remove_deep_tree)
        path = fixture.target / "ignored/deep"
        for _ in range(1001):
            path.mkdir()
            directories.append(path)
            path /= "d"
        seen, original = [], files._current
        def current(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except RecursionError:
                seen.append("RecursionError")
                raise
        with patch.object(files, "_current", side_effect=current):
            file, admin, basis = files.verify(fixture.project, row, [fixture.target, fixture.common])
        self.assertEqual(seen, ["RecursionError"])
        self.assertEqual(file["status"], "verified", file)
        self.assertTrue(file["restore_verified"])
        self.assertEqual(admin, {"status": "verified", "reasons": []})
        self.assertIsNone(file["current_match"])
        self.assertIn("file_current_unavailable", file["reasons"])
        self.assertEqual(basis["current"]["current"]["status"], "unavailable")
        self.assertEqual(fixture.row(), row)

    def test_restored_walk_recursion_still_invalidates_integrity(self):
        with patch.object(files, "_walk", side_effect=RecursionError("fixture restored walk unavailable")):
            file, admin, _ = self.fixture.verify()
        self.assertEqual(file["status"], "held")
        self.assertFalse(file["restore_verified"])
        self.assertEqual(admin["status"], "held")
        self.assertIn("file_archive_unavailable", file["reasons"])

    def test_post_current_retention_recursion_keeps_integrity_and_holds_production_data(self):
        original, calls = files._retained, 0
        def retained(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RecursionError("fixture current recheck unavailable")
            return original(*args, **kwargs)
        with patch.object(files, "_retained", side_effect=retained):
            row = self.entry()["resources"][0]
        record = row["reversible_eligibility"]
        self.assertTrue(record["archive_verified"], record)
        self.assertTrue(record["file_recovery"]["restore_verified"])
        self.assertEqual(record["admin_recovery"], {"status": "verified", "reasons": []})
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertEqual(record["status"], "held")
        self.assertIn("file_current_unavailable", record["reasons"])
        self.assertIsNone(row["delete_argv"])

    def test_unreadable_current_file_keeps_verified_artifacts_and_holds_data(self):
        fixture = self.fixture
        row = fixture.row()
        path = fixture.target / "ignored/private/b"
        mode = path.stat().st_mode & 0o777
        path.chmod(0)
        self.addCleanup(path.chmod, mode)
        record = self.entry(archives_input=[row])["resources"][0]["reversible_eligibility"]
        self.assertTrue(record["archive_verified"], record)
        self.assertTrue(record["file_recovery"]["restore_verified"])
        self.assertEqual(record["admin_recovery"]["status"], "verified")
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertEqual(record["status"], "held")
        self.assertIn("file_current_unavailable", record["reasons"])
        self.assertEqual(fixture.row(), row)

    def test_post_current_retention_read_failure_keeps_integrity(self):
        original, calls = files._retained, 0
        def retained(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture current recheck unavailable")
            return original(*args, **kwargs)
        with patch.object(files, "_retained", side_effect=retained):
            record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertTrue(record["archive_verified"], record)
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertEqual(record["status"], "held")
        self.assertIn("file_current_unavailable", record["reasons"])

    def test_artifact_tamper_during_current_uncertainty_still_invalidates_integrity(self):
        path = self.fixture.archive
        original = files._current
        def changed(*args, **kwargs):
            original(*args, **kwargs)
            with path.open("r+b") as stream:
                stream.write(b"tamper")
            raise OSError("fixture current read unavailable")
        with patch.object(files, "_current", side_effect=changed):
            record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertFalse(record["file_recovery"]["restore_verified"])
        self.assertIn("file_archive_changed", record["reasons"])


class SiblingPlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = FileFixture()
        cls.addClassCleanup(cls.fixture.close)
        fixture = cls.fixture
        destination = fixture.live / "ignored/artifacts"
        destination.mkdir(mode=0o700, parents=True)
        fixture.bundle.rename(destination / fixture.bundle.name)
        fixture.bundle = destination / fixture.bundle.name
        cls.paths = {"git_inside": fixture.target, "carrier": fixture.live,
                     "file_inside": fixture.root / "file-target", "outside": fixture.root / "outside-target"}
        for label in ("file_inside", "outside"):
            path = cls.paths[label]
            git(fixture.project, "worktree", "add", "--quiet", "-b", path.name, str(path))
        cls.rows = {"git_inside": fixture.row()}
        attributes = {key: getattr(fixture, key) for key in
                      ("target", "admin", "ref", "bundle", "archive", "recovery_refs")}
        try:
            # Capture the carrier last, after both independently misplaced artifacts exist.
            for label in ("file_inside", "outside", "carrier"):
                fixture.target = cls.paths[label]
                fixture.ref = "refs/heads/" + fixture.target.name
                fixture.admin = Path(git(fixture.target, "rev-parse", "--absolute-git-dir"))
                fixture.bundle = fixture.artifacts / (label + ".bundle")
                fixture.archive = (destination if label == "file_inside" else fixture.artifacts) / (label + ".tar")
                git(fixture.target, "bundle", "create", str(fixture.bundle), fixture.ref)
                fixture.bundle.chmod(0o600)
                fixture.recovery_refs = {name: oid for oid, name in (line.split() for line in
                    git(fixture.project, "bundle", "list-heads", str(fixture.bundle)).splitlines())}
                fixture.capture()
                cls.rows[label] = fixture.row()
        finally:
            for key, value in attributes.items():
                setattr(fixture, key, value)
        cls.state = {"checkpoint": {"worktree": {"path": str(fixture.target), "branch": "target"},
                                    "governance": {"base_ref": "refs/heads/main"}},
                     "admissions": {label: {"admission_id": label, "recovery": {
                         "placement_binding": {"path": str(path), "branch": path.name}}}
                         for label, path in cls.paths.items() if path != fixture.target}}
        # One real ordinary plan serves all four methods: isolated Git/file negatives
        # and two independently archived worktrees with both artifacts outside scope.
        # The real production plan's setup duration is recorded separately by the runner.
        cls.placements = []
        original = archives._outside
        artifact_paths = {Path(row["path"]) for row in cls.rows.values()}
        artifact_paths.update(Path(row["worktree_archive"]["path"]) for row in cls.rows.values())
        def placement(path, excluded):
            if path in artifact_paths:
                cls.placements.append((path, set(excluded)))
            return original(path, excluded)
        with patch.object(archives, "_outside", side_effect=placement):
            cls.result = cls().observed()

    def observed(self, **options):
        def reader(argv, **kwargs):
            if argv[:2] == ["worktree", "show"]:
                path = argv[argv.index("--worktree") + 1].removeprefix("path:")
                return {"result": {"worktree": {"path": path, "isMainWorktree": False}}}
            if argv[:2] == ["terminal", "list"]:
                return {"result": {"terminals": [], "truncated": False}}
            raise AssertionError(argv)
        with patch.object(cleanup, "read", return_value=self.state), \
                patch.object(cleanup, "_read_journal", return_value={"actions": [], "units": {}}), \
                patch("pod.evidence_closure.closed_older_state", return_value=None):
            return cleanup.plan(self.fixture.project, "fixture-objective",
                archives=list(self.rows.values()), orca_reader=reader, native_port=object(), **options)

    def worktree(self, result, path):
        return next(row for row in result["resources"] if row["id"] == "worktree:" + str(path))


    def test_ordinary_git_artifact_inside_other_removable_worktree_holds(self):
        result = self.result
        row = self.worktree(result, self.paths["git_inside"])
        record = row["reversible_eligibility"]
        self.assertFalse(row["archive_verified"])
        self.assertFalse(record["git_recovery"]["verified_covering"])
        self.assertEqual(record["file_recovery"]["status"], "verified")
        self.assertEqual(record["status"], "held")
        self.assertEqual(self.worktree(result, self.fixture.live)["reversible_eligibility"]["status"], "data_eligible")
        self.assertIsNone(row["delete_argv"])


    def test_ordinary_file_artifact_inside_other_removable_worktree_holds(self):
        result = self.result
        row = self.worktree(result, self.paths["file_inside"])
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertTrue(record["git_recovery"]["verified_covering"])
        self.assertNotEqual(record["file_recovery"]["status"], "verified")
        self.assertEqual(self.worktree(result, self.fixture.live)["reversible_eligibility"]["status"], "data_eligible")
        self.assertIsNone(row["delete_argv"])


    def test_two_resources_with_both_artifacts_outside_full_scope_keep_positive_data(self):
        result = self.result
        for path in (self.paths["outside"], self.paths["carrier"]):
            row = self.worktree(result, path)
            self.assertEqual(row["reversible_eligibility"]["status"], "data_eligible", row)
            self.assertIsNone(row["delete_argv"])

    def test_both_artifact_freshness_checks_keep_every_known_objective_boundary(self):
        fixture = self.fixture
        expected = {fixture.common, *self.paths.values(), *(Path(git(path, "rev-parse", "--absolute-git-dir"))
                    for path in self.paths.values())}
        for path, excluded in self.placements:
            self.assertLessEqual(expected, excluded, path)
        paths = {path for path, _ in self.placements}
        self.assertEqual(len(paths), 8)
        positive_paths = {Path(self.rows[label]["path"]) for label in ("outside", "carrier")}
        positive_paths.update(Path(self.rows[label]["worktree_archive"]["path"]) for label in ("outside", "carrier"))
        self.assertTrue(all(sum(current == path for current, _ in self.placements) >= 2 for path in positive_paths))


class LegacySelectedPlacementTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.fixture
        git(fixture.live, "stash", "clear")
        cls.selected_path = fixture.root / "outside-selected"
        git(fixture.project, "worktree", "add", "--quiet", "-b", cls.selected_path.name, str(cls.selected_path))
        inside = fixture.target / "ignored/artifacts"
        inside.mkdir(mode=0o700)
        cls.rows, cls.ordinary_rows = {}, {}
        for path, rows in ((cls.selected_path, cls.rows), (fixture.live, cls.ordinary_rows)):
            ref = "refs/heads/" + path.name
            outside = fixture.artifacts / (path.name + ".bundle")
            git(path, "bundle", "create", str(outside), ref)
            outside.chmod(0o600)
            nested = inside / outside.name
            nested.write_bytes(outside.read_bytes())
            nested.chmod(0o600)
            for label, bundle in (("inside", nested), ("outside", outside)):
                rows[label] = {"path": str(bundle), "ref": ref, "tip": fixture.tip}
        cls.state = {"checkpoint": {"worktree": {"path": str(fixture.live), "branch": "live"},
                                    "governance": {"base_ref": "refs/heads/main"}},
                     "admissions": {"known": {"admission_id": "known", "recovery": {
                         "placement_binding": {"path": str(fixture.target), "branch": "target"}}}}}
        # This one accepted current-scope baseline is shared by the coverage and
        # changed-scope fingerprint methods. No unrelated worktree is inferred.
        cls.before = cls().selected(include_target=False)

    def setUp(self):
        pass

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
        self.assertNotIn("reversible_eligibility", row)
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["archives"][0]["mode"], "branch_only")
        return row

    def test_legacy_selected_outside_ordinary_list_holds_bundle_inside_known_objective(self):
        row = self.selected_row(self.selected())
        self.assertNotIn(row["id"], self.ordinary_ids)
        self.assertIn("worktree:" + str(self.fixture.target), self.ordinary_ids)
        self.assertFalse(row["archive_verified"])
        self.assertIn("Archive is inside a removable resource or administrative directory", row["archives"][0]["holds"])

    def test_legacy_selected_outside_known_scope_keeps_original_narrow_coverage_and_shape(self):
        row = self.selected_row(self.selected(label="outside"))
        self.assertTrue(row["archive_verified"], row)
        self.assertTrue(row["archives"][0]["verified"])
        self.assertTrue(row["archives"][0]["covers_resource"])
        self.assertEqual(set(row), {"id", "kind", "class", "reasons", "identity", "delete_argv", "archive_argv",
                                   "archive_verified", "archives", "stash_observation", "fingerprint"})

    def test_legacy_selected_known_scope_does_not_infer_unlisted_worktree_boundaries(self):
        row = self.selected_row(self.before)
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertEqual(row["identity"]["observation_scope"]["mode"], "objective")

    def test_new_known_objective_boundary_invalidates_same_selected_legacy_expect(self):
        with self.assertRaises(PodError) as changed:
            self.selected(expect=self.before["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_ordinary_legacy_bundle_inside_known_sibling_holds(self):
        result = self.observed(ordinary=True)
        row = next(row for row in result["resources"] if row["id"] == "worktree:" + str(self.fixture.live))
        self.assertNotIn("reversible_eligibility", row)
        self.assertFalse(row["archive_verified"])
        self.assertIn("Archive is inside a removable resource or administrative directory", row["archives"][0]["holds"])

    def test_ordinary_legacy_bundle_outside_known_scope_keeps_positive_git_evidence(self):
        result = self.observed(ordinary=True, label="outside")
        row = next(row for row in result["resources"] if row["id"] == "worktree:" + str(self.fixture.live))
        self.assertNotIn("reversible_eligibility", row)
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["archives"][0]["mode"], "branch_only")

    def test_project_only_legacy_placement_keeps_only_its_observed_boundary(self):
        row = self.selected_row(self.entry(name=str(self.selected_path), archives_input=[self.rows["inside"]]))
        self.assertTrue(row["archive_verified"], row)
        self.assertEqual(row["identity"]["observation_scope"]["mode"], "project_only")
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")


class RetainedStashLogEntryTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.fixture
        cls.older = git(fixture.live, "rev-parse", "refs/stash")
        (fixture.live / "README").write_text("next stash\n")
        git(fixture.live, "stash", "push", "--quiet", "-m", "next stash")
        git(fixture.target, "checkout", "--quiet", "--detach", cls.older)
        git(fixture.target, "checkout", "--quiet", "target")
        fixture.capture()
        cls.stash_before = fixture.stash_bytes()

    def test_known_phase_excludes_log_only_retained_root_while_legacy_keeps_it(self):
        fixture = self.fixture
        repository = archives.repository_observation(fixture.project)
        expanded = archives.resource_observation(fixture.target, fixture.ref, fixture.tip, repository)
        known = archives.resource_observation(fixture.target, fixture.ref, fixture.tip, repository, known_phases=True)
        self.assertTrue(expanded["complete"] and known["complete"])
        self.assertIn(self.older, repository["stash_roots"])
        self.assertIn(self.older, expanded["required_objects"])
        self.assertNotIn(self.older, known["required_objects"])

    def test_log_only_retention_produces_positive_data_without_copying_live_stash(self):
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "data_eligible", row)
        self.assertFalse(row["archive_verified"])
        self.assertEqual(self.stash_before, self.fixture.stash_bytes())
        self.assertIsNone(row["delete_argv"])

    def test_destroyed_private_name_still_requires_bundle_copy_of_retained_object(self):
        fixture = self.fixture
        name = "refs/worktree/retained-stash"
        git(fixture.target, "update-ref", name, self.older)
        self.addCleanup(git, fixture.target, "update-ref", "-d", name)
        fixture.capture()
        record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])


class GitMountIncarnationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = plans.CleanupPlanTests()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        cls.bundle = cls.case.root / "mount.bundle"
        git(cls.case.project, "bundle", "create", str(cls.bundle), "refs/heads/objective")
        cls.rows = [{"path": str(cls.bundle), "ref": "refs/heads/objective", "tip": cls.case.base}]
        with cls().mounts(501):
            cls.first = cls.case.observed(archives=cls.rows)

    def mounts(self, identifier):
        original = Path.open
        def opened(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                return io.BytesIO(f"{identifier} 1 0:123 / / rw - tmpfs tmpfs rw\n".encode())
            return original(path, *args, **kwargs)
        return patch.object(Path, "open", opened)

    def test_mount_id_only_change_refuses_old_legacy_expect(self):
        first = self.first
        self.assertTrue(self.case.resource(first, "worktree")["archive_verified"])
        with self.mounts(502), self.assertRaises(PodError) as changed:
            self.case.observed(archives=self.rows, expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_opened_git_artifact_and_all_ancestors_bind_mount_incarnation(self):
        with archives._opened(self.bundle) as (_, value):
            self.assertIsInstance(value["file"]["mount_id"], int)
            self.assertTrue(all(isinstance(row["mount_id"], int) for row in value["components"]))

    def test_unavailable_mount_incarnation_refuses_verified_git_artifact(self):
        with patch.object(archives, "_mount_id", create=True, side_effect=archives.Unavailable("fixture mount unavailable")):
            result = self.case.observed(archives=self.rows)
        row = self.case.resource(result, "worktree")
        self.assertFalse(row["archive_verified"], row["archives"])


class SelectedRefLogFixture(EntryFixture):
    object_format = "sha1"
    log_repetitions = None
    absent_log = False

    @classmethod
    def setUpClass(cls):
        cls.fixture = FileFixture(object_format=cls.object_format)
        cls.addClassCleanup(cls.fixture.close)
        fixture = cls.fixture
        tree = git(fixture.project, "rev-parse", fixture.tip + "^{tree}")
        cls.unique = git(fixture.project, "commit-tree", tree, "-p", fixture.tip, "-m", "log only")
        git(fixture.project, "update-ref", "-m", "forward", fixture.ref, cls.unique, fixture.tip)
        git(fixture.project, "update-ref", "-m", "back", fixture.ref, fixture.tip, cls.unique)
        cls.log = fixture.common / "logs" / fixture.ref
        if cls.log_repetitions:
            cls.log.write_bytes(cls.log.read_bytes().splitlines(keepends=True)[-1] * cls.log_repetitions)
        if cls.absent_log:
            cls.log.unlink()
        cls.log_bytes = cls.log.read_bytes() if cls.log.exists() else None
        cls.rows = {"original": fixture.row()}
        # This is the disposable producer, not Pod: it creates ordinary recovery
        # aliases/objects before normal worktree removal and freezes the artifacts.
        aliases = []
        if cls.log_bytes is not None:
            cls.blob = git(fixture.project, "hash-object", "-w", "--stdin", data=cls.log_bytes)
            cls.blob_alias = "refs/recovery/arbitrary-data-name"
            git(fixture.project, "update-ref", cls.blob_alias, cls.blob)
            aliases.append(cls.blob_alias)
        git(fixture.project, "update-ref", "refs/recovery/log-root", cls.unique)
        choices = (("root_only", ["refs/recovery/log-root"]), ("log_only", aliases),
                   ("full", [*aliases, "refs/recovery/log-root"]))
        for label, extra in choices:
            bundle = fixture.artifacts / (label + ".bundle")
            git(fixture.target, "bundle", "create", str(bundle), fixture.ref,
                "refs/bisect/saved", "refs/worktree/saved", "refs/rewritten/saved", *extra)
            bundle.chmod(0o600)
            row = fixture.row()
            row.update(path=str(bundle), sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
                       recovery_refs={name: oid for oid, name in (line.split() for line in
                           git(fixture.project, "bundle", "list-heads", str(bundle)).splitlines())})
            cls.rows[label] = row
        # Recovery aliases are in the producer's private staging basis, not
        # permanent surviving source refs that can substitute for the artifact.
        for name in [*aliases, "refs/recovery/log-root"]:
            git(fixture.project, "update-ref", "-d", name)
        fixture.capture()
        for row in cls.rows.values():
            row["worktree_archive"] = fixture.row()["worktree_archive"]
        git(fixture.project, "worktree", "remove", str(fixture.target))

    def setUp(self):
        pass

    def remaining(self, label="full", **options):
        return self.entry(kind="local_branch", name=self.fixture.ref,
                          archives_input=[self.rows[label]], **options)

    def assert_file_admin_held(self, file, admin):
        self.assertEqual(file["status"], "held", file)
        self.assertFalse(file["restore_verified"])
        self.assertIsNone(file["current_match"])
        self.assertEqual(admin["status"], "held", admin)
        self.assertIn("admin_recovery_missing", admin["reasons"])

    def assert_incomplete_reference_held(self, record):
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertFalse(record["git_recovery"]["verified_covering"])
        self.assert_file_admin_held(record["file_recovery"], record["admin_recovery"])

    def reference_facts(self):
        fixture = self.fixture
        repository = archives.repository_observation(fixture.project)
        return archives.reference_observation(fixture.project, fixture.ref, fixture.tip, repository, known_phases=True)

    def verify_files(self, facts):
        return files.verify(self.fixture.project, self.rows["full"], [], resource=facts)


class SelectedRefLogEntryTests(SelectedRefLogFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = cls().remaining()

    def test_changed_selected_log_bytes_refuse_old_expect_with_the_same_roots(self):
        raw = self.log.read_bytes()
        self.addCleanup(self.log.write_bytes, raw)
        self.log.write_bytes(raw + raw.splitlines(keepends=True)[-1])
        with self.assertRaises(PodError) as changed:
            self.remaining(expect=self.first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_complete_blob_and_log_only_roots_keep_remaining_ref_data_positive(self):
        before = self.log.read_bytes(), git(self.fixture.project, "show-ref")
        actual, original = [], archives._run
        def inspect(root, argv, **kwargs):
            result = original(root, argv, **kwargs)
            if argv == ["cat-file", "blob", self.blob] and kwargs.get("isolated"):
                actual.append(result == self.log_bytes)
            return result
        with patch.object(archives, "_run", side_effect=inspect):
            row = self.remaining()["resources"][0]
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)
        self.assertTrue(record["archive_verified"])
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertEqual(actual, [True, True])
        self.assertEqual(before, (self.log.read_bytes(), git(self.fixture.project, "show-ref")))
        self.assertIsNone(row["delete_argv"])

    def test_direct_file_verification_requires_complete_reference_material(self):
        facts = self.reference_facts()
        self.assertTrue(facts["complete"])
        facts["complete"] = False
        file, admin, _ = self.verify_files(facts)
        self.assert_file_admin_held(file, admin)

    def test_direct_file_verification_requires_an_actual_selected_log_fact(self):
        facts = self.reference_facts()
        self.assertTrue(facts["complete"])
        for value in ("missing", None, {}):
            current = deepcopy(facts)
            if value == "missing":
                current.pop("selected_ref_log")
            else:
                current["selected_ref_log"] = value
            with self.subTest(selected_log=value):
                file, admin, _ = self.verify_files(current)
                self.assert_file_admin_held(file, admin)

    def test_unknown_retained_closure_cannot_verify_file_or_admin_recovery(self):
        with patch.object(archives, "retained_material", side_effect=archives.Unavailable("fixture closure unavailable")):
            facts = self.reference_facts()
        self.assertFalse(facts["complete"])
        self.assertNotIn("selected_ref_log", facts)
        file, admin, _ = self.verify_files(facts)
        self.assert_file_admin_held(file, admin)

    def test_unknown_reference_closure_holds_nested_production_recovery_fields(self):
        original = archives.reference_observation
        def observed(*args, **kwargs):
            if kwargs.get("known_phases"):
                with patch.object(archives, "retained_material", side_effect=archives.Unavailable("fixture closure unavailable")):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)
        with patch.object(archives, "reference_observation", side_effect=observed):
            record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assert_incomplete_reference_held(record)

    def test_source_blob_and_root_coverage_without_restored_log_bytes_hold(self):
        self.assertEqual(git(self.fixture.project, "cat-file", "-t", self.blob), "blob")
        record = self.remaining("root_only")["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])

    def test_raw_log_blob_without_otherwise_lost_old_new_root_holds(self):
        record = self.remaining("log_only")["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])

    def test_actual_recovered_byte_mismatch_holds_despite_advertisement_and_type(self):
        original = archives._run
        def mismatch(root, argv, **kwargs):
            result = original(root, argv, **kwargs)
            if argv == ["cat-file", "blob", self.blob] and kwargs.get("isolated"):
                return b"x" + result[1:]
            return result
        with patch.object(archives, "_run", side_effect=mismatch):
            record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])

    def test_unavailable_selected_log_is_not_positive_absence(self):
        raw = self.log.read_bytes()
        self.log.unlink()
        self.log.symlink_to(self.fixture.common / "logs/refs/stash")
        def restore():
            self.log.unlink()
            self.log.write_bytes(raw)
        self.addCleanup(restore)
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assert_incomplete_reference_held(record)

    def test_malformed_selected_log_never_becomes_empty_coverage(self):
        raw = self.log.read_bytes()
        self.addCleanup(self.log.write_bytes, raw)
        self.log.write_bytes(raw.rstrip(b"\n"))
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assert_incomplete_reference_held(record)
        facts = self.reference_facts()
        self.assertFalse(facts["complete"])
        file, admin, _ = self.verify_files(facts)
        self.assert_file_admin_held(file, admin)


class SelectedRefSHA256LogTests(SelectedRefLogFixture):
    object_format = "sha256"

    def test_actual_sha256_object_format_derives_and_verifies_log_blob(self):
        self.assertEqual(len(self.blob), 64)
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)


class SelectedRefAbsentLogTests(SelectedRefLogFixture):
    absent_log = True

    def test_positive_selected_log_absence_needs_no_manufactured_empty_blob(self):
        record = self.remaining("original")["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)
        self.assertTrue(record["archive_verified"])


class SelectedRefLogBoundTests(SelectedRefLogFixture):
    log_repetitions = 2048

    def test_exact_selected_2048_log_domain_passes_with_actual_bytes_and_roots(self):
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)

    def test_selected_2049_entries_hold_without_raising_resource_log_domain(self):
        raw = self.log.read_bytes()
        self.addCleanup(self.log.write_bytes, raw)
        self.log.write_bytes(raw + raw.splitlines(keepends=True)[-1])
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assert_incomplete_reference_held(record)
        facts = self.reference_facts()
        self.assertFalse(facts["complete"])
        file, admin, _ = self.verify_files(facts)
        self.assert_file_admin_held(file, admin)


if __name__ == "__main__":
    unittest.main()

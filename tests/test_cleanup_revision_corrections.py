"""Recovery correction boundaries, using disposable real Git and production plans."""

from __future__ import annotations

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


class SiblingScopeFixture(unittest.TestCase):
    inside = None

    @classmethod
    def setUpClass(cls):
        cls.fixture = FileFixture()
        cls.addClassCleanup(cls.fixture.close)
        fixture = cls.fixture
        if cls.inside:
            destination = fixture.live / "ignored/artifacts"
            destination.mkdir(mode=0o700, parents=True)
            if cls.inside == "git":
                fixture.bundle.rename(destination / fixture.bundle.name)
                fixture.bundle = destination / fixture.bundle.name
            else:
                fixture.archive.rename(destination / fixture.archive.name)
                fixture.archive = destination / fixture.archive.name
        fixture.capture()
        cls.first_row = fixture.row()
        attributes = {key: getattr(fixture, key) for key in
                      ("target", "admin", "ref", "bundle", "archive", "recovery_refs")}
        try:
            fixture.target, fixture.ref = fixture.live, "refs/heads/live"
            fixture.admin = Path(git(fixture.live, "rev-parse", "--absolute-git-dir"))
            fixture.bundle, fixture.archive = fixture.artifacts / "live.bundle", fixture.artifacts / "live.tar"
            git(fixture.live, "bundle", "create", str(fixture.bundle), fixture.ref)
            fixture.bundle.chmod(0o600)
            fixture.recovery_refs = {name: oid for oid, name in (line.split() for line in
                git(fixture.project, "bundle", "list-heads", str(fixture.bundle)).splitlines())}
            fixture.capture()
            cls.second_row = fixture.row()
        finally:
            for key, value in attributes.items():
                setattr(fixture, key, value)
        cls.state = {"checkpoint": {"worktree": {"path": str(fixture.target), "branch": "target"},
                                    "governance": {"base_ref": "refs/heads/main"}},
                     "admissions": {"sibling": {"admission_id": "sibling", "recovery": {
                         "placement_binding": {"path": str(fixture.live), "branch": "live"}}}}}
        # One complete immutable two-resource observation is shared by assertions.
        # The real production plan's setup duration is recorded separately by the runner.
        cls.placements = []
        original = archives._outside
        artifact_paths = {Path(row["path"]) for row in (cls.first_row, cls.second_row)}
        artifact_paths.update(Path(row["worktree_archive"]["path"]) for row in (cls.first_row, cls.second_row))
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
                archives=[self.first_row, self.second_row], orca_reader=reader, native_port=object(), **options)

    def worktree(self, result, path):
        return next(row for row in result["resources"] if row["id"] == "worktree:" + str(path))


class SiblingGitPlacementTests(SiblingScopeFixture):
    inside = "git"

    def test_ordinary_git_artifact_inside_other_removable_worktree_holds(self):
        result = self.result
        row = self.worktree(result, self.fixture.target)
        record = row["reversible_eligibility"]
        self.assertFalse(row["archive_verified"])
        self.assertFalse(record["git_recovery"]["verified_covering"])
        self.assertEqual(record["status"], "held")
        self.assertEqual(self.worktree(result, self.fixture.live)["reversible_eligibility"]["status"], "data_eligible")
        self.assertIsNone(row["delete_argv"])


class SiblingFilePlacementTests(SiblingScopeFixture):
    inside = "file"

    def test_ordinary_file_artifact_inside_other_removable_worktree_holds(self):
        result = self.result
        row = self.worktree(result, self.fixture.target)
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertNotEqual(record["file_recovery"]["status"], "verified")
        self.assertEqual(self.worktree(result, self.fixture.live)["reversible_eligibility"]["status"], "data_eligible")
        self.assertIsNone(row["delete_argv"])


class OutsideSiblingPlacementTests(SiblingScopeFixture):
    def test_two_resources_with_both_artifacts_outside_full_scope_keep_positive_data(self):
        result = self.result
        for path in (self.fixture.target, self.fixture.live):
            row = self.worktree(result, path)
            self.assertEqual(row["reversible_eligibility"]["status"], "data_eligible", row)
            self.assertIsNone(row["delete_argv"])

    def test_both_artifact_freshness_checks_keep_every_known_objective_boundary(self):
        fixture = self.fixture
        expected = {fixture.target, fixture.live, fixture.common, fixture.admin,
                    Path(git(fixture.live, "rev-parse", "--absolute-git-dir"))}
        for path, excluded in self.placements:
            self.assertLessEqual(expected, excluded, path)
        paths = {path for path, _ in self.placements}
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(sum(current == path for current, _ in self.placements) >= 2 for path in paths))


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
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])

    def test_malformed_selected_log_never_becomes_empty_coverage(self):
        raw = self.log.read_bytes()
        self.addCleanup(self.log.write_bytes, raw)
        self.log.write_bytes(raw.rstrip(b"\n"))
        record = self.remaining()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])


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
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["git_recovery"]["verified_covering"])


if __name__ == "__main__":
    unittest.main()

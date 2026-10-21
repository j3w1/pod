"""Optional recovery data through cleanup-plan's actual request and output boundary."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod import cleanup, cleanup_archives as archives, cleanup_reversible as reversible, cleanup_stashes as stashes
from pod.errors import PodError
from pod.internal import run
from tests.test_cleanup_files import FileFixture, git


class EntryFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = FileFixture()
        cls.addClassCleanup(cls.fixture.close)

    def setUp(self):
        self.fixture.capture()

    def entry(self, *, kind="worktree", name=None, archives_input=None, **options):
        fixture = self.fixture
        def reader(argv, **kwargs):
            if argv[:2] == ["worktree", "show"]:
                path = argv[argv.index("--worktree") + 1].removeprefix("path:")
                branch = git(Path(path), "symbolic-ref", "--short", "HEAD")
                return {"runtime": "fixture-runtime", "result": {"worktree": {
                    "path": path, "id": "fixture-" + Path(path).name, "branch": branch,
                    "isBare": False, "isMainWorktree": Path(path) == fixture.project,
                    "git": {"path": path, "branch": branch, "isBare": False}}}}
            if argv[:2] == ["terminal", "list"]:
                return {"runtime": "fixture-runtime", "result": {"terminals": [], "truncated": False}}
            raise AssertionError(argv)
        # Only the native read port is substituted. Request parsing, material reads,
        # archive restoration, decisions, compaction and expect are production code.
        original = cleanup.plan
        def observe(project, objective=None, **kwargs):
            return original(project, objective, orca_reader=reader, **kwargs)
        request = {"project": str(fixture.project), "observe": {"kind": kind, "name": name or str(fixture.target)},
                   **options}
        if kind is None:
            request.pop("observe")
        if archives_input is not False:
            request["archives"] = [fixture.row()] if archives_input is None else archives_input
        with patch.object(cleanup, "plan", side_effect=observe):
            return run("cleanup-plan", request)


class ReversibleEntryTests(EntryFixture):
    def test_positive_new_data_with_null_argv_preserves_live_stash_and_legacy_unknown(self):
        fixture = self.fixture
        before = fixture.stash_bytes()
        with subprocess.Popen(["git", "-C", str(fixture.live), "cat-file", "--batch"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as reader:
            def query():
                reader.stdin.write(b"refs/stash\n")
                reader.stdin.flush()
                header = reader.stdout.readline()
                size = int(header.split()[2])
                return header, reader.stdout.read(size + 1)
            first_read = query()
            result = self.entry()
            self.assertEqual(query(), first_read)
            self.assertIsNone(reader.poll())
            reader.stdin.close()
            reader.wait(timeout=2)
        row = result["resources"][0]
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)
        self.assertTrue(record["archive_verified"])
        self.assertTrue(record["git_recovery"]["verified_covering"])
        self.assertTrue(record["file_recovery"]["current_match"])
        self.assertEqual(record["stash_noninterference"]["status"], "preserved_by_known_git_phases")
        self.assertEqual(record["phases"], ["git_worktree_remove"])
        self.assertEqual(record["executor_checks_required"], list(reversible.EXECUTOR_CHECKS))
        self.assertEqual(record["reasons"], ["executor_checks_required"])
        self.assertFalse(row["archive_verified"])  # Existing Git worktree proof still requires stash roots.
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual({relation["relation"] for relation in row["stash_observation"]["relations"]}, {"unknown"})
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertEqual(before, fixture.stash_bytes())
        public = json.dumps(result)
        self.assertNotIn(fixture.marker, public)
        self.assertNotIn("private/b", public)
        self.assertNotIn("source_stat", public)

    def test_no_optional_reference_preserves_exact_legacy_wire_and_git_only_meaning(self):
        fixture = self.fixture
        prior_plan = fixture.row()
        prior_plan.pop("worktree_archive")
        row = self.entry(archives_input=[prior_plan])["resources"][0]
        self.assertNotIn("reversible_eligibility", row)
        self.assertEqual(set(row), {"id", "kind", "class", "reasons", "identity", "delete_argv", "archive_argv",
                                   "archive_verified", "archives", "stash_observation", "fingerprint"})
        self.assertFalse(row["archive_verified"])
        self.assertTrue(stashes.holds(row["stash_observation"]))
        ref = self.entry(kind="local_branch", name=fixture.ref, archives_input=[prior_plan])["resources"][0]
        self.assertTrue(ref["archive_verified"], ref["archives"])
        self.assertIsNone(ref["delete_argv"])

    def test_optional_reference_shape_and_caller_phase_or_authority_are_refused(self):
        for bad in ({"path": "relative", "sha256": "0" * 64}, {"path": "/x/../y", "sha256": "0" * 64},
                    {"path": "/x", "sha256": "0" * 64, "authority": True}, {"path": "/x", "sha256": True}):
            row = self.fixture.row()
            row["worktree_archive"] = bad
            with self.subTest(bad=bad), self.assertRaises(PodError):
                self.entry(archives_input=[row])
        row = self.fixture.row()
        row.pop("sha256")
        row.pop("recovery_refs")
        with self.assertRaises(PodError):
            self.entry(archives_input=[row])
        for key in ("authority", "phase", "footprint", "verdict", "journal"):
            with self.subTest(key=key), self.assertRaises(PodError):
                self.entry(**{key: True})

    def test_same_ref_and_tip_sibling_archive_cannot_cover_the_selected_worktree(self):
        fixture = self.fixture
        sibling = fixture.root / "same-ref-sibling"
        git(fixture.project, "worktree", "add", "--quiet", "--force", str(sibling), "target")
        self.addCleanup(git, fixture.project, "worktree", "remove", str(sibling))
        target, admin = fixture.target, fixture.admin
        try:
            fixture.target, fixture.admin = sibling, Path(git(sibling, "rev-parse", "--absolute-git-dir"))
            fixture.capture()
        finally:
            fixture.target, fixture.admin = target, admin
        row = self.entry()["resources"][0]
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertIn("file_current_changed", record["file_recovery"]["reasons"])
        self.assertIsNone(row["delete_argv"])

    def test_foreign_same_history_repository_archive_cannot_cover_current_resource(self):
        fixture = self.fixture
        foreign, sibling = fixture.root / "foreign", fixture.root / "foreign-linked"
        git(fixture.root, "clone", "--quiet", "--no-hardlinks", str(fixture.project), str(foreign))
        git(foreign, "worktree", "add", "--quiet", "-b", "target", str(sibling), "main")
        target, admin, common = fixture.target, fixture.admin, fixture.common
        try:
            fixture.target, fixture.admin, fixture.common = sibling, Path(git(sibling, "rev-parse", "--absolute-git-dir")), foreign / ".git"
            fixture.capture()
        finally:
            fixture.target, fixture.admin, fixture.common = target, admin, common
        row = self.entry()["resources"][0]
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "held")
        self.assertFalse(record["archive_verified"])
        self.assertIn("file_current_changed", record["file_recovery"]["reasons"])
        self.assertIsNone(row["delete_argv"])

    def test_file_and_manifest_corruption_hold_with_null_commands(self):
        fixture = self.fixture
        fixture.capture(transform=lambda manifest: manifest.update(schema="unsupported/v2"))
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertFalse(row["reversible_eligibility"]["archive_verified"])
        self.assertIsNone(row["delete_argv"])

    def test_stale_or_partial_git_recovery_map_holds_combined_data(self):
        row = self.fixture.row()
        row["recovery_refs"].pop(next(iter(row["recovery_refs"])))
        result = self.entry(archives_input=[row])["resources"][0]
        self.assertEqual(result["reversible_eligibility"]["status"], "held")
        self.assertFalse(result["reversible_eligibility"]["git_recovery"]["verified_covering"])
        self.assertIsNone(result["delete_argv"])

    def test_corrupt_git_bundle_holds_combined_data(self):
        path = self.fixture.bundle
        raw = path.read_bytes()
        self.addCleanup(path.write_bytes, raw)
        path.write_bytes(raw[:-20] + b"x" * 20)
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertFalse(row["reversible_eligibility"]["archive_verified"])
        self.assertIsNone(row["delete_argv"])

    def test_missing_retaining_log_and_material_snapshot_hold(self):
        log = self.fixture.common / "logs/refs/stash"
        saved = log.read_bytes()
        self.addCleanup(log.write_bytes, saved)
        log.unlink()
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertEqual(row["reversible_eligibility"]["stash_noninterference"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])

    def test_actual_storage_alias_and_unknown_footprint_hold(self):
        original = reversible._phase_footprint
        def overlap(*args):
            footprint = original(*args)
            path = self.fixture.common / "objects"
            from pod.cleanup_observations import filesystem
            footprint["paths"].append({"path": str(path), "identity": filesystem(path), "operation": "remove_tree"})
            return footprint
        with patch.object(reversible, "_phase_footprint", side_effect=overlap):
            row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["stash_noninterference"]["status"], "overlap")
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIsNone(row["delete_argv"])

    def test_equal_bases_cannot_replace_unavailable_current_support(self):
        self.assertEqual(git(self.fixture.live, "rev-parse", "HEAD"), self.fixture.tip)
        original = stashes.snapshot
        def incomplete(*args):
            result = original(*args)
            result.update(complete=False, closure=None)
            return result
        with patch.object(stashes, "snapshot", side_effect=incomplete):
            row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertEqual({relation["relation"] for relation in row["stash_observation"]["relations"]}, {"unknown"})

    def test_untracked_and_modified_worktrees_remain_held_with_complete_archives(self):
        fixture = self.fixture
        path = fixture.target / "README"
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        path.write_bytes(b"dirty tracked data\n")
        fixture.capture()
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIsNone(row["delete_argv"])

    def test_untracked_data_is_not_authorized_by_a_whole_file_archive(self):
        fixture = self.fixture
        path = fixture.target / "untracked"
        path.write_bytes(b"untracked fixture\n")
        self.addCleanup(path.unlink)
        fixture.capture()
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIsNone(row["delete_argv"])

    def test_staged_data_and_lock_state_hold_without_force(self):
        fixture = self.fixture
        path = fixture.target / "README"
        original = path.read_bytes()
        def restore():
            path.write_bytes(original)
            git(fixture.target, "reset", "--quiet", "HEAD", "README")
        self.addCleanup(restore)
        path.write_bytes(b"staged tracked data\n")
        git(fixture.target, "add", "README")
        fixture.capture()
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIsNone(row["delete_argv"])

    def test_locked_admin_is_unsupported_even_with_all_bytes_archived(self):
        path = self.fixture.admin / "locked"
        path.write_bytes(b"retained fixture\n")
        self.addCleanup(path.unlink)
        self.fixture.capture()
        row = self.entry()["resources"][0]
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIsNone(row["delete_argv"])


class RemainingRefEntryTests(EntryFixture):
    """Separate fixture so a real disposable Git removal cannot perturb other tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.fixture
        # The disposable producer freezes exact common selected-ref log bytes
        # in the ordinary complete map before any worktree phase occurs.
        raw = (fixture.common / "logs" / fixture.ref).read_bytes()
        blob = git(fixture.project, "hash-object", "-w", "--stdin", data=raw)
        alias = "refs/recovery/selected-log"
        git(fixture.project, "update-ref", alias, blob)
        fixture.bundle.unlink()
        git(fixture.target, "bundle", "create", str(fixture.bundle), fixture.ref,
            "refs/bisect/saved", "refs/worktree/saved", "refs/rewritten/saved", alias)
        fixture.bundle.chmod(0o600)
        fixture.recovery_refs = {name: oid for oid, name in (line.split() for line in
            git(fixture.project, "bundle", "list-heads", str(fixture.bundle)).splitlines())}
        git(fixture.project, "update-ref", "-d", alias)
        fixture.capture()
        cls.first = cls().entry()
        cls.stash_before = cls.fixture.stash_bytes()
        git(cls.fixture.project, "worktree", "remove", str(cls.fixture.target))

    def setUp(self):
        pass

    def test_removed_worktree_retains_only_integrity_and_fresh_ref_data_not_old_phase_success(self):
        fixture = self.fixture
        result = self.entry(kind="local_branch", name=fixture.ref)
        row = result["resources"][0]
        record = row["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)
        self.assertTrue(record["archive_verified"])
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertIn("new_operation_archive_scope_phase_continuity", record["executor_checks_required"])
        self.assertEqual(row["identity"]["current"]["status"], "present")
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])
        self.assertEqual(self.stash_before, fixture.stash_bytes())
        self.assertNotEqual(self.first["digest"], result["digest"])

    def test_old_worktree_expect_cannot_prove_the_current_remaining_ref_or_phase(self):
        with self.assertRaises(PodError) as caught:
            self.entry(kind="local_branch", name=self.fixture.ref, expect=self.first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_unavailable_existing_path_does_not_become_a_proved_absence(self):
        self.fixture.target.symlink_to(self.fixture.live, target_is_directory=True)
        self.addCleanup(self.fixture.target.unlink)
        row = self.entry(kind="local_branch", name=self.fixture.ref)["resources"][0]
        record = row["reversible_eligibility"]
        self.assertTrue(record["archive_verified"])
        self.assertIsNone(record["file_recovery"]["current_match"])
        self.assertEqual(record["status"], "held")
        self.assertIn("file_current_unavailable", record["reasons"])
        self.assertIsNone(row["delete_argv"])


class StableExpectEntryTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = cls().entry()

    def setUp(self):
        pass

    def test_a_same_data_expect_stays_stable(self):
        self.assertEqual(self.entry(expect=self.first["expect"])["digest"], self.first["digest"])

    def test_b_equal_byte_archive_identity_replacement_refuses_old_expect(self):
        path = self.fixture.archive
        replacement = path.with_name("replacement.tar")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        with self.assertRaises(PodError) as caught:
            self.entry(expect=self.first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")


class ChangedSourceEntryTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = cls().entry()
        (cls.fixture.target / "ignored/private/b").write_bytes(b"changed ignored bytes\n")

    def setUp(self):
        pass

    def test_current_byte_change_holds_eligibility_without_erasing_integrity(self):
        record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "held", record)
        self.assertTrue(record["archive_verified"])
        self.assertFalse(record["file_recovery"]["current_match"])

    def test_current_byte_change_refuses_old_expect(self):
        with self.assertRaises(PodError) as caught:
            self.entry(expect=self.first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")


class ChangedStashEntryTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first = cls().entry()
        log = cls.fixture.common / "logs/refs/stash"
        raw = log.read_bytes()
        with log.open("ab") as stream:
            stream.write(raw.splitlines(keepends=True)[-1])

    def setUp(self):
        pass

    def test_unrelated_retained_stash_change_requires_no_new_recovery_root(self):
        record = self.entry()["resources"][0]["reversible_eligibility"]
        self.assertEqual(record["status"], "data_eligible", record)
        self.assertEqual(record["git_recovery"], self.first["resources"][0]["reversible_eligibility"]["git_recovery"])
        self.assertNotEqual(record["stash_noninterference"], self.first["resources"][0]["reversible_eligibility"]["stash_noninterference"])

    def test_retaining_log_change_refuses_old_expect(self):
        with self.assertRaises(PodError) as caught:
            self.entry(expect=self.first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")


class OrdinaryGuardsEntryTests(EntryFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.fixture
        # Only objective selection bookkeeping and native ports are synthetic.
        # No admission, earlier creator, release, authority or phase is invented.
        state = {"checkpoint": {"worktree": {"path": str(fixture.target), "branch": "target"},
                                "governance": {"base_ref": "refs/heads/main"}}, "admissions": {}}
        journal = {"actions": [], "units": {}}
        prior_plan = fixture.row()
        prior_plan.pop("worktree_archive")
        with patch("pod.cleanup.read", return_value=state), patch("pod.cleanup._read_journal", return_value=journal):
            cls.prior_plan = cls().entry(kind=None, objective="fixture-objective", archives_input=[prior_plan])
            cls.current = cls().entry(kind=None, objective="fixture-objective")

    def setUp(self):
        pass

    def test_positive_preservation_keeps_ordinary_unique_and_null_commands(self):
        row = next(row for row in self.current["resources"] if row["kind"] == "worktree")
        self.assertEqual(row["class"], "unique")
        self.assertEqual(row["reversible_eligibility"]["status"], "data_eligible", row["reversible_eligibility"])
        self.assertFalse(row["archive_verified"])
        self.assertIsNone(row["delete_argv"])

    def test_optional_data_clears_no_legacy_class_relation_or_guard(self):
        for before, after in zip(self.prior_plan["resources"], self.current["resources"]):
            for key in ("id", "kind", "class", "reasons", "delete_argv", "archive_argv", "archive_verified", "stash_observation"):
                with self.subTest(resource=before["id"], key=key):
                    self.assertEqual(before[key], after[key])
        row = next(row for row in self.current["resources"] if row["kind"] == "local_branch")
        self.assertEqual(row["reversible_eligibility"]["status"], "held")
        self.assertIn("current_checkout_elsewhere", row["reversible_eligibility"]["executor_checks_required"])


if __name__ == "__main__":
    unittest.main()

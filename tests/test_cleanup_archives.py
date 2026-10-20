"""Exact recovery and expect boundaries over disposable, independently restored Git bundles."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from pod import cleanup_archives
from pod.errors import PodError
from tests import test_cleanup_plan as plans


def _git_input(path, argv, data):
    return subprocess.run(["git", "-C", str(path), *argv], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def _archive(path, ref, tip, *, expanded=True):
    row = {"path": str(path), "ref": ref, "tip": tip}
    if expanded:
        advertised = plans.git(path.parent, "bundle", "list-heads", str(path))
        row.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   recovery_refs={name: oid for oid, name in (line.split() for line in advertised.splitlines())})
    return row


class ArchiveBranchTests(unittest.TestCase):
    """Build Git history and bundles once; each method owns its archive pathname."""

    @classmethod
    def setUpClass(cls):
        cls.case = plans.CleanupPlanTests()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        case = cls.case
        (case.child / "README.md").write_text("unique branch history\n")
        plans.git(case.child, "commit", "-qam", "unique branch")
        cls.tip = plans.git(case.child, "rev-parse", "HEAD")
        cls.ref = "refs/heads/objective"
        cls.alias = "refs/recovery/selected"
        plans.git(case.project, "update-ref", cls.alias, cls.tip)
        plans.git(case.project, "update-ref", "refs/recovery/older", case.base)
        cls.bundles = {}
        for name, refs in (("branch_only", [cls.ref]), ("alias", [cls.alias]),
                           ("stale", ["refs/recovery/older"]),
                           ("incremental", [cls.ref, "^" + case.base])):
            file = case.root / (name + ".bundle")
            plans.git(case.project, "bundle", "create", str(file), *refs)
            cls.bundles[name] = file.read_bytes()
        compressed = _git_input(case.project, ["hash-object", "-w", "--stdin"], b"a" * 32768)
        plans.git(case.project, "update-ref", "refs/recovery/compressed", compressed)
        file = case.root / "compressed.bundle"
        plans.git(case.project, "bundle", "create", str(file), cls.alias, "refs/recovery/compressed")
        cls.bundles["compressed"] = file.read_bytes()
        cls.admin = Path(plans.git(case.child, "rev-parse", "--absolute-git-dir"))

    def setUp(self):
        self.case.terminals = []
        self.directory = self.case.root / self._testMethodName
        self.directory.mkdir()
        self.bundle = self.directory / "saved.bundle"
        self.bundle.write_bytes(self.bundles["alias"])

    def observed(self, row=None, **kwargs):
        row = row if row is not None else _archive(self.bundle, self.ref, self.tip)
        return self.case.observed(archives=[row], **kwargs)

    def worktree(self, result):
        return self.case.resource(result, "worktree")

    def rejected(self, row=None):
        result = self.worktree(self.observed(row))
        self.assertFalse(result["archive_verified"], result["archives"])
        self.assertIsNone(result["delete_argv"])
        return result["archives"][0]

    def assert_changed(self, expect, row=None):
        with self.assertRaises(PodError) as changed:
            self.observed(row, expect=expect)
        self.assertEqual(changed.exception.code, "cleanup_changed")
        self.assertIn("worktree:" + str(self.case.child), changed.exception.detail["changed"])

    def test_exact_alias_recovers_current_source_and_preserves_unique_authority(self):
        before = plans.git(self.case.project, "show-ref")
        result = self.observed()
        row = self.worktree(result)
        self.assertEqual(row["class"], "unique")
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertTrue(row["archives"][0]["covers_resource"])
        self.assertEqual(row["archives"][0]["recovery_refs"], {self.alias: self.tip})
        self.assertNotIn(self.ref, row["archives"][0]["recovery_refs"])
        self.assertIsNotNone(row["delete_argv"])
        self.assertIn(row["id"], result["standing_authorization"]["asks"])
        self.assertNotIn(row["id"], result["standing_authorization"]["covers"])
        self.assertEqual(plans.git(self.case.project, "show-ref"), before)
        self.assertEqual(self.bundle.read_bytes(), self.bundles["alias"])
        self.assertEqual(self.observed(expect=result["expect"])["digest"], result["digest"])

    def test_branch_only_requires_original_exact_advertisement_and_stays_branch_only(self):
        branch_only = _archive(self.bundle, self.ref, self.tip, expanded=False)
        self.rejected(branch_only)
        self.bundle.write_bytes(self.bundles["branch_only"])
        row = self.worktree(self.observed(branch_only))
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertEqual(row["archives"][0]["mode"], "branch_only")

    def test_paired_fields_and_duplicate_source_rows_refuse(self):
        row = _archive(self.bundle, self.ref, self.tip)
        missing_digest, missing_map = dict(row), dict(row)
        missing_digest.pop("sha256")
        missing_map.pop("recovery_refs")
        for rows in ([missing_digest], [missing_map], [row, deepcopy(row)], [row] * 97):
            with self.subTest(rows=rows), self.assertRaises(PodError) as refused:
                self.case.observed(archives=rows)
            self.assertEqual(refused.exception.code, "invalid_cleanup_archives")

    def test_caller_relation_profile_and_required_roots_are_not_inputs(self):
        from pod.internal import run
        request = {"project": str(self.case.project), "objective": "objective"}
        for name in ("relation", "profile", "required_roots"):
            with self.subTest(name=name), self.assertRaises(PodError) as refused:
                run("cleanup-plan", {**request, name: {}})
            self.assertEqual(refused.exception.code, "invalid_request")

    def test_invalid_expanded_map_digest_and_bounds_refuse(self):
        row = _archive(self.bundle, self.ref, self.tip)
        for update in ({"sha256": "x" * 64}, {"recovery_refs": {}},
                       {"recovery_refs": {f"refs/recovery/{i}": self.tip for i in range(97)}}):
            with self.subTest(update=update), self.assertRaises(PodError) as refused:
                self.observed({**row, **update})
            self.assertEqual(refused.exception.code, "invalid_cleanup_archives")

    def test_missing_and_digest_corrupt_archives_fail_closed(self):
        row = _archive(self.bundle, self.ref, self.tip)
        self.bundle.unlink()
        self.rejected(row)
        self.bundle.write_bytes(self.bundles["alias"] + b"changed")
        self.assertIn("SHA256", " ".join(self.rejected(row)["holds"]))

    def test_corrupt_and_truncated_pack_with_matching_digest_fail_closed(self):
        original = self.bundles["alias"]
        for data in (original[:-32], original[:-21] + bytes([original[-21] ^ 1]) + original[-20:]):
            with self.subTest(size=len(data)):
                self.bundle.write_bytes(data)
                self.rejected()

    def test_incremental_bundle_valid_at_source_cannot_borrow_source_objects(self):
        self.bundle.write_bytes(self.bundles["incremental"])
        plans.git(self.case.project, "bundle", "verify", str(self.bundle))
        row = self.rejected()
        self.assertIn("bundle", " ".join(row["holds"]))

    def test_stale_complete_bundle_cannot_cover_the_current_tip(self):
        self.bundle.write_bytes(self.bundles["stale"])
        self.rejected()
        self.rejected(_archive(self.bundle, self.ref, self.case.base))

    def test_compressed_pack_does_not_bypass_restored_object_byte_bound(self):
        self.bundle.write_bytes(self.bundles["compressed"])
        with patch.object(cleanup_archives, "MAX_BUNDLE", self.bundle.stat().st_size):
            result = self.rejected()
        self.assertIn("Restored object bytes exceed", " ".join(result["holds"]))

    def test_incomplete_extra_and_wrong_recovery_maps_fail_closed(self):
        row = _archive(self.bundle, self.ref, self.tip)
        for refs in ({self.alias: self.case.base}, {self.alias: self.tip, "refs/recovery/extra": self.tip}):
            with self.subTest(refs=refs):
                observed = self.rejected({**row, "recovery_refs": refs})
                self.assertEqual(observed["recovery_refs"], {self.alias: self.tip})

    def test_current_tip_and_actual_source_identity_are_independent_of_map(self):
        row = _archive(self.bundle, self.ref, self.tip)
        self.rejected({**row, "tip": self.case.base})
        original = (self.admin / "HEAD").read_bytes()
        (self.admin / "HEAD").write_text(self.case.base + "\n")
        self.addCleanup((self.admin / "HEAD").write_bytes, original)
        result = self.worktree(self.observed(row))
        self.assertIsNone(result["delete_argv"])
        self.assertFalse(result["archive_verified"])

    def test_same_validity_different_bundle_and_map_invalidates_expect(self):
        first = self.observed()
        self.bundle.write_bytes(self.bundles["branch_only"])
        current = _archive(self.bundle, self.ref, self.tip)
        self.assertTrue(self.worktree(self.observed(current))["archive_verified"])
        self.assert_changed(first["expect"], current)

    def test_changed_supplied_tip_or_map_invalidates_expect(self):
        first = self.observed()
        row = _archive(self.bundle, self.ref, self.tip)
        self.assert_changed(first["expect"], {**row, "tip": self.case.base})
        self.assert_changed(first["expect"], {**row, "recovery_refs": {self.alias: self.case.base}})

    def test_same_bytes_at_a_different_path_invalidates_expect(self):
        first = self.observed()
        other = self.directory / "another.bundle"
        other.write_bytes(self.bundle.read_bytes())
        self.assert_changed(first["expect"], _archive(other, self.ref, self.tip))

    def test_same_bytes_file_replacement_invalidates_expect(self):
        first = self.observed()
        replacement = self.directory / "replacement.bundle"
        replacement.write_bytes(self.bundle.read_bytes())
        replacement.replace(self.bundle)
        self.assert_changed(first["expect"])

    def test_same_bytes_directory_recreation_invalidates_expect(self):
        first = self.observed()
        old = self.directory.with_name(self.directory.name + "-old")
        self.directory.rename(old)
        self.directory.mkdir()
        shutil.copyfile(old / self.bundle.name, self.bundle)
        current = self.worktree(self.observed())
        self.assertTrue(current["archive_verified"])
        self.assertEqual(current["archives"][0]["sha256"], first["resources"][-1]["archives"][0]["sha256"])
        self.assert_changed(first["expect"])

    def test_symlink_file_and_parent_and_hardlink_are_refused(self):
        real = self.directory / "real.bundle"
        self.bundle.rename(real)
        self.bundle.symlink_to(real)
        self.rejected(_archive(real, self.ref, self.tip) | {"path": str(self.bundle)})
        self.bundle.unlink()
        os.link(real, self.bundle)
        self.rejected(_archive(real, self.ref, self.tip) | {"path": str(self.bundle)})
        link = self.directory.with_name(self.directory.name + "-link")
        link.symlink_to(self.directory, target_is_directory=True)
        self.rejected(_archive(real, self.ref, self.tip) | {"path": str(link / real.name)})

    def test_bundle_inside_checkout_or_admin_is_not_durable(self):
        for base in (self.case.child, self.admin):
            file = base / "retained.bundle"
            file.write_bytes(self.bundles["alias"])
            self.addCleanup(file.unlink)
            row = self.rejected(_archive(file, self.ref, self.tip))
            self.assertIn("inside", " ".join(row["holds"]))

    def test_file_swap_during_verification_uses_snapshot_and_refuses_changed_path(self):
        row = _archive(self.bundle, self.ref, self.tip)
        original = cleanup_archives._run
        swapped = False
        def run(root, argv, **kwargs):
            nonlocal swapped
            result = original(root, argv, **kwargs)
            if argv[0] == "fsck" and not swapped:
                replacement = self.directory / "raced.bundle"
                replacement.write_bytes(self.bundle.read_bytes())
                replacement.replace(self.bundle)
                swapped = True
            return result
        with patch.object(cleanup_archives, "_run", side_effect=run):
            found = self.rejected(row)
        self.assertTrue(swapped)
        self.assertIn("changed", " ".join(found["holds"]))

    def test_dirty_ignored_and_foreign_native_holder_stay_held_with_valid_archive(self):
        file = self.case.child / "valuable.txt"
        file.write_text("uncommitted data\n")
        self.addCleanup(file.unlink, missing_ok=True)
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertIsNone(row["delete_argv"])
        file.unlink()
        exclude = self.case.project / ".git" / "info" / "exclude"
        original = exclude.read_bytes()
        exclude.write_text("*.scratch\n")
        self.addCleanup(exclude.write_bytes, original)
        file = self.case.child / "valuable.scratch"
        file.write_text("ignored data\n")
        self.addCleanup(file.unlink)
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertIsNone(row["delete_argv"])
        self.assertIn("valuable.scratch", " ".join(row["reasons"]))
        self.case.terminals = [{"handle": "foreign", "worktreePath": str(self.case.child)}]
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])

    def test_older_head_reflog_root_needs_expanded_preservation(self):
        log = self.admin / "logs" / "HEAD"
        original = log.read_bytes()
        self.addCleanup(log.write_bytes, original)
        older = plans.git(self.case.project, "commit-tree", self.tip + "^{tree}", "-m", "older independent HEAD")
        plans.git(self.case.child, "checkout", "-q", "--detach", older)
        plans.git(self.case.child, "checkout", "-q", "objective")
        row = self.worktree(self.observed())
        self.assertIn(older, row["identity"]["git"]["required_objects"])
        self.assertFalse(row["archive_verified"])
        self.bundle.write_bytes(self.bundles["branch_only"])
        branch_only = _archive(self.bundle, self.ref, self.tip, expanded=False)
        row = self.worktree(self.observed(branch_only))
        self.assertTrue(row["archive_verified"])
        self.assertFalse(row["archives"][0]["covers_resource"])
        self.assertIsNone(row["delete_argv"])

    def test_private_ref_appearance_and_unsupported_root_change_expect_and_hold(self):
        first = self.observed()
        plans.git(self.case.child, "update-ref", "refs/worktree/private", self.tip)
        self.addCleanup(plans.git, self.case.child, "update-ref", "-d", "refs/worktree/private")
        self.assert_changed(first["expect"])
        unsupported = self.admin / "unknown-root"
        unsupported.write_text(self.tip + "\n")
        self.addCleanup(unsupported.unlink)
        row = self.worktree(self.observed())
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])

    def test_complete_map_cannot_hide_duplicate_or_missing_advertised_targets(self):
        original = self.bundles["alias"]
        header, pack = original.split(b"\n\n", 1)
        for extra in (f"{self.tip} {self.alias}", f"{'f' * 40} refs/recovery/missing"):
            with self.subTest(extra=extra):
                self.bundle.write_bytes(header + b"\n" + extra.encode() + b"\n\n" + pack)
                self.rejected()

    def test_head_and_worktree_qualified_advertisements_are_explicitly_refused(self):
        header, pack = self.bundles["alias"].split(b"\n\n", 1)
        for name in ("HEAD", "worktrees/selected/HEAD"):
            with self.subTest(name=name):
                self.bundle.write_bytes(header + f"\n{self.tip} {name}\n\n".encode() + pack)
                result = self.rejected()
                self.assertEqual(result["recovery_refs"][name], self.tip)
                self.assertIn("Unsupported bundle advertisement", " ".join(result["holds"]))


class ArchiveRootTests(unittest.TestCase):
    """One graph with two stash entries and independently rooted private objects."""

    @classmethod
    def setUpClass(cls):
        cls.case = plans.CleanupPlanTests()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        case = cls.case
        (case.child / "README.md").write_text("unique tip\n")
        plans.git(case.child, "commit", "-qam", "unique tip")
        cls.tip = plans.git(case.child, "rev-parse", "HEAD")
        cls.ref = "refs/heads/objective"
        cls.stashes = []
        for index in range(2):
            (case.child / "README.md").write_text(f"index {index}\n")
            plans.git(case.child, "add", "README.md")
            (case.child / "README.md").write_text(f"worktree {index}\n")
            (case.child / "untracked.txt").write_text(f"untracked {index}\n")
            plans.git(case.child, "stash", "push", "-uqm", f"entry {index}")
            cls.stashes.append(plans.git(case.child, "rev-parse", "refs/stash"))
        blob = _git_input(case.project, ["hash-object", "-w", "--stdin"], b"private rooted blob\n")
        base_blob = plans.git(case.project, "rev-parse", cls.tip + ":README.md")
        tree = _git_input(case.project, ["mktree"], f"100644 blob {base_blob}\tprivate.txt\n".encode())
        commit = plans.git(case.project, "commit-tree", cls.tip + "^{tree}", "-m", "independent private commit")
        plans.git(case.project, "tag", "-am", "private annotation", "private-tag", commit)
        tag = plans.git(case.project, "rev-parse", "refs/tags/private-tag")
        cls.private = {"commit": commit, "tree": tree, "blob": blob, "tag": tag}
        for name, oid in cls.private.items():
            plans.git(case.child, "update-ref", "refs/worktree/" + name, oid)
        aliases = {"refs/recovery/selected": cls.tip}
        aliases.update({f"refs/recovery/stash-{i}": oid for i, oid in enumerate(cls.stashes)})
        aliases.update({"refs/recovery/" + name: oid for name, oid in cls.private.items()})
        for name, oid in aliases.items():
            plans.git(case.project, "update-ref", name, oid)
        cls.bundles = {}
        variants = {"full": list(aliases), "branch": [cls.ref],
                    "latest": [name for name in aliases if name != "refs/recovery/stash-0"],
                    "no-blob": [name for name in aliases if name != "refs/recovery/blob"],
                    "no-tree": [name for name in aliases if name != "refs/recovery/tree"]}
        for name, refs in variants.items():
            file = case.root / (name + ".bundle")
            plans.git(case.project, "bundle", "create", str(file), *refs)
            cls.bundles[name] = file.read_bytes()

    def setUp(self):
        self.case.terminals = []
        directory = self.case.root / self._testMethodName
        directory.mkdir()
        self.bundle = directory / "saved.bundle"
        self.bundle.write_bytes(self.bundles["full"])

    def observed(self, *, expanded=True, **kwargs):
        return self.case.observed(archives=[_archive(self.bundle, self.ref, self.tip, expanded=expanded)], **kwargs)

    def worktree(self, result):
        return self.case.resource(result, "worktree")

    def preserve_stash_metadata(self):
        for relative in ("refs/stash", "logs/refs/stash"):
            file = self.case.project / ".git" / relative
            self.addCleanup(file.write_bytes, file.read_bytes())

    def test_full_recovery_verifies_stash_index_untracked_and_all_private_object_types(self):
        stash_before = plans.git(self.case.project, "stash", "list", "--format=%gd:%H")
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertTrue(row["archives"][0]["covers_resource"])
        facts = row["identity"]["git"]
        for oid in self.stashes:
            self.assertEqual(len(facts["commits"][oid]["parents"]), 3)
            for commit in [oid, *facts["commits"][oid]["parents"]]:
                self.assertEqual(facts["required_objects"][commit], "commit")
                self.assertEqual(facts["required_objects"][facts["commits"][commit]["tree"]], "tree")
        self.assertEqual(set(row["archives"][0]["recovery_types"].values()), {"commit", "tree", "blob", "tag"})
        self.assertEqual(row["class"], "unique")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("repository stash is present", row["reasons"])
        self.assertEqual(plans.git(self.case.project, "stash", "list", "--format=%gd:%H"), stash_before)

    def test_branch_only_expanded_bundle_cannot_claim_stash_or_private_roots(self):
        self.bundle.write_bytes(self.bundles["branch"])
        row = self.worktree(self.observed())
        self.assertFalse(row["archive_verified"], row["archives"])
        self.assertIsNone(row["delete_argv"])

    def test_full_private_recovery_without_stashes_still_needs_unique_authority(self):
        self.preserve_stash_metadata()
        plans.git(self.case.project, "update-ref", "-d", "refs/stash")
        result = self.observed()
        row = self.worktree(result)
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertEqual(row["class"], "unique")
        self.assertIsNotNone(row["delete_argv"])
        self.assertIn(row["id"], result["standing_authorization"]["asks"])
        self.assertNotIn(row["id"], result["standing_authorization"]["covers"])

    def test_changed_stash_oid_invalidates_expect_while_full_archive_stays_valid(self):
        self.preserve_stash_metadata()
        first = self.observed()
        plans.git(self.case.project, "stash", "drop", "-q", "stash@{0}")
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertIsNone(row["delete_argv"])
        with self.assertRaises(PodError) as changed:
            self.observed(expect=first["expect"])
        self.assertEqual(changed.exception.code, "cleanup_changed")

    def test_branch_only_branch_proof_never_becomes_full_root_preservation(self):
        self.bundle.write_bytes(self.bundles["branch"])
        row = self.worktree(self.observed(expanded=False))
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertFalse(row["archives"][0]["covers_resource"])
        self.assertIsNone(row["delete_argv"])

    def test_latest_stash_recovery_does_not_cover_older_reflog_root(self):
        self.bundle.write_bytes(self.bundles["latest"])
        row = self.worktree(self.observed())
        self.assertFalse(row["archive_verified"], row["archives"])
        self.assertIn("missing", " ".join(row["archives"][0]["holds"]))
        self.assertEqual(row["identity"]["git"]["repository"]["stash_roots"], list(reversed(self.stashes)))

    def test_commit_stash_recovery_does_not_cover_independent_noncommit_roots(self):
        for variant in ("no-blob", "no-tree"):
            with self.subTest(variant=variant):
                self.bundle.write_bytes(self.bundles[variant])
                row = self.worktree(self.observed())
                self.assertFalse(row["archive_verified"], row["archives"])
                self.assertEqual(set(row["identity"]["git"]["private_refs"].values()), set(self.private.values()))

    def test_unadvertised_dangling_pack_objects_do_not_supply_recovery_roots(self):
        header, pack = self.bundles["full"].split(b"\n\n", 1)
        lines = [line for line in header.split(b"\n") if not line.endswith(b" refs/recovery/blob")]
        self.bundle.write_bytes(b"\n".join(lines) + b"\n\n" + pack)
        row = self.worktree(self.observed())
        self.assertFalse(row["archive_verified"])
        self.assertIn("outside the advertised recovery closure", " ".join(row["archives"][0]["holds"]))

    def test_stash_read_unknown_and_foreign_live_holder_remain_held(self):
        real = plans.plan.__globals__["_git"]
        def read(path, argv):
            return None if argv[:2] == ["stash", "list"] else real(path, argv)
        with patch("pod.cleanup._git", side_effect=read):
            row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])
        self.case.terminals = [{"handle": "live-foreign", "worktreePath": str(self.case.child)}]
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertIsNone(row["delete_argv"])


class ArchiveObjectFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = patch.dict(os.environ, {"GIT_DEFAULT_HASH": "sha256"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        cls.case = plans.CleanupPlanTests()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        case = cls.case
        (case.child / "README.md").write_text("sha256 unique tip\n")
        plans.git(case.child, "commit", "-qam", "sha256 unique tip")
        cls.tip = plans.git(case.child, "rev-parse", "HEAD")
        cls.bundle = case.root / "sha256.bundle"
        plans.git(case.project, "bundle", "create", str(cls.bundle), "refs/heads/objective")

    def test_empty_restoration_matches_source_object_format(self):
        row = _archive(self.bundle, "refs/heads/objective", self.tip)
        result = self.case.observed(archives=[row])
        worktree = self.case.resource(result, "worktree")
        self.assertEqual(len(self.tip), 64)
        self.assertTrue(worktree["archive_verified"], worktree["archives"])
        self.assertEqual(worktree["identity"]["git"]["repository"]["object_format"], "sha256")


if __name__ == "__main__":
    unittest.main()

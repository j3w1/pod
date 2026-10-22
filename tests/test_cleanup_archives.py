"""Ordinary branch preservation and bounded current Git readers in disposable projects."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pod import cleanup_archives
from pod.errors import PodError
from tests import test_cleanup_plan as plans
from tests.common import fixture


def _git_input(path, argv, data):
    return subprocess.run(["git", "-C", str(path), *argv], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def _archive(path, ref, tip):
    return {"path": str(path), "ref": ref, "tip": tip}


@contextmanager
def observed_types():
    """Retain actual complete verification results without exporting the optional diagnostic."""
    checks, original = [], cleanup_archives._types
    def inspect(root, oids, **kwargs):
        found = original(root, oids, **kwargs)
        if kwargs.get("isolated"):
            checks.append(found)
        return found
    with patch.object(cleanup_archives, "_types", side_effect=inspect):
        yield checks


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
        plans.git(case.project, "bundle", "create", str(file), cls.ref, "refs/recovery/compressed")
        cls.bundles["compressed"] = file.read_bytes()
        cls.admin = Path(plans.git(case.child, "rev-parse", "--absolute-git-dir"))

    def setUp(self):
        self.case.terminals = []
        self.directory = self.case.root / self._testMethodName
        self.directory.mkdir()
        self.bundle = self.directory / "saved.bundle"
        self.bundle.write_bytes(self.bundles["branch_only"])

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
        with plans.current_resources() as captured, self.assertRaises(PodError) as changed:
            self.observed(row, expect=expect)
        self.assertEqual(changed.exception.code, "cleanup_changed")
        self.assertIn("worktree:" + str(self.case.child), changed.exception.detail["changed"])
        return next(resource for resource in captured if resource["kind"] == "worktree")


    def test_branch_only_requires_original_exact_advertisement_and_stays_branch_only(self):
        branch_only = _archive(self.bundle, self.ref, self.tip)
        self.bundle.write_bytes(self.bundles["alias"])
        self.rejected(branch_only)
        self.bundle.write_bytes(self.bundles["branch_only"])
        row = self.worktree(self.observed(branch_only))
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertEqual(row["archives"][0]["mode"], "branch_only")


    def test_caller_relation_profile_and_required_roots_are_not_inputs(self):
        from pod.internal import run
        request = {"project": str(self.case.project), "objective": "objective"}
        for name in ("relation", "profile", "required_roots"):
            with self.subTest(name=name), self.assertRaises(PodError) as refused:
                run("cleanup-plan", {**request, name: {}})
            self.assertEqual(refused.exception.code, "invalid_request")



    def test_corrupt_and_truncated_pack_with_matching_digest_fail_closed(self):
        original = self.bundles["branch_only"]
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
        with patch.object(cleanup_archives, "MAX_RESTORED_BYTES", 32767):
            result = self.rejected()
        self.assertIn("content bounds", " ".join(result["holds"]))

    def test_compressed_input_cap_is_independent_of_restored_content_limits(self):
        with patch.object(cleanup_archives, "MAX_BUNDLE", self.bundle.stat().st_size - 1):
            found = self.rejected()
        self.assertIn("Archive file is oversized", " ".join(found["holds"]))

    def test_compressed_pack_per_object_and_pre_restore_count_bounds_refuse(self):
        self.bundle.write_bytes(self.bundles["compressed"])
        with patch.object(cleanup_archives, "MAX_OBJECT_BYTES", 32767):
            self.assertIn("content bounds", " ".join(self.rejected()["holds"]))
        header, pack = self.bundles["branch_only"].split(b"\n\n", 1)
        count = (cleanup_archives.MAX_RESTORED_OBJECTS + 1).to_bytes(4, "big")
        self.bundle.write_bytes(header + b"\n\n" + pack[:8] + count + pack[12:])
        original = cleanup_archives._run
        def run(root, argv, **kwargs):
            self.assertNotEqual(argv[:2], ["bundle", "unbundle"])
            return original(root, argv, **kwargs)
        with patch.object(cleanup_archives, "_run", side_effect=run):
            self.assertIn("pack object count", " ".join(self.rejected()["holds"]))





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
        current = self.assert_changed(first["expect"])
        self.assertTrue(current["archive_verified"])
        self.assertEqual(current["archives"][0]["sha256"], first["resources"][-1]["archives"][0]["sha256"])




    def test_synthetic_sensitive_metadata_never_enters_output_or_a_disk_spool(self):
        marker = b"synthetic-nonsecret-credential-fixture"
        config = self.case.project / ".git" / "config"
        log = self.admin / "logs" / "HEAD"
        self.addCleanup(config.write_bytes, config.read_bytes())
        self.addCleanup(log.write_bytes, log.read_bytes())
        plans.git(self.case.project, "config", "credential.fixture", marker.decode())
        with log.open("ab") as stream:
            stream.write(f"{self.tip} {self.tip} Fixture <fixture@example.invalid> 1 +0000\t".encode()
                         + marker + b"\n")
        commit = plans.git(self.case.project, "commit-tree", self.tip + "^{tree}", "-m", marker.decode())
        spools, inspections = [], []
        original_file, original_run = cleanup_archives.tempfile.TemporaryFile, cleanup_archives._run
        @contextmanager
        def spool(*args, **kwargs):
            with original_file(*args, **kwargs) as stream:
                try:
                    yield stream
                finally:
                    stream.seek(0)
                    value = stream.read()
                    spools.append(value.encode() if isinstance(value, str) else value)
        def run(root, argv, **kwargs):
            value = original_run(root, argv, **kwargs)
            inspections.append((list(argv), kwargs.get("hash_output", False)))
            self.assertNotIn(marker, value)
            return value
        with patch.object(cleanup_archives.tempfile, "TemporaryFile", side_effect=spool), \
                patch.object(cleanup_archives, "_run", side_effect=run):
            facts = cleanup_archives._commits(self.case.project, {commit})
            result = self.observed()
        self.assertEqual(facts[commit]["tree"], plans.git(self.case.project, "rev-parse", self.tip + "^{tree}"))
        self.assertTrue(self.worktree(result)["archive_verified"], self.worktree(result)["archives"])
        self.assertNotIn(marker.decode(), repr(result))
        self.assertTrue(spools)  # The separate worktree inventory spool was actually inspected.
        self.assertTrue(all(marker not in value for value in spools))
        config_reads = [(argv, hashed) for argv, hashed in inspections if argv[0] == "config"]
        self.assertTrue(config_reads)
        self.assertTrue(all(hashed or "--get-regexp" in argv for argv, hashed in config_reads))
        self.assertTrue(all(argv[:2] != ["cat-file", "--batch"] for argv, _ in inspections))

    def test_arbitrary_synthetic_config_value_change_invalidates_expect(self):
        config = self.case.project / ".git" / "config"
        self.addCleanup(config.write_bytes, config.read_bytes())
        plans.git(self.case.project, "config", "credential.fixture", "synthetic-before")
        first = self.observed()
        plans.git(self.case.project, "config", "credential.fixture", "synthetic-after")
        self.assert_changed(first["expect"])

    def test_bounded_capture_discards_sensitive_error_and_oversized_output(self):
        config = self.case.project / ".git" / "config"
        self.addCleanup(config.write_bytes, config.read_bytes())
        plans.git(self.case.project, "config", "credential.fixture", "synthetic-nonsecret-value")
        with patch.object(cleanup_archives, "MAX_OUTPUT", 8), self.assertRaises(cleanup_archives.Unavailable) as exc:
            cleanup_archives._run(self.case.project, ["config", "--get", "credential.fixture"])
        self.assertIn("output bound", str(exc.exception))
        self.assertNotIn("synthetic", str(exc.exception))
        with self.assertRaises(cleanup_archives.Unavailable) as exc:
            cleanup_archives._run(self.case.project, ["config", "--type=bool", "--get", "credential.fixture"])
        self.assertNotIn("synthetic", str(exc.exception))

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

    def test_ignored_data_stays_held_with_valid_archive(self):
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

    def test_foreign_native_holder_stays_held_with_valid_archive(self):
        self.case.terminals = [{"handle": "foreign", "worktreePath": str(self.case.child)}]
        row = self.worktree(self.observed())
        self.assertTrue(row["archive_verified"])
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])

    def test_older_head_reflog_root_keeps_narrow_archive_insufficient(self):
        log = self.admin / "logs" / "HEAD"
        original = log.read_bytes()
        self.addCleanup(log.write_bytes, original)
        older = plans.git(self.case.project, "commit-tree", self.tip + "^{tree}", "-m", "older independent HEAD")
        plans.git(self.case.child, "checkout", "-q", "--detach", older)
        plans.git(self.case.child, "checkout", "-q", "objective")
        row = self.worktree(self.observed())
        self.assertIn(older, self.case.material(row)["required_objects"])
        self.assertFalse(row["archive_verified"])
        self.bundle.write_bytes(self.bundles["branch_only"])
        branch_only = _archive(self.bundle, self.ref, self.tip)
        row = self.worktree(self.observed(branch_only))
        self.assertTrue(row["archives"][0]["verified"])
        self.assertFalse(row["archive_verified"])
        self.assertFalse(row["archives"][0]["covers_resource"])
        self.assertIsNone(row["delete_argv"])

    def test_branch_and_worktree_cache_bind_their_independent_material_bases(self):
        log = self.admin / "logs" / "HEAD"
        original = log.read_bytes()
        self.addCleanup(log.write_bytes, original)
        older = plans.git(self.case.project, "commit-tree", self.tip + "^{tree}", "-m", "private old HEAD")
        plans.git(self.case.child, "checkout", "-q", "--detach", older)
        plans.git(self.case.child, "checkout", "-q", "objective")
        self.bundle.write_bytes(self.bundles["branch_only"])
        row = _archive(self.bundle, self.ref, self.tip)
        result = self.observed(row)
        worktree, branch = self.worktree(result), self.case.resource(result, "local_branch")
        self.assertIsNot(worktree["archives"], branch["archives"])
        self.assertFalse(worktree["archive_verified"])
        self.assertTrue(branch["archive_verified"], branch["archives"])
        self.assertTrue(branch["archives"][0]["covers_resource"])
        self.assertEqual((worktree["class"], branch["class"]), ("unique", "unique"))
        self.assertIsNone(branch["delete_argv"])
        self.assertIsNone(worktree["delete_argv"])

    def test_private_ref_appearance_and_unsupported_root_change_expect_and_hold(self):
        first = self.observed()
        plans.git(self.case.child, "update-ref", "refs/worktree/private", self.tip)
        self.addCleanup(plans.git, self.case.child, "update-ref", "-d", "refs/worktree/private")
        self.assert_changed(first["expect"])

    def test_unsupported_admin_root_holds_with_a_valid_archive(self):
        unsupported = self.admin / "unknown-root"
        unsupported.write_text(self.tip + "\n")
        self.addCleanup(unsupported.unlink)
        row = self.worktree(self.observed())
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])

    def test_branch_bundle_cannot_hide_duplicate_or_missing_advertised_targets(self):
        original = self.bundles["branch_only"]
        header, pack = original.split(b"\n\n", 1)
        for extra in (f"{self.tip} {self.ref}", f"{'f' * 40} refs/recovery/missing"):
            with self.subTest(extra=extra):
                self.bundle.write_bytes(header + b"\n" + extra.encode() + b"\n\n" + pack)
                self.rejected()

    def test_head_and_worktree_qualified_advertisements_are_explicitly_refused(self):
        header, pack = self.bundles["branch_only"].split(b"\n\n", 1)
        for name in ("HEAD", "worktrees/selected/HEAD"):
            with self.subTest(name=name):
                self.bundle.write_bytes(header + f"\n{self.tip} {name}\n\n".encode() + pack)
                result = self.rejected()
                self.assertEqual(result["recovery_refs"][name], self.tip)
                self.assertIn("Unsupported bundle advertisement", " ".join(result["holds"]))




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
        self.assertEqual(self.case.material(worktree)["repository"]["object_format"], "sha256")




class ArchiveInheritedLimitTests(unittest.TestCase):
    """Actual Popen limits change only inside disposable parent and adapter processes."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture()
        cls.root = cls.fixture.__enter__()
        cls.addClassCleanup(cls.fixture.__exit__, None, None, None)
        adapter = cls.root / "git"
        adapter.write_text("#!" + sys.executable + "\nimport json,resource\nprint(json.dumps(["
                           "resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_FSIZE)]))\n")
        adapter.chmod(0o700)

    def observed(self, limits):
        script = """import json,resource,sys
from pathlib import Path
from pod import cleanup_archives
limits=json.loads(sys.argv[2])
for kind, values in zip((resource.RLIMIT_AS,resource.RLIMIT_FSIZE),limits):
    resource.setrlimit(kind,tuple(values))
before=[resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_FSIZE)]
child=json.loads(cleanup_archives._run(Path(sys.argv[1]),['--version']))
print(json.dumps({'before':before,'child':child}))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), json.dumps(limits)],
                                env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"]},
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_finite_inherited_soft_and_hard_limits_are_never_raised(self):
        mib = 1024 * 1024
        result = self.observed([[256 * mib, 512 * mib], [mib, 16 * mib]])
        self.assertEqual(result["child"], result["before"])

    def test_infinite_inherited_limits_use_ordinary_bounds_without_changing_hard_limits(self):
        import resource
        unlimited = resource.RLIM_INFINITY
        result = self.observed([[unlimited, unlimited], [unlimited, unlimited]])
        self.assertEqual(result["child"], [[cleanup_archives.MAX_GIT_MEMORY, unlimited],
                                          [cleanup_archives.MAX_BUNDLE, unlimited]])

    def test_finite_soft_limits_survive_infinite_hard_limits(self):
        import resource
        mib, unlimited = 1024 * 1024, resource.RLIM_INFINITY
        result = self.observed([[256 * mib, unlimited], [mib, unlimited]])
        self.assertEqual(result["child"], result["before"])

    def test_finite_bounds_and_equal_tighter_hard_limits_are_respected(self):
        mib = 1024 * 1024
        result = self.observed([[512 * mib, 512 * mib], [16 * mib, 16 * mib]])
        self.assertEqual(result["child"], result["before"])
        result = self.observed([[3 * 1024 * mib, 3 * 1024 * mib], [256 * mib, 256 * mib]])
        self.assertEqual(result["child"], [[cleanup_archives.MAX_GIT_MEMORY, 3 * 1024 * mib],
                                          [cleanup_archives.MAX_BUNDLE, 256 * mib]])




class OidTraversalBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = {width: b"".join(f"{index:0{width}x}\n".encode() for index in range(1, 100001))
                     for width in (40, 64)}

    def parse(self, raw, width):
        output = cleanup_archives._OidOutput(width)
        for start in range(0, len(raw), 65536):
            output.feed(raw[start:start + 65536])
        output.finish()
        return output

    def test_sha256_exact_100000_records_and_6500000_bytes_pass(self):
        output = self.parse(self.lines[64], 64)
        self.assertEqual((output.records, len(output.found), output.size), (100000, 100000, 6500000))
        self.assertEqual(output.pending, b"")

    def test_sha1_exact_100000_records_have_the_smaller_exact_byte_domain(self):
        output = self.parse(self.lines[40], 40)
        self.assertEqual((output.records, len(output.found), output.size), (100000, 100000, 4100000))
        self.assertEqual(output.byte_limit, 4100000)

    def test_sha256_record_or_byte_overflow_is_held(self):
        with self.assertRaises(cleanup_archives.Unavailable):
            self.parse(self.lines[64] + f"{100001:064x}\n".encode(), 64)

    def test_sha1_record_or_byte_overflow_is_held(self):
        with self.assertRaises(cleanup_archives.Unavailable):
            self.parse(self.lines[40] + f"{100001:040x}\n".encode(), 40)

    def test_repeated_records_are_held_before_distinct_count_can_hide_them(self):
        output = cleanup_archives._OidOutput(64)
        with self.assertRaises(cleanup_archives.Unavailable) as error:
            output.feed((b"1" * 64 + b"\n") * 2)
        self.assertIn("duplicate", str(error.exception))
        self.assertEqual((output.records, len(output.found)), (2, 1))

    def test_invalid_mixed_width_blank_and_incomplete_records_are_held(self):
        for raw in (b"", b"\n", b"z" * 40 + b"\n", b"1" * 64 + b"\n", b"0" * 40 + b"\n",
                    b"1" * 40, b"1" * 40 + b"\r\n", b"1" * 40 + b" \n"):
            with self.subTest(raw_length=len(raw)), self.assertRaises(cleanup_archives.Unavailable):
                self.parse(raw, 40)

    def test_split_records_are_validated_across_fixed_read_boundaries(self):
        output = cleanup_archives._OidOutput(64)
        raw = self.lines[64][:650]
        for position in range(len(raw)):
            output.feed(raw[position:position + 1])
            self.assertLessEqual(len(output.pending), 64)
        output.finish()
        self.assertEqual(output.records, 10)

    @contextmanager
    def producer(self, script):
        original, children = subprocess.Popen, []
        def start(argv, **kwargs):
            process = original([sys.executable, "-c", script], **kwargs)
            children.append(process)
            return process
        with patch.object(cleanup_archives.subprocess, "Popen", side_effect=start):
            try:
                yield children
            finally:
                for child in children:
                    self.assertIsNotNone(child.poll(), "inspection child was not reaped")

    def test_real_pipe_uses_fixed_chunks_and_keeps_generic_output_cap_unchanged(self):
        script = "import sys; sys.stdout.buffer.write(b''.join(f'{i:064x}\\n'.encode() for i in range(1,100001)))"
        actual_read, reads = cleanup_archives.os.read, []
        def read(descriptor, size):
            reads.append(size)
            return actual_read(descriptor, size)
        output = cleanup_archives._OidOutput(64)
        with self.producer(script), patch.object(cleanup_archives.os, "read", side_effect=read), \
                patch.object(cleanup_archives.tempfile, "TemporaryFile", side_effect=AssertionError("OID spool")):
            value = cleanup_archives._run(Path("/"), ["rev-list"], _oid_output=output)
        self.assertEqual(value, b"")
        self.assertEqual((output.records, output.size), (100000, 6500000))
        self.assertTrue(reads)
        # Popen's exec-error pipe also reads once, independently of the inspector.
        self.assertIn(65536, reads)
        self.assertTrue(all(size <= 65536 for size in reads))
        with self.producer(script), self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._run(Path("/"), ["rev-list"])
        self.assertIn("output bound", str(error.exception))

    def test_producer_failure_after_valid_output_holds_and_discards_stderr(self):
        marker = "synthetic-private-error-marker"
        script = ("import sys; sys.stdout.write('1'*40+'\\n'); "
                  f"sys.stderr.write({marker!r}); sys.exit(3)")
        with self.producer(script), self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._run(Path("/"), ["rev-list"], _oid_output=cleanup_archives._OidOutput(40))
        self.assertNotIn(marker, str(error.exception))
        self.assertIn("failed", str(error.exception))

    def test_stderr_retains_the_generic_two_mb_bound_during_oid_traversal(self):
        script = "import sys; sys.stdout.write('1'*40+'\\n'); sys.stderr.write('x'*2000001)"
        with self.producer(script), self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._run(Path("/"), ["rev-list"], _oid_output=cleanup_archives._OidOutput(40))
        self.assertIn("output bound", str(error.exception))

    def test_one_absolute_deadline_covers_format_read_traversal_and_child_cleanup(self):
        ticks = iter((100.0, 101.0, 129.0, 131.0))
        with self.producer("import sys; sys.stdin.buffer.read()") as children, \
                patch.object(cleanup_archives.time, "monotonic", side_effect=lambda: next(ticks, 131.0)), \
                patch.object(cleanup_archives, "_text", return_value="sha1\n") as metadata, \
                self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._walk_oids(Path("/"), {"1" * 40})
        self.assertIn("time bound", str(error.exception))
        self.assertEqual(metadata.call_args.kwargs["deadline"], 130.0)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_missing_root_or_unsupported_format_cannot_be_a_complete_walk(self):
        with self.producer("import sys; sys.stdout.write('2'*40+'\\n')"), \
                patch.object(cleanup_archives, "_text", return_value="sha1\n"), \
                self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._walk_oids(Path("/"), {"1" * 40})
        self.assertIn("omitted", str(error.exception))
        with patch.object(cleanup_archives, "_text", return_value="unknown\n"), \
                self.assertRaises(cleanup_archives.Unavailable):
            cleanup_archives._walk_oids(Path("/"), {"1" * 40})


class TypeQueryBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oids = {f"{index:064x}" for index in range(1, 100001)}

    def answer(self, root, argv, **kwargs):
        raw = kwargs["data"]
        self.assertLessEqual(len(raw), cleanup_archives.MAX_OUTPUT)
        self.assertLessEqual(len(raw.splitlines()), 4096)
        output = b"".join(line + b" blob\n" for line in raw.splitlines())
        self.assertLessEqual(len(output), cleanup_archives.MAX_OUTPUT)
        self.deadlines.append(kwargs["deadline"])
        return output.decode()

    def test_complete_100000_object_types_use_bounded_metadata_queries_and_one_deadline(self):
        self.deadlines = []
        with patch.object(cleanup_archives, "_text", side_effect=self.answer) as query:
            found = cleanup_archives._types(Path("/"), self.oids, limit=100000)
        self.assertEqual(found, dict.fromkeys(self.oids, "blob"))
        self.assertEqual(query.call_count, 25)
        self.assertEqual(len(set(self.deadlines)), 1)

    def test_late_incomplete_duplicate_or_unsupported_type_answers_hold(self):
        oids = set(sorted(self.oids)[:4097])
        for ending in ("", "f" * 64 + " blob\n", sorted(oids)[-1] + " unknown\n"):
            self.deadlines = []
            def answer(root, argv, **kwargs):
                return self.answer(root, argv, **kwargs) if len(kwargs["data"].splitlines()) > 1 else ending
            with self.subTest(ending=ending), patch.object(cleanup_archives, "_text", side_effect=answer), \
                    self.assertRaises(cleanup_archives.Unavailable):
                cleanup_archives._types(Path("/"), oids, limit=100000)

    def test_type_chunks_do_not_create_a_new_time_budget(self):
        self.deadlines = []
        ticks = iter((100.0, 101.0, 131.0))
        with patch.object(cleanup_archives.time, "monotonic", side_effect=lambda: next(ticks, 131.0)), \
                patch.object(cleanup_archives, "_text", side_effect=self.answer), \
                self.assertRaises(cleanup_archives.Unavailable) as error:
            cleanup_archives._types(Path("/"), set(sorted(self.oids)[:4097]), limit=100000)
        self.assertIn("time bound", str(error.exception))
        self.assertEqual(self.deadlines, [130.0, 130.0])

    def test_source_type_limit_is_independent_of_restored_content_limit(self):
        self.deadlines = []
        with patch.object(cleanup_archives, "MAX_RESTORED_OBJECTS", 1), \
                patch.object(cleanup_archives, "_text", side_effect=self.answer):
            found = cleanup_archives._types(Path("/"), self.oids, limit=cleanup_archives.MAX_TRAVERSAL_OBJECTS)
        self.assertEqual(len(found), 100000)


class SourceTraversalScaleTests(unittest.TestCase):
    """Real SHA1/SHA256 graphs and stash closure; production checks stay inside methods."""

    @staticmethod
    def history(case, count, name):
        data = bytearray()
        for index in range(count):
            value = f"source history object {index}\n".encode()
            data.extend(f"blob\nmark :{index + 1}\ndata {len(value)}\n".encode() + value)
        data.extend(f"commit refs/heads/{name}\ncommitter Fixture <fixture@example.invalid> 1 +0000\ndata 4\nbig\n".encode())
        data.extend(f"from {case.base}\n".encode())
        data.extend("".join(f"M 100644 :{index + 1} f-{index}\n" for index in range(count)).encode())
        data.extend(b"\ndone\n")
        _git_input(case.project, ["fast-import", "--quiet", "--done"], bytes(data))
        return plans.git(case.project, "rev-parse", "refs/heads/" + name)

    @classmethod
    def setUpClass(cls):
        cls.cases, cls.envs, cls.tips = {}, {}, {}
        for object_format in ("sha1", "sha256"):
            environment = patch.dict(os.environ, {"GIT_DEFAULT_HASH": object_format})
            environment.start()
            cls.addClassCleanup(environment.stop)
            case = plans.CleanupPlanTests()
            cls.addClassCleanup(case.doCleanups)
            case.setUp()
            cls.cases[object_format] = case
            cls.envs[object_format] = {key: value for key, value in os.environ.items()
                                      if key.startswith("XDG_") or key == "HOME"}
            cls.tips[object_format] = cls.history(case, 50000, "surviving-large")
        case = cls.cases["sha256"]
        base_count = len(plans.git(case.project, "rev-list", "--objects", "--no-object-names", case.base).splitlines())
        cls.limit_tip = cls.history(case, 100000 - base_count - 2, "exact-limit")

    def assert_source(self, object_format):
        case, tip = self.cases[object_format], self.tips[object_format]
        with patch.dict(os.environ, self.envs[object_format]):
            result = plans.cleanup_entry(case)
        row = case.resource(result, "worktree")
        self.assertEqual(row["class"], "integrated", row["reasons"])
        self.assertIsNotNone(row["delete_argv"])

    def test_sha1_50008_object_surviving_history_passes_the_actual_entry(self):
        self.assert_source("sha1")

    def test_sha256_50008_object_surviving_history_passes_the_actual_entry(self):
        # The independent exact-boundary graph would make this combined closure too large.
        case = self.cases["sha256"]
        plans.git(case.project, "update-ref", "-d", "refs/heads/exact-limit")
        self.addCleanup(plans.git, case.project, "update-ref", "refs/heads/exact-limit", self.limit_tip)
        self.assert_source("sha256")

    def assert_stash(self, object_format):
        from pod import cleanup_stashes
        case, tip = self.cases[object_format], self.tips[object_format]
        index = plans.git(case.project, "commit-tree", tip + "^{tree}", "-p", tip, "-m", "fixture index")
        stash = plans.git(case.project, "commit-tree", case.base + "^{tree}", "-p", tip, "-p", index, "-m", "fixture stash")
        plans.git(case.project, "stash", "store", "-m", "fixture stash", stash)
        self.addCleanup(plans.git, case.project, "stash", "clear")
        snapshot = cleanup_stashes.snapshot(case.project, cleanup_archives.repository_observation(case.project))
        self.assertTrue(snapshot["complete"], snapshot["reasons"])
        self.assertGreater(snapshot["closure"]["object_count"], 50008)
        self.assertEqual(snapshot["closure"]["integrity"], "verified")

    def test_large_sha1_stash_closure_checks_complete_types_and_preserves_unknown_holders(self):
        self.assert_stash("sha1")

    def test_large_sha256_stash_closure_checks_complete_types_and_preserves_unknown_holders(self):
        self.assert_stash("sha256")

    def test_real_sha256_100000_object_boundary_passes_the_actual_entry(self):
        case = self.cases["sha256"]
        plans.git(case.project, "update-ref", "-d", "refs/heads/surviving-large")
        self.addCleanup(plans.git, case.project, "update-ref", "refs/heads/surviving-large", self.tips["sha256"])
        with patch.dict(os.environ, self.envs["sha256"]):
            result = plans.cleanup_entry(case)
        self.assertIsNotNone(case.resource(result, "worktree")["delete_argv"])

    def test_real_object_count_overflow_holds_the_actual_entry(self):
        case = self.cases["sha256"]
        with patch.dict(os.environ, self.envs["sha256"]):
            result = plans.cleanup_entry(case)
        row = case.resource(result, "worktree")
        self.assertFalse(row["identity"]["git"]["complete"])
        self.assertIsNone(row["delete_argv"])
        self.assertIn("bound", " ".join(row["reasons"]))

    def test_surviving_ref_change_after_walk_is_detected_before_a_command_can_survive(self):
        case = self.cases["sha1"]
        tip = self.tips["sha1"]
        plans.git(case.project, "update-ref", "refs/heads/race", tip)
        self.addCleanup(plans.git, case.project, "update-ref", "-d", "refs/heads/race")
        original, changed = cleanup_archives._walk_oids, False
        def walk(root, roots, **kwargs):
            nonlocal changed
            found = original(root, roots, **kwargs)
            if not changed:
                plans.git(case.project, "update-ref", "refs/heads/race", case.base)
                changed = True
            return found
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(case.root / "state"),
                                     "XDG_CONFIG_HOME": str(case.root / "config")}), \
                patch.object(cleanup_archives, "_walk_oids", side_effect=walk):
            result = plans.cleanup_entry(case)
        self.assertTrue(changed)
        self.assertIsNone(case.resource(result, "worktree")["delete_argv"])

    def test_arbitrary_tree_and_blob_roots_are_not_negative_commit_subtraction(self):
        case = self.cases["sha1"]
        tip = self.tips["sha1"]
        tree = plans.git(case.project, "rev-parse", tip + "^{tree}")
        blob = plans.git(case.project, "rev-parse", tip + ":f-0")
        outside = _git_input(case.project, ["hash-object", "-w", "--stdin"], b"independent object root\n")
        self.assertEqual(cleanup_archives._uncovered(case.project, {tree, blob, outside}, {tip}), {outside})




if __name__ == "__main__":
    unittest.main()


class OrdinaryArchiveInputTests(unittest.TestCase):
    def test_only_the_ordinary_branch_reference_shape_is_supported(self):
        row = {"path": "/fixture/saved.bundle", "ref": "refs/heads/task", "tip": "1" * 40}
        self.assertEqual(cleanup_archives.archive_rows([row]), [row])
        for field in ("sha256", "recovery_refs", "worktree_archive", "authority", "phase", "relation"):
            with self.subTest(field=field), self.assertRaises(PodError):
                cleanup_archives.archive_rows([{**row, field: {}}])

    def test_duplicate_sources_and_excess_archive_rows_still_refuse(self):
        row = {"path": "/fixture/saved.bundle", "ref": "refs/heads/task", "tip": "1" * 40}
        for rows in ([row, dict(row)], [row] * 97):
            with self.subTest(count=len(rows)), self.assertRaises(PodError):
                cleanup_archives.archive_rows(rows)

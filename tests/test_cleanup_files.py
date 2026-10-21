"""Real private pax extraction and recovery in disposable, configuration-isolated Git projects."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod import cleanup_archives as archives, cleanup_files as files, cleanup_observations as observations


def git(root, *args, data=None):
    return subprocess.run(["git", "-C", str(root), *args], input=data, check=True,
                          capture_output=True).stdout.decode().strip()


class FileFixture:
    """An independently authored file artifact, plus an unrelated retained live stash."""

    def __init__(self, *, object_format="sha1"):
        self.temporary = tempfile.TemporaryDirectory(prefix="pod-recovery-fixture-")
        self.root = Path(self.temporary.name)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                           **{key: str(self.root / key.lower()) for key in ("POD_CONFIG_HOME", "POD_STATE_HOME", "POD_CACHE_HOME")})
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.project, self.target, self.live = (self.root / name for name in ("project", "target", "live"))
        self.project.mkdir()
        git(self.project, "init", "--quiet", "--initial-branch=main", "--template=", "--object-format=" + object_format)
        (self.project / "README").write_text("tracked fixture\n")
        (self.project / ".gitignore").write_text("ignored/\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "--quiet", "-m", "fixture")
        self.tip = git(self.project, "rev-parse", "HEAD")
        git(self.project, "worktree", "add", "--quiet", "-b", "target", str(self.target))
        git(self.project, "worktree", "add", "--quiet", "-b", "live", str(self.live))
        (self.live / "README").write_text("live index\n")
        git(self.live, "add", "README")
        (self.live / "README").write_text("live workfiles\n")
        (self.live / "untracked").write_text("live untracked\n")
        git(self.live, "stash", "push", "--include-untracked", "--quiet", "-m", "fixture")
        self.ref = "refs/heads/target"
        self.common = self.project / ".git"
        self.admin = Path(git(self.target, "rev-parse", "--absolute-git-dir"))
        ignored = self.target / "ignored"
        ignored.mkdir(mode=0o700)
        (ignored / "private").mkdir(mode=0o770)
        (ignored / "private").chmod(0o770)
        (ignored / "private" / "a").write_bytes(b"private ignored data\n")
        (ignored / "private" / "a").chmod(0o600)
        (ignored / "private" / "b").write_bytes(b"group readable fixture\n")
        (ignored / "private" / "b").chmod(0o640)
        os.link(ignored / "private" / "a", ignored / "private" / "twin")
        (ignored / "private" / "link").symlink_to("a")
        self.marker = "synthetic-private-config-and-log-payload"
        (self.admin / "config.worktree").write_text("[include]\n path = /synthetic-missing-config\n[fixture]\n value = " + self.marker + "\n")
        (self.admin / "COMMIT_EDITMSG").write_text(self.marker + "\n")
        (self.admin / "ORIG_HEAD").write_text(self.tip + "\n")
        (self.admin / "FETCH_HEAD").write_text(self.tip + "\t\tfixture\n")
        for namespace in ("bisect", "worktree", "rewritten"):
            git(self.target, "update-ref", "--create-reflog", "-m", "fixture", f"refs/{namespace}/saved", self.tip)
        old_log = self.admin / "logs/refs/rewritten/old"
        old_log.write_bytes((self.admin / "logs/refs/rewritten/saved").read_bytes())
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.bundle = self.artifacts / "git.bundle"
        git(self.target, "bundle", "create", str(self.bundle), self.ref,
            "refs/bisect/saved", "refs/worktree/saved", "refs/rewritten/saved")
        self.bundle.chmod(0o600)
        self.recovery_refs = {ref: oid for oid, ref in (line.split() for line in
                              git(self.project, "bundle", "list-heads", str(self.bundle)).splitlines())}
        self.archive = self.artifacts / "files.tar"
        self.capture()

    def close(self):
        self.environment.stop()
        self.temporary.cleanup()

    def stash_bytes(self):
        roots = git(self.project, "rev-list", "--objects", "--no-object-names", "refs/stash").splitlines()
        objects = subprocess.run(["git", "-C", str(self.project), "cat-file", "--batch"],
                                 input=("\n".join(roots) + "\n").encode(), check=True, capture_output=True).stdout
        return ((self.common / "refs/stash").read_bytes(), (self.common / "logs/refs/stash").read_bytes(),
                roots, hashlib.sha256(objects).hexdigest())

    def capture(self, *, transform=None, member_transform=None, format=tarfile.PAX_FORMAT):
        entries, members, seen = [], [], {}
        for prefix, root in (("worktree", self.target), ("admin", self.admin)):
            paths = [root]
            for parent, directories, names in os.walk(root, followlinks=False):
                paths.extend(Path(parent) / name for name in sorted(directories + names))
            for path in sorted(set(paths), key=lambda value: value.relative_to(root).as_posix()):
                name = prefix + ("/" + path.relative_to(root).as_posix() if path != root else "")
                info = path.lstat()
                source_stat = {key: getattr(info, "st_" + key) for key in
                               ("dev", "ino", "mode", "nlink", "size", "mtime_ns", "ctime_ns")}
                kind = "dir" if path.is_dir() and not path.is_symlink() else "symlink" if path.is_symlink() else "file"
                raw, link = (None, None) if kind == "dir" else (os.readlink(path).encode(), os.readlink(path)) if kind == "symlink" else (path.read_bytes(), None)
                if kind == "file" and info.st_nlink > 1:
                    key = info.st_dev, info.st_ino
                    if key in seen:
                        kind, link = "hardlink", seen[key]
                    else:
                        seen[key] = name
                entries.append({"name": name, "type": kind, "size": 0 if raw is None else len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                                "link": link, "exec": bool(info.st_mode & 0o111), "source_stat": source_stat})
                member = tarfile.TarInfo(name)
                member.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE,
                               "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE}[kind]
                member.mode = info.st_mode & 0o7777
                member.linkname = link or ""
                member.size = len(raw) if kind == "file" else 0
                members.append((member, raw if kind == "file" else None))
        raw_status = subprocess.run(["git", "--no-optional-locks", "-C", str(self.target), "status", "--porcelain=v1", "-z",
                                     "--untracked-files=all", "--ignored"], check=True, capture_output=True).stdout
        manifest = {"schema": "pod-worktree-archive/v1", "source": {
            "path": str(self.target), "ref": self.ref, "tip": self.tip,
            "common_git_dir": str(self.common), "git_admin_dir": str(self.admin),
            "worktree_identity": observations.filesystem(self.target), "admin_identity": observations.filesystem(self.admin)},
            "status_sha256": hashlib.sha256(raw_status).hexdigest(), "entries": entries}
        if transform:
            transform(manifest)
        self.manifest = deepcopy(manifest)
        self.write(manifest, members, member_transform=member_transform, format=format)

    def write(self, manifest, members, *, member_transform=None, format=tarfile.PAX_FORMAT):
        with tarfile.open(self.archive, "w", format=format) as archive:
            raw = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode()
            header = tarfile.TarInfo(files.MANIFEST)
            header.mode, header.size = 0o600, len(raw)
            archive.addfile(header, io.BytesIO(raw))
            for member, raw in members:
                member = deepcopy(member)
                if member_transform:
                    member_transform(member)
                archive.addfile(member, io.BytesIO(raw) if raw is not None else None)
        self.archive.chmod(0o600)

    def row(self):
        return {"path": str(self.bundle), "ref": self.ref, "tip": self.tip,
                "sha256": hashlib.sha256(self.bundle.read_bytes()).hexdigest(),
                "recovery_refs": dict(self.recovery_refs), "worktree_archive": {
                    "path": str(self.archive), "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest()}}

    def verify(self, row=None):
        return files.verify(self.project, row or self.row(), [self.target, self.common])


class FileRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = FileFixture()
        cls.addClassCleanup(cls.fixture.close)

    def setUp(self):
        self.fixture.capture()

    def held(self, expected=None, row=None):
        file, admin, basis = self.fixture.verify(row)
        self.assertNotEqual(file["status"], "verified", file)
        self.assertFalse(file["restore_verified"])
        if expected:
            self.assertIn(expected, file["reasons"])
        return file, admin, basis

    def test_full_private_pax_restores_all_modes_links_admin_and_regenerated_git(self):
        fixture = self.fixture
        before = fixture.stash_bytes()
        original = files._compare
        comparisons = []
        def compare(entries, actual, **kwargs):
            comparisons.append((deepcopy(actual), kwargs))
            return original(entries, actual, **kwargs)
        original_run = archives._run
        controls = []
        def git_run(root, argv, **kwargs):
            if root.name == "recovered":
                marker = (root / ".git").read_text()
                self.assertNotEqual(marker, (fixture.target / ".git").read_text())
                private = Path(marker.removeprefix("gitdir: ").strip())
                self.assertTrue(private.is_relative_to(root.parent))
                self.assertFalse((private / "config.worktree").exists())
                self.assertFalse((private / "COMMIT_EDITMSG").exists())
                controls.append(argv[0])
            return original_run(root, argv, **kwargs)
        with patch.object(files, "_compare", side_effect=compare), patch.object(archives, "_run", side_effect=git_run):
            file, admin, basis = fixture.verify()
        self.assertEqual(file["status"], "verified", file)
        self.assertTrue(file["restore_verified"])
        self.assertTrue(file["current_match"])
        self.assertEqual(admin, {"status": "verified", "reasons": []})
        self.assertEqual(before, fixture.stash_bytes())
        recovered = next(actual for actual, options in comparisons if options.get("skip_marker"))
        self.assertEqual({name: recovered[name]["mode"] for name in (
            "worktree/ignored", "worktree/ignored/private", "worktree/ignored/private/a", "worktree/ignored/private/b")},
            {"worktree/ignored": 0o700, "worktree/ignored/private": 0o770,
             "worktree/ignored/private/a": 0o600, "worktree/ignored/private/b": 0o640})
        self.assertNotIn(fixture.marker, json.dumps((file, admin, basis)))
        self.assertEqual(set(basis["admin"]["refs"]), {f"refs/{name}/saved" for name in ("bisect", "worktree", "rewritten")})
        self.assertIn("refs/rewritten/old", basis["admin"]["logs"])
        self.assertEqual(controls, ["read-tree", "status"])

    def test_mode_loss_with_equal_exec_bits_fails_real_restore_comparison(self):
        real = files._extract
        def lose_mode(archive, members, entries, destination, deadline, **kwargs):
            real(archive, members, entries, destination, deadline, **kwargs)
            (destination / "worktree/ignored").chmod(0o755)
        with patch.object(files, "_extract", side_effect=lose_mode):
            self.held("file_restore_failed")

    def test_missing_and_corrupt_archive_hold(self):
        row = self.fixture.row()
        row["worktree_archive"]["path"] += ".missing"
        self.held("file_archive_missing", row)
        self.fixture.archive.write_bytes(b"not a tar")
        self.held()

    def test_digest_mismatch_and_source_tip_mismatch_hold(self):
        row = self.fixture.row()
        row["worktree_archive"]["sha256"] = "0" * 64
        self.held("file_archive_changed", row)
        self.fixture.capture(transform=lambda manifest: manifest["source"].update(tip="1" * 40))
        self.held("file_current_changed")

    def test_duplicate_manifest_entries_and_incomplete_payload_hold(self):
        self.fixture.capture(transform=lambda manifest: manifest["entries"].append(deepcopy(manifest["entries"][-1])))
        self.held("manifest_invalid")
        self.fixture.capture(transform=lambda manifest: manifest["entries"].pop())
        self.held("manifest_incomplete")

    def test_extra_manifest_fields_and_stat_bool_are_refused(self):
        self.fixture.capture(transform=lambda manifest: manifest.update(authority=True))
        self.held("manifest_invalid")
        self.fixture.capture(transform=lambda manifest: manifest["entries"][0]["source_stat"].update(ino=True))
        self.held("manifest_invalid")

    def test_header_mode_and_unsafe_special_mode_hold(self):
        self.fixture.capture(member_transform=lambda member: setattr(member, "mode", 0o755) if member.name == "worktree/ignored" else None)
        self.held("file_archive_unsafe")
        def unsafe(manifest):
            manifest["entries"][0]["source_stat"]["mode"] |= 0o2000
        self.fixture.capture(transform=unsafe)
        self.held("file_archive_unsafe")

    def test_absolute_and_escaping_symlink_targets_hold(self):
        for link in ("/etc/passwd", "../../outside"):
            def transform(manifest):
                next(entry for entry in manifest["entries"] if entry["type"] == "symlink")["link"] = link
            self.fixture.capture(transform=transform)
            self.held("unsupported_link")

    def test_cross_prefix_and_missing_hardlink_targets_hold(self):
        for link in ("admin/HEAD", "worktree/missing"):
            self.fixture.capture(transform=lambda manifest: next(entry for entry in manifest["entries"] if entry["type"] == "hardlink").update(link=link))
            self.held("unsupported_link")

    def test_unknown_vendor_pax_acl_and_gnu_headers_hold(self):
        self.fixture.capture(member_transform=lambda member: member.pax_headers.update({"SCHILY.acl.access": "user::rwx"}) if member.name == "worktree" else None)
        self.held("file_archive_unsafe")
        self.fixture.capture(format=tarfile.GNU_FORMAT)
        self.held("file_archive_unsafe")

    def test_world_readable_file_or_containing_directory_hold(self):
        fixture = self.fixture
        fixture.archive.chmod(0o640)
        self.held("file_archive_unsafe")
        fixture.archive.chmod(0o600)
        fixture.artifacts.chmod(0o750)
        self.addCleanup(fixture.artifacts.chmod, 0o700)
        self.held("file_archive_unsafe")

    def test_archive_inside_removed_path_or_symlinked_file_holds(self):
        fixture = self.fixture
        destination = fixture.target / "ignored/archive.tar"
        destination.write_bytes(fixture.archive.read_bytes())
        destination.chmod(0o600)
        self.addCleanup(destination.unlink)
        row = fixture.row()
        row["worktree_archive"]["path"] = str(destination)
        self.held(row=row)
        fixture.archive.unlink()
        fixture.archive.symlink_to(destination)
        self.addCleanup(fixture.archive.unlink, missing_ok=True)
        self.held(row=fixture.row())

    def test_changed_source_bytes_keep_integrity_separate_from_current_match(self):
        fixture = self.fixture
        path = fixture.target / "ignored/private/b"
        before = path.read_bytes()
        self.addCleanup(path.write_bytes, before)
        path.write_bytes(b"new bytes with the same Git status\n")
        file, admin, _ = fixture.verify()
        self.assertEqual(file["status"], "verified", file)
        self.assertTrue(file["restore_verified"])
        self.assertFalse(file["current_match"])
        self.assertIn("file_current_changed", file["reasons"])
        self.assertEqual(admin["status"], "verified")

    def test_unlisted_admin_root_is_not_silently_omitted(self):
        fixture = self.fixture
        path = fixture.admin / "unsupported"
        path.write_bytes(b"extra admin state")
        self.addCleanup(path.unlink)
        fixture.capture()
        self.held("unsupported_admin_state")

    def test_entry_manifest_byte_and_time_limits_are_conjunctive(self):
        with patch.object(files, "MAX_ENTRIES", 1):
            self.held("file_archive_budget")
        with patch.object(files, "MAX_MANIFEST", 100):
            self.held()
        with patch.object(files, "SECONDS", 0):
            self.held("file_archive_budget")

    def test_artifact_per_file_and_aggregate_limits_do_not_raise_physical_caps(self):
        with patch.object(files, "MAX_BYTES", 512):
            self.held("file_archive_budget")
        self.fixture.capture(transform=lambda manifest: manifest["entries"][-1].update(size=files.MAX_BYTES + 1))
        self.held("file_archive_budget")

    def test_unsupported_current_mount_is_held_after_integrity_verification(self):
        real = observations.identity
        target = self.fixture.target / "ignored"
        def mounted(path, info, descriptor):
            value = real(path, info, descriptor)
            if path == target:
                value["mount_id"] += 1
            return value
        with patch.object(observations, "identity", side_effect=mounted):
            file, _, _ = self.fixture.verify()
        self.assertNotEqual(file["current_match"], True)
        self.assertIn("unsupported_mount", file["reasons"])

    def test_actual_xattr_is_not_silently_discarded(self):
        path = self.fixture.target / "ignored/private/b"
        os.setxattr(path, "user.pod_fixture", b"fixture")
        self.addCleanup(os.removexattr, path, "user.pod_fixture")
        self.fixture.capture()
        file, admin, _ = self.fixture.verify()
        self.assertTrue(file["restore_verified"])
        self.assertEqual(admin["status"], "verified")
        self.assertIsNone(file["current_match"])
        self.assertIn("file_archive_unsafe", file["reasons"])
        self.assertIn("file_current_unavailable", file["reasons"])

    def test_special_current_file_is_held_without_opening_or_extracting_it(self):
        path = self.fixture.target / "ignored/private/fifo"
        os.mkfifo(path)
        self.addCleanup(path.unlink)
        file, admin, _ = self.fixture.verify()
        self.assertTrue(file["restore_verified"])
        self.assertEqual(admin["status"], "verified")
        self.assertIsNone(file["current_match"])
        self.assertIn("unsupported_file_type", file["reasons"])
        self.assertIn("file_current_unavailable", file["reasons"])

    def test_complete_4096_head_log_entries_are_preserved_as_data(self):
        path = self.fixture.admin / "logs/HEAD"
        raw = path.read_bytes()
        self.addCleanup(path.write_bytes, raw)
        path.write_bytes(raw.splitlines(keepends=True)[-1] * 4096)
        self.fixture.capture()
        file, admin, _ = self.fixture.verify()
        self.assertEqual(file["status"], "verified", file)
        self.assertEqual(admin["status"], "verified")

    def test_complete_4096_private_log_entries_are_preserved_as_data(self):
        path = self.fixture.admin / "logs/refs/worktree/saved"
        raw = path.read_bytes()
        self.addCleanup(path.write_bytes, raw)
        path.write_bytes(raw.splitlines(keepends=True)[-1] * 4096)
        self.fixture.capture()
        file, admin, _ = self.fixture.verify()
        self.assertEqual(file["status"], "verified", file)
        self.assertEqual(admin["status"], "verified")

    def test_4097_log_entries_hold(self):
        path = self.fixture.admin / "logs/HEAD"
        raw = path.read_bytes()
        self.addCleanup(path.write_bytes, raw)
        path.write_bytes(raw.splitlines(keepends=True)[-1] * 4097)
        self.fixture.capture()
        self.held("file_archive_budget")

    def test_missing_required_admin_marker_is_not_a_partial_success(self):
        self.fixture.capture(transform=lambda manifest: manifest["entries"].__setitem__(slice(None),
            [entry for entry in manifest["entries"] if entry["name"] != "admin/gitdir"]))
        self.held("admin_recovery_missing")

    def test_validation_of_every_member_precedes_any_extraction(self):
        self.fixture.capture(member_transform=lambda member: setattr(member, "name", "worktree/../../escape")
                             if member.name == "admin/ORIG_HEAD" else None)
        with patch.object(files, "_extract", side_effect=AssertionError("extraction before validation")):
            self.held("manifest_incomplete")

    def test_source_file_replacement_with_equal_bytes_does_not_match(self):
        path = self.fixture.target / "ignored/private/b"
        replacement = path.with_name("new-inode")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(path.stat().st_mode & 0o777)
        replacement.replace(path)
        file, _, _ = self.fixture.verify()
        self.assertFalse(file["current_match"], file)
        self.assertIn("file_current_changed", file["reasons"])

    def test_symlink_descendant_manifest_cannot_be_extracted(self):
        def descendant(manifest):
            entry = deepcopy(next(entry for entry in manifest["entries"] if entry["name"] == "worktree/ignored/private/b"))
            entry["name"] = "worktree/ignored/private/link/child"
            manifest["entries"].append(entry)
        self.fixture.capture(transform=descendant)
        with patch.object(files, "_extract", side_effect=AssertionError("unsafe extraction")):
            self.held("manifest_incomplete")

    def test_aggregate_payload_limit_is_independent_of_each_file_limit(self):
        def oversized(manifest):
            for entry in [entry for entry in manifest["entries"] if entry["type"] == "file"
                          and entry["source_stat"]["nlink"] == 1][:3]:
                entry["size"] = entry["source_stat"]["size"] = files.MAX_BYTES // 2
        self.fixture.capture(transform=oversized)
        self.held("file_archive_budget")

    def test_complete_admin_raw_bytes_are_bounded_together(self):
        path = self.fixture.admin / "COMMIT_EDITMSG"
        raw = path.read_bytes()
        self.addCleanup(path.write_bytes, raw)
        path.write_bytes(b"x" * (archives.MAX_OUTPUT + 1))
        self.fixture.capture()
        self.held("file_archive_budget")

    def test_unknown_empty_admin_namespace_is_not_a_known_directory(self):
        path = self.fixture.admin / "logs/unknown"
        path.mkdir()
        self.addCleanup(path.rmdir)
        self.fixture.capture()
        self.held("unsupported_admin_state")

    def test_relative_git_marker_and_absolute_common_pointer_remain_data(self):
        fixture = self.fixture
        marker, common = fixture.target / ".git", fixture.admin / "commondir"
        marker_raw, common_raw = marker.read_bytes(), common.read_bytes()
        self.addCleanup(marker.write_bytes, marker_raw)
        self.addCleanup(common.write_bytes, common_raw)
        marker.write_text("gitdir: " + os.path.relpath(fixture.admin, fixture.target) + "\n")
        common.write_text(str(fixture.common) + "\n")
        fixture.capture()
        file, admin, _ = fixture.verify()
        self.assertEqual(file["status"], "verified", file)
        self.assertEqual(admin["status"], "verified")


if __name__ == "__main__":
    unittest.main()

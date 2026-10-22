"""Selected protocol, physical ref freshness and observation-context boundaries."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pod import cleanup_archives as archives, cleanup_observations as observations, cleanup_stashes as stashes
from pod.cleanup import plan
from pod.errors import PodError
from pod.internal import run
from pod.util import digest
from tests import test_cleanup_plan as plans
from tests import test_cleanup_stashes as stash_tests


class SelectedObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = stash_tests.SourceFixture()
        cls.addClassCleanup(cls.source.close)

    def setUp(self):
        self.source.extra = {}
        self.source.unknown_holder = False

    def preserve(self, path):
        value = path.read_bytes() if path.exists() else None
        def restore():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
        self.addCleanup(restore)

    def assert_changed(self, first, **kwargs):
        with self.assertRaises(PodError) as caught:
            self.source.observe(expect=first["expect"], **kwargs)
        self.assertEqual(caught.exception.code, "cleanup_changed")

    def test_project_only_envelope_is_exact_protected_data_and_never_reads_objective_records(self):
        with patch("pod.cleanup.read", side_effect=AssertionError("objective read")), \
                patch("pod.ledger.read", side_effect=AssertionError("private read")), \
                patch("pod.cleanup._read_journal", side_effect=AssertionError("journal read")), \
                patch("pod.evidence_closure.closed_older_state", side_effect=AssertionError("objective closure read")):
            result = self.source.observe()
        self.assertEqual(set(result), {"schema", "objective", "closure_basis", "record_class", "standing_authorization",
                                      "target", "resources", "digest", "expect", "notes", "observation"})
        self.assertIsNone(result["objective"])
        self.assertEqual(result["record_class"], "unavailable")
        self.assertIsNone(result["closure_basis"])
        self.assertEqual(result["target"], {"ref": None, "commit": None, "moved_after_delivery": False})
        row = result["resources"][0]
        self.assertEqual(len(result["resources"]), 1)
        self.assertEqual(set(row), {"id", "kind", "class", "reasons", "identity", "delete_argv", "archive_argv",
                                   "archive_verified", "archives", "stash_observation", "fingerprint"})
        self.assertEqual(set(row["identity"]), {"ref", "tip", "checked_out", "git", "worker", "provenance",
                                               "current", "ref_storage", "observation_scope"})
        self.assertEqual(row["identity"]["provenance"], {"status": "unavailable", "source": None, "reference": None})
        self.assertEqual(row["identity"]["observation_scope"], {"mode": "project_only", "objective": None})
        self.assertIsNone(row["identity"]["worker"])
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertIn("resource_provenance_unavailable", row["reasons"])
        self.assertEqual(result["standing_authorization"], {"mode": "normal", "decision": None, "covers": [], "asks": [row["id"]]})
        self.assertEqual(row["fingerprint"], digest({key: value for key, value in row.items() if key != "fingerprint"}))
        self.assertEqual(result["digest"], digest([sorted([[row["id"], row["fingerprint"]]]), result["target"]]))

    def test_ordinary_objective_is_required_and_selected_null_empty_objective_is_not_omission(self):
        project = str(self.source.case.project)
        with self.assertRaises(PodError) as error:
            run("cleanup-plan", {"project": project})
        self.assertEqual(error.exception.code, "invalid_request")
        for value in (None, "", 4):
            with self.subTest(value=value), self.assertRaises(PodError) as error:
                run("cleanup-plan", {"project": project, "objective": value,
                                     "observe": {"kind": "local_branch", "name": self.source.ref}})
            self.assertEqual(error.exception.code, "invalid_objective")

    def test_selector_has_only_kind_and_name_and_no_caller_proof_or_context_flags(self):
        project = str(self.source.case.project)
        selector = {"kind": "local_branch", "name": self.source.ref}
        for key in ("phase", "profile", "relation", "footprint", "required_roots", "authority", "observation_scope"):
            with self.subTest(key=key), self.assertRaises(PodError) as error:
                run("cleanup-plan", {"project": project, "observe": selector, key: {}})
            self.assertEqual(error.exception.code, "invalid_request")
        for value in ({**selector, "profile": "anything"}, {"kind": "unknown", "name": "x"},
                      {"kind": "cache", "name": "relative"}, {"kind": "cache", "name": "/a/../b"}, None):
            with self.subTest(value=value), self.assertRaises(PodError) as error:
                run("cleanup-plan", {"project": project, "observe": value})
            self.assertEqual(error.exception.code, "invalid_cleanup_observe")

    def test_project_only_worker_is_refused_before_any_private_objective_read(self):
        with patch("pod.cleanup.read", side_effect=AssertionError("private read")), self.assertRaises(PodError) as error:
            run("cleanup-plan", {"project": str(self.source.case.project), "worker": {"dispatch": "foreign"},
                                 "observe": {"kind": "local_branch", "name": self.source.ref}})
        self.assertEqual(error.exception.code, "invalid_cleanup_observe")

    def test_same_mode_and_name_stay_stable_while_cross_mode_expect_refuses(self):
        first = self.source.observe()
        same = self.source.observe(expect=first["expect"])
        self.assertEqual(first["digest"], same["digest"])
        self.assert_changed(first, objective="missing-objective")

    def test_selected_opaque_git_digest_is_canonical_and_binds_every_internal_material_leaf(self):
        full = []
        original = archives.material_diagnostic
        def inspect(facts):
            full.append(deepcopy(facts))
            return original(facts)
        with patch.object(archives, "material_diagnostic", side_effect=inspect):
            first = self.source.row()
        facts = full[-1]
        self.assertEqual(first["identity"]["git"]["sha256"], digest(facts))
        self.assertEqual(set(first["identity"]["git"]), {"sha256", "complete", "kind", "reasons"})
        # Enumerate real internal leaf paths, including nested repository,
        # incarnation, config, commit, parent, tree, source and unknown facts.
        def leaves(value, path=()):
            if isinstance(value, dict) and value:
                for key, item in value.items():
                    yield from leaves(item, (*path, key))
            elif isinstance(value, list) and value:
                for key, item in enumerate(value):
                    yield from leaves(item, (*path, key))
            else:
                yield path
        for path in leaves(facts):
            changed = deepcopy(facts)
            parent = changed
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = {"fixture_changed": True}
            with self.subTest(path=path):
                self.assertNotEqual(archives.material_diagnostic(changed)["sha256"],
                                    first["identity"]["git"]["sha256"])

    def test_selected_hidden_head_log_change_refuses_old_expect_without_exporting_subjects(self):
        source = self.source
        log = source.private / "logs" / "HEAD"
        self.preserve(log)
        selection = {"kind": "worktree", "name": str(source.case.child)}
        first = source.observe(**selection)
        marker = "synthetic-private-head-subject"
        with log.open("ab") as stream:
            stream.write((source.case.base + " " + source.case.base
                          + " Fixture <fixture@example.invalid> 1 +0000\t" + marker + "\n").encode())
        self.assert_changed(first, **selection)
        row = source.row(**selection)
        self.assertEqual(row["class"], first["resources"][0]["class"])
        self.assertNotEqual(row["identity"]["git"]["sha256"], first["resources"][0]["identity"]["git"]["sha256"])
        self.assertNotIn(marker, json.dumps(row))
        self.assertIsNone(row["delete_argv"])

    def test_different_unavailable_objective_names_bind_the_same_other_facts_independently(self):
        first = self.source.observe(objective="missing-one")
        second = self.source.observe(objective="missing-two")
        left, right = deepcopy(first["resources"][0]), deepcopy(second["resources"][0])
        left.pop("fingerprint")
        right.pop("fingerprint")
        left["identity"].pop("observation_scope")
        right["identity"].pop("observation_scope")
        self.assertEqual(left, right)
        self.assertNotEqual(first["digest"], second["digest"])
        self.assert_changed(first, objective="missing-two")

    def test_objective_selection_bookkeeping_cannot_supply_creation_release_or_authority(self):
        result = self.source.observe(objective="objective")
        row = result["resources"][0]
        self.assertEqual(row["identity"]["provenance"], {"status": "observed", "source": "objective_checkpoint", "reference": "objective"})
        self.assertEqual(row["identity"]["observation_scope"], {"mode": "objective", "objective": "objective"})
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])
        self.assertIn("creation by this objective is unproven", row["reasons"])

    def test_missing_recorded_branch_cannot_associate_a_branch_literally_named_none(self):
        case = self.source.case
        plans.git(case.project, "branch", "None")
        self.addCleanup(plans.git, case.project, "branch", "-D", "None")
        from pod.ledger import read
        state = read(case.project, "objective")
        state["checkpoint"]["worktree"]["branch"] = None
        with patch("pod.cleanup.read", return_value=state):
            row = self.source.row(objective="objective", name="refs/heads/None")
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])

    def test_worktree_selected_identity_has_exact_table_and_native_effect_remains_unknown(self):
        row = self.source.row(kind="worktree", name=str(self.source.case.child))
        self.assertEqual(set(row["identity"]), {"path", "branch", "tip", "terminals", "shared_with", "git", "worker",
                                               "provenance", "current", "observation_scope"})
        self.assertEqual(row["identity"]["current"]["status"], "present")
        self.assertEqual(row["stash_observation"]["executor"]["kind"], "orca_worktree_remove")
        self.assertEqual({value["relation"] for value in row["stash_observation"]["relations"]}, {"unknown"})
        self.assertIsNone(row["delete_argv"])

    def test_cache_native_remote_project_only_rows_have_exact_unknown_tables_and_no_commands(self):
        cases = (("cache", str(self.source.case.root), {"path", "filesystem", "worker", "provenance", "current", "observation_scope"}),
                 ("native", "unbound-dispatch", {"dispatch", "native", "worker", "provenance", "current", "observation_scope"}),
                 ("remote_branch", "origin/unknown", {"remote", "ref", "tip", "git", "worker", "provenance", "current", "observation_scope"}))
        for kind, name, keys in cases:
            with self.subTest(kind=kind):
                row = self.source.row(kind=kind, name=name)
                self.assertEqual(set(row["identity"]), keys)
                self.assertIsNone(row["delete_argv"])
                self.assertIsNone(row["archive_argv"])
                self.assertTrue(stashes.holds(row["stash_observation"]))
                if kind in {"cache", "native"}:
                    self.assertEqual(row["archives"], [])
                    self.assertFalse(row["archive_verified"])

    def test_verified_branch_archive_is_preservation_only_in_project_only_mode(self):
        case = self.source.case
        path = case.root / "project-only.bundle"
        self.addCleanup(path.unlink)
        plans.git(case.project, "bundle", "create", str(path), self.source.ref, "refs/stash")
        refs = {ref: oid for oid, ref in (line.split() for line in plans.git(case.project, "bundle", "list-heads", str(path)).splitlines())}
        row = self.source.row(archives=[{"path": str(path), "ref": self.source.ref, "tip": case.base}])
        self.assertTrue(row["archive_verified"], row["archives"])
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["class"], "protected")

    def test_loose_ref_same_oid_replacement_and_log_only_change_invalidate_expect(self):
        ref = self.source.common / self.source.ref
        self.preserve(ref)
        first = self.source.observe()
        replacement = ref.with_name("replacement-fixture")
        replacement.write_bytes(ref.read_bytes())
        replacement.replace(ref)
        self.assert_changed(first)

    def test_selected_reflog_digest_changes_expect_without_changing_ref_oid(self):
        log = self.source.common / "logs" / self.source.ref
        self.preserve(log)
        first = self.source.observe()
        with log.open("ab") as output:
            output.write(f"{self.source.case.base} {self.source.case.base} Fixture <fixture@example.invalid> 1 +0000\tsynthetic\n".encode())
        self.assert_changed(first)

    def test_selected_branch_log_keeps_its_original_2048_entry_limit(self):
        log = self.source.common / "logs" / self.source.ref
        self.preserve(log)
        entry = log.read_bytes().splitlines(keepends=True)[-1]
        for count, status in ((2048, "observed"), (2049, "unavailable")):
            with self.subTest(entries=count):
                log.write_bytes(entry * count)
                row = self.source.row()
                self.assertEqual(row["identity"]["ref_storage"]["status"], status)
                self.assertEqual(row["identity"]["current"]["status"],
                                 "present" if status == "observed" else "unavailable")
                self.assertIsNone(row["delete_argv"])
                if status == "unavailable":
                    self.assertIn("resource_identity_unavailable", row["reasons"])

    def test_stash_storage_log_keeps_its_independent_2048_entry_limit(self):
        log = self.source.common / "logs" / "refs/stash"
        self.preserve(log)
        entry = log.read_bytes().splitlines(keepends=True)[-1]
        repository = archives.repository_observation(self.source.case.project)
        for count, status in ((2048, "observed"), (2049, "unavailable")):
            with self.subTest(entries=count):
                raw = entry * count
                log.write_bytes(raw)
                stored = observations.ref_storage(self.source.case.project, "refs/stash", repository)
                self.assertEqual(stored["status"], status)
                self.assertEqual(stored["reflog"]["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(stored["freshness"]["stable"], status == "observed")

    def test_recycled_ref_ancestor_inode_with_retained_leaf_invalidates_old_expect(self):
        source, case = self.source, self.source.case
        ref = "refs/heads/ancestor/retained/selected"
        plans.git(case.project, "update-ref", "-m", "synthetic ref", ref, case.base)
        self.addCleanup(plans.git, case.project, "update-ref", "--no-deref", "-d", ref)
        first = source.observe(name=ref)
        ancestor = source.common / "refs/heads/ancestor"
        retained = ancestor / "retained"
        leaf = retained / "selected"
        original_parent, before = ancestor.stat(), archives._identity(leaf.stat())
        parked = case.root / "parked-ref-directory"
        retained.rename(parked)
        ancestor.rmdir()
        ancestor.mkdir()
        parked.rename(retained)
        self.assertEqual(archives._identity(leaf.stat()), before)
        self.assertNotEqual(ancestor.stat().st_ctime_ns, original_parent.st_ctime_ns)
        original = os.fstat
        def recycled(descriptor):
            current = original(descriptor)
            if os.readlink(f"/proc/self/fd/{descriptor}") == str(ancestor):
                fields = {key: getattr(current, key) for key in dir(current) if key.startswith("st_")}
                fields["st_ino"] = original_parent.st_ino
                return SimpleNamespace(**fields)
            return current
        with patch.object(observations.os, "fstat", side_effect=recycled):
            self.assert_changed(first, name=ref)

    def test_absent_ref_is_current_absence_only_and_same_name_recreation_has_new_expect(self):
        case = self.source.case
        self.addCleanup(plans.git, case.project, "update-ref", self.source.ref, case.base)
        subprocess.run(stashes.local_argv(case.project, self.source.ref, case.base), check=True, capture_output=True)
        absent = self.source.observe()
        row = absent["resources"][0]
        self.assertEqual(row["identity"]["current"], {"status": "absent", "path": None, "ref": self.source.ref, "oid": None})
        self.assertIn("resource_absent", row["reasons"])
        self.assertIsNone(row["delete_argv"])
        plans.git(case.project, "update-ref", self.source.ref, case.base)
        self.assert_changed(absent)

    def test_packed_file_changes_expect_even_with_same_selected_loose_oid(self):
        packed = self.source.common / "packed-refs"
        self.preserve(packed)
        first = self.source.observe()
        plans.git(self.source.case.project, "pack-refs", "--all", "--no-prune")
        self.assert_changed(first)

    def test_duplicate_packed_selected_entry_is_unavailable_not_absent(self):
        packed = self.source.common / "packed-refs"
        self.preserve(packed)
        packed.write_text(f"# pack-refs with: sorted\n{self.source.case.base} {self.source.ref}\n{self.source.case.base} {self.source.ref}\n")
        row = self.source.row()
        self.assertEqual(row["identity"]["ref_storage"]["status"], "unavailable")
        self.assertEqual(row["identity"]["current"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])

    def test_symlink_storage_slot_is_unavailable_and_not_resolved_into_another_ref(self):
        ref = self.source.common / self.source.ref
        self.preserve(ref)
        retained = self.source.case.root / "retained-ref"
        retained.write_bytes(ref.read_bytes())
        self.addCleanup(retained.unlink)
        ref.unlink()
        ref.symlink_to(retained)
        self.addCleanup(ref.unlink, missing_ok=True)
        row = self.source.row()
        self.assertEqual(row["identity"]["ref_storage"]["status"], "unavailable")
        self.assertIsNone(row["delete_argv"])

    def test_stash_log_and_holder_races_keep_unknown_and_change_current_expect(self):
        # The racing terminal belongs to the deleted name's own checkout, a relevant holder.
        first = self.source.observe(name="refs/heads/live")
        original = self.source.reader
        calls = 0
        def changing(argv, **kwargs):
            nonlocal calls
            if argv[:2] == ["terminal", "show"]:
                calls += 1
                if calls >= 2:
                    self.source.unknown_holder = True
            return original(argv, **kwargs)
        with patch.object(self.source, "reader", side_effect=changing):
            row = self.source.row(name="refs/heads/live")
        self.assertFalse(row["stash_observation"]["freshness"]["stable"])
        self.assertIn("holder_changed", row["stash_observation"]["freshness"]["reasons"])
        self.assertIsNone(row["delete_argv"])
        self.assert_changed(first, name="refs/heads/live")

    def test_synthetic_config_environment_and_native_payload_values_never_leave_producer(self):
        config = self.source.common / "config"
        self.preserve(config)
        marker = "synthetic-nonsecret-producer-fixture"
        plans.git(self.source.case.project, "config", "credential.fixture", marker)
        self.source.extra = {"providerPrompt": marker, "privatePayload": {"value": marker}}
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.value", "GIT_CONFIG_VALUE_0": marker}):
            result = self.source.observe()
        self.assertNotIn(marker, json.dumps(result))
        self.assertTrue(result["resources"][0]["stash_observation"]["support"]["git_context_sha256"])

    def test_changed_hashed_context_keeps_booleans_but_invalidates_expect(self):
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.value", "GIT_CONFIG_VALUE_0": "one"}):
            first = self.source.observe()
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.value", "GIT_CONFIG_VALUE_0": "two"}):
            self.assert_changed(first)


class SelectedCacheBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = stash_tests.SourceFixture()
        cls.addClassCleanup(cls.source.close)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / "ancestor"
        self.selected = self.parent / "selected"
        self.selected.mkdir(parents=True)
        self.selection = {"kind": "cache", "name": str(self.selected)}

    def replace_parent(self):
        self.parent.rename(self.root / "parked")
        self.selected.mkdir(parents=True)

    def assert_held(self, row, status):
        self.assertEqual(row["identity"]["current"]["status"], status)
        if status != "present":
            self.assertIsNone(row["identity"]["filesystem"])
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["identity"]["provenance"]["status"], "unavailable")

    def test_public_cache_refuses_detached_ancestor_identity(self):
        before = self.selected.stat()
        with plans.path_change_on_fstat(self.parent, self.replace_parent) as changes:
            row = self.source.row(**self.selection)
        self.assertTrue(changes)
        self.assertNotEqual(self.selected.stat().st_ino, before.st_ino)
        self.assert_held(row, "unavailable")

    def test_public_cache_replacement_refuses_same_stale_expect(self):
        first = self.source.observe(**self.selection)
        self.assert_held(first["resources"][0], "present")
        with plans.current_resources() as rows, \
                plans.path_change_on_fstat(self.parent, self.replace_parent) as changes, \
                self.assertRaises(PodError) as caught:
            self.source.observe(**self.selection, expect=first["expect"])
        self.assertTrue(changes)
        self.assertEqual(caught.exception.code, "cleanup_changed")
        self.assert_held(rows[0], "unavailable")

    def test_public_cache_unrelated_ancestor_entries_accept_same_expect(self):
        first = self.source.observe(**self.selection)
        with plans.directory_entry_changes(self.parent) as changes:
            second = self.source.observe(**self.selection, expect=first["expect"])
        self.assertTrue(changes)
        self.assertEqual(second["expect"], first["expect"])
        self.assert_held(second["resources"][0], "present")

    def test_public_cache_genuine_absence_then_appearance_changes_expect(self):
        self.selected.rmdir()
        first = self.source.observe(**self.selection)
        self.assert_held(first["resources"][0], "absent")
        self.selected.mkdir()
        with self.assertRaises(PodError) as caught:
            self.source.observe(**self.selection, expect=first["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")
        self.assert_held(self.source.row(**self.selection), "present")

    def test_public_cache_absence_under_replaced_ancestor_refuses_old_expect(self):
        self.selected.rmdir()
        first = self.source.observe(**self.selection)
        self.assert_held(first["resources"][0], "absent")
        def replace():
            self.parent.rename(self.root / "parked")
            self.parent.mkdir()
        with plans.current_resources() as rows, \
                plans.path_change_on_fstat(self.parent, replace) as changes, \
                self.assertRaises(PodError) as caught:
            self.source.observe(**self.selection, expect=first["expect"])
        self.assertTrue(changes)
        self.assertFalse(self.selected.exists())
        self.assertEqual(caught.exception.code, "cleanup_changed")
        self.assert_held(rows[0], "unavailable")

    def test_public_cache_appearance_during_absence_read_refuses_old_expect(self):
        self.selected.rmdir()
        first = self.source.observe(**self.selection)
        self.assert_held(first["resources"][0], "absent")
        with plans.current_resources() as rows, \
                plans.path_change_on_fstat(self.parent, self.selected.mkdir, occurrence=2) as changes, \
                self.assertRaises(PodError) as caught:
            self.source.observe(**self.selection, expect=first["expect"])
        self.assertTrue(changes)
        self.assertTrue(self.selected.is_dir())
        self.assertEqual(caught.exception.code, "cleanup_changed")
        self.assert_held(rows[0], "unavailable")


class SelectedWorkerObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = plans.CleanupWorkerTests()
        cls.case._build_fixture()
        cls.addClassCleanup(cls.case.doCleanups)

    def selected(self, kind, name, **kwargs):
        return plan(self.case.project, "objective", worker={"dispatch": self.case.dispatch},
                    observe={"kind": kind, "name": name}, orca_reader=self.case.orca_read,
                    native_port=self.case.port, **kwargs)

    def test_incompatible_worker_selector_cannot_expand_to_foreign_local_ref(self):
        with self.assertRaises(PodError) as caught:
            self.selected("local_branch", "refs/heads/main")
        self.assertEqual(caught.exception.code, "invalid_cleanup_observe")

    def test_remote_branch_suffix_is_not_exact_worker_scope(self):
        with self.assertRaises(PodError) as caught:
            self.selected("remote_branch", "origin/other/worker")
        self.assertEqual(caught.exception.code, "invalid_cleanup_observe")

    def test_supported_native_readback_is_sanitized_current_data_and_no_release_command(self):
        marker = "synthetic-nonsecret-native-payload"
        original = self.case.port.show_worker
        def shown(dispatch):
            value = original(dispatch)
            value["result"]["worker"]["providerPrompt"] = marker
            value["result"]["projection"]["liveness"]["privatePayload"] = marker
            return value
        with patch.object(self.case.port, "show_worker", side_effect=shown):
            result = self.selected("native", self.case.dispatch)
        row = result["resources"][0]
        self.assertEqual(row["identity"]["provenance"], {"status": "observed", "source": "admission", "reference": self.case.admission["admission_id"]})
        self.assertEqual(row["identity"]["current"]["status"], "present")
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        self.assertEqual(row["class"], "protected")
        self.assertNotIn(marker, json.dumps(result))

    def test_previous_worktree_expect_cannot_be_used_for_current_remaining_branch(self):
        old = self.selected("worktree", str(self.case.child))
        row = old["resources"][0]
        self.assertEqual(row["kind"], "worktree")
        with self.assertRaises(PodError) as caught:
            self.selected("local_branch", "refs/heads/worker", expect=old["expect"])
        self.assertEqual(caught.exception.code, "cleanup_changed")


if __name__ == "__main__":
    unittest.main()

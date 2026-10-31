"""One physical writer, immutable page reuse and reader-safe reachability GC."""
from copy import deepcopy
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import _empty_journal, _read_journal, _write_journal
from pod.history import (ChunkStore, STORAGE_SCHEMA, audit_context, load_context,
                         prepare_context, publish_context)
from pod.history_tree import open_root, plain, plan
from pod.ledger import _write
from pod.util import encoded
from tests.history_support import compact, publish_preceding, synthetic_state
from tests.kernel_support import KernelCase


class ReclamationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = synthetic_state(4)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.json"
        self.store = ChunkStore(self.path.parent)

    def files(self):
        return {p.name: (p.stat().st_ino, p.read_bytes()) for p in self.store.root.glob("*.json")}

    def write(self, **changes):
        value = load_context(self.path)
        value["source_rejections"].update(changes)
        _write(self.path, value)
        value.reader.close()
        self.store.collect()

    def test_new_and_preceding_contexts_use_the_same_only_writer(self):
        state = deepcopy(self.state)
        _write(self.path, state)
        self.assertEqual(json.loads(self.path.read_bytes())["schema"], STORAGE_SCHEMA)
        old, chunks = compact(self.state, force=True)
        publish_preceding(self.path, old, chunks)
        _write(self.path, load_context(self.path))
        self.assertEqual(json.loads(self.path.read_bytes())["schema"], STORAGE_SCHEMA)
        self.assertEqual(audit_context(self.path)["state"]["checkpoint"], self.state["checkpoint"])

    def test_first_governor_write_and_context_write_share_one_store_and_gc_roots(self):
        _write(self.path, deepcopy(self.state))
        journal_path = self.path.with_name("governor.json")
        _write_journal(journal_path, _empty_journal())
        governor = json.loads(journal_path.read_bytes())
        root = governor["root"]["ref"]["sha256"]
        self.write(fixture="new observation")
        self.assertTrue((self.store.root / (root + ".json")).is_file())
        self.assertEqual(_read_journal(journal_path)["revision"], 1)
        self.assertEqual(governor["schema"], STORAGE_SCHEMA)

    def test_successful_switch_reclaims_preceding_and_interrupted_orphan_chunks(self):
        old, chunks = compact(self.state, force=True)
        publish_preceding(self.path, old, chunks)
        orphan = encoded({"fixture": "unpublished observation"}, compact=True)
        address = hashlib.sha256(orphan).hexdigest()
        self.store.put({address: orphan})
        _write(self.path, load_context(self.path))
        self.store.collect()
        self.assertFalse((self.store.root / (address + ".json")).exists())
        self.assertTrue(set(chunks).isdisjoint({p.stem for p in self.store.root.glob("*.json")}))
        self.assertEqual(audit_context(self.path)["state"]["admissions"], self.state["admissions"])

    def test_old_reader_pins_survive_publication_and_last_release_collects(self):
        _write(self.path, deepcopy(self.state))
        old = load_context(self.path)
        old_root = json.loads(self.path.read_bytes())["root"]["ref"]["sha256"]
        self.write(fixture="accepted next generation")
        self.assertTrue((self.store.root / (old_root + ".json")).exists())
        self.assertEqual(plain(old)["admissions"], self.state["admissions"])
        old.reader.close()
        self.assertTrue((self.store.root / (old_root + ".json")).exists())
        self.store.collect()  # Next mutating operation retries; read-only close does not write.
        self.assertFalse((self.store.root / (old_root + ".json")).exists())
        self.assertEqual(audit_context(self.path)["state"]["source_rejections"]["fixture"],
                         "accepted next generation")

    def test_corrupt_or_missing_live_root_aborts_sweep_before_any_unlink(self):
        _write(self.path, deepcopy(self.state))
        root = json.loads(self.path.read_bytes())["root"]["ref"]["sha256"]
        target = self.store.root / (root + ".json")
        original = target.read_bytes()
        orphan = encoded({"fixture": "orphan"}, compact=True)
        address = hashlib.sha256(orphan).hexdigest()
        self.store.put({address: orphan})
        target.unlink()
        self.store.request_collection(); self.store.collect()
        self.assertTrue((self.store.root / (address + ".json")).exists())
        target.write_bytes(original + b" ")
        self.store.collect()
        self.assertTrue((self.store.root / (address + ".json")).exists())
        target.write_bytes(original)
        self.store.collect()
        self.assertFalse((self.store.root / (address + ".json")).exists())
        self.assertTrue(audit_context(self.path)["complete"])

    def test_cleanup_failure_does_not_replay_or_undo_a_durable_publication(self):
        _write(self.path, deepcopy(self.state))
        before = self.path.read_bytes()
        with patch.object(ChunkStore, "_collect", side_effect=OSError(errno.EIO, "injected cleanup fault")):
            self.write(fixture="durable despite deferred cleanup")
        accepted = self.path.read_bytes()
        self.assertNotEqual(before, accepted)
        self.assertTrue((self.path.parent / ".history-gc").exists())
        self.store.collect()
        self.assertEqual(self.path.read_bytes(), accepted)
        self.assertFalse((self.path.parent / ".history-gc").exists())

    def test_unconfirmed_switch_cannot_reclaim_until_the_root_directory_is_durable(self):
        import os
        _write(self.path, deepcopy(self.state))
        old_root = json.loads(self.path.read_bytes())["root"]["ref"]["sha256"]
        self.store.request_collection()
        inode = self.path.parent.stat().st_ino
        real_sync = os.fsync
        def fail_root(fd):
            if os.fstat(fd).st_ino == inode:
                raise OSError(errno.EIO, "injected root sync failure")
            return real_sync(fd)
        value = load_context(self.path)
        value["source_rejections"]["fixture"] = "unconfirmed until readback"
        with patch("pod.history.os.fsync", side_effect=fail_root):
            with self.assertRaises(PodError) as caught: _write(self.path, value)
            self.assertEqual(caught.exception.code, "storage_publication_unconfirmed")
            value.reader.close()
            self.assertTrue((self.store.root / (old_root + ".json")).exists())
        self.store.collect()
        self.assertFalse((self.store.root / (old_root + ".json")).exists())
        self.assertEqual(audit_context(self.path)["state"]["source_rejections"]["fixture"],
                         "unconfirmed until readback")

    def test_unchanged_pages_keep_exact_bytes_and_inode_across_checkpoints(self):
        _write(self.path, deepcopy(self.state))
        before = self.files()
        initial = sum(len(raw) for _, raw in before.values())
        for _ in range(3): self.write()
        after = self.files()
        shared = set(before) & set(after)
        self.assertGreater(len(shared), 2)
        self.assertTrue(all(before[name] == after[name] for name in shared))
        self.assertLessEqual(sum(len(raw) for _, raw in after.values()) - initial, 1024)
        self.assertFalse((self.path.parent / ".history-gc").exists())

    def test_one_writer_can_publish_two_phases_but_an_independent_snapshot_stays_stale(self):
        _write(self.path, deepcopy(self.state))
        value, stale = load_context(self.path), load_context(self.path)
        value["source_rejections"]["first"] = "phase one"
        _write(self.path, value)
        value["source_rejections"]["second"] = "phase two"
        _write(self.path, value)
        accepted = self.path.read_bytes()
        with self.assertRaises(PodError) as caught: _write(self.path, stale)
        self.assertEqual(caught.exception.code, "map_stale")
        self.assertEqual(self.path.read_bytes(), accepted)
        self.assertEqual(audit_context(self.path)["state"]["source_rejections"],
                         {"first": "phase one", "second": "phase two"})

    def test_publisher_refuses_a_second_physical_representation(self):
        old, chunks = compact(self.state, force=True)
        with self.assertRaises(PodError) as caught: publish_context(self.path, old, chunks)
        self.assertEqual(caught.exception.code, "storage_format_invalid")
        self.assertFalse(self.path.exists())

    def test_routine_writes_queue_cleanup_without_scanning_retained_pages(self):
        with patch.object(ChunkStore, "_collect", side_effect=AssertionError("routine full scan")):
            _write(self.path, deepcopy(self.state))
            value = load_context(self.path)
            _write(self.path, value)
            value.reader.close()
        info = self.store.cleanup_info()
        self.assertTrue(info["pending"])
        self.assertGreater(info["queued_bytes"], 0)
        self.assertGreater(len(info["roots"]), 0)
        self.assertEqual(self.store.collect()["status"], "collected")

    def test_identical_replanned_pages_do_not_spend_the_cleanup_queue(self):
        _write(self.path, deepcopy(self.state))
        self.store.collect()
        with patch("pod.history.CLEANUP_TRIGGER_BYTES", 64 * 1024), \
             patch.object(ChunkStore, "_collect", side_effect=AssertionError("duplicate pages triggered a sweep")):
            for _ in range(3):
                _write(self.path, plain(load_context(self.path)))
        self.assertLess(self.store.cleanup_info()["queued_bytes"], 64 * 1024)

    def test_failed_automatic_sweep_does_not_scan_again_until_another_batch(self):
        _write(self.path, deepcopy(self.state))
        self.store.collect()
        from pod.history import CLEANUP_TRIGGER_BYTES
        self.store.request_collection(queued_bytes=CLEANUP_TRIGGER_BYTES)
        with patch.object(ChunkStore, "_collect", side_effect=PodError("history_deadline", "injected cleanup budget")) as sweep:
            self.assertEqual(self.store.maybe_collect()["reason"], "history_deadline")
            value = load_context(self.path)
            _write(self.path, value)
            value.reader.close()
            self.assertEqual(sweep.call_count, 1)
            self.assertEqual(self.store.collect()["reason"], "history_deadline")
            self.assertEqual(sweep.call_count, 2)

    def test_threshold_cleanup_runs_after_the_semantic_record_lock_is_released(self):
        import fcntl
        from pod.ledger import _lock
        observed = []
        collect = self.store._collect
        def outside(**kwargs):
            with self.path.with_name(".lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            observed.append(True)
            return collect(**kwargs)
        with patch("pod.history.CLEANUP_TRIGGER_BYTES", 1), patch.object(ChunkStore, "_collect", side_effect=outside):
            with _lock(self.path):
                _write(self.path, deepcopy(self.state))
                self.assertEqual(observed, [])
        self.assertEqual(observed, [True])

    def test_admission_effects_pin_precedes_primary_read(self):
        import fcntl
        import os
        from pod import history
        from pod.governor import load_admission_effects
        journal = self.path.with_name("governor.json")
        _write_journal(journal, _empty_journal())
        original = history._context_bytes
        observed = []
        def pinned(path, limit):
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(True)
            finally: os.close(fd)
            return original(path, limit)
        with patch("pod.history._context_bytes", side_effect=pinned):
            self.assertTrue(load_admission_effects(journal)["complete"])
        self.assertEqual(observed, [True])

    def test_cleanup_inventory_reads_only_the_bounded_hint(self):
        from pod.history import cleanup_inventory
        parent = self.path.parent / "state"
        target = parent / ("f" * 64)
        target.mkdir(parents=True)
        queued = ChunkStore(target)
        queued.request_collection(queued_bytes=1200, replaced=["a" * 64])
        with patch.object(ChunkStore, "load", side_effect=AssertionError("inventory opened history")):
            rows = cleanup_inventory(parent)
        self.assertEqual(rows, [{"record": "f" * 64, "queued_bytes": 1200, "untracked": False}])


class MaintenanceEntryTests(KernelCase):
    def test_preceding_compaction_entry_is_read_only_and_names_the_source_bound_route(self):
        from pod.internal import run
        from pod.ledger import objective_root
        from tests.common import authority_files
        self.intake()
        root = objective_root(self.project, "objective")
        before = authority_files(root)
        for field in ("request", "decision"):
            with self.subTest(field=field), self.assertRaises(PodError) as caught:
                run("record-compaction", {"project": str(self.project), "objective": "objective", field: {}})
            self.assertEqual(caught.exception.code, "storage_action_required")
            self.assertIn("storage inspect then migrate", str(caught.exception))
            self.assertEqual(authority_files(root), before)


class UnchangedValueTests(unittest.TestCase):
    def test_tracking_keeps_distinct_json_scalars_and_unchanged_subtree_identity(self):
        root, chunks = plan({"truth": True, "zero": -0.0, "items": [1, 2]}, force_root=True)
        class Store:
            def load(self, address): return chunks[address]
        value = open_root(root, Store())
        value["truth"] = True
        self.assertFalse(value.dirty)
        value["truth"] = 1
        value["zero"] = 0.0
        changed, added = plan(value)
        class After:
            def load(self, address): return (chunks | added)[address]
        result = plain(open_root(changed, After()))
        self.assertIs(type(result["truth"]), int)
        self.assertEqual(encoded(result["zero"], compact=True), b"0.0\n")

"""R121 storage, immutable evidence and complete logical projection invariants."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from pod.errors import PodError
from pod.ledger import _empty, _outstanding_ids
from pod.util import encoded
from pod.history import (CHUNK_LIMIT, COMPACT_AT, CONTEXT_LIMIT, FORMAT, MARKER, PART_BYTES,
                     ChunkStore, compact, hydrate, is_reference)



def size(value):
    return len(encoded(value, compact=True))


from tests.history_support import synthetic_state


class SidecarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = synthetic_state()  # >=60-admission fixture built once.
        cls.state["admissions"]["attempt-052"].update(state="reserved", native_binding=None, report=None)
        cls.state["admissions"]["attempt-053"].update(state="unresolved", native_binding=None, report=None)
        cls.state["admissions"]["attempt-054"]["report"]["outcome"] = "failed"
        cls.state["admissions"]["attempt-055"]["report"]["outcome"] = "UNKNOWN"
        cls.state["admissions"]["attempt-056"]["report"] = None
        cls.state["admissions"]["attempt-057"]["candidate"] = "f" * 40
        cls.state["admissions"]["attempt-058"].update(role="review", serves=["A0"])
        cls.state["admissions"]["attempt-059"]["failures"] = [{"kind": "unavailable"}]
        cls.protected = {"attempt-048", "attempt-049", "attempt-050", "attempt-051"}
        cls.record, cls.chunks = compact(cls.state, protected_admissions=cls.protected)
        cls.restored = hydrate(cls.record, cls.chunks.__getitem__)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.store = ChunkStore(cls.temporary.name)
        cls.store.put(cls.chunks)
        print(json.dumps({"fixture_admissions": len(cls.state["admissions"]),
                          "candidates": len(cls.state["checkpoint"]["governance_history"]["candidates"]),
                          "logical_bytes": size(cls.state), "record_bytes": size(cls.record),
                          "context_limit": CONTEXT_LIMIT, "chunk_files": len(cls.chunks),
                          "largest_chunk": max(map(len, cls.chunks.values())),
                          "archived_admissions": sum(is_reference(row) for row in cls.record["admissions"].values())},
                         sort_keys=True))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_exact_roundtrip_and_input_immutability(self):
        self.assertEqual(self.state, self.restored)
        self.assertEqual(encoded(self.state, compact=True), encoded(self.restored, compact=True))
        self.assertNotIn("history", self.state)
        self.assertEqual(list(self.state["admissions"]), list(self.restored["admissions"]))

    def test_large_record_fits_well_under_limit(self):
        self.assertGreater(size(self.state), CONTEXT_LIMIT)
        self.assertGreaterEqual(len(self.state["admissions"]), 60)
        self.assertLess(size(self.record), CONTEXT_LIMIT // 2)
        self.assertLessEqual(max(map(len, self.chunks.values())), CHUNK_LIMIT)

    def test_unsettled_and_current_items_stay_inline(self):
        kept = [*self.protected, *(f"attempt-{i:03}" for i in range(52, 60))]
        for key in kept:
            with self.subTest(key=key):
                self.assertFalse(is_reference(self.record["admissions"][key]))
                self.assertEqual(self.state["admissions"][key], self.record["admissions"][key])

    def test_implementation_needs_disposition_and_review_needs_consumption(self):
        state = deepcopy(self.state)
        state["admissions"]["attempt-042"]["disposition"] = None
        state["checkpoint"]["obligations"][0]["receipts"] = []
        record, chunks = compact(state)
        self.assertFalse(is_reference(record["admissions"]["attempt-042"]))
        self.assertFalse(is_reference(record["admissions"]["attempt-000"]))
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_selected_and_reused_receipts_stay_inline(self):
        state = deepcopy(self.state)
        ob = state["checkpoint"]["obligations"][0]
        ob["evidence"] = [deepcopy(ob["receipts"][0]["evidence"])]
        ob["receipts"][1]["reuse"] = {"from": "synthetic-old", "to": "synthetic-current"}
        record, chunks = compact(state)
        after = record["checkpoint"]["obligations"][0]
        self.assertEqual(after["receipts"][:2], ob["receipts"][:2])
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_only_old_unselected_accepted_pass_reuse_receipts_move(self):
        state = deepcopy(self.state)
        receipt = {"evidence": {"reference": "old-log", "candidate": "superseded", "status": "PASS",
                                "result": "synthetic check result; " * 40},
                   "accepted_seq": 3, "reuse": {"from": "superseded", "to": "later", "delta": ["src/new.py"]}}
        receipts = [deepcopy(receipt) for _ in range(7)]
        for index, row in enumerate(receipts):
            row["evidence"]["reference"] = f"log-{index}"
        receipts[1]["evidence"]["candidate"] = state["checkpoint"]["candidate"]
        receipts[3]["evidence"]["status"] = "FAILED"
        receipts[4]["evidence"]["status"] = "UNKNOWN"
        receipts[5].pop("accepted_seq")
        receipts[6]["evidence"].pop("candidate")
        ob = {"id": "ordinary", "kind": "criterion", "receipts": receipts,
              "evidence": [deepcopy(receipts[2]["evidence"])]}
        state["checkpoint"]["obligations"].append(ob)
        record, chunks = compact(state)
        after = record["checkpoint"]["obligations"][-1]["receipts"]
        self.assertTrue(is_reference(after[0]))
        self.assertEqual(after[1:], receipts[1:])
        self.assertEqual(hydrate(record, chunks.__getitem__), state)
        for options in ({"protected_candidates": ["superseded"]}, {"protect_receipts": True}):
            with self.subTest(options=options):
                protected, _ = compact(state, **options)
                self.assertEqual(protected["checkpoint"]["obligations"][-1]["receipts"], receipts)

    def test_unknown_route_and_failed_receipts_stay_inline(self):
        state = deepcopy(self.state)
        state["admissions"]["attempt-048"]["route_decision"]["effective_unknown"] = True
        state["admissions"]["attempt-049"]["route_decision"]["route_mismatch"] = True
        receipts = [{"evidence": {"reference": str(i), "candidate": "superseded", "status": outcome},
                     "accepted_seq": 1} for i, outcome in enumerate(("pending", "UNKNOWN", "FAILED"))]
        state["checkpoint"]["obligations"].append({"id": "O1", "kind": "criterion", "receipts": receipts})
        record, chunks = compact(state)
        for key in ("attempt-048", "attempt-049"):
            self.assertEqual(record["admissions"][key], state["admissions"][key])
        self.assertEqual(record["checkpoint"]["obligations"][-1]["receipts"], receipts)
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_native_settlement_and_production_projections_are_identical(self):
        from pod.governor import _phase, finalization
        from pod.obligations import quiescence
        from pod.readiness import base
        from pod.listing import _facts
        from pod.retirement import counts
        journal = {"actions": [{"record_id": "synthetic-gov", "decision": "ALLOW", "outcome": "UNKNOWN",
                                "action": {"kind": "workflow_dispatch", "candidate": "synthetic"}}]}
        native = {"assignments": [], "runtime": "synthetic-runtime"}
        for key, row in self.state["admissions"].items():
            binding = row["native_binding"]
            if binding:
                native["assignments"].append({"admission_id": key, "settled": key not in self.protected,
                    "runtime": row["runtime"], "run_id": binding["runId"], "task_id": binding["taskId"],
                    "dispatch_id": binding["dispatchId"], "worker_id": binding["workerId"]})
        def projections(state):
            outstanding = _outstanding_ids(state, native)
            preserved = [{"kind": "admission", "state": row["state"], "reported": row.get("report") is not None}
                         for row in state["admissions"].values()]
            return {"outstanding": outstanding, "without_native": _outstanding_ids(state, None),
                    "finalization": finalization(state, journal), "readiness": base(state, journal, outstanding=set(outstanding)),
                    "quiescence": quiescence(state["checkpoint"]), "locations": _facts(state),
                    "retirement_counts": counts(preserved),
                    "governor_phase": _phase(state, journal["actions"], None, "current", {"outstanding": []})}
        self.assertEqual(encoded(projections(self.state), compact=True), encoded(projections(self.restored), compact=True))

    def test_real_closed_snapshot_contract_is_identical(self):
        from pod.obligations import accept_write, report_projection, validate_closed_snapshot
        from tests.test_obligations import ctx, criterion, proof
        first = accept_write(None, {"governance": {"base_ref": None}, "obligations": [criterion()]}, ctx(candidate="c0"))
        first = accept_write(first, {"obligations": [criterion("satisfied", evidence=[proof("O1", "c0")])]}, ctx(candidate="c0"))
        second = accept_write(first, {"obligations": [criterion("satisfied", evidence=[proof("O1", "c1")])], "close": True}, ctx())
        state = _empty()
        state["checkpoint"] = {**second, "candidate": "c1"}
        record, chunks = compact(state, force=True)
        self.assertTrue(chunks)
        restored = hydrate(record, chunks.__getitem__)
        self.assertEqual(validate_closed_snapshot(second, ctx()), ())
        self.assertEqual(validate_closed_snapshot(restored["checkpoint"], ctx()), ())
        self.assertEqual(report_projection(second, ctx()), report_projection(restored["checkpoint"], ctx()))
        self.assertEqual(second["closure"], restored["checkpoint"]["closure"])

    def test_sixty_four_receipt_logical_limit_refuses_sixty_fifth(self):
        from pod.assurance import _stamp_evidence
        from tests.test_obligations import ctx, proof
        receipts = [{"evidence": proof("O1", "c0", reference=f"proof-{i}"), "accepted_seq": 1} for i in range(64)]
        state = _empty()
        ob = {"id": "O1", "kind": "criterion", "evidence": [], "receipts": receipts}
        state["checkpoint"] = {"candidate": "c1", "obligations": [ob]}
        record, chunks = compact(state, force=True)
        restored = hydrate(record, chunks.__getitem__)["checkpoint"]["obligations"][0]
        self.assertEqual(len(restored["receipts"]), 64)
        for prior in (ob, restored):
            new = {**deepcopy(prior), "evidence": [proof("O1", reference="sixty-fifth")]}
            with self.assertRaises(PodError) as caught:
                _stamp_evidence(new, prior, ctx(), "1" * 64, False, seq=20)
            self.assertEqual(caught.exception.detail["detail"], "receipt_limit")

    def test_store_roundtrip_and_write_once(self):
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.store.root.glob("*.json")}
        self.store.put(self.chunks)
        self.assertEqual(hydrate(self.record, self.store.load), self.state)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.store.root.glob("*.json")}
        self.assertEqual(before, after)

    def test_missing_chunk_refuses(self):
        def missing(_address):
            raise FileNotFoundError
        with self.assertRaises(PodError) as caught:
            hydrate(self.record, missing)
        self.assertEqual(caught.exception.code, "history_missing")

    def test_tampered_chunk_refuses(self):
        with self.assertRaises(PodError) as caught:
            hydrate(self.record, lambda address: self.chunks[address] + b" ")
        self.assertEqual(caught.exception.code, "history_integrity")

    def test_reference_tampering_and_path_traversal_refuse(self):
        for field, value in (("sha256", "a" * 64), ("parts", ["../context"]), ("bytes", 1)):
            with self.subTest(field=field):
                record = deepcopy(self.record)
                record["admissions"]["attempt-000"][MARKER][field] = value
                with self.assertRaises(PodError) as caught:
                    hydrate(record, self.chunks.__getitem__)
                self.assertIn(caught.exception.code, ("history_invalid", "history_integrity"))

    def test_symlink_store_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "other"
            target.mkdir()
            (Path(directory) / "history").symlink_to(target, target_is_directory=True)
            with self.assertRaises(PodError) as caught:
                ChunkStore(directory).put(self.chunks)
            self.assertEqual(caught.exception.code, "history_unsafe")
            self.assertEqual(list(target.iterdir()), [])

    def test_tampered_existing_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChunkStore(directory)
            address = next(iter(self.chunks))
            store.put({address: self.chunks[address]})
            path = store.root / (address + ".json")
            path.write_bytes(b"tampered")
            with self.assertRaises(PodError) as caught:
                store.put({address: self.chunks[address]})
            self.assertEqual(caught.exception.code, "history_integrity")
            self.assertEqual(path.read_bytes(), b"tampered")

    def test_unreferenced_crash_orphans_are_not_read_or_deleted(self):
        orphan = self.store.root / ("0" * 64 + ".json")
        orphan.write_bytes(b"synthetic-unreferenced-orphan")
        try:
            self.assertEqual(hydrate(self.record, self.store.load), self.state)
            self.store.put(self.chunks)
            self.assertEqual(orphan.read_bytes(), b"synthetic-unreferenced-orphan")
        finally:
            orphan.unlink()

    def test_large_single_item_is_split_into_bounded_immutable_files(self):
        state = _empty()
        row = deepcopy(self.state["admissions"]["attempt-048"])
        row["recovery"] = {"payload": "synthetic-value-" * 50000}
        state.update(admissions={"big": row}, checkpoint={"candidate": "new"})
        record, chunks = compact(state, force=True)
        self.assertGreater(len(chunks), 2)
        self.assertLessEqual(max(map(len, chunks.values())), CHUNK_LIMIT)
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_small_legacy_state_keeps_exact_shape(self):
        state = _empty()
        record, chunks = compact(state)
        self.assertEqual(record, state)
        self.assertEqual(chunks, {})
        self.assertNotIn("history", record)
        self.assertEqual(hydrate(record, lambda _: self.fail("unexpected read")), state)

    def test_full_record_retirement_entry_pointer_fits(self):
        state = _empty()
        # Wholly unsettled state stays inline; retirement itself is immutable history.
        state["source_rejections"] = {"synthetic": "x" * (CONTEXT_LIMIT - 1200)}
        state["historical_retirement"] = {"synthetic_entry": "retirement-evidence-" * 1400}
        with self.assertRaises(PodError) as caught:
            compact(state, force=True)
        self.assertEqual(caught.exception.code, "record_too_large")
        record, chunks = compact(state, force=True, archive_retirement=True)
        self.assertLess(size(record), CONTEXT_LIMIT)
        self.assertEqual(record["source_rejections"], state["source_rejections"])
        self.assertTrue(is_reference(record["historical_retirement"]))
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_impossible_fit_refuses_with_largest_sections(self):
        state = _empty()
        state["source_rejections"] = {"unsettled": "x" * CONTEXT_LIMIT}
        with self.assertRaises(PodError) as caught:
            compact(state, force=True, archive_retirement=True)
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertEqual(caught.exception.detail["largest_sections"][0]["section"], "source_rejections")



if __name__ == "__main__":
    unittest.main(verbosity=2)


class HistoryIntegrityTests(unittest.TestCase):
    def test_pointer_shaped_user_evidence_is_never_expanded(self):
        state = _empty()
        state["source_rejections"] = {"worker-text": {MARKER: {"parts": ["../anything"]}}}
        self.assertEqual(hydrate(state, lambda _: self.fail("arbitrary user content read")), state)

    def test_utf8_fragments_roundtrip_after_byte_reassembly(self):
        state = synthetic_state(count=1)
        state["admissions"]["attempt-000"]["role"] = "investigate"
        state["admissions"]["attempt-000"]["recovery"] = {"utf8": "雪🌊" * 20000}
        record, chunks = compact(state, force=True)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_recursive_pointer_and_bad_part_count_refuse(self):
        from pod.history import _reference
        chunks = {}
        pointer = _reference({MARKER: {"sha256": "0" * 64, "parts": ["0" * 64], "bytes": 1}}, chunks)
        state = _empty()
        state.update(history=FORMAT, admissions={"nested": pointer})
        with self.assertRaises(PodError) as caught:
            hydrate(state, chunks.__getitem__)
        self.assertEqual(caught.exception.code, "history_integrity")
        pointer[MARKER]["parts"] *= 2
        with self.assertRaises(PodError) as caught:
            hydrate(state, chunks.__getitem__)
        self.assertEqual(caught.exception.code, "history_invalid")

    def test_nonregular_and_oversized_chunks_are_never_read_as_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChunkStore(directory)
            store.root.mkdir(parents=True)
            address = "0" * 64
            path = store.root / (address + ".json")
            path.mkdir()
            with self.assertRaises(PodError) as caught:
                store.load(address)
            self.assertEqual(caught.exception.code, "history_unsafe")
            path.rmdir()
            path.write_bytes(b"x" * (CHUNK_LIMIT + 1))
            with self.assertRaises(PodError) as caught:
                store.load(address)
            self.assertEqual(caught.exception.code, "history_integrity")

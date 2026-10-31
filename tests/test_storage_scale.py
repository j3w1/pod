"""R180 revision storage: synthetic scale and real mutation boundaries, no live proof."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import (ChunkStore, CONTEXT_LIMIT, audit_context, capacity, load_context, migrate_context, prepare_context, publish_context)
from pod.history_tree import Reader, descriptor_size, open_root, plain, plan
from pod.ledger import _read, _write, objective_root
from pod.util import encoded
from tests.history_support import synthetic_state
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RevisionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value = {"current": {"state": "active", "proof": ["selected"]},
                     "retained": [{"identity": n, "sources": {"observations": "x" * 5000}}
                                  for n in range(700)]}
        cls.root, cls.chunks = plan(cls.value)

    def opened(self):
        class Store:
            def load(inner, address):
                raw = self.chunks[address]
                self.assertEqual(sha(raw), address)
                return raw
        return open_root(self.root, Store())

    def test_current_read_and_update_do_not_open_retained_payloads(self):
        value = self.opened()
        self.assertEqual(value["current"]["state"], "active")
        self.assertLess(value.reader.bytes, 10_000)
        value["current"]["state"] = "waiting"
        root, chunks = plan(value)
        self.assertLess(sum(map(len, chunks.values())), 10_000)
        self.assertEqual(descriptor_size(root), len(encoded({**self.value, "current": {
            "state": "waiting", "proof": ["selected"]}}, compact=True)))

    def test_full_audit_restores_every_value_and_order(self):
        self.assertEqual(plain(self.opened()), self.value)
        self.assertEqual(descriptor_size(self.root), len(encoded(self.value, compact=True)))
        self.assertEqual(json.loads(json.dumps(self.opened())), self.value)

    def test_verification_json_and_earlier_text_keep_exact_values(self):
        value = {"environment": {"schema": "synthetic-verification/v1", "facts": {
            "python": "3.14", "dependencies": {"fixture": "1"}}, "description": "synthetic host"},
            "earlier_environment": "unverified earlier prose"}
        root, chunks = plan(value)
        class Store:
            def load(inner, address): return chunks[address]
        self.assertEqual(plain(open_root(root, Store())), value)

    def test_nested_mutation_and_deepcopy_do_not_lose_writes(self):
        original = self.opened()
        value = deepcopy(original)
        value["current"]["proof"].append("new")
        value["retained"][10]["sources"]["observations"] = "corrected"
        root, chunks = plan(value)
        class Store:
            def load(inner, address): return (chunks | self.chunks)[address]
        after = plain(open_root(root, Store()))
        expected = deepcopy(self.value)
        expected["current"]["proof"].append("new")
        expected["retained"][10]["sources"]["observations"] = "corrected"
        self.assertEqual(after, expected)
        self.assertEqual(plain(original), self.value)

    def test_new_containers_keep_standard_alias_semantics_until_publication(self):
        value = self.opened()
        added = {"state": "active"}
        value["added"] = added
        added["state"] = "satisfied"
        item = {"id": "before"}
        value["current"]["proof"].append(item)
        item["id"] = "after"
        root, chunks = plan(value)
        class Store:
            def load(inner, address): return (chunks | self.chunks)[address]
        after = plain(open_root(root, Store()))
        self.assertEqual(after["added"], {"state": "satisfied"})
        self.assertEqual(after["current"]["proof"][-1], {"id": "after"})

    def test_budget_and_index_corruption_refuse_without_empty_projection(self):
        class Store:
            def load(inner, address): return self.chunks[address]
        reader = Reader(Store(), memory_limit=0)
        with self.assertRaises(PodError) as caught:
            plain(reader.value(self.root))
        self.assertEqual(caught.exception.code, "history_memory")
        corrupt = deepcopy(self.root)
        corrupt["ref"]["level"] = 63
        with self.assertRaises(PodError):
            plain(open_root(corrupt, Store()))

    def test_decision_state_binding_does_not_export_untouched_history(self):
        from pod.history_tree import state_digest
        value = self.opened()
        before = state_digest(value)
        self.assertEqual(value.reader.bytes, 0)
        changed = deepcopy(value)
        changed["current"]["state"] = "waiting"
        self.assertNotEqual(state_digest(changed), before)
        root, chunks = plan(changed)
        class Store:
            def load(inner, address): return (chunks | self.chunks)[address]
        self.assertEqual(state_digest(open_root(root, Store())), state_digest(changed))
        self.assertLess(changed.reader.bytes, 10_000)

    def test_streaming_reads_have_no_cumulative_sixty_four_mib_cliff(self):
        raw = b"x" * (128 * 1024)
        address = sha(raw)
        class Store:
            def load(inner, key): return raw
        reader = Reader(Store())
        for _ in range(520): reader.read_bytes(address)
        self.assertGreater(reader.bytes, 64 * 1024 * 1024)
        self.assertEqual(reader.nodes, 520)

    def test_memory_and_deadline_guards_measure_the_operation(self):
        class Store:
            def load(inner, key): raise AssertionError("must refuse before I/O")
        with patch("pod.history_tree._memory", return_value=(100, 100)):
            reader = Reader(Store(), memory_limit=50)
        with patch("pod.history_tree._memory", return_value=(200, 150)):
            with self.assertRaises(PodError) as caught: reader.read_bytes("a" * 64)
        self.assertEqual(caught.exception.code, "history_memory")
        reader = Reader(Store(), seconds=1)
        reader.elapsed = 2
        with patch("pod.history_tree.time.monotonic", return_value=100):
            with self.assertRaises(PodError) as caught: reader.read_bytes("a" * 64)
        self.assertEqual(caught.exception.code, "history_deadline")

    def test_lazy_reads_ignore_external_wait_and_charge_actual_storage_time(self):
        now = [0.0]
        raw = b'"verified"\n'
        address = hashlib.sha256(raw).hexdigest()
        class Store:
            def load(inner, key):
                now[0] += 0.25
                return raw
        with patch("pod.history_tree.time.monotonic", side_effect=lambda: now[0]):
            reader = Reader(Store(), seconds=1)
            self.assertEqual(reader.read_bytes(address), raw)
            now[0] += 31  # Orca/GitHub elapsed time between lazy reads.
            self.assertEqual(reader.read_bytes(address), raw)
            self.assertEqual(reader.elapsed, 0.5)
            now[0] += 300
            reader.read_bytes(address)
            reader.read_bytes(address)
            with self.assertRaises(PodError) as caught: reader.read_bytes(address)
        self.assertEqual(caught.exception.code, "history_deadline")


class MigrationFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = synthetic_state(4)
        cls.raw = encoded(cls.state, compact=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "context.json"
        self.path.write_bytes(self.raw)
        self.facts = {"revision": self.state["revision"], "sha256": sha(self.raw)}

    def migrate(self):
        return migrate_context(self.path, **self.facts)

    def test_explicit_migration_lossless_source_and_idempotent_replay(self):
        self.assertEqual(self.migrate()["status"], "migrated")
        after = self.path.read_bytes()
        self.assertEqual(self.migrate()["status"], "replayed")
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(audit_context(self.path)["state"], self.state)
        self.assertLess(len(after), CONTEXT_LIMIT)

    def test_migrated_envelope_refuses_a_different_record_kind(self):
        self.migrate()
        before = self.path.read_bytes()
        with self.assertRaises(PodError) as caught:
            migrate_context(self.path, kind="governor", **self.facts)
        self.assertEqual(caught.exception.code, "history_invalid")
        self.assertEqual(self.path.read_bytes(), before)

    def test_migration_stale_source_refuses(self):
        self.path.write_bytes(self.raw + b" ")
        with self.assertRaises(PodError) as caught:
            self.migrate()
        self.assertEqual(caught.exception.code, "storage_migration_stale")
        self.assertEqual(self.path.read_bytes(), self.raw + b" ")

    def test_conversion_input_bound_names_preservation_and_never_rewrites_source(self):
        from pod.history import MAINTENANCE_LIMIT, _context_bytes
        with self.path.open("wb") as file:
            file.truncate(MAINTENANCE_LIMIT + 1)
        with self.assertRaises(PodError) as caught:
            _context_bytes(self.path, MAINTENANCE_LIMIT)
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertIn("preserve this record", caught.exception.detail["next_action"])
        self.assertIn("bounded storage conversion cannot read it", caught.exception.detail["next_action"])
        self.assertEqual(self.path.stat().st_size, MAINTENANCE_LIMIT + 1)

    def test_crash_after_chunk_publication_leaves_old_authority_and_resumes(self):
        with patch("pod.history.atomic_json", side_effect=OSError(errno.EIO, "injected crash")):
            with self.assertRaises(OSError): self.migrate()
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertTrue(list(self.path.parent.glob("history/sha256/*.json")))
        self.migrate()
        self.assertEqual(audit_context(self.path)["state"], self.state)

    def test_disk_full_before_and_during_publication_preserves_source(self):
        empty = SimpleNamespace(f_bavail=0, f_frsize=4096)
        with patch("pod.history.os.statvfs", return_value=empty):
            with self.assertRaises(PodError) as caught: self.migrate()
        self.assertEqual(caught.exception.code, "storage_full")
        self.assertEqual(self.path.read_bytes(), self.raw)
        with patch("pod.history.atomic_json", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(PodError) as caught: self.migrate()
        self.assertEqual(caught.exception.code, "storage_full")
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.migrate()

    def test_concurrent_stale_revision_cannot_overwrite_accepted_write(self):
        self.migrate()
        first, stale = load_context(self.path), load_context(self.path)
        _write(self.path, first)
        committed = self.path.read_bytes()
        with self.assertRaises(PodError) as caught: _write(self.path, stale)
        self.assertEqual(caught.exception.code, "map_stale")
        self.assertEqual(self.path.read_bytes(), committed)

    def test_serialized_concurrent_writers_accept_exactly_one_snapshot(self):
        from pod.ledger import _lock
        self.migrate()
        snapshots = [load_context(self.path), load_context(self.path)]
        def write(value):
            try:
                with _lock(self.path): _write(self.path, value)
            except PodError as exc:
                return exc.code
            return "accepted"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, snapshots))
        self.assertEqual(sorted(outcomes), ["accepted", "map_stale"])
        self.assertEqual(load_context(self.path)["revision"], self.state["revision"] + 1)

    def test_missing_or_corrupt_required_page_is_an_explicit_failure(self):
        self.migrate()
        physical = json.loads(self.path.read_bytes())
        target = self.path.parent / "history/sha256" / (physical["root"]["ref"]["sha256"] + ".json")
        original = target.read_bytes()
        target.unlink()
        with self.assertRaises(PodError) as caught: load_context(self.path)
        self.assertEqual(caught.exception.code, "history_missing")
        target.write_bytes(original + b" ")
        with self.assertRaises(PodError) as caught: load_context(self.path)
        self.assertEqual(caught.exception.code, "history_integrity")


class LargeObjectiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        cls.path = objective_root(case.project, "objective") / "context.json"
        raw = cls.path.read_bytes()
        migrate_context(cls.path, revision=_read(cls.path)["revision"], sha256=sha(raw))
        case.move()
        case.write()
        # Synthetic retained sourcing/Director observations; these are opaque old
        # data, not native/owner evidence and never selected as current proof.
        state = _read(cls.path)
        state["source_rejections"] = {"synthetic-sourcing": [{"source": f"source-{n}",
            "observations": {"description": "retained source observation " * 100}}
            for n in range(300)], "synthetic-director": [{"request": f"request-{n}",
            "observations": {"description": "retained Director observation " * 100}}
            for n in range(300)]}
        _write(cls.path, state)
        for start in range(0, 600, 100):
            case.write(seq=_read(cls.path)["checkpoint"]["seq"] + 1, update={
                f"past-{n}": {key: item for key, item in case.sub(f"past-{n}", state="withdrawn",
                    withdrawal={"by": "coordinator", "reason": "synthetic old subgoal"}).items() if key != "id"}
                for n in range(start, start + 100)})
        before = _read(cls.path)
        cls.metrics = {"rows": len(before["checkpoint"]["obligations"]), "before": capacity(cls.path, before)}
        cls.before_logical = descriptor_size(json.loads(cls.path.read_bytes())["root"])
        cls.latencies = {}
        def measure(name, operation):
            started = time.perf_counter()
            result = operation()
            cls.latencies[name] = time.perf_counter() - started
            return result
        frozen = case.packet(["A"], role="review")
        admission = measure("review_admission", lambda: case.start("scale-review", frozen))["admission"]
        case.settle(admission)
        measure("report", lambda: case.report(admission, frozen, map={"seq": _read(cls.path)["checkpoint"]["seq"] + 1,
            "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}},
            "K": {"kind": "correction", "provenance": "coordinator", "parent": "A", "check": "fix defect",
                  "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}}, triage=[{
            "finding": "F", "severity": "major", "triage": "required_correction", "blocks": "outcome", "correction": "K",
            "summary": "synthetic review defect"}]))
        measure("correction", lambda: case.write(seq=_read(cls.path)["checkpoint"]["seq"] + 1,
            update={"K": {"state": "satisfied", "evidence": [case.proof("K")]}}))
        frozen = case.packet(["A"], role="review")
        review = measure("verifying_review_admission", lambda: case.start("scale-verifier", frozen))["admission"]
        case.settle(review)
        measure("verifying_report", lambda: case.report(review, frozen,
            map={"seq": _read(cls.path)["checkpoint"]["seq"] + 1, "update": {"A": {
                "state": "satisfied", "evidence": [{"attempt": review["admission_id"]}]}}}))
        # Provider readback is injected, as in the existing delivery fixtures;
        # the production delivery verifier still checks the real disposable Git.
        from pod.governor import _empty_journal, _record_path, _write_journal
        from tests.kernel_support import authorization, git
        tree = git(case.project, "rev-parse", case.candidate + "^{tree}")
        binding = {"id": "scale-candidate", "commit": case.candidate, "tree": tree}
        journal = _empty_journal()
        journal["units"]["delivery"] = {"name": "delivery", "generation": 1,
            "history": ["scale-candidate"], "preflight": {}, "published": {}, "tasks": [],
            "candidate": binding, "branch": {"remote": "origin", "base": "target", "branch": "delivery"}}
        journal["actions"].append({"record_id": "scale-merge", "logical_key": "scale-merge", "attempt": 1,
            "action": {"kind": "merge", "candidate": "scale-candidate", "target": "origin/target", "unit": "delivery",
                       "authorization": authorization(candidate=case.candidate, tree=tree, scope=("merge",), target="origin/target")},
            "target_binding": {"remote": "origin", "base": "target", "ref": "refs/remotes/origin/target"},
            "candidate_id": "scale-candidate", "commit": case.candidate, "decision": "ALLOW", "outcome": "PASS",
            "receipt": {"provider": {"merge_commit": case.candidate, "method": "fast-forward"}},
            "warnings": [], "reasons": [], "classifications": []})
        _write_journal(_record_path(case.project, "objective"), journal)
        git(case.project, "update-ref", "refs/remotes/origin/target", case.candidate)
        measure("delivery", lambda: case.write(seq=_read(cls.path)["checkpoint"]["seq"] + 1,
                                               delivery={"record": "scale-merge"}))
        measure("close", lambda: case.write(seq=_read(cls.path)["checkpoint"]["seq"] + 1,
            update={"O1": {"state": "satisfied", "evidence": [case.proof("O1")]}}, close=True))
        cls.after = audit_context(cls.path)
        cls.metrics.update(after={key: cls.after[key] for key in ("physical_bytes", "logical_bytes", "complete")},
                           latency_seconds=cls.latencies)
        print("storage_scale=" + json.dumps(cls.metrics, sort_keys=True))

    def test_large_map_crosses_old_physical_and_lifetime_row_ceilings(self):
        self.assertGreater(self.metrics["rows"], 512)
        self.assertGreater(self.before_logical, CONTEXT_LIMIT)
        self.assertLess(self.metrics["before"]["physical_bytes"], CONTEXT_LIMIT)

    def test_report_correction_and_review_keep_original_obligation_and_findings(self):
        rows = {row["id"]: row for row in self.after["state"]["checkpoint"]["obligations"]}
        self.assertEqual(rows["K"]["state"], "satisfied")
        self.assertEqual(rows["A"]["state"], "satisfied")
        self.assertEqual(rows["A"]["findings"][0]["finding"], "F")
        self.assertIn("resolved_seq", rows["A"]["findings"][0])
        self.assertNotIn("renewal", rows["A"])

    def test_complete_closure_keeps_all_retained_history(self):
        self.assertTrue(self.after["complete"])
        self.assertTrue(self.after["state"]["checkpoint"]["closure"])
        self.assertEqual(self.after["state"]["checkpoint"]["delivery"]["record"], "scale-merge")
        self.assertEqual(len(self.after["state"]["source_rejections"]["synthetic-sourcing"]), 300)
        self.assertEqual(len(self.after["state"]["source_rejections"]["synthetic-director"]), 300)

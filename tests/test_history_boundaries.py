"""Semantic scans and write protections must use complete verified logical records."""
from tests.history_support import publish_preceding
from tests.history_support import compact
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import prepare_context, publish_context, load_context, capacity, is_reference, CONTEXT_LIMIT, MARKER
from tests.history_support import WARN_AT, COMPACT_AT
from pod.ledger import (_write, _read, objective_root, contexts_for_run, state_inventory, superseded_for_run)
from pod.listing import objective_list
from pod.handoff import run_scope
from pod.retirement import _claimants
from pod.util import encoded, digest
from tests.history_support import synthetic_state, size
from tests.kernel_support import KernelCase
from tests.issue41_support import ProductionCase
from tests.test_runtime_continuity import ContinuityCase


class ReusedReceiptCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_history_regressions import previous_history_bundle
        from tests.test_obligations import ctx, ob, proof, intake
        from pod.obligations import accept_write
        from pod.ledger import _empty

        previous = tempfile.TemporaryDirectory(prefix="receipt-previous-")
        cls.addClassCleanup(previous.cleanup)
        cls.previous_bundle = previous_history_bundle(previous.name)

        def context(candidate):
            return ctx(candidate=candidate, git_delta=lambda source, target: ["src/new.py"])

        state = accept_write(None, intake(*(ob(f"S{i}", boundary={"paths": ["docs"]})
                                           for i in range(32))), context("candidate-0"))
        # Accepted production-kernel transitions, never a captured private record.
        # Build the expensive, valid history once; no clock or budget changes.
        for number in range(7):
            candidate = f"candidate-{number}"
            rows = deepcopy(state["obligations"])
            for row in rows[1:]:
                row.pop("wait", None)
                row.pop("reuse", None)
                evidence = {**proof(row["id"], candidate, definition=row,
                                    reference=f"log-{number}-{row['id']}", check="check " * 80,
                                    sources=[{"path": "docs/contract.md", "state": "present", "sha256": "a" * 64}]),
                            "command": "run " * 120, "result": "observed " * 55}
                row.update(state="satisfied", evidence=[evidence])
            proposed = accept_write(state, {"obligations": rows}, context(candidate))
            if number == 6:
                checkpoint = {"schema": "pod-checkpoint/v3", "candidate": candidate}
                cls.prior = {**_empty(), "checkpoint": {**state, **checkpoint}}
                cls.proposed = {**_empty(), "checkpoint": {**proposed, **checkpoint}}
                cls.context = context(candidate)
                break
            rows = deepcopy(proposed["obligations"])
            for row in rows[1:]:
                row["reuse"] = {"from": candidate}
            state = accept_write(proposed, {"obligations": rows}, context(f"candidate-{number + 1}"))

    def arrange(self, directory):
        path = Path(directory) / "context.json"
        physical, chunks = compact(self.prior, force=True)
        publish_preceding(path, physical, chunks)
        return path

    def test_same_valid_logical_write_fits_with_exact_reuse_and_selected_siblings(self):
        from pod.history import audit_context
        with tempfile.TemporaryDirectory() as directory:
            path = self.arrange(directory)
            expected = deepcopy(self.proposed)
            _write(path, expected)
            self.assertLess(path.stat().st_size, CONTEXT_LIMIT)
            self.assertEqual(audit_context(path)["state"], expected)
            for old, new in zip(self.prior["checkpoint"]["obligations"], expected["checkpoint"]["obligations"]):
                self.assertTrue(all(receipt in new["receipts"] for receipt in old["receipts"]))

    def test_genuine_prechange_writer_refuses_the_same_valid_logical_write(self):
        from tests.test_history_regressions import previous_history_call
        result = previous_history_call(self.previous_bundle, self.proposed, operation="write", prior=self.prior)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "record_too_large")
        self.assertEqual(result["detail"]["limit_bytes"], CONTEXT_LIMIT)
        self.assertEqual(result["detail"]["current_bytes"], size(self.prior))
        self.assertTrue(result["unchanged"])
        print("prechange_receipt_capacity=" + str({"version": result["version"], "code": result["code"],
                                                   **result["detail"]}))

    def test_genuine_prechange_reader_hydrates_relocated_reuse_without_rewriting(self):
        from tests.test_history_regressions import previous_history_call
        record, chunks = compact(self.proposed)
        context = {key: value for key, value in self.context.items() if not callable(value)}
        result = previous_history_call(self.previous_bundle, record, chunks=chunks, context=context)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["state"], self.proposed)
        self.assertTrue(result["unchanged"])

    def test_protected_join_still_refuses_atomically_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.arrange(directory)
            with patch("pod.governor._read_journal", side_effect=PodError("invalid_record", "unreadable")):
                expected = deepcopy(self.proposed)
                _write(path, expected)
            self.assertEqual(_read(path), expected)
            # Storage does not change the unavailable Governor or admit an effect.

    def test_publication_failure_and_retry_keep_prior_context_and_reuse_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.arrange(directory)
            before = path.read_bytes()
            with patch("pod.history.atomic_json", side_effect=OSError("synthetic interruption")):
                with self.assertRaises(OSError):
                    _write(path, deepcopy(self.proposed))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(_read(path)["checkpoint"], self.prior["checkpoint"])
            chunks = {p: p.read_bytes() for p in path.parent.glob("history/sha256/*.json")}
            self.assertTrue(chunks)
            expected = deepcopy(self.proposed)
            _write(path, expected)
            self.assertEqual(_read(path), expected)
            after = {p: p.read_bytes() for p in path.parent.glob("history/sha256/*.json")}
            self.assertTrue(all(after[p] == raw for p, raw in chunks.items() if p in after))

    def test_fresh_selection_and_governor_candidate_are_replanned(self):
        from pod.history import hydrate
        with tempfile.TemporaryDirectory() as directory:
            path = self.arrange(directory)
            changed = deepcopy(self.proposed)
            row = changed["checkpoint"]["obligations"][1]
            row["evidence"] = [deepcopy(row["receipts"][0]["evidence"])]
            for outcome in ("pending", "UNKNOWN", "FAILED"):
                journal = {"actions": [{"decision": "ALLOW", "outcome": outcome,
                                        "action": {"candidate": "candidate-0"}}]}
                record, chunks = prepare_context(path, changed)
                self.assertEqual(hydrate(record, chunks.__getitem__), changed)

    def broken_history(self, modes):
        from pod.history import audit_context
        for mode, code in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = self.arrange(directory)
                _write(path, deepcopy(self.proposed))
                physical = json.loads(path.read_bytes())
                ref = physical["root"]["ref"]
                chunk = path.parent / "history/sha256" / (ref["sha256"] + ".json")
                if mode == "missing": chunk.unlink()
                elif mode == "corrupt": chunk.write_bytes(chunk.read_bytes() + b" ")
                elif mode == "redirected":
                    target = path.parent / "redirected.json"; chunk.rename(target); chunk.symlink_to(target)
                else:
                    ref.pop("bytes"); path.write_bytes(encoded(physical, compact=True))
                before = path.read_bytes()
                with self.assertRaises(PodError) as caught: audit_context(path)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(path.read_bytes(), before)

    def test_relocated_reuse_missing_and_corrupt_history_refuses(self):
        self.broken_history((("missing", "history_missing"), ("corrupt", "history_integrity")))

    def test_relocated_reuse_redirected_and_incomplete_history_refuses(self):
        self.broken_history((("redirected", "history_unsafe"), ("incomplete", "history_invalid")))


class HistoryScanTests(KernelCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.large = synthetic_state()

    def arrange(self):
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.state = deepcopy(self.large)
        physical, chunks = compact(self.state, force=True)
        publish_preceding(self.path, physical, chunks)
        self.record = json.loads(self.path.read_bytes())
        self.chunk = self.path.parent / "history/sha256" / (next(iter(chunks)) + ".json")

    def test_run_membership_handoff_and_listing_retain_archived_references(self):
        self.arrange()
        self.assertEqual(contexts_for_run("synthetic-run")[0][1], self.state)
        self.assertEqual(run_scope(self.project, "own", "synthetic-run")["objectives"], ["synthetic-capacity"])
        listed = objective_list(self.project)
        row = listed["objectives"][0]
        self.assertEqual((row["schema"], row["phase"], row["runs"]), ("current", "open", ["synthetic-run"]))
        self.assertIn("/synthetic/project", row["worktrees"])

    def test_doctor_inventory_does_not_call_valid_compacted_state_superseded(self):
        self.arrange()
        result = state_inventory(self.project)
        self.assertEqual((result["current"], result["unreadable"], result["unsupported"]), (1, 0, 0))
        self.assertEqual(result["superseded"], [])
        self.assertEqual(superseded_for_run("synthetic-run"), [])
        raw = json.loads(self.path.read_bytes())
        raw["schema"] = "pod-context/v999"
        self.path.write_bytes(encoded(raw, compact=True))
        result = state_inventory(self.project)
        self.assertEqual((result["current"], result["unsupported"]), (0, 1))
        self.assertEqual(result["superseded"][0]["schemas"], ["pod-context/v999"])

    def test_missing_chunk_holds_every_semantic_scan(self):
        self.arrange()
        self.chunk.unlink()
        with self.assertRaises(PodError) as caught:
            contexts_for_run("synthetic-run")
        self.assertEqual(caught.exception.code, "history_missing")
        scope = run_scope(self.project, "own", "synthetic-run")
        self.assertEqual(scope["unreadable"], 1)
        listed = objective_list(self.project)
        self.assertEqual(listed["objectives"], [])
        self.assertEqual(listed["unreadable_records"][0]["code"], "history_missing")
        self.assertEqual(state_inventory(self.project)["unreadable"], 1)
        self.assertEqual(_claimants(self.project, "successor")[1], [str(self.path.parent)])

    def test_raw_claimant_scan_sees_link_even_with_all_admissions_archived(self):
        self.arrange()
        self.record["continued_by"] = {"objective": "successor"}
        # Raw diagnostic scan intentionally does not validate the whole link schema.
        self.path.write_bytes(encoded(self.record, compact=True))
        self.assertEqual(_claimants(self.project, "successor"), (["synthetic-capacity"], []))

    def test_fresh_native_outstanding_exact_identity_vetoes_move(self):
        from pod.history import hydrate
        self.arrange()
        record, chunks = prepare_context(self.path, self.state)
        self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
        self.assertEqual(self.path.read_bytes(), encoded(self.record, compact=True))

    def test_governor_pending_unknown_failed_candidate_and_unreadable_join_veto(self):
        from pod.history import hydrate
        self.arrange()
        # Placement is no longer conditional on settlement. Every status and
        # bound native identity survives, even when the Governor cannot be read.
        for outcome in ("pending", "UNKNOWN", "FAILED"):
            journal = {"actions": [{"decision": "ALLOW", "outcome": outcome}]}
            record, chunks = prepare_context(self.path, self.state)
            self.assertEqual(hydrate(record, chunks.__getitem__), self.state)

    def test_claim_capacity_preflight_uses_planner_and_publishes_nothing(self):
        self.arrange()
        from pod.succession import _require_fit
        from pod.governor import _empty_journal
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}
        with patch("pod.succession._carried_journal", return_value=(_empty_journal(), False)):
            _require_fit(self.project, "successor", "synthetic-capacity", self.state, [], at="synthetic-time",
                         records={"predecessor's record": self.state}, linked=False)
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()})

    def test_claim_preflight_accounts_for_future_carried_journal_protections(self):
        self.arrange()
        from pod.succession import _require_fit
        from pod.governor import _empty_journal
        future = _empty_journal()
        future["actions"] = [{"record_id": "pending", "decision": "ALLOW", "outcome": "pending",
                              "action": {"kind": "push", "candidate": "candidate", "target": "origin/work", "unit": "work"}}]
        with patch("pod.succession._carried_journal", return_value=(future, True)):
            _require_fit(self.project, "successor", "synthetic-capacity", self.state, [], at="synthetic-time",
                         records={"successor's record": self.state}, linked=False)
        self.assertFalse(objective_root(self.project, "successor").exists())
        self.assertEqual(future["actions"][0]["outcome"], "pending")

    def test_capacity_thresholds_and_largest_sections_are_additive_observations(self):
        self.arrange()
        block = capacity(self.path, self.state)
        self.assertTrue(block["conversion_pending"])
        self.assertGreater(block["logical_bytes"], block["physical_bytes"])
        self.assertFalse(block["warning"])
        self.assertIn("storage inspect and migrate", block["next_action"])

    def test_warning_before_trigger_describes_compaction_conditionally(self):
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        state = synthetic_state(count=1)
        state["source_rejections"] = {"synthetic-unsettled": "x" * (WARN_AT - size(state) + 1000)}
        _write(self.path, state)
        block = capacity(self.path, _read(self.path))
        self.assertFalse(block["warning"])
        self.assertEqual(block["format"], "pod-history-record/v1")
        self.assertIn("continue ordinary", block["next_action"])
        self.assertGreater(block["logical_bytes"], block["physical_bytes"])


class PublicHistoryProjectionTests(ProductionCase):
    def compact_current(self):
        path = objective_root(self.project, "objective") / "context.json"
        logical = _read(path)
        record, chunks = compact(logical, force=True)
        self.assertTrue(chunks)
        self.assertTrue(is_reference(record["checkpoint"]["revision_decisions"][0]))
        self.assertLess(size(record), size(logical))
        publish_preceding(path, record, chunks)
        return path

    def arrange(self):
        self.prepare()
        # Genuine earlier revision rows save bytes; short candidate IDs stay inline.
        for index in range(2):
            self.write(self.stored(), revision_authority={"provenance": "user_direct",
                "instruction": f"Owner revision {index}: " + "preserve this recorded instruction; " * 12})
        self.move()
        self.write(self.stored())

    def status(self, *, by_run=False):
        from pod.status import status as observe
        return observe(self.project, **({"run": "run"} if by_run else {"run": None, "objective": "objective"}),
                       current_run_fn=lambda: {"run": self.current},
                       worker_rows_fn=lambda _: {"workers": [], "scope": {"run": "run"}, "complete": True, "runtime": "runtime"})

    def test_status_by_run_and_objective_preserve_every_existing_key(self):
        self.arrange()
        before = [self.status(), self.status(by_run=True)]
        path = self.compact_current()
        for expected, actual in zip(before, [self.status(), self.status(by_run=True)]):
            expected.pop("record_capacity", None)
            self.assertIn("record_capacity", actual)
            actual.pop("record_capacity")
            self.assertEqual(expected, actual)
        self.assertEqual(state_inventory(self.project)["current"], 1)

    def test_governor_map_and_checkpoint_rewrite_hydrated_history(self):
        self.arrange()
        path = self.compact_current()
        logical = _read(path)
        self.assertEqual(self.op("map")["candidate"], logical["checkpoint"]["candidate"])
        result = self.op("governor-status")
        self.assertIsInstance(result, dict)
        self.write(self.stored())
        self.assertEqual(_read(path)["checkpoint"]["governance_history"], logical["checkpoint"]["governance_history"])

    def test_governor_allow_and_defer_decisions_are_identical_after_compaction(self):
        from datetime import datetime, timezone
        from pod.governor import decide, _record_path
        from pod.internal import _governor_projection
        self.arrange()
        self.prepare()  # Bind the current candidate's tree and authorization.
        journal = _record_path(self.project, "objective")
        before = journal.read_bytes()
        saved_chunks = {p: p.read_bytes() for p in journal.parent.glob("history/sha256/*.json")}
        def restore():
            for p, data in saved_chunks.items(): p.write_bytes(data)
            journal.write_bytes(before)
        now = datetime.now(timezone.utc)
        projection = _governor_projection(self.project, "objective", "owner", mutating=True)
        actions = [self.action(), {**self.action(), "authorization": None}]
        expected = []
        for action in actions:
            restore()
            expected.append(decide(self.project, "objective", owner="owner", action=action,
                                   now=now, native_projection=projection))
        restore()
        self.compact_current()
        actual = []
        for action in actions:
            restore()
            actual.append(decide(self.project, "objective", owner="owner", action=action,
                                 now=now, native_projection=projection))
        self.assertEqual(expected, actual)
        self.assertEqual([row["decision"] for row in actual], ["ALLOW", "DEFER"])

    def test_source_rejection_writes_complete_hydrated_state(self):
        from pod.ledger import check_bound_sources
        self.arrange()
        path = self.compact_current()
        before = _read(path)["checkpoint"]["governance_history"]
        with self.assertRaises(PodError) as caught:
            check_bound_sources(self.project, "objective", owner="owner", assignment="synthetic-packet",
                                sources=[{"path": "README.md", "state": "present", "sha256": "0" * 64}])
        self.assertEqual(caught.exception.code, "source_changed")
        state = _read(path)
        self.assertEqual(state["checkpoint"]["governance_history"], before)
        self.assertEqual(state["source_rejections"]["synthetic-packet"]["reason"], "source_changed")

    def test_report_replay_reads_compacted_state_without_another_logical_write(self):
        self.arrange()
        self.write([self.criterion(), self.sub("S")])
        frozen = self.packet(["S"], role="investigate", resolves="What changed?", stop_condition="Record result")
        admission = self.start("task-1", frozen)["admission"]
        self.settle(admission)
        rows = deepcopy(self.stored())
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(admission, frozen, map={"obligations": rows})
        path = self.compact_current()
        before = path.read_bytes()
        self.report(admission, frozen)
        self.assertEqual(path.read_bytes(), before)

    def test_native_observation_finally_persists_when_archive_and_native_contact_fail(self):
        from pod.operations import _observe_start
        self.arrange()
        self.write([self.criterion(), self.sub("S")])
        frozen = self.packet(["S"], role="investigate", resolves="Observe", stop_condition="Report")
        admission = self.start("task-1", frozen)["admission"]
        path = self.compact_current()
        prior = _read(path)["admissions"][admission["admission_id"]]
        before = len(prior["recovery"]["start_observations"])
        with patch("pod.ledger.require_authority", side_effect=AssertionError("no new native authority read")), \
             patch("pod.operations.atomic_json", side_effect=OSError("archive failed")):
            with self.assertRaises(OSError):
                _observe_start(self.project, "objective", owner="owner", admission_id=admission["admission_id"],
                               observation={"synthetic": "new immutable native response"}, retry_request=None,
                               reference={"runtime": "runtime"})
        after = _read(path)["admissions"][admission["admission_id"]]
        self.assertEqual(len(after["recovery"]["start_observations"]), before + 1)
        self.assertEqual((after["state"], after["request_uuid"]), (prior["state"], prior["request_uuid"]))


class HistoryContinuityTests(ContinuityCase):
    def test_proven_runtime_rebind_keeps_archived_decision_and_candidate_values(self):
        from tests.kernel_support import git
        self.intake()
        self.checkpoint_op()
        for index in range(2):
            self.checkpoint_op(revision_authority={"provenance": "user_direct",
                "instruction": f"Owner revision {index}: " + "preserve this recorded instruction; " * 12})
        (self.project / "README.md").write_text("new candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "new candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.checkpoint_op(candidate=self.candidate)
        state = _read(self.context)
        record, chunks = compact(state, force=True)
        self.assertTrue(chunks)
        self.assertTrue(is_reference(record["checkpoint"]["revision_decisions"][0]))
        publish_preceding(self.context, record, chunks)
        before = state["checkpoint"]["governance_history"]
        self.change_runtime()
        self.checkpoint_op()
        after = _read(self.context)
        self.assertEqual(after["checkpoint"]["governance_history"], before)
        self.assertEqual(after["checkpoint"]["revision_decisions"], state["checkpoint"]["revision_decisions"])
        self.assertEqual(after["checkpoint"]["native_refs"][0]["runtime"], "runtime-2")
        self.assertEqual(len(self.history()), 1)

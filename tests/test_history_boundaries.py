"""Semantic scans and write protections must use complete verified logical records."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from pod.errors import PodError
from pod.history import (compact, prepare_context, publish_context, load_context, capacity,
                         is_reference, CONTEXT_LIMIT, WARN_AT, COMPACT_AT, MARKER)
from pod.ledger import (_write, _read, objective_root, contexts_for_run, state_inventory, superseded_for_run)
from pod.listing import objective_list
from pod.handoff import run_scope
from pod.retirement import _claimants
from pod.util import encoded, digest
from tests.history_support import synthetic_state, size
from tests.kernel_support import KernelCase
from tests.issue41_support import ProductionCase
from tests.test_runtime_continuity import ContinuityCase


class HistoryScanTests(KernelCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.large = synthetic_state()

    def arrange(self):
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.state = deepcopy(self.large)
        _write(self.path, self.state)
        self.record = json.loads(self.path.read_bytes())
        self.chunk = next((self.path.parent / "history/sha256").glob("*.json"))

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
        self.arrange()
        key = "attempt-048"
        row = self.state["admissions"][key]
        binding = row["native_binding"]
        native = {"runtime": row["runtime"], "assignments": [
            {"admission_id": k, "runtime": value["runtime"], "settled": True,
             **{field: value["native_binding"][source] for field, source in
                (("run_id", "runId"), ("task_id", "taskId"), ("dispatch_id", "dispatchId"), ("worker_id", "workerId"))}}
            for k, value in self.state["admissions"].items()]}
        native["assignments"][48]["dispatch_id"] = "different"
        record, _ = prepare_context(self.path, self.state, native=native)
        self.assertFalse(is_reference(record["admissions"][key]))
        self.assertTrue(is_reference(record["admissions"]["attempt-049"]))

    def test_governor_pending_unknown_failed_candidate_and_unreadable_join_veto(self):
        self.arrange()
        from pod.governor import _empty_journal
        key = "attempt-048"
        journal = _empty_journal()
        for outcome in ("pending", "UNKNOWN", "FAILED"):
            journal["actions"] = [{"decision": "ALLOW", "outcome": outcome,
                                   "action": {"candidate": self.state["admissions"][key]["candidate"]}}]
            with patch("pod.governor._read_journal", return_value=journal):
                record, _ = prepare_context(self.path, self.state)
            self.assertFalse(is_reference(record["admissions"][key]))
            self.assertTrue(is_reference(record["admissions"]["attempt-049"]))
        with patch("pod.governor._read_journal", side_effect=PodError("invalid_record", "unreadable")):
            with self.assertRaises(PodError) as caught:
                prepare_context(self.path, self.state)
        self.assertEqual(caught.exception.code, "record_too_large")

    def test_claim_capacity_preflight_uses_planner_and_publishes_nothing(self):
        self.arrange()
        from pod.succession import _require_fit
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}
        with patch("pod.succession._carried_journal", return_value=({"revision": 0}, False)):
            _require_fit(self.project, "successor", "synthetic-capacity", self.state, [], at="synthetic-time",
                         records={"predecessor's record": self.state}, linked=False)
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()})

    def test_claim_preflight_accounts_for_future_carried_journal_protections(self):
        self.arrange()
        from pod.succession import _require_fit
        candidates = {row["candidate"] for row in self.state["admissions"].values()}
        future = {"revision": 0, "actions": [{"decision": "ALLOW", "outcome": "pending", "action": {"candidate": c}}
                                             for c in candidates]}
        with patch("pod.succession._carried_journal", return_value=(future, True)):
            with self.assertRaises(PodError) as caught:
                _require_fit(self.project, "successor", "synthetic-capacity", self.state, [], at="synthetic-time",
                             records={"successor's record": self.state}, linked=False)
        self.assertEqual(caught.exception.code, "claim_capacity")
        self.assertFalse(objective_root(self.project, "successor").exists())

    def test_capacity_thresholds_and_largest_sections_are_additive_observations(self):
        self.arrange()
        block = capacity(self.path, self.state)
        self.assertEqual((block["warning_bytes"], block["compaction_bytes"], block["limit_bytes"]),
                         (262144, 327680, 524288))
        self.assertGreater(block["logical_bytes"], block["physical_bytes"])
        self.assertFalse(block["warning"])
        self.assertEqual(block["largest_sections"][0]["section"], "admissions")


class PublicHistoryProjectionTests(ProductionCase):
    def compact_current(self):
        path = objective_root(self.project, "objective") / "context.json"
        logical = _read(path)
        record, chunks = compact(logical, force=True)
        self.assertTrue(chunks)
        publish_context(path, record, chunks)
        return path

    def arrange(self):
        self.prepare()
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
        now = datetime.now(timezone.utc)
        projection = _governor_projection(self.project, "objective", "owner", mutating=True)
        actions = [self.action(), {**self.action(), "authorization": None}]
        expected = []
        for action in actions:
            journal.write_bytes(before)
            expected.append(decide(self.project, "objective", owner="owner", action=action,
                                   now=now, native_projection=projection))
        journal.write_bytes(before)
        self.compact_current()
        actual = []
        for action in actions:
            journal.write_bytes(before)
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
        (self.project / "README.md").write_text("new candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "new candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.checkpoint_op(candidate=self.candidate)
        state = _read(self.context)
        record, chunks = compact(state, force=True)
        self.assertTrue(chunks)
        publish_context(self.context, record, chunks)
        before = state["checkpoint"]["governance_history"]
        self.change_runtime()
        self.checkpoint_op()
        after = _read(self.context)
        self.assertEqual(after["checkpoint"]["governance_history"], before)
        self.assertEqual(after["checkpoint"]["native_refs"][0]["runtime"], "runtime-2")
        self.assertEqual(len(self.history()), 1)

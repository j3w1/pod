"""Versioned claim reads: current ownership survives out-of-line evidence."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import (ChunkStore, audit_context, load_admission_facts, load_context, migrate_context)
from pod.ledger import objective_root
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal


class AdmissionFactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        cls.location = {key: case.port.placement[key] for key in ("repository", "repo_key", "path", "branch")}
        case.intake(case.sub("S"), worktree=cls.location)
        frozen = case.packet(["S"], role="investigate", resolves="What happened?", stop_condition="Report facts", worktree=cls.location)
        old = case.start("past", frozen)["admission"]; case.settle(old)
        report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                  "attempt": old["native_binding"]["dispatchId"], "candidate": case.candidate,
                  "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["fixture"],
                  "failures": [], "uncertainty": [], "questions": [],
                  "evidence": [f"retained-{n}:" + "x" * 400 for n in range(60)]}
        case.op("report", admission_id=old["admission_id"], packet=frozen, report=report,
                map={"seq": load_context(objective_root(case.project, "objective") / "context.json")["checkpoint"]["seq"] + 1,
                     "update": {"S": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
        cls.old = old["admission_id"]
        frozen = case.packet(["S"], role="investigate", resolves="What happens next?", stop_condition="Report facts", worktree=cls.location)
        cls.live = case.start("still-live", frozen)["admission"]
        source = objective_root(case.project, "objective") / "context.json"
        cls.before = load_admission_facts(source)
        migrate_context(source, revision=load_context(source)["revision"],
                        sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        cls.key = source.parent.name
        cls.files = {path.relative_to(source.parent): path.read_bytes()
                     for path in source.parent.rglob("*") if path.is_file() and path.name != ".lock"}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / self.key / "context.json"
        for relative, raw in self.files.items():
            target = self.path.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

    def remove(self, reference):
        target = self.path.parent / "history/sha256" / (reference["sha256"] + ".json")
        target.unlink()

    def test_possibly_live_worker_remains_visible_after_migration(self):
        facts = load_admission_facts(self.path)
        self.assertEqual(facts["schema"], "pod-admission-facts/v1")
        self.assertTrue(facts["complete"])
        self.assertTrue(facts["attribution_complete"])
        self.assertEqual(facts["phase"], "open")
        self.assertEqual(facts["admissions"], self.before["admissions"])
        row = facts["admissions"][self.live["admission_id"]]
        self.assertEqual(row["native_binding"], self.live["native_binding"])
        self.assertEqual(row["state"], "bound")
        self.assertNotIn("report", row)

    def test_exact_foreign_worker_placement_is_a_fact_not_objective_attribution(self):
        from pod.ledger import _write
        from pod.util import digest
        value = load_context(self.path)
        placement = {"repository": "acme/foreign", "repo_key": digest("acme/foreign"),
                     "path": "/synthetic/foreign-worktree", "branch": "resource-work"}
        value["admissions"][self.live["admission_id"]]["recovery"]["placement_binding"] = placement
        _write(self.path, value)
        facts = load_admission_facts(self.path)
        row = facts["admissions"][self.live["admission_id"]]
        self.assertEqual(row["placement_binding"], placement)
        self.assertNotIn("recovery", row)
        self.assertNotIn("report", row)
        self.assertEqual(facts["attribution"]["repository"], "acme/example")

    def test_accounting_presence_does_not_open_fence_or_report_payloads(self):
        from pod.history_tree import plan
        from pod.util import encoded
        import json
        value = load_context(self.path)
        row = value["admissions"][self.live["admission_id"]]
        marker = {"classification": "abandoned_no_observed_start", "execution": "unknown",
                  "fence": {"opaque": "frozen native observation " * 1000}}
        row["abandoned_accounting"] = marker
        # The current-facts boundary preserves the small stored marker without
        # claiming that this synthetic envelope is a valid full accounting.
        root, chunks = plan(value, store_root=ChunkStore(self.path.parent).root, force_root=True)
        physical = json.loads(self.path.read_bytes())
        from pod.history_tree import open_root, plain
        from pod.util import digest
        attribution = plain(open_root(physical["attribution"], ChunkStore(self.path.parent)))
        attribution["root_sha256"] = digest(root)
        physical["attribution"], added = plan(attribution, force_root=True)
        chunks.update(added); physical["root"] = root
        ChunkStore(self.path.parent).put(chunks)
        self.path.write_bytes(encoded(physical, compact=True))
        stored = load_context(self.path, validate=False)
        fence = stored["admissions"][self.live["admission_id"]]["abandoned_accounting"]["fence"]
        self.remove(fence.reference)
        projected = load_admission_facts(self.path)["admissions"][self.live["admission_id"]]
        self.assertEqual(projected["abandoned_accounting"],
                         {"classification": "abandoned_no_observed_start", "execution": "unknown"})
        self.assertNotIn("fence", projected["abandoned_accounting"])

    def test_foreign_attribution_does_not_open_claim_index(self):
        state = load_context(self.path)
        claims = state["admissions"].reference["sha256"]
        observed = []
        original = ChunkStore._load
        def record(store, directory, address):
            observed.append(address)
            return original(store, directory, address)
        with patch.object(ChunkStore, "_load", record):
            facts = load_admission_facts(self.path, include_admissions=False)
        self.assertNotIn(claims, observed)
        self.assertTrue(facts["attribution_complete"])
        self.assertFalse(facts["complete"])
        self.assertIsNone(facts["admissions"])
        self.assertNotEqual(facts["context"]["checkpoint"]["worktree"]["repo_key"], "0" * 64)

    def test_missing_unselected_report_does_not_hide_current_claims(self):
        state = load_context(self.path)
        observation = state["admissions"][self.old]["report"]["observation"]
        self.remove(observation.reference)
        facts = load_admission_facts(self.path)
        self.assertIn(self.live["admission_id"], facts["admissions"])
        self.assertTrue(facts["complete"])
        with self.assertRaises(PodError) as caught:
            audit_context(self.path)
        self.assertEqual(caught.exception.code, "history_missing")
    def test_routine_checkpoint_does_not_read_unselected_report_body(self):
        state = load_context(self.path)
        self.remove(state["admissions"][self.old]["report"]["observation"].reference)
        with patch.dict(os.environ, {"POD_STATE_HOME": str(self.path.parent.parent)}):
            self.case.write(next_safe_action="continue after current-facts inspection")
        self.assertEqual(load_context(self.path)["checkpoint"]["next_safe_action"],
                         "continue after current-facts inspection")

    def test_missing_claim_index_is_unknown_not_empty_complete(self):
        state = load_context(self.path)
        self.remove(state["admissions"].reference)
        self.assertTrue(load_admission_facts(self.path, include_admissions=False)["attribution_complete"])
        with self.assertRaises(PodError) as caught:
            load_admission_facts(self.path)
        self.assertEqual(caught.exception.code, "history_missing")


class SelectedReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        frozen = case.packet(["A"], role="review")
        cls.review = case.start("selected-review", frozen)["admission"]; case.settle(cls.review)
        path = cls.path = objective_root(case.project, "objective") / "context.json"
        report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
            "attempt": cls.review["native_binding"]["dispatchId"], "candidate": case.candidate,
            "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["fixture"],
            "failures": [], "uncertainty": [], "questions": [],
            "evidence": [f"required-{n}:" + "x" * 400 for n in range(60)]}
        case.op("report", admission_id=cls.review["admission_id"], packet=frozen, report=report,
            map={"seq": load_context(path)["checkpoint"]["seq"] + 1, "update": {"A": {
                "state": "satisfied", "evidence": [{"attempt": cls.review["admission_id"]}]}}})
        migrate_context(path, revision=load_context(path)["revision"], sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_selected_review_body_cannot_supply_current_proof(self):
        state = load_context(self.path)
        observation = state["admissions"][self.review["admission_id"]]["report"]["observation"]
        target = self.path.parent / "history/sha256" / (observation.reference["sha256"] + ".json")
        target.unlink()
        before = self.path.read_bytes()
        with self.assertRaises(PodError) as caught:
            self.case.write(next_safe_action="requires the selected proof")
        self.assertEqual(caught.exception.code, "history_missing")
        self.assertEqual(self.path.read_bytes(), before)

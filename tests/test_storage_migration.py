"""Lossless migration keeps authority history and honest attribution unknowns."""
from tests.history_support import publish_preceding
from tests.history_support import compact
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from pod.errors import PodError
from pod.history import (audit_context, load_admission_facts, load_context, migrate_context, publish_context, capacity)
from pod.ledger import objective_root
from pod.util import digest, encoded
from tests.history_support import synthetic_state
from tests.issue41_support import ProductionCase


class NormalizationTests(unittest.TestCase):
    def test_record_namespace_proves_repository_without_inventing_a_placement(self):
        case = ProductionCase(); case.setUp(); self.addCleanup(case.doCleanups)
        case.intake()
        path = objective_root(case.project, "objective") / "context.json"
        before = load_context(path)
        result = migrate_context(path, revision=before["revision"],
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), project=case.project)
        self.assertEqual(result["attribution"]["status"], "proved")
        facts = load_admission_facts(path, include_admissions=False)
        self.assertTrue(facts["attribution_complete"])
        self.assertEqual(facts["attribution"]["repository"], "acme/example")
        self.assertIsNone(facts["context"]["checkpoint"]["worktree"])
        unknown_scope = load_admission_facts(path)
        self.assertFalse(unknown_scope["complete"])
        self.assertEqual(unknown_scope["errors"], [{"code": "objective_placement_unknown"}])
        self.assertEqual(audit_context(path)["state"], before)

    def fixture(self, identities):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "record/context.json"; path.parent.mkdir()
        state = synthetic_state(2)
        for row, identity in zip(state["admissions"].values(), identities):
            row["recovery"]["placement_binding"] = {"repository": identity, "repo_key": digest(identity),
                "path": "/synthetic/repository", "branch": "work"}
        raw = encoded(state, compact=True); path.write_bytes(raw)
        result = migrate_context(path, revision=state["revision"], sha256=hashlib.sha256(raw).hexdigest())
        return path, state, result

    def test_captured_placement_facts_can_prove_repository_only(self):
        path, state, result = self.fixture(["acme/example", "acme/example"])
        self.assertEqual(result["attribution"]["status"], "proved")
        facts = load_admission_facts(path, include_admissions=False)
        self.assertEqual(facts["attribution"]["repository"], "acme/example")
        self.assertIsNone(facts["context"]["checkpoint"]["worktree"])
        self.assertEqual(audit_context(path)["state"], state)

    def test_conflicting_or_absent_facts_stay_scoped_unknown(self):
        for identities, reason in ((["acme/one", "acme/two"], "recorded_repository_conflict"),
                                   ([], "repository_unavailable")):
            with self.subTest(reason=reason):
                path, state, result = self.fixture(identities)
                self.assertEqual(result["attribution"], {"status": "unknown", "reason": reason,
                                                        "objective": "synthetic-capacity"})
                with self.assertRaises(PodError) as caught: load_admission_facts(path, include_admissions=False)
                self.assertEqual(caught.exception.code, "attribution_unavailable")
                self.assertEqual(audit_context(path)["state"], state)


class MaintenanceRetentionTests(unittest.TestCase):
    def test_many_complete_maintenance_receipts_live_outside_the_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "context.json"
            state = synthetic_state(4)
            receipts = []
            for n in range(200):
                request = {"schema": "pod-compaction-request/v1", "requested_by": "coordinator",
                    "reason": f"synthetic maintenance {n} " + "x" * 900,
                    "owner": state["owner"], "repository": "a" * 64, "objective": "synthetic-capacity",
                    "facts": {"record": "b" * 64, "revision": 0, "input_sha256": "c" * 64,
                              "logical_sha256": digest(state)}}
                receipts.append({"authority": request, "decision": digest(request), "input_sha256": "c" * 64,
                                 "logical_sha256": digest(state)})
            physical, chunks = compact(state, force=True, maintenance=receipts)
            publish_preceding(path, physical, chunks)
            raw = path.read_bytes()
            migrate_context(path, revision=state["revision"], sha256=hashlib.sha256(raw).hexdigest())
            observed = capacity(path, load_context(path))
            self.assertFalse(observed["maintenance_loaded"])
            self.assertIsNone(observed["maintenance"])
            self.assertLess(path.stat().st_size, 4096)
            audited = audit_context(path)
            self.assertEqual(audited["maintenance"], receipts)
            self.assertEqual(audited["state"], state)


class RetainedFindingTests(unittest.TestCase):
    def test_retained_findings_cross_sixty_four_without_spending_current_work(self):
        from pod.assurance import validate_findings, reserve_capacity
        findings = [{"attempt": f"synthetic-review-{n // 20}", "finding": f"F{n}", "severity": "minor",
            "triage": "advisory", "summary": "synthetic retained advisory", "recorded_seq": n + 1,
            "proposal": f"P{n}"} for n in range(200)]
        assurance = {"id": "A", "kind": "assurance", "state": "waiting", "findings": findings, "receipts": []}
        validate_findings(assurance, {"A": assurance}, seq=300)
        proposals = [{"id": f"P{n}", "status": "adopted"} for n in range(200)]
        context = {"map_state": {"obligations": [assurance], "proposals": proposals},
                   "admissions": {}, "outstanding": []}
        before = deepcopy(assurance)
        reserve_capacity(assurance, context, seq=301)
        self.assertEqual(assurance, before)
        context["map_state"]["proposals"] = []
        reserve_capacity(assurance, context, seq=301)
        self.assertEqual(assurance, before)

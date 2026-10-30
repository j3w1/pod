"""Lossless settlement placement and live-count capacity, never native proof."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pod.assurance import live_finding_count, reserve_capacity
from pod.errors import PodError
from pod.history import (FORMAT_V2, compact, compact_journal, hydrate, is_reference,
                         load_context, load_journal_history, publish_context, publish_journal)
from pod.ledger import _empty
from pod.limits import Limits, using_limits
from tests.test_history_reviews import review_state
from tests.test_history_current_reviews import current_reviews


class SettledHistoryLimitsTests(unittest.TestCase):
    def test_settled_decisions_questions_and_constraints_move_but_current_controls_stay(self):
        state = _empty()
        state["constraints"] = [{"id": "inactive", "active": False, "text": "x" * 1000},
                                {"id": "active", "active": True, "text": "y" * 1000}]
        state["checkpoint"] = {"questions": [{"id": "done", "resolved": True, "text": "z" * 1000},
                                               {"id": "pending", "resolved": False, "text": "w" * 1000}]}
        state["standing_authorization"] = {"decisions": [{"id": "Y1", "text": "a" * 1000},
                                                          {"id": "Y2", "text": "b" * 1000}],
                                             "answers": [{"id": "A1", "text": "c" * 1000}]}
        # Storage-only sanitized values; no owner/person or authority claim.
        physical, chunks = compact(state)
        self.assertTrue(is_reference(physical["constraints"][0]))
        self.assertEqual(physical["constraints"][1], state["constraints"][1])
        self.assertTrue(is_reference(physical["checkpoint"]["questions"][0]))
        self.assertEqual(physical["checkpoint"]["questions"][1], state["checkpoint"]["questions"][1])
        self.assertTrue(is_reference(physical["standing_authorization"]["decisions"][0]))
        self.assertEqual(physical["standing_authorization"]["decisions"][1], state["standing_authorization"]["decisions"][1])
        self.assertEqual(hydrate(physical, chunks.__getitem__), state)

    def test_existing_unknown_or_explicit_source_keeps_failed_current_review_inline(self):
        for policy in (None, {"keep_failed_current_candidate_reviews": True, "source": "synthetic protected source"}):
            state = current_reviews()
            state["source_rejections"] = {}
            if policy is not None:
                state["checkpoint"]["history_policy"] = policy
            else:
                state["checkpoint"].pop("history_policy", None)
            physical, chunks = compact(state, bulk_history=True)
            for key, row in state["admissions"].items():
                if row["report"]["outcome"] != "succeeded":
                    self.assertEqual(physical["admissions"][key], row)
            self.assertEqual(hydrate(physical, chunks.__getitem__), state)

    def test_current_failed_reviews_move_only_under_explicit_applicable_storage_policy(self):
        state = current_reviews()
        state["source_rejections"] = {}
        state["checkpoint"]["history_policy"] = {"keep_failed_current_candidate_reviews": False,
                                                   "source": "synthetic applicable current source"}
        physical, chunks = compact(state, bulk_history=True)
        self.assertFalse(physical["admissions"])
        self.assertEqual(hydrate(physical, chunks.__getitem__), state)

    def test_governance_above_old_64_is_complete_and_outside_live_record(self):
        state = _empty()
        candidates = [f"{index:040x}" for index in range(1, 71)]
        state["checkpoint"] = {"schema": "pod-checkpoint/v3", "objective": "synthetic-existing", "candidate": candidates[-1],
                               "governance_history": {"candidates": candidates, "decisions": []}}
        with tempfile.TemporaryDirectory() as directory, using_limits(Limits(governance_history=2)):
            path = Path(directory) / "context.json"
            physical, chunks = compact(state)
            self.assertEqual(physical["history"]["schema"], FORMAT_V2["schema"])
            self.assertTrue(is_reference(physical["checkpoint"]["governance_history"]["candidates"]))
            publish_context(path, physical, chunks)
            self.assertEqual(load_context(path), state)

    def test_bulk_settled_reviews_keep_every_outcome_and_protected_sibling(self):
        state = review_state()
        state["source_rejections"] = {}
        keep = "attempt-001"
        physical, chunks = compact(state, protected_admissions=[keep], bulk_history=True)
        self.assertEqual(physical["admissions"][keep], state["admissions"][keep])
        self.assertTrue(is_reference(physical["settled_admissions"]))
        self.assertEqual(hydrate(physical, chunks.__getitem__), state)
        self.assertEqual({row["report"]["outcome"] for row in state["admissions"].values()},
                         {"succeeded", "partial", "failed", "blocked", "uncertain"})

    def test_bulk_history_cannot_replace_a_live_admission(self):
        state = review_state()
        state["source_rejections"] = {}
        physical, chunks = compact(state, bulk_history=True)
        key = next(iter(state["admissions"]))
        physical["admissions"][key] = state["admissions"][key]
        with self.assertRaises(PodError) as caught:
            hydrate(physical, chunks.__getitem__)
        self.assertEqual(caught.exception.code, "history_integrity")

    def test_downgraded_bulk_or_governance_representation_refuses(self):
        state = review_state()
        state["source_rejections"] = {}
        physical, chunks = compact(state, bulk_history=True)
        physical["history"]["schema"] = "pod-history-refs/v1"
        with self.assertRaises(PodError) as caught:
            hydrate(physical, chunks.__getitem__)
        self.assertEqual(caught.exception.code, "history_invalid")

    def test_missing_and_corrupt_settled_chunks_fail_closed(self):
        state = review_state()
        state["source_rejections"] = {}
        physical, chunks = compact(state, bulk_history=True)
        def missing(address):
            raise FileNotFoundError(address)
        for loader, code in ((missing, "history_missing"), (lambda address: b"corrupt", "history_integrity")):
            with self.subTest(code=code), self.assertRaises(PodError) as caught:
                hydrate(physical, loader)
            self.assertEqual(caught.exception.code, code)

    def test_settled_findings_do_not_spend_new_live_reservations(self):
        settled = [{"triage": "required_correction", "resolved_seq": index + 1} for index in range(100)]
        ob = {"id": "A", "findings": settled, "receipts": [{"reported_seq": i + 1} for i in range(100)]}
        ctx = {"admissions": {}, "outstanding": []}
        with using_limits(Limits(findings=32)):
            reserve_capacity(ob, ctx, seq=200)
            self.assertEqual(live_finding_count(settled, ctx), 0)
            with self.assertRaises(PodError):
                reserve_capacity({**ob, "findings": [*settled, {"triage": "required_correction"}]}, ctx, seq=200)

    def test_unresolved_and_unadopted_findings_keep_live_capacity(self):
        findings = [{"triage": "required_correction"},
                    {"triage": "required_correction", "resolved_seq": True},
                    {"triage": "advisory", "proposal": "open"},
                    {"triage": "advisory", "proposal": "adopted"}]
        ctx = {"map_state": {"proposals": [{"id": "adopted", "status": "adopted"}]}}
        self.assertEqual(live_finding_count(findings, ctx), 3)

    def test_journal_placement_preserves_failed_unknown_pending_rows_and_counters(self):
        # A storage-only sanitized shape, not accepted provider outcome evidence.
        journal = {"actions": [{"record_id": str(i), "outcome": status, "payload": "x" * 1000}
                               for i, status in enumerate(("PASS", "FAILED", "CANCELED", "UNKNOWN", "pending"))],
                   "units": {}, "counters": {"decisions": {"ALLOW": 5}, "failures": 1}}
        physical, chunks = compact_journal(journal)
        self.assertTrue(all(is_reference(row) for row in physical["actions"][:3]))
        self.assertEqual(physical["actions"][3:], journal["actions"][3:])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governor.json"
            publish_journal(path, journal, limit=65536)
            import json
            self.assertEqual(load_journal_history(path, json.loads(path.read_text())), journal)
        self.assertEqual(physical["counters"], journal["counters"])

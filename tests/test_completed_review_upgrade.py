"""The sanitized pre-upgrade admission shape: consume its review after sixteen receipts."""

from copy import deepcopy
import unittest

from pod.ledger import read
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal
from tests.records_support import start_before_reservations


class PreUpgradeReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Preparing retained history is shared fixture work, never a timed table or a sleep.
        cls.case = case = ProductionCase()
        case.setUp(); cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        cls.waiting = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        for index in range(16):
            frozen = case.packet(["A"], role="review")
            admission = case.start(f"review-{index}", frozen)["admission"]; case.settle(admission)
            case.report(admission, frozen, map={"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1,
                                               "update": {"A": cls.waiting}})
        cls.retained = deepcopy(next(row for row in case.stored() if row["id"] == "A")["receipts"])
        cls.frozen = case.packet(["A"], role="review")
        cls.admission = start_before_reservations(case, "pre-upgrade", cls.frozen)["admission"]
        case.settle(cls.admission)

    def test_preexisting_review_consumes_after_upgrade_without_another_admission(self):
        case = self.case
        before = read(case.project, "objective")
        self.assertEqual(len(self.retained), 16)
        case.report(self.admission, self.frozen,
                    map={"seq": before["checkpoint"]["seq"] + 1, "update": {"A": self.waiting}},
                    triage=[{"finding": "F1", "severity": "minor", "triage": "advisory", "summary": "retained finding"}])
        after = read(case.project, "objective")
        row = next(row for row in after["checkpoint"]["obligations"] if row["id"] == "A")
        self.assertEqual(len(row["receipts"]), 17)
        self.assertEqual(row["receipts"][:16], self.retained)
        self.assertEqual(set(after["admissions"]), set(before["admissions"]))
        self.assertEqual(len(row["findings"]), 1)


class RetainedHistoryDisplayTests(unittest.TestCase):
    def test_map_and_status_show_frozen_receipt_and_finding_counts_without_writes(self):
        from pod.status import status
        from pod.ledger import objective_root
        case = ProductionCase(); case.setUp(); self.addCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        frozen = case.packet(["A"], role="review")
        admission = case.start("review", frozen)["admission"]; case.settle(admission)
        waiting = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        case.report(admission, frozen, map={"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1, "update": {"A": waiting}},
                    triage=[{"finding": "F1", "severity": "minor", "triage": "advisory", "summary": "kept"}])
        case.write(renew=[{"id": "A", "as": "A2"}])
        rows = case.stored()
        replacement = next(row for row in rows if row["id"] == "A2")
        replacement.pop("wait")
        replacement.update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scoped waiver"})
        case.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Waive the remaining audit"})
        path = objective_root(case.project, "objective") / "context.json"
        before = path.read_bytes()
        mapped = case.op("map")
        shown = status(case.project, None, objective="objective")
        expected = [{"id": "A", "state": "renewed", "retained_receipts": 1, "retained_findings": 1},
                    {"id": "A2", "state": "withdrawn", "retained_receipts": 0, "retained_findings": 0}]
        self.assertEqual(mapped["retained_history"], expected)
        self.assertEqual(shown["obligations"]["retained_history"], expected)
        self.assertTrue(any("1 receipt(s), 1 finding(s) retained" in line for line in shown["obligation_lines"]))
        self.assertEqual(path.read_bytes(), before)

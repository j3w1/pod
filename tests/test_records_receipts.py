"""Receipt admission reservations and preceding report overflow boundaries."""
from copy import deepcopy
import json
import unittest

from pod.errors import PodError
from pod.ledger import _read, objective_root
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal
from tests.records_support import start_before_reservations


class ReceiptBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        cls.path = objective_root(case.project, "objective") / "context.json"
        frozen = case.packet(["A"], role="review")
        admission = case.start("first-receipt", frozen)["admission"]; case.settle(admission)
        state = _read(cls.path)
        waiting = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        renew = [{"id": "A", "as": "A2", **waiting}]
        case.report(admission, frozen, map={"seq": state["checkpoint"]["seq"] + 1, "update": {"A": waiting}})
        # Seed retained observations in four bounded checkpoint writes. They are
        # deliberately not proof; only the first and overflowing reports cross
        # the native review boundary. This keeps the shared fixture cheap.
        for start in range(0, 63, 16):
            case.write(seq=_read(cls.path)["checkpoint"]["seq"] + 1, update={"A": {
                "evidence": [{"attempt": f"retained-{index}"} for index in range(start, min(start + 16, 63))]}})
        state = _read(cls.path)
        case.write(seq=state["checkpoint"]["seq"] + 1, update={"A": {"evidence": [{"attempt": admission["admission_id"]}]}})
        cls.replay_count = len(next(row for row in case.stored() if row["id"] == "A")["receipts"])
        cls.frozen = case.packet(["A"], role="review")
        cls.admission = start_before_reservations(case, "preceding-overflow", cls.frozen)["admission"]; case.settle(cls.admission)
        state = _read(cls.path)
        cls.map = {"seq": state["checkpoint"]["seq"] + 1, "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}}
        cls.before = cls.files()
        proposed = deepcopy(cls.map)
        proposed["update"]["A"]["evidence"] = [{"attempt": cls.admission["admission_id"]}]
        case.write(**proposed)
        cls.checkpoint_receipts = deepcopy(next(row for row in case.stored() if row["id"] == "A")["receipts"])
        cls.map["seq"] = _read(cls.path)["checkpoint"]["seq"] + 1
        case.report(cls.admission, cls.frozen, map=cls.map)
        cls.report_receipts = deepcopy(next(row for row in case.stored() if row["id"] == "A")["receipts"])
        # Preceding overcommitment still refuses without discarding the review.
        before = cls.files()
        try:
            case.write(renew=renew, dispositions=[{"admission": cls.admission["admission_id"],
                "discarded": True, "reason": "history is full"}])
        except PodError as exc:
            cls.discard_refusal = exc
        else:
            raise AssertionError("preceding review was discarded")
        cls.discard_atomic = cls.files() == before
        # Renewal remains an explicit semantic transition; storage growth did
        # not require it and the accepted report already has a durable receipt.
        case.write(renew=renew)
        cls.renewed = _read(cls.path)

    @classmethod
    def files(cls):
        return {str(path.relative_to(cls.path.parent)): path.read_bytes() for path in cls.path.parent.rglob("*") if path.is_file() and path.name != ".lock"}

    def test_65th_checkpoint_and_report_preserve_every_earlier_receipt(self):
        self.assertEqual(self.replay_count, 64)
        self.assertEqual(len(self.checkpoint_receipts), 65)
        self.assertEqual(len(self.report_receipts), 65)
        self.assertEqual(self.checkpoint_receipts[:64], self.report_receipts[:64])
        self.assertIn("reported_seq", self.report_receipts[-1])

    def test_historical_review_cannot_be_discarded(self):
        self.assertEqual(self.discard_refusal.detail["detail"], "disposition_invalid")
        self.assertTrue(self.discard_atomic)

    def test_consumption_then_renewal_keeps_history_without_replacement_proof(self):
        row = self.renewed["admissions"][self.admission["admission_id"]]
        self.assertIsNone(row["disposition"])
        self.assertEqual(row["report"]["outcome"], "succeeded")
        rows = {row["id"]: row for row in self.renewed["checkpoint"]["obligations"]}
        self.assertEqual(len(rows["A"]["receipts"]), 65)
        self.assertEqual(rows["A2"]["receipts"], [])
        self.assertEqual(rows["A2"]["state"], "waiting")
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.case.start("repeat-on-predecessor", self.frozen)
        self.assertEqual(caught.exception.detail["detail"], "renewed")
        self.assertEqual(self.files(), before)

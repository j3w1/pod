"""Report-ingested receipt exhaustion and the capacity-only disposition recipe."""
from copy import deepcopy
import json
import unittest

from pod.errors import PodError
from pod.ledger import _read, objective_root
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal


class ReceiptBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        cls.path = objective_root(case.project, "objective") / "context.json"
        for index in range(64):
            frozen = case.packet(["A"], role="review")
            admission = case.start(f"receipt-{index}", frozen)["admission"]; case.settle(admission)
            state = _read(cls.path)
            if index == 0:
                native = admission["native_binding"]
                body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"], "attempt": native["dispatchId"],
                    "candidate": case.candidate, "outcome": "succeeded", "scope": ["src"], "files": [], "checks": ["unit"],
                    "failures": [], "evidence": [], "uncertainty": [], "questions": []}
                probe_map = {"seq": state["checkpoint"]["seq"] + 1,
                    "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}}
                before = cls.files()
                try:
                    case.write(**probe_map, dispositions=[{"admission": admission["admission_id"], "discarded": True, "reason": "capacity probe",
                        "capacity": {"packet": frozen, "report": body, "map": probe_map}}])
                except PodError as exc:
                    cls.consumable_refusal = exc
                else:
                    raise AssertionError("consumable report was discarded")
                cls.consumable_atomic = cls.files() == before
            case.report(admission, frozen, map={"seq": state["checkpoint"]["seq"] + 1,
                "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
        state = _read(cls.path)
        case.write(seq=state["checkpoint"]["seq"] + 1, update={"A": {"evidence": [{"attempt": admission["admission_id"]}]}})
        cls.replay_count = len(next(row for row in case.stored() if row["id"] == "A")["receipts"])
        cls.frozen = case.packet(["A"], role="review")
        cls.admission = case.start("overflow", cls.frozen)["admission"]; case.settle(cls.admission)
        state = _read(cls.path)
        cls.map = {"seq": state["checkpoint"]["seq"] + 1, "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}}
        cls.before = cls.files()
        proposed = deepcopy(cls.map)
        proposed["update"]["A"]["evidence"] = [{"attempt": cls.admission["admission_id"]}]
        try:
            case.write(**proposed)
        except PodError as exc:
            cls.checkpoint_refusal = exc
        else:
            raise AssertionError("65th checkpoint receipt was accepted")
        cls.checkpoint_atomic = cls.files() == cls.before
        try:
            case.report(cls.admission, cls.frozen, map=cls.map)
        except PodError as exc:
            cls.refusal = exc
        else:
            raise AssertionError("65th report receipt was accepted")
        cls.unchanged = cls.files() == cls.before
        # The same observation is supplied to the existing disposition route;
        # its dry run decides whether capacity actually blocks consumption.
        native = cls.admission["native_binding"]
        body = {"schema": "pod-report/v1", "assignment": cls.frozen["packet_id"], "attempt": native["dispatchId"],
            "candidate": case.candidate, "outcome": "succeeded", "scope": ["src"], "files": [], "checks": ["unit"],
            "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        case.write(seq=cls.map["seq"], update=cls.map["update"], dispositions=[{"admission": cls.admission["admission_id"],
            "discarded": True, "reason": "receipt capacity prevents consuming this report", "capacity": {
                "packet": cls.frozen, "report": body, "map": cls.map}}])
        cls.discarded = _read(cls.path)
        case.write(renew=[{"id": "A", "as": "A2"}])
        cls.renewed = _read(cls.path)
        print("records_receipts=" + json.dumps({"retained": 64, "proposed": 65, "limit": 64, "replay_count": cls.replay_count,
            "before_context_bytes": len(cls.before["context.json"]), "before_chunk_bytes": sum(len(value) for key, value in cls.before.items() if key.startswith("history/")),
            "report_refusal_unchanged": cls.unchanged, "checkpoint_refusal_unchanged": cls.checkpoint_atomic,
            "after_renewal_context_bytes": cls.path.stat().st_size, "after_renewal_chunk_bytes": sum(len(value) for key, value in cls.files().items() if key.startswith("history/")),
            "renewed_old_receipts": len(next(row for row in cls.renewed["checkpoint"]["obligations"] if row["id"] == "A")["receipts"]),
            "replacement_receipts": len(next(row for row in cls.renewed["checkpoint"]["obligations"] if row["id"] == "A2")["receipts"])}))

    @classmethod
    def files(cls):
        return {str(path.relative_to(cls.path.parent)): path.read_bytes() for path in cls.path.parent.rglob("*") if path.is_file() and path.name != ".lock"}

    def test_65th_report_refuses_atomically_with_exact_renewal_request(self):
        detail = self.refusal.detail
        self.assertEqual(detail["detail"], "receipt_limit")
        self.assertEqual({key: detail["referent"][key] for key in ("retained", "proposed", "limit")}, {"retained": 64, "proposed": 65, "limit": 64})
        self.assertEqual(detail["next_action"]["renew"][0]["id"], "A")
        self.assertTrue(self.unchanged)
        self.assertEqual(self.checkpoint_refusal.detail["detail"], "receipt_limit")
        self.assertTrue(self.checkpoint_atomic)
        self.assertEqual(self.replay_count, 64)

    def test_consumable_review_cannot_be_capacity_discarded(self):
        self.assertEqual(self.consumable_refusal.detail["detail"], "disposition_invalid")
        self.assertTrue(self.consumable_atomic)

    def test_capacity_disposition_preserves_report_and_renewal_has_no_proof(self):
        row = self.discarded["admissions"][self.admission["admission_id"]]
        self.assertEqual(row["disposition"]["capacity_refusal"]["detail"], "receipt_limit")
        self.assertEqual(row["disposition"]["report"]["observation"]["attempt"], self.admission["native_binding"]["dispatchId"])
        self.assertIsNone(row["report"])
        rows = {row["id"]: row for row in self.renewed["checkpoint"]["obligations"]}
        self.assertEqual(len(rows["A"]["receipts"]), 64)
        self.assertEqual(rows["A2"]["receipts"], [])
        self.assertEqual(rows["A2"]["state"], "waiting")

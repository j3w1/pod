"""Capacity is reserved before launch and retained through unconsumed settlement."""
from copy import deepcopy
import shutil
import unittest

from pod.errors import PodError
from pod.ledger import objective_root, read
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal
from tests.test_records_corrections import files, waiting


class ReceiptReservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        for start in range(0, 63, 16):
            case.write(seq=read(case.project, "objective")["checkpoint"]["seq"] + 1, update={"A": {
                "evidence": [{"attempt": f"retained-{index}"}
                             for index in range(start, min(start + 16, 63))]}})
        cls.root = objective_root(case.project, "objective")
        cls.baseline = files(case)
        cls.port = deepcopy(case.port.__dict__)

    def restore(self):
        shutil.rmtree(self.root)
        for relative, data in self.baseline.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.case.port.__dict__.clear()
        self.case.port.__dict__.update(deepcopy(self.port))

    def setUp(self):
        self.restore()

    def write(self, patch):
        self.case.write(seq=read(self.case.project, "objective")["checkpoint"]["seq"] + 1, update={"A": patch})

    def test_retained_unknown_receipts_allow_launch_and_failed_report(self):
        self.write({"evidence": [{"attempt": "last"}]})
        packet = self.case.packet(["A"], role="review")
        before = next(row for row in self.case.stored() if row["id"] == "A")["receipts"]
        admission = self.case.start("failed-outcome-probe", packet)["admission"]
        self.case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        self.case.report(admission, packet, outcome="failed", map={
            "seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1,
            "update": {"A": waiting(), "K": {"kind": "correction", "provenance": "coordinator", "parent": "A",
                "check": "repair real defect", **waiting()}}}, triage=[{"finding": "real", "severity": "major",
                "triage": "required_correction", "blocks": "outcome", "summary": "failed review finding", "correction": "K"}])
        row = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertEqual(row["receipts"][:64], before)
        self.assertEqual(len(row["receipts"]), 64)
        self.assertEqual(row["findings"][0]["correction"], "K")
        self.assertEqual(renewal.RenewalAssuranceTests.qualification(self.case), "WITHHELD")

    def test_sibling_result_states_never_evict_retained_observations(self):
        for state in ("running", "settled-success", "settled-failure", "consumed-success", "consumed-failure"):
            with self.subTest(state=state):
                self.restore()
                packet = self.case.packet(["A"], role="review")
                admission = self.case.start("sibling", packet)["admission"]
                failed = state.endswith("failure")
                if state != "running":
                    self.case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed" if failed else "succeeded"
                if state.startswith("consumed"):
                    self.case.report(admission, packet, outcome="failed" if failed else "succeeded", map={
                        "seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1, "update": {"A": waiting()}})
                old = deepcopy(next(row for row in self.case.stored() if row["id"] == "A")["receipts"])
                value = {"evidence": [{"attempt": "padding-last"}]}
                if state != "running": value.update(waiting())
                self.write(value)
                row = next(row for row in self.case.stored() if row["id"] == "A")
                self.assertEqual(row["receipts"][:-1], old)
                self.assertEqual(row["receipts"][-1]["evidence"]["attempt"], "padding-last")
                self.write({**value, "evidence": [{"attempt": "retained-0"}]})
                self.assertEqual(len(next(row for row in self.case.stored() if row["id"] == "A")["receipts"]), len(old) + 1)

    def test_multiple_pending_reviews_reserve_finding_work_separately(self):
        packet = self.case.packet(["A"], role="review")
        first = self.case.start("first", packet)["admission"]; self.case.settle(first)
        second = self.case.start("second", packet)["admission"]; self.case.settle(second)
        before = files(self.case)
        with self.assertRaises(PodError) as caught:
            self.case.start("third", packet)
        self.assertEqual(caught.exception.detail["detail"], "finding_limit")
        self.assertEqual(caught.exception.detail["referent"]["reserved"], 96)
        self.assertEqual(files(self.case), before)


class AcceptedRenewalAdviceTests(ProductionCase):
    def test_retention_does_not_mask_invalid_coordinator_sequence(self):
        self.intake(self.sub("S"))
        proof = self.proof("S")
        for start in range(0, 64, 16):
            value = {"evidence": [{**proof, "reference": f"retained-{index}"} for index in range(start, start + 16)]}
            if start == 48: value["state"] = "satisfied"
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={"S": value})
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={
                "S": {"evidence": [{**proof, "reference": "overflow"}]},
                "O1": {"state": "waiting", "wait": {"class": "sequenced", "referent": "S"}}})
        self.assertEqual(caught.exception.detail["detail"], "sequenced_without_active")
        self.assertEqual(files(self), before)


class FindingReservationTests(ProductionCase):
    def test_incremental_old_report_cannot_take_a_pending_reviews_finding_budget(self):
        renewal.RenewalAssuranceTests.policy(self)
        attempts = []
        for name in ("old", "second"):
            packet = self.packet(["A"], role="review")
            admission = self.start(name, packet)["admission"]
            self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
            self.report(admission, packet, outcome="failed", map={
                "seq": read(self.project, "objective")["checkpoint"]["seq"] + 1, "update": {"A": waiting()}},
                triage=[self.advisory(name)])
            attempts.append((admission, packet))
        packet = self.packet(["A"], role="review")
        pending = self.start("pending", packet)["admission"]
        admission, old_packet = attempts[0]
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.report(admission, old_packet, outcome="failed", triage=[self.advisory(f"late-{index}") for index in range(31)])
        detail = caught.exception.detail
        self.assertEqual(detail["detail"], "finding_limit")
        self.assertEqual({key: detail["referent"][key] for key in ("retained", "proposed", "reserved", "limit")},
                         {"retained": 2, "proposed": 65, "reserved": 32, "limit": 64})
        self.assertEqual(files(self), before)
        # Exactly 30 incremental entries fit alongside the pending review's 32.
        self.report(admission, old_packet, outcome="failed", triage=[self.advisory(f"late-{index}") for index in range(30)])
        self.settle(pending)
        self.report(pending, packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
            "update": {"A": waiting()}}, triage=[self.advisory(f"pending-{index}") for index in range(32)])
        assurance = next(row for row in self.stored() if row["id"] == "A")
        self.assertEqual(len(assurance["findings"]), 64)
        self.assertEqual(assurance["receipts"][0]["evidence"]["attempt"], pending["admission_id"])

    @staticmethod
    def advisory(key):
        return {"finding": key, "severity": "minor", "triage": "advisory", "summary": "review observation"}

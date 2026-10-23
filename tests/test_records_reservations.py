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

    def test_full_history_refuses_before_an_outcome_or_native_launch(self):
        self.write({"evidence": [{"attempt": "last"}]})
        packet = self.case.packet(["A"], role="review")
        before = files(self.case)
        with self.assertRaises(PodError) as caught:
            self.case.start("failed-outcome-probe", packet)
        detail = caught.exception.detail
        self.assertEqual(detail["detail"], "receipt_limit")
        self.assertEqual({key: detail["referent"][key] for key in ("retained", "proposed", "reserved", "limit")},
                         {"retained": 64, "proposed": 65, "reserved": 1, "limit": 64})
        action = detail["next_action"]
        self.assertEqual(action, {"seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1,
            "renew": [{"id": "A", "as": "<fresh-id>", **waiting()}]})
        self.assertEqual(files(self.case), before)
        self.assertEqual(self.case.port.workers, self.port["workers"])
        action["renew"][0]["as"] = "A2"
        self.case.write(**action)
        packet = self.case.packet(["A2"], role="review")
        admission = self.case.start("renewed-failed-review", packet)["admission"]
        self.case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        self.case.report(admission, packet, outcome="failed", map={
            "seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1,
            "update": {"A2": waiting(), "K": {"kind": "correction", "provenance": "coordinator", "parent": "A2",
                "check": "repair real defect", **waiting()}}}, triage=[{"finding": "real", "severity": "major",
                "triage": "required_correction", "summary": "failed review finding", "correction": "K"}])
        rows = {row["id"]: row for row in self.case.stored()}
        self.assertEqual((len(rows["A"]["receipts"]), rows["A2"]["receipts"], len(rows["A2"]["findings"])), (64, [], 1))
        self.assertEqual(renewal.RenewalAssuranceTests.qualification(self.case), "WITHHELD")

    def test_sibling_result_states_reserve_until_consumed(self):
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
                before = files(self.case)
                patch = {"evidence": [{"attempt": "padding-last"}]}
                if state != "running":
                    patch.update(waiting())
                if state == "consumed-failure":
                    self.write(patch)  # Failed consumption releases the unused receipt.
                    self.assertEqual(len(next(row for row in self.case.stored() if row["id"] == "A")["receipts"]), 64)
                else:
                    with self.assertRaises(PodError) as caught:
                        self.write(patch)
                    detail = caught.exception.detail
                    self.assertEqual(detail["detail"], "receipt_limit")
                    self.assertEqual(detail["referent"]["reserved"], 0 if state.startswith("consumed") else 1)
                    self.assertEqual(files(self.case), before)
                # Replaying retained evidence spends no reserved slot.
                patch["evidence"] = [{"attempt": "retained-0"}]
                self.write(patch)

    def test_multiple_pending_reviews_reserve_separately(self):
        packet = self.case.packet(["A"], role="review")
        first = self.case.start("first", packet)["admission"]; self.case.settle(first)
        before = files(self.case)
        with self.assertRaises(PodError) as caught:
            self.case.start("second", packet)
        self.assertEqual(caught.exception.detail["detail"], "receipt_limit")
        self.assertEqual(caught.exception.detail["referent"]["reserved"], 2)
        self.assertEqual(files(self.case), before)


class AcceptedRenewalAdviceTests(ProductionCase):
    def test_receipt_refusal_uses_accepted_coordinator_slot(self):
        self.intake(self.sub("S"))
        proof = self.proof("S")
        for start in range(0, 64, 16):
            patch = {"evidence": [{**proof, "reference": f"retained-{index}"} for index in range(start, start + 16)]}
            if start == 48:
                patch["state"] = "satisfied"
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={"S": patch})
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={
                "S": {"evidence": [{**proof, "reference": "overflow"}]},
                "O1": {"state": "waiting", "wait": {"class": "sequenced", "referent": "S"}}})
        self.assertEqual(caught.exception.detail["detail"], "receipt_limit")
        action = caught.exception.detail["next_action"]
        self.assertEqual(action, {"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
            "renew": [{"id": "S", "as": "<fresh-id>", **waiting()}]})
        self.assertEqual(files(self), before)
        action["renew"][0]["as"] = "S2"
        self.write(**action)
        rows = {row["id"]: row for row in self.stored()}
        self.assertEqual(rows["O1"]["executor"], "coordinator")
        self.assertEqual(rows["S2"]["wait"], waiting()["wait"])


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

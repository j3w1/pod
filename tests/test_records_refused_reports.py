"""A route-mismatched review keeps its reservation and renewal hold (accepted, documented limitation)."""
from copy import deepcopy
import shutil
import unittest

from pod.assurance import pending_serving
from pod.errors import PodError
from pod.ledger import objective_root, read
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal
from tests.test_records_corrections import files, waiting


class RefusedReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        packet = case.packet(["A"], role="review")
        admission = case.start("earlier", packet)["admission"]
        case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        case.report(admission, packet, outcome="failed", map={
            "seq": read(case.project, "objective")["checkpoint"]["seq"] + 1, "update": {"A": waiting()}},
            triage=[{"finding": "earlier", "severity": "minor", "triage": "advisory", "summary": "retained observation"}])
        cls.root = objective_root(case.project, "objective")
        cls.baseline = files(case)
        cls.port = deepcopy(case.port.__dict__)

    def setUp(self):
        shutil.rmtree(self.root)
        for relative, data in self.baseline.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.case.port.__dict__.clear()
        self.case.port.__dict__.update(deepcopy(self.port))

    def report_map(self, **patch):
        row = {**waiting(), **patch}
        if row["state"] != "waiting":
            row.pop("wait", None)
        return {"seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1,
                "update": {"A": row}}

    def refused_review(self):
        packet = self.case.packet(["A"], role="review")
        admission = self.case.start("route-mismatch", packet)["admission"]; self.case.settle(admission)
        worker = self.case.port.workers[admission["native_binding"]["dispatchId"]]
        worker["route"] = {**worker["route"], "model": "gpt-6-luna"}
        for outcome in ("succeeded", "failed"):
            with self.assertRaises(PodError) as caught:
                self.case.report(admission, packet, outcome=outcome, map=self.report_map())
            self.assertEqual(caught.exception.code, "route_mismatch")
        state = read(self.case.project, "objective")
        rejected = state["admissions"][admission["admission_id"]]
        self.assertTrue(rejected["route_decision"]["route_mismatch"])
        self.assertIsNone(rejected["report"])
        self.assertIsNone(rejected["disposition"])
        return rejected, packet

    def test_recorded_mismatch_keeps_its_reservation_and_renewal_hold(self):
        # Accepted limitation (R150): a recorded route mismatch is not proof
        # that Pod will refuse every later report, so the review keeps its
        # slots; the Governor already holds such an objective for its route
        # decision.
        rejected, packet = self.refused_review()
        before = files(self.case)
        assurance = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertIn(rejected["admission_id"], pending_serving(assurance, {"admissions": read(self.case.project, "objective")["admissions"]}))
        with self.assertRaises(PodError) as caught:
            self.case.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        self.assertEqual(caught.exception.detail["detail"], "renewal_result_pending")
        self.assertEqual(caught.exception.detail["referent"]["admission"], rejected["admission_id"])
        self.assertEqual(files(self.case), before)

    def test_retained_receipt_growth_does_not_clear_a_recorded_mismatch(self):
        for start in range(0, 63, 16):
            self.case.write(**self.report_map(evidence=[{"attempt": f"retained-{index}"}
                for index in range(start, min(start + 16, 63))]))
        rejected, _ = self.refused_review()
        self.case.write(**self.report_map(evidence=[{"attempt": "last"}]))
        state = read(self.case.project, "objective")
        row = state["admissions"][rejected["admission_id"]]
        self.assertTrue(row["route_decision"]["route_mismatch"])
        self.assertIsNone(row["report"])
        self.assertIsNone(row["disposition"])
        self.assertEqual(len(next(row for row in self.case.stored() if row["id"] == "A")["receipts"]), 64)

    def test_consumable_review_keeps_its_slot_and_records_its_finding(self):
        # Baseline for the hold tests above: an ordinary review keeps its
        # reserved slot and its finding is recorded on the row it served. The
        # route-mismatch hold itself is pinned by the sibling table below.
        packet = self.case.packet(["A"], role="review")
        admission = self.case.start("consumable", packet)["admission"]; self.case.settle(admission)
        before = len(next(row for row in self.case.stored() if row["id"] == "A")["findings"])
        self.case.report(admission, packet, map=self.report_map(),
                         triage=[{"finding": "kept", "severity": "minor", "triage": "advisory",
                                  "summary": "finding keeps its reserved slot"}])
        assurance = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertEqual(len(assurance["findings"]), before + 1)

    def test_failed_without_report_names_consumption_and_keeps_renewal_blocked(self):
        packet = self.case.packet(["A"], role="review")
        admission = self.case.start("failed-no-report", packet)["admission"]
        self.case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        before = files(self.case)
        assurance = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertIn(admission["admission_id"], pending_serving(assurance, {"admissions": read(self.case.project, "objective")["admissions"]}))
        with self.assertRaises(PodError) as caught:
            self.case.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        self.assertEqual(caught.exception.detail["detail"], "renewal_result_pending")
        self.assertEqual(caught.exception.detail["referent"]["admission"], admission["admission_id"])
        self.assertEqual(caught.exception.detail["next_action"],
                         f"settle and consume the report for admission {admission['admission_id']} before retrying renewal")
        self.assertEqual(files(self.case), before)
        self.case.report(admission, packet, outcome="failed", map=self.report_map())
        replacement = self.case.start("replacement", packet)["admission"]
        self.assertEqual(replacement["serves"], ["A"])

    def test_sibling_states_never_release_a_hold_without_consumption_or_disposition(self):
        # Only consumption, deferral or an ordinary disposition ends a hold; a
        # recorded route mismatch, uncertainty or native failure does not.
        base = {"serves": ["A"], "state": "bound", "report": None, "disposition": None}
        cases = [
            ("unconsumed", {}, True),
            ("recorded-mismatch", {"route_decision": {"route_mismatch": True}}, True),
            ("unknown-effective", {"route_decision": {"effective_unknown": True}}, True),
            ("false-mismatch", {"route_decision": {"route_mismatch": False}}, True),
            ("malformed-marker", {"route_decision": {"route_mismatch": "unknown"}}, True),
            ("recoverable-error", {"error": {"code": "native_identity_unverified"}}, True),
            ("native-failure", {"failures": [{"kind": "failed"}]}, True),
            ("consumed-failure", {"report": {"outcome": "failed"}}, False),
            ("deferred", {"state": "deferred"}, False),
            ("dispositioned", {"disposition": {"kind": "discarded"}}, False),
        ]
        for name, patch, expected in cases:
            with self.subTest(state=name):
                ctx = {"admissions": {"attempt": {**base, **patch}}, "outstanding": []}
                self.assertEqual(pending_serving({"id": "A"}, ctx), ["attempt"] if expected else [])

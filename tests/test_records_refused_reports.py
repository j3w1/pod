"""Recorded permanent report refusal releases only the assurance result hold."""
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

    def test_recorded_mismatch_after_retained_finding_allows_replacement_review(self):
        rejected, packet = self.refused_review()
        replacement = self.case.start("replacement", packet)["admission"]; self.case.settle(replacement)
        self.case.report(replacement, packet, map=self.report_map(
            state="satisfied", evidence=[{"attempt": replacement["admission_id"]}]))
        state = read(self.case.project, "objective")
        self.assertEqual(state["admissions"][rejected["admission_id"]], rejected)
        assurance = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertEqual((len(assurance["receipts"]), len(assurance["findings"])), (1, 1))
        self.assertNotIn(rejected["admission_id"], [row["evidence"]["attempt"] for row in assurance["receipts"]])
        self.assertEqual(renewal.RenewalAssuranceTests.qualification(self.case), "QUALIFIED")

    def test_recorded_mismatch_releases_the_reserved_checkpoint_receipt(self):
        for start in range(0, 63, 16):
            self.case.write(**self.report_map(evidence=[{"attempt": f"retained-{index}"}
                for index in range(start, min(start + 16, 63))]))
        rejected, _ = self.refused_review()
        self.case.write(**self.report_map(evidence=[{"attempt": "last"}]))
        assurance = next(row for row in self.case.stored() if row["id"] == "A")
        self.assertEqual(len(assurance["receipts"]), 64)
        self.assertEqual(read(self.case.project, "objective")["admissions"][rejected["admission_id"]], rejected)

    def test_recorded_mismatch_does_not_block_renewal_or_supply_proof(self):
        rejected, _ = self.refused_review()
        self.case.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        state = read(self.case.project, "objective")
        self.assertEqual(state["admissions"][rejected["admission_id"]], rejected)
        rows = {row["id"]: row for row in self.case.stored()}
        self.assertEqual((rows["A"]["state"], len(rows["A"]["findings"])), ("renewed", 1))
        self.assertEqual((rows["A2"]["receipts"], rows["A2"].get("findings", [])), ([], []))
        self.assertEqual(renewal.RenewalAssuranceTests.qualification(self.case), "WITHHELD")
        packet = self.case.packet(["A2"], role="review")
        replacement = self.case.start("renewed-review", packet)["admission"]
        self.assertEqual(replacement["serves"], ["A2"])

    def test_failed_without_report_names_consumption_and_keeps_renewal_blocked(self):
        packet = self.case.packet(["A"], role="review")
        admission = self.case.start("failed-no-report", packet)["admission"]
        self.case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        before = files(self.case)
        with self.assertRaises(PodError) as caught:
            self.case.start("replacement", packet)
        detail = caught.exception.detail
        self.assertEqual(detail["detail"], "finding_limit")
        self.assertEqual(detail["referent"]["admission"], admission["admission_id"])
        self.assertEqual(detail["next_action"], f"consume the report for admission {admission['admission_id']} before retrying this write")
        self.assertEqual(files(self.case), before)
        with self.assertRaises(PodError) as caught:
            self.case.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        self.assertEqual(caught.exception.detail["detail"], "renewal_result_pending")
        self.assertEqual(caught.exception.detail["referent"]["admission"], admission["admission_id"])
        self.assertEqual(caught.exception.detail["next_action"],
                         f"consume the report for admission {admission['admission_id']} before retrying renewal")
        self.assertEqual(files(self.case), before)
        self.case.report(admission, packet, outcome="failed", map=self.report_map())
        replacement = self.case.start("replacement", packet)["admission"]
        self.assertEqual(replacement["serves"], ["A"])

    def test_sibling_states_release_only_explicit_permanent_refusal(self):
        # No uncertainty, native error or failed outcome is itself proof that
        # Pod permanently refused a report. Existing consumed/deferred rules stay.
        base = {"serves": ["A"], "state": "bound", "report": None, "disposition": None}
        cases = [
            ("unconsumed", {}, True),
            ("recorded-mismatch", {"route_decision": {"route_mismatch": True}}, False),
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

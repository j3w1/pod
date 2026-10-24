"""Records audit regressions through disposable production boundaries."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import objective_root, read
from tests.issue41_support import ProductionCase
from tests import test_records_boundaries as renewal


def waiting():
    return {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


def report_body(case, admission, packet):
    return {"schema": "pod-report/v1", "assignment": packet["packet_id"],
            "attempt": admission["native_binding"]["dispatchId"], "candidate": case.candidate,
            "outcome": "succeeded", "scope": packet["body"]["scope"], "files": [], "checks": ["unit"],
            "failures": [], "evidence": [], "uncertainty": [], "questions": []}


def files(case):
    root = objective_root(case.project, "objective")
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and path.name != ".lock"}


class CapacityPaddingTests(ProductionCase):
    def test_stored_padding_cannot_spend_a_settled_reviews_reserved_receipt(self):
        renewal.RenewalAssuranceTests.policy(self)
        packet = self.packet(["A"], role="review")
        admission = self.start("honest-review", packet)["admission"]; self.settle(admission)
        for start in range(0, 63, 16):
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={"A": {
                **waiting(), "evidence": [{"attempt": f"retained-{index}"}
                    for index in range(start, min(start + 16, 63))]}})
        state = read(self.project, "objective")
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.write(seq=state["checkpoint"]["seq"] + 1,
                       update={"A": {"evidence": [{"attempt": "padding-last"}]}})
        detail = caught.exception.detail
        self.assertEqual(detail["detail"], "receipt_limit")
        self.assertEqual({key: detail["referent"][key] for key in ("retained", "proposed", "reserved", "limit")},
                         {"retained": 63, "proposed": 65, "reserved": 1, "limit": 64})
        self.assertEqual(detail["referent"]["admission"], admission["admission_id"])
        self.assertEqual(detail["next_action"], f"settle and consume the report for admission {admission['admission_id']} before retrying this write")
        self.assertEqual(files(self), before)
        for extra in ({}, {"capacity": {"packet": packet, "report": report_body(self, admission, packet)}}):
            with self.subTest(extra=tuple(extra)):
                with self.assertRaises(PodError) as caught:
                    self.write(renew=[{"id": "A", "as": "A2", **waiting()}], dispositions=[{
                        "admission": admission["admission_id"], "discarded": True, "reason": "capacity", **extra}])
                self.assertEqual(caught.exception.detail["detail"], "malformed" if extra else "disposition_invalid")
                self.assertEqual(files(self), before)
        with self.assertRaises(PodError) as caught:
            self.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        self.assertEqual(caught.exception.detail["detail"], "renewal_result_pending")
        self.assertEqual(files(self), before)
        self.report(admission, packet, map={"seq": state["checkpoint"]["seq"] + 1, "update": {
            "A": waiting(), "K": {"kind": "correction", "provenance": "coordinator", "parent": "A",
                "check": "repair the reported defects", **waiting()}}}, triage=[{
                "finding": f"real-{index}", "severity": "major", "triage": "required_correction",
                "summary": "real defect", "correction": "K"} for index in range(3)])
        assurance = next(row for row in self.stored() if row["id"] == "A")
        self.assertEqual((len(assurance["receipts"]), len(assurance["findings"])), (64, 3))
        self.assertEqual(renewal.RenewalAssuranceTests.qualification(self), "WITHHELD")


class InvestigationCapacityTests(ProductionCase):
    def test_full_ordinary_history_never_allows_investigation_discard(self):
        self.intake(self.sub("S"))
        proof = self.proof("S")
        for start in range(0, 64, 16):
            self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, update={"S": {
                "evidence": [{**proof, "reference": f"retained-{index}"}
                             for index in range(start, start + 16)]}})
        packet = self.packet(["S"], role="investigate", resolves="which boundary failed?", stop_condition="answer")
        admission = self.start("investigate", packet)["admission"]; self.settle(admission)
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.write(dispositions=[{"admission": admission["admission_id"], "discarded": True,
                "reason": "full retained receipts"}], renew=[{"id": "S", "as": "S2", **waiting()}])
        self.assertEqual(caught.exception.detail["detail"], "disposition_invalid")
        self.assertEqual(files(self), before)
        self.report(admission, packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                           "update": {"S": waiting()}})
        self.write(renew=[{"id": "S", "as": "S2", **waiting()}])
        rows = {row["id"]: row for row in self.stored()}
        self.assertEqual((rows["S"]["state"], len(rows["S"]["receipts"])), ("renewed", 64))
        self.assertEqual(rows["S2"]["receipts"], [])


class AdmissionLineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        packet = case.packet(["A"], role="review")
        admission = case.start("original-review", packet)["admission"]; case.settle(admission)
        case.report(admission, packet, map={"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1,
            "update": {"A": waiting(), "K": {"kind": "correction", "provenance": "coordinator", "parent": "A",
                "check": "repair", **waiting()}}}, triage=[{"finding": "F", "severity": "major",
                "triage": "required_correction", "summary": "defect", "correction": "K"}])
        case.write(seq=read(case.project, "objective")["checkpoint"]["seq"] + 1,
                   update={"K": {"state": "satisfied", "evidence": [case.proof("K")]}})
        case.write(renew=[{"id": "A", "as": "A2"}])
        packet = case.packet(["A2"], role="review")
        cls.review = case.start("tip-review", packet)["admission"]; case.settle(cls.review)
        case.report(cls.review, packet, map={"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1,
            "update": {"A2": {"state": "satisfied", "evidence": [{"attempt": cls.review["admission_id"]}]}}})
        cls.saved, cls.port = files(case), deepcopy(case.port.__dict__)

    def test_reopening_or_renewing_predecessor_correction_holds_review_until_proved(self):
        case = self.case
        root = objective_root(case.project, "objective")
        for route in ("update", "renew"):
            with self.subTest(route=route):
                for path in root.rglob("*"):
                    if path.is_file() and path.name != ".lock":
                        path.unlink()
                for name, data in self.saved.items():
                    path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
                case.port.__dict__.clear(); case.port.__dict__.update(deepcopy(self.port))
                value = {"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1, "update": {"A2": waiting()}}
                if route == "update":
                    value["update"]["K"] = waiting()
                else:
                    value["renew"] = [{"id": "K", "as": "K2", **waiting()}]
                case.write(**value)
                before = files(case)
                with self.assertRaises(PodError) as caught:
                    case.write(seq=read(case.project, "objective")["checkpoint"]["seq"] + 1,
                        update={"A2": {"state": "satisfied", "evidence": [{"attempt": self.review["admission_id"]}]}})
                self.assertEqual(caught.exception.detail["detail"], "evidence_invalidated")
                self.assertEqual(files(case), before)
                packet = case.packet(["A2"], role="review")
                with self.assertRaises(PodError) as caught:
                    case.start("fresh-" + route, packet)
                self.assertEqual(caught.exception.code, "review_precedes_resolution")
                self.assertEqual(files(case), before)
                key = "K" if route == "update" else "K2"
                case.write(seq=read(case.project, "objective")["checkpoint"]["seq"] + 1,
                           update={key: {"state": "satisfied", "evidence": [case.proof(key, reference="fresh-" + route)]}})
                admitted = case.start("fresh-" + route, packet)["admission"]
                self.assertEqual(admitted["serves"], ["A2"])


class LineageReadTests(unittest.TestCase):
    def test_persisted_rows_share_one_lineage_for_all_finding_checks(self):
        from pod.lineage import Lineage
        from pod.obligations import accept_write, _persisted_rows
        from tests.test_obligations import ctx, intake, ob
        state = accept_write(None, intake(*[ob(f"S{index}") for index in range(64)]), ctx())
        state = accept_write(state, {"renew": [{"id": f"S{index}", "as": f"N{index}"} for index in range(16)]}, ctx())
        original, builds = Lineage.__init__, []
        def build(instance, rows):
            builds.append(len(rows)); original(instance, rows)
        with patch.object(Lineage, "__init__", build):
            _persisted_rows(state, ctx())
        self.assertEqual(builds, [81])


class TerminalCitationTests(ProductionCase):
    def test_renewal_in_adoption_or_refresh_checkpoint_relocates_generated_policy_tip(self):
        from pod.lineage import copied_identity
        from tests import kernel_support as boundaries
        renewal.RenewalAssuranceTests.policy(self)
        for old, new, path, content, refresh in (
                ("A", "A2", "README.md", "sibling work\n", False),
                ("A2", "A3", "AGENTS.md", "Inserted introduction.\n" + boundaries.AGENTS, True)):
            with self.subTest(refresh=refresh):
                (self.project / path).write_text(content)
                boundaries.git(self.project, "add", path)
                boundaries.git(self.project, "commit", "-qm", "advance target before renewal")
                target = boundaries.git(self.project, "rev-parse", "HEAD")
                boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
                fields = {"governance_refresh": True,
                          "revision_authority": {"provenance": "user_direct", "instruction": "Adopt the target policy"}} if refresh else {}
                self.write(renew=[{"id": old, "as": new}], **fields)
                rows = {row["id"]: row for row in self.stored()}
                for key in (old, new):
                    self.assertEqual(rows[key]["source"]["base"], target)
                self.assertEqual(rows[old]["renewal"]["definition"], copied_identity(rows[new]))
                self.assertEqual(rows[new]["receipts"], [])

    def test_terminal_policy_citations_follow_adoption_and_refresh_without_changing_identity(self):
        from pod.lineage import copied_identity
        from tests import kernel_support as boundaries
        renewal.RenewalAssuranceTests.policy(self)
        second = boundaries.governance_policy(self, "B")
        second["uncovered_risk"] = "separate policy audit"
        self.write([*self.stored(), second])
        self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1,
            update={"B": {"state": "withdrawn", "withdrawal": {"by": "user_direct", "reason": "waived"}}},
            revision_authority={"provenance": "user_direct", "instruction": "Waive B"})
        self.write(renew=[{"id": "A", "as": "A2"}])
        original = {row["id"]: row for row in self.stored()}
        for path, content, refresh in (("README.md", "sibling work\n", False),
                ("AGENTS.md", "Inserted introduction.\n" + boundaries.AGENTS, True)):
            with self.subTest(refresh=refresh):
                (self.project / path).write_text(content)
                boundaries.git(self.project, "add", path)
                boundaries.git(self.project, "commit", "-qm", "advance target")
                target = boundaries.git(self.project, "rev-parse", "HEAD")
                boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
                fields = {"governance_refresh": True,
                          "revision_authority": {"provenance": "user_direct", "instruction": "Adopt the target policy"}} if refresh else {}
                self.write(**fields)
                rows = {row["id"]: row for row in self.stored()}
                for key in ("A", "B"):
                    self.assertEqual(rows[key]["source"]["base"], target)
                    self.assertEqual(copied_identity(rows[key]), copied_identity(original[key]))
                    self.assertEqual(rows[key]["receipts"], original[key]["receipts"])
                self.assertEqual(rows["A"]["renewal"]["definition"], copied_identity(rows["A"]))
                self.assertEqual(rows["B"]["withdrawal"], original["B"]["withdrawal"])

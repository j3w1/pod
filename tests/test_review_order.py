"""Review admission and backward-readable proof chronology through production boundaries."""

from copy import deepcopy
import unittest

from pod.assurance import evidence_valid, invalidation, review_ordering
from pod.ledger import kernel_context, kernel_view, objective_root, read
from pod.obligations import admit, definition_id
from tests import kernel_support as boundaries
from tests.issue41_support import ProductionCase
from tests.records_support import start_before_review_guard


WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


class ReviewOrderBoundaryTests(ProductionCase):
    def setUp(self):
        super().setUp()
        (self.project / "AGENTS.md").write_text(boundaries.AGENTS)
        boundaries.git(self.project, "add", "AGENTS.md")
        boundaries.git(self.project, "commit", "-qm", "fixture audit policy")
        self.base = self.candidate = boundaries.git(self.project, "rev-parse", "HEAD")
        boundaries.git(self.project, "branch", "-f", "target", self.base)
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.intake(boundaries.governance_policy(self, "A"))

    def update(self, rows, **fields):
        return self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1,
                          update=rows, **fields)

    def findings(self, *, grouped=False, advisory=False):
        packet = self.packet(["A"], role="review")
        admission = self.start("find-defect", packet)["admission"]
        self.settle(admission)
        finding = {"summary": "repair required", "triage": "required_correction", "correction": "K"}
        if grouped:
            finding["findings"] = [{"finding": "F1", "severity": "major"}, {"finding": "F2", "severity": "minor"}]
        else:
            finding.update(finding="F1", severity="major")
        updates = {"A": WAITING, "K": {"kind": "correction", "provenance": "coordinator", "parent": "A",
                                      "check": "repair the defect", **WAITING}}
        if advisory:
            finding = {"finding": "note", "severity": "minor", "summary": "optional note", "triage": "advisory"}
            updates.pop("K")
        self.report(admission, packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                           "update": updates}, triage=[finding])

    def files(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}

    def refuse_start(self, packet, *, accompanying=None, correction="K", findings=("F1",)):
        before, starts = self.files(), deepcopy(self.port.starts)
        error = self.refused("review_precedes_resolution", "review_precedes_resolution",
                             self.start, "verify", packet, accompanying=accompanying)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.port.starts, starts)
        detail = error.detail
        named = detail["referent"]["findings"][0]
        self.assertEqual(named["correction"], correction)
        self.assertEqual(named.get("findings", [named["finding"]]), list(findings))
        self.assertIn("satisfy", detail["next_action"])
        self.assertIn("then admit", detail["next_action"])
        return error

    def finish_review(self, packet):
        review = self.start("verify", packet)["admission"]
        self.settle(review)
        served = packet["body"]["serves"][0]
        self.report(review, packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                        "update": {served: WAITING}})
        self.update({served: {"state": "satisfied", "evidence": [{"attempt": review["admission_id"]}]}})
        self.assertEqual(kernel_view(self.project, "objective", state=read(self.project, "objective"))["label"]["label"], "QUALIFIED")
        return review

    def test_refusal_is_atomic_then_fixer_proof_permits_the_same_review(self):
        self.findings()
        packet = self.packet(["A"], role="review")
        self.refuse_start(packet)
        fixer_packet = self.packet(["K"], boundary={"paths": ["src"]})
        fixer = self.start("fix", fixer_packet)["admission"]
        self.settle(fixer)
        self.report(fixer, fixer_packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                            "update": {"K": WAITING}})
        self.refuse_start(packet)
        self.update({"K": {"state": "satisfied", "evidence": [self.proof("K")]}},
                    dispositions=[{"admission": fixer["admission_id"], "integrated_into": self.candidate,
                                   "attestation": "the consumed fixture result is integrated"}])
        review = self.finish_review(packet)
        assurance = next(row for row in self.stored() if row["id"] == "A")
        self.assertGreater(review["admitted_seq"], assurance["findings"][0]["resolved_seq"])

    def test_admission_map_cannot_supply_unaccepted_resolution(self):
        self.findings()
        packet = self.packet(["A"], role="review")
        rows = self.stored()
        correction = next(row for row in rows if row["id"] == "K")
        correction.pop("wait")
        correction.update(state="satisfied", evidence=[self.proof("K")])
        accompanying = {"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1, "obligations": rows}
        self.refuse_start(packet, accompanying=accompanying)
        state = read(self.project, "objective")
        ctx = kernel_context(self.project, "objective", state, None)
        ctx["outstanding"] = []  # The injected native port settled the only earlier review.
        self.refused("review_precedes_resolution", "review_precedes_resolution", admit,
                     state["checkpoint"], packet["body"], ctx, admission_id="direct", accompanying=accompanying)

    def test_grouped_findings_follow_renewed_assurance_and_correction(self):
        self.findings(grouped=True)
        self.write(renew=[{"id": "A", "as": "A2"}])
        self.write(renew=[{"id": "K", "as": "K2"}])
        packet = self.packet(["A2"], role="review")
        self.refuse_start(packet, correction="K2", findings=("F1", "F2"))
        self.update({"K2": {"state": "satisfied", "evidence": [self.proof("K2")]}})
        self.finish_review(packet)

    def test_advisory_findings_do_not_block_admission(self):
        self.findings(advisory=True)
        self.finish_review(self.packet(["A"], role="review"))

    def test_user_withdrawn_correction_does_not_block_admission(self):
        self.findings()
        self.update({"K": {"state": "withdrawn", "withdrawal": {"by": "user_direct", "reason": "scoped waiver"}}},
                    revision_authority={"provenance": "user_direct", "instruction": "Waive correction K"})
        self.finish_review(self.packet(["A"], role="review"))

    def test_old_admission_reports_ordering_at_report_and_checkpoint(self):
        self.findings()
        packet = self.packet(["A"], role="review")
        early = start_before_review_guard(self, "older-review", packet)["admission"]
        self.settle(early)
        self.update({"A": WAITING, "K": {"state": "satisfied", "evidence": [self.proof("K")]}})
        update = {"A": {"state": "satisfied", "evidence": [{"attempt": early["admission_id"]}]}}
        before = self.files()
        error = self.refused("obligation_unaccounted", "evidence_invalidated", self.report, early, packet,
                             map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1, "update": update})
        self.assertEqual(self.files(), before)
        diagnostic = error.detail["referent"]["invalidations"][0]
        self.assertEqual(diagnostic["classification"], "ordering")
        finding = diagnostic["ordering"]["findings"][0]
        self.assertEqual((finding["finding"], finding["admitted_seq"]), ("F1", early["admitted_seq"]))
        self.assertLess(finding["admitted_seq"], finding["resolved_seq"])
        self.assertGreater(finding["reported_seq"], finding["resolved_seq"])
        self.report(early, packet, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                       "update": {"A": WAITING}})
        before = self.files()
        error = self.refused("obligation_unaccounted", "evidence_invalidated", self.update, update)
        self.assertEqual(error.detail["referent"]["invalidations"][0]["ordering"]["findings"][0], finding)
        self.assertEqual(self.files(), before)

    def test_environment_change_retains_binding_change_classification(self):
        self.finish_review(self.packet(["A"], role="review"))
        before = self.files()
        error = self.refused("obligation_unaccounted", "evidence_invalidated", self.write,
                             verification={**self.core()["verification"], "environment": "another runner"})
        self.assertEqual(error.detail["referent"]["invalidations"][0]["classification"], "binding change")
        self.assertEqual(self.files(), before)


class ReviewSequenceTests(unittest.TestCase):
    def fixture(self, *, admitted=76, reported=77, resolved=75, recorded=70):
        ob = {"id": "A", "kind": "assurance", "state": "satisfied",
              "findings": [{"finding": "F", "triage": "required_correction", "correction": "K",
                            "recorded_seq": recorded, "resolved_seq": resolved}]}
        binding = {"policy_revision": "r", "environment": "fixture", "dependencies": [], "sources": [],
                   "definitions": {"A": definition_id(ob)}}
        evidence = {"attempt": "review", "candidate": "candidate", "definition": definition_id(ob),
                    "governance": "g", "binding": binding}
        ob.update(evidence=[evidence], receipts=[{"evidence": evidence, "reported_seq": reported}])
        ctx = {"candidate": "candidate", "policy_revision": "r", "verification": {"environment": "fixture", "dependencies": []},
               "admissions": {"review": {"role": "review", "serves": ["A"], "candidate": "candidate", "binding": binding,
                                          "admitted_seq": admitted, "report": {"outcome": "succeeded", "status": "validated_observation"}}}}
        return ob, ctx

    def test_both_sequences_must_strictly_follow_resolution(self):
        for admitted, reported in ((73, 77), (75, 77), (76, 75), (76, 74), (76, 77)):
            with self.subTest(admitted=admitted, reported=reported):
                ob, ctx = self.fixture(admitted=admitted, reported=reported)
                valid = admitted > 75 and reported > 75
                self.assertEqual(evidence_valid(ob, ctx, "g"), valid)
                self.assertEqual(bool(review_ordering(ob, ctx, attempt="review")), not valid)
                if not valid:
                    self.assertEqual(invalidation(ob, ctx, "g")["classification"], "ordering")

    def test_unknown_or_invalid_sequence_evidence_never_qualifies(self):
        for field in ("admitted", "reported", "resolved", "recorded"):
            for value in (None, True, "76", 1.5):
                with self.subTest(field=field, value=value):
                    ob, ctx = self.fixture(**{field: value})
                    self.assertFalse(evidence_valid(ob, ctx, "g"))
                    self.assertEqual(invalidation(ob, ctx, "g")["classification"], "ordering")

    def test_binding_changes_keep_precedence_over_ordering(self):
        ob, ctx = self.fixture(admitted=73)
        ctx["verification"]["environment"] = "another runner"
        self.assertFalse(evidence_valid(ob, ctx, "g"))
        self.assertEqual(invalidation(ob, ctx, "g")["classification"], "binding change")

    def test_no_required_finding_keeps_older_sequence_free_proof_valid(self):
        ob, ctx = self.fixture(admitted=None, reported=None)
        ob["findings"] = [{"finding": "note", "triage": "advisory"}]
        self.assertTrue(evidence_valid(ob, ctx, "g"))
        self.assertEqual(review_ordering(ob, ctx, attempt="review"), [])

    def test_accepted_sequence_fallback_is_only_for_older_receipts(self):
        ob, ctx = self.fixture()
        receipt = ob["receipts"][0]
        receipt["accepted_seq"] = receipt.pop("reported_seq")
        self.assertTrue(evidence_valid(ob, ctx, "g"))
        receipt["reported_seq"] = None
        self.assertFalse(evidence_valid(ob, ctx, "g"))
        ctx["admissions"]["review"].pop("admitted_seq")
        receipt.pop("reported_seq")
        self.assertFalse(evidence_valid(ob, ctx, "g"))

    def test_ordering_diagnostics_are_bounded_without_losing_the_count(self):
        ob, ctx = self.fixture(admitted=73)
        ob["findings"] = [{**ob["findings"][0], "finding": f"F{index}"} for index in range(12)]
        diagnostic = invalidation(ob, ctx, "g")["ordering"]
        self.assertEqual((len(diagnostic["findings"]), diagnostic["remaining"]), (8, 4))

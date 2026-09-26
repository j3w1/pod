"""Assurance history at the checkpoint, admission, report and acceptance boundaries."""

from copy import deepcopy

from pod.ledger import read
from pod.obligations import label_qualification
from tests import kernel_support as boundaries


class AssuranceHistoryTests(boundaries.KernelCase):
    review = boundaries.assurance_review
    satisfied = boundaries.assurance_satisfied
    label = boundaries.assurance_label
    waiting = boundaries.assurance_waiting

    def assurance(self):
        return next(row for row in self.stored() if row["id"] == "A")

    def late_blocker(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        old_packet = self.packet(["A"], role="review")
        old = self.start("earlier-review", old_packet)["admission"]
        self.settle(old)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        accepted = self.review("later-admitted-review")
        self.assertEqual(self.label(), "QUALIFIED")

        rows = self.waiting()
        next(row for row in rows if row["id"] == "A")["wait"] = {
            "class": "dependency", "referent": "K0"}
        rows.append({"id": "K0", "kind": "correction", "provenance": "coordinator", "parent": "A",
                     "check": "repair the reported defect", "boundary": {"paths": ["src"]},
                     "state": "unassigned"})
        self.report(old, old_packet, map={"obligations": rows}, triage=[
            {"finding": "blocker", "severity": "blocker", "triage": "required_correction",
             "summary": "real defect", "correction": "K0"}])
        self.assertEqual(self.label(), "WITHHELD")
        return old, accepted

    def satisfy_correction(self):
        rows = self.stored()
        correction = next(row for row in rows if row["id"] == "K0")
        correction.pop("wait", None)
        correction.update(state="satisfied", evidence=[self.proof("K0")])
        next(row for row in rows if row["id"] == "A")["wait"] = {
            "class": "sequenced", "referent": "O1"}
        self.write(rows)

    def offer_old_proof(self, admission):
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("wait", None)
        assurance.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, rows)

    def test_late_required_correction_survives_open_and_satisfied_states(self):
        old, accepted = self.late_blocker()
        finding = self.assurance()["findings"][0]
        self.assertEqual(finding["attempt"], old["admission_id"])
        self.assertEqual(finding["recorded_seq"],
                         next(row for row in self.stored() if row["id"] == "K0")["introduced_seq"])
        self.assertEqual(next(row for row in self.stored() if row["id"] == "K0")["finding"]["attempt"],
                         old["admission_id"])
        self.offer_old_proof(accepted)
        self.satisfy_correction()
        self.offer_old_proof(accepted)
        self.assertEqual(self.label(), "WITHHELD")
        fresh = self.review("fresh-after-correction")
        self.assertGreater(self.assurance()["receipts"][-1]["reported_seq"], finding["recorded_seq"])
        self.assertEqual(self.assurance()["evidence"], [
            next(item["evidence"] for item in self.assurance()["receipts"]
                 if item["evidence"]["attempt"] == fresh["admission_id"])])
        self.assertEqual(self.label(), "QUALIFIED")

    def test_advisory_overflow_refuses_without_evicting_required_finding(self):
        _, accepted = self.late_blocker()
        before = read(self.project, "objective")["checkpoint"]["seq"]
        rows = self.stored()
        next(row for row in rows if row["id"] == "K0").update(
            state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        packet = self.packet(["A"], role="review")
        noisy = self.start("noisy-review", packet)["admission"]
        self.settle(noisy)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "dependency", "referent": "K0"})
        advisories = [{"finding": f"n{i}", "severity": "minor", "triage": "advisory",
                       "summary": f"style note {i}"} for i in range(32)]
        self.refused("obligation_invalid", "finding_limit", self.report, noisy, packet,
                     map={"obligations": rows}, triage=advisories)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["seq"], before + 2)
        self.assertEqual(len(self.assurance()["findings"]), 1)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        next(row for row in rows if row["id"] == "K0").update(
            state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        self.satisfy_correction()
        self.offer_old_proof(accepted)
        self.assertEqual(self.label(), "WITHHELD")

    def test_reverse_report_order_ignores_skewed_or_missing_admission_stamps(self):
        old, accepted = self.late_blocker()
        state = read(self.project, "objective")
        checkpoint = state["checkpoint"]
        ctx = {"admissions": deepcopy(state["admissions"]), "outstanding": [],
               "candidate": self.candidate, "policy_revision": self.core()["policy_revision"],
               "verification": self.core()["verification"], "source_current": lambda entry: True}
        ctx["admissions"][old["admission_id"]].pop("created_at", None)
        ctx["admissions"][accepted["admission_id"]]["created_at"] = "9999-01-01T00:00:00+00:00"
        self.assertEqual(label_qualification(checkpoint, ctx, self.candidate)["label"], "WITHHELD")
        self.satisfy_correction()
        self.offer_old_proof(accepted)
        fresh = self.review("fresh-with-clock-skew")
        state = read(self.project, "objective")
        ctx["admissions"] = deepcopy(state["admissions"])
        ctx["admissions"][fresh["admission_id"]].pop("created_at", None)
        ctx["admissions"][old["admission_id"]]["created_at"] = "9999-01-01T00:00:00+00:00"
        self.assertEqual(label_qualification(state["checkpoint"], ctx, self.candidate)["label"],
                         "QUALIFIED")

    def test_unaccepted_report_before_blocker_cannot_be_offered_as_fresh_proof(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        early_packet = self.packet(["A"], role="review")
        early = self.start("unaccepted-early", early_packet)["admission"]
        self.settle(early)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(early, early_packet, map={"obligations": rows})
        receipt = next(item for item in self.assurance()["receipts"]
                       if item["evidence"]["attempt"] == early["admission_id"])
        self.assertIn("reported_seq", receipt)
        self.assertNotIn("accepted_seq", receipt)

        blocker_packet = self.packet(["A"], role="review")
        blocker = self.start("later-blocker", blocker_packet)["admission"]
        self.settle(blocker)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "dependency", "referent": "K0"})
        rows.append({"id": "K0", "kind": "correction", "provenance": "coordinator", "parent": "A",
                     "check": "repair the reported defect", "boundary": {"paths": ["src"]},
                     "state": "unassigned"})
        self.report(blocker, blocker_packet, map={"obligations": rows}, triage=[
            {"finding": "later", "severity": "blocker", "triage": "required_correction",
             "summary": "real defect", "correction": "K0"}])
        self.satisfy_correction()
        self.offer_old_proof(early)
        self.assertEqual(self.label(), "WITHHELD")
        self.review("fresh-after-unaccepted-report")
        self.assertEqual(self.label(), "QUALIFIED")

    def test_review_reported_before_correction_completion_stays_too_early(self):
        self.late_blocker()
        rows = self.stored()
        next(row for row in rows if row["id"] == "K0").update(
            state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        packet = self.packet(["A"], role="review")
        premature = self.start("review-before-fix", packet)["admission"]
        self.settle(premature)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(premature, packet, map={"obligations": rows})
        self.satisfy_correction()
        finding = self.assurance()["findings"][0]
        receipt = next(item for item in self.assurance()["receipts"]
                       if item["evidence"]["attempt"] == premature["admission_id"])
        self.assertLess(receipt["reported_seq"], finding["resolved_seq"])
        self.offer_old_proof(premature)
        self.review("review-after-fix")
        self.assertEqual(self.label(), "QUALIFIED")

    def test_pre_correction_review_reported_late_stays_too_early(self):
        self.late_blocker()
        rows = self.stored()
        next(row for row in rows if row["id"] == "K0").update(
            state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        packet = self.packet(["A"], role="review")
        premature = self.start("admitted-before-fix", packet)["admission"]
        self.settle(premature)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        self.satisfy_correction()
        self.report(premature, packet, map={"obligations": self.stored()})
        finding = self.assurance()["findings"][0]
        receipt = next(item for item in self.assurance()["receipts"]
                       if item["evidence"]["attempt"] == premature["admission_id"])
        self.assertLess(premature["admitted_seq"], finding["resolved_seq"])
        self.assertGreater(receipt["reported_seq"], finding["resolved_seq"])
        self.offer_old_proof(premature)
        self.review("admitted-after-fix")
        self.assertEqual(self.label(), "QUALIFIED")

    def test_restored_proof_without_new_blocker_stays_bound(self):
        accepted = self.satisfied()
        self.write(self.waiting(), verification={**self.core()["verification"],
                                                 "environment": "another runner"})
        self.assertEqual(self.label(), "WITHHELD")
        self.refused("obligation_unaccounted", "assurance_still_bound", self.write, self.stored())
        self.assertEqual(self.label(), "WITHHELD")
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("wait")
        assurance.update(state="satisfied", evidence=[{"attempt": accepted["admission_id"]}])
        self.write(rows)
        self.assertEqual(self.label(), "QUALIFIED")
        self.refused("unbound_assignment", "satisfied", self.start,
                     "redundant-review", self.packet(["A"], role="review"))

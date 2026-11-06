"""R246: a correction retires with its withdrawn assurance, through Pod and never the owner."""
from pod.errors import PodError
from pod.ledger import objective_root, read
from tests.issue41_support import ProductionCase


def waiting(referent="O1"):
    return {"state": "waiting", "wait": {"class": "sequenced", "referent": referent}}


def files(case):
    root = objective_root(case.project, "objective")
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and path.name != ".lock"}


def review_with_correction(case, a="A", k="K"):
    """A coordinator review assurance whose consumed review raised one required correction."""
    case.intake({"id": a, "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                 "check": "optional evidence tool review", "scope": {"paths": ["src"]},
                 "question": "is the evidence tool correct?", "candidate": case.candidate,
                 "existing_evidence": "unit tests", "insufficiency": "no independent review", **waiting()})
    packet = case.packet([a], role="review")
    admission = case.start("review", packet)["admission"]; case.settle(admission)
    case.report(admission, packet, map={"seq": read(case.project, "objective")["checkpoint"]["seq"] + 1, "update": {
        a: waiting(), k: {"kind": "correction", "provenance": "coordinator", "parent": a,
                          "check": "repair the reported defect", **waiting()}}},
        triage=[{"finding": "F1", "severity": "major", "triage": "required_correction", "blocks": "outcome",
                 "summary": "tool defect", "correction": k}])


def status_lines(case):
    from pod.obligations import status_projection
    return status_projection(read(case.project, "objective")["checkpoint"], {})["lines"]


class WithdrawnAssuranceCorrectionTests(ProductionCase):
    """A coordinator review assurance whose review raised one required correction K."""

    def setUp(self):
        super().setUp()
        review_with_correction(self)

    def next_seq(self):
        return read(self.project, "objective")["checkpoint"]["seq"] + 1

    def rows(self):
        return {row["id"]: row for row in self.stored()}

    def refusal(self, **fields):
        before = files(self)
        with self.assertRaises(PodError) as caught:
            self.write(seq=self.next_seq(), **fields)
        self.assertEqual(files(self), before)
        return caught.exception

    def close(self):
        self.write(seq=self.next_seq(), update={"O1": {"state": "satisfied", "evidence": [self.proof("O1")]}})
        return self.write(seq=self.next_seq(), close=True)

    def assert_follows(self, by, reason):
        rows = self.rows()
        self.assertEqual(rows["K"]["state"], "withdrawn")
        self.assertEqual(rows["K"]["withdrawal"], {"by": "coordinator", "reason": "the tool was cancelled",
                                                   "follows": {"obligation": "A", "by": by, "reason": reason}})
        # R3: the finding stays in the withdrawn assurance's history, never marked resolved.
        finding = rows["A"]["findings"][0]
        self.assertEqual((finding["finding"], finding["triage"], finding["correction"]),
                         ("F1", "required_correction", "K"))
        self.assertNotIn("resolved_seq", finding)

    def test_correction_retires_with_withdrawn_coordinator_assurance_and_objective_closes(self):
        """PoD#1: withdraw the assurance, then its open correction by coordinator; the objective closes."""
        self.write(seq=self.next_seq(), update={"A": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}}})
        self.write(seq=self.next_seq(), update={"K": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}})
        self.assert_follows("coordinator", "another approach was accepted")
        # R3: status shows the correction withdrawn with its parent's reason.
        row = next(line for line in status_lines(self) if line.startswith("  K "))
        self.assertIn("withdrawn        follows withdrawn parent: another approach was accepted "
                      "(A, by coordinator); by coordinator: the tool was cancelled", row)
        closed = self.close()["checkpoint"]
        self.assertEqual(closed["closure"]["report"]["status"], "closed")
        withdrawn = {row["obligation"]: row for row in closed["closure"]["report"]["withdrawn"]}
        self.assertEqual(withdrawn["K"]["follows"]["reason"], "another approach was accepted")

    def test_same_write_and_owner_withdrawn_parent_follow_the_same_rule(self):
        """R2: one write may retire both, and an assurance the owner withdrew is followed the same way."""
        self.write(seq=self.next_seq(), update={
            "A": {"state": "withdrawn", "withdrawal": {"by": "user_direct", "reason": "j3w1 dropped the tool"}},
            "K": {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}},
            revision_authority={"provenance": "user_direct", "instruction": "Drop the evidence tool review"})
        self.assert_follows("user_direct", "j3w1 dropped the tool")
        self.assertEqual(self.close()["checkpoint"]["closure"]["report"]["status"], "closed")

    def test_open_parent_still_refuses_and_names_the_rule(self):
        """PoD#2: while the assurance is not withdrawn the coordinator cannot drop its correction."""
        for parent in (waiting(), {"state": "blocked_external", "external": {
                "party": "third_party", "need": "reviewer", "unblocks_when": "a reviewer is available"}}):
            with self.subTest(parent=parent["state"]):
                caught = self.refusal(update={"A": parent, "K": {
                    "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "not needed"}}})
                self.assertEqual((caught.code, caught.detail["detail"]), ("obligation_invalid", "withdrawal_unauthorized"))
                self.assertIn("parent assurance A is not withdrawn", str(caught))
                self.assertIn("a review's required finding cannot be dropped on its own", str(caught))
                self.assertNotIn("retires with it", caught.detail["next_action"])

    def test_orphaned_correction_refusals_name_the_mechanical_step(self):
        """R2: closure and a wrong withdrawal form name the coordinator step, never an owner decision."""
        self.write(seq=self.next_seq(), update={"A": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}},
            "K": {"state": "blocked_external", "external": {
                "party": "owner", "need": "nothing", "unblocks_when": "never"}}})
        self.write(seq=self.next_seq(), update={"O1": {"state": "satisfied", "evidence": [self.proof("O1")]}})
        caught = self.refusal(close=True)
        self.assertEqual(caught.detail["detail"], "closure_unfinished")
        self.assertEqual(caught.detail["referent"], {"obligation": "K"})
        self.assertIn('withdraw K with {"by": "coordinator"', caught.detail["next_action"])
        # Every refusal exit for the orphan, whatever check refuses, names the same coordinator step.
        for withdrawal in ({"by": "user_direct", "reason": "the tool was cancelled"},
                           {"by": "issue_revision", "reason": "review cancelled", "decision": 1},
                           {"by": "coordinator", "reason": "review cancelled", "channel": "session"},
                           {"by": "coordinator", "reason": "review cancelled", "unexpected": True},
                           {"by": "coordinator", "reason": ""}):
            with self.subTest(withdrawal=sorted(withdrawal)):
                caught = self.refusal(update={"K": {"state": "withdrawn", "withdrawal": withdrawal}})
                self.assertEqual(caught.code, "obligation_invalid")
                self.assertTrue(caught.detail["next_action"].startswith('withdraw it with {"by": "coordinator"'))
                self.assertIn("parent assurance A is withdrawn", caught.detail["next_action"])
                self.assertTrue(str(caught).endswith(". Next: " + caught.detail["next_action"]))

    def test_follows_is_kernel_owned_and_persisted_record_revalidates(self):
        self.write(seq=self.next_seq(), update={"A": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}}})
        caught = self.refusal(update={"K": {"state": "withdrawn", "withdrawal": {
            "by": "coordinator", "reason": "the tool was cancelled",
            "follows": {"obligation": "A", "by": "coordinator", "reason": "another approach was accepted"}}}})
        self.assertEqual(caught.detail["detail"], "withdrawal_unauthorized")
        self.write(seq=self.next_seq(), update={"K": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}})
        # A later write revalidates the stored record; a changed restatement stays frozen.
        self.write(seq=self.next_seq())
        rows = self.stored()
        for row in rows:
            if row["id"] == "K":
                row["withdrawal"] = {**row["withdrawal"], "follows": {**row["withdrawal"]["follows"], "reason": "other"}}
        caught = self.refusal(obligations=rows)
        self.assertEqual(caught.detail["detail"], "terminal_frozen")

    def test_satisfied_correction_keeps_its_receipts(self):
        """R3: a correction already satisfied stays satisfied with its receipts when the assurance goes."""
        self.write(seq=self.next_seq(), update={"K": {"state": "satisfied", "evidence": [self.proof("K")]}})
        before = self.rows()["K"]
        self.write(seq=self.next_seq(), update={"A": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}}})
        after = self.rows()["K"]
        self.assertEqual((after["state"], after["receipts"]), ("satisfied", before["receipts"]))
        self.assertEqual(self.close()["checkpoint"]["closure"]["report"]["status"], "closed")

    def test_correction_follows_the_withdrawn_tip_of_a_renewed_assurance(self):
        """The effective parent is the renewal tip; a live tip still holds the correction."""
        self.write(renew=[{"id": "A", "as": "A2", **waiting()}])
        caught = self.refusal(update={"K": {
            "state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}})
        self.assertIn("parent assurance A2 is not withdrawn", str(caught))
        self.write(seq=self.next_seq(), update={
            "A2": {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}},
            "K": {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}})
        self.assertEqual(self.rows()["K"]["withdrawal"]["follows"],
                         {"obligation": "A2", "by": "coordinator", "reason": "another approach was accepted"})
        self.assertEqual(self.close()["checkpoint"]["closure"]["report"]["status"], "closed")

    def test_non_orphan_refusals_keep_their_own_next_action(self):
        """R246 changes guidance only for an orphaned correction: other rows keep their existing retry action."""
        self.write(seq=self.next_seq(), obligations=[*self.stored(), {
            "id": "S", "kind": "subgoal", "provenance": "coordinator", "parent": "O1", "check": "S is delivered",
            **waiting()}])
        follows = {"obligation": "O1", "by": "user_direct", "reason": "unused"}
        for key in ("S", "K"):
            with self.subTest(row=key):
                caught = self.refusal(update={key: {"state": "withdrawn", "withdrawal": {
                    "by": "coordinator", "reason": "cancelled", "follows": follows}}})
                self.assertEqual((caught.code, caught.detail["detail"]), ("obligation_invalid", "withdrawal_unauthorized"))
                self.assertEqual(caught.detail["next_action"], 'withdraw it with {"by": "coordinator", "reason": "<why>"}')
                self.assertTrue(str(caught).endswith('. Next: withdraw it with {"by": "coordinator", "reason": "<why>"}'))


class LongIdentifierStatusTests(ProductionCase):
    def test_parent_reason_survives_the_status_bound_with_maximum_length_ids(self):
        """R3: 64-character ids and retained-history counts never push the parent's reason out of the row."""
        a, k = "A" + "a" * 63, "K" + "k" * 63
        review_with_correction(self, a, k)
        seq = read(self.project, "objective")["checkpoint"]["seq"]
        self.write(seq=seq + 1, update={
            a: {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "another approach was accepted"}},
            k: {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "the tool was cancelled"}}})
        row = next(line for line in status_lines(self) if line.startswith("  " + k))
        self.assertLessEqual(len(row), 160)
        self.assertIn("follows withdrawn parent: another approach was accepted", row)


if __name__ == "__main__":
    import unittest
    unittest.main()

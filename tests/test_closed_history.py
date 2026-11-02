"""R145 and R117 (A844): the closed-snapshot readers judge each row by its selected evidence. A closed record that
retains a REUSE receipt recorded under an older definition, whose original scope cannot be verified, still counts as
closed for succession and still permits its evidence-closure source effect when no row selects that receipt. A
selected such receipt still refuses, and the handoff hold still sees the unselected history."""
from pod.errors import PodError
from pod.evidence_closure import _read_record, _source_closure
from pod.handoff import run_scope
from pod.ledger import closed_snapshot_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from pod.succession import _closure_recorded
from tests.test_proof_history import History


class Closed(History):
    def closed(self):
        state = read(self.project, "objective")
        self.assertIsNotNone(state["checkpoint"]["closure"])
        return state, closed_snapshot_context(self.project, "objective", state)

    def source_closure(self):
        root = objective_root(self.project, "objective")
        raw, before = _read_record(root)
        return _source_closure(self.project, "objective", root, raw, before)


class ClosedHistoryTests(Closed):
    """The record closes after a fresh review satisfies A under its current definition and is carried by REUSE,
    while A keeps its old-definition REUSE receipt as unselected history. The class builds its record once."""

    def build(self):
        History.build(self)
        self.redefine()
        self.fresh = self.review("second-review")["admission_id"]
        rows = self.stored()
        rows[0].pop("executor"); rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)
        self.carry("notes/more.txt")
        self.write(self.stored(), close=True)

    def test_unselected_history_counts_as_closed_and_closes_by_evidence(self):
        state, context = self.closed()
        row, receipts = self.receipts()
        self.assertEqual([item["attempt"] for item in row["evidence"]], [self.fresh])
        self.assertNotEqual(receipts[self.old]["reuse"]["definition"], row["definition"])
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], context, selected=True), ())
        self.assertTrue(_closure_recorded(self.project, "objective", state))
        self.assertTrue(self.source_closure())

    def test_unselected_history_still_holds_handoff(self):
        state, context = self.closed()
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], context), ("A",))
        run = state["checkpoint"]["native_refs"][0]["runId"]
        self.assertEqual(run_scope(self.project, "another", run)["unverifiable"], ["objective"])


class SelectedClosedHistoryTests(Closed):
    """A selects its old-definition REUSE receipt after the definition change and is then withdrawn under user
    authority, keeping that receipt selected; the record closes by an ordinary checkpoint. The class builds its
    record once."""

    def build(self):
        History.build(self)
        self.redefine(evidence=[{"attempt": self.old}])
        rows = self.stored()
        rows[0].pop("executor"); rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait")
        rows[1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "User waived the assurance"})
        self.write(rows, revision_authority={"provenance": "user_direct",
                                             "instruction": "Withdraw A and retain its receipt history"})
        self.write(self.stored(), close=True)

    def test_a_selected_unverifiable_receipt_still_refuses(self):
        state, context = self.closed()
        row, receipts = self.receipts()
        self.assertEqual(row["state"], "withdrawn")
        self.assertEqual([item["attempt"] for item in row["evidence"]], [self.old])
        self.assertNotEqual(receipts[self.old]["reuse"]["definition"], row["definition"])
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], context, selected=True), ("A",))
        self.assertFalse(_closure_recorded(self.project, "objective", state))
        with self.assertRaises(PodError) as caught:
            self.source_closure()
        self.assertEqual(caught.exception.code, "closure_unverified")
        self.assertEqual(caught.exception.detail, {"detail": "receipt_unverifiable", "referent": {"obligations": "A"}})

"""R145 and R117 (A844): the closed-snapshot readers judge each row by its selected evidence. A closed record that
retains a REUSE receipt recorded under an older definition, whose original scope cannot be verified, still counts as
closed for succession and still permits its evidence-closure source effect when no row selects that receipt. A
selected such receipt still refuses, and the handoff hold still sees the unselected history."""
from unittest.mock import patch

from pod import assurance
from pod.errors import PodError
from pod.evidence_closure import _read_record, _source_closure
from pod.handoff import run_scope
from pod.ledger import closed_snapshot_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from pod.succession import _closure_recorded
from tests.test_proof_history import History


class ClosedHistoryTests(History):
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

    def closed(self):
        state = read(self.project, "objective")
        self.assertIsNotNone(state["checkpoint"]["closure"])
        return state, closed_snapshot_context(self.project, "objective", state)

    def source_closure(self):
        root = objective_root(self.project, "objective")
        raw, before = _read_record(root)
        return _source_closure(self.project, "objective", root, raw, before)

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

    def test_a_selected_unverifiable_receipt_still_refuses(self):
        """An ordinary write cannot satisfy a row with such a receipt, so a double reports A's selected receipt
        unverifiable as well, as a record from before that rule could hold it."""
        real = assurance.validate_receipts

        def validate(ob, ctx, *, seq):
            unknown = real(ob, ctx, seq=seq)
            return unknown + (self.fresh,) if ob["id"] == "A" else unknown
        state, context = self.closed()
        with patch("pod.assurance.validate_receipts", validate):
            self.assertEqual(validate_closed_snapshot(state["checkpoint"], context, selected=True), ("A",))
            self.assertFalse(_closure_recorded(self.project, "objective", state))
            with self.assertRaises(PodError) as caught:
                self.source_closure()
        self.assertEqual(caught.exception.code, "closure_unverified")
        self.assertEqual(caught.exception.detail["detail"], "receipt_unverifiable")
        self.assertEqual(caught.exception.detail["referent"], {"obligations": "A"})

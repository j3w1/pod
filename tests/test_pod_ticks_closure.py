"""Due ticks before closure, from post-write proof on both actual closing routes."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import read
from pod.operations import OrcaPort
from tests.issue_port_support import SimulatedInterruption
from tests.pod_tick_support import TickCase


class ClosureTests(TickCase):
    def proof_rows(self):
        rows = self.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        return rows

    def worker(self):
        self.bind("- [ ] **PoD#1", self.sub("S"))
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("worker", frozen)["admission"]
        self.settle(admission)
        return frozen, admission

    def report_close(self, frozen, admission, *, close=True, rows=None):
        return self.report(admission, frozen, map={"obligations": rows or self.proof_rows(),
                                                  **({"close": True} if close else {})})

    def assert_due_refusal(self, callback, *, direction="tick"):
        before, writes = self.saved(), list(self.issue.writes)
        exc = self.failure("pod_ticks_due", callback)
        self.assertEqual(self.saved(), before, "refused closing write changed context/map/report")
        self.assertEqual(self.issue.writes, writes)
        rows = exc.detail["pod_ticks"]["lines"]
        self.assertEqual(rows[0]["direction"], direction)
        self.assertIn("pod internal pod-tick", rows[0]["command"])
        self.assertIn("without closing", exc.detail["steps"])
        return exc

    def assert_closed_block(self, result, reason, direction="tick"):
        ack = self.ack(result) if "checkpoint" in result else result
        row = ack["pod_ticks"]["lines"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked"]["reason"], reason)
        self.assertEqual(row["blocked_at_closure"]["direction"], direction)
        self.assertEqual(row["ticked"], direction == "untick")
        self.assertFalse(row["due"])
        self.assertEqual(self.line()["blocked_at_closure"], row["blocked_at_closure"])
        self.failure("objective_closed", self.tick)

    def test_checkpoint_satisfying_last_criterion_and_closing_is_atomic_refusal(self):
        self.bind()
        self.assertEqual(self.stored()[0]["source"]["ref"], "PoD#1")
        self.assert_due_refusal(lambda: self.prove(close=True))

    def test_report_satisfying_last_criterion_and_closing_is_atomic_refusal(self):
        frozen, admission = self.worker()
        self.assert_due_refusal(lambda: self.report_close(frozen, admission))
        self.assertIsNone(read(self.project, "objective")["admissions"][admission["admission_id"]]["report"])

    def test_report_withdrawal_in_its_closing_map_makes_untick_due(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False); self.tick()
        rows = self.stored(); rows[0].pop("evidence", None); rows[0].pop("receipts", None)
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scope removed"})
        self.assert_due_refusal(lambda: self.report(admission, frozen, map={"obligations": rows, "close": True,
            "revision_authority": {"provenance": "user_direct", "instruction": "drop PoD#1"}}), direction="untick")

    def test_checkpoint_proof_tick_close_steps_make_exactly_one_issue_write(self):
        self.bind(); self.assert_due_refusal(lambda: self.prove(close=True))
        result = self.prove(); ack = self.ack(result)
        self.assertEqual(ack["pod_ticks"]["lines"][0]["direction"], "tick")
        self.tick(); closed = self.write(close=True)
        self.assertIsNotNone(closed["checkpoint"]["closure"])
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

    def test_report_proof_tick_close_steps_make_exactly_one_issue_write(self):
        frozen, admission = self.worker()
        self.assert_due_refusal(lambda: self.report_close(frozen, admission))
        result = self.report_close(frozen, admission, close=False)
        self.assertEqual(result["pod_ticks"]["lines"][0]["direction"], "tick")
        self.tick(); closed = self.report_close(frozen, admission)
        self.assertTrue(read(self.project, "objective")["checkpoint"]["closure"])
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

    def test_closing_withdrawal_makes_untick_due_and_refuses_atomically(self):
        self.bind(); self.prove(); self.tick()
        self.assert_due_refusal(lambda: self.lapse(withdraw=True, close=True), direction="untick")
        self.lapse(withdraw=True); self.tick(); self.write(close=True)
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1", "- [ ] **PoD#1"])

    def test_source_body_block_after_proof_permits_checkpoint_closure_B2(self):
        self.bind(); self.prove(); self.issue.body += " outside"
        self.failure("issue_reconciliation_required", self.tick)
        closed = self.write(close=True)
        self.assert_closed_block(closed, "issue_body_changed")
        self.assertEqual(self.issue.writes, [])

    def test_closed_issue_block_after_proof_permits_checkpoint_closure_B3(self):
        self.bind(); self.prove(); self.issue.state = "CLOSED"
        self.failure("issue_reconciliation_required", self.tick)
        self.assert_closed_block(self.write(close=True), "closed_issue_requires_intent_reconciliation")

    def test_unreadable_block_after_proof_permits_checkpoint_closure_B5(self):
        self.bind(); self.prove(); self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.assert_closed_block(self.write(close=True), "issue_access_unavailable")

    def test_report_closure_preserves_fresh_body_block_B2(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.body += " outside"
        self.failure("issue_reconciliation_required", self.tick)
        self.assert_closed_block(self.report_close(frozen, admission), "issue_body_changed")

    def test_report_closure_preserves_fresh_closed_issue_block_B3(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.state = "CLOSED"
        self.failure("issue_reconciliation_required", self.tick)
        self.assert_closed_block(self.report_close(frozen, admission), "closed_issue_requires_intent_reconciliation")

    def test_report_closure_preserves_fresh_unreadable_issue_block_B5(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.assert_closed_block(self.report_close(frozen, admission), "issue_access_unavailable")

    def test_report_closure_preserves_fresh_duplicate_block_C3(self):
        frozen, admission = self.worker()
        self.report_close(frozen, admission, close=False)
        self.issue.body += "\n- [ ] **PoD#1 duplicate"; self.rebind()
        self.failure("line_duplicated", self.tick)
        result = self.report_close(frozen, admission)
        self.assertEqual(result["pod_ticks"]["lines"][0]["blocked_at_closure"]["block"]["row"], "C3")

    def test_block_before_later_report_cannot_exempt_closure(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.report_close(frozen, admission, close=False)
        self.assert_due_refusal(lambda: self.report_close(frozen, admission))

    def test_own_untick_on_duplicate_lines_stays_due_blocked_C3(self):
        self.bind(); self.prove(); self.tick(); self.lapse(withdraw=True)
        self.issue.body += "\n- [x] **PoD#1 duplicate"; self.rebind()
        self.failure("line_duplicated", self.tick)
        self.assertEqual(self.line()["direction"], "untick")
        result = self.write(close=True)
        self.assertEqual(result["pod_ticks"]["lines"][0]["blocked_at_closure"]["direction"], "untick")

    def test_a_pending_tick_with_withdrawal_in_closing_map_must_recover(self):
        self.bind(); self.prove(); self.issue.interrupt_next = True
        with self.assertRaises(SimulatedInterruption):
            self.tick()
        self.assert_due_refusal(lambda: self.lapse(withdraw=True, close=True))

    def test_duplicate_without_fresh_request_still_holds_closure_C3(self):
        self.bind("- [ ] **PoD#1\n- [ ] **PoD#1 duplicate"); self.prove()
        self.assert_due_refusal(lambda: self.write(close=True))

    def test_duplicate_block_after_proof_permits_checkpoint_closure_C3(self):
        self.bind("- [ ] **PoD#1\n- [ ] **PoD#1 duplicate"); self.prove()
        self.failure("line_duplicated", self.tick)
        closed = self.write(close=True)
        row = closed["pod_ticks"]["lines"][0]
        self.assertEqual(row["blocked_at_closure"]["block"]["code"], "line_duplicated")
        self.assertIsNone(row["ticked"])
        self.assertEqual(self.issue.writes, [])

    def test_block_before_later_checkpoint_cannot_exempt_closure(self):
        self.bind(); self.prove(); self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.write()
        self.assert_due_refusal(lambda: self.write(close=True))

    def test_fresh_refused_tick_permits_checkpoint_closure_E2(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403; self.tick()
        self.assert_closed_block(self.write(close=True), "github_write_refused")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertEqual(self.line()["blocked"]["status"], 403)
        self.assertEqual(self.line()["authorship"], "not_pod")

    def test_fresh_refused_tick_permits_report_closure_E2(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.refuse_next = 429; self.tick()
        self.assert_closed_block(self.report_close(frozen, admission), "github_write_refused")
        self.assertEqual(len(self.issue.writes), 1)

    def test_fresh_refused_untick_permits_checkpoint_closure_E2(self):
        self.bind(); self.prove(); self.tick(); self.lapse(withdraw=True)
        self.issue.writes.clear(); self.issue.refuse_next = 403; self.tick()
        self.assert_closed_block(self.write(close=True), "github_write_refused", "untick")
        self.assertEqual(self.issue.writes, ["- [ ] **PoD#1"])

    def test_fresh_refused_untick_permits_report_closure_E2(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.tick()
        rows = self.stored(); rows[0].pop("evidence", None); rows[0].pop("receipts", None)
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scope dropped"})
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "drop PoD#1"})
        self.issue.writes.clear(); self.issue.refuse_next = 403; self.tick()
        result = self.report(admission, frozen, map={"obligations": self.stored(), "close": True})
        self.assert_closed_block(result, "github_write_refused", "untick")
        self.assertEqual(self.issue.writes, ["- [ ] **PoD#1"])

    def test_refusal_before_later_checkpoint_cannot_exempt_closure_E2(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403; self.tick()
        self.write()
        self.assert_due_refusal(lambda: self.write(close=True))

    def test_refusal_before_later_report_cannot_exempt_closure_E2(self):
        frozen, admission = self.worker(); self.report_close(frozen, admission, close=False)
        self.issue.refuse_next = 403; self.tick()
        self.report_close(frozen, admission, close=False)
        self.assert_due_refusal(lambda: self.report_close(frozen, admission))

    def test_lost_response_with_earlier_body_still_holds_closure_E3(self):
        self.bind(); self.prove(); self.issue.lose_next = "not_landed"; self.tick()
        self.assert_due_refusal(lambda: self.write(close=True))
        self.assertEqual(len(self.issue.writes), 1)

    def test_interrupted_intent_with_earlier_body_still_holds_closure_E4(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def lost(**arguments):
            self.issue.lose_next = "not_landed"
            try:
                original(**arguments)
            except PodError:
                raise SimulatedInterruption()
        self.issue.issue_body_write = lost
        with self.assertRaises(SimulatedInterruption):
            self.tick()
        self.assert_due_refusal(lambda: self.write(close=True))
        self.recover_tick()  # recovery reads only; UNKNOWN is still not a refusal
        self.assert_due_refusal(lambda: self.write(close=True))
        self.assertEqual(len(self.issue.writes), 1)

    def test_refused_changed_body_is_reconciliation_block_at_closure_E2_B2(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403
        self.issue.other_actor = "- [x] **PoD#1"
        self.failure("issue_reconciliation_required", self.tick)
        closed = self.write(close=True)
        self.assertEqual(closed["pod_ticks"]["lines"][0]["blocked_at_closure"]["block"]["row"], "B2")

    def test_lost_outside_body_is_reconciliation_block_at_closure_E3_B2(self):
        self.bind(); self.prove(); self.issue.lose_next = "not_landed"
        self.issue.other_actor = "outside body"
        self.failure("issue_reconciliation_required", self.tick)
        self.write(close=True)
        self.assertEqual(self.line()["blocked"]["row"], "B2")

    def test_success_mismatch_is_reconciliation_block_at_closure_E5_B2(self):
        self.bind(); self.prove(); self.issue.readback_override = "outside body"
        self.failure("issue_reconciliation_required", self.tick)
        self.write(close=True)
        self.assertEqual(self.line()["blocked"]["row"], "B2")

    def test_no_due_issue_objective_closes_in_one_write(self):
        self.bind("- [x] **PoD#1")
        result = self.prove(close=True)
        self.assertIsNotNone(result["checkpoint"]["closure"])
        self.assertEqual(self.issue.writes, [])

    def test_reopened_then_ticked_objective_recloses_without_old_blocked_limitations(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403; self.tick()
        self.write(close=True)
        self.assertIn("blocked_at_closure", self.line())
        self.write(reopen=True, revision_authority={"provenance": "user_direct",
            "instruction": "reopen to complete the previously refused tick"})
        self.tick()
        result = self.write(close=True)
        row = result["pod_ticks"]["lines"][0]
        self.assertEqual(row["status"], "current")
        self.assertNotIn("blocked_at_closure", row)
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["closure_blocked"], [])

    def test_direct_objective_still_closes_in_one_write(self):
        self.intake(); self.prove(close=True)
        self.assertEqual(self.issue.writes, [])

    def test_both_tab_and_tick_gates_are_named_and_tab_code_keeps_precedence(self):
        frozen, admission = self.worker()
        original = self.port.show_worker
        def show(dispatch):
            value = original(dispatch)
            worker = value["result"]["worker"]
            value["result"].update(terminal={"handle": worker["agentTerminalHandle"], "connected": True},
                terminalResource={"terminalHandle": worker["agentTerminalHandle"], "worktreeId": worker["worktreeId"],
                    "ownerDispatchId": dispatch, "originDispatchId": dispatch, "ownershipState": "owned",
                    "releaseState": "not_requested", "retainedReason": None})
            value["result"]["projection"].update(role="worker", resource={"state": "owned", "releaseState": "not_requested"},
                nextAction={"kind": "release", "argv": ["orchestration", "worker-release", "--dispatch", dispatch]})
            return value
        with patch.object(OrcaPort, "show_worker", side_effect=show):
            before = self.saved()
            exc = self.failure("worker_tabs_open", lambda: self.report_close(frozen, admission))
            self.assertEqual(self.saved(), before)
            self.assertEqual(exc.detail["codes"], ["worker_tabs_open", "pod_ticks_due"])
            self.assertTrue(exc.detail["worker_tabs"])
            self.assertTrue(exc.detail["pod_ticks"]["lines"][0]["due"])

    def test_forward_compatible_lost_mapping_own_tick_unticks_D7(self):
        """Synthetic mapping control; R149 revision/observation tests cover actual D7."""
        self.bind(); self.prove(); self.tick(); self.lapse(remove_mapping=True)
        self.assert_due_refusal(lambda: self.write(close=True), direction="untick")
        self.assertEqual(self.line()["criteria"], [])
        self.tick(); self.write(close=True)
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1", "- [ ] **PoD#1"])
        self.assertEqual(self.line()["blocked"]["code"], "unmapped")

    def test_ambiguous_tick_is_never_unticked_when_mapping_lost_D11(self):
        """Synthetic mapping control, separate from actual revision/observation cases."""
        self.bind(); self.prove(); self.issue.lose_next = "landed"; self.tick()
        self.lapse(remove_mapping=True); self.tick()
        self.assertEqual(self.line()["status"], "ambiguous")
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

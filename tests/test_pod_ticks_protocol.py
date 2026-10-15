"""R10 table: writes and recoveries through internal pod-tick with a recording port."""

from copy import deepcopy
import fcntl
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import objective_root, read, update_admission
from tests.issue_port_support import SimulatedInterruption
from tests.pod_tick_support import TickCase


class ProtocolTests(TickCase):
    def test_one_tick_changes_one_checkbox_byte_E1(self):
        original = "intro\r\n- [ ] **PoD#1 precise\r\n- [X] **PoD#2 unchanged\r\n"
        self.bind(original); self.prove()
        before = deepcopy(read(self.project, "objective")["checkpoint"])
        result = self.tick()
        after = read(self.project, "objective")["checkpoint"]
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["receipt"]["rebind"])
        self.assertEqual(result["readback"]["ticked"], True)
        self.assertEqual(sum(a != b for a, b in zip(original.encode(), self.issue.body.encode())), 1)
        self.assertEqual(len(self.issue.writes), 1)
        self.assertEqual({k: v for k, v in before.items() if k != "objective_source"},
                         {k: v for k, v in after.items() if k != "objective_source"})
        self.assertEqual(self.line()["authorship"], "own")

    def test_intent_precedes_send_and_lock_is_released(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            intent = read(self.project, "objective")["pod_ticks"]["requests"]["1"]
            self.assertEqual(intent["phase"], "intent")
            self.assertIsNone(intent["result"])
            with (objective_root(self.project, "objective") / ".lock").open("r") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return original(**arguments)
        self.issue.issue_body_write = send
        self.tick()

    def test_current_own_tick_is_never_rewritten_D5(self):
        self.bind(); self.prove(); self.tick()
        self.assertEqual(self.tick()["status"], "current")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertEqual(self.line()["status"], "current")

    def test_own_tick_unticks_after_evidence_reopen_D6(self):
        self.bind(); self.prove(); self.tick(); self.lapse()
        self.assertEqual(self.line()["direction"], "untick")
        result = self.tick()
        self.assertEqual(result["receipt"]["direction"], "untick")
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1", "- [ ] **PoD#1"])
        self.assertEqual(self.line()["status"], "not_proven")

    def test_own_tick_unticks_after_withdrawal_D6(self):
        self.bind(); self.prove(); self.tick(); self.lapse(withdraw=True)
        self.tick()
        self.assertFalse(self.line()["ticked"])
        self.assertEqual(self.line()["criteria"][0]["state"], "withdrawn")

    def test_candidate_movement_makes_own_untick_due_D6(self):
        self.bind(); self.prove(); self.tick()
        self.move()
        rows = self.stored(); rows[0].pop("evidence"); rows[0].pop("receipts")
        rows[0].update(state="active", executor="coordinator")
        self.write(rows)
        self.assertEqual(self.line()["direction"], "untick")
        self.tick()
        self.assertFalse(self.line()["ticked"])

    def test_changed_evidence_source_makes_untick_due_without_checkpoint_D6(self):
        self.bind()
        from pod.records import source_identity
        proof = self.proof("O1", sources=[source_identity(self.project, "README.md")])
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[proof])
        self.write(rows); self.tick()
        (self.project / "README.md").write_text("changed source\n")
        self.assertEqual(self.line()["direction"], "untick")
        self.tick()
        self.assertFalse(self.line()["ticked"])

    def test_hand_tick_proven_is_current_but_never_pod_authored_D8(self):
        self.bind("- [X] **PoD#1"); self.prove()
        self.assertEqual(self.tick()["status"], "current")
        row = self.line()
        self.assertEqual((row["status"], row["authorship"]), ("current", "not_pod"))
        self.assertEqual(self.issue.writes, [])

    def test_hand_tick_unproven_is_left_named_D9(self):
        self.bind("- [x] **PoD#1")
        self.assertEqual(self.tick()["status"], "ticked_without_pod_proof")
        self.assertEqual(self.line()["status"], "ticked_without_pod_proof")
        self.assertEqual(self.issue.writes, [])

    def test_hand_tick_unmapped_is_left_named_D10(self):
        self.bind("- [ ] **PoD#1\n- [x] **PoD#2")
        self.assertEqual(self.tick(2)["status"], "ticked_without_pod_proof")
        self.assertEqual(self.line(2)["status"], "ticked_without_pod_proof")
        self.assertEqual(self.line(2)["criteria"], [])
        self.assertEqual(self.issue.writes, [])

    def test_previously_mapped_hand_tick_is_left_when_mapping_lost_D10(self):
        """Mapping-only forward-compatible control; current criterion sources are immutable."""
        self.bind("- [x] **PoD#1"); self.prove(); self.tick()
        self.lapse(remove_mapping=True)
        self.assertEqual(self.tick()["status"], "ticked_without_pod_proof")
        self.assertEqual(self.line()["status"], "ticked_without_pod_proof")
        self.assertEqual(self.line()["criteria"], [])
        self.assertEqual(self.issue.writes, [])

    def test_held_refusal_reads_back_then_stays_due_E2(self):
        self.bind(); self.prove(); self.issue.calls.clear()
        self.issue.refuse_next = 403
        result = self.tick()
        self.assertEqual(result["status"], "refused")
        self.assertFalse(result["receipt"]["rebind"])
        self.assertEqual(result["receipt"]["result"]["status"], 403)
        self.assertEqual([call[0] for call in self.issue.calls], ["issue", "issue_body_write", "issue"])
        row = self.line()
        self.assertTrue(row["due"])
        self.assertEqual(row["blocked"]["row"], "E2")
        self.assertEqual(row["authorship"], "not_pod")

    def test_later_request_rechecks_and_replaces_refusal_E2_E1(self):
        self.bind(); self.prove(); self.issue.refuse_next = 429; self.tick()
        self.issue.calls.clear()
        self.tick()
        self.assertEqual([call[0] for call in self.issue.calls], ["issue", "issue_body_write", "issue"])
        self.assertIsNone(self.line()["blocked"])
        self.assertEqual(self.line()["authorship"], "own")

    def test_refused_write_with_intended_body_from_other_actor_is_outside_edit_E2(self):
        self.bind(); self.prove()
        self.issue.refuse_next = 403
        self.issue.other_actor = "- [x] **PoD#1"
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "issue_body_changed")
        self.assertFalse(exc.detail["receipt"]["rebind"])
        self.assertEqual(self.line()["authorship"], "not_pod")

    def test_lost_response_landed_tick_is_ambiguous_E3_D11(self):
        self.bind(); self.prove(); self.issue.lose_next = "landed"
        result = self.tick()
        self.assertEqual(result["status"], "unconfirmed")
        self.assertTrue(result["receipt"]["rebind"])
        self.assertEqual(self.line()["status"], "ambiguous")
        self.lapse()
        self.assertEqual(self.tick()["status"], "ambiguous")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertEqual(self.line()["authorship"], "ambiguous")

    def test_lost_unlanded_request_with_other_actor_intended_body_is_ambiguous_E3(self):
        self.bind(); self.prove()
        self.issue.lose_next = "not_landed"; self.issue.other_actor = "- [x] **PoD#1"
        self.tick()
        self.assertEqual(self.line()["authorship"], "ambiguous")
        self.lapse(withdraw=True); self.tick()
        self.assertEqual(len(self.issue.writes), 1)

    def test_lost_unlanded_request_stays_due_without_refusal_E3(self):
        self.bind(); self.prove(); self.issue.lose_next = "not_landed"
        self.tick()
        self.assertTrue(self.line()["due"])
        self.assertIsNone(self.line()["blocked"])
        self.assertEqual(self.line()["latest_request"]["result"]["result"], "unconfirmed")

    def test_lost_response_outside_body_requires_reconciliation_E3(self):
        self.bind(); self.prove(); self.issue.lose_next = "not_landed"
        self.issue.other_actor = "changed outside body"
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["receipt"]["result"]["result"], "unconfirmed")
        self.assertEqual(self.line()["blocked"]["row"], "B2")

    def test_interrupted_intent_recovers_read_only_and_never_claims_authorship_E4(self):
        self.bind(); self.prove(); self.issue.interrupt_next = True
        with self.assertRaises(SimulatedInterruption):
            self.tick()
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["requests"]["1"]["phase"], "intent")
        self.issue.calls.clear()
        result = self.recover_tick()
        self.assertEqual([call[0] for call in self.issue.calls], ["issue"])
        self.assertTrue(result["receipt"]["recovered"])
        self.assertEqual(self.line()["authorship"], "ambiguous")
        self.assertEqual(len(self.issue.writes), 1)

    def test_interrupted_held_success_recovers_own_tick_E4(self):
        self.bind(); self.prove()
        from pod import pod_ticks
        original = pod_ticks._readback
        with patch("pod.pod_ticks._readback", side_effect=SimulatedInterruption):
            with self.assertRaises(SimulatedInterruption):
                self.tick()
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["requests"]["1"]["phase"], "result")
        self.tick()
        self.assertEqual(self.line()["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)

    def test_ambiguous_untick_leaves_no_authorship(self):
        self.bind(); self.prove(); self.tick(); self.lapse()
        self.issue.lose_next = "landed"; self.tick()
        self.assertFalse(self.line()["ticked"])
        self.assertEqual(self.line()["authorship"], "not_pod")
        self.assertEqual(self.line()["status"], "not_proven")

    def test_success_readback_mismatch_stops_without_rebind_E5(self):
        self.bind(); self.prove(); self.issue.readback_override = "outside replacement"
        source = deepcopy(read(self.project, "objective")["checkpoint"]["objective_source"])
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "tick_readback_mismatch")
        self.assertFalse(exc.detail["receipt"]["rebind"])
        self.assertEqual(read(self.project, "objective")["checkpoint"]["objective_source"], source)
        self.assertEqual(self.line()["blocked"]["row"], "B2")
        self.assertEqual(len(self.issue.writes), 1)

    def test_rebind_never_invalidates_proof_or_causes_tick_untick_loop(self):
        self.bind(); self.prove()
        state = read(self.project, "objective")
        before, seq = deepcopy(state["checkpoint"]["obligations"]), state["checkpoint"]["seq"]
        for _ in range(3):
            self.tick()
        state = read(self.project, "objective")
        self.assertEqual(state["checkpoint"]["obligations"], before)
        self.assertEqual(state["checkpoint"]["seq"], seq)
        self.assertTrue(self.line()["proven"])
        self.assertFalse(self.line()["due"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_observed_untick_ends_pod_ownership_of_a_later_hand_tick(self):
        self.bind(); self.prove(); self.tick()
        self.issue.body = "- [ ] **PoD#1"; self.rebind()
        self.issue.body = "- [x] **PoD#1"; self.rebind()
        self.lapse(); self.tick()
        self.assertEqual(self.line()["authorship"], "not_pod")
        self.assertEqual(len(self.issue.writes), 1)

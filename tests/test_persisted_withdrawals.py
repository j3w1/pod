"""Malformed persisted withdrawals refuse through ordinary closure/status readers."""

from copy import deepcopy
import json

from pod.ledger import kernel_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from tests.pod_tick_support import TickCase
from tests.test_owner_handoff import HandoffCase


class PersistedWithdrawalTests(TickCase):
    def closed_withdrawal(self):
        self.bind("- [ ] **PoD#1** result")
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "Owner removed result"})
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Withdraw this result"}, close=True)
        state = read(self.project, "objective")
        ctx = kernel_context(self.project, "objective", state, None)
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], ctx), ())
        return state["checkpoint"], ctx

    def test_nonobject_withdrawal_siblings_refuse_without_effects(self):
        checkpoint, ctx = self.closed_withdrawal()
        before = self.saved()
        for withdrawal in (None, [], ["user_direct"], "user_direct", True, False, 0, 1, 1.5):
            with self.subTest(withdrawal=withdrawal):
                damaged = deepcopy(checkpoint)
                damaged["obligations"][0]["withdrawal"] = withdrawal
                error = self.failure("obligation_invalid", validate_closed_snapshot, damaged, ctx)
                self.assertEqual(error.detail["detail"], "malformed")
                self.assertEqual(self.saved(), before)
        self.assertEqual((self.port.starts, self.issue.writes, self.remote.calls), ([], [], []))

    def test_malformed_object_withdrawals_keep_named_refusals(self):
        checkpoint, ctx = self.closed_withdrawal()
        before = self.saved()
        original = checkpoint["obligations"][0]["withdrawal"]
        cases = ({}, {"by": "user_direct"}, {**original, "reason": None}, {**original, "extra": True},
                 {"by": "issue_revision", "reason": "unverified retirement"},
                 {"by": "issue_revision", "reason": "wrong decision", "decision": 999})
        for withdrawal in cases:
            with self.subTest(withdrawal=withdrawal):
                damaged = deepcopy(checkpoint)
                damaged["obligations"][0]["withdrawal"] = withdrawal
                error = self.failure("obligation_invalid", validate_closed_snapshot, damaged, ctx)
                self.assertIn(error.detail["detail"], ("malformed", "withdrawal_unauthorized"))
                self.assertEqual(self.saved(), before)
        self.assertEqual(validate_closed_snapshot(checkpoint, ctx), ())


class WithdrawalStatusTests(HandoffCase):
    def test_nonobject_peer_withdrawals_hold_public_status_and_handoff(self):
        self.establish()
        self.closed_review_peer(withdrawn=True)
        peer = objective_root(self.project, "closure-peer") / "context.json"
        from tests.common import logical_json
        genuine = logical_json(peer)
        self.lose()
        for withdrawal in ([], "coordinator", False, 1):
            with self.subTest(withdrawal=withdrawal):
                damaged = deepcopy(genuine)
                damaged["checkpoint"]["obligations"][1]["withdrawal"] = withdrawal
                peer.write_text(json.dumps(damaged))
                before = self.snapshot()
                native = deepcopy((self.current, self.port.starts, self.port.workers, self.mutations))
                self.assertEqual(self.status()["owner_handoff"]["condition"], "run_shared")
                self.assertIn("run_shared", self.status(text=True))
                self.refused(lambda: self.handoff())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual((self.current, self.port.starts, self.port.workers, self.mutations), native)

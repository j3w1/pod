"""Completion currency uses the authority-joined state without early persistence."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from pod.governance import closure_keeps_baseline
from pod.ledger import kernel_context, read
from tests import test_closure_fixes_b as currency
from tests.issue_port_support import patch_issue_port
from tests.pod_tick_support import TickCase
from tests.test_completed_closure import SourcePort


class ReboundCompletionCase(TickCase):
    linked = currency.LinkedCompletionTests.linked
    advance_policy = currency.LinkedCompletionTests.advance_policy
    assert_kept = currency.LinkedCompletionTests.assert_kept

    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        replacement = patch_issue_port(self.issue)
        replacement.start()
        self.addCleanup(replacement.stop)

    def change_runtime(self):
        self.port.runtime = "runtime-2"
        self.port.placement = {**self.port.placement, "runtime": self.port.runtime}

    def closing_rows(self):
        rows = self.stored()
        rows[-1].pop("executor")
        rows[-1].update(state="satisfied", evidence=[self.proof("S")])
        return rows

    def assert_rebound(self, result):
        self.assert_kept(result)
        state = read(self.project, "objective")
        checkpoint = state["checkpoint"]
        self.assertEqual(checkpoint["native_refs"], [{"runId": "run", "runtime": "runtime-2"}])
        history = checkpoint["continuity"]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["from_runtime"], history[0]["to_runtime"], history[0]["provenance"]),
                         ("runtime", "runtime-2", "automatic"))
        self.assertEqual(result["runtime_continuity"], history)
        self.assertTrue(all(row["runtime"] == "runtime-2" for row in state["admissions"].values()))

    def assert_lead_hold(self, error):
        action = error.detail["next_action"]
        self.assertIn("the project lead", action)
        self.assertIn("no recorded or linked merge", action)
        self.assertIn("blocked", action)
        self.assertIn("without governance_refresh", action)
        self.assertNotIn("then retries proved closure", action)
        self.assertNotIn("owner", (str(error) + action).lower())


class ClosingContinuityTests(ReboundCompletionCase):
    def test_shared_context_carries_the_current_utc_closure_timestamp(self):
        self.intake()
        before = datetime.now(timezone.utc)
        ctx = kernel_context(self.project, "objective", read(self.project, "objective"), None)
        after = datetime.now(timezone.utc)
        stamp = datetime.fromisoformat(ctx["closure_at"])
        self.assertEqual(stamp.tzinfo, timezone.utc)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)

    def test_checkpoint_closes_with_linked_delivery_after_runtime_and_governance_change(self):
        self.linked()
        self.advance_policy()
        self.change_runtime()
        self.assert_rebound(self.write(close=True))

    def test_report_closes_with_linked_delivery_after_runtime_and_governance_change(self):
        admission, frozen = self.linked(report=True)
        self.advance_policy()
        self.change_runtime()
        result = self.report(admission, frozen, map={"obligations": self.closing_rows(), "close": True})
        self.assert_rebound(result)
        report = read(self.project, "objective")["admissions"][admission["admission_id"]]["report"]
        self.assertEqual(report["status"], "validated_observation")
        self.assertEqual(report["outcome"], "succeeded")

    def test_checkpoint_without_link_keeps_rebind_and_closure_unwritten(self):
        self.linked()
        self.advance_policy()
        self.change_runtime()
        self.remote.linked.clear()
        before = self.saved()
        error = self.failure("governance_changed", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.closes, [])
        self.assert_lead_hold(error)

    def test_report_without_link_keeps_rebind_and_consumption_unwritten(self):
        admission, frozen = self.linked(report=True)
        self.advance_policy()
        self.change_runtime()
        self.remote.linked.clear()
        before = self.saved()
        error = self.failure("governance_changed", self.report, admission, frozen,
                             map={"obligations": self.closing_rows(), "close": True})
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.closes, [])
        self.assert_lead_hold(error)

    def test_unmerged_link_after_runtime_change_still_refuses_without_writes(self):
        self.linked()
        self.advance_policy()
        self.change_runtime()
        self.remote.linked[("acme/example", 7)][0]["state"] = "OPEN"
        before = self.saved()
        self.failure("governance_changed", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.closes, [])

    def test_changed_generation_cannot_use_the_completion_exemption(self):
        self.linked()
        self.advance_policy()
        self.change_runtime()
        self.current["consumer_generation"] += 1
        before, calls = self.saved(), deepcopy(self.remote.calls)
        self.failure("native_authority_unverified", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.remote.calls, calls)

    def test_checkpoint_finalization_refusal_does_not_persist_a_proved_rebind(self):
        self.linked()
        self.advance_policy()
        self.change_runtime()
        rows = self.stored()
        rows[0].pop("evidence")
        rows[0].update(state="active", executor="coordinator")
        before = self.saved()
        self.failure("obligation_unaccounted", self.write, rows, close=True)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.closes, [])

    def test_report_finalization_refusal_does_not_persist_rebind_or_consumption(self):
        admission, frozen = self.linked(report=True)
        self.advance_policy()
        self.change_runtime()
        before = self.saved()
        self.failure("obligation_unaccounted", self.report, admission, frozen, map={"close": True})
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.closes, [])

    def test_proposed_map_cannot_replace_the_authority_joined_baseline(self):
        self.linked()
        self.advance_policy()
        state = read(self.project, "objective")
        proposed = deepcopy(state["checkpoint"])
        proposed["seq"] += 1
        before, calls = self.saved(), deepcopy(self.remote.calls)
        self.assertIsNone(closure_keeps_baseline(self.project, proposed, state=state))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.remote.calls, calls)

    def test_direct_completion_without_merge_names_a_blocked_project_lead_task(self):
        self.intake()
        self.prove()
        self.advance_policy()
        before = self.saved()
        error = self.failure("governance_changed", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assert_lead_hold(error)


if __name__ == "__main__":
    unittest.main()

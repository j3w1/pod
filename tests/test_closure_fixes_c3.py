"""A sweep observation cannot authorize a later source completion effect."""

import json
import unittest
from unittest.mock import patch

from pod.evidence_closure import sweep
from pod.ledger import objective_root
from tests.issue_port_support import patch_issue_port
from tests.pod_tick_support import TickCase
from tests import test_closure_fixes_c2 as currency
from tests.test_completed_closure import SourcePort
from tests.test_evidence_closure import _questions


class FreshSourceTests(TickCase):
    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        replacement = patch_issue_port(self.issue)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.bind()
        self.prove()
        self.tick()
        self.issue.close_result = "refused"
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "refused")
        # Synthetic peer: the same source, closed before its latest reopen. The real closing write above
        # closed objective after that reopen, so only objective may retry source completion.
        path = objective_root(self.project, "objective") / "context.json"
        peer = json.loads(path.read_text())
        peer["checkpoint"]["objective"] = "another"
        peer["checkpoint"]["closure"]["at"] = "2000-01-01T00:00:00Z"
        other = objective_root(self.project, "another")
        other.mkdir(parents=True)
        (other / "context.json").write_text(json.dumps(peer))
        self.issue.state_reason = "REOPENED"
        self.issue.reopened_at = "2001-01-01T00:00:00Z"
        self.issue.close_result = "accepted"
        self.issue.calls.clear()
        self.issue.closes.clear()

    def sweep_after_observation(self, edit):
        root = objective_root(self.project, "objective").parent
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name != ".lock"}
        original = self.issue.issue
        reads = []

        def observe(**arguments):
            row = original(**arguments)
            reads.append((row["state"], row["stateReason"]))
            if len(reads) == 1:
                edit()
            return row

        with patch.object(self.issue, "issue", side_effect=observe):
            result = sweep(self.project)
        after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name != ".lock"}
        self.assertEqual(after, before)
        self.assertEqual(_questions(result), [])
        rows = {row["objective"]: row for row in result["repositories"][0]["objectives"]}
        self.assertEqual(rows["another"]["source_issues"][0]["outcome"], "reopened")
        self.assertEqual(rows["another"]["party"], "lead")
        return rows["objective"], reads

    def test_persons_not_planned_close_after_cached_read_stays_not_planned(self):
        def close_as_not_planned():
            self.issue.state, self.issue.state_reason = "CLOSED", "NOT_PLANNED"

        row, reads = self.sweep_after_observation(close_as_not_planned)
        effect = row["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["state_reason"]), ("closed_other_reason", "NOT_PLANNED"))
        self.assertEqual((row["party"], effect["task_with"]), (None, "another"))
        self.assertEqual(reads, [("OPEN", "REOPENED"), ("CLOSED", "NOT_PLANNED")])
        self.assertEqual((self.issue.state, self.issue.state_reason), ("CLOSED", "NOT_PLANNED"))
        self.assertEqual(self.issue.closes, [])

    def test_cached_preclosure_reopen_still_closes_after_its_own_fresh_read(self):
        at_send = []
        self.issue.close_hook = lambda: at_send.append(len(self.issue.calls))
        row, reads = self.sweep_after_observation(lambda: None)
        self.assertEqual(row["source_issues"][0]["outcome"], "closed")
        self.assertEqual(reads, [("OPEN", "REOPENED"), ("OPEN", "REOPENED"), ("CLOSED", "COMPLETED")])
        self.assertEqual(at_send, [2])
        self.assertEqual(len(self.issue.closes), 1)

    def test_fresh_source_body_change_holds_the_cached_completion(self):
        def change_body():
            self.issue.body += "\nNew work remains"

        row, reads = self.sweep_after_observation(change_body)
        effect = row["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["reason"]), ("refused", "source_changed"))
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.issue.closes, [])

    def test_fresh_reopen_timeline_holds_a_later_persons_reopen(self):
        # The first timeline read describes another's observation; a later reopen happens after it.
        original = self.issue.issue_reopened_at

        def timeline(**arguments):
            stamp = original(**arguments)
            self.issue.reopened_at = "2099-01-01T00:00:00Z"
            return stamp

        with patch.object(self.issue, "issue_reopened_at", side_effect=timeline):
            row, reads = self.sweep_after_observation(lambda: None)
        effect = row["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["reason"]), ("reopened", "source_reopened"))
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.issue.closes, [])


class CompletionGuidanceTests(currency.ReboundCompletionCase):
    def test_missing_merge_names_unavailable_proof_and_the_leads_task(self):
        self.intake()
        self.prove()
        self.advance_policy()
        before = self.saved()
        error = self.failure("governance_changed", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assert_lead_hold(error)
        action = error.detail["next_action"]
        self.assertIn("delivery proof is unavailable", action)
        self.assertIn("the project lead", action)
        self.assertNotIn("report the unavailable completion proof to the project lead", action)


if __name__ == "__main__":
    unittest.main()

"""R155 / A791: a recorded closure replays exactly as its closing write accepted it (pod#117)."""

import json
import unittest

from pod.evidence_closure import close_source_issues, sweep
from pod.governor import objective_delivery_reporting
from pod.ledger import closed_snapshot_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from tests import test_pod_ticks_closure as tick_closure
from tests.common import logical_json
from tests.issue_port_support import patch_issue_port
from tests.kernel_support import git
from tests.pod_tick_support import TickCase
from tests.test_completed_closure import SourcePort
from tests.test_standing_authorization import StandingCase


class ConsumedReconciliationTests(TickCase):
    """A settled worker's report consumed as `reconciliation_required` settles it in the replay too."""

    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        patched = patch_issue_port(self.issue)
        patched.start(); self.addCleanup(patched.stop)

    def reconciled_worker(self, *, scope=None, files=()):
        frozen, admission = tick_closure.ClosureTests.worker(self)
        body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admission["native_binding"]["dispatchId"], "candidate": frozen["body"]["candidate"],
                "outcome": "succeeded", "scope": scope or frozen["body"]["scope"], "files": list(files),
                "checks": ["unit"], "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        self.op("report", admission_id=admission["admission_id"], packet=frozen, report=body,
                map={"obligations": tick_closure.ClosureTests.proof_rows(self)})
        consumed = read(self.project, "objective")["admissions"][admission["admission_id"]]["report"]
        self.assertEqual(consumed["status"], "reconciliation_required")
        return admission["admission_id"]

    def close_in_one_step(self):
        self.tick()
        result = self.write(close=True)
        self.assertTrue(read(self.project, "objective")["checkpoint"]["closure"])
        self.assertEqual(result["source_issues"][0]["outcome"], "closed")
        self.assertEqual(result["source_issues"][0]["locator"], self.locator)
        self.assertEqual(len(self.issue.closes), 1)
        return result

    def test_a_prose_scope_report_closes_the_source_in_the_closing_step_and_sweep_is_a_no_op(self):
        self.reconciled_worker(scope=["a prose summary of the assigned work"])
        self.close_in_one_step()
        before = self.saved()
        again = sweep(self.project, objectives=["objective"])
        self.assertEqual(again["repositories"][0]["objectives"][0]["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual((len(self.issue.closes), self.saved()), (1, before))

    def test_a_file_outside_the_assignment_closes_the_source_in_the_closing_step(self):
        self.reconciled_worker(files=["elsewhere/changed.py"])
        self.close_in_one_step()

    def test_a_report_without_a_verifiable_settlement_record_stays_outstanding_and_is_named(self):
        key = self.reconciled_worker(scope=["a prose summary of the assigned work"])
        self.issue.close_result = "refused"
        self.tick()
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "refused")
        path = objective_root(self.project, "objective") / "context.json"
        original = path.read_bytes()
        for label, tamper in (("changed digest", lambda row: row["report"].update(observation_digest="0" * 64)),
                              ("no consumed report", lambda row: row.update(report=None))):
            with self.subTest(label):
                raw = logical_json(path); tamper(raw["admissions"][key])
                path.write_text(json.dumps(raw))
                closes = len(self.issue.closes)
                row = close_source_issues(self.project, "objective")["source_issues"][0]
                self.assertEqual((row["outcome"], row["reason"], row["detail"], row["locator"]),
                                 ("unverified", "obligation_unaccounted", "outstanding_unaccounted", self.locator))
                self.assertEqual(row["referent"]["admission"], key)
                self.assertIn("obligation", row["referent"])
                self.assertIn(key, row["message"])
                self.assertIn("project lead", row["next_action"])
                self.assertIn("no owner decision is needed", row["next_action"])
                self.assertEqual(len(self.issue.closes), closes)
                path.write_bytes(original)

    def test_a_refusal_before_closure_names_the_bound_source(self):
        self.bind(); self.prove(); self.tick()
        row = close_source_issues(self.project, "objective")["source_issues"][0]
        self.assertEqual((row["outcome"], row["reason"], row["locator"]),
                         ("refused", "objective_not_closed", self.locator))
        self.assertEqual(self.issue.closes, [])


class YoloDeliveryReplayTests(StandingCase):
    """The closure report keeps the consent its closing write judged; closure still ends yolo for new effects."""

    def deliver_under_yolo(self):
        delivery = {"id": "D", "kind": "delivery", "provenance": "user_direct",
                    "source": {"instruction": "Merge the candidate"}, "check": "merged",
                    "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), delivery],
                        revision_authority={"provenance": "user_direct", "instruction": "Merge the candidate"})
        self.activate()
        pushed = self.case.op("governor", action=self.proposed())
        self.case.remote.heads["origin/candidate"] = self.case.candidate
        self.case.op("governor-outcome", record_id=pushed["record_id"], outcome="PASS")
        merged = self.case.op("governor", action=self.proposed("merge", pr=23))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge candidate", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.case.op("governor-outcome", record_id=merged["record_id"], outcome="PASS",
                     provider={"merge_commit": result})
        self.case.checkpoint(delivery={"record": merged["record_id"]})
        return [self.case.criterion("satisfied", evidence=[self.case.proof("O1")]),
                {**{key: value for key, value in delivery.items() if key != "wait"},
                 "state": "satisfied", "evidence": [self.case.proof("D")]}]

    def close_and_replay(self, rows, expected):
        closed = self.case.write(rows, close=True)["checkpoint"]["closure"]["report"]
        self.assertEqual(closed["delivery_authorization"], {"delivery": {"publish": expected, "merge": expected}})
        self.assertEqual(closed["standing_authorization"]["state"], "ended")
        stored = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", stored)), ())
        self.assertEqual(objective_delivery_reporting(self.project, "objective")["units"]["delivery"],
                         {"publish": expected, "merge": expected})
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"], [])
        return closed

    def test_a_yolo_delivered_closure_replays_its_recorded_consent(self):
        closed = self.close_and_replay(self.deliver_under_yolo(), "RECORDED")
        self.assertTrue(closed["standing_authorization"]["at_closure"]["active_until_closure"])
        # Closure still ends yolo for any new effect.
        self.assertEqual(self.case.op("governor-status")["units"]["delivery"]["authorization"],
                         {"publish": "MISSING", "merge": "MISSING"})

    def test_consent_revoked_before_closure_still_reads_missing(self):
        rows = self.deliver_under_yolo()
        self.operation("revoke")
        closed = self.close_and_replay(rows, "MISSING")
        self.assertFalse(closed["standing_authorization"]["at_closure"]["active_until_closure"])

    def test_consent_replaced_before_closure_still_reads_missing(self):
        rows = self.deliver_under_yolo()
        self.activate(scope=["publish"])
        closed = self.close_and_replay(rows, "MISSING")
        self.assertTrue(closed["standing_authorization"]["at_closure"]["active_until_closure"])

if __name__ == "__main__":
    unittest.main()

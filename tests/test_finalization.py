"""R2 finalization: what a coordinator still records before it says the objective is finished."""

from copy import deepcopy
import unittest

from pod.governor import FINALIZATION_RECORDS, finalization


def row(record_id, kind, outcome, *, decision="ALLOW", derived_from=None):
    return {"record_id": record_id, "decision": decision, "outcome": outcome,
            "action": {"kind": kind}, "derived_from": derived_from}


def admission(role, state, *, report=None, disposition=None):
    return {"role": role, "state": state, "report": report, "disposition": disposition}


def context(*, admissions=None, closure=None, delivery=None, mapped=True):
    checkpoint = {"obligations": [{"id": "O1"}], "closure": closure, "delivery": delivery} if mapped else {}
    return {"admissions": admissions or {}, "checkpoint": checkpoint}


class FinalizationTests(unittest.TestCase):
    def test_every_remaining_record_is_named_in_recording_order(self):
        journal = {"actions": [
            row("p" * 64, "pr_update", "PASS"),
            row("d" * 64, "workflow_dispatch", "pending", derived_from="p" * 64),
            row("r" * 64, "release", "UNKNOWN"),
            row("x" * 64, "push", "pending", decision="DEFER"),
            row("m" * 64, "merge", "pending"),
            row("n" * 64, "merge", "PASS"),
        ]}
        state = context(admissions={
            "review-1": admission("review", "bound"),
            "impl-1": admission("implement", "bound", report={"status": "validated_observation"}),
            "impl-2": admission("implement", "closed"),
            "impl-3": admission("implement", "deferred"),
        })
        result = finalization(state, journal)
        self.assertEqual(result["remaining"], list(FINALIZATION_RECORDS))
        self.assertFalse(result["finished"])
        self.assertEqual(result["records"]["merge_outcome"],
                         [{"record": "m" * 64, "kind": "merge", "state": "pending", "derived_from": None}])
        self.assertEqual(result["records"]["delivery_record"],
                         [{"record": "n" * 64, "kind": "merge", "state": "PASS", "derived_from": None}])
        self.assertEqual(result["records"]["validation_readback"], [
            {"record": "d" * 64, "kind": "workflow_dispatch", "state": "pending", "derived_from": "p" * 64},
            {"record": "r" * 64, "kind": "release", "state": "UNKNOWN", "derived_from": None}])
        self.assertEqual(result["records"]["report_consumption"], [
            {"record": "impl-1", "kind": "implement", "state": "undispositioned", "derived_from": None},
            {"record": "review-1", "kind": "review", "state": "bound", "derived_from": None}])
        self.assertEqual(result["records"]["closure"], [])
        self.assertTrue(result["next_action"].startswith(
            "Not finished: record merge_outcome, delivery_record, validation_readback, report_consumption, "
            "closure in that order before saying the objective is finished or the coordinator safe to retire. "
            "Next: fetch the merge target, then record governor-outcome for merge " + "m" * 12))
        self.assertIn("never merge again", result["next_action"])

    def test_the_next_action_follows_the_first_remaining_record(self):
        cases = (
            ([row("n" * 64, "merge", "PASS")], {}, ["delivery_record", "closure"],
             "write a checkpoint with delivery: {record: " + "n" * 12 + "}"),
            ([row("n" * 64, "merge", "PASS"), row("d" * 64, "workflow_dispatch", "pending")],
             {"delivery": {"record": "n" * 64}}, ["validation_readback", "closure"],
             "settle " + "d" * 12 + " from Pod's own readback with governor-reconcile"),
            ([], {"admissions": {"review-1": admission("review", "reserved")}}, ["report_consumption", "closure"],
             "consume the worker report of admission review-1 with internal report"),
            ([row("n" * 64, "merge", "PASS")], {"delivery": {"record": "n" * 64}}, ["closure"],
             "write a checkpoint with close: true"),
        )
        for actions, fields, remaining, step in cases:
            with self.subTest(remaining=remaining):
                result = finalization(context(**fields), {"actions": actions})
                self.assertEqual((result["remaining"], result["finished"]), (remaining, False))
                self.assertEqual(list(result["records"]), remaining)
                self.assertIn("Next: " + step, result["next_action"])

    def test_closure_ends_finalization_and_only_then_reports_finished(self):
        journal = {"actions": [row("n" * 64, "merge", "PASS")]}
        closed = finalization(context(closure={"seq": 4, "revision": 4}), journal)
        self.assertEqual((closed["remaining"], closed["records"], closed["finished"]), ([], {}, True))
        self.assertTrue(closed["next_action"].startswith("Finished: closure is recorded and Pod needs nothing more"))
        self.assertIn("safe to retire", closed["next_action"])
        open_map = finalization(context(delivery={"record": "n" * 64}), journal)
        self.assertEqual((open_map["remaining"], open_map["finished"]), (["closure"], False))

    def test_a_mapless_objective_has_no_delivery_record_or_closure_to_write(self):
        pending = finalization(context(mapped=False), {"actions": [row("n" * 64, "merge", "PASS"),
                                                                   row("p" * 64, "push", "UNKNOWN")]})
        self.assertEqual(pending["remaining"], ["validation_readback"])
        settled = finalization(context(mapped=False), {"actions": [row("p" * 64, "push", "PASS")]})
        self.assertEqual((settled["remaining"], settled["finished"]), ([], True))
        self.assertIn("no obligation map to close", settled["next_action"])
        self.assertEqual(finalization(None, {"actions": []})["remaining"], [])

    def test_the_projection_reads_and_changes_nothing(self):
        state = context(admissions={"review-1": admission("review", "bound")})
        journal = {"actions": [row(str(index) * 64, "workflow_dispatch", "pending") for index in range(6)]}
        before = deepcopy((state, journal))
        result = finalization(state, journal)
        self.assertEqual((state, journal), before)
        self.assertIn("and 2 more", result["next_action"])
        self.assertEqual(result["schema"], "pod-finalization/v1")


if __name__ == "__main__":
    unittest.main()

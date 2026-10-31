import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests.kernel_support import KernelCase
from tests.test_decision_visibility import question
from tests import test_status_selection as selection

from pod import wake
from pod.errors import PodError
from pod.orca import mailbox_check

RUN = "run_fixture"


class FakeMailbox:
    """Native `check` semantics: FIFO batches replay until acknowledged, and a wait returns pending mail at once."""

    def __init__(self):
        self.pending, self.delivered, self.calls, self.serial = [], None, [], 0
        self.before_wait = None
        self.wait_result = {}
        self.refuse = {}

    def send(self, type_="worker_done", dispatch="d1", outcome="succeeded", id_=None):
        self.serial += 1
        self.pending.append({"id": id_ or f"msg_{self.serial}", "type": type_, "from_handle": "term_w",
                             "subject": "done", "payload": json.dumps({"dispatchId": dispatch, "outcome": outcome})})

    def batch(self):
        replayed = self.delivered is not None
        if self.delivered is None and self.pending:
            self.serial += 1
            self.delivered = {"delivery": f"delivery_{self.serial}", "messages": list(self.pending)}
        if self.delivered is None:
            return self.empty()
        return {"runtime": "rt", "delivery": self.delivered["delivery"], "messages": self.delivered["messages"],
                "replayed": replayed, "timed_out": False, "cancelled": False, "connection_lost": False}

    @staticmethod
    def empty(**flags):
        return {"runtime": "rt", "delivery": None, "messages": [], "replayed": False, "timed_out": False,
                "cancelled": False, "connection_lost": False, **flags}

    def check(self, run, *, ack=None, wait_ms=None):
        self.calls.append({"run": run, "ack": ack, "wait_ms": wait_ms})
        if "check" in self.refuse:
            raise PodError("mailbox_refused", "refused", {"native_code": self.refuse["check"]})
        if ack is not None:
            if "ack" in self.refuse:
                raise PodError("mailbox_refused", "refused", {"native_code": self.refuse["ack"]})
            if self.delivered is None or self.delivered["delivery"] != ack:
                raise PodError("mailbox_refused", "stale", {"native_code": "stale_delivery"})
            self.pending = [m for m in self.pending if m not in self.delivered["messages"]]
            self.delivered = None
        if wait_ms is not None and self.before_wait:
            self.before_wait()
        result = self.batch()
        if wait_ms is not None and not result["messages"]:
            return {**self.empty(**self.wait_result), **self.wait_result}
        return result


def bound(run=RUN):
    return lambda: {"runtime": "rt", "run": {"id": run, "coordinator_handle": "term_c", "consumer_generation": 1}}


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.mail = FakeMailbox()

    def arm(self, **options):
        return wake.arm(RUN, check_fn=self.mail.check, run_fn=bound(), **options)

    def waits(self):
        return [call for call in self.mail.calls if call["wait_ms"] is not None]

    def test_event_between_inspection_and_wait_arm_is_not_lost(self):
        self.mail.before_wait = lambda: self.mail.send()
        result = self.arm()
        self.assertEqual(result["status"], "events")
        self.assertTrue(result["waited"])
        self.assertEqual([event["kind"] for event in result["events"]], ["completion"])
        self.assertEqual(len(self.waits()), 1)
        self.assertEqual(self.mail.calls[0]["wait_ms"], None)

    def test_mail_pending_before_the_wait_is_returned_without_arming_one(self):
        self.mail.send()
        result = self.arm()
        self.assertEqual((result["status"], result["waited"]), ("events", False))
        self.assertEqual(self.waits(), [])

    def test_a_duplicate_is_delivered_once(self):
        self.mail.send(id_="msg_a")
        self.mail.send(id_="msg_a")
        self.mail.send(id_="msg_b", dispatch="d2")
        result = self.arm()
        self.assertEqual([event["id"] for event in result["events"]], ["msg_a", "msg_b"])
        self.assertEqual([(row["id"], row["reason"]) for row in result["duplicates"]],
                         [("msg_a", "repeated message")])

    def test_second_settlement_report_for_one_dispatch_is_one_event(self):
        self.mail.send(id_="msg_a", dispatch="d1")
        self.mail.send(id_="msg_b", dispatch="d1")
        self.mail.send(id_="msg_c", dispatch="d2", outcome="failed")
        result = self.arm()
        self.assertEqual([(e["id"], e["kind"]) for e in result["events"]],
                         [("msg_a", "completion"), ("msg_c", "failure")])
        self.assertEqual(result["duplicates"][0]["duplicate_of"], "msg_a")

    def test_wake_after_compaction_replays_the_unacknowledged_batch_then_continues(self):
        self.mail.send()
        first = self.arm()
        # Context compaction or a restart: the next call knows no delivery identity and replays the same batch.
        again = self.arm()
        self.assertTrue(again["replayed"] and not first["replayed"])
        self.assertEqual((again["delivery"], again["events"]), (first["delivery"], first["events"]))
        self.assertEqual(self.waits(), [])
        resumed = self.arm(ack=again["delivery"], timeout_ms=5)
        self.assertEqual((resumed["status"], resumed["waited"]), ("timeout", True))
        self.assertEqual(resumed["ack"]["status"], "applied")
        self.assertEqual(self.mail.pending, [])
        self.assertEqual(self.mail.calls[-2]["ack"], first["delivery"])

    def test_one_wait_per_call_and_a_timeout_is_a_checkpoint_not_a_rearm(self):
        self.mail.wait_result = {"timed_out": True}
        result = self.arm(timeout_ms=7)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.waits(), [{"run": RUN, "ack": None, "wait_ms": 7}])
        self.assertIn("worker-list", result["next_action"])

    def test_a_lost_connection_consumes_nothing_and_leaves_no_armed_wait(self):
        self.mail.wait_result = {"connection_lost": True, "cancelled": True}
        result = self.arm()
        self.assertEqual(result["status"], "disconnected")
        self.assertEqual(result["events"], [])

    def test_a_fenced_coordinator_stops_before_waiting(self):
        self.mail.refuse["check"] = "consumer_fenced"
        result = self.arm()
        self.assertEqual((result["status"], result["waited"]), ("fenced", False))
        self.assertEqual(self.waits(), [])

    def test_a_repeated_acknowledgement_reads_what_is_pending_now(self):
        self.mail.send()
        delivery = self.arm()["delivery"]
        self.arm(ack=delivery, timeout_ms=5)
        self.mail.send()
        result = self.arm(ack=delivery)
        self.assertEqual(result["ack"]["status"], "already_settled")
        self.assertEqual(result["status"], "events")

    def test_other_native_refusals_and_an_unbound_run_are_refused(self):
        self.mail.refuse["check"] = "something_else"
        with self.assertRaises(PodError):
            self.arm()
        with self.assertRaises(PodError) as caught:
            wake.arm(RUN, check_fn=self.mail.check, run_fn=bound("other"))
        self.assertEqual(caught.exception.code, "wake_run_not_bound")
        with self.assertRaises(PodError) as caught:
            self.arm(timeout_ms=wake.MAX_WAIT_MS + 1)
        self.assertEqual(caught.exception.code, "invalid_wake_timeout")
        self.assertEqual([call for call in self.mail.calls if call["wait_ms"] is not None], [])

    def test_every_message_type_wakes_and_each_is_classified(self):
        for type_, outcome in (("worker_done", "succeeded"), ("worker_done", "failed"), ("worker_done", None),
                               ("decision", None), ("escalation", None), ("heartbeat", None), ("status", None), ("custom", None)):
            self.mail.send(type_=type_, dispatch=f"d_{len(self.mail.pending)}", outcome=outcome)
        kinds = [event["kind"] for event in self.arm()["events"]]
        self.assertEqual(kinds, ["completion", "failure", "completion_unproven", "decision", "question",
                                 "informational", "message", "message"])

    def test_a_rejected_settlement_is_not_a_completion(self):
        self.mail.pending.append({"id": "msg_r", "type": "worker_done", "subject": "Rejected worker_done: x",
                                  "payload": json.dumps({"dispatchId": "d1", "outcome": "succeeded",
                                                         "_orcaLifecycleRejection": {"code": "missing_task_id"}})})
        self.mail.send(id_="msg_ok", dispatch="d1")
        result = self.arm()
        self.assertEqual([(e["id"], e["kind"]) for e in result["events"]],
                         [("msg_r", "lifecycle_rejected"), ("msg_ok", "completion")])
        self.assertEqual(result["duplicates"], [])

    def test_events_carry_the_native_text_and_payload_the_coordinator_must_act_on(self):
        rows = [("status", "Correction", "Stop editing parser.py and preserve the uncommitted draft.",
                 {"candidate": "c" * 40}),
                ("question", "Choose", "Which base should the retry use?", {"choices": ["main", "release"]}),
                ("decision", "Answer", "Use the release base.", {"verification": {"reference": "relay-1"}}),
                ("worker_done", "Done", "Fixed two findings.", {"dispatchId": "d9", "taskId": "t9",
                                                              "outcome": "succeeded", "reportPath": "/r/report.json"})]
        for type_, subject, body, payload in rows:
            self.mail.pending.append({"id": "msg_" + type_, "type": type_, "subject": subject, "body": body,
                                      "payload": json.dumps(payload), "thread_id": "th1", "priority": "high"})
        replay = [self.arm(), self.arm()]
        self.assertTrue(replay[1]["replayed"])
        for result in replay:
            seen = {event["type"]: event for event in result["events"]}
            for type_, subject, body, payload in rows:
                event = seen[type_]
                self.assertEqual((event["body"], event["subject"], event["thread"], event["priority"]),
                                 (body, subject, "th1", "high"))
                self.assertEqual({key: event["payload"][key] for key in payload}, payload)
                self.assertEqual(event["truncated"], [])
        self.assertEqual(seen["worker_done"]["kind"], "completion")

    def test_content_over_the_bounds_is_flagged_and_never_silently_dropped(self):
        self.mail.pending.append({"id": "msg_big", "type": "status", "subject": "Large",
                                  "body": "x" * (wake.BODY_LIMIT + 1),
                                  "payload": json.dumps({"blob": "y" * (wake.PAYLOAD_LIMIT + 1)})})
        result = self.arm()
        event = result["events"][0]
        self.assertEqual(event["truncated"], ["body", "payload"])
        self.assertEqual(len(event["body"]), wake.BODY_LIMIT)
        self.assertIsNone(event["payload"])
        self.assertIn("check --run " + RUN + " --all", result["next_action"])
        self.assertLess(result["next_action"].index("--all"), result["next_action"].index("acknowledge delivery"))

    def test_a_second_settlement_report_keeps_its_own_content(self):
        self.mail.pending.append({"id": "msg_1", "type": "worker_done", "subject": "first", "body": "first body",
                                  "payload": json.dumps({"dispatchId": "d1", "outcome": "succeeded"})})
        self.mail.pending.append({"id": "msg_2", "type": "worker_done", "subject": "second", "body": "new finding",
                                  "payload": json.dumps({"dispatchId": "d1", "outcome": "failed",
                                                         "reportPath": "/r/2.json"})})
        duplicate = self.arm()["duplicates"][0]
        self.assertEqual((duplicate["body"], duplicate["outcome"], duplicate["payload"]["reportPath"]),
                         ("new finding", "failed", "/r/2.json"))

    def test_status_reports_a_quiet_coordinator_that_owns_work_and_never_invents_an_armed_flag(self):
        block = lambda **owned: wake.status_block(RUN, "term_c", **owned)
        self.assertEqual(block(assignments=0, awaited_replies=0, activity_fn=lambda h: 10 ** 9)["state"], "not_needed")
        for owned in ({"assignments": 1, "awaited_replies": 0}, {"assignments": 0, "awaited_replies": 1}):
            quiet = block(**owned, activity_fn=lambda h: 300_000)
            self.assertEqual((quiet["state"], quiet["coordinator_quiet_seconds"], quiet["wait"]),
                             ("quiet_with_owned_work", 300, "unknown"))
            self.assertIn("pod wake --run " + RUN, quiet["next_action"])
            for activity in (5_000, None):
                active = block(**owned, activity_fn=lambda h, a=activity: a)
                self.assertEqual((active["state"], active["wait"], active["next_action"]), ("owned_work", "unknown", None))
        self.assertNotIn("armed", json.dumps(block(assignments=1, awaited_replies=0, activity_fn=lambda h: 1)))

    def test_owned_work_counts_unconsumed_reports_unverified_assignments_and_unknown_replies_once(self):
        assignments = {"active": [{"admission": "a1"}], "settled": [{"admission": "a2"}],
                       "unverified": [{"admission": "a3"}]}
        owned = wake.owned_work(assignments, [{"obligation": "O2"}], [{"record": "a2"}, {"record": "a1"}],
                                [{"missing": []}, {"missing": ["resolution", "categories"]}], closed=False)
        self.assertEqual(owned, {"assignments": 3, "unconsumed_reports": 1, "awaited_replies": 3, "unknown": 1})
        self.assertEqual(wake.owned_work(assignments, [{}], [{"record": "a2"}], [{}], closed=True),
                         {"assignments": 0, "awaited_replies": 0, "unconsumed_reports": 0, "unknown": 0})


class WakeStatusTests(KernelCase):
    """The production status path: wake ownership follows the existing question, settlement and finalization facts."""

    def wake(self, **options):
        with patch("pod.orca.terminal_activity", return_value=600_000):
            return selection.StatusSelectionTests.status(self, "--objective", "objective", **options)["wake"]

    def test_a_pending_question_is_an_awaited_reply_for_a_quiet_coordinator(self):
        self.intake(questions=[question()])
        pending = self.wake()
        self.assertEqual((pending["state"], pending["wait"]), ("quiet_with_owned_work", "unknown"))
        self.assertEqual((pending["owned"]["awaited_replies"], pending["owned"]["unknown_replies"]), (1, 0))
        self.assertIn("pod wake --run run", pending["next_action"])

    def test_resolved_questions_are_not_awaited_replies(self):
        self.intake(questions=[question(state="answered"), question("second", state="resolved")])
        resolved = self.wake()
        self.assertEqual((resolved["state"], resolved["owned"]["awaited_replies"]), ("not_needed", 0))

    def test_legacy_and_contradictory_questions_count_and_stay_unknown(self):
        self.intake(questions=[{"id": "older", "text": "Decide", "choices": []}])
        legacy = self.wake()
        self.assertEqual((legacy["state"], legacy["owned"]["awaited_replies"], legacy["owned"]["unknown_replies"]),
                         ("quiet_with_owned_work", 1, 1))

    def test_a_settled_worker_whose_report_is_unconsumed_is_owned_until_consumed(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review")
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        owned = self.wake()
        self.assertEqual(owned["state"], "quiet_with_owned_work")
        self.assertEqual(owned["owned"]["assignments"], 1)
        rows = self.stored()
        row = next(item for item in rows if item["id"] == "A")
        row.pop("executor")
        row.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        self.report(admission, frozen, map={"obligations": rows})
        self.assertEqual(self.wake()["state"], "not_needed")


class MailboxCheckTests(unittest.TestCase):
    def run_check(self, payload, **options):
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("pod.orca.executable", return_value=Path("/fixture/orca")), \
             patch("pod.orca.subprocess.run", return_value=done) as runner:
            return mailbox_check(RUN, **options), runner

    def test_argument_shape_is_exact_and_the_process_outlives_the_native_timeout(self):
        payload = {"ok": True, "result": {"messages": [], "count": 0, "timedOut": True}, "_meta": {"runtimeId": "rt"}}
        result, runner = self.run_check(payload, ack="delivery_1", wait_ms=2000)
        argv = runner.call_args.args[0]
        self.assertEqual(argv, ["/fixture/orca", "orchestration", "check", "--run", RUN, "--ack", "delivery_1",
                                "--wait", "--timeout-ms", "2000", "--json"])
        self.assertGreater(runner.call_args.kwargs["timeout"], 2)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["delivery"])
        for forbidden in ("--types", "--peek", "--all", "--from", "--terminal"):
            self.assertNotIn(forbidden, argv)

    def test_a_batch_carries_its_delivery_identity_and_replay_flag(self):
        message = {"id": "m1", "type": "worker_done"}
        result, _ = self.run_check({"ok": True, "result": {"messages": [message], "count": 1, "deliveryId": "delivery_9",
                                                          "replayed": True}, "_meta": {"runtimeId": "rt"}})
        self.assertEqual((result["delivery"], result["replayed"], result["messages"]), ("delivery_9", True, [message]))
        with self.assertRaises(PodError) as caught:
            self.run_check({"ok": True, "result": {"messages": [message]}, "_meta": {"runtimeId": "rt"}})
        self.assertEqual(caught.exception.code, "orca_contract")

    def test_native_refusals_keep_their_code_and_bad_input_never_starts_a_process(self):
        refused = {"ok": False, "error": {"code": "consumer_fenced", "message": "x"}, "_meta": {"runtimeId": "rt"}}
        with self.assertRaises(PodError) as caught:
            self.run_check(refused)
        self.assertEqual((caught.exception.code, caught.exception.detail), ("mailbox_refused",
                                                                           {"native_code": "consumer_fenced"}))
        with patch("pod.orca.subprocess.run") as runner:
            for bad in ({"run": "-x"}, {"run": RUN, "ack": "--wait"}, {"run": RUN, "wait_ms": 0}):
                with self.assertRaises(PodError):
                    mailbox_check(**bad)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()

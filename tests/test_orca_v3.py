"""R214: Orchestration V3 adoption, feature-detected from fake capability sets; no live Orca is touched.

A pre-V3 runtime (the installed 1.4.230 capability shape: no V3 strings, the same contract string) keeps every
native argv and result unchanged; V3 drains mail, treats acknowledged deliveries as final, classifies turn-stop
reports and keeps workers open only when retention or reuse is intended."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod import orca, wake
from pod.errors import PodError
from pod.ledger import read, update_admission
from pod.operations import recover_admission
from pod.operations import OrcaPort
from pod.orca import V3_CAPABILITY_KEYS, mailbox_check, mutate_command, orchestration_features
from pod.worker_tabs import project as tab_project
from tests import test_native_retry as retry_support
from tests.test_wake import FakeMailbox, RUN, bound

# The orchestration strings the installed Orca 1.4.230 advertises (read-only probe of `orca status --json`).
PRE_V3_ADVERTISED = ["orchestration.federation.v1", "orchestration.federation-control-mail.v1",
                     "orchestration.federation-lifecycle-settlement.v1", "orchestration.worker-stop-verdict.v1",
                     "orchestration.worker-launch-preferences.v1", "orchestration.federation-structured-read.v1",
                     "orchestration.federation-fleet-snapshot.v1", "orchestration.federation-release-archive.v1",
                     "orchestration.contract.v1"]
V3_ADVERTISED = PRE_V3_ADVERTISED + list(V3_CAPABILITY_KEYS.values())
PRE_V3 = orchestration_features(PRE_V3_ADVERTISED)
V3 = orchestration_features(V3_ADVERTISED)
UNREAD = orca.features_unread("orca_read_failed")


def status_envelope(capabilities):
    return {"runtime": "rt", "result": {"runtime": {"state": "ready", "capabilities": capabilities}},
            "request_uuid": None}


class FeatureDetectionTests(unittest.TestCase):
    def test_the_installed_pre_v3_list_is_pre_v3_and_all_six_strings_are_v3(self):
        self.assertEqual((PRE_V3["mode"], PRE_V3["basis"]), ("pre_v3", "advertised"))
        self.assertFalse(any(PRE_V3["capabilities"].values()))
        self.assertEqual(V3["mode"], "v3")
        self.assertTrue(all(V3["capabilities"].values()))
        # The contract string is on both runtimes; five of the six are still pre-V3.
        self.assertIn("orchestration.contract.v1", PRE_V3_ADVERTISED)
        partial = orchestration_features(V3_ADVERTISED[:-1])
        self.assertEqual(partial["mode"], "pre_v3")
        self.assertTrue(partial["capabilities"]["worker_turn_stop_reports"])

    def test_an_unread_or_unreadable_list_behaves_as_pre_v3_and_says_why(self):
        for advertised in (None, "orchestration.worker-keep-open.v1", {"x": 1}):
            found = orchestration_features(advertised)
            self.assertEqual((found["mode"], found["basis"], found["reason"]),
                             ("pre_v3", "unread", "capabilities_unreadable"))
        with patch("pod.orca.read_command", side_effect=PodError("orca_unavailable", "x")):
            self.assertEqual(orca.read_orchestration_features()["reason"], "orca_unavailable")
        with patch("pod.orca.read_command", return_value=status_envelope(V3_ADVERTISED)) as reader:
            self.assertEqual(orca.read_orchestration_features()["mode"], "v3")
        self.assertEqual(reader.call_args.args[0], ["status", "--json"])

    def test_the_contract_snapshot_carries_the_features_from_its_existing_read(self):
        version = {"version": "1.4.230", "executable": "/fixture/orca"}
        with patch("pod.orca.read_command", side_effect=[version, status_envelope(PRE_V3_ADVERTISED)]) as reader:
            snapshot = orca.contract()
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(snapshot["orchestration_features"], PRE_V3)
        self.assertEqual(orca.features_of(snapshot), PRE_V3)
        self.assertEqual(orca.features_of({"status": "unavailable", "reason": "orca_read_failed"})["reason"],
                         "orca_read_failed")
        self.assertEqual(orca.features_of({"status": "observed"})["basis"], "unread")


class Mail(FakeMailbox):
    """A V3 mailbox: a consuming check hands a batch over already acknowledged, and nothing replays."""

    def batch(self):
        if not self.pending:
            return self.empty()
        self.serial += 1
        messages, self.pending = list(self.pending), []
        return {"runtime": "rt", "delivery": f"delivery_{self.serial}", "messages": messages, "replayed": False,
                "delivery_acknowledged": True, "timed_out": False, "cancelled": False, "connection_lost": False}


class WakeDrainTests(unittest.TestCase):
    """pod#230: on V3 one plain consuming check, no wait, no acknowledgement, and the turn ends."""

    def setUp(self):
        self.mail = Mail()

    def arm(self, features=V3, run_fn=None, show_fn=None, **options):
        return wake.arm(RUN, check_fn=self.mail.check, run_fn=run_fn or bound(), features_fn=lambda: features,
                        show_fn=show_fn or (lambda run: self.fail("no retained-binding read expected")), **options)

    def test_v3_drains_once_with_no_wait_and_no_ack_and_ends_the_turn(self):
        self.mail.send()
        result = self.arm(ack="delivery_0", timeout_ms=5)
        self.assertEqual(self.mail.calls, [{"run": RUN, "ack": None, "wait_ms": None}])
        self.assertEqual((result["status"], result["waited"], result["delivery_final"]), ("events", False, True))
        self.assertEqual(result["ack"], {"requested": "delivery_0", "status": "already_final"})
        self.assertEqual(result["orchestration"], {"mode": "v3", "basis": "advertised"})
        self.assertIn("end your turn", result["next_action"])
        self.assertNotIn("--ack", result["next_action"])
        empty = self.arm()
        self.assertEqual((empty["status"], empty["waited"]), ("drained", False))
        self.assertIn("Hold no wait", empty["next_action"])
        self.assertTrue(all(call["wait_ms"] is None and call["ack"] is None for call in self.mail.calls))

    def test_v3_never_combines_ack_with_a_check_that_returns_new_mail(self):
        for _ in range(3):
            self.mail.send()
            result = self.arm(ack=self.mail.calls and "delivery_x" or None)
            self.assertEqual(result["status"], "events")
        self.assertEqual({(call["ack"], call["wait_ms"]) for call in self.mail.calls}, {(None, None)})

    def test_a_fenced_consumer_and_a_malformed_ack_on_v3(self):
        self.mail.refuse["check"] = "consumer_fenced"
        self.assertEqual(self.arm()["status"], "fenced")
        with self.assertRaises(PodError) as caught:
            self.arm(ack="--wait")
        self.assertEqual(caught.exception.code, "orca_contract")

    def test_a_retained_run_the_caller_coordinates_is_accepted_on_v3_and_refused_on_pre_v3(self):
        reads = []
        show = lambda run: reads.append(run) or {"run": run, "coordinator_handle": "term_c", "consumer_generation": 2}
        self.mail.send()
        retained = self.arm(run_fn=bound("run_newer"), show_fn=show)
        self.assertEqual((retained["status"], reads), ("events", [RUN]))
        for features in (PRE_V3, UNREAD):
            with self.subTest(features=features["basis"]), self.assertRaises(PodError) as caught:
                self.arm(features=features, run_fn=bound("run_newer"))
            self.assertEqual(caught.exception.code, "wake_run_not_bound")
        other = lambda run: {"run": run, "coordinator_handle": "term_other", "consumer_generation": 2}
        with self.assertRaises(PodError) as caught:
            self.arm(run_fn=bound("run_newer"), show_fn=other)
        self.assertEqual(caught.exception.code, "wake_run_not_bound")
        self.assertEqual(len(self.mail.calls), 1)

    def test_a_partial_capability_set_never_accepts_a_retained_run(self):
        show = lambda run: {"run": run, "coordinator_handle": "term_c", "consumer_generation": 2}
        for advertised in ([V3_CAPABILITY_KEYS["retained_run_bindings"]], V3_ADVERTISED[:-1]):
            partial = orchestration_features(advertised)
            self.assertEqual(partial["mode"], "pre_v3")
            self.assertTrue(partial["capabilities"]["retained_run_bindings"])
            for pending in (True, False):
                with self.subTest(advertised=len(advertised), pending=pending):
                    if pending:
                        self.mail.send()
                    with self.assertRaises(PodError) as caught:
                        self.arm(features=partial, run_fn=bound("run_newer"), show_fn=show, timeout_ms=5)
                    self.assertEqual(caught.exception.code, "wake_run_not_bound")
        self.assertEqual(self.mail.calls, [])


class PreV3ArgvTests(unittest.TestCase):
    """A pre-V3 runtime keeps every native argv and result byte for byte."""

    def native(self, features, script):
        """Run `pod wake` through the real adapter against scripted native answers; return argv and result."""
        answers = iter(script)
        def run(argv, **_):
            return subprocess.CompletedProcess(argv, 0, json.dumps(next(answers)), "")
        with patch("pod.orca.executable", return_value=Path("/fixture/orca")), \
             patch("pod.orca.subprocess.run", side_effect=run) as runner:
            result = wake.arm(RUN, ack="delivery_1", timeout_ms=2000, run_fn=bound(),
                              features_fn=lambda: features)
        return [call.args[0][1:] for call in runner.call_args_list], result

    def test_pre_v3_argv_and_result_are_unchanged_and_v3_sends_one_plain_check(self):
        meta = {"_meta": {"runtimeId": "rt"}}
        empty = {"ok": True, "result": {"messages": [], "count": 0}, **meta}
        timed = {"ok": True, "result": {"messages": [], "count": 0, "timedOut": True}, **meta}
        argv, result = self.native(PRE_V3, [empty, timed])
        self.assertEqual(argv, [["orchestration", "check", "--run", RUN, "--ack", "delivery_1", "--json"],
                                ["orchestration", "check", "--run", RUN, "--wait", "--timeout-ms", "2000", "--json"]])
        self.assertEqual(result, {"schema": "pod-wake/v1", "status": "timeout", "run": RUN, "waited": True,
                                  "delivery": None, "replayed": False, "events": [], "duplicates": [],
                                  "ack": {"requested": "delivery_1", "status": "applied"},
                                  "next_action": result["next_action"]})
        self.assertTrue(result["next_action"].startswith("The wait ended with no mail"))
        unread_argv, unread = self.native(UNREAD, [empty, timed])
        self.assertEqual(unread_argv, argv)
        self.assertEqual(unread["orchestration"], {"mode": "pre_v3", "basis": "unread", "reason": "orca_read_failed"})
        v3_argv, drained = self.native(V3, [empty])
        self.assertEqual(v3_argv, [["orchestration", "check", "--run", RUN, "--json"]])
        self.assertEqual(drained["status"], "drained")

    def test_pre_v3_worker_start_argv_is_unchanged_and_keep_open_adds_one_flag(self):
        route = {"agent": "codex", "model": "gpt-6-sol", "effort": "high", "context": "native_default",
                 "reason": "test"}
        base = ["orchestration", "worker-start", "--task", "t", "--run", "r", "--worktree", "current",
                "--agent", "codex", "--model", "gpt-6-sol", "--effort", "high"]
        receipt = {"runtime": "r", "exit": 0, "request_uuid": None, "result": {"state": "ready"}}
        with patch("pod.operations.mutate_command", return_value=receipt) as mutate:
            OrcaPort().start_worker(run="r", task="t", owner="o", route=route)
            self.assertEqual(mutate.call_args.args[0], base + ["--json"])
            OrcaPort().start_worker(run="r", task="t", owner="o", route=route, keep_open=True,
                                    retry_request="11111111-1111-4111-8111-111111111111")
            self.assertEqual(mutate.call_args.args[0], base + ["--keep-open", "--retry-request",
                                                               "11111111-1111-4111-8111-111111111111", "--json"])
        self.assertTrue(orca._mutate_allowed(base + ["--keep-open", "--json"]))
        self.assertTrue(orca._mutate_allowed(["orchestration", "worker-start", "--task", "t", "--run", "r",
                                              "--worktree", "current", "--terminal", "term", "--keep-open", "--json"]))
        for shape in (base[:8] + ["--keep-open"] + base[8:] + ["--json"], base + ["--keep-open", "--keep-open", "--json"],
                      ["orchestration", "worker-keep-open", "--dispatch", "d", "--json"],
                      ["orchestration", "send", "--to", "dispatch:d", "--subject", "s", "--body", "b", "--json"]):
            with self.subTest(shape=shape):
                self.assertFalse(orca._mutate_allowed(shape))
        with self.assertRaises(PodError) as caught:
            mutate_command(["orchestration", "send", "--to", "dispatch:d", "--json"])
        self.assertEqual(caught.exception.code, "unsupported_orca_mutation")


class DeliveryFinalTests(unittest.TestCase):
    """pod#231: `deliveryAcknowledged: true` is final; an unacknowledged pre-V3 batch still replays."""

    def run_check(self, result):
        payload = {"ok": True, "result": result, "_meta": {"runtimeId": "rt"}}
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("pod.orca.executable", return_value=Path("/fixture/orca")), \
             patch("pod.orca.subprocess.run", return_value=done):
            return mailbox_check(RUN)

    def test_the_adapter_reads_the_acknowledged_flag_only_for_a_handed_over_batch(self):
        message = {"id": "m1", "type": "worker_done"}
        self.assertTrue(self.run_check({"messages": [message], "deliveryId": "d1",
                                        "deliveryAcknowledged": True})["delivery_acknowledged"])
        self.assertFalse(self.run_check({"messages": [message], "deliveryId": "d1"})["delivery_acknowledged"])
        self.assertFalse(self.run_check({"messages": [], "deliveryAcknowledged": True})["delivery_acknowledged"])

    def test_an_acknowledged_delivery_is_final_and_never_acknowledged_again(self):
        mail = Mail()
        mail.send()
        # The capability read failed, so the call behaves as pre-V3, but the native batch says it is final.
        result = wake.arm(RUN, check_fn=mail.check, run_fn=bound(), features_fn=lambda: UNREAD)
        self.assertEqual((result["status"], result["delivery_final"]), ("events", True))
        self.assertNotIn("--ack", result["next_action"])
        self.assertIn("never acknowledge it again", result["next_action"])
        again = wake.arm(RUN, check_fn=mail.check, run_fn=bound(), features_fn=lambda: UNREAD, timeout_ms=5)
        self.assertEqual(again["status"], "timeout")
        self.assertEqual([call["ack"] for call in mail.calls], [None, None, None])

    def test_an_unacknowledged_pre_v3_batch_replays_as_today(self):
        mail = FakeMailbox()
        mail.send()
        arm = lambda **options: wake.arm(RUN, check_fn=mail.check, run_fn=bound(), features_fn=lambda: PRE_V3,
                                         **options)
        first, again = arm(), arm()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["delivery"], first["delivery"])
        self.assertNotIn("delivery_final", again)
        self.assertNotIn("orchestration", again)
        self.assertIn("--ack " + first["delivery"], again["next_action"])
        self.assertEqual(arm(ack=first["delivery"], timeout_ms=5)["ack"]["status"], "applied")


def escalation(payload, id_="msg_stop"):
    return {"id": id_, "type": "escalation", "from_handle": "term_w", "subject": "Worker stopped without reporting",
            "body": "The Dispatch is still open.", "payload": json.dumps(payload)}


class TurnStopTests(unittest.TestCase):
    """pod#232: a V3 turn-stop escalation is a stopped worker, never a question."""

    def test_a_turn_stop_escalation_is_a_stopped_worker_with_no_reply_suggested(self):
        event = wake.classify(escalation({"dispatchId": "d1", "taskId": "t1", "handle": "term_w",
                                          "stopReason": "finished"}), V3)
        self.assertEqual((event["kind"], event["stop_reason"], event["dispatch"]), ("worker_stopped", "finished", "d1"))
        self.assertIn("worker-show --dispatch d1", event["action"])
        self.assertIn("a reply does not settle it", event["action"])
        for suggestion in ("Reply through", "orchestration reply", "orchestration send"):
            self.assertNotIn(suggestion, event["action"])

    def test_an_ordinary_question_is_unchanged_and_pre_v3_keeps_the_question_kind(self):
        question = escalation({"dispatchId": "d1", "question": "Which base?"}, "msg_q")
        on_v3 = wake.classify(question, V3)
        self.assertEqual(on_v3, wake.classify(question))
        self.assertEqual(on_v3["kind"], "question")
        stopped = escalation({"dispatchId": "d1", "stopReason": "errored"})
        for features in (None, PRE_V3, UNREAD):
            with self.subTest(features=features and features["basis"]):
                event = wake.classify(stopped, features)
                self.assertEqual(event["kind"], "question")
                self.assertNotIn("stop_reason", event)
        partial = orchestration_features(PRE_V3_ADVERTISED + ["orchestration.worker-turn-stop-reports.v1"])
        self.assertEqual(wake.classify(stopped, partial)["kind"], "worker_stopped")

    def test_a_drained_turn_stop_is_reported_as_an_event_to_act_on(self):
        mail = Mail()
        mail.pending.append(escalation({"dispatchId": "d1", "stopReason": "exited"}))
        result = wake.arm(RUN, check_fn=mail.check, run_fn=bound(), features_fn=lambda: V3)
        self.assertEqual([event["kind"] for event in result["events"]], ["worker_stopped"])


class SettledDispatchTests(unittest.TestCase):
    """pod#233: on V3 Pod names keep-open for a finished worker and never routes a follow-up through send."""

    def test_a_v3_completion_names_keep_open_and_forbids_send_to_the_settled_dispatch(self):
        done = {"id": "m1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d1", "outcome": "succeeded"})}
        event = wake.classify(done, V3)
        self.assertIn("worker-keep-open --dispatch d1", event["action"])
        self.assertIn("Never send to this settled Dispatch", event["action"])
        self.assertEqual(wake.classify(done, PRE_V3), wake.classify(done))
        self.assertNotIn("keep-open", wake.classify(done, PRE_V3)["action"])


class TabKeepOpenTests(unittest.TestCase):
    """pod#233 at report time: keep-open is relayed only when retention is intended and Orca will auto-release."""

    STATE = {"owner": "term_c", "checkpoint": {"objective": "objective", "obligations": []}, "constraints": [],
             "admissions": {}}

    def facts(self, **release):
        return {"terminal_ownership": {"ownerDispatchId": "d1", "ownershipState": "owned",
                                       "releaseState": "unreleased", "terminalHandle": "term_w", "worktreeId": "w"},
                "native_release": {"state": "owned", "releaseState": "unreleased", "ownerDispatchId": "d1", **release},
                "terminal_readback": {"handle": "term_w", "worktreeId": "w", "connected": True},
                "native_next_action": {"argv": ["orchestration", "worker-release", "--dispatch", "d1"]}}

    def row(self, facts, keep=False):
        admission = {"admission_id": "a1", "objective": "objective", "role": "review",
                     "native_binding": {"terminalHandle": "term_w", "dispatchId": "d1", "worktreeId": "w"}}
        state = dict(self.STATE)
        if keep:
            state["constraints"] = [{"id": "C1", "kind": "keep_worker_tab", "provenance": "user_direct",
                                     "instruction": "keep it", "value": {"objective": "objective", "dispatch": "d1",
                                                                         "terminal": "term_w", "keep": True}}]
        with patch("pod.worker_tabs.worktree_command", return_value={}):
            return tab_project(state, admission, facts, settled=True)

    def test_keep_open_is_relayed_only_for_intended_retention_before_automatic_release(self):
        kept = self.row(self.facts(autoReleaseAt="2026-11-05T00:02:00Z"), keep=True)
        self.assertEqual(kept["keep_open_command"],
                         ["orca", "orchestration", "worker-keep-open", "--dispatch", "d1", "--json"])
        self.assertTrue(kept["wording"].startswith("Keep it open now"))
        plain = self.row(self.facts(autoReleaseAt="2026-11-05T00:02:00Z"))
        self.assertNotIn("keep_open_command", plain)
        earlier = self.row(self.facts(), keep=True)
        self.assertNotIn("keep_open_command", earlier)
        self.assertEqual(earlier["wording"], kept["wording"].split(". ", 1)[1])


class KeepOpenPort(retry_support.RetryPort):
    """A FakePort whose runtime advertises the given V3 features and records the keep-open start flag."""

    def __init__(self, features):
        super().__init__()
        self.features = features

    def capability(self):
        return {**super().capability(), "orchestration_features": self.features}

    def start_worker(self, *, keep_open=False, **kwargs):
        receipt = super().start_worker(**kwargs)
        self.starts[-1]["keep_open"] = keep_open
        return receipt


class AdmissionKeepOpenTests(unittest.TestCase):
    """pod#233 at dispatch time: keep-open when intended, early refusal of reuse after release."""

    setUp = retry_support.RetryAdmissionTests.setUp
    checkpoint = retry_support.RetryAdmissionTests.checkpoint
    frozen = retry_support.RetryAdmissionTests.frozen
    restated = retry_support.RetryAdmissionTests.restated
    discard = retry_support.RetryAdmissionTests.discard

    def use(self, features):
        placement = self.port.placement
        self.port = KeepOpenPort(features)
        self.port.placement = placement

    def admit(self, task="task", keep_open=None, reuse_of=None):
        from pod.operations import guarded_start
        return guarded_start(self.project, "objective", owner="owner", run="run", task=task,
                             plan_revision="plan", frozen_packet=self.frozen(task=task), worktree="current",
                             port=self.port, reuse_of=reuse_of, accompanying=self.restated(),
                             **({"keep_open": keep_open} if keep_open is not None else {}))

    def test_keep_open_is_requested_and_recorded_only_when_intended_on_v3(self):
        self.use(V3)
        kept = self.admit(keep_open=True)
        self.assertEqual((kept["keep_open"], kept["admission"]["keep_open"]), ("requested", "requested"))
        plain = self.admit(task="task2")
        self.assertNotIn("keep_open", plain["admission"])
        self.assertNotIn("keep_open", plain)
        self.assertEqual([start["keep_open"] for start in self.port.starts], [True, False])
        stored = read(self.project, "objective")["admissions"]
        self.assertEqual(stored[kept["admission"]["admission_id"]]["keep_open"], "requested")
        with self.assertRaises(PodError) as caught:
            self.admit(task="task3", keep_open="yes")
        self.assertEqual(caught.exception.code, "invalid_keep_open")

    def test_keep_open_on_pre_v3_adds_no_flag_and_records_that_orca_does_not_advertise_it(self):
        self.use(PRE_V3)
        result = self.admit(keep_open=True)
        self.assertEqual((result["keep_open"], result["admission"]["keep_open"]), ("not_advertised", "not_advertised"))
        self.assertEqual(self.port.starts[-1]["keep_open"], False)

    def settled_prior(self, resource):
        first = self.admit()["admission"]
        self.port.workers["dispatch-1"].update(outcome="succeeded", facts={"resource": resource})
        self.discard(first)
        return first

    def test_reuse_of_is_refused_early_after_release_and_accepted_while_kept_open(self):
        self.use(V3)
        prior = self.settled_prior({"state": "released", "releaseState": "released"})
        starts = list(self.port.starts)
        with self.assertRaises(PodError) as caught:
            self.admit(task="task2", reuse_of=prior["admission_id"])
        self.assertEqual(caught.exception.code, "reuse_released")
        self.assertIn("without reuse_of", str(caught.exception))
        self.assertEqual(self.port.starts, starts)
        self.port.workers["dispatch-1"]["facts"] = {"resource": {"state": "retained", "releaseState": "retained",
                                                                 "autoReleaseAt": None}}
        reused = self.admit(task="task2", reuse_of=prior["admission_id"])
        self.assertEqual(reused["status"], "bound")
        self.assertEqual(self.port.starts[-1]["terminal"], prior["native_binding"]["terminalHandle"])

    def pending(self, admission):
        """The start's native effect is unknown and Orca reports its request pending, so recovery replays it."""
        update_admission(self.project, "objective", owner="owner", admission_id=admission["admission_id"],
                         update=lambda row: row.update(state="unresolved", native_binding=None))
        self.port.state = "pending"
        return lambda: recover_admission(self.project, "objective", owner="owner",
                                         admission_id=admission["admission_id"], worktree="current", port=self.port)

    def test_pending_reuse_replay_is_refused_once_the_prior_worker_is_released(self):
        self.use(V3)
        prior = self.settled_prior({"state": "owned", "releaseState": "unreleased",
                                    "autoReleaseAt": "2026-11-05T00:02:00Z"})
        child = self.admit(task="task2", reuse_of=prior["admission_id"])["admission"]
        recover = self.pending(child)
        self.port.workers["dispatch-1"]["facts"] = {"resource": {"state": "released", "releaseState": "released"}}
        before, starts = read(self.project, "objective"), list(self.port.starts)
        with self.assertRaises(PodError) as caught:
            recover()
        self.assertEqual(caught.exception.code, "reuse_released")
        self.assertEqual(caught.exception.detail["request_uuid"], before["admissions"][child["admission_id"]]["request_uuid"])
        self.assertIn("request-show", caught.exception.detail["next_action"])
        self.assertEqual((self.port.starts, read(self.project, "objective")), (starts, before))
        # Still kept open: the same pending request replays onto its terminal.
        self.port.workers["dispatch-1"]["facts"] = {"resource": {"state": "retained", "releaseState": "retained"}}
        self.assertEqual(recover()["status"], "bound")
        self.assertEqual(self.port.starts[-1]["terminal"], prior["native_binding"]["terminalHandle"])

    def test_keep_open_replay_follows_the_current_capability_snapshot(self):
        self.use(V3)
        admission = self.admit(keep_open=True)["admission"]
        recover = self.pending(admission)
        self.port.features = PRE_V3
        result = recover()
        self.assertEqual(result["status"], "bound")
        self.assertEqual((self.port.starts[-1]["keep_open"], self.port.starts[-1]["retry_request"] is not None),
                         (False, True))
        self.assertEqual(read(self.project, "objective")["admissions"][admission["admission_id"]]["keep_open"],
                         "not_advertised")
        # With keep-open still advertised the replay keeps the flag.
        self.port.features = V3
        again = self.admit(task="task2", keep_open=True)["admission"]
        self.pending(again)()
        self.assertTrue(self.port.starts[-1]["keep_open"])

    def test_a_released_prior_on_pre_v3_keeps_todays_reuse_path(self):
        self.use(PRE_V3)
        prior = self.settled_prior({"state": "released", "releaseState": "released"})
        reused = self.admit(task="task2", reuse_of=prior["admission_id"])
        self.assertEqual(reused["status"], "bound")


class DocumentationTests(unittest.TestCase):
    """The installed guidance states each runtime's path (pod#230, pod#231, pod#233)."""

    ROOT = Path(__file__).resolve().parents[1] / "skills" / "pod" / "references"

    def test_the_wake_and_worker_tab_guidance_state_both_runtimes(self):
        wake_text = (self.ROOT / "wake.md").read_text()
        for name in V3_CAPABILITY_KEYS.values():
            self.assertIn(name.removeprefix("orchestration."), wake_text)
        for phrase in ("## On Orchestration V3", "Drain, act, end the turn", "no `--wait` and no `--ack`",
                       "`delivery_final: true`", "orchestration check --run RUN --all --json",
                       "orchestration inbox --terminal run:RUN", "`worker_stopped`", "Never send to a settled Dispatch",
                       # The pre-V3 replay promise stays, scoped to a pre-V3 runtime.
                       "On a pre-V3 runtime an unacknowledged batch replays with the same `delivery`"):
            self.assertIn(phrase, wake_text)
        tabs = (self.ROOT / "worker-tabs.md").read_text()
        for phrase in ('"keep_open": true', "keep_open_command", "reuse_released", "Never send to a settled Dispatch"):
            self.assertIn(phrase, tabs)


if __name__ == "__main__":
    unittest.main()

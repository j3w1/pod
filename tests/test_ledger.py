import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, reserve, reconcile, record_delivery, reconcile_delivery_item, read, check_bound_sources, intervention, _quota_hold
from pod.records import source_identity
from pod.config import effective
from tests.common import fixture


def checkpoint_body():
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
            "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"}


def bind_confirmed_task(project, body=None):
    checkpoint(project, "objective", owner="terminal", value=body or checkpoint_body(), native={"runtime": "r"})
    route = {"agent": "codex", "model": "m", "account": "a", "bucket": None, "effort": "high"}
    reserve(project, "objective", owner="terminal", operation_id="task-launch", requested=route,
            route_decision={"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]},
            capability_contract={"runtime": "r", "billing_preflight": True, "fanout_control": True,
                                 "account_binding": {"provider": "codex", "account": "a", "bucket": None}},
            native_reader=lambda: {"runtime": "r", "authoritative": True, "owner": "terminal",
                                   "scope": "all", "complete": True, "workers": [], "cross_host": False},
            capacity=1, run_id="run", plan_revision="plan")
    launch = {key: route[key] for key in ("agent", "model", "effort")}
    reconcile(project, "objective", owner="terminal", operation_id="task-launch",
              observed={"runtime": "r", "operation_id": "task-launch",
                        "launch": {"requested": launch, "effective": launch}, "state": "ready",
                        "runId": "run", "taskId": "task", "dispatchId": "dispatch",
                        "worker_show": {"dispatch": {"id": "dispatch"},
                                        "projection": {"id": "worker", "dispatchId": "dispatch",
                                                       "runId": "run", "taskId": "task"},
                                        "worker": {"startOptions": {"launch": {"requested": launch,
                                                                               "effective": launch}}}}})


class LedgerTests(unittest.TestCase):
    def test_exhaustion_hold_is_monotonic_by_window_after_restart(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state") } ):
            at = datetime(2026, 9, 20, 0, 20, 5, tzinfo=timezone.utc)
            def snapshot(seconds, hour, week):
                return {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                        "bucket": "shared", "observed_at": (at + timedelta(seconds=seconds)).isoformat(),
                        "source": "supported", "confidence": "observed", "unknowns": [],
                        "windows": [{"name": "hour", "remaining_percent": hour},
                                    {"name": "week", "remaining_percent": week}]}
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-5, 0, 40),
                                        "exhausted", now=at, freshness=60))
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-15, 0, 40),
                                        "exhausted", now=at, freshness=60))
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-10, 50, 40),
                                        "normal", now=at, freshness=60))
            script = ("from datetime import datetime,timezone; from pod.ledger import _quota_hold; "
                      "print(_quota_hold('codex','a','shared',None,'unknown', "
                      "now=datetime(2026,9,20,0,20,5,tzinfo=timezone.utc),freshness=60))")
            resumed = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                     text=True, check=True, env=os.environ.copy())
            self.assertEqual(resumed.stdout.strip(), "True")
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(0, 50, 0),
                                        "exhausted", now=at, freshness=60))
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-2, 50, 40),
                                        "normal", now=at, freshness=60))
            self.assertFalse(_quota_hold("codex", "a", "shared", snapshot(2, 50, 40),
                                         "normal", now=at + timedelta(seconds=2), freshness=60))
            self.assertFalse(_quota_hold("codex", "b", "independent", None,
                                         "unknown", now=at, freshness=60))

    def test_exhausted_bucket_hold_needs_later_supported_positive_read(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                         "LOCALAPPDATA": str(root / "state"),
                                                         "XDG_CONFIG_HOME": str(root / "config"),
                                                         "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"agent": "codex", "model": "m", "account": "a", "bucket": "shared", "effort": "high"}
            decision = {"status": "usable", "selected": route, "policy_revision": effective(project)["revision"]}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True,
                   "account_binding": {"provider": "codex", "account": "a", "bucket": "shared"}}
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False,
                      "quota": {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                                "bucket": "shared", "observed_at": now.isoformat(), "source": "supported",
                                "confidence": "observed", "unknowns": [],
                                "windows": [{"name": "hour", "remaining_percent": 0}], "remaining_percent": 0}}
            def call(operation, at):
                return reserve(project, "objective", owner="terminal", operation_id=operation,
                               requested=route, route_decision=decision, capability_contract=cap,
                               native_reader=lambda: native, capacity=2, run_id="run",
                               plan_revision="plan", now=at)
            with self.assertRaises(PodError) as exhausted:
                call("one", now)
            self.assertEqual(exhausted.exception.code, "quota_exhausted")
            native["quota"] = None
            with self.assertRaises(PodError) as missing:
                call("two", now + timedelta(seconds=5))
            self.assertEqual(missing.exception.code, "quota_exhausted")
            native["quota"] = {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                               "bucket": "shared", "observed_at": (now - timedelta(seconds=10)).isoformat(),
                               "source": "supported", "confidence": "observed", "unknowns": [],
                               "windows": [{"name": "hour", "remaining_percent": 0}]}
            with self.assertRaises(PodError):
                call("older-zero", now)
            native["quota"] = {**native["quota"],
                               "observed_at": (now - timedelta(seconds=5)).isoformat(),
                               "windows": [{"name": "hour", "remaining_percent": 30}]}
            with self.assertRaises(PodError) as middle:
                call("middle-positive", now)
            self.assertEqual(middle.exception.code, "quota_exhausted")
            native["quota"] = {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                               "bucket": "shared", "observed_at": (now + timedelta(seconds=10)).isoformat(),
                               "source": "supported", "confidence": "observed", "unknowns": [],
                               "windows": [{"name": "hour", "remaining_percent": 30}], "remaining_percent": 30}
            self.assertFalse(call("three", now + timedelta(seconds=10))["existing"])
    def test_two_equivalent_corrections_require_productive_diagnosis(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                         "LOCALAPPDATA": str(root / "state"),
                                                         "XDG_CONFIG_HOME": str(root / "config"),
                                                         "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            bind_confirmed_task(project)
            correction = {"criterion_id": "works", "failure_id": "parser-f1",
                          "obligation": "Fix failing parser", "failing_example": "input x",
                          "hypothesis": "wrong branch", "last_meaningful_evidence": "trace-1",
                          "next_discriminating_check": "probe y", "correction_key": "first"}
            self.assertFalse(intervention(project, "objective", owner="terminal", task="task",
                                          correction=correction)["dispatch_authorized"])
            self.assertFalse(intervention(project, "objective", owner="terminal", task="task",
                                          correction={**correction, "correction_key": "second"})["dispatch_authorized"])
            with self.assertRaises(PodError) as caught:
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**correction, "obligation": "Repair failing parser",
                                         "correction_key": "third"})
            self.assertEqual(caught.exception.code, "diagnosis_required")
            with self.assertRaises(PodError):
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**correction, "correction_key": "third"},
                             diagnosis={"diagnosis_evidence": "trace-1"})
            (project / "new-probe").write_text("new discriminating fixture")
            intervention(project, "objective", owner="terminal", task="task",
                         correction={**correction, "obligation": "Repair failing parser",
                                     "correction_key": "third"},
                         diagnosis={"diagnosis_evidence": "new-probe"})
            self.assertEqual(len(read(project, "objective")["interventions"]["task"]), 3)
            with self.assertRaises(PodError) as renamed:
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**correction, "failure_id": "new failure label",
                                         "correction_key": "fourth"})
            self.assertEqual(renamed.exception.code, "diagnosis_required")

    def test_correction_labels_do_not_reset_task_history_or_reuse_evidence(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                         "XDG_CONFIG_HOME": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            bound = {**checkpoint_body(), "criteria": ["works", "other accepted criterion"]}
            bind_confirmed_task(project, bound)
            base = {"criterion_id": "works", "failure_id": "first label", "obligation": "fix parse",
                    "failing_example": "x", "hypothesis": "branch", "last_meaningful_evidence": "trace",
                    "next_discriminating_check": "probe", "correction_key": "one"}
            intervention(project, "objective", owner="terminal", task="task", correction=base)
            intervention(project, "objective", owner="terminal", task="task",
                         correction={**base, "correction_key": "two"})
            for changed in ({"failure_id": "renamed", "obligation": "repair parser"},
                            {"criterion_id": "other accepted criterion", "failure_id": "new label"},
                            {"last_meaningful_evidence": "claimed new evidence"}):
                with self.assertRaises(PodError) as denied:
                    intervention(project, "objective", owner="terminal", task="task",
                                 correction={**base, **changed, "correction_key": "three"})
                self.assertEqual(denied.exception.code, "diagnosis_required")
            restarted = ("from pathlib import Path; from pod.ledger import intervention; "
                         "from pod.errors import PodError; import json,sys; "
                         "\ntry: intervention(Path(sys.argv[1]), 'objective', owner='terminal', task='task', "
                         "correction=json.loads(sys.argv[2]))\n"
                         "except PodError as error: print(error.code)")
            result = subprocess.run([sys.executable, "-c", restarted, str(project),
                                     json.dumps({**base, "failure_id": "after restart", "correction_key": "three"})],
                                    capture_output=True, text=True, check=True, env=os.environ.copy())
            self.assertEqual(result.stdout.strip(), "diagnosis_required")
            with self.assertRaises(PodError) as fake_task:
                intervention(project, "objective", owner="terminal", task="renamed task",
                             correction={**base, "correction_key": "three"})
            self.assertEqual(fake_task.exception.code, "task_unbound")
            (project / "proof.txt").write_text("distinct observed bytes")
            third = {**base, "failure_id": "renamed", "correction_key": "three"}
            intervention(project, "objective", owner="terminal", task="task", correction=third,
                         diagnosis={"diagnosis_evidence": "proof.txt"})
            (project / "same-proof.txt").write_text("distinct observed bytes")
            with self.assertRaises(PodError) as reused:
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**base, "correction_key": "four"},
                             diagnosis={"diagnosis_evidence": "same-proof.txt"})
            self.assertEqual(reused.exception.code, "diagnosis_replay")
            self.assertEqual(len(read(project, "objective")["interventions"]["task"]), 3)

    def test_unknown_bucket_pending_across_objectives_and_known_independence(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                         "XDG_CONFIG_HOME": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            for objective in ("first", "second", "third"):
                checkpoint(project, objective, owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False}
            def attempt(objective, account, bucket):
                route = {"agent": "codex", "model": "m", "account": account, "bucket": bucket,
                         "effort": "high"}
                return reserve(project, objective, owner="terminal", operation_id=objective,
                               requested=route, route_decision={"status": "usable", "selected": route,
                                                               "policy_revision": effective(project)["revision"]},
                               capability_contract={"runtime": "r", "billing_preflight": True,
                                                    "fanout_control": True,
                                                    "account_binding": {"provider": "codex", "account": account,
                                                                        "bucket": bucket}},
                               native_reader=lambda: native, capacity=2, run_id="run", plan_revision="plan")
            attempt("first", "a", None)
            with self.assertRaises(PodError) as blocked:
                attempt("second", "b", None)
            self.assertEqual(blocked.exception.code, "unknown_quota_capacity")
            with self.assertRaises(PodError) as also_blocked:
                attempt("third", "b", "independent")
            self.assertEqual(also_blocked.exception.code, "bucket_occupancy_unverified")
            native["quota"] = {"schema": "pod-quota/v1", "provider": "codex", "account": "b",
                               "bucket": "independent", "observed_at": datetime.now(timezone.utc).isoformat(),
                               "source": "supported", "confidence": "observed", "unknowns": [],
                               "windows": [{"name": "hour", "remaining_percent": 50}]}
            with self.assertRaises(PodError) as known_but_overlapping:
                attempt("third", "b", "independent")
            self.assertEqual(known_but_overlapping.exception.code, "bucket_occupancy_unverified")

        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                         "XDG_CONFIG_HOME": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            for objective in ("first", "second"):
                checkpoint(project, objective, owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route_a = {"agent": "codex", "model": "m", "account": "a", "bucket": "bucket-a", "effort": "high"}
            route_b = {"agent": "codex", "model": "m", "account": "b", "bucket": "bucket-b", "effort": "high"}
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False}
            for objective, route in (("first", route_a), ("second", route_b)):
                result = reserve(project, objective, owner="terminal", operation_id=objective,
                                 requested=route, route_decision={"status": "usable", "selected": route,
                                                                 "policy_revision": effective(project)["revision"]},
                                 capability_contract={"runtime": "r", "billing_preflight": True,
                                                      "fanout_control": True,
                                                      "account_binding": {"provider": "codex", "account": route["account"],
                                                                          "bucket": route["bucket"]}},
                                 native_reader=lambda: native, capacity=2, run_id="run", plan_revision="plan")
                self.assertFalse(result["existing"])
            for objective in ("third", "fourth"):
                checkpoint(project, objective, owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            native["workers"] = [{"state": "active", "account": "a", "objective": "external",
                                  "bucket": "bucket-a", "agent": "codex"}]
            def active_attempt(objective):
                route = {"agent": "codex", "model": "m", "account": "c",
                         "bucket": "bucket-c", "effort": "high"}
                return reserve(project, objective, owner="terminal", operation_id=objective,
                               requested=route, route_decision={"status": "usable", "selected": route,
                                                               "policy_revision": effective(project)["revision"]},
                               capability_contract={"runtime": "r", "billing_preflight": True,
                                                    "fanout_control": True,
                                                    "account_binding": {"provider": "codex", "account": "c",
                                                                        "bucket": "bucket-c"}},
                               native_reader=lambda: native, capacity=2, run_id="run", plan_revision="plan")
            self.assertFalse(active_attempt("third")["existing"])
            native["workers"][0]["bucket"] = None
            with self.assertRaises(PodError) as unknown_active:
                active_attempt("fourth")
            self.assertEqual(unknown_active.exception.code, "bucket_occupancy_unverified")
    def test_definitive_source_rejection_survives_restored_bytes(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            source = project / "a.txt"
            source.write_text("original")
            bound = [source_identity(project, "a.txt")]
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            self.assertEqual(check_bound_sources(project, "objective", owner="terminal",
                                                 assignment="assignment", sources=bound)["status"], "current")
            source.write_text("changed")
            with self.assertRaises(PodError) as first:
                check_bound_sources(project, "objective", owner="terminal",
                                    assignment="assignment", sources=bound)
            self.assertEqual(first.exception.code, "source_changed")
            source.write_text("original")
            with self.assertRaises(PodError) as second:
                check_bound_sources(project, "objective", owner="terminal",
                                    assignment="assignment", sources=bound)
            self.assertEqual(second.exception.code, "source_rejected")
    def test_concurrent_objectives_share_unknown_account_allowance(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            for objective in ("first", "second"):
                checkpoint(project, objective, owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"agent": "codex", "model": "m", "account": "a", "effort": "high"}
            decision = {"status": "usable", "selected": route, "policy_revision": effective(project)["revision"], "quota_state": "unknown"}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True,
                   "account_binding": {"provider": "codex", "account": "a", "bucket": None}}
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False}
            def attempt(objective):
                try:
                    reserve(project, objective, owner="terminal", operation_id=objective,
                            requested=route, route_decision=decision, capability_contract=cap,
                            native_reader=lambda: native, capacity=2, run_id="run",
                            plan_revision="plan")
                    return "admitted"
                except PodError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(attempt, ("first", "second")))
            self.assertEqual(sorted(outcomes), ["admitted", "unknown_quota_capacity"])

    def test_uncertain_effect_retains_capacity_and_exact_reconciliation(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"status": "usable", "selected": {"agent": "codex", "model": "m", "account": "a", "effort": "high"},
                     "policy_revision": effective(project)["revision"]}
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True,
                   "account_binding": {"provider": "codex", "account": "a", "bucket": None}}
            reserve(project, "objective", owner="terminal", operation_id="op", requested=route["selected"],
                    route_decision=route, capability_contract=cap, native_reader=lambda: native,
                    capacity=1, run_id="run", plan_revision="plan")
            with self.assertRaises(PodError):
                reserve(project, "objective", owner="terminal", operation_id="op2", requested=route["selected"],
                        route_decision=route, capability_contract=cap, native_reader=lambda: native,
                        capacity=1, run_id="run", plan_revision="plan")
            self.assertEqual(reconcile(project, "objective", owner="terminal", operation_id="op", observed=None)["state"], "uncertain")
            with self.assertRaises(PodError):
                reconcile(project, "objective", owner="terminal", operation_id="op", observed=None, definitive_absence=True)
            launch = {key: route["selected"][key] for key in ("agent", "model", "effort")}
            observed = {"runtime": "r", "operation_id": "op", "launch": {"requested": launch, "effective": launch},
                        "state": "ready", "runId": "run", "taskId": "task", "dispatchId": "d",
                        "worker_show": {"dispatch": {"id": "d"}, "projection": {
                            "id": "w", "dispatchId": "d", "runId": "run", "taskId": "task"},
                            "worker": {"startOptions": {"launch": {"requested": launch, "effective": launch}}}}}
            self.assertEqual(reconcile(project, "objective", owner="terminal", operation_id="op", observed=observed)["state"], "confirmed")
            self.assertIsNone(read(root / "unknown", "other"))

    def test_delivery_requires_every_item_and_replays(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            delivery = {"id": "delivery", "runtime": "r", "runId": "run", "messages": [
                {"id": "a", "type": "question", "runId": "run"},
                {"id": "b", "type": "heartbeat", "runId": "run"}]}
            self.assertFalse(record_delivery(project, "objective", owner="terminal", delivery=delivery)["ack_eligible"])
            first = {"runtime": "r", "runId": "run", "messageId": "a", "kind": "reply",
                     "status": "replied", "receiptId": "reply-a"}
            reconcile_delivery_item(project, "objective", owner="terminal", delivery_id="delivery",
                                    message_id="a", native_reader=lambda: first)
            self.assertEqual(record_delivery(project, "objective", owner="terminal", delivery=delivery)["unresolved"], ["b"])
            second = {"runtime": "r", "runId": "run", "messageId": "b", "kind": "observation",
                      "status": "recorded", "receiptId": "observation-b"}
            with self.assertRaises(PodError):
                reconcile_delivery_item(project, "objective", owner="terminal", delivery_id="delivery",
                                        message_id="b", native_reader=lambda: {**second, "runId": "wrong"})
            reconcile_delivery_item(project, "objective", owner="terminal", delivery_id="delivery",
                                    message_id="b", native_reader=lambda: second)
            self.assertTrue(record_delivery(project, "objective", owner="terminal", delivery=delivery)["ack_eligible"])
            self.assertEqual(read(project, "objective")["deliveries"]["delivery"]["items"]["a"]["effect"], first)

    def test_delivery_survives_process_exit_between_items(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            delivery = {"id": "crash-delivery", "runtime": "r", "runId": "run", "messages": [
                {"id": "question", "type": "question", "runId": "run"},
                {"id": "heartbeat", "type": "heartbeat", "runId": "run"}]}
            script = ("import json,sys; from pathlib import Path; from pod.ledger import record_delivery; "
                      "print(json.dumps(record_delivery(Path(sys.argv[1]), 'objective', owner='terminal', "
                      "delivery=json.loads(sys.argv[2]))))")
            def after_restart():
                completed = subprocess.run([sys.executable, "-c", script, str(project), json.dumps(delivery)],
                                           capture_output=True, text=True, check=True, env=os.environ.copy())
                return json.loads(completed.stdout)
            self.assertEqual(after_restart()["unresolved"], ["question", "heartbeat"])
            reply = {"runtime": "r", "runId": "run", "messageId": "question", "kind": "reply",
                     "status": "replied", "receiptId": "reply"}
            reconcile_delivery_item(project, "objective", owner="terminal", delivery_id="crash-delivery",
                                    message_id="question", native_reader=lambda: reply)
            self.assertEqual(after_restart()["unresolved"], ["heartbeat"])
            observation = {"runtime": "r", "runId": "run", "messageId": "heartbeat", "kind": "observation",
                           "status": "recorded", "receiptId": "observation"}
            reconcile_delivery_item(project, "objective", owner="terminal", delivery_id="crash-delivery",
                                    message_id="heartbeat", native_reader=lambda: observation)
            self.assertTrue(after_restart()["ack_eligible"])

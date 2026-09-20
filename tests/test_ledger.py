import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, reserve, reconcile, record_delivery, reconcile_delivery_item, read, check_bound_sources, intervention
from pod.records import source_identity
from pod.config import effective
from tests.common import fixture


def checkpoint_body():
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
            "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"}


class LedgerTests(unittest.TestCase):
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
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            correction = {"criterion_id": "parse-c1", "failure_id": "parser-f1",
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
            intervention(project, "objective", owner="terminal", task="task",
                         correction={**correction, "obligation": "Repair failing parser",
                                     "correction_key": "third"},
                         diagnosis={"diagnosis_evidence": "new-probe"})
            self.assertEqual(len(read(project, "objective")["interventions"]["task"]), 3)
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

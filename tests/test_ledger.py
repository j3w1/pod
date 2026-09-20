import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, reserve, reconcile, record_delivery, read, check_bound_sources, intervention
from pod.records import source_identity
from tests.common import fixture


def checkpoint_body():
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
            "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"}


class LedgerTests(unittest.TestCase):
    def test_exhausted_bucket_hold_needs_later_supported_positive_read(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"agent": "codex", "model": "m", "account": "a", "effort": "high"}
            decision = {"status": "usable", "selected": route, "policy_revision": "p"}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True}
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False,
                      "quota": {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                                "bucket": "shared", "observed_at": now.isoformat(), "source": "supported",
                                "confidence": "observed", "remaining_percent": 0}}
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
                               "source": "supported", "confidence": "observed", "remaining_percent": 30}
            self.assertFalse(call("three", now + timedelta(seconds=10))["existing"])
    def test_two_equivalent_corrections_require_productive_diagnosis(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            correction = {"obligation": "Fix failing parser", "failing_example": "input x",
                          "hypothesis": "wrong branch", "last_meaningful_evidence": "trace-1",
                          "next_discriminating_check": "probe y", "correction_key": "first"}
            self.assertFalse(intervention(project, "objective", owner="terminal", task="task",
                                          correction=correction)["dispatch_authorized"])
            self.assertFalse(intervention(project, "objective", owner="terminal", task="task",
                                          correction={**correction, "correction_key": "second"})["dispatch_authorized"])
            with self.assertRaises(PodError) as caught:
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**correction, "correction_key": "third"})
            self.assertEqual(caught.exception.code, "diagnosis_required")
            with self.assertRaises(PodError):
                intervention(project, "objective", owner="terminal", task="task",
                             correction={**correction, "correction_key": "third"},
                             diagnosis={"diagnosis_evidence": "trace-1"})
            intervention(project, "objective", owner="terminal", task="task",
                         correction={**correction, "correction_key": "third"},
                         diagnosis={"diagnosis_evidence": "new-probe"})
    def test_definitive_source_rejection_survives_restored_bytes(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
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
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            project = root / "project"
            project.mkdir()
            for objective in ("first", "second"):
                checkpoint(project, objective, owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"agent": "codex", "model": "m", "account": "a", "effort": "high"}
            decision = {"status": "usable", "selected": route, "policy_revision": "p", "quota_state": "unknown"}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True}
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
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            route = {"status": "usable", "selected": {"agent": "codex", "model": "m", "account": "a", "effort": "high"},
                     "policy_revision": "p"}
            native = {"runtime": "r", "authoritative": True, "owner": "terminal", "scope": "all",
                      "complete": True, "workers": [], "atomic_admission": False, "cross_host": False}
            cap = {"runtime": "r", "billing_preflight": True, "fanout_control": True}
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
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="terminal", value=checkpoint_body(), native={"runtime": "r"})
            delivery = {"id": "delivery", "runtime": "r", "messages": [{"id": "a"}, {"id": "b"}]}
            with self.assertRaises(PodError):
                record_delivery(project, "objective", owner="terminal", delivery=delivery, reconciled_message_ids={"a"})
            self.assertTrue(record_delivery(project, "objective", owner="terminal", delivery=delivery, reconciled_message_ids={"a", "b"})["ack_eligible"])
            self.assertTrue(record_delivery(project, "objective", owner="terminal", delivery=delivery, reconciled_message_ids={"a", "b"})["ack_eligible"])

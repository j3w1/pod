from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, read
from pod.operations import guarded_start, release_once, reconcile_release
from pod.config import route_identity
from tests.common import fixture


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class FixturePort:
    def __init__(self):
        self.starts = 0
        self.reads = 0
        self.assured = True
        self.workers = []
        self.receipt_override = {}
        self.settled = False
        self.release_status = "retained"
        self.release_calls = 0
        self.fail_release = False

    def assurance(self, route):
        return {"runtime": "runtime", "billing_preflight": self.assured, "fanout_control": self.assured}

    def read_native(self, owner):
        self.reads += 1
        return {"runtime": "runtime", "authoritative": True, "owner": owner, "scope": "all",
                "complete": True, "workers": self.workers, "cross_host": False, "atomic_admission": False}

    def start_worker(self, *, run, task, owner, route):
        self.starts += 1
        launch = {key: route[key] for key in ("agent", "model", "effort")}
        return {"runtime": "runtime", "runId": run, "taskId": task, "dispatchId": "dispatch",
                "state": "ready", "launch": {"requested": launch, "effective": launch},
                **self.receipt_override}

    def show_worker(self, dispatch):
        launch = {"agent": "codex", "model": "gpt-5.6-sol", "effort": "high"}
        return {"runtime": "runtime", "result": {"dispatch": {
                    "id": dispatch, "runId": "run", "taskId": "task",
                    "status": "completed" if self.settled else "active"},
                "projection": {"id": "worker", "runId": "run", "taskId": "task", "dispatchId": dispatch},
                "worker": {"dispatchId": dispatch, "startOptions": {
                    "launch": {"requested": launch, "effective": launch}}}, "terminal": None,
                "terminalResource": {"releaseState": "released"} if self.release_status == "released" and self.release_calls else None}}

    def release_worker(self, dispatch):
        self.release_calls += 1
        if self.fail_release:
            raise PodError("native_release_uncertain", "lost response")
        return {"runtime": "runtime", "status": self.release_status}


def inputs(root):
    project = root / "project"
    project.mkdir()
    config = root / "config" / "pod"
    config.mkdir(parents=True)
    binding = route_identity({"agent": "codex", "model": "gpt-5.6-sol", "account": "account"})
    (config / "config.yaml").write_text(f"""schema: pod/v1
models:
  sol:
    agent: codex
    model: gpt-5.6-sol
    account: account
    approved: true
    approval_ref: review-1
    approval_route: {binding}
    billing: included
    efforts: [high]
""")
    checkpoint(project, "objective", owner="owner", value={
        "schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
        "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
        "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"},
        native={"runtime": "runtime"})
    assessment = {"method": "delegate", "responsibility": "bounded edit", "complexity": "complex",
                  "risk": "low", "size": "small", "uncertainty": "low", "verifiability": "unit",
                  "capabilities": [], "context": [], "reason": "independent", "bounded": True}
    caps = {"sol": {"agent": "codex", "model": "gpt-5.6-sol", "account": "account",
                     "efforts": ["high"], "capabilities": [], "billing_preflight": True,
                     "fanout_control": True}}
    quota = {"account": {"schema": "pod-quota/v1", "provider": "codex", "account": "account",
                         "bucket": "shared", "observed_at": NOW.isoformat(), "source": "supported",
                         "confidence": "observed", "remaining_percent": 60}}
    return project, assessment, caps, quota


class GuardedOperationTests(unittest.TestCase):
    def test_settled_terminal_less_release_retained_once(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            with self.assertRaises(PodError):
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(port.release_calls, 0)
            port.settled = True
            self.assertEqual(release_once(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)["status"], "retained")
            with self.assertRaises(PodError):
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(port.release_calls, 1)

    def test_uncertain_release_is_not_repeated(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            port.settled = True
            port.fail_release = True
            with self.assertRaises(PodError):
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "uncertain")
            with self.assertRaises(PodError):
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(port.release_calls, 1)
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "uncertain")
            port.release_status = "released"
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")
            self.assertEqual(port.release_calls, 1)

    def test_complete_guarded_path_without_terminal_and_no_repeat(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            kw = dict(owner="owner", run="run", task="task", operation_id="op",
                      assessment=assessment, capabilities=caps, quotas=quota,
                      occupancy={}, plan_revision="plan", port=port, now=NOW)
            result = guarded_start(project, "objective", **kw)
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(result["effect"]["native_binding"]["workerId"], "worker")
            self.assertEqual(port.starts, 1)
            self.assertEqual(port.reads, 1)
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **kw)
            self.assertEqual(port.starts, 1)

    def test_prelaunch_denial_and_unknown_effect_do_not_retry(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.assured = False
            kw = dict(owner="owner", run="run", task="task", operation_id="op",
                      assessment=assessment, capabilities=caps, quotas=quota,
                      occupancy={}, plan_revision="plan", port=port, now=NOW)
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **kw)
            self.assertEqual(port.starts, 0)
            port.assured = True
            port.receipt_override = {"runId": "wrong"}
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **kw)
            self.assertEqual(read(project, "objective")["effects"]["op"]["state"], "uncertain")
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **kw)
            self.assertEqual(port.starts, 1)

    def test_fresh_native_read_and_shared_account_occupancy(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.workers = [{"state": "occupied", "account": "account", "objective": "other"}]
            quota = {}
            with self.assertRaises(PodError):
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            self.assertEqual(port.starts, 0)
            self.assertGreaterEqual(port.reads, 1)

    def test_exceptional_grant_exact_scope_and_request_identity(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            base = dict(owner="owner", run="run", task="task", operation_id="op",
                        assessment=assessment, capabilities=caps, quotas=quota,
                        occupancy={}, plan_revision="plan", port=port, now=NOW, capacity=4)
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **base,
                              exceptional_grant={"objective": "wrong", "run": "run",
                                                 "plan_revision": "plan", "limit": 4, "reason": "independent"})
            self.assertEqual(port.starts, 0)
            self.assertEqual(guarded_start(project, "objective", **base,
                             exceptional_grant={"objective": "objective", "run": "run",
                                                "plan_revision": "plan", "limit": 4,
                                                "reason": "independent"})["status"], "confirmed")

from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import effective, route_identity
from pod.errors import PodError
from pod.internal import run as helper_run
from pod.ledger import checkpoint, read, update_admission
from pod.operations import OrcaPort, guarded_start, recover_admission
from pod.records import packet
from tests.common import fixture


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
REQUEST_UUID = "11111111-1111-4111-8111-111111111111"


class FakePort:
    def __init__(self):
        self.runtime = "runtime"
        self.starts = []
        self.retry_requests = []
        self.workers = {}
        self.request_state = "completed"
        self.request_method = "orchestration.workerStart"
        self.request_id = REQUEST_UUID
        self.show_error = None
        self.find_rows = None

    def establish(self, route, model_policy, *, child_delegation=False):
        return {"schema": "pod-route-establishment/v1", "runtime": self.runtime,
                "version": "1.4.206", "executable": "/fixture/orca", "hard_stops": [],
                "disclosures": [], "route": {key: route.get(key) for key in
                ("agent", "model", "account", "bucket", "effort")}, "controls": {},
                "login": {"mode": "host_login", "auth": "oauth", "subscription": True,
                          "managed_accounts": 0, "identity_digest": None},
                "billing": {"observed": "subscription", "approved": "included"}}

    def read_native(self, owner, *, run=None, route=None, runs=()):
        rows = []
        for dispatch, item in self.workers.items():
            if run is None or item["run"] == run:
                rows.append({"dispatchId": dispatch, "runId": item["run"],
                             "taskId": item["task"], "terminalState": "active"})
        return {"runtime": self.runtime, "authoritative": True, "owner": owner,
                "scope": "all" if run is None else "bound+ledger_runs", "complete": True,
                "workers": rows, "cross_host": False, "atomic_admission": False,
                "quota": {"schema": "pod-quota/v1", "provider": "codex",
                          "account": "account", "bucket": "shared",
                          "observed_at": NOW.isoformat(), "source": "fixture",
                          "confidence": "observed", "unknowns": [],
                          "windows": [{"name": "hour", "remaining_percent": 80}]}}

    def start_worker(self, *, run, task, owner, route, worktree="current", retry_request=None):
        if retry_request is not None:
            self.retry_requests.append(retry_request)
        self.starts.append((run, task, worktree))
        dispatch = "dispatch" if not self.workers else f"dispatch-{len(self.workers) + 1}"
        self.workers[dispatch] = {"run": run, "task": task, "route": dict(route),
                                  "worktree": worktree}
        return {"runtime": self.runtime, "request_uuid": retry_request or REQUEST_UUID,
                "runId": run, "taskId": task, "dispatchId": dispatch, "state": "ready"}

    def request_show(self, request_uuid):
        receipt = None
        if self.workers:
            dispatch, item = next(iter(self.workers.items()))
            receipt = {"runId": item["run"], "taskId": item["task"],
                       "dispatchId": dispatch,
                       "mutation": {"requestId": request_uuid, "replayed": False}}
        return {"runtime": self.runtime, "result": {"requestId": self.request_id,
                "state": self.request_state, "method": self.request_method,
                "receipt": receipt}}

    def find_worker(self, *, run, task):
        if self.find_rows is not None:
            return list(self.find_rows)
        return [{"dispatchId": dispatch, "runId": item["run"], "taskId": item["task"]}
                for dispatch, item in self.workers.items()
                if item["run"] == run and item["task"] == task]

    def show_worker(self, dispatch):
        if self.show_error:
            raise self.show_error
        item = self.workers[dispatch]
        requested = {key: item["route"][key] for key in ("agent", "model", "effort")}
        return {"runtime": self.runtime, "result": {
            "dispatch": {"id": dispatch, "runId": item["run"], "taskId": item["task"]},
            "projection": {"id": "worker-" + dispatch, "dispatchId": dispatch,
                           "runId": item["run"], "taskId": item["task"]},
            "worker": {"dispatchId": dispatch, "worktreeId": item["worktree"],
                       "agentTerminalHandle": "terminal-" + dispatch, "state": "ready",
                       "startOptions": {"launch": {"requested": requested,
                                                     "effective": requested}}}}}


def setup_case(root, *, paid=False):
    project = root / "project"
    project.mkdir()
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod"
    config.mkdir(parents=True)
    binding = route_identity({"agent": "codex", "model": "gpt-5.6-sol",
                              "account": "account"})
    paid_policy = """policy:
  spending_grants:
    - id: paid-once
      action: paid_usage
      account: account
      model: gpt-5.6-sol
      objective: objective
      valid_until: '2026-09-23T00:00:00Z'
      max_units: 1
""" if paid else ""
    (config / "config.yaml").write_text(f"""schema: pod/v1
models:
  sol:
    agent: codex
    model: gpt-5.6-sol
    account: account
    approved: true
    approval_ref: review-1
    approval_route: {binding}
    billing: {"paid" if paid else "included"}
    efforts: [high]
{paid_policy}""")
    revision = effective(project)["revision"]
    checkpoint(project, "objective", owner="owner", value={
        "schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "plan",
        "candidate": "candidate", "policy_revision": revision, "native_refs": [],
        "assignments": [], "questions": [], "verification_gaps": ["works"],
        "next_safe_action": "inspect"}, native={"runtime": "runtime"})
    route = {"alias": "sol", "agent": "codex", "model": "gpt-5.6-sol",
             "account": "account", "bucket": "shared", "effort": "high"}
    frozen = packet({"schema": "pod-packet/v1", "objective": "objective",
                     "criteria": ["works"], "responsibility": "writer",
                     "scope": ["notes.txt"], "actions": ["edit"],
                     "candidate": "candidate", "context": [], "dependencies": [],
                     "route": route, "policy_revision": revision, "plan_revision": "plan",
                     "report_contract": "checks", "sources": []})
    assessment = {"method": "delegate", "responsibility": "bounded edit",
                  "complexity": "complex", "risk": "low", "size": "small",
                  "uncertainty": "low", "verifiability": "unit", "capabilities": [],
                  "context": [], "reason": "independent", "bounded": True}
    capabilities = {"sol": {"agent": "codex", "model": "gpt-5.6-sol",
                             "account": "account", "efforts": ["high"],
                             "capabilities": [], "bucket": "shared"}}
    quotas = {"account": {"schema": "pod-quota/v1", "provider": "codex",
                           "account": "account", "bucket": "shared",
                           "observed_at": NOW.isoformat(), "source": "fixture",
                           "confidence": "observed", "unknowns": [],
                           "windows": [{"name": "hour", "remaining_percent": 80}],
                           "remaining_percent": 80}}
    return project, assessment, capabilities, quotas, frozen


def start(project, assessment, capabilities, quotas, frozen, port, *, task="task"):
    return guarded_start(project, "objective", owner="owner", run="run", task=task,
                         assessment=assessment, capabilities=capabilities, quotas=quotas,
                         occupancy={}, plan_revision="plan", frozen_packet=frozen,
                         worktree="current", port=port, now=NOW)


def make_unresolved(project, admission_id, *, request_uuid=REQUEST_UUID):
    def apply(row):
        row["state"] = "unresolved"
        row["native_binding"] = None
        row["request_uuid"] = request_uuid
    return update_admission(project, "objective", owner="owner",
                            admission_id=admission_id, update=apply)


class AdmissionTests(unittest.TestCase):
    def test_policy_precedes_one_start_and_exact_binding(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            result = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(result["status"], "bound")
            self.assertEqual(len(port.starts), 1)
            row = result["admission"]
            self.assertEqual(row["request_uuid"], REQUEST_UUID)
            self.assertEqual(row["native_binding"]["dispatchId"], "dispatch")

    def test_unapproved_policy_stops_before_native_effect(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            config.write_text(config.read_text().replace("approved: true", "approved: false"))
            port = FakePort()
            with self.assertRaises(PodError):
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(port.starts, [])

    def test_uuid_is_persisted_before_worker_readback_failure(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.show_error = PodError("read_failed", "lost readback")
            with self.assertRaises(PodError):
                start(project, assessment, capabilities, quotas, frozen, port)
            row = next(iter(read(project, "objective")["admissions"].values()))
            self.assertEqual(row["request_uuid"], REQUEST_UUID)
            self.assertEqual(row["state"], "unresolved")

    def test_closed_admission_does_not_refund_a_spent_grant(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root, paid=True)
            port = FakePort()
            first = start(project, assessment, capabilities, quotas, frozen, port)
            update_admission(project, "objective", owner="owner",
                             admission_id=first["admission"]["admission_id"],
                             update=lambda row: row.update(state="closed"))
            port.workers.clear()
            with self.assertRaises(PodError) as caught:
                start(project, assessment, capabilities, quotas, frozen, port, task="task-two")
            self.assertEqual(caught.exception.code, "spending_grant_exhausted")
            self.assertEqual(len(port.starts), 1)

    def recovery_case(self):
        fixture_context = fixture()
        root = fixture_context.__enter__()
        self.addCleanup(fixture_context.__exit__, None, None, None)
        project, assessment, capabilities, quotas, frozen = setup_case(root)
        port = FakePort()
        result = start(project, assessment, capabilities, quotas, frozen, port)
        admission_id = result["admission"]["admission_id"]
        make_unresolved(project, admission_id)
        port.starts.clear()
        return project, port, admission_id

    def test_completed_request_records_receipt_without_second_start(self):
        project, port, admission_id = self.recovery_case()
        result = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(result["status"], "bound")
        self.assertEqual(result["action"], "recorded_receipt")
        self.assertEqual(port.starts, [])

    def test_pending_request_joins_exact_orca_uuid(self):
        project, port, admission_id = self.recovery_case()
        port.request_state = "pending"
        result = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(result["status"], "bound")
        self.assertEqual(port.retry_requests, [REQUEST_UUID])
        self.assertEqual(len(port.starts), 1)

    def test_absent_request_adopts_only_one_exact_attempt(self):
        project, port, admission_id = self.recovery_case()
        port.request_state = "absent"
        result = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(result["status"], "bound")
        self.assertEqual(port.starts, [])

    def test_absent_or_ambiguous_attempt_holds_without_start(self):
        for rows, code in (([], "native_attempt_absent"),
                           ([{"dispatchId": "dispatch"}, {"dispatchId": "dispatch"}],
                            "native_attempt_ambiguous")):
            with self.subTest(code=code):
                project, port, admission_id = self.recovery_case()
                port.request_state = "absent"
                port.find_rows = rows
                result = recover_admission(project, "objective", owner="owner",
                                           admission_id=admission_id, worktree="current", port=port)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(result["admission"]["error"]["code"], code)
                self.assertEqual(port.starts, [])

    def test_invalid_uuid_and_request_contradictions_hold_or_block(self):
        project, port, admission_id = self.recovery_case()
        make_unresolved(project, admission_id, request_uuid="not-a-uuid")
        result = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(result["admission"]["error"]["code"], "native_request_invalid")
        make_unresolved(project, admission_id)
        port.request_method = "orchestration.workerRelease"
        with self.assertRaises(PodError) as mismatch:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(mismatch.exception.code, "native_request_mismatch")
        self.assertEqual(port.starts, [])

    def test_recovery_rejects_changed_worktree_and_revoked_route(self):
        project, port, admission_id = self.recovery_case()
        with self.assertRaises(PodError) as worktree:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="other", port=port)
        self.assertEqual(worktree.exception.code, "admission_conflict")
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
        config.write_text(config.read_text().replace("approved: true", "approved: false"))
        with self.assertRaises(PodError) as revision:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(revision.exception.code, "policy_revision_mismatch")

    def test_worker_identity_or_launch_contradiction_cannot_bind(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            original = port.show_worker
            def wrong(dispatch):
                value = original(dispatch)
                value["result"]["dispatch"]["taskId"] = "other"
                return value
            port.show_worker = wrong
            with self.assertRaises(PodError) as caught:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(caught.exception.code, "native_identity_unverified")


class BoundaryTests(unittest.TestCase):
    def test_removed_lifecycle_operations_are_not_exposed(self):
        for operation in ("reconcile-launch", "delivery", "delivery-ack",
                          "release", "reconcile-release"):
            with self.subTest(operation=operation), self.assertRaises(PodError) as caught:
                helper_run(operation, {})
            self.assertEqual(caught.exception.code, "unknown_operation")

    def test_orca_port_pending_replay_uses_retry_request(self):
        payload = {"runtime": "runtime", "exit": 0, "request_uuid": REQUEST_UUID,
                   "result": {"runId": "run", "taskId": "task", "dispatchId": "dispatch"}}
        with (patch("pod.operations.worktree_selector", return_value="/worktree"),
              patch("pod.operations.mutate_command", return_value=payload) as command):
            result = OrcaPort().start_worker(run="run", task="task", owner="owner",
                                             route={"agent": "codex", "model": "model",
                                                    "effort": "high"}, worktree="current",
                                             retry_request=REQUEST_UUID)
        self.assertEqual(result["request_uuid"], REQUEST_UUID)
        self.assertIn("--retry-request", command.call_args.args[0])

    def test_report_joins_a_fresh_exact_worker_read(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            started = start(project, assessment, capabilities, quotas, frozen, port)
            admission = started["admission"]
            observation = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                           "attempt": admission["native_binding"]["dispatchId"],
                           "candidate": "candidate", "outcome": "succeeded",
                           "scope": ["notes.txt"], "files": ["notes.txt"],
                           "checks": ["unit"], "failures": [], "evidence": ["unit passed"],
                           "uncertainty": [], "questions": []}
            with patch("pod.operations.OrcaPort", return_value=port):
                result = helper_run("report", {"report": observation, "packet": frozen,
                                    "project": str(project), "objective": "objective",
                                    "admission_id": admission["admission_id"]})
            self.assertEqual(result["status"], "validated_observation")
            original = port.show_worker
            def wrong(dispatch):
                value = original(dispatch)
                value["result"]["projection"]["id"] = "different-worker"
                return value
            port.show_worker = wrong
            with patch("pod.operations.OrcaPort", return_value=port), self.assertRaises(PodError) as caught:
                helper_run("report", {"report": observation, "packet": frozen,
                            "project": str(project), "objective": "objective",
                            "admission_id": admission["admission_id"]})
            self.assertEqual(caught.exception.code, "report_attempt_unverified")


if __name__ == "__main__":
    unittest.main()

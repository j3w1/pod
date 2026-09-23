from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import effective, route_identity
from pod.errors import PodError
from pod.internal import run as helper_run
from pod.ledger import checkpoint, read, state_root, update_admission
from pod.operations import (OrcaPort, _assignment_evidence,
                            _capacity_refusal_classification, guarded_start,
                            recover_admission)
from pod.orca import _mutation_envelope
from pod.records import packet
from tests.common import fixture


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
REQUEST_UUID = "11111111-1111-4111-8111-111111111111"
ACCOUNT_IDENTITY = "a" * 64
OTHER_REQUEST_UUID = "22222222-2222-4222-8222-222222222222"


def decoded_success_receipt(*, result_request=REQUEST_UUID,
                            envelope_request=OTHER_REQUEST_UUID,
                            malformed_result=False):
    payload = {
        "ok": True,
        "result": {"runId": "run", "taskId": "task", "dispatchId": "dispatch",
                   "state": "ready",
                   "mutation": ("malformed" if malformed_result
                                else {"requestId": result_request})},
        "mutation": {"requestId": envelope_request},
        "_meta": {"runtimeId": "runtime"},
    }
    decoded = _mutation_envelope(json.dumps(payload))
    return {"runtime": decoded["runtime"], "exit": 0,
            "request_uuid": decoded["request_uuid"], **decoded["result"]}


class FakePort:
    def __init__(self):
        self.runtime = "runtime"
        self.starts = []
        self.retry_requests = []
        self.workers = {}
        self.request_state = "completed"
        self.request_method = "orchestration.workerStart"
        self.request_id = REQUEST_UUID
        self.request_receipt = None
        self.show_error = None
        self.find_rows = None
        self.quota = {"schema": "pod-quota/v1", "provider": "codex",
                      "account": ACCOUNT_IDENTITY, "bucket": "shared",
                      "observed_at": NOW.isoformat(), "source": "supported_metadata",
                      "confidence": "observed", "unknowns": [],
                      "windows": [{"name": "hour", "remaining_percent": 80}]}
        self.headless = False
        self.actual_identity = None
        self.start_receipt = None
        self.placement = None
        self.placement_error = None

    def establish(self, route, model_policy, *, child_delegation=False):
        observed_identity = self.actual_identity or route.get("account")
        return {"schema": "pod-route-establishment/v1", "runtime": self.runtime,
                "version": "1.4.206", "executable": "/fixture/orca", "hard_stops": [],
                "disclosures": [], "route": {**{key: route.get(key) for key in
                ("agent", "model", "bucket", "effort", "context", "effective_context")},
                "account": observed_identity},
                "controls": {"account_identity": {"tier": "runtime_observation",
                                                     "matched": observed_identity == route.get("account")},
                             "context_window": {"tier": "enforceable_control",
                                                "source": "explicit synthetic fixture"}},
                "login": {"mode": "host_login", "auth": "oauth", "subscription": True,
                          "managed_accounts": 0,
                          "identity_digest": observed_identity},
                "billing": {"observed": "subscription", "approved": "included"}}

    def resolve_worktree(self, selector):
        if self.placement_error is not None:
            raise self.placement_error
        if self.placement is None:
            raise PodError("worktree_resolution_unavailable", "fixture placement absent")
        return dict(self.placement)

    def read_native(self, owner, *, route=None, establishment=None, authority_runs=(),
                    assignments=()):
        rows = []
        for admission in assignments:
            binding = admission["native_binding"]
            item = self.workers.get(binding["dispatchId"])
            if item is None:
                continue
            rows.append(_assignment_evidence(self.show_worker(binding["dispatchId"]), admission))
        authoritative = bool(authority_runs)
        return {"runtime": self.runtime, "authoritative": authoritative,
                "owner": owner if authoritative else None,
                "scope": "objective_assignments", "complete": True,
                "assignments": rows, "physical_capacity": "unavailable",
                "quota": self.quota}

    def start_worker(self, *, run, task, owner, route, worktree="current", retry_request=None):
        if retry_request is not None:
            self.retry_requests.append(retry_request)
        self.starts.append((run, task, worktree))
        if self.start_receipt is not None:
            return dict(self.start_receipt)
        dispatch = "dispatch" if not self.workers else f"dispatch-{len(self.workers) + 1}"
        self.workers[dispatch] = {"run": run, "task": task, "route": dict(route),
                                  "worktree": worktree, "state": "ready",
                                  "outcome": "in_progress", "stage_detail": "input_accepted"}
        return {"runtime": self.runtime, "request_uuid": retry_request or REQUEST_UUID,
                "runId": run, "taskId": task, "dispatchId": dispatch, "state": "ready"}

    def request_show(self, request_uuid):
        receipt = self.request_receipt
        if receipt is None and self.workers:
            dispatch, item = next(iter(self.workers.items()))
            receipt = {"runId": item["run"], "taskId": item["task"],
                       "dispatchId": dispatch,
                       "mutation": {"requestId": request_uuid, "replayed": False}}
        result = {"requestId": self.request_id, "state": self.request_state,
                  "receipt": receipt}
        if self.request_state != "absent":
            result["method"] = self.request_method
        return {"runtime": self.runtime, "result": result}

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
        worker = {"dispatchId": dispatch, "worktreeId": item["worktree"],
                  "state": item["state"],
                  "startOptions": {"launch": {"requested": requested,
                                                  "effective": requested}}}
        if not self.headless:
            worker["agentTerminalHandle"] = "terminal-" + dispatch
        return {"runtime": self.runtime, "result": {
            "dispatch": {"id": dispatch, "runId": item["run"], "taskId": item["task"],
                         "status": "dispatched"},
            "projection": {"id": "worker-" + dispatch, "dispatchId": dispatch,
                           "runId": item["run"], "taskId": item["task"],
                           "outcome": item["outcome"],
                           "stage": {"dispatch": "dispatched", "worker": item["state"],
                                     "detail": item["stage_detail"]}},
            "worker": worker}}


def setup_case(root, *, paid=False):
    project = root / "project"
    project.mkdir()
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod"
    config.mkdir(parents=True)
    binding = route_identity({"agent": "codex", "model": "gpt-6-sol",
                              "account": ACCOUNT_IDENTITY})
    paid_policy = f"""policy:
  spending_grants:
    - id: paid-once
      action: paid_usage
      account: {ACCOUNT_IDENTITY}
      model: gpt-6-sol
      objective: objective
      valid_until: '2026-09-23T00:00:00Z'
      max_units: 1
""" if paid else ""
    (config / "config.yaml").write_text(f"""schema: pod/v1
models:
  sol:
    agent: codex
    model: gpt-6-sol
    account: {ACCOUNT_IDENTITY}
    approved: true
    approval_ref: review-1
    approval_route: {binding}
    billing: {"paid" if paid else "included"}
    efforts: [high]
routing:
  complex: {{model: sol, effort: high, context: max}}
{paid_policy}""")
    revision = effective(project)["revision"]
    checkpoint(project, "objective", owner="owner", value={
        "schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "plan",
        "candidate": "candidate", "policy_revision": revision, "native_refs": [],
        "assignments": [], "questions": [], "verification_gaps": ["works"],
        "next_safe_action": "inspect"}, native={"runtime": "runtime"})
    route = {"alias": "sol", "agent": "codex", "model": "gpt-6-sol",
             "account": ACCOUNT_IDENTITY, "bucket": "shared", "effort": "high",
             "context": "max", "effective_context": 900000}
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
    capabilities = {"sol": {"agent": "codex", "model": "gpt-6-sol",
                             "account": ACCOUNT_IDENTITY, "efforts": ["high"],
                             "capabilities": [], "bucket": "shared",
                             "suitable_for": ["complex"],
                             "context_control": "native_per_launch",
                             "contexts": {"256k": 256000, "max": 900000}}}
    quotas = {ACCOUNT_IDENTITY: {"schema": "pod-quota/v1", "provider": "codex",
                           "account": ACCOUNT_IDENTITY, "bucket": "shared",
                           "observed_at": NOW.isoformat(), "source": "fixture",
                           "confidence": "observed", "unknowns": [],
                           "windows": [{"name": "hour", "remaining_percent": 80}],
                           "remaining_percent": 80}}
    return project, assessment, capabilities, quotas, frozen


def start(project, assessment, capabilities, quotas, frozen, port, *, task="task"):
    return guarded_start(project, "objective", owner="owner", run="run", task=task,
                         assessment=assessment, capabilities=capabilities, quotas=quotas,
                         plan_revision="plan", frozen_packet=frozen,
                         worktree="current", port=port, now=NOW)


def make_unresolved(project, admission_id, *, request_uuid=REQUEST_UUID):
    def apply(row):
        row["state"] = "unresolved"
        row["native_binding"] = None
        row["request_uuid"] = request_uuid
    return update_admission(project, "objective", owner="owner",
                            admission_id=admission_id, update=apply)


class AdmissionTests(unittest.TestCase):
    def test_native_selector_resolution_fences_start_replay_and_allows_bound_isolation(self):
        with fixture() as root:
            project_path = root / "project"
            repo_key = "a" * 64
            context = {"repository": None, "repo_key": repo_key,
                       "worktree": str(project_path), "main_worktree": str(project_path),
                       "linked_worktrees": [str(project_path)], "branch": "orca/objective",
                       "dirty": None}
            with patch("pod.github.repository_context", return_value=context):
                project, assessment, capabilities, quotas, frozen = setup_case(root)
                objective = {"repository": None, "repo_key": repo_key,
                             "path": str(project), "branch": "orca/objective"}
                isolation = {"repository": None, "repo_key": repo_key,
                             "path": str(root / "assignment"), "branch": "orca/objective-check"}
                frozen = packet({**deepcopy(frozen["body"]), "worktree": objective,
                                 "placement": isolation})
                checkpoint_value = read(project, "objective")["checkpoint"]
                checkpoint_value["worktree"] = objective
                checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                           native={"runtime": "runtime"})
                port = FakePort()
                port.placement = {"runtime": "runtime", **{**isolation,
                                  "path": str(root / "wrong")}}
                with self.assertRaises(PodError) as wrong:
                    guarded_start(project, "objective", owner="owner", run="run", task="task",
                                  assessment=assessment, capabilities=capabilities, quotas=quotas,
                                  plan_revision="plan", frozen_packet=frozen,
                                  worktree="name:isolation", port=port, now=NOW)
                self.assertEqual(wrong.exception.code, "worktree_binding_changed")
                self.assertEqual(port.starts, [])

                for code in ("worktree_resolution_unavailable", "worktree_resolution_ambiguous"):
                    port.placement_error = PodError(code, "fixture refusal")
                    with self.subTest(code=code), self.assertRaises(PodError) as unresolved:
                        guarded_start(project, "objective", owner="owner", run="run", task="task",
                                      assessment=assessment, capabilities=capabilities, quotas=quotas,
                                      plan_revision="plan", frozen_packet=frozen,
                                      worktree="name:isolation", port=port, now=NOW)
                    self.assertEqual(unresolved.exception.code, code)
                    self.assertEqual(port.starts, [])
                port.placement_error = None
                port.placement = {"runtime": "runtime", **isolation}
                started = guarded_start(
                    project, "objective", owner="owner", run="run", task="task",
                    assessment=assessment, capabilities=capabilities, quotas=quotas,
                    plan_revision="plan", frozen_packet=frozen,
                    worktree="name:isolation", port=port, now=NOW)
                self.assertEqual(started["status"], "bound")

                admission_id = started["admission"]["admission_id"]
                make_unresolved(project, admission_id)
                port.starts.clear()
                port.request_state = "pending"
                port.placement = {"runtime": "runtime", **{**isolation,
                                  "repository": "other/repository"}}
                with self.assertRaises(PodError) as replay_wrong:
                    recover_admission(project, "objective", owner="owner",
                                      admission_id=admission_id, worktree="name:isolation", port=port)
                self.assertEqual(replay_wrong.exception.code, "worktree_binding_changed")
                self.assertEqual(port.starts, [])

    def test_worktree_binding_mismatch_stops_before_native_effect(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            bound = {"repository": "acme/widgets", "repo_key": "a" * 64,
                     "path": str(project), "branch": "orca/issue-7"}
            frozen = packet({**deepcopy(frozen["body"]), "worktree": bound})
            checkpoint_value = read(project, "objective")["checkpoint"]
            checkpoint_value["worktree"] = bound
            checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                       native={"runtime": "runtime"})
            observed = {"repository": "acme/widgets", "repo_key": None,
                        "worktree": str(project), "main_worktree": str(project),
                        "linked_worktrees": [str(project)], "branch": "orca/other",
                        "dirty": False}
            port = FakePort()
            with patch("pod.github.repository_context", return_value=observed), \
                 self.assertRaises(PodError) as mismatch:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(mismatch.exception.code, "worktree_binding_changed")
            self.assertEqual(port.starts, [])

    def test_issue_change_blocks_pending_replay_and_revised_duplicate_but_not_completed_read(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            source = {"schema": "pod-issue-source/v1", "repository": "acme/widgets",
                      "number": 7, "locator": "https://github.com/acme/widgets/issues/7",
                      "body_sha256": "a" * 64, "amendments": []}
            body = {**deepcopy(frozen["body"]), "objective_source": source}
            frozen = packet(body)
            checkpoint_value = read(project, "objective")["checkpoint"]
            checkpoint_value["objective_source"] = source
            checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                       native={"runtime": "runtime"})
            port = FakePort()
            current = {"status": "current"}
            with patch("pod.github.issue_recheck", return_value=current):
                started = start(project, assessment, capabilities, quotas, frozen, port)
            admission_id = started["admission"]["admission_id"]
            make_unresolved(project, admission_id)
            port.starts.clear()
            port.request_state = "pending"
            changed = {"status": "reconciliation_required"}
            with patch("pod.github.issue_recheck", return_value=changed), \
                 self.assertRaises(PodError) as blocked:
                recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)
            self.assertEqual(blocked.exception.code, "issue_reconciliation_required")
            self.assertEqual(port.starts, [])

            revised = {**source, "body_sha256": "b" * 64}
            revised_packet = packet({**deepcopy(body), "objective_source": revised})
            checkpoint_value = read(project, "objective")["checkpoint"]
            checkpoint_value["objective_source"] = revised
            checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                       native={"runtime": "runtime"})
            with patch("pod.github.issue_recheck", return_value=current), \
                 self.assertRaises(PodError) as duplicate:
                start(project, assessment, capabilities, quotas, revised_packet, port)
            self.assertEqual(duplicate.exception.code, "unresolved_prior_attempt")
            self.assertEqual(port.starts, [])

            port.request_state = "completed"
            with patch("pod.github.issue_recheck",
                       side_effect=AssertionError("completed recovery must stay observational")):
                recovered = recover_admission(project, "objective", owner="owner",
                                              admission_id=admission_id,
                                              worktree="current", port=port)
            self.assertEqual(recovered["status"], "bound")

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

    def test_decoded_success_request_contradiction_holds_fresh_admission_without_uuid_choice(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed), fixture() as root:
                project, assessment, capabilities, quotas, frozen = setup_case(root)
                port = FakePort()
                port.start_receipt = decoded_success_receipt(malformed_result=malformed)
                first = start(project, assessment, capabilities, quotas, frozen, port)
                port.workers["dispatch"] = {
                    "run": "run", "task": "task",
                    "route": dict(first["admission"]["request"]),
                    "worktree": "current", "state": "ready",
                    "outcome": "in_progress", "stage_detail": "input_accepted"}
                second = start(project, assessment, capabilities, quotas, frozen, port)
                self.assertEqual((first["status"], second["status"]),
                                 ("unresolved", "unresolved"))
                self.assertEqual(first["admission"]["error"]["code"],
                                 "native_request_conflict")
                self.assertEqual(second["admission"]["error"]["code"],
                                 "native_request_conflict")
                self.assertIsNone(first["admission"]["request_uuid"])
                self.assertIsNone(first["admission"]["native_binding"])
                self.assertEqual(len(port.starts), 1)

    def test_objective_fanout_admits_two_blocks_third_and_settlement_frees_slot(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            first = start(project, assessment, capabilities, quotas, frozen, port, task="one")
            start(project, assessment, capabilities, quotas, frozen, port, task="two")
            with self.assertRaises(PodError) as full:
                start(project, assessment, capabilities, quotas, frozen, port, task="three")
            self.assertEqual(full.exception.code, "logical_capacity_full")
            dispatch = first["admission"]["native_binding"]["dispatchId"]
            port.workers[dispatch]["state"] = "succeeded"
            port.workers[dispatch]["outcome"] = "succeeded"
            port.workers[dispatch]["stage_detail"] = "settled"
            third = start(project, assessment, capabilities, quotas, frozen, port, task="three")
            self.assertEqual(third["status"], "bound")
            self.assertIn("terminal-" + dispatch,
                          port.show_worker(dispatch)["result"]["worker"]["agentTerminalHandle"])

    def test_assignment_settlement_uses_one_coherent_projection_outcome(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            started = start(project, assessment, capabilities, quotas, frozen, port)
            admission = started["admission"]
            dispatch = admission["native_binding"]["dispatchId"]
            item = port.workers[dispatch]
            for outcome in ("succeeded", "failed", "stopped", "canceled", "cancelled",
                            "abandoned"):
                with self.subTest(outcome=outcome):
                    item.update(outcome=outcome, stage_detail="settled")
                    self.assertTrue(_assignment_evidence(
                        port.show_worker(dispatch), admission)["settled"])
            item.update(outcome="succeeded", stage_detail="working")
            self.assertFalse(_assignment_evidence(port.show_worker(dispatch), admission)["settled"])
            item.update(outcome="in_progress", stage_detail="settled")
            self.assertFalse(_assignment_evidence(port.show_worker(dispatch), admission)["settled"])
            shown = port.show_worker(dispatch)
            shown["result"]["projection"].pop("outcome")
            self.assertFalse(_assignment_evidence(shown, admission)["settled"])
            shown = port.show_worker(dispatch)
            shown["result"]["projection"]["stage"] = "settled"
            self.assertFalse(_assignment_evidence(shown, admission)["settled"])
            shown = port.show_worker(dispatch)
            shown["result"]["projection"]["outcome"] = ["succeeded"]
            self.assertFalse(_assignment_evidence(shown, admission)["settled"])

    def test_native_capacity_full_is_durable_deferred_not_a_binding_or_retry(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.start_receipt = {"runtime": "runtime", "exit": 1,
                                  "state": "deferred",
                                  "error": {"code": "capacity_full", "message": "full"}}
            first = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(first["status"], "deferred")
            self.assertIsNone(first["admission"]["native_binding"])
            self.assertIsNone(first["admission"]["request_uuid"])
            second = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(second["status"], "deferred")
            self.assertEqual(len(port.starts), 1)

    def test_conflicting_or_malformed_capacity_request_identity_holds_without_retry(self):
        variants = (
            {"request_uuid": REQUEST_UUID, "_request_conflict": True},
            {"request_uuid": "not-a-uuid"},
        )
        for evidence in variants:
            with self.subTest(evidence=evidence), fixture() as root:
                project, assessment, capabilities, quotas, frozen = setup_case(root)
                port = FakePort()
                port.start_receipt = {
                    "runtime": "runtime", "exit": 1, "state": "deferred",
                    "error": {"code": "capacity_full", "message": "full"}, **evidence}
                first = start(project, assessment, capabilities, quotas, frozen, port)
                second = start(project, assessment, capabilities, quotas, frozen, port)
                self.assertEqual((first["status"], second["status"]),
                                 ("unresolved", "unresolved"))
                self.assertEqual(first["admission"]["error"]["code"],
                                 "native_capacity_refusal_unverified")
                self.assertEqual(len(port.starts), 1)

    def test_capacity_full_with_partial_effect_evidence_remains_unresolved(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.start_receipt = {"runtime": "runtime", "exit": 1,
                                  "request_uuid": REQUEST_UUID, "state": "deferred",
                                  "dispatchId": "partial-dispatch",
                                  "error": {"code": "capacity_full", "message": "full"}}
            result = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(result["status"], "unresolved")
            self.assertIsNone(result["admission"]["native_binding"])
            self.assertEqual(result["admission"]["error"]["code"],
                             "native_capacity_refusal_unverified")

    def test_capacity_refusal_preserves_error_data_request_reference(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.start_receipt = {
                "runtime": "runtime", "exit": 1, "state": "deferred",
                "error": {"code": "capacity_full", "message": "full",
                          "data": {"orchestrationRequestId": REQUEST_UUID}}}
            result = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["admission"]["request_uuid"], REQUEST_UUID)

    def test_capacity_refusal_classifier_rejects_runtime_and_envelope_contradictions(self):
        admission = {"runtime": "runtime"}
        base = {"runtime": "runtime", "exit": 1, "request_uuid": REQUEST_UUID,
                "state": "deferred", "error": {"code": "capacity_full", "message": "full"}}
        self.assertEqual(_capacity_refusal_classification(base, admission), "authoritative")
        data_request = {**base, "request_uuid": None,
                        "error": {**base["error"],
                                  "data": {"orchestrationRequestId": REQUEST_UUID}}}
        self.assertEqual(_capacity_refusal_classification(data_request, admission),
                         "authoritative")
        variants = {
            "wrong_runtime": {**base, "runtime": "other"},
            "missing_runtime": {key: value for key, value in base.items() if key != "runtime"},
            "malformed_runtime": {**base, "runtime": 7},
            "nested_dispatch": {**base, "error": {**base["error"],
                                "data": {"dispatchId": "partial"}}},
            "nested_worker": {**base, "error": {**base["error"],
                              "data": {"worker_id": "partial"}}},
            "nested_residual": {**base, "error": {**base["error"],
                                "data": {"residualResources": [{"kind": "terminal"}]}}},
            "malformed_empty_residual": {**base, "error": {**base["error"],
                                         "data": {"residualResources": {}}}},
            "malformed_null_effects": {**base, "error": {**base["error"],
                                       "data": {"effects": None}}},
            "alias_conflict": {**base, "dispatchId": None, "dispatch_id": "partial"},
            "result_error_conflict": {**base, "_result_error": {"code": "other"}},
            "envelope_conflict": {**base, "_envelope_conflicts": {"workerId": "partial"}},
            "request_conflict": {**base, "_request_conflict": True},
            "malformed_data": {**base, "error": {**base["error"], "data": "unknown"}},
        }
        for name, receipt in variants.items():
            with self.subTest(name=name):
                self.assertEqual(_capacity_refusal_classification(receipt, admission),
                                 "unverified")

    def test_runtime_conflicting_capacity_refusal_is_held_with_request_reference(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.start_receipt = {"runtime": "other", "exit": 1,
                                  "request_uuid": REQUEST_UUID, "state": "deferred",
                                  "error": {"code": "capacity_full", "message": "full"}}
            result = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(result["admission"]["request_uuid"], REQUEST_UUID)
            self.assertEqual(result["admission"]["error"]["code"],
                             "native_capacity_refusal_unverified")

    def test_unapproved_policy_stops_before_native_effect(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            config.write_text(config.read_text().replace("approved: true", "approved: false"))
            port = FakePort()
            with self.assertRaises(PodError):
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(port.starts, [])

    def test_paid_grant_cannot_authorize_a_rotated_native_account(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root, paid=True)
            port = FakePort()
            port.actual_identity = "b" * 64
            with self.assertRaises(PodError) as caught:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(caught.exception.code, "account_binding_unverified")
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

    def test_decoded_success_request_contradiction_holds_completed_and_pending_recovery(self):
        for path in ("completed", "pending"):
            with self.subTest(path=path):
                project, port, admission_id = self.recovery_case()
                receipt = decoded_success_receipt()
                if path == "completed":
                    port.request_receipt = receipt
                else:
                    port.request_state = "pending"
                    port.start_receipt = receipt
                first = recover_admission(project, "objective", owner="owner",
                                          admission_id=admission_id,
                                          worktree="current", port=port)
                second = recover_admission(project, "objective", owner="owner",
                                           admission_id=admission_id,
                                           worktree="current", port=port)
                self.assertEqual((first["status"], second["status"]),
                                 ("unresolved", "unresolved"))
                self.assertEqual(first["admission"]["error"]["code"],
                                 "native_request_conflict")
                self.assertEqual(first["admission"]["request_uuid"], REQUEST_UUID)
                self.assertIsNone(first["admission"]["native_binding"])
                expected_starts = 0 if path == "completed" else 1
                self.assertEqual(len(port.starts), expected_starts)
                self.assertEqual(port.retry_requests,
                                 [] if path == "completed" else [REQUEST_UUID])
                if path == "pending":
                    port.request_state = "absent"
                    absent = recover_admission(project, "objective", owner="owner",
                                               admission_id=admission_id,
                                               worktree="current", port=port)
                    self.assertEqual(absent["status"], "unresolved")
                    self.assertEqual(absent["admission"]["error"]["code"],
                                     "native_request_conflict")
                    port.request_state = "completed"
                    completed = recover_admission(project, "objective", owner="owner",
                                                  admission_id=admission_id,
                                                  worktree="current", port=port)
                    self.assertEqual(completed["status"], "bound")
                    self.assertEqual(len(port.starts), 1)

    def test_request_conflict_survives_incomplete_completed_then_absent(self):
        project, port, admission_id = self.recovery_case()
        port.request_receipt = decoded_success_receipt()
        conflicted = recover_admission(
            project, "objective", owner="owner", admission_id=admission_id,
            worktree="current", port=port)
        self.assertEqual(conflicted["admission"]["recovery"]["request_conflict"],
                         "unresolved")

        port.request_receipt = "malformed"
        incomplete = recover_admission(
            project, "objective", owner="owner", admission_id=admission_id,
            worktree="current", port=port)
        self.assertEqual(incomplete["admission"]["error"]["code"],
                         "native_receipt_missing")
        self.assertEqual(incomplete["admission"]["recovery"]["request_conflict"],
                         "unresolved")

        port.request_state = "absent"
        absent = recover_admission(
            project, "objective", owner="owner", admission_id=admission_id,
            worktree="current", port=port)
        self.assertEqual((absent["status"], absent["action"]), ("unresolved", "hold"))
        self.assertIsNone(absent["admission"]["native_binding"])
        self.assertEqual(port.starts, [])

    def test_legacy_conflict_signals_are_promoted_before_error_replacement(self):
        for legacy_signal in ("error", "capacity_refusal"):
            with self.subTest(legacy_signal=legacy_signal):
                project, port, admission_id = self.recovery_case()

                def make_legacy(row):
                    row["recovery"].pop("request_conflict", None)
                    if legacy_signal == "error":
                        row["error"] = {"code": "native_request_conflict",
                                        "detail": "legacy conflict"}
                    else:
                        row["error"] = {"code": "native_capacity_refusal_unverified",
                                        "detail": "legacy capacity conflict"}
                        row["recovery"]["capacity_refusal"] = "unverified"

                update_admission(project, "objective", owner="owner",
                                 admission_id=admission_id, update=make_legacy)
                port.request_receipt = "malformed"
                incomplete = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual(incomplete["admission"]["error"]["code"],
                                 "native_receipt_missing")
                self.assertEqual(incomplete["admission"]["recovery"]["request_conflict"],
                                 "unresolved")

                port.request_state = "absent"
                absent = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual((absent["status"], absent["action"]),
                                 ("unresolved", "hold"))
                self.assertEqual(port.starts, [])

    def test_capacity_request_conflict_survives_absent_adoption(self):
        variants = (
            {"request_uuid": REQUEST_UUID, "_request_conflict": True},
            {"request_uuid": "not-a-uuid"},
        )
        for request_evidence in variants:
            with self.subTest(request_evidence=request_evidence):
                project, port, admission_id = self.recovery_case()
                port.request_receipt = {
                    "state": "deferred", "exit": 1,
                    "error": {"code": "capacity_full", "message": "full"},
                    **request_evidence,
                }
                conflicted = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual(conflicted["admission"]["error"]["code"],
                                 "native_capacity_refusal_unverified")
                self.assertEqual(conflicted["admission"]["recovery"]["request_conflict"],
                                 "unresolved")

                port.request_state = "absent"
                absent = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual((absent["status"], absent["action"]),
                                 ("unresolved", "hold"))
                self.assertIsNone(absent["admission"]["native_binding"])
                self.assertEqual(port.starts, [])

    def test_request_conflict_holds_pending_until_coherent_completed_reconciliation(self):
        completed_receipts = (
            ({"runId": "run", "taskId": "task", "dispatchId": "dispatch",
              "state": "ready"}, "bound"),
            ({"state": "deferred", "exit": 1,
              "error": {"code": "capacity_full", "message": "full"}}, "deferred"),
        )
        for completed_receipt, expected in completed_receipts:
            with self.subTest(expected=expected):
                project, port, admission_id = self.recovery_case()
                port.request_receipt = decoded_success_receipt()
                recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)
                port.request_receipt = "malformed"
                recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)

                port.request_state = "pending"
                first_pending = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                second_pending = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual((first_pending["action"], second_pending["action"]),
                                 ("hold", "hold"))
                self.assertEqual(port.starts, [])

                port.request_state = "completed"
                port.request_receipt = completed_receipt
                reconciled = recover_admission(
                    project, "objective", owner="owner", admission_id=admission_id,
                    worktree="current", port=port)
                self.assertEqual(reconciled["status"], expected)
                self.assertNotIn("request_conflict", reconciled["admission"]["recovery"])
                self.assertEqual(port.starts, [])

    def test_completed_raw_receipt_joins_present_request_references_to_outer_uuid(self):
        base = {"runId": "run", "taskId": "task", "dispatchId": "dispatch",
                "state": "ready"}
        variants = {
            "optional_absent": (base, "bound"),
            "wrong_request_uuid": ({**base, "request_uuid": OTHER_REQUEST_UUID},
                                   "unresolved"),
            "wrong_mutation": ({**base,
                                "mutation": {"requestId": OTHER_REQUEST_UUID}},
                               "unresolved"),
            "malformed_mutation": ({**base, "mutation": "malformed"}, "unresolved"),
        }
        for name, (receipt, expected) in variants.items():
            with self.subTest(name=name):
                project, port, admission_id = self.recovery_case()
                port.request_receipt = receipt
                first = recover_admission(project, "objective", owner="owner",
                                          admission_id=admission_id,
                                          worktree="current", port=port)
                self.assertEqual(first["status"], expected)
                self.assertEqual(port.starts, [])
                if expected == "unresolved":
                    self.assertEqual(first["admission"]["error"]["code"],
                                     "native_request_conflict")
                    second = recover_admission(project, "objective", owner="owner",
                                               admission_id=admission_id,
                                               worktree="current", port=port)
                    self.assertEqual(second["status"], "unresolved")
                    self.assertEqual(second["admission"]["request_uuid"], REQUEST_UUID)

    def test_completed_capacity_refusal_defers_once_without_start(self):
        project, port, admission_id = self.recovery_case()
        port.request_receipt = {"state": "deferred",
                                "error": {"code": "capacity_full", "message": "full"}}
        first = recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)
        second = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual((first["status"], second["status"]), ("deferred", "deferred"))
        self.assertEqual(first["admission"]["request_uuid"], REQUEST_UUID)
        self.assertEqual(port.starts, [])

    def test_pending_capacity_refusal_defers_once_without_another_replay(self):
        project, port, admission_id = self.recovery_case()
        port.request_state = "pending"
        port.start_receipt = {"runtime": "runtime", "exit": 1,
                              "request_uuid": REQUEST_UUID, "state": "deferred",
                              "error": {"code": "capacity_full", "message": "full"}}
        first = recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)
        second = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual((first["status"], second["status"]), ("deferred", "deferred"))
        self.assertEqual(port.retry_requests, [REQUEST_UUID])
        self.assertEqual(len(port.starts), 1)

    def test_unverified_recovered_capacity_refusals_hold_without_replay_loop(self):
        variants = (
            {"runtime": "other", "state": "deferred",
             "error": {"code": "capacity_full", "message": "full"}},
            {"state": "deferred", "error": {"code": "capacity_full", "message": "full",
                                              "data": {"dispatchId": "partial"}}},
        )
        for path in ("completed", "pending"):
            for receipt in variants:
                with self.subTest(path=path, receipt=receipt):
                    project, port, admission_id = self.recovery_case()
                    if path == "completed":
                        port.request_receipt = receipt
                    else:
                        port.request_state = "pending"
                        port.start_receipt = {"request_uuid": REQUEST_UUID, "exit": 1,
                                              **receipt}
                    first = recover_admission(project, "objective", owner="owner",
                                              admission_id=admission_id,
                                              worktree="current", port=port)
                    second = recover_admission(project, "objective", owner="owner",
                                               admission_id=admission_id,
                                               worktree="current", port=port)
                    self.assertEqual((first["status"], second["status"]),
                                     ("unresolved", "unresolved"))
                    self.assertEqual(first["admission"]["error"]["code"],
                                     "native_capacity_refusal_unverified")
                    self.assertLessEqual(len(port.starts), 1)

    def test_account_rotation_blocks_pending_replay_but_not_completed_observation(self):
        project, port, admission_id = self.recovery_case()
        port.actual_identity = "b" * 64
        port.request_state = "pending"
        with self.assertRaises(PodError) as caught:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(caught.exception.code, "account_binding_unverified")
        self.assertEqual(port.starts, [])

        project, port, admission_id = self.recovery_case()
        port.actual_identity = "b" * 64
        observed = recover_admission(project, "objective", owner="owner",
                                     admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(observed["status"], "bound")
        self.assertEqual(port.starts, [])

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

    def test_recovery_rejects_changed_worktree_but_completed_read_survives_revocation(self):
        project, port, admission_id = self.recovery_case()
        with self.assertRaises(PodError) as worktree:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="other", port=port)
        self.assertEqual(worktree.exception.code, "admission_conflict")
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
        config.write_text(config.read_text().replace("approved: true", "approved: false"))
        recovered = recover_admission(project, "objective", owner="owner",
                                      admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(recovered["status"], "bound")
        self.assertEqual(port.starts, [])

    def test_revocation_blocks_pending_replay_but_not_public_completed_recovery(self):
        project, port, admission_id = self.recovery_case()
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
        config.write_text(config.read_text().replace("approved: true", "approved: false"))
        port.request_state = "pending"
        with self.assertRaises(PodError) as revision:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(revision.exception.code, "policy_revision_mismatch")
        self.assertEqual(port.starts, [])

    def test_pending_replay_rechecks_new_task_and_checkpoint_restrictions(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            started = start(project, assessment, capabilities, quotas, frozen, port)
            admission_id = started["admission"]["admission_id"]
            make_unresolved(project, admission_id)
            port.starts.clear()
            port.request_state = "pending"
            with self.assertRaises(PodError) as task_limit:
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              assessment=assessment, capabilities=capabilities, quotas=quotas,
                              plan_revision="plan", frozen_packet=frozen,
                              task_policy={"schema": "pod/v1", "policy": {"max_workers": 0}},
                              port=port, now=NOW)
            self.assertEqual(task_limit.exception.code, "policy_revision_mismatch")
            self.assertEqual(port.starts, [])
            checkpoint_value = read(project, "objective")["checkpoint"]
            checkpoint_value["candidate"] = "changed-candidate"
            checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                       native={"runtime": "runtime"})
            with self.assertRaises(PodError) as checkpoint_change:
                recover_admission(project, "objective", owner="owner",
                                  admission_id=admission_id, worktree="current", port=port)
            self.assertEqual(checkpoint_change.exception.code, "checkpoint_binding_changed")
            self.assertEqual(port.starts, [])

    def test_pending_replay_requires_complete_known_checkpoint_binding(self):
        core = {"candidate", "criteria", "plan_revision", "policy_revision"}
        variants = ({}, {"candidate": "candidate"},
                    {"candidate": "candidate", "criteria": ["works"],
                     "plan_revision": "plan", "policy_revision": "policy",
                     "unknown": "field"})
        # The value of a malformed/unknown binding is irrelevant: its shape must stop replay.
        for index, binding in enumerate(variants):
            with self.subTest(index=index):
                project, port, admission_id = self.recovery_case()
                port.request_state = "pending"
                update_admission(project, "objective", owner="owner", admission_id=admission_id,
                                 update=lambda row, value=binding:
                                 row["recovery"].update(checkpoint_binding=value))
                with self.assertRaises(PodError) as blocked:
                    recover_admission(project, "objective", owner="owner",
                                      admission_id=admission_id, worktree="current", port=port)
                self.assertEqual(blocked.exception.code, "checkpoint_binding_changed")
                self.assertEqual(port.starts, [])

        project, port, admission_id = self.recovery_case()
        port.request_state = "pending"
        row = read(project, "objective")["admissions"][admission_id]
        legacy_core = {key: row["recovery"]["checkpoint_binding"][key] for key in core}
        update_admission(project, "objective", owner="owner", admission_id=admission_id,
                         update=lambda admission:
                         admission["recovery"].update(checkpoint_binding=legacy_core))
        checkpoint_value = read(project, "objective")["checkpoint"]
        checkpoint_value["objective_source"] = {
            "schema": "pod-issue-source/v1", "repository": "acme/widgets", "number": 7,
            "locator": "https://github.com/acme/widgets/issues/7",
            "body_sha256": "a" * 64, "amendments": []}
        checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                   native={"runtime": "runtime"})
        with self.assertRaises(PodError) as added_semantics:
            recover_admission(project, "objective", owner="owner",
                              admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(added_semantics.exception.code, "checkpoint_binding_changed")
        self.assertEqual(port.starts, [])

    def test_public_admission_reentry_recovers_completed_after_revocation(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            started = start(project, assessment, capabilities, quotas, frozen, port)
            make_unresolved(project, started["admission"]["admission_id"])
            port.starts.clear()
            config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            config.write_text(config.read_text().replace("approved: true", "approved: false"))
            recovered = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(recovered["status"], "bound")
            self.assertEqual(port.starts, [])

    def test_real_absent_shape_without_method_inspects_without_start(self):
        project, port, admission_id = self.recovery_case()
        port.request_state = "absent"
        result = recover_admission(project, "objective", owner="owner",
                                   admission_id=admission_id, worktree="current", port=port)
        self.assertEqual(result["action"], "inspect_after_absent")
        self.assertEqual(result["status"], "bound")
        self.assertEqual(port.starts, [])

    def test_headless_worker_binds_without_a_terminal_identity(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.headless = True
            result = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(result["status"], "bound")
            self.assertIsNone(result["admission"]["native_binding"]["terminalHandle"])

    def test_exceptional_grant_cannot_bypass_task_max_workers(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            grant = {"id": "wide", "action": "exceptional_capacity", "account": ACCOUNT_IDENTITY,
                     "objective": "objective", "run": "run", "plan_revision": "plan",
                     "limit": 4, "reason": "four independent checks",
                     "valid_until": "2026-09-23T00:00:00Z"}
            config = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            config.write_text(config.read_text() + "\npolicy:\n  exceptional_grants:\n"
                              "    - id: wide\n      action: exceptional_capacity\n"
                              f"      account: {ACCOUNT_IDENTITY}\n      objective: objective\n      run: run\n"
                              "      plan_revision: plan\n      limit: 4\n"
                              "      reason: four independent checks\n"
                              "      valid_until: '2026-09-23T00:00:00Z'\n")
            task_policy = {"schema": "pod/v1", "policy": {"max_workers": 2}}
            revision = effective(project, task=task_policy)["revision"]
            frozen = packet({**frozen["body"], "policy_revision": revision})
            port = FakePort()
            with self.assertRaises(PodError) as caught:
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              assessment=assessment, capabilities=capabilities, quotas=quotas,
                              plan_revision="plan", frozen_packet=frozen,
                              capacity=4, exceptional_grant=grant, task_policy=task_policy,
                              port=port, now=NOW)
            self.assertEqual(caught.exception.code, "capacity_ceiling")
            self.assertEqual(port.starts, [])
            personal_revision = effective(project)["revision"]
            personal_packet = packet({**frozen["body"], "policy_revision": personal_revision})
            allowed = guarded_start(project, "objective", owner="owner", run="run",
                                    task="task-positive", assessment=assessment,
                                    capabilities=capabilities, quotas=quotas,
                                    plan_revision="plan", frozen_packet=personal_packet,
                                    capacity=4, exceptional_grant=grant, port=port, now=NOW)
            self.assertEqual(allowed["status"], "bound")

    def test_source_and_instruction_context_state_matrix_blocks_before_start(self):
        for kind in ("source", "instruction"):
            for change, code in (("changed", "source_changed"), ("absent", "source_absent")):
                with self.subTest(kind=kind, change=change), fixture() as root:
                    project, assessment, capabilities, quotas, frozen = setup_case(root)
                    source = project / "bound.txt"
                    source.write_text("one")
                    from pod.records import source_identity
                    bound = source_identity(project, "bound.txt")
                    frozen = packet({**frozen["body"], "context": [
                        {"kind": kind, "path": "bound.txt", "sha256": bound["sha256"]}]})
                    source.write_text("two") if change == "changed" else source.unlink()
                    port = FakePort()
                    with self.assertRaises(PodError) as caught:
                        start(project, assessment, capabilities, quotas, frozen, port)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(port.starts, [])
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            frozen = packet({**frozen["body"], "sources": [
                {"path": "unavailable.txt", "state": "unavailable"}]})
            port = FakePort()
            with self.assertRaises(PodError) as caught:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(caught.exception.code, "source_unbound")
            self.assertEqual(port.starts, [])

    def test_independent_objectives_ignore_foreign_history_under_unknown_quota(self):
        with fixture() as root:
            project, assessment, capabilities, _, frozen = setup_case(root)
            revision = effective(project)["revision"]
            checkpoint(project, "second", owner="owner", value={
                "schema": "pod-checkpoint/v1", "criteria": ["works"],
                "plan_revision": "plan", "candidate": "candidate",
                "policy_revision": revision, "native_refs": [], "assignments": [],
                "questions": [], "verification_gaps": ["works"],
                "next_safe_action": "inspect"}, native={"runtime": "runtime"})
            second_packet = packet({**frozen["body"], "objective": "second"})
            port = FakePort()
            port.quota = None
            def attempt(item):
                objective, task_name, selected_packet = item
                try:
                    guarded_start(project, objective, owner="owner", run="run", task=task_name,
                                  assessment=assessment, capabilities=capabilities, quotas={},
                                  plan_revision="plan", frozen_packet=selected_packet,
                                  port=port, now=NOW)
                    return "admitted"
                except PodError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt, row) for row in (
                    ("objective", "task-one", frozen), ("second", "task-two", second_packet))]
                outcomes = [future.result(timeout=5) for future in futures]
            self.assertEqual(outcomes, ["admitted", "admitted"])

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

    def test_effective_route_mismatch_holds_same_attempt_without_replacement(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            original = port.show_worker
            def wrong(dispatch):
                value = original(dispatch)
                value["result"]["worker"]["startOptions"]["launch"]["effective"]["model"] = "other"
                return value
            port.show_worker = wrong
            with self.assertRaises(PodError) as first:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(first.exception.code, "effective_launch_unverified")
            port.starts.clear()
            with self.assertRaises(PodError) as repeated:
                start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual(repeated.exception.code, "effective_launch_unverified")
            self.assertEqual(port.starts, [])

    def test_failed_startup_holds_same_attempt_without_replacement(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            port = FakePort()
            port.workers["failed"] = {"run": "run", "task": "task",
                                      "route": frozen["body"]["route"],
                                      "worktree": "current", "state": "failed",
                                      "outcome": "failed", "stage_detail": "settled"}
            port.start_receipt = {"runtime": "runtime", "exit": 1,
                                  "request_uuid": REQUEST_UUID, "state": "failed",
                                  "runId": "run", "taskId": "task", "dispatchId": "failed",
                                  "failedStage": "dispatch_input"}
            first = start(project, assessment, capabilities, quotas, frozen, port)
            repeated = start(project, assessment, capabilities, quotas, frozen, port)
            self.assertEqual((first["status"], repeated["status"]), ("bound", "bound"))
            self.assertEqual(first["admission"]["native_binding"]["dispatchId"], "failed")
            self.assertEqual(len(port.starts), 1)


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

    def test_policy_evidence_never_enumerates_the_fleet(self):
        row = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 3}
        current = {"runtime": "runtime", "run": row}
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}), \
             patch("pod.operations.current_run", return_value=current), \
             patch("pod.operations.worker_rows", side_effect=AssertionError("fleet read forbidden")):
            native = OrcaPort().read_native("owner", authority_runs=("run",))
        self.assertEqual(native["scope"], "objective_assignments")
        self.assertEqual(native["assignments"], [])
        self.assertEqual(native["physical_capacity"], "unavailable")

    def test_fresh_account_read_never_relabels_a_rotated_identity(self):
        route = {"alias": "sol", "agent": "codex", "model": "gpt-6-sol",
                 "account": ACCOUNT_IDENTITY, "bucket": "default", "effort": "high",
                 "context": "max", "effective_context": 900000}
        established = FakePort().establish(route, {"billing": "included"})
        rotated = {"runtime": "runtime", "providers": {"codex": {
            "managed_accounts": 0, "default_identity": "b" * 64,
            "default_auth": "oauth", "default_has_auth": True, "windows": {}}}}
        with patch("pod.operations.contract", return_value={"status": "observed", "runtime": "runtime"}), \
             patch("pod.operations.account_metadata_raw", return_value=rotated), \
             patch("pod.operations.agent_login_mode", return_value={
                 "auth": "oauth", "subscription": True, "identity_digest": "b" * 64}), \
             self.assertRaises(PodError) as caught:
            OrcaPort().read_native("owner", route=route, establishment=established)
        self.assertEqual(caught.exception.code, "account_binding_unverified")

        current = {"runtime": "runtime", "providers": {"codex": {
            "managed_accounts": 0, "default_identity": ACCOUNT_IDENTITY,
            "default_auth": "oauth", "default_has_auth": True,
            "updated_at_ms": NOW.timestamp() * 1000,
            "windows": {"session": {"usedPercent": 20, "resetsAt": None}}}}}
        with patch("pod.operations.contract", return_value={"status": "observed", "runtime": "runtime"}), \
             patch("pod.operations.account_metadata_raw", return_value=current), \
             patch("pod.operations.agent_login_mode", return_value={
                 "auth": "api_key", "subscription": False,
                 "identity_digest": "b" * 64}):
            native = OrcaPort().read_native(
                "owner", route=route, establishment=established)
        self.assertEqual(native["quota"]["account"], ACCOUNT_IDENTITY)

    def test_production_authority_joins_current_run_owner_and_objective_reference(self):
        with fixture() as root:
            project, _, _, _, _ = setup_case(root)
            checkpoint_value = read(project, "objective")["checkpoint"]
            checkpoint_value["native_refs"] = [{"runId": "run", "runtime": "runtime"}]
            checkpoint(project, "objective", owner="owner", value=checkpoint_value,
                       native={"runtime": "runtime"})
            row = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 3}
            no_run = {"runtime": "runtime", "run": None}
            base = {"project": str(project), "objective": "objective", "owner": "owner"}
            with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}), \
                 patch("pod.operations.current_run", return_value=no_run), \
                 patch("pod.operations.contract", return_value={"status": "observed", "runtime": "runtime"}), \
                 patch("pod.governor.observe_candidate") as observer, \
                 patch("pod.governor.execute") as executor:
                with self.assertRaises(PodError) as blocked:
                    helper_run("governor-prepare", {**base, "unit": "unit"})
                self.assertEqual(blocked.exception.code, "native_authority_unverified")
                with self.assertRaises(PodError):
                    helper_run("governor-execute", {**base, "action": {}})
                status = helper_run("governor-status", base)
                observer.assert_not_called()
                executor.assert_not_called()
            self.assertEqual(status["schema"], "pod-governor/v2")
            self.assertEqual(list(state_root(project).rglob("governor.json")), [])

            takeover = {**base, "owner": "new-owner", "unit": "unit"}
            with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "new-owner"}), \
                 patch("pod.operations.current_run") as binding, \
                 patch("pod.governor.observe_candidate") as observer, \
                 self.assertRaises(PodError) as conflict:
                helper_run("governor-prepare", takeover)
            self.assertEqual(conflict.exception.code, "native_authority_unverified")
            binding.assert_not_called()
            observer.assert_not_called()

            observation = {"schema": "pod-candidate-observation/v1", "commit": "1" * 40,
                           "tree": "2" * 40, "dirty_paths": 0, "base": None,
                           "workflows": {}, "verification": [], "toolchain": {},
                           "environment": {},
                           "policy_revision": effective(project)["revision"]}
            current = {"runtime": "runtime", "run": row}
            with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}), \
                 patch("pod.operations.current_run", return_value=current), \
                 patch("pod.operations.worker_rows", side_effect=AssertionError("fleet read forbidden")), \
                 patch("pod.governor.observe_candidate", return_value=observation):
                prepared = helper_run("governor-prepare", {**base, "unit": "unit"})
            self.assertEqual(prepared["status"], "prepared")

    def test_native_authority_rejects_wrong_run_owner_runtime_and_changing_binding(self):
        owner_row = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 2}
        cases = {
            "wrong_run": [{"runtime": "runtime", "run": {**owner_row, "id": "other"}}] * 2,
            "wrong_owner": [{"runtime": "runtime", "run": {
                **owner_row, "coordinator_handle": "worker"}}] * 2,
            "changing": [{"runtime": "runtime", "run": owner_row},
                         {"runtime": "runtime", "run": None}],
        }
        for name, bindings in cases.items():
            with self.subTest(name=name), \
                 patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}), \
                 patch("pod.operations.current_run", side_effect=bindings):
                native = OrcaPort().read_native("owner", authority_runs=("run",))
            self.assertFalse(native["authoritative"])
            self.assertIsNone(native["owner"])
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}), \
             patch("pod.operations.current_run", side_effect=[
                 {"runtime": "other", "run": owner_row},
                 {"runtime": "other", "run": owner_row}]):
            native = OrcaPort().read_native("owner", authority_runs=("run",))
        self.assertEqual(native["runtime"], "other")

    def test_governor_mutations_require_native_owner_and_runtime_but_status_is_read_only(self):
        with fixture() as root:
            project, assessment, capabilities, quotas, frozen = setup_case(root)
            unowned = {"runtime": "runtime", "authoritative": False, "owner": None,
                       "scope": "objective_assignments", "complete": True,
                       "assignments": [], "physical_capacity": "unavailable"}
            requests = {
                "governor-prepare": {"unit": "unit"},
                "governor": {"action": {}},
                "governor-execute": {"action": {}},
                "governor-preflight": {"unit": "unit", "candidate": "candidate",
                                        "check": "unit", "status": "PASS"},
                "governor-outcome": {"record_id": "record", "outcome": "PASS"},
                "governor-classify": {"record_id": "record", "classification": "code_defect"},
                "governor-correct": {"unit": "unit", "correction": {}},
                "governor-reconcile": {"record_id": "record"},
            }
            base = {"project": str(project), "objective": "objective", "owner": "owner"}
            with patch("pod.operations.OrcaPort.read_native", return_value=unowned):
                for operation, extra in requests.items():
                    with self.subTest(operation=operation), self.assertRaises(PodError) as caught:
                        helper_run(operation, {**base, **extra})
                    self.assertEqual(caught.exception.code, "native_authority_unverified")
                status = helper_run("governor-status", base)
            self.assertEqual(status["schema"], "pod-governor/v2")
            port = FakePort()
            start(project, assessment, capabilities, quotas, frozen, port)
            wrong_runtime = {**unowned, "runtime": "other", "authoritative": True,
                             "owner": "owner"}
            with patch("pod.operations.OrcaPort.read_native", return_value=wrong_runtime), \
                 self.assertRaises(PodError) as changed:
                helper_run("governor", {**base, "action": {}})
            self.assertEqual(changed.exception.code, "native_authority_unverified")

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

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, read, reconcile, record_delivery, reconcile_delivery_item, reserve
from pod.operations import (OrcaPort, _quota_snapshot, acknowledge_delivery, guarded_start,
                            reconcile_launch, reconcile_release, release_once, settle_delivery)
from pod.config import effective, route_identity
from pod.records import packet, source_identity, verify_sources
from pod.internal import run as helper_run
from pod.util import atomic_json
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
        self.quota = None
        self.resource_state = None
        self.started = {}
        self.terminal_visible = True
        self.headless = False
        self.release_observation = "exited"
        self.login_auth = "oauth"
        self.runtime = "runtime"
        self.establish_bucket = ...
        self.establish_account = None
        self.hard_stops_override = None
        self.stopped = False
        self.worker_state = None
        self.worker_stage = None
        self.release_archive = None
        self.descendants_allowed = False
        self.scope = "all"
        self.find_rows = None
        self.deliveries = []
        self.delivery_calls = []

    def establish(self, route, model_policy, *, child_delegation=False):
        observed = {"oauth": "subscription", "api_key": "api"}.get(self.login_auth, "unknown")
        approved = (model_policy or {}).get("billing", "included")
        hard_stops = []
        if approved == "included" and observed != "subscription":
            hard_stops.append("billing_mode_unverified")
        if observed == "api" and approved != "paid":
            hard_stops.append("paid_route_forbidden")
        if not self.assured:
            hard_stops.append("billing_mode_unverified")
        if self.hard_stops_override is not None:
            hard_stops = list(self.hard_stops_override)
        bucket = route.get("bucket") if self.establish_bucket is ... else self.establish_bucket
        account = self.establish_account or route.get("account")
        return {"schema": "pod-route-establishment/v1", "runtime": self.runtime,
                "version": "1.4.206", "executable": "/fixture/orca",
                "route": {"agent": route.get("agent"), "model": route.get("model"),
                          "account": account, "bucket": bucket,
                          "effort": route.get("effort")},
                "controls": {}, "hard_stops": hard_stops, "disclosures": [],
                "login": {"mode": "host_login", "auth": self.login_auth,
                          "subscription": observed == "subscription", "managed_accounts": 0,
                          "identity_digest": None},
                "billing": {"observed": observed, "approved": approved}}

    def read_native(self, owner, *, run=None, route=None, runs=()):
        self.reads += 1
        return {"runtime": self.runtime, "authoritative": True, "owner": owner, "scope": self.scope,
                "complete": True, "workers": self.workers, "cross_host": False,
                "atomic_admission": False, "quota": self.quota,
                "descendants_allowed": self.descendants_allowed}

    def start_worker(self, *, run, task, owner, route, worktree="current"):
        self.starts += 1
        launch = {key: route[key] for key in ("agent", "model", "effort")}
        dispatch = "dispatch" if self.starts == 1 else f"dispatch-{self.starts}"
        self.settled = False
        self.resource_state = None
        self.release_calls = 0
        self.started[dispatch] = {"run": run, "task": task, "launch": launch,
                                  "worktree": worktree}
        return {"runtime": self.runtime, "runId": run, "taskId": task, "dispatchId": dispatch,
                "state": "ready", "launch": {"requested": launch, "effective": launch},
                **self.receipt_override}

    def find_worker(self, *, run, task):
        if self.find_rows is not None:
            return list(self.find_rows)
        return [{"dispatchId": dispatch, "runId": value["run"], "taskId": value["task"]}
                for dispatch, value in self.started.items() if value["task"] == task]

    def wait_delivery(self, *, run, timeout_ms=None, ack=None, types=()):
        self.delivery_calls.append({"run": run, "timeout_ms": timeout_ms, "ack": ack})
        if ack is not None:
            return {"runtime": self.runtime, "acknowledged": ack}
        if not self.deliveries:
            return {"runtime": self.runtime}
        return {"runtime": self.runtime, "delivery": self.deliveries.pop(0)}

    def show_worker(self, dispatch):
        started = self.started.get(dispatch, {"run": "run", "task": "task",
                                              "launch": {"agent": "codex", "model": "gpt-5.6-sol",
                                                         "effort": "high"}})
        launch = dict(started["launch"])
        resource_state = self.resource_state
        if resource_state is None and self.release_calls and self.release_status in ("released", "already_released"):
            resource_state = "released"
        elif resource_state is None and self.release_calls and self.release_status == "retained" and not self.fail_release:
            resource_state = "retained"
        elif resource_state is None:
            resource_state = "not_requested"
        if resource_state in ("released", "already_released"):
            resource = {"ownershipState": "released", "releaseState": "released",
                        "retainedReason": None, "releaseRequestedAt": "2026-09-20T00:00:00+00:00",
                        "releaseCompletedAt": "2026-09-20T00:00:01+00:00", "releaseError": None,
                        "archive": self.release_archive or {"source": "transcript",
                                                            "status": "captured"}}
            terminal = {"handle": f"term-{dispatch}", "connected": False, "writable": False,
                        "exitCause": {"kind": "operator_close"}}
            observation = {"status": "exited", "exactWorker": True}
        elif resource_state == "retained":
            resource = {"ownershipState": "user_owned", "releaseState": "retained",
                        "retainedReason": "user_takeover", "releaseRequestedAt": None,
                        "releaseCompletedAt": None, "releaseError": None,
                        "archive": {"source": None, "status": None}}
            terminal = {"handle": f"term-{dispatch}"}
            observation = {"status": "settled", "exactWorker": True}
        else:
            resource = {"ownershipState": "owned", "releaseState": "not_requested",
                        "retainedReason": None, "releaseRequestedAt": None,
                        "releaseCompletedAt": None, "releaseError": None,
                        "archive": {"source": None, "status": None}}
            terminal = ({"handle": f"term-{dispatch}", "connected": False, "writable": False,
                         "exitCause": {"kind": "operator_close"}} if self.stopped
                        else {"handle": f"term-{dispatch}", "connected": True, "writable": True})
            observation = ({"status": "exited", "exactWorker": True} if self.stopped
                           else {"status": "live", "exactWorker": True})
        if resource_state == "not_requested" and not self.terminal_visible:
            terminal = None
        resource.update({"id": f"resource-{dispatch}", "terminalHandle": f"term-{dispatch}",
                         "worktreeId": "worktree", "originDispatchId": dispatch,
                         "ownerDispatchId": dispatch})
        projection_resource = {"state": resource["ownershipState"], "id": resource["id"],
                               "ownerDispatchId": dispatch, "releaseState": resource["releaseState"],
                               "terminalState": "released" if resource_state in ("released", "already_released")
                               else "active"}
        worker = {"dispatchId": dispatch, "worktreeId": "worktree",
                  "agentTerminalHandle": f"term-{dispatch}",
                  "state": self.worker_state or ("stopped" if self.stopped else
                                                 "succeeded" if self.settled else "ready"),
                  "stage": self.worker_stage or ("process_stopped" if self.stopped else
                                                 "settled" if self.settled else "input_accepted"),
                  "lastError": "turn start was never observed" if self.stopped else None,
                  "startOptions": {"launch": {"requested": dict(launch),
                                                 "effective": dict(launch)}}}
        if self.release_observation == "missing" and resource_state in ("released", "already_released"):
            observation = {"status": "missing", "exactWorker": False}
        elif self.release_observation == "absent" and resource_state in ("released", "already_released"):
            observation = None
        if self.headless:
            worker.pop("agentTerminalHandle")
            resource = None
            terminal = None
            observation = None
            projection_resource = None
        result = {"dispatch": {
                    "id": dispatch, "runId": started["run"], "taskId": started["task"],
                    "status": "failed" if self.stopped else
                              "completed" if self.settled else "dispatched",
                    "lastFailure": "stopped" if self.stopped else None},
                "projection": {"id": f"worker-{dispatch}", "runId": started["run"],
                               "taskId": started["task"], "dispatchId": dispatch,
                               "resource": projection_resource},
                "worker": worker,
                "terminal": terminal, "observation": observation, "terminalResource": resource}
        if self.headless:
            for field in ("terminal", "observation", "terminalResource"):
                result.pop(field)
            result["projection"].pop("resource")
        return {"runtime": self.runtime, "result": result}

    def release_worker(self, dispatch):
        self.release_calls += 1
        if self.fail_release:
            raise PodError("native_release_uncertain", "lost response")
        value = {"runtime": self.runtime, "dispatchId": dispatch, "status": self.release_status}
        if self.release_status in ("release_pending", "release_unknown"):
            value.update({"processAction": "none", "recovery": ["worker-show", dispatch]})
        return value


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
                     "efforts": ["high"], "capabilities": [], "bucket": "shared"}}
    quota = {"account": {"schema": "pod-quota/v1", "provider": "codex", "account": "account",
                         "bucket": "shared", "observed_at": NOW.isoformat(), "source": "supported",
                         "confidence": "observed", "unknowns": [],
                         "windows": [{"name": "hour", "remaining_percent": 60}],
                         "remaining_percent": 60}}
    return project, assessment, caps, quota


def downgrade_worker_binding(root, project):
    context_path = next((root / "state" / "pod").glob("*/context.json"))
    state = read(project, "objective")
    binding = state["effects"]["op"]["native_binding"]
    predecessor = {field: binding[field] for field in
                   ("dispatchId", "workerId", "taskId", "runId")}
    state["effects"]["op"]["native_binding"] = predecessor
    if "dispatch" in state["cleanup"]:
        state["cleanup"]["dispatch"]["binding"] = predecessor
    atomic_json(context_path, state)
    return predecessor


def frozen_for(project, *, sources=None, context=None, actions=None):
    route = {"alias": "sol", "agent": "codex", "model": "gpt-5.6-sol",
             "account": "account", "bucket": "shared", "effort": "high"}
    return packet({"schema": "pod-packet/v1", "objective": "objective", "criteria": ["works"],
                   "responsibility": "writer", "scope": ["notes.txt"],
                   "actions": actions or ["edit"],
                   "candidate": "c", "context": context or [], "dependencies": [], "route": route,
                   "policy_revision": effective(project)["revision"], "plan_revision": "plan",
                   "report_contract": "checks", "sources": sources or []})


class GuardedOperationTests(unittest.TestCase):
    def test_installed_adapter_accepts_structured_release_unknown_exit_one(self):
        payload = {"ok": True, "result": {"dispatchId": "dispatch", "status": "release_unknown",
                                           "processAction": "none", "recovery": "read back worker"},
                   "_meta": {"runtimeId": "runtime"}}
        completed = subprocess.CompletedProcess([], 1, stdout=json.dumps(payload), stderr="")
        with (patch("pod.orca.executable", return_value=Path("orca")),
              patch("pod.orca.subprocess.run", return_value=completed)):
            result = OrcaPort().release_worker("dispatch")
        self.assertEqual(result["status"], "release_unknown")
        self.assertEqual(result["runtime"], "runtime")

    def test_paid_grant_units_are_durably_reserved_before_distinct_starts(self):
        with fixture() as root, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            config = root / "config" / "pod" / "config.yaml"
            config.write_text(config.read_text().replace("billing: included", "billing: paid") + """policy:
  spending_grants:
    - id: paid-once
      action: paid_usage
      account: account
      model: gpt-5.6-sol
      objective: objective
      valid_until: '2026-09-21T00:00:00Z'
      max_units: 1
""")
            port = FixturePort()
            first = guarded_start(project, "objective", owner="owner", run="run", task="task-one",
                                  operation_id="paid-one", assessment=assessment, capabilities=caps,
                                  quotas=quota, occupancy={}, plan_revision="plan", capacity=2,
                                  port=port, now=NOW)
            authorization = first["effect"]["spending_grant"]
            self.assertEqual(authorization["id"], "paid-once")
            self.assertEqual(authorization["units"], 1)
            self.assertTrue(authorization["identity"])
            self.assertTrue(authorization["scope"])
            with self.assertRaises(PodError) as exhausted:
                guarded_start(project, "objective", owner="owner", run="run", task="task-two",
                              operation_id="paid-two", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", capacity=2,
                              port=port, now=NOW)
            self.assertEqual(exhausted.exception.code, "spending_grant_exhausted")
            self.assertEqual(port.starts, 1)
            self.assertNotIn("paid-two", read(project, "objective")["effects"])
            with self.assertRaises(PodError) as replayed:
                guarded_start(project, "objective", owner="owner", run="run", task="task-one",
                              operation_id="paid-one", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", capacity=2,
                              port=port, now=NOW)
            self.assertEqual(replayed.exception.code, "operation_already_recorded")
            self.assertEqual(port.starts, 1)

    def test_contradictory_release_readback_retains_occupancy(self):
        with fixture() as root, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="first", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            port.settled = True
            port.resource_state = "released"
            valid = port.show_worker("dispatch")
            mutations = (
                lambda value: value["result"]["terminalResource"].update({"id": "unbound-resource"}),
                lambda value: value["result"]["terminalResource"].update({"ownershipState": "owned"}),
                lambda value: value["result"]["terminalResource"].update({"releaseError": "release failed"}),
                lambda value: value["result"]["terminal"].update({"connected": True, "writable": True}),
                lambda value: value["result"]["terminalResource"]["archive"].update({"status": "pending"}),
                lambda value: value["result"]["dispatch"].update({"status": "active"}),
                lambda value: value["result"]["worker"].update({"stage": "input_accepted"}),
                lambda value: value["result"]["terminalResource"].update({"releaseCompletedAt": None}),
                lambda value: value["result"]["terminal"].update({"handle": "other-terminal"}),
                lambda value: value["result"]["observation"].update({"status": "live"}),
                lambda value: value["result"]["observation"].update({"exactWorker": False}),
            )
            route = frozen_for(project)["body"]["route"]
            decision = {"status": "usable", "selected": route,
                        "policy_revision": effective(project)["revision"]}
            for index, mutate in enumerate(mutations):
                contradictory = deepcopy(valid)
                mutate(contradictory)
                with self.subTest(index=index), patch.object(port, "show_worker", return_value=contradictory):
                    try:
                        outcome = reconcile_release(project, "objective", owner="owner",
                                                    dispatch="dispatch", port=port)
                    except PodError as held:
                        self.assertEqual(held.code, "release_identity_unverified")
                    else:
                        self.assertEqual(outcome["status"], "confirmed")
                self.assertEqual(read(project, "objective")["cleanup"], {})
                with self.assertRaises(PodError) as occupied:
                    reserve(project, "objective", owner="owner", operation_id=f"replacement-{index}",
                            requested=route, route_decision=decision,
                            establishment=port.establish(route, {"billing": "included"}),
                            native_reader=lambda: port.read_native("owner"), capacity=1,
                            run_id="run", plan_revision="plan", now=NOW)
                self.assertEqual(occupied.exception.code, "capacity_full")
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")
            self.assertEqual(port.release_calls, 0)

    def test_release_requires_the_installed_owned_resource_and_matching_terminal(self):
        with fixture() as root, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="first", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            port.settled = True
            # A terminal that is merely closed no longer blocks ownership, because a
            # coordinator-fenced worker always has one. A terminal belonging to another
            # worker still does.
            for field, value in (("archive", None), ("terminal", {
                    "handle": "term-someone-else", "connected": True, "writable": True})):
                wrong = port.show_worker("dispatch")
                if field == "archive":
                    wrong["result"]["terminalResource"][field] = value
                else:
                    wrong["result"][field] = value
                with self.subTest(field=field), patch.object(port, "show_worker", return_value=wrong):
                    with self.assertRaises(PodError) as held:
                        release_once(project, "objective", owner="owner",
                                     dispatch="dispatch", port=port)
                    self.assertEqual(held.exception.code, "release_identity_unverified")
                self.assertEqual(port.release_calls, 0)
                self.assertEqual(read(project, "objective")["cleanup"], {})
            self.assertEqual(release_once(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)["status"], "retained")
            self.assertEqual(port.release_calls, 1)

    def test_occupancy_state_cleanup_fleet_matrix(self):
        """Both admission consumers honor one unresolved worker identity."""
        cases = [(effect, cleanup, fleet, consumer)
                 for effect in ("reserved", "uncertain", "confirmed")
                 for cleanup in (("none",) if effect != "confirmed" else
                                 ("none", "reserved", "uncertain", "retained",
                                  "released", "already_released"))
                 for fleet in ("omitted", "coarse_released", "active")
                 for consumer in ("objective", "shared_account")]
        for effect_state, cleanup_state, fleet, consumer in cases:
            with self.subTest(effect=effect_state, cleanup=cleanup_state,
                              fleet=fleet, consumer=consumer), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}

                def admit(target, operation, capacity=1):
                    return reserve(project, target, owner="owner", operation_id=operation,
                                   requested=route, route_decision=decision,
                                   establishment=port.establish(route, {"billing": "included"}),
                                   native_reader=lambda: port.read_native("owner"),
                                   capacity=capacity, run_id="run", plan_revision="plan", now=NOW)

                if effect_state == "confirmed":
                    guarded_start(project, "objective", owner="owner", run="run", task="task",
                                  operation_id="first", assessment=assessment, capabilities=caps,
                                  quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                    if cleanup_state != "none":
                        port.settled = True
                        if cleanup_state == "reserved":
                            with patch.object(port, "release_worker", side_effect=KeyboardInterrupt):
                                with self.assertRaises(KeyboardInterrupt):
                                    release_once(project, "objective", owner="owner",
                                                 dispatch="dispatch", port=port)
                        elif cleanup_state == "uncertain":
                            port.fail_release = True
                            with self.assertRaises(PodError):
                                release_once(project, "objective", owner="owner",
                                             dispatch="dispatch", port=port)
                        else:
                            port.release_status = cleanup_state
                            self.assertEqual(release_once(project, "objective", owner="owner",
                                                          dispatch="dispatch", port=port)["status"],
                                             cleanup_state)
                else:
                    admit("objective", "first")
                    if effect_state == "uncertain":
                        reconcile(project, "objective", owner="owner", operation_id="first",
                                  observed=None)
                self.assertEqual(read(project, "objective")["effects"]["first"]["state"], effect_state)
                self.assertEqual(read(project, "objective")["cleanup"].get("dispatch", {}).get("state", "none"),
                                 cleanup_state)
                if fleet != "omitted":
                    port.workers = [{"state": "released" if fleet == "coarse_released" else "occupied",
                                     "account": "account", "objective": "objective", "dispatchId": "dispatch",
                                     "agent": "codex", "bucket": "shared"}]
                port.quota = quota["account"] if consumer == "objective" else None
                target = "objective"
                if consumer == "shared_account":
                    target = "peer"
                    checkpoint(project, target, owner="owner", value=read(project, "objective")["checkpoint"],
                               native={"runtime": "runtime"})
                occupied = cleanup_state not in ("released", "already_released") or fleet == "active"
                if occupied:
                    with self.assertRaises(PodError) as held:
                        admit(target, "second")
                    self.assertEqual(held.exception.code,
                                     "capacity_full" if consumer == "objective" else "unknown_quota_capacity")
                else:
                    self.assertFalse(admit(target, "second")["existing"])

    def test_confirmed_dispatch_deduplicates_native_row(self):
        for cleanup_state in ("none", "retained", "released"):
            with self.subTest(cleanup=cleanup_state), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="first", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                if cleanup_state != "none":
                    port.settled = True
                    port.release_status = cleanup_state
                    release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
                port.workers = [{"state": "occupied", "account": "account", "objective": "objective",
                                 "dispatchId": "dispatch", "agent": "codex", "bucket": "shared"}]
                port.quota = quota["account"]
                route = frozen_for(project)["body"]["route"]
                intent = reserve(project, "objective", owner="owner", operation_id="second",
                                 requested=route, route_decision={"status": "usable", "selected": route,
                                                                   "policy_revision": effective(project)["revision"]},
                                 establishment=port.establish(route, {"billing": "included"}),
                                 native_reader=lambda: port.read_native("owner"), capacity=2,
                                 run_id="run", plan_revision="plan", now=NOW)
                self.assertFalse(intent["existing"])

    def test_unproved_effect_disposition_never_releases_capacity(self):
        for disposition in ("absent", "failed", "unrecognized"):
            with self.subTest(disposition=disposition), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, _, _, _ = inputs(root)
                port = FixturePort()
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}
                def admit(operation):
                    return reserve(project, "objective", owner="owner", operation_id=operation,
                                   requested=route, route_decision=decision,
                                   establishment=port.establish(route, {"billing": "included"}),
                                   native_reader=lambda: port.read_native("owner"),
                                   capacity=2, run_id="run", plan_revision="plan", now=NOW)
                admit("first")
                path = next((root / "state" / "pod").glob("*/context.json"))
                state = read(project, "objective")
                state["effects"]["first"]["state"] = disposition
                atomic_json(path, state)
                with self.assertRaises(PodError) as held:
                    admit("second")
                self.assertEqual(held.exception.code, "effect_disposition_unverified")
                self.assertNotIn("second", read(project, "objective")["effects"])
                state["effects"]["first"]["state"] = "reserved"
                atomic_json(path, state)
                port.workers = [{"state": "unrecognized", "account": "account",
                                 "objective": "objective", "dispatchId": "dispatch"}]
                with self.assertRaises(PodError) as unknown_native:
                    admit("second")
                self.assertEqual(unknown_native.exception.code, "native_occupancy_unverified")

    def test_confirmed_without_cleanup_reconciles_exact_release_read_only(self):
        for fleet in ("omitted", "coarse_released"):
            with self.subTest(fleet=fleet), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="first", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                if fleet == "coarse_released":
                    port.workers = [{"state": "released", "account": "account",
                                     "objective": "objective", "dispatchId": "dispatch",
                                     "agent": "codex", "bucket": "shared"}]
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "confirmed")
                self.assertEqual(read(project, "objective")["cleanup"], {})
                port.resource_state = "released"
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "confirmed")
                self.assertEqual(read(project, "objective")["cleanup"], {})
                port.settled = True
                wrong = port.show_worker("dispatch")
                wrong["result"]["projection"]["workerId"] = "wrong"
                wrong["result"]["projection"]["id"] = "wrong"
                with patch.object(port, "show_worker", return_value=wrong):
                    with self.assertRaises(PodError) as mismatch:
                        reconcile_release(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)
                self.assertEqual(mismatch.exception.code, "release_identity_unverified")
                self.assertEqual(read(project, "objective")["cleanup"], {})
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "released")
                self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "released")
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "released")
                self.assertEqual(port.release_calls, 0)
                route = frozen_for(project)["body"]["route"]
                intent = reserve(project, "objective", owner="owner", operation_id="second",
                                 requested=route, route_decision={"status": "usable", "selected": route,
                                                                   "policy_revision": effective(project)["revision"]},
                                 establishment=port.establish(route, {"billing": "included"}),
                                 native_reader=lambda: port.read_native("owner"), capacity=1,
                                 run_id="run", plan_revision="plan", now=NOW)
                self.assertFalse(intent["existing"])

    def test_read_only_release_requires_unchanged_launch_receipt(self):
        variants = [(side, field) for side in ("requested", "effective")
                    for field in ("agent", "model", "effort")]
        variants += [("missing", part) for part in ("requested", "effective", "launch")]
        for cleanup_state in ("none", "reserved", "uncertain", "retained"):
            for side, field in variants:
                with self.subTest(cleanup=cleanup_state, side=side, field=field), fixture() as root, patch.dict(
                        os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                     "XDG_CONFIG_HOME": str(root / "config")}):
                    project, assessment, caps, quota = inputs(root)
                    port = FixturePort()
                    guarded_start(project, "objective", owner="owner", run="run", task="task",
                                  operation_id="first", assessment=assessment, capabilities=caps,
                                  quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                    port.settled = True
                    if cleanup_state == "reserved":
                        with patch.object(port, "release_worker", side_effect=KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                release_once(project, "objective", owner="owner",
                                             dispatch="dispatch", port=port)
                    elif cleanup_state == "uncertain":
                        port.fail_release = True
                        with self.assertRaises(PodError):
                            release_once(project, "objective", owner="owner",
                                         dispatch="dispatch", port=port)
                    elif cleanup_state == "retained":
                        self.assertEqual(release_once(project, "objective", owner="owner",
                                                      dispatch="dispatch", port=port)["status"], "retained")
                    port.resource_state = "released"
                    release_calls = port.release_calls
                    wrong = port.show_worker("dispatch")
                    launch = wrong["result"]["worker"]["startOptions"]["launch"]
                    if side == "missing":
                        if field == "launch":
                            del wrong["result"]["worker"]["startOptions"]["launch"]
                        else:
                            del launch[field]
                    else:
                        launch[side][field] = {"agent": "claude", "model": "other-model",
                                               "effort": "low"}[field]
                    with patch.object(port, "show_worker", return_value=wrong):
                        with self.assertRaises(PodError) as mismatch:
                            reconcile_release(project, "objective", owner="owner",
                                              dispatch="dispatch", port=port)
                    self.assertEqual(mismatch.exception.code, "release_identity_unverified")
                    self.assertEqual(read(project, "objective")["cleanup"].get("dispatch", {}).get("state", "none"),
                                     cleanup_state)
                    self.assertEqual(port.release_calls, release_calls)
                    route = frozen_for(project)["body"]["route"]
                    with self.assertRaises(PodError) as occupied:
                        reserve(project, "objective", owner="owner", operation_id="second",
                                requested=route, route_decision={"status": "usable", "selected": route,
                                                                 "policy_revision": effective(project)["revision"]},
                                establishment=port.establish(route, {"billing": "included"}),
                                native_reader=lambda: port.read_native("owner"), capacity=1,
                                run_id="run", plan_revision="plan", now=NOW)
                    self.assertEqual(occupied.exception.code, "capacity_full")
                    self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                       dispatch="dispatch", port=port)["status"], "released")
                    self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "released")
                    self.assertEqual(port.release_calls, release_calls)

    def test_source_state_matrix_matches_direct_and_guarded_checks(self):
        expected = {
            ("present", "present"): None,
            ("present", "absent"): "source_absent",
            ("present", "unavailable"): "source_unavailable",
            ("absent", "present"): "source_changed",
            ("absent", "absent"): "source_absent",
            ("absent", "unavailable"): "source_unavailable",
            ("unavailable", "present"): "source_unbound",
            ("unavailable", "absent"): "source_unbound",
            ("unavailable", "unavailable"): "source_unbound",
        }
        for (frozen_state, current_state), code in expected.items():
            with self.subTest(frozen=frozen_state, current=current_state), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                source = project / "notes.txt"
                source.write_text("harmless bound bytes")
                bound = (source_identity(project, "notes.txt") if frozen_state == "present"
                         else {"path": "notes.txt", "state": frozen_state})
                if current_state == "absent":
                    source.unlink()
                observed = {"path": "notes.txt", "state": "unavailable"}
                observation = (patch("pod.records.source_identity", return_value=observed)
                               if current_state == "unavailable"
                               else patch("pod.records.source_identity", wraps=source_identity))
                frozen = frozen_for(project, sources=[bound])
                port = FixturePort()
                with observation:
                    if code is None:
                        verify_sources(project, [bound])
                        self.assertEqual(guarded_start(
                            project, "objective", owner="owner", run="run", task="task",
                            operation_id="op", assessment=assessment, capabilities=caps,
                            quotas=quota, occupancy={}, plan_revision="plan", port=port,
                            now=NOW, frozen_packet=frozen)["status"], "confirmed")
                    else:
                        with self.assertRaises(PodError) as direct:
                            verify_sources(project, [bound])
                        self.assertEqual(direct.exception.code, code)
                        with self.assertRaises(PodError) as guarded:
                            guarded_start(project, "objective", owner="owner", run="run", task="task",
                                          operation_id="op", assessment=assessment, capabilities=caps,
                                          quotas=quota, occupancy={}, plan_revision="plan", port=port,
                                          now=NOW, frozen_packet=frozen)
                        self.assertEqual(guarded.exception.code, code)
                self.assertEqual(port.starts, 1 if code is None else 0)
                self.assertEqual(bool(read(project, "objective")["source_rejections"]),
                                 code in ("source_absent", "source_changed"))

    def test_unavailable_at_freeze_remains_unbound_for_every_current_state(self):
        for current in ("present", "absent", "unavailable"):
            with self.subTest(current=current), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                source = project / "notes.txt"
                if current == "present":
                    source.write_text("harmless newly observed bytes")
                frozen = frozen_for(project, sources=[{"path": "notes.txt", "state": "unavailable"}])
                port = FixturePort()
                kw = dict(owner="owner", run="run", task="task", operation_id="op",
                          assessment=assessment, capabilities=caps, quotas=quota,
                          occupancy={}, plan_revision="plan", port=port, now=NOW,
                          frozen_packet=frozen)
                for operation in ("op", "retry"):
                    with self.assertRaises(PodError) as held:
                        guarded_start(project, "objective", **{**kw, "operation_id": operation})
                    self.assertEqual(held.exception.code, "source_unbound")
                    with self.assertRaises(PodError) as direct:
                        verify_sources(project, frozen["body"]["sources"])
                    self.assertEqual(direct.exception.code, "source_unbound")
                self.assertEqual(read(project, "objective")["source_rejections"], {})
                self.assertEqual(read(project, "objective")["effects"], {})
                self.assertEqual(port.starts, 0)
                if current == "present":
                    rebound = frozen_for(project, sources=[source_identity(project, "notes.txt")])
                    self.assertNotEqual(rebound["packet_id"], frozen["packet_id"])
                    self.assertEqual(guarded_start(project, "objective", **{
                        **kw, "frozen_packet": rebound})["status"], "confirmed")
                    self.assertEqual(port.starts, 1)

    def test_frozen_sources_are_checked_at_guarded_admission(self):
        for case in ("changed", "absent", "bound_absent", "unavailable", "context_changed", "unchanged"):
            with self.subTest(case=case), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                source = project / "notes.txt"
                source.write_text("first harmless placeholder")
                bound = source_identity(project, "notes.txt")
                if case == "bound_absent":
                    source.unlink()
                    frozen = frozen_for(project, sources=[{"path": "notes.txt", "state": "absent"}])
                elif case == "context_changed":
                    frozen = frozen_for(project, context=[{"kind": "instruction", "path": "notes.txt",
                                                           "sha256": bound["sha256"]}])
                    source.write_text("second harmless placeholder")
                else:
                    frozen = frozen_for(project, sources=[bound])
                    if case == "changed":
                        source.write_text("second harmless placeholder")
                    elif case == "absent":
                        source.unlink()
                port = FixturePort()
                kw = dict(owner="owner", run="run", task="task", operation_id="op",
                          assessment=assessment, capabilities=caps, quotas=quota,
                          occupancy={}, plan_revision="plan", port=port, now=NOW,
                          frozen_packet=frozen)
                if case == "unchanged":
                    self.assertEqual(guarded_start(project, "objective", **kw)["status"], "confirmed")
                    self.assertEqual(port.starts, 1)
                    continue
                if case == "unavailable":
                    unavailable = patch("pod.records.source_identity", return_value={
                        "path": "notes.txt", "state": "unavailable"})
                else:
                    unavailable = patch("pod.records.source_identity", wraps=source_identity)
                with unavailable:
                    with self.assertRaises(PodError) as rejected:
                        guarded_start(project, "objective", **kw)
                expected = {"changed": "source_changed", "absent": "source_absent",
                            "bound_absent": "source_absent", "unavailable": "source_unavailable",
                            "context_changed": "source_changed"}[case]
                self.assertEqual(rejected.exception.code, expected)
                self.assertEqual(port.starts, 0)
                self.assertEqual(read(project, "objective")["effects"], {})
                if case != "unavailable":
                    source.write_text("first harmless placeholder")
                    with self.assertRaises(PodError) as sticky:
                        guarded_start(project, "objective", **{**kw, "operation_id": "retry"})
                    self.assertEqual(sticky.exception.code, "source_rejected")
                    self.assertEqual(port.starts, 0)
                else:
                    self.assertEqual(guarded_start(project, "objective", **kw)["status"], "confirmed")
                    self.assertEqual(port.starts, 1)

    def test_settled_terminal_less_release_retained_once(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            route = {"alias": "sol", "agent": "codex", "model": "gpt-5.6-sol",
                     "account": "account", "bucket": "shared", "effort": "high"}
            frozen = packet({"schema": "pod-packet/v1", "objective": "objective", "criteria": ["works"],
                             "responsibility": "writer", "scope": ["src/a.py"], "actions": ["edit"],
                             "candidate": "c", "context": [], "dependencies": [], "route": route,
                             "policy_revision": effective(project)["revision"], "plan_revision": "plan",
                             "report_contract": "checks", "sources": []})
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW,
                          frozen_packet=frozen)
            worker_report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                             "attempt": "dispatch", "candidate": "c", "outcome": "succeeded",
                             "scope": ["src/a.py"], "files": ["outside.py"], "checks": [],
                             "failures": [], "evidence": [], "uncertainty": [], "questions": []}
            self.assertEqual(helper_run("report", {"report": worker_report, "packet": frozen,
                                                   "project": str(project), "objective": "objective",
                                                   "operation_id": "op"})["status"], "reconciliation_required")
            with self.assertRaises(PodError):
                helper_run("report", {"report": {**worker_report, "attempt": "unissued"},
                                      "packet": frozen, "project": str(project), "objective": "objective",
                                      "operation_id": "op"})
            forged = packet({**frozen["body"], "scope": ["outside.py"]})
            with self.assertRaises(PodError):
                helper_run("report", {"report": {**worker_report, "assignment": forged["packet_id"],
                                                 "scope": ["outside.py"]},
                                      "packet": forged, "project": str(project), "objective": "objective",
                                      "operation_id": "op"})
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
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
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
            port.workers = [{"state": "released", "account": "account", "objective": "objective",
                             "dispatchId": "dispatch", "agent": "codex", "bucket": "shared"}]
            next_start = dict(owner="owner", run="run", task="task", operation_id="op-2",
                              assessment=assessment, capabilities=caps, quotas=quota,
                              occupancy={}, plan_revision="plan", port=port, now=NOW, capacity=1)
            with self.assertRaises(PodError) as full:
                guarded_start(project, "objective", **next_start)
            self.assertEqual(full.exception.code, "capacity_full")
            self.assertEqual(port.starts, 1)
            checkpoint(project, "second", owner="owner", value={
                "schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
                "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
                "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"},
                native={"runtime": "runtime"})
            with self.assertRaises(PodError) as shared:
                guarded_start(project, "second", **{**next_start, "quotas": {}})
            self.assertEqual(shared.exception.code, "unknown_quota_capacity")
            self.assertEqual(port.starts, 1)
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "uncertain")
            port.release_status = "released"
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")
            self.assertEqual(port.release_calls, 1)
            self.assertEqual(guarded_start(project, "objective", **next_start)["status"], "confirmed")
            self.assertEqual(port.starts, 2)

    def test_retained_cleanup_occupies_until_exact_release_readback(self):
        for fleet in ("omitted", "coarse_released", "active"):
            with self.subTest(fleet=fleet), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                common = dict(owner="owner", run="run", task="task", assessment=assessment,
                              capabilities=caps, quotas=quota, occupancy={}, plan_revision="plan",
                              port=port, now=NOW)
                guarded_start(project, "objective", operation_id="op", **common)
                port.settled = True
                self.assertEqual(release_once(project, "objective", owner="owner",
                                              dispatch="dispatch", port=port)["status"], "retained")
                if fleet != "omitted":
                    port.workers = [{"state": "released" if fleet == "coarse_released" else "occupied",
                                     "account": "account", "objective": "objective",
                                     "dispatchId": "dispatch", "agent": "codex", "bucket": "shared"}]
                with self.assertRaises(PodError) as full:
                    guarded_start(project, "objective", operation_id="op-2", capacity=1, **common)
                self.assertEqual(full.exception.code, "capacity_full")
                checkpoint(project, "second", owner="owner", value={
                    "schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
                    "candidate": "c", "policy_revision": "r", "native_refs": [], "assignments": [],
                    "questions": [], "verification_gaps": ["works"], "next_safe_action": "inspect"},
                    native={"runtime": "runtime"})
                with self.assertRaises(PodError) as shared:
                    guarded_start(project, "second", operation_id="other", capacity=2,
                                  **{**common, "quotas": {}})
                self.assertEqual(shared.exception.code, "unknown_quota_capacity")
                self.assertEqual(port.starts, 1)
                wrong = port.show_worker("dispatch")
                wrong["result"]["projection"]["workerId"] = "other-worker"
                wrong["result"]["projection"]["id"] = "other-worker"
                with patch.object(port, "show_worker", return_value=wrong):
                    with self.assertRaises(PodError) as mismatch:
                        reconcile_release(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)
                self.assertEqual(mismatch.exception.code, "release_identity_unverified")
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "retained")
                self.assertEqual(port.release_calls, 1)
                if fleet == "active":
                    port.quota = quota["account"]
                    route = frozen_for(project)["body"]["route"]
                    decision = {"status": "usable", "selected": route,
                                "policy_revision": effective(project)["revision"]}
                    intent = reserve(project, "objective", owner="owner", operation_id="op-2",
                                     requested=route, route_decision=decision,
                                     establishment=port.establish(route, {"billing": "included"}),
                                     native_reader=lambda: port.read_native("owner"), capacity=2,
                                     run_id="run", plan_revision="plan", now=NOW)
                    self.assertFalse(intent["existing"])
                else:
                    port.resource_state = "released"
                    self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                       dispatch="dispatch", port=port)["status"], "released")
                    self.assertEqual(port.release_calls, 1)
                    self.assertEqual(guarded_start(project, "objective", operation_id="op-2",
                                                   capacity=1, **common)["status"], "confirmed")

    def test_pending_release_reconciles_positive_retained_readback(self):
        for pending in ("reserved", "uncertain"):
            with self.subTest(pending=pending), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                port.settled = True
                if pending == "reserved":
                    with patch.object(port, "release_worker", side_effect=KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
                else:
                    port.fail_release = True
                    with self.assertRaises(PodError):
                        release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
                port.resource_state = "retained"
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "retained")
                self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "retained")
                self.assertEqual(port.release_calls, 1 if pending == "uncertain" else 0)

    def test_reserved_release_reconciles_exact_readback_and_deduplicates_capacity(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            port.settled = True
            with patch.object(port, "release_worker", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "reserved")
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "reserved")
            wrong = port.show_worker("dispatch")
            wrong["result"]["projection"]["runId"] = "other-run"
            with patch.object(port, "show_worker", return_value=wrong):
                with self.assertRaises(PodError) as mismatch:
                    reconcile_release(project, "objective", owner="owner",
                                      dispatch="dispatch", port=port)
            self.assertEqual(mismatch.exception.code, "release_identity_unverified")
            self.assertEqual(read(project, "objective")["cleanup"]["dispatch"]["state"], "reserved")
            port.workers = [{"state": "occupied", "account": "account", "objective": "objective",
                             "dispatchId": "dispatch", "agent": "codex", "bucket": "shared"}]
            port.quota = quota["account"]
            decision = {"status": "usable", "selected": frozen_for(project)["body"]["route"],
                        "policy_revision": effective(project)["revision"]}
            route = decision["selected"]
            intent = reserve(project, "objective", owner="owner", operation_id="op-2", requested=route,
                             route_decision=decision, establishment=port.establish(route, {"billing": "included"}),
                             native_reader=lambda: port.read_native("owner"), capacity=2, run_id="run",
                             plan_revision="plan", now=NOW)
            self.assertFalse(intent["existing"])
            port.release_calls = 1
            port.release_status = "released"
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")

    def test_complete_guarded_path_without_terminal_and_no_repeat(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.headless = True
            kw = dict(owner="owner", run="run", task="task", operation_id="op",
                      assessment=assessment, capabilities=caps, quotas=quota,
                      occupancy={}, plan_revision="plan", port=port, now=NOW)
            result = guarded_start(project, "objective", **kw)
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(result["effect"]["native_binding"]["workerId"], "worker-dispatch")
            self.assertIsNone(result["effect"]["native_binding"]["terminalHandle"])
            self.assertIsNone(result["effect"]["native_binding"]["terminalResourceId"])
            self.assertEqual(port.starts, 1)
            self.assertEqual(port.reads, 1)
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **kw)
            self.assertEqual(port.starts, 1)
            port.settled = True
            contradictory = port.show_worker("dispatch")
            contradictory["result"]["terminal"] = {
                "handle": None, "connected": False, "writable": False,
                "exitCause": {"kind": "operator_close"},
            }
            with patch.object(port, "show_worker", return_value=contradictory):
                with self.assertRaises(PodError) as mismatch:
                    reconcile_release(project, "objective", owner="owner",
                                      dispatch="dispatch", port=port)
            self.assertEqual(mismatch.exception.code, "release_identity_unverified")
            self.assertEqual(release_once(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)["status"], "retained")
            with self.assertRaises(PodError):
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(port.release_calls, 1)

    def test_current_failed_settlement_and_headless_release_shapes_reconcile_read_only(self):
        for observation in ("missing", "absent"):
            with self.subTest(observation=observation), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                port.settled = True
                port.resource_state = "released"
                port.release_observation = observation
                shown = port.show_worker("dispatch")
                shown["result"]["dispatch"]["status"] = "failed"
                failure = {
                    "provenance": "worker_report", "outcome": "failed", "messageId": "message",
                    "reportedBy": "term-dispatch", "completedAt": NOW.isoformat(),
                }
                shown["result"]["dispatch"]["lastFailure"] = (
                    json.dumps(failure) if observation == "missing" else failure)
                shown["result"]["worker"].update({"state": "failed", "stage": "settled",
                                                    "lastError": None})
                shown["result"]["terminal"] = None
                shown["result"]["terminalResource"]["archive"]["source"] = "terminal"
                if observation == "absent":
                    shown["result"].pop("observation")
                with patch.object(port, "show_worker", return_value=shown):
                    result = reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)
                self.assertEqual(result["status"], "released")
                self.assertEqual(port.release_calls, 0)

    def test_release_pending_and_unknown_are_durable_read_only_recovery_states(self):
        for disposition in ("release_pending", "release_unknown"):
            with self.subTest(disposition=disposition), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                port.settled = True
                port.release_status = disposition
                self.assertEqual(release_once(project, "objective", owner="owner",
                                              dispatch="dispatch", port=port)["status"], disposition)
                cleanup = read(project, "objective")["cleanup"]["dispatch"]
                self.assertEqual(cleanup["state"], disposition)
                self.assertFalse(cleanup["repeat_allowed"])
                self.assertTrue(cleanup["recovery"])
                with self.assertRaises(PodError):
                    release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], disposition)
                port.resource_state = "released"
                port.release_observation = "missing"
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "released")
                self.assertEqual(port.release_calls, 1)

    def test_predecessor_binding_stays_occupied_and_rebinds_exactly_without_effect_replay(self):
        with fixture() as root, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            context_path = next((root / "state" / "pod").glob("*/context.json"))
            state = read(project, "objective")
            binding = state["effects"]["op"]["native_binding"]
            predecessor = {field: binding[field] for field in
                           ("dispatchId", "workerId", "taskId", "runId")}
            state["effects"]["op"]["native_binding"] = predecessor
            atomic_json(context_path, state)
            route = frozen_for(project)["body"]["route"]
            decision = {"status": "usable", "selected": route,
                        "policy_revision": effective(project)["revision"]}
            with self.assertRaises(PodError) as occupied:
                reserve(project, "objective", owner="owner", operation_id="replacement",
                        requested=route, route_decision=decision,
                        establishment=port.establish(route, {"billing": "included"}),
                        native_reader=lambda: port.read_native("owner"), capacity=1,
                        run_id="run", plan_revision="plan", now=NOW)
            self.assertEqual(occupied.exception.code, "capacity_full")
            port.settled = True
            port.resource_state = "released"
            contradictory = port.show_worker("dispatch")
            contradictory["result"]["projection"]["id"] = "other-worker"
            with patch.object(port, "show_worker", return_value=contradictory):
                with self.assertRaises(PodError) as mismatch:
                    reconcile_release(project, "objective", owner="owner",
                                      dispatch="dispatch", port=port)
            self.assertEqual(mismatch.exception.code, "release_identity_unverified")
            self.assertEqual(read(project, "objective")["effects"]["op"]["native_binding"], predecessor)
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")
            upgraded = read(project, "objective")["effects"]["op"]["native_binding"]
            self.assertEqual(upgraded["worktreeId"], "worktree")
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")
            self.assertEqual(port.starts, 1)
            self.assertEqual(port.release_calls, 0)

    def test_predecessor_cleanup_release_labels_hold_until_exact_rebind(self):
        for disposition in ("released", "already_released"):
            with self.subTest(disposition=disposition), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                port.settled = True
                port.release_status = disposition
                self.assertEqual(release_once(project, "objective", owner="owner",
                                              dispatch="dispatch", port=port)["status"], disposition)
                predecessor = downgrade_worker_binding(root, project)
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}
                reserve_args = dict(project=project, objective="objective", owner="owner",
                                    operation_id="replacement", requested=route,
                                    route_decision=decision,
                                    establishment=port.establish(route, {"billing": "included"}),
                                    native_reader=lambda: port.read_native("owner"), capacity=1,
                                    run_id="run", plan_revision="plan", now=NOW)
                with self.assertRaises(PodError) as occupied:
                    reserve(**reserve_args)
                self.assertEqual(occupied.exception.code, "capacity_full")
                before = read(project, "objective")
                contradictory = port.show_worker("dispatch")
                contradictory["result"]["projection"]["id"] = "other-worker"
                with patch.object(port, "show_worker", return_value=contradictory):
                    with self.assertRaises(PodError) as mismatch:
                        reconcile_release(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)
                self.assertEqual(mismatch.exception.code, "release_identity_unverified")
                self.assertEqual(read(project, "objective"), before)
                result = reconcile_release(project, "objective", owner="owner",
                                           dispatch="dispatch", port=port)
                self.assertEqual(result["status"], disposition)
                upgraded = read(project, "objective")
                effect_binding = upgraded["effects"]["op"]["native_binding"]
                cleanup_binding = upgraded["cleanup"]["dispatch"]["binding"]
                self.assertNotEqual(effect_binding, predecessor)
                self.assertEqual(cleanup_binding, effect_binding)
                self.assertEqual(reserve(**reserve_args)["state"], "reserved")
                self.assertEqual(port.starts, 1)
                self.assertEqual(port.release_calls, 1)

    def test_predecessor_pending_and_unknown_cleanup_rebind_without_repeating_release(self):
        for disposition in ("release_pending", "release_unknown"):
            with self.subTest(disposition=disposition), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
                port.settled = True
                port.release_status = disposition
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
                downgrade_worker_binding(root, project)
                first = reconcile_release(project, "objective", owner="owner",
                                          dispatch="dispatch", port=port)
                self.assertEqual(first["status"], disposition)
                rebound = read(project, "objective")
                self.assertEqual(rebound["effects"]["op"]["native_binding"],
                                 rebound["cleanup"]["dispatch"]["binding"])
                self.assertIn("worktreeId", rebound["effects"]["op"]["native_binding"])
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}
                with self.assertRaises(PodError) as occupied:
                    reserve(project, "objective", owner="owner", operation_id="replacement",
                            requested=route, route_decision=decision,
                            establishment=port.establish(route, {"billing": "included"}),
                            native_reader=lambda: port.read_native("owner"), capacity=1,
                            run_id="run", plan_revision="plan", now=NOW)
                self.assertEqual(occupied.exception.code, "capacity_full")
                port.resource_state = "released"
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "released")
                self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                                   dispatch="dispatch", port=port)["status"], "released")
                self.assertEqual(port.starts, 1)
                self.assertEqual(port.release_calls, 1)

    def test_malformed_predecessor_binding_remains_blocked(self):
        with fixture() as root, patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            context_path = next((root / "state" / "pod").glob("*/context.json"))
            state = read(project, "objective")
            state["effects"]["op"]["native_binding"] = {
                "dispatchId": "dispatch", "workerId": "worker-dispatch", "taskId": "task",
            }
            atomic_json(context_path, state)
            route = frozen_for(project)["body"]["route"]
            decision = {"status": "usable", "selected": route,
                        "policy_revision": effective(project)["revision"]}
            with self.assertRaises(PodError) as blocked:
                reserve(project, "objective", owner="owner", operation_id="replacement",
                        requested=route, route_decision=decision,
                        establishment=port.establish(route, {"billing": "included"}),
                        native_reader=lambda: port.read_native("owner"), capacity=2,
                        run_id="run", plan_revision="plan", now=NOW)
            self.assertEqual(blocked.exception.code, "effect_identity_unverified")
            self.assertEqual(port.starts, 1)

    def test_worker_done_delivery_requires_bound_settlement_and_disposition(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            delivery = {"id": "delivery", "runtime": "runtime", "runId": "run", "messages": [
                {"id": "done", "type": "worker_done", "runId": "run", "taskId": "task", "dispatchId": "dispatch"}]}
            self.assertFalse(record_delivery(project, "objective", owner="owner", delivery=delivery)["ack_eligible"])
            observed = {"runtime": "runtime", "runId": "run", "messageId": "done", "taskId": "task",
                        "dispatchId": "dispatch", "kind": "settlement", "status": "completed", "receiptId": "native-done"}
            with self.assertRaises(PodError):
                reconcile_delivery_item(project, "objective", owner="owner", delivery_id="delivery",
                                        message_id="done", native_reader=lambda: observed)
            port.settled = True
            release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            reconcile_delivery_item(project, "objective", owner="owner", delivery_id="delivery",
                                    message_id="done", native_reader=lambda: observed)
            self.assertTrue(record_delivery(project, "objective", owner="owner", delivery=delivery)["ack_eligible"])

    def test_prelaunch_denial_and_unknown_effect_do_not_retry(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
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

    def test_unestablished_route_blocks_before_any_native_effect(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            start = dict(owner="owner", run="run", task="task", operation_id="op",
                         assessment=assessment, capabilities=caps, quotas=quota, occupancy={},
                         plan_revision="plan", now=NOW)
            cases = {
                "account_binding_unverified": lambda port: setattr(port, "establish_account", "other"),
                "native_authority_unverified": lambda port: setattr(port, "runtime", None),
                "billing_mode_unverified": lambda port: setattr(port, "login_auth", "unknown"),
                "paid_route_forbidden": lambda port: setattr(port, "hard_stops_override",
                                                             ["paid_route_forbidden"]),
            }
            for code, arrange in cases.items():
                with self.subTest(code=code):
                    port = FixturePort()
                    arrange(port)
                    with self.assertRaises(PodError) as caught:
                        guarded_start(project, "objective",
                                      **{**start, "operation_id": "op-" + code, "port": port})
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(port.reads, 0)
                    self.assertEqual(port.starts, 0)

    def test_included_route_with_unknown_bucket_is_established(self):
        """A supported subscription route is not blocked by absent optional metadata."""
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            caps["sol"].pop("bucket")
            quota = {}
            port = FixturePort()
            port.establish_bucket = None
            result = guarded_start(project, "objective", owner="owner", run="run", task="task",
                                   operation_id="op", assessment=assessment, capabilities=caps,
                                   quotas=quota, occupancy={}, plan_revision="plan", port=port,
                                   now=NOW)
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(result["establishment"]["hard_stops"], [])
            self.assertEqual(port.starts, 1)

    def test_fresh_native_read_and_shared_account_occupancy(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
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
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            grant = {"id": "capacity-one", "action": "exceptional_capacity", "account": "account",
                     "objective": "objective", "run": "run", "plan_revision": "plan", "limit": 4,
                     "reason": "independent edits", "valid_until": "2026-09-21T00:00:00Z"}
            base = dict(owner="owner", run="run", task="task", operation_id="op",
                        assessment=assessment, capabilities=caps, quotas=quota,
                        occupancy={}, plan_revision="plan", port=port, now=NOW, capacity=4)
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **base, exceptional_grant=grant)
            self.assertEqual(port.starts, 0)
            config = root / "config" / "pod" / "config.yaml"
            config.write_text(config.read_text() + "policy:\n  exceptional_grants:\n    - id: capacity-one\n      action: exceptional_capacity\n      account: account\n      objective: objective\n      run: run\n      plan_revision: plan\n      limit: 4\n      reason: independent edits\n      valid_until: '2026-09-21T00:00:00Z'\n")
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **base,
                              exceptional_grant={**grant, "run": "other"})
            with self.assertRaises(PodError):
                guarded_start(project, "objective", **base,
                              exceptional_grant={**grant, "valid_until": "2027-09-21T00:00:00Z"})
            (project / ".pod").mkdir()
            local = project / ".pod" / "config.yaml"
            local.write_text("schema: pod/v1\npolicy: {max_workers: 2}\n")
            with self.assertRaises(PodError) as local_limit:
                guarded_start(project, "objective", **base, exceptional_grant=grant)
            self.assertEqual(local_limit.exception.code, "capacity_ceiling")
            local.unlink()
            self.assertEqual(guarded_start(project, "objective", **base,
                             exceptional_grant=grant)["status"], "confirmed")

    def test_ordinary_three_needs_reason_and_respects_personal_hard_ceiling(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            base = dict(owner="owner", run="run", task="task", operation_id="op",
                        assessment=assessment, capabilities=caps, quotas=quota,
                        occupancy={}, plan_revision="plan", port=port, now=NOW, capacity=3)
            with self.assertRaises(PodError) as missing:
                guarded_start(project, "objective", **base)
            self.assertEqual(missing.exception.code, "capacity_reason_required")
            config = root / "config" / "pod" / "config.yaml"
            original = config.read_text()
            config.write_text(original + "policy: {max_workers: 2}\n")
            with self.assertRaises(PodError) as ceiling:
                guarded_start(project, "objective", **base, capacity_reason="three independent edits")
            self.assertEqual(ceiling.exception.code, "capacity_ceiling")
            config.write_text(original)
            self.assertEqual(guarded_start(project, "objective", **base,
                                           capacity_reason="three independent edits")["status"], "confirmed")


class DelegationPathTests(unittest.TestCase):
    """The production delegation path: launch, recover, count, settle, release."""

    def prepared(self, root):
        return inputs(root)

    def start(self, project, assessment, caps, quota, port, **extra):
        arguments = dict(owner="owner", run="run", task="task", operation_id="op",
                         assessment=assessment, capabilities=caps, quotas=quota, occupancy={},
                         plan_revision="plan", port=port, now=NOW)
        arguments.update(extra)
        return guarded_start(project, "objective", **arguments)

    def test_a_structured_failure_occupies_capacity_and_is_never_relaunched(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.receipt_override = {"state": "failed", "failedStage": "dispatch_input",
                                     "residualResources": [{"kind": "terminal"}],
                                     "mutation": {"requestId": "req-1"}}
            result = self.start(project, assessment, caps, quota, port)
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(result["native_failure"]["failedStage"], "dispatch_input")
            effect = read(project, "objective")["effects"]["op"]
            self.assertEqual(effect["state"], "uncertain")
            self.assertEqual(effect["native_failure"]["mutation"], {"requestId": "req-1"})
            with self.assertRaises(PodError) as repeated:
                self.start(project, assessment, caps, quota, port)
            self.assertEqual(repeated.exception.code, "operation_already_recorded")
            self.assertEqual(port.starts, 1)

    def test_a_lost_response_is_recovered_by_readback_not_by_starting_again(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            original = port.start_worker

            def lost(**kwargs):
                original(**kwargs)
                raise PodError("native_effect_uncertain", "response never arrived")

            port.start_worker = lost
            with self.assertRaises(PodError):
                self.start(project, assessment, caps, quota, port)
            self.assertEqual(read(project, "objective")["effects"]["op"]["state"], "uncertain")
            port.start_worker = original
            adopted = reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                       run="run", task="task", port=port)
            self.assertEqual(adopted["action"], "adopted")
            self.assertEqual(adopted["status"], "confirmed")
            self.assertEqual(adopted["dispatchId"], "dispatch")
            self.assertEqual(port.starts, 1)

    def test_an_absent_row_holds_the_slot_and_two_rows_refuse(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.start_worker = lambda **kwargs: (_ for _ in ()).throw(
                PodError("native_effect_uncertain", "lost"))
            with self.assertRaises(PodError):
                self.start(project, assessment, caps, quota, port)
            port.find_rows = []
            held = reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                    run="run", task="task", port=port)
            self.assertEqual(held["action"], "hold")
            self.assertEqual(held["status"], "uncertain")
            port.find_rows = [{"dispatchId": "one", "taskId": "task"},
                              {"dispatchId": "two", "taskId": "task"}]
            with self.assertRaises(PodError) as ambiguous:
                reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                 run="run", task="task", port=port)
            self.assertEqual(ambiguous.exception.code, "native_occupancy_unverified")

    def test_a_worker_this_coordinator_never_launched_does_not_block_admission(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.workers = [{"state": "retained", "dispatchId": "ctx_foreign", "foreign": True,
                             "account": None, "objective": None},
                            {"state": "uncertain", "dispatchId": "ctx_other", "foreign": True,
                             "account": None, "objective": None}]
            result = self.start(project, assessment, caps, quota, port)
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(port.starts, 1)

    def test_a_descendant_counts_and_is_refused_when_policy_forbids_it(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.workers = [{"state": "active", "dispatchId": "ctx_child", "descendant": True,
                             "parent": "ctx_parent", "account": "account", "objective": "objective",
                             "agent": "codex", "bucket": "shared"}]
            with self.assertRaises(PodError) as refused:
                self.start(project, assessment, caps, quota, port)
            self.assertEqual(refused.exception.code, "unauthorized_descendant")
            self.assertEqual(port.starts, 0)

    def test_a_descendant_occupies_objective_capacity_once_allowed(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.descendants_allowed = True
            port.workers = [{"state": "active", "dispatchId": "ctx_child", "descendant": True,
                             "parent": "ctx_parent", "account": "account", "objective": "objective",
                             "agent": "codex", "bucket": "shared"}]
            with self.assertRaises(PodError) as full:
                self.start(project, assessment, caps, quota, port, capacity=1)
            self.assertEqual(full.exception.code, "capacity_full")

    def test_delegating_from_a_packet_needs_explicit_policy(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            (project / "notes.txt").write_text("value = 1\n")
            sources = [{"path": "notes.txt", "state": "present",
                        "sha256": source_identity(project, "notes.txt")["sha256"]}]
            frozen = frozen_for(project, sources=sources, actions=["delegate"])
            port = FixturePort()
            with self.assertRaises(PodError) as refused:
                self.start(project, assessment, caps, quota, port, frozen_packet=frozen)
            self.assertEqual(refused.exception.code, "delegation_unauthorized")
            self.assertEqual(port.starts, 0)

    def test_worker_placement_refuses_a_creation_mode(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            for placement in ("new-child", "new-top-level", "path:"):
                with self.subTest(placement=placement):
                    with self.assertRaises(PodError) as caught:
                        self.start(project, assessment, caps, quota, port, worktree=placement)
                    self.assertEqual(caught.exception.code, "invalid_worktree_selector")
            self.assertEqual(port.starts, 0)
            placed = self.start(project, assessment, caps, quota, port,
                                worktree="path:/fixture/repo")
            self.assertEqual(placed["status"], "confirmed")
            self.assertEqual(port.started["dispatch"]["worktree"], "path:/fixture/repo")

    def test_a_settlement_delivery_releases_once_and_acknowledges_after(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.release_status = "released"
            self.start(project, assessment, caps, quota, port)
            port.settled = True
            port.deliveries = [{"id": "delivery-1", "messages": [
                {"id": "message-1", "type": "worker_done",
                 "payload": json.dumps({"runId": "run", "taskId": "task",
                                        "dispatchId": "dispatch"})}]}]
            settled = settle_delivery(project, "objective", owner="owner", run="run",
                                      timeout_ms=60000, port=port)
            self.assertEqual(settled["status"], "recorded")
            self.assertTrue(settled["ack_eligible"])
            self.assertEqual(settled["settled"][0]["dispatchId"], "dispatch")
            self.assertEqual(port.release_calls, 1)
            acknowledged = acknowledge_delivery(project, "objective", owner="owner", run="run",
                                                delivery_id="delivery-1", port=port)
            self.assertEqual(acknowledged["status"], "acknowledged")
            self.assertEqual(port.delivery_calls[-1]["ack"], "delivery-1")
            state = read(project, "objective")
            self.assertEqual(state["cleanup"]["dispatch"]["state"], "released")
            self.assertIsNotNone(state["deliveries"]["delivery-1"]["items"]["message-1"]["effect"])

    def test_an_unresolved_delivery_cannot_be_acknowledged(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            self.start(project, assessment, caps, quota, port)
            port.deliveries = [{"id": "delivery-2", "messages": [
                {"id": "message-2", "type": "question",
                 "payload": json.dumps({"runId": "run"})}]}]
            settled = settle_delivery(project, "objective", owner="owner", run="run", port=port)
            self.assertFalse(settled["ack_eligible"])
            with self.assertRaises(PodError) as caught:
                acknowledge_delivery(project, "objective", owner="owner", run="run",
                                     delivery_id="delivery-2", port=port)
            self.assertEqual(caught.exception.code, "delivery_unresolved")
            self.assertFalse([call for call in port.delivery_calls if call["ack"]])

    def test_an_empty_mailbox_records_nothing(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            self.start(project, assessment, caps, quota, port)
            empty = settle_delivery(project, "objective", owner="owner", run="run", port=port)
            self.assertEqual(empty["status"], "empty")
            self.assertFalse(read(project, "objective")["deliveries"])


class FencedWorkerRecoveryTests(unittest.TestCase):
    """A worker the coordinator stopped is a settled outcome, not a leak.

    Every shape here was taken from a live Orca 1.4.206 fence: the agent never proved a
    turn start, the coordinator stopped it, and its terminal was closed before it wrote a
    transcript. Pod must be able to adopt, settle and release that worker exactly once.
    """

    def fenced(self, port, dispatch="dispatch"):
        port.settled = True
        port.stopped = True
        port.resource_state = "not_requested"
        port.release_status = "released"
        port.release_archive = {"source": None, "status": "unavailable"}
        return dispatch

    def prepared(self, root):
        return inputs(root)

    def test_a_stopped_worker_is_adopted_settled_and_released_once(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.start_worker = lambda **kwargs: (_ for _ in ()).throw(
                PodError("native_effect_uncertain", "turn start was never observed"))
            with self.assertRaises(PodError):
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port,
                              now=NOW)
            self.assertEqual(read(project, "objective")["effects"]["op"]["state"], "uncertain")
            live = FixturePort()
            live.started = {"dispatch": {"run": "run", "task": "task",
                                         "launch": {"agent": "codex", "model": "gpt-5.6-sol",
                                                    "effort": "high"},
                                         "worktree": "current"}}
            live.find_rows = [{"dispatchId": "dispatch", "runId": "run", "taskId": "task"}]
            self.fenced(live)
            adopted = reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                       run="run", task="task", port=live)
            self.assertEqual(adopted["action"], "adopted")
            self.assertEqual(adopted["status"], "confirmed")
            live.resource_state = None
            released = release_once(project, "objective", owner="owner", dispatch="dispatch",
                                    port=live)
            self.assertEqual(released["status"], "released")
            self.assertEqual(live.release_calls, 1)
            with self.assertRaises(PodError) as repeated:
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=live)
            self.assertEqual(repeated.exception.code, "release_already_recorded")
            self.assertEqual(live.release_calls, 1)

    def test_a_worker_that_never_started_is_not_adopted(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            port.start_worker = lambda **kwargs: (_ for _ in ()).throw(
                PodError("native_effect_uncertain", "lost"))
            with self.assertRaises(PodError):
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port,
                              now=NOW)
            unobserved = FixturePort()
            unobserved.started = {"dispatch": {"run": "run", "task": "task",
                                               "launch": {"agent": "codex", "model": "gpt-5.6-sol",
                                                          "effort": "high"},
                                               "worktree": "current"}}
            unobserved.find_rows = [{"dispatchId": "dispatch", "runId": "run", "taskId": "task"}]
            unobserved.worker_state = "start_unknown"
            unobserved.worker_stage = "turn_start_unobserved"
            with self.assertRaises(PodError) as held:
                reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                 run="run", task="task", port=unobserved)
            self.assertEqual(held.exception.code, "native_start_unsettled")
            self.assertEqual(read(project, "objective")["effects"]["op"]["state"], "uncertain")

    def test_an_unresolved_archive_still_blocks_a_release_claim(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = self.prepared(root)
            port = FixturePort()
            guarded_start(project, "objective", owner="owner", run="run", task="task",
                          operation_id="op", assessment=assessment, capabilities=caps,
                          quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            self.fenced(port)
            port.release_archive = {"source": None, "status": None}
            with self.assertRaises(PodError) as caught:
                release_once(project, "objective", owner="owner", dispatch="dispatch", port=port)
            self.assertEqual(caught.exception.code, "native_release_uncertain")


class ReviewFindingRegressions(unittest.TestCase):
    """Each of these reproduces a defect an independent review found."""

    def start(self, project, assessment, caps, quota, port, **extra):
        arguments = dict(owner="owner", run="run", task="task", operation_id="op",
                         assessment=assessment, capabilities=caps, quotas=quota, occupancy={},
                         plan_revision="plan", port=port, now=NOW)
        arguments.update(extra)
        return guarded_start(project, "objective", **arguments)

    def test_recovery_cannot_adopt_a_worker_from_another_run(self):
        """`run` and `task` arrive in caller JSON; the effect records the Run it holds.

        Without that join, naming a different Run bound the effect to a Dispatch Pod never
        started, and a later release would have acted on someone else's worker.
        """
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.start_worker = lambda **kwargs: (_ for _ in ()).throw(
                PodError("native_effect_uncertain", "lost"))
            with self.assertRaises(PodError):
                self.start(project, assessment, caps, quota, port)
            foreign = FixturePort()
            foreign.started = {"other": {"run": "other-run", "task": "other-task",
                                         "launch": {"agent": "codex", "model": "gpt-5.6-sol",
                                                    "effort": "high"},
                                         "worktree": "current"}}
            foreign.find_rows = [{"dispatchId": "other", "runId": "other-run",
                                  "taskId": "other-task"}]
            with self.assertRaises(PodError) as caught:
                reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                 run="other-run", task="other-task", port=foreign)
            self.assertEqual(caught.exception.code, "effect_identity_mismatch")
            self.assertEqual(read(project, "objective")["effects"]["op"]["state"], "uncertain")

    def test_recovery_refuses_a_readback_that_names_a_different_run(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.start_worker = lambda **kwargs: (_ for _ in ()).throw(
                PodError("native_effect_uncertain", "lost"))
            with self.assertRaises(PodError):
                self.start(project, assessment, caps, quota, port)
            lying = FixturePort()
            lying.started = {"dispatch": {"run": "elsewhere", "task": "task",
                                          "launch": {"agent": "codex", "model": "gpt-5.6-sol",
                                                     "effort": "high"},
                                          "worktree": "current"}}
            lying.find_rows = [{"dispatchId": "dispatch", "runId": "run", "taskId": "task"}]
            with self.assertRaises(PodError) as caught:
                reconcile_launch(project, "objective", owner="owner", operation_id="op",
                                 run="run", task="task", port=lying)
            self.assertEqual(caught.exception.code, "native_identity_unverified")

    def test_a_replayed_delivery_settles_the_rest_of_its_batch(self):
        """Orca replays an unacknowledged Delivery, so the natural retry must work.

        Raising on an already-released dispatch stranded every item behind it.
        """
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.release_status = "released"
            self.start(project, assessment, caps, quota, port)
            port.settled = True
            batch = {"id": "delivery-1", "messages": [
                {"id": "message-1", "type": "worker_done",
                 "payload": json.dumps({"runId": "run", "taskId": "task",
                                        "dispatchId": "dispatch"})}]}
            port.deliveries = [dict(batch)]
            first = settle_delivery(project, "objective", owner="owner", run="run", port=port)
            self.assertTrue(first["ack_eligible"])
            self.assertEqual(port.release_calls, 1)
            port.deliveries = [dict(batch)]
            replayed = settle_delivery(project, "objective", owner="owner", run="run", port=port)
            self.assertTrue(replayed["ack_eligible"])
            self.assertEqual(port.release_calls, 1, "the release must not be repeated")

    def test_an_acknowledgment_the_runtime_did_not_confirm_is_not_claimed(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.release_status = "released"
            self.start(project, assessment, caps, quota, port)
            port.settled = True
            port.deliveries = [{"id": "delivery-1", "messages": [
                {"id": "message-1", "type": "worker_done",
                 "payload": json.dumps({"runId": "run", "taskId": "task",
                                        "dispatchId": "dispatch"})}]}]
            settle_delivery(project, "objective", owner="owner", run="run", port=port)
            silent = lambda **kwargs: {"runtime": port.runtime}
            with patch.object(port, "wait_delivery", side_effect=silent):
                unconfirmed = acknowledge_delivery(project, "objective", owner="owner", run="run",
                                                   delivery_id="delivery-1", port=port)
            self.assertEqual(unconfirmed["status"], "acknowledgment_unconfirmed")
            self.assertFalse(unconfirmed["confirmed_by_runtime"])
            wrong_runtime = lambda **kwargs: {"runtime": "other", "acknowledged": "delivery-1"}
            with patch.object(port, "wait_delivery", side_effect=wrong_runtime):
                with self.assertRaises(PodError) as caught:
                    acknowledge_delivery(project, "objective", owner="owner", run="run",
                                         delivery_id="delivery-1", port=port)
            self.assertEqual(caught.exception.code, "delivery_ack_mismatch")

    def test_the_fleet_scope_names_only_what_was_read(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            page = {"runtime": "r", "scope": {"source": "bound"}, "workers": [], "complete": True}
            reads = []

            def rows(run=None):
                reads.append(run)
                return dict(page)

            with patch("pod.operations.worker_rows", side_effect=rows), \
                 patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}):
                port = OrcaPort(root)
                with patch("pod.operations._ledger_bindings", return_value={}), \
                     patch("pod.operations._quota_snapshot", return_value=None):
                    bound = port.read_native("owner", run="run-a")
                    merged = port.read_native("owner", run="run-a", runs=("run-b", "run-a"))
            self.assertEqual(bound["scope"], "bound")
            self.assertEqual(merged["scope"], "bound+ledger_runs")
            self.assertEqual(reads, ["run-a", "run-a", "run-b"])

    def test_a_descendant_named_by_an_object_is_still_counted(self):
        """Sibling projection fields are objects; assuming a bare string missed them."""
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            worker = {"dispatchId": "ctx_child", "terminalState": "active",
                      "projection": {"parent": {"id": "ctx_parent"}, "host": {"id": "local"}}}
            page = {"runtime": "r", "scope": {"source": "bound"}, "workers": [worker],
                    "complete": True}
            bindings = {"ctx_parent": {"account": "account", "objective": "objective",
                                       "agent": "codex", "bucket": "shared"}}
            with patch("pod.operations.worker_rows", return_value=page), \
                 patch("pod.operations._ledger_bindings", return_value=bindings), \
                 patch("pod.operations._quota_snapshot", return_value=None), \
                 patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}):
                native = OrcaPort(root).read_native("owner", run="run")
            row = native["workers"][0]
            self.assertTrue(row["descendant"])
            self.assertFalse(row.get("foreign"))
            self.assertEqual(row["objective"], "objective")

    def test_a_provider_reading_without_a_timestamp_is_not_a_quota_observation(self):
        with fixture() as root:
            metadata = {"providers": {"codex": {"windows": {"weekly": {"usedPercent": 40}},
                                                "updated_at_ms": None, "freshness": "unknown"}}}
            with patch("pod.operations.account_metadata_raw", return_value=metadata):
                self.assertIsNone(_quota_snapshot({"agent": "codex", "account": "a",
                                                   "model": "gpt-5.6-sol"}))

    def test_a_dated_provider_reading_is_a_usable_quota_observation(self):
        """`confidence` says where the numbers came from; age decides how much to trust them."""
        from pod.routing import quota_state
        with fixture() as root:
            now = datetime.now(timezone.utc)
            metadata = {"providers": {"codex": {"windows": {"weekly": {"usedPercent": 40,
                                                                       "resetsAt": 1}},
                                                "updated_at_ms": now.timestamp() * 1000,
                                                "freshness": "fresh"}}}
            with patch("pod.operations.account_metadata_raw", return_value=metadata):
                snapshot = _quota_snapshot({"agent": "codex", "account": "a",
                                            "model": "gpt-5.6-sol", "bucket": "default"})
            self.assertEqual(snapshot["confidence"], "observed")
            policy = {"quota_fresh_seconds": 60, "quota_low": 20, "quota_critical": 5}
            state, _ = quota_state(snapshot, provider="codex", account="a", bucket="default",
                                   policy=policy, now=now)
            self.assertEqual(state, "normal")
            stale = dict(snapshot)
            stale["observed_at"] = (now - timedelta(hours=3)).isoformat()
            aged, reason = quota_state(stale, provider="codex", account="a", bucket="default",
                                       policy=policy, now=now)
            self.assertEqual(aged, "unknown")
            self.assertIn("stale", reason)

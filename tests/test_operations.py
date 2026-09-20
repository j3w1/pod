from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, read, reconcile, record_delivery, reconcile_delivery_item, reserve
from pod.operations import guarded_start, release_once, reconcile_release
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

    def assurance(self, route):
        return {"runtime": "runtime", "billing_preflight": self.assured, "fanout_control": self.assured,
                "account_binding": {"provider": route["agent"], "account": route["account"],
                                    "bucket": route.get("bucket")}}

    def read_native(self, owner):
        self.reads += 1
        return {"runtime": "runtime", "authoritative": True, "owner": owner, "scope": "all",
                "complete": True, "workers": self.workers, "cross_host": False,
                "atomic_admission": False, "quota": self.quota}

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
                "terminalResource": {"releaseState": self.resource_state or "released"}
                if self.resource_state or (self.release_status == "released" and self.release_calls) else None}}

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
                     "fanout_control": True, "bucket": "shared"}}
    quota = {"account": {"schema": "pod-quota/v1", "provider": "codex", "account": "account",
                         "bucket": "shared", "observed_at": NOW.isoformat(), "source": "supported",
                         "confidence": "observed", "unknowns": [],
                         "windows": [{"name": "hour", "remaining_percent": 60}],
                         "remaining_percent": 60}}
    return project, assessment, caps, quota


def frozen_for(project, *, sources=None, context=None):
    route = {"alias": "sol", "agent": "codex", "model": "gpt-5.6-sol",
             "account": "account", "bucket": "shared", "effort": "high"}
    return packet({"schema": "pod-packet/v1", "objective": "objective", "criteria": ["works"],
                   "responsibility": "writer", "scope": ["notes.txt"], "actions": ["edit"],
                   "candidate": "c", "context": context or [], "dependencies": [], "route": route,
                   "policy_revision": effective(project)["revision"], "plan_revision": "plan",
                   "report_contract": "checks", "sources": sources or []})


class GuardedOperationTests(unittest.TestCase):
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
                project, assessment, caps, quota = inputs(root)
                port = FixturePort()
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}

                def admit(target, operation, capacity=1):
                    return reserve(project, target, owner="owner", operation_id=operation,
                                   requested=route, route_decision=decision,
                                   capability_contract=port.assurance(route),
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
                            if cleanup_state == "already_released":
                                port.resource_state = "already_released"
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                                 capability_contract=port.assurance(route),
                                 native_reader=lambda: port.read_native("owner"), capacity=2,
                                 run_id="run", plan_revision="plan", now=NOW)
                self.assertFalse(intent["existing"])

    def test_unproved_effect_disposition_never_releases_capacity(self):
        for disposition in ("absent", "failed", "unrecognized"):
            with self.subTest(disposition=disposition), fixture() as root, patch.dict(os.environ, {
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
                project, _, _, _ = inputs(root)
                port = FixturePort()
                route = frozen_for(project)["body"]["route"]
                decision = {"status": "usable", "selected": route,
                            "policy_revision": effective(project)["revision"]}
                def admit(operation):
                    return reserve(project, "objective", owner="owner", operation_id=operation,
                                   requested=route, route_decision=decision,
                                   capability_contract=port.assurance(route),
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                                 capability_contract=port.assurance(route),
                                 native_reader=lambda: port.read_native("owner"), capacity=1,
                                 run_id="run", plan_revision="plan", now=NOW)
                self.assertFalse(intent["existing"])

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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                                     capability_contract=port.assurance(route),
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
                    "XDG_STATE_HOME": str(root / "state"), "LOCALAPPDATA": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config")}):
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
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
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
                             route_decision=decision, capability_contract=port.assurance(route),
                             native_reader=lambda: port.read_native("owner"), capacity=2, run_id="run",
                             plan_revision="plan", now=NOW)
            self.assertFalse(intent["existing"])
            port.release_calls = 1
            port.release_status = "released"
            self.assertEqual(reconcile_release(project, "objective", owner="owner",
                                               dispatch="dispatch", port=port)["status"], "released")

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

    def test_worker_done_delivery_requires_bound_settlement_and_disposition(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
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

    def test_unverified_account_binding_blocks_before_native_effect(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
            project, assessment, caps, quota = inputs(root)
            port = FixturePort()
            port.assurance = lambda route: {"runtime": "runtime", "billing_preflight": True,
                                            "fanout_control": True,
                                            "account_binding": {"provider": "codex", "account": "other",
                                                                "bucket": "shared"}}
            with self.assertRaises(PodError) as caught:
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              operation_id="op", assessment=assessment, capabilities=caps,
                              quotas=quota, occupancy={}, plan_revision="plan", port=port, now=NOW)
            self.assertEqual(caught.exception.code, "account_binding_unverified")
            self.assertEqual(port.reads, 0)
            self.assertEqual(port.starts, 0)

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
                                                          "LOCALAPPDATA": str(root / "state"),
                                                          "XDG_CONFIG_HOME": str(root / "config"),
                                                          "APPDATA": str(root / "config")}):
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

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import (ADMISSION_STATES, _path, checkpoint, logical_projection,
                        migrate_v1, migration_inventory, read, _quota_hold)
from pod.records import source_identity
from tests.common import fixture


def checkpoint_body():
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "plan",
            "candidate": "candidate", "policy_revision": "policy", "native_refs": [],
            "assignments": [], "questions": [], "verification_gaps": ["works"],
            "next_safe_action": "inspect"}


def request():
    return {"alias": "sol", "agent": "codex", "model": "gpt-5.6-sol",
            "account": "account", "bucket": "shared", "effort": "high"}


def old_binding(dispatch="dispatch"):
    return {"dispatchId": dispatch, "workerId": "worker-" + dispatch, "taskId": "task",
            "runId": "run", "worktreeId": "worktree", "terminalHandle": "terminal-" + dispatch,
            "terminalResourceId": "resource-" + dispatch}


def shown(binding, *, released=False, launch=None):
    launch = launch or {key: request()[key] for key in ("agent", "model", "effort")}
    resource = {"id": binding.get("terminalResourceId"),
                "terminalHandle": binding.get("terminalHandle"),
                "worktreeId": binding.get("worktreeId"),
                "originDispatchId": binding["dispatchId"],
                "ownerDispatchId": binding["dispatchId"],
                "ownershipState": "released" if released else "owned"}
    return {"runtime": "runtime", "result": {
        "dispatch": {"id": binding["dispatchId"], "runId": binding["runId"],
                     "taskId": binding["taskId"],
                     **({"status": "completed"} if released else {})},
        "projection": {"id": binding["workerId"], "dispatchId": binding["dispatchId"],
                       "runId": binding["runId"], "taskId": binding["taskId"],
                       "outcome": "succeeded" if released else "in_progress",
                       "stage": {"dispatch": "completed" if released else "dispatched",
                                 "worker": "succeeded" if released else "running",
                                 "detail": "settled" if released else "working"},
                       "resource": {"state": "released" if released else "owned"}},
        "worker": {"dispatchId": binding["dispatchId"],
                   "worktreeId": binding.get("worktreeId", "worktree"),
                   "agentTerminalHandle": binding.get("terminalHandle", "terminal"),
                   "state": "succeeded" if released else "running",
                   "startOptions": {"launch": {"requested": launch, "effective": launch}}},
        "terminalResource": resource}}


def legacy(effects, cleanup=None):
    return {"schema": "pod-context/v1", "revision": 7, "owner": "owner",
            "effects": effects, "checkpoint": checkpoint_body(), "deliveries": {"old": {"history": True}},
            "interventions": {}, "source_rejections": {}, "cleanup": cleanup or {}}


def effect(state, *, binding=None, grant=None):
    return {"schema": "pod-effect/v1", "state": state, "request": request(),
            "runtime": "runtime", "operation_id": state, "route_revision": "policy",
            "native_binding": binding, "run_id": "run", "plan_revision": "plan",
            "bucket": "shared", "packet_id": "packet", "spending_grant": grant,
            "created_at": "2026-09-22T00:00:00+00:00"}


class V2StateTests(unittest.TestCase):
    def test_quota_exhaustion_is_monotonic_across_restart_and_window_renewal(self):
        with fixture() as root:
            now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
            def snapshot(seconds, hour, week, *, hour_reset="h1", week_reset="w1"):
                return {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                        "bucket": "shared",
                        "observed_at": (now + timedelta(seconds=seconds)).isoformat(),
                        "source": "supported", "confidence": "observed", "unknowns": [],
                        "windows": [{"name": "hour", "remaining_percent": hour,
                                     "reset_at": hour_reset},
                                    {"name": "week", "remaining_percent": week,
                                     "reset_at": week_reset}]}
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-5, 0, 40),
                                        "exhausted", now=now, freshness=60, project=root))
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-15, 0, 40),
                                        "exhausted", now=now, freshness=60, project=root))
            self.assertTrue(_quota_hold("codex", "a", "shared", snapshot(-10, 50, 40),
                                        "normal", now=now, freshness=60, project=root))
            script = ("from datetime import datetime,timezone; from pathlib import Path; "
                      "from pod.ledger import _quota_hold; "
                      "print(_quota_hold('codex','a','shared',None,'unknown',"
                      "now=datetime(2026,9,22,12,tzinfo=timezone.utc),freshness=60,"
                      "project=Path(r'" + str(root) + "')))" )
            resumed = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                     text=True, check=True, env=os.environ.copy())
            self.assertEqual(resumed.stdout.strip(), "True")
            self.assertFalse(_quota_hold("codex", "a", "shared",
                                         snapshot(0, 50, 40, hour_reset="h2"),
                                         "normal", now=now, freshness=60, project=root))
    def test_checkpoint_creates_only_v2_policy_state(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            state = checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                               native={"runtime": "runtime"})
            self.assertEqual(state["schema"], "pod-context/v2")
            self.assertEqual(set(state), {"schema", "revision", "owner", "admissions", "checkpoint",
                                          "interventions", "source_rejections", "legacy_archives"})
            for retired in ("effects", "deliveries", "cleanup"):
                self.assertNotIn(retired, state)

    def test_logical_projection_frees_only_one_exact_settled_assignment(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                       native={"runtime": "runtime"})
            path = _path(project, "objective")
            state = read(project, "objective")
            binding = {key: value for key, value in old_binding().items() if key != "terminalResourceId"}
            admission = {"schema": "pod-admission/v2", "state": "bound", "admission_id": "a",
                         "objective": "objective", "owner": "owner", "request": request(),
                         "route_decision": {"policy_revision": "policy"}, "effective_evidence": {},
                         "runtime": "runtime", "request_uuid": None, "run_id": "run", "task_id": "task",
                         "plan_revision": "plan", "packet_id": "packet", "worktree": "current",
                         "bucket": "shared", "native_binding": binding, "recovery": {}, "error": None,
                         "created_at": "x", "updated_at": "x"}
            state["admissions"] = {"a": admission}
            from pod.util import atomic_json
            atomic_json(path, state)
            base = {"runtime": "runtime", "scope": "objective_assignments",
                    "complete": True, "physical_capacity": "unavailable"}
            settled = {"admission_id": "a", "runtime": "runtime", "run_id": "run",
                       "task_id": "task", "dispatch_id": "dispatch",
                       "worker_id": "worker-dispatch", "settled": True}
            self.assertEqual(logical_projection(project, {**base, "assignments": [settled]},
                                                objective="objective")["outstanding"], [])
            missing = logical_projection(project, {**base, "assignments": []},
                                         objective="objective")
            self.assertEqual(len(missing["outstanding"]), 1)
            ambiguous = logical_projection(project, {**base, "assignments": [settled, settled]},
                                           objective="objective")
            self.assertEqual(len(ambiguous["outstanding"]), 1)
            wrong = {**settled, "task_id": "other"}
            self.assertEqual(len(logical_projection(project, {**base, "assignments": [wrong]},
                                                    objective="objective")["outstanding"]), 1)

    def test_projection_rejects_fleet_or_caller_completion_shapes(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            for native in ({"runtime": "runtime", "scope": "all_runs", "complete": True,
                            "workers": []},
                           {"runtime": "runtime", "scope": "objective_assignments",
                            "complete": True, "completed": True}):
                with self.subTest(native=native), self.assertRaises(PodError) as caught:
                    logical_projection(project, native, objective="objective")
                self.assertEqual(caught.exception.code, "native_assignment_unverified")

    def test_source_rejection_remains_durable(self):
        from pod.ledger import check_bound_sources
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            source = project / "a.txt"
            source.write_text("one")
            bound = [source_identity(project, "a.txt")]
            checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                       native={"runtime": "runtime"})
            source.write_text("two")
            with self.assertRaises(PodError) as changed:
                check_bound_sources(project, "objective", owner="owner", assignment="packet", sources=bound)
            self.assertEqual(changed.exception.code, "source_changed")
            source.write_text("one")
            with self.assertRaises(PodError) as sticky:
                check_bound_sources(project, "objective", owner="owner", assignment="packet", sources=bound)
            self.assertEqual(sticky.exception.code, "source_rejected")


class MigrationTests(unittest.TestCase):
    def write_old(self, project, value):
        path = _path(project, "objective")
        path.parent.mkdir(parents=True)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        path.write_bytes(encoded)
        return path, encoded

    def test_all_legacy_states_archive_and_exact_confirmed_bind(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            active = old_binding("active")
            released = old_binding("released")
            predecessor = {key: value for key, value in old_binding("predecessor").items()
                           if key in ("dispatchId", "workerId", "taskId", "runId")}
            grant = {"id": "paid", "identity": "g", "units": 1, "scope": "s"}
            old = legacy({"reserved": effect("reserved"), "uncertain": effect("uncertain"),
                          "active": effect("confirmed", binding=active, grant=grant),
                          "released": effect("confirmed", binding=released, grant=grant),
                          "predecessor": effect("confirmed", binding=predecessor)},
                         cleanup={"released": {"state": "release_unknown"}})
            path, original = self.write_old(project, old)
            calls = []
            def reader(dispatch):
                calls.append(dispatch)
                selected = active if dispatch == "active" else predecessor if dispatch == "predecessor" else released
                return shown(selected, released=dispatch == "released")
            result = migrate_v1(project, "objective", owner="owner", worker_reader=reader)
            self.assertEqual(sorted(calls), ["active", "predecessor", "released"])
            state = read(project, "objective")
            states = sorted(row["state"] for row in state["admissions"].values())
            self.assertEqual(states, ["bound", "bound", "closed", "legacy_hold", "legacy_hold"])
            self.assertEqual(result["admissions"]["legacy_hold"], 2)
            archive = path.parent / state["legacy_archives"][0]["path"]
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(state["legacy_archives"][0]["sha256"], hashlib.sha256(original).hexdigest())
            bound = next(row for row in state["admissions"].values() if row["state"] == "bound")
            self.assertEqual(bound["recovery"]["spending_grant"], grant)
            closed = next(row for row in state["admissions"].values() if row["state"] == "closed")
            self.assertEqual(closed["recovery"]["spending_grant"], grant)
            self.assertNotIn("deliveries", state)

    def test_migration_uses_the_same_coherent_assignment_settlement_rule(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            cases = {}
            terminal = ("succeeded", "failed", "stopped", "canceled", "cancelled",
                        "abandoned")
            for outcome in terminal:
                binding = old_binding(outcome)
                cases[outcome] = (binding, outcome, "settled", "closed")
            cases.update({
                "terminal_active_stage": (old_binding("terminal-active"), "succeeded",
                                          "working", "bound"),
                "active_settled_stage": (old_binding("active-settled"), "in_progress",
                                          "settled", "bound"),
                "missing_outcome": (old_binding("missing"), None, "settled", "bound"),
                "malformed_outcome": (old_binding("malformed-outcome"), ["succeeded"],
                                      "settled", "bound"),
                "malformed_stage": (old_binding("malformed"), "succeeded", None, "bound"),
            })
            self.write_old(project, legacy({
                name: effect("confirmed", binding=binding)
                for name, (binding, _, _, _) in cases.items()}))

            def reader(dispatch):
                case = next(case for case in cases.values()
                            if case[0]["dispatchId"] == dispatch)
                binding, outcome, detail, _ = case
                value = shown(binding)
                projection = value["result"]["projection"]
                if outcome is None:
                    projection.pop("outcome")
                else:
                    projection["outcome"] = outcome
                if detail is None:
                    projection["stage"] = "settled"
                else:
                    projection["stage"]["detail"] = detail
                return value

            migrate_v1(project, "objective", owner="owner", worker_reader=reader)
            migrated = read(project, "objective")["admissions"].values()
            by_effect = {row["recovery"]["legacy_effect"]: row["state"] for row in migrated}
            self.assertEqual(by_effect, {name: case[3] for name, case in cases.items()})

    def test_fifo_legacy_context_is_rejected_without_blocking(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path = _path(project, "objective")
            path.parent.mkdir(parents=True)
            os.mkfifo(path)
            with self.assertRaises(PodError) as caught:
                migrate_v1(project, "objective", owner="owner", worker_reader=lambda dispatch: {})
            self.assertEqual(caught.exception.code, "state_migration_failed")

    def test_oversized_archive_readback_is_bounded_and_keeps_v1(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path, original = self.write_old(project, legacy({"reserved": effect("reserved")}))
            digest_value = hashlib.sha256(original).hexdigest()
            archive = path.parent / "archives" / f"context-v1-{digest_value}.json"
            archive.parent.mkdir()
            archive.write_bytes(b"x" * (1_048_576 + 1))
            with self.assertRaises(PodError) as caught:
                migrate_v1(project, "objective", owner="owner", worker_reader=lambda dispatch: {})
            self.assertEqual(caught.exception.code, "state_migration_failed")
            self.assertEqual(path.read_bytes(), original)

    def test_redirected_archive_ancestry_is_rejected_before_write(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path, original = self.write_old(project, legacy({"reserved": effect("reserved")}))
            outside = root / "outside"
            outside.mkdir()
            (path.parent / "archives").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(PodError) as caught:
                migrate_v1(project, "objective", owner="owner", worker_reader=lambda dispatch: {})
            self.assertEqual(caught.exception.code, "state_migration_failed")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(outside.iterdir()), [])

    def test_retention_cleanup_history_does_not_change_exact_assignment_binding(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            binding = old_binding()
            self.write_old(project, legacy({"op": effect("confirmed", binding=binding)},
                                           cleanup={"dispatch": {"state": "release_pending"}}))
            migrate_v1(project, "objective", owner="owner",
                       worker_reader=lambda dispatch: shown(binding, released=False))
            row = next(iter(read(project, "objective")["admissions"].values()))
            self.assertEqual(row["state"], "bound")

    def test_worktree_or_launch_contradiction_never_promotes(self):
        for contradiction in ("worktree", "launch"):
            with self.subTest(contradiction=contradiction), fixture() as root:
                project = root / "project"
                project.mkdir()
                binding = old_binding()
                self.write_old(project, legacy({"op": effect("confirmed", binding=binding)}))
                observed = shown(binding)
                if contradiction == "worktree":
                    observed["result"]["worker"]["worktreeId"] = "other"
                else:
                    wrong = {"agent": "codex", "model": "other", "effort": "high"}
                    observed = shown(binding, launch=wrong)
                migrate_v1(project, "objective", owner="owner",
                           worker_reader=lambda dispatch, value=observed: value)
                row = next(iter(read(project, "objective")["admissions"].values()))
                self.assertEqual(row["state"], "legacy_hold")

    def test_validation_failure_leaves_v1_untouched_and_calls_no_native_mutation(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            invalid = legacy({"bad": {"state": "reserved"}})
            path, original = self.write_old(project, invalid)
            calls = []
            with self.assertRaises(PodError):
                migrate_v1(project, "objective", owner="owner",
                           worker_reader=lambda dispatch: calls.append(dispatch))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(calls, [])

    def test_atomic_replace_failure_keeps_v1_context(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path, original = self.write_old(project, legacy({"reserved": effect("reserved")}))
            with patch("pod.ledger.atomic_json", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    migrate_v1(project, "objective", owner="owner", worker_reader=lambda dispatch: {})
            self.assertEqual(path.read_bytes(), original)

    def test_read_only_inventory_requires_explicit_migration(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            self.write_old(project, legacy({"reserved": effect("reserved")}))
            inventory = migration_inventory(project)
            self.assertTrue(inventory["migration_required"])
            with self.assertRaises(PodError) as caught:
                read(project, "objective")
            self.assertEqual(caught.exception.code, "state_migration_required")

    def test_state_enum_is_exact(self):
        self.assertEqual(ADMISSION_STATES,
                         ("reserved", "bound", "unresolved", "closed", "deferred", "legacy_hold"))

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import (ADMISSION_STATES, CONTEXT_SCHEMA, _path, checkpoint,
                        logical_projection, read, state_inventory, state_root,
                        _quota_hold)
from pod.records import source_identity
from pod.util import digest
from tests.common import fixture


def checkpoint_body():
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "plan",
            "candidate": "candidate", "policy_revision": "policy", "native_refs": [],
            "assignments": [], "questions": [], "verification_gaps": ["works"],
            "next_safe_action": "inspect"}


class V2StateTests(unittest.TestCase):
    def test_native_state_root_symlink_is_allowed_but_owned_redirect_is_not(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            real = root.parent / "real-native-state"
            real.mkdir()
            linked = root.parent / "linked-native-state"
            linked.symlink_to(real, target_is_directory=True)
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(linked)}):
                checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                           native={"runtime": "runtime"})
                self.assertEqual(state_root(project), real / "pod")
                self.assertTrue((real / "pod").is_dir())

                outside = root.parent / "redirected-state"
                outside.mkdir()
                owned = real / "pod" / digest({"project": str(project.resolve()),
                                                 "objective": "blocked"})
                owned.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(PodError) as caught:
                    checkpoint(project, "blocked", owner="owner", value=checkpoint_body(),
                               native={"runtime": "runtime"})
                self.assertEqual(caught.exception.code, "unsafe_state")
                self.assertEqual(list(outside.iterdir()), [])

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
    def test_checkpoint_creates_exact_current_state(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            state = checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                               native={"runtime": "runtime"})
            self.assertEqual(state["schema"], CONTEXT_SCHEMA)
            self.assertEqual(set(state), {"schema", "revision", "owner", "admissions", "checkpoint",
                                          "interventions", "source_rejections"})

    def test_logical_projection_frees_only_one_exact_settled_assignment(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                       native={"runtime": "runtime"})
            path = _path(project, "objective")
            state = read(project, "objective")
            binding = {"dispatchId": "dispatch", "workerId": "worker-dispatch", "taskId": "task",
                       "runId": "run", "worktreeId": "worktree", "terminalHandle": "terminal-dispatch"}
            request = {"alias": "sol", "agent": "codex", "model": "gpt-6-sol",
                       "account": "account", "bucket": "shared", "effort": "high"}
            admission = {"schema": "pod-admission/v2", "state": "bound", "admission_id": "a",
                         "objective": "objective", "owner": "owner", "request": request,
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


class UnsupportedStateTests(unittest.TestCase):
    """A record in any other schema is reported and blocks its objective; nothing converts it."""

    def write_foreign(self, project, objective="objective", value=None):
        path = _path(project, objective)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value or {"schema": "pod-context/v9", "revision": 1, "owner": "owner",
                                       "admissions": {}, "checkpoint": None, "interventions": {},
                                       "source_rejections": {}},
                             sort_keys=True).encode() + b"\n"
        path.write_bytes(encoded)
        return path, encoded

    def test_foreign_schema_blocks_only_its_objective_and_is_left_untouched(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path, original = self.write_foreign(project)
            with self.assertRaises(PodError) as reading:
                read(project, "objective")
            self.assertEqual(reading.exception.code, "state_unsupported")
            with self.assertRaises(PodError) as writing:
                checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                           native={"runtime": "runtime"})
            self.assertEqual(writing.exception.code, "state_unsupported")
            self.assertEqual(path.read_bytes(), original)
            sibling = checkpoint(project, "another objective", owner="owner",
                                 value=checkpoint_body(), native={"runtime": "runtime"})
            self.assertEqual(sibling["schema"], CONTEXT_SCHEMA)
            inventory = state_inventory(project)
            self.assertEqual((inventory["current"], inventory["unsupported"],
                              inventory["unreadable"], inventory["blocked"]), (1, 1, 0, True))
            self.assertEqual(path.read_bytes(), original)

    def test_unreadable_records_are_counted_without_being_rewritten(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            path, original = self.write_foreign(project, value={"schema": CONTEXT_SCHEMA})
            with self.assertRaises(PodError) as reading:
                read(project, "objective")
            self.assertEqual(reading.exception.code, "invalid_context")
            broken = _path(project, "broken")
            broken.parent.mkdir(parents=True)
            broken.write_bytes(b"{not json")
            self.write_foreign(project, "foreign")
            inventory = state_inventory(project)
            self.assertEqual((inventory["current"], inventory["unsupported"], inventory["unreadable"]),
                             (0, 1, 2))
            self.assertTrue(inventory["blocked"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(broken.read_bytes(), b"{not json")

    def test_state_enum_is_exact(self):
        self.assertEqual(ADMISSION_STATES,
                         ("reserved", "bound", "unresolved", "closed", "deferred"))

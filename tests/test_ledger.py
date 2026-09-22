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
from pod.ledger import (ADMISSION_STATES, _path, checkpoint, fresh_projection,
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
                     "taskId": binding["taskId"]},
        "projection": {"id": binding["workerId"], "dispatchId": binding["dispatchId"],
                       "runId": binding["runId"], "taskId": binding["taskId"],
                       "resource": {"state": "released" if released else "owned"}},
        "worker": {"dispatchId": binding["dispatchId"],
                   "worktreeId": binding.get("worktreeId", "worktree"),
                   "agentTerminalHandle": binding.get("terminalHandle", "terminal"),
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

    def test_fresh_projection_releases_only_on_one_exact_native_row(self):
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
            base = {"runtime": "runtime", "scope": "all", "complete": True}
            released = {"dispatchId": "dispatch", "runId": "run", "taskId": "task",
                        "terminalState": "released"}
            self.assertEqual(fresh_projection(project, {**base, "workers": [released]},
                                              objective="objective")["occupied"], [])
            missing = fresh_projection(project, {**base, "workers": []}, objective="objective")
            self.assertEqual(len(missing["occupied"]), 1)
            ambiguous = fresh_projection(project, {**base, "workers": [released, released]},
                                          objective="objective")
            self.assertEqual(len(ambiguous["occupied"]), 1)
            wrong = {**released, "taskId": "other"}
            self.assertEqual(len(fresh_projection(project, {**base, "workers": [wrong]},
                                                  objective="objective")["occupied"]), 1)

    def test_exact_released_projection_frees_a_bound_legacy_hold_only(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            binding = old_binding()
            path = _path(project, "objective")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(legacy(
                {"op": effect("confirmed", binding=binding)},
                cleanup={"dispatch": {"state": "release_unknown"}})))
            migrate_v1(project, "objective", owner="owner",
                       worker_reader=lambda dispatch: shown(binding, released=False))
            self.assertEqual(next(iter(read(project, "objective")["admissions"].values()))["state"],
                             "legacy_hold")
            native = {"runtime": "runtime", "scope": "bound+ledger_runs", "complete": True,
                      "workers": [{"dispatchId": "dispatch", "runId": "run",
                                   "taskId": "task", "terminalState": "released"}]}
            self.assertEqual(fresh_projection(project, native, objective="objective")["occupied"], [])

    def test_foreign_and_descendant_rows_are_not_lost(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="owner", value=checkpoint_body(),
                       native={"runtime": "runtime"})
            path = _path(project, "objective")
            state = read(project, "objective")
            binding = {key: value for key, value in old_binding("parent").items()
                       if key != "terminalResourceId"}
            state["admissions"] = {"a": {
                "schema": "pod-admission/v2", "state": "bound", "admission_id": "a",
                "objective": "objective", "owner": "owner", "request": request(),
                "route_decision": {"policy_revision": "policy"}, "effective_evidence": {},
                "runtime": "runtime", "request_uuid": None, "run_id": "run",
                "task_id": "task", "plan_revision": "plan", "packet_id": "packet",
                "worktree": "current", "bucket": "shared", "native_binding": binding,
                "recovery": {}, "error": None, "created_at": "x", "updated_at": "x"}}
            from pod.util import atomic_json
            atomic_json(path, state)
            native = {"runtime": "runtime", "scope": "all", "complete": True,
                      "workers": [
                          {"dispatchId": "parent", "runId": "run", "taskId": "task",
                           "terminalState": "active", "projection": {"dispatchId": "parent"}},
                          {"dispatchId": "child", "terminalState": "active",
                           "projection": {"dispatchId": "child", "parent": "parent"}},
                          {"dispatchId": "foreign", "terminalState": "active",
                           "projection": {"dispatchId": "foreign"}}]}
            projection = fresh_projection(project, native, objective=None)
            self.assertEqual(len(projection["occupied"]), 3)
            self.assertEqual(sum(row["objective"] == "objective"
                                 for row in projection["occupied"]), 2)
            # Objective filtering retains the bound parent and descendant, but unrelated
            # native work cannot stall this direct-work Governor unit.
            selected = fresh_projection(project, native, objective="objective")["occupied"]
            self.assertEqual(len(selected), 2)
            self.assertTrue(all(row["objective"] == "objective" for row in selected))

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

    def test_uncertain_release_holds_without_exact_release_proof(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            binding = old_binding()
            self.write_old(project, legacy({"op": effect("confirmed", binding=binding)},
                                           cleanup={"dispatch": {"state": "release_pending"}}))
            migrate_v1(project, "objective", owner="owner",
                       worker_reader=lambda dispatch: shown(binding, released=False))
            row = next(iter(read(project, "objective")["admissions"].values()))
            self.assertEqual(row["state"], "legacy_hold")

    def test_worktree_terminal_resource_or_launch_contradiction_never_promotes(self):
        for contradiction in ("worktree", "terminal_resource", "launch"):
            with self.subTest(contradiction=contradiction), fixture() as root:
                project = root / "project"
                project.mkdir()
                binding = old_binding()
                self.write_old(project, legacy({"op": effect("confirmed", binding=binding)}))
                observed = shown(binding)
                if contradiction == "worktree":
                    observed["result"]["worker"]["worktreeId"] = "other"
                elif contradiction == "terminal_resource":
                    observed["result"]["terminalResource"]["id"] = "other"
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
                         ("reserved", "bound", "unresolved", "closed", "legacy_hold"))

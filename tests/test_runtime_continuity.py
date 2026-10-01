"""A2: runtime-continuity rebind after an Orca runtime change, never a takeover."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import (CONTINUITY_HISTORY, objective_root, read, update_admission)
from pod.operations import OrcaPort, recover_admission
from pod.status import status as pod_status
from tests.kernel_support import KernelCase, FakePort, git

AMBIGUOUS = "runtime_continuity_ambiguous"
NATIVE_METHODS = ("capability", "resolve_worktree", "read_native", "start_worker", "request_show",
                  "find_worker", "show_worker")


class ContinuityPort(FakePort):
    """A native port that also reports the stable current-Run binding and caller identity."""

    def __init__(self):
        super().__init__()
        self.run_binding = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        self.caller = "owner"
        self.reports_binding = True
        self.unavailable = set()
        self.find_failure = None
        self.calls = []

    def read_native(self, owner, *, authority_runs=(), assignments=()):
        self.calls.append("read_native")
        observed = super().read_native(owner, authority_runs=authority_runs, assignments=assignments)
        if authority_runs and self.reports_binding:
            binding = self.run_binding
            authoritative = bool(self.caller == owner and isinstance(binding, dict)
                                 and binding["id"] in authority_runs
                                 and binding["coordinator_handle"] == owner)
            observed.update(binding=dict(binding) if isinstance(binding, dict) else None,
                            caller=self.caller, authoritative=authoritative,
                            owner=owner if authoritative else None)
        return observed

    def show_worker(self, dispatch):
        self.calls.append("show_worker")
        if dispatch in self.unavailable:
            raise PodError("orca_unavailable", "worker-show is unavailable")
        return super().show_worker(dispatch)

    def find_worker(self, *, run, task):
        self.calls.append("find_worker")
        if self.find_failure is not None:
            if isinstance(self.find_failure, PodError):
                raise self.find_failure
            return self.find_failure
        return super().find_worker(run=run, task=task)

    def start_worker(self, **kwargs):
        self.calls.append("start_worker")
        return super().start_worker(**kwargs)

    def request_show(self, request_uuid):
        self.calls.append("request_show")
        return super().request_show(request_uuid)


class ContinuityCase(KernelCase):
    """Real native authority over a disposable objective; only the Orca port is a fake."""

    def setUp(self):
        super().setUp()
        patch.stopall()  # KernelCase's authority stand-in; A2 is exercised through the real join.
        self.port = ContinuityPort()
        context = __import__("pod.github", fromlist=["repository_context"]).repository_context(self.project)
        self.port.placement = {"repository": None, "repo_key": context["repo_key"],
                               "path": context["worktree"], "branch": "main", "runtime": "runtime"}
        self.frozen_packets = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in NATIVE_METHODS:
            self.stack.enter_context(patch.object(
                OrcaPort, name, autospec=True,
                side_effect=lambda _port, *args, _name=name, **kwargs: getattr(self.port, _name)(*args, **kwargs)))
        self.stack.enter_context(patch("pod.orca.current_run", side_effect=lambda: {
            "runtime": self.port.runtime, "run": self.port.run_binding}))
        self.stack.enter_context(patch("pod.orca.contract", side_effect=lambda: {
            "status": "observed", "runtime": self.port.runtime,
            "capabilities": {"launch_preferences_v1": True}}))

    # ------------------------------------------------------------------ helpers
    @property
    def context(self) -> Path:
        return objective_root(self.project, "objective") / "context.json"

    def journal(self) -> Path:
        return objective_root(self.project, "objective") / "governor.json"

    def snapshot(self) -> tuple[bytes, bytes | None]:
        journal = self.journal()
        return self.context.read_bytes(), journal.read_bytes() if journal.exists() else None

    def change_runtime(self, runtime="runtime-2"):
        self.port.runtime = runtime
        self.port.placement = {**self.port.placement, "runtime": runtime}
        self.port.calls.clear()

    def history(self) -> list[dict]:
        return (read(self.project, "objective")["checkpoint"].get("continuity") or {}).get("history", [])

    def checkpoint_op(self, **extra) -> dict:
        stored = read(self.project, "objective")["checkpoint"]
        value = {key: item for key, item in stored.items()
                 if key in ("schema", "criteria", "plan_revision", "candidate", "policy_revision",
                            "native_refs", "assignments", "questions", "verification_gaps",
                            "next_safe_action", "verification")}
        return internal_run("checkpoint", {"project": str(self.project), "objective": "objective",
                                           "owner": "owner",
                                           "value": {**value, "obligations": self.stored(), **extra}})

    def admission_op(self, task: str, serves: list[str], **packet) -> dict:
        frozen = self.packet(serves, **packet)
        return internal_run("admission", {"project": str(self.project), "objective": "objective",
                                          "owner": "owner", "run": "run", "task": task,
                                          "plan_revision": "plan", "packet": frozen,
                                          "worktree": "current"}), frozen

    def started(self, count=1, spare=0) -> list[dict]:
        self.intake(*[self.sub(f"S{index}", boundary={"paths": [f"area{index}"]})
                      for index in range(1, count + spare + 1)])
        rows = []
        for index in range(1, count + 1):
            result, frozen = self.admission_op(f"task-{index}", [f"S{index}"],
                                               boundary={"paths": [f"area{index}"]})
            self.frozen_packets[result["admission"]["admission_id"]] = frozen
            rows.append(result["admission"])
        return rows

    def refused(self, code, call, *args, **kwargs) -> PodError:
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def decision(self, error: PodError, **override) -> dict:
        detail = error.detail
        return {"provenance": "user_direct", "instruction": "Rebind this objective to the new runtime",
                "objective": detail["objective"], "recorded_runtime": detail["recorded_runtime"],
                "current_runtime": detail["current_runtime"], "ambiguity": list(detail["ambiguity"]),
                **override}

    def owner_op(self, decision: dict) -> dict:
        return internal_run("runtime-continuity", {"project": str(self.project), "objective": "objective",
                                                   "owner": "owner", "decision": decision})


class ProvenContinuityTests(ContinuityCase):
    def test_checkpoint_records_generation_and_proven_change_rebinds_checkpoint_write(self):
        self.started()
        self.assertEqual(read(self.project, "objective")["checkpoint"]["continuity"],
                         {"binding": {"run": "run", "coordinator": "owner", "generation": 1}, "history": []})
        self.change_runtime()
        result = self.checkpoint_op()
        [entry] = result["runtime_continuity"]
        self.assertEqual((entry["from_runtime"], entry["to_runtime"], entry["provenance"]),
                         ("runtime", "runtime-2", "automatic"))
        self.assertEqual(entry["verified"]["generation"], 1)
        self.assertEqual(len(entry["verified"]["admissions"]), 1)
        state = read(self.project, "objective")
        self.assertEqual(state["checkpoint"]["native_refs"], [{"runId": "run", "runtime": "runtime-2"}])
        self.assertEqual({row["runtime"] for row in state["admissions"].values()}, {"runtime-2"})
        self.assertEqual(self.history(), [entry])
        self.assertNotIn("start_worker", self.port.calls)
        observed = pod_status(self.project, None, objective="objective",
                              current_run_fn=lambda: {"run": {"id": "run"}, "runtime": "runtime-2"},
                              worker_rows_fn=lambda _run: {"runtime": "runtime-2", "workers": [],
                                                           "scope": None, "complete": True})
        self.assertEqual(observed["runtime_continuity"]["history"], [entry])
        self.assertEqual(observed["native_settlement"], "observed")
        # Afterwards mutations behave as for an unchanged runtime: no second rebind.
        self.assertNotIn("runtime_continuity", self.checkpoint_op())

    def test_governor_mutation_report_read_and_new_admission_each_rebind_and_continue(self):
        first, = self.started(spare=1)
        git(self.project, "remote", "add", "origin", str(self.project))
        self.change_runtime("runtime-2")
        prepared = internal_run("governor-prepare", {
            "project": str(self.project), "objective": "objective", "owner": "owner", "unit": "delivery",
            "branch": {"remote": "origin", "base": "target", "branch": "delivery"}})
        self.assertEqual([row["provenance"] for row in prepared["runtime_continuity"]], ["automatic"])
        self.assertIn("candidate", prepared)

        self.change_runtime("runtime-3")
        self.settle(first)
        rows = self.stored()
        row = next(item for item in rows if item.get("executor") == first["admission_id"])
        row.pop("executor")
        row.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        frozen = self.frozen_packets[first["admission_id"]]
        consumed = internal_run("report", {
            "project": str(self.project), "objective": "objective", "admission_id": first["admission_id"],
            "packet": frozen, "map": {"obligations": rows},
            "report": {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                       "attempt": first["native_binding"]["dispatchId"], "candidate": self.candidate,
                       "outcome": "succeeded", "scope": ["src"], "files": [], "checks": ["unit"],
                       "failures": [], "evidence": [], "uncertainty": [], "questions": []}})
        self.assertEqual(consumed["runtime_continuity"][0]["to_runtime"], "runtime-3")
        self.assertTrue(consumed["settled"])

        self.change_runtime("runtime-4")
        second, _ = self.admission_op("task-2", ["S2"], boundary={"paths": ["area2"]})
        self.assertEqual(second["status"], "bound")
        self.assertEqual(second["runtime_continuity"][0]["to_runtime"], "runtime-4")
        self.assertEqual(second["admission"]["runtime"], "runtime-4")
        self.assertEqual([entry["to_runtime"] for entry in self.history()],
                         ["runtime-2", "runtime-3", "runtime-4"])

    def test_reserved_admission_proven_absent_rebinds_then_continues_recovery(self):
        first, = self.started()
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="reserved", native_binding=None,
                                                       request_uuid=None))
        self.port.workers.clear()
        self.change_runtime()
        recovered = recover_admission(self.project, "objective", owner="owner",
                                      admission_id=first["admission_id"], worktree="current", port=self.port)
        [entry] = self.history()
        self.assertEqual(entry["verified"]["absent"], [first["admission_id"]])
        self.assertEqual((recovered["action"], recovered["status"]), ("inspect_without_uuid", "unresolved"))
        self.assertEqual(recovered["admission"]["error"]["code"], "native_attempt_absent")
        self.assertNotIn("start_worker", self.port.calls)

    def test_reserved_state_alone_without_a_lookup_result_does_not_rebind(self):
        first, = self.started()
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="reserved", native_binding=None,
                                                       request_uuid=None))
        self.change_runtime()
        before = self.snapshot()
        for failure in (PodError("orca_unavailable", "lookup failed"), "inconclusive"):
            self.port.find_failure = failure
            with self.subTest(failure=failure):
                error = self.refused(AMBIGUOUS, recover_admission, self.project, "objective", owner="owner",
                                     admission_id=first["admission_id"], worktree="current", port=self.port)
                self.assertIn(first["admission_id"], " ".join(error.detail["ambiguity"]))
                self.assertEqual(self.snapshot(), before)


class AmbiguousContinuityTests(ContinuityCase):
    def assert_ambiguous(self, fragment: str, call=None) -> PodError:
        before = self.snapshot()
        error = self.refused(AMBIGUOUS, call or self.checkpoint_op)
        self.assertTrue(any(fragment in item for item in error.detail["ambiguity"]), error.detail)
        self.assertIn("Ask the Owner", str(error))
        self.assertIn("ask the Owner", error.detail["next_action"])
        self.assertEqual(self.snapshot(), before)
        return error

    def test_each_ambiguous_case_refuses_with_the_distinct_code_and_writes_nothing(self):
        first, = self.started()
        self.journal().write_text(json.dumps({"fixture": "journal"}))  # must stay byte-identical
        dispatch = first["native_binding"]["dispatchId"]
        self.change_runtime()
        self.port.run_binding = None
        self.assert_ambiguous("run-current returned none")
        self.port.run_binding = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        self.port.unavailable = {dispatch}
        self.assert_ambiguous(f"admission {first['admission_id']}: worker read unavailable")
        self.port.unavailable = set()
        update_admission_raw = self.context.read_text()
        state = json.loads(update_admission_raw)
        state["admissions"][first["admission_id"]].update(state="unresolved", native_binding=None)
        self.context.write_text(json.dumps(state))
        self.assert_ambiguous(f"admission {first['admission_id']}: unresolved attempt")
        state["admissions"][first["admission_id"]].update(state="reserved")
        self.context.write_text(json.dumps(state))
        self.assert_ambiguous("Dispatch found without a recorded native binding")
        self.port.find_failure = PodError("orca_unavailable", "lookup unavailable")
        self.assert_ambiguous("exact Run/Task lookup unavailable")
        self.port.find_failure = None
        state["admissions"][first["admission_id"]].update(state="bound", native_binding=first["native_binding"])
        state["checkpoint"]["native_refs"].append({"runId": "run-b", "runtime": "runtime"})
        self.context.write_text(json.dumps(state))
        self.assert_ambiguous("run run-b: not shown by run-current")

    def test_missing_recorded_generation_is_ambiguous_and_owner_decision_rebinds(self):
        self.port.reports_binding = False  # a 0.6.6 objective never recorded its generation
        self.started()
        self.assertNotIn("continuity", read(self.project, "objective")["checkpoint"])
        self.port.reports_binding = True
        self.change_runtime()
        error = self.assert_ambiguous("recorded consumer generation")
        before = self.snapshot()
        for stale in ({"provenance": "standing_delegation"}, {"ambiguity": ["something else"]},
                      {"current_runtime": "runtime-9"}, {"recorded_runtime": "runtime-0"},
                      {"objective": "another"}):
            with self.subTest(stale=stale):
                self.refused("runtime_continuity_decision_mismatch", self.owner_op, self.decision(error, **stale))
                self.assertEqual(self.snapshot(), before)
        self.refused("invalid_continuity_decision", self.owner_op,
                     {**self.decision(error), "scope": "all objectives"})
        rebound = self.owner_op(self.decision(error))
        entry = rebound["runtime_continuity"]
        self.assertEqual((entry["provenance"], entry["decision"]["provenance"]), ("owner", "user_direct"))
        stored = read(self.project, "objective")["checkpoint"]["continuity"]
        self.assertEqual(stored["binding"], {"run": "run", "coordinator": "owner", "generation": 1})
        self.assertNotIn("runtime_continuity", self.checkpoint_op())
        self.assertEqual(self.port.calls.count("start_worker"), 0)

    def test_changed_facts_refuse_the_owner_decision_and_unreadable_identity_keeps_blocking(self):
        first, = self.started()
        dispatch = first["native_binding"]["dispatchId"]
        self.change_runtime()
        self.port.unavailable = {dispatch}
        error = self.assert_ambiguous("worker read unavailable")
        self.port.unavailable = set()
        self.refused("runtime_continuity_decision_mismatch", self.owner_op, self.decision(error))
        self.port.unavailable = {dispatch}
        self.port.run_binding = {**self.port.run_binding, "consumer_generation": 2}
        self.refused("native_authority_unverified", self.owner_op, self.decision(error))
        self.port.run_binding = {**self.port.run_binding, "consumer_generation": 1}
        rebound = self.owner_op(self.decision(error))
        self.assertEqual(rebound["runtime_continuity"]["provenance"], "owner")
        # The still-unreadable worker blocks every mutation until an exact read matches it.
        self.refused("orca_unavailable", self.checkpoint_op)
        self.assertEqual(read(self.project, "objective")["admissions"][first["admission_id"]]["state"], "bound")
        self.port.unavailable = set()
        self.assertNotIn("runtime_continuity", self.checkpoint_op())

    def test_admission_recovery_classifies_before_writing_any_hold(self):
        first, = self.started()
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="unresolved", native_binding=None,
                                                       request_uuid="not-a-uuid"))
        self.change_runtime()
        self.assert_ambiguous("unresolved attempt", lambda: recover_admission(
            self.project, "objective", owner="owner", admission_id=first["admission_id"],
            worktree="current", port=self.port))

    def test_reserved_dispatch_found_needs_the_owner_then_separate_recovery_binds_it(self):
        first, = self.started()
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="reserved", native_binding=None,
                                                       request_uuid=None))
        self.journal().write_text(json.dumps({"fixture": "journal"}))
        self.change_runtime()
        error = self.assert_ambiguous("Dispatch found without a recorded native binding", lambda: recover_admission(
            self.project, "objective", owner="owner", admission_id=first["admission_id"],
            worktree="current", port=self.port))
        before = read(self.project, "objective")
        self.port.calls.clear()
        self.owner_op(self.decision(error))
        after = read(self.project, "objective")
        self.assertEqual(set(self.port.calls) - {"read_native", "show_worker", "find_worker"}, set())
        row = after["admissions"][first["admission_id"]]
        self.assertEqual((row["state"], row["native_binding"]), ("reserved", None))
        self.assertEqual(after["owner"], before["owner"])
        for state in (before, after):  # only the recorded runtime and continuity authority moved
            state["revision"] = 0
            state["checkpoint"]["native_refs"] = [{"runId": "run", "runtime": "*"}]
            state["checkpoint"].pop("continuity")
            for item in state["admissions"].values():
                item["runtime"] = "*"
        self.assertEqual(after, before)
        recovered = recover_admission(self.project, "objective", owner="owner",
                                      admission_id=first["admission_id"], worktree="current", port=self.port)
        self.assertEqual((recovered["status"], recovered["action"]), ("bound", "inspect_without_uuid"))
        self.assertNotIn("start_worker", self.port.calls)


class DisprovenContinuityTests(ContinuityCase):
    def baseline_code(self, call) -> str:
        """The path's existing refusal with continuity classification switched off."""
        with patch("pod.ledger._continuity_step", return_value=None), \
             patch("pod.ledger.continuity_locked", return_value=None), self.assertRaises(PodError) as caught:
            call()
        return caught.exception.code

    def test_each_compared_identity_fails_closed_with_the_existing_code(self):
        first, = self.started()
        self.change_runtime()
        dispatch = first["native_binding"]["dispatchId"]
        worker = dict(self.port.workers[dispatch])
        variants = {
            "run": lambda: self.port.run_binding.update(id="run-other"),
            "coordinator": lambda: self.port.run_binding.update(coordinator_handle="other-terminal"),
            "generation": lambda: self.port.run_binding.update(consumer_generation=2),
            "dispatch run": lambda: self.port.workers[dispatch].update(run="run-other"),
            "task": lambda: self.port.workers[dispatch].update(task="task-other"),
            "worktree": lambda: self.port.workers[dispatch].update(worktree="other-worktree"),
        }
        for name, vary in variants.items():
            with self.subTest(identity=name):
                self.port.run_binding = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
                self.port.workers[dispatch] = dict(worker)
                vary()
                expected = self.baseline_code(self.checkpoint_op)
                before = self.snapshot()
                self.refused(expected, self.checkpoint_op)
                self.assertEqual(self.snapshot(), before)
                decision = {"provenance": "user_direct", "instruction": "rebind anyway", "objective": "objective",
                            "recorded_runtime": "runtime", "current_runtime": "runtime-2",
                            "ambiguity": ["anything"]}
                self.refused("native_authority_unverified", self.owner_op, decision)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.history(), [])
        # A changed Dispatch or worker id in the readback is a contradiction too.
        self.port.run_binding = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        self.port.workers[dispatch] = dict(worker)
        original = ContinuityPort.show_worker
        for field in ("dispatch", "projection"):
            def contradicted(port, shown_dispatch, _field=field):
                shown = original(port, shown_dispatch)
                shown["result"][_field]["id"] = "other-id"
                return shown
            with self.subTest(identity=field), patch.object(ContinuityPort, "show_worker", contradicted):
                before = self.snapshot()
                self.refused(self.baseline_code(self.checkpoint_op), self.checkpoint_op)
                self.assertEqual(self.snapshot(), before)


class ContinuityBoundaryTests(ContinuityCase):
    def test_unchanged_runtime_runs_no_continuity_path(self):
        first, = self.started(spare=1)
        with patch("pod.ledger.classify_continuity", side_effect=AssertionError("continuity ran")):
            self.checkpoint_op()
            self.assertEqual(self.admission_op("task-2", ["S2"], boundary={"paths": ["area2"]})[0]["status"],
                             "bound")
            recover_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                              worktree="current", port=self.port)
        self.assertEqual(self.history(), [])

    def test_status_never_rebinds(self):
        self.started()
        self.change_runtime()
        before = self.snapshot()
        observed = pod_status(self.project, None, objective="objective",
                              current_run_fn=lambda: {"run": {"id": "run"}, "runtime": "runtime-2"},
                              worker_rows_fn=lambda _run: {"runtime": "runtime-2", "workers": [],
                                                           "scope": None, "complete": True})
        self.assertEqual(observed["native_settlement"], "unverified")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.history(), [])

    def test_rebind_starts_replays_creates_adopts_and_reassigns_nothing(self):
        self.started()
        owner = read(self.project, "objective")["owner"]
        self.change_runtime()
        self.checkpoint_op()
        self.assertEqual(set(self.port.calls) - {"read_native", "show_worker", "find_worker"}, set())
        self.assertEqual(read(self.project, "objective")["owner"], owner)
        self.assertEqual(len(self.port.starts), 1)

    def test_rebind_past_the_history_bound_refuses_without_a_write(self):
        self.started()
        for index in range(CONTINUITY_HISTORY):
            self.change_runtime(f"runtime-{index + 2}")
            self.checkpoint_op()
        self.assertEqual(len(self.history()), CONTINUITY_HISTORY)
        self.change_runtime("runtime-overflow")
        before = self.snapshot()
        self.refused("runtime_continuity_full", self.checkpoint_op)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

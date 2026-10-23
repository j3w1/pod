"""Derived continuation after a verified owner-authorized Orca window (pod#110), through production boundaries.

Only Orca and the host verifier's process boundary are replaced. A verified window lets a new coordinator in the
recorded worktree continue with derived authority; every other case refuses to the project lead, never the owner.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import patch

import yaml

from pod import window
from pod.config import personal_path
from pod.errors import PodError
from pod.github import repository_context
from pod.ledger import objective_root, read
from pod.operations import OrcaPort
from pod.util import context_decisions, map_decisions
from tests.kernel_support import git
from tests.test_owner_handoff import HandoffCase, SENTINEL

OLD, NEW = "runtime", "runtime-new"
# The last release before window continuation (0.21.13): its helper must refuse a record carrying the new row.
PRECEDING = "bbe1785df23bf5c7b90e3058d782e54df7abe9ee"


class WindowCase(HandoffCase):
    """A host verifier double answers through relay's real request/verdict validation."""

    def setUp(self):
        super().setUp()
        self.host = {"mode": "verified"}
        self.host_requests = []
        output = patch("pod.relay._output", side_effect=self.verifier)
        output.start(); self.addCleanup(output.stop)
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text())
        value["authorization_verifier"] = ["/synthetic/ce-pod-verifier"]
        path.write_text(yaml.safe_dump(value))

    def binding(self):
        context = repository_context(self.project)
        return {"repository": context["repository"], "repo_key": context["repo_key"],
                "path": context["worktree"], "branch": context["branch"]}

    def core(self, **extra):
        return super().core(**{"worktree": self.binding(), **extra})

    def packet(self, serves, **extra):
        return super().packet(serves, **{"worktree": self.binding(), **extra})

    def verifier(self, command, request):
        self.host_requests.append(deepcopy(request))
        if self.host["mode"] == "fail":
            from pod.relay import refusal
            raise refusal("relay_verifier_failed")
        verdict = {**request, "single_use": "op-window:effect-1", "decided_at": "2026-01-01T00:00:00Z"}
        if self.host["mode"] == "verified":
            verdict.update(verified=True, owner_source="owner DRAINED stop (synthetic)", window={
                "schema": window.SCHEMA, "operation": "op-window", "effect": "effect-1", "kind": "stop",
                "stopped_at": "2026-01-01T00:05:00Z", "old_runtime": request["orca_window"]["old_runtime"],
                "after_runtime": None, "ended_sessions": True, "loss_basis": window.LOSS_BASIS,
                **self.host.get("window", {})})
        else:
            verdict.update(verified=False, owner_source="unverified", single_use=request["reference"], window=None,
                           condition=self.host["condition"])
        return json.dumps(verdict).encode()

    @contextmanager
    def inside(self, where=None):
        old = Path.cwd()
        os.chdir(where or self.project)
        try:
            yield
        finally:
            os.chdir(old)

    def lose(self, caller="next"):
        super().lose(caller)
        self.assertEqual(self.port.runtime, NEW)

    def resume(self, *, where=None, project=None):
        with self.inside(where):
            return self.op("window-continuation", owner=os.environ["ORCA_TERMINAL_HANDLE"],
                           **({"project": str(project)} if project else {}))

    def command(self, where=None):
        from io import StringIO
        from contextlib import redirect_stdout
        from pod import cli
        out = StringIO()
        with self.inside(where), redirect_stdout(out):
            code = cli.main(["continue", "--objective", "objective", "--json"])
        return code, json.loads(out.getvalue())

    def rows(self):
        return [row for row in self.state()["checkpoint"]["continuity"]["history"]
                if row["provenance"] == window.PROVENANCE]

    def assert_lead_refusal(self, condition, call=None, *, where=None):
        before, calls = self.snapshot(), len(self.mutations)
        call = call or (lambda: self.resume(where=where))
        with self.assertRaises(PodError) as caught:
            call()
        error = caught.exception
        self.assertEqual((error.code, error.detail["condition"]), ("window_continuation_refused", condition), str(error))
        text = str(error) + json.dumps(error.detail)
        self.assertTrue(error.detail["next_action"].startswith(window.LEAD))
        for owner_question in ("Ask the Owner", "ask the Owner", "owner's direct", "user_direct"):
            self.assertNotIn(owner_question, text)
        self.assertNotIn(SENTINEL, text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.mutations), calls)
        return error

    def assert_only_continuation_changes(self, before, after, *, owner):
        before, after = deepcopy(before), deepcopy(after)
        self.assertEqual(after["owner"], owner)
        for state in (before, after):
            state.pop("revision")
            state["checkpoint"].pop("continuity")
            state["owner"] = "*"
            for ref in state["checkpoint"]["native_refs"]:
                ref["runtime"] = "*"
            for row in state["admissions"].values():
                row["runtime"] = row["owner"] = "*"
        self.assertEqual(after, before)


class ContinuationTests(WindowCase):
    def test_settled_and_reserved_admissions_continue_with_history_and_provenance_preserved(self):
        self.establish(admissions=False)
        second = self.packet(["S2"], boundary={"paths": ["area2"]})
        with patch.object(OrcaPort, "start_worker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.start("reserved", second)
        settled = self.start("settled", self.frozen)["admission"]; self.settle(settled)
        self.lose()
        before = self.state()
        decisions = (context_decisions(before), map_decisions(before["checkpoint"]))
        history = deepcopy(before["checkpoint"]["continuity"]["history"])
        status = self.status()
        self.assertEqual(status["window_continuation"]["status"], "window_available")
        self.assertIn("pod continue --objective objective", status["next_action"])
        self.assertEqual(status["owner_handoff"]["status"], "handoff_available")  # R105 stays available, unchanged.
        result = self.resume()
        self.assertEqual(result["status"], "continued")
        entry = result["window_continuation"]
        self.assertEqual((entry["state"], entry["from_owner"], entry["to_owner"], entry["from_runtime"], entry["to_runtime"]),
                         ("done", "owner", "next", OLD, NEW))
        self.assertEqual(entry["run"], {"id": "run", "before": {"coordinator": "owner", "generation": 1},
                                        "after": {"coordinator": "next", "generation": 2}})
        verdicts = {row["id"]: (row["state"], row["verdict"]) for row in entry["admissions"]}
        self.assertEqual(verdicts, {settled["admission_id"]: ("bound", "settled"),
                                    next(key for key, row in before["admissions"].items() if row["state"] == "reserved"):
                                        ("reserved", "absent")})
        self.assertEqual(entry["window"]["window"]["operation"], "op-window")
        self.assertNotIn("decision", entry)
        after = self.state()
        self.assertEqual(after["checkpoint"]["continuity"]["history"][:len(history)], history)
        self.assertEqual(after["checkpoint"]["continuity"]["binding"], {"run": "run", "coordinator": "next", "generation": 2})
        self.assert_only_continuation_changes(before, after, owner="next")
        self.assertEqual((context_decisions(after), map_decisions(after["checkpoint"])), decisions)
        self.assertEqual(self.mutations, [["orchestration", "run-use", "--id", "run", "--json"]])
        self.assertEqual(self.host_requests[0]["orca_window"], {"old_runtime": OLD, "current_runtime": NEW,
                                                               "coordinator": "owner"})
        self.assertEqual(self.host_requests[0]["reference"], "orca-runtime:" + OLD)
        # The new coordinator now passes the authority join; the map's own settled-admission rule answers next.
        with self.assertRaises(PodError) as caught:
            self.op("checkpoint", owner="next", value=self.core())
        self.assertNotIn(caught.exception.code, ("native_authority_unverified", "coordinator_conflict"))
        self.assertIn("active_admission_settled", str(caught.exception))

    def test_unknown_work_stays_unknown_and_nothing_is_stamped(self):
        self.establish(admissions=False)
        running = self.start("running", self.frozen)["admission"]
        second = self.packet(["S2"], boundary={"paths": ["area2"]})
        with patch.object(OrcaPort, "start_worker", side_effect=PodError("native_effect_uncertain", "transport unavailable")):
            with self.assertRaises(PodError): self.start("unresolved", second)
        self.lose()
        before, pending = self.state(), self.status()["pending_admissions"]
        entry = self.resume()["window_continuation"]
        verdicts = {row["state"]: (row["verdict"], row["reason"]) for row in entry["admissions"]}
        self.assertEqual(verdicts["bound"], ("unknown", "not settled at readback"))
        self.assertEqual(verdicts["unresolved"], ("unknown", "unresolved attempt"))
        after = self.state()
        self.assertEqual({k: row["state"] for k, row in after["admissions"].items()},
                         {k: row["state"] for k, row in before["admissions"].items()})
        self.assertEqual(after["admissions"][running["admission_id"]]["native_binding"],
                         before["admissions"][running["admission_id"]]["native_binding"])
        self.assertEqual(self.status()["pending_admissions"], pending)

    def test_one_winner_among_claimants_and_idempotent_replay(self):
        self.establish()
        self.lose()
        self.mode = "lost"  # run-use lands, but its answer never arrives: the attempt stays pending.
        code, refused = self.command()
        self.assertEqual((code, refused["status"], refused["condition"]), (1, "refused", "run_use_failed"))
        self.assertEqual(self.rows()[-1]["state"], "pending")
        self.mode = "success"
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.assert_lead_refusal("pending_other_caller")
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.assertEqual(self.resume()["status"], "continued")  # Resume: no second run-use.
        self.assertEqual(len(self.mutations), 1)
        before = self.snapshot()
        code, replay = self.command()
        self.assertEqual((code, replay["status"]), (0, "already_continued"))
        self.assertEqual(self.snapshot(), before)
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        # The record now names the live winner at the current runtime: no window question remains.
        self.assert_lead_refusal("runtime_unchanged")
        self.assertEqual(len(self.rows()), 1)

    def test_a_lost_pending_claimant_is_replaced_without_extra_history(self):
        self.establish()
        self.lose()
        def interrupted(argv, **kwargs):
            raise KeyboardInterrupt  # The process dies after the pending write, before run-use runs.
        with patch("pod.orca.mutate_command", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.resume()
        self.assertEqual(self.rows()[-1]["state"], "pending")
        # The interrupted claimant is gone; another terminal replaces its pending attempt with no extra history.
        self.terminals["next"] = "stale"; self.terminals["third"] = "live"
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.mutations.clear()
        with patch("pod.orca.mutate_command", side_effect=self.mutate):
            entry = self.resume()["window_continuation"]
        self.assertEqual((entry["state"], entry["to_owner"]), ("done", "third"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.state()["owner"], "third")
        self.assertEqual([(item["to_owner"], item["abort_reason"]) for item in entry["attempts"]],
                         [("next", "pending caller handle stale or gone")])

    def test_a_refused_run_use_ends_the_attempt_and_the_retry_keeps_it(self):
        self.establish()
        self.lose()
        self.mode = "refused"
        code, refused = self.command()
        self.assertEqual((code, refused["condition"], refused["continuation_state"]), (1, "run_use_failed", "aborted"))
        self.mode = "success"
        entry = self.resume()["window_continuation"]
        self.assertEqual(entry["state"], "done")
        self.assertEqual([(item["to_owner"], item["abort_reason"]) for item in entry["attempts"]],
                         [("next", "run-use refused")])

    def test_the_initial_readback_stays_beside_the_completion_readback(self):
        self.establish()
        self.lose()
        def landing(argv, **kwargs):
            result = self.mutate(argv, **kwargs)
            self.settle(self.admission)  # The worker settles between the pending and the done write.
            return result
        with patch("pod.orca.mutate_command", side_effect=landing):
            entry = self.resume()["window_continuation"]
        (initial,), (final,) = entry["admissions"], entry["completion"]["admissions"]
        self.assertEqual((initial["verdict"], initial["outcome"]), ("unknown", "in_progress"))
        self.assertEqual((final["verdict"], final["outcome"]), ("settled", "succeeded"))
        self.assertGreaterEqual(entry["completion"]["at"], entry["at"])

    def test_a_retry_after_an_ended_attempt_keeps_every_readback(self):
        self.establish()
        self.lose()
        self.mode = "refused"
        self.command()
        first = deepcopy(self.rows()[-1])
        self.assertEqual((first["state"], first["admissions"][0]["verdict"]), ("aborted", "unknown"))
        self.assertEqual(first["initial"], {"at": first["at"], "runtime": NEW, "admissions": first["admissions"]})
        self.settle(self.admission)  # The worker settles before the retry reads it again.
        self.mode = "success"
        from pod import history
        with patch("pod.history.prepare_context", wraps=history.prepare_context) as preflight:
            entry = self.resume()["window_continuation"]
        self.assertEqual(entry["initial"], first["initial"])  # The transition's first readback never changes.
        self.assertEqual(entry["attempts"], [{"to_owner": "next", "at": first["at"], "runtime": NEW,
                                              "abort_reason": "run-use refused", "run_before": first["run"]["before"],
                                              "admissions": first["admissions"]}])
        # The capacity preflight sized the done record with the whole expanded row before the receipt was consumed.
        (call,) = [call for call in preflight.call_args_list if not call.kwargs]  # Not the writer's own compaction.
        projected = call.args[1]["checkpoint"]["continuity"]["history"][-1]
        self.assertEqual((projected["state"], projected["initial"], projected["attempts"]),
                         ("done", first["initial"], entry["attempts"]))
        self.assertEqual([item["verdict"] for item in entry["admissions"]], ["settled"])
        self.assertEqual([item["verdict"] for item in entry["completion"]["admissions"]], ["settled"])

    def test_a_lost_claimant_is_replaced_across_another_runtime_change_keeping_its_readback(self):
        self.establish()
        self.lose()
        self.mode = "lost"  # run-use lands, but its answer never arrives: the attempt stays pending.
        self.command()
        first = deepcopy(self.rows()[-1])
        self.assertEqual((first["state"], first["admissions"][0]["verdict"]), ("pending", "unknown"))
        # Orca restarts again: the claimant's session ends, the worker settles and a third terminal continues.
        self.terminals["next"] = "stale"
        self.port.runtime = self.port.placement["runtime"] = "runtime-later"
        self.settle(self.admission)
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.mode = "success"
        entry = self.resume()["window_continuation"]
        self.assertEqual((entry["state"], entry["to_owner"], entry["from_runtime"], entry["to_runtime"]),
                         ("done", "third", OLD, "runtime-later"))
        self.assertEqual(entry["initial"], first["initial"])
        self.assertEqual(entry["attempts"], [{"to_owner": "next", "at": first["at"], "runtime": NEW,
                                              "abort_reason": "pending caller handle stale or gone",
                                              "run_before": first["run"]["before"], "admissions": first["admissions"]}])
        self.assertEqual([(item["verdict"], item["runtime"]) for item in entry["admissions"]],
                         [("settled", "runtime-later")])
        self.assertEqual((len(self.rows()), len(self.mutations)), (1, 2))

    def test_an_interruption_after_the_receipt_recovers_after_another_restart(self):
        self.establish()
        self.lose()
        before, files = self.state(), self.snapshot()
        with patch("pod.ledger._write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.resume()
        self.assertEqual(self.state(), before)  # Only the receipt landed; the record is unchanged.
        added = set(self.snapshot()) - set(files)
        self.assertEqual([Path(name).parent.name for name in added], [".relay-authorizations"])
        self.port.runtime = self.port.placement["runtime"] = "runtime-later"
        entry = self.resume()["window_continuation"]  # The receipt binds the transition, not the current runtime.
        self.assertEqual((entry["state"], entry["to_runtime"]), ("done", "runtime-later"))
        self.assertEqual((len(self.rows()), len(self.mutations)), (1, 1))

    def test_an_interrupted_done_write_resumes_without_another_run_use(self):
        self.establish()
        self.lose()
        from pod import ledger
        original, written = ledger._write, []
        def write(path, state, **kwargs):
            written.append(state["checkpoint"]["continuity"]["history"][-1]["state"])
            if len(written) == 2:
                raise KeyboardInterrupt  # The process dies before the done write lands.
            return original(path, state, **kwargs)
        with patch("pod.ledger._write", side_effect=write):
            with self.assertRaises(KeyboardInterrupt): self.resume()
        self.assertEqual(written, ["pending", "done"])
        self.assertEqual((self.rows()[-1]["state"], len(self.mutations)), ("pending", 1))
        self.assertEqual(self.resume()["status"], "continued")
        self.assertEqual((len(self.rows()), len(self.mutations)), (1, 1))

    def test_the_preceding_helper_refuses_a_record_carrying_the_row(self):
        self.establish()
        self.lose()
        self.resume()
        old = self.root / "preceding-helper"
        old.mkdir()
        archive = old / "bundle.tar"
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), PRECEDING, "VERSION",
                        "skills/pod"], check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(old, filter="data")
        script = ("from pathlib import Path\nimport pod\nfrom pod.errors import PodError\nfrom pod.ledger import read\n"
                  "try:\n    read(Path.cwd(), 'objective')\nexcept PodError as exc:\n    print(pod.__version__, exc.code)\n"
                  "else:\n    print(pod.__version__, 'accepted')\n")
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-c", script], cwd=self.project, capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": str(old / "skills"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["0.21.13", "state_unsupported"])
        self.assertEqual(self.snapshot(), before)

    def test_a_pending_continuation_fences_a_successor_claim(self):
        self.establish()
        self.lose()
        with patch("pod.orca.mutate_command", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.resume()
        self.assertEqual(self.rows()[-1]["state"], "pending")
        from pod.internal import run as internal
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.current = {"id": "run-s", "coordinator_handle": "third", "consumer_generation": 1}
        internal("checkpoint", {"project": str(self.project), "objective": "successor", "owner": "third",
                                "value": self.core(obligations=[self.criterion()],
                                                   governance={"base_ref": "refs/remotes/origin/target"})})
        verdict = internal("succession", {"project": str(self.project), "objective": "successor", "owner": "third",
                                          "predecessor": "objective"})
        self.assertEqual(verdict["condition"], "predecessor_continuation_pending")
        self.assertEqual(self.rows()[-1]["state"], "pending")

    def test_the_pending_claimant_sees_its_own_resumable_step(self):
        self.establish()
        self.lose()
        self.mode = "lost"
        self.command()
        self.mode = "success"
        own = self.status()["window_continuation"]
        self.assertEqual((own["status"], own["caller"], own["pending"]["to_owner"]),
                         ("window_available", {"ready": True}, "next"))
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        other = self.status()["window_continuation"]
        self.assertEqual((other["status"], other["caller"]["condition"]), ("window_available", "pending_other_caller"))

    def test_same_caller_resumes_after_an_interruption_before_run_use(self):
        self.establish()
        self.lose()
        with patch("pod.orca.mutate_command", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.resume()
        pending = deepcopy(self.rows()[-1])
        entry = self.resume()["window_continuation"]
        self.assertEqual((entry["state"], entry["at"], entry["to_owner"]), ("done", pending["at"], "next"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.mutations, [["orchestration", "run-use", "--id", "run", "--json"]])

    def test_a_window_with_two_runtime_changes_continues_from_the_first_stop(self):
        self.establish()
        self.lose()
        self.host["window"] = {"kind": "stop", "after_runtime": "runtime-mid"}  # Started, then restarted again.
        entry = self.resume()["window_continuation"]
        self.assertEqual((entry["from_runtime"], entry["to_runtime"], entry["window"]["window"]["after_runtime"]),
                         (OLD, NEW, "runtime-mid"))

    def test_receipts_are_scoped_per_repository_and_objective(self):
        self.establish()
        value = {"action": window.ACTION, "repo_key": "a" * 64, "objective": "objective", "authorization": "op:effect",
                 "operation": "op", "effect": "effect", "old_runtime": OLD, "coordinator": "owner", "run": "run",
                 "generation": 1}
        window._receipt(self.project, value, consume=True)
        window._receipt(self.project, value, consume=True)  # The identical transition may finish.
        window._receipt(self.project, {**value, "repo_key": "b" * 64}, consume=True)  # Another checkout's same name.
        with self.assertRaises(PodError) as caught:
            window._receipt(self.project, {**value, "generation": 2}, consume=True)
        self.assertEqual(caught.exception.detail["condition"], "window_already_used")

    def test_status_list_shows_the_supported_step_and_lead_conditions(self):
        self.establish()
        self.lose()
        from pod.listing import objective_list
        with self.inside():
            row = next(row for row in objective_list(self.project)["objectives"] if row["objective"] == "objective")
        self.assertEqual(row["continuation"]["status"], "available")
        self.assertEqual(row["continuation"]["command"], "pod continue --objective objective --json")
        self.assertEqual(row["continuation"]["cwd"], repository_context(self.project)["worktree"])
        self.host.update(mode="denied", condition="window_not_session_ending")
        with self.inside():
            row = next(row for row in objective_list(self.project)["objectives"] if row["objective"] == "objective")
        self.assertEqual((row["continuation"]["status"], row["continuation"]["condition"]),
                         ("unavailable", "window_not_session_ending"))
        self.assertTrue(row["continuation"]["next_action"].startswith(window.LEAD))

    def test_a_pending_continuation_holds_an_owner_handoff(self):
        self.establish()
        self.lose()
        self.mode = "lost"
        self.command()
        self.mode = "success"
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        observed = self.status()["owner_handoff"]
        self.assertEqual((observed["status"], observed["condition"]), ("handoff_unavailable", "window_continuation_pending"))
        before = self.snapshot()
        with self.assertRaises(PodError):
            self.handoff({"provenance": "user_direct", "instruction": "Make this terminal the coordinator",
                          **observed["facts"]})
        self.assertEqual(self.snapshot(), before)

    def test_discovery_outside_orca_and_within_its_budget(self):
        self.establish()
        self.lose()
        rows = [{"objective": "objective", "phase": "open"}]
        with patch.dict(os.environ), patch("pod.orca.read_command", side_effect=AssertionError("no Orca read")):
            os.environ.pop("ORCA_TERMINAL_HANDLE")
            window.discover(rows, project=self.project)
        self.assertNotIn("continuation", rows[0])  # Outside Orca the list stays exactly record-only.
        rows = [{"objective": "objective", "phase": "open"}]
        with patch.object(window, "DISCOVERY_CALLS", 0):
            window.discover(rows, project=self.project)
        self.assertEqual((rows[0]["continuation"]["status"], rows[0]["continuation"]["condition"]),
                         ("unknown", "discovery_budget"))
        self.assertEqual(self.host_requests, [])

    def test_a_failed_or_slow_orca_read_degrades_the_list_to_unknown(self):
        self.establish()
        self.lose()
        from pod.listing import objective_list
        with self.inside(), patch("pod.window.DISCOVERY_RUNTIME_TIMEOUT", 3), \
                patch("pod.orca.current_run", side_effect=PodError("orca_unavailable", "timed out")):
            listed = objective_list(self.project)
        row = next(row for row in listed["objectives"] if row["objective"] == "objective")
        self.assertEqual(listed["status"], "listed")
        self.assertEqual((row["continuation"]["status"], row["continuation"]["condition"]), ("unknown", "orca_unreadable"))
        self.assertTrue(row["continuation"]["message"].startswith("continuation unknown: "))

    def test_the_budget_bounds_reads_inside_a_row_and_skips_the_verifier(self):
        self.establish()
        self.lose()
        class Clock(window.Budget):
            def __init__(self, seconds):
                self.now = 0.0
                self._clock = self.tick
                self.deadline = seconds
            def tick(self):
                self.now += 2.0  # Each native step costs two synthetic seconds; nothing sleeps.
                return self.now
        rows = [{"objective": "objective", "phase": "open"}]
        with patch.object(window, "Budget", Clock):
            window.discover(rows, project=self.project)
        self.assertEqual((rows[0]["continuation"]["status"], rows[0]["continuation"]["condition"]),
                         ("unknown", "discovery_budget"))
        self.assertEqual(self.host_requests, [])

    def test_stored_rows_validate_and_succession_lineage_recognizes_them(self):
        self.establish()
        self.lose()
        self.resume()
        state = self.state()
        row = deepcopy(self.rows()[-1])
        for broken in ({**row, "decision": {"provenance": "user_direct"}}, {**row, "state": "unknown"},
                       {**row, "window": {**row["window"], "verified": False}}):
            with self.subTest(field=sorted(set(broken) ^ set(row)) or "value"):
                with self.assertRaises(PodError) as caught:
                    window.validate_entry(broken)
                self.assertEqual(caught.exception.code, "state_unsupported")
        attempt = {"to_owner": "third", "at": row["at"], "runtime": NEW, "abort_reason": "run-use refused",
                   "run_before": row["run"]["before"], "admissions": row["admissions"]}
        window.validate_entry({**row, "attempts": [attempt]})
        for name, broken in (("initial missing", {key: value for key, value in row.items() if key != "initial"}),
                             ("initial runtime", {**row, "initial": {**row["initial"], "runtime": ""}}),
                             ("initial readback", {**row, "initial": {**row["initial"], "admissions": [{}]}}),
                             ("attempt runtime", {**row, "attempts": [{**attempt, "runtime": None}]}),
                             ("attempt readback", {**row, "attempts": [{k: v for k, v in attempt.items()
                                                                        if k != "admissions"}]}),
                             ("attempt bound", {**row, "attempts": [attempt] * (window.MAX_ATTEMPTS + 1)})):
            with self.subTest(shape=name):
                with self.assertRaises(PodError) as caught:
                    window.validate_entry(broken)
                self.assertEqual(caught.exception.code, "state_unsupported")
        from pod.succession import _handed_over
        self.assertTrue(_handed_over(state, "owner", "next", since="2000-01-01T00:00:00+00:00"))


class RefusalTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.establish()
        self.lose()

    def test_host_refusals_reach_the_lead(self):
        for condition in window.HOST_CONDITIONS:
            with self.subTest(condition=condition):
                self.host.update(mode="denied", condition=condition)
                self.assert_lead_refusal(condition)
        self.host.update(mode="fail")
        self.assert_lead_refusal("window_unverified")
        self.host.update(mode="verified", window={"old_runtime": "another-runtime"})
        self.assert_lead_refusal("window_unverified")
        self.host.update(window={"ended_sessions": False})
        self.assert_lead_refusal("window_unverified")
        self.host.update(window={"stopped_at": "2025-12-31T00:00:00Z"})  # Stopped before it was authorized.
        self.assert_lead_refusal("window_unverified")
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text()); value.pop("authorization_verifier"); path.write_text(yaml.safe_dump(value))
        self.assert_lead_refusal("window_verifier_unavailable")

    def test_loss_outside_the_window_and_runtime_unchanged(self):
        self.terminals["owner"] = "exited"
        self.assert_lead_refusal("coordinator_lost_outside_window")
        self.terminals["owner"] = "live"
        self.assert_lead_refusal("owner_live")
        self.terminals["owner"] = "stale"
        self.port.runtime = OLD; self.port.placement["runtime"] = OLD
        self.assert_lead_refusal("runtime_unchanged")

    def test_a_denied_window_leads_every_non_owner_refusal_with_a_lead_task(self):
        self.host.update(mode="denied", condition="window_not_authorized")
        for name, call in self.boundary_calls("next").items():
            with self.subTest(boundary=name):
                with self.assertRaises(PodError) as caught:
                    call()
                detail = caught.exception.detail
                self.assertEqual(detail["window_continuation"]["condition"], "window_not_authorized")
                self.assertTrue(detail["next_action"].startswith(window.LEAD), detail["next_action"])
                self.assertNotIn("Ask the Owner", detail["next_action"])
        status = self.status()
        self.assertTrue(status["next_action"].startswith(window.LEAD))
        self.assertEqual(status["owner_handoff"]["status"], "handoff_available")  # R105 stays, optional.

    def test_discovery_checks_the_recorded_worktree_itself(self):
        from pod.listing import objective_list
        git(self.project, "checkout", "-q", "-b", "renamed")
        with self.inside():
            row = next(row for row in objective_list(self.project)["objectives"] if row["objective"] == "objective")
        self.assertEqual((row["continuation"]["status"], row["continuation"]["condition"]),
                         ("unavailable", "worktree_changed"))

    def test_moved_repository_and_changed_worktree(self):
        moved = self.root / "moved"
        os.rename(self.project, moved)
        try:
            self.assert_lead_refusal("objective_unknown", lambda: self.resume(where=moved, project=moved))
            self.assertFalse(objective_root(moved, "objective").exists())
        finally:
            os.rename(moved, self.project)
        other = self.root / "other"
        git(self.project, "worktree", "add", "-q", "-b", "elsewhere", str(other))
        self.assert_lead_refusal("worktree_changed", lambda: self.resume(where=other, project=other))
        git(self.project, "checkout", "-q", "-b", "renamed")
        self.assert_lead_refusal("worktree_changed")

    def test_run_and_work_identity_refusals(self):
        self.current.update(coordinator_handle="foreign", consumer_generation=4)
        self.assert_lead_refusal("run_rebound_elsewhere")
        self.current.update(coordinator_handle="owner", consumer_generation=1)
        original = self.native_read
        def missing(argv, **kwargs):
            if argv[:2] == ["orchestration", "run-show"]:
                raise PodError("orca_read_failed", "Orca read did not succeed", {"native_code": "run_not_found"})
            return original(argv, **kwargs)
        with patch("pod.orca.read_command", side_effect=missing):
            self.assert_lead_refusal("run_missing")
        next(iter(self.port.workers.values())).update(worktree="different")
        self.assert_lead_refusal("identity_differs")

    def test_a_caller_in_the_wrong_place_never_ends_a_pending_attempt(self):
        self.mode = "lost"
        self.command()
        self.mode = "success"
        self.assertEqual(self.rows()[-1]["state"], "pending")
        other = self.root / "other"
        git(self.project, "worktree", "add", "-q", "-b", "elsewhere", str(other))
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.assert_lead_refusal("pending_other_caller", lambda: self.resume(where=other, project=other))
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.assert_lead_refusal("worktree_changed", lambda: self.resume(where=other, project=other))
        self.assertEqual(self.rows()[-1]["state"], "pending")
        self.assertEqual(self.resume()["status"], "continued")

    def test_status_elsewhere_reports_objective_eligibility_and_caller_readiness(self):
        other = self.root / "other"
        git(self.project, "worktree", "add", "-q", "-b", "elsewhere", str(other))
        old = Path.cwd(); os.chdir(other)
        try:
            from io import StringIO
            from contextlib import redirect_stdout
            from pod import cli
            out = StringIO()
            with redirect_stdout(out):
                cli.main(["status", "--objective", "objective", "--json"])
        finally:
            os.chdir(old)
        block = json.loads(out.getvalue())["window_continuation"]
        self.assertEqual(block["status"], "window_available")
        self.assertEqual(block["cwd"], repository_context(self.project)["worktree"])
        self.assertEqual((block["caller"]["ready"], block["caller"]["condition"]), (False, "worktree_changed"))

    def test_pending_owner_handoff_holds_window_continuation(self):
        self.mode = "lost"
        with self.assertRaises(PodError):
            self.handoff()
        self.assertEqual(self.state()["checkpoint"]["continuity"]["history"][-1]["state"], "pending")
        self.mode = "success"
        self.current.update(coordinator_handle="owner", consumer_generation=1)
        self.assert_lead_refusal("handoff_pending")


class DiscoveryTests(WindowCase):
    """The list's objective-level check: one deadline for every read, the attempt bound, and unknown never refused."""

    def listed(self):
        rows = [{"objective": "objective", "phase": "open"}]
        window.discover(rows, project=self.project)
        return rows[0].get("continuation")

    def test_one_deadline_stops_worker_pages_and_clamps_every_read(self):
        self.establish(admissions=False)
        with patch.object(OrcaPort, "start_worker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.start("reserved", self.frozen)
        self.lose()
        clock, reads, original = {"now": 0.0}, [], self.native_read

        class Clock(window.Budget):
            def __init__(self, seconds):
                self._clock = lambda: clock["now"]
                self.deadline = clock["now"] + seconds

        def read(argv, **kwargs):
            reads.append((argv[:2], clock["now"], kwargs.get("timeout")))
            clock["now"] += 1.0  # Each actual read costs one synthetic second; nothing sleeps.
            if argv[:2] != ["orchestration", "worker-list"]:
                return original(argv, **kwargs)
            page = sum(name == ["orchestration", "worker-list"] for name, _, _ in reads)
            return {"runtime": self.port.runtime, "result": {
                "workers": [{"dispatchId": f"other-dispatch-{page}", "taskId": f"other-task-{page}"}],
                "scope": {"run": "run", "source": "flag"}, "page": {"hasMore": True, "nextCursor": f"page-{page}"}}}

        with patch.object(window, "Budget", Clock), patch("pod.orca.read_command", side_effect=read):
            continuation = self.listed()
        self.assertEqual((continuation["status"], continuation["condition"]), ("unknown", "discovery_budget"))
        self.assertEqual(continuation["message"], "continuation unknown: the bounded check did not finish")
        pages = [start for name, start, _ in reads if name == ["orchestration", "worker-list"]]
        self.assertGreater(len(pages), 1)  # A genuinely multi-page reserved lookup, stopped by the deadline.
        deadline = window.DISCOVERY_SECONDS
        self.assertTrue(all(start < deadline and 0 < timeout <= deadline - start for _, start, timeout in reads), reads)
        self.assertEqual(reads[0][2], window.DISCOVERY_RUNTIME_TIMEOUT)  # The first runtime read is inside it too.
        self.assertEqual(self.host_requests, [])

    def test_a_read_failing_after_the_runtime_read_lists_unknown_not_a_refusal(self):
        self.establish()
        self.lose()
        original = self.native_read

        def failing_run_show(argv, **kwargs):
            if argv[:2] == ["orchestration", "run-show"]:
                raise PodError("orca_unavailable", SENTINEL)  # Timed out after the runtime read succeeded.
            return original(argv, **kwargs)

        with patch("pod.orca.read_command", side_effect=failing_run_show):
            continuation = self.listed()
        self.assertEqual((continuation["status"], continuation["condition"]), ("unknown", "run_unverified"))
        self.assertEqual(continuation["message"],
                         "continuation unknown: the objective's Run could not be read (orca_unavailable)")
        self.assertEqual(continuation["next_action"], "pod status --objective objective")
        self.terminals["owner"] = "unreadable"
        self.assertEqual(self.listed()["status"], "unknown")
        self.terminals["owner"] = "stale"
        self.host.update(mode="fail")
        self.assertEqual((self.listed()["status"], self.listed()["condition"]), ("unknown", "window_unverified"))
        # A proved refusal still lists as unavailable with its lead task.
        self.host.update(mode="verified")
        self.terminals["owner"] = "live"
        continuation = self.listed()
        self.assertEqual((continuation["status"], continuation["condition"]), ("unavailable", "owner_live"))
        self.assertTrue(continuation["next_action"].startswith(window.LEAD))
        self.assertNotIn(SENTINEL, json.dumps(continuation))

    def test_exhausted_attempts_are_never_advertised(self):
        self.establish()
        self.lose()
        self.mode = "refused"
        for _ in range(window.MAX_ATTEMPTS + 1):
            code, refused = self.command()
            self.assertEqual((code, refused["condition"]), (1, "run_use_failed"))
        row = self.rows()[-1]
        self.assertEqual((row["state"], len(row["attempts"])), ("aborted", window.MAX_ATTEMPTS))
        self.mode = "success"
        requests = len(self.host_requests)
        self.assertEqual((self.listed()["status"], self.listed()["condition"]), ("unavailable", "attempts_full"))
        status = self.status()["window_continuation"]
        self.assertEqual((status["status"], status["condition"]), ("window_unavailable", "attempts_full"))
        self.assert_lead_refusal("attempts_full")
        self.assertEqual(len(self.host_requests), requests)  # Refused before any verifier call.

    def test_an_exhausted_pending_attempt_resumes_for_its_claimant_and_is_never_replaced(self):
        self.establish()
        self.lose()
        self.mode = "refused"
        for _ in range(window.MAX_ATTEMPTS):
            self.command()
        self.mode = "lost"  # The fifth attempt's run-use lands, but its answer never arrives.
        self.command()
        pending = deepcopy(self.rows()[-1])
        self.assertEqual((pending["state"], len(pending["attempts"])), ("pending", window.MAX_ATTEMPTS))
        self.mode = "success"
        # The claimant's own rerun is no new attempt, so it stays available.
        self.assertEqual(self.listed()["status"], "available")
        own = self.status()["window_continuation"]
        self.assertEqual((own["status"], own["caller"]), ("window_available", {"ready": True}))
        # With the claimant lost, a replacement would need a fifth earlier attempt.
        self.terminals["next"] = "stale"
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.assertEqual((self.listed()["status"], self.listed()["condition"]), ("unavailable", "attempts_full"))
        self.assertEqual(self.status()["window_continuation"]["condition"], "attempts_full")
        self.assert_lead_refusal("attempts_full")
        self.assertEqual(self.rows()[-1], pending)
        self.terminals["next"] = "live"
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        entry = self.resume()["window_continuation"]
        self.assertEqual((entry["state"], len(entry["attempts"]), len(self.mutations)), ("done", window.MAX_ATTEMPTS, 5))

    def test_the_list_reads_no_configuration_outside_orca_and_only_the_verifier_setting_inside(self):
        self.establish()
        self.lose()
        from pod import config
        from pod.listing import objective_list

        def listed():
            with self.inside(), patch("pod.config._read_bytes", wraps=config._read_bytes) as reads, \
                    patch("pod.orca.read_command", side_effect=AssertionError("no Orca read")):
                rows = objective_list(self.project)["objectives"]
            return next(row for row in rows if row["objective"] == "objective"), [call.args[0] for call in
                                                                                 reads.call_args_list]

        with patch.dict(os.environ):
            os.environ.pop("ORCA_TERMINAL_HANDLE")
            row, reads = listed()
        self.assertEqual(reads, [])  # Outside Orca: no configuration, Orca or verifier read.
        self.assertNotIn("continuation", row)
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text())
        value.pop("authorization_verifier")
        path.write_text(yaml.safe_dump(value))
        row, reads = listed()
        self.assertEqual(reads, [path])  # Inside Orca without a verifier: that one setting, and no Orca read.
        self.assertNotIn("continuation", row)
        self.assertEqual(self.host_requests, [])

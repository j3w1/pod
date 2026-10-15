"""Regression controls for bounded tick recovery and truthful status/readback."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from pod import pod_ticks
from pod.errors import PodError
from pod.github import MUTATION_TIMEOUT_S, issue_intake
from pod.ledger import objective_root, read
from pod.status import status
from tests.pod_tick_support import TickCase


class StatusCorrections(TickCase):
    def test_mapless_issue_bound_status_cli_exits_zero(self):
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.op("checkpoint", value=self.core(objective_source=source))
        before = self.saved()
        native = self.root / "fixture-orca"
        native.write_text(f"#!{sys.executable}\n" + '''import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fixture-only")
    raise SystemExit(0)
if args[:2] == ["orchestration", "run-current"]:
    result = {"run": {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}}
elif args[:2] == ["orchestration", "worker-list"]:
    result = {"workers": [], "scope": {"run": "run", "source": "flag"}, "page": {"hasMore": False}}
elif args == ["status", "--json"]:
    result = {"runtime": {"capabilities": [], "state": "running"}}
else:
    raise AssertionError(args)
print(json.dumps({"ok": True, "result": result, "_meta": {"runtimeId": "runtime"}}))
''')
        native.chmod(0o700)
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "skills"),
                   ORCA_CLI_COMMAND=str(native))
        result = subprocess.run([sys.executable, "-m", "pod", "status", "--objective", "objective", "--json"],
                                cwd=self.project, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        view = json.loads(result.stdout)
        self.assertEqual(view["pod_ticks"]["status"], "observed")
        self.assertEqual(view["pod_ticks"]["lines"][0]["criteria"], [])
        self.assertEqual(self.saved(), before)

    def test_kernel_failure_never_fabricates_due_untick(self):
        self.bind(); self.prove(); self.tick()
        self.assertEqual(self.line()["status"], "current")
        before, calls = self.saved(), list(self.issue.calls)
        with patch("pod.ledger.kernel_view", side_effect=PodError("governance_changed", "view unavailable")):
            view = status(self.project, None, objective="objective")
        self.assertEqual(view["pod_ticks"], {"status": "unavailable", "error": "governance_changed", "lines": []})
        self.assertEqual(view["obligations"]["error"], "governance_changed")
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.calls, calls)


class ConcurrentCorrections(TickCase):
    def concurrent_callers(self, *, landed):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments) if landed else None
            before, calls = self.saved(), list(self.issue.calls)
            for _ in range(2):
                exc = self.failure("pod_tick_pending", self.tick)
                self.assertIn("in flight", str(exc))
            self.assertEqual(self.saved(), before)
            self.assertEqual(self.issue.calls, calls)
            return held if landed else original(**arguments)
        self.issue.issue_body_write = send
        with patch("pod.pod_ticks.time", return_value=1000):
            result = self.tick()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["receipt"]["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertIsNone(self.line()["blocked"])

    def test_in_flight_callers_before_landing_never_send_again_P1(self):
        self.concurrent_callers(landed=False)

    def test_in_flight_callers_after_landing_preserve_held_success_P2(self):
        self.concurrent_callers(landed=True)

    def test_expired_recovery_rebind_then_holder_success_is_E1(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments)
            self.issue._readback_pending = False
            with patch("pod.pod_ticks.time", return_value=1000 + MUTATION_TIMEOUT_S + pod_ticks.INTENT_MARGIN_S - 0.001):
                self.failure("pod_tick_pending", self.tick)
            with patch("pod.pod_ticks.time", return_value=1000 + MUTATION_TIMEOUT_S + pod_ticks.INTENT_MARGIN_S):
                recovered = self.tick()
            self.assertEqual(recovered["status"], "unconfirmed")
            self.assertEqual(recovered["receipt"]["authorship"], "ambiguous")
            return held
        self.issue.issue_body_write = send
        with patch("pod.pod_ticks.time", return_value=1000):
            result = self.tick()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["receipt"]["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertIsNone(self.line()["blocked"])

    def test_stale_result_cannot_replace_a_newer_completed_request(self):
        self.bind(); self.prove(); self.tick()
        stale = deepcopy(self.line()["latest_request"])
        self.tick()
        before = self.saved()
        self.failure("pod_tick_pending", pod_ticks._record, self.project, "objective", "owner", "1", stale)
        self.assertEqual(self.saved(), before)
        self.assertNotEqual(self.line()["latest_request"]["id"], stale["id"])

    def test_recovery_retains_success_recorded_during_its_read(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        issue_read = self.issue.issue
        def send(**arguments):
            held = original(**arguments)
            self.issue._readback_pending = False
            intent = deepcopy(read(self.project, "objective")["pod_ticks"]["requests"]["1"])
            def read_after_held(**query):
                intent.update(phase="result", result=held)
                pod_ticks._record(self.project, "objective", "owner", "1", intent)
                return issue_read(**query)
            with patch.object(self.issue, "issue", side_effect=read_after_held), \
                    patch("pod.pod_ticks.time", return_value=1000 + MUTATION_TIMEOUT_S + pod_ticks.INTENT_MARGIN_S):
                recovered = self.tick()
            self.assertEqual(recovered["status"], "confirmed")
            self.assertEqual(recovered["receipt"]["result"], held)
            return held
        self.issue.issue_body_write = send
        with patch("pod.pod_ticks.time", return_value=1000):
            result = self.tick()
        self.assertEqual(result["receipt"]["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)

    def test_earlier_intent_without_time_recovers_without_conversion(self):
        from tests.issue_port_support import SimulatedInterruption
        self.bind(); self.prove(); self.issue.interrupt_next = True
        with self.assertRaises(SimulatedInterruption):
            self.tick()
        # Pre-correction representation lacked only the optional intent time.
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_text())
        state["pod_ticks"]["requests"]["1"].pop("intent_at")
        path.write_text(json.dumps(state))
        self.issue.calls.clear()
        result = self.tick()
        self.assertEqual(result["receipt"]["authorship"], "ambiguous")
        self.assertEqual([call[0] for call in self.issue.calls], ["issue"])
        self.assertNotIn("intent_at", self.line()["latest_request"])
        self.assertEqual(len(self.issue.writes), 1)


class ReadbackCorrections(TickCase):
    def window_hold(self, *, reason, row, change, ambiguous=False):
        original = self.issue.issue_body_write
        def send(**arguments):
            change()
            return original(**arguments)
        self.issue.issue_body_write = send
        if ambiguous:
            self.issue.lose_next = "landed"
        before = deepcopy(read(self.project, "objective")["checkpoint"])
        exc = self.failure("issue_reconciliation_required" if row == "B3" or reason == "issue_amendment_changed"
                           else "issue_is_evidence_ledger", self.tick)
        self.assertEqual(exc.detail["receipt"]["rebind"], True)
        self.assertEqual(exc.detail["receipt"]["result"]["result"], "unconfirmed" if ambiguous else "accepted")
        state = read(self.project, "objective")["checkpoint"]
        self.assertEqual(state["objective_source"]["body_sha256"], exc.detail["receipt"]["intended"])
        self.assertEqual(state["objective_source"]["amendments"], before["objective_source"]["amendments"])
        self.assertEqual({k: v for k, v in state.items() if k != "objective_source"},
                         {k: v for k, v in before.items() if k != "objective_source"})
        line = self.line()
        self.assertEqual((line["status"], line["authorship"]), ("blocked", "ambiguous" if ambiguous else "own"))
        self.assertEqual((line["blocked"]["row"], line["blocked"]["reason"]), (row, reason))
        self.assertEqual(len(self.issue.writes), 1)

    def test_closed_in_window_rebinds_own_but_keeps_B3_until_reopen(self):
        self.bind(); self.prove()
        self.window_hold(reason="closed_issue_requires_intent_reconciliation", row="B3",
                         change=lambda: setattr(self.issue, "state", "CLOSED"))
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "closed_issue_requires_intent_reconciliation")
        self.issue.state = "OPEN"
        self.assertEqual(self.tick()["status"], "current")
        self.assertEqual(len(self.issue.writes), 1)
        self.assertIsNone(self.line()["blocked"])

    def test_amendment_in_window_rebinds_own_but_keeps_B2(self):
        self.issue.amendments[99] = "original"
        self.bind(amendments=[self.locator + "#issuecomment-99"]); self.prove()
        self.window_hold(reason="issue_amendment_changed", row="B2",
                         change=lambda: self.issue.amendments.update({99: "changed"}))
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "issue_amendment_changed")
        self.assertEqual(len(self.issue.writes), 1)

    def test_role_in_window_keeps_body_observation_and_B2(self):
        self.bind(); self.prove()
        self.window_hold(reason="issue_is_evidence_ledger", row="B2",
                         change=lambda: setattr(self.issue, "title", "EEL: evidence"))
        self.failure("issue_is_evidence_ledger", self.tick)
        self.assertEqual(len(self.issue.writes), 1)

    def test_unconfirmed_closed_in_window_rebinds_ambiguously(self):
        self.bind(); self.prove()
        self.window_hold(reason="closed_issue_requires_intent_reconciliation", row="B3", ambiguous=True,
                         change=lambda: setattr(self.issue, "state", "CLOSED"))


class BoundedCorrections(TickCase):
    def test_sixty_unknown_numbers_leave_context_unchanged(self):
        self.bind(); self.prove()
        before = self.saved()
        for number in range(100, 160):
            self.failure("line_missing", self.tick, number)
        self.assertEqual(self.saved(), before)
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["requests"], {})
        self.assertEqual(self.issue.writes, [])

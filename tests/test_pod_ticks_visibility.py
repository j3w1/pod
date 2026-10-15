"""R11 and compatibility: ledger-derived acknowledgements and fetch-free status."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import patch

from pod import internal
from pod.governor import finalization
from pod.ledger import kernel_context, map_of, objective_root, read
from pod.obligations import report_projection, validate_closed_snapshot
from pod.status import render, status
from tests.issue_port_support import SimulatedInterruption
from tests.pod_tick_support import TickCase


class VisibilityTests(TickCase):
    def test_due_ack_names_exact_tick_request_without_fresh_read(self):
        self.bind(); result = self.prove()
        row = self.ack(result)["pod_ticks"]["lines"][0]
        self.assertEqual(row["request"], {"operation": "pod-tick", "input": {
            "project": str(self.project), "objective": "objective", "owner": "owner", "pod": 1}})
        self.assertIn("pod internal pod-tick --input -", row["command"])
        self.assertEqual(row["status"], "due")

    def test_untick_ack_and_block_ack_use_ledger_without_fetch(self):
        self.bind(); self.prove(); self.tick()
        result = self.lapse()
        row = self.ack(result)["pod_ticks"]["lines"][0]
        self.assertEqual(row["direction"], "untick")
        self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        row = self.line()
        self.assertEqual(row["blocked"]["reason"], "issue_access_unavailable")
        self.assertEqual(row["direction"], "untick")

    def test_source_refusal_names_the_due_pod_direction_and_exact_request(self):
        self.bind(); self.prove(); self.issue.unreadable = True
        exc = self.failure("issue_access_unavailable", self.tick)
        self.assertEqual((exc.detail["pod"], exc.detail["direction"]), (1, "tick"))
        row = exc.detail["pod_ticks"]["lines"][0]
        self.assertEqual(row["blocked"]["reason"], "issue_access_unavailable")
        self.assertIn("pod internal pod-tick", row["command"])

    def test_duplicate_refusal_names_the_due_pod_direction_and_readback(self):
        self.bind("- [ ] **PoD#1\n- [ ] **PoD#1 duplicate"); self.prove()
        exc = self.failure("line_duplicated", self.tick)
        self.assertEqual((exc.detail["pod"], exc.detail["direction"]), (1, "tick"))
        row = exc.detail["pod_ticks"]["lines"][0]
        self.assertEqual(row["checkboxes"], [False, False])
        self.assertEqual(row["blocked"]["reason"], "line_duplicated")
        self.assertIn("pod internal pod-tick", row["command"])

    def test_render_gives_checkbox_authorship_proof_state_and_block_reason(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403; self.tick()
        view = status(self.project, None, objective="objective")
        out = StringIO()
        with redirect_stdout(out):
            render(view)
        text = out.getvalue()
        for part in ("PoD#1:", "checkbox False", "not_pod", "blocked", "github_write_refused", "O1=satisfied"):
            self.assertIn(part, text)

    def test_tick_does_not_change_finalization_records_remaining_or_finished(self):
        self.bind(); self.prove()
        before = finalization(read(self.project, "objective"), {"actions": []})
        self.tick()
        after = finalization(read(self.project, "objective"), {"actions": []})
        for field in ("remaining", "records", "finished"):
            self.assertEqual(before[field], after[field])

    def test_duplicate_line_status_and_render_keep_each_observed_checkbox(self):
        self.bind("- [ ] **PoD#1\n- [X] **PoD#1 duplicate")
        view = status(self.project, None, objective="objective")
        row = view["pod_ticks"]["lines"][0]
        self.assertEqual(row["checkboxes"], [False, True])
        self.assertIsNone(row["ticked"])
        out = StringIO()
        with redirect_stdout(out):
            render(view)
        self.assertIn("checkboxes [False, True]", out.getvalue())
        self.assertIn("line_duplicated", out.getvalue())

    def test_tick_does_not_change_readiness_state_or_coordinator_needed(self):
        self.bind(); self.prove()
        before = status(self.project, None, objective="objective")["readiness"]
        self.tick()
        after = status(self.project, None, objective="objective")["readiness"]
        for field in ("state", "coordinator_needed"):
            self.assertEqual(before[field], after[field])

    def test_closure_report_bytes_match_existing_report_projection(self):
        self.bind(); self.prove(); self.issue.refuse_next = 403; self.tick()
        self.write(close=True)
        state = read(self.project, "objective")
        ctx = kernel_context(self.project, "objective", state, None)
        stored = json.dumps(state["checkpoint"]["closure"]["report"], sort_keys=True).encode()
        self.assertEqual(stored, json.dumps(report_projection(map_of(state), ctx), sort_keys=True).encode())
        self.assertNotIn("pod_ticks", json.loads(stored))
        self.assertNotIn("pod_ticks", state["checkpoint"]["closure"])

    def test_existing_closed_snapshot_without_tick_records_validates_without_new_rule(self):
        self.intake(); self.prove(close=True)
        # Genuine preceding record shape: no optional tick field was introduced.
        state = read(self.project, "objective")
        self.assertNotIn("pod_ticks", state)
        ctx = kernel_context(self.project, "objective", state, None)
        self.assertEqual(validate_closed_snapshot(map_of(state), ctx), ())
        self.assertEqual(self.ticks()["status"], "not_applicable")

    def test_earlier_issue_bound_closure_with_unticked_proof_revalidates_unchanged(self):
        self.bind()
        # Synthetic preceding-mechanism control: write a closure without this
        # new gate, then remove its optional tick facts as the old writer did.
        with patch("pod.pod_ticks.require_closing"):
            self.prove(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_text()); state.pop("pod_ticks")
        path.write_text(json.dumps(state)); before = self.saved()
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(map_of(state), kernel_context(self.project, "objective", state, None)), ())
        self.assertFalse(self.line()["due"])
        self.assertEqual(self.saved(), before)

    def test_never_recorded_line_facts_on_older_issue_binding_hold_new_closure(self):
        self.bind(); self.prove()
        # Synthetic old-writer control, without inserting the capability under
        # test: remove only the optional field absent from preceding versions.
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_text()); state.pop("pod_ticks")
        path.write_text(json.dumps(state))
        before = self.saved()
        self.failure("pod_ticks_due", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.tick(); self.write(close=True)

    def test_no_issue_body_is_stored_in_context_or_receipts(self):
        marker = "Unique issue prose which belongs only in GitHub"
        self.bind(marker + "\n- [ ] **PoD#1"); self.prove(); self.tick()
        content = (objective_root(self.project, "objective") / "context.json").read_text()
        self.assertNotIn(marker, content)
        self.assertNotIn("- [x] **PoD#1", content)

    def test_pending_tick_blocks_new_admission_and_closure_until_readback(self):
        self.bind("- [ ] **PoD#1", self.sub("S"))
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.write(rows); frozen = self.packet(["S"])
        self.issue.interrupt_next = True
        with self.assertRaises(SimulatedInterruption):
            self.tick()
        before = self.saved()
        self.failure("pod_tick_pending", self.start, "worker", frozen)
        self.assertEqual(self.saved(), before)
        self.tick()
        self.assertEqual(self.line()["authorship"], "ambiguous")

    def test_unreadable_write_readback_recovers_before_any_new_send(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            result = original(**arguments)
            self.issue.unreadable = True
            return result
        self.issue.issue_body_write = send
        self.failure("issue_access_unavailable", self.tick)
        self.assertEqual(self.line()["latest_request"]["phase"], "result")
        self.issue.calls.clear(); result = self.tick()
        self.assertEqual([call[0] for call in self.issue.calls], ["issue"])
        self.assertEqual(result["receipt"]["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)

    def test_due_untick_after_direct_user_reopen(self):
        self.bind(); self.prove(); self.tick(); self.write(close=True)
        rows = self.stored(); rows[0].pop("evidence", None); rows[0].pop("receipts", None)
        rows[0].update(state="active", executor="coordinator")
        result = self.write(rows, reopen=True,
            revision_authority={"provenance": "user_direct", "instruction": "reopen PoD#1 for correction"})
        self.assertEqual(self.ack(result)["pod_ticks"]["lines"][0]["direction"], "untick")
        self.tick()
        self.assertFalse(self.line()["ticked"])

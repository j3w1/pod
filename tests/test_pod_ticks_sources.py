"""Objective/source gates and R64 rechecks after confirmed and ambiguous writes."""

from copy import deepcopy
import os
from unittest.mock import patch

from pod.errors import PodError
from pod.github import issue_intake
from pod.internal import run
from pod.ledger import read, update_admission
from tests.issue_port_support import RecordingIssuePort, patch_issue_port
from tests.pod_tick_support import TickCase
from tests.test_historical_retirement import RetirementCase
from tests.test_succession import SuccessionCase


class SourceTests(TickCase):
    def test_closed_objective_refuses_A2(self):
        self.bind(); self.prove(); self.tick(); self.write(close=True)
        before, writes = self.saved(), list(self.issue.writes)
        self.failure("objective_closed", self.tick)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.writes, writes)
        self.assertFalse(self.line()["due"])

    def test_repository_mismatch_refuses_without_issue_read_A3(self):
        self.bind(); self.prove()
        from tests.kernel_support import git
        git(self.project, "remote", "set-url", "origin", "https://github.com/acme/elsewhere.git")
        self.issue.calls.clear()
        self.failure("repository_mismatch", self.tick)
        self.assertEqual(self.issue.calls, [])
        # Restore repository identity to read this objective's recorded block.
        git(self.project, "remote", "set-url", "origin", "https://github.com/acme/example.git")
        self.assertEqual(self.line()["blocked"]["code"], "repository_mismatch")

    def test_outside_body_edit_holds_tick_and_names_reason_B2(self):
        self.bind(); self.prove(); self.issue.body += "\nOutside edit"
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "issue_body_changed")
        self.assertEqual(self.line()["blocked"]["reason"], "issue_body_changed")
        self.assertEqual(self.issue.writes, [])

    def test_amendment_edit_holds_tick_B2(self):
        self.issue.amendments[99] = "Original"
        self.bind(amendments=[self.locator + "#issuecomment-99"]); self.prove()
        self.issue.amendments[99] = "Changed"
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "issue_amendment_changed")
        self.assertEqual(self.issue.writes, [])

    def test_eel_title_marker_holds_tick_B2(self):
        self.bind(); self.prove(); self.issue.title = "EEL: evidence"
        self.failure("issue_is_evidence_ledger", self.tick)
        self.assertEqual(self.line()["blocked"]["code"], "issue_is_evidence_ledger")
        self.assertEqual(self.issue.writes, [])

    def test_seal_title_marker_holds_tick_B2(self):
        self.bind(); self.prove(); self.issue.title = "SEAL: requirements"
        self.failure("issue_is_requirements_spec", self.tick)
        self.assertEqual(self.issue.writes, [])

    def test_eel_body_marker_holds_tick_B2(self):
        self.bind(); self.prove(); self.issue.body = "**Format:** Evidence Evaluation Ledger v1\n" + self.issue.body
        self.failure("issue_is_evidence_ledger", self.tick)
        self.assertEqual(self.issue.writes, [])

    def test_seal_body_marker_holds_tick_B2(self):
        self.bind(); self.prove(); self.issue.body = "**Format:** SEAL v1\n" + self.issue.body
        self.failure("issue_is_requirements_spec", self.tick)
        self.assertEqual(self.issue.writes, [])

    def test_closed_issue_wins_over_changed_body_B3(self):
        self.bind(); self.prove(); self.issue.state = "CLOSED"; self.issue.body += " changed"
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "closed_issue_requires_intent_reconciliation")
        self.assertEqual(self.issue.writes, [])

    def test_reconciled_still_closed_issue_never_gets_exemption_B3(self):
        self.bind(); self.prove(); self.issue.state = "CLOSED"
        self.rebind()
        exc = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(exc.detail["reason"], "closed_issue_requires_intent_reconciliation")
        self.assertEqual(self.line()["blocked"]["reason"], "closed_issue_requires_intent_reconciliation")
        self.assertNotIn("closed_intent", read(self.project, "objective"))
        self.assertEqual(self.issue.writes, [])

    def test_reopened_current_issue_tick_resumes_B4(self):
        self.bind(); self.prove(); self.issue.state = "CLOSED"
        self.failure("issue_reconciliation_required", self.tick)
        self.issue.state = "OPEN"
        self.tick()
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

    def test_reopened_changed_issue_still_requires_rebind_B4(self):
        self.bind(); self.prove(); self.issue.state = "CLOSED"; self.issue.body += " revised"
        self.failure("issue_reconciliation_required", self.tick)
        self.issue.state = "OPEN"
        self.failure("issue_reconciliation_required", self.tick)
        self.rebind(); self.tick()
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1 revised"])

    def test_unreadable_issue_stays_due_and_blocked_B5(self):
        self.bind(); self.prove(); self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.assertEqual(self.line()["blocked"]["row"], "B5")
        self.assertEqual(self.issue.writes, [])

    def test_stale_packet_refuses_packet_plan_mismatch_before_its_recheck(self):
        self.bind("- [ ] **PoD#1", self.sub("S"))
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.write(rows)
        frozen = self.packet(["S"])
        self.tick(); calls = deepcopy(self.issue.calls)
        exc = self.failure("packet_plan_mismatch", self.start, "worker", frozen)
        self.assertEqual(exc.detail["fields"], ["objective_source"])
        self.assertEqual(self.issue.calls, calls)

    def test_uncertain_admission_holds_tick_without_mutation_or_source_read(self):
        self.bind("- [ ] **PoD#1", self.sub("S"))
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.write(rows)
        frozen = self.packet(["S"])
        self.port.lost = True
        with patch.object(self.port, "start_worker", side_effect=PodError("native_effect_uncertain", "lost")):
            # The production OrcaPort already wraps self.port; replace that
            # actual boundary for this single ambiguous request.
            from pod.operations import OrcaPort
            with patch.object(OrcaPort, "start_worker", side_effect=PodError("native_effect_uncertain", "lost")):
                self.failure("native_effect_uncertain", self.start, "worker", frozen)
        state = read(self.project, "objective")
        admission = next(iter(state["admissions"].values()))
        self.assertEqual(admission["state"], "unresolved")
        before, calls = self.saved(), deepcopy(self.issue.calls)
        exc = self.failure("pod_tick_admission_pending", self.tick)
        self.assertIn(admission["admission_id"], exc.detail["admissions"])
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.calls, calls)
        self.assertIn("unchanged", exc.detail["next_action"])
        row = self.line()
        self.assertTrue(row["due"])
        self.assertIsNone(row["blocked"])
        self.assertIn(admission["admission_id"], row["waiting_on_admissions"])

    def test_reserved_admission_holds_tick_without_changing_original_packet(self):
        from tests.issue_port_support import SimulatedInterruption
        from pod.operations import OrcaPort
        self.bind("- [ ] **PoD#1", self.sub("S"))
        frozen = self.packet(["S"])
        with patch.object(OrcaPort, "start_worker", side_effect=SimulatedInterruption):
            with self.assertRaises(SimulatedInterruption):
                self.start("worker", frozen)
        admission = next(iter(read(self.project, "objective")["admissions"].values()))
        self.assertEqual(admission["state"], "reserved")
        before, calls = self.saved(), deepcopy(self.issue.calls)
        self.failure("pod_tick_admission_pending", self.tick)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.calls, calls)
        self.assertEqual(admission["packet_id"], frozen["packet_id"])


class RecheckTests(TickCase):
    def prepare_proven(self, *, ambiguous=False):
        self.prepare()
        self.bind("- [ ] **PoD#1", self.sub("S"))
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.write(rows)
        if ambiguous:
            self.issue.lose_next = "landed"
        self.tick()

    def acceptance(self):
        return run("acceptance", {"project": str(self.project), "objective_source":
            read(self.project, "objective")["checkpoint"]["objective_source"],
            "criteria": ["PoD#1"], "evidence_rows": [], "candidate": self.candidate,
            "policy_revision": "fixture", "sources": [], "dependencies": [], "environment": "fixture",
            "review_required": False, "hosted_required": False})

    def test_confirmed_tick_allows_admission_governor_and_acceptance_rechecks(self):
        self.prepare_proven()
        self.start("worker", self.packet(["S"]))
        self.op("governor-execute", action=self.action())
        self.acceptance()

    def test_ambiguous_rebound_tick_allows_all_three_rechecks(self):
        self.prepare_proven(ambiguous=True)
        self.start("worker", self.packet(["S"]))
        self.op("governor-execute", action=self.action())
        self.acceptance()

    def test_other_byte_change_still_holds_all_three_rechecks(self):
        self.prepare_proven(); self.issue.body += " outside"
        for callback in (lambda: self.start("worker", self.packet(["S"])),
                         lambda: self.op("governor-execute", action=self.action()), self.acceptance):
            exc = self.failure("issue_reconciliation_required", callback)
            self.assertEqual(exc.detail["reason"], "issue_body_changed")


class TerminalStateTests(RetirementCase):
    def test_retired_objective_tick_refuses_without_write_A2(self):
        issue = RecordingIssuePort()
        with patch_issue_port(issue):
            self.delivered(unknown_start=False)
            self.retire(self.decide())
            before = self.snapshot()
            with self.assertRaises(PodError) as caught:
                run("pod-tick", {"project": str(self.project), "objective": "objective", "owner": "owner", "pod": 1})
            self.assertEqual(caught.exception.code, "objective_retired")
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(issue.calls, [])


class ContinuedStateTests(SuccessionCase):
    def test_continued_objective_tick_refuses_without_write_A2(self):
        issue = RecordingIssuePort()
        with patch_issue_port(issue):
            self.predecessor(); self.successor(); self.claim(self.decide()); self.returning()
            before = self.snapshot()
            with self.assertRaises(PodError) as caught:
                run("pod-tick", {"project": str(self.project), "objective": "objective", "owner": "owner", "pod": 1})
            self.assertEqual(caught.exception.code, "objective_continued")
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(issue.calls, [])

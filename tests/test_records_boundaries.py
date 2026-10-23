"""Renewal through production checkpoint/report/tick boundaries with injected ports."""
from copy import deepcopy
import unittest

from pod.ledger import read
from pod.errors import PodError
from tests.pod_tick_support import TickCase
from tests.issue41_support import ProductionCase
from tests import kernel_support as boundaries


class RenewalTickTests(TickCase):
    def renew_proved(self):
        self.bind()
        self.prove(); self.tick()
        before = read(self.project, "objective")
        self.write(renew=[{"id": "O1", "as": "O2", "state": "active", "executor": "coordinator"}])
        return before, read(self.project, "objective")

    def test_tick_pending_transfers_requirement_scope_without_proofs(self):
        before, after = self.renew_proved()
        old = before["pod_ticks"]["criterion_scopes"]["O1"]
        new = after["pod_ticks"]["criterion_scopes"]["O2"]
        self.assertEqual(new["acceptance"], old["acceptance"])
        self.assertEqual(new["block_sha256"], old["block_sha256"])
        self.assertNotIn("proofs", new)
        self.assertNotEqual(new["definition"], old["definition"])
        self.assertEqual(self.line()["status"], "renewal_pending")
        self.assertFalse(self.line()["proven"])
        self.assertFalse(self.line()["due"])
        rows = [row for row in self.stored() if row["id"] == "O2"]
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O2")])
        self.write(rows)
        self.assertEqual(self.line()["status"], "current")
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

    def test_candidate_move_independently_unticks_pending_renewal(self):
        self.renew_proved()
        self.move()
        self.write()
        self.assertEqual(self.line()["direction"], "untick")
        self.tick()
        self.assertEqual(self.issue.writes[-1], "- [ ] **PoD#1")

    def test_independent_source_policy_dependency_environment_changes_end_pending(self):
        self.renew_proved()
        from pod.ledger import kernel_context
        from pod.pod_ticks import projection
        state = read(self.project, "objective")
        original = kernel_context(self.project, "objective", state, None)
        for change in ({"policy_revision": "changed"}, {"verification": {"environment": "changed", "dependencies": []}}):
            with self.subTest(change=change):
                line = projection(state, state["checkpoint"], {**original, **change})["lines"][0]
                self.assertEqual(line["direction"], "untick")
        changed = deepcopy(state)
        changed["pod_ticks"]["facts"]["body_sha256"] = "0" * 64
        self.assertEqual(projection(changed, changed["checkpoint"], original)["lines"][0]["direction"], "untick")

    def test_pending_renewal_never_supplies_evidence_closure_source_proof(self):
        self.renew_proved()
        from pod.evidence_closure import _source
        class Proof:
            facts = {"objective": "objective"}
            def __init__(self):
                self.conditions = []
            def add(self, *args):
                self.conditions.append(args)
        class Remote:
            def get(inner):
                return inner
            def issue_state(inner, **arguments):
                return {"body": self.issue.body, "state": "OPEN", "state_reason": None}
        state = read(self.project, "objective")
        result = Proof()
        _source(None, {"issue": state["checkpoint"]["objective_source"]}, state, result, Remote())
        self.assertTrue(any(row[1] == "renewal_pending" for row in result.conditions))


class RenewalAssuranceTests(ProductionCase):
    def qualification(self):
        from pod.ledger import kernel_view
        return kernel_view(self.project, "objective", state=read(self.project, "objective"))["label"]["label"]

    def policy(self):
        (self.project / "AGENTS.md").write_text(boundaries.AGENTS)
        boundaries.git(self.project, "add", "AGENTS.md")
        boundaries.git(self.project, "commit", "-qm", "fixture audit policy")
        self.base = self.candidate = boundaries.git(self.project, "rev-parse", "HEAD")
        boundaries.git(self.project, "branch", "-f", "target", self.base)
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.intake(boundaries.governance_policy(self, "A"))

    def test_policy_renewal_without_owner_decision_remains_required(self):
        self.policy()
        before = read(self.project, "objective")["checkpoint"]
        self.write(renew=[{"id": "A", "as": "A2"}])
        after = read(self.project, "objective")["checkpoint"]
        old, new = [next(row for row in after["obligations"] if row["id"] == key) for key in ("A", "A2")]
        self.assertEqual(new["source"], old["source"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertNotIn("revision_decisions", after)
        self.assertEqual(self.qualification(), "WITHHELD")

    def test_assurance_and_correction_renewal_keep_review_order(self):
        self.policy()
        frozen = self.packet(["A"], role="review")
        admission = self.start("review", frozen)["admission"]; self.settle(admission)
        state = read(self.project, "objective")["checkpoint"]
        correction = {"kind": "correction", "provenance": "coordinator", "parent": "A", "state": "waiting", "check": "fix the defect",
                      "wait": {"class": "sequenced", "referent": "O1"}}
        self.report(admission, frozen, map={"seq": state["seq"] + 1, "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}, "K": correction}},
                    triage=[{"finding": "F", "severity": "major", "triage": "required_correction", "correction": "K", "summary": "repair required"}])
        self.write(renew=[{"id": "A", "as": "A2"}])
        self.write(renew=[{"id": "K", "as": "K2"}])
        state = read(self.project, "objective")["checkpoint"]
        self.assertEqual(next(row for row in state["obligations"] if row["id"] == "K2")["parent"], "A")
        frozen = self.packet(["A2"], role="review")
        early = self.start("early", frozen)["admission"]; self.settle(early)
        rows = [row for row in self.stored() if row["state"] != "renewed"]
        next(row for row in rows if row["id"] == "K2").update(state="satisfied", evidence=[self.proof("K2")])
        next(row for row in rows if row["id"] == "K2").pop("wait")
        a = next(row for row in rows if row["id"] == "A2"); a.pop("executor"); a.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        state = read(self.project, "objective")["checkpoint"]
        with self.assertRaises(PodError) as caught:
            self.report(early, frozen, map={"seq": state["seq"] + 1, "update": {"A2": {"state": "satisfied", "evidence": [{"attempt": early["admission_id"]}]}}})
        self.assertEqual(caught.exception.detail["detail"], "evidence_invalidated")
        self.report(early, frozen, map={"seq": state["seq"] + 1, "update": {"A2": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
        fresh_packet = self.packet(["A2"], role="review")
        fresh = self.start("fresh", fresh_packet)["admission"]; self.settle(fresh)
        state = read(self.project, "objective")["checkpoint"]
        self.report(fresh, fresh_packet, map={"seq": state["seq"] + 1, "update": {"A2": {"state": "satisfied", "evidence": [{"attempt": fresh["admission_id"]}]}}})
        self.assertEqual(self.qualification(), "QUALIFIED")


class RenewalMappingTests(unittest.TestCase):
    def setUp(self):
        from tests import test_issue_mapping_supersession as supersession
        self.case = supersession.SupersessionCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_verified_r149_mapping_keeps_approved_ids_through_renewal(self):
        case = self.case
        case.supersede()
        before = read(case.project, "objective")
        case.write(renew=[{"id": "R1", "as": "R2"}])
        after = read(case.project, "objective")
        self.assertEqual(after["checkpoint"]["revision_decisions"], before["checkpoint"]["revision_decisions"])
        self.assertEqual(case.line()["criteria"][0]["id"], "R2")
        case.write()
        self.assertEqual(next(row for row in case.stored() if row["id"] == "O1"), before["checkpoint"]["obligations"][0])

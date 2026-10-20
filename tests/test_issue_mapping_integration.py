"""Combined source-revision and mapping checks using disposable recording ports."""

from copy import deepcopy
import json

from pod.github import issue_intake, pod_blocks
from pod.ledger import read
from pod.pod_ticks import validate
from pod.util import digest
from tests.test_issue_revision import IssueRevisionCase
from tests.test_pod_mapping_repair import RepairCase


class RevisionMappingTests(IssueRevisionCase):
    def nested_proof(self):
        self.issue.body = ("- [ ] **PoD#1** old requirement\n"
                           "  - Accept the complete nested result.\n"
                           "    Preserve its detailed acceptance condition.\n")
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.criterion(check="Deliver the original nested result")],
                   objective_source=source, governance={"base_ref": "target"})
        self.prove()
        self.tick()

    def add_second(self):
        proposal = self.proposal()
        proposal["obligations"][-1].pop("wait")
        proposal["obligations"][-1].update(state="active", executor="coordinator")
        return self.revise(proposal)

    def test_delivery_only_observation_and_revision_keep_full_scope_and_proof(self):
        self.nested_proof()
        initial = deepcopy(read(self.project, "objective"))
        cache = initial["pod_ticks"]["criterion_scopes"]
        self.issue.body += "\n## Delivery\nRecord the ordinary delivery result.\n"
        self.rebind()
        observed = deepcopy(read(self.project, "objective"))
        self.assertEqual(self.requests, [])
        self.assertNotIn("revision_decisions", observed["checkpoint"])
        self.assertEqual(observed["pod_ticks"]["criterion_scopes"], cache)
        self.assertEqual(observed["pod_ticks"]["facts"]["blocks"], initial["pod_ticks"]["facts"]["blocks"])
        self.assertNotEqual(observed["pod_ticks"]["facts"]["mapping_scope_sha256"],
                            initial["pod_ticks"]["facts"]["mapping_scope_sha256"])
        self.add_second()
        after = read(self.project, "objective")
        facts = after["pod_ticks"]["facts"]
        self.assertEqual(facts["blocks"]["1"], [pod_blocks(self.issue.body)[1][0]["sha256"]])
        self.assertTrue(facts["mapping_source_current"])
        self.assertEqual(facts["source"], digest(after["checkpoint"]["objective_source"]))
        self.assertEqual(after["pod_ticks"]["criterion_scopes"]["O1"], cache["O1"])
        self.assertEqual(after["checkpoint"]["obligations"][0], initial["checkpoint"]["obligations"][0])
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["previous"]["pod_ticks"], observed["pod_ticks"])
        self.assertEqual((self.line()["proven"], self.line()["authorship"]), (True, "own"))
        validate(after["pod_ticks"])
        self.assertNotIn(self.issue.body, json.dumps(after))

    def test_withdrawal_after_revision_unticks_and_preserves_full_history(self):
        self.nested_proof()
        self.add_second()
        before = deepcopy(read(self.project, "objective"))
        rows = self.stored()
        rows[0].pop("evidence")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "withdraw current result"})
        self.write(rows, criteria=["PoD#1", "PoD#2"], revision_authority={
            "provenance": "user_direct", "instruction": "Withdraw the current result"})
        line = self.line()
        self.assertEqual(line["criteria"], [])
        self.assertEqual((line["proven"], line["authorship"], line["direction"]), (False, "own", "untick"))
        body = self.issue.body
        result = self.tick()
        self.assertEqual(result["receipt"]["direction"], "untick")
        self.assertEqual(self.issue.body, body.replace("- [x]", "- [ ]", 1))
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][0]["receipts"],
                         before["checkpoint"]["obligations"][0]["receipts"])
        self.assertEqual(after["pod_ticks"]["criterion_scopes"], before["pod_ticks"]["criterion_scopes"])
        old_decision = before["checkpoint"]["revision_decisions"][-1]
        self.assertIn(old_decision, after["checkpoint"]["revision_decisions"])
        self.assertEqual(after["pod_ticks"]["facts"]["blocks"], before["pod_ticks"]["facts"]["blocks"])
        self.assertFalse(self.line()["ticked"])


class RepairFactsTests(RepairCase):
    def nested_repair(self, *, amendments=None):
        self.bind_original(body=("PES fixture\r\n- [ ] **PoD 1: Result 1.** Check unchanged.\r\n"
                                 "  - Accept the complete nested scope.\r\n"
                                 "    Include this detailed condition.\r\n"), amendments=amendments)
        self.repair()
        self.prove()

    def test_repaired_own_tick_retains_complete_blocks_and_current_mapping_facts(self):
        self.nested_repair()
        before = deepcopy(read(self.project, "objective"))
        self.tick()
        after = read(self.project, "objective")
        facts = after["pod_ticks"]["facts"]
        self.assertEqual(facts["blocks"], before["pod_ticks"]["facts"]["blocks"])
        self.assertEqual(facts["blocks"]["1"], [pod_blocks(self.issue.body)[1][0]["sha256"]])
        self.assertEqual(facts["mapping_scope_sha256"], before["pod_ticks"]["facts"]["mapping_scope_sha256"])
        self.assertTrue(facts["mapping_source_current"])
        self.assertEqual(after["pod_ticks"]["criterion_scopes"], before["pod_ticks"]["criterion_scopes"])
        self.assertEqual(after["checkpoint"]["obligations"][0]["source"], before["checkpoint"]["obligations"][0]["source"])
        self.assertEqual(after["checkpoint"]["obligations"][0]["pod_mapping"]["current_source"],
                         after["checkpoint"]["objective_source"])
        self.assertEqual((self.line()["proven"], self.line()["authorship"]), (True, "own"))
        validate(after["pod_ticks"])

    def test_amendment_observation_keeps_blocks_and_receipts_but_retires_mapping(self):
        amendment = self.locator + "#issuecomment-11"
        self.nested_repair(amendments=[amendment])
        self.tick()
        before = deepcopy(read(self.project, "objective"))
        self.issue.amendments[11] = "Changed amendment"
        self.issue.state = "CLOSED"
        self.failure("issue_reconciliation_required", self.tick)
        after = read(self.project, "objective")
        self.assertEqual(after["pod_ticks"]["facts"]["blocks"], before["pod_ticks"]["facts"]["blocks"])
        self.assertFalse(after["pod_ticks"]["facts"]["mapping_source_current"])
        self.assertTrue(after["checkpoint"]["obligations"][0]["pod_mapping"]["invalidated"])
        self.assertEqual(after["checkpoint"]["obligations"][0]["receipts"],
                         before["checkpoint"]["obligations"][0]["receipts"])
        self.assertEqual(after["pod_ticks"]["criterion_scopes"], before["pod_ticks"]["criterion_scopes"])
        self.assertFalse(self.line()["proven"])
        validate(after["pod_ticks"])


class RepairRevisionBoundaryTests(IssueRevisionCase, RepairCase):
    def test_mapping_repair_and_verified_revision_are_distinct_checkpoint_operations(self):
        self.bind_original()
        self.repair()
        proposal = self.proposal()
        proposal["mapping_repair"] = {"original_body": self.original, "targets": {"O1": "PoD#1"}}
        before = deepcopy(read(self.project, "objective"))
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "combined_operation")
        self.assertEqual(self.requests, [])
        self.assertEqual(read(self.project, "objective"), before)
        self.failure("relayed_decision_refused", self.write, revision_authority={
            "provenance": "user_direct", "channel": "relayed", "instruction": "unverified revision",
            "relay": {"reference": "synthetic-unverified", "owner_source": "synthetic-unverified"}})
        self.assertEqual(read(self.project, "objective"), before)

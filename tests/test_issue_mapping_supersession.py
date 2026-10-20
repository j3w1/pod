"""Exact verified supersession of repaired originals, with recording ports only."""

from copy import deepcopy
from unittest.mock import patch

from pod.github import issue_intake, pod_blocks
from pod.issue_revision import source_check
from pod.ledger import kernel_context, read
from pod.obligations import definition_id, validate_closed_snapshot
from pod.pod_mapping import superseded_originals
from tests.test_issue_revision import IssueRevisionCase
from tests.test_pod_mapping_repair import RepairCase


class SupersessionCase(IssueRevisionCase, RepairCase):
    def remapping(self):
        proposal = self.proposal()
        old = proposal["obligations"][0]
        old.pop("executor", None)
        old.pop("evidence", None)
        old.update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "approved canonical replacement"})
        replacement = {**self.new_criterion(1), "id": "R1",
                       "check": source_check("PoD#1", pod_blocks(self.issue.body)[1][0]["text"])}
        replacement.pop("external")
        replacement.update(state="active", executor="coordinator")
        proposal["obligations"].insert(1, replacement)
        proposal["obligations"][-1]["wait"] = {"class": "dependency", "referent": "R1"}
        return proposal

    def supersede(self, *, old_proof=False):
        self.bind_original()
        self.repair()
        if old_proof:
            self.prove()
            self.tick()
        before = deepcopy(read(self.project, "objective"))
        self.revise(self.remapping())
        self.criteria = ["PoD#1", "PoD#2"]
        return before

    def prove_live(self):
        rows = self.stored()
        for row in rows:
            if row["state"] != "withdrawn":
                row.pop("executor", None)
                row.pop("wait", None)
                row.update(state="satisfied", evidence=[self.proof(row["id"])])
        return self.write(rows)


class SupersessionTests(SupersessionCase):
    def test_verified_remap_keeps_history_and_requires_fresh_replacement_proof(self):
        before = self.supersede(old_proof=True)
        after = read(self.project, "objective")
        old, replacement = after["checkpoint"]["obligations"][:2]
        self.assertEqual(old["definition"], before["checkpoint"]["obligations"][0]["definition"])
        self.assertEqual(old["source"], before["checkpoint"]["obligations"][0]["source"])
        self.assertEqual(old["receipts"], before["checkpoint"]["obligations"][0]["receipts"])
        self.assertEqual({key: value for key, value in old["pod_mapping"].items() if key != "invalidated"},
                         {key: value for key, value in before["checkpoint"]["obligations"][0]["pod_mapping"].items()
                          if key != "invalidated"})
        self.assertTrue(old["pod_mapping"]["invalidated"])
        self.assertEqual(replacement["receipts"], [])
        self.assertNotIn("evidence", replacement)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.line()["direction"], "untick")
        self.assertEqual(after["pod_ticks"]["criterion_scopes"]["O1"], before["pod_ticks"]["criterion_scopes"]["O1"])
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["previous"]["obligations"], before["checkpoint"]["obligations"])
        self.assertEqual(decision["previous"]["pod_ticks"], before["pod_ticks"])
        self.prove_live()
        self.assertTrue(self.line()["proven"])

    def test_later_delivery_observation_and_checkpoint_keep_approved_supersession(self):
        self.supersede()
        before = deepcopy(read(self.project, "objective"))
        calls = len(self.requests)
        self.issue.body += "\n\n## Delivery\nRecord the ordinary delivery result.\n"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source)
        self.write()
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["revision_decisions"], before["checkpoint"]["revision_decisions"])
        self.assertEqual(after["checkpoint"]["obligations"], before["checkpoint"]["obligations"])
        self.assertEqual(len(self.requests), calls)
        self.assertNotEqual(after["checkpoint"]["objective_source"], before["checkpoint"]["objective_source"])

    def test_prior_owner_withdrawal_keeps_its_provenance_under_verified_supersession(self):
        self.bind_original()
        self.repair()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "withdraw the old draft"})
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Withdraw the draft"})
        old = deepcopy(read(self.project, "objective")["checkpoint"]["obligations"][0])
        proposal = self.remapping()
        proposal["obligations"][0] = old
        self.revise(proposal)
        self.criteria = ["PoD#1", "PoD#2"]
        self.write()
        after = read(self.project, "objective")
        row = after["checkpoint"]["obligations"][0]
        for field in ("withdrawal", "source", "definition", "receipts"):
            self.assertEqual(row[field], old[field])
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["retired"], [])
        self.assertEqual(decision["previous"]["obligations"][0], old)

    def test_later_admission_map_keeps_approved_supersession(self):
        self.supersede()
        before = deepcopy(read(self.project, "objective"))
        frozen = self.packet(["R1"], criteria=self.criteria)
        self.start("post-remap", frozen, accompanying={"seq": before["checkpoint"]["seq"] + 1})
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][0], before["checkpoint"]["obligations"][0])
        self.assertEqual(after["checkpoint"]["revision_decisions"], before["checkpoint"]["revision_decisions"])
        self.assertEqual(len(self.port.starts), 1)

    def test_r6_proof_scope_metadata_preserves_supersession_and_acceptance_lineage(self):
        self.supersede()
        before = deepcopy(read(self.project, "objective"))
        calls = len(self.requests)
        rows = self.stored()
        rows[1]["proof_scope"] = ["src"]
        self.write(rows)
        self.issue.body += "\n\n## Delivery\nAn ordinary source metadata observation.\n"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source)
        self.write()
        after = read(self.project, "objective")
        self.assertNotEqual(after["checkpoint"]["obligations"][1]["definition"],
                            before["checkpoint"]["obligations"][1]["definition"])
        for field in ("check", "source"):
            self.assertEqual(after["checkpoint"]["obligations"][1][field],
                             before["checkpoint"]["obligations"][1][field])
        self.assertEqual(after["checkpoint"]["obligations"][0], before["checkpoint"]["obligations"][0])
        self.assertEqual(after["checkpoint"]["revision_decisions"], before["checkpoint"]["revision_decisions"])
        old_scope = before["pod_ticks"]["criterion_scopes"]["R1"]
        scope = after["pod_ticks"]["criterion_scopes"]["R1"]
        self.assertEqual({key: value for key, value in scope.items() if key != "definition"},
                         {key: value for key, value in old_scope.items() if key != "definition"})
        self.assertEqual(len(self.requests), calls)

    def test_later_report_map_keeps_approved_supersession(self):
        self.supersede()
        frozen = self.packet(["R1"], criteria=self.criteria)
        admission = self.start("post-remap", frozen)["admission"]
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "succeeded"
        before = deepcopy(read(self.project, "objective"))
        rows = self.stored()
        rows[1]["executor"] = "coordinator"
        self.report(admission, frozen, map={"seq": before["checkpoint"]["seq"] + 1, "obligations": rows})
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][0], before["checkpoint"]["obligations"][0])
        self.assertEqual(after["checkpoint"]["revision_decisions"], before["checkpoint"]["revision_decisions"])

    def test_closing_write_and_reader_agree_on_approved_supersession(self):
        self.supersede()
        rows = self.stored()
        rows[1]["proof_scope"] = ["src"]
        self.write(rows)
        self.prove_live()
        self.tick(1)
        self.tick(2)
        self.write(close=True)
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
            kernel_context(self.project, "objective", state, None)), ())
        self.assertEqual(state["checkpoint"]["obligations"][0]["state"], "withdrawn")


class SupersessionRefusalTests(SupersessionCase):
    def test_merely_withdrawn_repaired_row_cannot_normalize_original_criteria(self):
        self.bind_original()
        self.repair()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "ordinary withdrawal"})
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Withdraw this result"})
        self.held("original_criteria_required", self.write, criteria=["PoD#1"])
        self.held("original_criteria_required", self.write, criteria=["PoD#1"], close=True)
        self.assertEqual(self.requests, [])

    def test_denied_or_mismatched_verdict_cannot_supersede_repaired_row(self):
        self.bind_original()
        self.repair()
        proposal = self.remapping()
        for changes in ({"verified": False}, {"reference": "foreign-reference"}):
            with self.subTest(changes=changes):
                self.verdict_changes = changes
                self.refused_revision(proposal)
        self.assertEqual(self.receipts(), {})

    def test_unverified_revision_and_caller_history_cannot_create_supersession(self):
        self.bind_original()
        self.repair()
        proposal = self.remapping()
        proposal["revision_authority"].pop("operation")
        proposal["revision_authority"]["relay"]["owner_source"] = "unverified fixture"
        self.refused_revision(proposal, "relayed_decision_refused")
        self.held("original_criteria_required", self.write, criteria=["PoD#1"],
                  revision_decisions=[{"operation": "issue_revision"}])
        self.assertEqual(self.requests, [])

    def test_wrong_current_criteria_cannot_borrow_accepted_mapping(self):
        self.supersede()
        for criteria in (["PoD#2", "PoD#1"], ["PoD#1"]):
            with self.subTest(criteria=criteria):
                self.held("original_criteria_required", self.write, criteria=criteria)

    def test_changed_live_definition_cannot_borrow_accepted_mapping(self):
        self.supersede()
        rows = self.stored()
        rows[1]["check"] += " Changed semantic check."
        self.held("original_criteria_required", self.write, rows, revision_authority={
            "provenance": "user_direct", "instruction": "Change the live check"})

    def test_synthetic_fake_capability_and_stale_mapping_supply_no_exception(self):
        self.bind_original()
        self.repair()
        state = deepcopy(read(self.project, "objective"))
        rows = {row["id"]: row for row in state["checkpoint"]["obligations"]}
        ctx = kernel_context(self.project, "objective", state, None)
        self.assertEqual(superseded_originals(state["checkpoint"], rows,
            {**ctx, "issue_revision": {"operation": "issue_revision"}}, seq=3), set())
        self.supersede_on_existing_repair()
        state = deepcopy(read(self.project, "objective"))
        rows = deepcopy({row["id"]: row for row in state["checkpoint"]["obligations"]})
        ctx = kernel_context(self.project, "objective", state, None)
        rows["R1"]["check"] += " Synthetic stale current definition."
        rows["R1"]["definition"] = definition_id(rows["R1"])
        self.assertEqual(superseded_originals(state["checkpoint"], rows, ctx), set())
        rows = deepcopy({row["id"]: row for row in state["checkpoint"]["obligations"]})
        rows["O1"]["source"] = {"ref": "PoD 99: Synthetic prior identity mismatch"}
        rows["O1"]["definition"] = definition_id(rows["O1"])
        self.assertEqual(superseded_originals(state["checkpoint"], rows, ctx), set())
        rows = deepcopy({row["id"]: row for row in state["checkpoint"]["obligations"]})
        rows["R1"]["source"] = {"ref": "PoD#2"}
        self.assertEqual(superseded_originals(state["checkpoint"], rows, ctx), set())

    def test_genuine_stale_capability_cannot_authorize_a_later_mapping(self):
        captured = []
        def capture(prior, rows, ctx, **fields):
            if ctx.get("issue_revision") is not None:
                captured.append(dict(ctx))
            return superseded_originals(prior, rows, ctx, **fields)
        with patch("pod.pod_mapping.superseded_originals", side_effect=capture):
            self.supersede()
        self.assertEqual(len(captured), 1)
        state, before = read(self.project, "objective"), self.saved()
        rows = {row["id"]: row for row in state["checkpoint"]["obligations"]}
        self.assertEqual(superseded_originals(state["checkpoint"], rows, captured[0],
            seq=state["checkpoint"]["seq"] + 1), set())
        self.assertEqual(self.saved(), before)

    def supersede_on_existing_repair(self):
        self.revise(self.remapping())
        self.criteria = ["PoD#1", "PoD#2"]

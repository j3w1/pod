"""Proof/source event regressions through disposable production boundaries."""

from copy import deepcopy
from unittest.mock import patch

from pod.github import issue_intake
from pod.ledger import read
from tests import test_issue_revision as fixtures


ALPHA = "- [ ] **PoD#1** Verify the output\n  Acceptance requires alpha."
BETA = ALPHA.replace("alpha", "beta")
CHECK = "Output verified; acceptance requires alpha"
AUTHORITY = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
             "instruction": "Reconcile the approved source", "relay": {"reference": "synthetic-decision"}}
OWNER = {"provenance": "user_direct", "instruction": "Revise the acceptance check in this session"}


class CurrencyCase(fixtures.IssueRevisionCase):
    def source(self):
        return issue_intake(self.project, self.locator, port=self.issue)["source"]

    def state(self):
        return read(self.project, "objective")

    def row(self):
        return self.state()["checkpoint"]["obligations"][0]

    def scope(self):
        return self.state()["pod_ticks"]["criterion_scopes"]["O1"]

    def begin_alpha(self):
        self.issue.body = ALPHA
        self.write([self.criterion("blocked_external", check=CHECK,
                    external={"party": "user", "need": "result", "unblocks_when": "verified"})],
                   objective_source=self.source(), governance={"base_ref": "target"})

    def observe(self, body):
        self.issue.body = body
        self.write(objective_source=self.source())

    def proof_rows(self, reference="first-proof", *, append=False):
        rows = self.stored()
        for row in rows:
            for key in ("external", "wait", "executor"):
                row.pop(key, None)
            evidence = row.get("evidence", []) if append else []
            row.update(state="satisfied", evidence=[*evidence, self.proof(row["id"], reference=reference)])
        return rows

    def accept(self, reference="first-proof", **fields):
        self.write(self.proof_rows(reference), **fields)

    def proposal_for(self, body):
        self.issue.body = body + "\n- [ ] **PoD#2** added requirement"
        added = {**self.new_criterion(2), "check": "**PoD#2** added requirement"}
        return {"seq": self.state()["checkpoint"]["seq"] + 1, "obligations": self.stored() + [added],
                "objective_source": self.source(), "criteria": ["PoD#1", "PoD#2"],
                "revision_authority": deepcopy(AUTHORITY)}

    def remap(self, proposal):
        from pod.issue_revision import source_check
        from pod.github import pod_blocks
        proposal = deepcopy(proposal)
        old = proposal["obligations"][0]
        for key in ("external", "wait", "executor", "evidence", "reuse"):
            old.pop(key, None)
        old.update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "Proof/source currency differs"})
        check = source_check("PoD#1", pod_blocks(self.issue.body)[1][0]["text"])
        proposal["obligations"].append({**self.new_criterion(1), "id": "R1", "check": check})
        return proposal

    def assert_unknown(self, proposal, reason="prior_source_scope_ambiguous"):
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["fact_reason"], reason)


class ProofCurrencyTests(CurrencyCase):
    def test_n1_beta_proof_cannot_pass_restored_alpha_and_unknown_remap_resets(self):
        self.begin_alpha()
        self.observe(BETA)
        self.accept()
        before = deepcopy(self.row())
        proposal = self.proposal_for(ALPHA)
        self.assert_unknown(proposal)
        self.revise(self.remap(proposal))
        old = self.row()
        self.assertEqual((old["definition"], old["receipts"]), (before["definition"], before["receipts"]))
        self.assertEqual(old["state"], "withdrawn")
        fact = self.state()["checkpoint"]["revision_decisions"][-1]["scope_facts"]["previous"][0]
        self.assertEqual((fact["status"], fact["reason"]), ("unknown", "prior_source_scope_ambiguous"))
        self.assertFalse(self.line()["proven"])
        self.assertEqual([row["id"] for row in self.line()["criteria"]], ["R1"])

    def test_same_write_beta_observation_and_first_pass_does_not_become_alpha_proof(self):
        self.begin_alpha()
        self.issue.body = BETA
        self.accept(objective_source=self.source())
        self.assert_unknown(self.proposal_for(ALPHA))

    def test_genuine_alpha_proof_survives_beta_observation_and_alpha_restore(self):
        self.begin_alpha()
        self.accept()
        self.tick()
        before = deepcopy(self.row())
        alpha = self.issue.body
        self.observe(alpha.replace("alpha", "beta"))
        self.write()
        self.revise(self.proposal_for(alpha))
        self.assertEqual(self.row(), before)
        self.assertEqual((self.line()["proven"], self.line()["authorship"], self.line()["due"]), (True, "own", False))

    def test_reused_reference_never_reanchors_beta_proof_or_accepted_sequence(self):
        self.begin_alpha()
        self.observe(BETA)
        self.accept()
        before = deepcopy(self.row()["receipts"])
        scopes = deepcopy(self.scope())
        self.observe(ALPHA)
        self.accept()
        self.assertEqual(self.row()["receipts"], before)
        self.assertEqual(self.scope(), scopes)
        self.assert_unknown(self.proposal_for(ALPHA))

    def test_all_selected_pass_scopes_are_checked(self):
        self.begin_alpha()
        self.accept("alpha-proof")
        self.observe(BETA)
        self.write(self.proof_rows("beta-proof", append=True))
        self.assert_unknown(self.proposal_for(ALPHA))

    def test_missing_proof_time_observation_cannot_borrow_definition_cache(self):
        self.begin_alpha()
        with patch("pod.pod_ticks.refresh"):
            self.observe(BETA)
        self.accept()
        self.assert_unknown(self.proposal_for(ALPHA), "historical_body_unavailable")

    def test_older_local_cache_without_proof_fact_uses_exact_data_join(self):
        self.begin_alpha()
        self.observe(BETA)
        with patch("pod.pod_ticks._bind_proof_scopes", create=True):
            self.accept()
        proposal = self.proposal_for(ALPHA)
        self.assert_unknown(proposal, "historical_body_unavailable")
        proposal["issue_revision_data"] = self.historical_data(BETA)
        self.assert_unknown(proposal)

    def test_report_map_acceptance_keeps_proof_time_scope_after_later_writes(self):
        self.issue.body = ALPHA
        self.write([self.criterion(check=CHECK), self.sub("S")],
                   objective_source=self.source(), governance={"base_ref": "target"})
        self.observe(BETA)
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("proof-task", frozen)["admission"]
        self.settle(admission)
        self.report(admission, frozen, map={"obligations": self.proof_rows()})
        self.write()
        self.assert_unknown(self.proposal_for(ALPHA))

    def test_admission_map_acceptance_keeps_proof_time_scope(self):
        self.issue.body = ALPHA
        self.write([self.criterion(check=CHECK), self.sub("S")],
                   objective_source=self.source(), governance={"base_ref": "target"})
        self.observe(BETA)
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1", reference="admission-proof")])
        self.start("proof-task", frozen, accompanying={"obligations": rows})
        self.assert_unknown(self.proposal_for(ALPHA))

    def test_return_to_same_scope_with_only_alpha_selected_keeps_actual_proof(self):
        self.begin_alpha()
        self.accept("alpha-proof")
        self.observe(BETA)
        self.write(self.proof_rows("beta-proof", append=True))
        self.observe(ALPHA)
        rows = self.stored()
        rows[0]["evidence"] = [item for item in rows[0]["evidence"] if item["reference"] == "alpha-proof"]
        self.write(rows)
        before = deepcopy(self.row())
        self.revise(self.proposal_for(ALPHA))
        self.assertEqual(self.row(), before)
        self.assertTrue(self.line()["proven"])


class RequirementCurrencyTests(CurrencyCase):
    def test_n2_coordinator_proof_scope_change_carries_alpha_source(self):
        self.begin_alpha()
        before = deepcopy(self.scope())
        self.observe(BETA)
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        self.write(rows)
        self.assertEqual({key: self.scope()[key] for key in before if key != "definition"},
                         {key: value for key, value in before.items() if key != "definition"})
        self.assertEqual(self.scope()["definition"], self.row()["definition"])
        error = self.refused_revision(self.proposal_for(BETA), "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")

    def test_coordinator_definition_hash_return_still_requires_beta_replacement(self):
        self.begin_alpha()
        before = deepcopy(self.scope())
        self.observe(BETA)
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        self.write(rows)
        rows = self.stored(); rows[0].pop("proof_scope")
        self.write(rows)
        self.assertEqual(self.scope(), before)
        error = self.refused_revision(self.proposal_for(BETA), "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")

    def test_owner_session_semantic_revision_binds_beta_and_accepts_new_beta_proof(self):
        self.begin_alpha()
        self.observe(BETA)
        rows = self.stored(); rows[0]["check"] = CHECK.replace("alpha", "beta")
        self.write(rows, revision_authority=OWNER)
        self.accept("beta-proof")
        before = deepcopy(self.row())
        self.revise(self.proposal_for(BETA))
        self.assertEqual(self.row(), before)
        self.assertTrue(self.line()["proven"])

    def test_owner_definition_hash_return_cannot_resurrect_original_alpha_receipt(self):
        self.begin_alpha()
        self.accept()
        original = deepcopy(self.row())
        self.observe(BETA)
        rows = self.stored(); rows[0].pop("evidence")
        rows[0].update(check=CHECK.replace("alpha", "beta"), state="blocked_external",
                       external={"party": "user", "need": "result", "unblocks_when": "verified"})
        self.write(rows, revision_authority=OWNER)
        rows = self.proof_rows(); rows[0]["check"] = CHECK
        rows[0]["evidence"] = [{key: value for key, value in item.items() if key != "governance"}
                               for item in original["evidence"]]
        before, receipts = self.saved(), self.receipts()
        error = self.failure("obligation_unaccounted", self.write, rows, revision_authority=OWNER)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual((self.saved(), self.receipts()), (before, receipts))
        rows[0]["evidence"][0]["reference"] = "fresh-beta-proof"
        self.write(rows, revision_authority=OWNER)
        self.assertEqual(self.row()["definition"], original["definition"])
        self.assertEqual(self.row()["receipts"][0], original["receipts"][0])
        self.revise(self.proposal_for(BETA))
        self.assertTrue(self.line()["proven"])

    def test_unobserved_owner_revision_and_hash_return_stay_unknown(self):
        self.begin_alpha()
        with patch("pod.pod_ticks.refresh"):
            self.observe(BETA)
        rows = self.stored(); rows[0]["check"] = CHECK.replace("alpha", "beta")
        self.write(rows, revision_authority=OWNER)
        rows = self.stored(); rows[0]["check"] = CHECK
        self.write(rows, revision_authority=OWNER)
        self.observe(ALPHA)
        proposal = self.proposal_for(ALPHA)
        self.assert_unknown(proposal, "prior_source_association_unavailable")

    def test_owner_metadata_only_revision_does_not_reconcile_physical_scope(self):
        self.begin_alpha()
        self.observe(BETA)
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        self.write(rows, revision_authority=OWNER)
        error = self.refused_revision(self.proposal_for(BETA), "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")

    def test_missing_intake_scope_is_not_filled_by_coordinator_metadata(self):
        with patch("pod.pod_ticks.refresh"):
            self.begin_alpha()
        self.observe(BETA)
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        self.write(rows)
        self.assert_unknown(self.proposal_for(BETA), "prior_source_association_unavailable")

    def test_old_alpha_receipt_cannot_be_selected_in_later_beta_checkpoint(self):
        self.begin_alpha()
        self.accept()
        original = deepcopy(self.row())
        self.observe(BETA)
        for check in (CHECK.replace("alpha", "beta"), CHECK):
            rows = self.stored(); rows[0].pop("evidence", None)
            rows[0].update(check=check, state="blocked_external",
                           external={"party": "user", "need": "result", "unblocks_when": "verified"})
            self.write(rows, revision_authority=OWNER)
        before = self.saved()
        error = self.failure("obligation_unaccounted", self.accept)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.row()["receipts"], original["receipts"])

    def test_blocked_history_cannot_stage_old_alpha_receipt_as_current_beta_proof(self):
        self.begin_alpha()
        self.accept()
        original = deepcopy(self.row())
        self.observe(BETA)
        for check in (CHECK.replace("alpha", "beta"), CHECK):
            rows = self.stored(); rows[0].pop("evidence", None)
            rows[0].update(check=check, state="blocked_external",
                           external={"party": "user", "need": "result", "unblocks_when": "verified"})
            if check == CHECK:
                rows[0]["evidence"] = original["evidence"]
            self.write(rows, revision_authority=OWNER)
        before = self.saved()
        error = self.failure("obligation_unaccounted", self.accept)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual(self.saved(), before)


class CurrencyRepresentationTests(CurrencyCase):
    def test_missing_or_malformed_scope_fields_refuse_stored_reads(self):
        from pod.errors import PodError
        from pod.ledger import _validate_context
        self.begin_alpha(); self.accept()
        state = self.state()
        before = self.saved()
        scope = state["pod_ticks"]["criterion_scopes"]["O1"]
        self.assertEqual(len(scope["proofs"]), 1)
        for damage in ("partial_requirement", "missing_proof_definition", "invalid_proof_identity"):
            broken = deepcopy(state)
            target = broken["pod_ticks"]["criterion_scopes"]["O1"]
            key = next(iter(target["proofs"]))
            if damage == "partial_requirement":
                target.pop("source_digest")
            elif damage == "missing_proof_definition":
                target["proofs"][key].pop("definition")
            else:
                target["proofs"]["not-a-receipt-hash"] = target["proofs"].pop(key)
            with self.subTest(damage=damage), self.assertRaises(PodError):
                _validate_context(broken)
        self.assertEqual(self.saved(), before)


class LegacyCurrencyTests(CurrencyCase):
    setUpClass = classmethod(fixtures.IssueRevisionLegacyTests.setUpClass.__func__)
    previous_record = fixtures.IssueRevisionLegacyTests.previous_record
    old_proposal = fixtures.IssueRevisionLegacyTests.old_proposal

    def test_genuine_legacy_n1_resolves_beta_at_first_acceptance_and_resets_alpha(self):
        produced = self.previous_record(nested=True, unticked=True, observe_before_proof=True)
        beta = self.issue.body
        self.assertEqual(self.row()["receipts"][0]["accepted_seq"], 3)
        before = deepcopy(self.row())
        self.issue.body = beta.replace("beta", "alpha")
        proposal = self.old_proposal(produced)
        proposal["issue_revision_data"] = self.historical_data(beta)
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")
        self.revise(self.remap(proposal))
        self.assertEqual(self.row()["receipts"], before["receipts"])
        self.assertFalse(self.line()["proven"])

    def test_approved_legacy_same_write_first_acceptance_declares_beta_scope(self):
        from pod.github import pod_blocks
        produced = self.previous_record(nested=True, unticked=True, observe_with_proof=True)
        beta = self.issue.body
        before = deepcopy(self.row())
        self.assertEqual(before["receipts"][0]["accepted_seq"], 2)
        proposal = self.old_proposal(produced)
        proposal["issue_revision_data"] = self.historical_data(beta)
        self.revise(proposal)
        self.assertEqual(self.row(), before)
        self.assertTrue(self.line()["proven"])
        fact = self.state()["checkpoint"]["revision_decisions"][-1]["scope_facts"]["previous"][0]
        self.assertEqual(fact["scope"]["block_sha256"], pod_blocks(beta)[1][0]["sha256"])
        self.assertNotEqual(fact["scope"]["block_sha256"], pod_blocks(produced["original_body"])[1][0]["sha256"])

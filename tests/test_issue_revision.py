from tests.history_support import publish_preceding
"""Synthetic issue-revision boundary controls; no authentic host producer is exercised."""
from tests.history_support import compact

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest import TestCase
from unittest.mock import patch

import yaml

from pod.config import effective
from pod.errors import PodError
from pod.github import issue_intake
from pod.ledger import _path, read, state_root
from pod.util import digest
from tests.pod_tick_support import TickCase


class IssueRevisionCase(TickCase):
    def native_read(self, argv, **kwargs):
        result = super().native_read(argv, **kwargs)
        if argv[:2] == ["terminal", "show"]:
            result["result"]["terminal"]["incarnationId"] = "synthetic-incarnation"
        return result

    def setUp(self):
        super().setUp()
        self.configure(["/synthetic/verifier"])
        self.requests = []
        self.verdict_changes = {}
        self.verifier_callback = None
        replacement = patch("pod.relay._output", side_effect=self.verify_output)
        replacement.start(); self.addCleanup(replacement.stop)

    def configure(self, command):
        path = self.root / "config/pod/config.yaml"
        config = yaml.safe_load(path.read_text())
        config.pop("authorization_verifier", None)
        if command:
            config["authorization_verifier"] = command
        path.write_text(yaml.safe_dump(config))

    def verify_output(self, command, request):
        self.requests.append(deepcopy(request))
        if self.verifier_callback is not None:
            self.verifier_callback(len(self.requests))
        return json.dumps({**request, "verified": True, "owner_source": "synthetic-owner",
                           "decided_at": "2026-10-01T00:00:00Z", "single_use": "synthetic-key",
                           **self.verdict_changes}).encode()

    def begin(self):
        self.issue.body = "- [ ] **PoD#1** old requirement"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        return self.write([self.criterion(check="**PoD#1** old requirement")],
                          objective_source=source, governance={"base_ref": "target"})

    def proposal(self):
        self.issue.body += "\n- [ ] **PoD#2** new requirement"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = self.stored()
        rows.append(self.criterion("waiting", id="O2", source={"ref": "PoD#2"},
                                   check="**PoD#2** new requirement",
                                   wait={"class": "sequenced", "referent": "O1"}))
        return {"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                "obligations": rows, "objective_source": source, "criteria": ["PoD#1", "PoD#2"],
                "revision_authority": {"operation": "issue_revision", "provenance": "user_direct",
                    "channel": "relayed", "instruction": "Reconcile the approved issue criteria",
                    "relay": {"reference": "synthetic-decision"}}}

    def revise(self, proposal):
        fields = deepcopy(proposal)
        return self.write(fields.pop("obligations"), **fields)

    def receipts(self):
        root = state_root(self.project) / ".relay-authorizations"
        return {p.name: p.read_bytes() for p in root.glob("*.json")}

    def refused_revision(self, proposal, code=None):
        before, receipts, writes = self.saved(), self.receipts(), deepcopy(self.issue.writes)
        with self.assertRaises(PodError) as caught:
            self.revise(proposal)
        if code:
            self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(self.issue.writes, writes)
        return caught.exception

    @staticmethod
    def new_criterion(number):
        return {"id": f"O{number}", "kind": "criterion", "provenance": "objective",
                "source": {"ref": f"PoD#{number}"}, "check": f"**PoD#{number}** requirement {number}",
                "state": "blocked_external", "external": {"party": "user", "need": "provide the result",
                                                          "unblocks_when": "result is supplied"}}

    @staticmethod
    def historical_data(*bodies):
        return {"historical_bodies": [{"body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body}
                                       for body in bodies]}


class IssueRevisionAcceptanceTests(IssueRevisionCase):
    def test_verified_exact_source_criteria_revision_uses_current_map_and_native_binding(self):
        self.begin()
        previous = read(self.project, "objective")["checkpoint"]
        proposal = self.proposal()
        result = self.revise(proposal)
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual((checkpoint["seq"], checkpoint["revision"]), (2, 2))
        self.assertEqual(checkpoint["criteria"], ["PoD#1", "PoD#2"])
        self.assertEqual(checkpoint["native_refs"], previous["native_refs"])
        self.assertEqual(result["pod_tick_intake"]["unmapped"], [])
        request = self.requests[0]
        self.assertEqual(request["action"], "issue_revision")
        self.assertEqual(request["issue_revision"]["map"], {
            "seq": 1, "revision": 1, "policy_revision": effective(self.project)["revision"]})
        self.assertEqual(request["issue_revision"]["coordinator"], {
            "handle": "owner", "incarnation": "synthetic-incarnation", "run": "run",
            "runtime": "runtime", "generation": 1})
        self.assertEqual(len(self.receipts()), 1)

    def test_prior_observation_allows_equal_source_digests_for_material_reconciliation(self):
        self.begin()
        proposal = self.proposal()
        self.write(objective_source=proposal["objective_source"])
        observed = read(self.project, "objective")["checkpoint"]
        proposal["seq"] = observed["seq"] + 1
        self.revise(proposal)
        decision = read(self.project, "objective")["checkpoint"]["revision_decisions"][-1]
        source = decision["relay_verification"]["issue_revision"]["source"]
        self.assertEqual(source["previous_digest"], source["proposed_digest"])
        self.assertEqual(decision["previous"]["source"], observed["objective_source"])
        self.assertEqual(decision["previous"]["criteria"], ["PoD#1"])

    def test_delivery_only_source_observation_does_not_need_verifier(self):
        self.begin()
        self.configure(None)
        self.issue.body += "\nDelivery metadata edited"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.receipts(), {})
        self.assertEqual(read(self.project, "objective")["checkpoint"]["criteria"], ["PoD#1"])

    def test_changed_requirement_uses_fresh_id_and_leaves_own_tick_due_for_untick(self):
        self.begin(); self.prove(); self.tick()
        before = read(self.project, "objective")
        self.issue.body = "- [x] **PoD#1** new requirement"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = self.op("map")["obligations"]
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "approved requirement changed"})
        rows[0].pop("evidence", None)
        rows.append({**self.new_criterion(1), "id": "R1", "check": "**PoD#1** new requirement"})
        authority = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                     "instruction": "Reconcile the approved issue criteria", "relay": {"reference": "synthetic-decision"}}
        result = self.write(rows, seq=before["checkpoint"]["seq"] + 1, objective_source=source,
                            revision_authority=authority)
        self.assertEqual(result["pod_tick_intake"]["multiply_mapped"], [])
        line = self.line()
        self.assertEqual([row["id"] for row in line["criteria"]], ["R1"])
        self.assertFalse(line["proven"])
        self.assertEqual((line["authorship"], line["direction"], line["due"]), ("own", "untick", True))
        state = read(self.project, "objective")
        self.assertEqual(state["checkpoint"]["revision_decisions"][-1]["previous"]["pod_ticks"], before["pod_ticks"])
        self.assertEqual(state["checkpoint"]["obligations"][0]["definition"], before["checkpoint"]["obligations"][0]["definition"])
        self.write(self.stored(), criteria=["PoD#1"])
        self.assertEqual(self.tick()["receipt"]["direction"], "untick")

    def test_existing_native_attempt_and_unresolved_result_facts_are_unchanged(self):
        self.begin()
        self.port.state = "running"
        frozen = self.packet(["O1"])
        self.start("existing-task", frozen)
        before = deepcopy(read(self.project, "objective"))
        proposal = self.proposal()
        proposal["obligations"][-1].pop("wait")
        proposal["obligations"][-1].update(state="blocked_external", external={
            "party": "user", "need": "provide the result", "unblocks_when": "result is supplied"})
        self.revise(proposal)
        after = read(self.project, "objective")
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(after["source_rejections"], before["source_rejections"])
        self.assertEqual(after["checkpoint"]["native_refs"], before["checkpoint"]["native_refs"])
        self.assertEqual(after["checkpoint"]["revision_decisions"][-1]["previous"]["obligations"], before["checkpoint"]["obligations"])

    def test_five_to_eight_keeps_prior_definitions_failed_unknown_and_tick_authorship(self):
        self.issue.body = "\n".join(f"- [ ] **PoD#{n}** requirement {n}" for n in range(1, 6))
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = [self.new_criterion(n) for n in range(1, 6)]
        self.write(rows, objective_source=source, criteria=[f"PoD#{n}" for n in range(1, 6)],
                   governance={"base_ref": "target"})
        for status in ("FAILED", "UNAVAILABLE"):
            rows = self.stored()
            rows[3]["evidence"] = [self.proof("O4", status=status, reference="prior-" + status)]
            self.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)])
        rows = self.stored()
        for row in rows:
            row.pop("external", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        self.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)])
        self.tick(1)
        self.issue.refuse_next = 403
        self.tick(2)
        self.issue.lose_next = "landed"
        self.tick(3)
        before = deepcopy(read(self.project, "objective"))
        self.assertEqual(before["pod_ticks"]["authors"]["1"], "own")
        self.issue.body = "\n".join(f"- [ ] **PoD#{n}** requirement {n}" for n in range(2, 10))
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = self.stored()
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "PoD#1 removed from the approved source"})
        rows[0].pop("evidence", None)
        rows.extend(self.new_criterion(n) for n in range(6, 10))
        authority = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                     "instruction": "Reconcile the approved issue criteria", "relay": {"reference": "synthetic-decision"}}
        result = self.write(rows, seq=before["checkpoint"]["seq"] + 1, objective_source=source,
                            criteria=[f"PoD#{n}" for n in range(2, 10)], revision_authority=authority)
        after = read(self.project, "objective")
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["previous"]["obligations"], before["checkpoint"]["obligations"])
        self.assertEqual(decision["previous"]["pod_ticks"], before["pod_ticks"])
        self.assertEqual(decision["previous"]["source"], before["checkpoint"]["objective_source"])
        self.assertEqual(len(after["checkpoint"]["obligations"]), 9)
        self.assertEqual([row["state"] for row in after["checkpoint"]["obligations"]][0], "withdrawn")
        self.assertEqual(after["checkpoint"]["obligations"][3]["receipts"], before["checkpoint"]["obligations"][3]["receipts"])
        self.assertNotIn("1", after["pod_ticks"]["authors"])
        self.assertEqual(after["pod_ticks"]["requests"], before["pod_ticks"]["requests"])
        for row in after["checkpoint"]["obligations"][5:]:
            self.assertNotIn("evidence", row)
            self.assertEqual(row["receipts"], [])
        self.assertEqual(result["pod_tick_intake"]["unmapped"], [])
        public = self.op("map")
        exposed = public["revision_decisions"][-1]
        self.assertEqual(exposed["relay_verification"], decision["relay_verification"])
        self.assertEqual(exposed["previous"], decision["previous"])
        self.assertEqual(after["decision_channels"], 4)
        self.assertEqual(result["report"]["decision_channels"], 4)


class IssueRevisionCorrectionTests(IssueRevisionCase):
    def source_revision(self, body, rows, criteria):
        self.issue.body = body
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        return self.write(rows, seq=read(self.project, "objective")["checkpoint"]["seq"] + 1,
            objective_source=source, criteria=criteria, revision_authority={
            "operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
            "instruction": "Reconcile approved source requirements", "relay": {"reference": "synthetic-decision"}})

    def test_unchanged_paraphrased_five_to_eight_keeps_proof_and_own_tick(self):
        self.issue.body = "\n".join(f"- [ ] **PoD#{n}** requirement {n}" for n in range(1, 6))
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([{**self.new_criterion(n), "check": f"Result {n} meets acceptance"} for n in range(1, 6)],
                   objective_source=source, criteria=[f"PoD#{n}" for n in range(1, 6)],
                   governance={"base_ref": "target"})
        rows = self.stored()
        for row in rows:
            row.pop("external")
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        self.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)]); self.tick()
        before = read(self.project, "objective")
        rows = self.stored() + [self.new_criterion(n) for n in range(6, 9)]
        self.source_revision(self.issue.body + "\n" + "\n".join(
            f"- [ ] **PoD#{n}** requirement {n}" for n in range(6, 9)), rows,
            [f"PoD#{n}" for n in range(1, 9)])
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][:5], before["checkpoint"]["obligations"])
        self.assertTrue(self.line()["proven"])
        self.assertEqual((self.line()["status"], self.line()["authorship"], self.line()["due"]),
                         ("current", "own", False))

    def test_unchanged_unrelated_owner_criterion_is_preserved(self):
        self.begin()
        extra = self.criterion("blocked_external", id="EXTRA", provenance="user_direct",
            source={"instruction": "Keep the owner acceptance condition"},
            external={"party": "user", "need": "answer", "unblocks_when": "answered"})
        self.write(self.stored() + [extra], revision_authority={
            "provenance": "user_direct", "instruction": "Keep the owner acceptance condition"})
        before = read(self.project, "objective")["checkpoint"]["obligations"][1]
        self.revise(self.proposal())
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][1], before)

    def test_existing_owner_criterion_cannot_be_redefined_by_source_authority(self):
        self.begin()
        extra = self.criterion("blocked_external", id="EXTRA", provenance="user_direct",
            source={"instruction": "Keep the owner acceptance condition"},
            external={"party": "user", "need": "answer", "unblocks_when": "answered"})
        self.write(self.stored() + [extra], revision_authority={
            "provenance": "user_direct", "instruction": "Keep the owner acceptance condition"})
        proposal = self.proposal()
        proposal["obligations"][1]["check"] = "Different owner acceptance"
        self.refused_revision(proposal, "issue_revision_refused")

    def test_unchanged_owner_criterion_waiting_on_source_criterion_is_preserved(self):
        self.begin()
        extra = self.criterion("waiting", id="EXTRA", provenance="user_direct",
            source={"instruction": "Check the owner's additional condition"},
            wait={"class": "sequenced", "referent": "O1"})
        self.write(self.stored() + [extra], revision_authority={
            "provenance": "user_direct", "instruction": "Check the owner's additional condition"})
        before = read(self.project, "objective")["checkpoint"]["obligations"][1]
        self.revise(self.proposal())
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][1], before)

    def test_legacy_missing_definition_source_fact_has_precise_hold_and_no_effect(self):
        # A legitimate preceding writer omitted these optional observations.
        # Do not backfill an old definition from a newly read source.
        with patch("pod.pod_ticks.bind_criterion_scopes"):
            self.begin()
        proposal = self.proposal()
        before = read(self.project, "objective")
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["criterion"], "O1")
        self.assertEqual(error.detail["definition"], before["checkpoint"]["obligations"][0]["definition"])
        self.assertIn("complete prior physical source block", error.detail["missing_fact"])

    def test_approved_current_mapping_supersedes_old_owner_withdrawal_without_rewriting_it(self):
        self.issue.body = "- [ ] **PoD#1** requirement 1\n- [ ] **PoD#2** requirement 2"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.new_criterion(1), self.new_criterion(2)], objective_source=source,
                   criteria=["PoD#1", "PoD#2"], governance={"base_ref": "target"})
        rows = self.stored()
        rows[1].pop("external")
        rows[1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "draft withdrawn"})
        self.write(rows, criteria=["PoD#1", "PoD#2"], revision_authority={
            "provenance": "user_direct", "instruction": "Withdraw the draft"})
        withdrawn = read(self.project, "objective")["checkpoint"]["obligations"][1]
        result = self.source_revision(self.issue.body.replace("requirement 2", "approved requirement 2"),
            self.stored() + [{**self.new_criterion(2), "id": "R2", "check": "**PoD#2** approved requirement 2"}],
            ["PoD#1", "PoD#2"])
        self.assertEqual(result["pod_tick_intake"]["multiply_mapped"], [])
        self.assertEqual([row["id"] for row in self.line(2)["criteria"]], ["R2"])
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][1], withdrawn)
        rows = self.stored(); rows[-1].pop("external")
        rows[-1].update(state="satisfied", evidence=[self.proof("R2")])
        self.write(rows, criteria=["PoD#1", "PoD#2"]); self.tick(2)
        self.assertTrue(self.line(2)["proven"])
        rows = self.stored(); rows[-1].pop("evidence")
        rows[-1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "current criterion withdrawn"})
        self.write(rows, criteria=["PoD#1", "PoD#2"], revision_authority={
            "provenance": "user_direct", "instruction": "Withdraw the current criterion"})
        self.assertEqual(self.line(2)["criteria"], [])
        self.assertFalse(self.line(2)["proven"])
        self.assertEqual(self.line(2)["direction"], "untick")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][1], withdrawn)

    def test_changed_continuation_cannot_carry_passing_header_proof(self):
        self.issue.body = "- [ ] **PoD#1** Verify output\n  Acceptance requires alpha."
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.criterion(check="**PoD#1** Verify output")], objective_source=source,
                   governance={"base_ref": "target"})
        self.prove()
        proposal = self.proposal()
        self.issue.body = self.issue.body.replace("requires alpha", "requires beta")
        proposal["objective_source"] = issue_intake(self.project, self.locator, port=self.issue)["source"]
        proposal["obligations"][-1].pop("wait")
        proposal["obligations"][-1].update(state="active", executor="coordinator")
        self.refused_revision(proposal, "issue_revision_refused")

    def replace_changed_block(self, newline):
        old = "**PoD#1** Verify output" + newline + "  Acceptance:" + newline + "    - nested alpha"
        self.issue.body = "- [ ] " + old
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.criterion(check="The output meets the agreed checks")], objective_source=source,
                   governance={"base_ref": "target"})
        self.prove(); self.tick()
        before = read(self.project, "objective")
        new = old.replace("nested alpha", "nested beta")
        rows = self.stored()
        rows[0].pop("evidence")
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "nested acceptance changed"})
        rows.append({**self.new_criterion(1), "id": "R1", "check": new})
        self.source_revision("- [x] " + new, rows, ["PoD#1"])
        state = read(self.project, "objective")
        self.assertEqual(state["checkpoint"]["obligations"][0]["definition"],
                         before["checkpoint"]["obligations"][0]["definition"])
        self.assertEqual(state["checkpoint"]["revision_decisions"][-1]["previous"]["obligations"],
                         before["checkpoint"]["obligations"])
        replacement = state["checkpoint"]["obligations"][1]
        self.assertNotIn("evidence", replacement)
        self.assertEqual(replacement["receipts"], [])
        self.assertEqual((self.line()["proven"], self.line()["direction"], self.line()["due"]),
                         (False, "untick", True))

    def test_changed_nested_lf_block_resets_proof_and_due_own_tick(self):
        self.replace_changed_block("\n")

    def test_changed_nested_crlf_block_resets_proof_and_due_own_tick(self):
        self.replace_changed_block("\r\n")

    def test_long_complete_block_uses_strict_fingerprint_and_resets_changed_proof(self):
        text = "**PoD#1** Verify output\n  Acceptance:\n    - " + "alpha " * 150
        self.issue.body = "- [ ] " + text
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.criterion(check="Verify the full acceptance block")], objective_source=source,
                   governance={"base_ref": "target"})
        self.prove(); self.tick()
        before = read(self.project, "objective")
        changed = text + "\n    - beta after the check bound"
        self.issue.body = "- [x] " + changed
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = self.stored(); rows[0].pop("evidence")
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "complete block changed"})
        fingerprint = hashlib.sha256(changed.encode()).hexdigest()
        rows.append({**self.new_criterion(1), "id": "R1", "check": "PoD#1 complete source block sha256:" + fingerprint})
        authority = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                     "instruction": "Reconcile the complete block", "relay": {"reference": "synthetic-decision"}}
        bad = deepcopy(rows); bad[-1]["check"] = "**PoD#1** Verify output"
        self.refused_revision({"seq": before["checkpoint"]["seq"] + 1, "obligations": bad,
            "objective_source": source, "criteria": ["PoD#1"], "revision_authority": authority}, "issue_revision_refused")
        self.write(rows, seq=before["checkpoint"]["seq"] + 1, objective_source=source, revision_authority=authority)
        self.assertLess(len(rows[-1]["check"]), 512)
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["criterion_scopes"]["R1"]["block_sha256"], fingerprint)
        self.assertEqual((self.line()["proven"], self.line()["direction"]), (False, "untick"))
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][0]["receipts"],
                         before["checkpoint"]["obligations"][0]["receipts"])

    def test_source_only_observation_does_not_replace_prior_definition_scope(self):
        self.begin(); self.prove()
        before = deepcopy(read(self.project, "objective")["pod_ticks"]["criterion_scopes"])
        self.issue.body += "\n  Changed acceptance"
        self.rebind()
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["criterion_scopes"], before)
        proposal = self.proposal()
        proposal["obligations"][-1].pop("wait")
        proposal["obligations"][-1].update(state="active", executor="coordinator")
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")

    def retire_first_mapping(self, *, own=False, unread_refresh=False):
        self.issue.body = "- [ ] **PoD#1** requirement 1\n- [ ] **PoD#2** requirement 2"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write([self.new_criterion(1), self.new_criterion(2)], objective_source=source,
                   criteria=["PoD#1", "PoD#2"], governance={"base_ref": "target"})
        if own:
            rows = self.stored(); rows[0].pop("external")
            rows[0].update(state="satisfied", evidence=[self.proof("O1")])
            self.write(rows, criteria=["PoD#1", "PoD#2"]); self.tick()
        rows = self.stored(); rows[0].pop("external", None); rows[0].pop("evidence", None)
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "source removed PoD#1"})
        actual_issue, reads = self.issue.issue, []
        def issue(**arguments):
            reads.append(arguments)
            if unread_refresh and len(reads) == 3:
                raise PodError("issue_access_unavailable", "synthetic refresh failure")
            return actual_issue(**arguments)
        with patch.object(self.issue, "issue", side_effect=issue):
            self.source_revision("- [ ] **PoD#2** requirement 2", rows, ["PoD#2"])
        self.issue.body = "- [x] **PoD#1** requirement 1\n" + self.issue.body
        self.rebind()

    def test_real_revision_mapping_loss_unticked_is_unmapped_D2(self):
        self.retire_first_mapping()
        self.issue.body = self.issue.body.replace("- [x]", "- [ ]", 1); self.rebind()
        self.assertEqual(self.line()["criteria"], [])
        writes = deepcopy(self.issue.writes)
        self.failure("unmapped", self.tick)
        self.assertFalse(self.line()["due"])
        self.assertEqual(self.issue.writes, writes)

    def test_real_revision_mapping_loss_own_tick_is_due_and_unticks_D7(self):
        # A transient refresh failure preserves the earlier own-tick record;
        # supported observations then reintroduce its line without a mapping.
        self.retire_first_mapping(own=True, unread_refresh=True)
        self.assertEqual(self.line()["criteria"], [])
        self.assertEqual((self.line()["authorship"], self.line()["direction"]), ("own", "untick"))
        rows = self.stored(); rows[1].pop("external")
        rows[1].update(state="satisfied", evidence=[self.proof("O2")])
        self.write(rows, criteria=["PoD#2"]); self.tick(2)
        self.failure("pod_ticks_due", lambda: self.write(close=True, criteria=["PoD#2"]))
        result = self.tick()
        self.assertEqual(result["receipt"]["direction"], "untick")
        self.assertFalse(self.line()["ticked"])

    def test_real_revision_mapping_loss_leaves_other_actor_tick_untouched(self):
        self.retire_first_mapping()
        self.assertEqual(self.line()["criteria"], [])
        self.assertEqual(self.tick()["status"], "ticked_without_pod_proof")
        self.assertEqual(self.issue.writes, [])


class SourceBlockTests(TestCase):
    def test_complete_scope_includes_nested_and_lazy_continuation_but_stops_at_siblings(self):
        from pod.github import pod_blocks
        body = ("- [ ] **PoD#1** header\r\n"
                "lazy continuation\r\n\r\n  Acceptance:\r\n    - nested condition\r\n"
                "\r\nAn unrelated paragraph\r\n- [ ] **PoD#2** next\r\n"
                "- unrelated sibling\r\n  unrelated detail")
        blocks = pod_blocks(body)
        self.assertEqual(blocks[1][0]["text"],
            "**PoD#1** header\r\nlazy continuation\r\n\r\n  Acceptance:\r\n    - nested condition")
        self.assertEqual(blocks[2][0]["text"], "**PoD#2** next")

    def test_identity_ignores_only_header_checkbox_and_final_terminator(self):
        from pod.github import pod_blocks
        def block(body):
            return pod_blocks(body)[1][0]["sha256"]
        body = "- [ ] **PoD#1** header\n  - nested [ ] alpha"
        self.assertEqual(block(body), block(body + "\n"))
        self.assertEqual(block(body), block(body.replace("- [ ] **", "- [X] **")))
        for changed in (body.replace("alpha", "beta"), body.replace("\n", "\r\n"), body + " ",
                        body.replace("nested [ ]", "nested [x]")):
            self.assertNotEqual(block(body), block(changed))

    def test_recognition_excludes_quoted_indented_and_fenced_decoys(self):
        from pod.github import pod_blocks
        self.assertEqual(list(pod_blocks(
            "> - [ ] **PoD#8** quoted\n - [ ] **PoD#7** indented\n"
            "```markdown\n- [ ] **PoD#6** fenced\n```\n- [ ] **PoD#1** real")), [1])


class IssueRevisionLegacyTests(IssueRevisionCase):
    @classmethod
    def setUpClass(cls):
        from tests.issue_revision_previous_writer import BASELINE
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="issue-previous-", dir=parent)
        cls.addClassCleanup(temporary.cleanup)
        cls.bundle = Path(temporary.name)
        archive = cls.bundle / "previous.tar"
        subprocess.run(["git", "archive", "-o", str(archive), BASELINE, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def previous_record(self, *, delegated=False, unticked=False, unproved=False,
                        nested=False, long=False, conflicting=False,
                        observe_before_proof=False, observe_with_proof=False):
        from pod.github import repository_context
        from tests.issue_revision_previous_writer import BASELINE
        destination = self.root / "earlier"; destination.mkdir()
        repository = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, str(repository / "tests/issue_revision_previous_writer.py"),
                                    str(destination), *(["delegated"] if delegated else []),
                                    *(["unticked"] if unticked else []), *(["unproved"] if unproved else []),
                                    *(["nested"] if nested else []), *(["long"] if long else []),
                                    *(["conflicting"] if conflicting else []),
                                    *(["observe-before-proof"] if observe_before_proof else []),
                                    *(["observe-with-proof"] if observe_with_proof else [])], cwd=repository,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(repository)))},
            text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        produced = json.loads((destination / "writer.json").read_text())
        self.assertEqual(produced["writer"], BASELINE)
        self.root, self.project, self.candidate = destination, destination / "project", produced["candidate"]
        os.environ.update(XDG_STATE_HOME=str(destination / "state"), XDG_CONFIG_HOME=str(destination / "config"))
        self.issue.body = produced["body"]
        context = repository_context(self.project)
        self.port.placement.update(repository=context["repository"], repo_key=context["repo_key"],
                                   path=context["worktree"], branch=context["branch"])
        self.port.workers = produced["workers"]
        self.configure(["/synthetic/verifier"])
        before = self.saved()
        self.assertEqual(read(self.project, "objective"), produced["state"])
        self.op("map")
        self.assertEqual(self.saved(), before)
        return produced

    def test_genuine_previous_writer_reads_and_missing_identity_holds_without_rewrite(self):
        self.previous_record()
        proposal = self.proposal()
        proposal["obligations"][-1]["id"] = "O6"
        proposal["obligations"][-1]["source"] = {"ref": "PoD#6"}
        proposal["obligations"][-1]["check"] = "**PoD#6** new requirement"
        self.issue.body = self.issue.body.rsplit("\n", 1)[0] + "\n- [ ] **PoD#6** new requirement"
        proposal["objective_source"] = issue_intake(self.project, self.locator, port=self.issue)["source"]
        proposal["criteria"] = [f"PoD#{n}" for n in range(1, 7)]
        proposal["obligations"][-1].pop("wait")
        proposal["obligations"][-1].update(state="active", executor="coordinator")
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "prior_criterion_source_unavailable")

    def test_genuine_admission_records_source_and_definition_but_not_criterion_report_join(self):
        produced = self.previous_record(delegated=True)
        state = read(self.project, "objective")
        admission = next(iter(state["admissions"].values()))
        criterion = state["checkpoint"]["obligations"][0]
        self.assertEqual(admission["binding"]["definitions"]["O1"], criterion["definition"])
        self.assertEqual(admission["recovery"]["checkpoint_binding"]["objective_source"], produced["source"])
        self.assertEqual(admission["report"]["status"], "validated_observation")
        self.assertEqual(admission["report"]["observation"]["assignment"], admission["packet_id"])
        self.assertEqual(admission["report"]["observation"]["evidence"], [])
        self.assertNotIn("attempt", criterion["receipts"][0]["evidence"])
        self.assertNotIn("reported_seq", criterion["receipts"][0])
        self.assertGreater(criterion["receipts"][0]["accepted_seq"], admission["admitted_seq"])

    def test_genuine_tick_source_keeps_exact_proof_sequence_after_later_observation(self):
        produced = self.previous_record()
        before = read(self.project, "objective")
        proof_seq = before["checkpoint"]["seq"]
        request = deepcopy(before["pod_ticks"]["requests"]["1"])
        self.assertEqual(request["seq"], proof_seq)
        self.assertEqual(request["source"], produced["source"])
        self.assertTrue(all(row["receipts"][0]["accepted_seq"] == proof_seq
                            for row in before["checkpoint"]["obligations"]))
        self.issue.body += "\n\nDelivery metadata"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source, criteria=[f"PoD#{n}" for n in range(1, 6)])
        after = read(self.project, "objective")
        self.assertGreater(after["checkpoint"]["seq"], proof_seq)
        self.assertEqual(after["pod_ticks"]["requests"]["1"], request)
        self.assertEqual(after["checkpoint"]["obligations"], before["checkpoint"]["obligations"])
        self.assertNotIn("criterion_scopes", after["pod_ticks"])
        self.assertEqual(self.requests, [])

    def old_proposal(self, produced, *, observe=False, replace=False):
        self.issue.body += "\n" + "\n".join(f"- [ ] **PoD#{n}** requirement {n}" for n in range(6, 9))
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        if observe:
            self.write(objective_source=source, criteria=[f"PoD#{n}" for n in range(1, 6)])
        rows = self.stored()
        if replace:
            for row in rows:
                for key in ("external", "executor", "wait", "evidence"):
                    row.pop(key, None)
                row.update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "Current remap; prior scope fact unknown"})
            rows += [{**self.new_criterion(n), "id": f"R{n}"} for n in range(1, 6)]
        rows += [self.new_criterion(n) for n in range(6, 9)]
        return {"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1, "obligations": rows,
            "objective_source": source, "criteria": [f"PoD#{n}" for n in range(1, 9)],
            "revision_authority": {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                "instruction": "Reconcile exact current source; unknown mappings await new proof",
                "relay": {"reference": "synthetic-decision"}}}

    def preserve_old_proof(self, *, delegated=False, observe=False, long=False):
        produced = self.previous_record(delegated=delegated, long=long)
        before = read(self.project, "objective")
        proposal = self.old_proposal(produced, observe=observe)
        previous_source = read(self.project, "objective")["checkpoint"]["objective_source"]
        proposal["issue_revision_data"] = self.historical_data(produced["original_body"])
        self.revise(proposal)
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][:5], before["checkpoint"]["obligations"])
        self.assertEqual(after["admissions"], before["admissions"])
        line = self.line()
        self.assertEqual((line["proven"], line["status"], line["due"]), (True, "current", False))
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["previous"]["source"], previous_source)
        self.assertTrue(all(fact["status"] == "known" for fact in decision["scope_facts"]["previous"]))
        self.assertEqual(decision["scope_facts"]["data_digests"], [produced["source"]["body_sha256"]])
        raw = _path(self.project, "objective").read_text()
        self.assertNotIn("historical_bodies", raw)
        self.assertNotIn(json.dumps(produced["original_body"]), raw)
        self.assertTrue(all("issue_revision_data" not in request for request in self.requests))

    def test_genuine_direct_five_to_eight_body_data_preserves_proof_and_tick(self):
        self.preserve_old_proof()

    def test_genuine_direct_body_data_after_observation_preserves_actual_previous_binding(self):
        self.preserve_old_proof(observe=True)

    def test_genuine_delegated_body_data_preserves_definitions_proof_and_native_history(self):
        self.preserve_old_proof(delegated=True, observe=True)

    def test_genuine_long_paraphrased_block_preserves_full_scope_proof_with_data(self):
        self.preserve_old_proof(observe=True, long=True)

    def test_genuine_first_admission_body_data_preserves_unproved_definition_and_running_attempt(self):
        produced = self.previous_record(delegated=True, unproved=True)
        before = read(self.project, "objective")
        proposal = self.old_proposal(produced, observe=True)
        proposal["issue_revision_data"] = self.historical_data(produced["original_body"])
        self.revise(proposal)
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][:5], before["checkpoint"]["obligations"])
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertFalse(self.line()["proven"])
        self.assertTrue(all(fact["status"] == "known" for fact in after["checkpoint"]["revision_decisions"][-1]["scope_facts"]["previous"]))

    def test_genuine_unproved_unknown_remap_accepts_current_approval_without_old_proof(self):
        produced = self.previous_record(unproved=True)
        before = read(self.project, "objective")
        proposal = self.old_proposal(produced, observe=True, replace=True)
        self.revise(proposal)
        after = read(self.project, "objective")
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertTrue(all(fact["status"] == "unknown" for fact in decision["scope_facts"]["previous"]))
        self.assertEqual(decision["previous"]["obligations"], before["checkpoint"]["obligations"])
        self.assertTrue(all(row["state"] == "withdrawn" for row in after["checkpoint"]["obligations"][:5]))
        self.assertTrue(all(not row["receipts"] and "evidence" not in row for row in after["checkpoint"]["obligations"][5:]))
        self.assertTrue(all(not line["proven"] for line in self.ticks()["lines"]))

    def test_genuine_unknown_old_pass_keeps_receipts_but_new_mapping_awaits_proof(self):
        produced = self.previous_record(unticked=True)
        before = read(self.project, "objective")
        proposal = self.old_proposal(produced, observe=True, replace=True)
        self.revise(proposal)
        after = read(self.project, "objective")
        decision = after["checkpoint"]["revision_decisions"][-1]
        self.assertTrue(all(fact["status"] == "unknown" for fact in decision["scope_facts"]["previous"]))
        for old, current in zip(before["checkpoint"]["obligations"], after["checkpoint"]["obligations"][:5]):
            self.assertEqual((old["definition"], old["receipts"]), (current["definition"], current["receipts"]))
        self.assertFalse(self.line()["proven"])

    def test_later_admission_and_current_body_cannot_backdate_old_proof(self):
        produced = self.previous_record(unticked=True)
        proposal = self.old_proposal(produced, observe=True)
        rows = self.stored(); rows[0].update(state="active", executor="coordinator")
        self.write(rows, criteria=[f"PoD#{n}" for n in range(1, 6)])
        packet = self.packet(["O1"], criteria=[f"PoD#{n}" for n in range(1, 6)])
        self.start("existing-task", packet)
        proposal.update(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1,
                        obligations=self.stored() + [self.new_criterion(n) for n in range(6, 9)],
                        issue_revision_data=self.historical_data(self.issue.body))
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "body_data_unreferenced")

    def test_reusing_reference_after_observation_cannot_move_accepted_sequence(self):
        produced = self.previous_record(unticked=True)
        accepted = read(self.project, "objective")["checkpoint"]["obligations"][0]["receipts"][0]["accepted_seq"]
        proposal = self.old_proposal(produced, observe=True)
        self.write(self.stored(), criteria=[f"PoD#{n}" for n in range(1, 6)])
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][0]["receipts"][0]["accepted_seq"], accepted)
        proposal.update(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1,
                        obligations=self.stored() + [self.new_criterion(n) for n in range(6, 9)],
                        issue_revision_data=self.historical_data(self.issue.body))
        self.assertEqual(self.refused_revision(proposal, "issue_revision_refused").detail["reason"], "body_data_unreferenced")

    def test_genuine_changed_nested_scope_data_resets_only_changed_proof_and_own_tick(self):
        produced = self.previous_record(nested=True)
        before = read(self.project, "objective")
        self.issue.body = self.issue.body.replace("requires alpha", "requires beta")
        proposal = self.old_proposal(produced)
        proposal["issue_revision_data"] = self.historical_data(produced["original_body"])
        self.assertEqual(self.refused_revision(proposal, "issue_revision_refused").detail["reason"],
                         "changed_criterion_requires_replacement")
        proposal["obligations"][0].pop("evidence")
        proposal["obligations"][0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "nested acceptance changed"})
        proposal["obligations"].append({**self.new_criterion(1), "id": "R1",
            "check": "**PoD#1** requirement 1\n  Acceptance requires beta."})
        self.revise(proposal)
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][1:5], before["checkpoint"]["obligations"][1:5])
        self.assertEqual(after["checkpoint"]["obligations"][0]["receipts"], before["checkpoint"]["obligations"][0]["receipts"])
        line = self.line()
        self.assertEqual((line["proven"], line["authorship"], line["direction"]), (False, "own", "untick"))

    def test_selected_proof_with_missing_old_body_cannot_use_only_later_body(self):
        produced = self.previous_record(conflicting=True)
        proposal = self.old_proposal(produced)
        proposal["issue_revision_data"] = self.historical_data(produced["proof_bodies"][-1])
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["fact_reason"], "historical_body_unavailable")

    def test_conflicting_selected_complete_scopes_cannot_preserve_passing_proof(self):
        produced = self.previous_record(conflicting=True)
        proposal = self.old_proposal(produced)
        proposal["issue_revision_data"] = self.historical_data(*produced["proof_bodies"])
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["fact_reason"], "prior_source_scope_ambiguous")


class IssueRevisionDataTests(IssueRevisionCase):
    def test_malformed_data_and_duplicate_hashes_refuse_without_effect(self):
        self.begin(); proposal = self.proposal()
        entry = self.historical_data("- [ ] **PoD#1** old requirement")["historical_bodies"][0]
        for data in (None, [], {}, {"historical_bodies": None}, {"historical_bodies": [entry, entry]},
                     {"historical_bodies": [entry] * 65}):
            with self.subTest(data_type=type(data).__name__):
                self.refused_revision({**proposal, "issue_revision_data": data})

    def test_body_data_cannot_claim_identity_trust_or_mismatched_bytes(self):
        self.begin(); proposal = self.proposal()
        entry = self.historical_data("- [ ] **PoD#1** old requirement")["historical_bodies"][0]
        for fields in ({"id": "O1"}, {"definition": "a" * 64}, {"verified": True},
                       {"body": "different bytes"}, {"body": "\ud800"}):
            self.refused_revision({**proposal, "issue_revision_data": {"historical_bodies": [{**entry, **fields}]}})
        self.refused_revision({**proposal, "issue_revision_data": self.historical_data("unrelated body")})

    def test_data_budgets_and_checkpoint_only_boundary_refuse_without_effect(self):
        self.begin(); proposal = self.proposal()
        self.refused_revision({**proposal, "issue_revision_data": self.historical_data("x" * (128 * 1024))})
        self.refused_revision({"obligations": self.stored(), "issue_revision_data": self.historical_data("body")},
                              "issue_revision_refused")
        from pod.issue_revision import refuse_alternate
        for data in ({}, self.historical_data("body")):
            with self.assertRaises(PodError) as caught:
                refuse_alternate({"issue_revision_data": data})
            self.assertEqual(caught.exception.detail["reason"], "checkpoint_only")

    def test_body_data_supplies_no_approval(self):
        self.begin(); proposal = self.proposal()
        proposal["issue_revision_data"] = self.historical_data("- [ ] **PoD#1** old requirement")
        self.configure(None)
        self.refused_revision(proposal, "relay_verifier_unconfigured")

    def test_data_digests_bind_interrupted_receipt_to_identical_transition(self):
        self.begin(); proposal = self.proposal()
        proposal["issue_revision_data"] = self.historical_data("- [ ] **PoD#1** old requirement")
        before = self.saved()
        with patch("pod.ledger.publish_context", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.revise(proposal)
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.receipts()), 1)
        altered = deepcopy(proposal); altered.pop("issue_revision_data")
        self.refused_revision(altered, "relay_authorization_reused")
        self.revise(proposal)
        self.assertEqual(len(self.receipts()), 1)
        self.assertTrue(read(self.project, "objective")["checkpoint"]["revision_decisions"][-1]["scope_facts"]["data_digests"])


class IssueRevisionIntakeViewTests(IssueRevisionCase):
    def intake_view(self):
        from pod.internal import run
        return run("issue-intake", {"project": str(self.project), "locator": self.locator})

    def test_long_block_view_is_complete_deterministic_and_read_only(self):
        self.begin()
        text = "**PoD#1** Header\n  Acceptance:\n    - " + "alpha " * 150
        self.issue.body = "- [ ] " + text
        before = self.saved(); self.issue.calls.clear()
        result = self.intake_view()
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(result["issue_revision_criteria"], {"status": "ready", "reason": None, "criteria": [{
            "ref": "PoD#1", "check": "PoD#1 complete source block sha256:" + fingerprint, "block_sha256": fingerprint}]})
        self.assertEqual(result["body"], self.issue.body)
        self.assertEqual(self.issue.calls, [("issue", "acme/example", 7)])
        self.issue.body += "\n    - beta after the check limit"
        changed = self.intake_view()["issue_revision_criteria"]["criteria"][0]
        self.assertNotEqual(changed["block_sha256"], fingerprint)
        self.assertNotEqual(changed["check"], result["issue_revision_criteria"]["criteria"][0]["check"])
        self.assertEqual(self.saved(), before)
        self.assertEqual((self.requests, self.issue.writes), ([], []))

    def test_unavailable_view_is_empty_and_preserves_overall_intake_behavior(self):
        self.begin(); before = self.saved()
        for body in ("No PoD lines", "- [ ] **PoD#1** one\n- [ ] **PoD#01** duplicate",
                     "\n".join(f"- [ ] **PoD#{n}** requirement" for n in range(65))):
            self.issue.body = body
            result = self.intake_view()
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["body"], body)
            self.assertEqual(result["issue_revision_criteria"], {
                "status": "unavailable", "reason": "source_pod_mapping", "criteria": []})
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.writes, [])

    def test_intake_view_uses_same_recognition_and_keeps_quoted_fenced_decoys_out(self):
        self.issue.body = ("- [ ] **PoD#1** first\n\n> - [ ] **PoD#1** quote\n"
                           "```\n- [ ] **PoD#1** code\n```\n- [ ] **PoD#2** second")
        rows = self.intake_view()["issue_revision_criteria"]["criteria"]
        self.assertEqual([(row["ref"], row["check"]) for row in rows], [
            ("PoD#1", "**PoD#1** first"), ("PoD#2", "**PoD#2** second")])

    def test_intake_derived_long_check_is_accepted_by_checkpoint_with_fresh_proof_pending(self):
        self.begin()
        self.issue.body = "- [ ] **PoD#1** changed\n  " + "nested acceptance " * 80
        view = self.intake_view()
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "complete scope changed"})
        rows.append({**self.new_criterion(1), "id": "R1", "check": view["issue_revision_criteria"]["criteria"][0]["check"]})
        self.write(rows, seq=2, objective_source=view["source"], revision_authority={
            "operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
            "instruction": "Reconcile complete scope", "relay": {"reference": "synthetic-decision"}})
        self.assertFalse(self.line()["proven"])
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["criterion_scopes"]["R1"]["block_sha256"],
                         view["issue_revision_criteria"]["criteria"][0]["block_sha256"])


class IssueRevisionRefusalTests(IssueRevisionCase):
    def test_malformed_map_and_source_forms_refuse_with_no_effect(self):
        self.begin(); proposal = self.proposal()
        for rows in (None, {}, [None], [{**proposal["obligations"][1], "source": []}],
                     [{**proposal["obligations"][1], "source": {"ref": []}}]):
            self.refused_revision({**proposal, "obligations": rows})

    def test_unconfigured_denied_unsupported_and_partial_verifier_leave_no_effect(self):
        self.begin(); proposal = self.proposal()
        self.configure(None)
        self.refused_revision(proposal, "relay_verifier_unconfigured")
        self.configure(["/synthetic/verifier"])
        self.verdict_changes = {"verified": False}
        self.refused_revision(proposal, "relay_verifier_denied")
        self.verdict_changes = {"action": "unsupported"}
        self.refused_revision(proposal, "relay_verifier_malformed")
        for partial in ({}, {"source": {}}, {"map": {}}):
            self.verdict_changes = {"issue_revision": partial}
            self.refused_revision(proposal, "relay_verifier_malformed")

    def test_objective_reference_target_and_source_mismatches(self):
        self.begin(); proposal = self.proposal()
        for field, value in (("objective", "other"), ("reference", "other"),
                             ("target", {"repository": "other/repo", "remote": "origin", "base": "target"})):
            self.verdict_changes = {field: value}
            self.refused_revision(proposal)
        self.verdict_changes = {}
        self.refused_revision({**proposal, "objective_source": {**proposal["objective_source"], "repository": "other/repo",
                              "locator": "https://github.com/other/repo/issues/7"}}, "repository_mismatch")
        self.refused_revision({**proposal, "objective_source": {**proposal["objective_source"], "number": 8,
                              "locator": "https://github.com/acme/example/issues/8"}}, "issue_revision_refused")

    def test_each_exact_map_native_and_digest_field_mismatch(self):
        self.begin(); proposal = self.proposal()
        base_output = self.verify_output
        for section, field, value in (("map", "seq", 99), ("map", "revision", 99), ("map", "policy_revision", "other"),
                                      ("coordinator", "handle", "other"), ("coordinator", "run", "other"),
                                      ("coordinator", "incarnation", "other"), ("coordinator", "runtime", "other"),
                                      ("coordinator", "generation", 99), ("source", "previous_digest", "b" * 64),
                                      ("source", "proposed_digest", "c" * 64)):
            def mismatch(command, request):
                verdict = json.loads(base_output(command, request))
                verdict["issue_revision"][section][field] = value
                return json.dumps(verdict).encode()
            with self.subTest(field=section + "." + field), patch("pod.relay._output", side_effect=mismatch):
                self.refused_revision(proposal, "relay_verifier_mismatch")

    def test_caller_cannot_inject_evidence_or_other_decision_kinds(self):
        self.begin(); proposal = self.proposal()
        for fields in ({"relay_verification": {}}, {"issue_revision": {}}, {"question": "Q1"},
                       {"channel": "session"}, {"relay": {"reference": "synthetic", "owner_source": "untrusted"}}):
            altered = deepcopy(proposal)
            altered["revision_authority"].update(fields)
            self.refused_revision(altered)
        for fields in ({"governance_refresh": True}, {"governance": {"base_ref": "elsewhere"}},
                       {"rebind": ["O1"]}, {"reopen": True}, {"close": True}, {"delivery": {"record": "fake"}},
                       {"acceptances": []}, {"dispositions": []}, {"proposals": []}):
            self.refused_revision({**proposal, **fields}, "issue_revision_refused")

    def test_arbitrary_checks_unmapped_duplicate_and_nonmaterial_revision_refuse(self):
        self.begin(); proposal = self.proposal()
        altered = deepcopy(proposal); altered["obligations"][1]["check"] = "publish everything"
        self.refused_revision(altered, "issue_revision_refused")
        altered = deepcopy(proposal); altered["obligations"].pop()
        self.refused_revision(altered, "issue_revision_refused")
        altered = deepcopy(proposal); altered["obligations"].append({**altered["obligations"][1], "id": "duplicate"})
        self.refused_revision(altered, "issue_revision_refused")
        altered = deepcopy(proposal); altered["obligations"][1]["source"]["ref"] = "PoD#22"
        self.refused_revision(altered, "issue_revision_refused")
        self.issue.body += "\n- [ ] **PoD#2** duplicate"
        altered = deepcopy(proposal); altered["objective_source"] = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.refused_revision(altered, "issue_revision_refused")

    def test_unchanged_criteria_and_source_hash_alone_grant_no_authority(self):
        self.begin(); proposal = self.proposal()
        self.issue.body = "- [ ] **PoD#1** old requirement\nDelivery note"
        proposal["objective_source"] = issue_intake(self.project, self.locator, port=self.issue)["source"]
        proposal["criteria"] = ["PoD#1"]; proposal["obligations"] = self.stored()
        self.refused_revision(proposal, "issue_revision_refused")

    def test_replacing_an_identical_check_cannot_manufacture_material_change(self):
        self.begin(); proposal = self.proposal()
        self.issue.body = "- [ ] **PoD#1** old requirement"
        proposal["objective_source"] = issue_intake(self.project, self.locator, port=self.issue)["source"]
        old = self.stored()[0]
        old.pop("executor")
        old.update(state="withdrawn", withdrawal={"by": "issue_revision", "reason": "rename only"})
        proposal["obligations"] = [old, {**self.new_criterion(1), "id": "renamed", "check": "**PoD#1** old requirement"}]
        proposal["criteria"] = ["PoD#1"]
        self.refused_revision(proposal, "issue_revision_refused")

    def test_source_and_target_wire_identity_fields_are_exact(self):
        self.begin(); proposal = self.proposal()
        output = self.verify_output
        for section, field, value in (("target", "remote", "upstream"), ("target", "base", "elsewhere"),
                                      ("source", "repository", "other/repo"), ("source", "number", 8),
                                      ("source", "locator", "https://github.com/acme/example/issues/8")):
            def mismatch(command, request):
                verdict = json.loads(output(command, request))
                target = verdict["issue_revision"]["source"] if section == "source" else verdict[section]
                target[field] = value
                return json.dumps(verdict).encode()
            with self.subTest(field=field), patch("pod.relay._output", side_effect=mismatch):
                self.refused_revision(proposal)

    def test_new_criteria_cannot_inherit_proof_and_user_or_policy_injection_refuses(self):
        self.begin(); proposal = self.proposal()
        for fields in ({"evidence": [self.proof("O1")]}, {"receipts": []}, {"reuse": {"from": self.base}},
                       {"provenance": "user_direct", "source": {"instruction": "do more"}},
                       {"provenance": "project_policy", "source": {"path": "AGENTS.md", "lines": "1"}}):
            altered = deepcopy(proposal); altered["obligations"][1].update(fields)
            self.refused_revision(altered)

    def test_unknown_incarnation_and_changed_current_owner_or_run_fail_closed(self):
        self.begin(); proposal = self.proposal()
        native_read = self.native_read
        def unknown(argv, **kwargs):
            value = native_read(argv, **kwargs)
            if argv[:2] == ["terminal", "show"]:
                value["result"]["terminal"].pop("incarnationId")
            return value
        with patch("pod.orca.read_command", side_effect=unknown):
            self.refused_revision(proposal)
        for fields in ({"coordinator_handle": "other"}, {"id": "other"}):
            saved = deepcopy(self.current); self.current.update(fields)
            self.refused_revision(proposal)
            self.current = saved


class IssueRevisionAtomicityTests(IssueRevisionCase):
    def test_invalid_map_is_validated_before_receipt_consumption(self):
        self.begin(); proposal = self.proposal()
        proposal["obligations"][1]["wait"] = {"class": "sequenced", "referent": "missing"}
        self.refused_revision(proposal, "wait_invalid")
        self.assertEqual(len(self.requests), 1)

    def test_capacity_failure_does_not_consume_or_publish(self):
        self.begin(); proposal = self.proposal()
        with patch("pod.ledger.prepare_context", side_effect=PodError("record_too_large", "synthetic capacity hold")):
            self.refused_revision(proposal, "record_too_large")

    def test_policy_or_command_change_inside_final_verifier_callback_refuses(self):
        self.begin(); proposal = self.proposal()
        def moved(count):
            if count == 2:
                path = self.root / "config/pod/config.yaml"
                config = yaml.safe_load(path.read_text())
                config.setdefault("waste_governor", {})["preflight"] = ["another-local-check"]
                path.write_text(yaml.safe_dump(config))
        self.verifier_callback = moved
        self.refused_revision(proposal, "issue_revision_refused")

    def test_command_change_inside_final_verifier_callback_refuses(self):
        self.begin(); proposal = self.proposal()
        def moved(count):
            if count == 2:
                self.configure(["/synthetic/other-verifier"])
        self.verifier_callback = moved
        self.refused_revision(proposal, "issue_revision_refused")

    def test_stop_or_epoch_refusal_on_consumption_recheck_keeps_every_byte(self):
        self.begin(); proposal = self.proposal()
        def stop(count):
            if count == 2:
                self.verdict_changes = {"verified": False}
        self.verifier_callback = stop
        self.refused_revision(proposal, "relay_verifier_denied")

    def test_native_or_source_change_inside_verifier_callback_is_rechecked(self):
        self.begin(); proposal = self.proposal()
        for change in (lambda: self.current.update(consumer_generation=2),
                       lambda: setattr(self.issue, "body", self.issue.body + "\nchanged")):
            def moved(count):
                if count % 2 == 0:
                    change()
            self.verifier_callback = moved
            self.refused_revision(proposal, "issue_revision_refused")
            self.current["consumer_generation"] = 1
            self.issue.body = "- [ ] **PoD#1** old requirement\n- [ ] **PoD#2** new requirement"

    def test_interrupted_consume_recovers_exact_transition_only_and_replay_refuses(self):
        self.begin(); proposal = self.proposal()
        class Interrupted(BaseException):
            pass
        before = self.saved()
        with patch("pod.ledger.publish_context", side_effect=Interrupted), self.assertRaises(Interrupted):
            self.revise(proposal)
        self.assertEqual(self.saved(), before)
        receipt = self.receipts()
        self.assertEqual(len(receipt), 1)
        self.assertIn("transition", json.loads(next(iter(receipt.values()))))
        altered = deepcopy(proposal); altered["next_safe_action"] = "different accepted transition"
        self.refused_revision(altered, "relay_authorization_reused")
        altered = deepcopy(proposal)
        altered["obligations"].append({"id": "sub", "kind": "subgoal", "provenance": "coordinator", "parent": "O2",
                                       "check": "accountable decomposition", "state": "blocked_external",
                                       "external": {"party": "user", "need": "provide evidence", "unblocks_when": "evidence arrives"}})
        self.refused_revision(altered, "relay_authorization_reused")
        self.revise(proposal)
        self.assertEqual(self.receipts(), receipt)
        repeated = deepcopy(proposal); repeated["seq"] += 1
        self.refused_revision(repeated)

    def test_concurrent_same_transition_accepts_once(self):
        self.begin(); proposal = self.proposal()
        def revise(_):
            try:
                self.revise(proposal)
                return "accepted"
            except PodError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(revise, range(2)))
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["seq"], 2)

    def test_accepted_key_cannot_authorize_another_material_revision(self):
        self.begin(); proposal = self.proposal(); self.revise(proposal)
        self.issue.body += "\n- [ ] **PoD#3** requirement 3"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.refused_revision({**proposal, "seq": 3, "criteria": ["PoD#1", "PoD#2", "PoD#3"],
                               "objective_source": source, "obligations": self.stored() + [self.new_criterion(3)]},
                              "relay_authorization_reused")


class IssueRevisionCompatibilityTests(IssueRevisionCase):
    def test_report_and_admission_maps_explicitly_refuse_the_operation(self):
        from pod.ledger import consume_report, reserve
        self.begin(); proposal = self.proposal()
        before = self.saved()
        for callback in (lambda: consume_report(self.project, "objective", owner="owner", admission_id="unused",
                                                observation=None, accompanying=proposal),
                         lambda: reserve(self.project, "objective", owner="owner", admission_id="unused", requested={},
                            native_reader=None, run_id="run", task_id="task", plan_revision="plan", packet_id="packet",
                            worktree="current", frozen_packet={}, expected_runtime="runtime", placement_binding={}, accompanying=proposal)):
            with self.assertRaises(PodError) as caught:
                callback()
            self.assertEqual(caught.exception.code, "issue_revision_refused")
            self.assertEqual(caught.exception.detail["reason"], "checkpoint_only")
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.receipts(), {})
        # Public entry points refuse before any admission/report preparation can mutate history.
        for mapping in (proposal, {"issue_revision_data": self.historical_data("body DATA")}):
            for operation, values in (("report", {"admission_id": "unused", "packet": {}, "report": {}, "map": mapping}),
                                      ("admission", {"run": "run", "task": "unused", "plan_revision": "plan", "packet": {}, "map": mapping})):
                with self.assertRaises(PodError) as caught:
                    self.op(operation, **values)
                self.assertEqual(caught.exception.code, "issue_revision_refused")
                self.assertEqual(caught.exception.detail["reason"], "checkpoint_only")
        self.assertEqual(self.saved(), before)

    def test_plain_relay_revision_refuses_but_owner_typed_revision_stays_supported(self):
        self.begin(); proposal = self.proposal()
        proposal["revision_authority"].pop("operation")
        proposal["revision_authority"]["relay"]["owner_source"] = "synthetic-owner"
        self.refused_revision(proposal, "relayed_decision_refused")
        rows = self.stored(); rows[0]["check"] = "owner changes the check"
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "change this check"})
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"][0]["check"], "owner changes the check")
        self.assertEqual(self.requests, [])

    def test_persisted_evidence_corruption_fails_on_read_and_public_projections_are_read_only(self):
        self.begin(); self.revise(self.proposal())
        state = read(self.project, "objective")
        before = self.saved(); count = len(self.requests)
        self.op("map"); self.ticks()
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.requests), count)
        path = _path(self.project, "objective")
        for change in (lambda value: value.pop("decision_channels"),
                       lambda value: value["checkpoint"]["revision_decisions"][-1]["relay_verification"].update(verified=False),
                       lambda value: value["checkpoint"]["revision_decisions"][-1]["relay_verification"]["issue_revision"]["source"].update(proposed_digest="e" * 64),
                       lambda value: value["checkpoint"]["revision_decisions"][-1]["scope_facts"]["current"]["O1"].update(block_sha256="e" * 64),
                       lambda value: value["checkpoint"]["revision_decisions"][-1]["scope_facts"]["previous"][0].update(
                           status="unknown", reason="prior_source_association_unavailable", scope=None),
                       lambda value: value["checkpoint"].update(issue_revision_data={"historical_bodies": []})):
            corrupted = deepcopy(state); change(corrupted)
            path.write_text(json.dumps(corrupted))
            with self.assertRaises(PodError):
                read(self.project, "objective")
        path.write_text(json.dumps(state))

    def test_later_revision_and_immutable_history_retain_all_prior_verifier_and_tick_facts(self):
        from pod.history import (publish_context)
        from pod.status import status
        self.begin(); self.revise(self.proposal())
        before = read(self.project, "objective")
        self.issue.body += "\n- [ ] **PoD#3** requirement 3"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        rows = self.op("map")["obligations"] + [self.new_criterion(3)]
        self.verdict_changes = {"single_use": "next-key"}
        authority = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                     "instruction": "Reconcile another approved issue revision", "relay": {"reference": "next-decision"}}
        self.write(rows, seq=3, criteria=["PoD#1", "PoD#2", "PoD#3"], objective_source=source,
                   revision_authority=authority)
        state = read(self.project, "objective")
        path = _path(self.project, "objective")
        record, chunks = compact(state, force=True)
        publish_preceding(path, record, chunks)
        self.assertIn("$pod_history", path.read_text())
        self.assertEqual(read(self.project, "objective"), state)
        self.assertEqual(state["checkpoint"]["revision_decisions"][0], before["checkpoint"]["revision_decisions"][0])
        count, saved = len(self.requests), self.saved()
        public = status(self.project, None, objective="objective")
        decisions = [row["decision"] for row in public["owner_decisions"] if row["kind"] == "revision"]
        self.assertEqual(decisions, state["checkpoint"]["revision_decisions"])
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.saved(), saved)

    def test_real_bounded_subprocess_transport_is_synthetic_only(self):
        from pod.relay import _output
        self.begin()
        script = self.root / "verifier.py"
        script.write_text('import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({**r,"verified":True,"owner_source":"synthetic","decided_at":"2026-10-01T00:00:00Z","single_use":"process-key"}))\n')
        self.configure([sys.executable, str(script)])
        with patch("pod.relay._output", side_effect=_output):
            self.revise(self.proposal())
        self.assertEqual(len(self.receipts()), 1)

    def test_genuine_previous_reader_refuses_new_context_without_rewrite(self):
        self.begin(); self.revise(self.proposal())
        repository = Path(__file__).resolve().parents[1]
        previous = self.root / "previous"; previous.mkdir()
        archive = previous / "source.tar"
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive),
                        "ca5913f639b23d8af8869174fcb8ba72187977cd", "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(previous, filter="data")
        path = _path(self.project, "objective"); before = path.read_bytes()
        result = subprocess.run([sys.executable, "-c", '''import sys
from pathlib import Path
from pod.ledger import _read
from pod.errors import PodError
try:
    _read(Path(sys.argv[1]))
except PodError as exc:
    print(exc.code)
else:
    raise SystemExit("incorrectly accepted")
''', str(path)], env={**os.environ, "PYTHONPATH": str(previous / "skills")},
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The preceding helper rejects the new compact physical source facts
        # before it reaches the separately retained decision_channels=4 marker.
        self.assertEqual(result.stdout.strip(), "state_unsupported")
        self.assertEqual(read(self.project, "objective")["decision_channels"], 4)
        self.assertEqual(path.read_bytes(), before)

    def test_canonical_non_ascii_digest_vector_includes_amendments(self):
        # Generic encoding control: note is not an accepted issue-source field.
        value = {"schema": "pod-issue-source/v1", "repository": "acme/example", "number": 7,
                 "locator": "https://github.com/acme/example/issues/7", "body_sha256": "a" * 64,
                 "amendments": [{"locator": "https://github.com/acme/example/issues/7#issuecomment-9", "sha256": "b" * 64}],
                 "note": "café 雪"}
        self.assertEqual(digest(value), "8f936e7f2c72ce53dee33a88004a0463ffda0146d56f4dbe30355e015dac34b2")
        self.assertNotEqual(digest(value), digest({**value, "amendments": []}))

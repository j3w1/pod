"""Synthetic issue-revision boundary controls; no authentic host producer is exercised."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
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
        for operation, values in (("report", {"admission_id": "unused", "packet": {}, "report": {}, "map": proposal}),
                                  ("admission", {"run": "run", "task": "unused", "plan_revision": "plan", "packet": {}, "map": proposal})):
            with self.assertRaises(PodError) as caught:
                self.op(operation, **values)
            self.assertEqual(caught.exception.code, "issue_revision_refused")
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
                       lambda value: value["checkpoint"]["revision_decisions"][-1]["relay_verification"]["issue_revision"]["source"].update(proposed_digest="e" * 64)):
            corrupted = deepcopy(state); change(corrupted)
            path.write_text(json.dumps(corrupted))
            with self.assertRaises(PodError):
                read(self.project, "objective")
        path.write_text(json.dumps(state))

    def test_later_revision_and_immutable_history_retain_all_prior_verifier_and_tick_facts(self):
        from pod.history import compact, publish_context
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
        publish_context(path, record, chunks)
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
        self.assertEqual(result.stdout.strip(), "state_unsupported")
        self.assertEqual(path.read_bytes(), before)

    def test_canonical_non_ascii_digest_vector_includes_amendments(self):
        # Generic encoding control: note is not an accepted issue-source field.
        value = {"schema": "pod-issue-source/v1", "repository": "acme/example", "number": 7,
                 "locator": "https://github.com/acme/example/issues/7", "body_sha256": "a" * 64,
                 "amendments": [{"locator": "https://github.com/acme/example/issues/7#issuecomment-9", "sha256": "b" * 64}],
                 "note": "café 雪"}
        self.assertEqual(digest(value), "8f936e7f2c72ce53dee33a88004a0463ffda0146d56f4dbe30355e015dac34b2")
        self.assertNotEqual(digest(value), digest({**value, "amendments": []}))

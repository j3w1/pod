"""Closure-currency and bound-SEAL regressions through disposable production ports."""

from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import issue_intake, pod_lines
from pod.issue_revision import _source_blocks, criteria_view
from pod.ledger import objective_root, read
from pod.pod_mapping import scope_hash
from tests.issue_port_support import patch_issue_port
from tests.kernel_support import git
from tests.pod_tick_support import TickCase
from tests.test_completed_closure import SourcePort
from tests.test_issue_revision import IssueRevisionCase


class LinkedCompletionTests(TickCase):
    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        replacement = patch_issue_port(self.issue)
        replacement.start()
        self.addCleanup(replacement.stop)

    def linked(self, *, report=False, policy_in_result=False):
        self.bind()
        path = self.project / ("CLAUDE.md" if policy_in_result else "README.md")
        path.write_text("delivered change\n")
        git(self.project, "add", path.name)
        git(self.project, "commit", "-qm", "delivered candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write()
        self.prove()
        self.tick()
        assignment = None
        if report:
            self.write([*self.stored(), self.sub("S", "active", executor="coordinator")])
            frozen = self.packet(["S"], role="investigate", resolves="verify delivery", stop_condition="report consumed")
            admission = self.start("closing-report", frozen)["admission"]
            self.settle(admission)
            assignment = admission, frozen
        self.remote.issues[("acme/example", 7)] = {"body": self.issue.body, "state": "OPEN"}
        self.remote.linked[("acme/example", 7)] = [{"number": 9,
            "url": "https://github.com/acme/example/pull/9", "base_repository": "acme/example",
            "base_branch": "target", "state": "MERGED", "merged_at": "2026-10-01T00:00:00Z",
            "merge_commit": self.candidate}]
        git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
        self.baseline = deepcopy(read(self.project, "objective")["checkpoint"])
        return assignment

    def advance_policy(self):
        (self.project / "CLAUDE.md").write_text("later target governance\n")
        git(self.project, "add", "CLAUDE.md")
        git(self.project, "commit", "-qm", "later governance")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))

    def assert_kept(self, result):
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertTrue(checkpoint["closure"])
        for key in ("governance", "governance_sources", "revision_decisions"):
            self.assertEqual(checkpoint.get(key), self.baseline.get(key))
        self.assertIsNone(checkpoint.get("delivery"))
        kept = result["governance_adoption"]
        self.assertFalse(kept["adopted"])
        self.assertEqual(kept["delivery_proof"][0]["proof"], "github_link")
        self.assertFalse((objective_root(self.project, "objective") / "governor.json").exists())

    def test_linked_delivery_closes_after_governance_change_without_r93_or_owner_decision(self):
        self.linked()
        self.advance_policy()
        self.assert_kept(self.write(close=True))

    def test_exact_linked_result_with_changed_governance_keeps_baseline(self):
        self.linked(policy_in_result=True)
        self.assert_kept(self.write(close=True))

    def test_report_closure_uses_linked_proof_and_consumes_the_bound_report(self):
        admission, frozen = self.linked(report=True)
        self.advance_policy()
        rows = self.stored()
        rows[-1].pop("executor")
        rows[-1].update(state="satisfied", evidence=[self.proof("S")])
        result = self.report(admission, frozen, map={"obligations": rows, "close": True})
        self.assert_kept(result)
        self.assertIsNotNone(read(self.project, "objective")["admissions"][admission["admission_id"]]["report"])

    def test_linked_proof_does_not_exempt_an_ordinary_write(self):
        self.linked()
        self.advance_policy()
        before, calls = self.saved(), deepcopy(self.remote.calls)
        self.failure("governance_changed", self.write)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.remote.calls, calls)

    def test_missing_link_names_supported_lead_work_without_an_impossible_r93_step(self):
        self.linked()
        self.advance_policy()
        self.remote.linked.clear()
        before = self.saved()
        error = self.failure("governance_changed", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.assertIn("the project lead", error.detail["next_action"])
        self.assertIn("without governance_refresh", error.detail["next_action"])
        self.assertNotIn("records verified delivery", error.detail["next_action"])
        self.assertNotIn("owner", str(error))

    def test_unknown_ancestry_and_retargeting_never_exempt_closure(self):
        from pod.governance import closure_keeps_baseline
        self.linked()
        self.advance_policy()
        with patch("pod.governance.git_is_ancestor", return_value=None):
            self.assertIsNone(closure_keeps_baseline(self.project, self.baseline))
        target = deepcopy(self.baseline)
        target["governance"]["selection"] = "default"
        with patch("pod.governance._symbolic_ref", return_value="refs/remotes/origin/other"):
            self.assertIsNone(closure_keeps_baseline(self.project, target))

    def test_target_movement_during_linked_read_grants_no_exemption(self):
        from pod.governance import closure_keeps_baseline
        self.linked()
        self.advance_policy()
        original = self.remote.closing_prs

        def moved(**arguments):
            result = original(**arguments)
            git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
            return result

        before = self.saved()
        with patch.object(self.remote, "closing_prs", side_effect=moved):
            self.assertIsNone(closure_keeps_baseline(self.project, self.baseline))
        self.assertEqual(self.saved(), before)

    def test_unnecessary_refresh_names_closing_on_the_retained_baseline(self):
        self.linked()
        self.advance_policy()
        before = self.saved()
        error = self.failure("governance_unavailable", self.write, close=True, governance_refresh=True,
            governance={"base_ref": "refs/remotes/origin/target",
                        "base": git(self.project, "rev-parse", "refs/remotes/origin/target")})
        self.assertIn("without governance_refresh", error.detail["next_action"])
        self.assertNotIn("records verified delivery", error.detail["next_action"])
        self.assertEqual(self.saved(), before)

    def test_unmerged_or_wrong_target_link_never_grants_completion_currency(self):
        self.linked()
        self.advance_policy()
        row = self.remote.linked[("acme/example", 7)][0]
        before = self.saved()
        for changed in ({"state": "OPEN"}, {"base_branch": "other"}, {"merge_commit": "f" * 40}):
            with self.subTest(changed=changed), patch.dict(row, changed):
                self.failure("governance_changed", self.write, close=True)
                self.assertEqual(self.saved(), before)


class SealParserTests(unittest.TestCase):
    def test_revision_and_scope_parsers_share_bound_seal_tick_recognition(self):
        body = ("- [ ] **AC1** result\r\n  nested check\r\n- [x] **PoD#2** result\r\n"
                "```\r\n- [ ] **AC3** fenced\r\n```\r\n - [ ] **AC4** indented\r\n"
                "- [ ] **AC5suffix** not a criterion")
        self.assertEqual(set(pod_lines(body, seal=True)), {1, 2})
        self.assertEqual(list(_source_blocks(body, seal=True)), ["AC1", "PoD#2"])
        self.assertEqual(list(_source_blocks(body)), ["PoD#2"])
        self.assertEqual([row["ref"] for row in criteria_view(body, seal=True)["criteria"]], ["AC1", "PoD#2"])
        ticked = body.replace("[ ] **AC1", "[x] **AC1")
        self.assertEqual(scope_hash(body, seal=True), scope_hash(ticked, seal=True))
        self.assertNotEqual(scope_hash(body), scope_hash(ticked))

    def test_ac_and_pod_numeric_collision_remains_ambiguous_only_for_bound_seal(self):
        body = "- [ ] **AC1** result\n- [ ] **PoD#1** result"
        self.assertEqual(criteria_view(body, seal=True)["status"], "unavailable")
        self.assertEqual([row["ref"] for row in criteria_view(body)["criteria"]], ["PoD#1"])


class SealMappingCase(TickCase):
    def bind_legacy_seal(self):
        self.original = "- [ ] **AC1** accepted result\n- [ ] **PoD 2: Result two**"
        rows = [self.criterion(source={"ref": "AC1"}), self.criterion(
            "waiting", id="O2", source={"ref": "PoD 2: Result two"}, wait={"class": "sequenced", "referent": "O1"})]
        self.write(rows, criteria=["AC1", "PoD 2: Result two"], governance={"base_ref": "target"})
        self.issue.title, self.issue.body = "SEAL: acceptance", self.original
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.write(objective_source=source, criteria=["AC1", "PoD 2: Result two"])

    def core(self, **fields):
        fields.setdefault("criteria", ["AC1", "PoD 2: Result two"])
        return super().core(**fields)

    def repair(self):
        self.issue.body = self.issue.body.replace("**PoD 2:", "**PoD#2:")
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        return self.write(objective_source=source, mapping_repair={
            "original_body": self.original, "targets": {"O2": "PoD#2"}})


class SealMappingParserTests(SealMappingCase):
    def test_label_only_repair_scope_uses_bound_seal_recognition(self):
        from pod.pod_mapping import prepare
        self.bind_legacy_seal()
        self.issue.body = self.issue.body.replace("**PoD 2:", "**PoD#2:")
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        association = prepare(read(self.project, "objective"), source, {
            "original_body": self.original, "targets": {"O2": "PoD#2"}}, self.issue.body)["O2"]
        self.assertEqual(association["scope_sha256"], scope_hash(self.issue.body, seal=True))
        self.assertEqual(association["ref"], "PoD#2")
        self.assertFalse(association["invalidated"])

    def test_mapping_observation_and_tick_facts_agree_for_a_bound_seal(self):
        from pod.pod_mapping import current, observe, prepare
        from pod.pod_ticks import _facts
        self.bind_legacy_seal()
        self.issue.body = self.issue.body.replace("**PoD 2:", "**PoD#2:")
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        state = deepcopy(read(self.project, "objective"))
        association = prepare(state, source, {
            "original_body": self.original, "targets": {"O2": "PoD#2"}}, self.issue.body)["O2"]
        # Adapter control, kept in memory: the outer ledger refresh guard is
        # exercised separately by SealMappingTests at the checkpoint boundary.
        state["checkpoint"]["objective_source"] = source
        row = state["checkpoint"]["obligations"][1]
        row["pod_mapping"] = association
        facts = {"authors": {}}
        self.assertFalse(observe(state, facts, source, self.issue.body))
        _facts(facts, source, self.issue.body)
        self.assertTrue(current(row, {"pod_mapping_source": source,
                                     "pod_mapping_facts": facts["facts"], "pod_mapping_read": facts["read"]}))


class SealMappingTests(SealMappingCase):
    def test_seal_repair_scope_masks_ac_ticks_and_observation_keeps_derived_association_current(self):
        self.bind_legacy_seal()
        self.repair()
        association = deepcopy(self.stored()[1]["pod_mapping"])
        self.assertEqual(association["scope_sha256"], scope_hash(self.issue.body, seal=True))
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)
        self.tick(1)
        current = self.stored()[1]["pod_mapping"]
        self.assertFalse(current["invalidated"])
        self.assertEqual(current["scope_sha256"], association["scope_sha256"])
        self.assertEqual([row["id"] for row in self.line(2)["criteria"]], ["O2"])
        self.assertEqual(self.line(1)["authorship"], "own")


class SealRevisionCase(IssueRevisionCase):
    def begin_seal(self):
        self.issue.title, self.issue.body = "SEAL: acceptance", "- [ ] **AC1** original requirement"
        self.write([self.criterion(source={"ref": "AC1"}, check="**AC1** original requirement")],
                   criteria=["AC1"], governance={"base_ref": "target"})
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.write(objective_source=source, criteria=["AC1"])

    def core(self, **fields):
        fields.setdefault("criteria", ["AC1"])
        return super().core(**fields)

    def proposal(self):
        self.issue.body += "\n- [ ] **AC2** new requirement"
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        rows = self.stored()
        rows.append(self.criterion("waiting", id="O2", source={"ref": "AC2"}, check="**AC2** new requirement",
                                   wait={"class": "sequenced", "referent": "O1"}))
        return {"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                "obligations": rows, "objective_source": source, "criteria": ["AC1", "AC2"],
                "revision_authority": {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
                    "instruction": "Reconcile approved SEAL requirements", "relay": {"reference": "synthetic-decision"}}}


class SealRevisionParserTests(SealRevisionCase):
    def test_live_revision_parser_uses_the_bound_seal_role(self):
        from pod.issue_revision import _lines
        self.begin_seal()
        proposal = self.proposal()
        self.assertEqual(list(_lines(self.project, proposal["objective_source"])), ["AC1", "AC2"])

    def test_historical_ac_scope_recovers_from_genuine_proof_sequence_only_for_bound_seal(self):
        from pod.issue_revision import _prior_scopes
        self.begin_seal()
        self.prove()
        state = read(self.project, "objective")
        body = self.issue.body
        source = state["checkpoint"]["objective_source"]
        known, retained = _prior_scopes(state, {source["body_sha256"]: body}, source)
        self.assertEqual(known["O1"]["status"], "known")
        self.assertEqual(known["O1"]["scope"]["block_sha256"], hashlib.sha256(
            b"**AC1** original requirement").hexdigest())
        self.assertIn("O1", retained)
        plain = deepcopy(state)
        plain_source = plain["checkpoint"]["objective_source"]
        plain_source.pop("role")
        unknown, _ = _prior_scopes(plain, {plain_source["body_sha256"]: body}, plain_source)
        self.assertEqual(unknown["O1"]["status"], "unknown")

    def test_revision_capability_and_stored_validation_keep_exact_ac_references(self):
        from pod.issue_revision import Revision, validate_decision
        from pod.obligations import MAP_STORED
        self.begin_seal()
        self.prove()
        original = self.issue.body
        proposal = self.proposal()
        state = read(self.project, "objective")
        core = {key: deepcopy(value) for key, value in state["checkpoint"].items() if key not in MAP_STORED}
        core.update(criteria=proposal["criteria"], objective_source=proposal["objective_source"])
        proposed = {key: value for key, value in proposal.items() if key not in ("criteria", "objective_source")}
        capability = Revision(self.project, "objective", "owner", state, core, proposed,
                              data=self.historical_data(original))
        decision = {"kind": "issue_revision", "seq": proposal["seq"], **capability.authority}
        validate_decision(decision, {"objective": "objective", "seq": proposal["seq"]})
        self.assertEqual([row["ref"] for row in capability.criteria], ["AC1", "AC2"])
        self.assertEqual(capability.authority["scope_facts"]["previous"][0]["status"], "known")


class SealRevisionTests(SealRevisionCase):
    def test_seal_issue_revision_preserves_unchanged_ac_and_stores_valid_ac_mapping(self):
        self.begin_seal()
        self.prove()
        self.tick(1)
        before = deepcopy(self.stored()[0])
        result = self.revise(self.proposal())
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual(checkpoint["criteria"], ["AC1", "AC2"])
        self.assertEqual(checkpoint["obligations"][0], before)
        self.assertEqual([row["ref"] for row in checkpoint["revision_decisions"][-1]["criteria"]], ["AC1", "AC2"])
        self.assertEqual(result["pod_tick_intake"]["unmapped"], [])
        self.assertTrue(self.line(1)["proven"])


if __name__ == "__main__":
    unittest.main()

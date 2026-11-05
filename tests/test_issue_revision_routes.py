"""Issue-source change refusals name the route that applies; outcomes stay unchanged (pod#244)."""

from copy import deepcopy

from pod.github import issue_intake
from pod.ledger import read
from tests.test_issue_revision import IssueRevisionCase

AUTHORITY = {"operation": "issue_revision", "provenance": "user_direct", "channel": "relayed",
             "instruction": "Reconcile the approved issue criteria", "relay": {"reference": "synthetic-decision"}}
GENERIC = ("use a checkpoint with exact source-derived criteria and a host-verified issue_revision reference; "
           "otherwise obtain a coordinator-session revision")


class IssueRevisionRouteTests(IssueRevisionCase):
    def revise(self, proposal):
        fields = deepcopy(proposal)
        return self.write(fields.pop("obligations", None), **fields)

    def refused(self, proposal, reason):
        """Refusal code and reason are the outcome; no record, receipt or remote write changes."""
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], reason)
        self.assertEqual(self.requests, [])
        return error.detail

    def amended(self, body):
        self.issue.body = body
        return issue_intake(self.project, self.locator, port=self.issue)["source"]

    def subset(self):
        """An objective owning only PoD#2 of a two-PoD issue, then an appended amendment outside both blocks."""
        source = self.amended("- [ ] **PoD#1** other objective's requirement\n- [ ] **PoD#2** owned requirement")
        self.write([self.criterion(id="O2", source={"ref": "PoD#2"}, check="**PoD#2** owned requirement")],
                   criteria=["PoD#2"], objective_source=source, governance={"base_ref": "target"})
        return self.amended(self.issue.body + "\n\nAmendment: split the delivery into two pull requests.")

    def test_omitted_core_update_names_differing_keys_without_values(self):
        self.begin()
        self.write(blocker="waiting for the hosted run", remaining_gates=["hosted CI"])
        proposal = self.proposal()
        row = proposal["obligations"].pop()
        proposal["update"] = {row["id"]: row}
        proposal.pop("obligations")
        detail = self.refused(proposal, "unrelated_checkpoint_change")
        self.assertEqual(detail["differing"], ["blocker", "remaining_gates"])
        self.assertEqual(detail["omitted"], ["blocker", "remaining_gates"])
        self.assertIn("omitted core field", detail["note"])
        self.assertIn("only objective_source, criteria, plan_revision and next_safe_action", detail["next_action"])
        self.assertNotIn("waiting for the hosted run", repr(detail))
        # A changed (not omitted) field is named as differing only.
        detail = self.refused({**proposal, "blocker": "other"}, "unrelated_checkpoint_change")
        self.assertEqual((detail["differing"], detail["omitted"]), (["blocker", "remaining_gates"], ["remaining_gates"]))
        # Restating the stored core, with the update form for rows, is accepted.
        stored = read(self.project, "objective")["checkpoint"]
        self.revise({**proposal, "blocker": stored["blocker"], "remaining_gates": stored["remaining_gates"],
                     "next_safe_action": "start PoD#2", "plan_revision": "plan-2"})
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual((checkpoint["criteria"], checkpoint["next_safe_action"]), (["PoD#1", "PoD#2"], "start PoD#2"))

    def test_subset_objective_refusal_names_the_ordinary_rebind_which_is_accepted(self):
        source = self.subset()
        seq = read(self.project, "objective")["checkpoint"]["seq"] + 1
        for criteria in (["PoD#2"], ["PoD#2", "PoD#1"]):
            with self.subTest(criteria=criteria):
                detail = self.refused({"seq": seq, "update": {}, "objective_source": source, "criteria": criteria,
                                       "revision_authority": AUTHORITY}, "criteria_not_exact_source")
                self.assertIn("owns only some of the issue's PoDs", detail["next_action"])
                self.assertIn("ordinary checkpoint", detail["next_action"])
                self.assertIn("project lead", detail["next_action"])
        # The named route: fresh source, unchanged criteria, no revision_authority.
        self.write(criteria=["PoD#2"], objective_source=source)
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual((checkpoint["criteria"], checkpoint["objective_source"]), (["PoD#2"], source))

    def test_full_objective_keeps_its_existing_next_action(self):
        self.begin(); proposal = self.proposal()
        proposal["criteria"] = ["PoD#2", "PoD#1"]
        self.assertEqual(self.refused(proposal, "criteria_not_exact_source")["next_action"], GENERIC)

    def test_unchanged_criteria_revision_names_the_ordinary_rebind(self):
        self.begin()
        source = self.amended("- [ ] **PoD#1** old requirement\n\nAmendment: delivery note")
        seq = read(self.project, "objective")["checkpoint"]["seq"] + 1
        detail = self.refused({"seq": seq, "update": {}, "objective_source": source, "criteria": ["PoD#1"],
                               "revision_authority": AUTHORITY}, "material_complete_reconciliation_required")
        self.assertIn("no criterion changed", detail["next_action"])
        self.assertIn("ordinary checkpoint", detail["next_action"])
        self.write(objective_source=source)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["objective_source"], source)

    def test_incomplete_mapping_keeps_its_existing_next_action(self):
        self.begin(); proposal = self.proposal()
        proposal["obligations"].pop()
        self.assertEqual(self.refused(proposal, "material_complete_reconciliation_required")["next_action"], GENERIC)

    def test_route_wording_changes_only_detail_and_next_action(self):
        """The same inputs keep their code and reason; only bounded names and next_action are added."""
        self.begin(); self.write(blocker="held")
        proposal = self.proposal()
        cases = (({**proposal, "blocker": "other"}, "unrelated_checkpoint_change", {"differing", "omitted", "note"}),
                 ({**proposal, "blocker": "held", "criteria": ["PoD#2", "PoD#1"]}, "criteria_not_exact_source", set()))
        for value, reason, added in cases:
            with self.subTest(reason=reason):
                detail = self.refused(deepcopy(value), reason)
                self.assertEqual(set(detail), {"reason", "next_action", *added})
                for key in added - {"note"}:
                    self.assertTrue(set(detail[key]) <= set(read(self.project, "objective")["checkpoint"]))

    def test_unrelated_core_predicate_matches_the_previous_gate(self):
        from pod.issue_revision import unrelated_core
        from pod.obligations import MAP_STORED
        exempt = MAP_STORED | {"criteria", "objective_source", "plan_revision", "next_safe_action", "objective",
                               "pod_version", "bundle_digest", "continuity"}
        prior = {"seq": 1, "criteria": ["PoD#1"], "blocker": "held", "questions": [], "pod_version": "x",
                 "obligations": [], "next_safe_action": "a"}
        for core in ({}, dict(prior), {**prior, "blocker": None}, {**prior, "criteria": ["PoD#2"], "seq": 9},
                     {**prior, "questions": [{"id": "Q"}]}, {"blocker": "held", "questions": [], "display": {}},
                     {**prior, "next_safe_action": "b", "plan_revision": "p", "objective_source": {}}):
            with self.subTest(core=sorted(core)):
                previous = any(core.get(key) != prior.get(key) for key in set(core) | set(prior) if key not in exempt)
                self.assertEqual(bool(unrelated_core(core, prior, MAP_STORED)), previous)

    def test_checkpoint_contract_lists_the_issue_revision_core(self):
        from pod.internal import run
        from pod.issue_revision import REVISABLE
        core = run("contract", {"record": "checkpoint"})["records"]["checkpoint"]["issue_revision_core"]
        self.assertEqual(set(core["may_change"]), REVISABLE)
        self.assertTrue({"assignments", "blocker", "candidate", "display", "native_refs", "questions",
                         "remaining_gates", "route_decisions", "verification_gaps", "worktree"} <= set(core["unchanged"]))
        self.assertFalse(set(core["unchanged"]) & REVISABLE)

    def test_refusal_helper_keeps_code_and_reason_and_routes_only_next_action(self):
        from pod.issue_revision import NEXT_ACTIONS, refuse
        self.assertIn("own ordinary checkpoint", NEXT_ACTIONS["native_continuity_requires_separate_checkpoint"])
        for reason in (*NEXT_ACTIONS, "policy_changed", "source_not_current"):
            with self.subTest(reason=reason):
                error = refuse(reason)
                self.assertEqual((error.code, error.detail["reason"], set(error.detail)),
                                 ("issue_revision_refused", reason, {"reason", "next_action"}))
                self.assertEqual(error.detail["next_action"], NEXT_ACTIONS.get(reason, GENERIC))

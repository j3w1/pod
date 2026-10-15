"""R119: a checkpoint adopts an unchanged target without owner authority."""

from copy import deepcopy
from unittest.mock import patch

from pod import ledger
from pod.governance import require_governance_current
from pod.ledger import read
from tests.kernel_support import KernelCase, USER, git, governance_commit, governance_policy, assurance_review
from tests import test_delivery as delivery


class GovernanceAdoptionTests(KernelCase):
    commit = governance_commit
    policy = governance_policy
    review = assurance_review

    def advance(self, path="README.md", text="sibling work\n"):
        target = self.commit(path, text, "target advance")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        return target

    def test_sibling_target_advance_records_derived_adoption(self):
        self.intake()
        git(self.project, "checkout", "-qb", "sibling-one", self.base)
        self.commit("README.md", "first sibling work\n", "first sibling")
        git(self.project, "checkout", "-qb", "sibling-two", self.base)
        self.commit("sibling.txt", "second sibling work\n", "second sibling")
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "first sibling merge", "sibling-one")
        git(self.project, "merge", "--no-ff", "-qm", "second sibling merge", "sibling-two")
        target = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        state = self.write(self.stored())["checkpoint"]
        self.assertEqual(state["governance"]["base"], target)
        decision = state["governance_history"]["decisions"][-1]
        self.assertEqual(decision["provenance"], "derived")
        self.assertEqual((decision["previous_base"], decision["base"]), (self.base, target))
        self.assertEqual(decision["sources"], [
            {"path": row["path"], "revision": row["revision"]}
            for row in state["governance_sources"]])
        self.assertEqual(read(self.project, "objective")["checkpoint"], state)

    def test_closing_write_preserves_unchanged_candidate_proof_and_citations(self):
        initial = self.intake(self.policy())["checkpoint"]
        rows = self.stored()
        rows[1].pop("wait")
        rows[1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scoped waiver"})
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        bound = self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Waive this review"})["checkpoint"]
        receipts = deepcopy(bound["obligations"][0]["receipts"])
        target = self.advance()
        closed = self.write(self.stored(), close=True)["checkpoint"]
        self.assertEqual(closed["governance"]["base"], target)
        self.assertEqual(closed["governance"]["proof_base"], initial["governance"]["base"])
        self.assertEqual(closed["obligations"][0]["receipts"], receipts)
        self.assertEqual(closed["obligations"][1]["source"]["base"], target)
        self.assertEqual(closed["closure"]["report"]["derived_governance_decisions"]["base"], target)

    def test_candidate_change_cannot_rebind_proof_during_adoption(self):
        self.intake()
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")])]
        self.write(rows)
        self.candidate = self.advance()
        before = read(self.project, "objective")
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored(), close=True)
        self.assertEqual(read(self.project, "objective"), before)

    def test_adoption_preserves_bound_review_receipts_and_label(self):
        self.intake(self.policy("A"))
        admission = self.review()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        state = self.write(rows)["checkpoint"]
        receipts = deepcopy(state["obligations"][1]["receipts"])
        original_binding = deepcopy(read(self.project, "objective")["admissions"][admission["admission_id"]]["binding"])
        self.advance()
        closed = self.write(self.stored(), close=True)
        self.assertEqual(closed["checkpoint"]["obligations"][1]["receipts"], receipts)
        self.assertEqual(read(self.project, "objective")["admissions"][admission["admission_id"]]["binding"], original_binding)
        self.assertEqual(closed["report"]["label"]["label"], "QUALIFIED")

    def test_effect_boundary_names_plain_checkpoint_before_admission(self):
        self.intake()
        self.advance()
        error = self.refused("governance_changed", "governance_changed", require_governance_current,
                             self.project, read(self.project, "objective")["checkpoint"])
        self.assertIn("plain checkpoint", error.detail["next_action"])
        self.assertIn("without an owner question", error.detail["next_action"])
        self.write(self.stored())
        require_governance_current(self.project, read(self.project, "objective")["checkpoint"])

    def test_changed_source_refuses_without_record_write(self):
        self.intake()
        self.advance("AGENTS.md", "changed policy\n")
        before = read(self.project, "objective")
        error = self.refused("governance_changed", "governance_changed", self.write, self.stored())
        self.assertIn("governance_refresh", error.detail["next_action"])
        self.assertEqual(read(self.project, "objective"), before)

    def test_previously_absent_source_appearing_refuses(self):
        self.intake()
        self.advance("CLAUDE.md", "new policy\n")
        self.refused("governance_changed", "governance_changed", self.write, self.stored())

    def test_previously_present_source_disappearing_refuses(self):
        self.intake()
        git(self.project, "rm", "AGENTS.md")
        git(self.project, "commit", "-qm", "remove policy")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        self.refused("governance_changed", "governance_changed", self.write, self.stored())

    def test_changed_default_branch_refuses(self):
        self.intake()
        self.advance()
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.refused("governance_changed", "governance_changed", self.write, self.stored())

    def test_backward_target_move_refuses_even_with_identical_sources(self):
        target = self.advance()
        self.write([self.criterion()], governance={"base_ref": "refs/remotes/origin/target"})
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.refused("governance_changed", "governance_changed", self.write, self.stored())
        self.assertNotEqual(target, self.base)

    def test_sideways_target_move_refuses_even_with_identical_sources(self):
        self.advance()
        self.write([self.criterion()], governance={"base_ref": "refs/remotes/origin/target"})
        git(self.project, "checkout", "--detach", self.base)
        self.advance("README.md", "rewritten target\n")
        self.refused("governance_changed", "governance_changed", self.write, self.stored())

    def test_unreadable_target_source_refuses_without_record_write(self):
        self.intake()
        self.advance()
        before = read(self.project, "objective")
        original = ledger._git
        def unreadable(project, argv, **kwargs):
            return None if argv[:2] == ["cat-file", "blob"] else original(project, argv, **kwargs)
        with patch("pod.ledger._git", side_effect=unreadable):
            error = self.refused("governance_changed", "governance_changed", self.write, self.stored())
        self.assertIn("the target branch moved since governance was bound", str(error))
        self.assertIn("write a checkpoint with governance_refresh", error.detail["next_action"])
        self.assertEqual(read(self.project, "objective"), before)

    def test_owner_refresh_still_observes_unreadable_excluded_source(self):
        self.write([self.criterion()], governance={"base_ref": "target", "exclude": ["CLAUDE.md"]},
                   revision_authority=USER)
        self.commit("CLAUDE.md", "x" * (1_048_576 + 1), "oversized excluded source")
        target = self.advance("AGENTS.md", "changed policy\n")
        before = read(self.project, "objective")
        self.refused("governance_unavailable", "governance_unavailable", self.write, self.stored(),
                     governance_refresh=True, governance={"base_ref": "refs/remotes/origin/target", "base": target},
                     revision_authority=USER)
        self.assertEqual(read(self.project, "objective"), before)

    def test_bound_base_observation_still_reads_excluded_source(self):
        from pod.ledger import kernel_context
        self.write([self.criterion()], governance={"base_ref": "target", "exclude": ["CLAUDE.md"]},
                   revision_authority=USER)
        state = read(self.project, "objective")
        original = ledger._git
        seen = []
        def observe(project, argv, **kwargs):
            if argv[:2] == ["ls-tree", "-z"] and argv[-1] == "CLAUDE.md":
                seen.append(argv)
                return None
            return original(project, argv, **kwargs)
        with patch("pod.ledger._git", side_effect=observe):
            observed = kernel_context(self.project, "objective", state, None)["bound_governance"](state["checkpoint"]["governance"])
        self.assertTrue(seen)
        self.assertNotEqual(observed["status"], "observed")

    def test_excluded_sources_do_not_participate_in_adoption(self):
        self.write([self.criterion()], governance={"base_ref": "target", "exclude": ["CLAUDE.md"]},
                   revision_authority={"provenance": "user_direct", "instruction": "Exclude CLAUDE.md"})
        target = self.advance("CLAUDE.md", "changed excluded policy\n")
        state = self.write(self.stored())["checkpoint"]
        self.assertEqual(state["governance"]["base"], target)
        self.assertNotIn("CLAUDE.md", [row["path"] for row in state["governance_sources"]])


class DeliveredAdoptionTests(KernelCase):
    prepare_result = delivery.VerifiedDeliveryTests.prepare_result
    commit = governance_commit

    def test_only_objective_merge_adopts_derived(self):
        _, _, result = self.prepare_result()
        state = self.write(self.stored())["checkpoint"]
        self.assertEqual(state["governance"]["base"], result)
        self.assertEqual(state["governance_history"]["decisions"][-1]["provenance"], "derived")
        self.assertEqual(self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]["delivery"]["result"], result)

    def test_sibling_merge_after_delivery_adopts_derived(self):
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        target = self.commit("README.md", "sibling work\n", "sibling")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        state = self.write(self.stored())["checkpoint"]
        self.assertEqual(state["governance"]["base"], target)
        self.assertEqual([row["provenance"] for row in state["governance_history"]["decisions"]], ["derived"])

    def test_delivery_record_and_replay_at_unchanged_later_target_pass(self):
        _, _, result = self.prepare_result()
        target = self.commit("README.md", "sibling work\n", "sibling")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        state = self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]
        self.assertEqual((state["governance"]["base"], state["delivery"]["result"]), (target, result))
        later = self.commit("README.md", "later sibling\n", "sibling two")
        git(self.project, "update-ref", "refs/remotes/origin/target", later)
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        closed = self.write(rows, close=True, delivery={"record": "merge-1"})["checkpoint"]
        self.assertEqual((closed["governance"]["base"], closed["closure"]["report"]["status"]), (later, "closed"))

    def test_source_changing_exact_delivery_keeps_exemption_but_later_merge_refuses(self):
        _, _, result = self.prepare_result(policy_change=True)
        state = self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]
        self.assertEqual(state["governance"]["base"], self.base)
        self.assertEqual(state["governance_history"]["decisions"], [])
        self.assertEqual(self.write(self.stored())["checkpoint"]["governance"]["base"], self.base)
        target = self.commit("README.md", "sibling work\n", "sibling")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        self.refused("governance_changed", "governance_changed", self.write, self.stored())

    def test_delivery_missing_from_new_target_refuses(self):
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        git(self.project, "checkout", "--detach", self.base)
        target = self.commit("README.md", "replacement target\n", "replacement")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        before = read(self.project, "objective")
        self.refused("governance_changed", "governance_changed", self.write, self.stored())
        self.assertEqual(read(self.project, "objective"), before)

    def test_merge_outcome_after_adoption_keeps_its_admitted_base(self):
        from tests.test_finalization import ResumeTests
        patch.stopall()
        case = ResumeTests()
        case.setUp()
        try:
            decided = case.op("governor", action=case.merge())
            git(case.project, "checkout", "-q", "target")
            git(case.project, "merge", "--no-ff", "-qm", "merge", "candidate")
            result = git(case.project, "rev-parse", "HEAD")
            git(case.project, "update-ref", "refs/remotes/origin/target", result)
            case.checkpoint()
            case.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": result})
            self.assertEqual(case.checkpoint(delivery={"record": decided["record_id"]})["checkpoint"]["delivery"]["base"], case.base)
        finally:
            case.doCleanups()

    def test_earlier_shaped_merge_keeps_original_base_through_adoption_and_delivery(self):
        """Synthetic 0.13.3 shape: its merge rows lack governance_binding."""
        import json
        from pod.governor import _record_path
        from tests.test_finalization import ResumeTests
        patch.stopall()
        case = ResumeTests()
        case.setUp()
        try:
            decided = case.op("governor", action=case.merge())
            path = _record_path(case.project, "objective")
            journal = json.loads(path.read_bytes())
            row = next(row for row in journal["actions"] if row["record_id"] == decided["record_id"])
            self.assertIsNotNone(row.pop("governance_binding"))
            path.write_text(json.dumps(journal))
            git(case.project, "checkout", "-q", "target")
            git(case.project, "merge", "--no-ff", "-qm", "merge", "candidate")
            result = git(case.project, "rev-parse", "HEAD")
            git(case.project, "update-ref", "refs/remotes/origin/target", result)
            case.checkpoint()
            adopted = read(case.project, "objective")["checkpoint"]
            self.assertEqual(adopted["governance"]["proof_base"], case.base)
            case.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": result})
            delivery_row = case.checkpoint(delivery={"record": decided["record_id"]})["checkpoint"]["delivery"]
            self.assertEqual((delivery_row["base"], delivery_row["result"], delivery_row["method"]),
                             (case.base, result, "merge"))
        finally:
            case.doCleanups()

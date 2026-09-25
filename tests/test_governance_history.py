"""Governance guards retain candidate ancestry and exact user decisions."""

import subprocess
from unittest.mock import patch

from pod.errors import PodError
from pod import ledger
from pod.ledger import read
from tests import test_governance_refresh as refresh
from tests import test_kernel_boundaries as boundaries


class GovernanceHistoryTests(boundaries.KernelCase):
    commit = refresh.GovernanceRefreshTests.commit
    policy = refresh.GovernanceRefreshTests.policy
    guarded_refresh = refresh.GovernanceRefreshTests.guarded_refresh

    def authorize(self, target, rows=None, instruction="Adopt this exact target snapshot"):
        return self.write(self.stored() if rows is None else rows, governance_refresh=True,
                          governance={"base_ref": "refs/remotes/origin/target", "base": target},
                          revision_authority={"provenance": "user_direct", "instruction": instruction})

    def test_intermediate_candidate_policy_needs_exact_snapshot_authority(self):
        self.intake()
        policy = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n", "policy")
        self.candidate = self.commit("README.md", "later candidate work\n", "work")
        self.write(self.stored())
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", policy)
        rows = self.stored() + [self.policy("PX", "6")]
        self.guarded_refresh(rows)
        state = self.authorize(policy, rows)["checkpoint"]
        self.assertEqual(state["governance"]["base"], policy)
        self.assertEqual(next(row for row in state["obligations"] if row["id"] == "PX")["source"]["base"], policy)

    def test_old_checkpoint_candidates_survive_round_trip_omission_and_later_writes(self):
        self.intake()
        policy = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n", "policy")
        self.candidate = "main"
        self.write(self.stored())  # The alias must be frozen to this commit now.
        self.candidate = self.base
        boundaries.git(self.project, "reset", "-q", "--hard", self.base)
        for _ in range(3):
            self.write(self.stored(), governance_history={"candidates": [], "decisions": []})
        self.assertIn(policy, read(self.project, "objective")["checkpoint"]["governance_history"]["candidates"])
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", policy)
        self.guarded_refresh(self.stored() + [self.policy("PX", "6")])

    def test_pruned_discarded_result_requires_exact_consent_without_losing_history(self):
        self.intake(self.sub("S", boundary={"paths": ["."]}))
        frozen = self.packet(["S"], boundary={"paths": ["."]})
        admission = self.start("writer", frozen)["admission"]
        result = self.commit("AGENTS.md", boundaries.AGENTS + "Discarded policy.\n", "result")
        self.settle(admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(admission, frozen, map={"obligations": rows}, result_commit=result)
        self.write(self.stored(), dispositions=[{"admission": admission["admission_id"],
                                                "discarded": True, "reason": "preserve but do not integrate"}])
        boundaries.git(self.project, "reset", "-q", "--hard", self.base)
        boundaries.git(self.project, "reflog", "expire", "--expire=now", "--all")
        boundaries.git(self.project, "gc", "-q", "--prune=now")
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.project), "cat-file", "-e", result],
                                          capture_output=True).returncode, 0)
        target = self.commit("README.md", "independent target\n", "target")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as caught:
            self.write(self.stored(), governance_refresh=True)
        self.assertEqual(caught.exception.detail["referent"]["identity"], result)
        self.assertEqual(read(self.project, "objective"), before)
        state = self.authorize(target)
        self.assertEqual(state["admissions"][admission["admission_id"]], before["admissions"][admission["admission_id"]])
        self.assertEqual(state["checkpoint"]["governance_history"]["decisions"][-1]["base"], target)
        self.assertEqual(self.start("after", self.packet(["S"], boundary={"paths": ["."]}))["status"], "bound")

    def test_decisions_persist_through_admission_report_and_attempted_replacement(self):
        self.intake(self.sub("S"), revision_authority=refresh.USER)
        self.candidate = self.commit("README.md", "candidate\n", "work")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
        authorized = self.authorize(self.candidate)
        history = authorized["checkpoint"]["governance_history"]
        self.assertEqual([row["kind"] for row in history["decisions"]], ["initial", "snapshot"])
        self.assertEqual(authorized["report"]["governance_decisions"], history["decisions"])
        frozen = self.packet(["S"], role="investigate", resolves="question", stop_condition="answer")
        admission = self.start("investigator", frozen)["admission"]
        self.assertEqual(read(self.project, "objective")["checkpoint"]["governance_history"], history)
        self.settle(admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(admission, frozen, map={"obligations": rows})
        self.write(self.stored(), governance_history={"candidates": [], "decisions": []})
        self.assertEqual(read(self.project, "objective")["checkpoint"]["governance_history"], history)
        self.assertEqual(self.authorize(self.candidate)["checkpoint"]["governance_history"], history)

    def test_candidate_history_limit_preserves_the_previous_checkpoint(self):
        with patch("pod.ledger.MAX_GOVERNANCE_HISTORY", 2):
            self.intake()
            self.candidate = self.commit("README.md", "one\n", "one")
            self.write(self.stored())
            before = read(self.project, "objective")
            self.candidate = self.commit("README.md", "two\n", "two")
            with self.assertRaises(PodError) as caught:
                self.write(self.stored())
            self.assertEqual(caught.exception.detail["detail"], "governance_history_full")
            self.assertEqual(read(self.project, "objective"), before)

    def test_unresolvable_candidate_is_refused_before_intake_or_history_mutation(self):
        self.candidate = "pending-candidate"
        with self.assertRaises(PodError) as initial:
            self.intake()
        self.assertEqual(initial.exception.code, "invalid_checkpoint")
        self.assertIsNone(read(self.project, "objective"))
        self.candidate = self.base
        self.intake()
        before = read(self.project, "objective")
        self.candidate = "pending-candidate"
        with self.assertRaises(PodError) as later:
            self.write(self.stored())
        self.assertEqual(later.exception.detail["detail"], "candidate_identity_unavailable")
        self.assertEqual(read(self.project, "objective"), before)
        self.candidate = self.base
        target = self.commit("README.md", "independent target\n", "target")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
        state = self.write(self.stored(), governance_refresh=True)["checkpoint"]
        self.assertEqual(state["governance"]["base"], target)
        self.assertNotIn("pending-candidate", state["governance_history"]["candidates"])

    def test_malformed_shared_ancestry_observation_cannot_approve_refresh(self):
        self.intake()
        self.candidate = self.commit("README.md", "candidate\n", "work")
        self.write(self.stored())
        boundaries.git(self.project, "checkout", "-q", "target")
        target = self.commit("README.md", "independent\n", "target")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
        original = ledger._git
        for code, output in ((0, ""), (1, self.base + "\n"), (0, "unrecognized\n")):
            def observe(project, argv, **kwargs):
                if argv[:2] == ["merge-base", "--all"]:
                    return subprocess.CompletedProcess(argv, code, output, "")
                return original(project, argv, **kwargs)
            with self.subTest(code=code, output=output), patch("pod.ledger._git", side_effect=observe):
                self.guarded_refresh()

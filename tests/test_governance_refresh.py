"""Trusted target intake and exact-snapshot governance refresh boundaries."""

from copy import deepcopy
from unittest.mock import patch

from pod.internal import run as internal_run
from pod.ledger import read
from tests import kernel_support as boundaries


from tests.kernel_support import USER, governance_commit, governance_guarded_refresh, governance_policy


class GovernanceRefreshTests(boundaries.KernelCase):
    policy = governance_policy
    commit = governance_commit
    guarded_refresh = governance_guarded_refresh

    def test_missing_default_explicit_selection_allows_initial_baseline(self):
        boundaries.git(self.project, "branch", "other-target")
        boundaries.git(self.project, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        for target in ("main", "other-target"):
            with self.subTest(target=target):
                state = self.write([self.criterion(), self.policy()], objective=target,
                                   governance={"base_ref": target}, revision_authority=USER)
                self.assertEqual(state["checkpoint"]["governance"], {
                    "base_ref": "refs/heads/" + target, "selection": "user_direct",
                    "exclude": [], "base": self.base})

    def test_nested_default_alias_binds_the_canonical_default(self):
        boundaries.git(self.project, "checkout", "-qb", "release/stable")
        target = "refs/remotes/origin/release/stable"
        boundaries.git(self.project, "update-ref", target, self.base)
        boundaries.git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", target)
        state = self.write([self.criterion()], governance={"base_ref": "release/stable"})
        self.assertEqual(state["checkpoint"]["governance"], {
            "base_ref": target, "selection": "default", "exclude": [], "base": self.base})

    def test_maintained_release_policy_allows_equal_and_descendant_candidate(self):
        boundaries.git(self.project, "checkout", "-qb", "release")
        release = self.commit("AGENTS.md", boundaries.AGENTS + "Release needs a backport note.\n",
                              "release policy")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/release", release)
        for shape in ("equal", "descendant"):
            with self.subTest(shape=shape):
                self.candidate = (release if shape == "equal" else
                                  self.commit("README.md", "release fix\n", "release fix"))
                state = self.write([self.criterion(), self.policy("PR", "6")], objective=shape,
                                   governance={"base_ref": "origin/release"}, revision_authority=USER)
                self.assertEqual(state["checkpoint"]["governance"]["base"], release)
                self.assertEqual(state["checkpoint"]["obligations"][1]["source"]["base"], release)
        continued = self.write(self.stored("equal"), objective="equal",
                               governance={"base_ref": "origin/release"})
        self.assertEqual(continued["checkpoint"]["governance"]["base"], release)

    def test_initial_direct_selection_binds_exact_snapshot_without_authorship_inference(self):
        boundaries.git(self.project, "checkout", "-qb", "candidate")
        self.candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Selected baseline rule.\n",
                                     "policy before intake")
        declared = {"base_ref": "candidate", "base": self.base}
        # A stamped input base cannot override the commit actually read at intake.
        draft = internal_run("brief", {
            "project": str(self.project), "criteria": ["PoD#1"],
            "coverage": [{"criterion": "PoD#1", "check": "unit"}],
            "map": {"governance": declared, "revision_authority": USER,
                    "obligations": [self.criterion(), self.policy("PX", "6")]}})
        self.assertEqual(draft["map"]["governance"]["base"], self.candidate)
        state = self.write([self.criterion(), self.policy("PX", "6")],
                           governance=declared, revision_authority=USER)
        self.assertEqual(state["checkpoint"]["governance"]["base"], self.candidate)

    def test_changed_base_cannot_adopt_current_candidate_policy_on_same_ref(self):
        boundaries.git(self.project, "checkout", "-qb", "candidate")
        self.candidate = self.commit("README.md", "candidate work\n", "work before intake")
        initial = self.write([self.criterion()], governance={"base_ref": "candidate"},
                             revision_authority=USER)["checkpoint"]
        self.candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n",
                                     "candidate policy")
        self.refused("governance_changed", "governance_changed", self.write, self.stored())
        self.guarded_refresh()
        self.guarded_refresh([*self.stored(), self.policy("PX", "6")])
        self.guarded_refresh(governance={"base_ref": "candidate"}, revision_authority=USER)
        self.assertEqual(read(self.project, "objective")["checkpoint"], initial)

    def test_default_target_refresh_cannot_adopt_known_candidate_work(self):
        self.intake()
        self.candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n",
                                     "candidate policy")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
        self.guarded_refresh([*self.stored(), self.policy("PX", "6")])

    def test_equal_policy_bytes_do_not_authorize_a_changed_base(self):
        self.intake()
        self.candidate = self.commit("README.md", "candidate code only\n", "candidate code")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
        self.guarded_refresh()

    def test_refresh_checks_previous_candidate(self):
        self.intake()
        candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n", "policy")
        self.candidate = candidate
        self.write(self.stored())
        self.candidate = self.base
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", candidate)
        self.guarded_refresh()

    def test_refresh_checks_recorded_result_even_after_discard(self):
        self.intake(self.sub("S", boundary={"paths": ["."]}))
        frozen = self.packet(["S"], boundary={"paths": ["."]})
        admission = self.start("writer", frozen)["admission"]
        result = self.commit("AGENTS.md", boundaries.AGENTS + "Result-only rule.\n", "result policy")
        self.settle(admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.report(admission, frozen, map={"obligations": rows}, result_commit=result)
        self.write(self.stored(), dispositions=[{"admission": admission["admission_id"],
                                                "discarded": True, "reason": "do not integrate"}])
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.guarded_refresh()
        row = read(self.project, "objective")["admissions"][admission["admission_id"]]
        self.assertEqual(row["result"]["head"], result)
        self.assertEqual(row["disposition"]["kind"], "discarded")

    def test_refresh_checks_durable_admission_candidate_and_target_descendants(self):
        self.intake(self.sub("S"))
        self.candidate = self.commit("README.md", "candidate work\n", "candidate")
        self.write(self.stored())
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("investigator", frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.candidate = self.base
        self.write(rows)
        self.write(self.stored())  # neither current nor previous checkpoint is the admission candidate
        target = self.commit("AGENTS.md", boundaries.AGENTS + "Target contains candidate work.\n", "target")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
        self.guarded_refresh()

    def test_independent_target_advance_refreshes_without_new_user_selection(self):
        boundaries.git(self.project, "branch", "release")
        self.write([self.criterion(), self.policy()], governance={"base_ref": "release"},
                   revision_authority=USER)
        self.candidate = self.commit("README.md", "candidate work\n", "candidate")
        self.write(self.stored())
        boundaries.git(self.project, "checkout", "-q", "release")
        for count in (1, 2):
            target = self.commit("AGENTS.md", boundaries.AGENTS + f"Target policy {count}.\n", "target")
            state = self.write(self.stored(), governance_refresh=True)["checkpoint"]
            self.assertEqual(state["governance"]["base"], target)
            self.assertEqual(state["governance"]["base_ref"], "refs/heads/release")
            self.assertEqual(state["obligations"][1]["source"]["base"], target)

    def test_guarded_refresh_requires_fresh_exact_canonical_snapshot_decision(self):
        initial = self.intake(self.policy())["checkpoint"]
        self.candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\n", "policy")
        target = "refs/remotes/origin/target"
        boundaries.git(self.project, "update-ref", target, self.candidate)
        boundaries.git(self.project, "update-ref", "refs/heads/target", self.candidate)
        exact = {"base_ref": target, "base": self.candidate}
        for declared, authority in ((exact, None),
                                    ({"base_ref": target}, USER),
                                    (initial["governance"], USER),
                                    ({"base_ref": "origin/target", "base": self.candidate}, USER),
                                    ({"base_ref": "refs/heads/target", "base": self.candidate}, USER)):
            with self.subTest(declared=declared, authority=authority):
                self.guarded_refresh(governance=declared,
                                     **({"revision_authority": authority} if authority else {}))
        new_rule = {**self.policy("PX", "6"), "uncovered_risk": "the newly adopted target rule"}
        adopted = self.write([*self.stored(), new_rule], governance_refresh=True,
                             governance=exact, revision_authority=USER)["checkpoint"]
        self.assertEqual(adopted["governance"]["base"], self.candidate)
        self.assertEqual(adopted["obligations"][2]["source"]["base"], self.candidate)
        self.assertEqual({row["path"] for row in adopted["governance_sources"]},
                         {row["path"] for row in initial["governance_sources"]})
        self.assertEqual(adopted["obligations"][1]["introduced_seq"], initial["obligations"][1]["introduced_seq"])
        self.write(self.stored())  # the accepted snapshot is now the legitimate baseline
        old_authorization = deepcopy(exact)
        self.candidate = self.commit("AGENTS.md", boundaries.AGENTS + "Candidate-only rule.\nAnother rule.\n",
                                     "later policy")
        boundaries.git(self.project, "update-ref", target, self.candidate)
        self.guarded_refresh(governance=old_authorization, revision_authority=USER)
        accepted = self.write(self.stored(), governance_refresh=True,
                              governance={"base_ref": target, "base": self.candidate},
                              revision_authority=USER)["checkpoint"]
        self.assertEqual(accepted["governance"]["base"], self.candidate)

    def test_unavailable_refresh_relation_preserves_bound_state(self):
        self.intake()
        boundaries.git(self.project, "checkout", "-q", "target")
        target = self.commit("AGENTS.md", boundaries.AGENTS + "Target rule.\n", "target policy")
        boundaries.git(self.project, "update-ref", "refs/remotes/origin/target", target)
        with patch("pod.ledger.git_is_ancestor", return_value=None):
            self.guarded_refresh()

"""Governance re-approval without needless owner decisions, and closed worker tabs (pod#153).

Sanitized real shapes: tests/fixtures/governance-reapproval/. The first class rebuilds the rebased-candidate
topology as a synthetic Git history; the last projects a hand-closed tab from its real readback shape.
"""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from pod import config
from pod.abandoned import attach_closure
from pod.errors import PodError
from pod.governance import _independent_governance, governance_observation
from pod.ledger import read
from pod.operations import _assignment_evidence
from pod.worker_tabs import project
from tests.decision_policy_support import LOCAL, PERSON
from tests.kernel_support import USER, KernelCase, git, governance_commit

FIXTURES = Path(__file__).parent / "fixtures" / "governance-reapproval"
TARGET = "refs/remotes/origin/target"


def shape(name):
    return json.loads((FIXTURES / name).read_text())


class RebasedCandidateGovernanceTests(KernelCase):
    """A candidate rebased onto a target that moved only by other objectives' merges (pes-242 shape)."""

    commit = governance_commit

    def setUp(self):
        super().setUp()
        self.shape = shape("governance-shapes.json")["rebase_onto_moved_target"]
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.intake()
        git(self.project, "checkout", "-qb", "work")
        self.patch = self.commit("src/new.py", "reviewed patch\n", "reviewed patch")
        self.candidate = self.patch
        self.write(self.stored())  # recorded: built on the bound base

    def merge_other(self, index):
        """Merge another objective's pull request into the local target branch, as a merge commit."""
        git(self.project, "checkout", "-q", "-B", f"other-{index}", "target")
        if index == self.shape["governance_change_in_merge"]:
            self.commit("AGENTS.md", (self.project / "AGENTS.md").read_text() + "Another objective's rule.\n",
                        "other policy")
        else:
            self.commit(f"other-{index}.txt", "other work\n", f"other work {index}")
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", f"merge other {index}", f"other-{index}")
        return git(self.project, "rev-parse", "HEAD")

    def rebase_work(self, onto, *extra):
        git(self.project, "checkout", "-q", "-B", "work", onto)
        git(self.project, "cherry-pick", "--allow-empty", "--keep-redundant-commits", self.patch)
        head = git(self.project, "rev-parse", "HEAD")
        for path in extra:
            head = self.commit(path, "metadata\n", "metadata")
        return head

    def moved_target(self):
        """Other objectives' merges, one changing governance; publish the target and return an earlier state."""
        states = [self.merge_other(index) for index in range(1, self.shape["other_merges"] + 1)]
        middle = states[1]
        target = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", TARGET, target)
        return middle, target

    def adopt(self, target, authority, *, exact=True):
        fields = {"revision_authority": {**authority, "instruction": "Adopt the current target snapshot"}} \
            if authority else {}
        governance = {"base_ref": TARGET, "base": target} if exact else {"base_ref": TARGET}
        return self.write(self.stored(), governance_refresh=True, governance=governance, **fields)["checkpoint"]

    def refused_without_snapshot_decision(self, target):
        """Known objective work refuses every refresh lacking an exact snapshot decision, writing nothing."""
        before = read(self.project, "objective")
        for authority, exact in ((None, True), (LOCAL, False)):
            with self.subTest(authority=authority, exact=exact):
                error = self.refused("governance_unavailable", "governance_unavailable", self.adopt, target,
                                     authority, exact=exact)
                self.assertEqual(error.detail["referent"]["reason"], "candidate_policy_target")
                self.assertIn("snapshot decision", error.detail["next_action"])
                self.assertNotIn("direct user decision", error.detail["next_action"])
                self.assertEqual(read(self.project, "objective"), before)

    def test_rebase_onto_moved_target_adopts_by_agent_decision(self):
        _, target = self.moved_target()
        self.candidate = self.rebase_work(target, "VERSION")
        # The ordinary scope-change refresh names no exact snapshot, as the real attempt did.
        state = self.adopt(target, LOCAL, exact=False)
        self.assertEqual(state["governance"]["base"], target)
        self.assertEqual(state["governance"]["base_ref"], TARGET)

    def test_rebase_onto_moved_target_adopts_without_revision_authority(self):
        _, target = self.moved_target()
        self.candidate = self.rebase_work(target)
        self.assertEqual(self.adopt(target, None)["governance"]["base"], target)

    def test_target_holding_the_objectives_earlier_pull_request_needs_a_snapshot_decision(self):
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge this objective's earlier pull request", "work")
        self.merge_other(self.shape["governance_change_in_merge"])
        target = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", TARGET, target)
        self.candidate = self.rebase_work(target, "VERSION")
        self.refused_without_snapshot_decision(target)
        # An exact snapshot decision adopts it: a person's, or under Hands-off the coordinator's own.
        self.assertEqual(self.adopt(target, LOCAL)["governance"]["base"], target)

    def test_unrecorded_objective_commit_merged_into_the_target_still_counts(self):
        # The current candidate shares an objective commit with the target that no record names: the shared
        # commit is not a state of the target branch, so it stays objective work.
        unrecorded = self.commit("src/new.py", "reviewed patch\nfollow-up\n", "unrecorded objective commit")
        self.candidate = self.commit("src/later.py", "later\n", "later objective commit")
        self.write(self.stored())
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge part of this objective", unrecorded)
        self.merge_other(self.shape["governance_change_in_merge"])
        target = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", TARGET, target)
        self.refused_without_snapshot_decision(target)
        self.assertEqual(self.adopt(target, USER)["governance"]["base"], target)

    def test_fast_forwarded_recorded_candidate_still_counts(self):
        # A recorded objective commit on the target's own first-parent history is still objective work.
        git(self.project, "update-ref", "refs/heads/target", self.patch)
        git(self.project, "checkout", "-q", "target")
        target = self.commit("AGENTS.md", (self.project / "AGENTS.md").read_text() + "Later rule.\n", "policy")
        git(self.project, "update-ref", TARGET, target)
        self.candidate = target
        self.refused_without_snapshot_decision(target)

    def test_a_result_base_on_the_target_history_is_not_objective_work(self):
        middle, target = self.moved_target()
        observed = governance_observation(self.project, TARGET, bound_ref=TARGET, selection="default")
        established = deepcopy(read(self.project, "objective")["checkpoint"]["governance"])
        established["base"] = self.base
        admissions = {"worker": {"result": {"base": middle}}}
        self.assertEqual(_independent_governance(self.project, observed, self.patch, admissions,
                                                 established=established)["commit"], target)
        # The same commit recorded as a result head is objective work inside the target.
        with self.assertRaises(PodError) as caught:
            _independent_governance(self.project, observed, self.patch, {"worker": {"result": {"head": middle}}},
                                    established=established)
        self.assertEqual(caught.exception.detail["referent"]["reason"], "candidate_policy_target")


class ClosedWorkerTabTests(unittest.TestCase):
    """A retained, Orca-owned tab the owner closed by hand (pes-267 shape)."""

    def setUp(self):
        fixture = shape("closed-tab-readback.json")
        self.admission = fixture["admission"]
        self.readback = fixture["readback"]
        self.before = fixture["before_fix"]
        self.handle = self.admission["native_binding"]["terminalHandle"]
        self.state = {"owner": "coordinator", "checkpoint": {"objective": "objective"},
                      "admissions": {self.admission["admission_id"]: self.admission}}

    def row(self, readback=None, *, inventory=(), carriers=False):
        shown = attach_closure({"runtime": "runtime", "result": deepcopy(readback or self.readback)},
                               inventory=lambda: [dict(row) for row in inventory],
                               carriers=lambda handle: carriers)
        evidence = _assignment_evidence(shown, self.admission)
        return project(self.state, self.admission, evidence["placement"], settled=True), shown

    def test_pods_own_closed_terminal_proof_reads_as_disconnected(self):
        row, shown = self.row()
        self.assertIn("terminal_closed", shown)
        self.assertEqual((row["row"], row["listed"], row["blocks_closure"], row["command"]),
                         ("disconnected", False, False, None))

    def test_without_the_proof_the_owned_tab_stays_unreadable(self):
        for name, kwargs in (("terminal still listed", {"inventory": [{"handle": "term_worker"}]}),
                             ("incarnation still listed", {"inventory": [{"incarnationId": "incarnation-1"}]}),
                             ("carrier read unknown", {"carriers": None}),
                             ("a process carries the handle", {"carriers": True})):
            with self.subTest(name):
                row, shown = self.row(**kwargs)
                self.assertNotIn("terminal_closed", shown)
                self.assertEqual((row["row"], row.get("failed_read", {}).get("code"), row["blocks_closure"]),
                                 (self.before["row"], self.before["failed_read"], self.before["blocks_closure"]))

    def test_a_remote_host_or_another_terminal_gets_no_disconnected_row(self):
        remote = deepcopy(self.readback)
        remote["dispatch"]["hostScope"] = {"kind": "remote", "hostId": "host-b"}
        other = deepcopy(self.readback)
        other["worker"]["agentTerminalHandle"] = "term_other"
        for name, readback in (("remote host", remote), ("another terminal", other)):
            with self.subTest(name):
                row, _ = self.row(readback)
                self.assertTrue(row["blocks_closure"])
                self.assertNotEqual(row["row"], "disconnected")

    def test_a_present_terminal_keeps_its_row(self):
        connected = deepcopy(self.readback)
        connected["terminal"] = {"handle": self.handle, "worktreeId": self.admission["native_binding"]["worktreeId"],
                                 "connected": True}
        connected["observation"] = None
        # Orca's inventory lists the present terminal, so Pod attaches no proof; even a proof read from an
        # inventory that missed it leaves a connected readback's own row.
        for inventory in ([{"handle": self.handle}], []):
            with self.subTest(inventory=inventory):
                row, shown = self.row(connected, inventory=inventory)
                self.assertEqual("terminal_closed" in shown, not inventory)
                self.assertEqual((row["row"], row["blocks_closure"]), ("retained_owned", True))

    def test_a_stale_or_hand_written_proof_is_ignored(self):
        placement = _assignment_evidence({"runtime": "runtime", "result": deepcopy(self.readback),
                                          "terminal_closed": {"source": "terminal_closed", "handle": self.handle,
                                                              "incarnation": "incarnation-1"}},
                                         self.admission)["placement"]
        self.assertNotIn("terminal_closed", placement)
        stale = {**placement, "terminal_closed": "yes"}
        for facts in (placement, stale):
            with self.subTest(facts=facts.get("terminal_closed")):
                row = project(self.state, self.admission, facts, settled=True)
                self.assertEqual(row["row"], "unreadable")

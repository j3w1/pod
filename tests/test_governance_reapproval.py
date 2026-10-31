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
from pod.ledger import checkpoint, objective_root, read
from pod.operations import _assignment_evidence
from pod.worker_tabs import project
from tests import test_delivery as delivery
from tests.decision_policy_support import LOCAL, PERSON
from tests.kernel_support import USER, KernelCase, git, governance_commit

FIXTURES = Path(__file__).parent / "fixtures" / "governance-reapproval"
TARGET = "refs/remotes/origin/target"


def shape(name):
    return json.loads((FIXTURES / name).read_text())


class RebasedCandidateGovernanceTests(KernelCase):
    """A candidate rebased onto a target that moved only by other objectives' merges."""

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

    def test_rebase_onto_moved_target_adopts_without_revision_authority_in_two_steps(self):
        _, target = self.moved_target()
        recorded = self.candidate
        self.candidate = self.rebase_work(target)
        # Without a revision authority the exempted adoption cannot also change the candidate (R119).
        before = (objective_root(self.project, "objective") / "context.json").read_bytes()
        error = self.refused("governance_unavailable", "governance_unavailable", self.adopt, target, None)
        self.assertEqual(error.detail["referent"]["reason"], "adoption_only")
        self.assertIn("write the governance refresh alone first", error.detail["next_action"])
        self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), before)
        rebased, self.candidate = self.candidate, recorded
        self.assertEqual(self.adopt(target, None)["governance"]["base"], target)
        self.candidate = rebased
        self.assertEqual(self.write(self.stored())["checkpoint"]["candidate"], rebased)

    def test_target_holding_the_objectives_earlier_pull_request_needs_a_snapshot_decision(self):
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge this objective's earlier pull request", "work")
        self.merge_other(self.shape["governance_change_in_merge"])
        target = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", TARGET, target)
        self.candidate = self.rebase_work(target, "VERSION")
        self.refused_without_snapshot_decision(target)
        # A person's decision naming an alias is not the exact snapshot; the refusal names the canonical ref.
        error = self.refused("governance_unavailable", "governance_unavailable", self.write, self.stored(),
                             governance_refresh=True, governance={"base_ref": "origin/target", "base": target},
                             revision_authority=USER)
        self.assertIn(f"an alias; repeat it naming the canonical base_ref {TARGET}", error.detail["next_action"])
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


class ExemptedAdoptionOnlyTests(KernelCase):
    """A never-recorded prefix changes governance and becomes the target; its never-recorded suffix is the new
    candidate. Adopting through the first-parent exemption without authority is adoption-only (R119)."""

    commit = governance_commit

    def test_an_exempted_adoption_without_authority_changes_no_candidate_and_records_no_proof(self):
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.intake()
        git(self.project, "checkout", "-qb", "work")
        prefix = self.commit("AGENTS.md", "Changed target governance.\n", "unrecorded prefix")
        suffix = self.commit("src/old.py", "candidate work\n", "unrecorded suffix")
        git(self.project, "update-ref", TARGET, prefix)
        record = objective_root(self.project, "objective") / "context.json"
        governance = {"base_ref": TARGET, "base": prefix}
        self.candidate = suffix
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        before = record.read_bytes()
        for name, value in (("candidate and proof", rows), ("candidate alone", self.stored())):
            with self.subTest(name):
                error = self.refused("governance_unavailable", "governance_unavailable", self.write, value,
                                     governance_refresh=True, governance=governance)
                self.assertEqual(error.detail["referent"]["reason"], "adoption_only")
                self.assertIn("then record the new candidate, rows and proof", error.detail["next_action"])
                self.assertEqual(record.read_bytes(), before)
        # A policy-valid revision_authority keeps the one-write behaviour.
        self.assertEqual(self.write(self.stored(), governance_refresh=True, governance=governance,
                                    revision_authority={**LOCAL, "instruction": "Adopt the target"})
                         ["checkpoint"]["candidate"], suffix)

    def test_the_adoption_only_write_is_accepted_and_the_candidate_follows(self):
        self.intake()
        git(self.project, "checkout", "-qb", "work")
        prefix = self.commit("AGENTS.md", "Changed target governance.\n", "unrecorded prefix")
        suffix = self.commit("src/old.py", "candidate work\n", "unrecorded suffix")
        git(self.project, "update-ref", TARGET, prefix)
        adopted = self.write(self.stored(), governance_refresh=True, governance={"base_ref": TARGET, "base": prefix})
        self.assertEqual(adopted["checkpoint"]["governance"]["base"], prefix)
        self.candidate = suffix
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.assertEqual(self.write(rows)["checkpoint"]["obligations"][0]["state"], "satisfied")


class ResultBaseAdoptionOnlyTests(KernelCase):
    """A worker started on a later target state while every recorded candidate stays on the bound base; moving
    the target there with changed governance adopts only through that start point (R119 adoption-only)."""

    external = {"party": "third_party", "need": "acceptance", "unblocks_when": "accepted"}

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.intake(self.sub("S"),
                    self.sub("R", state="blocked_external", boundary={"paths": ["src"], "surfaces": []},
                             external=dict(self.external)),
                    {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "review src", "scope": {"paths": ["src"], "surfaces": []}, "question": "is src correct?",
                     "candidate": self.base, "existing_evidence": "unit", "insufficiency": "review needed",
                     "state": "blocked_external", "external": dict(self.external)})
        self.later = governance_commit(self, "AGENTS.md", "Changed target policy.\n", "later target state")
        frozen = self.packet(["S"], boundary={"paths": ["src"], "surfaces": []})
        self.start("writer", frozen)
        git(self.project, "update-ref", TARGET, self.later)
        self.record = objective_root(self.project, "objective") / "context.json"

    def send(self, changes=None, *, form="full", **extra):
        fields = {"governance_refresh": True, "governance": {"base_ref": TARGET, "base": self.later}, **extra}
        if form == "full":
            rows = self.stored()
            for key, change in (changes or {}).items():
                row = next(item for item in rows if item["id"] == key)
                if "state" in change:
                    for field in ("external", "wait", "executor", "withdrawal"):
                        row.pop(field, None)
                row.update(change)
            fields["obligations"] = rows
        else:
            fields.update(update=changes or {}, seq=read(self.project, "objective")["checkpoint"]["seq"] + 1)
        core = {key: fields.pop(key) for key in ("candidate", "criteria", "verification") if key in fields}
        return checkpoint(self.project, "objective", owner="owner", value=self.core(**core, **fields),
                          native={"runtime": "runtime", "delegation": "available"})

    def refused(self, *args, **kwargs):
        before = self.record.read_bytes()
        error = super().refused("governance_unavailable", "governance_unavailable", self.send, *args, **kwargs)
        self.assertEqual(error.detail["referent"]["reason"], "adoption_only")
        self.assertEqual(self.record.read_bytes(), before)

    def test_an_assurance_row_candidate_cannot_change(self):
        for form in ("full", "update"):
            with self.subTest(form=form):
                self.refused({"A": {"candidate": self.later}}, form=form)

    def test_a_remaining_row_cannot_change_or_take_proof(self):
        for name, change in (("state", {"R": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}),
                             ("referent", {"R": {"external": {**self.external, "need": "other"}}}),
                             ("proof", {"R": {"state": "satisfied", "evidence": [self.proof("R")]}})):
            for form in ("full", "update"):
                with self.subTest(name, form=form):
                    self.refused(change, form=form)

    def test_rows_cannot_be_added(self):
        rows = [*self.stored(), self.sub("NEW")]
        before = self.record.read_bytes()
        error = super().refused("governance_unavailable", "governance_unavailable", self.write, rows,
                                governance_refresh=True, governance={"base_ref": TARGET, "base": self.later})
        self.assertEqual((error.detail["referent"]["reason"], self.record.read_bytes()), ("adoption_only", before))

    def test_checkpoint_core_cannot_change(self):
        rebased = governance_commit(self, "src/new.py", "work on the later state\n", "new candidate")
        for name, extra in (("candidate", {"candidate": rebased}), ("criteria", {"criteria": ["PoD#1", "PoD#2"]}),
                            ("verification", {"verification": {"dependencies": ["other==1"], "environment": "other"}})):
            with self.subTest(name):
                self.refused(**extra)

    def test_map_inputs_and_dispositions_are_refused(self):
        admission = next(iter(read(self.project, "objective")["admissions"]))
        for name, extra in (("reuse", {"reuse": {"from": self.base, "ids": ["R"]}}), ("rebind", {"rebind": ["R"]}),
                            ("acceptances", {"acceptances": []}), ("close", {"close": True}),
                            ("proposals", {"proposals": []}), ("route settlements", {"route_settlements": []}),
                            ("renew", {"renew": []}),
                            ("disposition", {"dispositions": [{"admission": admission, "discarded": True,
                                                               "reason": "not needed"}]})):
            with self.subTest(name):
                self.refused(**extra)

    def test_the_adoption_only_write_and_the_authorized_one_write_route(self):
        adopted = self.send()["checkpoint"]
        self.assertEqual((adopted["governance"]["base"], adopted["candidate"]), (self.later, self.base))

    def test_a_policy_valid_authority_changes_the_row_in_the_same_write(self):
        state = self.send({"A": {"candidate": self.later}},
                          revision_authority={**LOCAL, "instruction": "Adopt and rebase the review"})["checkpoint"]
        self.assertEqual(next(row for row in state["obligations"] if row["id"] == "A")["candidate"], self.later)


class DeliveryBookkeepingRowTests(KernelCase):
    """After a delivery already inside the bound base, other merges change governance (R202): bookkeeping may
    change a remaining row's state, referent and evidence only."""

    prepare_result = delivery.VerifiedDeliveryTests.prepare_result
    move_target = delivery.VerifiedDeliveryTests.move_target
    commit = governance_commit
    external = {"party": "third_party", "need": "acceptance", "unblocks_when": "accepted"}

    def intake(self, *rows, **fields):
        return self.write([self.criterion(),
                           self.sub("R", state="blocked_external", boundary={"paths": ["src"], "surfaces": []},
                                    external=dict(self.external)),
                           {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                            "check": "review src", "scope": {"paths": ["src"], "surfaces": []},
                            "question": "is src correct?", "candidate": self.candidate, "existing_evidence": "unit",
                            "insufficiency": "review needed", "state": "blocked_external",
                            "external": dict(self.external)}], governance={"base_ref": "target"})

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.prepare_result()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)
        self.write(self.stored(), delivery={"record": "merge-1"})
        self.move_target()
        self.write(self.stored())  # an unchanged-source checkpoint adopts a base holding the delivery
        self.bound = read(self.project, "objective")["checkpoint"]["governance"]
        self.target = self.move_target("AGENTS.md")

    def record(self):
        return (objective_root(self.project, "objective") / "context.json").read_bytes()

    def update(self, changes):
        seq = read(self.project, "objective")["checkpoint"]["seq"] + 1
        return checkpoint(self.project, "objective", owner="owner", value=self.core(update=changes, seq=seq),
                          native={"runtime": "runtime", "delegation": "available"})

    def refused_both_ways(self, key, change):
        before = self.record()
        rows = self.stored()
        row = next(item for item in rows if item["id"] == key)
        row.update(change)
        for name, call in (("full rows", lambda: self.write(rows)), ("update", lambda: self.update({key: change}))):
            with self.subTest(name):
                self.refused("governance_changed", "governance_changed", call)
                self.assertEqual(self.record(), before)

    def test_external_evidence_for_a_remaining_row_keeps_the_baseline(self):
        rows = self.stored()
        rows[1].pop("external")
        rows[1].update(state="satisfied", evidence=[self.proof("R")])
        kept = self.write(rows)
        self.assertEqual(kept["checkpoint"]["governance"], self.bound)
        self.assertFalse(kept["governance_adoption"]["adopted"])
        self.assertEqual(kept["checkpoint"]["obligations"][1]["state"], "satisfied")

    def test_row_level_reuse_is_not_bookkeeping(self):
        self.refused_both_ways("R", {"state": "satisfied", "evidence": [self.proof("R")],
                                     "reuse": {"from": self.candidate}})

    def test_an_assurance_candidate_change_is_not_bookkeeping(self):
        self.refused_both_ways("A", {"candidate": self.target})


class ClosedWorkerTabTests(unittest.TestCase):
    """A retained, Orca-owned tab the owner closed by hand."""

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

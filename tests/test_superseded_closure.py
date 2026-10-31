"""Superseded objectives close by withdrawal: real-record regressions (pes-105, status-batch-142)."""

from copy import deepcopy
import json
from pathlib import Path

from pod import config
from pod.errors import PodError
from pod.ledger import checkpoint, objective_root, read
from pod.records import source_identity
from tests.decision_policy_support import LOCAL, PERSON
from tests.kernel_support import KernelCase, assurance_review, git, governance_commit, governance_policy

FIXTURES = Path(__file__).parent / "fixtures" / "superseded-closure"


def shape(name):
    return json.loads((FIXTURES / name).read_text())


class SupersededClosureCase(KernelCase):
    """The pes-105 relationships: satisfied criteria and a reviewed policy assurance, one externally
    blocked criterion; then the shared worktree moves inside the review's scope and the target gets a
    one-line governance change."""

    commit = governance_commit
    review = assurance_review

    def setUp(self):
        super().setUp()
        self.shape = shape("pes-105-shape.json")
        self.criteria = self.shape["criteria"]
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        rows = []
        wait = {"class": "sequenced", "referent": "P1"}
        for row in self.shape["rows"]:
            if row["kind"] == "criterion":
                fields = {"id": row["id"], "source": {"ref": row["ref"]}, "check": row["ref"] + " passes"}
                rows.append(self.criterion(**fields) if row["id"] == "P1"
                            else self.criterion("waiting", wait=wait, **fields))
            else:
                rows.append(governance_policy(self, key="A", parent=row["parent"], wait=wait))
        self.write(rows, governance={"base_ref": "target"})
        self.review(criteria=self.criteria, sources=[source_identity(self.project, path) for path in ("AGENTS.md", self.shape["worktree_change"]["path"])])
        rows = self.stored()
        for row in rows:
            want = next(item for item in self.shape["rows"] if item["id"] == row["id"] or item["kind"] == row["kind"] == "assurance")
            if row["kind"] == "criterion":
                row.pop("executor", None); row.pop("wait", None)
            if row["kind"] == "criterion" and want["state"] == "satisfied":
                row.update(state="satisfied", evidence=[self.proof(row["id"])])
            elif row["kind"] == "criterion":
                row.update(state="blocked_external", external={
                    "party": "third_party", "need": "an outside event", "unblocks_when": "the event arrives"})
        self.write(rows)
        self.bound = deepcopy(read(self.project, "objective")["checkpoint"])
        # The shared worktree later held another objective's commit inside the review's scope.
        self.commit(self.shape["worktree_change"]["path"], "another objective's work\n", "other work")
        change = self.shape["governance_change"]
        text = (self.project / change["path"]).read_text().replace("Docs are optional.", "Docs are required.")
        target = self.commit(change["path"], text, "one-line policy change")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        self.target = target

    def core(self, **extra):
        return super().core(**{"criteria": self.criteria, **extra})

    def withdrawn(self, *ids):
        rows = self.stored()
        for row in rows:
            if row["id"] in ids:
                row.pop("external", None); row.pop("executor", None); row.pop("wait", None)
                row.update(state="withdrawn", withdrawal={"by": "agent_decision",
                                                          "reason": "Superseded by a plain pull request"})
        return rows

    def record(self):
        return (objective_root(self.project, "objective") / "context.json").read_bytes()


class WithdrawalOnlyClosureTests(SupersededClosureCase):
    def test_withdrawal_only_closure_keeps_the_bound_baseline_and_proof(self):
        result = self.write(self.withdrawn("P3"), close=True, revision_authority=LOCAL)
        stored = read(self.project, "objective")["checkpoint"]
        self.assertTrue(stored["closure"])
        # No governance adoption: the bound base, sources and satisfied proof stay as recorded history.
        self.assertEqual(stored["governance"], self.bound["governance"])
        self.assertEqual(stored["governance_sources"], self.bound["governance_sources"])
        for before in self.bound["obligations"]:
            if before["state"] == "satisfied":
                after = next(row for row in stored["obligations"] if row["id"] == before["id"])
                self.assertEqual((after["state"], after["evidence"], after.get("receipts")),
                                 (before["state"], before["evidence"], before.get("receipts")))
        adoption = result["governance_adoption"]
        self.assertEqual((adoption["adopted"], adoption["closure"]), (False, "withdrawn"))
        self.assertEqual((adoption["base"], adoption["target"]), (self.bound["governance"]["base"], self.target))
        self.assertIn("withdrawn, not delivered", adoption["reason"])
        self.assertNotIn("delivery", adoption)
        report = stored["closure"]["report"]
        self.assertEqual([row["obligation"] for row in report["withdrawn"]], ["P3"])
        self.assertNotIn("delivery", report)
        self.assertIsNone(stored.get("delivery"))

    def test_the_label_is_left_as_it_was(self):
        from pod.ledger import kernel_view
        before = kernel_view(self.project, "objective", native={})["label"]["label"]
        result = self.write(self.withdrawn("P3"), close=True, revision_authority=LOCAL)
        self.assertEqual(result["report"]["label"]["label"], before)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["closure"]["report"]["label"]["label"], before)

    def test_refresh_route_still_requires_fresh_review_so_closure_does_not_use_it(self):
        # The loop the real record hit: adopting governance would invalidate the review binding.
        rows = [row["id"] for row in self.stored() if row["state"] == "satisfied"]
        before = self.record()
        with self.assertRaises(PodError) as caught:
            self.write(self.stored(), governance_refresh=True, rebind=rows,
                       governance={"base_ref": "refs/remotes/origin/target", "base": self.target},
                       revision_authority=LOCAL)
        expected = self.shape["before_fix"]["governance_refresh"]
        self.assertEqual((caught.exception.code, caught.exception.detail["detail"]),
                         (expected["code"], expected["detail"]))
        self.assertIn(expected["classification"], str(caught.exception))
        self.assertEqual(self.record(), before)


class WithdrawalOnlyBoundaryTests(SupersededClosureCase):
    def refused_unchanged(self, rows, **fields):
        before = self.record()
        with self.assertRaises(PodError) as caught:
            self.write(rows, **fields)
        self.assertEqual(self.record(), before)
        return caught.exception

    def test_withdrawal_without_closure_still_needs_current_governance(self):
        error = self.refused_unchanged(self.withdrawn("P3"), revision_authority=LOCAL)
        self.assertEqual(error.code, "governance_changed")

    def test_new_satisfaction_in_the_closing_write_still_needs_current_governance(self):
        rows = self.stored()
        for row in rows:
            if row["id"] == "P3":
                row.pop("external")
                row.update(state="satisfied", evidence=[self.proof("P3")])
        error = self.refused_unchanged(rows, close=True)
        self.assertEqual(error.code, "governance_changed")

    def test_a_new_row_in_the_closing_write_still_needs_current_governance(self):
        rows = self.withdrawn("P3") + [self.sub("S", state="withdrawn", parent="P1",
                                                 withdrawal={"by": "agent_decision", "reason": "never started"})]
        rows[-1].pop("wait", None)
        error = self.refused_unchanged(rows, close=True, revision_authority=LOCAL)
        self.assertEqual(error.code, "governance_changed")

    def test_withdrawal_closure_still_needs_valid_withdrawal_authority(self):
        error = self.refused_unchanged(self.withdrawn("P3"), close=True)
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_invalid", "withdrawal_unauthorized"))
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        error = self.refused_unchanged(self.withdrawn("P3"), close=True, revision_authority=LOCAL)
        self.assertEqual(error.code, "decision_policy_denied")


class WithdrawalAuthorityRefusalTests(KernelCase):
    """status-batch-142: a withdrawal without top-level revision authority names what is missing."""

    def setUp(self):
        super().setUp()
        self.shape = shape("status-batch-142-withdrawal.json")
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.write([self.criterion(id="P1")], governance={"base_ref": "target"})

    def send(self, value):
        state = read(self.project, "objective")["checkpoint"]
        return checkpoint(self.project, "objective", owner="owner",
                          value=self.core(seq=state["seq"] + 1, **value),
                          native={"runtime": "runtime", "delegation": "available"})

    def test_missing_revision_authority_is_named_with_every_allowed_form(self):
        before = (objective_root(self.project, "objective") / "context.json").read_bytes()
        with self.assertRaises(PodError) as caught:
            self.send(deepcopy(self.shape["refused_write"]))
        error = caught.exception
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_invalid", "withdrawal_unauthorized"))
        message, action = str(error), error.detail["next_action"]
        self.assertNotEqual(action, self.shape["before_fix"]["next_action"])
        self.assertIn("no top-level revision_authority", message)
        for form in ("user_direct", "agent_decision", "decision_request", "project_policy", "coordinator"):
            self.assertIn(form, message)
        self.assertIn("revision_authority", action)
        self.assertIn("agent_decision", action)
        self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), before)

    def test_the_named_next_action_shape_is_accepted(self):
        state = self.send(deepcopy(self.shape["accepted_write"]))
        row = next(row for row in state["checkpoint"]["obligations"] if row["id"] == "P1")
        self.assertEqual((row["state"], row["withdrawal"]["by"]), ("withdrawn", "agent_decision"))

    def test_mismatched_authority_names_the_matching_withdrawal(self):
        value = deepcopy(self.shape["accepted_write"])
        value["revision_authority"] = dict(PERSON)
        with self.assertRaises(PodError) as caught:
            self.send(value)
        self.assertIn('"by": "user_direct"', caught.exception.detail["next_action"])
        self.assertIn("person", str(caught.exception))

    def test_a_withdrawal_kind_that_does_not_apply_names_the_accepted_shape(self):
        for by in ("coordinator", "project_policy"):
            with self.subTest(by=by), self.assertRaises(PodError) as caught:
                self.send({"update": {"P1": {"state": "withdrawn", "withdrawal": {"by": by, "reason": "superseded"}}}})
            self.assertIn("does not apply to P1, a criterion with provenance objective", str(caught.exception))
            self.assertIn("revision_authority", caught.exception.detail["next_action"])

"""Production-boundary regression for the first out-of-scope review report."""

from unittest.mock import patch

from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import read
from pod.operations import OrcaPort
from tests.test_kernel_boundaries import KernelCase


class ScopeReconciliationTests(KernelCase):
    def label(self):
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            return internal_run("acceptance", {
                "project": str(self.project), "objective": "objective", "criteria": ["PoD#1"],
                "evidence_rows": [], "candidate": self.candidate, "policy_revision": "p", "sources": [],
                "dependencies": [], "environment": "fixture", "review_required": False,
                "hosted_required": False})["independently_reviewed"]

    def test_first_scope_mismatch_requires_reconciliation_and_withholds_binding(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]},
                     "question": "is src right?", "candidate": self.base, "existing_evidence": "tests",
                     "insufficiency": "no review", "state": "waiting",
                     "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.settle(review)
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        observation = self.report(review, frozen, scope=["src", "docs"],
                                  map={"obligations": rows})
        self.assertEqual(observation["status"], "reconciliation_required")

        state = read(self.project, "objective")
        stored = state["admissions"][review["admission_id"]]["report"]
        self.assertEqual(stored["status"], "reconciliation_required")
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("wait")
        assurance.update(state="satisfied", evidence=[{"attempt": review["admission_id"]}])
        with self.assertRaises(PodError) as bind:
            self.write(rows)
        self.assertEqual(bind.exception.code, "obligation_unaccounted")
        self.assertEqual(bind.exception.detail["detail"], "evidence_invalidated")
        self.assertEqual(self.label(), "WITHHELD")

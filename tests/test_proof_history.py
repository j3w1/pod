"""R185 (A843): the exact-candidate proof judges each row by its selected evidence. A retained REUSE receipt recorded
under an older definition, whose original scope cannot be verified, is nonqualifying history: it no longer makes the
proof unknown unless it is selected, while the handoff hold and evidence closure still see it."""
import json
from unittest.mock import patch

from pod.ledger import closed_snapshot_context, kernel_view, read
from pod.obligations import _persisted_rows, validate_closed_snapshot
from pod.operations import OrcaPort
from tests.kernel_support import git
from tests.test_admission_selected import SelectedCase
from tests.test_delivered_proof_sources import BuiltOnce

AUTHORITY = {"provenance": "user_direct", "instruction": "Revise the disposable review definition and keep its history"}


class History(BuiltOnce, SelectedCase):
    """Review A of `src` at the base, carried by REUSE to a docs-only commit, then an authorized definition change of
    A to `src` and `docs`. A's first receipt keeps that REUSE under the older definition, whose original scope can
    no longer be verified. Each class builds its record once."""

    CARRIED = ("port", "project", "root", "base", "candidate", "old", "fresh")

    def commit(self, path, text):
        (self.project / path).parent.mkdir(exist_ok=True)
        (self.project / path).write_text(text)
        git(self.project, "add", "."); git(self.project, "commit", "-qm", path)
        previous, self.candidate = self.candidate, git(self.project, "rev-parse", "HEAD")
        return previous

    def carry(self, path):
        """Carry O1 and A to a new commit outside A's scope: fresh criterion proof, REUSE for the review."""
        previous = self.commit(path, "outside the reviewed scope\n")
        rows = self.stored()
        rows[0]["evidence"] = [self.proof("O1")]
        rows[1].update(candidate=self.candidate, reuse={"from": previous})
        self.write(rows)
        return rows[1]["evidence"][0]["attempt"]

    def redefine(self, **review):
        """Reopen O1 and change A's definition under user authority, keeping its receipts."""
        rows = self.stored()
        rows[0].pop("evidence"); rows[0].update(state="active", executor="coordinator")
        rows[1].pop("evidence"); rows[1].pop("reuse")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"},
                       scope={"paths": ["src", "docs"]}, **review)
        self.write(rows, revision_authority=AUTHORITY)

    def build(self):
        self.ready()
        self.old, self.fresh = self.carry("docs/extra.txt"), None

    def setUp(self):
        BuiltOnce.setUp(self)
        native = patch.object(OrcaPort, "read_native", autospec=True,
                              side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs))
        native.start(); self.addCleanup(native.stop)

    def single_query(self):
        base, self.base = self.base, self.candidate
        try:
            return SelectedCase.single_query(self)
        finally:
            self.base = base

    def agree(self):
        """The selected read and `pod admission --objective O --candidate C` return byte-identical proof blocks."""
        single = self.single_query()
        selected = self.selected(candidate=self.candidate)
        self.assertEqual(json.dumps(selected["proof"]).encode(), json.dumps(single["proof"]).encode())
        return selected["proof"], [item.get("code") for item in single["unknowns"]]

    def receipts(self):
        row = next(row for row in self.stored() if row["id"] == "A")
        return row, {receipt["evidence"]["attempt"]: receipt for receipt in row["receipts"]}


class UnselectedHistoryTests(History):
    """After the definition change a fresh review satisfies A, and is carried by REUSE under the current definition to
    a commit outside A's new scope: A's selected evidence is fully verifiable."""

    def build(self):
        History.build(self)
        self.redefine()
        self.fresh = self.review("second-review")["admission_id"]
        rows = self.stored()
        rows[0].pop("executor"); rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)
        self.carry("notes/more.txt")

    def test_unselected_old_definition_reuse_leaves_the_proof_ready(self):
        row, receipts = self.receipts()
        self.assertEqual([item["attempt"] for item in row["evidence"]], [self.fresh])
        self.assertEqual(receipts[self.fresh]["reuse"]["definition"], row["definition"])
        self.assertNotEqual(receipts[self.old]["reuse"]["definition"], row["definition"])
        proof, unknowns = self.agree()
        self.assertEqual((proof["status"], proof["gaps"], proof["assurance"]["label"]), ("ready", [], "QUALIFIED"))
        self.assertNotIn("proof_unverified", unknowns)
        # The shared row validation still names A: the handoff hold and evidence closure read it.
        view = kernel_view(self.project, "objective", state=read(self.project, "objective"), candidate=self.candidate)
        unverifiable = []
        _persisted_rows(view["map"], view["ctx"], unverifiable=unverifiable)
        self.assertEqual(unverifiable, ["A"])

    def test_closed_history_still_holds_handoff_and_evidence_closure(self):
        self.write(self.stored(), close=True)
        state = read(self.project, "objective")
        context = closed_snapshot_context(self.project, "objective", state)
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], context), ("A",))


class SelectedHistoryTests(History):
    """The definition change selects A's old-definition REUSE receipt as A's evidence."""

    def build(self):
        History.build(self)
        self.redefine(evidence=[{"attempt": self.old}])

    def test_selected_old_definition_reuse_keeps_the_proof_unknown(self):
        row, _ = self.receipts()
        self.assertEqual([item["attempt"] for item in row["evidence"]], [self.old])
        proof, unknowns = self.agree()
        self.assertEqual(proof["status"], "unknown")
        self.assertIn("evidence_unverified:A", proof["gaps"])
        self.assertIn("proof_unverified", unknowns)

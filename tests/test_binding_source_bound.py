"""R227: admission and a completed review's receipt share one binding source bound."""
from pathlib import Path
from unittest.mock import patch

from pod.contracts import TABLE, bound
from pod.errors import PodError
from pod.history import CHUNK_LIMIT, CONTEXT_LIMIT
from pod.history_tree import PAGE_BYTES, plan
from pod.ledger import objective_root, read
from pod.obligations import admission_binding
from pod.records import source_identity
from tests.kernel_support import KernelCase, assurance_review, assurance_satisfied, git

LIMIT = bound("assurance binding", "sources", "max_items")
SHARED = bound("packet", "sources", "max_items")


def files(root):
    return {path.relative_to(root): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class BindingSourceBoundTests(KernelCase):
    review, satisfied = assurance_review, assurance_satisfied

    def setUp(self):
        super().setUp()
        bound_dir = self.project / "bound"
        bound_dir.mkdir()
        for number in range(LIMIT + 1):
            (bound_dir / f"s{number:03d}.md").write_text(f"source {number}\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "bound sources")
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
        git(self.project, "branch", "-f", "target", self.base)
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)

    def bound_fields(self, count):
        """Packet sources and source context entries that join into `count` binding sources."""
        identities = [source_identity(self.project, f"bound/s{number:03d}.md") for number in range(count)]
        sources, context = identities[:SHARED], identities[SHARED:]
        return {"sources": sources,
                "context": [{"kind": "source", "path": item["path"], "sha256": item["sha256"]} for item in context]}

    def receipt_sources(self):
        row = next(row for row in self.stored() if row["id"] == "A")
        self.assertEqual(row["state"], "satisfied")
        return row["receipts"][-1]["evidence"]["binding"]["sources"]

    def test_bound_is_both_packet_bounds_together(self):
        packet = TABLE["packet"]["fields"]
        self.assertEqual(LIMIT, packet["sources"]["max_items"] + packet["context"]["max_items"])
        self.assertEqual(LIMIT, 128)
        # No other evidence bound changes.
        self.assertEqual(bound("map evidence", "sources", "max_items"), 64)

    def test_review_over_64_and_at_the_bound_records_its_receipt(self):
        admission = self.satisfied(**self.bound_fields(LIMIT))
        self.assertEqual(len(self.receipt_sources()), LIMIT)
        stored = read(self.project, "objective")["admissions"][admission["admission_id"]]
        self.assertEqual(stored["report"]["outcome"], "succeeded")
        self.assertEqual(len(stored["binding"]["sources"]), LIMIT)

    def test_binding_over_the_bound_refuses_before_any_work(self):
        fields = self.bound_fields(LIMIT)
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review", **fields)
        before = files(objective_root(self.project, "objective"))
        # The packet bounds keep every real binding within the bound; a lower bound shows the guard.
        lowered = {**TABLE["assurance binding"]["fields"]["sources"], "max_items": LIMIT - 1}
        with patch.dict(TABLE["assurance binding"]["fields"], {"sources": lowered}):
            refusal = self.refused("unbound_assignment", "binding_sources_exceeded", self.start, "review", frozen)
        self.assertEqual(refusal.detail["referent"], {"sources": LIMIT, "limit": LIMIT - 1, "record": "assurance binding"})
        self.assertIn("no worker was started", refusal.detail["next_action"])
        self.assertEqual(self.port.workers, {})
        self.assertEqual(files(objective_root(self.project, "objective")), before)
        # A packet beyond its own bounds is refused before the binding exists.
        with self.assertRaises(PodError):
            self.packet(["A"], role="review", **self.bound_fields(LIMIT + 1))

    def test_success_refused_by_the_old_receipt_bound_records_without_a_new_review(self):
        fields = self.bound_fields(110)
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review", **fields)
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        a = next(row for row in rows if row["id"] == "A")
        a.pop("executor")
        a.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        root = objective_root(self.project, "objective")
        before = files(root)
        # Before the fix the receipt read the 64-entry map evidence bound.
        old = {**TABLE["assurance binding"]["fields"]["sources"], "max_items": 64}
        with patch.dict(TABLE["assurance binding"]["fields"], {"sources": old}):
            with self.assertRaises(PodError) as caught:
                self.report(admission, frozen, map={"obligations": rows})
        self.assertIn("binding sources are a bounded list", str(caught.exception))
        self.assertEqual(files(root), before)
        workers = dict(self.port.workers)
        self.report(admission, frozen, map={"obligations": rows})
        self.assertEqual(self.port.workers, workers)
        self.assertEqual(len(self.receipt_sources()), 110)
        self.assertEqual(list(read(self.project, "objective")["admissions"]), [admission["admission_id"]])

    def test_record_at_the_bound_stays_within_the_record_byte_cap(self):
        self.satisfied(**self.bound_fields(LIMIT))
        root = objective_root(self.project, "objective")
        self.assertLess((root / "context.json").stat().st_size, CONTEXT_LIMIT)
        chunks = [path.stat().st_size for path in (root / "history").rglob("*") if path.is_file()]
        self.assertTrue(chunks)
        self.assertLessEqual(max(chunks), CHUNK_LIMIT)

    def test_accumulated_receipts_at_the_bound_page_out_of_the_root(self):
        # The largest binding a packet can carry: 128 sources whose paths fill the packet's repr bound.
        path = "p" * bound("source", "path")

        def receipt(number):
            sources = [{"path": f"{path[:-4]}{index:04d}", "state": "present", "sha256": f"{number:064x}"}
                       for index in range(LIMIT)]
            return {"evidence": {"attempt": f"{number:064x}", "candidate": "c" * 40, "definition": "d" * 64,
                                 "governance": "e" * 64,
                                 "binding": {"policy_revision": "f" * 64, "environment": "fixture",
                                             "dependencies": [], "sources": sources, "governance": "e" * 64,
                                             "definitions": {"A": "d" * 64}}},
                    "reported_seq": number + 1}

        roots = {}
        for count in (1, bound("obligation", "receipts", "max_pending")):
            state = {"checkpoint": {"obligations": [{"id": "A", "receipts": [receipt(n) for n in range(count)]}]},
                     "admissions": {}}
            root, chunks = plan(state, force_root=True)
            self.assertLessEqual(max(map(len, chunks.values())), PAGE_BYTES)
            roots[count] = len(repr(root))
        # The root descriptor does not grow with retained receipts.
        self.assertLess(max(roots.values()), 1024)
        self.assertLessEqual(roots[64] - roots[1], 64)

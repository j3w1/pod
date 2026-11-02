"""R211 (A845, A846): a review report that lists files is refused at report time with guidance, and an
assurance whose selected review was consumed as `reconciliation_required` names that cause (pod#207)."""

from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import _path, read
from pod.records import refuse_review_files
from tests.kernel_support import KernelCase

ABSOLUTE = "/srv/evidence/review/notes.md"
OUTSIDE = "docs/review-notes.md"


class ReviewReportCase(KernelCase):
    def review(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]},
                     "question": "is src right?", "candidate": self.base, "existing_evidence": "tests",
                     "insufficiency": "no review", "state": "waiting",
                     "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.settle(review)
        return frozen, review

    def waiting(self):
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        return rows

    def satisfying(self, review):
        rows = self.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor", None)
        assurance.pop("wait", None)
        assurance.update(state="satisfied", evidence=[{"attempt": review["admission_id"]}])
        return rows

    def record_bytes(self):
        root = _path(self.project, "objective").parent
        return {str(path): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class ReportTimeTests(ReviewReportCase):
    def test_a_review_report_listing_files_is_refused_with_guidance_then_the_corrected_report_is_accepted(self):
        frozen, review = self.review()
        before = self.record_bytes()
        for files in ([ABSOLUTE], [OUTSIDE], [ABSOLUTE, "src/inside.py"]):
            with self.subTest(files=files), self.assertRaises(PodError) as caught:
                self.report(review, frozen, files=files, map={"obligations": self.satisfying(review)})
            error = caught.exception
            self.assertEqual((error.code, error.detail["detail"]), ("review_report_files", "files_listed"))
            self.assertEqual(error.detail["referent"]["field"], "files")
            self.assertEqual(error.detail["referent"]["admission"], review["admission_id"])
            self.assertIn("a review changes no repository files", error.detail["next_action"])
            self.assertIn("evidence or checks", error.detail["next_action"])
            self.assertIn("submit the corrected report", error.detail["next_action"])
            for path in files:
                self.assertNotIn(path, str(error))
                self.assertNotIn(path, str(error.detail))
            self.assertEqual(self.record_bytes(), before)
        consumed = self.report(review, frozen, files=[], map={"obligations": self.satisfying(review)})
        self.assertEqual(consumed["status"], "validated_observation")
        state = read(self.project, "objective")
        self.assertEqual(state["admissions"][review["admission_id"]]["report"]["status"], "validated_observation")
        self.assertEqual(next(row for row in self.stored() if row["id"] == "A")["state"], "satisfied")

    def test_implementation_and_investigation_roles_are_not_checked(self):
        for role in ("implement", "investigate", None):
            with self.subTest(role=role):
                self.assertIsNone(refuse_review_files(role, {"files": [ABSOLUTE]}, admission="a"))
        self.assertIsNone(refuse_review_files("review", {"files": []}, admission="a"))

    def test_an_implementation_report_outside_its_assignment_is_still_consumed_for_reconciliation(self):
        self.intake(self.sub("S", boundary={"paths": ["src"]}))
        frozen = self.packet(["S"], boundary={"paths": ["src"]})
        admission = self.start("impl", frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        s = next(row for row in rows if row["id"] == "S")
        s.pop("executor")
        s.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        consumed = self.report(admission, frozen, files=[ABSOLUTE, OUTSIDE], map={"obligations": rows})
        self.assertEqual((consumed["status"], consumed["deviations"]),
                         ("reconciliation_required", ["file_outside_assignment"]))
        stored = read(self.project, "objective")["admissions"][admission["admission_id"]]["report"]
        self.assertEqual(stored["status"], "reconciliation_required")


class ConsumedReviewTests(ReviewReportCase):
    def consumed(self, **report):
        """A review report consumed as `reconciliation_required` before report-time refusal existed."""
        frozen, review = self.review()
        with patch("pod.records.refuse_review_files"):
            observation = self.report(review, frozen, map={"obligations": self.waiting()}, **report)
        self.assertEqual(observation["status"], "reconciliation_required")
        return frozen, review

    def selection_refusal(self, review):
        with self.assertRaises(PodError) as caught:
            self.write(self.satisfying(review))
        error = caught.exception
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_unaccounted", "evidence_invalidated"))
        return error

    def test_selecting_a_review_consumed_with_listed_files_names_the_cause(self):
        frozen, review = self.consumed(files=[ABSOLUTE])
        before = self.record_bytes()
        error = self.selection_refusal(review)
        invalidation = error.detail["referent"]["invalidations"][0]
        self.assertEqual(invalidation["classification"], "review report unreconciled")
        self.assertIn("the review report was consumed as reconciliation_required: file_outside_assignment",
                      invalidation["correction"])
        self.assertIn(review["admission_id"], invalidation["correction"])
        self.assertIn("fresh review", invalidation["correction"])
        self.assertNotIn("binding change", str(error))
        self.assertNotIn(ABSOLUTE, str(error))
        self.assertEqual(self.record_bytes(), before)
        # The consumed report still reads, and replays, exactly as recorded.
        replay = self.report(review, frozen, files=[ABSOLUTE])
        self.assertEqual(replay["status"], "reconciliation_required")
        self.assertEqual(self.record_bytes(), before)
        conflict = self.refused("report_conflict", None, self.report, review, frozen, files=[])
        self.assertNotEqual(conflict.code, "review_report_files")

    def test_a_changed_scope_names_its_own_deviation(self):
        _, review = self.consumed(scope=["src", "docs"])
        invalidation = self.selection_refusal(review).detail["referent"]["invalidations"][0]
        self.assertEqual(invalidation["classification"], "review report unreconciled")
        self.assertIn("reconciliation_required: reported_scope_changed", invalidation["correction"])

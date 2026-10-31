"""Public status projects existing checkpoint questions without new state or authority."""

import json
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import objective_root, read
from pod.status import QUESTION_BYTES, status
from tests.kernel_support import KernelCase
from tests import test_status_selection as selection


class RecordedQuestionSelectionTests(KernelCase):
    def selected(self, *args):
        return selection.StatusSelectionTests.status(self, *args)

    def test_no_current_run_omits_projection(self):
        observed = status(self.project, None, current_run_fn=lambda: {"run": None})
        self.assertEqual(observed["status"], "selection_required")
        self.assertNotIn("recorded_questions", observed)

    def test_current_run_read_failure_omits_projection(self):
        def unavailable():
            raise PodError("fixture_native_unavailable", "unavailable")
        observed = status(self.project, None, current_run_fn=unavailable)
        self.assertEqual((observed["status"], observed["blocker"]), ("unavailable", "fixture_native_unavailable"))
        self.assertNotIn("recorded_questions", observed)

    def test_ambiguous_run_omits_projection_without_writing(self):
        self.intake()
        self.write([self.criterion()], objective="second", governance={"base_ref": "target"})
        paths = [objective_root(self.project, key) / "context.json" for key in ("objective", "second")]
        before = [path.read_bytes() for path in paths]
        observed = self.selected("--run", "run")
        self.assertEqual(observed["status"], "selection_required")
        self.assertEqual({row["objective"] for row in observed["choices"]}, {"objective", "second"})
        self.assertNotIn("recorded_questions", observed)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_run_without_matching_objective_omits_projection(self):
        self.intake()
        observed = self.selected("--run", "other")
        self.assertEqual((observed["status"], observed["objective"]), ("observed", None))
        self.assertNotIn("recorded_questions", observed)

    def test_superseded_run_omits_projection(self):
        superseded = [{"objective": "earlier", "blocked": True, "converted": False}]
        with patch("pod.ledger.superseded_for_run", return_value=superseded):
            observed = self.selected("--run", "run")
        self.assertEqual((observed["status"], observed["blocker"]), ("blocked", "objective_superseded"))
        self.assertEqual(observed["superseded"], superseded)
        self.assertNotIn("recorded_questions", observed)

    def test_run_record_read_failure_omits_projection(self):
        with patch("pod.status.contexts_for_run", side_effect=PodError("fixture_unreadable", "unavailable")):
            observed = self.selected("--run", "run")
        self.assertEqual((observed["status"], observed["blocker"]), ("blocked", "fixture_unreadable"))
        self.assertNotIn("recorded_questions", observed)

    def test_explicit_unknown_objective_has_unavailable_projection(self):
        observed = self.selected("--objective", "unknown")
        self.assertEqual((observed["status"], observed["blocker"]), ("blocked", "objective_unknown"))
        projection = observed["recorded_questions"]
        self.assertEqual((projection["status"], projection["total"], projection["missing"]),
                         ("unavailable", None, ["checkpoint"]))

    def test_explicit_objective_keeps_projection_on_run_mismatch(self):
        self.intake(questions=[])
        observed = self.selected("--run", "other", "--objective", "objective")
        self.assertEqual(observed["blocker"], "objective_run_mismatch")
        self.assertEqual(observed["recorded_questions"]["status"], "complete")

    def test_single_objective_run_has_same_projection_as_explicit_selection(self):
        self.intake(questions=[{"id": "question", "text": "Choose", "choices": ["yes"], "resolved": False}])
        by_run = self.selected("--run", "run")
        by_name = self.selected("--objective", "objective")
        self.assertEqual(by_run["objective"], "objective")
        self.assertEqual(by_run["recorded_questions"], by_name["recorded_questions"])
        self.assertEqual(by_run["recorded_questions"]["status"], "complete")


class RecordedQuestionStatusTests(KernelCase):
    def public(self):
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        result = selection.StatusSelectionTests.status(self, "--objective", "objective")
        self.assertEqual(path.read_bytes(), before)
        return result["recorded_questions"]

    def test_populated_questions_preserve_identity_choices_and_resolution(self):
        questions = [{"id": "delivery-choice", "text": "Where should this candidate go?",
                      "choices": ["merge remotely", "keep local", "defer"], "resolved": False},
                     {"reference": "owner-question", "question": "Use the proposed target?",
                      "choices": [{"value": "yes", "label": "Approve target"}],
                      "answer": "yes", "status": "resolved", "resolution": {"reference": "owner-answer"}}]
        self.intake(questions=questions)
        observed = self.public()
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual(observed["schema"], "pod-recorded-questions/v1")
        self.assertEqual((observed["status"], observed["total"], observed["truncated"], observed["missing"]),
                         ("complete", 2, False, []))
        self.assertEqual(observed["checkpoint"], {key: checkpoint[key] for key in ("seq", "revision")})
        first, second = observed["items"]
        self.assertEqual((first["id"], first["reference"], first["text"], first["choices"], first["resolution"]),
                         ("delivery-choice", None, questions[0]["text"], questions[0]["choices"], {"resolved": False}))
        self.assertEqual((second["id"], second["reference"], second["text"], second["choices"]),
                         (None, "owner-question", questions[1]["question"], questions[1]["choices"]))
        self.assertEqual(second["resolution"], {key: questions[1][key] for key in ("resolution", "answer", "status")})
        self.assertEqual((first["missing"], second["missing"]), ([], []))

    def test_explicit_empty_is_complete(self):
        self.intake(questions=[])
        observed = self.public()
        self.assertEqual((observed["status"], observed["items"], observed["total"], observed["reason"]),
                         ("complete", [], 0, None))

    def test_changed_question_retains_id_and_reports_new_checkpoint_sequence(self):
        row = {"id": "question", "text": "Old candidate?", "choices": ["yes", "no"], "resolved": False}
        self.intake(questions=[row])
        before = self.public()
        self.write(self.stored(), questions=[{**row, "text": "New candidate?", "resolved": True, "answer": "yes"}])
        after = self.public()
        self.assertEqual(before["items"][0]["id"], after["items"][0]["id"])
        self.assertGreater(after["checkpoint"]["seq"], before["checkpoint"]["seq"])
        self.assertEqual(after["items"][0]["text"], "New candidate?")
        self.assertEqual(after["items"][0]["resolution"], {"resolved": True, "answer": "yes"})

    def test_legacy_string_and_partial_objects_expose_missing_facts(self):
        self.intake(questions=["owner-question", {"id": "only-id"}, 17])
        observed = self.public()
        self.assertEqual((observed["status"], observed["total"], observed["truncated"]), ("partial", 3, False))
        earlier, partial, unsupported = observed["items"]
        self.assertEqual((earlier["id"], earlier["reference"], earlier["text"]), (None, "owner-question", "owner-question"))
        self.assertEqual(earlier["missing"], ["choices", "resolution"])
        self.assertEqual(partial["missing"], ["text", "choices", "resolution"])
        self.assertEqual(unsupported["missing"], ["identity", "text", "choices", "resolution"])
        self.assertIsNone(unsupported["reference"])

    def test_missing_legacy_questions_are_unavailable_not_empty(self):
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_bytes())
        # Synthetic earlier missing-field control; the status reader must not invent emptiness.
        state["checkpoint"].pop("questions")
        path.write_text(json.dumps(state))
        observed = self.public()
        self.assertEqual((observed["status"], observed["total"], observed["reason"]),
                         ("unavailable", None, "questions_missing"))
        self.assertIn("questions", observed["missing"])

    def test_unreadable_record_is_explicitly_unavailable(self):
        self.intake()
        with patch("pod.ledger.read", side_effect=PodError("fixture_unreadable", "unavailable")):
            observed = self.public()
        self.assertEqual((observed["status"], observed["total"], observed["reason"]),
                         ("unavailable", None, "fixture_unreadable"))
        self.assertEqual(observed["missing"], ["checkpoint"])

    def test_row_count_and_byte_bounds_report_partial_not_complete(self):
        question = {"id": "question", "text": "Choose", "choices": ["yes"], "resolved": False}
        self.intake(questions=[{**question, "id": str(index)} for index in range(65)])
        count = self.public()
        self.assertEqual((count["status"], count["total"], len(count["items"]), count["truncated"]),
                         ("partial", 65, 64, True))
        self.assertEqual(count["missing"], ["items"])
        self.write(self.stored(), questions=[{**question, "id": str(index), "text": "x" * 4096} for index in range(12)])
        sized = self.public()
        self.assertEqual((sized["status"], sized["truncated"], sized["reason"]), ("partial", True, "projection_limit"))
        self.assertTrue(0 < len(sized["items"]) < 12)
        self.assertTrue(len(json.dumps(sized, ensure_ascii=False).encode()) <= QUESTION_BYTES)

    def test_oversized_fact_and_missing_sequence_are_partial_without_invention(self):
        self.intake(questions=[{"id": "question", "text": "Choose", "choices": ["x" * 4096], "resolved": False,
                               "owner_only": True}])
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_bytes())
        # A synthetic earlier map-less checkpoint, which has no map revision.
        state["checkpoint"].pop("obligations")
        state["checkpoint"].pop("revision")
        path.write_text(json.dumps(state))
        observed = self.public()
        self.assertEqual(observed["status"], "partial")
        self.assertEqual(observed["missing"], ["checkpoint.revision"])
        self.assertIsNone(observed["checkpoint"]["revision"])
        self.assertEqual(observed["items"][0]["missing"], ["choices"])
        self.assertIsNone(observed["items"][0]["choices"])
        self.assertTrue(observed["items"][0]["owner_only"])

    def test_unsupported_questions_container_is_unavailable(self):
        self.intake(questions={"unexpected": "shape"})
        observed = self.public()
        self.assertEqual((observed["status"], observed["total"], observed["reason"]),
                         ("unavailable", None, "questions_unsupported"))

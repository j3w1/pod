"""Production status separates the derived lead allowance from the owner feed."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import unittest

from pod.assurance import allowance_question
from pod.config import load
from pod.errors import PodError
from pod.ledger import tool_decision
from pod.status import decision_status, recorded_questions, render, status
from tests import test_review_rounds as rounds


class LeadQuestionFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rounds.ReviewRoundsTests.setUpClass()
        cls.addClassCleanup(rounds.ReviewRoundsTests.doClassCleanups)

    def setUp(self):
        self.fixture = rounds.ReviewRoundsTests()
        self.fixture.restore("due")
        self.case = self.fixture.case

    def public(self, **fields):
        before = self.fixture.facts()
        result = status(self.case.project, fields.pop("run", "run"), objective="objective", **fields)
        self.assertEqual(self.fixture.facts(), before)
        # Filtering the owner feed must never filter or renumber decision waits.
        expected = decision_status(load(self.case.project), before["checkpoint"], objective=True)
        for key in ("needs_you", "waiting_decisions"):
            self.assertEqual(result[key], expected[key])
        return result

    def test_current_lead_question_stays_visible_without_an_owner_feed_row(self):
        result = self.public()
        self.assertEqual((result["recorded_questions"]["status"], result["recorded_questions"]["total"],
                          result["recorded_questions"]["items"]), ("complete", 0, []))
        self.assertEqual(result["review_allowance_question"], self.fixture.questions()[0])
        self.assertEqual((result["report"]["review_allowance"]["state"],
                          result["report"]["review_allowance"]["rounds"]), ("due", 3))
        self.assertEqual(result["waiting_decisions"]["total"], 1)
        self.assertIsNone(result["needs_you"])
        output = StringIO()
        with redirect_stdout(output):
            render(result)
        self.assertIn("Repository lead: " + self.fixture.questions()[0]["text"], output.getvalue())

    def test_exact_text_and_labels_without_due_admissions_remain_owner_questions(self):
        self.fixture.restore("one")
        canonical = allowance_question(3, 3)
        self.case.write(questions=[canonical])
        result = self.public()
        self.assertNotIn("review_allowance_question", result)
        self.assertEqual(result["recorded_questions"]["total"], 1)
        self.assertEqual(result["recorded_questions"]["items"][0]["id"], canonical["id"])
        self.assertIn(canonical, self.fixture.facts()["checkpoint"]["questions"])

    def lookalikes_reach_owner_feed(self, changes):
        for change in changes:
            with self.subTest(change=change):
                self.fixture.restore("due")
                row = {**allowance_question(3, 3), **change}
                self.case.write(questions=[row])
                result = self.public()
                self.assertEqual(result["recorded_questions"]["total"], 1)
                self.assertEqual(result["recorded_questions"]["items"][0]["index"], 0)
                self.assertEqual(result["review_allowance_question"], allowance_question(3, 3))
                self.assertIn(row, self.fixture.facts()["checkpoint"]["questions"])

    def test_identity_recipient_kind_and_text_lookalikes_reach_owner_feed(self):
        self.lookalikes_reach_owner_feed([
            {"id": "ordinary-owner"}, {"id": "review-allowance-99"}, {"recipient": "owner"}, {"owner_only": True},
            {"kind": "owner_question"}, {"text": "A separate actual owner decision"}])

    def test_boolean_sensitive_policy_and_schema_lookalikes_reach_owner_feed(self):
        self.lookalikes_reach_owner_feed([
            {"owner_only": 0}, {"resolved": 0}, {"category": "secrets"},
            {"categories": ["policy_changes"]}, {"schema": "ordinary-question/v1"}, {"choices": ["approve"]}])

    def test_mixed_ordinary_legacy_policy_and_real_tool_questions_keep_attention(self):
        ordinary = {"id": "owner-choice", "text": "Choose the owner outcome", "choices": ["yes", "no"],
                    "resolved": False}
        policy = {**ordinary, "id": "policy-choice", "categories": ["policy_changes"]}
        self.case.write(questions=[ordinary, policy, "earlier owner question"])
        tool = tool_decision(self.case.project, "objective", action="check", tool="fixture-vault",
                             category="secrets", operation="read fixture data")
        result = self.public()
        feed = result["recorded_questions"]
        self.assertEqual((feed["schema"], feed["total"]), ("pod-recorded-questions/v1", 4))
        self.assertEqual([row["index"] for row in feed["items"]], [0, 1, 2, 4])
        self.assertEqual(feed["items"][-1]["id"], tool["invocation"])
        self.assertEqual(result["waiting_decisions"]["total"], 5)
        self.assertTrue(result["needs_you"])
        self.assertEqual(len(recorded_questions(self.fixture.facts()["checkpoint"])["items"]), 5)

    def test_owner_projection_limits_exclude_only_the_lead_and_keep_source_positions(self):
        questions = [{"id": f"owner-{i}", "text": "Choose", "choices": ["yes"], "resolved": False}
                     for i in range(65)]
        self.case.write(questions=questions)
        result = self.public()
        self.assertEqual((result["recorded_questions"]["total"], len(result["recorded_questions"]["items"]),
                          result["recorded_questions"]["truncated"]), (65, 64, True))
        self.assertEqual(result["waiting_decisions"]["total"], 66)
        self.assertEqual(result["review_allowance_question"], allowance_question(3, 3))

    def test_recorded_lead_answer_stays_visible_and_grants_only_its_bounded_round(self):
        before = deepcopy(self.fixture.facts()["checkpoint"]["questions"])
        self.fixture.lead_answer()
        result = self.public()
        self.assertNotIn("review_allowance_question", result)
        answered = result["recorded_questions"]["items"][0]
        self.assertEqual(answered["text"], before[0]["text"])
        self.assertEqual(answered["resolution"], {"resolved": True, "status": "answered",
                                                "answer": {"choice": "continue", "rounds": 1}})
        self.assertEqual(result["report"]["review_allowance"]["granted"], 4)

    def test_run_mismatch_and_unavailable_native_read_keep_lead_feed_routing(self):
        result = self.public(run="other")
        self.assertEqual(result["blocker"], "objective_run_mismatch")
        self.assertEqual(result["recorded_questions"]["total"], 0)
        self.assertEqual(result["review_allowance_question"], allowance_question(3, 3))
        def unavailable(_):
            raise PodError("fixture_unavailable", "unavailable")
        result = self.public(worker_rows_fn=unavailable)
        self.assertEqual(result["recorded_questions"]["total"], 0)
        self.assertEqual(result["review_allowance_question"], allowance_question(3, 3))

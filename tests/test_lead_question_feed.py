"""Production status separates the derived lead allowance from the owner feed."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import unittest

from pod.assurance import REVIEW_ORIGIN, allowance_question, review_allowance
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
        seed = rounds.ReviewRoundsTests()
        seed.restore("third")
        seed.finish()
        seed.case.write()
        rounds.ReviewRoundsTests.save("clean-due", seed.pending)

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
        for projection in (result["report"], result["obligations"]):
            self.assertEqual(projection["review_allowance"]["question"], "review-allowance-3")
        self.assertEqual(result["waiting_decisions"]["total"], 1)
        self.assertIsNone(result["needs_you"])
        output = StringIO()
        with redirect_stdout(output):
            render(result)
        self.assertIn("Repository lead: " + self.fixture.questions()[0]["text"], output.getvalue())

    def test_public_open_and_running_allowances_omit_only_null_question(self):
        for seed, expected in (("one", "open"), ("third", "running")):
            with self.subTest(seed=seed):
                self.fixture.restore(seed)
                facts = self.fixture.facts()
                private = review_allowance(facts["checkpoint"], {"admissions": facts["admissions"]})
                self.assertIsNone(private["question"])
                result = self.public()
                for projection in (result["report"], result["obligations"]):
                    allowance = projection["review_allowance"]
                    self.assertEqual(allowance["state"], expected)
                    self.assertNotIn("question", allowance)
                    self.assertEqual(allowance, {key: value for key, value in private.items() if key != "question"})

    def test_public_stopped_allowance_has_no_null_question_and_keeps_answer(self):
        self.fixture.answer("stop")
        result = self.public()
        for projection in (result["report"], result["obligations"]):
            self.assertEqual(projection["review_allowance"]["state"], "stopped")
            self.assertNotIn("question", projection["review_allowance"])
        self.assertEqual(self.fixture.questions()[0]["answer"]["choice"], "stop")

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

    def clear_clean_review(self, mode):
        rows = self.case.stored()
        for row in rows:
            row.pop("wait", None)
            row.pop("executor", None)
            row.update(state="satisfied", evidence=[{"attempt": self.fixture.pending[0]["admission_id"]}]
                       if row["id"] == "A" else [self.case.proof(row["id"])])
        value = self.case.core(obligations=rows)
        if mode == "carried":
            value["questions"] = deepcopy(self.fixture.facts()["checkpoint"]["questions"])
        elif mode == "public":
            value["questions"] = self.fixture.questions()
        elif mode == "omitted":
            value.pop("questions")
        return self.case.op("checkpoint", value=value)

    def clear_controls(self, modes):
        for mode in modes:
            with self.subTest(mode=mode):
                self.fixture.restore("clean-due")
                before = deepcopy(self.fixture.facts()["checkpoint"]["questions"])
                self.clear_clean_review(mode)
                result = self.public()
                self.assertEqual(result["report"]["unfinished"], [])
                self.assertEqual(result["report"]["review_allowance"]["state"], "clear")
                for projection in (result["report"], result["obligations"]):
                    self.assertNotIn("question", projection["review_allowance"])
                self.assertEqual(result["recorded_questions"]["items"], [])
                self.assertEqual(self.fixture.facts()["checkpoint"]["questions"], before)
                self.assertFalse(result["review_allowance_question"]["resolved"])

    def test_carried_clean_completion_preserves_the_original_lead_source(self):
        self.clear_controls(("carried", "public"))

    def test_empty_and_omitted_questions_preserve_owned_clean_completion_history(self):
        self.clear_controls(("empty", "omitted"))

    def test_answers_simplification_stop_and_renewal_preserve_question_origin(self):
        for choice in ("continue", "simplify", "stop", "renew"):
            with self.subTest(choice=choice):
                self.fixture.restore("due")
                origin = self.fixture.facts()["checkpoint"]["questions"][0][REVIEW_ORIGIN]
                if choice == "renew":
                    self.case.write(renew=[{"id": "A", "as": "A2"}])
                else:
                    rows = self.case.stored()
                    if choice == "simplify":
                        next(row for row in rows if row["id"] == "K")["check"] = "simplified repair"
                    self.fixture.lead_answer(choice, rows=rows)
                self.assertEqual(self.fixture.facts()["checkpoint"]["questions"][0][REVIEW_ORIGIN], origin)
                result = self.public()
                self.assertTrue(all(row["resolution"]["resolved"] for row in result["recorded_questions"]["items"]))

    def test_next_due_and_candidate_movement_retain_prior_source_and_answer(self):
        origin = deepcopy(self.fixture.facts()["checkpoint"]["questions"][0][REVIEW_ORIGIN])
        self.fixture.lead_answer()
        self.fixture.repair()
        self.fixture.begin()
        self.fixture.finish()
        self.case.write()
        self.case.move()
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "K")["evidence"] = [self.case.proof("K")]
        self.case.write(rows)
        result = self.public()
        self.assertEqual(result["review_allowance_question"]["id"], "review-allowance-4")
        stored = self.fixture.facts()["checkpoint"]["questions"]
        self.assertEqual(stored[0][REVIEW_ORIGIN], origin)
        self.assertNotEqual(stored[-1][REVIEW_ORIGIN], origin)
        self.assertEqual(result["recorded_questions"]["items"][0]["resolution"]["answer"], {"choice": "continue", "rounds": 1})

    def test_forged_or_edited_origin_claims_remain_visible_owner_questions(self):
        for change in ({"seq": 999}, {"admission": "unknown"}, {"granted": 9}, {"seq": True}):
            with self.subTest(change=change):
                self.fixture.restore("due")
                row = deepcopy(self.fixture.facts()["checkpoint"]["questions"][0])
                row[REVIEW_ORIGIN].update(change)
                self.case.write(questions=[row])
                result = self.public()
                self.assertEqual(result["recorded_questions"]["total"], 1)
                self.assertEqual(result["recorded_questions"]["items"][0]["text"], row["text"])
                self.assertIsNone(self.fixture.facts()["checkpoint"]["questions"][0][REVIEW_ORIGIN])

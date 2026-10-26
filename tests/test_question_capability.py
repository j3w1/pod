"""Owned question records retain a fail-closed preceding-reader boundary."""
from copy import deepcopy
import tempfile
import unittest

from pod.errors import PodError
from pod.history import compact, hydrate, publish_context
from pod.ledger import _read, _validate_context, _write, objective_root, tool_decision
from tests import test_lead_question_feed as feed
from tests.test_history_regressions import previous_history_bundle, previous_history_call


class QuestionCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        feed.LeadQuestionFeedTests.setUpClass()
        cls.addClassCleanup(feed.LeadQuestionFeedTests.doClassCleanups)
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.previous = previous_history_bundle(temporary.name, "deda03a2eea5cc01ae4ed983f1f441d2d0b483ab")

    def setUp(self):
        self.probe = feed.LeadQuestionFeedTests()
        self.probe.setUp()
        self.case = self.probe.case

    def assert_preceding_refuses(self):
        state = self.probe.fixture.facts()
        self.assertEqual(state["question_channels"], 1)
        for operation in ("read", "write"):
            result = previous_history_call(self.previous, state, operation=operation)
            self.assertEqual(result["version"], "0.21.21")
            self.assertFalse(result["accepted"])
            self.assertTrue(result["unchanged"])

    def test_genuine_preceding_reader_and_writer_refuse_owned_due_and_clear(self):
        self.assert_preceding_refuses()
        self.probe.fixture.restore("clean-due")
        self.probe.clear_clean_review("carried")
        self.assert_preceding_refuses()

    def test_current_reader_refuses_missing_or_unknown_capability_without_mutation(self):
        state = self.probe.fixture.facts()
        for value in (None, 0, True, 2):
            proposed = deepcopy(state)
            if value is None:
                proposed.pop("question_channels")
            else:
                proposed["question_channels"] = value
            before = deepcopy(proposed)
            with self.assertRaises(PodError):
                _validate_context(proposed)
            self.assertEqual(proposed, before)

    def test_caller_flags_cannot_introduce_or_lower_the_capability(self):
        before = self.probe.fixture.facts()
        for value in (0, 1, 2):
            with self.assertRaises(PodError):
                self.case.write(question_channels=value)
            self.assertEqual(self.probe.fixture.facts(), before)
        self.probe.fixture.restore("one")
        self.case.write(questions=[{"id": "owner", "text": "Owner choice", "resolved": False,
                                    "choices": ["yes"], "question_channels": 1}])
        state = self.probe.fixture.facts()
        self.assertNotIn("question_channels", state)
        self.assertTrue(previous_history_call(self.previous, state)["accepted"])

    def test_capability_survives_tool_decision_answer_and_compaction(self):
        self.probe.fixture.lead_answer()
        tool_decision(self.case.project, "objective", action="check", tool="fixture-tool",
                      category="secrets", operation="read fixture data")
        self.case.write(questions=self.probe.fixture.facts()["checkpoint"]["questions"],
                        revision_authority={"provenance": "user_direct", "instruction": "Record the fixture revision"})
        state = self.probe.fixture.facts()
        record, chunks = compact(state, force=True)
        self.assertTrue(chunks)
        self.assertEqual(record["question_channels"], 1)
        self.assertEqual(hydrate(record, chunks.__getitem__), state)
        path = objective_root(self.case.project, "objective") / "context.json"
        publish_context(path, record, chunks)
        self.assertEqual(_read(path)["question_channels"], 1)
        self.assert_preceding_refuses()

    def test_capability_survives_closure_and_unknown_values_cannot_be_downgraded(self):
        self.probe.fixture.restore("clean-due")
        self.probe.clear_clean_review("empty")
        self.case.write(close=True)
        self.assert_preceding_refuses()
        path = objective_root(self.case.project, "objective") / "context.json"
        before = path.read_bytes()
        state = self.probe.fixture.facts()
        state["question_channels"] = 2
        with self.assertRaises(PodError):
            _write(path, state)
        self.assertEqual(path.read_bytes(), before)

"""Synthetic visibility facts and public status boundaries; no native mutation proof."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

from pod import display, policy, readiness
from pod.config import load
from pod.ledger import objective_root
from pod.status import QUESTION_BYTES, public_status, render, status
from tests.kernel_support import KernelCase
from tests.test_display import admit, inventories, state_fixture
from tests import test_status_selection as selection


def snapshot(name=None, pending=()):
    stored = ({"preset": name, "categories": policy.preset(name), "setup": "answered",
               "decision": {"instruction": "private policy answer"}} if name else None)
    return {"policy": stored, "permissions": [], "pending": list(pending), "generation": 3}


def question(identity="choice", category="secrets", **extra):
    return {"id": identity, "text": "Decide", "choices": [], "state": "pending",
            "categories": [category], **extra}


def checkpoint(questions=()):
    return {"seq": 1, "revision": 0, "questions": list(questions)}


class DecisionViewTests(unittest.TestCase):
    def test_setup_offer_is_not_a_recorded_wait_and_skipped_does_not_offer(self):
        observed = readiness.decision_view(snapshot())
        self.assertEqual(observed["decision_policy"]["preset"], "careful")
        self.assertIsNotNone(observed["setup_question"])
        self.assertFalse(observed["needs_you"])
        self.assertEqual(observed["waiting_decisions"]["items"], [])
        saved = snapshot()
        saved["policy"] = {"preset": "careful", "categories": policy.preset("careful"), "setup": "skipped"}
        self.assertIsNone(readiness.decision_view(saved)["setup_question"])

    def test_only_actual_policy_person_waits_need_you_ignoring_actor_claims(self):
        pending = [question(actor="person", required="person", waiting_on="person")]
        for name, required in (("careful", "person"), ("delegate", "trusted_agents"), ("hands_off", "any_agent")):
            with self.subTest(name=name):
                view = readiness.decision_view(snapshot(name, pending))
                self.assertEqual(view["needs_you"], required == "person")
                self.assertEqual(view["waiting_decisions"]["items"][0]["required"], required)

    def test_permission_summary_contains_no_decision_or_verifier_payload(self):
        data = snapshot("hands_off")
        data["permissions"] = [{"tool": "vault.read", "category": "secrets", "mode": "ask_each_time",
                                "decision": {"instruction": "private answer", "verifier": "private verifier"}}]
        before = deepcopy(data)
        view = readiness.decision_view(data)
        self.assertEqual(view["tool_permissions"], [{"tool": "vault.read", "category": "secrets", "mode": "ask_each_time", "changed_at": None}])
        self.assertNotIn("private", json.dumps(view))
        self.assertEqual(data, before)

    def test_objective_waits_use_objective_bounds_beyond_eight(self):
        questions = [question(str(index)) for index in range(12)]
        view = readiness.decision_view(snapshot("careful"), checkpoint(questions), objective=True)
        self.assertTrue(view["needs_you"])
        self.assertEqual(len(view["waiting_decisions"]["items"]), 12)
        self.assertEqual(view["waiting_decisions"]["total"], 12)
        self.assertFalse(view["waiting_decisions"]["truncated"])

    def test_old_opaque_or_unclassified_facts_are_unknown(self):
        for row in ("old question", {"id": "old", "resolved": False},
                    {"id": "old", "categories": ["secrets"]}):
            with self.subTest(row=row):
                observed = readiness.decision_view(snapshot("careful"), checkpoint([row]), objective=True)
                self.assertIsNone(observed["needs_you"])
                self.assertEqual(observed["waiting_decisions"]["items"][0]["waiting_on"], "unknown")
        unknown = readiness.decision_view(snapshot("hands_off"), checkpoint([question(category="unclassified")]),
                                          objective=True)
        self.assertTrue(unknown["needs_you"])

    def test_resolved_questions_clear_wait_and_contradictory_facts_are_unknown(self):
        resolved = question(state="resolved", resolved=True, answer="yes")
        view = readiness.decision_view(snapshot(), checkpoint([resolved]), objective=True)
        self.assertFalse(view["needs_you"])
        self.assertEqual(view["waiting_decisions"]["items"], [])
        conflicting = {**resolved, "state": "pending"}
        self.assertIsNone(readiness.decision_view(snapshot(), checkpoint([conflicting]), objective=True)["needs_you"])

    def test_missing_and_truncated_questions_never_claim_complete_absence(self):
        for stored in ({}, {"questions": "opaque"}, checkpoint([question(str(i)) for i in range(65)])):
            with self.subTest(stored_type=type(stored.get("questions")).__name__):
                view = readiness.decision_view(snapshot("hands_off"), stored, objective=True)
                self.assertIsNone(view["needs_you"])
                self.assertEqual(view["waiting_decisions"]["status"], "partial")
        bounded = readiness.decision_view(snapshot(), checkpoint([question(str(i)) for i in range(65)]), objective=True)
        self.assertEqual((len(bounded["waiting_decisions"]["items"]), bounded["waiting_decisions"]["total"],
                          bounded["waiting_decisions"]["truncated"]), (64, 65, True))

    def test_explicit_invalid_snapshot_fields_are_not_defaulted(self):
        for field in ("permissions", "pending", "generation"):
            data = {**snapshot(), field: None}
            view = readiness.decision_view(data)
            self.assertEqual(view["decision_policy"]["status"], "unavailable")
            self.assertIsNone(view["needs_you"])

    def test_wait_projection_byte_bound_has_explicit_truncation(self):
        questions = [question(str(index) + "x" * 500, reference="r" * 512,
                              categories=list(policy.CATEGORIES), resolved=False) for index in range(64)]
        view = readiness.decision_view(snapshot(), checkpoint(questions), objective=True)
        waiting = view["waiting_decisions"]
        self.assertTrue(waiting["truncated"])
        self.assertEqual(waiting["total"], 64)
        self.assertIn("items", waiting["missing"])
        self.assertLessEqual(len(json.dumps(waiting, ensure_ascii=False).encode()), QUESTION_BYTES)

    def test_readiness_wait_is_additive_and_preserves_finalization_and_ended_states(self):
        decisions = readiness.decision_view(snapshot("careful", [question()]))
        for state in readiness.STATES:
            block = {"state": state, "acts_next": "coordinator", "coordinator_needed": True,
                     "summary": "original", "finalization": {"remaining": ["closure"], "finished": False}}
            before = deepcopy(block)
            result = readiness.decision_readiness(block, decisions)
            self.assertEqual(result["finalization"], before["finalization"])
            self.assertEqual(result["state"], "waiting" if state in ("working", "waiting") else state)
            self.assertEqual(block, before)


class DecisionDisplayTests(unittest.TestCase):
    def project(self, state, terms=None):
        terminal_facts, trees = inventories(state)
        return display.projection(state, terms or terminal_facts, trees, {"version": "1.4.230"})

    def test_person_mark_appearance_and_clear_are_proposed_not_executed(self):
        state = state_fixture()
        state["checkpoint"].update(checkpoint())
        state["decision_input"] = snapshot("careful", [question()])
        worker = admit(state)
        worker_title = display.worker_title(state, worker)
        before = deepcopy(state)
        output = self.project(state)
        coordinator = output["rows"][1]
        self.assertEqual(coordinator["expected"]["title"], "Coordinator · needs you")
        self.assertEqual(coordinator["command"][3:7], ["--terminal", "owner", "--title", "Coordinator · needs you"])
        self.assertEqual(output["desktop"], "NOT_VERIFIED")
        self.assertEqual(state, before)
        terms, _ = inventories(state)
        terms["result"]["terminals"][0]["title"] = "Coordinator · needs you"
        state["decision_input"]["pending"] = []
        cleared = self.project(state, terms)["rows"][1]
        self.assertEqual((cleared["expected"]["title"], cleared["verdict"]), ("Coordinator", "differs"))
        self.assertEqual(display.worker_title(state, worker), worker_title)

    def test_host_owned_title_never_gets_title_or_provider_input_action(self):
        state = state_fixture()
        state["checkpoint"]["display"]["host_tab"] = True
        state["decision_input"] = snapshot("careful", [question()])
        terms, _ = inventories(state)
        terms["result"]["terminals"][0].update(title="Host title", agentIdentity="claude")
        output = self.project(state, terms)
        row = output["rows"][1]
        self.assertTrue(row["needs_you"])
        self.assertIn("Host owns", row["decision_limitation"])
        self.assertEqual((row["command"], row["session_rename"], row["verdict"]), (None, None, "host"))
        self.assertFalse(any("terminal" in action.get("command", []) for action in output["next_action"]))

    def test_missing_input_and_setup_offer_keep_old_title_with_explicit_facts(self):
        state = state_fixture()
        unavailable = self.project(state)
        self.assertIsNone(unavailable["needs_you"])
        self.assertEqual(unavailable["decision_policy"]["status"], "unavailable")
        self.assertEqual(unavailable["rows"][1]["expected"]["title"], "Coordinator")
        state["checkpoint"].update(checkpoint())
        state["decision_input"] = snapshot()
        offered = self.project(state)
        self.assertIsNotNone(offered["setup_question"])
        self.assertFalse(offered["needs_you"])
        self.assertEqual(offered["rows"][1]["expected"]["title"], "Coordinator")

    def test_structured_objective_person_and_agent_waits_drive_title(self):
        state = state_fixture()
        state["checkpoint"].update(checkpoint([question()]))
        for name, needs_you in (("careful", True), ("delegate", False), ("hands_off", False)):
            state["decision_input"] = snapshot(name)
            result = self.project(state)
            self.assertEqual(result["needs_you"], needs_you)
            self.assertEqual("needs you" in result["rows"][1]["expected"]["title"], needs_you)


class DecisionPublicStatusTests(KernelCase):
    def selected(self, preferences=None):
        with patch("pod.status.load_config", return_value=preferences or load(self.project)):
            return selection.StatusSelectionTests.status(self, "--objective", "objective")

    def test_public_status_without_objective_includes_setup_without_inventing_wait(self):
        observed = status(self.project, None, current_run_fn=lambda: {"run": None})
        self.assertFalse(observed["needs_you"])
        self.assertIsNotNone(observed["setup_question"])
        self.assertEqual(observed["tool_permissions"], [])

    def test_public_status_wait_updates_readiness_and_read_is_byte_preserving(self):
        self.intake(questions=[question()])
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        observed = self.selected()
        self.assertTrue(observed["needs_you"])
        self.assertEqual((observed["readiness"]["state"], observed["readiness"]["acts_next"]), ("waiting", "owner"))
        self.assertEqual(path.read_bytes(), before)
        out = StringIO()
        with redirect_stdout(out):
            render(observed)
        self.assertIn("Needs you", out.getvalue())

    def test_public_status_uses_raw_snapshot_policy_memory_pending_and_generation(self):
        self.intake(questions=[])
        data = snapshot("delegate", [question()])
        data["permissions"] = [{"tool": "vault.read", "category": "secrets", "mode": "ask_each_time",
                                "decision": {"instruction": "private memory answer"}}]
        preferences = {**load(self.project), "decision_policy": data["policy"],
                       "tool_permissions": data["permissions"], "pending_decisions": data["pending"],
                       "decision_generation": data["generation"]}
        observed = self.selected(preferences)
        self.assertEqual((observed["decision_policy"]["preset"], observed["decision_policy"]["generation"]),
                         ("delegate", 3))
        self.assertEqual(observed["waiting_decisions"]["items"][0]["required"], "trusted_agents")
        self.assertFalse(observed["needs_you"])
        self.assertNotIn("private memory", json.dumps(observed))

    def test_unreadable_record_and_invalid_config_keep_unknown_waits(self):
        from pod.errors import PodError
        self.intake()
        with patch("pod.ledger.read", side_effect=PodError("fixture_unreadable", "unavailable")):
            self.assertIsNone(self.selected()["needs_you"])
        invalid = self.selected({**load(self.project), "status": "invalid"})
        self.assertEqual(invalid["decision_policy"]["status"], "unavailable")
        self.assertIsNone(invalid["needs_you"])

    def test_public_status_sanitizes_original_rule_words_and_preserves_private_state(self):
        from tests.test_route_rules import rule
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_bytes())
        state["constraints"] = [{**rule(), "instruction": "private original rule request"}]
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        observed = self.selected()
        self.assertNotIn("private original", json.dumps(observed))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(observed["constraints"][0]["value"], state["constraints"][0]["value"])
        self.assertEqual(observed["constraints"][0]["provenance"], "user_direct")

    def test_agent_approval_channel_is_never_relabelled_by_public_sanitization(self):
        value = {"kind": "route_rule", "provenance": "agent_decision", "channel": "agent_session",
                 "instruction": "private", "request": "private", "value": {"roles": {}}}
        before = deepcopy(value)
        public = public_status(value)
        self.assertEqual((public["provenance"], public["channel"]), ("agent_decision", "agent_session"))
        self.assertNotIn("private", json.dumps(public))
        self.assertEqual(value, before)

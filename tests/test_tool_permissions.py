"""Pure memory/invocation checks; receipt consumption is integration-owned."""

import copy
import unittest

from pod import policy, tool_permissions as tools
from pod.errors import PodError
from tests.test_decision_policy import LOCAL, PERSON, answered


def memory(mode="ask_each_time", category="secrets"):
    return tools.remember([], "synthetic", category, mode,
                          {"provenance": "user_direct", "instruction": "Remember this mode"})


def revisions():
    return {"policy": "policy-revision", "memory": "memory-revision",
            "decision_generation": 2, "invocation": "unique-pending-use"}


def approval(category="secrets", spending=None, principal=None):
    return {"id": "unique-pending-use", "purpose": "operation", "tool": "synthetic",
            "category": category, "operation": "synthetic operation", "spending": spending,
            "revisions": revisions(), "state": "approved", "principal": principal or PERSON}


class ToolPermissionTests(unittest.TestCase):
    def evaluate(self, *, mode="ask_each_time", name="hands_off", category="secrets", **kwargs):
        return tools.check(answered(name), memory(mode, category), "synthetic", category,
                           "synthetic operation", **kwargs)

    def test_first_sensitive_use_asks_one_memory_question(self):
        for category in sorted(policy.SENSITIVE):
            with self.subTest(category=category):
                result = tools.check(answered(), [], "synthetic", category, "synthetic operation",
                                     spending="new_commitment" if category == "money" else None)
                self.assertEqual(result["status"], "permission_question")
                self.assertEqual(result["purpose"], "memory")
                self.assertEqual(result["choices"], ["ask_each_time"] if category == "money" else list(tools.MODES))
                if category == "money":
                    self.assertNotIn("hands-off", result["text"])
        result = tools.check(answered(), [], "synthetic", "secrets", "synthetic operation",
                             pending=approval(), revisions=revisions())
        self.assertEqual(result["status"], "permission_question")

    def test_remember_modes_and_exact_tool_category_replacement(self):
        original = memory()
        before = copy.deepcopy(original)
        updated = tools.remember(original, "synthetic", "secrets", "hands_off", {"instruction": "changed"})
        self.assertEqual(original, before)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["mode"], "hands_off")
        updated = tools.remember(updated, "synthetic", "irreversible_deletion", "ask_each_time", {"instruction": "other"})
        self.assertEqual(len(updated), 2)
        for tool, category in (("other", "secrets"), ("synthetic", "speaking_for_person")):
            self.assertEqual(tools.check(answered(), updated, tool, category, "operation")["status"], "permission_question")
        updated[0]["decision"]["instruction"] = "mutated copy"
        self.assertEqual(original, before)

    def test_stricter_policy_and_persistent_memory_intersection(self):
        rows = memory("hands_off")
        before = copy.deepcopy(rows)
        for name, status, decider in (("hands_off", "allowed", "any_agent"),
                                      ("delegate", "operation_question", "trusted_agents"),
                                      ("careful", "operation_question", "person")):
            with self.subTest(name=name):
                result = tools.check(answered(name), rows, "synthetic", "secrets", "operation")
                self.assertEqual((result["status"], result["required"]), (status, decider))
        self.assertEqual(rows, before)
        self.assertEqual(self.evaluate()["status"], "operation_question")
        self.assertEqual(self.evaluate(mode="hands_off")["status"], "allowed")
        self.assertFalse(self.evaluate(mode="hands_off")["consume_required"])

    def test_memory_choice_and_one_operation_approval_do_not_substitute(self):
        result = self.evaluate(pending={"purpose": "memory", "mode": "hands_off", "state": "approved"})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "memory_choice_is_not_operation_approval")
        grant = approval()
        before = copy.deepcopy((grant, revisions()))
        result = self.evaluate(pending=grant, revisions=revisions())
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(result["mode"], "one_operation")
        self.assertTrue(result["consume_required"])
        self.assertEqual(result["invocation"], grant["id"])
        self.assertEqual((grant, revisions()), before)
        # Repeated pure evaluation cannot consume: execution requires the receipt adapter.
        self.assertEqual(self.evaluate(pending=grant, revisions=revisions()), result)
        self.assertEqual(memory()[0]["mode"], "ask_each_time")

    def test_operation_and_unique_identity_are_bound(self):
        for field, changed in (("tool", "other"), ("category", "speaking_for_person"),
                               ("operation", "different operation"), ("id", "previous-invocation"),
                               ("spending", "new_commitment")):
            with self.subTest(field=field):
                result = self.evaluate(pending={**approval(), field: changed}, revisions=revisions())
                self.assertEqual(result["status"], "refused")
                self.assertEqual(result["reason"], "operation_binding_mismatch")
        live = {**revisions(), "invocation": "next-use-with-identical-operation-text"}
        self.assertEqual(self.evaluate(pending=approval(), revisions=live)["status"], "refused")

    def test_every_relevant_revision_and_generation_is_bound(self):
        for field, changed in (("policy", "new-policy"), ("memory", "new-memory"),
                               ("decision_generation", 3)):
            with self.subTest(field=field):
                result = self.evaluate(pending=approval(), revisions={**revisions(), field: changed})
                self.assertEqual(result["status"], "refused")
        self.assertEqual(self.evaluate(pending=approval())["reason"], "operation_revisions_required")
        for live in ({}, {**revisions(), "decision_generation": True},
                     {**revisions(), "decision_generation": -1}):
            with self.subTest(live=live), self.assertRaises(PodError):
                self.evaluate(pending=approval(), revisions=live)

    def test_current_policy_rechecks_prior_principal(self):
        local = approval(principal=LOCAL)
        self.assertEqual(self.evaluate(pending=local, revisions=revisions())["status"], "allowed")
        for name in ("delegate", "careful"):
            self.assertEqual(self.evaluate(name=name, pending=local, revisions=revisions())["status"], "refused")
        verified = {"kind": "agent", "channel": "relayed", "identity": "synthetic-agent",
                    "scope": {"categories": ["secrets"]}}
        self.assertEqual(self.evaluate(name="delegate", pending=approval(principal=verified), revisions=revisions())["status"], "allowed")
        out_of_scope = {**verified, "scope": {"categories": ["routine"]}}
        self.assertEqual(self.evaluate(pending=approval(principal=out_of_scope), revisions=revisions())["status"], "refused")

    def test_consumed_or_refused_use_cannot_grant_even_under_hands_off(self):
        for state in ("consumed", "refused"):
            result = self.evaluate(mode="hands_off", pending={**approval(), "state": state}, revisions=revisions())
            self.assertEqual(result["status"], "refused")
            self.assertEqual(result["reason"], "operation_" + state)

    def test_money_new_commitment_requires_person_every_time(self):
        for name in ("careful", "delegate", "hands_off"):
            with self.subTest(name=name):
                result = self.evaluate(name=name, category="money", spending="new_commitment")
                self.assertEqual((result["status"], result["required"]), ("operation_question", "person"))
                self.assertEqual(self.evaluate(name=name, category="money", spending="new_commitment",
                    pending=approval("money", "new_commitment"), revisions=revisions())["status"], "allowed")
                self.assertEqual(self.evaluate(name=name, category="money", spending="new_commitment",
                    pending=approval("money", "new_commitment", LOCAL), revisions=revisions())["status"], "refused")
        with self.assertRaises(PodError):
            memory("hands_off", "money")

    def test_included_subscription_is_automatic_without_memory(self):
        for name in ("careful", "delegate", "hands_off"):
            result = tools.check(answered(name), [], "synthetic", "money", "included usage",
                                 spending="included_subscription")
            self.assertEqual(result["status"], "allowed")
            self.assertEqual(result["categories"], ["routine"])
            self.assertFalse(result["consume_required"])
        for invalid in (None, "free", "normal"):
            with self.subTest(spending=invalid), self.assertRaises(PodError):
                self.evaluate(category="money", spending=invalid)
        with self.assertRaises(PodError):
            self.evaluate(spending="included_subscription")

    def test_memory_shape_limits_duplicates_and_wildcards(self):
        for tool in ("*", "synthetic?", "x" * 129, "", "x\x00", "\ud800"):
            with self.subTest(tool=repr(tool)), self.assertRaises(PodError):
                tools.remember([], tool, "secrets", "hands_off", {"instruction": "yes"})
        for rows in ([*memory(), *memory()], [{**memory()[0], "category": "routine"}],
                     [{**memory()[0], "mode": "forever"}], [{**memory()[0], "secret": "value"}],
                     [{**memory()[0], "decision": {"instruction": "x" * 4096}}], None):
            with self.subTest(rows=rows), self.assertRaises(PodError):
                tools.validate_memory(rows)
        rows = [{**memory()[0], "tool": f"synthetic-{index}"} for index in range(32)]
        self.assertEqual(len(tools.validate_memory(rows)), 32)
        self.assertEqual(len(tools.remember(rows, "synthetic-0", "secrets", "hands_off", {"instruction": "replace"})), 32)
        with self.assertRaises(PodError):
            tools.remember(rows, "synthetic-extra", "secrets", "hands_off", {"instruction": "add"})

    def test_operations_are_bounded_and_pending_shape_exact(self):
        for operation in ("", "x" * 257, "\ud800"):
            with self.subTest(operation=repr(operation)), self.assertRaises(PodError):
                tools.check(None, [], "synthetic", "secrets", operation)
        for extra in ({"verified": True}, {"consumed": False}):
            with self.subTest(extra=extra), self.assertRaises(PodError):
                self.evaluate(pending={**approval(), **extra}, revisions=revisions())


if __name__ == "__main__":
    unittest.main()

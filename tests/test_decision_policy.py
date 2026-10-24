"""Deterministic value-boundary tests, not host authorization evidence."""

import copy
import unittest

from pod import policy
from pod.errors import PodError


def answered(name="hands_off", categories=None):
    return {"preset": name, "categories": policy.preset(name, categories), "setup": "answered",
            "decision": {"provenance": "user_direct", "instruction": "Use this policy"}}


PERSON = {"kind": "person", "channel": "session"}
LOCAL = {"kind": "agent", "channel": "agent_session"}


class DecisionPolicyTests(unittest.TestCase):
    def test_preset_strictness_and_automatic_bookkeeping(self):
        presets = [policy.preset(name) for name in ("careful", "delegate", "hands_off")]
        for category in policy.CATEGORIES:
            levels = [policy.DECIDERS.index(value[category]) for value in presets]
            self.assertEqual(levels, sorted(levels, reverse=True), category)
        for category in policy.AUTOMATIC:
            self.assertTrue(all(value[category] == "any_agent" for value in presets))
            for decider in ("person", "trusted_agents"):
                with self.subTest(category=category, decider=decider), self.assertRaises(PodError):
                    policy.preset("custom", {**presets[0], category: decider})

    def test_all_presets_and_invariant_person_only(self):
        for name in ("careful", "delegate", "hands_off"):
            with self.subTest(name=name):
                record = answered(name)
                self.assertEqual(policy.validate(record), record)
                self.assertEqual(set(record["categories"]), set(policy.CATEGORIES))
                for category in policy.PERSON_ONLY:
                    self.assertEqual(policy.required(record, [category]), "person")
                    self.assertFalse(policy.permits(record, [category], LOCAL))
                    self.assertTrue(policy.permits(record, [category], PERSON))
        careful = answered("careful")
        for category in ("merge_publish", "scope_changes", "secrets", "coordinator_changes",
                         "speaking_for_person", "production_deployment", "irreversible_deletion"):
            self.assertEqual(policy.required(careful, [category]), "person")
        self.assertEqual(policy.required(careful, ["model_choices"]), "trusted_agents")
        for category in ("routine", "continuity"):
            self.assertEqual(policy.required(careful, [category]), "any_agent")

    def test_custom_complete_map_and_strictest_union(self):
        custom = policy.preset("careful")
        custom.update(secrets="trusted_agents", model_choices="any_agent")
        record = answered("custom", custom)
        self.assertEqual(policy.required(record, ["model_choices", "secrets"]), "trusted_agents")
        self.assertEqual(policy.required(record, ["money", "secrets"]), "person")
        for invalid in ({}, {**custom, "money": "any_agent"}, {**custom, "alien": "person"},
                        {**custom, "routine": "trusted"}):
            with self.subTest(invalid=invalid), self.assertRaises(PodError):
                policy.preset("custom", invalid)

    def test_missing_and_skip_are_distinct_and_read_only(self):
        current = policy.effective(None)
        self.assertEqual(current["setup"], "unasked")
        self.assertEqual(policy.effective(current), current)
        self.assertIsNot(policy.effective(current), current)
        self.assertIsNotNone(policy.projection(None, [], [])["setup_question"])
        skipped = {"preset": "careful", "categories": policy.preset("careful"), "setup": "skipped"}
        self.assertEqual(policy.validate(skipped), skipped)
        self.assertIsNone(policy.projection(skipped, [], [])["setup_question"])
        for record in (current, {**answered(), "setup": "skipped"},
                       {k: v for k, v in answered().items() if k != "decision"},
                       {**answered(), "categories": policy.preset("careful")}):
            with self.subTest(record=record), self.assertRaises(PodError):
                policy.validate(record)

    def test_complete_effect_union_cannot_be_laundered_by_labels(self):
        effect = {"action": "publish", "effects": [
            {"kind": "money", "effect": {"spending": "new_commitment"}},
            {"kind": "human_login", "effect": {}}]}
        expected = ["human_login", "merge_publish", "money"]
        self.assertEqual(policy.categories("authorization", effect), expected)
        self.assertEqual(policy.categories("question", {"effect": {"kind": "authorization", "effect": effect}}), expected)
        for assertion in ({"category": "routine"}, {"categories": ["merge_publish"]}):
            with self.subTest(assertion=assertion), self.assertRaises(PodError):
                policy.categories("authorization", {**effect, **assertion})
        self.assertEqual(policy.categories("authorization", {**effect, "categories": expected}), expected)

    def test_constraint_removals_and_standing_scopes_are_complete(self):
        effect = {"added": [{"kind": "only_agents", "value": ["agent"]}],
                  "removed": [{"kind": "money", "effect": {"spending": "new_commitment"}}]}
        self.assertEqual(policy.categories("constraints_update", effect), ["model_choices", "money"])
        self.assertEqual(policy.categories("constraint", {"removed": [{"kind": "opaque", "value": "unknown"}]}), ["unclassified"])
        self.assertEqual(policy.categories("standing_authorization", {"scopes": ["publish", "deployment",
            {"kind": "money", "effect": {"spending": "new_commitment"}}]}),
            ["merge_publish", "money", "production_deployment"])
        self.assertEqual(policy.categories("standing_authorization", {
            "scopes": ["publish", "merge", "release", "install", "cleanup"],
            "widening": ["deploy", "secrets", "unique_deletion", "scope_growth",
                         {"kind": "paid_resources", "effect": {"spending": "new_commitment"}}]}),
            ["irreversible_deletion", "merge_publish", "money", "production_deployment",
             "routine", "scope_changes", "secrets"])
        self.assertEqual(policy.categories("retirement", {"irreversible_deletion": True}),
                         ["irreversible_deletion", "scope_changes"])
        self.assertEqual(policy.categories("revision", {"effects": [{"kind": "secrets", "effect": {}}]}),
                         ["scope_changes", "secrets"])

    def test_unknown_is_explicit_person_under_every_preset(self):
        for kind, effect in (("alien", {}), ("question", {}), ("governor_exception", {}),
                             ("authorization", {"action": "opaque"}), ("constraint", {})):
            with self.subTest(kind=kind):
                values = policy.categories(kind, effect)
                self.assertEqual(values, ["unclassified"])
                for name in ("careful", "delegate", "hands_off"):
                    self.assertEqual(policy.required(answered(name), values), "person")
        with self.assertRaises(PodError):
            policy.required(answered(), [])
        with self.assertRaises(PodError):
            policy.preset("custom", {**policy.preset("careful"), "unclassified": "any_agent"})

    def test_spending_is_explicit_and_normal_subscription_is_routine(self):
        for invalid in (None, "normal", "free", False):
            with self.subTest(spending=invalid), self.assertRaises(PodError):
                policy.categories("money", {"spending": invalid})
        self.assertEqual(policy.categories("money", {"spending": "included_subscription"}), ["routine"])
        self.assertEqual(policy.categories("tool_permission", {"category": "money", "spending": "new_commitment"}), ["money"])
        with self.assertRaises(PodError):
            policy.categories("standing_authorization", {"scopes": ["paid_resources"]})
        with self.assertRaises(PodError):
            policy.categories("standing_authorization", {"scopes": ["publish"], "widening": ["paid_resources"]})

    def test_normal_bookkeeping_is_routine_under_careful(self):
        for kind, effect in (("closure", {}), ("pod_tick", {}), ("compaction", {}),
                             ("completed_cleanup", {"already_authorized": True, "completed": True})):
            self.assertEqual(policy.required(answered("careful"), policy.categories(kind, effect)), "any_agent")
        self.assertEqual(policy.categories("completed_cleanup", {"completed": True}), ["unclassified"])
        self.assertEqual(policy.categories("claim", {}), ["coordinator_changes", "scope_changes"])
        self.assertEqual(policy.categories("handoff", {}), ["coordinator_changes", "scope_changes"])

    def test_adapter_kind_matrix_and_underlying_exception_effect(self):
        groups = {
            "scope_changes": ("governance", "withdrawal", "retirement", "revision", "issue_revision", "reopen"),
            "model_choices": ("route_rule", "route_failure_clear", "only_agents", "only_models", "exclude_models", "role_model"),
            "policy_changes": ("policy", "policy_setup", "policy_change"),
            "continuity": ("continuity",), "human_login": ("human_login",),
        }
        for category, kinds in groups.items():
            for kind in kinds:
                with self.subTest(kind=kind):
                    self.assertEqual(policy.categories(kind, {}), [category])
        effect = {"effect": {"kind": "authorization", "effect": {"action": "deployment"}}}
        self.assertEqual(policy.categories("governor_exception", effect), ["production_deployment"])

    def test_local_and_attested_agent_evaluations_remain_distinct(self):
        trusted = {"kind": "agent", "channel": "relayed", "identity": "synthetic-agent",
                   "scope": {"categories": ["secrets", "model_choices"]}}
        self.assertFalse(policy.permits(answered("delegate"), ["secrets"], LOCAL))
        self.assertTrue(policy.permits(answered("hands_off"), ["secrets"], LOCAL))
        self.assertTrue(policy.permits(answered("delegate"), ["secrets"], trusted))
        self.assertFalse(policy.permits(answered("delegate"), ["scope_changes"], trusted))
        self.assertFalse(policy.permits(answered("careful"), ["secrets"], trusted))
        self.assertFalse(policy.permits(answered(), ["policy_changes"], trusted))
        for actor in ({"kind": "agent", "trusted": True}, {**LOCAL, "identity": "claimed"},
                      {**LOCAL, "scope": {"categories": ["secrets"]}},
                      {**LOCAL, "provenance": "user_direct"},
                      {"kind": "agent", "channel": "relayed"}, {"kind": "person"},
                      {**trusted, "scope": {"categories": ["routine"]}}):
            self.assertFalse(policy.permits(answered(), ["secrets"], actor))

    def test_projection_sanitizes_and_distinguishes_waits(self):
        pending = [{"id": "p1", "categories": ["money"], "state": "pending", "text": "private text"},
                   {"id": "p2", "category": "secrets", "state": "waiting", "operation": "private op"},
                   {"id": "p3", "categories": ["money"], "state": "answered"}]
        memory = [{"tool": "synthetic", "category": "secrets", "mode": "hands_off",
                   "decision": {"instruction": "private decision"}}]
        before = copy.deepcopy((pending, memory))
        view = policy.projection(answered("delegate"), memory, pending)
        self.assertTrue(view["waiting_on_person"])
        self.assertTrue(view["waiting_on_agent"])
        self.assertEqual(len(view["pending"]), 2)
        self.assertNotIn("private", str(view))
        self.assertEqual((pending, memory), before)
        self.assertEqual(policy.projection(answered(), [], [{"id": "p", "state": "pending"}])["pending"][0]["required"], "person")

    def test_bounded_invalid_values_fail_with_pod_error(self):
        for categories in ([], ["alien"], ["money", "money"], [None], "money"):
            with self.subTest(categories=categories), self.assertRaises(PodError):
                policy.required(None, categories)
        for decision in ({}, {"instruction": "x" * 4096}, {"value": float("nan")}):
            with self.subTest(decision=decision), self.assertRaises(PodError):
                policy.validate({**answered(), "decision": decision})
        chain = {"kind": "routine", "effect": {}}
        for _ in range(10):
            chain = {"kind": "question", "effect": {"effect": chain}}
        with self.assertRaises(PodError):
            policy.categories("question", {"effect": chain})
        with self.assertRaises(PodError):
            policy.projection(None, [], [{"id": str(i), "state": "pending"} for i in range(9)])


if __name__ == "__main__":
    unittest.main()

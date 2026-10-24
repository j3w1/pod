"""Pure selector evidence using synthetic snapshots, never native worker launches."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from pod.catalog import route_keys
from pod.errors import PodError
from pod.selection import allowed_routes, semantic_role, validate_choice, validate_constraint, validate_rule


SOL = "codex/gpt-6.1-sol/high"
LUNA = "codex/gpt-6-luna/xhigh"
OPUS = "claude/claude-opus-5-5/max"
SONNET = "claude/claude-sonnet-5-5/xhigh"


def snapshot(*enabled, pinned=None, preferred=None):
    return {"eligible": list(enabled), "routes": {key: "enabled" for key in enabled},
            "pinned": pinned, "preferred": preferred, "errors": [], "max_active": 2}


def rule(*, constraints=None, efforts=None, roles=None, fallback=None, identity="requested"):
    return {"id": identity, "kind": "route_rule", "provenance": "user_direct",
            "value": {"constraints": constraints or [], "efforts": efforts,
                      "roles": roles or {}, "fallback": fallback}}


def choice(key):
    agent, model, effort = key.split("/")
    return {"agent": agent, "model": model, "effort": effort, "context": "native_default",
            "reason": "synthetic selector case"}


def fallback_context(**changes):
    return {"admission": "prior-admission", "kind": "unavailable", "rule_id": "requested",
            "routing_role": "worker", "settlement": "settled", **changes}


def alternative(*, on=None, roles=None, routes=None):
    return {"roles": roles or ["worker"], "on": on or ["unavailable", "rate_limited"],
            "routes": routes or [SONNET]}


class RouteRuleTests(unittest.TestCase):
    def project(self, requested, *, current=None, role="worker", extra=(), fallback=None):
        return allowed_routes(current or snapshot(*route_keys()), [requested, *extra],
                              routing_role=role, fallback=fallback)

    def test_codex_only(self):
        requested = rule(constraints=[{"kind": "only_agents", "value": ["codex"]}])
        routes = self.project(requested)["routes"]
        self.assertTrue(routes)
        self.assertTrue(all(key.startswith("codex/") for key in routes))
        denied = validate_choice(snapshot(SOL, OPUS), [requested], [], choice(OPUS), routing_role="worker")
        self.assertEqual((denied["code"], denied["rule_id"], denied["predicate"]),
                         ("route_rule_excluded", "requested", "agent"))

    def test_claude_only(self):
        requested = rule(constraints=[{"kind": "only_agents", "value": ["claude"]}])
        routes = self.project(requested)["routes"]
        self.assertTrue(routes)
        self.assertTrue(all(key.startswith("claude/") for key in routes))

    def test_any_model_at_xhigh(self):
        routes = self.project(rule(efforts=["xhigh"]))["routes"]
        self.assertTrue(routes)
        self.assertTrue(all(key.endswith("/xhigh") for key in routes))
        self.assertIn(LUNA, routes)
        self.assertIn(SONNET, routes)

    def test_opus_max_coordinator_codex_workers(self):
        requested = rule(roles={"coordinator": [OPUS], "worker": {"only_agents": ["codex"]}})
        self.assertEqual(self.project(requested, role="coordinator")["routes"], [OPUS])
        worker = self.project(requested)["routes"]
        self.assertTrue(worker)
        self.assertTrue(all(key.startswith("codex/") for key in worker))
        with self.assertRaises(PodError):
            semantic_role("implement", [{"kind": "criterion"}], requested="coordinator", composite=True)

    def test_global_and_role_predicates_intersect(self):
        requested = rule(constraints=[{"kind": "only_agents", "value": ["codex"]},
                                      {"kind": "exclude_models", "value": ["gpt-6-luna"]}],
                         efforts=["high", "xhigh"], roles={"worker": {"efforts": ["xhigh"]}})
        self.assertEqual(self.project(requested, current=snapshot(SOL, LUNA, SONNET))["routes"], [])
        sol_xhigh = SOL.replace("/high", "/xhigh")
        self.assertEqual(self.project(requested, current=snapshot(SOL, sol_xhigh))["routes"], [sol_xhigh])

    def test_model_includes_and_nested_semantic_role_model(self):
        requested = rule(constraints=[{"kind": "only_models", "value": ["gpt-6.1-sol", "gpt-6-luna"]},
                                      {"kind": "role_model", "role": "worker", "value": "gpt-6-luna"}])
        self.assertEqual(self.project(requested, current=snapshot(SOL, LUNA, OPUS))["routes"], [LUNA])
        result = validate_choice(snapshot(SOL, LUNA), [requested], [], choice(SOL),
                                 role="unrelated free prose", routing_role="worker")
        self.assertEqual(result["predicate"], "model")

    def test_role_filters_share_the_full_agent_model_effort_vocabulary(self):
        requested = rule(roles={"worker": {"only_agents": ["codex"],
                                          "only_models": ["gpt-6.1-sol", "gpt-6-luna"],
                                          "exclude_models": ["gpt-6-luna"], "efforts": ["high"]}})
        self.assertEqual(self.project(requested, current=snapshot(SOL, LUNA, OPUS))["routes"], [SOL])

    def test_no_match_names_effort_and_never_relaxes(self):
        requested = rule(efforts=["xhigh"], roles={"worker": [SOL]})
        current = snapshot(SOL)
        projected = self.project(requested, current=current)
        self.assertEqual(projected["routes"], [])
        denied = validate_choice(current, [requested], [], choice(SOL), routing_role="worker")
        self.assertEqual((denied["rule_id"], denied["predicate"], denied["stage"]),
                         ("requested", "effort", "primary"))
        self.assertFalse(validate_choice(current, [requested], [], choice(SOL), routing_role="worker",
                                         fallback=fallback_context())["allowed"])

    def test_empty_pool_only_explicit_role_tuple_extends_it(self):
        self.assertEqual(self.project(rule(), current=snapshot())["routes"], [])
        broad = rule(roles={"worker": {"only_agents": ["codex"], "only_models": ["gpt-6.1-sol"]}})
        self.assertEqual(self.project(broad, current=snapshot())["routes"], [])
        exact = rule(roles={"worker": [SOL]})
        self.assertEqual(self.project(exact, current=snapshot())["routes"], [SOL])
        self.assertTrue(validate_choice(snapshot(), [exact], [], choice(SOL), routing_role="worker")["allowed"])

    def test_explicit_tuple_does_not_enable_other_efforts_or_edit_snapshot(self):
        current = snapshot(OPUS)
        current["routes"][SOL] = "disabled"
        before = deepcopy(current)
        requested = rule(roles={"worker": [SOL]})
        self.assertEqual(self.project(requested, current=current)["routes"], [SOL])
        self.assertEqual(current, before)
        self.assertFalse(validate_choice(current, [requested], [], choice(SOL.replace("/high", "/xhigh")),
                                         routing_role="worker")["allowed"])

    def test_indirect_exact_routes_cannot_expand_pool(self):
        for provenance in ("issue", "repository", "worker"):
            with self.subTest(provenance=provenance):
                requested = {**rule(roles={"worker": [SOL]}), "provenance": provenance}
                self.assertEqual(self.project(requested, current=snapshot())["routes"], [])

    def test_new_agent_provenance_alone_is_not_authority(self):
        for provenance, channel in (("agent_decision", "agent_session"), ("decision_request", "relayed")):
            with self.subTest(provenance=provenance), self.assertRaises(PodError):
                validate_constraint({**rule(roles={"worker": [SOL]}), "provenance": provenance,
                                     "channel": channel}, snapshot(), new=True)

    def test_excluded_pin_is_visible_but_inapplicable_and_preferred_is_filtered(self):
        requested = rule(constraints=[{"kind": "only_agents", "value": ["codex"]}])
        current = snapshot(SOL, LUNA, OPUS, pinned=OPUS, preferred=OPUS)
        projected = self.project(requested, current=current)
        self.assertEqual(projected["pinned_route"], OPUS)
        self.assertTrue(projected["pin_inapplicable"])
        self.assertIsNone(projected["preferred_route"])
        result = validate_choice(current, [requested], [], choice(SOL), routing_role="worker")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["pinned_route"], OPUS)
        self.assertTrue(result["pin_inapplicable"])
        current["preferred"] = LUNA
        self.assertEqual(self.project(requested, current=current)["preferred_route"], LUNA)

    def test_allowed_pin_stays_exact(self):
        requested = rule(efforts=["high", "xhigh"])
        current = snapshot(SOL, LUNA, pinned=SOL, preferred=LUNA)
        self.assertFalse(self.project(requested, current=current)["pin_inapplicable"])
        self.assertTrue(validate_choice(current, [requested], [], choice(SOL), routing_role="worker")["allowed"])
        self.assertEqual(validate_choice(current, [requested], [], choice(LUNA), routing_role="worker")["code"],
                         "pin_mismatch")

    def test_independent_repository_restriction_intersects_composite_even_under_pin(self):
        independent = {"id": "repository-rule", "kind": "only_models", "provenance": "repository",
                       "value": ["gpt-6-luna"]}
        requested = rule(roles={"worker": [SOL, LUNA]})
        current = snapshot(SOL, LUNA, pinned=SOL)
        projected = self.project(requested, current=current, extra=[independent])
        self.assertEqual(projected["routes"], [LUNA])
        self.assertTrue(projected["pin_inapplicable"])
        self.assertTrue(validate_choice(current, [requested, independent], [], choice(LUNA),
                                        routing_role="worker")["allowed"])

    def test_replaced_group_is_inert_and_unrelated_rows_remain(self):
        old = {**rule(roles={"worker": [OPUS]}, identity="old"), "active": False}
        replacement = rule(roles={"worker": [SOL, LUNA]}, identity="new")
        unrelated = {"kind": "exclude_models", "provenance": "issue", "value": ["gpt-6-luna"]}
        projected = allowed_routes(snapshot(), [old, replacement, unrelated], routing_role="worker")
        self.assertEqual(projected["routes"], [SOL])
        self.assertEqual(old["value"]["roles"]["worker"], [OPUS])
        with self.assertRaises(PodError):
            allowed_routes(snapshot(), [rule(identity="first"), replacement], routing_role="worker")

    def test_explicit_fallback_replaces_only_its_group_primary_predicates(self):
        requested = rule(constraints=[{"kind": "only_agents", "value": ["codex"]}], efforts=["high"],
                         roles={"worker": [SOL]}, fallback=[alternative()])
        current = snapshot(SOL, preferred=SONNET, pinned=SOL)
        projected = self.project(requested, current=current, fallback=fallback_context())
        self.assertEqual((projected["routes"], projected["stage"], projected["preferred_route"]),
                         ([SONNET], "fallback", SONNET))
        self.assertTrue(projected["pin_inapplicable"])
        self.assertTrue(validate_choice(current, [requested], [], choice(SONNET), routing_role="worker",
                                        fallback=fallback_context())["allowed"])
        independent = {"kind": "only_agents", "provenance": "repository", "value": ["codex"]}
        self.assertEqual(self.project(requested, current=current, extra=[independent],
                                      fallback=fallback_context())["routes"], [])

    def test_primary_cannot_use_fallback_routes_without_context(self):
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative()])
        denied = validate_choice(snapshot(SOL, SONNET), [requested], [], choice(SONNET), routing_role="worker")
        self.assertEqual((denied["code"], denied["stage"]), ("route_rule_excluded", "primary"))

    def test_fallback_wrong_rule_role_failure_or_unknown_settlement_refuses(self):
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative()])
        changes = ({"rule_id": "old"}, {"routing_role": "auditor"}, {"kind": "auth_failed"},
                   {"kind": "safety_refusal"}, {"kind": "permission"}, {"kind": "unknown"},
                   {"settlement": "unknown"}, {"admission": ""}, {"verified": True})
        for change in changes:
            with self.subTest(fields=list(change)):
                projected = self.project(requested, fallback=fallback_context(**change))
                self.assertEqual(projected["routes"], [])
                self.assertIn({"rule_id": "requested", "predicate": "stage"}, projected["unmet"])
        self.assertEqual(self.project(requested, role="auditor",
                                      fallback=fallback_context(routing_role="auditor"))["routes"], [])
        self.assertEqual(allowed_routes(snapshot(SONNET), [], routing_role="worker",
                                        fallback=fallback_context())["routes"], [])

    def test_fallback_on_is_finite_and_no_start_is_supported(self):
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative(on=["rate_limited"])])
        self.assertEqual(self.project(requested, fallback=fallback_context())["routes"], [])
        self.assertEqual(self.project(requested, fallback=fallback_context(kind="rate_limited", settlement="no_start"))
                         ["routes"], [SONNET])

    def test_fallback_pin_if_allowed_stays_exact(self):
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative(routes=[SONNET, OPUS])])
        current = snapshot(SOL, SONNET, OPUS, pinned=SONNET)
        result = validate_choice(current, [requested], [], choice(OPUS), routing_role="worker",
                                 fallback=fallback_context())
        self.assertEqual(result["code"], "pin_mismatch")

    def test_fallback_keeps_rate_auth_and_safety_protections(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative()])
        for kind in ("rate_limited", "unavailable", "auth_failed"):
            failure = {"kind": kind, "model": "claude-sonnet-5-5", "task": "task",
                       "retry_after": (now + timedelta(minutes=1)).isoformat()}
            with self.subTest(kind=kind):
                result = validate_choice(snapshot(SOL), [requested], [failure], choice(SONNET), task="task",
                                         routing_role="worker", fallback=fallback_context(), now=now)
                self.assertEqual(result["code"], "route_failed")
                self.assertEqual((result["rule_id"], result["predicate"], result["stage"]),
                                 ("requested", "stage", "fallback"))
        safety = {"kind": "safety_refusal", "model": "gpt-6.1-sol", "task": "task"}
        result = validate_choice(snapshot(SOL), [requested], [safety], choice(SONNET), task="task",
                                 routing_role="worker", fallback=fallback_context(), now=now)
        self.assertEqual(result["code"], "safety_refusal")

    def test_fallback_cannot_escape_model_suppression_by_changing_effort(self):
        other_effort = SOL.replace("/high", "/xhigh")
        requested = rule(roles={"worker": [SOL]}, fallback=[alternative(routes=[other_effort])])
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        failure = {"kind": "rate_limited", "model": "gpt-6.1-sol", "task": "task",
                   "retry_after": (now + timedelta(minutes=1)).isoformat()}
        result = validate_choice(snapshot(SOL), [requested], [failure], choice(other_effort), task="task",
                                 routing_role="worker", fallback=fallback_context(kind="rate_limited"), now=now)
        self.assertEqual(result["code"], "route_failed")

    def test_fallback_with_no_surviving_supported_alternative_waits(self):
        requested = rule(roles={"worker": [SOL]},
                         fallback=[alternative(routes=["codex/gpt-6-sol/high"])])
        self.assertEqual(self.project(requested, fallback=fallback_context())["routes"], [])
        strict = rule(roles={"worker": [SOL]}, fallback=[])
        self.assertEqual(self.project(strict, fallback=fallback_context())["routes"], [])

    def test_missing_semantic_role_and_invalid_preferences_do_not_expand(self):
        requested = rule(roles={"worker": [SOL]})
        self.assertEqual(self.project(requested, role=None)["routes"], [])
        current = snapshot()
        current["errors"] = [{"code": "config_missing"}]
        self.assertEqual(self.project(requested, current=current)["routes"], [])
        self.assertEqual(validate_choice(current, [requested], [], choice(SOL), routing_role="worker")["code"],
                         "preferences_unavailable")

    def test_historical_exact_tuple_is_readable_but_not_routable(self):
        requested = rule(roles={"worker": ["codex/gpt-6-sol/high"]})
        self.assertEqual(validate_rule(requested["value"], stored=True), requested["value"])
        with self.assertRaises(PodError):
            validate_rule(requested["value"])
        self.assertEqual(self.project(requested)["routes"], [])

    def test_bad_rule_shapes_and_nonfinite_fallback_refuse(self):
        base = rule()["value"]
        bad_values = [None, {}, {**base, "extra": True}, {**base, "efforts": []},
                      {**base, "efforts": ["high", "high"]}, {**base, "efforts": ["ultra"]},
                      {**base, "roles": {"worker": []}}, {**base, "roles": {"worker": {}}},
                      {**base, "roles": {"review": [SOL]}}, {**base, "roles": {"worker": [SOL, SOL]}},
                      {**base, "roles": {"worker": ["codex/gpt-6.1-sol/ultra"]}},
                      {**base, "constraints": [{"kind": "allow_disabled", "value": SOL}]},
                      {**base, "constraints": [{"kind": "only_agents", "value": ["codex"], "approval": True}]},
                      {**base, "constraints": [{"kind": "role_model", "role": "free prose", "value": "gpt-6.1-sol"}]},
                      {**base, "fallback": True}, {**base, "fallback": [alternative()] * 6},
                      {**base, "fallback": [alternative(), alternative()]},
                      {**base, "fallback": [alternative(on=["auth_failed"])]},
                      {**base, "fallback": [{**alternative(), "routes": []}]},
                      {**base, "fallback": [{**alternative(), "relax": True}]}]
        for value in bad_values:
            with self.subTest(index=bad_values.index(value)), self.assertRaises(PodError):
                validate_rule(value)

    def test_standalone_legacy_role_prose_and_pin_precedence(self):
        standalone = {"kind": "role_model", "provenance": "repository", "role": "review this change",
                      "value": "gpt-6-luna"}
        current = snapshot(SOL, LUNA)
        self.assertTrue(validate_choice(current, [standalone], [], choice(SOL), routing_role="reviewer")["allowed"])
        self.assertEqual(validate_choice(current, [standalone], [], choice(SOL), role="review this change")["code"],
                         "constraint_excluded")
        current["pinned"] = SOL
        self.assertTrue(validate_choice(current, [standalone], [], choice(SOL), role="review this change")["allowed"])
        # Under a composite, independent standalone rows still consume responsibility prose.
        result = validate_choice(current, [rule(), standalone], [], choice(SOL), role="review this change",
                                 routing_role="reviewer")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["predicate"], "model")
        projected = allowed_routes(current, [rule(), standalone], routing_role="reviewer", role="review this change")
        self.assertEqual(projected["routes"], [LUNA])
        self.assertTrue(projected["pin_inapplicable"])


class SemanticRoleTests(unittest.TestCase):
    def test_worker_and_correction_ancestry(self):
        rows = [{"kind": "criterion"}]
        for packet_role in ("implement", "investigate"):
            self.assertEqual(semantic_role(packet_role, rows, composite=True), "worker")
        correction = [*rows, {"kind": "correction"}]
        self.assertEqual(semantic_role("implement", correction, requested="fixer", composite=True), "fixer")
        for requested in (None, "worker", "reviewer", "auditor", "coordinator"):
            with self.subTest(requested=requested), self.assertRaises(PodError):
                semantic_role("implement", correction, requested=requested, composite=True)
        with self.assertRaises(PodError):
            semantic_role("implement", rows, requested="fixer", composite=True)

    def test_review_and_final_audit_are_bound_to_assurances(self):
        for designation in ("reviewer", "auditor"):
            rows = [{"kind": "assurance", "audit_role": designation}]
            self.assertEqual(semantic_role("review", rows, requested=designation, composite=True), designation)
            for wrong in (None, "worker", "fixer", "coordinator", "auditor" if designation == "reviewer" else "reviewer"):
                with self.subTest(designation=designation, requested=wrong), self.assertRaises(PodError):
                    semantic_role("review", rows, requested=wrong, composite=True)

    def test_missing_conflicting_or_untyped_assurances_refuse(self):
        for rows in ([], [{"kind": "assurance"}], [{"kind": "criterion", "audit_role": "auditor"}],
                     [{"kind": "assurance", "audit_role": []}],
                     [{"kind": "assurance", "audit_role": "reviewer"},
                      {"kind": "assurance", "audit_role": "auditor"}]):
            with self.subTest(rows=rows), self.assertRaises(PodError):
                semantic_role("review", rows, requested="auditor", composite=True)

    def test_legacy_packets_do_not_acquire_composite_prerequisites(self):
        self.assertEqual(semantic_role("review", []), "reviewer")
        self.assertEqual(semantic_role("implement", [{"kind": "correction"}]), "worker")

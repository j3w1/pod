"""Model constraints use the real mutation's decision capability."""

from copy import deepcopy
from unittest.mock import patch

from pod import config, ledger, policy, relay
from pod.catalog import parse_route_key
from pod.errors import PodError
from pod.internal import run
from pod.selection import active_constraints, allowed_routes, validate_choice, validate_constraint
from tests.decision_policy_support import LOCAL, PERSON, RELAY, save_vouch
from tests.test_verified_decisions import VerifiedObjectiveCase


MODEL = "gpt-6.1-sol"
OTHER_MODEL = "gpt-6-astra"
ROLE = "review the completed implementation carefully"
NARROWING = (
    {"kind": "only_agents", "value": ["codex"]},
    {"kind": "only_models", "value": [MODEL]},
    {"kind": "exclude_models", "value": [OTHER_MODEL]},
    {"kind": "role_model", "value": MODEL, "role": ROLE},
)


class ConstraintDecisionChannelTests(VerifiedObjectiveCase):
    def rows(self, authority=RELAY, *, prefix="narrow"):
        return [{"id": prefix + "-" + row["kind"], **deepcopy(row), **deepcopy(authority)}
                for row in NARROWING]

    def add(self, value):
        return run("constraint", {"project": str(self.project), "objective": "objective",
                                  "owner": "owner", "action": "add", "value": value})

    def revoke(self, identity, authority=RELAY):
        return run("constraint", {"project": str(self.project), "objective": "objective",
                                  "owner": "owner", "action": "revoke", "id": identity,
                                  "decision": authority})

    def receipts(self):
        return {path.name: path.read_bytes()
                for path in (ledger.state_root(None) / ".relay-authorizations").glob("*.json")}

    def refused(self, value, code):
        before, receipts = self.before(), self.receipts()
        with self.assertRaises(PodError) as caught:
            self.add(value)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.before(), before)
        self.assertEqual(self.receipts(), receipts)

    def assert_readback(self, expected):
        before, receipts = self.before(), self.receipts()
        with patch.object(relay, "verify_decision", side_effect=AssertionError("host read")):
            stored = ledger.read(self.project, "objective")
            listed = run("constraint", {"project": str(self.project), "objective": "objective",
                                        "owner": "owner", "action": "list"})
            self.assertEqual(stored["constraints"], expected)
            self.assertEqual(listed["constraints"], expected)
            self.assertEqual(active_constraints(expected, config.load(self.project)),
                             [row for row in expected if row.get("active", True)])
        self.assertEqual(self.before(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_verified_agent_all_four_kinds_without_recorded_question(self):
        self.configure_verifier(kind="agent")
        self.assertEqual(ledger.read(self.project, "objective")["checkpoint"]["questions"], [])
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                result = self.add(value)
                stored = result["constraints"][-1]
                self.assertEqual(stored["kind"], value["kind"])
                self.assertEqual(stored["value"], value["value"])
                self.assertEqual(stored["provenance"], "agent_decision")
                self.assertEqual(stored["channel"], "relayed")
                self.assertEqual(stored["instruction"], value["instruction"])
                self.assertEqual(stored["actor"], {
                    **stored["decision_verification"]["principal"], "channel": "relayed"})
                self.assertEqual(stored["actor"]["kind"], "agent")
                self.assertEqual(stored["relay"], value["relay"])
                self.assertEqual(stored["decided_at"], "2026-10-01T00:00:00Z")
                self.assertEqual(stored["decision_binding"]["kind"], "constraint")
                self.assertEqual(stored["decision_binding"]["categories"], ["model_choices"])
                self.assertEqual(stored["decision_verification"]["decision"], stored["decision_binding"])
                self.assertEqual(ledger.read(self.project, "objective")["decision_channels"], 5)
                self.assert_readback(result["constraints"])

    def test_local_agent_all_four_kinds_remain_cooperative(self):
        self.approve_scope()
        with patch.object(relay, "verify_decision", side_effect=AssertionError("local host call")):
            for value in self.rows(LOCAL):
                with self.subTest(kind=value["kind"]):
                    result = self.add(value)
                    stored = result["constraints"][-1]
                    self.assertEqual(stored["actor"], {"kind": "agent", "channel": "agent_session"})
                    self.assertEqual(stored["provenance"], "agent_decision")
                    self.assertEqual(stored["channel"], "agent_session")
                    self.assertEqual(stored["decided_at"], stored["recorded_at"])
                    self.assertNotIn("decision_verification", stored)
                    self.assertNotIn("relay", stored)
                    self.assert_readback(result["constraints"])

    def test_verified_person_all_four_kinds_preserve_person_origin(self):
        config.set_decision_policy(self.project, "custom", decision=PERSON,
                                   categories={**policy.preset("careful"), "model_choices": "person"})
        self.configure_verifier(kind="person")
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                result = self.add(value)
                stored = result["constraints"][-1]
                self.assertEqual(stored["provenance"], "user_direct")
                self.assertEqual(stored["actor"]["kind"], "person")
                self.assertEqual(stored["channel"], "relayed")
                self.assert_readback(result["constraints"])

    def test_session_person_and_indirect_narrowing_remain_unchanged(self):
        for provenance in ("issue", "repository", "worker"):
            for value in self.rows({"provenance": provenance}, prefix=provenance):
                with self.subTest(kind=value["kind"], provenance=provenance):
                    self.assertEqual(self.add(value)["constraints"][-1], value)
        for value in self.rows(PERSON):
            stored = self.add(value)["constraints"][-1]
            self.assertEqual(stored["provenance"], "user_direct")
            self.assertEqual(stored["channel"], "session")
        self.assert_readback(ledger.read(self.project, "objective")["constraints"])

    def test_session_person_revocation_preserves_original_source_and_unrelated_rows(self):
        originals = self.rows({"provenance": "issue"})
        for value in originals:
            self.add(value)
        for value in originals:
            with self.subTest(kind=value["kind"]):
                before = ledger.read(self.project, "objective")["constraints"]
                result = self.revoke(value["id"], PERSON)
                after = {row["id"]: row for row in result["constraints"]}
                self.assertEqual(after[value["id"]], {**value, "active": False})
                for row in before:
                    if row["id"] != value["id"]:
                        self.assertEqual(after[row["id"]], row)
                stop = result["constraints"][-1]
                self.assertEqual(stop["kind"], value["kind"])
                self.assertEqual(stop["value"], value["value"])
                self.assertEqual(stop["provenance"], "user_direct")
                self.assertEqual(stop["actor"]["kind"], "person")
                self.assertFalse(stop["active"])
                if "role" in value:
                    self.assertEqual(stop["role"], value["role"])
                self.assert_readback(result["constraints"])

    def test_verified_agent_revocation_preserves_actual_original_actor(self):
        self.configure_verifier(kind="agent")
        for value in self.rows():
            self.add(value)
        originals = ledger.read(self.project, "objective")["constraints"]
        self.configure_verifier(kind="agent", changes={"decided_at": "2026-10-02T00:00:00Z"})
        for original in originals:
            with self.subTest(kind=original["kind"]):
                result = self.revoke(original["id"])
                after = {row["id"]: row for row in result["constraints"]}
                self.assertEqual(after[original["id"]], {**original, "active": False})
                stop = result["constraints"][-1]
                self.assertEqual(stop["actor"]["kind"], "agent")
                self.assertEqual(stop["provenance"], "agent_decision")
                self.assertEqual(stop["decided_at"], "2026-10-02T00:00:00Z")
                self.assert_readback(result["constraints"])

    def test_unknown_original_chronology_still_needs_person_resolution(self):
        for value in self.rows({"provenance": "issue"}):
            self.add(value)
            for actor in ("agent", "person"):
                self.configure_verifier(kind=actor)
                before, receipts = self.before(), self.receipts()
                with self.subTest(kind=value["kind"], actor=actor), self.assertRaises(PodError) as caught:
                    self.revoke(value["id"])
                self.assertEqual(caught.exception.code, "decision_order_unknown")
                self.assertEqual(self.before(), before)
                self.assertEqual(self.receipts(), receipts)

    def test_approved_narrowing_preserves_independent_rows_and_requested_rule(self):
        self.configure_verifier(kind="agent")
        independent = [{"id": "independent-" + provenance, "kind": "only_agents",
                        "provenance": provenance, "value": ["codex"]}
                       for provenance in ("issue", "repository", "worker")]
        for value in independent:
            self.add(value)
        self.add({"id": "requested", "kind": "route_rule", "value": {
            "constraints": [], "efforts": None, "roles": {}, "fallback": None}, **RELAY})
        before = ledger.read(self.project, "objective")["constraints"]
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                result = self.add(value)
                self.assertEqual(result["constraints"][:len(before)], before)
        self.assert_readback(result["constraints"])

    def test_missing_verifier_refuses_all_four_without_effects(self):
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "relay_verifier_unconfigured")

    def test_verifier_denial_refuses_all_four_without_effects(self):
        self.configure_verifier(changes={"verified": False})
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "relay_verifier_denied")

    def test_person_only_policy_refuses_verified_agents_for_all_four(self):
        config.set_decision_policy(self.project, "custom", decision=PERSON,
                                   categories={**policy.preset("careful"), "model_choices": "person"})
        self.configure_verifier(kind="agent")
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "decision_policy_denied")

    def test_local_agent_cannot_cross_careful_or_delegate_boundary(self):
        for name in ("careful", "delegate"):
            self.approve_scope(name)
            for value in self.rows(LOCAL):
                with self.subTest(kind=value["kind"], policy=name):
                    self.refused(value, "decision_policy_denied")

    def test_wrong_verifier_kind_scope_refuses_all_four(self):
        self.configure_verifier(changes={"principal.scope.kinds": ["revision"]})
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "relay_verifier_mismatch")

    def test_wrong_verifier_category_scope_refuses_all_four(self):
        self.configure_verifier(changes={"principal.scope.categories": ["routine"]})
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "relay_verifier_mismatch")

    def test_wrong_verifier_resource_scope_refuses_all_four(self):
        self.configure_verifier(changes={"principal.scope.resources": {"objective": "different"}})
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                self.refused(value, "relay_verifier_mismatch")

    def test_saved_verdict_cannot_approve_changed_payload(self):
        self.configure_verifier()
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                save_vouch(self, lambda: self.add(value))
                changed = deepcopy(value)
                changed["value"] = (["claude"] if value["kind"] == "only_agents" else
                                    OTHER_MODEL if value["kind"] == "role_model" else
                                    [MODEL] if value["kind"] == "exclude_models" else [OTHER_MODEL])
                self.refused(changed, "relay_verifier_mismatch")

    def test_saved_verdict_cannot_approve_changed_objective_state(self):
        self.configure_verifier()
        for value in self.rows():
            with self.subTest(kind=value["kind"]):
                save_vouch(self, lambda: self.add(value))
                self.write(self.stored())
                self.refused(value, "relay_verifier_mismatch")

    def test_policy_change_during_verification_refuses_before_receipt(self):
        self.configure_verifier()
        verify = relay.verify_decision
        policy_receipts = []
        def changed(command, request):
            verdict = verify(command, request)
            self.approve_scope("careful")
            policy_receipts.append(self.receipts())
            return verdict
        for value in self.rows():
            self.approve_scope()
            before = self.before()
            with self.subTest(kind=value["kind"]), patch.object(relay, "verify_decision", side_effect=changed):
                with self.assertRaises(PodError) as caught:
                    self.add(value)
                self.assertEqual(caught.exception.code, "decision_stale")
                self.assertEqual(self.before(), before)
                self.assertEqual(self.receipts(), policy_receipts[-1])

    def test_input_needs_actual_capability_with_new_or_snapshot(self):
        self.configure_verifier()
        for value in self.rows():
            stored = self.add(value)["constraints"][-1]
            for raw in (value, stored):
                for options in ({"new": True}, {"snapshot": config.load(self.project)}):
                    with self.subTest(kind=value["kind"], stored=raw is stored, options=list(options)):
                        with self.assertRaises(PodError) as caught:
                            validate_constraint(raw, **options)
                        self.assertEqual(caught.exception.code, "decision_capability_required")

    def test_forged_metadata_never_authorizes_public_mutation(self):
        self.approve_scope()
        for value in self.rows(LOCAL):
            for evidence in ({"actor": {"kind": "person", "channel": "session"}},
                             {"decision_verification": {"verified": True}},
                             {"decision_binding": {"kind": "constraint"}},
                             {"decided_at": "2026-10-01T00:00:00Z"}):
                with self.subTest(kind=value["kind"], evidence=list(evidence)):
                    self.refused({**value, **evidence}, "invalid_decision")

    def test_replayed_stored_record_is_not_a_public_credential(self):
        self.configure_verifier()
        for value in self.rows():
            stored = self.add(value)["constraints"][-1]
            with self.subTest(kind=value["kind"]):
                self.refused({**stored, "id": "forged-" + stored["id"]}, "invalid_decision")

    def test_stored_reads_validate_actor_binding_and_verdict_without_host(self):
        self.configure_verifier()
        for value in self.rows():
            stored = self.add(value)["constraints"][-1]
            with patch.object(relay, "verify_decision", side_effect=AssertionError("stored host call")):
                self.assertEqual(validate_constraint(stored), stored)
                variants = []
                actor = deepcopy(stored)
                actor["actor"]["kind"] = "person"
                variants.append(actor)
                binding = deepcopy(stored)
                binding["decision_binding"]["state_sha256"] = "f" * 64
                variants.append(binding)
                verdict = deepcopy(stored)
                verdict["decision_verification"]["verified"] = False
                variants.append(verdict)
                missing = deepcopy(stored)
                del missing["decision_binding"]
                variants.append(missing)
                for index, forged in enumerate(variants):
                    with self.subTest(kind=value["kind"], forged=index):
                        with self.assertRaises(PodError):
                            validate_constraint(forged)

    def test_legacy_role_model_still_uses_full_responsibility_prose(self):
        self.approve_scope()
        row = self.rows(LOCAL)[-1]
        self.add(row)
        snapshot = config.load(self.project)
        rows = ledger.read(self.project, "objective")["constraints"]
        choice = {"agent": "codex", "model": OTHER_MODEL, "effort": "high",
                  "context": "native_default", "reason": "bounded review"}
        self.assertEqual(validate_choice(snapshot, rows, [], choice, role=ROLE)["code"], "constraint_excluded")
        self.assertTrue(validate_choice(snapshot, rows, [], choice, role="reviewer")["allowed"])
        projected = allowed_routes(snapshot, rows, routing_role="reviewer", role=ROLE)
        self.assertTrue(projected["routes"])
        self.assertTrue(all(parse_route_key(route)["model"] == MODEL for route in projected["routes"]))

    def test_new_actor_channels_do_not_admit_other_constraint_kinds(self):
        self.approve_scope()
        self.configure_verifier(kind="person")
        others = ({"kind": "max_workers", "value": 1},
                  {"kind": "allow_disabled", "value": "codex/gpt-6-luna/low"},
                  {"kind": "allow_delegation", "value": True},
                  {"kind": "keep_worker_tab", "value": {"objective": "objective", "dispatch": "fixture-dispatch",
                                                        "terminal": "fixture-terminal", "keep": True}},
                  {"kind": "unknown", "value": True})
        for item in others:
            for authority in (LOCAL, RELAY):
                value = {"id": "unrelated-" + item["kind"], **item, **authority}
                with self.subTest(kind=item["kind"], channel=authority["channel"]):
                    with self.assertRaises(PodError):
                        validate_constraint(value, new=True)
        snapshot = config.load(self.project)
        with self.assertRaises(PodError) as caught:
            validate_constraint({"kind": "max_workers", "value": snapshot["max_active"] + 1,
                                 "provenance": "worker"}, snapshot)
        self.assertEqual(caught.exception.code, "constraint_authority")

"""Public decision views over complete synthetic private authority records."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pod import cli, config, internal, ledger, relay
from pod.errors import PodError
from pod.status import public_status, recorded_questions
from pod.util import context_decisions, digest, map_decisions
from tests.decision_policy_support import MachineCase, PERSON, RELAY, VERIFIER, save_vouch
from tests.issue41_support import ProductionCase
from tests.test_route_rules import rule


INSTRUCTION = "PRIV_I_"
PRINCIPAL = "PRIV_P_"
SCOPE = "PRIV_S_"
REFERENCE = "PRIV_R_"
USE_KEY = "PRIV_U_"
MARKERS = (INSTRUCTION, PRINCIPAL, SCOPE, REFERENCE, USE_KEY)


class PublicDecisionPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build the expensive accepted authority fixture once; every public read
        # below uses the real commands and the same immutable private candidate.
        case = cls.case = ProductionCase()
        cls.addClassCleanup(case.doCleanups)
        case.setUp()
        case.intake()
        config.set_decision_policy(case.project, "delegate", decision={**PERSON, "instruction": INSTRUCTION + "policy"})
        case.controls = case.root / "privacy-controls.json"
        case.controls.write_text("{}")
        case.verifier = case.root / "privacy-verifier.py"
        case.verifier.write_text(VERIFIER)
        MachineCase.configure_verifier(case)

        def accept(kind, operation):
            authority = {**RELAY, "instruction": INSTRUCTION + kind,
                         "relay": {"reference": REFERENCE + kind, "verification": "decision"}}
            fixed = save_vouch(case, lambda: operation(authority))
            fixed["principal"]["identity"] = PRINCIPAL
            fixed["principal"]["scope"]["kinds"] = [*fixed["principal"]["scope"]["kinds"], SCOPE]
            fixed["single_use"] = USE_KEY + kind
            case.controls.write_text(json.dumps({"saved_verdict": fixed}))
            operation(authority)

        accept("rule", lambda authority: internal.run("constraint", {
            "project": str(case.project), "objective": "objective", "owner": "owner", "action": "add",
            "value": {**rule(), **authority}}))
        accept("constraint", lambda authority: internal.run("constraint", {
            "project": str(case.project), "objective": "objective", "owner": "owner", "action": "add",
            "value": {"id": "narrow", "kind": "only_agents", "value": ["codex"], **authority}}))
        revised = case.stored()
        revised[0]["check"] = "A revised exact fixture check"
        accept("revision", lambda authority: case.write(revised, revision_authority=authority))
        cls.memory = ledger.tool_decision(case.project, "objective", action="check", tool="fixture-tool",
                                          category="secrets", operation="read")
        accept("memory", lambda authority: ledger.tool_decision(
            case.project, "objective", action="answer", invocation=cls.memory["invocation"],
            answer="ask_each_time", decision=authority))
        cls.operation = ledger.tool_decision(case.project, "objective", action="check", tool="fixture-tool",
                                             category="secrets", operation="read")
        accept("operation", lambda authority: ledger.tool_decision(
            case.project, "objective", action="answer", invocation=cls.operation["invocation"],
            answer="approve", decision=authority))
        cls.memory = ledger.tool_decision(case.project, "objective", action="check", tool="fixture-memory",
                                          category="secrets", operation="read")
        accept("second-memory", lambda authority: ledger.tool_decision(
            case.project, "objective", action="answer", invocation=cls.memory["invocation"],
            answer="ask_each_time", decision=authority))
        from pod.standing import scope_digest
        material = scope_digest(ledger.read(case.project, "objective"))
        accept("standing", lambda authority: internal.run("standing-authorization", {
            "project": str(case.project), "objective": "objective", "owner": "owner", "action": "activate",
            "decision": {**authority, "scope_digest": material}}))
        cls.before = cls.private_bytes()

    @classmethod
    def private_bytes(cls):
        case = cls.case
        return {str(path): path.read_bytes() for path in (
            ledger.objective_root(case.project, "objective") / "context.json", config.personal_path(),
            *(ledger.state_root(None) / ".relay-authorizations").glob("*.json"))}

    def tearDown(self):
        self.assertEqual(self.private_bytes(), self.before)

    def command(self, *arguments):
        output = StringIO()
        with patch.object(Path, "cwd", return_value=self.case.project), \
             patch.object(relay, "verify_decision", side_effect=AssertionError("public read verified authority")), \
             redirect_stdout(output):
            self.assertEqual(cli.main(list(arguments)), 0, output.getvalue())
        return output.getvalue()

    def assert_private_absent(self, output):
        for marker in MARKERS:
            self.assertNotIn(marker, output)
        for field in ("decision_verification", "decision_binding", "single_use", "principal"):
            self.assertNotIn('"' + field + '"', output)

    def test_objective_json_projects_tool_route_and_revision_inventories(self):
        output = self.command("status", "--objective", "objective", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["objective"], "objective")
        for inventory in (result["owner_decisions"], result["report"]["owner_decisions"]):
            self.assertTrue({"constraint", "revision", "tool_permission", "tool_operation", "standing_authorization"}
                            <= {row["kind"] for row in inventory})
            for row in inventory:
                decision = row["decision"]
                if "actor" in decision:
                    self.assertEqual(decision["actor"], {"kind": "agent", "channel": "relayed"})
                    self.assertEqual(decision["provenance"], "agent_decision")
                    self.assertEqual(decision["channel"], "relayed")
                    self.assertEqual(len(decision["decision_sha256"]), 64)
        requested = next(row for row in result["constraints"] if row["kind"] == "route_rule")
        self.assertEqual(requested["value"], rule()["value"])
        self.assertEqual(result["tool_permissions"][0]["mode"], "ask_each_time")
        self.assertEqual(result["standing_authorization"]["decision"]["actor"], "agent")

    def test_config_json_omits_private_policy_and_permission_decisions(self):
        output = self.command("config", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["decision_policy"]["preset"], "delegate")
        self.assertEqual({row["tool"] for row in result["tool_permissions"]}, {"fixture-tool", "fixture-memory"})

    def test_config_text_preserves_preference_summary(self):
        output = self.command("config")
        self.assert_private_absent(output)
        self.assertIn("enabled routes", output)

    def test_policy_json_omits_its_original_private_instruction(self):
        output = self.command("policy", "--json")
        self.assert_private_absent(output)
        self.assertEqual(json.loads(output)["decision_policy"]["preset"], "delegate")

    def test_policy_text_keeps_decider_meaning(self):
        output = self.command("policy")
        self.assert_private_absent(output)
        self.assertIn("Decision policy: Delegate", output)
        self.assertIn("trusted agents", output)

    def test_models_json_keeps_private_preference_decisions_out_of_routes(self):
        output = self.command("models", "--json")
        self.assert_private_absent(output)
        self.assertTrue(json.loads(output)["routes"])

    def test_doctor_json_does_not_expand_private_decision_state(self):
        output = self.command("doctor", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["native_probe"], "not_run")
        self.assertIn("preferences", result)

    def test_objective_text_keeps_meaningful_decision_channels(self):
        output = self.command("status", "--objective", "objective")
        self.assert_private_absent(output)
        self.assertIn("objective objective", output)
        self.assertIn("Decision tool_permission: relayed", output)
        self.assertIn("Decision tool_operation: relayed", output)
        self.assertIn("Decision revision: relayed", output)
        self.assertIn("Decision standing_authorization: relayed", output)

    def test_list_json_contains_bounded_objective_facts(self):
        output = self.command("status", "--list", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["status"], "listed")
        self.assertEqual(result["objectives"][0]["objective"], "objective")
        self.assertEqual(result["objectives"][0]["schema"], "current")

    def test_list_text_still_identifies_the_objective(self):
        output = self.command("status", "--list")
        self.assert_private_absent(output)
        self.assertIn("objective", output)
        self.assertIn("open", output)

    def test_actual_display_command_exposes_wait_facts_without_private_evidence(self):
        terms = {"result": {"terminals": [{"handle": "owner", "connected": True,
                 "worktreePath": str(self.case.project), "title": "Coordinator"}]}}
        trees = {"result": {"worktrees": [{"path": str(self.case.project), "id": "task-tree",
                 "displayNameMode": "automatic", "displayName": "Fixture"}], "totalCount": 1,
                 "truncated": False, "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}}
        def read(argv):
            return {"version": "1.4.230"} if argv == ["--version"] else terms if argv[:2] == ["terminal", "list"] else trees
        request = self.case.root / "display-request.json"
        request.write_text(json.dumps({"project": str(self.case.project), "objective": "objective"}))
        output = StringIO()
        with patch("pod.orca.read_command", side_effect=read), patch("pod.orca.mutate_command") as mutation, \
             patch.object(relay, "verify_decision", side_effect=AssertionError("display verified authority")), \
             redirect_stdout(output):
            self.assertEqual(internal.main(["display", "--input", str(request)]), 0)
        self.assert_private_absent(output.getvalue())
        result = json.loads(output.getvalue())["result"]
        self.assertEqual(result["decision_policy"]["preset"], "delegate")
        self.assertEqual(result["tool_permissions"][0]["mode"], "ask_each_time")
        self.assertEqual(result["desktop"], "NOT_VERIFIED")
        self.assertTrue(result["rows"])
        mutation.assert_not_called()

    def test_private_inventory_and_authentication_keep_original_proof(self):
        state = ledger.read(self.case.project, "objective")
        rows = context_decisions(state) + map_decisions(state["checkpoint"])
        report = ledger.kernel_view(self.case.project, "objective", state=state)["report"]
        self.assertEqual(report["owner_decisions"], rows)
        for kind in ("constraint", "revision", "tool_permission", "tool_operation", "standing_authorization"):
            decisions = [row["decision"] for row in rows if row["kind"] == kind]
            self.assertTrue(decisions, kind)
            for decision in decisions:
                validated = relay.validate_decision_record(decision)
                self.assertIn(INSTRUCTION, validated["instruction"])
                self.assertEqual(validated["actor"]["identity"], PRINCIPAL)
                self.assertIn(SCOPE, validated["actor"]["scope"]["kinds"])
                self.assertEqual(validated["decision_binding"], validated["decision_verification"]["decision"])
        proof = next(row["decision"] for row in rows if row["kind"] == "tool_operation")
        with patch.object(relay, "_output", return_value=json.dumps(proof["decision_verification"]).encode()), \
             patch.object(relay, "verify_decision", wraps=relay.verify_decision) as verified:
            relay.Decision._recorded(self.case.project, "objective", proof).revalidate_authorizer()
        self.assertEqual(verified.call_args.args[1]["reference"], REFERENCE + "operation")

    def test_public_summary_cannot_replace_private_authority(self):
        state = ledger.read(self.case.project, "objective")
        proof = next(row["decision"] for row in context_decisions(state) if row["kind"] == "tool_operation")
        with self.assertRaises(PodError) as caught:
            relay.validate_decision_record(public_status(proof))
        self.assertEqual(caught.exception.code, "invalid_decision")


class RetiredDecisionPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_verified_lifecycle import VerifiedRetirementTests
        case = cls.case = VerifiedRetirementTests()
        cls.addClassCleanup(case.doCleanups)
        case.setUp()
        case.choose_policy()
        case.configure_verifier()
        authority = case.request({**RELAY, "instruction": INSTRUCTION,
                                  "relay": {"reference": REFERENCE, "verification": "decision"}})
        fixed = save_vouch(case, lambda: case.apply_decision(authority))
        fixed["principal"]["identity"] = PRINCIPAL
        fixed["principal"]["scope"]["kinds"] = [*fixed["principal"]["scope"]["kinds"], SCOPE]
        fixed["single_use"] = USE_KEY
        case.controls.write_text(json.dumps({"saved_verdict": fixed}))
        case.assert_accepted(case.apply_decision(authority))
        cls.before = case.durable()
        cls.config_before = config.personal_path().read_bytes()

    command = PublicDecisionPrivacyTests.command
    assert_private_absent = PublicDecisionPrivacyTests.assert_private_absent

    def tearDown(self):
        self.assertEqual(self.case.durable(), self.before)
        self.assertEqual(config.personal_path().read_bytes(), self.config_before)

    def test_retired_list_json_does_not_expand_authority_evidence(self):
        output = self.command("status", "--list", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        row = next(row for row in result["objectives"] if row["objective"] == "objective")
        self.assertEqual(row["phase"], "retired")
        self.assertEqual(row["retirement"]["actor"], {"kind": "agent", "channel": "relayed"})

    def test_retired_list_text_still_reports_retirement(self):
        output = self.command("status", "--list")
        self.assert_private_absent(output)
        self.assertIn("objective", output)
        self.assertIn("retired", output)

    def test_retired_objective_json_sanitizes_status_and_report_inventories(self):
        output = self.command("status", "--objective", "objective", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["retirement"]["actor"], {"kind": "agent", "channel": "relayed"})
        decision = next(row["decision"] for row in result["owner_decisions"] if row["kind"] == "retirement")
        self.assertEqual(decision["channel"], "relayed")

    def test_retired_objective_text_reports_truthful_decision_channel(self):
        output = self.command("status", "--objective", "objective")
        self.assert_private_absent(output)
        self.assertIn("Historical retirement", output)
        self.assertIn("Decision retirement: relayed", output)

    def test_retirement_private_read_and_authorizer_still_use_complete_evidence(self):
        state = ledger.read(self.case.project, "objective")
        proof = state["historical_retirement"]
        validated = relay.validate_decision_record(proof)
        self.assertEqual(validated["instruction"], INSTRUCTION)
        self.assertEqual(validated["actor"]["identity"], PRINCIPAL)
        self.assertIn(SCOPE, validated["actor"]["scope"]["kinds"])
        with patch.object(relay, "verify_decision", wraps=relay.verify_decision) as verifier:
            relay.Decision._recorded(self.case.project, "objective", proof).revalidate_authorizer()
        self.assertEqual(verifier.call_args.args[1]["reference"], REFERENCE)


class ClaimedDecisionPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_verified_lifecycle import VerifiedClaimTests
        case = cls.case = VerifiedClaimTests()
        cls.addClassCleanup(case.doCleanups)
        case.setUp()
        case.choose_policy()
        case.configure_verifier()
        authority = case.request({**RELAY, "instruction": INSTRUCTION,
                                  "relay": {"reference": REFERENCE, "verification": "decision"}})
        fixed = save_vouch(case, lambda: case.apply_decision(authority))
        fixed["principal"]["identity"] = PRINCIPAL
        fixed["principal"]["scope"]["kinds"] = [*fixed["principal"]["scope"]["kinds"], SCOPE]
        fixed["single_use"] = USE_KEY
        case.controls.write_text(json.dumps({"saved_verdict": fixed}))
        case.assert_accepted(case.apply_decision(authority))
        cls.before = case.durable()
        cls.config_before = config.personal_path().read_bytes()

    command = PublicDecisionPrivacyTests.command
    assert_private_absent = PublicDecisionPrivacyTests.assert_private_absent
    tearDown = RetiredDecisionPrivacyTests.tearDown

    def test_claimed_predecessor_json_keeps_safe_class_and_channel(self):
        output = self.command("status", "--objective", "objective", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        self.assertEqual(result["succession"]["continued_by"], "successor")
        self.assertEqual(result["succession"]["actor"], {"kind": "agent", "channel": "relayed"})
        decision = next(row["decision"] for row in result["owner_decisions"] if row["kind"] == "claim")
        self.assertEqual(decision["channel"], "relayed")

    def test_claimed_predecessor_text_keeps_claim_meaning(self):
        output = self.command("status", "--objective", "objective")
        self.assert_private_absent(output)
        self.assertIn("successor", output)
        self.assertIn("Decision claim: relayed", output)

    def test_claimed_list_json_keeps_both_objectives_without_private_proof(self):
        output = self.command("status", "--list", "--json")
        self.assert_private_absent(output)
        rows = {row["objective"]: row for row in json.loads(output)["objectives"]}
        self.assertEqual(rows["objective"]["phase"], "continued")
        self.assertEqual(rows["objective"]["continued_by"], "successor")
        self.assertEqual(rows["successor"]["continues"], "objective")

    def test_claimed_list_text_keeps_continuation_meaning(self):
        output = self.command("status", "--list")
        self.assert_private_absent(output)
        self.assertIn("objective | continued -> successor", output)

    def test_claim_private_record_and_authorizer_keep_original_scope(self):
        state = ledger.read(self.case.project, "objective")
        proof = state["continued_by"]
        validated = relay.validate_decision_record(proof)
        self.assertEqual(validated["instruction"], INSTRUCTION)
        self.assertEqual(validated["actor"]["identity"], PRINCIPAL)
        self.assertIn(SCOPE, validated["actor"]["scope"]["kinds"])
        with patch.object(relay, "verify_decision", wraps=relay.verify_decision) as verifier:
            relay.Decision._recorded(self.case.project, "successor", proof).revalidate_authorizer()
        self.assertEqual(verifier.call_args.args[1]["reference"], REFERENCE)


class GovernorDecisionPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_verified_decisions import VerifiedGovernorTests
        host = cls.governor = VerifiedGovernorTests()
        cls.addClassCleanup(host.doCleanups)
        host.setUp()
        cls.case = host.case
        value = host.generic_request()
        value["authorization"].update(instruction=INSTRUCTION, relay={"reference": REFERENCE, "verification": "decision"})
        operation = lambda: host.case.op("governor", action=value)
        fixed = save_vouch(host, operation)
        fixed["principal"]["identity"] = PRINCIPAL
        fixed["principal"]["scope"]["kinds"] = [*fixed["principal"]["scope"]["kinds"], SCOPE]
        fixed["single_use"] = USE_KEY
        host.controls.write_text(json.dumps({"saved_verdict": fixed}))
        host.assertEqual(operation()["decision"], "ALLOW")
        cls.before = cls.private_bytes()

    @classmethod
    def private_bytes(cls):
        return {str(path): path.read_bytes() for path in (
            config.personal_path(), *ledger.state_root(cls.case.project).rglob("*.json"))}

    def tearDown(self):
        self.assertEqual(self.private_bytes(), self.before)

    assert_private_absent = PublicDecisionPrivacyTests.assert_private_absent

    def command(self, *arguments):
        from pod.operations import OrcaPort
        with patch.object(cli, "current_run", return_value={"run": {"id": "run"}, "runtime": "runtime"}), \
             patch.object(cli, "worker_rows", return_value={"workers": [], "scope": {"run": "run"}, "complete": True}), \
             patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **fields: self.case.port.read_native(owner, **fields)):
            return PublicDecisionPrivacyTests.command(self, *arguments)

    def test_governor_authorization_json_sanitizes_both_public_inventories(self):
        output = self.command("status", "--objective", "objective", "--json")
        self.assert_private_absent(output)
        result = json.loads(output)
        for inventory in (result["owner_decisions"], result["report"]["owner_decisions"]):
            decision = next(row["decision"] for row in inventory if row["kind"] == "authorization")
            self.assertEqual(decision["actor"], {"kind": "agent", "channel": "relayed"})
            self.assertEqual(decision["channel"], "relayed")

    def test_governor_authorization_text_keeps_truthful_channel(self):
        output = self.command("status", "--objective", "objective")
        self.assert_private_absent(output)
        self.assertIn("Decision authorization: relayed", output)

    def test_private_journal_authorizer_still_validates_full_proof(self):
        proof = self.governor.journal()["actions"][-1]["action"]["authorization"]
        validated = relay.validate_decision_record(proof)
        self.assertEqual(validated["instruction"], INSTRUCTION)
        self.assertEqual(validated["actor"]["identity"], PRINCIPAL)
        self.assertIn(SCOPE, validated["actor"]["scope"]["kinds"])
        with patch.object(relay, "verify_decision", wraps=relay.verify_decision) as verifier:
            relay.Decision._recorded(self.case.project, "objective", proof).revalidate_authorizer()
        self.assertEqual(verifier.call_args.args[1]["reference"], REFERENCE)


class PublicDecisionProjectionTests(unittest.TestCase):
    def test_safe_scalar_actor_summary_remains_unchanged_and_idempotent(self):
        for actor, channel in (("person", "session"), ("person", "relayed"),
                               ("agent", "agent_session"), ("agent", "relayed")):
            with self.subTest(actor=actor, channel=channel):
                value = {"actor": actor, "channel": channel, "decided_at": "2026-10-01T00:00:00Z",
                         "scope": ["publish"], "id": "Y1"}
                before = deepcopy(value)
                observed = public_status(value)
                self.assertEqual(observed, before)
                self.assertEqual(public_status(observed), observed)
                self.assertEqual(value, before)

    def test_new_local_actor_and_legacy_channels_stay_distinct(self):
        earlier = {"provenance": "user_direct", "instruction": "earlier recorded words", "channel": "relayed",
                  "relay": {"reference": "old metadata", "owner_source": "old source"},
                  "relay_verification": {"decided_at": "2026-10-01T00:00:00Z", "owner_source": "old source"}}
        local = {"provenance": "agent_decision", "instruction": INSTRUCTION, "channel": "agent_session",
                 "actor": {"kind": "agent", "channel": "agent_session"}, "decision_binding": {"kind": "revision"}}
        value = {"owner_decisions": [{"kind": "revision", "reference": "revision_decisions/0", "decision": local},
                                      {"kind": "revision", "reference": "revision_decisions/1", "decision": earlier}]}
        before = deepcopy(value)
        observed = public_status(value)
        projected = observed["owner_decisions"][0]["decision"]
        self.assertNotIn(INSTRUCTION, json.dumps(observed))
        self.assertEqual(projected["actor"], local["actor"])
        self.assertEqual(projected["channel"], "agent_session")
        self.assertEqual(projected["instruction_sha256"], digest(INSTRUCTION))
        self.assertEqual(observed["owner_decisions"][1]["decision"], earlier)
        self.assertEqual(public_status(observed), observed)
        self.assertEqual(value, before)

    def test_compact_proof_keeps_only_actor_class_and_truthful_channel(self):
        for kind in ("person", "agent"):
            with self.subTest(kind=kind):
                compact = {"provenance": "user_direct" if kind == "person" else "agent_decision",
                           "instruction": INSTRUCTION, "channel": "relayed",
                           "relay": {"reference": REFERENCE, "verification": "decision"},
                           "decision_verification": {"principal": {"kind": kind, "identity": PRINCIPAL,
                                                     "scope": {"kinds": [SCOPE]}}}}
                before = deepcopy(compact)
                projected = public_status(compact)
                self.assertEqual(projected["actor"], {"kind": kind, "channel": "relayed"})
                self.assertEqual(projected["channel"], "relayed")
                self.assertEqual(projected["provenance"], compact["provenance"])
                self.assertTrue(all(marker not in json.dumps(projected) for marker in MARKERS))
                self.assertEqual(public_status(projected), projected)
                self.assertEqual(compact, before)

    def test_recorded_question_helper_keeps_identity_resolution_and_bounds(self):
        checkpoint = {"seq": 2, "revision": 1, "questions": [{"id": "q", "reference": "ref", "text": "Question?",
                      "choices": ["yes", "no"], "resolved": False}]}
        projected = recorded_questions(checkpoint)
        self.assertEqual(projected["items"], [{"index": 0, "id": "q", "reference": "ref", "text": "Question?",
                         "choices": ["yes", "no"], "resolution": {"resolved": False}, "missing": []}])
        self.assertEqual(projected["status"], "complete")
        checkpoint["questions"] *= 65
        bounded = recorded_questions(checkpoint)
        self.assertEqual((len(bounded["items"]), bounded["total"], bounded["truncated"]), (64, 65, True))


if __name__ == "__main__":
    unittest.main()

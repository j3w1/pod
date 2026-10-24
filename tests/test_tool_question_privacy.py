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
        config.set_decision_policy(case.project, "delegate", decision=PERSON)
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


class PublicDecisionProjectionTests(unittest.TestCase):
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

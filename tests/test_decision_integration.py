"""Machine policy and sensitive-use persistence at the actual config boundaries."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pod import config, relay
from pod.errors import PodError
from pod.ledger import state_root
from tests.decision_policy_support import LOCAL, PERSON, RELAY, MachineCase


class MachinePolicyTests(MachineCase, unittest.TestCase):
    def test_saved_person_policy_payload_state_scope_and_absent_verifier_refuse(self):
        import yaml
        from tests.decision_policy_support import save_vouch
        self.configure_verifier()
        operation = lambda: config.set_decision_policy(None, "hands_off", decision=RELAY)
        fixed = save_vouch(self, operation, actor="person")
        def observed():
            return config.personal_path().read_bytes(), {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}
        before = observed()
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(fixed)
            if field == "scope":
                wrong["principal"]["scope"]["resources"] = {"policy": "another"}
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError):
                operation()
            self.assertEqual(observed(), before)
        path = config.personal_path()
        document = yaml.safe_load(path.read_text()); document.pop("authorization_verifier")
        path.write_text(yaml.safe_dump(document))
        before = observed()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(observed(), before)

    def test_policy_and_memory_edits_preserve_unrelated_comments_and_preferences(self):
        path = config.personal_path()
        raw = path.read_text().replace("schema: " + config.SCHEMA, "# My preferences\nschema: " + config.SCHEMA + " # schema comment")
        path.write_text(raw + "\n# Keep this unrelated note\n")
        self.policy()
        self.memory("ask_each_time")
        self.assertIn("# My preferences", path.read_text())
        self.assertIn("# schema comment", path.read_text())
        self.assertIn("# Keep this unrelated note", path.read_text())
        before = config.read_yaml(path)["routes"]
        self.policy("delegate")
        self.assertEqual(config.read_yaml(path)["routes"], before)

    def test_policy_post_replace_failure_recovers_as_committed_readback(self):
        replace = config._replace
        def committed(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError("fixture lost acknowledgement")
        with patch.object(config, "_replace", side_effect=committed), self.assertRaises(OSError):
            self.policy()
        before = config.personal_path().read_bytes()
        result = self.policy()
        self.assertEqual(result["status"], "replayed")
        self.assertTrue(result["committed"])
        self.assertEqual(config.personal_path().read_bytes(), before)

    def test_interactive_custom_uses_plain_decider_words(self):
        from pod import policy
        from pod.cli import execute, parser
        count = len(policy.CATEGORIES) - len(policy.PERSON_ONLY | policy.AUTOMATIC)
        choices = (["me", "trusted agents", "any agent"] * count)[:count] + ["yes"]
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=choices) as prompt:
            execute(parser().parse_args(["policy", "custom", "--json"]), None)
        rendered = " ".join(str(call.args) for call in prompt.call_args_list)
        self.assertNotIn("trusted_agents", rendered)
        self.assertNotIn("any_agent", rendered)
        self.assertIn("trusted agents", rendered)
        current = config.load()["decision_policy"]
        for category in policy.PERSON_ONLY:
            self.assertEqual(current["categories"][category], "person")
        for category in policy.AUTOMATIC:
            self.assertEqual(current["categories"][category], "any_agent")

    def test_noninteractive_policy_text_shows_current_policy_without_writing(self):
        import contextlib
        from io import StringIO
        from pod.cli import main
        before = config.personal_path().read_bytes()
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(output):
            self.assertEqual(main(["policy"]), 0)
        self.assertIn("Decision policy: Careful", output.getvalue())
        self.assertIn("model choices: trusted agents", output.getvalue())
        self.assertNotIn("trusted_agents", output.getvalue())
        self.assertEqual(config.personal_path().read_bytes(), before)

    def test_missing_policy_is_read_only_and_skip_latches_careful(self):
        path = config.personal_path()
        before = path.read_bytes()
        snapshot = config.load()
        self.assertIsNone(snapshot["decision_policy"])
        self.assertEqual(config.decision_context(snapshot)["policy"]["setup"], "unasked")
        self.assertEqual(before, path.read_bytes())
        config.set_decision_policy(None, "skip")
        self.assertEqual(config.load()["decision_policy"]["setup"], "skipped")
        with self.assertRaises(PodError):
            config.set_decision_policy(None, "skip")

    def test_policy_changes_require_person_even_under_hands_off(self):
        self.policy()
        before = config.personal_path().read_bytes()
        with self.assertRaises(PodError) as caught:
            config.set_decision_policy(None, "delegate", decision=LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(before, config.personal_path().read_bytes())

    def test_generation_rejects_policy_aba_and_unrelated_route_edits_merge(self):
        self.policy()
        old = config.load()
        self.policy("careful")
        self.policy()
        self.assertGreater(config.load()["decision_generation"], old["decision_generation"])
        with self.assertRaises(PodError) as caught:
            config.set_decision_policy(None, "delegate", decision=PERSON, displayed=old)
        self.assertEqual(caught.exception.code, "decision_stale")
        fresh = config.load()
        config.edit(config.personal_path(), displayed=fresh, refresh="manual")
        config.set_decision_policy(None, "delegate", decision=PERSON, displayed=fresh)
        self.assertEqual(config.load()["refresh"], "manual")

    def test_receipt_before_policy_write_recovers_same_kernel_time(self):
        original = config._replace
        with patch.object(config, "_replace", side_effect=OSError("fixture interruption")):
            with self.assertRaises(OSError):
                self.policy()
        receipts = list((state_root(None) / ".relay-authorizations").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        with patch.object(config, "_replace", wraps=original):
            self.policy()
        decision = config.load()["decision_policy"]["decision"]
        self.assertEqual(decision["decided_at"], receipt["decision"]["decided_at"])

    def test_capability_cannot_serialize_and_forged_evidence_is_input_refused(self):
        snapshot = config.load()
        capability = relay.Decision(None, None, kind="policy", effect={}, state=config.decision_state(snapshot),
                                    decision=PERSON, resources={"policy": "personal"}, snapshot=snapshot)
        with self.assertRaises(TypeError):
            deepcopy(capability)
        with self.assertRaises(TypeError):
            json.dumps(capability)
        with self.assertRaises(PodError):
            config.set_decision_policy(None, "hands_off", decision=capability.record)

    def test_verified_person_policy_and_agent_denial(self):
        from tests.decision_policy_support import save_vouch
        self.configure_verifier(kind="agent")
        with self.assertRaises(PodError) as caught:
            config.set_decision_policy(None, "hands_off", decision=RELAY)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.configure_verifier(kind="person")
        save_vouch(self, lambda: config.set_decision_policy(None, "hands_off", decision=RELAY), actor="person")
        config.set_decision_policy(None, "hands_off", decision=RELAY)
        row = config.load()["decision_policy"]["decision"]
        self.assertEqual(row["provenance"], "user_direct")
        self.assertEqual(row["channel"], "relayed")
        self.assertEqual(row["actor"]["kind"], "person")


class MachineToolTests(MachineCase, unittest.TestCase):
    def test_new_question_reclaims_invalidated_approval_and_keeps_receipt(self):
        self.policy()
        self.memory("ask_each_time")
        old = config.tool_check(None, "fixture-tool", "secrets", "old operation")
        config.tool_answer(None, old["invocation"], "approve", decision=LOCAL)
        receipts = {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}
        self.policy("careful")
        question = config.tool_check(None, "fixture-tool", "secrets", "current operation")
        self.assertFalse(question["execute"])
        self.assertNotIn(old["invocation"], [row["id"] for row in config.load()["pending_decisions"]])
        with self.assertRaises(PodError):
            config.tool_check(None, "fixture-tool", "secrets", "old operation", invocation=old["invocation"])
        for name, data in receipts.items():
            self.assertEqual((state_root(None) / ".relay-authorizations" / name).read_bytes(), data)

    def answer_operation(self, purpose):
        if purpose == "operation":
            self.memory("ask_each_time")
        row = config.tool_check(None, "fixture-tool", "secrets", "read")
        self.assertEqual(row["purpose"], purpose)
        return lambda: config.tool_answer(None, row["invocation"], "approve" if purpose == "operation" else "hands_off", decision=RELAY)

    def observed_authority(self):
        return config.personal_path().read_bytes(), {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}

    def refused_answer_bindings(self, purpose):
        from tests.decision_policy_support import save_vouch
        self.policy()
        self.configure_verifier()
        operation = self.answer_operation(purpose)
        fixed = save_vouch(self, operation)
        before = self.observed_authority()
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(fixed)
            if field == "scope":
                wrong["principal"]["scope"]["resources"] = {"tool": "another"}
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual(self.observed_authority(), before)

    def refused_answer_authority(self, purpose):
        from tests.decision_policy_support import save_vouch
        operation = self.answer_operation(purpose)
        before = self.observed_authority()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(self.observed_authority(), before)
        self.configure_verifier()
        save_vouch(self, operation)
        before = self.observed_authority()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.observed_authority(), before)

    def test_memory_saved_payload_state_scope_refusals(self):
        self.refused_answer_bindings("memory")

    def test_memory_absent_verifier_and_policy_refusals(self):
        self.refused_answer_authority("memory")

    def test_operation_saved_payload_state_scope_refusals(self):
        self.refused_answer_bindings("operation")

    def test_operation_absent_verifier_and_policy_refusals(self):
        self.refused_answer_authority("operation")

    def test_factored_proof_lossless_full_reader_and_malformed_stored_refusal(self):
        import yaml
        from pod.relay import validate_decision_record, validate_decision_verdict
        from tests.decision_policy_support import save_vouch
        self.policy()
        self.configure_verifier()
        question = config.tool_check(None, "fixture-tool", "secrets", "read")
        original = save_vouch(self, lambda: config.tool_answer(None, question["invocation"], "hands_off", decision=RELAY))
        original["principal"]["scope"]["kinds"] = [*original["principal"]["scope"]["kinds"], "revision"]
        original["principal"]["scope"]["categories"] = [*original["principal"]["scope"]["categories"], "merge_publish"]
        self.controls.write_text(json.dumps({"saved_verdict": original}))
        config.tool_answer(None, question["invocation"], "hands_off", decision=RELAY)
        path = config.personal_path()
        document = yaml.safe_load(path.read_text())
        compact = document["pending_decisions"][0]["decision"]
        self.assertEqual(compact["decision_verification"]["schema"], "pod-tool-proof/v1")
        full = validate_decision_record(compact, kind="tool_permission")
        self.assertEqual(full["decision_verification"], validate_decision_verdict(original))
        self.assertEqual(validate_decision_record(full), full)
        mixed = {**compact, "actor": full["actor"]}
        with self.assertRaises(PodError):
            validate_decision_record(mixed)
        for changes in ({"resources": {}}, {"kinds": []}, {"categories": ["money"]}):
            wrong = deepcopy(document)
            wrong["pending_decisions"][0]["decision"]["decision_verification"]["principal"]["scope"] = changes
            path.write_text(yaml.safe_dump(wrong))
            before = path.read_bytes()
            self.assertEqual(config.load()["status"], "invalid")
            self.assertEqual(path.read_bytes(), before)

    def test_verified_agent_memory_and_operation_fit_without_duplicated_verdict(self):
        self.policy("delegate")
        self.configure_verifier(kind="agent")
        memory = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        config.tool_answer(None, memory["invocation"], "ask_each_time", decision=RELAY)
        operation = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        config.tool_answer(None, operation["invocation"], "approve", decision=RELAY)
        result = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=operation["invocation"])
        self.assertTrue(result["execute"])
        self.assertFalse(config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=operation["invocation"])["execute"])

    def test_answer_post_replace_failure_is_readback_without_execute(self):
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        replace = config._replace
        def committed(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError("fixture lost acknowledgement")
        with patch.object(config, "_replace", side_effect=committed), self.assertRaises(OSError):
            config.tool_answer(None, question["invocation"], "ask_each_time", decision=PERSON)
        before = config.personal_path().read_bytes()
        result = config.tool_answer(None, question["invocation"], "ask_each_time", decision=PERSON)
        self.assertTrue(result["committed"])
        self.assertFalse(result["execute"])
        self.assertEqual(before, config.personal_path().read_bytes())

    def test_review_changes_memory_both_directions_without_operation_grant(self):
        self.policy()
        self.memory("hands_off")
        first = config.tool_review(None, "fixture-tool", "secrets")
        answer = config.tool_answer(None, first["invocation"], "ask_each_time", decision=LOCAL)
        self.assertFalse(answer["execute"])
        self.assertFalse(config.tool_check(None, "fixture-tool", "secrets", "one exact operation")["execute"])
        second = config.tool_review(None, "fixture-tool", "secrets")
        answer = config.tool_answer(None, second["invocation"], "hands_off", decision=LOCAL)
        self.assertFalse(answer["execute"])
        self.assertTrue(config.tool_check(None, "fixture-tool", "secrets", "one exact operation")["execute"])
        from pod.cli import _decision_view
        projected = _decision_view(config.load())["tool_permissions"][0]
        self.assertEqual(projected["mode"], "hands_off")
        self.assertEqual(projected["changed_at"], config.load()["tool_permissions"][0]["decision"]["decided_at"])

    def test_review_rechecks_same_authority_and_stale_review_cannot_restore_memory(self):
        self.memory()
        review = config.tool_review(None, "fixture-tool", "secrets")
        before = config.personal_path().read_bytes()
        with self.assertRaises(PodError) as caught:
            config.tool_answer(None, review["invocation"], "hands_off", decision=LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(config.personal_path().read_bytes(), before)
        replacement = config.tool_review(None, "fixture-tool", "secrets")
        with self.assertRaises(PodError):
            config.tool_answer(None, review["invocation"], "hands_off", decision=PERSON)
        config.tool_answer(None, replacement["invocation"], "ask_each_time", decision=PERSON)
        self.assertEqual(config.load()["tool_permissions"][0]["mode"], "ask_each_time")

    def test_review_money_refuses_without_memory_or_pending_mutation(self):
        from pod.cli import execute, parser
        before = config.personal_path().read_bytes()
        with self.assertRaises(PodError) as caught:
            execute(parser().parse_args(["tool", "review", "--tool", "fixture-tool", "--category", "money", "--json"]), None)
        self.assertEqual(caught.exception.code, "tool_review_money")
        self.assertIn("each new spending commitment", str(caught.exception))
        self.assertEqual(config.personal_path().read_bytes(), before)

    def test_repeated_question_is_stable_and_stale_question_refreshes_identity(self):
        first = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        second = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        self.assertEqual(first["invocation"], second["invocation"])
        self.policy()
        with self.assertRaises(PodError) as caught:
            config.tool_answer(None, first["invocation"], "hands_off", decision=LOCAL)
        self.assertEqual(caught.exception.code, "decision_stale")
        refreshed = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=first["invocation"])
        self.assertNotEqual(first["invocation"], refreshed["invocation"])
        self.assertEqual(len(config.load()["pending_decisions"]), 1)

    def test_policy_change_during_automatic_check_denies_execute(self):
        from pod import tool_permissions
        self.policy()
        self.memory("hands_off")
        check = tool_permissions.check
        def changed(*args, **kwargs):
            result = check(*args, **kwargs)
            self.policy("careful")
            return result
        with patch.object(tool_permissions, "check", side_effect=changed), self.assertRaises(PodError) as caught:
            config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        self.assertEqual(caught.exception.code, "decision_stale")

    def test_cli_policy_and_tool_questions_are_plain_and_config_is_sanitized(self):
        from pod.cli import execute, parser
        question = execute(parser().parse_args(["tool", "check", "--tool", "fixture-tool", "--category", "secrets",
                                               "--operation", "private-operation-sentinel", "--json"]), None)
        self.assertEqual(question["status"], "permission_question")
        self.assertFalse(question["execute"])
        view = execute(parser().parse_args(["policy", "--json"]), None)
        self.assertNotIn("private-operation-sentinel", json.dumps(view))
        self.assertTrue(view["needs_you"])
        self.assertEqual(view["waiting_decisions"]["total"], 1)

    def test_memory_and_operation_have_different_invocation_identities(self):
        question = self.memory()
        operation = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertNotEqual(question["invocation"], operation["invocation"])
        self.assertEqual(operation["purpose"], "operation")

    def test_refusal_and_pruned_use_never_execute_again(self):
        self.memory()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        config.tool_answer(None, question["invocation"], "refuse", decision=PERSON)
        self.assertFalse(config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])["execute"])
        next_question = config.tool_check(None, "fixture-tool", "secrets", "another exact operation")
        with self.assertRaises(PodError):
            config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertNotEqual(next_question["invocation"], question["invocation"])

    def test_oversized_full_decision_refuses_before_receipt_or_config_effects(self):
        self.policy()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        self.configure_verifier(kind="agent")
        # Legal host output can exceed the smaller stored question bound.
        self.controls.write_text(json.dumps({"changes": {"single_use": "x" * 512,
            "principal.identity": "a" * 128}}))
        before = config.personal_path().read_bytes()
        receipts = sorted((state_root(None) / ".relay-authorizations").glob("*.json"))
        with self.assertRaises(PodError) as caught:
            config.tool_answer(None, question["invocation"], "ask_each_time", decision={**RELAY, "instruction": "x" * 1024})
        self.assertIn(caught.exception.code, ("pending_decision_too_large", "invalid_tool_decision", "yaml_resource_limit"))
        self.assertEqual(before, config.personal_path().read_bytes())
        self.assertEqual(receipts, sorted((state_root(None) / ".relay-authorizations").glob("*.json")))

    def test_memory_choice_never_approves_operation_and_use_is_consumed(self):
        self.memory()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        self.assertEqual(question["status"], "operation_question")
        self.assertFalse(question["execute"])
        config.tool_answer(None, question["invocation"], "approve", decision=PERSON)
        first = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertTrue(first["execute"])
        second = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertFalse(second["execute"])
        self.assertEqual(second["status"], "consumed")

    def test_hands_off_memory_still_obeys_stricter_policy(self):
        self.policy()
        self.memory("hands_off")
        self.assertTrue(config.tool_check(None, "fixture-tool", "secrets", "one exact operation")["execute"])
        self.policy("careful")
        result = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        self.assertFalse(result["execute"])
        self.assertEqual(result["required"], "person")
        self.assertEqual(config.load()["tool_permissions"][0]["mode"], "hands_off")

    def test_exact_invocation_and_generation_are_rechecked(self):
        self.memory()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        with self.assertRaises(PodError):
            config.tool_check(None, "fixture-tool", "secrets", "different operation", invocation=question["invocation"])
        config.tool_answer(None, question["invocation"], "approve", decision=PERSON)
        self.policy()
        with self.assertRaises(PodError) as caught:
            config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertEqual(caught.exception.code, "decision_stale")

    def test_use_receipt_interruption_returns_consumed_without_execute(self):
        self.memory()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        config.tool_answer(None, question["invocation"], "approve", decision=PERSON)
        with patch.object(config, "_replace", side_effect=OSError("fixture interruption")):
            with self.assertRaises(OSError):
                config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        result = config.tool_check(None, "fixture-tool", "secrets", "one exact operation", invocation=question["invocation"])
        self.assertEqual(result["status"], "consumed")
        self.assertFalse(result["execute"])

    def test_included_subscription_is_routine_and_money_never_hands_off(self):
        result = config.tool_check(None, "fixture-tool", "money", "included usage", spending="included_subscription")
        self.assertTrue(result["execute"])
        self.assertEqual(config.load()["pending_decisions"], [])
        question = config.tool_check(None, "fixture-tool", "money", "new account", spending="new_commitment")
        with self.assertRaises(PodError):
            config.tool_answer(None, question["invocation"], "hands_off", decision=PERSON)
        with self.assertRaises(PodError):
            config.tool_check(None, "fixture-tool", "money", "unknown spending")

    def test_local_agent_memory_truthful_and_policy_person_only(self):
        self.policy()
        question = config.tool_check(None, "fixture-tool", "secrets", "one exact operation")
        config.tool_answer(None, question["invocation"], "hands_off", decision=LOCAL)
        stored = config.load()["tool_permissions"][0]["decision"]
        self.assertEqual(stored["provenance"], "agent_decision")
        self.assertEqual(stored["actor"], {"kind": "agent", "channel": "agent_session"})


from tests.issue41_support import ProductionCase


class ObjectiveToolTests(ProductionCase):
    def setUp(self):
        super().setUp()
        self.intake()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.controls = self.root / "objective-tool-controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "objective-tool-verifier.py"
        from tests.decision_policy_support import VERIFIER
        self.verifier.write_text(VERIFIER)

    configure_verifier = MachineCase.configure_verifier

    def check(self, operation="read exact fixture", **fields):
        return self.tool_call(action="check", tool="fixture-tool", category="secrets", operation=operation, **fields)

    def tool_call(self, objective="objective", **fields):
        from pod.internal import run
        return run("tool", {"project": str(self.project), "objective": objective, "owner": "owner", **fields})

    def answer(self, identity, answer, authority=LOCAL):
        return self.tool_call(action="answer", invocation=identity, answer=answer, decision=authority)

    def test_objective_memory_and_operation_questions_do_not_duplicate_machine_pending(self):
        from pod.ledger import read
        first = self.check()
        self.assertEqual(first["purpose"], "memory")
        self.assertEqual(config.load(self.project)["pending_decisions"], [])
        self.answer(first["invocation"], "ask_each_time")
        self.assertEqual(config.load(self.project)["pending_decisions"], [])
        second = self.check()
        self.assertNotEqual(first["invocation"], second["invocation"])
        self.assertEqual(second["purpose"], "operation")
        self.answer(second["invocation"], "approve")
        result = self.check(invocation=second["invocation"])
        self.assertTrue(result["execute"])
        self.assertFalse(self.check(invocation=second["invocation"])["execute"])
        self.assertEqual(config.load(self.project)["pending_decisions"], [])
        self.assertEqual(read(self.project, "objective")["checkpoint"]["questions"][-1]["state"], "consumed")

    def test_new_objective_question_prunes_stale_approval_but_keeps_current_independent_grant(self):
        from pod.ledger import read
        first = self.check()
        self.answer(first["invocation"], "ask_each_time")
        old = self.check(operation="old operation")
        self.answer(old["invocation"], "approve")
        self.check(operation="independent operation")
        retained = read(self.project, "objective")["checkpoint"]["questions"]
        self.assertIn(old["invocation"], [row["id"] for row in retained])
        receipts = {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        question = self.check(operation="current operation")
        self.assertFalse(question["execute"])
        retained = read(self.project, "objective")["checkpoint"]["questions"]
        self.assertNotIn(old["invocation"], [row["id"] for row in retained])
        with self.assertRaises(PodError):
            self.check(operation="old operation", invocation=old["invocation"])
        for name, data in receipts.items():
            self.assertEqual((state_root(None) / ".relay-authorizations" / name).read_bytes(), data)

    def test_independent_question_on_the_same_objective_preserves_valid_execution_grant(self):
        first = self.check()
        self.answer(first["invocation"], "ask_each_time")
        use = self.check(operation="approved operation")
        self.answer(use["invocation"], "approve")
        self.check(operation="unrelated operation")
        self.assertTrue(self.check(operation="approved operation", invocation=use["invocation"])["execute"])
        self.assertFalse(self.check(operation="approved operation", invocation=use["invocation"])["execute"])

    def test_worker_public_text_and_json_delegate_without_fabricating_invocation(self):
        import contextlib
        from io import StringIO
        from pod.cli import main
        from pod.ledger import _path
        before = _path(self.project, "objective").read_bytes(), config.personal_path(self.project).read_bytes()
        args = ["tool", "check", "--objective", "objective", "--tool", "fixture-tool", "--category", "secrets",
                "--operation", "read exact fixture"]
        with patch("pathlib.Path.cwd", return_value=self.project), patch.dict("os.environ", {"ORCA_TERMINAL_HANDLE": "worker"}):
            output = StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(args), 0)
            self.assertIn("coordinator", output.getvalue())
            self.assertNotIn("Pending decision:", output.getvalue())
            output = StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([*args, "--json"]), 0)
            result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "coordinator_question")
        self.assertFalse(result["execute"])
        self.assertNotIn("invocation", result)
        self.assertEqual((_path(self.project, "objective").read_bytes(), config.personal_path(self.project).read_bytes()), before)

    def test_saved_verified_objective_memory_and_operation_answers_remain_bounded(self):
        from pod.ledger import read
        from pod.util import encoded
        from tests.decision_policy_support import save_vouch
        self.configure_verifier()
        first = self.check(operation="read")
        save_vouch(self, lambda: self.answer(first["invocation"], "ask_each_time", RELAY))
        self.assertFalse(self.answer(first["invocation"], "ask_each_time", RELAY)["execute"])
        second = self.check(operation="read")
        save_vouch(self, lambda: self.answer(second["invocation"], "approve", RELAY))
        self.assertFalse(self.answer(second["invocation"], "approve", RELAY)["execute"])
        self.assertTrue(self.check(operation="read", invocation=second["invocation"])["execute"])
        for row in read(self.project, "objective")["checkpoint"]["questions"]:
            self.assertLessEqual(len(encoded(row, compact=True)), 2048)

    def test_objective_answer_rechecks_native_generation_after_verification(self):
        from pod.ledger import _path
        from tests.decision_policy_support import save_vouch
        self.configure_verifier()
        first = self.check()
        save_vouch(self, lambda: self.answer(first["invocation"], "hands_off", RELAY))
        verify = relay.verify_decision
        def changed(command, request):
            result = verify(command, request)
            self.current["consumer_generation"] += 1
            return result
        before = _path(self.project, "objective").read_bytes(), config.personal_path(self.project).read_bytes()
        with patch.object(relay, "verify_decision", side_effect=changed), self.assertRaises(PodError) as caught:
            self.answer(first["invocation"], "hands_off", RELAY)
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual((_path(self.project, "objective").read_bytes(), config.personal_path(self.project).read_bytes()), before)

    def test_legal_oversize_objective_answer_refuses_before_personal_context_or_receipt(self):
        from pod.ledger import _path, state_root
        first = self.check()
        def observed():
            return (_path(self.project, "objective").read_bytes(), config.personal_path(self.project).read_bytes(),
                    {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")})
        before = observed()
        with self.assertRaises(PodError) as caught:
            self.answer(first["invocation"], "hands_off", {**LOCAL, "instruction": "x" * 1024})
        self.assertEqual(caught.exception.code, "pending_decision_too_large")
        self.assertEqual(observed(), before)

    def test_cancel_and_recreate_never_accepts_the_old_question(self):
        first = self.check()
        self.op("tool", action="cancel", invocation=first["invocation"])
        second = self.check()
        self.assertNotEqual(first["invocation"], second["invocation"])
        with self.assertRaises(PodError):
            self.answer(first["invocation"], "hands_off")
        self.assertFalse(self.answer(second["invocation"], "hands_off")["execute"])

    def other_objective(self):
        from pod.internal import run
        run("checkpoint", {"project": str(self.project), "objective": "other", "owner": "owner",
            "value": self.core(obligations=[self.criterion()], governance={"base_ref": "target"})})

    def test_independent_objective_question_does_not_invalidate_operation_approval(self):
        first = self.check()
        self.answer(first["invocation"], "ask_each_time")
        use = self.check()
        self.answer(use["invocation"], "approve")
        generation = config.load(self.project)["decision_generation"]
        self.other_objective()
        self.tool_call(objective="other", action="check", tool="different-tool", category="secrets", operation="read another fixture")
        self.assertEqual(config.load(self.project)["decision_generation"], generation)
        self.assertTrue(self.check(invocation=use["invocation"])["execute"])

    def test_two_objectives_same_memory_choice_do_not_misidentify_recovery(self):
        from pod.ledger import _path
        first = self.check()
        self.other_objective()
        second = self.tool_call(objective="other", action="check", tool="fixture-tool", category="secrets", operation="read exact fixture")
        generation = config.load(self.project)["decision_generation"]
        self.answer(first["invocation"], "hands_off")
        before = _path(self.project, "other").read_bytes(), config.personal_path(self.project).read_bytes()
        with self.assertRaises(PodError) as caught:
            self.tool_call(objective="other", action="answer", invocation=second["invocation"], answer="hands_off", decision=LOCAL)
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual((_path(self.project, "other").read_bytes(), config.personal_path(self.project).read_bytes()), before)
        self.assertEqual(config.load(self.project)["decision_generation"], generation + 1)

    def test_concurrent_objective_memory_answers_commit_only_one_generation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from pod import relay
        first = self.check()
        self.other_objective()
        second = self.tool_call(objective="other", action="check", tool="fixture-tool", category="secrets", operation="read exact fixture")
        generation = config.load(self.project)["decision_generation"]
        barrier, initialize = Barrier(2), relay.Decision.__init__
        def initialized(value, *args, **kwargs):
            initialize(value, *args, **kwargs)
            barrier.wait(timeout=1)
        def answer_one(objective, identity):
            try:
                return self.tool_call(objective=objective, action="answer", invocation=identity, answer="hands_off", decision=LOCAL)["status"]
            except PodError as error:
                return error.code
        with patch.object(relay.Decision, "__init__", new=initialized), ThreadPoolExecutor(max_workers=2) as workers:
            results = [workers.submit(answer_one, objective, identity) for objective, identity in (
                ("objective", first["invocation"]), ("other", second["invocation"]))]
            outcomes = sorted(result.result() for result in results)
        self.assertEqual(outcomes, ["answered", "decision_stale"])
        self.assertEqual(config.load(self.project)["decision_generation"], generation + 1)
        self.assertEqual(len(config.load(self.project)["tool_permissions"]), 1)

    def test_interrupted_memory_answer_finishes_only_context_after_personal_commit(self):
        from pod import ledger
        first = self.check()
        with patch.object(ledger, "publish_context", side_effect=OSError("fixture before objective replacement")), self.assertRaises(OSError):
            self.answer(first["invocation"], "ask_each_time")
        saved = config.personal_path(self.project).read_bytes()
        self.assertEqual(config.load(self.project)["tool_permissions"][0]["mode"], "ask_each_time")
        result = self.answer(first["invocation"], "ask_each_time")
        self.assertTrue(result["readback"])
        self.assertFalse(result["execute"])
        self.assertEqual(config.personal_path(self.project).read_bytes(), saved)

    def test_interrupted_operation_consumption_never_issues_another_execute(self):
        from pod import ledger
        first = self.check()
        self.answer(first["invocation"], "ask_each_time")
        use = self.check()
        self.answer(use["invocation"], "approve")
        with patch.object(ledger, "publish_context", side_effect=OSError("fixture before consumed record")), self.assertRaises(OSError):
            self.check(invocation=use["invocation"])
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        recovered = self.check(invocation=use["invocation"])
        self.assertEqual(recovered["status"], "consumed")
        self.assertFalse(recovered["execute"])

    def test_objective_scope_aba_and_forged_question_updates_refuse(self):
        from pod.ledger import _path, read
        from pod.internal import run
        first = self.check()
        original = self.stored()[0]["check"]
        changed = self.stored()
        changed[0]["check"] = "Changed then restored"
        self.write(changed, questions=read(self.project, "objective")["checkpoint"]["questions"], revision_authority=PERSON)
        restored = self.stored()
        restored[0]["check"] = original
        self.write(restored, questions=read(self.project, "objective")["checkpoint"]["questions"], revision_authority=PERSON)
        before = _path(self.project, "objective").read_bytes()
        with self.assertRaises(PodError) as caught:
            self.answer(first["invocation"], "hands_off")
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual(_path(self.project, "objective").read_bytes(), before)
        questions = deepcopy(read(self.project, "objective")["checkpoint"]["questions"])
        questions[0].update(state="approved", resolved=True, decision={"verified": True})
        with self.assertRaises(PodError) as caught:
            run("checkpoint", {"project": str(self.project), "objective": "objective", "owner": "owner", "value": self.core(questions=questions)})
        self.assertEqual(caught.exception.code, "tool_question_kernel_field")
        self.assertEqual(_path(self.project, "objective").read_bytes(), before)

    def test_completed_objective_closes_without_answering_an_unused_question(self):
        from pod.ledger import read
        self.check()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "explicit fixture scope withdrawal"})
        self.write(rows, questions=read(self.project, "objective")["checkpoint"]["questions"], close=True, revision_authority=PERSON)
        state = read(self.project, "objective")
        self.assertIsNotNone(state["checkpoint"]["closure"])
        self.assertEqual(state["checkpoint"]["questions"][0]["state"], "cancelled")


class MachineCapacityRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = MachineToolTests("runTest")
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        cls.case.policy()
        cls.case.memory("ask_each_time")
        for index in range(8):
            cls.last = config.tool_check(None, "fixture-tool", "secrets", f"prepared operation {index}")
            config.tool_answer(None, cls.last["invocation"], "approve", decision=LOCAL)
        cls.receipts = {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}

    def test_eight_approvals_then_stricter_policy_recover_without_file_edit_or_replay(self):
        self.case.policy("careful")
        result = config.tool_check(None, "fixture-tool", "secrets", "current operation")
        self.assertEqual((result["execute"], result["required"]), (False, "person"))
        with self.assertRaises(PodError):
            config.tool_check(None, "fixture-tool", "secrets", "prepared operation 7", invocation=self.last["invocation"])
        with self.assertRaises(PodError):
            config.tool_answer(None, self.last["invocation"], "approve", decision=LOCAL)
        for name, data in self.receipts.items():
            self.assertEqual((state_root(None) / ".relay-authorizations" / name).read_bytes(), data)
        self.assertEqual(config.load()["tool_permissions"][0]["mode"], "ask_each_time")


class ObjectiveCapacityRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pod.ledger import read
        cls.case = ObjectiveToolTests("runTest")
        cls.addClassCleanup(cls.case.doCleanups)
        cls.case.setUp()
        first = cls.case.check()
        cls.case.answer(first["invocation"], "ask_each_time")
        for index in range(8):
            cls.last = cls.case.check(operation=f"prepared operation {index}")
            cls.case.answer(cls.last["invocation"], "approve")
        cls.prepared_count = len(read(cls.case.project, "objective")["checkpoint"]["questions"])
        cls.receipts = {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}

    def test_all_eight_objective_slots_recover_without_reissuing_old_permission(self):
        self.assertEqual(self.prepared_count, 8)
        config.set_decision_policy(self.case.project, "careful", decision=PERSON)
        result = self.case.check(operation="current operation")
        self.assertEqual((result["execute"], result["required"]), (False, "person"))
        with self.assertRaises(PodError):
            self.case.check(operation="prepared operation 7", invocation=self.last["invocation"])
        with self.assertRaises(PodError):
            self.case.answer(self.last["invocation"], "approve")
        for name, data in self.receipts.items():
            self.assertEqual((state_root(None) / ".relay-authorizations" / name).read_bytes(), data)
        self.assertEqual(config.load(self.case.project)["tool_permissions"][0]["mode"], "ask_each_time")

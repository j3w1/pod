"""Current-vouch checks through public tool ports in disposable synthetic homes."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import yaml

from pod import config, ledger, relay
from pod.cli import execute, parser
from pod.errors import PodError
from pod.util import digest
from tests.decision_policy_support import LOCAL, PERSON, RELAY, VERIFIER, MachineCase, save_vouch
from tests.issue41_support import ProductionCase


class ToolUseCases:
    objective = None
    configure_verifier = MachineCase.configure_verifier

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Build Git, native doubles and the approved vouch once. Each test resets
        # only these disposable files, including every authority/use receipt.
        cls.case = cls("runTest")
        cls.addClassCleanup(cls.case.doCleanups)
        super(ToolUseCases, cls.case).setUp()
        cls.case.prepare()
        cls.state_homes = {ledger.state_root(cls.case.project), ledger.state_root(None)}
        paths = {config.personal_path(cls.case.project), cls.case.input, cls.case.controls, cls.case.verifier}
        for home in cls.state_homes:
            paths.update(path for path in home.rglob("*") if path.is_file())
        cls.prepared = {path: path.read_bytes() for path in paths}

    def setUp(self):
        for name in ("root", "project", "input", "controls", "verifier", "question", "vouch"):
            setattr(self, name, deepcopy(getattr(self.case, name)))
        for home in self.state_homes:
            for path in home.rglob("*"):
                if path.is_file() or path.is_symlink():
                    path.unlink()
        for path, raw in self.prepared.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def prepare(self):
        if self.objective:
            self.intake()
            self.controls = self.root / "controls.json"
            self.controls.write_text("{}")
            self.verifier = self.root / "verifier.py"
            self.verifier.write_text(VERIFIER)
        self.project = getattr(self, "project", None)
        self.input = self.root / "answer.json"
        self.call("policy", "delegate", decision=PERSON)
        memory = self.check()
        self.answer(memory["invocation"], "ask_each_time", PERSON)
        self.configure_verifier()
        self.verifier.write_text(VERIFIER.replace('if "saved_verdict" in controls:',
            'if controls.get("valid_until") and controls["now"] >= controls["valid_until"]:\n'
            '    controls["saved_verdict"]["verified"] = False\n'
            'if "saved_verdict" in controls:'))
        self.question = self.check()
        self.vouch = save_vouch(self, lambda: self.answer(self.question["invocation"], "approve", RELAY))
        self.answer(self.question["invocation"], "approve", RELAY)

    def call(self, *args, decision=None):
        arguments = list(args)
        if decision is not None:
            self.input.write_text(json.dumps(decision))
            arguments.extend(["--input", str(self.input)])
        if self.objective and args[0] == "tool":
            arguments.extend(["--objective", self.objective])
        return execute(parser().parse_args([*arguments, "--json"]), self.project)

    def check(self, *, invocation=None, category="secrets", operation="read exact fixture", spending=None):
        args = ["tool", "check", "--tool", "fixture-tool", "--category", category, "--operation", operation]
        if invocation is not None:
            args.extend(["--invocation", invocation])
        if spending is not None:
            args.extend(["--spending", spending])
        return self.call(*args)

    def answer(self, invocation, answer, decision):
        return self.call("tool", "answer", "--invocation", invocation, "--answer", answer, decision=decision)

    def use(self):
        return self.check(invocation=self.question["invocation"])

    def effects(self):
        paths = [config.personal_path(self.project)]
        if self.objective:
            paths.extend(ledger.objective_root(self.project, self.objective).rglob("*.json"))
        paths.extend(ledger.state_root(self.project).rglob(".relay-authorizations/*.json"))
        paths.extend((ledger.state_root(None) / ".relay-authorizations").glob("*.json"))
        return {str(path): path.read_bytes() for path in paths}

    def denied(self, code):
        before = self.effects()
        with self.assertRaises(PodError) as caught:
            self.use()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.effects(), before)

    def test_withdrawn_vouch_refuses_without_consumption(self):
        self.controls.write_text(json.dumps({"saved_verdict": {**self.vouch, "verified": False}}))
        self.denied("relay_verifier_denied")

    def test_expired_host_vouch_refuses_without_consumption(self):
        # The host owns expiry; the wire has no expiry claim for Pod to invent.
        self.controls.write_text(json.dumps({"saved_verdict": self.vouch,
            "valid_until": "2026-10-02T00:00:00Z", "now": "2026-10-03T00:00:00Z"}))
        self.denied("relay_verifier_denied")

    def test_changed_scope_refuses_even_with_positive_verdict(self):
        changed = deepcopy(self.vouch)
        changed["principal"]["scope"]["resources"] = {"tool": "another-tool"}
        self.controls.write_text(json.dumps({"saved_verdict": changed}))
        self.denied("relay_verifier_mismatch")

    def test_unavailable_verifier_refuses_without_consumption(self):
        self.verifier.unlink()
        self.denied("relay_verifier_failed")

    def test_unchanged_vouch_executes_once_and_consumed_readback_ignores_withdrawal(self):
        self.assertTrue(self.use()["execute"])
        self.controls.write_text(json.dumps({"saved_verdict": {**self.vouch, "verified": False}}))
        before = self.effects()
        observed = self.use()
        self.assertEqual((observed["status"], observed["execute"]), ("consumed", False))
        self.assertEqual(self.effects(), before)

    def change_authority_during_verification(self, kind):
        verify = relay.verify_decision
        def racing(command, request):
            # A second writer can acquire the config lock while the host is read.
            with config._edit_lock(config.personal_path(self.project)):
                path = config.personal_path(self.project)
                document = yaml.safe_load(path.read_text())
                if kind == "policy":
                    from pod.policy import preset
                    document["decision_policy"]["preset"] = "hands_off"
                    document["decision_policy"]["categories"] = preset("hands_off")
                else:
                    document["authorization_verifier"] = ["/missing-fixture-verifier"]
                path.write_text(yaml.safe_dump(document))
            return verify(command, request)
        receipts = self.effects()
        with patch.object(relay, "verify_decision", side_effect=racing), self.assertRaises(PodError) as caught:
            self.use()
        self.assertEqual(caught.exception.code, "decision_stale")
        after = self.effects()
        changed = str(config.personal_path(self.project))
        self.assertEqual({k: v for k, v in after.items() if k != changed},
                         {k: v for k, v in receipts.items() if k != changed})
        self.assertNotEqual(after[changed], receipts[changed])

    def test_policy_race_refuses_before_use_receipt(self):
        self.change_authority_during_verification("policy")

    def test_verifier_race_refuses_before_use_receipt(self):
        self.change_authority_during_verification("verifier")

    def test_interrupted_consumption_recovers_without_current_authority_or_execution(self):
        target = ledger if self.objective else config
        writer = "publish_context" if self.objective else "_replace"
        with patch.object(target, writer, side_effect=OSError("fixture interrupted publication")), self.assertRaises(OSError):
            self.use()
        self.call("policy", "careful", decision=PERSON)
        self.verifier.unlink()
        receipts = {k: v for k, v in self.effects().items() if ".relay-authorizations/" in k}
        observed = self.use()
        self.assertEqual((observed["status"], observed["execute"]), ("consumed", False))
        self.assertEqual({k: v for k, v in self.effects().items() if ".relay-authorizations/" in k}, receipts)
        self.assertFalse(self.use()["execute"])

    def test_mismatched_use_receipt_refuses_instead_of_recovering_another_invocation(self):
        target = ledger if self.objective else config
        writer = "publish_context" if self.objective else "_replace"
        with patch.object(target, writer, side_effect=OSError("fixture interrupted publication")), self.assertRaises(OSError):
            self.use()
        path = ledger.state_root(None) / ".relay-authorizations" / (digest("pod-tool-use:" + self.question["invocation"]) + ".json")
        receipt = json.loads(path.read_text())
        receipt["authorization"] = "f" * 64
        path.write_text(json.dumps(receipt))
        self.denied("tool_invocation_reused")

    def test_use_preflight_refuses_before_receipt_or_publication(self):
        target = ledger if self.objective else config
        preflight = "prepare_context" if self.objective else "_encode"
        before = self.effects()
        with patch.object(target, preflight, side_effect=PodError("record_too_large", "fixture capacity refusal")), self.assertRaises(PodError) as caught:
            self.use()
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertEqual(self.effects(), before)

    def test_included_subscription_stays_routine_and_new_money_needs_person(self):
        self.assertTrue(self.check(category="money", operation="included usage", spending="included_subscription")["execute"])
        memory = self.check(category="money", operation="new commitment", spending="new_commitment")
        self.answer(memory["invocation"], "ask_each_time", PERSON)
        question = self.check(category="money", operation="new commitment", spending="new_commitment")
        before = self.effects()
        with self.assertRaises(PodError) as caught:
            self.answer(question["invocation"], "approve", LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.effects(), before)


class MachineToolUseTests(ToolUseCases, MachineCase, unittest.TestCase):
    def test_withdrawn_vouch_refuses_without_consumption(self):
        super().test_withdrawn_vouch_refuses_without_consumption()

    def test_expired_host_vouch_refuses_without_consumption(self):
        super().test_expired_host_vouch_refuses_without_consumption()

    def test_changed_scope_refuses_even_with_positive_verdict(self):
        super().test_changed_scope_refuses_even_with_positive_verdict()

    def test_verifier_race_refuses_before_use_receipt(self):
        super().test_verifier_race_refuses_before_use_receipt()

    def test_interrupted_consumption_recovers_without_current_authority_or_execution(self):
        super().test_interrupted_consumption_recovers_without_current_authority_or_execution()


class ObjectiveToolUseTests(ToolUseCases, ProductionCase):
    objective = "objective"

    def test_withdrawn_vouch_refuses_without_consumption(self):
        super().test_withdrawn_vouch_refuses_without_consumption()

    def test_expired_host_vouch_refuses_without_consumption(self):
        super().test_expired_host_vouch_refuses_without_consumption()

    def test_changed_scope_refuses_even_with_positive_verdict(self):
        super().test_changed_scope_refuses_even_with_positive_verdict()

    def test_interrupted_consumption_recovers_without_current_authority_or_execution(self):
        super().test_interrupted_consumption_recovers_without_current_authority_or_execution()


if __name__ == "__main__":
    unittest.main()

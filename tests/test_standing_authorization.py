"""A429-A441: standing consent at production boundaries, with disposable ports only."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod import relay, standing
from pod.errors import PodError
from pod.governor import (_read_journal, _record_path, _write_journal, observe_candidate,
                          prepare_candidate, validate_authorization)
from pod.internal import run
from pod.ledger import _path, _read, _write, read
from pod.util import context_decisions, digest
from tests.kernel_support import action, git
from tests.test_relayed_authorization import RelayCase, VERIFIER


BASELINE = "3ba7c64f34da8471b289e92445a929369020f581"
REPOSITORY = Path(__file__).resolve().parents[1]
DECISION = {"provenance": "user_direct", "instruction": "use yolo from now on"}
ANSWER = {"question": "Q1", "text": "Use the existing endpoint", "relay": {"reference": "lead-answer"}}
RESPONDER_VERIFIER = VERIFIER.replace(
    '"owner_source": "authenticated-owner:fixture"',
    '("responder_source" if request["action"] == "answer" else "owner_source"): '
    '("authenticated-responder:fixture" if request["action"] == "answer" else "authenticated-owner:fixture")')


class StandingCase(RelayCase):
    def setUp(self):
        super().setUp()
        self.script.write_text(RESPONDER_VERIFIER)
        for target in ("pod.orca.executable", "pod.github._run"):
            guard = patch(target, side_effect=AssertionError("standing test escaped its disposable ports"))
            guard.start(); self.addCleanup(guard.stop)

    def operation(self, operation="activate", *, objective="objective", **fields):
        if operation in ("activate", "revoke"):
            fields.setdefault("decision", dict(DECISION))
        return run("standing-authorization", {"project": str(self.project), "objective": objective,
                    "owner": "owner", "action": operation, **fields})

    def activate(self, **fields):
        return self.operation(decision={**DECISION, **fields})

    def proposed(self, kind="push", **fields):
        targets = {"push": "origin/candidate", "pr_update": "origin/candidate", "merge": "origin/target",
                   "workflow_dispatch": "ci.yml", "validation_rerun": "ci.yml", "remote_diagnostic": "probe.yml",
                   "cancel_validation": "old-record", "release": "v-next", "deploy": "production"}
        if kind == "remote_diagnostic":
            fields.setdefault("diagnostic", {key: "bounded probe" for key in
                              ("question", "local_limitation", "check", "stopping_condition")})
        fields.setdefault("effects", [])
        return action(kind=kind, candidate=self.case.observed["commit"], target=targets[kind],
                      unit="delivery", **fields)

    def saved(self):
        return _path(self.project, "objective").read_bytes(), _record_path(self.project, "objective").read_bytes()

    def refusal(self, code, callback, *args, **kwargs):
        before, calls = self.saved(), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.case.remote.calls, calls)
        return caught.exception

    def public_status(self, *, by_run=False):
        from pod.status import status
        from pod.operations import OrcaPort
        with patch.object(OrcaPort, "read_native", side_effect=self.case.port.read_native):
            return status(self.project, "run", objective=None if by_run else "objective",
                          worker_rows_fn=lambda _: {"workers": [], "scope": {"run_id": "run"}, "complete": True})


class StandingRecordTests(StandingCase):
    def test_expiry_and_criteria_change_suspend_until_owner_reconfirmation(self):
        self.activate(expires_at="2099-01-01T00:00:00Z")
        with patch("pod.standing._clock", return_value=datetime(2100, 1, 1, tzinfo=timezone.utc)):
            self.assertEqual(self.operation("show")["standing_authorization"]["state"], "expired")
            self.assertEqual(self.operation("revoke")["standing_authorization"]["state"], "revoked")
        self.activate()
        new = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
               "source": {"instruction": "Add PoD#2", "ref": "PoD#2"},
               "check": "PoD#2 passes", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), new], criteria=["PoD#1", "PoD#2"], revision_authority=DECISION)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "suspended")
        self.assertEqual(self.activate()["standing_authorization"]["state"], "active")

    def test_activation_at_intake_and_mid_objective_satisfies_next_check(self):
        self.case.write([self.case.criterion()], objective="at-start", governance={"base_ref": "target"})
        initial = read(self.project, "at-start")
        started = self.operation(objective="at-start", decision={**DECISION, "instruction": "/pod objective yolo"})
        self.assertEqual(started["decision"]["seq"], initial["checkpoint"]["seq"])
        self.assertEqual(read(self.project, "at-start")["admissions"], {})
        before = read(self.project, "objective")
        deferred = self.case.op("governor", action=self.proposed())
        self.assertIn("authorization_missing", [row["code"] for row in deferred["reasons"]])
        result = self.activate()
        row = result["decision"]
        self.assertEqual(row["scope"], list(standing.DEFAULT_SCOPE))
        self.assertEqual(row["exclusions"], list(standing.STANDING_EXCLUSIONS))
        self.assertEqual((row["id"], row["operation"], row["channel"], row["expires_at"]), ("Y1", "activate", "session", None))
        after = read(self.project, "objective")
        self.assertEqual(after["decision_channels"], 3)
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(row["binding"]["scope_digest"], standing.scope_digest(after))
        allowed = self.case.op("governor", action=self.proposed())
        self.assertEqual(allowed["decision"], "ALLOW", allowed)

    def test_relay_activation_confirms_scope_operation_expiry_and_consumes_key(self):
        result = self.activate(channel="relayed", relay={"reference": "standing-one", "owner_source": "caller assertion"},
                               expires_at="2099-01-01T03:00:00+03:00")
        row = result["decision"]
        sent = json.loads(self.requests.read_text().splitlines()[0])
        self.assertEqual(sent, {"schema": relay.SCHEMA, "reference": "standing-one", "action": "standing",
            "objective": "objective", "candidate": None, "pr": None,
            "target": {"repository": "acme/widgets", "remote": "origin", "base": "target"},
            "standing": {"operation": "activate", "scope": list(standing.DEFAULT_SCOPE), "widening": [], "expires_at": "2099-01-01T00:00:00Z"}})
        self.assertEqual(row["relay"]["owner_source"], "authenticated-owner:fixture")
        self.assertEqual(row["relay_verification"]["standing"], sent["standing"])
        self.assertTrue(row["relay_verification"]["verified"])
        from pod.ledger import state_root
        self.assertEqual(len(list((state_root(self.project) / ".relay-authorizations").glob("*.json"))), 1)

    def test_malformed_bounded_decisions_refuse_without_writes(self):
        invalid = ({"scope": []}, {"scope": ["publish", "publish"]}, {"scope": ["unknown"]},
                   {"scope": ["deploy"]}, {"widening": ["secrets", "secrets"]}, {"widening": ["unknown"]},
                   {"expires_at": "2000-01-01T00:00:00Z"}, {"expires_at": "2099-01-01T00:00:00"},
                   {"instruction": "x" * 1025}, {"provenance": "worker"}, {"utc": "2099-01-01T00:00:00Z"})
        for fields in invalid:
            with self.subTest(fields=fields):
                exc = self.refusal("invalid_standing_authorization", self.activate, **fields)
                self.assertIn(exc.detail["record"], ("standing authorization", "standing decision"))
                self.assertTrue(exc.detail["bounded_fields"])
        self.assertFalse(self.requests.exists())

    def test_revoke_is_immediate_and_revoke_when_off_is_a_noop(self):
        before = self.saved()
        self.assertEqual(self.operation("revoke"), {"state": "none", "changed": False})
        self.assertEqual(before, self.saved())
        self.activate()
        revoked = self.operation("revoke", decision={**DECISION, "instruction": "stop yolo"})
        self.assertEqual(revoked["standing_authorization"]["state"], "revoked")
        before = self.saved()
        self.assertEqual(self.operation("revoke"), {"state": "revoked", "changed": False})
        self.assertEqual(before, self.saved())
        held = self.case.op("governor", action=self.proposed())
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_verified_relay_revoke_and_replacement_preserve_history(self):
        self.activate()
        self.activate(scope=["merge"])
        self.assertEqual(self.operation("show")["standing_authorization"]["decision"]["scope"], ["merge"])
        revoked = self.operation("revoke", decision={**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}})
        self.assertEqual(revoked["decision"]["relay_verification"]["standing"],
                         {"operation": "revoke", "scope": None, "widening": None, "expires_at": None})
        self.assertEqual(revoked["standing_authorization"]["history"]["total"], 3)

    def test_private_contract_show_and_native_ownership(self):
        contract = run("contract", {"record": "standing authorization"})["records"]["standing authorization"]
        self.assertEqual(contract["fields"]["action"]["allowed"], ["activate", "revoke", "answer", "show"])
        before = self.saved()
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "none")
        self.assertEqual(before, self.saved())
        self.refusal("coordinator_conflict", run, "standing-authorization",
                     {"project": str(self.project), "objective": "objective", "owner": "foreign", "action": "activate", "decision": DECISION})
        with patch("pod.ledger.require_authority", side_effect=PodError("native_authority_unverified", "fixture refusal")):
            self.refusal("native_authority_unverified", self.activate)


class StandingGovernorTests(StandingCase):
    def test_all_remote_action_kinds_receive_exact_standing_evidence(self):
        self.activate()
        self.case.op("governor-execute", action=self.proposed())
        for kind in ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic", "cancel_validation", "merge", "release"):
            with self.subTest(kind=kind):
                result = self.case.op("governor", action=self.proposed(kind, **({"pr": 23} if kind == "merge" else {})))
                self.assertIn(result["decision"], ("ALLOW", "REUSE"), result)
                consent = self.journal()["actions"][-1]["action"]["authorization"]
                self.assertEqual(consent["scope"], ["merge" if kind == "merge" else "release" if kind == "release" else "publish"])
                self.assertEqual((consent["candidate"], consent["tree"], consent["target"]),
                                 (self.case.observed["commit"], self.case.observed["tree"], "origin/target"))
                self.assertEqual(consent["standing"], {"id": "Y1", "reference": "standing_authorization/decisions/0", "channel": "session"})
                self.assertEqual(consent.get("pr"), 23 if kind == "merge" else None)
                self.assertTrue({"provenance", "instruction", "channel", "relay"}.isdisjoint(consent))
        self.assertEqual(self.journal()["decision_channels"], 3)
        inventory = context_decisions(read(self.project, "objective"), self.journal())
        self.assertEqual([row["kind"] for row in inventory], ["standing_authorization"])
        self.assertEqual([call[0] for call in self.case.remote.calls], ["push"])

    def test_managed_push_and_pr_execute_and_delivery_counts_recorded(self):
        self.activate()
        for kind in ("push", "pr_update"):
            result = self.case.op("governor-execute", action=self.proposed(kind))
            self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        self.assertEqual(sum(call[0] == "push" for call in self.case.remote.calls), 2)
        self.assertEqual(self.case.op("governor-status")["units"]["delivery"]["authorization"]["publish"], "RECORDED")
        self.assertFalse(self.requests.exists())

    def test_managed_dispatch_and_diagnostic_perform_without_consent_wait(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        for kind in ("workflow_dispatch", "remote_diagnostic"):
            result = self.case.op("governor-execute", action=self.proposed(kind))
            self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "pending"), result)
        self.assertEqual(sum(call[0] == "dispatch" for call in self.case.remote.calls), 2)

    def test_managed_failed_job_rerun_keeps_classification_and_attempt_binding(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        run_id = dispatched["receipt"]["provider"]["run_id"]
        self.case.remote.complete(run_id, "failure")
        self.case.op("governor-reconcile", record_id=dispatched["record_id"])
        self.case.op("governor-classify", record_id=dispatched["record_id"],
                     classification={"class": "transient", "reason": "runner disconnected"})
        rerun = self.case.op("governor-execute", action=self.proposed("validation_rerun"))
        self.assertEqual((rerun["decision"], rerun["outcome"]), ("ALLOW", "pending"), rerun)
        self.assertIn(("rerun", run_id, True), self.case.remote.calls)
        self.assertTrue(rerun["receipt"]["provider"]["accepted"])

    def test_managed_cancellation_only_reaches_older_validation_in_its_unit(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        (self.project / "README.md").write_text("next candidate\n")
        git(self.project, "add", "README.md"); git(self.project, "commit", "-qm", "next candidate")
        self.case.candidate = git(self.project, "rev-parse", "HEAD")
        self.case.write(self.case.stored())
        self.case.observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.case.observed)
        cancel = self.proposed("cancel_validation")
        cancel["target"] = dispatched["record_id"]
        result = self.case.op("governor-execute", action=cancel)
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"), result)
        self.assertIn(("cancel", dispatched["receipt"]["provider"]["run_id"]), self.case.remote.calls)

    def test_explicit_exact_consent_takes_precedence_and_bad_consent_never_falls_back(self):
        self.activate()
        result = self.case.op("governor", action=self.proposed(authorization=self.case.consent("publish")))
        self.assertEqual(result["decision"], "ALLOW")
        self.assertNotIn("standing", self.journal()["actions"][-1]["action"]["authorization"])
        wrong = {**self.case.consent("merge"), "candidate": "f" * 40}
        held = self.case.op("governor", action=self.proposed("merge", pr=23, authorization=wrong))
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_excluded_scopes_merge_without_pr_and_unprepared_targets_still_ask(self):
        self.activate(scope=["publish", "merge", "cleanup"])
        for kind in ("deploy", "release", "merge"):
            with self.subTest(kind=kind):
                held = self.case.op("governor", action=self.proposed(kind))
                self.assertEqual(held["decision"], "DEFER")
                reasons = [row for row in held["reasons"] if row["code"] == "authorization_missing"]
                self.assertTrue(reasons)
                self.assertIn("yolo", reasons[0]["detail"])
        for binding, unit in ((None, None), (self.case.observed, {})):
            self.assertIsNone(standing.synthesize(read(self.project, "objective"), binding, unit, {"publish"})[0])

    def test_explicit_deploy_widening_covers_all_needed_effect_scopes(self):
        result = self.activate(widening=["deploy"])
        decision = result["standing_authorization"]["decision"]
        self.assertIn("deploy", decision["scope"])
        self.assertNotIn("deploy", decision["exclusions"])
        self.assertEqual(decision["widening"], ["deploy"])
        allowed = self.case.op("governor", action=self.proposed("deploy"))
        self.assertEqual(allowed["decision"], "ALLOW", allowed)
        combined = self.case.op("governor", action=self.proposed(effects=["deploy:production", "release:next"]))
        self.assertEqual(combined["decision"], "ALLOW", combined)
        self.assertEqual(self.journal()["actions"][-1]["action"]["authorization"]["scope"], ["deploy", "publish", "release"])
        self.activate(scope=["publish"])
        held = self.case.op("governor", action=self.proposed(effects=["release:another"]))
        self.assertEqual(held["decision"], "DEFER")

    def test_revocation_between_admission_and_effect_fails_row_without_effect(self):
        self.activate()
        def revoked(_project):
            self.operation("revoke")
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=revoked), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        row = self.journal()["actions"][-1]
        self.assertEqual((row["outcome"], row["receipt"]["detail"]), ("FAILED", "standing_authorization_inactive"))
        self.assertEqual(self.case.remote.calls, [])

    def test_candidate_movement_between_admission_and_effect_fails_row_without_effect(self):
        self.activate()
        def moved(_project):
            (self.project / "README.md").write_text("changed at standing boundary\n")
            git(self.project, "add", "README.md")
            git(self.project, "commit", "-qm", "boundary change")
            observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
            prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed)
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=moved), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(self.journal()["actions"][-1]["outcome"], "FAILED")
        self.assertEqual(self.case.remote.calls, [])

    def test_target_candidate_and_missing_audit_gates_stay(self):
        self.activate()
        moved = self.proposed()
        moved["candidate"] = "f" * 40
        held = self.case.op("governor", action=moved)
        self.assertIn("superseded_candidate", [row["code"] for row in held["reasons"]])
        wrong_target = self.proposed("merge", pr=23)
        wrong_target["target"] = "origin/foreign"
        held = self.case.op("governor", action=wrong_target)
        self.assertIn("merge_target_mismatch", [row["code"] for row in held["reasons"]])
        from tests.kernel_support import governance_policy
        audit = governance_policy(self.case, "audit", unit="delivery", scope={"paths": ["src"]})
        self.case.write(self.case.stored() + [audit])
        held = self.case.op("governor", action=self.proposed("merge", pr=23))
        self.assertIn("assurance_unbound", [row["code"] for row in held["reasons"]])

    def test_red_hosted_ci_stays_a_failure_and_classification_gate(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        self.assertEqual(dispatched["decision"], "ALLOW", dispatched)
        self.case.remote.complete(dispatched["receipt"]["provider"]["run_id"], "failure")
        failed = self.case.op("governor-reconcile", record_id=dispatched["record_id"])
        self.assertEqual(failed["status"], "FAILED")
        again = self.case.op("governor", action=self.proposed("workflow_dispatch"))
        self.assertEqual(again["decision"], "DEFER")
        self.assertIn("failure_unclassified", [row["code"] for row in again["reasons"]])

    def test_governance_drift_still_refuses_before_a_journal_write(self):
        self.activate()
        git(self.project, "checkout", "-q", "target")
        (self.project / "AGENTS.md").write_text("# Changed policy\nDifferent review rules\n")
        git(self.project, "add", "AGENTS.md")
        git(self.project, "commit", "-qm", "policy changed")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        git(self.project, "checkout", "-q", "candidate")
        self.refusal("governance_changed", self.case.op, "governor", action=self.proposed())

    def test_replacement_or_dirty_prepared_binding_fails_mutation_recheck(self):
        self.activate()
        def replaced(_project):
            self.activate()
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=replaced), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(self.case.remote.calls, [])


class StandingResponderTests(StandingCase):
    def question(self):
        self.case.write(self.case.stored(), questions=[{"id": "Q1", "reference": "question-one", "text": "Which endpoint?"}])

    def test_verified_active_answer_is_responder_observation_and_never_authority(self):
        self.activate(scope=["publish"])
        self.question()
        self.control(changes={"single_use": "answer-one"})
        answered = self.operation("answer", answer=ANSWER)
        row = answered["answer"]
        self.assertEqual((row["id"], row["decided_by"], row["under"]), ("A1", "responder", "Y1"))
        self.assertEqual(row["responder_source"], "authenticated-responder:fixture")
        self.assertEqual(row["relay_verification"]["answer"], {"question": "Q1", "text": ANSWER["text"]})
        self.assertNotIn("owner_source", row["relay_verification"])
        owners = context_decisions(read(self.project, "objective"), self.journal())
        self.assertEqual([item["kind"] for item in owners], ["standing_authorization"])
        held = self.case.op("governor", action=self.proposed("merge", pr=23))
        self.assertEqual(held["decision"], "DEFER")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["questions"][0]["text"], "Which endpoint?")

    def test_without_active_yolo_answers_keep_owner_route_and_unrecorded_questions_refuse(self):
        self.question()
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.activate()
        self.refusal("decision_question_unrecorded", self.operation, "answer", answer={**ANSWER, "question": "absent"})
        self.operation("revoke")
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.assertFalse(self.requests.exists())

    def test_answer_cannot_carry_owner_decisions_or_verifier_proof(self):
        self.activate()
        self.question()
        for extra in ({"scope": ["deploy"]}, {"provenance": "user_direct"}, {"operation": "activate"},
                      {"relay_verification": {"verified": True}}):
            with self.subTest(extra=extra):
                self.refusal("invalid_standing_authorization", self.operation, "answer", answer={**ANSWER, **extra})


class StandingRefusalTests(StandingCase):
    def test_repository_verifier_mismatch_names_the_exact_target_field(self):
        self.control(changes={"target.repository": "other/widgets"})
        exc = self.refusal("relay_verifier_mismatch", self.activate, channel="relayed", relay={"reference": "standing-one"})
        self.assertEqual(exc.detail["fields"], ["target.repository"])
        self.control()
        git(self.project, "remote", "set-url", "origin", "not-a-github-remote")
        exc = self.refusal("relay_verifier_mismatch", self.activate, channel="relayed", relay={"reference": "standing-one"})
        self.assertEqual(exc.detail["fields"], ["target.repository"])

    def test_unconfigured_denied_and_mismatched_relay_activation_write_nothing(self):
        self.configure(None)
        fields = {"channel": "relayed", "relay": {"reference": "standing-one"}}
        self.refusal("relay_verifier_unconfigured", self.activate, **fields)
        self.configure(self.command)
        self.control(changes={"verified": False})
        self.refusal("relay_verifier_denied", self.activate, **fields)
        for changed in ({"objective": "other"}, {"standing.operation": "revoke", "standing.scope": None,
                       "standing.widening": None, "standing.expires_at": None}, {"standing.scope": ["merge"]},
                       {"standing.widening": ["deploy"]}, {"standing.expires_at": "2099-01-01T00:00:00Z"}):
            with self.subTest(changed=changed):
                self.control(changes=changed)
                self.refusal("relay_verifier_mismatch", self.activate, **fields)

    def test_reused_key_and_mismatched_question_or_answer_write_nothing(self):
        self.activate(channel="relayed", relay={"reference": "standing-one"})
        self.refusal("relay_authorization_reused", self.operation, "revoke",
                     decision={**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}})
        self.case.write(self.case.stored(), questions=["Q1"])
        for changed in ({"answer.question": "Q2"}, {"answer.text": "A different answer"}):
            self.control(changes={"single_use": "answer-one", **changed})
            self.refusal("relay_verifier_mismatch", self.operation, "answer", answer=ANSWER)

    def test_caller_cannot_supply_standing_or_verification_and_constraints_still_refuse(self):
        self.activate()
        for extra in ({"standing": {"id": "Y1"}}, {"relay_verification": {"verified": True}}):
            with self.subTest(extra=extra):
                self.refusal("invalid_authorization", self.case.op, "governor",
                             action=self.proposed(authorization={**self.case.consent("publish"), **extra}))
                self.refusal("invalid_standing_authorization", self.activate, **extra)
        self.refusal("relayed_decision_refused", self.case.op, "constraint", action="add", value={
            "id": "route", "kind": "only_agents", "value": ["codex"],
            "provenance": "user_direct", "instruction": "change route", "channel": "relayed",
            "relay": {"reference": "relay-one", "owner_source": "owner"}})


class StandingVisibilityTests(StandingCase):
    def test_delivery_checkpoint_acknowledgement_contains_the_same_mode_and_effects(self):
        self.activate()
        decided = self.case.op("governor", action=self.proposed("merge", pr=23))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge candidate", "candidate")
        merged = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", merged)
        self.case.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": merged})
        acknowledgement = self.case.checkpoint(delivery={"record": decided["record_id"]})
        self.assertEqual(acknowledgement["standing_authorization"], self.operation("show")["standing_authorization"])
        self.assertEqual(acknowledgement["standing_authorization"]["effects"]["items"][0]["outcome"], "PASS")

    def test_status_text_run_selection_and_closure_share_projection_without_verifier(self):
        self.activate(channel="relayed", relay={"reference": "standing-one"}, widening=["deploy"])
        self.case.write(self.case.stored(), questions=["Q1"])
        self.control(changes={"single_use": "answer-one"})
        self.operation("answer", answer=ANSWER)
        self.case.op("governor-execute", action=self.proposed())
        requests = self.requests.read_bytes()
        self.script.unlink()
        before = self.saved()
        objective, selected = self.public_status(), self.public_status(by_run=True)
        expected = self.operation("show")["standing_authorization"]
        self.assertEqual(objective["standing_authorization"], expected)
        self.assertEqual(selected["standing_authorization"], expected)
        self.assertEqual((expected["effects"]["total"], expected["responder_decisions"]["total"]), (1, 1))
        self.assertEqual(expected["decision"]["owner_source"], "authenticated-owner:fixture")
        from pod.status import render
        output = StringIO()
        with redirect_stdout(output):
            render(objective)
        self.assertIn("Mode: yolo (publish, merge, release, install, cleanup, deploy; relayed since", output.getvalue())
        self.assertEqual(before, self.saved())
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        projection = closed["checkpoint"]["closure"]["report"]["standing_authorization"]
        self.assertEqual((projection["mode"], projection["state"]), ("normal", "ended"))
        self.assertEqual(projection["effects"], expected["effects"])
        self.assertEqual(projection["responder_decisions"], expected["responder_decisions"])
        self.assertEqual(self.public_status()["standing_authorization"], projection)
        self.assertEqual(self.requests.read_bytes(), requests)
        self.refusal("objective_closed", self.activate)
        from pod.ledger import closed_snapshot_context
        from pod.obligations import validate_closed_snapshot
        stored = read(self.project, "objective")
        validate_closed_snapshot(stored["checkpoint"], closed_snapshot_context(self.project, "objective", stored))

    def test_projection_bounds_newest_effects_and_preserves_history(self):
        self.activate()
        self.case.op("governor", action=self.proposed())
        context, journal = read(self.project, "objective"), self.journal()
        sample = journal["actions"][0]
        journal["actions"] = [{**deepcopy(sample), "record_id": str(index)} for index in range(70)]
        before = deepcopy((context, journal))
        result = standing.projection(context, journal)
        self.assertEqual((result["effects"]["total"], result["effects"]["truncated"]), (70, True))
        self.assertEqual([row["record_id"] for row in result["effects"]["items"]], list(map(str, range(6, 70))))
        self.assertEqual((context, journal), before)
        self.assertEqual(result["history"]["total"], 1)

    def test_end_states_and_other_objectives_never_inherit_consent(self):
        self.activate()
        context = read(self.project, "objective")
        for transition in ({"continued_by": {"objective": "successor"}}, {"historical_retirement": {"at": "now"}}):
            self.assertEqual(standing.state({**context, **transition}), "ended")
        self.case.write([self.case.criterion()], objective="another", governance={"base_ref": "target"})
        self.assertEqual(self.operation("show", objective="another")["standing_authorization"]["state"], "none")
        copied = self.journal()
        copied["actions"] = []
        _write_journal(_record_path(self.project, "another"), copied)
        held = run("governor", {"project": str(self.project), "objective": "another", "owner": "owner", "action": self.proposed()})
        self.assertEqual(held["decision"], "DEFER")


from tests.pod_tick_support import TickCase


class StandingSourceTests(TickCase):
    def setUp(self):
        super().setUp()
        guard = patch("pod.orca.executable", side_effect=AssertionError("standing source test escaped native double"))
        guard.start(); self.addCleanup(guard.stop)

    def activate(self):
        return self.op("standing-authorization", action="activate", decision=DECISION)

    def summary(self):
        return self.op("standing-authorization", action="show")["standing_authorization"]

    def test_activation_reads_current_issue_and_refuses_unreconciled_body(self):
        self.bind()
        self.assertNotIn("standing_authorization", read(self.project, "objective"))
        before = self.saved()
        self.issue.body += "\nNew requirement"
        self.failure("issue_reconciliation_required", self.activate)
        self.assertEqual(self.saved(), before)
        self.rebind()
        activated = self.activate()
        context = read(self.project, "objective")
        self.assertEqual(activated["standing_authorization"]["state"], "active")
        self.assertEqual(context["standing_authorization"]["observed"], {
            "source": digest(context["checkpoint"]["objective_source"]),
            "body_scope_sha256": hashlib.sha256(self.issue.body.encode()).hexdigest()})

    def test_pod_tick_rebind_keeps_scope_active(self):
        self.bind(); self.activate(); self.prove()
        binding = read(self.project, "objective")["standing_authorization"]["decisions"][-1]["binding"]
        result = self.tick()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.summary()["state"], "active")

        context = read(self.project, "objective")
        self.assertEqual(context["standing_authorization"]["decisions"][-1]["binding"], binding)
        self.assertEqual(context["standing_authorization"]["observed"]["source"], digest(result["objective_source"]))

    def test_checkbox_reconciliation_and_metadata_only_edits_keep_active(self):
        self.bind("- [ ] **PoD#1\r\n  * [X] Extra check\r\n1. [x] Another check\r\n- [X]\r\n")
        self.activate()
        self.issue.updated = "2099-01-01T00:00:00Z"
        self.issue.title = "PES: Renamed widgets"
        self.assertEqual(self.summary()["state"], "active")
        self.issue.body = self.issue.body.replace("[ ]", "[x]").replace("[X]", "[ ]")
        self.rebind()
        self.assertEqual(self.summary()["state"], "active")

    def test_reconciled_requirement_text_suspends_and_owner_reconfirms(self):
        self.bind(); self.activate()
        self.issue.body += "\nA new requirement."
        self.rebind()
        self.assertEqual((self.summary()["state"], self.summary()["reason"]), ("suspended", "scope_changed"))
        self.assertEqual(self.activate()["standing_authorization"]["state"], "active")

    def test_amendment_change_suspends(self):
        from pod.github import issue_intake
        self.issue.amendments[99] = "Original decision"
        amendments = [self.locator + "#issuecomment-99"]
        self.bind(amendments=amendments); self.activate()
        self.issue.amendments[99] = "Changed decision"
        source = issue_intake(self.project, self.locator, port=self.issue, amendments=amendments)["source"]
        self.write(objective_source=source)
        self.assertEqual(self.summary()["state"], "suspended")

    def test_unobserved_rebind_fails_closed_until_matching_observation(self):
        self.bind(); self.activate()
        self.issue.body = self.issue.body.replace("[ ]", "[x]")
        with patch("pod.pod_ticks.refresh"):
            self.rebind()
        self.assertEqual((self.summary()["state"], self.summary()["reason"]), ("suspended", "scope_unobserved"))
        context = read(self.project, "objective")
        from pod.pod_ticks import refresh
        refresh(context, self.project, context["checkpoint"]["objective_source"])
        _write(_path(self.project, "objective"), context)
        self.assertEqual(self.summary()["state"], "active")


class StandingCleanupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_cleanup_plan import CleanupPlanTests
        self.case = CleanupPlanTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.project = self.case.project
        guard = patch("pod.orca.executable", side_effect=AssertionError("cleanup escaped recording ports"))
        guard.start(); self.addCleanup(guard.stop)

    def activate(self, **fields):
        return run("standing-authorization", {"project": str(self.project), "objective": "objective", "owner": "owner",
                                              "action": "activate", "decision": {**DECISION, **fields}})

    def test_cleanup_projection_covers_integrated_asks_unique_and_never_covers_protected(self):
        self.assertEqual(self.case.observed()["standing_authorization"]["covers"], [])
        self.activate()
        integrated = self.case.observed()
        self.assertTrue(integrated["standing_authorization"]["covers"])
        self.assertTrue(all(row["id"] in integrated["standing_authorization"]["covers"] for row in integrated["resources"]
                            if row["class"] == "integrated"))
        valuable = self.case.child / "valuable.txt"
        valuable.write_text("unique material\n")
        unique = self.case.observed()
        worktree = self.case.resource(unique, "worktree")
        self.assertIn(worktree["id"], unique["standing_authorization"]["asks"])
        self.assertNotIn(worktree["id"], unique["standing_authorization"]["covers"])
        self.activate(widening=["unique_deletion"])
        widened = self.case.observed()
        self.assertIn(worktree["id"], widened["standing_authorization"]["covers"])
        self.case.terminals = [{"handle": "foreign", "worktreePath": str(self.case.child)}]
        protected = self.case.observed()
        self.assertEqual(self.case.resource(protected, "worktree")["class"], "protected")
        self.assertNotIn(worktree["id"], protected["standing_authorization"]["covers"])
        self.assertEqual(valuable.read_text(), "unique material\n")


class StandingCapacityTests(StandingCase):
    @classmethod
    def setUpClass(cls):
        # The complete 31-operation history is built once, outside method timing.
        cls.prepared = StandingCase()
        cls.prepared.setUp()
        cls.addClassCleanup(cls.prepared.doCleanups)
        for _ in range(31):
            cls.prepared.activate()
        cls.prepared.case.write(cls.prepared.case.stored(), questions=["Q1"])
        for index in range(64):
            cls.prepared.control(changes={"single_use": f"answer-{index}"})
            cls.prepared.operation("answer", answer=ANSWER)
        cls.context_bytes = _path(cls.prepared.project, "objective").read_bytes()

    def setUp(self):
        # Reuse the real disposable context; no synthetic history bypasses activation.
        self.__dict__.update({key: value for key, value in self.prepared.__dict__.items()
                              if key not in ("_cleanups", "_outcome", "_testMethodName", "_testMethodDoc")})
        _path(self.project, "objective").write_bytes(self.context_bytes)

    def test_activation_reserves_last_slot_so_revoke_always_fits(self):
        self.assertEqual(len(read(self.project, "objective")["standing_authorization"]["decisions"]), 31)
        self.refusal("standing_authorization_full", self.activate)
        revoked = self.operation("revoke")
        self.assertEqual((revoked["decision"]["id"], revoked["standing_authorization"]["state"]), ("Y32", "revoked"))
        self.refusal("standing_authorization_full", self.activate)

    def test_sixty_four_verified_answers_fit_and_next_refuses_without_write(self):
        self.assertEqual(self.operation("show")["standing_authorization"]["responder_decisions"]["total"], 64)
        self.refusal("responder_answers_full", self.operation, "answer", answer=ANSWER)
        context = read(self.project, "objective")
        self.assertEqual([row["id"] for row in context["standing_authorization"]["answers"]], [f"A{index}" for index in range(1, 65)])


class StandingSuccessionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_carried_claim import CarriedCase
        self.case = CarriedCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        guard = patch("pod.orca.executable", side_effect=AssertionError("succession escaped native double"))
        guard.start(); self.addCleanup(guard.stop)

    def test_real_successor_claim_carries_effect_but_never_standing_authority(self):
        def reserve():
            self.case.op("standing-authorization", action="activate", decision=DECISION)
            return self.case.op("governor", action=self.case.action(authorization=None))
        reserved = self.case.lost_with(reserve)
        self.assertEqual(reserved["decision"], "ALLOW")
        self.case.carry()
        predecessor = read(self.case.project, "objective")
        successor = read(self.case.project, "successor")
        self.assertEqual(standing.state(predecessor), "ended")
        self.assertEqual(standing.state(successor), "none")
        self.assertNotIn("standing_authorization", successor)
        carried = self.case.row(reserved["record_id"])
        self.assertEqual(carried["action"]["authorization"]["standing"]["id"], "Y1")
        self.assertIn("carried_from", carried)
        self.assertEqual(context_decisions(successor, self.case.journal("successor")), [])
        proposed = self.case.action(authorization=None)
        held = self.case.mine("governor", action=proposed)
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [reason["code"] for reason in held["reasons"]])

    def test_successor_claim_between_admission_and_effect_preserves_pending_predecessor(self):
        case = self.case
        case.establish()
        case.op("checkpoint", value=case.core(obligations=case.stored(), worktree=case.binding()))
        case.settle(case.admission)
        rows = case.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        case.op("checkpoint", value=case.core(obligations=rows))
        case.op("standing-authorization", action="activate", decision=DECISION)
        admitted_bytes = []
        def claimed(_project):
            admitted_bytes.append(_record_path(case.project, "objective").read_bytes())
            case.lose()
            case.carry()
            return case.remote
        with patch("pod.governor_effects._port", side_effect=claimed), self.assertRaises(PodError) as caught:
            case.op("governor-execute", action=case.action(authorization=None))
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(_record_path(case.project, "objective").read_bytes(), admitted_bytes[0])
        self.assertEqual(case.journal()["actions"][-1]["outcome"], "pending")
        self.assertEqual(case.journal("successor")["actions"][-1]["outcome"], "pending")
        self.assertEqual(case.writes(), [])
        held = case.mine("governor", action=case.action(authorization=None))
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [reason["code"] for reason in held["reasons"]])


class StandingCompatibilityTests(StandingCase):
    @classmethod
    def setUpClass(cls):
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        cls.previous_home = tempfile.TemporaryDirectory(prefix="standing-0180-", dir=parent)
        cls.addClassCleanup(cls.previous_home.cleanup)
        cls.bundle = Path(cls.previous_home.name)
        archive = cls.bundle / "source.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), BASELINE, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def earlier(self, code, *args):
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=REPOSITORY,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(REPOSITORY)))},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ordinary_payload_vocabulary_does_not_select_new_discriminator(self):
        from pod.util import protect_decision_record
        for payload in ({"standing": "value", "channel": "session"},
                        {"standing_authorization": "workflow input", "channel": "session"}):
            protect_decision_record(payload)
            self.assertEqual(payload["decision_channels"], 1)

    def test_normal_closure_keeps_genuine_0180_shape_and_readers_add_normal_mode(self):
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        report = closed["checkpoint"]["closure"]["report"]
        self.assertNotIn("standing_authorization", report)
        self.assertEqual(closed["decision_channels"], 1)
        before = self.saved()
        earlier = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.ledger import _read, closed_snapshot_context
from pod.obligations import report_projection
assert __version__ == "0.18.0"
project, path = map(Path, sys.argv[1:])
state = _read(path)
print(json.dumps(report_projection(state["checkpoint"], closed_snapshot_context(project, "objective", state))))
''', self.project, _path(self.project, "objective"))
        self.assertEqual(report, earlier)
        self.assertEqual(self.public_status()["report"]["standing_authorization"]["state"], "none")
        self.assertEqual(closed["standing_authorization"]["mode"], "normal")
        self.assertEqual(self.saved(), before)

    def test_genuine_0180_refuses_standing_context_and_journal_preserving_bytes(self):
        self.activate()
        self.case.op("governor", action=self.proposed())
        context, journal = _path(self.project, "objective"), _record_path(self.project, "objective")
        before = self.saved()
        results = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.errors import PodError
from pod.governor import _read_journal
from pod.ledger import _read
assert __version__ == "0.18.0"
outcomes = []
for callback, path in ((_read, sys.argv[1]), (_read_journal, sys.argv[2])):
    try:
        callback(Path(path))
    except PodError as exc:
        outcomes.append(exc.code)
    else:
        outcomes.append("incorrectly accepted")
print(json.dumps(outcomes))
''', context, journal)
        self.assertTrue(all(result in ("state_unsupported", "invalid_context") for result in results), results)
        self.assertEqual(self.saved(), before)
        _read(context); self.journal()

    def test_genuine_0180_session_and_verified_relay_records_read_unchanged(self):
        destination = self.case.root / "previous"
        destination.mkdir()
        result = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.governor import _record_path
from pod.ledger import objective_root
from tests.test_relayed_authorization import RelayCase
assert __version__ == "0.18.0"
case = RelayCase(); case.setUp()
try:
    case.verdict()
    target = Path(sys.argv[1])
    target.joinpath("context.json").write_bytes(objective_root(case.project, "objective").joinpath("context.json").read_bytes())
    target.joinpath("governor.json").write_bytes(_record_path(case.project, "objective").read_bytes())
    print(json.dumps({"version": __version__, "writer": "3ba7c64f34da8471b289e92445a929369020f581"}))
finally:
    case.doCleanups()
''', destination)
        self.assertEqual(result, {"version": "0.18.0", "writer": BASELINE})
        from pod.ledger import _validate_context
        for name, reader in (("context.json", lambda p: _validate_context(json.loads(p.read_bytes()))),
                             ("governor.json", _read_journal)):
            path = destination / name
            before = path.read_bytes()
            self.assertEqual(reader(path), json.loads(before))
            self.assertEqual(path.read_bytes(), before)

    def test_publish_and_merge_verifier_requests_are_byte_identical_to_0180(self):
        # Run both genuine writer and candidate with one literal request and the same frozen fixture.
        payload = self.case.root / "request.json"
        for scope in ("publish", "merge"):
            request = self.request(scope)
            payload.write_text(json.dumps(request))
            earlier = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from tests.test_relayed_authorization import RelayCase
assert __version__ == "0.18.0"
case = RelayCase(); case.setUp()
try:
    request = json.loads(Path(sys.argv[1]).read_text())
    request["candidate"] = case.case.observed["commit"]
    request["authorization"].update(candidate=case.case.observed["commit"], tree=case.case.observed["tree"])
    case.case.op("governor", action=request)
    print(json.dumps(case.requests.read_text()))
finally:
    case.doCleanups()
''', payload)
            # Candidate identity is deliberately replaced with the same ids for this byte comparison;
            # all other request facts and serialization are untouched.
            previous = json.loads(earlier)
            request["authorization"].update(candidate=previous["candidate"]["commit"], tree=previous["candidate"]["tree"])
            from pod.governor import _verify_relay
            previous_binding = {"commit": previous["candidate"]["commit"], "tree": previous["candidate"]["tree"]}
            self.requests.unlink(missing_ok=True)
            self.control(changes={"single_use": "comparison-" + scope})
            _verify_relay(self.project, "objective", request["authorization"], previous_binding,
                          self.journal()["units"]["delivery"], self.command, pr=request.get("pr"))
            self.assertEqual(self.requests.read_text(), earlier)

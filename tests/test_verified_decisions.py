"""Additive verifier authority through real objective checkpoint mutations."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pod import config, relay
from pod.errors import PodError
from pod.ledger import _path, read
from tests.decision_policy_support import LOCAL, PERSON, RELAY, VERIFIER, MachineCase
from tests.kernel_support import KernelCase
from tests.test_relayed_authorization import RelayCase


class VerifiedObjectiveCase(KernelCase):
    def setUp(self):
        super().setUp()
        self.controls = self.root / "decision-controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "decision-verifier.py"
        self.verifier.write_text(VERIFIER)
        self.intake()

    configure_verifier = MachineCase.configure_verifier

    def approve_scope(self, name="hands_off"):
        config.set_decision_policy(self.project, name, decision=PERSON)

    def revision(self):
        rows = self.stored()
        rows[0]["check"] = "The revised exact check passes"
        return rows

    def before(self):
        return _path(self.project, "objective").read_bytes()


class VerifiedCheckpointTests(VerifiedObjectiveCase):
    def test_verified_agent_revision_needs_no_previously_recorded_question(self):
        self.approve_scope()
        self.configure_verifier(kind="agent")
        state = self.write(self.revision(), revision_authority=RELAY)
        decision = state["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["provenance"], "agent_decision")
        self.assertEqual(decision["channel"], "relayed")
        self.assertEqual(decision["actor"]["kind"], "agent")
        self.assertEqual(state["decision_channels"], 5)
        self.assertTrue(read(self.project, "objective")["decision_order"])

    def test_local_agent_revision_is_cooperative_and_requires_any_agent(self):
        self.approve_scope()
        state = self.write(self.revision(), revision_authority=LOCAL)
        decision = state["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["actor"], {"kind": "agent", "channel": "agent_session"})
        self.approve_scope("delegate")
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(self.revision(), revision_authority=LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_verified_wrong_scope_has_no_objective_effect(self):
        self.approve_scope()
        self.configure_verifier(changes={"principal.scope.resources": {"objective": "another"}})
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(self.revision(), revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(self.before(), before)

    def test_verified_stale_payload_has_no_objective_effect(self):
        self.approve_scope()
        self.configure_verifier(changes={"decision.payload_sha256": "f" * 64})
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(self.revision(), revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(self.before(), before)

    def test_verified_agent_policy_denial_has_no_objective_effect(self):
        self.configure_verifier(kind="agent")
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(self.revision(), revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_absent_verifier_keeps_legacy_session_revision_working(self):
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(self.revision(), revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(self.before(), before)
        self.write(self.revision(), revision_authority=PERSON)

    def test_policy_change_during_verification_fails_at_commit(self):
        self.approve_scope()
        self.configure_verifier(kind="agent")
        verify = relay.verify_decision
        def changed(command, request):
            verdict = verify(command, request)
            config.set_decision_policy(self.project, "careful", decision=PERSON)
            return verdict
        before = self.before()
        with patch.object(relay, "verify_decision", side_effect=changed):
            with self.assertRaises(PodError) as caught:
                self.write(self.revision(), revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual(self.before(), before)

    def test_stored_actor_or_capability_fields_are_not_public_credentials(self):
        self.approve_scope()
        for fields in ({"actor": {"kind": "person"}}, {"decided_at": "2026-01-01T00:00:00Z"},
                       {"decision_verification": {"verified": True}}):
            before = self.before()
            with self.subTest(fields=list(fields)), self.assertRaises(PodError):
                self.write(self.revision(), revision_authority={**LOCAL, **fields})
            self.assertEqual(self.before(), before)

    def test_packet_freeze_carries_current_summary_and_task_protocol_text(self):
        from pod.internal import run
        self.approve_scope()
        body = self.packet(["O1"])["body"]
        frozen = run("packet", body)
        summary = frozen["body"]["decision_context"]
        self.assertEqual(summary["decision_generation"], config.load(self.project)["decision_generation"])
        self.assertNotIn("decision_verification", json.dumps(summary))
        self.assertIn("native Dispatch mail", frozen["task_instructions"])
        self.assertIn("A saved summary grants no permission", frozen["task_instructions"])
        self.assertEqual(frozen["coordinator_route"]["status"], "unavailable")
        self.assertNotEqual(frozen["packet_id"], self.packet(["O1"])["packet_id"])
        for supplied in (None, "malformed", {"decision_generation": 999}, summary):
            self.assertEqual(run("packet", {**body, "decision_context": supplied})["body"]["decision_context"], summary)

    def test_refrozen_stale_summary_uses_current_policy_and_grants_no_tool_authority(self):
        from pod.internal import run
        self.approve_scope()
        old = run("packet", self.packet(["O1"])["body"])
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        new = run("packet", old["body"])
        self.assertNotEqual(new["packet_id"], old["packet_id"])
        self.assertEqual(new["body"]["decision_context"]["policy"]["preset"], "careful")
        question = config.tool_check(self.project, "fixture-tool", "secrets", "read")
        before = config.personal_path(self.project).read_bytes(), self.before()
        with self.assertRaises(PodError) as caught:
            config.tool_answer(self.project, question["invocation"], "hands_off", decision=LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual((config.personal_path(self.project).read_bytes(), self.before()), before)


class VerifiedMapKinds(VerifiedObjectiveCase):
    """Each map decision uses the public checkpoint and independently fixed vouch."""

    def operation(self, kind):
        from tests.kernel_support import git, governance_commit
        if kind == "governance":
            self.candidate = governance_commit(self, "AGENTS.md", "A changed fixture instruction\n", "policy")
            git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
            return lambda: self.write(self.stored(), governance_refresh=True,
                governance={"base_ref": "refs/remotes/origin/target", "base": self.candidate}, revision_authority=RELAY)
        if kind == "reopen":
            rows = self.stored()
            rows[0].pop("executor")
            rows[0].update(state="satisfied", evidence=[self.proof("O1")])
            self.write(rows, close=True)
            return lambda: self.write(self.stored(), reopen=True, revision_authority=RELAY)
        if kind == "withdrawal":
            rows = self.stored()
            rows[0].pop("executor")
            rows[0].update(state="withdrawn", withdrawal={"by": "agent_decision", "reason": "Exact scoped withdrawal"})
            return lambda: self.write(rows, revision_authority=RELAY)
        return lambda: self.write(self.revision(), revision_authority={**RELAY, "question": "not-previously-recorded"})

    def fixed(self, operation):
        from datetime import datetime, timezone
        from tests.decision_policy_support import save_vouch
        vouch = save_vouch(self, operation)
        # Reopen must be later than the genuine closure, without sleeping.
        vouch["decided_at"] = datetime.now(timezone.utc).isoformat()
        self.controls.write_text(json.dumps({"saved_verdict": vouch}))
        return vouch

    def observable(self):
        from pod.ledger import state_root
        return self.before(), {p.name: p.read_bytes() for p in (state_root(None) / ".relay-authorizations").glob("*.json")}

    def accepted(self, kind):
        self.approve_scope()
        self.configure_verifier()
        operation = self.operation(kind)
        self.fixed(operation)
        result = operation()
        state = read(self.project, "objective")
        self.assertEqual(state["decision_channels"], 5)
        rows = result["checkpoint"]["revision_decisions"]
        self.assertEqual(rows[-1]["actor"]["kind"], "agent")
        self.assertIn(kind, rows[-1]["decision_binding"]["kinds"])
        if kind == "withdrawal":
            row = state["checkpoint"]["obligations"][0]["withdrawal"]
            self.assertEqual(row["by"], "agent_decision")
            self.assertEqual(row["accepted_seq"], state["checkpoint"]["seq"])
        if kind == "reopen":
            self.assertIsNone(state["checkpoint"]["closure"])

    def rejected_bindings(self, kind):
        self.approve_scope()
        self.configure_verifier()
        operation = self.operation(kind)
        fixed = self.fixed(operation)
        before = self.observable()
        for path in ("payload_sha256", "state_sha256"):
            wrong = deepcopy(fixed)
            wrong["decision"][path] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=path), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual(self.observable(), before)
        wrong = deepcopy(fixed)
        wrong["principal"]["scope"]["kinds"] = ["compaction"]
        self.controls.write_text(json.dumps({"saved_verdict": wrong}))
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(self.observable(), before)

    def rejected_policy(self, kind):
        self.configure_verifier()
        operation = self.operation(kind)
        self.fixed(operation)
        before = self.observable()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.observable(), before)

    def rejected_absent(self, kind):
        self.approve_scope()
        operation = self.operation(kind)
        before = self.observable()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(self.observable(), before)

    def test_question_saved_vouch(self):
        self.accepted("question")

    def test_question_payload_state_scope(self):
        self.rejected_bindings("question")

    def test_question_policy(self):
        self.rejected_policy("question")

    def test_question_absent(self):
        self.rejected_absent("question")

    def test_governance_saved_vouch(self):
        self.accepted("governance")

    def test_governance_payload_state_scope(self):
        self.rejected_bindings("governance")

    def test_governance_policy(self):
        self.rejected_policy("governance")

    def test_governance_absent(self):
        self.rejected_absent("governance")

    def test_withdrawal_saved_vouch(self):
        self.accepted("withdrawal")

    def test_withdrawal_payload_state_scope(self):
        self.rejected_bindings("withdrawal")

    def test_withdrawal_policy(self):
        self.rejected_policy("withdrawal")

    def test_withdrawal_absent(self):
        self.rejected_absent("withdrawal")

    def test_reopen_saved_vouch(self):
        self.accepted("reopen")

    def test_reopen_payload_state_scope(self):
        self.rejected_bindings("reopen")

    def test_reopen_policy(self):
        self.rejected_policy("reopen")

    def test_reopen_absent(self):
        self.rejected_absent("reopen")


from tests.issue41_support import ProductionCase


class VerifiedReportDecisionTests(ProductionCase):
    configure_verifier = MachineCase.configure_verifier

    def setUp(self):
        super().setUp()
        self.intake(self.sub("S"))
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.controls = self.root / "report-controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "report-verifier.py"
        self.verifier.write_text(VERIFIER)
        self.configure_verifier()
        self.frozen = self.packet(["S"], role="investigate", resolves="What changed?", stop_condition="Report once")
        self.admission = self.start("report-decision", self.frozen)["admission"]
        self.settle(self.admission)
        state = read(self.project, "objective")
        self.proposed = {"seq": state["checkpoint"]["seq"] + 1, "update": {"O1": {"check": "Revised by exact report decision"},
                         "S": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}},
                         "revision_authority": RELAY}

    def operation(self):
        return self.report(self.admission, self.frozen, map=deepcopy(self.proposed))

    def before(self):
        return _path(self.project, "objective").read_bytes()

    def test_report_map_revision_uses_saved_vouch_and_replays_read_only(self):
        from tests.decision_policy_support import save_vouch
        save_vouch(self, self.operation)
        self.operation()
        state = read(self.project, "objective")
        self.assertEqual(state["checkpoint"]["obligations"][0]["check"], "Revised by exact report decision")
        self.assertEqual(state["checkpoint"]["revision_decisions"][-1]["actor"]["kind"], "agent")
        before = self.before()
        self.assertTrue(self.operation()["readback"])
        self.assertEqual(self.before(), before)

    def test_report_stale_scope_and_policy_leave_report_unconsumed(self):
        from tests.decision_policy_support import save_vouch
        original = save_vouch(self, self.operation)
        before = self.before()
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(original)
            if field == "scope":
                wrong["principal"]["scope"]["kinds"] = ["compaction"]
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError):
                self.operation()
            self.assertEqual(self.before(), before)
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        save_vouch(self, self.operation)
        with self.assertRaises(PodError) as caught:
            self.operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_report_receipt_before_context_write_recovers_identical_decision(self):
        from pod import ledger
        from tests.decision_policy_support import save_vouch
        save_vouch(self, self.operation)
        before = self.before()
        with patch.object(ledger, "publish_context", side_effect=OSError("fixture interrupted report replacement")), self.assertRaises(OSError):
            self.operation()
        self.assertEqual(self.before(), before)
        self.operation()
        self.assertEqual(read(self.project, "objective")["checkpoint"]["revision_decisions"][-1]["actor"]["kind"], "agent")


class VerifiedConstraintTests(VerifiedObjectiveCase):

    def rule(self, identity="rule-one", authority=LOCAL):
        return {"id": identity, "kind": "route_rule", "value": {
            "constraints": [], "efforts": None, "roles": {}, "fallback": None}, **authority}

    def add(self, value):
        from pod.internal import run
        return run("constraint", {"project": str(self.project), "objective": "objective", "owner": "owner",
                                  "action": "add", "value": value})

    def test_rule_replacement_is_atomic_and_preserves_independent_restrictions(self):
        self.approve_scope()
        self.add({"id": "independent", "kind": "only_agents", "provenance": "issue", "value": ["codex"]})
        self.add(self.rule())
        result = self.add(self.rule("rule-two"))
        rows = {row["id"]: row for row in result["constraints"]}
        self.assertTrue(rows["independent"].get("active", True))
        self.assertFalse(rows["rule-one"]["active"])
        self.assertTrue(rows["rule-two"].get("active", True))
        self.assertEqual(rows["rule-two"]["provenance"], "agent_decision")

    def test_verified_rule_acceptance_without_question_and_policy_denial(self):
        from pod import policy
        from tests.decision_policy_support import save_vouch
        self.configure_verifier(kind="agent")
        save_vouch(self, lambda: self.add(self.rule(authority=RELAY)))
        result = self.add(self.rule(authority=RELAY))
        self.assertEqual(result["constraints"][0]["actor"]["kind"], "agent")
        config.set_decision_policy(self.project, "custom", decision=PERSON,
                                   categories={**policy.preset("careful"), "model_choices": "person"})
        save_vouch(self, lambda: self.add(self.rule("rule-two", RELAY)))
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.add(self.rule("rule-two", RELAY))
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_rule_stale_payload_wrong_scope_and_missing_verifier_are_atomic(self):
        self.approve_scope()
        for changes in ({"decision.payload_sha256": "f" * 64},
                        {"principal.scope.resources": {"objective": "different"}}):
            self.configure_verifier(changes=changes)
            before = self.before()
            with self.subTest(changes=list(changes)), self.assertRaises(PodError):
                self.add(self.rule(authority=RELAY))
            self.assertEqual(self.before(), before)

    def test_rule_local_agent_cannot_cross_careful_trust_boundary(self):
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.add(self.rule())
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)


class VerifiedRoutingTests(VerifiedObjectiveCase):
    def install_rule(self, **value):
        from pod.ledger import constraints_update
        self.approve_scope()
        constraints_update(self.project, "objective", owner="owner", action="add", value={
            "id": "requested-rule", "kind": "route_rule", **LOCAL,
            "value": {"constraints": [], "efforts": None, "roles": {}, "fallback": None, **value}})

    def test_worker_admission_uses_semantic_role_and_actual_rule(self):
        self.write([*self.stored(), self.sub("S")])
        self.install_rule(roles={"worker": ["codex/gpt-6.1-sol/medium"]})
        frozen = self.packet(["S"], routing_role="worker")
        started = self.start("worker-role", frozen)
        decision = started["admission"]["route_decision"]
        self.assertEqual(decision["routing_role"], "worker")
        self.assertEqual(decision["rule_id"], "requested-rule")
        self.assertEqual(decision["stage"], "primary")

    def test_auditor_role_comes_from_typed_served_assurance(self):
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}, "audit_role": "auditor"}
        self.write([*self.stored(), assurance])
        self.install_rule()
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.start("audit-role", self.packet(["A"], role="review", routing_role="reviewer"))
        self.assertEqual(caught.exception.code, "routing_role_mismatch")
        self.assertEqual(self.before(), before)

        result = self.start("audit-role", self.packet(["A"], role="review", routing_role="auditor"))
        self.assertEqual(result["admission"]["route_decision"]["routing_role"], "auditor")

    def test_missing_audit_designation_refuses_new_composite_review(self):
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.write([*self.stored(), assurance])
        self.install_rule()
        with self.assertRaises(PodError) as caught:
            self.start("audit-role", self.packet(["A"], role="review", routing_role="reviewer"))
        self.assertEqual(caught.exception.code, "routing_role_required")

    def test_fallback_reference_cannot_supply_caller_authority(self):
        self.write([*self.stored(), self.sub("S")])
        self.install_rule(fallback=[{"roles": ["worker"], "on": ["unavailable"],
                                     "routes": ["codex/gpt-6.1-sol/medium"]}])
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.start("fallback-role", self.packet(["S"], routing_role="worker", fallback_of="f" * 64))
        self.assertEqual(caught.exception.code, "route_fallback_unavailable")
        self.assertEqual(self.before(), before)

    def test_fallback_uses_actual_settled_primary_and_current_failure(self):
        from pod.ledger import route_failure
        from pod.operations import guarded_start
        from tests.test_native_retry import RetryPort
        original = self.port
        self.port = RetryPort()
        self.port.placement = original.placement
        self.write([*self.stored(), self.sub("S")])
        self.install_rule(roles={"worker": ["codex/gpt-6.1-sol/medium"]},
                          fallback=[{"roles": ["worker"], "on": ["unavailable"], "routes": ["codex/gpt-6-luna/medium"]}])
        first_packet = self.packet(["S"], role="investigate", routing_role="worker", resolves="Which route is available?", stop_condition="Report once")
        primary = self.start("fallback-task", first_packet)["admission"]
        self.port.workers[primary["native_binding"]["dispatchId"]].update(
            state="stopped", outcome="failed", facts={"liveness": {"verdict": "exited", "source": "resource_release"},
            "resource": {"state": "released", "releaseState": "released"}})
        route_failure(self.project, "objective", owner="owner", admission_id=primary["admission_id"],
                      kind="unavailable", source="settled fixture attempt", native_reader=lambda row:
                      self.port.read_native("owner", authority_runs=(row["run_id"],), assignments=(row,)))
        second_packet = self.packet(["S"], role="investigate", routing_role="worker", resolves="Which route is available?", stop_condition="Report once", fallback_of=primary["admission_id"],
                                   route={**first_packet["body"]["route"], "model": "gpt-6-luna"})
        result = guarded_start(self.project, "objective", owner="owner", run="run", task="fallback-task",
                               plan_revision="plan", frozen_packet=second_packet, port=self.port,
                               retry_of=primary["admission_id"])["admission"]
        self.assertEqual(result["route_decision"]["stage"], "fallback")
        self.assertEqual(result["route_decision"]["fallback_of"], primary["admission_id"])
        self.assertEqual(result["route_decision"]["effective"]["model"], "gpt-6-luna")


class VerifiedFailureClearTests(VerifiedObjectiveCase):
    def operation(self):
        from pod.ledger import route_failure
        self.write([*self.stored(), self.sub("S")])
        admission = self.start("failed-route", self.packet(["S"]))["admission"]
        self.settle(admission)
        fields = {"owner": "owner", "admission_id": admission["admission_id"], "kind": "auth_failed", "source": "fixture",
                  "native_reader": lambda row: self.port.read_native("owner", authority_runs=(row["run_id"],), assignments=(row,))}
        route_failure(self.project, "objective", **fields)
        return lambda: route_failure(self.project, "objective", **fields, clear=True, cleared_by="agent", decision=RELAY)

    def test_saved_failure_clear_keeps_actual_agent_and_replays_read_only(self):
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        operation = self.operation()
        save_vouch(self, operation)
        result = operation()
        self.assertEqual(result["failures"][0]["cleared_by"], "agent")
        self.assertEqual(result["failures"][0]["clear_decision"]["actor"]["kind"], "agent")
        before = self.before()
        operation()
        self.assertEqual(self.before(), before)

    def test_failure_clear_payload_state_scope_refuse_without_effects(self):
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        operation = self.operation()
        fixed = save_vouch(self, operation)
        before = self.before()
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(fixed)
            if field == "scope":
                wrong["principal"]["scope"]["kinds"] = ["compaction"]
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual(self.before(), before)

    def test_failure_clear_custom_policy_denies_verified_agent(self):
        from pod import policy
        from tests.decision_policy_support import save_vouch
        categories = policy.preset("careful")
        categories["model_choices"] = "person"
        config.set_decision_policy(self.project, "custom", decision=PERSON, categories=categories)
        self.configure_verifier()
        operation = self.operation()
        save_vouch(self, operation)
        before = self.before()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_failure_clear_absent_verifier_keeps_failure_active(self):
        operation = self.operation()
        before = self.before()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(self.before(), before)



class VerifiedGovernorTests(RelayCase):
    def setUp(self):
        super().setUp()
        self.script.write_text(VERIFIER)
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        from pod import ledger
        observed = ledger.require_authority
        self.native_binding = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        self.native_reads = 0
        def read_authority(*args, **kwargs):
            self.native_reads += 1
            value = observed(*args, **kwargs)
            return {**value, "native": {**value["native"], "binding": deepcopy(self.native_binding),
                    "rpc_id": self.native_reads, "observed_at": "observation-" + str(self.native_reads)}}
        replacement = patch.object(ledger, "require_authority", side_effect=read_authority)
        replacement.start()
        self.addCleanup(replacement.stop)

    def generic_request(self, scope="publish"):
        value = self.request(scope)
        value["authorization"].update(deepcopy(RELAY))
        return value

    def test_saved_agent_publish_vouch_rechecks_and_executes_at_public_boundary(self):
        from tests.decision_policy_support import save_vouch
        value = self.generic_request()
        save_vouch(self, lambda: self.case.op("governor-execute", action=value))
        result = self.case.op("governor-execute", action=value)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["outcome"], "PASS")
        saved = self.journal()["actions"][-1]["action"]["authorization"]
        self.assertEqual(saved["provenance"], "agent_decision")
        self.assertEqual(saved["actor"]["kind"], "agent")

    def test_saved_exact_merge_vouch_accepts_only_the_prepared_pr_target(self):
        from tests.decision_policy_support import save_vouch
        value = self.generic_request("merge")
        operation = lambda: self.case.op("governor", action=value)
        save_vouch(self, operation)
        self.assertEqual(operation()["decision"], "ALLOW")
        saved = self.journal()["actions"][-1]["action"]["authorization"]
        self.assertEqual(saved["actor"]["kind"], "agent")
        self.assertEqual(saved["scope"], ["merge"])
        self.assertEqual(saved["pr"], 23)

    def test_authorization_payload_state_and_scope_refuse_without_provider_effects(self):
        from tests.decision_policy_support import save_vouch
        value = self.generic_request()
        operation = lambda: self.case.op("governor-execute", action=value)
        original = save_vouch(self, operation)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(original)
            if field == "scope":
                wrong["principal"]["scope"]["kinds"] = ["compaction"]
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual((self.journal(), self.case.remote.calls), before)

    def test_authorization_policy_denial_preserves_journal_and_provider(self):
        from tests.decision_policy_support import save_vouch
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        operation = lambda: self.case.op("governor-execute", action=self.generic_request())
        save_vouch(self, operation)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual((self.journal(), self.case.remote.calls), before)

    def test_authorization_absent_verifier_preserves_journal_and_provider(self):
        self.configure(None)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.generic_request())
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual((self.journal(), self.case.remote.calls), before)

    def test_policy_can_change_during_lookup_and_then_blocks_journal_commit(self):
        from tests.decision_policy_support import save_vouch
        operation = lambda: self.case.op("governor-execute", action=self.generic_request())
        save_vouch(self, operation)
        verify = relay.verify_decision
        def changed(command, request):
            result = verify(command, request)
            config.set_decision_policy(self.project, "careful", decision=PERSON)
            return result
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        with patch.object(relay, "verify_decision", side_effect=changed), self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual((self.journal(), self.case.remote.calls), before)



    def test_publish_grant_remains_applicable_to_pr_after_journal_progress(self):
        from pod.ledger import state_root
        from tests.kernel_support import action
        from tests.decision_policy_support import save_vouch
        value = self.generic_request()
        save_vouch(self, lambda: self.case.op("governor-execute", action=value))
        self.case.op("governor-execute", action=value)
        receipts = {path.name: path.read_bytes() for path in (state_root(None) / ".relay-authorizations").glob("*.json")}
        update = action(kind="pr_update", unit="delivery", candidate=self.case.observed["commit"],
                        target="origin/candidate", effects=[], authorization=deepcopy(value["authorization"]))
        result = self.case.op("governor-execute", action=update, pull_request={"title": "Fixture candidate", "body": "Fixture evidence"})
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in (state_root(None) / ".relay-authorizations").glob("*.json")})
        self.assertGreater(self.native_reads, 2)

    def test_publish_grant_remains_applicable_to_ci_after_journal_progress(self):
        from pod.ledger import state_root
        from tests.kernel_support import action
        from tests.decision_policy_support import save_vouch
        value = self.generic_request()
        save_vouch(self, lambda: self.case.op("governor-execute", action=value))
        self.case.op("governor-execute", action=value)
        receipts = {path.name: path.read_bytes() for path in (state_root(None) / ".relay-authorizations").glob("*.json")}
        dispatch = action(kind="workflow_dispatch", unit="delivery", candidate=self.case.observed["commit"],
                          target="ci.yml", effects=[], authorization=deepcopy(value["authorization"]))
        result = self.case.op("governor-execute", action=dispatch)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertTrue(any(call[0] == "dispatch" for call in self.case.remote.calls))
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in (state_root(None) / ".relay-authorizations").glob("*.json")})

    def test_changed_native_generation_between_vouch_and_commit_refuses_effect(self):
        from pod.governor import _record_path
        from tests.decision_policy_support import save_vouch
        value = self.generic_request()
        save_vouch(self, lambda: self.case.op("governor-execute", action=value))
        verify = relay.verify_decision
        def changed(command, request):
            result = verify(command, request)
            self.native_binding["consumer_generation"] += 1
            return result
        before = _record_path(self.project, "objective").read_bytes(), deepcopy(self.case.remote.calls)
        with patch.object(relay, "verify_decision", side_effect=changed), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=value)
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual((_record_path(self.project, "objective").read_bytes(), self.case.remote.calls), before)

    def exception_operation(self):
        import yaml
        from pod.config import personal_path
        from tests.kernel_support import action
        path = personal_path(self.project)
        document = yaml.safe_load(path.read_text())
        document.setdefault("waste_governor", {})["exceptions"] = [{
            "id": "fixture-efficiency", "action": "efficiency_exception", "objective": "objective",
            "kinds": ["workflow_dispatch"], "reason": "Only this remote fixture can answer", "valid_until": "2027-01-01T00:00:00Z"}]
        document["waste_governor"]["preflight"] = ["fixture-check"]
        path.write_text(yaml.safe_dump(document))
        self.case.write(self.case.stored())
        # The existing exact session grant publishes the candidate first.
        value = self.request()
        value["authorization"].pop("channel")
        value["authorization"].pop("relay")
        self.case.op("governor-execute", action=value)
        proposed = action(kind="workflow_dispatch", unit="delivery", candidate=self.case.observed["commit"],
                          target="ci.yml", effects=[], authorization=deepcopy(value["authorization"]))
        exception = {"grant": "fixture-efficiency", "reason": "Only runner answers", "by": "coordinator", **RELAY}
        return lambda: self.case.op("governor", action=proposed, exception=exception)

    def test_saved_exception_vouch_rejoins_existing_personal_grant(self):
        from tests.decision_policy_support import save_vouch
        operation = self.exception_operation()
        save_vouch(self, operation)
        result = operation()
        self.assertEqual(result["decision"], "ALLOW")
        self.assertTrue(result["exception"]["applied"])
        saved = self.journal()["actions"][-1]["exception"]
        self.assertEqual(saved["actor"]["kind"], "agent")
        self.assertEqual(saved["decision_binding"]["kind"], "governor_exception")

    def test_exception_wrong_payload_state_and_scope_preserve_journal(self):
        from tests.decision_policy_support import save_vouch
        operation = self.exception_operation()
        fixed = save_vouch(self, operation)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(fixed)
            if field == "scope":
                wrong["principal"]["scope"]["kinds"] = ["compaction"]
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual((self.journal(), self.case.remote.calls), before)

    def test_exception_policy_denial_preserves_journal(self):
        from tests.decision_policy_support import save_vouch
        operation = self.exception_operation()
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        save_vouch(self, operation)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual((self.journal(), self.case.remote.calls), before)

    def test_exception_absent_verifier_preserves_journal(self):
        operation = self.exception_operation()
        self.configure(None)
        before = deepcopy(self.journal()), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual((self.journal(), self.case.remote.calls), before)


class VerifiedStandingTests(VerifiedObjectiveCase):
    def standing_request(self, action="activate", authority=RELAY, **fields):
        from pod.standing import scope_digest
        value = {**authority, **({"scope": ["publish"], "widening": [],
                                  "scope_digest": scope_digest(read(self.project, "objective"))} if action == "activate" else {}), **fields}
        return {"project": str(self.project), "objective": "objective", "owner": "owner", "action": action, "decision": value}

    def test_saved_scoped_standing_activation_keeps_agent_origin(self):
        from pod.internal import run
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier(kind="agent")
        request = self.standing_request()
        save_vouch(self, lambda: run("standing-authorization", request))
        result = run("standing-authorization", request)
        self.assertEqual(result["standing_authorization"]["state"], "active")
        self.assertEqual(result["decision"]["provenance"], "agent_decision")
        self.assertEqual(result["decision"]["actor"]["kind"], "agent")
        self.assertEqual(read(self.project, "objective")["decision_channels"], 5)

    def test_standing_local_actor_and_policy_scope_money_invariants(self):
        from pod.internal import run
        self.approve_scope()
        result = run("standing-authorization", self.standing_request(authority=LOCAL))
        self.assertEqual(result["decision"]["channel"], "agent_session")
        self.assertEqual(result["standing_authorization"]["state"], "active")
        before = self.before()
        with self.assertRaises(PodError) as caught:
            run("standing-authorization", self.standing_request(authority=LOCAL, widening=["paid_resources"], spending="new_commitment"))
        self.assertEqual(caught.exception.code, "standing_money_scope")
        self.assertEqual(self.before(), before)

    def test_standing_payload_state_and_scope_refuse_without_effects(self):
        from pod.internal import run
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        request = self.standing_request()
        operation = lambda: run("standing-authorization", request)
        fixed = save_vouch(self, operation)
        before = self.before()
        for field in ("payload_sha256", "state_sha256", "scope"):
            wrong = deepcopy(fixed)
            if field == "scope":
                wrong["principal"]["scope"]["kinds"] = ["compaction"]
            else:
                wrong["decision"][field] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": wrong}))
            with self.subTest(binding=field), self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
            self.assertEqual(self.before(), before)

    def test_standing_policy_denial_preserves_inactive_state(self):
        from pod.internal import run
        from tests.decision_policy_support import save_vouch
        self.configure_verifier()
        operation = lambda: run("standing-authorization", self.standing_request())
        save_vouch(self, operation)
        before = self.before()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.before(), before)

    def test_standing_missing_verifier_preserves_inactive_state(self):
        from pod.internal import run
        before = self.before()
        with self.assertRaises(PodError) as caught:
            run("standing-authorization", self.standing_request())
        self.assertEqual(caught.exception.code, "relay_verifier_unconfigured")
        self.assertEqual(self.before(), before)

    def test_future_activation_refuses_but_future_stop_is_clamped_to_commit(self):
        from pod.internal import run
        from pod.standing import decision_time
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        activate = lambda: run("standing-authorization", self.standing_request())
        future = save_vouch(self, activate)
        future["decided_at"] = "2099-01-01T00:00:00Z"
        self.controls.write_text(json.dumps({"saved_verdict": future}))
        before = self.before()
        with self.assertRaises(PodError) as caught:
            activate()
        self.assertEqual(caught.exception.code, "decision_future")
        self.assertEqual(self.before(), before)
        stop = lambda: run("standing-authorization", self.standing_request(action="revoke"))
        future = save_vouch(self, stop)
        future["decided_at"] = "2099-01-01T00:00:00Z"
        self.controls.write_text(json.dumps({"saved_verdict": future}))
        result = stop()
        self.assertEqual(result["standing_authorization"]["state"], "revoked")
        self.assertEqual(decision_time(result["decision"]), result["decision"]["utc"])


from tests import test_issue_revision as issue_fixture


class VerifiedIssueRevisionTests(issue_fixture.IssueRevisionCase):
    def setUp(self):
        super().setUp()
        self.controls = self.root / "saved-source-vouch.json"
        self.controls.write_text("{}")
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.begin()

    def verify_output(self, command, request):
        if request["action"] != "decision":
            return super().verify_output(command, request)
        self.requests.append(deepcopy(request))
        controls = json.loads(self.controls.read_text())
        if "saved_verdict" in controls:
            return json.dumps(controls["saved_verdict"]).encode()
        binding = request["decision"]
        return json.dumps({**request, "verified": True, "decided_at": "2026-10-01T00:00:00Z",
            "single_use": "source-vouch:" + binding["request_id"], "principal": {"kind": "agent", "identity": "source-fixture",
            "scope": {"kinds": binding.get("kinds", [binding["kind"]]), "categories": binding["categories"],
                      "resources": binding["resources"]}}}).encode()

    def generic_proposal(self):
        value = self.proposal()
        value["revision_authority"].update(deepcopy(RELAY))
        return value

    def test_saved_source_vouch_accepts_exact_revision_without_a_question(self):
        from tests.decision_policy_support import save_vouch
        value = self.generic_proposal()
        save_vouch(self, lambda: self.revise(value))
        result = self.revise(value)
        self.assertEqual(result["checkpoint"]["criteria"], ["PoD#1", "PoD#2"])
        decision = read(self.project, "objective")["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["provenance"], "agent_decision")
        self.assertEqual(decision["actor"]["kind"], "agent")
        self.assertIn("scope_facts", decision)
        self.assertTrue(all(request["action"] == "decision" for request in self.requests))
        self.assertEqual(read(self.project, "objective")["decision_channels"], 5)

    def test_new_source_vouch_refuses_stale_payload_state_and_wrong_scope(self):
        from tests.decision_policy_support import save_vouch
        value = self.generic_proposal()
        saved = save_vouch(self, lambda: self.revise(value))
        before = self.saved(), self.receipts()
        for key in ("payload_sha256", "state_sha256"):
            changed = deepcopy(saved)
            changed["decision"][key] = "f" * 64
            self.controls.write_text(json.dumps({"saved_verdict": changed}))
            with self.subTest(field=key), self.assertRaises(PodError):
                self.revise(value)
            self.assertEqual((self.saved(), self.receipts()), before)
        changed = deepcopy(saved)
        changed["principal"]["scope"]["resources"] = {"objective": "different"}
        self.controls.write_text(json.dumps({"saved_verdict": changed}))
        with self.assertRaises(PodError):
            self.revise(value)
        self.assertEqual((self.saved(), self.receipts()), before)

    def test_new_source_decision_policy_and_missing_verifier_have_no_effects(self):
        value = self.generic_proposal()
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        self.refused_revision(value, "decision_policy_denied")
        self.configure(None)
        self.refused_revision(value, "relay_verifier_unconfigured")

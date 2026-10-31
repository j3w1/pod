"""Verifier authority never replaces lifecycle facts; all native ports are fixtures."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pod import config, relay
from pod.errors import PodError
from pod.ledger import read, state_root
from tests import decision_policy_support as decisions
from tests import test_historical_retirement as retirement
from tests import test_owner_handoff as handoff
from tests import test_runtime_continuity as continuity
from tests import test_succession as succession


class LifecycleMatrix:
    """Shared assertions, discovered once for each concrete production boundary."""

    configure_verifier = decisions.MachineCase.configure_verifier

    def setUp(self):
        super().setUp()
        self.controls = self.root / "decision-controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "decision-verifier.py"
        self.verifier.write_text(decisions.VERIFIER)
        self.prepare_lifecycle()
        self.maps_before = {name: read(self.project, name)["checkpoint"]["obligations"]
                            for name in self.objectives}

    def choose_policy(self, *, denied=False):
        config.set_decision_policy(self.project, "careful" if denied else "hands_off",
                                   decision=decisions.PERSON)

    def request(self, origin):
        return {**deepcopy(self.facts), **deepcopy(origin)}

    def durable(self):
        """Include every objective, Governor record and machine use receipt."""
        root = state_root(self.project)
        from tests.common import authority_files
        return {str(path.relative_to(root)): raw for path, raw in authority_files(root).items()
                if path.suffix == ".json"}

    def resources(self):
        return deepcopy((self.port.starts, self.port.workers,
                         getattr(self, "mutations", []),
                         vars(self.remote) if hasattr(self, "remote") else {}))

    def unchanged_refusal(self, decision, code):
        before, resources = self.durable(), self.resources()
        with self.assertRaises(PodError) as caught:
            self.apply_decision(decision)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.durable(), before)
        self.assertEqual(self.resources(), resources)

    def stored_decision(self):
        raise NotImplementedError

    def test_scoped_verified_agent_without_recorded_question(self):
        self.choose_policy()
        self.configure_verifier(kind="agent")
        for objective in self.objectives:
            self.assertEqual(read(self.project, objective)["checkpoint"]["questions"], [])
        before = self.resources()
        receipts = {path for path in self.durable() if ".relay-authorizations/" in path}
        result = self.apply_decision(self.request(decisions.RELAY))
        self.assert_accepted(result)
        stored = self.stored_decision()
        self.assertEqual(stored["provenance"], "agent_decision")
        self.assertEqual(stored["channel"], "relayed")
        self.assertEqual(stored["actor"]["kind"], "agent")
        self.assertEqual(set(stored["decision_binding"]["categories"]), set(self.categories))
        for objective in self.changed_objectives:
            self.assertEqual(read(self.project, objective)["decision_channels"], 5)
        self.assertEqual(self.resources()[:2], before[:2])
        consumed = {path for path in self.durable() if ".relay-authorizations/" in path}
        self.assertEqual(len(consumed - receipts), 1)

    def test_stale_verified_payload_has_no_consumption_or_effect(self):
        self.choose_policy()
        self.configure_verifier(changes={"decision.payload_sha256": "f" * 64})
        self.unchanged_refusal(self.request(decisions.RELAY), "relay_verifier_mismatch")

    def test_stale_verified_state_has_no_consumption_or_effect(self):
        self.choose_policy()
        self.configure_verifier(changes={"decision.state_sha256": "f" * 64})
        self.unchanged_refusal(self.request(decisions.RELAY), "relay_verifier_mismatch")

    def test_current_policy_controls_agent_acceptance(self):
        self.choose_policy(denied=True)
        self.configure_verifier(kind="agent")
        self.unchanged_refusal(self.request(decisions.RELAY), "decision_policy_denied")

    def test_agent_scope_cannot_name_another_resource(self):
        self.choose_policy()
        self.configure_verifier(changes={"principal.scope.resources": {"objective": "different"}})
        self.unchanged_refusal(self.request(decisions.RELAY), "relay_verifier_mismatch")

    def test_missing_verifier_denies_relay_and_preserves_existing_session(self):
        self.unchanged_refusal(self.request(decisions.RELAY), "relay_verifier_unconfigured")
        self.assert_accepted(self.apply_decision(self.request(decisions.PERSON)))

    def test_policy_generation_changes_during_verification(self):
        self.choose_policy()
        self.configure_verifier(kind="agent")
        verify = relay.verify_decision
        expected = []

        def changed(command, request):
            verdict = verify(command, request)
            self.choose_policy(denied=True)
            expected.append(self.durable())
            return verdict

        resources = self.resources()
        with patch.object(relay, "verify_decision", side_effect=changed):
            with self.assertRaises(PodError) as caught:
                self.apply_decision(self.request(decisions.RELAY))
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual(len(expected), 1)
        self.assertEqual(self.durable(), expected[0])
        self.assertEqual(self.resources(), resources)

    def test_native_or_resource_mismatch_is_not_decision_authority(self):
        self.choose_policy()
        self.configure_verifier(kind="agent")
        self.break_facts()
        self.unchanged_refusal(self.request(decisions.RELAY), self.factual_error)

    def test_local_agent_session_keeps_cooperative_origin(self):
        self.choose_policy()
        self.assert_accepted(self.apply_decision(self.request(decisions.LOCAL)))
        stored = self.stored_decision()
        self.assertEqual(stored["provenance"], "agent_decision")
        self.assertEqual(stored["actor"], {"kind": "agent", "channel": "agent_session"})
        self.assertNotIn("decision_verification", stored)


class VerifiedClaimTests(LifecycleMatrix, succession.SuccessionCase):
    categories = ("scope_changes", "coordinator_changes")
    objectives = changed_objectives = ("objective", "successor")
    factual_error = "predecessor_live"

    def prepare_lifecycle(self):
        self.predecessor()
        self.successor()
        self.facts = self.claim()["facts"]

    def apply_decision(self, decision):
        return self.claim(decision)

    def assert_accepted(self, result):
        self.assertEqual(result["status"], "claimed")
        self.assertEqual(read(self.project, "objective")["continued_by"]["objective"], "successor")
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "objective")
        self.assertEqual(read(self.project, "objective")["owner"], "owner")
        self.assertEqual(read(self.project, "successor")["owner"], "next")
        for name, rows in self.maps_before.items():
            self.assertEqual(read(self.project, name)["checkpoint"]["obligations"], rows)

    def stored_decision(self):
        return read(self.project, "objective")["continued_by"]

    def break_facts(self):
        self.terminals["owner"] = "live"


class VerifiedRetirementTests(LifecycleMatrix, retirement.RetirementCase):
    categories = ("scope_changes",)
    objectives = changed_objectives = ("objective",)
    factual_error = "retirement_decision_mismatch"

    def prepare_lifecycle(self):
        self.delivered(unknown_start=False)
        decision = self.decide(delivery=("fixture delivery evidence",))
        self.facts = {key: value for key, value in decision.items()
                      if key not in ("provenance", "instruction")}

    def apply_decision(self, decision):
        return self.retire(decision)

    def assert_accepted(self, result):
        self.assertEqual(result["status"], "retired")
        entry = read(self.project, "objective")["historical_retirement"]
        self.assertEqual(entry["released"], [self.where()])
        self.assertEqual(read(self.project, "objective")["owner"], "owner")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"],
                         self.maps_before["objective"])
        self.assertTrue(self.project.is_dir())

    def stored_decision(self):
        return read(self.project, "objective")["historical_retirement"]

    def break_facts(self):
        self.instances[self.where()] = "wt2:local:replacement"

    def test_native_or_resource_mismatch_is_not_decision_authority(self):
        # Retirement returns per-objective factual refusals so other eligible rows
        # can proceed; it does not turn this predicate into a top-level exception.
        self.choose_policy()
        self.configure_verifier(kind="agent")
        self.break_facts()
        before, resources = self.durable(), self.resources()
        result = self.apply_decision(self.request(decisions.RELAY))
        self.assertEqual(result["status"], "refused")
        [row] = result["objectives"]
        self.assertEqual(row["condition"], self.factual_error)
        self.assertIn("locations", row["changed"])
        self.assertEqual(self.durable(), before)
        self.assertEqual(self.resources(), resources)


class VerifiedHandoffTests(LifecycleMatrix, handoff.HandoffCase):
    categories = ("scope_changes", "coordinator_changes")
    objectives = changed_objectives = ("objective",)
    factual_error = "owner_handoff_refused"

    def prepare_lifecycle(self):
        self.establish()
        self.lose()
        self.facts = self.status()["owner_handoff"]["facts"]

    def apply_decision(self, decision):
        return self.handoff(decision)

    def assert_accepted(self, result):
        self.assertEqual(self.entry()["state"], "done")
        self.assertEqual(read(self.project, "objective")["owner"], "next")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"],
                         self.maps_before["objective"])
        self.assertEqual(self.mutations, [["orchestration", "run-use", "--id", "run", "--json"]])

    def stored_decision(self):
        return self.entry()["decision"]

    def break_facts(self):
        self.current["id"] = "different-run"

    def pending_verified_handoff(self):
        self.choose_policy()
        self.configure_verifier(kind="agent")
        decision = self.request(decisions.RELAY)
        self.mode = "lost"
        with self.assertRaises(PodError) as caught:
            self.apply_decision(decision)
        self.assertEqual(caught.exception.code, "owner_handoff_refused")
        self.assertEqual(self.entry()["state"], "pending")
        self.assertEqual(read(self.project, "objective")["owner"], "owner")
        self.assertEqual(len(self.mutations), 1)
        self.mode = "success"
        return decision

    def test_verified_pending_handoff_resumes_without_second_native_mutation(self):
        decision = self.pending_verified_handoff()
        receipts = {path: data for path, data in self.durable().items()
                    if ".relay-authorizations/" in path}
        self.assert_accepted(self.apply_decision(decision))
        self.assertEqual({path: data for path, data in self.durable().items()
                          if ".relay-authorizations/" in path}, receipts)

    def test_verified_pending_identity_loss_records_abort_without_rebinding(self):
        decision = self.pending_verified_handoff()
        before = self.durable()
        journal = {path: data for path, data in before.items() if path.endswith("governor.json")}
        next(iter(self.port.workers.values()))["worktree"] = "different-worktree"
        resources = self.resources()
        with self.assertRaises(PodError):
            self.apply_decision(decision)
        self.assertEqual(self.entry()["state"], "aborted")
        self.assertEqual(self.entry()["abort_reason"], "identity_differs")
        self.assertEqual(read(self.project, "objective")["owner"], "owner")
        after = self.durable()
        self.assertEqual({path: data for path, data in after.items() if path in journal}, journal)
        self.assertEqual(self.resources(), resources)
        self.assertEqual({path: data for path, data in before.items()
                          if ".relay-authorizations/" in path},
                         {path: data for path, data in after.items()
                          if ".relay-authorizations/" in path})


class VerifiedContinuityTests(LifecycleMatrix, continuity.ContinuityCase):
    categories = ("continuity",)
    objectives = changed_objectives = ("objective",)
    factual_error = "native_authority_unverified"

    def test_current_policy_controls_agent_acceptance(self):
        # Continuity stays automatic even in Careful; it has no legal denial policy.
        self.choose_policy(denied=True)
        self.configure_verifier(kind="agent")
        self.assert_accepted(self.apply_decision(self.request(decisions.RELAY)))

    def prepare_lifecycle(self):
        self.port.reports_binding = False
        self.started()
        self.port.reports_binding = True
        self.change_runtime()
        with self.assertRaises(PodError) as caught:
            self.checkpoint_op()
        self.assertEqual(caught.exception.code, continuity.AMBIGUOUS)
        decision = self.decision(caught.exception)
        self.facts = {key: value for key, value in decision.items()
                      if key not in ("provenance", "instruction")}

    def apply_decision(self, decision):
        return self.owner_op(decision)

    def assert_accepted(self, result):
        self.assertEqual(result["status"], "rebound")
        self.assertEqual(read(self.project, "objective")["owner"], "owner")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["obligations"],
                         self.maps_before["objective"])
        self.assertEqual(read(self.project, "objective")["checkpoint"]["native_refs"],
                         [{"runId": "run", "runtime": "runtime-2"}])

    def stored_decision(self):
        return self.history()[-1]["decision"]

    def break_facts(self):
        next(iter(self.port.workers.values()))["worktree"] = "different-worktree"


if __name__ == "__main__":
    unittest.main()

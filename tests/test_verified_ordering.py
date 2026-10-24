"""Actual objective publication boundaries and retained per-key decision clocks."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pod import ledger
from pod.errors import PodError
from pod.ledger import state_root
from tests.decision_policy_support import PERSON, RELAY
from tests.test_verified_decisions import VerifiedObjectiveCase


class VerifiedPublicationTests(VerifiedObjectiveCase):
    def request(self):
        return self.core(obligations=deepcopy(self.revision()), revision_authority=PERSON)

    def commit(self, request):
        return ledger.checkpoint(self.project, "objective", owner="owner", value=deepcopy(request),
                                 native={"runtime": "runtime", "delegation": "available"})

    def receipts(self):
        return {path.name: path.read_bytes() for path in (state_root(None) / ".relay-authorizations").glob("*.json")}

    def test_preflight_failure_has_no_receipt_or_context_effects(self):
        request = self.request()
        before, receipts = self.before(), self.receipts()
        with patch.object(ledger, "prepare_context", side_effect=OSError("fixture before publication")), self.assertRaises(OSError):
            self.commit(request)
        self.assertEqual(self.before(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_receipt_before_replace_recovers_only_identical_transition(self):
        request = self.request()
        before = self.before()
        with patch.object(ledger, "publish_context", side_effect=OSError("fixture before replacement")), self.assertRaises(OSError):
            self.commit(request)
        self.assertEqual(self.before(), before)
        receipts = self.receipts()
        self.assertTrue(receipts)
        accepted = self.commit(request)
        self.assertEqual(accepted["checkpoint"]["obligations"][0]["check"], "The revised exact check passes")
        self.assertEqual(self.receipts(), receipts)

    def test_failure_after_replace_is_committed_and_retry_is_readback(self):
        request = self.request()
        publish = ledger.publish_context
        def committed(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("fixture lost response after replacement")
        with patch.object(ledger, "publish_context", side_effect=committed), self.assertRaises(OSError):
            self.commit(request)
        before, receipts = self.before(), self.receipts()
        result = self.commit(request)
        self.assertTrue(result["committed"])
        self.assertTrue(result["readback"])
        self.assertEqual(self.before(), before)
        self.assertEqual(self.receipts(), receipts)


class VerifiedChronologyTests(VerifiedObjectiveCase):
    def test_equal_verified_time_cannot_replace_the_same_obligation(self):
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        first = self.revision()
        save_vouch(self, lambda: self.write(first, revision_authority=RELAY))
        self.write(first, revision_authority=RELAY)
        second = self.stored()
        second[0]["check"] = "A conflicting decision at the identical verified time"
        operation = lambda: self.write(second, revision_authority=RELAY)
        save_vouch(self, operation)
        before = self.before()
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual(self.before(), before)

    def test_identical_verified_time_on_an_unrelated_key_remains_eligible(self):
        from tests.decision_policy_support import save_vouch
        self.approve_scope()
        self.configure_verifier()
        first = self.revision()
        save_vouch(self, lambda: self.write(first, revision_authority=RELAY))
        self.write(first, revision_authority=RELAY)
        self.write([*self.stored(), self.sub("S")])
        second = self.stored()
        second[1]["check"] = "A separate scoped decision"
        operation = lambda: self.write(second, revision_authority=RELAY)
        save_vouch(self, operation)
        result = operation()
        self.assertEqual(result["checkpoint"]["obligations"][1]["check"], second[1]["check"])
        self.assertEqual(len(result["decision_order"]), 2)

    def test_delayed_relay_cannot_override_newer_session_on_same_key(self):
        self.approve_scope()
        self.write(self.revision(), revision_authority=PERSON)
        self.configure_verifier(kind="agent")
        rows = self.revision()
        rows[0]["check"] = "A different delayed check"
        before = self.before()
        with self.assertRaises(PodError) as caught:
            self.write(rows, revision_authority=RELAY)
        self.assertEqual(caught.exception.code, "decision_stale")
        self.assertEqual(self.before(), before)

    def test_session_commit_clock_and_verified_decision_clock_are_distinct(self):
        self.approve_scope()
        self.configure_verifier(kind="agent")
        accepted = self.write(self.revision(), revision_authority=RELAY)
        decision = accepted["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["decided_at"], "2026-10-01T00:00:00Z")
        self.assertNotEqual(decision["recorded_at"], decision["decided_at"])
        fresh = self.revision()
        fresh[0]["check"] = "A fresh session check"
        accepted = self.write(fresh, revision_authority=PERSON)
        decision = accepted["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["recorded_at"], decision["decided_at"])

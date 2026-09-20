from datetime import datetime, timedelta, timezone
import unittest

from pod.context import bind_context, context_valid, steer, feedback
from pod.errors import PodError
from pod.quota import reset_intent, validate_snapshot


class ContextQuotaTests(unittest.TestCase):
    def test_context_binding_and_steering(self):
        record = bind_context(candidate="c", instructions=[], requirements="r",
                              sources=[], policy_revision="p", summary="bounded")
        self.assertTrue(context_valid(record, record["binding"]))
        self.assertFalse(context_valid(record, {**record["binding"], "candidate": "changed"}))
        plan = {"schema": "pod-plan/v1", "revision": 1, "criteria": ["works"],
                "assignments": ["a", "b"], "candidate": "c"}
        with self.assertRaises(PodError):
            steer(plan, changes={"reason": "new goal", "criteria": ["different"]},
                  authorized_acceptance_change=False)
        changed = steer(plan, changes={"reason": "narrow a", "assignments": ["b"]},
                        authorized_acceptance_change=False)
        self.assertEqual(changed["affected"], ["a"])
        self.assertEqual(changed["plan"]["revision"], 2)

    def test_feedback_does_not_write_or_infer_cost(self):
        self.assertIsNone(feedback([{"route": "sol", "outcome": "blocked"}])["suggestion"])
        result = feedback([{"route": "sol", "outcome": "blocked"}] * 3)
        self.assertFalse(result["writes"])
        self.assertNotIn("cost", result)

    def test_quota_metadata_and_reset_exact_grant(self):
        now = datetime.now(timezone.utc)
        snapshot = {"schema": "pod-quota/v1", "provider": "codex", "account": "a",
                    "bucket": "b", "windows": [{"name": "hour"}], "observed_at": now.isoformat(),
                    "source": "supported", "confidence": "observed", "unknowns": []}
        self.assertEqual(validate_snapshot(snapshot)["bucket"], "b")
        grant = {"id": "one", "action": "reset_credit", "account": "a", "bucket": "b",
                 "valid_until": (now + timedelta(minutes=5)).isoformat(), "max_units": 1}
        with self.assertRaises(PodError):
            reset_intent(grant, account="other", bucket="b", operation_id="op",
                         supported_idempotency=True, now=now)
        with self.assertRaises(PodError):
            reset_intent(grant, account="a", bucket="b", operation_id="op",
                         supported_idempotency=False, now=now)
        result = reset_intent(grant, account="a", bucket="b", operation_id="op",
                              supported_idempotency=True, now=now)
        self.assertFalse(result["repeat_allowed"])

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import unittest

from pod.config import DEFAULT, route_identity
from pod.routing import preview, replay
from pod.util import digest


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
ACCOUNT_IDENTITY = "a" * 64


def assessment(**changes):
    value = {"method": "delegate", "responsibility": "bounded edit", "complexity": "complex",
             "risk": "low", "size": "small", "uncertainty": "low", "verifiability": "unit test",
             "capabilities": [], "context": [], "reason": "independent edit", "bounded": True}
    value.update(changes)
    return value


def setup():
    p = deepcopy(DEFAULT)
    p["routing"]["complex"] = {"model": "sol", "effort": "high", "context": "max"}
    for alias in ("sol", "astra"):
        p["models"][alias].update({"account": ACCOUNT_IDENTITY,
                                    "approved": True, "approval_ref": "personal-1",
                                    "billing": "included", "efforts": ["high", "medium"],
                                    "capabilities": []})
        p["models"][alias]["approval_route"] = route_identity(p["models"][alias])
    return {"policy": p, "revision": digest(p)}


def caps():
    return {alias: {"agent": "codex", "model": model, "account": ACCOUNT_IDENTITY,
                    "efforts": ["high", "medium"],
                    "capabilities": [], "suitable_for": ["complex"],
                    "billing_preflight": True, "fanout_control": True, "bucket": "shared",
                    "context_control": "native_per_launch",
                    "contexts": {"256k": 262144, "max": 900000}}
            for alias, model in (("sol", "gpt-6-sol"), ("astra", "gpt-6-astra"))}


def quota(percent=50):
    return {ACCOUNT_IDENTITY: {"schema": "pod-quota/v1", "provider": "codex",
                     "account": ACCOUNT_IDENTITY,
                     "bucket": "shared", "observed_at": NOW.isoformat(), "source": "supported_metadata",
                     "confidence": "observed", "unknowns": [],
                     "windows": [{"name": "hour", "remaining_percent": percent}],
                     "remaining_percent": percent}}


class RoutingTests(unittest.TestCase):
    def test_preferred_and_replay(self):
        e = setup()
        result = preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW)
        self.assertEqual(result["selected"]["alias"], "sol")
        capture = {"assessment": assessment(), "effective": e, "capabilities": caps(),
                   "quotas": quota(), "at": NOW.isoformat()}
        self.assertEqual(replay(capture), result)
        self.assertEqual(result["selected"]["context"], "max")
        self.assertEqual(result["selected"]["effective_context"], 900000)

    def test_context_needs_explicit_native_capability_and_respects_provider_ceiling(self):
        e = setup()
        unavailable = caps()
        unavailable["sol"].pop("context_control")
        self.assertEqual(preview(assessment(), e, capabilities=unavailable,
                                 quotas=quota(), strict_pin="sol", now=NOW)["status"], "blocked")
        excessive = caps()
        excessive["sol"]["contexts"]["max"] = 1_050_001
        result = preview(assessment(), e, capabilities=excessive, quotas=quota(), now=NOW)
        self.assertIn("provider ceiling", " ".join(result["rejections"]["sol"]))

        clamped = setup()
        clamped["policy"]["routing"]["complex"]["context"] = "256k"
        clamped["revision"] = digest(clamped["policy"])
        capability = caps()
        capability["sol"]["contexts"]["256k"] = 200000
        decision = preview(assessment(), clamped, capabilities=capability,
                           quotas=quota(), strict_pin="sol", now=NOW)
        self.assertEqual(decision["selected"]["context"], "256k")
        self.assertEqual(decision["selected"]["effective_context"], 200000)

    def test_strict_pin_blocks_fallback_and_safety_refusal_blocks_all(self):
        e = setup()
        cap = caps()
        del cap["sol"]
        pinned = preview(assessment(), e, capabilities=cap, quotas=quota(),
                         strict_pin="sol", now=NOW)
        self.assertEqual(pinned["status"], "blocked")
        self.assertIsNone(pinned["selected"])
        self.assertIn("strict pin excludes substitution", pinned["rejections"]["astra"])
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(),
                                 safety_refusal=True, now=NOW)["status"], "blocked")

    def test_unknown_quota_is_left_to_objective_admission_and_exhausted_blocks(self):
        e = setup()
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas={},
                                 now=NOW)["status"], "usable")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(0),
                                 now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(4),
                                 now=NOW)["status"], "usable")
        self.assertEqual(preview(assessment(bounded=False), e, capabilities=caps(), quotas=quota(4),
                                 now=NOW)["status"], "blocked")

    def test_provider_bucket_and_every_window_bind_route(self):
        e = setup()
        snapshot = quota(70)
        snapshot[ACCOUNT_IDENTITY]["windows"].append({"name": "week", "remaining_percent": 0})
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot, now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 now=NOW + timedelta(minutes=10))["status"], "blocked")
        snapshot[ACCOUNT_IDENTITY]["windows"][1]["remaining_percent"] = 70
        snapshot[ACCOUNT_IDENTITY]["provider"] = "claude"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 now=NOW)["status"], "usable")
        snapshot[ACCOUNT_IDENTITY]["provider"] = "codex"
        snapshot[ACCOUNT_IDENTITY]["bucket"] = "other"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 now=NOW)["status"], "usable")

    def test_changed_alias_and_paid_route_need_separate_grants(self):
        e = setup()
        e["policy"]["models"]["sol"]["model"] = "changed"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW)["selected"]["alias"], "astra")
        e = setup()
        e["policy"]["models"]["sol"]["billing"] = "paid"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW)["selected"]["alias"], "astra")
        e["policy"]["policy"]["spending_grants"] = [{"id": "g", "action": "paid_usage",
                                                          "account": ACCOUNT_IDENTITY,
            "model": "gpt-6-sol", "objective": "o",
            "valid_until": (NOW + timedelta(days=1)).isoformat(), "max_units": 1}]
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW, objective="o")["selected"]["alias"], "sol")

    def test_fallback_effort_is_independently_approved_and_location_restricts(self):
        e = setup()
        e["policy"]["models"]["astra"]["efforts"] = ["medium"]
        e["policy"]["models"]["sol"]["locations"] = ["eu"]
        cap = caps()
        cap["astra"]["efforts"] = ["medium"]
        result = preview(assessment(data_location="us"), e, capabilities=cap, quotas=quota(), now=NOW)
        self.assertEqual(result["selected"]["alias"], "astra")
        self.assertEqual(result["selected"]["effort"], "medium")

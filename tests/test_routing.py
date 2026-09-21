from copy import deepcopy
from datetime import datetime, timezone, timedelta
import unittest

from pod.config import DEFAULT, route_identity
from pod.routing import preview, replay
from pod.util import digest


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def assessment(**changes):
    value = {"method": "delegate", "responsibility": "bounded edit", "complexity": "complex",
             "risk": "low", "size": "small", "uncertainty": "low", "verifiability": "unit test",
             "capabilities": [], "context": [], "reason": "independent edit", "bounded": True}
    value.update(changes)
    return value


def setup():
    p = deepcopy(DEFAULT)
    for alias in ("sol", "terra"):
        p["models"][alias].update({"account": "acct", "approved": True, "approval_ref": "personal-1",
                                    "billing": "included", "efforts": ["high"], "capabilities": []})
        p["models"][alias]["approval_route"] = route_identity(p["models"][alias])
    return {"policy": p, "revision": digest(p)}


def caps():
    return {alias: {"agent": "codex", "model": model, "account": "acct", "efforts": ["high"],
                    "capabilities": [], "suitable_for": ["complex"],
                    "billing_preflight": True, "fanout_control": True, "bucket": "shared"}
            for alias, model in (("sol", "gpt-5.6-sol"), ("terra", "gpt-5.6-terra"))}


def quota(percent=50):
    return {"acct": {"schema": "pod-quota/v1", "provider": "codex", "account": "acct",
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

    def test_strict_pin_blocks_fallback_and_safety_refusal_blocks_all(self):
        e = setup()
        cap = caps()
        del cap["sol"]
        self.assertEqual(preview(assessment(), e, capabilities=cap, quotas=quota(),
                                 strict_pin="sol", now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(),
                                 safety_refusal=True, now=NOW)["status"], "blocked")

    def test_unknown_quota_one_account_and_exhausted(self):
        e = setup()
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas={},
                                 occupancy={"acct": 1}, now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(0),
                                 now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(4),
                                 now=NOW)["status"], "usable")
        self.assertEqual(preview(assessment(bounded=False), e, capabilities=caps(), quotas=quota(4),
                                 now=NOW)["status"], "blocked")

    def test_provider_bucket_and_every_window_bind_route(self):
        e = setup()
        snapshot = quota(70)
        snapshot["acct"]["windows"].append({"name": "week", "remaining_percent": 0})
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot, now=NOW)["status"], "blocked")
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 now=NOW + timedelta(minutes=10))["status"], "blocked")
        snapshot["acct"]["windows"][1]["remaining_percent"] = 70
        snapshot["acct"]["provider"] = "claude"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 occupancy={"acct": 1}, now=NOW)["status"], "blocked")
        snapshot["acct"]["provider"] = "codex"
        snapshot["acct"]["bucket"] = "other"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=snapshot,
                                 occupancy={"acct": 1}, now=NOW)["status"], "blocked")

    def test_changed_alias_and_paid_route_need_separate_grants(self):
        e = setup()
        e["policy"]["models"]["sol"]["model"] = "changed"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW)["selected"]["alias"], "terra")
        e = setup()
        e["policy"]["models"]["sol"]["billing"] = "paid"
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW)["selected"]["alias"], "terra")
        e["policy"]["policy"]["spending_grants"] = [{"id": "g", "action": "paid_usage", "account": "acct",
            "model": "gpt-5.6-sol", "objective": "o",
            "valid_until": (NOW + timedelta(days=1)).isoformat(), "max_units": 1}]
        self.assertEqual(preview(assessment(), e, capabilities=caps(), quotas=quota(), now=NOW, objective="o")["selected"]["alias"], "sol")

    def test_fallback_effort_is_independently_approved_and_location_restricts(self):
        e = setup()
        e["policy"]["models"]["terra"]["efforts"] = ["medium"]
        e["policy"]["models"]["sol"]["locations"] = ["eu"]
        cap = caps()
        cap["terra"]["efforts"] = ["medium"]
        result = preview(assessment(data_location="us"), e, capabilities=cap, quotas=quota(), now=NOW)
        self.assertEqual(result["selected"]["alias"], "terra")
        self.assertEqual(result["selected"]["effort"], "medium")

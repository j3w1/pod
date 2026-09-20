import unittest

from pod.release import LIVE_CHECKS, REQUIRED_GATES, release_gate
from pod.errors import PodError


def row(gate, outcome="PASS"):
    value = {"schema": "pod-validation/v1", "candidate": "commit", "tree": "tree",
             "host": "fixture", "utc": "2026-09-20T00:00:00Z", "gate": gate,
             "command": "bounded check", "outcome": outcome, "report": "artifact sha256:abc"}
    if gate.startswith("live_"):
        value["checks"] = {name: "PASS" for name in LIVE_CHECKS}
    return value


class ReleaseGateTests(unittest.TestCase):
    def test_missing_live_core_blocks_even_with_offline_pass(self):
        records = [row(gate) for gate in REQUIRED_GATES if not gate.startswith("live_")]
        result = release_gate("commit", "tree", records)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gates"]["live_codex_windows"], "NOT_RUN")
        self.assertFalse(result["release_authorized"])

    def test_all_records_only_request_owner_decision(self):
        result = release_gate("commit", "tree", [row(gate) for gate in REQUIRED_GATES])
        self.assertEqual(result["status"], "owner_decision_required")
        self.assertFalse(result["release_authorized"])
        partial = [row(gate) for gate in REQUIRED_GATES]
        partial[0]["candidate"] = "old"
        self.assertEqual(release_gate("commit", "tree", partial)["gates"][REQUIRED_GATES[0]], "NOT_RUN")

    def test_live_record_needs_all_subchecks(self):
        records = [row(gate) for gate in REQUIRED_GATES]
        live = next(x for x in records if x["gate"] == "live_codex_linux")
        live["checks"]["adoption"] = "NOT_RUN"
        self.assertEqual(release_gate("commit", "tree", records)["gates"]["live_codex_linux"], "UNAVAILABLE")

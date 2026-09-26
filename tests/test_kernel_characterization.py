"""Public projection and refusal ordering across kernel seams."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pod.cli import execute, parser
from pod.errors import PodError
from tests.kernel_support import KernelCase


class StatusProjectionTests(unittest.TestCase):
    def test_unselected_run_has_the_existing_json_shape(self):
        preferences = {"path": "/fixture/config.yaml", "revision": "r", "mode": "all",
                       "eligible": [], "not_set": [], "max_active": 2, "errors": []}
        with patch("pod.status.load_config", return_value=preferences), \
             patch("pod.status.running_identity", return_value={"version": "0.6.4", "bundle_digest": "d"}), \
             patch("pod.cli.current_run", return_value={"run": None}):
            result = execute(parser().parse_args(["status", "--json"]), Path("/fixture"))
        self.assertEqual(result, {
            "schema": "pod-cli/v4", "status": "selection_required", "run": None,
            "bundle_identity": {"running": {"version": "0.6.4", "bundle_digest": "d"},
                                "checkpoint": None, "drift": None},
            "preferences": preferences, "constraints": [], "route_decisions": [],
            "route_mismatch": False, "active_constraints": [], "effective_unknown": False,
            "installed_version_drift": False, "blocker": None,
            "next_safe_action": "select a native Run", "usage": "unknown", "cost": "unknown"})


class CheckpointRefusalOrderTests(KernelCase):
    def test_malformed_checkpoint_refuses_before_native_authority_read(self):
        with patch("pod.ledger.require_authority", side_effect=AssertionError("authority was read")) as authority:
            with self.assertRaises(PodError) as caught:
                self.write([self.criterion()], schema="unsupported")
        self.assertEqual(caught.exception.code, "invalid_checkpoint")
        authority.assert_not_called()


if __name__ == "__main__":
    unittest.main()

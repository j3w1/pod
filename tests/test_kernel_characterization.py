"""Public projection and refusal ordering across kernel seams."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.cli import execute, parser
from pod.errors import PodError
from tests.kernel_support import KernelCase


class StatusProjectionTests(unittest.TestCase):
    def test_unselected_run_has_the_existing_json_shape(self):
        preferences = {"path": "/fixture/config.yaml", "schema": "pod/v2", "status": "valid",
                       "revision": "r", "routes": {"codex/gpt-6.1-sol/high": "enabled"},
                       "eligible": ["codex/gpt-6.1-sol/high"], "preferred": None,
                       "pinned": "codex/gpt-6.1-sol/high", "max_active": 2, "refresh": "manual",
                       "errors": [], "setup": None}
        before = deepcopy(preferences)
        with patch("pod.status.load_config", return_value=preferences), \
             patch("pod.status.running_identity", return_value={"version": "0.6.4", "bundle_digest": "d"}), \
             patch("pod.config.edit", side_effect=AssertionError("status wrote configuration")) as edit, \
             patch("pod.cli.current_run", return_value={"run": None}):
            result = execute(parser().parse_args(["status", "--json"]), Path("/fixture"))
        edit.assert_not_called()
        self.assertEqual(preferences, before)
        self.assertEqual(result, {
            "schema": "pod-cli/v5", "status": "selection_required", "run": None,
            "bundle_identity": {"running": {"version": "0.6.4", "bundle_digest": "d"},
                                "checkpoint": None, "drift": None},
            "preferences": {key: value for key, value in preferences.items() if key != "routes"},
            "routes": {"routes": 35, "enabled": 1, "disabled": 0, "not_set": 34, "preferred": None,
                       "pinned": "codex/gpt-6.1-sol/high", "unmapped": 0},
            "constraints": [], "route_decisions": [],
            "route_mismatch": False, "active_constraints": [], "effective_unknown": False,
            "installed_version_drift": False, "blocker": None,
            "decision_policy": {"status": "available", "preset": "careful", "categories": {
                "merge_publish": "person", "scope_changes": "person", "secrets": "person",
                "speaking_for_person": "person", "production_deployment": "person",
                "irreversible_deletion": "person", "model_choices": "trusted_agents",
                "coordinator_changes": "person", "continuity": "any_agent", "routine": "any_agent",
                "money": "person", "policy_changes": "person", "human_login": "person"},
                "setup": "unasked", "generation": 0},
            "tool_permissions": [],
            "waiting_decisions": {"status": "complete", "items": [], "total": 0, "truncated": False,
                                  "missing": [], "reason": None},
            "needs_you": False,
            "setup_question": {"text": "Who should decide? Choose Careful, Delegate, Hands-off or Custom.",
                               "choices": ["careful", "delegate", "hands_off", "custom", "skip"],
                               "required": "person"},
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

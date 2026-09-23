"""Deterministic scenario slices; live and conversation behavior need separate proof."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import DEFAULT, route_identity
from pod.context import execution_brief
from pod.errors import PodError
from pod.operations import guarded_start
from pod.records import packet
from pod.routing import preview
from pod.util import digest
from tests.common import fixture


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
ACCOUNT_IDENTITY = "a" * 64


def scenario_inputs():
    policy = deepcopy(DEFAULT)
    model = policy["models"]["sol"]
    model.update({"account": ACCOUNT_IDENTITY,
                  "approved": True, "approval_ref": "personal",
                  "billing": "included", "efforts": ["high"], "capabilities": []})
    model["approval_route"] = route_identity(model)
    policy["routing"]["complex"] = {"model": "sol", "effort": "high", "context": "max"}
    effective = {"policy": policy, "revision": digest(policy)}
    assessment = {"method": "delegate", "responsibility": "bounded edit", "complexity": "complex",
                  "risk": "low", "size": "small", "uncertainty": "low", "verifiability": "unit",
                  "capabilities": [], "context": [], "reason": "isolated edit", "bounded": True}
    capabilities = {"sol": {"agent": "codex", "model": "gpt-6-sol",
                            "account": ACCOUNT_IDENTITY,
                            "bucket": "shared", "efforts": ["high"], "capabilities": [],
                            "billing_preflight": True, "fanout_control": True,
                            "suitable_for": ["complex"],
                            "context_control": "native_per_launch",
                            "contexts": {"256k": 256000, "max": 900000}}}
    quota = {ACCOUNT_IDENTITY: {"schema": "pod-quota/v1", "provider": "codex",
                         "account": ACCOUNT_IDENTITY,
                         "bucket": "shared", "windows": [{"name": "hour", "remaining_percent": 60}],
                         "remaining_percent": 60, "observed_at": NOW.isoformat(), "source": "supported",
                         "confidence": "observed", "unknowns": []}}
    return effective, assessment, capabilities, quota


class ScenarioFixtureTests(unittest.TestCase):
    def test_preference_change_only_changes_later_route_and_revocation_blocks(self):
        effective, assessment, capabilities, quota = scenario_inputs()
        first = preview(assessment, effective, capabilities=capabilities, quotas=quota, now=NOW)
        self.assertEqual(first["status"], "usable")
        changed = deepcopy(effective["policy"])
        changed["policy"]["allowed_agents"] = []
        later = preview(assessment, {"policy": changed, "revision": digest(changed)},
                        capabilities=capabilities, quotas=quota, now=NOW)
        self.assertEqual(later["status"], "blocked")
        self.assertEqual(first["selected"]["agent"], "codex")
        self.assertNotEqual(first["policy_revision"], later["policy_revision"])

    def test_execution_brief_keeps_every_original_criterion(self):
        result = execution_brief(["edit", "review"], [
            {"criterion": "review", "dependency": "human review"},
            {"criterion": "edit", "check": "unit test"}])
        self.assertEqual([row["criterion"] for row in result["coverage"]], ["edit", "review"])
        with self.assertRaises(PodError):
            execution_brief(["edit", "review"], [{"criterion": "edit", "check": "unit test"}])
        with self.assertRaises(PodError):
            execution_brief(["edit"], [{"criterion": "different", "check": "unit test"}])

    def test_unusable_route_stops_before_native_read_or_start(self):
        class Spy:
            def assurance(self, route):
                raise AssertionError("native assurance called")

            def read_native(self, owner):
                raise AssertionError("native read called")

            def start_worker(self, **kwargs):
                raise AssertionError("native start called")

        with fixture() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            policy, assessment, capabilities, quota = scenario_inputs()
            route = {"alias": "sol", "agent": "codex", "model": "gpt-6-sol",
                     "account": ACCOUNT_IDENTITY, "bucket": "shared", "effort": "high",
                     "context": "max", "effective_context": 900000}
            frozen = packet({"schema": "pod-packet/v1", "objective": "objective",
                             "criteria": ["works"], "responsibility": "worker",
                             "scope": ["notes.txt"], "actions": ["edit"],
                             "candidate": "candidate", "context": [], "dependencies": [],
                             "route": route, "policy_revision": policy["revision"],
                             "plan_revision": "plan", "report_contract": "checks",
                             "sources": []})
            with self.assertRaises(PodError) as caught:
                guarded_start(project, "objective", owner="owner", run="run", task="task",
                              assessment=assessment, capabilities=capabilities,
                              quotas=quota, plan_revision="plan",
                              frozen_packet=frozen, port=Spy(), now=NOW)
            self.assertEqual(caught.exception.code, "route_unusable")

    def test_coverage_inventory_is_complete_without_claiming_behavior(self):
        root = Path(__file__).resolve().parents[1]
        inventory = json.loads((root / "docs" / "pod-coverage.json").read_text())
        ids = [row["id"] for row in inventory["scenarios"]]
        self.assertEqual(ids, [f"A{number:02d}" for number in range(1, len(ids) + 1)])
        self.assertTrue(all(row["candidate_status"] in ("NOT_RUN", "PARTIAL", "PASS", "RETIRED")
                            for row in inventory["scenarios"]))

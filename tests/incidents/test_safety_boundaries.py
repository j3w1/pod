import os
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.records import source_identity
from pod.internal import run
from tests.common import fixture


class SafetyBoundaryIncidents(unittest.TestCase):
    def test_parent_symlink_and_secret_source_refused(self):
        with fixture() as root:
            outside = root / "outside"
            outside.mkdir()
            (outside / "data").write_text("private")
            project = root / "project"
            project.mkdir()
            try:
                (project / "redirect").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit disposable symlink creation")
            with self.assertRaises(PodError):
                source_identity(project, "redirect/data")
            (project / ".env").write_text("secret")
            with self.assertRaises(PodError):
                source_identity(project, ".env")

    def test_caller_json_cannot_claim_live_admission(self):
        with patch("pod.operations.OrcaPort.assurance", return_value={"runtime": None, "billing_preflight": False, "fanout_control": False}):
            with self.assertRaises(PodError) as caught:
                run("admission", {"project": ".", "objective": "o", "owner": "t", "run": "r", "task": "t",
                                  "operation_id": "op", "assessment": {}, "capabilities": {},
                                  "quotas": {}, "occupancy": {}, "plan_revision": "p",
                                  "capability_contract": {"billing_preflight": True, "fanout_control": True}})
            self.assertEqual(caught.exception.code, "invalid_request")

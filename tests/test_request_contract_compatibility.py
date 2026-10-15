"""The genuine 0.15.0 bundle retains the same representative refusal codes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from tests.test_unit_audits import PREVIOUS, REPOSITORY


def observe(bundle):
    completed = subprocess.run([sys.executable, str(REPOSITORY / "tests/request_contract_previous_writer.py")],
        cwd=REPOSITORY, env={**os.environ, "PYTHONPATH": os.pathsep.join((str(bundle / "skills"), str(REPOSITORY)))},
        capture_output=True, text=True, timeout=15)
    if completed.returncode:
        raise AssertionError(completed.stderr)
    if "PRIVATE-SENTINEL" in completed.stdout + completed.stderr:
        raise AssertionError("A refused value escaped")
    return json.loads(completed.stdout)


class RefusalCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="refusal-previous-", dir=parent)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.bundle = Path(cls.temporary.name)
        archive = cls.bundle / "source.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), PREVIOUS,
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")
        cls.previous = observe(cls.bundle)

    def test_same_requests_keep_genuine_0150_refusal_codes(self):
        self.assertEqual(self.previous["version"], "0.15.0")
        current = observe(REPOSITORY)
        self.assertEqual(current["codes"], self.previous["codes"])
        self.assertEqual(current["codes"]["malformed_nonclosing_route_mismatch"], "route_mismatch")
        self.assertTrue(self.previous["route_mismatch"])
        self.assertTrue(current["route_mismatch"])
        self.assertFalse(self.previous["effective_unknown"])
        self.assertFalse(current["effective_unknown"])
        self.assertNotIn("ACCEPTED", current["codes"].values())

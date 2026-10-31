"""Genuine preceding config readers and unchanged specialized relay functions."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from pod import config
from tests.decision_policy_support import MachineCase

BASELINE = "42214fdca819fb85571ef16cad2c8f2479536867"
REPOSITORY = Path(__file__).resolve().parents[1]


def preceding_source(cls):
    temporary = tempfile.TemporaryDirectory(prefix="decision-previous-")
    cls.addClassCleanup(temporary.cleanup)
    cls.previous = Path(temporary.name)
    archive = cls.previous / "source.tar"
    subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), BASELINE, "skills", "VERSION"], check=True)
    with tarfile.open(archive) as source:
        source.extractall(cls.previous, filter="data")


def preceding_refuses(test, path, module, reader, expected):
    before = {item: item.read_bytes() for item in path.parent.rglob("*") if item.is_file()}
    result = subprocess.run([sys.executable, "-c", '''import importlib, sys
from pathlib import Path
from pod.errors import PodError
try:
    getattr(importlib.import_module(sys.argv[2]), sys.argv[3])(Path(sys.argv[1]))
except PodError as error:
    print(error.code)
else:
    print("ACCEPTED")
''', str(path), module, reader], cwd=test.previous,
        env={**os.environ, "PYTHONPATH": str(test.previous / "skills")}, capture_output=True, text=True, timeout=10)
    test.assertEqual(result.returncode, 0, result.stderr)
    test.assertEqual(result.stdout.strip(), "state_unsupported")
    test.assertEqual(before, {item: item.read_bytes() for item in path.parent.rglob("*") if item.is_file()})


class VerifiedReaderTests(MachineCase, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        preceding_source(cls)

    def test_genuine_preceding_config_reader_refuses_new_semantics_without_write(self):
        self.policy()
        path = config.personal_path()
        before = path.read_bytes()
        result = subprocess.run([sys.executable, "-c", '''import sys
from pathlib import Path
from pod.config import read_yaml
from pod.errors import PodError
try:
    read_yaml(Path(sys.argv[1]))
except PodError as exc:
    print(exc.code)
else:
    print("ACCEPTED")
''', str(path)], cwd=self.previous, env={**os.environ, "PYTHONPATH": str(self.previous / "skills")},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "invalid_config")
        self.assertEqual(before, path.read_bytes())

    def test_every_old_relay_wire_function_has_identical_preceding_code(self):
        def functions(path):
            source = path.read_text()
            return {item.name: ast.get_source_segment(source, item) for item in ast.parse(source).body
                    if isinstance(item, ast.FunctionDef)}
        earlier = functions(self.previous / "skills/pod/relay.py")
        current = functions(REPOSITORY / "skills/pod/relay.py")
        for name in ("_output", "_window_fields", "validate_verdict", "verify", "check_use"):
            with self.subTest(function=name):
                self.assertEqual(earlier[name], current[name])


class VerifiedContainerReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        preceding_source(cls)

    def test_genuine_preceding_reader_refuses_actual_verified_context_without_rewrite(self):
        from pod.ledger import _path
        from tests.decision_policy_support import RELAY, save_vouch
        from tests.test_verified_decisions import VerifiedObjectiveCase
        case = VerifiedObjectiveCase("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.approve_scope()
        case.configure_verifier()
        proposed = case.revision()
        operation = lambda: case.write(proposed, revision_authority=RELAY)
        save_vouch(case, operation)
        state = operation()
        self.assertEqual(state["decision_channels"], 5)
        self.assertEqual(state["checkpoint"]["revision_decisions"][-1]["actor"]["kind"], "agent")
        preceding_refuses(self, _path(case.project, "objective"), "pod.ledger", "_read", "invalid_context")

    def test_genuine_preceding_reader_refuses_actual_verified_journal_without_rewrite(self):
        from pod.governor import _record_path
        from tests.decision_policy_support import save_vouch
        from tests.test_verified_decisions import VerifiedGovernorTests
        case = VerifiedGovernorTests("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        request = case.generic_request()
        operation = lambda: case.case.op("governor", action=request)
        save_vouch(case, operation)
        operation()
        state = case.journal()
        self.assertEqual(state["decision_channels"], 5)
        self.assertEqual(state["actions"][-1]["action"]["authorization"]["actor"]["kind"], "agent")
        preceding_refuses(self, _record_path(case.project, "objective"), "pod.governor", "_read_journal", "invalid_governor")

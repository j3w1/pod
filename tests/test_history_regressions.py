"""Production PoD#3/PoD#4 regressions, run before and after the storage boundary."""
import tempfile
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

from pod.ledger import CONTEXT_LIMIT, _write, _read
from tests.history_support import synthetic_state, size
from tests.test_historical_retirement import RetirementCase


PREVIOUS = "678fb09c1d3ade80614cc763c87776b2072d01ce"
REPOSITORY = Path(__file__).resolve().parents[1]


def previous_history_bundle(directory, revision=PREVIOUS):
    """Archive the complete reviewed pre-change bundle, never installed helpers."""
    bundle = Path(directory)
    archive = bundle / "source.tar"
    subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), revision,
                    "skills", "VERSION"], check=True)
    with tarfile.open(archive) as source:
        source.extractall(bundle, filter="data")
    return bundle


def previous_history_call(bundle, state, *, operation="read", context=None, chunks=None, prior=None):
    """Exercise real earlier physical and semantic boundaries in a separate process."""
    import base64
    program = """
import base64, json, sys, tempfile
from pathlib import Path
from pod import __version__
from pod.errors import PodError
from pod.ledger import _read, _write
from pod.obligations import _persisted_rows
from pod.util import encoded
request = json.load(sys.stdin)
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'context.json'
    if request['prior'] is not None:
        _write(path, request['prior'])
    if request['operation'] == 'read':
        path.write_bytes(encoded(request['state'], compact=True))
        for address, data in request['chunks'].items():
            chunk = path.parent / 'history/sha256' / (address + '.json')
            chunk.parent.mkdir(parents=True, exist_ok=True)
            chunk.write_bytes(base64.b64decode(data))
    before = {str(p.relative_to(path.parent)): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    try:
        if request['operation'] == 'write':
            _write(path, request['state'])
        logical = _read(path)
        if request['context'] is not None:
            context = request['context']
            context['source_current'] = lambda entry: True
            context['git_delta'] = lambda source, target: ['src/new.py']
            _persisted_rows(logical['checkpoint'], context)
        result = {'accepted': True, 'state': logical}
    except PodError as exc:
        result = {'accepted': False, 'code': exc.code, 'message': str(exc), 'detail': exc.detail}
    after = {str(p.relative_to(path.parent)): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    result.update(version=__version__, unchanged=before == after)
    print(json.dumps(result))
"""
    completed = subprocess.run([sys.executable, "-c", program], cwd=bundle,
        env={**os.environ, "PYTHONPATH": str(bundle / "skills")},
        input=json.dumps({"operation": operation, "state": state, "context": context, "prior": prior,
                          "chunks": {key: base64.b64encode(value).decode("ascii")
                                     for key, value in (chunks or {}).items()}}),
        capture_output=True, text=True, check=True, timeout=15)
    return json.loads(completed.stdout)


class CapacityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = synthetic_state()

    def test_sixty_consumed_admissions_fit_and_read_exactly(self):
        self.assertGreater(size(self.state), CONTEXT_LIMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            from copy import deepcopy
            state = deepcopy(self.state)
            _write(path, state)
            self.assertLess(path.stat().st_size, CONTEXT_LIMIT)
            self.assertEqual(_read(path), state)


class RetirementMoveRegressionTests(RetirementCase):
    def test_explicitly_unbound_run_at_new_generation_retires(self):
        self.delivered()
        self.run_holders["run"] = None
        self.generations["run"] = 2
        detected = self.retire()
        self.assertEqual(detected["status"], "retirement_available", detected)
        decision = self.decide()
        self.assertEqual(decision["objectives"][0]["runs"], [{"run": "run", "generation": 2}])
        self.assertEqual(self.retire(decision)["status"], "retired")

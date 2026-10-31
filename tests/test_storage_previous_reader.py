"""The actual preceding bundle refuses revision storage without changing data."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from pod.history import (migrate_context)
from pod.util import encoded
from tests.history_support import synthetic_state

PRECEDING = "deda03a2eea5cc01ae4ed983f1f441d2d0b483ab"


class PrecedingReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.source = cls.root / "source"; cls.source.mkdir()
        archive = cls.root / "source.tar"
        subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[1]), "archive", "-o", str(archive),
                        PRECEDING, "skills", "VERSION"], check=True, capture_output=True)
        with tarfile.open(archive) as packed: packed.extractall(cls.source, filter="data")
        cls.path = cls.root / "record/context.json"; cls.path.parent.mkdir()
        state = synthetic_state(4)
        raw = encoded(state, compact=True); cls.path.write_bytes(raw)
        migrate_context(cls.path, revision=state["revision"], sha256=hashlib.sha256(raw).hexdigest())

    def test_preceding_reader_and_writer_refuse_without_changing_context_or_chunks(self):
        before = {path.relative_to(self.path.parent): path.read_bytes()
                  for path in self.path.parent.rglob("*") if path.is_file()}
        result = subprocess.run([sys.executable, "-c", '''
import sys
from pathlib import Path
from pod.errors import PodError
from pod.ledger import _read, _write
path = Path(sys.argv[1])
try:
    value = _read(path)
    _write(path, value)
except PodError as error:
    print(error.code)
else:
    print("ACCEPTED")
''', str(self.path)], cwd=self.source, capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": str(self.source / "skills"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "state_unsupported")
        after = {path.relative_to(self.path.parent): path.read_bytes()
                 for path in self.path.parent.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

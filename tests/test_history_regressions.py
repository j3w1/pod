"""Production PoD#3/PoD#4 regressions, run before and after the storage boundary."""
import tempfile
from pathlib import Path
import unittest

from pod.ledger import CONTEXT_LIMIT, _write, _read
from tests.history_support import synthetic_state, size
from tests.test_historical_retirement import RetirementCase


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

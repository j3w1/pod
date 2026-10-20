"""Existing R98 continuity preserves immutable no-start proof across a runtime rebind."""

from copy import deepcopy
import os
import shutil
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import objective_root, read, state_root
from pod.operations import _retry_after, guarded_start
from tests import test_retry_no_start as no_start
from tests import test_runtime_continuity as continuity
from tests.kernel_support import REQUEST
from tests.common import fixture


class RetryContinuityPort(no_start.NoStartPort, continuity.ContinuityPort):
    pass


class RetryFixtureCase(continuity.ContinuityCase):
    """Copy settled production fixtures while retaining the real native authority join."""
    restated = no_start.NoStartTests.restated
    discard = no_start.NoStartTests.discard
    settle = no_start.NoStartTests.settle

    @classmethod
    def setUpClass(cls):
        # Build the common, genuinely admitted and settled parent once. The Git
        # project is read-only in these tests; every mutable Pod file is copied.
        seed = cls.seed = cls()
        continuity.ContinuityCase.setUp(seed)
        cls.addClassCleanup(seed.doCleanups)
        placement = seed.port.placement
        seed.port = RetryContinuityPort()
        seed.port.placement = placement
        cls.prepare_seed(seed)
        cls.seed_state = state_root(seed.project)
        cls.seed_bytes = {path.relative_to(cls.seed_state): path.read_bytes()
                          for path in cls.seed_state.rglob('*') if path.is_file()}
        cls.seed_config = (seed.root / 'config' / 'pod' / 'config.yaml').read_bytes()
        seed.stack.close()  # Each test installs its own native ports below.

    def copy_fixture(self):
        self.temp = fixture()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.project = self.seed.project
        self.base, self.candidate = self.seed.base, self.seed.candidate
        shutil.copytree(self.seed_state, self.root / 'state')
        shutil.copytree(self.seed.root / 'config', self.root / 'config')
        self.env = patch.dict(os.environ, {
            'POD_STATE_HOME': str(self.root / 'state'),
            'POD_CONFIG_HOME': str(self.root / 'config' / 'pod'),
            'POD_CACHE_HOME': str(self.root / 'cache')})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    def setUp(self):
        # Reuse ContinuityCase's real R98 authority join and native-port wiring.
        with patch.object(continuity.KernelCase, 'setUp', self.copy_fixture):
            super().setUp()
        self.port = deepcopy(self.seed.port)
        self.frozen_packets = deepcopy(self.seed.frozen_packets)

    def tearDown(self):
        self.assertEqual({path.relative_to(self.seed_state): path.read_bytes()
                          for path in self.seed_state.rglob('*') if path.is_file()}, self.seed_bytes)
        self.assertEqual(self.seed.port.runtime, 'runtime')
        self.assertEqual(len(self.seed.port.starts), 1)
        self.assertEqual((self.seed.root / 'config' / 'pod' / 'config.yaml').read_bytes(), self.seed_config)


class RetryContinuityTests(RetryFixtureCase):
    @classmethod
    def prepare_seed(cls, seed):
        cls.parent_admission, = seed.started()
        seed.settle(cls.parent_admission)
        seed.discard(cls.parent_admission)

    def retry(self, parent, previous=None):
        return guarded_start(self.project, 'objective', owner='owner', run='run', task=parent['task_id'],
                             plan_revision='plan',
                             frozen_packet=self.packet(['S1'], boundary={'paths': ['area1']}),
                             port=self.port, retry_of=parent['admission_id'],
                             retry_after=previous['admission_id'] if previous else None,
                             accompanying=self.restated())

    def test_original_blocked_refusal_survives_proven_runtime_rebind(self):
        parent = deepcopy(self.parent_admission)
        self.port.receipt = {
            'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
            'error': {'code': 'task_not_startable', 'message': 'opaque refusal',
                      'data': {'runId': 'run', 'taskId': parent['task_id'],
                               'retryOf': parent['native_binding']['dispatchId'], 'status': 'blocked'}}}
        previous = self.retry(parent)['admission']
        path = objective_root(self.project, 'objective') / 'no-start' / (previous['recovery']['no_start'] + '.json')
        proof = path.read_bytes()
        self.port.receipt = None
        self.change_runtime()
        self.checkpoint_op(**self.restated())
        rebound = read(self.project, 'objective')['admissions'][previous['admission_id']]
        self.assertEqual(rebound['runtime'], 'runtime-2')
        self.assertEqual(rebound['request_uuid'], previous['request_uuid'])
        self.assertEqual(rebound['recovery'], previous['recovery'])
        self.assertEqual(rebound['route_decision'], previous['route_decision'])
        self.assertEqual(self.history()[-1]['from_runtime'], 'runtime')
        unlinked = deepcopy(read(self.project, 'objective'))
        unlinked['checkpoint']['continuity']['history'] = []
        with self.assertRaises(PodError) as caught:
            _retry_after(self.project, unlinked, unlinked['admissions'][parent['admission_id']],
                         previous['admission_id'])
        self.assertEqual(caught.exception.code, 'native_evidence_unavailable')
        self.assertEqual(self.retry(parent, previous)['status'], 'bound')
        self.assertEqual(path.read_bytes(), proof)
        self.assertEqual(len(self.port.starts), 3)

    def test_caught_pre_start_proof_survives_proven_runtime_rebind(self):
        parent = deepcopy(self.parent_admission)
        original = self.port.show_worker
        def unavailable(dispatch):
            if len(read(self.project, 'objective')['admissions']) == 2:
                raise PodError('orca_unavailable', 'transient parent read')
            return original(dispatch)
        with patch.object(self.port, 'show_worker', side_effect=unavailable):
            with self.assertRaises(PodError):
                self.retry(parent)
        previous = next(row for row in read(self.project, 'objective')['admissions'].values()
                        if row.get('retry_of') == parent['admission_id'])
        path = objective_root(self.project, 'objective') / 'no-start' / (previous['recovery']['no_start'] + '.json')
        proof = path.read_bytes()
        self.change_runtime()
        self.checkpoint_op(**self.restated())
        self.assertEqual(self.retry(parent, previous)['status'], 'bound')
        self.assertEqual(path.read_bytes(), proof)
        self.assertEqual(len(self.port.starts), 2)

"""Existing R98 continuity preserves immutable no-start proof across a runtime rebind."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import objective_root, read
from pod.operations import _retry_after, guarded_start
from tests import test_retry_no_start as no_start
from tests import test_runtime_continuity as continuity
from tests.kernel_support import REQUEST


class RetryContinuityPort(no_start.NoStartPort, continuity.ContinuityPort):
    pass


class RetryContinuityTests(continuity.ContinuityCase):
    restated = no_start.NoStartTests.restated
    discard = no_start.NoStartTests.discard
    settle = no_start.NoStartTests.settle

    def setUp(self):
        super().setUp()
        placement = self.port.placement
        self.port = RetryContinuityPort()
        self.port.placement = placement

    def retry(self, parent, previous=None):
        return guarded_start(self.project, 'objective', owner='owner', run='run', task=parent['task_id'],
                             plan_revision='plan',
                             frozen_packet=self.packet(['S1'], boundary={'paths': ['area1']}),
                             port=self.port, retry_of=parent['admission_id'],
                             retry_after=previous['admission_id'] if previous else None,
                             accompanying=self.restated())

    def test_original_blocked_refusal_survives_proven_runtime_rebind(self):
        parent, = self.started()
        self.settle(parent)
        self.discard(parent)
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
        parent, = self.started()
        self.settle(parent)
        self.discard(parent)
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

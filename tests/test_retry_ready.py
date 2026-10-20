"""Typed READY preflight recovery preserves the native attempt and policy boundaries."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event
from unittest.mock import patch

from pod import operations
from pod.config import load, set_pin, set_route
from pod.errors import PodError
from pod.ledger import admission_identity, constraints_update, objective_root, read
from tests.common import GroupedTable
from tests.kernel_support import OTHER, REQUEST
from tests import test_retry_continuity as continuity
from tests import test_operations as support


class ReadyFixture(continuity.RetryFixtureCase):
    record_failure = support.AdmissionTests.record_failure
    retry = continuity.RetryContinuityTests.retry

    @classmethod
    def prepare_seed(cls, seed):
        cls.parent_admission, = seed.started()
        seed.settle(cls.parent_admission)
        seed.discard(cls.parent_admission)

    def setUp(self):
        super().setUp()
        self.parent = read(self.project, 'objective')['admissions'][self.parent_admission['admission_id']]

    def deferred(self, *, status='ready', omit=(), code='task_not_startable', **detail):
        # Native-shaped detail: Run id is absent, but the immutable invocation has it.
        data = {'taskId': self.parent['task_id'], 'status': status, 'unmetDependencies': [],
                'retryOf': self.parent['native_binding']['dispatchId'],
                'nextSteps': ['Inspect the latest settled Dispatch and Task prerequisite.'], **detail}
        for key in omit:
            data.pop(key)
        self.port.receipt = {'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
                             'error': {'code': code, 'message': 'opaque native refusal', 'data': data}}
        previous = self.retry(self.parent)['admission']
        self.port.receipt = None
        return previous

    def proof_files(self, previous):
        root = objective_root(self.project, 'objective')
        return [root / 'no-start' / (previous['recovery']['no_start'] + '.json'),
                *[root / 'native-start' / (ref + '.json')
                  for ref in previous['recovery']['start_observations']]]

    def unchanged_refusal(self, code, call):
        before, starts = self.snapshot(), deepcopy(self.port.starts)
        self.refused(code, call)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.port.starts, starts)


class ReadyRecoveryTests(ReadyFixture):
    def test_native_shaped_ready_proof_allows_one_new_uuid_and_immutable_repeat(self):
        previous = self.deferred()
        self.assertEqual(previous['state'], 'deferred')
        files = {path: path.read_bytes() for path in self.proof_files(previous)}
        proof = json.loads(next(iter(files.values())))
        self.assertEqual(proof['receipt']['error']['data']['status'], 'ready')
        self.assertNotIn('runId', proof['receipt']['error']['data'])
        observed = json.loads(list(files.values())[1])
        self.assertEqual(observed['invocation'], {
            'run_id': 'run', 'task_id': self.parent['task_id'], 'runtime': 'runtime',
            'retry_of_dispatch_id': self.parent['native_binding']['dispatchId'], 'retry_request': None})
        original = self.port.start_worker

        def start(**kwargs):
            return {**original(**kwargs), 'request_uuid': OTHER}

        with patch.object(self.port, 'start_worker', side_effect=start):
            child = self.retry(self.parent, previous)['admission']
        self.assertEqual(child['state'], 'bound')
        self.assertEqual(child['request_uuid'], OTHER)
        self.assertNotEqual(child['request_uuid'], previous['request_uuid'])
        self.assertEqual(child['retry_of'], self.parent['admission_id'])
        self.assertEqual(child['retry_after'], previous['admission_id'])
        self.assertEqual(child['native_binding']['taskId'], self.parent['task_id'])
        self.assertNotEqual(child['native_binding']['dispatchId'], self.parent['native_binding']['dispatchId'])
        self.assertEqual(child['admission_id'], admission_identity(
            objective='objective', run_id='run', task_id=self.parent['task_id'],
            retry_of_dispatch_id=self.parent['native_binding']['dispatchId'],
            retry_after=previous['admission_id']))
        self.assertIsNone(self.port.starts[-1]['terminal'])
        self.assertIsNone(self.port.starts[-1]['retry_request'])
        self.assertEqual(self.port.starts[-1]['retry_of'], self.parent['native_binding']['dispatchId'])
        self.assertEqual(self.retry(self.parent, previous)['admission'], child)
        self.assertEqual(self.retry(self.parent)['admission'], previous)
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        current = read(self.project, 'objective')['admissions']
        self.assertEqual(current[previous['admission_id']], previous)
        self.assertEqual(current[self.parent['admission_id']], self.parent)
        self.assertEqual(len(self.port.starts), 3)
        self.assertEqual(len(self.port.workers), 2)

    def test_concurrent_identical_ready_successors_start_once(self):
        previous = self.deferred()
        frozen = self.packet(['S1'], boundary={'paths': ['area1']})
        mapped = self.restated()
        reserved, release = Event(), Event()
        original = operations._launch_reserved

        def launch(*args, **kwargs):
            reserved.set()
            if not release.wait(1):
                raise AssertionError('successor reservation was not released')
            return original(*args, **kwargs)

        def call():
            return operations.guarded_start(
                self.project, 'objective', owner='owner', run='run', task=self.parent['task_id'],
                plan_revision='plan', frozen_packet=frozen, port=self.port,
                retry_of=self.parent['admission_id'], retry_after=previous['admission_id'], accompanying=mapped)

        with patch.object(operations, '_launch_reserved', side_effect=launch), ThreadPoolExecutor(1) as pool:
            first = pool.submit(call)
            try:
                self.assertTrue(reserved.wait(1))
                second = call()
                self.assertEqual(second['status'], 'unresolved')
            finally:
                release.set()
            child = first.result(1)['admission']
        self.assertEqual(child['state'], 'bound')
        self.assertEqual(second['admission']['admission_id'], child['admission_id'])
        self.assertEqual(self.retry(self.parent, previous)['admission'], child)
        self.assertEqual(len(self.port.starts), 3)
        self.assertEqual(len(self.port.workers), 2)

    def test_uncorrected_native_prerequisite_defers_without_automatic_retry(self):
        previous = self.deferred()
        raw = json.loads(self.proof_files(previous)[0].read_text())['receipt']
        self.port.receipt = raw
        second = self.retry(self.parent, previous)['admission']
        self.assertEqual(second['state'], 'deferred')
        self.assertIsNone(second['native_binding'])
        self.assertEqual(len(self.port.workers), 1)
        self.assertEqual(self.retry(self.parent, previous)['admission'], second)
        self.assertEqual(len(self.port.starts), 3)

    def test_missing_invocation_cannot_fill_omitted_run_id(self):
        original = operations._observe_start

        def without_invocation(*args, **kwargs):
            kwargs.pop('invocation', None)
            return original(*args, **kwargs)

        with patch.object(operations, '_observe_start', side_effect=without_invocation):
            previous = self.deferred()
        self.unchanged_refusal('retry_no_start_unverified', lambda: self.retry(self.parent, previous))

    def test_proven_runtime_rebind_preserves_ready_receipt_bytes(self):
        previous = self.deferred()
        files = {path: path.read_bytes() for path in self.proof_files(previous)}
        self.change_runtime()
        self.checkpoint_op(**self.restated())
        self.assertEqual(self.retry(self.parent, previous)['status'], 'bound')
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        self.assertEqual(self.history()[-1]['from_runtime'], 'runtime')

    def test_corrupt_ready_proof_holds_without_start(self):
        previous = self.deferred()
        self.proof_files(previous)[0].write_bytes(b'{}')
        self.unchanged_refusal('native_evidence_unavailable', lambda: self.retry(self.parent, previous))

    def test_wrong_current_coordinator_holds_without_state_or_start_change(self):
        previous = self.deferred()
        self.port.run_binding.update(coordinator_handle='another-coordinator', consumer_generation=2)
        self.unchanged_refusal('native_authority_unverified', lambda: self.retry(self.parent, previous))

    def test_unknown_successor_effect_cannot_authorize_another_fresh_intent(self):
        previous = self.deferred()
        original = self.port.start_worker

        def unknown(**kwargs):
            original(**kwargs)
            raise TimeoutError('synthetic transport outcome unknown')

        with patch.object(self.port, 'start_worker', side_effect=unknown):
            with self.assertRaises(TimeoutError):
                self.retry(self.parent, previous)
        child = next(row for row in read(self.project, 'objective')['admissions'].values()
                     if row.get('retry_after') == previous['admission_id'])
        self.assertEqual(child['state'], 'unresolved')
        self.unchanged_refusal('retry_no_start_unverified', lambda: self.retry(self.parent, child))
        recovered = operations.recover_admission(
            self.project, 'objective', owner='owner', admission_id=child['admission_id'],
            worktree='current', port=self.port)
        self.assertEqual(recovered['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)
        self.assertEqual(len(self.port.workers), 2)


class ReadyRefusalTests(GroupedTable):
    @classmethod
    def setUpClass(cls):
        ReadyFixture.setUpClass()
        cls.addClassCleanup(ReadyFixture.doClassCleanups)

    GROUPS = {
        'test_missing_and_null_status': ({'omit': ('status',)}, {'status': None}),
        'test_malformed_status': ({'status': []}, {'status': {}}),
        'test_failed_and_running_status': ({'status': 'failed'}, {'status': 'running'}),
        'test_other_status': ({'status': 'dispatched'}, {'status': 'cancelled'}),
        'test_permission_and_safety_reason': ({'reason': 'permission_required'}, {'reason': 'safety_refusal'}),
        'test_authentication_and_unknown_reason': ({'reason': 'auth_failed'}, {'reason': 'unknown'}),
        'test_other_task_and_parent': ({'taskId': 'foreign'}, {'retryOf': 'foreign'}),
        'test_other_run_and_runtime': ({'runId': 'foreign'}, {'runtimeId': 'foreign'}),
        'test_permission_and_authentication_code': ({'code': 'permission_required'}, {'code': 'auth_failed'}),
        'test_safety_and_unknown_code': ({'code': 'safety_refusal'}, {'code': 'runtime_error'}),
        'test_circuit_and_cancelled_code': ({'status': 'failed', 'failureCount': 3}, {'code': 'cancelled'}),
        'test_effect_detail_and_malformed_reason': ({'dispatchId': 'possible-child'}, {'reason': []}),
    }

    def row(self, kwargs):
        case = ReadyFixture()
        case.setUp()
        try:
            previous = case.deferred(**kwargs)
            case.unchanged_refusal('retry_no_start_unverified', lambda: case.retry(case.parent, previous))
            case.assertEqual(len(case.port.workers), 1)
        finally:
            try:
                case.tearDown()
            finally:
                case.doCleanups()

    def test_missing_and_null_status(self):
        self.rows()

    def test_malformed_status(self):
        self.rows()

    def test_failed_and_running_status(self):
        self.rows()

    def test_other_status(self):
        self.rows()

    def test_permission_and_safety_reason(self):
        self.rows()

    def test_authentication_and_unknown_reason(self):
        self.rows()

    def test_other_task_and_parent(self):
        self.rows()

    def test_other_run_and_runtime(self):
        self.rows()

    def test_permission_and_authentication_code(self):
        self.rows()

    def test_safety_and_unknown_code(self):
        self.rows()

    def test_circuit_and_cancelled_code(self):
        self.rows()

    def test_effect_detail_and_malformed_reason(self):
        self.rows()


class ReadyPolicyTests(ReadyFixture):
    def test_current_pin_still_controls_ready_successor(self):
        previous = self.deferred()
        set_pin(self.root / 'config' / 'pod' / 'config.yaml', 'codex/gpt-6-luna/medium',
                displayed=load(self.project))
        self.unchanged_refusal('pin_mismatch', lambda: self.retry(self.parent, previous))

    def test_current_route_eligibility_still_controls_ready_successor(self):
        previous = self.deferred()
        set_route(self.root / 'config' / 'pod' / 'config.yaml', 'codex/gpt-6.1-sol/medium',
                  'disabled', displayed=load(self.project))
        self.unchanged_refusal('route_ineligible', lambda: self.retry(self.parent, previous))

    def test_recorded_constraint_still_controls_ready_successor(self):
        previous = self.deferred()
        constraints_update(self.project, 'objective', owner='owner', action='add', value={
            'id': 'claude-only', 'kind': 'only_agents', 'provenance': 'user_direct', 'value': ['claude']})
        self.unchanged_refusal('constraint_excluded', lambda: self.retry(self.parent, previous))

    def test_current_authentication_failure_still_blocks_ready_successor(self):
        previous = self.deferred()
        self.record_failure(self.parent, kind='auth_failed', source='synthetic native authentication refusal')
        self.unchanged_refusal('route_failed', lambda: self.retry(self.parent, previous))

    def test_current_safety_refusal_still_blocks_ready_successor(self):
        previous = self.deferred()
        self.record_failure(self.parent, kind='safety_refusal', source='synthetic native safety refusal')
        self.unchanged_refusal('safety_refusal', lambda: self.retry(self.parent, previous))

    def test_placement_change_still_blocks_ready_successor(self):
        previous = self.deferred()
        self.port.placement['path'] = '/foreign'
        self.unchanged_refusal('worktree_binding_changed', lambda: self.retry(self.parent, previous))


class ReadyParentTests(ReadyFixture):
    def parent_hold(self, *, dispatch=None, liveness=None, release=None, task=None):
        previous = self.deferred()
        original = self.port.show_worker

        def show(parent):
            shown = original(parent)
            result = shown['result']
            if dispatch is not None:
                result['dispatch']['status'] = dispatch
            if task is not None:
                result['dispatch']['taskId'] = task
            if liveness is not None:
                result['projection']['liveness']['verdict'] = liveness
            if release is not None:
                result['projection']['resource']['releaseState'] = release
            return shown

        with patch.object(self.port, 'show_worker', side_effect=show):
            with self.assertRaises(PodError) as caught:
                self.retry(self.parent, previous)
        self.assertEqual(caught.exception.code,
                         'native_assignment_unverified' if task else 'retry_parent_unsettled')
        self.assertEqual(len(self.port.starts), 2)
        self.assertEqual(len(self.port.workers), 1)

    def test_circuit_broken_parent_remains_held(self):
        self.parent_hold(dispatch='circuit_broken')

    def test_cancelled_parent_remains_held(self):
        self.parent_hold(dispatch='cancelled')

    def test_live_parent_remains_held(self):
        self.parent_hold(liveness='live')

    def test_unknown_parent_liveness_remains_held(self):
        self.parent_hold(liveness='unverifiable')

    def test_unknown_parent_resource_release_remains_held(self):
        self.parent_hold(release='release_unknown')

    def test_mismatched_native_parent_task_remains_held(self):
        self.parent_hold(task='foreign')

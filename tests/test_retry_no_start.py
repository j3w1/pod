"""Explicit fresh retry intents require positive, admission-bound no-start evidence."""

from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.config import load, set_pin, set_route
from pod.internal import run as internal_run
from pod.ledger import admission_identity, constraints_update, objective_root, read
from pod.operations import guarded_start
from tests.kernel_support import OTHER, REQUEST, ROUTE
from tests import test_native_retry as fixtures
from tests.common import GroupedTable


class NoStartPort(fixtures.RetryPort):
    """Record refused native retry arguments; joined receipts keep their actual UUID."""

    def start_worker(self, *, retry_of=None, **kwargs):
        receipt = super().start_worker(retry_of=retry_of, **kwargs)
        self.starts[-1]['retry_of'] = retry_of
        if kwargs.get('retry_request') and 'error' not in receipt:
            receipt['request_uuid'] = kwargs['retry_request']
        return receipt


class NoStartTests(unittest.TestCase):
    checkpoint = fixtures.RetryAdmissionTests.checkpoint
    frozen = fixtures.RetryAdmissionTests.frozen
    restated = fixtures.RetryAdmissionTests.restated
    discard = fixtures.RetryAdmissionTests.discard
    recover = fixtures.RetryAdmissionTests.recover
    record_failure = fixtures.RetryAdmissionTests.record_failure
    settle = fixtures.RetryAdmissionTests.settle
    parent = fixtures.RetryAdmissionTests.parent
    retry = fixtures.RetryAdmissionTests.retry
    refused = fixtures.RetryAdmissionTests.refused

    def setUp(self):
        fixtures.RetryAdmissionTests.setUp(self)
        placement = self.port.placement
        self.port = NoStartPort()
        self.port.placement = placement

    def start(self, task='task', frozen=None, retry_of=None, reuse_of=None, retry_after=None):
        return guarded_start(self.project, 'objective', owner='owner', run='run', task=task,
                             plan_revision='plan', frozen_packet=frozen or self.frozen(task=task),
                             worktree='current', port=self.port, retry_of=retry_of,
                             reuse_of=reuse_of, accompanying=self.restated(),
                             **({'retry_after': retry_after} if retry_after is not None else {}))

    def blocked(self, parent, *, omit=(), **data):
        self.port.receipt = {
            'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
            'error': {'code': 'task_not_startable', 'message': 'opaque native refusal',
                      'data': {'taskId': 'task', 'runId': 'run',
                               'retryOf': parent['native_binding']['dispatchId'], 'status': 'blocked',
                               'nextSteps': 'inspect blocked Task through Orca', **data}}}
        for field in omit:
            self.port.receipt['error']['data'].pop(field)
        return self.retry(parent)['admission']

    def fresh(self, parent, previous, frozen=None):
        return self.start(retry_of=parent['admission_id'], retry_after=previous['admission_id'], frozen=frozen)

    def test_blocked_refusal_allows_one_explicit_revalidated_successor(self):
        parent = self.parent()
        previous = self.blocked(parent)
        old = read(self.project, 'objective')['admissions']
        frozen = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        self.port.receipt = None  # The coordinator corrected the native prerequisite.
        original = self.port.start_worker
        def start(**kwargs):
            return {**original(**kwargs), 'request_uuid': OTHER}
        with patch.object(self.port, 'start_worker', side_effect=start):
            result = self.fresh(parent, previous, frozen)
        child = result['admission']
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(child['retry_after'], previous['admission_id'])
        self.assertEqual(child['retry_of'], parent['admission_id'])
        self.assertEqual(child['recovery']['retry_of_dispatch_id'], 'dispatch-1')
        self.assertEqual(child['request_uuid'], OTHER)
        self.assertEqual(child['route_decision']['effective']['model'], 'gpt-6-luna')
        self.assertIsNone(self.port.starts[-1]['retry_request'])
        self.assertEqual(self.port.starts[-1]['retry_of'], 'dispatch-1')
        self.assertEqual(child['admission_id'], admission_identity(
            objective='objective', run_id='run', task_id='task', retry_of_dispatch_id='dispatch-1',
            retry_after=previous['admission_id']))
        for key, row in old.items():
            self.assertEqual(read(self.project, 'objective')['admissions'][key], row)
        for _ in range(2):
            self.assertEqual(self.fresh(parent, previous, frozen)['admission'], child)
            self.assertEqual(self.retry(parent)['admission'], previous)
            self.assertEqual(self.recover(previous)['status'], 'deferred')
        self.assertEqual(len(self.port.starts), 3)
        self.assertEqual(len(self.port.workers), 2)

    def test_fresh_intent_cannot_replace_original_or_successor_packet(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        changed = self.frozen(route={**ROUTE, 'reason': 'new intent'})
        self.refused('admission_conflict', lambda: self.retry(parent, changed))
        child = self.fresh(parent, previous, changed)['admission']
        self.refused('admission_conflict', lambda: self.fresh(parent, previous))
        self.assertEqual(self.fresh(parent, previous, changed)['admission'], child)
        self.assertEqual(len(self.port.starts), 3)

    def test_fresh_intent_rechecks_preferences_and_actual_placement(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        frozen = self.frozen()
        set_route(self.personal, fixtures.support.SOL, 'disabled', displayed=load(self.project))
        self.refused('preference_changed', lambda: self.fresh(parent, previous, frozen))
        self.refused('route_ineligible', lambda: self.fresh(parent, previous))
        alternate = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        placement = self.port.placement.copy()
        self.port.placement['path'] = '/foreign'
        self.refused('worktree_binding_changed', lambda: self.fresh(parent, previous, alternate))
        self.port.placement = placement
        self.assertEqual(self.fresh(parent, previous, alternate)['status'], 'bound')

    def test_fresh_intent_obeys_current_pin(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        set_pin(self.personal, fixtures.support.LUNA, displayed=load(self.project))
        self.refused('pin_mismatch', lambda: self.fresh(parent, previous))
        alternate = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        self.assertEqual(self.fresh(parent, previous, alternate)['status'], 'bound')

    def test_fresh_intent_obeys_recorded_user_constraint(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        constraints_update(self.project, 'objective', owner='owner', action='add', value={
            'id': 'claude-only', 'kind': 'only_agents', 'provenance': 'user_direct', 'value': ['claude']})
        self.refused('constraint_excluded', lambda: self.fresh(parent, previous))
        alternate = self.frozen(route={**ROUTE, 'agent': 'claude', 'model': 'claude-sonnet-5-5'})
        self.assertEqual(self.fresh(parent, previous, alternate)['status'], 'bound')

    def test_authentication_after_no_start_holds_fresh_intent_across_routes(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        self.record_failure(parent, kind='auth_failed', source='native authentication refusal')
        alternate = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        self.refused('route_failed', lambda: self.fresh(parent, previous, alternate))
        self.assertEqual(len(self.port.starts), 2)

    def test_safety_after_no_start_holds_fresh_intent_across_routes(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        self.record_failure(parent, kind='safety_refusal', source='native safety refusal')
        alternate = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        self.refused('safety_refusal', lambda: self.fresh(parent, previous, alternate))
        self.assertEqual(len(self.port.starts), 2)

    def test_repeated_no_starts_keep_exact_genuine_native_parent(self):
        parent = self.parent()
        first = self.blocked(parent)
        second = self.fresh(parent, first)['admission']  # Native prerequisite still refuses.
        self.assertEqual(second['state'], 'deferred')
        self.port.receipt = None
        child = self.fresh(parent, second)['admission']
        self.assertEqual(child['retry_after'], second['admission_id'])
        self.assertEqual(child['retry_of'], parent['admission_id'])
        self.assertEqual([row.get('retry_of') for row in self.port.starts[1:]], ['dispatch-1'] * 3)
        self.refused('retry_parent_unverified', lambda: self.start(retry_of=second['admission_id']))
        self.assertEqual(len(self.port.workers), 2)

    def test_no_start_predecessor_cannot_be_borrowed_by_another_parent(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        child = self.fresh(parent, previous)['admission']
        self.settle(child)
        self.discard(child)
        self.refused('retry_no_start_unverified', lambda: self.fresh(child, previous))
        grandchild = self.retry(child)['admission']
        self.assertEqual(grandchild['recovery']['retry_of_dispatch_id'], 'dispatch-2')
        self.assertEqual(self.port.starts[-1]['retry_of'], 'dispatch-2')

    def test_missing_and_corrupt_no_start_proof_never_authorize_a_start(self):
        parent = self.parent()
        previous = self.blocked(parent)
        path = objective_root(self.project, 'objective') / 'no-start' / (previous['recovery']['no_start'] + '.json')
        original = path.read_bytes()
        path.unlink()
        self.refused('native_evidence_unavailable', lambda: self.fresh(parent, previous))
        path.write_bytes(b'{}')
        self.refused('native_evidence_unavailable', lambda: self.fresh(parent, previous))
        path.write_bytes(original)
        self.port.receipt = None
        self.assertEqual(self.fresh(parent, previous)['status'], 'bound')

    def test_internal_contract_reaches_explicit_fresh_intent(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        with patch('pod.operations.OrcaPort', return_value=self.port):
            result = internal_run('admission', {
                'project': str(self.project), 'objective': 'objective', 'owner': 'owner',
                'run': 'run', 'task': 'task', 'plan_revision': 'plan', 'packet': self.frozen(),
                'retry_of': parent['admission_id'], 'retry_after': previous['admission_id'],
                'worktree': 'current', 'map': self.restated()})
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['retry_after'], previous['admission_id'])

    def test_optional_native_ids_use_exact_recorded_invocation(self):
        parent = self.parent()
        previous = self.blocked(parent, omit=('runId', 'taskId', 'retryOf'))
        observed = previous['recovery']['start_observations'][-1]
        raw = json.loads((objective_root(self.project, 'objective') / 'native-start' / (observed + '.json')).read_text())
        self.assertEqual(raw['invocation'], {'run_id': 'run', 'task_id': 'task',
                                            'retry_of_dispatch_id': 'dispatch-1',
                                            'runtime': 'runtime', 'retry_request': None})
        self.port.receipt = None
        self.assertEqual(self.fresh(parent, previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)

    def test_missing_ids_without_invocation_provenance_stay_held(self):
        from pod import operations
        parent = self.parent()
        original = operations._observe_start
        def without_invocation(*args, **kwargs):
            kwargs.pop('invocation', None)
            return original(*args, **kwargs)
        with patch.object(operations, '_observe_start', side_effect=without_invocation):
            previous = self.blocked(parent, omit=('runId', 'taskId', 'retryOf'))
        self.port.receipt = None
        self.refused('retry_no_start_unverified', lambda: self.fresh(parent, previous))

    def test_authoritative_no_uuid_refusal_uses_positive_invocation_evidence(self):
        parent = self.parent()
        self.port.omit_uuid = True
        previous = self.blocked(parent, omit=('runId', 'taskId', 'retryOf'), reason='')
        self.assertIsNone(previous['request_uuid'])
        self.port.omit_uuid = False
        self.port.receipt = None
        self.assertEqual(self.fresh(parent, previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)

    def pre_start_failure(self, flip=False):
        parent = self.parent()
        original = self.port.show_worker
        def show(dispatch):
            shown = original(dispatch)
            if dispatch == 'dispatch-1' and len(read(self.project, 'objective')['admissions']) == 2:
                if not flip:
                    raise PodError('orca_unavailable', 'transient parent read', {'read': 'worker-show'})
                shown['result']['projection']['liveness']['verdict'] = 'unverifiable'
            return shown
        with patch.object(self.port, 'show_worker', side_effect=show):
            with self.assertRaises(PodError) as caught:
                self.retry(parent)
        expected = 'retry_parent_unsettled' if flip else 'orca_unavailable'
        self.assertEqual(caught.exception.code, expected)
        previous = next(row for row in read(self.project, 'objective')['admissions'].values()
                        if row.get('retry_of') == parent['admission_id'])
        self.assertEqual(previous['state'], 'deferred')
        self.assertEqual(len(self.port.starts), 1)
        self.assertEqual(previous['recovery'].get('start_observations', []), [])
        proof = json.loads((objective_root(self.project, 'objective') / 'no-start' /
                            (previous['recovery']['no_start'] + '.json')).read_text())
        self.assertEqual(proof['stage'], 'retry_parent_recheck')
        self.assertEqual(proof['effect'], 'not_called')
        self.assertEqual(proof['error']['code'], expected)
        if flip:
            self.assertEqual(proof['error']['detail']['worker_show']['result']['projection']['liveness']['verdict'],
                             'unverifiable')
        else:
            self.assertEqual(proof['error']['detail'], {'read': 'worker-show'})
        self.assertIn('retry_after', previous['recovery']['next'])
        self.assertEqual(self.recover(previous)['admission'], previous)
        self.assertEqual(self.fresh(parent, previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)
        self.assertEqual(self.port.starts[-1]['retry_of'], 'dispatch-1')
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], parent)
        self.assertEqual(read(self.project, 'objective')['admissions'][previous['admission_id']], previous)

    def test_transient_parent_read_is_proven_no_start_then_recovers(self):
        self.pre_start_failure()

    def test_parent_liveness_flip_is_proven_no_start_then_recovers(self):
        self.pre_start_failure(flip=True)

    def test_started_child_binds_while_parent_release_is_unknown(self):
        parent = self.parent()
        original = self.port.start_worker
        def start(**kwargs):
            receipt = original(**kwargs)
            self.port.workers['dispatch-1']['facts']['resource'] = {
                'state': 'owned', 'releaseState': 'release_unknown'}
            return receipt
        with patch.object(self.port, 'start_worker', side_effect=start):
            self.assertEqual(self.retry(parent)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)

    def test_unrelated_inventory_conflict_is_ambiguous_and_can_be_reconciled(self):
        parent = self.parent()
        self.port.omit_uuid = True
        with self.assertRaises(PodError):
            self.retry(parent)
        previous = next(row for row in read(self.project, 'objective')['admissions'].values()
                        if row.get('retry_of') == parent['admission_id'])
        self.port.workers['stranger'] = {
            **deepcopy(self.port.workers['dispatch-2']), 'retry_of': 'another-parent', 'terminal': 'stranger'}
        held = self.recover(previous)['admission']
        self.assertEqual(held['error']['code'], 'native_attempt_ambiguous')
        self.assertNotIn('retry_parent_conflict', held['recovery'])
        self.port.workers.pop('stranger')
        self.assertEqual(self.recover(previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)

    def test_concurrent_identical_successors_share_one_reservation_and_start(self):
        from pod import operations
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        frozen = self.frozen()
        accompanying = self.restated()
        reserved, release = Event(), Event()
        original = operations._launch_reserved
        def launch(*args, **kwargs):
            reserved.set()
            if not release.wait(1):
                raise AssertionError('test did not release reserved successor')
            return original(*args, **kwargs)
        def call():
            return guarded_start(self.project, 'objective', owner='owner', run='run', task='task',
                                 plan_revision='plan', frozen_packet=frozen, port=self.port,
                                 retry_of=parent['admission_id'], retry_after=previous['admission_id'],
                                 accompanying=accompanying)
        with patch.object(operations, '_launch_reserved', side_effect=launch), ThreadPoolExecutor(1) as executor:
            first = executor.submit(call)
            try:
                self.assertTrue(reserved.wait(1))
                second = call()
                self.assertEqual(second['status'], 'unresolved')
            finally:
                release.set()
            child = first.result(1)['admission']
        self.assertEqual(child['state'], 'bound')
        self.assertEqual(second['admission']['admission_id'], child['admission_id'])
        self.assertEqual(len(read(self.project, 'objective')['admissions']), 3)
        self.assertEqual(len(self.port.starts), 3)
        self.assertEqual(self.fresh(parent, previous, frozen)['admission'], child)

    def test_reservation_crash_gap_never_becomes_no_start_proof(self):
        parent = self.parent()
        with patch('pod.operations._launch_reserved', side_effect=RuntimeError('crash gap')):
            with self.assertRaises(RuntimeError):
                self.retry(parent)
        previous = next(row for row in read(self.project, 'objective')['admissions'].values()
                        if row.get('retry_of') == parent['admission_id'])
        self.assertEqual(previous['state'], 'reserved')
        self.assertIsNone(previous['request_uuid'])
        self.assertNotIn('start_observations', previous['recovery'])
        self.refused('retry_no_start_unverified', lambda: self.fresh(parent, previous))
        for _ in range(2):
            held = self.recover(previous)['admission']
            self.assertEqual(held['state'], 'unresolved')
            self.assertEqual(held['error']['code'], 'native_attempt_absent')
        self.assertEqual(len(self.port.starts), 1)

    def test_unknown_after_effect_retains_reservation_then_adopts_exact_attempt(self):
        parent = self.parent()
        original = self.port.start_worker
        def unknown(**kwargs):
            original(**kwargs)
            raise TimeoutError('native response unknown')
        with patch.object(self.port, 'start_worker', side_effect=unknown):
            with self.assertRaises(TimeoutError):
                self.retry(parent)
        previous = next(row for row in read(self.project, 'objective')['admissions'].values()
                        if row.get('retry_of') == parent['admission_id'])
        self.assertEqual(previous['state'], 'unresolved')
        self.assertIsNone(previous['request_uuid'])
        self.refused('retry_no_start_unverified', lambda: self.fresh(parent, previous))
        self.assertEqual(self.recover(previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)

    def interrupted_fresh(self):
        parent = self.parent()
        previous = self.blocked(parent)
        self.port.receipt = None
        original = self.port.start_worker
        def start(**kwargs):
            return {**original(**kwargs), 'request_uuid': OTHER}
        with patch.object(self.port, 'start_worker', side_effect=start), \
                patch('pod.operations._bind', side_effect=PodError('read_lost', 'after native start')):
            with self.assertRaises(PodError):
                self.fresh(parent, previous)
        child = next(row for row in read(self.project, 'objective')['admissions'].values()
                     if row.get('retry_after') == previous['admission_id'])
        self.port.request_id = OTHER
        self.port.request_receipt = {**self.port.child_receipt('dispatch-2'), 'request_uuid': OTHER}
        return parent, previous, child

    def test_fresh_pending_replay_joins_original_uuid_and_parent(self):
        parent, previous, child = self.interrupted_fresh()
        self.port.state = 'pending'
        result = self.recover(child)
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['request_uuid'], OTHER)
        self.assertEqual(self.port.starts[-1]['retry_request'], OTHER)
        self.assertEqual(self.port.starts[-1]['retry_of'], 'dispatch-1')
        self.assertEqual(len(self.port.workers), 2)
        self.assertEqual(len(self.port.starts), 4)  # includes one same-UUID join, no new worker
        self.assertEqual(self.fresh(parent, previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 4)

    def test_fresh_absent_adoption_keeps_history(self):
        parent, previous, child = self.interrupted_fresh()
        self.port.state = 'absent'
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(read(self.project, 'objective')['admissions'][previous['admission_id']], previous)
        self.assertEqual(len(self.port.starts), 3)

    def test_fresh_completed_receipt_is_recorded_without_start(self):
        parent, previous, child = self.interrupted_fresh()
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)

    def test_post_start_adoption_does_not_recheck_parent_release(self):
        parent, previous, child = self.interrupted_fresh()
        self.port.workers['dispatch-1']['facts']['resource'] = {'state': 'owned', 'releaseState': 'release_unknown'}
        self.port.state = 'absent'
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)

    def test_settled_cancelled_native_dispatch_remains_ineligible(self):
        from pod.ledger import _native_assignment_settled
        parent = self.parent()
        original = self.port.show_worker
        def cancelled(dispatch):
            shown = original(dispatch)
            shown['result']['dispatch']['status'] = 'cancelled'
            shown['result']['projection'].update(outcome='cancelled', stage={'dispatch': 'cancelled'})
            return shown
        with patch.object(self.port, 'show_worker', side_effect=cancelled):
            self.assertTrue(_native_assignment_settled(self.port.show_worker('dispatch-1')))
            self.refused('retry_parent_unsettled', lambda: self.retry(parent))


class RefusalGuardTests(GroupedTable):
    GROUPS = {
        'test_terminal_and_running_status': (('status', 'failed'), ('status', 'running')),
        'test_missing_and_malformed_status': (('status', None), ('status', [])),
        'test_permission_and_safety_reasons': (('reason', 'permission_required'), ('reason', 'safety_refusal')),
        'test_auth_and_unknown_reasons': (('reason', 'auth_failed'), ('reason', 'unknown')),
        'test_other_run_and_task': (('runId', 'foreign'), ('taskId', 'foreign')),
        'test_foreign_parent_and_invalid_run': (('retryOf', 'foreign'), ('runId', 7)),
        'test_permission_and_auth_refusals': (('code', 'permission_required'), ('code', 'auth_failed')),
        'test_safety_and_runtime_refusals': (('code', 'safety_refusal'), ('code', 'runtime_error')),
        'test_other_effect_free_refusals': (('code', 'task_not_found'), ('code', 'inject_rejected')),
    }

    def row(self, item):
        case = NoStartTests()
        case.setUp()
        try:
            parent = case.parent()
            field, value = item
            if field == 'code':
                case.port.receipt = {
                    'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
                    'error': {'code': value, 'message': 'opaque refusal'}}
                previous = case.retry(parent)['admission']
            else:
                previous = case.blocked(parent, **{field: value})
            case.port.receipt = None
            case.refused('retry_no_start_unverified', lambda: case.fresh(parent, previous))
            self.assertEqual(len(case.port.starts), 2)
            self.assertEqual(len(case.port.workers), 1)
        finally:
            case.doCleanups()

    def test_terminal_and_running_status(self):
        self.rows()

    def test_missing_and_malformed_status(self):
        self.rows()

    def test_permission_and_safety_reasons(self):
        self.rows()

    def test_auth_and_unknown_reasons(self):
        self.rows()

    def test_other_run_and_task(self):
        self.rows()

    def test_foreign_parent_and_invalid_run(self):
        self.rows()

    def test_permission_and_auth_refusals(self):
        self.rows()

    def test_safety_and_runtime_refusals(self):
        self.rows()

    def test_other_effect_free_refusals(self):
        self.rows()

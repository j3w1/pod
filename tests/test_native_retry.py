"""Same-Task retries exercise admission, native adapter and immutable recovery boundaries."""

from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import patch

from pod.config import load, set_route
from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import admission_identity, constraints_update, objective_root, read
from pod.operations import OrcaPort, guarded_start
from pod.orca import mutate_command
from pod.util import digest
from tests import test_operations as support
from tests.kernel_support import FakePort, REQUEST, ROUTE


class RetryPort(FakePort):
    """Synthetic native chain; exit/resource fields follow the sanitized stopped capture."""

    def __init__(self):
        super().__init__()
        self.omit_uuid = False
        self.child_parent = None
        self.native_placements = {}

    def resolve_worktree(self, selector):
        if selector in self.native_placements:
            placement = self.native_placements[selector]
            if placement is None:
                raise PodError('worktree_resolution_unavailable', 'unknown native workspace')
            return dict(placement)
        return super().resolve_worktree(selector)

    def start_worker(self, *, retry_of=None, **kwargs):
        if retry_of is not None and kwargs.get('retry_request') is not None:
            children = [key for key, row in self.workers.items() if row.get('retry_of') == retry_of]
            if children:
                self.starts.append({**kwargs, 'retry_of': retry_of})
                return self.child_receipt(children[0])
        receipt = super().start_worker(**kwargs)
        if retry_of is not None and receipt.get('dispatchId') in self.workers:
            dispatch = receipt['dispatchId']
            self.starts[-1]['retry_of'] = retry_of
            self.workers[dispatch]['retry_of'] = self.child_parent or retry_of
            receipt['retryOfDispatchId'] = self.child_parent or retry_of
        if self.omit_uuid:
            receipt.pop('request_uuid', None)
        return receipt

    def child_receipt(self, dispatch):
        row = self.workers[dispatch]
        return {'runtime': self.runtime, 'request_uuid': REQUEST,
                'runId': row['run'], 'taskId': row['task'], 'dispatchId': dispatch,
                'retryOfDispatchId': row.get('retry_of'), 'state': row['state']}

    def show_worker(self, dispatch):
        shown = super().show_worker(dispatch)
        row = self.workers[dispatch]
        if 'retry_of' in row:
            shown['result']['dispatch']['retryOfDispatchId'] = row['retry_of']
        shown['result']['projection'].update(deepcopy(row.get('facts', {})))
        return shown


class RetryAdmissionTests(unittest.TestCase):
    checkpoint = support.AdmissionTests.checkpoint
    frozen = support.AdmissionTests.frozen
    restated = support.AdmissionTests.restated
    discard = support.AdmissionTests.discard
    recover = support.AdmissionTests.recover
    record_failure = support.AdmissionTests.record_failure

    def setUp(self):
        support.AdmissionTests.setUp(self)
        original = self.port
        self.port = RetryPort()
        self.port.placement = original.placement

    def start(self, task='task', frozen=None, retry_of=None, reuse_of=None):
        return guarded_start(self.project, 'objective', owner='owner', run='run', task=task,
                             plan_revision='plan', frozen_packet=frozen or self.frozen(task=task),
                             worktree='current', port=self.port, retry_of=retry_of,
                             reuse_of=reuse_of, accompanying=self.restated())

    def settle(self, admission):
        self.port.workers[admission['native_binding']['dispatchId']].update(
            state='stopped', outcome='failed', facts={
                'liveness': {'verdict': 'exited', 'source': 'resource_release'},
                'resource': {'state': 'released', 'releaseState': 'released'}})

    def parent(self):
        admission = self.start()['admission']
        self.settle(admission)
        self.discard(admission)
        return read(self.project, 'objective')['admissions'][admission['admission_id']]

    def retry(self, parent, frozen=None):
        return self.start(retry_of=parent['admission_id'], frozen=frozen)

    def refused(self, code, call):
        before = read(self.project, 'objective')
        starts = deepcopy(self.port.starts)
        with self.assertRaises(PodError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(read(self.project, 'objective'), before)
        self.assertEqual(self.port.starts, starts)

    def test_retry_has_exact_parent_fresh_route_terminal_and_immutable_history(self):
        parent = self.parent()
        route = {**ROUTE, 'model': 'gpt-6-luna'}
        frozen = self.frozen(route=route)
        result = self.retry(parent, frozen)
        child = result['admission']
        native_parent = parent['native_binding']['dispatchId']
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(child['admission_id'], admission_identity(
            objective='objective', run_id='run', task_id='task', retry_of_dispatch_id=native_parent))
        self.assertEqual(parent['admission_id'], digest({'objective': 'objective', 'run': 'run', 'task': 'task'}))
        self.assertNotIn('retry_of', parent)
        self.assertNotEqual(child['admission_id'], parent['admission_id'])
        self.assertEqual(child['retry_of'], parent['admission_id'])
        self.assertEqual(child['recovery']['retry_of_dispatch_id'], native_parent)
        self.assertEqual(child['route_decision']['effective']['model'], 'gpt-6-luna')
        self.assertEqual(child['route_decision']['pinned_route'], load(self.project)['pinned'])
        self.assertEqual(self.port.starts[-1]['retry_of'], native_parent)
        self.assertEqual(self.port.starts[-1]['task'], parent['task_id'])
        self.assertIsNone(self.port.starts[-1]['terminal'])
        self.assertNotEqual(child['native_binding']['terminalHandle'], parent['native_binding']['terminalHandle'])
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], parent)
        self.assertIsNone(parent['report'])  # No fabricated report fills the original unknown.
        self.assertEqual(self.retry(parent, frozen)['admission']['admission_id'], child['admission_id'])
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(len(self.port.workers), 2)
        self.assertEqual(len(self.port.starts), 2)

    def test_retry_preserves_result_disposition_obligation(self):
        parent = self.start()['admission']
        self.settle(parent)
        self.refused('integration_pending', lambda: self.retry(parent))
        self.discard(parent)
        self.assertEqual(self.retry(parent)['status'], 'bound')

    def test_parent_must_be_an_owned_admission_on_the_same_task_and_run(self):
        parent = self.parent()
        for name in ('missing', parent['native_binding']['dispatchId'], 7):
            with self.subTest(parent=name):
                self.refused('retry_parent_unverified', lambda: self.start(retry_of=name))
        self.refused('retry_parent_unverified', lambda: self.start(task='other', retry_of=parent['admission_id']))
        self.refused('native_authority_unverified', lambda: guarded_start(
            self.project, 'objective', owner='owner', run='foreign', task='task', plan_revision='plan',
            frozen_packet=self.frozen(), retry_of=parent['admission_id'], port=self.port))
        self.refused('retry_reuse_conflict', lambda: self.start(
            retry_of=parent['admission_id'], reuse_of=parent['admission_id']))

    def parent_refusals(self, cases):
        parent = self.parent()
        dispatch = parent['native_binding']['dispatchId']
        original = deepcopy(self.port.workers[dispatch])
        for name, mutate in cases:
            with self.subTest(case=name):
                self.port.workers[dispatch] = deepcopy(original)
                mutate(self.port.workers[dispatch])
                self.refused('retry_parent_unsettled', lambda: self.retry(parent))

    def test_parent_needs_positive_exit_despite_settled_dispatch(self):
        self.parent_refusals((
            ('live', lambda row: row['facts']['liveness'].update(verdict='live')),
            ('stale', lambda row: row['facts']['liveness'].update(verdict='unverifiable', reason='stale_status')),
            ('absent', lambda row: row['facts'].pop('liveness')),
            ('unknown_worker', lambda row: row.update(state='outcome_unknown'))))

    def test_parent_needs_terminal_failed_or_stopped_outcome(self):
        self.parent_refusals((
            ('success', lambda row: row.update(outcome='succeeded')),
            ('active', lambda row: row.update(outcome='in_progress')),
            ('cancelled', lambda row: row.update(outcome='cancelled')),
            ('stop_failed', lambda row: row.update(state='ready'))))

    def test_parent_resource_unknown_pending_and_failed_release_hold(self):
        self.parent_refusals((
            ('missing_resource', lambda row: row['facts'].pop('resource')),
            ('unknown', lambda row: row['facts']['resource'].update(state='unknown')),
            ('pending', lambda row: row['facts']['resource'].update(releaseState='release_pending')),
            ('failed', lambda row: row['facts']['resource'].update(releaseState='release_unknown'))))

    def test_retry_uses_current_eligibility_and_stale_packet_refuses(self):
        parent = self.parent()
        frozen = self.frozen()
        set_route(self.personal, support.SOL, 'disabled', displayed=load(self.project))
        self.refused('preference_changed', lambda: self.retry(parent, frozen))
        self.refused('route_ineligible', lambda: self.retry(parent))
        set_route(self.personal, support.SOL, 'enabled', displayed=load(self.project))
        self.assertEqual(self.retry(parent)['status'], 'bound')

    def test_retry_obeys_current_exact_pin_and_records_fresh_selection(self):
        from pod.config import set_pin
        parent = self.parent()
        set_pin(self.personal, support.LUNA, displayed=load(self.project))
        self.refused('pin_mismatch', lambda: self.retry(parent))
        child = self.retry(parent, self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'}))['admission']
        self.assertEqual(child['route_decision']['pinned_route'], support.LUNA)
        self.assertNotEqual(child['route_decision']['preference_revision'], parent['route_decision']['preference_revision'])

    def test_retry_obeys_recorded_direct_user_constraints(self):
        parent = self.parent()
        constraints_update(self.project, 'objective', owner='owner', action='add', value={
            'id': 'claude-only', 'kind': 'only_agents', 'provenance': 'user_direct', 'value': ['claude']})
        self.refused('constraint_excluded', lambda: self.retry(parent))
        child = self.retry(parent, self.frozen(route={**ROUTE, 'agent': 'claude', 'model': 'claude-sonnet-5-5'}))['admission']
        self.assertIn('claude-only', child['route_decision']['constraint_refs'])

    def test_retry_rechecks_native_authority_and_authorized_placement(self):
        parent = self.parent()
        original = self.port.read_native
        with patch.object(self.port, 'read_native', side_effect=lambda *args, **kwargs: {
                **original(*args, **kwargs), 'authoritative': False, 'owner': None}):
            self.refused('native_authority_unverified', lambda: self.retry(parent))
        self.port.placement['path'] = '/unrelated'
        self.refused('worktree_binding_changed', lambda: self.retry(parent))

    def test_safety_failure_history_blocks_an_alternative_retry(self):
        parent = self.parent()
        self.record_failure(parent, kind='safety_refusal', source='native refusal')
        prior = read(self.project, 'objective')['admissions'][parent['admission_id']]
        self.refused('safety_refusal', lambda: self.retry(parent, self.frozen(
            route={**ROUTE, 'model': 'gpt-6-luna'})))
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], prior)

    def test_availability_failure_can_select_another_current_route_after_exit(self):
        parent = self.parent()
        self.record_failure(parent, kind='unavailable', source='native availability')
        prior = read(self.project, 'objective')['admissions'][parent['admission_id']]
        self.refused('route_failed', lambda: self.retry(parent))
        child = self.retry(parent, self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'}))['admission']
        self.assertEqual(child['route_decision']['effective']['model'], 'gpt-6-luna')
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], prior)

    def test_authentication_refusal_blocks_same_task_across_routes_until_explicit_clear(self):
        parent = self.parent()
        self.record_failure(parent, kind='auth_failed', source='native authentication refusal')
        from pod.selection import validate_choice
        failures = read(self.project, 'objective')['admissions'][parent['admission_id']]['failures']
        # Existing model-local suppression admits another model; retry must retain the Task hold.
        self.assertTrue(validate_choice(load(self.project), [], failures,
                                       {**ROUTE, 'model': 'gpt-6-luna'}, task='task')['allowed'])
        self.refused('route_failed', lambda: self.retry(parent))
        alternate = self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})
        self.refused('route_failed', lambda: self.retry(parent, alternate))
        self.record_failure(parent, kind='auth_failed', clear=True, cleared_by='user', decision={
            'provenance': 'user_direct', 'instruction': 'Authentication resolved; clear this recorded refusal'})
        old = read(self.project, 'objective')['admissions'][parent['admission_id']]
        self.assertEqual(self.retry(parent, alternate)['status'], 'bound')
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], old)
        self.assertTrue(old['failures'][0]['cleared_at'])

    def test_authentication_retry_after_expiry_uses_existing_failure_policy(self):
        parent = self.parent()
        self.record_failure(parent, kind='auth_failed', source='native authentication wait',
                            retry_after='2000-01-01T00:00:00Z')
        self.assertEqual(self.retry(parent)['status'], 'bound')

    def test_permission_refusal_stays_unresolved_and_cannot_select_a_new_route(self):
        parent = self.parent()
        self.port.receipt = {'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
                             'error': {'code': 'permission_required', 'message': 'waiting for permission'}}
        child = self.retry(parent)['admission']
        self.assertEqual(child['state'], 'unresolved')
        self.assertTrue(child['recovery']['start_observations'])
        self.refused('unresolved_prior_attempt', lambda: self.retry(
            parent, self.frozen(route={**ROUTE, 'model': 'gpt-6-luna'})))
        self.assertEqual(len(self.port.workers), 1)

    def test_parent_may_have_reused_an_earlier_terminal_with_a_different_origin(self):
        first = self.start()['admission']
        self.port.workers['dispatch-1']['outcome'] = 'succeeded'
        self.discard(first)
        parent = self.start(task='task2', reuse_of=first['admission_id'])['admission']
        self.settle(parent)
        self.discard(parent)
        original = self.port.show_worker
        def show(dispatch):
            shown = original(dispatch)
            if dispatch == 'dispatch-2':
                shown['result']['projection']['resource']['ownerDispatchId'] = dispatch
                shown['result']['terminalResource'] = {
                    'ownerDispatchId': dispatch, 'originDispatchId': 'dispatch-1',
                    'terminalHandle': parent['native_binding']['terminalHandle'], 'worktreeId': 'current',
                    'ownershipState': 'released', 'releaseState': 'released'}
            return shown
        with patch.object(self.port, 'show_worker', side_effect=show):
            result = self.start(task='task2', retry_of=parent['admission_id'])
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['recovery']['retry_of_dispatch_id'], 'dispatch-2')
        self.assertNotEqual(result['admission']['native_binding']['terminalHandle'], parent['native_binding']['terminalHandle'])
        self.assertEqual(len(self.port.workers), 3)

    def test_same_coordinator_run_may_serve_another_explicit_objective(self):
        from pod.config import effective
        from pod.ledger import checkpoint
        from tests.common import kernel_map
        value = {'schema': 'pod-checkpoint/v3', 'criteria': ['peer works'], 'plan_revision': 'plan',
                 'candidate': 'peer-candidate', 'policy_revision': effective(self.project)['revision'],
                 'native_refs': [], 'assignments': [], 'questions': [], 'verification_gaps': ['peer works'],
                 'next_safe_action': 'inspect', **kernel_map(['peer works'])}
        checkpoint(self.project, 'peer', owner='owner', value=value, native={'runtime': 'runtime'})
        peer = read(self.project, 'peer')
        parent = self.parent()
        self.assertEqual(self.retry(parent)['status'], 'bound')
        self.assertEqual(read(self.project, 'peer'), peer)

    def test_identical_request_recovers_and_changed_packet_or_placement_cannot_replace_it(self):
        parent = self.parent()
        frozen = self.frozen()
        child = self.retry(parent, frozen)['admission']
        changed = self.frozen(route={**ROUTE, 'reason': 'different assignment'})
        self.refused('admission_conflict', lambda: self.retry(parent, changed))
        self.refused('admission_conflict', lambda: guarded_start(
            self.project, 'objective', owner='owner', run='run', task='task', plan_revision='plan',
            frozen_packet=frozen, worktree='path:/other', retry_of=parent['admission_id'], port=self.port))
        self.assertEqual(self.start()['admission']['admission_id'], parent['admission_id'])
        self.assertEqual(self.retry(parent, frozen)['admission']['admission_id'], child['admission_id'])
        self.assertEqual(len(self.port.starts), 2)

    def unresolved(self, parent, *, no_uuid=False):
        self.port.omit_uuid = no_uuid
        if no_uuid:
            with self.assertRaises(PodError) as caught:
                self.retry(parent)
            self.assertEqual(caught.exception.code, 'native_request_invalid')
        else:
            with patch('pod.operations._bind', side_effect=PodError('read_lost', 'lost receipt read')):
                with self.assertRaises(PodError):
                    self.retry(parent)
        self.port.omit_uuid = False
        rows = read(self.project, 'objective')['admissions']
        child = next(row for row in rows.values() if row.get('retry_of') == parent['admission_id'])
        self.assertEqual(child['state'], 'unresolved')
        self.assertTrue(child['recovery']['start_observations'])
        return child

    def test_completed_request_recovery_binds_exact_child_without_another_start(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.request_receipt = self.port.child_receipt('dispatch-2')
        result = self.recover(child)
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['native_binding']['dispatchId'], 'dispatch-2')
        self.assertEqual(result['admission']['recovery']['start_observations'][:-1], child['recovery']['start_observations'])
        self.assertEqual(len(self.port.starts), 2)

    def test_pending_joins_same_uuid_parent_and_original_route_after_preferences_change(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.state = 'pending'
        set_route(self.personal, support.SOL, 'disabled', displayed=load(self.project))
        result = self.recover(child)
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(self.port.starts[-1]['retry_request'], REQUEST)
        self.assertEqual(self.port.starts[-1]['retry_of'], parent['native_binding']['dispatchId'])
        self.assertEqual(result['admission']['request'], child['request'])
        self.assertEqual(len(self.port.workers), 2)
        self.assertEqual(self.recover(result['admission'])['status'], 'bound')
        self.assertEqual(len(self.port.starts), 3)

    def test_absent_request_adopts_child_and_never_the_original_parent(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.state = 'absent'
        result = self.recover(child)
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['native_binding']['dispatchId'], 'dispatch-2')
        self.assertEqual(len(self.port.starts), 2)

    def test_no_uuid_recovery_filters_all_historical_attempts_by_exact_parent(self):
        original = self.parent()
        parent = self.retry(original)['admission']
        self.settle(parent)
        self.discard(parent)
        child = self.unresolved(parent, no_uuid=True)
        result = self.recover(child)
        self.assertEqual(result['status'], 'bound')
        self.assertEqual(result['admission']['native_binding']['dispatchId'], 'dispatch-3')
        self.assertEqual(len(self.port.workers), 3)
        self.assertEqual(len(self.port.starts), 3)

    def test_absent_child_holds_and_does_not_adopt_parent_or_start_again(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.port.workers.pop('dispatch-2')
        for _ in range(2):
            result = self.recover(child)
            self.assertEqual(result['status'], 'unresolved')
            self.assertEqual(result['admission']['error']['code'], 'native_attempt_absent')
        self.assertEqual(len(self.port.starts), 2)

    def test_multiple_matching_children_hold_without_starting(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.port.workers['duplicate'] = deepcopy(self.port.workers['dispatch-2'])
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['admission']['error']['code'], 'native_attempt_ambiguous')
        self.assertEqual(len(self.port.starts), 2)

    def test_wrong_parent_receipt_contradiction_survives_incomplete_later_readback(self):
        parent = self.parent()
        self.port.child_parent = 'wrong-parent'
        result = self.retry(parent)
        child = result['admission']
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(child['error']['code'], 'native_retry_parent_mismatch')
        self.port.workers['dispatch-2'].pop('retry_of')
        self.port.state = 'absent'
        self.assertEqual(self.recover(child)['admission'], child)
        self.assertEqual(len(self.port.starts), 2)

    def test_wrong_parent_without_uuid_is_held_before_any_later_adoption(self):
        parent = self.parent()
        self.port.omit_uuid = True
        self.port.child_parent = 'wrong-parent'
        child = self.retry(parent)['admission']
        self.assertIsNone(child['request_uuid'])
        self.assertEqual(child['error']['code'], 'native_retry_parent_mismatch')
        self.port.workers['dispatch-2']['retry_of'] = 'dispatch-1'
        self.assertEqual(self.recover(child)['admission'], child)
        self.assertEqual(len(self.port.starts), 2)

    def test_native_retry_cannot_adopt_the_released_parent_terminal(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.request_receipt = self.port.child_receipt('dispatch-2')
        self.port.workers['dispatch-2']['terminal'] = parent['native_binding']['terminalHandle']
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['admission']['error']['code'], 'native_retry_terminal_reused')
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], parent)
        self.assertEqual(len(self.port.starts), 2)

    def wrong_child_placement(self):
        self.port.workers['dispatch-2']['worktree'] = 'foreign'
        self.port.native_placements['id:foreign'] = {**self.port.placement, 'path': '/foreign-child'}

    def assert_placement_hold(self, result):
        self.assertEqual(result['status'], 'unresolved')
        self.assertIsNone(result['admission']['native_binding'])
        self.assertEqual(result['admission']['error']['code'], 'native_retry_placement_mismatch')
        self.assertEqual(len(self.port.starts), 2)

    def test_direct_retry_checks_the_actual_child_placement_before_binding(self):
        parent = self.parent()
        original = self.port.start_worker
        def start(**kwargs):
            receipt = original(**kwargs)
            self.wrong_child_placement()
            return receipt
        with patch.object(self.port, 'start_worker', side_effect=start):
            self.assert_placement_hold(self.retry(parent))

    def test_completed_retry_checks_the_actual_child_placement_before_binding(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.wrong_child_placement()
        self.port.request_receipt = self.port.child_receipt('dispatch-2')
        self.assert_placement_hold(self.recover(child))

    def test_absent_retry_checks_the_actual_child_placement_before_adoption(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.wrong_child_placement()
        self.port.state = 'absent'
        self.assert_placement_hold(self.recover(child))

    def test_no_uuid_retry_checks_placement_and_preserves_a_known_mismatch(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.wrong_child_placement()
        held = self.recover(child)
        self.assert_placement_hold(held)
        self.port.native_placements['id:foreign'] = None
        self.assertEqual(self.recover(child)['admission'], held['admission'])

    def test_unknown_actual_child_placement_holds_without_adoption(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.wrong_child_placement()
        self.port.native_placements['id:foreign'] = None
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertIsNone(result['admission']['native_binding'])
        self.assertEqual(len(self.port.starts), 2)

    def test_malformed_inventory_prevents_unique_retry_adoption(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.port.find_rows = [{'dispatchId': 'dispatch-2'}, {'projection': {'taskId': 'task'}}]
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['admission']['error']['code'], 'native_attempt_ambiguous')
        self.assertEqual(len(self.port.starts), 2)

    def test_completed_receipt_cannot_bind_wrong_parent_or_the_parent_itself(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.request_receipt = self.port.child_receipt('dispatch-1')
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['admission']['error']['code'], 'native_retry_parent_mismatch')
        self.assertEqual(len(self.port.starts), 2)

    def test_completed_wrong_parent_receipt_is_archived_and_sticky_on_absent_recovery(self):
        parent = self.parent()
        child = self.unresolved(parent)
        receipt = {**self.port.child_receipt('dispatch-2'), 'retryOfDispatchId': 'wrong-parent'}
        self.port.request_receipt = receipt
        result = self.recover(child)
        held = result['admission']
        self.assertEqual(held['error']['code'], 'native_retry_parent_mismatch')
        ref = held['recovery']['start_observations'][-1]
        archived = json.loads((objective_root(self.project, 'objective') / 'native-start' / (ref + '.json')).read_text())
        self.assertEqual(archived['observation']['request_show_receipt'], receipt)
        self.port.state = 'absent'
        self.port.workers['dispatch-2'].pop('retry_of')
        self.assertEqual(self.recover(child)['admission'], held)
        self.assertEqual(len(self.port.starts), 2)

    def test_pending_wrong_parent_receipt_persists_without_a_duplicate_worker(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.state = 'pending'
        original = self.port.child_receipt
        with patch.object(self.port, 'child_receipt', side_effect=lambda dispatch: {
                **original(dispatch), 'retryOfDispatchId': 'wrong-parent'}):
            held = self.recover(child)['admission']
        self.assertEqual(held['error']['code'], 'native_retry_parent_mismatch')
        self.port.workers['dispatch-2'].pop('retry_of')
        self.assertEqual(self.recover(child)['admission'], held)
        self.assertEqual(len(self.port.workers), 2)
        self.assertEqual(len(self.port.starts), 3)

    def test_no_uuid_wrong_parent_candidate_is_sticky_on_incomplete_readback(self):
        parent = self.parent()
        child = self.unresolved(parent, no_uuid=True)
        self.port.workers['dispatch-2']['retry_of'] = 'wrong-parent'
        held = self.recover(child)['admission']
        self.assertEqual(held['error']['code'], 'native_retry_parent_mismatch')
        self.port.workers['dispatch-2'].pop('retry_of')
        self.assertEqual(self.recover(child)['admission'], held)
        self.assertEqual(len(self.port.starts), 2)

    def test_pending_retry_does_not_replay_when_parent_exit_becomes_unverifiable(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.state = 'pending'
        self.port.workers['dispatch-1']['facts']['liveness']['verdict'] = 'unverifiable'
        result = self.recover(child)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['admission']['error']['code'], 'retry_parent_unsettled')
        self.assertEqual(len(self.port.starts), 2)

    def test_missing_native_parent_field_holds_until_exact_evidence_arrives(self):
        parent = self.parent()
        child = self.unresolved(parent)
        self.port.request_receipt = self.port.child_receipt('dispatch-2')
        self.port.workers['dispatch-2'].pop('retry_of')
        result = self.recover(child)
        self.assertEqual(result['admission']['error']['code'], 'native_retry_parent_unverified')
        self.port.workers['dispatch-2']['retry_of'] = 'dispatch-1'
        self.assertEqual(self.recover(child)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)

    def test_native_circuit_breaker_refusal_is_preserved_and_never_bypassed(self):
        parent = self.parent()
        self.port.receipt = {'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
                             'error': {'code': 'task_not_startable', 'message': 'circuit broken',
                                       'data': {'taskId': 'task', 'runId': 'run', 'retryOf': 'dispatch-1',
                                                'status': 'failed', 'nextSteps': 'report circuit breaker'}}}
        result = self.retry(parent)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['admission']['recovery']['next'], 'report circuit breaker')
        self.assertTrue(result['admission']['recovery']['start_observations'])
        self.assertEqual(self.retry(parent)['status'], 'deferred')
        self.assertEqual(len(self.port.starts), 2)
        self.assertEqual(len(self.port.workers), 1)

    def test_native_task_startability_is_not_inferred_from_failed_dispatch(self):
        parent = self.parent()
        self.port.receipt = {'runtime': 'runtime', 'exit': 1, 'request_uuid': REQUEST,
                             'error': {'code': 'task_not_startable', 'message': 'Task is blocked',
                                       'data': {'taskId': 'task', 'runId': 'run', 'retryOf': 'dispatch-1',
                                                'status': 'blocked', 'nextSteps': 'inspect blocked Task through Orca'}}}
        result = self.retry(parent)
        self.assertEqual(result['status'], 'deferred')
        self.assertIsNone(result['admission']['native_binding'])
        self.assertEqual(result['admission']['recovery']['next'], 'inspect blocked Task through Orca')
        self.assertEqual(self.retry(parent)['status'], 'deferred')
        self.assertEqual(len(self.port.starts), 2)
        self.assertEqual(len(self.port.workers), 1)

    def test_internal_admission_contract_reaches_the_retry_boundary(self):
        parent = self.parent()
        frozen = self.frozen()
        with patch('pod.operations.OrcaPort', return_value=self.port):
            result = internal_run('admission', {
                'project': str(self.project), 'objective': 'objective', 'owner': 'owner',
                'run': 'run', 'task': 'task', 'plan_revision': 'plan', 'packet': frozen,
                'retry_of': parent['admission_id'], 'worktree': 'current', 'map': self.restated()})
        self.assertEqual(result['admission']['retry_of'], parent['admission_id'])
        self.assertEqual(self.port.starts[-1]['retry_of'], 'dispatch-1')


class RetryAdapterTests(unittest.TestCase):
    def test_exact_retry_and_pending_replay_argv_reach_native_adapter(self):
        envelope = {'ok': True, '_meta': {'runtimeId': 'runtime'},
                    'result': {'runId': 'run', 'taskId': 'task', 'dispatchId': 'child',
                               'retryOfDispatchId': 'parent', 'mutation': {'requestId': REQUEST}}}
        process = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b'')
        for request in (None, REQUEST):
            with self.subTest(request=request), patch('pod.orca.executable', return_value='orca'), \
                    patch('pod.orca.subprocess.run', return_value=process) as runner:
                result = OrcaPort().start_worker(run='run', task='task', owner='owner', route=ROUTE,
                                                 worktree='current', retry_of='parent', retry_request=request)
                expected = ['orca', 'orchestration', 'worker-start', '--task', 'task', '--run', 'run',
                            '--worktree', 'current', '--agent', 'codex', '--model', ROUTE['model'],
                            '--effort', 'medium', '--retry-of', 'parent']
                if request:
                    expected += ['--retry-request', request]
                self.assertEqual(runner.call_args.args[0], expected + ['--json'])
                self.assertEqual(result['retryOfDispatchId'], 'parent')

    def test_retry_adapter_rejects_terminal_escape_invalid_parent_and_duplicate_flags(self):
        base = ['orchestration', 'worker-start', '--task', 'task', '--run', 'run', '--worktree', 'current']
        invalid = [base + ['--terminal', 'term', '--retry-of', 'parent', '--json'],
                   base + ['--agent', 'codex', '--model', 'model', '--retry-of', 'parent', '--retry-of', 'parent', '--json']]
        for value in ('-bad', 'x\x00y', 'x\ud800', ''):
            invalid.append(base + ['--agent', 'codex', '--model', 'model', '--retry-of', value, '--json'])
        with patch('pod.orca.subprocess.run') as runner:
            for argv in invalid:
                with self.subTest(argv=ascii(argv)), self.assertRaises(PodError) as caught:
                    mutate_command(argv)
                self.assertEqual(caught.exception.code, 'unsupported_orca_mutation')
            runner.assert_not_called()

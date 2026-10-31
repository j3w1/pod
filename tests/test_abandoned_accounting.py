"""Fenced abandonment accounts the exact assignment, never proves no execution."""

from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import consume_report, logical_projection, objective_root, read, retry_parent
from pod.operations import STARTED_STATES, _binding_from_show, guarded_start, recover_admission
from tests import test_operations as support
from tests.kernel_support import REQUEST, FakePort


class AbandonedPort(FakePort):
    def start_worker(self, **kwargs):
        receipt = super().start_worker(**kwargs)
        dispatch = receipt['dispatchId']
        if getattr(self, 'normal_start', False):
            self.workers[dispatch]['effects'] = []
            return receipt
        terminal = self.workers[dispatch]['terminal']
        effects = [{'kind': 'worktree', 'id': kwargs['worktree'], 'action': 'reused'},
                   *[{'kind': 'dispatch_input', 'role': 'agent', 'id': terminal, 'state': state}
                     for state in ('accepted', 'turn_unobserved')]]
        receipt.update(state='outcome_unknown', stage='turn_start_unobserved', turnStart='unobserved',
                       prompt={'requestId': REQUEST, 'stages': ['input_accepted']}, effects=effects)
        if getattr(self, 'null_launch', False):
            receipt['launch'] = {key: {name: None for name in ('agent', 'model', 'effort')}
                                 for key in ('requested', 'effective')}
        self.workers[dispatch].update(state='start_unknown', effects=effects)
        raw = json.dumps({'ok': True, 'result': {**receipt, 'mutation': {'requestId': REQUEST}},
                          '_meta': {'runtimeId': self.runtime}}).encode()
        receipt['native_observation'] = {
            'transport': 'completed', 'exit': 1,
            'stdout': {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                       'base64': base64.b64encode(raw).decode()}}
        self.request_receipt = deepcopy({key: value for key, value in receipt.items()
                                        if key != 'native_observation'})
        return receipt

    def show_worker(self, dispatch):
        shown = super().show_worker(dispatch)
        item = self.workers[dispatch]
        result = shown['result']
        result['worker'].update(stage=item['state'], effects=deepcopy(item['effects']))
        if getattr(self, 'null_launch', False) and item['state'] in ('start_unknown', 'abandoned'):
            result['worker']['startOptions']['launch'] = deepcopy(self.request_receipt['launch'])
        if item['state'] == 'abandoned':
            result['dispatch'].update(lastFailure='abandoned', capabilityRevokedAt='2026-10-01T12:00:00Z',
                                      completedAt='2026-10-01T12:00:00Z', lastHeartbeatAt=None)
            result['projection']['stage'].update(worker='abandoned', detail='abandoned', activity='unknown')
            released = item.get('released', False)
            result['projection'].update(
                liveness=item.get('liveness', {'verdict': 'unverifiable', 'reason': 'missing_status'}),
                resource={'state': 'released' if released else 'owned',
                          'ownerDispatchId': None if released else dispatch,
                          'releaseState': 'released' if released else 'retained', 'terminalState': 'retained'})
            result['terminalResource'] = {
                'ownerDispatchId': None if released else dispatch, 'terminalHandle': item['terminal'],
                'worktreeId': item['worktree'], 'ownershipState': 'released' if released else 'owned',
                'releaseState': 'released' if released else 'retained', 'retainedReason': 'user_requested'}
        return shown


class AbandonedFixture(unittest.TestCase):
    setUp = support.AdmissionTests.setUp
    checkpoint = support.AdmissionTests.checkpoint
    frozen = support.AdmissionTests.frozen
    restated = support.AdmissionTests.restated
    discard = support.AdmissionTests.discard
    start = support.AdmissionTests.start

    def held(self):
        port = AbandonedPort()
        port.placement = self.port.placement
        self.port = port
        with self.assertRaises(PodError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, 'native_start_unsettled')
        admission = next(iter(read(self.project, 'objective')['admissions'].values()))
        self.assertEqual(admission['state'], 'unresolved')
        self.port.workers['dispatch-1'].update(state='abandoned', outcome='failed')
        return admission

    def settled(self, role):
        """A no-output abandoned attempt; a review's assurance serving is set on the record, as that map is heavy."""
        from pod.ledger import update_admission
        self.port = AbandonedPort()
        self.port.placement = {'repository': None, 'repo_key': None, 'path': str(self.project.resolve()),
                               'branch': None, 'runtime': 'runtime'}
        with self.assertRaises(PodError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, 'native_start_unsettled')
        admission = next(iter(read(self.project, 'objective')['admissions'].values()))
        if role == 'review':
            update_admission(self.project, 'objective', owner='owner', admission_id=admission['admission_id'],
                             update=lambda row: row.update(role='review', serves=['PA']))
        admission = read(self.project, 'objective')['admissions'][admission['admission_id']]
        self.assertEqual(admission['role'], role)
        self.port.workers['dispatch-1'].update(state='abandoned', outcome='failed')
        return admission

    def normal(self):
        port = AbandonedPort()
        port.placement = self.port.placement
        port.normal_start = True
        self.port = port

    def account(self, admission, reason='Account this fenced nonsuccess; execution remains unknown'):
        return recover_admission(self.project, 'objective', owner='owner',
                                 admission_id=admission['admission_id'], worktree='current',
                                 port=self.port, abandoned_reason=reason)

    def refused(self, admission, change, code='native_abandoned_unverified'):
        shown = self.port.show_worker('dispatch-1')
        change(shown)
        before = read(self.project, 'objective')
        with patch.object(self.port, 'show_worker', return_value=shown), self.assertRaises(PodError) as caught:
            self.account(admission)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(read(self.project, 'objective'), before)
        self.assertEqual(len(self.port.starts), 1)


class AbandonedAccountingTests(AbandonedFixture):
    def test_exact_accounting_frees_only_logical_slot_and_keeps_unknown_resources_and_history(self):
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        original = deepcopy(admission)
        ref = admission['recovery']['start_observations'][0]
        path = objective_root(self.project, 'objective') / 'native-start' / (ref + '.json')
        raw = path.read_bytes()
        before_show = self.port.show_worker('dispatch-1')
        result = self.account(admission)
        row = result['admission']
        self.assertEqual((result['status'], result['action']), ('bound', 'accounted_abandoned'))
        proof = row['abandoned_accounting']
        self.assertEqual(proof['execution'], 'unknown')
        self.assertEqual(proof['previous_error'], original['error'])
        self.assertEqual((proof['recorded_by'], proof['native_abandoner']), ('owner', 'unknown'))
        for key in ('request_uuid', 'task_id', 'run_id', 'packet_id', 'candidate', 'result', 'failures', 'reuse_of'):
            self.assertEqual(row[key], original[key])
        self.assertEqual(self.port.show_worker('dispatch-1'), before_show)
        self.assertEqual(path.read_bytes(), raw)
        native = self.port.read_native('owner', authority_runs=('run',), assignments=(row,))
        self.assertEqual(logical_projection(self.project, native, objective='objective')['outstanding_ids'], [])
        self.assertFalse(native['assignments'][0]['retry_eligible'])
        self.assertEqual(self.account(admission)['admission'], row)
        self.assertEqual(len(self.port.starts), 1)
        with self.assertRaises(PodError) as caught: self.start('next-task', frozen=self.frozen())
        self.assertEqual(caught.exception.code, 'integration_pending')
        self.discard(row, 'No accepted output; native resources remain user-requested retained')
        self.port.normal_start = True
        self.assertEqual(self.start('next-task', frozen=self.frozen())['status'], 'bound')

    def outstanding(self, row):
        native = self.port.read_native('owner', authority_runs=('run',), assignments=(row,))
        return logical_projection(self.project, native, objective='objective')

    def test_never_started_attempt_is_never_recorded_as_started_exited_or_successful(self):
        admission = self.held()
        before = read(self.project, 'objective')
        with self.assertRaises(PodError) as caught:
            guarded_start(self.project, 'objective', owner='owner', run='run', task='never-started',
                          frozen_packet=self.frozen(task='never-started'), plan_revision='plan',
                          worktree='current', port=self.port, abandoned_reason='nothing exists to account')
        self.assertEqual(caught.exception.code, 'unknown_admission')
        self.assertEqual(read(self.project, 'objective'), before)
        row = self.account(admission)['admission']
        self.assertNotIn(self.port.show_worker('dispatch-1')['result']['worker']['state'], STARTED_STATES)
        proof = row['abandoned_accounting']
        self.assertEqual((proof['classification'], proof['execution']), ('abandoned_no_observed_start', 'unknown'))
        self.assertEqual((row['report'], row['changed_paths'], row['disposition']), (None, None, None))
        text = json.dumps(row).lower()
        for claim in ('never_started', 'never started', 'exited', 'process_exited', 'succeeded'):
            self.assertNotIn(claim, text)
        self.assertEqual(len(self.port.starts), 1)

    def test_unknown_start_with_exited_process_settles_and_releases_the_slot(self):
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        result = self.account(admission)
        self.assertEqual((result['status'], result['release'], result['hold']), ('bound', 'process_exited', None))
        self.assertEqual(result['admission']['abandoned_accounting']['execution'], 'unknown')
        self.assertEqual(self.outstanding(result['admission'])['outstanding_ids'], [])
        self.assertEqual(len(self.port.starts), 1)

    def test_released_resource_metadata_never_clears_a_live_or_unverifiable_process(self):
        admission = self.held()
        self.port.workers['dispatch-1']['released'] = True
        for verdict in ('alive', 'live', 'unverifiable', 'unknown'):
            with self.subTest(verdict=verdict):
                self.port.workers['dispatch-1']['liveness'] = {'verdict': verdict, 'reason': 'test'}
                result = self.account(admission)
                self.assertEqual((result['release'], result['hold'] is not None), (None, True))
                self.assertEqual(self.outstanding(result['admission'])['outstanding_ids'],
                                 [admission['admission_id']])
        self.port.workers['dispatch-1'].pop('liveness')  # a missing verdict is unknown, not gone
        shown = self.port.show_worker('dispatch-1')
        shown['result']['projection'].pop('liveness')
        with patch.object(self.port, 'show_worker', return_value=shown):
            self.assertEqual(self.outstanding(read(self.project, 'objective')['admissions'][admission['admission_id']])
                             ['outstanding_ids'], [admission['admission_id']])
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        self.assertEqual(self.account(admission)['release'], 'process_exited')
        self.assertEqual(self.outstanding(read(self.project, 'objective')['admissions'][admission['admission_id']])
                         ['outstanding_ids'], [])

    def test_bound_attempt_that_becomes_abandoned_keeps_its_claim_before_any_accounting(self):
        from pod.governor import _active
        self.normal()
        bound = self.start()['admission']
        self.port.workers['dispatch-1'].update(state='abandoned', outcome='failed')
        for verdict in ('alive', 'unverifiable'):
            with self.subTest(verdict=verdict):
                self.port.workers['dispatch-1']['liveness'] = {'verdict': verdict, 'reason': 'test'}
                held = self.outstanding(bound)
                self.assertEqual(held['outstanding_ids'], [bound['admission_id']])
                self.assertEqual(held['outstanding'][0]['hold']['process'], verdict)
                self.assertEqual(len(_active(held)), 1)
                # While the process may run there is no settled result to discard, and no replacement.
                with self.assertRaises(PodError) as refused:
                    self.discard(bound, 'abandoned; replacing')
                self.assertEqual(refused.exception.detail['detail'], 'disposition_invalid')
                with self.assertRaises(PodError) as caught:
                    self.start('next-task', frozen=self.frozen(task='next-task'))
                self.assertEqual(caught.exception.code, 'abandoned_claim_held')
                self.assertEqual(len(self.port.starts), 1)
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        self.assertEqual(self.outstanding(bound)['outstanding_ids'], [])

    def test_a_pending_effect_target_is_a_claim_across_worktrees_and_an_unrelated_one_is_not(self):
        from pod.abandoned import refuse_held_claim
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        shown = self.port.show_worker('dispatch-1')
        shown['result']['worker']['effects'].append({'kind': 'push', 'id': 'origin/main', 'state': 'pending'})
        with patch.object(self.port, 'show_worker', return_value=shown):
            row = self.account(admission)['admission']
            projection = self.outstanding(row)
        hold = projection['outstanding'][0]['hold']
        self.assertEqual(hold['effects'], [{'kind': 'push', 'id': 'origin/main', 'state': 'pending'}])
        other = {'repo_key': hold['repo_key'], 'path': '/work/b'}
        with self.assertRaises(PodError) as caught:
            refuse_held_claim(projection, {**other, 'branch': 'main'})
        self.assertEqual(caught.exception.code, 'abandoned_claim_held')
        refuse_held_claim(projection, {**other, 'branch': 'feature'})
        projection['outstanding'][0]['hold']['effects'] = [{'kind': 'tool', 'id': None, 'state': None}]
        with self.assertRaises(PodError):
            refuse_held_claim(projection, {**other, 'branch': 'feature'})

    def held_effects(self, effects):
        """Account an exited attempt with these pending effects; the projection a new admission is checked against."""
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        shown = self.port.show_worker('dispatch-1')
        shown['result']['worker']['effects'].extend(effects)
        with patch.object(self.port, 'show_worker', return_value=shown):
            return self.outstanding(self.account(admission)['admission'])

    @staticmethod
    def push(ident):
        return {'kind': 'push', 'id': ident, 'state': 'pending'}

    @staticmethod
    def elsewhere(projection, branch):
        return {'repo_key': projection['outstanding'][0]['hold']['repo_key'], 'path': '/work/b', 'branch': branch}

    def test_the_seventeenth_held_effect_is_held_like_the_first(self):
        from pod.abandoned import refuse_held_claim
        projection = self.held_effects([self.push(f'origin/other-{index}') for index in range(16)]
                                       + [self.push('origin/main')])
        self.assertEqual(len(projection['outstanding'][0]['hold']['effects']), 17)
        with self.assertRaises(PodError) as caught:
            refuse_held_claim(projection, self.elsewhere(projection, 'main'))
        self.assertEqual(caught.exception.code, 'abandoned_claim_held')
        refuse_held_claim(projection, self.elsewhere(projection, 'feature'))

    def test_a_long_held_target_is_compared_in_full_never_truncated(self):
        from pod.abandoned import refuse_held_claim
        target = 'origin/' + 'x' * 300
        projection = self.held_effects([self.push(target)])
        self.assertEqual(projection['outstanding'][0]['hold']['effects'][0]['id'], target)
        with self.assertRaises(PodError):
            refuse_held_claim(projection, self.elsewhere(projection, 'x' * 300))
        refuse_held_claim(projection, self.elsewhere(projection, 'x' * 128))

    def test_effects_past_the_bound_fail_closed_and_hold_the_whole_repository(self):
        from pod.abandoned import MAX_PENDING_EFFECTS, refuse_held_claim
        projection = self.held_effects([self.push(f'origin/other-{index}') for index in range(MAX_PENDING_EFFECTS + 6)]
                                       + [self.push('origin/main')])
        self.assertEqual(projection['outstanding'][0]['hold']['effects'][-1]['kind'], 'unknown')
        with self.assertRaises(PodError):
            refuse_held_claim(projection, self.elsewhere(projection, 'feature'))

    def test_the_same_target_name_in_another_repository_is_independent(self):
        from pod.abandoned import refuse_held_claim
        hold = {'worktree': 'w', 'repo_key': 'repo-a', 'branch': 'feature', 'path': '/work/a', 'paths': [],
                'surfaces': [], 'process': 'exited',
                'effects': [{'kind': 'push', 'id': 'origin/main', 'state': 'pending'}]}
        held = {'outstanding': [{'admission_id': 'a', 'hold': hold}]}
        with self.assertRaises(PodError):
            refuse_held_claim(held, {'repo_key': 'repo-a', 'path': '/work/b', 'branch': 'main'})
        refuse_held_claim(held, {'repo_key': 'repo-b', 'path': '/work/b', 'branch': 'main'})
        # An unknown repository on either side cannot establish independence.
        with self.assertRaises(PodError):
            refuse_held_claim(held, {'repo_key': None, 'path': '/work/b', 'branch': 'main'})
        with self.assertRaises(PodError):
            refuse_held_claim({'outstanding': [{'admission_id': 'a', 'hold': {**hold, 'repo_key': None}}]},
                              {'repo_key': 'repo-b', 'path': '/work/b', 'branch': 'main'})

    def test_held_abandoned_reviewer_on_the_exact_candidate_still_blocks_validation(self):
        from pod.governor import _active
        from pod.ledger import update_admission
        sha = 'a' * 40
        reviewer = self.settled('review')
        update_admission(self.project, 'objective', owner='owner', admission_id=reviewer['admission_id'],
                         update=lambda row: row.update(candidate=sha))
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'alive', 'reason': 'test'}
        row = self.account(reviewer)['admission']
        held = self.outstanding(row)
        self.assertEqual(held['outstanding'][0]['candidate'], sha)
        self.assertEqual(len(_active(held, ignore_review_commit=sha)), 1)
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        cleared = self.outstanding(row)
        self.assertEqual(_active(cleared, ignore_review_commit=sha), [])
        self.assertEqual(_active(cleared), [])

    def test_unknown_start_with_live_process_settles_but_the_hold_remains(self):
        admission = self.held()
        for verdict in ('alive', 'unverifiable'):
            self.port.workers['dispatch-1']['liveness'] = {'verdict': verdict, 'reason': 'test'}
            result = self.account(admission)
            row = result['admission']
            self.assertEqual((result['status'], result['release']), ('bound', None))
            self.assertEqual(row['abandoned_accounting']['execution'], 'unknown')
            placement = admission['recovery']['placement_binding']
            self.assertEqual({key: result['hold'][key] for key in ('worktree', 'branch', 'path')},
                             {'worktree': row['native_binding']['worktreeId'],
                              'branch': placement['branch'], 'path': placement['path']})
            held = self.outstanding(row)
            self.assertEqual(held['outstanding_ids'], [admission['admission_id']])
            self.assertEqual(held['outstanding'][0]['hold'], result['hold'])
            with self.assertRaises(PodError) as caught:
                self.start('next-task', frozen=self.frozen(task='next-task'))
            self.assertEqual(caught.exception.code, 'abandoned_claim_held')
            self.assertEqual(len(self.port.starts), 1)
            # Exit evidence arrives later: the same record releases without another accounting or worker.
            self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
            self.assertEqual(self.outstanding(row)['outstanding_ids'], [])
            break

    def test_bound_abandoned_attempt_without_observed_start_evidence_is_refused_and_ordinary_failure_binds(self):
        self.normal()
        bound = self.start()['admission']
        self.assertEqual(bound['state'], 'bound')
        self.port.workers['dispatch-1'].update(state='abandoned', outcome='failed')
        before = read(self.project, 'objective')
        with self.assertRaises(PodError) as caught:
            self.account(bound)
        self.assertEqual(caught.exception.code, 'native_abandoned_unverified')
        self.assertEqual(read(self.project, 'objective'), before)
        self.port.workers['dispatch-1'].update(state='failed', outcome='failed',
                                               liveness={'verdict': 'exited', 'reason': 'terminal_closed'})
        self.assertEqual(_binding_from_show(self.port.show_worker('dispatch-1'), bound, 'dispatch-1')['dispatchId'],
                         'dispatch-1')
        self.assertNotIn('abandoned_accounting', read(self.project, 'objective')['admissions'][bound['admission_id']])

    def test_independent_scope_is_usable_while_the_same_scope_stays_held(self):
        from pod.abandoned import refuse_held_claim
        row = self.account(self.held())['admission']
        projection = self.outstanding(row)
        placement = self.port.placement
        with self.assertRaises(PodError) as caught:
            refuse_held_claim(projection, placement)
        self.assertEqual(caught.exception.code, 'abandoned_claim_held')
        refuse_held_claim(projection, {**placement, 'path': '/elsewhere', 'branch': 'other'})
        hold = {'worktree': 'w', 'repo_key': 'repo', 'branch': 'feature', 'path': '/work/a', 'paths': [], 'surfaces': []}
        held = {'outstanding': [{'admission_id': 'a', 'hold': hold}]}
        for same in ({'path': '/work/a', 'branch': 'x', 'repo_key': 'repo'},
                     {'path': '/work/b', 'branch': 'feature', 'repo_key': 'repo'}):
            with self.assertRaises(PodError):
                refuse_held_claim(held, same)
        for independent in ({'path': '/work/b', 'branch': 'other', 'repo_key': 'repo'},
                            {'path': '/work/b', 'branch': 'feature', 'repo_key': 'another'}, None):
            refuse_held_claim(held, independent)

    def test_a_retained_reused_resource_is_not_treated_as_released(self):
        admission = self.held()
        shown = self.port.show_worker('dispatch-1')
        self.assertEqual(shown['result']['projection']['resource']['releaseState'], 'retained')
        self.assertEqual(shown['result']['terminalResource']['retainedReason'], 'user_requested')
        result = self.account(admission)
        self.assertEqual((result['release'], self.outstanding(result['admission'])['outstanding_ids']),
                         (None, [admission['admission_id']]))

    def test_a_relevant_external_effect_still_blocks_after_the_agent_exits(self):
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        shown = self.port.show_worker('dispatch-1')
        shown['result']['worker']['effects'].append({'kind': 'push', 'id': 'origin/feature', 'state': 'pending'})
        with patch.object(self.port, 'show_worker', return_value=shown):
            result = self.account(admission)
            self.assertEqual(result['release'], None)
            self.assertIsNotNone(result['hold'])
            self.assertEqual(self.outstanding(result['admission'])['outstanding_ids'], [admission['admission_id']])
        # The effect is read back as resolved: the same record then clears.
        self.assertEqual(self.outstanding(result['admission'])['outstanding_ids'], [])

    def test_wrong_identity_or_another_attempts_result_is_refused(self):
        admission = self.held()
        self.port.workers['dispatch-1']['released'] = True
        for change in (
            lambda s: s['result']['projection']['resource'].update(ownerDispatchId='dispatch-9'),
            lambda s: s['result']['terminalResource'].update(ownerDispatchId='dispatch-9'),
            lambda s: s['result']['terminalResource'].update(terminalHandle='term-other'),
            lambda s: s['result']['dispatch'].update(taskId='other-task'),
            lambda s: s['result']['worker'].update(dispatchId='dispatch-9'),
        ):
            shown = self.port.show_worker('dispatch-1'); change(shown)
            before = read(self.project, 'objective')
            with patch.object(self.port, 'show_worker', return_value=shown), self.assertRaises(PodError):
                self.account(admission)
            self.assertEqual(read(self.project, 'objective'), before)
        # A retained resource never loses its owner silently.
        self.port.workers['dispatch-1']['released'] = False
        self.refused(admission, lambda s: s['result']['projection']['resource'].update(ownerDispatchId=None))
        self.assertEqual(len(self.port.starts), 1)

    def test_stale_runtime_is_refused(self):
        admission = self.held()
        before = read(self.project, 'objective')
        native = self.port.read_native('owner', authority_runs=('run',))
        with patch.object(self.port, 'read_native', return_value={**native, 'runtime': 'old-runtime'}), \
                self.assertRaises(PodError) as caught:
            self.account(admission)
        self.assertEqual(caught.exception.code, 'native_authority_unverified')
        shown = self.port.show_worker('dispatch-1')
        shown['runtime'] = 'old-runtime'
        with patch.object(self.port, 'show_worker', return_value=shown), self.assertRaises(PodError) as caught:
            self.account(admission)
        self.assertEqual(caught.exception.code, 'native_identity_unverified')
        self.assertEqual(read(self.project, 'objective'), before)
        self.assertEqual(len(self.port.starts), 1)

    def test_repeated_recovery_is_idempotent_and_takes_no_extra_slot(self):
        admission = self.held()
        first = self.account(admission)
        self.assertIsNotNone(first['hold'])
        for _ in range(3):
            again = self.account(admission)
            self.assertEqual(again['admission'], first['admission'])
            self.assertEqual((again['action'], again['release'], again['hold']), ('reuse', None, first['hold']))
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        released = self.account(admission)
        self.assertEqual((released['admission'], released['release'], released['hold']),
                         (first['admission'], 'process_exited', None))
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'alive', 'reason': 'test'}
        again = self.account(admission)
        self.assertEqual((again['release'], again['hold'] is not None), (None, True))
        self.assertEqual(self.outstanding(first['admission'])['outstanding_ids'], [admission['admission_id']])
        stored = read(self.project, 'objective')
        self.assertEqual(len(stored['admissions']), 1)
        self.assertEqual(self.outstanding(first['admission'])['outstanding_ids'], [admission['admission_id']])
        with self.assertRaises(PodError) as caught:
            self.account(admission, reason='A different reason must not overwrite the record')
        self.assertEqual(caught.exception.code, 'admission_conflict')
        self.assertEqual(read(self.project, 'objective'), stored)
        self.assertEqual(len(self.port.starts), 1)
        self.assertEqual(len(self.port.workers), 1)

    def test_normal_binding_and_report_cannot_promote_accounting_to_started_output(self):
        admission = self.held()
        row = self.account(admission)['admission']
        with self.assertRaises(PodError) as caught:
            _binding_from_show(self.port.show_worker('dispatch-1'), row, 'dispatch-1')
        self.assertEqual(caught.exception.code, 'native_start_unsettled')
        with self.assertRaises(PodError) as caught:
            consume_report(self.project, 'objective', owner='owner', admission_id=row['admission_id'],
                           observation={})
        self.assertEqual(caught.exception.code, 'report_attempt_unverified')
        with self.assertRaises(PodError) as caught:
            retry_parent(read(self.project, 'objective'), objective='objective', owner='owner',
                         run_id='run', task_id='task', retry_of=row['admission_id'])
        self.assertEqual(caught.exception.code, 'retry_parent_unverified')

    def test_unresolved_start_later_proved_observed_is_accounted_as_observed_not_unknown(self):
        changes = (
            lambda s: s['result']['worker']['effects'].append(
                {'kind': 'dispatch_input', 'role': 'agent', 'id': 'term-dispatch-1', 'state': 'turn_observed'}),
            lambda s: s['result']['worker'].update(turnStart='observed'),
            lambda s: s['result']['worker'].update(prompt={'stages': ['input_accepted', 'turn_started']}),
            lambda s: s['result']['dispatch'].update(lastHeartbeatAt='2026-10-01T11:59:00Z'),
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                admission = self.held()
                shown = self.port.show_worker('dispatch-1')
                change(shown)
                with patch.object(self.port, 'show_worker', return_value=shown):
                    result = self.account(admission)
                row = result['admission']
                proof = row['abandoned_accounting']
                self.assertEqual((row['state'], result['action']), ('bound', 'accounted_abandoned'))
                self.assertEqual((proof['classification'], proof['execution']), ('abandoned_observed_start', 'observed'))
                self.assertTrue(proof['observed_start'])
                self.assertEqual((row['report'], row['changed_paths'], row['disposition']), (None, None, None))
                self.assertIsNotNone(result['hold'])
                with self.assertRaises(PodError) as caught:
                    _binding_from_show(shown, row, 'dispatch-1')
                self.assertEqual(caught.exception.code, 'native_start_unsettled')
                self.assertEqual(len(self.port.starts), 1)

    def test_bound_admission_abandoned_after_an_observed_start_is_accounted_without_a_report(self):
        self.normal()
        bound = self.start()['admission']
        self.port.workers['dispatch-1'].update(state='abandoned', outcome='failed',
                                               liveness={'verdict': 'exited', 'reason': 'terminal_closed'})
        shown = self.port.show_worker('dispatch-1')
        shown['result']['worker']['startedAt'] = '2026-10-01T11:00:00Z'
        shown['result']['dispatch']['lastHeartbeatAt'] = '2026-10-01T11:30:00Z'
        with patch.object(self.port, 'show_worker', return_value=shown):
            result = self.account(bound)
            row = result['admission']
            self.assertEqual((row['state'], row['native_binding']), ('bound', bound['native_binding']))
            self.assertEqual(row['abandoned_accounting']['classification'], 'abandoned_observed_start')
            self.assertEqual(row['abandoned_accounting']['observed_start']['workerStartedAt'], '2026-10-01T11:00:00Z')
            self.assertEqual((result['release'], result['hold']), ('process_exited', None))
            self.assertEqual((row['report'], row['effective_evidence'], row['route_decision']),
                             (None, bound['effective_evidence'], bound['route_decision']))
            self.assertEqual(self.account(bound)['admission'], row)
            self.assertEqual(self.outstanding(row)['outstanding_ids'], [])
        self.assertEqual(len(self.port.starts), 1)
        # An unobserved record never turns observed later, and a recorded observed start never regresses.
        regress = self.port.show_worker('dispatch-1')
        with patch.object(self.port, 'show_worker', return_value=regress), self.assertRaises(PodError):
            self.port.read_native('owner', authority_runs=('run',), assignments=(row,))

    def test_missing_or_conflicting_fence_remains_unresolved(self):
        admission = self.held()
        for change in (
            lambda s: s['result']['dispatch'].pop('capabilityRevokedAt'),
            lambda s: s['result']['dispatch'].update(lastFailure='other'),
            lambda s: s['result']['projection'].update(outcome='succeeded'),
            lambda s: s['result']['worker'].update(effects=[]),
            lambda s: s['result']['terminalResource'].update(ownerDispatchId='other'),
        ):
            self.refused(admission, change)

    def test_native_identity_conflicts_keep_existing_guards(self):
        admission = self.held()
        for change in (
            lambda s: s.update(runtime='other'),
            lambda s: s['result']['dispatch'].update(taskId='other'),
            lambda s: s['result']['projection'].update(runId='other'),
            lambda s: s['result']['worker'].update(worktreeId='other'),
        ):
            shown = self.port.show_worker('dispatch-1'); change(shown)
            with patch.object(self.port, 'show_worker', return_value=shown), self.assertRaises(PodError):
                self.account(admission)
        self.assertEqual(read(self.project, 'objective')['admissions'][admission['admission_id']]['state'], 'unresolved')
        self.assertEqual(len(self.port.starts), 1)

    def test_observed_or_missing_original_receipt_and_request_conflict_refuse(self):
        admission = self.held()
        before = deepcopy(self.port.request_receipt)
        self.port.request_receipt['turnStart'] = 'observed'
        with self.assertRaises(PodError): self.account(admission)
        self.port.request_receipt = before
        self.port.request_id = 'other'
        with self.assertRaises(PodError) as caught: self.account(admission)
        self.assertEqual(caught.exception.code, 'native_request_mismatch')
        self.port.request_id = REQUEST
        ref = admission['recovery']['start_observations'][0]
        (objective_root(self.project, 'objective') / 'native-start' / (ref + '.json')).unlink()
        with self.assertRaises(PodError) as caught: self.account(admission)
        self.assertEqual(caught.exception.code, 'native_evidence_unavailable')
        self.assertEqual(len(self.port.starts), 1)

    def test_pending_request_is_accounted_without_replay(self):
        admission = self.held()
        self.port.state = 'pending'
        self.assertEqual(self.account(admission)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 1)

    def test_internal_original_admission_contract_reaches_accounting_and_rejects_changed_packet(self):
        admission = self.held()
        request = {'project': str(self.project), 'objective': 'objective', 'owner': 'owner',
                   'run': 'run', 'task': 'task', 'plan_revision': 'plan', 'packet': self.frozen(),
                   'worktree': 'current', 'abandoned_reason': 'Account this exact fenced original request'}
        with patch('pod.operations.OrcaPort', return_value=self.port):
            result = internal_run('admission', request)
            self.assertEqual(result['action'], 'accounted_abandoned')
            self.assertEqual(result['admission']['admission_id'], admission['admission_id'])
            from pod.records import packet
            changed = {**request, 'packet': packet({**request['packet']['body'],
                                                   'report_contract': 'A changed report contract'})}
            with self.assertRaises(PodError) as caught: internal_run('admission', changed)
        self.assertEqual(caught.exception.code, 'admission_conflict')
        self.assertEqual(len(self.port.starts), 1)

    def test_accounting_cannot_integrate_or_reuse_unobserved_terminal(self):
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        row = self.account(admission)['admission']
        state = read(self.project, 'objective')
        value = {key: value for key, value in state['checkpoint'].items()
                 if key not in ('pod_version', 'bundle_digest', 'seq')}
        value.update(self.restated())
        value['dispositions'] = [{'admission': row['admission_id'], 'integrated_into': 'candidate',
                                  'attestation': 'This must never count as accepted output'}]
        from pod.ledger import checkpoint
        with self.assertRaises(PodError) as caught:
            checkpoint(self.project, 'objective', owner='owner', value=value, native={'runtime': 'runtime'})
        self.assertEqual(caught.exception.detail['detail'], 'disposition_invalid')
        self.discard(row)
        with self.assertRaises(PodError) as caught:
            self.start('next-task', frozen=self.frozen(), reuse_of=row['admission_id'])
        self.assertEqual(caught.exception.code, 'reuse_unavailable')
        self.assertEqual(len(self.port.starts), 1)

    def test_later_observed_native_start_cannot_keep_freeing_the_accounted_slot(self):
        row = self.account(self.held())['admission']
        shown = self.port.show_worker('dispatch-1')
        shown['result']['worker']['turnStart'] = 'observed'
        with patch.object(self.port, 'show_worker', return_value=shown), self.assertRaises(PodError):
            self.port.read_native('owner', authority_runs=('run',), assignments=(row,))
        self.assertEqual(len(self.port.starts), 1)

    def test_missing_authority_and_conflicting_stored_request_cannot_account(self):
        admission = self.held()
        before = read(self.project, 'objective')
        with patch.object(self.port, 'read_native', return_value={'authoritative': False}), self.assertRaises(PodError) as caught:
            self.account(admission)
        self.assertEqual(caught.exception.code, 'native_authority_unverified')
        self.assertEqual(read(self.project, 'objective'), before)
        from pod.ledger import update_admission
        update_admission(self.project, 'objective', owner='owner', admission_id=admission['admission_id'],
                         update=lambda row: row['recovery'].update(request_conflict='unresolved'))
        with self.assertRaises(PodError): self.account(admission)
        self.assertEqual(len(self.port.starts), 1)

    def test_interruption_after_readback_archive_recovers_once_without_start_or_proof_loss(self):
        admission = self.held()
        with patch('pod.abandoned.update_admission', side_effect=PodError('native_authority_unverified', 'lost')):
            with self.assertRaises(PodError): self.account(admission)
        held = read(self.project, 'objective')['admissions'][admission['admission_id']]
        self.assertEqual(held['state'], 'unresolved')
        refs = held['recovery']['start_observations']
        self.assertEqual(len(refs), 2)
        accounted = self.account(admission)['admission']
        self.assertEqual(accounted['recovery']['start_observations'], refs)
        self.assertEqual(self.account(admission)['admission'], accounted)
        self.assertEqual(len(self.port.starts), 1)

    def test_accounting_input_for_a_new_task_never_reserves_or_starts(self):
        self.held()
        before = read(self.project, 'objective')
        with self.assertRaises(PodError) as caught:
            guarded_start(self.project, 'objective', owner='owner', run='run', task='new-task',
                          frozen_packet=self.frozen(task='new-task'), plan_revision='plan',
                          worktree='current', port=self.port, abandoned_reason='account existing only')
        self.assertEqual(caught.exception.code, 'unknown_admission')
        self.assertEqual(read(self.project, 'objective'), before)
        self.assertEqual(len(self.port.starts), 1)

    def test_any_immutable_observed_start_blocks_accounting_even_with_unobserved_latest_read(self):
        admission = self.held()
        from pod.operations import _observe_start
        receipt = {**self.port.request_receipt, 'state': 'ready', 'turnStart': 'observed'}
        _observe_start(self.project, 'objective', owner='owner', admission_id=admission['admission_id'],
                       observation={'receipt': receipt}, retry_request=None, reference=receipt)
        with self.assertRaises(PodError) as caught: self.account(admission)
        self.assertEqual(caught.exception.code, 'native_abandoned_unverified')
        self.assertEqual(len(self.port.starts), 1)

    def test_actual_worktree_must_join_original_placement_before_accounting(self):
        admission = self.held()
        self.port.placement['branch'] = 'different-branch'
        with self.assertRaises(PodError) as caught: self.account(admission)
        self.assertEqual(caught.exception.code, 'worktree_binding_changed')
        self.assertEqual(read(self.project, 'objective')['admissions'][admission['admission_id']]['state'], 'unresolved')
        self.assertEqual(len(self.port.starts), 1)

    def test_reused_terminal_null_launch_keeps_prior_effective_route_with_provenance(self):
        prior = self.start()['admission']
        self.port.workers['dispatch-1']['outcome'] = 'succeeded'
        self.discard(prior)
        port = AbandonedPort()
        port.placement = self.port.placement
        port.workers = deepcopy(self.port.workers)
        port.workers['dispatch-1']['effects'] = []
        port.null_launch = True
        self.port = port
        with self.assertRaises(PodError) as caught:
            self.start('followup', frozen=self.frozen(), reuse_of=prior['admission_id'])
        self.assertEqual(caught.exception.code, 'native_start_unsettled')
        admission = next(row for row in read(self.project, 'objective')['admissions'].values()
                         if row['task_id'] == 'followup')
        self.port.workers['dispatch-2'].update(state='abandoned', outcome='failed')
        row = self.account(admission)['admission']
        self.assertEqual(row['route_decision']['effective'], prior['route_decision']['effective'])
        self.assertEqual(row['effective_evidence']['inherited']['admission'], prior['admission_id'])
        self.assertEqual(row['effective_evidence']['inherited']['terminal'], prior['native_binding']['terminalHandle'])
        self.assertFalse(row['route_decision']['effective_unknown'])
        self.assertEqual(len(self.port.starts), 1)


class AbandonedReadinessTests(AbandonedFixture):
    """A settled non-success attempt with no live claim is history; a live or unknown claim still blocks."""

    def accounted(self, liveness):
        admission = self.held()
        self.port.workers['dispatch-1']['liveness'] = liveness
        row = self.account(admission)['admission']
        from pod.ledger import update_admission
        # An unknown effective route would veto readiness for an ordinary attempt.
        update_admission(self.project, 'objective', owner='owner', admission_id=row['admission_id'],
                         update=lambda value: value['route_decision'].update(effective_unknown=True))
        return row

    def evaluate(self, row):
        from pod import governor
        from pod.config import effective
        native = self.port.read_native('owner', authority_runs=('run',), assignments=(row,))
        projection = logical_projection(self.project, native, objective='objective')
        verdict = governor._evaluate(
            {'kind': 'validation_rerun', 'candidate': 'candidate', 'unit': 'default', 'target': 'origin/main',
             'reason': 'readiness', 'effects': []},
            read(self.project, 'objective'), {'units': {}, 'actions': [], 'counters': {}},
            effective(self.project)['policy']['waste_governor'],
            native_projection=projection, project=self.project, objective='objective')
        return projection, {item['code'] for item in verdict['reasons']}, {item['code'] for item in verdict['warnings']}

    def test_settled_non_success_with_no_live_claim_is_non_material_and_reported_as_history(self):
        from pod import governor
        row = self.accounted({'verdict': 'exited', 'reason': 'terminal_closed'})
        projection, reasons, warnings = self.evaluate(row)
        self.assertEqual(governor._active(projection), [])
        self.assertNotIn('effective_unknown', reasons)
        self.assertIn('abandoned_attempt_history', warnings)
        finalization = governor.finalization(read(self.project, 'objective'), {'actions': []})
        self.assertNotIn('report_consumption', finalization['remaining'])
        self.assertEqual(finalization['history'], [row['admission_id']])

    def test_live_or_unknown_process_claim_is_material(self):
        from pod import governor
        for verdict in ('alive', 'unverifiable'):
            with self.subTest(verdict=verdict):
                self.setUp()
                row = self.accounted({'verdict': verdict, 'reason': 'test'})
                projection, reasons, warnings = self.evaluate(row)
                self.assertEqual(len(governor._active(projection)), 1)
                self.assertEqual(len(governor._active(projection, ['unrelated-task'])), 1)
                self.assertIn('effective_unknown', reasons)
                self.assertIn('integration_unsettled', reasons)
                self.assertNotIn('abandoned_attempt_history', warnings)

    def test_without_the_exact_native_read_nothing_is_history(self):
        from pod import governor
        row = self.accounted({'verdict': 'exited', 'reason': 'terminal_closed'})
        self.assertEqual(governor._settled_abandoned(read(self.project, 'objective')['admissions'], None), set())
        self.assertEqual(governor._settled_abandoned(read(self.project, 'objective')['admissions'],
                                                    {'outstanding': [{'admission_id': row['admission_id']}]}), set())


class AbandonedRoleTests(AbandonedFixture):
    """A no-output abandoned attempt completes for the implement and review roles without a manufactured report."""

    def context(self, row):
        native = self.port.read_native('owner', authority_runs=('run',), assignments=(row,))
        outstanding = logical_projection(self.project, native, objective='objective')['outstanding_ids']
        return {'admissions': read(self.project, 'objective')['admissions'], 'outstanding': outstanding}

    def test_review_role_releases_its_reservation_only_when_the_claim_clears_and_finalizes_without_a_report(self):
        from pod import governor
        from pod.assurance import pending_serving
        admission = self.settled('review')
        served = {'id': 'PA'}
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'alive', 'reason': 'test'}
        row = self.account(admission)['admission']
        self.assertEqual(pending_serving(served, self.context(row)), [admission['admission_id']])
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        self.assertEqual(self.account(admission)['release'], 'process_exited')
        self.assertEqual(pending_serving(served, self.context(row)), [])
        done = governor.finalization(read(self.project, 'objective'), {'actions': []})
        self.assertNotIn('report_consumption', done['remaining'])
        self.assertEqual(done['history'], [admission['admission_id']])
        stored = read(self.project, 'objective')['admissions'][admission['admission_id']]
        self.assertEqual((stored['report'], stored['disposition']), (None, None))
        with self.assertRaises(PodError) as caught:
            consume_report(self.project, 'objective', owner='owner', admission_id=admission['admission_id'],
                           observation={})
        self.assertEqual(caught.exception.code, 'report_attempt_unverified')

    def test_implement_role_still_takes_the_ordinary_discard_before_replacement(self):
        from pod import governor
        admission = self.settled('implement')
        self.port.workers['dispatch-1']['liveness'] = {'verdict': 'exited', 'reason': 'terminal_closed'}
        row = self.account(admission)['admission']
        self.discard(row, 'No accepted output; the abandoned attempt remains history')
        stored = read(self.project, 'objective')['admissions'][admission['admission_id']]
        self.assertEqual(stored['disposition']['kind'], 'discarded')
        self.assertEqual(governor.finalization(read(self.project, 'objective'), {'actions': []})['history'],
                         [admission['admission_id']])


class AbandonedPrecedingReaderTests(AbandonedFixture):
    @classmethod
    def setUpClass(cls):
        # Genuine preceding installed source, not a simulated reader or edited installed copy.
        cls.temporary = tempfile.TemporaryDirectory(prefix='abandoned-previous-',
                                                   dir=os.environ.get('POD_TEST_ROOT'))
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.bundle = Path(cls.temporary.name)
        archive = cls.bundle / 'source.tar'
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(['git', '-C', str(repository), 'archive', '-o', str(archive),
                        'deda03a2eea5cc01ae4ed983f1f441d2d0b483ab', 'skills', 'VERSION'], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter='data')

    def previous(self):
        code = '''import json,sys
from pathlib import Path
from pod.bundle import version
from pod.errors import PodError
from pod.ledger import read
try:
    state=read(Path(sys.argv[1]),'objective')
    result={'status':'read','admissions':len(state['admissions'])}
except PodError as exc:
    result={'status':'refused','code':exc.code,'detail':str(exc)}
print(json.dumps({'version':version(),**result}))
'''
        completed = subprocess.run([sys.executable, '-c', code, str(self.project)], check=True,
                                   env={**os.environ, 'PYTHONPATH': str(self.bundle / 'skills')},
                                   cwd=self.bundle, text=True, capture_output=True, timeout=10)
        return json.loads(completed.stdout)

    def test_genuine_02121_reads_unchanged_records_and_refuses_new_accounting_without_writes(self):
        admission = self.held()
        self.assertEqual(self.previous(), {'version': '0.21.21', 'status': 'read', 'admissions': 1})
        self.account(admission)
        path = objective_root(self.project, 'objective') / 'context.json'
        before = path.read_bytes()
        result = self.previous()
        self.assertEqual((result['version'], result['status'], result['code']),
                         ('0.21.21', 'refused', 'invalid_admission'))
        self.assertIn('abandoned_accounting', result['detail'])
        self.assertEqual(path.read_bytes(), before)

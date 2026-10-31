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
from pod.operations import _binding_from_show, guarded_start, recover_admission
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
        if item['state'] == 'abandoned':
            result['dispatch'].update(lastFailure='abandoned', capabilityRevokedAt='2026-10-01T12:00:00Z',
                                      completedAt='2026-10-01T12:00:00Z', lastHeartbeatAt=None)
            result['projection']['stage'].update(worker='abandoned', detail='abandoned', activity='unknown')
            result['projection'].update(liveness={'verdict': 'unverifiable', 'reason': 'missing_status'},
                                       resource={'state': 'owned', 'ownerDispatchId': dispatch,
                                                 'releaseState': 'retained', 'terminalState': 'retained'})
            result['terminalResource'] = {
                'ownerDispatchId': dispatch, 'terminalHandle': item['terminal'],
                'worktreeId': item['worktree'], 'ownershipState': 'owned',
                'releaseState': 'retained', 'retainedReason': 'user_requested'}
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

    def test_observed_start_in_current_effects_or_fields_refuses_without_changes(self):
        admission = self.held()
        self.refused(admission, lambda s: s['result']['worker']['effects'].append(
            {'kind': 'dispatch_input', 'role': 'agent', 'id': 'term-dispatch-1', 'state': 'turn_observed'}))
        self.refused(admission, lambda s: s['result']['worker'].update(turnStart='observed'))
        self.refused(admission, lambda s: s['result']['worker'].update(prompt={'stages': ['turn_started']}))
        self.refused(admission, lambda s: s['result']['dispatch'].update(lastHeartbeatAt='2026-10-01T11:59:00Z'))

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

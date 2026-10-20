"""Caught no-start facts never replace current authority or runtime fencing."""

from copy import deepcopy
import json
import os
from unittest.mock import patch

from pod import ledger, operations
from pod.errors import PodError
from pod.util import digest
from tests import test_retry_continuity as fixtures

NATIVE_READ = operations.OrcaPort.read_native


class RetryAuthorityTests(fixtures.RetryFixtureCase):
    retry = fixtures.RetryContinuityTests.retry

    @classmethod
    def prepare_seed(cls, seed):
        cls.parent_admission, = seed.started()
        seed.settle(cls.parent_admission)
        seed.discard(cls.parent_admission)

    def setUp(self):
        super().setUp()
        self.parent = ledger.read(self.project, 'objective')['admissions'][self.parent_admission['admission_id']]

    def previous(self):
        return next(row for row in ledger.read(self.project, 'objective')['admissions'].values()
                    if row.get('retry_of') == self.parent['admission_id'])

    def pending(self, previous):
        root = ledger.objective_root(self.project, 'objective') / 'no-start'
        index = root / 'pending' / (previous['admission_id'] + '.json')
        ref = json.loads(index.read_text())['no_start']
        proof = root / (ref + '.json')
        return index, proof

    def recover(self, previous):
        return operations.recover_admission(
            self.project, 'objective', owner='owner', admission_id=previous['admission_id'],
            worktree='current', port=self.port)

    def interrupted(self, change, *, error=None):
        original = self.port.show_worker
        seen = {}

        def show(dispatch):
            if len(ledger.read(self.project, 'objective')['admissions']) == 2 and not seen:
                seen['reserved'] = self.previous()
                change()
                seen['before'] = self.snapshot()
                raise error or PodError('orca_unavailable', 'parent read lost', {'read': 'worker-show'})
            return original(dispatch)

        with patch.object(self.port, 'show_worker', side_effect=show):
            with self.assertRaises(Exception) as caught:
                self.retry(self.parent)
        self.assertEqual(len(self.port.starts), 1)
        return self.previous(), seen, caught.exception

    def displace(self):
        self.port.run_binding.update(coordinator_handle='another-coordinator', consumer_generation=2)

    def restore(self):
        self.port.run_binding = {'id': 'run', 'coordinator_handle': 'owner', 'consumer_generation': 1}

    def assert_held(self, previous, seen):
        self.assertEqual(previous['state'], 'reserved')
        self.assertEqual(self.snapshot(), seen['before'])
        self.assertEqual(previous, seen['reserved'])
        self.assertIsNone(previous['request_uuid'])
        self.assertIsNone(previous['native_binding'])
        self.assertNotIn('no_start', previous['recovery'])

    def assert_recovery(self, previous):
        index, path = self.pending(previous)
        before = {file: file.read_bytes() for file in (index, path)}
        proof = json.loads(before[path])
        self.assertEqual(proof['runtime'], 'runtime')
        self.assertEqual(proof['effect'], 'not_called')
        self.assertEqual(proof['retry_of_dispatch_id'], self.parent['native_binding']['dispatchId'])
        recovered = self.recover(previous)
        self.assertEqual(recovered['status'], 'deferred')
        deferred = recovered['admission']
        self.assertEqual(deferred['admission_id'], previous['admission_id'])
        self.assertIsNone(deferred['request_uuid'])
        self.assertEqual(deferred['recovery']['no_start'], path.stem)
        self.assertEqual(len(self.port.starts), 1)
        self.assertEqual(self.recover(deferred)['admission'], deferred)
        self.assertEqual(self.retry(self.parent)['admission'], deferred)
        child = self.retry(self.parent, deferred)['admission']
        self.assertEqual(child['state'], 'bound')
        self.assertEqual(child['retry_of'], self.parent['admission_id'])
        self.assertEqual(child['retry_after'], deferred['admission_id'])
        self.assertEqual(self.port.starts[-1]['retry_of'], self.parent['native_binding']['dispatchId'])
        self.assertEqual(self.retry(self.parent, deferred)['admission'], child)
        self.assertEqual(len(self.port.starts), 2)
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        return deferred

    def test_foreign_coordinator_cannot_publish_deferral(self):
        previous, seen, error = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        self.assertEqual(error.code, 'native_authority_unverified')
        with self.assertRaises(PodError) as caught:
            ledger.require_authority(self.project, 'objective', owner='owner', native_port=self.port)
        self.assertEqual(caught.exception.code, 'native_authority_unverified')
        self.assertTrue(self.pending(previous)[1].is_file())

    def test_restored_authority_classifies_persisted_no_start_and_successor_once(self):
        previous, seen, _ = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        with self.assertRaises(PodError):
            self.recover(previous)
        self.assertEqual(self.snapshot(), seen['before'])
        self.restore()
        self.assert_recovery(previous)
        self.assertEqual(ledger.read(self.project, 'objective')['admissions'][self.parent['admission_id']],
                         self.parent)

    def test_missing_current_run_holds_until_native_identity_returns(self):
        def missing():
            self.port.run_binding = None
        previous, seen, error = self.interrupted(missing)
        self.assert_held(previous, seen)
        self.assertEqual(error.code, 'native_authority_unverified')
        self.restore()
        self.assert_recovery(previous)

    def test_generation_change_during_real_native_join_cannot_publish_deferral(self):
        read_native = self.port.read_native
        unstable = []

        def read(owner, **kwargs):
            if not unstable:
                return read_native(owner, **kwargs)
            before = {'runtime': 'runtime', 'run': deepcopy(self.port.run_binding)}
            after = deepcopy(before)
            after['run']['consumer_generation'] = 2
            with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'owner'}), \
                    patch('pod.operations.current_run', side_effect=[before, after]):
                return NATIVE_READ(self.port, owner, **kwargs)

        with patch.object(self.port, 'read_native', side_effect=read):
            previous, seen, error = self.interrupted(lambda: unstable.append(True))
        self.assert_held(previous, seen)
        self.assertEqual(error.code, 'native_authority_unverified')
        self.assert_recovery(previous)

    def test_identical_admission_after_restoration_classifies_only_its_original_row(self):
        previous, seen, _ = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        self.restore()
        deferred = self.retry(self.parent)['admission']
        self.assertEqual(deferred['state'], 'deferred')
        self.assertEqual(deferred['admission_id'], previous['admission_id'])
        self.assertEqual(len(self.port.starts), 1)
        self.assertEqual(self.retry(self.parent)['admission'], deferred)
        child = self.retry(self.parent, deferred)['admission']
        self.assertEqual(child['state'], 'bound')
        self.assertEqual(child['retry_after'], previous['admission_id'])
        self.assertEqual(len(self.port.starts), 2)

    def test_unreadable_current_authority_preserves_only_control_flow(self):
        read_native = self.port.read_native
        lost = []
        def read(*args, **kwargs):
            if lost:
                raise PodError('orca_unavailable', 'current Run unavailable')
            return read_native(*args, **kwargs)
        with patch.object(self.port, 'read_native', side_effect=read):
            previous, seen, error = self.interrupted(lambda: lost.append(True))
            self.assert_held(previous, seen)
            self.assertEqual(error.code, 'orca_unavailable')
            with self.assertRaises(PodError):
                self.recover(previous)
            self.assertEqual(self.snapshot(), seen['before'])
        self.assert_recovery(previous)

    def test_old_invocation_cannot_publish_after_actual_owner_runtime_rebind(self):
        def rebind():
            self.change_runtime()
            with self.assertRaises(PodError) as caught:
                ledger.continuity_locked(self.project, 'objective', owner='owner', port=self.port)
            self.assertEqual(caught.exception.code, 'runtime_continuity_ambiguous')
            self.assertIn('Dispatch found', ' '.join(caught.exception.detail['ambiguity']))
            self.owner_op(self.decision(caught.exception))
        previous, seen, error = self.interrupted(rebind)
        self.assertEqual(previous['state'], 'reserved')
        self.assertEqual(previous['runtime'], 'runtime-2')
        self.assertEqual(self.snapshot(), seen['before'])
        self.assertEqual(error.code, 'native_authority_unverified')
        self.assertEqual(self.history()[-1]['provenance'], 'owner')
        history = deepcopy(self.history())
        self.assert_recovery(previous)
        self.assertEqual(self.history(), history)

    def test_changed_runtime_needs_real_continuity_before_later_classification(self):
        previous, seen, _ = self.interrupted(self.change_runtime)
        self.assert_held(previous, seen)
        with self.assertRaises(PodError) as caught:
            self.recover(previous)
        self.assertEqual(caught.exception.code, 'runtime_continuity_ambiguous')
        self.assertEqual(self.snapshot(), seen['before'])
        self.owner_op(self.decision(caught.exception))
        self.assert_recovery(self.previous())

    def test_crash_during_parent_read_has_no_affirmative_no_start(self):
        previous, _, error = self.interrupted(lambda: None, error=RuntimeError('caught caller crash'))
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(previous['state'], 'reserved')
        root = ledger.objective_root(self.project, 'objective') / 'no-start'
        self.assertFalse(root.exists())
        with self.assertRaises(PodError) as caught:
            self.retry(self.parent, previous)
        self.assertEqual(caught.exception.code, 'retry_no_start_unverified')
        held = self.recover(previous)['admission']
        self.assertEqual(held['state'], 'unresolved')
        self.assertEqual(held['error']['code'], 'native_attempt_absent')
        self.assertEqual(len(self.port.starts), 1)

    def test_missing_or_corrupt_persisted_proof_cannot_release_reservation(self):
        previous, seen, _ = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        index, path = self.pending(previous)
        proof = path.read_bytes()
        self.restore()
        for content in (None, b'{}', b'[]'):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                with self.assertRaises(PodError) as caught:
                    self.recover(previous)
                self.assertEqual(caught.exception.code, 'native_evidence_unavailable')
                self.assertEqual(self.snapshot(), seen['before'])
        path.write_bytes(proof)
        self.assert_recovery(previous)

    def test_persisted_proof_requires_exact_admission_run_task_and_parent(self):
        previous, seen, _ = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        index, path = self.pending(previous)
        original_index = index.read_bytes()
        proof = json.loads(path.read_text())
        self.restore()
        for key in ('admission_id', 'run_id', 'task_id', 'retry_of_dispatch_id', 'runtime'):
            with self.subTest(key=key):
                changed = {**proof, key: 'another-identity'}
                ref = digest(changed)
                (path.parent / (ref + '.json')).write_text(json.dumps(changed))
                index.write_text(json.dumps({'no_start': ref}))
                with self.assertRaises(PodError) as caught:
                    self.recover(previous)
                self.assertEqual(caught.exception.code, 'native_evidence_unavailable')
                self.assertEqual(self.snapshot(), seen['before'])
        index.write_bytes(original_index)
        self.assert_recovery(previous)

    def test_corrupt_pending_index_holds_until_original_bytes_return(self):
        previous, seen, _ = self.interrupted(self.displace)
        self.assert_held(previous, seen)
        index, _ = self.pending(previous)
        original = index.read_bytes()
        self.restore()
        for value in ({}, {'no_start': 'invalid'}, {'no_start': None}, []):
            with self.subTest(value=value):
                index.write_text(json.dumps(value))
                with self.assertRaises(PodError) as caught:
                    self.recover(previous)
                self.assertEqual(caught.exception.code, 'native_evidence_unavailable')
                self.assertEqual(self.snapshot(), seen['before'])
        index.write_bytes(original)
        self.assert_recovery(previous)

    def test_unknown_effect_never_uses_no_start_classification(self):
        original = self.port.start_worker
        def lost(**kwargs):
            original(**kwargs)
            raise TimeoutError('response lost after effect')
        with patch.object(self.port, 'start_worker', side_effect=lost):
            with self.assertRaises(TimeoutError):
                self.retry(self.parent)
        previous = self.previous()
        self.assertEqual(previous['state'], 'unresolved')
        self.assertTrue(previous['recovery']['start_observations'])
        self.assertFalse((ledger.objective_root(self.project, 'objective') / 'no-start').exists())
        with self.assertRaises(PodError) as caught:
            self.retry(self.parent, previous)
        self.assertEqual(caught.exception.code, 'retry_no_start_unverified')
        self.assertEqual(self.recover(previous)['status'], 'bound')
        self.assertEqual(len(self.port.starts), 2)

    def pre_start_policy_hold(self, code):
        previous, seen, error = self.interrupted(
            lambda: None, error=PodError(code, 'opaque parent read refusal'))
        self.assert_held(previous, seen)
        self.assertEqual(error.code, 'retry_no_start_unverified')
        for operation in (lambda: self.recover(previous), lambda: self.retry(self.parent, previous)):
            with self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'retry_no_start_unverified')
            self.assertEqual(self.snapshot(), seen['before'])

    def test_pre_start_permission_refusal_remains_held(self):
        self.pre_start_policy_hold('permission_required')

    def test_pre_start_authentication_refusal_remains_held(self):
        self.pre_start_policy_hold('auth_failed')

    def test_pre_start_safety_refusal_remains_held(self):
        self.pre_start_policy_hold('safety_refusal')

    def test_pre_start_unknown_or_circuit_refusal_remains_held(self):
        self.pre_start_policy_hold('task_not_startable')

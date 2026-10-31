"""Synthetic reviewer retries cross real admission, report and assurance boundaries."""

from copy import deepcopy

from pod.config import load, set_pin, set_route
from pod.internal import run as internal_run
from pod.ledger import constraints_update, read
from pod.operations import guarded_start
from tests import kernel_support as support
from tests import test_operations as operations
from tests.test_retry_continuity import RetryFixtureCase


class RetryReviewTests(RetryFixtureCase):
    record_failure = operations.AdmissionTests.record_failure

    def label(self):
        return internal_run('acceptance', {
            'project': str(self.project), 'objective': 'objective', 'criteria': ['PoD#1'],
            'evidence_rows': [], 'candidate': self.candidate, 'policy_revision': 'p', 'sources': [],
            'dependencies': [], 'environment': 'fixture', 'review_required': True,
            'hosted_required': False})['independently_reviewed']

    @classmethod
    def prepare_seed(cls, seed):
        seed.intake({
            'id': 'A', 'kind': 'assurance', 'provenance': 'coordinator', 'parent': 'O1',
            'check': 'independent review', 'scope': {'paths': ['src']},
            'question': 'is src correct?', 'candidate': seed.candidate,
            'existing_evidence': 'local checks', 'insufficiency': 'no independent verdict',
            'state': 'waiting', 'wait': {'class': 'sequenced', 'referent': 'O1'}}, seed.sub('S'))
        cls.old_packet = seed.packet(['A'], role='review')
        parent = seed.start('review', cls.old_packet)['admission']
        seed.settle(parent)
        seed.record_failure(parent, kind='rate_limited', source='synthetic provider quota refusal',
                            retry_after='2000-01-01T00:00:00Z')
        cls.old_observation = seed.observation(parent, cls.old_packet, outcome='blocked')
        seed.consume(parent, cls.old_packet, cls.old_observation, seed.restated())
        cls.parent_admission = read(seed.project, 'objective')['admissions'][parent['admission_id']]
        # A real Git candidate advance is shared read-only; mutable records are copied.
        seed.candidate = support.governance_commit(seed, 'README.md', 'current candidate\n', 'advance')
        rows = seed.stored()
        next(row for row in rows if row['id'] == 'A')['candidate'] = seed.candidate
        seed.write(rows)

    def assurance(self):
        return next(row for row in self.stored() if row['id'] == 'A')

    def observation(self, admission, frozen, *, outcome='succeeded'):
        return {'schema': 'pod-report/v1', 'assignment': frozen['packet_id'],
                'attempt': admission['native_binding']['dispatchId'],
                'candidate': frozen['body']['candidate'], 'outcome': outcome,
                'scope': ['src'], 'files': [],
                'checks': ['independent source audit'] if outcome == 'succeeded' else [],
                'failures': [] if outcome == 'succeeded' else ['quota blocked; no review verdict'],
                'evidence': [], 'questions': [],
                'uncertainty': [] if outcome == 'succeeded' else ['provider archive unavailable']}

    def consume(self, admission, frozen, observation, mapped=None):
        return internal_run('report', {
            'project': str(self.project), 'objective': 'objective',
            'admission_id': admission['admission_id'], 'packet': frozen,
            'report': observation, **({'map': mapped} if mapped else {})})

    def fresh_packet(self, **fields):
        return self.packet(['A'], role='review',
                           route={**support.ROUTE, 'model': 'gpt-6-luna',
                                  'preference_revision': load(self.project)['revision']}, **fields)

    def retry(self, frozen=None, *, parent=None, task='review'):
        return guarded_start(
            self.project, 'objective', owner='owner', run='run', task=task,
            plan_revision='plan', frozen_packet=frozen or self.fresh_packet(),
            port=self.port, retry_of=parent or self.parent_admission['admission_id'])

    def fresh(self, frozen=None, *, task='review-2'):
        """A review of a newer candidate is a new attempt on its own Task; a same-Task retry replays the original spec."""
        return guarded_start(
            self.project, 'objective', owner='owner', run='run', task=task,
            plan_revision='plan', frozen_packet=frozen or self.fresh_packet(), port=self.port)

    def refuses_unchanged(self, code, call):
        before = self.snapshot()
        starts = deepcopy(self.port.starts)
        self.refused(code, call)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.port.starts, starts)

    def assert_old_incomplete(self):
        old = read(self.project, 'objective')['admissions'][self.parent_admission['admission_id']]
        self.assertEqual(old, self.parent_admission)
        self.assertEqual(old['report']['observation'], self.old_observation)
        self.assertEqual(old['failures'][0]['kind'], 'rate_limited')
        self.assertEqual(old['failures'][0]['retry_after'], '2000-01-01T00:00:00Z')
        self.assertIsNone(old['result'])
        self.assertIsNone(old['changed_paths'])
        self.assertIsNone(old['disposition'])
        self.assertFalse(any('accepted_seq' in row for row in self.assurance()['receipts']
                             if row['evidence']['attempt'] == old['admission_id']))

    def satisfied_map(self, admission):
        rows = self.stored()
        assurance = next(row for row in rows if row['id'] == 'A')
        assurance.pop('executor', None)
        assurance.pop('wait', None)
        assurance.update(state='satisfied', evidence=[{'attempt': admission['admission_id']}])
        return {'obligations': rows}

    def test_fresh_review_attempt_qualifies_only_its_own_completed_verdict(self):
        self.assertEqual(self.label(), 'WITHHELD')
        frozen = self.fresh_packet()
        child = self.fresh(frozen)['admission']
        parent = self.parent_admission
        self.assertEqual(child['role'], 'review')
        self.assertEqual(child['serves'], parent['serves'])
        self.assertEqual(child['serves'], ['A'])
        self.assertEqual(child['candidate'], self.candidate)
        self.assertNotEqual(child['candidate'], parent['candidate'])
        self.assertEqual(child['packet_id'], frozen['packet_id'])
        self.assertNotEqual(child['packet_id'], parent['packet_id'])
        self.assertNotIn('retry_of', child)
        self.assertNotEqual(child['native_binding']['taskId'], parent['native_binding']['taskId'])
        self.assertNotEqual(child['native_binding']['dispatchId'], parent['native_binding']['dispatchId'])
        self.assertNotEqual(child['native_binding']['terminalHandle'], parent['native_binding']['terminalHandle'])
        self.assertIsNone(self.port.starts[-1]['retry_of'])
        self.assertEqual(child['route_decision']['effective']['model'], 'gpt-6-luna')
        self.assertIsNone(child['report'])
        self.assertEqual(self.label(), 'WITHHELD')
        observation = self.observation(child, frozen)
        self.refuses_unchanged('report_attempt_unverified', lambda: self.consume(child, frozen, observation))
        self.port.workers[child['native_binding']['dispatchId']]['outcome'] = 'succeeded'
        result = self.consume(child, frozen, observation, self.satisfied_map(child))
        self.assertTrue(result['settled'])
        self.assertEqual(result['ingestion']['status'], 'not_applicable')
        recorded = read(self.project, 'objective')['admissions'][child['admission_id']]
        self.assertEqual(recorded['report']['observation'], observation)
        self.assertEqual(recorded['report']['status'], 'validated_observation')
        self.assertIsNone(recorded['result'])
        self.assertIsNone(recorded['disposition'])
        accepted = [row for row in self.assurance()['receipts'] if 'accepted_seq' in row]
        self.assertEqual([row['evidence']['attempt'] for row in accepted], [child['admission_id']])
        self.assertEqual(self.label(), 'QUALIFIED')
        self.assert_old_incomplete()

    def test_old_blocked_report_and_incomplete_proof_cannot_be_promoted(self):
        parent = self.parent_admission
        self.refuses_unchanged('report_conflict', lambda: self.consume(
            parent, self.old_packet, self.observation(parent, self.old_packet)))
        # Match the original candidate too: incompletion itself must reject proof.
        self.candidate = parent['candidate']
        rows = self.stored()
        next(row for row in rows if row['id'] == 'A')['candidate'] = self.candidate
        self.write(rows)
        self.refuses_unchanged('obligation_unaccounted', lambda: self.write(
            self.satisfied_map(parent)['obligations']))
        self.assertEqual(self.label(), 'WITHHELD')
        self.assert_old_incomplete()

    def test_retry_of_a_review_on_a_newer_candidate_is_refused_without_a_start(self):
        starts = deepcopy(self.port.starts)
        self.refuses_unchanged('assignment_changed', self.retry)
        self.assertEqual(self.port.starts, starts)
        self.assert_old_incomplete()

    def test_new_review_report_rejects_old_packet_dispatch_and_candidate(self):
        frozen = self.fresh_packet()
        child = self.fresh(frozen)['admission']
        self.port.workers[child['native_binding']['dispatchId']]['outcome'] = 'succeeded'
        observation = self.observation(child, frozen)
        self.refuses_unchanged('report_attempt_unverified', lambda: self.consume(
            child, self.old_packet, self.old_observation))
        self.refuses_unchanged('report_attempt_mismatch', lambda: self.consume(
            child, frozen, {**observation, 'attempt': self.old_observation['attempt']}))
        self.refuses_unchanged('report_binding_mismatch', lambda: self.consume(
            child, frozen, {**observation, 'candidate': self.old_observation['candidate']}))
        self.assertEqual(self.label(), 'WITHHELD')
        self.assert_old_incomplete()

    def test_retry_requires_positive_exit_and_known_released_resources(self):
        parent = self.port.workers[self.parent_admission['native_binding']['dispatchId']]
        parent['outcome'] = 'in_progress'
        self.refuses_unchanged('retry_parent_unsettled', self.retry)
        parent['outcome'] = 'failed'
        parent['facts']['liveness']['verdict'] = 'unverifiable'
        self.refuses_unchanged('retry_parent_unsettled', self.retry)
        parent['facts']['liveness']['verdict'] = 'exited'
        parent['facts']['resource']['releaseState'] = 'release_unknown'
        self.refuses_unchanged('retry_parent_unsettled', self.retry)
        parent['facts']['resource']['releaseState'] = 'released'
        self.refuses_unchanged('assignment_changed', self.retry)  # Settled and eligible, but not the same work.
        self.assert_old_incomplete()

    def test_retry_rejects_wrong_role_serves_and_foreign_parent(self):
        self.refuses_unchanged('unbound_assignment', lambda: self.retry(self.packet(['A'], role='implement')))
        self.refuses_unchanged('unbound_assignment', lambda: self.retry(self.packet(['S'], role='review')))
        self.refuses_unchanged('retry_parent_unverified', lambda: self.retry(parent='foreign-admission'))
        self.refuses_unchanged('retry_parent_unverified', lambda: self.retry(task='foreign-task'))
        self.assert_old_incomplete()

    def test_retry_uses_current_route_eligibility_and_pin(self):
        personal = self.root / 'config' / 'pod' / 'config.yaml'
        route = 'codex/gpt-6-luna/medium'
        set_route(personal, route, 'disabled', displayed=load(self.project))
        self.refuses_unchanged('route_ineligible', self.fresh)
        set_route(personal, route, 'enabled', displayed=load(self.project))
        set_pin(personal, 'codex/gpt-6.1-sol/medium', displayed=load(self.project))
        self.refuses_unchanged('pin_mismatch', self.fresh)
        child = self.fresh(self.packet(['A'], role='review'))['admission']
        self.assertEqual(child['route_decision']['pinned_route'], 'codex/gpt-6.1-sol/medium')
        self.assertEqual(child['route_decision']['effective']['model'], 'gpt-6.1-sol')
        self.assert_old_incomplete()

    def test_current_user_constraint_still_holds_a_reviewer_retry(self):
        constraints_update(self.project, 'objective', owner='owner', action='add', value={
            'id': 'claude-only', 'kind': 'only_agents', 'provenance': 'user_direct', 'value': ['claude']})
        self.refuses_unchanged('constraint_excluded', self.fresh)
        self.assert_old_incomplete()

    def test_rate_limit_holds_until_a_recorded_reset_without_rewriting_the_report(self):
        parent = self.parent_admission
        self.record_failure(parent, kind='rate_limited', source='synthetic quota still exhausted',
                            retry_after='2099-01-01T00:00:00Z')
        self.refuses_unchanged('route_failed', lambda: self.fresh(self.packet(['A'], role='review')))
        self.record_failure(parent, kind='rate_limited', clear=True, cleared_by='user', decision={
            'provenance': 'user_direct', 'instruction': 'Quota reset confirmed; clear the recorded limit'})
        reset = read(self.project, 'objective')['admissions'][parent['admission_id']]
        self.assertEqual(reset['report'], parent['report'])
        self.assertEqual(len(reset['failures']), 2)
        self.assertTrue(all(row['cleared_at'] and row['cleared_by'] == 'user' for row in reset['failures']))
        self.assertEqual(self.fresh(self.packet(['A'], role='review'))['status'], 'bound')
        self.assertEqual(read(self.project, 'objective')['admissions'][parent['admission_id']], reset)
        self.assertEqual(self.label(), 'WITHHELD')

"""Production refusals retain removed instruction details without starting work."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import _path, consume_report, read, update_admission
from tests.kernel_support import ROUTE
from tests import test_operations as operation_tests


class InstructionRefusalTests(TestCase):
    setUp = operation_tests.AdmissionTests.setUp
    checkpoint = operation_tests.AdmissionTests.checkpoint
    frozen = operation_tests.AdmissionTests.frozen
    restated = operation_tests.AdmissionTests.restated
    start = operation_tests.AdmissionTests.start

    def assert_effect_free(self, code, rule, action, operation):
        path = _path(self.project, 'objective')
        before = path.read_bytes()
        starts = deepcopy(self.port.starts)
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertIn(rule, str(caught.exception))
        self.assertIn('Next:', str(caught.exception))
        self.assertIn(action, str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.port.starts, starts)

    def test_new_native_default_effort_refuses_before_reservation_or_start(self):
        frozen = self.frozen(route={**ROUTE, 'effort': 'native_default'})
        self.assert_effect_free('effort_required', 'native_default effort', 'supported exact effort',
                               lambda: self.start(frozen=frozen))
        self.assertFalse(read(self.project, 'objective')['admissions'])

    def test_both_preference_races_refuse_with_rule_and_bounded_next_action(self):
        from pod.config import load, set_route
        for route, code in (('codex/gpt-6.1-sol/medium', 'preference_changed'),
                            ('codex/gpt-6-luna/medium', 'preference_revision_stale')):
            with self.subTest(code=code):
                frozen = self.frozen()
                set_route(self.personal, route, 'disabled', displayed=load(self.project))
                self.assert_effect_free(code, 'Preferences changed', 'rechoose at most twice',
                                       lambda: self.start(frozen=frozen))
                set_route(self.personal, route, 'enabled', displayed=load(self.project))

    def test_unsettled_attempt_cannot_be_consumed_or_reused(self):
        first = self.start()['admission']
        dispatch = first['native_binding']['dispatchId']
        original = self.port.show_worker
        # An outcome, terminal ownership or stage alone cannot replace exact Dispatch proof.
        variants = ('active', 'missing_dispatch_status', 'contradictory_stage')
        for variant in variants:
            with self.subTest(variant=variant):
                self.port.workers[dispatch]['outcome'] = 'succeeded' if variant != 'active' else 'running'
                def show(identity):
                    shown = original(identity)
                    if variant == 'missing_dispatch_status':
                        shown['result']['dispatch'].pop('status')
                    elif variant == 'contradictory_stage':
                        shown['result']['projection']['stage']['dispatch'] = 'running'
                    return shown
                with patch.object(self.port, 'show_worker', side_effect=show):
                    self.assert_effect_free('reuse_unavailable', 'settled', 'exact Dispatch',
                                           lambda: self.start('task2', reuse_of=first['admission_id']))
                    observation = {'native_binding': {'runtime': 'runtime'}, 'observation': {}}
                    self.assert_effect_free('report_attempt_unverified', 'exact Dispatch', 'native settlement',
                                           lambda: consume_report(self.project, 'objective', owner='owner',
                                                   admission_id=first['admission_id'], observation=observation))

    def test_reserved_and_unresolved_attempts_keep_the_ceiling(self):
        import yaml
        value = yaml.safe_load(self.personal.read_text())
        value['workers']['max_active'] = 1
        self.personal.write_text(yaml.safe_dump(value))
        first = self.start()['admission']
        for state in ('reserved', 'unresolved'):
            with self.subTest(state=state):
                update_admission(self.project, 'objective', owner='owner', admission_id=first['admission_id'],
                                 update=lambda row: row.update(state=state, native_binding=None))
                self.assert_effect_free('logical_capacity_full', 'outstanding attempts', 'exact native settlement',
                                       lambda: self.start('task2'))

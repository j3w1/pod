"""Terminal assignment proof from a sanitized stopped Orca worker readback."""

from copy import deepcopy
import unittest

from pod.ledger import _native_assignment_settled
from tests.common import receipt


class NativeSettlementTests(unittest.TestCase):
    def setUp(self):
        body=receipt('stopped-worker-show')
        self.shown={'runtime':body['_meta']['runtimeId'],'result':body['result']}

    def test_stopped_failed_dispatch_is_settled_without_success_detail(self):
        self.assertEqual(self.shown['result']['projection']['stage']['detail'],'process_stopped')
        self.assertTrue(_native_assignment_settled(self.shown))
        retained=deepcopy(self.shown)
        retained['result']['projection'].pop('resource')
        self.assertTrue(_native_assignment_settled(retained))
        without_stage_status=deepcopy(self.shown)
        without_stage_status['result']['projection']['stage'].pop('dispatch')
        self.assertTrue(_native_assignment_settled(without_stage_status))

    def test_success_terminal_path_is_still_settled(self):
        completed=deepcopy(self.shown)
        completed['result']['projection']['outcome']='succeeded'
        completed['result']['projection']['stage'].update(dispatch='completed',detail='settled')
        completed['result']['dispatch']['status']='completed'
        self.assertTrue(_native_assignment_settled(completed))

    def test_active_ambiguous_and_foreign_attempts_remain_unsettled(self):
        for name,change in (
            ('in_progress',lambda r:(r['projection'].update(outcome='in_progress'),
                                     r['projection']['stage'].update(dispatch='running'),
                                     r['dispatch'].update(status='running'))),
            ('nonterminal_dispatch',lambda r:(r['projection']['stage'].update(dispatch='running'),
                                               r['dispatch'].update(status='running'))),
            ('missing_status',lambda r:(r['projection']['stage'].pop('dispatch'),
                                        r['dispatch'].pop('status'))),
            ('missing_authoritative_status',lambda r:r['dispatch'].pop('status')),
            ('unknown_authoritative_status',lambda r:r['dispatch'].update(status='unknown')),
            ('missing_stage_status_value',lambda r:r['projection']['stage'].update(dispatch=None)),
            ('contradictory_status',lambda r:r['dispatch'].update(status='completed')),
            ('contradictory_outcome',lambda r:r['projection'].update(outcome='succeeded')),
            ('malformed_status',lambda r:r['dispatch'].update(status={'bad':'status'})),
            ('foreign_run',lambda r:r['projection'].update(runId='other-run')),
            ('foreign_task',lambda r:r['projection'].update(taskId='other-task')),
            ('foreign_dispatch',lambda r:r['projection'].update(dispatchId='other-dispatch')),
            ('abandoned_unproven',lambda r:(r['projection'].update(outcome='abandoned'),
                                            r['projection']['stage'].update(dispatch='abandoned'),
                                            r['dispatch'].update(status='abandoned'))),
        ):
            with self.subTest(name=name):
                case=deepcopy(self.shown)
                change(case['result'])
                self.assertFalse(_native_assignment_settled(case))

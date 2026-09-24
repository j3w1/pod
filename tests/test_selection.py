from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from pod.config import load, set_mode, set_model, write_defaults
from pod.errors import PodError
from pod.selection import active_constraints, failure_active, validate_choice, validate_constraint, worker_ceiling
from tests.common import fixture


def choice(model='gpt-6-sol', agent='codex', effort='medium'):
    return {'agent':agent,'model':model,'effort':effort,'context':'native_default','reason':'bounded implementation'}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.path=self.root/'pod'/'config.yaml'; write_defaults(self.path)

    def test_one_three_zero_pools_and_preferred_does_not_rank(self):
        for model in list(load(personal=self.path)['eligible']):
            if model != 'gpt-6-sol':
                set_model(self.path,model,'disabled',displayed=load(personal=self.path))
        one=load(personal=self.path)
        self.assertTrue(validate_choice(one,[],[],choice())['allowed'])
        self.assertEqual(validate_choice(one,[],[],choice('gpt-6-astra'))['code'],'model_ineligible')
        for model in ('gpt-6-astra','claude-sonnet-5'):
            set_model(self.path,model,'available',displayed=load(personal=self.path))
        three=load(personal=self.path)
        self.assertEqual(len(three['eligible']),3)
        self.assertTrue(validate_choice(three,[],[],choice('claude-sonnet-5','claude'))['allowed'])
        for model in three['eligible']:
            set_model(self.path,model,'disabled',displayed=load(personal=self.path))
        self.assertEqual(validate_choice(load(personal=self.path),[],[],choice())['code'],'empty_pool')

    def test_effort_context_agent_and_reason_are_checked_without_ranking(self):
        snapshot=load(personal=self.path)
        for selected, expected in ((choice(effort='ultra'),'effort_unsupported'),
                                   ({**choice(),'context':'max'},'context_unsupported'),
                                   (choice(agent='claude'),'agent_mismatch'),
                                   ({**choice(),'reason':''},'reason_missing')):
            self.assertEqual(validate_choice(snapshot,[],[],selected)['code'],expected)
        self.assertTrue(validate_choice(snapshot,[],[],choice(effort='native_default'))['allowed'])

    def test_indirect_constraints_narrow_and_disabled_exception_lapses(self):
        snap=load(personal=self.path)
        constraint={'id':'c1','kind':'only_agents','provenance':'issue','value':['claude']}
        self.assertEqual(validate_choice(snap,[constraint],[],choice())['code'],'constraint_excluded')
        with self.assertRaises(PodError):
            validate_constraint({'kind':'allow_disabled','provenance':'issue','value':'gpt-6-luna'},snap)
        set_model(self.path,'gpt-6-luna','disabled',displayed=snap)
        disabled=load(personal=self.path)
        exception=validate_constraint({'id':'u1','kind':'allow_disabled','provenance':'user_direct','value':'gpt-6-luna'},disabled)
        self.assertTrue(validate_choice(disabled,[exception],[],choice('gpt-6-luna'))['allowed'])
        set_mode(self.path,'all',displayed=disabled)
        set_mode(self.path,'custom',displayed=load(personal=self.path))
        self.assertEqual(active_constraints([exception],load(personal=self.path)),[])
        set_model(self.path,'gpt-6-luna','available',displayed=disabled)
        self.assertEqual(active_constraints([exception],load(personal=self.path)),[])
        with self.assertRaises(PodError):
            validate_constraint({'kind':'max_workers','provenance':'worker','value':3},disabled)
        self.assertEqual(worker_ceiling(disabled,[{'kind':'max_workers','provenance':'user_direct','value':1}]),1)

    def test_failure_retry_after_and_safety_refusal(self):
        snap=load(personal=self.path); now=datetime(2026,9,24,tzinfo=timezone.utc)
        failure={'kind':'rate_limited','model':'gpt-6-sol','task':'task-1','retry_after':(now+timedelta(minutes=1)).isoformat(),'cleared_at':None}
        self.assertEqual(validate_choice(snap,[],[failure],choice(),task='task-2',now=now)['code'],'route_failed')
        self.assertFalse(failure_active(failure,now=now+timedelta(minutes=2)))
        safety={'kind':'safety_refusal','model':'gpt-6-sol','task':'task-1','retry_after':None,'cleared_at':None}
        self.assertEqual(validate_choice(snap,[],[safety],choice('gpt-6-luna'),task='task-1')['code'],'safety_refusal')

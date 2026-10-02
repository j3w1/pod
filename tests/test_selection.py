from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from pod.catalog import route_keys
from pod.config import load, set_preferred, set_route, set_routes, write_defaults
from pod.errors import PodError
from pod.selection import active_constraints, failure_active, validate_choice, validate_constraint, worker_ceiling
from tests.common import fixture, without_module

SOL = 'codex/gpt-6.1-sol/medium'


def choice(model='gpt-6.1-sol', agent='codex', effort='medium'):
    return {'agent':agent,'model':model,'effort':effort,'context':'native_default','reason':'bounded implementation'}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.path=self.root/'pod'/'config.yaml'; write_defaults(self.path)

    def only(self, *keys):
        set_routes(self.path, {key: (None if key not in keys else 'enabled') for key in route_keys()},
                   displayed=load(personal=self.path))

    def test_one_three_zero_route_pools_and_preferred_does_not_rank(self):
        self.only(SOL)
        one=load(personal=self.path)
        self.assertEqual(validate_choice(one,[],[],choice())['route'], SOL)
        self.assertEqual(validate_choice(one,[],[],choice(effort='high'))['code'],'route_ineligible')
        self.assertEqual(validate_choice(one,[],[],choice('gpt-6-astra'))['code'],'route_ineligible')
        self.only(SOL, 'codex/gpt-6-astra/high', 'claude/claude-sonnet-5-5/low')
        three=load(personal=self.path)
        self.assertEqual(len(three['eligible']),3)
        self.assertTrue(validate_choice(three,[],[],choice('claude-sonnet-5-5','claude','low'))['allowed'])
        set_preferred(self.path, 'codex/gpt-6-astra/high', displayed=three)
        preferred=load(personal=self.path)
        # Preferred is recorded, never a ranking: other enabled routes stay allowed.
        for selected in (choice(), choice('gpt-6-astra', effort='high'), choice('claude-sonnet-5-5','claude','low')):
            result=validate_choice(preferred,[],[],selected)
            self.assertTrue(result['allowed'])
            self.assertEqual(result['preferred_route'],'codex/gpt-6-astra/high')
        set_routes(self.path, {key: 'disabled' for key in preferred['eligible']}, displayed=preferred,
                   preferred=None)
        self.assertEqual(validate_choice(load(personal=self.path),[],[],choice())['code'],'empty_pool')

    def test_exact_effort_context_agent_and_reason_are_checked_without_ranking(self):
        snapshot=load(personal=self.path)
        for selected, expected in ((choice(effort='native_default'),'effort_required'),
                                   (choice(effort='ultra'),'effort_unsupported'),
                                   (choice(effort='none'),'effort_unsupported'),
                                   (choice(effort=None),'effort_unsupported'),
                                   (choice('gpt-6-sol'),'unsupported_model'),
                                   (choice('claude-sonnet-5','claude'),'unsupported_model'),
                                   (choice('gpt-daybreak-red-latest'),'unsupported_model'),
                                   ({**choice(),'context':'max'},'context_unsupported'),
                                   (choice(agent='claude'),'agent_mismatch'),
                                   ({**choice(),'reason':''},'reason_missing'),
                                   ({**choice(),'route':SOL},'invalid_choice')):
            with self.subTest(expected=expected, selected=selected):
                self.assertEqual(validate_choice(snapshot,[],[],selected)['code'],expected)
        allowed=validate_choice(snapshot,[],[],choice(effort='xhigh'))
        self.assertEqual((allowed['route'], allowed['effort'], allowed['pinned_route']),
                         ('codex/gpt-6.1-sol/xhigh','xhigh',None))

    def test_observations_and_their_absence_never_affect_selection(self):
        snapshot=load(personal=self.path)
        with without_module('pod.observations'), \
             patch('socket.socket.connect', side_effect=AssertionError('network')):
            self.assertTrue(validate_choice(snapshot,[],[],choice())['allowed'])
            self.assertTrue(validate_choice(snapshot,[],[],choice('gpt-6-luna', effort='max'))['allowed'])

    def test_setup_required_and_invalid_preferences_block_every_route(self):
        self.path.write_text('schema: pod/v1\nselection: all\nmodels: {}\nworkers: {max_active: 2}\n')
        for selected in (choice(), choice('claude-opus-5-5','claude','high')):
            self.assertEqual(validate_choice(load(personal=self.path),[],[],selected)['code'],'setup_required')
        self.path.write_text('schema: pod/v2\nroutes: [bad]\nworkers: {max_active: 2}\n')
        self.assertEqual(validate_choice(load(personal=self.path),[],[],choice())['code'],'preferences_unavailable')

    def test_indirect_constraints_narrow_and_disabled_route_exception_lapses(self):
        snap=load(personal=self.path)
        constraint={'id':'c1','kind':'only_agents','provenance':'issue','value':['claude']}
        self.assertEqual(validate_choice(snap,[constraint],[],choice())['code'],'constraint_excluded')
        luna='codex/gpt-6-luna/low'
        with self.assertRaises(PodError):
            validate_constraint({'kind':'allow_disabled','provenance':'issue','value':luna},snap)
        with self.assertRaises(PodError):
            validate_constraint({'kind':'allow_disabled','provenance':'user_direct','value':luna},snap)
        set_route(self.path,luna,'disabled',displayed=snap)
        disabled=load(personal=self.path)
        for value in ('gpt-6-luna', 'codex/gpt-6-luna/medium', 'codex/gpt-6-luna/ultra'):
            with self.subTest(value=value), self.assertRaises(PodError):
                validate_constraint({'kind':'allow_disabled','provenance':'user_direct','value':value},disabled)
        exception=validate_constraint({'id':'u1','kind':'allow_disabled','provenance':'user_direct','value':luna},disabled)
        self.assertEqual(exception['saved_state'],'disabled')
        self.assertTrue(validate_choice(disabled,[exception],[],choice('gpt-6-luna',effort='low'))['allowed'])
        # The exception is one exact route, not the model or another effort.
        self.assertEqual(validate_choice(disabled,[exception],[],choice('gpt-6-luna',effort='medium'))['allowed'], True)
        set_route(self.path,'codex/gpt-6-luna/medium','disabled',displayed=disabled)
        narrowed=load(personal=self.path)
        self.assertEqual(active_constraints([exception],narrowed),[])
        self.assertEqual(validate_choice(narrowed,[exception],[],choice('gpt-6-luna',effort='low'))['code'],'route_ineligible')
        with self.assertRaises(PodError):
            validate_constraint({'kind':'max_workers','provenance':'worker','value':3},disabled)
        self.assertEqual(worker_ceiling(disabled,[{'kind':'max_workers','provenance':'user_direct','value':1}]),1)

    def test_earlier_constraint_rows_stay_readable_and_inert(self):
        snapshot=load(personal=self.path)
        older=[{'id':'o1','kind':'allow_disabled','provenance':'user_direct','value':'gpt-6-luna',
                'saved_state':'disabled','mode':'custom','file_stamp':snapshot['file_stamp']},
               {'id':'o2','kind':'exclude_models','provenance':'user_direct','value':['gpt-6-sol','claude-sonnet-5']},
               {'id':'o3','kind':'role_model','provenance':'repository','role':'review','value':'gpt-6-sol'}]
        for row in older:
            self.assertEqual(validate_constraint(row), row)
        active=active_constraints(older,snapshot)
        self.assertEqual([row['id'] for row in active],['o2','o3'])
        self.assertTrue(validate_choice(snapshot,older,[],choice())['allowed'])
        # A repository role rule naming a replaced model narrows that role, conservatively.
        self.assertEqual(validate_choice(snapshot,older,[],choice(),role='review')['code'],'constraint_excluded')

    def test_failure_retry_after_and_safety_refusal(self):
        snap=load(personal=self.path); now=datetime(2026,9,24,tzinfo=timezone.utc)
        failure={'kind':'rate_limited','model':'gpt-6.1-sol','task':'task-1','retry_after':(now+timedelta(minutes=1)).isoformat(),'cleared_at':None}
        # Failure suppression stays keyed by model, across efforts.
        for effort in ('medium','xhigh'):
            self.assertEqual(validate_choice(snap,[],[failure],choice(effort=effort),task='task-2',now=now)['code'],'route_failed')
        self.assertFalse(failure_active(failure,now=now+timedelta(minutes=2)))
        safety={'kind':'safety_refusal','model':'gpt-6.1-sol','task':'task-1','retry_after':None,'cleared_at':None}
        self.assertEqual(validate_choice(snap,[],[safety],choice('gpt-6-luna'),task='task-1')['code'],'safety_refusal')
        self.assertTrue(validate_choice(snap,[],[safety],choice('gpt-6-luna'),task='task-2')['allowed'])

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import load as load_config, set_model, write_defaults
from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import (admission_identity, checkpoint, constraints_update, read,
                        route_failure, update_admission)
from pod.operations import (_assignment_evidence, guarded_start, recover_admission, OrcaPort)
from pod.records import packet, source_identity
from tests.common import fixture

REQUEST='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'
ROUTE={'agent':'codex','model':'gpt-6-sol','effort':'medium','context':'native_default',
       'reason':'bounded implementation with tests'}


class FakePort:
    def __init__(self):
        self.runtime='runtime'; self.starts=[]; self.workers={}; self.receipt=None
        self.state='completed'; self.request_id=REQUEST; self.request_receipt=None
        self.find_rows=None; self.show_failure=None; self.effective=None
        self.before_native=None; self.placement=None; self.capable=True
        self.headless=False

    def capability(self):
        if not self.capable:
            raise PodError('launch_preferences_unavailable','no launch preference support')
        return {'status':'observed','runtime':self.runtime,
                'capabilities':{'launch_preferences_v1':True}}

    def resolve_worktree(self, selector):
        if self.placement is None:
            raise PodError('worktree_resolution_unavailable','no placement fixture')
        return dict(self.placement)

    def read_native(self, owner, *, authority_runs=(), assignments=()):
        if self.before_native:
            callback=self.before_native; self.before_native=None; callback()
        evidence=[]
        for row in assignments:
            binding=row['native_binding']
            if binding['dispatchId'] in self.workers:
                evidence.append(_assignment_evidence(self.show_worker(binding['dispatchId']),row))
        return {'runtime':self.runtime,'owner':owner if authority_runs else None,
                'authoritative':bool(authority_runs),'scope':'objective_assignments',
                'complete':True,'assignments':evidence,'physical_capacity':'unavailable'}

    def start_worker(self, *, run, task, owner, route, worktree='current', retry_request=None, terminal=None):
        self.starts.append({'run':run,'task':task,'route':route.copy(),'worktree':worktree,
                            'retry_request':retry_request,'terminal':terminal})
        if self.receipt is not None:
            return dict(self.receipt)
        dispatch='dispatch-'+str(len(self.workers)+1)
        self.workers[dispatch]={'run':run,'task':task,'route':route.copy(),'worktree':worktree,
                                'state':'ready','outcome':'in_progress',
                                'terminal':None if self.headless else terminal or 'term-'+dispatch}
        return {'runtime':self.runtime,'request_uuid':retry_request or REQUEST,
                'runId':run,'taskId':task,'dispatchId':dispatch,'state':'ready',
                'launch':{'requested':self._launch(route),'effective':self._launch(route)}}

    @staticmethod
    def _launch(route):
        data={key:route[key] for key in ('agent','model')}
        if route['effort']!='native_default': data['effort']=route['effort']
        return data

    def show_worker(self, dispatch):
        if self.show_failure: raise self.show_failure
        item=self.workers[dispatch]
        effective=self.effective if self.effective is not None else self._launch(item['route'])
        return {'runtime':self.runtime,'result':{
            'dispatch':{'id':dispatch,'runId':item['run'],'taskId':item['task']},
            'projection':{'id':'worker-'+dispatch,'dispatchId':dispatch,'runId':item['run'],
                          'taskId':item['task'],'outcome':item['outcome'],
                          'stage':{'detail':'settled' if item['outcome']!='in_progress' else 'input_accepted'}},
            'worker':{'dispatchId':dispatch,'worktreeId':item['worktree'],'state':item['state'],
                      'agentTerminalHandle':item['terminal'],
                      'startOptions':{'launch':{'requested':self._launch(item['route']),
                                                'effective':effective}}}}}

    def request_show(self, request_uuid):
        receipt=self.request_receipt
        if receipt is None and self.workers:
            dispatch,item=next(iter(self.workers.items()))
            receipt={'runId':item['run'],'taskId':item['task'],'dispatchId':dispatch,
                     'launch':{'requested':self._launch(item['route']),
                               'effective':self._launch(item['route'])}}
        result={'requestId':self.request_id,'state':self.state,'receipt':receipt}
        if self.state!='absent': result['method']='orchestration.workerStart'
        return {'runtime':self.runtime,'result':result}

    def find_worker(self, *, run, task):
        if self.find_rows is not None: return list(self.find_rows)
        return [{'dispatchId':dispatch} for dispatch,item in self.workers.items()
                if item['run']==run and item['task']==task]


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.env=patch.dict(os.environ,{'XDG_STATE_HOME':str(self.root/'state'),
                                     'XDG_CONFIG_HOME':str(self.root/'config')})
        self.env.__enter__(); self.addCleanup(self.env.__exit__,None,None,None)
        self.project=self.root/'project'; self.project.mkdir()
        self.personal=self.root/'config'/'pod'/'config.yaml'; write_defaults(self.personal)
        self.port=FakePort()
        self.port.placement={'repository':None,'repo_key':None,'path':str(self.project.resolve()),
                             'branch':None,'runtime':'runtime'}
        self.checkpoint()

    def checkpoint(self):
        from pod.config import effective
        value={'schema':'pod-checkpoint/v2','criteria':['works'],'plan_revision':'plan',
               'candidate':'candidate','policy_revision':effective(self.project)['revision'],
               'native_refs':[],'assignments':[],'questions':[],
               'verification_gaps':['works'],'next_safe_action':'inspect'}
        return checkpoint(self.project,'objective',owner='owner',value=value,native={'runtime':'runtime'})

    def frozen(self,route=None,sources=None,context=None):
        pref=load_config(self.project)
        return packet({'schema':'pod-packet/v2','objective':'objective','criteria':['works'],
                       'responsibility':'writer','scope':['notes.txt'],'actions':['edit'],
                       'candidate':'candidate','context':context or [],'dependencies':[],
                       'route':{**(route or ROUTE),'preference_revision':pref['revision']},
                       'policy_revision':pref['policy_revision'],
                       'plan_revision':'plan','report_contract':'checks','sources':sources or []})

    def start(self,task='task',frozen=None,reuse_of=None):
        return guarded_start(self.project,'objective',owner='owner',run='run',task=task,
                             plan_revision='plan',frozen_packet=frozen or self.frozen(),
                             worktree='current',port=self.port,reuse_of=reuse_of)

    def test_exact_start_binds_decision_and_native_effect(self):
        result=self.start()
        self.assertEqual(result['status'],'bound')
        row=result['admission']
        self.assertEqual(row['route_decision']['model'],'gpt-6-sol')
        self.assertEqual(row['route_decision']['effective']['model'],'gpt-6-sol')
        self.assertEqual(row['route_decision']['requested_context'],'native_default')
        self.assertFalse(row['route_decision']['route_mismatch'])
        self.assertEqual(len(self.port.starts),1)
        self.assertEqual(self.start()['status'],'bound')
        self.assertEqual(len(self.port.starts),1)

    def test_before_boundary_edit_refuses_with_no_admission_and_after_row_keeps_start(self):
        frozen=self.frozen()
        set_model(self.personal,'gpt-6-sol','disabled',displayed=load_config(self.project))
        with self.assertRaises(PodError) as caught:
            self.start(frozen=frozen)
        self.assertEqual(caught.exception.code,'preference_changed')
        self.assertFalse(read(self.project,'objective')['admissions'])
        set_model(self.personal,'gpt-6-sol','available',displayed=load_config(self.project))
        frozen=self.frozen()
        self.port.before_native=lambda:set_model(self.personal,'gpt-6-sol','disabled',
                                                  displayed=load_config(self.project))
        with self.assertRaises(PodError): self.start(frozen=frozen)
        self.assertFalse(read(self.project,'objective')['admissions'])
        set_model(self.personal,'gpt-6-sol','available',displayed=load_config(self.project))
        result=self.start(frozen=self.frozen())
        set_model(self.personal,'gpt-6-sol','disabled',displayed=load_config(self.project))
        self.assertEqual(read(self.project,'objective')['admissions'][result['admission']['admission_id']]['state'],'bound')

    def test_missing_and_partial_preferences_disable_new_workers(self):
        self.personal.unlink()
        with self.assertRaises(PodError): self.start(frozen=self.frozen())
        self.assertEqual(self.port.starts,[])
        self.personal.write_text('schema: pod/v1\nselection: all\nmodels: {gpt-6-sol: available}\n')
        with self.assertRaises(PodError): self.start(frozen=self.frozen())
        self.assertEqual(self.port.starts,[])

    def test_logical_ceiling_and_exact_settlement(self):
        first=self.start('task1')['admission']; second=self.start('task2')['admission']
        with self.assertRaises(PodError) as caught: self.start('task3')
        self.assertEqual(caught.exception.code,'logical_capacity_full')
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        self.assertEqual(self.start('task3')['status'],'bound')
        self.assertEqual(len(self.port.starts),3)

    def test_personal_ceiling_accepts_zero_and_eight_without_extra_grants(self):
        import yaml
        document=yaml.safe_load(self.personal.read_text())
        document['workers']['max_active']=0
        self.personal.write_text(yaml.safe_dump(document,sort_keys=False))
        with self.assertRaises(PodError) as zero: self.start()
        self.assertEqual(zero.exception.code,'logical_capacity_full')
        document['workers']['max_active']=8
        self.personal.write_text(yaml.safe_dump(document,sort_keys=False))
        for number in range(8):
            self.assertEqual(self.start(f'task-{number}')['status'],'bound')
        with self.assertRaises(PodError) as ninth: self.start('task-8')
        self.assertEqual(ninth.exception.code,'logical_capacity_full')

    def test_concurrent_admissions_serialize_the_objective_ceiling(self):
        from concurrent.futures import ThreadPoolExecutor
        import yaml
        document=yaml.safe_load(self.personal.read_text())
        document['workers']['max_active']=1
        self.personal.write_text(yaml.safe_dump(document,sort_keys=False))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.start,f'task-{number}') for number in (1,2)]
            outcomes=[]
            for future in futures:
                try: outcomes.append(future.result()['status'])
                except PodError as exc: outcomes.append(exc.code)
        self.assertEqual(sorted(outcomes),['bound','logical_capacity_full'])
        self.assertEqual(len(self.port.starts),1)

    def test_pending_replay_keeps_original_route_across_preference_and_version_edit(self):
        first=self.start()['admission']
        def unresolved(row): row.update(state='unresolved',native_binding=None)
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],update=unresolved)
        self.port.state='pending'
        set_model(self.personal,'gpt-6-sol','disabled',displayed=load_config(self.project))
        from pod import __version__
        with patch('pod.__version__','different'):
            result=recover_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                                     worktree='current',port=self.port)
        self.assertEqual(result['status'],'bound')
        self.assertEqual(self.port.starts[-1]['retry_request'],REQUEST)
        self.assertEqual(len(self.port.starts),2)

    def test_completed_and_absent_recovery_do_not_launch_replacement(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.state='completed'
        self.assertEqual(recover_admission(self.project,'objective',owner='owner',
                         admission_id=first['admission_id'],worktree='current',port=self.port)['status'],'bound')
        self.assertEqual(len(self.port.starts),1)
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.state='absent'
        self.assertEqual(recover_admission(self.project,'objective',owner='owner',
                         admission_id=first['admission_id'],worktree='current',port=self.port)['status'],'bound')
        self.assertEqual(len(self.port.starts),1)

    def test_invalid_uuid_and_ambiguous_attempt_hold(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None,request_uuid='bad'))
        self.assertEqual(recover_admission(self.project,'objective',owner='owner',
                         admission_id=first['admission_id'],worktree='current',port=self.port)['action'],'hold')
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None,request_uuid=REQUEST))
        self.port.state='absent'; self.port.find_rows=[{'dispatchId':'dispatch-1'}, {'dispatchId':'dispatch-1'}]
        self.assertEqual(recover_admission(self.project,'objective',owner='owner',
                         admission_id=first['admission_id'],worktree='current',port=self.port)['status'],'unresolved')

    def test_effective_unknown_and_mismatch_bind_without_acceptance(self):
        self.port.effective={'agent':'codex','model':'gpt-6-luna','effort':'medium'}
        row=self.start()['admission']
        self.assertTrue(row['route_decision']['route_mismatch'])
        self.assertEqual(row['error']['code'],'route_mismatch')
        self.port.effective={}
        second=self.start('task2')['admission']
        self.assertEqual(second['route_decision']['effective']['model'],'unknown')
        self.assertTrue(second['route_decision']['effective_unknown'])

    def test_status_exposes_route_mismatch_and_version_drift(self):
        from pod.cli import execute, parser
        self.port.effective={'agent':'codex','model':'gpt-6-luna','effort':'medium'}
        self.start()
        with patch('pod.cli.current_run',return_value={'runtime':'runtime','run':{'id':'run'}}), \
             patch('pod.cli.worker_rows',return_value={'runtime':'runtime','scope':'run',
                                                       'workers':[],'complete':True}):
            status=execute(parser().parse_args(['status','--json']),self.project)
        self.assertEqual(status['blocker'],'route_mismatch')
        self.assertTrue(status['route_mismatch'])
        with patch('pod.__version__','different'), \
             patch('pod.cli.current_run',return_value={'runtime':'runtime','run':{'id':'run'}}), \
             patch('pod.cli.worker_rows',return_value={'runtime':'runtime','scope':'run',
                                                       'workers':[],'complete':True}):
            drift=execute(parser().parse_args(['status','--json']),self.project)
        self.assertEqual(drift['blocker'],'installed_version_changed')

    def test_reuse_requires_settlement_and_exact_effective_model(self):
        first=self.start()['admission']; identity=first['admission_id']
        with self.assertRaises(PodError): self.start('task2',reuse_of=identity)
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        second=self.start('task2',reuse_of=identity)['admission']
        self.assertEqual(self.port.starts[-1]['terminal'],first['native_binding']['terminalHandle'])
        self.assertEqual(second['reuse_of'],identity)

    def test_constraints_and_failures_are_objective_local(self):
        constraints_update(self.project,'objective',owner='owner',action='add',
                           value={'id':'claude-only','kind':'only_agents','provenance':'user_direct','value':['claude']})
        with self.assertRaises(PodError): self.start()
        constraints_update(self.project,'objective',owner='owner',action='revoke',constraint_id='claude-only')
        first=self.start()['admission']
        route_failure(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                      kind='rate_limited',source='native',retry_after='2099-01-01T00:00:00Z')
        with self.assertRaises(PodError): self.start('task2')
        route_failure(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                      kind='rate_limited',source='native',clear=True,cleared_by='runtime_change')
        self.assertEqual(self.start('task2')['status'],'bound')

    def test_source_and_instruction_changes_reject_before_start(self):
        file=self.project/'notes.txt'; file.write_text('original')
        source=source_identity(self.project,'notes.txt')
        frozen=self.frozen(sources=[source])
        file.write_text('changed')
        with self.assertRaises(PodError): self.start(frozen=frozen)
        self.assertEqual(self.port.starts,[])
        self.assertTrue(read(self.project,'objective')['source_rejections'])

    def test_version_drift_blocks_new_start_but_read_only_recovery_survives(self):
        with patch('pod.__version__','different'):
            with self.assertRaises(PodError) as caught: self.start()
            self.assertEqual(caught.exception.code,'installed_version_changed')
        self.assertEqual(self.start()['status'],'bound')

    def test_internal_admission_cannot_accept_caller_asserted_capability(self):
        request={'project':str(self.project),'objective':'objective','owner':'owner',
                 'run':'run','task':'task','plan_revision':'plan','packet':self.frozen(),
                 'capabilities':{'launch_preferences_v1':True}}
        with self.assertRaises(PodError) as caught: internal_run('admission',request)
        self.assertEqual(caught.exception.code,'invalid_request')
        for removed in ('preview','replay'):
            with self.subTest(removed=removed), self.assertRaises(PodError) as gone:
                internal_run(removed,{})
            self.assertEqual(gone.exception.code,'unknown_operation')

    def test_documented_refusal_defers_but_uncertain_failure_holds(self):
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':None,
                           'error':{'code':'task_not_startable','message':'unmet dependency'},
                           'state':'refused'}
        refused=self.start()['admission']
        self.assertEqual(refused['state'],'deferred')
        self.assertIsNone(refused['native_binding'])
        self.assertEqual(self.start()['status'],'deferred')
        self.assertEqual(len(self.port.starts),1)
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
                           'error':{'code':'runtime_error','message':'uncertain'}}
        held=self.start('other')['admission']
        self.assertEqual(held['state'],'unresolved')
        self.assertEqual(held['request_uuid'],REQUEST)

    def test_request_contradiction_holds_without_uuid_substitution(self):
        self.port.receipt={'runtime':'runtime','exit':0,'request_uuid':REQUEST,
                           'mutation':{'requestId':OTHER},'runId':'run','taskId':'task',
                           'dispatchId':'dispatch-1','state':'ready'}
        held=self.start()['admission']
        self.assertEqual(held['state'],'unresolved')
        self.assertEqual(held['error']['code'],'native_request_conflict')
        self.assertEqual(len(self.port.starts),1)

    def test_lost_start_response_without_uuid_holds_instead_of_restarting(self):
        with patch.object(self.port,'start_worker',side_effect=PodError('native_effect_uncertain','lost')) as start:
            with self.assertRaises(PodError): self.start()
        self.assertEqual(start.call_count,1)
        admission=next(iter(read(self.project,'objective')['admissions'].values()))
        self.assertEqual(admission['state'],'unresolved')
        self.assertIsNone(admission['request_uuid'])
        self.assertEqual(self.start()['action'],'hold')
        self.assertEqual(len(self.port.starts),0)

    def test_alternative_after_failure_requires_native_settlement(self):
        first=self.start()['admission']
        route_failure(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                      kind='unavailable',source='native')
        alternate={**ROUTE,'model':'gpt-6-luna','reason':'available alternative'}
        with self.assertRaises(PodError) as caught:
            self.start('task2',frozen=self.frozen(route=alternate))
        self.assertEqual(caught.exception.code,'failed_attempt_unsettled')
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        self.assertEqual(self.start('task2',frozen=self.frozen(route=alternate))['status'],'bound')

    def test_safety_refusal_bars_same_task_on_another_run(self):
        first=self.start()['admission']
        route_failure(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                      kind='safety_refusal',source='native')
        alternate={**ROUTE,'model':'gpt-6-luna'}
        with self.assertRaises(PodError) as caught:
            guarded_start(self.project,'objective',owner='owner',run='run2',task='task',
                          plan_revision='plan',frozen_packet=self.frozen(route=alternate),
                          worktree='current',port=self.port)
        self.assertEqual(caught.exception.code,'safety_refusal')

    def test_pending_replay_rechecks_governor_policy_and_checkpoint_core(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.state='pending'
        local=self.project/'.pod'/'config.yaml'; local.parent.mkdir()
        local.write_text('schema: pod/v1\nwaste_governor: {preflight: [unit]}\n')
        with self.assertRaises(PodError) as caught:
            recover_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                              worktree='current',port=self.port)
        self.assertEqual(caught.exception.code,'policy_revision_mismatch')
        self.assertEqual(len(self.port.starts),1)

    def test_rebuilt_packet_cannot_replace_an_uncertain_same_task_attempt(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        changed=self.frozen(route={**ROUTE,'reason':'a revised packet'})
        with self.assertRaises(PodError) as caught: self.start(frozen=changed)
        self.assertEqual(caught.exception.code,'unresolved_prior_attempt')
        self.assertEqual(len(self.port.starts),1)

    def test_pending_replay_rejects_changed_worktree_and_request_method(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.state='pending'
        with self.assertRaises(PodError) as moved:
            recover_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                              worktree='name:other',port=self.port)
        self.assertEqual(moved.exception.code,'admission_conflict')
        original=self.port.request_show
        def wrong_method(request_uuid):
            shown=original(request_uuid)
            shown['result']['method']='otherMutation'
            return shown
        with patch.object(self.port,'request_show',side_effect=wrong_method):
            with self.assertRaises(PodError) as wrong:
                recover_admission(self.project,'objective',owner='owner',
                                  admission_id=first['admission_id'],worktree='current',port=self.port)
        self.assertEqual(wrong.exception.code,'native_request_mismatch')
        self.assertEqual(len(self.port.starts),1)

    def test_headless_worker_binds_and_readback_failure_keeps_uuid(self):
        self.port.headless=True
        first=self.start()['admission']
        self.assertIsNone(first['native_binding']['terminalHandle'])
        shown=self.port.show_worker
        def unavailable(dispatch):
            if dispatch=='dispatch-2':
                raise PodError('orca_read_failed','worker-show unavailable')
            return shown(dispatch)
        with patch.object(self.port,'show_worker',side_effect=unavailable):
            with self.assertRaises(PodError): self.start('task2')
        rows=read(self.project,'objective')['admissions']
        second=next(row for row in rows.values() if row['task_id']=='task2')
        self.assertEqual(second['state'],'unresolved')
        self.assertEqual(second['request_uuid'],REQUEST)
        self.assertIsNone(second['native_binding'])

    def test_worker_identity_contradiction_cannot_bind(self):
        shown=self.port.show_worker
        def contradictory(dispatch):
            result=shown(dispatch)
            result['result']['projection']['taskId']='other'
            return result
        with patch.object(self.port,'show_worker',side_effect=contradictory):
            with self.assertRaises(PodError) as caught: self.start()
        self.assertEqual(caught.exception.code,'native_identity_unverified')
        row=next(iter(read(self.project,'objective')['admissions'].values()))
        self.assertEqual(row['state'],'unresolved')
        self.assertEqual(len(self.port.starts),1)

    def test_missing_launch_capability_and_wrong_placement_stop_before_start(self):
        self.port.capable=False
        with self.assertRaises(PodError) as caught: self.start()
        self.assertEqual(caught.exception.code,'launch_preferences_unavailable')
        self.assertFalse(read(self.project,'objective')['admissions'])
        self.port.capable=True
        placement={'repository':None,'repo_key':'a'*64,'path':str(self.project),
                   'branch':None}
        frozen=packet({**self.frozen()['body'],'placement':placement})
        self.port.placement={**placement,'runtime':'runtime','path':str(self.root/'other')}
        with self.assertRaises(PodError) as caught: self.start(frozen=frozen)
        self.assertEqual(caught.exception.code,'worktree_binding_changed')
        self.assertEqual(self.port.starts,[])

    def test_private_constraint_and_failure_updates_never_write_preferences(self):
        before=self.personal.read_bytes()
        internal_run('constraint',{'project':str(self.project),'objective':'objective',
                                   'owner':'owner','action':'add','value':{
                                       'id':'one','kind':'max_workers','provenance':'user_direct','value':1}})
        self.assertEqual(self.personal.read_bytes(),before)
        first=self.start()['admission']
        internal_run('route-failure',{'project':str(self.project),'objective':'objective',
                                      'owner':'owner','admission_id':first['admission_id'],
                                      'kind':'unavailable','source':'native'})
        self.assertEqual(self.personal.read_bytes(),before)

    def test_fresh_report_read_detects_later_effective_route_change(self):
        frozen=self.frozen()
        first=self.start(frozen=frozen)['admission']
        binding=first['native_binding']
        report={'schema':'pod-report/v1','assignment':frozen['packet_id'],
                'attempt':binding['dispatchId'],'candidate':'candidate','outcome':'succeeded',
                'scope':['notes.txt'],'files':[],'checks':['unit passed'],'failures':[],
                'evidence':[],'uncertainty':[],'questions':[]}
        self.port.effective={'agent':'codex','model':'gpt-6-luna','effort':'medium'}
        with patch.object(OrcaPort,'show_worker',autospec=True,
                          side_effect=lambda _port,dispatch:self.port.show_worker(dispatch)):
            with self.assertRaises(PodError) as caught:
                internal_run('report',{'project':str(self.project),'objective':'objective',
                                       'admission_id':first['admission_id'],
                                       'packet':frozen,'report':report})
        self.assertEqual(caught.exception.code,'route_mismatch')
        stored=read(self.project,'objective')['admissions'][first['admission_id']]
        self.assertTrue(stored['route_decision']['route_mismatch'])

    def test_preference_change_before_alternative_requires_new_packet_revision(self):
        first=self.start()['admission']
        route_failure(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                      kind='unavailable',source='native')
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        alternative={**ROUTE,'model':'gpt-6-luna','reason':'failed original route'}
        frozen=self.frozen(route=alternative)
        set_model(self.personal,'gpt-6-luna','disabled',displayed=load_config(self.project))
        with self.assertRaises(PodError) as caught: self.start('task2',frozen=frozen)
        self.assertEqual(caught.exception.code,'preference_changed')
        with self.assertRaises(PodError) as caught: self.start('task2',frozen=self.frozen(route=alternative))
        self.assertEqual(caught.exception.code,'preference_changed')
        self.assertEqual(len(self.port.starts),1)

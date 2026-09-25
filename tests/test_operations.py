from copy import deepcopy
from datetime import datetime, timezone
import base64
import json
import os
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import load as load_config, set_model, write_defaults
from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import (admission_identity, checkpoint, constraints_update, objective_root, read,
                        route_failure, update_admission)
from pod.operations import (_assignment_evidence, _preflight_refusal_classification,
                            guarded_start, recover_admission, OrcaPort)
from pod.records import packet, source_identity
from tests.common import (fake_authority, fixture, kernel_binding, kernel_map, modified_bundle, receipt,
                          restated_map, stored_map)

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
        status=('completed' if item['outcome']=='succeeded' else
                'failed' if item['outcome'] in ('failed','stopped') else 'running')
        return {'runtime':self.runtime,'result':{
            'dispatch':{'id':dispatch,'runId':item['run'],'taskId':item['task'],'status':status},
            'projection':{'id':'worker-'+dispatch,'dispatchId':dispatch,'runId':item['run'],
                          'taskId':item['task'],'outcome':item['outcome'],
                          'stage':{'dispatch':status,
                                   'detail':'settled' if item['outcome']!='in_progress' else 'input_accepted'}},
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
        authority=patch('pod.ledger.require_authority',
                        side_effect=lambda *args,**kwargs:fake_authority(self.port)(*args,**kwargs))
        authority.start(); self.addCleanup(authority.stop)
        self.project=self.root/'project'; self.project.mkdir()
        self.personal=self.root/'config'/'pod'/'config.yaml'; write_defaults(self.personal)
        self.port=FakePort()
        self.port.placement={'repository':None,'repo_key':None,'path':str(self.project.resolve()),
                             'branch':None,'runtime':'runtime'}
        self.checkpoint()

    def checkpoint(self):
        from pod.config import effective
        state=read(self.project,'objective')
        mapped=stored_map(state) if state and state.get('checkpoint') else kernel_map(['works'])
        value={'schema':'pod-checkpoint/v3','criteria':['works'],'plan_revision':'plan',
               'candidate':'candidate','policy_revision':effective(self.project)['revision'],
               'native_refs':[],'assignments':[],'questions':[],
               'verification_gaps':['works'],'next_safe_action':'inspect',**mapped}
        return checkpoint(self.project,'objective',owner='owner',value=value,native={'runtime':'runtime'})

    def frozen(self,route=None,sources=None,context=None,task='task'):
        pref=load_config(self.project)
        return packet({'schema':'pod-packet/v3','objective':'objective','criteria':['works'],
                       'responsibility':'writer','scope':['notes.txt'],'actions':['edit'],
                       'candidate':'candidate','context':context or [],'dependencies':[],
                       'route':{**(route or ROUTE),'preference_revision':pref['revision']},
                       'policy_revision':pref['policy_revision'],
                       'plan_revision':'plan','report_contract':'checks','sources':sources or [],
                       **kernel_binding(task)})

    def restated(self,objective='objective'):
        return restated_map(self.project,objective,self.port)

    def discard(self,admission,reason='a fresh attempt replaces this settled result'):
        """0.6: a settled result takes a disposition before its replacement serves the obligation."""
        state=read(self.project,'objective')
        core={key:value for key,value in state['checkpoint'].items()
              if key not in ('pod_version','bundle_digest','seq')}
        value={**core,**(self.restated() or stored_map(state)),
               'dispositions':[{'admission':admission['admission_id'],'discarded':True,'reason':reason}]}
        return checkpoint(self.project,'objective',owner='owner',value=value,native={'runtime':'runtime'})

    def start(self,task='task',frozen=None,reuse_of=None):
        return guarded_start(self.project,'objective',owner='owner',run='run',task=task,
                             plan_revision='plan',frozen_packet=frozen or self.frozen(task=task),
                             worktree='current',port=self.port,reuse_of=reuse_of,
                             accompanying=self.restated())

    def recovery_case(self):
        first=self.start()['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.starts.clear()
        return first

    def recover(self, admission):
        return recover_admission(self.project,'objective',owner='owner',
                                 admission_id=admission['admission_id'],worktree='current',port=self.port)

    def record_failure(self, admission, *, kind, source='native', **kwargs):
        return route_failure(self.project,'objective',owner='owner',
                             admission_id=admission['admission_id'],kind=kind,source=source,
                             native_reader=lambda row:self.port.read_native(
                                 'owner',authority_runs=(row['run_id'],),assignments=(row,)),
                             **kwargs)

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

    def test_missing_benchmark_row_cannot_block_admission(self):
        from pod.catalog import load as load_catalog
        document=deepcopy(load_catalog())
        del document['reference_benchmark']['models']['gpt-6-luna']
        with patch('pod.catalog.load',return_value=document):
            result=self.start()
        self.assertEqual(result['status'],'bound')
        self.assertEqual(result['admission']['route_decision']['model'],'gpt-6-sol')
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

    def test_boundary_distinguishes_stale_revision_from_current_ineligibility(self):
        set_model(self.personal,'gpt-6-sol','disabled',displayed=load_config(self.project))
        disabled_packet=self.frozen(task='disabled-now')
        with self.assertRaises(PodError) as disabled:
            self.start(task='disabled-now',frozen=disabled_packet)
        self.assertEqual(disabled.exception.code,'model_ineligible')
        self.assertFalse(read(self.project,'objective')['admissions'])
        set_model(self.personal,'gpt-6-sol','available',displayed=load_config(self.project))
        permitted_packet=self.frozen(task='other-edit')
        set_model(self.personal,'gpt-6-luna','disabled',displayed=load_config(self.project))
        with self.assertRaises(PodError) as stale:
            self.start(task='other-edit',frozen=permitted_packet)
        self.assertEqual(stale.exception.code,'preference_revision_stale')
        self.assertFalse(read(self.project,'objective')['admissions'])
        unsupported=self.frozen(route={**ROUTE,'effort':'ultra'},task='bad-effort')
        with self.assertRaises(PodError) as effort:
            self.start(task='bad-effort',frozen=unsupported)
        self.assertEqual(effort.exception.code,'effort_unsupported')
        self.assertFalse(read(self.project,'objective')['admissions'])

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
        self.discard(first,reason='the first result is set aside before another implementation')
        self.assertEqual(self.start('task3')['status'],'bound')
        self.assertEqual(len(self.port.starts),3)

    def test_stopped_native_fixture_frees_one_slot_for_replacement_task(self):
        import yaml
        from pod.ledger import logical_projection
        document=yaml.safe_load(self.personal.read_text())
        document['workers']['max_active']=1
        self.personal.write_text(yaml.safe_dump(document,sort_keys=False))
        sonnet={**ROUTE,'agent':'claude','model':'claude-sonnet-5'}
        first=self.start(frozen=self.frozen(route=sonnet))['admission']
        with self.assertRaises(PodError) as full:
            self.start('replacement-task',frozen=self.frozen(route=sonnet,task='replacement-task'))
        self.assertEqual(full.exception.code,'logical_capacity_full')
        with self.assertRaises(PodError) as still_running:
            self.record_failure(first,kind='unavailable')
        self.assertEqual(still_running.exception.code,'failure_attempt_unsettled')
        stopped=receipt('stopped-worker-show')
        proven=deepcopy(stopped)
        original=self.port.show_worker
        def show(dispatch):
            return {'runtime':'runtime','result':deepcopy(stopped['result'])} if dispatch==first['native_binding']['dispatchId'] else original(dispatch)
        with patch.object(self.port,'show_worker',side_effect=show):
            stopped['result']['dispatch'].pop('status')
            with self.assertRaises(PodError) as stage_only:
                self.start('replacement-task',frozen=self.frozen(route=ROUTE,task='replacement-task'))
            self.assertEqual(stage_only.exception.code,'logical_capacity_full')
            stopped['result']['projection']['stage'].pop('dispatch')
            with self.assertRaises(PodError) as unknown:
                self.start('replacement-task',frozen=self.frozen(route=ROUTE,task='replacement-task'))
            self.assertEqual(unknown.exception.code,'logical_capacity_full')
            stopped=deepcopy(proven)
            stopped['result']['dispatch']['status']='abandoned'
            stopped['result']['projection']['stage']['dispatch']='abandoned'
            stopped['result']['projection']['outcome']='abandoned'
            with self.assertRaises(PodError) as abandoned:
                self.start('replacement-task',frozen=self.frozen(route=ROUTE,task='replacement-task'))
            self.assertEqual(abandoned.exception.code,'logical_capacity_full')
            stopped=proven
            native=self.port.read_native('owner',authority_runs=('run',),assignments=(first,))
            self.assertTrue(native['assignments'][0]['settled'])
            self.assertEqual(logical_projection(self.project,native,objective='objective')['outstanding'],[])
            self.record_failure(first,kind='unavailable')
            self.discard(first,reason='the stopped attempt has no implementation result to integrate')
            replacement=self.start('replacement-task',frozen=self.frozen(route=ROUTE,task='replacement-task'))
        self.assertEqual(replacement['status'],'bound')
        self.assertEqual(len(self.port.starts),2)

    def test_stopped_native_fixture_allows_same_task_on_another_objective_run(self):
        from pod.ledger import _lock, _path, _read, _write
        first=self.start('shared-task')['admission']
        state_path=_path(self.project,'objective')
        with _lock(state_path):
            state=_read(state_path)
            state['checkpoint']['native_refs'].append({'runId':'another-run','runtime':'runtime'})
            _write(state_path,state)
        stopped=receipt('stopped-worker-show')
        stopped['result']['dispatch']['taskId']='shared-task'
        stopped['result']['projection']['taskId']='shared-task'
        original=self.port.show_worker
        def show(dispatch):
            return {'runtime':'runtime','result':deepcopy(stopped['result'])} if dispatch==first['native_binding']['dispatchId'] else original(dispatch)
        with patch.object(self.port,'show_worker',side_effect=show):
            stopped['result']['dispatch'].pop('status')
            with self.assertRaises(PodError) as unverified:
                guarded_start(self.project,'objective',owner='owner',run='another-run',
                              task='shared-task',plan_revision='plan',
                              frozen_packet=self.frozen(task='shared-task'),worktree='current',port=self.port,
                              accompanying=self.restated())
            self.assertEqual(unverified.exception.code,'unresolved_prior_attempt')
            stopped['result']['dispatch']['status']='failed'
            self.discard(first)
            replacement=guarded_start(self.project,'objective',owner='owner',run='another-run',
                                      task='shared-task',plan_revision='plan',
                                      frozen_packet=self.frozen(task='shared-task'),worktree='current',
                                      port=self.port,accompanying=self.restated())
        self.assertEqual(replacement['status'],'bound')
        self.assertEqual(len(self.port.starts),2)

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

    def test_no_uuid_recovery_binds_one_exact_existing_worker_without_relaunch(self):
        first=self.recovery_case()
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(request_uuid=None))
        result=self.recover(first)
        self.assertEqual(result['action'],'inspect_without_uuid')
        self.assertEqual(result['status'],'bound')
        self.assertIsNone(result['admission']['request_uuid'])
        self.assertEqual(result['admission']['native_binding']['dispatchId'],
                         first['native_binding']['dispatchId'])
        self.assertEqual(self.port.starts,[])

    def test_no_uuid_recovery_holds_zero_or_several_exact_workers(self):
        first=self.recovery_case()
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(request_uuid=None))
        worker=self.port.workers.pop(first['native_binding']['dispatchId'])
        absent=self.recover(first)
        self.assertEqual(absent['status'],'unresolved')
        self.assertEqual(absent['admission']['error']['code'],'native_attempt_absent')
        self.assertEqual(absent['admission']['error']['detail']['matching'],0)
        self.port.workers['dispatch-1']=worker
        self.port.workers['dispatch-2']=dict(worker)
        ambiguous=self.recover(first)
        self.assertEqual(ambiguous['status'],'unresolved')
        self.assertEqual(ambiguous['admission']['error']['code'],'native_attempt_ambiguous')
        self.assertEqual(ambiguous['admission']['error']['detail']['matching'],2)
        self.assertEqual(self.port.starts,[])

    def test_completed_conflict_survives_incomplete_and_absent_until_coherent_receipt(self):
        prior=self.recovery_case()
        coherent={'runId':'run','taskId':'task','dispatchId':prior['native_binding']['dispatchId'],
                  'state':'ready'}
        self.port.request_receipt={**coherent,'mutation':{'requestId':OTHER}}
        conflict=self.recover(prior)
        self.assertEqual(conflict['admission']['error']['code'],'native_request_conflict')
        self.assertEqual(conflict['admission']['request_uuid'],REQUEST)
        self.port.request_receipt='malformed'
        incomplete=self.recover(prior)
        self.assertEqual(incomplete['admission']['error']['code'],'native_receipt_missing')
        self.assertEqual(incomplete['admission']['recovery']['request_conflict'],'unresolved')
        self.port.state='absent'
        absent=self.recover(prior)
        self.assertEqual((absent['status'],absent['action']),('unresolved','hold'))
        self.assertEqual(self.port.starts,[])
        self.port.state='completed';self.port.request_receipt=coherent
        settled=self.recover(prior)
        self.assertEqual(settled['status'],'bound')
        self.assertNotIn('request_conflict',settled['admission']['recovery'])
        self.assertEqual(self.port.starts,[])

    def test_pending_conflict_never_replays_again_then_completed_reconciles(self):
        prior=self.recovery_case()
        self.port.state='pending'
        self.port.receipt={'runtime':'runtime','exit':0,'request_uuid':REQUEST,
                           'mutation':{'requestId':OTHER},'runId':'run','taskId':'task',
                           'dispatchId':prior['native_binding']['dispatchId'],'state':'ready'}
        first=self.recover(prior)
        self.assertEqual(first['admission']['error']['code'],'native_request_conflict')
        self.assertEqual(self.recover(prior)['action'],'hold')
        self.assertEqual(len(self.port.starts),1)
        self.port.state='absent'
        self.assertEqual(self.recover(prior)['action'],'hold')
        self.port.state='completed'
        self.port.request_receipt={'runId':'run','taskId':'task',
                                   'dispatchId':prior['native_binding']['dispatchId'],'state':'ready'}
        self.assertEqual(self.recover(prior)['status'],'bound')
        self.assertEqual(len(self.port.starts),1)

    def test_completed_raw_receipt_rejects_each_conflicting_request_reference(self):
        prior=self.recovery_case()
        base={'runId':'run','taskId':'task','dispatchId':prior['native_binding']['dispatchId'],
              'state':'ready'}
        for receipt in ({**base,'request_uuid':OTHER},
                        {**base,'mutation':{'requestId':OTHER}},
                        {**base,'mutation':'malformed'}):
            with self.subTest(receipt=receipt):
                self.port.request_receipt=receipt
                conflicted=self.recover(prior)
                self.assertEqual(conflicted['status'],'unresolved')
                self.assertEqual(conflicted['admission']['error']['code'],'native_request_conflict')
                self.assertEqual(conflicted['admission']['request_uuid'],REQUEST)
        self.assertEqual(self.port.starts,[])

    def test_refusal_request_conflict_survives_absent_adoption(self):
        prior=self.recovery_case()
        self.port.request_receipt={'exit':1,'request_uuid':REQUEST,'_request_conflict':True,
                                   'error':{'code':'task_not_startable','message':'refused'}}
        first=self.recover(prior)
        self.assertEqual((first['status'],first['admission']['error']['code']),
                         ('unresolved','native_refusal_unverified'))
        self.assertEqual(first['admission']['recovery']['request_conflict'],'unresolved')
        self.port.state='absent'
        absent=self.recover(prior)
        self.assertEqual((absent['status'],absent['action']),('unresolved','hold'))
        self.assertIsNone(absent['admission']['native_binding'])
        self.assertEqual(self.port.starts,[])

    def test_malformed_refusal_request_identity_stays_unresolved(self):
        prior=self.recovery_case()
        self.port.request_receipt={'exit':1,'request_uuid':'not-a-uuid',
                                   'error':{'code':'task_not_startable','message':'refused'}}
        first=self.recover(prior)
        self.assertEqual(first['admission']['error']['code'],'native_refusal_unverified')
        self.assertEqual(first['admission']['request_uuid'],REQUEST)
        self.port.state='absent'
        self.assertEqual(self.recover(prior)['action'],'hold')
        self.assertEqual(self.port.starts,[])

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

    def test_reused_terminal_null_launch_receipt_is_unknown_not_an_observed_mismatch(self):
        from pod.operations import _effective_evidence, _receipt_from_request_show
        from tests.common import envelope
        admission={'request':dict(ROUTE)}
        status,native=_receipt_from_request_show(envelope('reused-terminal-request-show'),
                                                 '33333333-3333-4333-8333-333333333333')
        self.assertEqual(status,'completed')
        shown=envelope('reused-terminal-worker-show')
        effective,mismatch=_effective_evidence(native,shown,admission)
        self.assertEqual(effective,{'agent':'unknown','model':'unknown','effort':'unknown',
                                    'context':'native_default'})
        self.assertFalse(mismatch)
        self.assertEqual(admission['request'],ROUTE)
        observed=deepcopy(shown)
        observed['result']['worker']['startOptions']['launch']['effective']={
            'agent':'codex','model':'gpt-6-luna','effort':'medium'}
        self.assertTrue(_effective_evidence({},observed,admission)[1])
        partial=deepcopy(shown)
        partial['result']['worker']['startOptions']['launch']['effective']={'agent':'codex','model':'','effort':7}
        effective,mismatch=_effective_evidence({},partial,admission)
        self.assertEqual((effective['agent'],effective['model'],effective['effort'],mismatch),
                         ('codex','unknown','unknown',False))
        # Known-null effective values on a real reuse bind as unknown, which still blocks acceptance.
        first=self.start()['admission']
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        self.port.effective={'agent':None,'model':None,'effort':None}
        self.discard(first,reason='the first implementation is set aside before reuse')
        original=self.port.start_worker
        def reuse(**kwargs):
            receipt=original(**kwargs)
            receipt['launch']={'requested':{'agent':None,'model':None,'effort':None},
                               'effective':{'agent':None,'model':None,'effort':None}}
            return receipt
        shown_before=self.port.show_worker
        def show(dispatch):
            shown=shown_before(dispatch)
            if dispatch!=first['native_binding']['dispatchId']:
                shown['result']['worker']['startOptions']['launch']['requested']={
                    'agent':None,'model':None,'effort':None}
            return shown
        with patch.object(self.port,'start_worker',side_effect=reuse), \
             patch.object(self.port,'show_worker',side_effect=show):
            reused=self.start('task2',reuse_of=first['admission_id'])['admission']
        self.assertFalse(reused['route_decision']['route_mismatch'])
        self.assertTrue(reused['route_decision']['effective_unknown'])
        self.assertEqual(reused['effective_evidence']['requested']['model'],ROUTE['model'])

    def test_partial_launch_shapes_compare_only_known_relevant_fields(self):
        from pod.operations import _effective_evidence
        from tests.common import envelope
        shown=envelope('reused-terminal-worker-show')
        shown['result']['worker']['startOptions']['launch']={
            'effective':{},'requested':{'agent':'codex','model':'gpt-6-sol'},'extra':'ignored'}
        receipt={'launch':{'effective':{'agent':None,'model':None,'effort':None,'extra':'ignored'},
                           'requested':{'agent':'codex','model':'gpt-6-sol','effort':None,
                                        'extra':'different'}}}
        effective,mismatch=_effective_evidence(receipt,shown,{'request':dict(ROUTE)})
        self.assertEqual([effective[key] for key in ('agent','model','effort')],['unknown']*3)
        self.assertFalse(mismatch)
        shown['result']['worker']['startOptions']['launch']['effective']['model']='gpt-6-luna'
        receipt['launch']['effective']['model']='gpt-6-sol'
        self.assertTrue(_effective_evidence(receipt,shown,{'request':dict(ROUTE)})[1])
        shown['result']['worker']['startOptions']['launch']['effective'].clear()
        receipt['launch']['effective']['model']='gpt-6-luna'
        self.assertTrue(_effective_evidence(receipt,shown,{'request':dict(ROUTE)})[1])

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

    def test_status_joins_checkpoint_and_exact_native_references_without_write(self):
        from pod.cli import execute,parser
        from pod.ledger import _path
        started=self.start()['admission']
        path=_path(self.project,'objective');before=path.read_bytes()
        with patch('pod.cli.current_run',return_value={'runtime':'runtime','run':{'id':'run'}}), \
             patch('pod.cli.worker_rows',return_value={'runtime':'runtime',
                 'scope':{'source':'flag','run':'run'},'workers':[], 'complete':True}):
            status=execute(parser().parse_args(['status','--json']),self.project)
        self.assertEqual(status['objective'],'objective')
        self.assertEqual(status['checkpoint_join']['plan_revision'],'plan')
        self.assertEqual(status['checkpoint_join']['candidate'],'candidate')
        self.assertEqual(status['checkpoint_join']['native_refs'],[{'runId':'run','runtime':'runtime'}])
        ref=status['native_references'][0]
        self.assertEqual((ref['task'],ref['dispatch'],ref['worker'],ref['terminal']),
                         ('task',started['native_binding']['dispatchId'],
                          started['native_binding']['workerId'],
                          started['native_binding']['terminalHandle']))
        self.assertEqual(path.read_bytes(),before)
        with patch('pod.cli.current_run',return_value={'runtime':'runtime','run':{'id':'run'}}), \
             patch('pod.cli.worker_rows',side_effect=PodError('orca_read_failed','offline')):
            offline=execute(parser().parse_args(['status','--json']),self.project)
        self.assertEqual(offline['status'],'unavailable')
        self.assertEqual(offline['native_references'][0]['task'],'task')
        self.assertEqual(path.read_bytes(),before)

    def test_reuse_requires_settlement_and_exact_effective_model(self):
        first=self.start()['admission']; identity=first['admission_id']
        with self.assertRaises(PodError): self.start('task2',reuse_of=identity)
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        self.discard(first,reason='the first implementation is set aside before reuse')
        second=self.start('task2',reuse_of=identity)['admission']
        self.assertEqual(self.port.starts[-1]['terminal'],first['native_binding']['terminalHandle'])
        self.assertEqual(second['reuse_of'],identity)

    def test_reuse_requires_dispatch_status_even_when_stage_is_terminal(self):
        first=self.start()['admission']; identity=first['admission_id']
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        original=self.port.show_worker
        def show_without_status(dispatch):
            shown=original(dispatch)
            shown['result']['dispatch'].pop('status')
            return shown
        with patch.object(self.port,'show_worker',side_effect=show_without_status):
            with self.assertRaises(PodError) as blocked:
                self.start('task2',reuse_of=identity)
            self.assertEqual(blocked.exception.code,'reuse_unavailable')
        self.assertEqual(len(self.port.starts),1)
        self.discard(first,reason='the first implementation is set aside before reuse')
        def show_without_stage_status(dispatch):
            shown=original(dispatch)
            shown['result']['projection']['stage'].pop('dispatch')
            return shown
        with patch.object(self.port,'show_worker',side_effect=show_without_stage_status):
            reused=self.start('task2',reuse_of=identity)['admission']
        self.assertEqual(reused['reuse_of'],identity)

    def test_route_failure_requires_dispatch_status_even_when_stage_is_terminal(self):
        first=self.start()['admission']
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        original=self.port.show_worker
        def show_without_status(dispatch):
            shown=original(dispatch)
            shown['result']['dispatch'].pop('status')
            return shown
        with patch.object(self.port,'show_worker',side_effect=show_without_status):
            with self.assertRaises(PodError) as blocked:
                self.record_failure(first,kind='unavailable')
            self.assertEqual(blocked.exception.code,'failure_attempt_unsettled')
        self.assertEqual(read(self.project,'objective')['admissions'][first['admission_id']]['failures'],[])
        self.record_failure(first,kind='unavailable')
        self.assertEqual(len(read(self.project,'objective')['admissions'][first['admission_id']]['failures']),1)

    def test_constraints_and_failures_are_objective_local(self):
        constraints_update(self.project,'objective',owner='owner',action='add',
                           value={'id':'claude-only','kind':'only_agents','provenance':'user_direct','value':['claude']})
        with self.assertRaises(PodError): self.start()
        constraints_update(self.project,'objective',owner='owner',action='revoke',constraint_id='claude-only')
        first=self.start()['admission']
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        self.record_failure(first,kind='rate_limited',retry_after='2099-01-01T00:00:00Z')
        with self.assertRaises(PodError): self.start('task2')
        self.record_failure(first,kind='rate_limited',clear=True,cleared_by='runtime_change')
        self.discard(first,reason='the failed first attempt has no result to integrate')
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

    def test_same_version_changed_bundle_blocks_new_start_but_not_existing_recovery(self):
        from pod.bundle import running_identity
        from pod.cli import execute, main as cli_main, parser
        from pod.installer import bundle_digest, read_receipt
        from contextlib import redirect_stdout
        from io import StringIO
        original=running_identity()
        first=self.start()['admission']
        changed=modified_bundle(self.root)
        self.assertEqual((changed/'VERSION').read_text().strip(),original['version'])
        receipt_path=Path(os.environ['XDG_DATA_HOME'])/'pod/install.json'
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text(json.dumps({'schema':'pod-install/v1','status':'installed',
            'previous':{'version':original['version'],'digest':original['bundle_digest']},
            'target':{'version':original['version'],'digest':bundle_digest(changed)}}))
        receipt=read_receipt(receipt_path)
        self.assertEqual(receipt['previous']['version'],receipt['target']['version'])
        self.assertNotEqual(receipt['previous']['digest'],receipt['target']['digest'])
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        with patch('pod.bundle.bundle_root',return_value=changed):
            current=running_identity()
            self.assertEqual(current['version'],original['version'])
            self.assertNotEqual(current['bundle_digest'],original['bundle_digest'])
            self.assertEqual(current['bundle_digest'],receipt['target']['digest'])
            with self.assertRaises(PodError) as blocked:
                self.start('new-task')
            self.assertEqual(blocked.exception.code,'installed_version_changed')
            self.assertIn(original['bundle_digest'][:12],str(blocked.exception))
            self.assertIn(current['bundle_digest'][:12],str(blocked.exception))
            self.assertIn('pod config --json',str(blocked.exception))
            self.assertEqual(len(self.port.starts),1)
            self.assertEqual(len(read(self.project,'objective')['admissions']),1)
            self.port.state='completed'
            recovered=self.recover(first)
            self.assertEqual(recovered['status'],'bound')
            self.assertEqual(len(self.port.starts),1)
            with patch('pod.cli.worker_rows',return_value={'workers':[],
                     'scope':{'source':'flag','run':'run'},'complete':True}):
                status=execute(parser().parse_args(['status','--run','run','--json']),self.project)
            self.assertTrue(status['installed_version_drift'])
            self.assertEqual(status['bundle_identity']['checkpoint'],original)
            self.assertEqual(status['bundle_identity']['running'],current)
            self.assertEqual(status['blocker'],'installed_version_changed')
            with patch('pod.cli.worker_rows',return_value={'workers':[],
                     'scope':{'source':'flag','run':'run'},'complete':True}), \
                 patch('pathlib.Path.cwd',return_value=self.project), \
                 redirect_stdout(StringIO()) as human:
                self.assertEqual(cli_main(['status','--run','run']),0)
            self.assertIn(original['bundle_digest'][:12],human.getvalue())
            self.assertIn(current['bundle_digest'][:12],human.getvalue())
            self.checkpoint()
            self.assertEqual(self.start('new-task')['status'],'bound')

    def test_missing_checkpoint_digest_blocks_only_new_admission(self):
        from pod.ledger import _lock, _path, _read, _write
        path=_path(self.project,'objective')
        with _lock(path):
            state=_read(path)
            state['checkpoint'].pop('bundle_digest')
            _write(path,state)
        with self.assertRaises(PodError) as blocked:
            self.start()
        self.assertEqual(blocked.exception.code,'installed_version_changed')
        self.assertIn('digest missing',str(blocked.exception))
        self.assertEqual(self.port.starts,[])
        self.assertFalse(read(self.project,'objective')['admissions'])
        self.checkpoint()
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

    def test_refusal_classifier_rejects_runtime_envelope_and_effect_contradictions(self):
        admission={'runtime':'runtime'}
        base={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
              'error':{'code':'task_not_startable','message':'refused'}}
        self.assertEqual(_preflight_refusal_classification(base,admission),'authoritative')
        self.assertEqual(_preflight_refusal_classification({**base,'request_uuid':None,
            'error':{**base['error'],'data':{'orchestrationRequestId':REQUEST}}},admission),'authoritative')
        documented={**base,'error':{**base['error'],'data':{
            'taskId':'task','runId':'run','status':'blocked','unmetDependencies':['other'],
            'retryOf':None,'terminal':'term-1','reason':'no agent','nextSteps':'wait'}}}
        self.assertEqual(_preflight_refusal_classification(documented,admission),'authoritative')
        variants={
            'wrong_runtime':{**base,'runtime':'other'},
            'missing_runtime':{k:v for k,v in base.items() if k!='runtime'},
            'zero_exit':{**base,'exit':0},
            'nested_dispatch':{**base,'error':{**base['error'],'data':{'dispatchId':'partial'}}},
            'nested_residual':{**base,'error':{**base['error'],'data':{'residualResources':[{'kind':'terminal'}]}}},
            'malformed_effects':{**base,'error':{**base['error'],'data':{'effects':None}}},
            'top_dispatch':{**base,'dispatchId':'partial'},
            'result_error':{**base,'_result_error':{'code':'other'}},
            'envelope':{**base,'_envelope_conflicts':{'workerId':'partial'}},
            'request_conflict':{**base,'_request_conflict':True},
            'malformed_data':{**base,'error':{**base['error'],'data':'unknown'}},
            'foreign_runtime_data':{**base,'error':{**base['error'],'data':{'runtimeId':'other'}}},
        }
        for name,receipt in variants.items():
            with self.subTest(name=name):
                self.assertEqual(_preflight_refusal_classification(receipt,admission),'unverified')

    def test_effect_bearing_refusal_stays_unresolved_and_error_data_uuid_is_kept(self):
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
                           'dispatchId':'partial-dispatch',
                           'error':{'code':'inject_rejected','message':'refused'}}
        partial=self.start('task1')['admission']
        self.assertEqual((partial['state'],partial['error']['code']),
                         ('unresolved','native_refusal_unverified'))
        self.assertIsNone(partial['native_binding'])
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':None,
                           'error':{'code':'task_not_startable','message':'refused',
                                    'data':{'orchestrationRequestId':OTHER}}}
        deferred=self.start('task2')['admission']
        self.assertEqual(deferred['state'],'deferred')
        self.assertEqual(deferred['request_uuid'],OTHER)

    def test_completed_and_pending_refusals_do_not_start_twice(self):
        prior=self.recovery_case()
        self.port.request_receipt={'exit':1,
                                   'error':{'code':'task_not_found','message':'refused'}}
        first=self.recover(prior)
        self.assertEqual((first['status'],self.recover(prior)['status']),('deferred','deferred'))
        self.assertEqual(self.port.starts,[])

    def test_pending_effect_free_refusal_defers_after_one_exact_replay(self):
        prior=self.recovery_case()
        self.port.state='pending'
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
                           'error':{'code':'inject_rejected','message':'refused'}}
        first=self.recover(prior)
        self.assertEqual((first['status'],self.recover(prior)['status']),('deferred','deferred'))
        self.assertEqual(len(self.port.starts),1)
        self.assertEqual(self.port.starts[0]['retry_request'],REQUEST)

    def test_unverified_recovered_refusal_holds_without_replay_loop(self):
        prior=self.recovery_case()
        self.port.state='pending'
        self.port.receipt={'runtime':'other','exit':1,'request_uuid':REQUEST,
                           'error':{'code':'task_not_startable','message':'refused'}}
        first=self.recover(prior)
        self.assertEqual(first['admission']['error']['code'],'native_refusal_unverified')
        self.assertEqual(self.recover(prior)['status'],'unresolved')
        self.assertEqual(len(self.port.starts),1)

    def test_completed_effect_bearing_refusal_holds_without_native_retry(self):
        prior=self.recovery_case()
        self.port.request_receipt={'exit':1,
            'error':{'code':'task_not_startable','message':'refused',
                     'data':{'dispatchId':'partial'}}}
        first=self.recover(prior)
        self.assertEqual(first['admission']['error']['code'],'native_refusal_unverified')
        self.assertEqual(self.recover(prior)['status'],'unresolved')
        self.assertEqual(self.port.starts,[])

    def test_runtime_error_readback_never_infers_no_effect_or_restarts(self):
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
                           'error':{'code':'runtime_error','message':'target busy'}}
        held=self.start()['admission']
        self.assertEqual((held['state'],held['error']['code']),('unresolved','native_runtime_error'))
        self.port.request_receipt={'exit':1,'error':{'code':'runtime_error','message':'again'}}
        repeated=self.recover(held)
        self.assertEqual((repeated['status'],repeated['admission']['request_uuid']),('unresolved',REQUEST))
        self.port.state='absent'
        absent=self.recover(held)
        self.assertEqual((absent['status'],absent['admission']['error']['code']),
                         ('unresolved','native_attempt_absent'))
        self.assertEqual(len(self.port.starts),1)
        self.port.workers['dispatch-1']={'run':'run','task':'task','route':ROUTE.copy(),
                                          'worktree':'current','state':'ready',
                                          'outcome':'in_progress','terminal':'term-dispatch-1'}
        self.port.state='completed';self.port.request_receipt=None
        self.assertEqual(self.recover(held)['status'],'bound')
        self.assertEqual(len(self.port.starts),1)

    def test_runtime_error_can_settle_as_completed_effect_free_refusal(self):
        self.port.receipt={'runtime':'runtime','exit':1,'request_uuid':REQUEST,
                           'error':{'code':'runtime_error','message':'uncertain'}}
        held=self.start()['admission']
        self.port.request_receipt={'exit':1,
                                   'error':{'code':'task_not_startable','message':'not ready'}}
        result=self.recover(held)
        self.assertEqual((result['status'],result['action']),('deferred','recorded_receipt'))
        self.assertEqual(len(self.port.starts),1)

    def test_request_contradiction_holds_without_uuid_substitution(self):
        self.port.receipt={'runtime':'runtime','exit':0,'request_uuid':REQUEST,
                           'mutation':{'requestId':OTHER},'runId':'run','taskId':'task',
                           'dispatchId':'dispatch-1','state':'ready'}
        held=self.start()['admission']
        self.assertEqual(held['state'],'unresolved')
        self.assertEqual(held['error']['code'],'native_request_conflict')
        self.assertEqual(len(self.port.starts),1)

    def test_lost_start_response_without_uuid_reads_back_and_holds_without_relaunch(self):
        with patch.object(self.port,'start_worker',side_effect=PodError('native_effect_uncertain','lost')) as start:
            with self.assertRaises(PodError): self.start()
        self.assertEqual(start.call_count,1)
        admission=next(iter(read(self.project,'objective')['admissions'].values()))
        self.assertEqual(admission['state'],'unresolved')
        self.assertIsNone(admission['request_uuid'])
        recovered=self.start()
        self.assertEqual(recovered['action'],'inspect_without_uuid')
        self.assertEqual(recovered['admission']['error']['code'],'native_attempt_absent')
        self.assertEqual(len(self.port.starts),0)

    def unknown_native_start(self,request=REQUEST):
        error={'code':'future_native_refusal','message':'Native rejected this exact request',
               'data':{'taskId':'task','runId':'run'}}
        if request: error['data']['orchestrationRequestId']=request
        raw=json.dumps({'ok':False,'error':error,'_meta':{'runtimeId':'runtime'}}).encode()
        original_run=subprocess.run
        def run(argv,**kwargs):
            if argv[0]=='/fixture/orca':return subprocess.CompletedProcess(argv,1,raw,b'')
            return original_run(argv,**kwargs)
        with patch.object(self.port,'start_worker',side_effect=OrcaPort().start_worker) as start, \
             patch('pod.orca.executable',return_value=Path('/fixture/orca')), \
             patch('pod.orca.subprocess.run',side_effect=run):
            admission=self.start()['admission']
        self.assertEqual(start.call_count,1)
        self.assertEqual((admission['state'],admission['request_uuid']),('unresolved',request))
        self.assertEqual(admission['error'],{'code':'native_effect_uncertain','detail':error})
        ref=admission['recovery']['start_observations'][0]
        path=objective_root(self.project,'objective')/'native-start'/(ref+'.json')
        self.assertEqual(base64.b64decode(json.loads(path.read_text())['observation']['stdout']['base64']),raw)
        return admission,path

    def test_unknown_refusal_retains_provenance_through_exact_recovery(self):
        admission,path=self.unknown_native_start();before=path.read_bytes()
        self.port.state='absent'
        with patch.object(self.port,'request_show',wraps=self.port.request_show) as shown:
            missing=self.recover(admission)
        shown.assert_called_once_with(REQUEST)
        self.assertEqual(missing['admission']['error']['code'],'native_attempt_absent')
        self.port.state='completed'
        self.port.request_receipt={'exit':1,'error':{'code':'task_not_startable','message':'not ready'}}
        self.assertEqual(self.recover(admission)['status'],'deferred')
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.port.starts,[])

    def test_unknown_refusal_without_uuid_never_becomes_absence_proof(self):
        admission,path=self.unknown_native_start(None);before=path.read_bytes()
        recovered=self.recover(admission)
        self.assertEqual((recovered['status'],recovered['action']),('unresolved','inspect_without_uuid'))
        self.assertIsNone(recovered['admission']['request_uuid'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.port.starts,[])

    def test_corrupt_or_missing_start_evidence_blocks_native_recovery(self):
        admission,path=self.unknown_native_start();before=path.read_bytes()
        for mode in ('corrupt','missing'):
            with self.subTest(mode=mode):
                if mode=='corrupt': path.write_text('{}')
                else: path.unlink()
                with patch.object(self.port,'start_worker') as start, \
                     patch.object(self.port,'request_show') as shown:
                    with self.assertRaises(PodError) as caught:self.recover(admission)
                self.assertEqual(caught.exception.code,'native_evidence_unavailable')
                start.assert_not_called();shown.assert_not_called()
                path.write_bytes(before)

    def test_pending_timeout_keeps_original_uuid_and_both_native_observations(self):
        admission,path=self.unknown_native_start();before=path.read_bytes()
        self.port.state='pending'
        partial=json.dumps({'ok':False,'error':{'code':'another_refusal',
                          'data':{'orchestrationRequestId':REQUEST}},'_meta':{'runtimeId':'runtime'}}).encode()
        original_run=subprocess.run
        calls=[]
        def run(argv,**kwargs):
            if argv[0]=='/fixture/orca':
                calls.append(argv)
                raise subprocess.TimeoutExpired(argv,1,output=partial)
            return original_run(argv,**kwargs)
        with patch.object(self.port,'start_worker',side_effect=OrcaPort().start_worker), \
             patch('pod.orca.executable',return_value=Path('/fixture/orca')), \
             patch('pod.orca.subprocess.run',side_effect=run):
            with self.assertRaises(PodError):self.recover(admission)
        self.assertEqual(len(calls),1)
        self.assertIn('--retry-request',calls[0])
        held=read(self.project,'objective')['admissions'][admission['admission_id']]
        self.assertEqual((held['state'],held['request_uuid']),('unresolved',REQUEST))
        self.assertEqual(len(held['recovery']['start_observations']),2)
        self.assertEqual(path.read_bytes(),before)

    def test_observation_limit_blocks_before_another_native_effect(self):
        admission,_=self.unknown_native_start();self.port.state='pending'
        with patch('pod.operations.MAX_START_OBSERVATIONS',1), \
             patch.object(self.port,'start_worker') as start:
            with self.assertRaises(PodError) as caught:self.recover(admission)
        self.assertEqual(caught.exception.code,'native_evidence_full');start.assert_not_called()

    def test_alternative_after_failure_requires_native_settlement(self):
        first=self.start()['admission']
        with self.assertRaises(PodError) as unsettled:
            self.record_failure(first,kind='unavailable')
        self.assertEqual(unsettled.exception.code,'failure_attempt_unsettled')
        # A persisted failure from an earlier observation still cannot justify
        # an alternate start while the exact native assignment remains live.
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row['failures'].append({
                             'kind':'unavailable','model':'gpt-6-sol','task':'task',
                             'retry_after':None,'cleared_at':None}))
        alternate={**ROUTE,'model':'gpt-6-luna','reason':'available alternative'}
        with self.assertRaises(PodError) as blocked:
            self.start('task2',frozen=self.frozen(route=alternate,task='task2'))
        self.assertEqual(blocked.exception.code,'failed_attempt_unsettled')
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row['failures'].clear())
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        self.record_failure(first,kind='unavailable')
        self.discard(first,reason='the failed attempt has no result to integrate')
        self.assertEqual(self.start('task2',frozen=self.frozen(route=alternate,task='task2'))['status'],'bound')

    def test_safety_refusal_bars_same_task_on_another_run(self):
        first=self.start()['admission']
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        self.record_failure(first,kind='safety_refusal')
        from pod.ledger import _lock, _path, _read, _write
        state_path=_path(self.project,'objective')
        with _lock(state_path):
            state=_read(state_path)
            state['checkpoint']['native_refs'].append({'runId':'run2','runtime':'runtime'})
            _write(state_path,state)
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        alternate={**ROUTE,'model':'gpt-6-luna'}
        with self.assertRaises(PodError) as caught:
            guarded_start(self.project,'objective',owner='owner',run='run2',task='task',
                          plan_revision='plan',frozen_packet=self.frozen(route=alternate),
                          worktree='current',port=self.port,accompanying=self.restated())
        self.assertEqual(caught.exception.code,'safety_refusal')

    def test_live_bound_task_on_another_run_blocks_replacement_until_settled(self):
        first=self.start('shared-task')['admission']
        from pod.ledger import _lock, _path, _read, _write
        state_path=_path(self.project,'objective')
        with _lock(state_path):
            state=_read(state_path)
            state['checkpoint']['native_refs'].append({'runId':'another-run','runtime':'runtime'})
            _write(state_path,state)
        def replacement():
            return guarded_start(self.project,'objective',owner='owner',run='another-run',
                                 task='shared-task',plan_revision='plan',
                                 frozen_packet=self.frozen(task='shared-task'),worktree='current',
                                 port=self.port,accompanying=self.restated())
        with self.assertRaises(PodError) as blocked:
            replacement()
        self.assertEqual(blocked.exception.code,'unresolved_prior_attempt')
        self.assertEqual(len(self.port.starts),1)
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='succeeded'
        self.discard(first)
        self.assertEqual(replacement()['status'],'bound')
        self.assertEqual(len(self.port.starts),2)

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
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        with patch.object(OrcaPort,'read_native',autospec=True,
                          side_effect=lambda _port,owner,**kwargs:self.port.read_native(owner,**kwargs)):
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
        self.port.workers[first['native_binding']['dispatchId']]['outcome']='failed'
        self.record_failure(first,kind='unavailable')
        self.discard(first,reason='the failed attempt has no result to integrate')
        alternative={**ROUTE,'model':'gpt-6-luna','reason':'failed original route'}
        frozen=self.frozen(route=alternative,task='task2')
        set_model(self.personal,'gpt-6-luna','disabled',displayed=load_config(self.project))
        with self.assertRaises(PodError) as caught: self.start('task2',frozen=frozen)
        self.assertEqual(caught.exception.code,'preference_changed')
        with self.assertRaises(PodError) as caught: self.start('task2',frozen=self.frozen(route=alternative,task='task2'))
        self.assertEqual(caught.exception.code,'model_ineligible')
        self.assertEqual(len(self.port.starts),1)

    def test_issue_change_holds_pending_replay_but_completed_read_is_observational(self):
        source={'schema':'pod-issue-source/v1','repository':'acme/widgets','number':7,
                'locator':'https://github.com/acme/widgets/issues/7','body_sha256':'a'*64,
                'amendments':[]}
        checkpoint_value=read(self.project,'objective')['checkpoint']
        checkpoint(self.project,'objective',owner='owner',
                   value={key:value for key,value in {**checkpoint_value,'objective_source':source}.items()
                      if key not in ('pod_version','bundle_digest','seq')},native={'runtime':'runtime'})
        frozen=packet({**self.frozen()['body'],'objective_source':source})
        with patch('pod.github.issue_recheck',return_value={'status':'current'}):
            first=self.start(frozen=frozen)['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=first['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.starts.clear();self.port.state='pending'
        with patch('pod.github.issue_recheck',return_value={'status':'reconciliation_required'}), \
             self.assertRaises(PodError) as changed:
            self.recover(first)
        self.assertEqual(changed.exception.code,'issue_reconciliation_required')
        self.assertEqual(self.port.starts,[])
        revised={**source,'body_sha256':'b'*64}
        revised_packet=packet({**frozen['body'],'objective_source':revised})
        with self.assertRaises(PodError) as duplicate:
            self.start(frozen=revised_packet)
        self.assertEqual(duplicate.exception.code,'unresolved_prior_attempt')
        self.port.state='completed'
        with patch('pod.github.issue_recheck',side_effect=AssertionError('completed is observational')):
            self.assertEqual(self.recover(first)['status'],'bound')

    def test_same_selector_resolving_elsewhere_blocks_start_and_pending_replay(self):
        placement={'repository':None,'repo_key':'a'*64,
                   'path':str(self.root/'assignment'),'branch':'orca/check'}
        frozen=packet({**self.frozen()['body'],'placement':placement})
        self.port.placement={**placement,'runtime':'runtime','path':str(self.root/'wrong')}
        with self.assertRaises(PodError) as wrong:
            guarded_start(self.project,'objective',owner='owner',run='run',task='task',
                          plan_revision='plan',frozen_packet=frozen,
                          worktree='name:isolation',port=self.port)
        self.assertEqual(wrong.exception.code,'worktree_binding_changed')
        self.assertEqual(self.port.starts,[])
        self.port.placement={**placement,'runtime':'runtime'}
        started=guarded_start(self.project,'objective',owner='owner',run='run',task='task',
                              plan_revision='plan',frozen_packet=frozen,
                              worktree='name:isolation',port=self.port)['admission']
        update_admission(self.project,'objective',owner='owner',admission_id=started['admission_id'],
                         update=lambda row:row.update(state='unresolved',native_binding=None))
        self.port.starts.clear();self.port.state='pending'
        self.port.placement={**placement,'runtime':'runtime','repository':'other/repository'}
        with self.assertRaises(PodError) as moved:
            recover_admission(self.project,'objective',owner='owner',admission_id=started['admission_id'],
                              worktree='name:isolation',port=self.port)
        self.assertEqual(moved.exception.code,'worktree_binding_changed')
        self.assertEqual(self.port.starts,[])

    def test_source_and_instruction_context_state_matrix_before_worker_start(self):
        for kind in ('source','instruction'):
            for change,code in (('changed','source_changed'),('absent','source_absent')):
                with self.subTest(kind=kind,change=change):
                    path=self.project/f'{kind}-{change}.txt';path.write_text('one')
                    bound=source_identity(self.project,path.name)
                    frozen=self.frozen(context=[{'kind':kind,'path':path.name,'sha256':bound['sha256']}],
                                       task=f'{kind}-{change}')
                    path.write_text('two') if change=='changed' else path.unlink()
                    with self.assertRaises(PodError) as blocked:
                        self.start(task=f'{kind}-{change}',frozen=frozen)
                    self.assertEqual(blocked.exception.code,code)
        self.assertEqual(self.port.starts,[])
        unavailable=self.frozen(sources=[{'path':'unavailable.txt','state':'unavailable'}],task='unavailable')
        with self.assertRaises(PodError) as blocked:
            self.start(task='unavailable',frozen=unavailable)
        self.assertEqual(blocked.exception.code,'source_unbound')
        self.assertEqual(self.port.starts,[])

    def test_new_output_file_is_not_a_bound_source_and_refusal_names_next_action(self):
        output='trial/t3-install-notes.md'
        frozen=self.frozen(sources=[{'path':output,'state':'present','sha256':'a'*64}],task='future-output')
        with self.assertRaises(PodError) as blocked:
            self.start(task='future-output',frozen=frozen)
        self.assertEqual(blocked.exception.code,'source_absent')
        self.assertIn(output,str(blocked.exception))
        self.assertIn('existing inputs',str(blocked.exception))
        self.assertIn('scope/actions',str(blocked.exception))
        self.assertIn('rebuild the packet',str(blocked.exception))
        self.assertEqual(self.port.starts,[])

    def test_two_objectives_have_independent_logical_slots(self):
        checkpoint_value={key:value for key,value in read(self.project,'objective')['checkpoint'].items()
                          if key not in ('pod_version','bundle_digest','objective')}
        checkpoint(self.project,'second',owner='owner',value=checkpoint_value,native={'runtime':'runtime'})
        for task in ('first','second'):
            self.assertEqual(self.start(task)['status'],'bound')
        with self.assertRaises(PodError) as full:
            self.start('third')
        self.assertEqual(full.exception.code,'logical_capacity_full')
        second_packet=packet({**self.frozen(task='other')['body'],'objective':'second'})
        other=guarded_start(self.project,'second',owner='owner',run='run',task='other',
                            plan_revision='plan',frozen_packet=second_packet,port=self.port)
        self.assertEqual(other['status'],'bound')

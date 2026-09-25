import base64
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.orca import (contract, current_run, executable, mutate_command, read_command,
                      worker_rows, worktree_identity, worktree_selector, _mutation_envelope)
from pod.operations import OrcaPort
from tests.common import envelope, ORCA_VERSION

START=['orchestration','worker-start','--task','task','--run','run','--worktree','current',
       '--agent','codex','--model','gpt-6-sol','--effort','high','--json']


class OrcaAdapterTests(unittest.TestCase):
    def test_cleanup_terminal_inventory_is_an_exact_read_shape(self):
        from pod.orca import _read_allowed
        self.assertTrue(_read_allowed(["terminal", "list", "--worktree", "path:/fixture/work", "--json"]))
        self.assertTrue(_read_allowed(["worktree", "show", "--worktree", "path:/fixture/work", "--json"]))
        self.assertFalse(_read_allowed(["terminal", "list", "--worktree", "new-child", "--json"]))
        self.assertFalse(_read_allowed(["terminal", "close", "--worktree", "path:/fixture/work", "--json"]))

    def test_real_capture_shapes_parse_after_sanitization(self):
        root=Path(__file__).parent/'fixtures'/'orca-1.4.209'
        start=json.loads((root/'worker-start.json').read_text())
        shown=json.loads((root/'worker-show.json').read_text())
        request=json.loads((root/'request-show.json').read_text())
        decoded=_mutation_envelope(json.dumps(start))
        self.assertEqual(decoded['result']['launch']['effective']['model'],'gpt-6-sol')
        self.assertEqual(shown['result']['worker']['startOptions']['launch']['effective']['effort'],'high')
        self.assertEqual(request['result']['method'],'orchestration.workerStart')
        self.assertEqual(request['result']['receipt']['dispatchId'],shown['result']['dispatch']['id'])

    def test_current_run_binding_is_stable_and_exact_for_authority(self):
        from pod.operations import OrcaPort
        row={'runtime':'runtime','run':{'id':'run','coordinator_handle':'owner','consumer_generation':4}}
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'owner'}), \
             patch('pod.operations.current_run',side_effect=[row,row]):
            self.assertTrue(OrcaPort().read_native('owner',authority_runs=('run',))['authoritative'])
        changed={**row,'run':{**row['run'],'consumer_generation':5}}
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'owner'}), \
             patch('pod.operations.current_run',side_effect=[row,changed]):
            self.assertFalse(OrcaPort().read_native('owner',authority_runs=('run',))['authoritative'])
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':'other'}), \
             patch('pod.operations.current_run',side_effect=[row,row]):
            self.assertFalse(OrcaPort().read_native('owner',authority_runs=('run',))['authoritative'])

    def test_discovery_and_native_profile_are_unchanged(self):
        with patch.dict(os.environ,{},clear=False):
            for key in ('ORCA_CLI_COMMAND','ORCA_DEV_REPO_ROOT','ORCA_TERMINAL_HANDLE'):
                os.environ.pop(key,None)
            with patch('pod.orca.shutil.which',return_value='/fixture/orca-ide') as which, \
                 patch('pod.orca.Path.is_file',return_value=True):
                self.assertEqual(executable(),Path('/fixture/orca-ide'))
            which.assert_called_once_with('orca-ide')
        native={'CODEX_HOME':'native-codex','CLAUDE_CONFIG_DIR':'native-claude'}
        with patch.dict(os.environ,{**native,'POD_CONFIG_HOME':'/isolated/config'}), \
             patch('pod.orca.executable',return_value=Path('/fixture/orca')), \
             patch('pod.orca.subprocess.run',return_value=subprocess.CompletedProcess([],0,'orca 1\n','')) as runner:
            read_command(['--version'])
            self.assertNotIn('env',runner.call_args.kwargs)

    def test_read_allowlist_refuses_native_mutations_and_account_reads(self):
        with patch('pod.orca.subprocess.run') as runner:
            for argv in (['account','list','--json'],['host','list','--json'],
                         ['orchestration','worker-list','--include-remote','--json'],
                         ['orchestration','check','--json'],['orchestration','run-list','--json']):
                with self.subTest(argv=argv),self.assertRaises(PodError): read_command(argv)
            runner.assert_not_called()

    def test_exact_run_worker_pages_and_authority_shape(self):
        pages=[{'runtime':'r','result':{'scope':{'source':'flag','run':'run'},'workers':[{'dispatchId':'a'}],
                                       'page':{'hasMore':True,'nextCursor':'next'}}},
               {'runtime':'r','result':{'scope':{'source':'flag','run':'run'},'workers':[{'dispatchId':'b'}],
                                       'page':{'hasMore':False}}}]
        with patch('pod.orca.read_command',side_effect=pages):
            self.assertEqual([row['dispatchId'] for row in worker_rows('run')['workers']],['a','b'])
        with patch('pod.orca.read_command',return_value={'runtime':'r','result':{'run':{
                'id':'run','coordinator_handle':'owner','consumer_generation':1}}}):
            self.assertEqual(current_run()['run']['id'],'run')
        with patch('pod.orca.read_command',return_value={'runtime':'r','result':{'run':{'id':'run'}}}):
            with self.assertRaises(PodError): current_run()

    def test_capability_is_required_for_delegation_only(self):
        with patch('pod.operations.contract',return_value={'status':'observed','runtime':'r',
                'capabilities':{'launch_preferences_v1':True}}):
            self.assertEqual(OrcaPort().capability()['runtime'],'r')
        with patch('pod.operations.contract',return_value={'status':'observed','runtime':'r',
                'capabilities':{'launch_preferences_v1':False}}):
            with self.assertRaises(PodError) as caught: OrcaPort().capability()
            self.assertEqual(caught.exception.code,'launch_preferences_unavailable')

    def test_three_exact_worker_argv_shapes(self):
        port=OrcaPort()
        route={'agent':'codex','model':'gpt-6-sol','effort':'high','context':'native_default','reason':'test'}
        with patch('pod.operations.mutate_command',return_value={'runtime':'r','exit':0,'request_uuid':None,
                'result':{'state':'ready'}}) as mutate:
            port.start_worker(run='r',task='t',owner='o',route=route)
            self.assertEqual(mutate.call_args.args[0],['orchestration','worker-start',
                '--task','t','--run','r','--worktree','current','--agent','codex',
                '--model','gpt-6-sol','--effort','high','--json'])
            port.start_worker(run='r',task='t',owner='o',route={**route,'effort':'native_default'})
            self.assertNotIn('--effort',mutate.call_args.args[0])
            port.start_worker(run='r',task='t',owner='o',route=route,terminal='terminal')
            argv=mutate.call_args.args[0]
            self.assertIn('--terminal',argv)
            self.assertNotIn('--model',argv)
            self.assertNotIn('--agent',argv)

    def test_pending_retry_uses_the_original_uuid_in_orca_argv(self):
        request='11111111-1111-4111-8111-111111111111'
        route={'agent':'codex','model':'gpt-6-sol','effort':'high',
               'context':'native_default','reason':'same admitted start'}
        with patch('pod.operations.mutate_command',return_value={
                'runtime':'runtime','exit':0,'request_uuid':request,
                'result':{'runId':'run','taskId':'task','dispatchId':'dispatch'}}) as native:
            result=OrcaPort().start_worker(run='run',task='task',owner='owner',
                                           route=route,worktree='current',retry_request=request)
        self.assertEqual(result['request_uuid'],request)
        self.assertEqual(native.call_args.args[0][-3:],['--retry-request',request,'--json'])
        self.assertEqual(native.call_args.args[0].count('--retry-request'),1)

    def test_mutation_allowlist_blocks_every_other_native_effect(self):
        with patch('pod.orca.subprocess.run') as runner:
            for argv in (['orchestration','worker-release','--dispatch','d','--json'],
                         ['orchestration','worker-start','--task','t','--json'],
                         START[:-1]+['--context','max','--json'],
                         START[:-1]+['--terminal','t','--json'],
                         ['orchestration','worker-start','--task','t','--run','r','--worktree',
                          'new-child','--agent','codex','--model','gpt-6-sol','--json']):
                with self.subTest(argv=argv),self.assertRaises(PodError) as caught:
                    mutate_command(argv)
                self.assertEqual(caught.exception.code,'unsupported_orca_mutation')
            runner.assert_not_called()

    def test_refusal_envelope_preserves_request_and_partial_effects(self):
        request='11111111-1111-4111-8111-111111111111'
        for code in ('task_not_found','task_not_startable','inject_rejected','runtime_error','unknown'):
            value={'ok':False,'error':{'code':code,'message':'refused',
                                      'data':{'orchestrationRequestId':request}},
                   'result':{'dispatchId':'dispatch'},'_meta':{'runtimeId':'r'}}
            decoded=_mutation_envelope(json.dumps(value))
            self.assertEqual(decoded['request_uuid'],request)
            self.assertEqual(decoded['result']['dispatchId'],'dispatch')
            self.assertEqual(decoded['error'],value['error'])

    def test_uncertain_outcomes_preserve_raw_provenance_and_available_request(self):
        request='11111111-1111-4111-8111-111111111111'
        response=json.dumps({'ok':False,'error':{'code':'future_refusal',
                            'data':{'orchestrationRequestId':request}},
                            '_meta':{'runtimeId':'r'}}).encode()
        cases=[('exit',subprocess.CompletedProcess([],7,response,b'detail'),response,request),
               ('timeout',subprocess.TimeoutExpired([],1,output=response,stderr=b'detail'),response,request),
               ('malformed',subprocess.CompletedProcess([],1,b'{invalid',b'detail'),b'{invalid',None),
               ('invalid_encoding',subprocess.CompletedProcess([],1,b'\xff',b'detail'),b'\xff',None),
               ('deep_json',subprocess.CompletedProcess([],1,b'['*200000,b'detail'),b'['*200000,None),
               ('malformed_result',subprocess.CompletedProcess([],1,
                    response.replace(b'"ok": false',b'"result": [], "ok": false'),b'detail'),
                    response.replace(b'"ok": false',b'"result": [], "ok": false'),request)]
        for name,outcome,raw,expected_request in cases:
            with (self.subTest(case=name), patch('pod.orca.executable',return_value=Path('/fixture/orca')),
                 patch('pod.orca.subprocess.run',**({'side_effect':outcome} if isinstance(outcome,Exception)
                                                  else {'return_value':outcome}))):
                with self.assertRaises(PodError) as caught: mutate_command(START,accept_exit=(0,1))
                exc=caught.exception
                self.assertEqual(exc.code,'native_effect_uncertain')
                self.assertEqual(base64.b64decode(exc.native_observation['stdout']['base64']),raw)
                self.assertEqual(base64.b64decode(exc.native_observation['stderr']['base64']),b'detail')
                self.assertEqual(getattr(exc,'native_reference',{}).get('request_uuid'),expected_request)
                self.assertIsNone(exc.detail)  # Raw private output is not copied into public error text.

    def test_request_reference_conflict_is_not_flattened_away(self):
        one='11111111-1111-4111-8111-111111111111'
        two='22222222-2222-4222-8222-222222222222'
        value={'ok':False,'error':{'code':'inject_rejected','data':{'orchestrationRequestId':one}},
               'result':{'mutation':{'requestId':two}},'_meta':{'runtimeId':'r'}}
        decoded=_mutation_envelope(json.dumps(value))
        self.assertTrue(decoded['result']['_request_conflict'])

    def test_refusal_envelope_preserves_conflicting_partial_effects(self):
        payload={'ok':False,'error':{'code':'inject_rejected','message':'refused'},
                 'result':{'dispatchId':'one'},'dispatchId':'two','workerId':'three',
                 '_meta':{'runtimeId':'runtime'}}
        decoded=_mutation_envelope(json.dumps(payload))
        self.assertEqual(decoded['result']['dispatchId'],'one')
        self.assertEqual(decoded['result']['workerId'],'three')
        self.assertEqual(decoded['result']['_envelope_conflicts']['dispatchId'],'two')

    def test_worktree_selector_and_identity_refuse_creation_or_contradiction(self):
        for value in ('current','active','path:/fixture/repo','id:abc','name:task'):
            self.assertEqual(worktree_selector(value),value)
        for value in ('new-child','new-top-level','',None):
            self.assertIsNone(worktree_selector(value))
        row={'path':'/fixture/repo','branch':'refs/heads/orca/task','isBare':False,
             'git':{'path':'/fixture/repo','branch':'refs/heads/orca/task','isBare':False}}
        with patch('pod.orca.read_command',return_value={'runtime':'r','result':{'worktree':row}}):
            self.assertEqual(worktree_identity('current')['branch'],'orca/task')
        with patch('pod.orca.read_command',return_value={'runtime':'r','result':{'worktree':{
                **row,'git':{**row['git'],'path':'/fixture/other'}}}}):
            with self.assertRaises(PodError): worktree_identity('current')

    def test_contract_reports_observed_capabilities_without_probing_accounts(self):
        with patch('pod.orca.read_command',side_effect=[{'version':ORCA_VERSION,'executable':'/fixture/orca'},
                envelope('status')]) as reader:
            found=contract()
        self.assertEqual(found['status'],'observed')
        self.assertTrue(found['capabilities']['launch_preferences_v1'])
        self.assertEqual(reader.call_count,2)

    def test_worker_page_scope_must_bind_exact_run_and_stay_stable(self):
        valid={'runtime':'runtime','result':{'scope':{'source':'flag','run':'run'},
                'workers':[{'dispatchId':'one'}], 'page':{'hasMore':True,'nextCursor':'next'}}}
        for scope in (None, {'source':'fleet'}, {'source':'flag','run':'other'},
                      {'source':'bound','run':'run'}):
            with self.subTest(scope=scope), patch('pod.orca.read_command',return_value={
                    **valid,'result':{**valid['result'],'scope':scope}}), self.assertRaises(PodError):
                worker_rows('run')
        later={**valid,'result':{**valid['result'],'scope':{'source':'flag','run':'other'},
                                'page':{'hasMore':False}}}
        with patch('pod.orca.read_command',side_effect=[valid,later]), self.assertRaises(PodError):
            worker_rows('run')

    def test_contract_needs_runtime_status_not_version_only(self):
        with patch('pod.orca.read_command',side_effect=[
                {'version':ORCA_VERSION,'executable':'/fixture/orca'},
                PodError('orca_read_failed','status offline')]):
            report=contract()
        self.assertEqual(report['status'],'unavailable')
        self.assertIsNone(report['runtime'])
        self.assertEqual(report['reason'],'orca_read_failed')

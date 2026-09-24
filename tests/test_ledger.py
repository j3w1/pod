import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import effective, write_defaults
from pod.errors import PodError
from pod.ledger import (ADMISSION_STATES, checkpoint, check_bound_sources, logical_projection,
                        context_root_for_run, read, state_inventory, state_root, update_admission)
from pod.records import source_identity
from tests.common import fixture


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.env=patch.dict(os.environ,{'XDG_STATE_HOME':str(self.root/'state'),
                                     'XDG_CONFIG_HOME':str(self.root/'config')})
        self.env.__enter__(); self.addCleanup(self.env.__exit__,None,None,None)
        authority=patch('pod.ledger.require_authority',return_value={
            'runtime':'runtime','run_id':'run','references':{'run':'runtime'}})
        authority.start(); self.addCleanup(authority.stop)
        self.project=self.root/'project'; self.project.mkdir()
        write_defaults(self.root/'config'/'pod'/'config.yaml')

    def checkpoint(self, objective='objective'):
        value={'schema':'pod-checkpoint/v2','criteria':['works'],'plan_revision':'plan',
               'candidate':'candidate','policy_revision':effective(self.project)['revision'],
               'native_refs':[],'assignments':[],'questions':[],
               'verification_gaps':[],'next_safe_action':'inspect'}
        return checkpoint(self.project,objective,owner='owner',value=value,native={'runtime':'runtime'})

    def test_checkpoint_is_versioned_and_context_is_compact(self):
        state=self.checkpoint()
        from pod import __version__
        self.assertEqual(state['schema'],'pod-context/v4')
        self.assertEqual(state['checkpoint']['pod_version'],__version__)
        self.assertEqual(state['constraints'],[])
        self.assertEqual(ADMISSION_STATES,('reserved','bound','unresolved','closed','deferred'))

    def test_foreign_schema_blocks_only_its_objective_without_conversion(self):
        from pod.ledger import _path
        path=_path(self.project,'other'); path.parent.mkdir(parents=True)
        raw=b'{"schema":"pod-context/v0"}\n'; path.write_bytes(raw)
        with self.assertRaises(PodError) as caught: read(self.project,'other')
        self.assertEqual(caught.exception.code,'state_unsupported')
        self.assertEqual(path.read_bytes(),raw)
        self.checkpoint('objective')
        self.assertEqual(state_inventory(self.project)['unsupported'],1)
        self.assertIsNone(context_root_for_run('unrelated-run'))

    def test_malformed_and_unreadable_records_are_counted_without_rewrite(self):
        from pod.ledger import _path
        path=_path(self.project,'broken');path.parent.mkdir(parents=True)
        malformed=b'{not-json\n';path.write_bytes(malformed)
        self.assertEqual(state_inventory(self.project)['unreadable'],1)
        self.assertEqual(path.read_bytes(),malformed)
        with self.assertRaises(PodError): read(self.project,'broken')
        path.write_text('{"schema":"pod-context/v4"}\n')
        path.chmod(0)
        try:
            self.assertEqual(state_inventory(self.project)['unreadable'],1)
        finally:
            path.chmod(0o600)
        self.assertEqual(path.read_text(),'{"schema":"pod-context/v4"}\n')

    def test_source_rejection_is_durable(self):
        self.checkpoint()
        file=self.project/'source.txt'; file.write_text('first')
        bound=source_identity(self.project,'source.txt')
        file.write_text('second')
        with self.assertRaises(PodError) as caught:
            check_bound_sources(self.project,'objective',owner='owner',assignment='assignment',sources=[bound])
        self.assertEqual(caught.exception.code,'source_changed')
        file.write_text('first')
        with self.assertRaises(PodError) as caught:
            check_bound_sources(self.project,'objective',owner='owner',assignment='assignment',sources=[bound])
        self.assertEqual(caught.exception.code,'source_rejected')

    def test_projection_rejects_fleet_shapes_and_frees_only_exact_settlement(self):
        from pod.ledger import _path, _read, _write, _lock
        self.checkpoint()
        path=_path(self.project,'objective')
        with _lock(path):
            state=_read(path)
            row={'schema':'pod-admission/v3','state':'bound','admission_id':'a','objective':'objective',
                 'owner':'owner','request':{'agent':'codex','model':'gpt-6-sol','effort':'medium',
                                          'context':'native_default','reason':'test'},
                 'route_decision':{},'effective_evidence':{},'runtime':'runtime','request_uuid':None,
                 'run_id':'run','task_id':'task','plan_revision':'plan','packet_id':'packet',
                 'worktree':'current','reuse_of':None,
                 'native_binding':{'runId':'run','taskId':'task','dispatchId':'dispatch',
                                   'workerId':'worker','worktreeId':'worktree','terminalHandle':None},
                 'recovery':{},'error':None,'failures':[],
                 'created_at':'2026-09-24T00:00:00Z','updated_at':'2026-09-24T00:00:00Z'}
            state['admissions']['a']=row; _write(path,state)
        with self.assertRaises(PodError):
            logical_projection(self.project,{'runtime':'runtime','scope':'fleet','complete':True,'assignments':[]},objective='objective')
        base={'runtime':'runtime','scope':'objective_assignments','complete':True,'authoritative':True,
              'owner':'owner','assignments':[]}
        self.assertEqual(len(logical_projection(self.project,base,objective='objective')['outstanding']),1)
        evidence={'admission_id':'a','runtime':'runtime','run_id':'run','task_id':'task',
                  'dispatch_id':'dispatch','worker_id':'worker','settled':True}
        self.assertEqual(logical_projection(self.project,{**base,'assignments':[evidence]},
                                            objective='objective')['outstanding'],[])
        self.assertEqual(len(logical_projection(self.project,{**base,'assignments':[evidence,evidence]},
                                                objective='objective')['outstanding']),1)

    def test_native_state_root_symlink_is_allowed_but_owned_redirect_is_not(self):
        real=self.root/'real-state'; real.mkdir()
        link=self.root/'linked-state'; link.symlink_to(real,target_is_directory=True)
        with patch.dict(os.environ,{'XDG_STATE_HOME':str(link)}):
            self.assertEqual(state_root(self.project),real/'pod')
            self.checkpoint()
            owned=real/'pod'
            self.assertTrue(owned.is_dir())
            (owned/'redirect').symlink_to(self.root,target_is_directory=True)
            self.assertIsNotNone(read(self.project,'objective'))

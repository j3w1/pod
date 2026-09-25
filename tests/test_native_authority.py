"""Native ownership fences, exercised without the offline kernel's authority stub."""

from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.config import effective, load as load_config, write_defaults
from pod.errors import PodError
from pod.governor import prepare_candidate
from pod.internal import run as internal_run
from pod.ledger import (check_bound_sources, checkpoint, constraints_update,
                        intervention, objective_root, read, route_failure, update_admission)
from pod.operations import guarded_start
from pod.records import packet
from tests.common import fixture, kernel_binding, kernel_map
from tests.test_governor import observation
from tests.test_operations import FakePort, ROUTE


def binding(run='run', owner='owner', generation=1, runtime='runtime'):
    return {'runtime':runtime,'run':{'id':run,'coordinator_handle':owner,
                                    'consumer_generation':generation}}


class NativeAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.project=self.root/'project';self.project.mkdir()
        write_defaults(self.root.parent/'config-home'/'pod'/'config.yaml')
        self.value={'schema':'pod-checkpoint/v3','criteria':['works'],'plan_revision':'plan',
                    'candidate':'candidate','policy_revision':effective(self.project)['revision'],
                    'native_refs':[],'assignments':[],'questions':[],
                    'verification_gaps':[],'next_safe_action':'inspect',**kernel_map()}

    @contextmanager
    def native(self, *, first=None, second=None, bootstrap=None, owner='owner'):
        before=first if first is not None else binding(owner=owner)
        after=second if second is not None else before
        initial=bootstrap if bootstrap is not None else before
        with patch.dict(os.environ,{'ORCA_TERMINAL_HANDLE':owner}), \
             patch('pod.orca.current_run',return_value=initial), \
             patch('pod.operations.current_run',side_effect=[before,after]):
            yield

    def establish(self):
        with self.native():
            return checkpoint(self.project,'objective',owner='owner',
                              value=self.value,native={'runtime':'runtime'})

    def test_first_checkpoint_binds_actual_run_and_rejects_foreign_supplied_ref(self):
        with self.native():
            state=checkpoint(self.project,'objective',owner='owner',
                             value=self.value,native={'runtime':'runtime'})
        self.assertEqual(state['checkpoint']['native_refs'],[{'runId':'run','runtime':'runtime'}])
        changed={**self.value,'candidate':'wrong','native_refs':[{'runId':'foreign','runtime':'runtime'}]}
        before=(objective_root(self.project,'objective')/'context.json').read_bytes()
        with self.native(),self.assertRaises(PodError) as caught:
            checkpoint(self.project,'objective',owner='owner',value=changed,native={'runtime':'runtime'})
        self.assertEqual(caught.exception.code,'native_authority_unverified')
        self.assertEqual((objective_root(self.project,'objective')/'context.json').read_bytes(),before)

    def test_checkpoint_rewrite_without_binding_or_with_changed_generation_is_refused(self):
        self.establish()
        changed={**self.value,'candidate':'new'}
        path=objective_root(self.project,'objective')/'context.json'; before=path.read_bytes()
        for pair in (({'runtime':'runtime','run':None},)*2,
                     (binding(),binding(generation=2)),
                     (binding(run='foreign'),binding(run='foreign')),
                     (binding(owner='other'),binding(owner='other')),
                     (binding(runtime='other'),binding(runtime='other'))):
            with self.subTest(pair=pair),self.native(first=pair[0],second=pair[1]), \
                 self.assertRaises(PodError) as blocked:
                checkpoint(self.project,'objective',owner='owner',value=changed,native={'runtime':'runtime'})
            self.assertEqual(blocked.exception.code,'native_authority_unverified')
            self.assertEqual(path.read_bytes(),before)

    def test_private_checkpoint_and_governor_mutations_cannot_claim_owner_without_current_run(self):
        self.establish()
        path=objective_root(self.project,'objective')/'context.json';before=path.read_bytes()
        missing={'runtime':'runtime','run':None}
        base={'project':str(self.project),'objective':'objective','owner':'owner'}
        with self.native(first=missing,second=missing), \
             patch('pod.orca.contract',return_value={'status':'observed','runtime':'runtime'}), \
             self.assertRaises(PodError) as blocked:
            internal_run('checkpoint',{**base,'value':{**self.value,'candidate':'rewritten'}})
        self.assertEqual(blocked.exception.code,'native_authority_unverified')
        self.assertEqual(path.read_bytes(),before)
        requests={
            'governor-prepare':{'unit':'unit'},
            'governor':{'action':{}},
            'governor-execute':{'action':{}},
            'governor-preflight':{'unit':'unit','candidate':'candidate','check':'unit','status':'PASS'},
            'governor-outcome':{'record_id':'record','outcome':'PASS'},
            'governor-classify':{'record_id':'record','classification':{'class':'code_defect'}},
            'governor-correct':{'unit':'unit','correction':{}},
            'governor-reconcile':{'record_id':'record'},
        }
        for operation,extra in requests.items():
            with self.subTest(operation=operation),self.native(first=missing,second=missing), \
                 self.assertRaises(PodError) as blocked:
                internal_run(operation,{**base,**extra})
            self.assertEqual(blocked.exception.code,'native_authority_unverified')
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(objective_root(self.project,'objective').glob('governor.json')),[])

    def test_foreign_run_admission_refuses_before_native_call_or_write(self):
        self.establish()
        snapshot=load_config(self.project)
        frozen=packet({'schema':'pod-packet/v3','objective':'objective','criteria':['works'],**kernel_binding(),
                       'responsibility':'writer','scope':['notes.txt'],'actions':['edit'],
                       'candidate':'candidate','context':[],'dependencies':[],
                       'route':{**ROUTE,'preference_revision':snapshot['revision']},
                       'policy_revision':snapshot['policy_revision'],'plan_revision':'plan',
                       'report_contract':'checks','sources':[]})
        path=objective_root(self.project,'objective')/'context.json';before=path.read_bytes()
        port=FakePort()
        with patch.object(port,'resolve_worktree',side_effect=AssertionError('native placement must not run')), \
             patch.object(port,'capability',side_effect=AssertionError('native capability must not run')), \
             self.assertRaises(PodError) as blocked:
            guarded_start(self.project,'objective',owner='owner',run='foreign',task='task',
                          plan_revision='plan',frozen_packet=frozen,port=port)
        self.assertEqual(blocked.exception.code,'native_authority_unverified')
        self.assertEqual(port.starts,[])
        self.assertEqual(path.read_bytes(),before)

    def test_constraint_and_governor_mutations_require_native_owner_but_reads_do_not(self):
        self.establish()
        path=objective_root(self.project,'objective')/'context.json';before=path.read_bytes()
        missing={'runtime':'runtime','run':None}
        with self.native(first=missing,second=missing),self.assertRaises(PodError) as blocked:
            constraints_update(self.project,'objective',owner='owner',action='add',
                               value={'id':'one','kind':'max_workers','provenance':'user_direct','value':1})
        self.assertEqual(blocked.exception.code,'native_authority_unverified')
        self.assertEqual(path.read_bytes(),before)
        with self.native(first=missing,second=missing),self.assertRaises(PodError) as blocked:
            prepare_candidate(self.project,'objective',owner='owner',unit='release',
                              observation=observation(policy=effective(self.project)['revision']))
        self.assertEqual(blocked.exception.code,'native_authority_unverified')
        self.assertEqual(list(objective_root(self.project,'objective').glob('governor.json')),[])
        self.assertEqual(read(self.project,'objective')['checkpoint']['candidate'],'candidate')

    def test_attempt_and_intervention_mutations_reject_unbound_caller(self):
        self.establish()
        snapshot=load_config(self.project)
        frozen=packet({'schema':'pod-packet/v3','objective':'objective','criteria':['works'],**kernel_binding(),
                       'responsibility':'writer','scope':['notes.txt'],'actions':['edit'],
                       'candidate':'candidate','context':[],'dependencies':[],
                       'route':{**ROUTE,'preference_revision':snapshot['revision']},
                       'policy_revision':snapshot['policy_revision'],'plan_revision':'plan',
                       'report_contract':'checks','sources':[]})
        port=FakePort()
        port.placement={'repository':None,'repo_key':None,'path':str(self.project.resolve()),
                        'branch':None,'runtime':'runtime'}
        with patch('pod.ledger.require_authority',return_value={
                'runtime':'runtime','run_id':'run','references':{'run':'runtime'}}):
            row=guarded_start(self.project,'objective',owner='owner',run='run',task='task',
                              plan_revision='plan',frozen_packet=frozen,port=port)['admission']
        path=objective_root(self.project,'objective')/'context.json';before=path.read_bytes()
        missing={'runtime':'runtime','run':None}
        operations=(
            lambda:update_admission(self.project,'objective',owner='owner',admission_id=row['admission_id'],
                                    update=lambda target:target.update(state='closed')),
            lambda:route_failure(self.project,'objective',owner='owner',admission_id=row['admission_id'],
                                 kind='unavailable',source='native'),
            lambda:check_bound_sources(self.project,'objective',owner='owner',
                                       assignment='packet',sources=[]),
            lambda:intervention(self.project,'objective',owner='owner',task='task',
                                correction={'criterion_id':'works','failure_id':'failure',
                                            'obligation':'fix','failing_example':'bad',
                                            'hypothesis':'cause','last_meaningful_evidence':'test',
                                            'next_discriminating_check':'check','correction_key':'one'}),
        )
        for operation in operations:
            with self.subTest(operation=operation),self.native(first=missing,second=missing), \
                 patch('pod.operations.OrcaPort.show_worker',autospec=True,
                       side_effect=lambda _port,dispatch:port.show_worker(dispatch)), \
                 self.assertRaises(PodError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code,'native_authority_unverified')
            self.assertEqual(path.read_bytes(),before)

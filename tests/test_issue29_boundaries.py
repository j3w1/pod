"""Issue 29 through normal intake, checkpoint, admission, report and wait readers."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from pod.config import effective, load, set_pin
from pod.errors import PodError
from pod.governor import (_check_merge_target, _record_path, _write_journal,
                          objective_delivery_reporting, status)
from pod.internal import run as internal_run
from pod.ledger import _authorization_granted, kernel_view, read
from pod.operations import OrcaPort
from tests import kernel_support as ks


class RequiredAuditTests(ks.KernelCase):
    policy = ks.governance_policy

    def setUp(self):
        super().setUp()
        native_read = patch.object(OrcaPort, "read_native", autospec=True,
            side_effect=lambda _port, owner, **kwargs:self.port.read_native(owner, **kwargs))
        native_read.start(); self.addCleanup(native_read.stop)

    def prepare_delivery(self):
        from pod.governor import prepare_candidate
        tree = ks.git(self.project, 'rev-parse', self.candidate + '^{tree}')
        return prepare_candidate(self.project, 'objective', owner='owner', unit='delivery',
            observation=ks.observation(commit=self.candidate, tree=tree, base=self.base,
                policy=effective(self.project)['revision'], workflows={}, verification=[]),
            branch={'remote':'origin','base':'target','branch':'delivery'})['candidate']

    def delivery_decision(self, binding, kind='merge'):
        from pod.governor import decide
        from pod.ledger import logical_projection
        state = read(self.project, 'objective')
        native = self.port.read_native('owner', authority_runs=('run',),
                                      assignments=tuple(state['admissions'].values()))
        scope = 'merge' if kind=='merge' else 'publish'
        return decide(self.project, 'objective', owner='owner',
            native_projection=logical_projection(self.project,native,objective='objective'),
            action={'kind':kind,'candidate':binding['id'],'unit':'delivery',
                    'target':'origin/target' if kind=='merge' else 'origin/delivery',
                    'reason':'exact fixture delivery', 'effects':[] if kind=='merge' else ['workflow:ci.yml'],
                    'authorization':ks.authorization(candidate=binding['commit'],tree=binding['tree'],
                                                     scope=(scope,),target='origin/target')})

    def finish_audit(self, **packet_fields):
        frozen = self.packet(['PA'], role='review', **packet_fields)
        admitted = self.start('fresh-review',frozen)['admission']
        self.settle(admitted)
        rows = self.stored(); audit = next(row for row in rows if row['id']=='PA')
        audit.pop('executor'); audit.update(state='satisfied',evidence=[{'attempt':admitted['admission_id']}])
        self.report(admitted,frozen,map={'obligations':rows})
        return admitted

    def draft_intake(self):
        draft = internal_run('brief', {'project':str(self.project), 'criteria':['PoD#1'],
            'coverage':[{'criterion':'PoD#1','check':'unit'}],
            'map':{'governance':{'base_ref':'target'},
                   'obligations':[self.criterion(), self.policy()]}})
        bound = draft['map']
        self.write(bound['obligations'], governance=bound['governance'])
        cited = next(row for row in self.stored() if row['id'] == 'PA')
        self.assertEqual(cited['provenance'], 'project_policy')
        self.assertEqual(cited['source']['base'], self.base)
        self.assertEqual(cited['source']['quote_sha256'],
                         hashlib.sha256(ks.AGENTS.splitlines()[2].encode()).hexdigest())

    def acceptance(self):
        return internal_run('acceptance', {'project':str(self.project),'objective':'objective',
            'criteria':['O1'],'evidence_rows':[self.proof('O1')], 'candidate':self.candidate,
            'policy_revision':effective(self.project)['revision'],
            'sources':[], 'dependencies':ks.VERIFICATION['dependencies'], 'environment':ks.VERIFICATION['environment'],
            'review_required':False, 'hosted_required':False,
            'owner_acceptance':{'schema':'pod-acceptance-authorization/v1','candidate':self.candidate,
                'policy_revision':effective(self.project)['revision'],
                'accepted_by':'fixture owner','utc':'2026-09-26T00:00:00Z'}})

    def test_normal_intake_required_audit_cannot_be_omitted_relabelled_or_withdrawn(self):
        self.draft_intake()
        before = read(self.project, 'objective')
        with self.assertRaises(PodError): self.write(self.stored()[:1])
        for field, value in (('provenance','coordinator'), ('kind','subgoal'), ('check','optional advice')):
            rows = deepcopy(self.stored()); rows[1][field] = value
            with self.assertRaises(PodError): self.write(rows)
        rows = self.stored(); rows[1].pop('wait')
        rows[1].update(state='withdrawn',withdrawal={'by':'coordinator','reason':'reviewer unavailable'})
        with self.assertRaises(PodError): self.write(rows)
        self.assertEqual(read(self.project, 'objective'), before)
        result = self.acceptance()
        self.assertFalse(result['accepted'])
        self.assertEqual(result['independently_reviewed'], 'WITHHELD')
        # Closure cannot convert the required review into a non-gate.
        rows = self.stored(); rows[0].pop('executor')
        rows[0].update(state='satisfied',evidence=[self.proof('O1')])
        rows[1]['wait'] = {'class':'authority','referent':{'kind':'permission','need':'fresh reviewer'}}
        self.write(rows)
        with self.assertRaises(PodError): self.write(self.stored(), close=True)

    def test_model_free_policy_pin_report_and_acceptance_preserve_audit(self):
        self.draft_intake()
        pref = load(self.project)
        set_pin(Path(pref['path']), 'gpt-6-sol', displayed=pref)
        from pod.ledger import constraints_update
        constraints_update(self.project, 'objective', owner='owner', action='add', value=
            {'id':'review-route','kind':'role_model','provenance':'repository',
             'role':'writer','value':'claude-opus-5-5'})
        frozen = self.packet(['PA'], role='review')
        admission = self.start('fresh-review', frozen)['admission']
        self.assertEqual(admission['route_decision']['pinned_model'], 'gpt-6-sol')
        self.settle(admission)
        bad = self.stored()[:1]
        with self.assertRaises(PodError): self.report(admission, frozen, map={'obligations':bad})
        self.assertEqual(next(row for row in self.stored() if row['id']=='PA')['provenance'], 'project_policy')
        rows = self.stored(); a = rows[1]; a.pop('executor')
        a.update(state='satisfied',evidence=[{'attempt':admission['admission_id']}])
        self.report(admission, frozen, map={'obligations':rows})
        self.assertEqual(self.acceptance()['independently_reviewed'], 'QUALIFIED')
        self.assertTrue(self.acceptance()['accepted'])

    def test_scoped_user_waiver_is_withdrawal_not_passing_review(self):
        self.draft_intake()
        rows = self.stored(); rows[1].pop('wait')
        rows[1].update(state='withdrawn',withdrawal={'by':'user_direct','reason':'waive this audit'})
        result = self.write(rows, revision_authority={'provenance':'user_direct',
                                                   'instruction':'Waive PA for this objective only'})
        self.assertEqual(kernel_view(self.project, 'objective')['label']['label'], 'WITHHELD')
        self.assertEqual(result['report']['withdrawn'][0]['provenance'], 'project_policy')

    def test_governor_required_audit_holds_merge_but_allows_publication_and_current_proof(self):
        self.draft_intake()
        binding = self.prepare_delivery()
        missing = self.delivery_decision(binding)
        self.assertEqual(missing['decision'],'DEFER')
        self.assertIn('assurance_unbound',[r['code'] for r in missing['reasons']])
        self.assertTrue(all(r['class']=='correctness' for r in missing['reasons']
                            if r['code']=='assurance_unbound'))
        # Same-candidate publication/validation can overlap a still running required review.
        frozen = self.packet(['PA'],role='review')
        admitted = self.start('review',frozen)['admission']
        self.assertEqual(self.delivery_decision(binding,kind='push')['decision'],'ALLOW')
        self.settle(admitted)
        rows = self.stored(); audit = rows[1]; audit.pop('executor')
        audit.update(state='satisfied',evidence=[{'attempt':admitted['admission_id']}])
        self.report(admitted,frozen,map={'obligations':rows})
        self.assertEqual(self.delivery_decision(binding)['decision'],'ALLOW')

    def test_governor_stale_proof_stays_held_until_explicit_scoped_waiver(self):
        self.draft_intake(); self.finish_audit()
        binding = self.prepare_delivery()
        rows = self.stored(); audit = rows[1]; audit.pop('evidence')
        audit.update(state='waiting',wait={'class':'sequenced','referent':'O1'})
        self.write(rows,verification={**ks.VERIFICATION,'environment':'changed fixture'})
        stale = self.delivery_decision(binding)
        self.assertEqual(stale['decision'],'DEFER')
        self.assertIn('assurance_unbound',[r['code'] for r in stale['reasons']])
        rows = self.stored(); rows[1].pop('wait')
        rows[1].update(state='withdrawn',withdrawal={'by':'user_direct','reason':'exact scoped waiver'})
        self.write(rows,verification={**ks.VERIFICATION,'environment':'changed fixture'},
                   revision_authority={'provenance':'user_direct','instruction':'Waive PA for this candidate'})
        self.assertEqual(self.delivery_decision(binding)['decision'],'ALLOW')

    def test_governor_genuinely_removed_trusted_audit_can_be_withdrawn(self):
        self.draft_intake()
        # Advance only the independently selected target, without candidate ancestry.
        ks.git(self.project,'checkout','-q','target')
        (self.project/'AGENTS.md').write_text('# Project rules\nDocs are optional.\n')
        ks.git(self.project,'commit','-q','-am','trusted policy removal')
        ks.git(self.project,'update-ref','refs/remotes/origin/target','target')
        ks.git(self.project,'checkout','-q','main')
        self.write(self.stored(),governance_refresh=True)
        self.assertTrue(self.stored()[1]['source']['gone'])
        rows = self.stored(); rows[1].pop('wait')
        rows[1].update(state='withdrawn',withdrawal={'by':'project_policy','reason':'trusted source gone'})
        self.write(rows)
        self.assertEqual(self.delivery_decision(self.prepare_delivery())['decision'],'ALLOW')

    def test_governor_current_receipt_with_changed_source_is_invalidated(self):
        from pod.records import source_identity
        self.draft_intake()
        self.finish_audit(sources=[source_identity(self.project,'src/old.py')])
        binding = self.prepare_delivery()
        # The immutable receipt stays recorded; current source validity controls its use.
        (self.project/'src/old.py').write_text('changed source\n')
        invalid = self.delivery_decision(binding)
        self.assertEqual(invalid['decision'],'DEFER')
        gap = next(r for r in invalid['reasons'] if r['code']=='assurance_unbound')
        self.assertIn('invalidated',gap['detail'])
        self.assertEqual(self.stored()[1]['state'],'satisfied')
        self.assertEqual(len(self.stored()[1]['receipts']),1)

    def test_governor_optional_coordinator_assurance_is_not_a_new_release_gate(self):
        optional = self.policy('optional'); optional.pop('source')
        optional.update(provenance='coordinator',parent='O1')
        self.intake(optional)
        self.assertEqual(self.delivery_decision(self.prepare_delivery())['decision'],'ALLOW')


class PinRoleBoundaryTests(ks.KernelCase):
    def admitted_role(self, role):
        row = (ks.governance_policy(self) if role == 'review' else self.sub('S'))
        self.intake(row)
        pref = load(self.project)
        set_pin(Path(pref['path']), 'gpt-6-sol', displayed=pref)
        extras = {'resolves':'bounded question','stop_condition':'one answer'} if role == 'investigate' else {}
        frozen = self.packet([row['id']], role=role, **extras)
        admitted = self.start(role, frozen)['admission']
        self.assertEqual(admitted['request']['model'], 'gpt-6-sol')
        self.assertEqual(admitted['request']['effort'], 'medium')
        self.assertEqual(admitted['role'], role)
        self.assertEqual(admitted['route_decision']['pinned_model'], 'gpt-6-sol')

    def test_implementation_role_uses_pin(self): self.admitted_role('implement')
    def test_investigation_role_uses_pin(self): self.admitted_role('investigate')
    def test_review_role_uses_pin(self): self.admitted_role('review')


class LegacyConsentTests(ks.KernelCase):
    def journal(self, scenario):
        return json.loads((Path(__file__).parent/'fixtures/pod-0.6.4/merge-journals.json').read_text())[scenario]

    def test_released_writer_records_leave_real_checkpoint_authority_wait_applicable(self):
        self.intake()
        for scenario in ('A_prepared_unit_no_branch','B_default_unit_checkpoint_only','C_control_bound_branch'):
            with self.subTest(scenario=scenario):
                journal = self.journal(scenario)
                _write_journal(_record_path(self.project,'objective'), journal)
                reported = objective_delivery_reporting(self.project,'objective')
                for unit in reported['units'].values(): self.assertEqual(unit['merge'], 'MISSING')
                for unit in status(self.project,'objective')['units'].values():
                    self.assertEqual(unit['authorization']['merge'], 'MISSING')
                candidates = {ks.COMMIT} | {r['action']['candidate'] for r in journal['actions']}
                for candidate in candidates:
                    self.assertFalse(_authorization_granted(self.project,'objective')('merge',candidate))
                    rows = self.stored(); rows[0].pop('executor',None)
                    rows[0].update(state='waiting',wait={'class':'authority','referent':{
                        'kind':'authorization','scope':'merge','candidate':candidate}})
                    self.write(rows)
                    self.assertEqual(self.stored()[0]['wait']['referent']['candidate'], candidate)
                reasons = []
                _check_merge_target({'kind':'merge','target':'main'}, next(iter(journal['units'].values()), {}), reasons)
                if scenario != 'C_control_bound_branch':
                    self.assertIn('unit_unbound', [r['code'] for r in reasons])

    def test_synthetic_current_consent_controls_share_status_and_wait_truth(self):
        # These current-target controls derive from the captured row; they are synthetic,
        # not a claim that 0.6.4 wrote target-bearing consent.
        self.intake()
        for change, granted in (('valid',True), ('absent',False), ('wrong-target',False),
                                ('stale-tree',False), ('stale-commit',False), ('unbound',False),
                                ('unit-moved',False)):
            with self.subTest(change=change):
                journal = deepcopy(self.journal('C_control_bound_branch'))
                row = journal['actions'][0]
                auth = row['action']['authorization']; auth['target'] = 'origin/main'
                if change == 'absent': row['action']['authorization'] = None
                if change == 'wrong-target': auth['target'] = 'origin/other'
                if change == 'stale-tree': auth['tree'] = 'd'*40
                if change == 'stale-commit': auth['candidate'] = 'b'*40
                if change == 'unbound': journal['units']['release']['branch'] = None
                if change == 'unit-moved':
                    journal['units']['release']['candidate']['commit'] = 'b'*40
                    journal['units']['release']['candidate']['tree'] = 'd'*40
                _write_journal(_record_path(self.project,'objective'), journal)
                self.assertEqual(objective_delivery_reporting(self.project,'objective')['units']['release']['merge'],
                                 'RECORDED' if granted else 'MISSING')
                self.assertEqual(_authorization_granted(self.project,'objective')('merge', ks.COMMIT), granted)
                rows = self.stored(); rows[0].pop('executor',None)
                rows[0].update(state='waiting',wait={'class':'authority','referent':{
                    'kind':'authorization','scope':'merge','candidate':ks.COMMIT}})
                if granted:
                    self.refused('wait_invalid','resolved_referent',self.write,rows)
                else: self.write(rows)

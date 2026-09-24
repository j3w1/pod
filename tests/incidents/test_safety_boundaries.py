import unittest

from pod.config import load, write_defaults
from pod.errors import PodError
from pod.records import source_identity
from pod.internal import run
from pod.selection import validate_choice
from tests.common import fixture


class SafetyBoundaryIncidents(unittest.TestCase):
    def test_parent_symlink_and_secret_source_refused(self):
        with fixture() as root:
            outside=root/'outside'; outside.mkdir(); (outside/'data').write_text('private')
            project=root/'project'; project.mkdir()
            (project/'redirect').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(PodError): source_identity(project,'redirect/data')
            (project/'.env').write_text('secret')
            with self.assertRaises(PodError): source_identity(project,'.env')

    def test_context_control_cannot_be_invented_by_a_choice(self):
        with fixture() as root:
            path=root/'pod'/'config.yaml'; write_defaults(path)
            snapshot=load(personal=path)
            choice={'agent':'codex','model':'gpt-6-sol','effort':'medium',
                    'context':'max','reason':'bounded worker'}
            self.assertEqual(validate_choice(snapshot,[],[],choice)['code'],'context_unsupported')
            choice['context']='native_default'
            self.assertTrue(validate_choice(snapshot,[],[],choice)['allowed'])

    def test_caller_json_cannot_claim_native_capability(self):
        base={'project':'.','objective':'o','owner':'t','run':'r','task':'t',
              'plan_revision':'p','packet':{}}
        for forbidden in ('capabilities','assurance','establishment','account'):
            with self.subTest(forbidden=forbidden), self.assertRaises(PodError) as caught:
                run('admission',{**base,forbidden:{'launch_preferences_v1':True}})
            self.assertEqual(caught.exception.code,'invalid_request')

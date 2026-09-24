from copy import deepcopy
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pod.catalog import IDS
from pod.config import DEFAULT, effective, load, personal_path, read_yaml, set_mode, set_model, write_defaults
from pod.errors import PodError
from pod.ledger import state_root
from pod.util import native_home
from tests.common import fixture


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixture()
        self.root = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.path = self.root / 'pod' / 'config.yaml'

    def test_missing_invalid_and_partial_files_never_enable_delegation(self):
        self.assertEqual(load(personal=self.path)['eligible'], [])
        self.path.parent.mkdir()
        for body in ('schema: pod/v1\nmodels: {}\n',
                     'schema: pod/v1\nselection: custom\nmodels: {gpt-6-sol: invalid}\nworkers: {max_active: 2}\n',
                     'schema: pod/v1\nmodels: {sol: {approved: true}}\nselection: all\nworkers: {max_active: 2}\n',
                     'schema: pod/v1\nmodels: {gpt-6-sol: available}\nselection: all\nworkers: {max_active: 2}\n',
                     'schema: pod/v1\nselection: custom\nmodels: [bad]\nworkers: {max_active: 2}\n'):
            with self.subTest(body=body):
                self.path.write_text(body)
                result = load(personal=self.path)
                self.assertEqual(result['eligible'], [])
                self.assertTrue(result['errors'])
                with self.assertRaises(PodError):
                    set_mode(self.path, 'all', displayed=result)
        self.path.write_text('schema: pod/v1\nmodels: {sol: {approved: true}}\n'
                             'routing: {complex: {model: sol}}\n')
        self.assertIn('models.sol', load(personal=self.path)['errors'][0]['message'])

    def test_defaults_and_three_state_toggle_round_trip_across_restart(self):
        write_defaults(self.path)
        current = load(personal=self.path)
        self.assertEqual(current['eligible'], list(IDS))
        self.assertEqual(current['max_active'], 2)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        set_model(self.path, 'gpt-6-sol', 'preferred', displayed=current)
        set_model(self.path, 'gpt-6-luna', 'disabled', displayed=load(personal=self.path))
        custom = load(personal=self.path)
        self.assertEqual(len(custom['eligible']), 5)
        set_mode(self.path, 'all', displayed=custom)
        all_state = load(personal=self.path)
        self.assertEqual(all_state['effective']['gpt-6-luna'], 'available')
        self.assertEqual(all_state['saved']['gpt-6-sol'], 'preferred')
        set_mode(self.path, 'custom', displayed=all_state)
        restored = load(personal=self.path)
        self.assertEqual(restored['saved'], custom['saved'])
        self.assertEqual(restored['eligible'], custom['eligible'])
        set_mode(self.path, 'all', displayed=restored)
        notice = set_model(self.path, 'gpt-6-luna', 'preferred', displayed=load(personal=self.path))
        self.assertEqual(notice['mode_notice'], 'Returned to My selection')
        self.assertEqual(load(personal=self.path)['saved']['gpt-6-sol'], 'preferred')

    def test_external_edits_preserve_other_keys_and_comments(self):
        write_defaults(self.path)
        original = self.path.read_text().replace('  gpt-6-luna: available', '  gpt-6-luna: available # manual')
        self.path.write_text(original)
        displayed = load(personal=self.path)
        changed = original.replace('  gpt-6-sol: available', '  gpt-6-sol: preferred')
        self.path.write_text(changed)
        saved = set_model(self.path, 'gpt-6-luna', 'disabled', displayed=displayed)
        self.assertEqual(saved['notice'], '')
        self.assertIn('  gpt-6-sol: preferred', self.path.read_text())
        self.assertIn('  gpt-6-luna: disabled # manual', self.path.read_text())
        with self.assertRaises(PodError) as caught:
            set_model(self.path, 'gpt-6-sol', 'disabled', displayed=displayed)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')

    def test_save_failure_or_invalid_external_edit_preserves_bytes(self):
        write_defaults(self.path)
        shown = load(personal=self.path)
        self.path.write_text('schema: pod/v1\nselection: custom\nmodels: {sol: available}\nworkers: {max_active: 2}\n')
        before = self.path.read_bytes()
        with self.assertRaises(PodError):
            set_model(self.path, 'gpt-6-sol', 'preferred', displayed=shown)
        self.assertEqual(self.path.read_bytes(), before)
        write_path = self.root / 'file' / 'config.yaml'
        (self.root / 'file').write_text('occupied')
        with self.assertRaises(OSError):
            write_defaults(write_path)

    def test_safe_yaml_and_project_scope_are_strict(self):
        self.path.parent.mkdir()
        for body in ('schema: pod/v1\nschema: pod/v1\n',
                     'schema: !!python/object/apply:os.system [echo hi]\n',
                     'schema: pod/v1\nselection: custom\nmodels: {gpt-6-sol: available}\nworkers: {max_active: true}\n',
                     'schema: pod/v1\nselection: custom\nmodels: {gpt-6-sol: &a available, gpt-6-luna: *a}\nworkers: {max_active: 2}\n'):
            self.path.write_text(body)
            with self.assertRaises(PodError):
                read_yaml(self.path)
        project = self.root / 'project'
        project.mkdir()
        local = project / '.pod' / 'config.yaml'
        local.parent.mkdir()
        local.write_text('schema: pod/v1\nmodels: {gpt-6-sol: available}\n')
        with self.assertRaises(PodError):
            effective(project, personal=self.path)
        local.write_text('schema: pod/v1\nwaste_governor:\n'
                         '  triggers: {push: [{bad: type}]}\n')
        with self.assertRaises(PodError):
            effective(project, personal=self.path)

    def test_governor_revision_is_independent_of_model_edits(self):
        project = self.root / 'project'
        project.mkdir()
        write_defaults(self.path)
        first = effective(project, personal=self.path)
        set_model(self.path, 'gpt-6-sol', 'preferred', displayed=load(personal=self.path))
        second = effective(project, personal=self.path)
        self.assertEqual(first['revision'], second['revision'])
        self.assertNotEqual(first['preference_revision'], second['preference_revision'])
        local = project / '.pod' / 'config.yaml'
        local.parent.mkdir()
        local.write_text('schema: pod/v1\nwaste_governor:\n  preflight: [unit]\n')
        third = effective(project, personal=self.path)
        self.assertNotEqual(second['revision'], third['revision'])

    def test_project_preflight_and_trigger_layers_cannot_remove_personal_rules(self):
        project=self.root/'project'; project.mkdir()
        write_defaults(self.path)
        document=read_yaml(self.path)
        document['waste_governor']={'preflight':['personal-check'],
                                     'triggers':{'push':['workflow:personal.yml']}}
        import yaml
        self.path.write_text(yaml.safe_dump(document,sort_keys=False))
        local=project/'.pod'/'config.yaml'; local.parent.mkdir()
        local.write_text('schema: pod/v1\nwaste_governor:\n'
                         '  preflight: [project-check]\n'
                         '  triggers: {push: ["workflow:project.yml"]}\n')
        governed=effective(project,personal=self.path)['policy']['waste_governor']
        self.assertEqual(governed['preflight'],['personal-check','project-check'])
        self.assertEqual(governed['triggers']['push'],
                         ['workflow:personal.yml','workflow:project.yml'])
        local.write_text('schema: pod/v1\nwaste_governor:\n'
                         '  preflight: []\n  triggers: {push: []}\n')
        guarded=effective(project,personal=self.path)['policy']['waste_governor']
        self.assertEqual(guarded['preflight'],['personal-check'])
        self.assertEqual(guarded['triggers']['push'],['workflow:personal.yml'])

    def test_sparse_custom_map_is_valid_but_omissions_are_ineligible(self):
        self.path.parent.mkdir()
        self.path.write_text('schema: pod/v1\nselection: custom\n'
                             'models: {gpt-6-sol: available}\nworkers: {max_active: 2}\n')
        view=load(personal=self.path)
        self.assertEqual(view['errors'],[])
        self.assertEqual(view['eligible'],['gpt-6-sol'])
        self.assertEqual(set(view['not_set']),set(IDS)-{'gpt-6-sol'})

    def test_project_cannot_expand_governor_authority(self):
        project = self.root / 'project'
        project.mkdir()
        write_defaults(self.path)
        local = project / '.pod' / 'config.yaml'
        local.parent.mkdir()
        for value in ('mode: observe', 'transient_retries: 3', 'host_control: claimed',
                      'exceptions: [{id: fake}]'):
            with self.subTest(value=value):
                local.write_text('schema: pod/v1\nwaste_governor:\n  '+value+'\n')
                with self.assertRaises(PodError):
                    effective(project, personal=self.path)

    def test_unreadable_project_policy_is_not_treated_as_absent(self):
        project=self.root/'project';project.mkdir()
        write_defaults(self.path)
        folder=project/'.pod';folder.mkdir()
        (folder/'config.yaml').write_text('schema: pod/v1\nwaste_governor:\n'
                                          '  transient_retries: 0\n')
        self.assertEqual(effective(project,personal=self.path)['policy']['waste_governor']['transient_retries'],0)
        folder.chmod(0)
        try:
            with self.assertRaises(PodError) as unavailable:
                effective(project,personal=self.path)
            self.assertEqual(unavailable.exception.code,'unsafe_config')
        finally:
            folder.chmod(0o700)

    def test_running_from_home_skips_cwd_containment_outside_git(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / '.config')}):
            with patch('pathlib.Path.cwd', return_value=self.root):
                self.assertEqual(personal_path(), self.root / '.config' / 'pod' / 'config.yaml')

    def test_invalid_pod_home_overrides_do_not_resolve_or_create_paths(self):
        for name,resolve in (('POD_CONFIG_HOME',personal_path),('POD_STATE_HOME',state_root)):
            for value in ('','.','relative/path'):
                with self.subTest(name=name,value=value),patch.dict(os.environ,{name:value}), \
                     self.assertRaises(PodError) as blocked:
                    resolve()
                self.assertEqual(blocked.exception.code,'invalid_location_override')
            file=self.root/f'{name}.txt';file.write_text('not a directory')
            with patch.dict(os.environ,{name:str(file)}),self.assertRaises(PodError):
                resolve()

    def test_native_homes_reject_relative_malformed_and_project_redirects(self):
        project=self.root/'git-project';project.mkdir()
        subprocess.run(['git','init','-q',str(project)],check=True)
        contained=project/'native-home';contained.mkdir()
        redirected=self.root/'redirected-native-home';redirected.symlink_to(contained,target_is_directory=True)
        for name in ('XDG_CONFIG_HOME','XDG_STATE_HOME','CODEX_HOME','CLAUDE_CONFIG_DIR'):
            for value in ('','.','relative/path','bad\x00path'):
                with self.subTest(name=name,value=repr(value)),patch.object(os,'environ',{**os.environ,name:value}), \
                     self.assertRaises(PodError) as blocked:
                    native_home(name,default=self.root/'safe-default',project=project)
                self.assertEqual(blocked.exception.code,'invalid_native_home')
            file=self.root/f'{name}.txt';file.write_text('not a directory')
            with patch.dict(os.environ,{name:str(file)}),self.assertRaises(PodError):
                native_home(name,project=project)
            for value in (contained,redirected):
                with self.subTest(name=name,value=value),patch.dict(os.environ,{name:str(value)}), \
                     self.assertRaises(PodError) as blocked:
                    native_home(name,project=project)
                self.assertEqual(blocked.exception.code,'project_contained_native_home')

    def test_relative_xdg_homes_and_fixture_override_restoration(self):
        with patch.dict(os.environ,{'XDG_CONFIG_HOME':'relative-config',
                                    'XDG_STATE_HOME':'relative-state'}):
            with self.assertRaises(PodError):personal_path()
            with self.assertRaises(PodError):state_root()
        with tempfile.TemporaryDirectory() as directory:
            inherited={'POD_CONFIG_HOME':str(Path(directory)/'inherited-config'),
                       'POD_STATE_HOME':str(Path(directory)/'inherited-state')}
            with patch.dict(os.environ,inherited):
                with fixture() as isolated:
                    self.assertNotIn('POD_CONFIG_HOME',os.environ)
                    self.assertNotIn('POD_STATE_HOME',os.environ)
                    self.assertFalse(Path(inherited['POD_CONFIG_HOME']).exists())
                    self.assertFalse(Path(inherited['POD_STATE_HOME']).exists())
                self.assertEqual({key:os.environ[key] for key in inherited},inherited)

    def test_yaml_include_size_depth_and_type_boundaries(self):
        self.path.parent.mkdir(exist_ok=True)
        variants=(
            'schema: pod/v1\nmodels: {gpt-6-sol: &x available, gpt-6-luna: *x}\n',
            'schema: pod/v1\nmodels: !include secret\n',
            'schema: pod/v1\nworkers: [wrong]\n',
            'a: '+('['*20)+'0'+(']'*20)+'\n',
            'a'*65537,
        )
        for value in variants:
            with self.subTest(value=value[:40]):
                self.path.write_text(value)
                with self.assertRaises(PodError):read_yaml(self.path)

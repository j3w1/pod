from copy import deepcopy
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from pod.catalog import route_keys
from pod.config import (defaults, edit, effective, load, personal_path, read_yaml, set_refresh,
                        set_route, set_routes, write_defaults)
from pod.errors import PodError
from pod.ledger import state_root
from pod.util import native_home
from tests.common import fixture

SOL = 'codex/gpt-6.1-sol/medium'
LUNA = 'codex/gpt-6-luna/low'
OPUS = 'claude/claude-opus-5-5/high'


def v2(routes='{}', extra=''):
    return f'schema: pod/v2\nroutes: {routes}\nworkers: {{max_active: 2}}\n{extra}'


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixture()
        self.root = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.path = self.root / 'pod' / 'config.yaml'

    def test_missing_invalid_and_partial_files_never_enable_delegation(self):
        missing = load(personal=self.path)
        self.assertEqual((missing['eligible'], missing['status']), ([], 'missing'))
        self.path.parent.mkdir()
        for body in ('schema: pod/v2\nroutes: {}\n',
                     v2('{codex/gpt-6.1-sol/medium: invalid}'),
                     v2('{codex/gpt-6-sol/medium: enabled}'),
                     v2('{codex/gpt-6.1-sol/ultra: enabled}'),
                     v2('{codex/gpt-6.1-sol: enabled}'),
                     v2('[bad]'),
                     v2('{codex/gpt-6.1-sol/medium: enabled}', 'refresh: sometimes\n'),
                     v2('{codex/gpt-6.1-sol/medium: enabled}', 'selection: all\n'),
                     'schema: pod/v3\nroutes: {}\nworkers: {max_active: 2}\n'):
            with self.subTest(body=body):
                self.path.write_text(body)
                result = load(personal=self.path)
                self.assertEqual(result['eligible'], [])
                self.assertEqual(result['status'], 'invalid')
                self.assertEqual(result['refresh'], 'manual')
                self.assertTrue(result['errors'])
                before = self.path.read_bytes()
                with self.assertRaises(PodError):
                    set_route(self.path, SOL, 'enabled', displayed=result)
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(v2('{codex/gpt-6-sol/high: enabled}'))
        self.assertIn('routes.codex/gpt-6-sol/high', load(personal=self.path)['errors'][0]['message'])

    def test_defaults_and_route_state_edits_round_trip_across_restart(self):
        write_defaults(self.path)
        current = load(personal=self.path)
        self.assertEqual(current['eligible'], list(route_keys()))
        self.assertEqual((current['max_active'], current['refresh'], current['status']), (2, 'automatic', 'valid'))
        self.assertEqual((current['preferred'], current['pinned'], current['schema']), (None, None, 'pod/v2'))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        set_route(self.path, LUNA, 'disabled', displayed=current)
        set_route(self.path, SOL, None, displayed=load(personal=self.path))
        changed = load(personal=self.path)
        self.assertEqual(len(changed['eligible']), 28)
        self.assertEqual(changed['routes'][LUNA], 'disabled')
        self.assertNotIn(SOL, changed['routes'])
        set_route(self.path, SOL, 'enabled', displayed=changed)
        set_route(self.path, LUNA, 'enabled', displayed=load(personal=self.path))
        self.assertEqual(load(personal=self.path)['eligible'], list(route_keys()))
        with self.assertRaises(PodError) as caught:
            write_defaults(self.path)
        self.assertEqual(caught.exception.code, 'config_exists')

    def test_external_edits_preserve_other_keys_and_comments(self):
        write_defaults(self.path)
        original = self.path.read_text().replace(f'  {LUNA}: enabled', f'  {LUNA}: enabled # manual')
        self.path.write_text('# my routes\n' + original)
        displayed = load(personal=self.path)
        self.path.write_text(self.path.read_text().replace(f'  {SOL}: enabled', f'  {SOL}: disabled'))
        saved = set_route(self.path, LUNA, 'disabled', displayed=displayed)
        self.assertEqual(saved['notice'], '')
        text = self.path.read_text()
        self.assertIn(f'  {SOL}: disabled', text)
        self.assertIn(f'  {LUNA}: disabled # manual', text)
        self.assertTrue(text.startswith('# my routes\n'))
        with self.assertRaises(PodError) as caught:
            set_route(self.path, SOL, 'enabled', displayed=displayed)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')
        self.assertIn(f'  {SOL}: disabled', self.path.read_text())

    def test_save_failure_or_invalid_external_edit_preserves_bytes(self):
        write_defaults(self.path)
        shown = load(personal=self.path)
        self.path.write_text(v2('{codex/gpt-6-sol/high: enabled}'))
        before = self.path.read_bytes()
        with self.assertRaises(PodError):
            set_route(self.path, SOL, 'disabled', displayed=shown)
        self.assertEqual(self.path.read_bytes(), before)
        write_defaults(self.root / 'other' / 'config.yaml')
        other = self.root / 'other' / 'config.yaml'
        shown = load(personal=other)
        original = other.read_bytes()
        with patch('pod.config.os.replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
            set_route(other, SOL, 'disabled', displayed=shown)
        self.assertEqual(other.read_bytes(), original)
        self.assertEqual([path.name for path in other.parent.iterdir() if path.name.startswith('.pod-config-')], [])
        write_path = self.root / 'file' / 'config.yaml'
        (self.root / 'file').write_text('occupied')
        with self.assertRaises(OSError):
            write_defaults(write_path)

    def test_safe_yaml_and_project_scope_are_strict(self):
        self.path.parent.mkdir()
        for body in ('schema: pod/v2\nschema: pod/v2\n',
                     'schema: !!python/object/apply:os.system [echo hi]\n',
                     'schema: pod/v2\nroutes: {}\nworkers: {max_active: true}\n',
                     f'schema: pod/v2\nroutes: {{{SOL}: &a enabled, {LUNA}: *a}}\nworkers: {{max_active: 2}}\n'):
            self.path.write_text(body)
            with self.assertRaises(PodError):
                read_yaml(self.path)
        project = self.root / 'project'
        project.mkdir()
        local = project / '.pod' / 'config.yaml'
        local.parent.mkdir()
        for body in ('schema: pod/v1\nroutes: {codex/gpt-6.1-sol/medium: enabled}\n',
                     'schema: pod/v1\npinned: codex/gpt-6.1-sol/medium\n',
                     'schema: pod/v2\nwaste_governor: {}\n',
                     'schema: pod/v1\nwaste_governor:\n  triggers: {push: [{bad: type}]}\n'):
            with self.subTest(body=body):
                local.write_text(body)
                with self.assertRaises(PodError):
                    effective(project, personal=self.path)

    def test_schema_split_keeps_governor_policy_in_both_files(self):
        project = self.root / 'project'
        project.mkdir()
        (project / '.pod').mkdir()
        (project / '.pod' / 'config.yaml').write_text('schema: pod/v1\nwaste_governor:\n  preflight: [unit]\n')
        document = defaults()
        document['waste_governor'] = {'preflight': ['personal-check']}
        self.path.parent.mkdir()
        self.path.write_text(yaml.safe_dump(document, sort_keys=False))
        policy = effective(project, personal=self.path)
        self.assertEqual(policy['schema'], 'pod/v1')
        self.assertEqual(policy['policy']['waste_governor']['preflight'], ['personal-check', 'unit'])
        self.assertEqual(policy['preferences']['status'], 'valid')
        # A kept 0.6.x personal file stays Governor policy while route setup is pending.
        self.path.write_text('schema: pod/v1\nselection: custom\nmodels: {gpt-6-sol: available}\n'
                             'workers: {max_active: 2}\nwaste_governor:\n  preflight: [personal-check]\n')
        kept = effective(project, personal=self.path)
        self.assertEqual(kept['preferences']['status'], 'setup_required')
        self.assertEqual(kept['preferences']['eligible'], [])
        self.assertEqual(kept['revision'], policy['revision'])

    def test_governor_revision_is_independent_of_route_edits(self):
        project = self.root / 'project'
        project.mkdir()
        write_defaults(self.path)
        first = effective(project, personal=self.path)
        edit(self.path, displayed=load(personal=self.path), routes={SOL: 'disabled'}, refresh='manual')
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

    def test_sparse_and_empty_route_maps_are_valid_but_omissions_are_ineligible(self):
        self.path.parent.mkdir()
        self.path.write_text(v2(f'{{{SOL}: enabled, {LUNA}: disabled}}'))
        view=load(personal=self.path)
        self.assertEqual((view['errors'], view['status']), ([], 'valid'))
        self.assertEqual(view['eligible'],[SOL])
        self.assertEqual(view['routes'], {SOL: 'enabled', LUNA: 'disabled'})
        self.path.write_text(v2('{}'))
        empty=load(personal=self.path)
        self.assertEqual((empty['errors'], empty['eligible'], empty['status']), ([], [], 'valid'))

    def test_bulk_edit_names_exact_routes_in_one_write(self):
        self.path.parent.mkdir()
        self.path.write_text(v2('{}') + '# keep\n')
        shown = load(personal=self.path)
        opus = [key for key in route_keys() if '/claude-opus-5-5/' in key]
        with patch('pod.config._replace', wraps=__import__('pod.config', fromlist=['_replace'])._replace) as writes:
            saved = set_routes(self.path, {key: 'enabled' for key in opus}, displayed=shown)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(saved['changed'], sorted(opus))
        self.assertEqual(load(personal=self.path)['eligible'], opus)
        self.assertIn('# keep', self.path.read_text())
        with self.assertRaises(PodError):
            set_routes(self.path, {}, displayed=load(personal=self.path))
        for bad in ({'codex/*/high': 'enabled'}, {'all': 'enabled'}, {SOL: 'preferred'}):
            with self.subTest(bad=bad), self.assertRaises(PodError) as caught:
                set_routes(self.path, bad, displayed=load(personal=self.path))
            self.assertEqual(caught.exception.code, 'invalid_config_edit')
        # A later registry route is not covered by an earlier bulk action.
        later = 'codex/gpt-6-nova/low'
        with patch('pod.config.route_keys', return_value=(*route_keys(), later)):
            self.assertNotIn(later, load(personal=self.path)['eligible'])

    def test_concurrent_edits_keep_each_targeted_change(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        write_defaults(self.path)
        shown = load(personal=self.path)
        keys = [key for key in route_keys() if '/gpt-6-luna/' in key]
        barrier = threading.Barrier(len(keys))

        def disable(key):
            barrier.wait()
            for _ in range(20):
                try:
                    return set_route(self.path, key, 'disabled', displayed=shown)['changed']
                except PodError as exc:
                    if exc.code != 'config_busy':
                        raise
            raise AssertionError('lock never became available')
        with ThreadPoolExecutor(len(keys)) as pool:
            changed = list(pool.map(disable, keys))
        self.assertEqual(changed, [[key] for key in keys])
        final = load(personal=self.path)
        self.assertTrue(all(final['routes'][key] == 'disabled' for key in keys))
        self.assertEqual(len(final['eligible']), 25)
        # The same targeted route from a stale display is refused, not overwritten.
        with self.assertRaises(PodError) as caught:
            set_route(self.path, keys[0], 'enabled', displayed=shown)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')

    def test_refresh_setting_and_worker_ceiling_edits(self):
        write_defaults(self.path)
        set_refresh(self.path, 'manual', displayed=load(personal=self.path))
        self.assertEqual(load(personal=self.path)['refresh'], 'manual')
        with self.assertRaises(PodError):
            set_refresh(self.path, 'hourly', displayed=load(personal=self.path))
        edit(self.path, displayed=load(personal=self.path), max_active=0)
        view = load(personal=self.path)
        self.assertEqual((view['max_active'], len(view['eligible'])), (0, 30))
        for bad in (9, -1, True, '2'):
            with self.subTest(bad=bad), self.assertRaises(PodError):
                edit(self.path, displayed=view, max_active=bad)
        stale = dict(view, refresh='automatic')
        with self.assertRaises(PodError) as caught:
            set_refresh(self.path, 'automatic', displayed=stale)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')

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
            f'schema: pod/v2\nroutes: {{{SOL}: &x enabled, {LUNA}: *x}}\n',
            'schema: pod/v2\nroutes: !include secret\n',
            'schema: pod/v2\nworkers: [wrong]\n',
            'a: '+('['*20)+'0'+(']'*20)+'\n',
            'a'*65537,
        )
        for value in variants:
            with self.subTest(value=value[:40]):
                self.path.write_text(value)
                with self.assertRaises(PodError):read_yaml(self.path)

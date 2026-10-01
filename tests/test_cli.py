import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.bundle import version
from pod.cli import execute, main, parser
from pod.config import load, write_defaults
from pod.errors import PodError
from pod.placement import inspect
from tests.common import HOST_HOME, HOST_POD_DATA, disposable_path, fixture, modified_bundle, without_module


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.env=patch.dict(os.environ,{'HOME':str(self.root),'XDG_CONFIG_HOME':str(self.root/'config'),
                                     'XDG_DATA_HOME':str(self.root/'data'),
                                     'XDG_STATE_HOME':str(self.root/'state'),
                                     'CODEX_HOME':str(self.root/'other-codex'),
                                     'CLAUDE_CONFIG_DIR':str(self.root/'other-claude'),
                                     'PATH':disposable_path(self.root)})
        self.env.__enter__(); self.addCleanup(self.env.__exit__,None,None,None)
        self.project=self.root/'project'; self.project.mkdir()

    def test_minimal_public_surface_and_version(self):
        help_text=parser().format_help()
        for family in ('config','doctor','status','update','models'): self.assertIn(family,help_text)
        for obsolete in ('setup','approve','revoke'): self.assertNotIn(obsolete,help_text)
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as versioned:
            parser().parse_args(['--version'])
        self.assertEqual(versioned.exception.code,0)
        # `pod update` stays software-only; model data refreshes only through `pod models refresh`.
        for argv in (['config','approve'],['update','models'],['models','update'],['models','refresh','--all']):
            with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(argv)
        self.assertEqual(parser().parse_args(['models','refresh','--check','--json']).check,True)

    def test_config_summary_and_invalid_file_exit_honestly(self):
        args=parser().parse_args(['config','--json'])
        missing=execute(args,self.project)
        self.assertEqual((missing['status'],missing['preference_status']),('invalid','missing'))
        self.assertEqual(missing['eligible'],[])
        path=self.root/'config'/'pod'/'config.yaml'; write_defaults(path)
        valid=execute(args,self.project)
        self.assertEqual((valid['status'],valid['schema'],valid['preference_schema']),('valid','pod-cli/v4','pod/v2'))
        self.assertEqual(valid['path'],str(path.resolve()))
        self.assertEqual(len(valid['eligible']),30)
        self.assertEqual(len(valid['routes']),30)
        self.assertEqual(set(valid['routes'][0]),{'key','agent','model','effort','state','preferred','pinned'})
        self.assertEqual([row['id'] for row in valid['models']],
                         ['claude-opus-5-5','claude-fable-5-1','claude-sonnet-5-5','gpt-6-astra','gpt-6.1-sol','gpt-6-luna'])
        self.assertTrue(all(row['guide']['efforts'] and row['guide']['use'] for row in valid['models']))
        self.assertEqual(valid['summary']['enabled'],30)
        self.assertEqual(valid['observations'],{'status':'not_read','command':'pod models --json'})
        for removed in ('catalog','mode','saved','not_set','pinned_model','ranks_of_six'):
            self.assertNotIn(removed,valid)
        path.write_text('schema: pod/v1\nselection: all\nmodels: {gpt-6-sol: available}\n')
        self.assertEqual(execute(args,self.project)['eligible'],[])
        path.write_text('schema: pod/v1\nselection: all\nmodels: {gpt-6-sol: available}\nworkers: {max_active: 3}\n')
        setup=execute(args,self.project)
        self.assertEqual((setup['status'],setup['eligible'],setup['setup']['from_schema']),
                         ('setup_required',[],'pod/v1'))
        output=StringIO()
        with patch('pathlib.Path.cwd',return_value=self.project), redirect_stdout(output):
            self.assertEqual(main(['config']),1)
        self.assertIn('need route setup',output.getvalue())
        self.assertIn('confirm the route setup',output.getvalue())

    def test_bare_non_tty_summary_uses_validity_exit_code(self):
        output=StringIO()
        with patch('pathlib.Path.cwd',return_value=self.project), redirect_stdout(output):
            self.assertEqual(main([]),1)
        self.assertIn('need attention',output.getvalue())
        write_defaults(self.root/'config'/'pod'/'config.yaml')
        for argv in ([],['models']):
            output=StringIO()
            with self.subTest(argv=argv), patch('pathlib.Path.cwd',return_value=self.project), redirect_stdout(output):
                self.assertEqual(main(argv),0)
            text=output.getvalue()
            self.assertIn('30 supported, 30 enabled',text)
            self.assertIn('codex/gpt-6.1-sol/xhigh',text)
            self.assertIn('bundled snapshot',text)
            self.assertIn('AA metrics are informational',text)

    def test_config_edit_creates_defaults_and_preserves_invalid_manual_change(self):
        path=self.root/'config'/'pod'/'config.yaml'
        script=self.root/'edit.py'
        script.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text("invalid: [\\n")\n')
        with patch.dict(os.environ,{'EDITOR':f'{sys.executable} {script}','VISUAL':''}):
            result=execute(parser().parse_args(['config','edit']),self.project)
        self.assertEqual(result['status'],'invalid')
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(),'invalid: [\n')
        self.assertEqual(load(personal=path)['eligible'],[])

    def test_doctor_reads_no_accounts_or_models(self):
        with patch('pod.cli.contract',return_value={'status':'observed','runtime':'r',
                'capabilities':{'launch_preferences_v1':True}}), \
             patch('pod.orca.read_command') as native:
            report=execute(parser().parse_args(['doctor','--json']),self.project)
        self.assertEqual(report['status'],'observed')
        self.assertEqual(report['native_probe'],'not_run')
        self.assertEqual(report['installation'],'not installed by the one-shot installer')
        native.assert_not_called()

    def test_config_status_and_doctor_expose_valid_and_invalid_pins_passively(self):
        from pod.config import defaults
        import yaml
        path = self.root/'config'/'pod'/'config.yaml'
        path.parent.mkdir(parents=True)
        sol = 'codex/gpt-6.1-sol/high'
        for pin, disabled in [(sol, False), (sol, True), ('unknown', False), ([], False)]:
            with self.subTest(pin=pin, disabled=disabled):
                document = {**defaults(), 'pinned': pin, 'preferred': 'claude/claude-opus-5-5/high'}
                if disabled:
                    document['routes'][sol] = 'disabled'
                path.write_text(yaml.safe_dump(document))
                original = path.read_bytes()
                with patch('pod.cli.contract', return_value={'status': 'observed', 'capabilities': {}}), \
                     patch('pod.cli.current_run', return_value={'run': None}), \
                     patch('pod.orca.read_command') as native:
                    config = execute(parser().parse_args(['config', '--json']), self.project)
                    status = execute(parser().parse_args(['status', '--json']), self.project)
                    doctor = execute(parser().parse_args(['doctor', '--json']), self.project)
                valid = pin == sol and not disabled
                expected = '<invalid list pin>' if isinstance(pin, list) else pin
                for report in (config, status['preferences'], doctor['preferences']):
                    self.assertEqual(report['pinned'], pin if valid else None)
                    self.assertEqual(bool(report['errors']), not valid)
                    self.assertEqual(report['eligible'] == [], not valid)
                for report in (config, doctor['preferences']):
                    self.assertEqual(report['pin_diagnostic'], None if valid else expected)
                    self.assertEqual(report['preferred'], 'claude/claude-opus-5-5/high' if valid else None)
                self.assertEqual(status['routes']['pinned'], pin if valid else None)
                # An invalid file has no trusted saved map, so the view shows no enabled route.
                self.assertEqual(doctor['routes']['enabled'], 30 if valid else 0)
                native.assert_not_called()
                self.assertEqual(path.read_bytes(), original)

    def test_route_setup_is_named_by_config_status_and_doctor(self):
        path = self.root/'config'/'pod'/'config.yaml'
        path.parent.mkdir(parents=True)
        path.write_text('schema: pod/v1\nselection: custom\nmodels: {gpt-6-sol: preferred}\n'
                        'workers: {max_active: 2}\npinned_model: gpt-6-sol\n')
        original = path.read_bytes()
        with patch('pod.cli.contract', return_value={'status': 'unavailable'}), \
             patch('pod.cli.worker_rows', return_value={'workers': [], 'scope': None, 'complete': True}):
            config = execute(parser().parse_args(['config', '--json']), self.project)
            status = execute(parser().parse_args(['status', '--run', 'run', '--json']), self.project)
            doctor = execute(parser().parse_args(['doctor', '--json']), self.project)
        self.assertEqual(config['status'], 'setup_required')
        self.assertEqual(config['pin_diagnostic'], 'gpt-6-sol')
        self.assertEqual(status['preferences']['status'], 'setup_required')
        self.assertEqual(status['blocker'], 'route_setup_required')
        self.assertIn('confirm the route setup', status['next_safe_action'])
        self.assertEqual(doctor['preferences']['setup']['from_schema'], 'pod/v1')
        output = StringIO()
        with patch('pathlib.Path.cwd', return_value=self.project), redirect_stdout(output), \
             patch('pod.cli.contract', return_value={'status': 'unavailable'}):
            self.assertEqual(main(['doctor']), 0)
        self.assertIn('Preferences: setup required', output.getvalue())
        self.assertIn('confirm the route setup', output.getvalue())
        self.assertEqual(path.read_bytes(), original)

    def test_models_commands_delegate_to_observations_and_report_honestly(self):
        write_defaults(self.root/'config'/'pod'/'config.yaml')
        projection = execute(parser().parse_args(['models', '--json']), self.project)['projection']
        self.assertEqual(projection['schema'], 'pod-routes/v1')
        self.assertEqual(projection['observations']['origin'], 'bundled')
        self.assertEqual(projection['native'], {'access': 'unknown'})
        self.assertTrue(all(row['native'] == {'access': 'unknown'} for row in projection['routes']))
        for name in ('rank', 'score', 'recommended'):
            self.assertNotIn(name, json.dumps(projection))
        output = StringIO()
        with patch('pathlib.Path.cwd', return_value=self.project), redirect_stdout(output):
            self.assertEqual(main(['models', '--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['schema'], 'pod-routes/v1')
        outcome = {'outcome': 'checked', 'check': True, 'promoted': False, 'generation': None,
                   'base_generation': '1', 'diff': None, 'diagnostics': [],
                   'sources': {'artificial_analysis': {'status': 'ok', 'rows': 53, 'mapped': 37,
                                                       'diagnostics': ['\x1b[31mred']}}}
        for result, code in ((outcome, 0), ({**outcome, 'outcome': 'promoted', 'check': False}, 0),
                             ({**outcome, 'outcome': 'refused'}, 1), ({**outcome, 'outcome': 'failed'}, 1)):
            output = StringIO()
            with self.subTest(outcome=result['outcome']), patch('pod.observations.refresh', return_value=result) as refresh, \
                 patch('pathlib.Path.cwd', return_value=self.project), redirect_stdout(output):
                self.assertEqual(main(['models', 'refresh', '--check']), code)
            refresh.assert_called_once_with(check=True)
            self.assertIn(f"refresh: {result['outcome']}", output.getvalue())
            self.assertNotIn('\x1b', output.getvalue())
        output = StringIO()
        with patch('pod.observations.refresh', side_effect=PodError('observations_busy', 'busy')), \
             patch('pathlib.Path.cwd', return_value=self.project), redirect_stdout(output):
            self.assertEqual(main(['models', 'refresh', '--json']), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {'schema': 'pod-cli/v4', 'status': 'blocked',
                          'error': {'code': 'observations_busy', 'message': 'busy'}})
        from pod.config import load as load_config, set_refresh
        set_refresh(Path(load_config(self.project)['path']), 'manual', displayed=load_config(self.project))
        with patch('pod.observations.status', wraps=__import__('pod.observations', fromlist=['status']).status) as status:
            report = execute(parser().parse_args(['models', 'status', '--json']), self.project)
        status.assert_called_once_with(setting='manual')
        self.assertEqual(report['observations']['auto_refresh']['setting'], 'manual')
        self.assertFalse(report['observations']['auto_refresh']['due'])
        with without_module('pod.observations'):
            for argv in (['models', '--json'], ['models', 'status', '--json'], ['models', 'refresh', '--json']):
                output = StringIO()
                with self.subTest(argv=argv), patch('pathlib.Path.cwd', return_value=self.project), \
                     redirect_stdout(output):
                    self.assertEqual(main(argv), 1)
                self.assertEqual(json.loads(output.getvalue())['error']['code'], 'observations_unavailable')

    def test_config_status_doctor_and_route_views_make_no_network_call(self):
        import socket
        import urllib.request
        write_defaults(self.root/'config'/'pod'/'config.yaml')
        guard = AssertionError('network access')
        with patch.object(socket.socket, 'connect', side_effect=guard), \
             patch.object(socket, 'create_connection', side_effect=guard), \
             patch.object(urllib.request, 'urlopen', side_effect=guard), \
             patch('pod.observations.refresh', side_effect=guard), \
             patch('pod.cli.contract', return_value={'status': 'unavailable'}), \
             patch('pod.cli.current_run', return_value={'run': None}), \
             patch('pathlib.Path.cwd', return_value=self.project):
            for argv in (['config', '--json'], ['status', '--json'], ['doctor', '--json'], ['models', '--json'],
                         ['models', 'status', '--json'], [], ['config'], ['doctor'], ['models']):
                with self.subTest(argv=argv), redirect_stdout(StringIO()):
                    self.assertIn(main(argv), (0, 1))

    def test_disposable_doctor_paths_never_resolve_into_host_pod_data(self):
        from pod.installer import receipt_path
        receipt=receipt_path().resolve(strict=False)
        self.assertFalse(receipt.is_relative_to(HOST_POD_DATA))
        self.assertTrue(receipt.is_relative_to(self.root))
        self.assertNotIn(str(HOST_HOME / '.local/bin'),os.environ['PATH'].split(os.pathsep))

    def test_doctor_shows_same_version_bundle_drift_and_both_identities(self):
        from pod.bundle import bundle_root, running_identity
        original=running_identity()
        canonical=self.root/'.agents/skills/pod'
        canonical.parent.mkdir(parents=True)
        shutil.copytree(bundle_root(),canonical,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        receipt=self.root/'data/pod/install.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'schema':'pod-install/v1','status':'installed',
                                       'previous':None,'target':{'version':original['version'],
                                                                 'digest':original['bundle_digest']}}))
        changed=modified_bundle(self.root)
        with patch('pod.cli.contract',return_value={'status':'unavailable'}):
            same=execute(parser().parse_args(['doctor','--json']),self.project)
        self.assertFalse(same['installed_version_drift'])
        with patch('pod.bundle.bundle_root',return_value=changed), \
             patch('pod.cli.contract',return_value={'status':'unavailable'}):
            drift=execute(parser().parse_args(['doctor','--json']),self.project)
            output=StringIO()
            with patch('pathlib.Path.cwd',return_value=self.project),redirect_stdout(output):
                self.assertEqual(main(['doctor']),0)
        self.assertTrue(drift['installed_version_drift'])
        self.assertEqual(drift['bundle_identity']['receipt'],original)
        self.assertEqual(drift['bundle_identity']['running']['version'],original['version'])
        self.assertNotEqual(drift['bundle_identity']['running']['bundle_digest'],original['bundle_digest'])
        self.assertIn(original['bundle_digest'][:12],output.getvalue())
        self.assertIn(drift['bundle_identity']['running']['bundle_digest'][:12],output.getvalue())
        self.assertIn('Bundle drift',output.getvalue())

    def test_doctor_is_passive_and_reports_unsupported_state_objective_scoped(self):
        from pod.ledger import _path
        preferences=self.root/'config'/'pod'/'config.yaml';write_defaults(preferences)
        unsupported=_path(self.project,'other');unsupported.parent.mkdir(parents=True)
        data=b'{"schema":"other"}\n';unsupported.write_bytes(data)
        original=preferences.read_bytes()
        with patch('pod.cli.contract',return_value={'status':'unavailable','reason':'offline'}), \
             patch('pod.orca.mutate_command',side_effect=AssertionError('no native mutation')):
            doctor=execute(parser().parse_args(['doctor','--json']),self.project)
        self.assertEqual(doctor['state']['unsupported'],1)
        self.assertEqual(doctor['state']['scope'],'affected_objectives_only')
        self.assertFalse(doctor['state']['blocks_unrelated_objectives'])
        self.assertEqual(preferences.read_bytes(),original)
        self.assertEqual(unsupported.read_bytes(),data)

    def test_version_only_orca_status_is_unavailable_in_json_and_human_doctor(self):
        from pod.errors import PodError
        version={'version':'1.4.209','executable':'/fixture/orca'}
        with patch('pod.orca.read_command',side_effect=[version,PodError('orca_read_failed','offline')]):
            observed=execute(parser().parse_args(['doctor','--json']),self.project)
        self.assertEqual(observed['orca']['status'],'unavailable')
        self.assertEqual(observed['orca']['reason'],'orca_read_failed')
        output=StringIO()
        with patch('pathlib.Path.cwd',return_value=self.project), \
             patch('pod.orca.read_command',side_effect=[version,PodError('orca_read_failed','offline')]), \
             redirect_stdout(output):
            self.assertEqual(main(['doctor']),0)
        self.assertIn('Orca: unavailable',output.getvalue())

    def test_sparse_route_map_is_reported_explicitly_in_reads(self):
        path=self.root/'config'/'pod'/'config.yaml';path.parent.mkdir(parents=True)
        path.write_text('schema: pod/v2\nroutes: {codex/gpt-6.1-sol/high: enabled, codex/gpt-6-luna/low: disabled}\n'
                        'workers: {max_active: 2}\n')
        config=execute(parser().parse_args(['config','--json']),self.project)
        self.assertEqual(config['eligible'],['codex/gpt-6.1-sol/high'])
        states={row['key']:row['state'] for row in config['routes']}
        self.assertEqual(states['claude/claude-opus-5-5/high'],'not_set')
        self.assertEqual(states['codex/gpt-6-luna/low'],'disabled')
        self.assertEqual(config['not_set_meaning'],'not set (not eligible)')
        self.assertEqual(config['summary']['not_set'],28)
        with patch('pod.cli.contract',return_value={'status':'unavailable'}):
            doctor=execute(parser().parse_args(['doctor','--json']),self.project)
        self.assertEqual((doctor['routes']['enabled'],doctor['routes']['not_set']),(1,28))
        with patch('pod.cli.worker_rows',return_value={'workers':[],'scope':{'source':'flag','run':'run'},
                                                      'complete':True}), \
             patch('pod.cli.context_root_for_run',return_value=None):
            status=execute(parser().parse_args(['status','--run','run','--json']),self.project)
        self.assertEqual(status['preferences']['eligible'],['codex/gpt-6.1-sol/high'])
        self.assertEqual(status['routes']['not_set'],28)

    def test_placement_reports_canonical_and_missing_claude_link_precisely(self):
        canonical=self.root/'.agents'/'skills'/'pod'; canonical.mkdir(parents=True)
        report=inspect(self.project)
        self.assertEqual(report['codex_configured']['status'],'present_elsewhere')
        self.assertEqual(report['claude']['status'],'missing_claude_link')
        link=self.root/'other-claude'/'skills'/'pod'; link.parent.mkdir(parents=True)
        link.symlink_to(canonical,target_is_directory=True)
        self.assertEqual(inspect(self.project)['claude']['status'],'linked_to_canonical')

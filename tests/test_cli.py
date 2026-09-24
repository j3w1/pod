import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.bundle import version
from pod.cli import execute, main, parser
from pod.config import load, write_defaults
from pod.errors import PodError
from pod.placement import inspect
from tests.common import fixture


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.env=patch.dict(os.environ,{'HOME':str(self.root),'XDG_CONFIG_HOME':str(self.root/'config'),
                                     'XDG_STATE_HOME':str(self.root/'state'),
                                     'CODEX_HOME':str(self.root/'other-codex'),
                                     'CLAUDE_CONFIG_DIR':str(self.root/'other-claude')})
        self.env.__enter__(); self.addCleanup(self.env.__exit__,None,None,None)
        self.project=self.root/'project'; self.project.mkdir()

    def test_minimal_public_surface_and_version(self):
        help_text=parser().format_help()
        for family in ('config','doctor','status','update'): self.assertIn(family,help_text)
        for obsolete in ('setup','approve','revoke'): self.assertNotIn(obsolete,help_text)
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as versioned:
            parser().parse_args(['--version'])
        self.assertEqual(versioned.exception.code,0)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['config','approve'])

    def test_config_summary_and_invalid_file_exit_honestly(self):
        args=parser().parse_args(['config','--json'])
        missing=execute(args,self.project)
        self.assertEqual(missing['status'],'invalid')
        self.assertEqual(missing['eligible'],[])
        path=self.root/'config'/'pod'/'config.yaml'; write_defaults(path)
        valid=execute(args,self.project)
        self.assertEqual(valid['status'],'valid')
        self.assertEqual(len(valid['eligible']),6)
        self.assertEqual(len(valid['catalog']),6)
        path.write_text('schema: pod/v1\nselection: all\nmodels: {gpt-6-sol: available}\n')
        self.assertEqual(execute(args,self.project)['eligible'],[])

    def test_bare_non_tty_summary_uses_validity_exit_code(self):
        output=StringIO()
        with patch('pathlib.Path.cwd',return_value=self.project), redirect_stdout(output):
            self.assertEqual(main([]),1)
        self.assertIn('need attention',output.getvalue())
        write_defaults(self.root/'config'/'pod'/'config.yaml')
        output=StringIO()
        with patch('pathlib.Path.cwd',return_value=self.project), redirect_stdout(output):
            self.assertEqual(main([]),0)
        self.assertIn('6 eligible models',output.getvalue())

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

    def test_placement_reports_canonical_and_missing_claude_link_precisely(self):
        canonical=self.root/'.agents'/'skills'/'pod'; canonical.mkdir(parents=True)
        report=inspect(self.project)
        self.assertEqual(report['codex_configured']['status'],'present_elsewhere')
        self.assertEqual(report['claude']['status'],'missing_claude_link')
        link=self.root/'other-claude'/'skills'/'pod'; link.parent.mkdir(parents=True)
        link.symlink_to(canonical,target_is_directory=True)
        self.assertEqual(inspect(self.project)['claude']['status'],'linked_to_canonical')

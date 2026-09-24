"""Real launcher/PTY proof; pyte and wcwidth remain test-only dependencies."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.pty_harness import PtySession, dependencies_available, environment, fixture_config, LAUNCHER


class TuiPtyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not dependencies_available():
            if os.environ.get('POD_REQUIRE_PTY') == '1':
                raise AssertionError('POD_REQUIRE_PTY=1 needs pinned pyte and wcwidth')
            raise unittest.SkipTest('pyte and wcwidth are optional local test dependencies')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.home=self.root/'home'; self.work=self.root/'work'
        self.home.mkdir(); self.work.mkdir()
        self.config=self.home/'config'/'pod'/'config.yaml'
        fixture_config(self.config)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills'))
        from pod.config import load, set_model, set_mode
        self.load,self.set_model,self.set_mode=load,set_model,set_mode

    def open(self, *, cols=80, rows=24, **env):
        session=PtySession(home=self.home,cwd=self.work,cols=cols,rows=rows,env_extra=env)
        self.addCleanup(session.close)
        session.wait_for('Details  ',timeout=4)
        session.settle()
        return session

    def focused(self, session):
        return next((line for line in session.lines() if line.startswith('Details')), '')

    def table_line(self, session, name):
        return next((line for line in session.lines() if name in line and 'Details' not in line), '')

    def test_all_six_guidance_panels_change_on_focus(self):
        session=self.open()
        expected=['Claude Opus 5.5','Claude Sonnet 5','GPT-6 Astra',
                  'GPT-6 Luna','GPT-6 Sol','Claude Fable 5.1']
        for index,name in enumerate(expected):
            with self.subTest(name=name):
                if index:
                    session.send('\x1bOB')
                    session.wait_for(lambda s:name in self.focused(s))
                self.assertIn(name,self.focused(session))
                self.assertIn('Pod example:',session.text())
                self.assertIn('context native_default',session.text())
                self.assertIn('AA ',session.text())

    def test_sort_filter_focus_identity_and_space_edits_same_id(self):
        session=self.open()
        session.send('s')
        session.wait_for(lambda s:'Sort Intelligence' in s.text()
                         and 'Claude Opus 5.5' in self.focused(s))
        self.assertIn('Claude Opus 5.5',self.focused(session))
        session.send('f')
        session.wait_for(lambda s:'Filter Claude' in s.text() and bool(self.focused(s)))
        session.send('f')
        session.wait_for(lambda s:'Filter Codex' in s.text() and bool(self.focused(s)))
        session.send('f')
        session.wait_for(lambda s:'Filter All' in s.text() and 'Claude Opus 5.5' in self.focused(s))
        self.assertIn('Claude Opus 5.5',self.focused(session))
        session.send(' ')
        session.wait_for(lambda _s:self.load(personal=self.config)['saved']['claude-opus-5-5']=='preferred')
        self.assertEqual(self.load(personal=self.config)['saved']['claude-fable-5-1'],'available')
        session.wait_for('Saved just now')

    def test_sizes_resize_and_tiny_recovery(self):
        session=self.open()
        self.assertIn('Runtime',session.lines()[3])
        session.resize(60,20)
        session.wait_for(lambda s:'Runtime' not in s.lines()[3] and 'Details' in s.text())
        self.assertIn('USD/task',session.lines()[3])
        session.resize(40,15)
        session.wait_for(lambda s:'Rank' not in s.lines()[3] and 'Details' in s.text())
        session.resize(39,11)
        session.wait_for('Terminal too small')
        session.resize(40,15)
        session.wait_for('Details')

    def test_ascii_monochrome_no_results_help_and_expanded(self):
        session=self.open(LC_ALL='C',NO_COLOR='1')
        self.assertTrue(session.text().isascii())
        self.assertIn('>+ Available',session.text())
        self.assertNotIn(b'\x1b[38;',session.output)
        session.send('/')
        session.send('zzzz-no-model')
        session.wait_for('No models match')
        session.send('\n')
        session.send(' ')
        session.wait_for('no change saved')
        session.send('\x1b')
        session.wait_for(lambda s:'No models match' not in s.text())
        session.send('?')
        session.wait_for('Help:')
        session.send('\x1b')
        session.send('\n')
        session.wait_for('Official source:')
        self.assertIn('max with fallback',session.text())
        self.assertTrue(session.text().isascii())

    def test_runtime_unknown_offline_unsupported_and_slow_probe(self):
        unknown=self.open()
        unknown.wait_for('Unknown')
        stub=self.root/'orca-stub'
        stub.write_text('#!/bin/sh\nexit 1\n'); stub.chmod(0o700)
        offline=self.open(ORCA_CLI_COMMAND=str(stub))
        offline.wait_for('Offline')
        stub.write_text('#!/bin/sh\nprintf "{\\"ok\\":true,\\"result\\":{\\"runtime\\":{\\"capabilities\\":[]}}}"\n')
        unsupported=self.open(ORCA_CLI_COMMAND=str(stub))
        unsupported.wait_for('Unsupported')
        stub.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ok\":true,\"result\":{\"runtime\":{\"capabilities\":[\"orchestration.worker-launch-preferences.v1\"]}}}'\n")
        checked=self.open(ORCA_CLI_COMMAND=str(stub))
        checked.wait_for('Not checked')
        stub.write_text('#!/bin/sh\nsleep 5\n'); stub.chmod(0o700)
        slow=self.open(ORCA_CLI_COMMAND=str(stub))
        start=time.monotonic()
        slow.send('\x1bOB')
        slow.wait_for(lambda s:'Claude Sonnet 5' in self.focused(s),timeout=1)
        self.assertLess(time.monotonic()-start,.5)
        slow.wait_for('Offline',timeout=4)

    def test_invalid_missing_partial_and_external_refresh(self):
        session=self.open()
        self.config.unlink()
        session.wait_for('read-only',timeout=2)
        session.send(' ')
        session.wait_for('no change saved')
        self.assertFalse(self.config.exists())
        self.config.write_text('schema: pod/v1\nselection: all\nmodels: {gpt-6-sol: available}\n')
        session.wait_for('read-only',timeout=2)
        self.assertEqual(self.load(personal=self.config)['eligible'],[])
        fixture_config(self.config)
        session.wait_for('Preferences changed externally',timeout=2)
        start=time.monotonic()
        self.set_model(self.config,'claude-opus-5-5','preferred',
                       displayed=self.load(personal=self.config))
        session.wait_for(lambda s:'Preferred' in self.table_line(s,'Claude Opus 5.5'),timeout=1)
        self.assertLessEqual(time.monotonic()-start,1)

    def test_save_failure_keeps_file_and_reports_failure(self):
        session=self.open()
        before=self.config.read_bytes()
        folder=self.config.parent
        try:
            folder.chmod(0o500)
            session.send(' ')
            session.wait_for('Not saved',timeout=2)
            self.assertEqual(self.config.read_bytes(),before)
        finally:
            folder.chmod(0o700)

    def test_two_tuis_staged_conflict_and_recovery(self):
        first=self.open(); second=self.open()
        os.kill(second.pid,signal.SIGSTOP)
        try:
            second.send(' ')
            first.send(' ')
            first.wait_for(lambda _s:self.load(personal=self.config)['saved']['claude-opus-5-5']=='preferred')
        finally:
            os.kill(second.pid,signal.SIGCONT)
        second.wait_for('changed elsewhere',timeout=2)
        self.assertEqual(self.load(personal=self.config)['saved']['claude-opus-5-5'],'preferred')
        second.send(' ')
        second.wait_for(lambda _s:self.load(personal=self.config)['saved']['claude-opus-5-5']=='disabled')
        second.wait_for('Saved just now')

    def test_fleet_toggle_across_restart_and_edit_in_all_mode(self):
        self.set_model(self.config,'gpt-6-sol','preferred',displayed=self.load(personal=self.config))
        self.set_model(self.config,'gpt-6-luna','disabled',displayed=self.load(personal=self.config))
        original=self.load(personal=self.config)['saved'].copy()
        first=self.open()
        first.send('f')
        first.wait_for('Filter Claude')
        first.send('r')
        first.wait_for('All models')
        self.assertEqual(len(self.load(personal=self.config)['eligible']),6)
        first.send('q')
        first.close()
        second=self.open()
        self.assertIn('your choices are saved',second.text())
        second.send('r')
        second.wait_for('My selection')
        self.assertEqual(self.load(personal=self.config)['saved'],original)
        second.send('r')
        second.wait_for('All models')
        second.send(' ')
        second.wait_for('Returned to My selection')
        self.assertEqual(self.load(personal=self.config)['mode'],'custom')
        self.assertEqual(self.load(personal=self.config)['saved']['gpt-6-sol'],'preferred')

    def test_ctrl_c_and_sigterm_exit_without_writing(self):
        first=self.open()
        before=self.config.read_bytes()
        first.send('\x03')
        first.wait_for(lambda s:s.closed,timeout=2)
        self.assertEqual(self.config.read_bytes(),before)
        second=self.open()
        os.kill(second.pid,signal.SIGTERM)
        second.wait_for(lambda s:s.closed,timeout=2)
        self.assertEqual(self.config.read_bytes(),before)

    def test_non_tty_summary_and_exit_codes(self):
        env=environment(self.home)
        got=subprocess.run([sys.executable,'-I',str(LAUNCHER)],cwd=self.work,env=env,
                           capture_output=True,text=True,timeout=10)
        self.assertEqual(got.returncode,0,got.stderr)
        self.assertIn('6 eligible models',got.stdout)
        self.assertNotIn('\x1b',got.stdout)
        self.config.unlink()
        invalid=subprocess.run([sys.executable,'-I',str(LAUNCHER)],cwd=self.work,env=env,
                               capture_output=True,text=True,timeout=10)
        self.assertEqual(invalid.returncode,1)
        self.assertIn('need attention',invalid.stdout)

    def test_sparse_custom_map_stays_eligible_only_for_set_model(self):
        self.config.write_text('schema: pod/v1\nselection: custom\n'
                               'models: {gpt-6-sol: available}\nworkers: {max_active: 2}\n')
        session=self.open()
        self.assertIn('Not set (not eligible)',session.text())
        self.assertEqual(self.load(personal=self.config)['eligible'],['gpt-6-sol'])
        session.send(' ')
        session.wait_for(lambda _s:self.load(personal=self.config)['saved']['claude-opus-5-5']=='available')
        self.assertEqual(set(self.load(personal=self.config)['eligible']),
                         {'gpt-6-sol','claude-opus-5-5'})

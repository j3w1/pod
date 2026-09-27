"""Real-terminal model-pool interaction and contrast checks."""
from contextlib import nullcontext
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from pod.catalog import IDS, load as load_catalog
from pod.config import load as load_config, set_model
from tests.pty_harness import LAUNCHER, PtySession, dependencies_available, environment, fixture_config
from tests.install_support import ignored_sigint


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
        self.config=self.home/'config/pod/config.yaml'
        fixture_config(self.config)

    def open(self, *, cols=80, rows=24, ignore_sigint=False, **env):
        with ignored_sigint() if ignore_sigint else nullcontext():
            session=PtySession(home=self.home,cwd=self.work,cols=cols,rows=rows,env_extra=env)
        self.addCleanup(session.close)
        session.wait_for('Details',timeout=4)
        session.settle()
        return session

    def focused(self, session):
        return next((line for line in session.lines() if 'Details  ' in line), '')

    def table_line(self, session, name):
        return next((line for line in session.lines() if name in line and 'Available' in line or name in line and 'Preferred' in line), '')

    def cell_at(self, session, text, offset=0):
        """The styled cell at the first occurrence of text on screen, or None."""
        for row,line in enumerate(session.lines()):
            column=line.find(text)
            if column>=0:
                return session.cell(row,column+offset)
        return None

    def test_all_six_guidance_panels_change_on_focus(self):
        session=self.open(cols=100,rows=30)
        catalog=load_catalog()
        expected=['Claude Opus 5.5','GPT-6 Astra','Claude Sonnet 5','GPT-6 Luna','Claude Fable 5.1','GPT-6 Sol']
        # First focus is Opus, then wrap through the recommended coding order.
        for index,name in enumerate(expected):
            if index:
                session.send('\x1bOB')
                session.wait_for(lambda s:name in self.focused(s) and 'BENCHMARK SOURCE' in s.text())
            self.assertIn(name,self.focused(session))
            model=next(row for row in catalog['models'] if row['name']==name)
            self.assertIn(model['guide']['suggested_use'][:8],session.text())
            self.assertIn('BENCHMARK SOURCE',session.text())
        session.send('\n')
        session.wait_for('[expanded]')
        session.send('\x1b')
        session.wait_for(lambda s:'[expanded]' not in s.text())

    def test_sort_filter_focus_identity_and_space_edits_same_id(self):
        session=self.open()
        session.send('s')
        session.wait_for('Sort Model')
        session.settle()
        self.assertIn('Claude Opus 5.5',self.focused(session))
        for _ in range(3):
            session.send('f')
        session.wait_for('Filter All')
        session.send(' ')
        session.wait_for(lambda _s:load_config(personal=self.config)['saved']['claude-opus-5-5']=='preferred')
        session.wait_for('Saved just now')
        self.assertEqual(load_config(personal=self.config)['saved']['gpt-6-sol'],'available')

    def test_two_tuis_staged_conflict_and_recovery(self):
        first=self.open(); second=self.open()
        os.kill(second.pid,signal.SIGSTOP)
        try:
            second.send(' ')
            first.send(' ')
            first.wait_for(lambda _s:load_config(personal=self.config)['saved']['claude-opus-5-5']=='preferred')
        finally:
            os.kill(second.pid,signal.SIGCONT)
        second.wait_for('changed elsewhere',timeout=2)
        self.assertEqual(load_config(personal=self.config)['saved']['claude-opus-5-5'],'preferred')
        second.send(' ')
        second.wait_for(lambda _s:load_config(personal=self.config)['saved']['claude-opus-5-5']=='disabled')

    def test_browsing_never_writes_yaml(self):
        session=self.open()
        before=self.config.read_bytes(); stamp=self.config.stat().st_mtime_ns
        for key in ('s','f','/','Sol','\n','?','\x1b','\x1b','\n','\x1b'):
            session.send(key)
            session.settle()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(self.config.stat().st_mtime_ns,stamp)
        self.assertNotIn('Saved just now',session.text())

    def test_sizes_contrast_and_resize(self):
        for cols,rows in ((160,45),(100,30),(80,24),(60,20),(40,12)):
            session=self.open(cols=cols,rows=rows)
            for name in ('Claude Opus 5.5','Claude Fable 5.1','Claude Sonnet 5',
                         'GPT-6 Astra','GPT-6 Sol','GPT-6 Luna'):
                self.assertIn(name,session.text())
            if cols==160:
                self.assertIn('│',session.text())
            if cols==80:
                body=self.cell_at(session,'BEST FOR',offset=17)
                heading=self.cell_at(session,'BEST FOR')
                badge=self.cell_at(session,'Available')
                metric=self.cell_at(session,'/task')
                for cell in (body,heading,badge,metric):
                    self.assertIsNotNone(cell)
                self.assertNotEqual(body['fg'],'default')
                self.assertNotEqual(heading['fg'],body['fg'])
                self.assertNotEqual(badge['fg'],body['fg'])
                self.assertNotEqual(metric['fg'],body['fg'])
                session.resize(40,12)
                session.wait_for(lambda s:'GPT-6 Luna' in s.text() and 'Details' in s.text())
                session.resize(80,24)
                session.wait_for('BENCHMARK SOURCE')

    def test_ascii_nocolor_help_and_unknown_runtime(self):
        session=self.open(LC_ALL='C',NO_COLOR='1')
        self.assertTrue(session.text().isascii())
        sgr=set(re.findall(rb'\x1b\[[0-9;]*m',session.output))
        self.assertTrue(sgr <= {b'\x1b[1m',b'\x1b[m'},sgr)
        session.send('?')
        session.wait_for('Help:')
        session.wait_for('Preferences:')
        session.send('\x1b')
        session.wait_for('RUNTIME / ACCESS')
        self.assertIn('Orca launch: unknown',session.text())

    def test_save_failure_keeps_bytes_and_reports_failure(self):
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

    def test_runtime_capability_is_not_model_access(self):
        stub=self.root/'orca-stub'
        stub.write_text("#!/bin/sh\nprintf '%s\n' '{\"ok\":true,\"result\":{\"runtime\":{\"capabilities\":[\"orchestration.worker-launch-preferences.v1\"]}},\"_meta\":{\"runtimeId\":\"fixture-runtime\"}}'\n")
        stub.chmod(0o700)
        session=self.open(ORCA_CLI_COMMAND=str(stub))
        session.wait_for('Orca launch: supported',timeout=3)
        self.assertIn('model access not verified',session.text())

    def test_snapshot_helper_renders_cell_styling(self):
        from tests.tui_snapshot import render
        session=self.open(cols=80,rows=24)
        svg=render(session.screen,'pink')
        self.assertIn('<svg ',svg)
        self.assertIn('#1e1e2e',svg)
        self.assertIn('>P</text>',svg)
        with self.assertRaises(ValueError):
            render(session.screen,'missing')

    def test_external_refresh_invalid_yaml_and_non_tty(self):
        session=self.open()
        current=load_config(personal=self.config)
        set_model(self.config,'claude-opus-5-5','preferred',displayed=current)
        session.wait_for(lambda s:'Preferred' in self.table_line(s,'Claude Opus 5.5'),timeout=1)
        self.config.write_text('schema: [\n')
        session.wait_for('READ-ONLY',timeout=2)
        session.send(' ')
        self.assertIn('READ-ONLY',session.text())
        env=environment(self.home)
        result=subprocess.run([sys.executable,'-I',str(LAUNCHER)],cwd=self.work,env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertIn('need attention',result.stdout)

    def test_ctrl_c_and_sigterm_exit_without_writing(self):
        first=self.open(ignore_sigint=True)
        before=self.config.read_bytes()
        first.send('\x03')
        first.wait_for(lambda s:s.closed,timeout=2)
        self.assertEqual(self.config.read_bytes(),before)
        second=self.open(ignore_sigint=True)
        os.kill(second.pid,signal.SIGTERM)
        second.wait_for(lambda s:s.closed,timeout=2)
        self.assertEqual(self.config.read_bytes(),before)

    def test_pin_radio_moves_clears_and_survives_restart_filter_sort_resize(self):
        session = self.open(cols=100, rows=30)
        saved = load_config(personal=self.config)['saved']
        session.send('p')
        session.wait_for(lambda _s:load_config(personal=self.config)['pinned_model']=='claude-opus-5-5')
        session.wait_for(lambda s:s.text().count('●') == 1)
        self.assertIn('● Claude Opus 5.5', session.text())
        session.send('\x1bOBp')
        session.wait_for(lambda _s:load_config(personal=self.config)['pinned_model']=='gpt-6-astra')
        session.wait_for(lambda s:'● GPT-6 Astra' in s.text() and s.text().count('●') == 1)
        session.send('sf')
        session.wait_for(lambda s:'Pin gpt-6-astra' in s.text() and '●' not in s.text())
        session.resize(40, 12)
        session.wait_for(lambda s:'Pin gpt-6-astra' in s.text())
        self.assertEqual(load_config(personal=self.config)['saved'], saved)
        session.close()
        restarted = self.open(cols=80, rows=24)
        self.assertIn('● GPT-6 Astra', restarted.text())
        # Recommended order starts at Opus; Down reaches Astra. Pressing p clears its pin.
        restarted.send('\x1bOBp')
        restarted.wait_for(lambda _s:load_config(personal=self.config)['pinned_model'] is None)
        restarted.wait_for(lambda s:'●' not in s.text())
        self.assertEqual(load_config(personal=self.config)['saved'], saved)

    def test_pin_invalidating_edit_and_all_custom_mode_are_atomic(self):
        session = self.open()
        session.send('p ')
        session.wait_for(lambda _s:load_config(personal=self.config)['saved']['claude-opus-5-5']=='preferred')
        before = self.config.read_bytes()
        session.send(' ')
        session.wait_for('Unpin or replace')
        self.assertEqual(self.config.read_bytes(), before)
        session.send('pr')
        session.wait_for(lambda _s:load_config(personal=self.config)['mode']=='all')
        from pod.config import set_pin
        # A valid All-mode pin may point at a saved Disabled row.
        import yaml
        document = yaml.safe_load(self.config.read_text())
        document['models']['gpt-6-astra'] = 'disabled'
        self.config.write_text(yaml.safe_dump(document,sort_keys=False))
        set_pin(self.config, 'gpt-6-astra', displayed=load_config(personal=self.config))
        session.wait_for('Pin gpt-6-astra')
        before = self.config.read_bytes()
        session.send('r')
        session.wait_for('Unpin or replace')
        self.assertEqual(self.config.read_bytes(), before)

    def test_hidden_pin_survives_persistent_refusal_and_save_error_notices(self):
        for cols, rows in ((80, 24), (40, 12)):
            for failure in ('refusal', 'save_error'):
                with self.subTest(cols=cols, failure=failure):
                    fixture_config(self.config)
                    session = self.open(cols=cols, rows=rows, LC_ALL='C', NO_COLOR='1')
                    session.send('\x1bOBp')
                    session.wait_for(lambda s:'(*) GPT-6 Astra' in s.text())
                    if failure == 'refusal':
                        session.send(' ')
                        session.wait_for(lambda _s:load_config(personal=self.config)['saved']['gpt-6-astra']=='preferred')
                    before = self.config.read_bytes()
                    try:
                        if failure == 'save_error':
                            self.config.parent.chmod(0o500)
                        session.send(' ' if failure == 'refusal' else 'p')
                        session.wait_for('Not saved')
                        session.send('f')
                        session.wait_for(lambda s:'Pin gpt-6-astra' in s.text() and '(*)' not in s.text())
                        session.settle(quiet=.8, timeout=1)
                        self.assertIn('Pin gpt-6-astra', session.text())
                        self.assertIn('Not saved', session.text())
                        self.assertEqual(load_config(personal=self.config)['pinned_model'], 'gpt-6-astra')
                        self.assertEqual(self.config.read_bytes(), before)
                    finally:
                        self.config.parent.chmod(0o700)
                        session.close()

    def test_invalid_pin_is_visible_read_only_and_names_editor_recovery(self):
        import yaml
        for pin in ('gpt-6-sol', 'unknown', []):
            with self.subTest(pin=pin):
                fixture_config(self.config)
                document = yaml.safe_load(self.config.read_text())
                document['pinned_model'] = pin
                document['models']['gpt-6-sol'] = 'disabled'
                self.config.write_text(yaml.safe_dump(document))
                before = self.config.read_bytes()
                session = self.open()
                expected = '<invalid list pin>' if isinstance(pin, list) else pin
                session.wait_for('Pin ' + expected)
                session.wait_for('READ-ONLY')
                session.wait_for('pod config edit')
                session.send('p r')
                session.settle()
                self.assertIn('Pin ' + expected, session.text())
                self.assertEqual(self.config.read_bytes(), before)
                session.close()

    def test_ascii_monochrome_pin_and_concurrent_pin_conflict(self):
        first = self.open(LC_ALL='C', NO_COLOR='1')
        second = self.open(LC_ALL='C', NO_COLOR='1')
        os.kill(second.pid, signal.SIGSTOP)
        try:
            second.send('p')
            first.send('p')
            first.wait_for(lambda _s:load_config(personal=self.config)['pinned_model']=='claude-opus-5-5')
        finally:
            os.kill(second.pid, signal.SIGCONT)
        second.wait_for('changed elsewhere')
        self.assertEqual(load_config(personal=self.config)['pinned_model'], 'claude-opus-5-5')
        first.wait_for(lambda s:s.text().count('(*)') == 1)
        self.assertTrue(first.text().isascii())
        second.send('\x1bOBp')
        second.wait_for(lambda _s:load_config(personal=self.config)['pinned_model']=='gpt-6-astra')
        first.wait_for('(*) GPT-6 Astra')
        self.assertEqual(first.text().count('(*)'), 1)

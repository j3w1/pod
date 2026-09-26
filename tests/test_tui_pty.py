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

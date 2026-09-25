"""A copied bundle remains browsable when an AA reference row is unavailable."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.pty_harness import (ROOT, PtySession, dependencies_available,
                               environment, fixture_config)


class OptionalCatalogPtyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not dependencies_available():
            if os.environ.get("POD_REQUIRE_PTY") == "1":
                raise AssertionError("POD_REQUIRE_PTY=1 needs pinned pyte and wcwidth")
            raise unittest.SkipTest("pyte and wcwidth are optional local test dependencies")

    def test_missing_reference_row_still_renders_and_config_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'home';work=root/'work'
            home.mkdir();work.mkdir()
            fixture_config(home/'config/pod/config.yaml')
            copied=root/'copy/pod'
            shutil.copytree(ROOT/'skills/pod',copied,
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            catalog_path=copied/'catalog.json'
            document=json.loads(catalog_path.read_text())
            del document['benchmarks']['models']['gpt-6-luna']
            catalog_path.write_text(json.dumps(document))
            launcher=copied/'scripts/pod.py'
            with PtySession(home=home,cwd=work,launcher=launcher) as screen:
                screen.wait_for('Details',timeout=4)
                screen.send('/Luna\n')
                screen.wait_for(lambda session: 'Details  GPT-6 Luna' in session.text()
                                and 'unknown' in session.text(),timeout=2)
                screen.settle()
                self.assertIn('unknown',screen.text())
                self.assertIn('BENCHMARK SOURCE',screen.text())
                screen.send('\n')
                screen.wait_for('Details  GPT-6 Luna [expanded]')
            result=subprocess.run([sys.executable,'-I',str(launcher),'config','--json'],
                                  cwd=work,env=environment(home),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(len(json.loads(result.stdout)['eligible']),6)
            doctor=subprocess.run([sys.executable,'-I',str(launcher),'doctor','--json'],
                                  cwd=work,env=environment(home),capture_output=True,text=True,timeout=10)
            self.assertEqual(doctor.returncode,0,doctor.stdout+doctor.stderr)
            self.assertEqual(set(json.loads(doctor.stdout)['catalog']['ranks_of_six'].values()),{None})

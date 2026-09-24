import json
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.bundle import bundle_root, version
from tests.common import fixture

BUNDLE=bundle_root()


def copy_bundle(target:Path)->Path:
    destination=target/'pod'
    shutil.copytree(BUNDLE,destination,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    return destination


def run(skill:Path,args:list[str],*,home:Path,cwd:Path)->subprocess.CompletedProcess:
    env={key:value for key,value in os.environ.items() if key in ('PATH','LANG','LC_ALL','TMPDIR')}
    env.update({'HOME':str(home),'XDG_CONFIG_HOME':str(home/'config'),
                'XDG_STATE_HOME':str(home/'state'),'CODEX_HOME':str(home/'agents'),
                'CLAUDE_CONFIG_DIR':str(home/'claude')})
    return subprocess.run([sys.executable,'-I',str(skill/'scripts'/'pod.py'),*args],
                          capture_output=True,text=True,cwd=cwd,env=env,timeout=30)


class CopiedBundleTests(unittest.TestCase):
    def test_version_is_available_before_pyyaml_and_no_checkout_is_needed(self):
        with fixture() as root:
            skill=copy_bundle(root/'installed'); home=root/'home'; work=root/'unrelated'
            home.mkdir(); work.mkdir()
            got=run(skill,['--version'],home=home,cwd=work)
            self.assertEqual(got.returncode,0,got.stderr)
            self.assertEqual(got.stdout.strip(),version())
            help_output=run(skill,['--help'],home=home,cwd=work)
            self.assertEqual(help_output.returncode,0,help_output.stderr)
            for family in ('config','doctor','status','update'):
                self.assertIn(family,help_output.stdout)
            self.assertNotIn('setup',help_output.stdout)
            self.assertNotIn('internal',help_output.stdout)
            doctor=run(skill,['doctor','--json'],home=home,cwd=work)
            self.assertEqual(doctor.returncode,0,doctor.stderr)
            report=json.loads(doctor.stdout)
            self.assertEqual(report['schema'],'pod-cli/v4')
            self.assertTrue(report['bundle'].startswith(str(skill)))
            self.assertEqual(report['installation'],'not installed by the one-shot installer')
            config=run(skill,['config','--json'],home=home,cwd=work)
            self.assertEqual(config.returncode,1)
            self.assertEqual(json.loads(config.stdout)['eligible'],[])

    def test_private_helper_runs_only_through_bundle(self):
        with fixture() as root:
            skill=copy_bundle(root/'installed'); home=root/'home'; work=root/'unrelated'
            home.mkdir(); work.mkdir()
            request=work/'brief.json'
            request.write_text(json.dumps({'criteria':['works'],'coverage':[{'criterion':'works','check':'unit'}]}))
            got=run(skill,['internal','brief','--input',str(request)],home=home,cwd=work)
            self.assertEqual(got.returncode,0,got.stderr)
            self.assertEqual(json.loads(got.stdout)['schema'],'pod-cli/v4')
            self.assertNotIn('\x1b',got.stdout)
            self.assertNotEqual(run(skill,['internal-preview'],home=home,cwd=work).returncode,0)

    def test_missing_dependency_is_actionable_and_version_still_works(self):
        with fixture() as root:
            skill=copy_bundle(root/'installed')
            home=root/'home'; work=root/'unrelated'; home.mkdir(); work.mkdir()
            script=skill/'scripts'/'pod.py'
            spec=importlib.util.spec_from_file_location('copied_pod_launcher',script)
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            output=StringIO()
            with patch.object(module,'_yaml_importable',return_value=False) as yaml_probe, \
                 redirect_stdout(output):
                self.assertEqual(module.main(['--version']),0)
            self.assertEqual(output.getvalue().strip(),version())
            yaml_probe.assert_not_called()
            self.assertIn('one-shot',module.preflight(find_spec=lambda:False)['message'])

    def test_bundle_wins_over_an_unrelated_importable_package(self):
        with fixture() as root:
            skill=copy_bundle(root/'installed'); home=root/'home'; work=root/'unrelated'
            home.mkdir(); work.mkdir()
            imposter=work/'pod'; imposter.mkdir()
            (imposter/'__init__.py').write_text('raise RuntimeError("wrong package")')
            result=run(skill,['doctor','--json'],home=home,cwd=work)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(json.loads(result.stdout)['bundle'].startswith(str(skill)))

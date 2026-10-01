"""Kept 0.6.x preferences, the explicit route setup, and the genuine 0.6.7 parser and installer."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from pod.catalog import route_keys
from pod.config import BACKUP_SUFFIX, load, setup_apply, setup_preview
from pod.errors import PodError
from tests.common import POD_067, earlier_validate, fixture

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'pod-0.6.7'
DEFAULT_067 = 'config-default.yaml'
PINNED_067 = 'config-preferred-disabled-pinned.yaml'
SPARSE_067 = 'config-all-sparse-replaced.yaml'


def routes_of(model: str) -> list[str]:
    return [key for key in route_keys() if key.split('/')[1] == model]


class RouteSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'pod' / 'config.yaml'
        self.path.parent.mkdir()

    def kept(self, name: str) -> bytes:
        raw = (FIXTURES / name).read_bytes()
        self.path.write_bytes(raw)
        return raw

    def codes(self, preview: dict) -> list[str]:
        return [note['code'] for note in preview['notes']]

    def test_genuine_067_files_stay_readable_as_setup_required(self):
        for name in (DEFAULT_067, PINNED_067, SPARSE_067):
            with self.subTest(name=name):
                raw = self.kept(name)
                self.assertTrue(earlier_validate(POD_067, yaml.safe_load(raw))['accepted'])
                snapshot = load(personal=self.path)
                self.assertEqual((snapshot['status'], snapshot['schema'], snapshot['eligible']),
                                 ('setup_required', 'pod/v1', []))
                self.assertEqual(snapshot['errors'][0]['code'], 'setup_required')
                self.assertIn('confirm the route setup', snapshot['errors'][0]['message'])
                self.assertEqual(snapshot['refresh'], 'manual')
                self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(load(personal=self.path)['pin_diagnostic'], 'gpt-6-sol')
        self.assertEqual(load(personal=self.path)['waste_governor']['preflight'], ['personal-check'])

    def test_preview_is_pure_deterministic_and_conservative(self):
        raw = self.kept(DEFAULT_067)
        with patch('pod.config._replace', side_effect=AssertionError('preview writes nothing')):
            first, second = setup_preview(raw), setup_preview(raw)
        self.assertEqual(first, second)
        self.assertEqual(first['revision'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['config.yaml'])
        document = first['document']
        enabled = sorted(key for key, state in document['routes'].items() if state == 'enabled')
        self.assertEqual(enabled, sorted(routes_of('claude-opus-5-5') + routes_of('claude-fable-5-1')
                                         + routes_of('gpt-6-astra') + routes_of('gpt-6-luna')))
        # Replacement generations are never authorized from their predecessors.
        for model in ('claude-sonnet-5-5', 'gpt-6.1-sol'):
            self.assertFalse(any(key in document['routes'] for key in routes_of(model)))
        self.assertEqual((document['preferred'], document['pinned'], document['workers'], document['refresh']),
                         (None, None, {'max_active': 2}, 'automatic'))
        self.assertEqual(first['choices'], [])
        self.assertEqual(self.codes(first), ['replaced_not_transferred', 'refresh_automatic'])
        self.assertIn('claude-sonnet-5 (replaced by claude-sonnet-5-5)', first['notes'][0]['message'])
        self.assertIn('gpt-6-sol (replaced by gpt-6.1-sol)', first['notes'][0]['message'])

        raw = self.kept(PINNED_067)
        pinned = setup_preview(raw)
        self.assertEqual(pinned['document']['workers'], {'max_active': 3})
        self.assertTrue(all(pinned['document']['routes'][key] == 'disabled' for key in routes_of('gpt-6-luna')))
        self.assertTrue(all(pinned['document']['routes'][key] == 'enabled' for key in routes_of('claude-opus-5-5')))
        self.assertIsNone(pinned['document']['preferred'])
        self.assertEqual(pinned['choices'], [{'id': 'pin', 'model': 'claude-opus-5-5',
                                              'options': routes_of('claude-opus-5-5'), 'clear': True}])
        self.assertEqual(self.codes(pinned), ['replaced_not_transferred', 'preferred_not_inferred',
                                              'pin_effort_required', 'refresh_automatic'])

        raw = self.kept(SPARSE_067)
        sparse = setup_preview(raw)
        self.assertEqual(sorted(sparse['document']['routes']), sorted(routes_of('gpt-6-astra')))
        self.assertEqual(sparse['document']['waste_governor'], {'preflight': ['personal-check']})
        self.assertEqual(sparse['choices'], [{'id': 'pin', 'model': 'gpt-6-sol', 'options': [], 'clear': True}])
        self.assertEqual(self.codes(sparse), ['replaced_not_transferred', 'selection_all_narrowed',
                                              'preferred_not_inferred', 'pin_base_unsupported', 'refresh_automatic'])
        for bad, code in ((self.path.read_bytes().replace(b'gpt-6-astra', b'gpt-6.1-sol'), 'invalid_config'),
                          (b'schema: pod/v2\nroutes: {}\nworkers: {max_active: 2}\n', 'setup_not_required'),
                          (b'schema: pod/v1\nmodels: [\n', 'invalid_yaml'), ('text', 'invalid_setup')):
            with self.subTest(code=code), self.assertRaises(PodError) as caught:
                setup_preview(bad)
            self.assertEqual(caught.exception.code, code)

    def test_apply_needs_explicit_pin_choice_and_keeps_the_original(self):
        raw = self.kept(PINNED_067)
        revision = hashlib.sha256(raw).hexdigest()
        for choices, code in (({}, 'setup_choice_required'),
                              ({'pin': 'codex/gpt-6-astra/high'}, 'invalid_setup_choice'),
                              ({'pin': 'claude/claude-opus-5-5/ultra'}, 'invalid_setup_choice'),
                              ({'pin': None, 'preferred': 'codex/gpt-6-luna/low'}, 'preferred_ineligible'),
                              ({'pin': None, 'routes': {'codex/gpt-6-sol/high': 'enabled'}}, 'invalid_setup_choice'),
                              ({'pin': None, 'mode': 'all'}, 'invalid_setup_choice')):
            with self.subTest(choices=choices), self.assertRaises(PodError) as caught:
                setup_apply(self.path, expected_revision=revision, choices=choices)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertFalse(self.path.with_name('config.yaml' + BACKUP_SUFFIX).exists())
        saved = setup_apply(self.path, expected_revision=revision,
                            choices={'pin': 'claude/claude-opus-5-5/high', 'preferred': 'claude/claude-fable-5-1/xhigh',
                                     'routes': {'codex/gpt-6.1-sol/medium': 'enabled'}})
        self.assertEqual(Path(saved['backup']).read_bytes(), raw)
        self.assertEqual(Path(saved['backup']).name, 'config.yaml.pod-v1')
        snapshot = load(personal=self.path)
        self.assertEqual((snapshot['status'], snapshot['pinned'], snapshot['preferred'], snapshot['max_active']),
                         ('valid', 'claude/claude-opus-5-5/high', 'claude/claude-fable-5-1/xhigh', 3))
        self.assertIn('codex/gpt-6.1-sol/medium', snapshot['eligible'])
        self.assertEqual(snapshot['routes']['codex/gpt-6-luna/low'], 'disabled')
        self.assertEqual(snapshot['revision'], saved['revision'])
        # Older releases refuse the converted file instead of reading part of it.
        self.assertFalse(earlier_validate(POD_067, yaml.safe_load(self.path.read_bytes()))['accepted'])
        with self.assertRaises(PodError) as again:
            setup_apply(self.path, expected_revision=revision, choices={'pin': None})
        self.assertEqual(again.exception.code, 'config_changed_elsewhere')

    def test_an_unsupported_pin_can_only_be_cleared_explicitly(self):
        raw = self.kept(SPARSE_067)
        revision = hashlib.sha256(raw).hexdigest()
        with self.assertRaises(PodError) as caught:
            setup_apply(self.path, expected_revision=revision, choices={'pin': 'codex/gpt-6.1-sol/medium',
                        'routes': {'codex/gpt-6.1-sol/medium': 'enabled'}})
        self.assertEqual(caught.exception.code, 'invalid_setup_choice')
        saved = setup_apply(self.path, expected_revision=revision, choices={'pin': None})
        snapshot = load(personal=self.path)
        self.assertEqual((snapshot['pinned'], snapshot['eligible']), (None, routes_of('gpt-6-astra')))
        self.assertEqual(snapshot['waste_governor']['preflight'], ['personal-check'])
        self.assertEqual(saved['document']['waste_governor'], {'preflight': ['personal-check']})

    def test_changed_bytes_and_concurrent_setup_fail_safely(self):
        raw = self.kept(DEFAULT_067)
        revision = hashlib.sha256(raw).hexdigest()
        changed = raw.replace(b'max_active: 2', b'max_active: 1')
        self.path.write_bytes(changed)
        with self.assertRaises(PodError) as caught:
            setup_apply(self.path, expected_revision=revision, choices={})
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertFalse(self.path.with_name('config.yaml' + BACKUP_SUFFIX).exists())
        self.path.write_bytes(raw)
        barrier = threading.Barrier(4)

        def attempt(_index):
            barrier.wait()
            try:
                return setup_apply(self.path, expected_revision=revision, choices={})['status']
            except PodError as exc:
                return exc.code
        with ThreadPoolExecutor(4) as pool:
            outcomes = list(pool.map(attempt, range(4)))
        self.assertEqual(outcomes.count('saved'), 1, outcomes)
        self.assertTrue(set(outcomes) <= {'saved', 'config_changed_elsewhere', 'config_busy'}, outcomes)
        self.assertEqual(load(personal=self.path)['status'], 'valid')
        self.assertEqual(self.path.with_name('config.yaml' + BACKUP_SUFFIX).read_bytes(), raw)

    def test_interrupted_setup_loses_nothing_and_can_resume(self):
        raw = self.kept(PINNED_067)
        revision = hashlib.sha256(raw).hexdigest()
        choices = {'pin': None}
        backup = self.path.with_name('config.yaml' + BACKUP_SUFFIX)
        with patch('pod.config.os.link', side_effect=OSError('interrupted before the copy')), \
             self.assertRaises(OSError):
            setup_apply(self.path, expected_revision=revision, choices=choices)
        self.assertEqual((self.path.read_bytes(), backup.exists()), (raw, False))
        with patch('pod.config._replace', side_effect=OSError('interrupted before the swap')), \
             self.assertRaises(OSError):
            setup_apply(self.path, expected_revision=revision, choices=choices)
        self.assertEqual((self.path.read_bytes(), backup.read_bytes()), (raw, raw))
        self.assertEqual(load(personal=self.path)['status'], 'setup_required')
        self.assertEqual([path.name for path in self.path.parent.iterdir() if path.name.startswith('.pod-config-')], [])
        # The kept copy is reused, never overwritten; a different one stops the setup.
        setup_apply(self.path, expected_revision=revision, choices=choices)
        self.assertEqual(backup.read_bytes(), raw)
        other = self.root / 'other' / 'config.yaml'
        other.parent.mkdir()
        other.write_bytes(raw)
        other.with_name('config.yaml' + BACKUP_SUFFIX).write_bytes(b'someone else\n')
        with self.assertRaises(PodError) as caught:
            setup_apply(other, expected_revision=revision, choices=choices)
        self.assertEqual(caught.exception.code, 'setup_backup_exists')
        self.assertEqual(other.read_bytes(), raw)
        self.assertEqual(other.with_name('config.yaml' + BACKUP_SUFFIX).read_bytes(), b'someone else\n')


class GenuineInstallUpgradeTests(unittest.TestCase):
    def setUp(self):
        from tests.install_support import sandbox
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env, self.archive = sandbox(self.root)
        self.config = Path(self.env['HOME']) / 'config/pod/config.yaml'

    def test_genuine_067_install_keeps_preferences_and_setup_converts_them(self):
        from tests.install_support import ROOT, run_pod
        old_tar = self.root / 'public-067.tar.gz'
        subprocess.run(['git', 'archive', '--format=tar.gz', '--prefix=pod-source/', '-o', str(old_tar), POD_067],
                       cwd=ROOT, check=True)
        old_script = self.root / 'install-067.sh'
        old_script.write_bytes(subprocess.check_output(['git', 'show', f'{POD_067}:install.sh'], cwd=ROOT))
        result = subprocess.run(['sh', str(old_script)], env={**self.env, 'POD_INSTALL_SOURCE': old_tar.as_uri()},
                                cwd=self.root / 'work', capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(run_pod(self.env, '--version').stdout.strip(), '0.6.7')
        shutil.copyfile(FIXTURES / PINNED_067, self.config)
        kept = self.config.read_bytes()
        self.assertEqual(json.loads(run_pod(self.env, 'config', '--json').stdout)['pinned_model'], 'claude-opus-5-5')
        upgraded = run_pod(self.env, 'update')
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
        self.assertIn('route setup', upgraded.stdout)
        self.assertEqual(run_pod(self.env, '--version').stdout.strip(), (ROOT / 'VERSION').read_text().strip())
        self.assertEqual(self.config.read_bytes(), kept)
        report = json.loads(run_pod(self.env, 'config', '--json').stdout)
        self.assertEqual((report['status'], report['eligible'], report['pin_diagnostic']),
                         ('setup_required', [], 'claude-opus-5-5'))
        doctor = json.loads(run_pod(self.env, 'doctor', '--json').stdout)
        self.assertEqual(doctor['preferences']['status'], 'setup_required')
        self.assertEqual(self.config.read_bytes(), kept)
        preview = setup_preview(kept)
        setup_apply(self.config, expected_revision=preview['revision'],
                    choices={'pin': 'claude/claude-opus-5-5/medium'})
        report = json.loads(run_pod(self.env, 'config', '--json').stdout)
        self.assertEqual((report['status'], report['pinned'], report['max_active']),
                         ('valid', 'claude/claude-opus-5-5/medium', 3))
        self.assertEqual(self.config.with_name('config.yaml.pod-v1').read_bytes(), kept)


if __name__ == '__main__':
    unittest.main()

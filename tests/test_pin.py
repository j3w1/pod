"""Personal pin behavior without native effects or implicit preference migration."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import subprocess
import unittest
from unittest.mock import patch

import yaml
from pod.catalog import IDS
from pod.config import DEFAULT, load, set_mode, set_model, set_pin, validate, write_defaults
from pod.errors import PodError
from pod.selection import failure_active, validate_choice
from tests.common import fixture
from tests.test_selection import choice


class PinTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'preferences/config.yaml'
        write_defaults(self.path)

    def snapshot(self):
        return load(personal=self.path)

    def test_missing_null_pin_and_old_parser_refusal(self):
        before = self.path.read_bytes()
        self.assertIsNone(self.snapshot()['pinned_model'])
        self.assertTrue(validate_choice(self.snapshot(), [], [], choice())['allowed'])
        self.assertEqual(self.path.read_bytes(), before)
        set_pin(self.path, None, displayed=self.snapshot())
        self.assertTrue(validate_choice(self.snapshot(), [], [], choice())['allowed'])
        # The exact public 0.6.5 parser forbids the new key, rather than ignoring it.
        source = subprocess.check_output(['git', 'show',
            '6a7b2ba79e8883946f0f880d55c8dcebe3cf086c:skills/pod/config.py'], text=True)
        namespace = {'__name__': 'pod.old_config', '__package__': 'pod'}
        exec(compile(source, '<public 0.6.5 config>', 'exec'), namespace)
        self.assertEqual(namespace['validate'](deepcopy(DEFAULT)), DEFAULT)
        with self.assertRaises(PodError):
            namespace['validate']({**deepcopy(DEFAULT), 'pinned_model': 'gpt-6-sol'})

    def test_pin_switch_clear_comments_and_concurrent_target(self):
        self.path.write_text(self.path.read_text() + '# personal note\n')
        saved = self.snapshot()['saved']
        displayed = self.snapshot()
        set_pin(self.path, 'gpt-6-sol', displayed=displayed)
        with self.assertRaises(PodError) as caught:
            set_pin(self.path, 'gpt-6-astra', displayed=displayed)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')
        stale = self.snapshot()
        set_model(self.path, 'gpt-6-luna', 'preferred', displayed=stale)
        set_pin(self.path, 'gpt-6-astra', displayed=stale)
        self.assertEqual(self.snapshot()['saved']['gpt-6-luna'], 'preferred')
        set_pin(self.path, None, displayed=self.snapshot())
        self.assertEqual(self.snapshot()['saved'], {**saved, 'gpt-6-luna': 'preferred'})
        self.assertIn('# personal note', self.path.read_text())

    def test_invalid_pins_and_effective_all_custom_eligibility(self):
        for pin in (True, [], {}, 3, 'unknown'):
            with self.subTest(pin=pin), self.assertRaises(PodError) as caught:
                validate({**deepcopy(DEFAULT), 'pinned_model': pin})
            self.assertEqual(caught.exception.code, 'invalid_pin')
        sparse = {**deepcopy(DEFAULT), 'models': {'gpt-6-sol': 'available'}}
        self.path.write_text(yaml.safe_dump(sparse))
        for omitted in ('gpt-6-luna',):
            with self.assertRaises(PodError):
                set_pin(self.path, omitted, displayed=self.snapshot())
        set_mode(self.path, 'all', displayed=self.snapshot())
        set_pin(self.path, 'gpt-6-luna', displayed=self.snapshot())
        original = self.path.read_bytes()
        for action in (lambda: set_mode(self.path, 'custom', displayed=self.snapshot()),
                       lambda: set_model(self.path, 'gpt-6-sol', 'preferred', displayed=self.snapshot())):
            with self.assertRaises(PodError) as caught: action()
            self.assertEqual(caught.exception.code, 'pin_ineligible')
            self.assertEqual(self.path.read_bytes(), original)
        set_pin(self.path, 'gpt-6-sol', displayed=self.snapshot())
        set_mode(self.path, 'custom', displayed=self.snapshot())
        self.assertEqual(self.snapshot()['saved']['gpt-6-luna'], None)
        with self.assertRaises(PodError):
            set_model(self.path, 'gpt-6-sol', 'disabled', displayed=self.snapshot())

    def test_save_failure_invalid_yaml_and_precise_route_refusal(self):
        original = self.path.read_bytes()
        with patch('pod.config._replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): set_pin(self.path, 'gpt-6-sol', displayed=self.snapshot())
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text('schema: [invalid\n')
        with self.assertRaises(PodError): set_pin(self.path, None, displayed=self.snapshot())
        self.assertFalse(self.snapshot()['eligible'])
        invalid = {**deepcopy(DEFAULT), 'pinned_model': 'unknown'}
        self.path.write_text(yaml.safe_dump(invalid))
        self.assertEqual(validate_choice(self.snapshot(), [], [], choice())['code'], 'invalid_pin')

    def test_all_pod_roles_repository_precedence_adaptive_effort_and_hard_constraints(self):
        set_pin(self.path, 'gpt-6-sol', displayed=self.snapshot())
        snapshot = self.snapshot()
        for role in ('implement', 'investigate', 'review', 'writer', 'correction'):
            constraints = [{'kind': 'role_model', 'provenance': 'repository',
                            'role': role, 'value': 'claude-opus-5-5'},
                           {'kind': 'only_models', 'provenance': 'repository', 'value': ['gpt-6-astra']},
                           {'kind': 'exclude_models', 'provenance': 'repository', 'value': ['gpt-6-sol']}]
            for effort in ('low', 'medium', 'high', 'xhigh', 'native_default'):
                with self.subTest(role=role, effort=effort):
                    self.assertTrue(validate_choice(snapshot, constraints, [], choice(effort=effort), role=role)['allowed'])
            self.assertEqual(validate_choice(snapshot, [], [], choice('gpt-6-luna'), role=role)['code'], 'pin_mismatch')
            hard = {'kind': 'only_agents', 'provenance': 'repository', 'value': ['claude']}
            self.assertEqual(validate_choice(snapshot, [hard], [], choice(), role=role)['code'], 'constraint_excluded')
            for provenance in ('user_direct', 'issue', 'worker'):
                self.assertEqual(validate_choice(snapshot, [{**constraints[0], 'provenance': provenance}], [],
                                                 choice(), role=role)['code'], 'constraint_excluded')
        set_pin(self.path, None, displayed=snapshot)
        self.assertEqual(validate_choice(self.snapshot(), constraints, [], choice(), role=role)['code'], 'constraint_excluded')

    def test_temporary_expiry_native_deadline_auth_and_safety_stay_distinct(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        unavailable = {'kind': 'unavailable', 'model': 'gpt-6-sol', 'task': 'old',
                       'source': 'readiness timeout; cause unknown', 'recorded_at': now.isoformat()}
        self.assertTrue(failure_active(unavailable, now=now + timedelta(seconds=59)))
        self.assertFalse(failure_active(unavailable, now=now + timedelta(seconds=60)))
        self.assertTrue(failure_active({**unavailable, 'retry_after': (now + timedelta(minutes=2)).isoformat()},
                                       now=now + timedelta(seconds=60)))
        for kind in ('auth_failed', 'rate_limited', 'safety_refusal'):
            self.assertTrue(failure_active({**unavailable, 'kind': kind}, now=now + timedelta(days=20)))
        set_pin(self.path, 'gpt-6-sol', displayed=self.snapshot())
        self.assertEqual(validate_choice(self.snapshot(), [], [unavailable], choice(), now=now)['code'], 'route_failed')
        self.assertEqual(validate_choice(self.snapshot(), [], [unavailable], choice('gpt-6-luna'), now=now)['code'], 'pin_mismatch')
        self.assertTrue(validate_choice(self.snapshot(), [], [unavailable], choice(), now=now + timedelta(days=1))['allowed'])
        safety = {**unavailable, 'kind': 'safety_refusal'}
        self.assertEqual(validate_choice(self.snapshot(), [], [safety], choice(), task='old',
                                        now=now + timedelta(days=1))['code'], 'safety_refusal')

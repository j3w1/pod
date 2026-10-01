"""Exact Preferred and Pinned routes without native effects or implicit preference conversion."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import yaml
from pod.config import (defaults, edit, load, set_pin, set_preferred, set_route, set_routes, validate,
                        write_defaults)
from pod.errors import PodError
from pod.selection import failure_active, validate_choice
from tests.common import POD_067, earlier_validate, fixture
from tests.test_selection import choice

SOL = 'codex/gpt-6.1-sol/medium'
SOL_HIGH = 'codex/gpt-6.1-sol/high'
ASTRA = 'codex/gpt-6-astra/high'
OPUS = 'claude/claude-opus-5-5/high'


class PinTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'preferences/config.yaml'
        write_defaults(self.path)

    def snapshot(self):
        return load(personal=self.path)

    def test_missing_null_pin_and_genuine_067_parser_refusal(self):
        self.assertIsNone(self.snapshot()['pinned'])
        self.assertTrue(validate_choice(self.snapshot(), [], [], choice())['allowed'])
        self.path.write_text(self.path.read_text().replace('pinned: null\n', '').replace('preferred: null\n', ''))
        absent = self.snapshot()
        self.assertEqual((absent['status'], absent['pinned'], absent['preferred']), ('valid', None, None))
        self.assertTrue(validate_choice(absent, [], [], choice())['allowed'])
        # The genuine 0.6.7 parser refuses pod/v2 rather than reading part of it.
        for document in (defaults(), {**defaults(), 'pinned': SOL}, {**defaults(), 'schema': 'pod/v1'}):
            with self.subTest(document=sorted(document)):
                refused = earlier_validate(POD_067, document)
                self.assertFalse(refused['accepted'])
                self.assertEqual(refused['code'], 'invalid_config')

    def test_preferred_and_pin_coexist_move_and_clear_atomically(self):
        self.path.write_text(self.path.read_text() + '# personal note\n')
        set_preferred(self.path, OPUS, displayed=self.snapshot())
        set_pin(self.path, SOL, displayed=self.snapshot())
        both = self.snapshot()
        self.assertEqual((both['preferred'], both['pinned']), (OPUS, SOL))
        # The pin controls while set; Preferred stays saved and is never inferred into a pin.
        self.assertEqual(validate_choice(both, [], [], choice('claude-opus-5-5', 'claude', 'high'))['code'], 'pin_mismatch')
        allowed = validate_choice(both, [], [], choice())
        self.assertEqual((allowed['route'], allowed['pinned_route'], allowed['preferred_route']), (SOL, SOL, OPUS))
        displayed = self.snapshot()
        set_pin(self.path, ASTRA, displayed=displayed)
        with self.assertRaises(PodError) as caught:
            set_pin(self.path, SOL_HIGH, displayed=displayed)
        self.assertEqual(caught.exception.code, 'config_changed_elsewhere')
        self.assertEqual(self.snapshot()['pinned'], ASTRA)
        stale = self.snapshot()
        set_route(self.path, 'codex/gpt-6-luna/low', 'disabled', displayed=stale)
        set_pin(self.path, SOL_HIGH, displayed=stale)
        set_pin(self.path, None, displayed=self.snapshot())
        cleared = self.snapshot()
        self.assertEqual((cleared['pinned'], cleared['preferred']), (None, OPUS))
        self.assertEqual(cleared['routes']['codex/gpt-6-luna/low'], 'disabled')
        self.assertTrue(validate_choice(cleared, [], [], choice('claude-opus-5-5', 'claude', 'high'))['allowed'])
        set_preferred(self.path, None, displayed=cleared)
        self.assertIsNone(self.snapshot()['preferred'])
        self.assertIn('# personal note', self.path.read_text())

    def test_creating_and_invalidating_edits_are_refused_with_distinct_reasons(self):
        set_routes(self.path, {SOL: 'disabled', SOL_HIGH: None}, displayed=self.snapshot())
        original = self.path.read_bytes()
        for field, setter in (('pin', set_pin), ('preference', set_preferred)):
            for key, state in ((SOL, 'Disabled'), (SOL_HIGH, 'Not set')):
                with self.subTest(field=field, key=key), self.assertRaises(PodError) as caught:
                    setter(self.path, key, displayed=self.snapshot())
                self.assertEqual(caught.exception.code, 'pin_ineligible' if field == 'pin' else 'preferred_ineligible')
                self.assertIn(f'Cannot make {key} the {field}: it is {state}', str(caught.exception))
                self.assertEqual(self.path.read_bytes(), original)
        set_pin(self.path, ASTRA, displayed=self.snapshot())
        set_preferred(self.path, OPUS, displayed=self.snapshot())
        original = self.path.read_bytes()
        for key, field, code in ((ASTRA, 'pin', 'pin_invalidated'), (OPUS, 'preference', 'preferred_invalidated')):
            for state, label in (('disabled', 'Disabled'), (None, 'Not set')):
                for action in (lambda: set_route(self.path, key, state, displayed=self.snapshot()),
                               lambda: set_routes(self.path, {key: state, SOL: 'enabled'}, displayed=self.snapshot())):
                    with self.subTest(key=key, state=state), self.assertRaises(PodError) as caught:
                        action()
                    self.assertEqual(caught.exception.code, code)
                    self.assertIn(f'Clear or replace the {field} {key} in the same action', str(caught.exception))
                    self.assertIn(label, str(caught.exception))
                    self.assertEqual(self.path.read_bytes(), original)
        # The same explicit action may clear or replace what it would invalidate.
        set_route(self.path, ASTRA, 'disabled', displayed=self.snapshot(), pinned='codex/gpt-6-luna/high')
        set_route(self.path, OPUS, None, displayed=self.snapshot(), preferred=None)
        moved = self.snapshot()
        self.assertEqual((moved['pinned'], moved['preferred'], moved['routes'][ASTRA]),
                         ('codex/gpt-6-luna/high', None, 'disabled'))
        self.assertNotIn(OPUS, moved['routes'])
        # Disabled intent is never re-enabled as a side effect.
        with self.assertRaises(PodError):
            edit(self.path, displayed=moved, pinned=ASTRA)
        self.assertEqual(self.snapshot()['routes'][ASTRA], 'disabled')

    def test_invalid_pin_or_preference_is_bounded_single_parse_and_preserves_bytes(self):
        for field in ('pinned', 'preferred'):
            for value, expected in [('unknown', 'unknown'), (True, '<invalid bool'), ([], '<invalid list'),
                                    ({}, '<invalid dict'), ('', '<empty'), ('x' * 4000, 'x' * 125 + '...'),
                                    ('bad\x1b\nroute', 'badroute'), ('codex/gpt-6-sol/high', 'codex/gpt-6-sol/high'),
                                    (SOL, SOL)]:
                with self.subTest(field=field, value=expected):
                    document = {**defaults(), field: value}
                    document['routes'][SOL] = 'disabled'
                    self.path.write_text(yaml.safe_dump(document))
                    original = self.path.read_bytes()
                    with patch('pod.config.yaml.load', wraps=yaml.load) as parse:
                        snapshot = self.snapshot()
                    self.assertEqual(parse.call_count, 1)
                    diagnostic = snapshot['pin_diagnostic' if field == 'pinned' else 'preferred_diagnostic']
                    self.assertTrue(diagnostic.startswith(expected))
                    self.assertEqual((snapshot['pinned'], snapshot['preferred'], snapshot['eligible']), (None, None, []))
                    self.assertEqual(snapshot['status'], 'invalid')
                    self.assertIn(expected, snapshot['errors'][0]['message'])
                    for role in ('implement', 'investigate', 'review', 'correction'):
                        self.assertFalse(validate_choice(snapshot, [], [], choice(effort='high'), role=role)['allowed'])
                    with self.assertRaises(PodError):
                        set_pin(self.path, None, displayed=snapshot)
                    self.assertEqual(self.path.read_bytes(), original)
        # Unsafe syntax is never reparsed to scrape a diagnostic pin.
        for raw in ('pinned: unknown\nroutes: [\n', 'pinned: unknown\npinned: codex/gpt-6.1-sol/high\n',
                    'pinned: &pin unknown\nroutes: *pin\n'):
            self.path.write_text(raw)
            with patch('pod.config.yaml.load', wraps=yaml.load) as parse:
                snapshot = self.snapshot()
            self.assertEqual(parse.call_count, 1)
            self.assertIsNone(snapshot['pin_diagnostic'])
            self.assertFalse(snapshot['eligible'])
            self.assertEqual(self.path.read_text(), raw)

    def test_validate_refuses_unsupported_and_non_enabled_pins(self):
        for pin, code in ((True, 'invalid_pin'), ([], 'invalid_pin'), ('gpt-6.1-sol', 'invalid_pin'),
                          ('codex/gpt-6.1-sol/native_default', 'invalid_pin'), ('codex/gpt-6-sol/high', 'invalid_pin'),
                          ('codex/gpt-6-luna/ultra', 'invalid_pin')):
            with self.subTest(pin=pin), self.assertRaises(PodError) as caught:
                validate({**defaults(), 'pinned': pin})
            self.assertEqual(caught.exception.code, code)
        sparse = {**defaults(), 'routes': {SOL: 'enabled'}, 'pinned': SOL_HIGH}
        with self.assertRaises(PodError) as caught:
            validate(sparse)
        self.assertEqual(caught.exception.code, 'pin_ineligible')

    def test_every_pod_role_obeys_the_exact_pin_despite_repository_model_rules(self):
        set_pin(self.path, SOL_HIGH, displayed=self.snapshot())
        snapshot = self.snapshot()
        for role in ('implement', 'investigate', 'review', 'writer', 'correction'):
            constraints = [{'kind': 'role_model', 'provenance': 'repository',
                            'role': role, 'value': 'claude-opus-5-5'},
                           {'kind': 'only_models', 'provenance': 'repository', 'value': ['gpt-6-astra']},
                           {'kind': 'exclude_models', 'provenance': 'repository', 'value': ['gpt-6.1-sol']}]
            with self.subTest(role=role):
                self.assertTrue(validate_choice(snapshot, constraints, [], choice(effort='high'), role=role)['allowed'])
                # No adaptive effort: every other effort of the pinned model is a mismatch.
                for effort in ('low', 'medium', 'xhigh', 'max'):
                    self.assertEqual(validate_choice(snapshot, constraints, [], choice(effort=effort), role=role)['code'],
                                     'pin_mismatch')
                self.assertEqual(validate_choice(snapshot, [], [], choice(effort='native_default'), role=role)['code'],
                                 'effort_required')
                self.assertEqual(validate_choice(snapshot, [], [], choice('gpt-6-luna', effort='high'), role=role)['code'],
                                 'pin_mismatch')
                hard = {'kind': 'only_agents', 'provenance': 'repository', 'value': ['claude']}
                self.assertEqual(validate_choice(snapshot, [hard], [], choice(effort='high'), role=role)['code'],
                                 'constraint_excluded')
                for provenance in ('user_direct', 'issue', 'worker'):
                    self.assertEqual(validate_choice(snapshot, [{**constraints[0], 'provenance': provenance}], [],
                                                     choice(effort='high'), role=role)['code'], 'constraint_excluded')
        set_pin(self.path, None, displayed=snapshot)
        self.assertEqual(validate_choice(self.snapshot(), constraints, [], choice(effort='high'), role=role)['code'],
                         'constraint_excluded')

    def test_unavailable_pin_never_falls_back_and_failures_stay_distinct(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        unavailable = {'kind': 'unavailable', 'model': 'gpt-6.1-sol', 'task': 'old',
                       'source': 'readiness timeout; cause unknown', 'recorded_at': now.isoformat()}
        self.assertTrue(failure_active(unavailable, now=now + timedelta(seconds=59)))
        self.assertFalse(failure_active(unavailable, now=now + timedelta(seconds=60)))
        self.assertTrue(failure_active({**unavailable, 'retry_after': (now + timedelta(minutes=2)).isoformat()},
                                       now=now + timedelta(seconds=60)))
        for kind in ('auth_failed', 'rate_limited', 'safety_refusal'):
            self.assertTrue(failure_active({**unavailable, 'kind': kind}, now=now + timedelta(days=20)))
        set_pin(self.path, SOL_HIGH, displayed=self.snapshot())
        held = validate_choice(self.snapshot(), [], [unavailable], choice(effort='high'), now=now)
        self.assertEqual((held['code'], held['pinned_route']), ('route_failed', SOL_HIGH))
        self.assertEqual(validate_choice(self.snapshot(), [], [unavailable], choice('gpt-6-luna', effort='high'),
                                         now=now)['code'], 'pin_mismatch')
        self.assertTrue(validate_choice(self.snapshot(), [], [unavailable], choice(effort='high'),
                                        now=now + timedelta(days=1))['allowed'])
        safety = {**unavailable, 'kind': 'safety_refusal'}
        self.assertEqual(validate_choice(self.snapshot(), [], [safety], choice(effort='high'), task='old',
                                         now=now + timedelta(days=1))['code'], 'safety_refusal')

    def test_save_failure_and_invalid_yaml_never_widen_or_drop_the_pin(self):
        set_pin(self.path, SOL, displayed=self.snapshot())
        original = self.path.read_bytes()
        with patch('pod.config._replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): set_pin(self.path, ASTRA, displayed=self.snapshot())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.snapshot()['pinned'], SOL)
        self.path.write_text('schema: [invalid\n')
        with self.assertRaises(PodError): set_pin(self.path, None, displayed=self.snapshot())
        self.assertFalse(self.snapshot()['eligible'])
        self.assertEqual(self.path.read_text(), 'schema: [invalid\n')


if __name__ == '__main__':
    unittest.main()

"""Route registry schema, exact source aliases and no-longer-supported identities."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest

from pod.catalog import (EFFORTS, by_id, format_latency, format_usd, known, load, main, match,
                         parse_route_key, route_key, route_keys, routes, supported_route, validate)
from pod.errors import PodError
from tests.common import fixture

BASES = ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5",
         "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")


class CatalogTests(unittest.TestCase):
    def test_six_current_bases_have_verified_exact_efforts(self):
        document = load()
        self.assertEqual(document['schema'], 'pod-catalog/v3')
        self.assertEqual(tuple(by_id(document)), BASES)
        for model in document['models']:
            self.assertEqual(model['efforts'], list(EFFORTS))
            self.assertEqual(model['agent'], 'claude' if model['id'].startswith('claude-') else 'codex')
        self.assertEqual(len(routes(document)), 30)
        self.assertEqual(route_keys(document)[0], 'claude/claude-opus-5-5/low')
        self.assertIn('codex/gpt-6.1-sol/xhigh', route_keys(document))
        # No benchmark measurements live in the registry.
        self.assertNotIn('benchmarks', document)
        self.assertNotIn('usd_per_task', json.dumps(document))

    def test_ultra_none_and_replaced_generations_are_not_routes(self):
        for key in ('codex/gpt-6-astra/ultra', 'codex/gpt-6.1-sol/ultra', 'codex/gpt-6-luna/none',
                    'codex/gpt-6-sol/high', 'claude/claude-sonnet-5/high', 'codex/gpt-6.1-sol/native_default',
                    'claude/gpt-6.1-sol/high', 'codex/gpt-daybreak-red-latest/high', 'codex/gpt-6.1-sol'):
            with self.subTest(key=key):
                self.assertIsNone(supported_route(key))
                self.assertNotIn(key, route_keys())
        self.assertEqual(set(known()) - set(by_id()),
                         {'claude-sonnet-5', 'gpt-6-sol', 'gpt-daybreak-blue-latest', 'gpt-daybreak-red-latest'})
        self.assertEqual(supported_route('codex/gpt-6.1-sol/max')['name'], 'GPT-6.1 Sol')
        self.assertEqual(route_key('codex', 'gpt-6.1-sol', 'high'), 'codex/gpt-6.1-sol/high')
        self.assertEqual(parse_route_key('claude/claude-opus-5-5/xhigh'),
                         {'agent': 'claude', 'model': 'claude-opus-5-5', 'effort': 'xhigh'})
        for bad in ('claude/claude-opus-5-5', 'x/claude-opus-5-5/high', 'codex/gpt-6.1-sol/ultra', 7):
            with self.subTest(bad=bad), self.assertRaises(PodError):
                parse_route_key(bad)

    def test_match_is_exact_and_keeps_qualifiers_informational(self):
        aa = 'artificial_analysis'
        self.assertEqual(match(aa, 'GPT-6.1 Sol (xhigh)'),
                         {'route': 'codex/gpt-6.1-sol/xhigh', 'model': 'gpt-6.1-sol', 'effort': 'xhigh',
                          'informational': False, 'supported': True})
        # AA's "with fallback" harness profile maps to the same exact effort; it never enables fallback.
        self.assertEqual(match(aa, 'Claude Opus 5.5 (max with fallback)')['route'], 'claude/claude-opus-5-5/max')
        self.assertEqual(match(aa, 'GPT-6 Luna (non-reasoning)'),
                         {'route': None, 'model': 'gpt-6-luna', 'effort': 'none',
                          'informational': True, 'supported': True})
        self.assertEqual(match(aa, 'GPT-6 Sol (high)'),
                         {'route': None, 'model': 'gpt-6-sol', 'effort': 'high',
                          'informational': True, 'supported': False})
        self.assertEqual(match('anthropic_models', 'claude-sonnet-5-5'),
                         {'route': None, 'model': 'claude-sonnet-5-5', 'effort': None,
                          'informational': True, 'supported': True})
        for source, name in ((aa, 'gpt-6.1 sol (xhigh)'), (aa, 'GPT-6.1 Sol (xhigh) '), (aa, 'GPT-6.1 Sol'),
                             (aa, 'GPT-6.1 Sol (ultra)'), (aa, 'GPT-5.6 Terra (max)'),
                             ('openai_models', 'GPT-6.1 Sol (xhigh)'), (aa, None), (None, 'gpt-6-luna')):
            with self.subTest(name=name):
                self.assertIsNone(match(source, name))
        # Every route of the six bases has an explicit AA alias.
        mapped = {match(aa, name)['route'] for model in load()['models']
                  for name in model['aliases'][aa]} - {None}
        self.assertEqual(mapped, set(route_keys()))

    def test_formatters_never_hide_subcent_or_missing_values(self):
        self.assertEqual(format_usd(None), 'unknown')
        self.assertEqual(format_usd(.0045), '$0.0045')
        self.assertEqual(format_usd(.37), '$0.37')
        self.assertEqual(format_usd(5.98), '$5.98')
        self.assertEqual(format_latency(None), 'unknown')
        self.assertEqual(format_latency(9.62), '9.6 s')
        self.assertEqual(format_latency(138.95), '139 s')

    def test_duplicate_keys_and_corrupt_metadata_are_rejected(self):
        with fixture() as root:
            path = root / 'catalog.json'
            path.write_text('{"schema":"pod-catalog/v3","schema":"pod-catalog/v3"}')
            with self.assertRaises(PodError):
                load(path)
        document = load()
        edits = (
            lambda d: d.update(schema='pod-catalog/v2'),
            lambda d: d.update(benchmarks={}),
            lambda d: d['models'].append(deepcopy(d['models'][0])),
            lambda d: d['models'][0].update(guidance='short'),
            lambda d: d['models'][0].update(documented_context_tokens=-1),
            lambda d: d['models'][3].update(efforts=[{'bad': 'type'}]),
            lambda d: d['models'][3].update(efforts=['low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
            lambda d: d['models'][3].update(efforts=['high', 'low']),
            lambda d: d['models'][0].update(agent='codex'),
            lambda d: d['models'][0].update(native_default='medium'),
            lambda d: d['models'][0]['sources'][0].update(url='http://invalid.example'),
            lambda d: d['models'][0]['sources'][0].update(url='https://example.invalid/models'),
            lambda d: d['models'][0]['sources'][0].update(checked='2999-01-01'),
            lambda d: d['models'][0]['guide'].update(ladder={}),
            lambda d: d['models'][0]['aliases']['artificial_analysis'].update({'X (high)': 'ultra'}),
            lambda d: d['models'][1]['aliases']['artificial_analysis'].update({'Claude Opus 5.5 (high)': 'high'}),
            lambda d: d['models'][0]['aliases'].update({'Bad Source': {}}),
            lambda d: d['models'][0]['aliases']['artificial_analysis'].update({'gpt-6-luna': None}),
            lambda d: d['not_routable'].append({**deepcopy(d['not_routable'][0]), 'id': 'claude-opus-5-5'}),
        )
        for index, edit in enumerate(edits):
            with self.subTest(edit=index):
                changed = deepcopy(document)
                edit(changed)
                with self.assertRaises(PodError):
                    validate(changed)

    def test_adding_a_model_is_a_registry_edit_and_check_reports_it(self):
        document = deepcopy(load())
        extra = deepcopy(document['models'][5])
        extra.update(id='gpt-6-nova', name='GPT-6 Nova', efforts=['low', 'medium'],
                     aliases={'artificial_analysis': {'GPT-6 Nova (low)': 'low'}})
        document['models'].append(extra)
        with fixture() as root:
            path = root / 'catalog.json'
            path.write_text(json.dumps(document))
            loaded = load(path)
            self.assertIn('codex/gpt-6-nova/medium', route_keys(loaded))
            self.assertEqual(match('artificial_analysis', 'GPT-6 Nova (low)', loaded)['route'],
                             'codex/gpt-6-nova/low')
            # The shipped registry is unaffected.
            self.assertIsNone(supported_route('codex/gpt-6-nova/low'))
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(['--check']), 0)
        self.assertIn('6 supported models, 30 routes', output.getvalue())


if __name__ == '__main__':
    unittest.main()

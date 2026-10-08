"""The one joined route projection: registry, preferences, observations and native unknown."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import socket
import unittest
from unittest.mock import patch

from pod.catalog import load as load_catalog, route_keys
from pod.config import defaults, load, set_pin, set_preferred, set_routes, write_defaults
from pod.routes import METRICS, observed, project
from tests.common import fixture, without_module

NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
SOL_HIGH = 'codex/gpt-6.1-sol/high'


def row(name, creator='OpenAI', qualifiers=(), **metrics):
    return {'name': name, 'creator': creator, 'qualifiers': list(qualifiers),
            'metrics': {key: metrics.get(key) for key in METRICS}}


def block(rows, *, retrieved='2026-10-01T16:00:00Z', status='ok', required=False, methodology=None):
    return {'url': 'https://example.invalid/models', 'attribution': 'Example', 'required': required,
            'status': status, 'retrieved_at': retrieved, 'published_at': None, 'methodology': methodology,
            'rows': rows, 'diagnostics': []}


def seen(**sources):
    return {'status': 'observed', 'origin': 'cache', 'generation': '7', 'created_at': '2026-10-01T16:00:00Z',
            'stale': False, 'age_s': 3600, 'diagnostics': [], 'sources': sources}


SNAPSHOT = seen(
    artificial_analysis=block([
        row('GPT-6.1 Sol (high)', intelligence=50, usd_per_task=0.32, output_tps=None, first_response_s=56.9,
            total_response_s=60.1, context_tokens=272000),
        row('Claude Opus 5.5 (max with fallback)', 'Anthropic', ['with fallback'], intelligence=58, usd_per_task=5.98),
        row('GPT-6 Luna (non-reasoning)', intelligence=18),
        row('GPT-6 Sol (high)', intelligence=43),
        row('GPT-7 Nova (high)', intelligence=61),
        row('Gemini 9 (high)', 'Google', intelligence=70),
    ], methodology='Artificial Analysis Intelligence Index v4.3', required=True),
    openai_models=block([row('gpt-6.1-sol', context_tokens=1000000), row('gpt-6-sol')],
                        retrieved='2026-09-20T00:00:00Z'),
)


class RouteProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.path = self.root / 'pod' / 'config.yaml'
        write_defaults(self.path)

    def projected(self, snapshot=SNAPSHOT, **kwargs):
        return project(preferences=load(personal=self.path), observations=snapshot, now=NOW, **kwargs)

    def test_route_rows_carry_identity_state_guide_and_unknown_native_access(self):
        set_routes(self.path, {'codex/gpt-6-luna/low': 'disabled', 'codex/gpt-6-luna/max': None},
                   displayed=load(personal=self.path))
        set_preferred(self.path, 'claude/claude-opus-5-5/high', displayed=load(personal=self.path))
        set_pin(self.path, SOL_HIGH, displayed=load(personal=self.path))
        projection = self.projected()
        rows = {row['key']: row for row in projection['routes']}
        self.assertEqual(list(rows), list(route_keys()))
        sol = rows[SOL_HIGH]
        self.assertEqual({key: sol[key] for key in ('agent', 'model', 'name', 'effort', 'provider', 'state',
                                                    'preferred', 'pinned', 'supported', 'discovery', 'native')},
                         {'agent': 'codex', 'model': 'gpt-6.1-sol', 'name': 'GPT-6.1 Sol', 'effort': 'high',
                          'provider': 'OpenAI', 'state': 'enabled', 'preferred': False, 'pinned': True,
                          'supported': True, 'discovery': 'supported', 'native': {'access': 'unknown'}})
        self.assertTrue(rows['claude/claude-opus-5-5/high']['preferred'])
        self.assertEqual(rows['codex/gpt-6-luna/low']['state'], 'disabled')
        self.assertEqual(rows['codex/gpt-6-luna/max']['state'], 'not_set')
        self.assertEqual(rows['codex/gpt-6-luna/max']['guide']['max'],
                         load_catalog()['max_guide'])
        self.assertIsNone(sol['guide']['max'])
        self.assertTrue(sol['guide']['use'] and sol['guidance'].startswith('OpenAI'))
        self.assertEqual(projection['summary'], {'routes': 35, 'enabled': 33, 'disabled': 1, 'not_set': 1,
                                                 'preferred': 'claude/claude-opus-5-5/high',
                                                 'pinned': SOL_HIGH, 'unmapped': 4})
        self.assertEqual(projection['native'], {'access': 'unknown'})
        # Display order and judgment belong to readers: there is no routing input here.
        text = json.dumps(projection)
        for name in ('rank', 'score', 'recommend', 'winner'):
            self.assertNotIn(name, text)

    def test_metrics_stay_paired_with_their_row_and_unknown_is_never_zero(self):
        rows = {row['key']: row for row in self.projected()['routes']}
        sol = rows[SOL_HIGH]['metrics']
        self.assertEqual(sol['intelligence']['value'], 50)
        self.assertIsNone(sol['output_tps']['value'])
        self.assertEqual({sol[name]['row'] for name in METRICS}, {'GPT-6.1 Sol (high)'})
        self.assertEqual(sol['intelligence']['source'], 'artificial_analysis')
        self.assertEqual(sol['intelligence']['methodology'], 'Artificial Analysis Intelligence Index v4.3')
        self.assertEqual(sol['intelligence']['retrieved_at'], '2026-10-01T16:00:00Z')
        opus = rows['claude/claude-opus-5-5/max']['metrics']['intelligence']
        self.assertEqual((opus['value'], opus['qualifiers']), (58, ['with fallback']))
        empty = rows['codex/gpt-6-astra/low']['metrics']
        self.assertTrue(all(empty[name]['value'] is None and empty[name]['source'] is None for name in METRICS))
        # Provider and benchmark context stay separate observations, each with its own age.
        observations = rows[SOL_HIGH]['observations']
        self.assertEqual([(entry['source'], entry['metrics']['context_tokens'], entry['retrieved_at'])
                          for entry in observations],
                         [('artificial_analysis', 272000, '2026-10-01T16:00:00Z'),
                          ('openai_models', 1000000, '2026-09-20T00:00:00Z')])
        self.assertIsNone(rows[SOL_HIGH]['documented_context_tokens'])

    def test_unmapped_rows_are_new_or_unsupported_and_never_routable(self):
        projection = self.projected()
        unmapped = {entry['row']: entry for entry in projection['unmapped']}
        self.assertEqual(set(unmapped), {'GPT-6 Luna (non-reasoning)', 'GPT-6 Sol (high)', 'GPT-7 Nova (high)',
                                         'gpt-6-sol'})
        self.assertEqual({row: (entry['discovery'], entry['model'], entry['effort'], entry['routable'])
                          for row, entry in unmapped.items()},
                         {'GPT-6 Luna (non-reasoning)': ('unsupported', 'gpt-6-luna', 'none', False),
                          'GPT-6 Sol (high)': ('unsupported', 'gpt-6-sol', 'high', False),
                          'GPT-7 Nova (high)': ('new', None, None, False),
                          'gpt-6-sol': ('unsupported', 'gpt-6-sol', None, False)})
        # A discovery never enters the route list or the saved pool.
        self.assertEqual(len(projection['routes']), 35)
        self.assertNotIn('Gemini 9 (high)', json.dumps(projection))

    def test_observation_changes_never_widen_the_pool_or_write_preferences(self):
        set_routes(self.path, {key: None for key in route_keys() if key != SOL_HIGH}, displayed=load(personal=self.path))
        before = self.path.read_bytes()
        growth = deepcopy(SNAPSHOT)
        growth['sources']['artificial_analysis']['rows'].append(row('GPT-6 Astra (max)', intelligence=53))
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
            for snapshot in (SNAPSHOT, growth, seen()):
                projection = self.projected(snapshot)
                self.assertEqual(projection['summary']['enabled'], 1)
                self.assertEqual(projection['preferences']['eligible'], [SOL_HIGH])
        self.assertEqual(self.path.read_bytes(), before)

    def test_registry_aliases_apply_at_read_time_without_refetching(self):
        document = deepcopy(load_catalog())
        nova = deepcopy(document['models'][5])
        nova.update(id='gpt-7-nova', name='GPT-7 Nova', aliases={'artificial_analysis': {'GPT-7 Nova (high)': 'high'}})
        document['models'].append(nova)
        projection = self.projected(catalog=document)
        rows = {row['key']: row for row in projection['routes']}
        self.assertEqual(rows['codex/gpt-7-nova/high']['metrics']['intelligence']['value'], 61)
        # A route the registry adds is not set, so the saved authorization did not grow.
        self.assertEqual(rows['codex/gpt-7-nova/high']['state'], 'not_set')
        self.assertNotIn('GPT-7 Nova (high)', {entry['row'] for entry in projection['unmapped']})

    def test_sources_keep_their_own_age_and_stale_marker(self):
        sources = self.projected()['observations']['sources']
        self.assertFalse(sources['artificial_analysis']['stale'])
        self.assertTrue(sources['openai_models']['stale'])
        self.assertEqual(sources['artificial_analysis']['rows'], 6)
        self.assertEqual(sources['openai_models']['retrieved_at'], '2026-09-20T00:00:00Z')

    def test_unavailable_or_unknown_observations_still_list_every_supported_route(self):
        with without_module('pod.observations'):
            missing = observed(now=NOW)
        self.assertEqual(missing['status'], 'unavailable')
        projection = self.projected(missing)
        self.assertEqual((projection['observations']['status'], len(projection['routes']), projection['unmapped']),
                         ('unavailable', 35, []))
        with patch('pod.observations.load', return_value={'origin': 'unknown', 'snapshot': None, 'diagnostics': ['x']}):
            unknown = observed(now=NOW)
        self.assertEqual((unknown['status'], unknown['origin'], unknown['diagnostics']), ('unknown', 'unknown', ['x']))
        bundled = observed(now=NOW)
        self.assertEqual((bundled['status'], bundled['origin']), ('observed', 'bundled'))
        self.assertGreaterEqual(sum(row['metrics']['intelligence']['value'] is not None
                                    for row in self.projected(bundled)['routes']), 25)

    def test_an_already_read_view_converts_through_the_same_observed_function(self):
        # The workspace reads current and previous under one lock and passes that view here, so the
        # observation shape has one implementation.
        from pod import observations
        view = observations.load(now=NOW)
        self.assertEqual(observed(view=view), observed(now=NOW))
        self.assertEqual(observed(view={'origin': 'unknown', 'snapshot': None, 'diagnostics': ['x']})['status'],
                         'unknown')

    def test_invalid_or_setup_required_preferences_show_no_enabled_route(self):
        self.path.write_text('schema: pod/v1\nselection: all\nmodels: {}\nworkers: {max_active: 2}\n')
        projection = self.projected()
        self.assertEqual(projection['preferences']['status'], 'setup_required')
        self.assertEqual(projection['summary']['enabled'], 0)
        self.assertEqual(projection['preferences']['setup']['from_schema'], 'pod/v1')
        self.path.write_text(json.dumps({**defaults(), 'pinned': 'codex/gpt-6-sol/high'}))
        invalid = self.projected()
        self.assertEqual((invalid['preferences']['status'], invalid['preferences']['pin_diagnostic']),
                         ('invalid', 'codex/gpt-6-sol/high'))
        self.assertEqual(invalid['summary']['enabled'], 0)


if __name__ == '__main__':
    unittest.main()

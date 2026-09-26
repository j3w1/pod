"""Catalog schema, AA pairing, and optional-reference checks."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

from pod.catalog import (IDS, age, benchmark_warnings, by_id, format_latency,
                         format_usd, guide_projection, load, main, ranks, record,
                         records, reference_rows, validate)
from pod.errors import PodError
from tests.common import fixture


class CatalogTests(unittest.TestCase):
    def test_six_base_models_and_reference_rank_are_coherent(self):
        document = load()
        self.assertEqual(document['schema'], 'pod-catalog/v2')
        self.assertEqual(tuple(by_id(document)), IDS)
        self.assertEqual(document['benchmarks']['captured'], '2026-09-25')
        self.assertEqual(ranks(document), dict(zip(IDS, (1, 2, 6, 3, 4, 5))))
        self.assertEqual(age(document, today=date(2026, 9, 25)), 0)
        self.assertEqual(len(guide_projection(document)), 6)
        self.assertEqual(sorted(row['guide']['coding_order'] for row in document['models']), list(range(1, 7)))

    def test_variant_metrics_stay_together_and_ultra_is_read_only(self):
        document = load()
        self.assertEqual(sum(len(rows) for rows in document['benchmarks']['models'].values()), 33)
        for model_id in IDS:
            model = by_id(document)[model_id]
            selected = record(document, model_id, model['guide']['profile'])
            self.assertIn(selected, records(document, model_id))
            self.assertEqual(reference_rows(document)[model_id], selected)
            self.assertEqual(selected['effort'], model['guide']['profile'])
        self.assertEqual(record(document, 'claude-opus-5-5', 'high')['first_chunk_s'], 34.88)
        self.assertEqual(record(document, 'gpt-6-luna', 'max')['first_chunk_s'], 138.95)
        self.assertIn('ultra', by_id(document)['gpt-6-sol']['efforts'])
        self.assertNotIn('ultra', by_id(document)['gpt-6-luna']['efforts'])
        self.assertTrue(all(row['effort'] != 'ultra' for rows in document['benchmarks']['models'].values() for row in rows))

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
            path.write_text('{"schema":"pod-catalog/v2","schema":"pod-catalog/v2"}')
            with self.assertRaises(PodError):
                load(path)
        document = load()
        edits = (
            lambda d: d['models'].append(deepcopy(d['models'][0])),
            lambda d: d['models'][0].update(guidance='short'),
            lambda d: d['models'][3].update(documented_context_tokens=872000),
            lambda d: d['models'][3].update(efforts=[{'bad':'type'}]),
            lambda d: d['models'][0]['sources'][0].update(url='http://invalid.example'),
            lambda d: d['models'][0]['guide'].update(coding_order=1),
            lambda d: d['models'][0]['guide']['ladder'].update(escalation='ultra'),
            lambda d: d['benchmarks'].update(captured='2999-01-01'),
        )
        for edit in edits:
            with self.subTest(edit=edit):
                changed = deepcopy(document)
                edit(changed)
                with self.assertRaises(PodError):
                    validate(changed)

    def test_reference_gaps_are_unknown_without_invalidating_supported_models(self):
        document = load()
        with fixture() as root:
            path = root / 'catalog.json'
            for change in ('missing_group', 'missing_score', 'invalid_price', 'missing_profile'):
                with self.subTest(change=change):
                    copy = deepcopy(document)
                    rows = copy['benchmarks']['models']['gpt-6-luna']
                    if change == 'missing_group':
                        del copy['benchmarks']['models']['gpt-6-luna']
                    elif change == 'missing_score':
                        del rows[0]['intelligence']
                    elif change == 'invalid_price':
                        rows[0]['usd_per_task'] = -1
                    else:
                        rows[:] = [row for row in rows if row['effort'] != 'max']
                    path.write_text(json.dumps(copy))
                    loaded = load(path)
                    self.assertEqual(tuple(by_id(loaded)), IDS)
                    unknown = reference_rows(loaded)['gpt-6-luna']
                    self.assertIsNone(unknown['intelligence'])
                    self.assertIsNone(unknown['usd_per_task'])
                    self.assertTrue(benchmark_warnings(loaded))
                    with patch('pod.catalog.load', return_value=loaded), redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(['--check']), 0)
                    self.assertIn('Benchmark warning:', output.getvalue())

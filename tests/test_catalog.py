from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

from pod.catalog import IDS, age, benchmark_warnings, by_id, load, main, ranks, reference_rows, validate
from pod.errors import PodError
from tests.common import fixture


class CatalogTests(unittest.TestCase):
    def test_six_base_models_and_reference_rank_are_coherent(self):
        document = load()
        self.assertEqual(tuple(by_id(document)), IDS)
        self.assertEqual(ranks(document), dict(zip(IDS, (1, 2, 5, 2, 4, 6))))
        rows = reference_rows(document)
        self.assertEqual(rows['claude-opus-5-5']['first_chunk_s'], None)
        self.assertEqual(rows['gpt-6-luna']['usd_per_task'], .07)
        self.assertEqual(age(document, today=date(2026, 9, 25)), 1)
        self.assertTrue(all(35 <= len(row['guidance'].split()) <= 55 for row in document['models']))
        self.assertTrue(all(row['examples'] for row in document['models']))
        self.assertTrue(all(row['id'] not in row['efforts'] for row in document['models']))

    def test_variant_metrics_stay_together_and_ultra_is_read_only(self):
        document = load()
        for model_id in IDS:
            group = document['reference_benchmark']['models'][model_id]
            self.assertEqual(group['reference_variant'], group['variants'][0]['profile'])
        self.assertIn('ultra', by_id(document)['gpt-6-sol']['efforts'])
        self.assertNotIn('ultra', by_id(document)['gpt-6-luna']['efforts'])
        self.assertIsNone(by_id(document)['gpt-6-sol']['documented_context_tokens'])

    def test_duplicate_keys_and_corrupt_metadata_are_rejected(self):
        with fixture() as root:
            path = root / 'catalog.json'
            path.write_text('{"schema":"pod-catalog/v1","schema":"pod-catalog/v1"}')
            with self.assertRaises(PodError):
                load(path)
        document = load()
        for edit in (
            lambda d: d['models'].append(deepcopy(d['models'][0])),
            lambda d: d['models'][0].update(guidance='short'),
            lambda d: d['models'][3].update(documented_context_tokens=872000),
            lambda d: d['models'][3].update(efforts=[{'bad':'type'}]),
            lambda d: d['models'][0]['sources'][0].update(url='http://invalid.example'),
        ):
            with self.subTest(edit=edit):
                changed = deepcopy(document)
                edit(changed)
                with self.assertRaises(PodError):
                    validate(changed)

    def test_reference_gaps_are_unknown_without_invalidating_supported_models(self):
        document=load()
        with fixture() as root:
            path=root/'catalog.json'
            for change in ('missing_group','missing_score','invalid_price','missing_benchmark'):
                with self.subTest(change=change):
                    copy=deepcopy(document)
                    if change=='missing_group':
                        del copy['reference_benchmark']['models']['gpt-6-luna']
                    elif change=='missing_score':
                        del copy['reference_benchmark']['models']['gpt-6-luna']['variants'][0]['intelligence']
                    elif change=='invalid_price':
                        copy['reference_benchmark']['models']['gpt-6-luna']['variants'][0]['usd_per_task']=-1
                    else:
                        del copy['reference_benchmark']
                    path.write_text(json.dumps(copy))
                    loaded=load(path)
                    self.assertEqual(tuple(by_id(loaded)),IDS)
                    unknown=reference_rows(loaded)['gpt-6-luna']
                    self.assertIsNone(unknown['intelligence'])
                    self.assertIsNone(unknown['usd_per_task'])
                    self.assertIsNone(unknown['first_chunk_s'])
                    self.assertEqual(set(ranks(loaded).values()),{None})
                    self.assertTrue(benchmark_warnings(loaded))
                    with patch('pod.catalog.load',return_value=loaded), redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(['--check']),0)
                    self.assertIn('Benchmark warning:',output.getvalue())

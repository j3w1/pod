from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest

from pod.catalog import IDS, age, by_id, load, ranks, reference_rows, validate
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
            lambda d: d['reference_benchmark']['models']['gpt-6-sol']['variants'][0].update(usd_per_task=-1),
            lambda d: d['models'][3].update(documented_context_tokens=872000),
            lambda d: d['models'][3].update(efforts=[{'bad':'type'}]),
            lambda d: d['models'][0]['sources'][0].update(url='http://invalid.example'),
        ):
            with self.subTest(edit=edit):
                changed = deepcopy(document)
                edit(changed)
                with self.assertRaises(PodError):
                    validate(changed)

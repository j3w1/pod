"""Pure frame and state tests for the model-pool editor."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from pod.catalog import IDS, by_id, format_latency, format_usd, load, record
from pod.config import DEFAULT, load as preferences, set_model
from pod.term import capabilities, display_width
from pod.tui_render import frame, summary
from pod.tui_state import initial, reduce, refresh, visible_ids, with_notice


class TuiRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.yaml'
        import yaml
        self.path.write_text(yaml.safe_dump(DEFAULT, sort_keys=False))
        self.catalog = load()
        self.state = initial(self.catalog, preferences(personal=self.path))
        self.now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        self.caps = capabilities({'LANG': 'C.UTF-8', 'TERM': 'xterm-256color'}, has_colors=True)

    def picture(self, state=None, size=(80, 24), caps=None):
        return frame(state or self.state, *size, caps or self.caps, self.now)

    def test_profile_consistency_table_effort_and_expanded(self):
        for model_id in IDS:
            model = by_id(self.catalog)[model_id]
            guide = model['guide']
            selected = record(self.catalog, model_id, guide['profile'])
            picture = self.picture(replace(self.state, focus_id=model_id), (160,45)).plain
            row = next(line for line in picture.splitlines() if model['name'] in line and 'Available' in line)
            self.assertIn(selected['profile'], row)
            self.assertIn(str(selected['intelligence']), row)
            self.assertIn(format_usd(selected['usd_per_task']), row)
            self.assertIn(format_latency(selected['first_chunk_s']), row)
            expanded = self.picture(replace(self.state, focus_id=model_id, expanded=True), (100,30)).plain
            for effort in guide['ladder'].values():
                metric = record(self.catalog, model_id, effort)
                self.assertIn(format_usd(metric['usd_per_task']), expanded)

    def test_sorts_keep_identity_and_unknown_last(self):
        self.assertEqual(visible_ids(self.state)[0], 'gpt-6-sol')
        self.assertEqual(visible_ids(replace(self.state, sort_index=1))[0], 'claude-fable-5-1')
        self.assertEqual(visible_ids(replace(self.state, sort_index=2))[0], 'claude-opus-5-5')
        self.assertEqual(visible_ids(replace(self.state, sort_index=3))[0], 'gpt-6-luna')
        self.assertEqual(visible_ids(replace(self.state, sort_index=4))[0], 'gpt-6-astra')
        moved,_ = reduce(self.state, 's')
        self.assertEqual(moved.focus_id, self.state.focus_id)
        filtered,_ = reduce(moved, 'f')
        self.assertEqual(filtered.hidden_focus_id, 'claude-opus-5-5' if filtered.focus_id != 'claude-opus-5-5' else None)

    def test_browsing_has_no_mutation_effect(self):
        for key in ('s','f','/','?', 'ENTER', 'ESC', 'PAGE_DOWN'):
            with self.subTest(key=key):
                _, effect = reduce(self.state, key)
                self.assertIsNone(effect)

    def test_all_six_rows_and_widths(self):
        for size in ((160,45),(100,30),(80,24),(60,20),(40,12)):
            with self.subTest(size=size):
                picture = self.picture(size=size)
                self.assertEqual(len(picture.lines), size[1])
                self.assertTrue(all(display_width(line.text) <= size[0]-1 for line in picture.lines))
                for model in self.catalog['models']:
                    self.assertIn(model['name'], picture.plain)
                self.assertIn('Details', picture.plain)
        self.assertIn('│', self.picture(size=(160,45)).plain)
        self.assertNotIn('│', self.picture(size=(100,30)).plain)

    def test_sections_states_and_read_only(self):
        text = self.picture(size=(160,45)).plain
        for heading in ('BEST FOR','USE WHEN','QUICK / NORMAL / HARD / ESCALATION','TRADE-OFF',
                        'EXAMPLE','YOUR PREFERENCE','RUNTIME / ACCESS','BENCHMARK SOURCE'):
            self.assertIn(heading,text)
        self.assertIn('Model access: not',text)
        self.assertIn('verified by Pod',text)
        self.assertIn('not total task duration',text)
        bad = refresh(self.state,{**self.state.preferences,'errors':[{'code':'invalid_yaml','message':'Bad YAML'}]})
        self.assertIn('READ-ONLY',self.picture(bad).plain)
        self.assertIsNone(reduce(bad,'SPACE')[1])

    def test_spans_and_ascii(self):
        picture = self.picture(size=(80,24))
        roles = {span.role for line in picture.lines for span in line.styled}
        self.assertTrue({'body','heading','label','metric','badge_available','focus'} <= roles)
        ascii_caps = capabilities({'LC_ALL':'C','TERM':'linux','NO_COLOR':'1'},has_colors=True)
        self.assertFalse(ascii_caps.color)
        self.assertTrue(self.picture(caps=ascii_caps).plain.isascii())

    def test_save_effect_summary_and_notice(self):
        _, effect = reduce(self.state,'SPACE')
        self.assertEqual((effect.kind,effect.model_id,effect.value),('set_model','claude-opus-5-5','preferred'))
        self.assertEqual(reduce(self.state,'r')[1].value,'all')
        self.assertIn('6 eligible models',summary(self.state.preferences))
        self.assertIn('Saved just now',self.picture(with_notice(self.state,'Saved just now')).lines[0].text)

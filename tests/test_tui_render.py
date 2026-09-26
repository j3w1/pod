"""Pure frame and state tests for the model-pool editor."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from pod.catalog import IDS, by_id, format_latency, format_usd, load, record
from pod.config import DEFAULT, load as preferences
from pod.term import capabilities, display_width
from pod.tui_render import SPLIT_COLUMNS, frame, summary
from pod.tui_state import initial, reduce, refresh, visible_ids, with_notice, with_viewport

SECTIONS = ('BEST FOR', 'USE WHEN', 'TRADE-OFF', 'EXAMPLE', 'YOUR PREFERENCE', 'RUNTIME / ACCESS',
            'BENCHMARK SOURCE')
STAGES = ('QUICK', 'NORMAL', 'HARD', 'ESCALATION')


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
        return frame(state or self.state, *size, caps or self.caps, self.now, strict=True)

    def table_line(self, picture, name):
        """The table half of the line naming a model (the split layout adds Details on the right)."""
        for line in picture.plain.splitlines():
            left = line.split('│')[0]
            if name in left and ('Available' in left or 'Preferred' in left or 'Disabled' in left):
                return left
        self.fail(f'no table row for {name}')

    def test_profile_consistency_table_effort_and_expanded(self):
        for model_id in IDS:
            model = by_id(self.catalog)[model_id]
            guide = model['guide']
            selected = record(self.catalog, model_id, guide['profile'])
            row = self.table_line(self.picture(replace(self.state, focus_id=model_id), (160, 45)), model['name'])
            self.assertIn(selected['profile'], row)
            self.assertIn(str(selected['intelligence']), row)
            self.assertIn(format_usd(selected['usd_per_task']), row)
            self.assertIn(format_latency(selected['first_chunk_s']), row)
            details = self.picture(replace(self.state, focus_id=model_id), (100, 30)).plain
            for effort in guide['ladder'].values():
                metric = record(self.catalog, model_id, effort)
                ladder = next(line for line in details.splitlines()
                              if line.split()[1:2] == [effort] and line.split()[0] in STAGES)
                self.assertIn(f"AA {metric['intelligence']}", ladder)
                self.assertIn(format_usd(metric['usd_per_task']) + '/task', ladder)
                self.assertIn(format_latency(metric['first_chunk_s']), ladder)
            expanded = self.picture(replace(self.state, focus_id=model_id, expanded=True), (100, 40)).plain
            for effort in guide['ladder'].values():
                self.assertIn(format_usd(record(self.catalog, model_id, effort)['usd_per_task']), expanded)

    def test_every_size_fits_without_overflow(self):
        """Strict frames prove the layout for every width and height, including the split threshold."""
        views = [self.state, replace(self.state, expanded=True), replace(self.state, help_open=True),
                 replace(self.state, query='zzz'), with_notice(self.state, 'Not saved — disk full; file unchanged')]
        for cols in range(40, 201, 3):
            for rows in (12, 20, 24, 30, 36, 45):
                for view in views:
                    with self.subTest(cols=cols, rows=rows, view=view.expanded or view.help_open or view.query):
                        picture = self.picture(view, (cols, rows))
                        self.assertEqual(len(picture.lines), rows)
        for cols in (119, 120, 121, SPLIT_COLUMNS - 1, SPLIT_COLUMNS):
            self.picture(size=(cols, 36))

    def test_standard_size_shows_every_section_for_every_model(self):
        for model_id in IDS:
            text = self.picture(replace(self.state, focus_id=model_id)).plain
            with self.subTest(model=model_id):
                for heading in SECTIONS + STAGES:
                    self.assertIn(heading, text)
                self.assertNotIn('more: Page Down', text)
                self.assertNotIn('…', text.split('Details')[1])

    def test_sorts_keep_identity_and_unknown_last(self):
        self.assertEqual(visible_ids(self.state)[0], 'gpt-6-sol')
        self.assertEqual(visible_ids(replace(self.state, sort_index=1))[0], 'claude-fable-5-1')
        self.assertEqual(visible_ids(replace(self.state, sort_index=2))[0], 'claude-opus-5-5')
        self.assertEqual(visible_ids(replace(self.state, sort_index=3))[0], 'gpt-6-luna')
        self.assertEqual(visible_ids(replace(self.state, sort_index=4))[0], 'gpt-6-astra')
        moved, _ = reduce(self.state, 's')
        self.assertEqual(moved.focus_id, self.state.focus_id)
        filtered, _ = reduce(moved, 'f')
        self.assertEqual(filtered.hidden_focus_id,
                         'claude-opus-5-5' if filtered.focus_id != 'claude-opus-5-5' else None)

    def test_browsing_has_no_mutation_effect(self):
        for key in ('s', 'f', '/', '?', 'ENTER', 'ESC', 'PAGE_DOWN', 'UP', 'DOWN'):
            with self.subTest(key=key):
                _, effect = reduce(self.state, key)
                self.assertIsNone(effect)

    def pages(self, size, state=None):
        """Page Down to the end the way the loop does, feeding each frame's viewport back."""
        state = state or self.state
        frames = []
        for _ in range(60):
            picture = self.picture(state, size)
            state = with_viewport(state, picture.scroll, picture.page)
            if frames and picture.scroll == frames[-1].scroll:
                return state, frames, '\n'.join(frame.plain for frame in frames)
            frames.append(picture)
            state, effect = reduce(state, 'PAGE_DOWN')
            self.assertIsNone(effect)
        self.fail(f'paging never reached the end at {size}')

    def test_paging_reaches_every_details_line_at_any_height(self):
        for size in ((40, 12), (80, 12), (40, 18), (40, 24), (80, 24), (60, 20), (160, 20)):
            with self.subTest(size=size):
                state, frames, text = self.pages(size)
                # Each page starts no later than where the previous page's content ended.
                for before, after in zip(frames, frames[1:]):
                    self.assertLessEqual(after.scroll, before.scroll + before.page)
                    self.assertGreater(after.scroll, before.scroll)
                self.assertNotIn('more: Page Down', frames[-1].plain)
                flat = ' '.join(text.replace('│', ' ').split())
                for heading in SECTIONS + STAGES:
                    self.assertIn(heading, flat)
                if len(frames) > 1:
                    self.assertEqual(reduce(state, 'PAGE_UP')[0].detail_scroll,
                                     max(0, frames[-1].scroll - frames[-1].page))

    def test_sixteen_colour_light_palette_keeps_essential_roles_readable(self):
        from unittest.mock import patch
        from pod import tui
        from pod.term import Capabilities
        pairs = {}
        with patch.multiple(tui.curses, start_color=lambda: None, use_default_colors=lambda: None,
                            init_pair=lambda index, fg, bg: pairs.__setitem__(index, fg),
                            color_pair=lambda index: index << 8, COLORS=16, create=True):
            for light in (True, False):
                pairs.clear()
                tui._styles(Capabilities(ascii_only=False, color=True, light_background=light))
                faint = {tui.curses.COLOR_YELLOW, tui.curses.COLOR_GREEN, tui.curses.COLOR_CYAN,
                         tui.curses.COLOR_WHITE} if light else {tui.curses.COLOR_BLACK}
                with self.subTest(light=light):
                    self.assertTrue(pairs)
                    self.assertFalse(faint & set(pairs.values()))

    def test_all_six_rows_and_layouts(self):
        for size in ((160, 45), (140, 40), (120, 36), (100, 30), (80, 24), (60, 20), (40, 12)):
            with self.subTest(size=size):
                picture = self.picture(size=size)
                self.assertTrue(all(display_width(line.text) <= size[0] - 1 for line in picture.lines))
                for model in self.catalog['models']:
                    self.assertIn(model['name'], picture.plain)
                self.assertIn('Details', picture.plain)
                self.assertEqual('│' in picture.plain, size[0] >= SPLIT_COLUMNS)
        wide = self.picture(size=(160, 45)).plain
        self.assertIn('POOL', wide)
        self.assertIn('QUICK / NORMAL / HARD / ESCALATION', wide)

    def test_sections_states_and_read_only(self):
        text = ' '.join(self.picture(size=(160, 45)).plain.replace('│', ' ').split())
        for heading in SECTIONS + ('QUICK / NORMAL / HARD / ESCALATION',):
            self.assertIn(heading, text)
        self.assertIn('Model access: not verified by Pod', text)
        self.assertIn('not your subscription cost', text)
        self.assertIn("running coordinator's model is unchanged", text)
        bad = refresh(self.state, {**self.state.preferences, 'errors': [{'code': 'invalid_yaml', 'message': 'Bad YAML'}]})
        self.assertIn('READ-ONLY', self.picture(bad).plain)
        self.assertIsNone(reduce(bad, 'SPACE')[1])

    def test_spans_ascii_and_focus_marker(self):
        picture = self.picture(size=(80, 24))
        roles = {span.role for line in picture.lines for span in line.styled}
        self.assertTrue({'body', 'heading', 'label', 'value', 'metric', 'advisory', 'key', 'title',
                         'badge_available', 'focus'} <= roles)
        focused = [line for line in picture.lines if line.styled[0].role == 'focus']
        self.assertEqual(len(focused), 1)
        self.assertIn('Claude Opus 5.5', focused[0].text)
        ascii_caps = capabilities({'LC_ALL': 'C', 'TERM': 'linux', 'NO_COLOR': '1'}, has_colors=True)
        self.assertFalse(ascii_caps.color)
        for size in ((80, 24), (160, 45), (40, 12)):
            self.assertTrue(self.picture(size=size, caps=ascii_caps).plain.isascii())

    def test_interpreter_coerced_c_locale_stays_ascii(self):
        """PEP 538 sets LC_CTYPE=C.UTF-8 for a LANG=C session; the terminal still declared C."""
        coerced = capabilities({'LANG': 'C', 'LC_CTYPE': 'C.UTF-8', 'TERM': 'xterm-256color'})
        self.assertTrue(coerced.ascii_only)
        self.assertTrue(capabilities({'LC_CTYPE': 'C.UTF-8', 'TERM': 'xterm-256color'}).ascii_only)
        self.assertFalse(capabilities({'LANG': 'C.UTF-8', 'LC_CTYPE': 'C.UTF-8', 'TERM': 'xterm'}).ascii_only)
        self.assertFalse(capabilities({'LC_ALL': 'C.UTF-8', 'LANG': 'C', 'TERM': 'xterm'}).ascii_only)
        self.assertFalse(capabilities({'LANG': 'en_US.UTF-8', 'TERM': 'xterm'}).ascii_only)

    def test_footer_is_descriptive_and_fits(self):
        self.assertIn('Space change state', self.picture(size=(100, 30)).lines[-1].text)
        self.assertIn('s sort: Recommended', self.picture(size=(100, 30)).lines[-1].text)
        compact = self.picture(size=(40, 12)).lines[-1].text
        for fragment in ('Space', 'help', 'quit'):
            self.assertIn(fragment, compact)

    def test_save_effect_summary_and_notice(self):
        _, effect = reduce(self.state, 'SPACE')
        self.assertEqual((effect.kind, effect.model_id, effect.value), ('set_model', 'claude-opus-5-5', 'preferred'))
        self.assertEqual(reduce(self.state, 'r')[1].value, 'all')
        self.assertIn('6 eligible models', summary(self.state.preferences))
        self.assertIn('Saved just now', self.picture(with_notice(self.state, 'Saved just now')).lines[0].text)
        failed = self.picture(with_notice(self.state, 'Not saved — busy; file unchanged')).lines[0]
        self.assertIn('error', {span.role for span in failed.styled})

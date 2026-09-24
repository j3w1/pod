from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest

from pod.catalog import IDS, load as catalog
from pod.config import load as preferences, write_defaults
from pod.config import set_model
from pod.term import Capabilities, capabilities, clean, display_width, safe_text, elide_middle
from pod.tui_render import AA_CLARIFICATION, AA_URL, frame, summary
from pod.tui_state import initial, reduce, refresh, visible_ids, with_runtime
from tests.common import fixture

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


class TuiRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixture(); self.root=self.temp.__enter__(); self.addCleanup(self.temp.__exit__,None,None,None)
        self.path=self.root/'pod'/'config.yaml'; write_defaults(self.path)
        self.state=initial(catalog(),preferences(personal=self.path))
        self.caps=capabilities({'TERM':'xterm-256color','LANG':'en_US.UTF-8'},has_colors=True)

    def picture(self,state=None,size=(80,24),caps=None):
        return frame(state or self.state,*size,caps or self.caps,NOW)

    def test_all_six_focus_driven_details_change_without_enter(self):
        for model_id in IDS:
            with self.subTest(model=model_id):
                state=replace(self.state,focus_id=model_id)
                text=self.picture(state).plain
                model=next(row for row in state.catalog['models'] if row['id']==model_id)
                self.assertIn('Details',text)
                self.assertIn(model['name'],text)
                self.assertIn('Pod example:',text)
                self.assertIn(model_id,text)
                self.assertIn(model['guidance'], ' '.join(text.split()))
                self.assertIn('context native default',text)
                self.assertIn('AA ',text)
        first=self.picture(replace(self.state,focus_id=IDS[0])).plain
        second=self.picture(replace(self.state,focus_id=IDS[1])).plain
        self.assertNotEqual(first,second)

    def test_reference_metrics_rank_age_attribution_and_missing_value(self):
        text=self.picture().plain
        self.assertIn('1/6',text)
        self.assertIn('2=/6',text)
        self.assertIn('6/6',text)
        self.assertIn('max with fallback',text)
        self.assertIn('$5.98/task',text)
        self.assertIn('—',text)
        self.assertIn('Benchmark reference',text)
        self.assertIn('2026-09-24',text)
        self.assertIn('1 day',text)
        self.assertIn(AA_CLARIFICATION.split('.')[0],' '.join(text.split()))
        self.assertIn(AA_URL,text)
        self.assertIn('Runs through your connected Codex / Claude Code sessions.',text)

    def test_sort_filter_and_search_preserve_model_identity_without_writes(self):
        before=self.path.read_bytes()
        state=replace(self.state,focus_id='gpt-6-sol')
        for key in ('s','s','f','f','f'):
            state,effect=reduce(state,key)
            self.assertIsNone(effect)
        self.assertEqual(state.focus_id,'gpt-6-sol')
        self.assertIn(state.focus_id,visible_ids(state))
        state,_=reduce(state,'/')
        for key in 'not-a-model': state,_=reduce(state,key)
        self.assertEqual(visible_ids(state),[])
        state,_=reduce(state,'ENTER')
        held,effect=reduce(state,'SPACE')
        self.assertIsNone(effect)
        self.assertIn('No models match',held.notice)
        self.assertIn('No models match',self.picture(held).plain)
        state,_=reduce(held,'ESC')
        self.assertTrue(visible_ids(state))
        self.assertEqual(self.path.read_bytes(),before)

    def test_sort_orders_ties_and_missing_latency_last(self):
        state=replace(self.state,sort_index=1)
        ranked=visible_ids(state)
        self.assertEqual(ranked[:3],['claude-opus-5-5','claude-fable-5-1','gpt-6-astra'])
        prices=visible_ids(replace(state,sort_index=2))
        self.assertEqual(prices[0],'gpt-6-luna')
        latencies=visible_ids(replace(state,sort_index=3))
        self.assertEqual(latencies[-1],'claude-opus-5-5')

    def test_state_actions_are_effects_and_all_mode_has_saved_message(self):
        focused=replace(self.state,focus_id='gpt-6-sol')
        unchanged,effect=reduce(focused,'SPACE')
        self.assertEqual(unchanged,focused)
        self.assertEqual((effect.kind,effect.model_id,effect.value),('set_model','gpt-6-sol','preferred'))
        _,mode=reduce(focused,'r')
        self.assertEqual((mode.kind,mode.value),('set_mode','all'))
        all_mode=refresh(focused,{**focused.preferences,'mode':'all'})
        self.assertIn('your choices are saved (r restores)',self.picture(all_mode).plain)
        self.assertEqual(reduce(all_mode,'r')[1].value,'custom')
        remembered=replace(all_mode,focus_id='gpt-6-luna',preferences={**all_mode.preferences,
                             'saved':{**all_mode.preferences['saved'],'gpt-6-luna':'disabled'},
                             'effective':{**all_mode.preferences['effective'],'gpt-6-luna':'available'}})
        self.assertEqual(reduce(remembered,'SPACE')[1].value,'available')
        invalid=refresh(focused,{**focused.preferences,'errors':[{'code':'invalid'}]})
        self.assertIsNone(reduce(invalid,'SPACE')[1])
        self.assertIn('read-only',self.picture(invalid).plain)

    def test_layouts_short_narrow_tiny_and_expansion(self):
        normal=self.picture(size=(80,24))
        self.assertEqual(len(normal.lines),24)
        self.assertIn('Runtime',normal.lines[2].text)
        narrow=self.picture(size=(60,20))
        self.assertEqual(len(narrow.lines),20)
        self.assertNotIn('Runtime',narrow.lines[2].text)
        self.assertNotIn('1st chunk',narrow.lines[2].text)
        smallest=self.picture(size=(40,15))
        self.assertEqual(len(smallest.lines),15)
        self.assertIn('Details',smallest.plain)
        self.assertNotIn('Rank',smallest.lines[2].text)
        tiny=self.picture(size=(39,11))
        self.assertIn('Terminal too small',tiny.plain)
        expanded=self.picture(replace(self.state,expanded=True))
        self.assertIn('[expanded]',expanded.plain)
        self.assertIn('Documented context',expanded.plain)
        self.assertIn('Official source:',expanded.plain)
        self.assertIn('max with fallback',expanded.plain)
        help_frame=self.picture(replace(self.state,help_open=True))
        self.assertIn('Help:',help_frame.plain)
        self.assertIn('Space cycles',help_frame.plain)
        help_short=replace(self.state,help_open=True,help_scroll=12)
        self.assertIn('artificialanalysis.ai',self.picture(help_short,size=(40,15)).plain)

    def test_ascii_monochrome_and_sanitization(self):
        ascii_caps=capabilities({'LC_ALL':'C','TERM':'linux','NO_COLOR':'1'},has_colors=True)
        self.assertTrue(ascii_caps.ascii_only)
        self.assertFalse(ascii_caps.color)
        text=self.picture(caps=ascii_caps).plain
        self.assertTrue(text.isascii())
        self.assertIn('>+ Available',text)
        self.assertNotIn('\x1b',text)
        self.assertEqual(clean('safe\x1b[31m\u202eBAD\x7f'),'safe[31mBAD')
        self.assertTrue(safe_text('café',ascii_caps).isascii())
        self.assertEqual(display_width('海'),2)

    def test_plain_summary_reports_validity_without_ansi(self):
        good=summary(self.state.preferences)
        self.assertIn('6 eligible models',good)
        self.assertNotIn('\x1b',good)
        bad=summary({**self.state.preferences,'errors':[{'code':'invalid'}]})
        self.assertIn('need attention',bad)

    def test_empty_custom_pool_keeps_coordinator_work_available(self):
        for model_id in IDS:
            set_model(self.path,model_id,'disabled',displayed=preferences(personal=self.path))
        empty=refresh(self.state,preferences(personal=self.path))
        self.assertEqual(empty.preferences['eligible'],[])
        self.assertIn('Delegation disabled — coordinator work remains available',
                      self.picture(empty).plain)
        self.assertEqual(reduce(empty,'SPACE')[1].value,'available')

    def test_sparse_custom_map_has_explicit_not_set_state(self):
        self.path.write_text('schema: pod/v1\nselection: custom\n'
                             'models: {gpt-6-sol: available}\nworkers: {max_active: 2}\n')
        sparse=refresh(self.state,preferences(personal=self.path))
        self.assertIn('Not set (not eligible)',self.picture(sparse).plain)
        self.assertEqual(reduce(sparse,'SPACE')[1].value,'available')

    def test_drawable_width_alignment_and_complete_guidance(self):
        for caps in (self.caps, capabilities({'LC_ALL':'C','TERM':'linux','NO_COLOR':'1'},has_colors=True)):
            for size in ((80,24),(60,20),(40,15)):
                picture=self.picture(size=size,caps=caps)
                self.assertTrue(all(display_width(line.text) <= size[0]-1 for line in picture.lines))
                header=picture.lines[2].text
                row=picture.lines[3].text
                self.assertEqual(header.index('State'),1)
                self.assertIn(row[1],('✓','+'))
                self.assertEqual(header.index('Model'),row.index('Claude'))
            self.assertNotIn('…',self.picture(size=(60,20),caps=caps).lines[4].text)
        for model_id in IDS:
            model=next(row for row in self.state.catalog['models'] if row['id']==model_id)
            picture=self.picture(replace(self.state,focus_id=model_id))
            self.assertIn(model['guidance'],' '.join(picture.plain.split()))
        compact=self.picture(size=(40,15)).plain
        self.assertIn('Runtime',compact)
        self.assertIn('Unknown',compact)
        self.assertIn('Pod example:',compact)

    def test_expanded_variants_sources_and_small_scroll(self):
        for model_id in IDS:
            state=replace(self.state,focus_id=model_id,expanded=True)
            text=self.picture(state).plain
            reference=state.catalog['reference_benchmark']['models'][model_id]
            for variant in reference['variants']:
                self.assertIn(variant['profile']+': score',text)
            self.assertIn('Documented context:',text)
            self.assertIn('AA score index;',text)
            self.assertIn('Official source:',text)
            self.assertIn('AA source:',text)
        state=replace(self.state,expanded=True)
        before=self.picture(state,size=(40,15)).plain
        after,_=reduce(state,'PAGE_DOWN')
        self.assertNotEqual(before,self.picture(after,size=(40,15)).plain)
        restored,_=reduce(after,'ESC')
        self.assertEqual(restored.focus_id,state.focus_id)
        self.assertFalse(restored.expanded)

    def test_long_path_help_missing_invalid_and_age(self):
        long_path='/tmp/'+'very-long-component/'*16+'config.yaml'
        state=refresh(self.state,{**self.state.preferences,'path':long_path})
        help_state=replace(state,help_open=True)
        pages=[self.picture(replace(help_state,help_scroll=index),size=(40,15)).plain
               for index in range(0,22)]
        self.assertTrue(any(AA_URL in page.replace('\n','') for page in pages))
        self.assertTrue(any('AA metrics show' in page for page in pages))
        self.assertTrue(any('Preferences:' in page and '…' in page for page in pages))
        self.assertLessEqual(display_width(elide_middle(long_path,39)),39)
        missing=refresh(state,{**state.preferences,'errors':[{'code':'config_missing','message':'Personal preferences are missing'}],
                               'eligible':[]})
        text=self.picture(missing).plain
        self.assertIn('Personal preferences are missing',text)
        self.assertIn('one-shot installer',text)
        invalid=refresh(state,{**state.preferences,'errors':[{'code':'invalid_yaml','message':'Bad YAML at line 7'}],
                               'eligible':[]})
        text=self.picture(invalid).plain
        self.assertIn('line 7',text)
        self.assertIn('pod config edit',text)
        self.assertIn('1 day',self.picture().plain)
        self.assertIn('today',frame(self.state,80,24,self.caps,datetime(2026,9,24,tzinfo=timezone.utc)).plain)
        self.assertIn('2 days',frame(self.state,80,24,self.caps,datetime(2026,9,26,tzinfo=timezone.utc)).plain)

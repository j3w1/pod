"""Workspace actions through the real launcher: palette, edits, bulk, setup, compare and refresh."""

from datetime import datetime, timedelta, timezone
import json
import time

from pod.config import edit as edit_config, load as load_config
from tests.pty_harness import PtyCase, fixture_config

DOWN, UP, RIGHT, LEFT = "\x1bOB", "\x1bOA", "\x1bOC", "\x1bOD"
OPUS_MAX = "claude/claude-opus-5-5/max"
V1 = ("schema: pod/v1\nselection: custom\nmodels:\n  claude-opus-5-5: available\n  gpt-6-luna: disabled\n"
      "workers:\n  max_active: 3\npinned_model: claude-opus-5-5\n")


class WorkspacePtyTests(PtyCase):
    def saved(self):
        return load_config(personal=self.config)

    def test_palette_runs_sorting_grouping_and_help_from_the_keyboard(self):
        session = self.open(cols=100, rows=30)
        session.send(":")
        session.wait_for("COMMAND PALETTE")
        session.send("total")
        session.wait_for("Sort by AA total response")
        session.send("\r")
        session.wait_for(lambda s: "Total s↑" in s.text() and "COMMAND PALETTE" not in s.text())
        session.send("\x10")
        session.wait_for("COMMAND PALETTE")
        session.send("model-grouped\r")
        session.wait_for(lambda s: "Grouped" in s.text() and "[-] Claude Fable 5.1 (5/5 enabled)" in s.text())
        session.send(LEFT)
        session.wait_for("[+] Claude Opus 5.5 (5/5 enabled)")
        session.send(":help\r")
        session.wait_for("HELP: MODEL ROUTES")
        session.send("\x1b")
        session.wait_for("[Details]")
        self.assertEqual(self.saved()["routes"][OPUS_MAX], "enabled")

    def test_state_pin_and_preferred_save_at_once_and_survive_restart(self):
        session = self.open(cols=100, rows=30)
        session.send(" ")
        session.wait_for(lambda _s: self.saved()["routes"][OPUS_MAX] == "disabled")
        session.wait_for("Saved: Disabled " + OPUS_MAX)
        session.send("p")
        session.wait_for("Cannot make " + OPUS_MAX)
        self.assertIsNone(self.saved()["pinned"])
        session.send(" p")
        session.wait_for(lambda _s: self.saved()["pinned"] == OPUS_MAX)
        session.send(DOWN + "P")
        session.wait_for(lambda _s: self.saved()["preferred"] == "claude/claude-opus-5-5/xhigh")
        session.wait_for("Pin " + OPUS_MAX)
        session.wait_for("Preferred claude/claude-opus-5-5/xhigh")
        session.send(UP + " ")
        session.wait_for("Clear or replace the pin")
        self.assertEqual(self.saved()["routes"][OPUS_MAX], "enabled")
        session.close()
        restarted = self.open(cols=100, rows=30)
        self.assertIn("Pin " + OPUS_MAX, restarted.text())
        self.assertIn("▸✓●", restarted.text())
        restarted.send("p")
        restarted.wait_for(lambda _s: self.saved()["pinned"] is None)
        self.assertEqual(self.saved()["preferred"], "claude/claude-opus-5-5/xhigh")

    def test_bulk_preview_saves_once_and_protects_the_pin(self):
        fixture_config(self.config, pinned=OPUS_MAX)
        session = self.open(cols=100, rows=30)
        before = self.config.read_bytes()
        session.send("b" + DOWN + RIGHT)
        session.wait_for("CHANGES (5)")
        session.wait_for("claude/claude-opus-5-5/low: enabled -> disabled")
        session.send("\r")
        session.wait_for("this disables Pin " + OPUS_MAX)
        self.assertEqual(self.config.read_bytes(), before)
        session.send(DOWN + RIGHT + "\r")
        session.wait_for(lambda _s: self.saved()["pinned"] is None)
        saved = self.saved()
        self.assertEqual({key: value for key, value in saved["routes"].items() if "opus" in key},
                         {f"claude/claude-opus-5-5/{effort}": "disabled"
                          for effort in ("low", "medium", "high", "xhigh", "max")})
        session.wait_for("Bulk saved 5 routes")
        session.send(":reset\r")
        session.wait_for("Reset to shipped defaults")
        session.wait_for("CHANGES (5)")
        session.send("\r")
        session.wait_for(lambda _s: all(value == "enabled" for value in self.saved()["routes"].values()))
        self.assertIsNone(self.saved()["pinned"], "reset never re-creates or clears selections silently")

    def test_hidden_pin_and_focus_survive_filters_search_and_errors(self):
        fixture_config(self.config, pinned="codex/gpt-6-luna/high")
        session = self.open(cols=80, rows=24)
        session.send("/gpt-6-luna/high\r")
        session.wait_for(lambda s: "1 rows" in s.text())
        session.send("\x1b" + "f")
        session.wait_for("Pin codex/gpt-6-luna/high (hidden)")
        session.send("/zzz")
        session.wait_for("No rows match")
        session.send("\r" + "F")
        session.wait_for(lambda s: "Pin codex/gpt-6-luna/high" in s.text() and "(hidden)" not in s.text())
        try:
            self.config.parent.chmod(0o500)
            session.send(" ")
            session.wait_for("Not saved")
            session.send("f")
            session.settle(quiet=.5, timeout=1)
            self.assertIn("Not saved", session.text())
            self.assertIn("Pin codex/gpt-6-luna/high", session.text())
        finally:
            self.config.parent.chmod(0o700)

    def test_new_and_unsupported_observations_are_reachable_not_routable(self):
        session = self.open(cols=100, rows=30)
        session.wait_for("Showing all")
        session.send("ooo")
        session.wait_for("Showing unsupported")
        session.wait_for("GPT-6 Sol (")
        before = self.config.read_bytes()
        session.send(" p")
        session.wait_for("not routable")
        self.assertEqual(self.config.read_bytes(), before)

    def test_compare_tab_and_frontier_create_no_recommendation(self):
        session = self.open(cols=120, rows=30)
        session.send("c" + DOWN + "c" + DOWN + "c" + "e")
        session.wait_for("frontier per AA profile")
        self.assertIn("not a recommendation", session.text())
        session.send(":show the comparison\r")
        session.wait_for("[Compare(3)]")
        session.wait_for("DIFFERENCES FROM 1")
        session.send("\t\x1b[6~")
        session.wait_for("pts")
        text = session.text()
        self.assertIn("pts", text)
        self.assertNotIn("Recommended", text)
        self.assertNotIn("twice", text)

    def test_compact_inspector_at_40_by_12_is_reachable(self):
        session = self.open(cols=40, rows=12)
        self.assertIn("[Det]", session.text())
        session.send("\t" + RIGHT)
        session.wait_for("[Bench]")
        session.wait_for("AA benchmark cost")
        for _ in range(12):
            session.send("\x1b[6~")
        session.wait_for("Output speed")
        session.send("\x1b")
        session.wait_for(lambda s: s.lines()[-1].startswith("Space state"))

    def test_setup_screen_saves_only_after_explicit_confirmation(self):
        self.config.write_text(V1)
        session = self.open(cols=100, rows=30, wait="ROUTE SETUP")
        session.send("\r")
        session.wait_for("Choose an exact effort")
        session.send(RIGHT + RIGHT + "\r")
        session.wait_for("Save this route setup now?")
        self.assertEqual(self.config.read_text(), V1)
        session.send("n")
        session.settle()
        self.assertEqual(self.config.read_text(), V1)
        session.send("\ry")
        session.wait_for(lambda _s: self.saved()["status"] == "valid")
        saved = self.saved()
        self.assertEqual((saved["pinned"], saved["max_active"]), ("claude/claude-opus-5-5/medium", 3))
        self.assertEqual((self.config.parent / "config.yaml.pod-v1").read_text(), V1)
        session.wait_for("Saved: route setup")

    def test_setup_conflict_rereads_the_file_so_confirming_again_saves(self):
        self.config.write_text(V1)
        session = self.open(cols=100, rows=30, wait="ROUTE SETUP")
        session.send(RIGHT + RIGHT + "\r")
        session.wait_for("Save this route setup now?")
        changed = V1.replace("max_active: 3", "max_active: 4")
        self.config.write_text(changed)
        session.send("y")
        session.wait_for("changed elsewhere")
        self.assertEqual(self.config.read_text(), changed)
        session.send("\r")
        session.wait_for("Save this route setup now?")
        session.send("y")
        session.wait_for(lambda _s: self.saved()["status"] == "valid")
        self.assertEqual((self.saved()["max_active"], self.saved()["pinned"]), (4, "claude/claude-opus-5-5/medium"))
        session.wait_for("Saved: route setup")

    def test_missing_preferences_file_gets_a_read_only_notice(self):
        session = self.open()
        self.config.unlink()
        session.wait_for("Preferences missing - read-only", timeout=3)
        session.wait_for("READ-ONLY")
        session.send(" ")
        session.settle()
        self.assertFalse(self.config.exists())

    def test_stale_data_is_marked_at_40_by_12_with_pin_and_preferred(self):
        from pod.observations import bundled
        pin, preferred = "codex/gpt-6.1-sol/high", "claude/claude-opus-5-5/medium"
        fixture_config(self.config, pinned=pin, preferred=preferred)
        snapshot = bundled()
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot["created_at"] = old
        for block in snapshot["sources"].values():
            block["retrieved_at"] = old
        self.cache.mkdir(parents=True)
        (self.cache / "current.json").write_text(json.dumps(snapshot))
        session = self.open(cols=40, rows=12)
        text = session.text()
        self.assertIn("Data STALE", text)
        self.assertIn("Pin " + pin, text)
        self.assertIn("Preferred " + preferred, text)
        session.resize(160, 45)
        session.wait_for("10d old STALE")

    def test_setup_can_be_left_for_read_only_browsing(self):
        self.config.write_text(V1)
        session = self.open(wait="ROUTE SETUP")
        session.send("\x1b")
        session.wait_for("SETUP REQUIRED")
        session.send(" ")
        session.wait_for("Route setup required")
        self.assertEqual(self.config.read_text(), V1)


class RefreshPtyTests(PtyCase):
    saved = WorkspacePtyTests.saved

    def test_unfiltered_ascii_routes_keep_distinct_labels_with_observations(self):
        for columns in (40, 41):
            with self.subTest(columns=columns):
                session = self.open(cols=columns, rows=80, LC_ALL="C", NO_COLOR="1")
                session.send("s")
                session.wait_for("$/task")
                session.settle(quiet=.3, timeout=2)
                self.assertIn("Opus 5.5 high", session.text())
                self.assertIn("Sonnet", session.text())
                self.assertNotIn("Cla...5.5", session.text())
                session.close()

    def test_narrow_default_discoveries_keep_profile_labels_and_remain_read_only(self):
        for cols, rows in ((40, 12), (60, 20)):
            with self.subTest(size=(cols, rows)):
                session = self.open(cols=cols, rows=rows, fetch="growth", LC_ALL="C", NO_COLOR="1")
                before = self.config.read_bytes()
                session.send("R")
                session.wait_for(lambda s: "Data updated" in s.text() or "Data refreshed" in s.text(), timeout=4)
                session.send("/GPT-7 Nova\r")
                session.wait_for(lambda s: any("new" in line and "Nova high" in line for line in s.lines()))
                session.send(" pP")
                session.settle(quiet=.3, timeout=1)
                self.assertEqual(self.config.read_bytes(), before)
                session.close()

    def test_R_adds_new_rows_to_default_view_and_reopen_keeps_them(self):
        session = self.open(cols=160, rows=45, fetch="growth")
        session.wait_for("Showing all")
        before = self.config.read_bytes()
        session.send("R")
        session.wait_for("Data updated: AA rows", timeout=4)
        self.assertEqual(self.fetches(), 3)
        session.send("/GPT-7 Nova\r")
        session.wait_for("GPT-7 Nova (high)")
        self.assertIn("new", session.text())
        session.send(" pP")
        session.wait_for("not routable")
        self.assertEqual(self.config.read_bytes(), before)
        session.close()
        again = self.open(cols=160, rows=45, fetch="growth")
        again.send("/GPT-7 Nova\r")
        again.wait_for("GPT-7 Nova (high)")
        self.assertEqual(self.fetches(), 3, "manual-only reopen uses the promoted cache")

    def test_R_loads_haiku_metrics_without_enabling_new_routes(self):
        document = self.saved()
        document["routes"] = {key: value for key, value in document["routes"].items()
                              if not key.startswith("claude/claude-haiku-5-5/")}
        fixture_config(self.config, routes=document["routes"])
        before = self.config.read_bytes()
        session = self.open(cols=160, rows=45, fetch="growth")
        session.send("/Haiku 5.5\r")
        session.wait_for("Claude Haiku 5.5")
        session.send("R")
        session.wait_for("Data updated: AA rows", timeout=4)
        session.wait_for(lambda s: any("Claude Haiku 5.5" in line and " max " in line and " 43 " in line
                                      for line in s.lines()))
        self.assertEqual(self.config.read_bytes(), before)
        snapshot = json.loads((self.cache / "current.json").read_text())
        names = {row["name"] for row in snapshot["sources"]["artificial_analysis"]["rows"]}
        self.assertTrue(all(f"Claude Haiku 5.5 ({effort})" in names
                            for effort in ("low", "medium", "high", "xhigh", "max")))
        self.assertFalse(any("haiku" in key for key in self.saved()["eligible"]))
        session.send(" ")
        session.wait_for(lambda _s: any("haiku" in key for key in self.saved()["eligible"]))
        selected = [key for key in self.saved()["eligible"] if "haiku" in key]
        self.assertEqual(len(selected), 1)
        session.close()
        again = self.open(cols=100, rows=30)
        again.send("/Haiku 5.5\r")
        effort = selected[0].rsplit("/", 1)[1]
        again.wait_for(lambda s: any("Claude Haiku 5.5" in line and f" {effort} " in line and "enabled" in line
                                    for line in s.lines()))
        self.assertEqual([key for key in self.saved()["eligible"] if "haiku" in key], selected)

    def test_automatic_refresh_renders_cache_first_then_updates_once(self):
        fixture_config(self.config, refresh="automatic")
        session = self.open(fetch="ok")
        session.wait_for("Data updated", timeout=4)
        session.wait_for("Data cache")
        self.assertEqual(self.fetches(), 3)
        self.assertTrue((self.cache / "current.json").exists())
        session.close()
        again = self.open(fetch="ok")
        again.settle(quiet=.6, timeout=1.5)
        self.assertEqual(self.fetches(), 3, "fresh data is not fetched again on reopening")
        self.assertNotIn("Refreshing", again.text())

    def test_manual_only_never_fetches_until_refresh_is_pressed(self):
        session = self.open(fetch="ok")
        session.settle(quiet=.6, timeout=1.5)
        self.assertEqual(self.fetches(), 0)
        session.send("R")
        session.wait_for("Data updated", timeout=4)
        self.assertEqual(self.fetches(), 3)

    def test_quit_cancels_a_running_refresh_without_promoting(self):
        fixture_config(self.config, refresh="automatic")
        session = self.open(fetch="slow")
        session.wait_for("Refreshing data")
        session.send(DOWN)
        session.wait_for(lambda s: s.lines()[4].startswith("▸") and "Refreshing" in s.text(), timeout=2)
        started = time.monotonic()
        session.send("q")
        session.wait_for(lambda s: s.closed, timeout=3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse((self.cache / "current.json").exists())
        record = json.loads((self.cache / "refresh.json").read_text())
        self.assertIn(record["attempt"]["outcome"], ("cancelled", "running"))

    def test_coverage_collapse_is_named_refused_with_its_diagnostic(self):
        session = self.open(cols=160, rows=45, fetch="collapse")
        session.send("R")
        session.wait_for("Refresh refused: artificial_analysis: Coverage collapsed", timeout=4)
        self.assertNotIn("Refresh failed", session.text())
        self.assertFalse((self.cache / "current.json").exists())

    def test_denial_is_reported_and_restrains_reopening(self):
        fixture_config(self.config, refresh="automatic")
        session = self.open(fetch="deny")
        session.wait_for("Refresh failed", timeout=4)
        session.send("\t" + RIGHT * 3)
        session.wait_for("LAST REFRESH IN THIS WINDOW")
        session.wait_for("Refresh failed: access_denied: artificial_analysis: HTTP 403 bot challenge")
        session.send("\x1b")
        session.send(DOWN)
        session.settle()
        self.assertIn("Data bundled", session.text())
        session.close()
        count = self.fetches()
        again = self.open(fetch="deny")
        again.settle(quiet=.6, timeout=1.5)
        self.assertEqual(self.fetches(), count, "a recent failed attempt is not repeated on reopening")


class AuditCorrectionPtyTests(PtyCase):
    """Real-terminal regressions for the FIX_TUI2 audit corrections (T1, T3, M1, M2, T5-T8, A11)."""

    def saved(self):
        return load_config(personal=self.config)

    def outside(self, **change):
        """Another window or `pod config` saves while this one is open."""
        edit_config(self.config, displayed=self.saved(), **change)

    def write_cache(self, required: timedelta, optional: timedelta | None = None) -> None:
        from pod.observations import bundled
        snapshot = bundled()
        now = datetime.now(timezone.utc)
        stamp = lambda age: (now - age).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot["created_at"] = stamp(required)
        for block in snapshot["sources"].values():
            block["retrieved_at"] = stamp(required if block["required"] or optional is None else optional)
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / "current.json").write_text(json.dumps(snapshot))

    def test_bulk_saves_nothing_unreviewed_after_an_outside_not_set(self):
        # Audit scenario A: the reviewed preview enabled two routes; another writer set a third to not set.
        document = load_config(personal=self.config)
        routes = dict(document["routes"])
        for key in ("codex/gpt-6-luna/low", "codex/gpt-6-luna/medium"):
            routes.pop(key)
        fixture_config(self.config, routes=routes)
        session = self.open(cols=100, rows=30)
        session.send("b" + RIGHT + RIGHT)
        session.wait_for("CHANGES (2)")
        self.outside(routes={OPUS_MAX: None})
        session.wait_for("changed since this preview")
        before = self.config.read_bytes()
        session.send("\r")
        session.wait_for("changed elsewhere; review again")
        self.assertEqual(self.config.read_bytes(), before, "nothing the user did not review is saved")
        self.assertNotIn(OPUS_MAX, self.saved()["routes"])
        session.wait_for("CHANGES (3)")
        session.wait_for(OPUS_MAX + ": not set -> enabled")
        session.send("\r")
        session.wait_for(lambda _s: self.saved()["routes"].get(OPUS_MAX) == "enabled")
        session.wait_for("Bulk saved 3 routes")

    def test_bulk_clear_pin_consent_never_moves_to_another_pin(self):
        # Audit scenario G: consent to clear the pin .../high must not clear a pin moved to .../max.
        high = "claude/claude-opus-5-5/high"
        fixture_config(self.config, pinned=high)
        session = self.open(cols=100, rows=30)
        session.send("b" + DOWN + RIGHT + DOWN + RIGHT)
        session.wait_for(f"yes ({high} would be disabled)")
        self.outside(pinned=OPUS_MAX)
        session.wait_for("changed since this preview")
        before = self.config.read_bytes()
        session.send("\r")
        session.wait_for("changed elsewhere; review again")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.saved()["pinned"], OPUS_MAX)
        session.wait_for(f"no ({OPUS_MAX} would be disabled)")
        session.send("\r")
        session.wait_for(f"this disables Pin {OPUS_MAX}")
        self.assertEqual(self.config.read_bytes(), before, "the moved pin needs its own consent")

    def test_open_window_ages_and_turns_stale_without_a_reload(self):
        self.write_cache(timedelta(days=7) - timedelta(seconds=3))
        session = self.open(cols=100, rows=30)
        self.assertIn("Data cache 6d old", session.text())
        self.assertNotIn("STALE", session.text())
        time.sleep(3.5)
        session.send(DOWN)
        session.wait_for("Data cache 7d old STALE")
        session.wait_for("Data STALE")

    def test_mixed_source_ages_are_never_hidden_behind_one_time(self):
        self.write_cache(timedelta(hours=1), optional=timedelta(days=3))
        session = self.open(cols=100, rows=30)
        session.wait_for("Data cache 1h old; mixed ages (oldest 3d)")

    def test_profile_marks_sit_beside_the_numbers_in_both_glyph_sets(self):
        session = self.open(cols=80, rows=24)
        lines = session.lines()
        opus = next(line for line in lines if "Claude Opus 5.5" in line and " max " in line)
        astra = next(line for line in lines if "GPT-6 Astra" in line and " max " in line)
        self.assertRegex(opus, r"58†\s+\$5\.98†\s+\d+†")
        self.assertNotIn("†", astra)
        session.send("\t\x1b[6~")
        session.wait_for("AA profile with fallback: AA index 58†")
        plain = self.open(cols=80, rows=24, LC_ALL="C", NO_COLOR="1")
        self.assertRegex(plain.text(), r"58#\s+\$5\.98#")
        plain.send("?")
        plain.wait_for("HELP: MODEL ROUTES")
        plain.send("\x1b[6~")
        plain.wait_for(lambda s: "# AA ran this" in " ".join(s.text().split()))

    def test_compare_names_a_profile_difference_instead_of_a_delta(self):
        session = self.open(cols=100, rows=30)
        session.send("c" + DOWN * 6)
        session.wait_for(lambda s: "GPT-6 Astra · max" in self.heading(s))
        session.send("c:show the comparison\r")
        session.wait_for("[Compare(2)]")
        session.send("\t" + DOWN * 4)
        session.wait_for("Caveat: 1 and 2 are not like-for-like")
        session.wait_for("not comparable (AA profile with fallback vs (none))")

    def test_cost_or_time_column_always_keeps_a_disclaimer_at_40_by_12(self):
        session = self.open(cols=40, rows=12)
        self.assertNotIn("$/task", session.text())
        session.send("s")
        session.wait_for("$/task")
        session.wait_for("AA $ not your bill/quota; time not task")
        session.send("ss")
        session.wait_for(lambda s: "First s" in s.text() and "AA $ not your bill/quota; time not task" in s.text())

    def test_narrow_labels_keep_the_effort_under_every_sort(self):
        session = self.open(cols=40, rows=12)
        for _ in range(8):  # every sort in turn
            labels = [line[6:].split("  ")[0].strip() for line in session.lines()[3:]
                      if line[:2].strip() in ("▸✓", "✓", "▸")]
            self.assertTrue(labels, session.text())
            for label in labels:
                self.assertRegex(label, r" (low|medium|high|xhigh|max)$", session.text())
            self.assertEqual(len(labels), len(set(labels)), session.text())
            session.send("s")
            session.settle()

    def test_focus_stays_on_a_shown_row_and_edits_only_that_row(self):
        # Audit T8: an empty search plus a filter change left no marker while edits hit the first row.
        session = self.open(cols=100, rows=30)
        session.send("/zzz\rff/\r")
        session.wait_for("Filter provider=OpenAI")
        session.settle()
        marked = [line for line in session.lines()[3:15] if line.startswith("▸")]
        self.assertEqual(len(marked), 1, session.text())
        key = "codex/gpt-6-astra/max"
        self.assertIn("GPT-6 Astra", marked[0])
        session.send(" ")
        session.wait_for(lambda _s: self.saved()["routes"][key] == "disabled")
        session.send("/zzz")
        session.wait_for("No rows match")
        before = self.config.read_bytes()
        session.send("\r ")
        session.wait_for("No row is shown")
        self.assertEqual(self.config.read_bytes(), before)

    def test_bulk_scroll_hint_names_the_key_that_scrolls(self):
        session = self.open(cols=80, rows=24)
        session.send("b" + RIGHT + DOWN + RIGHT)
        session.wait_for("CHANGES (35)")
        session.wait_for(lambda s: "more (" in s.text() and "lines): Page Down" in s.text())
        self.assertNotIn("Tab, then", session.text())


class DeltaAuditPtyTests(PtyCase):
    """Real-terminal regressions for the FIX_TUI3 delta-review corrections (D1-D4)."""

    saved, outside, write_cache = (AuditCorrectionPtyTests.saved, AuditCorrectionPtyTests.outside,
                                   AuditCorrectionPtyTests.write_cache)
    HIGH = "claude/claude-opus-5-5/high"

    def range_consent(self) -> "object":
        fixture_config(self.config, pinned=self.HIGH)
        session = self.open(cols=100, rows=30)
        session.send("b")
        session.wait_for("BULK EDIT")
        session.send(RIGHT)
        session.wait_for("Shown routes in an effort range")
        session.send(DOWN + RIGHT)
        session.wait_for(f"({self.HIGH} would be disabled)")
        session.send(DOWN * 3 + RIGHT)
        session.wait_for(f"yes ({self.HIGH} would be disabled)")
        return session

    def assert_moved_pin_kept(self, session) -> None:
        session.wait_for(f"no ({OPUS_MAX} would be disabled)")
        before = self.config.read_bytes()
        session.send("\r")
        session.wait_for(f"this disables Pin {OPUS_MAX}")
        self.assertEqual(self.config.read_bytes(), before, "consent for another pin never clears this one")
        self.assertEqual(self.saved()["pinned"], OPUS_MAX)

    def test_bulk_range_edit_never_moves_clear_pin_consent_to_a_moved_pin(self):
        session = self.range_consent()
        self.outside(pinned=OPUS_MAX)
        session.wait_for("changed since this preview")
        session.send(UP + UP + RIGHT + LEFT)        # From effort low -> medium -> low rebuilds the preview
        self.assert_moved_pin_kept(session)

    def test_bulk_range_narrowed_then_widened_drops_hidden_consent(self):
        session = self.range_consent()
        session.send(UP + LEFT * 3)                 # To effort max -> medium hides the pin conflict
        session.wait_for(lambda s: "Clear Pin" not in s.text())
        self.outside(pinned=OPUS_MAX)
        session.wait_for("Preferences changed outside this window")
        session.send(RIGHT * 3)
        self.assert_moved_pin_kept(session)

    def test_palette_toggle_refuses_a_route_changed_while_the_palette_was_open(self):
        session = self.open(cols=100, rows=30)
        session.send(":")
        session.wait_for("COMMAND PALETTE")
        session.send("enable or disable")
        session.wait_for("Enable or disable the focused route")
        self.outside(routes={OPUS_MAX: "disabled"})
        session.wait_for("Preferences changed outside this window")
        before = self.config.read_bytes()
        session.send("\r")
        session.wait_for("changed elsewhere; review again")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.saved()["routes"][OPUS_MAX], "disabled")

    def test_an_outside_edit_merged_under_the_preference_lock_is_announced(self):
        import fcntl
        import os
        import threading
        import yaml
        session = self.open(cols=160, rows=30)
        held = threading.Event()

        def writer():
            fd = os.open(self.config.parent / ".config.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                held.set()
                time.sleep(.25)                         # the window's save now waits for the lock
                document = yaml.safe_load(self.config.read_text())
                document["routes"]["codex/gpt-6-luna/low"] = "disabled"
                self.config.with_suffix(".tmp").write_text(yaml.safe_dump(document, sort_keys=False))
                os.replace(self.config.with_suffix(".tmp"), self.config)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(held.wait(2))
        session.send(" ")
        thread.join(2)
        session.wait_for(f"Saved: Disabled {OPUS_MAX}; Preferences changed outside this window")
        self.assertEqual(self.saved()["routes"]["codex/gpt-6-luna/low"], "disabled")

    def test_data_age_outlasts_the_mixed_ages_note_and_a_notice(self):
        self.write_cache(timedelta(hours=1), optional=timedelta(days=3))
        session = self.open(cols=40, rows=12)
        session.wait_for("Data cache 1h old; mixed (oldest 3d)")
        self.write_cache(timedelta(days=8), optional=timedelta(days=10))
        session.wait_for("Data cache 8d old STALE")
        for cols in (60, 70, 80):
            session.resize(cols, 24)
            session.send(" ")
            session.wait_for(lambda s: any("Saved: " in line and "Data cache 8d old STALE" in line
                                           for line in s.lines()))

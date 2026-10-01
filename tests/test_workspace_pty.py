"""Workspace actions through the real launcher: palette, edits, bulk, setup, compare and refresh."""

from datetime import datetime, timedelta, timezone
import json
import time

from pod.config import load as load_config
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
        self.assertNotIn("GPT-6 Sol (", session.text())
        session.send("oo")
        session.wait_for("Showing unsupported")
        session.wait_for("GPT-6 Sol (")
        before = self.config.read_bytes()
        session.send(" p")
        session.wait_for("not routable")
        self.assertEqual(self.config.read_bytes(), before)

    def test_compare_tab_and_frontier_create_no_recommendation(self):
        session = self.open(cols=100, rows=30)
        session.send("c" + DOWN + "c" + DOWN + "c" + "e")
        session.wait_for("AA frontier of shown rows")
        session.send(":show the comparison\r")
        session.wait_for("[Compare(3)]")
        session.wait_for("DIFFERENCES FROM 1")
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

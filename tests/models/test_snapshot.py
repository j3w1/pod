import contextlib
from datetime import datetime, timezone
import http.client
import io
import json
from unittest.mock import patch

from pod import paths, routefile
from pod.models import adapters, cli, leaderboard, snapshot, view
from tests.models.support import FIXTURES, ModelCase, ROUTES, current_page, live_page, page, without


class RefreshTests(ModelCase):
    def test_removing_one_of_three_published_profiles_preserves_the_whole_snapshot(self):
        source = (FIXTURES / "leaderboard-profiles.html").read_text(encoding="utf-8")
        source = source.replace(r'Xhigh)\"],', r'Xhigh)\",\"GPT-6.2 Astra (Low)\"],', 1)
        low = {"release": {"slug": "gpt-6-2-astra", "name": "GPT-6.2 Astra"}, "modelCreatorName": "OpenAI",
               "name": "GPT-6.2 Astra (Low)", "suffix": "Low", "intelligenceIndex": 45.6504174923967}
        push = "<script>self.__next_f.push(" + json.dumps([1, json.dumps(low) + ","]) + ")</script>\n"
        position = source.index('<script>self.__next_f.push([1,"{')
        good = self.refresh_from(source[:position] + push + source[position:])
        self.assertTrue(good["ok"], good)
        path = snapshot.home() / "snapshot.json"
        kept = path.read_bytes()
        failed = self.refresh_from(source)
        self.assertFalse(failed["ok"])
        self.assertEqual(snapshot.last_attempt(), failed)
        self.assertEqual(path.read_bytes(), kept)
        self.assertEqual(len(snapshot.load()["rows"]), good["rows"])

    def test_read_and_replace_use_the_same_serialized_cache_limit(self):
        source = page().replace("GPT-6.1 Sol", "Café")
        self.assertTrue(self.refresh_from(source)["ok"])
        path = snapshot.home() / "snapshot.json"
        size = path.stat().st_size
        with patch.object(snapshot, "MAX_CACHE_BYTES", size, create=True):
            self.assertTrue(self.refresh_from(source)["ok"])
            kept = path.read_bytes()
            self.assertEqual(len(kept), size)
            self.assertEqual(snapshot.load()["rows"][1]["name"], "Café (high)")

            attempt = self.refresh_from(source.replace("Café", "Cafés"))
            self.assertFalse(attempt["ok"])
            self.assertIn("cache limit", attempt["failure"])
            self.assertEqual(snapshot.last_attempt(), attempt)
            self.assertEqual(path.read_bytes(), kept)
            self.assertIsNotNone(snapshot.load())

            path.write_bytes(kept + b" ")
            self.assertIsNone(snapshot.load())

    def test_current_refresh_fetches_once_preserves_preferences_and_last_good_on_failure(self):
        preferences = ROUTES.replace("preferred: null", "preferred: codex/gpt-6.1-sol/high").replace(
            "pinned: null", "pinned: codex/gpt-6.1-sol/high")
        target = self.write_routes(preferences)
        before, stamp = target.read_bytes(), target.stat().st_mtime_ns
        with patch.object(leaderboard, "fetch", return_value=current_page()) as fetch:
            attempt = snapshot.refresh()
        self.assertTrue(attempt["ok"], attempt)
        fetch.assert_called_once_with()
        self.assertEqual(attempt["rows"], 197)
        kept = (snapshot.home() / "snapshot.json").read_bytes()
        for old, new in (("$4.00", "invalid"), (">62<", ">inf<"),
                         ('<img alt="OpenAI">', '<img alt="">')):
            with self.subTest(old=old):
                attempt = self.refresh_from(current_page().replace(old, new, 1))
                self.assertFalse(attempt["ok"])
                self.assertEqual((snapshot.home() / "snapshot.json").read_bytes(), kept)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertEqual(routefile.load().enabled(), ["codex/gpt-6.1-sol/high", "claude/claude-opus-5-5/high"])

    def test_refresh_succeeds_against_the_current_public_table(self):
        attempt = self.refresh_from(live_page())
        self.assertEqual((attempt["ok"], attempt["rows"], attempt["failure"]), (True, 284, None))
        shot = snapshot.load()
        self.assertEqual(shot["methodology"], "Artificial Analysis Intelligence Index v5.0.1")
        self.assertEqual(shot["fetched_at"], attempt["attempted_at"])
        self.assertEqual(shot["units"]["cost_per_task_usd"], "US dollars per benchmark task")
        self.assertEqual(snapshot.last_attempt()["ok"], True)

    def test_a_missing_required_column_fails_as_a_whole_and_keeps_the_last_good_snapshot(self):
        self.refresh_from(live_page())
        kept = (snapshot.home() / "snapshot.json").read_bytes()
        attempt = self.refresh_from(page(*without("Cost per Task USD")))
        self.assertFalse(attempt["ok"])
        self.assertIn("lacks required columns: cost_per_task_usd", attempt["failure"])
        self.assertEqual((snapshot.home() / "snapshot.json").read_bytes(), kept)
        self.assertEqual(snapshot.last_attempt()["failure"], attempt["failure"])

    def test_a_failed_first_refresh_leaves_no_snapshot(self):
        def unreachable():
            raise leaderboard.RefreshError("The source could not be reached (TimeoutError)")
        attempt = snapshot.refresh(unreachable)
        self.assertEqual((attempt["ok"], snapshot.load()), (False, None))
        self.assertIn("could not be reached", snapshot.last_attempt()["failure"])

    def test_a_broken_transfer_is_a_recorded_failed_refresh_and_keeps_the_last_good_snapshot(self):
        self.refresh_from(live_page())
        kept = (snapshot.home() / "snapshot.json").read_bytes()
        for error in (http.client.IncompleteRead(b"<html>"), http.client.BadStatusLine("garbage"), KeyError("x")):
            def broken(error=error):
                raise error
            with self.subTest(error=type(error).__name__):
                attempt = snapshot.refresh(broken)
                self.assertEqual((attempt["ok"], attempt["failure"]),
                                 (False, f"The refresh failed ({type(error).__name__})"))
                self.assertEqual(snapshot.last_attempt(), attempt)
                self.assertEqual((snapshot.home() / "snapshot.json").read_bytes(), kept)
                self.assertIn("Last refresh failed", "\n".join(view.build().header()))

    def test_a_cache_that_cannot_be_written_fails_cleanly_with_a_message(self):
        paths.cache_home().mkdir(parents=True)
        (paths.cache_home() / "models").write_text("a file where the cache directory belongs")
        for fetch, reason in ((live_page, "The snapshot could not be written to"),
                              (lambda: page(*without("Cost per Task USD")), "lacks required columns")):
            attempt = snapshot.refresh(fetch)
            self.assertFalse(attempt["ok"])
            self.assertIn(reason, attempt["failure"])
            self.assertIn(f"this attempt could not be recorded in {snapshot.home()}", attempt["failure"])
        out, err = io.StringIO(), io.StringIO()
        with patch.object(leaderboard, "fetch", live_page), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["refresh"]), 1)
        self.assertIn("Refresh failed: The snapshot could not be written to", err.getvalue())
        self.assertIn("There is no earlier snapshot.", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_the_fetch_is_called_exactly_once(self):
        calls = []
        snapshot.refresh(lambda: calls.append(1) or "<html></html>")
        self.assertEqual(len(calls), 1)

    def test_a_damaged_snapshot_reads_as_no_snapshot(self):
        snapshot.home().mkdir(parents=True)
        for text in ("{", "[]", json.dumps({"schema": "other", "rows": []}), json.dumps({"schema": snapshot.SCHEMA})):
            (snapshot.home() / "snapshot.json").write_text(text)
            self.assertIsNone(snapshot.load())

    def test_age_is_short_and_never_guessed(self):
        at = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(snapshot.age("2026-11-12T11:59:40Z", at=at), "just now")
        self.assertEqual(snapshot.age("2026-11-12T09:00:00Z", at=at), "3 h ago")
        self.assertEqual(snapshot.age("2026-11-01T09:00:00Z", at=at), "11 d ago")
        self.assertEqual(snapshot.age("soon", at=at), "unknown age")
        self.assertEqual(snapshot.age("2026-12-01T00:00:00Z", at=at), "unknown age")


class PreferenceIsolationTests(ModelCase):
    def test_refresh_and_reading_never_change_the_preference_file(self):
        from pod.models import workspace
        target = self.write_routes(ROUTES)
        before = target.read_bytes()
        stamp = target.stat().st_mtime_ns
        self.refresh_from(live_page())
        self.refresh_from(page(*without("Creator")))
        view.build()
        workspace.Workspace(refresh=lambda: self.fail("opening must not refresh"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["status"]), 0)
            self.assertEqual(cli.main(["--plain"]), 0)
            self.assertEqual(cli.main(["--json", "--all"]), 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, stamp)

    def test_refresh_without_a_preference_file_creates_none_and_enables_nothing(self):
        self.refresh_from(live_page())
        self.assertFalse(routefile.load().path.exists())
        self.assertEqual(routefile.load().enabled(), [])


class AdapterTests(ModelCase):
    def test_the_catalog_is_optional_and_read_defensively(self):
        self.assertEqual(adapters.catalogs(), {"codex": None, "claude": None})
        self.codex_catalog({"gpt-6.1-sol": ["low", "ultra"]})
        self.assertEqual(adapters.codex(), {"gpt-6.1-sol": {"efforts": ["low", "ultra"], "name": "GPT-6.1-SOL",
                                                            "context_tokens": 500000}})
        from pathlib import Path
        import os
        target = Path(os.environ["HOME"]) / ".codex" / "models_cache.json"
        for text in ("{", "{}", '{"models": 3}', '{"models": [{"slug": 7}, "x", {"slug": "h", "visibility": "hide"}]}'):
            target.write_text(text)
            self.assertIn(adapters.codex(), (None, {}), text)

    def test_the_adapter_home_override_is_honoured(self):
        import os
        other = self.tmp / "codex-home"
        other.mkdir()
        (other / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": "m-1", "supported_reasoning_levels": []}]}))
        os.environ["CODEX_HOME"] = str(other)
        self.assertEqual(list(adapters.codex()), ["m-1"])


if __name__ == "__main__":
    import unittest
    unittest.main()

from datetime import datetime, timezone
import json

from pod import routefile
from pod.models import adapters, leaderboard, snapshot
from tests.models.support import ModelCase, ROUTES, live_page, page, without


class RefreshTests(ModelCase):
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
        from pod.models import cli, view, workspace
        target = self.write_routes(ROUTES)
        before = target.read_bytes()
        stamp = target.stat().st_mtime_ns
        self.refresh_from(live_page())
        self.refresh_from(page(*without("Creator")))
        view.build()
        workspace.Workspace(refresh=lambda: self.fail("opening must not refresh"))
        import contextlib
        import io
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

import contextlib
import io
import json
import subprocess
import sys
from os import terminal_size
from unittest.mock import patch

from pod import routefile
from pod.models import cli, leaderboard
from tests.models.support import FIXTURES, ModelCase, ROUTES, current_page, live_page, page, without
from tests.support import ROOT


class CliCase(ModelCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CliTests(CliCase):
    def test_current_refresh_and_json_expose_new_routes_without_preferences(self):
        with patch.object(leaderboard, "fetch", return_value=current_page()) as fetch:
            code, out, err = self.run_cli("refresh")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Refreshed: 197 public rows", out)
        fetch.assert_called_once_with()
        code, out, err = self.run_cli("--json")
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["rows"]
        for model_id in ("gpt-6.2-astra", "gpt-6.2-sol"):
            matched = [row for row in rows if row["route"] and row["route"].startswith(f"codex/{model_id}/")]
            self.assertEqual(len(matched), 6)
            self.assertTrue(all(row["state"] == "routable" for row in matched))
            measured = next(row for row in matched if row["route"].endswith("/max"))
            self.assertTrue(measured["observed_publicly"])
        self.assertFalse(routefile.load().path.exists())

    def test_refresh_reports_success_and_failure_and_fetches_once(self):
        calls = []
        with patch.object(leaderboard, "fetch", lambda: calls.append(1) or live_page()):
            code, out, _err = self.run_cli("refresh")
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertIn("Refreshed: 284 public rows", out)
        self.assertIn("No route was enabled", out)
        with patch.object(leaderboard, "fetch", lambda: page(*without("Cost per Task USD"))):
            code, out, err = self.run_cli("refresh")
        self.assertEqual(code, 1)
        self.assertIn("lacks required columns", err)
        self.assertIn("earlier snapshot was kept", err)
        with patch.object(leaderboard, "fetch", lambda: "<html></html>"):
            code, out, _err = self.run_cli("refresh", "--json")
        self.assertEqual((code, json.loads(out)["refresh"]["ok"]), (1, False))

    def test_the_plain_table_is_used_without_a_terminal(self):
        self.write_routes(ROUTES)
        self.refresh_from(live_page())
        code, out, _err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Model and effort", out)
        self.assertIn("enabled", out)
        self.assertNotIn("Gemini", out)
        self.assertNotIn("Gemini 4 Carbon (high)", self.run_cli("--all")[1])

    def test_narrow_plain_table_rounds_full_precision_indexes_and_keeps_the_estimate_marker(self):
        source = (FIXTURES / "leaderboard-profiles.html").read_text(encoding="utf-8")
        self.assertTrue(self.refresh_from(source.replace(">62<", ">62*<"))["ok"])
        with patch.object(cli.shutil, "get_terminal_size", return_value=terminal_size((54, 24))):
            code, out, err = self.run_cli("--plain")
        self.assertEqual((code, err), (0, ""))
        for name, index in (("GPT-6.2 Astra (max)", "62.14e"), ("GPT-6.2 Astra (xhigh)", "61.56")):
            with self.subTest(profile=name):
                line = next(line for line in out.splitlines() if name in line)
                self.assertEqual(line.split()[-2], index)
        rows = json.loads(self.run_cli("--json")[1])["rows"]
        max_profile = next(row for row in rows if row["name"] == "GPT-6.2 Astra (max)")
        self.assertEqual(max_profile["metrics"]["intelligence_index"], 62.1387990593969)
        self.assertTrue(max_profile["index_estimated"])

    def test_visible_catalog_preserves_hidden_routes_and_refreshed_measurements(self):
        target = self.write_routes(ROUTES.replace("preferred: null",
            "  claude/claude-fable-5-2/high: enabled\npreferred: claude/claude-fable-5-2/high"))
        before = target.read_bytes()
        self.refresh_from(current_page())
        for args in ((), ("--plain",), ("--plain", "--all")):
            shown = self.run_cli(*args)[1]
            for name in ("GPT-6.2 Astra", "GPT-6.2 Sol", "Claude Opus"):
                self.assertIn(name, shown)
            self.assertIn("Astra for Law 6.2", shown)  # public observation outside the registry
            for hidden in ("Fable", "Haiku", "Sonnet", "Gemini"):
                self.assertNotIn(hidden, shown)
        raw = json.loads(self.run_cli("--json", "--all")[1])["rows"]
        hidden = next(row for row in raw if row["route"] == "claude/claude-fable-5-2/high")
        self.assertTrue(hidden["routable"] and hidden["preferred"])
        self.assertEqual(hidden["state"], "enabled")
        self.assertEqual(target.read_bytes(), before)

    def test_an_agent_name_alone_does_not_identify_the_provider(self):
        self.codex_catalog({"deepseek-r1": ["high"]})
        target = self.write_routes(ROUTES.replace("preferred: null", "  codex/qwen3/high: enabled\npreferred: null"))
        before = target.read_bytes()
        for args in (("--plain",), ("--plain", "--all")):
            shown = self.run_cli(*args)[1].casefold()
            self.assertNotIn("deepseek", shown)
            self.assertNotIn("qwen3", shown)
        raw = json.loads(self.run_cli("--json", "--all")[1])["rows"]
        self.assertTrue(any(row["name"] == "DEEPSEEK-R1" for row in raw))
        self.assertTrue(any(row["route"] == "codex/qwen3/high" and row["preference"] == "enabled" for row in raw))
        self.assertEqual(target.read_bytes(), before)

    def test_json_rows_keep_unavailable_as_null(self):
        self.refresh_from(live_page())
        code, out, _err = self.run_cli("--json")
        document = json.loads(out)
        self.assertEqual((code, document["schema"]), (0, "pod-models/v1"))
        ultra = next(row for row in document["rows"] if row["route"] == "codex/gpt-6.1-sol/ultra")
        self.assertEqual((ultra["state"], ultra["metrics"]["cost_per_task_usd"], ultra["observed_publicly"]),
                         ("routable", None, False))
        terra = next(row for row in document["rows"] if row["name"] == "GPT-6.1 Terra (non-reasoning)")
        self.assertEqual((terra["route"], terra["state"]), (None, "no route"))
        self.assertIn("non-reasoning", terra["reason"])

    def test_status_is_offline_and_reports_each_source(self):
        self.write_routes(ROUTES)
        with patch.object(leaderboard, "fetch", lambda *a, **k: self.fail("status must not fetch")):
            code, out, _err = self.run_cli("status", "--json")
        status = json.loads(out)["status"]
        self.assertEqual(code, 0)
        self.assertIsNone(status["snapshot"])
        self.assertEqual(status["preferences"]["enabled"], 2)
        self.assertEqual(status["adapters"], {"codex": "no catalog", "claude": "no catalog"})
        self.assertEqual(status["registry"]["routes"], 60)
        self.refresh_from(page(HEADERS_WITH_UNKNOWN, ROWS_WITH_UNKNOWN))
        code, out, _err = self.run_cli("status")
        self.assertIn("Columns not known to Pod, not shown: Median Tokens/s", out)

    def test_explicit_flags_are_the_only_way_the_command_writes(self):
        target = self.write_routes(ROUTES)
        code, out, _err = self.run_cli("--enable", "claude/claude-fable-5-2/high",
                                       "--disable", "codex/gpt-6.1-sol/high",
                                       "--preferred", "claude/claude-fable-5-2/high")
        self.assertEqual(code, 0, out)
        loaded = routefile.load()
        self.assertEqual((loaded.routes["claude/claude-fable-5-2/high"], loaded.routes["codex/gpt-6.1-sol/high"],
                          loaded.preferred), ("enabled", "disabled", "claude/claude-fable-5-2/high"))
        before = target.read_bytes()
        code, _out, err = self.run_cli("--enable", "codex/gpt-9/low")
        self.assertEqual(code, 2)
        self.assertIn("To proceed:", err)
        code, out, _err = self.run_cli("--pin", "codex/gpt-6.1-sol/high", "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "route_not_enabled"))
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.run_cli("--preferred", "none")[0], 0)
        self.assertIsNone(routefile.load().preferred)


from tests.models.support import HEADERS, ROWS  # noqa: E402

HEADERS_WITH_UNKNOWN = HEADERS[:5] + ["Median Tokens/s"] + HEADERS[5:]
ROWS_WITH_UNKNOWN = [row[:5] + ["123"] + row[5:] for row in ROWS]


class IsolationTests(CliCase):
    def test_coordination_loads_no_workspace_parsing_or_network_code(self):
        code = ("import sys, pod.routefile, pod.paths, pod.yamlsubset, pod.errors\n"
                "bad = sorted(name for name in sys.modules if name.split('.')[0] in "
                "('curses', 'urllib', 'html', 'http', 'socket', 'ssl', 'zlib') or name.startswith('pod.models'))\n"
                "print(bad)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env={"PYTHONPATH": str(ROOT / "skills"), "PATH": "/usr/bin:/bin"})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr)

    def test_the_plain_paths_never_load_curses(self):
        code = ("import sys\nfrom pod.models import cli\ncli.main(['status'])\ncli.main(['--plain'])\n"
                "print('curses' in sys.modules, file=sys.stderr)")
        import os
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "skills")})
        self.assertEqual((result.returncode, result.stderr.strip()), (0, "False"), result.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()

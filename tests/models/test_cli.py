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
        self.assertIn("Gemini 4 Carbon (high)", out)
        self.assertNotIn("Qwen", out)
        self.assertNotIn("Qwen", self.run_cli("--all")[1])

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

    def test_visible_catalog_shows_three_providers_only_and_preserves_routes(self):
        target = self.write_routes(ROUTES.replace("preferred: null",
            "  claude/claude-fable-5-2/high: enabled\npreferred: claude/claude-fable-5-2/high"))
        before = target.read_bytes()
        self.refresh_from(current_page())
        for args in ((), ("--plain",), ("--plain", "--all")):
            shown = self.run_cli(*args)[1]
            for name in ("GPT-6.2 Astra", "GPT-6.2 Sol", "Claude Opus", "Claude Sonnet", "Claude Haiku",
                         "Claude Fable", "Gemini 4 Neon", "Gemini 4 Carbon"):
                self.assertIn(name, shown)
            self.assertIn("Astra for Law 6.2", shown)  # public observation outside the registry
            for non_three in ("Qwen", "DeepSeek", "Llama"):
                self.assertNotIn(non_three, shown)
        raw = json.loads(self.run_cli("--json", "--all")[1])["rows"]
        fable = next(row for row in raw if row["route"] == "claude/claude-fable-5-2/high")
        self.assertTrue(fable["routable"] and fable["preferred"])
        self.assertEqual(fable["state"], "enabled")
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
        self.assertEqual(status["adapters"], {"antigravity": "no catalog", "codex": "no catalog", "claude": "no catalog"})
        self.assertEqual(status["registry"]["routes"], 70)
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

    def test_cli_twins_edit_profiles_paused_agents_and_review_agents(self):
        self.write_routes(ROUTES)
        code, out, err = self.run_cli(
            "--pause", "antigravity",
            "--profile", "scout-fast=investigate antigravity/gemini-4-neon/low codex/gpt-6.1-terra/low",
            "--review-agents", "codex,claude,antigravity",
        )
        self.assertEqual((code, err), (0, ""), out)
        loaded = routefile.load()
        self.assertEqual(loaded.paused_agents, ["antigravity"])
        self.assertEqual(loaded.review_agents, ["codex", "claude", "antigravity"])
        self.assertIn("scout-fast", loaded.profiles)
        code, out, err = self.run_cli("--unpause", "antigravity", "--delete-profile", "scout-fast")
        self.assertEqual((code, err), (0, ""), out)
        loaded = routefile.load()
        self.assertEqual(loaded.paused_agents, [])
        self.assertEqual(loaded.profiles, {})
        self.assertEqual(self.run_cli("--pause", "codex", "--unpause", "codex")[0], 2)
        self.assertEqual(self.run_cli("--profile", "missing_equals")[0], 2)

    def test_cli_twins_drain_and_pause_support_reason_ttl_and_unpause_clears_both(self):
        self.write_routes(ROUTES)
        code, out, err = self.run_cli("--drain", "claude", "--reason", "weekly_10pct", "--ttl", "2h")
        self.assertEqual((code, err), (0, ""), out)
        loaded = routefile.load()
        self.assertEqual(loaded.soft_drained_agents, ["claude"])
        self.assertEqual(loaded.soft_drained_meta["claude"].reason, "weekly_10pct")
        self.assertEqual(loaded.soft_drained_meta["claude"].ttl, 7200)
        self.assertIsNotNone(loaded.soft_drained_meta["claude"].until)
        code, out, err = self.run_cli("--pause", "antigravity", "--reason", "rate_limit", "--ttl", "30m")
        self.assertEqual((code, err), (0, ""), out)
        loaded = routefile.load()
        self.assertEqual(loaded.paused_agents, ["antigravity"])
        self.assertEqual(loaded.paused_meta["antigravity"].ttl, 1800)
        status = json.loads(self.run_cli("status", "--json")[1])["status"]["preferences"]
        self.assertEqual(status["soft_drained_agents"], ["claude"])
        self.assertEqual(status["paused_agents"], ["antigravity"])
        # --unpause clears both paused_agents and soft_drained_agents
        self.assertEqual(self.run_cli("--unpause", "claude", "--unpause", "antigravity")[0], 0)
        loaded = routefile.load()
        self.assertEqual((loaded.paused_agents, loaded.soft_drained_agents), ([], []))
        # Invalid combinations are refused
        self.assertEqual(self.run_cli("--pause", "claude", "--drain", "claude")[0], 2)
        self.assertEqual(self.run_cli("--reason", "orphan")[0], 2)
        self.assertEqual(self.run_cli("--drain", "claude", "--ttl", "bad")[0], 2)

    def test_cli_twins_pause_and_unpause_by_quota_pool(self):
        self.write_routes(ROUTES)
        code, out, err = self.run_cli("--pause", "agy-gemini", "--reason", "manual_1pct")
        self.assertEqual((code, err), (0, ""), out)
        loaded = routefile.load()
        self.assertEqual(loaded.paused_agents, ["agy-gemini"])
        self.assertEqual(loaded.paused_meta["agy-gemini"].reason, "manual_1pct")
        code, out, err = self.run_cli("--unpause", "agy-gemini")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(routefile.load().paused_agents, [])

    def test_refresh_isolates_antigravity_adapter_errors_from_aa_snapshot(self):
        from pod.models import adapters
        with patch.object(leaderboard, "fetch", return_value=current_page()), \
                patch.object(adapters, "refresh_antigravity",
                             return_value={"ok": False, "installed": True, "failure": "agy failed"}):
            code, out, err = self.run_cli("refresh", "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertTrue(payload["refresh"]["ok"])
        self.assertFalse(payload["adapters"]["antigravity"]["ok"])
        self.assertFalse(routefile.load().path.exists())

    def test_models_json_exposes_stable_routing_block_and_default_profile_cli_flag(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/low: enabled\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  agy-gemini: quota_low\n"
            "soft_drained_agents:\n"
            "  agy-claude: keep_bonus_pool\n"
            "profiles:\n"
            "  build-fast: implement antigravity/gemini-4-neon/medium codex/gpt-6.2-sol/medium\n"
            "  build-deep: implement antigravity/gemini-4-neon/high codex/gpt-6.2-sol/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high\n"
            "  scout: investigate+mcp antigravity/gemini-4-neon/low codex/gpt-6.2-sol/medium\n"
        )
        self.write_routes(cfg)
        code, out, err = self.run_cli("--default-profile", "implement=build-deep", "--json")
        self.assertEqual((code, err), (0, ""), out)
        document = json.loads(out)
        routing = document["routing"]
        self.assertEqual(routing["schema"], "pod-routing/v1")
        self.assertEqual(routing["status"], "ok")
        self.assertEqual(routing["default_profiles"],
                         {"implement": "build-deep", "review": "review-fast", "investigate": "scout"})
        self.assertEqual(routing["review_agents"], ["codex", "antigravity"])
        self.assertEqual(routing["profiles"]["scout"], {
            "role": "investigate",
            "mcp": True,
            "routes": ["antigravity/gemini-4-neon/low", "codex/gpt-6.2-sol/medium"],
            "spec": "investigate+mcp antigravity/gemini-4-neon/low codex/gpt-6.2-sol/medium",
        })
        self.assertEqual(routing["pauses"]["paused_agents"],
                         [{"agent": "agy-gemini", "reason": "quota_low", "until": None, "ttl": None, "active": True}])
        self.assertEqual(routing["pauses"]["soft_drained_agents"],
                         [{"agent": "agy-claude", "reason": "keep_bonus_pool", "until": None, "ttl": None, "active": True}])
        self.assertEqual(document["default_profiles"], routing["default_profiles"])
        self.assertEqual(document["review_agents"], ["codex", "antigravity"])
        self.assertEqual(self.run_cli("--default-profile", "missing_equals")[0], 2)
        # Profiles named 'none'/'null' can be set via --default-profile and cleared unambiguously via --clear-default.
        code, out, err = self.run_cli(
            "--profile", "null=implement codex/gpt-6.2-sol/medium",
            "--default-profile", "implement=null",
        )
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(routefile.load().default_profiles["implement"], "null")
        code, out, err = self.run_cli("--clear-default", "implement")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("implement", routefile.load().default_profiles)
        self.assertEqual(self.run_cli("--default-profile", "implement=build-deep", "--clear-default", "implement")[0], 2)

    def test_default_profile_against_absent_config_creates_file_even_when_matching_seed(self):
        target = routefile.load().path
        self.assertFalse(target.exists())
        code, out, err = self.run_cli("--default-profile", "implement=build-fast")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Saved ", out)
        self.assertTrue(target.exists())
        loaded = routefile.load(target)
        self.assertEqual(loaded.status, "ok", loaded.problem)
        self.assertEqual(loaded.default_profiles["implement"], "build-fast")


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

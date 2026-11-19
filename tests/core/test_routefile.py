from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
from unittest.mock import patch

from pod import cli, log, routefile, routing
from pod.errors import PodError
from tests.support import PodCase, ROOT

FULL = """schema: pod/v2
routes:
  codex/gpt-6.1-sol/high: enabled   # the usual one
  claude/claude-opus-5-5/max: disabled
preferred: codex/gpt-6.1-sol/high
pinned: null
workers:
  max_active: 6
authorization_verifier:
- /usr/local/bin/verifier
- --strict
decision_policy:
  preset: hands_off
  categories:
    merge_publish: any_agent
tool_permissions:
- tool: x
  decision:
    actor: {kind: person}
"""


class RouteFileTests(PodCase):
    def test_it_reads_routes_preferred_pin_and_the_verifier_and_ignores_keys_it_does_not_use(self):
        found = routefile.load(self.write_routes(FULL))
        self.assertEqual((found.status, found.problem), ("ok", None))
        self.assertEqual(found.enabled(), ["codex/gpt-6.1-sol/high"])
        self.assertEqual((found.preferred, found.pinned), ("codex/gpt-6.1-sol/high", None))
        self.assertEqual(found.authorization_verifier, ["/usr/local/bin/verifier", "--strict"])

    def test_only_the_person_setting_lets_the_coordinator_authorize(self):
        self.assertEqual(routefile.load(self.write_routes(FULL)).merge_authorization, "person")
        found = routefile.load(self.write_routes(FULL + "merge_authorization: coordinator\n"))
        self.assertEqual(found.merge_authorization, "coordinator")
        self.assertEqual(routefile.load(self.write_routes(FULL + "merge_authorization: anyone\n")).status, "invalid")

    def test_reading_never_writes_the_file(self):
        path = self.write_routes(FULL)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        routefile.load(path).check("codex/gpt-6.1-sol/high")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_coordinator_selection_limits_enabled_routes_before_balancing(self):
        text = """schema: pod/v2
routing: balance
routes:
  codex/gpt-6.2-sol/high: enabled
  codex/gpt-6-luna/high: enabled
  claude/claude-opus-5-5/high: enabled
  claude/claude-haiku-5-5/high: enabled
preferred: codex/gpt-6.2-sol/high
coordinator_routes:
- codex/gpt-6.2-sol/high
- claude/claude-opus-5-5/high
"""
        target = self.write_routes(text)
        with log.writer(log.path("repo", "objective"), create=True) as writer:
            writer.append(
                {"kind": "objective", "id": "objective"},
                {"kind": "assignment", "id": "a1", "route": "codex/gpt-6.2-sol/high"},
                {"kind": "assignment", "id": "a1", "task": "task_1", "dispatch": "ctx_1"})
        args = cli.parser().parse_args(["config", "--coordinator", "--json"])
        result = cli.execute(args, self.tmp)
        self.assertEqual(result["coordinator"]["eligible"],
                         ["codex/gpt-6.2-sol/high", "claude/claude-opus-5-5/high"])
        self.assertEqual(result["coordinator"]["selected"], "claude/claude-opus-5-5/high")
        self.assertEqual(target.read_text(), text)
        self.write_routes(text + "pinned: codex/gpt-6-luna/high\n")
        blocked = cli.execute(args, self.tmp)["coordinator"]
        self.assertEqual((blocked["eligible"], blocked["selected"]), ([], None))

    def test_invalid_balance_configuration_fails_closed(self):
        for setting in ("routing: on\n", "codex_share: true\n", "codex_share: 100\n",
                        "coordinator_routes:\n- nonsense\n"):
            with self.subTest(setting=setting):
                self.assertEqual(routefile.load(self.write_routes(FULL + setting)).status, "invalid")

    def test_balance_keeps_the_configured_share_when_the_window_rolls(self):
        text = FULL.replace("max: disabled", "max: enabled") + "routing: balance\ncodex_share: 60\n"
        preferences = routefile.load(self.write_routes(text))
        target = log.path("repo", "objective")
        with log.writer(target, create=True) as writer:
            writer.append({"kind": "objective", "id": "objective"})
            claude, codex = reversed(preferences.enabled())
            for index, route in enumerate([codex, claude] + [codex] * 12 + [claude] * 7):
                writer.append(
                    {"kind": "assignment", "id": f"a{index}", "route": route},
                    {"kind": "assignment", "id": f"a{index}", "task": f"task_{index}", "dispatch": f"ctx_{index}"})
        self.assertEqual(routing.history(), ["claude"] + ["codex"] * 12 + ["claude"] * 7)
        self.assertEqual(routing.choose(preferences, preferences.enabled()), (claude, "balance"))

    def test_history_uses_current_confirmations_and_the_observed_effective_route(self):
        target = log.path("repo", "objective")
        with log.writer(target, create=True) as writer:
            writer.append(
                {"kind": "objective", "id": "objective"},
                {"kind": "assignment", "id": "a1", "route": "codex/sol/high"},
                {"kind": "assignment", "id": "a1", "task": "task_1", "dispatch": "ctx_1",
                 "at": "2000-01-01T00:00:00Z"},
                {"kind": "assignment", "id": "a2", "route": "codex/sol/high"},
                {"kind": "assignment", "id": "a2", "task": "task_2", "dispatch": "ctx_2",
                 "effective": "claude/opus/high"})
        with target.open("ab") as stream:
            stream.write(b'{"kind":')
        self.assertEqual(routing.history(), ["claude"])

    def test_history_excludes_a_ten_day_old_dispatch_in_a_recent_log(self):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        target = log.path("repo", "objective")
        with log.writer(target, create=True) as writer:
            writer.append(
                {"kind": "objective", "id": "objective"},
                {"kind": "assignment", "id": "a1", "route": "codex/sol/high"},
                {"kind": "assignment", "id": "a1", "task": "task_1", "dispatch": "ctx_1", "at": old.isoformat()})
        self.assertEqual(routing.history(), [])

    def test_history_skips_logs_untouched_for_more_than_a_week(self):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        target = log.path("repo", "old")
        target.parent.mkdir(parents=True)
        target.write_text("unreadable\n")
        os.utime(target, (old.timestamp(), old.timestamp()))
        self.assertEqual(routing.history(), [])

    def test_history_unavailable_when_enumeration_exceeds_the_path_budget(self):
        with log.writer(log.path("repo", "objective"), create=True) as writer:
            writer.append({"kind": "objective", "id": "objective"})
        with patch.object(log, "RECENT_PATH_LIMIT", 1):
            self.assertIsNone(routing.history())

    def test_history_unavailable_with_a_symlinked_repository_or_log(self):
        target = log.path("repo", "objective")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        target.parent.parent.mkdir(parents=True)
        target.parent.symlink_to(elsewhere, target_is_directory=True)
        self.assertIsNone(routing.history())

        target.parent.unlink()
        target.parent.mkdir()
        copied = elsewhere / "elsewhere.jsonl"
        copied.touch()
        target.symlink_to(copied)
        self.assertIsNone(routing.history())

    def test_dispatch_requires_confirmation_and_must_not_be_void_to_count(self):
        intent = {"kind": "assignment", "id": "a1", "route": "codex/sol/high"}
        confirmation = {"kind": "assignment", "id": "a1", "task": "task_1", "dispatch": "ctx_1"}
        void = {"kind": "assignment", "id": "a1", "void": True}
        target = log.path("repo", "objective")
        with log.writer(target, create=True) as writer:
            writer.append({"kind": "objective", "id": "objective"}, intent)
            self.assertEqual(routing.history(), [])
            writer.append(confirmation)
            self.assertEqual(routing.history(), ["codex"])
            writer.append(void)
            self.assertEqual(routing.history(), [])

    def test_balanced_ties_use_preferred_or_the_first_family(self):
        text = FULL.replace("max: disabled", "max: enabled") + "routing: balance\n"
        codex, claude = routefile.load(self.write_routes(text)).enabled()
        for preferred, expected in ((claude, claude), ("null", codex)):
            with self.subTest(preferred=preferred):
                settings = routefile.load(self.write_routes(text.replace(f"preferred: {codex}", f"preferred: {preferred}")))
                self.assertEqual(routing.choose(settings, settings.enabled()), (expected, "balance"))

    def test_unreadable_history_falls_back_even_if_an_empty_sample_would_choose_differently(self):
        text = FULL.replace("max: disabled", "max: enabled") + "routing: balance\ncodex_share: 20\n"
        settings = routefile.load(self.write_routes(text))
        codex, claude = settings.enabled()
        self.assertEqual(routing.choose(settings, settings.enabled()), (claude, "balance"))
        damaged = log.path("repo", "damaged")
        damaged.parent.mkdir(parents=True)
        damaged.write_text("unreadable\n")
        args = cli.parser().parse_args(["config", "--coordinator", "--json"])
        result = cli.execute(args, self.tmp)["coordinator"]
        self.assertEqual((result["selected"], result["basis"]), (codex, "history_unavailable"))

    def test_coordinator_default_pin_and_empty_allow_list(self):
        args = cli.parser().parse_args(["config", "--coordinator", "--json"])
        route = "codex/gpt-6.1-sol/high"
        for text, eligible, selected, basis in (
                (FULL, [route], route, "preferred"),
                (FULL.replace("pinned: null", f"pinned: {route}") + f"coordinator_routes:\n- {route}\n",
                 [route], route, "pin"),
                (FULL + "coordinator_routes: []\n", [], None, "preferred")):
            with self.subTest(basis=basis, eligible=eligible):
                self.write_routes(text)
                self.assertEqual(cli.execute(args, self.tmp)["coordinator"],
                                 {"eligible": eligible, "selected": selected, "basis": basis})

    def test_a_route_that_is_not_enabled_is_refused_with_the_enabled_ones(self):
        found = routefile.load(self.write_routes(FULL))
        for route, code in (("claude/claude-opus-5-5/max", "route_not_enabled"), ("codex/unlisted/high", "route_not_enabled"),
                            ("nonsense", "route_invalid")):
            with self.assertRaises(PodError) as caught:
                found.check(route)
            self.assertEqual(caught.exception.code, code)
            self.assertIn("codex/gpt-6.1-sol/high", caught.exception.fix)

    def test_while_a_pin_is_set_no_other_route_is_used(self):
        text = FULL.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high").replace("max: disabled", "max: enabled")
        found = routefile.load(self.write_routes(text))
        found.check("codex/gpt-6.1-sol/high")
        with self.assertRaises(PodError) as caught:
            found.check("claude/claude-opus-5-5/max")
        self.assertEqual(caught.exception.code, "route_not_pinned")

    def test_an_absent_or_unreadable_file_makes_no_route_eligible_and_is_left_alone(self):
        absent = routefile.load()
        self.assertEqual((absent.status, absent.enabled()), ("absent", []))
        for text in ("schema: pod/v1\nmodels: {}\n", "schema: pod/v2\nroutes:\n  bad key: enabled\n",
                     "schema: pod/v2\nroutes:\n  a/b/c: maybe\n", "schema: pod/v2\npreferred: a/b/c\n",
                     "schema: pod/v2\nroutes: [a, b]\n", "\tschema: pod/v2\n", "schema: pod/v2\nschema: pod/v2\n"):
            path = self.write_routes(text)
            found = routefile.load(path)
            self.assertEqual((found.status, found.enabled(), found.merge_authorization), ("invalid", [], "person"), text)
            self.assertTrue(found.problem)
            self.assertEqual(path.read_text(), text)
            with self.assertRaises(PodError) as caught:
                found.check("a/b/c")
            self.assertEqual(caught.exception.code, "routes_unavailable")

    def test_coordination_loads_no_model_terminal_or_network_code(self):
        code = ("import sys; import pod.cli, pod.commands, pod.gate, pod.routefile; "
                "bad = [m for m in sys.modules if m.split('.')[0] in ('curses', 'urllib', 'html', 'http', 'yaml') "
                "or m.startswith('pod.models')]; print(bad); raise SystemExit(bool(bad))")
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                              env={"PYTHONPATH": str(ROOT / "skills"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

import contextlib
import io
import json
import os

from pod import cli, commands
from tests.core.support import CoreCase
from tests.support import ROOT


class CliTests(CoreCase):
    def run_pod(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            os.chdir(here)
        return code, out.getvalue(), err.getvalue()

    def test_the_whole_path_from_the_command_line(self):
        code, out, _ = self.run_pod("open", "--text", "Make the widget", "--objective", "widget", "--criterion", "it works")
        self.assertEqual(code, 0)
        self.assertIn("- c1 open: it works", out)
        code, out, _ = self.run_pod("dispatch", "--role", "implement", "--title", "write", "--spec", "Do it.", "--paths", "src, docs")
        self.assertEqual((code, "assignment: a1" in out), (0, True))
        commit = self.commit({"src/a.py": "a\n"})
        self.finish("task_1", {"outcome": "succeeded", "commit": commit})
        self.assertEqual(self.run_pod("accept", "a1", "--integrated", commit)[0], 0)
        self.assertEqual(self.run_pod("record", "--met", "c1", "--facts", "5", "--note", "ready to deliver")[0], 0)
        self.pull_request()
        code, out, _ = self.run_pod("deliver", "--check", "--json")
        self.assertEqual((code, json.loads(out)["pass"], json.loads(out)["authorized"]), (0, True, False))
        code, _, err = self.run_pod("deliver")
        self.assertEqual(code, 2)
        self.assertIn("refused (authorization_required)", err)
        self.assertIn("fix: ask the person", err)
        self.assertEqual(self.run_pod("record", "--authorize", "--words", "go ahead")[0], 0)
        code, out, _ = self.run_pod("deliver")
        self.assertEqual((code, "state: closed" in out), (0, True))
        code, out, _ = self.run_pod("status")
        self.assertIn("state: closed", out)

    def test_a_refusal_says_what_to_change_and_json_carries_the_same(self):
        code, out, _ = self.run_pod("status", "--json")
        refused = json.loads(out)["refused"]
        self.assertEqual((code, refused["code"]), (2, "objective_unclear"))
        self.assertIn("pod open", refused["fix"])
        self.opened()
        code, _, err = self.run_pod("deliver")
        self.assertEqual(code, 2)
        self.assertIn("needs: criterion c1 met or dropped", err)

    def test_a_failing_check_only_verdict_exits_nonzero_without_being_a_refusal(self):
        self.opened()
        code, out, _ = self.run_pod("deliver", "--check", "--json")
        self.assertEqual((code, json.loads(out)["pass"]), (1, False))

    def test_the_list_read_for_other_tools(self):
        self.opened()
        code, out, _ = self.run_pod("status", "--all", "--json")
        self.assertEqual((code, [row["objective"] for row in json.loads(out)["objectives"]]), (0, ["widget"]))

    def test_config_shows_the_preferences_and_changes_nothing(self):
        from pod import paths
        before = paths.route_file().read_bytes()
        code, out, _ = self.run_pod("config", "--json")
        shown = json.loads(out)
        self.assertEqual((code, shown["status"], shown["enabled"], shown["merge_authorization"]),
                         (0, "ok", ["claude/opus/high", "codex/sol/high"], "person"))
        self.assertEqual(paths.route_file().read_bytes(), before)

    def test_the_coordinator_facing_surface_is_the_eight_commands(self):
        choices = cli.parser()._subparsers._group_actions[0].choices
        self.assertEqual(sorted(choices), sorted(["open", "dispatch", "accept", "record", "status", "deliver", "resume", "drop",
                                                  "config", "models", "doctor", "update", "export"]))
        self.assertEqual(self.run_pod("status", "--local")[0], 2)

    def test_output_is_compact_text(self):
        text = cli.render({"objective": "x", "empty": [], "none": None, "criteria": ["c1 open: a"], "gate": {"needs": ["one"]}})
        self.assertEqual(text, "objective: x\ncriteria:\n  - c1 open: a\ngate:\n  needs:\n    - one")


class ArchitectureTests(CoreCase):
    GONE = ("obligations", "ledger", "governor", "governor_effects", "governance", "handoff", "succession", "window",
            "evidence_closure", "retirement", "abandoned", "readiness", "admission", "admission_view", "process_exit",
            "listing", "placement", "cleanup_observations", "cleanup_archives", "cleanup_stashes", "history", "history_tree", "internal", "contracts", "records", "assurance",
            "lineage", "local_append", "display", "worker_tabs", "wake", "standing", "relay", "policy", "tool_permissions",
            "pod_ticks", "pod_mapping", "issue_revision", "tui", "tui_state", "tui_render", "observations", "sources")

    def test_the_replaced_modules_and_the_inventory_are_gone(self):
        for name in self.GONE:
            self.assertFalse((ROOT / "skills" / "pod" / f"{name}.py").exists(), name)
        for name in ("docs/spec", "docs/pod-spec.md", "docs/pod-coverage.json", "docs/validation.md", "tools/suite_lanes.py",
                     "tools/isolate.py", "tests/suite_lanes.json"):
            self.assertFalse((ROOT / name).exists(), name)

    def test_the_core_never_reads_records_written_by_0_21(self):
        import re
        users = [module.name for module in (ROOT / "skills" / "pod").glob("*.py")
                 if module.name not in ("export.py", "cli.py")        # the command line only hands `pod export` over
                 and re.search(r"from \. import .*\bexport\b|from \.export\b|import pod\.export", module.read_text())]
        self.assertEqual(users, [])

    def test_the_writer_is_never_a_terminal_handle(self):
        self.opened()
        commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        text = json.dumps(self.entries("widget"))
        self.assertNotIn("term_", text)
        self.assertNotIn("handle", text)

    def test_pod_stores_identifiers_and_never_copies_of_provider_state(self):
        self.issue(5, "Make it", "- [ ] works\n")
        commands.open_(self.repo, source="5", objective="five")
        started = commands.dispatch(self.repo, "five", role="investigate", title="look", spec="A long task text. " * 50)
        self.finish(started["task"], {"outcome": "succeeded", "summary": "s"}, text="A long report. " * 500)
        commands.accept(self.repo, "five", "a1")
        size = len(json.dumps(self.entries("five")))
        self.assertLess(size, 2500, "task text, issue body and report text stay with their owners")

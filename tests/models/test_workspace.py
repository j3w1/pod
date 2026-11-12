import curses
import locale
import os
import pty
import select
import sys
import time
import unittest
from unittest.mock import patch

from pod import routefile
from pod.models import snapshot, table, view, workspace
from tests.models.support import ModelCase, ROUTES, live_page, page, without
from tests.support import ROOT


def text(lines) -> list:
    return ["".join(part for part, _style in segments) for segments, _selected in lines]


class WorkspaceCase(ModelCase):
    def setUp(self):
        super().setUp()
        self.target = self.write_routes(ROUTES)
        self.refresh_from(live_page())
        self.fetches = []
        self.space = workspace.Workspace(refresh=lambda: self.fetches.append(1) or snapshot.refresh(live_page))

    def go(self, name: str) -> None:
        self.space.cursor = [row.name for row in self.space.rows].index(name)

    def keys(self, *keys) -> None:
        for key in keys:
            self.space.handle(key)


class KeyTests(WorkspaceCase):
    def test_opening_and_browsing_touch_neither_the_network_nor_the_file(self):
        before = self.target.read_bytes()
        self.keys("down", "j", "pagedown", "up", "k", "end", "home", "s", "r", "f", "enter")
        self.keys("/", "s", "o", "l", "enter")
        self.space.lines(120, 40)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.space.search, "sol")

    def test_space_enables_and_disables_the_selected_route(self):
        self.go("Claude Fable 5.2 (high)")
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-fable-5-2/high"], "enabled")
        self.assertEqual(self.space.current.name, "Claude Fable 5.2 (high)")
        self.assertEqual(self.space.current.state, "enabled")
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-fable-5-2/high"], "disabled")
        self.assertIn("now disabled", self.space.message)

    def test_a_row_without_a_route_cannot_be_changed_and_says_why(self):
        before = self.target.read_bytes()
        self.go("GPT-6.1 Terra (non-reasoning)")
        self.keys(" ", "p", "P", "E")
        self.assertEqual(self.target.read_bytes(), before)
        self.go("GPT-6.1 Terra (non-reasoning)")
        self.keys(" ")
        self.assertIn("no non-reasoning effort", self.space.message)

    def test_bulk_keys_change_every_effort_of_one_model(self):
        self.go("GPT-6.1 Terra (low)")
        self.keys("E")
        routes = routefile.load().routes
        self.assertEqual({routes[f"codex/gpt-6.1-terra/{effort}"] for effort in
                          ("low", "medium", "high", "xhigh", "max", "ultra")}, {"enabled"})
        self.assertEqual(routes["codex/gpt-6.1-sol/medium"], "disabled")
        self.keys("D")
        self.assertEqual(set(routefile.load().routes[f"codex/gpt-6.1-terra/{effort}"] for effort in ("low", "ultra")),
                         {"disabled"})

    def test_preferred_and_pin_toggle_on_an_enabled_route_only(self):
        self.go("GPT-6.1 Sol (high)")
        self.keys("p", "P")
        loaded = routefile.load()
        self.assertEqual((loaded.preferred, loaded.pinned), ("codex/gpt-6.1-sol/high",) * 2)
        self.keys(" ")
        self.assertIn("Not changed", self.space.message)
        self.assertEqual(routefile.load().routes["codex/gpt-6.1-sol/high"], "enabled")
        self.keys("p", "P")
        self.assertEqual((routefile.load().preferred, routefile.load().pinned), (None, None))
        self.go("GPT-6.1 Sol (medium)")
        self.keys("P")
        self.assertIn("not an enabled route", self.space.message)

    def test_a_file_changed_elsewhere_is_not_overwritten(self):
        self.target.write_text(ROUTES.replace("preferred: null", "preferred: codex/gpt-6.1-sol/high"))
        self.go("Claude Fable 5.2 (high)")
        self.keys(" ")
        self.assertIn("changed since it was shown", self.space.message)
        self.assertNotIn("claude/claude-fable-5-2/high", routefile.load().routes)
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-fable-5-2/high"], "enabled")

    def test_an_invalid_preference_file_is_shown_and_never_edited(self):
        self.target.write_text("schema: pod/v2\nroutes: [a]\n")
        before = self.target.read_bytes()
        space = workspace.Workspace(refresh=lambda: self.fail("no fetch"))
        self.assertIn("Preference file is invalid", "\n".join(text(space.lines(160, 30))))
        space.cursor = [row.name for row in space.rows].index("GPT-6.1 Sol (high)")
        space.handle(" ")
        self.assertIn("Not changed", space.message)
        self.assertIn("pod config edit", space.message)
        self.assertEqual(self.target.read_bytes(), before)

    def test_only_the_refresh_key_fetches_and_a_failure_keeps_the_snapshot(self):
        self.assertEqual(self.space.handle("R"), "refresh")
        self.space.refresh()
        self.assertEqual(len(self.fetches), 1)
        self.assertIn("Refreshed: 284 public rows", self.space.message)
        failing = workspace.Workspace(refresh=lambda: snapshot.refresh(lambda: page(*without("Creator"))))
        failing.refresh()
        self.assertIn("Refresh failed", failing.message)
        shown = "\n".join(text(failing.lines(160, 30)))
        self.assertIn("Last refresh failed", shown)
        self.assertIn("Public snapshot:", shown)
        self.assertEqual(len(snapshot.load()["rows"]), 284)

    def test_search_filter_sort_and_quit(self):
        self.keys("/", "f", "a", "b", "l", "e", "enter")
        self.assertEqual({row.model_id for row in self.space.rows}, {"claude-fable-5-1", "claude-fable-5-2"})
        self.keys("/", "escape")
        self.assertEqual(self.space.search, "")
        self.keys("f")
        self.assertEqual(self.space.provider, "all")
        self.assertEqual(len(self.space.rows), len(self.space.view.rows))
        self.keys("f")
        self.assertEqual({row.provider for row in self.space.rows}, {"Anthropic"})
        self.keys("s")
        self.assertEqual(self.space.sort, "cost")
        self.assertEqual(self.space.rows[0].name, "Claude Haiku 5.5 (low)")
        self.assertEqual(self.space.handle("q"), "quit")


class DrawTests(WorkspaceCase):
    def test_the_header_always_shows_the_snapshot_age_and_states_are_words(self):
        shown = text(self.space.lines(160, 30))
        self.assertRegex(shown[0], r"^Public snapshot: (just now|\d+ min ago) \(20")
        self.assertIn("Intelligence Index v5.0.1", shown[0])
        body = "\n".join(shown)
        for word in ("enabled", "disabled", "routable", "State", "Public", "Adapter"):
            self.assertIn(word, body)
        self.assertIn("space on/off", "\n".join(shown[-3:]))
        self.assertIn("q quit", "\n".join(text(self.space.lines(60, 30))[-4:]))
        self.assertTrue(all(len(line) <= 160 for line in shown))

    def test_without_a_snapshot_the_header_says_so_and_nothing_is_fetched(self):
        (snapshot.home() / "snapshot.json").unlink()
        empty = workspace.Workspace(refresh=lambda: self.fail("no fetch on open"))
        self.assertIn("No public snapshot yet", text(empty.lines(120, 20))[0])
        self.assertTrue(all(row.metrics["intelligence_index"] is None for row in empty.rows))

    def test_narrow_terminals_drop_columns_in_a_fixed_order(self):
        kept = {width: [key for key, *_rest in table.columns_for(width)] for width in range(160, 39, -1)}
        self.assertEqual(kept[160], [key for key, *_rest in table.COLUMNS])
        order = []
        for width in range(159, 39, -1):
            self.assertLessEqual(set(kept[width]), set(kept[width + 1]))
            order += [key for key in kept[width + 1] if key not in kept[width]]
        self.assertEqual(tuple(order), table.DROP_ORDER)
        self.assertEqual(kept[40], ["state", "name", "index", "cost"])
        self.assertTrue(all(len(line) <= 60 for line in text(self.space.lines(60, 20))))

    def test_the_cursor_row_stays_visible(self):
        self.keys("end")
        lines = self.space.lines(120, 15)
        self.assertEqual(sum(selected for _segments, selected in lines), 1)

    def test_high_value_is_marked_with_a_star_and_explained_in_the_detail_panel(self):
        self.go("GPT-6.1 Sol (high)")
        line = next(segments for segments, selected in self.space.lines(160, 60) if selected)
        self.assertIn(("   $0.59* ", "value"), line)
        self.keys("enter")
        shown = "\n".join(text(self.space.lines(160, 60)))
        self.assertIn("High value, benchmark-relative", shown)
        self.assertIn("not a subscription charge", shown)
        self.assertIn("Route: codex/gpt-6.1-sol/high", shown)
        self.assertIn("cost_per_task_usd: 0.59  [US dollars per benchmark task]", shown)
        self.assertIn("context_window_tokens: 1000000  [tokens]", shown)
        self.assertTrue(all(len(line) <= 60 for line in text(self.space.lines(60, 40))))
        self.go("GPT-6.1 Sol (ultra)")
        detail = "\n".join(table.detail(self.space.current, self.space.view.snapshot))
        self.assertIn("intelligence_index: n/a", detail)
        self.assertIn("no row on the leaderboard snapshot", detail)
        self.assertNotIn("High value", detail)

    def test_the_plain_table_names_every_reason_and_the_cost_statement(self):
        shown = table.plain(self.space.view, view.select(self.space.view), 140)
        self.assertIn("GPT-6.1 Terra (non-reasoning): ", shown)
        self.assertIn("no non-reasoning effort for gpt-6.1-terra", shown)
        self.assertIn(view.COST_STATEMENT, shown)
        self.assertIn("* high value", shown)


PAUSE = object()    # stands for the terminal sending nothing more for the moment


class FakeWindow:
    """A stand-in for the curses window: it hands out queued input as the terminal would deliver it."""

    def __init__(self, *chunks):
        self.pending = [item for chunk in chunks for item in (chunk if isinstance(chunk, str) else [chunk])]
        self.waits = []

    def get_wch(self):
        if not self.pending:
            raise AssertionError("the loop read past the end of the input")
        item = self.pending.pop(0)
        if item is PAUSE:
            raise curses.error("no input")
        return item

    def timeout(self, delay):
        self.waits.append(delay)

    def getmaxyx(self):
        return 40, 120

    def keypad(self, flag):
        pass

    def erase(self):
        pass

    def addstr(self, *args):
        pass

    def refresh(self):
        pass


class KeyReadingTests(WorkspaceCase):
    # Sequences curses does not name under TERM=screen-256color (Ctrl+Left first), each ending in a command key.
    UNRECOGNISED = ("\x1b[1;5D", "\x1b[1;5C", "\x1b[1;2P", "\x1b[1;3p", "\x1b[1;5E", "\x1bD", "\x1bE",
                    "\x1b ", "\x1bq")

    def test_no_byte_of_an_unrecognised_escape_sequence_is_a_command(self):
        before = self.target.read_bytes()
        self.go("GPT-6.1 Sol (high)")
        for sequence in self.UNRECOGNISED:
            with self.subTest(sequence=sequence):
                window = FakeWindow(sequence, PAUSE)
                key = workspace._read_key(window)
                self.assertIsNone(key)
                self.assertEqual(window.pending, [])
                self.assertEqual(window.waits[-1], -1, "reading blocks again afterwards")
                self.assertIsNone(self.space.handle(key))
                self.assertEqual(self.target.read_bytes(), before)

    def test_the_loop_ignores_such_sequences_and_changes_nothing(self):
        before = self.target.read_bytes()
        self.go("GPT-6.1 Sol (high)")
        chunks = [item for sequence in self.UNRECOGNISED for item in (sequence, PAUSE)]
        workspace._loop(FakeWindow(*chunks, "q"), self.space)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.space.current.name, "GPT-6.1 Sol (high)")
        self.assertEqual((self.space.detail, self.space.searching, self.space.sort), (False, False, "index"))

    def test_a_lone_escape_and_ordinary_keys_keep_their_meaning(self):
        window = FakeWindow("/so\x1b", PAUSE, "D", curses.KEY_DOWN, curses.KEY_RESIZE, PAUSE)
        read = [workspace._read_key(window) for _ in range(8)]
        self.assertEqual(read, ["/", "s", "o", "\x1b", "D", "down", None, None])
        self.keys("/", "s", "o", "\x1b")
        self.assertEqual((self.space.searching, self.space.search), (False, ""))


class LocaleTests(WorkspaceCase):
    def test_a_locale_the_host_has_not_generated_does_not_stop_the_workspace(self):
        opened = []
        with patch.object(locale, "setlocale", side_effect=locale.Error("unsupported locale setting")), \
                patch.object(curses, "wrapper", lambda loop, space: opened.append(space)):
            self.assertEqual(workspace.run(), 0)
        self.assertEqual(len(opened), 1)


class TerminalSmokeTest(WorkspaceCase):
    def test_the_workspace_opens_in_a_terminal_and_quits_on_q(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "skills"), "TERM": "xterm", "LINES": "30", "COLUMNS": "120"}
        pid, master = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-c",
                                       "from pod.models import cli; raise SystemExit(cli.main([]))"], env)
        seen, deadline, sent = b"", time.monotonic() + 20, False
        try:
            while time.monotonic() < deadline:
                ready, _w, _x = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    seen += chunk
                if not sent and b"space on/off" in seen:
                    os.write(master, b"q")
                    sent = True
            _pid, status = os.waitpid(pid, 0)
        finally:
            os.close(master)
        self.assertTrue(sent, seen[-400:])
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"Public snapshot", seen)
        self.assertEqual(self.target.read_text(), ROUTES)


if __name__ == "__main__":
    unittest.main()

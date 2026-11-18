import curses
import locale
import os
import pty
import select
import sys
import textwrap
import time
import unittest
from unittest.mock import patch

from pod import routefile
from pod.models import snapshot, table, view, workspace
from tests.models.support import FIXTURES, ModelCase, ROUTES, current_page, live_page, page, without
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
        self.go("Claude Opus 5.5 (low)")
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-opus-5-5/low"], "enabled")
        self.assertEqual(self.space.current.name, "Claude Opus 5.5 (low)")
        self.assertEqual(self.space.current.state, "enabled")
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-opus-5-5/low"], "disabled")
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
        self.go("Claude Opus 5.5 (low)")
        self.keys(" ")
        self.assertIn("changed since it was shown", self.space.message)
        self.assertNotIn("claude/claude-opus-5-5/low", routefile.load().routes)
        self.keys(" ")
        self.assertEqual(routefile.load().routes["claude/claude-opus-5-5/low"], "enabled")

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
        self.keys("/", *"Opus 5.5", "enter")
        self.assertEqual({row.model_id for row in self.space.rows}, {"claude-opus-5-5"})
        self.keys("/", "escape")
        self.assertEqual(self.space.search, "")
        self.assertEqual(self.space.provider, "all")
        self.assertCountEqual(self.space.rows, [row for row in self.space.view.rows if row.routable or row.observed])
        self.keys("f")
        self.assertEqual({row.provider for row in self.space.rows}, {"Anthropic"})
        self.keys("s")
        self.assertEqual(self.space.sort, "cost")
        self.assertEqual(self.space.rows[0].name, "Claude Haiku 5.5 (low)")
        self.assertEqual(self.space.handle("q"), "quit")


class DrawTests(WorkspaceCase):
    def test_first_frame_identifies_the_profile_and_its_guidance_in_the_inspector(self):
        self.refresh_from(current_page())
        for width, height in ((120, 24), (40, 16)):
            space = workspace.Workspace()
            lines = text(space.lines(width, height))
            if width == 120:
                pane = [line.split(" │ ", 1)[1] for line in lines if " │ " in line]
            else:
                divider = next(i for i, line in enumerate(lines) if line.startswith("─"))
                pane = lines[divider + 1:]
            self.assertEqual(pane[0], "Claude Fable 5.2 (max with fallback)")
            self.assertIn("fallback", " ".join(pane).casefold())
            self.assertIn(table.guidance(space.current)[0].split(".")[0][:25], " ".join(pane))

    def test_cached_fallback_is_inspectable_separately_from_the_ordinary_route(self):
        self.refresh_from(current_page())
        space = workspace.Workspace(refresh=lambda: self.fail("opening the cache fetched the source"))
        self.assertEqual((space.current.name, space.current.route, space.current.metrics["intelligence_index"]),
                         ("Claude Fable 5.2 (max with fallback)", None, 65))
        space.handle("enter")
        before = text(space.lines(40, 12))
        self.assertIn("[] scroll · Claude Fable 5.2 (max with fallback)", " ".join(before))
        space.handle("]")
        self.assertNotEqual(before, text(space.lines(40, 12)))
        space.handle("enter")
        space.handle(" ")
        self.assertEqual(self.target.read_text(), ROUTES)
        space.handle("left")
        self.assertEqual(space.current.route, "claude/claude-fable-5-2/max")
        self.assertFalse(space.current.observed)
        self.assertIsNone(space.current.metrics["intelligence_index"])

    def test_first_answer_latency_uses_its_own_label_and_public_selection_survives_reordering(self):
        self.refresh_from((FIXTURES / "leaderboard-profiles.html").read_text())
        space = workspace.Workspace()
        space.handle("down")
        space.handle("down")
        self.assertEqual(space.current.name, "Qwen4 Max")
        inspector = "\n".join(text(space.lines(200, 50)))
        self.assertIn("155.4 s to first answer token", inspector)
        shot = snapshot.load()
        shot["rows"].reverse()
        with patch.object(snapshot, "load", return_value=shot):
            space.reload()
        self.assertEqual(space.current.name, "Qwen4 Max")
        self.assertEqual(space.current.release, "qwen4-max")

    def test_table_only_profile_keeps_focus_when_snapshot_rows_move(self):
        self.refresh_from(current_page())
        space = workspace.Workspace()
        space.handle("down")
        space.handle("down")
        self.assertEqual(space.current.name, "Gemini 4 Carbon (high)")
        snapshot_data = snapshot.load()
        snapshot_data["rows"].reverse()
        with patch.object(snapshot, "load", return_value=snapshot_data):
            space.reload()
        self.assertEqual(space.current.name, "Gemini 4 Carbon (high)")
        self.assertEqual(space.current.provider, "Google")

    def test_refresh_keeps_a_unique_public_selection_when_the_snapshot_gains_release_ids(self):
        self.refresh_from(current_page())
        self.space.reload()
        self.go("Qwen4 Max")
        self.assertIsNone(self.space.current.release)
        self.refresh_from((FIXTURES / "leaderboard-profiles.html").read_text())
        self.space.reload()
        self.assertEqual(self.space.current.release, "qwen4-max")
        self.assertEqual(self.space.current.provider, "Alibaba")

    def test_model_navigation_groups_efforts_and_effort_keys_visit_exact_routes(self):
        self.refresh_from(current_page())
        space = workspace.Workspace()
        screen = "\n".join(text(space.lines(200, 50)))
        self.assertIn("Model", screen)
        self.assertIn("Index", screen)
        self.assertIn("$/task", screen)
        self.assertIn("█", screen)
        self.assertEqual(space.current.model_id, "claude-fable-5-2")
        space.handle("down")
        self.assertEqual(space.current.name, "GPT-6.2 Astra (max)")
        seen = {}
        for _ in range(6):
            row = space.current
            seen[row.route] = row.metrics["intelligence_index"]
            self.assertIn(row.route, "\n".join(text(space.lines(100, 28))))
            space.handle("right")
        self.assertEqual(seen, {f"codex/gpt-6.2-astra/{effort}": 62 if effort == "max" else None
                                for effort in ("low", "medium", "high", "xhigh", "max", "ultra")})
        self.assertEqual(space.current.effort, "max")
        space.handle("left")
        self.assertEqual(space.current.effort, "xhigh")

    def test_expanded_profile_scores_follow_keyboard_navigation_without_changing_routes(self):
        self.refresh_from((FIXTURES / "leaderboard-profiles.html").read_text())
        space = workspace.Workspace()
        self.assertEqual(space.current.name, "Claude Fable 5.2 (max with fallback)")
        self.assertIsNone(space.current.route)
        self.assertEqual(space.current.profile, "Max, Default Fallback")
        space.handle("down")
        self.assertEqual(space.current.route, "codex/gpt-6.2-astra/max")
        space.handle("left")
        self.assertEqual(space.current.route, "codex/gpt-6.2-astra/xhigh")
        lines = text(space.lines(100, 28))
        self.assertIn("61.6", next(line for line in lines if line.startswith("▸")))
        space.handle("enter")
        self.assertIn("61.5602325371388", "\n".join(text(space.lines(100, 60))))
        space.handle("right")
        space.handle("right")
        self.assertEqual(space.current.effort, "ultra")
        self.assertIsNone(space.current.metrics["intelligence_index"])
        self.assertEqual(self.target.read_text(), ROUTES)

    def test_selected_details_follow_navigation_and_fit_the_terminal(self):
        for width, height in ((120, 24), (60, 20), (40, 16)):
            with self.subTest(width=width):
                self.keys("home")
                for move in ("down", "end", "up"):
                    self.keys(move)
                    lines = text(self.space.lines(width, height))
                    self.assertIn(self.space.current.name, " ".join(part.strip() for part in lines))
                    self.assertLessEqual(len(lines), height)
                    self.assertTrue(all(len(line) <= width for line in lines))
                    self.assertIn("/ search", "\n".join(lines))
                    self.assertIn("? keys", "\n".join(lines))
                    self.keys("?")
                    self.assertIn("P Pin", "\n".join(text(self.space.lines(width, height))))
                    self.keys("?")

    def test_opening_and_moving_show_the_selected_familys_advice(self):
        self.refresh_from(current_page())
        for width, height in ((120, 24), (40, 16)):
            space = workspace.Workspace()
            space.handle("/")
            for key in "GPT-6.2":
                space.handle(key)
            space.handle("enter")
            shown = "\n".join(text(space.lines(width, height)))
            self.assertIn("GPT-6.2 Astra (max)", shown)
            self.assertIn("Try high effort for a bug", shown)
            space.handle("down")
            shown = "\n".join(text(space.lines(width, height)))
            self.assertIn("GPT-6.2 Sol (max)", shown)
            self.assertIn("Start a well-specified feature", shown)
            self.assertNotIn("Try high effort for a bug", shown)

    def test_every_wrapped_detail_line_can_be_read_in_order(self):
        self.refresh_from(current_page())
        space = workspace.Workspace()
        space.handle("down")  # Astra's detail title fits the 40-column scrolling fixture.
        space.handle("enter")
        expected = [part for item in table.detail(space.current, space.view.snapshot)[1:]
                    for part in textwrap.wrap(item.strip(), 39)]
        def panel():
            lines = text(space.lines(40, 12))
            start = lines.index("[] scroll · " + space.current.name) + 1
            end = next(i for i in range(start, len(lines)) if "/ search" in lines[i])
            return lines[start:end]

        window = len(panel())
        self.assertGreater(window, 0)
        for start in range(len(expected) - window + 1):
            self.assertEqual(panel(), expected[start:start + window])
            space.handle("]")

    def test_the_header_always_shows_the_snapshot_age_and_states_are_words(self):
        shown = text(self.space.lines(160, 30))
        self.assertRegex(shown[0], r"^Public snapshot: (just now|\d+ min ago) \(20")
        self.assertIn("Intelligence Index v5.0.1", shown[0])
        body = "\n".join(shown)
        for word in (self.space.current.state, "Published", "Index", "$/task"):
            self.assertIn(word, body)
        self.assertIn("? keys", "\n".join(shown[-3:]))
        self.keys("?")
        self.assertIn("space on/off", "\n".join(text(self.space.lines(160, 30))))
        self.keys("?")
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
        self.assertTrue(any(part.strip() == "$0.59*" and style == "value" for part, style in line))
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
                if not sent and b"/ search" in seen:
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

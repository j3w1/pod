"""Real-terminal workspace behavior: sizes, contrast, resize, keyboard, saves and exits."""

import os
import re
import signal

from pod.config import load as load_config, set_route
from tests.install_support import ignored_sigint
from tests.pty_harness import PtyCase

SIZES = ((160, 45), (100, 30), (80, 24), (60, 20), (40, 12))
DOWN, UP, RIGHT, LEFT = "\x1bOB", "\x1bOA", "\x1bOC", "\x1bOD"


class TuiPtyTests(PtyCase):
    def table_row(self, session, key_part):
        return next((line for line in session.lines()[3:] if key_part in line), "")

    def cell_at(self, session, text, offset=0):
        for row, line in enumerate(session.lines()):
            column = line.find(text)
            if column >= 0:
                return session.cell(row, column + offset)
        return None

    def test_five_sizes_keep_the_bottom_dock_and_contrast(self):
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                session = self.open(cols=cols, rows=rows)
                lines = session.lines()
                bar = next(index for index, line in enumerate(lines) if "[Det" in line)
                self.assertGreater(bar, 3)
                self.assertNotIn("│", "\n".join(lines[:bar]))
                self.assertIn("Claude Opus 5.5", session.text())
                self.assertIn("q quit", lines[-1])
                if cols == 80:
                    heading = self.cell_at(session, "Sort")
                    label = self.cell_at(session, "Model")
                    metric = self.cell_at(session, "$5.98")
                    badge = self.cell_at(session, "enabled", offset=1)
                    for cell in (heading, label, metric, badge):
                        self.assertIsNotNone(cell)
                    self.assertNotEqual(metric["fg"], label["fg"])
                    self.assertNotEqual(badge["fg"], metric["fg"])
                    focused = session.cell(3, 10)
                    self.assertNotEqual(focused["bg"], "default", "focus has its own background")
                session.close()

    def test_resize_keeps_focus_and_compact_inspector(self):
        session = self.open(cols=100, rows=30)
        session.send(DOWN * 3)
        session.wait_for(lambda s: "Claude Opus 5.5 · high" in self.heading(s))
        session.resize(40, 12)
        session.wait_for(lambda s: "[Det]" in s.text() and "Claude Opus 5.5 · high" in s.text())
        session.send("\t" + "\x1b[6~")
        session.wait_for(lambda s: "Route key" not in s.text())
        session.resize(160, 45)
        session.send("\x1bOH")
        session.wait_for(lambda s: "Route key" in s.text() and "AA profile" in s.text())
        session.wait_for(lambda s: "Claude Opus 5.5 · high" in self.heading(s))

    def test_ascii_monochrome_help_and_native_unknown(self):
        session = self.open(LC_ALL="C", NO_COLOR="1")
        self.assertTrue(session.text().isascii())
        sgr = set(re.findall(rb"\x1b\[[0-9;]*m", session.output))
        self.assertTrue(sgr <= {b"\x1b[1m", b"\x1b[m", b"\x1b[7m", b"\x1b[1;7m", b"\x1b[0m", b"\x1b[0;1m",
                                b"\x1b[0;7m", b"\x1b[0;1;7m"}, sgr)
        self.assertIn(">+", session.text())
        self.assertIn("Native access unknown", session.text())
        session.send("?")
        session.wait_for("HELP: MODEL ROUTES")
        session.wait_for("Esc close")
        session.send("\x1b")
        session.wait_for("[Details]")

    def test_keyboard_moves_table_and_inspector_independently(self):
        session = self.open(cols=100, rows=30)
        first = self.heading(session)
        session.send(DOWN)
        session.wait_for(lambda s: self.heading(s) != first and " · " in self.heading(s))
        second = self.heading(session)
        session.send("\t" + RIGHT)
        session.wait_for("[Benchmarks]")
        session.send(DOWN * 3)
        session.settle()
        name, effort = second.split(" · ")
        focused = next(line for line in session.lines()[3:] if line.startswith("▸"))
        self.assertIn(name, focused)
        self.assertIn(effort, focused.split())
        session.send("\x1b" + UP)
        session.wait_for(lambda s: self.heading(s) != second)
        session.send("\x1b[6~\x1bOH")
        session.wait_for(lambda s: any(line.startswith("▸") and "Opus 5.5" in line and " max " in line
                                       for line in s.lines()))

    def test_browsing_palette_sort_filter_compare_and_help_never_write(self):
        session = self.open()
        before, stamp = self.config.read_bytes(), self.config.stat().st_mtime_ns
        for key in ("s", "S", "g", LEFT, RIGHT, "g", "f", "o", "F", "/", "luna", "\r", "\x1b", "c", DOWN, "c", "e",
                    ":", "show the comparison", "\r", "]", "[", "?", "\x1b", ":", "sort", "\x1b", "\t", DOWN, "\x1b"):
            session.send(key)
            session.settle(quiet=.03, timeout=.3)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)
        self.assertNotIn("Saved:", session.text())
        self.assertFalse(self.cache.exists(), "browsing never refreshes data")

    def test_save_failure_keeps_bytes_and_stays_reachable(self):
        session = self.open()
        before = self.config.read_bytes()
        try:
            self.config.parent.chmod(0o500)
            session.send(" ")
            session.wait_for("Not saved", timeout=2)
            self.assertEqual(self.config.read_bytes(), before)
            session.send(DOWN + DOWN)
            session.settle(quiet=.3, timeout=1)
            self.assertIn("Not saved", session.text())
        finally:
            self.config.parent.chmod(0o700)
        session.send(" ")
        session.wait_for("Saved: Disabled")
        self.assertNotIn("Not saved", session.text())

    def test_two_windows_conflict_and_recover(self):
        first, second = self.open(), self.open()
        key = "claude/claude-opus-5-5/max"
        os.kill(second.pid, signal.SIGSTOP)
        try:
            second.send(" ")
            first.send(" ")
            first.wait_for(lambda _s: load_config(personal=self.config)["routes"][key] == "disabled")
        finally:
            os.kill(second.pid, signal.SIGCONT)
        second.wait_for("changed elsewhere", timeout=3)
        self.assertEqual(load_config(personal=self.config)["routes"][key], "disabled")
        second.send(" ")
        second.wait_for(lambda _s: load_config(personal=self.config)["routes"][key] == "enabled")

    def test_external_edit_invalid_yaml_and_read_only(self):
        session = self.open()
        key = "claude/claude-opus-5-5/max"
        set_route(self.config, key, "disabled", displayed=load_config(personal=self.config))
        session.wait_for(lambda s: "disabled" in self.table_row(s, "Opus 5.5"), timeout=2)
        session.wait_for("changed outside")
        self.config.write_text("schema: [\n")
        session.wait_for("READ-ONLY", timeout=3)
        before = self.config.read_bytes()
        session.send(" p")
        session.settle()
        self.assertIn("read-only", session.text())
        self.assertEqual(self.config.read_bytes(), before)

    def test_ctrl_c_and_sigterm_exit_without_writing(self):
        before = self.config.read_bytes()
        with ignored_sigint():
            first = self.open()
        first.send("\x03")
        first.wait_for(lambda s: s.closed, timeout=2)
        with ignored_sigint():
            second = self.open()
        os.kill(second.pid, signal.SIGTERM)
        second.wait_for(lambda s: s.closed, timeout=2)
        self.assertEqual(self.config.read_bytes(), before)

    def test_snapshot_helper_renders_cell_styling(self):
        from tests.tui_snapshot import render
        session = self.open()
        svg = render(session.screen, "pink")
        self.assertIn("<svg ", svg)
        self.assertIn("#1e1e2e", svg)
        self.assertIn(">P</text>", svg)
        with self.assertRaises(ValueError):
            render(session.screen, "missing")

    def test_too_small_terminal_recovers(self):
        session = self.open(cols=80, rows=24)
        session.resize(39, 11)
        session.wait_for("Terminal too small")
        session.resize(40, 12)
        session.wait_for("[Det]")


class LauncherPtyTests(PtyCase):
    def test_missing_preferences_open_read_only(self):
        self.config.unlink()
        session = self.open(wait="READ-ONLY")
        session.send(" ")
        session.settle()
        self.assertFalse(self.config.exists())
        self.assertIn("installer", session.text().replace("\n", " "))

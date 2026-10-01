"""Pure workspace frames: presets, bottom inspector dock, strict width sweep and terminal roles."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from pod import tui_render as tr
from pod import tui_state as ts
from pod.term import Capabilities, capabilities, display_width
from tests.test_workspace_state import T0, fixture_snapshot, prefs, press, state

SIZES = ((160, 45), (100, 30), (80, 24), (60, 20), (40, 12))
UNICODE = Capabilities(ascii_only=False, color=True)
ASCII = Capabilities(ascii_only=True, color=False)


def picture(current, size=(80, 24), caps=UNICODE):
    return tr.frame(current, *size, caps, T0, strict=True)


def modes() -> dict[str, ts.State]:
    base = state(prefs(pinned="codex/gpt-6.1-sol/high", preferred="claude/claude-opus-5-5/medium"))
    marked, _ = press(base, "c", "DOWN", "c", "DOWN", "c")
    return {"browse": base, "inspector": replace(base, pane="inspector", tab="sources"),
            "benchmarks": replace(base, tab="benchmarks", frontier=True),
            "routing": replace(base, tab="routing"),
            "compare": replace(marked, tab="compare", frontier=True),
            "grouped": replace(press(base, "g")[0], discovery="all"),
            "observations": replace(base, discovery="all", sort="output_tps"),
            "filtered": replace(base, provider="OpenAI", effort="max", query="gpt"),
            "empty": replace(base, query="none-such"),
            "help": replace(base, mode="help"), "palette": replace(base, mode="palette", palette_query="sort"),
            "bulk": press(base, "b")[0], "reset": press(base, ":", *"reset", "ENTER")[0],
            "error": ts.with_notice(base, "Not saved - " + "a long honest failure " * 6, error=True),
            "read_only": state(dict(prefs(), status="invalid", errors=[{"code": "invalid_yaml", "message": "Bad"}]))}


class FrameTests(unittest.TestCase):
    def test_strict_sweep_has_no_overflow_from_40_to_200_columns(self):
        for name, current in modes().items():
            for caps in (UNICODE, ASCII):
                for cols in range(40, 201):
                    for rows in ((12, 24, 45) if name in ("browse", "compare", "grouped") else (12, 30)):
                        try:
                            picture(current, (cols, rows), caps)
                        except ValueError as exc:  # pragma: no cover - the failure message is the point
                            self.fail(f"{name} {cols}x{rows} ascii={caps.ascii_only}: {exc}")

    def test_five_sizes_keep_a_full_width_bottom_dock_never_a_right_panel(self):
        for size in SIZES:
            with self.subTest(size=size):
                lines = picture(state(), size).plain.splitlines()
                self.assertEqual(len(lines), size[1])
                bar = next(index for index, line in enumerate(lines) if "[Det" in line)
                self.assertGreater(bar, 3, "the dock sits below the table")
                self.assertGreaterEqual(size[1] - 2 - bar, 2, "a compact dock stays reachable")
                self.assertTrue(lines[bar].startswith("─"))
                self.assertNotIn("│", "\n".join(lines[:bar]), "no side-by-side split")
                self.assertTrue(all(display_width(line) <= size[0] - 1 for line in lines))
                header = lines[2]
                self.assertIn("Index", header)
                self.assertIn("Route" if size[0] < 80 else "Model", header)
        self.assertEqual([tr.preset(cols) for cols, _rows in SIZES], ["wide", "normal", "normal", "narrow", "narrow"])

    def test_all_four_tabs_render_at_every_size_and_short_height(self):
        for size in SIZES:
            for tab in ts.TABS:
                with self.subTest(size=size, tab=tab):
                    text = picture(replace(state(), tab=tab, pane="inspector"), size).plain
                    full, short = tr.TAB_LABELS[tab]
                    self.assertTrue(f"[{full}]" in text or f"[{short}]" in text, text)

    def test_inspector_paging_reaches_every_line_at_any_height(self):
        for size in ((40, 12), (60, 20), (80, 24), (160, 45)):
            for tab in ("benchmarks", "sources"):
                with self.subTest(size=size, tab=tab):
                    current = replace(state(), tab=tab, pane="inspector")
                    full = tr.inspector_lines(current, size[0] - 1, UNICODE, T0)
                    seen, previous = [], None
                    for _ in range(200):
                        frame = picture(current, size)
                        if frame.inspector_scroll == previous:
                            break
                        previous = frame.inspector_scroll
                        seen.append(frame.plain)
                        current = ts.with_viewport(current, table=frame.table_scroll, table_page=frame.table_page,
                                                   inspector=frame.inspector_scroll,
                                                   inspector_page=frame.inspector_page)
                        current, _ = press(current, "PAGE_DOWN")
                    joined = "\n".join(seen)
                    for line in full:
                        self.assertIn(line.text, joined)

    def test_sort_metric_stays_visible_in_every_preset(self):
        headers = {"intelligence": "Index", "usd_per_task": "$/task", "output_tps": "Tok/s",
                   "first_response_s": "First s", "total_response_s": "Total s"}
        for name, header in headers.items():
            for size in SIZES:
                with self.subTest(sort=name, size=size):
                    current = replace(state(), sort=name, descending=name in ts.DESCENDING_FIRST)
                    arrow = "↓" if current.descending else "↑"
                    self.assertIn(header + arrow, picture(current, size).plain.splitlines()[2])

    def test_disclaimer_units_and_scope_are_visible(self):
        text = picture(state(), (100, 30)).plain
        self.assertIn(tr.SHORT_DISCLAIMERS[1], text)
        bench = " ".join(picture(replace(state(), tab="benchmarks"), (160, 45)).plain.split())
        self.assertIn(tr.DISCLAIMER, bench)
        for size in SIZES:
            first = picture(replace(state(), tab="benchmarks", pane="inspector"), size).plain
            self.assertIn("AA benchmark cost", first, "the full disclaimer opens the Benchmarks tab")
        help_text = " ".join(picture(replace(state(), mode="help"), (160, 45)).plain.split())
        for phrase in ("AA index: points", "USD per AA benchmark task", "no global rank", tr.DISCLAIMER):
            self.assertIn(phrase, help_text)

    def test_status_line_reports_counts_selections_age_refresh_native_and_path(self):
        current = state(prefs(pinned="codex/gpt-6.1-sol/high", preferred="claude/claude-opus-5-5/medium"))
        lines = picture(ts.with_refresh(current, "failed", "HTTP 403 bot challenge"), (160, 45)).plain.splitlines()
        self.assertIn("30/30 enabled", lines[0])
        self.assertIn("Pin codex/gpt-6.1-sol/high", lines[0])
        self.assertIn("Preferred claude/claude-opus-5-5/medium", lines[0])
        for phrase in ("Refresh failed: HTTP 403 bot challenge", "Data cache 1h old", "Native access unknown",
                       "Config /fixture/config.yaml"):
            self.assertIn(phrase, lines[-2])
        stale = replace(current, projection={**current.projection,
                                             "observations": {**current.projection["observations"], "stale": True,
                                                              "age_s": 8 * 86400}})
        self.assertIn("Data cache 8d old STALE", picture(stale, (100, 30)).plain)
        narrow = picture(current, (40, 12)).plain.splitlines()
        self.assertIn("Pin codex/gpt-6.1-sol/high", narrow[0])
        self.assertIn("Preferred claude/claude-opus-5-5/medium", narrow[-2], "exact keys move, never shorten")

    def test_stale_data_stays_marked_at_every_size_with_pin_and_preferred(self):
        pin, preferred = "codex/gpt-6.1-sol/high", "claude/claude-opus-5-5/medium"
        current = state(prefs(pinned=pin, preferred=preferred))
        stale = replace(current, projection={**current.projection,
                                             "observations": {**current.projection["observations"], "stale": True,
                                                              "age_s": 10 * 86400}})
        for size in SIZES:
            for caps in (UNICODE, ASCII):
                with self.subTest(size=size, ascii=caps.ascii_only):
                    text = picture(stale, size, caps).plain
                    self.assertIn("STALE", text)
                    self.assertIn("Pin " + pin, text)
                    self.assertIn("Preferred " + preferred, text)
        self.assertNotIn("STALE", picture(current, (40, 12)).plain)

    def test_marks_are_distinct_from_focus_and_ascii_safe(self):
        key = "codex/gpt-6.1-sol/high"
        current = replace(state(prefs(pinned=key, preferred=key, routes={key: "enabled"})), focus=key)
        current, _ = press(current, "c")
        frame = picture(current, (100, 30))
        focused = [line for line in frame.lines if line.styled[0].role == "focus"]
        self.assertEqual(len(focused), 1)
        self.assertTrue(focused[0].text.startswith("▸✓●★1"))
        others = [line.text for line in frame.lines[3:12] if line.text.startswith(" ·")]
        self.assertTrue(others, "not-set routes show their own mark")
        ascii_frame = picture(current, (100, 30), ASCII)
        self.assertTrue(ascii_frame.plain.isascii())
        self.assertIn(">+@*1", ascii_frame.plain)
        inspector = picture(replace(current, pane="inspector"), (100, 30))
        self.assertFalse([line for line in inspector.lines if line.styled[0].role == "focus"],
                         "inspector focus moves the highlight off the table")

    def test_no_superseded_concepts_appear(self):
        for name, current in modes().items():
            text = picture(current, (160, 45)).plain
            for word in ("Recommended", "All models", "My selection", "six", "Available"):
                self.assertNotIn(word, text, (name, word))

    def test_roles_and_too_small(self):
        roles = {span.role for line in picture(state(), (100, 30)).lines for span in line.styled}
        self.assertTrue({"body", "heading", "label", "value", "metric", "advisory", "key", "title", "focus",
                         "badge_enabled"} <= roles)
        small = tr.frame(state(), 39, 12, UNICODE, T0, strict=True)
        self.assertIn("Terminal too small", small.plain)

    def test_footer_describes_keys_and_fits(self):
        for size in SIZES:
            footer = picture(state(), size).plain.splitlines()[-1]
            self.assertIn("q quit", footer)
            self.assertIn("?", footer)
            self.assertIn(":", footer)
        self.assertIn("Enter save", picture(press(state(), "b")[0], (40, 12)).plain)

    def test_interpreter_coerced_c_locale_stays_ascii(self):
        """PEP 538 sets LC_CTYPE=C.UTF-8 for a LANG=C session; the terminal still declared C."""
        coerced = capabilities({"LANG": "C", "LC_CTYPE": "C.UTF-8", "TERM": "xterm-256color"})
        self.assertTrue(coerced.ascii_only)
        self.assertTrue(capabilities({"LC_CTYPE": "C.UTF-8", "TERM": "xterm-256color"}).ascii_only)
        self.assertFalse(capabilities({"LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8", "TERM": "xterm"}).ascii_only)
        self.assertFalse(capabilities({"LANG": "en_US.UTF-8", "TERM": "xterm"}).ascii_only)
        self.assertFalse(capabilities({"LANG": "C.UTF-8", "NO_COLOR": "1"}, has_colors=True).color)

    def test_sixteen_colour_light_palette_keeps_essential_roles_readable(self):
        from pod import tui
        pairs = {}
        with patch.multiple(tui.curses, start_color=lambda: None, use_default_colors=lambda: None,
                            init_pair=lambda index, fg, bg: pairs.__setitem__(index, fg),
                            color_pair=lambda index: index << 8, COLORS=16, create=True):
            for light in (True, False):
                pairs.clear()
                tui._styles(Capabilities(ascii_only=False, color=True, light_background=light))
                with self.subTest(light=light):
                    if light:
                        # The terminal defines these hues; on light themes only the body colour is safe.
                        self.assertEqual(set(pairs.values()), {tui.curses.COLOR_BLACK, tui.curses.COLOR_RED})
                    else:
                        self.assertNotIn(tui.curses.COLOR_BLACK, set(pairs.values()))

    def test_invalid_file_keeps_its_pin_visible_as_read_only_diagnostic(self):
        broken = state(dict(prefs(), status="invalid", pin_diagnostic="gpt-6-sol",
                            errors=[{"code": "invalid_pin", "message": "pinned 'gpt-6-sol' is not a supported route"}]))
        text = " ".join(picture(broken, (160, 45)).plain.split())
        for phrase in ("PREFERENCES UNAVAILABLE - READ-ONLY", "Run pod config edit", "Pin in file: gpt-6-sol",
                       "READ-ONLY"):
            self.assertIn(phrase, text)
        self.assertEqual(press(broken, " ", "p", "P", "b")[1], [])

    def test_unknown_metrics_render_as_dashes(self):
        current = state(snapshot=fixture_snapshot(edit=lambda rows: []))
        self.assertEqual({ts.value(row, "intelligence") for row in ts.routes(current)}, {None})
        rows = picture(current, (100, 30)).plain.splitlines()[3:8]
        self.assertTrue(all("—" in row for row in rows))
        self.assertIn("metrics unknown", " ".join(picture(replace(current, tab="benchmarks"), (160, 45)).plain.split()))


if __name__ == "__main__":
    unittest.main()

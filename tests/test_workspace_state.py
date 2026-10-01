"""Model workspace state, comparisons and session I/O with fixture data and controlled time."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from pod import config, observations, sources, tui
from pod import tui_render as tr
from pod import tui_state as ts
from pod.routes import project
from pod.term import Capabilities

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PAGES = {sources.AA: "aa-leaderboard.html", sources.ANTHROPIC: "anthropic-models-overview.html",
         sources.OPENAI: "openai-models.html"}
NAVIGATION = ("DOWN", "UP", "j", "k", "PAGE_DOWN", "PAGE_UP", "HOME", "END", "s", "S", "g", "LEFT", "RIGHT",
              "ENTER", "TAB", "]", "[", "ESC", "f", "o", "F", "c", "C", "e", "/", "x", "ENTER", "?", "ESC")


def page(source_id: str) -> str:
    return (FIXTURES / PAGES[source_id]).read_text(encoding="utf-8")


def fixture_snapshot(methodology: str | None = "Artificial Analysis Intelligence Index v4.3", *,
                     edit=None) -> dict:
    """A snapshot built from the sanitized fixture pages, independent of the bundled data."""
    blocks = {}
    for source in sources.SOURCES:
        parsed = sources.PARSERS[source.id](page(source.id))
        rows = parsed["rows"]
        if edit and source.id == sources.AA:
            rows = edit(rows)
        blocks[source.id] = {"url": source.url, "attribution": source.attribution, "required": source.required,
                             "status": "ok", "retrieved_at": "2026-10-01T11:00:00Z", "published_at": None,
                             "methodology": methodology if source.id == sources.AA else None,
                             "rows": rows, "diagnostics": []}
    return {"schema": observations.SCHEMA, "generation": "1", "created_at": "2026-10-01T11:00:00Z",
            "sources": blocks}


def view(snapshot: dict) -> dict:
    return {"status": "observed", "origin": "cache", "generation": snapshot["generation"],
            "created_at": snapshot["created_at"], "stale": False, "age_s": 3600, "diagnostics": [],
            "sources": snapshot["sources"]}


def prefs(**changes) -> dict:
    document = config.defaults()
    document.update(changes)
    return {"path": "/fixture/config.yaml", "revision": "r", "schema": "pod/v2", "status": "valid",
            "routes": document["routes"], "eligible": [], "preferred": document["preferred"],
            "pinned": document["pinned"], "max_active": 2, "refresh": "manual", "errors": [], "setup": None,
            "pin_diagnostic": None, "preferred_diagnostic": None}


def state(preferences: dict | None = None, snapshot: dict | None = None, **changes) -> ts.State:
    projection = project(Path("/nonexistent"), preferences=preferences or prefs(),
                         observations=view(snapshot or fixture_snapshot()), now=T0)
    return replace(ts.initial(projection), **changes)


def press(current: ts.State, *keys: str) -> tuple[ts.State, list]:
    effects = []
    for key in keys:
        current, effect = ts.reduce(current, key)
        if effect is not None:
            effects.append(effect)
    return current, effects


def text(current: ts.State, cols: int = 100, rows: int = 30, ascii_only: bool = False) -> str:
    return tr.frame(current, cols, rows, Capabilities(ascii_only, False), T0, strict=True).plain


def inspector(current: ts.State, tab: str, width: int = 400) -> str:
    """One inspector tab's full text, unwrapped enough to search sentences."""
    return "\n".join(line.text for line in tr.inspector_lines(replace(current, tab=tab), width,
                                                             Capabilities(False, False), T0))


class TableTests(unittest.TestCase):
    def test_rows_are_exact_routes_sorted_by_index_with_stable_ties(self):
        current = state()
        keys = ts.visible_keys(current)
        self.assertEqual(len(keys), 30)
        self.assertEqual(len(set(keys)), 30)
        scores = [ts.value(ts.route(current, key), "intelligence") for key in keys]
        known = [score for score in scores if score is not None]
        self.assertEqual(known, sorted(known, reverse=True))
        self.assertTrue(all(score is None for score in scores[len(known):]), "unknown values sort last")
        self.assertEqual(current.focus, keys[0])
        # Equal scores keep registry model order and then effort order, never an invented rank.
        order = ts.model_order(current)
        for first, second in zip(keys, keys[1:]):
            a, b = ts.route(current, first), ts.route(current, second)
            if ts.value(a, "intelligence") == ts.value(b, "intelligence") is not None:
                self.assertLessEqual((order.index(a["model"]), ts.effort_index(a["effort"])),
                                     (order.index(b["model"]), ts.effort_index(b["effort"])))
        self.assertEqual(ts.visible_keys(state()), keys, "same data, same order")

    def test_every_sort_in_both_directions_keeps_unknown_last(self):
        current = state()
        for name in ts.SORTS:
            for descending in (False, True):
                with self.subTest(sort=name, descending=descending):
                    ordered = [ts.route(current, key) for key in
                               ts.visible_keys(replace(current, sort=name, descending=descending))]
                    if name in ts.METRICS:
                        values = [ts.value(row, name) for row in ordered]
                        known = [value for value in values if value is not None]
                        self.assertEqual(known, sorted(known, reverse=descending))
                        self.assertTrue(all(value is None for value in values[len(known):]))
                    elif name == "effort":
                        efforts = [ts.effort_index(row["effort"]) for row in ordered]
                        self.assertEqual(efforts, sorted(efforts, reverse=descending))
                    elif name == "model":
                        names = [row["name"].casefold() for row in ordered]
                        self.assertEqual(names, sorted(names, reverse=descending))
        cycled, _ = press(current, "s")
        self.assertEqual((cycled.sort, cycled.descending), ("usd_per_task", False))
        reversed_, _ = press(cycled, "S")
        self.assertTrue(reversed_.descending)

    def test_grouped_view_collapses_and_keeps_routes_reachable(self):
        current, _ = press(state(), "g")
        rows = ts.items(current)
        self.assertEqual([item.kind for item in rows[:6]], ["group"] + ["route"] * 5)
        self.assertEqual([item.route["effort"] for item in rows[1:6]], list(ts.EFFORTS))
        current, _ = press(current, "LEFT")
        self.assertTrue(current.focus.startswith("model:"))
        self.assertIn(current.focus[6:], current.collapsed)
        current, _ = press(current, "RIGHT")
        self.assertEqual(current.collapsed, frozenset())
        current, _ = press(current, "ENTER")
        self.assertEqual(len(ts.items(current)), 30 + 6 - 5)
        self.assertIn("[+] Claude Opus 5.5 (5/5 enabled)", text(current))

    def test_filters_compose_and_search_matches_display_and_native_identity(self):
        current = state()
        current, _ = press(current, "f")
        self.assertEqual(current.provider, "Anthropic")
        current = ts.run_command(ts.run_command(current, "effort")[0], "effort")[0]
        self.assertEqual(current.effort, "medium")
        rows = [ts.route(current, key) for key in ts.visible_keys(current)]
        self.assertEqual({(row["provider"], row["effort"]) for row in rows}, {("Anthropic", "medium")})
        self.assertEqual(len(rows), 3)
        for query, expected in (("gpt-6.1-sol", 5), ("GPT-6 Luna", 5), ("codex/gpt-6-astra/max", 1)):
            searched, _ = press(state(), "/", *query)
            self.assertEqual(len(ts.visible_keys(searched)), expected, query)
        closed = press(state(), "/", "x", "y", "z", "ESC")[0]
        self.assertEqual(closed.query, "")

    def test_new_and_unsupported_observations_are_reachable_but_not_routable(self):
        current = state()
        self.assertFalse(any(key.startswith("obs:") for key in ts.visible_keys(current)))
        current, _ = press(current, "o")
        self.assertEqual(current.discovery, "new")
        found = [item.observation for item in ts.items(current)]
        self.assertTrue(found and all(row["discovery"] == "new" for row in found))
        current, _ = press(current, "o")
        unsupported = [item.observation for item in ts.items(current)]
        self.assertTrue(any(row["model"] == "gpt-6-sol" for row in unsupported))
        focused, effects = press(current, " ")
        self.assertEqual(effects, [])
        self.assertIn("not routable", focused.notice)
        self.assertEqual(press(current, "p")[1], [])

    def test_focus_and_hidden_selections_survive_filter_sort_and_refresh(self):
        pin = "codex/gpt-6-luna/high"
        current = state(prefs(pinned=pin, preferred="claude/claude-opus-5-5/medium"))
        current = replace(current, focus=pin)
        filtered, _ = press(current, "f")
        self.assertNotIn(pin, ts.visible_keys(filtered))
        self.assertEqual(filtered.hidden_focus, pin)
        self.assertEqual(ts.hidden_selections(filtered), {"preferred": False, "pinned": True})
        self.assertIn(f"Pin {pin} (hidden)", text(filtered))
        sorted_, _ = press(filtered, "s")
        refreshed = ts.with_projection(sorted_, sorted_.projection)
        restored, _ = press(refreshed, "F")
        self.assertEqual(restored.focus, pin)
        self.assertIsNone(restored.hidden_focus)
        searched, _ = press(current, "/", "q", "q", "q")
        self.assertEqual(ts.visible_keys(searched), [])
        self.assertIn("No rows match", text(searched))
        self.assertIn("(hidden)", text(searched))


class EditTests(unittest.TestCase):
    def test_route_state_preferred_and_pin_effects_name_exact_routes(self):
        current = state()
        key = current.focus
        _, effects = press(current, " ")
        self.assertEqual(effects[0].payload, {"routes": {key: "disabled"}})
        _, effects = press(current, "p")
        self.assertEqual(effects[0].payload, {"pinned": key})
        _, effects = press(current, "P")
        self.assertEqual(effects[0].payload, {"preferred": key})
        pinned = state(prefs(pinned=key, preferred=key))
        self.assertEqual(press(pinned, "p")[1][0].payload, {"pinned": None})
        self.assertEqual(press(pinned, "P")[1][0].payload, {"preferred": None})
        unset = state(prefs(routes={}))
        self.assertEqual(press(unset, " ")[1][0].payload, {"routes": {unset.focus: "enabled"}})
        self.assertEqual(ts.run_command(current, "unset")[1].payload, {"routes": {key: None}})

    def test_read_only_and_setup_required_files_refuse_edits(self):
        for status in ("invalid", "setup_required", "missing"):
            with self.subTest(status=status):
                current = state(dict(prefs(), status=status, errors=[{"code": "x", "message": "bad"}]))
                for key in (" ", "p", "P", "b"):
                    changed, effects = press(current, key)
                    self.assertEqual(effects, [])
                    self.assertTrue(changed.notice)
                self.assertIn("READ-ONLY" if status != "setup_required" else "SETUP REQUIRED", text(current))

    def test_navigation_view_and_compare_keys_never_write(self):
        current = state()
        for key in NAVIGATION:
            current, effect = ts.reduce(current, key)
            self.assertTrue(effect is None or effect.kind == "quit" and key == "q", key)
        for command in ("grouped", "provider", "model", "effort", "state", "discovery", "compare", "compare_show",
                        "frontier", "changes", "inspector", "next_tab", "help", *(f"sort:{n}" for n in ts.SORTS)):
            self.assertIsNone(ts.run_command(state(), command)[1], command)

    def test_bulk_scopes_preview_exact_routes_and_save_once(self):
        current = state(prefs(routes={"claude/claude-opus-5-5/low": "enabled"}))
        dialog, _ = press(replace(current, focus="claude/claude-opus-5-5/max"), "b")
        self.assertEqual(dialog.mode, "bulk")
        self.assertEqual(ts.bulk_changes(dialog, dialog.bulk),
                         {f"claude/claude-opus-5-5/{effort}": "enabled" for effort in ts.EFFORTS[1:]})
        self.assertIn("CHANGES (4)", text(dialog))
        saved, effects = press(dialog, "ENTER")
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].payload["routes"], ts.bulk_changes(dialog, dialog.bulk))
        self.assertEqual(saved.mode, "browse")
        ranged = replace(dialog, bulk=ts.Bulk(scope="range", action="enabled", low=3, high=4))
        self.assertEqual(set(ts.bulk_changes(ranged, ranged.bulk)),
                         {row["key"] for row in ts.routes(current) if row["effort"] in ("xhigh", "max")})
        unset = replace(dialog, bulk=ts.Bulk(scope="not_set", action="disabled"))
        self.assertEqual(len(ts.bulk_changes(unset, unset.bulk)), 29)
        cancelled, effects = press(dialog, "ESC")
        self.assertEqual((cancelled.mode, effects), ("browse", []))

    def test_reset_enables_listed_routes_and_keeps_pin_and_preference(self):
        key = "codex/gpt-6-astra/high"
        current = state(prefs(routes={key: "enabled", "codex/gpt-6-luna/low": "disabled"}, pinned=key, preferred=key))
        dialog, _ = press(current, ":", *"reset", "ENTER")
        self.assertEqual(dialog.bulk.scope, "reset")
        self.assertIn("routes added by a later Pod update are not included", text(dialog, 120, 40))
        _, effects = press(dialog, "ENTER")
        payload = effects[0].payload
        self.assertEqual(set(payload), {"routes"}, "reset never clears the pin or preference")
        self.assertEqual(len(payload["routes"]), 29)
        self.assertTrue(all(target == "enabled" for target in payload["routes"].values()))

    def test_bulk_disable_of_pin_requires_clearing_it_in_the_same_save(self):
        key = "claude/claude-opus-5-5/high"
        current = replace(state(prefs(pinned=key)), focus=key)
        dialog, _ = press(current, "b", "DOWN", "RIGHT")
        self.assertEqual(dialog.bulk.action, "disabled")
        self.assertEqual(ts.bulk_conflicts(dialog, dialog.bulk), {"pinned": key})
        refused, effects = press(dialog, "ENTER")
        self.assertEqual(effects, [])
        self.assertIn("Not saved", refused.error)
        cleared, _ = press(dialog, "DOWN", "RIGHT")
        self.assertTrue(cleared.bulk.clear_pin)
        _, effects = press(cleared, "ENTER")
        self.assertIsNone(effects[0].payload["pinned"])

    def test_palette_searches_and_runs_the_same_commands(self):
        current, _ = press(state(), ":", *"total")
        matches = ts.palette_matches(current)
        self.assertEqual(matches[0][0], "sort:total_response_s")
        current, effects = press(current, "ENTER")
        self.assertEqual((current.mode, current.sort, effects), ("browse", "total_response_s", []))
        listed = {command for command, _label, _key in ts.COMMANDS}
        for needed in ("refresh", "toggle", "preferred", "pin", "bulk", "reset", "grouped", "provider", "search",
                       "compare", "frontier", "help", "discovery", "refresh_setting"):
            self.assertIn(needed, listed)
        current, effects = press(state(), "CTRL_P", *"refresh public", "ENTER")
        self.assertEqual([effect.kind for effect in effects], ["refresh"])
        self.assertEqual(current.refresh_state, "running")
        closed, _ = press(state(), ":", "x", "ESC")
        self.assertEqual(closed.mode, "browse")


class ComparisonTests(unittest.TestCase):
    def test_compare_holds_four_routes_and_reports_bounded_deltas(self):
        current = state()
        for _ in range(5):
            current, _ = press(current, "c", "DOWN")
        self.assertEqual(len(current.compare), 4)
        self.assertIn("up to 4", current.notice)
        current, _ = press(current, ":", *"show the comparison", "ENTER")
        self.assertEqual(current.tab, "compare")
        rendered = inspector(current, "compare")
        self.assertIn("pts", rendered)
        self.assertIn("%", rendered)
        self.assertNotIn("twice", rendered.casefold())
        self.assertNotIn("times as", rendered.casefold())
        cleared, _ = press(current, "C")
        self.assertEqual((cleared.compare, cleared.tab), ((), "details"))

    def test_delta_rules_for_missing_zero_and_methodology(self):
        current = state()
        base = ts.route(current, "codex/gpt-6.1-sol/xhigh")
        other = ts.route(current, "codex/gpt-6.1-sol/low")
        self.assertRegex(ts.delta(base, other, "intelligence"), r"^-\d+ pts$")
        self.assertRegex(ts.delta(base, other, "usd_per_task"), r"^-\d+\.\d%$")
        missing = ts.route(current, "claude/claude-sonnet-5-5/low")
        self.assertEqual(ts.delta(base, missing, "intelligence"), "unknown")
        zero = json.loads(json.dumps(base))
        zero["metrics"]["usd_per_task"]["value"] = 0
        self.assertEqual(ts.delta(zero, other, "usd_per_task"), "no percentage (zero baseline)")
        moved = json.loads(json.dumps(other))
        moved["metrics"]["intelligence"]["methodology"] = "Artificial Analysis Intelligence Index v5.0"
        self.assertIn("not comparable", ts.delta(base, moved, "intelligence"))
        self.assertEqual(ts.delta(base, base, "intelligence"), "same")

    def test_frontier_marks_shown_comparable_rows_only(self):
        current = replace(state(), frontier=True)
        marks = ts.frontier(current)
        self.assertEqual(set(marks.values()) - {"frontier", "dominated", "unknown"}, set())
        self.assertEqual(marks["claude/claude-sonnet-5-5/low"], "unknown", "missing index is unknown, not efficient")
        frontier = [key for key, mark in marks.items() if mark == "frontier"]
        for key in frontier:
            row = ts.route(current, key)
            index, cost, first = (ts.value(row, name) for name in ts.FRONTIER_DIMENSIONS)
            for other in ts.routes(current):
                if marks.get(other["key"]) == "unknown" or other is row:
                    continue
                values = tuple(ts.value(other, name) for name in ts.FRONTIER_DIMENSIONS)
                self.assertFalse(values[0] >= index and values[1] <= cost and values[2] <= first
                                 and values != (index, cost, first), (key, other["key"]))
        narrowed = replace(current, effort="low")
        self.assertEqual(set(ts.frontier(narrowed)), set(ts.visible_keys(narrowed)), "scope is the shown rows")
        tied = state(snapshot=fixture_snapshot(edit=lambda rows: [
            {**row, "metrics": {**row["metrics"], "intelligence": 50, "usd_per_task": 1.0, "first_response_s": 5.0}}
            for row in rows]))
        self.assertEqual({mark for mark in ts.frontier(replace(tied, frontier=True)).values() if mark != "unknown"},
                         {"frontier"}, "identical rows do not dominate each other")
        self.assertIn("not a recommendation", text(current, 160, 40))
        self.assertNotIn("Recommended", text(current, 160, 40))

    def test_changed_data_and_methodology_change(self):
        previous = {"generation": "1", "created_at": "2026-09-30T10:00:00Z",
                    "methodology": {sources.AA: "Artificial Analysis Intelligence Index v4.3"},
                    "routes": {"codex/gpt-6.1-sol/xhigh": {"intelligence": 50, "usd_per_task": 0.39,
                                                           "output_tps": 63, "first_response_s": 107.76,
                                                           "total_response_s": 115.66, "context_tokens": 1_000_000}}}
        current = replace(state(), previous=previous, focus="codex/gpt-6.1-sol/xhigh")
        changed = ts.changes(current, "codex/gpt-6.1-sol/xhigh")
        self.assertIn(("intelligence", 50, 51), changed)
        rendered = inspector(current, "benchmarks")
        self.assertIn("AA index 50 -> 51 (+1 pts)", rendered)
        self.assertNotIn("not model improvement", rendered)
        moved = replace(state(snapshot=fixture_snapshot("Artificial Analysis Intelligence Index v5.0")),
                        previous=previous, focus="codex/gpt-6.1-sol/xhigh")
        self.assertTrue(ts.methodology_changed(moved))
        rendered = inspector(moved, "benchmarks")
        self.assertIn("differences are not model improvement", rendered)
        self.assertIn("not comparable", rendered)
        self.assertIn("No previous snapshot", inspector(state(), "benchmarks"))


class InspectorTests(unittest.TestCase):
    def test_tabs_render_disclaimer_profiles_sources_and_routing(self):
        current = replace(state(), focus="claude/claude-opus-5-5/xhigh")
        benchmarks = inspector(current, "benchmarks")
        self.assertIn(tr.DISCLAIMER, benchmarks)
        self.assertIn("Claude Opus 5.5 (xhigh with fallback)", benchmarks)
        self.assertIn("never enables Pod fallback", benchmarks)
        self.assertIn("Artificial Analysis Intelligence Index v4.3", benchmarks)
        self.assertIn("date not published by the source", benchmarks)
        self.assertIn("no global rank", benchmarks)
        self.assertIn("anthropic_models", benchmarks, "provider context stays a separate attributed observation")
        routing = inspector(current, "routing")
        self.assertIn("not a prediction", routing)
        self.assertIn("Planning, architecture", routing)
        source_text = inspector(current, "sources")
        self.assertIn("https://artificialanalysis.ai/leaderboards/models", source_text)
        self.assertIn("Native access is unknown", source_text)
        details = inspector(current, "details")
        self.assertIn("claude/claude-opus-5-5/xhigh", details)
        self.assertIn("/fixture/config.yaml", details)
        estimated = state(snapshot=fixture_snapshot(edit=lambda rows: [
            {**row, "qualifiers": row["qualifiers"] + (["estimated index"] if row["name"] == "GPT-6 Luna (high)" else [])}
            for row in rows]))
        self.assertIn("estimated", inspector(replace(estimated, focus="codex/gpt-6-luna/high"), "benchmarks"))
        self.assertIn("*", text(replace(estimated, focus="codex/gpt-6-luna/high", sort="effort"), 100, 45))

    def test_failed_optional_source_shows_attempt_and_earlier_rows(self):
        snapshot = fixture_snapshot()
        snapshot["sources"][sources.OPENAI].update(status="access_denied",
                                                   diagnostics=["openai_models: HTTP 403 bot challenge; keeping rows"])
        rendered = inspector(state(snapshot=snapshot), "sources")
        self.assertIn("Latest attempt access_denied; 3 rows retrieved 2026-10-01T11:00:00Z", rendered)
        self.assertIn("these rows are from that earlier read", rendered)

    def test_tab_and_pane_keys_scroll_the_inspector_independently(self):
        current, _ = press(state(), "TAB")
        self.assertEqual(current.pane, "inspector")
        focus = current.focus
        current, _ = press(current, "DOWN", "DOWN", "RIGHT")
        self.assertEqual((current.focus, current.tab, current.inspector_scroll), (focus, "benchmarks", 0))
        current, _ = press(current, "DOWN")
        self.assertEqual(current.inspector_scroll, 1)
        current, _ = press(current, "ESC", "DOWN")
        self.assertEqual(current.pane, "table")
        self.assertNotEqual(current.focus, focus)


class SetupTests(unittest.TestCase):
    V1 = (b"schema: pod/v1\nselection: custom\nmodels:\n  claude-opus-5-5: preferred\n  gpt-6-sol: available\n"
          b"  gpt-6-luna: disabled\nworkers:\n  max_active: 3\npinned_model: claude-opus-5-5\n")

    def test_setup_collects_pin_and_preferred_then_confirms(self):
        preview = config.setup_preview(self.V1)
        current = ts.with_setup(state(dict(prefs(), status="setup_required")), preview)
        self.assertEqual(current.mode, "setup")
        rendered = text(current, 100, 30)
        self.assertIn("ROUTE SETUP", rendered)
        self.assertIn("Nothing is saved until you confirm", rendered)
        waiting, effects = press(current, "ENTER")
        self.assertEqual(effects, [])
        self.assertIn("earlier pin", waiting.notice)
        chosen, _ = press(current, "RIGHT", "RIGHT", "RIGHT", "DOWN", "RIGHT")
        self.assertEqual(ts.setup_choices(chosen),
                         {"pin": "claude/claude-opus-5-5/high",
                          "preferred": ts.setup_options(chosen)[1][0]})
        confirming, effects = press(chosen, "ENTER")
        self.assertTrue(confirming.setup.confirming)
        self.assertEqual(effects, [])
        back, _ = press(confirming, "n")
        self.assertFalse(back.setup.confirming)
        _, effects = press(confirming, "y")
        self.assertEqual(effects[0].kind, "setup_apply")
        self.assertEqual(effects[0].payload["revision"], preview["revision"])
        cleared, _ = press(current, "LEFT")
        self.assertIsNone(ts.setup_choices(cleared)["pin"])
        later, _ = press(current, "ESC")
        self.assertEqual(later.mode, "browse")


class SetupRebaseTests(unittest.TestCase):
    def chosen(self, raw: bytes) -> ts.State:
        current = ts.with_setup(state(dict(prefs(), status="setup_required")), config.setup_preview(raw))
        current, _ = press(current, "RIGHT", "RIGHT", "RIGHT", "DOWN", "RIGHT")
        return current

    def test_rebase_keeps_choices_that_still_apply(self):
        current = self.chosen(SetupTests.V1)
        before = ts.setup_choices(current)
        changed = SetupTests.V1.replace(b"max_active: 3", b"max_active: 4")
        rebased, note = ts.rebase_setup(current, config.setup_preview(changed))
        self.assertEqual(rebased.setup.preview["revision"], config.setup_preview(changed)["revision"])
        self.assertEqual(ts.setup_choices(rebased), before)
        self.assertIn("your choices still apply", note)
        self.assertEqual(ts.rebase_setup(rebased, config.setup_preview(changed)), (rebased, ""))

    def test_rebase_clears_choices_that_no_longer_apply_and_says_so(self):
        current = self.chosen(SetupTests.V1)
        other = (b"schema: pod/v1\nselection: custom\nmodels:\n  gpt-6-astra: available\n"
                 b"workers:\n  max_active: 2\npinned_model: gpt-6-astra\n")
        rebased, note = ts.rebase_setup(current, config.setup_preview(other))
        self.assertEqual((rebased.setup.pin, rebased.setup.preferred), (-1, 0))
        self.assertIn("cleared the pin choice and the Preferred choice", note)
        self.assertIsNone(ts.setup_choices(rebased), "the new pin still needs an explicit choice")

    def test_rebase_closes_setup_when_the_file_is_no_longer_v1(self):
        closed, note = ts.rebase_setup(self.chosen(SetupTests.V1), None)
        self.assertIsNone(closed.setup)
        self.assertEqual(closed.mode, "browse")
        self.assertIn("no longer pod/v1", note)


class WorkspaceSessionTests(unittest.TestCase):
    """Session I/O with disposable homes, an injected fetch and a controlled clock."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "config" / "config.yaml"
        self.config.parent.mkdir()
        document = config.defaults()
        document["refresh"] = "automatic"
        self.config.write_text(yaml.safe_dump(document, sort_keys=False))
        environment = patch.dict(os.environ, {"POD_CONFIG_HOME": str(self.config.parent),
                                              "POD_CACHE_HOME": str(self.base / "cache"),
                                              "POD_STATE_HOME": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = T0
        self.calls = []

    def clock(self):
        return self.now

    def fetch(self, url, *, deadline, cancel):
        self.calls.append(url)
        source = next(source for source in sources.SOURCES if source.url == url)
        return sources.Page(url, page(source.id))

    def refresher(self, **kwargs):
        return observations.refresh(now=self.now, fetch=self.fetch, **kwargs)

    def open(self, **options) -> tui.Workspace:
        workspace = tui.Workspace(self.base, clock=self.clock, refresher=self.refresher, **options)
        self.addCleanup(workspace.close)
        return workspace

    def settle(self, workspace: tui.Workspace) -> None:
        if workspace.thread is not None:
            workspace.thread.join(5)
        workspace.poll()

    def set_refresh(self, setting: str) -> None:
        document = yaml.safe_load(self.config.read_text())
        document["refresh"] = setting
        self.config.write_text(yaml.safe_dump(document, sort_keys=False))

    def test_automatic_refresh_runs_once_when_due_and_writes_cache_only(self):
        before = self.config.read_bytes()
        workspace = self.open()
        self.assertEqual(workspace.state.refresh_state, "running")
        self.settle(workspace)
        self.assertEqual(workspace.state.refresh_state, "updated")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(workspace.state.projection["observations"]["origin"], "cache")
        self.assertEqual(self.config.read_bytes(), before, "refresh never writes preferences")
        self.calls.clear()
        self.now = T0 + timedelta(hours=23)
        self.settle(self.open())
        self.assertEqual(self.calls, [], "fresh data is not refreshed again")
        self.now = T0 + timedelta(hours=24)
        self.settle(self.open())
        self.assertEqual(len(self.calls), 3, "24-hour-old data refreshes on opening")

    def test_manual_only_never_touches_the_network(self):
        self.set_refresh("manual")
        workspace = self.open()
        self.settle(workspace)
        self.assertEqual((self.calls, workspace.state.refresh_state), ([], "idle"))
        workspace.handle("R")
        self.settle(workspace)
        self.assertEqual(len(self.calls), 3, "the Refresh action uses the same refresh")
        self.assertEqual(workspace.state.refresh_state, "updated")

    def test_seven_day_data_is_stale_and_reopen_restraint_holds_after_a_failure(self):
        self.settle(self.open())
        self.now = T0 + timedelta(days=8)
        failing = self.fetch

        def denied(url, *, deadline, cancel):
            self.calls.append(url)
            raise sources.SourceError("access_denied", "HTTP 403 bot challenge")

        self.fetch = denied
        workspace = self.open()
        self.assertTrue(workspace.state.projection["observations"]["stale"])
        self.settle(workspace)
        self.assertEqual(workspace.state.refresh_state, "failed")
        self.assertIn("bot challenge", workspace.state.refresh_detail)
        self.assertIn("STALE", text(workspace.state, 120, 30))
        self.assertIn("Refresh failed", text(workspace.state, 120, 30))
        self.calls.clear()
        self.now += timedelta(minutes=5)
        self.settle(self.open())
        self.assertEqual(self.calls, [], "a recent failed attempt restrains reopening")
        self.fetch = failing
        self.now += timedelta(hours=6)
        self.settle(self.open())
        self.assertEqual(len(self.calls), 3)

    def test_cancel_on_quit_promotes_nothing_and_keys_stay_responsive(self):
        release = threading.Event()

        def slow(url, *, deadline, cancel):
            self.calls.append(url)
            release.wait(5)
            return sources.Page(url, page(sources.AA))

        self.fetch = slow
        workspace = self.open()
        self.assertEqual(workspace.state.refresh_state, "running")
        focus = workspace.state.focus
        self.assertFalse(workspace.handle("DOWN"))
        self.assertNotEqual(workspace.state.focus, focus, "keys work while refresh runs")
        workspace.cancel.set()
        release.set()
        workspace.thread.join(5)
        workspace.poll()
        self.assertEqual(workspace.state.refresh_state, "cancelled")
        self.assertFalse((self.base / "cache" / "models" / "current.json").exists())

    def test_refresh_keeps_focus_view_and_compare_marks(self):
        self.set_refresh("manual")
        workspace = self.open()
        for key in ("s", "DOWN", "DOWN", "c", "g"):
            workspace.handle(key)
        before = workspace.state
        workspace.handle("R")
        self.settle(workspace)
        after = workspace.state
        self.assertEqual((after.focus, after.sort, after.grouped, after.compare),
                         (before.focus, before.sort, before.grouped, before.compare))

    def test_saves_report_success_conflict_and_failure_honestly(self):
        self.set_refresh("manual")
        workspace = self.open()
        key = workspace.state.focus
        workspace.handle(" ")
        self.assertEqual(config.load(personal=self.config)["routes"][key], "disabled")
        self.assertIn("Saved: Disabled " + key, workspace.state.notice)
        self.assertIsNotNone(workspace.state.saved_at)
        outside = config.load(personal=self.config)
        config.set_route(self.config, key, "enabled", displayed=outside)
        workspace.handle(" ")
        self.assertIn("changed elsewhere", workspace.state.error)
        self.assertEqual(config.load(personal=self.config)["routes"][key], "enabled")
        self.assertEqual(ts.route(workspace.state, key)["state"], "enabled", "the window shows the file again")
        before = self.config.read_bytes()
        self.config.parent.chmod(0o500)
        try:
            workspace.handle("p")
        finally:
            self.config.parent.chmod(0o700)
        self.assertTrue(workspace.state.error.startswith("Not saved"))
        self.assertEqual(self.config.read_bytes(), before)
        workspace.handle("DOWN")
        self.assertTrue(workspace.state.error, "a failure stays reachable after moving")
        workspace.handle("p")
        self.assertEqual(workspace.state.error, "")

    def test_external_edit_and_invalid_file_are_noticed(self):
        self.set_refresh("manual")
        workspace = self.open()
        key = workspace.state.focus
        config.set_route(self.config, key, "disabled", displayed=config.load(personal=self.config))
        self.assertTrue(workspace.poll())
        self.assertEqual(ts.route(workspace.state, key)["state"], "disabled")
        self.assertIn("changed outside", workspace.state.notice)
        self.config.write_text("schema: [\n")
        workspace.poll()
        self.assertTrue(workspace.poll())
        self.assertEqual(ts.preferences(workspace.state)["status"], "invalid")
        workspace.handle(" ")
        self.assertIn("read-only", workspace.state.notice)

    def test_setup_conflict_rebuilds_the_preview_so_a_second_confirm_saves(self):
        self.config.write_bytes(SetupTests.V1)
        workspace = self.open()
        for key in ("RIGHT", "ENTER"):
            workspace.handle(key)
        changed = SetupTests.V1.replace(b"max_active: 3", b"max_active: 4")
        self.config.write_bytes(changed)
        workspace.handle("y")
        self.assertIn("changed elsewhere", workspace.state.error)
        self.assertIn("your choices still apply", workspace.state.error)
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertEqual(workspace.state.setup.preview["revision"], config.setup_preview(changed)["revision"])
        workspace.handle("ENTER")
        workspace.handle("y")
        saved = config.load(personal=self.config)
        self.assertEqual((saved["status"], saved["max_active"], saved["pinned"]),
                         ("valid", 4, "claude/claude-opus-5-5/low"))

    def test_setup_follows_external_changes_while_open(self):
        self.config.write_bytes(SetupTests.V1)
        workspace = self.open()
        workspace.handle("RIGHT")
        other = (b"schema: pod/v1\nselection: custom\nmodels:\n  gpt-6-astra: available\n"
                 b"workers:\n  max_active: 2\npinned_model: gpt-6-astra\n")
        self.config.write_bytes(other)
        self.assertTrue(workspace.poll())
        self.assertIn("cleared the pin choice", workspace.state.notice)
        self.assertEqual(workspace.state.setup.preview["revision"], config.setup_preview(other)["revision"])
        document = config.defaults()
        self.config.write_text(yaml.safe_dump(document, sort_keys=False))
        self.assertTrue(workspace.poll())
        self.assertIsNone(workspace.state.setup)
        self.assertEqual(workspace.state.mode, "browse")
        self.assertIn("no longer pod/v1", workspace.state.notice)

    def test_refresh_outcomes_are_named_as_they_are(self):
        self.set_refresh("manual")
        workspace = self.open()
        diff = {sources.AA: {"counts": {"added": 0, "removed": 0, "changed": 2}}}
        cases = [
            (("done", {"outcome": "promoted", "diff": diff}), "updated", "Data updated"),
            (("done", {"outcome": "promoted", "diff": {sources.AA: {"counts": {"added": 0, "removed": 0,
                                                                              "changed": 0}}}}),
             "unchanged", "Data refreshed; no changes"),
            (("done", {"outcome": "refused", "diagnostics": ["artificial_analysis: Coverage collapsed from 53 rows"]}),
             "refused", "Refresh refused: artificial_analysis: Coverage collapsed from 53 rows"),
            (("done", {"outcome": "superseded"}), "superseded", "A newer refresh won; data reloaded"),
            (("done", {"outcome": "cancelled"}), "cancelled", "Refresh cancelled"),
            (("done", {"outcome": "checked"}), "checked", "Refresh checked; nothing saved"),
            (("done", {"outcome": "failed", "diagnostics": ["artificial_analysis: HTTP 403 bot challenge"],
                       "sources": {sources.AA: {"status": "access_denied"},
                                   sources.ANTHROPIC: {"status": "not_attempted"}}}),
             "failed", "Refresh failed: access_denied: artificial_analysis: HTTP 403 bot challenge"),
            (("error", "observations_busy: Another Pod process is updating observations"), "failed",
             "Refresh failed: observations_busy: Another Pod process is updating observations"),
        ]
        for (kind, result), name, label in cases:
            with self.subTest(name=name, label=label):
                workspace._refresh_done(kind, result)
                self.assertEqual(workspace.state.refresh_state, name)
                self.assertEqual(tr.refresh_label(workspace.state)[0], label)
                rendered = text(workspace.state, 200, 30)
                self.assertIn(label, rendered)
                if name in ("refused", "superseded", "unchanged", "checked", "cancelled", "updated"):
                    self.assertNotIn("Refresh failed", rendered)

    def test_coverage_collapse_is_reported_as_refused_not_failed(self):
        self.set_refresh("manual")
        tiny = ("<table><tr><th>Model</th><th>Context Window</th><th>Creator</th>"
                "<th>Artificial Analysis Intelligence Index</th><th>Cost per TaskUSD</th><th>MedianTokens/s</th>"
                "<th>LatencyFirst Chunk (s)</th><th>TotalResponse (s)</th></tr><tr><td>GPT-6.1 Sol (xhigh)</td>"
                "<td>1M</td><td>OpenAI</td><td>51</td><td>$0.39</td><td>63</td><td>107.76</td><td>115.66</td></tr>"
                "</table>")
        self.fetch = lambda url, *, deadline, cancel: sources.Page(url, tiny if url == sources.SOURCES[0].url
                                                                   else page(sources.ANTHROPIC))
        workspace = self.open()
        workspace.handle("R")
        self.settle(workspace)
        self.assertEqual(workspace.state.refresh_state, "refused")
        self.assertIn("Coverage collapsed", workspace.state.refresh_detail)
        self.assertNotIn("Refresh failed", text(workspace.state, 200, 30))
        self.assertFalse((self.base / "cache" / "models" / "current.json").exists())

    def test_missing_file_gets_a_read_only_notice_after_one_held_tick(self):
        self.set_refresh("manual")
        workspace = self.open()
        self.config.unlink()
        self.assertFalse(workspace.poll(), "one read of a missing file may be mid-write")
        self.assertEqual(workspace.state.notice, "")
        self.assertTrue(workspace.poll())
        self.assertEqual(workspace.state.notice, "Preferences missing - read-only")
        self.assertEqual(ts.preferences(workspace.state)["status"], "missing")
        workspace.handle(" ")
        self.assertIn("read-only", workspace.state.notice)

    def test_setup_apply_saves_only_after_confirmation(self):
        self.config.write_bytes(SetupTests.V1)
        workspace = self.open()
        self.assertEqual(workspace.state.mode, "setup")
        for key in ("RIGHT", "ENTER"):
            workspace.handle(key)
        self.assertEqual(self.config.read_bytes(), SetupTests.V1, "nothing saved before confirmation")
        workspace.handle("y")
        saved = config.load(personal=self.config)
        self.assertEqual(saved["status"], "valid")
        self.assertEqual(saved["pinned"], "claude/claude-opus-5-5/low")
        self.assertEqual((self.config.parent / "config.yaml.pod-v1").read_bytes(), SetupTests.V1)
        self.assertEqual(workspace.state.mode, "browse")
        self.assertIn("route setup", workspace.state.notice)


if __name__ == "__main__":
    unittest.main()

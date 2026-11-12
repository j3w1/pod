from pathlib import Path

from pod import routefile
from pod.models import leaderboard, registry, view
from tests.models.support import ModelCase, ROUTES, live_page

EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]
CODEX = {"gpt-6.1-sol": EFFORTS, "gpt-6.1-terra": EFFORTS, "gpt-6-astra": EFFORTS, "gpt-6-luna": EFFORTS[:5],
         "gpt-5.6-sol": EFFORTS}


def shot(rows):
    return {"rows": rows, "methodology": "m", "fetched_at": "2026-11-12T00:00:00Z"}


def public(name, index, cost, creator="OpenAI", **extra):
    return {"name": name, "creator": creator, "index_estimated": False, "intelligence_index": index,
            "cost_per_task_usd": cost, "context_window_tokens": None, "time_per_task_min": None,
            "first_answer_token_s": None, **extra}


class ViewCase(ModelCase):
    def build(self, rows=None, routes=None, catalogs=None):
        if routes is not None:
            self.write_routes(routes)
        return view.build(shot=shot(rows) if rows is not None else None, attempt=None,
                          catalogs=catalogs or {"codex": None, "claude": None})

    def by_name(self, model, name):
        return next(row for row in model.rows if row.name == name)


class StateTests(ViewCase):
    def test_each_state_comes_from_its_one_source(self):
        self.codex_catalog(CODEX)
        from pod.models import adapters
        rows = [public("GPT-6.1 Sol (high)", 50, 0.59), public("Gemini 4 Carbon (high)", 60, 7.76, "Google")]
        model = self.build(rows, ROUTES, adapters.catalogs())
        enabled = self.by_name(model, "GPT-6.1 Sol (high)")
        self.assertEqual((enabled.state, enabled.observed, enabled.adapter, enabled.routable, enabled.preference),
                         ("enabled", True, "listed", True, "enabled"))
        disabled = self.by_name(model, "GPT-6.1 Sol (medium)")
        self.assertEqual((disabled.state, disabled.observed), ("disabled", False))
        unset = self.by_name(model, "GPT-6.1 Sol (ultra)")
        self.assertEqual((unset.state, unset.preference, unset.adapter), ("routable", None, "listed"))
        claude = self.by_name(model, "Claude Opus 5.5 (high)")
        self.assertEqual((claude.state, claude.adapter), ("enabled", "no catalog"))
        other = self.by_name(model, "Gemini 4 Carbon (high)")
        self.assertEqual((other.state, other.route, other.reason), ("no route", None, "not in the shipped registry"))
        adapter_only = self.by_name(model, "GPT-5.6-SOL")
        self.assertEqual((adapter_only.state, adapter_only.adapter, adapter_only.routable),
                         ("no route", "listed", False))
        self.assertIn("not in the shipped registry", adapter_only.reason)

    def test_the_context_column_is_the_public_value_and_the_adapters_is_labelled_in_the_detail(self):
        self.codex_catalog(CODEX)       # the stand-in catalog states 500000 tokens for every model
        from pod.models import adapters, table
        rows = [public("GPT-6.1 Sol (high)", 50, 0.59),
                public("GPT-6.1 Sol (max)", 55, 1.2, context_window_tokens=1_000_000)]
        model = self.build(rows, ROUTES, adapters.catalogs())
        unstated, stated, unobserved = (self.by_name(model, f"GPT-6.1 Sol ({effort})")
                                        for effort in ("high", "max", "low"))
        self.assertEqual([row.adapter_context for row in (unstated, stated, unobserved)], [500000] * 3)
        self.assertEqual([table.cells(row)["context"] for row in (unstated, stated, unobserved)],
                         ["n/a", "1M", "n/a"])
        self.assertNotIn("500k", table.plain(model, model.rows, 200))
        detail = table.detail(unstated, model.snapshot)
        self.assertIn("  Installed adapter: listed; context window stated by the adapter's catalog: 500k", detail)
        self.assertIn("  context_window_tokens: n/a", detail)

    def test_a_public_observation_never_makes_or_enables_a_route(self):
        model = self.build(leaderboard.parse(live_page())["rows"])
        self.assertEqual({row.state for row in model.rows}, {"routable", "no route"})
        self.assertFalse(any(row.routable for row in model.rows if row.provider not in ("Anthropic", "OpenAI")))
        self.assertEqual(sum(row.routable for row in model.rows), len(registry.routes(registry.load())))
        self.assertFalse(routefile.load().path.exists())

    def test_fable_5_2_and_every_adapter_effort_are_routable_and_not_enabled(self):
        self.codex_catalog(CODEX)
        from pod.models import adapters
        catalogs = adapters.catalogs()
        model = self.build(leaderboard.parse(live_page())["rows"], ROUTES, catalogs)
        routes = registry.routes(registry.load())
        for listed in registry.load():
            for effort in (catalogs["codex"].get(listed.id) or {"efforts": []})["efforts"]:
                self.assertIn(listed.route(effort), routes)
        for key in ("claude/claude-fable-5-2/high", "codex/gpt-6.1-sol/ultra", "codex/gpt-6.1-terra/ultra"):
            row = next(row for row in model.rows if row.route == key)
            self.assertEqual((row.routable, row.state, row.preference), (True, "routable", None))
        self.assertEqual(sorted(row.route for row in model.rows if row.state == "enabled"),
                         ["claude/claude-opus-5-5/high", "codex/gpt-6.1-sol/high"])

    def test_a_non_reasoning_row_has_its_reason_its_measurements_and_no_route(self):
        self.codex_catalog(CODEX)
        from pod.models import adapters
        model = self.build(leaderboard.parse(live_page())["rows"], ROUTES, adapters.catalogs())
        row = self.by_name(model, "GPT-6.1 Terra (non-reasoning)")
        self.assertEqual((row.state, row.route, row.routable), ("no route", None, False))
        self.assertIn("installed codex offers no non-reasoning effort for gpt-6.1-terra", row.reason)
        self.assertEqual((row.metrics["intelligence_index"], row.metrics["cost_per_task_usd"]), (31, 0.23))
        self.assertFalse(row.value)

    def test_a_stored_key_the_registry_does_not_list_is_unknown_and_kept(self):
        self.write_routes(ROUTES.replace("routes:\n", "routes:\n  codex/gpt-9/low: disabled\n"))
        model = view.build(shot=None, attempt=None, catalogs={"codex": None, "claude": None})
        row = next(row for row in model.rows if row.route == "codex/gpt-9/low")
        self.assertEqual((row.state, row.routable, row.preference, row.provider),
                         ("unknown", False, "disabled", "OpenAI"))
        self.assertIn("not in the shipped registry", row.reason)

    def test_a_fallback_row_is_used_only_without_an_exact_row_and_duplicates_are_unavailable(self):
        rows = [public("GPT-6.1 Sol (high with fallback)", 49, 0.5), public("GPT-6.1 Sol (high)", 50, 0.59),
                public("GPT-6.1 Sol (low with fallback)", 30, 0.1),
                public("GPT-6.1 Sol (max)", 55, 1.2), public("GPT-6.1 Sol (max)", 56, 1.3)]
        model = self.build(rows)
        high = self.by_name(model, "GPT-6.1 Sol (high)")
        self.assertEqual((high.metrics["intelligence_index"], high.fallback), (50, False))
        low = self.by_name(model, "GPT-6.1 Sol (low)")
        self.assertEqual((low.metrics["intelligence_index"], low.fallback), (30, True))
        duplicate = self.by_name(model, "GPT-6.1 Sol (max)")
        self.assertEqual((duplicate.observed, duplicate.metrics["intelligence_index"]), (False, None))
        self.assertIn("more than once", duplicate.notes[0])
        extra = self.by_name(model, "GPT-6.1 Sol (high with fallback)")
        self.assertEqual(extra.state, "no route")
        self.assertIn("fallback profile of codex/gpt-6.1-sol/high", extra.reason)


class ValueTests(ViewCase):
    def test_the_rule_on_the_public_table(self):
        model = self.build(leaderboard.parse(live_page())["rows"])
        self.assertEqual(sorted(row.name for row in model.rows if row.value),
                         ["Claude Fable 5.2 (high)", "Claude Fable 5.2 (max)", "GPT-6.1 Sol (high)",
                          "GPT-6.1 Sol (max)", "GPT-6.1 Sol (medium)"])
        self.assertTrue(all(row.routable for row in model.rows if row.value))

    def test_tolerance_and_floor_are_the_two_constants(self):
        self.assertEqual((view.VALUE_TOLERANCE_POINTS, view.VALUE_FLOOR_QUANTILE), (2, 0.5))
        rows = [public("GPT-6.1 Sol (max)", 60, 5.0),      # beaten: xhigh is cheaper within 2 points
                public("GPT-6.1 Sol (xhigh)", 58, 2.0),    # marked
                public("GPT-6.1 Sol (high)", 55, 1.0),     # marked: 3 points below xhigh, nothing cheaper near it
                public("GPT-6.1 Sol (medium)", 40, 0.5),   # below the median of 47.5
                public("GPT-6.1 Sol (low)", 30, 0.1)]
        model = self.build(rows + [public("GPT-6.1 Terra (high)", 30, 0.2)])
        self.assertEqual(sorted(row.name for row in model.rows if row.value),
                         ["GPT-6.1 Sol (high)", "GPT-6.1 Sol (xhigh)"])

    def test_estimated_fallback_unroutable_and_unmeasured_rows_are_left_out(self):
        rows = [public("GPT-6.1 Sol (max)", 60, 5.0),
                public("GPT-6.1 Sol (xhigh)", 60, 1.0, index_estimated=True),
                public("GPT-6.1 Sol (high with fallback)", 60, 0.5),
                public("Gemini 4 Carbon (high)", 60, 0.1, "Google"),
                public("GPT-6.1 Terra (non-reasoning)", 60, 0.01),
                public("GPT-6.1 Sol (low)", 59, None)]
        model = self.build(rows)
        self.assertEqual([row.name for row in model.rows if row.value], ["GPT-6.1 Sol (max)"])

    def test_marking_changes_no_state(self):
        target = self.write_routes(ROUTES)
        before = target.read_bytes()
        model = self.build(leaderboard.parse(live_page())["rows"])
        self.assertTrue(any(row.value and row.state != "enabled" for row in model.rows))
        self.assertEqual(target.read_bytes(), before)


class SelectTests(ViewCase):
    def setUp(self):
        super().setUp()
        self.model = self.build(leaderboard.parse(live_page())["rows"], ROUTES)

    def test_default_filter_keeps_routed_providers_and_all_keeps_everything(self):
        routed = view.select(self.model)
        self.assertEqual({row.provider for row in routed}, {"Anthropic", "OpenAI"})
        self.assertEqual(len(view.select(self.model, provider="all")), len(self.model.rows))
        self.assertEqual({row.provider for row in view.select(self.model, provider="Anthropic")}, {"Anthropic"})

    def test_sorting_puts_unavailable_values_last_in_both_directions(self):
        for reverse in (False, True):
            rows = view.select(self.model, sort="cost", reverse=reverse)
            values = [row.metrics["cost_per_task_usd"] for row in rows]
            have = [value for value in values if value is not None]
            self.assertEqual(values[:len(have)], sorted(have, reverse=reverse))
            self.assertTrue(all(value is None for value in values[len(have):]))
        self.assertEqual(view.select(self.model)[0].name, "Claude Fable 5.2 (max)")

    def test_search_matches_names_routes_and_states(self):
        self.assertEqual({row.model_id for row in view.select(self.model, search="terra") if row.routable},
                         {"gpt-6.1-terra"})
        self.assertEqual({row.state for row in view.select(self.model, search="enabled")}, {"enabled"})
        self.assertEqual(view.select(self.model, search="no such thing"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()

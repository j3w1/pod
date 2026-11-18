"""Selection advice for routable models and profiles seen only on the public table."""

from urllib.parse import urlsplit

from pod.models import leaderboard, registry, table, view
from tests.models.support import FIXTURES, ModelCase, current_page


class GuidanceTests(ModelCase):
    def test_model_and_effort_advice_precedes_sources_for_the_visible_families(self):
        shot = leaderboard.parse(current_page())
        model = view.build(shot=shot, catalogs={})
        # Distinguish concrete use cases, including public profiles with no route.
        use_cases = {
            "GPT-6 Astra": "bug", "GPT-6.2 Astra": "bug",
            "GPT-6 Sol": "feature", "GPT-6.1 Sol": "feature", "GPT-6.2 Sol": "feature",
            "GPT-6.1 Terra": "screen", "GPT-6 Luna": "extract", "Claude Opus 5.5": "review",
            "Astra for Law 6.2": "legal", "GPT-5.3 Codex": "patch",
            "GPT-5.5 Instant": "draft", "GPT-5.5 Pro": "analysis", "o3": "math",
            "gpt-oss-20b": "memory", "gpt-oss-120b": "memory",
        }
        seen = set()
        for row in model.rows:
            name = registry.read_name(row.name).base
            if name not in use_cases:
                continue
            with self.subTest(row=row.name):
                advice, *sources = table.guidance(row)
                self.assertIn(use_cases[name], advice.lower())
                self.assertRegex(advice.lower(), r"\b(low|medium|high|effort)\b")
                self.assertTrue(sources)
                hosts = {"platform.claude.com"} if row.provider == "Anthropic" else {
                    "openai.com", "developers.openai.com", "help.openai.com"}
                for url in sources:
                    self.assertEqual(urlsplit(url).scheme, "https")
                    self.assertIn(urlsplit(url).hostname, hosts)
                details = [line.strip() for line in table.detail(row, shot)]
                start = details.index(advice)
                self.assertEqual(details[start + 1:start + 1 + len(sources)], sources)
                seen.add(name)
        self.assertEqual(seen, set(use_cases))

    def test_unfamiliar_public_profiles_offer_an_effort_comparison_without_assuming_support(self):
        for name, provider in [("Future Model", "OpenAI"), ("Claude Opus Future", "Anthropic")]:
            with self.subTest(name=name):
                row = view.Row(name, provider, "no route", observed=True)
                advice, *sources = table.guidance(row)
                self.assertIn("supported effort", advice)
                self.assertTrue(sources)

    def test_detail_shows_full_measurements_and_their_leaderboard_source(self):
        shot = {"source": leaderboard.URL, "units": leaderboard.UNITS}
        row = view.Row("Claude Fable 5.2 (max with fallback)", "Anthropic", "no route",
                       observed=True, fallback=True)
        row.metrics.update(intelligence_index=64.987654, cost_per_task_usd=26.543219,
                           time_per_task_min=40.81234, first_answer_token_s=221.823456)
        lines = [line.strip() for line in table.detail(row, shot)]
        self.assertIn("State: no route", lines)
        self.assertIn("intelligence_index: 64.987654  [index points]", lines)
        self.assertIn("cost_per_task_usd: 26.543219  [US dollars per benchmark task]", lines)
        self.assertIn("time_per_task_min: 40.81234  [minutes per benchmark task]", lines)
        self.assertIn("first_answer_token_s: 221.823456  [seconds]", lines)
        self.assertTrue(any(leaderboard.URL in line for line in lines))
        _, *sources = table.guidance(row)
        self.assertTrue(any(urlsplit(url).hostname == "platform.claude.com" for url in sources))

    def test_another_provider_can_offer_guidance_without_inventing_efforts(self):
        row = view.Row("Gemini 4 Carbon (high)", "Google", "no route", observed=True)
        advice, *sources = table.guidance(row)
        self.assertIn("known answer", advice)
        self.assertNotIn("effort", advice)
        self.assertEqual(sources, [])


class SupplementalDetailsTests(ModelCase):
    @classmethod
    def setUpClass(cls):
        source = (FIXTURES / "leaderboard-profiles.html").read_text(encoding="utf-8")
        cls.shot = {**leaderboard.parse(source), "units": leaderboard.UNITS}

    def test_detail_displays_exact_supplemental_metrics_and_scores_for_the_selected_profile(self):
        model = view.build(shot=self.shot, attempt=None, catalogs={})
        low = next(row for row in model.rows if row.name == "Claude Fable 5.2 (low with fallback)")
        maximum = next(row for row in model.rows if row.name == "Claude Fable 5.2 (max with fallback)")
        lines = [line.strip() for line in table.detail(low, model.snapshot)]
        for expected in (
            "Time to first token: 7.313568137324001  [seconds]",
            "Output speed: 111.74295995068549  [tokens per second]",
            "Omniscience: 62.93333333333333  [net score points]",
            "Input token price: 8  [US dollars per million tokens]",
            "Output token price: 40  [US dollars per million tokens]",
            "Cache hit price: 0.2  [US dollars per million tokens]",
            "Cache write price: 10  [US dollars per million tokens]",
            "Benchmark critpt: 0.294285714285714  [each benchmark's native score]",
            "Benchmark gdpval-aa-normalized: 1545.68  [each benchmark's native score]",
        ):
            self.assertIn(expected, lines)
        self.assertIn("Benchmark critpt: 0.354285714285714  [each benchmark's native score]",
                      [line.strip() for line in table.detail(maximum, model.snapshot)])

    def test_detail_uses_the_observation_index_for_duplicate_names_and_summarizes_missing_values(self):
        entry = next(row for row in self.shot["rows"] if row["name"] == "JT-35B-Flash")
        duplicate = {**entry, "release": "jt-35b-flash-second",
                     "benchmark_scores": {"critpt": 0, "gpqa-diamond": None}}
        shot = {**self.shot, "rows": [*self.shot["rows"], duplicate]}
        model = view.build(shot=shot, attempt=None, catalogs={})
        selected = next(row for row in model.rows if row.release == duplicate["release"])
        lines = [line.strip() for line in table.detail(selected, model.snapshot)]
        self.assertIn("Benchmark critpt: 0  [each benchmark's native score]", lines)
        self.assertFalse(any("gpqa-diamond" in line for line in lines))
        summary = next(line for line in lines if line.startswith("Supplemental measurements unavailable"))
        self.assertIn("Output speed", summary)
        self.assertIn("1 of 2 benchmark scores", summary)

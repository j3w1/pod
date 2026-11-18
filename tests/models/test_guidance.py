"""Selection advice for routable models and profiles seen only on the public table."""

from urllib.parse import urlsplit

from pod.models import leaderboard, registry, table, view
from tests.models.support import ModelCase, current_page


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

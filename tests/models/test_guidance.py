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

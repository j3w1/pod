import json
import unittest

from pod.models import registry


class RegistryTests(unittest.TestCase):
    def test_an_entry_is_only_identity_agent_provider_and_efforts(self):
        document = json.loads(registry.FILE.read_text())
        self.assertEqual(set(document), {"schema", "models"})
        for entry in document["models"]:
            self.assertEqual(set(entry), {"id", "name", "agent", "provider", "efforts"})

    def test_fable_5_2_and_ultra_are_routable(self):
        routes = registry.routes(registry.load())
        for effort in ("low", "medium", "high", "xhigh", "max"):
            self.assertIn(f"claude/claude-fable-5-2/{effort}", routes)
        for model in ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra"):
            self.assertIn(f"codex/{model}/ultra", routes)
        self.assertNotIn("codex/gpt-6-luna/ultra", routes)
        self.assertFalse(any(key.endswith(("/none", "/non-reasoning")) for key in routes))

    def test_one_rule_reads_every_public_row_name(self):
        cases = {
            "Claude Opus 5.5 (high)": registry.RowName("Claude Opus 5.5", effort="high"),
            "Claude Opus 5.5 (high with fallback)": registry.RowName("Claude Opus 5.5", effort="high", fallback=True),
            "GPT-6.1 Terra (non-reasoning)": registry.RowName("GPT-6.1 Terra", non_reasoning=True),
            "GPT-6.1 Sol (ultra)": registry.RowName("GPT-6.1 Sol", effort="ultra"),
            "o3": registry.RowName("o3"),
            "Qwen3.8 Max (0902)": registry.RowName("Qwen3.8 Max", qualifier="0902"),
            "GPT-6 Sol (Daybreak Blue, max)": registry.RowName("GPT-6 Sol", qualifier="Daybreak Blue, max"),
        }
        for name, expected in cases.items():
            self.assertEqual(registry.read_name(name), expected, name)

    def test_names_match_exactly_and_never_by_resemblance(self):
        models = registry.load()
        self.assertEqual(registry.find(models, "gpt-6.1  sol").id, "gpt-6.1-sol")
        self.assertIsNone(registry.find(models, "GPT-6 Sol"))
        self.assertIsNone(registry.find(models, "Claude Fable 5"))

    def test_a_damaged_registry_is_refused(self):
        import tempfile
        from pathlib import Path
        from pod.errors import PodError
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "registry.json"
            for text in ("{}", '{"schema": "pod-registry/v1", "models": [{"id": "x"}]}',
                         json.dumps({"schema": "pod-registry/v1", "models": [
                             {"id": "m-1", "name": "M", "agent": "codex", "provider": "P", "efforts": ["none"]}]})):
                path.write_text(text)
                with self.assertRaises(PodError) as caught:
                    registry.load(path)
                self.assertEqual(caught.exception.code, "registry_invalid")


if __name__ == "__main__":
    unittest.main()

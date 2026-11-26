import json
import unittest

from pod.models import registry


class RegistryTests(unittest.TestCase):
    def test_6_2_models_match_exactly_at_every_verified_effort(self):
        models = registry.load()
        routes = registry.routes(models)
        for name, model_id in (("GPT-6.2 Astra", "gpt-6.2-astra"), ("GPT-6.2 Sol", "gpt-6.2-sol")):
            model = registry.find(models, name)
            self.assertIsNotNone(model)
            self.assertEqual((model.id, model.agent, model.provider), (model_id, "codex", "OpenAI"))
            self.assertEqual(model.efforts, ("low", "medium", "high", "xhigh", "max", "ultra"))
            for effort in model.efforts:
                self.assertIn(f"codex/{model_id}/{effort}", routes)
                self.assertEqual(registry.read_name(f"{name} ({effort})").base, name)
        self.assertIsNone(registry.find(models, "GPT-6.2"))
        self.assertIsNone(registry.find(models, "GPT-6.2 Astra Preview"))

    def test_an_entry_is_only_identity_agent_provider_efforts_and_optional_wire(self):
        document = json.loads(registry.FILE.read_text())
        self.assertEqual(set(document), {"schema", "models", "defaults"})
        for entry in document["models"]:
            self.assertLessEqual(set(entry), {"id", "name", "agent", "provider", "efforts", "wire"})
            self.assertTrue({"id", "name", "agent", "provider", "efforts"} <= set(entry))

    def test_gemini_4_neon_and_defaults_are_shipped_in_registry(self):
        models = registry.load()
        neon = registry.find(models, "Gemini 4 Neon")
        self.assertIsNotNone(neon)
        self.assertEqual((neon.id, neon.agent, neon.provider, neon.wire),
                         ("gemini-4-neon", "antigravity", "Google", "model-suffix"))
        self.assertEqual(neon.efforts, ("low", "medium", "high", "xhigh"))
        defaults = registry.defaults()
        self.assertEqual(defaults["schema"], "pod/v2")
        self.assertEqual(defaults["merge_authorization"], "person")
        self.assertEqual(defaults["review_agents"], ["codex", "claude", "antigravity"])
        self.assertEqual(set(defaults["profiles"]),
                         {"scout-fast", "scout", "build-fast", "build-deep",
                          "ui-light", "ui-design", "review-fast", "review-deep"})
        self.assertTrue(defaults["profiles"]["scout"].startswith("investigate+mcp "))
        self.assertTrue(defaults["profiles"]["scout-fast"].startswith("investigate "))

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

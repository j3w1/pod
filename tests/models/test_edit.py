import os
import stat

from pod import routefile
from pod.errors import PodError
from pod.models import edit
from tests.models.support import ModelCase, ROUTES


class EditTests(ModelCase):
    def changed_lines(self, before: str, after: str) -> list:
        old, new = before.split("\n"), after.split("\n")
        return [line for line in new if line not in old] + [f"-{line}" for line in old if line not in new]

    def test_one_change_rewrites_one_line_and_keeps_every_other_byte(self):
        target = self.write_routes(ROUTES)
        result = edit.apply(routes={"codex/gpt-6.1-sol/medium": "enabled"})
        after = target.read_text()
        self.assertEqual(self.changed_lines(ROUTES, after),
                         ["  codex/gpt-6.1-sol/medium: enabled   # kept comment",
                          "-  codex/gpt-6.1-sol/medium: disabled   # kept comment"])
        self.assertEqual(result.routes["codex/gpt-6.1-sol/medium"], "enabled")
        self.assertIn("decision_policy:\n  preset: hands_off\n  categories:\n    merge_publish: any_agent\n", after)

    def test_a_new_route_is_added_inside_the_routes_block(self):
        target = self.write_routes(ROUTES)
        edit.apply(routes={"claude/claude-fable-5-2/high": "enabled"})
        self.assertEqual(target.read_text(), ROUTES.replace(
            "  claude/claude-opus-5-5/high: enabled\n",
            "  claude/claude-opus-5-5/high: enabled\n  claude/claude-fable-5-2/high: enabled\n"))

    def test_preferred_and_pin_change_only_their_own_lines(self):
        target = self.write_routes(ROUTES)
        edit.apply(preferred="codex/gpt-6.1-sol/high", pinned="claude/claude-opus-5-5/high")
        self.assertEqual(target.read_text(), ROUTES.replace("preferred: null", "preferred: codex/gpt-6.1-sol/high")
                         .replace("pinned: null", "pinned: claude/claude-opus-5-5/high"))
        edit.apply(pinned=None)
        self.assertEqual(routefile.load().pinned, None)
        self.assertEqual(routefile.load().preferred, "codex/gpt-6.1-sol/high")

    def test_windows_line_endings_and_missing_keys_survive(self):
        text = "schema: pod/v2\r\nroutes:\r\n  codex/gpt-6.1-sol/high: disabled\r\n"
        target = self.write_routes("")
        target.write_bytes(text.encode())
        edit.apply(routes={"codex/gpt-6.1-sol/high": "enabled"}, preferred="codex/gpt-6.1-sol/high")
        self.assertEqual(target.read_bytes(), b"schema: pod/v2\r\nroutes:\r\n  codex/gpt-6.1-sol/high: enabled\r\n"
                                              b"preferred: codex/gpt-6.1-sol/high\r\n")

    def test_an_inline_empty_routes_value_becomes_a_block(self):
        target = self.write_routes("schema: pod/v2\nroutes: {}\npreferred: null\n")
        edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(target.read_text(),
                         "schema: pod/v2\nroutes:\n  codex/gpt-6.1-sol/low: enabled\npreferred: null\n")

    def test_the_first_explicit_change_creates_the_file_private_to_the_person(self):
        target = routefile.load().path
        self.assertFalse(target.exists())
        edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        loaded = routefile.load()
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(loaded.enabled(), ["codex/gpt-6.1-sol/low"])
        self.assertEqual((loaded.preferred, loaded.pinned, loaded.merge_authorization), (None, None, "person"))
        self.assertEqual(loaded.review_agents, ["codex", "antigravity", "claude"])
        self.assertIn("scout-fast", loaded.profiles)
        self.assertIn("scout-deep", loaded.profiles)
        self.assertTrue(loaded.profiles["scout"].mcp)
        self.assertTrue(loaded.profiles["scout-deep"].mcp)

    def test_profiles_and_paused_agents_edit_surgically_and_preserve_every_other_byte(self):
        target = self.write_routes(ROUTES)
        result = edit.apply(
            paused_agents=["antigravity"],
            profiles={"scout-fast": "investigate antigravity/gemini-4-neon/low codex/gpt-6.1-terra/low"},
        )
        self.assertEqual(result.paused_agents, ["antigravity"])
        self.assertEqual(result.profiles["scout-fast"].routes,
                         ("antigravity/gemini-4-neon/low", "codex/gpt-6.1-terra/low"))
        after = target.read_text()
        self.assertIn("  codex/gpt-6.1-sol/medium: disabled   # kept comment\n", after)
        self.assertIn("decision_policy:\n  preset: hands_off\n", after)
        self.assertIn("paused_agents: [antigravity]\n", after)
        updated = edit.apply(
            paused_agents=[],
            profiles={"scout-fast": "investigate+mcp antigravity/gemini-4-neon/low"},
        )
        self.assertEqual(updated.paused_agents, [])
        self.assertTrue(updated.profiles["scout-fast"].mcp)
        removed = edit.apply(profiles={"scout-fast": None})
        self.assertEqual(removed.profiles, {})
        self.assertEqual(routefile.load().status, "ok")
        with self.assertRaises(PodError) as caught:
            edit.apply(profiles={"bad": "unknownrole codex/gpt-6.1-sol/high"})
        self.assertEqual(caught.exception.code, "profile_invalid")
        with self.assertRaises(PodError) as caught:
            edit.apply(paused_agents=["Not Valid"])
        self.assertEqual(caught.exception.code, "agent_invalid")

    def test_paused_and_soft_drained_holds_with_reason_and_ttl_round_trip_cleanly(self):
        target = self.write_routes(ROUTES)
        hold = routefile.AgentHold("antigravity", reason="rate_limit", until="2026-11-26T04:00:00Z", ttl=1800)
        drain = routefile.AgentHold("claude", reason="weekly_10pct", until="2026-11-26T06:00:00Z", ttl=7200)
        res = edit.apply(paused_agents={"antigravity": hold}, soft_drained_agents={"claude": drain})
        self.assertEqual(res.paused_meta["antigravity"], hold)
        self.assertEqual(res.soft_drained_meta["claude"], drain)
        text = target.read_text()
        self.assertIn("  codex/gpt-6.1-sol/medium: disabled   # kept comment\n", text)
        self.assertIn("paused_agents:\n  antigravity: rate_limit ttl=1800 until=2026-11-26T04:00:00Z\n", text)
        self.assertIn("soft_drained_agents:\n  claude: weekly_10pct ttl=7200 until=2026-11-26T06:00:00Z\n", text)
        # Transition mapping back to simple inline list and then empty list
        res2 = edit.apply(paused_agents=["codex"], soft_drained_agents=[])
        self.assertEqual((res2.paused_agents, res2.soft_drained_agents), (["codex"], []))
        self.assertIn("paused_agents: [codex]\n", target.read_text())
        self.assertIn("soft_drained_agents: []\n", target.read_text())

    def test_an_invalid_file_is_refused_and_left_untouched(self):
        target = self.write_routes("schema: pod/v2\nroutes: [a, b]\n")
        before = target.read_bytes()
        with self.assertRaises(PodError) as caught:
            edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(caught.exception.code, "preferences_invalid")
        self.assertIn("pod config edit", caught.exception.fix)
        self.assertEqual(target.read_bytes(), before)

    def test_refusals_change_nothing(self):
        target = self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high"))
        before = target.read_bytes()
        cases = {
            "route_not_routable": dict(routes={"codex/gpt-9/low": "enabled"}),
            "route_in_use": dict(routes={"codex/gpt-6.1-sol/high": "disabled"}),
            "route_not_enabled": dict(preferred="codex/gpt-6.1-sol/medium"),
            "route_invalid": dict(routes={"codex/gpt-6.1-sol/high": "on"}),
            "preferences_changed": dict(routes={"codex/gpt-6.1-sol/medium": "enabled"}, expected=b"earlier"),
        }
        for code, changes in cases.items():
            with self.subTest(code), self.assertRaises(PodError) as caught:
                edit.apply(**changes)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.fix)
            self.assertEqual(target.read_bytes(), before)

    def test_a_stored_key_outside_the_registry_can_be_disabled_but_not_enabled(self):
        self.write_routes(ROUTES.replace("routes:\n", "routes:\n  codex/gpt-9/low: enabled\n"))
        edit.apply(routes={"codex/gpt-9/low": "disabled"})
        self.assertEqual(routefile.load().routes["codex/gpt-9/low"], "disabled")
        with self.assertRaises(PodError):
            edit.apply(routes={"codex/gpt-9/low": "enabled"})

    def test_a_pin_can_be_cleared_and_its_route_disabled_in_one_change(self):
        self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high"))
        result = edit.apply(routes={"codex/gpt-6.1-sol/high": "disabled"}, pinned=None)
        self.assertEqual((result.pinned, result.routes["codex/gpt-6.1-sol/high"]), (None, "disabled"))

    def test_no_change_does_not_touch_the_file(self):
        target = self.write_routes(ROUTES)
        os.utime(target, (1, 1))
        edit.apply(routes={"codex/gpt-6.1-sol/high": "enabled"}, preferred=None)
        self.assertEqual(target.stat().st_mtime, 1)
        self.assertEqual(sorted(os.listdir(target.parent)), [".config.lock", "config.yaml"])

    def test_default_profiles_edit_and_refuse_deleting_in_use_profile(self):
        self.write_routes(ROUTES)
        res = edit.apply(
            profiles={
                "build-fast": "implement codex/gpt-6.1-sol/high",
                "build-deep": "implement claude/claude-opus-5-5/high",
            },
            default_profiles={"implement": "build-fast"},
        )
        self.assertEqual(res.default_profiles, {"implement": "build-fast"})
        with self.assertRaises(PodError) as caught:
            edit.apply(profiles={"build-fast": None})
        self.assertEqual(caught.exception.code, "profile_in_use")
        moved = edit.apply(profiles={"build-fast": None}, default_profiles={"implement": "build-deep"})
        self.assertEqual(moved.default_profiles, {"implement": "build-deep"})
        self.assertNotIn("build-fast", moved.profiles)
        cleared = edit.apply(default_profiles={"implement": None})
        self.assertEqual(cleared.default_profiles, {})

    def test_default_profiles_quotes_yaml_literals_supports_none_null_and_preserves_comments(self):
        target = self.write_routes(ROUTES + "default_profiles: {} # owner comment\n")
        # YAML literal profile names 'true', 'none' and 'null' round-trip cleanly as strings.
        res = edit.apply(
            profiles={
                "true": "implement codex/gpt-6.1-sol/high",
                "null": "review codex/gpt-6.1-sol/high",
                "none": "investigate codex/gpt-6.1-sol/high",
            },
            default_profiles={"implement": "true", "review": "null", "investigate": "none"},
        )
        self.assertEqual(res.default_profiles, {"implement": "true", "review": "null", "investigate": "none"})
        text = target.read_text()
        self.assertIn("default_profiles: # owner comment\n", text)
        self.assertIn("  implement: 'true'\n", text)
        self.assertIn("  review: 'null'\n", text)
        self.assertIn("  investigate: none\n", text)
        # Clearing the last default preserves the header comment when converting back to inline `{}`.
        cleared = edit.apply(default_profiles={"implement": None, "review": None, "investigate": None})
        self.assertEqual(cleared.default_profiles, {})
        self.assertIn("default_profiles: {} # owner comment\n", target.read_text())


if __name__ == "__main__":
    import unittest
    unittest.main()

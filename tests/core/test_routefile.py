import subprocess
import sys

from pod import routefile
from pod.errors import PodError
from tests.support import PodCase, ROOT

FULL = """schema: pod/v2
routes:
  codex/gpt-6.1-sol/high: enabled   # the usual one
  claude/claude-opus-5-5/max: disabled
preferred: codex/gpt-6.1-sol/high
pinned: null
workers:
  max_active: 6
authorization_verifier:
- /usr/local/bin/verifier
- --strict
decision_policy:
  preset: hands_off
  categories:
    merge_publish: any_agent
tool_permissions:
- tool: x
  decision:
    actor: {kind: person}
"""


class RouteFileTests(PodCase):
    def test_it_reads_routes_preferred_pin_and_the_verifier_and_ignores_keys_it_does_not_use(self):
        found = routefile.load(self.write_routes(FULL))
        self.assertEqual((found.status, found.problem), ("ok", None))
        self.assertEqual(found.enabled(), ["codex/gpt-6.1-sol/high"])
        self.assertEqual((found.preferred, found.pinned), ("codex/gpt-6.1-sol/high", None))
        self.assertEqual(found.authorization_verifier, ["/usr/local/bin/verifier", "--strict"])

    def test_only_the_person_setting_lets_the_coordinator_authorize(self):
        self.assertEqual(routefile.load(self.write_routes(FULL)).merge_authorization, "person")
        found = routefile.load(self.write_routes(FULL + "merge_authorization: coordinator\n"))
        self.assertEqual(found.merge_authorization, "coordinator")
        self.assertEqual(routefile.load(self.write_routes(FULL + "merge_authorization: anyone\n")).status, "invalid")

    def test_review_agents_accepts_inline_lists_of_plain_agent_names(self):
        for setting, expected in (
            ("review_agents: [codex]\n", ["codex"]),
            ("review_agents: [codex, local_agent]\n", ["codex", "local_agent"]),
            ("review_agents: [ codex , local_agent ]\r\n", ["codex", "local_agent"]),
            ("review_agents: []\n", []),
            ("review_agents: [ ] # vacío\n", []),
        ):
            with self.subTest(setting=setting):
                found = routefile.load(self.write_routes(FULL + setting))
                self.assertEqual(found.status, "ok", found.problem)
                self.assertEqual(found.review_agents, expected)

    def test_invalid_review_agents_make_the_preferences_invalid(self):
        for value in ("codex", "null", "{}", "[codex/sol/high]", "[codex, false]", "[codex,,claude]", "\n- 42"):
            with self.subTest(value=value):
                found = routefile.load(self.write_routes(FULL + f"review_agents: {value}\n"))
                self.assertEqual((found.status, found.enabled()), ("invalid", []))
                self.assertIn("review_agents", found.problem)

    def test_review_agents_rejects_block_lists_and_non_ascii_whitespace(self):
        for setting in (
            "review_agents:\n  - codex\n  - claude\n",
            "review_agents:\n  - codex\n    - claude\n",
            "review_agents:\n  - codex\n  \u00a0 - claude\n",
            "review_agents:\u00a0[codex]\n",
            "review_agents: [codex,\u00a0claude]\n",
            "review_agents: [codex]\u00a0\n",
            "review_agents:\v[codex]\n",
        ):
            with self.subTest(setting=setting):
                found = routefile.load(self.write_routes(FULL + setting))
                self.assertEqual((found.status, found.enabled()), ("invalid", []))
                self.assertIn("review_agents", found.problem)

    def test_review_agents_rejects_malformed_brackets(self):
        for value in ("[", "]", "[codex,]", "[,]", "[[codex]]"):
            with self.subTest(value=value):
                found = routefile.load(self.write_routes(FULL + f"review_agents: {value}\n"))
                self.assertEqual((found.status, found.enabled()), ("invalid", []))
                self.assertIn("review_agents", found.problem)

    def test_reading_never_writes_the_file(self):
        path = self.write_routes(FULL)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        routefile.load(path).check("codex/gpt-6.1-sol/high")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_a_route_that_is_not_enabled_is_refused_with_the_enabled_ones(self):
        found = routefile.load(self.write_routes(FULL))
        for route, code in (("claude/claude-opus-5-5/max", "route_not_enabled"), ("codex/unlisted/high", "route_not_enabled"),
                            ("nonsense", "route_invalid")):
            with self.assertRaises(PodError) as caught:
                found.check(route)
            self.assertEqual(caught.exception.code, code)
            self.assertIn("codex/gpt-6.1-sol/high", caught.exception.fix)

    def test_while_a_pin_is_set_no_other_route_is_used(self):
        text = FULL.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high").replace("max: disabled", "max: enabled")
        found = routefile.load(self.write_routes(text))
        found.check("codex/gpt-6.1-sol/high")
        with self.assertRaises(PodError) as caught:
            found.check("claude/claude-opus-5-5/max")
        self.assertEqual(caught.exception.code, "route_not_pinned")

    def test_an_absent_or_unreadable_file_makes_no_route_eligible_and_is_left_alone(self):
        absent = routefile.load()
        self.assertEqual((absent.status, absent.enabled()), ("absent", []))
        for text in ("schema: pod/v1\nmodels: {}\n", "schema: pod/v2\nroutes:\n  bad key: enabled\n",
                     "schema: pod/v2\nroutes:\n  a/b/c: maybe\n", "schema: pod/v2\npreferred: a/b/c\n",
                     "schema: pod/v2\nroutes: [a, b]\n", "\tschema: pod/v2\n", "schema: pod/v2\nschema: pod/v2\n"):
            path = self.write_routes(text)
            found = routefile.load(path)
            self.assertEqual((found.status, found.enabled(), found.merge_authorization), ("invalid", [], "person"), text)
            self.assertTrue(found.problem)
            self.assertEqual(path.read_text(), text)
            with self.assertRaises(PodError) as caught:
                found.check("a/b/c")
            self.assertEqual(caught.exception.code, "routes_unavailable")

    def test_coordination_loads_no_model_terminal_or_network_code(self):
        code = ("import sys; import pod.cli, pod.commands, pod.gate, pod.routefile; "
                "bad = [m for m in sys.modules if m.split('.')[0] in ('curses', 'urllib', 'html', 'http', 'yaml') "
                "or m.startswith('pod.models')]; print(bad); raise SystemExit(bool(bad))")
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                              env={"PYTHONPATH": str(ROOT / "skills"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_profiles_and_paused_agents_parse_under_pod_v2(self):
        extra = (
            "paused_agents: [antigravity]\n"
            "profiles:\n"
            "  scout-fast: investigate antigravity/gemini-4-neon/low codex/gpt-6.1-sol/high\n"
            "  scout: investigate+mcp antigravity/gemini-4-neon/medium codex/gpt-6.1-sol/high\n"
        )
        found = routefile.load(self.write_routes(FULL + extra))
        self.assertEqual((found.status, found.problem), ("ok", None))
        self.assertEqual(found.paused_agents, ["antigravity"])
        self.assertEqual(found.profiles["scout-fast"].role, "investigate")
        self.assertFalse(found.profiles["scout-fast"].mcp)
        self.assertTrue(found.profiles["scout"].mcp)
        for bad in (
            "paused_agents:\n  - codex\n",
            "paused_agents: codex\n",
            "profiles:\n  bad: unknownrole codex/gpt-6.1-sol/high\n",
            "profiles:\n  bad: investigate\n",
            "profiles:\n  bad: investigate not-a-route\n",
            "profiles:\n  bad: investigate codex/gpt-6.1-sol/high codex/gpt-6.1-sol/high\n",
        ):
            with self.subTest(bad=bad):
                invalid = routefile.load(self.write_routes(FULL + bad))
                self.assertEqual((invalid.status, invalid.enabled()), ("invalid", []))

    def test_wire_route_and_canonical_effective_handle_antigravity_effort_suffix(self):
        for effort in ("low", "medium", "high", "xhigh"):
            canonical = f"antigravity/gemini-4-neon/{effort}"
            self.assertEqual(routefile.wire_route(canonical), ("antigravity", f"gemini-4-neon-{effort}", None))
            self.assertEqual(routefile.canonical_effective(
                {"agent": "antigravity", "model": f"gemini-4-neon-{effort}", "effort": None}), canonical)
            self.assertEqual(routefile.canonical_effective(
                {"agent": "antigravity", "model": f"gemini-4-neon-{effort}", "effort": effort}), canonical)
        self.assertEqual(routefile.wire_route("codex/gpt-6.2-sol/high"), ("codex", "gpt-6.2-sol", "high"))
        self.assertEqual(routefile.canonical_effective(
            {"agent": "codex", "model": "gpt-6.2-sol", "effort": "high"}), "codex/gpt-6.2-sol/high")

    def test_resolve_dispatch_fallbacks_pin_fail_closed_and_review_author_set_sorting(self):
        cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/low: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/low: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, claude, antigravity]\n"
            "profiles:\n"
            "  scout-fast: investigate antigravity/gemini-4-neon/low codex/gpt-6.1-terra/low\n"
            "  ui-design: implement claude/claude-opus-5-5/high claude/claude-sonnet-5-5/high\n"
            "  review-fast: review codex/gpt-6.1-terra/high codex/gpt-6.2-sol/high claude/claude-sonnet-5-5/high claude/claude-opus-5-5/high antigravity/gemini-4-neon/high\n"
        )
        prefs = routefile.load(self.write_routes(cfg))
        # Ordered fallback when excluded
        res = routefile.resolve_dispatch(prefs, profile="scout-fast", exclude_agents=["antigravity"])
        self.assertTrue(res.ok)
        self.assertEqual(res.route, "codex/gpt-6.1-terra/low")
        self.assertEqual([step.code for step in res.trace], ["excluded_agent", "selected"])
        # Clean pause when zero candidates remain
        paused = routefile.resolve_dispatch(prefs, profile="ui-design", exclude_agents=["claude"])
        self.assertFalse(paused.ok)
        self.assertEqual(paused.error_code, "no_eligible_route")
        self.assertIn("Paused:", paused.error_message)
        self.assertEqual(len(paused.trace), 2)
        # Review ordering + honest factual tags:
        # 1. Neon built diff, all up -> Codex primary (cross_provider)
        r1 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"antigravity"})
        self.assertEqual((r1.route, r1.review_independence), ("codex/gpt-6.1-terra/high", "cross_provider"))
        # 2. Neon built diff, Codex down -> Claude Sonnet (cross_provider_fallback)
        r2 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"antigravity"},
                                        exclude_agents=["codex"])
        self.assertEqual((r2.route, r2.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 3. Claude built diff, Codex down -> AGY Neon beats Claude (cross_provider_fallback)
        r3 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"claude"},
                                        exclude_agents=["codex"])
        self.assertEqual((r3.route, r3.review_independence), ("antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        # 4. Codex built diff, all up -> Claude Sonnet beats Codex (cross_provider_fallback)
        r4 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"codex"})
        self.assertEqual((r4.route, r4.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 5. Mixed authorship {antigravity, codex} -> Claude Sonnet (cross_provider_fallback)
        r5 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"antigravity", "codex"})
        self.assertEqual((r5.route, r5.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 6. Neon built diff, Codex & Claude down -> AGY Neon (same_provider_fallback)
        r6 = routefile.resolve_dispatch(prefs, profile="review-fast", author_agents={"antigravity"},
                                        exclude_agents=["codex", "claude"])
        self.assertEqual((r6.route, r6.review_independence), ("antigravity/gemini-4-neon/high", "same_provider_fallback"))
        # Pin fail-closed when pinned agent is paused or excluded
        pin_prefs = routefile.load(self.write_routes(
            cfg.replace("pinned: null", "pinned: antigravity/gemini-4-neon/low") + "paused_agents: [antigravity]\n"
        ))
        blocked = routefile.resolve_dispatch(pin_prefs, profile="scout-fast")
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "pinned_agent_excluded")

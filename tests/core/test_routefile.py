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
        for model in ("claude-opus-5-5", "claude-sonnet-5-5"):
            for effort in ("low", "medium", "high"):
                canonical = f"antigravity/{model}/{effort}"
                self.assertEqual(routefile.wire_route(canonical), ("antigravity", f"{model}-{effort}", None))
                self.assertEqual(routefile.canonical_effective(
                    {"agent": "antigravity", "model": f"{model}-{effort}", "effort": None}), canonical)
        self.assertEqual(routefile.wire_route("codex/gpt-6.2-sol/high"), ("codex", "gpt-6.2-sol", "high"))
        self.assertEqual(routefile.canonical_effective(
            {"agent": "codex", "model": "gpt-6.2-sol", "effort": "high"}), "codex/gpt-6.2-sol/high")
        # Unlisted efforts for registered (agent, model) pairs are refused cleanly.
        for bad in ("antigravity/claude-opus-5-5/xhigh", "antigravity/claude-sonnet-5-5/max",
                    "antigravity/gemini-4-neon/ultra", "codex/gpt-6-luna/ultra"):
            with self.subTest(bad=bad), self.assertRaises(PodError) as caught:
                routefile.wire_route(bad)
            self.assertEqual(caught.exception.code, "effort_not_supported")

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
        # 1. Neon built diff (Google), all up -> Codex primary (cross_provider)
        r1 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google"})
        self.assertEqual((r1.route, r1.review_independence), ("codex/gpt-6.1-terra/high", "cross_provider"))
        # 2. Neon built diff (Google), Codex down -> Claude Sonnet (cross_provider_fallback)
        r2 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google"},
                                        exclude_agents=["codex"])
        self.assertEqual((r2.route, r2.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 3. Claude built diff (Anthropic), Codex down -> AGY Neon beats Claude (cross_provider_fallback)
        r3 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Anthropic"},
                                        exclude_agents=["codex"])
        self.assertEqual((r3.route, r3.review_independence), ("antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        # 4. Codex built diff (OpenAI), all up -> Claude Sonnet beats Codex (cross_provider_fallback)
        r4 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"OpenAI"})
        self.assertEqual((r4.route, r4.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 5. Mixed authorship {Google, OpenAI} -> Claude Sonnet (cross_provider_fallback)
        r5 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google", "OpenAI"})
        self.assertEqual((r5.route, r5.review_independence), ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        # 6. Neon built diff (Google), Codex & Claude down -> AGY Neon (same_provider_fallback)
        r6 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google"},
                                        exclude_agents=["codex", "claude"])
        self.assertEqual((r6.route, r6.review_independence), ("antigravity/gemini-4-neon/high", "same_provider_fallback"))
        # Pin fail-closed when pinned agent is paused or excluded
        pin_prefs = routefile.load(self.write_routes(
            cfg.replace("pinned: null", "pinned: antigravity/gemini-4-neon/low") + "paused_agents: [antigravity]\n"
        ))
        blocked = routefile.resolve_dispatch(pin_prefs, profile="scout-fast")
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "pinned_agent_excluded")

    def test_two_key_identity_separates_model_family_from_quota_pool(self):
        cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-opus-5-5/high: enabled\n"
            "  antigravity/claude-sonnet-5-5/high: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity, claude]\n"
            "profiles:\n"
            "  build-deep: implement antigravity/gemini-4-neon/high antigravity/claude-opus-5-5/high\n"
            "  review-deep: review codex/gpt-6.2-sol/high antigravity/claude-opus-5-5/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
        )
        # Pausing pool agy-gemini skips gemini-4-neon while leaving antigravity/claude-* 100% routable!
        prefs_paused_gemini = routefile.load(self.write_routes(cfg + "paused_agents: [agy-gemini]\n"))
        built = routefile.resolve_dispatch(prefs_paused_gemini, profile="build-deep")
        self.assertTrue(built.ok)
        self.assertEqual(built.route, "antigravity/claude-opus-5-5/high")
        self.assertEqual([step.code for step in built.trace], ["paused_agent", "selected"])
        # Author antigravity/gemini-4-neon (Google), codex paused, agy-gemini paused:
        # Reviewer antigravity/claude-opus-5-5 (Anthropic) is cross_provider_fallback and still routable!
        prefs_no_codex_no_gemini = routefile.load(self.write_routes(cfg + "paused_agents: [codex, agy-gemini]\n"))
        rev_cross = routefile.resolve_dispatch(prefs_no_codex_no_gemini, profile="review-deep",
                                               author_families={"antigravity/gemini-4-neon/high"})
        self.assertTrue(rev_cross.ok)
        self.assertEqual((rev_cross.route, rev_cross.review_independence),
                         ("antigravity/claude-opus-5-5/high", "cross_provider_fallback"))
        # Author claude/claude-opus-5-5 (Anthropic), codex paused:
        # Sort prefers antigravity/gemini-4-neon (Google, cross_provider_fallback) over antigravity/claude-opus-5-5 (Anthropic)!
        prefs_no_codex = routefile.load(self.write_routes(cfg + "paused_agents: [codex]\n"))
        rev_sorted = routefile.resolve_dispatch(prefs_no_codex, profile="review-deep",
                                                author_families={"claude/claude-opus-5-5/high"})
        self.assertEqual((rev_sorted.route, rev_sorted.review_independence),
                         ("antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        # Author claude/claude-opus-5-5 (Anthropic), reviewer antigravity/claude-opus-5-5 (Anthropic):
        # SAME model family -> same_provider (when primary/no review_agents) or same_provider_fallback (when codex is primary), NEVER cross_provider!
        self.assertEqual(
            routefile.review_tag("antigravity/claude-opus-5-5/high", {"Anthropic"}, None),
            "same_provider",
        )
        self.assertEqual(
            routefile.review_tag("antigravity/claude-opus-5-5/high", {"Anthropic"}, "codex"),
            "same_provider_fallback",
        )
        self.assertEqual(
            routefile.review_tag("antigravity/claude-opus-5-5/high", {"Google"}, "codex"),
            "cross_provider_fallback",
        )
        self.assertEqual(
            routefile.review_tag("codex/gpt-6.2-sol/high", set(), "codex"),
            "unknown_author",
        )
        self.assertEqual(
            routefile.review_tag("antigravity/claude-opus-5-5/high", set(), "codex"),
            "fallback_unknown_author",
        )
        # Pin on antigravity/claude-opus-5-5/high survives paused_agents: [agy-gemini],
        # while Pin on antigravity/gemini-4-neon/high is blocked fail-closed.
        pin_claude = routefile.load(self.write_routes(
            cfg.replace("pinned: null", "pinned: antigravity/claude-opus-5-5/high") + "paused_agents: [agy-gemini]\n"
        ))
        self.assertTrue(routefile.resolve_dispatch(pin_claude, role="implement").ok)
        pin_gemini = routefile.load(self.write_routes(
            cfg.replace("pinned: null", "pinned: antigravity/gemini-4-neon/high") + "paused_agents: [agy-gemini]\n"
        ))
        blocked = routefile.resolve_dispatch(pin_gemini, role="implement")
        self.assertEqual((blocked.ok, blocked.error_code), (False, "pinned_agent_excluded"))
        self.assertIn("agy-gemini", blocked.error_message)

    def test_two_part_effective_routes_and_unregistered_antigravity_models_resolve_safely(self):
        neon_two_part = routefile.canonical_effective(
            {"agent": "antigravity", "model": "gemini-4-neon", "effort": None}
        )
        opus_two_part = routefile.canonical_effective(
            {"agent": "antigravity", "model": "claude-opus-5-5", "effort": None}
        )
        self.assertEqual(neon_two_part, "antigravity/gemini-4-neon")
        self.assertEqual(opus_two_part, "antigravity/claude-opus-5-5")
        self.assertEqual((routefile.route_family(neon_two_part), routefile.route_pool(neon_two_part)),
                         ("Google", "agy-gemini"))
        self.assertEqual((routefile.route_family(opus_two_part), routefile.route_pool(opus_two_part)),
                         ("Anthropic", "agy-claude"))
        self.assertEqual(routefile.review_tag(opus_two_part, {neon_two_part}, "codex"), "cross_provider_fallback")
        self.assertEqual(routefile.review_tag(opus_two_part, {"Anthropic"}, None), "same_provider")
        # Unregistered antigravity models never falsely guess Google when they are Claude or unknown.
        self.assertEqual(routefile.route_family("antigravity/claude-opus-6-0/high"), "Anthropic")
        self.assertEqual(routefile.route_pool("antigravity/claude-opus-6-0/high"), "agy-claude")
        self.assertNotEqual(routefile.route_family("antigravity/other-model/high"), "Google")

    def test_soft_drained_and_paused_holds_support_reason_ttl_expiry_and_skip_trace(self):
        from datetime import datetime, timezone
        cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/low: enabled\n"
            "  codex/gpt-6.1-terra/low: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "paused_agents:\n"
            "  antigravity: rate_limit ttl=1800 until=2026-11-26T04:00:00Z\n"
            "soft_drained_agents:\n"
            "  codex: 5h_window until=2026-11-26T03:30:00Z\n"
            "profiles:\n"
            "  scout: investigate antigravity/gemini-4-neon/low codex/gpt-6.1-terra/low claude/claude-sonnet-5-5/high\n"
        )
        prefs = routefile.load(self.write_routes(cfg))
        self.assertEqual((prefs.status, prefs.paused_agents, prefs.soft_drained_agents),
                         ("ok", ["antigravity"], ["codex"]))
        self.assertEqual(prefs.paused_meta["antigravity"].reason, "rate_limit")
        self.assertEqual(prefs.paused_meta["antigravity"].ttl, 1800)
        self.assertEqual(prefs.soft_drained_meta["codex"].until, "2026-11-26T03:30:00Z")
        # Before either expires: skips paused antigravity and soft-drained codex, picks claude
        t_early = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
        r1 = routefile.resolve_dispatch(prefs, profile="scout", now=t_early,
                                        reviewer_notes=["a2: check edge case"])
        self.assertTrue(r1.ok)
        self.assertEqual(r1.route, "claude/claude-sonnet-5-5/high")
        self.assertEqual([s.code for s in r1.trace], ["paused_agent", "soft_drained_agent", "selected"])
        self.assertIn("rate_limit", r1.trace[0].reason)
        self.assertIn("5h_window", r1.trace[1].reason)
        self.assertEqual(r1.reviewer_notes, ("a2: check edge case",))
        # After codex soft-drain expires (03:45) while antigravity pause still holds (until 04:00): picks codex
        t_mid = datetime(2026, 11, 26, 3, 45, tzinfo=timezone.utc)
        r2 = routefile.resolve_dispatch(prefs, profile="scout", now=t_mid)
        self.assertEqual((r2.ok, r2.route), (True, "codex/gpt-6.1-terra/low"))
        # After both expire (04:05): picks default antigravity
        t_late = datetime(2026, 11, 26, 4, 5, tzinfo=timezone.utc)
        r3 = routefile.resolve_dispatch(prefs, profile="scout", now=t_late)
        self.assertEqual((r3.ok, r3.route), (True, "antigravity/gemini-4-neon/low"))
        # Simple inline list on soft_drained_agents also parses and blocks Pin fail-closed
        inline = routefile.load(self.write_routes(
            "schema: pod/v2\nroutes:\n  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\npinned: claude/claude-sonnet-5-5/high\nsoft_drained_agents: [claude]\n"
        ))
        self.assertEqual((inline.status, inline.soft_drained_agents), ("ok", ["claude"]))
        pin_blocked = routefile.resolve_dispatch(inline, role="implement")
        self.assertEqual((pin_blocked.ok, pin_blocked.error_code), (False, "pinned_agent_excluded"))
        self.assertIn("soft_drained_agents", pin_blocked.error_message)
        # Invalid metadata is refused cleanly
        for bad in (
            "soft_drained_agents:\n  - claude\n",
            "soft_drained_agents:\n  claude: until=not-a-date\n",
            "paused_agents:\n  codex: ttl=0\n",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(routefile.load(self.write_routes(FULL + bad)).status, "invalid")

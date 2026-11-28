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
        # 6. Neon built diff (Google), Codex & Claude down -> refused unless a review exception is supplied
        r6_blocked = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google"},
                                                exclude_agents=["codex", "claude"])
        self.assertEqual((r6_blocked.ok, r6_blocked.error_code), (False, "reviewer_same_family"))
        self.assertTrue(any(step.code == "author_family" for step in r6_blocked.trace))
        r6 = routefile.resolve_dispatch(prefs, profile="review-fast", author_families={"Google"},
                                        exclude_agents=["codex", "claude"], allow_review_exception=True)
        self.assertEqual((r6.ok, r6.route, r6.review_independence),
                         (True, "antigravity/gemini-4-neon/high", "same_provider_fallback"))
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

    def test_default_profiles_resolution_route_outside_profiles_and_routing_policy(self):
        from datetime import datetime, timezone
        cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/low: enabled\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "  codex/gpt-6.2-sol/ultra: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "preferred: claude/claude-opus-5-5/high\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  claude: manual until=2026-11-26T04:00:00Z\n"
            "soft_drained_agents:\n"
            "  agy-claude: keep_bonus_pool\n"
            "profiles:\n"
            "  build-fast: implement antigravity/gemini-4-neon/medium codex/gpt-6.2-sol/medium\n"
            "  build-deep: implement antigravity/gemini-4-neon/high codex/gpt-6.2-sol/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
            "  scout: investigate+mcp antigravity/gemini-4-neon/low codex/gpt-6.2-sol/medium\n"
        )
        prefs = routefile.load(self.write_routes(cfg))
        self.assertEqual((prefs.status, prefs.default_profiles), ("ok", {}))
        self.assertEqual(routefile.effective_default_profiles(prefs),
                         {"implement": "build-fast", "review": "review-fast", "investigate": "scout"})
        # Role-only dispatch uses default profile (never preferred / highest-ranked route).
        imp = routefile.resolve_dispatch(prefs, role="implement")
        self.assertEqual((imp.ok, imp.profile, imp.route), (True, "build-fast", "antigravity/gemini-4-neon/medium"))
        inv = routefile.resolve_dispatch(prefs, role="investigate")
        self.assertEqual((inv.ok, inv.profile, inv.mcp, inv.route), (True, "scout", True, "antigravity/gemini-4-neon/low"))
        # Role-only review filters by review_agents (skips claude) and skips author's model family.
        rev_after_codex = routefile.resolve_dispatch(prefs, role="review", author_families={"OpenAI"})
        self.assertEqual((rev_after_codex.ok, rev_after_codex.profile, rev_after_codex.route, rev_after_codex.review_independence),
                         (True, "review-fast", "antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        self.assertTrue(any(step.code == "author_family" for step in rev_after_codex.trace))
        rev_after_neon = routefile.resolve_dispatch(prefs, role="review", author_families={"Google"})
        self.assertEqual((rev_after_neon.ok, rev_after_neon.profile, rev_after_neon.route, rev_after_neon.review_independence),
                         (True, "review-fast", "codex/gpt-6.1-terra/high", "cross_provider"))
        # Explicit default_profiles overrides the built-in mapping.
        custom = routefile.load(self.write_routes(cfg + "default_profiles:\n  implement: build-deep\n"))
        self.assertEqual(custom.status, "ok", custom.problem)
        imp_custom = routefile.resolve_dispatch(custom, role="implement")
        self.assertEqual((imp_custom.ok, imp_custom.profile, imp_custom.route),
                         (True, "build-deep", "antigravity/gemini-4-neon/high"))
        # --route outside every profile is refused unless --reason is given (including empty/absent profiles and Pin).
        refused = routefile.resolve_dispatch(prefs, role="implement", route="codex/gpt-6.2-sol/ultra")
        self.assertEqual((refused.ok, refused.error_code), (False, "route_outside_profiles"))
        blank = routefile.resolve_dispatch(prefs, role="implement", route="codex/gpt-6.2-sol/ultra", reason="   ")
        self.assertEqual((blank.ok, blank.error_code), (False, "route_outside_profiles"))
        allowed = routefile.resolve_dispatch(prefs, role="implement", route="codex/gpt-6.2-sol/ultra", reason="full trace")
        self.assertEqual((allowed.ok, allowed.route, allowed.reason), (True, "codex/gpt-6.2-sol/ultra", "full trace"))
        # Empty/absent profiles: explicit --route still requires --reason.
        no_profiles = routefile.load(self.write_routes(
            "schema: pod/v2\nroutes:\n  codex/gpt-6.2-sol/high: enabled\npreferred: null\npinned: null\n"
        ))
        self.assertEqual(routefile.resolve_dispatch(no_profiles, role="implement",
                                                    route="codex/gpt-6.2-sol/high").error_code, "route_outside_profiles")
        self.assertTrue(routefile.resolve_dispatch(no_profiles, role="implement",
                                                   route="codex/gpt-6.2-sol/high", reason="one-off").ok)
        # Matching Pin outside configured profiles: explicit --route still requires --reason, while Pin alone does not.
        pinned_outside = routefile.load(self.write_routes(cfg.replace("pinned: null", "pinned: codex/gpt-6.2-sol/ultra")))
        self.assertEqual(routefile.resolve_dispatch(pinned_outside, role="implement",
                                                    route="codex/gpt-6.2-sol/ultra").error_code, "route_outside_profiles")
        pin_with_reason = routefile.resolve_dispatch(pinned_outside, role="implement",
                                                     route="codex/gpt-6.2-sol/ultra", reason="pinned override")
        self.assertEqual((pin_with_reason.ok, pin_with_reason.route, pin_with_reason.reason),
                         (True, "codex/gpt-6.2-sol/ultra", "pinned override"))
        self.assertTrue(routefile.resolve_dispatch(pinned_outside, role="implement").ok)
        # --route listed in any profile needs no --reason.
        in_profile = routefile.resolve_dispatch(prefs, role="implement", route="codex/gpt-6.2-sol/medium")
        self.assertEqual((in_profile.ok, in_profile.route, in_profile.reason), (True, "codex/gpt-6.2-sol/medium", None))
        # Single authoring source: older config without default_profiles picks up a modified disposable registry.json
        # via the real reader without rewriting config.yaml.
        import json as _json
        from unittest.mock import patch
        older_path = self.write_routes(cfg)
        before_bytes, before_mtime = older_path.read_bytes(), older_path.stat().st_mtime_ns
        older_prefs = routefile.load(older_path)
        disposable_reg = self.tmp / "registry.json"
        disposable_reg.write_text(_json.dumps({
            "schema": "pod-registry/v1",
            "models": [],
            "defaults": {
                "profiles": {
                    "build-deep": "implement antigravity/gemini-4-neon/high codex/gpt-6.2-sol/medium",
                    "review-fast": "review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high",
                    "scout": "investigate+mcp antigravity/gemini-4-neon/low codex/gpt-6.2-sol/medium",
                },
                "default_profiles": {
                    "implement": "build-deep",
                    "review": "review-fast",
                    "investigate": "scout",
                },
            },
        }), encoding="utf-8")
        with patch.object(routefile, "REGISTRY_PATH", disposable_reg):
            routefile._read_shipped_default_profiles.cache_clear()
            self.assertEqual(routefile.effective_default_profiles(older_prefs),
                             {"implement": "build-deep", "review": "review-fast", "investigate": "scout"})
            picked = routefile.resolve_dispatch(older_prefs, role="implement")
            self.assertEqual((picked.ok, picked.profile, picked.route),
                             (True, "build-deep", "antigravity/gemini-4-neon/high"))
            # Non-dict JSON root and dangling shipped defaults never crash and are ignored safely.
            bad_root = self.tmp / "bad-root.json"
            bad_root.write_text("[]", encoding="utf-8")
            self.assertEqual(routefile._shipped_default_profiles(bad_root), {})
            dangling = self.tmp / "dangling.json"
            dangling.write_text(_json.dumps({"defaults": {"profiles": {}, "default_profiles": {"implement": "build-deep"}}}),
                                encoding="utf-8")
            self.assertEqual(routefile._shipped_default_profiles(dangling), {})
        routefile._read_shipped_default_profiles.cache_clear()
        self.assertEqual((older_path.read_bytes(), older_path.stat().st_mtime_ns), (before_bytes, before_mtime))
        # Profiles literally named 'none' and 'null' stay valid and usable as in 0.23.1.
        literal_cfg = (
            "schema: pod/v2\nroutes:\n  codex/gpt-6.2-sol/medium: enabled\n"
            "preferred: null\npinned: null\n"
            "profiles:\n"
            "  none: implement codex/gpt-6.2-sol/medium\n"
            "  null: review codex/gpt-6.2-sol/medium\n"
        )
        literal_prefs = routefile.load(self.write_routes(literal_cfg))
        self.assertEqual(literal_prefs.status, "ok", literal_prefs.problem)
        self.assertEqual(routefile.resolve_dispatch(literal_prefs, profile="none").route, "codex/gpt-6.2-sol/medium")
        self.assertEqual(routefile.resolve_dispatch(literal_prefs, profile="null").route, "codex/gpt-6.2-sol/medium")
        # Wire validation runs before family filtering so unusable efforts report effort_not_supported, not reviewer_same_family.
        bad_effort_prefs = routefile.load(self.write_routes(
            "schema: pod/v2\nroutes:\n  antigravity/gemini-4-neon/ultra: enabled\n"
            "preferred: antigravity/gemini-4-neon/ultra\npinned: null\n"
        ))
        wire_first = routefile.resolve_dispatch(bad_effort_prefs, role="review", author_families={"Google"})
        self.assertEqual((wire_first.ok, wire_first.error_code), (False, "effort_not_supported"))
        # Remediation hints include --reason when the suggested explicit --route is outside configured profiles.
        no_prof_review = routefile.load(self.write_routes(
            "schema: pod/v2\nroutes:\n  claude/claude-opus-5-5/high: enabled\n  codex/gpt-6.2-sol/high: enabled\n"
            "preferred: claude/claude-opus-5-5/high\npinned: null\nreview_agents: [codex]\n"
        ))
        disallowed = routefile.resolve_dispatch(no_prof_review, role="review")
        self.assertEqual(disallowed.error_code, "reviewer_agent_not_allowed")
        self.assertIn("--reason", disallowed.error_fix)
        mismatched_pin = routefile.resolve_dispatch(pinned_outside, role="implement", route="codex/gpt-6.2-sol/medium")
        self.assertEqual(mismatched_pin.error_code, "route_not_pinned")
        self.assertIn("--reason", mismatched_pin.error_fix)
        self.assertIn("omit --route", mismatched_pin.error_fix)
        # routing_policy() returns one stable pod-routing/v1 block with active pause expiry.
        early = routefile.routing_policy(prefs, now=datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc))
        late = routefile.routing_policy(prefs, now=datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(early["schema"], "pod-routing/v1")
        self.assertEqual(early["default_profiles"],
                         {"implement": "build-fast", "review": "review-fast", "investigate": "scout"})
        self.assertEqual(early["review_agents"], ["codex", "antigravity"])
        self.assertEqual(early["profiles"]["scout"]["mcp"], True)
        self.assertEqual(early["pauses"]["paused_agents"][0]["active"], True)
        self.assertEqual(late["pauses"]["paused_agents"][0]["active"], False)
        self.assertEqual(early["pauses"]["soft_drained_agents"][0]["agent"], "agy-claude")
        # Invalid default_profiles entries invalidate preferences.
        for bad in (
            "default_profiles:\n  unknown: build-fast\n",
            "default_profiles:\n  implement: missing-profile\n",
            "default_profiles:\n  implement: review-fast\n",
        ):
            with self.subTest(bad=bad):
                invalid = routefile.load(self.write_routes(cfg + bad))
                self.assertEqual(invalid.status, "invalid")

    def test_review_mixed_author_fallback_picks_the_least_authored_family(self):
        cfg = (
            "schema: pod/v2\nroutes:\n  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n  claude/claude-opus-5-5/high: enabled\n"
            "preferred: null\npinned: null\nreview_agents: [codex, antigravity]\n"
            "profiles:\n  review-deep: review codex/gpt-6.2-sol/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
            "  review-codex: review codex/gpt-6.2-sol/high\n")
        prefs = routefile.load(self.write_routes(cfg))
        both = {"OpenAI", "Google"}
        res = routefile.resolve_dispatch(prefs, role="review", profile="review-deep", author_families=both,
                                         author_counts={"OpenAI": 3, "Google": 1})
        self.assertEqual((res.ok, res.route, res.review_independence), (True, "antigravity/gemini-4-neon/high", "mixed_author_fallback"))
        self.assertTrue(res.review_fallback_reason.startswith("mixed authors: "))
        tie = routefile.resolve_dispatch(prefs, role="review", profile="review-deep", author_families=both,
                                         author_counts={"OpenAI": 2, "Google": 2})
        self.assertEqual((tie.route, tie.review_independence), ("codex/gpt-6.2-sol/high", "mixed_author_fallback"))
        # Without counts scoped to the reviewed commit there is no mixed fallback: the refusal stands.
        unscoped = routefile.resolve_dispatch(prefs, role="review", profile="review-deep", author_families=both)
        self.assertEqual((unscoped.ok, unscoped.error_code), (False, "reviewer_same_family"))
        one = routefile.resolve_dispatch(prefs, role="review", profile="review-deep", author_families={"OpenAI"},
                                         author_counts={"OpenAI": 2})
        self.assertEqual((one.ok, one.route, one.review_independence), (True, "antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        # An enabled non-Claude family that did not author the diff blocks the mixed claim.
        narrow = routefile.resolve_dispatch(prefs, role="review", profile="review-codex",
                                            author_families={"OpenAI", "Anthropic"},
                                            author_counts={"OpenAI": 2, "Anthropic": 1})
        self.assertEqual((narrow.ok, narrow.error_code), (False, "reviewer_same_family"))

    def test_review_same_family_quota_fallback_and_boundaries(self):
        from datetime import datetime, timezone
        base = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
        )
        # 1. Both families available -> cross-family, no fallback reason.
        both = routefile.load(self.write_routes(base))
        codex_cross = routefile.resolve_dispatch(both, role="review", author_families={"OpenAI"})
        self.assertEqual((codex_cross.ok, codex_cross.route, codex_cross.review_independence,
                          codex_cross.review_fallback_reason),
                         (True, "antigravity/gemini-4-neon/high", "cross_provider_fallback", None))
        neon_cross = routefile.resolve_dispatch(both, role="review", author_families={"Google"})
        self.assertEqual((neon_cross.ok, neon_cross.route, neon_cross.review_independence,
                          neon_cross.review_fallback_reason),
                         (True, "codex/gpt-6.1-terra/high", "cross_provider", None))

        # 2. Codex author with antigravity paused -> Codex reviewer tagged same_provider_fallback.
        agy_paused = routefile.load(self.write_routes(
            base + "paused_agents:\n  antigravity: quota_exhausted until=2026-11-27T08:00:00Z\n"
        ))
        t_during = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc)
        t_after = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)
        fb_codex = routefile.resolve_dispatch(agy_paused, role="review", author_families={"OpenAI"}, now=t_during)
        self.assertEqual((fb_codex.ok, fb_codex.route, fb_codex.review_independence),
                         (True, "codex/gpt-6.1-terra/high", "same_provider_fallback"))
        self.assertIsNotNone(fb_codex.review_fallback_reason)
        self.assertIn("antigravity", fb_codex.review_fallback_reason)
        self.assertIn("paused", fb_codex.review_fallback_reason)
        # Decision trace does not show the chosen fallback route as both skipped and selected.
        self.assertFalse(any(step.route == fb_codex.route and step.status == "skipped" for step in fb_codex.trace))
        self.assertTrue(any(step.route == fb_codex.route and step.status == "selected" for step in fb_codex.trace))
        # Once the pause expires, new reviews return to cross-family.
        returned = routefile.resolve_dispatch(agy_paused, role="review", author_families={"OpenAI"}, now=t_after)
        self.assertEqual((returned.ok, returned.route, returned.review_independence, returned.review_fallback_reason),
                         (True, "antigravity/gemini-4-neon/high", "cross_provider_fallback", None))

        # 3. Pool-level soft-drain also qualifies for quota fallback.
        pool_drained = routefile.load(self.write_routes(
            base + "soft_drained_agents:\n  agy-gemini: quota_low\n"
        ))
        fb_pool = routefile.resolve_dispatch(pool_drained, role="review", author_families={"OpenAI"})
        self.assertEqual((fb_pool.ok, fb_pool.route, fb_pool.review_independence),
                         (True, "codex/gpt-6.1-terra/high", "same_provider_fallback"))
        self.assertIn("agy-gemini", fb_pool.review_fallback_reason)
        self.assertIn("soft-drained", fb_pool.review_fallback_reason)

        # 4. Neon (antigravity) author with codex paused -> antigravity reviewer tagged same_provider_fallback.
        codex_paused = routefile.load(self.write_routes(base + "paused_agents: [codex]\n"))
        fb_neon = routefile.resolve_dispatch(codex_paused, role="review", author_families={"Google"})
        self.assertEqual((fb_neon.ok, fb_neon.route, fb_neon.review_independence),
                         (True, "antigravity/gemini-4-neon/high", "same_provider_fallback"))
        self.assertIn("codex", fb_neon.review_fallback_reason)

        # 5. Author-family route paused too -> refusal; Claude is never chosen even when only Claude remains.
        both_paused = routefile.load(self.write_routes(base + "paused_agents: [codex, antigravity]\n"))
        for authors in ({"OpenAI"}, {"Google"}):
            with self.subTest(authors=authors):
                refused = routefile.resolve_dispatch(both_paused, role="review", author_families=authors)
                self.assertEqual((refused.ok, refused.error_code, refused.route),
                                 (False, "no_eligible_route", None))

        # 6. Preserved 0.23.2 review-fast config with Anthropic author: fallback NEVER selects Claude (AGY or native).
        preserved_0232 = (
            "schema: pod/v2\n"
            "routes:\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "  antigravity/claude-sonnet-5-5/high: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity, claude]\n"
            "paused_agents: [codex, agy-gemini]\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high codex/gpt-6.2-sol/high antigravity/claude-sonnet-5-5/high antigravity/gemini-4-neon/high claude/claude-sonnet-5-5/high\n"
        )
        no_agy_claude = routefile.resolve_dispatch(
            routefile.load(self.write_routes(preserved_0232)), role="review", author_families={"Anthropic"}
        )
        self.assertEqual((no_agy_claude.ok, no_agy_claude.error_code, no_agy_claude.route),
                         (False, "reviewer_same_family", None))
        native_only_claude = routefile.resolve_dispatch(
            routefile.load(self.write_routes(
                preserved_0232.replace("antigravity/claude-sonnet-5-5/high: enabled",
                                       "antigravity/claude-sonnet-5-5/high: disabled")
            )),
            role="review", author_families={"Anthropic"},
        )
        self.assertEqual((native_only_claude.ok, native_only_claude.error_code, native_only_claude.route),
                         (False, "reviewer_same_family", None))

        # 7. Non-quota hold reasons (maintenance, migration, negation, free text) do NOT authorize fallback; unreasoned/quota holds do.
        for non_quota in (
            "paused_agents:\n  antigravity: scheduled maintenance\n",
            "soft_drained_agents:\n  agy-gemini: scheduled maintenance\n",
            "paused_agents:\n  antigravity: scheduled pool maintenance\n",
            "soft_drained_agents:\n  agy-gemini: manual drain for migration\n",
            "paused_agents:\n  antigravity: no quota issue, taking offline\n",
            "paused_agents:\n  antigravity: random free text\n",
            "paused_agents:\n  antigravity: the quota team asked for this\n",
            "soft_drained_agents:\n  agy-gemini: quota exhausted during testing\n",
            "soft_drained_agents:\n  agy-gemini: Rate limit lifted yesterday; paused for billing\n",
            "soft_drained_agents:\n  agy-gemini: quota exhausted; scheduled_maintenance\n",
            "paused_agents:\n  antigravity: quota\nsoft_drained_agents:\n  agy-gemini: scheduled maintenance\n",
            "paused_agents:\n  antigravity: quota\n  agy-gemini: scheduled maintenance\n",
        ):
            with self.subTest(non_quota=non_quota):
                blocked_maint = routefile.resolve_dispatch(
                    routefile.load(self.write_routes(base + non_quota)), role="review", author_families={"OpenAI"}
                )
                self.assertEqual((blocked_maint.ok, blocked_maint.error_code), (False, "reviewer_same_family"))
        for quota_hold in (
            "paused_agents: [antigravity]\n",
            "paused_agents:\n  antigravity: out of quota\n",
            "paused_agents:\n  antigravity: rate_limit\n",
            "soft_drained_agents:\n  agy-gemini: weekly_10pct\n",
            "soft_drained_agents:\n  agy-gemini: \"Gemini Neon individual quota exhausted (~4 days); reviews use same_provider_fallback\"\n",
            "paused_agents:\n  antigravity: Rate-Limited by the provider until Thursday\n",
            "soft_drained_agents:\n  agy-gemini: Paused at weekly 10% to preserve quota\n",
        ):
            with self.subTest(quota_hold=quota_hold):
                ok_quota = routefile.resolve_dispatch(
                    routefile.load(self.write_routes(base + quota_hold)), role="review", author_families={"OpenAI"}
                )
                self.assertEqual((ok_quota.ok, ok_quota.route, ok_quota.review_independence),
                                 (True, "codex/gpt-6.1-terra/high", "same_provider_fallback"))

        # 8. Non-quota blockers are never masked by a quota hold (held+excluded, held+wire error, or usable independent).
        held_and_excluded = routefile.resolve_dispatch(
            agy_paused, role="review", author_families={"OpenAI"}, exclude_agents=["antigravity"], now=t_during
        )
        self.assertEqual((held_and_excluded.ok, held_and_excluded.error_code), (False, "reviewer_same_family"))

        held_plus_wire_cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/gemini-4-neon/ultra: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  agy-gemini: quota\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high antigravity/gemini-4-neon/ultra\n"
        )
        held_plus_wire = routefile.resolve_dispatch(
            routefile.load(self.write_routes(held_plus_wire_cfg)), role="review", author_families={"OpenAI"}
        )
        self.assertEqual((held_plus_wire.ok, held_plus_wire.error_code), (False, "reviewer_same_family"))
        # Same when the wire-error independent route is on a different pool (not paused).
        split_wire_cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-opus-5-5/xhigh: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  agy-gemini: quota\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high antigravity/claude-opus-5-5/xhigh\n"
        )
        split_wire = routefile.resolve_dispatch(
            routefile.load(self.write_routes(split_wire_cfg)), role="review", author_families={"OpenAI"}
        )
        self.assertEqual((split_wire.ok, split_wire.error_code), (False, "reviewer_same_family"))
        # When one independent route stays usable alongside a held one, stay cross-family.
        usable_other_cfg = (
            "schema: pod/v2\n"
            "routes:\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\n"
            "pinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  agy-gemini: quota\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high antigravity/claude-sonnet-5-5/high\n"
        )
        usable_other = routefile.resolve_dispatch(
            routefile.load(self.write_routes(usable_other_cfg)), role="review", author_families={"OpenAI"}
        )
        self.assertEqual((usable_other.ok, usable_other.route, usable_other.review_independence,
                          usable_other.review_fallback_reason),
                         (True, "antigravity/claude-sonnet-5-5/high", "cross_provider_fallback", None))

        # 9. Non-quota causes (disabled, --exclude-agent, pinned) do NOT trigger quota fallback.
        agy_disabled = routefile.load(self.write_routes(
            base.replace("antigravity/gemini-4-neon/high: enabled", "antigravity/gemini-4-neon/high: disabled")
        ))
        self.assertEqual(
            routefile.resolve_dispatch(agy_disabled, role="review", author_families={"OpenAI"}).error_code,
            "reviewer_same_family",
        )
        self.assertEqual(
            routefile.resolve_dispatch(both, role="review", author_families={"OpenAI"},
                                       exclude_agents=["antigravity"]).error_code,
            "reviewer_same_family",
        )
        pinned_codex = routefile.load(self.write_routes(
            base.replace("pinned: null", "pinned: codex/gpt-6.1-terra/high") + "paused_agents: [antigravity]\n"
        ))
        self.assertEqual(
            routefile.resolve_dispatch(pinned_codex, role="review", author_families={"OpenAI"}).error_code,
            "reviewer_same_family",
        )

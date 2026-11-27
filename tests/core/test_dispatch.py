import os

from pod import cli, commands, orca, state
from tests.core.support import CoreCase, ROUTES


class DispatchTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.opened()

    def start(self, **options):
        return commands.dispatch(self.repo, None, **{"role": "implement", "title": "write", "spec": "Do it.", **options})

    def test_it_starts_one_worker_on_the_requested_route_with_a_frozen_packet(self):
        started = self.start(paths_=["src/"], route="codex/sol/high")
        task = self.orca()["tasks"][started["task"]]
        self.assertEqual(task["task_title"], "implement · sol/high · widget:a1 write")
        self.assertIn("Change only: src", task["spec"])
        self.assertIn('"outcome"', task["spec"])
        row = {**self.entries("widget")[-2], **self.entries("widget")[-1]}
        self.assertEqual((row["route"], row["paths"], row["task"], row["dispatch"]),
                         ("codex/sol/high", ["src"], started["task"], started["dispatch"]))
        import hashlib
        self.assertEqual(row["packet"], hashlib.sha256(task["spec"].encode()).hexdigest())
        self.assertNotIn("spec", row)                # the task text is Orca's; Pod keeps its digest

    def test_the_worker_tab_is_renamed_and_a_failed_rename_only_adds_a_note(self):
        started = self.start(title="roles cards", route="codex/sol/high", paths_=["first/"])
        self.assertEqual(self.orca()["titles"], {f"term_{started['dispatch']}": "implement · roles cards · sol/high"})
        self.assertNotIn("note", started)
        self.set_orca(rename_fails=True)
        later = self.start(title="x" * 80, paths_=["other/"], worktree="new-child")
        self.assertIn("terminal_not_found", later["note"])
        self.assertTrue(self.entries("widget")[-1]["dispatch"])
        self.assertNotIn("void", self.entries("widget")[-1])
        self.assertEqual(len(self.orca()["titles"]), 1)
        self.assertEqual(self.orca()["starts"], 2)
        self.set_orca(rename_fails=False)
        long = self.start(title="y" * 80, paths_=["third/"], worktree="new-child")
        self.assertLessEqual(len(self.orca()["titles"][f"term_{long['dispatch']}"]), 60)

    def test_the_tab_title_shows_the_route_orca_started(self):
        self.set_orca(effective={"agent": "codex", "model": "sol", "effort": "low"})
        started = self.start(title="t", route="codex/sol/high", paths_=["e/"])
        self.assertTrue(self.orca()["titles"][f"term_{started['dispatch']}"].endswith("sol/low"))

    def test_the_worker_lookup_stops_at_the_matching_page(self):
        pages = {None: ([{"dispatchId": "d1", "agentTerminalHandle": "t1"}], "c2"),
                 "c2": ([{"dispatchId": "d2", "agentTerminalHandle": "t2"}], None)}
        asked = []
        def fake_call(*args, **kw):
            cursor = args[args.index("--cursor") + 1] if "--cursor" in args else None
            asked.append(cursor)
            rows, nxt = pages[cursor]
            return {"workers": rows, "page": {"hasMore": bool(nxt), "nextCursor": nxt}}
        original = orca.call
        try:
            orca.call = fake_call
            self.assertEqual(len(orca.workers("r", 15, "d1")), 1)
            self.assertEqual(asked, [None])
            self.assertEqual(len(orca.workers("r")), 2)
        finally:
            orca.call = original

    def test_a_rename_that_raises_anything_or_finds_no_terminal_is_only_a_note(self):
        original = orca.workers
        try:
            orca.workers = lambda run, *rest: (_ for _ in ()).throw(TypeError("odd reply"))
            self.assertEqual(self.start(paths_=["a/"])["note"], "the worker tab may keep Orca's title (rename: TypeError)")
            orca.workers = lambda run, *rest: []
            self.assertIn("no terminal", self.start(paths_=["b/"], worktree="new-child")["note"])
        finally:
            orca.workers = original
        self.refused("dispatch_incomplete", self.start, title="   ")
        self.refused("dispatch_incomplete", self.start, title=None)

    def test_only_enabled_routes_and_the_pin_are_used(self):
        self.refused("route_not_enabled", self.start, route="codex/sol/low")
        self.refused("route_not_enabled", self.start, route="codex/unlisted/high")
        self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/sol/high"))
        self.refused("route_not_pinned", self.start, route="claude/opus/high")
        self.assertEqual(self.start()["route"], "codex/sol/high")
        self.assertEqual(self.orca()["starts"], 1)

    def test_without_a_pin_or_preferred_route_one_must_be_named(self):
        self.write_routes(ROUTES.replace("preferred: claude/opus/high", "preferred: null"))
        self.assertIn("claude/opus/high", self.refused("route_required", self.start).fix)

    def test_one_writer_per_overlapping_path_set_and_one_builder_per_worktree_at_a_time(self):
        first = self.start(paths_=["src/app"])
        self.refused("paths_overlap", self.start, paths_=["src"])
        self.refused("paths_overlap", self.start, paths_=["src/app/main.py"])
        self.refused("paths_overlap", self.start)                         # no declared paths means the whole tree
        # Disjoint path in the same worktree is refused with worktree_busy; --worktree new-child is allowed.
        busy = self.refused("worktree_busy", self.start, paths_=["docs"])
        self.assertIn("--worktree new-child", busy.fix)
        self.start(paths_=["docs"], worktree="new-child")
        self.finish(first["task"], {"outcome": "succeeded"})
        self.assertEqual(self.start(paths_=["src"])["assignment"], "a3")
        self.assertEqual(self.orca()["starts"], 3)

    def test_worktree_busy_allows_concurrent_scouts_and_reviewers(self):
        self.commit({"src/a.py": "a\n"})
        self.start(paths_=["src"])
        for index in range(4):
            self.assertEqual(self.start(role="investigate", title=f"scout-{index}")["assignment"], f"a{index + 2}")
        for index in range(2):
            self.assertEqual(self.start(role="review", title=f"rev-{index}")["assignment"], f"a{index + 6}")
        self.assertEqual(self.orca()["starts"], 7)

    def test_a_lost_start_reply_is_settled_by_reading_orca_and_never_starts_a_second_worker(self):
        self.set_orca(lose_reply=True)
        problem = self.refused("orca_unknown", self.start, paths_=["src"])
        self.assertIn("pod dispatch --retry a1", problem.fix)
        self.assertEqual(self.orca()["starts"], 1)
        self.refused("paths_overlap", self.start, paths_=["src"])          # unknown counts as running
        settled = commands.dispatch(self.repo, None, retry="a1")
        self.assertEqual((settled["task"], settled["dispatch"]), ("task_1", "ctx_1"))
        self.assertEqual(commands.dispatch(self.repo, None, retry="a1")["already"], True)
        self.assertEqual(self.orca()["starts"], 1)

    def test_a_start_that_never_reached_orca_is_voided_and_frees_its_paths(self):
        self.set_orca(fail="worker-start")
        self.refused("orca_unknown", self.start, paths_=["src"])
        self.assertEqual(commands.dispatch(self.repo, None, retry="a1")["void"], True)
        self.assertEqual(self.start(paths_=["src"])["assignment"], "a2")
        self.assertEqual(self.orca()["starts"], 1)

    def test_the_same_assignment_always_carries_the_same_request(self):
        self.assertEqual(orca.request_id("acme--widgets", "widget", "a1", 1), orca.request_id("acme--widgets", "widget", "a1", 1))
        self.assertNotEqual(orca.request_id("acme--widgets", "widget", "a1", 1), orca.request_id("acme--widgets", "widget", "a2", 1))
        self.start(paths_=["src"])
        self.assertEqual(self.entries("widget")[-2]["request"], orca.request_id("acme--widgets", "widget", "a1", 1))
        self.assertIn(orca.request_id("acme--widgets", "widget", "a1", 1), self.orca()["requests"])

    def test_a_second_coordinator_cannot_start_work_until_it_takes_the_run_over(self):
        self.start(paths_=["src"])
        os.environ["FAKE_ORCA_CALLER"] = "term_two"
        problem = self.refused("consumer_fenced", self.start, paths_=["docs"], worktree="new-child")
        self.assertIn("pod resume", problem.fix)
        self.assertEqual(self.orca()["starts"], 1)
        self.assertTrue(self.entries("widget")[-1]["void"])
        commands.resume(self.repo, None)
        self.refused("paths_overlap", self.start, paths_=["src"])          # it sees the first coordinator's worker
        self.assertEqual(self.start(paths_=["docs"], worktree="new-child")["assignment"], "a3")
        os.environ["FAKE_ORCA_CALLER"] = "term_one"
        self.refused("consumer_fenced", self.start, paths_=["tests"], worktree="new-child")      # the earlier conversation is fenced now
        self.assertEqual(self.orca()["starts"], 2)

    def test_a_route_orca_did_not_honour_is_recorded_and_shown(self):
        self.set_orca(effective={"agent": "codex", "model": "sol", "effort": "low"})
        started = self.start(route="codex/sol/high")
        self.assertEqual(started["route_mismatch"], "asked for codex/sol/high, Orca started codex/sol/low")
        self.assertEqual(self.entries("widget")[-1]["effective"], "codex/sol/low")

    def test_a_review_freezes_the_commit_and_the_paths_it_covers(self):
        commit = self.commit({"src/a.py": "a\n", "docs/b.md": "b\n"})
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", paths_=["src"], context=["docs/b.md"])
        row = self.entries("widget")[-2]
        self.assertEqual((row["base"], row["paths"], row["context"]), (commit, ["src/a.py"], ["docs/b.md"]))
        self.assertIn(f"Review commit {commit}", self.orca()["tasks"][started["task"]]["spec"])
        self.refused("nothing_to_review", commands.dispatch, self.repo, None, role="review", title="r", spec="x", paths_=["nowhere"])

    def test_deleted_finding_paths_can_be_the_entire_review_scope(self):
        names = ["src/a.py", "src/a.py:111"]
        self.commit(dict.fromkeys(names, "bad\n"))
        self.review("fail", [{"path": p, "required": True, "text": "remove this"}
                             for p in ("src/a.py:12:2", "src/a.py:111")])
        self.commit(dict.fromkeys(names))
        self.assertEqual(self.git(self.repo, "diff", "--name-only", "origin/main", "HEAD"), "")
        for requested, expected in (([names[0]], [names[0]]), ([names[1]], [names[1]]), (["src"], names), (None, names)):
            with self.subTest(requested=requested):
                self.review("pass", paths_=requested)
                self.assertEqual(self.entries("widget")[-1]["paths"], expected)
        self.refused("nothing_to_review", commands.dispatch, self.repo, None, role="review", title="r", spec="Review.",
                     paths_=["missing.py"])

    def test_a_new_worktree_starts_from_the_objectives_branch(self):
        here = self.start(paths_=["src"])
        there = self.start(paths_=["docs"], worktree="new-child")
        tasks = self.orca()["tasks"]
        self.assertEqual((tasks[here["task"]]["base_branch"], tasks[there["task"]]["base_branch"]), (None, "work"))
        self.assertEqual((tasks[here["task"]]["repo"], tasks[there["task"]]["repo"]), (None, f"path:{self.repo}"))

    def test_a_start_orca_reports_as_failed_is_a_refusal_with_its_reason_and_holds_no_paths(self):
        self.set_orca(start_fails="fatal: invalid reference: work")
        problem = self.refused("start_failed", self.start, paths_=["src"], worktree="new-child")
        self.assertIn("invalid reference", str(problem))
        self.assertTrue(self.entries("widget")[-1]["void"])
        self.set_orca(start_fails=None)
        self.assertEqual(self.start(paths_=["src"])["assignment"], "a2")

    def test_a_closed_objective_starts_nothing(self):
        commands.drop(self.repo, None, why="stopped")
        self.refused("objective_closed", self.start)
        self.assertFalse(self.orca_file.exists())

    def start_review(self, **options):
        return self.start(role="review", title="review", **options)

    def test_review_agents_absent_or_empty_leaves_route_selection_unchanged(self):
        self.commit({"src/a.py": "a\n"})
        self.assertEqual(self.start_review()["route"], "claude/opus/high")
        self.write_routes(ROUTES + "review_agents: []\n")
        self.assertEqual(self.start_review()["route"], "claude/opus/high")

    def test_disallowed_review_route_refuses_before_any_write_or_worker_start(self):
        self.commit({"src/a.py": "a\n"})
        preferences = ROUTES + "review_agents: [codex]\n"
        entries = self.entries("widget")
        for source, config, options in (
            ("explicit", preferences, {"route": "claude/opus/high"}),
            ("pinned", preferences.replace("pinned: null", "pinned: claude/opus/high"), {}),
            ("preferred", preferences, {}),
        ):
            with self.subTest(source=source):
                self.write_routes(config)
                refusal = self.refused("reviewer_agent_not_allowed", self.start_review, **options)
                self.assertIn("codex/sol/high", refusal.fix)
                self.assertEqual(self.entries("widget"), entries)
                self.assertFalse(self.orca_file.exists())

    def test_other_roles_can_still_use_agents_outside_the_review_list(self):
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        self.assertEqual(self.start()["route"], "claude/opus/high")
        self.assertEqual(self.start(role="investigate")["route"], "claude/opus/high")

    def test_review_refusal_with_a_disallowed_pin_and_no_allowed_route_explains_both_changes(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES.replace("pinned: null", "pinned: claude/opus/high") + "review_agents: [other]\n")
        refusal = self.refused("reviewer_agent_not_allowed", self.start_review)
        self.assertIn("enable", refusal.fix)
        self.assertIn("Pin", refusal.fix)

    def test_words_exception_is_written_on_the_initial_assignment(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        words = "Please use Claude for this review."
        args = cli.parser().parse_args(["dispatch", "--role", "review", "--title", "review", "--spec", "Review.",
                                       "--words", words])
        started = cli.execute(args, self.repo)
        initial = self.entries("widget")[-2]
        self.assertEqual(initial["id"], started["assignment"])
        self.assertEqual(initial["review_exception"], {"kind": "words", "words": words})
        self.assertNotIn("dispatch", initial)

    def test_after_exception_is_written_for_a_failed_allowed_review(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        prior = self.start_review(route="codex/sol/high")
        self.finish(prior["task"], None, outcome="failed")
        started = self.start_review(after=prior["assignment"], why="The first worker failed.")
        initial = self.entries("widget")[-2]
        self.assertEqual(initial["id"], started["assignment"])
        self.assertEqual(initial["review_exception"], {"kind": "after", "after": prior["assignment"],
                                                       "why": "The first worker failed."})

    def test_after_exception_can_reference_a_void_allowed_review(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        self.set_orca(start_fails="Could not launch")
        self.refused("start_failed", self.start_review, route="codex/sol/high")
        self.set_orca(start_fails=None)
        args = cli.parser().parse_args(["dispatch", "--role", "review", "--title", "review", "--spec", "Review.",
                                       "--after", "a1", "--why", "The first worker could not launch."])
        self.assertEqual(cli.execute(args, self.repo)["assignment"], "a2")

    def test_after_exception_cannot_use_a_later_dispatch_failure_on_the_same_task(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        prior = self.start_review(route="codex/sol/high")
        native = self.orca()
        native["tasks"][prior["task"]]["status"] = "failed"
        first = native["workers"][prior["dispatch"]]
        first.update(workerState="stopped", dispatchStatus="cancelled")
        native["workers"]["ctx_retry"] = dict(first, dispatchId="ctx_retry", workerState="failed", dispatchStatus="failed")
        self.set_orca(**native)
        entries = self.entries("widget")
        self.refused("review_exception_invalid", self.start_review, after=prior["assignment"], why="Try another agent.")
        self.assertEqual(self.entries("widget"), entries)
        self.assertEqual(self.orca()["starts"], 1)

    def test_after_exception_refuses_ineligible_prior_assignments_without_effects(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        different = self.start_review(route="codex/sol/high")
        self.finish(different["task"], None, outcome="failed")
        self.commit({"src/a.py": "b\n"})
        completed = self.start_review(route="codex/sol/high")
        self.finish(completed["task"], {"outcome": "succeeded", "verdict": "pass"})
        failed_with_review = self.start_review(route="codex/sol/high")
        self.finish(failed_with_review["task"], {"outcome": "failed", "verdict": "fail"}, outcome="failed")
        commands.accept(self.repo, None, failed_with_review["assignment"])
        running = self.start_review(route="codex/sol/high")
        disallowed = self.start_review(words="Please use Claude for this review.")
        self.finish(disallowed["task"], None, outcome="failed")
        nonreview = self.start(role="investigate", route="codex/sol/high")
        self.finish(nonreview["task"], None, outcome="failed")
        entries, starts = self.entries("widget"), self.orca()["starts"]
        for ident in (different["assignment"], completed["assignment"], failed_with_review["assignment"],
                      running["assignment"], disallowed["assignment"], nonreview["assignment"], "a99"):
            with self.subTest(ident=ident):
                self.refused("review_exception_invalid", self.start_review, after=ident, why="Try another agent.")
                self.assertEqual(self.entries("widget"), entries)
                self.assertEqual(self.orca()["starts"], starts)

    def test_incomplete_or_combined_exception_flags_are_refused_without_effects(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        entries = self.entries("widget")
        for flags in ({"words": " "}, {"after": "a1"}, {"why": "Try another agent."},
                      {"words": "Please use Claude.", "after": "a1", "why": "Try another agent."}):
            with self.subTest(flags=flags):
                self.refused("review_exception_invalid", self.start_review, **flags)
                self.assertEqual(self.entries("widget"), entries)
                self.assertFalse(self.orca_file.exists())

    def test_lost_reply_and_retry_keep_the_exception_even_after_preferences_change(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        self.set_orca(lose_reply=True)
        words = "Please use Claude for this review."
        problem = self.refused("orca_unknown", self.start_review, words=words)
        self.assertIn("--retry a1", problem.fix)
        self.write_routes(ROUTES + "review_agents: invalid\n")
        self.assertEqual(commands.dispatch(self.repo, None, retry="a1")["dispatch"], "ctx_1")
        self.assertTrue(commands.dispatch(self.repo, None, retry="a1")["already"])
        row = state.fold(self.entries("widget")).assignments["a1"]
        self.assertEqual(row["review_exception"], {"kind": "words", "words": words})
        self.assertEqual(self.orca()["starts"], 1)

    def test_antigravity_wire_translation_omits_effort_and_avoids_false_route_mismatch(self):
        self.write_routes(
            ROUTES.replace("routes:\n", "routes:\n  antigravity/gemini-4-neon/high: enabled\n")
        )
        started = self.start(paths_=["src"], route="antigravity/gemini-4-neon/high")
        self.assertEqual(started["route"], "antigravity/gemini-4-neon/high")
        self.assertNotIn("route_mismatch", started)
        req = next(iter(self.orca()["requests"].values()))["receipt"]["launch"]["requested"]
        self.assertEqual(req, {"agent": "antigravity", "model": "gemini-4-neon-high", "effort": None})
        # Genuine mismatch on AGY (low instead of high) is still flagged.
        self.set_orca(effective={"agent": "antigravity", "model": "gemini-4-neon-low", "effort": None})
        mismatch = self.start(paths_=["docs"], route="antigravity/gemini-4-neon/high", worktree="new-child")
        self.assertEqual(mismatch["route_mismatch"],
                         "asked for antigravity/gemini-4-neon/high, Orca started antigravity/gemini-4-neon/low")

    def test_profile_dispatch_dry_run_paused_agents_and_review_tags_end_to_end(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, claude, antigravity]\n"
            "profiles:\n"
            "  scout: investigate+mcp antigravity/gemini-4-neon/medium claude/claude-sonnet-5-5/high\n"
            "  build-deep: implement antigravity/gemini-4-neon/high codex/gpt-6.1-terra/high\n"
            "  review-fast: review codex/gpt-6.1-terra/high claude/claude-sonnet-5-5/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        before_entries = self.entries("widget")
        dry = commands.dispatch(self.repo, None, profile="scout", exclude_agents=["antigravity"], dry_run=True)
        self.assertTrue(dry["dry_run"] and dry["pass"])
        self.assertEqual((dry["route"], dry["mcp"]), ("claude/claude-sonnet-5-5/high", True))
        self.assertEqual(self.entries("widget"), before_entries)
        self.assertFalse(self.orca_file.exists())
        # Implement with Neon via build-deep
        built = commands.dispatch(self.repo, None, profile="build-deep", title="build", spec="Build it.")
        self.assertEqual((built["route"], built["profile"], built["mcp"]),
                         ("antigravity/gemini-4-neon/high", "build-deep", False))
        commit = self.commit({"src/a.py": "a\n"})
        self.finish(built["task"], {"outcome": "succeeded", "commit": commit})
        commands.accept(self.repo, None, built["assignment"], integrated=commit)
        # Pause Codex -> review-fast falls back to Claude Sonnet with cross_provider_fallback
        self.write_routes(cfg + "paused_agents: [codex]\n")
        rev = commands.dispatch(self.repo, None, profile="review-fast", title="rev", spec="Review it.")
        self.assertEqual((rev["route"], rev["review_independence"]),
                         ("claude/claude-sonnet-5-5/high", "cross_provider_fallback"))
        self.finish(rev["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        accepted = commands.accept(self.repo, None, rev["assignment"])
        self.assertEqual(accepted["review_independence"], "cross_provider_fallback")
        review_fact = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(review_fact["review_independence"], "cross_provider_fallback")

    def test_same_session_review_covers_nothing_and_never_persists_handles(self):
        from pod import proof
        commit, _ = self.implement({"src/a.py": "a\n"})
        rev = commands.dispatch(self.repo, None, role="review", title="rev", spec="Review.", route="codex/sol/high")
        # Simulate Orca reporting the same terminal handle as the implementer (a1 -> ctx_1).
        native = self.orca()
        native["workers"][rev["dispatch"]]["agentTerminalHandle"] = native["workers"]["ctx_1"]["agentTerminalHandle"]
        self.set_orca(**native)
        self.finish(rev["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        accepted = commands.accept(self.repo, None, rev["assignment"])
        self.assertIn("same session", accepted["note"])
        folded = state.fold(self.entries("widget"))
        review_fact = folded.of("review")[-1]
        self.assertTrue(review_fact["same_session"])
        # Same-session review covers nothing.
        base = self.git(self.repo, "merge-base", commit, "origin/main")
        self.assertEqual(proof.coverage(self.repo, folded, commit, base)["uncovered"], ["src/a.py"])
        # Never persist terminal handles into the objective log.
        import json as _json
        log_text = _json.dumps(self.entries("widget"))
        self.assertNotIn("term_", log_text)
        self.assertNotIn("handle", log_text)

    def test_mixed_authorship_soft_drain_and_persisted_reviewer_notes_survive_handoff(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, claude, antigravity]\n"
            "profiles:\n"
            "  build-deep: implement antigravity/gemini-4-neon/high codex/gpt-6.2-sol/high claude/claude-sonnet-5-5/high\n"
            "  review-fast: review codex/gpt-6.1-terra/high claude/claude-sonnet-5-5/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        # Step 1: Antigravity implements part 1 and integrates WIP.
        b1 = commands.dispatch(self.repo, None, profile="build-deep", title="part1", spec="Part 1.")
        c1 = self.commit({"src/a.py": "part1\n"})
        self.finish(b1["task"], {"outcome": "succeeded", "commit": c1})
        commands.accept(self.repo, None, b1["assignment"], integrated=c1)
        # Step 2: Antigravity is soft-drained -> successor Builder falls back to Codex (mixed authorship).
        self.write_routes(cfg + "soft_drained_agents:\n  antigravity: quota_low\n")
        b2 = commands.dispatch(self.repo, None, profile="build-deep", title="part2", spec="Part 2.")
        self.assertEqual(b2["route"], "codex/gpt-6.2-sol/high")
        self.assertTrue(any("soft-drained" in line for line in b2["trace"]))
        c2 = self.commit({"src/b.py": "part2\n"})
        self.finish(b2["task"], {"outcome": "succeeded", "commit": c2})
        commands.accept(self.repo, None, b2["assignment"], integrated=c2)
        # Step 3: Review under mixed authorship {Google, OpenAI} -> picks third family Anthropic (Claude)!
        self.write_routes(cfg)
        rev1 = commands.dispatch(self.repo, None, profile="review-fast", title="rev1", spec="Review.")
        self.assertEqual((rev1["route"], rev1["review_independence"], rev1["author_families"]),
                         ("claude/claude-sonnet-5-5/high", "cross_provider_fallback", ["Google", "OpenAI"]))
        self.assertEqual(self.orca()["tasks"][rev1["task"]]["task_title"],
                         "review-fast · claude-sonnet-5-5/high · widget:a3 rev1")
        # Starved/interrupted Reviewer emits notes + partial finding without verdict -> persisted on result fact.
        self.finish(rev1["task"], {
            "outcome": "failed",
            "notes": "Checked src/a.py; still need to verify src/b.py error handling.",
            "findings": [{"path": "src/a.py", "required": True, "text": "missing null guard"}],
        }, outcome="failed")
        acc1 = commands.accept(self.repo, None, rev1["assignment"])
        self.assertIn("Checked src/a.py", acc1["reviewer_notes"])
        self.assertEqual(len(acc1["partial_findings"]), 1)
        # Dry-run and status surface persisted reviewer notes & mixed authors.
        dry = commands.dispatch(self.repo, None, profile="review-fast", dry_run=True)
        self.assertEqual(dry["author_families"], ["Google", "OpenAI"])
        self.assertTrue(any("Checked src/a.py" in line for line in dry["reviewer_notes"]))
        # Successor Reviewer packet carries Prior reviewer notes!
        rev2 = commands.dispatch(self.repo, None, profile="review-fast", title="rev2", spec="Finish review.")
        rev2_spec = self.orca()["tasks"][rev2["task"]]["spec"]
        self.assertIn("## Prior reviewer notes", rev2_spec)
        self.assertIn("Checked src/a.py", rev2_spec)
        self.finish(rev2["task"], {
            "outcome": "succeeded", "verdict": "fail",
            "notes": "Verified both files; fix src/a.py guard.",
            "findings": [{"path": "src/a.py", "required": True, "text": "missing null guard"}],
        })
        acc2 = commands.accept(self.repo, None, rev2["assignment"])
        self.assertEqual(acc2["notes"], "Verified both files; fix src/a.py guard.")
        self.assertEqual(acc2["author_families"], ["Google", "OpenAI"])
        # Successor Builder packet receives Reviewer context automatically!
        b3 = commands.dispatch(self.repo, None, profile="build-deep", title="fix", spec="Fix finding.")
        b3_spec = self.orca()["tasks"][b3["task"]]["spec"]
        self.assertIn("## Reviewer context", b3_spec)
        self.assertIn("Verified both files; fix src/a.py guard.", b3_spec)

    def test_agy_claude_review_of_neon_is_cross_provider_and_of_claude_is_same_provider(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-opus-5-5/high: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity, claude]\n"
            "profiles:\n"
            "  build-deep: implement antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
            "  review-deep: review codex/gpt-6.2-sol/high antigravity/claude-opus-5-5/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
        )
        self.write_routes(cfg)
        b1 = commands.dispatch(self.repo, None, profile="build-deep", title="neon work", spec="Build.")
        c1 = self.commit({"src/a.py": "neon\n"})
        self.finish(b1["task"], {"outcome": "succeeded", "commit": c1})
        commands.accept(self.repo, None, b1["assignment"], integrated=c1)
        # Pause codex + agy-gemini: antigravity/claude-opus-5-5 (agy-claude pool) still reviews Neon as cross_provider_fallback.
        self.write_routes(cfg + "paused_agents: [codex, agy-gemini]\n")
        rev = commands.dispatch(self.repo, None, profile="review-deep", title="opus review", spec="Review.")
        self.assertEqual((rev["route"], rev["review_independence"], rev["author_families"]),
                         ("antigravity/claude-opus-5-5/high", "cross_provider_fallback", ["Google"]))
        self.assertEqual(self.orca()["tasks"][rev["task"]]["task_title"],
                         "review-deep · claude-opus-5-5/high · widget:a2 opus review")
        self.finish(rev["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc = commands.accept(self.repo, None, rev["assignment"])
        self.assertEqual((acc["review_independence"], acc["author_families"]),
                         ("cross_provider_fallback", ["Google"]))
        # Now claude/claude-opus-5-5 also integrates -> authors = [Anthropic, Google];
        # antigravity/claude-opus-5-5 reviewing Anthropic diff is same_provider (when no review_agents) / same_provider_fallback!
        b2 = commands.dispatch(self.repo, None, role="implement", route="claude/claude-opus-5-5/high",
                               title="claude work", spec="Edit.")
        c2 = self.commit({"src/b.py": "claude\n"})
        self.finish(b2["task"], {"outcome": "succeeded", "commit": c2})
        commands.accept(self.repo, None, b2["assignment"], integrated=c2)
        self.write_routes(cfg.replace("review_agents: [codex, antigravity, claude]\n", ""))
        rev2 = commands.dispatch(self.repo, None, role="review", route="antigravity/claude-opus-5-5/high",
                                 title="same family", spec="Review.")
        self.assertEqual(rev2["review_independence"], "same_provider")

    def test_two_part_antigravity_effective_route_never_crashes_dispatch_or_wedges_objective(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-opus-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity, claude]\n"
            "profiles:\n"
            "  build-deep: implement antigravity/gemini-4-neon/high\n"
            "  review-deep: review antigravity/claude-opus-5-5/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        # Orca reports a two-part effective launch for implementer -> recorded as route_mismatch without wedging.
        self.set_orca(effective={"agent": "antigravity", "model": "gemini-4-neon", "effort": None})
        built = commands.dispatch(self.repo, None, profile="build-deep", title="build", spec="Build.")
        self.assertEqual(built["route_mismatch"],
                         "asked for antigravity/gemini-4-neon/high, Orca started antigravity/gemini-4-neon")
        commit = self.commit({"src/a.py": "a\n"})
        self.finish(built["task"], {"outcome": "succeeded", "commit": commit})
        commands.accept(self.repo, None, built["assignment"], integrated=commit)
        # Subsequent dry-run, review dispatch (also with two-part effective), and review accept all succeed cleanly.
        dry = commands.dispatch(self.repo, None, profile="review-deep", dry_run=True)
        self.assertEqual((dry["ok"], dry["author_families"], dry["review_independence"]),
                         (True, ["Google"], "cross_provider_fallback"))
        self.set_orca(effective={"agent": "antigravity", "model": "claude-opus-5-5", "effort": None})
        rev = commands.dispatch(self.repo, None, profile="review-deep", title="rev", spec="Review.")
        self.assertEqual((rev["review_independence"], rev["author_families"]),
                         ("cross_provider_fallback", ["Google"]))
        self.finish(rev["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        accepted = commands.accept(self.repo, None, rev["assignment"])
        self.assertEqual((accepted["verdict"], accepted["review_independence"], accepted["author_families"]),
                         ("pass", "cross_provider_fallback", ["Google"]))

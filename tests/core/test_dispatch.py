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
        started = self.start(paths_=["src/"], route="codex/sol/high", reason="test route")
        task = self.orca()["tasks"][started["task"]]
        self.assertEqual(task["task_title"], "[widget:a1] implement · sol/high · write")
        self.assertIn("Change only: src", task["spec"])
        self.assertIn('"outcome"', task["spec"])
        row = {**self.entries("widget")[-2], **self.entries("widget")[-1]}
        self.assertEqual((row["route"], row["paths"], row["task"], row["dispatch"]),
                         ("codex/sol/high", ["src"], started["task"], started["dispatch"]))
        import hashlib
        self.assertEqual(row["packet"], hashlib.sha256(task["spec"].encode()).hexdigest())
        self.assertNotIn("spec", row)                # the task text is Orca's; Pod keeps its digest

    def test_the_worker_tab_is_renamed_and_a_failed_rename_only_adds_a_note(self):
        started = self.start(title="roles cards", route="codex/sol/high", reason="test route", paths_=["first/"])
        self.assertEqual(self.orca()["titles"], {f"term_{started['dispatch']}": "Builder · roles cards · sol/high"})
        self.assertNotIn("note", started)
        self.set_orca(rename_fails=True)
        later = self.start(title="x" * 80, paths_=["other/"], worktree="new-child")
        self.assertIn("terminal_not_found", later["note"])
        self.assertTrue(self.entries("widget")[-1]["dispatch"])
        self.assertNotIn("void", self.entries("widget")[-1])
        self.assertEqual(self.orca()["titles"][f"term_{later['dispatch']}"], f"worker-{later['task']}")
        self.assertEqual(self.orca()["starts"], 2)
        self.set_orca(rename_fails=False)
        long = self.start(title="y" * 80, paths_=["third/"], worktree="new-child")
        long_title = self.orca()["titles"][f"term_{long['dispatch']}"]
        self.assertTrue(long_title.startswith("Builder · "))
        self.assertLessEqual(len(long_title), 60)

    def test_the_tab_title_shows_the_route_orca_started(self):
        self.set_orca(effective={"agent": "codex", "model": "sol", "effort": "low"})
        started = self.start(title="t", route="codex/sol/high", reason="test route", paths_=["e/"])
        self.assertEqual(self.orca()["titles"][f"term_{started['dispatch']}"], "Builder · t · sol/low")

    def test_tab_title_uses_ce_dev_role_words_and_short_models(self):
        role_cases = (
            (None, "implement", "Builder"),
            (None, "review", "Reviewer"),
            (None, "investigate", "Scout"),
            ("build-fast", "implement", "Builder"),
            ("build-deep", "implement", "Builder (deep)"),
            ("ui-design", "implement", "Builder (UI)"),
            ("ui-light", "implement", "Builder (UI)"),
            ("ui-accessibility", "implement", "Builder (UI)"),
            ("ui-analyst", "implement", "Builder (UI)"),
            ("ui-challenger", "implement", "Builder (UI)"),
            ("review-fast", "review", "Reviewer (fast)"),
            ("review-deep", "review", "Reviewer (deep)"),
            ("scout", "investigate", "Scout"),
            ("scout-fast", "investigate", "Scout"),
            ("scout-deep", "investigate", "Scout (deep)"),
            ("analyst", "investigate", "Analyst"),
            ("analyst-deep", "investigate", "Analyst"),
            ("repo-analyst", "investigate", "Analyst"),
            ("challenger", "review", "Challenger"),
            ("challenger-deep", "investigate", "Challenger"),
            ("custom-worker", "implement", "Builder"),
            ("custom-worker", "review", "Reviewer"),
            ("custom-worker", "investigate", "Scout"),
        )
        for profile, role, expected in role_cases:
            with self.subTest(profile=profile, role=role):
                rendered = commands._tab_title(profile, role, "codex/gpt-6.2-sol/high", "check tabs")
                self.assertEqual(rendered, f"{expected} · check tabs · sol/high")
                first_word = rendered.split(" · ", 1)[0].lower()
                self.assertNotIn(first_word, ("implement", "investigate", "review"))
                if profile:
                    self.assertNotIn(profile, rendered)
        # Long topics and 96-character model tokens stay <= 60 chars while keeping role, topic and effort.
        long_rev = commands._tab_title("review-deep", "review", "codex/gpt-6.2-astra/xhigh", "very " * 20)
        self.assertTrue(long_rev.startswith("Reviewer (deep) · "))
        self.assertTrue(long_rev.endswith(" · astra/xhigh"))
        self.assertLessEqual(len(long_rev), 60)
        long_model = commands._tab_title("review-deep", "review", "codex/" + "m" * 96 + "/high", "audit tab title length")
        self.assertTrue(long_model.startswith("Reviewer (deep) · "))
        self.assertTrue(long_model.endswith(f" · {'m' * 16}/high"))
        self.assertNotIn("…/", long_model)
        self.assertLessEqual(len(long_model), 60)
        model_cases = (
            ("claude/claude-opus-5-5/high", "opus/high"),
            ("claude/claude-sonnet-5-5/medium", "sonnet/medium"),
            ("claude/claude-haiku-5-5/low", "haiku/low"),
            ("claude/claude-fable-5-2/high", "fable/high"),
            ("antigravity/gemini-4-neon/xhigh", "neon/xhigh"),
            ("antigravity/gemini-4-neon", "neon"),
            ("codex/gpt-6.2-sol/medium", "sol/medium"),
            ("codex/gpt-6.1-terra/low", "terra/low"),
            ("codex/gpt-6.2-astra/high", "astra/high"),
            ("codex/gpt-6-luna/low", "luna/low"),
            ("codex/acme-7-nova-2.1/high", "nova/high"),
            ("codex/vendor-zephyr-v3/medium", "zephyr/medium"),
            ("codex/1-2-3/low", "1-2-3/low"),
            ("codex/4.0-5.1/high", "4.0-5.1/high"),
        )
        for route, expected_tail in model_cases:
            with self.subTest(route=route):
                self.assertEqual(commands._tab_title("build-fast", "implement", route, "topic"),
                                 f"Builder · topic · {expected_tail}")
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.1-terra/low: enabled\n"
            "preferred: null\npinned: null\n"
            "profiles:\n"
            "  build-deep: implement antigravity/gemini-4-neon/high\n"
            "  ui-analyst: implement claude/claude-opus-5-5/high\n"
            "  analyst-deep: investigate antigravity/gemini-4-neon/high\n"
            "  challenger: review codex/gpt-6.2-sol/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high\n"
            "  scout-deep: investigate antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        self.commit({"src/a.py": "a\n"})
        deep = commands.dispatch(self.repo, None, profile="build-deep", title="core refactor", spec="Build.", paths_=["src/"])
        ui = commands.dispatch(self.repo, None, profile="ui-analyst", title="card contrast", spec="Style.", paths_=["ui/"],
                               worktree="new-child")
        analyst = commands.dispatch(self.repo, None, profile="analyst-deep", title="latency study", spec="Study.")
        challenger = commands.dispatch(self.repo, None, profile="challenger", title="stress check", spec="Challenge.")
        reviewer = commands.dispatch(self.repo, None, profile="review-fast", title="check diff", spec="Review.")
        scout = commands.dispatch(self.repo, None, profile="scout-deep", title="find callers", spec="Search.")
        titles = self.orca()["titles"]
        self.assertEqual(titles[f"term_{deep['dispatch']}"], "Builder (deep) · core refactor · neon/high")
        self.assertEqual(titles[f"term_{ui['dispatch']}"], "Builder (UI) · card contrast · opus/high")
        self.assertEqual(titles[f"term_{analyst['dispatch']}"], "Analyst · latency study · neon/high")
        self.assertEqual(titles[f"term_{challenger['dispatch']}"], "Challenger · stress check · sol/medium")
        self.assertEqual(titles[f"term_{reviewer['dispatch']}"], "Reviewer (fast) · check diff · terra/high")
        self.assertEqual(titles[f"term_{scout['dispatch']}"], "Scout (deep) · find callers · neon/high")
        # worker-start --task-title starts with the stable '[<objective>:<ident>]' marker so 80-char truncation keeps it.
        self.assertTrue(
            self.orca()["tasks"][deep["task"]]["task_title"].startswith(f"[widget:{deep['assignment']}] ")
        )
        # Exact example string, stripping accidental objective:aN prefix from topic.
        self.assertEqual(
            commands._tab_title("build-deep", "implement", "antigravity/gemini-4-neon/high",
                                "#520 lifecycle implementation"),
            "Builder (deep) · #520 lifecycle implementation · neon/high",
        )
        self.assertEqual(
            commands._tab_title("build-deep", "implement", "antigravity/gemini-4-neon/high",
                                "issue-520-v5:a11 #520 lifecycle implementation"),
            "Builder (deep) · #520 lifecycle implementation · neon/high",
        )

    def test_pre_named_tab_is_not_overwritten_and_empty_or_default_tab_is_renamed(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  codex/gpt-6.2-sol/high: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "preferred: null\npinned: null\n"
            "profiles:\n"
            "  build-fast: implement codex/gpt-6.2-sol/high\n"
            "  build-deep: implement antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        task_title_4 = commands._task_title(
            "build-deep", "implement", "antigravity/gemini-4-neon/high",
            "widget", "a4", "#520 lifecycle implementation",
        )
        task_title_5 = commands._task_title(
            "build-deep", "implement", "antigravity/gemini-4-neon/high",
            "widget", "a5", "#520 lifecycle implementation",
        )
        coincidental_prefix_5 = task_title_5[:32] + "…"
        task_title_9 = commands._task_title(
            "build-deep", "implement", "antigravity/gemini-4-neon/high",
            "widget", "a9", "trailing spaces  ",
        )
        self.set_orca(titles={
            "term_ctx_1": "Coordinator · pod · sol/med",
            "term_ctx_2": "",
            "term_ctx_4": task_title_4,
            "term_ctx_5": coincidental_prefix_5,
            "term_ctx_6": "worker-task_notes",
            "term_ctx_7": "Custom · build-deep…",
            "term_ctx_8": "Builder · " + task_title_4,
            "term_ctx_9": task_title_9,
        })
        preserved = commands.dispatch(self.repo, None, profile="build-fast", title="keep coordinator", spec="Do.", paths_=["a/"])
        self.assertNotIn("note", preserved)
        self.assertEqual(self.orca()["titles"]["term_ctx_1"], "Coordinator · pod · sol/med")
        self.assertEqual(self.orca().get("rename_calls", []), [])
        empty = commands.dispatch(self.repo, None, profile="build-fast", title="empty title", spec="Do.", paths_=["b/"], worktree="new-child")
        self.assertNotIn("note", empty)
        self.assertEqual(self.orca()["titles"]["term_ctx_2"], "Builder · empty title · sol/high")
        self.assertEqual(len(self.orca()["rename_calls"]), 1)
        default = commands.dispatch(self.repo, None, profile="build-fast", title="default title", spec="Do.", paths_=["c/"], worktree="new-child")
        self.assertNotIn("note", default)
        self.assertEqual(self.orca()["titles"]["term_ctx_3"], "Builder · default title · sol/high")
        self.assertEqual(len(self.orca()["rename_calls"]), 2)
        # Exact --task-title passed to worker-start IS treated as default and renamed.
        exact_task = commands.dispatch(self.repo, None, profile="build-deep", title="#520 lifecycle implementation",
                                       spec="Do.", paths_=["d/"], worktree="new-child")
        self.assertNotIn("note", exact_task)
        self.assertEqual(self.orca()["titles"]["term_ctx_4"],
                         "Builder (deep) · #520 lifecycle implementation · neon/high")
        # Coincidental prefix+ellipsis, custom worker-task_notes, and arbitrary wrappers are NEVER overwritten.
        for idx, (path, expected_kept) in enumerate((
            ("e/", coincidental_prefix_5),
            ("f/", "worker-task_notes"),
            ("g/", "Custom · build-deep…"),
            ("h/", "Builder · " + task_title_4),
        ), start=5):
            commands.dispatch(self.repo, None, profile="build-deep", title="#520 lifecycle implementation",
                              spec="Do.", paths_=[path], worktree="new-child")
            self.assertEqual(self.orca()["titles"][f"term_ctx_{idx}"], expected_kept)
        # Exact task title with trailing spaces matches consistently and IS renamed.
        trailing = commands.dispatch(self.repo, None, profile="build-deep", title="trailing spaces  ",
                                     spec="Do.", paths_=["i/"], worktree="new-child")
        self.assertNotIn("note", trailing)
        self.assertEqual(self.orca()["titles"]["term_ctx_9"], "Builder (deep) · trailing spaces · neon/high")
        self.assertEqual(len(self.orca()["rename_calls"]), 4)

    def test_unreadable_tab_title_returns_a_note_and_does_not_rename(self):
        self.set_orca(list_fails=True)
        failed_list = self.start(title="first", paths_=["a/"])
        self.assertIn("terminal_list_failed", failed_list["note"])
        self.assertEqual(self.orca()["titles"][f"term_{failed_list['dispatch']}"], f"worker-{failed_list['task']}")
        self.assertEqual(self.orca().get("rename_calls", []), [])
        self.set_orca(list_fails=False, omit_title=True)
        missing_title = self.start(title="second", paths_=["b/"], worktree="new-child")
        self.assertIn("could not be read", missing_title["note"])
        self.assertEqual(self.orca()["titles"][f"term_{missing_title['dispatch']}"], f"worker-{missing_title['task']}")
        self.assertEqual(self.orca().get("rename_calls", []), [])
        calls = []
        original_workers, original_call = orca.workers, orca.call
        try:
            orca.workers = lambda run, *rest: [{"dispatch": "ctx_9", "handle": "term_ctx_9", "worktree": None}]
            orca.call = lambda *args, **kw: (calls.append(args), {"terminals": []})[1]
            self.assertEqual(orca.rename("run_1", {"dispatch": "ctx_9"}, "Builder · x · sol/high"),
                             "terminal title could not be read")
            self.assertEqual(calls, [("terminal", "list")])
        finally:
            orca.workers, orca.call = original_workers, original_call

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

    def test_retry_holds_assignment_when_receipt_missing_and_task_title_truncated(self):
        self.set_orca(lose_reply=True)
        self.refused("orca_unknown", self.start, paths_=["src"])
        # Simulate missing receipt + Orca 80-char task-list truncation dropping the marker.
        tasks = self.orca()["tasks"]
        tasks["task_1"]["task_title"] = "build-fast · gpt-6.2-sol/medium · very-long-objective-slug:a..."
        self.set_orca(requests={}, tasks=tasks)
        problem = self.refused("start_unsettled", commands.dispatch, self.repo, None, retry="a1")
        self.assertIn("task-list", problem.fix)
        # Assignment is HELD (never voided) and blocks a second implementer on the same paths.
        self.assertFalse(state.fold(self.entries("widget")).assignments["a1"].get("void"))
        self.refused("paths_overlap", self.start, paths_=["src"])
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
        started = self.start(route="codex/sol/high", reason="test route")
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
        here = self.start(title="#520 lifecycle implementation", paths_=["src"])
        there = self.start(title="#520 lifecycle implementation", paths_=["docs"], worktree="new-child")
        custom = self.start(title="#520 lifecycle implementation", paths_=["tests"], worktree="new-child", name="Custom WT")
        tasks = self.orca()["tasks"]
        self.assertEqual((tasks[here["task"]]["base_branch"], tasks[there["task"]]["base_branch"]), (None, "work"))
        self.assertEqual((tasks[here["task"]]["repo"], tasks[there["task"]]["repo"]), (None, f"path:{self.repo}"))
        # Current worktree gets no creation flags; new-child nests under caller workspace with human --display-name/--name.
        self.assertEqual(tasks[here["task"]]["worktree"], f"path:{self.repo}")
        self.assertEqual(tasks[there["task"]]["worktree"], "new-child")
        self.assertEqual(tasks[custom["task"]]["worktree"], "new-child")
        self.assertEqual((tasks[here["task"]]["name"], tasks[here["task"]]["display_name"]), (None, None))
        self.assertEqual(tasks[there["task"]]["display_name"], "#520 lifecycle implementation")
        self.assertEqual(tasks[there["task"]]["name"], "520-lifecycle-implementation")
        self.assertEqual(tasks[custom["task"]]["display_name"], "Custom WT")
        self.assertEqual(tasks[custom["task"]]["name"], "custom-wt")

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
        prior = self.start_review(route="codex/sol/high", reason="test review")
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
        self.refused("start_failed", self.start_review, route="codex/sol/high", reason="test review")
        self.set_orca(start_fails=None)
        args = cli.parser().parse_args(["dispatch", "--role", "review", "--title", "review", "--spec", "Review.",
                                       "--after", "a1", "--why", "The first worker could not launch."])
        self.assertEqual(cli.execute(args, self.repo)["assignment"], "a2")

    def test_after_exception_cannot_use_a_later_dispatch_failure_on_the_same_task(self):
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        prior = self.start_review(route="codex/sol/high", reason="test review")
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
        different = self.start_review(route="codex/sol/high", reason="test review")
        self.finish(different["task"], None, outcome="failed")
        self.commit({"src/a.py": "b\n"})
        completed = self.start_review(route="codex/sol/high", reason="test review")
        self.finish(completed["task"], {"outcome": "succeeded", "verdict": "pass"})
        failed_with_review = self.start_review(route="codex/sol/high", reason="test review")
        self.finish(failed_with_review["task"], {"outcome": "failed", "verdict": "fail"}, outcome="failed")
        commands.accept(self.repo, None, failed_with_review["assignment"])
        running = self.start_review(route="codex/sol/high", reason="test review")
        disallowed = self.start_review(words="Please use Claude for this review.")
        self.finish(disallowed["task"], None, outcome="failed")
        nonreview = self.start(role="investigate", route="codex/sol/high", reason="test route")
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
        started = self.start(paths_=["src"], route="antigravity/gemini-4-neon/high", reason="test route")
        self.assertEqual(started["route"], "antigravity/gemini-4-neon/high")
        self.assertNotIn("route_mismatch", started)
        req = next(iter(self.orca()["requests"].values()))["receipt"]["launch"]["requested"]
        self.assertEqual(req, {"agent": "antigravity", "model": "gemini-4-neon-high", "effort": None})
        # Genuine mismatch on AGY (low instead of high) is still flagged.
        self.set_orca(effective={"agent": "antigravity", "model": "gemini-4-neon-low", "effort": None})
        mismatch = self.start(paths_=["docs"], route="antigravity/gemini-4-neon/high", reason="test route", worktree="new-child")
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
        rev = commands.dispatch(self.repo, None, role="review", title="rev", spec="Review.",
                                route="codex/sol/high", reason="test review")
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
                         "[widget:a3] review-fast · claude-sonnet-5-5/high · rev1")
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
                         "[widget:a2] review-deep · claude-opus-5-5/high · opus review")
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

    def test_role_only_dispatch_uses_default_profiles_and_skips_author_family_for_review(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/low: enabled\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "profiles:\n"
            "  build-fast: implement codex/gpt-6.2-sol/medium antigravity/gemini-4-neon/medium\n"
            "  build-deep: implement antigravity/gemini-4-neon/high codex/gpt-6.2-sol/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
            "  scout: investigate+mcp antigravity/gemini-4-neon/low codex/gpt-6.2-sol/medium\n"
        )
        self.write_routes(cfg)
        # No --profile and no --route -> implement->build-fast, investigate->scout, review->review-fast.
        built = commands.dispatch(self.repo, None, role="implement", title="ship feature", spec="Build.", paths_=["src/"])
        self.assertEqual((built["profile"], built["route"]), ("build-fast", "codex/gpt-6.2-sol/medium"))
        scout = commands.dispatch(self.repo, None, role="investigate", title="check callers", spec="Read.")
        self.assertEqual((scout["profile"], scout["mcp"], scout["route"]),
                         ("scout", True, "antigravity/gemini-4-neon/low"))
        commit = self.commit({"src/a.py": "openai\n"})
        self.finish(built["task"], {"outcome": "succeeded", "commit": commit})
        commands.accept(self.repo, None, built["assignment"], integrated=commit)
        # Role-only review uses review-fast + review_agents and skips OpenAI (author family) -> antigravity!
        rev = commands.dispatch(self.repo, None, role="review", title="audit change", spec="Review.")
        self.assertEqual((rev["profile"], rev["route"], rev["review_independence"]),
                         ("review-fast", "antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        self.assertTrue(any("authored this diff" in line for line in rev["trace"]))
        # When no independent reviewer remains for a non-quota reason (other-family routes disabled), role-only review refuses unless given --words/--after.
        self.write_routes(
            cfg.replace("antigravity/gemini-4-neon/high: enabled", "antigravity/gemini-4-neon/high: disabled")
               .replace("claude/claude-opus-5-5/high: enabled", "claude/claude-opus-5-5/high: disabled")
        )
        self.refused("reviewer_same_family", commands.dispatch, self.repo, None,
                     role="review", title="same family blocked", spec="Review.")
        # Dry-run rejects malformed review exception flags and notes unchecked prior failure for --after/--why.
        for bad_flags in ({"words": "   "}, {"after": "a999", "why": "missing"}, {"after": rev["assignment"]},
                          {"why": "orphan"}, {"words": "ok", "after": rev["assignment"], "why": "both"}):
            with self.subTest(bad_flags=bad_flags):
                bad_dry = commands.dispatch(self.repo, None, role="review", dry_run=True, **bad_flags)
                self.assertFalse(bad_dry["ok"])
                self.assertEqual(bad_dry["refused"]["code"], "review_exception_invalid")
        ok_after_dry = commands.dispatch(self.repo, None, role="review", dry_run=True,
                                         after=rev["assignment"], why="first review failed")
        self.assertTrue(ok_after_dry["ok"])
        self.assertIn("not verified against Orca", ok_after_dry["note"])
        exc_rev = commands.dispatch(self.repo, None, role="review", title="same family override", spec="Review.",
                                    words="Only Codex is available; run the review on Codex.")
        self.assertEqual((exc_rev["route"], exc_rev["review_independence"]),
                         ("codex/gpt-6.1-terra/high", "same_provider"))
        self.assertEqual(self.entries("widget")[-2]["review_exception"],
                         {"kind": "words", "words": "Only Codex is available; run the review on Codex."})
        # Configurable default_profiles in config.yaml overrides the role default.
        self.write_routes(cfg + "default_profiles:\n  implement: build-deep\n")
        dry = commands.dispatch(self.repo, None, role="implement", dry_run=True)
        self.assertEqual((dry["ok"], dry["profile"], dry["route"]), (True, "build-deep", "antigravity/gemini-4-neon/high"))

    def test_route_outside_every_profile_requires_reason_and_records_it_on_assignment(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  codex/gpt-6.2-sol/ultra: enabled\n"
            "preferred: null\npinned: null\n"
            "profiles:\n"
            "  build-fast: implement antigravity/gemini-4-neon/medium\n"
        )
        self.write_routes(cfg)
        entries_before = self.entries("widget")
        self.refused("route_outside_profiles", commands.dispatch, self.repo, None,
                     role="implement", route="codex/gpt-6.2-sol/ultra", title="deep dive", spec="Do it.", paths_=["src/"])
        self.assertEqual(self.entries("widget"), entries_before)
        self.assertFalse(self.orca_file.exists())
        # Empty/absent profiles AND route == Pin still require --reason for explicit --route.
        self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/sol/high"))
        self.refused("route_outside_profiles", commands.dispatch, self.repo, None,
                     role="implement", route="codex/sol/high", title="pinned", spec="Do it.", paths_=["src/"])
        pinned_ok = commands.dispatch(self.repo, None, role="implement", route="codex/sol/high",
                                      reason="explicit pinned route", title="pinned", spec="Do it.", paths_=["pin/"])
        self.assertEqual(pinned_ok["reason"], "explicit pinned route")
        self.assertEqual(self.entries("widget")[-2]["reason"], "explicit pinned route")
        # Supplying --reason through CLI succeeds and persists reason on the assignment row.
        self.write_routes(cfg)
        args = cli.parser().parse_args([
            "dispatch", "--role", "implement", "--route", "codex/gpt-6.2-sol/ultra",
            "--reason", "benchmark verification requires ultra",
            "--title", "deep dive", "--spec", "Do it.", "--paths", "src/", "--worktree", "new-child",
        ])
        started = cli.execute(args, self.repo)
        self.assertEqual((started["route"], started["reason"]),
                         ("codex/gpt-6.2-sol/ultra", "benchmark verification requires ultra"))
        initial = self.entries("widget")[-2]
        self.assertEqual(initial["id"], started["assignment"])
        self.assertEqual(initial["reason"], "benchmark verification requires ultra")
        # Route inside a configured profile needs no --reason.
        in_profile = commands.dispatch(self.repo, None, role="implement", route="antigravity/gemini-4-neon/medium",
                                       title="normal", spec="Do it.", paths_=["docs/"], worktree="new-child")
        self.assertNotIn("reason", in_profile)
        self.assertNotIn("reason", self.entries("widget")[-2])

    def test_review_same_family_quota_fallback_end_to_end(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/medium: enabled\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "  claude/claude-opus-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "profiles:\n"
            "  build-fast: implement codex/gpt-6.2-sol/medium antigravity/gemini-4-neon/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high claude/claude-opus-5-5/high\n"
        )
        self.write_routes(cfg)
        built = commands.dispatch(self.repo, None, role="implement", title="ship feature", spec="Build.", paths_=["src/"])
        commit = self.commit({"src/a.py": "openai\n"})
        self.finish(built["task"], {"outcome": "succeeded", "commit": commit})
        commands.accept(self.repo, None, built["assignment"], integrated=commit)

        # Pause antigravity for quota -> dry-run and real dispatch fall back to Codex tagged same_provider_fallback.
        self.write_routes(cfg + "paused_agents:\n  antigravity: quota\n")
        dry = commands.dispatch(self.repo, None, role="review", dry_run=True)
        self.assertEqual((dry["ok"], dry["route"], dry["review_independence"]),
                         (True, "codex/gpt-6.1-terra/high", "same_provider_fallback"))
        self.assertIn("antigravity", dry["review_fallback_reason"])

        rev = commands.dispatch(self.repo, None, role="review", title="quota fallback review", spec="Review.")
        self.assertEqual((rev["route"], rev["review_independence"]),
                         ("codex/gpt-6.1-terra/high", "same_provider_fallback"))
        self.assertIn("antigravity", rev["review_fallback_reason"])
        assignment_row = state.fold(self.entries("widget")).assignments[rev["assignment"]]
        self.assertEqual(assignment_row["id"], rev["assignment"])
        self.assertEqual(assignment_row["review_independence"], "same_provider_fallback")
        self.assertIn("antigravity", assignment_row["review_fallback_reason"])
        self.assertFalse(any(step["route"] == rev["route"] and step["status"] == "skipped" for step in dry["trace"]))

        # Even if antigravity is unpaused before accept, the accepted review fact keeps same_provider_fallback + reason.
        self.write_routes(cfg)
        self.finish(rev["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        accepted = commands.accept(self.repo, None, rev["assignment"])
        self.assertEqual((accepted["verdict"], accepted["review_independence"]), ("pass", "same_provider_fallback"))
        self.assertIn("antigravity", accepted["review_fallback_reason"])
        review_fact = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(review_fact["review_independence"], "same_provider_fallback")
        self.assertIn("antigravity", review_fact["review_fallback_reason"])

        # Once antigravity is back, new reviews return to cross-family.
        cross = commands.dispatch(self.repo, None, role="review", title="cross family again", spec="Review.")
        self.assertEqual((cross["route"], cross["review_independence"]),
                         ("antigravity/gemini-4-neon/high", "cross_provider_fallback"))
        self.assertNotIn("review_fallback_reason", cross)

        # Explicit review exceptions (--words and --after/--why) keep working in the quota-fallback case (dry-run & dispatch).
        self.finish(cross["task"], {"outcome": "failed"}, outcome="failed")
        commands.accept(self.repo, None, cross["assignment"])
        self.write_routes(
            cfg.replace("claude/claude-opus-5-5/high: enabled", "claude/claude-opus-5-5/high: disabled")
            + "paused_agents:\n  antigravity: quota\n"
        )
        dry_words = commands.dispatch(self.repo, None, role="review", dry_run=True, words="Use Codex.")
        self.assertEqual((dry_words["ok"], dry_words["route"]), (True, "codex/gpt-6.1-terra/high"))
        dry_after = commands.dispatch(self.repo, None, role="review", dry_run=True,
                                      after=cross["assignment"], why="Gemini failed")
        self.assertEqual((dry_after["ok"], dry_after["route"]), (True, "codex/gpt-6.1-terra/high"))
        bad_exc = commands.dispatch(self.repo, None, role="review", dry_run=True, words="   ")
        self.assertEqual((bad_exc["ok"], bad_exc["refused"]["code"]), (False, "review_exception_invalid"))
        exc_after = commands.dispatch(self.repo, None, role="review", title="explicit after", spec="Review.",
                                      after=cross["assignment"], why="Gemini failed")
        self.assertEqual(exc_after["route"], "codex/gpt-6.1-terra/high")
        self.assertEqual(state.fold(self.entries("widget")).assignments[exc_after["assignment"]]["review_exception"],
                         {"kind": "after", "after": cross["assignment"], "why": "Gemini failed"})
        exc_words = commands.dispatch(self.repo, None, role="review", title="explicit words", spec="Review.",
                                      words="Run on Codex.")
        self.assertEqual(exc_words["route"], "codex/gpt-6.1-terra/high")
        self.assertEqual(state.fold(self.entries("widget")).assignments[exc_words["assignment"]]["review_exception"],
                         {"kind": "words", "words": "Run on Codex."})

        # If both codex and antigravity are paused, only Claude remains -> refused (Claude is never chosen).
        self.write_routes(cfg + "paused_agents: [codex, antigravity]\n")
        self.refused("no_eligible_route", commands.dispatch, self.repo, None,
                     role="review", title="all paused", spec="Review.")

    def test_review_fallback_proof_binds_to_observed_effective_launch_and_retry(self):
        cfg = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "profiles:\n"
            "  build-fast: implement codex/gpt-6.2-sol/medium\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg)
        built = commands.dispatch(self.repo, None, role="implement", title="build", spec="Build.", paths_=["src/"])
        commit = self.commit({"src/a.py": "openai\n"})
        self.finish(built["task"], {"outcome": "succeeded", "commit": commit})
        commands.accept(self.repo, None, built["assignment"], integrated=commit)

        # Pause antigravity so Pod requests Codex fallback, but Orca actually launches Gemini.
        self.write_routes(cfg + "paused_agents:\n  antigravity: quota\n")
        self.set_orca(effective={"agent": "antigravity", "model": "gemini-4-neon-high", "effort": None})
        mismatched = commands.dispatch(self.repo, None, role="review", title="mismatch review", spec="Review.")
        self.assertEqual(mismatched["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", mismatched)
        self.assertIn("route_mismatch", mismatched)
        row1 = state.fold(self.entries("widget")).assignments[mismatched["assignment"]]
        self.assertEqual(row1["effective"], "antigravity/gemini-4-neon/high")
        self.assertEqual(row1["review_independence"], "cross_provider_fallback")
        self.assertIsNone(row1.get("review_fallback_reason"))
        self.finish(mismatched["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc1 = commands.accept(self.repo, None, mismatched["assignment"])
        self.assertEqual(acc1["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", acc1)
        fact1 = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(fact1["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", fact1)

        # Lost-reply + --retry must also read receipt.launch.effective and bind proof to Gemini, not Codex fallback.
        self.set_orca(lose_reply=True, effective={"agent": "antigravity", "model": "gemini-4-neon-high", "effort": None})
        problem = self.refused("orca_unknown", commands.dispatch, self.repo, None,
                               role="review", title="lost reply review", spec="Review.")
        retry_id = problem.fix.split("--retry ")[1].split("`")[0]
        settled = commands.dispatch(self.repo, None, retry=retry_id)
        self.assertEqual(settled["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", settled)
        self.assertIn("route_mismatch", settled)
        row2 = state.fold(self.entries("widget")).assignments[retry_id]
        self.assertEqual(row2["effective"], "antigravity/gemini-4-neon/high")
        self.assertEqual(row2["review_independence"], "cross_provider_fallback")
        self.assertIsNone(row2.get("review_fallback_reason"))
        self.finish(settled["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc2 = commands.accept(self.repo, None, retry_id)
        self.assertEqual(acc2["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", acc2)
        fact2 = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(fact2["review_independence"], "cross_provider_fallback")
        self.assertNotIn("review_fallback_reason", fact2)
        # Recovered start with known effective route also renames its default worker-task_* tab title.
        self.assertEqual(self.orca()["titles"][f"term_{settled['dispatch']}"],
                         "Reviewer (fast) · lost reply review · neon/high")

        # Lost reply where Orca receipt is absent (effective route unknown) -> NO fallback proof recorded.
        self.set_orca(lose_reply=True, effective={"agent": "antigravity", "model": "gemini-4-neon-high", "effort": None})
        problem_nr = self.refused("orca_unknown", commands.dispatch, self.repo, None,
                                  role="review", title="no receipt review", spec="Review.")
        nr_id = problem_nr.fix.split("--retry ")[1].split("`")[0]
        # Remove the receipt from fake_orca so --retry recovers only via task/worker lookup with no effective route.
        self.set_orca(requests={})
        settled_nr = commands.dispatch(self.repo, None, retry=nr_id)
        self.assertNotIn("review_independence", settled_nr)
        self.assertNotIn("review_fallback_reason", settled_nr)
        row3 = state.fold(self.entries("widget")).assignments[nr_id]
        self.assertTrue(row3.get("effective_unknown"))
        self.assertNotIn("review_independence", row3)
        self.assertNotIn("review_fallback_reason", row3)
        self.finish(settled_nr["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc3 = commands.accept(self.repo, None, nr_id)
        self.assertNotIn("review_independence", acc3)
        self.assertNotIn("review_fallback_reason", acc3)
        fact3 = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertNotIn("review_independence", fact3)
        self.assertNotIn("review_fallback_reason", fact3)

        # Effective mismatch to an ineligible same-family Claude route NEVER keeps same_provider_fallback or quota reason.
        cfg_mixed = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  antigravity/claude-sonnet-5-5/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: enabled\n"
            "  claude/claude-sonnet-5-5/high: enabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "profiles:\n"
            "  build-fast: implement claude/claude-sonnet-5-5/high\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg_mixed)
        self.set_orca(effective=None)
        claude_impl = commands.dispatch(self.repo, None, role="implement", title="ui part", spec="UI.", paths_=["ui/"])
        commit2 = self.commit({"ui/b.py": "anthropic\n"})
        self.finish(claude_impl["task"], {"outcome": "succeeded", "commit": commit2})
        commands.accept(self.repo, None, claude_impl["assignment"], integrated=commit2)
        self.write_routes(cfg_mixed + "paused_agents:\n  agy-gemini: quota\n")
        self.set_orca(effective={"agent": "antigravity", "model": "claude-sonnet-5-5-high", "effort": None})
        bad_mismatch = commands.dispatch(self.repo, None, role="review", title="bad claude mismatch", spec="Review.")
        self.assertEqual(bad_mismatch["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", bad_mismatch)
        self.assertIn("route_mismatch", bad_mismatch)
        row4 = state.fold(self.entries("widget")).assignments[bad_mismatch["assignment"]]
        self.assertEqual(row4["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", row4)
        self.finish(bad_mismatch["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc4 = commands.accept(self.repo, None, bad_mismatch["assignment"])
        self.assertEqual(acc4["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", acc4)
        fact4 = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(fact4["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", fact4)

        # Same-family mismatch to a disabled route outside review-fast (codex/gpt-6.2-sol/medium) drops same_provider_fallback.
        cfg_disabled_sol = (
            "schema: pod/v2\nroutes:\n"
            "  antigravity/gemini-4-neon/high: enabled\n"
            "  codex/gpt-6.1-terra/high: enabled\n"
            "  codex/gpt-6.2-sol/medium: disabled\n"
            "preferred: null\npinned: null\n"
            "review_agents: [codex, antigravity]\n"
            "paused_agents:\n"
            "  agy-gemini: quota\n"
            "profiles:\n"
            "  review-fast: review codex/gpt-6.1-terra/high antigravity/gemini-4-neon/high\n"
        )
        self.write_routes(cfg_disabled_sol)
        self.set_orca(effective={"agent": "codex", "model": "gpt-6.2-sol", "effort": "medium"})
        disabled_mismatch = commands.dispatch(self.repo, None, role="review", title="disabled sol mismatch", spec="Review.")
        self.assertEqual(disabled_mismatch["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", disabled_mismatch)
        self.assertIn("route_mismatch", disabled_mismatch)
        row5 = state.fold(self.entries("widget")).assignments[disabled_mismatch["assignment"]]
        self.assertEqual(row5["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", row5)
        self.finish(disabled_mismatch["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        acc5 = commands.accept(self.repo, None, disabled_mismatch["assignment"])
        self.assertEqual(acc5["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", acc5)
        fact5 = [e for e in self.entries("widget") if e.get("fact") == "review"][-1]
        self.assertEqual(fact5["review_independence"], "same_provider")
        self.assertNotIn("review_fallback_reason", fact5)

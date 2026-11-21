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
        self.assertEqual(task["task_title"], "write [widget:a1]")
        self.assertIn("Change only: src", task["spec"])
        self.assertIn('"outcome"', task["spec"])
        row = {**self.entries("widget")[-2], **self.entries("widget")[-1]}
        self.assertEqual((row["route"], row["paths"], row["task"], row["dispatch"]),
                         ("codex/sol/high", ["src"], started["task"], started["dispatch"]))
        import hashlib
        self.assertEqual(row["packet"], hashlib.sha256(task["spec"].encode()).hexdigest())
        self.assertNotIn("spec", row)                # the task text is Orca's; Pod keeps its digest

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

    def test_one_writer_per_overlapping_path_set_at_a_time(self):
        first = self.start(paths_=["src/app"])
        self.refused("paths_overlap", self.start, paths_=["src"])
        self.refused("paths_overlap", self.start, paths_=["src/app/main.py"])
        self.refused("paths_overlap", self.start)                         # no declared paths means the whole tree
        self.start(paths_=["docs"])
        self.finish(first["task"], {"outcome": "succeeded"})
        self.assertEqual(self.start(paths_=["src"])["assignment"], "a3")
        self.assertEqual(self.orca()["starts"], 3)

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
        problem = self.refused("consumer_fenced", self.start, paths_=["docs"])
        self.assertIn("pod resume", problem.fix)
        self.assertEqual(self.orca()["starts"], 1)
        self.assertTrue(self.entries("widget")[-1]["void"])
        commands.resume(self.repo, None)
        self.refused("paths_overlap", self.start, paths_=["src"])          # it sees the first coordinator's worker
        self.assertEqual(self.start(paths_=["docs"])["assignment"], "a3")
        os.environ["FAKE_ORCA_CALLER"] = "term_one"
        self.refused("consumer_fenced", self.start, paths_=["tests"])      # the earlier conversation is fenced now
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

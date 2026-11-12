import os

from pod import commands, orca
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

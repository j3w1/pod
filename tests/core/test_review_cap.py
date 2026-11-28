"""j3w1's two-round rule: after two failed review rounds, a third waits for a re-scope or j3w1's decision."""

from pod import cli, commands
from tests.core.support import CoreCase, ROUTES

WORDS = "Ship the smaller cut."


class ReviewCapTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.opened()

    def rounds(self):
        return commands.status(self.repo, None, remote=False)["review_rounds"]

    def failed_review(self, **result):
        return self.review("fail", [{"path": "a.py", "required": True, "text": "no"}], **result)

    def change(self, text):
        self.commit({"a.py": text})

    def test_distinct_failed_targets_count_and_a_pass_does_not_reset(self):
        self.assertEqual(self.rounds(), {"failed": 0, "cap": 2, "rescope_left": 1})
        self.change("a\n")
        self.failed_review()
        self.failed_review()                                  # a second reviewer failing the same commit is the same round
        self.assertEqual(self.rounds()["failed"], 1)
        self.change("b\n")
        self.review("pass")
        self.assertEqual(self.rounds()["failed"], 1)
        self.change("c\n")
        self.failed_review()
        self.assertEqual(self.rounds(), {"failed": 2, "cap": 2, "rescope_left": 1})

    def test_reviews_that_cover_nothing_never_count(self):
        self.change("a\n")
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", route="codex/sol/high",
                                    reason="r")
        self.finish(started["task"], {"outcome": "failed", "verdict": "fail", "findings": []}, outcome="failed")
        commands.accept(self.repo, None, started["assignment"])                  # did not complete
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", route="codex/sol/high",
                                    reason="r")
        wrote = self.commit({"b.py": "b\n"})
        self.finish(started["task"], {"outcome": "succeeded", "verdict": "fail", "commit": wrote, "findings": []})
        commands.accept(self.repo, None, started["assignment"])                  # the reviewer changed files
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", route="codex/sol/high",
                                    reason="r")
        self.finish(started["task"], {"outcome": "succeeded", "summary": "no verdict"})
        commands.accept(self.repo, None, started["assignment"])
        self.assertEqual(self.rounds()["failed"], 0)

    def test_at_the_cap_a_review_waits_for_one_re_scope_then_only_for_j3w1s_words(self):
        for text in ("a\n", "b\n"):
            self.change(text)
            self.failed_review()
        before = self.entries("widget")
        refusal = self.refused("review_cap", self.review)
        self.assertIn("--scope", refusal.fix)
        self.assertIn("--words", refusal.fix)
        self.assertEqual(self.entries("widget"), before)
        commands.record(self.repo, None, scope="Cut it to the parser change.")
        self.assertEqual(self.rounds(), {"failed": 0, "cap": 2, "rescope_left": 0})
        for text in ("c\n", "d\n"):
            self.change(text)
            self.failed_review()
        commands.record(self.repo, None, scope="Cut again.")                     # the one re-scope is used
        commands.record(self.repo, None, decision="Keep going.")                 # no words: not j3w1's decision
        refusal = self.refused("review_cap", self.review)
        self.assertNotIn("--scope", refusal.fix)
        self.assertIn("only j3w1", refusal.fix)
        commands.record(self.repo, None, decision="Ship the smaller cut.", words=WORDS)
        self.assertEqual(self.rounds(), {"failed": 0, "cap": 2, "rescope_left": 0})
        self.assertEqual(self.review()["verdict"], "pass")

    def test_a_scope_before_the_cap_neither_resets_nor_uses_the_re_scope(self):
        self.change("a\n")
        self.failed_review()
        commands.record(self.repo, None, scope="Narrower.")
        self.assertEqual(self.rounds(), {"failed": 1, "cap": 2, "rescope_left": 1})

    def test_the_count_follows_the_source_issue_across_objectives(self):
        self.issue(5, "Make the widget", "## Done when\n- it works\n")
        commands.drop(self.repo, None, why="replaced by the issue objective")
        commands.open_(self.repo, source="5", objective="first")
        for text in ("a\n", "b\n"):
            self.change(text)
            self.failed_review()
        commands.drop(self.repo, "first", why="reopen it")
        commands.open_(self.repo, source="5", objective="second")
        self.assertEqual(commands.status(self.repo, "second", remote=False)["review_rounds"]["failed"], 2)
        self.refused("review_cap", commands.dispatch, self.repo, "second", role="review", title="r", spec="Review.",
                     route="codex/sol/high", reason="r")

    def test_a_failed_review_in_the_implementers_session_never_counts(self):
        import json
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.")
        one = self.commit({"a.py": "a\n"})
        self.finish(first["task"], {"outcome": "succeeded", "commit": one})
        commands.accept(self.repo, None, "a1", integrated=one)
        review = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", route="codex/sol/high",
                                   reason="r")
        state = self.orca()
        state["workers"][review["dispatch"]]["agentTerminalHandle"] = state["workers"][first["dispatch"]]["agentTerminalHandle"]
        self.orca_file.write_text(json.dumps(state))
        self.finish(review["task"], {"outcome": "succeeded", "verdict": "fail", "findings": []})
        commands.accept(self.repo, None, review["assignment"])
        self.assertEqual(self.rounds()["failed"], 0)

    def test_facts_in_the_same_second_replay_in_objective_order_across_a_reopen(self):
        from unittest import mock
        from pod import log
        self.issue(5, "Make the widget", "## Done when\n- it works\n")
        commands.drop(self.repo, None, why="replaced by the issue objective")
        with mock.patch.object(log, "now", return_value="2026-11-29T01:00:00Z"):
            commands.open_(self.repo, source="5", objective="first")
            for text in ("a\n", "b\n"):
                self.change(text)
                self.failed_review()
            commands.drop(self.repo, "first", why="reopen it")
            commands.open_(self.repo, source="acme/Widgets#5", objective="second")     # the same issue, other case
            commands.record(self.repo, "second", scope="Cut it.")
        self.assertEqual(commands.status(self.repo, "second", remote=False)["review_rounds"],
                         {"failed": 0, "cap": 2, "rescope_left": 0})

    def test_a_scope_keeps_the_meaning_it_had_when_recorded(self):
        self.write_routes(ROUTES + "review_cap: 3\n")
        for text in ("a\n", "b\n"):
            self.change(text)
            self.failed_review()
        commands.record(self.repo, None, scope="Narrower.")                     # 2 of 3: no reset
        self.write_routes(ROUTES)                                                # the cap goes back to 2
        self.assertEqual(self.rounds(), {"failed": 2, "cap": 2, "rescope_left": 1})

    def test_a_configured_cap_refuses_at_its_own_limit(self):
        self.write_routes(ROUTES + "review_cap: 3\n")
        for text in ("a\n", "b\n"):
            self.change(text)
            self.failed_review()
        self.change("c\n")
        self.failed_review()                                                     # the third round still runs
        self.refused("review_cap", self.review)

    def test_blank_words_are_not_j3w1s_decision(self):
        for text in ("a\n", "b\n"):
            self.change(text)
            self.failed_review()
        commands.record(self.repo, None, decision="Go on.", words="   ")
        self.assertEqual(self.rounds()["failed"], 2)

    def test_a_dropped_objective_still_shows_its_rounds(self):
        self.change("a\n")
        self.failed_review()
        commands.drop(self.repo, None, why="done with it")
        self.assertEqual(self.rounds()["failed"], 1)

    def test_the_cap_is_j3w1s_config_value_shown_by_pod_config(self):
        self.assertEqual(cli._config(False)["review_cap"], 2)
        self.write_routes(ROUTES + "review_cap: 3\n")
        self.assertEqual(cli._config(False)["review_cap"], 3)
        self.assertEqual(self.rounds()["cap"], 3)
        for bad in ("0", "21", "two", "true"):
            self.write_routes(ROUTES + f"review_cap: {bad}\n")
            self.assertEqual(cli._config(False)["status"], "invalid", bad)
            self.assertEqual(cli._config(False)["review_cap"], 2)


class ExceptionMarkerTests(CoreCase):
    def test_a_review_under_a_recorded_exception_shows_a_flag_on_its_tab(self):
        self.opened()
        self.commit({"src/a.py": "a\n"})
        self.write_routes(ROUTES + "review_agents: [codex]\n")
        args = cli.parser().parse_args(["dispatch", "--role", "review", "--title", "review", "--spec", "Review.",
                                       "--words", "Please use Claude for this review."])
        cli.execute(args, self.repo)
        self.assertTrue(self.orca()["rename_calls"][-1]["title"].startswith("⚑ Reviewer"))
        commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.")
        self.assertFalse(self.orca()["rename_calls"][-1]["title"].startswith("⚑"))

"""`pod dispatch --follow-up`: the next step of the same job goes to the same worker and is its own assignment."""

from tests.core.support import CoreCase


class FollowUpTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.opened()

    def started(self, role="investigate"):
        from pod import commands
        return commands.dispatch(self.repo, None, role=role, title="look", spec="Look at it.",
                                 **({"reason": "test review", "route": "codex/sol/high"} if role == "review" else {}))

    def follow(self, parent: str, **options):
        from pod import commands
        return commands.dispatch(self.repo, None, follow_up=parent, spec=options.pop("spec", "Now the next step."), **options)

    def test_a_follow_up_goes_to_the_same_dispatch_and_is_accepted_as_its_own_assignment(self):
        from pod import commands
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded", "summary": "first look"})
        commands.accept(self.repo, None, first["assignment"])
        then = self.follow(first["assignment"], title="look again")
        self.assertEqual((then["assignment"], then["follows"], then["delivery"]), ("a2", "a1", "follow_up"))
        orca = self.orca()
        self.assertEqual(orca["sends"][0]["to"], first["dispatch"])
        self.assertIn("# Assignment widget a2 (investigate)", orca["sends"][0]["body"])
        self.assertIn("[widget:a2]", orca["sends"][0]["subject"])
        self.assertNotEqual(then["dispatch"], first["dispatch"])
        self.assertEqual(orca["workers"][then["dispatch"]]["agentTerminalHandle"],
                         orca["workers"][first["dispatch"]]["agentTerminalHandle"])
        row = [entry for entry in self.entries("widget") if entry.get("id") == "a2" and entry.get("role")][0]
        self.assertEqual((row["role"], row["route"], row["follows"]), ("investigate", "claude/opus/high", "a1"))
        self.finish(then["task"], {"outcome": "succeeded", "summary": "second look"})
        done = commands.accept(self.repo, None, "a2")
        self.assertEqual((done["decision"], done["summary"]), ("read", "second look"))
        results = {entry["assignment"]: entry["summary"] for entry in self.entries("widget") if entry.get("fact") == "result"}
        self.assertEqual(results, {"a1": "first look", "a2": "second look"})
        self.assertIn("a2 investigate (follows a1) accepted (read, succeeded): look again",
                      commands.status(self.repo, None, remote=False)["assignments"])
        typed = commands.status(self.repo, None, remote=False, typed=True)["assignment_records"]
        self.assertEqual([row.get("follows") for row in typed], [None, "a1"])

    def test_a_follow_up_may_follow_a_follow_up_and_need_not_wait_for_accept(self):
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded", "summary": "one"})
        second = self.follow(first["assignment"])
        self.finish(second["task"], {"outcome": "succeeded", "summary": "two"})
        third = self.follow("a2")
        self.assertEqual(self.orca()["sends"][1]["to"], second["dispatch"])
        self.assertEqual(third["follows"], "a2")

    def test_an_implement_follow_up_diffs_from_its_parent_result_and_keeps_its_paths(self):
        from pod import commands
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"])
        one = self.commit({"src/a.py": "a\n"})
        self.finish(first["task"], {"outcome": "succeeded", "commit": one, "summary": "a"})
        commands.accept(self.repo, None, "a1", integrated=one)
        then = self.follow("a1")
        row = [entry for entry in self.entries("widget") if entry.get("id") == "a2" and entry.get("role")][0]
        self.assertEqual((row["base"], row["paths"]), (one, ["src"]))
        self.assertIn("- Change only: src", self.orca()["sends"][0]["body"])
        two = self.commit({"src/b.py": "b\n"})
        self.finish(then["task"], {"outcome": "succeeded", "commit": two, "summary": "b"})
        done = commands.accept(self.repo, None, "a2", integrated=two)
        result = [entry for entry in self.entries("widget") if entry.get("fact") == "result" and entry["assignment"] == "a2"][0]
        self.assertEqual(result["paths"], ["src/b.py"])
        self.assertNotIn("outside_declared_paths", done)

    def test_an_implement_follow_up_waits_while_another_implementer_works_in_its_worktree(self):
        from pod import commands
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"])
        self.finish(first["task"], {"outcome": "succeeded"})
        commands.accept(self.repo, None, "a1", discarded=True)
        commands.dispatch(self.repo, None, role="implement", title="other", spec="Other.", paths_=["docs"])
        self.refused("worktree_busy", self.follow, "a1")
        self.assertNotIn("sends", self.orca())

    def test_an_unaccepted_implement_parent_needs_its_commit_named(self):
        from pod import commands
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"])
        one = self.commit({"src/a.py": "a\n"})
        self.finish(first["task"], {"outcome": "succeeded", "commit": one})
        self.refused("follow_up_unaccepted", self.follow, "a1")
        self.assertNotIn("sends", self.orca())
        then = self.follow("a1", commit=one)
        two = self.commit({"src/b.py": "b\n"})
        self.finish(then["task"], {"outcome": "succeeded", "commit": two})
        commands.accept(self.repo, None, "a2", integrated=two)
        result = [entry for entry in self.entries("widget") if entry.get("fact") == "result" and entry["assignment"] == "a2"][0]
        self.assertEqual(result["paths"], ["src/b.py"])

    def test_only_the_newest_step_is_followed_and_orca_ancestry_is_reported(self):
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        second = self.follow("a1")
        self.finish(second["task"], {"outcome": "succeeded"})
        self.refused("follow_up_superseded", self.follow, "a1")
        self.assertEqual(len(self.orca()["sends"]), 1)
        state = self.orca()                     # a native follow-up of a2 sent outside Pod, already finished
        state["workers"]["ctx_native"] = {**state["workers"][second["dispatch"]], "dispatchId": "ctx_native",
                                          "taskId": "task_native", "followUpOf": second["dispatch"],
                                          "workerState": "succeeded", "dispatchStatus": "completed"}
        state["tasks"]["task_native"] = {"id": "task_native", "run_id": state["tasks"][second["task"]]["run_id"],
                                         "task_title": "native", "status": "completed", "result": None}
        self.orca_file.write_text(__import__("json").dumps(state))
        third = self.follow("a2")
        self.assertIn("Orca followed up Dispatch ctx_native", third["note"])

    def test_a_follow_up_in_a_made_worktree_waits_for_another_writer_there(self):
        from pod import commands
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=["src"],
                                  worktree="new-child")
        self.finish(first["task"], {"outcome": "succeeded"})
        commands.accept(self.repo, None, "a1", discarded=True)
        made = self.orca()["workers"][first["dispatch"]]["resource"]["worktreeId"].partition("::")[2]
        commands.dispatch(self.repo, None, role="implement", title="other", spec="Other.", paths_=["docs"],
                          worktree=f"path:{made}")
        self.refused("worktree_busy", self.follow, "a1")
        self.assertNotIn("sends", self.orca())

    def test_the_observed_launch_route_is_recorded(self):
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(follow_launch={"agent": "codex", "model": "sol", "effort": "high"})
        then = self.follow("a1")
        self.assertEqual(then["route_mismatch"], "asked for claude/opus/high, Orca started codex/sol/high")
        effective = [entry.get("effective") for entry in self.entries("widget") if entry.get("id") == "a2"]
        self.assertIn("codex/sol/high", effective)

    def test_a_review_of_an_implement_follow_up_in_the_same_terminal_covers_nothing(self):
        from pod import commands
        first = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.")
        one = self.commit({"a.py": "a\n"})
        self.finish(first["task"], {"outcome": "succeeded", "commit": one})
        commands.accept(self.repo, None, "a1", integrated=one)
        then = self.follow("a1")
        two = self.commit({"b.py": "b\n"})
        self.finish(then["task"], {"outcome": "succeeded", "commit": two})
        commands.accept(self.repo, None, "a2", integrated=two)
        review = commands.dispatch(self.repo, None, role="review", title="review", spec="Review it.",
                                   route="codex/sol/high", reason="test review")
        state = self.orca()                     # only the follow-up's own Dispatch shares the review's terminal
        state["workers"][first["dispatch"]]["agentTerminalHandle"] = "term_moved"
        state["workers"][review["dispatch"]]["agentTerminalHandle"] = state["workers"][then["dispatch"]]["agentTerminalHandle"]
        self.orca_file.write_text(__import__("json").dumps(state))
        self.finish(review["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        self.assertIn("same session", commands.accept(self.repo, None, review["assignment"])["note"])

    def test_a_review_never_follows_up_and_nothing_is_sent_or_recorded(self):
        self.commit({"a.py": "a\n"})
        review = self.started("review")
        self.finish(review["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        before = self.entries("widget")
        self.refused("follow_up_review", self.follow, review["assignment"])
        self.assertEqual(self.entries("widget"), before)
        self.assertNotIn("sends", self.orca())

    def test_a_worker_still_working_gets_no_mail_from_pod(self):
        first = self.started()
        self.refused("follow_up_busy", self.follow, first["assignment"])
        self.assertNotIn("sends", self.orca())
        self.assertNotIn("a2", {entry.get("id") for entry in self.entries("widget")})

    def test_a_follow_up_cannot_change_the_role_profile_route_or_worktree(self):
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        for flag in ({"role": "implement"}, {"profile": "x"}, {"route": "codex/sol/high"}, {"worktree": "new-child"},
                     {"paths_": ["src"]}, {"dry_run": True}):
            self.refused("follow_up_flags", self.follow, first["assignment"], **flag)
        self.refused("assignment_unknown", self.follow, "a9")
        self.assertNotIn("sends", self.orca())

    def test_an_orca_refusal_or_a_receipt_without_a_task_leaves_no_live_assignment(self):
        from pod import commands
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(send_refuses="dispatch_inactive")
        refusal = self.refused("dispatch_inactive", self.follow, first["assignment"])
        self.assertIn("fresh", refusal.fix)
        self.set_orca(send_refuses=None)
        state = self.orca()                               # Orca raced: the worker took the mail into a live turn
        state["tasks"][first["task"]]["status"] = "completed"
        self.orca_file.write_text(__import__("json").dumps(state))
        self.set_orca(send_as_mail=True)
        self.assertIn("fresh", self.refused("follow_up_not_started", self.follow, first["assignment"]).fix)
        self.assertEqual(len(self.orca()["sends"]), 1)
        listed = commands.listing()["objectives"][0]["unaccepted"]
        self.assertEqual([row["assignment"] for row in listed], ["a1"])
        self.refused("assignment_unknown", commands.accept, self.repo, None, "a2")
        self.refused("assignment_unknown", commands.accept, self.repo, None, "a3")

    def test_a_lost_reply_is_settled_from_orca_and_never_sent_twice(self):
        from pod import commands
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(lose_reply=True)
        self.refused("orca_unknown", self.follow, first["assignment"])
        settled = commands.dispatch(self.repo, None, retry="a2")
        self.assertEqual(len(self.orca()["sends"]), 1)
        self.assertEqual(settled["task"], [key for key, row in self.orca()["tasks"].items()
                                           if row["task_title"].startswith("[widget:a2]")][0])
        self.finish(settled["task"], {"outcome": "succeeded", "summary": "after the lost reply"})
        self.assertEqual(commands.accept(self.repo, None, "a2")["summary"], "after the lost reply")

    def test_a_retry_never_binds_a_task_by_its_title_and_keeps_the_follow_up_unknown(self):
        from pod import commands
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(fail="send")                  # the send never reached Orca
        self.refused("orca_unknown", self.follow, "a1")
        commands.dispatch(self.repo, None, role="investigate", title="[widget:a2] quoted", spec="Other.")
        self.refused("start_unknown", commands.dispatch, self.repo, None, retry="a2")
        row = [entry for entry in self.entries("widget") if entry.get("id") == "a2"]
        self.assertFalse(any(entry.get("task") for entry in row))

    def test_a_retry_settles_mail_and_fresh_starts_as_the_first_reply_does(self):
        from pod import commands
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(send_as_mail=True, lose_reply=True)
        self.refused("orca_unknown", self.follow, "a1")
        self.refused("follow_up_not_started", commands.dispatch, self.repo, None, retry="a2")
        self.assertTrue(commands.fold(self.entries("widget")).assignments["a2"]["void"])
        self.set_orca(send_as_mail=False, lose_reply=True, delivery="started_fresh")
        self.refused("orca_unknown", self.follow, "a1")
        settled = commands.dispatch(self.repo, None, retry="a3")
        self.assertEqual(settled["delivery"], "started_fresh")
        self.assertIn("context is gone", settled["note"])

    def test_a_fresh_start_is_recorded_and_says_the_context_is_gone(self):
        first = self.started()
        self.finish(first["task"], {"outcome": "succeeded"})
        self.set_orca(delivery="started_fresh")
        then = self.follow(first["assignment"])
        self.assertEqual(then["delivery"], "started_fresh")
        self.assertIn("context is gone", then["note"])


if __name__ == "__main__":
    import unittest
    unittest.main()

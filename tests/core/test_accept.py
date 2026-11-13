import json

from pod import commands
from tests.core.support import CoreCase


class AcceptTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.opened()

    def facts(self, kind):
        return [entry for entry in self.entries("widget") if entry.get("fact") == kind]

    def test_pod_reads_the_result_and_the_diff_and_the_coordinator_states_only_the_decision(self):
        commit, output = self.implement({"src/a.py": "a\n"}, paths=["src"], checks=[{"command": "make test", "exit": 0}])
        self.assertEqual((output["outcome"], output["decision"]), ("succeeded", "integrated"))
        result = self.facts("result")[0]
        self.assertEqual((result["commit"], result["paths"], result["integrated_at"], result["contained"]),
                         (commit, ["src/a.py"], commit, True))
        check = self.facts("check")[0]
        self.assertEqual((check["commit"], check["tree"], check["exit"], check["source"]),
                         (commit, self.git(self.repo, "rev-parse", "HEAD^{tree}"), 0, "reported by a1"))
        self.assertEqual(output["facts"], [result["n"], check["n"]])

    def test_paths_outside_the_declared_set_are_flagged(self):
        _, output = self.implement({"src/a.py": "a\n", "README.md": "changed\n"}, paths=["src"])
        self.assertEqual(output["outside_declared_paths"], ["README.md"])
        self.assertEqual(self.facts("result")[0]["outside"], ["README.md"])

    def test_a_decision_is_required_for_work_that_writes(self):
        started = commands.dispatch(self.repo, None, role="implement", title="w", spec="Do.")
        self.finish(started["task"], {"outcome": "succeeded"})
        self.refused("decision_required", commands.accept, self.repo, None, "a1")
        output = commands.accept(self.repo, None, "a1", discarded=True, why="superseded")
        self.assertEqual(output["decision"], "discarded")

    def test_a_result_is_never_recorded_before_the_worker_reports_and_is_recorded_once(self):
        started = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        self.refused("not_finished", commands.accept, self.repo, None, "a1")
        self.refused("assignment_unknown", commands.accept, self.repo, None, "a9")
        self.assertEqual(self.facts("result"), [])
        self.finish(started["task"], {"outcome": "succeeded", "summary": "found it"})
        self.assertEqual(commands.accept(self.repo, None, "a1")["summary"], "found it")
        count = len(self.entries("widget"))
        self.assertEqual(commands.accept(self.repo, None, "a1")["already"], True)
        self.assertEqual(len(self.entries("widget")), count)

    def test_what_was_not_observed_is_not_recorded_as_success(self):
        started = commands.dispatch(self.repo, None, role="implement", title="w", spec="Do.")
        head = self.commit({"x": "x\n"})
        self.finish(started["task"], None, text="All tests pass, trust me.")       # no result object, no commit named
        output = commands.accept(self.repo, None, "a1", integrated=head)
        self.assertIn("named no result commit", output["note"])
        result = self.facts("result")[0]
        self.assertEqual((result["commit"], result["paths"], result["contained"]), (None, [], False))
        self.assertEqual(self.facts("check"), [])
        failed = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        self.finish(failed["task"], {"outcome": "succeeded"}, outcome="failed")
        self.assertEqual(commands.accept(self.repo, None, "a2")["outcome"], "failed")   # Orca's outcome, not the claim

    def test_a_commit_that_does_not_hold_the_result_is_reported(self):
        started = commands.dispatch(self.repo, None, role="implement", title="w", spec="Do.")
        base = self.git(self.repo, "rev-parse", "HEAD")
        result = self.commit({"src/a.py": "worker\n"})
        self.git(self.repo, "reset", "-q", "--hard", base)
        elsewhere = self.commit({"src/a.py": "rewritten by hand\n"})
        self.finish(started["task"], {"outcome": "succeeded", "commit": result})
        output = commands.accept(self.repo, None, "a1", integrated=elsewhere)
        self.assertIn("does not contain", output["note"])
        self.assertFalse(self.facts("result")[0]["contained"])

    def test_a_review_becomes_a_review_fact_bound_to_the_reviewed_commit(self):
        commit, _ = self.implement({"src/a.py": "a\n"})
        output = self.review("fail", [{"path": "src/a.py", "required": True, "text": "off by one"},
                                      {"path": None, "required": False, "text": "naming"}])
        self.assertEqual(output["verdict"], "fail")
        self.assertEqual(output["findings"], ["a2.1 required src/a.py: off by one", "a2.2 advisory : naming"])
        review = self.facts("review")[0]
        self.assertEqual((review["commit"], review["paths"], review["reviewer"], review["wrote"]),
                         (commit, ["src/a.py"], "a2", False))

    def test_a_review_without_a_verdict_or_by_a_reviewer_who_changed_files_proves_nothing(self):
        self.implement({"src/a.py": "a\n"})
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        self.finish(started["task"], {"outcome": "succeeded", "summary": "looks good"})
        self.assertIn("no review is recorded", commands.accept(self.repo, None, "a2")["note"])
        self.assertEqual(self.facts("review"), [])
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        fixed = self.commit({"src/a.py": "fixed by the reviewer\n"})
        self.finish(started["task"], {"outcome": "succeeded", "verdict": "pass", "commit": fixed})
        self.assertIn("covers nothing", commands.accept(self.repo, None, "a3")["note"])
        self.assertTrue(self.facts("review")[0]["wrote"])

    def stop(self, task, worker="stopped"):
        """What Orca 1.4.251 shows after `orchestration worker-stop`: the Task is blocked at once; a proven stop settles
        the worker as stopped and fails its Dispatch, a pending or unproven one leaves it stopping or stop_unknown.
        An abandon also fails the Dispatch without proving that the process ended."""
        state = self.orca()
        state["tasks"][task]["status"] = "blocked"
        row = next(item for item in state["workers"].values() if item["taskId"] == task)
        row.update(workerState=worker, **({"dispatchStatus": "failed"} if worker in ("stopped", "abandoned") else {}))
        row["terminalState"] = "retained" if worker == "stopped" else row["terminalState"]
        self.orca_file.write_text(json.dumps(state))

    def test_a_worker_orca_shows_stopped_before_it_reported_can_be_discarded_and_its_output_never_used(self):
        self.implement({"src/a.py": "a\n"})
        review = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.", route="codex/sol/high")
        writer = commands.dispatch(self.repo, None, role="implement", title="w", spec="Do.", paths_=["src"])
        head = self.commit({"src/a.py": "half done\n"})
        for started in (review, writer):
            self.stop(started["task"])
        self.refused("not_finished", commands.accept, self.repo, None, "a2")
        self.refused("not_finished", commands.accept, self.repo, None, "a3", integrated=head)
        output = commands.accept(self.repo, None, "a2", discarded=True, why="stopped by j3w1's decision")
        self.assertEqual((output["decision"], output["outcome"]), ("discarded", "unknown"))
        self.assertEqual(self.facts("review"), [])
        output = commands.accept(self.repo, None, "a3", discarded=True)
        self.assertEqual((output["decision"], output["outcome"]), ("discarded", "unknown"))
        result = self.facts("result")[-1]
        self.assertEqual((result["assignment"], result["commit"], result["paths"]), ("a3", None, []))
        self.assertEqual(self.facts("check"), [])

    def test_a_blocked_task_or_a_stop_orca_has_not_proven_is_not_a_stopped_worker(self):
        held = commands.dispatch(self.repo, None, role="investigate", title="asks", spec="Look.")
        state = self.orca()
        state["tasks"][held["task"]]["status"] = "blocked"          # waiting on a question or a permission
        self.orca_file.write_text(json.dumps(state))
        for worker in ("stopping", "stop_unknown", "abandoned", "stopped"):
            last = commands.dispatch(self.repo, None, role="investigate", title=worker, spec="Look.")
            self.stop(last["task"], worker=worker)
        state = self.orca()
        del state["workers"][last["dispatch"]]                      # a5 was stopped, and Orca no longer lists its worker
        self.orca_file.write_text(json.dumps(state))
        for ident in ("a1", "a2", "a3", "a4", "a5"):
            self.refused("not_finished", commands.accept, self.repo, None, ident, discarded=True)
        self.set_orca(fail="worker-list")
        self.refused("orca_unknown", commands.accept, self.repo, None, "a3", discarded=True)
        self.assertEqual(self.facts("result"), [])

    def test_the_result_object_is_the_last_one_that_states_an_outcome(self):
        text = 'I ran {"x": 1} first.\n{"outcome": "failed"}\nthen fixed it\n{"outcome": "succeeded", "commit": null}\ntrailing {'
        self.assertEqual(commands.parse_result(text), {"outcome": "succeeded", "commit": None})
        self.assertEqual(commands.parse_result("no object here"), {})
        self.assertEqual(commands.parse_result(None), {})

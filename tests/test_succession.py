"""R110: a successor's claim ends a lost coordinator's write authority, never steals it."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import repository_context
from pod.internal import run
from pod.ledger import objective_root, read
from tests.test_owner_handoff import HandoffCase


class SuccessionCase(HandoffCase):
    def setUp(self):
        super().setUp()
        self.run_holders = {}

    def native_read(self, argv, **kwargs):
        if argv[:2] == ["orchestration", "run-show"]:
            self.native_calls.append(list(argv))
            return {"runtime": self.port.runtime, "result": {"run": {
                "id": argv[3], "coordinator_handle": self.run_holders.get(argv[3], "owner"),
                "consumer_generation": 1}}}
        return super().native_read(argv, **kwargs)

    def binding(self):
        context = repository_context(self.project)
        return {"repository": context["repository"], "repo_key": context["repo_key"],
                "path": context["worktree"], "branch": context["branch"]}

    def predecessor(self, *, settled=True):
        """The original objective, then its coordinator lost; its worker settled unless asked."""
        self.establish()
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        if settled:
            self.settle(self.admission)
        self.lose()

    def successor(self, name="successor", caller="next", run_id="run-s"):
        os.environ["ORCA_TERMINAL_HANDLE"] = caller
        self.current = {"id": run_id, "coordinator_handle": caller, "consumer_generation": 1}
        return run("checkpoint", {"project": str(self.project), "objective": name, "owner": caller,
                                  "value": self.core(obligations=[self.criterion()],
                                                     governance={"base_ref": "refs/remotes/origin/target"},
                                                     worktree=self.binding())})

    def claim(self, decision=None, *, name="successor", caller="next", predecessor="objective"):
        request = {"project": str(self.project), "objective": name, "owner": caller, "predecessor": predecessor}
        if decision is not None:
            request["decision"] = decision
        return run("succession", request)

    def decide(self, **changes):
        facts = self.claim()["facts"]
        return {"provenance": "user_direct", "instruction": "Continue the interrupted objective",
                **facts, **changes}

    def returning(self):
        """The predecessor's own coordinator comes back on its original Run."""
        self.terminals["owner"] = "live"
        os.environ["ORCA_TERMINAL_HANDLE"] = "owner"
        self.current = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}


class ClaimTests(SuccessionCase):
    def test_detection_writes_nothing_and_a_matching_decision_claims(self):
        self.predecessor(); self.successor()
        before = self.snapshot()
        verdict = self.claim()
        self.assertEqual(verdict["status"], "claim_available", verdict)
        self.assertEqual(verdict["facts"], {"predecessor": "objective", "predecessor_owner": "owner",
                                            "successor": "successor", "successor_owner": "next",
                                            "runs": ["run"], "ambiguity": []})
        for identity in ("owner", "next", "run-s"):  # The plain question names objectives, never handles or Runs.
            self.assertNotIn(identity, verdict["question"])
        self.assertEqual(before, self.snapshot())
        own_before = deepcopy(self.state())
        result = self.claim(self.decide())
        self.assertEqual((result["status"], result["replayed"]), ("claimed", False))
        after = self.state()
        link = after.pop("continued_by")
        self.assertEqual((link["objective"], link["owner"], link["runs"], link["orca_claim"]),
                         ("successor", "next", ["run"], "unavailable"))
        self.assertEqual(link["unfinished"], ["O1", "S1", "S2"])
        self.assertEqual(link["unreported"], [self.admission["admission_id"]])
        own_before["revision"] += 1
        self.assertEqual(after, own_before)  # Owner, admissions, Runs and evidence are kept as recorded.
        successor = read(self.project, "successor")
        self.assertEqual(successor["continues"]["objective"], "objective")
        self.assertEqual(successor["continues"]["record"], objective_root(self.project, "objective").name)

    def test_never_steals_and_holds_on_unproven_facts(self):
        cases = {
            "owner-live": ("predecessor_live", lambda case: case.terminals.update(owner="live")),
            "owner-unreadable": ("predecessor_unverified", lambda case: case.terminals.update(owner="unreadable")),
            "run-held": ("predecessor_run_held", lambda case: case.run_holders.update(run="third")),
            "worker-unsettled": ("predecessor_work_live", None),
            "workers-unreadable": ("predecessor_unverified", lambda case: setattr(case, "worker_list_failure", True)),
        }
        for kind, (condition, arrange) in cases.items():
            case = SuccessionCase(); case.setUp()
            try:
                with self.subTest(kind=kind):
                    case.predecessor(settled=kind != "worker-unsettled"); case.successor()
                    if arrange is not None:
                        arrange(case)
                    before = case.snapshot()
                    verdict = case.claim()
                    self.assertEqual((verdict["status"], verdict["condition"]), ("claim_unavailable", condition))
                    decision = {"provenance": "user_direct", "instruction": "Continue it", **verdict["facts"]}
                    error = case.refused(lambda: case.claim(decision), code=condition)
                    self.assertEqual(error.detail["succession"]["condition"], condition)
                    self.assertEqual(before, case.snapshot())
            finally:
                case.doCleanups()

    def test_stale_or_indirect_decisions_and_unknown_predecessors_write_nothing(self):
        self.predecessor(); self.successor()
        decision = self.decide()
        before = self.snapshot()
        for changes in ({"runs": ["other"]}, {"predecessor_owner": "someone"}, {"ambiguity": ["x"]}):
            with self.subTest(changes=changes):
                self.refused(lambda: self.claim({**decision, **changes}), code="succession_decision_mismatch")
        self.refused(lambda: self.claim({**decision, "provenance": "coordinator"}), code="succession_decision_mismatch")
        self.refused(lambda: self.claim(predecessor="missing"), code="predecessor_unknown")
        self.assertFalse(objective_root(self.project, "missing").exists())
        self.refused(lambda: self.claim(name="never-recorded"), code="successor_unrecorded")
        self.refused(lambda: self.claim(predecessor="successor"), code="predecessor_unknown")
        self.assertEqual(before, self.snapshot())

    def test_a_successor_that_started_work_cannot_claim(self):
        self.predecessor(); self.successor()
        run("governor-prepare", {"project": str(self.project), "objective": "successor", "owner": "next",
                                 "unit": "default", "branch": {"remote": "origin", "branch": "next", "base": "target"}})
        verdict = self.claim()
        self.assertEqual(verdict["condition"], "successor_started")


class AfterClaimTests(SuccessionCase):
    def claimed(self):
        self.predecessor(); self.successor()
        self.claim(self.decide())

    def test_every_write_of_the_returning_coordinator_is_refused_by_name(self):
        self.claimed()
        self.returning()
        before = self.snapshot()
        calls = dict(self.boundary_calls("owner"))
        calls["runtime-continuity"] = lambda: self.op("runtime-continuity", decision={
            "provenance": "user_direct", "instruction": "rebind", "objective": "objective",
            "recorded_runtime": "runtime", "current_runtime": "runtime-new", "ambiguity": ["x"]})
        for name, call in calls.items():
            with self.subTest(boundary=name):
                error = self.refused(call, code="objective_continued")
                self.assertEqual(error.detail["successor"], "successor")
                self.assertIn("pod status --objective successor", error.detail["next_action"])
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.terminals["owner"] = "stale"
        error = self.refused(lambda: self.handoff(decision={"provenance": "user_direct", "instruction": "x",
                             **{key: None for key in ("objective", "from_owner", "to_owner", "run",
                                                      "recorded_generation", "recorded_runtime",
                                                      "current_runtime")}, "ambiguity": []}, owner="third"))
        self.assertEqual(error.code, "objective_continued")
        self.assertEqual(self.status()["owner_handoff"]["condition"], "objective_continued")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.port.starts[1:], [])

    def test_status_and_list_name_the_successor_until_it_closes(self):
        self.claimed()
        status = self.status()
        self.assertEqual(status["blocker"], "objective_continued")
        self.assertEqual((status["succession"]["continued_by"], status["succession"]["state"]),
                         ("successor", "successor_open"))
        self.assertIn("Continued by successor objective successor", self.status(text=True))
        successor = self.status(objective="successor")
        self.assertEqual(successor["succession"]["continues"], "objective")
        listing = run("map", {"project": str(self.project), "objective": "successor"})
        self.assertEqual(listing["obligations"][0]["id"], "O1")
        from pod.listing import objective_list
        rows = {row["objective"]: row for row in objective_list(self.project)["objectives"]}
        self.assertEqual((rows["objective"]["phase"], rows["objective"]["continued_by"],
                          rows["objective"]["succession_state"]), ("continued", "successor", "successor_open"))
        self.assertEqual(rows["successor"]["continues"], "objective")
        run("checkpoint", {"project": str(self.project), "objective": "successor", "owner": "next",
                           "value": self.core(obligations=[self.criterion("satisfied", evidence=[
                               self.proof_for("successor")])], close=True)})
        status = self.status()
        self.assertEqual(status["succession"]["state"], "successor_closed")
        rows = {row["objective"]: row for row in objective_list(self.project)["objectives"]}
        self.assertEqual(rows["objective"]["succession_state"], "successor_closed")
        self.assertEqual(rows["successor"]["phase"], "closed")

    def test_a_second_claim_is_refused_and_a_replay_writes_nothing(self):
        self.claimed()
        before = self.snapshot()
        replay = self.claim({"provenance": "user_direct", "instruction": "again", "predecessor": "objective",
                             "predecessor_owner": "owner", "successor": "successor", "successor_owner": "next",
                             "runs": ["run"], "ambiguity": []})
        self.assertEqual((replay["status"], replay["replayed"]), ("claimed", True))
        self.assertEqual(before, self.snapshot())
        self.successor(name="second", caller="third", run_id="run-t")
        self.terminals["third"] = "live"
        verdict = self.claim(name="second", caller="third")
        self.assertEqual(verdict["condition"], "predecessor_claimed")
        decision = {"provenance": "user_direct", "instruction": "Continue it", **verdict["facts"]}
        self.refused(lambda: self.claim(decision, name="second", caller="third"), code="predecessor_claimed")
        self.assertEqual(read(self.project, "objective")["continued_by"]["objective"], "successor")

    def test_a_claim_interrupted_after_the_predecessor_write_completes_on_replay(self):
        self.predecessor(); self.successor()
        decision = self.decide()
        from pod import ledger
        original = ledger._write
        calls = []

        def interrupted(path, value):
            calls.append(path)
            if path == objective_root(self.project, "successor") / "context.json" and len(calls) == 1:
                raise PodError("unsafe_state", "interrupted")
            return original(path, value)
        with patch("pod.ledger._write", side_effect=interrupted):
            self.refused(lambda: self.claim(decision), code="unsafe_state")
        self.assertEqual(read(self.project, "objective")["continued_by"]["objective"], "successor")
        self.assertNotIn("continues", read(self.project, "successor"))
        self.assertEqual(self.status()["succession"]["state"], "claim_incomplete")
        before = self.snapshot()
        verdict = self.claim()  # Detection reports the gap and still writes nothing.
        self.assertEqual((verdict["status"], verdict["complete"]), ("claimed", False))
        self.assertEqual(before, self.snapshot())
        result = self.claim(decision)
        self.assertEqual((result["status"], result["replayed"]), ("claimed", True))
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "objective")
        self.assertEqual(self.status()["succession"]["state"], "successor_open")
        self.assertEqual(self.status(objective="successor")["succession"]["state"], "claimed")

    def test_cleanup_of_the_predecessor_protects_the_successor_worktree(self):
        self.claimed()
        plan = run("cleanup-plan", {"project": str(self.project), "objective": "objective"})
        reasons = [reason for row in plan["resources"] if row.get("kind") == "worktree" for reason in row["reasons"]]
        self.assertIn("recorded by successor objective successor", reasons)

    def proof_for(self, objective):
        from tests.kernel_support import proof
        from pod.config import effective
        definition = read(self.project, objective)["checkpoint"]["obligations"][0]
        return proof("O1", self.candidate, definition=definition,
                     policy_revision=effective(self.project)["revision"])


if __name__ == "__main__":
    unittest.main()

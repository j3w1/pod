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
from tests.common import GroupedTable, assert_grouped_table
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


def _raw(case, objective):
    path = objective_root(case.project, objective) / "context.json"
    return path, json.loads(path.read_text())


def _admission_state(state):
    def arrange(case):
        path, raw = _raw(case, "objective")
        row = raw["admissions"][case.admission["admission_id"]]
        row.update(state=state, native_binding=None)
        path.write_text(json.dumps(raw))
    return arrange


def _differing_identity(case):
    path, raw = _raw(case, "objective")
    raw["admissions"][case.admission["admission_id"]]["native_binding"]["worktreeId"] = "another-worktree"
    path.write_text(json.dumps(raw))


def _closed(case):
    path, raw = _raw(case, "objective")
    checkpoint = raw["checkpoint"]
    checkpoint["closure"] = {"seq": checkpoint["seq"], "revision": checkpoint["revision"], "report": {}}
    path.write_text(json.dumps(raw))


# kind: (arrange before detection, expected condition, patch target or None)
SIBLINGS = {
    "predecessor-closed": (_closed, "predecessor_closed", None),
    "handoff-pending": (None, "predecessor_handoff_pending", ("pod.handoff.unfinished", {"state": "pending"})),
    "effect-pending": (None, "predecessor_effect_pending", ("pod.ledger._governor_pending", True)),
    "admission-unresolved": (_admission_state("unresolved"), "predecessor_work_unsettled", None),
    "reserved-with-dispatch": (_admission_state("reserved"), "predecessor_work_unsettled", None),
    "differing-identity": (_differing_identity, "predecessor_unverified", None),
}


class SiblingRefusalTableTests(GroupedTable):
    """Each unproven predecessor fact holds the claim and writes nothing."""
    GROUPS = {"test_predecessor_record_holds_the_claim": ["predecessor-closed", "handoff-pending", "effect-pending"],
              "test_predecessor_work_holds_the_claim": ["admission-unresolved", "reserved-with-dispatch",
                                                        "differing-identity"]}

    def row(self, kind):
        arrange, condition, target = SIBLINGS[kind]
        case = SuccessionCase(); case.setUp()
        try:
            with self.subTest(kind=kind):
                case.predecessor(); case.successor()
                if arrange is not None:
                    arrange(case)
                patcher = patch(target[0], return_value=target[1]) if target else None
                if patcher:
                    patcher.start()
                try:
                    before = case.snapshot()
                    verdict = case.claim()
                    self.assertEqual(verdict["condition"], condition, verdict.get("message"))
                    decision = {"provenance": "user_direct", "instruction": "Continue it", **verdict["facts"]}
                    case.refused(lambda: case.claim(decision), code=condition)
                    self.assertEqual(before, case.snapshot())
                finally:
                    if patcher:
                        patcher.stop()
        finally:
            case.doCleanups()

    def test_predecessor_record_holds_the_claim(self):
        self.rows()

    def test_predecessor_work_holds_the_claim(self):
        self.rows()


class SiblingRefusalTests(SuccessionCase):
    def test_the_sibling_table_runs_every_row_once(self):
        assert_grouped_table(self, SiblingRefusalTableTests, list(SIBLINGS), limit=3)

    def test_a_reserved_start_without_dispatch_is_claimed_only_when_the_decision_names_it(self):
        self.predecessor(); self.successor()
        _admission_state("reserved")(self)
        self.port.find_rows = []
        verdict = self.claim()
        self.assertEqual(verdict["status"], "claim_available")
        entry = f"reserved admission {self.admission['admission_id']}: no Dispatch found"
        self.assertEqual(verdict["facts"]["ambiguity"], [entry])
        self.refused(lambda: self.claim(self.decide(ambiguity=[])), code="succession_decision_mismatch")
        self.claim(self.decide())
        self.assertEqual(read(self.project, "objective")["continued_by"]["ambiguity"], [entry])
        status = self.status()
        self.assertEqual(status["assignments"]["settled"][0]["settlement"], "claim_no_dispatch")
        self.assertIn("no Dispatch was found at the claim", status["assignments"]["settled"][0]["basis"])
        self.assertEqual(status["succession"]["ambiguity"], [entry])
        self.assertIn("Claimed without a Dispatch: 1 reserved assignment(s)", self.status(text=True))

    def test_an_unreadable_predecessor_or_a_started_successor_cannot_claim(self):
        self.predecessor(); self.successor()
        path, raw = _raw(self, "objective")
        original = path.read_text()
        path.write_text("truncated{")
        self.refused(lambda: self.claim(), code="predecessor_unverified")
        path.write_text(original)
        run("checkpoint", {"project": str(self.project), "objective": "successor", "owner": "next",
                           "value": self.core(obligations=[self.criterion("satisfied", evidence=[
                               AfterClaimTests.proof_for(self, "successor")])])})
        self.assertEqual(self.claim()["condition"], "successor_started")


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
        for name, call in self.boundary_calls("third").items():  # The session back in a new terminal.
            with self.subTest(boundary=name, caller="third"):
                os.environ["ORCA_TERMINAL_HANDLE"] = "third"
                self.refused(call, code="objective_continued")
        os.environ["ORCA_TERMINAL_HANDLE"] = "owner"
        from pod.operations import _start_observed
        starts = list(self.port.starts)
        self.refused(lambda: _start_observed(self.project, "objective", admission_id=self.admission["admission_id"],
                                             port=self.port, owner="owner", run="run", task="task-9",
                                             route={"agent": "claude", "model": "m", "effort": "high"}),
                     code="objective_continued")
        self.assertEqual(self.port.starts, starts)
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
        # lose() changed the runtime, as an Orca restart does; the claim's proof still settles them.
        self.assertEqual(status["native_settlement"], "unverified")
        self.assertEqual(status["assignments"]["unverified"], [])
        self.assertEqual([(row["admission"], row["settlement"]) for row in status["assignments"]["settled"]],
                         [(self.admission["admission_id"], "claim")])
        self.assertFalse(any("settlement unverified" in line for line in status["progress"]))
        self.assertIn("Settled by the successor claim: 1 assignment(s)", self.status(text=True))
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
        self.refused(lambda: self.claim({"provenance": "user_direct", "instruction": "again", "predecessor": "objective",
                                         "predecessor_owner": "someone", "successor": "successor",
                                         "successor_owner": "next", "runs": ["unrelated-run"], "ambiguity": []}),
                     code="succession_decision_mismatch")
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
        self.assertEqual(verdict["facts"], {key: decision[key] for key in verdict["facts"]})
        # A decision rebuilt from those facts completes the link, as R110 asks.
        result = self.claim({"provenance": "user_direct", "instruction": "Finish the interrupted claim",
                             **verdict["facts"]})
        self.assertEqual((result["status"], result["replayed"]), ("claimed", True))
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "objective")
        self.assertEqual(self.status()["succession"]["state"], "successor_open")
        self.assertEqual(self.status(objective="successor")["succession"]["state"], "claimed")

    def test_a_replay_never_moves_a_successor_that_continues_another_objective(self):
        self.predecessor(); self.successor()
        decision = self.decide()
        from pod import ledger
        original = ledger._write
        with patch("pod.ledger._write", side_effect=lambda path, value: (_ for _ in ()).throw(PodError("unsafe_state", "x"))
                   if path.parent.name == objective_root(self.project, "successor").name else original(path, value)):
            self.refused(lambda: self.claim(decision), code="unsafe_state")
        path, raw = _raw(self, "successor")
        raw["continues"] = {"objective": "another", "record": "0" * 64, "at": "2026-01-01T00:00:00+00:00"}
        path.write_text(json.dumps(raw))
        before = self.snapshot()
        self.refused(lambda: self.claim(decision), code="successor_started")
        self.assertEqual(before, self.snapshot())

    def test_cleanup_protection_follows_the_successor_chain_and_holds_on_unknown_locations(self):
        from pod import cleanup

        def record(path=None, successor=None, placement=None):
            value = {"checkpoint": {"worktree": {"path": path}} if path else {}, "admissions": {}}
            if placement:
                value["admissions"]["a"] = {"recovery": {"placement_binding": {"path": placement}}}
            if successor:
                value["continued_by"] = {"objective": successor}
            return value
        cases = {
            "chain": ({"s1": record("/w/s1", "s2"), "s2": record("/w/s2", placement="/w/s2-child")},
                      {"/w/s1", "/w/s2", "/w/s2-child"}),
            "unrecorded": ({"s1": record(None, "s2"), "s2": record("/w/s2")}, None),
            "unreadable": ({"s1": record("/w/s1", "s2")}, None),
            "loop": ({"s1": record("/w/s1", "s2"), "s2": record("/w/s2", "s1")}, None),
        }
        for kind, (records, expected) in cases.items():
            with self.subTest(kind=kind), patch("pod.cleanup.read",
                                                side_effect=lambda _project, name: records.get(name)):
                self.assertEqual(cleanup._successor_paths(self.project, record(successor="s1")), ("s1", expected))

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

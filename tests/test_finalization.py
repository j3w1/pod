"""R2 finalization: what a coordinator still records before it says the objective is finished."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import (FINALIZATION_RECORDS, _read_journal, _record_path, finalization, observe_candidate,
                          prepare_candidate)
from pod.internal import acknowledge, run as internal_run
from pod.ledger import closed_snapshot_context, map_of, objective_root, read
from pod.obligations import validate_closed_snapshot
from tests.kernel_support import KernelCase, GovernorFakePort as FakePort, action, authorization, git


def row(record_id, kind, outcome, *, decision="ALLOW", derived_from=None):
    return {"record_id": record_id, "decision": decision, "outcome": outcome,
            "action": {"kind": kind}, "derived_from": derived_from}


def admission(role, state, *, report=None, disposition=None):
    return {"role": role, "state": state, "report": report, "disposition": disposition}


def context(*, admissions=None, closure=None, delivery=None, mapped=True):
    checkpoint = {"obligations": [{"id": "O1"}], "closure": closure, "delivery": delivery} if mapped else {}
    return {"admissions": admissions or {}, "checkpoint": checkpoint}


class FinalizationTests(unittest.TestCase):
    def test_every_remaining_record_is_named_in_recording_order(self):
        journal = {"actions": [
            row("p" * 64, "pr_update", "PASS"),
            row("d" * 64, "workflow_dispatch", "pending", derived_from="p" * 64),
            row("r" * 64, "release", "UNKNOWN"),
            row("x" * 64, "push", "pending", decision="DEFER"),
            row("m" * 64, "merge", "pending"),
            row("n" * 64, "merge", "PASS"),
        ]}
        state = context(admissions={
            "review-1": admission("review", "bound"),
            "impl-1": admission("implement", "bound", report={"status": "validated_observation"}),
            "impl-2": admission("implement", "closed"),
            "impl-3": admission("implement", "deferred"),
        })
        result = finalization(state, journal)
        self.assertEqual(result["remaining"], list(FINALIZATION_RECORDS))
        self.assertFalse(result["finished"])
        self.assertEqual(result["records"]["merge_outcome"],
                         [{"record": "m" * 64, "kind": "merge", "state": "pending", "derived_from": None}])
        self.assertEqual(result["records"]["delivery_record"],
                         [{"record": "n" * 64, "kind": "merge", "state": "PASS", "derived_from": None}])
        self.assertEqual(result["records"]["validation_readback"], [
            {"record": "d" * 64, "kind": "workflow_dispatch", "state": "pending", "derived_from": "p" * 64},
            {"record": "r" * 64, "kind": "release", "state": "UNKNOWN", "derived_from": None}])
        self.assertEqual(result["records"]["report_consumption"], [
            {"record": "impl-1", "kind": "implement", "state": "undispositioned", "derived_from": None},
            {"record": "review-1", "kind": "review", "state": "bound", "derived_from": None}])
        self.assertEqual(result["records"]["closure"], [])
        self.assertTrue(result["next_action"].startswith(
            "Not finished: record merge_outcome, delivery_record, validation_readback, report_consumption, "
            "closure in that order before saying the objective is finished or the coordinator safe to retire. "
            "Next: fetch the merge target, then record governor-outcome for merge " + "m" * 12))
        self.assertIn("never merge again", result["next_action"])

    def test_the_next_action_follows_the_first_remaining_record(self):
        cases = (
            ([row("n" * 64, "merge", "PASS")], {}, ["delivery_record", "closure"],
             "write a checkpoint with delivery: {record: " + "n" * 12 + "}"),
            ([row("n" * 64, "merge", "PASS"), row("d" * 64, "workflow_dispatch", "pending")],
             {"delivery": {"record": "n" * 64}}, ["validation_readback", "closure"],
             "settle " + "d" * 12 + " from Pod's own readback with governor-reconcile"),
            ([], {"admissions": {"review-1": admission("review", "reserved")}}, ["report_consumption", "closure"],
             "consume the worker report of admission review-1 with internal report"),
            ([row("n" * 64, "merge", "PASS")], {"delivery": {"record": "n" * 64}}, ["closure"],
             "write a checkpoint with close: true"),
        )
        for actions, fields, remaining, step in cases:
            with self.subTest(remaining=remaining):
                result = finalization(context(**fields), {"actions": actions})
                self.assertEqual((result["remaining"], result["finished"]), (remaining, False))
                self.assertEqual(list(result["records"]), remaining)
                self.assertIn("Next: " + step, result["next_action"])

    def test_closure_ends_finalization_and_only_then_reports_finished(self):
        journal = {"actions": [row("n" * 64, "merge", "PASS")]}
        closed = finalization(context(closure={"seq": 4, "revision": 4}), journal)
        self.assertEqual((closed["remaining"], closed["records"], closed["finished"]), ([], {}, True))
        self.assertTrue(closed["next_action"].startswith("Finished: closure is recorded and Pod needs nothing more"))
        self.assertIn("safe to retire", closed["next_action"])
        open_map = finalization(context(delivery={"record": "n" * 64}), journal)
        self.assertEqual((open_map["remaining"], open_map["finished"]), (["closure"], False))

    def test_a_mapless_objective_has_no_delivery_record_or_closure_to_write(self):
        pending = finalization(context(mapped=False), {"actions": [row("n" * 64, "merge", "PASS"),
                                                                   row("p" * 64, "push", "UNKNOWN")]})
        self.assertEqual(pending["remaining"], ["validation_readback"])
        settled = finalization(context(mapped=False), {"actions": [row("p" * 64, "push", "PASS")]})
        self.assertEqual((settled["remaining"], settled["finished"]), ([], True))
        self.assertIn("no obligation map to close", settled["next_action"])
        self.assertEqual(finalization(None, {"actions": []})["remaining"], [])

    def test_the_projection_reads_and_changes_nothing(self):
        state = context(admissions={"review-1": admission("review", "bound")})
        journal = {"actions": [row(str(index) * 64, "workflow_dispatch", "pending") for index in range(6)]}
        before = deepcopy((state, journal))
        result = finalization(state, journal)
        self.assertEqual((state, journal), before)
        self.assertIn("and 2 more", result["next_action"])
        self.assertEqual(result["schema"], "pod-finalization/v1")


CONTRACT = {"status": "observed", "runtime": "runtime", "capabilities": {"launch_preferences_v1": True}}


class ResumeTests(KernelCase):
    """PoD#3 — a coordinator records before it announces and resumes on the same Run without repeating."""

    def setUp(self):
        super().setUp()
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        (self.project / "README.md").write_text("candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write(self.stored())
        self.observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        self.remote = FakePort()
        self.acks = []
        for target, value in (("pod.internal._governor_projection", {"outstanding": []}),
                              ("pod.orca.contract", CONTRACT)):
            started = patch(target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        remote = patch("pod.governor_effects.GhPort", side_effect=lambda _project: self.remote)
        remote.start()
        self.addCleanup(remote.stop)

    def op(self, operation, **fields):
        """One helper call through the CLI boundary; every acknowledgement is kept."""
        request = {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}
        result = acknowledge(operation, request, internal_run(operation, request))
        self.acks.append((operation, result))
        return result

    def refusal(self, operation, **fields):
        with self.assertRaises(PodError) as caught:
            self.op(operation, **fields)
        return caught.exception

    def consent(self, scope):
        return authorization(candidate=self.observed["commit"], tree=self.observed["tree"], scope=(scope,),
                             target="origin/target")

    def merge(self):
        return action(kind="merge", candidate=self.observed["commit"], target="origin/target", unit="delivery",
                      effects=[], authorization=self.consent("merge"))

    def checkpoint(self, **fields):
        return self.op("checkpoint", value=self.core(obligations=self.stored(), **fields))

    def merges(self):
        return int(git(self.project, "rev-list", "--merges", "--count", "refs/remotes/origin/target"))

    def test_an_interrupted_merge_finishes_once_and_announces_only_after_closure(self):
        published = self.op("governor", action=action(candidate=self.observed["commit"], target="origin/candidate",
                                                      unit="delivery", effects=["workflow:ci.yml"],
                                                      authorization=self.consent("publish")))
        self.remote.heads["origin/candidate"] = self.candidate
        self.op("governor-outcome", record_id=published["record_id"], outcome="PASS")
        derived = [row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                   if row.get("derived_from") == published["record_id"]][0]["record_id"]
        decided = self.op("governor", action=self.merge())
        self.assertIn("fetch first", decided["next_action"])
        # The remote effect lands; the coordinator is lost before it records anything.
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        # Resumed on the same Run: the map names what remains, in order, and the merge is not repeated.
        resumed = internal_run("map", {"project": str(self.project), "objective": "objective"})["finalization"]
        self.assertEqual(resumed["remaining"], ["merge_outcome", "validation_readback", "closure"])
        self.assertEqual(resumed["records"]["merge_outcome"][0]["record"], decided["record_id"])
        self.assertIn("never merge again", resumed["next_action"])
        again = self.op("governor", action=self.merge())
        self.assertEqual((again["decision"], again["reuse"]["kind"], again["record_id"]),
                         ("REUSE", "attach", decided["record_id"]))
        self.assertIn("Never merge again", again["next_action"])
        git(self.project, "update-ref", "refs/remotes/origin/target", result)  # fetch first
        run = self.remote._start("ci.yml", self.candidate, event="push",
                                 created_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.remote.complete(run["id"], "success")
        recorded = self.op("governor-outcome", record_id=decided["record_id"], outcome="PASS",
                           provider={"merge_commit": result, "method": "merge"})
        # A merged candidate leaves no validation whose readback was never attempted.
        self.assertEqual(recorded["readback"], [{"record_id": derived, "status": "PASS", "action": "readback"}])
        self.assertEqual(recorded["finalization"]["remaining"], ["delivery_record", "closure"])
        replay = self.op("governor-outcome", record_id=decided["record_id"], outcome="PASS",
                         provider={"merge_commit": result, "method": "merge"})
        self.assertEqual(replay["outcome"], "PASS")
        delivered = self.checkpoint(delivery={"record": decided["record_id"]})
        self.assertEqual(delivered["finalization"]["remaining"], ["closure"])
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        closed = self.op("checkpoint", value=self.core(obligations=rows, close=True))
        self.assertEqual((closed["finalization"]["remaining"], closed["finalization"]["finished"]), ([], True))
        self.assertTrue(closed["finalization"]["next_action"].startswith("Finished: closure is recorded"))
        announced = [operation for operation, ack in self.acks if (ack.get("finalization") or {}).get("finished")]
        self.assertEqual(announced, ["checkpoint"])
        self.assertIs(self.acks[-1][1], closed)
        # A lost reply after closure is answered from the record; nothing is repeated or reopened.
        late = self.refusal("governor-outcome", record_id=decided["record_id"], outcome="PASS",
                            provider={"merge_commit": result, "method": "merge"})
        self.assertEqual((late.code, late.detail["finalization"]["finished"]), ("objective_closed", True))
        reclose = self.refusal("checkpoint", value=self.core(obligations=self.stored(), close=True))
        self.assertTrue(reclose.detail["finalization"]["finished"])
        self.assertEqual(self.merges(), 1)
        self.assertFalse([call for call in self.remote.calls if call[0] in ("push", "dispatch", "rerun", "cancel")])

    def test_a_closed_objective_stays_finished_after_its_target_advances(self):
        decided = self.op("governor", action=self.merge())
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.op("governor-outcome", record_id=decided["record_id"], outcome="PASS",
                provider={"merge_commit": result})
        self.checkpoint(delivery={"record": decided["record_id"]})
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.op("checkpoint", value=self.core(obligations=rows, close=True))
        root = objective_root(self.project, "objective")
        before = {name: (root / name).read_bytes() for name in ("context.json", "governor.json")}
        (self.project / "later.txt").write_text("later\n")
        git(self.project, "add", "later.txt")
        git(self.project, "commit", "-qm", "later work")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        # The projection needs only the stored records: no Git, worktree or provider read.
        stored = {name: json.loads(data) for name, data in before.items()}
        self.assertTrue(finalization(stored["context.json"], stored["governor.json"])["finished"])
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(map_of(state),
                                                  closed_snapshot_context(self.project, "objective", state)), ())
        self.assertEqual({name: (root / name).read_bytes() for name in before}, before)

    def test_an_interrupted_publication_resumes_by_attachment_and_readback(self):
        request = action(candidate=self.observed["commit"], target="origin/candidate", unit="delivery",
                         effects=["workflow:ci.yml"], authorization=self.consent("publish"))
        from pod.governor import _admit
        admitted = _admit(self.project, "objective", owner="owner", action=request, exception=None,
                          now=datetime.now(timezone.utc), managed=True, native_projection={"outstanding": []})
        # The push lands, then the coordinator is lost before execution records it.
        self.remote.push(remote="origin", branch="candidate", commit=self.candidate, expected=None)
        resumed = internal_run("map", {"project": str(self.project), "objective": "objective"})["finalization"]
        self.assertEqual(resumed["remaining"], ["validation_readback", "closure"])
        attached = self.op("governor-execute", action=request)
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]), ("attach", admitted["record_id"]))
        settled = self.op("governor-reconcile", record_id=admitted["record_id"])
        self.assertEqual(settled["status"], "PASS")
        self.assertFalse(settled["finalization"]["finished"])
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "push"]), 1)


if __name__ == "__main__":
    unittest.main()

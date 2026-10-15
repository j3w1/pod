"""R118: a successor's claim carries a lost coordinator's unsettled Governor rows and settles them only by readback.

Production boundaries throughout: Orca and the GitHub port are the only fakes, and every carried row comes from
the predecessor's own Governor operations. A row written by hand is named a synthetic control.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import tarfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import CHAIN_FIELDS, _read_journal, _record_path, _write_journal, reconcile, record_outcome
from pod.internal import run
from pod.ledger import objective_root, read, state_root
from tests.common import GroupedTable, assert_grouped_table
from tests.kernel_support import git
from tests.test_succession import SuccessionCase

WRITES = ("push", "pr_create", "dispatch", "rerun", "cancel")


class CarriedCase(SuccessionCase):
    def setUp(self):
        super().setUp()
        # These production paths run on the real clock: runs and attempts the fake remote starts begin a minute
        # after now, so a slow fixture never makes one look older than the submission that started it.
        self.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()

    def lost_with(self, build):
        """The predecessor's coordinator leaves Governor work unsettled, then Orca reports it lost."""
        self.establish()
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        self.settle(self.admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.op("checkpoint", value=self.core(obligations=rows))
        built = build()
        self.lose()
        return built

    def journal(self, objective="objective"):
        return _read_journal(_record_path(self.project, objective))

    def row(self, record_id, objective="successor"):
        return next(row for row in self.journal(objective)["actions"] if row["record_id"] == record_id)

    def derived(self, publication, objective="objective"):
        return next(row["record_id"] for row in self.journal(objective)["actions"]
                    if row.get("derived_from") == publication)

    def writes(self):
        return [call for call in self.remote.calls if call[0] in WRITES]

    def close_successor(self):
        rows = read(self.project, "successor")["checkpoint"]["obligations"]
        for row in rows:
            row.pop("executor", None)
            row.update(state="satisfied", evidence=[{"check": "criterion", "command": "unit", "result": "passed",
                                                     "reference": "successor-proof"}])
        return self.mine("checkpoint", value=self.core(obligations=rows, close=True))

    def merge_decision(self):
        candidate = self.renew()
        consent = {**self.authorization, "scope": ["merge"], "target": "origin/target"}
        decided = self.op("governor", action={"kind": "merge", "candidate": candidate["commit"],
                                              "target": "origin/target", "reason": "deliver", "unit": "default",
                                              "effects": [], "authorization": consent})
        self.assertEqual(decided["decision"], "ALLOW", decided["explanation"])
        self.consent = consent
        return {"merge": decided["record_id"]}

    def pull_request(self, state, merge_commit=None):
        self.remote.pr_history[("delivery", "target")] = [{
            "number": 9, "url": "https://example.invalid/pull/9", "head_sha": self.candidate, "state": state,
            "merge_commit": merge_commit, "merged_at": None}]

    def merged(self, *, fetched):
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "main")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-q", "main")
        if fetched:
            git(self.project, "update-ref", "refs/remotes/origin/target", result)
        return result

    def mine(self, operation, **fields):
        """A successor coordinator's own helper call."""
        return run(operation, {"project": str(self.project), "objective": "successor", "owner": "next", **fields})

    def carry(self):
        if read(self.project, "successor") is None:
            self.successor()
        decision = self.decide()
        return decision, self.claim(decision)

    def renew(self):
        """A new candidate generation of the default unit, with publish consent for it."""
        self.move("README.md")
        self.op("checkpoint", value=self.core(obligations=self.stored()))
        candidate = self.op("governor-prepare", unit="default",
                            branch={"remote": "origin", "branch": "delivery", "base": "target"})["candidate"]
        self.authorization = {**self.authorization, "candidate": candidate["commit"], "tree": candidate["tree"]}
        return candidate

    def publications(self):
        pushed = self.op("governor-execute", action=self.action("push", effects=["workflow:ci.yml"]))
        reserved = self.op("governor", action=self.action("pr_update"))
        self.renew()
        self.remote.lost.add("push")
        lost = self.op("governor-execute", action=self.action("push"))
        self.remote.lost.clear()
        self.assertEqual(lost["outcome"], "UNKNOWN")
        return {"derived": self.derived(pushed["record_id"]), "pr_update": reserved["record_id"],
                "push": lost["record_id"]}

    def failed_release(self):
        """A pushed candidate whose release.yml dispatch failed transiently, so a rerun of it is allowed."""
        self.op("governor-execute", action=self.action("push"))
        failed = self.op("governor-execute", action=self.action("workflow_dispatch", target="release.yml"))
        self.remote.complete(failed["receipt"]["provider"]["run_id"], "failure")
        self.assertEqual(self.op("governor-reconcile", record_id=failed["record_id"])["status"], "FAILED")
        self.op("governor-classify", record_id=failed["record_id"],
                classification={"class": "transient", "reason": "runner lost"})
        return failed

    def lost_reply_rerun(self, *, landed):
        """The predecessor's rerun loses GitHub's reply, after or before it landed, so no acceptance is recorded."""
        self.failed_release()
        original = self.remote.rerun

        def lost(*, run_id, failed_only):
            if landed:
                original(run_id=run_id, failed_only=failed_only)
            raise PodError("remote_effect_uncertain", "the reply was lost")
        self.remote.rerun = lost
        try:
            rerun = self.op("governor-execute", action=self.action("validation_rerun", target="release.yml"))
        finally:
            self.remote.rerun = original
        self.assertEqual(rerun["outcome"], "UNKNOWN")
        return rerun["record_id"]

    def runs(self):
        self.failed_release()
        rerun = self.op("governor-execute", action=self.action("validation_rerun", target="release.yml"))
        nightly = self.op("governor-execute", action=self.action("workflow_dispatch", target="nightly.yml"))
        probe = self.op("governor", action=self.action("remote_diagnostic"))
        cancel = self.op("governor", action=self.action("cancel_validation", target=nightly["record_id"]))
        return {"validation_rerun": rerun["record_id"], "workflow_dispatch": nightly["record_id"],
                "remote_diagnostic": probe["record_id"], "cancel_validation": cancel["record_id"]}


class CarryTests(CarriedCase):
    """PoD#4 — a claim with each carried kind succeeds; the predecessor keeps every byte but its link."""

    def check_carried(self, built):
        journal_path = _record_path(self.project, "objective")
        before = journal_path.read_bytes()
        decision, claimed = self.carry()
        self.assertEqual(sorted(claimed["carried"]), sorted(built.values()))
        self.assertEqual(sorted(decision["carried"]), sorted(built.values()))
        self.assertEqual(journal_path.read_bytes(), before)
        source = {row["record_id"]: row for row in _read_journal(journal_path)["actions"]}
        for kind, record_id in built.items():
            with self.subTest(kind=kind):
                copy = self.row(record_id)
                marker = copy.pop("carried_from")
                self.assertEqual((marker["objective"], marker["record"]), ("objective", record_id))
                self.assertEqual(copy, source[record_id])
        self.assertEqual(self.journal("successor")["units"]["default"]["carried_from"], {"objective": "objective"})
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "objective")
        status = self.status()["succession"]["carried"]
        self.assertEqual({row["record"] for row in status["rows"]}, set(built.values()))
        self.assertEqual(status["unsettled"], len(built))

    def test_publication_rows_are_carried(self):
        built = self.lost_with(self.publications)
        self.successor()
        self.assertIn("Its 3 unsettled Governor effect(s) move to the successor", self.claim()["question"])
        self.check_carried(built)

    def test_run_rows_are_carried(self):
        built = self.lost_with(self.runs)
        self.check_carried(built)


class RefusalTests(CarriedCase):
    """PoD#4 — every listed refusal still refuses, and writes nothing."""

    def synthetic(self, **action):
        """Synthetic control: one unsettled row of a kind the predecessor's own operations do not leave here."""
        path = _record_path(self.project, "objective")
        journal = _read_journal(path)
        journal["actions"].append({
            "record_id": "s" * 64, "logical_key": "s" * 64, "attempt": 1,
            "action": {"candidate": self.candidate, "target": "ci.yml", "unit": "default", **action},
            "decision": "ALLOW", "outcome": "pending", "commit": None,
            "receipt": {"started_at": "2026-01-01T00:00:00+00:00", "provider": None},
            "warnings": [], "reasons": [], "classifications": [], "run_event": None, "rejected_run": None})
        _write_journal(path, journal)

    def test_rows_without_a_readback_and_unreadable_journals_hold_the_claim(self):
        cases = {"release": ({"kind": "release"}, "a release is performed by the project"),
                 "deploy": ({"kind": "deploy"}, "a deploy is performed by the project"),
                 "unbound": ({"kind": "workflow_dispatch", "unit": "elsewhere"}, "no delivery unit binds it"),
                 "no-commit": ({"kind": "workflow_dispatch"}, "neither a run nor a commit")}
        self.lost_with(lambda: None)
        self.successor()
        path = _record_path(self.project, "objective")
        original = path.read_bytes()
        for name, (action, reason) in cases.items():
            with self.subTest(case=name):
                path.write_bytes(original)
                self.synthetic(**action)
                before = self.snapshot()
                verdict = self.claim()
                self.assertEqual(verdict["condition"], "predecessor_effect_pending", verdict)
                self.assertIn(reason, verdict["message"])
                self.refused(lambda: self.claim({"provenance": "user_direct", "instruction": "Continue it",
                                                 **verdict["facts"]}), code="predecessor_effect_pending")
                self.assertEqual(before, self.snapshot())
        path.write_text("{")
        self.assertEqual(self.claim()["condition"], "predecessor_unverified")

    def unattributable(self, build, reason="without the attempt read and start recorded before submission"):
        """A rerun no readback can ever attribute (R115) holds the claim, naming why, and writes nothing."""
        record_id = self.lost_with(build)
        self.successor()
        before = self.snapshot()
        verdict = self.claim()
        self.assertEqual(verdict["condition"], "predecessor_effect_pending", verdict)
        self.assertIn(f"validation_rerun {record_id[:12]} cannot be carried", verdict["message"])
        self.assertIn(reason, verdict["message"])
        self.assertEqual(before, self.snapshot())

    def rewritten_rerun(self, provider):
        """Synthetic control: the predecessor's executed rerun, its provider receipt rewritten by `provider`."""
        self.failed_release()
        record_id = self.op("governor-execute",
                            action=self.action("validation_rerun", target="release.yml"))["record_id"]
        path = _record_path(self.project, "objective")
        journal = _read_journal(path)
        row = next(row for row in journal["actions"] if row["record_id"] == record_id)
        row["receipt"]["provider"] = provider(row["receipt"]["provider"])
        _write_journal(path, journal)
        return record_id

    def test_a_caller_performed_rerun_reservation_holds_the_claim(self):
        def reserved():
            self.failed_release()
            return self.op("governor", action=self.action("validation_rerun", target="release.yml"))["record_id"]
        self.unattributable(reserved)

    def test_a_rerun_recorded_without_provenance_holds_the_claim(self):
        # As 0.12.0 recorded an executed rerun: its run only.
        self.unattributable(lambda: self.rewritten_rerun(lambda bound: {"run_id": bound["run_id"], "rerun": True}))

    def test_a_rerun_bound_without_provenance_holds_the_claim(self):
        # As an earlier 0.13.0 candidate bound a pending rerun: its attempt, with no pre-read, start or acceptance.
        def earlier(bound):
            return {key: value for key, value in bound.items()
                    if key not in ("before_attempt", "submitted_at", "accepted", "started_at")}
        self.unattributable(lambda: self.rewritten_rerun(earlier),
                            "attempt 2 was bound to this rerun without the attempt read and start")

    def test_a_decision_must_name_exactly_the_carried_rows(self):
        built = self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        before = self.snapshot()
        for carried in ([], decision["carried"][:-1], [*decision["carried"], "x" * 64]):
            with self.subTest(count=len(carried)):
                self.refused(lambda: self.claim({**decision, "carried": carried}), code="succession_decision_mismatch")
        stripped = {key: value for key, value in decision.items() if key != "carried"}
        self.refused(lambda: self.claim(stripped), code="succession_decision_mismatch")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(sorted(self.claim(decision)["carried"]), sorted(built.values()))


class ReplayTests(CarriedCase):
    """PoD#4 — replay, an interrupted claim, a competing claim and a returning predecessor behave as R110."""

    def interrupted(self, patched):
        built = self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patched, self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        self.assertEqual(read(self.project, "objective")["continued_by"]["objective"], "successor")
        self.assertEqual(read(self.project, "successor")["continues"]["state"], "claiming")
        self.assertEqual(self.status()["succession"]["state"], "claim_incomplete")
        self.assertEqual(self.status(objective="successor")["succession"]["state"], "claim_incomplete")
        detected = self.claim()
        self.assertEqual((detected["condition"], detected["complete"]), ("claimed_by_this_successor", False))
        self.assertEqual(sorted(detected["facts"]["carried"]), sorted(built.values()))
        replayed = self.claim(decision)
        self.assertEqual((replayed["status"], replayed["replayed"]), ("claimed", True))
        carried = [row["record_id"] for row in self.journal("successor")["actions"]]
        self.assertEqual(sorted(carried), sorted(built.values()))
        settled = self.snapshot()
        self.assertTrue(self.claim(decision)["replayed"])
        self.assertEqual(settled, self.snapshot())

    def test_a_claim_interrupted_after_the_predecessor_link_completes_by_replay(self):
        self.interrupted(patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")))

    def test_a_claim_interrupted_after_the_carried_rows_completes_by_replay(self):
        from pod import ledger
        real = ledger._write

        def write(path, value):
            if "state" not in value.get("continues", {"state": None}):  # The claim's last write completes the link.
                raise KeyboardInterrupt("after the carried rows")
            return real(path, value)
        self.interrupted(patch("pod.ledger._write", side_effect=write))

    def test_competing_claims_and_the_returning_predecessor_are_refused(self):
        built = self.lost_with(self.publications)
        effects = self.writes()
        self.carry()
        before = _record_path(self.project, "objective").read_bytes()
        self.successor(name="third", caller="third", run_id="run-t")
        verdict = self.claim(name="third", caller="third")
        self.assertEqual(verdict["condition"], "predecessor_claimed")
        self.returning()
        for operation, fields in (("governor-reconcile", {"record_id": built["push"]}),
                                  ("governor-outcome", {"record_id": built["push"], "outcome": "UNKNOWN"}),
                                  ("governor-execute", {"action": self.action("push")})):
            with self.subTest(operation=operation):
                self.refused(lambda: self.op(operation, **fields), code="objective_continued")
        self.assertEqual(_record_path(self.project, "objective").read_bytes(), before)
        self.assertEqual(self.writes(), effects)  # Neither the claim nor the refusals wrote to the provider.


class ReservationTests(CarriedCase):
    """PoD#4 — an incomplete claim reserves its successor: no other write or closure until the claim completes."""

    def own_work(self):
        """The successor coordinator's unrelated work: a delivery unit of its own, and closing the objective."""
        return {"prepare": lambda: self.mine("governor-prepare", unit="own",
                                             branch={"remote": "origin", "branch": "own", "base": "target"}),
                "close": self.close_successor}

    def interrupted_at(self, patched):
        built = self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patched, self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        self.assertEqual(read(self.project, "successor")["continues"]["state"], "claiming")
        self.assertEqual(self.status(objective="successor")["succession"]["state"], "claim_incomplete")
        before = self.snapshot()
        for name, call in self.own_work().items():
            with self.subTest(work=name):
                self.refused(call, code="claim_incomplete")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.claim(decision)["status"], "claimed")
        self.assertNotIn("state", read(self.project, "successor")["continues"])
        self.assertEqual(sorted(row["record_id"] for row in self.journal("successor")["actions"]),
                         sorted(built.values()))
        # Once complete, the carried rows block closure as the predecessor's would.
        self.assertEqual(self.refused(self.close_successor).detail["detail"], "closure_unfinished")

    def test_a_claim_interrupted_after_its_reservation_holds_the_successor(self):
        self.interrupted_at(patch("pod.ledger._write_succession", side_effect=KeyboardInterrupt("reserved")))

    def test_a_claim_interrupted_after_the_predecessor_link_holds_the_successor(self):
        self.interrupted_at(patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")))

    def test_a_claim_interrupted_after_the_carried_rows_holds_the_successor(self):
        from pod import ledger
        real = ledger._write

        def write(path, value):
            if "state" not in value.get("continues", {"state": None}):
                raise KeyboardInterrupt("after the carried rows")
            return real(path, value)
        self.interrupted_at(patch("pod.ledger._write", side_effect=write))

    def test_a_reserved_successor_may_still_change_coordinator_or_rebind(self):
        """A handoff or a runtime rebind moves authority, not work, so a new coordinator can finish the claim."""
        self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        handoff = {"provenance": "user_direct", "instruction": "Hand over", "objective": "successor",
                   "from_owner": "next", "to_owner": "next", "run": "run-s", "recorded_generation": 1,
                   "recorded_runtime": "runtime-new", "current_runtime": "runtime-new", "ambiguity": []}
        rebind = {"provenance": "user_direct", "instruction": "Rebind", "objective": "successor",
                  "recorded_runtime": "runtime", "current_runtime": "runtime-new", "ambiguity": ["x"]}
        # Each reaches its own fact checks, past the reservation; the closure still refuses.
        self.refused(lambda: self.mine("owner-handoff", decision=handoff), code="handoff_decision_mismatch")
        self.refused(lambda: self.mine("runtime-continuity", decision=rebind),
                     code="runtime_continuity_decision_mismatch")
        self.refused(self.close_successor, code="claim_incomplete")

    def test_a_reservation_whose_claim_cannot_complete_is_released(self):
        self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patch("pod.ledger._write_succession", side_effect=KeyboardInterrupt("reserved")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        self.terminals["owner"] = "live"  # The predecessor's coordinator returns before the claim links it.
        detected = self.claim()
        self.assertEqual((detected["status"], detected["reserved"]), ("claim_unavailable", True))
        # Only the decision that made the reservation releases it; any other refuses and writes nothing.
        before = self.snapshot()
        others = {"predecessor": "elsewhere", "successor": "elsewhere", "predecessor_owner": "someone",
                  "successor_owner": "someone", "runs": ["run-x"], "ambiguity": ["x"],
                  "carried": decision["carried"][:-1]}
        for field, value in others.items():
            with self.subTest(field=field):
                self.refused(lambda: self.claim({**decision, field: value}), code="succession_decision_mismatch")
                self.assertEqual(before, self.snapshot())
        released = self.refused(lambda: self.claim(decision), code=detected["condition"])
        self.assertIn("reservation was released", str(released))
        self.assertNotIn("continues", read(self.project, "successor"))
        self.assertNotIn("continued_by", read(self.project, "objective"))
        self.close_successor()  # Released, the successor works and closes as its own objective again.
        self.assertTrue(read(self.project, "successor")["checkpoint"]["closure"])

    def test_an_objective_named_by_an_incomplete_earlier_claim_is_never_claimed(self):
        """Synthetic control: a claim an earlier Pod left after the predecessor's link, without a reservation."""
        self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        from pod import ledger
        state = read(self.project, "successor")
        state.pop("continues")
        ledger._write(ledger._path(self.project, "successor"), state)
        self.terminals["next"] = "exited"
        self.run_holders["run-s"] = "next"
        self.successor(name="third", caller="third", run_id="run-t")
        before = self.snapshot()
        refused = self.claim(name="third", caller="third", predecessor="successor")
        self.assertEqual(refused["condition"], "predecessor_claim_incomplete", refused.get("message"))
        self.assertIn("claim on objective objective is incomplete", refused["message"])
        self.assertEqual(before, self.snapshot())

    def earlier_shape(self):
        """Synthetic control: a claim an earlier Pod left after the predecessor's link, without a reservation."""
        self.lost_with(self.publications)
        self.successor()
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(self.decide())
        from pod import ledger
        state = read(self.project, "successor")
        state.pop("continues")
        ledger._write(ledger._path(self.project, "successor"), state)

    def test_closure_proves_no_claim_names_a_successor_that_already_started(self):
        """Work an earlier Pod let it start does not prove the absence of the claim: closure reads for it."""
        self.earlier_shape()
        with patch("pod.succession.refuse_incomplete"):  # An earlier Pod's work, which did not check.
            self.mine("governor-prepare", unit="own", branch={"remote": "origin", "branch": "own", "base": "target"})
        before = self.snapshot()
        self.refused(self.close_successor, code="claim_incomplete")
        self.assertEqual(before, self.snapshot())

    def test_an_unreadable_record_holds_an_unstarted_objective_and_any_closure(self):
        """While the scan cannot read a record, an objective its own records do not prove started does no work, which
        would start it; a started one works on, and closure asks the proof of either."""
        self.lost_with(lambda: None)
        self.successor()
        broken = state_root(self.project) / "zz-unreadable"
        broken.mkdir()
        (broken / "context.json").write_text("{")
        prepare = self.own_work()["prepare"]
        before = self.snapshot()
        refused = self.refused(prepare, code="claim_unverified")
        self.assertEqual((refused.detail["unreadable"], refused.detail["next_action"]),
                         ([str(broken)], "repair or remove the unreadable record, then retry"))
        self.assertEqual(before, self.snapshot())
        (broken / "context.json").unlink()
        prepare()
        (broken / "context.json").write_text("{")
        self.mine("checkpoint", value=self.core(
            obligations=read(self.project, "successor")["checkpoint"]["obligations"]))  # Started, it works on.
        self.refused(self.close_successor, code="claim_unverified")
        (broken / "context.json").unlink()
        broken.rmdir()
        self.close_successor()
        self.assertTrue(read(self.project, "successor")["checkpoint"]["closure"])

    def test_a_repeated_close_answers_from_the_recorded_closure(self):
        """R117/A300: an unreadable record never changes the answer once closure is recorded."""
        self.lost_with(lambda: None)
        self.successor()
        self.close_successor()
        broken = state_root(self.project) / "zz-unreadable"
        broken.mkdir()
        (broken / "context.json").write_text("{")
        before = self.snapshot()
        repeated = self.refused(self.close_successor, code="objective_closed")
        self.assertEqual((repeated.detail["finalization"]["finished"], repeated.detail["finalization"]["remaining"]),
                         (True, []))
        self.assertEqual(before, self.snapshot())

    def test_a_replay_refuses_a_successor_that_changed_without_a_reservation(self):
        """Synthetic control: an interrupted claim recorded without the reservation, then the successor closed by a
        Pod that did not check for it."""
        self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        from pod import ledger
        path = ledger._path(self.project, "successor")
        state = read(self.project, "successor")
        state.pop("continues")
        ledger._write(path, state)
        self.refused(self.close_successor, code="claim_incomplete")  # This version refuses that closure.
        with patch("pod.succession.refuse_incomplete"):
            self.close_successor()
        before = self.snapshot()
        refused = self.refused(lambda: self.claim(decision), code="successor_started")
        self.assertIn("the successor is closed or delivered", str(refused))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.journal("successor")["actions"], [])


class CapacityTests(CarriedCase):
    """R118: a claim too large for the successor's journal is refused before any write, with a supported route."""

    def near_limit(self, built):
        """Requested dispatches with bounded valid inputs until the predecessor's journal nearly fills its limit."""
        from pod.governor import MAX_JOURNAL
        self.op("governor-execute", action=self.action("push"))
        for index in range(40):
            action = self.action("workflow_dispatch", target=f"capacity{index}.yml", reason="r" * 512)
            action["evidence"] = ["e" * 512 for _ in range(16)]
            action["inputs"] = {f"input{i}": "v" * 512 for i in range(8)}
            try:
                built.append(self.op("governor-execute", action=action)["record_id"])
            except PodError as exc:
                if exc.code != "record_too_large":
                    raise
                # The refused write preceded the provider call; trimming only caller evidence fits the last row.
                excess = exc.detail["proposed_bytes"] - MAX_JOURNAL + 256
                for position, item in enumerate(action["evidence"]):
                    cut = min(excess, len(item) - 1)
                    action["evidence"][position], excess = item[cut:], excess - cut
                built.append(self.op("governor", action=action)["record_id"])
                return built
        raise AssertionError("the journal never reached its limit")

    def test_a_claim_too_large_for_the_successor_writes_nothing(self):
        built = self.lost_with(lambda: self.near_limit([]))
        self.successor()
        effects, before = self.writes(), self.snapshot()
        decision = self.decide()
        refused = self.refused(lambda: self.claim(decision), code="claim_capacity")
        self.assertEqual((refused.detail["record"], refused.detail["limit_bytes"]),
                         ("successor's Governor journal", 262144))
        self.assertGreater(refused.detail["proposed_bytes"], 262144)
        self.assertIn("owner handoff of the predecessor", refused.detail["next_action"])
        self.assertEqual(before, self.snapshot())  # Neither frozen nor reserved.
        self.assertNotIn("continued_by", read(self.project, "objective"))
        self.assertNotIn("continues", read(self.project, "successor"))
        self.assertEqual(self.writes(), effects)
        self.assertEqual(len(built), len(decision["carried"]))

    def test_settling_in_the_predecessor_lets_a_just_fitting_claim_complete(self):
        built = self.lost_with(lambda: self.near_limit([]))
        self.successor()
        self.refused(lambda: self.claim(self.decide()), code="claim_capacity")
        # The supported route: the predecessor's coordinator settles a row by readback, then is lost again.
        self.returning()
        settled = self.row(built[0], "objective")["receipt"]["provider"]["run_id"]
        self.remote.complete(settled, "success")
        self.assertEqual(self.op("governor-reconcile", record_id=built[0])["status"], "PASS")
        self.lose()
        self.act_successor()
        decision = self.decide()
        self.assertEqual(self.claim(decision)["status"], "claimed")
        carried = [row["record_id"] for row in self.journal("successor")["actions"]]
        self.assertEqual(sorted(carried), sorted(built[1:]))
        size = _record_path(self.project, "successor").stat().st_size
        self.assertGreater(size, 262144 * 0.9)  # It fits, and only just.

    def act_successor(self):
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.current = {"id": "run-s", "coordinator_handle": "next", "consumer_generation": 1}

    def test_a_reservation_that_cannot_fit_is_released_by_its_decision(self):
        """Synthetic control: a reservation made before the predecessor grew, resumed once the claim no longer fits."""
        self.lost_with(self.publications)
        self.successor()
        decision = self.decide()
        with patch("pod.ledger._write_succession", side_effect=KeyboardInterrupt("reserved")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision)
        too_large = PodError("claim_capacity", "The claim cannot fit.", {"record": "successor's Governor journal"})
        with patch("pod.succession._require_fit", side_effect=too_large):
            refused = self.refused(lambda: self.claim(decision), code="claim_capacity")
        self.assertIn("reservation was released", str(refused))
        self.assertNotIn("continues", read(self.project, "successor"))
        self.assertNotIn("continued_by", read(self.project, "objective"))

    def test_a_claim_linked_before_the_check_refuses_its_replay_by_name(self):
        """Synthetic control: an earlier candidate froze the predecessor and then failed to copy its rows."""
        self.lost_with(lambda: self.near_limit([]))
        self.successor()
        decision = self.decide()
        with patch("pod.succession._require_fit"):
            self.refused(lambda: self.claim(decision), code="record_too_large")
        self.assertEqual(read(self.project, "objective")["continued_by"]["objective"], "successor")
        before = self.snapshot()
        refused = self.refused(lambda: self.claim(decision), code="claim_capacity")
        self.assertIn("no supported route completes a claim linked before this check", refused.detail["next_action"])
        self.assertEqual(before, self.snapshot())


class SettlementTests(CarriedCase):
    """PoD#4 — readback settles carried rows and nothing is resubmitted; absence waits for its margin."""

    def test_readback_settles_carried_publications_including_a_merged_pull_request(self):
        built = self.lost_with(self.publications)
        effects = self.writes()
        self.carry()
        first = self.row(built["pr_update"])["commit"]
        self.assertEqual(self.mine("governor-reconcile", record_id=built["push"])["status"], "PASS")
        held = self.mine("governor-reconcile", record_id=built["derived"])
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        run_id = self.remote._start("ci.yml", first, event="pull_request")["id"]
        self.remote.complete(run_id, "success")
        self.assertEqual(self.mine("governor-reconcile", record_id=built["derived"])["status"], "PASS")
        # The pull request merged and its branch was deleted: the carried update reads the pull request back.
        self.remote.heads.pop("origin/delivery")
        self.remote.pr_history[("delivery", "target")] = [{
            "number": 7, "url": "https://example.invalid/pull/7", "head_sha": first, "state": "MERGED",
            "merge_commit": "9" * 40, "merged_at": "2026-01-01T00:00:00Z"}]
        for outcome in ("CANCELED", "FAILED"):
            self.refused(lambda: self.mine("governor-outcome", record_id=built["pr_update"], outcome=outcome,
                                           detail="it never ran"), code="outcome_evidence_required")
        recorded = self.mine("governor-outcome", record_id=built["pr_update"], outcome="PASS")
        self.assertEqual((recorded["outcome"], recorded["receipt"]["provider"]["pr"],
                          recorded["receipt"]["provider"]["pr_state"]), ("PASS", 7, "MERGED"))
        self.assertEqual(self.writes(), effects)
        settled = {row["record"]: row["outcome"] for row in self.status()["succession"]["carried"]["rows"]}
        self.assertEqual(settled, {record_id: "PASS" for record_id in built.values()})

    def test_carried_rows_are_never_canceled_and_their_absence_waits_for_the_margin(self):
        def build():
            self.op("governor-execute", action=self.action("push"))
            self.renew()
            return {"push": self.op("governor", action=self.action("push"))["record_id"]}
        built = self.lost_with(build)
        self.carry()
        row = self.row(built["push"])
        refused = self.refused(lambda: self.mine("governor-outcome", record_id=built["push"], outcome="CANCELED",
                                                 detail="never pushed"), code="outcome_evidence_required")
        self.assertEqual(refused.detail["detail"], "carried_row")
        carried = self.mine("governor-execute", action=self.action("push"))
        self.assertEqual((carried["decision"], carried["reasons"][0]["code"]), ("DEFER", "authorization_missing"))
        self.assertIn("carried from objective objective and authorizes no new effect", carried["reasons"][0]["detail"])
        fresh = {**self.authorization, "authorized_by": "owner in the successor's session", "reference": "fresh"}
        attached = self.mine("governor-execute", action=self.action("push", authorization=fresh))
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]), ("attach", built["push"]))
        self.assertIn("carried from objective objective and settles only by Pod's own readback",
                      attached["next_action"])
        started = datetime.fromisoformat(row["receipt"]["started_at"])
        early = reconcile(self.project, "successor", owner="next", record_id=built["push"], port=self.remote,
                          now=started + timedelta(seconds=60))
        self.assertEqual((early["status"], early["action"]), ("pending", "hold"))
        self.assertIn("a carried effect's absence is not read as FAILED before", early["reason"])
        late = reconcile(self.project, "successor", owner="next", record_id=built["push"], port=self.remote,
                         now=started + timedelta(seconds=720))
        self.assertEqual(late["status"], "FAILED")
        self.assertFalse([call for call in self.writes() if call[3] == row["commit"]])

    def test_a_successor_never_cancels_a_carried_validation(self):
        def build():
            self.op("governor-execute", action=self.action("push"))
            return {"dispatch": self.op("governor-execute", action=self.action(
                "workflow_dispatch", target="release.yml"))["record_id"]}
        built = self.lost_with(build)
        self.carry()
        fresh = {**self.authorization, "authorized_by": "owner in the successor's session", "reference": "fresh"}
        held = self.mine("governor", action=self.action("cancel_validation", target=built["dispatch"],
                                                         authorization=fresh))
        self.assertEqual((held["decision"], held["reasons"][0]["code"]), ("DEFER", "carried_row"))
        # A new candidate generation supersedes the carried run without cancelling it.
        self.move("README.md")
        self.mine("checkpoint", value=self.core(obligations=read(self.project, "successor")["checkpoint"]
                                                ["obligations"]))
        renewed = self.mine("governor-prepare", unit="default",
                            branch={"remote": "origin", "branch": "delivery", "base": "target"})
        self.assertEqual(renewed["superseded_validation"], [])
        self.assertFalse([call for call in self.writes() if call[0] == "cancel"])


class CarriedSettlementTests(CarriedCase):
    """PoD#4 — a carried update settles only from its pull request; a carried cancellation from its own run."""

    def test_a_carried_pull_request_update_settles_only_from_its_pull_request(self):
        def pushed_then_reserved():
            self.op("governor-execute", action=self.action("push"))
            return self.op("governor", action=self.action("pr_update"))["record_id"]
        record_id = self.lost_with(pushed_then_reserved)
        effects = self.writes()
        self.carry()
        commit = self.row(record_id)["commit"]
        held = self.mine("governor-reconcile", record_id=record_id)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn("proves a push, not the update", held["reason"])
        unrelated = [{"number": number, "url": "u", "head_sha": "8" * 40, "state": "CLOSED", "merge_commit": None,
                      "merged_at": None} for number in range(10)]
        mine = {"number": 9, "url": "https://example.invalid/pull/9", "head_sha": commit, "state": "OPEN",
                "merge_commit": None, "merged_at": None}
        self.remote.pr_history[("delivery", "target")] = unrelated + [mine]
        truncated = self.mine("governor-reconcile", record_id=record_id)
        self.assertEqual((truncated["status"], truncated["action"]), ("pending", "hold"))
        self.assertIn("were truncated", truncated["reason"])
        self.remote.pr_history[("delivery", "target")] = [mine]
        settled = self.mine("governor-reconcile", record_id=record_id)
        self.assertEqual((settled["status"], settled["receipt"]["provider"]["pr"]), ("PASS", 9))
        self.assertEqual(self.writes(), effects)

    def test_a_carried_cancellation_settles_whether_or_not_its_target_was_carried(self):
        def reserved_cancellation():
            self.op("governor-execute", action=self.action("push"))
            settled = self.op("governor-execute", action=self.action("workflow_dispatch", target="nightly.yml"))
            pending = self.op("governor-execute", action=self.action("workflow_dispatch", target="release.yml"))
            cancels = {name: self.op("governor", action=self.action("cancel_validation", target=row["record_id"]))
                       for name, row in (("settled", settled), ("pending", pending))}
            for row in (settled, pending):
                self.remote.complete(row["receipt"]["provider"]["run_id"], "cancelled")  # An outside actor's.
            self.assertEqual(self.op("governor-reconcile", record_id=settled["record_id"])["status"], "CANCELED")
            return {"settled": settled["record_id"], "pending": pending["record_id"],
                    **{"cancel_" + name: row["record_id"] for name, row in cancels.items()}}
        built = self.lost_with(reserved_cancellation)
        effects = self.writes()
        _, claimed = self.carry()
        self.assertNotIn(built["settled"], claimed["carried"])
        self.assertIn(built["pending"], claimed["carried"])
        for name in ("cancel_settled", "cancel_pending"):
            with self.subTest(cancellation=name):
                self.assertEqual(self.mine("governor-reconcile", record_id=built[name])["status"], "PASS")
        # The carried target is never settled by the cancellation's assertion; its own readback settles it.
        self.assertEqual(self.row(built["pending"])["outcome"], "pending")
        target = self.mine("governor-reconcile", record_id=built["pending"])
        self.assertEqual((target["status"], target["receipt"]["provider"]["conclusion"]), ("CANCELED", "cancelled"))
        self.assertEqual(self.writes(), effects)


class CarriedChainTests(CarriedCase):
    """PoD#4 — a predecessor reads each carried row where it now stands, also after a second claim."""

    def test_the_predecessor_follows_a_row_carried_again_and_keeps_an_earlier_settled_one(self):
        built = self.lost_with(self.publications)
        self.carry()
        self.assertEqual(self.mine("governor-reconcile", record_id=built["push"])["status"], "PASS")
        self.terminals["next"] = "exited"  # The first successor's coordinator is lost in turn.
        self.run_holders["run-s"] = "next"
        self.successor(name="third", caller="third", run_id="run-t")
        decision = {"provenance": "user_direct", "instruction": "Continue the interrupted successor",
                    **self.claim(name="third", caller="third", predecessor="successor")["facts"]}
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision, name="third", caller="third", predecessor="successor")
        rows = {row["record"]: row for row in self.status()["succession"]["carried"]["rows"]}
        self.assertEqual((rows[built["push"]]["outcome"], rows[built["push"]]["held_by"]), ("PASS", "successor"))
        for record in (built["derived"], built["pr_update"]):
            self.assertIsNone(rows[record]["outcome"])
            self.assertIn("has not recorded this row", rows[record]["reason"])
        self.claim(decision, name="third", caller="third", predecessor="successor")
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        run_id = self.remote._start("ci.yml", self.row(built["derived"], "third")["commit"], event="pull_request")["id"]
        self.remote.complete(run_id, "success")
        self.assertEqual(reconcile(self.project, "third", owner="third", record_id=built["derived"],
                                   port=self.remote)["status"], "PASS")
        carried = self.status()["succession"]["carried"]
        rows = {row["record"]: row for row in carried["rows"]}
        self.assertEqual({record: (row["outcome"], row["held_by"]) for record, row in rows.items()},
                         {built["push"]: ("PASS", "successor"), built["derived"]: ("PASS", "third"),
                          built["pr_update"]: ("pending", "third")})
        self.assertEqual(carried["unsettled"], 1)


class CarriedRunTests(CarriedCase):
    """PoD#4 with R115: carried runs settle on the same exact provenance as the successor's own."""

    def test_a_carried_rerun_reads_its_own_attempt_and_a_carried_dispatch_its_own_run(self):
        built = self.lost_with(self.runs)
        effects = self.writes()
        self.carry()
        rerun = self.row(built["validation_rerun"])
        run_id = rerun["receipt"]["provider"]["run_id"]
        self.assertEqual(rerun["receipt"]["provider"]["attempt"], 2)
        self.remote.complete(run_id, "failure")
        self.remote.rerun(run_id=run_id, failed_only=True)  # Another actor's attempt 3 passes.
        self.remote.complete(run_id, "success")
        settled = self.mine("governor-reconcile", record_id=built["validation_rerun"])
        self.assertEqual((settled["status"], settled["receipt"]["provider"]["attempt"]), ("FAILED", 2))
        nightly = self.row(built["workflow_dispatch"])["receipt"]["provider"]["run_id"]
        self.remote.complete(nightly, "success")
        self.assertEqual(self.mine("governor-reconcile", record_id=built["workflow_dispatch"])["status"], "PASS")
        # The only provider write after the claim is the other actor's rerun this test simulated; Pod made none.
        self.assertEqual(self.writes(), effects + [("rerun", run_id, True)])


class CarriedUnconfirmedRerunTests(CarriedCase):
    """PoD#4 with R115's strict rule: a carried rerun whose acceptance was never recorded never binds an attempt."""

    def test_a_landed_rerun_whose_reply_was_lost_holds_and_blocks_the_successors_closure(self):
        record_id = self.lost_with(lambda: self.lost_reply_rerun(landed=True))
        effects = self.writes()
        self.carry()
        carried = self.row(record_id)
        provider = carried["receipt"]["provider"]
        self.assertEqual((carried["outcome"], provider["before_attempt"]), ("UNKNOWN", 1))
        self.assertNotIn("accepted", provider)
        self.remote.complete(provider["run_id"], "success")
        held = self.mine("governor-reconcile", record_id=record_id)
        self.assertEqual((held["status"], held["action"]), ("UNKNOWN", "hold"))
        self.assertIn("never confirmed accepted", held["reason"])
        self.assertIn("never confirmed accepted", self.row(record_id)["readback"]["reason"])
        blocked = self.refused(self.close_successor)
        self.assertEqual(blocked.detail["detail"], "closure_unfinished")
        self.assertEqual(self.writes(), effects)

    def test_a_rerun_that_never_landed_reads_failed_only_after_the_absence_floor(self):
        record_id = self.lost_with(lambda: self.lost_reply_rerun(landed=False))
        self.carry()
        submitted = datetime.fromisoformat(self.row(record_id)["receipt"]["provider"]["submitted_at"])
        early = reconcile(self.project, "successor", owner="next", record_id=record_id, port=self.remote,
                          now=submitted + timedelta(seconds=300))
        self.assertEqual((early["status"], early["action"]), ("UNKNOWN", "hold"))
        late = reconcile(self.project, "successor", owner="next", record_id=record_id, port=self.remote,
                         now=submitted + timedelta(seconds=721))
        self.assertEqual(late["status"], "FAILED")
        self.assertIn("the unconfirmed rerun did not take effect", late["receipt"]["detail"])
        self.assertFalse([call for call in self.remote.calls if call[0] == "rerun"])


class MergeTests(CarriedCase):
    """PoD#4 — a carried merge stays pending while its pull request is open and settles PASS only by R93."""

    def test_open_then_merged_pull_request_settles_by_r93_and_closes_the_successor(self):
        built = self.lost_with(self.merge_decision)
        effects = self.writes()
        self.carry()
        merge = built["merge"]
        self.pull_request("OPEN")
        held = self.mine("governor-reconcile", record_id=merge)
        self.assertIn("pull request #9 is open; the merge stays pending", held["reason"])
        blocked = self.refused(self.close_successor)
        self.assertEqual(blocked.detail["detail"], "closure_unfinished")
        self.assertEqual(blocked.detail["finalization"]["remaining"][0], "merge_outcome")
        # The carried consent records a merge that may already have happened; it never authorizes a new one.
        carried = self.mine("governor", action={"kind": "merge", "candidate": self.candidate, "target": "origin/target",
                                                "reason": "deliver", "unit": "default", "effects": [],
                                                "authorization": self.consent})
        self.assertEqual((carried["decision"], carried["reasons"][0]["code"]), ("DEFER", "authorization_missing"))
        fresh = {**self.consent, "authorized_by": "owner in the successor's session", "reference": "fresh-consent"}
        attached = self.mine("governor", action={"kind": "merge", "candidate": self.candidate, "target": "origin/target",
                                                 "reason": "deliver", "unit": "default", "effects": [],
                                                 "authorization": fresh})
        self.assertEqual((attached["decision"], attached["reuse"]["kind"], attached["record_id"]),
                         ("REUSE", "attach", merge))
        result = self.merged(fetched=False)
        self.pull_request("MERGED", result)
        unfetched = self.mine("governor-reconcile", record_id=merge)
        self.assertEqual(unfetched["status"], "pending")
        self.assertIn("fetch first (git fetch origin target)", unfetched["reason"])
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        settled = self.mine("governor-reconcile", record_id=merge)
        provider = settled["receipt"]["provider"]
        self.assertEqual((settled["status"], provider["merge_commit"], provider["method"], provider["pr"]),
                         ("PASS", result, "merge", 9))
        self.mine("checkpoint", value=self.core(obligations=read(self.project, "successor")["checkpoint"]
                                                ["obligations"], delivery={"record": merge}))
        self.assertEqual(read(self.project, "successor")["checkpoint"]["delivery"]["result"], result)
        from pod.governor import objective_finalization
        self.assertEqual(objective_finalization(self.project, "successor")["remaining"], ["closure"])
        self.assertIsNotNone(read(self.project, "successor")["checkpoint"]["delivery"])
        self.close_successor()
        self.assertTrue(read(self.project, "successor")["checkpoint"]["closure"])
        self.assertEqual(self.writes(), effects)
        self.assertEqual(int(git(self.project, "rev-list", "--merges", "--count", "refs/remotes/origin/target")), 1)

    def test_a_truncated_pull_request_list_never_settles_a_carried_merge(self):
        built = self.lost_with(self.merge_decision)
        self.carry()
        result = self.merged(fetched=True)
        self.pull_request("MERGED", result)
        older = [{"number": number, "url": "u", "head_sha": "8" * 40, "state": "CLOSED", "merge_commit": None,
                  "merged_at": None} for number in range(10)]
        self.remote.pr_history[("delivery", "target")] = older + self.remote.pr_history[("delivery", "target")]
        held = self.mine("governor-reconcile", record_id=built["merge"])
        self.assertEqual(held["status"], "pending")
        self.assertIn("were truncated", held["reason"])

    def manual(self, record_id, outcome, *, at, **fields):
        """The successor coordinator's governor-outcome for a carried merge, at a chosen moment."""
        return record_outcome(self.project, "successor", owner="next", record_id=record_id, outcome=outcome,
                              port=self.remote, now=at, **fields)

    def test_a_manual_outcome_of_a_carried_merge_holds_like_its_readback(self):
        """governor-outcome reads the pull request back as reconcile does: open, missing or truncated holds."""
        built = self.lost_with(self.merge_decision)
        effects = self.writes()
        self.carry()
        merge = built["merge"]
        late = datetime.fromisoformat(self.row(merge)["receipt"]["started_at"]) + timedelta(seconds=721)
        self.pull_request("OPEN")
        mine = self.remote.pr_history[("delivery", "target")]
        older = [{"number": number, "url": "u", "head_sha": "8" * 40, "state": "CLOSED", "merge_commit": None,
                  "merged_at": None} for number in range(10)]
        for name, history, reason in (("open", mine, "is open"), ("missing", [], "no pull request with head"),
                                      ("truncated", older + mine, "were truncated")):
            self.remote.pr_history[("delivery", "target")] = history
            for outcome in ("FAILED", "PASS"):
                with self.subTest(history=name, outcome=outcome):
                    refused = self.refused(lambda: self.manual(merge, outcome, at=late),
                                           code="outcome_evidence_required")
                    self.assertEqual(refused.detail["detail"], "readback_disagrees")
                    self.assertIn(reason, str(refused))
        receipt = self.refused(lambda: self.manual(merge, "PASS", at=late, provider={"merge_commit": "a" * 40}),
                               code="outcome_evidence_required")
        self.assertEqual(receipt.detail["detail"], "receipt_from_readback")
        self.assertEqual(self.row(merge)["outcome"], "pending")
        self.assertEqual(self.writes(), effects)

    def test_a_manual_failed_carried_merge_needs_a_closed_pull_request_past_the_margin(self):
        built = self.lost_with(self.merge_decision)
        self.carry()
        merge = built["merge"]
        self.pull_request("CLOSED")
        started = datetime.fromisoformat(self.row(merge)["receipt"]["started_at"])
        early = self.refused(lambda: self.manual(merge, "FAILED", at=started + timedelta(seconds=30)),
                             code="outcome_evidence_required")
        self.assertIn("a carried effect's absence is not read as FAILED before", str(early))
        recorded = self.manual(merge, "FAILED", at=started + timedelta(seconds=721))
        self.assertEqual(recorded["outcome"], "FAILED")
        self.assertIn("closed without merging", recorded["receipt"]["detail"])

    def test_a_closed_pull_request_fails_only_after_the_margin_and_with_git_evidence(self):
        built = self.lost_with(self.merge_decision)
        self.carry()
        merge = built["merge"]
        self.pull_request("CLOSED")
        started = datetime.fromisoformat(self.row(merge)["receipt"]["started_at"])
        early = reconcile(self.project, "successor", owner="next", record_id=merge, port=self.remote,
                          now=started + timedelta(seconds=30))
        self.assertIn("a carried effect's absence is not read as FAILED before", early["reason"])
        late = reconcile(self.project, "successor", owner="next", record_id=merge, port=self.remote,
                         now=started + timedelta(seconds=721))
        self.assertEqual(late["status"], "FAILED")
        self.assertIn("closed without merging", late["receipt"]["detail"])


# R118's chain invariant over every sibling state: how many claims the rows moved through, where the last claim
# stopped, and which kinds of row the predecessor left unsettled.
CHAIN_STOPS = {
    "before_link": "pod.ledger._write_succession",  # After the successor's reservation.
    "after_link": "pod.succession._carry",  # After the predecessor's link, before the rows.
    "after_rows": "pod.ledger._write",  # After the rows, before the successor's link completes.
}
CHAIN_ROWS = [(depth, stop, kinds) for depth in (1, 2, 3) for stop in CHAIN_STOPS
              for kinds in ("publications", "runs", "merge")]


class ChainCase(CarriedCase):
    """A predecessor's unsettled rows carried along one to three claims, the last one interrupted (R118)."""

    HOPS = (("successor", "next", "run-s"), ("third", "third", "run-t"), ("fourth", "fourth", "run-f"))

    def setUp(self):
        super().setUp()
        self.terminals.update(fourth="live", other="live")

    def act(self, caller, run_id):
        """Calls from here on come from that terminal on that Run."""
        os.environ["ORCA_TERMINAL_HANDLE"] = caller
        self.current = {"id": run_id, "coordinator_handle": caller, "consumer_generation": 1}

    def runs_rows(self):
        """A rerun, a requested dispatch, and cancellations of a pending and of an already settled validation."""
        self.failed_release()
        rerun = self.op("governor-execute", action=self.action("validation_rerun", target="release.yml"))
        nightly = self.op("governor-execute", action=self.action("workflow_dispatch", target="nightly.yml"))
        weekly = self.op("governor-execute", action=self.action("workflow_dispatch", target="weekly.yml"))
        pending = self.op("governor", action=self.action("cancel_validation", target=nightly["record_id"]))
        settled = self.op("governor", action=self.action("cancel_validation", target=weekly["record_id"]))
        self.weekly_run = weekly["receipt"]["provider"]["run_id"]
        self.remote.complete(self.weekly_run, "cancelled")  # An outside actor's.
        self.assertEqual(self.op("governor-reconcile", record_id=weekly["record_id"])["status"], "CANCELED")
        return {"rerun": rerun["record_id"], "requested": nightly["record_id"],
                "cancel_with_target": pending["record_id"], "cancel_without_target": settled["record_id"]}

    def merge_rows(self):
        """A merge decision, then the merge lands and the target advances before any successor checkpoints."""
        self.admitted_base = git(self.project, "rev-parse", "refs/remotes/origin/target")
        built = self.merge_decision()
        self.pull_request("MERGED", self.merged(fetched=True))
        return built

    def close(self, name, caller, run_id):
        self.act(caller, run_id)
        rows = read(self.project, name)["checkpoint"]["obligations"]
        for row in rows:
            row.pop("executor", None)
            row.update(state="satisfied", evidence=[{"check": "criterion", "command": "unit", "result": "passed",
                                                     "reference": "successor-proof"}])
        return run("checkpoint", {"project": str(self.project), "objective": name, "owner": caller,
                                  "value": self.core(obligations=rows, close=True)})

    def claimed(self, depth, stop):
        """Claims along the chain, each earlier successor lost in turn; the last claim stops at `stop`."""
        predecessor = "objective"
        for index, (name, caller, run_id) in enumerate(self.HOPS[:depth]):
            self.successor(name=name, caller=caller, run_id=run_id)
            facts = self.claim(name=name, caller=caller, predecessor=predecessor)["facts"]
            decision = {"provenance": "user_direct", "instruction": "Continue the lost objective", **facts}
            if index < depth - 1:
                self.claim(decision, name=name, caller=caller, predecessor=predecessor)
                self.terminals[caller] = "exited"
                self.run_holders[run_id] = caller
                predecessor = name
                continue
            from pod import ledger
            target, real = CHAIN_STOPS[stop], ledger._write

            def completing(path, value):
                if "state" not in value.get("continues", {"state": None}):
                    raise KeyboardInterrupt("after the rows")
                return real(path, value)
            stopped = patch(target, side_effect=completing if stop == "after_rows" else KeyboardInterrupt(stop))
            with stopped, self.assertRaises(KeyboardInterrupt):
                self.claim(decision, name=name, caller=caller, predecessor=predecessor)
            return name, caller, run_id, predecessor, decision

    def read_back_all(self, kinds, built, holder, caller):
        """Make the provider show each original effect, then read each back in the final successor."""
        if kinds == "publications":
            first = self.row(built["pr_update"], holder)["commit"]
            self.remote.pr_history[("delivery", "target")] = [{
                "number": 7, "url": "https://example.invalid/pull/7", "head_sha": first, "state": "OPEN",
                "merge_commit": None, "merged_at": None}]
            self.remote.complete(self.remote._start("ci.yml", self.row(built["derived"], holder)["commit"],
                                                    event="pull_request")["id"], "success")
            expected = {"push": "PASS", "pr_update": "PASS", "derived": "PASS"}
        elif kinds == "runs":
            rerun = self.row(built["rerun"], holder)["receipt"]["provider"]["run_id"]
            self.remote.complete(rerun, "success")
            self.remote.complete(self.row(built["requested"], holder)["receipt"]["provider"]["run_id"], "cancelled")
            expected = {"rerun": "PASS", "cancel_with_target": "PASS", "cancel_without_target": "PASS",
                        "requested": "CANCELED"}
        else:
            expected = {"merge": "PASS"}
        settled = {}
        for name in expected:  # A carried cancellation reads back before the carried target it names.
            settled[name] = run("governor-reconcile", {"project": str(self.project), "objective": holder,
                                                       "owner": caller, "record_id": built[name]})["status"]
        return settled, expected

    def chain_row(self, depth, stop, kinds):
        built = self.lost_with({"publications": self.publications, "runs": self.runs_rows,
                                "merge": self.merge_rows}[kinds])
        effects = self.writes()
        name, caller, run_id, predecessor, decision = self.claimed(depth, stop)
        # The interrupted claim reserves its successor: it neither works nor closes, and nothing can claim it.
        self.refused(lambda: self.close(name, caller, run_id), code="claim_incomplete")
        self.successor(name="other", caller="other", run_id="run-o")
        before = self.snapshot()
        refused = self.claim(name="other", caller="other", predecessor=name)
        self.assertEqual(refused["condition"], "predecessor_claim_incomplete", refused.get("message"))
        self.assertIn(f"objective {name} with predecessor {predecessor}", refused["next_action"])
        self.assertEqual(before, self.snapshot())
        # The same decision completes it; the rows then block its closure until each reads back.
        self.act(caller, run_id)
        self.assertEqual(self.claim(decision, name=name, caller=caller, predecessor=predecessor)["status"], "claimed")
        self.assertEqual(self.refused(lambda: self.close(name, caller, run_id)).detail["detail"], "closure_unfinished")
        first = {row["record_id"]: row["carried_from"] for row in self.journal("successor")["actions"]}
        for row in self.journal(name)["actions"]:
            kept = {key: value for key, value in row["carried_from"].items() if key not in CHAIN_FIELDS}
            self.assertEqual(kept, {key: value for key, value in first[row["record_id"]].items()
                                    if key not in CHAIN_FIELDS})
            self.assertEqual(row["carried_from"]["objective"], predecessor)
        if kinds == "merge":
            self.assertEqual(self.row(built["merge"], name)["carried_from"]["governance"]["base"], self.admitted_base)
        if kinds == "runs":
            self.assertEqual(self.row(built["cancel_without_target"], name)["carried_from"]["run_id"], self.weekly_run)
        self.act(caller, run_id)
        settled, expected = self.read_back_all(kinds, built, name, caller)
        self.assertEqual(settled, expected)
        self.assertEqual(self.writes(), effects)


class ChainTableTests(GroupedTable):
    """R118's chain invariant: one named group method per row, so each stays within the fast lane's limit."""
    GROUPS = {f"test_{depth}_hop_{stop}_{kinds}": [(depth, stop, kinds)] for depth, stop, kinds in CHAIN_ROWS}

    def row(self, item):
        case = ChainCase()
        case.setUp()
        try:
            with self.subTest(row=item):
                case.chain_row(*item)
        finally:
            case.doCleanups()

    def test_1_hop_before_link_publications(self): self.rows()
    def test_1_hop_before_link_runs(self): self.rows()
    def test_1_hop_before_link_merge(self): self.rows()
    def test_1_hop_after_link_publications(self): self.rows()
    def test_1_hop_after_link_runs(self): self.rows()
    def test_1_hop_after_link_merge(self): self.rows()
    def test_1_hop_after_rows_publications(self): self.rows()
    def test_1_hop_after_rows_runs(self): self.rows()
    def test_1_hop_after_rows_merge(self): self.rows()
    def test_2_hop_before_link_publications(self): self.rows()
    def test_2_hop_before_link_runs(self): self.rows()
    def test_2_hop_before_link_merge(self): self.rows()
    def test_2_hop_after_link_publications(self): self.rows()
    def test_2_hop_after_link_runs(self): self.rows()
    def test_2_hop_after_link_merge(self): self.rows()
    def test_2_hop_after_rows_publications(self): self.rows()
    def test_2_hop_after_rows_runs(self): self.rows()
    def test_2_hop_after_rows_merge(self): self.rows()
    def test_3_hop_before_link_publications(self): self.rows()
    def test_3_hop_before_link_runs(self): self.rows()
    def test_3_hop_before_link_merge(self): self.rows()
    def test_3_hop_after_link_publications(self): self.rows()
    def test_3_hop_after_link_runs(self): self.rows()
    def test_3_hop_after_link_merge(self): self.rows()
    def test_3_hop_after_rows_publications(self): self.rows()
    def test_3_hop_after_rows_runs(self): self.rows()
    def test_3_hop_after_rows_merge(self): self.rows()


class ChainTableShapeTests(unittest.TestCase):
    def test_the_chain_table_runs_every_row_once(self):
        assert_grouped_table(self, ChainTableTests, CHAIN_ROWS, limit=1)


# Every on-disk shape an interrupted claim leaves, by the writer that left it and where it stopped, crossed with the
# route that recovers it (R110, R118): Pod 0.13.1 linked the predecessor and then completed the successor, as 0.10.0
# to 0.13.0 did and 0.13.2 does with the same routine; the earlier PR-B candidate 2b613c8 copied rows in between; this
# version reserves the successor first. The earlier writers are their genuine succession routines from Git objects,
# over the current records.
EARLIER_WRITERS = {"pod-0.13.1": "13f63a1ccb58d3bb74f53efbaeeec7a7df35fcef",
                   "candidate-2b613c8": "2b613c8fe4be566de28ed4149c8998c97d6c160c"}
SHAPE_STOPS = {"pod-0.13.1": ("after_link",), "candidate-2b613c8": ("after_link", "after_rows"),
               "current": ("before_link", "after_link", "after_rows")}
SHAPE_ROWS = [(writer, stop, recovery) for writer, stops in SHAPE_STOPS.items() for stop in stops
              for recovery in ("replay", "handoff", "rebind") + (("release",) if stop == "before_link" else ())]


class ShapeCase(ChainCase):
    """One interrupted claim of each shape, then its successor, a third objective and the recovery routes."""

    _routines = {}

    def routine(self, writer):
        """The genuine succession routine of an earlier writer, from Git objects, over the current records."""
        if writer not in self._routines:
            repository = Path(__file__).resolve().parents[1]
            source = subprocess.run(["git", "-C", str(repository), "show",
                                     EARLIER_WRITERS[writer] + ":skills/pod/succession.py"],
                                    check=True, capture_output=True, text=True).stdout
            module = types.ModuleType("pod._succession_" + writer.replace("-", "_").replace(".", "_"))
            module.__package__ = "pod"
            exec(compile(source, f"succession-{writer}.py", "exec"), module.__dict__)
            self._routines[writer] = module
        return self._routines[writer]

    def interrupted(self, writer, stop):
        """The claim of `objective` by `successor`, as `writer` makes it, stopped at `stop`; returns its decision."""
        from pod import ledger
        claim = (lambda decision=None: self.claim(decision)) if writer == "current" else (
            lambda decision=None: self.routine(writer).succession(
                self.project, "successor", owner="next", predecessor="objective", decision=decision))
        decision = {"provenance": "user_direct", "instruction": "Continue the lost objective", **claim()["facts"]}
        real = ledger._write

        def completing(path, value):
            if "state" not in value.get("continues", {"state": None}):
                raise KeyboardInterrupt("after the rows")
            return real(path, value)
        if stop == "after_rows" or writer == "pod-0.13.1":
            stopped = patch("pod.ledger._write", side_effect=completing)
        elif writer == "current":
            stopped = patch(CHAIN_STOPS[stop], side_effect=KeyboardInterrupt(stop))
        else:
            stopped = patch.object(self.routine(writer), "_carry", side_effect=KeyboardInterrupt(stop))
        with stopped, self.assertRaises(KeyboardInterrupt):
            claim(decision)
        return decision

    def handed_over(self):
        """The owner hands the reserved successor to terminal `third` after its coordinator is lost."""
        self.terminals["next"] = "stale"
        self.act("third", "run-s")
        self.current["coordinator_handle"] = "next"
        verdict = self.status(objective="successor")["owner_handoff"]
        self.assertEqual(verdict["status"], "handoff_available", verdict)

        def moved(argv, **kwargs):
            self.current.update(coordinator_handle="third", consumer_generation=2)
            return {"runtime": self.port.runtime, "exit": 0, "error": None, "result": {}}
        with patch("pod.orca.mutate_command", side_effect=moved):
            run("owner-handoff", {"project": str(self.project), "objective": "successor", "owner": "third",
                                  "decision": {"provenance": "user_direct", "instruction": "Recover it",
                                               **verdict["facts"]}})
        return "third"

    def shape_row(self, writer, stop, recovery):
        built = self.lost_with(lambda: {} if writer == "pod-0.13.1" else self.publications())
        effects = self.writes()
        self.successor()
        decision = self.interrupted(writer, stop)
        # The successor of an incomplete claim, whatever its shape, neither works nor closes, before any write.
        before = self.snapshot()
        self.refused(lambda: self.mine("governor-prepare", unit="own",
                                       branch={"remote": "origin", "branch": "own", "base": "target"}),
                     code="claim_incomplete")
        self.refused(lambda: self.close("successor", "next", "run-s"), code="claim_incomplete")
        self.assertEqual(before, self.snapshot())
        # A third objective never continues it.
        self.successor(name="other", caller="other", run_id="run-o")
        before = self.snapshot()
        refused = self.claim(name="other", caller="other", predecessor="successor")
        self.assertEqual(refused["condition"], "predecessor_claim_incomplete", refused.get("message"))
        self.assertEqual(before, self.snapshot())
        caller = "next"
        self.act("next", "run-s")
        if recovery == "release":
            self.terminals["owner"] = "live"  # The predecessor's coordinator returns before the claim links it.
            self.refused(lambda: self.claim({**decision, "ambiguity": ["x"]}), code="succession_decision_mismatch")
            self.refused(lambda: self.claim(decision), code="predecessor_live")
            self.assertNotIn("continues", read(self.project, "successor"))
            self.close("successor", "next", "run-s")  # Released, it works and closes as its own objective.
            self.assertEqual(self.writes(), effects)
            return
        if recovery == "handoff":
            caller = self.handed_over()
        if recovery == "rebind":
            self.port.runtime = self.port.placement["runtime"] = "runtime-restarted"
        self.assertEqual(self.claim(decision, caller=caller)["status"], "claimed")  # The decision that made it.
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "objective")
        self.assertNotIn("state", read(self.project, "successor")["continues"])
        self.assertEqual(sorted(row["record_id"] for row in self.journal("successor")["actions"]),
                         sorted(built.values()))
        if built:
            blocked = self.refused(lambda: self.close("successor", caller, "run-s"))
            self.assertEqual(blocked.detail["detail"], "closure_unfinished")
        else:
            self.close("successor", caller, "run-s")
        self.assertEqual(self.writes(), effects)


class ShapeTableTests(GroupedTable):
    """R118's incomplete-claim predicate: one named group method per shape and recovery route."""
    GROUPS = {f"test_{writer.replace('-', '_').replace('.', '_')}_{stop}_{recovery}": [(writer, stop, recovery)]
              for writer, stop, recovery in SHAPE_ROWS}

    def row(self, item):
        case = ShapeCase()
        case.setUp()
        try:
            with self.subTest(row=item):
                case.shape_row(*item)
        finally:
            case.doCleanups()

    def test_pod_0_13_1_after_link_replay(self): self.rows()
    def test_pod_0_13_1_after_link_handoff(self): self.rows()
    def test_pod_0_13_1_after_link_rebind(self): self.rows()
    def test_candidate_2b613c8_after_link_replay(self): self.rows()
    def test_candidate_2b613c8_after_link_handoff(self): self.rows()
    def test_candidate_2b613c8_after_link_rebind(self): self.rows()
    def test_candidate_2b613c8_after_rows_replay(self): self.rows()
    def test_candidate_2b613c8_after_rows_handoff(self): self.rows()
    def test_candidate_2b613c8_after_rows_rebind(self): self.rows()
    def test_current_before_link_replay(self): self.rows()
    def test_current_before_link_handoff(self): self.rows()
    def test_current_before_link_rebind(self): self.rows()
    def test_current_before_link_release(self): self.rows()
    def test_current_after_link_replay(self): self.rows()
    def test_current_after_link_handoff(self): self.rows()
    def test_current_after_link_rebind(self): self.rows()
    def test_current_after_rows_replay(self): self.rows()
    def test_current_after_rows_handoff(self): self.rows()
    def test_current_after_rows_rebind(self): self.rows()


class PrelinkDecisionTests(ShapeCase):
    """R118: before the link, a new direct owner decision naming the current owner also completes a reservation."""

    def test_a_new_decision_naming_the_handed_over_owner_completes_the_reservation(self):
        self.lost_with(self.publications)
        self.successor()
        self.interrupted("current", "before_link")
        caller = self.handed_over()
        fresh = self.claim(caller=caller)["facts"]
        self.assertEqual(fresh["successor_owner"], caller)
        renewed = {"provenance": "user_direct", "instruction": "A new owner decision", **fresh}
        before = self.snapshot()
        self.refused(lambda: self.claim({**renewed, "ambiguity": ["x"]}, caller=caller),
                     code="succession_decision_mismatch")  # Neither the reserving decision nor the fresh facts.
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.claim(renewed, caller=caller)["status"], "claimed")
        self.assertEqual(read(self.project, "objective")["continued_by"]["owner"], caller)


class ShapeTableShapeTests(unittest.TestCase):
    def test_the_shape_table_runs_every_row_once(self):
        assert_grouped_table(self, ShapeTableTests, SHAPE_ROWS, limit=1)


EARLIER_IDENTITY = {"version": "0.13.1", "bundle_digest": "0" * 64}


class UnreadableScanTests(ShapeCase):
    """R118: an unreadable record never hides a readable incoming claim, and while the scan cannot read a record an
    objective its own records do not prove started accepts no work, so absence of proof never starts it."""

    def broken(self, name):
        """An unrelated record of the repository that no longer parses."""
        path = state_root(self.project) / name
        path.mkdir()
        (path / "context.json").write_text("{")
        return path

    def earlier_successor(self):
        """The successor as an earlier bundle recorded it, so its Governor work first needs a refreshed checkpoint."""
        with patch("pod.bundle.running_identity", return_value=EARLIER_IDENTITY):
            self.successor()
        self.refresh = lambda: self.mine("checkpoint", value=self.core(
            obligations=read(self.project, "successor")["checkpoint"]["obligations"]))
        self.prepare_own = lambda: self.mine("governor-prepare", unit="own",
                                             branch={"remote": "origin", "branch": "own", "base": "target"})

    def masked(self, name):
        built = self.lost_with(self.publications)
        effects = self.writes()
        self.earlier_successor()
        decision = self.interrupted("candidate-2b613c8", "after_link")  # Its genuine routine; no backlink.
        broken = self.broken(name)
        place = objective_root(self.project, "objective").name
        self.assertTrue(broken.name < place if name.startswith("00") else place < broken.name)
        before = self.snapshot()
        for call in (self.refresh, self.prepare_own, lambda: self.close("successor", "next", "run-s")):
            refused = self.refused(call, code="claim_incomplete")
            self.assertEqual(refused.detail["predecessor"], "objective")
        self.assertEqual(before, self.snapshot())
        # Its own replay with the decision that made it is never held by the unrelated record.
        self.assertEqual(self.claim(decision)["status"], "claimed")
        self.assertEqual(sorted(row["record_id"] for row in self.journal("successor")["actions"]),
                         sorted(built.values()))
        self.refresh()  # The refresh route opens once the claim is complete.
        # Closure still asks that no unreadable record hides another claimant; once it is gone the rows hold it.
        self.refused(lambda: self.close("successor", "next", "run-s"), code="claim_unverified")
        (broken / "context.json").unlink()
        self.assertEqual(self.refused(lambda: self.close("successor", "next", "run-s")).detail["detail"],
                         "closure_unfinished")
        self.assertEqual(self.writes(), effects)

    def test_a_readable_claim_is_found_before_an_unreadable_record(self):
        self.masked("zz-unreadable")

    def test_a_readable_claim_is_found_after_an_unreadable_record(self):
        self.masked("00-unreadable")

    def test_an_unreadable_predecessor_record_holds_the_successor_until_repaired(self):
        built = self.lost_with(self.publications)
        self.earlier_successor()
        decision = self.interrupted("candidate-2b613c8", "after_link")
        record = objective_root(self.project, "objective") / "context.json"
        original = record.read_bytes()
        record.write_text("{")
        before = self.snapshot()
        for call in (self.refresh, self.prepare_own, lambda: self.close("successor", "next", "run-s")):
            refused = self.refused(call, code="claim_unverified")
            self.assertEqual(refused.detail["unreadable"], [str(record.parent)])
        self.assertEqual(before, self.snapshot())
        record.write_bytes(original)
        self.refused(self.prepare_own, code="claim_incomplete")
        self.assertEqual(self.claim(decision)["status"], "claimed")
        self.assertEqual(sorted(row["record_id"] for row in self.journal("successor")["actions"]),
                         sorted(built.values()))

    def test_an_unreadable_journal_of_its_own_never_proves_a_successor_started(self):
        self.lost_with(lambda: {})
        self.successor()
        decision = self.interrupted("pod-0.13.1", "after_link")
        journal = _record_path(self.project, "successor")
        journal.write_text("{")
        before = self.snapshot()
        self.refused(lambda: self.mine("checkpoint", value=self.core(
            obligations=read(self.project, "successor")["checkpoint"]["obligations"])), code="claim_incomplete")
        self.assertEqual(before, self.snapshot())
        journal.unlink()
        self.assertEqual(self.claim(decision)["status"], "claimed")
        self.close("successor", "next", "run-s")


class ContinuityCapacityTests(ShapeCase):
    """R118: a claim links the predecessor only while its successor can still record the owner handoff or runtime
    rebind that finishes an interrupted claim; the eight-entry history bound (R98, R105) is unchanged."""

    def setUp(self):
        super().setUp()
        self.coordinator, self.generation = "next", 1

    def handoffs(self, count):
        """`count` more completed owner handoffs of the successor through the production helper, each after Orca
        reports its coordinator lost; returns the new coordinator."""
        for _ in range(count):
            old, new = self.coordinator, f"coordinator-{self.generation}"
            self.terminals[old], self.terminals[new] = "stale", "live"
            self.act(new, "run-s")
            self.current.update(coordinator_handle=old, consumer_generation=self.generation)
            verdict = self.status(objective="successor")["owner_handoff"]

            def moved(argv, **kwargs):
                self.current.update(coordinator_handle=new, consumer_generation=self.generation + 1)
                return {"runtime": self.port.runtime, "exit": 0, "error": None, "result": {}}
            with patch("pod.orca.mutate_command", side_effect=moved):
                run("owner-handoff", {"project": str(self.project), "objective": "successor", "owner": new,
                                      "decision": {"provenance": "user_direct", "instruction": "Recover it",
                                                   **verdict["facts"]}})
            self.coordinator, self.generation = new, self.generation + 1
        history = read(self.project, "successor")["checkpoint"]["continuity"]["history"]
        self.assertEqual(len(history), self.generation - 1)
        return self.coordinator

    def decided(self, caller):
        return {"provenance": "user_direct", "instruction": "Carry the lost effects",
                **self.claim(caller=caller)["facts"]}

    def fresh_successor_claims(self, built):
        """The predecessor was never frozen: a fresh successor claims it and carries every original effect."""
        self.assertNotIn("continued_by", read(self.project, "objective"))
        self.successor(name="other", caller="other", run_id="run-o")
        facts = self.claim(name="other", caller="other")["facts"]
        decision = {"provenance": "user_direct", "instruction": "Carry the lost effects", **facts}
        self.assertEqual(self.claim(decision, name="other", caller="other")["status"], "claimed")
        self.assertEqual(sorted(row["record_id"] for row in self.journal("other")["actions"]),
                         sorted(built.values()))

    def test_a_full_history_refuses_the_claim_before_any_write(self):
        built = self.lost_with(self.publications)
        effects = self.writes()
        self.successor()
        caller = self.handoffs(8)
        detected = self.claim(caller=caller)
        self.assertEqual(detected["condition"], "successor_continuity_full", detected.get("message"))
        before = self.snapshot()
        refused = self.refused(lambda: self.claim({"provenance": "user_direct", "instruction": "Carry them",
                                                   **detected["facts"]}, caller=caller),
                               code="successor_continuity_full")
        self.assertIn("fresh successor objective", refused.detail["next_action"])
        self.assertEqual(before, self.snapshot())
        self.fresh_successor_claims(built)
        self.assertEqual(self.writes(), effects)

    def test_one_free_entry_finishes_a_claim_interrupted_after_the_link(self):
        built = self.lost_with(self.publications)
        self.successor()
        caller = self.handoffs(7)
        decision = self.decided(caller)
        with patch("pod.succession._carry", side_effect=KeyboardInterrupt("after the link")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision, caller=caller)
        self.port.runtime = self.port.placement["runtime"] = "runtime-restarted"  # The rebind takes the last entry.
        self.assertEqual(self.claim(decision, caller=caller)["status"], "claimed")
        self.assertEqual(len(read(self.project, "successor")["checkpoint"]["continuity"]["history"]), 8)
        self.assertEqual(sorted(row["record_id"] for row in self.journal("successor")["actions"]),
                         sorted(built.values()))

    def test_a_reservation_whose_last_entry_went_to_a_handoff_is_released_unlinked(self):
        built = self.lost_with(self.publications)
        effects = self.writes()
        self.successor()
        caller = self.handoffs(7)
        decision = self.decided(caller)
        with patch("pod.ledger._write_succession", side_effect=KeyboardInterrupt("reserved")), \
                self.assertRaises(KeyboardInterrupt):
            self.claim(decision, caller=caller)
        rescuer = self.handoffs(1)  # Its coordinator lost before the link: the handoff takes the last entry.
        released = self.refused(lambda: self.claim(decision, caller=rescuer), code="successor_continuity_full")
        self.assertIn("reservation was released", str(released))
        self.assertNotIn("continues", read(self.project, "successor"))
        self.fresh_successor_claims(built)
        self.assertEqual(self.writes(), effects)


class DoubleClaimTests(ShapeCase):
    """R118: earlier writers never checked for an incoming claim, so two of their interrupted claims can name one
    successor. That ambiguity never resolves by completing one: the successor neither works nor closes, and no
    answer reads it closed or finished, while the other predecessor's effects stay unsettled."""

    def earlier(self, writer, predecessor, decision=None):
        return self.routine(writer).succession(self.project, "successor", owner="next", predecessor=predecessor,
                                               decision=decision)

    def claims(self, writer, predecessors, *, completed=None):
        """`writer`'s genuine routine links each predecessor to the successor, each stopped after the link, then
        optionally completes one; its bundle's authority join had no incoming-claim check (`refuse_incomplete`)."""
        from pod import ledger
        real = ledger._write

        def completing(path, value):
            if "state" not in value.get("continues", {"state": None}):
                raise KeyboardInterrupt("after the link")
            return real(path, value)
        decisions = {}
        with patch("pod.succession.refuse_incomplete"):
            for predecessor in predecessors:
                facts = self.earlier(writer, predecessor)
                self.assertEqual(facts["status"], "claim_available", facts.get("message"))
                decisions[predecessor] = {"provenance": "user_direct", "instruction": "Continue the lost objective",
                                          **facts["facts"]}
                stopped = (patch("pod.ledger._write", side_effect=completing) if writer == "pod-0.13.1" else
                           patch.object(self.routine(writer), "_carry", side_effect=KeyboardInterrupt("after link")))
                with stopped, self.assertRaises(KeyboardInterrupt):
                    self.earlier(writer, predecessor, decisions[predecessor])
            if completed is not None:
                self.assertEqual(self.earlier(writer, completed, decisions[completed])["status"], "claimed")
        for predecessor in predecessors:
            self.assertEqual(read(self.project, predecessor)["continued_by"]["objective"], "successor")
        return decisions

    def setting(self, build):
        """Predecessor `objective` with what `build` leaves, a second lost predecessor `empty`, and the successor."""
        built = self.lost_with(build)
        self.successor(name="empty", caller="empty-owner", run_id="run-empty")
        self.terminals["empty-owner"] = "stale"
        self.run_holders["run-empty"] = "empty-owner"
        self.successor()
        return built

    def held(self, decisions):
        """The current reader: every write, replay and further claim refuses `claim_ambiguous`; nothing changes."""
        self.successor(name="other", caller="other", run_id="run-o")
        further = self.claim(name="other", caller="other", predecessor="successor")
        self.assertEqual(further["condition"], "claim_ambiguous", further.get("message"))
        self.act("next", "run-s")
        source = {name: (objective_root(self.project, name) / "context.json").read_bytes() for name in decisions}
        journal = _record_path(self.project, "objective").read_bytes()
        effects, before = self.writes(), self.snapshot()
        names = sorted(decisions)
        for predecessor, decision in decisions.items():
            refused = self.refused(lambda: self.claim(decision, predecessor=predecessor), code="claim_ambiguous")
            self.assertEqual(refused.detail["predecessors"], names)
            self.assertIn("no supported route merges", refused.detail["next_action"])
        for call in (lambda: self.mine("checkpoint", value=self.core(
                         obligations=read(self.project, "successor")["checkpoint"]["obligations"])),
                     lambda: self.mine("governor-prepare", unit="own",
                                       branch={"remote": "origin", "branch": "own", "base": "target"}),
                     lambda: self.close("successor", "next", "run-s")):
            self.refused(call, code="claim_ambiguous")
        readiness = self.status(objective="successor")["readiness"]
        self.assertNotEqual(readiness["state"], "closed")
        self.assertIs(readiness["finalization"]["finished"], False)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(source, {name: (objective_root(self.project, name) / "context.json").read_bytes()
                                  for name in decisions})
        self.assertEqual(journal, _record_path(self.project, "objective").read_bytes())
        self.assertEqual(self.writes(), effects)

    def test_two_genuine_earlier_claims_hold_the_successor(self):
        built = self.setting(self.publications)
        self.held(self.claims("candidate-2b613c8", ("empty", "objective")))
        self.assertEqual(sorted(row["record_id"] for row in self.journal("objective")["actions"]
                                if row["outcome"] in ("pending", "UNKNOWN")), sorted(built.values()))

    def test_a_claim_an_earlier_writer_completed_never_hides_the_other(self):
        self.setting(self.publications)
        decisions = self.claims("candidate-2b613c8", ("empty", "objective"), completed="empty")
        self.assertNotIn("state", read(self.project, "successor")["continues"])
        self.held(decisions)

    def test_two_released_link_only_claims_hold_the_successor_after_one_completes(self):
        self.setting(lambda: {})
        decisions = self.claims("pod-0.13.1", ("empty", "objective"), completed="empty")
        self.assertEqual(read(self.project, "successor")["continues"]["objective"], "empty")
        self.held(decisions)

    def test_a_single_earlier_claim_still_completes_and_closes(self):
        self.setting(self.publications)
        decision = self.claims("candidate-2b613c8", ("empty",))["empty"]
        self.assertEqual(self.claim(decision, predecessor="empty")["status"], "claimed")
        self.close("successor", "next", "run-s")
        self.assertIs(self.status(objective="successor")["readiness"]["finalization"]["finished"], True)


class EarlierReaderTests(CarriedCase):
    """R6 — the oldest and newest earlier versions, 0.12.0 and main's 0.13.2, read carried rows as recorded."""

    RELEASED = {"0.12.0": "5f7298bab47c4a3263496dc2a7fe50ecfcda0530",
                "0.13.2": "1705da5ab84de38f6e6f1f7f745d6e46939643a2"}

    def test_earlier_pods_load_the_carried_journal_and_the_continued_record_unchanged(self):
        self.lost_with(self.publications)
        self.carry()
        repository = Path(__file__).resolve().parents[1]
        journal = _record_path(self.project, "successor")
        before = self.snapshot()
        program = ("import json, sys\nfrom pathlib import Path\nfrom pod import __version__\n"
                   "from pod.governor import _read_journal, status_at\nfrom pod.ledger import _read\n"
                   "assert __version__ == sys.argv[3], __version__\n"
                   "root = Path(sys.argv[1])\nrows = _read_journal(root / 'governor.json')['actions']\n"
                   "print(json.dumps({'rows': rows, 'status': status_at(root)['actions'],\n"
                   "                  'predecessor': _read(Path(sys.argv[2]) / 'context.json')['continued_by']}))\n")
        for version, commit in self.RELEASED.items():
            with self.subTest(version=version):
                released = self.root / f"released-{version}"
                released.mkdir()
                archive = self.root / f"released-{version}.tar"
                subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), commit,
                                "skills", "VERSION"], check=True)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(released, filter="data")
                read_back = subprocess.run(
                    [sys.executable, "-c", program, str(journal.parent), str(objective_root(self.project, "objective")),
                     version], cwd=released, env={**os.environ, "PYTHONPATH": str(released / "skills")},
                    capture_output=True, text=True)
                self.assertEqual(read_back.returncode, 0, read_back.stderr)
                earlier = json.loads(read_back.stdout)
                self.assertEqual(earlier["rows"], self.journal("successor")["actions"])
                self.assertEqual([row["outcome"] for row in earlier["status"]],
                                 [row["outcome"] for row in self.journal("successor")["actions"]][-16:])
                self.assertEqual(earlier["predecessor"], read(self.project, "objective")["continued_by"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()

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
import unittest
from pathlib import Path
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import _read_journal, _record_path, _write_journal, reconcile, record_outcome
from pod.internal import run
from pod.ledger import objective_root, read
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
        released = self.refused(lambda: self.claim(decision), code=detected["condition"])
        self.assertIn("reservation was released", str(released))
        self.assertNotIn("continues", read(self.project, "successor"))
        self.assertNotIn("continued_by", read(self.project, "objective"))
        self.close_successor()  # Released, the successor works and closes as its own objective again.
        self.assertTrue(read(self.project, "successor")["checkpoint"]["closure"])

    def test_a_replay_refuses_a_successor_that_changed_without_a_reservation(self):
        """Synthetic control: an interrupted claim recorded without the reservation, then the successor closed."""
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
        self.close_successor()
        before = self.snapshot()
        refused = self.refused(lambda: self.claim(decision), code="successor_started")
        self.assertIn("the successor is closed or delivered", str(refused))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.journal("successor")["actions"], [])


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


class EarlierReaderTests(CarriedCase):
    """R6 — the oldest and newest earlier versions, 0.12.0 and main's 0.13.1, read carried rows as recorded."""

    RELEASED = {"0.12.0": "5f7298bab47c4a3263496dc2a7fe50ecfcda0530",
                "0.13.1": "13f63a1ccb58d3bb74f53efbaeeec7a7df35fcef"}

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

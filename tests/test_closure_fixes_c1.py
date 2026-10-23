"""Reopen chronology and source retries through the accepted closure boundaries."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from unittest.mock import patch

from pod.errors import PodError
from pod.evidence_closure import close_by_evidence, close_source_issues, sweep
from pod.internal import run
from pod.ledger import closed_snapshot_context, objective_root, read, validate_checkpoint_snapshot
from pod.obligations import validate_closed_snapshot, validate_map_record
from tests.issue_port_support import patch_issue_port
from tests.pod_tick_support import TickCase
from tests import test_closure_fixes_a as earlier
from tests import test_completed_closure as completed
from tests.test_evidence_closure import ClosureCase, _questions

BEFORE = "2000-01-01T00:00:00Z"
AFTER = "2099-01-01T00:00:00Z"


class ReopenTimingTests(TickCase):
    def setUp(self):
        super().setUp()
        self.issue = completed.SourcePort()
        replacement = patch_issue_port(self.issue)
        replacement.start(); self.addCleanup(replacement.stop)

    def ready(self):
        self.bind(); self.prove(); self.tick()

    def reopen(self, stamp):
        self.issue.state, self.issue.state_reason, self.issue.reopened_at = "OPEN", "REOPENED", stamp

    def assert_held(self, result):
        effect = result["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["reason"]), ("unverified", "source_reopen_unverified"))
        self.assertIn("project lead", effect["next_action"])
        self.assertEqual(_questions(result), [])
        self.assertEqual(self.issue.closes, [])

    def test_reopen_while_work_is_in_progress_closes_normally(self):
        self.reopen(BEFORE)
        self.ready()
        result = self.write(close=True)
        effect = result["source_issues"][0]
        self.assertEqual(effect["outcome"], "closed")
        self.assertEqual(effect["reopened_at"], BEFORE)
        self.assertEqual(effect["pod_closed_at"], result["checkpoint"]["closure"]["at"])
        self.assertEqual(self.issue.reopen_reads, [("acme/example", 7)])
        self.assertEqual(len(self.issue.closes), 1)

    def test_preclosure_reopen_retries_after_the_first_close_is_refused(self):
        self.ready(); self.reopen(BEFORE)
        self.issue.close_result = "refused"
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "refused")
        before = self.saved()
        self.issue.close_result = "accepted"
        row = sweep(self.project, objectives=["objective"])["repositories"][0]["objectives"][0]
        self.assertEqual((row["source_issues"][0]["outcome"], row["party"]), ("closed", None))
        self.assertEqual(len(self.issue.closes), 2)
        self.assertEqual(self.saved(), before)

    def test_reopen_after_recorded_closure_stays_open_across_repeated_sweeps(self):
        self.ready()
        stamp = self.write(close=True)["checkpoint"]["closure"]["at"]
        reopened = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(seconds=1)
        self.reopen(reopened.isoformat())
        before = self.saved()
        for _ in range(2):
            result = sweep(self.project, objectives=["objective"])
            row = result["repositories"][0]["objectives"][0]
            effect = row["source_issues"][0]
            self.assertEqual((row["party"], effect["outcome"], effect["reason"]),
                             ("lead", "reopened", "source_reopened"))
            self.assertEqual(effect["pod_closed_at"], stamp)
            self.assertEqual(_questions(result), [])
        self.assertEqual((self.issue.state, len(self.issue.closes)), ("OPEN", 1))
        self.assertEqual(len(self.issue.reopen_reads), 2)
        self.assertEqual(self.saved(), before)

    def test_equal_reopen_and_closure_times_are_not_later_intent(self):
        self.ready(); self.issue.close_result = "refused"
        stamp = self.write(close=True)["checkpoint"]["closure"]["at"]
        self.reopen(stamp); self.issue.close_result = "accepted"
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"][0]["outcome"], "closed")
        self.assertEqual(len(self.issue.closes), 2)

    def test_shared_reopened_source_reads_one_timeline_per_sweep(self):
        self.ready(); self.write(close=True)
        earlier.SourceIntentTests.copy_closed(self, "another")
        self.reopen(AFTER)
        for _ in range(2):
            self.issue.reopen_reads.clear()
            result = sweep(self.project)
            rows = result["repositories"][0]["objectives"]
            self.assertEqual([row["source_issues"][0]["outcome"] for row in rows], ["reopened", "reopened"])
            self.assertEqual(sum(row["party"] == "lead" for row in rows), 1)
            self.assertEqual(self.issue.reopen_reads, [("acme/example", 7)])
        self.assertEqual(len(self.issue.closes), 1)

    def test_report_closure_records_its_time_and_completes_a_preclosure_reopen(self):
        helpers = completed.tick_closure.ClosureTests
        frozen, admission = helpers.worker(self)
        self.report(admission, frozen, map={"obligations": helpers.proof_rows(self)})
        self.tick(); self.reopen(BEFORE)
        result = self.report(admission, frozen, map={"obligations": helpers.proof_rows(self), "close": True})
        stamp = read(self.project, "objective")["checkpoint"]["closure"]["at"]
        self.assertEqual(result["source_issues"][0]["outcome"], "closed")
        self.assertEqual(result["source_issues"][0]["pod_closed_at"], stamp)
        self.assertEqual(len(self.issue.closes), 1)

    def test_unreadable_reopen_timeline_is_a_lead_task_without_a_send(self):
        self.ready(); self.reopen(BEFORE)
        with patch.object(self.issue, "issue_reopened_at", side_effect=PodError("remote_read_failed", "fixture")):
            result = self.write(close=True)
        self.assertTrue(result["checkpoint"]["closure"])
        self.assert_held(result)

    def test_absent_reopen_event_does_not_authorize_a_send(self):
        self.ready(); self.reopen(None)
        self.assert_held(self.write(close=True))

    def test_malformed_reopen_time_does_not_authorize_a_send(self):
        self.ready(); self.reopen("2026-10-23T12:00:00")
        self.assert_held(self.write(close=True))

    def test_legacy_closure_without_a_time_does_not_reclose_a_reopened_source(self):
        self.ready(); self.write(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        raw = json.loads(path.read_text()); raw["checkpoint"]["closure"].pop("at")
        path.write_text(json.dumps(raw))
        self.reopen(BEFORE)
        before = self.saved()
        effect = close_source_issues(self.project, "objective")["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["reason"]), ("unverified", "source_reopen_unverified"))
        self.assertEqual(len(self.issue.closes), 1)
        self.assertEqual(self.saved(), before)

    def test_closure_time_is_recorded_once_and_replayed_without_the_new_clock(self):
        self.ready(); self.write(close=True)
        state = read(self.project, "objective")
        stamp = state["checkpoint"]["closure"]["at"]
        self.assertTrue(stamp.endswith("Z"))
        before = self.saved()
        ctx = closed_snapshot_context(self.project, "objective", state)
        ctx["closure_at"] = AFTER
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], ctx), ())
        validate_checkpoint_snapshot(self.project, state)
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual(self.saved(), before)

    def test_persisted_closure_time_requires_a_canonical_utc_instant(self):
        self.ready(); self.write(close=True)
        checkpoint = read(self.project, "objective")["checkpoint"]
        for stamp in (None, "yesterday", "2026-10-23T12:00:00", "2026-10-23T13:00:00+01:00"):
            with self.subTest(stamp=stamp):
                changed = deepcopy(checkpoint); changed["closure"]["at"] = stamp
                with self.assertRaises(PodError) as caught:
                    validate_map_record(changed)
                self.assertEqual(caught.exception.code, "obligation_invalid")

    def broken_chain(self, kind):
        self.ready(); self.issue.close_result = "refused"; self.write(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        raw = json.loads(path.read_text())
        raw["continues"] = {"objective": "prior-1", "record": "fixture-prior", "at": BEFORE}
        path.write_text(json.dumps(raw))
        if kind == "unreadable":
            prior = objective_root(self.project, "prior-1"); prior.mkdir()
            (prior / "context.json").write_text("incomplete JSON")
        elif kind == "unknown":
            prior = objective_root(self.project, "prior-1"); prior.mkdir()
            (prior / "context.json").write_text(json.dumps({"schema": "pod-context/v999",
                "checkpoint": {"objective": "prior-1"}, "continued_by": {"objective": "objective"}}))
        else:
            for index in range(1, 10):
                name, next_name = f"prior-{index}", f"prior-{index + 1}"
                prior = objective_root(self.project, name); prior.mkdir()
                member = deepcopy(raw); member["checkpoint"]["objective"] = name
                member["continues"] = {"objective": next_name, "record": "fixture-prior", "at": BEFORE}
                member["continued_by"] = {"objective": "objective" if index == 1 else f"prior-{index - 1}",
                    "record": "fixture-next", "owner": "owner", "runs": ["run"], "at": BEFORE,
                    "instruction": "fixture claim", "unfinished": [], "unreported": [], "ambiguity": [],
                    "orca_claim": "unavailable"}
                (prior / "context.json").write_text(json.dumps(member))
        self.issue.close_result = "accepted"
        before = self.saved()
        result = close_source_issues(self.project, "objective")
        self.assertEqual(result["source_issues"][0]["outcome"], "closed")
        self.assertEqual(result["source_issues"][1]["reason"],
                         {"unreadable": "record_unreadable", "unknown": "schema_unknown", "long": "scope_too_large"}[kind])
        self.assertEqual([row["number"] for row in self.issue.closes], [7, 7])
        self.assertEqual(_questions(result), [])
        self.assertEqual(self.saved(), before)

    def test_own_source_closes_with_an_unreadable_claimed_predecessor(self):
        self.broken_chain("unreadable")

    def test_own_source_closes_with_an_unknown_schema_claimed_predecessor(self):
        self.broken_chain("unknown")

    def test_own_source_closes_when_the_claimed_chain_exceeds_its_bound(self):
        self.broken_chain("long")


class ClaimedSweepTests(ClosureCase):
    def test_narrowed_predecessor_sweep_retries_its_source_through_the_closed_tail(self):
        request = earlier.ClaimedSourceTests.ready_successor(self)
        with patch("pod.governor_effects.GhPort", return_value=self.remote), \
                patch.object(self.remote, "issue_close", return_value={"result": "refused", "status": 403}):
            first = run("checkpoint", request)
        self.assertEqual(first["source_issues"][0]["outcome"], "refused")
        before = (self.files(), self.files("successor"))
        result = sweep(self.project, objectives=["objective"], remote=self.remote)
        rows = {row["objective"]: row for row in result["repositories"][0]["objectives"]}
        self.assertEqual(rows["objective"]["source_issues"][0]["outcome"], "closed")
        self.assertEqual(rows["objective"]["outcome"], "skipped")
        self.assertEqual(self.remote.issues[("acme/example", 82)]["state"], "CLOSED")
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "issue_close"]), 1)
        self.assertEqual(_questions(result), [])
        self.assertEqual((self.files(), self.files("successor")), before)

    def test_evidence_closure_uses_its_recorded_time_for_a_preclosure_reopen(self):
        self.shipped(unknown_start=False)
        self.remote.issues[("acme/example", 82)]["state_reason"] = "REOPENED"
        self.remote.issue_reopened_at = lambda **arguments: BEFORE
        result = close_by_evidence(self.project, "objective", caller="fixture", remote=self.remote)
        effect = result["source_issues"][0]
        self.assertEqual(effect["outcome"], "closed")
        self.assertEqual(effect["pod_closed_at"], self.closure()["at"].replace("+00:00", "Z"))
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "issue_close"]), 1)

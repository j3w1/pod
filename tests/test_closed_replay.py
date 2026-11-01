"""R155 / A791: a recorded closure replays exactly as its closing write accepted it (pod#117)."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from pod import config, ledger
from pod.evidence_closure import close_source_issues, sweep
from pod.governor import objective_delivery_reporting
from pod.ledger import closed_snapshot_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from tests import test_pod_ticks_closure as tick_closure
from tests.common import logical_json, proof
from tests.issue_port_support import patch_issue_port
from tests.kernel_support import git
from tests.pod_tick_support import TickCase
from tests.test_completed_closure import SourcePort
from tests.test_standing_authorization import StandingCase


class ConsumedReconciliationTests(TickCase):
    """A settled worker's report consumed as `reconciliation_required` settles it in the replay too."""

    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        patched = patch_issue_port(self.issue)
        patched.start(); self.addCleanup(patched.stop)

    def reconciled_worker(self, *, scope=None, files=()):
        frozen, admission = tick_closure.ClosureTests.worker(self)
        body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admission["native_binding"]["dispatchId"], "candidate": frozen["body"]["candidate"],
                "outcome": "succeeded", "scope": scope or frozen["body"]["scope"], "files": list(files),
                "checks": ["unit"], "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        self.op("report", admission_id=admission["admission_id"], packet=frozen, report=body,
                map={"obligations": tick_closure.ClosureTests.proof_rows(self)})
        consumed = read(self.project, "objective")["admissions"][admission["admission_id"]]["report"]
        self.assertEqual(consumed["status"], "reconciliation_required")
        return admission["admission_id"]

    def close_in_one_step(self):
        self.tick()
        result = self.write(close=True)
        self.assertTrue(read(self.project, "objective")["checkpoint"]["closure"])
        self.assertEqual(result["source_issues"][0]["outcome"], "closed")
        self.assertEqual(result["source_issues"][0]["locator"], self.locator)
        self.assertEqual(len(self.issue.closes), 1)
        return result

    def test_a_prose_scope_report_closes_the_source_in_the_closing_step_and_sweep_is_a_no_op(self):
        self.reconciled_worker(scope=["a prose summary of the assigned work"])
        self.close_in_one_step()
        before = self.saved()
        again = sweep(self.project, objectives=["objective"])
        self.assertEqual(again["repositories"][0]["objectives"][0]["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual((len(self.issue.closes), self.saved()), (1, before))

    def test_a_file_outside_the_assignment_closes_the_source_in_the_closing_step(self):
        self.reconciled_worker(files=["elsewhere/changed.py"])
        self.close_in_one_step()

    def closed_with_open_source(self):
        """Close normally while the fake source refuses its first close, so the issue stays open."""
        self.issue.close_result = "refused"
        self.tick()
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "refused")
        self.issue.close_result = "accepted"
        return objective_root(self.project, "objective") / "context.json"

    def assert_refused(self, path, tamper, *, earlier, detail, key, label):
        """Apply one edit to the closed record (and, for `earlier`, drop the recorded settled set as a closure
        written before it had one); the replay refuses, names `key` and sends nothing."""
        original = path.read_bytes()
        try:
            with self.subTest(label, earlier=earlier):
                raw = logical_json(path)
                if earlier:
                    raw["checkpoint"]["closure"].pop("settlement_digest")
                    raw["checkpoint"]["closure"].pop("settlement_admissions")
                tamper(raw)
                path.write_text(json.dumps(raw))
                closes = len(self.issue.closes)
                row = close_source_issues(self.project, "objective")["source_issues"][0]
                self.assertEqual(len(self.issue.closes), closes)
                self.assertEqual((row["outcome"], row.get("reason"), row.get("detail"), row["locator"]),
                                 ("unverified", "obligation_unaccounted", detail, self.locator))
                self.assertEqual(row["referent"].get("admission", row["referent"].get("record")), key)
                self.assertIn(key, row["message"])
                self.assertIn("project lead", row["next_action"])
                self.assertIn("no owner decision is needed", row["next_action"])
        finally:
            path.write_bytes(original)

    def assert_replays(self, path, *, earlier):
        """The intact record (as written, or without its recorded settled set) closes its source."""
        original = path.read_bytes()
        try:
            raw = logical_json(path)
            if earlier:
                raw["checkpoint"]["closure"].pop("settlement_digest")
                raw["checkpoint"]["closure"].pop("settlement_admissions")
                path.write_text(json.dumps(raw))
            row = close_source_issues(self.project, "objective")["source_issues"][0]
            self.assertEqual(row["outcome"], "closed", row)
        finally:
            path.write_bytes(original)
            self.issue.state, self.issue.state_reason = "OPEN", None

    def tampers(self, key, seq):
        corrupt = lambda raw: raw["admissions"][key]["report"].update(observation_digest="0" * 64)  # noqa: E731
        return (
            ("changed digest", corrupt),
            ("no consumed report", lambda raw: raw["admissions"][key].update(report=None)),
            # A disposition never stands in for a corrupted report, nor settles a review or investigation.
            ("changed digest with an empty disposition",
             lambda raw: (corrupt(raw), raw["admissions"][key].update(disposition={}))),
            ("no report with an invented discard", lambda raw: raw["admissions"][key].update(
                report=None, disposition={"kind": "discarded", "reason": "invented", "seq": seq})))

    def test_an_edited_settlement_record_is_refused_by_the_recorded_settled_set(self):
        key = self.reconciled_worker(scope=["a prose summary of the assigned work"])
        path = self.closed_with_open_source()
        seq = read(self.project, "objective")["checkpoint"]["seq"]
        for label, tamper in self.tampers(key, seq):
            self.assert_refused(path, tamper, earlier=False, detail="settlement_changed", key=key, label=label)
        self.assert_refused(path, lambda raw: raw["checkpoint"]["closure"].update(settlement_digest="0" * 64),
                            earlier=False, detail="settlement_changed", key="closure", label="changed settled-set digest")
        self.assert_replays(path, earlier=False)

    def test_an_edited_admission_identity_is_refused_by_the_recorded_settled_set(self):
        from pod.ledger import settlement_facts
        key = self.reconciled_worker(scope=["a prose summary of the assigned work"])
        path = self.closed_with_open_source()
        def binding(field):
            return lambda raw: raw["admissions"][key]["native_binding"].update({field: "edited-" + field})
        def field(name, value):
            return lambda raw: raw["admissions"][key].update({name: value})
        # Identity only the settled set binds: each edit is refused as `settlement_changed`.
        bound = [(f"native {name}", binding(name)) for name in ("runId", "taskId", "workerId", "terminalHandle", "worktreeId")]
        bound += [("worktree", field("worktree", "/elsewhere")), ("request", field("request_uuid", "0" * 32)),
                  # A writer eligibility input: the recorded result paths a disposition is judged on.
                  ("changed paths", field("changed_paths", ["edited/path.py"]))]
        for label, tamper in bound:
            self.assert_refused(path, tamper, earlier=False, detail="settlement_changed", key=key, label=label)
        self.assert_replays(path, earlier=False)

    def test_identity_the_stored_record_already_joins_is_refused_before_replay(self):
        from pod.ledger import settlement_facts
        key = self.reconciled_worker(scope=["a prose summary of the assigned work"])
        path = self.closed_with_open_source()
        def binding(field):
            return lambda raw: raw["admissions"][key]["native_binding"].update({field: "edited-" + field})
        def field(name, value):
            return lambda raw: raw["admissions"][key].update({name: value})
        # Identity the stored record's own validation already joins (the report's attempt and assignment, the
        # runtime's authority) is refused before replay, with no write; the settled set binds it as well.
        joined = [("native dispatchId", binding("dispatchId")), ("runtime", field("runtime", "another-runtime")),
                  ("role", field("role", "review")), ("serves", field("serves", ["another"])),
                  ("packet", field("packet_id", "0" * 64)), ("candidate", field("candidate", "0" * 40))]
        original = logical_json(path)
        recorded = original["checkpoint"]["closure"]["settlement_digest"]
        runtimes, seq = set(), original["checkpoint"]["seq"]
        for label, tamper in joined:
            with self.subTest(label):
                raw = logical_json(path); tamper(raw)
                self.assertNotEqual(settlement_facts(raw["admissions"], runtimes=runtimes, seq=seq)[0], recorded)
                before = path.read_bytes()
                try:
                    path.write_text(json.dumps(raw))
                    closes = len(self.issue.closes)
                    row = close_source_issues(self.project, "objective")["source_issues"][0]
                    self.assertNotIn(row["outcome"], ("closed", "already_completed"))
                    self.assertEqual(len(self.issue.closes), closes)
                finally:
                    path.write_bytes(before)
        self.assertEqual(settlement_facts(original["admissions"], runtimes=runtimes, seq=seq)[0], recorded)
        self.assert_replays(path, earlier=False)

    def test_a_closure_without_the_recorded_settled_set_checks_each_settlement_record(self):
        key = self.reconciled_worker(scope=["a prose summary of the assigned work"])
        path = self.closed_with_open_source()
        seq = read(self.project, "objective")["checkpoint"]["seq"]
        for label, tamper in self.tampers(key, seq):
            self.assert_refused(path, tamper, earlier=True, detail="outstanding_unaccounted", key=key, label=label)
        self.assert_replays(path, earlier=True)

    def implementation(self, *, result=True):
        """A settled implementation admission, with a real Git result commit and its consumed report, or
        report-less."""
        self.bind("- [ ] **PoD#1", self.sub("S", boundary={"paths": ["src"]}))
        frozen = self.packet(["S"], role="implement", boundary={"paths": ["src"]})
        admission = self.start("writer", frozen)["admission"]
        head = None
        if result:
            (self.project / "src" / "new.py").write_text("new\n")
            git(self.project, "add", "."); git(self.project, "commit", "-qm", "worker result")
            head = git(self.project, "rev-parse", "HEAD")
        self.settle(admission)
        if result:
            rows = self.stored()
            served = next(row for row in rows if row["id"] == "S")
            served.pop("executor", None); served.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
            self.report(admission, frozen, result_commit=head, map={"obligations": rows})
        return admission["admission_id"], head

    def test_an_edited_integration_target_is_refused_and_the_genuine_one_closes(self):
        key, head = self.implementation()
        self.write(tick_closure.ClosureTests.proof_rows(self), dispositions=[{"admission": key, "integrated_into": head}])
        path = self.closed_with_open_source()
        self.assertEqual(read(self.project, "objective")["admissions"][key]["disposition"]["validated"], "ancestry")
        # The result commit is not an ancestor of its base: the writer would refuse this target.
        retarget = lambda raw: raw["admissions"][key]["disposition"].update(candidate=self.base)  # noqa: E731
        self.assert_refused(path, retarget, earlier=False, detail="settlement_changed", key=key, label="recorded set")
        # The recorded result paths the writer judged the integration on: without them it needs an attestation.
        self.assert_refused(path, lambda raw: raw["admissions"][key].update(changed_paths=None), earlier=False,
                            detail="settlement_changed", key=key, label="changed paths removed")
        self.assert_refused(path, retarget, earlier=True, detail="outstanding_unaccounted", key=key, label="Git check")
        self.assert_replays(path, earlier=True)
        self.assert_replays(path, earlier=False)

    def test_a_report_less_discarded_result_closes_the_source_in_the_closing_step(self):
        key, _ = self.implementation(result=False)
        self.write(tick_closure.ClosureTests.proof_rows(self),
                   dispositions=[{"admission": key, "discarded": True, "reason": "superseded result"}])
        self.tick()
        self.assertIsNone(read(self.project, "objective")["admissions"][key]["report"])
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "closed")
        self.assertEqual(len(self.issue.closes), 1)

    def test_a_refusal_before_closure_names_the_bound_source(self):
        self.bind(); self.prove(); self.tick()
        row = close_source_issues(self.project, "objective")["source_issues"][0]
        self.assertEqual((row["outcome"], row["reason"], row["locator"]),
                         ("refused", "objective_not_closed", self.locator))
        self.assertEqual(self.issue.closes, [])


class RecordedDispositionTests(unittest.TestCase):
    """The replay accepts exactly the dispositions `obligations.disposition` records (R155)."""

    def written(self, request, **row):
        from pod.obligations import disposition
        admission = {"admission_id": "a1", "role": "implement", "state": "bound", "changed_paths": ["src/x.py"],
                     "result": {"base": "1" * 40, "head": "2" * 40}, "boundary_exceeded": [], **row}
        ctx = {"outstanding": [], "admissions": {"a1": admission}, "is_ancestor": lambda head, candidate: True}
        return {**admission, "disposition": disposition(admission, {"admission": "a1", **request}, ctx, seq=5)}

    def test_every_written_shape_is_accepted_and_any_other_is_not(self):
        from pod.ledger import _recorded_disposition
        written = {
            "ancestry": self.written({"integrated_into": "3" * 40}),
            "ancestry with reason and attestation": self.written(
                {"integrated_into": "3" * 40, "reason": "why", "attestation": "checked"}),
            "attestation without a commit": self.written({"integrated_into": "c", "attestation": "checked"},
                                                         result={"base": "1" * 40, "head": "1" * 40}),
            "attestation without a Git base": self.written({"integrated_into": "c", "attestation": "checked"},
                                                           changed_paths=None, result={"base": None}),
            "boundary exceeded with reason": self.written({"integrated_into": "3" * 40, "reason": "why"},
                                                          boundary_exceeded=["elsewhere"]),
            "discarded": self.written({"discarded": True, "reason": "superseded"}),
        }
        for label, admission in written.items():
            with self.subTest(label):
                self.assertTrue(_recorded_disposition(admission, 5, is_ancestor=lambda head, candidate: True))
                self.assertFalse(_recorded_disposition(admission, 4, is_ancestor=lambda head, candidate: True),
                                 "a disposition after the closed map")
                for role in ("review", "investigate"):
                    self.assertFalse(_recorded_disposition({**admission, "role": role}, 5))
                for change in ({"extra": 1}, {"kind": "accepted"}, {"seq": 0}):
                    self.assertFalse(_recorded_disposition(
                        {**admission, "disposition": {**admission["disposition"], **change}}, 5), change)
        ancestry = written["ancestry"]
        # Shape alone never attests an integration target: the writer's Git ancestry check runs again.
        self.assertFalse(_recorded_disposition(ancestry, 5, is_ancestor=lambda head, candidate: False))
        self.assertFalse(_recorded_disposition(ancestry, 5))
        self.assertFalse(_recorded_disposition({**ancestry, "disposition": {**ancestry["disposition"],
                                                                            "validated": "attestation"}}, 5))
        self.assertFalse(_recorded_disposition({**ancestry, "disposition": {}}, 5))
        self.assertFalse(_recorded_disposition({**ancestry, "boundary_exceeded": ["elsewhere"]}, 5))


class RouteDiscardSettlementTests(unittest.TestCase):
    """An R160 terminal discard is bound into the settled set with its decision inputs (R155)."""

    @classmethod
    def setUpClass(cls):
        from tests.test_route_settlements import SettlementCase
        cls.case_class = SettlementCase
        SettlementCase.setUpClass()
        cls.addClassCleanup(SettlementCase.doClassCleanups)

    def test_a_pending_discard_matches_its_written_form_and_its_decision_inputs_are_bound(self):
        from pod.ledger import settlement_facts
        case = self.case_class(); case.setUp(); self.addCleanup(case.doCleanups)
        admission, _ = case.original(role="investigate", resolves="one question", stop_condition="one answer")
        entry = case.lead(admission, corrections=[])
        before = case.state(); seq = before["checkpoint"]["seq"] + 1
        pending = settlement_facts(before["admissions"], runtimes=set(), seq=seq, pending=[entry])
        case.c.write(**case.parked(), route_settlements=[entry])
        after = case.state()
        self.assertEqual(settlement_facts(after["admissions"], runtimes=set(), seq=after["checkpoint"]["seq"]), pending)
        written = after["admissions"][admission["admission_id"]]["route_decision"]["settlements"][-1]
        self.assertEqual((written["basis"], written["decision"]), ("lead_decision", "discard"))
        for change in ({"reason": "another reason"}, {"source": {"reference": "another"}}, {"corrections": ["C9"]}):
            with self.subTest(change):
                edited = json.loads(json.dumps(after["admissions"]))
                edited[admission["admission_id"]]["route_decision"]["settlements"][-1].update(change)
                self.assertNotEqual(settlement_facts(edited, runtimes=set(), seq=after["checkpoint"]["seq"])[0],
                                    pending[0])


class YoloDeliveryReplayTests(StandingCase):
    """The closure report keeps the consent its closing write judged; closure still ends yolo for new effects."""

    def deliver_under_yolo(self):
        delivery = {"id": "D", "kind": "delivery", "provenance": "user_direct",
                    "source": {"instruction": "Merge the candidate"}, "check": "merged",
                    "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), delivery],
                        revision_authority={"provenance": "user_direct", "instruction": "Merge the candidate"})
        self.activate()
        pushed = self.case.op("governor", action=self.proposed())
        self.case.remote.heads["origin/candidate"] = self.case.candidate
        self.case.op("governor-outcome", record_id=pushed["record_id"], outcome="PASS")
        merged = self.case.op("governor", action=self.proposed("merge", pr=23))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge candidate", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.case.op("governor-outcome", record_id=merged["record_id"], outcome="PASS",
                     provider={"merge_commit": result})
        self.case.checkpoint(delivery={"record": merged["record_id"]})
        return [self.case.criterion("satisfied", evidence=[self.case.proof("O1")]),
                {**{key: value for key, value in delivery.items() if key != "wait"},
                 "state": "satisfied", "evidence": [self.case.proof("D")]}]

    def close_and_replay(self, rows, expected):
        closed = self.case.write(rows, close=True)["checkpoint"]["closure"]["report"]
        self.assertEqual(closed["delivery_authorization"], {"delivery": {"publish": expected, "merge": expected}})
        self.assertEqual(closed["standing_authorization"]["state"], "ended")
        stored = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", stored)), ())
        self.assertEqual(objective_delivery_reporting(self.project, "objective")["units"]["delivery"],
                         {"publish": expected, "merge": expected})
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"], [])
        return closed

    def test_a_yolo_delivered_closure_replays_its_recorded_consent(self):
        closed = self.close_and_replay(self.deliver_under_yolo(), "RECORDED")
        self.assertTrue(closed["standing_authorization"]["at_closure"]["active_until_closure"])
        # Closure still ends yolo for any new effect.
        self.assertEqual(self.case.op("governor-status")["units"]["delivery"]["authorization"],
                         {"publish": "MISSING", "merge": "MISSING"})

    def test_consent_revoked_before_closure_still_reads_missing(self):
        rows = self.deliver_under_yolo()
        self.operation("revoke")
        closed = self.close_and_replay(rows, "MISSING")
        self.assertFalse(closed["standing_authorization"]["at_closure"]["active_until_closure"])

    def test_consent_replaced_before_closure_still_reads_missing(self):
        rows = self.deliver_under_yolo()
        self.activate(scope=["publish"])
        closed = self.close_and_replay(rows, "MISSING")
        self.assertTrue(closed["standing_authorization"]["at_closure"]["active_until_closure"])

    def test_a_scope_changing_close_judges_consent_on_its_own_closing_record(self):
        rows = self.deliver_under_yolo()
        added = self.case.criterion("satisfied", id="O2", provenance="user_direct", check="PoD#2 passes",
                                    source={"ref": "PoD#2", "instruction": "Include the second criterion",
                                            "channel": "session"})
        added["evidence"] = [proof("O2", self.case.candidate, definition=added,
                                   policy_revision=config.effective(self.project)["revision"])]
        closed = self.case.write([*rows, added], criteria=["PoD#1", "PoD#2"], close=True, revision_authority={
            "provenance": "user_direct", "instruction": "Include the second criterion"})
        report = closed["checkpoint"]["closure"]["report"]
        stored = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", stored)), ())
        # The closing scope suspends yolo at closure, for the report's consent and its standing projection alike.
        self.assertEqual(report["delivery_authorization"], {"delivery": {"publish": "MISSING", "merge": "MISSING"}})
        self.assertEqual(report["standing_authorization"]["at_closure"].get("state"), "suspended")

    def close_at(self, closing, expires, *, advance=None):
        """Close with the standing clock at `closing`, optionally moving it to `advance` once the closing context
        has read consent, as a slow closing operation would."""
        moment, real = [closing], ledger.kernel_context
        def context(*args, **kwargs):
            value = real(*args, **kwargs)
            if advance is not None and kwargs.get("criteria") is not None:
                moment[0] = advance
            return value
        activate = self.activate
        with patch.object(self, "activate", side_effect=lambda **fields: activate(expires_at=expires.isoformat(), **fields)):
            rows = self.deliver_under_yolo()
        with patch("pod.standing._clock", side_effect=lambda now: now or moment[0]), \
                patch("pod.ledger.decision_commit_time", return_value=closing.isoformat()), \
                patch("pod.ledger.kernel_context", side_effect=context):
            closed = self.case.write(rows, close=True)["checkpoint"]["closure"]
            stored = read(self.project, "objective")
            self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                      closed_snapshot_context(self.project, "objective", stored)), ())
            replayed = objective_delivery_reporting(self.project, "objective")["units"]["delivery"]
        self.assertEqual(closed["report"]["standing_authorization"]["at_closure"]["utc"], closed["at"])
        self.assertEqual(closed["report"]["delivery_authorization"]["delivery"], replayed)
        return closed["report"]

    def test_expiry_during_the_closing_operation_is_judged_at_the_one_closure_instant(self):
        closing = datetime(2030, 1, 1, tzinfo=timezone.utc)
        report = self.close_at(closing, closing + timedelta(seconds=1), advance=closing + timedelta(seconds=1))
        self.assertEqual(report["delivery_authorization"]["delivery"], {"publish": "RECORDED", "merge": "RECORDED"})
        self.assertEqual(report["standing_authorization"]["at_closure"].get("state"), "active")

    def test_expiry_exactly_at_the_closure_instant_reads_expired_in_both(self):
        closing = datetime(2030, 1, 1, tzinfo=timezone.utc)
        report = self.close_at(closing, closing)
        self.assertEqual(report["delivery_authorization"]["delivery"], {"publish": "MISSING", "merge": "MISSING"})
        self.assertEqual(report["standing_authorization"]["at_closure"].get("state"), "expired")

    def test_a_revoke_after_closure_is_refused_and_the_replay_keeps_the_recorded_consent(self):
        self.close_and_replay(self.deliver_under_yolo(), "RECORDED")
        self.refusal("objective_closed", self.operation, "revoke")
        with patch("pod.standing._clock", side_effect=lambda now: now or datetime(2100, 1, 1, tzinfo=timezone.utc)):
            stored = read(self.project, "objective")
            self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                      closed_snapshot_context(self.project, "objective", stored)), ())
            self.assertEqual(objective_delivery_reporting(self.project, "objective")["units"]["delivery"],
                             {"publish": "RECORDED", "merge": "RECORDED"})

    def test_an_earlier_closure_without_the_recorded_facts_replays_at_its_own_recorded_instant(self):
        self.close_and_replay(self.deliver_under_yolo(), "RECORDED")
        path = objective_root(self.project, "objective") / "context.json"
        raw = logical_json(path)
        at_closure = raw["checkpoint"]["closure"]["report"]["standing_authorization"]["at_closure"]
        # The shape 0.21.32 recorded: no consent state or scope digest, and a separately sampled instant.
        del at_closure["state"], at_closure["scope_digest"]
        at_closure["utc"] = raw["standing_authorization"]["closed_at"] = "2099-06-01T00:00:00Z"
        path.write_text(json.dumps(raw))
        stored = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(stored["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", stored)), ())
        self.assertEqual(objective_delivery_reporting(self.project, "objective")["units"]["delivery"],
                         {"publish": "RECORDED", "merge": "RECORDED"})

if __name__ == "__main__":
    unittest.main()

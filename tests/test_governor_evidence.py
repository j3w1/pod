"""R4 exact provider event matching and R5 outcome evidence (PoD#5, PoD#6).

Every test is offline: a fake GitHub port stands in for the remote and a disposable Git
repository for the merge target. A row edited to look like an earlier writer's is named a
synthetic control; the genuine earlier reader is the released 0.12.0 source from Git objects.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

from pod.errors import PodError
from pod.governor import (_read_journal, _record_execution, _record_path, classify_failure, decide, execute,
                          prepare_candidate, reconcile, record_outcome)
from pod.ledger import objective_root
from tests.kernel_support import (COMMIT, NOW, RELEASE, KernelCase, action, authorization, dispatch,
                                  GovernorFakePort as FakePort, git)
from tests.test_governor import GovernorCase


def timed(run, second):
    """Give a fake run its own creation time so the newest-run pick is meaningful."""
    run["created_at"] = f"2026-09-21T00:00:{second:02d}+00:00"
    return run


class UnreadablePort(FakePort):
    def branch_head(self, *, remote, branch):
        raise PodError("remote_read_failed", "The remote branch head could not be read")


class EvidenceCase(GovernorCase):
    def setUp(self):
        super().setUp()
        self.binding = self.prepared()["candidate"]
        self.preflight(self.binding["id"])
        self.journal = _record_path(self.project, "objective")

    def execute(self, request, port, **extra):
        return execute(self.project, "objective", owner="owner",
                       action=self.with_publish_authorization(request), port=port, now=NOW, **extra)

    def reconcile(self, record_id, port):
        return reconcile(self.project, "objective", owner="owner", record_id=record_id, port=port, now=NOW)

    def outcome(self, record_id, outcome, port=None, **extra):
        return record_outcome(self.project, "objective", owner="owner", record_id=record_id, outcome=outcome,
                              port=port or FakePort(), now=NOW, **extra)

    def row(self, record_id):
        return [row for row in _read_journal(self.journal)["actions"] if row["record_id"] == record_id][0]

    def refused(self, subcode, record_id, outcome, port=None, **extra):
        """The outcome refuses with that reason and the journal keeps every byte."""
        before = self.journal.read_bytes()
        with self.assertRaises(PodError) as caught:
            self.outcome(record_id, outcome, port, **extra)
        self.assertEqual(caught.exception.detail["detail"], subcode, str(caught.exception))
        self.assertEqual(self.journal.read_bytes(), before)
        return caught.exception

    def published(self, port, kind="pr_update"):
        pushed = self.execute(action(kind=kind, candidate=self.binding["id"]), port)
        self.assertEqual(pushed["outcome"], "PASS")
        return pushed, self.derived(pushed["record_id"])

    def requested(self):
        return [row for row in _read_journal(self.journal)["actions"]
                if row["action"]["kind"] == "workflow_dispatch" and row.get("derived_from") is None][-1]


class ProviderEventTests(EvidenceCase):
    """PoD#5 — event provenance (R4)."""

    def test_an_actual_dispatch_refusal_stays_unknown_and_leaves_the_publication_row_alone(self):
        port = FakePort(auto_ci=("ci.yml",), rejected=("dispatch",))
        _, derived = self.published(port)
        refused = self.execute(dispatch(candidate=self.binding["id"]), port)
        self.assertEqual(refused["outcome"], "UNKNOWN")
        requested = self.requested()["record_id"]
        held = self.reconcile(requested, port)
        self.assertEqual((held["status"], held["action"]), ("UNKNOWN", "hold"))
        self.assertIn("no workflow_dispatch run", held["reason"])
        self.assertEqual((held["provider_read"]["listed"], held["provider_read"]["truncated"]), (1, False))
        self.assertEqual(self.row(requested)["readback"]["reason"], held["reason"])
        self.refused("unknown_not_cancelable", requested, "CANCELED", detail="GitHub refused the dispatch")
        self.refused("performed_by_pod", requested, "FAILED")
        # The missing manual trigger says nothing about the publication's own run.
        port.complete(self.row(derived)["receipt"]["provider"]["run_id"], "success")
        settled = self.reconcile(derived, port)
        self.assertEqual((settled["status"], self.row(derived)["run_event"]), ("PASS", "push"))
        self.assertEqual(self.row(requested)["outcome"], "UNKNOWN")

    def test_a_publication_derived_row_binds_only_a_publication_event_run(self):
        """The SEAL case: an internal workflow_dispatch row a pull-request update started."""
        port = FakePort(blind=True)
        _, derived = self.published(port)
        self.assertEqual(self.row(derived)["action"]["kind"], "workflow_dispatch")
        self.assertIsNone(self.row(derived)["receipt"]["provider"])
        port.blind = False
        early = port._start("ci.yml", COMMIT, event="pull_request", created_at="2026-09-20T23:59:59+00:00")
        pull = timed(port._start("ci.yml", COMMIT, event="pull_request"), 11)
        for second, event in ((30, "workflow_dispatch"), (40, "schedule"), (50, "merge_group")):
            port.complete(timed(port._start("ci.yml", COMMIT, event=event), second)["id"], "failure")
        port.complete(early["id"], "failure")
        port.complete(pull["id"], "success")
        settled = self.reconcile(derived, port)
        provider = self.row(derived)["receipt"]["provider"]
        self.assertEqual((settled["status"], provider["run_id"], provider["event"], provider["attempt"]),
                         ("PASS", pull["id"], "pull_request", 1))
        self.assertEqual(self.row(derived)["run_event"], "pull_request")

    def test_an_unexecuted_reservation_is_never_failed_or_proof(self):
        port = FakePort()
        self.published(port)
        reserved = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        self.assertTrue(self.row(reserved["record_id"])["decision_reserved"])
        held = self.reconcile(reserved["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.refused("readback_disagrees", reserved["record_id"], "FAILED", port)
        self.refused("readback_disagrees", reserved["record_id"], "PASS", port)
        canceled = self.outcome(reserved["record_id"], "CANCELED", port, detail="the owner chose local checks")
        self.assertEqual(canceled["outcome"], "CANCELED")
        executed = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        self.assertEqual((executed["decision"], executed["receipt"]["provider"]["event"]),
                         ("ALLOW", "workflow_dispatch"))
        self.assertEqual(len([call for call in port.calls if call[0] == "dispatch"]), 1)

    def test_missing_and_partial_history_hold_the_row_with_their_reason(self):
        port = FakePort(blind=True)
        _, derived = self.published(port)
        port.blind = False
        missing = self.reconcile(derived, port)
        self.assertEqual((missing["status"], missing["action"]), ("pending", "hold"))
        self.assertEqual({key: missing["provider_read"][key] for key in ("listed", "limit", "truncated")},
                         {"listed": 0, "limit": 20, "truncated": False})
        self.assertIn("retains", missing["provider_read"]["retention"])
        for _ in range(21):
            port._start("ci.yml", COMMIT, event="workflow_dispatch")
        partial = self.reconcile(derived, port)
        self.assertEqual((partial["status"], partial["provider_read"]["truncated"]), ("pending", True))
        self.assertIn("truncated", partial["reason"])
        self.assertEqual(self.row(derived)["outcome"], "pending")
        self.assertIn("truncated", self.row(derived)["readback"]["reason"])

    def test_a_completed_matching_run_settles_with_its_attempt(self):
        port = FakePort(auto_ci=("ci.yml",))
        _, derived = self.published(port, kind="push")
        run_id = self.row(derived)["receipt"]["provider"]["run_id"]
        port.complete(run_id, "success")
        self.assertEqual(self.reconcile(derived, port)["status"], "PASS")
        provider = self.row(derived)["receipt"]["provider"]
        self.assertEqual((provider["run_id"], provider["event"], provider["attempt"]), (run_id, "push", 1))

    def test_a_later_attempt_never_settles_the_first(self):
        port = FakePort(auto_ci=("ci.yml",))
        _, derived = self.published(port)
        run_id = self.row(derived)["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        port.rerun(run_id=run_id, failed_only=True)
        port.complete(run_id, "success")
        settled = self.reconcile(derived, port)
        self.assertEqual((settled["status"], self.row(derived)["receipt"]["provider"]["attempt"]), ("FAILED", 1))

    def failed_dispatch(self, port):
        """A requested dispatch whose run failed, read back and classified: the run a rerun reruns."""
        self.published(port)
        first = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        run_id = first["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        self.assertEqual(self.reconcile(first["record_id"], port)["status"], "FAILED")
        classify_failure(self.project, "objective", owner="owner", record_id=first["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        return first, run_id

    def rerun_row(self):
        return next(row for row in _read_journal(self.journal)["actions"]
                    if row["action"]["kind"] == "validation_rerun")

    def test_a_rerun_reservation_has_no_provenance_and_holds(self):
        """A caller-performed rerun has no pre-read or submission start, so no attempt can be proved its own."""
        port = FakePort()
        first, run_id = self.failed_dispatch(port)
        rerun = self.decide(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]))
        for _ in range(2):
            waiting = self.reconcile(rerun["record_id"], port)
            self.assertEqual((waiting["status"], waiting["action"]), ("pending", "hold"))
            self.assertIn("no recorded pre-submission attempt and start", waiting["reason"])
            port.rerun(run_id=run_id, failed_only=True)
            port.complete(run_id, "success")
        self.assertEqual(self.row(first["record_id"])["outcome"], "FAILED")

    def test_a_rerun_reads_its_own_attempt_and_never_a_later_one(self):
        """FA-A-03: another actor's later attempt never settles the attempt this rerun started."""
        port = FakePort()
        self.published(port)
        first = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        run_id = first["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        self.reconcile(first["record_id"], port)
        classify_failure(self.project, "objective", owner="owner", record_id=first["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        self.assertEqual(rerun["receipt"]["provider"]["attempt"], 2)
        port.complete(run_id, "failure")
        port.rerun(run_id=run_id, failed_only=True)  # Another actor starts attempt 3, which passes.
        port.complete(run_id, "success")
        settled = self.reconcile(rerun["record_id"], port)
        provider = self.row(rerun["record_id"])["receipt"]["provider"]
        self.assertEqual((settled["status"], provider["attempt"], provider["conclusion"]), ("FAILED", 2, "failure"))

    def test_a_rerun_that_cannot_name_its_attempt_reruns_nothing(self):
        port = FakePort()
        self.published(port)
        first = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        port.complete(first["receipt"]["provider"]["run_id"], "failure")
        self.reconcile(first["record_id"], port)
        classify_failure(self.project, "objective", owner="owner", record_id=first["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        port.run = lambda **_: (_ for _ in ()).throw(PodError("remote_read_failed", "down"))
        with self.assertRaises(PodError) as caught:
            self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        self.assertEqual(caught.exception.code, "rerun_attempt_unread")
        rerun = [row for row in _read_journal(self.journal)["actions"] if row["action"]["kind"] == "validation_rerun"]
        self.assertEqual([(row["outcome"], row["receipt"]["detail"]) for row in rerun], [("FAILED", "rerun_attempt_unread")])
        self.assertFalse([call for call in port.calls if call[0] == "rerun"])

    def test_a_rerun_row_without_provenance_holds_rather_than_infer(self):
        """A rerun row recorded before its provenance was kept reads as recorded and never infers an attempt."""
        port = FakePort()
        _, run_id = self.failed_dispatch(port)
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        # Synthetic control: the receipt a writer before this correction left, the run and nothing else.
        journal = _read_journal(self.journal)
        row = next(item for item in journal["actions"] if item["record_id"] == rerun["record_id"])
        row["receipt"]["provider"] = {"run_id": run_id, "rerun": True}
        row["run_event"] = None
        from pod.governor import _write_journal
        _write_journal(self.journal, journal)
        before = self.journal.read_bytes()
        port.complete(run_id, "failure")
        held = self.reconcile(rerun["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn("cannot be told from another actor's", held["reason"])
        self.assertEqual(self.row(rerun["record_id"])["receipt"]["provider"], {"run_id": run_id, "rerun": True})
        self.assertNotEqual(self.journal.read_bytes(), before)  # Only the readback note is added.

    def interrupted_rerun(self, port, *, after=None):
        """Pod submits its rerun and is interrupted before the result write (the probe's boundary)."""
        from unittest.mock import patch
        original = port.rerun

        def submit(*, run_id, failed_only):
            answer = original(run_id=run_id, failed_only=failed_only)
            if after is not None:
                after(original, run_id)
            return answer
        port.rerun = submit
        with patch("pod.governor_effects._record_execution", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        port.rerun = original
        row = self.rerun_row()
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(set(row["receipt"]["provider"]),
                         {"run_id", "rerun", "before_attempt", "submitted_at", "accepted"})
        return row

    def test_an_interrupted_rerun_resumes_from_its_recorded_provenance(self):
        """DA-A-01: provenance written before the effect survives the interruption; resume binds the own attempt."""
        port = FakePort()
        _, run_id = self.failed_dispatch(port)
        port.rerun(run_id=run_id, failed_only=True)  # Another actor's attempt 2 fails before Pod's pre-read.
        port.complete(run_id, "failure")
        row = self.interrupted_rerun(port)
        self.assertEqual(row["receipt"]["provider"]["before_attempt"], 2)
        port.complete(run_id, "success")
        settled = self.reconcile(row["record_id"], port)
        self.assertEqual((settled["status"], settled["receipt"]["provider"]["attempt"]), ("PASS", 3))
        self.assertEqual(len([call for call in port.calls if call[0] == "rerun"]), 2)

    def test_an_interrupted_rerun_with_a_later_actor_holds(self):
        port = FakePort()
        _, run_id = self.failed_dispatch(port)

        def later_actor(rerun, run_id):
            port.complete(run_id, "failure")
            rerun(run_id=run_id, failed_only=True)  # Another actor's attempt 3, after Pod's attempt 2.
            port.complete(run_id, "success")
        row = self.interrupted_rerun(port, after=later_actor)
        held = self.reconcile(row["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn("more than one new attempt", held["reason"])
        self.assertEqual(len([call for call in port.calls if call[0] == "rerun"]), 2)  # Pod's one, the actor's one.

    def test_a_rerun_interrupted_inside_its_call_never_binds_an_unconfirmed_attempt(self):
        """Strict: without Pod's confirmed acceptance a lone new attempt may be another actor's, so it holds."""
        from contextlib import nullcontext
        from unittest.mock import patch
        for hard_kill in (False, True):
            with self.subTest(hard_kill=hard_kill):
                port = FakePort()
                _, run_id = self.failed_dispatch(port)
                original = port.rerun

                def landed_then_killed(*, run_id, failed_only):
                    original(run_id=run_id, failed_only=failed_only)
                    raise KeyboardInterrupt("killed before the reply was processed")
                port.rerun = landed_then_killed
                # A hard kill writes nothing more; otherwise execution records the interrupted effect UNKNOWN.
                stop = (patch("pod.governor_effects._record_execution", side_effect=RuntimeError("killed"))
                        if hard_kill else nullcontext())
                with stop, self.assertRaises((KeyboardInterrupt, RuntimeError)):
                    self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
                port.rerun = original
                row = self.rerun_row()
                self.assertNotIn("accepted", row["receipt"]["provider"])
                self.assertEqual(row["outcome"], "pending" if hard_kill else "UNKNOWN")
                port.complete(run_id, "success")
                held = self.reconcile(row["record_id"], port)
                self.assertEqual(held["action"], "hold")
                self.assertIn("never confirmed accepted", held["reason"])
                self.assertEqual(len([call for call in port.calls if call[0] == "rerun"]), 1)
                self.journal.unlink()
                self.binding = self.prepared()["candidate"]
                self.preflight(self.binding["id"])

    def test_an_unconfirmed_rerun_is_failed_only_once_proved_absent(self):
        """With no attempt after the pre-read past the call's time limit and margin, the rerun did not happen."""
        from datetime import timedelta
        from unittest.mock import patch
        port = FakePort()
        _, run_id = self.failed_dispatch(port)

        def killed(*, run_id, failed_only):
            raise KeyboardInterrupt("killed before the call left")
        port.rerun = killed
        with patch("pod.governor_effects._record_execution", side_effect=RuntimeError("killed")), \
                self.assertRaises(RuntimeError):
            self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        row = self.rerun_row()
        early = reconcile(self.project, "objective", owner="owner", record_id=row["record_id"], port=port,
                          now=NOW + timedelta(seconds=300))
        self.assertEqual((early["status"], early["action"]), ("pending", "hold"))
        late = reconcile(self.project, "objective", owner="owner", record_id=row["record_id"], port=port,
                         now=NOW + timedelta(seconds=720))
        self.assertEqual(late["status"], "FAILED")
        self.assertIn("did not take effect", late["receipt"]["detail"])
        self.assertFalse([call for call in port.calls if call[0] == "rerun"])

    def test_an_actor_between_the_pre_read_and_the_submission_never_binds(self):
        """DA-A-01: the pre-read race, with Pod's own attempt landing or its reply lost."""
        port = FakePort()
        _, run_id = self.failed_dispatch(port)
        original = port.rerun

        def raced(*, run_id, failed_only):
            original(run_id=run_id, failed_only=failed_only)  # Another actor's attempt 2 fails first.
            port.complete(run_id, "failure")
            return original(run_id=run_id, failed_only=failed_only)
        port.rerun = raced
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        port.rerun = original
        self.assertNotIn("attempt", rerun["receipt"]["provider"])
        port.complete(run_id, "success")
        held = self.reconcile(rerun["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn("more than one new attempt", held["reason"])

    def test_one_new_attempt_that_started_before_the_submission_never_binds(self):
        port = FakePort()
        _, run_id = self.failed_dispatch(port)
        original = port.rerun

        def lost(*, run_id, failed_only):
            port.rerun_stamp = "2026-09-20T23:59:59+00:00"
            original(run_id=run_id, failed_only=failed_only)  # Another actor's attempt, before the submission.
            raise PodError("remote_effect_uncertain", "The remote command gave no response")
        port.rerun = lost
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        port.rerun = original
        self.assertEqual(rerun["outcome"], "UNKNOWN")
        port.complete(run_id, "success")
        held = self.reconcile(rerun["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("UNKNOWN", "hold"))
        self.assertIn("Pod cannot prove which is its own", held["reason"])

    def test_truncated_history_never_settles_even_a_visible_match(self):
        """FA-A-02: partial history holds on readback, publication binding and reservation readback."""
        for conclusion in ("failure", "success"):
            with self.subTest(conclusion=conclusion):
                port = FakePort(blind=True)
                pushed, derived = self.published(port)
                port.blind = False
                for second in range(21):
                    port.complete(timed(port._start("ci.yml", COMMIT, event="push"), second + 1)["id"], conclusion)
                held = self.reconcile(derived, port)
                self.assertEqual((held["status"], held["action"], held["provider_read"]["truncated"]),
                                 ("pending", "hold", True))
                self.assertIn("truncated at 20", held["reason"])
                self.assertEqual(self.row(derived)["outcome"], "pending")
                self.journal.unlink()
                self.binding = self.prepared()["candidate"]
                self.preflight(self.binding["id"])
        # A publication whose run list is already truncated binds no run when it lands.
        port = FakePort(auto_ci=("ci.yml",))
        for second in range(21):
            port._start("ci.yml", COMMIT, event="push", created_at=f"2026-09-21T00:00:{second + 1:02d}+00:00")
        _, derived = self.published(port, kind="push")
        self.assertIsNone(self.row(derived)["receipt"]["provider"])
        # A reservation's outcome is read back the same way: partial history shows no settled result.
        reserved = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        for second in range(21):
            port.complete(timed(port._start(RELEASE, COMMIT, event="workflow_dispatch"), second + 1)["id"], "success")
        self.refused("readback_disagrees", reserved["record_id"], "PASS", port)

    def test_a_bound_run_visible_in_a_truncated_list_never_settles(self):
        """FA-A-02 (continued): the partial-history hold follows every history read, a bound run's too."""
        for conclusion in ("failure", "success"):
            with self.subTest(conclusion=conclusion):
                port = FakePort(auto_ci=("ci.yml",))
                for _ in range(19):
                    port._start("ci.yml", COMMIT, event="push", created_at="2026-09-20T23:59:00+00:00")
                _, derived = self.published(port, kind="push")
                run_id = self.row(derived)["receipt"]["provider"]["run_id"]
                port.complete(run_id, conclusion)
                port._start("ci.yml", COMMIT, event="workflow_dispatch")  # A 21st run truncates the list.
                held = self.reconcile(derived, port)
                self.assertEqual((held["status"], held["action"], held["provider_read"]["truncated"]),
                                 ("pending", "hold", True))
                self.assertIn("truncated at 20", held["reason"])
                self.assertEqual((self.row(derived)["outcome"], self.row(derived).get("rejected_run")), ("pending", None))
                self.journal.unlink()
                self.binding = self.prepared()["candidate"]
                self.preflight(self.binding["id"])

    def test_a_reservation_bound_to_a_visible_run_is_held_by_a_truncated_list(self):
        port = FakePort()
        self.published(port)
        reserved = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        run = port._start(RELEASE, COMMIT, event="workflow_dispatch")
        self.assertEqual(self.reconcile(reserved["record_id"], port)["receipt"]["provider"]["run_id"], run["id"])
        port.complete(run["id"], "success")
        for _ in range(20):
            port._start(RELEASE, COMMIT, event="workflow_dispatch")
        refused = self.refused("readback_disagrees", reserved["record_id"], "PASS", port)
        self.assertIn("truncated", str(refused))

    def test_a_bound_run_is_verified_like_a_newly_selected_one(self):
        """FA-A-01: a run the genuine 0.12.0 writer bound is held to the creation floor and workflow identity."""
        import subprocess
        import types
        from unittest.mock import patch
        source = subprocess.run(["git", "show", "5f7298bab47c4a3263496dc2a7fe50ecfcda0530:skills/pod/governor.py"],
                                check=True, capture_output=True, text=True).stdout
        earlier = types.ModuleType("pod._governor_0_12_0")
        earlier.__package__ = "pod"
        exec(compile(source, "governor-0.12.0.py", "exec"), earlier.__dict__)
        for case, workflow, created in (("early", "ci.yml", "2026-09-20T23:59:00+00:00"),
                                        ("other workflow", "other.yml", "2026-09-21T00:00:01+00:00")):
            with self.subTest(case=case):
                port = FakePort(blind=True)
                _, derived = self.published(port)
                port.blind = False
                run = port._start(workflow, COMMIT, event="push", created_at=created)
                port.complete(run["id"], "success")
                with patch.object(earlier, "_assert_authority", return_value=None):
                    earlier.record_outcome(self.project, "objective", owner="owner", record_id=derived,
                                           outcome="UNKNOWN", provider={"run_id": run["id"]}, now=NOW)
                held = self.reconcile(derived, port)
                self.assertEqual((held["status"], held["action"]), ("UNKNOWN", "hold"))
                self.assertEqual(self.row(derived)["rejected_run"]["run_id"], run["id"])
                own = port._start("ci.yml", COMMIT, event="push")
                port.complete(own["id"], "failure")
                settled = self.reconcile(derived, port)
                self.assertEqual((settled["status"], settled["receipt"]["provider"]["run_id"]), ("FAILED", own["id"]))
                self.journal.unlink()
                self.binding = self.prepared()["candidate"]
                self.preflight(self.binding["id"])

    def test_a_run_of_another_event_or_commit_does_not_bind(self):
        port = FakePort(blind=True)
        _, derived = self.published(port)
        port.blind = False
        dispatched = port._start("ci.yml", COMMIT, event="workflow_dispatch")
        # Synthetic control: the any-event binding 0.12.0 made, before R4 read the event.
        _record_execution(self.project, "objective", owner="owner", record_id=derived, outcome="pending",
                          provider={"run_id": dispatched["id"]}, detail=None, now=NOW)
        held = self.reconcile(derived, port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertEqual(self.row(derived)["rejected_run"], {"run_id": dispatched["id"], "event": "workflow_dispatch"})
        pushed = port._start("ci.yml", COMMIT, event="push")
        self.assertEqual(self.reconcile(derived, port)["receipt"]["provider"]["run_id"], pushed["id"])
        self.assertNotIn("rejected_run", self.row(derived))
        # A requested dispatch's run that reports another head commit is not its run either.
        requested = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), FakePort(blind=True))
        stray = port._start(RELEASE, COMMIT, event="workflow_dispatch")
        stray["head_sha"] = "9" * 40
        held = self.reconcile(requested["record_id"], port)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))


class OutcomeEvidenceTests(EvidenceCase):
    """PoD#6 — each outcome needs its own evidence (R5)."""

    def test_rows_pod_performed_take_only_unknown_by_hand(self):
        port = FakePort(blind=True)
        _, derived = self.published(port)
        running = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        for record_id in (running["record_id"], derived):
            for outcome in ("PASS", "FAILED"):
                with self.subTest(record=record_id[:8], outcome=outcome):
                    self.refused("performed_by_pod", record_id, outcome)
            self.refused("performed_by_pod", record_id, "CANCELED", detail="never needed")
        self.refused("receipt_from_readback", running["record_id"], "UNKNOWN", provider={"run_id": "900"})
        self.assertEqual(self.outcome(running["record_id"], "UNKNOWN")["outcome"], "UNKNOWN")
        self.refused("unknown_not_cancelable", running["record_id"], "CANCELED", detail="it never ran")
        before = self.journal.read_bytes()
        self.assertEqual(self.outcome(running["record_id"], "UNKNOWN")["outcome"], "UNKNOWN")
        self.assertEqual(self.journal.read_bytes(), before)
        self.refused("receipt_recorded", running["record_id"], "UNKNOWN", detail="another note")

    def test_a_reservation_settles_only_from_pods_readback(self):
        reserved = self.decide(action(candidate=self.binding["id"]))["record_id"]
        self.refused("receipt_from_readback", reserved, "PASS", provider={"remote_head": COMMIT})
        self.refused("readback_disagrees", reserved, "PASS")
        self.refused("readback_disagrees", reserved, "FAILED")
        self.refused("attestation_missing", reserved, "CANCELED")
        landed = FakePort()
        landed.heads["origin/agent/release"] = COMMIT
        self.refused("execution_signs", reserved, "CANCELED", landed, detail="I never pushed")
        self.refused("readback_unavailable", reserved, "PASS", UnreadablePort())
        recorded = self.outcome(reserved, "PASS", landed, evidence=["push log"])
        self.assertEqual((recorded["outcome"], recorded["receipt"]["provider"]["remote_head"]), ("PASS", COMMIT))
        self.assertIn("push log", self.row(reserved)["receipt"]["evidence"])
        self.assertEqual(self.row(self.derived(reserved))["outcome"], "pending")

    def test_cancel_then_execute_is_offered_only_while_no_sign_of_execution(self):
        port = FakePort()
        self.published(port)
        request = dispatch(candidate=self.binding["id"], target=RELEASE)
        reserved = self.decide(request)
        self.assertIn("Only if it was never performed: record governor-outcome CANCELED", reserved["next_action"])
        port._start(RELEASE, COMMIT, event="workflow_dispatch")
        self.assertEqual(self.reconcile(reserved["record_id"], port)["status"], "pending")
        attached = self.execute(request, port)
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]), ("attach", reserved["record_id"]))
        self.assertIn("never canceled: call governor-reconcile", attached["next_action"])
        self.assertNotIn("CANCELED with detail", attached["next_action"])
        self.refused("execution_signs", reserved["record_id"], "CANCELED", port, detail="never dispatched")
        self.assertFalse([call for call in port.calls if call[0] == "dispatch"])

    def test_release_and_deploy_need_a_receipt_or_evidence(self):
        released = self.decide(action(kind="release", target="v1", candidate=self.binding["id"],
                                      authorization=authorization()))
        self.assertEqual(released["decision"], "ALLOW", released["explanation"])
        for outcome in ("PASS", "FAILED"):
            self.refused("receipt_missing", released["record_id"], outcome)
        self.assertEqual(self.outcome(released["record_id"], "PASS", evidence=["releases/v1.json"])["outcome"],
                         "PASS")
        deployed = self.decide(action(kind="deploy", target="staging", candidate=self.binding["id"],
                                      authorization=authorization(scope=("deploy",))))
        self.refused("attestation_missing", deployed["record_id"], "CANCELED")
        self.assertEqual(self.outcome(deployed["record_id"], "CANCELED", detail="the owner withdrew it")["outcome"],
                         "CANCELED")

    def test_a_restart_or_lost_reply_duplicates_no_effect(self):
        port = FakePort(lost=("push",))
        request = action(candidate=self.binding["id"])
        lost = self.execute(request, port)
        self.assertEqual(lost["outcome"], "UNKNOWN")
        port.lost.clear()
        # A restarted coordinator repeats its request: the unknown push holds it, readback settles it.
        held = self.execute(request, port)
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["effect_unresolved"]))
        self.assertEqual(self.reconcile(lost["record_id"], port)["status"], "PASS")
        self.assertEqual(self.execute(request, port)["reuse"]["kind"], "already_done")
        self.assertEqual(len([call for call in port.calls if call[0] == "push"]), 1)
        # A lost reply to a recorded outcome is answered again from the record, with no write.
        reserved = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        first = self.outcome(reserved["record_id"], "CANCELED", port, detail="never dispatched")
        before = self.journal.read_bytes()
        again = self.outcome(reserved["record_id"], "CANCELED", port, detail="never dispatched")
        self.assertEqual((first["outcome"], again["outcome"]), ("CANCELED", "CANCELED"))
        self.assertEqual(self.journal.read_bytes(), before)
        self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        self.assertEqual(len([call for call in port.calls if call[0] == "dispatch"]), 1)


class MergeOutcomeTests(KernelCase):
    """PoD#6 — a merge outcome is checked against R93's Git facts at outcome time."""

    def setUp(self):
        super().setUp()
        from pod.governor import observe_candidate
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        (self.project / "README.md").write_text("candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write(self.stored())
        observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        self.observed = observed
        self.journal = _record_path(self.project, "objective")

    def merge_decision(self):
        consent = authorization(candidate=self.observed["commit"], tree=self.observed["tree"], scope=("merge",),
                                target="origin/target")
        return decide(self.project, "objective", owner="owner", native_projection={"outstanding": []},
                      action=action(kind="merge", candidate=self.observed["commit"], target="origin/target",
                                    unit="delivery", effects=[], authorization=consent))

    def merged(self, *, fetched=True):
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        if fetched:
            git(self.project, "update-ref", "refs/remotes/origin/target", result)
        return result

    def outcome(self, record_id, outcome, **extra):
        return record_outcome(self.project, "objective", owner="owner", record_id=record_id, outcome=outcome,
                              port=FakePort(), **extra)

    def refused(self, subcode, record_id, outcome, **extra):
        before = self.journal.read_bytes()
        with self.assertRaises(PodError) as caught:
            self.outcome(record_id, outcome, **extra)
        self.assertEqual(caught.exception.detail["detail"], subcode, str(caught.exception))
        self.assertEqual(self.journal.read_bytes(), before)
        return caught.exception

    def test_a_merge_pass_keeps_its_verified_merge_commit_and_says_fetch_first(self):
        decided = self.merge_decision()
        self.assertEqual(decided["decision"], "ALLOW", decided["explanation"])
        self.assertIn("fetch first (git fetch origin target)", decided["next_action"])
        self.assertIn("Never merge again", decided["next_action"])
        self.refused("merge_commit_missing", decided["record_id"], "PASS")
        result = self.merged(fetched=False)
        stale = self.refused("merge_unverified", decided["record_id"], "PASS", provider={"merge_commit": result})
        self.assertIn("fetch first", stale.detail["next_action"])
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        recorded = self.outcome(decided["record_id"], "PASS", provider={"merge_commit": result, "method": "merge"})
        self.assertEqual((recorded["outcome"], recorded["receipt"]["provider"]["merge_commit"]), ("PASS", result))
        self.assertEqual(recorded["readback"], [])
        with self.assertRaises(PodError) as conflict:
            self.outcome(decided["record_id"], "FAILED")
        self.assertEqual(conflict.exception.code, "outcome_conflict")

    def test_failed_or_canceled_only_when_the_target_lacks_the_result(self):
        decided = self.merge_decision()["record_id"]
        self.refused("attestation_missing", decided, "CANCELED")
        git(self.project, "update-ref", "-d", "refs/remotes/origin/target")
        self.refused("target_unobservable", decided, "FAILED")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        result = self.merged()
        self.refused("target_contains_result", decided, "FAILED")
        self.refused("target_contains_result", decided, "CANCELED", detail="never merged")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.refused("merge_commit_conflict", decided, "FAILED", provider={"merge_commit": result})
        self.assertEqual(self.outcome(decided, "FAILED")["outcome"], "FAILED")

    def test_the_merge_pass_sweep_is_held_by_a_truncated_list(self):
        """FA-A-02 (continued): the merge PASS readback sweep reads through the same partial-history guard."""
        consent = authorization(candidate=self.observed["commit"], tree=self.observed["tree"], scope=("publish",))
        pushed = decide(self.project, "objective", owner="owner", native_projection={"outstanding": []},
                        action=action(kind="push", candidate=self.observed["commit"], target="origin/candidate",
                                      unit="delivery", effects=["workflow:ci.yml"], authorization=consent))
        port = FakePort()
        port.heads["origin/candidate"] = self.candidate
        bound = port._start("ci.yml", self.candidate, event="push", created_at="2099-01-01T00:00:00+00:00")
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS",
                       port=port)
        derived = next(row for row in _read_journal(self.journal)["actions"]
                       if row.get("derived_from") == pushed["record_id"])
        self.assertEqual(derived["receipt"]["provider"]["run_id"], bound["id"])
        port.complete(bound["id"], "failure")
        for _ in range(20):
            port._start("ci.yml", self.candidate, event="workflow_dispatch")
        decided = self.merge_decision()["record_id"]
        result = self.merged()
        recorded = record_outcome(self.project, "objective", owner="owner", record_id=decided, outcome="PASS",
                                  provider={"merge_commit": result}, port=port)
        self.assertEqual(recorded["readback"], [{"record_id": derived["record_id"], "status": "pending",
                                                 "action": "hold"}])
        self.assertEqual(next(row for row in _read_journal(self.journal)["actions"]
                              if row["record_id"] == derived["record_id"])["outcome"], "pending")

    def test_a_squash_result_counts_as_contained(self):
        decided = self.merge_decision()["record_id"]
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--squash", "candidate")
        git(self.project, "commit", "-qm", "squash")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        self.refused("target_contains_result", decided, "FAILED")


class EarlierReaderTests(EvidenceCase):
    """R6 — the released 0.12.0 source reads a journal with R4's new fields unchanged."""

    RELEASED = "5f7298bab47c4a3263496dc2a7fe50ecfcda0530"

    def test_pod_0_12_0_loads_new_rows_and_reads_them_as_recorded(self):
        port = FakePort(blind=True)
        _, derived = self.published(port)
        port.blind = False
        port.complete(port._start("ci.yml", COMMIT, event="workflow_dispatch")["id"], "success")
        self.reconcile(derived, port)
        self.assertIn("readback", self.row(derived))
        repository = Path(__file__).resolve().parents[1]
        released = self.root / "released-0120"
        released.mkdir()
        archive = self.root / "released-0120.tar"
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), self.RELEASED,
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(released, filter="data")
        program = ("import json, sys\nfrom pathlib import Path\nfrom pod import __version__\n"
                   "from pod.governor import _read_journal, status_at\n"
                   "assert __version__ == '0.12.0', __version__\n"
                   "root = Path(sys.argv[1])\njournal = _read_journal(root / 'governor.json')\n"
                   "print(json.dumps({'rows': journal['actions'], 'status': status_at(root)['actions']}))\n")
        before = self.journal.read_bytes()
        read = subprocess.run([sys.executable, "-c", program, str(objective_root(self.project, "objective"))],
                              cwd=released, env={**os.environ, "PYTHONPATH": str(released / "skills")},
                              capture_output=True, text=True)
        self.assertEqual(read.returncode, 0, read.stderr)
        earlier = json.loads(read.stdout)
        self.assertEqual(earlier["rows"], _read_journal(self.journal)["actions"])
        self.assertEqual({row["record_id"]: row["outcome"] for row in earlier["status"]},
                         {row["record_id"]: row["outcome"] for row in _read_journal(self.journal)["actions"]})
        self.assertEqual(self.journal.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

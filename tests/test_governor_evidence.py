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

    def test_a_rerun_binds_only_the_attempt_after_the_failed_one(self):
        port = FakePort()
        self.published(port)
        first = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        run_id = first["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        self.assertEqual(self.reconcile(first["record_id"], port)["status"], "FAILED")
        classify_failure(self.project, "objective", owner="owner", record_id=first["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        rerun = self.decide(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]))
        waiting = self.reconcile(rerun["record_id"], port)
        self.assertEqual((waiting["status"], waiting["action"]), ("pending", "hold"))
        self.assertIn("an attempt after 1", waiting["reason"])
        port.rerun(run_id=run_id, failed_only=True)
        port.complete(run_id, "success")
        self.assertEqual(self.reconcile(rerun["record_id"], port)["status"], "PASS")
        self.assertEqual(self.row(rerun["record_id"])["receipt"]["provider"]["attempt"], 2)
        self.assertEqual(self.row(first["record_id"])["outcome"], "FAILED")

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

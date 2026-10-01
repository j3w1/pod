"""Exact CI proof identity (A3): the Governor reuses only a run of the same event and inputs.

The stored logical key is unchanged. Event and dispatch inputs are an extra condition where
validation reuse is decided; attempt numbering, failure gating, unresolved-effect checks and
derived-row de-duplication stay keyed. Every test is offline: a fake GitHub port stands in
for the remote, and the 0.6.6 journals are sanitized fixtures emitted by the released writer.
"""

import inspect
import json
from pathlib import Path
import unittest

from pod.governor import (GitHubPort, GhPort, _logical_key, _read_journal, _record_path, classify_failure,
                          decide, execute, reconcile, record_outcome, status)
from tests.kernel_support import (COMMIT, RELEASE, action, authorization, diagnostic, dispatch,
                                  GovernorFakePort as FakePort, NOW)
from tests.test_governor import GovernorCase

FIXTURES = Path(__file__).parent / "fixtures" / "governor-0.6.6"


def warnings_of(result):
    return [warning["code"] for warning in result["warnings"]]


def timed(run, second):
    """Give a fake run its own creation time so the newest-run pick is meaningful."""
    run["created_at"] = f"2026-09-21T00:00:{second:02d}+00:00"
    return run


class ProofCase(GovernorCase):
    def setUp(self):
        super().setUp()
        self.binding = self.prepared()["candidate"]
        self.preflight(self.binding["id"])

    def execute(self, request, port, **extra):
        return execute(self.project, "objective", owner="owner",
                       action=self.with_publish_authorization(request), port=port, now=NOW, **extra)

    def reconcile(self, record_id, port):
        return reconcile(self.project, "objective", owner="owner", record_id=record_id, port=port, now=NOW)

    def row(self, record_id):
        return [row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                if row["record_id"] == record_id][0]

    def publish(self, port=None):
        """Push the candidate; the push's own ci.yml run is journaled as a derived row."""
        pushed = self.execute(action(candidate=self.binding["id"]), port or FakePort())
        self.assertEqual(pushed["outcome"], "PASS")
        return pushed, self.derived(pushed["record_id"])


class ExactRunReuseTests(ProofCase):
    """PoD#8 — an identical request still attaches while pending and reuses after PASS."""

    def test_an_identical_dispatch_attaches_then_reuses_its_read_back_run(self):
        port = FakePort()
        self.publish(port)
        request = dispatch(candidate=self.binding["id"], target=RELEASE, inputs={"suite": "full"})
        first = self.execute(request, port)
        self.assertEqual((first["decision"], first["receipt"]["provider"]["event"]), ("ALLOW", "workflow_dispatch"))
        self.assertEqual(port.calls[-2], ("dispatch", RELEASE, "agent/release", {"suite": "full"}))
        attached = self.decide(request)
        self.assertEqual((attached["decision"], attached["reuse"]["kind"], attached["record_id"]),
                         ("REUSE", "attach", first["record_id"]))
        port.complete(first["receipt"]["provider"]["run_id"], "success")
        self.assertEqual(self.reconcile(first["record_id"], port)["status"], "PASS")
        self.assertEqual(self.row(first["record_id"])["run_event"], "workflow_dispatch")
        evidence = self.decide(request)
        self.assertEqual((evidence["decision"], evidence["reuse"]["kind"], evidence["record_id"]),
                         ("REUSE", "evidence", first["record_id"]))
        self.assertEqual(len([call for call in port.calls if call[0] == "dispatch"]), 1)

    def test_other_inputs_are_another_proof(self):
        port = FakePort()
        self.publish(port)
        full = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE, inputs={"suite": "full"}), port)
        for inputs in ({}, {"suite": "quick"}, {"suite": "full", "extra": "1"}):
            with self.subTest(inputs=inputs):
                other = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE, inputs=inputs))
                self.assertNotEqual(other["decision"], "REUSE")
                self.assertIn("proof_not_exact", warnings_of(other))
        port.complete(full["receipt"]["provider"]["run_id"], "success")
        self.reconcile(full["record_id"], port)
        self.assertEqual(self.decide(dispatch(candidate=self.binding["id"], target=RELEASE,
                                              inputs={"suite": "full"}))["reuse"]["kind"], "evidence")

    def test_a_pass_without_pod_readback_is_not_proof(self):
        self.publish()
        manual = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        record_outcome(self.project, "objective", owner="owner", record_id=manual["record_id"], outcome="PASS",
                       provider={"run_id": "900", "event": "workflow_dispatch"})
        self.assertNotIn("run_event", self.row(manual["record_id"]))
        again = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        self.assertEqual((again["decision"], again["reuse"]), ("ALLOW", None))
        self.assertIn("proof_not_exact", warnings_of(again))
        self.assertEqual(self.row(again["record_id"])["attempt"], 2)


class SameRunRerunTests(ProofCase):
    """PoD#9 — a failed exact run's failed jobs are rerun within that same run, as in 0.6.6."""

    def test_a_failed_dispatch_run_reruns_its_failed_jobs_in_the_same_run(self):
        port = FakePort()
        self.publish(port)
        run = self.execute(dispatch(candidate=self.binding["id"], target=RELEASE), port)
        run_id = run["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        self.assertEqual(self.reconcile(run["record_id"], port)["status"], "FAILED")
        classify_failure(self.project, "objective", owner="owner", record_id=run["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]), port)
        self.assertEqual((rerun["decision"], rerun["receipt"]["provider"]), ("ALLOW", {"run_id": run_id, "rerun": True}))
        self.assertEqual(port.calls[-1], ("rerun", run_id, True))
        attached = self.decide(action(kind="validation_rerun", target=RELEASE, candidate=self.binding["id"]))
        self.assertEqual((attached["decision"], attached["reuse"]["kind"]), ("REUSE", "attach"))
        port.complete(run_id, "success")
        self.assertEqual(self.reconcile(rerun["record_id"], port)["status"], "PASS")
        self.assertEqual(self.row(rerun["record_id"])["run_event"], "workflow_dispatch")

    def test_rerun_selection_keying_and_gating_are_unchanged_for_a_publication_run(self):
        port = FakePort(auto_ci=("ci.yml",))
        _, derived = self.publish(port)
        self.reconcile(derived, port)
        run_id = self.row(derived)["receipt"]["provider"]["run_id"]
        port.complete(run_id, "failure")
        self.assertEqual(self.reconcile(derived, port)["status"], "FAILED")
        rerun = self.execute(action(kind="validation_rerun", target="ci.yml", candidate=self.binding["id"]), port)
        # Selection is the failed run bound to the candidate and workflow, whatever event started it.
        self.assertEqual((rerun["decision"], port.calls[-1]), ("ALLOW", ("rerun", run_id, True)))
        self.assertEqual(self.row(rerun["record_id"])["logical_key"],
                         _logical_key({"kind": "validation_rerun", "unit": "release", "target": "ci.yml",
                                       "effects": None, "diagnostic": None}, self.binding["id"]))


class NoCrossRunCompositionTests(ProofCase):
    """PoD#10 — job results are never combined across runs; the port reads no jobs."""

    def test_the_github_port_gains_no_job_level_read(self):
        members = {name for name, value in vars(GitHubPort).items()
                   if inspect.isfunction(value) and not name.startswith("_")}
        self.assertEqual(members, {"branch_head", "push", "pull_request", "open_pull_request", "dispatch",
                                   "runs", "run", "rerun", "cancel"})
        self.assertFalse([name for name in dir(GhPort) if "job" in name.lower()])
        from pod.github import RUN_FIELDS
        self.assertNotIn("jobs", RUN_FIELDS.split(","))

    def test_another_runs_passing_jobs_never_complete_a_failed_dispatch(self):
        port = FakePort(auto_ci=("ci.yml",))
        _, derived = self.publish(port)
        self.reconcile(derived, port)
        port.complete(self.row(derived)["receipt"]["provider"]["run_id"], "success")
        self.assertEqual(self.reconcile(derived, port)["status"], "PASS")
        full = self.execute(dispatch(candidate=self.binding["id"]), port)
        self.assertEqual(full["decision"], "ALLOW")
        port.complete(full["receipt"]["provider"]["run_id"], "failure")
        self.assertEqual(self.reconcile(full["record_id"], port)["status"], "FAILED")
        held = self.decide(dispatch(candidate=self.binding["id"]))
        self.assertEqual((held["decision"], self.codes(held), held["reuse"]),
                         ("DEFER", ["failure_unclassified"], None))
        classify_failure(self.project, "objective", owner="owner", record_id=full["record_id"], now=NOW,
                         classification={"class": "transient", "reason": "runner lost"})
        # The push run and the dispatch share the key, so the dispatch was attempt 2 and the
        # transient budget is spent; whatever the gate says, it is never REUSE.
        retry = self.decide(dispatch(candidate=self.binding["id"]))
        self.assertEqual((retry["decision"], self.codes(retry), retry["reuse"]),
                         ("DEFER", ["transient_budget_exhausted"], None))


class UnclearEquivalenceTests(ProofCase):
    """PoD#11 — another event, other inputs or an unknown event is never REUSE."""

    def test_unknown_and_other_events_are_allowed_when_the_checks_pass(self):
        pushed = self.decide(action(candidate=self.binding["id"]))
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        derived = self.derived(pushed["record_id"])
        self.assertIsNone(self.row(derived)["run_event"])
        unknown = self.decide(dispatch(candidate=self.binding["id"], target="ci.yml"))
        self.assertEqual((unknown["decision"], unknown["reuse"]), ("ALLOW", None))
        self.assertIn("proof_not_exact", warnings_of(unknown))
        self.assertEqual(self.row(unknown["record_id"])["attempt"], 2)

    def test_a_failed_run_of_another_event_still_gates_the_key(self):
        port = FakePort(auto_ci=("ci.yml",))
        _, derived = self.publish(port)
        self.reconcile(derived, port)
        port.complete(self.row(derived)["receipt"]["provider"]["run_id"], "failure")
        self.reconcile(derived, port)
        self.assertEqual(self.row(derived)["run_event"], "push")
        held = self.decide(dispatch(candidate=self.binding["id"]))
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["failure_unclassified"]))

    def test_push_dispatch_then_pr_opened_binds_only_the_dispatch_run(self):
        port = FakePort(auto_ci=("ci.yml",))
        self.publish(port)
        port.blind = True
        requested = self.execute(dispatch(candidate=self.binding["id"]), port)
        self.assertEqual(requested["receipt"]["provider"], {"ref_commit": COMMIT})
        opened = self.execute(action(kind="pr_update", candidate=self.binding["id"]), port)
        self.assertEqual(opened["outcome"], "PASS")
        runs = port.runs_by[("ci.yml", COMMIT)]
        self.assertEqual([run["event"] for run in runs], ["push", "workflow_dispatch", "push"])
        dispatched = runs[1]
        for second, run in enumerate(runs, start=1):
            timed(run, second)
        later = timed(port._start("ci.yml", COMMIT, event="pull_request"), 9)
        port.blind = False
        found = self.reconcile(requested["record_id"], port)
        self.assertEqual((found["status"], found["receipt"]["provider"]["run_id"]), ("pending", dispatched["id"]))
        self.assertNotEqual(found["receipt"]["provider"]["run_id"], later["id"])
        attached = self.decide(dispatch(candidate=self.binding["id"]))
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]), ("attach", requested["record_id"]))
        port.complete(dispatched["id"], "success")
        port.complete(later["id"], "failure")
        self.assertEqual(self.reconcile(requested["record_id"], port)["status"], "PASS")
        self.assertEqual(self.decide(dispatch(candidate=self.binding["id"]))["reuse"]["kind"], "evidence")

    def test_an_unsettled_publication_defers_without_claiming_its_run(self):
        self.decide(action(candidate=self.binding["id"]))
        held = self.decide(dispatch(candidate=self.binding["id"]))
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["publication_unsettled"]))
        text = held["explanation"] + held["next_action"]
        self.assertNotIn("is the validation", text)
        self.assertNotIn("will start this workflow", text)
        self.assertIn("before requesting this validation", held["next_action"])


class OldJournalTests(ProofCase):
    """PoD#12 — 0.6.6 journals load unchanged and are read by A3's rules."""

    def setUp(self):
        super().setUp()
        self.provider = json.loads((FIXTURES / "provider.json").read_text())
        self.assertEqual(self.binding["id"], self.provider["candidate"]["id"])
        self.path = _record_path(self.project, "objective")
        self.original = (FIXTURES / "journal.json").read_bytes()
        self.path.write_bytes(self.original)
        self.rows = self.provider["rows"]

    def port(self, *, listed=True):
        port = FakePort()
        runs = {run_id: dict(run) for run_id, run in self.provider["runs"].items()}
        port.run_status.update(runs)
        if listed:
            for key, ids in self.provider["listed"].items():
                workflow, commit = key.split(" ")
                port.runs_by[(workflow, commit)] = [runs[run_id] for run_id in ids]
        return port

    def test_the_journal_loads_without_conversion_and_keys_are_unchanged(self):
        status(self.project, "objective")
        journal = _read_journal(self.path)
        self.assertEqual(self.path.read_bytes(), self.original)
        for row in journal["actions"]:
            with self.subTest(kind=row["action"]["kind"], target=row["action"]["target"]):
                self.assertNotIn("run_event", row)
                self.assertEqual(_logical_key(row["action"], row["candidate_id"]), row["logical_key"])

    def test_publication_derived_rows_never_satisfy_a_dispatch(self):
        result = self.decide(dispatch(candidate=self.binding["id"], target="ci.yml"))
        self.assertEqual((result["decision"], result["reuse"]), ("ALLOW", None))
        self.assertIn("proof_not_exact", warnings_of(result))

    def test_a_settled_dispatch_row_never_counts_and_is_not_read_again(self):
        port = self.port()
        result = self.decide(dispatch(candidate=self.binding["id"], target=RELEASE))
        self.assertEqual((result["decision"], result["reuse"]), ("ALLOW", None))
        again = self.reconcile(self.rows["dispatch_pass"], port)
        self.assertEqual((again["status"], again["action"]), ("PASS", "none"))
        self.assertEqual(port.calls, [])

    def test_a_pending_row_without_a_run_attaches_and_counts_only_for_a_dispatch_run(self):
        attached = self.decide(dispatch(candidate=self.binding["id"], target="nightly.yml"))
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]),
                         ("attach", self.rows["dispatch_pending_unbound"]))
        port = self.port()
        port.runs_by[("nightly.yml", COMMIT)].insert(0, timed(port._start("nightly.yml", COMMIT,
                                                                            event="pull_request"), 59))
        found = self.reconcile(self.rows["dispatch_pending_unbound"], port)
        self.assertEqual(found["receipt"]["provider"]["run_id"], "102")
        port.complete("102", "success")
        self.assertEqual(self.reconcile(self.rows["dispatch_pending_unbound"], port)["status"], "PASS")
        self.assertEqual(self.decide(dispatch(candidate=self.binding["id"], target="nightly.yml"))["reuse"]["kind"],
                         "evidence")

    def test_a_pending_row_bound_to_another_events_run_neither_binds_nor_blocks(self):
        row = self.rows["dispatch_pending_misbound"]
        before = self.decide(dispatch(candidate=self.binding["id"], target="full.yml"))
        self.assertEqual((before["reuse"]["kind"], before["record_id"]), ("attach", row))
        held = self.reconcile(row, self.port(listed=False))
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn("pull_request", held["reason"])
        self.assertEqual(self.row(row)["rejected_run"], {"run_id": "104", "event": "pull_request"})
        allowed = self.decide(dispatch(candidate=self.binding["id"], target="full.yml"))
        self.assertEqual((allowed["decision"], allowed["reuse"], self.codes(allowed)), ("ALLOW", None, []))
        # The dispatch's own run, once visible, binds; the rejected run never does.
        rebound = self.reconcile(row, self.port())
        self.assertEqual((rebound["status"], rebound["receipt"]["provider"]["run_id"]), ("pending", "103"))
        self.assertNotIn("rejected_run", self.row(row))
        self.assertEqual(self.row(row)["run_event"], "workflow_dispatch")

    def test_an_unknown_row_bound_to_another_events_run_resolves_by_readback(self):
        # Synthetic control: the captured misbound row with an UNKNOWN outcome, as a lost response leaves it.
        journal = json.loads(self.original)
        target = [row for row in journal["actions"] if row["record_id"] == self.rows["dispatch_pending_misbound"]][0]
        target["outcome"] = "UNKNOWN"
        self.path.write_text(json.dumps(journal))
        held = self.decide(dispatch(candidate=self.binding["id"], target="full.yml"))
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["effect_unresolved"]))
        found = self.reconcile(target["record_id"], self.port())
        self.assertEqual((found["status"], found["receipt"]["provider"]["run_id"]), ("pending", "103"))
        attached = self.decide(dispatch(candidate=self.binding["id"], target="full.yml"))
        self.assertEqual((attached["reuse"]["kind"], attached["record_id"]), ("attach", target["record_id"]))

    def test_a_pending_row_bound_to_its_dispatch_run_counts_after_readback(self):
        row = self.rows["dispatch_pending_bound"]
        port = self.port()
        port.complete("105", "success")
        self.assertEqual(self.reconcile(row, port)["status"], "PASS")
        self.assertEqual(self.row(row)["run_event"], "workflow_dispatch")
        reused = self.decide(dispatch(candidate=self.binding["id"], target="deep.yml"))
        self.assertEqual((reused["reuse"]["kind"], reused["record_id"]), ("evidence", row))

    def test_remote_diagnostic_reuse_is_unchanged(self):
        answered = self.decide(diagnostic(candidate=self.binding["id"]))
        self.assertEqual((answered["decision"], answered["reuse"]["kind"], answered["record_id"]),
                         ("REUSE", "evidence", self.rows["diagnostic_pass"]))


if __name__ == "__main__":
    unittest.main()

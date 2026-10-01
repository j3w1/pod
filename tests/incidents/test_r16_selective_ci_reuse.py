"""Regression for R16: a selective pull-request run answered REUSE for an explicit full dispatch.

Sanitized shape of what happened: the project's CI ran selectively on a pull request,
skipping checks that the changed paths did not touch, and ran in full when dispatched. The
pull-request run was journaled as the validation the publication started. An explicit full
workflow_dispatch on the same candidate was answered REUSE twice, first while that run was
pending and again after it passed; both times the owner overrode it, and the second time the
full run was the only check that caught a real defect. No identifiers from the incident are
kept here, only its shape.
"""

import unittest

from pod.governor import _read_journal, _record_path, execute, reconcile
from tests.kernel_support import GovernorFakePort, NOW, action, dispatch
from tests.test_governor import GovernorCase


class SelectivePullRequestPort(GovernorFakePort):
    """Publication starts ci.yml as a pull_request run, as the incident's PR events did."""

    def push(self, *, remote, branch, commit, expected):
        result = super().push(remote=remote, branch=branch, commit=commit, expected=expected)
        self._start("ci.yml", commit, event="pull_request")
        return result


class SelectiveCiReuse(GovernorCase):
    def setUp(self):
        super().setUp()
        self.binding = self.prepared()["candidate"]
        self.preflight(self.binding["id"])
        self.port = SelectivePullRequestPort()
        opened = self.run_action(action(kind="pr_update", candidate=self.binding["id"]))
        self.assertEqual(opened["outcome"], "PASS")
        self.selective = self.derived(opened["record_id"])

    def run_action(self, request):
        return execute(self.project, "objective", owner="owner", action=self.with_publish_authorization(request),
                       port=self.port, now=NOW)

    def readback(self, record_id):
        return reconcile(self.project, "objective", owner="owner", record_id=record_id, port=self.port, now=NOW)

    def row(self, record_id):
        return [row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                if row["record_id"] == record_id][0]

    def full(self, inputs):
        return dispatch(candidate=self.binding["id"], target="ci.yml", inputs=inputs)

    def test_a_pending_selective_run_does_not_answer_a_full_dispatch(self):
        self.assertEqual(self.readback(self.selective)["status"], "pending")
        self.assertEqual(self.row(self.selective)["run_event"], "pull_request")
        for inputs in ({}, {"scope": "full"}):
            with self.subTest(inputs=inputs):
                answer = self.decide(self.full(inputs))
                self.assertEqual((answer["decision"], answer["reuse"]), ("ALLOW", None), answer["explanation"])
                self.assertIn("proof_not_exact", [warning["code"] for warning in answer["warnings"]])

    def test_a_passing_selective_run_does_not_answer_a_full_dispatch_and_the_full_run_decides(self):
        self.readback(self.selective)
        self.port.complete(self.row(self.selective)["receipt"]["provider"]["run_id"], "success")
        self.assertEqual(self.readback(self.selective)["status"], "PASS")
        full = self.run_action(self.full({"scope": "full"}))
        self.assertEqual((full["decision"], full["reuse"]), ("ALLOW", None))
        self.assertEqual([call for call in self.port.calls if call[0] == "dispatch"],
                         [("dispatch", "ci.yml", "agent/release", {"scope": "full"})])
        run = full["receipt"]["provider"]
        self.assertEqual(run["event"], "workflow_dispatch")
        self.assertNotEqual(run["run_id"], self.row(self.selective)["receipt"]["provider"]["run_id"])
        # The checks the selective run skipped are what the full run proves; its failure stands.
        self.port.complete(run["run_id"], "failure")
        self.assertEqual(self.readback(full["record_id"])["status"], "FAILED")
        held = self.decide(self.full({"scope": "full"}))
        self.assertEqual((held["decision"], [reason["code"] for reason in held["reasons"]], held["reuse"]),
                         ("DEFER", ["failure_unclassified"], None))


if __name__ == "__main__":
    unittest.main()

"""Regression for the incident that motivated the waste governor.

Sanitized shape of what happened: one piece of work, still converging, crossed the
pull-request and CI boundary once per intermediate correction. Each push started a full
hosted run; each run failed on something a local check would have caught, or on a
remote-only question that a narrow probe would have answered; the next correction was
pushed before the previous failure was understood. No identifiers from the incident are
kept here, only its shape.
"""

from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import (classify_failure, decide, execute, prepare_candidate, reconcile, record_correction,
                          record_preflight, status)
from pod.ledger import checkpoint
from tests.common import fixture
from tests.kernel_support import (BRANCH, GovernorFakePort as FakePort, action, authorization,
                                  body, correction, diagnostic, dispatch, observation)

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
CONFIG = """schema: pod/v1
waste_governor:
  preflight: [workflow-validation, release-boundary-tests]
  triggers:
    push: ["workflow:ci.yml"]
    pr_update: ["workflow:ci.yml"]
"""


class RepeatedPullRequestCycle(unittest.TestCase):
    def test_a_converging_unit_crosses_the_boundary_once_per_ready_candidate(self):
        authority = patch('pod.governor._assert_authority', return_value=None)
        authority.start(); self.addCleanup(authority.stop)
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = root / "project"
            project.mkdir()
            (project / ".pod").mkdir()
            (project / ".pod" / "config.yaml").write_text(CONFIG)
            with patch('pod.ledger.require_authority', return_value={
                    'runtime':'runtime','run_id':'run','references':{'run':'runtime'}}):
                checkpoint(project, "objective", owner="owner", value=body("0" * 40), native={"runtime": "runtime"})
            # The repository runs ci.yml on every push, exactly as in the incident.
            port = FakePort(auto_ci=("ci.yml",))

            def prepare(index):
                return prepare_candidate(project, "objective", owner="owner", unit="release-boundary",
                                         observation=observation(commit=str(index) * 40, tree=chr(ord("a") + index) * 40),
                                         branch=BRANCH, now=NOW)["candidate"]

            def push(candidate):
                return execute(project, "objective", owner="owner", port=port, now=NOW,
                               action=action(kind="pr_update", unit="release-boundary", candidate=candidate["id"],
                                             authorization=authorization(candidate=candidate["commit"],
                                                                         tree=candidate["tree"], scope=("publish",))))

            def ci_row(candidate):
                rows = [row for row in status(project, "objective")["units"]["release-boundary"]["active_validation"]
                        if row["target"] == "ci.yml"]
                self.assertEqual(len(rows), 1)
                return rows[0]

            # Three intermediate corrections, each pushed as it was in the incident. None is
            # locally checked, so none crosses the boundary; the coordinator is told why and
            # what to do instead, and the pull request is not touched.
            for index in (1, 2, 3):
                candidate = prepare(index)
                result = push(candidate)
                self.assertEqual(result["decision"], "DEFER", result["explanation"])
                self.assertEqual([reason["code"] for reason in result["reasons"]], ["local_preflight_missing"])
                self.assertIn("run the configured local preflight", result["next_action"])
            self.assertEqual(port.calls, [])

            # The fourth correction is checked locally first: one publication, and the run it
            # starts is journaled, so asking for the same validation attaches instead of
            # starting a second run.
            candidate = prepare(4)
            for check in ("workflow-validation", "release-boundary-tests"):
                record_preflight(project, "objective", owner="owner", unit="release-boundary",
                                 candidate=candidate["id"], check=check, status="PASS", now=NOW)
            published = push(candidate)
            self.assertEqual((published["decision"], published["outcome"]), ("ALLOW", "PASS"))
            self.assertFalse(published["receipt"]["provider"]["pr_reused"])
            run = ci_row(candidate)
            self.assertEqual(run["provider"]["run_id"], "100")
            attached = execute(project, "objective", owner="owner", port=port, now=NOW,
                               action=dispatch(unit="release-boundary", candidate=candidate["id"]))
            self.assertEqual((attached["decision"], attached["reuse"]["kind"], attached["record_id"]),
                             ("REUSE", "attach", run["record_id"]))

            # The run fails on a remote-only question. The reflex was another push; the
            # governor asks for a classification, then admits one narrow probe.
            port.complete("100", "failure")
            self.assertEqual(reconcile(project, "objective", owner="owner", record_id=run["record_id"],
                                       port=port, now=NOW)["status"], "FAILED")
            held = decide(project, "objective", owner="owner", now=NOW,
                          action=dispatch(unit="release-boundary", candidate=candidate["id"]))
            self.assertEqual([reason["code"] for reason in held["reasons"]], ["failure_unclassified"])
            classify_failure(project, "objective", owner="owner", record_id=run["record_id"], now=NOW,
                             classification={"class": "remote_only",
                                             "reason": "the workflow actor cannot read the ruleset field locally"})
            held = decide(project, "objective", owner="owner", now=NOW,
                          action=dispatch(unit="release-boundary", candidate=candidate["id"]))
            self.assertEqual([reason["code"] for reason in held["reasons"]], ["remote_only_needs_diagnostic"])
            probe = execute(project, "objective", owner="owner", port=port, now=NOW,
                            action=diagnostic(unit="release-boundary", candidate=candidate["id"]))
            self.assertEqual((probe["decision"], probe["receipt"]["provider"]["run_id"]), ("ALLOW", "101"))
            self.assertEqual(execute(project, "objective", owner="owner", port=port, now=NOW,
                                     action=diagnostic(unit="release-boundary", candidate=candidate["id"]))["decision"],
                             "REUSE")

            # The answer produces one more correction, which is a code defect the second
            # time round: recorded as a correction, then validated once on a new candidate.
            # The superseded probe is canceled; the failed run needs no cancellation.
            classify_failure(project, "objective", owner="owner", record_id=run["record_id"], now=NOW,
                             classification={"class": "code_defect", "reason": "the ruleset read needs a scope",
                                             "correction": correction("scope")})
            candidate = prepare(5)
            for check in ("workflow-validation", "release-boundary-tests"):
                record_preflight(project, "objective", owner="owner", unit="release-boundary",
                                 candidate=candidate["id"], check=check, status="PASS", now=NOW)
            updated = push(candidate)
            self.assertEqual(updated["outcome"], "PASS")
            self.assertTrue(updated["receipt"]["provider"]["pr_reused"])
            self.assertEqual([call for call in port.calls if call[0] == "cancel"], [("cancel", "101")])
            final = ci_row(candidate)
            self.assertEqual(final["provider"]["run_id"], "102")
            self.assertEqual(execute(project, "objective", owner="owner", port=port, now=NOW,
                                     action=dispatch(unit="release-boundary", candidate=candidate["id"]))["decision"],
                             "REUSE")

            projection = status(project, "objective")
            started = sorted(port.run_status)
            self.assertEqual(started, ["100", "101", "102"])
            self.assertEqual([call for call in port.calls if call[0] == "dispatch"],
                             [("dispatch", "probe.yml", "agent/release", {})])
            self.assertEqual(projection["counters"]["decisions"]["DEFER"], 5)
            self.assertEqual(projection["counters"]["deferrals"]["local_preflight_missing"], 3)
            self.assertEqual(projection["counters"]["decisions"], {"ALLOW": 4, "DEFER": 5, "REUSE": 3})
            self.assertEqual(projection["counters"]["attachments"], 3)
            self.assertEqual(projection["counters"]["evidence_reused"], 0)
            self.assertEqual(projection["counters"]["cancellations"], 1)
            self.assertEqual(projection["units"]["release-boundary"]["generation"], 5)
            self.assertEqual(len([call for call in port.calls if call[0] == "pr_create"]), 1)
            with self.assertRaises(PodError) as replay:
                record_correction(project, "objective", owner="owner", unit="release-boundary",
                                  correction=correction("scope"))
            self.assertEqual(replay.exception.code, "correction_replay")

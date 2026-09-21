from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import decide, record_outcome, status
from pod.ledger import checkpoint, intervention, reserve
from pod.config import effective
from tests.common import establishment, fixture

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
CANDIDATE = "a" * 40
SUPERSEDED = "b" * 40


def authorization(candidate=CANDIDATE, scope=("merge", "release")):
    return {"schema": "pod-release-authorization/v1", "candidate": candidate, "tree": "c" * 40,
            "scope": list(scope), "authorized_by": "owner", "utc": "2026-09-21T00:00:00Z",
            "reference": "tasks/pod/authorization.md"}


def action(kind="push", candidate=CANDIDATE, target="origin/agent/pod", **extra):
    return {"kind": kind, "candidate": candidate, "target": target,
            "reason": "converged candidate", **extra}


def body(candidate=CANDIDATE, gaps=()):
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
            "candidate": candidate, "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": list(gaps), "next_safe_action": "inspect"}


class GovernorTests(unittest.TestCase):
    def prepared(self, root, *, candidate=CANDIDATE, gaps=()):
        project = root / "project"
        project.mkdir(exist_ok=True)
        checkpoint(project, "objective", owner="owner", value=body(candidate, gaps),
                   native={"runtime": "runtime"})
        return project

    def test_duplicate_action_defers_and_unchanged_state_is_a_no_op(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            first = decide(project, "objective", owner="owner", action=action(), now=NOW)
            self.assertEqual(first["decision"], "ALLOW")
            self.assertTrue(first["recorded"])
            second = decide(project, "objective", owner="owner", action=action(),
                            now=NOW + timedelta(minutes=5))
            self.assertEqual(second["decision"], "DEFER")
            self.assertEqual([reason["code"] for reason in second["reasons"]], ["duplicate"])
            self.assertFalse(second["recorded"])
            self.assertEqual(len(status(project, "objective")["actions"]), 1)

    def test_superseded_candidate_defers_except_a_justified_diagnostic(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            stale = decide(project, "objective", owner="owner",
                           action=action(candidate=SUPERSEDED), now=NOW)
            self.assertEqual(stale["decision"], "DEFER")
            self.assertIn("superseded_candidate", [reason["code"] for reason in stale["reasons"]])
            diagnostic = decide(project, "objective", owner="owner",
                                action=action(kind="remote_diagnostic", candidate=SUPERSEDED,
                                              target="ci/linux",
                                              diagnostic_value="only the runner reproduces this"),
                                now=NOW)
            self.assertEqual(diagnostic["decision"], "WARN")

    def test_premature_validation_defers_until_effects_settle(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            route = {"agent": "codex", "model": "m", "account": "a", "bucket": None, "effort": "high"}
            reserve(project, "objective", owner="owner", operation_id="op", requested=route,
                    route_decision={"status": "usable", "selected": route,
                                    "policy_revision": effective(project)["revision"]},
                    establishment=establishment(route, runtime="runtime"),
                    native_reader=lambda: {"runtime": "runtime", "authoritative": True,
                                           "owner": "owner", "scope": "all", "complete": True,
                                           "workers": [], "cross_host": False},
                    capacity=2, run_id="run", plan_revision="p")
            blocked = decide(project, "objective", owner="owner",
                             action=action(kind="workflow_dispatch", target="ci/linux"), now=NOW)
            self.assertEqual(blocked["decision"], "DEFER")
            self.assertEqual(blocked["phase"], "working")
            self.assertIn("integration_unsettled", [reason["code"] for reason in blocked["reasons"]])

    def test_failed_run_reruns_only_after_an_input_changed(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            first = decide(project, "objective", owner="owner",
                           action=action(kind="workflow_dispatch", target="ci/linux"), now=NOW)
            record_outcome(project, "objective", owner="owner", record_id=first["record_id"],
                           outcome="FAILED")
            unchanged = decide(project, "objective", owner="owner",
                               action=action(kind="workflow_dispatch", target="ci/linux"), now=NOW)
            self.assertEqual(unchanged["decision"], "DEFER")
            self.assertIn("unchanged_rerun", [reason["code"] for reason in unchanged["reasons"]])
            checkpoint(project, "objective", owner="owner", value=body(CANDIDATE) | {"plan_revision": "p2"},
                       native={"runtime": "runtime"})
            changed = decide(project, "objective", owner="owner",
                             action=action(kind="workflow_dispatch", target="ci/linux"), now=NOW)
            self.assertEqual(changed["decision"], "ALLOW")
            self.assertIn("necessary_rerun", [reason["code"] for reason in changed["reasons"]])

    def test_override_softens_only_an_efficiency_deferral(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            decide(project, "objective", owner="owner", action=action(), now=NOW)
            softened = decide(project, "objective", owner="owner", action=action(),
                              override={"reason": "the remote rejected the first push", "by": "owner"},
                              now=NOW)
            self.assertEqual(softened["decision"], "WARN")
            self.assertTrue(softened["override"]["applied"])
            held = decide(project, "objective", owner="owner",
                          action=action(kind="merge", target="main"),
                          override={"reason": "ship it", "by": "owner"}, now=NOW)
            self.assertEqual(held["decision"], "DEFER")
            self.assertFalse(held["override"]["applied"])
            self.assertEqual(held["override"]["ignored_because"], "authorization")

    def test_merge_needs_authorization_and_closed_verification_gaps(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root, gaps=("live matrix",))
            unauthorized = decide(project, "objective", owner="owner",
                                  action=action(kind="merge", target="main"), now=NOW)
            self.assertEqual(unauthorized["decision"], "DEFER")
            self.assertEqual({reason["code"] for reason in unauthorized["reasons"]},
                             {"authorization_missing", "verification_gaps"})
            authorized = decide(project, "objective", owner="owner",
                                action=action(kind="merge", target="main",
                                              authorization=authorization()), now=NOW)
            self.assertEqual({reason["code"] for reason in authorized["reasons"]},
                             {"verification_gaps"})
            checkpoint(project, "objective", owner="owner", value=body(CANDIDATE),
                       native={"runtime": "runtime"})
            clear = decide(project, "objective", owner="owner",
                           action=action(kind="merge", target="main",
                                         authorization=authorization()), now=NOW)
            self.assertEqual(clear["decision"], "ALLOW")
            self.assertEqual(clear["phase"], "candidate")

    def test_authorization_must_name_this_candidate_and_kind(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            for grant in (authorization(candidate=SUPERSEDED), authorization(scope=("deploy",))):
                with self.subTest(scope=grant["scope"], candidate=grant["candidate"]):
                    result = decide(project, "objective", owner="owner",
                                    action=action(kind="release", target="v0.1.0",
                                                  authorization=grant), now=NOW)
                    self.assertEqual(result["decision"], "DEFER")
                    self.assertIn("authorization_missing",
                                  [reason["code"] for reason in result["reasons"]])

    def test_one_early_remote_diagnostic_is_allowed_then_warned(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            probe = action(kind="remote_diagnostic", target="ci/linux",
                           diagnostic_value="the runner's Python differs from this host")
            first = decide(project, "objective", owner="owner", action=probe, now=NOW)
            self.assertEqual(first["decision"], "ALLOW")
            record_outcome(project, "objective", owner="owner", record_id=first["record_id"],
                           outcome="PASS")
            repeat = decide(project, "objective", owner="owner", action=probe, now=NOW)
            self.assertEqual(repeat["decision"], "DEFER")

    def test_outcome_binding_is_exact_and_idempotent(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            allowed = decide(project, "objective", owner="owner", action=action(), now=NOW)
            record_outcome(project, "objective", owner="owner", record_id=allowed["record_id"],
                           outcome="PASS")
            record_outcome(project, "objective", owner="owner", record_id=allowed["record_id"],
                           outcome="PASS")
            with self.assertRaises(PodError) as conflict:
                record_outcome(project, "objective", owner="owner",
                               record_id=allowed["record_id"], outcome="FAILED")
            self.assertEqual(conflict.exception.code, "outcome_conflict")
            with self.assertRaises(PodError) as unknown:
                record_outcome(project, "objective", owner="owner", record_id="absent",
                               outcome="PASS")
            self.assertEqual(unknown.exception.code, "unknown_governed_action")

    def test_malformed_actions_and_foreign_coordinators_are_refused(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            for broken in ({**action(), "kind": "publish"},
                           {**action(), "extra": True},
                           {**action(kind="push"), "diagnostic_value": "not a diagnostic"}):
                with self.subTest(broken=sorted(broken)):
                    with self.assertRaises(PodError):
                        decide(project, "objective", owner="owner", action=broken, now=NOW)
            with self.assertRaises(PodError) as foreign:
                decide(project, "objective", owner="other", action=action(), now=NOW)
            self.assertEqual(foreign.exception.code, "coordinator_conflict")


class ReviewFindingRegressions(unittest.TestCase):
    """Each of these reproduces a defect an independent review found."""

    def prepared(self, root, *, candidate=CANDIDATE, gaps=()):
        project = root / "project"
        project.mkdir(exist_ok=True)
        checkpoint(project, "objective", owner="owner", value=body(candidate, gaps),
                   native={"runtime": "runtime"})
        return project

    def correction(self, key):
        return {"criterion_id": "works", "failure_id": key, "obligation": "make it pass",
                "failing_example": "the test fails", "hypothesis": "an off-by-one",
                "last_meaningful_evidence": "the failing assertion",
                "next_discriminating_check": "run the single test",
                "correction_key": key}

    def test_a_task_awaiting_a_diagnosis_keeps_the_objective_converging(self):
        """Interventions are a list of correction rows per Task, never a single record.

        Reading them as a record meant a Task past the correction threshold never held
        back validation, so a documented DEFER condition was dead code.
        """
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            route = {"agent": "codex", "model": "m", "account": "a", "bucket": None,
                     "effort": "high"}
            reserve(project, "objective", owner="owner", operation_id="op", requested=route,
                    route_decision={"status": "usable", "selected": route,
                                    "policy_revision": effective(project)["revision"]},
                    establishment=establishment(route, runtime="runtime"),
                    native_reader=lambda: {"runtime": "runtime", "authoritative": True,
                                           "owner": "owner", "scope": "all", "complete": True,
                                           "workers": [], "cross_host": False},
                    capacity=2, run_id="run", plan_revision="p")
            from pod.ledger import _lock, _path, _read, _write
            path = _path(project, "objective")
            with _lock(path):
                state = _read(path)
                effect = state["effects"]["op"]
                effect["state"] = "confirmed"
                effect["native_binding"] = {"dispatchId": "d", "workerId": "w", "taskId": "t",
                                            "runId": "run", "worktreeId": "wt",
                                            "terminalHandle": None, "terminalResourceId": None}
                state["cleanup"]["d"] = {"schema": "pod-cleanup/v1", "state": "released",
                                         "binding": effect["native_binding"],
                                         "runtime": "runtime", "repeat_allowed": False}
                _write(path, state)
            self.assertEqual(status(project, "objective")["phase"], "candidate")
            intervention(project, "objective", owner="owner", task="t",
                         correction=self.correction("first"))
            self.assertEqual(status(project, "objective")["phase"], "candidate")
            intervention(project, "objective", owner="owner", task="t",
                         correction=self.correction("second"))
            projection = status(project, "objective")
            self.assertEqual(projection["phase"], "converging")
            self.assertEqual(projection["pending_diagnosis"], ["t"])
            blocked = decide(project, "objective", owner="owner",
                             action=action(kind="workflow_dispatch", target="ci/linux"), now=NOW)
            self.assertEqual(blocked["decision"], "DEFER")
            self.assertIn("integration_unsettled", [r["code"] for r in blocked["reasons"]])

    def test_a_malformed_journal_asks_for_migration_rather_than_raising(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            decide(project, "objective", owner="owner", action=action(), now=NOW)
            from pod.governor import _record_path
            from pod.util import atomic_json
            for broken in ({"schema": "pod-governor/v1", "revision": 1, "actions": [{}]},
                           {"schema": "pod-governor/v1", "revision": 1,
                            "actions": [{"record_id": "x", "action": {"kind": "push"},
                                         "decision": "ALLOW", "outcome": "pending"}]},
                           {"schema": "pod-governor/v1", "revision": 1, "actions": "not a list"}):
                with self.subTest(broken=str(broken)[:40]):
                    atomic_json(_record_path(project, "objective"), broken)
                    with self.assertRaises(PodError) as caught:
                        decide(project, "objective", owner="owner", action=action(), now=NOW)
                    self.assertEqual(caught.exception.code, "state_migration_required")

    def test_the_governor_and_the_release_gate_agree_on_an_authorization(self):
        """Two validators for one schema let a record pass here and fail at the gate."""
        from pod.release import validate_authorization as gate_validator
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
            project = self.prepared(root)
            for broken in ({**authorization(), "candidate": "short"},
                           {**authorization(), "utc": "2026-09-21T00:00:00+00:00"},
                           {**authorization(), "scope": ["publish"]}):
                with self.subTest(broken=str(broken["scope"])):
                    with self.assertRaises(PodError):
                        gate_validator(broken, candidate=CANDIDATE, tree="c" * 40)
                    with self.assertRaises(PodError):
                        decide(project, "objective", owner="owner",
                               action=action(kind="merge", target="main", authorization=broken),
                               now=NOW)

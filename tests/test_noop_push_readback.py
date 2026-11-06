"""R115: a recorded push that moved no remote ref binds its validation from the remote's own update of the head.

A push already on the remote leaves nothing to start a run, so a publication-derived row may bind a push or
pull-request run on the exact head created at or after GitHub's recorded update of the branch to that head. Only
that positive proof relaxes the floor; every other answer keeps the submission, and Pod says which time it used.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.governor import (_read_journal, _record_path, _write_journal, execute, observe_candidate,
                          prepare_candidate, reconcile, status)
from pod.internal import acknowledge, run as internal_run
from tests.kernel_support import (NOW, COMMIT, COMMIT2, KernelCase, GovernorFakePort as FakePort, action,
                                  authorization, dispatch, git)
from tests.test_governor import GovernorCase

BRANCH_NAME = "agent/release"
CONTRACT = {"status": "observed", "runtime": "runtime", "capabilities": {"launch_preferences_v1": True}}


def at(minutes):
    """A whole-second provider time relative to the fixture clock, as GitHub reports it."""
    return (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def update(after, minutes, *, before=COMMIT2, kind="push", ref="refs/heads/" + BRANCH_NAME):
    return {"before": before, "after": after, "ref": ref, "timestamp": at(minutes), "activity_type": kind}


# The branch was created at the head, moved away, then set back to the head before Pod recorded the push.
HISTORY = [update(COMMIT, -10), update(COMMIT2, -20, before=COMMIT), update(COMMIT, -30, before="0" * 40,
                                                                             kind="branch_creation")]


class NoopPushCase(GovernorCase):
    def setUp(self):
        super().setUp()
        self.binding = self.prepared()["candidate"]
        self.preflight(self.binding["id"])
        self.port = FakePort()

    def push(self, *, landed):
        """Record the push through governor-execute; `landed` says the remote already held the head."""
        if landed:
            self.port.heads["origin/" + BRANCH_NAME] = COMMIT
        result = execute(self.project, "objective", owner="owner", port=self.port, now=NOW,
                         action=self.with_publish_authorization(action(candidate=self.binding["id"])))
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        return result

    def ci_run(self, minutes, *, event="pull_request", conclusion="success"):
        run = self.port._start("ci.yml", COMMIT, event=event, created_at=at(minutes))
        if conclusion is not None:
            self.port.complete(run["id"], conclusion)
        return run

    def reconcile(self, record_id, port=None):
        return reconcile(self.project, "objective", owner="owner", record_id=record_id, port=port or self.port,
                         now=NOW)


class NoopPushTests(NoopPushCase):
    """PoD#1 — a no-op push settles from a run created after the remote's own update of the head."""

    def test_a_noop_push_binds_a_run_created_after_the_remote_update(self):
        self.port.updates[BRANCH_NAME] = HISTORY
        stale = self.ci_run(-25)  # on the head, but before the remote's latest update set the branch back to it
        pushed = self.push(landed=True)
        self.assertIs(pushed["receipt"]["provider"]["ref_changed"], False)
        self.assertEqual(pushed["receipt"]["provider"]["triggered"], {})
        derived = self.derived(pushed["record_id"])
        held = self.reconcile(derived)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn(f"created at or after {(NOW + timedelta(minutes=-10)).isoformat()} (the remote's update of "
                      "the branch to the head", held["reason"])
        self.assertNotIn(stale["id"], str(held["receipt"].get("provider")))
        fresh = self.ci_run(-5, conclusion=None)
        bound = self.reconcile(derived)
        self.assertEqual((bound["status"], bound["receipt"]["provider"]["run_id"]), ("pending", fresh["id"]))
        self.port.complete(fresh["id"], "success")
        # Later updates push the proving one out of the list: the floor Pod's own readback recorded still holds.
        self.port.updates[BRANCH_NAME] = [update(COMMIT, 1 + index) for index in range(21)]
        # The bound run is verified again against the same floor before it settles the row.
        settled = self.reconcile(derived)
        provider = settled["receipt"]["provider"]
        self.assertEqual((settled["status"], provider["run_id"], provider["event"]),
                         ("PASS", fresh["id"], "pull_request"))
        self.assertEqual(provider["created_after"],
                         {"at": (NOW + timedelta(minutes=-10)).isoformat(), "basis": "remote_update"})
        self.assertEqual(self.port.update_reads, [BRANCH_NAME] * 2)

    def test_a_bound_run_from_before_the_remote_update_is_rejected(self):
        self.port.updates[BRANCH_NAME] = HISTORY
        pushed = self.push(landed=True)
        derived = self.derived(pushed["record_id"])
        stale = self.ci_run(-25)
        journal = _read_journal(_record_path(self.project, "objective"))
        row = next(row for row in journal["actions"] if row["record_id"] == derived)
        row["receipt"]["provider"] = {"run_id": stale["id"]}
        _write_journal(_record_path(self.project, "objective"), journal)
        held = self.reconcile(derived)
        self.assertEqual(held["status"], "pending")
        self.assertIn(f"run {stale['id']} was not created at or after", held["reason"])
        self.assertIn("the remote's update of the branch to the head", held["reason"])


class ReadbackSettlesUnknownTests(KernelCase):
    """PoD#2 — the same row recorded UNKNOWN settles PASS by governor-reconcile, and the objective closes."""

    def setUp(self):
        super().setUp()
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        (self.project / "README.md").write_text("candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write(self.stored())
        self.observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        self.remote = FakePort()
        for target, value in (("pod.internal._governor_projection", {"outstanding": []}),
                              ("pod.orca.contract", CONTRACT)):
            started = patch(target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        remote = patch("pod.governor_effects.GhPort", side_effect=lambda _project: self.remote)
        remote.start()
        self.addCleanup(remote.stop)

    def op(self, operation, **fields):
        request = {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}
        return acknowledge(operation, request, internal_run(operation, request))

    def consent(self, scope):
        return authorization(candidate=self.observed["commit"], tree=self.observed["tree"], scope=(scope,),
                             target="origin/target")

    def test_an_unknown_noop_validation_settles_by_readback_and_the_objective_closes(self):
        # The candidate was pushed with plain git; GitHub recorded that update and the pull request's run passed.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.remote.heads["origin/candidate"] = self.candidate
        self.remote.updates["candidate"] = [
            {"before": self.base, "after": self.candidate, "ref": "refs/heads/candidate",
             "timestamp": (now - timedelta(minutes=10)).isoformat(), "activity_type": "push"}]
        run = self.remote._start("ci.yml", self.candidate, event="pull_request",
                                 created_at=(now - timedelta(minutes=5)).isoformat())
        self.remote.complete(run["id"], "success")
        pushed = self.op("governor-execute", action=action(
            candidate=self.observed["commit"], target="origin/candidate", unit="delivery",
            effects=["workflow:ci.yml"], authorization=self.consent("publish")))
        self.assertEqual((pushed["outcome"], pushed["receipt"]["provider"]["ref_changed"]), ("PASS", False))
        derived = [row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                   if row.get("derived_from") == pushed["record_id"]][0]["record_id"]
        self.op("governor-outcome", record_id=derived, outcome="UNKNOWN", detail="recorded by hand while stuck")
        stuck = internal_run("map", {"project": str(self.project), "objective": "objective"})["finalization"]
        self.assertEqual(stuck["records"]["validation_readback"][0]["record"], derived)
        settled = self.op("governor-reconcile", record_id=derived)
        self.assertEqual((settled["status"], settled["receipt"]["provider"]["run_id"],
                          settled["receipt"]["provider"]["created_after"]["basis"]),
                         ("PASS", run["id"], "remote_update"))
        decided = self.op("governor", action=action(kind="merge", candidate=self.observed["commit"],
                                                    target="origin/target", unit="delivery", effects=[],
                                                    authorization=self.consent("merge")))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        merged = self.op("governor-outcome", record_id=decided["record_id"], outcome="PASS",
                         provider={"merge_commit": result, "method": "merge"})
        self.assertNotIn("validation_readback", merged["finalization"]["remaining"])
        self.op("checkpoint", value=self.core(obligations=self.stored(), delivery={"record": decided["record_id"]}))
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        closed = self.op("checkpoint", value=self.core(obligations=rows, close=True))
        self.assertEqual((closed["finalization"]["remaining"], closed["finalization"]["finished"]), ([], True))
        self.assertIsNotNone(closed["checkpoint"]["closure"])


class RefChangingPushTests(NoopPushCase):
    """PoD#3 — a push that set the head keeps the created-after-submission rule."""

    def assert_submission_rule(self, derived, *, note=None):
        earlier = self.ci_run(-5)
        held = self.reconcile(derived)
        self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
        self.assertIn(f"created at or after {NOW.isoformat()} (the submission", held["reason"])
        if note:
            self.assertIn(note, held["reason"])
        self.assertNotIn(earlier["id"], str(held["receipt"].get("provider")))
        later = self.port._start("ci.yml", COMMIT, event="push", created_at=at(1))
        self.port.complete(later["id"], "success")
        settled = self.reconcile(derived)
        self.assertEqual((settled["status"], settled["receipt"]["provider"]["run_id"]), ("PASS", later["id"]))
        self.assertEqual(settled["receipt"]["provider"]["created_after"],
                         {"at": NOW.isoformat(), "basis": "publication"})

    def test_a_push_that_moved_the_ref_keeps_the_submission(self):
        # Even an activity read that would show the head earlier is never consulted for a ref-changing push.
        self.port.updates[BRANCH_NAME] = HISTORY
        pushed = self.push(landed=False)
        self.assertIs(pushed["receipt"]["provider"]["ref_changed"], True)
        self.assert_submission_rule(self.derived(pushed["record_id"]))
        self.assertEqual(self.port.update_reads, [])

    def test_an_older_row_whose_activity_shows_the_push_set_the_head_keeps_the_submission(self):
        pushed = self.decide(action(candidate=self.binding["id"]))
        self.performed(pushed["record_id"])  # a caller-performed push, recorded without the ref_changed fact
        row = next(row for row in status(self.project, "objective")["actions"]
                   if row["record_id"] == pushed["record_id"])
        self.assertNotIn("ref_changed", row["provider"])
        self.port.updates[BRANCH_NAME] = [update(COMMIT, 0.05), update(COMMIT2, -20, before=COMMIT)]
        self.assert_submission_rule(self.derived(pushed["record_id"]),
                                    note="does not show it set to " + COMMIT[:12])


class FallbackTests(NoopPushCase):
    """A no-op push without positive proof of the remote's earlier update keeps the submission, saying why."""

    def test_every_unproved_activity_keeps_the_submission(self):
        pushed = self.push(landed=True)
        derived = self.derived(pushed["record_id"])
        earlier = self.ci_run(-5)
        newer_only = [update(COMMIT, 1 + index) for index in range(21)]
        cases = (
            ("unreadable", None, "is not readable (remote_read_failed)"),
            ("empty", [], "no update of branch agent/release at or before the submission is listed"),
            ("truncated", newer_only, "were truncated (20 listed, limit 20) and none is at or before"),
            ("only later", newer_only[:3], "no update of branch agent/release at or before the submission"),
            ("other commit", [update(COMMIT2, -10)], "does not show it set to " + COMMIT[:12]),
            ("deleted", [update(COMMIT, -10, kind="branch_deletion")], "does not show it set to"),
            ("same second disagrees", [update(COMMIT, -10), update(COMMIT2, -10)], "does not show it set to"),
            ("other ref", [update(COMMIT, -10, ref="refs/heads/other")], "no update of branch agent/release"),
            ("unreadable time", [{**update(COMMIT, -10), "timestamp": None}], "reports no readable time"),
        )
        for name, updates, reason in cases:
            with self.subTest(name):
                port = FakePort()
                port.runs_by, port.run_status = self.port.runs_by, self.port.run_status
                port.heads = dict(self.port.heads)
                if updates is None:
                    port.unreadable.add("activity")
                else:
                    port.updates[BRANCH_NAME] = updates
                held = self.reconcile(derived, port)
                self.assertEqual((held["status"], held["action"]), ("pending", "hold"))
                self.assertIn(f"created at or after {NOW.isoformat()} (the submission", held["reason"])
                self.assertIn(reason, held["reason"])
                self.assertNotIn(earlier["id"], str(held["receipt"].get("provider")))
        # The same row settles from the earlier run once the remote's own update is proved.
        self.port.updates[BRANCH_NAME] = HISTORY
        self.assertEqual(self.reconcile(derived)["status"], "PASS")

    def test_a_requested_dispatch_keeps_its_own_rule(self):
        self.push(landed=True)
        self.port.updates[BRANCH_NAME] = HISTORY
        self.port._start("release.yml", COMMIT, event="workflow_dispatch", created_at=at(-5))
        self.port.stamp = at(1)
        requested = execute(self.project, "objective", owner="owner", port=self.port, now=NOW,
                            action=self.with_publish_authorization(dispatch(candidate=self.binding["id"],
                                                                            target="release.yml")))
        self.assertEqual(requested["receipt"]["provider"]["created_at"], at(1))
        settled = self.reconcile(requested["record_id"])
        self.assertNotIn("created_after", settled["receipt"]["provider"])
        self.assertEqual(self.port.update_reads, [])


class GuidanceTests(unittest.TestCase):
    """PoD#4 — the installed guidance names Governor publication as the route."""

    def test_the_governor_reference_names_governor_publication(self):
        text = (Path(__file__).resolve().parents[1] / "skills" / "pod" / "references" / "governor.md").read_text()
        self.assertIn("Publish the candidate through `governor-execute` (push or pr_update), never plain git.", text)
        self.assertIn("created at or after the remote's own update of the branch to it", text)
        from pod.governor import NEXT_ACTIONS
        self.assertIn("through governor-execute (push or pr_update), never plain git",
                      NEXT_ACTIONS["candidate_unpublished"])

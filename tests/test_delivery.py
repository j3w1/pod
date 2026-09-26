"""Candidate-bound delivery authorization and exact timestamp boundaries."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import (_check_authority, _check_unresolved, _empty_journal, _record_path, _write_journal,
                          validate_authorization)
from pod.governance import require_governance_current
from pod.ledger import _authorization_granted, read
from pod.records import evidence_record
from tests.common import proof
from tests.kernel_support import COMMIT, TREE, KernelCase, authorization, git


class DeliveryAuthorizationTests(unittest.TestCase):
    def check(self, kind: str, consent=None, *, binding=None, deploys=False, releases=False):
        reasons = []
        action = {"kind": kind, "candidate": COMMIT, "authorization": consent}
        _check_authority(action, {"commit": COMMIT, "tree": TREE} if binding is None else binding,
                         deploys, releases, reasons)
        return action, reasons

    def test_publication_and_merge_have_distinct_exact_scopes(self):
        for kind, scope in (("push", "publish"), ("pr_update", "publish"), ("merge", "merge")):
            with self.subTest(kind=kind):
                _, reasons = self.check(kind)
                self.assertEqual([(row["class"], row["code"]) for row in reasons],
                                 [("authorization", "authorization_missing")])
                self.assertIn(scope, reasons[0]["detail"])
                self.assertIn(COMMIT, reasons[0]["detail"])
                self.assertIn(TREE, reasons[0]["detail"])
                self.assertIn("merge remotely / keep local / defer", reasons[0]["detail"])
                _, accepted = self.check(kind, authorization(scope=(scope,)))
                self.assertEqual(accepted, [])

    def test_candidate_tree_and_wrong_scope_never_inherit_consent(self):
        for consent in (authorization(candidate="b" * 40, scope=("publish",)),
                        authorization(tree="d" * 40, scope=("publish",)),
                        authorization(scope=("merge",)),
                        {**authorization(scope=("publish",)), "utc": "not a time"}):
            with self.subTest(consent=consent):
                _, reasons = self.check("push", consent)
                self.assertEqual(reasons[0]["code"], "authorization_missing")

    def test_local_only_holds_every_remote_kind(self):
        for kind in ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic",
                     "cancel_validation", "merge", "release", "deploy"):
            with self.subTest(kind=kind):
                self.assertIn("authorization_missing", [row["code"] for row in self.check(kind)[1]])
        # The remote delivery decision's publish consent covers CI, reruns, diagnostics and cancellation.
        for kind in ("workflow_dispatch", "validation_rerun", "remote_diagnostic", "cancel_validation"):
            with self.subTest(consented=kind):
                self.assertEqual(self.check(kind, authorization(scope=("publish",)))[1], [])

    def test_authorization_and_evidence_timestamps_share_utc_normalization(self):
        normalized = validate_authorization(
            {**authorization(scope=("publish",)), "utc": "2026-09-25T15:01:22.5-05:00"})
        self.assertEqual(normalized["utc"], "2026-09-25T20:01:22.500000Z")
        receipt = {**proof("O1", COMMIT), "timestamp": "2026-09-25T15:01:22-05:00"}
        self.assertEqual(evidence_record(receipt)["timestamp"], "2026-09-25T20:01:22Z")
        for utc in ("2026-09-25T20:01:22", "2026-13-25T20:01:22Z"):
            with self.subTest(utc=utc), self.assertRaises(PodError) as caught:
                validate_authorization({**authorization(), "utc": utc})
            self.assertEqual(caught.exception.code, "invalid_authorization")
            self.assertIn("2026-09-25T20:01:22Z", str(caught.exception))

    def test_a_published_push_cannot_grant_merge_authority(self):
        row = {"decision": "ALLOW", "action": {"kind": "push", "candidate": COMMIT,
                                              "authorization": authorization(scope=("publish",))},
               "commit": COMMIT, "candidate_id": "candidate-id"}
        with patch("pod.governor._read_journal", return_value={"actions": [row]}):
            granted = _authorization_granted(Path("/fixture"), "objective")
            self.assertTrue(granted("publish", COMMIT))
            self.assertFalse(granted("merge", COMMIT))


class VerifiedDeliveryTests(KernelCase):
    def prepare_result(self, method="merge", *, policy_change=False):
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        path = self.project / ("AGENTS.md" if policy_change else "README.md")
        path.write_text(path.read_text() + "candidate change\n")
        git(self.project, "add", path.name)
        git(self.project, "commit", "-qm", "candidate")
        candidate = git(self.project, "rev-parse", "HEAD")
        tree = git(self.project, "rev-parse", "HEAD^{tree}")
        git(self.project, "checkout", "-q", "target")
        if method == "merge":
            git(self.project, "merge", "--no-ff", "-qm", "merge result", "candidate")
        elif method == "squash":
            git(self.project, "merge", "--squash", "candidate")
            git(self.project, "commit", "-qm", "squash result")
        else:
            git(self.project, "merge", "--ff-only", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        self.candidate = candidate
        binding = {"id": "candidate-1", "commit": candidate, "tree": tree}
        unit = {"name": "delivery", "generation": 1, "history": ["candidate-1"],
                "preflight": {}, "published": {}, "tasks": [], "candidate": binding,
                "branch": {"remote": "origin", "base": "target", "branch": "delivery"}}
        row = {"record_id": "merge-1", "logical_key": "merge-1", "attempt": 1,
               "action": {"kind": "merge", "candidate": "candidate-1", "target": "origin/target",
                          "unit": "delivery", "authorization": authorization(
                              candidate=candidate, tree=tree, scope=("merge",))},
               "target_binding": {"remote": "origin", "base": "target", "ref": "refs/remotes/origin/target"},
               "candidate_id": "candidate-1", "commit": candidate, "decision": "ALLOW", "outcome": "PASS",
               "receipt": {"provider": {"merge_commit": result, "method": method}},
               "warnings": [], "reasons": [], "classifications": []}
        journal = _empty_journal()
        journal["units"]["delivery"] = unit
        journal["actions"].append(row)
        _write_journal(_record_path(self.project, "objective"), journal)
        return candidate, tree, result

    def check_exact_result(self, method):
        candidate, tree, result = self.prepare_result(method, policy_change=True)
        before = read(self.project, "objective")["checkpoint"]
        outcome = self.write(self.stored(), delivery={"record": "merge-1"})
        accepted = outcome["checkpoint"]
        delivery = accepted["delivery"]
        self.assertEqual(outcome["report"]["delivery"], delivery)
        self.assertEqual((delivery["method"], delivery["base"], delivery["result"],
                          delivery["candidate"], delivery["tree"]),
                         (method, self.base, result, candidate, tree))
        self.assertEqual(accepted["governance_sources"], before["governance_sources"])
        self.assertEqual(accepted["governance"]["base"], self.base)
        self.assertEqual(self.write(self.stored())["checkpoint"]["delivery"], delivery)
        require_governance_current(self.project, accepted)
        self.assertEqual(self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]["delivery"],
                         delivery)
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        closed = self.write(rows, close=True)["checkpoint"]
        self.assertIsNotNone(closed["closure"])
        self.assertEqual(closed["delivery"], delivery)

    def test_exact_merge_keeps_trusted_base(self):
        self.check_exact_result("merge")

    def test_exact_squash_keeps_trusted_base(self):
        self.check_exact_result("squash")

    def test_direct_child_fast_forward_keeps_trusted_base(self):
        self.check_exact_result("fast-forward")

    def test_wrong_target_and_second_record_refuse_without_state_write(self):
        self.prepare_result()
        journal_path = _record_path(self.project, "objective")
        from pod.governor import _read_journal
        admitted = _read_journal(journal_path)
        git(self.project, "update-ref", "refs/remotes/origin/other",
            git(self.project, "rev-parse", "refs/remotes/origin/target"))
        other = {"remote": "origin", "base": "other", "ref": "refs/remotes/origin/other"}
        before = read(self.project, "objective")
        # Another admitted target with an equal SHA and a row without an admitted target both refuse;
        # a later unit branch edit is irrelevant.
        for target, binding in (("origin/other", other), ("origin/target", None)):
            journal = _read_journal(journal_path)
            journal["actions"][0]["action"]["target"] = target
            journal["actions"][0]["target_binding"] = binding
            _write_journal(journal_path, journal)
            with self.subTest(target=target, binding=binding), self.assertRaises(PodError) as caught:
                self.write(self.stored(), delivery={"record": "merge-1"})
            self.assertEqual(caught.exception.detail["detail"], "target_identity")
            self.assertEqual(read(self.project, "objective"), before)
        admitted["units"]["delivery"]["branch"]["base"] = "other"
        _write_journal(journal_path, admitted)
        self.assertEqual(self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]["delivery"]
                         ["target_ref"], "refs/remotes/origin/target")
        with self.assertRaises(PodError) as second:
            self.write(self.stored(), delivery={"record": "another-record"})
        self.assertEqual(second.exception.code, "delivery_recorded")

    def test_target_moved_after_verified_result_holds_future_work(self):
        self.prepare_result()
        delivered = self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]
        (self.project / "README.md").write_text("later target change\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later target")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        with self.assertRaises(PodError) as caught:
            self.write(self.stored())
        self.assertEqual(caught.exception.code, "governance_changed")
        self.assertIn("target moved after the recorded delivery", str(caught.exception))
        self.assertEqual(read(self.project, "objective")["checkpoint"]["delivery"], delivered["delivery"])

    def test_owner_refresh_after_delivery_restores_currency(self):
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        (self.project / "README.md").write_text("later target change\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later target")
        moved = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", moved)
        refreshed = self.write(self.stored(), governance_refresh=True,
                               revision_authority={"provenance": "user_direct",
                                                   "instruction": f"Adopt target at {moved}"},
                               governance={"base_ref": "refs/remotes/origin/target", "base": moved})
        self.assertEqual(refreshed["checkpoint"]["governance"]["base"], moved)
        require_governance_current(self.project, refreshed["checkpoint"])
        self.assertEqual(self.write(self.stored())["checkpoint"]["governance"]["base"], moved)

    def test_missing_record_and_wrong_authorization_refuse_without_checkpoint_write(self):
        candidate, tree, _ = self.prepare_result()
        from pod.governor import _read_journal
        journal_path = _record_path(self.project, "objective")
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as missing:
            self.write(self.stored(), delivery={"record": "unknown"})
        self.assertEqual(missing.exception.detail["detail"], "record_missing")
        for consent in (None, authorization(candidate="b" * 40, tree=tree, scope=("merge",)),
                        authorization(candidate=candidate, tree="d" * 40, scope=("merge",)),
                        authorization(candidate=candidate, tree=tree, scope=("publish",))):
            journal = _read_journal(journal_path)
            journal["actions"][0]["action"]["authorization"] = consent
            _write_journal(journal_path, journal)
            with self.subTest(consent=consent), self.assertRaises(PodError) as refused:
                self.write(self.stored(), delivery={"record": "merge-1"})
            self.assertEqual(refused.exception.detail["detail"], "authorization_invalid")
            self.assertEqual(read(self.project, "objective"), before)

    def test_readback_mismatch_and_method_mismatch_refuse(self):
        _, _, result = self.prepare_result()
        from pod.governor import _read_journal
        journal_path = _record_path(self.project, "objective")
        journal = _read_journal(journal_path)
        journal["actions"][0]["receipt"]["provider"]["method"] = "squash"
        _write_journal(journal_path, journal)
        with self.assertRaises(PodError) as method:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(method.exception.detail["detail"], "method_mismatch")
        journal["actions"][0]["receipt"]["provider"]["method"] = "merge"
        git(self.project, "checkout", "-qb", "unrelated", self.base)
        (self.project / "README.md").write_text("unrelated\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "unrelated")
        journal["actions"][0]["receipt"]["provider"]["merge_commit"] = git(self.project, "rev-parse", "HEAD")
        _write_journal(journal_path, journal)
        with self.assertRaises(PodError) as readback:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(readback.exception.detail["detail"], "readback_mismatch")
        self.assertEqual(git(self.project, "rev-parse", "refs/remotes/origin/target"), result)

    def test_nonexact_parentage_and_tree_mismatch_refuse(self):
        candidate, tree, _ = self.prepare_result()
        from pod.governor import _read_journal
        journal_path = _record_path(self.project, "objective")
        journal = _read_journal(journal_path)
        # A result with the right tree but a different second parent is not this merge.
        extra = git(self.project, "commit-tree", tree, "-p", self.base, "-m", "other parent")
        rebased = git(self.project, "commit-tree", tree, "-p", self.base, "-p", extra,
                      "-m", "nonexact merge")
        git(self.project, "update-ref", "refs/remotes/origin/target", rebased)
        journal["actions"][0]["receipt"]["provider"]["merge_commit"] = rebased
        _write_journal(journal_path, journal)
        with self.assertRaises(PodError) as nonexact:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(nonexact.exception.detail["detail"], "result_not_exact")
        self.assertNotEqual(extra, candidate)
        # A single-parent result with another tree is not the authorized squash.
        wrong_tree = git(self.project, "rev-parse", self.base + "^{tree}")
        squashed = git(self.project, "commit-tree", wrong_tree, "-p", self.base,
                       "-m", "wrong tree")
        git(self.project, "update-ref", "refs/remotes/origin/target", squashed)
        journal["actions"][0]["receipt"]["provider"].update(merge_commit=squashed, method="squash")
        _write_journal(journal_path, journal)
        with self.assertRaises(PodError) as mismatch:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(mismatch.exception.detail["detail"], "tree_mismatch")

    def move_target(self) -> str:
        (self.project / "README.md").write_text("later target change\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later target")
        moved = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", moved)
        return moved

    def test_moved_target_refuses_recording_a_stale_delivery(self):
        self.prepare_result()
        self.move_target()
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as stale:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(stale.exception.code, "governance_changed")
        self.assertEqual(read(self.project, "objective"), before)

    def test_replaying_the_delivery_never_skips_currency_or_closes(self):
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        self.move_target()
        before = read(self.project, "objective")
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        for obligations, fields in ((self.stored(), {}), (rows, {"close": True})):
            with self.subTest(close=bool(fields)), self.assertRaises(PodError) as caught:
                self.write(obligations, delivery={"record": "merge-1"}, **fields)
            self.assertEqual(caught.exception.code, "governance_changed")
            self.assertEqual(read(self.project, "objective"), before)

    def test_a_report_map_cannot_close_against_a_moved_target(self):
        from pod.github import repository_context
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        review = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                  "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src correct?",
                  "candidate": self.candidate, "existing_evidence": "unit", "insufficiency": "no review",
                  "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.write([*self.stored(), review])
        self.port.placement.update(branch=repository_context(self.project)["branch"])
        frozen = self.packet(["A"], role="review")
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        self.move_target()
        rows = self.stored()
        for row in rows:
            row.pop("executor", None)
            row.pop("wait", None)
            row["state"] = "satisfied"
        rows[0]["evidence"] = [self.proof("O1")]
        before = read(self.project, "objective")["checkpoint"]
        with self.assertRaises(PodError) as caught:
            self.report(admission, frozen, map={"obligations": rows, "close": True})
        self.assertEqual(caught.exception.code, "governance_changed")
        self.assertEqual(read(self.project, "objective")["checkpoint"], before)

    def test_a_later_snapshot_decision_ends_the_old_delivery_exception(self):
        _, _, result = self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        moved = self.move_target()
        self.write(self.stored(), governance_refresh=True,
                   revision_authority={"provenance": "user_direct", "instruction": f"Adopt target at {moved}"},
                   governance={"base_ref": "refs/remotes/origin/target", "base": moved})
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        with self.assertRaises(PodError) as caught:
            require_governance_current(self.project, read(self.project, "objective")["checkpoint"])
        self.assertEqual(caught.exception.code, "governance_changed")
        with self.assertRaises(PodError):
            self.write(self.stored())


class AdmittedMergeTargetTests(KernelCase):
    def observed_candidate(self):
        from pod.governor import observe_candidate
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        (self.project / "README.md").write_text("candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "candidate")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write(self.stored())
        return observe_candidate(self.project, base_ref="refs/remotes/origin/target")

    def merge(self, observed, target, consent_target="origin/target"):
        from pod.governor import decide
        from tests.kernel_support import action
        consent = authorization(candidate=observed["commit"], tree=observed["tree"], scope=("merge",),
                                target=consent_target)
        return decide(self.project, "objective", owner="owner",
                      action=action(kind="merge", candidate=observed["commit"], target=target, unit="delivery",
                                    effects=[], authorization=consent),
                      native_projection={"outstanding": []})

    def test_a_branchless_unit_cannot_merge(self):
        from pod.governor import prepare_candidate
        observed = self.observed_candidate()
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed)
        for target in ("origin/other", "other", "refs/remotes/origin/other"):
            with self.subTest(target=target):
                held = self.merge(observed, target)
                self.assertEqual(held["decision"], "DEFER")
                self.assertIn("unit_unbound", [row["code"] for row in held["reasons"]])

    def test_a_retarget_leaves_no_recorded_merge_consent(self):
        from pod.governor import prepare_candidate, record_outcome, status
        observed = self.observed_candidate()
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        row = self.merge(observed, "origin/target")
        self.assertEqual(row["decision"], "ALLOW")
        record_outcome(self.project, "objective", owner="owner", record_id=row["record_id"], outcome="FAILED")
        self.assertEqual(status(self.project, "objective")["units"]["delivery"]["authorization"]["merge"],
                         "RECORDED")
        git(self.project, "update-ref", "refs/remotes/origin/other", self.base)
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "other", "branch": "candidate"})
        self.assertEqual(status(self.project, "objective")["units"]["delivery"]["authorization"]["merge"],
                         "MISSING")
        self.assertFalse(_authorization_granted(self.project, "objective")("merge", observed["commit"]))

    def test_merge_decision_binds_the_prepared_target_that_delivery_checks(self):
        from pod.governor import prepare_candidate, record_outcome
        observed = self.observed_candidate()
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        # Another target, another spelling of this target, or consent given for another target all hold.
        for target, consent_target, code in (("other-target", "origin/target", "merge_target_mismatch"),
                                             ("target", "origin/target", "merge_target_mismatch"),
                                             ("origin/target", "origin/other", "authorization_missing")):
            with self.subTest(target=target, consent=consent_target):
                held = self.merge(observed, target, consent_target)
                self.assertEqual(held["decision"], "DEFER")
                self.assertIn(code, [row["code"] for row in held["reasons"]])
        row = self.merge(observed, "origin/target")
        self.assertEqual(row["decision"], "ALLOW")
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        record_outcome(self.project, "objective", owner="owner", record_id=row["record_id"], outcome="PASS",
                       provider={"merge_commit": result, "method": "merge"})
        delivered = self.write(self.stored(), delivery={"record": row["record_id"]})["checkpoint"]["delivery"]
        self.assertEqual((delivered["target_ref"], delivered["result"]), ("refs/remotes/origin/target", result))


class ReviewOverlapTests(KernelCase):
    def verdict(self, *, purpose: str, role: str, candidate: str) -> list[str]:
        projection = {"outstanding": [{"task": "review-task", "role": role, "candidate": candidate}]}
        unit = {"name": "delivery", "tasks": ["review-task"]}
        state = {"admissions": {}, "interventions": {}, "checkpoint": {}}
        reasons, warnings = [], []
        _check_unresolved({"unit": "delivery"}, {"actions": []}, "key", "candidate-id", state, unit,
                          "candidate-id", projection, True, purpose,
                          "workflow_dispatch" if purpose == "validation" else "merge",
                          self.project, {"commit": self.base}, reasons, warnings)
        return [reason["code"] for reason in reasons]

    def test_same_candidate_review_can_overlap_validation_but_holds_release(self):
        self.assertEqual(self.verdict(purpose="validation", role="review", candidate=self.base), [])
        self.assertEqual(self.verdict(purpose="release", role="review", candidate=self.base),
                         ["integration_unsettled"])

    def test_other_candidate_review_and_implementation_still_hold_validation(self):
        self.assertEqual(self.verdict(purpose="validation", role="review", candidate="0" * 40),
                         ["integration_unsettled"])
        self.assertEqual(self.verdict(purpose="validation", role="implement", candidate=self.base),
                         ["integration_unsettled"])

    def test_a_review_admitted_on_a_moving_ref_never_counts_as_this_candidate(self):
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), self.base)
        for alias in ("HEAD", "target", self.base[:12]):
            with self.subTest(alias=alias):
                self.assertEqual(self.verdict(purpose="validation", role="review", candidate=alias),
                                 ["integration_unsettled"])


class LocalOnlyReportTests(KernelCase):
    def test_user_direct_local_delivery_reports_missing_scopes_and_unrun_hosted_checks(self):
        self.intake()
        delivery = {"id": "D", "kind": "delivery", "provenance": "user_direct",
                    "source": {"instruction": "Keep this delivery local"}, "check": "local delivery is complete",
                    "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        outcome = self.write([*self.stored(), delivery],
                             revision_authority={"provenance": "user_direct",
                                                 "instruction": "Keep this delivery local"})
        report = outcome["report"]
        self.assertEqual(report["hosted_checks"], "NOT_RUN")
        self.assertEqual(report["delivery_authorization"],
                         {"unprepared": {"publish": "MISSING", "merge": "MISSING"}})


if __name__ == "__main__":
    unittest.main()

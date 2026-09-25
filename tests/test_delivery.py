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

    def test_local_only_has_no_remote_authority_and_ci_is_unchanged(self):
        for kind in ("push", "pr_update", "merge", "release", "deploy"):
            with self.subTest(kind=kind):
                self.assertIn("authorization_missing", [row["code"] for row in self.check(kind)[1]])
        self.assertEqual(self.check("workflow_dispatch")[1], [])

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
               "action": {"kind": "merge", "candidate": "candidate-1", "target": "target",
                          "unit": "delivery", "authorization": authorization(
                              candidate=candidate, tree=tree, scope=("merge",))},
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
        journal = _read_journal(journal_path)
        journal["units"]["delivery"]["branch"]["base"] = "other"
        git(self.project, "update-ref", "refs/remotes/origin/other",
            git(self.project, "rev-parse", "refs/remotes/origin/target"))
        _write_journal(journal_path, journal)
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as caught:
            self.write(self.stored(), delivery={"record": "merge-1"})
        self.assertEqual(caught.exception.detail["detail"], "target_identity")
        self.assertEqual(read(self.project, "objective"), before)
        journal["units"]["delivery"]["branch"]["base"] = "target"
        _write_journal(journal_path, journal)
        self.write(self.stored(), delivery={"record": "merge-1"})
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

    def test_moved_target_can_store_verified_provider_result_but_stays_stale(self):
        self.prepare_result()
        (self.project / "README.md").write_text("later target change\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later target")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        delivered = self.write(self.stored(), delivery={"record": "merge-1"})["checkpoint"]
        self.assertIn("delivery", delivered)
        with self.assertRaises(PodError) as stale:
            require_governance_current(self.project, delivered)
        self.assertEqual(stale.exception.code, "governance_changed")


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


if __name__ == "__main__":
    unittest.main()

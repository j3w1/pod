"""Candidate-bound delivery authorization and exact timestamp boundaries."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import _check_authority, validate_authorization
from pod.ledger import _authorization_granted
from pod.records import evidence_record
from tests.common import proof
from tests.kernel_support import COMMIT, TREE, authorization


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


if __name__ == "__main__":
    unittest.main()

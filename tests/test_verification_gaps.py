"""A checkpoint's verification gaps are a list of text items; a wrong type is refused, never converted (CE #263)."""

import unittest

from pod.ledger import checkpoint, read
from tests.kernel_support import COMMIT, PROJECT_CONFIG, body
from tests.test_governor import GovernorCase

SENTENCE = "The live matrix and the installed-bundle trial are not yet run for this candidate. " * 4


class VerificationGapsTests(GovernorCase):
    def write(self, gaps):
        self.configure(PROJECT_CONFIG, None)
        return checkpoint(self.project, "objective", owner="owner", value={**body(COMMIT), "verification_gaps": gaps},
                          native={"runtime": "runtime"})

    def test_a_sentence_is_refused_precisely_and_never_counted_per_character(self):
        with self.assertRaises(Exception) as caught:
            self.write(SENTENCE)
        error = caught.exception
        self.assertEqual((error.code, error.detail["detail"]), ("invalid_checkpoint", "verification_gaps_invalid"))
        self.assertEqual(error.detail["referent"]["field"], "verification_gaps")
        self.assertIn("got str, not a list", str(error))
        self.assertIn("own list item", error.detail["next_action"])
        self.assertNotIn(SENTENCE[:30], str(error.detail))
        self.assertIsNone(read(self.project, "objective"))

    def test_other_wrong_types_and_items_are_refused(self):
        for gaps in (None, 3, {"gap": "x"}, [3], [""], ["  "], [["nested"]], ["x" * 1025], ["nul\0"], ["x"] * 65):
            with self.subTest(gaps=repr(gaps)[:40]), self.assertRaises(Exception) as caught:
                self.write(gaps)
            self.assertEqual(caught.exception.detail["detail"], "verification_gaps_invalid")

    def test_genuine_gaps_and_the_empty_list_are_accepted_and_kept_as_given(self):
        for gaps in ([], ["live matrix"], ["live matrix", "installed-bundle trial"]):
            with self.subTest(gaps=gaps):
                self.write(gaps)
                self.assertEqual(read(self.project, "objective")["checkpoint"]["verification_gaps"], gaps)

    def test_a_stored_legacy_sentence_is_one_unresolved_requirement_not_hundreds(self):
        from pod import governor
        for purpose, collected in (("release", 0), ("push", 1)):
            reasons, warnings = [], []
            governor._check_readiness({"verification_gaps": SENTENCE}, None, None, None, purpose, {}, reasons, warnings)
            found = [reasons, warnings][collected]
            self.assertEqual(len(found), 1, purpose)
            self.assertIn("1 verification gap(s) remain", str(found[0]))
            self.assertIn("rewrite it as one item per gap", str(found[0]))
            self.assertNotIn(str(len(SENTENCE)), str(found[0]))
        reasons, warnings = [], []
        governor._check_readiness({"verification_gaps": ["a", "b"]}, None, None, None, "release", {}, reasons, warnings)
        self.assertIn("2 verification gap(s) remain", str(reasons[0]))


if __name__ == "__main__":
    unittest.main()

"""A checkpoint's verification gaps are a list of text items; a wrong type is refused, never converted (CE #263)."""

from pathlib import Path
import unittest

from pod.governor import decide, record_preflight
from pod.ledger import checkpoint, read
from tests.kernel_support import COMMIT, NOW, PROJECT_CONFIG, action, authorization, body
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

    def test_a_stored_non_list_is_one_unresolved_requirement_never_none_or_hundreds(self):
        from pod import governor
        for purpose, collected in (("release", 0), ("push", 1)):
            reasons, warnings = [], []
            governor._check_readiness({"verification_gaps": SENTENCE}, None, None, None, purpose, {}, reasons, warnings)
            found = [reasons, warnings][collected]
            self.assertEqual(len(found), 1, purpose)
            self.assertIn("1 verification gap(s) remain", str(found[0]))
            self.assertIn("rewrite it as one item per gap", str(found[0]))
            self.assertNotIn(str(len(SENTENCE)), str(found[0]))
        for value in (None, False, 0, "", {}, 3):
            for purpose, collected in (("release", 0), ("push", 1)):
                with self.subTest(value=value, purpose=purpose):
                    reasons, warnings = [], []
                    governor._check_readiness({"verification_gaps": value}, None, None, None, purpose, {},
                                              reasons, warnings)
                    found = [reasons, warnings][collected]
                    self.assertEqual(len(found), 1)
                    self.assertIn("1 verification gap(s) remain", str(found[0]))
        for purpose in ("release", "push"):
            reasons, warnings = [], []
            governor._check_readiness({"verification_gaps": []}, None, None, None, purpose, {}, reasons, warnings)
            self.assertEqual([item for item in reasons + warnings if "verification gap" in str(item)], [])
        reasons, warnings = [], []
        governor._check_readiness({"verification_gaps": ["a", "b"]}, None, None, None, "release", {}, reasons, warnings)
        self.assertIn("2 verification gap(s) remain", str(reasons[0]))


class GapHomeTests(GovernorCase):
    """R219: a deferral on a post-merge-only gap says where that item belongs; the gate itself is unchanged."""

    def test_a_post_merge_only_gap_still_defers_and_the_deferral_names_both_homes(self):
        gap = "owner acceptance check on merged main after installation: NOT_RUN"
        candidate = self.prepared(gaps=(gap,))["candidate"]
        for check in ("unit", "workflow-lint"):
            record_preflight(self.project, "objective", owner="owner", unit="release", candidate=candidate["id"],
                             check=check, status="PASS", now=NOW)
        held = decide(self.project, "objective", owner="owner", now=NOW,
                      action=action(kind="merge", target="origin/main", candidate=candidate["id"],
                                    authorization=authorization()))
        self.assertEqual((held["decision"], [reason["code"] for reason in held["reasons"]]),
                         ("DEFER", ["verification_gaps"]))
        detail = held["reasons"][0]["detail"]
        self.assertIn("1 verification gap(s) remain", detail)
        self.assertIn("each holds the merge", detail)
        self.assertIn("post-merge or owner-acceptance item belongs in an obligation", detail)
        for words in ("verifying the merge unit itself", "holds the merge", "blocked_external or waiting",
                      "does not hold the merge", "open after the merge"):
            self.assertIn(words, held["next_action"])
            self.assertIn(words, held["explanation"])
        self.assertNotIn(gap, held["next_action"])

    def test_the_installed_delivery_reference_names_both_homes(self):
        import pod
        text = " ".join((Path(pod.__file__).parent / "references" / "governor.md").read_text().split())
        for words in ("`verification_gaps` holds only gaps in verifying the merge unit itself",
                      "Each gap holds the merge", "Record it as an obligation instead: `blocked_external`",
                      "`waiting` when it is a sequenced step after the merge",
                      "Such an obligation does not hold the merge",
                      "stays open, honestly, after the merge",
                      "When the Governor defers on `verification_gaps`, move each gap that is really post-merge"):
            self.assertIn(words, text)


if __name__ == "__main__":
    unittest.main()

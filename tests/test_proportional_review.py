"""Proportional review and reuse (R6/R7) through the pure obligation boundaries."""

from copy import deepcopy
import unittest

from pod.assurance import _bind_reuse, evidence_source
from pod.errors import PodError
from pod.obligations import accept_write, admission_refusal, admit, label_qualification, status_projection, triage
from tests import test_obligations as base
from tests.common import VERIFICATION, proof

ctx, ob, write, rows_of, by_id, refused = base.ctx, base.ob, base.write, base.rows_of, base.by_id, base.refused
MOVED = {"candidate": "c2"}


def reusable(scope):
    """A satisfied row proved at c1 whose reusable scope is the given path list."""
    definition = ob("S", boundary={"paths": scope})
    row = ob("S", "satisfied", boundary={"paths": scope}, evidence=[proof("S", definition=definition)])
    return accept_write(None, base.intake(row), ctx())


def move(state, delta, **extra):
    rows = rows_of(state)
    next(row for row in rows if row["id"] == "S")["reuse"] = {"from": "c1", **extra}
    return rows, ctx(**MOVED, git_delta=lambda source, target: delta)


class VersionOnlyBumpTests(unittest.TestCase):
    def test_a_version_only_bump_keeps_proof_for_a_repository_wide_scope(self):
        state = reusable(["."])
        rows, moved = move(state, ["VERSION"])
        reused = accept_write(state, write(state, rows), moved)
        self.assertEqual(by_id(reused, "S")["state"], "satisfied")
        self.assertEqual(by_id(reused, "S")["reuse"]["delta"], ["VERSION"])

    def test_a_version_only_bump_keeps_a_repository_wide_review(self):
        rows = [ob("S", boundary={"paths": ["src"]}),
                ob("A", kind="assurance", scope={"paths": ["."]}, question="is it correct?", candidate="c1",
                   existing_evidence="tests", insufficiency="no independent review")]
        state = accept_write(None, base.intake(*rows), ctx())
        review = admit(state, base.packet(["A"], role="review"), ctx(), admission_id="R1")
        row = base.settled_row(["A"], role="review", report={"outcome": "succeeded", "status": "validated_observation"})
        from pod.obligations import admission_binding
        row["binding"] = admission_binding(base.packet(["A"], role="review"), ctx(), state)
        admissions = {"R1": row}
        waiting = rows_of(review)
        assurance = next(item for item in waiting if item["id"] == "A")
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        assurance.pop("executor")
        value, triaged = triage(review, write(review, waiting), [], [], ctx(admissions=admissions), admission_id="R1")
        reported = accept_write(review, value, ctx(admissions=admissions), triaged=triaged)
        satisfied = rows_of(reported)
        assurance = next(item for item in satisfied if item["id"] == "A")
        assurance.pop("wait")
        assurance.update(state="satisfied", evidence=[{"attempt": "R1"}])
        done = accept_write(reported, write(reported, satisfied), ctx(admissions=admissions))
        moved = ctx(admissions=admissions, **MOVED, git_delta=lambda source, target: ["VERSION"])
        again = rows_of(done)
        next(item for item in again if item["id"] == "A")["reuse"] = {"from": "c1"}
        reused = accept_write(done, write(done, again), moved)
        self.assertEqual(label_qualification(reused, moved, "c2")["label"], "QUALIFIED")

    def test_a_scope_that_names_version_still_reopens(self):
        state = reusable(["VERSION"])
        rows, moved = move(state, ["VERSION"])
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                        write(state, rows), moved)
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["eligibility"], "touched")

    def test_metadata_that_affects_behavior_or_installation_still_reopens(self):
        state = reusable(["."])
        for changed in (["VERSION", "skills/pod/catalog.json"], ["install.sh", "VERSION"],
                        [".github/workflows/ci.yml"], ["skills/pod/VERSION"]):
            with self.subTest(changed=changed):
                rows, moved = move(state, changed)
                error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                                write(state, rows), moved)
                self.assertEqual(error.detail["referent"]["invalidations"][0]["eligibility"], "touched")


class RealSourceTests(unittest.TestCase):
    def test_a_proposal_derives_from_the_evidence_candidate_not_the_callers_reuse_from(self):
        state = reusable(["docs"])
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "S")["reuse"] = {"from": "c0"}
        moved = ctx(**MOVED, git_delta=lambda source, target: ["src/x.py"])
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                        write(state, rows), moved)
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["eligibility"], "eligible")
        self.assertIn("{from: c1}", entry["correction"])
        self.assertNotIn("c0", entry["correction"])
        self.assertEqual(entry["reuse_failure"], {"predicate": "reuse_from_is_evidence_candidate",
                                                  "given": "c0", "evidence_candidate": "c1"})
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "S")["reuse"] = {"from": "c1"}
        self.assertEqual(by_id(accept_write(state, write(state, rows), moved), "S")["reuse"]["from"], "c1")

    def test_a_wrong_source_refusal_names_the_predicate_and_a_valid_next_action(self):
        state = reusable(["docs"])
        row = by_id(state, "S")
        row["reuse"] = {"from": "c0"}
        moved = ctx(**MOVED, git_delta=lambda source, target: ["src/x.py"])
        self.assertEqual(evidence_source(row), "c1")
        with self.assertRaises(PodError) as caught:
            _bind_reuse(row, None, moved)
        referent = caught.exception.detail["referent"]
        self.assertEqual((referent["predicate"], referent["given"], referent["evidence_candidate"]),
                         ("reuse_from_is_evidence_candidate", "c0", "c1"))
        self.assertIn("reuse {from: c1}", caught.exception.detail["next_action"])
        self.assertNotIn("c0", caught.exception.detail["next_action"])

    def test_a_touched_scope_proposes_fresh_evidence_never_the_same_reuse(self):
        state = reusable(["src"])
        row = by_id(state, "S")
        row["reuse"] = {"from": "c1"}
        moved = ctx(**MOVED, git_delta=lambda source, target: ["src/x.py"])
        with self.assertRaises(PodError) as caught:
            _bind_reuse(row, None, moved)
        self.assertEqual(caught.exception.detail["referent"]["predicate"], "reuse_scope_untouched")
        action = caught.exception.detail["next_action"]
        self.assertIn("record fresh evidence for S", action)
        self.assertNotIn("set reuse", action)
        rows, moved = move(state, ["src/x.py"])
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                        write(state, rows), moved)
        correction = error.detail["referent"]["invalidations"][0]["correction"]
        self.assertTrue(correction.startswith("record fresh evidence for S"))
        self.assertEqual(error.detail["referent"]["invalidations"][0]["reuse_failure"],
                         {"predicate": "reuse_scope_untouched"})
        self.assertNotIn("add reuse", correction)

    def test_unreadable_or_unscoped_facts_stay_unknown(self):
        unreadable = reusable(["src"])
        rows, moved = move(unreadable, None)
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, unreadable,
                        write(unreadable, rows), moved)
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["eligibility"], "delta unreadable")
        self.assertNotIn("add reuse", entry["correction"])
        unscoped = accept_write(None, base.intake(ob("S", "satisfied", evidence=[
            proof("S", definition=ob("S"))])), ctx())
        rows, moved = move(unscoped, ["docs/x.md"])
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, unscoped,
                        write(unscoped, rows), moved)
        self.assertEqual(error.detail["referent"]["invalidations"][0]["eligibility"], "no declared scope")
        # Evidence without a recorded candidate has no source to derive anything from.
        self.assertIsNone(evidence_source({"evidence": [{"candidate": None}]}))
        self.assertIsNone(evidence_source({"evidence": [{"candidate": "c1"}, {"candidate": "c2"}]}))
        self.assertIsNone(evidence_source({"evidence": []}))


class ReviewProgressTests(unittest.TestCase):
    def test_repeated_review_without_progress_flags_a_diagnostic_and_blocks_nothing(self):
        state = base.assurance_state()
        admissions = {f"R{index}": base.settled_row(["A"], role="review", report={
            "outcome": "partial", "status": "validated_observation"}) for index in range(1, 7)}
        moved = ctx(admissions=admissions)
        flagged = accept_write(state, write(state, rows_of(state)), moved)
        self.assertIn("A", flagged["observations"]["churn"])
        line = next(item for item in status_projection(flagged, moved)["lines"] if "churn: A" in item)
        self.assertIn("diagnose", line)
        self.assertIn("blocks nothing", line)
        admitted = admit(flagged, base.packet(["A"], role="review"), moved, admission_id="R7")
        self.assertEqual(by_id(admitted, "A")["executor"], "R7")
        admission_refusal(flagged, base.packet(["A"], role="review"), moved, admission_id="R7")


class GoalScopedFindingTests(unittest.TestCase):
    CORRECTION = ob("K1", "unassigned", kind="correction", parent="A", boundary={"paths": ["src/p.py"]})
    FINDING = {"finding": "f1", "severity": "blocker", "triage": "required_correction", "summary": "off by one",
               "correction": "K1"}

    def settle(self, findings):
        return base.AssuranceTests().settle_review(findings, [self.CORRECTION])

    def test_an_unlinked_required_finding_refuses_with_a_valid_next_action(self):
        error = refused(self, "obligation_invalid", "finding_unlinked", self.settle, [self.FINDING])
        action = error.detail["next_action"]
        self.assertIn("blocks", action)
        self.assertIn("policy:safety", action)

    def test_a_linked_finding_retains_its_goal_and_safety_stays_required(self):
        for blocks in ("PoD#1", "policy:safety", "policy:privacy"):
            with self.subTest(blocks=blocks):
                state, _ = self.settle([{**self.FINDING, "blocks": blocks}])
                finding = by_id(state, "A")["findings"][0]
                self.assertEqual((finding["triage"], finding["blocks"]), ("required_correction", blocks))

    def test_an_advisory_carries_no_link_and_needs_its_downgrade_reason(self):
        advisory = {"finding": "f2", "severity": "major", "triage": "advisory", "summary": "rename"}
        refused(self, "obligation_invalid", "downgrade_unreasoned", self.settle, [advisory])
        refused(self, "obligation_invalid", "malformed", self.settle,
                [{**advisory, "reason": "cosmetic", "blocks": "PoD#1"}])
        state, _ = self.settle([{**advisory, "reason": "cosmetic"}])
        self.assertNotIn("blocks", by_id(state, "A")["findings"][0])


if __name__ == "__main__":
    unittest.main()

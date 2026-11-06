"""Offline Governor regressions; legacy two-ALLOW histories are explicitly synthetic."""
from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import (_read_journal, _record_path, _write_journal, decide, observe_candidate,
                          prepare_candidate, record_outcome, reconcile)
from pod.ledger import _governor_pending
from tests.kernel_support import KernelCase, action, authorization, git


class MergeSupersedenceTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        git(self.project, "checkout", "-qb", "candidate")
        (self.project / "README.md").write_text("candidate A\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "candidate A")
        self.prepare()
        self.first = self.admit()["record_id"]
        self.old_head = self.candidate
        self.journal = _record_path(self.project, "objective")

    def prepare(self):
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.write(self.stored())
        observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        self.request = action(kind="merge", candidate=self.candidate, target="origin/target", unit="delivery",
                              pr=7, effects=[], authorization=authorization(candidate=self.candidate,
                              tree=observed["tree"], scope=("merge",), target="origin/target"))

    def admit(self):
        return decide(self.project, "objective", owner="owner", action=self.request,
                      native_projection={"outstanding": []})

    def outcome(self, record, outcome, **extra):
        return record_outcome(self.project, "objective", owner="owner", record_id=record, outcome=outcome, **extra)

    def row(self, record):
        return next(row for row in _read_journal(self.journal)["actions"] if row["record_id"] == record)

    def refresh(self):
        git(self.project, "checkout", "-q", "target")
        (self.project / "target.txt").write_text("concurrent main change\n")
        git(self.project, "add", "target.txt")
        git(self.project, "commit", "-qm", "advance main")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        git(self.project, "checkout", "-q", "candidate")
        git(self.project, "merge", "--no-ff", "-qm", "refresh", "target")
        self.prepare()

    def legacy_pair(self, *, unknown=False):
        if unknown:
            self.outcome(self.first, "UNKNOWN", detail="earlier settlement was inconclusive")
        self.refresh()
        # Synthetic earlier-writer history: this was possible before the admission guard.
        journal = _read_journal(self.journal)
        original = deepcopy(journal["actions"].pop())
        _write_journal(self.journal, journal)
        self.second = self.admit()["record_id"]
        self.assertEqual(self.row(self.second)["outcome"], "pending")
        journal = _read_journal(self.journal)
        journal["actions"].insert(0, original)
        _write_journal(self.journal, journal)
        return original

    def land(self):
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge candidate", "candidate")
        self.result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.result)
        return self.result

    def assert_superseded(self):
        row = self.row(self.first)
        self.assertEqual(row["outcome"], "superseded")
        self.assertEqual(row["superseded_by"], {"record_id": self.second, "merge_commit": self.result})
        self.assertIsNone(row["receipt"]["provider"])
        self.assertFalse(_governor_pending(self.project, "objective"))

    def test_new_merge_defers_with_settlement_next_action_then_allows(self):
        self.refresh()
        held = self.admit()
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("prior_merge_unsettled", [reason["code"] for reason in held["reasons"]])
        self.assertIn("governor-outcome CANCELED", held["next_action"])
        self.outcome(self.first, "CANCELED", detail="never executed before refresh")
        self.assertEqual(self.admit()["decision"], "ALLOW")

    def test_merge_pass_settles_legacy_pending_and_closure_proceeds_without_provider(self):
        self.legacy_pair()
        self.land()
        with patch("pod.governor_effects._port", side_effect=AssertionError("no provider read")):
            result = self.outcome(self.second, "PASS", provider={"merge_commit": self.result})
        self.assertEqual(result["readback"][0]["status"], "superseded")
        self.assert_superseded()
        self.write(self.stored(), delivery={"record": self.second})
        rows = self.stored()
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[0].pop("executor", None)
        self.write(rows)
        closed = self.write(self.stored(), close=True)
        self.assertIsNotNone(closed["checkpoint"]["closure"])

    def test_legacy_unknown_settles_without_erasing_original_detail(self):
        original = self.legacy_pair(unknown=True)
        self.land()
        self.outcome(self.second, "PASS", provider={"merge_commit": self.result})
        self.assert_superseded()
        self.assertEqual(self.row(self.first)["receipt"]["detail"], original["receipt"]["detail"])

    def restore_legacy_pending(self):
        self.legacy_pair()
        original = deepcopy(self.row(self.first))
        self.land()
        self.outcome(self.second, "PASS", provider={"merge_commit": self.result})
        journal = _read_journal(self.journal)
        journal["actions"][0] = original
        _write_journal(self.journal, journal)

    def test_reconcile_uses_local_replacement_proof_and_repeats_without_effect(self):
        self.restore_legacy_pending()
        with patch("pod.governor_effects._port", side_effect=AssertionError("no provider read")):
            answer = reconcile(self.project, "objective", owner="owner", record_id=self.first)
            before = self.journal.read_bytes()
            repeated = reconcile(self.project, "objective", owner="owner", record_id=self.first)
        self.assertEqual(answer["status"], "superseded")
        self.assertEqual(repeated["status"], "superseded")
        self.assertEqual(before, self.journal.read_bytes())
        self.assert_superseded()

    def test_explicit_superseded_outcome_is_idempotent(self):
        self.restore_legacy_pending()
        answer = self.outcome(self.first, "superseded")
        before = self.journal.read_bytes()
        self.assertEqual(self.outcome(self.first, "superseded"), answer)
        self.assertEqual(before, self.journal.read_bytes())
        self.assert_superseded()

    def test_actual_earlier_head_merge_remains_pass(self):
        self.land()
        answer = self.outcome(self.first, "PASS", provider={"merge_commit": self.result})
        self.assertEqual(answer["outcome"], "PASS")
        self.assertNotIn("superseded_by", self.row(self.first))
        with self.assertRaises(PodError) as caught:
            self.outcome(self.first, "superseded")
        self.assertEqual(caught.exception.code, "outcome_conflict")

    def test_no_newer_verified_merge_cannot_be_superseded(self):
        self.refresh()
        before = self.journal.read_bytes()
        with self.assertRaises(PodError) as caught:
            self.outcome(self.first, "superseded")
        self.assertEqual(caught.exception.detail["detail"], "supersedence_unverified")
        self.assertEqual(before, self.journal.read_bytes())

    def assert_control_held(self, mutate):
        self.restore_legacy_pending()
        journal = _read_journal(self.journal)
        mutate(journal["actions"][0], journal["actions"][1])
        _write_journal(self.journal, journal)
        before = self.journal.read_bytes()
        with self.assertRaises(PodError):
            self.outcome(self.first, "superseded")
        self.assertEqual(before, self.journal.read_bytes())

    def test_other_pr_is_not_a_replacement(self):
        self.assert_control_held(lambda old, new: new["action"].update(pr=8))

    def test_recorded_execution_evidence_holds(self):
        self.assert_control_held(lambda old, new: old["receipt"].update(provider={"attempted": True}))

    def test_carried_row_keeps_its_readback_rule(self):
        self.assert_control_held(lambda old, new: old.update(carried_from={"objective": "predecessor"}))

    def test_unverified_result_holds(self):
        self.assert_control_held(lambda old, new: new["receipt"]["provider"].update(merge_commit=self.base))

    def test_earlier_merged_head_in_target_history_holds(self):
        self.legacy_pair()
        # A was actually merged before B; a same-PR assertion cannot erase the Git fact.
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "earlier merge A", self.old_head)
        prior_result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-q", "candidate")
        git(self.project, "merge", "--no-ff", "-qm", "refresh after earlier merge", "target")
        self.prepare()
        # Synthetic legacy replacement binding to the target that already contains A.
        journal = _read_journal(self.journal)
        newer = journal["actions"][1]
        bound = journal["units"]["delivery"]["candidate"]
        newer.update(commit=bound["commit"], candidate_id=bound["id"])
        newer["action"].update(candidate=bound["commit"], authorization=authorization(
            candidate=bound["commit"], tree=bound["tree"], scope=("merge",), target="origin/target"))
        newer["target_binding"]["commit"] = prior_result
        _write_journal(self.journal, journal)
        self.land()
        self.outcome(self.second, "PASS", provider={"merge_commit": self.result})
        self.assertEqual(self.row(self.first)["outcome"], "pending")
        with self.assertRaises(PodError):
            self.outcome(self.first, "superseded")

    def test_guidance_explains_refresh_and_recovery(self):
        from pathlib import Path
        guidance = (Path(__file__).resolve().parents[1] / "skills/pod/references/governor.md").read_text()
        for phrase in ("prior_merge_unsettled", "governor-outcome superseded", "without provider readback",
                       "superseded_by", "earlier head still settles PASS"):
            self.assertIn(phrase, guidance)

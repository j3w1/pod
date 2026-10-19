"""Synthetic R93 multi-delivery graphs through Governor admission and outcome operations.

Earlier-row controls remove the new binding explicitly; they are not released-writer or live evidence.
"""

from copy import deepcopy
import json
from unittest.mock import patch

from pod.errors import PodError
from pod.governance import verify_merge_result
from pod.governor import _empty_journal, _read_journal, _record_path, _write_journal, carry_into
from pod.internal import acknowledge, run as internal_run
from pod.ledger import objective_root, read, state_root
from tests.kernel_support import KernelCase, GovernorFakePort, action, authorization, git
from tests import test_relayed_authorization as relayed


class MergeParentTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        self.remote = GovernorFakePort()
        for target, value in (("pod.internal._governor_projection", {"outstanding": []}),
                              ("pod.orca.contract", {"status": "observed", "runtime": "runtime",
                                                     "capabilities": {"launch_preferences_v1": True}})):
            started = patch(target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        remote = patch("pod.governor_effects.GhPort", side_effect=lambda _project: self.remote)
        remote.start()
        self.addCleanup(remote.stop)

    def op(self, operation, **fields):
        request = {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}
        return acknowledge(operation, request, internal_run(operation, request))

    def checkpoint(self, **fields):
        return self.op("checkpoint", value=self.core(obligations=self.stored(), **fields))

    def prepare(self, unit, *, start=None, policy=False):
        git(self.project, "checkout", "-qb", unit, start or "refs/remotes/origin/target")
        path = self.project / ("AGENTS.md" if policy else "README.md")
        path.write_text(path.read_text() + unit + "\n")
        git(self.project, "add", path.name)
        git(self.project, "commit", "-qm", unit)
        self.candidate = git(self.project, "rev-parse", "HEAD")
        self.checkpoint()
        prepared = self.op("governor-prepare", unit=unit,
                           branch={"remote": "origin", "base": "target", "branch": unit})
        candidate = prepared["candidate"]
        self.request = action(kind="merge", candidate=candidate["commit"], target="origin/target", unit=unit,
                              effects=[], authorization=authorization(candidate=candidate["commit"],
                                  tree=candidate["tree"], scope=("merge",), target="origin/target"))
        return candidate

    def admit(self):
        decided = self.op("governor", action=self.request)
        self.assertEqual(decided["decision"], "ALLOW", decided)
        return decided["record_id"]

    def land(self, unit, method="merge"):
        git(self.project, "checkout", "-q", "target")
        if method == "merge":
            git(self.project, "merge", "--no-ff", "-qm", "merge " + unit, unit)
        elif method == "squash":
            git(self.project, "merge", "--squash", unit)
            git(self.project, "commit", "-qm", "squash " + unit)
        else:
            git(self.project, "merge", "--ff-only", unit)
        result = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        return result

    def outcome(self, record, result, **fields):
        return self.op("governor-outcome", record_id=record, outcome="PASS",
                       provider={"merge_commit": result}, **fields)

    def second_candidate(self, first_method="merge", *, policy=True):
        self.prepare("first", policy=policy)
        first = self.admit()
        delivered = self.land("first", first_method)
        self.outcome(first, delivered)
        self.checkpoint(delivery={"record": first})
        self.assertEqual(read(self.project, "objective")["checkpoint"]["governance"]["base"], self.base)
        candidate = self.prepare("second")
        self.first, self.delivered = first, delivered
        return candidate

    def row(self, record):
        return next(row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                    if row["record_id"] == record)

    def bytes(self):
        root = objective_root(self.project, "objective")
        return {name: (root / name).read_bytes() for name in ("context.json", "governor.json")}

    def refuses_unchanged(self, operation, **fields):
        before = self.bytes()
        with self.assertRaises(PodError) as caught:
            self.op(operation, **fields)
        self.assertEqual(self.bytes(), before)
        return caught.exception

    def two_deliveries(self, first_method):
        candidate = self.second_candidate(first_method)
        second = self.admit()
        result = self.land("second")
        self.assertEqual(git(self.project, "rev-list", "--parents", "-n", "1", result).split(),
                         [result, self.delivered, candidate["commit"]])
        # On the preceding implementation this real outcome operation refuses result_not_exact.
        settled = self.outcome(second, result)
        self.assertEqual(settled["outcome"], "PASS")
        row = self.row(second)
        self.assertEqual(row["governance_binding"]["base"], self.base)
        self.assertEqual(row["target_binding"]["commit"], self.delivered)
        self.assertNotIn("parent_basis", row)
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual((checkpoint["governance"]["base"], checkpoint["delivery"]["base"]),
                         (self.base, self.base))
        # One verified delivery per objective remains unchanged; M is no new currency exception.
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_changed")
        self.assertIn("direct user decision", str(refused))

    def test_second_merge_after_merge_delivery_uses_delivered_parent(self):
        self.two_deliveries("merge")

    def test_second_merge_after_squash_delivery_uses_delivered_parent(self):
        self.two_deliveries("squash")

    def test_second_merge_after_fast_forward_delivery_uses_delivered_parent(self):
        self.two_deliveries("fast-forward")

    def second_result(self, method):
        self.second_candidate()
        second = self.admit()
        result = self.land("second", method)
        self.assertEqual(self.outcome(second, result)["outcome"], "PASS")
        self.assertEqual(self.row(second)["target_binding"]["commit"], self.delivered)

    def test_second_squash_uses_observed_parent(self):
        self.second_result("squash")

    def test_second_fast_forward_uses_observed_ancestor(self):
        self.second_result("fast-forward")

    def test_third_admission_with_unchanged_sources_needs_a_plain_checkpoint(self):
        self.second_candidate(policy=False)
        second = self.admit()
        result = self.land("second")
        self.outcome(second, result)
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_changed")
        self.assertIn("plain checkpoint", str(refused))
        self.checkpoint()
        self.assertEqual(read(self.project, "objective")["checkpoint"]["governance"]["base"], result)
        self.prepare("third")
        third = self.admit()
        self.assertEqual(self.row(third)["target_binding"]["commit"], result)

    def synthetic_journal(self, change):
        """A named earlier-row or corrupt-binding control in a disposable objective only."""
        path = _record_path(self.project, "objective")
        journal = _read_journal(path)
        change(journal)
        _write_journal(path, journal)

    def synthetic_context(self, change):
        """A corrupt earlier-delivery control, never a supported state-editing recovery route."""
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_bytes())
        change(state["checkpoint"])
        path.write_text(json.dumps(state))

    def move_target(self, parent):
        moved = git(self.project, "commit-tree", parent + "^{tree}", "-p", parent, "-m", "other target work")
        git(self.project, "update-ref", "refs/remotes/origin/target", moved)
        return moved

    def result_on(self, parent, *, method="merge", tree=None):
        parents = ["-p", parent] + (["-p", self.candidate] if method == "merge" else [])
        result = git(self.project, "commit-tree", tree or self.candidate + "^{tree}", *parents, "-m", "result")
        git(self.project, "update-ref", "refs/remotes/origin/target", result)
        return result

    def pass_refuses(self, record, result, contains):
        refused = self.refuses_unchanged("governor-outcome", record_id=record, outcome="PASS",
                                          provider={"merge_commit": result})
        self.assertEqual(refused.code, "outcome_evidence_required")
        self.assertIn(contains, str(refused))
        return refused

    def test_stale_target_admission_refuses_without_any_write(self):
        self.prepare("first")
        self.move_target(self.base)
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_changed")
        self.assertIn("plain checkpoint", str(refused))

    def test_own_landed_merge_without_delivery_does_not_become_currency(self):
        self.prepare("first", policy=True)
        self.admit()
        self.land("first")
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_changed")
        self.assertIn("governance_refresh", str(refused))

    def test_unavailable_target_admission_refuses_without_any_write(self):
        self.prepare("first")
        git(self.project, "update-ref", "-d", "refs/remotes/origin/target")
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_unavailable")

    def test_unrelated_prepared_target_at_equal_commit_refuses_without_any_write(self):
        self.prepare("first")
        git(self.project, "update-ref", "refs/remotes/origin/other", self.base)
        self.synthetic_journal(lambda journal: journal["units"]["first"]["branch"].update(base="other"))
        self.request["target"] = self.request["authorization"]["target"] = "origin/other"
        refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual((refused.code, refused.detail["detail"]), ("delivery_unverified", "target_identity"))
        self.assertIn("governor-prepare", refused.detail["next_action"])

    def test_target_moved_during_admission_is_rechecked_before_writes(self):
        self.prepare("first")
        from pod.governor import _evaluate

        def move_after_evaluation(*args, **kwargs):
            result = _evaluate(*args, **kwargs)
            self.move_target(self.base)
            return result

        with patch("pod.governor._evaluate", side_effect=move_after_evaluation):
            refused = self.refuses_unchanged("governor", action=self.request)
        self.assertEqual(refused.code, "governance_changed")

    def test_new_row_wrong_parent_refuses_and_snapshot_cannot_settle_it(self):
        self.second_candidate()
        second = self.admit()
        moved = self.move_target(self.delivered)
        result = self.result_on(moved)
        refused = self.pass_refuses(second, result, "result_not_exact")
        self.assertIn("may have moved after admission or the merge rewrote commits", refused.detail["next_action"])
        self.assertIn("snapshot decision cannot settle this row", refused.detail["next_action"])
        self.assertIn("keep the row UNKNOWN", refused.detail["next_action"])
        for outcome in ("FAILED", "CANCELED"):
            refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome=outcome,
                                              detail="the merge landed")
            self.assertEqual(refused.detail["detail"], "target_contains_result")

    def test_new_row_invalid_target_commit_is_not_a_historical_row(self):
        self.second_candidate()
        second = self.admit()
        result = self.land("second")
        for commit in (None, "1234567", "z" * 40):
            with self.subTest(commit=commit):
                self.synthetic_journal(lambda journal: journal["actions"][-1]["target_binding"].update(commit=commit))
                self.pass_refuses(second, result, "target_identity")
                self.assertNotIn("parent_basis", self.row(second))

    def test_new_squash_on_a_later_parent_cannot_settle_the_admitted_row(self):
        self.second_candidate()
        second = self.admit()
        moved = self.move_target(self.delivered)
        result = self.result_on(moved, method="squash")
        self.pass_refuses(second, result, "result_not_exact")
        refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome="FAILED")
        self.assertEqual(refused.detail["detail"], "target_contains_result")

    def test_new_fast_forward_requires_its_admitted_commit_as_an_ancestor(self):
        self.second_candidate()
        second = self.admit()
        result = self.land("second", "fast-forward")
        unrelated = git(self.project, "commit-tree", self.base + "^{tree}", "-p", self.base,
                        "-m", "independent sibling")
        self.synthetic_journal(lambda journal: journal["actions"][-1]["target_binding"].update(commit=unrelated))
        self.pass_refuses(second, result, "result_not_exact")

    def test_changed_candidate_and_tree_still_refuse_without_any_write(self):
        self.second_candidate()
        second = self.admit()
        result = self.land("second")
        original = _read_journal(_record_path(self.project, "objective"))
        for key, value in (("commit", self.delivered), ("tree", git(self.project, "rev-parse", self.delivered + "^{tree}"))):
            with self.subTest(changed=key):
                _write_journal(_record_path(self.project, "objective"), deepcopy(original))
                self.synthetic_journal(lambda journal: journal["units"]["second"]["candidate"].update({key: value}))
                if key == "tree":
                    self.synthetic_journal(lambda journal: journal["actions"][-1]["action"]["authorization"].update(tree=value))
                self.pass_refuses(second, result, "candidate_mismatch")

    def test_changed_authorization_and_result_tree_still_refuse_without_any_write(self):
        self.second_candidate()
        second = self.admit()
        result = self.land("second")
        original = _read_journal(_record_path(self.project, "objective"))
        self.synthetic_journal(lambda journal: journal["actions"][-1]["action"]["authorization"].update(tree="a" * 40))
        self.pass_refuses(second, result, "authorization_invalid")
        _write_journal(_record_path(self.project, "objective"), original)
        wrong_tree = self.result_on(self.delivered, tree=self.delivered + "^{tree}")
        self.pass_refuses(second, wrong_tree, "tree_mismatch")

    def earlier(self, method="merge"):
        self.second_candidate()
        second = self.admit()
        result = self.land("second", method)
        # Synthetic 0.21.1 shape: its target ref was recorded, but its operational commit was not.
        self.synthetic_journal(lambda journal: journal["actions"][-1]["target_binding"].pop("commit"))
        return second, result

    def test_historical_pass_recovers_only_at_outcome_and_keeps_admitted_evidence(self):
        second, result = self.earlier()
        self.op("governor-outcome", record_id=second, outcome="UNKNOWN", evidence=["merge-parent-refusal"],
                provider={"merge_commit": result, "observation": "before recovery"},
                detail="the earlier parent was not observed at admission")
        before = self.row(second)
        journal = _read_journal(_record_path(self.project, "objective"))
        checkpoint = read(self.project, "objective")["checkpoint"]
        with self.assertRaises(PodError) as caught:
            verify_merge_result(self.project, {**before, "receipt": {**before["receipt"],
                                "provider": {"merge_commit": result}}}, journal["units"]["second"],
                                checkpoint["governance"])
        self.assertEqual(caught.exception.detail["detail"], "result_not_exact")
        refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome="PASS",
                                          provider={"merge_commit": result}, detail="recovered")
        self.assertEqual(refused.detail["detail"], "receipt_recorded")
        ack = self.outcome(second, result)
        basis = {"commit": self.delivered, "basis": "verified_delivery", "delivery_record": self.first,
                 "admitted_base": self.base, "observed_at_admission": False,
                 "prior": {"outcome": "UNKNOWN", "finished_at": before["receipt"]["finished_at"]}}
        after = self.row(second)
        self.assertEqual((ack["parent_basis"], after["parent_basis"]), (basis, basis))
        for key in set(before) - {"outcome", "receipt"}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["receipt"]["detail"], before["receipt"]["detail"])
        self.assertEqual(after["receipt"]["evidence"], before["receipt"]["evidence"])
        self.assertEqual(after["receipt"]["provider"], before["receipt"]["provider"])
        self.assertNotIn("commit", after["target_binding"])
        # A recovery is durable and idempotent, including its acknowledgement after a lost reply.
        stored = self.bytes()
        self.assertEqual(self.outcome(second, result)["parent_basis"], basis)
        self.assertEqual(self.bytes(), stored)
        self.assertEqual(verify_merge_result(self.project, after, journal["units"]["second"],
                                            checkpoint["governance"])["base"], self.base)

    def test_historical_squash_recovers_from_the_same_bounded_parent(self):
        second, result = self.earlier("squash")
        basis = self.outcome(second, result)["parent_basis"]
        # A pending row had no recorded uncertainty, so its marker records none.
        self.assertEqual((basis["commit"], basis["prior"]), (self.delivered, None))

    def test_historical_fast_forward_keeps_original_base_check_first(self):
        second, result = self.earlier("fast-forward")
        self.assertEqual(self.outcome(second, result)["outcome"], "PASS")
        self.assertNotIn("parent_basis", self.row(second))

    def test_historical_wrong_parent_and_failed_landed_merge_keep_refusal(self):
        second, _ = self.earlier()
        moved = self.move_target(self.delivered)
        result = self.result_on(moved)
        refused = self.pass_refuses(second, result, "result_not_exact")
        self.assertIn("cannot change this row's admitted base", refused.detail["next_action"])
        self.assertIn("keep the row UNKNOWN", refused.detail["next_action"])
        self.assertNotIn("obtain a direct user decision", refused.detail["next_action"])
        for outcome in ("FAILED", "CANCELED"):
            refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome=outcome,
                                              detail="a merge that landed")
            self.assertEqual(refused.detail["detail"], "target_contains_result")
        self.assertNotIn("parent_basis", self.row(second))

    def test_historical_delivery_settled_after_admission_cannot_recover(self):
        second, result = self.earlier()
        self.synthetic_journal(lambda journal: journal["actions"][0]["receipt"].update(finished_at="9999-01-01T00:00:00Z"))
        self.pass_refuses(second, result, "result_not_exact")

    def test_historical_delivery_settled_exactly_at_admission_can_recover(self):
        second, result = self.earlier()
        admitted = self.row(second)["at"]
        self.synthetic_journal(lambda journal: journal["actions"][0]["receipt"].update(finished_at=admitted))
        self.assertEqual(self.outcome(second, result)["parent_basis"]["commit"], self.delivered)

    def test_historical_delivery_without_a_zoned_settlement_cannot_recover(self):
        second, result = self.earlier()
        for timestamp in (None, "not a timestamp", "2026-09-21T00:00:00"):
            with self.subTest(timestamp=timestamp):
                self.synthetic_journal(lambda journal: journal["actions"][0]["receipt"].update(finished_at=timestamp))
                self.pass_refuses(second, result, "result_not_exact")

    def test_historical_different_delivery_base_or_target_cannot_recover(self):
        second, result = self.earlier()
        original = read(self.project, "objective")["checkpoint"]["delivery"]
        for changes in ({"base": self.delivered}, {"target_ref": "refs/remotes/origin/other"}):
            with self.subTest(changes=changes):
                self.synthetic_context(lambda checkpoint: checkpoint.update(delivery={**original, **changes}))
                self.pass_refuses(second, result, "result_not_exact")

    def test_historical_delivery_must_name_another_allowed_merge_pass_in_this_journal(self):
        second, result = self.earlier()
        original = _read_journal(_record_path(self.project, "objective"))
        for changes in ({"decision": "DEFER"}, {"outcome": "UNKNOWN"},
                        {"action": {**original["actions"][0]["action"], "kind": "release"}},
                        {"record_id": "unrelated-record"}):
            with self.subTest(changes=changes):
                _write_journal(_record_path(self.project, "objective"), deepcopy(original))
                self.synthetic_journal(lambda journal: journal["actions"][0].update(changes))
                self.pass_refuses(second, result, "result_not_exact")

    def test_historical_absent_or_self_referential_delivery_cannot_recover(self):
        second, result = self.earlier()
        delivery = read(self.project, "objective")["checkpoint"]["delivery"]
        for value in (None, {**delivery, "record": second}):
            with self.subTest(delivery=value):
                self.synthetic_context(lambda checkpoint: checkpoint.pop("delivery", None) if value is None
                                       else checkpoint.update(delivery=value))
                self.pass_refuses(second, result, "result_not_exact")

    def test_historical_recovery_keeps_authorization_and_result_tree_checks(self):
        second, result = self.earlier()
        original = _read_journal(_record_path(self.project, "objective"))
        self.synthetic_journal(lambda journal: journal["actions"][-1]["action"]["authorization"].update(tree="a" * 40))
        self.pass_refuses(second, result, "authorization_invalid")
        _write_journal(_record_path(self.project, "objective"), original)
        result = self.result_on(self.delivered, tree=self.delivered + "^{tree}")
        self.pass_refuses(second, result, "tree_mismatch")
        self.assertNotIn("parent_basis", self.row(second))

    def test_historical_recovery_never_overwrites_recorded_provider_evidence(self):
        second, result = self.earlier()
        self.op("governor-outcome", record_id=second, outcome="UNKNOWN",
                provider={"merge_commit": result, "observation": "before recovery"})
        refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome="PASS",
                                          provider={"merge_commit": result, "observation": "replacement"})
        self.assertEqual(refused.detail["detail"], "receipt_recorded")
        self.assertNotIn("parent_basis", self.row(second))

    def carried_copy(self, record, result):
        """Exercise the real claim-copy and readback paths; surrounding claim authority has its own suite."""
        journal = _read_journal(_record_path(self.project, "objective"))
        carried = _empty_journal()
        governance = read(self.project, "objective")["checkpoint"]["governance"]
        carry_into(carried, journal, [record], objective="predecessor", governance=governance,
                   at="2026-10-01T00:00:00Z")
        journal["actions"][-1] = carried["actions"][0]
        _write_journal(_record_path(self.project, "objective"), journal)
        self.remote.pr_history[("second", "target")] = [{"number": 7, "head_sha": self.candidate,
            "state": "MERGED", "merge_commit": result, "url": "https://example.invalid/pull/7"}]

    def test_carried_row_uses_its_copied_observed_parent_for_provider_readback(self):
        self.second_candidate()
        second = self.admit()
        result = self.land("second")
        self.carried_copy(second, result)
        # The successor's unit can now name a different candidate; copied readback identity still controls.
        self.synthetic_journal(lambda journal: journal["units"]["second"]["candidate"].update(commit=self.base))
        self.assertEqual(self.op("governor-outcome", record_id=second, outcome="PASS")["outcome"], "PASS")
        self.assertEqual(self.row(second)["target_binding"]["commit"], self.delivered)

    def test_historical_carried_row_never_uses_successor_delivery_recovery(self):
        second, result = self.earlier()
        self.carried_copy(second, result)
        refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome="PASS")
        self.assertEqual(refused.detail["detail"], "readback_disagrees")
        self.assertIn("result_not_exact", str(refused))
        self.assertNotIn("parent_basis", self.row(second))

    def test_caller_cannot_supply_a_parent_basis_or_replace_the_admitted_binding(self):
        second, result = self.earlier()
        for field in ("parent_basis", "target_binding"):
            with self.subTest(field=field):
                refused = self.refuses_unchanged("governor-outcome", record_id=second, outcome="PASS",
                    provider={"merge_commit": result}, **{field: {"commit": self.delivered}})
                self.assertEqual(refused.code, "invalid_request")

    def test_row_without_admitted_binding_keeps_the_snapshot_next_action(self):
        """A 0.13.3-shaped row is checked against the map's base, which an owner refresh does change."""
        second, _ = self.earlier()
        self.synthetic_journal(lambda journal: journal["actions"][-1].pop("governance_binding"))
        result = self.result_on(self.move_target(self.delivered))
        refused = self.pass_refuses(second, result, "result_not_exact")
        self.assertIn("obtain a direct user decision for the exact new target snapshot", refused.detail["next_action"])

    def test_a_malformed_recorded_prior_is_refused(self):
        second, result = self.earlier()
        self.outcome(second, result)
        journal = _read_journal(_record_path(self.project, "objective"))
        governance = read(self.project, "objective")["checkpoint"]["governance"]
        row = self.row(second)
        for prior in ({"outcome": "PASS", "finished_at": None}, {"outcome": "UNKNOWN"}, "UNKNOWN"):
            with self.subTest(prior=prior), self.assertRaises(PodError) as caught:
                verify_merge_result(self.project, {**row, "parent_basis": {**row["parent_basis"], "prior": prior}},
                                    journal["units"]["second"], governance)
            self.assertEqual(caught.exception.code, "state_unsupported")


class RelayedMergeAdmissionTests(relayed.RelayCase):
    def test_a_target_moving_during_admission_consumes_no_relayed_consent(self):
        from pod.governor import _evaluate
        base = read(self.project, "objective")["checkpoint"]["governance"]["base"]
        moved = git(self.project, "commit-tree", base + "^{tree}", "-p", base, "-m", "moved")
        uses = state_root(self.project) / ".relay-authorizations"
        receipts = lambda: sorted(path.name for path in uses.glob("*.json")) if uses.exists() else []
        before = receipts()

        def move_after_evaluation(*args, **kwargs):
            result = _evaluate(*args, **kwargs)
            git(self.project, "update-ref", "refs/remotes/origin/target", moved)
            return result

        with patch("pod.governor._evaluate", side_effect=move_after_evaluation):
            self.refuse("governance_changed", operation="governor", request=self.request("merge"))
        self.assertEqual(receipts(), before)
        git(self.project, "update-ref", "refs/remotes/origin/target", base)
        self.assertEqual(self.case.op("governor", action=self.request("merge"))["decision"], "ALLOW")
        self.assertEqual(self.journal()["actions"][-1]["target_binding"]["commit"], base)
        self.assertEqual(len(receipts()), len(before) + 1)

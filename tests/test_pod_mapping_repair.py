"""Ordinary checkpoint repair through real production boundaries and synthetic ports.

The earlier-writer case executes the reviewed preceding commit's complete package
in its own process. All bodies, native identities and projects here are synthetic.
"""

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from pod.errors import PodError
from pod.github import issue_intake
from pod.ledger import kernel_context, objective_root, read
from tests.pod_tick_support import TickCase


PRECEDING = "ca5913f639b23d8af8869174fcb8ba72187977cd"
REPOSITORY = Path(__file__).resolve().parents[1]


class RepairCase(TickCase):
    def core(self, **fields):
        fields.setdefault("criteria", getattr(self, "criteria", ["PoD 1: First result"]))
        return super().core(**fields)

    def bind_original(self, count=1, *, body=None, extra=(), amendments=None):
        self.criteria = [f"PoD {number}: Result {number}" for number in range(1, count + 1)]
        self.original = body if body is not None else "PES fixture\r\n" + "".join(
            f"- [ ] **PoD {number}: Result {number}.** Check unchanged.\r\n" for number in range(1, count + 1))
        self.issue.body = self.original
        source = issue_intake(self.project, self.locator, port=self.issue, amendments=amendments)["source"]
        rows = [self.criterion(source={"ref": self.criteria[0]})]
        rows += [self.criterion("waiting", id=f"O{number}", source={"ref": self.criteria[number - 1]},
                                wait={"class": "sequenced", "referent": "O1"}) for number in range(2, count + 1)]
        self.write([*rows, *extra], governance={"base_ref": "target"}, objective_source=source)
        self.issue.calls.clear()
        return source

    def canonical(self):
        for number in range(1, len(self.criteria) + 1):
            self.issue.body = self.issue.body.replace(f"**PoD {number}:", f"**PoD#{number}:")
        source = read(self.project, "objective")["checkpoint"]["objective_source"]
        return issue_intake(self.project, self.locator, port=self.issue,
                            amendments=[row["locator"] for row in source["amendments"]])["source"]

    def repair(self, *, targets=None, original=None, source=None, rows=None, **fields):
        checkpoint = read(self.project, "objective")["checkpoint"]
        source = source or self.canonical()
        return self.write(rows, seq=checkpoint["seq"] + 1, objective_source=source,
                          mapping_repair={"original_body": self.original if original is None else original,
                                          "targets": targets or {f"O{n}": f"PoD#{n}" for n in range(1, len(self.criteria) + 1)}},
                          **fields)

    def anchor(self, obligation="O1", task="work"):
        rows = self.stored()
        affected = [row for row in rows if row.get("wait") == {"class": "sequenced", "referent": obligation}]
        if affected:
            for row in affected:
                row["wait"] = {"class": "dependency", "referent": obligation}
            self.write(rows)
        frozen = self.packet([obligation], criteria=self.criteria)
        return self.start(task, frozen)["admission"]

    def held(self, detail, callback, *args, **fields):
        before, writes, starts = self.saved(), deepcopy(self.issue.writes), deepcopy(self.port.starts)
        error = self.failure("mapping_repair_refused", callback, *args, **fields)
        self.assertEqual(error.detail["detail"], detail, str(error))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.writes, writes)
        self.assertEqual(self.port.starts, starts)
        return error

    def synthetic_admission_damage(self, transform):
        """Adversarial stored-record control, never positive provenance evidence."""
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_text())
        transform(state)
        path.write_text(json.dumps(state))


def preceding_writer(destination):
    """Called only in the genuine preceding package's isolated subprocess."""
    @contextmanager
    def fixed_fixture():
        yield Path(destination)
    with patch("tests.issue41_support.fixture", fixed_fixture):
        case = RepairCase()
        case.setUp()
        case.bind_original(4)
        case.anchor("O2")
        case.write(objective_source=case.canonical())
        print(json.dumps({"writer": PRECEDING, "workers": case.port.workers,
                          "placement": case.port.placement, "criteria": case.criteria,
                          "original": case.original, "body": case.issue.body, "candidate": case.candidate}))
        case.doCleanups()


class MappingRepairTests(RepairCase):
    def test_four_open_original_rows_gain_only_kernel_association(self):
        self.bind_original(4)
        before = deepcopy(read(self.project, "objective"))
        result = self.repair()
        stored = read(self.project, "objective")
        self.assertEqual(result["pod_tick_intake"]["unmapped"], [])
        self.assertEqual([row["pod"] for row in self.ticks()["lines"]], [1, 2, 3, 4])
        for old, new in zip(before["checkpoint"]["obligations"], stored["checkpoint"]["obligations"]):
            self.assertEqual(old, {key: value for key, value in new.items() if key != "pod_mapping"})
            self.assertFalse(new["pod_mapping"]["invalidated"])
        for key in ("admissions", "interventions", "constraints", "source_rejections"):
            self.assertEqual(stored[key], before[key])
        self.assertFalse(any(row["proven"] or row["due"] for row in self.ticks()["lines"]))
        self.assertEqual(self.issue.writes, [])
        self.assertEqual(self.remote.calls, [])
        self.assertNotIn(self.original, json.dumps(stored))
        self.assertEqual(self.ack(result)["pod_ticks"], result["pod_ticks"])

    def test_retained_native_admission_supplies_original_after_late_canonical_checkpoint(self):
        self.bind_original()
        self.anchor()
        self.write(objective_source=self.canonical())
        before = deepcopy(read(self.project, "objective"))
        result = self.repair()
        self.assertEqual(result["pod_ticks"]["lines"][0]["criteria"][0]["id"], "O1")
        after = read(self.project, "objective")
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(after["checkpoint"]["native_refs"], before["checkpoint"]["native_refs"])

    def test_late_observation_without_retained_original_has_precise_hold(self):
        self.bind_original()
        self.write(objective_source=self.canonical())
        self.held("historical_binding_unavailable", self.repair)

    def test_fake_or_stale_body_cannot_supply_a_baseline(self):
        self.bind_original()
        self.held("body_not_label_only", self.repair, original=self.original.replace("unchanged", "invented"))

    def test_label_only_data_comparison_does_not_substitute_for_the_original_hash(self):
        self.bind_original()
        invented = self.original.replace("unchanged", "invented")
        self.issue.body = invented
        self.held("original_body_mismatch", self.repair, original=invented)

    def test_changed_checkbox_and_changed_meaning_are_not_repairs(self):
        self.bind_original()
        self.canonical()
        self.issue.body = self.issue.body.replace("[ ]", "[x]")
        self.held("body_not_label_only", self.repair)
        self.issue.body = self.issue.body.replace("[x]", "[ ]").replace("unchanged", "changed")
        self.held("body_not_label_only", self.repair)

    def test_changed_check_or_source_cannot_hide_in_repair(self):
        self.bind_original()
        rows = self.stored(); rows[0]["check"] += " another check"
        before = self.saved()
        self.failure("obligation_invalid", self.repair, rows=rows)
        self.assertEqual(self.saved(), before)
        rows = self.stored(); rows[0]["source"] = {"ref": "PoD#1"}
        self.held("repair_changes_obligation", self.repair, rows=rows)

    def test_current_source_must_be_real_and_exact(self):
        self.bind_original()
        source = self.canonical()
        self.issue.body += "outside edit"
        before = self.saved()
        self.failure("issue_reconciliation_required", self.repair, source=source)
        self.assertEqual(self.saved(), before)
        self.issue.body = self.issue.body.removesuffix("outside edit")
        with patch.object(self.issue, "issue", side_effect=PodError("issue_access_unavailable", "unavailable")):
            self.failure("issue_access_unavailable", self.repair, source=source)
        self.assertEqual(self.saved(), before)

    def test_wrong_number_alias_or_free_prose_is_not_a_target(self):
        self.bind_original()
        self.held("target_not_original_criterion", self.repair, targets={"O1": "PoD#2"})
        self.held("targets_invalid", self.repair, targets={"O1": "first"})

    def test_duplicate_original_or_current_line_refuses(self):
        self.bind_original(body="- [ ] **PoD 1: First\n- [ ] **PoD 1: Again\n")
        self.held("target_line_not_unique", self.repair)

    def test_decoys_and_unclosed_fences_are_not_repaired(self):
        self.bind_original(body="> - [ ] **PoD 1: Quote\n - [ ] **PoD 1: Indent\n~~~\n- [ ] **PoD 1: Fence\n")
        self.held("body_not_label_only", self.repair)

    def test_lf_and_crlf_fences_and_unrelated_bytes_are_preserved(self):
        self.bind_original(body="```md\r\n- [ ] **PoD 9: Decoy\r\n```\r\n- [X] **PoD 1: One\r\nü\n")
        self.issue.body = self.original.replace("**PoD 1:", "**PoD#1:")
        self.repair()
        self.assertEqual(self.line()["authorship"], "not_pod")
        self.assertFalse(self.line()["proven"])

    def test_request_and_stored_metadata_have_no_authority_input(self):
        self.bind_original()
        before = self.saved()
        self.failure("invalid_mapping repair", self.write, seq=2,
                     mapping_repair={"original_body": self.original, "targets": {"O1": "PoD#1"}, "approved": True})
        self.assertEqual(self.saved(), before)
        self.repair()
        rows = self.stored(); rows[0]["pod_mapping"]["invalidated"] = True
        self.held("association_kernel_field", self.write, rows)

    def test_stale_seq_and_replay_are_atomic(self):
        self.bind_original()
        self.repair()
        before = self.saved()
        self.failure("map_stale", self.write, seq=2,
                     mapping_repair={"original_body": self.original, "targets": {"O1": "PoD#1"}})
        self.assertEqual(self.saved(), before)
        self.held("already_repaired", self.repair)
        self.write(seq=3)
        self.assertEqual(self.stored()[0]["pod_mapping"]["accepted_seq"], 2)

    def test_sibling_failure_cannot_partially_annotate(self):
        self.bind_original(4)
        self.held("target_not_original_criterion", self.repair,
                  targets={"O1": "PoD#1", "O2": "PoD#2", "O3": "PoD#4", "O4": "PoD#3"})
        self.assertTrue(all("pod_mapping" not in row for row in self.stored()))

    def test_shared_coverage_intake_status_tick_untick_and_closure(self):
        from pod.obligations import _covered
        self.bind_original()
        self.repair()
        state = read(self.project, "objective")
        ctx = kernel_context(self.project, "objective", state, None)
        self.assertEqual(_covered({row["id"]: row for row in self.stored()}, ctx), {self.criteria[0], "PoD#1"})
        self.prove()
        before = self.saved()
        self.failure("pod_ticks_due", self.write, close=True)
        self.assertEqual(self.saved(), before)
        self.tick()
        self.assertEqual(self.line()["authorship"], "own")
        self.assertTrue(self.line()["proven"])
        self.lapse()
        self.assertEqual(self.line()["direction"], "untick")
        self.tick()
        self.assertFalse(self.line()["ticked"])
        self.prove(); self.tick(); self.write(close=True)
        self.assertEqual(self.ticks()["lines"][0]["status"], "current")
        self.assertEqual(self.stored()[0]["source"]["ref"], self.criteria[0])

    def test_unknown_receipts_and_failed_tick_history_are_preserved(self):
        second = self.criterion("waiting", id="O2", source={"ref": "PoD#2"},
                                wait={"class": "sequenced", "referent": "O1"})
        self.bind_original(body="- [ ] **PoD 1: First\n- [ ] **PoD#2: Existing\n", extra=(second,))
        self.criteria.append("PoD#2")
        rows = self.stored(); rows[0]["evidence"] = [self.proof("O1", status="NOT_RUN")]
        rows[1].pop("wait"); rows[1].update(state="satisfied", evidence=[self.proof("O2")])
        self.write(rows)
        self.issue.refuse_next = 403
        self.tick(2)
        before = read(self.project, "objective")
        self.repair(targets={"O1": "PoD#1"})
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][0]["receipts"], before["checkpoint"]["obligations"][0]["receipts"])
        self.assertEqual(after["pod_ticks"]["requests"], before["pod_ticks"]["requests"])
        self.assertFalse(self.line()["proven"])


class MappingProofTests(RepairCase):
    def amendment_original(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()

    def test_retained_pass_holds_even_when_caller_omits_selected_evidence(self):
        self.bind_original()
        self.prove()
        rows = self.stored(); rows[0].pop("evidence"); rows[0].pop("receipts")
        rows[0].update(state="active", executor="coordinator")
        self.held("proof_binding_unavailable", self.repair, rows=rows)

    def test_alpha_beta_pass_alpha_restored_never_inherits_beta_pass(self):
        self.bind_original()
        admission = self.anchor()
        self.issue.body = self.original.replace("unchanged", "Beta")
        self.rebind()
        self.settle(admission)
        self.prove(dispositions=[{"admission": admission["admission_id"], "discarded": True, "reason": "synthetic result"}])
        self.issue.body = self.original
        self.canonical()
        self.rebind()
        # Complete source history lacks a proof-time issue binding; restoration
        # never substitutes for it, even with a genuine original native anchor.
        self.held("proof_binding_unavailable", self.repair)

    def test_observed_scope_divergence_then_restoration_cannot_reactivate(self):
        self.bind_original(); self.repair(); self.prove(); self.tick()
        row = self.stored()[0]
        receipts = deepcopy(row["receipts"])
        canonical = self.issue.body
        self.issue.body = canonical.replace("unchanged", "Beta")
        self.failure("issue_reconciliation_required", self.tick)
        self.issue.body = canonical
        self.tick()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], receipts)
        self.held("mapping_invalidated", self.write)

    def test_accepted_scope_rebind_is_sticky_through_exact_restoration(self):
        self.bind_original(); self.repair()
        canonical = self.issue.body
        self.issue.body = canonical.replace("unchanged", "Beta")
        self.rebind()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.issue.body = canonical
        self.rebind()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.held("mapping_invalidated", self.prove)

    def test_new_pass_cannot_be_added_by_the_repair_write(self):
        self.bind_original()
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.held("repair_changes_obligation", self.repair, rows=rows)

    def test_prior_pass_cannot_escape_by_a_separate_unsatisfied_write(self):
        self.bind_original(); self.prove(); self.lapse()
        self.held("proof_binding_unavailable", self.repair)

    def test_unconfirmed_tick_never_establishes_own_authorship_or_keeps_proof(self):
        self.bind_original(); self.repair(); self.prove()
        receipts = deepcopy(self.stored()[0]["receipts"])
        self.issue.lose_next = "landed"
        self.tick()
        self.assertEqual(self.line()["authorship"], "ambiguous")
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], receipts)
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        self.held("mapping_binding_unavailable", self.write, close=True)

    def test_ambiguous_same_request_then_held_success_restores_current_mapping(self):
        from pod.github import MUTATION_TIMEOUT_S
        from pod.pod_ticks import INTENT_MARGIN_S
        self.bind_original(); self.repair(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments)
            self.issue._readback_pending = False
            with patch("pod.pod_ticks.time", return_value=1000 + MUTATION_TIMEOUT_S + INTENT_MARGIN_S):
                recovered = self.tick()
            self.assertEqual(recovered["receipt"]["authorship"], "ambiguous")
            self.assertFalse(self.line()["proven"])
            self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
            return held
        self.issue.issue_body_write = send
        receipts = deepcopy(self.stored()[0]["receipts"])
        with patch("pod.pod_ticks.time", return_value=1000):
            result = self.tick()
        self.assertEqual(result["receipt"]["authorship"], "own")
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], receipts)
        self.assertEqual(len(self.issue.writes), 1)

    def test_unknown_not_landed_keeps_existing_proof_and_due_tick(self):
        self.bind_original(); self.repair(); self.prove()
        self.issue.lose_next = "not_landed"
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.line()["direction"], "tick")
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        self.tick()
        self.assertEqual(self.line()["authorship"], "own")

    def test_amendment_change_retires_the_derived_association(self):
        self.bind_original(); self.repair()
        amendment = self.locator + "#issuecomment-3"
        source = issue_intake(self.project, self.locator, port=self.issue, amendments=[amendment])["source"]
        self.write(objective_source=source)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.rebind()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])

    def test_check_revision_and_restoration_retire_association(self):
        self.bind_original(); self.repair()
        original_check = self.stored()[0]["check"]
        authority = {"provenance": "user_direct", "instruction": "revise the check"}
        rows = self.stored(); rows[0]["check"] = "Revised check"
        self.write(rows, revision_authority=authority)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        rows = self.stored(); rows[0]["check"] = original_check
        self.write(rows, revision_authority=authority)
        self.held("mapping_invalidated", self.prove)

    def test_unavailable_mapping_observation_holds_until_real_read_without_inventing_scope(self):
        self.bind_original(); self.repair(); self.prove()
        def missing(state):
            state["pod_ticks"]["facts"].pop("mapping_scope_sha256")
        self.synthetic_admission_damage(missing)
        self.assertFalse(self.line()["proven"])
        self.held("mapping_binding_unavailable", self.write)
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])

    def test_observed_amendment_edit_before_send_stays_invalid_after_restoration(self):
        self.amendment_original()
        receipts = deepcopy(self.stored()[0]["receipts"])
        self.issue.amendments[3] = "Changed decision"
        self.failure("issue_reconciliation_required", self.tick)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertFalse(self.line()["proven"])
        self.issue.amendments[3] = "Decision"
        self.failure("unmapped", self.tick)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertEqual(self.stored()[0]["receipts"], receipts)
        self.assertEqual(self.issue.writes, [])

    def amendment_window(self, *, closed=False):
        self.amendment_original()
        receipts = deepcopy(self.stored()[0]["receipts"])
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments)
            self.issue.amendments[3] = "Changed decision"
            if closed:
                self.issue.state = "CLOSED"
            return held
        self.issue.issue_body_write = send
        error = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(error.detail["reason"], "closed_issue_requires_intent_reconciliation" if closed else "issue_amendment_changed")
        self.assertEqual(error.detail["receipt"]["authorship"], "own")
        self.assertTrue(error.detail["receipt"]["rebind"])
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], receipts)
        self.issue.issue_body_write = original
        self.issue.amendments[3], self.issue.state = "Decision", "OPEN"
        self.tick()
        self.assertFalse(self.line()["ticked"])
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])

    def test_observed_amendment_edit_during_send_stays_invalid_after_restoration(self):
        self.amendment_window()

    def test_closed_primary_refusal_does_not_hide_observed_amendment_divergence(self):
        self.amendment_window(closed=True)

    def test_closed_only_readback_preserves_current_proof_with_separate_hold(self):
        self.amendment_original()
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments)
            self.issue.state = "CLOSED"
            return held
        self.issue.issue_body_write = send
        error = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(error.detail["reason"], "closed_issue_requires_intent_reconciliation")
        self.assertEqual(error.detail["receipt"]["authorship"], "own")
        self.assertTrue(self.line()["proven"])
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertEqual(self.line()["status"], "current")
        self.assertEqual(self.line()["hold"]["reason"], "closed_issue_requires_intent_reconciliation")
        self.issue.issue_body_write, self.issue.state = original, "OPEN"
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_role_only_readback_keeps_unchanged_body_proof_and_authority_hold(self):
        self.bind_original(); self.repair(); self.prove()
        original = self.issue.issue_body_write
        def send(**arguments):
            held = original(**arguments)
            self.issue.title = "EEL: Evidence"
            return held
        self.issue.issue_body_write = send
        self.failure("issue_is_evidence_ledger", self.tick)
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.line()["authorship"], "own")
        self.assertEqual(self.line()["hold"]["code"], "issue_is_evidence_ledger")
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])

    def test_unavailable_amendment_readback_holds_currency_without_guessing_drift(self):
        self.amendment_original()
        original = self.issue.issue_body_write
        amendment_read = self.issue.amendment
        def unavailable(**query):
            raise PodError("issue_access_unavailable", "amendment unreadable")
        def send(**arguments):
            held = original(**arguments)
            self.issue.amendment = unavailable
            return held
        self.issue.issue_body_write = send
        error = self.failure("issue_access_unavailable", self.tick)
        self.assertTrue(error.detail["receipt"]["rebind"])
        self.assertEqual(error.detail["receipt"]["authorship"], "own")
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assertFalse(self.line()["proven"])
        self.held("mapping_binding_unavailable", self.write)
        self.issue.amendment, self.issue.issue_body_write = amendment_read, original
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])


class MappingCurrencyTests(RepairCase):
    def introducing_refresh(self, *, invalid_sibling=False):
        rows = self.stored()
        rows[0].pop("evidence", None)
        rows[0].update(state="active", executor="coordinator")
        rows.append(self.criterion("waiting", id="O2", provenance="user_direct",
                    source={"ref": "extra criterion", "instruction": "Add a fixture criterion"},
                    wait={"class": "dependency", "referent": "O1"},
                    **({"check": ""} if invalid_sibling else {})))
        return self.write(rows, criteria=[*self.criteria, "extra criterion"],
                          revision_authority={"provenance": "user_direct", "instruction": "Add a fixture criterion"})

    def unavailable_amendment(self, **arguments):
        raise PodError("issue_access_unavailable", "Required fixture amendment is unreadable")

    def assert_original_history(self, original):
        row = self.stored()[0]
        for key in ("id", "kind", "provenance", "source", "check", "introduced_seq", "definition", "receipts"):
            self.assertEqual(row[key], original[key], key)
        for key, value in original["pod_mapping"].items():
            if key != "invalidated":
                self.assertEqual(row["pod_mapping"][key], value, key)

    def test_introducing_refresh_drift_retires_mapping_before_exact_restoration(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        canonical = self.issue.body
        self.issue.body = canonical.replace("unchanged", "Different requirement")
        self.introducing_refresh()
        state = read(self.project, "objective")
        self.assertEqual(state["pod_ticks"]["read"], {"status": "not_read", "reason": "issue_body_changed"})
        self.assertEqual(state["pod_ticks"]["facts"], facts)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.issue.body = canonical
        self.failure("unmapped", self.tick)
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=original["evidence"])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.held("mapping_invalidated", self.write, rows, criteria=[*self.criteria, "extra criterion"])
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.issue.writes, [])

    def test_refresh_drift_with_invalid_sibling_refuses_atomically(self):
        self.bind_original(); self.repair(); self.prove()
        before = self.saved()
        canonical = self.issue.body
        self.issue.body = canonical.replace("unchanged", "Different requirement")
        self.failure("obligation_invalid", self.introducing_refresh, invalid_sibling=True)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.writes, [])
        self.issue.body = canonical
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])

    def test_retained_earlier_reader_drift_cannot_be_overwritten_by_restored_refresh(self):
        """Synthetic retained compact facts from the earlier pre-send reader."""
        from hashlib import sha256
        from pod.pod_ticks import _facts
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        changed = self.issue.body + "\nPreviously observed outside edit"
        def earlier_reader(state):
            source = {**state["checkpoint"]["objective_source"], "body_sha256": sha256(changed.encode()).hexdigest()}
            _facts(state["pod_ticks"], source, changed)
        self.synthetic_admission_damage(earlier_reader)
        self.introducing_refresh()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.assertEqual(self.issue.writes, [])

    def test_refresh_body_drift_survives_unavailable_amendment(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        self.issue.body += "\nChanged requirement"
        with patch.object(self.issue, "amendment", side_effect=self.unavailable_amendment):
            self.introducing_refresh()
        state = read(self.project, "objective")
        self.assertEqual(state["pod_ticks"]["read"]["reason"], "issue_body_changed")
        self.assertEqual(state["pod_ticks"]["facts"], facts)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)

    def test_refresh_observed_amendment_drift_accepts_exact_metadata_restatement(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.amendments[3] = "Changed fixture decision"
        self.introducing_refresh()
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.issue.writes, [])

    def test_refresh_partial_amendment_drift_survives_unavailable_sibling(self):
        self.bind_original(amendments=[self.locator + f"#issuecomment-{number}" for number in (3, 4)])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        amendment = self.issue.amendment
        def partial(**arguments):
            return amendment(**arguments) if arguments["comment"] == 3 else self.unavailable_amendment(**arguments)
        self.issue.amendments[3] = "Changed fixture decision"
        with patch.object(self.issue, "amendment", side_effect=partial):
            self.introducing_refresh()
        state = read(self.project, "objective")
        self.assertEqual(state["pod_ticks"]["read"]["reason"], "issue_amendment_changed")
        self.assertEqual(state["pod_ticks"]["facts"], facts)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)

    def test_refresh_unknown_amendment_recovers_only_after_complete_recheck(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        with patch.object(self.issue, "amendment", side_effect=self.unavailable_amendment):
            self.introducing_refresh()
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["facts"], facts)
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=original["evidence"])
        rows[1].pop("wait"); rows[1].update(state="active", executor="coordinator")
        self.held("mapping_binding_unavailable", self.write, rows, criteria=[*self.criteria, "extra criterion"])
        self.tick()
        self.write(rows, criteria=[*self.criteria, "extra criterion"])
        self.assertTrue(self.line()["proven"])
        self.assert_original_history(original)
        self.assertEqual(self.issue.writes, [])

    def test_unavailable_pre_send_amendment_holds_proof_and_closure_until_real_recheck(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        with patch.object(self.issue, "amendment", side_effect=self.unavailable_amendment):
            error = self.failure("issue_access_unavailable", self.tick)
            self.assertEqual(error.detail["blocked"]["row"], "B5")
            self.assertFalse(self.line()["proven"])
            self.assertEqual(read(self.project, "objective")["pod_ticks"]["facts"], facts)
            self.assertEqual(self.stored()[0], original)
            self.held("mapping_binding_unavailable", self.write, close=True)
        self.assertFalse(self.line()["proven"])  # Restored access alone is not an observation.
        self.assertEqual(self.issue.writes, [])
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.line()["authorship"], "own")
        self.assertEqual(self.stored()[0]["receipts"], original["receipts"])
        self.assertFalse(self.stored()[0]["pod_mapping"]["invalidated"])
        self.write(close=True)
        self.assertEqual(len(self.issue.writes), 1)

    def test_wholly_unavailable_pre_send_issue_is_a_recoverable_currency_hold(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.held("mapping_binding_unavailable", self.write, close=True)
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], original["receipts"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_pre_send_observed_body_drift_survives_unavailable_amendment(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        canonical = self.issue.body
        self.issue.body += "\nChanged requirement"
        with patch.object(self.issue, "amendment", side_effect=self.unavailable_amendment):
            self.failure("issue_access_unavailable", self.tick)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.issue.body = canonical
        self.failure("unmapped", self.tick)
        self.held("mapping_invalidated", self.write)
        self.assertEqual(self.issue.writes, [])

    def test_pre_send_partial_amendment_drift_survives_unavailable_sibling(self):
        self.bind_original(amendments=[self.locator + f"#issuecomment-{number}" for number in (3, 4)])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        amendment = self.issue.amendment
        def partial(**arguments):
            return amendment(**arguments) if arguments["comment"] == 3 else self.unavailable_amendment(**arguments)
        self.issue.amendments[3] = "Changed fixture decision"
        with patch.object(self.issue, "amendment", side_effect=partial):
            self.failure("issue_access_unavailable", self.tick)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.issue.writes, [])

    def test_foreign_amendment_response_holds_without_inventing_observed_drift(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.amendments[3] = {"id": 99, "url": self.locator + "#issuecomment-99", "body": "Foreign decision"}
        self.failure("issue_identity_mismatch", self.tick)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.issue.amendments.pop(3)
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_absent_requested_number_cannot_discard_observed_source_drift(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        facts = deepcopy(read(self.project, "objective")["pod_ticks"]["facts"])
        self.issue.body += "\nChanged requirement"
        self.failure("issue_reconciliation_required", self.tick, 99)
        ticks = read(self.project, "objective")["pod_ticks"]
        self.assertNotIn("99", ticks["requests"])
        self.assertEqual(ticks["facts"], facts)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.assertEqual(self.issue.writes, [])

    def test_absent_requested_number_cannot_discard_required_source_unavailability(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.unreadable = True
        self.failure("issue_access_unavailable", self.tick, 99)
        self.assertNotIn("99", read(self.project, "objective")["pod_ticks"]["requests"])
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.held("mapping_binding_unavailable", self.write, close=True)
        self.tick()
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.stored()[0]["receipts"], original["receipts"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_unavailable_readback_issue_holds_proof_and_recovers_without_resending(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        send = self.issue.issue_body_write
        def unreadable_after_send(**arguments):
            result = send(**arguments)
            self.issue.unreadable = True
            return result
        with patch.object(self.issue, "issue_body_write", side_effect=unreadable_after_send):
            self.failure("issue_access_unavailable", self.tick)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.held("mapping_binding_unavailable", self.write, close=True)
        result = self.tick()
        self.assertTrue(result["receipt"]["recovered"])
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.line()["authorship"], "own")
        self.assertEqual(self.stored()[0]["receipts"], original["receipts"])
        self.assertEqual(len(self.issue.writes), 1)

    def test_readback_partial_amendment_drift_survives_unavailable_sibling(self):
        self.bind_original(amendments=[self.locator + f"#issuecomment-{number}" for number in (3, 4)])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        send, amendment = self.issue.issue_body_write, self.issue.amendment
        def partial(**arguments):
            return amendment(**arguments) if arguments["comment"] == 3 else self.unavailable_amendment(**arguments)
        def changed_after_send(**arguments):
            result = send(**arguments)
            self.issue.amendments[3] = "Changed fixture decision"
            self.issue.amendment = partial
            return result
        with patch.object(self.issue, "issue_body_write", side_effect=changed_after_send):
            self.failure("issue_access_unavailable", self.tick)
        self.assertTrue(self.stored()[0]["pod_mapping"]["invalidated"])
        self.assert_original_history(original)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.line()["authorship"], "own")
        self.assertEqual(len(self.issue.writes), 1)

    def test_unavailable_source_holds_repaired_siblings_without_rewriting_history(self):
        self.bind_original(2, amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored())
        with patch.object(self.issue, "amendment", side_effect=self.unavailable_amendment):
            self.failure("issue_access_unavailable", self.tick)
        self.assertEqual(self.stored(), original)
        self.assertFalse(self.line(1)["proven"])
        self.assertFalse(self.line(2)["proven"])
        self.tick()
        self.assertTrue(self.line(1)["proven"])
        self.assertTrue(self.line(2)["proven"])
        self.assertEqual([row["receipts"] for row in self.stored()], [row["receipts"] for row in original])

    def test_pre_send_closed_only_hold_preserves_current_derived_proof(self):
        self.bind_original(amendments=[self.locator + "#issuecomment-3"])
        self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.state = "CLOSED"
        error = self.failure("issue_reconciliation_required", self.tick)
        self.assertEqual(error.detail["blocked"]["row"], "B3")
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.assertEqual(self.issue.writes, [])

    def test_pre_send_role_only_hold_preserves_currency_without_new_standing_observation(self):
        self.bind_original(); self.repair(); self.prove()
        original = deepcopy(self.stored()[0])
        self.issue.title = "EEL: Fixture evidence"
        with patch("pod.standing.observe", side_effect=AssertionError("new standing observation")):
            self.failure("issue_is_evidence_ledger", self.tick)
        self.assertTrue(self.line()["proven"])
        self.assertEqual(self.stored()[0], original)
        self.assertEqual(self.issue.writes, [])


class HistoricalBindingTests(RepairCase):
    def test_foreign_issue_and_amendments_do_not_match_original_identity(self):
        self.bind_original()
        self.canonical()
        foreign = issue_intake(self.project, "https://github.com/acme/example/issues/8", port=self.issue)["source"]
        self.held("historical_binding_conflict", self.repair, source=foreign)
        amended = issue_intake(self.project, self.locator, port=self.issue,
                               amendments=[self.locator + "#issuecomment-3"])["source"]
        self.held("historical_binding_conflict", self.repair, source=amended)

    def test_foreign_repository_never_becomes_the_current_port_target(self):
        self.bind_original()
        source = self.canonical()
        source.update(repository="other/project", locator="https://github.com/other/project/issues/7")
        before = self.saved()
        self.failure("repository_mismatch", self.repair, source=source)
        self.assertEqual(self.saved(), before)

    def test_native_identity_and_introduction_sequence_joins_are_required(self):
        self.bind_original(); self.anchor(); self.rebind_source = self.canonical()
        self.write(objective_source=self.rebind_source)
        def damage(state):
            admission = next(iter(state["admissions"].values()))
            admission["native_binding"]["taskId"] = "foreign-task"
        self.synthetic_admission_damage(damage)
        before = self.saved()
        self.failure("native_assignment_unverified", self.repair)
        self.assertEqual(self.saved(), before)

    def test_stale_admission_sequence_and_missing_criterion_membership_hold(self):
        self.bind_original(); self.anchor(); self.write(objective_source=self.canonical())
        def damage(state):
            admission = next(iter(state["admissions"].values()))
            admission["admitted_seq"] = state["checkpoint"]["seq"] + 1
        self.synthetic_admission_damage(damage)
        self.held("historical_binding_unavailable", self.repair)
        def missing(state):
            admission = next(iter(state["admissions"].values()))
            admission["admitted_seq"] = 2
            admission["recovery"]["checkpoint_binding"]["criteria"] = ["foreign criterion"]
        self.synthetic_admission_damage(missing)
        self.held("historical_binding_unavailable", self.repair)

    def test_conflicting_accepted_native_history_cannot_be_hash_selected(self):
        self.bind_original(2)
        self.anchor("O1", "first")
        self.issue.body = self.original.replace("unchanged", "Beta")
        self.rebind()
        self.anchor("O2", "second")
        self.issue.body = self.original
        self.write(objective_source=self.canonical())
        self.held("historical_binding_conflict", self.repair)

    def test_ambiguous_earliest_native_binding_holds(self):
        self.bind_original(2)
        self.anchor("O1", "first"); self.anchor("O2", "second")
        self.write(objective_source=self.canonical())
        def damage(state):
            first, second = state["admissions"].values()
            second["admitted_seq"] = first["admitted_seq"]
            second["recovery"]["checkpoint_binding"]["objective_source"]["body_sha256"] = "b" * 64
        self.synthetic_admission_damage(damage)
        self.held("historical_binding_ambiguous", self.repair)

    def test_admission_bound_to_different_definition_is_not_original_provenance(self):
        self.bind_original(); self.anchor(); self.write(objective_source=self.canonical())
        def damage(state):
            next(iter(state["admissions"].values()))["binding"]["definitions"]["O1"] = "f" * 64
        self.synthetic_admission_damage(damage)
        self.held("historical_definition_conflict", self.repair)

    def test_unserved_criterion_after_real_check_revision_has_no_original_definition_fact(self):
        self.bind_original(2)
        self.anchor("O1")
        rows = self.stored(); rows[1]["check"] = "Different authorized check"
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "revise O2 check"})
        self.write(objective_source=self.canonical())
        self.held("historical_binding_unavailable", self.repair, targets={"O2": "PoD#2"})

    def test_later_served_current_definition_cannot_hide_earliest_original_conflict(self):
        self.bind_original()
        first = self.anchor(task="first")
        self.settle(first)
        rows = self.stored(); rows[0].update(executor="coordinator", check="H2 revised check")
        self.write(rows, dispositions=[{"admission": first["admission_id"], "discarded": True, "reason": "old check"}],
                   revision_authority={"provenance": "user_direct", "instruction": "revise the existing check"})
        self.anchor(task="second")
        self.write(objective_source=self.canonical())
        self.held("historical_definition_conflict", self.repair)


class LiveTargetTests(RepairCase):
    def canonical_sibling(self):
        return self.criterion("waiting", id="O2", source={"ref": "PoD#1: Existing canonical"},
                              wait={"class": "sequenced", "referent": "O1"})

    def test_live_canonical_sibling_conflicts_with_derived_target(self):
        self.bind_original(extra=(self.canonical_sibling(),))
        self.criteria.append(self.canonical_sibling()["source"]["ref"])
        self.write()
        self.held("target_conflict", self.repair, targets={"O1": "PoD#1"})

    def test_withdrawn_canonical_sibling_does_not_duplicate_derived_target(self):
        self.bind_original(extra=(self.canonical_sibling(),))
        self.criteria.append(self.canonical_sibling()["source"]["ref"])
        rows = self.stored(); rows[1].pop("wait")
        rows[1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "superseded"})
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "withdraw the superseded criterion"})
        self.repair(targets={"O1": "PoD#1"})
        self.assertEqual([row["id"] for row in self.line()["criteria"]], ["O1"])

    def test_withdrawn_derived_target_keeps_own_untick_and_can_be_superseded(self):
        self.bind_original(); self.repair(); self.prove(); self.tick()
        self.lapse(withdraw=True)
        self.assertEqual(self.line()["criteria"], [])
        self.assertEqual(self.line()["direction"], "untick")
        self.tick()
        rows = self.stored()
        successor = self.canonical_sibling()
        successor.update(provenance="user_direct", state="active", executor="coordinator",
                         source={"ref": "PoD#1: Successor", "instruction": "replace the withdrawn criterion"})
        successor.pop("wait")
        self.write([*rows, successor], revision_authority={"provenance": "user_direct", "instruction": "replace the withdrawn criterion"})
        self.assertEqual([row["id"] for row in self.line()["criteria"]], ["O2"])

    def test_withdrawn_target_cannot_be_newly_annotated(self):
        self.bind_original(); self.prove(); self.lapse(withdraw=True)
        self.held("target_not_original_criterion", self.repair)


class RepairBoundaryTests(RepairCase):
    def test_canonical_only_core_restatement_is_refused_during_and_after_repair(self):
        self.bind_original()
        self.held("original_criteria_required", self.repair, criteria=["PoD#1"])
        self.repair()
        self.held("original_criteria_required", self.write, criteria=["PoD#1"])

    def test_supported_original_criteria_close_and_closing_reader_agree(self):
        from pod.obligations import validate_closed_snapshot
        self.bind_original(); self.repair(); self.prove(); self.tick()
        self.held("original_criteria_required", self.write, criteria=["PoD#1"], close=True)
        self.write(close=True)
        state = read(self.project, "objective")
        ctx = kernel_context(self.project, "objective", state, None)
        self.assertEqual(validate_closed_snapshot(state["checkpoint"], ctx), ())

    def test_mapping_only_read_cannot_activate_or_observe_standing_consent(self):
        self.bind_original(); self.anchor(); self.write(objective_source=self.canonical())
        before = deepcopy(read(self.project, "objective").get("standing_authorization"))
        with patch("pod.standing.observe", side_effect=AssertionError("mapping activated a consent observation")):
            self.repair()
        self.assertEqual(read(self.project, "objective").get("standing_authorization"), before)

    def test_bounded_utf8_body_and_target_count_refuse_before_effects(self):
        self.bind_original()
        self.held("original_body_invalid", self.repair, original="ü" * 262145)
        self.held("targets_invalid", self.repair, targets={f"C{n}": f"PoD#{n}" for n in range(65)})

    def test_caller_cannot_inject_association_at_intake(self):
        from pod.pod_mapping import scope_hash
        self.issue.body = "- [ ] **PoD#1: One\n"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        row = self.criterion(source={"ref": "PoD 1: Result"})
        row["pod_mapping"] = {"ref": "PoD#1", "original_source": source, "accepted_source": source,
                              "current_source": source, "accepted_seq": 1,
                              "scope_sha256": scope_hash(self.issue.body), "invalidated": False}
        self.held("association_kernel_field", self.write, [row], governance={"base_ref": "target"}, objective_source=source)

    def test_report_and_admission_map_cannot_invoke_repair(self):
        self.bind_original(extra=(self.sub("S"),))
        frozen = self.packet(["S"], criteria=self.criteria)
        request = {"seq": 2, "mapping_repair": {"original_body": self.original, "targets": {"O1": "PoD#1"}}}
        before = self.saved()
        self.failure("obligation_invalid", self.start, "first", frozen, accompanying=request)
        self.assertEqual(self.saved(), before)
        admission = self.start("first", frozen)["admission"]
        self.settle(admission)
        before = self.saved()
        self.held("checkpoint_only", self.report, admission, frozen, map={**request, "seq": 3})
        self.assertEqual(self.saved(), before)

    def test_identical_metadata_restatement_and_omission_carry_forward(self):
        self.bind_original(); self.repair()
        metadata = deepcopy(self.stored()[0]["pod_mapping"])
        self.write(self.stored())
        rows = self.stored(); rows[0].pop("pod_mapping")
        self.write(rows)
        self.assertEqual(self.stored()[0]["pod_mapping"], metadata)

    def test_old_metadata_cannot_clear_sticky_invalidation(self):
        self.bind_original(); self.repair()
        old = deepcopy(self.stored()[0]["pod_mapping"])
        canonical = self.issue.body
        self.issue.body = canonical.replace("unchanged", "Beta")
        self.rebind()
        rows = self.stored(); rows[0]["pod_mapping"] = old
        self.held("association_kernel_field", self.write, rows)


class PrecedingWriterTests(RepairCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.previous = tempfile.TemporaryDirectory(prefix="mapping-writer-", dir=os.environ.get("POD_CACHE_HOME"))
        cls.addClassCleanup(cls.previous.cleanup)
        cls.bundle = Path(cls.previous.name)
        archive = cls.bundle / "previous.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), PRECEDING, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def test_genuine_preceding_four_open_rows_repair_on_same_objective(self):
        destination = self.root / "earlier"; destination.mkdir()
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(REPOSITORY)))}
        result = subprocess.run([sys.executable, "-c",
            "from tests.test_pod_mapping_repair import preceding_writer; import sys; preceding_writer(sys.argv[1])",
            str(destination)], env=env, cwd=REPOSITORY, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual(facts["writer"], PRECEDING)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(destination / "state"),
                                          "XDG_CONFIG_HOME": str(destination / "config")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.root, self.project = destination, destination / "project"
        self.criteria, self.original, self.candidate = facts["criteria"], facts["original"], facts["candidate"]
        self.port.workers, self.port.placement = facts["workers"], facts["placement"]
        self.issue.body = facts["body"]
        before = deepcopy(read(self.project, "objective"))
        self.assertEqual([row["criteria"] for row in self.ticks()["lines"]], [[], [], [], []])
        self.repair()
        after = read(self.project, "objective")
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(after["checkpoint"]["criteria"], before["checkpoint"]["criteria"])
        self.assertEqual([row["criteria"][0]["id"] for row in self.ticks()["lines"]], ["O1", "O2", "O3", "O4"])
        self.assertTrue(all(not row["proven"] and not row["due"] for row in self.ticks()["lines"]))

"""Current-candidate review history relocates only after its exact assurance consumes it (R121)."""
from pod.history_tree import plain
from tests.history_support import publish_preceding
from tests.history_support import compact
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import CONTEXT_LIMIT, hydrate, is_reference, prepare_context
from tests.history_support import COMPACT_AT
from pod.util import encoded
from tests.history_support import size
from tests.test_history_reviews import OUTCOMES, correction_of, outcome, review_state


def current_reviews():
    state = review_state()
    current = state["checkpoint"]["candidate"]
    for row in state["admissions"].values():
        row["candidate"] = row["report"]["observation"]["candidate"] = current
        outcome(row, row["report"]["outcome"])
    for ob in state["checkpoint"]["obligations"]:
        for receipt in ob.get("receipts", []):
            if receipt["evidence"].get("attempt") in state["admissions"]:
                receipt["evidence"]["candidate"] = current
    state["source_rejections"]["synthetic-padding"] = ""
    state["source_rejections"]["synthetic-padding"] = "x" * (COMPACT_AT - size(state))
    return state


def served(state, key):
    return next(ob for ob in state["checkpoint"]["obligations"]
                if ob["id"] == state["admissions"][key]["serves"][0])


class CurrentReviewEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = current_reviews()

    def test_current_consumed_reviews_of_every_outcome_move_at_the_unchanged_trigger(self):
        self.assertEqual(size(self.state), COMPACT_AT)
        below = deepcopy(self.state)
        below["source_rejections"]["synthetic-padding"] = below["source_rejections"]["synthetic-padding"][:-1]
        self.assertEqual(compact(below), (below, {}))
        record, chunks = compact(self.state)
        self.assertTrue(all(is_reference(row) for row in record["admissions"].values()))
        self.assertEqual({row["report"]["outcome"] for row in self.state["admissions"].values()}, set(OUTCOMES))
        self.assertEqual(encoded(hydrate(record, chunks.__getitem__), compact=True), encoded(self.state, compact=True))

    def check_vetoes(self, cases, key="attempt-001"):
        for label, change in cases:
            with self.subTest(label=label):
                state = deepcopy(self.state)
                row = state["admissions"][key]
                change(state, row)
                record, chunks = compact(state, force=True)
                self.assertEqual(record["admissions"][key], row)
                self.assertTrue(is_reference(record["admissions"]["attempt-005"]))
                self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_current_reviews_need_their_single_served_consumed_receipt_for_every_outcome(self):
        for index, result in enumerate(OUTCOMES):
            key = f"attempt-{index:03}"
            def unconsumed(state, row):
                for receipt in served(state, key)["receipts"]:
                    if receipt["evidence"].get("attempt") == key:
                        receipt.pop("reported_seq", None)
                        receipt.pop("accepted_seq", None)
            def misplaced(state, row):
                source = served(state, key)
                receipts = [item for item in source["receipts"] if item["evidence"].get("attempt") == key]
                source["receipts"] = [item for item in source["receipts"] if item not in receipts]
                next(ob for ob in state["checkpoint"]["obligations"]
                     if ob["kind"] == "assurance" and ob["id"] != source["id"])["receipts"] += receipts
            self.check_vetoes([
                (f"{result}: no consumption sequence", unconsumed),
                (f"{result}: receipt in another assurance", misplaced),
                (f"{result}: several served rows", lambda state, row: row.update(serves=[row["serves"][0], "A6"])),
                (f"{result}: absent served row", lambda state, row: row.update(serves=["absent"])),
                (f"{result}: served row is not assurance", lambda state, row: served(state, key).update(kind="subgoal")),
            ], key=key)

    def test_unsettled_report_route_and_failure_vetoes_remain(self):
        self.check_vetoes([
            ("unreported", lambda state, row: row.update(report=None)),
            ("unrecognized report", lambda state, row: row["report"].update(status="unknown")),
            ("reconciliation report", lambda state, row: outcome(row, "partial", "reconciliation_required")),
            ("unrecognized outcome", lambda state, row: row["report"].update(outcome="UNKNOWN")),
            ("reserved", lambda state, row: row.update(state="reserved")),
            ("unresolved", lambda state, row: row.update(state="unresolved")),
            ("unknown candidate", lambda state, row: row.update(candidate=None)),
            ("active failure", lambda state, row: row.update(failures=[{"kind": "unavailable"}])),
            ("unknown route", lambda state, row: row["route_decision"].update(effective_unknown=True)),
            ("route hold", lambda state, row: row["route_decision"].update(route_mismatch=True)),
        ])

    def test_unsuccessful_current_reviews_keep_the_resolved_finding_rule(self):
        self.check_vetoes([
            ("open correction", lambda state, row: correction_of(state, row["admission_id"]).update(
                state="active", executor="coordinator", evidence=[])),
            ("unaccepted correction", lambda state, row: correction_of(state, row["admission_id"])["receipts"][0].pop("accepted_seq")),
            ("coordinator withdrawal", lambda state, row: correction_of(state, row["admission_id"]).update(
                state="withdrawn", evidence=[], withdrawal={"by": "coordinator", "reason": "synthetic"})),
            ("unreadable lineage", lambda state, row: state["checkpoint"]["obligations"].append({"id": "orphan", "renews": "absent"})),
        ])

    def test_current_implementation_and_investigation_remain_inline(self):
        self.check_vetoes([
            (role, lambda state, row, role=role: row.update(role=role,
                disposition={"kind": "discarded", "reason": "synthetic"} if role == "implement" else None))
            for role in ("implement", "investigate")
        ], key="attempt-000")

    def test_accepted_sequence_and_closed_consumed_reviews_can_move(self):
        state = deepcopy(self.state)
        key = "attempt-001"
        row = state["admissions"][key]
        row.update(state="closed", failures=[{"kind": "unavailable", "cleared_at": "synthetic-time"}])
        row["route_decision"].update(route_mismatch=True, mismatch_occurrence=1, settlements=[{
            "basis": "matching_report", "occurrence": 1, "utc": "2026-01-01T00:00:00Z",
            "report_digest": row["report"]["observation_digest"]}])
        for receipt in served(state, key)["receipts"]:
            if receipt["evidence"].get("attempt") == key:
                receipt["accepted_seq"] = receipt.pop("reported_seq")
        record, chunks = compact(state, force=True)
        self.assertTrue(is_reference(record["admissions"][key]))
        self.assertEqual(hydrate(record, chunks.__getitem__), state)
        row["route_decision"]["mismatch_occurrence"] = 2
        record, chunks = compact(state, force=True)
        self.assertEqual(record["admissions"][key], row)  # An earlier settlement cannot clear a new hold.
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_native_governor_and_protection_vetoes_remain(self):
        from pod.history import hydrate
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "context.json"
            for outcome in ("pending", "UNKNOWN", "FAILED"):
                journal = {"actions": [{"decision": "ALLOW", "outcome": outcome}]}
                record, chunks = prepare_context(path, self.state)
                self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
                self.assertEqual(record["schema"], "pod-history-record/v1")


PREVIOUS = "4830f22427029ff78c69b5fb4042da486b056534"
WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


class CurrentReviewProductionTests(unittest.TestCase):
    """Production entries on one candidate, including admission, reads, later writes and closed replay."""

    @classmethod
    def setUpClass(cls):
        from pod.assurance import binding_current, evidence_valid, validate_receipts
        from pod.governor import _read_journal, finalization
        from pod.history import (capacity, publish_context)
        from pod.internal import run
        from pod.ledger import (_read, _validate_context, closed_snapshot_context, map_of, objective_root, read)
        from pod.obligations import bound_governance_digest, report_projection, validate_closed_snapshot
        from pod.status import status
        from pod.util import atomic_json
        from tests.issue41_support import ProductionCase
        from tests import test_records_boundaries as renewal
        from tests.test_history_regressions import previous_history_bundle

        previous = tempfile.TemporaryDirectory(prefix="history-current-previous-")
        cls.addClassCleanup(previous.cleanup)
        cls.bundle = previous_history_bundle(previous.name, PREVIOUS)
        case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        path = cls.path = objective_root(case.project, "objective") / "context.json"

        def seq():
            return read(case.project, "objective")["checkpoint"]["seq"]

        def review(name, result="succeeded", correction=None):
            frozen = case.packet(["A"], role="review")
            admission = case.start(name, frozen)["admission"]
            case.settle(admission)
            update, triage = {"A": WAITING}, []
            if correction:
                update[correction] = {"kind": "correction", "provenance": "coordinator", "parent": "A",
                                      "check": "repair the finding", "proof_scope": ["src"], **WAITING}
                triage = [{"finding": "F1", "severity": "major", "triage": "required_correction", "blocks": "outcome",
                           "correction": correction, "summary": "repair required"}]
            case.report(admission, frozen, outcome=result, map={"seq": seq() + 1, "update": update}, triage=triage)
            return admission["admission_id"]

        # A resolved unsuccessful review has no served consumed receipt and must stay inline even now.
        cls.unbound = review("unbound-review", "failed", "K")
        case.write(None, seq=seq() + 1, update={"K": {"state": "satisfied", "evidence": [case.proof("K")]}})
        cls.settled = [review(f"current-review-{index}") for index in range(3)]
        unpadded = _read(path)
        cls.prior = deepcopy(unpadded)
        cls.prior["source_rejections"] = {"synthetic-padding": ""}
        shaped, _ = compact(cls.prior, protected_admissions=cls.prior["admissions"], force=True)
        cls.prior["source_rejections"]["synthetic-padding"] = "x" * (CONTEXT_LIMIT - 400 - size(shaped))
        shaped, chunks = compact(cls.prior, protected_admissions=cls.prior["admissions"], force=True)
        publish_preceding(path, shaped, chunks)
        cls.prior_bytes = path.stat().st_size
        cls.capacity = capacity(path, _read(path))
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        cls.detected_open = run("record-compaction", {"project": str(case.project), "objective": "objective"})
        cls.detection_unchanged = before == {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        frozen = case.packet(["A"], role="review")
        pending = case.start("next-current-review", frozen)["admission"]
        cls.pending = pending["admission_id"]
        cls.physical = json.loads(path.read_bytes())
        cls.proposed = _read(path)
        cls.physical_bytes = path.stat().st_size
        # The subsequent production report and checkpoint writes must hydrate the earlier reviews.
        case.settle(pending)
        case.report(pending, frozen, map={"seq": seq() + 1, "update": {"A": WAITING}})
        cls.reported = _read(path)
        cls.reported_physical = json.loads(path.read_bytes())
        atomic_json(path, {**cls.reported, "source_rejections": unpadded["source_rejections"]},
                    limit=CONTEXT_LIMIT, compact=True)
        case.write(case.stored())
        cls.restated = _read(path)
        rows = case.stored()
        for row in rows:
            if row["id"] == "A":
                row.pop("wait")
                row.update(state="satisfied", evidence=[{"attempt": cls.pending}])
            elif row["id"] == "O1":
                row.pop("executor")
                row.update(state="satisfied", evidence=[case.proof("O1")])
        case.write(rows)
        case.write(case.stored(), close=True)
        cls.closed_inline = json.loads(path.read_bytes())

        def facts():
            state = _read(path)
            mapped = map_of(state)
            context = closed_snapshot_context(case.project, "objective", state)
            gov = bound_governance_digest(mapped)
            assurance = next(row for row in mapped["obligations"] if row["id"] == "A")
            shown = status(case.project, None, objective="objective")
            shown.pop("record_capacity")
            conflicts = {}
            for label, field, value in (("candidate", "candidate", "0" * 40),
                                         ("definition", "definition", "0" * 64),
                                         ("binding", "binding", None)):
                corrupted = deepcopy(assurance)
                corrupted["receipts"][-1]["evidence"][field] = value
                try:
                    validate_receipts(corrupted, context, seq=mapped["seq"])
                except PodError as exc:
                    conflicts[label] = {"code": exc.code, "detail": exc.detail}
                else:
                    raise AssertionError(f"{label} receipt corruption was accepted")
            duplicate = deepcopy(assurance)
            duplicate["receipts"].append(deepcopy(duplicate["receipts"][-1]))
            try:
                validate_receipts(duplicate, context, seq=mapped["seq"])
            except PodError as exc:
                conflicts["duplicate"] = {"code": exc.code, "detail": exc.detail}
            else:
                raise AssertionError("duplicate receipt was accepted")
            return {"logical": state, "validation": _validate_context(deepcopy(state)),
                    "bindings": {key: binding_current(row["binding"], context)
                                 for key, row in state["admissions"].items()},
                    "stale_binding": binding_current(state["admissions"][cls.pending]["binding"],
                                                       {**context, "policy_revision": "different-policy"}),
                    "evidence_valid": evidence_valid(assurance, context, gov),
                    "outstanding_evidence": evidence_valid(assurance, {**context, "outstanding": [cls.pending]}, gov),
                    "stale_evidence": evidence_valid(assurance, context, gov, "0" * 40),
                    "receipts": validate_receipts(assurance, context, seq=mapped["seq"]),
                    "conflicts": conflicts, "replay": validate_closed_snapshot(mapped, context),
                    "report": report_projection(mapped, context), "closure": mapped["closure"],
                    "finalization": finalization(state, _read_journal(path.parent / "governor.json")),
                    "map": run("map", {"project": str(case.project), "objective": "objective"}), "status": shown}

        cls.before = facts()
        cls.detected_closed = run("record-compaction", {"project": str(case.project), "objective": "objective"})
        request = None
        cls.compacted = run("record-compaction", {"project": str(case.project), "objective": "objective", **({"request": request} if request is not None else {})})
        cls.closed_compacted = json.loads(path.read_bytes())
        cls.after = facts()
        before_replay = path.read_bytes()
        cls.replayed = run("record-compaction", {"project": str(case.project), "objective": "objective", **({"request": request} if request is not None else {})})
        cls.replay_unchanged = before_replay == path.read_bytes()

    def test_genuine_prechange_writer_refuses_the_next_admission_without_publication(self):
        from tests.test_history_regressions import previous_history_call
        self.assertGreater(size(self.proposed), CONTEXT_LIMIT)
        result = previous_history_call(self.bundle, self.proposed, operation="write", prior=self.prior)
        self.assertFalse(result["accepted"])
        self.assertEqual((result["version"], result["code"]), ("0.21.18", "record_too_large"))
        self.assertTrue(result["unchanged"])

    def test_near_limit_next_admission_moves_only_consumed_current_reviews(self):
        self.assertEqual(self.prior_bytes, CONTEXT_LIMIT - 400)
        self.assertLess(self.physical_bytes, CONTEXT_LIMIT)
        self.assertEqual(self.physical["schema"], "pod-history-record/v1")
        self.assertIsNone(self.proposed["admissions"][self.pending]["report"])
        for key, row in self.prior["admissions"].items():
            self.assertEqual(self.proposed["admissions"][key], row)
        self.assertEqual(self.proposed["admissions"][self.unbound]["report"]["outcome"], "failed")

    def test_capacity_and_read_only_detection_report_current_review_savings(self):
        self.assertFalse(self.capacity["warning"])
        self.assertTrue(self.capacity["conversion_pending"])
        self.assertTrue(self.detection_unchanged)
        self.assertEqual(self.detected_open["status"], "conversion_available")
        for result in (self.detected_closed, self.compacted, self.replayed):
            self.assertEqual((result["status"], result["written"]), ("unchanged", False))
        self.assertTrue(self.replay_unchanged)

    def test_subsequent_report_and_restatement_hydrate_current_review_history(self):
        self.assertEqual(self.reported_physical["schema"], "pod-history-record/v1")
        for key in self.settled:
            self.assertEqual(self.reported["admissions"][key], self.prior["admissions"][key])
            self.assertEqual(self.restated["admissions"][key], self.prior["admissions"][key])
        self.assertEqual(self.reported["admissions"][self.pending]["report"]["status"], "validated_observation")
        self.assertEqual(self.reported["checkpoint"]["obligations"], self.restated["checkpoint"]["obligations"])

    def test_closed_validation_bindings_evidence_receipt_conflicts_and_all_views_are_identical(self):
        self.assertEqual(self.closed_inline, self.closed_compacted)
        self.assertTrue(all(self.before["bindings"].values()))
        self.assertTrue(self.before["evidence_valid"])
        for key in ("stale_binding", "outstanding_evidence", "stale_evidence"):
            self.assertFalse(self.before[key])
        self.assertEqual(self.before["replay"], ())
        self.assertIsNotNone(self.before["closure"])
        self.assertTrue(all(value["detail"]["detail"] == "receipt_conflict" for value in self.before["conflicts"].values()))
        self.assertEqual(encoded(self.before, compact=True), encoded(self.after, compact=True))

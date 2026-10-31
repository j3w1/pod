"""Current-candidate review history relocates only after its exact assurance consumes it (R121)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import COMPACT_AT, CONTEXT_LIMIT, compact, hydrate, is_reference, prepare_context
from pod.util import encoded
from tests.history_support import size
from tests.test_history_reviews import OUTCOMES, correction_of, outcome, review_state
from tests.records_support import start_before_review_guard, report_before_goal_links


def current_reviews():
    state = review_state()
    state["checkpoint"]["history_policy"] = {"keep_failed_current_candidate_reviews": False,
                                               "source": "synthetic applicable current storage policy"}
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


# These retained incident controls explicitly select the preceding 512 KiB soft size.
from tests.history_support import configured_context_case
CONTEXT_LIMIT = 512 * 1024
COMPACT_AT = 5 * CONTEXT_LIMIT // 8
WARN_AT = CONTEXT_LIMIT // 2

@configured_context_case(CONTEXT_LIMIT)
class CurrentReviewEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = current_reviews()

    def test_current_consumed_reviews_of_every_outcome_move_before_byte_pressure(self):
        self.assertEqual(size(self.state), COMPACT_AT)
        below = deepcopy(self.state)
        below["source_rejections"]["synthetic-padding"] = below["source_rejections"]["synthetic-padding"][:-1]
        placed, parts = compact(below)
        self.assertTrue(parts)
        self.assertEqual(hydrate(placed, parts.__getitem__), below)
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
        key = "attempt-001"
        row = self.state["admissions"][key]
        native = {"runtime": row["runtime"], "assignments": [
            {"admission_id": k, "runtime": value["runtime"], "settled": k != key,
             **{field: value["native_binding"][source] for field, source in
                (("run_id", "runId"), ("task_id", "taskId"), ("dispatch_id", "dispatchId"), ("worker_id", "workerId"))}}
            for k, value in self.state["admissions"].items()]}
        journals = [{"actions": [{"decision": "ALLOW", "outcome": result,
                     "action": {"candidate": row["candidate"]}}]} for result in ("pending", "UNKNOWN", "FAILED")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            for options in ({"native": native, "journal": {"actions": []}},
                            {"protected": [key], "journal": {"actions": []}},
                            *({"journal": journal} for journal in journals),
                            {"journal": {"actions": [{"decision": "ALLOW", "outcome": "pending", "action": {}}]}}):
                with self.subTest(options=options):
                    record, chunks = prepare_context(path, self.state, **options)
                    self.assertEqual(record["admissions"][key], row)
                    self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
            with patch("pod.governor._read_journal", side_effect=PodError("invalid_record", "synthetic unreadable")):
                record, _ = prepare_context(path, self.state)
            self.assertEqual(record["admissions"], self.state["admissions"])


PREVIOUS = "4830f22427029ff78c69b5fb4042da486b056534"
WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


@configured_context_case(CONTEXT_LIMIT)
class CurrentReviewProductionTests(unittest.TestCase):
    """Production entries on one candidate, including admission, reads, later writes and closed replay."""

    @classmethod
    def setUpClass(cls):
        from pod.assurance import binding_current, evidence_valid, validate_receipts
        from pod.governor import _read_journal, finalization
        from pod.history import capacity, publish_context
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
            # Synthetic earlier admissions preserve the pre-allowance history shape.
            admission = start_before_review_guard(case, name, frozen)["admission"]
            case.settle(admission)
            update, triage = {"A": WAITING}, []
            if correction:
                update[correction] = {"kind": "correction", "provenance": "coordinator", "parent": "A",
                                      "check": "repair the finding", "proof_scope": ["src"], **WAITING}
                triage = [{"finding": "F1", "severity": "major", "triage": "required_correction",
                           "correction": correction, "summary": "repair required"}]
            report_before_goal_links(case, admission, frozen, outcome=result,
                                     map={"seq": seq() + 1, "update": update}, triage=triage)
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
        publish_context(path, shaped, chunks)
        cls.prior_bytes = path.stat().st_size
        cls.capacity = capacity(path, _read(path))
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        cls.detected_open = run("record-compaction", {"project": str(case.project), "objective": "objective"})
        cls.detection_unchanged = before == {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        frozen = case.packet(["A"], role="review")
        pending = start_before_review_guard(case, "next-current-review", frozen)["admission"]
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
        from tests.history_support import inline_settled_work
        cls.closed_inline = inline_settled_work(json.loads(path.read_bytes()), _read(path))
        atomic_json(path, cls.closed_inline, limit=CONTEXT_LIMIT, compact=True)

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
        request = {**cls.detected_closed["next_action"]["request"]["request"], "reason": "synthetic lossless compaction"}
        cls.compacted = run("record-compaction", {"project": str(case.project), "objective": "objective", "request": request})
        cls.closed_compacted = json.loads(path.read_bytes())
        cls.after = facts()
        before_replay = path.read_bytes()
        cls.replayed = run("record-compaction", {"project": str(case.project), "objective": "objective", "request": request})
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
        self.assertEqual({key for key, row in self.physical["admissions"].items() if is_reference(row)}, set(self.settled))
        for key in (self.unbound, self.pending):
            self.assertEqual(self.physical["admissions"][key], self.proposed["admissions"][key])
        self.assertIsNone(self.proposed["admissions"][self.pending]["report"])
        self.assertEqual({row["candidate"] for row in self.proposed["admissions"].values()},
                         {self.proposed["checkpoint"]["candidate"]})
        for key, row in self.prior["admissions"].items():
            self.assertEqual(self.proposed["admissions"][key], row)
        print("current_review_capacity=" + str({"prior_bytes": self.prior_bytes, "logical_bytes": size(self.proposed),
                                                "physical_bytes": self.physical_bytes, "limit_bytes": CONTEXT_LIMIT}))

    def test_capacity_and_read_only_detection_report_current_review_savings(self):
        self.assertTrue(self.capacity["warning"])
        self.assertGreater(self.capacity["reclaimable_bytes"], 0)
        self.assertTrue(self.detection_unchanged)
        for detected in (self.detected_open, self.detected_closed, self.compacted):
            self.assertGreater(detected["reclaimable_bytes"], 0)
            self.assertEqual(detected["reclaimable_bytes"], detected["current_bytes"] - detected["proposed_bytes"])
        self.assertEqual(self.detected_open["status"], "compaction_available")
        self.assertEqual((self.replayed["status"], self.replayed["written"], self.replayed["reclaimable_bytes"]),
                         ("replayed", False, 0))
        self.assertTrue(self.replay_unchanged)

    def test_subsequent_report_and_restatement_hydrate_current_review_history(self):
        self.assertEqual({key for key, row in self.reported_physical["admissions"].items() if is_reference(row)},
                         {*self.settled, self.pending})
        for key in self.settled:
            self.assertEqual(self.reported["admissions"][key], self.prior["admissions"][key])
            self.assertEqual(self.restated["admissions"][key], self.prior["admissions"][key])
        self.assertEqual(self.reported["admissions"][self.pending]["report"]["status"], "validated_observation")
        self.assertEqual(self.reported["checkpoint"]["obligations"], self.restated["checkpoint"]["obligations"])

    def test_closed_validation_bindings_evidence_receipt_conflicts_and_all_views_are_identical(self):
        self.assertFalse(any(is_reference(row) for row in self.closed_inline["admissions"].values()))
        self.assertEqual({key for key, row in self.closed_compacted["admissions"].items() if is_reference(row)},
                         {*self.settled, self.pending})
        self.assertTrue(all(self.before["bindings"].values()))
        self.assertTrue(self.before["evidence_valid"])
        for key in ("stale_binding", "outstanding_evidence", "stale_evidence"):
            self.assertFalse(self.before[key])
        self.assertEqual(self.before["replay"], ())
        self.assertIsNotNone(self.before["closure"])
        self.assertTrue(all(value["detail"]["detail"] == "receipt_conflict" for value in self.before["conflicts"].values()))
        self.assertEqual(encoded(self.before, compact=True), encoded(self.after, compact=True))

"""Settled superseded reviews and reconciliation reports relocate without changing meaning (R121)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import COMPACT_AT, CONTEXT_LIMIT, compact, hydrate, is_reference, prepare_context
from pod.util import digest, encoded
from tests.history_support import inline_copies, size, synthetic_state


OUTCOMES = ("succeeded", "partial", "failed", "blocked", "uncertain")


def outcome(row, value, status="validated_observation"):
    report = row["report"]
    report["status"] = status
    report["outcome"] = report["observation"]["outcome"] = value
    report["observation"]["failures"] = ["synthetic unsuccessful check"] if value != "succeeded" else []
    report["observation_digest"] = digest({"status": report["status"], "observation": report["observation"],
        "native_binding": {"runtime": row["runtime"], **{k: row["native_binding"][k]
                           for k in ("runId", "taskId", "dispatchId", "workerId")}}})


def satisfied(key, parent, seq=5):
    evidence = {"reference": f"synthetic-proof-{key}", "status": "PASS"}
    return {"id": key, "kind": "correction", "provenance": "coordinator", "parent": parent, "state": "satisfied",
            "check": f"repair {key}", "evidence": [evidence], "receipts": [{"evidence": evidence, "accepted_seq": seq}]}


def review_state():
    """Ten superseded reviews, two per outcome, each finding triaged and every correction resolved."""
    state = synthetic_state(count=10)
    checkpoint = state["checkpoint"]
    checkpoint["governance_history"] = {"candidates": checkpoint["governance_history"]["candidates"], "decisions": []}
    rows = {row["id"]: row for row in checkpoint["obligations"]}
    for index, row in enumerate(state["admissions"].values()):
        row["role"] = "review"
        outcome(row, OUTCOMES[index % 5])
        served = rows[row["serves"][0]]
        key = row["admission_id"]
        if row["report"]["outcome"] == "succeeded":
            served["receipts"].append({"reported_seq": 10, "evidence": {"attempt": key}})
            continue
        correction = f"K{index}"
        checkpoint["obligations"].append(satisfied(correction, served["id"]))
        served["findings"] += [
            {"attempt": key, "finding": "F1", "severity": "major", "triage": "required_correction",
             "summary": "repair required", "correction": correction, "recorded_seq": 4, "resolved_seq": 5},
            {"attempt": key, "finding": "F2", "severity": "minor", "triage": "advisory",
             "summary": "kept as a proposal", "proposal": f"adv-{index}", "recorded_seq": 4}]
    state["source_rejections"] = {"synthetic-padding": ""}
    state["source_rejections"]["synthetic-padding"] = "x" * (COMPACT_AT - size(state))
    return state


def correction_of(state, key):
    finding = next(item for ob in state["checkpoint"]["obligations"] for item in ob.get("findings", [])
                   if item["attempt"] == key and item["triage"] == "required_correction")
    return next(ob for ob in state["checkpoint"]["obligations"] if ob["id"] == finding["correction"])


class ReviewEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = review_state()

    def test_settled_reviews_of_every_outcome_move_at_trigger_exactly(self):
        self.assertEqual(size(self.state), COMPACT_AT)
        below = deepcopy(self.state)
        below["source_rejections"]["synthetic-padding"] = below["source_rejections"]["synthetic-padding"][:-1]
        self.assertEqual(compact(below), (below, {}))
        record, chunks = compact(self.state)
        self.assertTrue(all(is_reference(row) for row in record["admissions"].values()))
        restored = hydrate(record, chunks.__getitem__)
        self.assertEqual(encoded(restored, compact=True), encoded(self.state, compact=True))

    def check_vetoes(self, cases, key="attempt-001"):
        for label, change in cases:
            with self.subTest(label=label):
                state = deepcopy(self.state)
                row = state["admissions"][key]
                change(state, row)
                record, chunks = compact(state, trigger_bytes=0)
                self.assertEqual(record["admissions"][key], row)
                self.assertTrue(is_reference(record["admissions"]["attempt-002"]))
                self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_current_and_unresolved_reviews_stay_inline(self):
        def correction(**fields):
            return lambda state, row: correction_of(state, row["admission_id"]).update(fields)

        def withdrawn(by):
            return correction(state="withdrawn", withdrawal={"by": by, "reason": "synthetic"}, evidence=[])
        self.check_vetoes([
            ("current candidate", lambda state, row: row.update(candidate=state["checkpoint"]["candidate"])),
            ("open correction", correction(state="active", executor="coordinator", evidence=[])),
            ("unaccepted correction proof", lambda state, row: correction_of(state, row["admission_id"])["receipts"][0].pop("accepted_seq")),
            ("coordinator-withdrawn correction", withdrawn("coordinator")),
            ("missing correction", lambda state, row: next(
                item for ob in state["checkpoint"]["obligations"] for item in ob.get("findings", [])
                if item["attempt"] == row["admission_id"] and "correction" in item).update(correction="absent")),
            ("served assurance absent", lambda state, row: row.update(serves=["absent"])),
            ("served row is not an assurance", lambda state, row: next(
                ob for ob in state["checkpoint"]["obligations"] if ob["id"] == row["serves"][0]).update(kind="subgoal")),
            ("several served rows", lambda state, row: row.update(serves=[row["serves"][0], "A6"])),
        ])

    def test_unreadable_lineage_keeps_every_unsuccessful_review_inline(self):
        state = deepcopy(self.state)
        state["checkpoint"]["obligations"].append({"id": "orphan", "renews": "absent"})
        record, _ = compact(state, trigger_bytes=0)
        for key, row in state["admissions"].items():
            self.assertEqual(is_reference(record["admissions"][key]), row["report"]["outcome"] == "succeeded")

    def test_unsettled_reviews_stay_inline(self):
        self.check_vetoes([
            ("unreported", lambda state, row: row.update(report=None)),
            ("reserved", lambda state, row: row.update(state="reserved")),
            ("unresolved", lambda state, row: row.update(state="unresolved")),
            ("unknown candidate", lambda state, row: row.update(candidate=None)),
            ("review needing reconciliation", lambda state, row: outcome(row, "partial", "reconciliation_required")),
            ("invalid outcome", lambda state, row: row["report"].update(outcome="UNKNOWN")),
            ("active failure", lambda state, row: row.update(failures=[{"kind": "unavailable"}])),
            ("unknown route", lambda state, row: row["route_decision"].update(effective_unknown=True)),
            ("unsettled route mismatch", lambda state, row: row["route_decision"].update(route_mismatch=True)),
        ])

    def test_a_later_unresolved_finding_returns_a_moved_review_inline(self):
        # Reports carry no findings; coordinator triage records them, also on a later report replay.
        record, chunks = compact(self.state, trigger_bytes=0)
        self.assertTrue(is_reference(record["admissions"]["attempt-001"]))
        state = hydrate(record, chunks.__getitem__)
        served = next(ob for ob in state["checkpoint"]["obligations"]
                      if ob["id"] == state["admissions"]["attempt-001"]["serves"][0])
        state["checkpoint"]["obligations"].append({"id": "KL", "kind": "correction", "provenance": "coordinator",
            "parent": served["id"], "state": "waiting", "check": "repair a later finding", "evidence": [], "receipts": []})
        served["findings"].append({"attempt": "attempt-001", "finding": "F3", "severity": "major",
            "triage": "required_correction", "summary": "triaged later", "correction": "KL", "recorded_seq": 9})
        record, _ = compact(state, trigger_bytes=0)
        self.assertEqual(record["admissions"]["attempt-001"], state["admissions"]["attempt-001"])
        self.assertTrue(is_reference(record["admissions"]["attempt-002"]))

    def test_user_withdrawn_corrections_count_as_resolved(self):
        state = deepcopy(self.state)
        correction_of(state, "attempt-001").update(state="withdrawn", evidence=[],
            withdrawal={"by": "user_direct", "reason": "synthetic owner waiver"})
        for ob in state["checkpoint"]["obligations"]:
            for finding in ob.get("findings", []):
                if finding["attempt"] == "attempt-001":
                    finding.pop("resolved_seq", None)  # Owner withdrawal alone resolves it.
        record, chunks = compact(state, trigger_bytes=0)
        self.assertTrue(is_reference(record["admissions"]["attempt-001"]))
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_succeeded_reviews_keep_the_served_receipt_rule(self):
        state = deepcopy(self.state)
        served = next(ob for ob in state["checkpoint"]["obligations"]
                      if ob["id"] == state["admissions"]["attempt-000"]["serves"][0])
        served["receipts"] = [item for item in served["receipts"] if item["evidence"]["attempt"] != "attempt-000"]
        record, _ = compact(state, trigger_bytes=0)
        self.assertEqual(record["admissions"]["attempt-000"], state["admissions"]["attempt-000"])

    def test_native_governor_and_ambiguous_joins_preserve_vetoes(self):
        key = "attempt-002"
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
            for kwargs in [{"native": native, "journal": {"actions": []}},
                           {"protected": [key], "journal": {"actions": []}},
                           *({"journal": journal} for journal in journals)]:
                record, chunks = prepare_context(path, self.state, **kwargs)
                self.assertEqual(record["admissions"][key], row)
                self.assertTrue(is_reference(record["admissions"]["attempt-003"]))
                self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
            with patch("pod.governor._read_journal", side_effect=PodError("invalid_record", "synthetic unreadable")):
                record, _ = prepare_context(path, self.state)
            self.assertEqual(record["admissions"], self.state["admissions"])


class ReconciliationEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = synthetic_state(count=10)
        state["checkpoint"]["obligations"] = []
        for index, row in enumerate(state["admissions"].values()):
            row["role"] = "implement" if index < 5 else "investigate"
            row["disposition"] = {"kind": "integrated_into", "candidate": "f" * 40} if index < 5 else None
            outcome(row, OUTCOMES[index % 5], "reconciliation_required")
        cls.state = state

    def test_settled_reconciliation_reports_move_for_every_outcome(self):
        record, chunks = compact(self.state, trigger_bytes=0)
        self.assertTrue(all(is_reference(row) for row in record["admissions"].values()))
        self.assertEqual(hydrate(record, chunks.__getitem__), self.state)

    def test_unsettled_or_current_reconciliation_reports_stay_inline(self):
        cases = [("attempt-001", "undispositioned implementation", lambda state, row: row.update(disposition=None)),
                 ("attempt-001", "current implementation", lambda state, row: row.update(candidate=state["checkpoint"]["candidate"])),
                 ("attempt-006", "current investigation", lambda state, row: row.update(candidate=state["checkpoint"]["candidate"])),
                 ("attempt-006", "investigation with an active failure", lambda state, row: row.update(failures=[{"kind": "unavailable"}])),
                 ("attempt-006", "investigation with an open proposal", lambda state, row: state["checkpoint"]["proposals"].append(
                     {"id": "rep-synthetic", "source": "worker_report", "origin_ref": row["admission_id"][:64],
                      "summary": "synthetic follow-up", "status": "open"})),
                 ("attempt-006", "protected investigation", None)]
        for key, label, change in cases:
            with self.subTest(label=label):
                state = deepcopy(self.state)
                row = state["admissions"][key]
                if change:
                    change(state, row)
                record, _ = compact(state, trigger_bytes=0, protected_admissions=() if change else (key,))
                self.assertEqual(record["admissions"][key], row)
                self.assertTrue(is_reference(record["admissions"]["attempt-002"]))

    def test_adopted_proposals_leave_a_reconciled_investigation_movable(self):
        state = deepcopy(self.state)
        state["checkpoint"]["proposals"] = [{"id": "rep-synthetic", "source": "worker_report", "status": "adopted",
                                             "origin_ref": "attempt-006", "summary": "synthetic follow-up"}]
        record, _ = compact(state, trigger_bytes=0)
        self.assertTrue(is_reference(record["admissions"]["attempt-006"]))

    def test_validated_unsuccessful_investigations_still_stay_inline(self):
        state = deepcopy(self.state)
        for row in list(state["admissions"].values())[5:]:
            outcome(row, row["report"]["outcome"])
        record, _ = compact(state, trigger_bytes=0)
        for index, (key, row) in enumerate(state["admissions"].items()):
            self.assertEqual(is_reference(record["admissions"][key]), index < 5 or row["report"]["outcome"] == "succeeded")


PREVIOUS = "c6891428e3d65dc6b72806168c5125d657dc79a9"
WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


class LiveShapeTests(unittest.TestCase):
    """The incident's shape through production entries: superseded failed and partial reviews whose
    findings became resolved corrections, then a pending current-candidate report near the limit."""

    @classmethod
    def setUpClass(cls):
        from pod.governor import _read_journal
        from pod.history import capacity
        from pod.internal import run
        from pod.ledger import _read, objective_root, read
        from pod.history import publish_context
        from pod.util import atomic_json
        from tests.issue41_support import ProductionCase
        from tests.test_history_regressions import previous_history_bundle
        from tests import test_records_boundaries as renewal
        previous = tempfile.TemporaryDirectory(prefix="history-reviews-previous-")
        cls.addClassCleanup(previous.cleanup)
        cls.bundle = previous_history_bundle(previous.name, PREVIOUS)
        # Shared fixture work, built once; no test waits for time to pass.
        case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        path = cls.path = objective_root(case.project, "objective") / "context.json"

        def seq():
            return read(case.project, "objective")["checkpoint"]["seq"]

        def review(name, result, correction=None):
            frozen = case.packet(["A"], role="review")
            admission = case.start(name, frozen)["admission"]
            case.settle(admission)
            update, triage = {"A": WAITING}, []
            if correction:
                update[correction] = {"kind": "correction", "provenance": "coordinator", "parent": "A",
                                      "check": f"repair {correction}", "proof_scope": ["src"], **WAITING}
                triage = [{"finding": "F1", "severity": "major", "triage": "required_correction",
                           "correction": correction, "summary": "repair required"},
                          {"finding": "F2", "severity": "minor", "triage": "advisory", "summary": "noted"}]
            case.report(admission, frozen, outcome=result, map={"seq": seq() + 1, "update": update}, triage=triage)
            return admission["admission_id"]

        def resolve(*keys):
            case.write(None, seq=seq() + 1, update={key: {"state": "satisfied", "evidence": [case.proof(key)]}
                                                    for key in keys})

        def move():
            case.move()
            case.write(None, seq=seq() + 1, update={row["id"]: {"reuse": {"from": row["evidence"][0]["candidate"]}}
                                                    for row in case.stored()
                                                    if row["state"] == "satisfied" and row["kind"] == "correction"})

        cls.settled = []
        for index, result in enumerate(("failed", "partial", "blocked", "uncertain", "failed", "partial")):
            cls.settled.append(review(f"review-{index}", result, f"K{index}"))
            resolve(f"K{index}")
            move()
        cls.unresolved = review("review-open", "failed", "KU")
        move()
        cls.current = review("review-current", "partial", "KC")
        frozen = case.packet(["A"], role="review")
        pending = case.start("review-pending", frozen)["admission"]
        case.settle(pending)
        cls.pending = pending["admission_id"]
        unpadded = _read(path)
        cls.prior = deepcopy(unpadded)
        cls.prior["source_rejections"] = {"synthetic-padding": ""}
        # The preceding mechanism's physical shape: every review and receipt copy inline, other settled
        # history moved, 400 bytes below the limit (the pending report adds about a kilobyte).
        reviews = set(cls.prior["admissions"])
        shaped, _ = compact(cls.prior, protected_admissions=reviews, force=True)
        cls.prior["source_rejections"]["synthetic-padding"] = (
            "x" * (CONTEXT_LIMIT - 400 - size(inline_copies(shaped, cls.prior))))
        shaped, chunks = compact(cls.prior, protected_admissions=reviews, force=True)
        publish_context(path, inline_copies(shaped, cls.prior), chunks)
        cls.prior_bytes = path.stat().st_size
        cls.capacity = capacity(path, _read(path))
        case.report(pending, frozen, outcome="succeeded", map={"seq": seq() + 1, "update": {"A": WAITING}})
        cls.physical_bytes = path.stat().st_size
        cls.physical = json.loads(path.read_bytes())
        cls.proposed = _read(path)
        # Remove only the synthetic padding, then resolve, review and close through ordinary entries.
        atomic_json(path, {**cls.proposed, "source_rejections": unpadded["source_rejections"]},
                    limit=CONTEXT_LIMIT, compact=True)
        resolve("KU", "KC")
        final = review("review-final", "succeeded")
        rows = case.stored()
        for row in rows:
            if row["id"] == "A":
                row.pop("wait")
                row.update(state="satisfied", evidence=[{"attempt": final}])
            elif row["id"] == "O1":
                row.pop("executor")
                row.update(state="satisfied", evidence=[case.proof("O1")])
        case.write(rows)
        case.write(case.stored(), close=True)
        cls.closed_inline = json.loads(path.read_bytes())

        def facts():
            from pod.ledger import closed_snapshot_context, map_of
            from pod.obligations import report_projection, validate_closed_snapshot
            from pod.status import status
            state = _read(path)
            mapped = map_of(state)
            context = closed_snapshot_context(case.project, "objective", state)
            shown = status(case.project, None, objective="objective")
            shown.pop("record_capacity")  # Physical bytes differ by design; every other key must not.
            return {"replay": validate_closed_snapshot(mapped, context), "report": report_projection(mapped, context),
                    "closure": mapped["closure"], "finalization": finalization(state, _read_journal(path.parent / "governor.json")),
                    "map": run("map", {"project": str(case.project), "objective": "objective"}), "status": shown,
                    "logical": state}
        from pod.governor import finalization
        cls.before = facts()
        cls.detected = run("record-compaction", {"project": str(case.project), "objective": "objective"})
        request = {**cls.detected["next_action"]["request"]["request"], "reason": "synthetic lossless compaction"}
        cls.compacted = run("record-compaction", {"project": str(case.project), "objective": "objective",
                                                  "request": request})
        cls.closed_compacted = json.loads(path.read_bytes())
        cls.after = facts()
        cls.replayed = run("record-compaction", {"project": str(case.project), "objective": "objective",
                                                 "request": request})
        cls.replayed_bytes = path.read_bytes()

    def test_genuine_prechange_writer_refuses_the_pending_report_without_writing(self):
        from tests.test_history_regressions import previous_history_call
        self.assertGreater(size(self.proposed), CONTEXT_LIMIT)
        result = previous_history_call(self.bundle, self.proposed, operation="write", prior=self.prior)
        self.assertFalse(result["accepted"])
        self.assertEqual((result["version"], result["code"]), ("0.21.15", "record_too_large"))
        self.assertTrue(result["unchanged"])

    def test_pending_current_report_records_once_settled_reviews_move(self):
        self.assertTrue(CONTEXT_LIMIT - 1000 < self.prior_bytes < CONTEXT_LIMIT)
        self.assertTrue(self.capacity["warning"])
        self.assertGreater(self.capacity["reclaimable_bytes"], 0)
        self.assertLess(self.physical_bytes, CONTEXT_LIMIT)
        moved = {key for key, row in self.physical["admissions"].items() if is_reference(row)}
        self.assertEqual(moved, set(self.settled))
        for key in (self.unresolved, self.current, self.pending):
            self.assertEqual(self.physical["admissions"][key], self.proposed["admissions"][key])
        self.assertEqual({row["report"]["outcome"] for key, row in self.proposed["admissions"].items() if key in moved},
                         {"failed", "partial", "blocked", "uncertain"})
        self.assertEqual(self.proposed["admissions"][self.pending]["report"]["outcome"], "succeeded")
        for key, row in self.prior["admissions"].items():
            if key != self.pending:
                self.assertEqual(self.proposed["admissions"][key], row)
        print("review_capacity=" + str({"prior_bytes": self.prior_bytes, "proposed_bytes": size(self.proposed),
                                        "physical_bytes": self.physical_bytes, "limit_bytes": CONTEXT_LIMIT}))

    def test_closure_replay_and_status_are_identical_after_compaction(self):
        self.assertFalse(any(is_reference(row) for row in self.closed_inline["admissions"].values()))
        self.assertEqual(self.compacted["status"], "compacted")
        moved = {key for key, row in self.closed_compacted["admissions"].items() if is_reference(row)}
        self.assertEqual(moved, {*self.settled, self.unresolved})
        self.assertEqual(self.before["replay"], ())
        self.assertIsNotNone(self.before["closure"])
        self.assertEqual(encoded(self.before, compact=True), encoded(self.after, compact=True))

    def test_compaction_reports_the_bytes_it_reclaims(self):
        self.assertEqual(self.detected["status"], "compaction_available")
        self.assertEqual(self.detected["reclaimable_bytes"], self.detected["current_bytes"] - self.detected["proposed_bytes"])
        self.assertEqual(self.compacted["reclaimable_bytes"],
                         self.compacted["current_bytes"] - self.compacted["proposed_bytes"])
        self.assertEqual(self.compacted["proposed_bytes"], len(encoded(self.closed_compacted, compact=True)))
        self.assertEqual((self.replayed["status"], self.replayed["written"], self.replayed["reclaimable_bytes"]),
                         ("replayed", False, 0))
        self.assertEqual(self.replayed_bytes, encoded(self.closed_compacted, compact=True))


class RenewedAssuranceTests(unittest.TestCase):
    """An owner withdraws the correction after the assurance renewed, freezing its finding."""

    @classmethod
    def setUpClass(cls):
        from pod.ledger import read
        from tests.issue41_support import ProductionCase
        from tests import test_records_boundaries as renewal
        case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)

        def seq():
            return read(case.project, "objective")["checkpoint"]["seq"]
        frozen = case.packet(["A"], role="review")
        admission = case.start("review", frozen)["admission"]
        case.settle(admission)
        case.report(admission, frozen, outcome="failed", map={"seq": seq() + 1, "update": {"A": WAITING, "K": {
            "kind": "correction", "provenance": "coordinator", "parent": "A", "check": "repair the defect",
            "proof_scope": ["src"], **WAITING}}}, triage=[{"finding": "F1", "severity": "major",
            "triage": "required_correction", "correction": "K", "summary": "repair required"}])
        case.write(renew=[{"id": "A", "as": "A2"}])
        case.write(seq=seq() + 1, update={"K": {"state": "withdrawn", "withdrawal": {"by": "user_direct",
                   "reason": "owner waiver"}}}, revision_authority={"provenance": "user_direct", "instruction": "Waive K"})
        case.move()
        case.write(seq=seq() + 1)
        cls.key = admission["admission_id"]
        cls.state = read(case.project, "objective")

    def test_owner_withdrawal_after_renewal_releases_the_frozen_review(self):
        from pod.history import admission_movable
        frozen = next(row for row in self.state["checkpoint"]["obligations"] if row["id"] == "A")
        self.assertEqual(frozen["state"], "renewed")
        self.assertNotIn("resolved_seq", frozen["findings"][0])
        row = self.state["admissions"][self.key]
        self.assertNotEqual(row["candidate"], self.state["checkpoint"]["candidate"])
        self.assertTrue(admission_movable(self.state, self.key, row))
        record, chunks = compact(self.state, trigger_bytes=0)
        self.assertTrue(is_reference(record["admissions"][self.key]))
        self.assertEqual(hydrate(record, chunks.__getitem__), self.state)

    def test_a_coordinator_held_correction_still_pins_the_frozen_review(self):
        from pod.history import admission_movable
        state = deepcopy(self.state)
        correction = next(row for row in state["checkpoint"]["obligations"] if row["id"] == "K")
        correction.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        correction.pop("withdrawal")
        self.assertFalse(admission_movable(state, self.key, state["admissions"][self.key]))

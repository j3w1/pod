"""A satisfied row keeps one inline copy of its current proof; its receipt copy moves (R121)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.history import CONTEXT_LIMIT, compact, hydrate, is_reference, prepare_context
from pod.util import encoded
from tests.history_support import inline_copies, size, synthetic_state
from tests.records_support import start_before_review_guard, report_before_goal_links


OLD = f"{1:040x}"
REUSE = {"from": OLD, "to": "f" * 40, "delta": [f"src/module_{n}.py" for n in range(40)]}


def proof(name, candidate, status="PASS"):
    return {"reference": f"synthetic-log-{name}", "candidate": candidate, "status": status,
            "result": "synthetic check result; " * 20}


def satisfied_rows(state):
    """Satisfied rows whose receipts copy their selected or current proof, with and without REUSE."""
    current = state["checkpoint"]["candidate"]
    rows = []
    for key, candidate, reuse in (("K1", OLD, REUSE), ("K2", current, None), ("K3", OLD, None)):
        evidence = proof(key, candidate)
        receipt = {"evidence": deepcopy(evidence), "accepted_seq": 5}
        row = {"id": key, "kind": "correction", "provenance": "coordinator", "parent": "A1", "state": "satisfied",
               "check": f"repair {key}", "evidence": [evidence], "receipts": [receipt]}
        if reuse:
            row["reuse"] = deepcopy(reuse)
            receipt["reuse"] = deepcopy(reuse)
        rows.append(row)
    # A current-candidate PASS receipt that the row no longer selects is a copy of current proof too.
    rows[2]["receipts"].insert(0, {"evidence": proof("K3-current", current), "accepted_seq": 4})
    assurance = state["checkpoint"]["obligations"][0]
    selected = deepcopy(assurance["receipts"][0])
    assurance.update(state="satisfied", evidence=[deepcopy(selected["evidence"])], reuse=deepcopy(REUSE))
    assurance.pop("executor")
    assurance["receipts"][0]["reuse"] = deepcopy(REUSE)
    state["checkpoint"]["obligations"] += rows
    return state


# These retained incident controls explicitly select the preceding 512 KiB soft size.
from tests.history_support import configured_context_case
CONTEXT_LIMIT = 512 * 1024
COMPACT_AT = 5 * CONTEXT_LIMIT // 8
WARN_AT = CONTEXT_LIMIT // 2

@configured_context_case(CONTEXT_LIMIT)
class ReceiptCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = satisfied_rows(synthetic_state(count=14))
        cls.rows = {row["id"]: index for index, row in enumerate(cls.state["checkpoint"]["obligations"])}

    def receipts(self, record, key):
        return record["checkpoint"]["obligations"][self.rows[key]]["receipts"]

    def test_receipt_copies_move_while_rows_keep_their_evidence_and_reuse(self):
        record, chunks = compact(self.state, trigger_bytes=0)
        for key in ("K1", "K2", "K3"):
            self.assertTrue(all(is_reference(item) for item in self.receipts(record, key)), key)
        self.assertTrue(is_reference(self.receipts(record, "A0")[0]))
        for key in ("A0", "K1", "K2", "K3"):
            logical = self.state["checkpoint"]["obligations"][self.rows[key]]
            physical = record["checkpoint"]["obligations"][self.rows[key]]
            self.assertEqual(physical["evidence"], logical["evidence"])
            self.assertEqual(physical.get("reuse"), logical.get("reuse"))
        self.assertEqual(encoded(hydrate(record, chunks.__getitem__), compact=True), encoded(self.state, compact=True))

    def check_inline(self, cases, key, index=0, **options):
        for label, change in cases:
            with self.subTest(label=label):
                state = deepcopy(self.state)
                change(state["checkpoint"]["obligations"][self.rows[key]], state)
                record, chunks = compact(state, trigger_bytes=0, **options)
                self.assertEqual(self.receipts(record, key)[index],
                                 state["checkpoint"]["obligations"][self.rows[key]]["receipts"][index])
                self.assertTrue(is_reference(self.receipts(record, "K3")[1]))
                self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_failed_unknown_unaccepted_and_unsatisfied_copies_stay_inline(self):
        def status(value):
            def change(row, state):
                row["evidence"][0]["status"] = row["receipts"][0]["evidence"]["status"] = value
            return change

        def unsatisfied(value):
            return lambda row, state: row.update(state=value)
        self.check_inline([("failed test", status("FAILED")), ("unknown test", status("UNKNOWN")),
                           ("pending test", status("pending")),
                           ("unaccepted", lambda row, state: row["receipts"][0].pop("accepted_seq")),
                           ("no candidate", lambda row, state: [item.pop("candidate") for item in
                                                                (row["evidence"][0], row["receipts"][0]["evidence"])]),
                           *((f"{value} row", unsatisfied(value)) for value in ("active", "waiting", "unassigned"))],
                          "K1")
        self.check_inline([("current proof of a waiting row", unsatisfied("waiting"))], "K2")

    def test_assurance_copies_keep_their_review_and_reuse_vetoes(self):
        attempt = self.state["checkpoint"]["obligations"][0]["receipts"][0]["evidence"]["attempt"]
        self.check_inline([
            ("unconsumed review", lambda row, state: row["receipts"][0].pop("reported_seq")),
            ("unknown review", lambda row, state: [item.update(attempt="absent") for item in
                                                   (row["evidence"][0], row["receipts"][0]["evidence"])]),
            ("different reuse", lambda row, state: row["receipts"][0].update(reuse={**REUSE, "to": "other"})),
            ("unselected reused review", lambda row, state: row.update(evidence=[])),
        ], "A0")
        self.check_inline([("protected review", lambda row, state: None)], "A0", protected_admissions=(attempt,))

    def test_protected_receipts_and_candidates_stay_inline(self):
        for options in ({"protect_receipts": True}, {"protected_candidates": [OLD, "f" * 40]}):
            with self.subTest(options=options):
                record, _ = compact(self.state, trigger_bytes=0, **options)
                for key, index in (("A0", 0), ("K1", 0), ("K2", 0), ("K3", 0), ("K3", 1)):
                    self.assertFalse(is_reference(self.receipts(record, key)[index]), key)

    def test_governor_joins_native_outstanding_and_unreadable_joins_keep_copies_inline(self):
        state = self.state
        assurance = state["checkpoint"]["obligations"][0]
        attempt = assurance["receipts"][0]["evidence"]["attempt"]
        row = state["admissions"][attempt]
        native = {"runtime": row["runtime"], "assignments": [
            {"admission_id": key, "runtime": value["runtime"], "settled": key != attempt,
             **{field: value["native_binding"][source] for field, source in
                (("run_id", "runId"), ("task_id", "taskId"), ("dispatch_id", "dispatchId"), ("worker_id", "workerId"))}}
            for key, value in state["admissions"].items()]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            for label, kwargs, held in [
                    ("native outstanding review", {"native": native, "journal": {"actions": []}}, ["A0"]),
                    *((f"{result} Governor candidate",
                       {"journal": {"actions": [{"decision": "ALLOW", "outcome": result, "action": {"candidate": OLD}}]}},
                       ["K1", "K3"]) for result in ("pending", "UNKNOWN", "FAILED"))]:
                with self.subTest(label=label):
                    record, chunks = prepare_context(path, state, force=True, **kwargs)
                    for key in held:  # Each row's selected copy is its last receipt; A0's is its first.
                        index = 0 if key == "A0" else -1
                        self.assertEqual(self.receipts(record, key)[index],
                                         state["checkpoint"]["obligations"][self.rows[key]]["receipts"][index])
                    self.assertTrue(is_reference(self.receipts(record, "K2")[0]))
                    self.assertEqual(hydrate(record, chunks.__getitem__), state)
            with patch("pod.governor._read_journal", side_effect=PodError("invalid_record", "synthetic unreadable")):
                record, _ = prepare_context(path, state, force=True)
            self.assertEqual(record["checkpoint"]["obligations"], state["checkpoint"]["obligations"])


PREVIOUS = "438dde67737a68c6f1f175f11ed9c2d00c857e71"
WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


def copies(physical):
    return {(row["id"], index) for row in physical["checkpoint"]["obligations"] if not is_reference(row)
            for index, receipt in enumerate(row.get("receipts", [])) if is_reference(receipt)}


@configured_context_case(CONTEXT_LIMIT)
class LiveShapeTests(unittest.TestCase):
    """The incident's shape through production entries: corrections satisfied with REUSE whose receipts
    copy their proof, near the limit in the preceding mechanism's shape, then a pending report."""

    @classmethod
    def setUpClass(cls):
        from pod.governor import _read_journal, finalization
        from pod.history import capacity, publish_context
        from pod.internal import run
        from pod.ledger import _read, objective_root, read
        from pod.util import atomic_json
        from tests.issue41_support import ProductionCase
        from tests.test_history_regressions import previous_history_bundle
        from tests import test_records_boundaries as renewal
        previous = tempfile.TemporaryDirectory(prefix="history-copies-previous-")
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

        def review(name, result, corrections=()):
            frozen = case.packet(["A"], role="review")
            admission = case.start(name, frozen)["admission"]
            case.settle(admission)
            update = {"A": WAITING, **{key: {"kind": "correction", "provenance": "coordinator", "parent": "A",
                                             "check": f"repair {key}", "proof_scope": ["src"], **WAITING}
                                       for key in corrections}}
            triage = [{"finding": f"F{index}", "severity": "major", "triage": "required_correction",
                       "correction": key, "summary": f"repair {key}"} for index, key in enumerate(corrections)]
            report_before_goal_links(case, admission, frozen, outcome=result,
                                     map={"seq": seq() + 1, "update": update}, triage=triage)
            return admission["admission_id"]

        corrections = [f"K{index}" for index in range(12)]
        review("review-failed", "failed", corrections)
        case.write(None, seq=seq() + 1, update={key: {"state": "satisfied", "evidence": [case.proof(key)]}
                                                for key in corrections})
        case.move()
        case.write(None, seq=seq() + 1, update={row["id"]: {"reuse": {"from": row["evidence"][0]["candidate"]}}
                                                for row in case.stored()
                                                if row["state"] == "satisfied" and row["kind"] == "correction"})
        frozen = case.packet(["A"], role="review")
        pending = case.start("review-pending", frozen)["admission"]
        case.settle(pending)
        unpadded = _read(path)
        cls.prior = deepcopy(unpadded)
        cls.prior["source_rejections"] = {"synthetic-padding": ""}
        shaped, _ = compact(cls.prior, force=True)
        cls.prior["source_rejections"]["synthetic-padding"] = (
            "x" * (CONTEXT_LIMIT - 400 - size(inline_copies(shaped, cls.prior))))
        shaped, chunks = compact(cls.prior, force=True)
        publish_context(path, inline_copies(shaped, cls.prior), chunks)
        cls.prior_bytes = path.stat().st_size
        cls.capacity = capacity(path, _read(path))
        case.report(pending, frozen, outcome="succeeded", map={"seq": seq() + 1, "update": {"A": WAITING}})
        cls.physical_bytes = path.stat().st_size
        cls.physical = json.loads(path.read_bytes())
        cls.proposed = _read(path)
        cls.corrections = corrections
        # Remove only the synthetic padding, then satisfy the assurance and close through ordinary entries.
        atomic_json(path, {**cls.proposed, "source_rejections": unpadded["source_rejections"]},
                    limit=CONTEXT_LIMIT, compact=True)
        rows = case.stored()
        for row in rows:
            if row["id"] == "A":
                row.pop("wait")
                row.update(state="satisfied", evidence=[{"attempt": pending["admission_id"]}])
            elif row["id"] == "O1":
                row.pop("executor")
                row.update(state="satisfied", evidence=[case.proof("O1")])
        case.write(rows)
        case.write(case.stored(), close=True)
        from tests.history_support import inline_settled_work
        cls.closed_inline = inline_settled_work(json.loads(path.read_bytes()), _read(path))
        atomic_json(path, cls.closed_inline, limit=CONTEXT_LIMIT, compact=True)

        def facts():
            from pod.ledger import _validate_context, closed_snapshot_context, map_of
            from pod.obligations import report_projection, validate_closed_snapshot
            from pod.status import status
            state = _read(path)
            mapped = map_of(state)
            context = closed_snapshot_context(case.project, "objective", state)
            shown = status(case.project, None, objective="objective")
            shown.pop("record_capacity")  # Physical bytes differ by design; every other key must not.
            return {"validation": _validate_context(deepcopy(state)),
                    "replay": validate_closed_snapshot(mapped, context), "report": report_projection(mapped, context),
                    "closure": mapped["closure"], "finalization": finalization(state, _read_journal(path.parent / "governor.json")),
                    "map": run("map", {"project": str(case.project), "objective": "objective"}), "status": shown,
                    "logical": state}
        cls.before = facts()
        cls.detected = run("record-compaction", {"project": str(case.project), "objective": "objective"})
        request = {**cls.detected["next_action"]["request"]["request"], "reason": "synthetic lossless compaction"}
        cls.compacted = run("record-compaction", {"project": str(case.project), "objective": "objective",
                                                  "request": request})
        cls.closed_compacted = json.loads(path.read_bytes())
        cls.after = facts()

    def test_genuine_prechange_writer_refuses_the_pending_report_without_writing(self):
        from tests.test_history_regressions import previous_history_call
        self.assertGreater(size(self.proposed), CONTEXT_LIMIT)
        result = previous_history_call(self.bundle, self.proposed, operation="write", prior=self.prior)
        self.assertFalse(result["accepted"])
        self.assertEqual((result["version"], result["code"]), ("0.21.16", "record_too_large"))
        self.assertTrue(result["unchanged"])

    def test_pending_report_records_once_receipt_copies_move(self):
        self.assertTrue(CONTEXT_LIMIT - 1000 < self.prior_bytes < CONTEXT_LIMIT)
        self.assertTrue(self.capacity["warning"])
        self.assertGreater(self.capacity["reclaimable_bytes"], 0)
        self.assertLess(self.physical_bytes, CONTEXT_LIMIT)
        self.assertEqual(copies(self.physical), {(key, 0) for key in self.corrections})
        for row, logical in zip(self.physical["checkpoint"]["obligations"], self.proposed["checkpoint"]["obligations"]):
            if row.get("id") in self.corrections:
                self.assertEqual((row["evidence"], row["reuse"]), (logical["evidence"], logical["reuse"]))
                self.assertIsNotNone(row["reuse"])
        print("receipt_copy_capacity=" + str({"prior_bytes": self.prior_bytes, "proposed_bytes": size(self.proposed),
                                             "physical_bytes": self.physical_bytes, "limit_bytes": CONTEXT_LIMIT}))

    def test_validation_closure_replay_and_status_are_identical_after_compaction(self):
        self.assertEqual(copies(self.closed_inline), set())
        self.assertEqual(self.compacted["status"], "compacted")
        # Every satisfied row's receipt copy, including the assurance's current review and the criterion's proof.
        self.assertEqual(copies(self.closed_compacted), {("A", 0), ("O1", 0), *((key, 0) for key in self.corrections)})
        self.assertEqual(self.before["replay"], ())
        self.assertIsNotNone(self.before["closure"])
        self.assertEqual(encoded(self.before, compact=True), encoded(self.after, compact=True))

    def test_compaction_reports_the_bytes_the_copies_reclaim(self):
        self.assertEqual(self.detected["status"], "compaction_available")
        self.assertGreater(self.detected["reclaimable_bytes"], 0)
        self.assertEqual(self.detected["reclaimable_bytes"], self.detected["current_bytes"] - self.detected["proposed_bytes"])
        self.assertEqual(self.compacted["reclaimable_bytes"], self.compacted["current_bytes"] - self.compacted["proposed_bytes"])
        self.assertEqual(self.compacted["proposed_bytes"], len(encoded(self.closed_compacted, compact=True)))


@configured_context_case(CONTEXT_LIMIT)
class LaterWriteTests(unittest.TestCase):
    """After the copies move, later writes hydrate them: restatement, REUSE, renewal and leaving satisfied."""

    @classmethod
    def setUpClass(cls):
        from pod.history import compact_at
        from pod.ledger import _read, _write, objective_root, read
        from tests.issue41_support import ProductionCase
        from tests import test_records_boundaries as renewal
        case = cls.case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        renewal.RenewalAssuranceTests.policy(case)
        path = objective_root(case.project, "objective") / "context.json"

        def seq():
            return read(case.project, "objective")["checkpoint"]["seq"]

        def snapshot():
            return {"physical": json.loads(path.read_bytes()), "logical": _read(path)}

        corrections = cls.corrections = ["K0", "K1", "K2", "K3"]
        frozen = case.packet(["A"], role="review")
        admission = case.start("review-failed", frozen)["admission"]
        case.settle(admission)
        case.report(admission, frozen, outcome="failed", map={"seq": seq() + 1, "update": {"A": WAITING, **{
            key: {"kind": "correction", "provenance": "coordinator", "parent": "A", "check": f"repair {key}",
                  "proof_scope": ["src"], **WAITING} for key in corrections}}},
            triage=[{"finding": f"F{index}", "severity": "major", "triage": "required_correction", "blocks": "outcome",
                     "correction": key, "summary": f"repair {key}"} for index, key in enumerate(corrections)])
        case.write(None, seq=seq() + 1, update={key: {"state": "satisfied", "evidence": [case.proof(key)]}
                                                for key in corrections})

        def reuse(keys=corrections):
            case.write(None, seq=seq() + 1, update={row["id"]: {"reuse": {"from": row["evidence"][0]["candidate"]}}
                                                    for row in case.stored() if row["id"] in keys})
        case.move()
        reuse()
        frozen = case.packet(["A"], role="review")
        passed = case.start("review-passed", frozen)["admission"]
        case.settle(passed)
        case.report(passed, frozen, outcome="succeeded", map={"seq": seq() + 1, "update": {"A": WAITING}})
        cls.review = passed["admission_id"]
        case.write(None, seq=seq() + 1, update={"A": {"state": "satisfied", "evidence": [{"attempt": cls.review}]}})
        # Pad above the unchanged trigger so every later write compacts; well below the limit.
        state = _read(path)
        state["source_rejections"] = {"synthetic-padding": "x" * compact_at()}
        _write(path, state)
        cls.moved = snapshot()
        case.write(case.stored())
        cls.restated = snapshot()
        case.move()
        reuse([*corrections, "A"])
        cls.reused = snapshot()
        # Retain an already-admitted review from before the review-order guard:
        # the current reader and writer still hydrate its reopened proof rows.
        case.write(None, seq=seq() + 1, update={"K1": WAITING, "A": WAITING})
        cls.rereview = start_before_review_guard(case, "review-again", case.packet(["A"], role="review"))
        cls.left = snapshot()
        case.write(renew=[{"id": "K0", "as": "K0R", **WAITING}])
        cls.renewed = snapshot()

    @staticmethod
    def row(snapshot, key, layer="logical"):
        return next(row for row in snapshot[layer]["checkpoint"]["obligations"]
                    if not is_reference(row) and row["id"] == key)

    def test_moved_copies_hydrate_for_restatement_reuse_and_renewal(self):
        from pod.ledger import _validate_context
        moved = {*((key, 0) for key in self.corrections), ("A", 0)}
        self.assertEqual(copies(self.moved["physical"]) & moved, moved)
        self.assertEqual(self.restated["logical"]["checkpoint"]["obligations"],
                         self.moved["logical"]["checkpoint"]["obligations"])
        self.assertEqual(copies(self.restated["physical"]), copies(self.moved["physical"]))
        for key in self.corrections:
            before, after = self.row(self.restated, key), self.row(self.reused, key)
            # REUSE rebinds the hydrated receipt to the new candidate; its proof is unchanged.
            self.assertEqual([item["evidence"] for item in after["receipts"]],
                             [item["evidence"] for item in before["receipts"]])
            self.assertEqual(after["receipts"][-1]["reuse"], after["reuse"])
            self.assertEqual(after["evidence"], before["evidence"])
            self.assertNotEqual(after["reuse"], before["reuse"])
            self.assertEqual(self.row(self.reused, key, "physical")["reuse"], after["reuse"])
            self.assertTrue(all(is_reference(item) for item in self.row(self.reused, key, "physical")["receipts"]))
        self.assertEqual(self.row(self.renewed, "K0R")["renews"], "K0")
        archived = next(row for row in self.renewed["logical"]["checkpoint"]["obligations"] if row["id"] == "K0")
        self.assertEqual(archived["state"], "renewed")
        self.assertEqual(archived["receipts"], self.row(self.reused, "K0")["receipts"])
        self.assertTrue(any(is_reference(row) for row in self.renewed["physical"]["checkpoint"]["obligations"]))
        for snapshot in (self.moved, self.restated, self.reused, self.renewed, self.left):
            _validate_context(deepcopy(snapshot["logical"]))

    def test_rows_that_leave_satisfied_keep_current_proof_inline_as_before(self):
        self.assertTrue(self.rereview["admission"]["admission_id"])
        for key in ("K1", "A"):
            logical, physical = self.row(self.left, key), self.row(self.left, key, "physical")
            self.assertIn(logical["state"], ("waiting", "active"))  # Reopened, or under its re-review.
            selected = [json.dumps(item, sort_keys=True) for item in logical["evidence"]]
            current = self.left["logical"]["checkpoint"]["candidate"]
            for receipt, stored in zip(logical["receipts"], physical["receipts"]):
                evidence = receipt["evidence"]
                if evidence.get("candidate") == current or json.dumps(evidence, sort_keys=True) in selected:
                    self.assertEqual(stored, receipt, key)
        self.assertTrue(self.row(self.left, "K1")["evidence"])

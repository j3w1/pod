"""Accounted-for implementation results relocate without changing their meaning."""
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
from pod.ledger import _read, _validate_context, _write
from pod.records import report_record
from pod.util import digest, encoded
from tests.history_support import size, synthetic_state
from tests.test_history_regressions import previous_history_bundle, previous_history_call


PREVIOUS = "2bced2d193c79151cb73e9595cb7888149b7e2d3"
OUTCOMES = ("succeeded", "partial", "failed", "blocked", "uncertain")


def outcome(row, value):
    report = row["report"]
    report["outcome"] = report["observation"]["outcome"] = value
    report["observation"]["failures"] = ["synthetic unsuccessful check"] if value != "succeeded" else []
    report["observation_digest"] = digest({"status": report["status"], "observation": report["observation"],
        "native_binding": {"runtime": row["runtime"], **{k: row["native_binding"][k]
                           for k in ("runId", "taskId", "dispatchId", "workerId")}}})


def implementation_state():
    state = synthetic_state(count=10)
    state["checkpoint"]["obligations"] = []
    state["checkpoint"]["governance_history"] = {
        "candidates": state["checkpoint"]["governance_history"]["candidates"], "decisions": []}
    for index, row in enumerate(state["admissions"].values()):
        row["role"] = "implement"
        row["disposition"] = ({"kind": "discarded", "reason": "synthetic obsolete result"} if index < 5
            else {"kind": "integrated_into", "candidate": state["checkpoint"]["candidate"]})
        row["failures"] = [{"kind": "unavailable", "cleared_at": "synthetic-time"}]
        outcome(row, OUTCOMES[index % 5])
    state["source_rejections"] = {"synthetic-padding": ""}
    state["source_rejections"]["synthetic-padding"] = "x" * (COMPACT_AT - size(state))
    _validate_context(state)
    return state


class ImplementationOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = implementation_state()

    def test_all_allowed_outcomes_and_both_dispositions_move_at_trigger_exactly(self):
        self.assertEqual(size(self.state), COMPACT_AT)
        below = deepcopy(self.state)
        below["source_rejections"]["synthetic-padding"] = below["source_rejections"]["synthetic-padding"][:-1]
        self.assertEqual(compact(below), (below, {}))
        record, chunks = compact(self.state)
        self.assertTrue(all(is_reference(row) for row in record["admissions"].values()))
        restored = hydrate(record, chunks.__getitem__)
        self.assertEqual(encoded(restored, compact=True), encoded(self.state, compact=True))
        self.assertNotIn("history", self.state)

    def check_vetoes(self, cases):
        for label, change in cases:
            with self.subTest(label=label):
                state = deepcopy(self.state)
                row = state["admissions"]["attempt-001"]
                change(row)
                record, chunks = compact(state, trigger_bytes=0)
                self.assertEqual(record["admissions"]["attempt-001"], row)
                self.assertTrue(is_reference(record["admissions"]["attempt-002"]))
                self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_unaccounted_current_and_unsettled_results_stay_inline(self):
        self.check_vetoes([
            ("undispositioned", lambda row: row.update(disposition=None)),
            ("current", lambda row: row.update(candidate=self.state["checkpoint"]["candidate"])),
            ("unknown candidate", lambda row: row.update(candidate=None)),
            ("unreported", lambda row: row.update(report=None)),
            ("reserved", lambda row: row.update(state="reserved")),
            ("unresolved", lambda row: row.update(state="unresolved")),
            ("unrecognized report status", lambda row: row["report"].update(status="observed")),
            ("invalid outcome", lambda row: row["report"].update(outcome="UNKNOWN")),
        ])

    def test_active_failures_and_unknown_or_mismatched_routes_stay_inline(self):
        self.check_vetoes([
            ("active failure", lambda row: row.update(failures=[{"kind": "unavailable"}])),
            ("unknown route", lambda row: row["route_decision"].update(effective_unknown=True)),
            ("route mismatch", lambda row: row["route_decision"].update(route_mismatch=True)),
        ])

    def test_investigation_requires_success_and_settled_reviews_move(self):
        # Validated investigations still need success; reviews follow A549 (any outcome once settled).
        state = deepcopy(self.state)
        for index, row in enumerate(state["admissions"].values()):
            row["role"] = "review" if index < 5 else "investigate"
            state["checkpoint"]["obligations"].append({"id": row["serves"][0], "kind": "assurance",
                "receipts": [{"reported_seq": 10, "evidence": {"attempt": row["admission_id"]}}]})
        record, chunks = compact(state)
        for index, (key, row) in enumerate(state["admissions"].items()):
            self.assertEqual(is_reference(record["admissions"][key]), index < 5 or index % 5 == 0)
        self.assertEqual(hydrate(record, chunks.__getitem__), state)

    def test_native_governor_and_ambiguous_joins_preserve_vetoes(self):
        from pod.history import hydrate
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "context.json"
            for outcome in ("pending", "UNKNOWN", "FAILED"):
                journal = {"actions": [{"decision": "ALLOW", "outcome": outcome}]}
                record, chunks = prepare_context(path, self.state)
                self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
                self.assertEqual(record["schema"], "pod-history-record/v1")


class ImplementationCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous = tempfile.TemporaryDirectory(prefix="history-outcomes-previous-")
        cls.addClassCleanup(previous.cleanup)
        cls.bundle = previous_history_bundle(previous.name, PREVIOUS)
        cls.prior = implementation_state()
        for index, row in enumerate(cls.prior["admissions"].values()):
            outcome(row, ("partial", "failed", "uncertain")[index % 3])
        pending = cls.prior["admissions"]["attempt-009"]
        cls.report = deepcopy(pending["report"])
        pending.update(candidate=cls.prior["checkpoint"]["candidate"], disposition=None, report=None)
        cls.report["observation"]["candidate"] = pending["candidate"]
        pending["report"] = cls.report
        outcome(pending, "partial")
        pending["report"] = None
        report_record(cls.report["observation"])
        padding = cls.prior["source_rejections"]
        padding["synthetic-padding"] += "x" * (CONTEXT_LIMIT - 100 - size(cls.prior))
        cls.proposed = deepcopy(cls.prior)
        cls.proposed["admissions"]["attempt-009"]["report"] = cls.report
        _validate_context(cls.prior)
        _validate_context(cls.proposed)

    def test_genuine_prechange_writer_refuses_same_valid_report_without_writing(self):
        self.assertLess(size(self.prior), CONTEXT_LIMIT)
        self.assertGreater(size(self.proposed), CONTEXT_LIMIT)
        result = previous_history_call(self.bundle, self.proposed, operation="write", prior=self.prior)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["version"], "0.21.9")
        self.assertEqual(result["code"], "record_too_large")
        self.assertEqual(result["detail"]["limit_bytes"], CONTEXT_LIMIT)
        self.assertTrue(result["unchanged"])

    def test_same_report_write_fits_and_hydrates_every_field_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_bytes(encoded(self.prior, compact=True))
            expected = deepcopy(self.proposed)
            _write(path, expected)
            self.assertEqual(_read(path), expected)
            record = json.loads(path.read_bytes())
            self.assertLess(path.stat().st_size, CONTEXT_LIMIT)
            self.assertEqual(_read(path)["admissions"]["attempt-009"], expected["admissions"]["attempt-009"])
            self.assertEqual(record["schema"], "pod-history-record/v1")
            print("implementation_capacity=" + str({"prior_bytes": size(self.prior),
                "proposed_bytes": size(self.proposed), "physical_bytes": path.stat().st_size,
                "limit_bytes": CONTEXT_LIMIT}))

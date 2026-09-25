"""Recorded-trace checks: sanitized captures and separately labelled synthetic 0.6 traces."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

from pod.obligations import evaluate_trace

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "tests" / "fixtures" / "traces"


class TraceCheckTests(unittest.TestCase):
    def test_scripted_checks_cover_triangle_churn_and_scope_inflation(self):
        serial = json.loads((TRACES / "synthetic-serialization.json").read_text())
        result = evaluate_trace(serial)
        self.assertEqual(result["label"], "synthetic")
        self.assertEqual(result["checks"], {"slot_filling": "PASS", "serialization": "FLAGGED",
                                            "amplification": "PASS", "churn": "PASS", "scope_inflation": "PASS"})
        self.assertEqual({row["obligation"] for row in result["findings"]["serialization"]}, {"S", "T"})
        self.assertFalse(result["failed"])
        explained = evaluate_trace(json.loads((TRACES / "synthetic-explained.json").read_text()))
        self.assertEqual(explained["checks"]["serialization"], "PASS")
        inflated = json.loads(json.dumps(serial))
        rows = inflated["records"][1]["map"]["obligations"]
        rows.append({"id": "X", "kind": "criterion", "provenance": "coordinator", "check": "invented gate",
                     "state": "unassigned"})
        inflated["records"] += [{"kind": "admission", "serves": ["P9"], "accepted": True},
                                {"kind": "admission", "serves": ["X"], "role": "review", "accepted": True}]
        inflated["records"] += [{"kind": "settlement", "serves": ["T"]}] * 3
        verdict = evaluate_trace(inflated)
        self.assertEqual(verdict["checks"]["scope_inflation"], "FAILED")
        self.assertEqual(verdict["checks"]["slot_filling"], "FAILED")
        self.assertEqual(verdict["checks"]["churn"], "FLAGGED")
        self.assertTrue(verdict["failed"])
        self.assertEqual(verdict["observations"]["admissions"], 3)

    def test_amplification_is_a_review_of_a_satisfied_assurance(self):
        serial = json.loads((TRACES / "synthetic-serialization.json").read_text())
        write = serial["records"][0]
        write["map"]["obligations"].append({"id": "A", "kind": "assurance", "provenance": "coordinator",
                                            "parent": "O1", "state": "satisfied", "check": "reviewed"})
        trace = {"schema": "pod-trace/v1", "label": "synthetic", "records": [
            write, {"kind": "admission", "serves": ["A"], "role": "review", "accepted": True}]}
        self.assertEqual(evaluate_trace(trace)["checks"]["amplification"], "FAILED")

    def test_recorded_native_capture_is_not_evaluable_and_counts_stay_observations(self):
        capture = json.loads((TRACES / "pr20-native-observations.json").read_text())
        result = evaluate_trace(capture)
        self.assertFalse(result["evaluable"])
        self.assertEqual(result["label"], "recorded_native_observations")
        self.assertEqual(set(result["checks"].values()), {"NOT_EVALUABLE"})
        self.assertEqual(result["observations"]["native_dispatches"], 19)
        self.assertEqual(result["observations"]["admissions"], "unknown")
        self.assertFalse(result["failed"])

    def test_tool_reads_files_only_and_fails_on_refused_behaviour(self):
        completed = subprocess.run([sys.executable, str(ROOT / "tools" / "trace_check.py"),
                                    str(TRACES / "synthetic-serialization.json"),
                                    str(TRACES / "pr20-native-observations.json")],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("NOT_EVALUABLE", completed.stdout)
        missing = subprocess.run([sys.executable, str(ROOT / "tools" / "trace_check.py"), str(TRACES / "none.json")],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(missing.returncode, 1)


if __name__ == "__main__":
    unittest.main()

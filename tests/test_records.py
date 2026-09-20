import unittest

from pod.records import packet, report, source_identity, verify_sources, acceptance
from pod.errors import PodError
from tests.common import fixture


def packet_body():
    return {"schema": "pod-packet/v1", "objective": "make change", "criteria": ["works"],
            "responsibility": "writer", "scope": ["src/a.py"], "actions": ["edit"],
            "candidate": "abc", "context": ["read AGENTS.md"], "dependencies": [],
            "route": {"agent": "codex"}, "policy_revision": "p", "plan_revision": "plan",
            "report_contract": "report checks", "sources": []}


class RecordTests(unittest.TestCase):
    def test_packet_report_scope_and_binding(self):
        frozen = packet(packet_body())
        row = {"schema": "pod-report/v1", "assignment": frozen["packet_id"], "attempt": "one",
               "candidate": "abc", "outcome": "succeeded", "scope": ["src/a.py"], "files": [],
               "checks": [], "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        self.assertEqual(report(row, frozen), row)
        row["scope"] = ["src/other.py"]
        with self.assertRaises(PodError):
            report(row, frozen)
        row["scope"] = []
        row["candidate"] = "other"
        with self.assertRaises(PodError):
            report(row, frozen)

    def test_source_changed_absent_unavailable_distinct(self):
        with fixture() as root:
            path = root / "a.txt"
            path.write_text("one")
            before = source_identity(root, "a.txt")
            self.assertEqual(before["state"], "present")
            verify_sources(root, [before])
            path.write_text("two")
            with self.assertRaises(PodError):
                verify_sources(root, [before])
            path.unlink()
            self.assertEqual(source_identity(root, "a.txt")["state"], "absent")
            with self.assertRaises(PodError):
                verify_sources(root, [before])

    def test_criterion_cannot_be_satisfied_by_wrong_candidate(self):
        row = {"schema": "pod-evidence/v1", "criterion": "works", "candidate": "old",
               "sources": [], "policy_revision": "p", "dependencies": [], "environment": "fixture",
               "check": "unit", "command": "python -m unittest", "result": "pass",
               "timestamp": "2026-09-20T00:00:00Z", "status": "PASS", "reference": "artifact"}
        with self.assertRaises(PodError):
            acceptance(["works"], [row], candidate="new", policy_revision="p",
                       sources=[], dependencies=[], environment="fixture",
                       review_required=True, hosted_required=True)

    def test_failed_required_check_blocks_even_with_pass_and_review_is_contextual(self):
        base = {"schema": "pod-evidence/v1", "criterion": "works", "candidate": "c",
                "sources": [], "policy_revision": "p", "dependencies": [], "environment": "fixture",
                "check": "unit", "command": "python -m unittest", "result": "observed",
                "timestamp": "2026-09-20T00:00:00Z", "status": "PASS", "reference": "artifact"}
        result = acceptance(["works"], [base, {**base, "status": "FAILED"}],
                            candidate="c", policy_revision="p", sources=[], dependencies=[],
                            environment="fixture", review_required=False, hosted_required=False)
        self.assertFalse(result["required_checks_pass"])
        self.assertEqual(result["independently_reviewed"], "NOT_REQUIRED")
        self.assertFalse(result["accepted"])
        with self.assertRaises(PodError):
            acceptance(["works"], [base], candidate="c", policy_revision="p",
                       sources=[{"path": "changed"}], dependencies=[], environment="fixture",
                       review_required=False, hosted_required=False)

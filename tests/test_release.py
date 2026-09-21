import unittest

from pod.release import LIVE_CHECKS, REQUIRED_GATES, release_gate
from pod.errors import PodError

COMMIT = "1" * 40
TREE = "2" * 40
OTHER_COMMIT = "3" * 40
REPORT_DIGEST = "4" * 64


def row(gate, outcome="PASS"):
    host = ("Linux" if gate.endswith("_linux") else
            "Windows" if gate.endswith("_windows") else "Linux")
    value = {"schema": "pod-validation/v1", "candidate": COMMIT, "tree": TREE,
             "host": host, "utc": "2026-09-20T00:00:00Z", "gate": gate,
             "command": "bounded check", "outcome": outcome,
             "report": f"reports/{gate}.txt sha256:{REPORT_DIGEST}"}
    if gate.startswith("live_"):
        value["checks"] = {name: "PASS" for name in LIVE_CHECKS}
    return value


class ReleaseGateTests(unittest.TestCase):
    def test_missing_live_core_blocks_even_with_offline_pass(self):
        records = [row(gate) for gate in REQUIRED_GATES if not gate.startswith("live_")]
        result = release_gate(COMMIT, TREE, records)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gates"]["live_codex_windows"], "NOT_RUN")
        self.assertFalse(result["release_authorized"])

    def test_all_records_only_request_owner_decision(self):
        result = release_gate(COMMIT, TREE, [row(gate) for gate in REQUIRED_GATES])
        self.assertEqual(result["status"], "owner_decision_required")
        self.assertFalse(result["release_authorized"])
        partial = [row(gate) for gate in REQUIRED_GATES]
        partial[0]["candidate"] = OTHER_COMMIT
        self.assertEqual(release_gate(COMMIT, TREE, partial)["gates"][REQUIRED_GATES[0]], "NOT_RUN")

    def test_skill_validation_is_a_required_release_gate(self):
        self.assertIn("skill_validation", REQUIRED_GATES)
        records = [row(gate) for gate in REQUIRED_GATES if gate != "skill_validation"]
        result = release_gate(COMMIT, TREE, records)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gates"]["skill_validation"], "NOT_RUN")

    def test_hosted_linux_and_windows_are_independently_required(self):
        self.assertIn("hosted_ci_linux", REQUIRED_GATES)
        self.assertIn("hosted_ci_windows", REQUIRED_GATES)
        for missing in ("hosted_ci_linux", "hosted_ci_windows"):
            with self.subTest(missing=missing):
                records = [row(gate) for gate in REQUIRED_GATES if gate != missing]
                result = release_gate(COMMIT, TREE, records)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["gates"][missing], "NOT_RUN")
        complete = release_gate(COMMIT, TREE, [row(gate) for gate in REQUIRED_GATES])
        self.assertEqual(complete["status"], "owner_decision_required")
        self.assertFalse(complete["release_authorized"])

    def test_os_labelled_gates_reject_single_host_spoofing(self):
        for host in ("Linux", "Windows"):
            with self.subTest(host=host):
                records = [row(gate) for gate in REQUIRED_GATES]
                for record in records:
                    record["host"] = host
                with self.assertRaises(PodError) as caught:
                    release_gate(COMMIT, TREE, records)
                self.assertEqual(caught.exception.code, "invalid_validation")

    def test_non_os_gate_host_vocabulary_and_duplicate_unknown_validation(self):
        invalid_host = row("skill_validation")
        invalid_host["host"] = "fixture"
        with self.assertRaises(PodError) as caught:
            release_gate(COMMIT, TREE, [invalid_host])
        self.assertEqual(caught.exception.code, "invalid_validation")
        duplicate = row("unit_linux")
        with self.assertRaises(PodError) as caught:
            release_gate(COMMIT, TREE, [duplicate, dict(duplicate)])
        self.assertEqual(caught.exception.code, "duplicate_validation")
        unknown = row("unit_linux")
        unknown["gate"] = "unit_other"
        with self.assertRaises(PodError) as caught:
            release_gate(COMMIT, TREE, [unknown])
        self.assertEqual(caught.exception.code, "invalid_validation")

    def test_live_record_needs_all_subchecks(self):
        records = [row(gate) for gate in REQUIRED_GATES]
        live = next(x for x in records if x["gate"] == "live_codex_linux")
        live["checks"]["adoption"] = "NOT_RUN"
        self.assertEqual(release_gate(COMMIT, TREE, records)["gates"]["live_codex_linux"], "UNAVAILABLE")

    def test_malformed_git_identities_are_rejected(self):
        for candidate, tree, records in (
                ("not-a-git-object", TREE, []),
                (COMMIT, "not-a-tree", []),
                (COMMIT, TREE, [{**row("unit_linux"), "candidate": "short"}]),
                (COMMIT, TREE, [{**row("unit_linux"), "tree": "F" * 40}])):
            with self.subTest(candidate=candidate, tree=tree, records=records):
                with self.assertRaises(PodError) as caught:
                    release_gate(candidate, tree, records)
                self.assertEqual(caught.exception.code, "invalid_validation")

    def test_timestamp_requires_canonical_utc(self):
        for timestamp in (
                "2026-09-20T00:00:00-05:00",
                "2026-09-20T00:00:00+00:00",
                "2026-09-20T00:00:00",
                "2026-02-30T00:00:00Z"):
            with self.subTest(timestamp=timestamp):
                invalid = {**row("unit_linux"), "utc": timestamp}
                with self.assertRaises(PodError) as caught:
                    release_gate(COMMIT, TREE, [invalid])
                self.assertEqual(caught.exception.code, "invalid_validation")

    def test_report_requires_sanitized_reference_and_full_digest(self):
        valid = row("unit_linux")
        invalid_reports = (
            "artifact",
            "artifact sha256:abc",
            f"/absolute/report.txt sha256:{REPORT_DIGEST}",
            f"../report.txt sha256:{REPORT_DIGEST}",
            f"reports//report.txt sha256:{REPORT_DIGEST}",
            f"reports/report.txt sha256:{'A' * 64}",
        )
        for report in invalid_reports:
            with self.subTest(report=report):
                with self.assertRaises(PodError) as caught:
                    release_gate(COMMIT, TREE, [{**valid, "report": report}])
                self.assertEqual(caught.exception.code, "invalid_validation")
        missing = dict(valid)
        del missing["report"]
        with self.assertRaises(PodError) as caught:
            release_gate(COMMIT, TREE, [missing])
        self.assertEqual(caught.exception.code, "invalid_validation")

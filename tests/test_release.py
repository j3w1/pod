import unittest

from pod.release import DELEGATION_CHECKS, LIVE_CHECKS, REQUIRED_GATES, release_gate
from pod.errors import PodError

COMMIT = "1" * 40
TREE = "2" * 40
OTHER_COMMIT = "3" * 40
REPORT_DIGEST = "4" * 64
SHA256_COMMIT = "5" * 64
SHA256_TREE = "6" * 64


def row(gate, outcome="PASS"):
    value = {"schema": "pod-validation/v1", "candidate": COMMIT, "tree": TREE,
             "host": "Linux", "utc": "2026-09-20T00:00:00Z", "gate": gate,
             "command": "bounded check", "outcome": outcome,
             "report": f"reports/{gate}.txt sha256:{REPORT_DIGEST}"}
    if gate.startswith("live_"):
        value["checks"] = {name: "PASS" for name in LIVE_CHECKS}
    if gate.startswith("orca_delegation_"):
        value["checks"] = {name: "PASS" for name in DELEGATION_CHECKS}
    return value


def authorization(candidate=COMMIT, tree=TREE, scope=("merge", "release")):
    return {"schema": "pod-release-authorization/v1", "candidate": candidate, "tree": tree,
            "scope": list(scope), "authorized_by": "owner instruction",
            "utc": "2026-09-21T00:00:00Z", "reference": "tasks/pod/authorization.md"}


class ReleaseGateTests(unittest.TestCase):
    def test_missing_live_core_blocks_even_with_offline_pass(self):
        records = [row(gate) for gate in REQUIRED_GATES if not gate.startswith("live_")]
        result = release_gate(COMMIT, TREE, records)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gates"]["live_codex_linux"], "NOT_RUN")
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

    def test_gate_list_is_linux_only_and_covers_delegation_and_packaging(self):
        self.assertNotIn("matched_evaluation", REQUIRED_GATES)
        self.assertFalse([gate for gate in REQUIRED_GATES if gate.endswith("_windows")])
        for gate in ("hosted_ci_linux", "skill_bundle_parity", "skills_cli_install",
                     "bundle_copy_form", "orca_delegation_codex", "orca_delegation_claude"):
            with self.subTest(gate=gate):
                self.assertIn(gate, REQUIRED_GATES)
                records = [row(other) for other in REQUIRED_GATES if other != gate]
                result = release_gate(COMMIT, TREE, records)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["gates"][gate], "NOT_RUN")

    def test_non_linux_host_is_rejected(self):
        for host in ("Windows", "Darwin", "linux"):  # platform-audit: refusal
            with self.subTest(host=host):
                records = [{**row("unit_linux"), "host": host}]
                with self.assertRaises(PodError) as caught:
                    release_gate(COMMIT, TREE, records)
                self.assertEqual(caught.exception.code, "invalid_validation")

    def test_delegation_record_needs_every_subcheck(self):
        records = [row(gate) for gate in REQUIRED_GATES]
        delegation = next(x for x in records if x["gate"] == "orca_delegation_claude")
        delegation["checks"]["delivery"] = "NOT_RUN"
        result = release_gate(COMMIT, TREE, records)
        self.assertEqual(result["gates"]["orca_delegation_claude"], "UNAVAILABLE")
        self.assertEqual(result["status"], "blocked")

    def test_owner_authorization_is_an_input_not_a_test_result(self):
        records = [row(gate) for gate in REQUIRED_GATES]
        complete = release_gate(COMMIT, TREE, records)
        self.assertEqual(complete["status"], "owner_decision_required")
        self.assertFalse(complete["release_authorized"])
        self.assertFalse(complete["authorization"]["present"])
        granted = release_gate(COMMIT, TREE, records, authorization())
        self.assertEqual(granted["status"], "authorized")
        self.assertTrue(granted["release_authorized"])
        self.assertEqual(granted["authorization"]["scope"], ["merge", "release"])
        for mismatch in (authorization(candidate=OTHER_COMMIT),
                         authorization(tree="7" * 40),
                         authorization(scope=("merge",))):
            with self.subTest(mismatch=mismatch["scope"]):
                result = release_gate(COMMIT, TREE, records, mismatch)
                self.assertEqual(result["status"], "owner_decision_required")
                self.assertFalse(result["release_authorized"])
        incomplete = [row(gate) for gate in REQUIRED_GATES if gate != "hosted_ci_linux"]
        still_blocked = release_gate(COMMIT, TREE, incomplete, authorization())
        self.assertEqual(still_blocked["status"], "blocked")
        self.assertFalse(still_blocked["release_authorized"])

    def test_malformed_authorization_is_refused(self):
        records = [row(gate) for gate in REQUIRED_GATES]
        for broken in ({**authorization(), "schema": "other/v1"},
                       {**authorization(), "utc": "2026-09-21T00:00:00+00:00"},
                       {**authorization(), "scope": []},
                       {**authorization(), "scope": ["publish"]},
                       {**authorization(), "candidate": "short"}):
            with self.subTest(broken=broken.get("schema")):
                with self.assertRaises(PodError) as caught:
                    release_gate(COMMIT, TREE, records, broken)
                self.assertEqual(caught.exception.code, "invalid_authorization")

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

    def test_mixed_git_object_formats_are_rejected(self):
        for candidate, tree, records in (
                (COMMIT, SHA256_TREE, []),
                (SHA256_COMMIT, TREE, []),
                (COMMIT, TREE, [{**row("unit_linux"), "tree": SHA256_TREE}]),
                (COMMIT, TREE, [{**row("unit_linux"), "candidate": SHA256_COMMIT}])):
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

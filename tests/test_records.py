import unittest
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pod.records import (acceptance, integration_observation, packet, report,
                         source_identity, verify_sources)
from pod.errors import PodError
from tests.common import fixture


def packet_body():
    return {"schema": "pod-packet/v1", "objective": "make change", "criteria": ["works"],
            "responsibility": "writer", "scope": ["src/a.py"], "actions": ["edit"],
            "candidate": "abc", "context": [{"kind": "instruction", "path": "AGENTS.md", "sha256": "a" * 64}], "dependencies": [],
            "route": {"agent": "codex"}, "policy_revision": "p", "plan_revision": "plan",
            "report_contract": "report checks", "sources": []}


class RecordTests(unittest.TestCase):
    def test_conventional_credential_paths_rejected_before_open_and_packet_admission(self):
        excluded = (
            ".env", ".env.production", "nested/.netrc", "nested/_netrc", "nested\\_netrc",
            "id_rsa", "id_dsa.old", "id_ecdsa_sk", "id_ed25519-work",
            "nested/.npmrc", ".pypirc", ".git-credentials", "certs/client.pfx",
            "certs/client.p12", "certs/client.pem", "certs/client.key",
            ".aws/credentials", ".azure/accessTokens.json", ".kube/config",
            ".docker/config.json", ".config/gcloud/application_default_credentials.json",
            ".config/gh/hosts.yml", ".gnupg/private-keys-v1.d/key",
            ".local/share/keyrings/login.keyring", ".authinfo", ".pgpass",
            "credentials.json", "auth.json", "tokens.json",
        )
        with fixture() as root:
            for path in excluded:
                with self.subTest(path=path):
                    with patch("pod.records.os.open", side_effect=AssertionError("source opened")) as opened:
                        with self.assertRaises(PodError) as rejected:
                            source_identity(root, path)
                    self.assertEqual(rejected.exception.code, "secret_source")
                    opened.assert_not_called()
                    for field, reference in (
                        ("sources", {"path": path, "state": "absent"}),
                        ("context", {"kind": "source", "path": path, "sha256": "a" * 64}),
                        ("context", {"kind": "instruction", "path": path, "sha256": "a" * 64}),
                    ):
                        with self.subTest(field=field, kind=reference.get("kind")):
                            with self.assertRaises(PodError):
                                packet({**packet_body(), field: [reference]})

    def test_ordinary_sources_and_public_key_files_remain_admissible(self):
        with fixture() as root:
            for path in ("src/ordinary.py", "keys/id_rsa.pub", "keys/id_ed25519_sk.pub"):
                with self.subTest(path=path):
                    file = root / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(b"harmless fixture")
                    bound = source_identity(root, path)
                    self.assertEqual(bound["state"], "present")
                    self.assertEqual(bound["sha256"], hashlib.sha256(b"harmless fixture").hexdigest())
                    self.assertEqual(packet({**packet_body(), "sources": [bound]})["body"]["sources"], [bound])
                    context = {"kind": "source", "path": path, "sha256": bound["sha256"]}
                    self.assertEqual(packet({**packet_body(), "context": [context]})["body"]["context"], [context])

    def test_packet_report_scope_and_binding(self):
        frozen = packet(packet_body())
        row = {"schema": "pod-report/v1", "assignment": frozen["packet_id"], "attempt": "dispatch",
               "candidate": "abc", "outcome": "succeeded", "scope": ["src/a.py"], "files": [],
               "checks": [], "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        binding = {"runtime": "runtime", "runId": "run", "taskId": "task",
                   "dispatchId": "dispatch", "workerId": "worker"}
        self.assertEqual(report(row, frozen, binding)["status"], "validated_observation")
        row["scope"] = ["src/other.py"]
        self.assertEqual(report(row, frozen, binding)["status"], "reconciliation_required")
        row["scope"] = []
        row["candidate"] = "other"
        with self.assertRaises(PodError):
            report(row, frozen, binding)

    def test_nested_context_and_report_scope_deviations(self):
        body = packet_body()
        body["context"] = [{"kind": "source", "path": ".env.production", "sha256": "digest"}]
        with self.assertRaises(PodError):
            packet(body)
        body["context"] = [{"kind": "source", "path": "ok.txt", "sha256": "digest",
                            "nested": {"TOKEN": "placeholder"}}]
        with self.assertRaises(PodError):
            packet(body)
        frozen = packet(packet_body())
        binding = {"runtime": "runtime", "runId": "run", "taskId": "task",
                   "dispatchId": "dispatch", "workerId": "worker"}
        row = {"schema": "pod-report/v1", "assignment": frozen["packet_id"], "attempt": "dispatch",
               "candidate": "abc", "outcome": "succeeded", "scope": [], "files": ["outside.py"],
               "checks": [], "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        self.assertEqual(report(row, frozen, binding)["status"], "reconciliation_required")
        with self.assertRaises(PodError):
            report({**row, "attempt": "unissued"}, frozen, binding)

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
            (root / ".env.production").write_text("placeholder")
            with self.assertRaises(PodError):
                source_identity(root, ".env.production")
            with self.assertRaises(PodError):
                verify_sources(root, [before])

    def test_source_parent_replacement_keeps_original_directory(self):
        with fixture() as root:
            project = root / "project"
            parent = project / "parent"
            parent.mkdir(parents=True)
            (parent / "ordinary.txt").write_bytes(b"inside fixture")
            outside = root / "outside"
            outside.mkdir()
            (outside / "ordinary.txt").write_bytes(b"outside fixture")
            real_open = os.open
            swapped = False

            def swap_before_final(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == "ordinary.txt" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    parent.rename(project / "retained")
                    parent.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with patch("pod.records.os.open", side_effect=swap_before_final):
                result = source_identity(project, "parent/ordinary.txt")
            self.assertTrue(swapped)
            self.assertEqual(result["sha256"], hashlib.sha256(b"inside fixture").hexdigest())
            with self.assertRaises(PodError) as redirected:
                source_identity(project, "parent/ordinary.txt")
            self.assertEqual(redirected.exception.code, "unsafe_source")

    def test_fifo_source_returns_without_waiting_for_writer(self):
        with fixture() as root:
            os.mkfifo(root / "pipe")
            script = ("from pathlib import Path; from pod.records import source_identity; "
                      "from pod.errors import PodError; import sys; "
                      "\ntry: source_identity(Path(sys.argv[1]), 'pipe')\n"
                      "except PodError as error: print(error.code)")
            result = subprocess.run([sys.executable, "-c", script, str(root)],
                                    capture_output=True, text=True, timeout=2, check=True)
            self.assertEqual(result.stdout.strip(), "unsafe_source")

    def test_missing_no_follow_primitive_fails_closed(self):
        with fixture() as root:
            (root / "ordinary.txt").write_text("fixture")
            with patch("pod.records.os.O_NOFOLLOW", 0):
                with self.assertRaises(PodError) as unavailable:
                    source_identity(root, "ordinary.txt")
            self.assertEqual(unavailable.exception.code, "source_unavailable")

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

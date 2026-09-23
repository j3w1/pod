from pathlib import Path
import subprocess
import unittest
import zipfile

from tests.common import fixture
from tools.artifact_audit import audit, audit_source


def tracked(root: Path, files: dict[str, bytes]) -> None:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)


class TrackedSourceAuditTests(unittest.TestCase):
    def test_generic_source_hygiene_detects_private_residue(self):
        with fixture() as root:
            tracked(root, {"ok.txt": b"public"})
            self.assertEqual(audit_source(root), [])
            leak = b"/home/" + b"private-user/worktree\n"
            (root / "leak.txt").write_bytes(leak)
            subprocess.run(["git", "-C", str(root), "add", "leak.txt"], check=True)
            self.assertTrue(any("personal path" in row for row in audit_source(root)))

    def test_credential_findings_never_echo_the_credential(self):
        credential = "gh" + "p_" + "A" * 24
        with fixture() as root:
            tracked(root, {"leak.txt": credential.encode()})
            source_findings = audit_source(root)
            self.assertTrue(any("credential-shaped" in row for row in source_findings))
            self.assertNotIn(credential, "\n".join(source_findings))

            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("pod/leak.txt", credential)
            artifact_findings = audit([wheel])
            self.assertTrue(any("credential-shaped" in row for row in artifact_findings))
            self.assertNotIn(credential, "\n".join(artifact_findings))

    def test_public_product_metadata_is_permitted_and_no_path_is_exempt(self):
        with fixture() as root:
            tracked(root, {
                "README.md": b"j3w1/pod https://github.com/j3w1/pod fixture@example.invalid\n",
                "tests/fixtures/capture.json": b'{"runtime":"uuid-0001"}\n',
            })
            self.assertEqual(audit_source(root), [])
            residue = b"/home/" + b"some-user/record\n"
            for name in ("docs/notes/record.md", "tests/fixtures/capture.json"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(residue)
                subprocess.run(["git", "-C", str(root), "add", name], check=True)
                findings = audit_source(root)
                self.assertTrue(any(row.startswith(name) and "personal path" in row for row in findings), findings)
                (root / name).write_bytes(b"clean\n")
                subprocess.run(["git", "-C", str(root), "add", name], check=True)

    def test_trail_vocabulary_is_a_finding_outside_the_guard_and_fixtures(self):
        with fixture() as root:
            tracked(root, {
                "docs/notes.md": b"see docs/history/record.md\n",
                "skills/pod/ledger.py": b"STATES = ('legacy_hold',)\n",
                "tests/fixtures/capture.json": b'{"note":"legacy migration capacity_full"}\n',
            })
            findings = audit_source(root)
            self.assertEqual(sorted(row.split(" :: ")[0] for row in findings),
                             ["docs/notes.md", "skills/pod/ledger.py"])
            self.assertTrue(all("trail" in row for row in findings), findings)
            self.assertFalse(any("legacy_hold" in row or "docs/history" in row for row in findings))
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("pod/references/orca-boundary.md", "use internal state-migrate\n")
                archive.writestr("j3w1_pod-0.0.0/tests/fixtures/x.json", "capacity_full\n")
            artifact_findings = audit([wheel])
            self.assertEqual(len(artifact_findings), 1, artifact_findings)
            self.assertIn("orca-boundary.md :: trail vocabulary", artifact_findings[0])

    def test_repository_tracked_source_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_source(root), [])

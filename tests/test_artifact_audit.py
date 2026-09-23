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

    def test_public_product_metadata_and_sanitized_history_are_permitted(self):
        with fixture() as root:
            historical = b"/home/" + b"retired-user/old-record\n"
            tracked(root, {
                "README.md": b"j3w1/pod https://github.com/j3w1/pod fixture@example.invalid\n",
                "docs/history/record.md": historical,
                "tests/fixtures/capture.json": b'{"runtime":"uuid-0001"}\n',
            })
            self.assertEqual(audit_source(root), [])

    def test_repository_tracked_source_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_source(root), [])

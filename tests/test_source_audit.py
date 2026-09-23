from pathlib import Path
import subprocess
import unittest

from tests.common import fixture
from tools.source_audit import audit_source


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
            findings = audit_source(root)
            self.assertTrue(any("credential-shaped" in row for row in findings))
            self.assertNotIn(credential, "\n".join(findings))

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

    def test_pod_publication_material_is_a_finding(self):
        with fixture() as root:
            tracked(root, {
                "docs/notes.md": b"see docs/history/record.md\n",
                "skills/pod/ledger.py": b"STATES = ('legacy_hold',)\n",
                "README.md": b"pin it with npx skills add " + b"j3w1/pod#" + b"v0.1.0\n",
                "docs/checks.md": b"run tools/" + b"release.py gate\n",
                "tests/fixtures/capture.json": b'{"note":"legacy migration capacity_full"}\n',
            })
            findings = audit_source(root)
            self.assertEqual(sorted(row.split(" :: ")[0] for row in findings),
                             ["README.md", "docs/checks.md", "docs/notes.md", "skills/pod/ledger.py"])
            self.assertFalse(any("legacy_hold" in row or "docs/history" in row for row in findings))

    def test_publication_paths_are_findings_but_skill_setup_is_not(self):
        with fixture() as root:
            tracked(root, {"skills/pod/setup.py": b"# project enrollment\n"})
            self.assertEqual(audit_source(root), [])
            for name in ("release/NOTES.md", "CHANGELOG.md", "pyproject.toml", "install.py",
                         "skills/pod/release.py"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"x\n")
                subprocess.run(["git", "-C", str(root), "add", name], check=True)
            flagged = sorted(row.split(" :: ")[0] for row in audit_source(root))
            self.assertEqual(flagged, ["CHANGELOG.md", "install.py", "pyproject.toml",
                                       "release/NOTES.md", "skills/pod/release.py"])

    def test_a_governed_project_release_remains_ordinary_vocabulary(self):
        """Pod governs a project's own release and deploy steps; that is not Pod publishing."""
        with fixture() as root:
            tracked(root, {"skills/pod/references/governor.md":
                           b"Merge, release and deployment need an owner authorization.\n"
                           b"A release candidate of the governed project is prepared first.\n"
                           b"The project's CHANGELOG and GitHub Release belong to that project.\n"})
            self.assertEqual(audit_source(root), [])

    def test_repository_tracked_source_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_source(root), [])

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
    def test_removed_mechanisms_are_findings(self):
        with fixture() as root:
            blocked = (b"spending_grants\nexceptional_grants\nquota_fresh\n"
                       b"pod-quota\nconfig approve\npod-context/v3\n")
            tracked(root, {"notes.txt": blocked})
            findings = audit_source(root)
            self.assertEqual(len(findings), 1)
            self.assertIn("unsupported mechanism", findings[0])

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
                "docs/pod-install.md": b"Publish " + b"Pod with gh release create v0.5.0\n",
                "docs/gate.md": b"Pod " + b"release-gate reports technical readiness.\n",
                "docs/gate2.md": b"The Pod " + b"release_gate is authorized.\n",
                "docs/tags.md": b"Every " + b"Pod release gets a tag.\n",
                "tests/fixtures/capture.json": b'{"note":"legacy migration"}\n',
                "tests/fixtures/sanitized.txt": b"pod-" + b"release-gate/v1\n",
                "tests/fixtures/state.txt": b"pod-" + b"context/v1\n",
            })
            findings = audit_source(root)
            self.assertEqual(sorted(row.split(" :: ")[0] for row in findings),
                             ["README.md", "docs/checks.md", "docs/gate.md", "docs/gate2.md",
                              "docs/notes.md", "docs/pod-install.md", "docs/tags.md",
                              "skills/pod/ledger.py", "tests/fixtures/sanitized.txt",
                              "tests/fixtures/state.txt"])
            self.assertFalse(any("legacy_hold" in row or "docs/history" in row for row in findings))

    def test_publication_paths_are_findings_including_a_skill_setup_module(self):
        with fixture() as root:
            tracked(root, {"skills/pod/setup.py": b"# project enrollment\n"})
            self.assertTrue(any(row.startswith("skills/pod/setup.py") for row in audit_source(root)))
            for name in ("release/NOTES.md", "CHANGELOG.md", "pyproject.toml", "install.py",
                         "skills/pod/release.py"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"x\n")
                subprocess.run(["git", "-C", str(root), "add", name], check=True)
            flagged = sorted(row.split(" :: ")[0] for row in audit_source(root))
            self.assertEqual(flagged, ["CHANGELOG.md", "install.py", "pyproject.toml",
                                       "release/NOTES.md", "skills/pod/release.py",
                                       "skills/pod/setup.py"])

    def test_a_governed_project_release_remains_ordinary_vocabulary(self):
        """Pod governs a project's own release and deploy steps; that is not Pod publishing."""
        with fixture() as root:
            tracked(root, {"skills/pod/references/governor.md":
                           b"Merge, release and deployment need an owner authorization.\n"
                           b"A release candidate of the governed project is prepared first.\n"
                           b"The project's CHANGELOG and GitHub Release belong to that project.\n"
                           b"The governed project uses a release-gate for its own deployment.\n"
                           b"The project then runs gh release create v2.0.0 and git tag v2.0.0.\n"
                           b"Orca releases the worker once Pod has preserved its report.\n",
                           "tests/fixtures/orca-1.4.209/capture.txt":
                           b"gh release create v1.0.0 for the governed project\n"})
            self.assertEqual(audit_source(root), [])

    def test_pod_automation_cannot_build_tag_or_release(self):
        """Pod's own workflows and tools never build, tag or release, whatever the wording."""
        with fixture() as root:
            tracked(root, {
                ".github/workflows/checks.yml": b"steps:\n  - run: python -m " + b"build\n  - run: gh " + b"release create v2.0 dist/*\n",
                "tools/publish.py": b"subprocess.run(['gh', 'release', 'create'])\nrun('gh " + b"release create v2')\n",
                "dist/pod-2.0-py3-none-any.whl": b"PK",
                ".github/workflows/release.yml": b"name: checks\n",
                "pod-0.4.0.tar.gz": b"x",
            })
            flagged = sorted(row.split(" :: ")[0] for row in audit_source(root))
            self.assertEqual(flagged, [".github/workflows/checks.yml", ".github/workflows/release.yml",
                                       "dist/pod-2.0-py3-none-any.whl", "pod-0.4.0.tar.gz",
                                       "tools/publish.py"])

    def test_pinned_sources_and_passive_pod_publication_prose_are_findings(self):
        with fixture() as root:
            tracked(root, {
                "a.md": b"Pod is " + b"released as a versioned archive.\n",
                "b.md": b"Pod is " + b"published to PyPI.\n",
                "c.md": b"Pod " + b"v2.0 is tagged and published.\n",
                "d.md": b"Install from https://github.com/j3w1/pod/" + b"tree/v2.0.\n",
                "tests/test_source_audit.py.backup": b"Pod " + b"v2.0 is out\n",
            })
            self.assertEqual(sorted(row.split(" :: ")[0] for row in audit_source(root)),
                             ["a.md", "b.md", "c.md", "d.md", "tests/test_source_audit.py.backup"])

    def test_ordinary_current_wording_is_allowed(self):
        with fixture() as root:
            tracked(root, {"README.md": b"Pod tags the objective with its source digest.\n"
                                        b"Orca orchestrates native workers.\n"
                                        b"Pod can coordinate a project migration.\n"
                                        b"Orca releases the worker once its report is preserved.\n"
                                        b"See https://github.com/j3w1/pod and its README.\n",
                           "skills/pod/references/governor.md":
                           b"The governed project runs gh release create v2.0.0 and git tag v2.0.0.\n"})
            self.assertEqual(audit_source(root), [])

    def test_repository_tracked_source_is_clean(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_source(root), [])

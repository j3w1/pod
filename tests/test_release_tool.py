import json
from pathlib import Path
import subprocess
import sys
import unittest

from pod.release import DELEGATION_CHECKS, LIVE_CHECKS, REQUIRED_GATES
from tests.common import fixture
from tools.release import (BLOCKED, check, gate, notes, product_tree, release_relevant,
                           version_at)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = "# Pod 1.2.0\n\n### Detail\n\n- shipped\n"


def git(root, *args, **kwargs):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, **kwargs).stdout.strip()


def repo(root, version="1.0.0"):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "fixture")
    (root / "skills" / "pod").mkdir(parents=True)
    (root / "release").mkdir()
    write(root, version, "product one\n")
    git(root, "add", "."); git(root, "commit", "-q", "-m", "base")


def write(root, version, product, notes_text=None):
    (root / "skills" / "pod" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "skills" / "pod" / "cli.py").write_text(product)
    (root / "release" / "NOTES.md").write_text(notes_text or f"# Pod {version}\n\n- notes\n")


def evidence(candidate, tree, version, *, gates=REQUIRED_GATES, authorized=True):
    records = []
    for name in gates:
        row = {"schema": "pod-validation/v1", "candidate": candidate, "tree": tree,
               "host": "Linux", "utc": "2026-09-23T00:00:00Z", "gate": name,
               "command": "bounded check", "outcome": "PASS",
               "report": f"reports/{name}.txt sha256:{'4' * 64}"}
        if name.startswith("live_"):
            row["checks"] = {item: "PASS" for item in LIVE_CHECKS}
        if name.startswith("orca_delegation_"):
            row["checks"] = {item: "PASS" for item in DELEGATION_CHECKS}
        records.append(row)
    authorization = {"schema": "pod-release-authorization/v1", "candidate": candidate,
                     "tree": tree, "scope": ["release"], "authorized_by": "owner",
                     "utc": "2026-09-23T00:00:00Z", "reference": "owner decision"} if authorized else None
    return {"schema": "pod-release-evidence/v1", "version": version, "candidate": candidate,
            "tree": tree, "records": records, "authorization": authorization}


class NotesAndPathsTests(unittest.TestCase):
    def test_notes_require_the_heading_for_this_version(self):
        self.assertEqual(notes(SAMPLE, "1.2.0"), "### Detail\n\n- shipped\n")
        self.assertEqual(notes(SAMPLE, "1.2"), "")
        self.assertEqual(notes("# Pod 1.2.0\n\n\n", "1.2.0"), "")

    def test_product_paths_are_the_ones_that_ship(self):
        changed = ["README.md", "docs/pod-spec.md", "tests/test_x.py", "tools/release.py",
                   "skills/pod/cli.py", "skills/pod/references/routing.md", "pyproject.toml",
                   "install.py", "MANIFEST.in", ".github/workflows/ci.yml", "release/NOTES.md"]
        self.assertEqual(release_relevant(changed),
                         ["MANIFEST.in", "install.py", "pyproject.toml", "skills/pod/cli.py",
                          "skills/pod/references/routing.md"])

    def test_the_repository_notes_describe_the_declared_version(self):
        from pod import __version__
        self.assertEqual(version_at(ROOT), __version__)
        completed = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "notes", __version__],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip())
        missing = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "notes", "0.0.0"],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 1)


class PullRequestCheckTests(unittest.TestCase):
    def test_product_changes_need_a_bump_and_current_notes(self):
        with fixture() as root:
            repo(root)
            base = git(root, "rev-parse", "HEAD")
            write(root, "1.0.0", "product two\n")
            git(root, "commit", "-qam", "change product")
            problems = check(root, base)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("without a version bump", problems[0])
            self.assertIn("skills/pod/cli.py", problems[0])
            write(root, "1.1.0", "product two\n", notes_text="# Pod 1.0.0\n\n- stale\n")
            git(root, "commit", "-qam", "bump without notes")
            problems = check(root, base)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("# Pod 1.1.0", problems[0])
            write(root, "1.1.0", "product two\n")
            git(root, "commit", "-qam", "notes")
            self.assertEqual(check(root, base), [])

    def test_non_product_changes_merge_without_a_bump(self):
        with fixture() as root:
            repo(root)
            base = git(root, "rev-parse", "HEAD")
            (root / "README.md").write_text("docs only\n")
            git(root, "add", "."); git(root, "commit", "-qm", "docs")
            self.assertEqual(check(root, base), [])
            (root / "release" / "evidence.json").write_text(json.dumps({"schema": "other"}))
            git(root, "add", "."); git(root, "commit", "-qm", "bad evidence")
            problems = check(root, base)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("evidence.json", problems[0])


class PublishGateTests(unittest.TestCase):
    def test_evidence_publishes_only_the_content_it_binds(self):
        with fixture() as root:
            repo(root)
            publish, reason, decision = gate(root)
            self.assertFalse(publish)
            self.assertIn("no release/evidence.json", reason)
            candidate = git(root, "rev-parse", "HEAD")
            tree = product_tree(root)
            # With no evidence record yet, the releasable tree is the whole commit tree.
            self.assertEqual(tree, git(root, "rev-parse", "HEAD^{tree}"))
            (root / "release" / "evidence.json").write_text(json.dumps(evidence(candidate, tree, "1.0.0")))
            git(root, "add", "."); git(root, "commit", "-qm", "evidence")
            # The evidence commit adds only the evidence record, so the bound tree still holds.
            self.assertEqual(product_tree(root), tree)
            publish, reason, decision = gate(root)
            self.assertTrue(publish, reason)
            self.assertEqual(decision["status"], "authorized")
            # A later product change breaks the binding: merged, but not publishable.
            (root / "skills" / "pod" / "cli.py").write_text("product three\n")
            git(root, "commit", "-qam", "drift")
            publish, reason, _ = gate(root)
            self.assertFalse(publish)
            self.assertIn("releasable tree", reason)

    def test_notes_are_bound_and_only_the_evidence_record_is_not(self):
        with fixture() as root:
            repo(root)
            candidate = git(root, "rev-parse", "HEAD")
            tree = product_tree(root)
            (root / "release" / "evidence.json").write_text(json.dumps(evidence(candidate, tree, "1.0.0")))
            git(root, "add", "."); git(root, "commit", "-qm", "evidence")
            self.assertTrue(gate(root)[0])
            # Rewriting only the evidence record leaves the bound content intact.
            record = evidence(candidate, tree, "1.0.0")
            record["records"][0]["command"] = "reworded bounded check"
            (root / "release" / "evidence.json").write_text(json.dumps(record))
            git(root, "commit", "-qam", "evidence only")
            self.assertEqual(product_tree(root), tree)
            self.assertTrue(gate(root)[0])
            # Changing the approved release notes after approval blocks publication.
            (root / "release" / "NOTES.md").write_text("# Pod 1.0.0\n\n- different notes\n")
            git(root, "commit", "-qam", "edit notes")
            self.assertNotEqual(product_tree(root), tree)
            publish, reason, _ = gate(root)
            self.assertFalse(publish)
            self.assertIn("releasable tree", reason)
            # So does adding any other file under release/.
            (root / "release" / "NOTES.md").write_text("# Pod 1.0.0\n\n- notes\n")
            (root / "release" / "extra.md").write_text("extra\n")
            git(root, "add", "."); git(root, "commit", "-qm", "extra")
            self.assertFalse(gate(root)[0])

    def test_incomplete_or_unauthorized_evidence_blocks_without_error(self):
        with fixture() as root:
            repo(root)
            candidate = git(root, "rev-parse", "HEAD")
            tree = product_tree(root)
            partial = evidence(candidate, tree, "1.0.0",
                               gates=[g for g in REQUIRED_GATES if not g.startswith("live_")])
            (root / "release" / "evidence.json").write_text(json.dumps(partial))
            git(root, "add", "."); git(root, "commit", "-qm", "partial")
            publish, reason, decision = gate(root)
            self.assertFalse(publish)
            self.assertIn("live_codex_linux", reason)
            self.assertEqual(decision["status"], "blocked")
            (root / "release" / "evidence.json").write_text(
                json.dumps(evidence(candidate, tree, "1.0.0", authorized=False)))
            git(root, "commit", "-qam", "unauthorized")
            publish, reason, decision = gate(root)
            self.assertFalse(publish)
            self.assertEqual(decision["status"], "owner_decision_required")
            (root / "release" / "evidence.json").write_text(
                json.dumps(evidence(candidate, tree, "0.9.0")))
            git(root, "commit", "-qam", "wrong version")
            with self.assertRaises(ValueError):
                gate(root)
            self.assertEqual(subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "gate"],
                                            cwd=ROOT, capture_output=True).returncode, BLOCKED)

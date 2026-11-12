"""The hosted workflow's shape, and j3w1's Python decision: one latest stable CPython per run."""

import re
import unittest

from tests.support import ROOT

TEXT = (ROOT / ".github/workflows/ci.yml").read_text()


class WorkflowTests(unittest.TestCase):
    def test_the_hosted_workflow_resolves_one_stable_python_once_and_runs_every_job_on_it(self):
        # Resolved once, from the stable selector, never a prerelease or a pinned minor.
        self.assertEqual(TEXT.count('python-version: "3.x"'), 1)
        self.assertEqual(TEXT.count("check-latest: true"), 1)
        self.assertIn("allow-prereleases: false", TEXT)
        self.assertEqual(re.findall(r'python-version: (.*)', TEXT),
                         ['"3.x"'] + ["${{ needs.python.outputs.version }}"] * 3)
        self.assertNotRegex(TEXT, r"matrix\.python|python: \"")
        self.assertNotRegex(TEXT, r"3\.1\d")
        # Every job needs the resolution job and proves it runs the resolved interpreter.
        self.assertEqual(TEXT.count("needs: python"), 3)
        self.assertEqual(TEXT.count("The resolved interpreter is the one running"), 3)

    def test_three_plain_jobs_stand_behind_the_one_required_check(self):
        self.assertIn("needs: [python, core, models, install]", TEXT)
        self.assertEqual(re.findall(r"python -m unittest discover -s (\S+) -t \.", TEXT),
                         ["tests/core", "tests/models", "tests/install"])
        self.assertNotRegex(TEXT, r"matrix|shard|bubblewrap|suite_lanes|paths-ignore|paths:")
        for job in ("core", "models", "install"):
            self.assertIn(f'test "${{{{ needs.{job}.result }}}}" = success', TEXT)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", TEXT)

    def test_every_test_directory_belongs_to_a_job(self):
        found = sorted(path.name for path in (ROOT / "tests").iterdir() if path.is_dir() and any(path.glob("test_*.py")))
        self.assertEqual(found, ["core", "install", "models"])

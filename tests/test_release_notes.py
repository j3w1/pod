from pathlib import Path
import subprocess
import sys
import unittest

from tools.release_notes import section

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = """# Changelog

Intro line.

## 1.2.0 — 2026-09-30

- newest

### Detail

- nested heading stays inside the section

## 1.1.0 — 2026-09-21

- older
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_a_section_runs_to_the_next_top_level_heading(self):
        self.assertEqual(section(SAMPLE, "1.2.0"),
                         "- newest\n\n### Detail\n\n- nested heading stays inside the section\n")
        self.assertEqual(section(SAMPLE, "1.1.0"), "- older\n")

    def test_a_missing_or_empty_section_is_nothing(self):
        self.assertEqual(section(SAMPLE, "1.2"), "")
        self.assertEqual(section(SAMPLE, "1.2.0-rc1"), "")
        self.assertEqual(section("## 2.0.0 — 2026-10-01\n\n## 1.9.0 — x\n- y\n", "2.0.0"), "")

    def test_the_command_fails_for_an_unlisted_version(self):
        missing = subprocess.run([sys.executable, str(ROOT / "tools" / "release_notes.py"), "0.0.0"],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no section for 0.0.0", missing.stderr)
        from pod import __version__
        current = subprocess.run([sys.executable, str(ROOT / "tools" / "release_notes.py"), __version__],
                                 capture_output=True, text=True)
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertTrue(current.stdout.strip())

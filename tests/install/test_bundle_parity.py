from pathlib import Path
import subprocess
import os
import unittest

from pod.bundle import BUNDLE_FILES, bundle_root, canonical, frontmatter, version

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "skills" / "pod"


class BundleParityTests(unittest.TestCase):
    def test_the_package_directory_is_the_bundle(self):
        self.assertEqual(bundle_root().resolve(), BUNDLE.resolve())

    def test_tracked_files_match_the_declared_inventory(self):
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "skills/pod"],
                                capture_output=True, text=True, check=True)
        tracked = {line[len("skills/pod/"):] for line in listed.stdout.splitlines() if line.strip()}
        self.assertEqual(tracked, set(BUNDLE_FILES))

    def test_one_authored_version(self):
        """Root VERSION is authored; the bundle links to it, so the two cannot drift."""
        text = (ROOT / "VERSION").read_text(encoding="ascii")
        self.assertRegex(text, r"\A\d+\.\d+\.\d+\n\Z")
        link = BUNDLE / "VERSION"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../../VERSION")
        self.assertEqual(version(), text.strip())
        self.assertNotIn("version", frontmatter((BUNDLE / "SKILL.md").read_text(encoding="utf-8"))
                         ["metadata"]["metadata"])
        listed = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "-E",
                                 r"__version__ = \"[0-9]|^version = |(^|[[:space:]])version: \"?[0-9]",
                                 "--", ".", ":!tests"],
                                capture_output=True, text=True)
        self.assertEqual(listed.stdout, "", "a second authored version appeared")

    def test_canonical_bytes_come_from_the_tracked_tree(self):
        source = canonical()
        self.assertEqual(set(source), set(BUNDLE_FILES))
        for name, data in source.items():
            self.assertEqual(data, (BUNDLE / name).read_bytes())

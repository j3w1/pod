"""One bundle, derived from the package directory, with one authored version."""

import os
import subprocess
import unittest

from pod import bundle, installer
from pod.errors import PodError
from tests.install.install_support import BUNDLE, ROOT
from tests.support import PodCase


class BundleTests(PodCase):
    def test_the_package_directory_is_the_bundle(self):
        self.assertEqual(bundle.bundle_root().resolve(), BUNDLE.resolve())
        self.assertEqual(bundle.digest(), installer.bundle_digest(BUNDLE))

    def test_the_inventory_is_read_from_the_directory_and_follows_what_is_added(self):
        root = self.tmp / "pod"
        for name, data in (("SKILL.md", "text"), ("VERSION", "1.2.3\n"), ("cli.py", ""), ("models/registry.json", "{}"),
                           ("agents/openai.yaml", ""), ("__pycache__/cli.cpython-314.pyc", "cache"), ("notes.txt", "")):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(data)
        self.assertEqual(bundle.files(root), ["SKILL.md", "VERSION", "agents/openai.yaml", "cli.py", "models/registry.json"])
        before = bundle.digest(root)
        (root / "models/added.py").write_text("ADDED = True\n")
        self.assertIn("models/added.py", bundle.files(root))
        self.assertNotEqual(bundle.digest(root), before)
        (root / "models/added.py").unlink()
        (root / "__pycache__/other.pyc").write_text("more cache")
        self.assertEqual(bundle.digest(root), before)
        (root / "VERSION").unlink()
        self.assertIsNone(bundle.digest(root))

    def test_one_authored_version(self):
        """The root VERSION is authored; the bundle links to it, so the two cannot drift."""
        text = (ROOT / "VERSION").read_text(encoding="ascii")
        self.assertRegex(text, r"\A\d+\.\d+\.\d+\n\Z")
        self.assertEqual(os.readlink(BUNDLE / "VERSION"), "../../VERSION")
        self.assertEqual(bundle.version(), text.strip())
        listed = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "-E",
                                 r"__version__ = \"[0-9]|^version = |(^|[[:space:]])version: \"?[0-9]",
                                 "--", ".", ":!tests"], capture_output=True, text=True)
        self.assertEqual(listed.stdout, "", "a second authored version appeared")

    def test_front_matter_is_plain_values_and_one_level_mappings(self):
        parsed = bundle.frontmatter("---\nname: pod\nmetadata:\n  source: https://example.invalid/x\n---\n# Body\n")
        self.assertEqual(parsed, {"metadata": {"name": "pod", "metadata": {"source": "https://example.invalid/x"}},
                                  "body": "# Body\n"})
        for text in ("# no front matter\n", "---\nname: pod\n", "---\ncompatibility: |\n  folded\n---\nbody\n",
                     "---\nname: [a, b]\n---\nbody\n"):
            with self.subTest(text=text), self.assertRaises(PodError):
                bundle.frontmatter(text)


if __name__ == "__main__":
    unittest.main()

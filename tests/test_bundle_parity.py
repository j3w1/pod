from pathlib import Path
import subprocess
import tomllib
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

    def test_one_version_across_package_skill_and_project_metadata(self):
        metadata = frontmatter((BUNDLE / "SKILL.md").read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(metadata["metadata"]["version"], version())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"],
                         {"attr": "pod.__version__"})
        self.assertEqual(project["tool"]["setuptools"]["package-dir"], {"pod": "skills/pod"})

    def test_canonical_bytes_come_from_the_tracked_tree(self):
        source = canonical()
        self.assertEqual(set(source), set(BUNDLE_FILES))
        for name, data in source.items():
            self.assertEqual(data, (BUNDLE / name).read_bytes())

    def test_the_distribution_targets_linux_only(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        classifiers = project["project"]["classifiers"]
        operating_systems = [row for row in classifiers if row.startswith("Operating System ::")]
        self.assertEqual(operating_systems, ["Operating System :: POSIX :: Linux"])

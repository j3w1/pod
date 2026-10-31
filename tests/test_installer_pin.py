"""The pinned PyYAML installs from a wheel; only without a wheel does it build its hash-pinned source archive."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod import installer
from pod.installer import InstallError

# PyPI's JSON for PyYAML 6.0.3 lists this digest for pyyaml-6.0.3.tar.gz.
SDIST_SHA256 = "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
NO_WHEEL = "ERROR: Could not find a version that satisfies the requirement PyYAML==6.0.3\nERROR: No matching distribution found for PyYAML==6.0.3\n"


class Pip:
    """Records the pip invocations and the requirements file each one read."""

    def __init__(self, *outcomes):
        self.outcomes, self.commands, self.requirements = list(outcomes), [], []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if "-r" in command:
            self.requirements.append(Path(command[command.index("-r") + 1]).read_text())
        code, stderr = self.outcomes.pop(0)
        return subprocess.CompletedProcess(command, code, "", stderr)


class PinnedPyYamlTests(unittest.TestCase):
    def install(self, *outcomes):
        pip = Pip(*outcomes)
        with patch.object(installer.subprocess, "run", pip):
            try:
                installer._install_pin(Path("/venv/bin/python"))
            finally:
                self.pip = pip
        return pip

    def test_the_pinned_source_digest_is_the_published_one_for_exactly_this_pin(self):
        self.assertEqual(installer.PIN, "PyYAML==6.0.3")
        self.assertEqual(installer.PIN_SOURCE_SHA256, {"PyYAML==6.0.3": SDIST_SHA256})

    def test_a_prebuilt_wheel_keeps_the_wheel_only_path_unchanged(self):
        pip = self.install((0, ""))
        self.assertEqual(len(pip.commands), 1)
        self.assertEqual(pip.commands[0][-2:], ["--only-binary=:all:", "PyYAML==6.0.3"])
        self.assertNotIn("--no-binary", pip.commands[0])

    def test_without_a_wheel_it_builds_exactly_the_hash_pinned_archive(self):
        pip = self.install((1, NO_WHEEL), (0, ""))
        self.assertEqual(len(pip.commands), 2)
        build = pip.commands[1]
        self.assertIn("--require-hashes", build)
        self.assertEqual(build[build.index("--no-binary") + 1], "PyYAML")
        self.assertNotIn("--only-binary=:all:", build)
        self.assertEqual(pip.requirements, [f"PyYAML==6.0.3 --hash=sha256:{SDIST_SHA256}\n"])

    def test_a_wrong_hash_or_a_failed_build_refuses_with_a_clear_message(self):
        with self.assertRaisesRegex(InstallError, "did not match its pinned hash"):
            self.install((1, NO_WHEEL), (1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."))
        with self.assertRaisesRegex(InstallError, "source build failed"):
            self.install((1, NO_WHEEL), (1, "error: command 'gcc' failed"))

    def test_a_network_failure_or_another_pin_never_tries_a_source_build(self):
        with self.assertRaisesRegex(InstallError, "check network or wheel source"):
            self.install((1, "WARNING: Retrying (Retry(total=4)) after connection broken"))
        self.assertEqual(len(self.pip.commands), 1)
        with patch.object(installer, "PIN", "PyYAML==6.0.2"), \
                self.assertRaisesRegex(InstallError, "check network or wheel source"):
            self.install((1, NO_WHEEL))
        self.assertEqual(len(self.pip.commands), 1)


if __name__ == "__main__":
    unittest.main()

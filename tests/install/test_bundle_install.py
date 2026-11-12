"""A copied bundle runs from anywhere through its own launcher script, with no checkout and no package on the path."""

import json
import os
from pathlib import Path
import subprocess
import sys

from tests.install.install_support import VERSION, fixture_skill
from tests.support import PodCase


class CopiedBundleTests(PodCase):
    def setUp(self):
        super().setUp()
        self.skill = fixture_skill(self.tmp / "installed")
        self.work = self.tmp / "unrelated"
        self.work.mkdir()

    def env(self):
        env = {name: os.environ[name] for name in ("PATH", "HOME", "POD_STATE_HOME", "POD_CONFIG_HOME", "POD_CACHE_HOME")}
        return {**env, "XDG_DATA_HOME": str(self.tmp / "data")}

    def run_script(self, *args):
        env = self.env()
        return subprocess.run([sys.executable, "-I", str(self.skill / "scripts/pod.py"), *args], env=env, cwd=self.work,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def probe(self, program):
        """Load the launcher script as a module in a fresh interpreter and report one JSON value."""
        source = ("import importlib.util, json, sys\n"
                  "spec = importlib.util.spec_from_file_location('launcher', sys.argv[1])\n"
                  "launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)\n" + program)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(self.skill / "scripts/pod.py"),
                                 str(self.skill)], env=self.env(), cwd=self.work, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_version_needs_neither_a_checkout_nor_the_package(self):
        (self.skill / "cli.py").unlink(missing_ok=True)
        result = self.run_script("--version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, VERSION), result.stderr)

    def test_a_prerelease_interpreter_is_refused_before_the_package_is_imported(self):
        observed = self.probe("failure = launcher.preflight(version_info=(9, 9, 0, 'beta', 1), bundle=sys.argv[2])\n"
                              "print(json.dumps({'failure': failure, 'imported': 'pod' in sys.modules}))\n")
        self.assertEqual(observed["failure"]["code"], "python_not_stable")
        self.assertIn("9.9.0-beta", observed["failure"]["message"])
        self.assertFalse(observed["imported"])

    def test_a_missing_entry_file_or_deeper_module_is_a_clean_refusal_never_a_traceback(self):
        (self.skill / "cli.py").write_text("from . import absent_module\n")
        for remove in (None, "errors.py"):
            if remove:
                (self.skill / remove).unlink()
            for arguments, stream in ((("doctor", "--json"), "stdout"), (("doctor",), "stderr")):
                blocked = self.run_script(*arguments)
                self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
                self.assertNotIn("Traceback", blocked.stderr)
                self.assertIn("/main/install.sh", getattr(blocked, stream))
                if stream == "stdout":
                    self.assertEqual(json.loads(blocked.stdout)["error"]["code"], "bundle_incomplete")

    def test_the_bundle_wins_over_an_unrelated_importable_package(self):
        imposter = self.work / "pod"
        imposter.mkdir()
        (imposter / "__init__.py").write_text("raise RuntimeError('wrong package')\n")
        observed = self.probe("sys.path.insert(0, '.')\npod = launcher.load_pod(sys.argv[2])\n"
                              "from pod import maintenance\n"
                              "print(json.dumps([pod.__file__, maintenance.doctor()['bundle']['path']]))\n")
        self.assertEqual(observed, [str(self.skill / "__init__.py"), str(self.skill)])

    def test_a_copy_placed_by_hand_reports_that_no_installer_placed_it(self):
        observed = self.probe("launcher.load_pod(sys.argv[2])\nfrom pod import maintenance\n"
                              "print(json.dumps(maintenance.doctor()))\n")
        self.assertEqual(observed["version"], VERSION)
        self.assertEqual(Path(observed["bundle"]["path"]), self.skill)
        self.assertEqual(observed["installation"]["status"], "not installed by the one-shot installer")
        self.assertIsNone(observed["installation"]["healthy"])

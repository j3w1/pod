import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from pod.bundle import bundle_root
from tests.common import fixture

BUNDLE = bundle_root()
LAUNCHER = ("pod", "scripts", "pod.py")


def copied(target: Path) -> Path:
    """Place the bundle the way an installer would: a plain copy, nothing linked."""
    destination = target / "pod"
    shutil.copytree(BUNDLE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def environment(home: Path, **extra) -> dict:
    """A clean environment: no PYTHONPATH, no developer checkout, isolated homes."""
    base = {key: value for key, value in os.environ.items()
            if key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
    base.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                 "XDG_STATE_HOME": str(home / "state"), "CODEX_HOME": str(home / "agents"),
                 "CLAUDE_CONFIG_DIR": str(home / "claude")})
    base.update(extra)
    return base


def launch(skill: Path, arguments, *, home: Path, cwd: Path, isolated=True, env_extra=None):
    flags = ["-I"] if isolated else ["-s", "-P"]
    return subprocess.run([sys.executable, *flags, str(skill / "scripts" / "pod.py"), *arguments],
                          capture_output=True, text=True, cwd=str(cwd), timeout=120,
                          env=environment(home, **(env_extra or {})))


def pyyaml_visible() -> bool:
    probe = subprocess.run([sys.executable, "-I", "-c", "import yaml"], capture_output=True)
    return probe.returncode == 0


class BundleInstallTests(unittest.TestCase):
    def test_a_copied_bundle_runs_with_no_checkout_and_no_pythonpath(self):
        if not pyyaml_visible():
            self.skipTest("PyYAML is not importable under an isolated interpreter here")
        with fixture() as root:
            skill = copied(root / "installed")
            home = root / "home-a"
            work = root / "elsewhere"
            for path in (home, work):
                path.mkdir(parents=True)
            helped = launch(skill, ["--help"], home=home, cwd=work)
            self.assertEqual(helped.returncode, 0, helped.stderr)
            for family in ("setup", "config", "doctor", "status"):
                self.assertIn(family, helped.stdout)
            self.assertNotIn("internal", helped.stdout)
            doctor = launch(skill, ["doctor", "--json"], home=home, cwd=work)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            report = json.loads(doctor.stdout)
            self.assertTrue(report["bundle"]["path"].startswith(str(skill)))
            self.assertEqual(report["prerequisites"]["python"], "ok")
            checked = launch(skill, ["config", "--check", "--json"], home=home, cwd=work)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_private_operations_stay_reachable_only_through_the_bundle(self):
        if not pyyaml_visible():
            self.skipTest("PyYAML is not importable under an isolated interpreter here")
        with fixture() as root:
            skill = copied(root / "installed")
            home = root / "home-b"
            work = root / "work"
            for path in (home, work):
                path.mkdir(parents=True)
            request = work / "brief.json"
            request.write_text(json.dumps({"criteria": ["works"],
                                           "coverage": [{"criterion": "works",
                                                         "check": "unit suite"}]}))
            helper = launch(skill, ["internal", "brief", "--input", str(request)],
                            home=home, cwd=work)
            self.assertEqual(helper.returncode, 0, helper.stderr)
            self.assertEqual(json.loads(helper.stdout)["schema"], "pod-helper/v1")
            unknown = launch(skill, ["internal-preview"], home=home, cwd=work)
            self.assertNotEqual(unknown.returncode, 0)

    def test_missing_pyyaml_names_the_one_bootstrap_step(self):
        with fixture() as root:
            skill = copied(root / "installed")
            home = root / "home-c"
            work = root / "work"
            shadow = root / "shadow"
            (shadow / "yaml").mkdir(parents=True)
            (shadow / "yaml" / "__init__.py").write_text("raise ImportError('simulated')\n")
            for path in (home, work):
                path.mkdir(parents=True)
            blocked = launch(skill, ["doctor", "--json"], home=home, cwd=work, isolated=False,
                             env_extra={"PYTHONPATH": str(shadow)})
            self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
            report = json.loads(blocked.stdout)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["error"]["code"], "pyyaml_missing")
            self.assertIn("pip install --user 'PyYAML", report["error"]["message"])
            self.assertNotIn("Traceback", blocked.stderr)

    def test_an_old_interpreter_is_reported_without_importing_the_package(self):
        with fixture() as root:
            skill = copied(root / "installed")
            probe = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import importlib.util,json,sys\n"
                 "spec=importlib.util.spec_from_file_location('podlauncher', sys.argv[1])\n"
                 "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                 "failure=module.preflight(version_info=(3,12,0), bundle=sys.argv[2])\n"
                 "print(json.dumps({'failure': failure, 'imported': 'pod' in sys.modules}))\n",
                 str(skill / "scripts" / "pod.py"), str(skill)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            observed = json.loads(probe.stdout)
            self.assertEqual(observed["failure"]["code"], "python_too_old")
            self.assertIn("3.12.0", observed["failure"]["message"])
            self.assertFalse(observed["imported"])

    def test_an_incomplete_bundle_says_how_to_reinstall(self):
        with fixture() as root:
            skill = copied(root / "installed")
            (skill / "cli.py").unlink()
            home = root / "home-d"
            work = root / "work"
            for path in (home, work):
                path.mkdir(parents=True)
            broken = launch(skill, ["doctor", "--json"], home=home, cwd=work)
            self.assertEqual(broken.returncode, 2)
            report = json.loads(broken.stdout)
            self.assertEqual(report["error"]["code"], "bundle_incomplete")
            self.assertIn("skills add j3w1/pod", report["error"]["message"])

    def test_the_bundle_wins_over_an_unrelated_installed_package(self):
        if not pyyaml_visible():
            self.skipTest("PyYAML is not importable under an isolated interpreter here")
        with fixture() as root:
            skill = copied(root / "installed")
            home = root / "home-e"
            work = root / "work"
            decoy = root / "decoy"
            (decoy / "pod").mkdir(parents=True)
            (decoy / "pod" / "__init__.py").write_text("raise SystemExit('decoy package used')\n")
            for path in (home, work):
                path.mkdir(parents=True)
            report = launch(skill, ["doctor", "--json"], home=home, cwd=work, isolated=False,
                            env_extra={"PYTHONPATH": str(decoy)})
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
            self.assertTrue(json.loads(report.stdout)["bundle"]["path"].startswith(str(skill)))

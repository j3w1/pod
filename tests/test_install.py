from importlib import metadata
import os
import re
import subprocess
import unittest
from unittest.mock import patch

import install
from tests.common import fixture


class IsolatedInstallTests(unittest.TestCase):
    def test_reviewed_clean_checkout_and_selected_environment(self):
        with fixture() as root:
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text("[project]\n")
            outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "pip 26.2.1 from somewhere\n", "")]
            with patch("install.subprocess.run", side_effect=outputs) as runner:
                result = install.plan(checkout, root / "selected environment", "abcdef1")
            self.assertEqual(result["checkout_commit"], "abcdef123456")
            python = root / "selected environment" / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            self.assertEqual(result["install_command"], [install.sys.executable, "-I", "-m", "pip",
                                                         "--python", str(python), "--isolated", "install",
                                                         "--no-input", "--no-warn-script-location", str(checkout)])
            self.assertEqual(result["pip_version"], (26, 2, 1))
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(
                runner.call_args_list[2].args[0],
                [install.sys.executable, "-I", "-m", "pip", "--isolated", "--version"],
            )
            self.assertEqual(runner.call_args_list[2].kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
            self.assertEqual(
                result["verification_command"],
                [str(python), "-I", "-c", "from importlib.metadata import distribution; "
                 "distribution('j3w1-pod'); import pod"],
            )
            with self.assertRaises(RuntimeError):
                install.plan(checkout, checkout / "venv", "abcdef1")

    def test_dirty_checkout_refused_without_install_effect(self):
        with fixture() as root:
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text("[project]\n")
            outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                       subprocess.CompletedProcess([], 0, " M src/file.py\n", "")]
            with patch("install.subprocess.run", side_effect=outputs):
                with self.assertRaises(RuntimeError):
                    install.plan(checkout, root / "venv", "abcdef1")

    def test_missing_or_old_bootstrap_pip_fails_before_target_creation(self):
        for pip_result, message in (
            (subprocess.CalledProcessError(1, [], stderr="No module named pip"),
             "Bootstrap pip is unavailable; install pip 22.3 or newer"),
            (subprocess.CompletedProcess([], 0, "pip 22.2.2 from somewhere\n", ""), "pip 22.2.2 is unsupported"),
        ):
            with self.subTest(message=message), fixture() as root:
                checkout = root / "checkout"
                checkout.mkdir()
                (checkout / "pyproject.toml").write_text("[project]\n")
                outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                           subprocess.CompletedProcess([], 0, "", ""), pip_result]
                with patch("install.subprocess.run", side_effect=outputs), \
                     patch("install.venv.EnvBuilder") as builder:
                    with self.assertRaisesRegex(RuntimeError, message):
                        install.execute(checkout, root / "venv", "abcdef1")
                builder.assert_not_called()
                self.assertFalse((root / "venv").exists())

    def test_bootstrap_preflight_ignores_disposable_user_pip_redirect(self):
        with fixture() as root:
            configured_python = root / "configured-python-must-not-run"
            if install.sys.platform == "win32":
                config_home = root / "appdata"
                config_file = config_home / "pip" / "pip.ini"
                user_environment = {"APPDATA": str(config_home)}
            else:
                config_home = root / "xdg-config"
                config_file = config_home / "pip" / "pip.conf"
                user_environment = {"XDG_CONFIG_HOME": str(config_home)}
            config_file.parent.mkdir(parents=True)
            config_file.write_text(f"[global]\npython = {configured_python}\n")

            with patch.dict(os.environ, user_environment):
                environment = install._subprocess_environment()
                unprotected = dict(environment)
                unprotected.pop("PIP_CONFIG_FILE")
                unisolated = subprocess.run(
                    [install.sys.executable, "-I", "-m", "pip", "--version"],
                    capture_output=True, text=True, env=unprotected,
                )
                self.assertNotEqual(unisolated.returncode, 0)
                self.assertIn(str(configured_python), unisolated.stdout + unisolated.stderr)
                self.assertFalse(configured_python.exists())
                actual = install._bootstrap_pip(environment)
                self.assertEqual(environment["PIP_CONFIG_FILE"], os.devnull)

            expected_match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", metadata.version("pip"))
            self.assertIsNotNone(expected_match)
            expected = tuple(int(part or 0) for part in expected_match.groups())
            self.assertEqual(actual, expected)
            self.assertFalse(configured_python.exists())

    def test_install_ignores_user_pip_version_false_success(self):
        with fixture() as root:
            if install.sys.platform == "win32":
                config_home = root / "appdata"
                config_file = config_home / "pip" / "pip.ini"
                user_environment = {"APPDATA": str(config_home)}
            else:
                config_home = root / "xdg-config"
                config_file = config_home / "pip" / "pip.conf"
                user_environment = {"XDG_CONFIG_HOME": str(config_home)}
            config_file.parent.mkdir(parents=True)
            config_file.write_text("[global]\nversion = true\n")
            target = root / "pipless-target"
            install.venv.EnvBuilder(with_pip=False).create(target)
            target_python = target / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            command = [install.sys.executable, "-I", "-m", "pip", "--python", str(target_python),
                       "--isolated", "install", str(root / "missing-package.whl")]

            with patch.dict(os.environ, user_environment):
                protected = install._subprocess_environment()
                unprotected = dict(protected)
                unprotected.pop("PIP_CONFIG_FILE")
                false_success = subprocess.run(command, capture_output=True, text=True, env=unprotected)
                actual = subprocess.run(command, capture_output=True, text=True, env=protected)

            self.assertEqual(false_success.returncode, 0)
            self.assertRegex(false_success.stdout, r"^pip \d+\.\d+")
            self.assertNotEqual(actual.returncode, 0)
            self.assertIn("missing-package.whl", actual.stdout + actual.stderr)
            self.assertEqual(protected["PIP_CONFIG_FILE"], os.devnull)

    def test_pipless_target_exact_python_and_process_isolation(self):
        with fixture() as root:
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text("[project]\n")
            target = root / "selected environment"
            target_python = target / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "pip 22.3 from somewhere\n", ""),
                       subprocess.CompletedProcess([], 0, "installed\n", ""),
                       subprocess.CompletedProcess([], 0, "", "")]

            def create(path):
                self.assertEqual(path, target)
                target_python.parent.mkdir(parents=True)
                target_python.touch()

            inherited = {"PYTHONPATH": "untrusted", "PYTHONHOME": "untrusted",
                         "PYTHONUSERBASE": "untrusted", "PIP_CONFIG_FILE": "untrusted",
                         "_PIP_RUNNING_IN_SUBPROCESS": "1"}
            with patch.dict(os.environ, inherited), \
                 patch("install.subprocess.run", side_effect=outputs) as runner, \
                 patch("install.venv.EnvBuilder") as builder:
                builder.return_value.create.side_effect = create
                result = install.execute(checkout, target, "abcdef1")

            builder.assert_called_once_with(with_pip=False, clear=False)
            self.assertEqual(result["install_command"][4:7], ["--python", str(target_python), "--isolated"])
            install_call = runner.call_args_list[-2]
            self.assertEqual(install_call.args[0], result["install_command"])
            child_environment = install_call.kwargs["env"]
            self.assertEqual(child_environment["PYTHONNOUSERSITE"], "1")
            for key in inherited:
                if key == "PIP_CONFIG_FILE":
                    self.assertEqual(child_environment[key], os.devnull)
                else:
                    self.assertNotIn(key, child_environment)
            verification_call = runner.call_args_list[-1]
            self.assertEqual(verification_call.args[0], result["verification_command"])
            self.assertEqual(verification_call.args[0][:2], [str(target_python), "-I"])
            self.assertEqual(verification_call.kwargs["env"], child_environment)

    def test_install_subprocess_failure_keeps_precise_stderr(self):
        with fixture() as root:
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text("[project]\n")
            target = root / "venv"
            target_python = target / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "pip 22.3 from somewhere\n", ""),
                       subprocess.CalledProcessError(9, [], stderr="specific pip failure")]

            def create(_path):
                target_python.parent.mkdir(parents=True)
                target_python.touch()

            with patch("install.subprocess.run", side_effect=outputs), \
                 patch("install.venv.EnvBuilder") as builder:
                builder.return_value.create.side_effect = create
                with self.assertRaisesRegex(RuntimeError, "exit 9: specific pip failure"):
                    install.execute(checkout, target, "abcdef1")

    def test_post_install_verification_failure_is_precise_and_preserves_target(self):
        with fixture() as root:
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "pyproject.toml").write_text("[project]\n")
            target = root / "venv"
            target_python = target / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            outputs = [subprocess.CompletedProcess([], 0, "abcdef123456\n", ""),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "pip 22.3 from somewhere\n", ""),
                       subprocess.CompletedProcess([], 0, "installed\n", ""),
                       subprocess.CalledProcessError(1, [], stderr="No module named 'pod'")]

            def create(_path):
                target_python.parent.mkdir(parents=True)
                target_python.touch()

            with patch("install.subprocess.run", side_effect=outputs), \
                 patch("install.venv.EnvBuilder") as builder:
                builder.return_value.create.side_effect = create
                with self.assertRaisesRegex(
                    RuntimeError, "installed Pod verification failed with exit 1: No module named 'pod'"
                ):
                    install.execute(checkout, target, "abcdef1")
            self.assertTrue(target.is_dir())
            self.assertTrue(target_python.is_file())

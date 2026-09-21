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
                       subprocess.CompletedProcess([], 0, "", "")]
            with patch("install.subprocess.run", side_effect=outputs) as runner:
                result = install.plan(checkout, root / "selected environment", "abcdef1")
            self.assertEqual(result["checkout_commit"], "abcdef123456")
            python = root / "selected environment" / ("Scripts/python.exe" if install.sys.platform == "win32" else "bin/python")
            self.assertEqual(result["install_command"], [str(python), "-I", "-m", "pip", "--isolated",
                                                         "install", "--no-input", "--no-warn-script-location",
                                                         str(checkout)])
            self.assertEqual(runner.call_count, 2)
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

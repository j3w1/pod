"""`pod doctor` reads and reports; it changes nothing and guesses nothing."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
from unittest.mock import patch

from pod import maintenance
from tests.support import PodCase


class DoctorTests(PodCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "data")})
        env.start()
        self.addCleanup(env.stop)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ZDOTDIR"):
            os.environ.pop(name, None)

    def snapshot(self):
        return sorted((str(path), path.stat().st_mtime_ns) for path in self.tmp.rglob("*"))

    def test_absent_tools_and_preferences_are_reported_as_unknown_and_nothing_is_written(self):
        before = self.snapshot()
        report = maintenance.doctor()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["tools"]["orca"], {"path": None, "version": None})
        self.assertEqual((report["preferences"]["status"], report["preferences"]["enabled"]), ("absent", 0))
        self.assertEqual(report["installation"]["status"], "not installed by the one-shot installer")
        self.assertEqual(report["locations"], {name: str(self.tmp / f"pod_{name}_home") for name in ("config", "state", "cache")})
        self.assertTrue(report["python"]["stable"])

    def test_a_tool_is_reported_with_its_own_version_line_and_a_failing_one_without(self):
        self.script("orca", "#!/bin/sh\necho 1.4.251\necho more\n")
        self.script("gh", "#!/bin/sh\nexit 3\n")
        report = maintenance.doctor()["tools"]
        self.assertEqual(report["orca"], {"path": str(self.bin / "orca"), "version": "1.4.251"})
        self.assertEqual(report["gh"], {"path": str(self.bin / "gh"), "version": None})

    def test_preferences_and_an_unusable_location_are_described_not_repaired(self):
        path = self.write_routes("schema: pod/v2\nroutes:\n  codex/gpt-6.1-sol/high: enabled\npinned: codex/gpt-6.1-sol/high\n"
                                 "merge_authorization: coordinator\n")
        raw = path.read_bytes()
        report = maintenance.doctor()["preferences"]
        self.assertEqual((report["status"], report["enabled"], report["pinned"], report["merge_authorization"]),
                         ("ok", 1, "codex/gpt-6.1-sol/high", "coordinator"))
        path.write_text("schema: pod/v9\n")
        self.assertEqual(maintenance.doctor()["preferences"]["status"], "invalid")
        self.assertEqual(path.read_text(), "schema: pod/v9\n")
        path.write_bytes(raw)
        with patch.dict(os.environ, {"POD_STATE_HOME": "relative"}):
            self.assertIn("POD_STATE_HOME", maintenance.doctor()["locations"]["problem"])

    def test_a_damaged_receipt_needs_attention_and_names_the_repair(self):
        receipt = self.tmp / "data/pod/install.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{not json")
        report = maintenance.doctor()["installation"]
        self.assertEqual((report["status"], report["healthy"]), ("needs attention", False))
        self.assertIn("install.sh", report["next"])
        self.assertEqual(receipt.read_text(), "{not json")

    def test_update_takes_no_arguments(self):
        errors = StringIO()
        with redirect_stderr(errors), patch("pod.installer.main") as installer_main:
            self.assertEqual(maintenance.update(["--force"]), 2)
            installer_main.assert_not_called()
            installer_main.return_value = 0
            self.assertEqual(maintenance.update([]), 0)
            installer_main.assert_called_once_with(["--update"])
        self.assertIn("no arguments", errors.getvalue())

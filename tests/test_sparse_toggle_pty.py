"""Sparse saved-map fleet-toggle proof through the real terminal launcher."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from pod.catalog import IDS
from pod.config import load
from tests.pty_harness import PtySession, dependencies_available


class SparseTogglePtyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not dependencies_available():
            if os.environ.get("POD_REQUIRE_PTY") == "1":
                raise AssertionError("POD_REQUIRE_PTY=1 needs pinned pyte and wcwidth")
            raise unittest.SkipTest("pyte and wcwidth are optional local test dependencies")

    def test_sparse_custom_all_custom_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, work = root / "home", root / "work"
            home.mkdir(); work.mkdir()
            config = home / "config/pod/config.yaml"
            config.parent.mkdir(parents=True)
            original = ("schema: pod/v1\nselection: custom # retain choice\n"
                        "models:\n  gpt-6-sol: preferred # explicit\n"
                        "workers: {max_active: 2}\n")
            config.write_text(original)
            with PtySession(home=home, cwd=work) as first:
                first.wait_for("Details", timeout=4)
                self.assertIn("1 eligible", first.text())
                first.send("r")
                first.wait_for("All models")
                all_mode = load(personal=config)
                self.assertEqual(all_mode["mode"], "all")
                self.assertEqual(set(all_mode["eligible"]), set(IDS))
                self.assertEqual(all_mode["saved"]["gpt-6-sol"], "preferred")
                self.assertEqual(len(all_mode["not_set"]), 5)
            with PtySession(home=home, cwd=work) as second:
                second.wait_for("All models", timeout=4)
                second.send("r")
                second.wait_for("My selection")
                restored = load(personal=config)
                self.assertEqual(restored["mode"], "custom")
                self.assertEqual(restored["eligible"], ["gpt-6-sol"])
                self.assertEqual(config.read_text(), original)

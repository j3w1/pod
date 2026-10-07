"""Offline fixtures own their caller and homes, including after stopall."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pod.ledger import state_root
from tests.common import fixture


class FixtureAuthorityTests(unittest.TestCase):
    def test_default_and_explicit_callers_survive_stopall_without_home_escape(self):
        keys = ("ORCA_TERMINAL_HANDLE", "HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "POD_STATE_HOME", "POD_CONFIG_HOME", "POD_CACHE_HOME")
        for ambient in ("dispatched-fixture-caller", None):
            for selected in ("default", "different", None):
                with self.subTest(ambient=ambient, selected=selected):
                    with patch.dict(os.environ, {}):
                        if ambient is None: os.environ.pop("ORCA_TERMINAL_HANDLE", None)
                        else: os.environ["ORCA_TERMINAL_HANDLE"] = ambient
                        outer = {key: os.environ.get(key) for key in keys}
                        args = {} if selected == "default" else {"caller":selected}
                        with fixture(**args) as root:
                            expected = "owner" if selected == "default" else selected
                            self.assertEqual(os.environ.get("ORCA_TERMINAL_HANDLE"), expected)
                            self.assertTrue(state_root(root).is_relative_to(root.parent))
                            inner = {key: os.environ.get(key) for key in keys}
                            overlay = patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE":"overlay",
                                "XDG_STATE_HOME":str(root / "overlay-state")})
                            overlay.start()
                            patch.stopall()
                            self.assertEqual({key:os.environ.get(key) for key in keys}, inner)
                            self.assertTrue(state_root(root).is_relative_to(root.parent))
                        self.assertEqual({key:os.environ.get(key) for key in keys}, outer)
                        if outer["POD_STATE_HOME"] is not None:
                            self.assertEqual(state_root(), Path(outer["POD_STATE_HOME"]))

    def test_explicit_caller_changes_inside_default_fixture_remain_effective(self):
        with fixture() as root:
            for caller in ("different", None):
                with self.subTest(caller=caller), patch.dict(os.environ, {}):
                    if caller is None: os.environ.pop("ORCA_TERMINAL_HANDLE", None)
                    else: os.environ["ORCA_TERMINAL_HANDLE"] = caller
                    self.assertEqual(os.environ.get("ORCA_TERMINAL_HANDLE"), caller)
                    self.assertTrue(state_root(root).is_relative_to(root.parent))

"""Shared test base: every test gets its own Pod homes and never sees the real ones."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class PodCase(unittest.TestCase):
    """A temporary directory, fresh POD_*_HOME locations inside it, and a PATH without the real `orca`."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="pod-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        homes = {name: str(self.tmp / name.lower()) for name in ("POD_STATE_HOME", "POD_CONFIG_HOME", "POD_CACHE_HOME")}
        path = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        env = patch.dict(os.environ, {**homes, "PATH": path, "HOME": str(self.tmp / "home"),
                                      "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        env.start()
        self.addCleanup(env.stop)
        (self.tmp / "home").mkdir()
        for name in ("ORCA_TERMINAL_HANDLE", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            os.environ.pop(name, None)

    def write_routes(self, text: str) -> Path:
        from pod import paths
        target = paths.route_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def script(self, name: str, body: str) -> Path:
        """Put an executable stand-in on PATH."""
        target = self.bin / name
        target.write_text(body, encoding="utf-8")
        target.chmod(0o755)
        return target

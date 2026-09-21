from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


@contextmanager
def fixture():
    root = os.environ.get("POD_TEST_ROOT")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as name:
        path = Path(name)
        homes = {"XDG_CONFIG_HOME": str(path / "config-home"),
                 "XDG_STATE_HOME": str(path / "state-home"),
                 "APPDATA": str(path / "appdata"),
                 "LOCALAPPDATA": str(path / "localappdata"),
                 "CODEX_HOME": str(path / "codex-home"),
                 "CLAUDE_CONFIG_DIR": str(path / "claude-home")}
        with patch.dict(os.environ, homes, clear=False):
            os.environ.pop("POD_CONFIG_HOME", None)
            os.environ.pop("POD_STATE_HOME", None)
            yield path

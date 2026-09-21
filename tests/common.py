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
        base = Path(name)
        path = base / "fixture"
        path.mkdir()
        homes = {"XDG_CONFIG_HOME": str(base / "config-home"),
                 "XDG_STATE_HOME": str(base / "state-home"),
                 "APPDATA": str(base / "appdata"),
                 "LOCALAPPDATA": str(base / "localappdata"),
                 "CODEX_HOME": str(base / "codex-home"),
                 "CLAUDE_CONFIG_DIR": str(base / "claude-home")}
        with patch.dict(os.environ, homes, clear=False):
            os.environ.pop("POD_CONFIG_HOME", None)
            os.environ.pop("POD_STATE_HOME", None)
            yield path

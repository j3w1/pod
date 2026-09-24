from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

ORCA_VERSION = "1.4.209"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / f"orca-{ORCA_VERSION}"
HOST_HOME = Path.home().resolve()
HOST_POD_DATA = Path(os.environ.get("XDG_DATA_HOME", HOST_HOME / ".local/share")).resolve() / "pod"


def disposable_path(home: Path) -> str:
    """Expose only a disposable launcher directory and system executables."""
    return os.pathsep.join((str(home / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"))


def modified_bundle(root: Path) -> Path:
    """An independent bundle copy with changed code bytes and the same VERSION."""
    from pod.bundle import bundle_root
    destination = root / "modified-pod"
    shutil.copytree(bundle_root(), destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    module = destination / "operations.py"
    module.write_bytes(module.read_bytes() + b"\n# disposable changed-code proof\n")
    return destination


def receipt(name: str) -> dict:
    """One sanitized capture of the installed Orca runtime's own output."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def envelope(name: str) -> dict:
    """The same capture in the shape read_command returns."""
    body = receipt(name)
    return {"runtime": body["_meta"]["runtimeId"], "result": body["result"]}


@contextmanager
def fixture():
    root = os.environ.get("POD_TEST_ROOT")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as name:
        base = Path(name)
        path = base / "fixture"
        path.mkdir()
        homes = {"HOME": str(base / "home"),
                 "XDG_CONFIG_HOME": str(base / "config-home"),
                 "XDG_DATA_HOME": str(base / "data-home"),
                 "XDG_STATE_HOME": str(base / "state-home"),
                 "CODEX_HOME": str(base / "codex-home"),
                 "CLAUDE_CONFIG_DIR": str(base / "claude-home"),
                 "PATH": disposable_path(base / "home")}
        if (Path(homes["XDG_DATA_HOME"]).resolve(strict=False) / "pod").is_relative_to(HOST_POD_DATA):
            raise AssertionError("Disposable fixture resolved into the host Pod data directory")
        (base / "home").mkdir()
        with patch.dict(os.environ, homes, clear=False):
            os.environ.pop("POD_CONFIG_HOME", None)
            os.environ.pop("POD_STATE_HOME", None)
            yield path

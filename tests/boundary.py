"""The environment every test process starts with, established before discovery or import.

`tests/__init__.py` calls `enter()` first, so this exists before any test module, Pod module, fixture
or subprocess is created, whether the run is `python -m unittest`, `discover`, the lane runner or one of
its workers. Nothing here snapshots real state; the process is given nothing real to reach:

* disposable HOME, XDG, provider and Pod homes, TMPDIR and PATH inside one private root;
* every inherited `GIT_*` variable removed and Git config pinned to nothing (tests/isolation.py);
* credential-looking and Orca variables dropped.

This is hygiene for accidents, not a sandbox: a program can still open an absolute path. Filesystem
isolation is the job of an established mechanism around the whole command: the ephemeral hosted runner
in CI, and locally `tools/isolate.py`, which wraps the command in bubblewrap so the home directory,
credentials and every other checkout are absent and the checkout is read-only before Python starts.

Switches, for tools and tests rather than for running a suite:

* POD_TEST_BOUNDARY=off: the maintenance command that refreshes recorded timings needs the network
  and the checkout; it runs no tests.
* POD_TEST_BOUNDARY=nested: a child starts its own private root (tests that need a second one).
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

SWITCH = "POD_TEST_BOUNDARY"
ROOT_VARIABLE = "POD_TEST_PRIVATE_ROOT"
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE_?KEY|AUTH|^ORCA_|^SSH_|COOKIE|SESSION",
                       re.IGNORECASE)
KEPT = frozenset({"GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_SHA", "GITHUB_STEP_SUMMARY", "POD_REQUIRE_PTY"})

PRIVATE_ROOT: Path | None = None


def _environment(root: Path) -> dict:
    home, tmp = root / "home", root / "tmp"
    for path in (home, tmp, home / ".local/bin"):
        path.mkdir(parents=True, exist_ok=True)
    homes = {"HOME": home, "XDG_CONFIG_HOME": root / "config-home", "XDG_DATA_HOME": root / "data-home",
             "XDG_STATE_HOME": root / "state-home", "XDG_CACHE_HOME": root / "cache-home",
             "CODEX_HOME": root / "codex-home", "CLAUDE_CONFIG_DIR": root / "claude-home",
             "POD_STATE_HOME": root / "pod-state", "POD_CONFIG_HOME": root / "pod-config",
             "POD_CACHE_HOME": root / "pod-cache", "TMPDIR": tmp, "POD_TEST_ROOT": tmp}
    for path in homes.values():
        path.mkdir(parents=True, exist_ok=True)
    values = {key: str(path) for key, path in homes.items()}
    values["PATH"] = os.pathsep.join((str(home / ".local/bin"), *SYSTEM_PATH))
    values["PYTHONDONTWRITEBYTECODE"] = "1"
    return values


def _drop_sensitive() -> None:
    for key in [key for key in os.environ if SENSITIVE.search(key) and key not in KEPT]:
        del os.environ[key]


def enter() -> None:
    """Establish the boundary for this process and every process it starts. Idempotent."""
    global PRIVATE_ROOT
    mode = os.environ.get(SWITCH)
    if mode == "off" or PRIVATE_ROOT is not None:
        return
    inherited = os.environ.get(ROOT_VARIABLE)
    from tests import isolation
    if inherited and mode != "nested" and Path(inherited).is_dir():
        # A worker or helper of a process that is already inside: same root, and an environment
        # an explicit `env=` cannot have reintroduced anything into.
        PRIVATE_ROOT = Path(inherited)
        _drop_sensitive()
        isolation.scrub_git_environment(os.environ, ceiling=PRIVATE_ROOT)
        sys.dont_write_bytecode = True
        return
    parent = os.environ.get("TMPDIR") if inherited else os.environ.get("POD_TEST_ROOT") or os.environ.get("TMPDIR")
    base = Path(parent) if parent and os.path.isdir(parent) else Path(tempfile.gettempdir())
    root = Path(tempfile.mkdtemp(prefix="pod-test-", dir=base)).resolve()
    atexit.register(shutil.rmtree, root, True)
    _drop_sensitive()
    os.environ.pop(SWITCH, None)
    os.environ.update(_environment(root))
    isolation.scrub_git_environment(os.environ, ceiling=root)
    PRIVATE_ROOT = root
    os.environ[ROOT_VARIABLE] = str(root)
    tempfile.tempdir = str(root / "tmp")
    sys.dont_write_bytecode = True

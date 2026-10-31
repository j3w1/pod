"""The execution boundary every test process runs inside, established before discovery or import.

`tests/__init__.py` calls `enter()` first, so the boundary exists before any test module, Pod module,
fixture or subprocess is created, whether the run is `python -m unittest`, `discover`, the lane runner
or one of its workers. Detecting damage afterwards cannot undo it and races legitimate work on the
host, so nothing here snapshots real state. The process is instead made unable to reach it:

* Environment: disposable HOME, XDG, provider and Pod homes, TMPDIR and PATH inside one private root;
  every inherited `GIT_*` variable removed and Git config pinned to nothing; credential-looking and
  Orca variables dropped.
* Filesystem (Linux Landlock, inherited by every child process): writes are possible only inside the
  private root, `/dev` and the CI step summary file; file contents can be read only under system
  directories, this checkout (read-only, with its Git directories), the interpreter and the paths in
  POD_TEST_READABLE. Directory names are listable everywhere, because Pod opens "/" itself. A home
  directory's files, its credentials, an installed launcher and every other checkout are therefore
  neither readable nor writable, however a command is started: explicit `GIT_DIR`, a `.git` pointer
  file, a linked worktree or common directory, or an absolute path. A fixture's Git metadata lives
  in its own repository inside the private root.

Where Landlock is unavailable the environment still isolates and says so on stderr, `CONFINED` is
false and the tests that prove the filesystem boundary skip, which the CI lanes forbid.

Switches, for tools and tests rather than for running a suite:

* POD_TEST_BOUNDARY=off: the maintenance command that refreshes recorded timings needs the network
  and the checkout; it runs no tests.
* POD_TEST_BOUNDARY=nested: a child starts its own, narrower private root (the leak tests).
* POD_TEST_READABLE: extra read-only paths, separated like PATH.
"""

from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

SWITCH = "POD_TEST_BOUNDARY"
ROOT_VARIABLE = "POD_TEST_PRIVATE_ROOT"
READABLE = "POD_TEST_READABLE"
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
# Directories every program needs to read. Deliberately not /home, /root, /mnt, /media, /srv, /tmp
# or /var/tmp, where credentials, checkouts and other people's scratch live.
READ_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev",
               "/run", "/snap", "/nix", "/var/lib", "/var/cache")
SENSITIVE = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE_?KEY|AUTH|^ORCA_|^SSH_|COOKIE|SESSION",
                       re.IGNORECASE)
KEPT = frozenset({"GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_SHA", "GITHUB_STEP_SUMMARY", "POD_REQUIRE_PTY"})

CONFINED = False
PRIVATE_ROOT: Path | None = None

# Landlock, linux/landlock.h
_CREATE, _ADD, _RESTRICT = 444, 445, 446
_ACCESS = {"WRITE_FILE": 1 << 1, "READ_FILE": 1 << 2, "READ_DIR": 1 << 3, "REMOVE_DIR": 1 << 4,
           "REMOVE_FILE": 1 << 5, "MAKE_CHAR": 1 << 6, "MAKE_DIR": 1 << 7, "MAKE_REG": 1 << 8,
           "MAKE_SOCK": 1 << 9, "MAKE_FIFO": 1 << 10, "MAKE_BLOCK": 1 << 11, "MAKE_SYM": 1 << 12,
           "REFER": 1 << 13, "TRUNCATE": 1 << 14}
_READ = _ACCESS["READ_FILE"] | _ACCESS["READ_DIR"]
_FILE_RIGHTS = _ACCESS["WRITE_FILE"] | _ACCESS["READ_FILE"] | _ACCESS["TRUNCATE"]
_NO_NEW_PRIVS = 38


class _Rule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _abi(libc) -> int:
    ctypes.set_errno(0)
    return libc.syscall(_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))


def _restrict(*, writable: list[str], files: list[str], readable: list[str]) -> bool:
    """Deny every filesystem right except those granted; False when the kernel cannot."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        abi = _abi(libc)
    except (OSError, AttributeError):
        return False
    if abi < 1:
        return False
    handled = sum(_ACCESS.values())
    if abi < 2:
        handled &= ~_ACCESS["REFER"]
    if abi < 3:
        handled &= ~_ACCESS["TRUNCATE"]
    attribute = ctypes.c_uint64(handled)
    ruleset = libc.syscall(_CREATE, ctypes.byref(attribute), ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        return False
    try:
        def grant(path: str, rights: int) -> None:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                return
            try:
                if not os.path.isdir(path):
                    rights &= _FILE_RIGHTS
                rule = _Rule(rights & handled, descriptor)
                if rule.allowed_access and libc.syscall(_ADD, ctypes.c_int(ruleset), ctypes.c_int(1),
                                                         ctypes.byref(rule), ctypes.c_uint32(0)) < 0:
                    raise OSError(ctypes.get_errno(), "landlock_add_rule", path)
            finally:
                os.close(descriptor)

        for path in readable:
            grant(path, _READ)
        # Pod opens "/" as a directory descriptor for its no-follow reads. A right on "/" covers the
        # whole tree, so it is names only: file contents stay unreadable outside `readable`.
        grant("/", _ACCESS["READ_DIR"])
        for path in writable:
            grant(path, handled)
        for path in files:
            grant(path, _ACCESS["WRITE_FILE"] | _ACCESS["TRUNCATE"] | _ACCESS["READ_FILE"])
        if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False
        return libc.syscall(_RESTRICT, ctypes.c_int(ruleset), ctypes.c_uint32(0)) == 0
    except OSError:
        return False
    finally:
        os.close(ruleset)


def _git_directories(checkout: Path) -> list[str]:
    """This checkout's Git directory and, for a linked worktree, the common one, read from files."""
    found = []
    pointer = checkout / ".git"
    try:
        if pointer.is_file():
            gitdir = Path(pointer.read_text().strip().removeprefix("gitdir:").strip())
            gitdir = gitdir if gitdir.is_absolute() else (checkout / gitdir)
            found.append(str(gitdir.resolve()))
            common = gitdir / "commondir"
            if common.is_file():
                found.append(str((gitdir / common.read_text().strip()).resolve()))
    except OSError:
        pass
    return found


def _readable(checkout: Path) -> list[str]:
    paths = list(READ_SYSTEM) + [str(checkout)] + _git_directories(checkout)
    paths += [entry for entry in sys.path if entry and os.path.isabs(entry)]
    paths += [sys.prefix, sys.base_prefix, sys.exec_prefix, str(Path(sys.executable).resolve().parent)]
    paths += [item for item in os.environ.get(READABLE, "").split(os.pathsep) if item]
    return list(dict.fromkeys(str(Path(path).resolve()) for path in paths if os.path.exists(path)))


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
    global CONFINED, PRIVATE_ROOT
    mode = os.environ.get(SWITCH)
    if mode == "off" or PRIVATE_ROOT is not None:
        return
    inherited = os.environ.get(ROOT_VARIABLE)
    from tests import isolation
    if inherited and mode != "nested" and Path(inherited).is_dir():
        # A worker or helper of a process that is already inside: same root and restrictions, and
        # an environment an explicit `env=` cannot have reintroduced anything into.
        PRIVATE_ROOT, CONFINED = Path(inherited), os.environ.get("POD_TEST_CONFINED") == "1"
        _drop_sensitive()
        isolation.scrub_git_environment(os.environ, ceiling=PRIVATE_ROOT)
        sys.dont_write_bytecode = True
        return
    parent = os.environ.get("TMPDIR") if inherited else os.environ.get("POD_TEST_ROOT") or os.environ.get("TMPDIR")
    base = Path(parent) if parent and os.path.isdir(parent) else Path(tempfile.gettempdir())
    root = Path(tempfile.mkdtemp(prefix="pod-test-", dir=base)).resolve()
    atexit.register(shutil.rmtree, root, True)
    checkout = Path(__file__).resolve().parents[1]
    readable = _readable(checkout)
    _drop_sensitive()
    os.environ.pop(SWITCH, None)
    os.environ.update(_environment(root))
    isolation.scrub_git_environment(os.environ, ceiling=root)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    files = [summary] if summary and os.path.isfile(summary) else []
    CONFINED = _restrict(writable=[str(root), "/dev"], files=files, readable=readable)
    PRIVATE_ROOT = root
    os.environ[ROOT_VARIABLE] = str(root)
    os.environ["POD_TEST_CONFINED"] = "1" if CONFINED else "0"
    tempfile.tempdir = str(root / "tmp")
    sys.dont_write_bytecode = True
    if not CONFINED:
        print("tests: Landlock is unavailable; fixtures are isolated by environment only", file=sys.stderr)

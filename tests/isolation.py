"""Fixture isolation: scrubbed Git environment and a guard over real checkouts and host files.

Cause of #141. Git commands in the fixtures were written as `git -C <disposable> ...`, or as
`git init <disposable>`, and trusted the process environment. A test run started by anything
that exports `GIT_DIR` (a Git hook, `git rebase --exec`, `git bisect run`, `git submodule foreach`,
a wrapper script) sent every one of them to that repository instead: `git init` re-initialised it,
`git config user.name Fixture` wrote its config, and `git add .` + `git commit -m base` committed the
fixture's two files over its HEAD. HOME and XDG redirection did not help, since `GIT_DIR` outranks
the working directory and `GIT_CONFIG_GLOBAL` outranks HOME.

Two controls:

* `scrub_git_environment` removes every inherited `GIT_*` variable, then pins config to nothing
  (no system or global file, so no `includeIf`, hooks path, credential helper or identity) and a
  discovery ceiling at the fixture, so a repository above it can never be found by walking up.
* `Guard` snapshots what a fixture must never change - HEAD, the checked-out branch ref, the index
  and config of the real checkouts, plus the installed launcher and credential files - and
  `verify()` fails naming what changed. `fixture()` verifies on exit, so the breach is reported by
  the test that caused it.

The real checkouts are the one that contains these tests, its main (canonical) checkout when this is
a linked worktree, and any listed in POD_TEST_PROTECT (paths separated like PATH). Other worktrees
of the same repository belong to other tasks and legitimately change while a suite runs.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable, Iterator, Mapping, MutableMapping

CEILING = "GIT_CEILING_DIRECTORIES"
# Applied last, so an inherited value can never win. maintenance.auto stops Git detaching a
# background maintenance process after a commit (see tests/__init__.py).
GIT_CONFIG = (("maintenance.auto", "false"),)
# Choose a repository's format and cannot redirect a command, so a test may set them on purpose.
KEPT = ("GIT_DEFAULT_HASH", "GIT_DEFAULT_REF_FORMAT")
PINNED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
          "GIT_EDITOR": "true"}
# Host files whose content a fixture must never write, relative to the real HOME.
CREDENTIALS = (".gitconfig", ".git-credentials", ".netrc", ".config/git/config", ".config/git/credentials",
               ".config/gh/hosts.yml", ".claude/.credentials.json", ".codex/auth.json")
LAUNCHER = (".local/bin/pod",)


class IsolationBreach(AssertionError):
    """A fixture changed state that belongs to the host."""


def scrub_git_environment(env: MutableMapping[str, str], *, ceiling: Path | str | None = None) -> None:
    """Drop every inherited GIT_* variable and pin the ones that keep Git inside the fixture."""
    for key in [key for key in env if key.upper().startswith("GIT_") and key not in KEPT]:
        del env[key]
    env.update(PINNED)
    env["GIT_CONFIG_COUNT"] = str(len(GIT_CONFIG))
    for index, (key, value) in enumerate(GIT_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if ceiling is not None:
        env[CEILING] = str(ceiling)


def _clean_git() -> dict:
    env = dict(os.environ)
    scrub_git_environment(env)
    return env


def _git(*argv: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(["git", "--no-optional-locks", *argv], cwd=cwd, env=_clean_git(),
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def _config_digest(path: Path) -> str:
    """The config without `[branch ...]` sections, which every task on a shared host adds and edits."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{exc.errno}"
    kept, skipping = [], False
    for line in lines:
        if line.lstrip().startswith("["):
            skipping = line.lstrip().lower().startswith("[branch ")
        if not skipping:
            kept.append(line)
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


def _index_digest(path: Path) -> str:
    """Entries of a SHA-1 index (version 2 or 3) without their stat data, which `git status` rewrites.

    Anything else, including an unreadable or version 4 index, falls back to the bytes of the file.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{exc.errno}"
    fallback = hashlib.sha256(data).hexdigest()
    if data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") not in (2, 3):
        return fallback
    found, position = hashlib.sha256(), 12
    for _ in range(int.from_bytes(data[8:12], "big")):
        flags = int.from_bytes(data[position + 60:position + 62], "big")
        fixed = 64 if flags & 0x4000 else 62
        end = data.find(b"\0", position + fixed)
        if end < 0:
            return fallback
        found.update(data[position + 24:position + 28] + data[position + 40:position + 62]
                     + data[position + 62:end + 1])
        position += (end - position + 8) // 8 * 8
    # What follows the entries must be extensions and the 20 byte checksum, never a half-read entry.
    return found.hexdigest() if len(data) - position >= 20 else fallback


def _branch_ref(gitdir: Path, common: Path) -> tuple[str, str]:
    """The ref HEAD names and its object id, read from files so a snapshot spawns nothing."""
    head = _digest_text(gitdir / "HEAD")
    if not head.startswith("ref: "):
        return head, head
    name = head[5:].strip()
    for base in (gitdir, common):
        loose = _digest_text(base / name)
        if loose != "absent":
            return name, loose
    packed = _digest_text(common / "packed-refs")
    for line in packed.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1] == name:
            return name, parts[0]
    return name, "absent"


def _digest_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def _gitdirs(checkout: Path) -> tuple[Path, Path] | None:
    """(this checkout's git directory, the common one), or None when it is not a checkout."""
    absolute = _git("rev-parse", "--absolute-git-dir", cwd=checkout)
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout)
    return (Path(absolute), Path(common)) if absolute and common else None


def checkout_state(dirs: tuple[Path, Path] | None) -> dict:
    """HEAD, the checked-out branch ref, the index and the config of one checkout, from files."""
    if dirs is None:
        return {"checkout": "not a repository"}
    gitdir, common = dirs
    name, oid = _branch_ref(gitdir, common)
    return {"HEAD": name, f"ref {name}": oid, "index": _index_digest(gitdir / "index"),
            "config": _config_digest(common / "config")}


def default_checkouts(tests: Path | None = None, extra: str | None = None) -> list[Path]:
    """The checkout holding these tests, its main checkout, and every POD_TEST_PROTECT entry."""
    here = tests or Path(__file__).resolve().parent
    found: list[Path] = []
    top = _git("rev-parse", "--show-toplevel", cwd=here)
    if top:
        found.append(Path(top))
        common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=here)
        if common and Path(common).name == ".git":
            found.append(Path(common).parent)
    listed = os.environ.get("POD_TEST_PROTECT") if extra is None else extra
    found += [Path(item) for item in (listed or "").split(os.pathsep) if item]
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(str(path.resolve(strict=False)), path.resolve(strict=False))
    return list(unique.values())


def default_files(home: Path | None = None, path: str | None = None) -> list[Path]:
    """Installed launchers and credential files of the real user."""
    home = home or Path.home()
    found = [home / name for name in CREDENTIALS + LAUNCHER]
    on_path = shutil.which("pod", path=path if path is not None else os.environ.get("PATH"))
    if on_path:
        found.append(Path(on_path))
    return list(dict.fromkeys(found))


class Guard:
    """A snapshot of host state, taken now, that `verify` compares against."""

    def __init__(self, checkouts: Iterable[Path] = (), files: Iterable[Path] = ()):
        self.checkouts = [Path(item) for item in checkouts]
        # Resolved once: a snapshot then only reads files, so verifying on every fixture exit is cheap.
        self.dirs = {path: _gitdirs(path) for path in self.checkouts}
        self.files = [Path(item) for item in files]
        self.before = self.snapshot()

    def snapshot(self) -> dict:
        state = {f"checkout {path}": checkout_state(self.dirs[path]) for path in self.checkouts}
        state.update({f"file {path}": _digest(path) for path in self.files})
        return state

    def changes(self, after: dict | None = None) -> list[str]:
        after = self.snapshot() if after is None else after
        lines = []
        for subject, was in self.before.items():
            now = after[subject]
            if isinstance(was, dict) and isinstance(now, dict):
                lines += [f"{subject}: {key} changed" for key in sorted(set(was) | set(now))
                          if was.get(key) != now.get(key)]
            elif was != now:
                lines.append(f"{subject} changed")
        return lines

    def verify(self) -> None:
        """Fail once per change: the next check compares against what this one saw."""
        after = self.snapshot()
        changed = self.changes(after)
        self.before = after
        if changed:
            raise IsolationBreach("a test changed state that belongs to the host: " + "; ".join(changed))


_ACTIVE: list[Guard] = []
_HOST: dict[str, Guard] = {}


def host_guard() -> Guard:
    """The real checkouts and files, snapshotted once from the environment the suite started in."""
    if "guard" not in _HOST:
        _HOST["guard"] = Guard(default_checkouts(), default_files())
    return _HOST["guard"]


def snapshot_host() -> None:
    """Called at import of the tests package, before any fixture changes the environment."""
    host_guard()


def guards() -> list[Guard]:
    return [host_guard(), *_ACTIVE]


@contextmanager
def watching(guard: Guard) -> Iterator[Guard]:
    """Also verify `guard` whenever a fixture exits; for the guard's own tests."""
    _ACTIVE.append(guard)
    try:
        yield guard
    finally:
        _ACTIVE.remove(guard)


def verify_all() -> None:
    breaches = []
    for guard in guards():
        try:
            guard.verify()
        except IsolationBreach as exc:
            breaches.append(str(exc))
    if breaches:
        raise IsolationBreach(" | ".join(dict.fromkeys(breaches)))


def assert_disposable(base: Path, env: Mapping[str, str]) -> None:
    """Every home a fixture resolves must sit inside its own disposable directory."""
    base = base.resolve()
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        value = env.get(key)
        if not value or not Path(value).resolve(strict=False).is_relative_to(base):
            raise IsolationBreach(f"{key} escapes the disposable fixture directory")
    for entry in env.get("PATH", "").split(os.pathsep):
        if entry.startswith(str(base)) or entry in ("/usr/local/bin", "/usr/bin", "/bin"):
            continue
        raise IsolationBreach("PATH reaches a directory outside the disposable fixture and system tools")

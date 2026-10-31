"""Git environment scrubbing and the disposable-directory check for fixtures.

Cause of #141. Git commands in the fixtures were written as `git -C <disposable> ...`, or as
`git init <disposable>`, and trusted the process environment. A test run started by anything
that exports `GIT_DIR` (a Git hook, `git rebase --exec`, `git bisect run`, `git submodule foreach`,
a wrapper script) sent every one of them to that repository instead: `git init` re-initialised it,
`git config user.name Fixture` wrote its config, and `git add .` + `git commit -m base` committed the
fixture's two files over its HEAD. HOME and XDG redirection did not help, since `GIT_DIR` outranks
the working directory and `GIT_CONFIG_GLOBAL` outranks HOME.

`scrub_git_environment` removes every inherited `GIT_*` variable, pins config to nothing (no system or
global file, so no `includeIf`, hooks path, credential helper or identity) and, for a fixture, sets a
discovery ceiling. Scrubbing only stops the accident. `tests/boundary.py` makes the host unwritable,
which also stops an explicit `GIT_DIR`, a `.git` pointer file or a linked worktree.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, MutableMapping

CEILING = "GIT_CEILING_DIRECTORIES"
# Applied last, so an inherited value can never win. maintenance.auto stops Git detaching a
# background maintenance process after a commit (see tests/__init__.py).
GIT_CONFIG = (("maintenance.auto", "false"),)
# Choose a repository's format and cannot redirect a command, so a test may set them on purpose.
KEPT = ("GIT_DEFAULT_HASH", "GIT_DEFAULT_REF_FORMAT")
PINNED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
          "GIT_EDITOR": "true"}


class IsolationBreach(AssertionError):
    """A fixture resolved a home or tool outside its disposable directory."""


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

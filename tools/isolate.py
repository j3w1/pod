#!/usr/bin/env python3
"""Run a command with the home directory, credentials and other checkouts absent (bubblewrap).

Fixture tests must not be able to reach a real checkout or credential, and the protection has to be
in place before test discovery and before any subprocess starts. An established mechanism around the
whole command does that; Python code inside the test process cannot. Hosted CI is already an
ephemeral runner, so this is for local runs:

    python tools/isolate.py -- python -m unittest discover -s tests -t .
    python tools/isolate.py --readable /path/to/more -- COMMAND ...
    python tools/isolate.py --self-check

The command sees the host file system read-only, with /home, /root, /mnt, /media, /srv, /tmp,
/var/tmp and /run/user replaced by empty memory file systems, then this checkout (and the Git
directories its `.git` file points at) and any `--readable` path put back read-only. The environment
is rebuilt from a short allowlist, for bubblewrap itself as well as for the command, so no token or
Orca variable crosses (the command's own `/proc/1/environ` is bubblewrap's). A fixture's own files live
in a fresh `/tmp`. This is protection from accidental contamination of a trusted developer's host,
not a sandbox for hostile code: the network is shared and the host's read-only system tree stays
visible.

`tests/boundary.py` still gives every test process disposable homes and a scrubbed Git environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = ("/home", "/root", "/mnt", "/media", "/srv", "/tmp", "/var/tmp", "/run/user")
PASSED = ("PATH", "LANG", "LANGUAGE", "TERM", "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "CI",
          "GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_SHA", "POD_REQUIRE_PTY")


def git_directories(checkout: Path) -> list[Path]:
    """The Git directory and, for a linked worktree, the common one, read from the `.git` file."""
    found: list[Path] = []
    pointer = checkout / ".git"
    try:
        if pointer.is_file():
            gitdir = Path(pointer.read_text().strip().removeprefix("gitdir:").strip())
            gitdir = (gitdir if gitdir.is_absolute() else checkout / gitdir).resolve()
            found.append(gitdir)
            common = gitdir / "commondir"
            if common.is_file():
                found.append((gitdir / common.read_text().strip()).resolve())
    except OSError:
        pass
    return found


def environment(env: dict[str, str]) -> dict[str, str]:
    kept = {key: value for key, value in env.items() if key in PASSED or key.startswith("LC_")}
    kept["HOME"] = "/tmp/home"
    kept["TMPDIR"] = "/tmp"
    return kept


def arguments(command: list[str], *, checkout: Path = ROOT, readable: tuple[str, ...] = (),
              env: dict[str, str] | None = None, cwd: Path | None = None) -> list[str]:
    """The bubblewrap command line; pure, so it can be tested without bubblewrap."""
    env = os.environ if env is None else env
    shown = [checkout, *git_directories(checkout), *map(Path, readable)]
    shown += [Path(entry) for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
    shown += [Path(sys.prefix), Path(sys.base_prefix)]
    argv = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for hidden in HIDDEN:
        if os.path.isdir(hidden):
            argv += ["--tmpfs", hidden]
    for path in dict.fromkeys(item.resolve() for item in shown if item.exists()):
        argv += ["--ro-bind", str(path), str(path)]
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and os.path.isfile(summary):
        argv += ["--bind", summary, summary]
    argv += ["--chdir", str(cwd or checkout), "--clearenv"]
    for key, value in environment(dict(env)).items():
        argv += ["--setenv", key, value]
    if summary and os.path.isfile(summary):
        argv += ["--setenv", "GITHUB_STEP_SUMMARY", summary]
    return argv + ["--", *command]


def git(where: Path, *argv: str) -> str:
    """Git for the self-check's setup, outside bubblewrap: the allowlisted environment carries no
    inherited GIT_* variable (an inherited GIT_DIR sends any command to that repository), and config is
    pinned to nothing, as for the fixtures (tests/isolation.py)."""
    env = {**environment(dict(os.environ)), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CEILING_DIRECTORIES": str(where.parent)}
    return subprocess.run(["git", "-C", str(where), *argv], capture_output=True, text=True, check=True,
                          env=env).stdout.strip()


def launch(command: list[str], *, capture: bool = False, env: dict[str, str] | None = None, **options):
    """Start the command under bubblewrap, itself started with only the allowlisted environment."""
    env = dict(os.environ if env is None else env)
    tool = shutil.which("bwrap", path=env.get("PATH"))
    if not tool:
        raise SystemExit("isolate: bubblewrap (bwrap) is required")
    argv = arguments(command, env=env, **options)
    argv[0] = tool
    return subprocess.run(argv, check=False, env=environment(env), capture_output=capture, text=capture)


def run(command: list[str], **options) -> int:
    return launch(command, **options).returncode


PROBE = textwrap.dedent('''
    import json, os, subprocess, sys
    from pathlib import Path
    import tests
    spec, out = json.loads(sys.argv[1]), {}
    def attempt(name, action):
        try:
            action(); out[name] = "allowed"
        except OSError as error:
            out[name] = type(error).__name__
    stand_in, gitdir = Path(spec["stand_in"]), Path(spec["stand_in"]) / ".git"
    out["environment"] = [name for name in list(os.environ) + Path("/proc/1/environ").read_text().split("\\0")
                          if "CANARY" in name]
    attempt("read credentials", lambda: Path(spec["credentials"]).read_text())
    attempt("write credentials", lambda: Path(spec["credentials"]).write_text("x"))
    attempt("write stand-in checkout", lambda: (stand_in / "new").write_text("x"))
    attempt("write checkout", lambda: Path(spec["checkout"], ".canary").write_text("x"))
    def git(where, *argv, env=None):
        return subprocess.run(["git", "-C", str(where), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                               *argv], capture_output=True, env={**os.environ, **(env or {})}).returncode
    own = Path(os.environ["TMPDIR"]) / "own"; own.mkdir()
    out["control"] = git(own, "init", "-q", "-b", "main") + git(own, "commit", "--allow-empty", "-qm", "own")
    pointer = Path(os.environ["TMPDIR"]) / "pointer"; pointer.mkdir()
    (pointer / ".git").write_text("gitdir: " + str(gitdir) + "\\n")
    out["pointer"] = git(pointer, "config", "user.name", "Fixture") or git(pointer, "commit", "--allow-empty", "-qm", "base")
    out["explicit"] = git(stand_in, "commit", "--allow-empty", "-qm", "base",
                          env={"GIT_DIR": str(gitdir), "GIT_WORK_TREE": str(stand_in)})
    print(json.dumps(out))
''')


def self_check() -> int:
    """Prove the wrapper against stand-ins: nothing real is read or written."""
    def snapshot(where):
        return [git(where, "rev-parse", "HEAD"), git(where, "for-each-ref"), (where / ".git/config").read_text()]

    base = Path(tempfile.mkdtemp(prefix="pod-isolate-check-"))
    try:
        stand_in, home = base / "checkout", base / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex/auth.json").write_text("synthetic\n")
        stand_in.mkdir()
        git(stand_in, "init", "-q", "-b", "main")
        git(stand_in, "config", "user.name", "Real")
        git(stand_in, "config", "user.email", "real@example.invalid")
        (stand_in / "tracked").write_text("real\n")
        git(stand_in, "add", ".")
        git(stand_in, "commit", "-qm", "real")
        before = snapshot(stand_in)
        discovery = base / "discovery"
        discovery.mkdir()
        (discovery / "test_import_write.py").write_text(
            "import tests\nfrom pathlib import Path\n"
            f"try:\n    Path({str(home / 'import-leak')!r}).write_text('x'); LEAKED = True\n"
            "except OSError:\n    LEAKED = False\n"
            "import unittest\nclass T(unittest.TestCase):\n    def test_x(self): self.assertFalse(LEAKED)\n")
        # The stand-in checkout is shown read-only as a real checkout is; the home is simply absent,
        # because only the checkout and what is passed with --readable are put back.
        options = {"readable": (str(stand_in), str(discovery))}
        spec = {"stand_in": str(stand_in), "credentials": str(home / ".codex/auth.json"), "checkout": str(ROOT)}
        caller = {**os.environ, "PYTHONPATH": str(ROOT), "POD_CANARY_TOKEN": "canary"}
        probe = launch([sys.executable, "-c", PROBE, json.dumps(spec)], capture=True, env=caller, **options)
        discover = launch([sys.executable, "-m", "unittest", "discover", "-s", str(discovery), "-t", str(discovery)],
                          capture=True, env=caller, **options)
        problems = []
        if probe.returncode:
            problems.append(f"probe failed: {probe.stderr.strip()[-300:]}")
        else:
            seen = json.loads(probe.stdout)
            if seen.pop("environment"):
                problems.append("the caller's environment reached the sandbox (own or /proc/1/environ)")
            if seen.pop("control") != 0:
                problems.append("a fixture could not use Git in its own /tmp repository")
            problems += [f"{name}: {result}" for name, result in seen.items()
                         if result in ("allowed", 0)]
        if discover.returncode:
            problems.append(f"discovery-time import was not confined: {discover.stderr.strip()[-300:]}")
        if snapshot(stand_in) != before or (home / ".codex/auth.json").read_text() != "synthetic\n" \
                or (home / "import-leak").exists():
            problems.append("a stand-in changed")
        print("\n".join(problems) or "isolate: self-check passed")
        return 1 if problems else 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--readable", action="append", default=[], help="extra read-only path")
    parser.add_argument("--self-check", action="store_true", help="prove the wrapper against stand-ins")
    parser.add_argument("command", nargs="*")
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()
    if not args.command:
        parser.error("a command is required after --")
    return run(args.command, readable=tuple(args.readable), cwd=Path.cwd() if Path.cwd().is_relative_to(ROOT) else None)


if __name__ == "__main__":
    raise SystemExit(main())

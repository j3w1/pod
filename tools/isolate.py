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
in one more fresh memory file system at `/tmp/isolated-<random>`, with TMPDIR there and HOME at its
`home`. Hiding a host `/tmp` that is itself a memory file system stacks a second mount on `/tmp`, and
Pod's cleanup placement checks rightly refuse a path whose mount point has two entries in
`/proc/self/mountinfo`. So each invocation picks a mount point inside the hidden `/tmp` that nothing
in the caller's `/proc/self/mountinfo` is at or under: not a host mount, and not the fixture mount of
a wrapper this one runs inside, whose entry the read-only root bind carries in even though `/tmp` hides
it. The fixture mount point therefore has exactly one entry, in a nested run too, and no readable path
may cover it or lie inside it. This is protection from accidental contamination of a trusted developer's
host, not a sandbox for hostile code: the network is shared and the host's read-only system tree stays
visible.

`tests/boundary.py` still gives every test process disposable homes and a scrubbed Git environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = ("/home", "/root", "/mnt", "/media", "/srv", "/tmp", "/var/tmp", "/run/user")
# The fixture tree's mount point is this prefix plus a random name, inside the hidden /tmp (fresh_fixture).
FIXTURE_PREFIX = "/tmp/isolated-"
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


def mount_points(mountinfo: str) -> list[Path]:
    """Every mount point in `/proc/self/mountinfo` text, with its octal escapes decoded."""
    return [Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split(" ")[4]))
            for line in mountinfo.splitlines() if line.strip()]


def fresh_fixture(taken: list[Path] | None = None) -> str:
    """A new mount point for the fixture tree, inside the hidden /tmp, that no mount the caller can see
    (`taken`, by default its own mountinfo) is at or under: neither a host mount nor the fixture mount
    of a wrapper this one runs inside, so the fixture mount point gets exactly one entry."""
    if taken is None:
        taken = mount_points(Path("/proc/self/mountinfo").read_text())
    for _ in range(64):
        candidate = Path(FIXTURE_PREFIX + secrets.token_hex(6))
        if not any(point.is_relative_to(candidate) for point in taken):
            return str(candidate)
    raise SystemExit("isolate: found no fresh fixture mount point")


def environment(env: dict[str, str], fixture: str | None = None) -> dict[str, str]:
    """The allowlisted environment, with HOME and TMPDIR on the fixture tree when one is given."""
    kept = {key: value for key, value in env.items() if key in PASSED or key.startswith("LC_")}
    if fixture:
        kept["HOME"] = f"{fixture}/home"
        kept["TMPDIR"] = fixture
    return kept


def arguments(command: list[str], *, fixture: str, checkout: Path = ROOT, readable: tuple[str, ...] = (),
              env: dict[str, str] | None = None, cwd: Path | None = None) -> list[str]:
    """The bubblewrap command line for the fixture mount point `fixture` (fresh_fixture); pure, so it can
    be tested without bubblewrap."""
    env = os.environ if env is None else env
    shown = [checkout, *git_directories(checkout), *map(Path, readable)]
    shown += [Path(entry) for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
    shown += [Path(sys.prefix), Path(sys.base_prefix)]
    argv = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for hidden in HIDDEN:
        if os.path.isdir(hidden):
            argv += ["--tmpfs", hidden]
    # One fresh mount for HOME and TMPDIR at a new mount point; --self-check proves it is not stacked.
    argv += ["--tmpfs", fixture, "--dir", f"{fixture}/home"]
    for path in dict.fromkeys(item.resolve() for item in shown if item.exists()):
        if Path(fixture).is_relative_to(path) or path.is_relative_to(fixture):
            raise SystemExit(f"isolate: showing {path} would overmount or fill the fixture tree {fixture}")
        argv += ["--ro-bind", str(path), str(path)]
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and os.path.isfile(summary):
        argv += ["--bind", summary, summary]
    argv += ["--chdir", str(cwd or checkout), "--clearenv"]
    for key, value in environment(dict(env), fixture).items():
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


def launch(command: list[str], *, capture: bool = False, env: dict[str, str] | None = None,
           fixture: str | None = None, **options):
    """Start the command under bubblewrap, itself started with only the allowlisted environment. The
    fixture mount point is chosen once (fresh_fixture), and HOME and TMPDIR follow it."""
    env = dict(os.environ if env is None else env)
    tool = shutil.which("bwrap", path=env.get("PATH"))
    if not tool:
        raise SystemExit("isolate: bubblewrap (bwrap) is required")
    fixture = fixture or fresh_fixture()
    argv = arguments(command, fixture=fixture, env=env, **options)
    argv[0] = tool
    return subprocess.run(argv, check=False, env=environment(env, fixture), capture_output=capture, text=capture)


def run(command: list[str], **options) -> int:
    return launch(command, **options).returncode


# The fixture part of the probe, also run alone under a nested wrapper. With no "fixture" in the spec
# (a nested wrapper picks its own), TMPDIR's own path is expected to be the fixture mount point.
FIXTURE_PROBE = textwrap.dedent('''
    import json, os, re, subprocess, sys
    from pathlib import Path
    spec, out = json.loads(sys.argv[1]), {}
    def unescape(value):
        return re.sub(r"\\\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split(" ")
        mounts.append((Path(unescape(fields[4])), fields[0], fields[fields.index("-", 6) + 1]))
    expected = spec.get("fixture") or os.path.realpath(os.environ["TMPDIR"])
    def mount(path):
        point = max((row[0] for row in mounts if path.is_relative_to(row[0])), key=lambda row: len(row.parts))
        return point, [row for row in mounts if row[0] == point]
    def on_fixture(when):
        for name in ("TMPDIR", "HOME"):
            point, entries = mount(Path(os.path.realpath(os.environ[name])))
            if str(point) != expected:
                yield f"{when} {name} is on {point}, not on the fixture mount {expected}"
            elif len(entries) != 1 or entries[0][2] != "tmpfs":
                yield f"{when} {name}'s mount point {point} is overmounted or not one tmpfs: {entries}"
    fixture = list(on_fixture("the wrapper's"))
    if sorted(os.listdir(os.environ["TMPDIR"])) != ["home"] or os.listdir(os.environ["HOME"]):
        fixture.append("the fixture mount was not fresh")
    # Every fixture mount point, this wrapper's and any inherited from a wrapper outside, has one entry.
    points = [str(row[0]) for row in mounts if str(row[0]).startswith(spec["prefix"])]
    fixture += [f"the fixture mount point {point} has {points.count(point)} entries"
                for point in sorted(set(points)) if points.count(point) != 1]
    import tests  # the test boundary's own disposable homes must land on the same single mount
    out["fixture"] = fixture + list(on_fixture("the test boundary's"))
    out["tmpdir"], out["points"] = expected, sorted(set(points))
''')
PROBE = FIXTURE_PROBE + textwrap.dedent('''
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
        fixture = fresh_fixture()
        spec = {"stand_in": str(stand_in), "credentials": str(home / ".codex/auth.json"), "checkout": str(ROOT),
                "fixture": fixture, "prefix": FIXTURE_PREFIX}
        caller = {**os.environ, "PYTHONPATH": str(ROOT), "POD_CANARY_TOKEN": "canary"}
        probe = launch([sys.executable, "-c", PROBE, json.dumps(spec)], capture=True, env=caller, fixture=fixture,
                       **options)
        discover = launch([sys.executable, "-m", "unittest", "discover", "-s", str(discovery), "-t", str(discovery)],
                          capture=True, env=caller, **options)
        # A wrapper inside this one inherits this fixture's mount entry and must still get a mount of its own.
        nested = launch([sys.executable, "-B", str(ROOT / "tools/isolate.py"), "--", sys.executable, "-c",
                         FIXTURE_PROBE + "print(json.dumps(out))\n", json.dumps({"prefix": FIXTURE_PREFIX})],
                        capture=True, env=caller, fixture=fixture)
        problems = []
        if probe.returncode:
            problems.append(f"probe failed: {probe.stderr.strip()[-300:]}")
        else:
            seen = json.loads(probe.stdout)
            if seen.pop("environment"):
                problems.append("the caller's environment reached the sandbox (own or /proc/1/environ)")
            problems += seen.pop("fixture")
            seen.pop("tmpdir"), seen.pop("points")
            if seen.pop("control") != 0:
                problems.append("a fixture could not use Git in its own /tmp repository")
            problems += [f"{name}: {result}" for name, result in seen.items()
                         if result in ("allowed", 0)]
        if discover.returncode:
            problems.append(f"discovery-time import was not confined: {discover.stderr.strip()[-300:]}")
        if nested.returncode:
            problems.append(f"nested probe failed: {nested.stderr.strip()[-300:]}")
        else:
            inner = json.loads(nested.stdout)
            problems += [f"nested: {problem}" for problem in inner["fixture"]]
            if inner["tmpdir"] == fixture or Path(inner["tmpdir"]).parent != Path("/tmp"):
                problems.append(f"nested: the inner fixture {inner['tmpdir']} is not a fresh mount point in /tmp")
            if fixture not in inner["points"]:
                problems.append("nested: the outer fixture's mount entry was not inherited, so nesting was not tested")
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

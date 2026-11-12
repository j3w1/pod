"""Disposable, network-free installer trials. Nothing of the real profile is inherited.

Every installer base directory lives inside one temporary root, and PATH holds only that
root's `bin` and the system directories, so the real `pod` launcher cannot be selected.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import http.server
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tarfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "skills/pod"
VERSION = (ROOT / "VERSION").read_text().strip()


@contextmanager
def ignored_sigint():
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def restore_child_signals() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def source_names() -> list[str]:
    """What an install needs from the working tree: the bootstrap, VERSION and the skill directory."""
    names = ["install.sh", "VERSION"]
    for directory, subdirs, files in os.walk(BUNDLE):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        names += [(Path(directory) / name).relative_to(ROOT).as_posix() for name in files
                  if not name.endswith((".pyc", ".pyo"))]
    return sorted(names)


def archive_tree(target: Path, *, changes: dict[str, bytes | None] | None = None) -> Path:
    """Archive the working tree under one tar root, keeping VERSION's link; None removes a file."""
    changes = dict(changes or {})
    with tarfile.open(target, "w:gz") as archive:
        for name in sorted(set(source_names()) | set(changes)):
            if name not in changes:
                archive.add(ROOT / name, arcname="pod-source/" + name, recursive=False)
            elif changes[name] is not None:
                entry = tarfile.TarInfo("pod-source/" + name)
                entry.size, entry.mode = len(changes[name]), 0o644
                archive.addfile(entry, io.BytesIO(changes[name]))
    return target


STUB_NPX = '''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
args=sys.argv[1:]
log=os.environ.get('POD_TEST_NPX_LOG')
if log: pathlib.Path(log).write_text(json.dumps(args))
mode=os.environ.get('POD_TEST_NPX_MODE','copy')
if mode=='fail': sys.exit(17)
source=pathlib.Path(args[args.index('add')+1])/'skills'/'pod'
canonical=pathlib.Path(os.environ['HOME'])/'.agents/skills/pod'
if mode=='sleep':
 pathlib.Path(os.environ['POD_TEST_NPX_STARTED']).write_text('started')
 time.sleep(60)
if canonical.is_symlink(): sys.exit(18)
if canonical.exists(): shutil.rmtree(canonical)
if mode in ('half_copy','partial_fail'):
 canonical.mkdir(parents=True)
 shutil.copy2(source/'SKILL.md',canonical/'SKILL.md')
 if mode=='half_copy': time.sleep(30)
 sys.exit(19)
canonical.parent.mkdir(parents=True,exist_ok=True)
shutil.copytree(source,canonical,symlinks=False)
if mode=='tamper': (canonical/'errors.py').write_text('TAMPERED = True')
claude=pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR',str(pathlib.Path.home()/'.claude')))/'skills/pod'
claude.parent.mkdir(parents=True,exist_ok=True)
if claude.is_symlink(): claude.unlink()
if claude.exists(): sys.exit(20)
claude.symlink_to(os.path.relpath(canonical,claude.parent),target_is_directory=True)
'''


def sandbox(root: Path) -> tuple[dict[str, str], Path]:
    """A scrubbed home: every base directory the installer reads is inside `root`."""
    home, work, bin_dir = root / "home", root / "work", root / "bin"
    for path in (home, work, bin_dir, home / "tmp", home / "zsh"):
        path.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python3").symlink_to(sys.executable)
    (bin_dir / "npx").write_text(STUB_NPX)
    (bin_dir / "node").write_text("#!/bin/sh\nexit 0\n")
    for name in ("npx", "node"):
        (bin_dir / name).chmod(0o755)
    archive = archive_tree(root / "source.tar.gz")
    env = {
        "PATH": str(bin_dir) + ":/usr/bin:/bin", "HOME": str(home), "SHELL": "/bin/bash",
        "ZDOTDIR": str(home / "zsh"), "TMPDIR": str(home / "tmp"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "CODEX_HOME": str(home / "codex"), "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "npm_config_cache": str(home / "npm-cache"),
        "POD_INSTALL_SOURCE": archive.as_uri(), "POD_TEST_NPX_LOG": str(root / "npx.json"),
        "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8",
    }
    return env, archive


def run_install(env: dict[str, str], *, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(ROOT / "install.sh")], env=env, cwd=Path(env["HOME"]).parent / "work",
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def run_pod(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    launcher = Path(env["HOME"]) / ".local/bin/pod"
    return subprocess.run([str(launcher), *args], env=env, cwd=Path(env["HOME"]).parent / "work",
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


def doctor(env: dict[str, str]) -> dict:
    """`maintenance.doctor()` of the placed bundle, run in the sandbox as the launcher would."""
    skills = Path(env["HOME"]) / ".agents/skills"
    program = ("import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom pod import maintenance\n"
               "print(json.dumps(maintenance.doctor()))\n")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(skills)], env=env,
                            cwd=Path(env["HOME"]).parent / "work", stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def run_install_pty(env: dict[str, str], *, timeout: float = 60) -> tuple[int, bytes]:
    """Run the real shell entry point in a disposable 80x24 terminal."""
    work = Path(env["HOME"]).parent / "work"
    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.chdir(work)
        os.execve("/bin/sh", ["sh", str(ROOT / "install.sh")], env)
    output = bytearray()
    deadline = time.monotonic() + timeout
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
            done, observed = os.waitpid(pid, os.WNOHANG)
            if done:
                status = observed
                if not ready:
                    break
        else:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise TimeoutError("Installer PTY timed out")
        if status is None:
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                status = 0
        return os.waitstatus_to_exitcode(status), bytes(output)
    finally:
        os.close(master)


@contextmanager
def local_server(directory: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


FIXTURE_SKILL = """---
name: pod
description: Use when the user invokes Pod to deliver an objective through supervised Orca workers.
license: MIT
compatibility: Linux, the latest stable CPython and Orca.
metadata:
  source: https://github.com/j3w1/pod
---

# Pod

- Planning: [planning](references/planning.md).
- The launcher: [script](scripts/pod.py).
"""


def fixture_skill(target: Path) -> Path:
    """A small valid skill tree with the real modules and its own text, independent of the shipped guidance."""
    import shutil
    destination = target / "pod"
    shutil.copytree(BUNDLE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "references", "SKILL.md"))
    (destination / "references").mkdir()
    (destination / "references/planning.md").write_text("# Planning\n\nPlan the work.\n")
    (destination / "SKILL.md").write_text(FIXTURE_SKILL)
    return destination

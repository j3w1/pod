"""Disposable PTY runner for the real bundled launcher and measured TUI latency."""

from __future__ import annotations

import argparse
import codecs
import fcntl
import json
import os
from pathlib import Path
import platform
import pty
import select
import signal
import statistics
import struct
import sys
import tempfile
import termios
import time
from typing import Callable

from tests.common import disposable_path

try:
    import pyte
    import wcwidth
except ImportError:
    pyte = None
    wcwidth = None

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "skills" / "pod" / "scripts" / "pod.py"


def dependencies_available() -> bool:
    return pyte is not None and wcwidth is not None


def fixture_config(path: Path) -> None:
    sys.path.insert(0, str(ROOT / "skills"))
    import yaml
    from pod.config import DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(DEFAULT, sort_keys=False))


def environment(home: Path, **extra: str) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items()
              if key in ("TMPDIR", "SYSTEMROOT")}
    result.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                   "XDG_DATA_HOME": str(home / "data"),
                   "XDG_STATE_HOME": str(home / "state"),
                   "CODEX_HOME": str(home / "codex"),
                   "CLAUDE_CONFIG_DIR": str(home / "claude"),
                   "PATH": disposable_path(home),
                   "ORCA_CLI_COMMAND": str(home / "missing-orca"),
                   "TERM": "xterm-256color", "LANG": "C.UTF-8"})
    result.update(extra)
    return result


class PtySession:
    def __init__(self, *, home: Path, cwd: Path, cols: int = 80, rows: int = 24,
                 env_extra: dict[str, str] | None = None, argv: list[str] | None = None,
                 launcher: Path = LAUNCHER):
        if not dependencies_available():
            raise RuntimeError("Install pinned pyte and wcwidth test dependencies")
        self.cols, self.rows = cols, rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.output = bytearray()
        self.closed = False
        env = environment(home, **(env_extra or {}))
        pid, master = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                os.chdir(cwd)
                os.execve(sys.executable, [sys.executable, "-I", str(launcher), *(argv or [])], env)
            except Exception:
                os._exit(127)
        self.pid, self.master = pid, master
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.set_blocking(master, False)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def lines(self) -> list[str]:
        return [line.rstrip() for line in self.screen.display]

    def text(self) -> str:
        return "\n".join(self.lines())

    def poll(self, timeout: float = .05) -> None:
        if self.closed:
            return
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return
        while True:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                break
            except OSError:
                self.closed = True
                break
            if not data:
                self.closed = True
                break
            self.output.extend(data)
            self.stream.feed(self.decoder.decode(data))
            if len(data) < 65536:
                break

    def send(self, value: str | bytes) -> None:
        os.write(self.master, value.encode() if isinstance(value, str) else value)

    def settle(self, *, quiet: float = .08, timeout: float = .6) -> None:
        """Collect a complete repaint for evidence after a key or resize."""
        deadline = time.monotonic() + timeout
        quiet_until = time.monotonic() + quiet
        size = len(self.output)
        while time.monotonic() < deadline:
            self.poll(.02)
            if len(self.output) != size:
                size = len(self.output)
                quiet_until = time.monotonic() + quiet
            if time.monotonic() >= quiet_until:
                return

    def wait_for(self, expected: str | Callable[["PtySession"], bool], *, timeout: float = 3.0) -> float:
        started = time.monotonic()
        match = (lambda: expected in self.text()) if isinstance(expected, str) else lambda: expected(self)
        while time.monotonic() - started < timeout:
            self.poll(.02)
            if match():
                return (time.monotonic() - started) * 1000
        raise AssertionError(f"Timed out waiting for {expected!r}; screen:\n{self.text()}")

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.screen.resize(lines=rows, columns=cols)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def close(self) -> None:
        if not self.closed:
            try:
                self.send("\x1bq")
            except OSError:
                pass
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self.closed = True
                    break
                self.poll(.02)
            if not self.closed:
                try:
                    os.killpg(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(self.pid, 0)
                except ChildProcessError:
                    pass
                self.closed = True
        else:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def _percentiles(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"samples": len(ordered), "p50_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[max(0, int(.95 * len(ordered) + .999999) - 1)], 1),
            "max_ms": round(max(ordered), 1)}


def measure() -> dict:
    if not dependencies_available():
        raise RuntimeError("Install tests/requirements-pty.txt into .venv-dev first")
    import curses
    sys.path.insert(0, str(ROOT / "skills"))
    from pod.config import load as load_config, set_model
    focus, save, refresh = [], [], []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home, work = root / "home", root / "work"
        home.mkdir(); work.mkdir()
        config = home / "config" / "pod" / "config.yaml"
        fixture_config(config)
        with PtySession(home=home, cwd=work) as session:
            session.wait_for("Details", timeout=4)
            for index in range(50):
                target = "Claude Sonnet 5" if index % 2 == 0 else "Claude Opus 5.5"
                session.send("\x1bOB" if index % 2 == 0 else "\x1bOA")
                focus.append(session.wait_for(lambda s: target in next(
                    (line for line in s.lines() if "Details" in line), ""), timeout=2))
            names = ("available", "preferred", "disabled")
            for _ in range(50):
                current = load_config(personal=config)["saved"]["claude-opus-5-5"]
                expected = names[(names.index(current) + 1) % 3]
                session.send(" ")
                save.append(session.wait_for(
                    lambda _s: load_config(personal=config)["saved"]["claude-opus-5-5"] == expected,
                    timeout=2))
            session.settle(quiet=.08, timeout=1)
            for index in range(20):
                current = load_config(personal=config)
                state = "preferred" if current["saved"]["claude-opus-5-5"] != "preferred" else "available"
                set_model(config, "claude-opus-5-5", state, displayed=current)
                refresh.append(session.wait_for(
                    lambda s: any(state.capitalize() in line and "Claude Opus 5.5" in line
                                  for line in s.lines()), timeout=2))
    cpu = "unknown"
    try:
        cpu = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                   if line.startswith("model name"))
    except (OSError, StopIteration):
        pass
    result = {"environment": {"kernel": platform.release(), "cpu": cpu,
                              "python": platform.python_version(),
                              "ncurses": str(curses.ncurses_version), "TERM": "xterm-256color",
                              "locale": "C.UTF-8", "size": "80x24",
                              "pyte": getattr(pyte, "__version__", "0.8.2"),
                              "wcwidth": getattr(wcwidth, "__version__", "0.2.13")},
              "focus": _percentiles(focus), "save": _percentiles(save),
              "refresh": _percentiles(refresh)}
    assert result["focus"]["p95_ms"] < 500
    assert result["save"]["p95_ms"] < 500
    assert result["refresh"]["max_ms"] <= 1000
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.pty_harness")
    parser.add_argument("command", choices=("measure",))
    args = parser.parse_args(argv)
    if args.command == "measure":
        print(json.dumps(measure(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

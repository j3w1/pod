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
import unittest

from tests.common import disposable_path

try:
    import pyte
    import wcwidth
except ImportError:
    pyte = None
    wcwidth = None

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "skills" / "pod" / "scripts" / "pod.py"
SOURCE_FIXTURES = ROOT / "tests" / "fixtures" / "sources"
# A test-only entry point: it serves fixture pages instead of network reads, then runs the real CLI.
FETCH_WRAPPER = """import os, sys, threading, time
sys.path.insert(0, {skills!r})
from pathlib import Path
from pod import sources
PAGES = {{sources.AA: "aa-leaderboard.html", sources.ANTHROPIC: "anthropic-models-overview.html",
          sources.OPENAI: "openai-models.html"}}
def fetch(url, *, policy=None, deadline=None, cancel=None, opener=None):
    log = os.environ.get("POD_TEST_FETCH_LOG")
    if log:
        with open(log, "a") as stream:
            stream.write(url + "\\n")
    mode = os.environ.get("POD_TEST_FETCH", "ok")
    if mode == "slow":
        for _ in range(200):
            if cancel is not None and cancel.is_set():
                raise sources.SourceError("not_attempted", "Refresh was cancelled")
            time.sleep(.05)
    if mode == "deny":
        raise sources.SourceError("access_denied", "HTTP 403 bot challenge")
    if mode == "collapse" and url == sources.SOURCES[0].url:
        return sources.Page(url, COLLAPSED)
    source = next(source for source in sources.SOURCES if source.url == url)
    return sources.Page(url, (Path({fixtures!r}) / PAGES[source.id]).read_text(encoding="utf-8"))
COLLAPSED = ("<table><tr><th>Model</th><th>Context Window</th><th>Creator</th>"
             "<th>Artificial Analysis Intelligence Index</th><th>Cost per TaskUSD</th><th>MedianTokens/s</th>"
             "<th>LatencyFirst Chunk (s)</th><th>TotalResponse (s)</th></tr><tr><td>GPT-6.1 Sol (xhigh)</td>"
             "<td>1M</td><td>OpenAI</td><td>51</td><td>$0.39</td><td>63</td><td>107.76</td><td>115.66</td></tr></table>")
sources.fetch = fetch
from pod.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def dependencies_available() -> bool:
    return pyte is not None and wcwidth is not None


def fixture_config(path: Path, **changes) -> None:
    """Every shipped route enabled; automatic refresh is off unless a test asks for it."""
    sys.path.insert(0, str(ROOT / "skills"))
    import yaml
    from pod.config import defaults
    document = defaults()
    document["refresh"] = "manual"
    document.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def fetch_launcher(directory: Path) -> Path:
    """Write the fixture-fetch entry point; it never reaches the network."""
    path = directory / "pod-fixture-fetch.py"
    path.write_text(FETCH_WRAPPER.format(skills=str(ROOT / "skills"), fixtures=str(SOURCE_FIXTURES)))
    return path


def environment(home: Path, **extra: str) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items()
              if key in ("TMPDIR", "SYSTEMROOT")}
    result.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                   "XDG_DATA_HOME": str(home / "data"),
                   "XDG_STATE_HOME": str(home / "state"),
                   "XDG_CACHE_HOME": str(home / "cache"),
                   # Any accidental live read fails at once instead of reaching a website.
                   "https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                   "http_proxy": "http://127.0.0.1:9", "no_proxy": "",
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
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
                signal.signal(signal.SIGINT, signal.SIG_DFL)
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

    def cell(self, row: int, column: int) -> dict[str, object]:
        """Decoded terminal cell, including foreground and background semantics."""
        char = self.screen.buffer[row][column]
        return {"text": char.data, "fg": char.fg, "bg": char.bg,
                "bold": char.bold, "reverse": char.reverse}

    def cells(self, row: int) -> list[dict[str, object]]:
        return [self.cell(row, column) for column in range(self.cols)]

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


class PtyCase(unittest.TestCase):
    """A disposable home with valid preferences, opened through the real launcher."""

    @classmethod
    def setUpClass(cls):
        if not dependencies_available():
            if os.environ.get("POD_REQUIRE_PTY") == "1":
                raise AssertionError("POD_REQUIRE_PTY=1 needs pinned pyte and wcwidth")
            raise unittest.SkipTest("pyte and wcwidth are optional local test dependencies")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.work = self.root / "home", self.root / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.config = self.home / "config" / "pod" / "config.yaml"
        self.cache = self.home / "cache" / "pod" / "models"
        self.fetch_log = self.root / "fetch.log"
        fixture_config(self.config)

    def open(self, *, cols: int = 80, rows: int = 24, fetch: str | None = None, wait: str = "[Det",
             **env: str) -> "PtySession":
        launcher = LAUNCHER
        if fetch is not None:
            launcher = fetch_launcher(self.root)
            env = {"POD_TEST_FETCH": fetch, "POD_TEST_FETCH_LOG": str(self.fetch_log), **env}
        session = PtySession(home=self.home, cwd=self.work, cols=cols, rows=rows, env_extra=env, launcher=launcher)
        self.addCleanup(session.close)
        session.wait_for(wait, timeout=6)
        session.settle()
        return session

    def fetches(self) -> int:
        return self.fetch_log.read_text().count("\n") if self.fetch_log.exists() else 0

    def heading(self, session: "PtySession") -> str:
        return _inspector_heading(session)


def _percentiles(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"samples": len(ordered), "p50_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[max(0, int(.95 * len(ordered) + .999999) - 1)], 1),
            "max_ms": round(max(ordered), 1)}


def _inspector_heading(session: "PtySession") -> str:
    lines = session.lines()
    bar = next((index for index, line in enumerate(lines) if "[Det" in line), None)
    return lines[bar + 1] if bar is not None and bar + 1 < len(lines) else ""


def measure() -> dict:
    if not dependencies_available():
        raise RuntimeError("Install tests/requirements-pty.txt into .venv-dev first")
    import curses
    import subprocess
    sys.path.insert(0, str(ROOT / "skills"))
    from pod.config import load as load_config, set_route
    focus, save, refresh, data, startup, imports = [], [], [], [], [], []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home, work = root / "home", root / "work"
        home.mkdir(); work.mkdir()
        config = home / "config" / "pod" / "config.yaml"
        fixture_config(config)
        log = root / "fetch.log"
        launcher = fetch_launcher(root)
        for _ in range(10):
            started = time.monotonic()
            with PtySession(home=home, cwd=work) as session:
                session.wait_for("[Det", timeout=6)
                startup.append((time.monotonic() - started) * 1000)
        for _ in range(10):
            completed = subprocess.run(
                [sys.executable, "-I", "-c", "import sys, time; sys.path.insert(0, sys.argv[1]); "
                 "started = time.perf_counter(); import pod.tui; print(time.perf_counter() - started)",
                 str(ROOT / "skills")], capture_output=True, text=True, check=True)
            imports.append(float(completed.stdout) * 1000)
        with PtySession(home=home, cwd=work, launcher=launcher,
                        env_extra={"POD_TEST_FETCH_LOG": str(log)}) as session:
            session.wait_for("[Det", timeout=6)
            first = _inspector_heading(session)
            for index in range(50):
                session.send("\x1bOB" if index % 2 == 0 else "\x1bOA")
                expected_change = index % 2 == 0
                focus.append(session.wait_for(
                    lambda s: (_inspector_heading(s) != first) == expected_change, timeout=2))
            key = None
            for _ in range(50):
                current = load_config(personal=config)
                if key is None:
                    key = next(name for name, value in current["routes"].items() if value == "enabled")
                    session.send("/" + key + "\r")
                    session.wait_for(lambda s: "1 rows" in s.text(), timeout=2)
                    current = load_config(personal=config)
                expected = "disabled" if current["routes"].get(key) == "enabled" else "enabled"
                session.send(" ")
                save.append(session.wait_for(
                    lambda _s: load_config(personal=config)["routes"].get(key) == expected, timeout=2))
                session.settle(quiet=.05, timeout=.5)
            session.settle(quiet=.08, timeout=1)
            for _ in range(20):
                current = load_config(personal=config)
                target = "disabled" if current["routes"].get(key) == "enabled" else "enabled"
                set_route(config, key, target, displayed=current)
                refresh.append(session.wait_for(
                    lambda s: target in s.lines()[3].split(), timeout=2))
            for _ in range(10):
                before = log.read_text().count("\n") if log.exists() else 0
                session.send("R")
                data.append(session.wait_for(
                    lambda s: (log.exists() and log.read_text().count("\n") >= before + 3
                               and "Data updated" in s.text() and "Refreshing" not in s.text()), timeout=3))
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
              "refresh": _percentiles(refresh), "data_refresh": _percentiles(data),
              "startup_to_first_frame": _percentiles(startup), "import_pod_tui": _percentiles(imports)}
    assert result["focus"]["p95_ms"] < 500
    assert result["save"]["p95_ms"] < 500
    assert result["refresh"]["max_ms"] <= 1000
    assert result["data_refresh"]["max_ms"] <= 1000
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

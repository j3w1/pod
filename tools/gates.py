"""Run the local verification commands documented in docs/validation.md."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
    # Always the checkout whose gates run, never the caller's current directory.
    result = subprocess.run(["git", "-C", str(root), *args], input=input_bytes, capture_output=True,
                            check=True)
    return result.stdout.decode().strip()


def commands(interpreter: str, empty_tree: str, root: Path) -> dict[str, list[str]]:
    return {
        "unit": [interpreter, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "incidents": [interpreter, "-m", "unittest", "discover", "-s", "tests/incidents",
                      "-t", ".", "-v"],
        "pty": [interpreter, "-m", "unittest", "tests.test_tui_pty",
                "tests.test_sparse_toggle_pty", "tests.test_catalog_optional_pty", "-v"],
        "installer": [interpreter, "-m", "unittest", "tests.test_installer",
                      "tests.test_bundle_install", "-v"],
        "compile": [interpreter, "-m", "compileall", "-q", "skills", "tests", "tools"],
        "whitespace": ["git", "diff", "--check", empty_tree, "HEAD"],
        "skill": [interpreter, "-m", "pod.skill_validation", "skills/pod"],
        "catalog": [interpreter, "-m", "pod.catalog", "--check"],
        "source": [interpreter, "tools/source_audit.py", "."],
        "traces": [interpreter, "tools/trace_check.py",
                   *(str(path.relative_to(root))
                     for path in sorted((root / "tests/fixtures/traces").glob("*.json")))],
    }


def ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for summary.json and gate logs")
    parser.add_argument("--gate", action="append",
                        choices=("unit", "incidents", "pty", "installer", "compile",
                                 "whitespace", "skill", "catalog", "source", "traces"),
                        help="run only this gate; repeat to select several")
    parser.add_argument("--sigint-ignored", action="store_true",
                        help="run each gate with SIGINT ignored")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    empty_tree = git(root, "hash-object", "-t", "tree", "--stdin", input_bytes=b"")
    selected = commands(sys.executable, empty_tree, root)
    if args.gate:
        selected = {name: command for name, command in selected.items()
                    if name in args.gate}
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "skills"
    summary = {
        "candidate": git(root, "rev-parse", "HEAD"),
        "dirty": bool(git(root, "status", "--porcelain")),
        "host": platform.node(),
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interpreter": sys.executable,
        "gates": {},
    }
    for name, command in selected.items():
        if name == "pty":
            environment["POD_REQUIRE_PTY"] = "1"
        else:
            environment.pop("POD_REQUIRE_PTY", None)
        log = output / f"{name}.log"
        started = time.monotonic()
        with log.open("wb") as stream:
            try:
                result = subprocess.run(command, cwd=root, env=environment, stdout=stream,
                                        stderr=subprocess.STDOUT,
                                        preexec_fn=ignore_sigint if args.sigint_ignored else None)
                exit_code = result.returncode
            except OSError as error:
                stream.write(f"Could not start gate: {error}\n".encode())
                exit_code = 127
        summary["gates"][name] = {
            "command": command,
            "exit_code": exit_code,
            "duration_seconds": round(time.monotonic() - started, 3),
            "log": str(log),
        }
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    for name, result in summary["gates"].items():
        print(f"{name}: {'PASS' if result['exit_code'] == 0 else 'FAIL'} ({result['log']})")
    print(f"summary: {summary_path}")
    return int(any(result["exit_code"] != 0 for result in summary["gates"].values()))


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit reviewed-checkout installer into a caller-selected isolated environment.

This file is intentionally not a console entry point. Running it is an explicit
installation action; ordinary Pod setup never invokes it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import venv


def plan(checkout: Path, environment: Path, expected_commit: str) -> dict:
    if sys.version_info < (3, 13):
        raise RuntimeError("Pod requires Python 3.13 or newer")
    if sys.platform not in ("linux", "win32"):
        raise RuntimeError("Pod isolated installation supports native Linux and Windows")
    if not expected_commit or len(expected_commit) < 7:
        raise RuntimeError("Provide the reviewed checkout commit")
    if checkout.is_symlink() or not (checkout / "pyproject.toml").is_file():
        raise RuntimeError("Checkout path is not a regular reviewed source tree")
    if environment == checkout or checkout in environment.parents:
        raise RuntimeError("Select an isolated environment outside the checkout")
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"],
                           capture_output=True, text=True, check=True).stdout
    if not head.startswith(expected_commit) or dirty:
        raise RuntimeError("Checkout does not match the reviewed clean commit")
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return {"checkout_commit": head, "environment": str(environment),
            "install_command": [str(python), "-m", "pip", "install", "--no-input", str(checkout)]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--venv", required=True, type=Path)
    p.add_argument("--expected-commit", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    checkout = Path(__file__).resolve().parent
    environment = args.venv.expanduser().resolve()
    try:
        result = plan(checkout, environment, args.expected_commit)
        if not args.dry_run:
            if environment.exists():
                raise RuntimeError("Selected environment already exists; choose a fresh path")
            venv.EnvBuilder(with_pip=True, clear=False).create(environment)
            subprocess.run(result["install_command"], check=True)
        print(" ".join(result["install_command"]))
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"pod installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

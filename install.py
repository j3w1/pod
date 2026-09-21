"""Explicit reviewed-checkout installer into a caller-selected isolated environment.

This file is intentionally not a console entry point. Running it is an explicit
installation action; ordinary Pod setup never invokes it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import venv


MINIMUM_PIP = (22, 3)


def _subprocess_environment() -> dict[str, str]:
    """Return process-only isolation that also survives pip's --python re-exec."""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("PYTHON", "PIP_", "_PIP_"))
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def _run(command: list[str], stage: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, env=env)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"{stage} failed with exit {exc.returncode}{suffix}") from exc
    except OSError as exc:
        raise RuntimeError(f"{stage} failed: {exc}") from exc


def _bootstrap_pip(environment: dict[str, str]) -> tuple[int, ...]:
    command = [sys.executable, "-I", "-m", "pip", "--isolated", "--version"]
    try:
        result = _run(command, "bootstrap pip preflight", env=environment)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Bootstrap pip is unavailable; install pip 22.3 or newer for {sys.executable}: {exc}"
        ) from exc
    match = re.match(r"^pip\s+(\d+)\.(\d+)(?:\.(\d+))?", result.stdout.strip())
    if not match:
        raise RuntimeError("Bootstrap pip reported an unrecognized version; install pip 22.3 or newer for this interpreter")
    version = tuple(int(part or 0) for part in match.groups())
    if version < MINIMUM_PIP:
        rendered = ".".join(str(part) for part in version)
        raise RuntimeError(f"Bootstrap pip {rendered} is unsupported; install pip 22.3 or newer for this interpreter")
    return version


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
    head = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                "checkout commit inspection").stdout.strip()
    dirty = _run(["git", "-C", str(checkout), "status", "--porcelain"],
                 "checkout cleanliness inspection").stdout
    if not head.startswith(expected_commit) or dirty:
        raise RuntimeError("Checkout does not match the reviewed clean commit")
    process_environment = _subprocess_environment()
    pip_version = _bootstrap_pip(process_environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return {"checkout_commit": head, "environment": str(environment), "target_python": str(python),
            "pip_version": pip_version,
            "install_command": [sys.executable, "-I", "-m", "pip", "--python", str(python),
                                "--isolated", "install",
                                "--no-input", "--no-warn-script-location", str(checkout)],
            "verification_command": [str(python), "-I", "-c",
                                     "from importlib.metadata import distribution; "
                                     "distribution('j3w1-pod'); import pod"]}


def execute(checkout: Path, environment: Path, expected_commit: str) -> dict:
    result = plan(checkout, environment, expected_commit)
    if environment.exists():
        raise RuntimeError("Selected environment already exists; choose a fresh path")
    try:
        venv.EnvBuilder(with_pip=False, clear=False).create(environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Target environment creation failed: {exc}") from exc
    target = Path(result["target_python"])
    if not target.is_file():
        raise RuntimeError(f"Target environment creation did not produce its Python interpreter: {target}")
    process_environment = _subprocess_environment()
    _run(result["install_command"], "isolated Pod installation", env=process_environment)
    _run(result["verification_command"], "installed Pod verification", env=process_environment)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--venv", required=True, type=Path)
    p.add_argument("--expected-commit", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    checkout = Path(__file__).resolve().parent
    environment = args.venv.expanduser().resolve()
    try:
        result = (plan(checkout, environment, args.expected_commit) if args.dry_run
                  else execute(checkout, environment, args.expected_commit))
        print(" ".join(result["install_command"]))
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"pod installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

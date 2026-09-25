"""Bounded, read-only Git observations shared by the kernel."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def read(root: Path, argv: list[str], *, binary: bool = False,
         timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *argv],
                              capture_output=True, text=not binary, timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_commit(root: Path, ref: str) -> str | None:
    if not isinstance(ref, str) or not ref or "\x00" in ref or ref.startswith("-"):
        return None
    completed = read(root, ["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"])
    value = completed.stdout.strip() if completed is not None and completed.returncode == 0 else ""
    return value if OBJECT_ID.fullmatch(value) else None


def is_ancestor(root: Path, commit: str, candidate: str) -> bool | None:
    if not isinstance(commit, str) or not OBJECT_ID.fullmatch(commit):
        return None
    target = resolve_commit(root, candidate)
    if target is None:
        return None
    completed = read(root, ["merge-base", "--is-ancestor", commit, target])
    if completed is None or completed.returncode not in (0, 1):
        return None
    return completed.returncode == 0


def name_status_paths(output: str) -> set[str]:
    """Parse Git's NUL-delimited diff format, including both rename paths."""
    fields = output.split("\0")
    changed: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        width = 2 if fields[index][:1] in ("R", "C") else 1
        changed.update(item for item in fields[index + 1:index + 1 + width] if item)
        index += 1 + width
    return changed

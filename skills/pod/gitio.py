"""Bounded, read-only Git observations shared by the kernel."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable


OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def worktree_rows(stream):
    """Read NUL-delimited porcelain incrementally, including paths containing newlines."""
    from .errors import PodError
    pending, current, seen = "", {}, False
    while chunk := stream.read(65536):
        fields = (pending + chunk).split("\0")
        pending = fields.pop()
        for field in fields:
            if field:
                key, _, value = field.partition(" ")
                if key in current or not current and key != "worktree":
                    raise PodError("git_contract", "Git worktree inventory record is malformed")
                current[key] = value
            elif current:
                if (not Path(current.get("worktree", "")).is_absolute()
                        or ("bare" in current and (current["bare"] or
                            any(key in current for key in ("HEAD", "branch", "detached"))))
                        or ("bare" not in current and (
                            not OBJECT_ID.fullmatch(current.get("HEAD", ""))
                            or ("branch" in current) == ("detached" in current)
                            or "branch" in current and not current["branch"].startswith("refs/heads/")
                            or current.get("detached", "")))):
                    raise PodError("git_contract", "Git worktree inventory record is incomplete")
                seen = True
                yield current
                current = {}
    if pending or current or not seen:
        raise PodError("git_contract", "Git worktree inventory is incomplete")


@contextmanager
def worktree_inventory(root: Path, *, executable: str = "git"):
    """Spool the whole read to disk, independent of repository worktree count and bytes."""
    from .errors import PodError
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as inventory:
            result = subprocess.run([executable, "--no-optional-locks", "-C", str(root),
                                     "worktree", "list", "--porcelain", "-z"],
                                    stdout=inventory, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    timeout=30 if budget is None else budget.limit(30), check=False)
            if result.returncode:
                raise PodError("repository_identity_unavailable", "Git worktree inventory cannot be read")
            inventory.seek(0)
            yield inventory
    except UnicodeError as exc:
        raise PodError("git_contract", "Git worktree inventory is not valid UTF-8") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if budget is not None and isinstance(exc, subprocess.TimeoutExpired):
            budget.check()
        raise PodError("git_unavailable", "Git worktree inventory cannot be read") from exc


def read(root: Path, argv: list[str], *, binary: bool = False,
         timeout: int = 30) -> subprocess.CompletedProcess | None:
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *argv],
                              capture_output=True, text=not binary,
                              timeout=timeout if budget is None else budget.limit(timeout),
                              check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:  # Output that is not UTF-8 is no answer.
        if budget is not None and isinstance(exc, subprocess.TimeoutExpired):
            budget.check()
        return None


def resolve_commit(root: Path, ref: str) -> str | None:
    if not isinstance(ref, str) or not ref or "\x00" in ref or ref.startswith("-"):
        return None
    completed = read(root, ["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"])
    value = completed.stdout.strip() if completed is not None and completed.returncode == 0 else ""
    return value if OBJECT_ID.fullmatch(value) else None


def is_ancestor(root: Path, commit: str, candidate: str, *,
                resolve: Callable[[Path, str], str | None] = resolve_commit) -> bool | None:
    if not isinstance(commit, str) or not OBJECT_ID.fullmatch(commit):
        return None
    target = resolve(root, candidate)
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


def require_candidate(root: Path, candidate: object, *, assurance: bool = False) -> None:
    """Refuse mutable Git names without rewriting the caller's proof identity."""
    from .errors import PodError
    from .util import bounded_text
    bounded_text(candidate, name="candidate", limit=128 if assurance else 512)
    repository = read(root, ["rev-parse", "--git-dir"])
    if repository is None:
        raise PodError("git_unavailable", "Git cannot determine the candidate's repository context")
    if repository.returncode != 0:
        return
    resolved = resolve_commit(root, candidate)
    if not OBJECT_ID.fullmatch(candidate) or (not assurance and resolved != candidate):
        raise PodError("obligation_invalid" if assurance else "invalid_checkpoint",
                       "candidate must be a full Git object id" + (f"; resolves to {resolved}" if resolved else ""),
                       {"detail": "malformed" if assurance else "candidate_unresolved", "field": "candidate", "resolved": resolved,
                        "next_action": "restate candidate as its full object id; proof bound to the old text must be reconciled"})

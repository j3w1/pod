"""Bounded, read-only Git observations shared by the kernel."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
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


# Every Pod read sees the repository's own objects and history, exactly as Git stores them:
# - replacement refs (refs/replace) are not applied, so an object id names its own content;
# - grafts (info/grafts) and shallow boundaries (shallow) are not applied, so a commit has its recorded
#   parents; a parent missing from a shallow clone makes the read fail, which callers treat as unknown;
# - pathspecs are literal, so a path names only itself;
# - text output is strict UTF-8 without newline translation, so a carriage return in a path stays one.
# Alternates only add storage for the same content-addressed objects; blob readers verify their bytes.
_READ_ENVIRONMENT = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull, "GIT_SHALLOW_FILE": os.devnull,
                     "GIT_LITERAL_PATHSPECS": "1"}


def read(root: Path, argv: list[str], *, binary: bool = False,
         timeout: int = 30) -> subprocess.CompletedProcess | None:
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    try:
        completed = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "-C", str(root), *argv],
                                   capture_output=True, text=False,
                                   timeout=timeout if budget is None else budget.limit(timeout),
                                   check=False, stdin=subprocess.DEVNULL,
                                   env={**os.environ, **_READ_ENVIRONMENT})
        if not binary:
            completed.stdout, completed.stderr = (
                value.decode("utf-8") if isinstance(value, bytes) else value
                for value in (completed.stdout, completed.stderr))
        return completed
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:  # Output that is not UTF-8 is no answer.
        if budget is not None and isinstance(exc, subprocess.TimeoutExpired):
            budget.check()
        return None


def tree_entries(root: Path, commit: str, *, reader: Callable | None = None) -> dict[str, tuple[str, str, str]] | None:
    """Every path in one commit's tree, recursively with its trees, as exact names: {path: (mode, type, id)}.

    The NUL-delimited listing is parsed as bytes and each name decoded as strict UTF-8, so distinct Git names stay
    distinct; a name that is not UTF-8 is left out, since no Pod path can name it. None when Git cannot list it.
    """
    if not isinstance(commit, str) or not OBJECT_ID.fullmatch(commit):
        return None
    listed = (reader or read)(root, ["ls-tree", "-r", "-t", "-z", "--full-tree", commit], binary=True)
    if listed is None or listed.returncode != 0 or not isinstance(listed.stdout, bytes):
        return None
    rows: dict[str, tuple[str, str, str]] = {}
    for entry in listed.stdout.split(b"\0"):
        if not entry:
            continue
        meta, tab, raw = entry.partition(b"\t")
        parts = meta.split(b" ")
        try:
            name, fields = raw.decode("utf-8"), [part.decode("ascii") for part in parts]
        except UnicodeError:
            continue
        if not tab or not name or len(fields) != 3 or not OBJECT_ID.fullmatch(fields[2]):
            return None
        rows[name] = (fields[0], fields[1], fields[2])
    return rows


def tree_entry(root: Path, commit: str, path: str, *,
               reader: Callable | None = None) -> tuple[bool, tuple[str, str, str] | None]:
    """(readable, (mode, type, id) or None when absent) for one exact path at one commit, without recursion."""
    if not isinstance(commit, str) or not OBJECT_ID.fullmatch(commit) or not isinstance(path, str) or not path:
        return False, None
    listed = (reader or read)(root, ["ls-tree", "-z", commit, "--", path], binary=True)
    if listed is None or listed.returncode != 0 or not isinstance(listed.stdout, bytes):
        return False, None
    exact = path.encode("utf-8")
    for entry in listed.stdout.split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        if tab and name == exact:
            parts = meta.split(b" ")
            try:
                fields = [part.decode("ascii") for part in parts]
            except UnicodeError:
                return False, None
            if len(fields) != 3 or not OBJECT_ID.fullmatch(fields[2]):
                return False, None
            return True, (fields[0], fields[1], fields[2])
    return True, None


def blob(root: Path, obj: str, *, limit: int, reader: Callable | None = None) -> bytes | None:
    """One blob's bytes within `limit`, verified against its object id; None when unreadable, larger or different."""
    if not isinstance(obj, str) or not OBJECT_ID.fullmatch(obj):
        return None
    run = reader or read
    size = run(root, ["cat-file", "-s", obj])
    text = size.stdout.strip() if size is not None and size.returncode == 0 and isinstance(size.stdout, str) else ""
    if not text.isdigit() or int(text) > limit:
        return None
    shown = run(root, ["cat-file", "blob", obj], binary=True)
    data = shown.stdout if shown is not None and shown.returncode == 0 else None
    if not isinstance(data, bytes) or len(data) != int(text):
        return None
    algorithm = "sha1" if len(obj) == 40 else "sha256"
    return data if hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest() == obj else None


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

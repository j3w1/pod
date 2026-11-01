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
                                    env=read_environment(),
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


# Every Pod read sees this repository's own objects and history, exactly as Git stores them:
# - the environment is an allow-list: no ambient GIT_DIR, GIT_OBJECT_DIRECTORY, alternates variable or other
#   GIT_* redirection reaches Git, so `-C root` alone selects the repository; only two variables that can only
#   narrow what Git reads (a discovery ceiling and skipping system config) are kept;
# - replacement refs, grafts (info/grafts) and shallow boundaries are not applied, so an object id names its own
#   content and a commit its recorded parents; a parent missing from a shallow clone makes the read fail, which
#   callers treat as unknown;
# - lazy fetching is off, so a missing object is unreadable and no read reaches a remote;
# - pathspecs are literal, and text output is strict UTF-8 without newline translation.
# Object storage itself (including alternates) is not trusted for proof: TreeView and blob rehash every object.
_KEPT_ENVIRONMENT = frozenset({"PATH", "HOME", "LANG", "TZ", "TMPDIR", "XDG_CONFIG_HOME",
                               "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_NOSYSTEM"})
_READ_ENVIRONMENT = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull, "GIT_SHALLOW_FILE": os.devnull,
                     "GIT_LITERAL_PATHSPECS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}


def read_environment() -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items()
               if key in _KEPT_ENVIRONMENT or key.startswith("LC_")}, **_READ_ENVIRONMENT}


def read(root: Path, argv: list[str], *, binary: bool = False,
         timeout: int = 30) -> subprocess.CompletedProcess | None:
    from .orca import _DEADLINE
    budget = _DEADLINE.get()
    try:
        completed = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "-C", str(root), *argv],
                                   capture_output=True, text=False,
                                   timeout=timeout if budget is None else budget.limit(timeout),
                                   check=False, stdin=subprocess.DEVNULL, env=read_environment())
        if not binary:
            completed.stdout, completed.stderr = (
                value.decode("utf-8") if isinstance(value, bytes) else value
                for value in (completed.stdout, completed.stderr))
        return completed
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:  # Output that is not UTF-8 is no answer.
        if budget is not None and isinstance(exc, subprocess.TimeoutExpired):
            budget.check()
        return None


MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_COMMIT_BYTES = 1_048_576
MAX_TREES = 4096
MAX_DEPTH = 256
UNREADABLE = "unreadable"  # TreeView.entry: Pod cannot establish the path either way


def _verified_object(root: Path, obj: str, kind: str, *, limit: int, reader: Callable | None) -> bytes | None:
    """One object's raw content within `limit`, rehashed against its id with its claimed type."""
    if not isinstance(obj, str) or not OBJECT_ID.fullmatch(obj):
        return None
    run = reader or read
    size = run(root, ["cat-file", "-s", obj])
    text = size.stdout.strip() if size is not None and size.returncode == 0 and isinstance(size.stdout, str) else ""
    if not text.isdigit() or int(text) > limit:
        return None
    shown = run(root, ["cat-file", kind, obj], binary=True)
    data = shown.stdout if shown is not None and shown.returncode == 0 else None
    if not isinstance(data, bytes) or len(data) != int(text):
        return None
    algorithm = "sha1" if len(obj) == 40 else "sha256"
    header = kind.encode("ascii") + b" %d\0" % len(data)
    return data if hashlib.new(algorithm, header + data).hexdigest() == obj else None


def blob(root: Path, obj: str, *, limit: int, reader: Callable | None = None) -> bytes | None:
    """One blob's bytes within `limit`, verified against its object id; None when unreadable, larger or different."""
    return _verified_object(root, obj, "blob", limit=limit, reader=reader)


def _tree_rows(data: bytes, width: int) -> dict[str, tuple[str, str, str]] | None:
    """A verified tree's entries by exact name; None for a malformed tree.

    A name that is not UTF-8 is left out (no Pod path can name it); a name that is empty, `.`, `..` or contains
    `/`, a non-numeric mode or a repeated name makes the whole tree malformed."""
    rows: dict[str, tuple[str, str, str]] = {}
    index = 0
    while index < len(data):
        space = data.find(b" ", index)
        nul = data.find(b"\0", space + 1) if space >= 0 else -1
        if space < 0 or nul < 0 or nul + 1 + width > len(data):
            return None
        mode, name, obj = data[index:space], data[space + 1:nul], data[nul + 1:nul + 1 + width].hex()
        index = nul + 1 + width
        if not mode.isdigit() or not name or name in (b".", b"..") or b"/" in name:
            return None
        try:
            text = name.decode("utf-8")
        except UnicodeError:
            continue
        if text in rows:
            return None
        mode_text = mode.decode("ascii").zfill(6)
        rows[text] = (mode_text, "tree" if mode_text == "040000" else "commit" if mode_text == "160000" else "blob",
                      obj)
    return rows


_GIT_TRUE = frozenset({"true", "yes", "on", "1"})
_GIT_FALSE = frozenset({"false", "no", "off", "0", ""})


def git_bool(value: str | None) -> bool | None:
    """A boolean Git setting under Git's rules, case-insensitively: a valueless key (None) and `true`, `yes`, `on`
    or `1` are true; `false`, `no`, `off`, `0` and the empty string are false; anything else is unknown (None)."""
    if value is None:
        return True
    lowered = value.lower()
    return True if lowered in _GIT_TRUE else False if lowered in _GIT_FALSE else None


def partial_clone(root: Path, *, reader: Callable | None = None) -> bool | None:
    """Whether the repository is a partial clone or has a promisor remote; None when its config is unreadable or a
    remote's `promisor` is not a Git boolean. Entries are read with `--null`, so a valueless key stays distinct
    from an empty value."""
    listed = (reader or read)(root, ["config", "--null", "--get-regexp",
                                     r"^(extensions\.partialclone|remote\..*\.promisor)$"])
    if listed is None or listed.returncode not in (0, 1) or not isinstance(listed.stdout, str):
        return None
    flags = []
    for entry in listed.stdout.split("\0"):
        if not entry:
            continue
        key, newline, value = entry.partition("\n")
        flags.append(True if key == "extensions.partialclone" else git_bool(value if newline else None))
    return True if True in flags else None if None in flags else False


class TreeView:
    """One commit's tree read through verified objects: the commit, each tree on a path and (via `blob`) its blob
    are rehashed against their ids, and membership is parsed from those verified bytes, so forged storage reads as
    unreadable rather than as other content. A partial clone or promisor repository is refused outright. Trees are
    cached for the view's life; at most MAX_TREES trees and MAX_DEPTH components are read."""

    def __init__(self, root: Path, commit: str, *, reader: Callable | None = None):
        self.root, self.commit, self.reader = root, commit, reader
        self._top: str | None | bool = False
        self._trees: dict[str, dict | None] = {}

    def _rows(self, obj: str) -> dict | None:
        if obj not in self._trees:
            if len(self._trees) >= MAX_TREES:
                return None
            data = _verified_object(self.root, obj, "tree", limit=MAX_TREE_BYTES, reader=self.reader)
            self._trees[obj] = None if data is None else _tree_rows(data, len(obj) // 2)
        return self._trees[obj]

    def top(self) -> str | None:
        """The verified root tree id, or None when the commit or repository cannot be trusted."""
        if self._top is False:
            self._top = None
            if partial_clone(self.root, reader=self.reader) is False:
                data = _verified_object(self.root, self.commit, "commit", limit=MAX_COMMIT_BYTES,
                                        reader=self.reader)
                head = data.split(b"\n", 1)[0] if data is not None else b""
                tree = head[5:].decode("ascii", "replace") if head.startswith(b"tree ") else ""
                if len(tree) == len(self.commit) and OBJECT_ID.fullmatch(tree) and self._rows(tree) is not None:
                    self._top = tree
        return self._top

    def entry(self, path: str):
        """(mode, type, id) for one exact path, None when absent (a parent that is not a tree included), or
        UNREADABLE."""
        top = self.top()
        parts = path.split("/") if isinstance(path, str) else []
        if top is None or not parts or len(parts) > MAX_DEPTH or any(part in ("", ".", "..") for part in parts):
            return UNREADABLE
        rows = self._rows(top)
        for part in parts[:-1]:
            found = rows.get(part)
            if found is None or found[0] != "040000":
                return None
            rows = self._rows(found[2])
            if rows is None:
                return UNREADABLE
        return rows.get(parts[-1])


def tree_entry(root: Path, commit: str, path: str, *,
               reader: Callable | None = None) -> tuple[bool, tuple[str, str, str] | None]:
    """(readable, (mode, type, id) or None when absent) for one exact path at one commit, through TreeView."""
    found = TreeView(root, commit, reader=reader).entry(path)
    return (False, None) if found == UNREADABLE else (True, found)


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

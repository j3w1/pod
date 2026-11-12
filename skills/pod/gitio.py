"""Reads from Git. Pod never copies what Git holds; it asks again when it needs the answer."""

import hashlib
from pathlib import Path
import posixpath
import re
import subprocess

from .errors import PodError

SHA = re.compile(r"[0-9a-f]{40}")


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("git_unavailable", f"git {args[0]} did not complete: {exc}", fix="check the repository and retry") from exc
    if check and done.returncode != 0:
        raise PodError("git_failed", f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()[:300]}",
                       fix="resolve the Git error and retry")
    return done


def _text(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.decode().strip()


def root(cwd: Path) -> Path:
    done = git(cwd, "rev-parse", "--show-toplevel", check=False)
    if done.returncode != 0:
        raise PodError("not_a_repository", f"{cwd} is not inside a Git worktree.", fix="run Pod from the objective's worktree")
    return Path(done.stdout.decode().strip())


def repository(cwd: Path) -> tuple[str, str | None]:
    """A stable id for this repository's logs, and its GitHub `owner/name` when the origin names one."""
    url = git(cwd, "config", "--get", "remote.origin.url", check=False).stdout.decode().strip()
    match = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    if match:
        slug = f"{match[1]}/{match[2]}"
        return re.sub(r"[^a-z0-9._-]", "-", slug.lower().replace("/", "--")), slug
    common = Path(_text(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return "local-" + hashlib.sha256(str(common).encode()).hexdigest()[:16], None


def commit(cwd: Path, ref: str = "HEAD") -> str:
    done = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    value = done.stdout.decode().strip()
    if done.returncode != 0 or not SHA.fullmatch(value):
        raise PodError("commit_unknown", f"{ref} does not name a commit in this repository.",
                       fix="fetch it or name an existing commit")
    return value


def known(cwd: Path, ref: str) -> str | None:
    """The commit a ref names, or None when this repository has no such ref."""
    try:
        return commit(cwd, ref)
    except PodError:
        return None


def tree(cwd: Path, ref: str = "HEAD") -> str:
    return _text(cwd, "rev-parse", "--verify", f"{ref}^{{tree}}")


def branch(cwd: Path) -> str | None:
    value = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.decode().strip()
    return value or None


def dirty(cwd: Path, *, untracked: bool = True) -> list[str]:
    """Paths with uncommitted changes, and untracked files unless told to leave them out."""
    out = git(cwd, "status", "--porcelain=v1", "-z", f"--untracked-files={'normal' if untracked else 'no'}").stdout.decode(errors="replace")
    return [item[3:] for item in out.split("\0") if item]


def fetch(cwd: Path, remote: str, name: str) -> str | None:
    """Bring the remote branch up to date and return its commit; None when it cannot be read."""
    if git(cwd, "fetch", "--quiet", "--no-tags", remote, name, check=False).returncode != 0:
        return None
    done = git(cwd, "rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}", check=False)
    return done.stdout.decode().strip() or None


def merge_base(cwd: Path, one: str, other: str) -> str | None:
    done = git(cwd, "merge-base", one, other, check=False)
    return done.stdout.decode().strip() or None


def is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def changed(cwd: Path, base: str, head: str) -> list[str]:
    """Every path whose content differs between two commits; a rename is both of its paths."""
    out = git(cwd, "diff", "--name-only", "--no-renames", "-z", base, head).stdout.decode(errors="surrogateescape")
    return sorted(item for item in out.split("\0") if item)


def blobs(cwd: Path, ref: str, paths_: list[str]) -> dict:
    """File mode and blob id per path at a commit, as one comparable value; a path absent there maps to None."""
    result = dict.fromkeys(paths_)
    if not paths_:
        return result
    out = git(cwd, "ls-tree", "-r", "-z", ref).stdout.decode(errors="surrogateescape")
    wanted = set(paths_)
    for item in out.split("\0"):
        if not item:
            continue
        meta, _, name = item.partition("\t")
        if name in wanted:
            result[name] = f"{meta.split()[0]} {meta.split()[2]}"
    return result


def identities(cwd: Path, ref: str, names: list[str]) -> dict:
    """What each path is at a commit, file or directory alike: a directory changes when anything under it does.

    `.` is the whole tree. A path absent at the commit maps to None.
    """
    result = dict.fromkeys(names)
    for name in names:
        if name == ".":
            result[name] = tree(cwd, ref)
            continue
        out = git(cwd, "ls-tree", "-z", ref, "--", name).stdout.decode(errors="surrogateescape")
        entry = next((item for item in out.split("\0") if item.partition("\t")[2] == name), None)
        result[name] = entry.partition("\t")[0] if entry else None
    return result


def show(cwd: Path, ref: str, name: str) -> str | None:
    """A file's text at a commit, or None when it is not there."""
    done = git(cwd, "show", f"{ref}:{name}", check=False)
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None


def declared(values: list[str] | None, *, keep_root: bool = False) -> list[str]:
    """Declared paths in one spelling, relative to the repository root. A path that leaves it is refused.

    The root itself means the whole tree: an empty list for paths a worker may change, or `.` when
    `keep_root` asks for it by name.
    """
    result = []
    for value in values or []:
        if not value.strip():
            continue
        name = posixpath.normpath(value.strip())
        if name.startswith("/") or name == ".." or name.startswith("../"):
            raise PodError("path_invalid", f"{value!r} is not a path inside the repository.",
                           fix="name paths relative to the repository root, such as src/app")
        if name != ".":
            result.append(name)
        else:
            return ["."] if keep_root else []
    return result


def under(name: str, prefixes: list[str]) -> bool:
    """Whether a path is one of the declared paths or inside a declared directory."""
    return any(name == prefix.rstrip("/") or name.startswith(prefix.rstrip("/") + "/") or prefix in (".", "")
               for prefix in prefixes)


def overlap(one: list[str], other: list[str]) -> bool:
    """Whether two declared path sets can touch the same file. An empty set means the whole tree."""
    if not one or not other:
        return True
    return any(under(a.rstrip("/"), [b]) or under(b.rstrip("/"), [a]) for a in one for b in other)

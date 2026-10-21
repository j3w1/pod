"""Private whole-worktree recovery verification. Archive content is never Git configuration."""

from __future__ import annotations

from contextlib import contextmanager
from copy import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
import time

from . import cleanup_archives as git
from . import cleanup_observations as observations
from .errors import PodError
from .util import digest, exact

MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_MANIFEST = 2_000_000
MAX_PATH = 4096
SECONDS = 30
MANIFEST = "pod-worktree-archive.json"
ADMIN_FILES = {"HEAD", "gitdir", "commondir", "index", "config.worktree",
               "ORIG_HEAD", "FETCH_HEAD", "COMMIT_EDITMSG"}
REQUIRED_ADMIN = {"HEAD", "gitdir", "commondir"}
STAT_KEYS = {"dev", "ino", "mode", "nlink", "size", "mtime_ns", "ctime_ns"}
IDENTITY_KEYS = {"path", "device", "inode", "mode", "size", "mtime_ns", "ctime_ns", "mount_id", "incarnation"}
_TAR_TYPES = {tarfile.REGTYPE: "file", tarfile.AREGTYPE: "file", tarfile.DIRTYPE: "dir",
              tarfile.SYMTYPE: "symlink", tarfile.LNKTYPE: "hardlink"}
_PAX_KEYS = {"path", "linkpath", "mtime", "atime", "ctime"}


class Held(Exception):
    """Only bounded public reason codes escape this verifier."""


def _check(condition, code="manifest_invalid"):
    if not condition:
        raise Held(code)


def _tick(deadline):
    _check(time.monotonic() < deadline, "file_archive_budget")


def _text(value):
    return isinstance(value, str) and "\0" not in value and 0 < len(value.encode("utf-8")) <= MAX_PATH


def _absolute(value):
    return _text(value) and Path(value).is_absolute() and str(Path(value)) == value and ".." not in Path(value).parts


def archive_reference(value):
    try:
        row = exact(value, {"path", "sha256"}, {"path", "sha256"}, name="cleanup_archive")
        if not _absolute(row["path"]) or not isinstance(row["sha256"], str) or not git._SHA256.fullmatch(row["sha256"]):
            raise ValueError
        return row
    except (ValueError, TypeError, UnicodeError, PodError) as exc:
        raise PodError("invalid_cleanup_archives", "worktree_archive needs exactly a normalized absolute path and SHA256") from exc


def source_stat(info):
    return {key: getattr(info, "st_" + key) for key in STAT_KEYS}


def _stable_identity(value):
    return {key: value[key] for key in ("path", "device", "inode", "mode", "mount_id", "incarnation")}


def _binding(path, descriptor):
    value = observations.identity(path, os.fstat(descriptor), descriptor)
    parent, _ = observations.slot(path.parent, Path("/"), directory=True)
    _check(observations.reliable(value) and parent["status"] == "present"
           and all(observations.reliable(item) for item in [*parent["ancestors"], parent["identity"]]),
           "file_archive_unavailable")
    _no_xattrs(path.parent)
    return {"file": value, "ancestors": [_stable_identity(item) for item in [*parent["ancestors"], parent["identity"]]]}


def _hash(stream, deadline, *, size=None):
    found, length = hashlib.sha256(), 0
    while True:
        _tick(deadline)
        block = stream.read(65536)
        if not block:
            break
        length += len(block)
        _check(length <= min(MAX_BYTES, git.MAX_BUNDLE), "file_archive_budget")
        found.update(block)
    _check(size is None or size == length, "file_archive_changed")
    return found.hexdigest()


def _no_xattrs(path_or_fd):
    # This includes POSIX ACLs, capabilities and vendor xattrs; none can be lost silently.
    names = (os.listxattr(path_or_fd) if isinstance(path_or_fd, int)
             else os.listxattr(path_or_fd, follow_symlinks=False))
    _check(not names, "file_archive_unsafe")


def _name(name):
    _check(_text(name))
    path = PurePosixPath(name)
    _check(not path.is_absolute() and str(path) == name and all(part not in {"", ".", ".."} for part in name.split("/")))
    _check(path.parts[0] in {"worktree", "admin"})
    return path


def _admin_name(name, kind):
    path = PurePosixPath(name)
    parts = path.parts[1:]
    if not parts:
        return kind == "dir"
    if parts[0] in ADMIN_FILES:
        return len(parts) == 1 and kind == "file"
    if parts[0] not in {"refs", "logs"} or kind not in {"dir", "file"}:
        return False
    if parts == ("logs", "HEAD"):
        return kind == "file"
    prefix = "refs/" if parts[0] == "refs" else "logs/refs/"
    relative = "/".join(parts)
    if any(part.endswith(".lock") or part.startswith(".") for part in parts):
        return False
    namespaces = [prefix + value for value in ("bisect", "worktree", "rewritten")]
    if kind == "dir":
        return relative in {"refs", "logs", "logs/refs"} or any(
            relative == root or relative.startswith(root + "/") for root in namespaces)
    return any(relative.startswith(root + "/") for root in namespaces) and git._ref(relative.removeprefix("logs/"))


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result)
        result[key] = value
    return result


def _manifest(raw, row):
    _check(len(raw) <= min(MAX_MANIFEST, git.MAX_OUTPUT), "file_archive_budget")
    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs)
    _check(isinstance(manifest, dict) and set(manifest) == {"schema", "source", "status_sha256", "entries"})
    _check(manifest["schema"] == "pod-worktree-archive/v1")
    source = manifest["source"]
    _check(isinstance(source, dict) and set(source) == {
        "path", "ref", "tip", "common_git_dir", "git_admin_dir", "worktree_identity", "admin_identity"})
    _check(all(_absolute(source[key]) for key in ("path", "common_git_dir", "git_admin_dir")))
    _check(source["ref"] == row["ref"] and source["tip"] == row["tip"], "file_current_changed")
    common, admin = Path(source["common_git_dir"]), Path(source["git_admin_dir"])
    _check(admin.parent == common / "worktrees" and admin != common, "unsupported_admin_state")
    for key, path in (("worktree_identity", source["path"]), ("admin_identity", str(admin))):
        value = source[key]
        _check(isinstance(value, dict) and set(value) == IDENTITY_KEYS and value["path"] == path
               and all(type(value[field]) is int and value[field] >= 0 for field in IDENTITY_KEYS - {"path"})
               and stat.S_ISDIR(value["mode"]) and observations.reliable(value))
    _check(isinstance(manifest["status_sha256"], str) and git._SHA256.fullmatch(manifest["status_sha256"]))
    rows = manifest["entries"]
    _check(isinstance(rows, list) and 0 < len(rows) <= MAX_ENTRIES, "file_archive_budget")
    entries, size, groups = {}, 0, {}
    for entry in rows:
        _check(isinstance(entry, dict) and set(entry) == {"name", "type", "size", "sha256", "link", "exec", "source_stat"})
        name, kind, info = entry["name"], entry["type"], entry["source_stat"]
        path = _name(name)
        _check(name not in entries)
        _check(kind in {"file", "dir", "symlink", "hardlink"}, "unsupported_file_type")
        _check(isinstance(info, dict) and set(info) == STAT_KEYS
               and all(type(value) is int and value >= 0 for value in info.values()))
        _check(not info["mode"] & 0o7000, "file_archive_unsafe")
        _check(stat.S_IFMT(info["mode"]) == {"dir": stat.S_IFDIR, "file": stat.S_IFREG,
               "hardlink": stat.S_IFREG, "symlink": stat.S_IFLNK}[kind], "unsupported_file_type")
        _check(info["mode"] == stat.S_IFMT(info["mode"]) | (info["mode"] & 0o777)
               and info["nlink"] > 0, "file_archive_unsafe")
        _check(type(entry["exec"]) is bool and entry["exec"] == bool(info["mode"] & 0o111))
        _check(type(entry["size"]) is int and 0 <= entry["size"] <= MAX_BYTES, "file_archive_budget")
        _check(kind == "dir" and entry["sha256"] is None or kind != "dir"
               and isinstance(entry["sha256"], str) and git._SHA256.fullmatch(entry["sha256"]))
        _check(entry["size"] == (0 if kind == "dir" else info["size"]))
        if len(path.parts) > 1:
            _check(str(path.parent) in entries and entries[str(path.parent)]["type"] == "dir", "manifest_incomplete")
        else:
            _check(kind == "dir")
        if kind in {"file", "dir"}:
            _check(entry["link"] is None)
        else:
            link = entry["link"]
            _check(_text(link), "unsupported_link")
            if kind == "symlink":
                # The initial data-filter profile permits only normalized relative in-prefix links.
                target = PurePosixPath(link)
                _check(not target.is_absolute() and str(target) == link and ".." not in target.parts
                       and info["nlink"] == 1 and info["mode"] & 0o777 == 0o777, "unsupported_link")
                _check(entry["sha256"] == hashlib.sha256(link.encode()).hexdigest()
                       and entry["size"] == len(link.encode()), "manifest_invalid")
            else:
                _name(link)
                target = entries.get(link)
                _check(target is not None and target["type"] == "file"
                       and PurePosixPath(link).parts[0] == path.parts[0], "unsupported_link")
                _check(all(entry[key] == target[key] for key in ("size", "sha256", "exec", "source_stat")), "unsupported_link")
        if kind in {"file", "hardlink"}:
            groups.setdefault((info["dev"], info["ino"]), []).append(entry)
        if path.parts[0] == "admin":
            _check(_admin_name(name, kind) and (kind == "dir" or info["nlink"] == 1), "unsupported_admin_state")
        elif ".git" in path.parts[1:]:
            _check(name == "worktree/.git" and kind == "file" and info["nlink"] == 1, "unsupported_admin_state")
        size += entry["size"]
        _check(size <= MAX_BYTES, "file_archive_budget")
        entries[name] = entry
    _check({"worktree", "admin", "worktree/.git", *("admin/" + name for name in REQUIRED_ADMIN)} <= entries.keys(),
           "admin_recovery_missing")
    _check(all(len(group) == group[0]["source_stat"]["nlink"]
               and sum(entry["type"] == "file" for entry in group) == 1 for group in groups.values()), "unsupported_link")
    _check(sum(name.startswith("admin/") for name in entries) <= git.MAX_REFS, "unsupported_admin_state")
    return manifest, entries, size


def _pax(raw):
    found, offset = set(), 0
    while offset < len(raw):
        space = raw.find(b" ", offset)
        _check(space > offset and raw[offset:space].isdigit())
        length = int(raw[offset:space])
        _check(length > space - offset + 2 and offset + length <= len(raw))
        record = raw[space + 1:offset + length]
        _check(record.endswith(b"\n") and b"=" in record and b"\0" not in record)
        key, value = record[:-1].decode("utf-8").split("=", 1)
        _check(key in _PAX_KEYS and key not in found, "file_archive_unsafe")
        if key in {"path", "linkpath"}:
            _check(_text(value), "file_archive_budget")
        found.add(key)
        offset += length


def _headers(stream, deadline):
    """Bound extensions before tarfile parses them; reject compressed, GNU and trailing payloads."""
    stream.seek(0)
    count, pending = 0, False
    while True:
        _tick(deadline)
        block = stream.read(512)
        _check(len(block) == 512, "manifest_invalid")
        if block == b"\0" * 512:
            _check(not pending and stream.read(512) == b"\0" * 512)
            while block := stream.read(65536):
                _tick(deadline)
                _check(not block.strip(b"\0"))
            break
        _check(block[257:265] == b"ustar\x0000", "file_archive_unsafe")
        member = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        count += 1
        _check(count <= 2 * (MAX_ENTRIES + 1), "file_archive_budget")
        _check(0 <= member.size <= MAX_BYTES, "file_archive_budget")
        if member.type == tarfile.XHDTYPE:
            _check(not pending and member.size <= min(MAX_MANIFEST, git.MAX_OUTPUT), "file_archive_budget")
            _pax(stream.read(member.size))
            stream.seek((-member.size) % 512, os.SEEK_CUR)
            pending = True
        else:
            _check(member.type in _TAR_TYPES, "unsupported_file_type")
            stream.seek((member.size + 511) // 512 * 512, os.SEEK_CUR)
            pending = False
    stream.seek(0)


def _members(archive, row, deadline):
    first = archive.next()
    _check(first is not None and first.name == MANIFEST and first.isreg() and first.size <= min(MAX_MANIFEST, git.MAX_OUTPUT))
    _check(not set(first.pax_headers) - _PAX_KEYS and first.mode & 0o7077 == 0, "file_archive_unsafe")
    manifest, entries, size = _manifest(archive.extractfile(first).read(), row)
    members, seen = [], set()
    while member := archive.next():
        _tick(deadline)
        _check(member.name in entries and member.name not in seen, "manifest_incomplete")
        entry = entries[member.name]
        _check(not set(member.pax_headers) - _PAX_KEYS and member.sparse is None, "file_archive_unsafe")
        _check(_TAR_TYPES.get(member.type) == entry["type"], "unsupported_file_type")
        _check(member.mode == entry["source_stat"]["mode"] & 0o777, "file_archive_unsafe")
        _check(member.size == (entry["size"] if entry["type"] == "file" else 0))
        _check(member.linkname == (entry["link"] or ""), "unsupported_link")
        if member.islnk():
            _check(member.linkname in seen and entries[member.linkname]["type"] == "file", "unsupported_link")
        if entry["type"] == "file":
            _check(_hash(archive.extractfile(member), deadline, size=entry["size"]) == entry["sha256"], "file_archive_changed")
        seen.add(member.name)
        members.append(member)
        _check(len(members) <= MAX_ENTRIES, "file_archive_budget")
    _check(seen == entries.keys(), "manifest_incomplete")
    return manifest, entries, size, members


@contextmanager
def _scratch():
    with tempfile.TemporaryDirectory(prefix="pod-files-") as temporary:
        root = Path(temporary)
        try:
            yield root
        finally:
            # Only scratch directories are made traversable for cleanup, never source or archive objects.
            for parent, directories, _ in os.walk(root, topdown=True, followlinks=False):
                os.chmod(parent, 0o700)
                for name in directories:
                    path = Path(parent) / name
                    if not path.is_symlink():
                        os.chmod(path, 0o700)


def _extract(archive, members, entries, destination, deadline, *, workfiles=False):
    _check(destination.is_dir() and (workfiles or not any(destination.iterdir())), "file_restore_failed")
    directories = []
    for original in members:
        _tick(deadline)
        entry = entries[original.name]
        member = copy(original)
        if workfiles:
            if not member.name.startswith("worktree/") or member.name == "worktree/.git":
                continue
            member.name = member.name.removeprefix("worktree/")
            if member.islnk():
                member.linkname = member.linkname.removeprefix("worktree/")
        # The filter is mandatory; exact validated modes are restored afterwards, in scratch only.
        archive.extract(member, destination, filter="data")
        path = destination / member.name
        if member.isdir():
            os.chmod(path, 0o700)
            directories.append((path, entry["source_stat"]["mode"] & 0o777))
        elif not member.issym():
            os.chmod(path, entry["source_stat"]["mode"] & 0o777)
    if workfiles:
        directories.append((destination, entries["worktree"]["source_stat"]["mode"] & 0o777))
    for path, mode in reversed(directories):
        _tick(deadline)
        os.chmod(path, mode)


def _walk(root, prefix, deadline, *, restored=False, skip_marker=False):
    found, total = {}, 0
    with git._opened(root, directory=True) as (root_fd, _):
        root_mount = observations.identity(root, os.fstat(root_fd), root_fd)
        _check(observations.reliable(root_mount), "unsupported_mount")
        def walk(path, fd, relative):
            nonlocal total
            _tick(deadline)
            before = os.fstat(fd)
            current = observations.identity(path, before, fd)
            _check(current["mount_id"] == root_mount["mount_id"] and before.st_dev == root_mount["device"], "unsupported_mount")
            _no_xattrs(fd)
            _check(not before.st_mode & 0o7000, "file_archive_unsafe")
            found[relative] = {"type": "dir", "size": 0, "sha256": None, "link": None,
                               "mode": before.st_mode & 0o777, "source_stat": source_stat(before)}
            names = sorted(os.listdir(fd))
            for name in names:
                _tick(deadline)
                full, key = path / name, relative + "/" + name
                if skip_marker and key == "worktree/.git":
                    continue
                _name(key)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if prefix == "worktree" and ".git" in PurePosixPath(key).parts[1:]:
                    _check(key == "worktree/.git" and stat.S_ISREG(info.st_mode)
                           and info.st_nlink == 1, "unsupported_admin_state")
                _check(not info.st_mode & 0o7000, "file_archive_unsafe")
                _no_xattrs(full)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    try:
                        _check(source_stat(os.fstat(child)) == source_stat(info), "file_current_changed")
                        _check(observations.identity(full, os.fstat(child), child)["mount_id"] == root_mount["mount_id"], "unsupported_mount")
                        _no_xattrs(child)
                        walk(full, child, key)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        _check(source_stat(os.fstat(child)) == source_stat(info), "file_current_changed")
                        _check(observations.identity(full, os.fstat(child), child)["mount_id"] == root_mount["mount_id"], "unsupported_mount")
                        _no_xattrs(child)
                        _check(info.st_size <= MAX_BYTES, "file_archive_budget")
                        with os.fdopen(os.dup(child), "rb") as stream:
                            hashed = _hash(stream, deadline, size=info.st_size)
                        _check(source_stat(os.fstat(child)) == source_stat(info), "file_current_changed")
                    finally:
                        os.close(child)
                    found[key] = {"type": "file", "size": info.st_size, "sha256": hashed,
                                  "link": None, "mode": info.st_mode & 0o777, "source_stat": source_stat(info)}
                    total += info.st_size
                    _check(total <= MAX_BYTES, "file_archive_budget")
                elif stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=fd)
                    _check(_text(target), "unsupported_link")
                    found[key] = {"type": "symlink", "size": len(target.encode()),
                                  "sha256": hashlib.sha256(target.encode()).hexdigest(), "link": target,
                                  "mode": info.st_mode & 0o777, "source_stat": source_stat(info)}
                    total += len(target.encode())
                    _check(total <= MAX_BYTES, "file_archive_budget")
                else:
                    raise Held("unsupported_file_type")
                _check(len(found) <= MAX_ENTRIES, "file_archive_budget")
                _check(source_stat(os.stat(name, dir_fd=fd, follow_symlinks=False)) == source_stat(info), "file_current_changed")
            _check(os.listdir(fd) == names or sorted(os.listdir(fd)) == names, "file_current_changed")
            _check(source_stat(os.fstat(fd)) == source_stat(before), "file_current_changed")
        walk(root, root_fd, prefix)
    return found


def _compare(entries, found, *, current, skip_marker=False):
    expected = {name: entry for name, entry in entries.items() if not skip_marker or name != "worktree/.git"}
    _check(found.keys() == expected.keys(), "manifest_incomplete" if current else "file_restore_failed")
    for name, entry in expected.items():
        item = found[name]
        kind = "file" if entry["type"] == "hardlink" else entry["type"]
        _check(item["type"] == kind and all(item[key] == entry[key] for key in ("size", "sha256"))
               and item["mode"] == entry["source_stat"]["mode"] & 0o777
               and (kind != "symlink" or item["link"] == entry["link"]), "file_current_changed" if current else "file_restore_failed")
        if current:
            _check(item["source_stat"] == entry["source_stat"], "file_current_changed")
        if entry["type"] == "hardlink":
            target = found[entry["link"]]["source_stat"]
            _check(all(item["source_stat"][key] == target[key] for key in ("dev", "ino", "nlink")), "unsupported_link")


def _admin_roots(extracted, entries, object_format):
    roots, refs, logs, total = set(), {}, {}, 0
    for name, entry in entries.items():
        if not name.startswith("admin/") or entry["type"] != "file":
            continue
        path, relative = extracted / name, name[6:]
        total += entry["size"]
        _check(total <= git.MAX_OUTPUT, "file_archive_budget")
        if relative.startswith(("refs/", "logs/")) or relative in {"HEAD", "ORIG_HEAD", "FETCH_HEAD"}:
            raw = path.read_bytes()
            if relative.startswith("refs/"):
                oid = git._oid(raw.decode("ascii").rstrip("\n"))
                _check(raw in {oid.encode(), (oid + "\n").encode()}, "unsupported_admin_state")
                refs[relative] = oid
                roots.add(oid)
            elif relative.startswith("logs/"):
                pairs = observations.reflog_pairs(raw, object_format, limit=git.MAX_RESOURCE_REFLOG_ENTRIES)
                logs[relative[5:]] = {"sha256": hashlib.sha256(raw).hexdigest(), "entries": [list(pair) for pair in pairs]}
                roots.update(oid for pair in pairs for oid in pair if set(oid) != {"0"})
            elif relative != "HEAD":
                roots.update(git._oid(line.split(b"\t", 1)[0].decode("ascii")) for line in raw.splitlines())
    _check(len(roots) <= git.MAX_OBJECTS and len(refs) <= git.MAX_REFS, "file_archive_budget")
    return roots, refs, logs


def _retained(project, source, deadline):
    repository = git.repository_observation(project, deadline=deadline)
    return git.retained_material(project, source["ref"], repository, source["tip"], deadline=deadline)


def _no_gitlinks(root, tip, deadline, *, isolated=False, index=False):
    modes = git._text(root, ["ls-tree", "-r", "--format=%(objectmode)", tip],
                      isolated=isolated, deadline=deadline).splitlines()
    _check("160000" not in modes, "unsupported_admin_state")
    if index:
        entries = git._run(root, ["ls-files", "--stage", "-z"], deadline=deadline)
        _check(all(entry.split(b" ", 1)[0] != b"160000" for entry in entries.split(b"\0") if entry),
               "unsupported_admin_state")


def _pointers(manifest, extracted):
    source = manifest["source"]
    path, admin = Path(source["path"]), Path(source["git_admin_dir"])
    def pointer(name, base, expected, prefix=""):
        raw = (extracted / name).read_bytes()
        _check(raw.endswith(b"\n") and raw.count(b"\n") == 1, "unsupported_admin_state")
        value = raw[:-1].decode("utf-8")
        _check(value.startswith(prefix) and _text(value[len(prefix):]), "unsupported_admin_state")
        value = Path(value[len(prefix):])
        # Lexical interpretation of DATA; never resolve or load an archived control file.
        _check(os.path.normpath(value if value.is_absolute() else base / value) == str(expected), "unsupported_admin_state")
    pointer("worktree/.git", path, admin, "gitdir: ")
    pointer("admin/gitdir", admin, path / ".git")
    pointer("admin/commondir", admin, Path(source["common_git_dir"]))
    _check((extracted / "admin/HEAD").read_bytes() == ("ref: " + source["ref"] + "\n").encode(), "unsupported_admin_state")


def _current(project, manifest, entries, extracted, deadline):
    source = manifest["source"]
    path, admin, common = (Path(source[key]) for key in ("path", "git_admin_dir", "common_git_dir"))
    root, _ = observations.slot(path, Path("/"), directory=True)
    binding = {**root, "ancestors": [_stable_identity(value) for value in root["ancestors"] or []]}
    if root["status"] == "absent":
        # Absence describes current data only; the caller cannot assert a completed phase.
        return None, {"source": source, "current": binding}
    _check(root["status"] == "present", "file_current_unavailable")
    _check(root["identity"] == source["worktree_identity"] and observations.filesystem(admin) == source["admin_identity"], "file_current_changed")
    directories = git._text(path, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"], deadline=deadline).splitlines()
    _check(directories == [str(admin), str(common), str(path)], "unsupported_admin_state")
    _check(git._text(path, ["symbolic-ref", "HEAD"], deadline=deadline).strip() == source["ref"]
           and git._text(path, ["rev-parse", "--verify", "HEAD"], deadline=deadline).strip() == source["tip"], "file_current_changed")
    _no_gitlinks(path, source["tip"], deadline, index=True)
    # Reject nested controls in the complete no-follow walk before status can
    # inspect any current submodule. Recovery rejects them before extraction.
    actual = _walk(path, "worktree", deadline)
    actual.update(_walk(admin, "admin", deadline))
    before = git._run(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"], deadline=deadline)
    _check(hashlib.sha256(before).hexdigest() == manifest["status_sha256"], "file_current_changed")
    _check(len(actual) <= MAX_ENTRIES, "file_archive_budget")
    _compare(entries, actual, current=True)
    after = _walk(path, "worktree", deadline)
    after.update(_walk(admin, "admin", deadline))
    _check(after == actual, "file_current_changed")
    _check(git._run(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"], deadline=deadline) == before, "file_current_changed")
    _check(observations.filesystem(path) == source["worktree_identity"]
           and observations.filesystem(admin) == source["admin_identity"], "file_current_changed")
    return True, {"source": source, "current": binding, "entries_sha256": digest(actual), "status_sha256": hashlib.sha256(before).hexdigest()}


def verify(project, row, excluded, *, resource=None):
    """Return bounded public summaries and a private fingerprint basis, never a clearance."""
    reference = row["worktree_archive"]
    file = {"status": "unavailable", "path": reference["path"], "sha256": None, "manifest_sha256": None,
            "entries": None, "bytes": None, "restore_verified": False, "current_match": None, "reasons": []}
    admin = {"status": "unavailable", "reasons": []}
    basis, manifest = {}, None
    deadline = time.monotonic() + SECONDS
    try:
        path = Path(reference["path"])
        _check(path.exists(), "file_archive_missing")
        with git._opened(path) as (descriptor, _):
            bound = _binding(path, descriptor)
            _no_xattrs(descriptor)
            _check(bound["file"]["mode"] & 0o7077 == 0 and bound["ancestors"][-1]["mode"] & 0o7077 == 0, "file_archive_unsafe")
            _check(bound["file"]["size"] <= min(MAX_BYTES, git.MAX_BUNDLE), "file_archive_budget")
            basis["artifact"] = bound
            with os.fdopen(os.dup(descriptor), "rb") as stream, _scratch() as scratch:
                file["sha256"] = _hash(stream, deadline, size=bound["file"]["size"])
                _check(file["sha256"] == reference["sha256"], "file_archive_changed")
                _headers(stream, deadline)
                with tarfile.open(fileobj=stream, mode="r:", encoding="utf-8", errors="strict") as archive:
                    manifest, entries, size, members = _members(archive, row, deadline)
                    source = manifest["source"]
                    # Source identity is compared to the actual selected resource, never just
                    # to the archive's own assertions or an equal ref/tip in another checkout.
                    common = git._text(project, ["rev-parse", "--path-format=absolute", "--git-common-dir"], deadline=deadline).strip()
                    _check(common == source["common_git_dir"], "file_current_changed")
                    if resource is not None:
                        actual_common = resource.get("common_dir") or resource.get("repository", {}).get("common_dir")
                        _check(actual_common is not None and git._directory(Path(common)) == actual_common, "file_current_changed")
                        if resource.get("kind") == "worktree":
                            _check(source["path"] == resource["observation_path"]
                                   and source["git_admin_dir"] == (resource.get("git_dir") or {}).get("path"), "file_current_changed")
                    boundaries = [*excluded, *(Path(source[key]) for key in ("path", "common_git_dir", "git_admin_dir"))]
                    basis["placement"] = git._outside(path, boundaries)
                    file.update(manifest_sha256=hashlib.sha256(archive.extractfile(archive.getmember(MANIFEST)).read()).hexdigest(),
                                entries=len(entries), bytes=size)
                    extracted = scratch / "data"
                    extracted.mkdir(mode=0o700)
                    _extract(archive, members, entries, extracted, deadline)
                    found = _walk(extracted / "worktree", "worktree", deadline, restored=True)
                    found.update(_walk(extracted / "admin", "admin", deadline, restored=True))
                    _compare(entries, found, current=False)
                    _pointers(manifest, extracted)
                    object_format = "sha256" if len(row["tip"]) == 64 else "sha1"
                    roots, refs, logs = _admin_roots(extracted, entries, object_format)
                    basis["admin"] = {"refs": refs, "logs": logs}
                    retained, closure = _retained(project, source, deadline)
                    # Copy what the known phase loses. Retained live stash material is
                    # fingerprinted, never made a mandatory recovery root by its presence.
                    needed = {source["tip"], *refs.values(), *(roots - closure)}
                    if resource is not None and "selected_ref_log" in resource:
                        _check(resource["complete"], "admin_recovery_missing")
                        needed.update(resource["required_objects"])
                    basis["retained_material"] = retained
                    # Recover Git and workfiles separately, preserving regenerated control pointers.
                    recovered_git, recovered = scratch / "recovery.git", scratch / "recovered"
                    git._run(scratch, ["init", "--bare", "--quiet", "--template=", "--object-format=" + object_format, str(recovered_git)], isolated=True, deadline=deadline)
                    with git._opened(Path(row["path"])) as (bundle_fd, bundle_before):
                        bundle_binding = _binding(Path(row["path"]), bundle_fd)
                        _check(bundle_binding["file"]["mode"] & 0o7077 == 0
                               and bundle_binding["ancestors"][-1]["mode"] & 0o7077 == 0, "file_archive_unsafe")
                        bundle = scratch / "recovery.bundle"
                        with os.fdopen(os.dup(bundle_fd), "rb") as original, bundle.open("wb") as saved:
                            copied = 0
                            while block := original.read(65536):
                                _tick(deadline)
                                copied += len(block)
                                _check(copied <= min(MAX_BYTES, git.MAX_BUNDLE), "file_archive_budget")
                                saved.write(block)
                        with bundle.open("rb") as saved:
                            _check(_hash(saved, deadline) == row["sha256"], "file_archive_changed")
                        _check(git._identity(os.fstat(bundle_fd), descriptor=bundle_fd) == bundle_before["file"], "file_archive_changed")
                    git._run(recovered_git, ["bundle", "verify", str(bundle)], isolated=True, deadline=deadline)
                    git._pack_bound(bundle)
                    git._run(recovered_git, ["bundle", "unbundle", str(bundle)], isolated=True, deadline=deadline)
                    git._types(recovered_git, needed, isolated=True, deadline=deadline)
                    _check(not git._uncovered(recovered_git, needed, set(row["recovery_refs"].values()), isolated=True, deadline=deadline), "admin_recovery_missing")
                    if resource is not None:
                        git.verify_selected_log(project, recovered_git, resource, row["recovery_refs"], deadline=deadline)
                    _no_gitlinks(recovered_git, row["tip"], deadline, isolated=True)
                    git._run(recovered_git, ["worktree", "add", "--quiet", "--detach", "--no-checkout", str(recovered), row["tip"]], isolated=True, deadline=deadline)
                    git._run(recovered, ["read-tree", row["tip"]], isolated=True, deadline=deadline)
                    _extract(archive, members, entries, recovered, deadline, workfiles=True)
                    workfiles = {name: entry for name, entry in entries.items() if name == "worktree" or name.startswith("worktree/")}
                    _compare(workfiles, _walk(recovered, "worktree", deadline, restored=True, skip_marker=True), current=False, skip_marker=True)
                    status = git._run(recovered, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored", "--ignore-submodules=all"], isolated=True, deadline=deadline)
                    _check(hashlib.sha256(status).hexdigest() == manifest["status_sha256"], "file_restore_failed")
                    file.update(status="verified", restore_verified=True)
                    admin.update(status="verified")
                    try:
                        file["current_match"], basis["current"] = _current(project, manifest, entries, extracted, deadline)
                        _check(_retained(project, source, deadline)[0] == retained, "file_current_changed")
                    except (Held, git.Unavailable, OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
                        code = str(exc) if isinstance(exc, Held) else "file_current_unavailable"
                        file["current_match"] = False if code in {"file_current_changed", "manifest_incomplete"} else None
                        basis["current"] = {"current": {"status": "unavailable"}, "reason": code}
                        file["reasons"].append(code)
                    if file["current_match"] is None:
                        file["reasons"].append("file_current_unavailable")
                with git._opened(path) as (again, _):
                    _check(_binding(path, again) == bound, "file_archive_changed")
                    with os.fdopen(os.dup(again), "rb") as fresh:
                        _check(_hash(fresh, deadline) == file["sha256"], "file_archive_changed")
                    _check(git._outside(path, boundaries) == basis["placement"], "file_archive_changed")
                _check(_binding(path, descriptor) == bound, "file_archive_changed")
                _tick(deadline)
        _tick(deadline)
    except (Held, git.Unavailable, OSError, ValueError, TypeError, KeyError, UnicodeError, tarfile.TarError, RecursionError) as exc:
        code = str(exc) if isinstance(exc, Held) else "file_archive_unavailable"
        if isinstance(exc, git.Unavailable) and ("bound" in str(exc) or "time" in str(exc)):
            code = "file_archive_budget"
        if code in {"file_current_changed", "file_current_unavailable", "manifest_incomplete"} and file["restore_verified"]:
            file["current_match"] = False if code != "file_current_unavailable" else None
        else:
            file.update(status="held", restore_verified=False)
            admin.update(status="held", reasons=["admin_recovery_missing"])
        file["reasons"] = sorted(set(file["reasons"] + [code]))
    file["reasons"] = sorted(set(file["reasons"]))
    return file, admin, basis

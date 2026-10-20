"""Bounded, read-only recovery proofs. Preservation never supplies cleanup authority."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .errors import PodError
from .gitio import OBJECT_ID
from .util import exact

MAX_ROWS = 96
MAX_REFS = 96
MAX_OUTPUT = 2_000_000
MAX_BUNDLE = 128 * 1024 * 1024
MAX_OBJECTS = 1024
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"refs/[A-Za-z0-9_./+@=-]{1,506}\Z")
_PRIVATE = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")
_TYPES = {"commit", "tree", "blob", "tag"}


class Unavailable(Exception):
    """An incomplete observation, never an empty successful proof."""


def archive_rows(value: object) -> list[dict]:
    rows = [] if value is None else value
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise PodError("invalid_cleanup_archives", "Archives are a bounded list")
    seen = set()
    for item in rows:
        row = exact(item, {"path", "ref", "tip", "sha256", "recovery_refs"},
                    {"path", "ref", "tip"}, name="cleanup_archive")
        if not all(isinstance(row[key], str) and len(row[key]) <= 4096
                   and "\0" not in row[key] for key in ("path", "ref", "tip")):
            raise PodError("invalid_cleanup_archives", "Archive path, ref and tip are bounded text")
        if ("sha256" in row) != ("recovery_refs" in row):
            raise PodError("invalid_cleanup_archives", "sha256 and recovery_refs must be supplied together")
        if "sha256" in row:
            refs = row["recovery_refs"]
            if (not isinstance(row["sha256"], str) or not _SHA256.fullmatch(row["sha256"])
                    or not isinstance(refs, dict) or not 0 < len(refs) <= MAX_REFS
                    or any(not isinstance(name, str) or not 0 < len(name) <= 512
                           or any(ord(char) <= 32 or ord(char) >= 127 for char in name)
                           or not isinstance(oid, str) or not OBJECT_ID.fullmatch(oid)
                           for name, oid in refs.items())):
                raise PodError("invalid_cleanup_archives", "Archive digest and complete recovery map are bounded")
        key = row["ref"], row["tip"]
        if key in seen:
            raise PodError("invalid_cleanup_archives", "Duplicate archive source ref and tip")
        seen.add(key)
    return rows


def _run(root: Path, argv: list[str], *, data: bytes | None = None, isolated: bool = False) -> bytes:
    # Refuse ambient object/repository overrides at the source, and never inherit them in restoration.
    env = dict(os.environ)
    if isolated:
        env = {key: value for key, value in env.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
    env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                ["git", "--no-optional-locks", "--no-replace-objects", "-c", "maintenance.auto=false",
                 "-c", "gc.auto=0", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *argv],
                input=data, stdin=subprocess.DEVNULL if data is None else None,
                stdout=output, stderr=errors, timeout=30, check=False, env=env)
            if output.tell() > MAX_OUTPUT or errors.tell() > MAX_OUTPUT:
                raise Unavailable("Git archive inspection exceeded its output bound")
            if result.returncode:
                raise Unavailable("Git archive inspection failed: " + argv[0])
            output.seek(0)
            return output.read(MAX_OUTPUT + 1)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable("Git archive inspection is unavailable") from exc


def _text(root: Path, argv: list[str], **kwargs) -> str:
    try:
        return _run(root, argv, **kwargs).decode("utf-8")
    except UnicodeError as exc:
        raise Unavailable("Git archive inspection is not UTF-8") from exc


def _identity(value: os.stat_result, *, directory: bool = False) -> dict:
    result = {"dev": value.st_dev, "ino": value.st_ino, "mode": value.st_mode}
    if not directory:
        result.update(size=value.st_size, mtime_ns=value.st_mtime_ns, ctime_ns=value.st_ctime_ns,
                      links=value.st_nlink)
    return result


@contextmanager
def _opened(path: Path, *, directory: bool = False):
    """Walk every component without following links, keeping the descriptors alive."""
    if not path.is_absolute() or ".." in path.parts:
        raise Unavailable("Archive and resource paths must be absolute without parent traversal")
    descriptors, components = [], []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(current)
        components.append({"path": "/", **_identity(os.fstat(current), directory=True)})
        for index, part in enumerate(path.parts[1:]):
            is_directory = directory or index < len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            current = os.open(part, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=current)
            descriptors.append(current)
            if is_directory:
                components.append({"path": str(Path(*path.parts[:index + 2])),
                                   **_identity(os.fstat(current), directory=True)})
        info = os.fstat(current)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise Unavailable("Archive or resource has an unsupported file type")
        if not directory and (info.st_size > MAX_BUNDLE or info.st_nlink != 1):
            raise Unavailable("Archive file is oversized or has multiple hard links")
        yield current, {"path": str(path), "components": components,
                        "file": _identity(info, directory=directory)}
    except OSError as exc:
        raise Unavailable("Archive or resource path is unavailable or redirected") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _directory(path: Path) -> dict:
    with _opened(path, directory=True) as (_, identity):
        return identity


def _bytes(path: Path, *, limit: int = MAX_OUTPUT) -> bytes:
    with _opened(path) as (descriptor, identity):
        if identity["file"]["size"] > limit:
            raise Unavailable("Resource metadata exceeds its bound")
        blocks, size = [], 0
        while block := os.read(descriptor, min(65536, limit + 1 - size)):
            blocks.append(block)
            size += len(block)
            if size > limit:
                raise Unavailable("Resource metadata exceeds its bound")
        value = b"".join(blocks)
        if _identity(os.fstat(descriptor)) != identity["file"] or len(value) != identity["file"]["size"]:
            raise Unavailable("Resource metadata changed during observation")
        return value


def _hash(descriptor: int, destination=None) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    hashed, size = hashlib.sha256(), 0
    while block := os.read(descriptor, 65536):
        size += len(block)
        if size > MAX_BUNDLE:
            raise Unavailable("Archive exceeds its byte bound")
        hashed.update(block)
        if destination is not None:
            destination.write(block)
    return hashed.hexdigest()


def _ref(name: str) -> bool:
    return bool(_REF.fullmatch(name) and not any(token in name for token in ("..", "@{", "//"))
                and not name.endswith(("/", "."))
                and all(not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")))


def _oid(value: str) -> str:
    if not OBJECT_ID.fullmatch(value):
        raise Unavailable("Git object identity is malformed or unavailable")
    return value


def _types(root: Path, oids: set[str], *, isolated: bool = False) -> dict[str, str]:
    if not oids or len(oids) > MAX_OBJECTS:
        raise Unavailable("Required object observation is empty or over its bound")
    ordered = sorted(oids)
    output = _text(root, ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                   data=("\n".join(ordered) + "\n").encode(), isolated=isolated)
    lines = output.splitlines()
    if len(lines) != len(ordered):
        raise Unavailable("Required object inspection is incomplete")
    found = {}
    for expected, line in zip(ordered, lines):
        parts = line.split(" ")
        if len(parts) != 2 or parts[0] != expected or parts[1] not in _TYPES:
            raise Unavailable("Required recovery object is missing or has an unsupported type")
        found[expected] = parts[1]
    return found


def _commits(root: Path, oids: set[str]) -> dict:
    if not oids:
        return {}
    if len(oids) > MAX_OBJECTS:
        raise Unavailable("Required commit metadata exceeds its bound")
    output = _run(root, ["cat-file", "--batch"], data=("\n".join(sorted(oids)) + "\n").encode())
    result, cursor = {}, 0
    try:
        for expected in sorted(oids):
            end = output.index(b"\n", cursor)
            oid, kind, size = output[cursor:end].decode("ascii").split(" ")
            size = int(size)
            start, cursor = end + 1, end + 1 + size + 1
            if oid != expected or kind != "commit" or size < 0 or output[cursor - 1:cursor] != b"\n":
                raise ValueError
            headers = output[start:start + size].split(b"\n\n", 1)[0].split(b"\n")
            tree = _oid(headers[0].removeprefix(b"tree ").decode("ascii"))
            parents = [_oid(line[7:].decode("ascii")) for line in headers if line.startswith(b"parent ")]
            if not headers[0].startswith(b"tree ") or len(parents) > MAX_REFS:
                raise ValueError
            result[oid] = {"tree": tree, "parents": parents}
        if cursor != len(output):
            raise ValueError
    except (ValueError, IndexError, UnicodeError) as exc:
        raise Unavailable("Required commit metadata is malformed or incomplete") from exc
    return result


def _object_inventory(restored: Path) -> dict:
    output = _text(restored, ["cat-file", "--batch-all-objects",
                              "--batch-check=%(objectname) %(objecttype) %(objectsize)"], isolated=True)
    seen, size = set(), 0
    try:
        for line in output.splitlines():
            oid, kind, length = line.split(" ")
            length = int(length)
            if oid in seen or kind not in _TYPES or length < 0:
                raise ValueError
            seen.add(_oid(oid))
            size += length
            if size > MAX_BUNDLE:
                raise Unavailable("Restored object bytes exceed the archive byte bound")
        if not seen:
            raise ValueError
    except ValueError as exc:
        raise Unavailable("Restored object inventory is malformed or incomplete") from exc
    return {"objects": len(seen), "bytes": size}


def _describe(root: Path, roots: dict[str, str], stash: list[str]) -> dict:
    types = _types(root, set(roots.values()))
    commits = _commits(root, {oid for oid, kind in types.items() if kind == "commit"})
    parents = {parent for row in commits.values() for parent in row["parents"]}
    parent_facts = _commits(root, parents - commits.keys())
    commits.update(parent_facts)
    required = dict(types)
    for oid, row in commits.items():
        required[oid] = "commit"
        required[row["tree"]] = "tree"
    if _types(root, set(required)) != required:
        raise Unavailable("Required roots, parents or trees have inconsistent types")
    for oid in stash:
        if types.get(oid) != "commit" or len(commits[oid]["parents"]) not in (2, 3):
            raise Unavailable("Stash root has an unsupported commit shape")
    return {"roots": roots, "required_objects": required, "commits": commits}


def repository_observation(project: Path) -> dict:
    """Internal archive basis only; this is no stash relation or native footprint proof."""
    result = {"complete": False, "holds": [], "stash_roots": []}
    try:
        overrides = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_FILE", "GIT_SHALLOW_FILE",
                     "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE"}
        if overrides & os.environ.keys():
            raise Unavailable("Git repository or object overrides are unsupported for archival")
        topology = _text(project, ["rev-parse", "--path-format=absolute", "--git-common-dir",
                                   "--show-object-format=storage"]).splitlines()
        if len(topology) != 2 or topology[1] not in ("sha1", "sha256"):
            raise Unavailable("Git storage format or common directory is unavailable")
        common = Path(topology[0])
        result.update(common_dir=_directory(common), object_format=topology[1])
        if _text(project, ["rev-parse", "--is-shallow-repository"]).strip() != "false":
            raise Unavailable("Shallow repositories cannot supply complete archive roots")
        config = _text(project, ["config", "--null", "--list"])
        result["config_sha256"] = hashlib.sha256(config.encode()).hexdigest()
        settings = [field.split("\n", 1) for field in config.split("\0") if field]
        if any(parts[0].lower() == "extensions.partialclone"
               or parts[0].lower() == "extensions.refstorage" and parts[1:] != ["files"]
               or parts[0].lower().endswith(".promisor") and parts[1:] != ["false"]
               for parts in settings):
            raise Unavailable("Partial or unsupported ref storage cannot supply complete archive roots")
        for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts", "shallow"):
            if os.path.lexists(common / name):
                raise Unavailable("Alternate, graft or shallow storage is unsupported for archival")
        if _text(project, ["for-each-ref", "--format=%(refname)", "refs/replace/"]).strip():
            raise Unavailable("Replacement refs are unsupported for archival")
        stashes = _text(project, ["stash", "list", "--format=%H"]).splitlines()
        if len(stashes) > MAX_REFS:
            raise Unavailable("Stash roots exceed the archive observation bound")
        result["stash_roots"] = [_oid(value) for value in stashes]
        result["complete"] = True
    except Unavailable as exc:
        result["holds"].append(str(exc))
    return result


def resource_observation(path: Path, ref: str | None, tip: str | None, repository: dict) -> dict:
    result = {"kind": "worktree", "observation_path": str(path),
              "complete": False, "holds": [], "repository": repository, "source_ref": ref,
              "source_tip": tip, "private_refs": {}, "reflogs": {}, "required_objects": {}}
    try:
        result["directory"] = _directory(path)
        directories = _text(path, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]).splitlines()
        if len(directories) != 2:
            raise Unavailable("Resource administrative directories are unavailable")
        admin, common = map(Path, directories)
        result.update(git_dir=_directory(admin), common_dir=_directory(common))
        if not repository["complete"] or str(common) != repository["common_dir"]["path"]:
            raise Unavailable("Resource repository identity or storage is uncertain")
        result["head_file_sha256"] = hashlib.sha256(_bytes(admin / "HEAD")).hexdigest()
        head = _oid(_text(path, ["rev-parse", "--verify", "HEAD"]).strip())
        head_ref = _text(path, ["symbolic-ref", "HEAD"]).strip()
        source = _oid(_text(path, ["rev-parse", "--verify", "--end-of-options", ref or "HEAD"]).strip())
        result.update(head=head, head_ref=head_ref, actual_source_tip=source)
        if not ref or head_ref != ref or source != tip or head != tip:
            raise Unavailable("Actual resource source ref, tip or HEAD differs from its binding")
        # A symbolic branch deletion can resolve into another ref; the archive grants no such effect.
        refs = _text(path, ["for-each-ref", "--format=%(refname) %(objectname) %(symref)",
                            ref, *_PRIVATE]).splitlines()
        roots = {"HEAD": head}
        for line in refs:
            parts = line.split(" ")
            if len(parts) != 3 or not _ref(parts[0]):
                raise Unavailable("Resource reference observation is incomplete")
            name, oid, symbolic = parts
            if symbolic:
                raise Unavailable("Symbolic resource refs require separate binding preservation")
            _oid(oid)
            if name.startswith(_PRIVATE):
                result["private_refs"][name] = oid
                roots[name] = oid
            elif name == ref and oid != tip:
                raise Unavailable("Resource source reference changed during observation")
        if len(refs) > MAX_REFS:
            raise Unavailable("Resource refs exceed the archive observation bound")
        if admin == common:
            raise Unavailable("Main-repository administrative removal is unsupported")
        allowed = {"HEAD", "gitdir", "commondir", "index", "config.worktree", "logs", "refs",
                   "ORIG_HEAD", "FETCH_HEAD", "COMMIT_EDITMSG"}
        names = set(os.listdir(admin))
        result["admin_entries"] = sorted(names)
        if len(names) > MAX_REFS or names - allowed:
            raise Unavailable("Resource has unsupported administrative roots or an operation in progress")
        metadata = {}
        for name in sorted(names - {"logs", "refs"}):
            value = _bytes(admin / name)
            metadata[name] = hashlib.sha256(value).hexdigest()
            if name in ("ORIG_HEAD", "FETCH_HEAD"):
                for index, line in enumerate(value.splitlines()):
                    roots[f"{name}:{index}"] = _oid(line.split(b"\t", 1)[0].decode("ascii"))
        result["metadata"] = metadata
        # Physical walks detect unrecognized private namespaces and symlinks Git might otherwise omit.
        for base in ("refs", "logs"):
            start = admin / base
            if not os.path.lexists(start):
                continue
            count = 0
            for current, directories, files in os.walk(start, followlinks=False):
                _directory(Path(current))
                count += len(directories) + len(files)
                if count > MAX_REFS:
                    raise Unavailable("Resource private metadata exceeds its bound")
                for name in directories:
                    _directory(Path(current) / name)
                for name in files:
                    file = Path(current) / name
                    relative = file.relative_to(start).as_posix()
                    refname = "refs/" + relative if base == "refs" else relative
                    value = _bytes(file)
                    if base == "refs":
                        if refname not in result["private_refs"]:
                            raise Unavailable("Resource has an unsupported private reference")
                    else:
                        if relative != "HEAD" and not relative.startswith(_PRIVATE):
                            raise Unavailable("Resource has an unsupported private reflog")
                        entries = []
                        for line in value.splitlines():
                            parts = line.split(b" ", 2)
                            if len(parts) != 3:
                                raise Unavailable("Resource reflog is incomplete")
                            pair = [_oid(part.decode("ascii")) for part in parts[:2]]
                            entries.append(pair)
                            for oid in pair:
                                if set(oid) != {"0"}:
                                    roots["reflog:" + oid] = oid
                        if len(entries) > MAX_REFS:
                            raise Unavailable("Resource reflog exceeds its bound")
                        result["reflogs"][relative] = {"sha256": hashlib.sha256(value).hexdigest(),
                                                        "entries": entries}
        for index, oid in enumerate(repository["stash_roots"]):
            roots[f"stash:{index}"] = oid
        if len(roots) > MAX_OBJECTS:
            raise Unavailable("Resource roots exceed the archive observation bound")
        result.update(_describe(path, roots, repository["stash_roots"]))
        if result["required_objects"].get(head) != "commit":
            raise Unavailable("Resource HEAD is not a commit")
        branch_objects = set(_text(path, ["rev-list", "--objects", "--no-object-names", head]).splitlines())
        result["additional_roots"] = sorted(name for name, oid in roots.items()
                                            if oid not in branch_objects or name in result["private_refs"])
        result["complete"] = True
    except (Unavailable, OSError, UnicodeError) as exc:
        result["holds"].append(str(exc) if isinstance(exc, Unavailable) else "Resource metadata is unavailable")
    return result


def reference_observation(project: Path, ref: str, tip: str | None, repository: dict) -> dict:
    result = {"kind": "reference", "observation_path": str(project), "complete": False, "holds": [],
              "repository": repository, "source_ref": ref, "source_tip": tip, "private_refs": {},
              "required_objects": {}}
    try:
        if not repository["complete"]:
            raise Unavailable("Reference repository identity or storage is uncertain")
        result["directory"] = _directory(project)
        result["head"] = _oid(_text(project, ["rev-parse", "--verify", "HEAD"]).strip())
        result["head_tree"] = _oid(_text(project, ["rev-parse", "--verify", "HEAD^{tree}"]).strip())
        result["head_binding"] = _text(project, ["rev-parse", "--symbolic-full-name", "HEAD"]).strip()
        actual = _text(project, ["for-each-ref", "--format=%(refname) %(objectname) %(symref)", ref])
        if actual != f"{ref} {tip} \n":
            raise Unavailable("Actual source reference is missing, changed or symbolic")
        result.update(_describe(project, {ref: _oid(tip or "")}, []))
        if result["required_objects"].get(tip) != "commit":
            raise Unavailable("Source branch is not a commit")
        result["complete"] = True
    except Unavailable as exc:
        result["holds"].append(str(exc))
    return result


def _outside(path: Path, excluded: list[Path]) -> dict:
    """Compare filesystem coordinates as well as lexical names, including Linux bind mounts."""
    try:
        with Path("/proc/self/mountinfo").open("rb") as source:
            raw = source.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise ValueError
        mounts = []
        unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        for line in raw.decode().splitlines():
            fields = line.split(" ")
            mounts.append((Path(unescape(fields[4])), fields[2], Path(unescape(fields[3]))))
        def location(candidate):
            applicable = [(mount, device, root) for mount, device, root in mounts
                          if candidate.is_relative_to(mount)]
            mount, device, root = max(applicable, key=lambda row: len(row[0].parts))
            return device, root / candidate.relative_to(mount)
        device, physical = location(path)
        for boundary in excluded:
            other_device, other = location(boundary)
            if path.is_relative_to(boundary) or device == other_device and physical.is_relative_to(other):
                raise Unavailable("Archive is inside a removable resource or administrative directory")
        return {"device": device, "path": str(physical)}
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise Unavailable("Archive placement or mount aliases cannot be verified") from exc


def verify(project: Path, row: dict, resource: dict, excluded: list[Path]) -> dict:
    """Observe exact bytes and restoration; branch-only input retains its narrow proof."""
    expanded = "sha256" in row
    result = {"declared": row, "mode": "expanded" if expanded else "branch_only", "verified": False,
              "covers_resource": False, "recovery_refs": {}, "holds": []}
    path = Path(row["path"])
    try:
        with _opened(path) as (descriptor, identity):
            result["identity"] = identity
            result["placement"] = _outside(path, excluded)
            with tempfile.TemporaryDirectory(prefix="pod-cleanup-") as temporary:
                scratch = Path(temporary)
                bundle = scratch / "archive.bundle"
                with bundle.open("wb") as copied:
                    result["sha256"] = _hash(descriptor, copied)
                if _identity(os.fstat(descriptor)) != identity["file"]:
                    raise Unavailable("Archive changed while reading its bytes")
                if expanded and result["sha256"] != row["sha256"]:
                    raise Unavailable("Archive SHA256 differs from the supplied digest")
                ref, tip = row["ref"], row["tip"]
                named = (ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/")
                         else ref.removeprefix("refs/remotes/") if ref.startswith("refs/remotes/") else "")
                if not named or not _BRANCH.fullmatch(named) or not OBJECT_ID.fullmatch(tip):
                    raise Unavailable("Archive source ref or tip is unsupported")
                if (not resource["complete"] or ref != resource["source_ref"]
                        or tip != resource["source_tip"]):
                    raise Unavailable("Archive does not identify the actual current source resource")
                restored = scratch / "restored.git"
                _run(scratch, ["init", "--bare", "--quiet", "--template=",
                               "--object-format=" + resource["repository"]["object_format"], str(restored)],
                     isolated=True)
                listed = _text(restored, ["bundle", "list-heads", str(bundle)], isolated=True)
                actual = {}
                for line in listed.splitlines():
                    parts = line.split(" ")
                    if len(parts) != 2 or parts[1] in actual or len(actual) >= MAX_REFS:
                        raise Unavailable("Bundle advertisements are duplicate, malformed or over their bound")
                    actual[parts[1]] = _oid(parts[0])
                result["recovery_refs"] = actual
                if not actual:
                    raise Unavailable("Bundle has no advertised recovery refs")
                if expanded and actual != row["recovery_refs"]:
                    raise Unavailable("Bundle advertisements differ from the complete recovery map")
                if any(not _ref(name) for name in actual):
                    raise Unavailable("Unsupported bundle advertisement, including HEAD or worktree-qualified names")
                if not expanded and actual.get(ref) != tip:
                    raise Unavailable("Branch-only bundle does not advertise the exact source ref and tip")
                if expanded and tip not in actual.values():
                    raise Unavailable("Bundle does not advertise a restoration alias for the exact source tip")
                # Verification in an EMPTY repository refuses prerequisites and thin/incremental borrowing.
                _run(restored, ["bundle", "verify", str(bundle)], isolated=True)
                _run(restored, ["bundle", "unbundle", str(bundle)], isolated=True)
                result["restored_objects"] = _object_inventory(restored)
                targets = _types(restored, set(actual.values()), isolated=True)
                result["recovery_types"] = {name: targets[oid] for name, oid in actual.items()}
                updates = "".join(f"create {name} {oid}\n" for name, oid in actual.items())
                _run(restored, ["update-ref", "--stdin"], data=updates.encode(), isolated=True)
                found = _text(restored, ["for-each-ref", "--format=%(refname) %(objectname)"], isolated=True)
                if dict(line.split(" ") for line in found.splitlines()) != actual:
                    raise Unavailable("Restored recovery refs differ from the advertisements")
                _run(restored, ["fsck", "--full", "--strict", "--no-reflogs", "--no-dangling"], isolated=True)
                required = resource["required_objects"] if expanded else {tip: "commit"}
                if _types(restored, set(required), isolated=True) != required:
                    raise Unavailable("Restored required roots, parents or trees have incorrect types")
                if expanded:
                    reachable = set(_text(restored, ["rev-list", "--objects", "--no-object-names", "--all"],
                                          isolated=True).splitlines())
                    if not set(required) <= reachable:
                        raise Unavailable("Required roots are outside the advertised recovery closure")
                result["verified"] = True
                if expanded:
                    result["covers_resource"] = True
                elif not resource["private_refs"] and not resource["repository"]["stash_roots"]:
                    reachable = set(_text(restored, ["rev-list", "--objects", "--no-object-names", tip],
                                          isolated=True).splitlines())
                    result["covers_resource"] = set(resource["required_objects"]) <= reachable
                if not result["covers_resource"]:
                    result["holds"].append("Branch-only proof does not cover additional resource roots")
                fresh_repository = repository_observation(project)
                observer = resource_observation if resource["kind"] == "worktree" else reference_observation
                fresh = observer(Path(resource["observation_path"]), resource["source_ref"],
                                 resource["source_tip"], fresh_repository)
                if fresh != resource:
                    raise Unavailable("Resource, HEAD, local roots or repository identity changed during verification")
                with _opened(path) as (again, observed):
                    if (observed != identity or _hash(again) != result["sha256"]
                            or _identity(os.fstat(descriptor)) != identity["file"]
                            or _outside(path, excluded) != result["placement"]):
                        raise Unavailable("Archive file, containing directories, placement or bytes changed")
    except (Unavailable, OSError) as exc:
        result.update(verified=False, covers_resource=False)
        result["holds"].append(str(exc) if isinstance(exc, Unavailable) else "Archive bytes are unavailable")
    return result

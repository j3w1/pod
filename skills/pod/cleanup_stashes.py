"""Independent stash material and exact primitive noninterference observations."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from . import cleanup_archives as archives
from . import cleanup_observations as observations
from .errors import PodError
from .gitio import worktree_inventory, worktree_rows
from .util import digest

PROFILE = "git-local-ref-delete/v1"
MAX_HOLDER_REFS = 100_000
REASONS = frozenset({"snapshot_unavailable", "snapshot_incomplete", "malformed_stash", "stash_changed",
    "resource_identity_unavailable", "resource_changed", "storage_unavailable", "storage_overlap",
    "unsupported_storage", "ref_alias_overlap", "private_ref_dependency", "effect_footprint_unavailable",
    "effect_footprint_changed", "executor_binding_unavailable", "git_context_mismatch", "holder_unavailable",
    "holder_live", "holder_changed", "ref_changed", "reflog_changed", "config_changed", "budget_exhausted",
    "inspection_error", "resource_provenance_unavailable", "resource_absent", "unsupported_primitive",
    "ref_symbolic", "ref_identity_unavailable", "native_observation_unavailable", "phase_continuity_unavailable"})


def _unknown(keys: tuple[str, ...], code: str, *, operation: str, kind="git_read") -> dict:
    return {**dict.fromkeys(keys), "status": "unavailable", "reasons": [code],
            "witnesses": [observations.witness(kind, operation, status="UNAVAILABLE")]}


def local_argv(project: Path, ref: str, oid: str) -> list[str]:
    return ["git", "--no-optional-locks", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(project),
            "update-ref", "--no-deref", "-d", ref, oid]


def _retaining(project: Path, repository: dict) -> tuple[dict, list[str], dict]:
    storage, raw = observations.storage_once(project, "refs/stash", repository)
    result = {"status": "unavailable", "ref": storage["current"], "entries": None,
              "ref_identity": None, "log_identity": storage["reflog"]["identity"],
              "sha256": storage["reflog"]["sha256"], "reasons": [], "witnesses": []}
    roots = []
    try:
        if storage["status"] != "observed":
            raise archives.Unavailable("Stash storage is unavailable")
        current, log = storage["current"], storage["reflog"]
        result["ref_identity"] = (storage["loose"]["identity"] if storage["loose"]["status"] == "present"
                                  else storage["packed"]["identity"] if current["status"] == "present" else None)
        if current["status"] == "absent" and log["status"] == "absent":
            result.update(status="absent", entries=[])
        elif current["status"] != "present" or current["symref"] or log["status"] != "present":
            raise archives.Unavailable("Stash retaining reference and log are incomplete")
        else:
            pairs = observations.reflog_pairs(raw["reflog"], repository["object_format"])
            if not pairs or pairs[-1][1] != current["oid"] or current["object_type"] != "commit":
                raise archives.Unavailable("Stash retaining root and ordered log disagree")
            entries = [new for _, new in reversed(pairs)]
            if any(set(oid) == {"0"} for oid in entries):
                raise archives.Unavailable("Stash new roots are malformed")
            roots = list(dict.fromkeys([current["oid"], *entries,
                                       *(old for old, _ in pairs if set(old) != {"0"})]))
            if len(roots) > 2048:
                raise archives.Unavailable("Stash roots exceed their bound")
            result.update(status="present", entries=entries)
        result["witnesses"] = [observations.witness("git_read", "stash_ref_read", current,
                                                     reference="reflog.ref"),
                                 observations.witness("filesystem_read", "stash_storage_read", storage,
                                                      reference="reflog")]
    except (archives.Unavailable, OSError, ValueError, UnicodeError) as exc:
        result["reasons"] = ["snapshot_incomplete", observations.reason(exc)]
        result["witnesses"] = [observations.witness("filesystem_read", "stash_storage_read", storage,
                                                     status="UNAVAILABLE", reference="reflog")]
    return result, roots, storage


def retaining_roots(project: Path, repository: dict) -> list[str]:
    """The archive basis includes direct retaining and every nonzero old/new log root."""
    result, roots, _ = _retaining(project, repository)
    if result["status"] == "unavailable":
        raise archives.Unavailable("Stash retaining reference or complete log is unavailable")
    return roots


def _context(project: Path, repository: dict) -> dict:
    paths = archives._text(project, ["rev-parse", "--path-format=absolute", "--git-dir",
                                    "--git-path", "objects", "--show-toplevel", "HEAD", "HEAD^{tree}"]).splitlines()
    if (len(paths) != 5 or paths[2] != str(project)
            or not repository.get("storage_complete", repository["complete"])):
        raise archives.Unavailable("Git storage context is incomplete")
    admin, objects = map(Path, paths[:2])
    common = Path(repository["common_dir"]["path"])
    identities = [observations.filesystem(path) for path in (project, admin, common, objects)]
    if not all(observations.reliable(value) for value in identities):
        raise archives.Unavailable("Git storage identities are incomplete")
    head, tree = map(archives._oid, paths[3:])
    unsupported = {}
    for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts", "shallow"):
        unsupported[name], _ = observations.slot(common / name, common)
        if unsupported[name]["status"] != "absent":
            raise archives.Unavailable("Unsupported or redirected storage metadata")
    binding = archives._text(project, ["symbolic-ref", "-q", "HEAD"], missing=True).strip() or None
    environment = {key: value for key, value in os.environ.items()
                   if key.startswith("GIT_") or key in {"PATH", "HOME", "XDG_CONFIG_HOME"}}
    if sum(len(key.encode()) + len(value.encode()) for key, value in environment.items()) > archives.MAX_OUTPUT:
        raise archives.Unavailable("Git context exceeds its bound")
    return {"project": identities[0], "admin": identities[1], "common": identities[2],
            "objects": identities[3], "head": head, "head_tree": tree, "head_ref": binding,
            "config_sha256": repository["config_sha256"], "environment_sha256": digest(environment),
            "unsupported_slots": unsupported,
            "origin_sha256": archives._run(project, ["config", "--get-all", "remote.origin.url"],
                                            hash_output=True, missing=True).decode("ascii")}


def snapshot(project: Path, repository: dict) -> dict:
    reflog, roots, storage = _retaining(project, repository)
    result = {"complete": False, "snapshot": [], "reflog": reflog, "storage": storage,
              "context": None, "closure": None, "reasons": [], "witnesses": []}
    try:
        if reflog["status"] == "unavailable":
            raise archives.Unavailable("Stash snapshot is incomplete")
        context = _context(project, repository)
        result["context"] = context
        if roots:
            types = archives._types(project, set(roots), limit=8192)
            if set(types.values()) != {"commit"}:
                raise archives.Unavailable("Stash roots must be commits")
            commits = archives._commits(project, set(roots), limit=8192)
            parents = {oid for value in commits.values() for oid in value["parents"]}
            if set(archives._types(project, parents, limit=8192).values()) != {"commit"}:
                raise archives.Unavailable("Stash parents must be commits")
            commits.update(archives._commits(project, parents - commits.keys(), limit=8192))
            needed = {oid: "commit" for oid in commits}
            needed.update({value["tree"]: "tree" for value in commits.values()})
            if archives._types(project, set(needed), limit=archives.MAX_TRAVERSAL_OBJECTS) != needed:
                raise archives.Unavailable("Stash roots, parents and trees have inconsistent types")
            for oid in roots:
                row = commits[oid]
                if (len(row["parents"]) not in (2, 3)
                        or commits[row["parents"][1]]["parents"] != [row["parents"][0]]
                        or len(row["parents"]) == 3 and commits[row["parents"][2]]["parents"]):
                    raise archives.Unavailable("Stash commit shape is malformed")
                result["snapshot"].append({"oid": oid, "tree": row["tree"], "parents": row["parents"],
                                             "parent_trees": [commits[parent]["tree"] for parent in row["parents"]]})
            closure = archives._walk_oids(project, set(roots))
            types = archives._types(project, closure, limit=archives.MAX_TRAVERSAL_OBJECTS)
            if not set(needed) <= closure:
                raise archives.Unavailable("Stash closure is incomplete")
            archives._run(project, ["fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", *roots])
        else:
            types = {}
        result["closure"] = {"roots": roots, "object_count": len(types),
                             "sha256": digest(sorted(types.items())), "integrity": "verified"}
        result["witnesses"] = [observations.witness("git_read", "stash_closure_read", result["closure"],
                                                     reference="support.closure")]
        # The enclosing producer takes this complete material observation before
        # and after all resource reads; it shares the pair without reusing old facts.
        result["complete"] = True
    except (archives.Unavailable, OSError, KeyError, ValueError, UnicodeError) as exc:
        result["reasons"] = sorted(set(result["reasons"] + ["snapshot_incomplete", observations.reason(exc)]))
        if roots and not result["snapshot"]:
            result["reasons"].append("malformed_stash")
    return result


def _executor(project: Path, ref: str | None, tip: str | None, kind: str, context: dict | None) -> dict:
    result = _unknown(("kind", "runtime", "version", "profile", "artifacts", "source_binding_sha256"),
                      "executor_binding_unavailable", operation="primitive_binding", kind="source_correspondence")
    result["kind"] = {"worktree": "orca_worktree_remove", "local_branch": "git_local_ref_delete",
                       "remote_branch": "git_remote_ref_delete"}.get(kind)
    if kind != "local_branch" or not ref or not tip or context is None:
        return result
    try:
        executable = shutil.which("git")
        if not executable:
            raise archives.Unavailable("Git executable is unavailable")
        path = Path(executable).resolve(strict=True)
        with archives._opened(path) as (descriptor, before):
            hashed = archives._hash(descriptor)
            if archives._identity(os.fstat(descriptor)) != before["file"]:
                raise archives.Unavailable("Git executable changed")
        version = archives._text(project, ["--version"]).strip()
        if not re.fullmatch(r"git version [0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[. -][A-Za-z0-9.-]+)?", version):
            raise archives.Unavailable("Git version is unsupported")
        artifact = {"kind": "member", "sha256": hashed, "reference": str(path)}
        source = hashlib.sha256(archives._bytes(Path(__file__))).hexdigest()
        binding = digest({"profile": PROFILE, "argv": local_argv(project, ref, tip), "artifact": artifact,
                          "source": source, "context": context})
        result.update(status="observed", version=version, profile=PROFILE, artifacts=[artifact],
                      source_binding_sha256=binding, reasons=[],
                      witnesses=[observations.witness("source_correspondence", "primitive_binding", binding,
                                                       reference="executor.source_binding_sha256")])
    except (archives.Unavailable, OSError, ValueError):
        pass
    return result


def _footprint(project: Path, ref: str | None, tip: str | None, kind: str, storage: dict | None,
               context: dict | None) -> dict:
    result = _unknown(("paths", "refs", "config", "native", "opaque", "sha256"),
                      "effect_footprint_unavailable", operation="primitive_footprint", kind="source_correspondence")
    result["opaque"] = ["effect_footprint_unavailable"]
    if kind != "local_branch" or storage is None:
        return result
    paths = []
    try:
        if (storage["status"] != "observed" or storage["current"]["status"] != "present"
                or storage["current"]["symref"] or storage["packed"]["status"] != "absent"):
            raise archives.Unavailable("Primitive storage footprint is unsupported")
        # Git may split an update of HEAD's referent into another HEAD-log
        # transaction. This profile does not claim that additional effect.
        if context is None or context["head_ref"] == ref:
            raise archives.Unavailable("Primitive HEAD-log effects require separate binding")
        packed_lock, _ = observations.slot(Path(storage["packed"]["path"] + ".lock"), Path("/"))
        if packed_lock["status"] != "absent":
            raise archives.Unavailable("Packed reference transaction is pending")
        paths.append({"path": packed_lock["path"], "identity": None, "operation": "unlink"})
        for key in ("loose", "reflog"):
            slot = storage[key]
            if slot["status"] not in {"present", "absent"}:
                raise archives.Unavailable("Primitive path footprint is incomplete")
            path = Path(slot["path"])
            paths.append({"path": str(path), "identity": slot["identity"], "operation": "unlink"})
            lock, _ = observations.slot(path.with_name(path.name + ".lock"), Path("/"))
            if lock["status"] != "absent":
                raise archives.Unavailable("Primitive has a pending reference transaction")
            paths.append({"path": lock["path"], "identity": None, "operation": "unlink"})
            # The files backend may prune empty ref/log directories. Only an
            # actually empty-after-unlink prefix can be removed; retained siblings stop it.
            parent = path.parent
            stop = Path(storage["packed"]["path"]).parent
            while parent != stop and parent.is_relative_to(stop):
                parent_slot, _ = observations.slot(parent, stop, directory=True)
                if parent_slot["status"] == "absent":
                    parent = parent.parent
                    continue
                if parent_slot["status"] != "present":
                    raise archives.Unavailable("Primitive ancestor footprint is unavailable")
                names = observations.directory_names(parent)
                if names - {path.name, path.name + ".lock"}:
                    break
                paths.append({"path": str(parent), "identity": parent_slot["identity"], "operation": "remove_tree"})
                path, parent = parent, parent.parent
        result.update(status="observed", paths=paths,
                      refs=[{"name": ref, "scope": "exact", "operation": "delete", "expected_oid": tip,
                             "resolution": [ref]}], config=[], native=[], opaque=[], reasons=[])
        result["sha256"] = digest({key: result[key] for key in ("paths", "refs", "config", "native", "opaque")})
        result["witnesses"] = [observations.witness("source_correspondence", "primitive_footprint",
                                                    result["sha256"], reference="footprint.sha256")]
    except (archives.Unavailable, OSError, ValueError) as exc:
        result["reasons"] = sorted(set(result["reasons"] + [observations.reason(exc)]))
    return result


def _holder_refs(path: Path, common: Path, admin: Path, deadline: float) -> list[dict]:
    output = archives._text(path, ["for-each-ref", "--format=%(refname) %(objectname) %(objecttype) %(symref)"], deadline=deadline)
    catalog = {}
    for line in output.splitlines():
        parts = line.split(" ")
        if len(parts) != 4 or not archives._ref(parts[0]) or parts[2] not in archives._TYPES:
            raise archives.Unavailable("Holder reference catalog is incomplete")
        name, oid, kind, target = parts
        archives._oid(oid)
        if name in catalog or len(catalog) >= MAX_HOLDER_REFS:
            raise archives.Unavailable("Holder reference catalog is duplicate or over its bound")
        catalog[name] = (oid, kind, target)
    refs = []
    for name, (oid, kind, target) in catalog.items():
        if not target and not name.startswith(archives._PRIVATE):
            continue
        resolution, current = [name], name
        while catalog[current][2]:
            current = catalog[current][2]
            if current in resolution or current not in catalog or len(resolution) >= 96:
                raise archives.Unavailable("Holder symbolic resolution is incomplete")
            resolution.append(current)
        if name.startswith(archives._PRIVATE):
            prefix = "main-worktree/" if admin == common else "worktrees/" + admin.name + "/"
            qualified = prefix + name
            if archives._text(path, ["rev-parse", "--verify", qualified], deadline=deadline).strip() != oid:
                raise archives.Unavailable("Holder private reference identity is incomplete")
            resolution[0] = qualified
            name = qualified
        refs.append({"name": name, "status": "present", "oid": oid, "object_type": kind,
                     "symref": target or None, "resolution": resolution})
    return refs


def _holders(project: Path, selected_ref: str | None, kind: str, orca_reader, context: dict | None) -> tuple[dict, list[dict]]:
    result = _unknown(("observations",), "holder_unavailable", operation="holder_read", kind="native_read")
    refs = []
    if kind != "local_branch" or context is None:
        return result, refs
    try:
        deadline, read_count = time.monotonic() + 30, 0
        def native_read(argv):
            nonlocal read_count
            remaining = deadline - time.monotonic()
            if remaining <= 0 or read_count >= 32:
                raise archives.Unavailable("Native holder inspection exceeded its bound")
            read_count += 1
            return orca_reader(argv, timeout=min(10, remaining))
        rows = []
        with worktree_inventory(project) as inventory:
            for row in worktree_rows(inventory):
                rows.append(row)
                if len(rows) > observations.MAX_ITEMS:
                    raise archives.Unavailable("Holder inventory exceeds its bound")
        holders, facts = [], []
        for row in rows:
            path, branch = row.get("worktree"), row.get("branch")
            if not isinstance(path, str) or not isinstance(branch, str):
                raise archives.Unavailable("Holder checkout identity is incomplete")
            directories = archives._text(Path(path), ["rev-parse", "--path-format=absolute", "--git-common-dir",
                                                       "--git-dir", "--git-path", "objects"], deadline=deadline).splitlines()
            if (len(directories) != 3 or directories[0] != context["common"]["path"]
                    or directories[2] != context["objects"]["path"]
                    or not observations.reliable(observations.filesystem(Path(path)))):
                raise archives.Unavailable("Holder storage context is incomplete")
            refs += _holder_refs(Path(path), Path(directories[0]), Path(directories[1]), deadline)
            shown = native_read(["worktree", "show", "--worktree", "path:" + path, "--json"])
            listed = native_read(["terminal", "list", "--worktree", "path:" + path, "--json"])
            native = shown["result"]["worktree"]
            terminals = listed["result"]["terminals"]
            git = native.get("git") or {}
            if (not shown.get("runtime") or listed.get("runtime") != shown["runtime"]
                    or native.get("path") != path or git.get("path") != path
                    or native.get("branch") != git.get("branch")
                    or "refs/heads/" + str(git.get("branch")).removeprefix("refs/heads/") != branch
                    or native.get("isBare") is not False or git.get("isBare") is not False
                    or listed["result"].get("truncated") is not False or not isinstance(terminals, list)):
                raise archives.Unavailable("Native holder inventory is incomplete")
            refs.append({"name": branch, "status": "present", "oid": row["HEAD"],
                         "object_type": "commit", "symref": None, "resolution": [branch]})
            facts.append({"path": path, "branch": branch, "oid": row["HEAD"], "runtime": shown["runtime"],
                          "worktree_id": native.get("id"), "terminals": []})
            for terminal in terminals:
                handle = terminal.get("handle")
                if (terminal.get("worktreePath") != path or not isinstance(handle, str)
                        or terminal.get("worktreeId") != native.get("id") or native.get("id") is None):
                    raise archives.Unavailable("Holder terminal binding is incomplete")
                readback = native_read(["terminal", "show", "--terminal", handle, "--json"])
                current = readback["result"]["terminal"]
                if (readback.get("runtime") != shown["runtime"] or current.get("handle") != handle
                        or current.get("worktreeId") != native["id"]):
                    raise archives.Unavailable("Holder terminal identity changed")
                cause = current.get("exitCause") or {}
                if current.get("connected") is True and current.get("writable") is True and current.get("orphaned") is False:
                    liveness = "live"
                elif (current.get("connected") is False and current.get("writable") is False
                      and cause.get("kind") in {"exited", "signaled", "operator_close"}):
                    liveness = "exited"
                else:
                    liveness = "unverifiable"
                binding = {"handle": handle, "worktree_id": native["id"], "runtime": shown["runtime"],
                           "path": path, "branch": branch}
                safe = {"binding": binding, "liveness": liveness}
                facts[-1]["terminals"].append(safe)
                role = native.get("role")
                holders.append({"binding_sha256": digest(binding), "resource_path": path,
                                "role": role if role in {"coordinator", "worker", "setup", "role"} else "unknown",
                                "liveness": liveness, "dependency": "inside" if branch == selected_ref else "unknown",
                                "source_sha256": digest(safe)})
        # Existing native metadata binds current placement/liveness. It supplies
        # no complete needed-name/dependency set, including terminal-less holders.
        # A different branch/path cannot manufacture that indispensable exclusion.
        result.update(observations=holders or None, reasons=["holder_unavailable"],
                      witnesses=[observations.witness("native_read", "holder_read", facts,
                                                       reference="holders.observations"),
                                 observations.witness("native_read", "holder_read", status="UNAVAILABLE",
                                                      reference="holders.current_dependencies")])
    except (archives.Unavailable, PodError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        result["reasons"] = sorted(set(result["reasons"] + [observations.reason(exc)]))
    return result, refs


def _worktree_stamp(facts: dict) -> dict:
    """Recheck every already-described admin byte/name without repeating graph traversal."""
    admin = Path(facts["git_dir"]["path"])
    path = Path(facts["observation_path"])
    directories = archives._text(path, ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"]).splitlines()
    if directories != [str(admin), facts["common_dir"]["path"], str(path)]:
        raise archives.Unavailable("Resource administrative binding changed")
    for key, current_path in (("directory", path), ("git_dir", admin), ("common_dir", Path(directories[1]))):
        if archives._directory(current_path) != facts[key]:
            raise archives.Unavailable("Resource directory incarnation changed")
    current_refs, current_source = {}, None
    for line in archives._text(path, ["for-each-ref", "--format=%(refname) %(objectname) %(symref)"]).splitlines():
        name, oid, symbolic = line.split(" ")
        archives._oid(oid)
        if name == facts["source_ref"]:
            current_source = oid if not symbolic else None
        elif not name.startswith(archives._PRIVATE) and not symbolic:
            current_refs[name] = oid
    if current_source != facts["source_tip"] or current_refs != facts["surviving_refs"]:
        raise archives.Unavailable("Resource source or surviving refs changed")
    if observations.directory_names(admin) != set(facts["admin_entries"]):
        raise archives.Unavailable("Resource administrative names changed")
    pending, found = [admin], {}
    while pending:
        parent = pending.pop()
        for name in observations.directory_names(parent):
            path = parent / name
            relative = path.relative_to(admin).as_posix()
            if path.is_dir():
                directory = observations.filesystem(path)
                if directory is None or relative.split("/", 1)[0] not in {"logs", "refs"}:
                    raise archives.Unavailable("Resource metadata is redirected or changed")
                found[relative] = directory
                pending.append(path)
            else:
                value, raw = observations.slot(path, admin)
                if value["status"] != "present":
                    raise archives.Unavailable("Resource metadata is unavailable")
                if relative.startswith("refs/"):
                    oid = facts["private_refs"].get(relative)
                    if oid is None or raw not in (oid.encode(), (oid + "\n").encode()):
                        raise archives.Unavailable("Resource private reference changed")
                elif relative.startswith("logs/"):
                    if value["sha256"] != (facts["reflogs"].get(relative[5:]) or {}).get("sha256"):
                        raise archives.Unavailable("Resource reflog changed")
                elif value["sha256"] != facts["metadata"].get(relative):
                    raise archives.Unavailable("Resource metadata changed")
                found[relative] = value
            if len(found) > archives.MAX_REFS:
                raise archives.Unavailable("Resource metadata exceeds its bound")
    expected = {*facts["metadata"], *facts["private_refs"], *("logs/" + key for key in facts["reflogs"])}
    if {name for name, value in found.items() if "status" in value} != expected:
        raise archives.Unavailable("Resource metadata names changed")
    return found


def _resource(project: Path, row: dict, context: dict | None) -> dict:
    result = _unknown(("path", "ref", "tip", "head_ref", "head_oid", "head_tree", "identity",
                       "private_dir", "common_dir", "local_refs", "origin_sha256"),
                      "resource_identity_unavailable", operation="resource_read")
    kind, source = row["kind"], row["identity"]
    current_path = ((source.get("current") or {}).get("path") if kind == "native"
                    else source.get("path") or str(project))
    path = Path(current_path) if current_path is not None else None
    result["path"] = current_path
    result["ref"] = source.get("branch") if kind == "worktree" else source.get("ref")
    result["tip"] = source.get("tip")
    if context is None or kind not in {"worktree", "local_branch", "remote_branch"}:
        result["identity"] = source.get("filesystem")
        return result
    facts = source.get("git") or {}
    try:
        if not result["ref"] or not result["tip"]:
            raise archives.Unavailable("Actual source reference identity is unavailable")
        if kind == "worktree" and not facts.get("complete"):
            raise archives.Unavailable("Current worktree identity is incomplete")
        stamp = _worktree_stamp(facts) if kind == "worktree" else None
        if kind == "worktree":
            from .cleanup import _dirt
            dirty, unavailable = _dirt(path)
            if dirty or unavailable:
                raise archives.Unavailable("Resource worktree data changed or needs preservation")
        identity = observations.filesystem(path)
        admin = (observations.filesystem(Path(facts["git_dir"]["path"]))
                 if kind == "worktree" else context["admin"])
        refs = []
        for name, oid in facts.get("private_refs", {}).items():
            refs.append({"name": name, "status": "present", "oid": oid,
                         "object_type": facts.get("required_objects", {}).get(oid),
                         "symref": None, "resolution": [name]})
        if not observations.reliable(identity) or not observations.reliable(admin):
            raise archives.Unavailable("Resource incarnation or mount identity is unavailable")
        head = facts.get("head", context["head"])
        tree = (facts.get("commits", {}).get(head) or {}).get("tree", facts.get("head_tree", context["head_tree"]))
        origin = (archives._run(path, ["config", "--get-all", "remote.origin.url"], hash_output=True,
                               missing=True).decode("ascii") if kind == "worktree" else context["origin_sha256"])
        result.update(status="observed", head_ref=facts.get("head_ref", context["head_ref"]),
                      head_oid=head, head_tree=tree, identity=identity, private_dir=admin,
                      common_dir=context["common"], local_refs=refs, origin_sha256=origin,
                      reasons=[], witnesses=[observations.witness("git_read", "resource_read", [facts, stamp],
                                                                   reference="resource")])
    except (archives.Unavailable, OSError, KeyError):
        pass
    return result


def _storage_exclusion(footprint: dict, support: dict, reflog: dict, stash_storage: dict) -> None:
    if footprint["status"] != "observed":
        return
    protected = [*(support["object_dirs"] or []), *(support["alternates"] or [])]
    protected += [value for value in (reflog["ref_identity"], reflog["log_identity"]) if value is not None]
    try:
        # Parent identities, no-follow opens and mount coordinates are indispensable
        # alongside inode checks: lexical separation does not exclude bind aliases.
        ancestors = [identity for key in ("loose", "packed", "reflog")
                     for identity in (stash_storage[key]["ancestors"] or [])]
        if not all(observations.reliable(value) for value in [*protected, *ancestors]):
            raise archives.Unavailable("Storage alias or incarnation facts are unavailable")
        proof = []
        for effect in footprint["paths"]:
            try:
                coordinate = archives._outside(Path(effect["path"]), [Path(value["path"]) for value in protected])
            except archives.Unavailable as exc:
                if "inside a removable" in str(exc):
                    footprint["reasons"].append("storage_overlap")
                    coordinate = {"overlap": True}
                else:
                    raise
            current = effect["identity"]
            if current and any((current["device"], current["inode"]) == (value["device"], value["inode"])
                               for value in protected):
                footprint["reasons"].append("storage_overlap")
            proof.append({"effect": effect, "coordinate": coordinate})
        footprint["witnesses"].append(observations.witness("filesystem_read", "storage_exclusion", proof,
                                                            reference="footprint.paths"))
    except (archives.Unavailable, OSError, KeyError, TypeError):
        footprint.update(status="unavailable", opaque=["storage_unavailable"])
        footprint["reasons"].append("storage_unavailable")
    footprint["reasons"] = sorted(set(footprint["reasons"]))


def _once(project: Path, row: dict, repository: dict, saved: dict, orca_reader) -> dict:
    context = saved["context"]
    kind, identity = row["kind"], row["identity"]
    resource = _resource(project, row, context)
    ref, tip = resource["ref"], resource["tip"]
    storage = (observations.storage_once(project, ref, repository)[0]
               if kind == "local_branch" and ref else None)
    executor = _executor(project, ref, tip, kind, context)
    footprint = _footprint(project, ref, tip, kind, storage, context)
    holders, holder_refs = _holders(project, ref, kind, orca_reader, context) if saved["snapshot"] else (
        _unknown(("observations",), "holder_unavailable", operation="holder_read", kind="native_read"), [])
    support = _unknown(("object_format", "object_dirs", "alternates", "refs", "reflog_sha256", "closure",
                        "git_context_sha256", "config_sha256", "unsupported"),
                       "storage_unavailable", operation="git_context_read")
    support["unsupported"] = ["storage_unavailable"]
    if context and saved["complete"]:
        refs = [saved["reflog"]["ref"], *holder_refs]
        refs = list({value["name"]: value for value in refs}.values())
        support.update(status="observed", object_format=repository["object_format"],
                       object_dirs=[context["objects"]], alternates=[], refs=refs,
                       reflog_sha256=saved["reflog"]["sha256"], closure=saved["closure"],
                       git_context_sha256=digest(context), config_sha256=repository["config_sha256"],
                       unsupported=[], reasons=[], witnesses=[*saved["witnesses"],
                           observations.witness("git_read", "git_context_read", context, reference="support")])
        if set(holders["reasons"]) - {"holder_unavailable"}:
            support.update(status="unavailable", reasons=holders["reasons"])
            support["witnesses"].append(observations.witness("native_read", "holder_read", status="UNAVAILABLE",
                                                            reference="holders.observations"))
    _storage_exclusion(footprint, support, saved["reflog"], saved["storage"])
    # The selected slot is diagnostic physical identity, not an ownership/release assertion.
    if storage is not None:
        resource["witnesses"].append(observations.witness("filesystem_read", "resource_storage_read", storage,
                                                          status="PASS" if storage["status"] == "observed" else "UNAVAILABLE",
                                                          reference="identity.ref_storage"))
        if (storage["status"] != "observed" or storage["current"]["status"] != "present"
                or storage["current"]["object_type"] != "commit"
                or storage["current"]["oid"] != tip
                or identity.get("ref_storage") is not None
                and {key: value for key, value in identity["ref_storage"].items() if key != "freshness"}
                    != {key: value for key, value in storage.items() if key != "freshness"}):
            resource.update(status="unavailable")
            resource["reasons"] = sorted(set(resource["reasons"] + storage["reasons"] + ["ref_identity_unavailable"]))
    return {"schema": "pod-stash-observation/v1", "complete": saved["complete"],
            "snapshot": saved["snapshot"], "reflog": saved["reflog"], "resource": resource,
            "executor": executor, "footprint": footprint, "support": support, "holders": holders}


_KEYS = {
    "reflog": "status ref entries ref_identity log_identity sha256 reasons witnesses",
    "resource": "status path ref tip head_ref head_oid head_tree identity private_dir common_dir local_refs origin_sha256 reasons witnesses",
    "executor": "status kind runtime version profile artifacts source_binding_sha256 reasons witnesses",
    "footprint": "status paths refs config native opaque sha256 reasons witnesses",
    "support": "status object_format object_dirs alternates refs reflog_sha256 closure git_context_sha256 config_sha256 unsupported reasons witnesses",
    "holders": "status observations reasons witnesses",
    "freshness": "before_sha256 after_sha256 stable reasons",
}
_OPERATIONS = {"stash_ref_read", "stash_storage_read", "stash_closure_read", "primitive_binding",
               "primitive_footprint", "holder_read", "resource_read", "resource_storage_read",
               "git_context_read", "storage_exclusion"}


def _text(value, *, nullable=False, limit=4096) -> bool:
    try:
        return value is None and nullable or (isinstance(value, str) and bool(value)
                and "\0" not in value and len(value.encode("utf-8")) <= limit)
    except UnicodeError:
        return False


def _sha(value, *, nullable=True) -> bool:
    return value is None and nullable or isinstance(value, str) and bool(archives._SHA256.fullmatch(value))


def _oid(value, *, nullable=True, width=None) -> bool:
    return value is None and nullable or (isinstance(value, str) and bool(archives.OBJECT_ID.fullmatch(value))
                                         and (width is None or len(value) == width))


def _list(value, check, *, nullable=False, limit=4096) -> bool:
    return value is None and nullable or isinstance(value, list) and len(value) <= limit and all(check(row) for row in value)


def _path(value, *, nullable=False) -> bool:
    return value is None and nullable or _text(value) and Path(value).is_absolute() and str(Path(value)) == value and ".." not in Path(value).parts


def _identity(value) -> bool:
    return value is None or (isinstance(value, dict)
        and set(value) == {"path", "device", "inode", "mode", "size", "mtime_ns", "ctime_ns", "mount_id", "incarnation"}
        and _path(value["path"]) and all(type(value[key]) is int and value[key] >= 0
            for key in ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns"))
        and all(value[key] is None or type(value[key]) is int and value[key] >= 0 for key in ("mount_id", "incarnation")))


def _ref(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"name", "status", "oid", "object_type", "symref", "resolution"}
            and _text(value["name"]) and value["status"] in {"present", "absent", "unavailable"}
            and _oid(value["oid"]) and value["object_type"] in {*archives._TYPES, None}
            and _text(value["symref"], nullable=True) and _list(value["resolution"], _text, nullable=True))


def _effect(value, kind) -> bool:
    if not isinstance(value, dict):
        return False
    if kind == "paths":
        return (set(value) == {"path", "identity", "operation"} and _path(value["path"])
                and _identity(value["identity"]) and value["operation"] in {"remove_tree", "unlink", "rewrite"})
    if kind == "refs":
        return (set(value) == {"name", "scope", "operation", "expected_oid", "resolution"}
                and _text(value["name"]) and value["scope"] in {"exact", "namespace"}
                and value["operation"] in {"delete", "update", "prune"} and _oid(value["expected_oid"])
                and _list(value["resolution"], _text, nullable=True))
    if kind == "config":
        return (set(value) == {"section", "operation", "sha256"} and _text(value["section"])
                and value["operation"] in {"remove_section", "rewrite"} and _sha(value["sha256"]))
    return (set(value) == {"kind", "operation", "binding_sha256"}
            and value["kind"] in {"worktree_metadata", "terminal_resource", "watcher", "registration", "cache"}
            and value["operation"] in {"remove", "update"} and _sha(value["binding_sha256"]))


def _holder(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"binding_sha256", "resource_path", "role", "liveness", "dependency", "source_sha256"}
            and _sha(value["binding_sha256"]) and _sha(value["source_sha256"]) and _path(value["resource_path"], nullable=True)
            and value["role"] in {"coordinator", "worker", "setup", "role", "unknown"}
            and value["liveness"] in {"live", "exited", "unverifiable"} and value["dependency"] in {"inside", "outside", "unknown"})


def _valid(record: dict, *, check_relations: bool = False) -> bool:
    try:
        if (set(record) != {"schema", "complete", "snapshot", "reflog", "resource", "executor",
                           "footprint", "support", "holders", "freshness", "relations"}
                or record["schema"] != "pod-stash-observation/v1" or type(record["complete"]) is not bool):
            return False
        for key, fields in _KEYS.items():
            if not isinstance(record[key], dict) or set(record[key]) != set(fields.split()):
                return False
            if not _list(record[key]["reasons"], lambda code: isinstance(code, str) and code in REASONS):
                return False
            for witness in record[key].get("witnesses", []):
                if (set(witness) != {"kind", "operation", "status", "sha256", "reference"}
                        or witness["operation"] not in _OPERATIONS
                        or witness["kind"] not in {"git_read", "filesystem_read", "native_read", "source_correspondence"}
                        or witness["status"] not in {"PASS", "FAILED", "UNAVAILABLE"}
                        or not _sha(witness["sha256"]) or not _text(witness["reference"], nullable=True)):
                    return False
        for key in ("resource", "executor", "footprint", "support", "holders"):
            if record[key]["status"] not in {"observed", "unavailable"}:
                return False
        log, resource, executor, footprint, support, holders, fresh = (
            record[key] for key in ("reflog", "resource", "executor", "footprint", "support", "holders", "freshness"))
        if (log["status"] not in {"present", "absent", "unavailable"} or not _ref(log["ref"])
                or not _list(log["entries"], lambda value: _oid(value, nullable=False), nullable=True, limit=2048)
                or not _identity(log["ref_identity"]) or not _identity(log["log_identity"]) or not _sha(log["sha256"])):
            return False
        if (not _path(resource["path"], nullable=True)
                or any(not _text(resource[key], nullable=True) for key in ("ref", "head_ref"))
                or any(not _oid(resource[key]) for key in ("tip", "head_oid", "head_tree"))
                or any(not _identity(resource[key]) for key in ("identity", "private_dir", "common_dir"))
                or not _list(resource["local_refs"], _ref, nullable=True) or not _sha(resource["origin_sha256"])):
            return False
        if (executor["kind"] not in {"orca_worktree_remove", "git_worktree_remove", "git_local_ref_delete", "git_remote_ref_delete", None}
                or any(not _text(executor[key], nullable=True, limit=256) for key in ("runtime", "version", "profile"))
                or not _sha(executor["source_binding_sha256"]) or not _list(executor["artifacts"], lambda value:
                    isinstance(value, dict) and set(value) == {"kind", "sha256", "reference"}
                    and value["kind"] in {"package", "member", "source"} and _sha(value["sha256"], nullable=False)
                    and _text(value["reference"]), nullable=True)):
            return False
        if any(not _list(footprint[key], lambda value, key=key: _effect(value, key), nullable=True)
               for key in ("paths", "refs", "config", "native")) or not _sha(footprint["sha256"]):
            return False
        if (not _list(footprint["opaque"], lambda code: isinstance(code, str) and code in REASONS)
                or not _list(support["unsupported"], lambda code: isinstance(code, str) and code in REASONS)
                or support["object_format"] not in {"sha1", "sha256", None}
                or any(not _list(support[key], _identity, nullable=True) for key in ("object_dirs", "alternates"))
                or not _list(support["refs"], _ref, nullable=True)
                or any(not _sha(support[key]) for key in ("reflog_sha256", "git_context_sha256", "config_sha256"))
                or not _list(holders["observations"], _holder, nullable=True)
                or any(not _sha(fresh[key]) for key in ("before_sha256", "after_sha256")) or type(fresh["stable"]) is not bool):
            return False
        closure = support["closure"]
        if closure is not None and (not isinstance(closure, dict)
                or set(closure) != {"roots", "object_count", "sha256", "integrity"}
                or not _list(closure["roots"], lambda oid: _oid(oid, nullable=False), limit=2048)
                or len(set(closure["roots"])) != len(closure["roots"])
                or type(closure["object_count"]) is not int or closure["object_count"] < 0
                or not _sha(closure["sha256"]) or closure["integrity"] not in {"verified", "unavailable", "failed"}):
            return False
        seen = set()
        for row in record["snapshot"]:
            if (set(row) != {"oid", "tree", "parents", "parent_trees"} or row["oid"] in seen
                    or len(row["parents"]) not in (2, 3) or len(row["parent_trees"]) != len(row["parents"])):
                return False
            seen.add(row["oid"])
            for oid in [row["oid"], row["tree"], *row["parents"], *row["parent_trees"]]:
                archives._oid(oid)
        if len(seen) > 2048:
            return False
        if not isinstance(record["relations"], list):
            return False
        if check_relations:
            actual = set()
            for relation in record["relations"]:
                if (not isinstance(relation, dict) or set(relation) != {"oid", "relation", "basis"}
                        or relation["oid"] in actual or relation["relation"] not in {"related", "unrelated", "unknown"}
                        or not isinstance(relation["basis"], dict)
                        or set(relation["basis"]) != {"kind", "reasons", "witnesses"}
                        or relation["basis"]["kind"] != {"related": "dependency_overlap", "unrelated": "removal_dependency_exclusion", "unknown": "unavailable"}[relation["relation"]]
                        or not _list(relation["basis"]["reasons"], lambda code: isinstance(code, str) and code in REASONS)
                        or not isinstance(relation["basis"]["witnesses"], list)):
                    return False
                actual.add(relation["oid"])
            if actual != seen:
                return False
        for key in ("object_dirs", "alternates"):
            for identity in record["support"][key] or []:
                if set(identity) != {"path", "device", "inode", "mode", "size", "mtime_ns", "ctime_ns", "mount_id", "incarnation"}:
                    return False
        if len(json.dumps(record, ensure_ascii=False).encode()) > archives.MAX_OUTPUT:
            return False
        return True
    except (TypeError, ValueError, KeyError, archives.Unavailable, UnicodeError):
        return False


def relations(record: dict) -> list[dict]:
    """Three states; only complete independently produced exclusion can be unrelated."""
    roots = [row["oid"] for row in record.get("snapshot", []) if isinstance(row, dict) and "oid" in row]
    unknown, overlaps = set(), set()
    if not _valid(record):
        unknown.add("snapshot_incomplete")
    else:
        if not record["complete"]:
            unknown.add("snapshot_incomplete")
        for key in ("reflog", "resource", "executor", "footprint", "support", "holders"):
            part = record[key]
            if part["status"] not in {"observed", "present", "absent"}:
                unknown.update(part["reasons"] or ["inspection_error"])
            for code in part["reasons"]:
                (overlaps if code in {"storage_overlap", "ref_alias_overlap", "private_ref_dependency", "holder_live"}
                 else unknown).add(code)
            if not part["witnesses"] or any(value["status"] != "PASS" or not value["sha256"] for value in part["witnesses"]):
                unknown.add("inspection_error")
        fresh = record["freshness"]
        if not fresh["stable"] or not fresh["before_sha256"] or fresh["before_sha256"] != fresh["after_sha256"]:
            unknown.update(fresh["reasons"] or ["stash_changed"])
        executor, footprint, support = record["executor"], record["footprint"], record["support"]
        if executor["kind"] != "git_local_ref_delete" or executor["profile"] != PROFILE or not executor["source_binding_sha256"]:
            unknown.add("executor_binding_unavailable")
        expected = {"name": record["resource"]["ref"], "scope": "exact", "operation": "delete",
                    "expected_oid": record["resource"]["tip"], "resolution": [record["resource"]["ref"]]}
        if (footprint["refs"] != [expected] or footprint["config"] != [] or footprint["native"] != []
                or footprint["opaque"] != []):
            unknown.add("effect_footprint_unavailable")
        if not support["closure"] or support["closure"]["integrity"] != "verified":
            unknown.add("snapshot_incomplete")
        if (support["object_format"] not in {"sha1", "sha256"} or not support["object_dirs"]
                or support["alternates"] != [] or support["unsupported"] != [] or support["refs"] is None
                or not support["git_context_sha256"] or not support["config_sha256"]
                or not all(observations.reliable(value) for value in support["object_dirs"] or [])):
            unknown.add("storage_unavailable")
        if (record["holders"]["observations"] is None or not footprint["paths"]
                or not footprint["sha256"] or not executor["artifacts"]
                or not all(observations.reliable(record["resource"][key])
                           for key in ("identity", "private_dir", "common_dir"))):
            unknown.add("resource_identity_unavailable")
        if footprint["sha256"] != digest({key: footprint[key] for key in ("paths", "refs", "config", "native", "opaque")}):
            unknown.add("effect_footprint_changed")
        if not any(value["operation"] == "primitive_footprint" and value["status"] == "PASS"
                   and value["sha256"] == digest(footprint["sha256"]) for value in footprint["witnesses"]):
            unknown.add("effect_footprint_changed")
        if not any(value["operation"] == "primitive_binding" and value["status"] == "PASS"
                   and value["sha256"] == digest(executor["source_binding_sha256"]) for value in executor["witnesses"]):
            unknown.add("executor_binding_unavailable")
        log = record["reflog"]
        snapshot_oids = {row["oid"] for row in record["snapshot"]}
        if (log["status"] != "present" or not log["entries"] or log["ref"]["status"] != "present"
                or log["ref"]["oid"] not in snapshot_oids or not set(log["entries"] or []) <= snapshot_oids
                or not observations.reliable(log["ref_identity"]) or not observations.reliable(log["log_identity"])
                or not log["sha256"] or not support["closure"] or set(support["closure"]["roots"]) != snapshot_oids):
            unknown.add("snapshot_incomplete")
        if log["ref"] not in (support["refs"] or []) or record["resource"]["local_refs"] is None:
            unknown.add("ref_identity_unavailable")
        width = 64 if support["object_format"] == "sha256" else 40
        all_oids = [oid for row in record["snapshot"] for oid in
                    [row["oid"], row["tree"], *row["parents"], *row["parent_trees"]]]
        if (any(len(oid) != width for oid in all_oids)
                or support["closure"] and support["closure"]["object_count"] < len(set(all_oids))):
            unknown.add("snapshot_incomplete")
        for ref in support["refs"] or []:
            if ref["status"] == "unavailable" or not ref["resolution"]:
                unknown.add("ref_identity_unavailable")
            if ref["status"] == "present" and expected["name"] in (ref["resolution"] or []):
                overlaps.add("ref_alias_overlap")
        for holder in record["holders"]["observations"] or []:
            if (holder["dependency"] == "unknown" or holder["liveness"] == "unverifiable"
                    or not holder["binding_sha256"] or not holder["source_sha256"]):
                unknown.add("holder_unavailable")
            elif holder["dependency"] == "inside":
                overlaps.add("holder_live" if holder["liveness"] == "live" else "private_ref_dependency")
    verdict = "unknown" if unknown else "related" if overlaps else "unrelated"
    kind = {"unknown": "unavailable", "related": "dependency_overlap", "unrelated": "removal_dependency_exclusion"}[verdict]
    witnesses = [value for key in ("reflog", "resource", "executor", "footprint", "support", "holders")
                 for value in record.get(key, {}).get("witnesses", [])]
    return [{"oid": oid, "relation": verdict, "basis": {"kind": kind,
             "reasons": sorted(unknown | overlaps), "witnesses": witnesses}} for oid in roots]


def _finish(before: dict, after: dict, saved: dict, fresh: dict) -> dict:
    before_hash, after_hash = digest(before), digest(after)
    inspected = (before_hash == after_hash and saved["complete"] and fresh["complete"]
                 and after["resource"]["status"] == after["support"]["status"] == "observed"
                 and after["reflog"]["status"] in {"present", "absent"})
    required = ["resource", "support"]
    if saved["snapshot"] or fresh["snapshot"]:
        required += ["executor", "footprint", "holders"]
    missing = [code for key in required if after[key]["status"] != "observed"
               for code in after[key]["reasons"]]
    stable = bool(inspected and not missing)
    changes = []
    for key, code in (("reflog", "reflog_changed"), ("snapshot", "stash_changed"),
                      ("resource", "resource_changed"), ("holders", "holder_changed"),
                      ("footprint", "effect_footprint_changed"), ("executor", "git_context_mismatch")):
        if before[key] != after[key]:
            changes.append(code)
    if before["support"]["config_sha256"] != after["support"]["config_sha256"]:
        changes.append("config_changed")
    after.update(complete=bool(inspected),
                 freshness={"before_sha256": before_hash, "after_sha256": after_hash, "stable": bool(stable),
                            "reasons": [] if stable else sorted(set(changes + missing or ["snapshot_incomplete"]))}, relations=[])
    after["relations"] = relations(after)
    if len(json.dumps(after, ensure_ascii=True).encode()) > archives.MAX_OUTPUT:
        raise PodError("cleanup_unavailable", "Stash observation exceeds its byte bound")
    return after


def observe_many(project: Path, rows: list[dict], repository: dict, saved: dict, *, orca_reader) -> list[dict]:
    """One fresh before/after closure pair shared by the resources it surrounds."""
    before = [_once(project, row, repository, saved, orca_reader) for row in rows]
    fresh_repository = archives.repository_observation(project)
    fresh = snapshot(project, fresh_repository)
    after = [_once(project, row, fresh_repository, fresh, orca_reader) for row in rows]
    return [_finish(left, right, saved, fresh) for left, right in zip(before, after)]


def observe(project: Path, row: dict, repository: dict, saved: dict, *, orca_reader) -> dict:
    return observe_many(project, [row], repository, saved, orca_reader=orca_reader)[0]


def holds(record: dict) -> bool:
    """Unknown snapshot is never empty; unrelated is the only positive exemption."""
    if (not _valid(record, check_relations=True) or not record["complete"] or not record["freshness"]["stable"]
            or record["reflog"]["status"] == "unavailable"):
        return True
    if not record["snapshot"]:
        log = record["reflog"]
        return not (log["status"] == "absent" and log["entries"] == [] and log["ref"]["status"] == "absent"
                    and log["ref"]["oid"] is None and log["ref_identity"] is log["log_identity"] is log["sha256"] is None
                    and log["witnesses"] and all(row["status"] == "PASS" and row["sha256"] for row in log["witnesses"]))
    return any(value["relation"] != "unrelated" for value in record["relations"])


def apply(row: dict, record: dict, *, branch_integrated: bool) -> None:
    """Preservation and relation can remove no independent admission hold."""
    row["stash_observation"] = record
    if holds(record):
        row["delete_argv"] = None
        if row["class"] in {"integrated", "merged_remote_head"}:
            row["class"] = "protected"
        row["reasons"] = sorted(set(row["reasons"] + ["stash observation is incomplete or has a removal dependency"]))
    elif (record["relations"] and branch_integrated and row["class"] == "unique"
          and row["kind"] == "local_branch" and row["reasons"] == ["branch has unique work or a repository stash"]):
        # A proved-unrelated stash creates no UNIQUE/copying obligation. The
        # existing associated-worktree/creation/admission hold remains conjunctive.
        row.update(**{"class": "protected"}, delete_argv=None, archive_argv=None,
                   reasons=["independent worktree and branch admission remain required"])

"""Current read-only filesystem/ref facts; observation is never phase continuity."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat

from . import cleanup_archives as archives
from .errors import PodError
from .util import digest, exact

MAX_ITEMS = 4096
KINDS = {"worktree", "local_branch", "remote_branch", "cache", "native"}


def witness(kind: str, operation: str, value=None, *, status="PASS", reference=None) -> dict:
    return {"kind": kind, "operation": operation, "status": status,
            "sha256": digest(value) if value is not None else None, "reference": reference}


def reason(exc: Exception) -> str:
    return "budget_exhausted" if "bound" in str(exc) else "inspection_error"


def selector(value: object) -> dict:
    try:
        row = exact(value, {"kind", "name"}, {"kind", "name"}, name="cleanup_observe")
        kind, name = row["kind"], row["name"]
        if (not isinstance(kind, str) or kind not in KINDS or not isinstance(name, str)
                or not name or "\0" in name or len(name.encode("utf-8")) > 4096):
            raise ValueError
        if kind in {"worktree", "cache"}:
            path = Path(name)
            if not path.is_absolute() or ".." in path.parts or str(path) != name:
                raise ValueError
        elif kind == "local_branch":
            if not name.startswith("refs/heads/") or not archives._ref(name):
                raise ValueError
        elif kind == "remote_branch":
            if "/" not in name or not archives._BRANCH.fullmatch(name):
                raise ValueError
        elif any(ord(char) <= 32 for char in name) or len(name) > 256:
            raise ValueError
        return dict(row)
    except (PodError, ValueError, UnicodeError, TypeError) as exc:
        raise PodError("invalid_cleanup_observe", "Observe needs exactly one bounded resource kind and name") from exc


def identity(path: Path, info, descriptor: int) -> dict:
    mount, incarnation = None, None
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", "rb") as source:
            raw = source.read(4097)
        if len(raw) <= 4096:
            rows = [line[7:].strip() for line in raw.splitlines() if line.startswith(b"mnt_id:")]
            if len(rows) == 1 and rows[0].isdigit():
                mount = int(rows[0])
        incarnation = archives._incarnation(descriptor)
    except (OSError, archives.Unavailable):
        pass
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "mount_id": mount, "incarnation": incarnation}


def reliable(value: dict | None) -> bool:
    return (isinstance(value, dict) and value.get("mount_id") is not None
            and value.get("incarnation") is not None)


def slot(path: Path, root: Path, *, directory: bool = False) -> tuple[dict, bytes | None]:
    """Read a no-follow slot, including successful absence and its existing parent chain.

    Raw bytes exist only inside this observation call and its parser; callers export
    the slot's digest. Descriptor/path facts are reobserved by the enclosing producer.
    """
    result = {"path": str(path), "status": "unavailable", "identity": None,
              "ancestors": None, "sha256": None}
    descriptors, observed, ancestors = [], [], []
    raw = None
    try:
        if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
            raise archives.Unavailable("Unresolved storage path")
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(current)
        for index, part in enumerate(path.parts[1:]):
            parent = Path(*path.parts[:index + 1])
            parent_identity = identity(parent, os.fstat(current), current)
            observed.append((current, parent_identity))
            if parent.is_relative_to(root):
                ancestors.append(parent_identity)
            is_directory = directory or index < len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                current = os.open(part, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=current)
            except FileNotFoundError:
                result.update(status="absent", ancestors=ancestors)
                break
            descriptors.append(current)
        else:
            info = os.fstat(current)
            if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or not directory and info.st_nlink != 1):
                raise archives.Unavailable("Unsupported or aliased storage slot")
            found = identity(path, info, current)
            observed.append((current, found))
            if not directory:
                if info.st_size > archives.MAX_OUTPUT:
                    raise archives.Unavailable("Storage metadata exceeds its bound")
                chunks, length = [], 0
                while block := os.read(current, min(65536, archives.MAX_OUTPUT + 1 - length)):
                    chunks.append(block)
                    length += len(block)
                    if length > archives.MAX_OUTPUT:
                        raise archives.Unavailable("Storage metadata exceeds its bound")
                raw = b"".join(chunks)
                if len(raw) != info.st_size:
                    raise archives.Unavailable("Storage metadata changed")
            result.update(status="present", identity=found, ancestors=ancestors,
                          sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None)
        for descriptor, before in observed:
            after = identity(Path(before["path"]), os.fstat(descriptor), descriptor)
            keys = before.keys() if Path(before["path"]).is_relative_to(root) else (
                "path", "device", "inode", "mode", "mount_id", "incarnation")
            if any(after[key] != before[key] for key in keys):
                raise archives.Unavailable("Storage metadata changed")
    except (OSError, archives.Unavailable):
        result.update(status="unavailable", identity=None, ancestors=None, sha256=None)
        raw = None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return result, raw


def filesystem(path: Path) -> dict | None:
    found, _ = slot(path, Path("/"), directory=True)
    return found["identity"] if found["status"] == "present" else None


def directory_names(path: Path) -> set[str]:
    names = set()
    with os.scandir(path) as entries:
        for entry in entries:
            names.add(entry.name)
            if len(names) > MAX_ITEMS:
                raise archives.Unavailable("Directory metadata exceeds its bound")
    return names


def empty_ref(name: str, status="unavailable") -> dict:
    return {"name": name, "status": status, "oid": None, "object_type": None,
            "symref": None, "resolution": [] if status == "absent" else None}


def logical_ref(project: Path, name: str, object_format: str | None) -> dict:
    row = empty_ref(name)
    output = archives._text(project, ["for-each-ref", "--format=%(refname) %(objectname) %(objecttype) %(symref)", name])
    matches = []
    for line in output.splitlines():
        parts = line.split(" ")
        if len(parts) != 4:
            raise archives.Unavailable("Reference observation is incomplete")
        if parts[0] == name:
            matches.append(parts)
    if not matches:
        return empty_ref(name, "absent")
    if len(matches) != 1:
        raise archives.Unavailable("Reference observation is ambiguous")
    _, oid, kind, symbolic = matches[0]
    if (len(archives._oid(oid)) != (64 if object_format == "sha256" else 40)
            or kind not in archives._TYPES):
        raise archives.Unavailable("Reference object format is inconsistent")
    row.update(status="present", oid=oid, object_type=kind, symref=symbolic or None,
               resolution=[name, symbolic] if symbolic else [name])
    return row


def reflog_pairs(raw: bytes, object_format: str) -> list[tuple[str, str]]:
    if raw and not raw.endswith(b"\n"):
        raise archives.Unavailable("Reflog is incomplete")
    result, width = [], 64 if object_format == "sha256" else 40
    for line in raw.splitlines():
        parts = line.split(b" ", 2)
        if len(parts) != 3 or b"\0" in line:
            raise archives.Unavailable("Reflog is malformed")
        metadata = parts[2].split(b"\t", 1)[0].rsplit(b" ", 2)
        if (len(metadata) != 3 or not metadata[0] or not metadata[1].lstrip(b"-").isdigit()
                or not re.fullmatch(rb"[+-][0-9]{4}", metadata[2])):
            raise archives.Unavailable("Reflog metadata is malformed")
        pair = tuple(archives._oid(value.decode("ascii")) for value in parts[:2])
        if any(len(value) != width for value in pair):
            raise archives.Unavailable("Reflog object format is inconsistent")
        result.append(pair)
        if len(result) > 2048:
            raise archives.Unavailable("Reflog exceeds its bound")
    return result


def _packed(raw: bytes, selected: str, object_format: str) -> str | None:
    if raw and not raw.endswith(b"\n"):
        raise archives.Unavailable("Packed refs are incomplete")
    selected_oid, seen, previous = None, set(), False
    for index, line in enumerate(raw.splitlines()):
        if line.startswith(b"# pack-refs with:") and index == 0:
            if set(line.removeprefix(b"# pack-refs with:").split()) - {b"peeled", b"fully-peeled", b"sorted"}:
                raise archives.Unavailable("Packed ref format is unsupported")
            continue
        if line.startswith(b"^"):
            if not previous:
                raise archives.Unavailable("Packed ref peel is ambiguous")
            oid = archives._oid(line[1:].decode("ascii"))
            previous = False
        else:
            oid, name = line.decode("utf-8").split(" ")
            if not archives._ref(name) or name in seen:
                raise archives.Unavailable("Packed refs are duplicate or unsupported")
            seen.add(name)
            if name == selected:
                selected_oid = oid
            previous = True
            archives._oid(oid)
        if len(oid) != (64 if object_format == "sha256" else 40):
            raise archives.Unavailable("Packed ref object format is inconsistent")
    return selected_oid


def storage_once(project: Path, name: str, repository: dict) -> tuple[dict, dict]:
    result = {"schema": "pod-ref-storage/v1", "status": "unavailable", "backend": None,
              "ref": name, "current": empty_ref(name), "loose": None, "packed": None,
              "reflog": None, "freshness": {"before_sha256": None, "after_sha256": None,
                                          "stable": False, "reasons": ["storage_unavailable"]},
              "reasons": []}
    contents = {}
    try:
        common = Path(repository["common_dir"]["path"])
        output = archives._text(project, ["rev-parse", "--path-format=absolute", "--git-path", name,
                                           "--git-path", "packed-refs", "--git-path", "logs/" + name]).splitlines()
        if len(output) != 3:
            raise archives.Unavailable("Storage paths are unresolved")
        for key, value in zip(("loose", "packed", "reflog"), output):
            result[key], contents[key] = slot(Path(value), common)
        if (not repository.get("storage_complete", repository["complete"])
                or any(result[key]["status"] == "unavailable" for key in contents)):
            raise archives.Unavailable("Reference storage is unavailable")
        result["backend"] = "files"
        current = logical_ref(project, name, repository["object_format"])
        result["current"] = current
        packed = _packed(contents["packed"], name, repository["object_format"]) if contents["packed"] is not None else None
        loose = contents["loose"]
        if loose is not None:
            if current["status"] != "present":
                raise archives.Unavailable("A present loose slot has no current reference")
            if loose.startswith(b"ref: ") or current["symref"]:
                result["reasons"].append("ref_symbolic")
                raise archives.Unavailable("Symbolic reference storage is unsupported")
            if loose not in ((current["oid"] or "").encode(), ((current["oid"] or "") + "\n").encode()):
                raise archives.Unavailable("Loose reference identity is inconsistent")
        elif current["symref"] or (current["oid"] != packed):
            raise archives.Unavailable("Logical and packed reference identities differ")
        if contents["reflog"] is not None:
            reflog_pairs(contents["reflog"], repository["object_format"])
        result["status"] = "observed"
    except (archives.Unavailable, OSError, KeyError, ValueError, UnicodeError):
        result["reasons"].append("storage_unavailable")
    for key in ("loose", "packed", "reflog"):
        if result[key] is None:
            result[key] = {"path": None, "status": "unavailable", "identity": None,
                           "ancestors": None, "sha256": None}
    return result, contents


def ref_storage(project: Path, name: str, repository: dict) -> dict:
    before, _ = storage_once(project, name, repository)
    after, _ = storage_once(project, name, repository)
    stable = before["status"] == after["status"] == "observed" and before == after
    after["freshness"] = {"before_sha256": digest(before), "after_sha256": digest(after),
                          "stable": stable, "reasons": [] if stable else ["ref_changed"]}
    if not stable:
        after["status"] = "unavailable"
        after["reasons"] = sorted(set(after["reasons"] + ["ref_changed"]))
    return after


def _provenance(kind: str, name: str, objective: str | None, state: dict | None, journal: dict,
                selected: dict | None) -> dict:
    unavailable = {"status": "unavailable", "source": None, "reference": None}
    if state is None or objective is None:
        return unavailable
    checkpoint = state.get("checkpoint") or {}
    bound = checkpoint.get("worktree") or {}
    if (kind == "worktree" and bound.get("path") == name
            or kind == "local_branch" and "refs/heads/" + str(bound.get("branch")) == name):
        return {"status": "observed", "source": "objective_checkpoint", "reference": objective}
    candidates = []
    for admission in state.get("admissions", {}).values():
        if selected is not None and admission.get("admission_id") != selected.get("admission_id"):
            continue
        placement = (admission.get("recovery") or {}).get("placement_binding") or {}
        binding = admission.get("native_binding") or {}
        if (kind == "worktree" and placement.get("path") == name
                or kind == "local_branch" and "refs/heads/" + str(placement.get("branch")) == name
                or kind == "native" and binding.get("dispatchId") == name):
            candidates.append(admission["admission_id"])
    if len(candidates) == 1:
        return {"status": "observed", "source": "admission", "reference": candidates[0]}
    candidates = []
    for unit, row in journal.get("units", {}).items():
        branch = row.get("branch") or {}
        if (kind == "local_branch" and "refs/heads/" + str(branch.get("branch")) == name
                or kind == "remote_branch" and name in row.get("published", {})):
            candidates.append(unit)
    if len(candidates) == 1:
        return {"status": "observed", "source": "governor", "reference": candidates[0]}
    return unavailable


def _worker_scope(selected: dict | None, observe: dict, remote_names: set[str]) -> bool:
    if selected is None:
        return True
    placement = (selected.get("recovery") or {}).get("placement_binding") or {}
    binding = selected.get("native_binding") or {}
    kind, name = observe["kind"], observe["name"]
    return bool(kind == "worktree" and name == placement.get("path")
                or kind == "local_branch" and name == "refs/heads/" + str(placement.get("branch"))
                or kind == "remote_branch" and name in remote_names
                or kind == "native" and name == binding.get("dispatchId"))


def _current_row(project: Path, observe: dict, repository: dict, *, base: dict | None,
                 state: dict | None, native_port) -> dict:
    from . import cleanup
    kind, name = observe["kind"], observe["name"]
    current = {"status": "unavailable", "path": None, "ref": None, "oid": None}
    git, worker = (base or {}).get("identity", {}).get("git"), None
    row = deepcopy(base) if base is not None else cleanup._resource(kind, name, {}, "protected", [])
    row.pop("fingerprint", None)
    row["identity"] = {}
    try:
        if kind == "worktree":
            path = Path(name)
            slot_fact, _ = slot(path, Path("/"), directory=True)
            by_path, checkouts, _ = cleanup._objective_worktrees(project, {name}, set())
            registration = by_path.get(name)
            current["path"] = name
            ref = registration.get("branch") if registration else None
            tip = archives._text(path, ["rev-parse", "--verify", "HEAD"]).strip() if registration else None
            if registration and slot_fact["status"] == "present":
                archives._oid(tip)
                current.update(status="present", ref=ref, oid=tip)
                git = archives.resource_observation(path, ref, tip, repository)
            elif registration is None and slot_fact["status"] == "absent":
                current["status"] = "absent"
            row["identity"] = {"path": name, "branch": ref, "tip": tip,
                               "terminals": (base or {}).get("identity", {}).get("terminals"),
                               "shared_with": sorted(other for other in checkouts.get(ref, []) if other != name),
                               "git": git, "worker": (base or {}).get("identity", {}).get("worker"),
                               "provenance": None, "current": current}
        elif kind == "local_branch":
            storage = ref_storage(project, name, repository)
            logical = storage["current"]
            tip = logical["oid"]
            _, checkouts, _ = cleanup._objective_worktrees(project, set(), {name})
            current.update(status=logical["status"] if storage["status"] == "observed" else "unavailable",
                           ref=name, oid=tip)
            if tip and logical["object_type"] == "commit":
                git = archives.reference_observation(project, name, tip, repository)
            row["identity"] = {"ref": name, "tip": tip, "checked_out": sorted(checkouts.get(name, [])),
                               "git": git, "worker": (base or {}).get("identity", {}).get("worker"),
                               "provenance": None, "current": current, "ref_storage": storage}
            if logical["symref"]:
                row["reasons"].append("ref_symbolic")
            if base is not None and row["delete_argv"] is not None and tip:
                from .cleanup_stashes import local_argv
                row["delete_argv"] = local_argv(project, name, tip)
        elif kind == "remote_branch":
            identity = (base or {}).get("identity", {})
            remote, ref, tip = identity.get("remote"), identity.get("ref"), identity.get("tip")
            current.update(status="present" if tip else "unavailable", ref=ref, oid=tip)
            row["identity"] = {"remote": remote, "ref": ref, "tip": tip, "git": git,
                               "worker": identity.get("worker"), "provenance": None, "current": current}
        elif kind == "cache":
            fact, _ = slot(Path(name), Path("/"), directory=True)
            current.update(status=fact["status"], path=name)
            row["identity"] = {"path": name, "filesystem": fact["identity"], "worker": None,
                               "provenance": None, "current": current}
        else:
            admission = next((value for value in (state or {}).get("admissions", {}).values()
                              if (value.get("native_binding") or {}).get("dispatchId") == name), None)
            safe = None
            if admission is not None:
                worker, _ = cleanup._worker_facts(admission, state, native_port, None)
                shown = native_port.show_worker(name)
                binding = admission.get("native_binding") or {}
                resource = shown["result"].get("terminalResource")
                from .operations import _assignment_evidence
                evidence = _assignment_evidence(shown, admission)
                safe = {"runtime": shown["runtime"], "binding": binding, "settled": evidence["settled"],
                        "resource": worker.get("release"), "liveness": worker.get("liveness"),
                        "ownership": worker.get("ownership"), "terminal": worker.get("terminal")}
                current["path"] = ((admission.get("recovery") or {}).get("placement_binding") or {}).get("path")
                if (isinstance(resource, dict) and resource.get("worktreeId") == binding.get("worktreeId")
                        and resource.get("terminalHandle") == binding.get("terminalHandle")
                        and resource.get("ownerDispatchId") == name
                        and resource.get("ownershipState") in {"owned", "released"}):
                    current["status"] = "present"
            row["identity"] = {"dispatch": name, "native": safe, "worker": worker,
                               "provenance": None, "current": current}
    except (archives.Unavailable, PodError, OSError, KeyError, ValueError, TypeError, AttributeError):
        row["reasons"].append("resource_identity_unavailable")
    # Every unavailable kind retains its exact required identity keys.
    if not row["identity"]:
        common = {"worker": None, "provenance": None, "current": current}
        if kind == "worktree":
            row["identity"] = {"path": name, "branch": None, "tip": None, "terminals": None,
                               "shared_with": None, "git": None, **common}
        elif kind == "local_branch":
            row["identity"] = {"ref": name, "tip": None, "checked_out": None, "git": None,
                               "ref_storage": ref_storage(project, name, repository), **common}
        elif kind == "remote_branch":
            row["identity"] = {"remote": None, "ref": None, "tip": None, "git": None, **common}
        elif kind == "cache":
            row["identity"] = {"path": name, "filesystem": None, **common}
        else:
            row["identity"] = {"dispatch": name, "native": None, **common}
    if current["status"] != "present":
        row["class"] = "unique" if row["class"] == "unique" else "protected"
        row["delete_argv"] = row["archive_argv"] = None
        row["reasons"].append("resource_absent" if current["status"] == "absent" else "resource_identity_unavailable")
    return row


def selected_plan(project: Path, objective: str | None, value: object, *, expect=None, archive_input=None,
                  worker=None, orca_reader, native_port=None) -> dict:
    from . import cleanup, cleanup_stashes
    from .evidence_closure import closed_older_state
    from .operations import OrcaPort
    from .standing import cleanup_projection
    from .util import bounded_text

    observe = selector(value)
    if not project.is_absolute() or str(project) != os.path.normpath(str(project)) or ".." in project.parts:
        raise PodError("invalid_cleanup_observe", "Observation project must be a normalized absolute checkout path")
    if objective is not None:
        bounded_text(objective, name="objective", limit=256)
    elif worker is not None:
        raise PodError("invalid_cleanup_observe", "Project-only observation cannot select a worker")
    rows = archives.archive_rows(archive_input)
    port = native_port or OrcaPort(project)
    state, ordinary, selected = None, None, None
    journal = {"actions": [], "units": {}}
    if objective is not None:
        state = closed_older_state(project, objective) or cleanup.read(project, objective)
        if state and isinstance(state.get("checkpoint"), dict):
            selected, _ = cleanup._selected_worker(state, worker)
            ordinary = cleanup._objective_plan(project, objective, archives=rows, worker=worker,
                                               orca_reader=orca_reader, native_port=port, stash_observations=False)
            remote_names = {row["id"].removeprefix("remote_branch:") for row in ordinary["resources"]
                            if row["kind"] == "remote_branch"}
            if not _worker_scope(selected, observe, remote_names):
                raise PodError("invalid_cleanup_observe", "Worker and observed resource scopes are incompatible")
            try:
                journal = cleanup._read_journal(cleanup._record_path(project, objective))
            except PodError:
                pass
        elif worker is not None:
            raise PodError("invalid_cleanup_observe", "Worker objective is unavailable")
    resource_id = observe["kind"] + ":" + observe["name"]
    base = next((row for row in (ordinary or {}).get("resources", []) if row["id"] == resource_id), None)
    repository = archives.repository_observation(project)
    row = _current_row(project, observe, repository, base=base, state=state, native_port=port)
    provenance = _provenance(observe["kind"], observe["name"], objective, state, journal, selected)
    row["identity"].update(provenance=provenance,
                           observation_scope={"mode": "objective" if objective is not None else "project_only",
                                              "objective": objective})
    if provenance["status"] != "observed":
        row["delete_argv"] = row["archive_argv"] = None
        row["class"] = "unique" if row["class"] == "unique" else "protected"
        row["reasons"].append("resource_provenance_unavailable")
    if base is None:
        facts = row["identity"].get("git")
        excluded = [Path(repository["common_dir"]["path"])] if "common_dir" in repository else []
        if observe["kind"] == "worktree":
            excluded.append(Path(observe["name"]))
        if facts:
            row["archives"] = [archives.verify(project, item, facts, excluded)
                               for item in rows if item["ref"] == facts.get("source_ref")]
            row["archive_verified"] = any(item["verified"] and item["covers_resource"] for item in row["archives"])
    saved = cleanup_stashes.snapshot(project, repository)
    observation = cleanup_stashes.observe(project, row, repository, saved, orca_reader=orca_reader)
    target = (ordinary or {}).get("target", {"ref": None, "commit": None, "moved_after_delivery": False})
    tip = row["identity"].get("tip")
    integrated = bool(tip and target["commit"] and cleanup.is_ancestor(project, tip, target["commit"]) is True)
    cleanup_stashes.apply(row, observation, branch_integrated=integrated)
    if observe["kind"] in {"cache", "native"} or objective is None:
        row["delete_argv"] = row["archive_argv"] = None
    if observe["kind"] in {"cache", "native"}:
        row.update(archives=[], archive_verified=False)
    row["reasons"] = sorted(set(row["reasons"]))
    plan_hash, fingerprints = cleanup._expect_change(expect, [row], target)
    row["fingerprint"] = fingerprints[row["id"]]
    result = {"schema": "pod-cleanup-plan/v1", "objective": objective,
              "closure_basis": (ordinary or {}).get("closure_basis"),
              "record_class": (ordinary or {}).get("record_class", "current") if objective is not None else "unavailable",
              "standing_authorization": cleanup_projection(state, [row]) if state else {
                  "mode": "normal", "decision": None, "covers": [], "asks": [row["id"]]},
              "target": target, "resources": [row], "digest": plan_hash,
              "expect": {"plan": plan_hash, "resources": fingerprints},
              "notes": ["Current read-only observation; matching expect proves stability only.",
                        "Current absence and provenance are no completed effect, original identity, release or authority.",
                        "The direct local-ref primitive covers no wider CE, config, cache, remote or Orca action."],
              "observation": {"schema": "pod-cleanup-observation/v1", "selector": observe, "read_only": True}}
    if ordinary and "worker" in ordinary:
        result["worker"] = ordinary["worker"]
    if len(str(result).encode()) > archives.MAX_OUTPUT:
        raise PodError("cleanup_unavailable", "Selected observation exceeds its byte bound")
    return result

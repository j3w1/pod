"""Known Git-phase recovery DATA, separate from existing relations and executor authority."""

from __future__ import annotations

from pathlib import Path

from . import cleanup_archives as archives
from . import cleanup_files as files
from . import cleanup_observations as observations
from . import cleanup_stashes as stashes
from .util import digest

EXECUTOR_CHECKS = (
    "authenticated_finite_owner_scope", "current_inode_process_scan", "no_attached_orca_terminal",
    "no_open_live_keep_reuse_role_default_or_shared_binding", "current_source_bytes_status_tip_identity",
    "current_checkout_elsewhere", "remote_origin_lease_policy_open_pr",
    "new_operation_archive_scope_phase_continuity", "native_public_absence_or_hold",
)
REASONS = frozenset({"file_archive_missing", "file_archive_unavailable", "file_archive_changed",
    "file_archive_unsafe", "file_archive_budget", "manifest_invalid", "manifest_incomplete",
    "file_recovery_missing", "file_restore_failed", "file_current_changed", "file_current_unavailable",
    "admin_recovery_missing", "unsupported_file_type", "unsupported_link", "unsupported_mount",
    "unsupported_admin_state", "expanded_git_archive_required", "known_phase_footprint_unavailable",
    "stash_support_overlap", "executor_checks_required"})


def _phase_footprint(project, row, facts, saved, repository):
    """Only the named Git operation, with actual current paths, slots and identities."""
    kind, context = row["kind"], saved["context"]
    if not context or not facts.get("complete"):
        raise archives.Unavailable("Current material is incomplete")
    if kind in {"local_branch", "remote_branch"}:
        ref, tip = facts["source_ref"], facts["source_tip"]
        storage = observations.ref_storage(project, ref, repository)
        footprint = stashes._footprint(project, ref, tip, "local_branch", storage, context)
        if kind == "remote_branch":
            # No remote-server/storage effect profile is established by a local tracking name.
            raise archives.Unavailable("Remote footprint is unavailable")
        if row["identity"].get("checked_out") != []:
            raise archives.Unavailable("A current checkout still holds the reference")
        if footprint["status"] != "observed" or footprint["reasons"]:
            raise archives.Unavailable("Selected reference footprint is unavailable")
        return footprint
    if kind != "worktree":
        raise archives.Unavailable("No supported Git phase")
    paths = []
    for key in ("directory", "git_dir"):
        path = Path(facts[key]["path"])
        value = observations.filesystem(path)
        if not observations.reliable(value):
            raise archives.Unavailable("Worktree footprint identity is unavailable")
        paths.append({"path": str(path), "identity": value, "operation": "remove_tree"})
    # Normal worktree remove destroys this private admin, never the source/common refs or objects.
    refs = [{"name": name, "scope": "exact", "operation": "delete", "expected_oid": oid,
             "resolution": [name]} for name, oid in sorted(facts["private_refs"].items())]
    footprint = {"status": "observed", "paths": paths, "refs": refs,
                 "config": [], "native": [], "opaque": [], "reasons": [], "witnesses": []}
    footprint["sha256"] = digest({key: footprint[key] for key in ("paths", "refs", "config", "native", "opaque")})
    return footprint


def _noninterference(project, row, facts, saved, repository):
    result = {"status": "unavailable", "snapshot_sha256": digest(saved),
              "support_sha256": None, "footprint_sha256": None, "reasons": []}
    basis = {}
    try:
        if not saved["complete"] or saved["closure"] is None or saved["closure"]["integrity"] != "verified":
            raise archives.Unavailable("Stash material is unavailable")
        context = saved["context"]
        footprint = _phase_footprint(project, row, facts, saved, repository)
        support = {"object_dirs": [context["objects"]], "alternates": [],
                   "refs": [saved["reflog"]["ref"]], "closure": saved["closure"],
                   "storage": saved["storage"], "context": context}
        result.update(support_sha256=digest(support), footprint_sha256=digest(footprint))
        stashes._storage_exclusion(footprint, support, saved["reflog"], saved["storage"])
        if footprint["status"] != "observed":
            raise archives.Unavailable("Physical exclusion is unavailable")
        overlap = bool(footprint["reasons"])
        # Both directions matter: a removed tree must not CONTAIN a retaining slot/object directory.
        protected = [context["objects"], *(value for key in ("loose", "packed", "reflog")
                      if (value := saved["storage"][key]["identity"]) is not None)]
        coordinates = []
        for effect in footprint["paths"]:
            for value in protected:
                try:
                    coordinates.append(archives._outside(Path(value["path"]), [Path(effect["path"])]))
                except archives.Unavailable as exc:
                    if "inside a removable" not in str(exc):
                        raise
                    overlap = True
        deleted_names = {effect["name"] for effect in footprint["refs"]}
        if deleted_names.intersection(saved["reflog"]["ref"]["resolution"] or []):
            overlap = True
        basis.update(footprint=footprint, support=support, coordinates=coordinates)
        result.update(status="overlap" if overlap else "preserved_by_known_git_phases",
                      footprint_sha256=digest(footprint), reasons=["stash_support_overlap"] if overlap else [])
    except (archives.Unavailable, OSError, KeyError, TypeError, ValueError):
        result["reasons"] = ["known_phase_footprint_unavailable"]
    return result, basis


def apply(project, row, archive_input, repository, saved, *, excluded=()):
    """Add exactly the optional v1 record; leave every existing decision field untouched."""
    identity = row["identity"]
    original = identity.get("git") or {}
    ref = original.get("source_ref")
    candidates = [item for item in archive_input if "worktree_archive" in item and item["ref"] == ref]
    if not candidates or row["kind"] not in {"worktree", "local_branch", "remote_branch"}:
        return
    # The input admits only one row per exact ref/tip. A changed tip still produces held data.
    selected = next((item for item in candidates if item["tip"] == original.get("source_tip")), candidates[0])
    phases = {"worktree": ["git_worktree_remove"], "local_branch": ["git_local_ref_delete"],
              "remote_branch": ["git_tracking_ref_delete", "git_remote_ref_delete"]}[row["kind"]]
    excluded = [*excluded, *([Path(repository["common_dir"]["path"])] if "common_dir" in repository else [])]
    if row["kind"] == "worktree":
        excluded.append(Path(identity["path"]))
        facts = archives.resource_observation(Path(identity["path"]), identity.get("branch"),
                                              identity.get("tip"), repository, known_phases=True)
    else:
        facts = archives.reference_observation(project, original.get("source_ref"),
                                               original.get("source_tip"), repository, known_phases=True)
    if facts.get("git_dir"):
        excluded.append(Path(facts["git_dir"]["path"]))
    git = archives.verify(project, selected, facts, excluded, known_phases=True)
    file, admin, file_basis = files.verify(project, selected, excluded, resource=facts)
    noninterference, phase_basis = _noninterference(project, row, facts, saved, repository)
    fresh_repository = archives.repository_observation(project)
    fresh = stashes.snapshot(project, fresh_repository)
    observer = archives.resource_observation if row["kind"] == "worktree" else archives.reference_observation
    fresh_facts = observer(Path(facts["observation_path"]), facts["source_ref"], facts["source_tip"], fresh_repository,
                           known_phases=True)
    fresh_phase, fresh_basis = _noninterference(project, row, fresh_facts, fresh, fresh_repository)
    if (not fresh["complete"] or fresh != saved or fresh_facts != facts
            or fresh_phase != noninterference or fresh_basis != phase_basis):
        noninterference.update(status="unavailable", reasons=["known_phase_footprint_unavailable"])
    # Match the physical private inventory to the complete Git logical/ref/log observation.
    captured = file_basis.get("admin")
    if row["kind"] == "worktree" and captured is not None and (
            captured["refs"] != facts.get("private_refs") or captured["logs"] != facts.get("reflogs")):
        admin.update(status="held", reasons=["unsupported_admin_state"])
    covering = git["verified"] and git["covers_resource"]
    combined = bool(covering and file["status"] == admin["status"] == "verified" and file["restore_verified"])
    reasons = {"executor_checks_required"}
    if not covering:
        reasons.add("expanded_git_archive_required")
    reasons.update(file["reasons"])
    reasons.update(admin["reasons"])
    reasons.update(noninterference["reasons"])
    absent = (file_basis.get("current", {}).get("current") or {}).get("status") == "absent"
    current_ok = file["current_match"] is True or (
        row["kind"] != "worktree" and file["current_match"] is None and absent)
    if row["kind"] == "worktree" and file["current_match"] is True:
        try:
            raw = archives._run(Path(identity["path"]), ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"])
            if any(entry and not entry.startswith(b"!! ") for entry in raw.split(b"\0")):
                current_ok = False
                reasons.add("file_current_changed")
        except archives.Unavailable:
            current_ok = False
            reasons.add("file_current_unavailable")
    status = ("data_eligible" if combined and current_ok
              and noninterference["status"] == "preserved_by_known_git_phases" else "held")
    row["reversible_eligibility"] = {
        "schema": "pod-reversible-eligibility/v1", "status": status, "phases": phases,
        "archive_verified": combined,
        "git_recovery": {"verified_covering": bool(covering),
                         "archive_sha256s": [git["sha256"]] if "sha256" in git else None},
        "file_recovery": file, "admin_recovery": admin, "stash_noninterference": noninterference,
        "executor_checks_required": list(EXECUTOR_CHECKS), "reasons": sorted(reasons),
    }
    # The public material diagnostic binds private identities and full verification facts,
    # without exporting manifest names, raw status/config/log bytes or a parallel authority record.
    identity["git"] = {**original, "reversible_recovery": {"git": git, "files": file_basis,
                        "phases": phase_basis, "fresh_snapshot_sha256": digest(fresh)}}

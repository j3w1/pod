"""Small public Pod launcher surface; private mechanics stay in internal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .bundle import (RELOAD_ACTION, bundle_root, checkpoint_identity, identity_drift_message,
                     identity_label, identity_matches, running_identity, version)
from .catalog import age, by_id, load as load_catalog, ranks, reference_rows
from .config import load as load_config, personal_path, read_yaml, write_defaults
from .errors import PodError
from .ledger import context_root_for_run, state_inventory
from .orca import contract, current_run, worker_rows
from .placement import inspect as inspect_placements, skills_cli_entry
from .selection import active_constraints


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pod", description="In-session Orca coordination")
    root.add_argument("--version", action="version", version=version())
    sub = root.add_subparsers(dest="command")
    config = sub.add_parser("config", help="show or edit personal model preferences")
    config.add_argument("config_action", nargs="?", choices=("edit",))
    config.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor", help="read-only installation and capability diagnostics")
    doctor.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="read-only objective and native work status")
    status.add_argument("--run")
    status.add_argument("--json", action="store_true")
    sub.add_parser("update", help="update the installed bundle")
    return root


def _edit(path: Path) -> dict:
    if not path.exists():
        write_defaults(path)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise PodError("editor_unavailable", "Set VISUAL or EDITOR before running config edit")
    argv = shlex.split(editor)
    if not argv:
        raise PodError("editor_unavailable", "Editor command is empty")
    try:
        completed = subprocess.run([*argv, str(path)], check=False)
    except OSError as exc:
        raise PodError("editor_failed", "Editor could not start") from exc
    if completed.returncode:
        raise PodError("editor_failed", "Editor did not complete")
    try:
        read_yaml(path)
    except PodError as exc:
        return {"status": "invalid", "reason": exc.code, "path": str(path),
                "message": "Edited file was kept; correct it before delegation"}
    return {"status": "valid", "path": str(path)}


def _config(project: Path, *, edit: bool) -> dict:
    if edit:
        result = _edit(personal_path(project))
    else:
        result = {"status": "valid"}
    snapshot = load_config(project)
    document = load_catalog()
    result.update({"schema": "pod-cli/v4", "path": snapshot["path"],
                   "revision": snapshot["revision"], "mode": snapshot["mode"],
                   "saved": snapshot["saved"], "effective": snapshot["effective"],
                   "eligible": snapshot["eligible"], "not_set": snapshot["not_set"],
                   "not_set_meaning": "not set (not eligible)",
                   "max_active": snapshot["max_active"],
                   "policy_revision": snapshot["policy_revision"], "errors": snapshot["errors"],
                   "catalog": [{"id": row["id"], "name": row["name"], "agent": row["agent"],
                                "efforts": [effort for effort in row["efforts"] if effort != "ultra"],
                                "guidance": row["guidance"]} for row in document["models"]]})
    if snapshot["errors"]:
        result["status"] = "invalid"
    return result


def _doctor(project: Path) -> dict:
    from . import installer
    running = running_identity()
    preferences = load_config(project)
    snapshot = contract()
    try:
        state = state_inventory(project)
    except PodError as exc:
        state = {"blocked": True, "reason": exc.code}
    state = {**state, "scope": "affected_objectives_only", "blocks_unrelated_objectives": False}
    try:
        paths = installer._paths()
        receipt_path = paths["data"] / "install.json"
        receipt = installer.read_receipt(receipt_path)
        target = receipt.get("target") if isinstance(receipt, dict) and isinstance(receipt.get("target"), dict) else {}
        receipt_identity = {"version": target.get("version"), "bundle_digest": target.get("digest")} if receipt else None
        drift = not identity_matches(receipt_identity, running) if receipt_identity else None
        canonical_digest = installer.optional_digest(paths["canonical"])
        venv = installer._venv_path(paths)
        launcher = paths["launcher"]
        template = installer._launcher_template(venv / "bin/python", paths["canonical"])
        launcher_ownership = installer.launcher_info(launcher, template)
        found = shutil.which("pod")
        shadowed = bool(found and Path(found).resolve(strict=False) != launcher.resolve(strict=False))
        installed = ("not installed by the one-shot installer" if receipt is None else
                     "installed" if receipt.get("status") == "installed" else "installing")
        installation_checks = {"receipt_path": str(receipt_path), "receipt_status": receipt.get("status") if receipt else None,
                               "receipt_version": target.get("version"),
                               "receipt_digest": target.get("digest"),
                               "canonical_digest": canonical_digest,
                               "digest_matches": bool(receipt and canonical_digest == target.get("digest")),
                               "venv": {"path": str(venv), "ready": installer._venv_ready(venv), "pin": installer.PIN},
                               "duplicates": installer._duplicates(paths),
                               "lock_entry": skills_cli_entry()}
        installation_checks["running_identity"] = running
        installation_checks["receipt_identity"] = receipt_identity
        installation_checks["installed_version_drift"] = drift
        installation_checks["healthy"] = bool(installed == "installed" and installation_checks["digest_matches"]
                                               and installation_checks["venv"]["ready"]
                                               and launcher_ownership["owned"]
                                               and (paths["claude"] / "skills/pod").resolve(strict=False)
                                               == paths["canonical"].resolve(strict=False))
        if receipt is not None and not installation_checks["healthy"]:
            installed = "installation needs attention"
            installation_checks["next_action"] = "rerun the one-shot installer"
    except (installer.InstallError, OSError, ValueError) as exc:
        receipt = None
        launcher = Path.home() / ".local/bin/pod"
        launcher_ownership = {"owned": False, "reason": "unavailable"}
        shadowed = False
        installed = "installation needs attention"
        installation_checks = {"error": str(exc)}
        receipt_identity = None
        drift = None
    return {"schema": "pod-cli/v4", "status": "observed", "version": version(),
            "bundle": str(bundle_root()), "installation": installed,
            "bundle_identity": {"running": running, "receipt": receipt_identity, "drift": drift},
            "installed_version_drift": drift,
            "installation_checks": installation_checks,
            "launcher": {"path": str(launcher), "exists": launcher.is_file(), "shadowed": shadowed,
                         "ownership": launcher_ownership},
            "placements": inspect_placements(project), "skills_cli": skills_cli_entry(),
            "preferences": {key: preferences[key] for key in ("path", "revision", "mode", "eligible", "not_set",
                                                           "max_active", "errors", "policy_revision")},
            "catalog": {"models": list(by_id()), "ranks_of_six": ranks(),
                        "benchmark_age_days": age()},
            "orca": {"status": snapshot.get("status"), "capabilities": snapshot.get("capabilities", {}),
                     "runtime_state": snapshot.get("runtime_state"),
                     "reason": snapshot.get("reason")},
            "state": state, "native_probe": "not_run"}


def _status(project: Path, run: str | None) -> dict:
    preferences = load_config(project)
    running = running_identity()
    result = {"schema": "pod-cli/v4", "status": "selection_required", "run": run,
              "bundle_identity": {"running": running, "checkpoint": None, "drift": None},
              "preferences": {key: preferences[key] for key in ("path", "revision", "mode", "eligible", "not_set",
                                                               "max_active", "errors")},
              "constraints": [], "route_decisions": [], "route_mismatch": False,
              "active_constraints": [],
              "effective_unknown": False,
              "installed_version_drift": False, "blocker": None,
              "next_safe_action": "select a native Run", "usage": "unknown", "cost": "unknown"}
    if run is None:
        try:
            current = current_run().get("run")
            run = current.get("id") if isinstance(current, dict) else None
        except PodError as exc:
            result.update({"status": "unavailable", "blocker": exc.code})
            return result
        if run is None:
            return result
    result["run"] = run
    native_error = None
    try:
        workers = worker_rows(run)
    except PodError as exc:
        native_error = exc.code
        workers = {"workers": [], "scope": None, "complete": False}
    from .ledger import _read
    try:
        context_root = context_root_for_run(run)
        state = _read(context_root / "context.json") if context_root else None
    except PodError as exc:
        result.update({"status": "blocked", "blocker": exc.code,
                       "next_safe_action": "inspect the objective state"})
        return result
    checkpoint = state.get("checkpoint") if state else None
    recorded = checkpoint_identity(checkpoint) if checkpoint else None
    drift = bool(checkpoint and not identity_matches(recorded, running))
    admissions = list(state["admissions"].values()) if state else []
    checkpoint_refs = checkpoint.get("native_refs") if isinstance(checkpoint, dict) else None
    checkpoint_assignments = checkpoint.get("assignments") if isinstance(checkpoint, dict) else None
    checkpoint_refs = checkpoint_refs if isinstance(checkpoint_refs, list) else []
    checkpoint_assignments = checkpoint_assignments if isinstance(checkpoint_assignments, list) else []
    native_references = []
    for row in admissions[:64]:
        binding = row.get("native_binding") if isinstance(row.get("native_binding"), dict) else {}
        native_references.append({"admission_id": row["admission_id"], "run": row["run_id"],
                                  "task": row["task_id"], "dispatch": binding.get("dispatchId"),
                                  "worker": binding.get("workerId"),
                                  "terminal": binding.get("terminalHandle"),
                                  "state": row["state"], "runtime": row["runtime"]})
    result.update({"status": "unavailable" if native_error else "observed",
                   "objective": checkpoint.get("objective") if checkpoint else None,
                   "source": checkpoint.get("objective_source", "direct_objective") if checkpoint else None,
                   "selected_worktree": checkpoint.get("worktree") if checkpoint else None,
                   "constraints": state["constraints"] if state else [],
                   "active_constraints": active_constraints(state["constraints"], preferences) if state else [],
                   "route_decisions": [row["route_decision"] for row in admissions[:64]],
                   "native_references": native_references,
                   "native_references_truncated": len(admissions) > 64,
                   "checkpoint_join": {"native_refs": checkpoint_refs[:64],
                                       "assignments": checkpoint_assignments[:64],
                                       "plan_revision": checkpoint.get("plan_revision"),
                                       "candidate": checkpoint.get("candidate")}
                                      if checkpoint else None,
                   "route_mismatch": any(row["route_decision"].get("route_mismatch") for row in admissions),
                   "effective_unknown": any(row["route_decision"].get("effective_unknown") for row in admissions),
                   "installed_version_drift": drift,
                   "bundle_identity": {"checkpoint": recorded, "running": running, "drift": drift},
                   "installed_version_detail": identity_drift_message(checkpoint, running=running) if drift else None,
                   "native": {"worker_count": len(workers["workers"]), "scope": workers.get("scope"),
                              "complete": workers.get("complete")},
                   "pending_admissions": sum(row["state"] in ("reserved", "unresolved") for row in admissions),
                   "verification_gaps": checkpoint.get("verification_gaps", []) if checkpoint else [],
                   "remaining_gates": checkpoint.get("remaining_gates", []) if checkpoint else [],
                   "blocker": "route_mismatch" if any(row["route_decision"].get("route_mismatch") for row in admissions)
                              else "effective_unknown" if any(row["route_decision"].get("effective_unknown") for row in admissions)
                              else native_error or checkpoint.get("blocker") if checkpoint else native_error,
                   "next_safe_action": checkpoint.get("next_safe_action", "inspect native Run")
                                       if checkpoint else "inspect native Run"})
    if result["installed_version_drift"]:
        result["blocker"] = "installed_version_changed"
        result["next_safe_action"] = RELOAD_ACTION
    elif preferences["errors"] and result["blocker"] is None:
        result["blocker"] = "preferences_unavailable"
        result["next_safe_action"] = "inspect and correct the personal preference file"
    return result


def execute(args: argparse.Namespace, project: Path) -> dict:
    if args.command in (None, "config"):
        return _config(project, edit=getattr(args, "config_action", None) == "edit")
    if args.command == "doctor":
        return _doctor(project)
    if args.command == "status":
        return _status(project, args.run)
    if args.command == "update":
        raise PodError("update_route", "Update must run through the installer entrypoint")
    raise PodError("unknown_command", "Unsupported public command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "update":
        from .installer import main as installer_main
        return installer_main(["--update"])
    if args.command is None and sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import run as run_tui
        try:
            return run_tui(Path.cwd())
        except PodError as exc:
            print(f"pod: {exc.code}: {exc}", file=sys.stderr)
            return 1
    try:
        result = execute(args, Path.cwd())
    except PodError as exc:
        result = {"schema": "pod-cli/v4", "status": "blocked",
                  "error": {"code": exc.code, "message": str(exc)}}
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    elif args.command is None:
        from .tui_render import summary
        if result["status"] == "blocked":
            print(f"pod: {result['error']['code']}: {result['error']['message']}")
        else:
            print(summary(result))
    elif args.command in (None, "config"):
        if result["status"] == "invalid":
            print(f"Pod preferences need attention: {result['path']}")
            for error in result.get("errors", []):
                print(f"  {error['code']}: {error['message']}")
        elif result["status"] == "blocked":
            print(f"pod: {result['error']['code']}: {result['error']['message']}")
        else:
            print(f"Pod {version()}: {len(result['eligible'])} eligible models, "
                  f"{result['mode']} selection, maximum {result['max_active']} workers")
            print(f"Preferences: {result['path']}")
            if result["not_set"]:
                print("Not set (not eligible): " + ", ".join(result["not_set"]))
    elif result["status"] == "blocked":
        print(f"pod: {result['error']['code']}: {result['error']['message']}")
    elif args.command == "doctor":
        print(f"Pod {version()}: {result['installation']}")
        checks = result["installation_checks"]
        identity = result["bundle_identity"]
        print(f"Running bundle: {identity_label(identity['running'])}")
        if identity["receipt"] is not None:
            print(f"Receipt bundle: {identity_label(identity['receipt'])}")
        if identity["drift"]:
            print(f"Bundle drift: {RELOAD_ACTION}")
        if checks.get("receipt_status"):
            print(f"Installer: receipt {checks['receipt_status']}; bundle "
                  f"{'matches' if checks['digest_matches'] else 'differs from'} receipt; "
                  f"venv {'ready' if checks['venv']['ready'] else 'unavailable'}")
        if checks.get("next_action"):
            print(f"Next: {checks['next_action']}")
        if result["launcher"]["shadowed"]:
            print("Launcher: another pod command is earlier on PATH")
        for duplicate in checks.get("duplicates", []):
            print(f"Duplicate skill: {duplicate}")
        print(f"Preferences: {'valid' if not result['preferences']['errors'] else 'invalid'}")
        if result["preferences"]["not_set"]:
            print("Not set (not eligible): " + ", ".join(result["preferences"]["not_set"]))
        print(f"Orca: {result['orca']['status']}")
    elif args.command == "status":
        print(f"Pod status: {result['status']}; objective {result.get('objective') or 'unknown'}")
        identity = result["bundle_identity"]
        if identity["checkpoint"] is not None:
            print(f"Checkpoint bundle: {identity_label(identity['checkpoint'])}; "
                  f"running: {identity_label(identity['running'])}")
        for ref in result.get("native_references", []):
            print(f"  Task {ref['task']} | Dispatch {ref['dispatch'] or 'pending'} | "
                  f"worker {ref['worker'] or 'unknown'} | terminal {ref['terminal'] or 'none'} | {ref['state']}")
        print(f"Blocker: {result.get('blocker') or 'none'}; next: {result['next_safe_action']}")
    else:
        print(f"pod: {result['status']}")
    return 0 if result["status"] in ("valid", "observed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

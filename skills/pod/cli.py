"""Small public Pod launcher surface; private mechanics stay in internal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from .bundle import bundle_root, version
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
                   "eligible": snapshot["eligible"], "max_active": snapshot["max_active"],
                   "policy_revision": snapshot["policy_revision"], "errors": snapshot["errors"],
                   "catalog": [{"id": row["id"], "name": row["name"], "agent": row["agent"],
                                "efforts": [effort for effort in row["efforts"] if effort != "ultra"],
                                "guidance": row["guidance"]} for row in document["models"]]})
    if snapshot["errors"]:
        result["status"] = "invalid"
    return result


def _doctor(project: Path) -> dict:
    preferences = load_config(project)
    snapshot = contract()
    try:
        state = state_inventory(project)
    except PodError as exc:
        state = {"blocked": True, "reason": exc.code}
    receipt = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "pod" / "install.json"
    launcher = Path.home() / ".local" / "bin" / "pod"
    installed = "not installed by the one-shot installer" if not receipt.is_file() else "receipt_present"
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    shadowed = any((Path(entry) / "pod").exists() for entry in path_entries
                   if entry and Path(entry).resolve() != launcher.parent.resolve())
    return {"schema": "pod-cli/v4", "status": "observed", "version": version(),
            "bundle": str(bundle_root()), "installation": installed,
            "launcher": {"path": str(launcher), "exists": launcher.is_file(), "shadowed": shadowed},
            "placements": inspect_placements(project), "skills_cli": skills_cli_entry(),
            "preferences": {key: preferences[key] for key in ("path", "revision", "mode", "eligible",
                                                           "max_active", "errors", "policy_revision")},
            "catalog": {"models": list(by_id()), "ranks_of_six": ranks(),
                        "benchmark_age_days": age()},
            "orca": {"status": snapshot.get("status"), "capabilities": snapshot.get("capabilities", {}),
                     "runtime_state": snapshot.get("runtime_state"),
                     "reason": snapshot.get("reason")},
            "state": state, "native_probe": "not_run"}


def _status(project: Path, run: str | None) -> dict:
    preferences = load_config(project)
    result = {"schema": "pod-cli/v4", "status": "selection_required", "run": run,
              "preferences": {key: preferences[key] for key in ("path", "revision", "mode", "eligible",
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
    try:
        workers = worker_rows(run)
    except PodError as exc:
        result.update({"status": "unavailable", "blocker": exc.code})
        return result
    from .ledger import _read
    try:
        context_root = context_root_for_run(run)
        state = _read(context_root / "context.json") if context_root else None
    except PodError as exc:
        result.update({"status": "blocked", "blocker": exc.code,
                       "next_safe_action": "inspect the objective state"})
        return result
    checkpoint = state.get("checkpoint") if state else None
    admissions = list(state["admissions"].values()) if state else []
    result.update({"status": "observed", "objective": checkpoint.get("objective") if checkpoint else None,
                   "source": checkpoint.get("objective_source", "direct_objective") if checkpoint else None,
                   "selected_worktree": checkpoint.get("worktree") if checkpoint else None,
                   "constraints": state["constraints"] if state else [],
                   "active_constraints": active_constraints(state["constraints"], preferences) if state else [],
                   "route_decisions": [row["route_decision"] for row in admissions],
                   "route_mismatch": any(row["route_decision"].get("route_mismatch") for row in admissions),
                   "effective_unknown": any(row["route_decision"].get("effective_unknown") for row in admissions),
                   "installed_version_drift": bool(checkpoint and checkpoint.get("pod_version") != version()),
                   "native": {"worker_count": len(workers["workers"]), "scope": workers.get("scope"),
                              "complete": workers.get("complete")},
                   "pending_admissions": sum(row["state"] in ("reserved", "unresolved") for row in admissions),
                   "verification_gaps": checkpoint.get("verification_gaps", []) if checkpoint else [],
                   "remaining_gates": checkpoint.get("remaining_gates", []) if checkpoint else [],
                   "blocker": "route_mismatch" if any(row["route_decision"].get("route_mismatch") for row in admissions)
                              else "effective_unknown" if any(row["route_decision"].get("effective_unknown") for row in admissions)
                              else checkpoint.get("blocker") if checkpoint else None,
                   "next_safe_action": checkpoint.get("next_safe_action", "inspect native Run")
                                       if checkpoint else "inspect native Run"})
    if result["installed_version_drift"]:
        result["blocker"] = "installed_version_changed"
        result["next_safe_action"] = "reload Pod and write a fresh checkpoint"
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
        raise PodError("installer_unavailable", "The one-shot installer update path is not installed yet")
    raise PodError("unknown_command", "Unsupported public command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args, Path.cwd())
    except PodError as exc:
        result = {"schema": "pod-cli/v4", "status": "blocked",
                  "error": {"code": exc.code, "message": str(exc)}}
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    elif args.command is None and sys.stdout.isatty():
        print("Pod model TUI arrives with the terminal milestone; use `pod config --json` now.")
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
    elif result["status"] == "blocked":
        print(f"pod: {result['error']['code']}: {result['error']['message']}")
    elif args.command == "doctor":
        print(f"Pod {version()}: {result['installation']}")
        print(f"Preferences: {'valid' if not result['preferences']['errors'] else 'invalid'}")
        print(f"Orca: {result['orca']['status']}")
    elif args.command == "status":
        print(f"Pod status: {result['status']}; objective {result.get('objective') or 'unknown'}")
        print(f"Blocker: {result.get('blocker') or 'none'}; next: {result['next_safe_action']}")
    else:
        print(f"pod: {result['status']}")
    return 0 if result["status"] in ("valid", "observed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
from .term import clean


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
        installation = installer.doctor_snapshot()
        paths = installation["paths"]
        receipt_path = installation["receipt_path"]
        receipt = installation["receipt"]
        target = receipt.get("target") if isinstance(receipt, dict) and isinstance(receipt.get("target"), dict) else {}
        receipt_identity = {"version": target.get("version"), "bundle_digest": target.get("digest")} if receipt else None
        drift = not identity_matches(receipt_identity, running) if receipt_identity else None
        canonical_digest = installation["canonical_digest"]
        venv = installation["venv"]
        launcher = installation["launcher"]
        launcher_ownership = installation["launcher_ownership"]
        found = shutil.which("pod")
        shadowed = bool(found and Path(found).resolve(strict=False) != launcher.resolve(strict=False))
        installed = ("not installed by the one-shot installer" if receipt is None else
                     "installed" if receipt.get("status") == "installed" else "installing")
        installation_checks = {"receipt_path": str(receipt_path), "receipt_status": receipt.get("status") if receipt else None,
                               "receipt_version": target.get("version"),
                               "receipt_digest": target.get("digest"),
                               "canonical_digest": canonical_digest,
                               "digest_matches": bool(receipt and canonical_digest == target.get("digest")),
                               "venv": {"path": str(venv), "ready": installation["venv_ready"], "pin": installer.PIN},
                               "duplicates": installation["duplicates"],
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


from .status import project_for_state, status as _status, render as render_status


def execute(args: argparse.Namespace, project: Path) -> dict:
    if args.command in (None, "config"):
        return _config(project, edit=getattr(args, "config_action", None) == "edit")
    if args.command == "doctor":
        return _doctor(project)
    if args.command == "status":
        return _status(project, args.run, current_run_fn=current_run, worker_rows_fn=worker_rows)
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
        for row in result["state"].get("superseded", []):
            print(f"Superseded objective {row['objective'] or row['record']}: "
                  f"{', '.join(row['schemas'])} (blocked, not converted)")
    elif args.command == "status":
        render_status(result)
    else:
        print(f"pod: {result['status']}")
    return 0 if result["status"] in ("valid", "observed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

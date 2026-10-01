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
from .catalog import SCHEMA as CATALOG_SCHEMA, format_latency, format_usd, load as load_catalog
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
    status.add_argument("--objective")
    status.add_argument("--run")
    status.add_argument("--json", action="store_true")
    sub.add_parser("update", help="update the installed bundle")
    models = sub.add_parser("models", help="model routes and cached public observations")
    models.add_argument("--json", action="store_true")
    models_sub = models.add_subparsers(dest="models_command")
    refresh = models_sub.add_parser("refresh", help="fetch and promote public model observations")
    refresh.add_argument("--check", action="store_true", help="validate and compare without saving")
    refresh.add_argument("--json", action="store_true")
    models_status = models_sub.add_parser("status", help="offline source, cache and mapping diagnostics")
    models_status.add_argument("--json", action="store_true")
    return root


# Config, doctor and status describe preferences without reading or fetching observations.
NOT_READ = {"status": "not_read", "sources": {}}
PREFERENCE_KEYS = ("path", "schema", "status", "revision", "eligible", "preferred", "pinned",
                   "max_active", "refresh", "errors", "setup", "pin_diagnostic",
                   "preferred_diagnostic", "policy_revision")


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
        return {"status": "setup_required" if exc.code == "setup_required" else "invalid",
                "reason": exc.code, "path": str(path),
                "message": "Edited file was kept; correct it before delegation"}
    return {"status": "valid", "path": str(path)}


def _config(project: Path, *, edit: bool) -> dict:
    from .routes import project as project_routes
    if edit:
        result = _edit(personal_path(project))
    else:
        result = {"status": "valid"}
    snapshot = load_config(project)
    projection = project_routes(project, preferences=snapshot, observations=NOT_READ)
    result.update({key: snapshot[key] for key in PREFERENCE_KEYS if key not in ("schema", "status")})
    result.update({"schema": "pod-cli/v4", "preference_schema": snapshot["schema"],
                   "preference_status": snapshot["status"],
                   "routes": [{key: row[key] for key in ("key", "agent", "model", "effort", "state",
                                                         "preferred", "pinned")}
                              for row in projection["routes"]],
                   "not_set_meaning": "not set (not eligible)",
                   "models": [{"id": model["id"], "name": model["name"], "agent": model["agent"],
                               "provider": model["provider"], "efforts": list(model["efforts"]),
                               "guide": dict(model["guide"]), "guidance": model["guidance"],
                               "documented_context_tokens": model["documented_context_tokens"]}
                              for model in load_catalog()["models"]],
                   "summary": projection["summary"],
                   "observations": {"status": "not_read", "command": "pod models --json"}})
    if snapshot["errors"]:
        result["status"] = "setup_required" if snapshot["setup"] else "invalid"
    return result


def _doctor(project: Path) -> dict:
    from . import installer
    from .routes import project as project_routes
    running = running_identity()
    preferences = load_config(project)
    try:
        document = load_catalog()
        catalog_state = {"status": "valid", "schema": CATALOG_SCHEMA,
                         "models": [row["id"] for row in document["models"]],
                         "not_routable": [row["id"] for row in document["not_routable"]]}
    except PodError as exc:
        catalog_state = {"status": "invalid", "error": {"code": exc.code, "message": str(exc)}}
    observation_state = _observation_status(preferences)
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
            "preferences": {key: preferences[key] for key in PREFERENCE_KEYS},
            "routes": project_routes(project, preferences=preferences, observations=NOT_READ)["summary"],
            "catalog": catalog_state, "observations": observation_state,
            "orca": {"status": snapshot.get("status"), "capabilities": snapshot.get("capabilities", {}),
                     "runtime_state": snapshot.get("runtime_state"),
                     "reason": snapshot.get("reason")},
            "state": state, "native_probe": "not_run"}


from .status import project_for_state, status as _status, render as render_status


def _observations():
    """The observations module, imported only by the commands that read it."""
    try:
        from . import observations
    except ImportError as exc:
        raise PodError("observations_unavailable", "The observations module is unavailable in this bundle") from exc
    return observations


def _observation_status(preferences: dict) -> dict:
    """Offline cache and source diagnostics for doctor; never a refresh."""
    try:
        report = _observations().status(setting=preferences["refresh"])
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    return {"status": "observed", **{key: report.get(key) for key in (
        "cache", "origin", "generation", "created_at", "age_s", "stale", "auto_refresh")},
            "sources": {source: {key: row.get(key) for key in ("status", "rows", "mapped", "stale",
                                                                "retrieved_at")}
                        for source, row in (report.get("sources") or {}).items()},
            "diagnostics": list(report.get("diagnostics") or [])[:16]}


def _models(project: Path, action: str | None, *, check: bool = False) -> dict:
    observations = _observations()
    if action == "refresh":
        result = observations.refresh(check=check)
        return {"schema": "pod-cli/v4", "status": result["outcome"], "refresh": result}
    if action == "status":
        report = observations.status(setting=load_config(project)["refresh"])
        return {"schema": "pod-cli/v4", "status": "observed", "observations": report}
    from .routes import project as project_routes
    return {"schema": "pod-cli/v4", "status": "observed", "projection": project_routes(project)}


def execute(args: argparse.Namespace, project: Path) -> dict:
    if args.command == "config":
        return _config(project, edit=getattr(args, "config_action", None) == "edit")
    if args.command in (None, "models"):
        return _models(project, getattr(args, "models_command", None), check=getattr(args, "check", False))
    if args.command == "doctor":
        return _doctor(project)
    if args.command == "status":
        return _status(project, args.run, objective=args.objective,
                       current_run_fn=current_run, worker_rows_fn=worker_rows)
    if args.command == "update":
        raise PodError("update_route", "Update must run through the installer entrypoint")
    raise PodError("unknown_command", "Unsupported public command")


def _metric(row: dict, name: str, render) -> str:
    value = row["metrics"][name]["value"]
    return "—" if value is None else render(value)


def _profile(row: dict) -> str:
    """The AA profile qualifiers the shown metrics were measured under ("—" without an AA row)."""
    for name in ("intelligence", "usd_per_task", "first_response_s"):
        metric = row["metrics"][name]
        if metric.get("row"):
            return ", ".join(metric.get("qualifiers") or []) or "standard"
    return "—"


def _print_models(projection: dict) -> None:
    preferences, summary = projection["preferences"], projection["summary"]
    print(f"Pod {version()} routes: {summary['routes']} supported, {summary['enabled']} enabled, "
          f"{summary['disabled']} disabled, {summary['not_set']} not set (not eligible)")
    print(f"Preferences: {preferences['path']} ({preferences['status'].replace('_', ' ')})")
    if preferences["status"] != "valid":
        print(f"Pod preferences need {'route setup' if preferences['setup'] else 'attention'}; "
              "no route is eligible for new delegation")
        for error in preferences["errors"]:
            print(f"  {error['code']}: {error['message']}")
    print(f"Preferred route: {preferences['preferred'] or 'none'}; pinned route: {preferences['pinned'] or 'none'}")
    seen = projection["observations"]
    if seen["status"] == "observed":
        print(f"Observations: {seen['origin']} snapshot, generation {seen['generation']}, "
              f"created {seen['created_at']}{' (stale)' if seen.get('stale') else ''}")
        # Each source keeps its own retrieval time, so a kept older block is never hidden behind one date.
        for source, block in sorted((seen.get("sources") or {}).items()):
            print(f"  {source}: {block.get('status')}, retrieved {block.get('retrieved_at') or 'unknown'}"
                  f"{', published ' + block['published_at'] if block.get('published_at') else ''}"
                  f"{' (stale)' if block.get('stale') else ''}")
    else:
        print(f"Observations: {seen['status']}")
    print(f"{'ROUTE':34} {'STATE':9} {'MARK':5} {'INDEX':>5} {'USD/TASK':>9} {'FIRST':>8}  AA PROFILE")
    for row in projection["routes"]:
        mark = ("pin " if row["pinned"] else "") + ("pref" if row["preferred"] else "")
        print(f"{row['key']:34} {row['state'].replace('_', ' '):9} {mark.strip():5} "
              f"{_metric(row, 'intelligence', str):>5} {_metric(row, 'usd_per_task', format_usd):>9} "
              f"{_metric(row, 'first_response_s', format_latency):>8}  {_profile(row)}")
    if projection["unmapped"]:
        print(f"New or unsupported observations: {len(projection['unmapped'])} (not routable; pod models --json lists them)")
    print("AA benchmark cost is not the user's subscription charge, quota consumption or Pod invoice; "
          "first/total benchmark response is not worker task duration.")
    print("AA metrics are informational; Pod chooses each exact route by suitability. "
          "\"with fallback\" is AA's harness profile and never enables Pod fallback.")


def _print_refresh(result: dict) -> None:
    print(f"Model observations refresh: {result['outcome']}"
          + (" (check only; nothing saved)" if result["check"] else ""))
    for source, row in sorted(result["sources"].items()):
        print(f"  {source}: {row['status']}; {row.get('mapped', 0)}/{row.get('rows', 0)} rows mapped")
        for message in row.get("diagnostics", [])[:4]:
            print(f"    {clean(message)}")


def _print_observation_status(report: dict) -> None:
    print(f"Model observations: {report['origin']} snapshot"
          + (f", generation {report['generation']}" if report.get("generation") else "")
          + (" (stale)" if report.get("stale") else ""))
    print(f"Cache: {report.get('cache') or 'unavailable'}")
    for source, row in sorted(report["sources"].items()):
        print(f"  {source}: {row['status']}; {row.get('mapped', 0)}/{row.get('rows', 0)} rows mapped"
              + (" (stale)" if row.get("stale") else ""))
    auto = report.get("auto_refresh") or {}
    print(f"Automatic refresh: {auto.get('setting')}; due: {'yes' if auto.get('due') else 'no'}"
          + (f" ({clean(str(auto['reason']))})" if auto.get("reason") else ""))
    for message in report.get("diagnostics", [])[:8]:
        print(f"  {clean(message)}")


def _exit_code(args: argparse.Namespace, result: dict) -> int:
    if "projection" in result:
        # The route view succeeds as a read; its exit code still reports preference validity.
        return 0 if result["projection"]["preferences"]["status"] == "valid" else 1
    return 0 if result["status"] in ("valid", "observed", "promoted", "checked") else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "update":
        from .installer import main as installer_main
        return installer_main(["--update"])
    workspace = (args.command is None or args.command == "models" and args.models_command is None
                 and not args.json)
    if workspace and sys.stdin.isatty() and sys.stdout.isatty():
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
        print(json.dumps(result.get("projection", result) if args.command == "models"
                         and getattr(args, "models_command", None) is None and result["status"] != "blocked"
                         else result, sort_keys=True, ensure_ascii=False))
    elif result["status"] == "blocked":
        print(f"pod: {result['error']['code']}: {result['error']['message']}")
    elif args.command in (None, "models"):
        action = getattr(args, "models_command", None)
        if action == "refresh":
            _print_refresh(result["refresh"])
        elif action == "status":
            _print_observation_status(result["observations"])
        else:
            _print_models(result["projection"])
    elif args.command == "config":
        if result["status"] != "valid":
            print(f"Pod preferences need {'route setup' if result['status'] == 'setup_required' else 'attention'}: "
                  f"{result['path']}")
            for error in result.get("errors", []):
                print(f"  {error['code']}: {error['message']}")
            if result.get("setup"):
                print(f"Next: {result['setup']['action']}")
        else:
            print(f"Pod {version()}: {len(result['eligible'])} enabled routes, "
                  f"maximum {result['max_active']} workers, refresh {result['refresh']}")
            print(f"Preferences: {result['path']}")
            print(f"Preferred route: {result['preferred'] or 'none'}")
            print(f"Pinned route: {result['pinned'] or 'none'}")
            if result["summary"]["not_set"]:
                print(f"Not set (not eligible): {result['summary']['not_set']} routes")
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
        preferences = result["preferences"]
        print(f"Preferences: {preferences['status'].replace('_', ' ')} ({preferences['path']})")
        if preferences["setup"]:
            print(f"Next: {preferences['setup']['action']}")
        print(f"Routes: {result['routes']['enabled']} enabled of {result['routes']['routes']}; "
              f"preferred {preferences['preferred'] or 'none'}; pinned {preferences['pinned'] or 'none'}")
        seen = result["observations"]
        print(f"Model observations: {seen.get('origin') or seen['status']}"
              + (" (stale)" if seen.get("stale") else ""))
        print(f"Orca: {result['orca']['status']}")
        for row in result["state"].get("superseded", []):
            print(f"Superseded objective {row['objective'] or row['record']}: "
                  f"{', '.join(row['schemas'])} (blocked, not converted)")
    elif args.command == "status":
        render_status(result)
    else:
        print(f"pod: {result['status']}")
    return _exit_code(args, result)


if __name__ == "__main__":
    raise SystemExit(main())

"""The four public Pod command families."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import yaml

from .bundle import bundle_root, version
from .config import DEFAULT, effective, personal_path, route_identity
from .errors import PodError
from .ledger import context_root_for_run
from .orca import (account_metadata, account_metadata_raw, agent_login_mode, contract, executable,
                   hosts, read_command, route_establishment, worker_rows)
from .setup import inspect, setup, skills_cli_entry


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pod", description="In-session Orca coordination policy")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="enroll owned local skills, or user skills with --global")
    s.add_argument("--global", dest="global_scope", action="store_true")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("config", help="show or validate effective YAML policy")
    c.add_argument("--check", action="store_true")
    c.add_argument("--edit", action="store_true")
    c.add_argument("--scope", choices=("personal", "project"), default="personal")
    c.add_argument("--json", action="store_true")
    d = sub.add_parser("doctor", help="read-only installation and capability diagnostics")
    d.add_argument("--json", action="store_true")
    st = sub.add_parser("status", help="read-only native status and local decision context")
    st.add_argument("--run")
    st.add_argument("--json", action="store_true")
    return p


def _edit(path: Path, *, project_scope: bool) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise PodError("unsafe_config", "Configuration path is redirected")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        seed = {"schema": "pod/v1"} if project_scope else DEFAULT
        with path.open("x", encoding="utf-8") as stream:
            stream.write(yaml.safe_dump(seed, sort_keys=False))
    editor = os.environ.get("EDITOR", "vi")
    argv = shlex.split(editor)
    if not argv:
        raise PodError("editor_unavailable", "EDITOR is empty")
    try:
        subprocess.run([*argv, str(path)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PodError("editor_failed", "Editor did not complete") from exc


def _status(root: Path, run: str | None) -> dict:
    if run is None:
        try:
            listing = read_command(["orchestration", "run-list", "--json"])
        except PodError as exc:
            return {"status": "unavailable", "reason": exc.code, "selection_required": True}
        result = listing["result"]
        runs = result.get("runs", result.get("items", []))
        if not isinstance(runs, list) or len(runs) != 1 or result.get("nextCursor"):
            return {"status": "selection_required", "run_count": len(runs) if isinstance(runs, list) else "unknown"}
        run = runs[0].get("id") if isinstance(runs[0], dict) else None
        if not run:
            return {"status": "selection_required", "reason": "native Run identity unavailable"}
    try:
        workers = worker_rows(run)
    except PodError as exc:
        return {"status": "unavailable", "run": run, "reason": exc.code}
    counts: dict[str, int] = {}
    attention = 0
    for worker in workers["workers"]:
        state = str(worker.get("terminalState", "unknown"))
        counts[state] = counts.get(state, 0) + 1
        projection = worker.get("projection")
        attention_value = projection.get("attention") if isinstance(projection, dict) else None
        if isinstance(attention_value, dict) and attention_value.get("requiresAction"):
            attention += 1
    governor = "unknown"
    try:
        context_root = context_root_for_run(run)
        context = _read_context(context_root)
        if context_root is not None:
            from .governor import status_at
            governor = _governor_projection(status_at(context_root, project=root))
    except PodError as exc:
        context = {"error": exc.code}
    checkpoint_value = context.get("checkpoint") if isinstance(context, dict) else None
    return {"status": "observed", "run": run,
            "native": {"runtime_observed": True, "scope": workers["scope"],
                       "workers_by_state": counts, "attention_count": attention,
                       "complete": workers["complete"]},
            "verification_gaps": checkpoint_value.get("verification_gaps", []) if checkpoint_value else "unknown",
            "pending_effects": sum(e.get("state") in ("reserved", "uncertain") for e in context.get("effects", {}).values()) if checkpoint_value else "unknown",
            "route_decisions": checkpoint_value.get("route_decisions", "unknown") if checkpoint_value else "unknown",
            "quota_visibility": checkpoint_value.get("quota_visibility", "unknown") if checkpoint_value else "unknown",
            "next_safe_action": checkpoint_value.get("next_safe_action") if checkpoint_value else "inspect native Run",
            "governor": governor,
            "usage": "unknown", "cost": "unknown"}


def _read_context(context_root: Path | None) -> dict | None:
    from .ledger import _read

    return _read(context_root / "context.json") if context_root is not None else None


def _governor_projection(projection: dict) -> dict:
    """The compact governor facts a status reader acts on: unit, candidate, blocker, activity."""
    units = {}
    for name, unit in projection.get("units", {}).items():
        candidate = unit.get("candidate")
        units[name] = {"generation": unit["generation"],
                       "candidate": candidate["commit"][:12] if candidate else None,
                       "preflight": unit["preflight"],
                       "last_decision": (unit["last_decision"] or {}).get("decision"),
                       "blocker": unit.get("blocker"), "next_action": unit.get("next_action"),
                       "active_validation": len(unit["active_validation"]),
                       "unresolved": len(unit["unresolved"])}
    return {"mode": projection.get("mode"), "enforcement": projection.get("enforcement", {}).get("level"),
            "phase": projection["phase"], "units": units,
            "counters": {key: projection["counters"][key] for key in
                         ("decisions", "attachments", "evidence_reused", "cancellations",
                          "failures_interrupted", "observed_deferrals")}}


def _bundle_report() -> dict:
    return {"version": version(), "path": str(bundle_root()),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "pyyaml": getattr(yaml, "__version__", "unknown")}


def _prerequisites(snapshot: dict) -> dict:
    """Actionable prerequisite guidance. Nothing here installs or repairs anything."""
    report = {"python": "ok", "pyyaml": "ok"}
    if sys.version_info[:2] < (3, 13):
        report["python"] = ("Python 3.13 or newer is required; this interpreter is "
                            + ".".join(str(part) for part in sys.version_info[:3]))
    if snapshot.get("status") == "observed":
        report["orca"] = "ok"
    else:
        try:
            executable()
            report["orca"] = ("Orca is installed but did not answer a status read ("
                              + str(snapshot.get("reason", "unknown")) + "); start it with `orca open`")
        except PodError:
            report["orca"] = ("Orca is not on PATH. Install Orca and expose `orca`, or set "
                              "ORCA_CLI_COMMAND. Pod never installs it.")
    return report


def _route_report(root: Path, snapshot: dict) -> dict:
    """Which approved routes the installed runtime actually establishes, read-only."""
    try:
        policy = effective(root)
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    if snapshot.get("status") != "observed":
        return {"status": "unavailable", "reason": snapshot.get("reason", "orca_unavailable")}
    models = {alias: model for alias, model in policy["policy"]["models"].items()
              if model.get("approved") and model.get("agent") in ("codex", "claude")}
    if not models:
        return {"status": "none_approved"}
    try:
        accounts = account_metadata_raw()
        fleet = hosts()
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    delegation = bool(policy["policy"]["policy"].get("child_delegation"))
    logins: dict[str, dict] = {}
    report = {}
    for alias, model in models.items():
        agent = model["agent"]
        logins.setdefault(agent, agent_login_mode(agent))
        route = {"alias": alias, "agent": agent, "model": model.get("model"),
                 "account": model.get("account"), "bucket": None, "effort": None}
        established = route_establishment(route, model, snapshot=snapshot, accounts=accounts,
                                          login=logins[agent], fleet=fleet,
                                          child_delegation=delegation)
        report[alias] = {"tiers": {name: control["tier"] for name, control
                                   in established["controls"].items()},
                         "login_mode": established["login"]["mode"],
                         "billing": established["billing"],
                         "hard_stops": established["hard_stops"],
                         "disclosures": established["disclosures"]}
    return {"status": "observed", "routes": report}


def execute(args: argparse.Namespace, root: Path) -> dict:
    if args.command == "setup":
        return {"schema": "pod-cli/v1", "status": "ok", **setup(root, global_scope=args.global_scope)}
    if args.command == "config":
        if args.edit:
            target = personal_path(root) if args.scope == "personal" else root / ".pod" / "config.yaml"
            _edit(target, project_scope=args.scope == "project")
        value = effective(root)
        approval_routes = {alias: route_identity(model) for alias, model in value["policy"]["models"].items()
                           if all(model.get(key) for key in ("agent", "model", "account"))}
        return {"schema": "pod-cli/v1", "status": "valid", "effective": value,
                "approval_routes": approval_routes, "edited": args.edit,
                "scope": args.scope if args.edit else None}
    if args.command == "doctor":
        try:
            policy = effective(root)
            config = {"status": "valid", "revision": policy["revision"]}
        except PodError as exc:
            config = {"status": "invalid", "reason": exc.code}
        try:
            quota = account_metadata()["providers"]
        except PodError as exc:
            quota = {"status": "unavailable", "reason": exc.code}
        local_skills = inspect(root)
        global_skills = inspect(root, global_scope=True)
        overlap = {}
        for host in local_skills:
            local_status = local_skills[host]["status"]
            global_status = global_skills[host]["status"]
            local_version = local_skills[host].get("version")
            global_version = global_skills[host].get("version")
            if local_status == "current" and global_status == "current":
                overlap[host] = "duplicate_current_copies"
            elif local_status == "managed_by_skills_cli" and global_status == "managed_by_skills_cli":
                overlap[host] = "duplicate_current_copies"
            elif local_status not in ("missing", "current") and global_status == "managed_by_skills_cli":
                overlap[host] = "skills_cli_global_shadowed_by_local"
            elif (local_version and global_version and local_version != global_version):
                overlap[host] = "version_mismatch"
            elif local_status not in ("missing", "current") and global_status == "current":
                overlap[host] = "modified_local_may_shadow_global"
            elif local_status == "current" and global_status not in ("missing", "current"):
                overlap[host] = "modified_global_copy"
        snapshot = contract()
        return {"schema": "pod-cli/v1", "status": "observed", "config": config,
                "orca": snapshot, "project_skills": local_skills,
                "global_skills": global_skills, "integration_overlap": overlap,
                "quota": quota, "bundle": _bundle_report(),
                "prerequisites": _prerequisites(snapshot),
                "routes": _route_report(root, snapshot),
                "skills_cli": skills_cli_entry(), "native_probe": "not_run"}
    if args.command == "status":
        return {"schema": "pod-cli/v1", **_status(root, args.run)}
    raise PodError("unknown_command", "Unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args, Path.cwd())
    except PodError as exc:
        result = {"schema": "pod-cli/v1", "status": "blocked", "error": {"code": exc.code, "message": str(exc)}}
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print("pod: " + result["status"])
        if "error" in result:
            print(f"  {result['error']['code']}: {result['error']['message']}")
        elif args.command == "config":
            print(f"  Revision: {result['effective']['revision']}")
            for level, row in result["effective"]["policy"]["routing"].items():
                model = result["effective"]["policy"]["models"][row["model"]]
                print(f"  {level}: {model.get('agent', 'unknown')} / {model.get('model', 'unknown')} / {row['effort']} ({'approved' if model.get('approved') else 'pending'})")
                if row["model"] in result["approval_routes"] and not model.get("approved"):
                    print(f"    Exact route binding for review: {result['approval_routes'][row['model']]}")
        else:
            for key, value in result.items():
                if key not in ("schema", "status"):
                    print(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if result["status"] in ("ok", "valid", "observed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

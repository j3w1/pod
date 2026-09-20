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

from .config import DEFAULT, effective, personal_path, route_identity
from .errors import PodError
from .ledger import context_for_run
from .orca import account_metadata, contract, read_command, worker_rows
from .setup import inspect, setup


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
    editor = os.environ.get("EDITOR", "notepad" if os.name == "nt" else "vi")
    argv = shlex.split(editor, posix=os.name != "nt")
    if os.name == "nt":
        argv = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in argv]
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
    try:
        context = context_for_run(run)
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
            "usage": "unknown", "cost": "unknown"}


def execute(args: argparse.Namespace, root: Path) -> dict:
    if args.command == "setup":
        return {"schema": "pod-cli/v1", "status": "ok", **setup(root, global_scope=args.global_scope)}
    if args.command == "config":
        if args.edit:
            target = personal_path() if args.scope == "personal" else root / ".pod" / "config.yaml"
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
            if local_status == "current" and global_status == "current":
                overlap[host] = "duplicate_current_copies"
            elif local_status not in ("missing", "current") and global_status == "current":
                overlap[host] = "modified_local_may_shadow_global"
            elif local_status == "current" and global_status not in ("missing", "current"):
                overlap[host] = "modified_global_copy"
        return {"schema": "pod-cli/v1", "status": "observed", "config": config,
                "orca": contract(), "project_skills": local_skills,
                "global_skills": global_skills, "integration_overlap": overlap,
                "quota": quota, "billing_preflight": "unverified", "fanout_control": "unverified",
                "native_probe": "not_run"}
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

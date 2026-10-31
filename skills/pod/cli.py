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
from .orca import contract, current_run, message_route, terminal_activity, worker_rows
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
    policy = sub.add_parser("policy", help="choose who decides in plain words")
    policy.add_argument("preset", nargs="?", choices=("careful", "delegate", "hands_off", "custom", "skip"))
    policy.add_argument("--categories", help="complete Custom category map as JSON")
    policy.add_argument("--input", help="decision JSON file, or - for stdin")
    policy.add_argument("--json", action="store_true")
    tool = sub.add_parser("tool", help="check current permission before each sensitive tool use")
    tool_sub = tool.add_subparsers(dest="tool_action", required=True)
    check = tool_sub.add_parser("check", help="check one exact invocation; never executes a tool")
    check.add_argument("--tool", required=True)
    check.add_argument("--category", required=True)
    check.add_argument("--operation", required=True)
    check.add_argument("--spending", choices=("included_subscription", "new_commitment"))
    check.add_argument("--invocation")
    check.add_argument("--objective")
    check.add_argument("--json", action="store_true")
    review = tool_sub.add_parser("review", help="reconsider one remembered tool/category mode")
    review.add_argument("--tool", required=True)
    review.add_argument("--category", required=True)
    review.add_argument("--objective")
    review.add_argument("--json", action="store_true")
    answer = tool_sub.add_parser("answer", help="answer one pending memory or operation question")
    answer.add_argument("--invocation", required=True)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--input", help="decision JSON file, or - for stdin")
    answer.add_argument("--objective")
    answer.add_argument("--json", action="store_true")
    cancel = tool_sub.add_parser("cancel", help="cancel one unanswered permission question")
    cancel.add_argument("--invocation", required=True)
    cancel.add_argument("--objective")
    cancel.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor", help="read-only installation and capability diagnostics")
    doctor.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="read-only objective and native work status")
    status.add_argument("--objective")
    status.add_argument("--run")
    status.add_argument("--list", action="store_true", dest="list_objectives",
                        help="list the objectives recorded for a repository or worktree")
    scope = status.add_mutually_exclusive_group()
    scope.add_argument("--repository", metavar="PATH", help="with --list: this repository (default: here)")
    scope.add_argument("--worktree", metavar="PATH", help="with --list: objectives that touch this worktree")
    status.add_argument("--delivered", action="store_true",
                        help="with --list: prove each open objective's delivery (reads Orca and GitHub)")
    status.add_argument("--json", action="store_true")
    admission = sub.add_parser("admission", help="read current claims and exact candidate proof")
    for name in ("repository", "objective", "candidate", "worktree", "branch", "target", "target-commit", "expect"):
        admission.add_argument("--" + name)
    admission.add_argument("--effect", action="append", default=[])
    admission.add_argument("--known-repository", action="append", default=[],
                           help="CE-owned repository path used to prove earlier record attribution")
    admission.add_argument("--timeout", type=float, default=10.0)
    admission.add_argument("--json", action="store_true")
    sweep = sub.add_parser("sweep", help="close every provably delivered open objective by evidence")
    sweep.add_argument("--repository", metavar="PATH", action="append",
                       help="a repository to sweep (repeatable; default: here)")
    sweep.add_argument("--objective", metavar="NAME", action="append", help="only this objective (repeatable)")
    sweep.add_argument("--json", action="store_true")
    wake = sub.add_parser("wake", help="recheck mail, then arm one native wait for this Run's coordinator")
    wake.add_argument("--run", required=True, help="the Run this terminal coordinates")
    wake.add_argument("--ack", metavar="DELIVERY", help="acknowledge the batch you finished processing")
    wake.add_argument("--timeout-ms", type=int, help="wait limit; default one hour, at most six")
    wake.add_argument("--json", action="store_true")
    resume = sub.add_parser("continue", help="continue an objective whose coordinator a verified Orca window ended")
    resume.add_argument("--objective", metavar="NAME", required=True)
    resume.add_argument("--json", action="store_true")
    sub.add_parser("update", help="update the installed bundle")
    models = sub.add_parser("models", help="model routes and cached public observations")
    models.add_argument("--json", action="store_true")
    models.add_argument("--eligible-only", action="store_true",
                        help="limit models --json to eligible routes with guidance and observations")
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
    from .github import forget_repository_reads
    forget_repository_reads()  # The editor is an arbitrary program; read identity again after it.
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
    result.update({"schema": "pod-cli/v5", "preference_schema": snapshot["schema"],
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
    result.update(_decision_view(snapshot))
    return result


def _decision_view(snapshot: dict) -> dict:
    if snapshot["status"] == "invalid":
        from .readiness import decision_view
        reason = snapshot["errors"][0]["code"] if snapshot["errors"] else "invalid_config"
        return {**decision_view({"status": "invalid", "reason": reason}), "decision_generation": None}
    from .policy import projection
    view = projection(snapshot["decision_policy"], snapshot["tool_permissions"], snapshot["pending_decisions"])
    waiting = {"items": view["pending"], "total": len(view["pending"]), "truncated": False,
               "missing": [], "status": "known"}
    return {"decision_policy": {key: view[key] for key in ("preset", "categories", "setup")},
            "tool_permissions": view["permissions"], "waiting_decisions": waiting,
            "needs_you": any(row["waiting_on"] == "person" for row in view["pending"]),
            "setup_question": view["setup_question"], "decision_generation": snapshot["decision_generation"]}


def _decision_input(path: str | None, *, instruction: str) -> dict:
    from .util import bounded_json, bounded_stdin_json, exact
    if path is not None:
        value = bounded_stdin_json(limit=8192) if path == "-" else bounded_json(Path(path), limit=8192)
        return exact(value, {"provenance", "instruction", "channel", "relay"},
                     {"provenance", "instruction"}, name="decision input")
    if not sys.stdin.isatty():
        raise PodError("decision_input_required", "Answer in the session or terminal; pass its decision JSON through --input")
    if input("Save this as your decision? [yes/no] ").strip().lower() != "yes":
        raise PodError("decision_deferred", "Decision deferred")
    return {"provenance": "user_direct", "instruction": instruction, "channel": "session"}


def _policy_command(args, project):
    from .config import set_decision_policy
    snapshot = load_config(project)
    name = args.preset
    if name is None:
        if snapshot["status"] == "invalid" or args.json or not sys.stdin.isatty():
            return {"status": "ok", **_decision_view(snapshot)}
        print("Who should decide? Careful, Delegate, Hands-off, Custom, or skip setup.")
        name = input("Choice: ").strip().lower().replace("-", "_")
    categories = None
    if name == "custom" and args.categories is None and sys.stdin.isatty():
        from .policy import AUTOMATIC, CATEGORIES, PERSON_ONLY
        categories = {}
        deciders = {"me": "person", "trusted agents": "trusted_agents", "any agent": "any_agent"}
        for category in CATEGORIES:
            if category in PERSON_ONLY | AUTOMATIC:
                categories[category] = "person" if category in PERSON_ONLY else "any_agent"
            else:
                choice = input(f"Who decides {category.replace('_', ' ')}? me, trusted agents, any agent: ").strip().lower()
                if choice not in deciders:
                    raise PodError("invalid_decision_policy", "Choose me, trusted agents, or any agent")
                categories[category] = deciders[choice]
    elif args.categories is not None:
        try:
            categories = json.loads(args.categories)
        except ValueError:
            raise PodError("invalid_decision_policy", "Custom categories must be a complete JSON map") from None
    decision = None if name == "skip" else _decision_input(args.input, instruction="Use decision policy " + name)
    return set_decision_policy(project, name, categories=categories, decision=decision, displayed=snapshot)


def _tool_command(args, project):
    from .config import tool_answer, tool_check, tool_review, tool_cancel
    if args.objective is not None:
        from .ledger import tool_decision
        values = {key: getattr(args, key) for key in ("tool", "category", "operation", "spending", "invocation", "answer")
                  if hasattr(args, key)}
        if args.tool_action == "answer":
            values["decision"] = _decision_input(args.input, instruction="Answer this pending question: " + args.answer)
        return tool_decision(project, args.objective, action=args.tool_action, **values)
    if args.tool_action == "check":
        return tool_check(project, args.tool, args.category, args.operation, spending=args.spending, invocation=args.invocation)
    if args.tool_action == "review":
        return tool_review(project, args.tool, args.category)
    if args.tool_action == "cancel":
        return tool_cancel(project, args.invocation)
    return tool_answer(project, args.invocation, args.answer,
                       decision=_decision_input(args.input, instruction="Answer this pending question: " + args.answer))


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
        installation_checks["storage_cleanup"] = installation.get("storage_cleanup", [])
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
    return {"schema": "pod-cli/v5", "status": "observed", "version": version(),
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
            "message_route": message_route(snapshot),
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


def _models(project: Path, action: str | None, *, check: bool = False,
            eligible_only: bool = False) -> dict:
    observations = _observations()
    if action == "refresh":
        result = observations.refresh(check=check)
        return {"schema": "pod-cli/v5", "status": result["outcome"], "refresh": result}
    if action == "status":
        report = observations.status(setting=load_config(project)["refresh"])
        return {"schema": "pod-cli/v5", "status": "observed", "observations": report}
    from .routes import project as project_routes
    projection = project_routes(project)
    if eligible_only:
        eligible = set(projection["preferences"]["eligible"])
        projection = {**projection, "filter": "eligible", "unmapped": [],
                      "routes": [row for row in projection["routes"] if row["key"] in eligible]}
    return {"schema": "pod-cli/v5", "status": "observed", "projection": projection}


def execute(args: argparse.Namespace, project: Path) -> dict:
    if args.command == "admission":
        from .admission import query
        return query(project, repository=args.repository, objective=args.objective, candidate=args.candidate,
                     worktree=args.worktree, branch=args.branch, target=args.target,
                     target_commit=args.target_commit, effects=args.effect, expect=args.expect, timeout=args.timeout,
                     known_repositories=args.known_repository)
    if args.command == "policy":
        return _policy_command(args, project)
    if args.command == "tool":
        return _tool_command(args, project)
    if args.command == "config":
        return _config(project, edit=getattr(args, "config_action", None) == "edit")
    if args.command in (None, "models"):
        return _models(project, getattr(args, "models_command", None), check=getattr(args, "check", False),
                       eligible_only=getattr(args, "eligible_only", False))
    if args.command == "doctor":
        return _doctor(project)
    if args.command == "status" and getattr(args, "list_objectives", False):
        from .listing import objective_list
        return objective_list(project, repository=args.repository, worktree=args.worktree,
                              delivered=args.delivered)
    if args.command == "sweep":
        from .evidence_closure import sweep
        return sweep(project, repositories=args.repository, objectives=args.objective)
    if args.command == "wake":
        from .wake import DEFAULT_WAIT_MS, arm
        return arm(args.run, ack=args.ack, timeout_ms=args.timeout_ms or DEFAULT_WAIT_MS)
    if args.command == "continue":
        from .window import continue_command
        return continue_command(project, args.objective)
    if args.command == "status":
        return _status(project, args.run, objective=args.objective,
                       current_run_fn=current_run, worker_rows_fn=worker_rows,
                       terminal_activity_fn=terminal_activity)
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
            return ", ".join(metric.get("qualifiers") or []) or "(none)"
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
    if args.command == "admission" and result.get("schema") == "pod-admission-query/v1":
        return 0  # A factual answer, including scoped uncertainty, is not authorization.
    if args.command in ("policy", "tool"):
        return 1 if result["status"] == "blocked" else 0
    if "projection" in result:
        # The route view succeeds as a read; its exit code still reports preference validity.
        return 0 if result["projection"]["preferences"]["status"] == "valid" else 1
    return 0 if result["status"] in ("valid", "observed", "promoted", "checked", "listed", "absent", "swept",
                                     "continued", "already_continued", "events", "timeout") else 1


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if getattr(args, "eligible_only", False) and (not args.json or args.models_command is not None):
        command_parser.error("--eligible-only requires models --json without a subcommand")
    if args.command == "status" and args.list_objectives and (args.objective or args.run):
        command_parser.error("--list reads every objective; it takes --repository or --worktree, not --objective or --run")
    if args.command == "status" and not args.list_objectives and (args.repository or args.worktree):
        command_parser.error("--repository and --worktree select the scope of --list")
    if args.command == "status" and args.delivered and not args.list_objectives:
        command_parser.error("--delivered adds delivery proofs to --list")
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
        from .github import repository_reads
        with repository_reads():  # One command; the long-running workspace above reads Git afresh.
            result = execute(args, Path.cwd())
    except PodError as exc:
        result = {"schema": "pod-cli/v5", "status": "blocked",
                  "error": {"code": exc.code, "message": str(exc)}}
    if getattr(args, "json", False) or args.command == "wake":
        print(json.dumps(result.get("projection", result) if args.command == "models"
                         and getattr(args, "models_command", None) is None and result["status"] != "blocked"
                         else result, sort_keys=True, ensure_ascii=False))
    elif result["status"] == "blocked" and "error" in result:
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
        if result.get("waiting_decisions", {}).get("status") == "unavailable":
            print("Decision waits: unavailable (" + result["waiting_decisions"]["reason"] + ")")
    elif args.command in ("policy", "tool"):
        policy_view = result.get("decision_policy", result.get("policy")) if args.command == "policy" else None
        if policy_view is not None and policy_view.get("status") == "unavailable":
            print("Decision policy: unavailable (" + policy_view["reason"] + ")")
            print("Decision waits: unavailable (" + result["waiting_decisions"]["reason"] + ")")
        elif policy_view is not None:
            print("Decision policy: " + policy_view["preset"].replace("_", "-").capitalize())
            print("Setup: " + policy_view["setup"])
            deciders = {"person": "me", "trusted_agents": "trusted agents", "any_agent": "any agent"}
            for category, decider in policy_view["categories"].items():
                print("  " + category.replace("_", " ") + ": " + deciders[decider])
            if result.get("setup_question"):
                print(result["setup_question"]["text"])
        elif result.get("question"):
            print(result["question"])
            if result.get("choices"):
                print("Choices: " + ", ".join(choice.replace("_", " ") for choice in result["choices"]))
            if result.get("invocation"):
                print("Pending decision: " + result["invocation"])
            if result.get("next_action"):
                print("Next: " + result["next_action"])
        elif result.get("setup_question"):
            print(result["setup_question"]["text"])
        else:
            print("Decision " + result["status"] + ("; permission to execute" if result.get("execute") else ""))
        if result.get("notice"):
            print(result["notice"])
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
        for row in checks.get("storage_cleanup", []):
            print(f"Storage cleanup {row['record']}: {row['queued_bytes'] if row['queued_bytes'] is not None else 'unknown'} bytes queued")
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
        route = result["message_route"]
        print(f"Message route to a Run's coordinator: conditional send {route['conditional_send']}, "
              f"administrator sender {route['administrator_sender']} ({route['basis']}; {route['dependency']})")
        print(f"Administrator relay: {route['administrator_relay']} through {route['relay_address']} from "
              f"{route['relay_sender']} ({route['relay_basis']})")
        for row in result["state"].get("superseded", []):
            print(f"Superseded objective {row['objective'] or row['record']}: "
                  f"{', '.join(row['schemas'])} (blocked, not converted)")
    elif args.command == "admission":
        from .admission import render
        render(result)
    elif args.command == "status" and args.list_objectives:
        from .listing import render as render_list
        render_list(result)
    elif args.command == "sweep":
        from .evidence_closure import render_sweep
        render_sweep(result)
    elif args.command == "continue":
        from .window import render
        render(result)
    elif args.command == "status":
        render_status(result)
    else:
        print(f"pod: {result['status']}")
    return _exit_code(args, result)


if __name__ == "__main__":
    raise SystemExit(main())

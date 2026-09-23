"""The four public Pod command families."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any

import yaml

from .bundle import bundle_root, version
from .config import (DEFAULT, MODEL_CATALOG, effective, guided_personal_update,
                     personal_path, personal_revision, route_identity)
from .errors import PodError
from .ledger import context_root_for_run, state_inventory
from .orca import (account_metadata, account_metadata_raw, agent_login_mode, contract, executable,
                   current_run, hosts, route_establishment, selected_account_evidence,
                   selected_account_identity, worker_rows)
from .setup import inspect, setup, skills_cli_entry
from .util import digest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pod", description="In-session Orca coordination policy")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="enroll owned local skills, or user skills with --global")
    s.add_argument("--global", dest="global_scope", action="store_true")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("config", help="show or validate effective YAML policy")
    c.add_argument("config_action", nargs="?", choices=("approve", "revoke"))
    c.add_argument("alias", nargs="?")
    c.add_argument("--check", action="store_true")
    c.add_argument("--edit", action="store_true")
    c.add_argument("--scope", choices=("personal", "project"), default="personal")
    c.add_argument("--confirm", metavar="PROPOSAL")
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
            binding = current_run()
        except PodError as exc:
            return {"status": "unavailable", "reason": exc.code, "selection_required": True}
        current = binding.get("run")
        run = current.get("id") if isinstance(current, dict) else None
        if not isinstance(run, str) or not run:
            return {"status": "selection_required", "reason": "no current native Run; pass --run"}
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
            from .operations import OrcaPort
            from .ledger import binding_valid, logical_projection
            assignments = tuple(row for row in context.get("admissions", {}).values()
                                if isinstance(row, dict) and row.get("state") == "bound"
                                and binding_valid(row.get("native_binding")))
            native = OrcaPort(root).read_native(context.get("owner"), assignments=assignments)
            objective_names = {row.get("objective") for row in context.get("admissions", {}).values()
                               if isinstance(row, dict) and row.get("objective")}
            selected_objective = next(iter(objective_names)) if len(objective_names) == 1 else None
            projection = (logical_projection(root, native, objective=selected_objective)
                          if selected_objective is not None else {
                              "schema": "pod-logical-projection/v1", "runtime": native["runtime"],
                              "authoritative": False, "owner": None, "outstanding": [],
                              "outstanding_ids": [], "physical_capacity": "unavailable"})
            governor = _governor_projection(status_at(
                context_root, project=root, native_projection=projection))
    except PodError as exc:
        context = {"error": exc.code}
    checkpoint_value = context.get("checkpoint") if isinstance(context, dict) else None
    source = checkpoint_value.get("objective_source") if checkpoint_value else None
    worktree = checkpoint_value.get("worktree") if checkpoint_value else None
    return {"status": "observed", "run": run,
            "objective": checkpoint_value.get("objective") if checkpoint_value else None,
            "source": dict(source) if isinstance(source, dict) else "direct_objective",
            "selected_worktree": worktree or "unknown",
            "native": {"runtime_observed": True, "scope": workers["scope"],
                       "workers_by_state": counts, "attention_count": attention,
                       "complete": workers["complete"]},
            "verification_gaps": checkpoint_value.get("verification_gaps", []) if checkpoint_value else "unknown",
            "pending_admissions": sum(a.get("state") in ("reserved", "unresolved")
                                      for a in context.get("admissions", {}).values()) if checkpoint_value else "unknown",
            "route_decisions": checkpoint_value.get("route_decisions", "unknown") if checkpoint_value else "unknown",
            "quota_visibility": checkpoint_value.get("quota_visibility", "unknown") if checkpoint_value else "unknown",
            "blocker": checkpoint_value.get("blocker") if checkpoint_value else None,
            "remaining_gates": checkpoint_value.get("remaining_gates", []) if checkpoint_value else "unknown",
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
    if (snapshot.get("status") == "observed"
            and isinstance(snapshot.get("runtime"), str) and snapshot["runtime"]):
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
        contexts = [row["context"] for row in policy["policy"]["routing"].values()
                    if row["model"] == alias]
        requested_context = contexts[0] if len(set(contexts)) == 1 else "max"
        route = {"alias": alias, "agent": agent, "model": model.get("model"),
                 "account": model.get("account"), "bucket": None, "effort": None,
                 "context": requested_context, "effective_context": None}
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


def _config_proposal(root: Path, action: str, alias: str) -> dict:
    if alias not in MODEL_CATALOG:
        raise PodError("unknown_model_alias", "Alias is outside the active Pod catalog")
    path = personal_path(root)
    revision = personal_revision(path)
    catalog = MODEL_CATALOG[alias]
    base = {"schema": "pod-config-proposal/v1", "action": action, "alias": alias,
            "agent": catalog["agent"], "model": catalog["model"],
            "personal_revision": revision}
    if action == "revoke":
        proposal = {**base, "account": None, "account_display": "not required",
                    "auth": "not required", "billing": "not required",
                    "efforts": {"status": "not_applicable", "requested": []},
                    "context": {"status": "not_applicable"}}
    else:
        snapshot = contract()
        if snapshot.get("status") != "observed" or not isinstance(snapshot.get("runtime"), str):
            raise PodError("native_authority_unverified",
                           "Orca must be connected before approving a route")
        accounts = account_metadata_raw()
        if accounts.get("runtime") != snapshot["runtime"]:
            raise PodError("orca_runtime_changed", "Account evidence belongs to another runtime")
        evidence = selected_account_evidence(
            accounts.get("providers", {}).get(catalog["agent"], {}),
            agent_login_mode(catalog["agent"]))
        account = evidence.get("identity_digest")
        if not isinstance(account, str) or not re.fullmatch(r"[0-9a-f]{64}", account):
            raise PodError("account_binding_unverified",
                           "The selected native account has no redacted identity")
        billing = ("included" if evidence.get("billing") == "subscription"
                   else "paid" if evidence.get("billing") == "api" else "unknown")
        configured_efforts = sorted({row["effort"] for row in DEFAULT["routing"].values()
                                     if row["model"] == alias})
        configured_contexts = sorted({row["context"] for row in DEFAULT["routing"].values()
                                      if row["model"] == alias})
        proposal = {**base, "runtime": snapshot["runtime"], "account": account,
                    "account_display": "…" + account[-8:], "auth": evidence.get("auth", "unknown"),
                    "billing": billing,
                    "efforts": {"status": "requestable_not_model_verified",
                                "requested": configured_efforts},
                    "context": {"status": "unavailable",
                                "profiles": configured_contexts,
                                "reason": "installed Orca exposes no per-worker context control",
                                "provider_ceiling": catalog["provider_ceiling"],
                                "provider_ceiling_is_live_proof": False}}
    proposal["proposal"] = digest(proposal)
    return proposal


def _guided_config(root: Path, *, action: str, alias: str | None,
                   confirmation: str | None) -> dict:
    if alias is None:
        raise PodError("model_alias_required", f"config {action} requires a catalog alias")
    # A malformed/shadowing project layer must be repaired before personal authority changes.
    effective(root)
    proposal = _config_proposal(root, action, alias)
    if confirmation is None:
        public = {key: value for key, value in proposal.items() if key != "account"}
        return {"schema": "pod-cli/v3", "status": "confirmation_required",
                "proposal": public, "written": False}
    if confirmation != proposal["proposal"]:
        raise PodError("approval_evidence_changed",
                       "Configuration, route, runtime, or account evidence changed after review")
    approval = None
    if action == "approve":
        approval = {"account": proposal["account"], "billing": proposal["billing"],
                    "approval_ref": "guided:" + proposal["proposal"]}
    guided_personal_update(personal_path(root),
                           expected_revision=proposal["personal_revision"],
                           alias=alias, approval=approval)
    # Read the resulting effective policy back through the normal validator.
    policy = effective(root)
    return {"schema": "pod-cli/v3", "status": "approved" if approval else "revoked",
            "alias": alias, "model": proposal["model"], "account": proposal["account_display"],
            "billing": proposal["billing"], "revision": policy["revision"], "written": True}


def _readiness(*, config: dict, snapshot: dict, routes: dict, state: dict,
               policy: dict | None) -> dict:
    approved = (sum(bool(model.get("approved")) for model in policy["policy"]["models"].values())
                if policy is not None else 0)
    route_rows = routes.get("routes", {}) if isinstance(routes, dict) else {}
    usable = sum(not row.get("hard_stops") for row in route_rows.values())
    limitations = sorted({stop for row in route_rows.values()
                          for stop in row.get("hard_stops", [])})
    connected = (snapshot.get("status") == "observed"
                 and isinstance(snapshot.get("runtime"), str) and bool(snapshot["runtime"]))
    if not connected:
        limitations.append(snapshot.get("reason", "orca_unavailable"))
    return {"direct_work": "ready", "configuration": config["status"],
            "orca": "connected" if connected else "unavailable",
            "approved_worker_routes": approved, "usable_worker_routes": usable,
            "limitations": sorted(set(limitations)),
            "state": {**state, "scope": "affected_objectives_only",
                      "blocks_unrelated_objectives": False}}


def execute(args: argparse.Namespace, root: Path) -> dict:
    if args.command == "setup":
        return {"schema": "pod-cli/v3", "status": "ok", **setup(root, global_scope=args.global_scope)}
    if args.command == "config":
        if args.config_action:
            if args.edit or args.check or args.scope != "personal":
                raise PodError("invalid_config_action",
                               "Guided approval/revocation cannot be combined with edit/check/scope")
            return _guided_config(root, action=args.config_action, alias=args.alias,
                                  confirmation=args.confirm)
        if args.alias is not None or args.confirm is not None:
            raise PodError("invalid_config_action", "A confirmation applies only to approve/revoke")
        if args.edit:
            target = personal_path(root) if args.scope == "personal" else root / ".pod" / "config.yaml"
            _edit(target, project_scope=args.scope == "project")
        value = effective(root)
        approval_routes = {alias: route_identity(model) for alias, model in value["policy"]["models"].items()
                           if all(model.get(key) for key in ("agent", "model", "account"))}
        return {"schema": "pod-cli/v3", "status": "valid", "effective": value,
                "approval_routes": approval_routes, "edited": args.edit,
                "scope": args.scope if args.edit else None}
    if args.command == "doctor":
        try:
            policy = effective(root)
            config = {"status": "valid", "revision": policy["revision"]}
        except PodError as exc:
            config = {"status": "invalid", "reason": exc.code}
        try:
            raw_accounts = account_metadata_raw()
            quota = account_metadata(raw_accounts)["providers"]
            account_identities = {}
            for agent in ("codex", "claude"):
                account_identities[agent] = selected_account_identity(
                    raw_accounts["providers"].get(agent, {}), agent_login_mode(agent))
        except PodError as exc:
            quota = {"status": "unavailable", "reason": exc.code}
            account_identities = {"status": "unavailable", "reason": exc.code}
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
        try:
            state_report = state_inventory(root)
        except PodError as exc:
            state_report = {"current": 0, "unsupported": 0, "unreadable": 1,
                            "blocked": True, "reason": exc.code}
        routes = _route_report(root, snapshot)
        return {"schema": "pod-cli/v3", "status": "observed", "config": config,
                "orca": snapshot, "project_skills": local_skills,
                "global_skills": global_skills, "integration_overlap": overlap,
                "quota": quota, "account_identities": account_identities,
                "bundle": _bundle_report(),
                "prerequisites": _prerequisites(snapshot),
                "routes": routes,
                "skills_cli": skills_cli_entry(), "native_probe": "not_run",
                "state": state_report,
                "readiness": _readiness(config=config, snapshot=snapshot, routes=routes,
                                        state=state_report,
                                        policy=policy if config["status"] == "valid" else None)}
    if args.command == "status":
        result = _status(root, args.run)
        try:
            result["state"] = state_inventory(root)
        except PodError as exc:
            result["state"] = {"current": 0, "unsupported": 0, "unreadable": 1,
                               "blocked": True, "reason": exc.code}
        return {"schema": "pod-cli/v3", **result}
    raise PodError("unknown_command", "Unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args, Path.cwd())
    except PodError as exc:
        result = {"schema": "pod-cli/v3", "status": "blocked", "error": {"code": exc.code, "message": str(exc)}}
    if (not getattr(args, "json", False) and args.command == "config"
            and result.get("status") == "confirmation_required"):
        proposal = result["proposal"]
        print(f"Pod proposes to {proposal['action']} {proposal['alias']}:")
        print(f"  Agent/model: {proposal['agent']} / {proposal['model']}")
        print(f"  Account: {proposal['account_display']}")
        print(f"  Authentication/billing: {proposal['auth']} / {proposal['billing']}")
        requested_efforts = proposal["efforts"].get("requested", [])
        if proposal["action"] == "approve":
            configured = ", ".join(requested_efforts) if requested_efforts else "no default"
            print(f"  Configured effort: {configured}; model-specific support is unverified")
            contexts = proposal["context"].get("profiles", [])
            configured_context = ", ".join(contexts) if contexts else "no default"
            print(f"  Configured context: {configured_context}; native per-worker control is unavailable")
        else:
            print("  Effort/context: not applicable to revocation")
        if sys.stdin.isatty():
            answer = input(f"Explicitly confirm {proposal['action']}? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                args.confirm = proposal["proposal"]
                try:
                    result = execute(args, Path.cwd())
                except PodError as exc:
                    result = {"schema": "pod-cli/v3", "status": "blocked",
                              "error": {"code": exc.code, "message": str(exc)}}
            else:
                result = {"schema": "pod-cli/v3", "status": "cancelled", "written": False}
        else:
            print("  No change written: confirmation is required in an interactive terminal or host conversation.")
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        if not (args.command == "config" and result.get("status") == "confirmation_required"):
            print("pod: " + result["status"])
        if "error" in result:
            print(f"  {result['error']['code']}: {result['error']['message']}")
        elif args.command == "doctor":
            ready = result["readiness"]
            print(f"Pod {result['bundle']['version']} ready for direct work")
            print(f"  ✓ skill installed at {result['bundle']['path']}")
            print(f"  {'✓' if ready['orca'] == 'connected' else '!'} Orca {ready['orca']}")
            print(f"  {'✓' if ready['configuration'] == 'valid' else '!'} configuration {ready['configuration']}")
            print(f"  ! worker routes: {ready['approved_worker_routes']} approved, {ready['usable_worker_routes']} usable")
            for limitation in ready["limitations"]:
                message = {
                    "context_control_unavailable":
                        "worker context unavailable: installed Orca has no per-worker context control",
                    "native_authority_unverified":
                        "Orca connection is incomplete; start or reconnect Orca",
                    "account_binding_unverified":
                        "worker account identity is unavailable or changed; rerun this helper with `doctor --json`",
                    "billing_mode_unverified":
                        "worker billing/auth evidence is incomplete; rerun this helper with `doctor --json`",
                    "orca_read_failed":
                        "Orca did not answer; start or reconnect Orca, then rerun doctor",
                    "orca_unavailable":
                        "Orca is unavailable; start Orca, then rerun doctor",
                }.get(limitation, limitation)
                print(f"  ! {message}")
            state_report = ready["state"]
            if state_report["unsupported"] or state_report["unreadable"]:
                affected = state_report["unsupported"] + state_report["unreadable"]
                print(f"  ! unsupported or unreadable state affects {affected} objective(s) only")
        elif args.command == "config" and result.get("status") == "valid":
            for level, row in result["effective"]["policy"]["routing"].items():
                model = result["effective"]["policy"]["models"][row["model"]]
                print(f"  {level}: {model.get('agent', 'unknown')} / {model.get('model', 'unknown')} / {row['effort']} / {row['context']} ({'approved' if model.get('approved') else 'pending'})")
        elif args.command == "config" and result.get("status") == "confirmation_required":
            pass
        elif args.command == "config" and result.get("status") in ("approved", "revoked"):
            print(f"  {result['alias']}: {result['model']}")
            if result["status"] == "approved":
                print(f"  Account/billing: {result['account']} / {result['billing']}")
        elif args.command == "status" and result.get("status") == "observed":
            print(f"  Objective: {result.get('objective') or 'not recorded'}")
            source = result.get("source")
            print(f"  Source: {source.get('locator') if isinstance(source, dict) else source}")
            selected = result.get("selected_worktree")
            if isinstance(selected, dict):
                print(f"  Worktree: {selected.get('branch') or 'detached'} at {selected.get('path')}")
            else:
                print(f"  Worktree: {selected}")
            print(f"  Native work: {json.dumps(result['native'], ensure_ascii=False)}")
            print(f"  Blocker: {result.get('blocker') or 'none'}")
            print(f"  Next: {result.get('next_safe_action')}")
            print(f"  Remaining gates: {json.dumps(result.get('remaining_gates'), ensure_ascii=False)}")
        else:
            for key, value in result.items():
                if key not in ("schema", "status"):
                    print(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if result["status"] in ("ok", "valid", "observed", "confirmation_required",
                                     "approved", "revoked", "cancelled") else 1


if __name__ == "__main__":
    raise SystemExit(main())

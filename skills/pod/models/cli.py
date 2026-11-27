"""`pod models`: the TUI, the plain table, the explicit refresh and offline diagnostics."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import shutil
import sys

from .. import routefile
from ..errors import PodError
from . import adapters, edit, registry, snapshot, table, view

SCHEMA = "pod-models/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pod models", description="Model routes and public observations. "
                                     "Opening this never uses the network; `refresh` does, once.")
    parser.add_argument("action", nargs="?", choices=("refresh", "status"),
                        help="refresh: fetch the public leaderboard and adapter catalog once; status: offline diagnostics")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--plain", action="store_true", help="print the table even on a terminal")
    parser.add_argument("--all", action="store_true", help="include all supported providers in JSON output")
    parser.add_argument("--enable", action="append", default=[], metavar="ROUTE", help="enable agent/model/effort")
    parser.add_argument("--disable", action="append", default=[], metavar="ROUTE", help="disable agent/model/effort")
    parser.add_argument("--preferred", metavar="ROUTE|none", help="set or clear the Preferred route")
    parser.add_argument("--pin", metavar="ROUTE|none", help="set or clear the Pin")
    parser.add_argument("--pause", action="append", default=[], metavar="POOL|AGENT",
                        help="pause a quota pool (e.g. agy-gemini) or agent host-wide in paused_agents (repeatable)")
    parser.add_argument("--drain", action="append", default=[], metavar="POOL|AGENT",
                        help="soft-drain a quota pool or agent in soft_drained_agents to block new dispatches (repeatable)")
    parser.add_argument("--unpause", action="append", default=[], metavar="POOL|AGENT",
                        help="clear a quota pool or agent from paused_agents and soft_drained_agents (repeatable)")
    parser.add_argument("--reason", metavar="TEXT",
                        help="optional reason attached to --pause or --drain")
    parser.add_argument("--ttl", metavar="DURATION",
                        help="optional TTL attached to --pause or --drain (seconds or s/m/h/d such as 30m, 2h)")
    parser.add_argument("--profile", action="append", default=[], metavar="NAME=SPEC",
                        help="set or update a profile: NAME='<role>[+mcp] <route> ...' (repeatable)")
    parser.add_argument("--delete-profile", action="append", default=[], metavar="NAME",
                        help="remove a profile from config.yaml (repeatable)")
    parser.add_argument("--default-profile", action="append", default=[], metavar="ROLE=NAME|none",
                        help="set or clear default_profiles for a role: implement|review|investigate (repeatable)")
    parser.add_argument("--review-agents", metavar="A,B|none",
                        help="set review_agents priority list (comma-separated, or none)")
    return parser


def _emit(value: dict) -> None:
    print(json.dumps({"schema": SCHEMA, **value}, indent=2, sort_keys=True))


def _refresh(args) -> int:
    attempt = snapshot.refresh()
    agy = adapters.refresh_antigravity()
    if args.json:
        _emit({"refresh": attempt, "adapters": {"antigravity": agy}})
    elif attempt["ok"]:
        print(f"Refreshed: {attempt['rows']} public rows, {attempt['methodology'] or 'methodology not stated'}.")
        for line in attempt["diagnostics"]:
            print("  " + line)
        if agy["ok"]:
            extra = f" ({len(agy['ignored_unregistered'])} unregistered ignored)" if agy.get("ignored_unregistered") else ""
            print(f"Antigravity adapter: {agy['models']} models cached{extra}.")
        elif agy.get("installed"):
            print(f"Antigravity adapter refresh failed: {agy['failure']}.")
        print("No route was enabled, disabled or moved by this.")
    else:
        kept = snapshot.load()
        print(f"Refresh failed: {attempt['failure']}.", file=sys.stderr)
        print("The earlier snapshot was kept." if kept else "There is no earlier snapshot.", file=sys.stderr)
        if agy["ok"]:
            print(f"Antigravity adapter: {agy['models']} models cached.")
    return 0 if attempt["ok"] else 1


def _status(args) -> int:
    model = view.build()
    shot, attempt, preferences = model.snapshot, model.attempt, model.preferences
    catalogs = adapters.catalogs()
    counts: dict = {}
    for row in model.rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    report = {
        "snapshot": None if shot is None else {
            "fetched_at": shot.get("fetched_at"), "age": snapshot.age(shot.get("fetched_at")),
            "methodology": shot.get("methodology"), "rows": len(shot["rows"]), "source": shot.get("source"),
            "unknown_columns": shot.get("unknown_columns", []), "absent_metrics": shot.get("absent_metrics", []),
            "diagnostics": shot.get("diagnostics", [])},
        "last_attempt": attempt,
        "registry": {"models": len(registry.load()), "routes": sum(1 for row in model.rows if row.routable)},
        "adapters": {agent: ("no catalog" if catalog is None else f"{len(catalog)} models listed")
                     for agent, catalog in catalogs.items()},
        "preferences": {"path": str(preferences.path), "status": preferences.status, "problem": preferences.problem,
                        "enabled": len(preferences.enabled()), "preferred": preferences.preferred,
                        "pinned": preferences.pinned,
                        "paused_agents": list(preferences.paused_agents),
                        "paused_meta": {k: routefile.hold_as_dict(v) for k, v in preferences.paused_meta.items()},
                        "soft_drained_agents": list(preferences.soft_drained_agents),
                        "soft_drained_meta": {k: routefile.hold_as_dict(v) for k, v in preferences.soft_drained_meta.items()},
                        "review_agents": list(preferences.review_agents),
                        "default_profiles": routefile.effective_default_profiles(preferences),
                        "profiles": len(preferences.profiles)},
        "rows_by_state": counts,
        "cache": str(snapshot.home())}
    if args.json:
        _emit({"status": report})
        return 0
    for line in model.header():
        print(line)
    if shot is not None:
        print(f"Snapshot rows: {report['snapshot']['rows']}; source {report['snapshot']['source']}")
        for line in report["snapshot"]["diagnostics"]:
            print("  " + line)
    print(f"Registry: {report['registry']['models']} models, {report['registry']['routes']} routes")
    for agent, state in report["adapters"].items():
        print(f"Installed {agent}: {state}")
    paused = ", ".join(routefile.hold_summary(v) for v in preferences.paused_meta.values()) or "none"
    drained = ", ".join(routefile.hold_summary(v) for v in preferences.soft_drained_meta.values()) or "none"
    print(f"Preferences: {preferences.status} at {preferences.path}; {len(preferences.enabled())} enabled; "
          f"Preferred {preferences.preferred or 'none'}; Pin {preferences.pinned or 'none'}; "
          f"Paused {paused}; Drained {drained}; Profiles {len(preferences.profiles)}")
    print("Rows by state: " + ", ".join(f"{state} {count}" for state, count in sorted(counts.items())))
    return 0


def _build_hold(agent: str, *, reason: str | None, ttl_seconds: int | None, until: str | None) -> routefile.AgentHold:
    cleaned_reason = " ".join(reason.split()) if reason and reason.strip() else None
    return routefile.AgentHold(agent=agent, reason=cleaned_reason, until=until, ttl=ttl_seconds)


def _edit(args) -> None:
    changes: dict = {"routes": {**dict.fromkeys(args.enable, "enabled"), **dict.fromkeys(args.disable, "disabled")}}
    if set(args.enable) & set(args.disable):
        raise PodError("route_invalid", "A route is named by both --enable and --disable.", fix="name it once")
    for flag, name in (("preferred", "preferred"), ("pin", "pinned")):
        value = getattr(args, flag)
        if value is not None:
            changes[name] = None if value.lower() in ("none", "null") else value
    if (args.reason is not None or args.ttl is not None) and not (args.pause or args.drain):
        raise PodError("agent_invalid", "--reason and --ttl require --pause or --drain.",
                       fix="pass --pause AGENT or --drain AGENT alongside --reason or --ttl")
    if args.pause or args.drain or args.unpause:
        pause_set, drain_set, unpause_set = set(args.pause), set(args.drain), set(args.unpause)
        if pause_set & unpause_set or drain_set & unpause_set or pause_set & drain_set:
            raise PodError("agent_invalid", "An agent cannot be combined across --pause, --drain and --unpause.",
                           fix="name each agent under one action")
        ttl_seconds: int | None = None
        until: str | None = None
        if args.ttl is not None:
            try:
                ttl_seconds = routefile.parse_ttl(args.ttl)
            except ValueError as exc:
                raise PodError("agent_invalid", str(exc),
                               fix="pass a positive TTL such as 30m, 2h or 1800") from exc
            until = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        current = routefile.load()
        paused = dict(current.paused_meta) if current.status == "ok" else {}
        drained = dict(current.soft_drained_meta) if current.status == "ok" else {}
        for agent in args.pause:
            paused[agent] = _build_hold(agent, reason=args.reason, ttl_seconds=ttl_seconds, until=until)
            drained.pop(agent, None)
        for agent in args.drain:
            drained[agent] = _build_hold(agent, reason=args.reason, ttl_seconds=ttl_seconds, until=until)
            paused.pop(agent, None)
        for agent in args.unpause:
            paused.pop(agent, None)
            drained.pop(agent, None)
        changes["paused_agents"] = paused
        changes["soft_drained_agents"] = drained
    if args.profile or args.delete_profile:
        parsed_profiles: dict[str, str | None] = {}
        for item in args.profile:
            if "=" not in item:
                raise PodError("profile_invalid", f"{item!r} must use NAME=SPEC format.",
                               fix="pass --profile 'name=role[+mcp] agent/model/effort ...'")
            name, spec = item.split("=", 1)
            name, spec = name.strip(), spec.strip()
            if not name or not spec:
                raise PodError("profile_invalid", f"{item!r} must name both a profile and its spec.",
                               fix="pass --profile 'name=role[+mcp] agent/model/effort ...'")
            parsed_profiles[name] = spec
        overlap = set(parsed_profiles) & set(args.delete_profile)
        if overlap:
            raise PodError("profile_invalid", "A profile is named by both --profile and --delete-profile.",
                           fix="name it once")
        for name in args.delete_profile:
            parsed_profiles[name] = None
        changes["profiles"] = parsed_profiles
    if args.default_profile:
        parsed_defaults: dict[str, str | None] = {}
        for item in args.default_profile:
            if "=" not in item:
                raise PodError("profile_invalid", f"{item!r} must use ROLE=NAME|none format.",
                               fix="pass --default-profile implement=build-fast")
            role_key, pname = (part.strip() for part in item.split("=", 1))
            if not role_key or not pname:
                raise PodError("profile_invalid", f"{item!r} must name both a role and a profile or none.",
                               fix="pass --default-profile implement=build-fast")
            parsed_defaults[role_key] = None if pname.lower() in ("none", "null") else pname
        changes["default_profiles"] = parsed_defaults
    if args.review_agents is not None:
        raw = args.review_agents.strip()
        changes["review_agents"] = ([] if raw.lower() in ("", "none", "null")
                                    else [item.strip() for item in raw.split(",") if item.strip()])
    result = edit.apply(**changes)
    if not args.json:
        paused = ", ".join(routefile.hold_summary(v) for v in result.paused_meta.values()) or "none"
        drained = ", ".join(routefile.hold_summary(v) for v in result.soft_drained_meta.values()) or "none"
        print(f"Saved {result.path}: {len(result.enabled())} enabled; Preferred {result.preferred or 'none'}; "
              f"Pin {result.pinned or 'none'}; Paused {paused}; Drained {drained}; Profiles {len(result.profiles)}")


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "refresh":
            return _refresh(args)
        if args.action == "status":
            return _status(args)
        has_edit = bool(args.enable or args.disable or args.preferred is not None or args.pin is not None
                        or args.pause or args.drain or args.unpause
                        or args.reason is not None or args.ttl is not None
                        or args.profile or args.delete_profile or args.default_profile or args.review_agents is not None)
        if has_edit:
            _edit(args)
            if not args.json:
                return 0
        if not (args.json or args.plain) and sys.stdin.isatty() and sys.stdout.isatty():
            from . import tui
            return tui.run()
        model = view.build()
        rows = view.select(model, provider="all" if args.all else "routed")
        if args.json:
            routing = routefile.routing_policy(model.preferences)
            _emit({"header": model.header(), "rows": [view.as_json(row) for row in rows],
                   "routing": routing,
                   "default_profiles": routing["default_profiles"],
                   "review_agents": routing["review_agents"],
                   "paused_agents": list(model.preferences.paused_agents),
                   "paused_meta": {k: routefile.hold_as_dict(v) for k, v in model.preferences.paused_meta.items()},
                   "soft_drained_agents": list(model.preferences.soft_drained_agents),
                   "soft_drained_meta": {k: routefile.hold_as_dict(v) for k, v in model.preferences.soft_drained_meta.items()},
                   "profiles": {name: routefile.format_profile(prof)
                                for name, prof in model.preferences.profiles.items()}})
        else:
            sys.stdout.write(table.plain(model, view.visible(rows), shutil.get_terminal_size((120, 24)).columns))
        return 0
    except PodError as exc:
        if args.json:
            _emit({"error": exc.as_dict()})
        else:
            print(f"pod models: {exc}" + (f"\n  To proceed: {exc.fix}" if exc.fix else ""), file=sys.stderr)
        return 2

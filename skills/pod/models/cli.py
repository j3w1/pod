"""`pod models`: the workspace, the plain table, the explicit refresh and offline diagnostics."""

import argparse
import json
import shutil
import sys

from ..errors import PodError
from . import adapters, edit, registry, snapshot, table, view

SCHEMA = "pod-models/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pod models", description="Model routes and public observations. "
                                     "Opening this never uses the network; `refresh` does, once.")
    parser.add_argument("action", nargs="?", choices=("refresh", "status"),
                        help="refresh: fetch the public leaderboard once; status: offline diagnostics")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--plain", action="store_true", help="print the table even on a terminal")
    parser.add_argument("--all", action="store_true", help="include providers Pod has no route for")
    parser.add_argument("--enable", action="append", default=[], metavar="ROUTE", help="enable agent/model/effort")
    parser.add_argument("--disable", action="append", default=[], metavar="ROUTE", help="disable agent/model/effort")
    parser.add_argument("--preferred", metavar="ROUTE|none", help="set or clear the Preferred route")
    parser.add_argument("--pin", metavar="ROUTE|none", help="set or clear the Pin")
    return parser


def _emit(value: dict) -> None:
    print(json.dumps({"schema": SCHEMA, **value}, indent=2, sort_keys=True))


def _refresh(args) -> int:
    attempt = snapshot.refresh()
    if args.json:
        _emit({"refresh": attempt})
    elif attempt["ok"]:
        print(f"Refreshed: {attempt['rows']} public rows, {attempt['methodology'] or 'methodology not stated'}.")
        for line in attempt["diagnostics"]:
            print("  " + line)
        print("No route was enabled, disabled or moved by this.")
    else:
        kept = snapshot.load()
        print(f"Refresh failed: {attempt['failure']}.", file=sys.stderr)
        print("The earlier snapshot was kept." if kept else "There is no earlier snapshot.", file=sys.stderr)
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
                        "pinned": preferences.pinned},
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
    print(f"Preferences: {preferences.status} at {preferences.path}; {len(preferences.enabled())} enabled; "
          f"Preferred {preferences.preferred or 'none'}; Pin {preferences.pinned or 'none'}")
    print("Rows by state: " + ", ".join(f"{state} {count}" for state, count in sorted(counts.items())))
    return 0


def _edit(args) -> None:
    changes: dict = {"routes": {**dict.fromkeys(args.enable, "enabled"), **dict.fromkeys(args.disable, "disabled")}}
    if set(args.enable) & set(args.disable):
        raise PodError("route_invalid", "A route is named by both --enable and --disable.", fix="name it once")
    for flag, name in (("preferred", "preferred"), ("pin", "pinned")):
        value = getattr(args, flag)
        if value is not None:
            changes[name] = None if value.lower() in ("none", "null") else value
    result = edit.apply(**changes)
    if not args.json:
        print(f"Saved {result.path}: {len(result.enabled())} enabled; Preferred {result.preferred or 'none'}; "
              f"Pin {result.pinned or 'none'}")


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "refresh":
            return _refresh(args)
        if args.action == "status":
            return _status(args)
        if args.enable or args.disable or args.preferred is not None or args.pin is not None:
            _edit(args)
            if not args.json:
                return 0
        if not (args.json or args.plain) and sys.stdin.isatty() and sys.stdout.isatty():
            from . import workspace
            return workspace.run()
        model = view.build()
        rows = view.select(model, provider="all" if args.all else "routed")
        if args.json:
            _emit({"header": model.header(), "rows": [view.as_json(row) for row in rows]})
        else:
            sys.stdout.write(table.plain(model, rows, shutil.get_terminal_size((120, 24)).columns))
        return 0
    except PodError as exc:
        if args.json:
            _emit({"error": exc.as_dict()})
        else:
            print(f"pod models: {exc}" + (f"\n  To proceed: {exc.fix}" if exc.fix else ""), file=sys.stderr)
        return 2

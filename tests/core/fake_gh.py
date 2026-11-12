#!/usr/bin/env python3
"""A stand-in `gh` for tests: issues, one pull request per number, its checks, and a merge
that happens only when the head still matches, as GitHub's own does."""

import json
import os
import sys

STATE = os.environ["FAKE_GH_STATE"]


def load():
    with open(STATE) as stream:
        return json.load(stream)


def save(state):
    with open(STATE, "w") as stream:
        json.dump(state, stream)


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def main():
    args = sys.argv[1:]
    state = load()
    state.setdefault("calls", []).append(args[:3])
    if state.get("down"):
        save(state)
        print("gh: network unreachable", file=sys.stderr)
        raise SystemExit(1)
    what = args[:2]
    if what == ["issue", "view"]:
        save(state)
        row = state["issues"].get(args[2])
        if not row:
            print("issue not found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({"number": int(args[2]), "state": "OPEN", "url": "", **row}))
    elif what == ["issue", "edit"]:
        state["issues"][args[2]]["body"] = sys.stdin.read()
        state["edits"] = state.get("edits", 0) + 1
        save(state)
    elif what == ["issue", "comment"]:
        state.setdefault("comments", []).append(sys.stdin.read())
        save(state)
    elif what == ["pr", "view"]:
        save(state)
        row = next((row for number, row in state["prs"].items()
                    if args[2] in (number, row["headRefName"])), None)
        if not row:
            print("no pull requests found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(row))
    elif what == ["pr", "checks"]:
        save(state)
        rows = state.get("checks", [])
        if "--required" in args:
            rows = [row for row in rows if row.get("required", True)]
            if not rows:
                print("no required checks reported on the 'x' branch", file=sys.stderr)
                raise SystemExit(1)
        print(json.dumps([{key: row[key] for key in ("name", "bucket", "state")} for row in rows]))
        raise SystemExit(0 if all(row["bucket"] == "pass" for row in rows) else 8)
    elif what == ["pr", "merge"]:
        row = state["prs"][args[2]]
        state["merge_attempts"] = state.get("merge_attempts", 0) + 1
        if row["state"] != "OPEN" or row["headRefOid"] != option(args, "--match-head-commit"):
            save(state)
            print("head commit does not match or pull request is not open", file=sys.stderr)
            raise SystemExit(1)
        row.update(state="MERGED", mergedAt="2026-01-01T00:00:00Z", mergeCommit={"oid": "m" * 40})
        state["merges"] = state.get("merges", 0) + 1
        save(state)
        if state.get("lose_merge_reply"):
            raise SystemExit(1)
    else:
        save(state)
        print("unknown gh command", file=sys.stderr)
        raise SystemExit(2)


main()

#!/usr/bin/env python3
"""A stand-in `orca` for tests. It keeps Runs, Tasks and Dispatches in one JSON file and
reproduces the two behaviours Pod relies on: a coordinator that no longer holds the Run is
refused, and a repeated request returns the first outcome instead of starting a second worker."""

import json
import os
import subprocess
import sys

STATE = os.environ["FAKE_ORCA_STATE"]
ME = os.environ.get("FAKE_ORCA_CALLER", "term_one")


def load():
    try:
        with open(STATE) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {"runs": {}, "tasks": {}, "workers": {}, "requests": {}, "bound": {}, "starts": 0, "fail": None}


def reply(ok, value, state=None):
    if state is not None:
        with open(STATE, "w") as stream:
            json.dump(state, stream)
    print(json.dumps({"ok": True, "result": value} if ok else {"ok": False, "error": value}))
    raise SystemExit(0 if ok else 1)


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def main():
    if "--full" in sys.argv and sys.argv[1] != "orchestration":
        print(json.dumps({"ok": False, "error": {"code": "invalid_argument", "message": "Unknown flag --full"}}))
        raise SystemExit(1)
    args = [item for item in sys.argv[1:] if item not in ("--json", "--full")]
    state = load()
    command = args[1] if args[:1] == ["orchestration"] else None
    if command and state.get("fail") == command:
        state["fail"] = None
        with open(STATE, "w") as stream:
            json.dump(state, stream)
        print("not json")
        raise SystemExit(1)
    if command == "run-current":
        reply(True, {"run": {"id": state["bound"][ME]} if ME in state["bound"] else None})
    if command == "run-create":
        run = f"run_{len(state['runs']) + 1}"
        state["runs"][run] = {"id": run, "coordinator_handle": ME, "consumer_generation": 1}
        state["bound"][ME] = run
        reply(True, {"run": state["runs"][run]}, state)
    if command in ("run-use", "run-show"):
        run = state["runs"].get(option(args, "--id"))
        if not run:
            reply(False, {"code": "run_not_found", "message": "no such Run"})
        if command == "run-use" and run["coordinator_handle"] != ME:
            run.update(coordinator_handle=ME, consumer_generation=run["consumer_generation"] + 1)
            state["bound"][ME] = run["id"]
        reply(True, {"run": run}, state)
    if command == "worker-start":
        run = state["runs"].get(option(args, "--run"))
        request = option(args, "--retry-request")
        spec = sys.stdin.read() if option(args, "--spec-file") == "-" else option(args, "--spec", "")
        if request in state["requests"]:
            first = state["requests"][request]
            if first["spec"] != spec:
                reply(False, {"code": "request_mismatch", "message": "already used with different input"})
            reply(True, {**first["receipt"], "mutation": {"requestId": request, "replayed": True}})
        if not run or run["coordinator_handle"] != ME:
            reply(False, {"code": "consumer_fenced", "message": "worker-start needs the coordinator bound to the Run"})
        state["starts"] += 1
        number = len(state["tasks"]) + 1
        task, dispatch = f"task_{number}", f"ctx_{number}"
        state["tasks"][task] = {"id": task, "run_id": run["id"], "task_title": option(args, "--task-title"),
                                "status": "dispatched", "result": None, "spec": spec,
                                "base_branch": option(args, "--base-branch"), "repo": option(args, "--repo")}
        where = option(args, "--worktree", "")
        if where.startswith("new-"):               # Orca makes a real worktree from the named repository and branch
            repo = option(args, "--repo", "")[len("path:"):]
            place = os.path.join(os.path.dirname(STATE), f"worker-{number}")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "-b", f"worker/{number}", place,
                            option(args, "--base-branch")], check=True, capture_output=True)
            state.setdefault("worktrees", {})[place] = repo
        else:
            place = where[len("path:"):]
        state["workers"][dispatch] = {"dispatchId": dispatch, "taskId": task, "runId": run["id"], "workerState": "running",
                                      "dispatchStatus": "dispatched", "terminalState": "active",
                                      "agentTerminalHandle": f"term_{dispatch}", "ownWorktree": where.startswith("new-"),
                                      "resource": {"ownershipState": "owned", "retainedReason": None,
                                                   "worktreeId": f"repo::{place}"}}
        route = {"agent": option(args, "--agent"), "model": option(args, "--model"), "effort": option(args, "--effort")}
        receipt = {"taskId": task, "dispatchId": dispatch, "launch": {"requested": route, "effective": state.get("effective") or route}}
        state["requests"][request] = {"spec": spec, "receipt": receipt}
        if state.get("start_fails"):
            state["workers"][dispatch].update(workerState="failed", dispatchStatus="failed", lastFailure=state["start_fails"])
            state["tasks"][task]["status"] = "failed"
            receipt["state"] = "failed"
            reply(True, {**receipt, "mutation": {"requestId": request, "replayed": False}}, state)
        if state.get("lose_reply"):
            state["lose_reply"] = False
            with open(STATE, "w") as stream:
                json.dump(state, stream)
            raise SystemExit(1)
        reply(True, {**receipt, "mutation": {"requestId": request, "replayed": False}}, state)
    if command == "worker-release":
        row = state["workers"][option(args, "--dispatch")]
        if row["terminalState"] not in ("reclaimable", "retained"):      # an explicit release also ends a kept terminal
            reply(False, {"code": "worker_not_releasable", "message": f"terminal is {row['terminalState']}"})
        row["terminalState"] = "released"
        state["releases"] = state.get("releases", 0) + 1
        reply(True, {"dispatchId": row["dispatchId"], "state": "released"}, state)
    if args[:2] == ["worktree", "rm"]:
        place = option(args, "--worktree")[len("path:"):]
        state["removal_attempts"] = state.get("removal_attempts", 0) + 1
        done = None if state.get("removal_fails") or "--force" in args else subprocess.run(
            ["git", "-C", state["worktrees"][place], "worktree", "remove", place], capture_output=True, text=True)
        if done is None or done.returncode:
            reply(False, {"code": "worktree_remove_failed", "message": done.stderr if done else "the disk said no"}, state)
        reply(True, {"removed": True}, state)
    if args[:2] == ["terminal", "list"]:
        place = option(args, "--worktree")[len("path:"):]
        own = [{"handle": row["agentTerminalHandle"]} for row in state["workers"].values()
               if row["resource"]["worktreeId"].endswith("::" + place) and row["terminalState"] in ("active", "retained", "reclaimable")]
        reply(True, {"terminals": own + state.get("open_terminals", {}).get(place, [])})
    if command == "worker-show":
        row = state["workers"].get(option(args, "--dispatch"), {})
        reply(True, {"dispatch": {"id": row.get("dispatchId"), "status": row.get("dispatchStatus"),
                                  "lastFailure": row.get("lastFailure")}})
    if command == "request-show":
        request = option(args, "--request")
        known = state["requests"].get(request)
        reply(True, {"requestId": request, "state": "completed" if known else "absent",
                     **({"receipt": known["receipt"]} if known else {})})
    if command == "worker-list":
        rows = [row for row in state["workers"].values() if row["runId"] == option(args, "--run")]
        reply(True, {"workers": rows, "page": {"hasMore": False, "nextCursor": None}})
    if command == "task-list":
        rows = [{key: value for key, value in row.items() if key not in ("spec", "base_branch", "repo")} for row in state["tasks"].values()
                if row["run_id"] == option(args, "--run")]
        reply(True, {"tasks": rows})
    reply(False, {"code": "unknown_command", "message": " ".join(args)})


main()

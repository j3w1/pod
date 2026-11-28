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
                                "worktree": option(args, "--worktree"),
                                "base_branch": option(args, "--base-branch"), "repo": option(args, "--repo"),
                                "name": option(args, "--name"), "display_name": option(args, "--display-name")}
        where = option(args, "--worktree", "")
        if where.startswith("new-"):               # Orca makes a real worktree from the named repository and branch
            repo = option(args, "--repo", "")[len("path:"):]
            place = os.path.join(os.path.dirname(STATE), f"worker-{number}")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "-b", f"worker/{number}", place,
                            option(args, "--base-branch")], check=True, capture_output=True)
            state.setdefault("worktrees", {})[place] = repo
        else:
            folders = {row["address"]: row["path"] for row in state.get("folders", [])}
            place = folders.get(where) or where[len("path:"):]
        handle = f"term_{dispatch}"
        state["workers"][dispatch] = {"dispatchId": dispatch, "taskId": task, "runId": run["id"], "workerState": "running",
                                      "dispatchStatus": "dispatched", "terminalState": "active",
                                      "agentTerminalHandle": handle, "ownWorktree": where.startswith("new-"),
                                      "resource": {"ownershipState": "owned", "retainedReason": None,
                                                   "worktreeId": f"repo::{place}"}}
        titles = state.setdefault("titles", {})
        if handle not in titles:
            titles[handle] = state["initial_title"] if "initial_title" in state else f"worker-{task}"
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
        if state.get("release_stays"):              # Orca answers ok and the terminal is still there
            reply(True, {"dispatchId": row["dispatchId"], "state": state["release_stays"]}, state)
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
    if args[:2] == ["terminal", "rename"]:
        state.setdefault("rename_calls", []).append(
            {"terminal": option(args, "--terminal"), "title": option(args, "--title")})
        if state.get("rename_fails"):
            reply(False, {"code": "terminal_not_found", "message": "no such terminal"}, state)
        state.setdefault("titles", {})[option(args, "--terminal")] = option(args, "--title")
        reply(True, {"renamed": True}, state)
    if args[:2] == ["terminal", "list"]:
        if state.get("list_fails"):
            reply(False, {"code": "terminal_list_failed", "message": "cannot list terminals"})
        raw = option(args, "--worktree")
        place = raw[len("path:"):] if raw else None
        titles = state.get("titles", {})
        own = []
        for row in state["workers"].values():
            if row["terminalState"] not in ("active", "retained", "reclaimable"):
                continue
            if place is not None and not row["resource"]["worktreeId"].endswith("::" + place):
                continue
            handle = row["agentTerminalHandle"]
            item = {"handle": handle}
            if not state.get("omit_title"):
                item["title"] = titles.get(handle, f"worker-{row['taskId']}")
            own.append(item)
        extra = (state.get("open_terminals", {}).get(place, []) if place is not None
                 else [item for items in state.get("open_terminals", {}).values() for item in items])
        reply(True, {"terminals": own + extra})
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
    if args[:2] == ["terminal", "show"]:            # the workspace Orca started this terminal in
        held = state.get("terminals", {}).get(option(args, "--terminal"))
        if held is None:
            reply(False, {"code": "terminal_not_found", "message": "no such terminal"})
        rows = [row for row in state.get("folders", []) if row["id"] == held]
        reply(True, {"terminal": {"handle": option(args, "--terminal"), "worktreeId": held,
                                  "worktreePath": rows[0]["path"] if rows else None}})
    if args[:2] == ["worktree", "show"]:            # folder workspaces only; Git repositories never ask
        state["shows"] = state.get("shows", 0) + 1
        wanted = option(args, "--worktree")
        for row in state.get("folders", []):
            if wanted in (row["address"], f"id:{row['id']}", f"identity:{row['instanceId']}"):
                reply(True, {"worktree": {"id": row["id"], "repoId": row["repoId"], "path": row["path"], "branch": "",
                                          "head": "", "instanceId": row["instanceId"],
                                          "address": {"environment": "local", "worktree": row["address"]}}}, state)
        reply(False, {"code": "selector_not_found", "message": f"no workspace {wanted}"}, state)
    if args[:2] == ["worktree", "list"]:
        project = option(args, "--repo")[len("id:"):]
        reply(True, {"worktrees": [{"id": row["id"], "repoId": row["repoId"], "path": row["path"]}
                                   for row in state.get("folders", []) if row["repoId"] == project]})
    if args[:2] == ["repo", "show"]:
        project = option(args, "--repo")[len("id:"):]
        if project in state.get("repos", {}):
            reply(True, {"repo": {"id": project, "kind": state["repos"][project]}})
        reply(False, {"code": "repo_not_found", "message": "no such repo"})
    reply(False, {"code": "unknown_command", "message": " ".join(args)})


main()

"""Pod's port to Orca. Orca owns Runs, Tasks, Dispatches, workers and who coordinates a Run.

Pod stores Orca's identifiers and reads the rest at use. A worker is started only here, with a
request id the caller derives, so a repeated start replays Orca's recorded outcome instead of
starting a second worker, and a coordinator that no longer holds the Run is refused by Orca.
"""

import json
import os
import re
import subprocess
import uuid

from .errors import PodError

REPEAT = "repeat the same Pod command; a start is replayed, never duplicated"
FIXES = {"consumer_fenced": "this conversation does not coordinate the objective's Run (it holds another Run, or another "
                            "conversation took this one over); run `pod resume` for this objective",
         "request_mismatch": "this assignment was already started with a different packet; dispatch a new assignment"}
UNKNOWN = ("runtime_timeout", "runtime_unavailable", "invalid_runtime_response")   # Orca itself does not know
SETTLED = ("completed", "failed", "succeeded", "stopped", "abandoned", "cancelled", "released")


def call(*args: str, stdin: str | None = None, timeout: int = 90) -> dict:
    """One Orca command. Returns its result, or raises the refusal Orca gave."""
    what = " ".join(args[:2])
    try:
        flags = ["--json", "--full"] if args[0] == "orchestration" else ["--json"]     # only orchestration has a short view
        done = subprocess.run(["orca", *args, *flags], capture_output=True, timeout=timeout,
                              input=None if stdin is None else stdin.encode())
    except FileNotFoundError as exc:
        raise PodError("orca_unavailable", "The `orca` command is not on PATH.", fix="install Orca or run Pod inside it") from exc
    except subprocess.TimeoutExpired as exc:
        raise PodError("orca_unknown", f"orca {what} did not answer in {timeout} s; its outcome is unknown.", fix=REPEAT) from exc
    try:
        reply = json.loads(done.stdout.decode(errors="replace"), strict=False)
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or "ok" not in reply:
        raise PodError("orca_unknown", f"orca {what} gave no readable answer; its outcome is unknown.", fix=REPEAT)
    if not reply["ok"]:
        error = reply.get("error") or {}
        code = str(error.get("code") or "orca_refused")
        if code in UNKNOWN:
            raise PodError("orca_unknown", f"orca {what} could not say what happened ({code}).", fix=REPEAT)
        raise PodError(code, f"Orca refused: {str(error.get('message') or '')[:400]}",
                       fix=FIXES.get(code, "read Orca's message and correct the request"))
    return reply.get("result") or {}


def folder_workspace(path: str) -> dict | None:
    """The registered Orca folder workspace of the calling terminal, when it is at `path`; else None.

    Every workspace of a folder project can share one path, so the directory never says which one
    this is: the terminal Orca started the caller in does. Pod keeps the workspace's address, its
    instance and its project, and refuses a terminal workspace that names more than one.
    """
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    if not handle:
        return None
    try:
        terminal = call("terminal", "show", "--terminal", handle).get("terminal") or {}
    except PodError as exc:
        if exc.code == "orca_unknown":
            raise
        return None
    held = str(terminal.get("worktreeId") or "")
    if not held:
        return None
    found = workspace(held if held.startswith(("folder:", "id:", "identity:")) else f"id:{held}")   # `<repo>::<path>` as is
    if not found or found["path"] != path or terminal.get("worktreePath") not in (None, path):
        return None
    try:
        rows = call("worktree", "list", "--repo", f"id:{found['project']}").get("worktrees") or []
    except PodError as exc:
        if exc.code == "orca_unknown":
            raise
        rows = []
    if sum(1 for row in rows if row.get("id") == held) > 1:
        raise PodError("folder_workspace_ambiguous", f"This terminal's workspace {held} names several Orca folder workspaces.",
                       fix="start the coordinator in its own folder workspace, whose terminal names exactly one")
    return found


def workspace(selector: str) -> dict | None:
    """The Orca folder workspace an exact selector names, or None when it names none or not a folder."""
    try:
        row = call("worktree", "show", "--worktree", selector).get("worktree") or {}
        project, path = str(row.get("repoId") or ""), str(row.get("path") or "")
        kind = (call("repo", "show", "--repo", f"id:{project}").get("repo") or {}).get("kind") if project else None
    except PodError as exc:
        if exc.code == "orca_unknown":
            raise
        return None
    address = str((row.get("address") or {}).get("worktree") or "")
    if kind != "folder" or not path or not (address.startswith("folder:") or address == f"id:{project}::{path}"):
        return None
    return {"path": path, "workspace_id": address, "instance_id": row.get("instanceId") or None, "project": project}


def request_id(repository: str, objective: str, assignment: str, attempt: int) -> str:
    """The same assignment and attempt always give the same request, so a retry cannot start twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pod:{repository}:{objective}:{assignment}:{attempt}"))


def run_current() -> str | None:
    result = call("orchestration", "run-current")
    return (result.get("run") or {}).get("id") or result.get("runId")


def run_create(objective: str) -> str:
    return call("orchestration", "run-create", "--objective", objective[:200])["run"]["id"]


def run_use(run: str) -> dict:
    """Bind this conversation as the Run's coordinator. Orca fences the earlier one."""
    return call("orchestration", "run-use", "--id", run)["run"]


def run_show(run: str) -> dict:
    return call("orchestration", "run-show", "--id", run)["run"]


def start(*, run: str, spec: str, title: str, worktree: str, route: tuple[str, str, str | None], request: str,
          name: str | None = None, worktree_name: str | None = None,
          new_from: tuple[str, str] | None = None) -> dict:
    """Start one worker. Repeating the call with the same request returns the first outcome.

    `new_from` is the repository and branch a new worktree is made from. Without it Orca would
    make the worktree in the coordinator's own repository, which need not be the objective's.
    """
    agent, model, effort = route
    args = ["orchestration", "worker-start", "--spec-file", "-", "--task-title", title, "--worktree", worktree,
            "--agent", agent, "--model", model]
    if effort:
        args += ["--effort", effort]
    args += ["--run", run, "--retry-request", request]
    args += (["--name", worktree_name] if (new_from and worktree_name) else []) + (
        ["--display-name", name] if name else []) + (
        ["--repo", f"path:{new_from[0]}", "--base-branch", new_from[1]] if new_from else [])
    result = call(*args, stdin=spec, timeout=180)
    launch = result.get("launch") or {}
    if result.get("state") == "outcome_unknown" or not result.get("taskId"):
        raise PodError("orca_unknown", "Orca accepted the start and does not know whether the worker is running.", fix=REPEAT)
    return {"task": result.get("taskId"), "dispatch": result.get("dispatchId"), "state": result.get("state"),
            "replayed": bool((result.get("mutation") or {}).get("replayed")),
            "effective": launch.get("effective"), "requested": launch.get("requested")}


def _is_default_tab_title(current: str, task_id: str | None = None, task_title: str | None = None,
                          marker: str | None = None) -> bool:
    """True for an empty title, exact `worker-<taskId>`, the exact `task_title` Pod passed, or a daemon's
    `<Role> · <task title…>` rename that starts the task title with this assignment's own marker."""
    if not current.strip():
        return True
    if task_id and current == f"worker-{task_id}":
        return True
    if task_title and current == task_title:
        return True
    if marker and task_title and task_title.startswith(marker + " "):
        head, sep, rest = current.partition(" · ")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z ()]{0,23}", head):
            if rest == task_title:
                return True
            rest = re.sub(r" · [A-Za-z0-9.\-]+/[a-z]+$", "", rest)       # a daemon's trailing model/effort
            if rest == task_title:
                return True
            # Orca's readback truncation: the task title's own leading words, then an ellipsis
            return rest.endswith("…") and len(rest) > len(marker) + 2 and task_title.startswith(rest[:-1])
    return False


def rename(run: str, started: dict, title: str, *, task_title: str | None = None,
           marker: str | None = None) -> str | None:
    """Give a default-titled worker tab a readable title. Returns why it could not, or None; never raises."""
    try:
        worker = next((row for row in workers(run, 15, started.get("dispatch")) if row["dispatch"] == started.get("dispatch")), None)
        handle = worker.get("handle") if isinstance(worker, dict) else None
        if not handle:
            return "Orca named no terminal for the worker"
        worktree = worker.get("worktree")
        # Orca has no atomic conditional rename; reading the title first accepts a narrow concurrent-rename window.
        args = ("terminal", "list", "--worktree", f"path:{worktree}") if worktree else ("terminal", "list")
        reply = call(*args, timeout=15)
        listed = reply.get("terminals") if isinstance(reply, dict) else None
        if not isinstance(listed, list):
            return "terminal title could not be read"
        terminal = next((item for item in listed if isinstance(item, dict) and item.get("handle") == handle), None)
        current = terminal.get("title") if isinstance(terminal, dict) else None
        if not isinstance(current, str):
            return "terminal title could not be read"
        task_id = started.get("task") or (worker.get("task") if isinstance(worker, dict) else None)
        if not _is_default_tab_title(current, task_id=task_id, task_title=task_title, marker=marker):
            return None
        try:
            call("terminal", "rename", "--terminal", handle, "--title", title, timeout=15)
        except PodError as exc:
            return "outcome unknown, the title may have changed" if exc.code == "orca_unknown" else exc.code
    except PodError as exc:
        return exc.code
    except Exception as exc:      # cosmetic: nothing here may fail a dispatch that already started a worker
        return type(exc).__name__
    return None


def failure(dispatch: str) -> str:
    """Why Orca says a Dispatch failed, in its own words; empty when it does not say."""
    try:
        return str((call("orchestration", "worker-show", "--dispatch", dispatch).get("dispatch") or {}).get("lastFailure") or "")
    except PodError:
        return ""


def workers(run: str, timeout: int = 90, until: str | None = None) -> list[dict]:
    """Every worker Dispatch of the Run with its live state."""
    rows, cursor = [], None
    while True:
        args = ["orchestration", "worker-list", "--run", run, "--limit", "100"] + (["--cursor", cursor] if cursor else [])
        result = call(*args, timeout=timeout)
        for row in result.get("workers", []):
            held = row.get("resource") or {}
            rows.append({"dispatch": row.get("dispatchId"), "task": row.get("taskId"), "state": row.get("workerState"),
                         "status": row.get("dispatchStatus"), "terminal": row.get("terminalState"),
                         "handle": row.get("agentTerminalHandle"),
                         "owned": held.get("ownershipState") == "owned", "retained": held.get("retainedReason"),
                         "worktree": str(held.get("worktreeId") or "").partition("::")[2] or None})
        if until and any(row["dispatch"] == until for row in rows):
            return rows
        page = result.get("page") or {}
        cursor = page.get("nextCursor")
        if not page.get("hasMore") or not cursor:
            return rows


def live(row: dict) -> bool:
    """Whether a worker may still be changing files. Anything not known to be settled counts as live."""
    return str(row.get("status") or "").lower() not in SETTLED and str(row.get("state") or "").lower() not in SETTLED


def tasks(run: str) -> dict:
    """Tasks of the Run by id, each with the worker's own result when it has reported one."""
    result = {}
    for row in call("orchestration", "task-list", "--run", run).get("tasks", []):
        report = row.get("result")
        if isinstance(report, str):
            try:
                report = json.loads(report, strict=False)
            except ValueError:
                report = None
        result[row["id"]] = {"status": row.get("status"), "title": row.get("task_title"),
                             "report": report if isinstance(report, dict) else None}
    return result

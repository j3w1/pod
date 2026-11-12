"""Pod's port to Orca. Orca owns Runs, Tasks, Dispatches, workers and who coordinates a Run.

Pod stores Orca's identifiers and reads the rest at use. A worker is started only here, with a
request id the caller derives, so a repeated start replays Orca's recorded outcome instead of
starting a second worker, and a coordinator that no longer holds the Run is refused by Orca.
"""

import json
import subprocess
import uuid

from .errors import PodError

REPEAT = "repeat the same Pod command; a start is replayed, never duplicated"
FIXES = {"consumer_fenced": "another conversation coordinates this Run now; run `pod resume` to take it over",
         "request_mismatch": "this assignment was already started with a different packet; dispatch a new assignment"}
SETTLED = ("completed", "failed", "succeeded", "stopped", "abandoned", "cancelled", "released")


def call(*args: str, stdin: str | None = None, timeout: int = 90) -> dict:
    """One Orca command. Returns its result, or raises the refusal Orca gave."""
    what = " ".join(args[:2])
    try:
        done = subprocess.run(["orca", *args, "--json", "--full"], capture_output=True, timeout=timeout,
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
        raise PodError(code, f"Orca refused: {str(error.get('message') or '')[:400]}",
                       fix=FIXES.get(code, "read Orca's message and correct the request"))
    return reply.get("result") or {}


def request_id(objective: str, assignment: str, attempt: int) -> str:
    """The same assignment and attempt always give the same request, so a retry cannot start twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pod:{objective}:{assignment}:{attempt}"))


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


def start(*, run: str, spec: str, title: str, worktree: str, route: tuple[str, str, str], request: str,
          name: str | None = None) -> dict:
    """Start one worker. Repeating the call with the same request returns the first outcome."""
    agent, model, effort = route
    args = ["orchestration", "worker-start", "--spec-file", "-", "--task-title", title, "--worktree", worktree,
            "--agent", agent, "--model", model, "--effort", effort, "--run", run, "--retry-request", request]
    result = call(*args, *(["--display-name", name] if name else []), stdin=spec, timeout=180)
    launch = result.get("launch") or {}
    return {"task": result.get("taskId"), "dispatch": result.get("dispatchId"),
            "replayed": bool((result.get("mutation") or {}).get("replayed")),
            "effective": launch.get("effective"), "requested": launch.get("requested")}


def workers(run: str) -> list[dict]:
    """Every worker Dispatch of the Run with its live state."""
    rows, cursor = [], None
    while True:
        args = ["orchestration", "worker-list", "--run", run, "--limit", "100"] + (["--cursor", cursor] if cursor else [])
        result = call(*args)
        rows += [{"dispatch": row.get("dispatchId"), "task": row.get("taskId"), "state": row.get("workerState"),
                  "status": row.get("dispatchStatus"), "terminal": row.get("terminalState")} for row in result.get("workers", [])]
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

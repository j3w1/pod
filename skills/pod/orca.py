"""Installed-Orca adapter: bounded reads and an exact worker-start mutation allowlist."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .errors import PodError

MAX_OUTPUT = 2_000_000
CAPABILITY_KEYS = {"contract_v1": "orchestration.contract.v1",
                   "launch_preferences_v1": "orchestration.worker-launch-preferences.v1"}
AGENTS = ("codex", "claude")
PREFLIGHT_REFUSALS = ("task_not_found", "task_not_startable", "inject_rejected")
WORKTREE_SELECTOR_PREFIXES = ("path:", "id:", "identity:", "name:", "branch:", "issue:")


def executable() -> Path:
    selected = os.environ.get("ORCA_CLI_COMMAND")
    if selected:
        found = shutil.which(selected)
    elif os.environ.get("ORCA_DEV_REPO_ROOT"):
        found = shutil.which("orca-dev")
    elif sys.platform.startswith("linux") and not os.environ.get("ORCA_TERMINAL_HANDLE"):
        found = shutil.which("orca-ide")
    else:
        found = shutil.which("orca")
    if not found:
        raise PodError("orca_unavailable", "Configured Orca executable is unavailable")
    path = Path(found).resolve()
    if not path.is_file():
        raise PodError("orca_unavailable", "Orca executable is not a file")
    return path


def _argument(value: object, *, limit: int = 4096) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("-") and len(value) <= limit


def _read_allowed(argv: list[str]) -> bool:
    if argv in (["--version"], ["status", "--json"],
                ["orchestration", "run-current", "--json"]):
        return True
    if (len(argv) == 5 and argv[:3] == ["orchestration", "worker-show", "--dispatch"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if (len(argv) == 5 and argv[:3] == ["orchestration", "request-show", "--request"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if (len(argv) == 5 and argv[:3] == ["worktree", "show", "--worktree"]
            and worktree_selector(argv[3]) is not None and argv[4] == "--json"):
        return True
    if argv[:4] == ["orchestration", "worker-list", "--include-remote", "--json"]:
        return _worker_list_tail(argv[4:])
    return False


def read_command(argv: list[str], *, timeout: int = 10) -> dict:
    if not _read_allowed(argv):
        raise PodError("unsupported_orca_read", "Orca adapter accepts only known read operations")
    command = [str(executable()), *argv]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("orca_unavailable", "Orca read failed") from exc
    if proc.returncode:
        raise PodError("orca_read_failed", "Orca read did not succeed")
    if len(proc.stdout) > MAX_OUTPUT:
        raise PodError("orca_contract", "Orca read exceeds bounded output")
    if argv == ["--version"]:
        return {"version": proc.stdout.strip(), "executable": str(executable())}
    return _envelope(proc.stdout)


def _envelope(stdout: str) -> dict:
    try:
        value = json.loads(stdout)
    except ValueError as exc:
        raise PodError("orca_contract", "Orca returned invalid JSON") from exc
    if not isinstance(value, dict) or not value.get("ok") or not isinstance(value.get("result"), dict):
        raise PodError("orca_contract", "Orca returned an unsupported result")
    meta = value.get("_meta")
    runtime = meta.get("runtimeId") if isinstance(meta, dict) else None
    if not isinstance(runtime, str) or not runtime:
        raise PodError("orca_contract", "Orca runtime identity is unavailable")
    mutation = value["result"].get("mutation")
    if not isinstance(mutation, dict):
        mutation = value.get("mutation")
    request_uuid = mutation.get("requestId") if isinstance(mutation, dict) else None
    return {"runtime": runtime, "result": value["result"], "request_uuid": request_uuid}


def _mutation_envelope(stdout: str | bytes) -> dict:
    """Decode native response fields; refusal classification belongs to admission."""
    try:
        value = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        raise PodError("native_effect_uncertain", "Native response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PodError("native_effect_uncertain", "Native response is malformed")
    meta = value.get("_meta")
    runtime = meta.get("runtimeId") if isinstance(meta, dict) else None
    if not isinstance(runtime, str) or not runtime:
        raise PodError("native_effect_uncertain", "Native response has no runtime identity")
    if value.get("ok") is True and isinstance(value.get("result"), dict):
        result = value["result"]
        error = None
    elif value.get("ok") is False and isinstance(value.get("error"), dict):
        error = value["error"]
        native_result = value.get("result")
        if native_result is not None and not isinstance(native_result, dict):
            raise PodError("native_effect_uncertain", "Native refusal carries a malformed result")
        result = dict(native_result or {})
        if "error" in result:
            result["_result_error"] = result["error"]
        result["error"] = error
        for key in ("dispatchId", "workerId", "residualResources", "effects", "terminal",
                    "terminalHandle", "worktreeId", "terminalResourceId", "resource",
                    "failedStage"):
            if key not in value:
                continue
            if key in result and result[key] != value[key]:
                result.setdefault("_envelope_conflicts", {})[key] = value[key]
            else:
                result[key] = value[key]
    else:
        raise PodError("native_effect_uncertain", "Native response does not prove an effect or refusal")
    request_uuid, conflict = _request_identity(value)
    if conflict:
        result["_request_conflict"] = True
    return {"runtime": runtime, "result": result, "request_uuid": request_uuid,
            "error": error}


def _request_identity(value: dict) -> tuple[object, bool]:
    """Collect native identifiers even when the result body cannot be interpreted."""
    result = value.get("result") if isinstance(value.get("result"), dict) else {}
    error = value.get("error")
    request_references = []
    request_malformed = False
    for carrier in (result, value):
        if "mutation" not in carrier:
            continue
        mutation = carrier["mutation"]
        if not isinstance(mutation, dict) or mutation.get("requestId") is None:
            request_malformed = True
            continue
        request_references.append(mutation["requestId"])
    error_data = error.get("data") if isinstance(error, dict) else None
    error_request = (error_data.get("orchestrationRequestId")
                     if isinstance(error_data, dict) else None)
    if isinstance(error_data, dict) and "orchestrationRequestId" in error_data:
        if error_request is None:
            request_malformed = True
        else:
            request_references.append(error_request)
    request_uuid = request_references[0] if request_references else None
    return request_uuid, (request_malformed or any(
        reference != request_uuid for reference in request_references[1:]))


def _captured_output(value: str | bytes | None) -> dict:
    raw = value.encode("utf-8") if isinstance(value, str) else value or b""
    # Out-of-contract oversized output is identified, never silently truncated into proof.
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "base64": base64.b64encode(raw).decode("ascii") if len(raw) <= MAX_OUTPUT else None}


def _worker_list_tail(tail: list[str]) -> bool:
    if tail[:2] == ["--limit", "100"]:
        tail = tail[2:]
    if len(tail) < 2 or tail[0] != "--run" or not _argument(tail[1]):
        return False
    tail = tail[2:]
    if len(tail) >= 2 and tail[0] == "--cursor":
        if not _argument(tail[1]):
            return False
        tail = tail[2:]
    return not tail


def _mutate_allowed(argv: list[str]) -> bool:
    if argv[:2] == ["orchestration", "worker-start"]:
        return _worker_start_shape(argv[2:])
    return False


def _worker_start_shape(tail: list[str]) -> bool:
    retry = None
    if len(tail) >= 3 and tail[-3] == "--retry-request" and tail[-1] == "--json":
        retry = tail[-2]
        tail = tail[:-3] + ["--json"]
    if (len(tail) < 9 or tail[:1] != ["--task"] or not _argument(tail[1])
            or tail[2:3] != ["--run"] or not _argument(tail[3])
            or tail[4:5] != ["--worktree"] or worktree_selector(tail[5]) is None
            or tail[-1:] != ["--json"] or (retry is not None and not _argument(retry))):
        return False
    route = tail[6:-1]
    if len(route) == 2 and route[0] == "--terminal":
        return _argument(route[1])
    if len(route) == 4 and route[0] == "--agent" and route[2] == "--model":
        return _argument(route[1]) and _argument(route[3])
    if len(route) == 6 and route[0] == "--agent" and route[2] == "--model" and route[4] == "--effort":
        return all(_argument(route[i]) for i in (1, 3, 5))
    return False


def worktree_selector(value: object) -> str | None:
    """Accept an existing-worktree selector only; creation modes are refused here."""
    if value in ("current", "active"):
        return value
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value in ("new-child", "new-top-level"):
        return None
    for prefix in WORKTREE_SELECTOR_PREFIXES:
        if value.startswith(prefix) and len(value) > len(prefix):
            return value
    return None


def worktree_identity(selector: str) -> dict:
    """Resolve an existing native selector to its actual local Git workspace."""
    selected = worktree_selector(selector)
    if selected is None:
        raise PodError("invalid_worktree_selector", "Worker placement must name an existing worktree")
    try:
        response = read_command(["worktree", "show", "--worktree", selected, "--json"])
    except PodError as exc:
        raise PodError("worktree_resolution_unavailable",
                       "Orca could not resolve the selected worker worktree") from exc
    row = response["result"].get("worktree")
    git = row.get("git") if isinstance(row, dict) else None
    if not isinstance(row, dict) or not isinstance(git, dict):
        raise PodError("worktree_resolution_unavailable", "Orca worktree readback is incomplete")
    path, git_path = row.get("path"), git.get("path")
    branch, git_branch = row.get("branch"), git.get("branch")
    if (not isinstance(path, str) or not Path(path).is_absolute() or path != git_path
            or branch != git_branch or row.get("isBare") is not False
            or git.get("isBare") is not False):
        raise PodError("worktree_resolution_ambiguous", "Orca worktree identity is contradictory")
    normalized_branch = branch.removeprefix("refs/heads/") if isinstance(branch, str) else None
    if normalized_branch is not None and (not normalized_branch or len(normalized_branch) > 256):
        raise PodError("worktree_resolution_ambiguous", "Orca worktree branch is malformed")
    return {"runtime": response["runtime"], "path": str(Path(path).resolve()),
            "branch": normalized_branch}


def mutate_command(argv: list[str], *, timeout: int = 120, accept_exit: tuple[int, ...] = (0,)) -> dict:
    """Run one allowlisted state-changing Orca command and return its exact receipt."""
    if not _mutate_allowed(argv):
        raise PodError("unsupported_orca_mutation", "Orca adapter accepts only known mutations")
    command = [str(executable()), *argv]
    stdout, stderr, returncode, transport = None, None, None, "completed"
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, transport = exc.stdout, exc.stderr, "timeout"
    except OSError as exc:
        transport = type(exc).__name__
    observation = {"argv": command, "transport": transport, "exit": returncode,
                   "stdout": _captured_output(stdout), "stderr": _captured_output(stderr)}
    try:
        if transport != "completed":
            raise PodError("native_effect_uncertain", "Native response is unavailable")
        if returncode not in accept_exit:
            raise PodError("native_effect_uncertain", "Native command returned an unsupported exit status")
        if observation["stdout"]["bytes"] > MAX_OUTPUT:
            raise PodError("native_effect_uncertain", "Native response exceeds bounded output")
        envelope = _mutation_envelope(stdout)
    except PodError as exc:
        # Private exception attributes are persisted by admission, not echoed as CLI error text.
        exc.native_observation = observation
        try:
            value = json.loads(stdout) if observation["stdout"]["bytes"] <= MAX_OUTPUT else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            value = None
        if isinstance(value, dict):
            meta = value.get("_meta")
            request, conflict = _request_identity(value)
            exc.native_reference = {"runtime": meta.get("runtimeId") if isinstance(meta, dict) else None,
                                    "request_uuid": request, "_request_conflict": conflict}
        raise
    return {"runtime": envelope["runtime"], "exit": completed.returncode,
            "result": envelope["result"], "request_uuid": envelope.get("request_uuid"),
            "error": envelope.get("error"), "native_observation": observation}


def contract() -> dict:
    """What the installed runtime is and which orchestration contracts it advertises."""
    try:
        version = read_command(["--version"])
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    snapshot = {"status": "unavailable", "version": version["version"],
                "executable": version["executable"], "runtime": None, "capabilities": {}}
    try:
        status = read_command(["status", "--json"])
    except PodError as exc:
        snapshot["reason"] = exc.code
        return snapshot
    result = status["result"]
    runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    advertised = runtime.get("capabilities")
    advertised = advertised if isinstance(advertised, list) else []
    snapshot["runtime"] = status["runtime"]
    snapshot["status"] = "observed"
    snapshot["capabilities"] = {key: value in advertised for key, value in CAPABILITY_KEYS.items()}
    snapshot["runtime_state"] = runtime.get("state")
    return snapshot


def worker_rows(run: str) -> dict:
    """Read workers for one exact Run; unscoped fleet enumeration is forbidden."""
    if not _argument(run):
        raise PodError("orca_contract", "Exact Run identity is required for worker reads")
    prefix = ["orchestration", "worker-list", "--include-remote", "--json", "--limit", "100"]
    prefix += ["--run", run]
    all_rows: list[dict] = []
    cursor = None
    seen = set()
    runtime = None
    scope = None
    for _ in range(100):
        response = read_command(prefix + (["--cursor", cursor] if cursor else []))
        result = response["result"]
        page = result.get("page", {})
        rows = result.get("workers")
        if not isinstance(rows, list) or not isinstance(page, dict):
            raise PodError("orca_contract", "Run worker page is malformed")
        page_scope = result.get("scope")
        if (not isinstance(page_scope, dict) or page_scope.get("run") != run
                or page_scope.get("source") not in ("flag", "run")):
            raise PodError("orca_scope_changed", "Run worker page does not prove exact requested Run scope")
        if runtime is not None and response["runtime"] != runtime:
            raise PodError("orca_runtime_changed", "Runtime changed during Run worker read")
        if runtime is not None and result.get("scope") != scope:
            raise PodError("orca_scope_changed", "Run worker scope changed during read")
        runtime, scope = response["runtime"], result.get("scope")
        all_rows.extend(rows)
        if not page.get("hasMore"):
            return {"runtime": runtime, "scope": scope, "workers": all_rows,
                    "complete": True, "page_count": len(seen) + 1}
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or cursor in seen:
            raise PodError("orca_pagination", "Run worker cursor is missing or repeated")
        seen.add(cursor)
    raise PodError("orca_pagination", "Run worker read exceeds bounded pages")


def current_run() -> dict:
    """Read this terminal's native coordinator binding without adopting or creating one."""
    response = read_command(["orchestration", "run-current", "--json"])
    result = response["result"]
    if "run" not in result:
        raise PodError("orca_contract", "Current Run response lacks an explicit binding")
    row = result["run"]
    if row is None:
        return {"runtime": response["runtime"], "run": None}
    if not isinstance(row, dict):
        raise PodError("orca_contract", "Current Run binding is malformed")
    run_id = row.get("id")
    coordinator = row.get("coordinator_handle")
    generation = row.get("consumer_generation")
    if (not isinstance(run_id, str) or not run_id
            or not isinstance(coordinator, str) or not coordinator
            or type(generation) is not int or generation < 0):
        raise PodError("orca_contract", "Current Run ownership proof is incomplete")
    return {"runtime": response["runtime"], "run": {
            "id": run_id, "coordinator_handle": coordinator,
            "consumer_generation": generation}}


def worker_show(dispatch: str) -> dict:
    return read_command(["orchestration", "worker-show", "--dispatch", dispatch, "--json"])

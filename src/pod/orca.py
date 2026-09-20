"""Read-only Orca contract adapter. Mutations require separate guarded admission."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import json
from datetime import datetime, timezone

from .errors import PodError


def executable() -> Path:
    selected = os.environ.get("ORCA_CLI_COMMAND")
    if selected:
        found = shutil.which(selected)
    elif os.environ.get("ORCA_DEV_REPO_ROOT"):
        found = shutil.which("orca-dev")
    else:
        found = shutil.which("orca" if os.name == "nt" or os.environ.get("ORCA_TERMINAL_HANDLE") else "orca-ide")
    if not found:
        raise PodError("orca_unavailable", "Configured Orca executable is unavailable")
    path = Path(found).resolve()
    if not path.is_file():
        raise PodError("orca_unavailable", "Orca executable is not a file")
    return path


def read_command(argv: list[str], *, timeout: int = 10) -> dict:
    allowed = (
        argv == ["--version"]
        or argv == ["account", "list", "--json"]
        or argv == ["orchestration", "run-list", "--json"]
        or (argv[:4] == ["orchestration", "worker-list", "--include-remote", "--json"]
            and _worker_list_tail(argv[4:]))
        or (len(argv) == 5 and argv[:3] == ["orchestration", "worker-show", "--dispatch"]
            and bool(argv[3]) and not argv[3].startswith("-") and argv[4] == "--json")
    )
    if not allowed:
        raise PodError("unsupported_orca_read", "Orca adapter accepts only known read operations")
    command = [str(executable()), *argv]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("orca_unavailable", "Orca read failed") from exc
    if proc.returncode:
        raise PodError("orca_read_failed", "Orca read did not succeed")
    if len(proc.stdout) > 2_000_000:
        raise PodError("orca_contract", "Orca read exceeds bounded output")
    if argv == ["--version"]:
        return {"version": proc.stdout.strip(), "executable": str(executable())}
    try:
        value = json.loads(proc.stdout)
    except ValueError as exc:
        raise PodError("orca_contract", "Orca returned invalid JSON") from exc
    if not isinstance(value, dict) or not value.get("ok") or not isinstance(value.get("result"), dict):
        raise PodError("orca_contract", "Orca returned an unsupported result")
    runtime = value.get("_meta", {}).get("runtimeId")
    if not isinstance(runtime, str) or not runtime:
        raise PodError("orca_contract", "Orca runtime identity is unavailable")
    return {"runtime": runtime, "result": value["result"]}


def _worker_list_tail(tail: list[str]) -> bool:
    if tail[:2] == ["--limit", "100"]:
        tail = tail[2:]
    if len(tail) >= 2 and tail[0] == "--run":
        if not tail[1] or tail[1].startswith("-"):
            return False
        tail = tail[2:]
    if len(tail) >= 2 and tail[0] == "--cursor":
        if not tail[1] or tail[1].startswith("-") or len(tail[1]) > 4096:
            return False
        tail = tail[2:]
    return not tail


def contract() -> dict:
    try:
        version = read_command(["--version"])
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    return {"status": "observed", "version": version["version"], "executable": version["executable"],
            "worker_identity": "readback_required", "terminal": "optional",
            "billing_preflight": "unverified", "fanout_control": "unverified",
            "effective_launch": "readback_required", "provider_quota": "unavailable"}


def worker_rows(run: str | None = None) -> dict:
    prefix = ["orchestration", "worker-list", "--include-remote", "--json", "--limit", "100"]
    if run:
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
            raise PodError("orca_contract", "Worker fleet page is malformed")
        if runtime is not None and response["runtime"] != runtime:
            raise PodError("orca_runtime_changed", "Runtime changed during fleet read")
        if scope is not None and result.get("scope") != scope:
            raise PodError("orca_scope_changed", "Fleet scope changed during read")
        runtime, scope = response["runtime"], result.get("scope")
        all_rows.extend(rows)
        if not page.get("hasMore"):
            return {"runtime": runtime, "scope": scope, "workers": all_rows,
                    "complete": True, "page_count": len(seen) + 1}
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or cursor in seen:
            raise PodError("orca_pagination", "Worker fleet cursor is missing or repeated")
        seen.add(cursor)
    raise PodError("orca_pagination", "Worker fleet exceeds bounded pages")


def account_metadata() -> dict:
    response = read_command(["account", "list", "--json"])
    rates = response["result"].get("rateLimits", {})
    if not isinstance(rates, dict):
        raise PodError("orca_contract", "Account metadata shape is unsupported")
    out = {}
    for provider in ("codex", "claude"):
        raw = rates.get(provider)
        if not isinstance(raw, dict):
            out[provider] = {"status": "unavailable"}
            continue
        windows = {}
        for name in ("session", "weekly", "fableWeekly"):
            window = raw.get(name)
            if isinstance(window, dict):
                windows[name] = {key: window.get(key) for key in ("usedPercent", "windowMinutes", "resetsAt")
                                 if key in window}
        updated = raw.get("updatedAt")
        age = datetime.now(timezone.utc).timestamp() * 1000 - updated if type(updated) in (int, float) else None
        freshness = "fresh" if age is not None and 0 <= age <= 60_000 else "stale" if age is not None else "unknown"
        out[provider] = {"status": raw.get("status", "unknown"), "updated_at_ms": updated,
                         "freshness": freshness,
                         "windows": windows, "account_association": "unknown",
                         "source": "orca_cached_account_metadata"}
    return {"runtime": response["runtime"], "providers": out}


def require_prelaunch_assurance(contract_snapshot: dict) -> None:
    if contract_snapshot.get("billing_preflight") is not True or contract_snapshot.get("fanout_control") is not True:
        raise PodError("prelaunch_assurance_unverified", "Native billing or hidden fan-out control is unverified")


def effective_launch(requested: dict, observed: dict) -> None:
    launch = observed.get("launch") if isinstance(observed, dict) else None
    if observed.get("state") != "ready":
        raise PodError("native_start_unsettled", "Worker start did not prove ready state")
    if not isinstance(launch, dict) or launch.get("requested") != requested or launch.get("effective") != requested:
        raise PodError("effective_launch_unverified", "Requested and effective route do not match exactly")
    if not all(isinstance(observed.get(key), str) and observed[key] for key in ("runId", "taskId", "dispatchId")):
        raise PodError("native_identity_unverified", "Native Run/Task/Dispatch identity is incomplete")
    # Terminal identity is intentionally optional on current Orca.


def worker_show(dispatch: str) -> dict:
    return read_command(["orchestration", "worker-show", "--dispatch", dispatch, "--json"])

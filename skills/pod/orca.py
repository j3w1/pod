"""Installed-Orca adapter: bounded reads, an exact mutation allowlist, route establishment."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from .errors import PodError
from .util import digest

MAX_OUTPUT = 2_000_000
CAPABILITY_KEYS = {"contract_v1": "orchestration.contract.v1",
                   "launch_preferences_v1": "orchestration.worker-launch-preferences.v1",
                   "fleet_snapshot_v1": "orchestration.federation-fleet-snapshot.v1",
                   "reset_credit_v1": "accounts.codex-reset-credit.v1"}
AGENTS = ("codex", "claude")
TIERS = ("enforceable_control", "runtime_observation", "owner_route_config", "unavailable")
IDENTITY_TWINS = {"taskId": "task_id", "runId": "run_id", "lastFailure": "last_failure",
                  "worktreeId": "worktree_id", "agentTerminalHandle": "agent_terminal_handle",
                  "lastError": "last_error"}
WORKTREE_SELECTOR_PREFIXES = ("path:", "id:", "identity:", "name:", "branch:")


def executable() -> Path:
    selected = os.environ.get("ORCA_CLI_COMMAND")
    if selected:
        found = shutil.which(selected)
    elif os.environ.get("ORCA_DEV_REPO_ROOT"):
        found = shutil.which("orca-dev")
    else:
        found = shutil.which("orca") or shutil.which("orca-ide")
    if not found:
        raise PodError("orca_unavailable", "Configured Orca executable is unavailable")
    path = Path(found).resolve()
    if not path.is_file():
        raise PodError("orca_unavailable", "Orca executable is not a file")
    return path


def _argument(value: object, *, limit: int = 4096) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("-") and len(value) <= limit


def _read_allowed(argv: list[str]) -> bool:
    if argv in (["--version"], ["status", "--json"], ["host", "list", "--json"],
                ["account", "list", "--json"], ["orchestration", "run-list", "--json"],
                ["orchestration", "run-current", "--json"]):
        return True
    if (len(argv) == 5 and argv[:3] == ["orchestration", "worker-show", "--dispatch"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if (len(argv) == 5 and argv[:3] == ["orchestration", "task-list", "--run"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if (len(argv) == 5 and argv[:3] == ["orchestration", "request-show", "--request"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if (len(argv) == 7 and argv[:3] == ["orchestration", "worker-read", "--dispatch"]
            and _argument(argv[3]) and argv[4] == "--limit"
            and argv[5].isdigit() and 1 <= int(argv[5]) <= 200 and argv[6] == "--json"):
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
    runtime = value.get("_meta", {}).get("runtimeId")
    if not isinstance(runtime, str) or not runtime:
        raise PodError("orca_contract", "Orca runtime identity is unavailable")
    return {"runtime": runtime, "result": value["result"]}


def _worker_list_tail(tail: list[str]) -> bool:
    if tail[:2] == ["--limit", "100"]:
        tail = tail[2:]
    if len(tail) >= 2 and tail[0] == "--run":
        if not _argument(tail[1]):
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
    if (len(argv) == 5 and argv[:3] == ["orchestration", "worker-release", "--dispatch"]
            and _argument(argv[3]) and argv[4] == "--json"):
        return True
    if argv[:2] == ["orchestration", "check"]:
        return _check_shape(argv[2:])
    if (len(argv) == 5 and argv[:3] == ["orchestration", "run-create", "--objective"]
            and isinstance(argv[3], str) and argv[3].strip() and len(argv[3]) <= 4096
            and argv[4] == "--json"):
        return True
    if argv[:2] == ["orchestration", "task-create"]:
        return _task_create_shape(argv[2:])
    return False


def _worker_start_shape(tail: list[str]) -> bool:
    expected = ["--task", None, "--run", None, "--worktree", None,
                "--agent", None, "--model", None, "--effort", None, "--json"]
    if len(tail) != len(expected):
        return False
    for index, flag in enumerate(expected):
        if flag is None:
            if not _argument(tail[index]):
                return False
        elif tail[index] != flag:
            return False
    return worktree_selector(tail[5]) is not None


def _check_shape(tail: list[str]) -> bool:
    if len(tail) < 2 or tail[0] != "--run" or not _argument(tail[1]):
        return False
    rest = tail[2:]
    if rest[:1] == ["--ack"]:
        if len(rest) < 2 or not _argument(rest[1]):
            return False
        rest = rest[2:]
    if rest[:1] == ["--wait"]:
        rest = rest[1:]
        if rest[:1] == ["--types"]:
            if len(rest) < 2 or not _argument(rest[1]):
                return False
            rest = rest[2:]
        if rest[:1] != ["--timeout-ms"]:
            return False
        if len(rest) < 2 or not rest[1].isdigit() or not 1000 <= int(rest[1]) <= 900_000:
            return False
        rest = rest[2:]
    return rest == ["--json"]


def _task_create_shape(tail: list[str]) -> bool:
    if len(tail) < 2 or tail[0] != "--run" or not _argument(tail[1]):
        return False
    rest = tail[2:]
    if rest[:1] != ["--spec"] or len(rest) < 2 or not isinstance(rest[1], str) or not rest[1].strip():
        return False
    rest = rest[2:]
    if rest[:1] == ["--task-title"]:
        if len(rest) < 2 or not isinstance(rest[1], str) or not rest[1].strip():
            return False
        rest = rest[2:]
    return rest == ["--json"]


def worktree_selector(value: object) -> str | None:
    """Accept an existing-worktree selector only; creation modes are refused here."""
    if value == "current":
        return "current"
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value in ("new-child", "new-top-level"):
        return None
    for prefix in WORKTREE_SELECTOR_PREFIXES:
        if value.startswith(prefix) and len(value) > len(prefix):
            return value
    return None


def mutate_command(argv: list[str], *, timeout: int = 120, accept_exit: tuple[int, ...] = (0,)) -> dict:
    """Run one allowlisted state-changing Orca command and return its exact receipt."""
    if not _mutate_allowed(argv):
        raise PodError("unsupported_orca_mutation", "Orca adapter accepts only known mutations")
    command = [str(executable()), *argv]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("native_effect_uncertain", "Native response is unavailable") from exc
    if completed.returncode not in accept_exit:
        raise PodError("native_effect_uncertain", "Native command returned an unsupported exit status")
    if len(completed.stdout) > MAX_OUTPUT:
        raise PodError("orca_contract", "Native response exceeds bounded output")
    envelope = _envelope(completed.stdout)
    return {"runtime": envelope["runtime"], "exit": completed.returncode, "result": envelope["result"]}


def identity(mapping: object, camel: str) -> object:
    """Read one identity field across the known camel/snake spellings, refusing conflicts."""
    if not isinstance(mapping, dict):
        return None
    snake = IDENTITY_TWINS.get(camel)
    if snake is None:
        return mapping.get(camel)
    if camel in mapping and snake in mapping and mapping[camel] != mapping[snake]:
        raise PodError("orca_contract", f"Conflicting {camel} spellings in one native record")
    return mapping.get(camel, mapping.get(snake))


def contract() -> dict:
    """What the installed runtime is and which orchestration contracts it advertises."""
    try:
        version = read_command(["--version"])
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code}
    snapshot = {"status": "observed", "version": version["version"],
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
    snapshot["capabilities"] = {key: value in advertised for key, value in CAPABILITY_KEYS.items()}
    snapshot["runtime_state"] = runtime.get("state")
    return snapshot


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


def hosts() -> dict:
    """Bounded host inventory; a fleet beyond this machine is a disclosed observation."""
    response = read_command(["host", "list", "--json"])
    rows = response["result"].get("hosts")
    rows = rows if isinstance(rows, list) else []
    names = [row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    return {"runtime": response["runtime"], "hosts": names,
            "local_only": names in ([], ["local"])}


def account_metadata() -> dict:
    """Redacted provider rate-limit projection. Account identifiers never leave this call."""
    raw = account_metadata_raw()
    out = {}
    for provider in AGENTS:
        block = raw["providers"].get(provider, {})
        out[provider] = {key: block[key] for key in
                         ("status", "updated_at_ms", "freshness", "windows", "account_association", "source")
                         if key in block}
    return {"runtime": raw["runtime"], "providers": out}


def account_metadata_raw() -> dict:
    """Provider metadata including a non-reversible account digest, for establishment only."""
    response = read_command(["account", "list", "--json"])
    result = response["result"]
    rates = result.get("rateLimits", {})
    if not isinstance(rates, dict):
        raise PodError("orca_contract", "Account metadata shape is unsupported")
    out = {}
    for provider in AGENTS:
        raw = rates.get(provider)
        provider_block = result.get(provider) if isinstance(result.get(provider), dict) else {}
        managed = provider_block.get("accounts")
        managed = managed if isinstance(managed, list) else []
        default = provider_block.get("systemDefault")
        default = default if isinstance(default, dict) else {}
        active = provider_block.get("activeAccountId")
        record = {"managed_accounts": len(managed),
                  "active_account": digest(active) if isinstance(active, str) and active else None,
                  "default_identity": digest(default.get("providerAccountId"))
                  if isinstance(default.get("providerAccountId"), str) else None,
                  "default_auth": default.get("authKind") if isinstance(default.get("authKind"), str) else None,
                  "default_has_auth": bool(default.get("hasAuth")),
                  "reset_credits": None}
        credits = raw.get("rateLimitResetCredits") if isinstance(raw, dict) else None
        if isinstance(credits, dict) and isinstance(credits.get("availableCount"), int):
            record["reset_credits"] = credits["availableCount"]
        if not isinstance(raw, dict):
            out[provider] = {**record, "status": "unavailable", "windows": {},
                             "account_association": "unknown", "source": "orca_cached_account_metadata"}
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
        out[provider] = {**record, "status": raw.get("status", "unknown"), "updated_at_ms": updated,
                         "freshness": freshness, "windows": windows,
                         "account_association": "managed" if managed else "host_login",
                         "source": "orca_cached_account_metadata"}
    return {"runtime": response["runtime"], "providers": out}


def agent_login_mode(agent: str) -> dict:
    """Observe one agent's non-secret authentication mode. No credential is read or stored."""
    if agent not in AGENTS:
        raise PodError("unsupported_agent", "Only the advertised worker agents can be probed")
    command = "codex" if agent == "codex" else "claude"
    found = shutil.which(command)
    if not found:
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_executable_absent"}
    argv = [found, "login", "status"] if agent == "codex" else [found, "auth", "status", "--json"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_probe_failed"}
    if completed.returncode or len(completed.stdout) > 64 * 1024:
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_probe_unreadable"}
    if agent == "codex":
        text = completed.stdout.strip().lower()
        if "api key" in text:
            return {"auth": "api_key", "subscription": False, "identity_digest": None}
        if "logged in" in text:
            return {"auth": "oauth", "subscription": True, "identity_digest": None}
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_login_unrecognised"}
    try:
        value = json.loads(completed.stdout)
    except ValueError:
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_probe_unreadable"}
    if not isinstance(value, dict) or not value.get("loggedIn"):
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_not_logged_in"}
    method, provider = value.get("authMethod"), value.get("apiProvider")
    org = value.get("orgId")
    stamp = digest(org) if isinstance(org, str) and org else None
    if method == "claude.ai" and provider == "firstParty":
        return {"auth": "oauth", "subscription": True, "identity_digest": stamp}
    if isinstance(method, str) and "api" in method.lower():
        return {"auth": "api_key", "subscription": False, "identity_digest": stamp}
    return {"auth": "unknown", "subscription": None, "identity_digest": stamp,
            "reason": "agent_login_unrecognised"}


def bucket_for(agent: str, model: str) -> str:
    """The quota window family a route draws on, as Pod labels it."""
    if agent == "claude" and isinstance(model, str) and model.lower().startswith("fable"):
        return "fable"
    return "default"


def _tier(name: str, tier: str, source: str, **extra: object) -> dict:
    if tier not in TIERS:
        raise PodError("invalid_establishment", f"Unsupported control tier for {name}")
    return {"tier": tier, "source": source, **extra}


def route_establishment(route: dict, model_policy: dict, *, snapshot: dict | None = None,
                        accounts: dict | None = None, login: dict | None = None,
                        fleet: dict | None = None, child_delegation: bool = False) -> dict:
    """Describe exactly which controls establish one route, and at what strength."""
    observed = snapshot if snapshot is not None else contract()
    if observed.get("status") != "observed" or not isinstance(observed.get("runtime"), str):
        return {"schema": "pod-route-establishment/v1", "runtime": None,
                "version": observed.get("version"), "executable": observed.get("executable"),
                "route": dict(route), "controls": {}, "login": {}, "billing": {},
                "hard_stops": ["native_authority_unverified"],
                "disclosures": ["installed Orca runtime identity is unavailable"]}
    agent = route.get("agent")
    account_block = (accounts or account_metadata_raw())["providers"].get(agent, {})
    login_block = login if login is not None else agent_login_mode(agent)
    host_block = fleet if fleet is not None else hosts()
    capabilities = observed.get("capabilities", {})
    bucket = route.get("bucket") or bucket_for(agent, route.get("model", ""))
    managed = account_block.get("managed_accounts", 0)
    mode = "managed_account" if managed else "host_login"
    stamp = account_block.get("active_account") or account_block.get("default_identity") \
        or login_block.get("identity_digest")
    auth = login_block.get("auth", "unknown")
    if auth == "unknown" and account_block.get("default_auth") == "oauth" and account_block.get("default_has_auth"):
        auth, subscription = "oauth", True
    else:
        subscription = login_block.get("subscription")
    observed_billing = "subscription" if auth == "oauth" and subscription else (
        "api" if auth == "api_key" else "unknown")
    approved_billing = model_policy.get("billing") if isinstance(model_policy, dict) else None
    windows = account_block.get("windows", {})
    controls = {
        "effective_launch": _tier("effective_launch",
                                  "enforceable_control" if capabilities.get("launch_preferences_v1")
                                  else "runtime_observation",
                                  "worker-launch-preferences with startOptions.launch readback"),
        "descendant_depth": _tier("descendant_depth",
                                  "enforceable_control" if capabilities.get("contract_v1")
                                  else "unavailable",
                                  "native nested-worker depth limit"),
        "descendant_count": _tier("descendant_count", "runtime_observation",
                                  "worker-list projection.parent and worker-show creatorDispatchId"),
        "child_delegation": _tier("child_delegation", "owner_route_config",
                                  "policy child_delegation with packet action refusal",
                                  enabled=bool(child_delegation),
                                  note="behavioural policy enforced by Pod admission, not a provider sandbox"),
        "billing_mode": _tier("billing_mode",
                              "runtime_observation" if observed_billing != "unknown" else "unavailable",
                              "managed account metadata and the agent's own non-secret login mode",
                              observed=observed_billing),
        "account_identity": _tier("account_identity",
                                  "runtime_observation" if stamp else "unavailable",
                                  "digest of the provider account or organisation identifier"),
        "route_approval": _tier("route_approval", "owner_route_config",
                                "personal policy approval provenance",
                                billing=approved_billing),
        "quota_bucket": _tier("quota_bucket",
                              "runtime_observation" if windows else "unavailable",
                              "provider rate-limit windows reported by Orca", bucket=bucket),
        "cross_host": _tier("cross_host", "runtime_observation", "host inventory",
                            local_only=bool(host_block.get("local_only", True))),
    }
    hard_stops = []
    disclosures = []
    if approved_billing == "included" and observed_billing != "subscription":
        hard_stops.append("billing_mode_unverified")
    if observed_billing == "api" and approved_billing != "paid":
        hard_stops.append("paid_route_forbidden")
    if not windows:
        disclosures.append("provider quota windows are unavailable to the installed runtime")
    if controls["quota_bucket"]["tier"] == "runtime_observation" and "bucket" not in account_block:
        disclosures.append("quota bucket is a Pod label, not a provider-issued identifier")
    if controls["descendant_depth"]["tier"] == "unavailable":
        disclosures.append("installed runtime advertises no nested-worker depth control")
    if not child_delegation:
        disclosures.append("worker-initiated delegation is refused by Pod admission, not by a provider sandbox")
    if not host_block.get("local_only", True):
        disclosures.append("the fleet reaches beyond this host; cross-host admission is not atomic")
    return {"schema": "pod-route-establishment/v1", "runtime": observed["runtime"],
            "version": observed.get("version"), "executable": observed.get("executable"),
            "route": {"agent": agent, "model": route.get("model"), "account": route.get("account"),
                      "bucket": bucket, "effort": route.get("effort")},
            "controls": controls,
            "login": {"mode": mode, "auth": auth, "subscription": subscription,
                      "managed_accounts": managed, "identity_digest": stamp},
            "billing": {"observed": observed_billing, "approved": approved_billing},
            "hard_stops": hard_stops, "disclosures": disclosures}


def require_route_establishment(establishment: dict, route: dict) -> None:
    """Refuse a launch whose route is not established by an actual control."""
    if not isinstance(establishment, dict) or establishment.get("schema") != "pod-route-establishment/v1":
        raise PodError("route_establishment_missing", "Native route establishment is absent")
    if not isinstance(establishment.get("runtime"), str) or not establishment["runtime"]:
        raise PodError("native_authority_unverified", "Installed Orca runtime identity is unproven")
    established = establishment.get("route", {})
    for field in ("agent", "model", "account"):
        if not isinstance(route.get(field), str) or established.get(field) != route.get(field):
            raise PodError("account_binding_unverified", "Established route differs from the requested route")
    if route.get("bucket") is not None and established.get("bucket") != route["bucket"]:
        raise PodError("account_binding_unverified", "Established quota bucket differs from the requested one")
    for stop in establishment.get("hard_stops", []):
        raise PodError(stop if stop in ("billing_mode_unverified", "paid_route_forbidden",
                                        "native_authority_unverified") else "route_establishment_failed",
                       "Route establishment refuses this launch: " + str(stop))


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

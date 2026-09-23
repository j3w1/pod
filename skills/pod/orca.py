"""Installed-Orca adapter: bounded reads, an exact mutation allowlist, route establishment."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .errors import PodError
from .util import digest

MAX_OUTPUT = 2_000_000
CAPABILITY_KEYS = {"contract_v1": "orchestration.contract.v1",
                   "launch_preferences_v1": "orchestration.worker-launch-preferences.v1",
                   "reset_credit_v1": "accounts.codex-reset-credit.v1"}
AGENTS = ("codex", "claude")
TIERS = ("enforceable_control", "runtime_observation", "owner_route_config", "unavailable")
IDENTITY_TWINS = {"dispatchId": "dispatch_id", "taskId": "task_id", "runId": "run_id",
                  "lastFailure": "last_failure",
                  "worktreeId": "worktree_id", "agentTerminalHandle": "agent_terminal_handle",
                  "lastError": "last_error"}
WORKTREE_SELECTOR_PREFIXES = ("path:", "id:", "identity:", "name:", "branch:")


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
    if argv in (["--version"], ["status", "--json"], ["host", "list", "--json"],
                ["account", "list", "--json"],
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
    mutation = value["result"].get("mutation")
    if not isinstance(mutation, dict):
        mutation = value.get("mutation")
    request_uuid = mutation.get("requestId") if isinstance(mutation, dict) else None
    return {"runtime": runtime, "result": value["result"], "request_uuid": request_uuid}


def _mutation_envelope(stdout: str) -> dict:
    """Decode a mutation receipt, including Orca's authoritative no-start refusal."""
    try:
        value = json.loads(stdout)
    except ValueError as exc:
        raise PodError("native_effect_uncertain", "Native response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PodError("native_effect_uncertain", "Native response is malformed")
    runtime = value.get("_meta", {}).get("runtimeId")
    if not isinstance(runtime, str) or not runtime:
        raise PodError("native_effect_uncertain", "Native response has no runtime identity")
    if value.get("ok") is True and isinstance(value.get("result"), dict):
        result = value["result"]
        error = None
    elif value.get("ok") is False and isinstance(value.get("error"), dict):
        error = value["error"]
        if error.get("code") != "capacity_full":
            raise PodError("native_effect_uncertain", "Native refusal is not a supported no-start result")
        native_result = value.get("result")
        if native_result is not None and not isinstance(native_result, dict):
            raise PodError("native_effect_uncertain", "Native refusal carries a malformed result")
        result = dict(native_result or {})
        if "error" in result:
            result["_result_error"] = result["error"]
        result.setdefault("state", "deferred")
        result["error"] = error
        for key in ("dispatchId", "dispatch_id", "workerId", "worker_id",
                    "residualResources", "residual_resources", "effects",
                    "terminal", "terminalHandle", "terminal_handle",
                    "worktreeId", "worktree_id", "terminalResourceId",
                    "terminal_resource_id", "resource", "failedStage", "failed_stage"):
            if key not in value:
                continue
            if key in result and result[key] != value[key]:
                result.setdefault("_envelope_conflicts", {})[key] = value[key]
            else:
                result[key] = value[key]
    else:
        raise PodError("native_effect_uncertain", "Native response does not prove an effect or refusal")
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
    if (request_malformed
            or any(reference != request_uuid for reference in request_references[1:])):
        result["_request_conflict"] = True
    return {"runtime": runtime, "result": result, "request_uuid": request_uuid,
            "error": error}


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
    return worktree_selector(tail[5]) is not None and (retry is None or _argument(retry))


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
    envelope = _mutation_envelope(completed.stdout)
    return {"runtime": envelope["runtime"], "exit": completed.returncode,
            "result": envelope["result"], "request_uuid": envelope.get("request_uuid"),
            "error": envelope.get("error")}


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


def hosts() -> dict:
    """Bounded host inventory; a fleet beyond this machine is a disclosed observation."""
    response = read_command(["host", "list", "--json"])
    rows = response["result"].get("hosts")
    rows = rows if isinstance(rows, list) else []
    names = [row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    return {"runtime": response["runtime"], "hosts": names,
            "local_only": names in ([], ["local"])}


def account_metadata(raw: dict | None = None) -> dict:
    """Redacted provider rate-limit projection. Account identifiers never leave this call."""
    raw = raw or account_metadata_raw()
    out = {}
    for provider in AGENTS:
        block = raw["providers"].get(provider, {})
        out[provider] = {key: block[key] for key in
                         ("status", "updated_at_ms", "freshness", "windows", "account_association",
                          "identity_digest", "source")
                         if key in block}
    return {"runtime": raw["runtime"], "providers": out}


def selected_account_identity(account_block: dict, login_block: dict) -> dict:
    """Choose the one redacted identity that approval, accounting and grants share."""
    if account_block.get("managed_accounts"):
        value = account_block.get("active_account")
        source = "orca_active_managed_account"
    else:
        default_present = account_block.get("default_present")
        if default_present is None:
            default_present = any(account_block.get(key) is not None
                                  for key in ("default_identity", "default_auth")) \
                or account_block.get("default_has_auth") is True
        if default_present:
            value = account_block.get("default_identity")
            source = "orca_system_default"
        else:
            value = login_block.get("identity_digest")
            source = "agent_login_status"
    return {"identity_digest": value if isinstance(value, str) and value else None,
            "source": source if value else "unavailable"}


def selected_account_evidence(account_block: dict, login_block: dict) -> dict:
    """Join identity and billing proof from one selected account context."""
    identity = selected_account_identity(account_block, login_block)
    stamp, source = identity["identity_digest"], identity["source"]
    auth = "unknown"
    subscription = None
    proof_source = "unavailable"
    context_matched = False
    if source == "orca_active_managed_account":
        if account_block.get("selected_has_auth"):
            auth = account_block.get("selected_auth", "unknown")
            proof_source = source
            context_matched = auth in ("oauth", "api_key")
    elif source == "orca_system_default":
        if account_block.get("default_has_auth"):
            auth = account_block.get("default_auth", "unknown")
            proof_source = source
            context_matched = auth in ("oauth", "api_key")
    elif source == "agent_login_status" and login_block.get("identity_digest") == stamp:
        auth = login_block.get("auth", "unknown")
        subscription = login_block.get("subscription")
        proof_source = source
        context_matched = auth in ("oauth", "api_key")
    if auth == "oauth":
        if proof_source.startswith("orca_"):
            subscription = True
        elif subscription is not True:
            context_matched = False
            subscription = None
    elif auth == "api_key":
        subscription = False
    else:
        auth = "unknown"
        subscription = None
        context_matched = False
    billing = ("subscription" if context_matched and auth == "oauth" and subscription is True
               else "api" if context_matched and auth == "api_key" else "unknown")
    return {**identity, "auth": auth, "subscription": subscription,
            "billing": billing, "proof_source": proof_source,
            "context_matched": context_matched}


def account_evidence_stops(evidence: dict, *, expected_identity: object,
                           approved_billing: object) -> list[str]:
    """Apply one fail-closed identity/billing policy to every pre-effect account read."""
    observed = evidence.get("identity_digest")
    if (not isinstance(expected_identity, str) or not isinstance(observed, str)
            or observed != expected_identity):
        return ["account_binding_unverified"]
    if evidence.get("context_matched") is not True or evidence.get("billing") == "unknown":
        return ["billing_mode_unverified"]
    stops = []
    if approved_billing == "included" and evidence["billing"] != "subscription":
        stops.append("billing_mode_unverified")
    if evidence["billing"] == "api" and approved_billing != "paid":
        stops.append("paid_route_forbidden")
    return stops


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
        default_value = provider_block.get("systemDefault")
        default_present = isinstance(default_value, dict)
        default = default_value if default_present else {}
        active = provider_block.get("activeAccountId")
        selected = [row for row in managed
                    if isinstance(row, dict) and row.get("id") == active]
        selected = selected[0] if len(selected) == 1 else {}
        record = {"managed_accounts": len(managed),
                  "active_account": digest(active) if isinstance(active, str) and active else None,
                  "selected_auth": selected.get("authKind")
                  if isinstance(selected.get("authKind"), str) else None,
                  "selected_has_auth": bool(selected.get("hasAuth")),
                  "default_present": default_present,
                  "default_identity": digest(default.get("providerAccountId"))
                  if isinstance(default.get("providerAccountId"), str) else None,
                  "default_auth": default.get("authKind") if isinstance(default.get("authKind"), str) else None,
                  "default_has_auth": bool(default.get("hasAuth")),
                  "reset_credits": None}
        record["identity_digest"] = selected_account_identity(record, {})["identity_digest"]
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
    # Codex prints its login line on stderr; Claude answers on stdout. Read both.
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode or len(output) > 64 * 1024:
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_probe_unreadable"}
    if agent == "codex":
        text = output.strip().lower()
        # "logged in" is a substring of "not logged in", so the negative is checked first
        # and the positive is anchored. Reading this wrong would report an absent login as
        # an approved subscription and silently defeat the billing hard stop.
        if re.search(r"\bnot logged in\b|\bnot signed in\b|\blogged out\b", text):
            return {"auth": "unknown", "subscription": None, "identity_digest": None,
                    "reason": "agent_not_logged_in"}
        if re.search(r"\bapi key\b", text):
            return {"auth": "api_key", "subscription": False, "identity_digest": None}
        if re.match(r"^logged in\b", text):
            return {"auth": "oauth", "subscription": True, "identity_digest": None}
        return {"auth": "unknown", "subscription": None, "identity_digest": None,
                "reason": "agent_login_unrecognised"}
    try:
        value = json.loads(completed.stdout or completed.stderr)
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
    if agent == "claude" and model == "claude-fable-5-1":
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
    account_snapshot = accounts or account_metadata_raw()
    account_block = account_snapshot.get("providers", {}).get(agent, {})
    login_block = login if login is not None else agent_login_mode(agent)
    host_block = fleet if fleet is not None else hosts()
    capabilities = observed.get("capabilities", {})
    # Derived from the route's own agent and model. A caller cannot assert this and then
    # have the record present its assertion back as a runtime observation.
    bucket = bucket_for(agent, route.get("model", ""))
    managed = account_block.get("managed_accounts", 0)
    mode = "managed_account" if managed else "host_login"
    account_evidence = selected_account_evidence(account_block, login_block)
    stamp = account_evidence["identity_digest"]
    expected_stamp = route.get("account")
    auth = account_evidence["auth"]
    subscription = account_evidence["subscription"]
    observed_billing = account_evidence["billing"]
    approved_billing = model_policy.get("billing") if isinstance(model_policy, dict) else None
    windows = account_block.get("windows", {})
    controls = {
        "effective_launch": _tier("effective_launch",
                                  "enforceable_control" if capabilities.get("launch_preferences_v1")
                                  else "runtime_observation",
                                  "Orca applies the requested launch preferences; Pod refuses "
                                  "the effect unless a readback shows requested equals effective"),
        "descendant_depth": _tier("descendant_depth",
                                  "enforceable_control" if capabilities.get("contract_v1")
                                  else "unavailable",
                                  "Orca refuses a nested worker past its own depth limit; Pod "
                                  "observes the depth and does not set the limit",
                                  limit="unknown_to_pod"),
        "descendant_count": _tier("descendant_count", "owner_route_config",
                                  "objective-local Pod admissions for delegated assignments"),
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
                                  "digest of the active provider account or organisation identifier",
                                  matched=bool(stamp and expected_stamp and stamp == expected_stamp)),
        "route_approval": _tier("route_approval", "owner_route_config",
                                "personal policy approval provenance",
                                billing=approved_billing),
        "context_window": _tier(
            "context_window", "unavailable",
            "installed Orca worker-start exposes model and effort but no per-worker context control",
            requested=route.get("context"), effective=None),
        "quota_bucket": _tier("quota_bucket",
                              "runtime_observation" if windows else "unavailable",
                              "provider rate-limit windows reported by Orca", bucket=bucket),
        "cross_host": _tier("cross_host", "runtime_observation", "host inventory",
                            local_only=bool(host_block.get("local_only", True))),
        "physical_capacity": _tier("physical_capacity", "unavailable",
                                   "Orca owns placement and runtime capacity; Pod reads no capacity census"),
    }
    hard_stops = []
    disclosures = []
    if account_snapshot.get("runtime") != observed["runtime"]:
        hard_stops.append("native_authority_unverified")
    hard_stops.extend(account_evidence_stops(
        account_evidence, expected_identity=expected_stamp,
        approved_billing=approved_billing))
    if route.get("context") in ("256k", "max"):
        hard_stops.append("context_control_unavailable")
        disclosures.append("requested context cannot be established by this Orca worker-start contract")
    if not windows:
        disclosures.append("provider quota windows are unavailable to the installed runtime")
    if controls["quota_bucket"]["tier"] == "runtime_observation" and "bucket" not in account_block:
        disclosures.append("quota bucket is a Pod label, not a provider-issued identifier")
    if controls["descendant_depth"]["tier"] == "unavailable":
        disclosures.append("installed runtime advertises no nested-worker depth control")
    else:
        disclosures.append("the runtime enforces a nested-worker depth limit whose value Pod "
                           "cannot read; Pod requires a logical admission for each delegated assignment")
    if not child_delegation:
        disclosures.append("worker-initiated delegation is refused by Pod admission, not by a provider sandbox")
    if not host_block.get("local_only", True):
        disclosures.append("the runtime reports more than one host; Pod does not infer capacity from it")
    disclosures.append("physical worker capacity is unavailable to Pod and enforced by Orca/the host")
    return {"schema": "pod-route-establishment/v1", "runtime": observed["runtime"],
            "version": observed.get("version"), "executable": observed.get("executable"),
            "route": {"agent": agent, "model": route.get("model"), "account": stamp,
                      "bucket": bucket,
                      "effort": route.get("effort"), "context": route.get("context"),
                      "effective_context": None},
            "controls": controls,
            "login": {"mode": mode, "auth": auth, "subscription": subscription,
                      "managed_accounts": managed, "identity_digest": stamp,
                      "proof_source": account_evidence["proof_source"]},
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
    if route.get("context") is not None:
        context_control = establishment.get("controls", {}).get("context_window")
        if (established.get("context") != route.get("context")
                or established.get("effective_context") != route.get("effective_context")
                or not isinstance(context_control, dict)
                or context_control.get("tier") != "enforceable_control"):
            raise PodError("context_control_unavailable",
                           "Installed Orca cannot establish the requested worker context")
    identity_control = establishment.get("controls", {}).get("account_identity")
    expected_identity = route.get("account")
    observed_identity = establishment.get("login", {}).get("identity_digest")
    if (not isinstance(expected_identity, str) or not isinstance(observed_identity, str)
            or expected_identity != observed_identity or not isinstance(identity_control, dict)
            or identity_control.get("tier") != "runtime_observation"
            or identity_control.get("matched") is not True):
        raise PodError("account_binding_unverified",
                       "Native account identity does not match personal route approval")
    for stop in establishment.get("hard_stops", []):
        raise PodError(stop if stop in ("billing_mode_unverified", "paid_route_forbidden",
                                        "context_control_unavailable",
                                        "native_authority_unverified", "account_binding_unverified")
                       else "route_establishment_failed",
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

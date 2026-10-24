"""One personal YAML preference authority and restrictive Governor project policy.

Personal preferences use `pod/v2`: explicit exact-route states, one optional Preferred route,
one optional Pinned route, the worker ceiling and the automatic-refresh setting. The project
file keeps `pod/v1` with restrictive `waste_governor` policy only. A `pod/v1` personal file
from 0.6.x is read as setup required: no route is eligible until its explicit setup is saved.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Iterator

import yaml

from .catalog import by_id, route_keys, supported_route
from .contracts import ACTION_KINDS, EFFECT_PREFIXES, bound
from .errors import PodError
from .util import digest, exact, explicit_home, native_home

SCHEMA = "pod/v2"
PROJECT_SCHEMA = "pod/v1"
V1_SCHEMA = "pod/v1"
ROUTE_STATES = ("enabled", "disabled")
REFRESH_SETTINGS = ("automatic", "manual")
DEFAULT_WORKER_CAPACITY = 2
PERSONAL_FIELDS = {"schema", "routes", "preferred", "pinned", "workers", "refresh", "waste_governor",
                   "authorization_verifier", "decision_policy", "decision_generation",
                   "tool_permissions", "pending_decisions"}
# The 0.6.x personal shape, read only to explain and convert it.
V1_IDS = ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5",
          "gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
V1_STATES = ("available", "preferred", "disabled")
V1_MODES = ("custom", "all")
REPLACED = {"claude-sonnet-5": "claude-sonnet-5-5", "gpt-6-sol": "gpt-6.1-sol"}
SETUP_SCHEMA = "pod-route-setup/v1"
BACKUP_SUFFIX = ".pod-v1"
SETUP_ACTION = "open pod in a terminal and confirm the route setup"
GOVERNED_KINDS = ACTION_KINDS
TRIGGER_KINDS = ("push", "pr_update")
WASTE_GOVERNOR_FIELDS = {"mode", "cancel_superseded_validation", "preflight", "triggers",
                         "transient_retries", "host_control", "exceptions"}
DEFAULT_GOVERNOR = {"mode": "enforce", "cancel_superseded_validation": True,
                    "preflight": [], "triggers": {}, "transient_retries": 1, "exceptions": []}
KEEP = object()
DECISION_FIELDS = ("decision_policy", "tool_permissions", "pending_decisions", "decision_generation")


def defaults() -> dict:
    """A fresh installation's explicit first-install authorization of the shipped routes."""
    return {"schema": SCHEMA, "routes": {key: "enabled" for key in route_keys()},
            "preferred": None, "pinned": None,
            "workers": {"max_active": DEFAULT_WORKER_CAPACITY}, "refresh": "automatic"}


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise PodError("yaml_alias_unsupported", "YAML aliases are not supported")
        node = super().compose_node(parent, index)
        if not str(node.tag).startswith("tag:yaml.org,2002:"):
            raise PodError("yaml_tag_unsupported", "Custom YAML tags are not supported")
        return node

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise PodError("yaml_duplicate_or_key", "YAML keys must be unique strings")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _shape(value: Any, depth: int = 0, counter: list[int] | None = None) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > 2048 or depth > 12:
        raise PodError("yaml_resource_limit", "Configuration structure exceeds limits")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise PodError("yaml_invalid_key", "Configuration key is invalid")
            _shape(item, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            _shape(item, depth + 1, counter)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise PodError("yaml_invalid_type", "Configuration contains an unsupported value")
    elif isinstance(value, str) and len(value) > 4096:
        raise PodError("yaml_resource_limit", "Configuration scalar exceeds limit")


def _read_bytes(path: Path) -> bytes | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PodError("unsafe_config", "Configuration availability cannot be proven") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise PodError("unsafe_config", "Configuration must be a bounded regular file")
        data = os.read(fd, 64 * 1024 + 1)
        if len(data) != info.st_size:
            raise PodError("unsafe_config", "Configuration changed while it was read")
        return data
    finally:
        os.close(fd)


def _diagnostic(value: Any, name: str) -> str | None:
    """Bounded display data only; never a validated routing preference."""
    if value is None:
        return None
    if isinstance(value, str):
        from .term import clean
        text = clean(value)
        return text[:125] + "..." if len(text) > 128 else text or f"<empty {name}>"
    return f"<invalid {type(value).__name__} {name}>"


def _decode(data: bytes) -> Any:
    # The same bytes always decode to the same document; each caller gets its own copy.
    return deepcopy(_decoded(data))


@lru_cache(maxsize=32)
def _decoded(data: bytes) -> Any:
    try:
        value = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)
    except PodError:
        raise
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise PodError("invalid_yaml", f"Configuration cannot be safely decoded{where}") from exc
    _shape(value)
    return value


def _parse(data: bytes | None, *, project: bool = False, diagnostic: dict | None = None) -> dict | None:
    if data is None:
        return None
    value = _decode(data)
    if diagnostic is not None and isinstance(value, dict) and not project:
        if value.get("schema") == V1_SCHEMA:
            diagnostic["pinned"] = _diagnostic(value.get("pinned_model"), "pin")
        else:
            diagnostic["pinned"] = _diagnostic(value.get("pinned"), "pin")
            diagnostic["preferred"] = _diagnostic(value.get("preferred"), "preference")
    if not project and isinstance(value, dict) and value.get("schema") == V1_SCHEMA:
        older = _validate_v1(value)
        if diagnostic is not None:
            diagnostic["v1"] = older
    return validate(value, project=project)


def read_yaml(path: Path, *, project: bool = False) -> dict | None:
    return _parse(_read_bytes(path), project=project)


def _strings(value: Any, name: str) -> list[str]:
    if (not isinstance(value, list) or len(value) > 64
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in value)
            or len(set(value)) != len(value)):
        raise PodError("invalid_config", f"{name} must be a bounded unique string list")
    return value


def validate_effects(value: Any, *, name: str = "effects") -> list[str]:
    if (not isinstance(value, list) or len(value) > bound("action", "effects", "max_items")
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)):
        raise PodError("invalid_" + name, f"{name} must be a bounded unique list")
    for item in value:
        if (not isinstance(item, str) or not item.startswith(EFFECT_PREFIXES)
                or len(item) > bound("action", "effects", "items")["max_length"] or not item.split(":", 1)[1]):
            raise PodError("invalid_" + name, f"{name} entries name a downstream effect")
    return value


def _validate_waste_governor(value: Any) -> dict:
    wg = exact(value, WASTE_GOVERNOR_FIELDS, name="waste_governor")
    if "mode" in wg and wg["mode"] not in ("enforce", "observe"):
        raise PodError("invalid_config", "waste_governor.mode must be enforce or observe")
    if "cancel_superseded_validation" in wg and not isinstance(wg["cancel_superseded_validation"], bool):
        raise PodError("invalid_config", "waste_governor.cancel_superseded_validation must be boolean")
    if "preflight" in wg:
        _strings(wg["preflight"], "preflight")
    if "triggers" in wg:
        if not isinstance(wg["triggers"], dict) or set(wg["triggers"]) - set(TRIGGER_KINDS):
            raise PodError("invalid_config", "waste_governor.triggers maps push and pr_update only")
        for kind, effects in wg["triggers"].items():
            validate_effects(effects, name=f"triggers.{kind}")
    if "transient_retries" in wg and (type(wg["transient_retries"]) is not int
                                      or not 0 <= wg["transient_retries"] <= 3):
        raise PodError("invalid_config", "waste_governor.transient_retries must be 0 through 3")
    if "host_control" in wg and (not isinstance(wg["host_control"], str)
                                 or not wg["host_control"].strip() or len(wg["host_control"]) > 512):
        raise PodError("invalid_config", "waste_governor.host_control needs a host policy reference")
    if "exceptions" in wg:
        if not isinstance(wg["exceptions"], list) or len(wg["exceptions"]) > 32:
            raise PodError("invalid_config", "waste_governor.exceptions must be bounded")
        ids = []
        for exception in wg["exceptions"]:
            row = exact(exception, {"id", "action", "objective", "unit", "kinds", "candidate",
                                    "reason", "valid_until"},
                        {"id", "action", "objective", "kinds", "reason", "valid_until"},
                        name="efficiency_exception")
            for field in ("id", "objective", "reason", "valid_until"):
                if not isinstance(row[field], str) or not row[field] or len(row[field]) > 512:
                    raise PodError("invalid_config", "Efficiency exception has invalid text")
            for field in ("unit", "candidate"):
                if field in row and (not isinstance(row[field], str) or not row[field]
                                     or len(row[field]) > 256):
                    raise PodError("invalid_config", "Efficiency exception binding is invalid")
            if row["action"] != "efficiency_exception":
                raise PodError("invalid_config", "Unsupported efficiency exception action")
            if (not isinstance(row["kinds"], list) or not row["kinds"]
                    or len(row["kinds"]) > 8 or any(kind not in GOVERNED_KINDS for kind in row["kinds"])):
                raise PodError("invalid_config", "Efficiency exception kinds are invalid")
            try:
                expiry = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise PodError("invalid_config", "Efficiency exception validity is invalid") from exc
            if expiry.tzinfo is None:
                raise PodError("invalid_config", "Efficiency exception validity needs a timezone")
            ids.append(row["id"])
        if len(ids) != len(set(ids)):
            raise PodError("invalid_config", "Efficiency exception ids must be unique")
    return wg


def _state_name(state: str | None) -> str:
    return "Disabled" if state == "disabled" else "Not set"


def _validate_v1(value: Any) -> dict:
    """The 0.6.x personal shape; valid files are kept and read as setup required."""
    if isinstance(value, dict) and isinstance(value.get("models"), dict):
        for model_id in value["models"]:
            if model_id not in V1_IDS:
                raise PodError("invalid_config", f"models.{model_id} is not a pod/v1 model id")
    doc = exact(value, {"schema", "selection", "models", "workers", "waste_governor", "pinned_model"},
                {"schema", "selection", "models", "workers"}, name="config")
    if doc["selection"] not in V1_MODES:
        raise PodError("invalid_config", "selection must be custom or all")
    models = doc["models"]
    if not isinstance(models, dict):
        raise PodError("invalid_config", "models must map pod/v1 model ids to states")
    for model_id, state in models.items():
        if not isinstance(state, str) or state not in V1_STATES:
            raise PodError("invalid_config", f"models.{model_id} must be preferred, available or disabled")
    pin = doc.get("pinned_model")
    if pin is not None:
        if not isinstance(pin, str) or pin not in V1_IDS:
            raise PodError("invalid_pin", f"pinned_model {_diagnostic(pin, 'pin')!r} is not a pod/v1 model id")
        if doc["selection"] != "all" and models.get(pin) not in ("available", "preferred"):
            raise PodError("pin_ineligible", f"Pinned model {pin} is {_state_name(models.get(pin))} in My selection")
    workers = exact(doc["workers"], {"max_active"}, {"max_active"}, name="workers")
    if type(workers["max_active"]) is not int or not 0 <= workers["max_active"] <= 8:
        raise PodError("invalid_config", "workers.max_active must be 0 through 8")
    if "waste_governor" in doc:
        _validate_waste_governor(doc["waste_governor"])
    return doc


def validate(value: Any, *, project: bool = False) -> dict:
    """The current shape: personal `pod/v2`, or project `pod/v1` with Governor policy only."""
    if project:
        doc = exact(value, {"schema", "waste_governor"}, {"schema"}, name="config")
        if doc["schema"] != PROJECT_SCHEMA:
            raise PodError("invalid_config", "Unsupported project configuration schema")
        if "waste_governor" in doc:
            _validate_waste_governor(doc["waste_governor"])
        return doc
    schema = value.get("schema") if isinstance(value, dict) else None
    if schema == V1_SCHEMA:
        _validate_v1(value)
        raise PodError("setup_required", "Personal preferences use the earlier pod/v1 model shape; "
                       f"no route is eligible until route setup is saved: {SETUP_ACTION}")
    if isinstance(value, dict) and schema != SCHEMA:
        raise PodError("invalid_config", "Unsupported configuration schema")
    if isinstance(value, dict) and isinstance(value.get("routes"), dict):
        for key in value["routes"]:
            if supported_route(key) is None:
                raise PodError("invalid_config", f"routes.{key} is not a supported route")
    doc = exact(value, PERSONAL_FIELDS, {"schema", "routes", "workers"}, name="config")
    routes = doc["routes"]
    if not isinstance(routes, dict) or len(routes) > 512:
        raise PodError("invalid_config", "routes must map supported route keys to states")
    for key, state in routes.items():
        if not isinstance(state, str) or state not in ROUTE_STATES:
            raise PodError("invalid_config", f"routes.{key} must be enabled or disabled")
    for field, code, label in (("pinned", "pin", "Pinned"), ("preferred", "preferred", "Preferred")):
        chosen = doc.get(field)
        if chosen is None:
            continue
        if not isinstance(chosen, str) or supported_route(chosen) is None:
            raise PodError(f"invalid_{code}",
                           f"{field} {_diagnostic(chosen, code)!r} is not a supported route key or null")
        if routes.get(chosen) != "enabled":
            raise PodError(f"{code}_ineligible",
                           f"{label} route {chosen} is {_state_name(routes.get(chosen))}; "
                           f"enable it or clear {field} with pod config edit")
    workers = exact(doc["workers"], {"max_active"}, {"max_active"}, name="workers")
    if type(workers["max_active"]) is not int or not 0 <= workers["max_active"] <= 8:
        raise PodError("invalid_config", "workers.max_active must be 0 through 8")
    if "refresh" in doc and (not isinstance(doc["refresh"], str) or doc["refresh"] not in REFRESH_SETTINGS):
        raise PodError("invalid_config", "refresh must be automatic or manual")
    if "waste_governor" in doc:
        _validate_waste_governor(doc["waste_governor"])
    if "authorization_verifier" in doc:
        command = doc["authorization_verifier"]
        if (not isinstance(command, list) or not 1 <= len(command) <= 16
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg
                       for arg in command)
                or not Path(command[0]).is_absolute()):
            raise PodError("invalid_config", "authorization_verifier needs 1-16 command arguments, "
                           "each at most 4096 characters, with an absolute executable path")
    from . import policy, tool_permissions
    from .relay import validate_decision_record
    if "decision_generation" in doc and (type(doc["decision_generation"]) is not int or doc["decision_generation"] < 0):
        raise PodError("invalid_config", "decision_generation must be a nonnegative integer")
    if "decision_policy" in doc:
        policy.validate(doc["decision_policy"])
        if "decision" in doc["decision_policy"]:
            validate_decision_record(doc["decision_policy"]["decision"], kind="policy")
    if "tool_permissions" in doc:
        for item in tool_permissions.validate_memory(doc["tool_permissions"]):
            validate_decision_record(item["decision"], kind="tool_permission")
    if "pending_decisions" in doc:
        validate_pending(doc["pending_decisions"])
    return doc


def personal_path(project: Path | None = None) -> Path:
    override = explicit_home("POD_CONFIG_HOME")
    if override is not None:
        return override / "config.yaml"
    return native_home("XDG_CONFIG_HOME", default=Path.home() / ".config",
                       project=project) / "pod" / "config.yaml"


def _safe_config_parent(path: Path) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise PodError("unsafe_config", "Configuration path is redirected")


def _project_layers(project: Path) -> list[Path]:
    from .github import repository_context
    context = repository_context(project)
    if context.get("repo_key") is None:
        return [project / ".pod" / "config.yaml"]
    return list(dict.fromkeys([Path(context["main_worktree"]) / ".pod" / "config.yaml",
                               Path(context["worktree"]) / ".pod" / "config.yaml"]))


def _governor_policy(project: Path | None, personal: dict | None) -> tuple[dict, dict]:
    policy = deepcopy(DEFAULT_GOVERNOR)
    provenance = {key: "default" for key in policy}
    for scope, layer in [("personal", personal or {})] + (
            [("project", read_yaml(path, project=True) or {}) for path in _project_layers(project)]
            if project is not None else []):
        for key, val in layer.get("waste_governor", {}).items():
            previous = policy.get(key)
            if scope == "project":
                if key == "mode" and val == "observe" and previous == "enforce":
                    raise PodError("authority_expansion", "Project cannot relax Governor mode")
                if key == "cancel_superseded_validation" and val and not previous:
                    raise PodError("authority_expansion", "Project cannot enable remote cancellation")
                if key == "transient_retries" and val > previous:
                    raise PodError("authority_expansion", "Project cannot widen retry budget")
                if key in ("host_control", "exceptions") and (key == "host_control" or val):
                    raise PodError("authority_expansion", "Project cannot add Governor authority")
                if key == "exceptions":
                    continue
                if key == "preflight":
                    policy[key] = list(dict.fromkeys([*previous, *val]))
                    provenance[f"waste_governor.{key}"] = "personal+project"
                    continue
                if key == "triggers":
                    combined = deepcopy(previous)
                    for kind, effects in val.items():
                        combined[kind] = list(dict.fromkeys([*combined.get(kind, []), *effects]))
                    policy[key] = combined
                    provenance[f"waste_governor.{key}"] = "personal+project"
                    continue
            policy[key] = deepcopy(val)
            provenance[f"waste_governor.{key}"] = scope
    return policy, provenance


def load(project: Path | None = None, *, personal: Path | None = None) -> dict:
    """The personal snapshot that selection, admission and every read-only view share.

    `status` is `valid`, `setup_required` (a kept pod/v1 file), `invalid` or `missing`. Only
    a valid file has eligible routes; any error blocks new delegation, never widens it.
    `refresh` is `manual` unless a valid file allows automatic refresh.
    """
    path = personal or personal_path(project)
    data = _read_bytes(path)
    revision = hashlib.sha256(data).hexdigest() if data is not None else None
    try:
        metadata = path.stat(follow_symlinks=False) if data is not None else None
    except OSError:
        metadata = None
    file_stamp = (f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_ctime_ns}"
                  if metadata is not None else None)
    errors = []
    diagnostic: dict = {}
    if data is not None and (metadata is None or not stat.S_ISREG(metadata.st_mode)):
        document = None
        errors.append({"code": "unsafe_config", "message": "Configuration metadata is unavailable"})
    else:
        try:
            document = _parse(data, diagnostic=diagnostic)
        except PodError as exc:
            document = None
            errors.append({"code": exc.code, "message": str(exc)})
    older = diagnostic.get("v1") if document is None else None
    if data is None:
        errors.append({"code": "config_missing", "message": "Personal preferences are missing"})
        status = "missing"
    else:
        status = "valid" if document else "setup_required" if older else "invalid"
    saved = dict(document["routes"]) if document else {}
    governor, provenance = _governor_policy(project, document or older)
    return {"path": str(path.resolve(strict=False)), "revision": revision, "file_stamp": file_stamp,
            "schema": (document or older or {}).get("schema"), "status": status,
            "routes": saved, "eligible": [key for key in route_keys() if saved.get(key) == "enabled"],
            "preferred": document.get("preferred") if document else None,
            "pinned": document.get("pinned") if document else None,
            "max_active": document["workers"]["max_active"] if document else 0,
            "refresh": document.get("refresh", "automatic") if document else "manual",
            "errors": errors,
            "setup": {"from_schema": V1_SCHEMA, "action": SETUP_ACTION} if older else None,
            "pin_diagnostic": None if document else diagnostic.get("pinned"),
            "preferred_diagnostic": None if document else diagnostic.get("preferred"),
            "authorization_verifier": document.get("authorization_verifier") if document else None,
            "decision_policy": document.get("decision_policy") if document else None,
            "tool_permissions": document.get("tool_permissions", []) if document else [],
            "pending_decisions": document.get("pending_decisions", []) if document else [],
            "decision_generation": document.get("decision_generation", 0) if document else 0,
            "policy_revision": digest(governor), "waste_governor": governor, "provenance": provenance}


def effective(project: Path, *, personal: Path | None = None) -> dict:
    snapshot = load(project, personal=personal)
    return {"schema": PROJECT_SCHEMA, "policy": {"waste_governor": snapshot["waste_governor"]},
            "revision": snapshot["policy_revision"], "preference_revision": snapshot["revision"],
            "preferences": snapshot, "provenance": snapshot["provenance"]}


def _encode(document: dict) -> bytes:
    data = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
    if len(data) > 64 * 1024:
        raise PodError("yaml_resource_limit", "Configuration exceeds its size limit")
    return data


def _fsync_parent(path: Path) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _temporary(path: Path, data: bytes) -> str:
    fd, name = tempfile.mkstemp(prefix=".pod-config-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(name)
        raise
    return name


def _replace(path: Path, data: bytes) -> None:
    _safe_config_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = _temporary(path, data)
    try:
        os.replace(name, path)
        _fsync_parent(path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _edit_lock(path: Path) -> Iterator[None]:
    _safe_config_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.parent / ".config.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + .5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PodError("config_busy", "Another Pod window is saving")
                time.sleep(.02)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_defaults(path: Path) -> dict:
    """Explicit installer or config-edit action only; never an internal helper side effect."""
    with _edit_lock(path):
        if _read_bytes(path) is not None:
            raise PodError("config_exists", "Personal preferences already exist")
        document = defaults()
        _replace(path, _encode(document))
        return document


def _surgical(raw: bytes, before: dict, after: dict) -> tuple[bytes, bool]:
    """Edit only the changed lines so unrelated YAML and comments survive when possible."""
    changed = raw.decode("utf-8")
    ok = True
    for key in sorted(set(before["routes"]) | set(after["routes"])):
        old, new = before["routes"].get(key), after["routes"].get(key)
        if old == new:
            continue
        line = rf"(?m)^([ \t]+{re.escape(key)}:[ \t]*)(?:enabled|disabled)([ \t]*(?:#.*)?)"
        if old is not None and new is not None:
            changed, count = re.subn(line + "$", lambda match: match[1] + new + match[2], changed, count=1)
        elif old is not None:
            changed, count = re.subn(line + r"(?:\r?\n|$)", "", changed, count=1)
        else:
            changed = re.sub(r"(?m)^routes:[ \t]*\{\}([ \t]*(?:#.*)?)$", r"routes:\1", changed, count=1)
            indent = re.search(r"(?m)^([ \t]+)\S+:[ \t]*(?:enabled|disabled)[ \t]*(?:#.*)?$", changed)
            changed, count = re.subn(r"(?m)^(routes:[ \t]*(?:#.*)?)(\r?\n|$)",
                                     lambda match: (match[1] + "\n" + (indent[1] if indent else "  ")
                                                    + f"{key}: {new}" + (match[2] or "\n")),
                                     changed, count=1)
        ok = ok and count == 1
    for field in ("preferred", "pinned", "refresh"):
        if before.get(field) == after.get(field):
            continue
        value = after.get(field) or "null"
        changed, count = re.subn(rf"(?m)^({field}:[ \t]*)[^#\r\n]*?([ \t]*(?:#.*)?)$",
                                 lambda match: match[1] + value + match[2], changed, count=1)
        if not count:
            changed += ("" if changed.endswith("\n") else "\n") + f"{field}: {value}\n"
    if before["workers"] != after["workers"]:
        changed, count = re.subn(r"(?m)^([ \t]+max_active:[ \t]*)\d+([ \t]*(?:#.*)?)$",
                                 lambda match: match[1] + str(after["workers"]["max_active"]) + match[2],
                                 changed, count=1)
        ok = ok and count == 1
    for field in DECISION_FIELDS:
        if before.get(field) == after.get(field):
            continue
        lines = changed.splitlines(keepends=True)
        starts = [index for index, line in enumerate(lines) if re.match(rf"^{field}:[ \t]*", line)]
        block = yaml.safe_dump({field: after[field]}, sort_keys=False, allow_unicode=True)
        if len(starts) == 1:
            start, end = starts[0], starts[0] + 1
            while end < len(lines):
                line = lines[end]
                if line.startswith("#") or re.match(r"^(?:[A-Za-z_][A-Za-z0-9_-]*|\"[^\"]+\"|'[^']+')[ \t]*:", line):
                    break
                end += 1
            lines[start:end] = [block]
            changed = "".join(lines)
        elif not starts:
            changed += ("" if changed.endswith("\n") else "\n") + block
        else:
            ok = False
    encoded = changed.encode("utf-8")
    try:
        if ok and len(encoded) <= 64 * 1024 and _parse(encoded) == after:
            return encoded, True
    except PodError:
        pass
    return _encode(after), False


def _set_optional(document: dict, field: str, value: object) -> None:
    if value is None and field not in document:
        return
    document[field] = value


def edit(path: Path, *, displayed: dict, routes: dict | None = None, preferred: object = KEEP,
         pinned: object = KEEP, refresh: object = KEEP, max_active: object = KEEP) -> dict:
    """One explicit atomic preference edit with targeted compare-and-swap.

    `routes` lists the exact route keys this action changes (`enabled`, `disabled`, or None for
    not set); it is never a wildcard for routes added later. Preferred and Pin move or clear only
    when named here. An edit that would leave the pin or preference on a route that is not enabled
    is refused unless this same action clears or replaces it. `merged_outside` reports whether
    the bytes read under the lock differ from `displayed["revision"]`, so an outside edit merged
    into this save can be announced; it is False when `displayed` names no revision.
    """
    changes = dict(routes or {})
    if len(changes) > 512:
        raise PodError("invalid_config_edit", "Too many routes in one edit")
    for key, state in changes.items():
        if supported_route(key) is None or state not in (*ROUTE_STATES, None):
            raise PodError("invalid_config_edit", "Route key or state is unsupported")
    for field, value in (("preferred", preferred), ("pinned", pinned)):
        if value is not KEEP and value is not None and supported_route(value) is None:
            raise PodError("invalid_pin" if field == "pinned" else "invalid_preferred",
                           f"{field} must name a supported route key or null")
    if refresh is not KEEP and refresh not in REFRESH_SETTINGS:
        raise PodError("invalid_config_edit", "refresh must be automatic or manual")
    if max_active is not KEEP and (type(max_active) is not int or not 0 <= max_active <= 8):
        raise PodError("invalid_config_edit", "workers.max_active must be 0 through 8")
    if not changes and all(value is KEEP for value in (preferred, pinned, refresh, max_active)):
        raise PodError("invalid_config_edit", "The edit changes nothing")
    with _edit_lock(path):
        raw = _read_bytes(path)
        if raw is None:
            raise PodError("config_missing", "Personal preferences are missing")
        document = _parse(raw)
        shown = displayed.get("routes") or {}
        stale = (any(document["routes"].get(key) != shown.get(key) for key in changes)
                 or any(value is not KEEP and document.get(field, default) != displayed.get(field)
                        for field, value, default in (("preferred", preferred, None),
                                                      ("pinned", pinned, None),
                                                      ("refresh", refresh, "automatic")))
                 or max_active is not KEEP and document["workers"]["max_active"] != displayed.get("max_active"))
        if stale:
            raise PodError("config_changed_elsewhere", "Changed elsewhere — press again")
        updated = deepcopy(document)
        for key, state in changes.items():
            if state is None:
                updated["routes"].pop(key, None)
            else:
                updated["routes"][key] = state
        for field, value in (("preferred", preferred), ("pinned", pinned), ("refresh", refresh)):
            if value is not KEEP:
                _set_optional(updated, field, value)
        if max_active is not KEEP:
            updated["workers"]["max_active"] = max_active
        try:
            validate(updated)
        except PodError as exc:
            if exc.code not in ("pin_ineligible", "preferred_ineligible"):
                raise
            field, label, value = (("pinned", "pin", pinned) if exc.code == "pin_ineligible"
                                   else ("preferred", "preference", preferred))
            target = updated[field]
            state = _state_name(updated["routes"].get(target))
            if value is not KEEP:
                raise PodError(exc.code, f"Cannot make {target} the {label}: it is {state}; enable it first") from exc
            raise PodError("pin_invalidated" if field == "pinned" else "preferred_invalidated",
                           f"Clear or replace the {label} {target} in the same action: "
                           f"this edit would make it {state}") from exc
        encoded, preserved = _surgical(raw, document, updated)
        _replace(path, encoded)
    return {"path": str(path.resolve(strict=False)), "revision": hashlib.sha256(encoded).hexdigest(),
            "routes": dict(updated["routes"]), "preferred": updated.get("preferred"),
            "pinned": updated.get("pinned"), "refresh": updated.get("refresh", "automatic"),
            "max_active": updated["workers"]["max_active"], "changed": sorted(changes),
            "notice": "" if preserved else "comments not preserved",
            "merged_outside": (displayed.get("revision") is not None
                               and hashlib.sha256(raw).hexdigest() != displayed["revision"])}


def set_route(path: Path, key: str, state: str | None, *, displayed: dict,
              preferred: object = KEEP, pinned: object = KEEP) -> dict:
    return edit(path, displayed=displayed, routes={key: state}, preferred=preferred, pinned=pinned)


def set_routes(path: Path, changes: dict, *, displayed: dict,
               preferred: object = KEEP, pinned: object = KEEP) -> dict:
    """A bulk edit of exactly the listed routes, saved in one atomic write."""
    if not changes:
        raise PodError("invalid_config_edit", "A bulk edit names the exact routes it changes")
    return edit(path, displayed=displayed, routes=changes, preferred=preferred, pinned=pinned)


def set_preferred(path: Path, key: str | None, *, displayed: dict) -> dict:
    return edit(path, displayed=displayed, preferred=key)


def set_pin(path: Path, key: str | None, *, displayed: dict) -> dict:
    return edit(path, displayed=displayed, pinned=key)


def set_refresh(path: Path, setting: str, *, displayed: dict) -> dict:
    return edit(path, displayed=displayed, refresh=setting)


def decision_state(snapshot: dict) -> dict:
    """Authority facts only, so unrelated route edits can merge under the lock."""
    return {key: deepcopy(snapshot[key]) for key in DECISION_FIELDS}


def prepare_decision_document(snapshot: dict, changes: dict, capability=None) -> tuple[dict, bytes, bool]:
    """Caller holds the short config lock; return the complete validated replacement."""
    raw = _read_bytes(Path(snapshot["path"]))
    document = _parse(raw) if raw is not None else {
        "schema": SCHEMA, "routes": {}, "workers": {"max_active": DEFAULT_WORKER_CAPACITY}}
    updated = {**document, **deepcopy(changes), "decision_generation": snapshot["decision_generation"] + 1}
    if capability is not None:
        capability.finalize(updated)
    _shape(updated)
    validate(updated)
    _encode(updated)
    encoded, preserved = _surgical(raw, document, updated) if raw is not None else (_encode(updated), True)
    return updated, encoded, preserved


def document_decision_state(document: dict) -> dict:
    return {key: deepcopy(document.get(key, None if key == "decision_policy" else 0 if key == "decision_generation" else []))
            for key in DECISION_FIELDS}


def tool_question_text(result: dict, tool: str, category: str, operation: str) -> str:
    from .term import clean
    category = category.replace("_", " ")
    text = (f"May {tool} use {category} to {operation}?" if result["purpose"] == "operation" else
            result["text"].replace(result["category"], category))
    return clean(text)


def validate_pending(value: object) -> list[dict]:
    from . import policy, tool_permissions
    from .relay import validate_decision_record
    from .util import bounded_text, encoded
    if not isinstance(value, list) or len(value) > 8:
        raise PodError("invalid_pending_decisions", "At most eight unresolved machine questions are retained")
    seen = set()
    for item in value:
        names = {"id", "purpose", "tool", "category", "categories", "operation", "spending", "revisions",
                 "state", "text", "choices", "decision", "answer", "principal"}
        row = exact(item, names, names - {"decision", "answer", "principal"}, name="pending decision")
        bounded_text(row["id"], name="pending_id", limit=128)
        if row["id"] in seen or row["purpose"] not in ("memory", "operation"):
            raise PodError("invalid_pending_decisions", "Pending identity or purpose is invalid")
        seen.add(row["id"])
        tool_permissions._tool(row["tool"])
        tool_permissions._category(row["category"])
        bounded_text(row["operation"], name="tool_operation", limit=256)
        bounded_text(row["text"], name="question", limit=512)
        expected = policy.categories("tool_operation", {"category": row["category"], "spending": row["spending"]})
        if row["categories"] != expected or row["state"] not in ("pending", "approved", "refused", "consumed", "answered", "cancelled"):
            raise PodError("invalid_pending_decisions", "Pending categories or state are invalid")
        choices = (["ask_each_time"] if row["category"] == "money" else list(tool_permissions.MODES)) if row["purpose"] == "memory" else ["approve", "refuse"]
        if row["choices"] != choices:
            raise PodError("invalid_pending_decisions", "Pending choices disagree with its purpose")
        revisions = tool_permissions._revisions(row["revisions"])
        if revisions["invocation"] != row["id"]:
            raise PodError("invalid_pending_decisions", "Pending invocation is inconsistent")
        if row["state"] not in ("pending", "cancelled"):
            if row.get("answer") not in choices:
                raise PodError("invalid_pending_decisions", "Answered question needs an exact choice")
            approval = validate_decision_record(row.get("decision", {}), kind="tool_permission" if row["purpose"] == "memory" else "tool_operation")
            if ("principal" in row and row["principal"] != approval["actor"]
                    or "principal" not in row and "decision_binding" in row["decision"]):
                raise PodError("invalid_pending_decisions", "Pending actor is inconsistent")
        elif {"decision", "answer", "principal"} & set(row):
            raise PodError("invalid_pending_decisions", "Pending question carries no approval")
        if len(encoded(row, compact=True)) > 2048:
            raise PodError("pending_decision_too_large", "Question exceeds 2048 bytes; shorten the operation or decision evidence")
    return deepcopy(value)


def _decision_prior(snapshot: dict) -> list[dict]:
    rows = []
    current = snapshot["decision_policy"]
    if current is not None and "decision" in current:
        rows.append({"keys": ["policy"], "decided_at": current["decision"].get("decided_at")})
    for row in snapshot["tool_permissions"]:
        rows.append({"keys": ["memory:" + digest([row["tool"], row["category"]])],
                     "decided_at": row["decision"].get("decided_at")})
    for row in snapshot["pending_decisions"]:
        if "decision" in row:
            rows.append({"keys": ["invocation:" + row["id"]], "decided_at": row["decision"].get("decided_at")})
    return rows


def decision_edit(project: Path | None, *, displayed: dict, changes: dict, capability=None,
                  keys: list[str] = ()) -> dict:
    """Commit only decision fields with generation CAS, preflight, then receipt.

    This is the existing personal writer and receipt home, with config -> receipt
    locking for machine-only work. No verifier or native lookup runs in this lock.
    """
    if not changes or set(changes) - (set(DECISION_FIELDS) - {"decision_generation"}):
        raise PodError("invalid_config_edit", "Decision edit names supported personal decision fields")
    from . import policy
    path = personal_path(project)
    with _edit_lock(path):
        raw = _read_bytes(path)
        document = _parse(raw) if raw is not None else {
            "schema": SCHEMA, "routes": {}, "workers": {"max_active": DEFAULT_WORKER_CAPACITY}}
        current = load(project)
        if decision_state(current) != decision_state(displayed):
            raise PodError("decision_stale", "Policy, permission memory or pending decisions changed; read again")
        updated, encoded, preserved = prepare_decision_document(current, changes, capability)
        after = document_decision_state(updated)
        if capability is not None:
            from .relay import Decision
            if not isinstance(capability, Decision):
                raise PodError("decision_capability_required", "The mutation needs its exact decision capability")
            capability.consume(current_state=decision_state(current), snapshot=current, result=after,
                               keys=keys, prior=_decision_prior(current))
        elif (set(changes) != {"pending_decisions"}
              and not (set(changes) == {"decision_policy"} and current["decision_policy"] is None
                       and changes["decision_policy"] == {"preset": "careful", "categories": policy.preset("careful"), "setup": "skipped"})):
            raise PodError("decision_capability_required", "An answered decision requires current authority")
        elif set(changes) == {"pending_decisions"}:
            # Unapproved question creation/pruning carries no approval or permission.
            old = {row["id"]: row for row in current["pending_decisions"]}
            if any(row.get("state") != "pending" and row != old.get(row["id"])
                   and not (row.get("state") == "cancelled" and old.get(row["id"], {}).get("state") == "pending"
                            and {key: value for key, value in row.items() if key != "state"}
                                == {key: value for key, value in old[row["id"]].items() if key != "state"})
                   for row in changes["pending_decisions"]):
                raise PodError("decision_capability_required", "An answered question requires current authority")
        _replace(path, encoded)
    saved = load(project)
    saved["notice"] = "" if preserved else "comments not preserved"
    return saved


def decision_context(snapshot: dict) -> dict:
    from .policy import projection
    view = projection(snapshot["decision_policy"], snapshot["tool_permissions"], snapshot["pending_decisions"])
    return {"policy": {key: view[key] for key in ("preset", "categories", "setup")},
            "permissions": view["permissions"], "decision_generation": snapshot["decision_generation"],
            "every_sensitive_use": view["every_sensitive_use"]}


def set_decision_policy(project: Path | None, name: str, *, decision: dict | None = None,
                        categories: dict | None = None, displayed: dict | None = None) -> dict:
    from . import policy
    from .relay import Decision
    snapshot = displayed or load(project)
    if decision is not None:
        exact(decision, {"provenance", "instruction", "channel", "relay"}, {"provenance", "instruction"}, name="decision input")
    if name == "skip":
        if snapshot["decision_policy"] is not None or decision is not None or categories is not None:
            raise PodError("policy_skip_refused", "Skip only the first unanswered setup; it keeps Careful")
        value = {"preset": "careful", "categories": policy.preset("careful"), "setup": "skipped"}
        capability = None
    else:
        value = {"preset": name, "categories": policy.preset(name, categories), "setup": "answered"}
        current = snapshot["decision_policy"]
        if current is not None and {key: item for key, item in current.items() if key != "decision"} == value:
            from .relay import same_decision_input
            if "decision" in current and same_decision_input(decision, current["decision"]):
                return {"status": "replayed", "committed": True, "decision_generation": snapshot["decision_generation"],
                        **decision_context(snapshot)}
        capability = Decision(project, None, kind="policy", effect={"previous": snapshot["decision_policy"], "proposed": value},
                              state=decision_state(snapshot), decision=decision, resources={"policy": "personal"}, snapshot=snapshot)
        value["decision"] = capability.record
    saved = decision_edit(project, displayed=snapshot, changes={"decision_policy": value}, capability=capability, keys=["policy"])
    return {"status": "saved", "decision_generation": saved["decision_generation"], **decision_context(saved),
            **({"notice": saved["notice"]} if saved.get("notice") else {})}


def tool_check(project: Path | None, tool: str, category: str, operation: str, *, spending=None,
               invocation: str | None = None) -> dict:
    """Check at every sensitive use; this helper executes no external tool."""
    from uuid import uuid4
    from . import tool_permissions
    snapshot = load(project)
    pending = next((row for row in snapshot["pending_decisions"] if row["id"] == invocation), None)
    if invocation is not None and pending is None:
        raise PodError("tool_invocation_unavailable", "The pending invocation is unavailable; an old answer grants nothing")
    if invocation is None:
        pending = next((row for row in snapshot["pending_decisions"] if row["state"] == "pending"
                        and (row["tool"], row["category"], row["operation"], row["spending"]) == (tool, category, operation, spending)), None)
        if pending is not None:
            invocation = pending["id"]
    if pending is not None and (pending["tool"], pending["category"], pending["operation"], pending["spending"]) != (tool, category, operation, spending):
        raise PodError("tool_invocation_mismatch", "Invocation binds another exact operation")
    if pending is not None and pending["state"] == "consumed":
        return {"status": "consumed", "execute": False, "invocation": invocation}
    if pending is not None and pending["purpose"] == "memory" and pending["state"] == "answered":
        invocation, pending = None, None
    refreshed = None
    if pending is not None and pending["state"] == "pending" and pending["revisions"] != {
            "policy": digest(snapshot["decision_policy"]), "memory": digest(snapshot["tool_permissions"]),
            "decision_generation": snapshot["decision_generation"], "invocation": invocation}:
        refreshed = pending["id"]
        invocation, pending = None, None
    revisions = {"policy": digest(snapshot["decision_policy"]), "memory": digest(snapshot["tool_permissions"]),
                 "decision_generation": snapshot["decision_generation"], "invocation": invocation or uuid4().hex}
    evaluated = None
    if pending is not None and pending["purpose"] == "operation":
        if pending["state"] == "approved":
            if pending["revisions"] != revisions:
                raise PodError("decision_stale", "Policy or permission state changed after operation approval")
            from .relay import validate_decision_record
            evaluated = {key: pending[key] for key in ("id", "purpose", "tool", "category", "operation", "spending", "revisions", "state")}
            evaluated["principal"] = validate_decision_record(pending["decision"])["actor"]
    result = tool_permissions.check(snapshot["decision_policy"], snapshot["tool_permissions"], tool, category,
                                    operation, spending=spending, pending=evaluated, revisions=revisions)
    if "text" in result:
        result = {**result, "text": tool_question_text(result, tool, category, operation)}
    if pending is not None and pending["state"] in ("refused", "cancelled"):
        return {"status": "refused", "execute": False, "invocation": invocation}
    if result["status"] == "allowed":
        if result.get("consume_required"):
            # Approval is already committed. The use has a separate durable receipt,
            # and its recorded post-answer generation is rechecked under the lock.
            return _consume_tool(project, snapshot, pending, result)
        with _edit_lock(personal_path(project)):
            if decision_state(snapshot) != decision_state(load(project)):
                raise PodError("decision_stale", "Policy or permission changed before the sensitive-use response")
            return {**result, "execute": True}
    if pending is not None and pending["state"] == "pending":
        return {**result, "execute": False, "invocation": pending["id"], "question": pending["text"]}
    # Finished memory choices do not implicitly approve this use: create its exact
    # operation question only after memory has been committed independently.
    # This personal pending mutation advances generation, invalidating every
    # earlier operation approval. Retain its non-evicting receipt, not a dead slot.
    rows = [row for row in snapshot["pending_decisions"]
            if row["id"] != refreshed and row["state"] == "pending"]
    if len(rows) >= 8:
        raise PodError("pending_decision_limit", "Resolve an existing machine question before creating another")
    revisions["decision_generation"] += 1
    row = {"id": revisions["invocation"], "purpose": result["purpose"], "tool": tool, "category": category,
           "categories": result["categories"], "operation": operation, "spending": spending,
           "revisions": revisions, "state": "pending", "text": result["text"], "choices": result["choices"]}
    decision_edit(project, displayed=snapshot, changes={"pending_decisions": rows + [row]})
    return {**result, "execute": False, "invocation": row["id"], "question": row["text"]}


def tool_review(project: Path | None, tool: str, category: str) -> dict:
    """Ask to change memory only; this never approves an operation."""
    from uuid import uuid4
    from . import policy, tool_permissions
    tool, category = tool_permissions._tool(tool), tool_permissions._category(category)
    if category == "money":
        raise PodError("tool_review_money", "Money has no hands-off permission; check and approve each new spending commitment")
    snapshot = load(project)
    identity = uuid4().hex
    rows = [row for row in snapshot["pending_decisions"] if row["state"] == "pending"
            and not (row["purpose"] == "memory" and (row["tool"], row["category"]) == (tool, category))]
    if len(rows) >= 8:
        raise PodError("pending_decision_limit", "Resolve an existing machine question before reviewing another")
    entry = next((row for row in snapshot["tool_permissions"] if (row["tool"], row["category"]) == (tool, category)), None)
    row = {"id": identity, "purpose": "memory", "tool": tool, "category": category, "categories": [category],
           "operation": "Review remembered tool permission", "spending": None,
           "revisions": {"policy": digest(snapshot["decision_policy"]), "memory": digest(snapshot["tool_permissions"]),
                         "decision_generation": snapshot["decision_generation"] + 1, "invocation": identity},
           "state": "pending", "choices": list(tool_permissions.MODES),
           "text": f"For {tool} and {category.replace('_', ' ')}, choose hands-off or ask each time" + (
               f" (currently {entry['mode'].replace('_', ' ')})." if entry else ".")}
    decision_edit(project, displayed=snapshot, changes={"pending_decisions": rows + [row]})
    return {"status": "permission_question", "execute": False, "purpose": "memory", "invocation": identity,
            "question": row["text"], "choices": row["choices"], "required": policy.required(snapshot["decision_policy"], [category])}


def tool_cancel(project: Path | None, invocation: str) -> dict:
    snapshot = load(project)
    rows = deepcopy(snapshot["pending_decisions"])
    row = next((item for item in rows if item["id"] == invocation), None)
    if row is None or row["state"] != "pending":
        raise PodError("tool_cancel_refused", "Cancel only an unanswered question")
    row["state"] = "cancelled"
    decision_edit(project, displayed=snapshot, changes={"pending_decisions": rows})
    return {"status": "cancelled", "execute": False, "invocation": invocation}


def tool_answer(project: Path | None, invocation: str, answer: str, *, decision: dict,
                displayed: dict | None = None) -> dict:
    from . import tool_permissions
    from .relay import Decision, compact_tool_decision
    exact(decision, {"provenance", "instruction", "channel", "relay"}, {"provenance", "instruction"}, name="decision input")
    snapshot = displayed or load(project)
    rows = deepcopy(snapshot["pending_decisions"])
    row = next((item for item in rows if item["id"] == invocation), None)
    if row is not None and row["state"] != "pending" and row.get("answer") == answer:
        from .relay import same_decision_input
        if same_decision_input(decision, row["decision"]):
            return {"status": row["state"], "execute": False, "invocation": invocation, "committed": True,
                    "readback": True, "decision_generation": snapshot["decision_generation"]}
    if row is None or row["state"] != "pending" or answer not in row["choices"]:
        raise PodError("tool_answer_refused", "Answer a current unresolved question with one offered choice")
    expected = {"policy": digest(snapshot["decision_policy"]), "memory": digest(snapshot["tool_permissions"]),
                "decision_generation": snapshot["decision_generation"], "invocation": invocation}
    if row["revisions"] != expected:
        raise PodError("decision_stale", "Policy or memory changed; check the operation again before answering")
    kind = "tool_permission" if row["purpose"] == "memory" else "tool_operation"
    effect = {"category": row["category"], "spending": row["spending"], "question": row, "answer": answer}
    capability = Decision(project, None, kind=kind, effect=effect, state=decision_state(snapshot), decision=decision,
                          resources={"tool": row["tool"], "category": row["category"], "invocation": invocation}, snapshot=snapshot)
    row.update(answer=answer, decision=compact_tool_decision(capability.record),
               state="answered" if row["purpose"] == "memory" else "approved" if answer == "approve" else "refused")
    if capability.verdict is None:
        row["principal"] = capability.actor
    changes = {"pending_decisions": rows}
    key = "invocation:" + invocation
    if row["purpose"] == "memory":
        changes["tool_permissions"] = tool_permissions.remember(snapshot["tool_permissions"], row["tool"], row["category"], answer, capability.record)
        key = "memory:" + digest([row["tool"], row["category"]])
    row["revisions"]["decision_generation"] += 1
    saved = decision_edit(project, displayed=snapshot, changes=changes, capability=capability, keys=[key])
    return {"status": row["state"], "execute": False, "invocation": invocation,
            "decision_generation": saved["decision_generation"], **({"notice": saved["notice"]} if saved.get("notice") else {})}


def _consume_tool(project: Path | None, snapshot: dict, pending: dict, result: dict) -> dict:
    from .ledger import _lock, state_root
    from .util import atomic_json, bounded_json
    path = personal_path(project)
    receipt = state_root(None) / ".relay-authorizations" / (digest("pod-tool-use:" + pending["id"]) + ".json")
    with _edit_lock(path):
        current = load(project)
        if decision_state(snapshot) != decision_state(current):
            raise PodError("decision_stale", "Policy or memory changed before operation consumption")
        raw = _read_bytes(path)
        document = _parse(raw)
        updated = deepcopy(document)
        row = next(item for item in updated["pending_decisions"] if item["id"] == pending["id"])
        row["state"] = "consumed"
        updated["decision_generation"] += 1
        _shape(updated)
        validate(updated)
        encoded = _encode(updated)
        use = {"schema": "pod-relay-use/v1", "authorization": digest(pending),
               "transition": digest({"before": decision_state(snapshot), "invocation": pending["id"]})}
        with _lock(receipt):
            recovered = receipt.exists() or receipt.is_symlink()
            if recovered and bounded_json(receipt, limit=1024) != use:
                raise PodError("tool_invocation_reused", "Invocation already consumed with another binding")
            if not recovered:
                atomic_json(receipt, use, limit=1024)
            _replace(path, encoded)
        # A crash after the use receipt cannot produce a fresh execute response.
        return {**result, "status": "consumed" if recovered else "allowed", "execute": not recovered,
                "invocation": pending["id"]}


def setup_preview(raw: bytes) -> dict:
    """Pure, deterministic conversion of kept pod/v1 bytes into a proposed pod/v2 document.

    Supported bases that were Available or Preferred get their supported efforts enabled,
    Disabled bases get every route disabled, and Not set stays not set. Replaced generations,
    old Preferred models and an old pin's effort are never inferred.
    """
    if not isinstance(raw, bytes):
        raise PodError("invalid_setup", "Setup preview needs the kept file bytes")
    value = _decode(raw)
    if not isinstance(value, dict) or value.get("schema") != V1_SCHEMA:
        raise PodError("setup_not_required", "Only a pod/v1 personal file needs route setup")
    older = _validate_v1(value)
    supported = by_id()
    saved, mode = older["models"], older["selection"]
    routes, notes, choices = {}, [], []
    for model_id in V1_IDS:
        state = saved.get(model_id)
        model = supported.get(model_id)
        if model is not None and state is not None:
            for effort in model["efforts"]:
                routes[f"{model['agent']}/{model_id}/{effort}"] = "disabled" if state == "disabled" else "enabled"
    replaced = [model_id for model_id in V1_IDS if model_id in REPLACED and saved.get(model_id) is not None]
    if replaced:
        notes.append({"code": "replaced_not_transferred",
                      "message": "Choices for " + ", ".join(f"{old} (replaced by {REPLACED[old]})" for old in replaced)
                      + " are not transferred; the replacements stay not set until you enable exact routes"})
    if mode == "all":
        notes.append({"code": "selection_all_narrowed",
                      "message": "selection: all converts from the saved model map only; models it did not "
                                 "list stay not set"})
    preferred_models = [model_id for model_id in V1_IDS if saved.get(model_id) == "preferred"]
    if preferred_models:
        notes.append({"code": "preferred_not_inferred",
                      "message": "Earlier Preferred models (" + ", ".join(preferred_models) + ") now have "
                                 "every effort enabled; choose one exact Preferred route if you want one"})
    pin = older.get("pinned_model")
    if pin is not None:
        model = supported.get(pin)
        options = [f"{model['agent']}/{pin}/{effort}" for effort in model["efforts"]] if model else []
        choices.append({"id": "pin", "model": pin, "options": options, "clear": True})
        notes.append({"code": "pin_effort_required" if model else "pin_base_unsupported",
                      "message": (f"Choose an exact effort for the earlier pin {pin}, or clear the pin" if model
                                  else f"The earlier pin {pin} is no longer supported; clear the pin to continue")})
    notes.append({"code": "refresh_automatic",
                  "message": "Automatic model-data refresh is on; set refresh: manual to avoid automatic "
                             "network access"})
    document = {"schema": SCHEMA, "routes": routes, "preferred": None, "pinned": None,
                "workers": {"max_active": older["workers"]["max_active"]}, "refresh": "automatic"}
    if "waste_governor" in older:
        document["waste_governor"] = deepcopy(older["waste_governor"])
    return {"schema": SETUP_SCHEMA, "from_schema": V1_SCHEMA, "revision": hashlib.sha256(raw).hexdigest(),
            "document": document, "notes": notes, "choices": choices}


def _backup(path: Path, raw: bytes) -> Path:
    """Copy the original exclusively and durably; an equal earlier copy is reused."""
    target = path.with_name(path.name + BACKUP_SUFFIX)
    name = _temporary(path, raw)
    try:
        try:
            os.link(name, target, follow_symlinks=False)
        except FileExistsError:
            if _read_bytes(target) != raw:
                raise PodError("setup_backup_exists",
                               f"{target} already holds different bytes; move it aside before setup") from None
        _fsync_parent(path)
    finally:
        os.unlink(name)
    return target


def setup_apply(path: Path, *, expected_revision: str, choices: dict) -> dict:
    """Save the explicit route setup of a kept pod/v1 file after a preview.

    `choices` may hold `pin` (an exact route of the earlier pinned model, or None to clear it;
    required when the file had a pin), `preferred` (an exact route or None) and `routes` (explicit
    route states that adjust the proposal). The original is kept as `config.yaml.pod-v1`.
    """
    if not isinstance(choices, dict) or set(choices) - {"pin", "preferred", "routes"}:
        raise PodError("invalid_setup_choice", "Setup choices are pin, preferred and routes")
    adjust = choices.get("routes") or {}
    if not isinstance(adjust, dict) or any(supported_route(key) is None or state not in (*ROUTE_STATES, None)
                                           for key, state in adjust.items()):
        raise PodError("invalid_setup_choice", "Setup route choices name supported routes and states")
    with _edit_lock(path):
        raw = _read_bytes(path)
        if raw is None:
            raise PodError("config_missing", "Personal preferences are missing")
        if hashlib.sha256(raw).hexdigest() != expected_revision:
            raise PodError("config_changed_elsewhere", "Changed elsewhere — review the setup again")
        preview = setup_preview(raw)
        document = preview["document"]
        for key, state in adjust.items():
            if state is None:
                document["routes"].pop(key, None)
            else:
                document["routes"][key] = state
        pin_choice = next((row for row in preview["choices"] if row["id"] == "pin"), None)
        if pin_choice is not None and "pin" not in choices:
            raise PodError("setup_choice_required",
                           f"Choose an exact route for the earlier pin {pin_choice['model']} or clear it")
        pin = choices.get("pin")
        if pin is not None and (pin_choice is None or pin not in pin_choice["options"]):
            raise PodError("invalid_setup_choice", "The pin must be an exact route of the earlier pinned model")
        document["pinned"] = pin
        document["preferred"] = choices.get("preferred")
        validate(document)
        backup = _backup(path, raw)
        encoded = _encode(document)
        _replace(path, encoded)
    return {"status": "saved", "path": str(path.resolve(strict=False)),
            "revision": hashlib.sha256(encoded).hexdigest(), "backup": str(backup.resolve(strict=False)),
            "document": document, "notes": preview["notes"]}

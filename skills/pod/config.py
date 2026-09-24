"""One personal YAML preference authority and restrictive Governor project policy."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Iterator

import yaml

from .catalog import IDS
from .errors import PodError
from .util import digest, exact, explicit_home, native_home

SCHEMA = "pod/v1"
STATES = ("available", "preferred", "disabled")
MODES = ("custom", "all")
DEFAULT_WORKER_CAPACITY = 2
GOVERNED_KINDS = ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic",
                  "merge", "release", "deploy", "cancel_validation")
TRIGGER_KINDS = ("push", "pr_update")
EFFECT_PREFIXES = ("workflow:", "deploy:", "release:")
WASTE_GOVERNOR_FIELDS = {"mode", "cancel_superseded_validation", "preflight", "triggers",
                         "transient_retries", "host_control", "exceptions"}
DEFAULT_GOVERNOR = {"mode": "enforce", "cancel_superseded_validation": True,
                    "preflight": [], "triggers": {}, "transient_retries": 1, "exceptions": []}
DEFAULT = {"schema": SCHEMA, "selection": "custom",
           "models": {model_id: "available" for model_id in IDS},
           "workers": {"max_active": DEFAULT_WORKER_CAPACITY}}


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


def _parse(data: bytes | None, *, project: bool = False) -> dict | None:
    if data is None:
        return None
    try:
        value = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)
    except PodError:
        raise
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise PodError("invalid_yaml", "Configuration cannot be safely decoded") from exc
    _shape(value)
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
    if (not isinstance(value, list) or len(value) > 16
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)):
        raise PodError("invalid_" + name, f"{name} must be a bounded unique list")
    for item in value:
        if (not isinstance(item, str) or not item.startswith(EFFECT_PREFIXES)
                or len(item) > 256 or not item.split(":", 1)[1]):
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


def validate(value: Any, *, project: bool = False) -> dict:
    if not project and isinstance(value, dict) and isinstance(value.get("models"), dict):
        for model_id in value["models"]:
            if model_id not in IDS:
                raise PodError("invalid_config", f"models.{model_id} is not a supported model id")
    allowed = {"schema", "waste_governor"} if project else {"schema", "selection", "models", "workers", "waste_governor"}
    required = {"schema"} if project else {"schema", "selection", "models", "workers"}
    doc = exact(value, allowed, required, name="config")
    if doc["schema"] != SCHEMA:
        raise PodError("invalid_config", "Unsupported configuration schema")
    if not project:
        if doc["selection"] not in MODES:
            raise PodError("invalid_config", "selection must be custom or all")
        models = doc["models"]
        if not isinstance(models, dict) or len(models) > len(IDS):
            raise PodError("invalid_config", "models must map supported model ids to states")
        for model_id, state in models.items():
            if model_id not in IDS:
                raise PodError("invalid_config", f"models.{model_id} is not a supported model id")
            if state not in STATES or not isinstance(state, str):
                raise PodError("invalid_config", f"models.{model_id} must be preferred, available or disabled")
        if doc["selection"] == "all" and set(models) != set(IDS):
            raise PodError("invalid_config", "All models mode needs the complete saved model map")
        workers = exact(doc["workers"], {"max_active"}, {"max_active"}, name="workers")
        if type(workers["max_active"]) is not int or not 0 <= workers["max_active"] <= 8:
            raise PodError("invalid_config", "workers.max_active must be 0 through 8")
    if "waste_governor" in doc:
        _validate_waste_governor(doc["waste_governor"])
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
            policy[key] = deepcopy(val)
            provenance[f"waste_governor.{key}"] = scope
    return policy, provenance


def load(project: Path | None = None, *, personal: Path | None = None) -> dict:
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
    if data is not None and (metadata is None or not stat.S_ISREG(metadata.st_mode)):
        document = None
        errors.append({"code": "unsafe_config", "message": "Configuration metadata is unavailable"})
    else:
        try:
            document = _parse(data)
        except PodError as exc:
            document = None
            errors.append({"code": exc.code, "message": str(exc)})
    if data is None:
        errors.append({"code": "config_missing", "message": "Personal preferences are missing"})
    saved = {model_id: document["models"].get(model_id) if document else None for model_id in IDS}
    mode = document["selection"] if document else None
    effective_states = {model_id: ("available" if mode == "all" else saved[model_id])
                        for model_id in IDS}
    eligible = [model_id for model_id in IDS if effective_states[model_id] in ("preferred", "available")]
    governor, provenance = _governor_policy(project, document)
    policy_revision = digest(governor)
    return {"path": str(path.resolve(strict=False)), "revision": revision, "mode": mode,
            "file_stamp": file_stamp,
            "saved": saved, "effective": effective_states, "eligible": eligible,
            "max_active": document["workers"]["max_active"] if document else 0,
            "errors": errors, "policy_revision": policy_revision,
            "waste_governor": governor, "provenance": provenance}


def effective(project: Path, *, personal: Path | None = None) -> dict:
    snapshot = load(project, personal=personal)
    return {"schema": SCHEMA, "policy": {"waste_governor": snapshot["waste_governor"]},
            "revision": snapshot["policy_revision"], "preference_revision": snapshot["revision"],
            "preferences": snapshot, "provenance": snapshot["provenance"]}


def _encode(document: dict) -> bytes:
    data = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
    if len(data) > 64 * 1024:
        raise PodError("yaml_resource_limit", "Configuration exceeds its size limit")
    return data


def _replace(path: Path, data: bytes) -> None:
    _safe_config_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pod-config-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
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
        document = deepcopy(DEFAULT)
        _replace(path, _encode(document))
        return document


def _surgical(raw: bytes, document: dict, *, model_id: str | None, state: str | None,
              mode: str | None) -> tuple[bytes, bool]:
    text = raw.decode("utf-8")
    changed = text
    if mode is not None:
        changed, count = re.subn(r"(?m)^([ \t]*selection:[ \t]*)(?:custom|all)([ \t]*(?:#.*)?)$",
                                 lambda match: match[1] + mode + match[2], changed, count=1)
        if count != 1:
            return _encode(document), False
    if model_id is not None:
        changed, count = re.subn(rf"(?m)^([ \t]{{2}}{re.escape(model_id)}:[ \t]*)(?:preferred|available|disabled)([ \t]*(?:#.*)?)$",
                                 lambda match: match[1] + state + match[2], changed, count=1)
        if count != 1:
            return _encode(document), False
    encoded = changed.encode("utf-8")
    try:
        if _parse(encoded) == document:
            return encoded, True
    except PodError:
        pass
    return _encode(document), False


def _save(path: Path, *, model_id: str | None = None, state: str | None = None,
          mode: str | None = None, displayed: dict) -> dict:
    with _edit_lock(path):
        raw = _read_bytes(path)
        if raw is None:
            raise PodError("config_missing", "Personal preferences are missing")
        document = _parse(raw)
        if model_id is not None:
            if document["models"].get(model_id) != displayed["saved"].get(model_id):
                raise PodError("config_changed_elsewhere", "Changed elsewhere — press again")
        elif document["selection"] != displayed["mode"]:
            raise PodError("config_changed_elsewhere", "Changed elsewhere — press again")
        next_mode = mode
        if model_id is not None and document["selection"] == "all":
            next_mode = "custom"
        if next_mode is not None:
            document["selection"] = next_mode
        if model_id is not None:
            document["models"][model_id] = state
        validate(document)
        encoded, preserved = _surgical(raw, document, model_id=model_id, state=state, mode=next_mode)
        _replace(path, encoded)
    return {"path": str(path.resolve(strict=False)), "revision": hashlib.sha256(encoded).hexdigest(),
            "mode": document["selection"], "saved": document["models"].copy(),
            "notice": "" if preserved else "comments not preserved",
            "mode_notice": "Returned to My selection" if model_id is not None and next_mode == "custom" and displayed["mode"] == "all" else ""}


def set_model(path: Path, model_id: str, state: str, *, displayed: dict) -> dict:
    if model_id not in IDS or state not in STATES:
        raise PodError("invalid_config_edit", "Model id or state is unsupported")
    return _save(path, model_id=model_id, state=state, displayed=displayed)


def set_mode(path: Path, mode: str, *, displayed: dict) -> dict:
    if mode not in MODES:
        raise PodError("invalid_config_edit", "Selection mode is unsupported")
    return _save(path, mode=mode, displayed=displayed)

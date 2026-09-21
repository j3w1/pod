"""Bounded YAML preferences and restrictive effective-policy merge."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
from typing import Any

import yaml

from .errors import PodError
from .util import digest, exact, explicit_home, native_home

SCHEMA = "pod/v1"
DEFAULT_WORKER_CAPACITY = 2
COMPLEXITIES = ("trivial", "simple", "standard", "complex", "very_complex")
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
MODEL_FIELDS = {"agent", "model", "account", "approved", "approval_ref", "approval_route", "billing", "efforts", "capabilities", "locations"}
POLICY_FIELDS = {"max_workers", "ordinary_max", "allowed_agents", "allowed_accounts", "allowed_locations", "quota_low", "quota_critical", "quota_fresh_seconds", "retain_idle_minutes", "child_delegation", "review", "spending_grants", "reset_grants", "exceptional_grants"}
ROUTE_FIELDS = {"model", "effort", "strict"}
STARTER = {
    "luna": {"agent": "codex", "model": "gpt-5.6-luna", "approved": False, "billing": "unknown"},
    "sonnet": {"agent": "claude", "model": "sonnet", "approved": False, "billing": "unknown"},
    "terra": {"agent": "codex", "model": "gpt-5.6-terra", "approved": False, "billing": "unknown"},
    "sol": {"agent": "codex", "model": "gpt-5.6-sol", "approved": False, "billing": "unknown"},
    "astra": {"agent": "codex", "model": "gpt-6-astra", "approved": False, "billing": "unknown"},
    "opus": {"agent": "claude", "model": "opus", "approved": False, "billing": "unknown"},
    "fable": {"agent": "claude", "model": "fable", "approved": False, "billing": "unknown"},
}
DEFAULT = {
    "schema": SCHEMA,
    "models": STARTER,
    "routing": {
        "trivial": {"model": "luna", "effort": "low"},
        "simple": {"model": "sonnet", "effort": "medium"},
        "standard": {"model": "terra", "effort": "medium"},
        "complex": {"model": "sol", "effort": "high"},
        "very_complex": {"model": "astra", "effort": "high"},
    },
    "policy": {
        # The normal starting capacity is DEFAULT_WORKER_CAPACITY. These are
        # hard ordinary ceilings; a scoped personal grant is needed above 3.
        "max_workers": 3, "ordinary_max": 3, "quota_low": 20,
        "quota_critical": 5, "quota_fresh_seconds": 60,
        "retain_idle_minutes": 30, "child_delegation": False,
        "review": "independent",
        "spending_grants": [], "reset_grants": [], "exceptional_grants": [],
    },
}


def route_identity(model: dict) -> str:
    return digest({key: model.get(key) for key in ("agent", "model", "account")})


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
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > 128:
                raise PodError("yaml_invalid_key", "Configuration key is invalid")
            _shape(v, depth + 1, counter)
    elif isinstance(value, list):
        for v in value:
            _shape(v, depth + 1, counter)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise PodError("yaml_invalid_type", "Configuration contains an unsupported value")
    elif isinstance(value, str) and len(value) > 4096:
        raise PodError("yaml_resource_limit", "Configuration scalar exceeds limit")


def read_yaml(path: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise PodError("unsafe_config", "Configuration must be a bounded regular file")
    try:
        value = yaml.load(path.read_bytes().decode("utf-8"), Loader=_StrictLoader)
    except PodError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise PodError("invalid_yaml", "Configuration cannot be safely decoded") from exc
    _shape(value)
    return validate(value)


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v or len(v) > 128 for v in value) or len(value) > 64 or len(set(value)) != len(value):
        raise PodError("invalid_config", f"{name} must be a bounded unique string list")
    return value


def validate(value: Any) -> dict:
    obj = exact(value, {"schema", "models", "routing", "policy", "context"}, {"schema"}, name="config")
    if obj["schema"] != SCHEMA:
        raise PodError("invalid_config", "Unsupported configuration schema")
    models = obj.get("models", {})
    if not isinstance(models, dict) or len(models) > 64:
        raise PodError("invalid_config", "models must be a bounded mapping")
    for alias, raw in models.items():
        if not isinstance(alias, str) or len(alias) > 64:
            raise PodError("invalid_config", "Invalid model alias")
        m = exact(raw, MODEL_FIELDS, name="model")
        if "agent" in m and m["agent"] not in ("codex", "claude"):
            raise PodError("invalid_config", "Unsupported agent")
        for field in ("model", "account", "approval_ref", "approval_route"):
            if field in m and (not isinstance(m[field], str) or not m[field] or len(m[field]) > 256):
                raise PodError("invalid_config", f"Invalid {field}")
        if "approved" in m and not isinstance(m["approved"], bool):
            raise PodError("invalid_config", "approved must be a boolean")
        if m.get("approved") and (not m.get("approval_ref") or not all(m.get(key) for key in ("agent", "model", "account"))
                                   or m.get("approval_route") != route_identity(m)):
            raise PodError("invalid_config", "Approval must bind the exact agent/model/account route")
        if "billing" in m and m["billing"] not in ("included", "paid", "unknown"):
            raise PodError("invalid_config", "Invalid billing class")
        for field in ("efforts", "capabilities", "locations"):
            if field in m:
                _strings(m[field], field)
        if "efforts" in m and not set(m["efforts"]) <= EFFORTS:
            raise PodError("invalid_config", "Unsupported effort")
    routing = obj.get("routing", {})
    if not isinstance(routing, dict) or set(routing) - set(COMPLEXITIES):
        raise PodError("invalid_config", "Invalid routing rows")
    for row in routing.values():
        r = exact(row, ROUTE_FIELDS, name="route")
        if "model" in r and (not isinstance(r["model"], str) or not r["model"]):
            raise PodError("invalid_config", "Invalid routing model")
        if "effort" in r and r["effort"] not in EFFORTS:
            raise PodError("invalid_config", "Invalid routing effort")
        if "strict" in r and not isinstance(r["strict"], bool):
            raise PodError("invalid_config", "strict must be boolean")
    policy = obj.get("policy", {})
    if not isinstance(policy, dict) or set(policy) - POLICY_FIELDS:
        raise PodError("invalid_config", "Invalid policy fields")
    for field in ("max_workers", "ordinary_max", "quota_low", "quota_critical", "quota_fresh_seconds", "retain_idle_minutes"):
        if field in policy and (type(policy[field]) is not int or policy[field] < 0 or policy[field] > 3600):
            raise PodError("invalid_config", f"Invalid {field}")
    if "max_workers" in policy and not 0 <= policy["max_workers"] <= 8:
        raise PodError("invalid_config", "max_workers exceeds eight")
    if "ordinary_max" in policy and policy["ordinary_max"] > 3:
        raise PodError("invalid_config", "ordinary_max exceeds three")
    for field in ("allowed_agents", "allowed_accounts", "allowed_locations"):
        if field in policy:
            _strings(policy[field], field)
    if "child_delegation" in policy and not isinstance(policy["child_delegation"], bool):
        raise PodError("invalid_config", "child_delegation must be boolean")
    if "review" in policy and policy["review"] not in ("independent", "project_stricter"):
        raise PodError("invalid_config", "Invalid review policy")
    for field in ("spending_grants", "reset_grants", "exceptional_grants"):
        if field in policy:
            if not isinstance(policy[field], list) or len(policy[field]) > 32:
                raise PodError("invalid_config", f"Invalid {field}")
            grant_ids = [grant.get("id") for grant in policy[field] if isinstance(grant, dict)]
            if len(grant_ids) != len(set(grant_ids)):
                raise PodError("invalid_config", f"{field} grant ids must be unique")
            for grant in policy[field]:
                exact(grant, {"id", "action", "account", "bucket", "model", "objective", "run", "plan_revision", "limit", "reason", "valid_until", "max_units"}, {"id", "action", "account", "valid_until"}, name="grant")
                for required_string in ("id", "action", "account", "valid_until"):
                    if not isinstance(grant[required_string], str) or not grant[required_string]:
                        raise PodError("invalid_config", "Grant identity and validity must be strings")
                try:
                    expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
                except ValueError as exc:
                    raise PodError("invalid_config", "Grant validity is not an ISO timestamp") from exc
                if expiry.tzinfo is None:
                    raise PodError("invalid_config", "Grant validity needs a timezone")
                if field == "spending_grants":
                    if grant["action"] not in ("paid_usage", "premium_mode") or not isinstance(grant.get("objective"), str) or not isinstance(grant.get("model"), str) or type(grant.get("max_units")) is not int or grant["max_units"] <= 0:
                        raise PodError("invalid_config", "Spending grant needs exact action, objective and bound")
                elif field == "reset_grants":
                    if grant["action"] != "reset_credit" or not isinstance(grant.get("bucket"), str) or type(grant.get("max_units")) is not int or grant["max_units"] != 1:
                        raise PodError("invalid_config", "Reset grant needs exact bucket and one credit")
                else:
                    if grant["action"] != "exceptional_capacity" or any(not isinstance(grant.get(key), str) or not grant[key].strip() for key in ("objective", "run", "plan_revision", "reason")) or type(grant.get("limit")) is not int or not 4 <= grant["limit"] <= 8:
                        raise PodError("invalid_config", "Exceptional grant needs objective, Run, plan and limit")
    if "context" in obj:
        exact(obj["context"], {"references", "checks"}, name="context")
        for field in obj["context"]:
            _strings(obj["context"][field], field)
    return obj


def personal_path(project: Path | None = None) -> Path:
    override = explicit_home("POD_CONFIG_HOME")
    if override is not None:
        return override / "config.yaml"
    if os.name == "nt":
        if "APPDATA" not in os.environ:
            raise PodError("config_home_unavailable", "APPDATA is required for personal configuration")
        return native_home("APPDATA", project=project) / "pod" / "config.yaml"
    return native_home("XDG_CONFIG_HOME", default=Path.home() / ".config",
                       project=project) / "pod" / "config.yaml"


def _merge(base: dict, layer: dict, scope: str, provenance: dict) -> None:
    for alias, model in layer.get("models", {}).items():
        if scope != "personal":
            old = base["models"].get(alias)
            if old is None or any(key in model and model[key] != old.get(key) for key in ("agent", "model", "account", "approved", "approval_ref", "approval_route", "billing")):
                raise PodError("authority_expansion", "Project/task model identity or approval cannot expand personal authority")
            for key in ("efforts", "capabilities", "locations"):
                if key in model and key in old and not set(model[key]) <= set(old[key]):
                    raise PodError("authority_expansion", f"{key} cannot broaden personal restrictions")
            base["models"][alias].update(model)
        else:
            base["models"][alias] = {**base["models"].get(alias, {}), **model}
        provenance[f"models.{alias}"] = scope
    for complexity, row in layer.get("routing", {}).items():
        prior_row = base["routing"][complexity]
        if (scope != "personal" and prior_row.get("strict") is True
                and (row.get("strict") is False
                     or ("model" in row and row["model"] != prior_row.get("model")))):
            raise PodError("authority_expansion", "Project/task routing cannot weaken or replace a strict pin")
        base["routing"][complexity].update(row)
        provenance[f"routing.{complexity}"] = scope
    for key, val in layer.get("policy", {}).items():
        prior = base["policy"].get(key)
        if scope != "personal":
            if key in ("spending_grants", "reset_grants", "exceptional_grants") and val:
                raise PodError("authority_expansion", "Local grants cannot create authority")
            if key in ("max_workers", "ordinary_max") and val > prior:
                raise PodError("authority_expansion", "Local capacity cannot exceed personal ceiling")
            if key in ("allowed_agents", "allowed_accounts", "allowed_locations") and prior is not None and not set(val) <= set(prior):
                raise PodError("authority_expansion", "Local list cannot broaden personal restriction")
            if key == "child_delegation" and val and not prior:
                raise PodError("authority_expansion", "Local policy cannot grant delegation")
            if key in ("quota_low", "quota_critical") and val < prior:
                raise PodError("authority_expansion", "Local quota threshold cannot weaken personal policy")
            if key == "quota_fresh_seconds" and val > prior:
                raise PodError("authority_expansion", "Local quota freshness cannot exceed personal policy")
            if key == "review" and prior == "project_stricter" and val != prior:
                raise PodError("authority_expansion", "Local review cannot weaken personal policy")
            if key in ("spending_grants", "reset_grants", "exceptional_grants"):
                val = [g for g in prior if g in val]
        base["policy"][key] = val
        provenance[f"policy.{key}"] = scope
    if "context" in layer:
        base["context"] = layer["context"]
        provenance["context"] = scope


def effective(project: Path, *, personal: Path | None = None, task: dict | None = None) -> dict:
    base = deepcopy(DEFAULT)
    provenance = {"defaults": "pending recommendations"}
    for scope, layer in (
        ("personal", read_yaml(personal or personal_path(project))),
        ("project", read_yaml(project / ".pod" / "config.yaml")),
        ("task", validate(task) if task is not None else None),
    ):
        if layer is not None:
            _merge(base, layer, scope, provenance)
    for row in base["routing"].values():
        if row["model"] not in base["models"]:
            raise PodError("invalid_route", "Routing references an unknown model alias")
    return {"schema": SCHEMA, "policy": base, "provenance": provenance, "revision": digest(base)}

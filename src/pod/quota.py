"""Metadata-only quota and credit guards; no inference or redemption transport."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .errors import PodError
from .util import bounded_text, exact


def validate_snapshot(value: Any) -> dict:
    s = exact(value, {"schema", "provider", "account", "bucket", "windows",
                      "consumption", "remaining_percent", "reset_at", "observed_at",
                      "source", "confidence", "unknowns"},
              {"schema", "provider", "account", "bucket", "windows",
               "observed_at", "source", "confidence", "unknowns"}, name="quota")
    if s["schema"] != "pod-quota/v1":
        raise PodError("invalid_quota", "Unsupported quota schema")
    for key in ("provider", "account", "bucket", "observed_at", "source", "confidence"):
        bounded_text(s[key], name=key, limit=256)
    if not isinstance(s["windows"], list) or len(s["windows"]) > 16:
        raise PodError("invalid_quota", "Quota windows must be bounded")
    names = set()
    for window in s["windows"]:
        exact(window, {"name", "remaining_percent", "reset_at", "consumed"},
              {"name"}, name="quota_window")
        bounded_text(window["name"], name="quota window", limit=128)
        if window["name"] in names:
            raise PodError("invalid_quota", "Duplicate quota window")
        names.add(window["name"])
        if "remaining_percent" in window and (type(window["remaining_percent"]) not in (int, float)
                or not 0 <= window["remaining_percent"] <= 100):
            raise PodError("invalid_quota", "Invalid window remainder")
    if "remaining_percent" in s and (type(s["remaining_percent"]) not in (int, float)
            or not 0 <= s["remaining_percent"] <= 100):
        raise PodError("invalid_quota", "Invalid aggregate remainder")
    if not isinstance(s["unknowns"], list) or len(s["unknowns"]) > 32:
        raise PodError("invalid_quota", "Quota unknowns must be bounded")
    return s


def reset_intent(grant: dict, *, account: str, bucket: str, operation_id: str,
                 supported_idempotency: bool, now: datetime) -> dict:
    exact(grant, {"id", "action", "account", "bucket", "valid_until", "max_units"},
          {"id", "action", "account", "bucket", "valid_until", "max_units"}, name="grant")
    if grant["action"] != "reset_credit" or grant["account"] != account or grant["bucket"] != bucket:
        raise PodError("reset_not_granted", "Reset grant does not bind this account and bucket")
    if type(grant["max_units"]) is not int or grant["max_units"] != 1:
        raise PodError("reset_not_granted", "Reset grant must bound exactly one credit")
    try:
        expiry = datetime.fromisoformat(grant["valid_until"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PodError("reset_not_granted", "Reset grant validity is invalid") from exc
    if expiry.tzinfo is None or now.tzinfo is None or now > expiry or not supported_idempotency:
        raise PodError("reset_unsupported", "Reset validity or native idempotency is unproven")
    bounded_text(operation_id, name="operation_id", limit=128)
    return {"schema": "pod-reset-intent/v1", "operation_id": operation_id,
            "grant_id": grant["id"], "account": account, "bucket": bucket,
            "state": "reserved", "repeat_allowed": False, "readback_required": True}

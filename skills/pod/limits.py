"""Pure soft-limit values, bounded by fixed safety ceilings.

This module does not read or write configuration, authorize an edit, move history
or start continuity. Callers must obtain their configuration through the existing
validated personal-config and decision boundaries.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType

from .errors import PodError


# Safety ceilings are finite source policy, never configurable values. Each is
# eight times its default soft limit; none is an allowance for native work.
HARD_LIMITS = MappingProxyType({
    "record_bytes": 4 * 1024 * 1024,
    "context_bytes": 16 * 1024 * 1024,
    "governance_history": 8192,
    "findings": 1024,
    "successor_chain": 512,
})


@dataclass(frozen=True, slots=True)
class Limits:
    record_bytes: int = 512 * 1024
    context_bytes: int = 2 * 1024 * 1024
    governance_history: int = 1024
    findings: int = 128
    successor_chain: int = 64

    def __post_init__(self):
        for field, ceiling in HARD_LIMITS.items():
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise PodError(
                    "invalid_limits",
                    f"limits.{field} must be an integer from 1 through {ceiling}",
                    {"field": field, "hard_limit": ceiling},
                )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


DEFAULT_LIMITS = Limits()
HARD_RECEIPTS = 1024
HARD_MAP_HISTORY = 8192
HARD_LOGICAL_BYTES = 16 * HARD_LIMITS["context_bytes"]
_CURRENT = ContextVar("pod_limits_snapshot", default=None)
_CONFIG_PATH = ContextVar("pod_limits_config_path", default=None)
_CONFIG_ERROR = ContextVar("pod_limits_config_error", default=None)


def parse_limits(value: object) -> Limits:
    """Validate an explicit limits object; omitted fields keep their defaults.

    An absent optional config key should be passed as an empty dict. Explicit
    null, coercions and arbitrary new keys are not an instruction to use defaults.
    The caller's mapping is never modified or retained.
    """
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or key not in HARD_LIMITS for key in value
    ):
        raise PodError("invalid_limits", "limits must map only the supported limit names")
    return Limits(**{**DEFAULT_LIMITS.as_dict(), **value})


def current() -> Limits:
    """One operation's validated snapshot, or source defaults for pure callers."""
    return _CURRENT.get() or DEFAULT_LIMITS


def _unavailable(code, path):
    raise PodError(code, f"Personal limits are unavailable ({code}): {path}",
                   {"path": str(path), "next_action":
                    f"Read and preserve {path}; correct its {code} configuration error before mutation"})


@contextmanager
def using_limits(limits: Limits):
    if not isinstance(limits, Limits):
        raise PodError("invalid_limits", "A limits snapshot must be validated")
    token = _CURRENT.set(limits)
    try:
        yield limits
    finally:
        _CURRENT.reset(token)


@contextmanager
def configured(project=None, *, strict=True):
    """Freeze the existing personal authority for a whole supported operation.

    Nested helpers share it rather than mixing multiple configuration reads.
    Diagnostic callers can still report an invalid file; mutating helpers refuse
    malformed limits instead of silently substituting more permissive defaults.
    """
    if _CURRENT.get() is not None:
        if strict and _CONFIG_ERROR.get() is not None:
            _unavailable(_CONFIG_ERROR.get(), _CONFIG_PATH.get())
        yield current()
        return
    from .config import load
    snapshot = load(project)
    if strict and snapshot["limits_status"] == "unavailable":
        error = snapshot["errors"][0] if snapshot["errors"] else {"code": "invalid_limits"}
        _unavailable(error["code"], snapshot["path"])
    token = _CONFIG_PATH.set(Path(snapshot["path"]))
    error_token = _CONFIG_ERROR.set(snapshot["errors"][0]["code"] if snapshot["limits_status"] == "unavailable" and snapshot["errors"] else None)
    try:
        with using_limits(parse_limits(snapshot["limits"])) as limits:
            yield limits
    finally:
        _CONFIG_ERROR.reset(error_token)
        _CONFIG_PATH.reset(token)


def require_current():
    """A limits change during an operation cannot silently alter its publication."""
    path = _CONFIG_PATH.get()
    if path is None:
        return
    from .config import _read_bytes, _decode, SCHEMA, V1_SCHEMA
    raw = _read_bytes(path)
    document = _decode(raw) if raw is not None else None
    if document is None and current() == DEFAULT_LIMITS:
        return
    if (not isinstance(document, dict) or document.get("schema") not in (SCHEMA, V1_SCHEMA)
            or parse_limits(document.get("limits", {})) != current()):
        raise PodError("limits_changed", "Personal limits changed during this operation; read current configuration before retrying")

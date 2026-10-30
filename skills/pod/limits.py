"""Pure soft-limit values, bounded by fixed safety ceilings.

This module does not read or write configuration, authorize an edit, move history
or start continuity. Callers must obtain their configuration through the existing
validated personal-config and decision boundaries.
"""

from dataclasses import asdict, dataclass
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

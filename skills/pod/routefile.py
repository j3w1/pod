"""Read the personal preference file and resolve dispatches. Coordination reads this and nothing else about models.

The file is the only authority for enabled routes, Preferred, Pin, profiles, permitted review
agents, paused or soft-drained agents and who may authorize a merge. Nothing here writes it.
A file that cannot be read safely yields no eligible route and says why; it is never repaired
or replaced.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import functools
import json
from pathlib import Path
import re

from . import paths, yamlsubset
from .errors import PodError
from .state import ROLES

STATES = ("enabled", "disabled")
AUTHORIZERS = ("person", "coordinator")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_SUFFIX_AGENTS = frozenset({"antigravity"})
REGISTRY_PATH = Path(__file__).resolve().parent / "models" / "registry.json"
_AGENT_DEFAULT_FAMILY = {"claude": "Anthropic", "codex": "OpenAI", "antigravity": "Google"}
_AGENT_DEFAULT_POOL = {"claude": "claude-max", "codex": "codex", "antigravity": "agy-gemini"}
AGENT = re.compile(r"[a-z][a-z0-9_-]*")
PROFILE = re.compile(r"[a-z][a-z0-9_-]*")
ROUTE = re.compile(rf"{AGENT.pattern}/[A-Za-z0-9][A-Za-z0-9._-]*/[a-z][a-z0-9_-]*")
AGENT_LIST = re.compile(rf"\[ *(?:{AGENT.pattern}(?: *, *{AGENT.pattern})* *)?\]", re.ASCII)
_TTL = re.compile(r"^([1-9][0-9]*)([smhd]?)$")
_TTL_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_QUOTA_HOLD_TERMS = re.compile(
    r"(?:quota|rate[ _-]?limit|429|limit(?:ed|s)?|exhaust(?:ed)?|cap(?:ped)?|budget|credit|usage|"
    r"throttl\w*|weekly|daily|monthly|pct|%|pool|drain)",
    re.IGNORECASE,
)
MAX_BYTES = 256 * 1024
EDIT = "run `pod models` to choose routes, or `pod config edit` to correct the file"


@dataclass(frozen=True)
class Profile:
    name: str
    role: str
    mcp: bool
    routes: tuple[str, ...]


@dataclass(frozen=True)
class AgentHold:
    agent: str
    reason: str | None = None
    until: str | None = None      # ISO-8601 UTC timestamp e.g. 2026-11-26T04:00:00Z
    ttl: int | None = None        # positive integer seconds

    @property
    def has_metadata(self) -> bool:
        return bool(self.reason or self.until or self.ttl is not None)


@dataclass(frozen=True)
class TraceStep:
    route: str
    status: str        # selected, skipped or blocked
    code: str
    reason: str


@dataclass(frozen=True)
class Resolution:
    ok: bool
    role: str | None
    route: str | None
    wire: tuple[str, str, str | None] | None
    profile: str | None
    mcp: bool
    review_independence: str | None
    trace: list[TraceStep]
    error_code: str | None = None
    error_message: str | None = None
    error_fix: str | None = None
    author_families: tuple[str, ...] = ()
    reviewer_notes: tuple[str, ...] = ()
    reason: str | None = None
    review_fallback_reason: str | None = None


@dataclass
class Preferences:
    path: Path
    status: str = "absent"                 # ok, absent or invalid
    problem: str | None = None
    routes: dict = field(default_factory=dict)   # "agent/model/effort" -> "enabled" | "disabled"
    preferred: str | None = None
    pinned: str | None = None
    merge_authorization: str = "person"
    authorization_verifier: list | None = None
    review_agents: list[str] = field(default_factory=list)
    paused_agents: list[str] = field(default_factory=list)
    paused_meta: dict[str, AgentHold] = field(default_factory=dict)
    soft_drained_agents: list[str] = field(default_factory=list)
    soft_drained_meta: dict[str, AgentHold] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    default_profiles: dict[str, str] = field(default_factory=dict)

    def enabled(self) -> list[str]:
        return [key for key, state in self.routes.items() if state == "enabled"]

    def check(self, route: str) -> None:
        """Refuse a route the person has not enabled, or any route but the Pin while one is set."""
        if self.status != "ok":
            raise PodError("routes_unavailable", f"No route is eligible: {self.problem or 'no preference file'}.",
                           fix=EDIT)
        if not ROUTE.fullmatch(route or ""):
            raise PodError("route_invalid", f"{route!r} is not an agent/model/effort route.",
                           fix="name one enabled route, for example " + (self.enabled() or ["agent/model/effort"])[0])
        if self.pinned and route != self.pinned:
            raise PodError("route_not_pinned", f"The Pin is {self.pinned}; {route} cannot be used while it is set.",
                           fix=f"use --route {self.pinned}, or clear the Pin in `pod models`")
        if self.routes.get(route) != "enabled":
            raise PodError("route_not_enabled", f"{route} is not enabled.",
                           fix="use one of: " + (", ".join(self.enabled()) or "none enabled; " + EDIT))


@dataclass(frozen=True)
class _ModelInfo:
    agent: str
    id: str
    provider: str
    pool: str
    efforts: tuple[str, ...]
    wire: str


@functools.lru_cache(maxsize=1)
def _registry_index() -> dict[tuple[str, str], _ModelInfo]:
    """Read model metadata from shipped registry.json directly without importing pod.models."""
    try:
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(document, dict):
        return {}
    index: dict[tuple[str, str], _ModelInfo] = {}
    for entry in document.get("models") or ():
        if not isinstance(entry, dict):
            continue
        agent, model_id = entry.get("agent"), entry.get("id")
        provider, pool = entry.get("provider"), entry.get("pool")
        efforts = entry.get("efforts") or ()
        wire = entry.get("wire", "effort-flag")
        if isinstance(agent, str) and isinstance(model_id, str) and isinstance(provider, str):
            index[(agent, model_id)] = _ModelInfo(
                agent=agent,
                id=model_id,
                provider=provider,
                pool=pool if isinstance(pool, str) else _AGENT_DEFAULT_POOL.get(agent, agent),
                efforts=tuple(item for item in efforts if isinstance(item, str)),
                wire=wire if isinstance(wire, str) else "effort-flag",
            )
    return index


def split(route: str) -> tuple[str, str, str]:
    agent, model, effort = route.split("/")
    return agent, model, effort


def _route_parts(route: str) -> tuple[str, str | None]:
    """Extract (agent, model | None) from a 1-, 2-, or 3-part route without raising ValueError."""
    parts = (route or "").split("/")
    agent = parts[0]
    model = parts[1] if len(parts) >= 2 and parts[1] else None
    return agent, model


def route_family(route: str) -> str:
    """Model family (provider: Anthropic, OpenAI, Google) for a route (2- or 3-part) or agent name."""
    agent, model = _route_parts(route)
    if model is None:
        return _AGENT_DEFAULT_FAMILY.get(agent, agent)
    info = _registry_index().get((agent, model))
    if info is not None:
        return info.provider
    if agent == "antigravity":
        if model.startswith("gemini-"):
            return "Google"
        if model.startswith("claude-"):
            return "Anthropic"
        return f"{agent}/{model}"
    return _AGENT_DEFAULT_FAMILY.get(agent, agent)


def route_pool(route: str) -> str:
    """Quota pool id (claude-max, codex, agy-gemini, agy-claude) for a route (2- or 3-part) or agent name."""
    agent, model = _route_parts(route)
    if model is None:
        return _AGENT_DEFAULT_POOL.get(agent, agent)
    info = _registry_index().get((agent, model))
    if info is not None:
        return info.pool
    if agent == "antigravity":
        if model.startswith("gemini-"):
            return "agy-gemini"
        if model.startswith("claude-"):
            return "agy-claude"
        return agent
    return _AGENT_DEFAULT_POOL.get(agent, agent)


def wire_route(route: str) -> tuple[str, str, str | None]:
    """Translate a canonical agent/model/effort route to Orca CLI (agent, model, effort | None).

    Refuses efforts not listed by the shipped registry for a known `(agent, model)`.
    Model-suffix routes (such as Antigravity) embed effort in the model id and omit `--effort`.
    """
    agent, model, effort = split(route)
    info = _registry_index().get((agent, model))
    if info is not None:
        if effort not in info.efforts:
            supported = ", ".join(info.efforts)
            raise PodError(
                "effort_not_supported",
                f"{route} uses effort {effort!r}, which {agent}/{model} does not support (supported: {supported}).",
                fix=f"use one of the supported efforts for {agent}/{model}: {supported}",
            )
        use_suffix = info.wire == "model-suffix"
    else:
        if effort not in EFFORTS:
            raise PodError(
                "effort_not_supported",
                f"{route} uses unsupported effort {effort!r}.",
                fix=f"use one of: {', '.join(EFFORTS)}",
            )
        use_suffix = agent in MODEL_SUFFIX_AGENTS
    if use_suffix:
        if model.endswith(f"-{effort}"):
            return agent, model, None
        return agent, f"{model}-{effort}", None
    return agent, model, effort


def canonical_effective(effective: dict | str | None) -> str | None:
    """Normalize Orca's launch.effective back to canonical 3-part agent/model/effort."""
    if not effective:
        return None
    if isinstance(effective, str):
        if ROUTE.fullmatch(effective):
            agent, model, effort = split(effective)
            if agent in MODEL_SUFFIX_AGENTS and model.endswith(f"-{effort}"):
                return f"{agent}/{model[:-len(effort) - 1]}/{effort}"
        return effective
    if not isinstance(effective, dict):
        return None
    agent = effective.get("agent")
    model = effective.get("model")
    effort = effective.get("effort")
    if not agent or not model:
        return None
    agent_s, model_s = str(agent), str(model)
    effort_s = str(effort) if effort not in (None, "") else None
    if agent_s in MODEL_SUFFIX_AGENTS:
        if effort_s:
            if model_s.endswith(f"-{effort_s}"):
                model_s = model_s[:-len(effort_s) - 1]
            return f"{agent_s}/{model_s}/{effort_s}"
        base, sep, suffix = model_s.rpartition("-")
        if sep and base and suffix in EFFORTS:
            return f"{agent_s}/{base}/{suffix}"
        return f"{agent_s}/{model_s}"
    return f"{agent_s}/{model_s}/{effort_s}"


def parse_ttl(raw: str | int) -> int:
    """Parse a positive TTL in seconds, or with suffix s/m/h/d (for example 1800, 30m, 2h)."""
    if type(raw) is int:
        if raw <= 0:
            raise ValueError("ttl must be a positive duration")
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"invalid ttl {raw!r}")
    match = _TTL.fullmatch(raw.strip().lower())
    if not match:
        raise ValueError(f"invalid ttl {raw!r}; use seconds or s/m/h/d such as 30m or 1800")
    return int(match.group(1)) * _TTL_UNITS[match.group(2)]


def _parse_iso_utc(stamp: str) -> datetime:
    if not isinstance(stamp, str) or not stamp.strip():
        raise ValueError("until must be an ISO-8601 UTC timestamp")
    parsed = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"until timestamp {stamp!r} must include a timezone or Z")
    return parsed.astimezone(timezone.utc)


def parse_agent_hold(agent: str, spec: object) -> AgentHold:
    """Parse one `agent: [reason] [ttl=SECONDS] [until=ISO]` mapping entry."""
    if not AGENT.fullmatch(agent or ""):
        raise ValueError(f"{agent!r} is not a valid agent name")
    if spec is None or spec is True:
        return AgentHold(agent=agent)
    if not isinstance(spec, str):
        raise ValueError(f"hold metadata for {agent} must be text or null")
    text = spec.strip()
    if not text:
        return AgentHold(agent=agent)
    reason_parts: list[str] = []
    until: str | None = None
    ttl: int | None = None
    for token in text.split():
        if token.startswith("until="):
            raw_until = token[len("until="):]
            parsed = _parse_iso_utc(raw_until)
            until = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
        elif token.startswith("ttl="):
            ttl = parse_ttl(token[len("ttl="):])
        else:
            reason_parts.append(token)
    reason = " ".join(reason_parts).strip() or None
    return AgentHold(agent=agent, reason=reason, until=until, ttl=ttl)


def format_agent_hold(hold: AgentHold) -> str:
    """Serialize an AgentHold's metadata value for a 1-level YAML mapping."""
    parts = []
    if hold.reason:
        parts.append(" ".join(hold.reason.split()))
    if hold.ttl is not None:
        parts.append(f"ttl={hold.ttl}")
    if hold.until:
        parts.append(f"until={hold.until}")
    return " ".join(parts) if parts else "null"


def hold_summary(hold: AgentHold) -> str:
    """Compact human-readable description of an AgentHold."""
    details = []
    if hold.reason:
        details.append(hold.reason)
    if hold.until:
        details.append(f"until {hold.until}")
    elif hold.ttl is not None:
        details.append(f"ttl {hold.ttl}s")
    return f"{hold.agent} ({', '.join(details)})" if details else hold.agent


def hold_as_dict(hold: AgentHold) -> dict:
    return {"agent": hold.agent, "reason": hold.reason, "until": hold.until, "ttl": hold.ttl}


def active_holds(holds: dict[str, AgentHold], *, agents: list[str] | tuple[str, ...] = (),
                 now: datetime | None = None) -> dict[str, AgentHold]:
    """Return holds that are currently in effect (dropping any whose `until` timestamp has elapsed)."""
    current = datetime.now(timezone.utc) if now is None else (
        now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc))
    source = dict(holds)
    for agent in agents:
        source.setdefault(agent, AgentHold(agent=agent))
    result: dict[str, AgentHold] = {}
    for agent, hold in source.items():
        if hold.until:
            try:
                if current >= _parse_iso_utc(hold.until):
                    continue
            except ValueError:
                pass
        result[agent] = hold
    return result


def valid_profile_name(name: object) -> bool:
    return isinstance(name, str) and bool(PROFILE.fullmatch(name))


@functools.lru_cache(maxsize=4)
def _read_shipped_default_profiles(target: Path) -> dict[str, str]:
    """Read validated shipped role defaults from registry.json directly without importing pod.models."""
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(document, dict):
        return {}
    defaults = document.get("defaults")
    if not isinstance(defaults, dict):
        return {}
    raw_profiles = defaults.get("profiles")
    if not isinstance(raw_profiles, dict):
        return {}
    shipped_profiles: dict[str, Profile] = {}
    for name, spec in raw_profiles.items():
        try:
            shipped_profiles[name] = parse_profile(name, spec)
        except ValueError:
            continue
    raw = defaults.get("default_profiles")
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, str] = {}
    for role, pname in raw.items():
        if role not in ROLES or not valid_profile_name(pname):
            continue
        prof = shipped_profiles.get(pname)
        if prof is not None and prof.role == role:
            resolved[role] = pname
    return resolved


def _shipped_default_profiles(path: Path | None = None) -> dict[str, str]:
    return _read_shipped_default_profiles(REGISTRY_PATH if path is None else path)


def parse_profile(name: str, spec: object) -> Profile:
    """Parse one flat profile line: `<role>[+mcp] <route1> [<route2> ...]`."""
    if not valid_profile_name(name):
        raise ValueError(f"profiles.{name!r} must be a lowercase profile name")
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"profiles.{name} must name a role and at least one route")
    parts = spec.split()
    if len(parts) < 2:
        raise ValueError(f"profiles.{name} must name a role and at least one route")
    head = parts[0]
    if head.endswith("+mcp"):
        role, mcp = head[:-4], True
    else:
        role, mcp = head, False
    if role not in ROLES:
        raise ValueError(f"profiles.{name} role {head!r} must be one of {', '.join(ROLES)} (with optional +mcp)")
    routes = tuple(parts[1:])
    if any(not ROUTE.fullmatch(item) for item in routes):
        raise ValueError(f"profiles.{name} routes must be agent/model/effort values")
    if len(set(routes)) != len(routes):
        raise ValueError(f"profiles.{name} lists the same route more than once")
    return Profile(name=name, role=role, mcp=mcp, routes=routes)


def format_profile(profile: Profile) -> str:
    head = f"{profile.role}+mcp" if profile.mcp else profile.role
    return " ".join((head, *profile.routes))


def review_tag(
    reviewer: str,
    author_families: set[str] | frozenset[str],
    primary_agent: str | None,
    *,
    fallback: bool = False,
) -> str:
    """Compute an honest factual proof tag for a review by comparing model families (providers)."""
    reviewer_agent = _route_parts(reviewer)[0]
    reviewer_family = route_family(reviewer)
    normalized_authors = {route_family(item) for item in author_families if item}
    on_primary = not fallback and (primary_agent is None or reviewer_agent == primary_agent)
    if not normalized_authors:
        return "unknown_author" if on_primary else "fallback_unknown_author"
    if reviewer_family not in normalized_authors:
        return "cross_provider" if on_primary else "cross_provider_fallback"
    return "same_provider" if on_primary else "same_provider_fallback"


def _agent_list(block: yamlsubset.Block, raw_line: str, key: str) -> list[str]:
    # The YAML reader trims whitespace. Check the original syntax before accepting its value.
    syntax = raw_line.partition("#")[0].removesuffix("\r")
    value = block.value
    if value and not block.lines and syntax.isascii() and syntax.isprintable() and AGENT_LIST.fullmatch(value):
        agents = AGENT.findall(value)
        if all(isinstance(yamlsubset.scalar(agent), str) for agent in agents):
            return agents
    raise ValueError(f"{key} must be an inline list of agent names such as [codex]")


def _agent_holds(block: yamlsubset.Block, raw_line: str, key: str) -> tuple[list[str], dict[str, AgentHold]]:
    """Read `paused_agents` or `soft_drained_agents` as either an inline list or a 1-level metadata map."""
    if block.value is not None and not block.lines:
        stripped = block.value.strip()
        if stripped in ("{}", "null", "~"):
            return [], {}
        if stripped.startswith("["):
            agents = _agent_list(block, raw_line, key)
            deduped: list[str] = []
            meta: dict[str, AgentHold] = {}
            for agent in agents:
                if agent not in meta:
                    deduped.append(agent)
                    meta[agent] = AgentHold(agent=agent)
            return deduped, meta
        raise ValueError(f"{key} must be an inline list such as [antigravity] or an indented agent map")
    raw_map = yamlsubset.mapping(block)
    agents_out: list[str] = []
    meta_out: dict[str, AgentHold] = {}
    for name, spec in raw_map.items():
        hold = parse_agent_hold(name, spec)
        agents_out.append(hold.agent)
        meta_out[hold.agent] = hold
    return agents_out, meta_out


def _route_hint(preferences: Preferences, route: str) -> str:
    profile_routes = {item for prof in preferences.profiles.values() for item in prof.routes}
    if route in profile_routes:
        return f"--route {route}"
    return f"--route {route} --reason 'why this route is needed'"


def _review_disallowed_fix(preferences: Preferences) -> str:
    agents = preferences.review_agents
    allowed = next((key for key in preferences.enabled() if key.partition("/")[0] in agents), None)
    fix = f"pass {_route_hint(preferences, allowed)}" if allowed else "enable a route on an allowed agent in `pod models`"
    if preferences.pinned:
        fix = (f"clear or change the Pin in `pod models`, then {fix}" if allowed
               else "enable a route on an allowed agent and clear or change the Pin in `pod models`")
    return fix


def _hold_clause(hold: AgentHold) -> str:
    parts = []
    if hold.reason:
        parts.append(hold.reason)
    if hold.until:
        parts.append(f"until {hold.until}")
    elif hold.ttl is not None:
        parts.append(f"ttl {hold.ttl}s")
    return f" ({', '.join(parts)})" if parts else ""


def _is_claude_route(route: str) -> bool:
    agent, model = _route_parts(route)
    return agent == "claude" or route_family(route) == "Anthropic" or (model is not None and model.startswith("claude-"))


def _is_quota_hold(hold: AgentHold) -> bool:
    """True for unreasoned holds or holds whose reason indicates quota/rate-limit exhaustion."""
    if not hold.reason or not hold.reason.strip():
        return True
    return bool(_QUOTA_HOLD_TERMS.search(hold.reason))


def _matched_hold(route: str, holds: dict[str, AgentHold]) -> tuple[str, str, AgentHold] | None:
    """Match a route against holds keyed by agent name or quota pool id."""
    agent = split(route)[0]
    if agent in holds:
        return "agent", agent, holds[agent]
    pool = route_pool(route)
    if pool in holds:
        return "pool", pool, holds[pool]
    return None


def _matched_exclude(route: str, excluded: set[str]) -> tuple[str, str] | None:
    agent = split(route)[0]
    if agent in excluded:
        return "agent", agent
    pool = route_pool(route)
    if pool in excluded:
        return "pool", pool
    return None


def effective_default_profiles(preferences: Preferences) -> dict[str, str]:
    """Resolved default profile per role: explicit default_profiles, else shipped registry defaults when present."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for role in ROLES:
        pname = preferences.default_profiles.get(role)
        if pname and pname in preferences.profiles and preferences.profiles[pname].role == role:
            resolved[role] = pname
        else:
            missing.append(role)
    if missing:
        shipped = _shipped_default_profiles()
        for role in missing:
            builtin = shipped.get(role)
            if builtin and builtin in preferences.profiles and preferences.profiles[builtin].role == role:
                resolved[role] = builtin
    return resolved


def routing_policy(preferences: Preferences, *, now: datetime | None = None) -> dict:
    """Stable routing-authority block for `pod models --json` and downstream doc renderers."""
    active_paused = active_holds(preferences.paused_meta, agents=preferences.paused_agents, now=now)
    active_drained = active_holds(preferences.soft_drained_meta, agents=preferences.soft_drained_agents, now=now)

    def _hold_rows(agents: list[str], meta: dict[str, AgentHold], active: dict[str, AgentHold]) -> list[dict]:
        rows = []
        seen: set[str] = set()
        for agent in [*agents, *meta]:
            if agent in seen:
                continue
            seen.add(agent)
            hold = meta.get(agent) or AgentHold(agent=agent)
            rows.append({**hold_as_dict(hold), "active": agent in active})
        return rows

    return {
        "schema": "pod-routing/v1",
        "status": preferences.status,
        "preferred": preferences.preferred,
        "pinned": preferences.pinned,
        "default_profiles": effective_default_profiles(preferences),
        "review_agents": list(preferences.review_agents),
        "profiles": {
            name: {
                "role": prof.role,
                "mcp": prof.mcp,
                "routes": list(prof.routes),
                "spec": format_profile(prof),
            }
            for name, prof in preferences.profiles.items()
        },
        "pauses": {
            "paused_agents": _hold_rows(preferences.paused_agents, preferences.paused_meta, active_paused),
            "soft_drained_agents": _hold_rows(preferences.soft_drained_agents, preferences.soft_drained_meta, active_drained),
        },
    }


def resolve_dispatch(
    preferences: Preferences,
    *,
    role: str | None = None,
    profile: str | None = None,
    route: str | None = None,
    reason: str | None = None,
    exclude_agents: list[str] | tuple[str, ...] | set[str] = (),
    author_families: set[str] | frozenset[str] = frozenset(),
    reviewer_notes: list[str] | tuple[str, ...] = (),
    allow_review_exception: bool = False,
    now: datetime | None = None,
) -> Resolution:
    """Single pure dispatch resolver used by `pod dispatch`, `--dry-run`, and `pod models` What-If."""
    normalized_authors = frozenset(route_family(item) for item in author_families if item)
    sorted_authors = tuple(sorted(normalized_authors))
    kept_notes = tuple(str(note) for note in reviewer_notes if str(note).strip())
    cleaned_reason = " ".join(reason.split())[:500] if isinstance(reason, str) and reason.strip() else None
    if preferences.status != "ok":
        return Resolution(False, role, None, None, profile, False, None, [],
                          "routes_unavailable",
                          f"No route is eligible: {preferences.problem or 'no preference file'}.", EDIT,
                          author_families=sorted_authors, reviewer_notes=kept_notes)
    prof: Profile | None = None
    mcp = False
    if profile is not None:
        prof = preferences.profiles.get(profile)
        if prof is None:
            known = ", ".join(preferences.profiles) or "none configured; define profiles in `pod models`"
            return Resolution(False, role, None, None, profile, False, None, [],
                              "profile_unknown", f"There is no profile {profile!r}.",
                              f"choose one of: {known}",
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if role is not None and role != prof.role:
            return Resolution(False, role, None, None, profile, prof.mcp, None, [],
                              "profile_role_mismatch",
                              f"Profile {profile!r} has role {prof.role}, not {role}.",
                              f"omit --role or pass --role {prof.role}",
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        role = prof.role
        mcp = prof.mcp
    if role not in ROLES:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "dispatch_incomplete", "A dispatch needs a valid role.",
                          "pass --role implement|review|investigate or --profile NAME",
                          author_families=sorted_authors, reviewer_notes=kept_notes)
    if prof is None and route is None:
        default_name = effective_default_profiles(preferences).get(role)
        if default_name:
            prof = preferences.profiles[default_name]
            profile = prof.name
            mcp = prof.mcp
    excluded_list = [item.strip() for item in exclude_agents if item and item.strip()]
    for item in excluded_list:
        if not AGENT.fullmatch(item):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "agent_invalid", f"{item!r} is not a valid agent or pool name.",
                              "pass a plain agent or pool name such as codex, claude, antigravity, agy-gemini or agy-claude",
                              author_families=sorted_authors, reviewer_notes=kept_notes)
    paused = active_holds(preferences.paused_meta, agents=preferences.paused_agents, now=now)
    drained = active_holds(preferences.soft_drained_meta, agents=preferences.soft_drained_agents, now=now)
    excluded = set(excluded_list)
    profile_routes = {item for prof_item in preferences.profiles.values() for item in prof_item.routes}
    trace: list[TraceStep] = []

    def _refuse_outside_profiles(chosen_route: str) -> Resolution:
        trace.append(TraceStep(
            chosen_route, "blocked", "route_outside_profiles",
            f"Blocked {chosen_route}: route is outside every configured profile and no --reason was given."))
        return Resolution(
            False, role, None, None, profile, mcp, None, trace,
            "route_outside_profiles",
            f"{chosen_route} is outside every configured profile; pass --reason TEXT to use it.",
            "choose a --profile, pick a route listed in a profile, or pass --reason 'why this route is needed'",
            author_families=sorted_authors, reviewer_notes=kept_notes)

    # Manual Pin is fail-closed and never silently overridden.
    if preferences.pinned:
        if route is not None and route != preferences.pinned:
            trace.append(TraceStep(route, "blocked", "route_not_pinned",
                                   f"Blocked {route}: Pin is set to {preferences.pinned}."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "route_not_pinned",
                              f"The Pin is {preferences.pinned}; {route} cannot be used while it is set.",
                              f"omit --route, use {_route_hint(preferences, preferences.pinned)}, or clear the Pin in `pod models`",
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        pin_agent = split(preferences.pinned)[0]
        paused_hit = _matched_hold(preferences.pinned, paused)
        drained_hit = _matched_hold(preferences.pinned, drained)
        excluded_hit = _matched_exclude(preferences.pinned, excluded)
        if paused_hit or drained_hit or excluded_hit:
            if paused_hit:
                kind, key, hold = paused_hit
                why = f"{kind} {key} is paused in paused_agents{_hold_clause(hold)}"
                fix = f"unpause {key} with `pod models --unpause {key}` or clear the Pin in `pod models`"
            elif drained_hit:
                kind, key, hold = drained_hit
                why = f"{kind} {key} is soft-drained in soft_drained_agents{_hold_clause(hold)}"
                fix = f"unpause {key} with `pod models --unpause {key}` or clear the Pin in `pod models`"
            else:
                kind, key = excluded_hit
                why = f"{kind} {key} is excluded by --exclude-agent"
                fix = f"omit --exclude-agent {key} or clear the Pin in `pod models`"
            trace.append(TraceStep(preferences.pinned, "blocked", "pinned_agent_excluded",
                                   f"Blocked Pin {preferences.pinned}: {why}."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "pinned_agent_excluded",
                              f"The Pin {preferences.pinned} cannot run because {why}.", fix,
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        chosen = preferences.pinned
        try:
            wired = wire_route(chosen)
        except PodError as exc:
            trace.append(TraceStep(chosen, "blocked", exc.code, f"Blocked Pin {chosen}: {exc}"))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              exc.code, str(exc), exc.fix,
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if (role == "review" and preferences.review_agents and pin_agent not in preferences.review_agents
                and not allow_review_exception):
            trace.append(TraceStep(preferences.pinned, "blocked", "reviewer_agent_not_allowed",
                                   f"Blocked Pin {preferences.pinned}: agent {pin_agent} is outside review_agents."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {pin_agent} is outside review_agents.",
                              _review_disallowed_fix(preferences),
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if (role == "review" and route is None and normalized_authors
                and route_family(preferences.pinned) in normalized_authors and not allow_review_exception):
            family = route_family(preferences.pinned)
            trace.append(TraceStep(preferences.pinned, "blocked", "author_family",
                                   f"Blocked Pin {preferences.pinned}: model family {family} authored this diff."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_same_family",
                              f"The pinned reviewer {preferences.pinned} uses the author's model family ({family}).",
                              "clear the Pin in `pod models`, or pass a review exception (--words or --after/--why)",
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if route is not None and chosen not in profile_routes and not cleaned_reason:
            return _refuse_outside_profiles(chosen)
        primary_agent = preferences.review_agents[0] if preferences.review_agents else None
        tag = review_tag(chosen, normalized_authors, primary_agent) if role == "review" else None
        extra = ""
        if tag:
            extra = f" ({tag}" + (f"; authors: {', '.join(sorted_authors)}" if sorted_authors else "") + ")"
        trace.append(TraceStep(chosen, "selected", "selected", f"Selected Pin {chosen}{extra}."))
        return Resolution(True, role, chosen, wired, profile, mcp, tag, trace,
                          author_families=sorted_authors, reviewer_notes=kept_notes, reason=cleaned_reason)

    # Ordered candidate list: explicit --route, else --profile / default_profiles, else Preferred.
    if route is not None:
        if not ROUTE.fullmatch(route or ""):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "route_invalid", f"{route!r} is not an agent/model/effort route.",
                              "name one enabled route, for example " + (preferences.enabled() or ["agent/model/effort"])[0],
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        candidates = [route]
    elif prof is not None:
        candidates = list(prof.routes)
    elif preferences.preferred:
        candidates = [preferences.preferred]
    else:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "route_required", "No Pin, default profile or Preferred route is set.",
                          "pass --profile or --route with --reason (if outside profiles), enabled: "
                          + (", ".join(preferences.enabled()) or "none enabled; run `pod models`"),
                          author_families=sorted_authors, reviewer_notes=kept_notes)

    eligible: list[str] = []
    held_keys: set[str] = set()
    effort_errors: dict[str, PodError] = {}
    auto_review_family_check = (role == "review" and route is None and bool(normalized_authors)
                                and not allow_review_exception)
    same_family_eligible: list[str] = []
    quota_blocked_other: dict[tuple[str, str, str], str] = {}
    other_family_non_quota_blocked = False

    def _is_other_family_allowed_enabled(cand: str, agent: str) -> bool:
        if not auto_review_family_check or route_family(cand) in normalized_authors:
            return False
        if preferences.review_agents and agent not in preferences.review_agents:
            return False
        return preferences.routes.get(cand) == "enabled"

    def _wire_valid(cand: str) -> bool:
        try:
            wire_route(cand)
            return True
        except PodError:
            return False

    for cand in candidates:
        agent = split(cand)[0]
        paused_hit = _matched_hold(cand, paused)
        if paused_hit:
            kind, key, hold = paused_hit
            held_keys.add(key)
            if _is_other_family_allowed_enabled(cand, agent):
                if not _wire_valid(cand) or _matched_exclude(cand, excluded) or not _is_quota_hold(hold):
                    other_family_non_quota_blocked = True
                else:
                    quota_blocked_other.setdefault(
                        (kind, key, "paused"), f"{kind} {key} paused{_hold_clause(hold)}")
            trace.append(TraceStep(cand, "skipped", "paused_agent",
                                   f"Skipped {cand}: {kind} {key} is paused in paused_agents{_hold_clause(hold)}."))
            continue
        drained_hit = _matched_hold(cand, drained)
        if drained_hit:
            kind, key, hold = drained_hit
            held_keys.add(key)
            if _is_other_family_allowed_enabled(cand, agent):
                if not _wire_valid(cand) or _matched_exclude(cand, excluded) or not _is_quota_hold(hold):
                    other_family_non_quota_blocked = True
                else:
                    quota_blocked_other.setdefault(
                        (kind, key, "soft_drained"), f"{kind} {key} soft-drained{_hold_clause(hold)}")
            trace.append(TraceStep(cand, "skipped", "soft_drained_agent",
                                   f"Skipped {cand}: {kind} {key} is soft-drained in soft_drained_agents"
                                   f"{_hold_clause(hold)}."))
            continue
        excluded_hit = _matched_exclude(cand, excluded)
        if excluded_hit:
            kind, key = excluded_hit
            if _is_other_family_allowed_enabled(cand, agent):
                other_family_non_quota_blocked = True
            trace.append(TraceStep(cand, "skipped", "excluded_agent",
                                   f"Skipped {cand}: {kind} {key} is excluded by --exclude-agent."))
            continue
        if preferences.routes.get(cand) != "enabled":
            trace.append(TraceStep(cand, "skipped", "route_not_enabled",
                                   f"Skipped {cand}: route is not enabled."))
            continue
        try:
            wire_route(cand)
        except PodError as exc:
            effort_errors[cand] = exc
            if _is_other_family_allowed_enabled(cand, agent):
                other_family_non_quota_blocked = True
            trace.append(TraceStep(cand, "skipped", exc.code, f"Skipped {cand}: {exc}"))
            continue
        if (role == "review" and preferences.review_agents and agent not in preferences.review_agents
                and not allow_review_exception):
            trace.append(TraceStep(cand, "skipped", "reviewer_agent_not_allowed",
                                   f"Skipped {cand}: agent {agent} is outside review_agents "
                                   f"({', '.join(preferences.review_agents)})."))
            continue
        if auto_review_family_check and route_family(cand) in normalized_authors:
            if not _is_claude_route(cand):
                same_family_eligible.append(cand)
            trace.append(TraceStep(cand, "skipped", "author_family",
                                   f"Skipped {cand}: model family {route_family(cand)} authored this diff."))
            continue
        eligible.append(cand)

    review_fallback_reason: str | None = None
    if (not eligible and auto_review_family_check and same_family_eligible
            and quota_blocked_other and not other_family_non_quota_blocked):
        eligible = list(same_family_eligible)
        review_fallback_reason = (
            "other-family review routes unavailable: " + "; ".join(quota_blocked_other.values())
        )

    if role == "review" and len(eligible) > 1:
        order = {agent: index for index, agent in enumerate(preferences.review_agents)}
        fallback_rank = len(order)
        eligible = sorted(eligible, key=lambda item: (route_family(item) in normalized_authors,
                                                      order.get(split(item)[0], fallback_rank)))

    if eligible:
        chosen = eligible[0]
        if route is not None and chosen not in profile_routes and not cleaned_reason:
            return _refuse_outside_profiles(chosen)
        if review_fallback_reason:
            trace = [step for step in trace if not (step.route == chosen and step.code == "author_family")]
        primary_agent = preferences.review_agents[0] if preferences.review_agents else None
        tag = review_tag(chosen, normalized_authors, primary_agent,
                         fallback=bool(review_fallback_reason)) if role == "review" else None
        extra = ""
        if tag:
            details = [tag]
            if review_fallback_reason:
                details.append(review_fallback_reason)
            if sorted_authors:
                details.append(f"authors: {', '.join(sorted_authors)}")
            extra = f" ({'; '.join(details)})"
        trace.append(TraceStep(chosen, "selected", "selected", f"Selected {chosen}{extra}."))
        return Resolution(True, role, chosen, wire_route(chosen), profile, mcp, tag, trace,
                          author_families=sorted_authors, reviewer_notes=kept_notes, reason=cleaned_reason,
                          review_fallback_reason=review_fallback_reason)

    # Zero candidates remain: preserve exact refusal codes for single-route mode and review-family blocks.
    if prof is None and len(trace) == 1:
        only = trace[0]
        if only.code == "route_not_enabled":
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "route_not_enabled", f"{only.route} is not enabled.",
                              "use one of: " + (", ".join(preferences.enabled()) or "none enabled; " + EDIT),
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if only.code == "reviewer_agent_not_allowed":
            agent = split(only.route)[0]
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {agent} is outside review_agents.",
                              _review_disallowed_fix(preferences),
                              author_families=sorted_authors, reviewer_notes=kept_notes)
        if only.code == "effort_not_supported" and only.route in effort_errors:
            exc = effort_errors[only.route]
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              exc.code, str(exc), exc.fix,
                              author_families=sorted_authors, reviewer_notes=kept_notes)

    reasons = " ".join(step.reason for step in trace)
    if any(step.code == "author_family" for step in trace):
        subject = f"profile {profile}" if profile else f"route {candidates[0]}"
        return Resolution(False, role, None, None, profile, mcp, None, trace,
                          "reviewer_same_family",
                          f"No independent reviewer remains for {subject} outside author families "
                          f"({', '.join(sorted_authors)}). {reasons}",
                          "enable or unpause a reviewer from another model family, or pass a review exception "
                          "(--words or --after/--why)",
                          author_families=sorted_authors, reviewer_notes=kept_notes)
    hints = []
    held_names = sorted(held_keys)
    if held_names:
        hints.append("unpause " + ", ".join(held_names) + " with `pod models --unpause <pool|agent>`")
    if any(step.code == "excluded_agent" for step in trace):
        hints.append("omit --exclude-agent")
    if any(step.code == "route_not_enabled" for step in trace):
        hints.append("enable a candidate route in `pod models`")
    if any(step.code == "reviewer_agent_not_allowed" for step in trace):
        hints.append("allow one of the candidate agents in review_agents")
    if any(step.code == "effort_not_supported" for step in trace):
        hints.append("choose a supported effort for the model")
    subject = f"profile {profile}" if profile else f"route {candidates[0]}"
    return Resolution(False, role, None, None, profile, mcp, None, trace,
                      "no_eligible_route",
                      f"Paused: no eligible route remains for {subject}. {reasons}",
                      "; or ".join(hints) or EDIT,
                      author_families=sorted_authors, reviewer_notes=kept_notes)


def load(path: Path | None = None) -> Preferences:
    path = paths.route_file() if path is None else path
    result = Preferences(path)
    try:
        if path.is_symlink() or not path.is_file():
            if path.is_symlink() or path.exists():
                result.status, result.problem = "invalid", "the preference file is not a regular file"
            return result
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError("the file is larger than 256 KiB")
        text = raw.decode("utf-8")
        blocks = yamlsubset.parse(text)
        schema = yamlsubset.scalar(blocks["schema"].value) if "schema" in blocks else None
        if schema != "pod/v2":
            raise ValueError(f"schema is {schema!r}, expected pod/v2")
        routes = yamlsubset.mapping(blocks["routes"]) if "routes" in blocks else {}
        for key, state in routes.items():
            if not ROUTE.fullmatch(key) or state not in STATES:
                raise ValueError(f"routes.{key} must be an agent/model/effort key set to enabled or disabled")
        for name in ("preferred", "pinned"):
            value = yamlsubset.scalar(blocks[name].value) if name in blocks and not blocks[name].lines else None
            if name in blocks and blocks[name].lines:
                raise ValueError(f"{name} must be one route or null")
            if value is not None and routes.get(value) != "enabled":
                raise ValueError(f"{name} names {value!r}, which is not an enabled route")
            setattr(result, name, value)
        if "merge_authorization" in blocks:
            who = yamlsubset.scalar(blocks["merge_authorization"].value)
            if who not in AUTHORIZERS:
                raise ValueError("merge_authorization must be person or coordinator")
            result.merge_authorization = who
        if "authorization_verifier" in blocks:
            command = yamlsubset.sequence(blocks["authorization_verifier"])
            if command and (len(command) > 16 or any(not isinstance(part, str) or not part for part in command)
                            or not command[0].startswith("/")):
                raise ValueError("authorization_verifier must be an absolute command and up to 15 arguments")
            result.authorization_verifier = command or None
        raw_lines = text.split("\n")
        if "review_agents" in blocks:
            block = blocks["review_agents"]
            result.review_agents = _agent_list(block, raw_lines[block.start], "review_agents")
        if "paused_agents" in blocks:
            block = blocks["paused_agents"]
            result.paused_agents, result.paused_meta = _agent_holds(block, raw_lines[block.start], "paused_agents")
        if "soft_drained_agents" in blocks:
            block = blocks["soft_drained_agents"]
            result.soft_drained_agents, result.soft_drained_meta = _agent_holds(
                block, raw_lines[block.start], "soft_drained_agents")
        if "profiles" in blocks:
            raw_profiles = yamlsubset.mapping(blocks["profiles"])
            result.profiles = {name: parse_profile(name, spec) for name, spec in raw_profiles.items()}
        if "default_profiles" in blocks:
            raw_defaults = yamlsubset.mapping(blocks["default_profiles"])
            parsed_defaults: dict[str, str] = {}
            for role_key, pname in raw_defaults.items():
                if role_key not in ROLES:
                    raise ValueError(f"default_profiles.{role_key} must be one of {', '.join(ROLES)}")
                if not valid_profile_name(pname):
                    raise ValueError(f"default_profiles.{role_key} must name a valid profile")
                prof = result.profiles.get(pname)
                if prof is None:
                    raise ValueError(f"default_profiles.{role_key} names unknown profile {pname!r}")
                if prof.role != role_key:
                    raise ValueError(f"default_profiles.{role_key} names profile {pname!r} with role {prof.role}, not {role_key}")
                parsed_defaults[role_key] = pname
            result.default_profiles = parsed_defaults
        result.routes, result.status = routes, "ok"
    except (OSError, UnicodeError, ValueError) as exc:
        result.status, result.problem = "invalid", str(exc)
        result.routes, result.preferred, result.pinned = {}, None, None
        result.merge_authorization, result.authorization_verifier = "person", None
        result.review_agents, result.paused_agents, result.paused_meta = [], [], {}
        result.soft_drained_agents, result.soft_drained_meta, result.profiles = [], {}, {}
        result.default_profiles = {}
    return result

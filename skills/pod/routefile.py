"""Read the personal preference file and resolve dispatches. Coordination reads this and nothing else about models.

The file is the only authority for enabled routes, Preferred, Pin, profiles, permitted review
agents, paused or soft-drained agents and who may authorize a merge. Nothing here writes it.
A file that cannot be read safely yields no eligible route and says why; it is never repaired
or replaced.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re

from . import paths, yamlsubset
from .errors import PodError
from .state import ROLES

STATES = ("enabled", "disabled")
AUTHORIZERS = ("person", "coordinator")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_SUFFIX_AGENTS = frozenset({"antigravity"})
AGENT = re.compile(r"[a-z][a-z0-9_-]*")
PROFILE = re.compile(r"[a-z][a-z0-9_-]*")
ROUTE = re.compile(rf"{AGENT.pattern}/[A-Za-z0-9][A-Za-z0-9._-]*/[a-z][a-z0-9_-]*")
AGENT_LIST = re.compile(rf"\[ *(?:{AGENT.pattern}(?: *, *{AGENT.pattern})* *)?\]", re.ASCII)
_TTL = re.compile(r"^([1-9][0-9]*)([smhd]?)$")
_TTL_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
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
    author_agents: tuple[str, ...] = ()
    reviewer_notes: tuple[str, ...] = ()


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


def split(route: str) -> tuple[str, str, str]:
    agent, model, effort = route.split("/")
    return agent, model, effort


def wire_route(route: str) -> tuple[str, str, str | None]:
    """Translate a canonical agent/model/effort route to Orca CLI (agent, model, effort | None).

    Agents in MODEL_SUFFIX_AGENTS (antigravity) embed effort in the model id (`gemini-4-neon-high`)
    and omit the separate `--effort` flag.
    """
    agent, model, effort = split(route)
    if agent in MODEL_SUFFIX_AGENTS:
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


def parse_profile(name: str, spec: object) -> Profile:
    """Parse one flat profile line: `<role>[+mcp] <route1> [<route2> ...]`."""
    if not PROFILE.fullmatch(name or ""):
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


def review_tag(reviewer_agent: str, author_agents: set[str] | frozenset[str], primary_agent: str | None) -> str:
    """Compute an honest factual proof tag for a review from observed agents."""
    on_primary = primary_agent is None or reviewer_agent == primary_agent
    if not author_agents:
        return "unknown_author" if on_primary else "fallback_unknown_author"
    if reviewer_agent not in author_agents:
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


def _review_disallowed_fix(preferences: Preferences) -> str:
    agents = preferences.review_agents
    allowed = next((key for key in preferences.enabled() if key.partition("/")[0] in agents), None)
    fix = f"pass --route {allowed}" if allowed else "enable a route on an allowed agent in `pod models`"
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


def resolve_dispatch(
    preferences: Preferences,
    *,
    role: str | None = None,
    profile: str | None = None,
    route: str | None = None,
    exclude_agents: list[str] | tuple[str, ...] | set[str] = (),
    author_agents: set[str] | frozenset[str] = frozenset(),
    reviewer_notes: list[str] | tuple[str, ...] = (),
    allow_review_exception: bool = False,
    now: datetime | None = None,
) -> Resolution:
    """Single pure dispatch resolver used by `pod dispatch`, `--dry-run`, and `pod models` What-If."""
    sorted_authors = tuple(sorted(author_agents))
    kept_notes = tuple(str(note) for note in reviewer_notes if str(note).strip())
    if preferences.status != "ok":
        return Resolution(False, role, None, None, profile, False, None, [],
                          "routes_unavailable",
                          f"No route is eligible: {preferences.problem or 'no preference file'}.", EDIT,
                          author_agents=sorted_authors, reviewer_notes=kept_notes)
    prof: Profile | None = None
    mcp = False
    if profile is not None:
        prof = preferences.profiles.get(profile)
        if prof is None:
            known = ", ".join(preferences.profiles) or "none configured; define profiles in `pod models`"
            return Resolution(False, role, None, None, profile, False, None, [],
                              "profile_unknown", f"There is no profile {profile!r}.",
                              f"choose one of: {known}",
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        if role is not None and role != prof.role:
            return Resolution(False, role, None, None, profile, prof.mcp, None, [],
                              "profile_role_mismatch",
                              f"Profile {profile!r} has role {prof.role}, not {role}.",
                              f"omit --role or pass --role {prof.role}",
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        role = prof.role
        mcp = prof.mcp
    if role not in ROLES:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "dispatch_incomplete", "A dispatch needs a valid role.",
                          "pass --role implement|review|investigate or --profile NAME",
                          author_agents=sorted_authors, reviewer_notes=kept_notes)
    excluded_list = [item.strip() for item in exclude_agents if item and item.strip()]
    for item in excluded_list:
        if not AGENT.fullmatch(item):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "agent_invalid", f"{item!r} is not a valid agent name.",
                              "pass a plain agent name such as codex, claude or antigravity",
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
    paused = active_holds(preferences.paused_meta, agents=preferences.paused_agents, now=now)
    drained = active_holds(preferences.soft_drained_meta, agents=preferences.soft_drained_agents, now=now)
    excluded = set(excluded_list)
    trace: list[TraceStep] = []

    # Manual Pin is fail-closed and never silently overridden.
    if preferences.pinned:
        if route is not None and route != preferences.pinned:
            trace.append(TraceStep(route, "blocked", "route_not_pinned",
                                   f"Blocked {route}: Pin is set to {preferences.pinned}."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "route_not_pinned",
                              f"The Pin is {preferences.pinned}; {route} cannot be used while it is set.",
                              f"use --route {preferences.pinned}, or clear the Pin in `pod models`",
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        pin_agent = split(preferences.pinned)[0]
        if pin_agent in paused or pin_agent in drained or pin_agent in excluded:
            if pin_agent in paused:
                why = f"agent {pin_agent} is paused in paused_agents{_hold_clause(paused[pin_agent])}"
                fix = f"unpause {pin_agent} with `pod models --unpause {pin_agent}` or clear the Pin in `pod models`"
            elif pin_agent in drained:
                why = f"agent {pin_agent} is soft-drained in soft_drained_agents{_hold_clause(drained[pin_agent])}"
                fix = f"unpause {pin_agent} with `pod models --unpause {pin_agent}` or clear the Pin in `pod models`"
            else:
                why = f"agent {pin_agent} is excluded by --exclude-agent"
                fix = f"omit --exclude-agent {pin_agent} or clear the Pin in `pod models`"
            trace.append(TraceStep(preferences.pinned, "blocked", "pinned_agent_excluded",
                                   f"Blocked Pin {preferences.pinned}: {why}."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "pinned_agent_excluded",
                              f"The Pin {preferences.pinned} cannot run because {why}.", fix,
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        if (role == "review" and preferences.review_agents and pin_agent not in preferences.review_agents
                and not allow_review_exception):
            trace.append(TraceStep(preferences.pinned, "blocked", "reviewer_agent_not_allowed",
                                   f"Blocked Pin {preferences.pinned}: agent {pin_agent} is outside review_agents."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {pin_agent} is outside review_agents.",
                              _review_disallowed_fix(preferences),
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        chosen = preferences.pinned
        primary_agent = preferences.review_agents[0] if preferences.review_agents else None
        tag = review_tag(pin_agent, author_agents, primary_agent) if role == "review" else None
        extra = ""
        if tag:
            extra = f" ({tag}" + (f"; authors: {', '.join(sorted_authors)}" if sorted_authors else "") + ")"
        trace.append(TraceStep(chosen, "selected", "selected", f"Selected Pin {chosen}{extra}."))
        return Resolution(True, role, chosen, wire_route(chosen), profile, mcp, tag, trace,
                          author_agents=sorted_authors, reviewer_notes=kept_notes)

    # Ordered candidate list: explicit --route, else --profile routes, else Preferred.
    if route is not None:
        if not ROUTE.fullmatch(route or ""):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "route_invalid", f"{route!r} is not an agent/model/effort route.",
                              "name one enabled route, for example " + (preferences.enabled() or ["agent/model/effort"])[0],
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        candidates = [route]
    elif prof is not None:
        candidates = list(prof.routes)
    elif preferences.preferred:
        candidates = [preferences.preferred]
    else:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "route_required", "No Pin or Preferred route is set.",
                          "pass --route, one of: " + (", ".join(preferences.enabled()) or "none enabled; run `pod models`"),
                          author_agents=sorted_authors, reviewer_notes=kept_notes)

    eligible: list[str] = []
    for cand in candidates:
        agent = split(cand)[0]
        if agent in paused:
            trace.append(TraceStep(cand, "skipped", "paused_agent",
                                   f"Skipped {cand}: agent {agent} is paused in paused_agents{_hold_clause(paused[agent])}."))
            continue
        if agent in drained:
            trace.append(TraceStep(cand, "skipped", "soft_drained_agent",
                                   f"Skipped {cand}: agent {agent} is soft-drained in soft_drained_agents"
                                   f"{_hold_clause(drained[agent])}."))
            continue
        if agent in excluded:
            trace.append(TraceStep(cand, "skipped", "excluded_agent",
                                   f"Skipped {cand}: agent {agent} is excluded by --exclude-agent."))
            continue
        if preferences.routes.get(cand) != "enabled":
            trace.append(TraceStep(cand, "skipped", "route_not_enabled",
                                   f"Skipped {cand}: route is not enabled."))
            continue
        if (role == "review" and preferences.review_agents and agent not in preferences.review_agents
                and not allow_review_exception):
            trace.append(TraceStep(cand, "skipped", "reviewer_agent_not_allowed",
                                   f"Skipped {cand}: agent {agent} is outside review_agents "
                                   f"({', '.join(preferences.review_agents)})."))
            continue
        eligible.append(cand)

    if role == "review" and len(eligible) > 1:
        order = {agent: index for index, agent in enumerate(preferences.review_agents)}
        fallback_rank = len(order)
        eligible = sorted(eligible, key=lambda item: (split(item)[0] in author_agents,
                                                      order.get(split(item)[0], fallback_rank)))

    if eligible:
        chosen = eligible[0]
        chosen_agent = split(chosen)[0]
        primary_agent = preferences.review_agents[0] if preferences.review_agents else None
        tag = review_tag(chosen_agent, author_agents, primary_agent) if role == "review" else None
        extra = ""
        if tag:
            extra = f" ({tag}" + (f"; authors: {', '.join(sorted_authors)}" if sorted_authors else "") + ")"
        trace.append(TraceStep(chosen, "selected", "selected", f"Selected {chosen}{extra}."))
        return Resolution(True, role, chosen, wire_route(chosen), profile, mcp, tag, trace,
                          author_agents=sorted_authors, reviewer_notes=kept_notes)

    # Zero candidates remain: preserve exact refusal codes for single-route mode, clean pause for profiles.
    if prof is None and len(trace) == 1:
        only = trace[0]
        if only.code == "route_not_enabled":
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "route_not_enabled", f"{only.route} is not enabled.",
                              "use one of: " + (", ".join(preferences.enabled()) or "none enabled; " + EDIT),
                              author_agents=sorted_authors, reviewer_notes=kept_notes)
        if only.code == "reviewer_agent_not_allowed":
            agent = split(only.route)[0]
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {agent} is outside review_agents.",
                              _review_disallowed_fix(preferences),
                              author_agents=sorted_authors, reviewer_notes=kept_notes)

    reasons = " ".join(step.reason for step in trace)
    hints = []
    held_names = sorted({split(step.route)[0] for step in trace if step.code in ("paused_agent", "soft_drained_agent")})
    if held_names:
        hints.append("unpause " + ", ".join(held_names) + " with `pod models --unpause <agent>`")
    if any(step.code == "excluded_agent" for step in trace):
        hints.append("omit --exclude-agent")
    if any(step.code == "route_not_enabled" for step in trace):
        hints.append("enable a candidate route in `pod models`")
    if any(step.code == "reviewer_agent_not_allowed" for step in trace):
        hints.append("allow one of the candidate agents in review_agents")
    subject = f"profile {profile}" if profile else f"route {candidates[0]}"
    return Resolution(False, role, None, None, profile, mcp, None, trace,
                      "no_eligible_route",
                      f"Paused: no eligible route remains for {subject}. {reasons}",
                      "; or ".join(hints) or EDIT,
                      author_agents=sorted_authors, reviewer_notes=kept_notes)


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
        result.routes, result.status = routes, "ok"
    except (OSError, UnicodeError, ValueError) as exc:
        result.status, result.problem = "invalid", str(exc)
        result.routes, result.preferred, result.pinned = {}, None, None
        result.merge_authorization, result.authorization_verifier = "person", None
        result.review_agents, result.paused_agents, result.paused_meta = [], [], {}
        result.soft_drained_agents, result.soft_drained_meta, result.profiles = [], {}, {}
    return result

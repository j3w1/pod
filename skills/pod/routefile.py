"""Read the personal preference file and resolve dispatches. Coordination reads this and nothing else about models.

The file is the only authority for enabled routes, Preferred, Pin, profiles, permitted review
agents, paused agents and who may authorize a merge. Nothing here writes it. A file that cannot
be read safely yields no eligible route and says why; it is never repaired or replaced.
"""

from dataclasses import dataclass, field
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
MAX_BYTES = 256 * 1024
EDIT = "run `pod models` to choose routes, or `pod config edit` to correct the file"


@dataclass(frozen=True)
class Profile:
    name: str
    role: str
    mcp: bool
    routes: tuple[str, ...]


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


def _review_disallowed_fix(preferences: Preferences) -> str:
    agents = preferences.review_agents
    allowed = next((key for key in preferences.enabled() if key.partition("/")[0] in agents), None)
    fix = f"pass --route {allowed}" if allowed else "enable a route on an allowed agent in `pod models`"
    if preferences.pinned:
        fix = (f"clear or change the Pin in `pod models`, then {fix}" if allowed
               else "enable a route on an allowed agent and clear or change the Pin in `pod models`")
    return fix


def resolve_dispatch(
    preferences: Preferences,
    *,
    role: str | None = None,
    profile: str | None = None,
    route: str | None = None,
    exclude_agents: list[str] | tuple[str, ...] | set[str] = (),
    author_agents: set[str] | frozenset[str] = frozenset(),
    allow_review_exception: bool = False,
) -> Resolution:
    """Single pure dispatch resolver used by `pod dispatch`, `--dry-run`, and `pod models` What-If."""
    if preferences.status != "ok":
        return Resolution(False, role, None, None, profile, False, None, [],
                          "routes_unavailable",
                          f"No route is eligible: {preferences.problem or 'no preference file'}.", EDIT)
    prof: Profile | None = None
    mcp = False
    if profile is not None:
        prof = preferences.profiles.get(profile)
        if prof is None:
            known = ", ".join(preferences.profiles) or "none configured; define profiles in `pod models`"
            return Resolution(False, role, None, None, profile, False, None, [],
                              "profile_unknown", f"There is no profile {profile!r}.",
                              f"choose one of: {known}")
        if role is not None and role != prof.role:
            return Resolution(False, role, None, None, profile, prof.mcp, None, [],
                              "profile_role_mismatch",
                              f"Profile {profile!r} has role {prof.role}, not {role}.",
                              f"omit --role or pass --role {prof.role}")
        role = prof.role
        mcp = prof.mcp
    if role not in ROLES:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "dispatch_incomplete", "A dispatch needs a valid role.",
                          "pass --role implement|review|investigate or --profile NAME")
    excluded_list = [item.strip() for item in exclude_agents if item and item.strip()]
    for item in excluded_list:
        if not AGENT.fullmatch(item):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "agent_invalid", f"{item!r} is not a valid agent name.",
                              "pass a plain agent name such as codex, claude or antigravity")
    paused = set(preferences.paused_agents)
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
                              f"use --route {preferences.pinned}, or clear the Pin in `pod models`")
        pin_agent = split(preferences.pinned)[0]
        if pin_agent in paused or pin_agent in excluded:
            why = (f"agent {pin_agent} is paused in paused_agents" if pin_agent in paused
                   else f"agent {pin_agent} is excluded by --exclude-agent")
            trace.append(TraceStep(preferences.pinned, "blocked", "pinned_agent_excluded",
                                   f"Blocked Pin {preferences.pinned}: {why}."))
            fix = (f"unpause {pin_agent} with `pod models --unpause {pin_agent}` or clear the Pin in `pod models`"
                   if pin_agent in paused
                   else f"omit --exclude-agent {pin_agent} or clear the Pin in `pod models`")
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "pinned_agent_excluded",
                              f"The Pin {preferences.pinned} cannot run because {why}.", fix)
        if (role == "review" and preferences.review_agents and pin_agent not in preferences.review_agents
                and not allow_review_exception):
            trace.append(TraceStep(preferences.pinned, "blocked", "reviewer_agent_not_allowed",
                                   f"Blocked Pin {preferences.pinned}: agent {pin_agent} is outside review_agents."))
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {pin_agent} is outside review_agents.",
                              _review_disallowed_fix(preferences))
        chosen = preferences.pinned
        primary_agent = preferences.review_agents[0] if preferences.review_agents else None
        tag = review_tag(pin_agent, author_agents, primary_agent) if role == "review" else None
        trace.append(TraceStep(chosen, "selected", "selected",
                               f"Selected Pin {chosen}" + (f" ({tag})" if tag else "") + "."))
        return Resolution(True, role, chosen, wire_route(chosen), profile, mcp, tag, trace)

    # Ordered candidate list: explicit --route, else --profile routes, else Preferred.
    if route is not None:
        if not ROUTE.fullmatch(route or ""):
            return Resolution(False, role, None, None, profile, mcp, None, [],
                              "route_invalid", f"{route!r} is not an agent/model/effort route.",
                              "name one enabled route, for example " + (preferences.enabled() or ["agent/model/effort"])[0])
        candidates = [route]
    elif prof is not None:
        candidates = list(prof.routes)
    elif preferences.preferred:
        candidates = [preferences.preferred]
    else:
        return Resolution(False, role, None, None, profile, mcp, None, [],
                          "route_required", "No Pin or Preferred route is set.",
                          "pass --route, one of: " + (", ".join(preferences.enabled()) or "none enabled; run `pod models`"))

    eligible: list[str] = []
    for cand in candidates:
        agent = split(cand)[0]
        if agent in paused:
            trace.append(TraceStep(cand, "skipped", "paused_agent",
                                   f"Skipped {cand}: agent {agent} is paused in paused_agents."))
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
        trace.append(TraceStep(chosen, "selected", "selected",
                               f"Selected {chosen}" + (f" ({tag})" if tag else "") + "."))
        return Resolution(True, role, chosen, wire_route(chosen), profile, mcp, tag, trace)

    # Zero candidates remain: preserve exact refusal codes for single-route mode, clean pause for profiles.
    if prof is None and len(trace) == 1:
        only = trace[0]
        if only.code == "route_not_enabled":
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "route_not_enabled", f"{only.route} is not enabled.",
                              "use one of: " + (", ".join(preferences.enabled()) or "none enabled; " + EDIT))
        if only.code == "reviewer_agent_not_allowed":
            agent = split(only.route)[0]
            return Resolution(False, role, None, None, profile, mcp, None, trace,
                              "reviewer_agent_not_allowed",
                              f"The review agent {agent} is outside review_agents.",
                              _review_disallowed_fix(preferences))

    reasons = " ".join(step.reason for step in trace)
    hints = []
    if any(step.code == "paused_agent" for step in trace):
        paused_names = sorted({split(step.route)[0] for step in trace if step.code == "paused_agent"})
        hints.append("unpause " + ", ".join(paused_names) + " with `pod models --unpause <agent>`")
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
                      "; or ".join(hints) or EDIT)


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
            result.paused_agents = _agent_list(block, raw_lines[block.start], "paused_agents")
        if "profiles" in blocks:
            raw_profiles = yamlsubset.mapping(blocks["profiles"])
            result.profiles = {name: parse_profile(name, spec) for name, spec in raw_profiles.items()}
        result.routes, result.status = routes, "ok"
    except (OSError, UnicodeError, ValueError) as exc:
        result.status, result.problem = "invalid", str(exc)
        result.routes, result.preferred, result.pinned = {}, None, None
        result.merge_authorization, result.authorization_verifier = "person", None
        result.review_agents, result.paused_agents, result.profiles = [], [], {}
    return result

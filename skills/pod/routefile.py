"""Read the personal preference file. Coordination reads this and nothing else about models.

The file is the only authority for which routes are enabled, the Preferred route, the Pin and
who may authorize a merge. Nothing here writes it. A file that cannot be read safely yields no
eligible route and says why; it is never repaired or replaced.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re

from . import paths, yamlsubset
from .errors import PodError

STATES = ("enabled", "disabled")
AUTHORIZERS = ("person", "coordinator")
ROUTE = re.compile(r"[a-z][a-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*/[a-z][a-z0-9_-]*")
MAX_BYTES = 256 * 1024
EDIT = "run `pod models` to choose routes, or `pod config edit` to correct the file"


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
        blocks = yamlsubset.parse(raw.decode("utf-8"))
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
        result.routes, result.status = routes, "ok"
    except (OSError, UnicodeError, ValueError) as exc:
        result.status, result.problem = "invalid", str(exc)
        result.routes, result.preferred, result.pinned = {}, None, None
        result.merge_authorization, result.authorization_verifier = "person", None
    return result

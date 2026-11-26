"""Change the personal preference file: only on the person's explicit action, one line at a time.

An edit rewrites the affected lines and leaves every other byte as it was, including keys this
version does not use. The result is checked before it replaces the file, so an edit can never
leave a file Pod cannot read. Refresh, discovery and update never call this module.
"""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import tempfile

from .. import installer, paths, routefile, yamlsubset
from ..errors import PodError
from . import registry

KEEP = object()
_VALUE = re.compile(r"^(?P<head>\s*(?:\"[^\"]*\"|'[^']*'|[^:#]+?)\s*:)"
                    r"(?P<gap>\s*)(?P<value>[^#]*?)(?P<tail>\s*(?:#.*)?)$")


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.parent / ".config.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _refuse_invalid(preferences: routefile.Preferences) -> None:
    if preferences.status == "invalid":
        raise PodError("preferences_invalid", f"The preference file cannot be read: {preferences.problem}.",
                       fix="correct it with `pod config edit`; Pod never repairs or replaces it")


def _set_line(line: str, value: str) -> str:
    """Replace the value of one `key: value` line, keeping indentation, comment and line ending."""
    ending = "\r" if line.endswith("\r") else ""
    match = _VALUE.match(line[:len(line) - len(ending)])
    return f"{match['head']} {value}{match['tail']}{ending}"


def _rewrite_mapping(lines: list[str], key: str, updates: dict[str, str | None], ending: str) -> list[str]:
    if not updates:
        return lines
    blocks = yamlsubset.parse("\n".join(lines))
    block = blocks.get(key)
    if block is None:
        added = [f"  {name}: {value}{ending}" for name, value in updates.items() if value is not None]
        if not added:
            return lines
        if lines and lines[-1] == "":
            lines.pop()
        return lines + [f"{key}:{ending}", *added, ""]
    present = {}
    for index, content in block.lines:
        name = content.strip().split(":", 1)[0].strip().strip("'\"")
        present[name] = index
    indent = "  "
    if block.lines:
        first = lines[block.lines[0][0]]
        indent = first[:len(first) - len(first.lstrip())]
    removed_indices = set()
    added = []
    for name, value in updates.items():
        if value is None:
            if name in present:
                removed_indices.add(present[name])
        elif name in present:
            lines[present[name]] = _set_line(lines[present[name]], value)
        else:
            added.append(f"{indent}{name}: {value}{ending}")
    if block.value is not None:            # `<key>: {}` written inline
        if added:
            lines[block.start] = f"{key}:{ending}"
            lines[block.start + 1:block.start + 1] = added
        return lines
    if added:
        lines[block.end:block.end] = added
    if removed_indices:
        lines = [line for idx, line in enumerate(lines) if idx not in removed_indices]
        # If every child line of the mapping was removed, turn `<key>:` into `<key>: {}` so YAML stays valid.
        re_blocks = yamlsubset.parse("\n".join(lines))
        re_block = re_blocks.get(key)
        if re_block is not None and re_block.value is None and not re_block.lines:
            lines[re_block.start] = f"{key}: {{}}{ending}"
    return lines


def _rewrite(text: str, routes: dict, scalars: dict, profiles: dict | None = None) -> str:
    lines = text.split("\n")
    ending = "\r" if lines and lines[0].endswith("\r") else ""
    lines = _rewrite_mapping(lines, "routes", routes, ending)
    lines = _rewrite_mapping(lines, "profiles", profiles or {}, ending)
    if scalars:
        blocks = yamlsubset.parse("\n".join(lines))
        for name, value in scalars.items():
            written = "null" if value is None else value
            block = blocks.get(name)
            if block is None:
                if lines and lines[-1] == "":
                    lines.pop()
                lines += [f"{name}: {written}{ending}", ""]
            else:
                lines[block.start] = _set_line(lines[block.start], written)
    return "\n".join(lines)


def _validate_agent_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise PodError("agent_invalid", f"{field_name} must be a list of agent names.",
                       fix="pass plain agent names such as codex, claude or antigravity")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not routefile.AGENT.fullmatch(item):
            raise PodError("agent_invalid", f"{item!r} is not a valid agent name for {field_name}.",
                           fix="use plain lowercase agent names such as codex, claude or antigravity")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _initial_text() -> str:
    """Single shipped default source (`registry.json`) with empty `routes: {}` so only explicit edits enable routes."""
    return installer.default_preferences(registry.FILE.parent.parent, include_routes=False).decode("utf-8")


def apply(*, routes: dict | None = None, preferred=KEEP, pinned=KEEP,
          profiles: dict[str, str | None] | None = None, paused_agents=KEEP, review_agents=KEEP,
          expected: bytes | None = None, path: Path | None = None, models=None) -> routefile.Preferences:
    """Apply one explicit change. `expected` is the file content the person was looking at.

    Enabling needs a route in the shipped registry; a stored key the registry does not list can
    only be disabled. Preferred and Pin name an enabled route or None.
    """
    path = paths.route_file() if path is None else path
    routes = dict(routes or {})
    profile_updates = dict(profiles or {})
    known = registry.routes(registry.load() if models is None else models)
    with _locked(path):
        current = routefile.load(path)
        _refuse_invalid(current)
        raw = path.read_bytes() if current.status == "ok" else b""
        if expected is not None and raw != expected:
            raise PodError("preferences_changed", "The preference file changed since it was shown.",
                           fix="reload and repeat the change")
        for key, state in routes.items():
            if state not in routefile.STATES or not routefile.ROUTE.fullmatch(key):
                raise PodError("route_invalid", f"{key!r} cannot be set to {state!r}.",
                               fix="name an agent/model/effort route and enabled or disabled")
            if state == "enabled" and key not in known:
                raise PodError("route_not_routable", f"{key} is not in the shipped registry, so it cannot be enabled.",
                               fix="choose a route `pod models` shows as routable")
        parsed_profiles: dict[str, routefile.Profile | None] = {}
        for name, spec in profile_updates.items():
            if not routefile.PROFILE.fullmatch(name or ""):
                raise PodError("profile_invalid", f"{name!r} is not a valid profile name.",
                               fix="use a lowercase profile name such as scout-fast or build-deep")
            if spec is None:
                parsed_profiles[name] = None
            else:
                try:
                    parsed_profiles[name] = routefile.parse_profile(name, spec)
                except ValueError as exc:
                    raise PodError("profile_invalid", str(exc),
                                   fix="use '<role>[+mcp] <agent/model/effort> [...]'") from exc
        after = {**current.routes, **routes}
        scalars = {}
        for name, value in (("preferred", preferred), ("pinned", pinned)):
            held = getattr(current, name)
            if value is KEEP:
                if held is not None and after.get(held) != "enabled":
                    label = "Preferred route" if name == "preferred" else "Pin"
                    raise PodError("route_in_use", f"{held} is the {label}; it cannot be disabled while it is.",
                                   fix=f"clear the {label} first, then disable the route")
                continue
            if value is not None and after.get(value) != "enabled":
                raise PodError("route_not_enabled", f"{value} is not an enabled route.",
                               fix="enable the route first")
            if value != held:
                scalars[name] = value
        wanted_paused = current.paused_agents
        if paused_agents is not KEEP:
            wanted_paused = _validate_agent_list(paused_agents, "paused_agents")
            if wanted_paused != current.paused_agents:
                scalars["paused_agents"] = "[" + ", ".join(wanted_paused) + "]"
        wanted_review = current.review_agents
        if review_agents is not KEEP:
            wanted_review = _validate_agent_list(review_agents, "review_agents")
            if wanted_review != current.review_agents:
                scalars["review_agents"] = "[" + ", ".join(wanted_review) + "]"

        routes = {key: state for key, state in routes.items() if current.routes.get(key) != state}
        formatted_profiles: dict[str, str | None] = {}
        for name, parsed in parsed_profiles.items():
            if parsed is None:
                if name in current.profiles:
                    formatted_profiles[name] = None
            elif current.profiles.get(name) != parsed:
                formatted_profiles[name] = routefile.format_profile(parsed)

        if not routes and not scalars and not formatted_profiles:
            return current
        base_text = raw.decode("utf-8") if current.status == "ok" else _initial_text()
        base_prefs = current if current.status == "ok" else None
        text = _rewrite(base_text, routes, scalars, formatted_profiles)
        handle, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            result = routefile.load(Path(temporary))
            wanted_routes = {**current.routes, **routes}
            expected_profiles = dict(base_prefs.profiles) if base_prefs is not None else dict(result.profiles)
            for name, parsed in parsed_profiles.items():
                if parsed is None:
                    expected_profiles.pop(name, None)
                else:
                    expected_profiles[name] = parsed
            expected_review = wanted_review if (base_prefs is not None or review_agents is not KEEP) else result.review_agents
            if (result.status != "ok" or result.routes != wanted_routes
                    or result.preferred != scalars.get("preferred", current.preferred)
                    or result.pinned != scalars.get("pinned", current.pinned)
                    or result.paused_agents != wanted_paused
                    or result.review_agents != expected_review
                    or result.profiles != expected_profiles):
                raise PodError("preferences_edit_unsafe", "The change could not be written as a one-line edit.",
                               fix="make the change with `pod config edit`")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        result.path = path
        return result

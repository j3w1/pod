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
import stat
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


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _read_regular(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Bytes and (device, inode) of one regular file read through the same open handle; never follows a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(), (info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def edit_in_editor(path: Path, editor: list[str]) -> None:
    """Open the file in an editor on a copy, then save it under the config lock.

    The editor runs outside the lock so `pod models` is not held up. Saving takes the same lock
    as `apply` and first checks, in this order, that the live file is still the same regular file
    with the same bytes it was when the editor opened (else `preferences_changed`); only then are the
    edited bytes, read once from the regular copy, checked to be readable if the original was, and
    written to a fresh file that replaces the live one with the live file's mode. A failed or missing
    editor installs nothing. An edited copy that is not saved is kept and its path reported."""
    import subprocess
    try:
        original, identity = _read_regular(path)
    except OSError:
        raise PodError("preferences_invalid", f"{path} is not a regular preference file.",
                       fix="replace it with a regular file; Pod never follows a symlink here") from None
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-edit-", suffix=".yaml", delete=False) as handle:
        handle.write(original)
    copy = Path(handle.name)
    copy.chmod(0o600)

    def stage(data: bytes) -> Path:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-stage-", suffix=".yaml", delete=False) as out:
            out.write(data)
        return Path(out.name)

    snapshot = stage(original)
    try:
        original_ok = routefile.load(snapshot).status == "ok"
    finally:
        snapshot.unlink(missing_ok=True)

    def keep(code: str, message: str, fix: str) -> PodError:
        return PodError(code, f"{message} Your edit is kept at {copy}." if copy.exists() else message, fix=fix)

    def unusable_output() -> PodError:
        if copy.is_symlink():                      # our own staging name, never the person's file
            try:
                copy.unlink()
            except OSError:
                pass
        return PodError("editor_failed", "The editor left no readable regular file to save; nothing was saved.",
                        fix=f"run `pod config edit` again" + (f"; any edit is at {copy}" if copy.exists() else ""))

    try:
        done = subprocess.run([*editor, str(copy)], check=False)
    except OSError as exc:
        copy.unlink(missing_ok=True)
        raise PodError("editor_failed", f"The editor could not run: {exc.strerror or type(exc).__name__}.",
                       fix="set VISUAL or EDITOR to an editor that exists") from exc
    try:
        edited, _ = _read_regular(copy)            # the one snapshot every later decision uses
    except OSError:
        raise unusable_output() from None
    if done.returncode != 0:
        if edited == original:
            copy.unlink(missing_ok=True)
            raise PodError("editor_failed", f"The editor exited with status {done.returncode}; nothing was saved.",
                           fix="run `pod config edit` again")
        raise keep("editor_failed", f"The editor exited with status {done.returncode}; nothing was saved.",
                   f"check {copy}, then copy it over {path} if it is right")
    if edited == original:
        copy.unlink(missing_ok=True)
        return
    with _locked(path):
        try:
            live, live_identity = _read_regular(path)
        except OSError:
            live, live_identity = None, None
        if live is None or live_identity != identity or live != original:
            raise keep("preferences_changed", "The preference file changed, was replaced or removed while you edited.",
                       f"merge {copy} into {path} by hand, then delete the copy")
        try:
            again, _ = _read_regular(copy)
        except OSError:
            again = None
        if again != edited:                          # the editor saved again while Pod waited for the lock
            raise keep("preferences_changed", "The editor's output changed while it was being saved; nothing was saved.",
                       f"run `pod config edit` again or merge {copy} into {path} by hand")
        staged = stage(edited)
        try:
            if original_ok:
                checked = routefile.load(staged)
                if checked.status != "ok":
                    raise keep("preferences_invalid", f"The edited file cannot be read: {checked.problem}.",
                               f"correct {copy}, then copy it over {path}")
            staged.chmod(os.stat(path).st_mode & 0o7777)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
        try:
            if _read_regular(copy)[0] == edited:      # never delete a copy the editor saved again
                copy.unlink()
        except OSError:
            pass


def _refuse_invalid(preferences: routefile.Preferences) -> None:
    if preferences.status == "invalid":
        raise PodError("preferences_invalid", f"The preference file cannot be read: {preferences.problem}.",
                       fix="correct it with `pod config edit`; Pod never repairs or replaces it")


def _set_line(line: str, value: str) -> str:
    """Replace the value of one `key: value` line, keeping indentation, comment and line ending."""
    ending = "\r" if line.endswith("\r") else ""
    match = _VALUE.match(line[:len(line) - len(ending)])
    return f"{match['head']} {value}{match['tail']}{ending}"


def _header_line(line: str, value: str | None) -> str:
    """Rewrite a mapping header `key: [value]`, preserving indentation, any `# comment` and line ending."""
    ending = "\r" if line.endswith("\r") else ""
    raw = line[:len(line) - len(ending)]
    match = _VALUE.match(raw)
    tail = match["tail"].strip()
    comment = ""
    if tail:
        idx = raw.rfind(tail)
        before = raw[:idx]
        ws = before[len(before.rstrip(" \t")):] or " "
        comment = f"{ws}{tail}"
    if value is None:
        return f"{match['head']}{comment}{ending}"
    return f"{match['head']} {value}{comment}{ending}"


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
            lines[block.start] = _header_line(lines[block.start], None)
            lines[block.start + 1:block.start + 1] = added
        return lines
    if added:
        lines[block.end:block.end] = added
    if removed_indices:
        lines = [line for idx, line in enumerate(lines) if idx not in removed_indices]
        re_blocks = yamlsubset.parse("\n".join(lines))
        re_block = re_blocks.get(key)
        if re_block is not None and re_block.value is None and not re_block.lines:
            lines[re_block.start] = _header_line(lines[re_block.start], "{}")
    return lines


def _rewrite_holds(lines: list[str], key: str, holds: dict[str, routefile.AgentHold], ending: str) -> list[str]:
    blocks = yamlsubset.parse("\n".join(lines))
    block = blocks.get(key)
    use_map = any(hold.has_metadata for hold in holds.values())
    if not use_map:
        written = "[" + ", ".join(holds) + "]"
        if block is None:
            if lines and lines[-1] == "":
                lines.pop()
            return lines + [f"{key}: {written}{ending}", ""]
        if not block.lines:
            lines[block.start] = _set_line(lines[block.start], written)
            return lines
        lines[block.start:block.end] = [f"{key}: {written}{ending}"]
        return lines
    entries = [f"  {agent}: {routefile.format_agent_hold(hold)}{ending}" for agent, hold in holds.items()]
    if block is None:
        if lines and lines[-1] == "":
            lines.pop()
        return lines + [f"{key}:{ending}", *entries, ""]
    lines[block.start:block.end] = [f"{key}:{ending}", *entries]
    return lines


def _rewrite(text: str, routes: dict, scalars: dict, profiles: dict | None = None,
             holds: dict[str, dict[str, routefile.AgentHold]] | None = None,
             default_profiles: dict[str, str | None] | None = None) -> str:
    lines = text.split("\n")
    ending = "\r" if lines and lines[0].endswith("\r") else ""
    lines = _rewrite_mapping(lines, "routes", routes, ending)
    lines = _rewrite_mapping(lines, "default_profiles", default_profiles or {}, ending)
    lines = _rewrite_mapping(lines, "profiles", profiles or {}, ending)
    for key, mapping in (holds or {}).items():
        lines = _rewrite_holds(lines, key, mapping, ending)
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


def _validate_holds(value: object, field_name: str) -> dict[str, routefile.AgentHold]:
    if isinstance(value, (list, tuple)):
        return {agent: routefile.AgentHold(agent=agent) for agent in _validate_agent_list(value, field_name)}
    if isinstance(value, dict):
        result: dict[str, routefile.AgentHold] = {}
        for agent, spec in value.items():
            if not isinstance(agent, str) or not routefile.AGENT.fullmatch(agent):
                raise PodError("agent_invalid", f"{agent!r} is not a valid agent name for {field_name}.",
                               fix="use plain lowercase agent names such as codex, claude or antigravity")
            if isinstance(spec, routefile.AgentHold):
                try:
                    normalized = routefile.parse_agent_hold(agent, routefile.format_agent_hold(spec) if spec.has_metadata else None)
                except ValueError as exc:
                    raise PodError("agent_invalid", str(exc),
                                   fix="supply valid reason text, positive ttl and ISO-8601 UTC until timestamp") from exc
                result[agent] = normalized
            else:
                try:
                    result[agent] = routefile.parse_agent_hold(agent, spec)
                except ValueError as exc:
                    raise PodError("agent_invalid", str(exc),
                                   fix="supply valid reason text, positive ttl and ISO-8601 UTC until timestamp") from exc
        return result
    raise PodError("agent_invalid", f"{field_name} must be a list of agent names or a mapping of agent metadata.",
                   fix="pass plain agent names such as codex, claude or antigravity")


def _initial_text() -> str:
    """Single shipped default source (`registry.json`) with empty `routes: {}` so only explicit edits enable routes."""
    return installer.default_preferences(registry.FILE.parent.parent, include_routes=False).decode("utf-8")


def apply(*, routes: dict | None = None, preferred=KEEP, pinned=KEEP,
          profiles: dict[str, str | None] | None = None,
          default_profiles: dict[str, str | None] | None = None,
          paused_agents=KEEP, soft_drained_agents=KEEP, review_agents=KEEP,
          expected: bytes | None = None, path: Path | None = None, models=None) -> routefile.Preferences:
    """Apply one explicit change. `expected` is the file content the person was looking at.

    Enabling needs a route in the shipped registry; a stored key the registry does not list can
    only be disabled. Preferred and Pin name an enabled route or None.
    """
    path = paths.route_file() if path is None else path
    routes = dict(routes or {})
    profile_updates = dict(profiles or {})
    default_updates = dict(default_profiles or {})
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
            if not routefile.valid_profile_name(name):
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
        if current.status == "ok":
            base_profiles = dict(current.profiles)
            base_defaults = dict(current.default_profiles)
        else:
            shipped = registry.defaults()
            base_profiles = {name: routefile.parse_profile(name, spec)
                             for name, spec in (shipped.get("profiles") or {}).items()}
            base_defaults = dict(shipped.get("default_profiles") or {})
        after_profiles = dict(base_profiles)
        for name, parsed in parsed_profiles.items():
            if parsed is None:
                after_profiles.pop(name, None)
            else:
                after_profiles[name] = parsed
        after_defaults = dict(base_defaults)
        for role_key, pname in default_updates.items():
            if role_key not in routefile.ROLES:
                raise PodError("profile_invalid", f"{role_key!r} is not a valid role for default_profiles.",
                               fix="use one of: " + ", ".join(routefile.ROLES))
            if pname is None:
                after_defaults.pop(role_key, None)
            else:
                if not routefile.valid_profile_name(pname):
                    raise PodError("profile_invalid", f"{pname!r} is not a valid profile name.",
                                   fix="use a configured profile name")
                prof = after_profiles.get(pname)
                if prof is None:
                    raise PodError("profile_unknown", f"There is no profile {pname!r}.",
                                   fix="choose one of: " + (", ".join(after_profiles) or "define a profile first"))
                if prof.role != role_key:
                    raise PodError("profile_role_mismatch",
                                   f"Profile {pname!r} has role {prof.role}, not {role_key}.",
                                   fix=f"choose a profile with role {role_key}")
                after_defaults[role_key] = pname
        for role_key, pname in after_defaults.items():
            prof = after_profiles.get(pname)
            if prof is None or prof.role != role_key:
                raise PodError("profile_in_use",
                               f"Profile {pname!r} is the default profile for {role_key} and cannot be removed or changed to another role.",
                               fix=f"update or clear default_profiles.{role_key} first")
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

        hold_updates: dict[str, dict[str, routefile.AgentHold]] = {}
        wanted_paused_meta = dict(current.paused_meta)
        if paused_agents is not KEEP:
            wanted_paused_meta = _validate_holds(paused_agents, "paused_agents")
            if wanted_paused_meta != current.paused_meta or list(wanted_paused_meta) != current.paused_agents:
                hold_updates["paused_agents"] = wanted_paused_meta
        wanted_drained_meta = dict(current.soft_drained_meta)
        if soft_drained_agents is not KEEP:
            wanted_drained_meta = _validate_holds(soft_drained_agents, "soft_drained_agents")
            if wanted_drained_meta != current.soft_drained_meta or list(wanted_drained_meta) != current.soft_drained_agents:
                hold_updates["soft_drained_agents"] = wanted_drained_meta

        wanted_review = current.review_agents
        if review_agents is not KEEP:
            wanted_review = _validate_agent_list(review_agents, "review_agents")
            if wanted_review != current.review_agents:
                scalars["review_agents"] = "[" + ", ".join(wanted_review) + "]"

        routes = {key: state for key, state in routes.items() if current.routes.get(key) != state}
        formatted_profiles: dict[str, str | None] = {}
        for name, parsed in parsed_profiles.items():
            if parsed is None:
                if name in base_profiles:
                    formatted_profiles[name] = None
            elif base_profiles.get(name) != parsed:
                formatted_profiles[name] = routefile.format_profile(parsed)
        formatted_defaults: dict[str, str | None] = {}
        for role_key, pname in default_updates.items():
            if pname is None:
                if role_key in base_defaults:
                    formatted_defaults[role_key] = None
            elif base_defaults.get(role_key) != pname:
                formatted_defaults[role_key] = installer.yaml_string(pname)

        creating = current.status == "absent" and bool(
            routes or scalars or parsed_profiles or default_updates or hold_updates
        )
        if not creating and not routes and not scalars and not formatted_profiles and not formatted_defaults and not hold_updates:
            return current
        base_text = raw.decode("utf-8") if current.status == "ok" else _initial_text()
        base_prefs = current if current.status == "ok" else None
        text = _rewrite(base_text, routes, scalars, formatted_profiles, hold_updates, formatted_defaults)
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
            expected_defaults = dict(base_prefs.default_profiles) if base_prefs is not None else dict(result.default_profiles)
            for role_key, pname in default_updates.items():
                if pname is None:
                    expected_defaults.pop(role_key, None)
                else:
                    expected_defaults[role_key] = pname
            expected_review = wanted_review if (base_prefs is not None or review_agents is not KEEP) else result.review_agents
            if (result.status != "ok" or result.routes != wanted_routes
                    or result.preferred != scalars.get("preferred", current.preferred)
                    or result.pinned != scalars.get("pinned", current.pinned)
                    or result.paused_agents != list(wanted_paused_meta)
                    or result.paused_meta != wanted_paused_meta
                    or result.soft_drained_agents != list(wanted_drained_meta)
                    or result.soft_drained_meta != wanted_drained_meta
                    or result.review_agents != expected_review
                    or result.profiles != expected_profiles
                    or result.default_profiles != expected_defaults):
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

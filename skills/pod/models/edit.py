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

from .. import paths, routefile, yamlsubset
from ..errors import PodError
from . import registry

KEEP = object()
NEW_FILE = "schema: pod/v2\nroutes: {}\npreferred: null\npinned: null\n"
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


def _rewrite(text: str, routes: dict, scalars: dict) -> str:
    lines = text.split("\n")
    blocks = yamlsubset.parse(text)
    ending = "\r" if lines and lines[0].endswith("\r") else ""
    if routes:
        block = blocks.get("routes")
        if block is None:
            if lines and lines[-1] == "":
                lines.pop()
            lines += ["routes:" + ending, *[f"  {key}: {state}{ending}" for key, state in routes.items()], ""]
        else:
            present = {}
            for index, content in block.lines:
                name = content.strip().split(":", 1)[0].strip().strip("'\"")
                present[name] = index
            indent = "  "
            if block.lines:
                first = lines[block.lines[0][0]]
                indent = first[:len(first) - len(first.lstrip())]
            added = [f"{indent}{key}: {state}{ending}" for key, state in routes.items() if key not in present]
            for key, state in routes.items():
                if key in present:
                    lines[present[key]] = _set_line(lines[present[key]], state)
            if block.value is not None:            # `routes: {}` written inline
                lines[block.start] = "routes:" + ending
                lines[block.start + 1:block.start + 1] = added
            else:
                lines[block.end:block.end] = added
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


def apply(*, routes: dict | None = None, preferred=KEEP, pinned=KEEP, expected: bytes | None = None,
          path: Path | None = None, models=None) -> routefile.Preferences:
    """Apply one explicit change. `expected` is the file content the person was looking at.

    Enabling needs a route in the shipped registry; a stored key the registry does not list can
    only be disabled. Preferred and Pin name an enabled route or None.
    """
    path = paths.route_file() if path is None else path
    routes = dict(routes or {})
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
        routes = {key: state for key, state in routes.items() if current.routes.get(key) != state}
        if not routes and not scalars:
            return current
        text = _rewrite(raw.decode("utf-8") if current.status == "ok" else NEW_FILE, routes, scalars)
        handle, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            result = routefile.load(Path(temporary))
            wanted = {**current.routes, **routes}
            if (result.status != "ok" or result.routes != wanted
                    or result.preferred != scalars.get("preferred", current.preferred)
                    or result.pinned != scalars.get("pinned", current.pinned)):
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

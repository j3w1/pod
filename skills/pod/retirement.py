"""The owner's historical retirement of delivered objectives whose coordinator is lost (R112, R113).

Retirement releases named worktrees of delivered history and nothing else. The objective is not
closed, continued, QUALIFIED or successful: its Governor journal, admissions, obligations and
evidence stay exactly as recorded, and the record accepts no later write. No coordinator is
revived, and no successor, agent session or worker is created. CE stays the resource authority.

A released worktree is either an existing worktree of this repository or one proven absent: gone from
this host's filesystem, from the repository's Git worktree registry and from Orca's complete worktree
and terminal inventories (R113). A missing directory never proves that work stopped; the coordinator,
Run, Dispatch and start checks prove that.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat

from .errors import PodError
from .util import bounded_text, digest, exact

FIELD = "historical_retirement"
REQUEST = {"objective", "release"}
DECISION = {"provenance", "instruction", "repository", "reason", "objectives"}
DECIDED = {"objective", "owner", "runs", "release", "locations", "retained", "unrecorded_locations", "records",
           "delivery"}
FACTS = DECIDED - {"delivery"}
ENTRY = {"at", "decision", "instruction", "reason", "repository", "objectives", "owner", "runs",
         "released", "preserved", "unfinished", "delivery", "native"}
# Pod 0.11.0 recorded no release dispositions; an entry from R113 on records where each released path stood.
LOCATED_ENTRY = ENTRY | {"locations"}
# How a released path stood when the owner decided: a verified worktree of this repository, or proven gone.
RELEASABLE = ("existing", "absent")
# Cleanup's conservative hold on locations a record never listed: ended by retirement, or never held (R113).
UNRECORDED = ("hold_ends", "none")
GOVERNOR_ROW = {"kind", "id", "action", "candidate", "outcome"}
ADMISSION_ROW = {"kind", "id", "role", "state", "dispatch", "reported"}
NATIVE = {"runtime", "owner", "runs", "dispatches", "exited_terminals"}
PRESERVED_OUTCOMES = ("pending", "UNKNOWN", "FAILED")
MAX_OBJECTIVES = 16
MAX_PATHS = 8
MAX_RECORDS = 64
MAX_LISTED = 64
MAX_DELIVERY = 16
ACTION = ("Read it with pod status --objective {}; the owner's historical retirement released its worktrees: "
          "it is not completed, and its record accepts no writes.")


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _texts(value: object, bound: int, limit: int) -> bool:
    return isinstance(value, list) and len(value) <= bound and all(_text(item, limit) for item in value)


def _path_text(value: object) -> bool:
    return (_text(value, 4096) and value.startswith("/") and os.path.normpath(value) == value
            and ".." not in Path(value).parts)


def _record_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("kind") == "governor":
        return (set(row) == GOVERNOR_ROW and all(_text(row[key], 256) for key in ("id", "action", "candidate"))
                and row["outcome"] in PRESERVED_OUTCOMES)
    if row.get("kind") == "admission":
        return (set(row) == ADMISSION_ROW and all(_text(row[key], 256) for key in ("id", "role", "state"))
                and row["state"] in ("reserved", "unresolved", "bound")
                and (row["dispatch"] is None or _text(row["dispatch"], 256))
                and type(row["reported"]) is bool and (row["state"] == "bound" or not row["reported"]))
    return False


def _runs(value: object) -> bool:
    return (isinstance(value, list) and 0 < len(value) <= 32
            and all(isinstance(row, dict) and set(row) == {"run", "generation"} and _text(row["run"], 256)
                    and type(row["generation"]) is int and row["generation"] >= 0 for row in value))


def _location_fact(row: object, path: str) -> bool:
    """One released path and the disposition the owner decided on; an existing one names its Orca worktree."""
    if not isinstance(row, dict) or row.get("path") != path:
        return False
    if row.get("disposition") == "absent":
        return set(row) == {"path", "disposition"}
    return (row.get("disposition") == "existing" and set(row) == {"path", "disposition", "worktree"}
            and _text(row["worktree"], 256))


def _locations_valid(value: object, released: list) -> bool:
    return (isinstance(value, list) and len(value) == len(released)
            and all(_location_fact(row, path) for row, path in zip(value, released)))


def _facts_valid(value: dict) -> bool:
    return (_text(value["objective"], 256) and _text(value["owner"], 256) and _runs(value["runs"])
            and isinstance(value["release"], list) and 0 < len(value["release"]) <= MAX_PATHS
            and all(_path_text(path) for path in value["release"])
            and len(set(value["release"])) == len(value["release"])
            and _locations_valid(value["locations"], value["release"])
            and isinstance(value["retained"], list) and len(value["retained"]) <= MAX_LISTED
            and all(_path_text(path) for path in value["retained"])
            and value["retained"] == sorted(set(value["retained"]))
            and not set(value["retained"]) & set(value["release"])
            and value["unrecorded_locations"] in UNRECORDED
            and isinstance(value["records"], list) and len(value["records"]) <= MAX_RECORDS
            and all(_record_row(row) for row in value["records"]))


def validate_entry(value: object) -> None:
    """The stored shape of a retirement; anything else makes the record unsupported."""
    native = value.get("native") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) not in (ENTRY, LOCATED_ENTRY)
            or not all(_text(value[key], 1024) for key in ("at", "decision", "instruction", "reason", "repository"))
            or not _texts(value["objectives"], MAX_OBJECTIVES, 256) or not value["objectives"]
            or not _text(value["owner"], 256) or not _runs(value["runs"])
            or not isinstance(value["released"], list) or not 0 < len(value["released"]) <= MAX_PATHS
            or not all(_path_text(path) for path in value["released"])
            or not isinstance(value["preserved"], list) or len(value["preserved"]) > MAX_RECORDS
            or not all(_record_row(row) for row in value["preserved"])
            or not _texts(value["unfinished"], MAX_LISTED, 256)
            or not _texts(value["delivery"], MAX_DELIVERY, 512) or not value["delivery"]
            or not isinstance(native, dict) or set(native) != NATIVE
            or not all(_text(native[key], 256) for key in ("runtime", "owner"))
            or any(type(native[key]) is not int or native[key] < 0 for key in ("runs", "dispatches", "exited_terminals"))
            or "locations" in value and not _locations_valid(value["locations"], value["released"])):
        raise PodError("state_unsupported", "Objective historical retirement record is malformed")


def refuse_retired(state: dict | None) -> None:
    """The one refusal an objective under historical retirement gives every write, its coordinator's included."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if isinstance(entry, dict):
        name = ((state.get("checkpoint") or {}).get("objective")) or "this objective"
        raise PodError("objective_retired",
                       f"The owner's historical retirement of this objective at {entry['at']} released its worktrees; "
                       "it is not completed, and its record accepts no writes",
                       {"retired_at": entry["at"], "next_action": ACTION.format(name)})


def counts(rows: list[dict]) -> dict:
    """Preserved records by meaning; a bound start whose report Pod validated is settled, as in R110."""
    tally = {"pending": 0, "unknown": 0, "failed": 0, "unreported": 0}
    for row in rows:
        if row["kind"] == "governor":
            tally[{"pending": "pending", "UNKNOWN": "unknown", "FAILED": "failed"}[row["outcome"]]] += 1
        elif row["state"] != "bound":
            tally["unknown"] += 1
        elif not row["reported"]:
            tally["unreported"] += 1
    return tally


def _recorded(state: dict) -> tuple[set[str], bool]:
    """The objective's recorded worktrees, and whether its record lists every location its work used (R108)."""
    from .listing import _facts
    facts = _facts(state)
    return {os.path.normpath(path) for path in facts["worktrees"]}, facts["locations_complete"]


def _reservations(state: dict, release: list[str]) -> tuple[list[str], str]:
    """What retirement leaves held and ends: the recorded worktrees it does not release, and whether it ends
    cleanup's conservative hold on locations the record never listed. A record under retirement accepts no write, so
    these read the same afterwards as when the owner decided."""
    recorded, complete = _recorded(state)
    return sorted(recorded - set(release)), "none" if complete else "hold_ends"


def status_block(state: dict) -> dict | None:
    """Public meaning of a retirement for status and the objective list; never closure or success."""
    entry = state.get(FIELD) if isinstance(state, dict) else None
    if not isinstance(entry, dict):
        return None
    retained, unrecorded = _reservations(state, entry["released"])
    return {"retired_at": entry["at"], "completed": False, "released": list(entry["released"]),
            "locations": [dict(row) for row in entry["locations"]] if "locations" in entry else None,
            "retained": retained, "unrecorded_locations": unrecorded,
            "preserved": [dict(row) for row in entry["preserved"]], "counts": counts(entry["preserved"]),
            "unfinished": list(entry["unfinished"]), "delivery": list(entry["delivery"]),
            "reason": entry["reason"], "decided_with": list(entry["objectives"])}


def _governor_rows(project: Path, objective: str) -> list[dict]:
    from .governor import _read_journal, _record_path
    journal = _read_journal(_record_path(project, objective))
    return [{"kind": "governor", "id": row["record_id"], "action": row["action"]["kind"],
             "candidate": row["action"]["candidate"], "outcome": row["outcome"]}
            for row in journal["actions"]
            if row["decision"] == "ALLOW" and row["outcome"] in PRESERVED_OUTCOMES]


def _claimants(project: Path, objective: str) -> list[str] | None:
    """Objectives of this repository whose claim names this one as successor; None when a record that could
    hold such a claim is unreadable. Directories without a record carry no claim and are skipped."""
    from .github import repository_context
    from .ledger import CONTEXT_LIMIT, _record_exists, record_name, state_root
    from .listing import _name
    from .util import bounded_json
    context = repository_context(project)
    root = state_root(project)
    try:
        if root.is_symlink():
            return None
        with os.scandir(root) as frontier:
            entries = sorted(frontier, key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    found = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                return None
            if not entry.is_dir(follow_symlinks=False) or not _record_exists(Path(entry.path) / "context.json"):
                continue
            raw = bounded_json(Path(entry.path) / "context.json", limit=CONTEXT_LIMIT)
        except (OSError, PodError):
            return None
        if not isinstance(raw, dict):
            return None
        link = raw.get("continued_by")
        name = _name(raw)
        if (isinstance(link, dict) and link.get("objective") == objective and name is not None
                and record_name(project, context, name) == entry.name):
            found.append(name)
    return found


def _readable(place: object) -> bool:
    """Whether a location is an absolute path this host can resolve; any other makes its inventory unusable."""
    if not isinstance(place, str) or not place.startswith("/"):
        return False
    try:
        os.fsencode(place)
    except (UnicodeError, ValueError):
        return False
    return "\x00" not in place


def _within(candidate: object, path: str) -> bool:
    """Whether a location another inventory names is the path or lies beneath it, links resolved. Inventories admit
    only readable locations; should resolution still fail, the location could be anywhere and counts as a match."""
    if not isinstance(candidate, str) or not candidate.startswith("/"):
        return False
    try:
        resolved = os.path.realpath(candidate)
    except (OSError, ValueError):
        return True
    return resolved == path or resolved.startswith(path + "/")


def _filesystem(path: str) -> str:
    """How this host's filesystem shows the path, without following any link: `directory`, `absent`, or why its
    absence stays unproven (`linked` for a link or file where a directory belongs, `inaccessible`)."""
    parts = Path(path).parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "inaccessible"
        if not stat.S_ISDIR(mode):
            return "linked"
    return "directory"


def _complete(result: object, kind: str) -> list | None:
    """The rows of an Orca list it reports complete: untruncated, its total equal to its rows, covering the local
    execution host and omitting none. Anything else is None, never an empty list (R113)."""
    if not isinstance(result, dict):
        return None
    rows, scope = result.get(kind), result.get("hostScope")
    hosts = scope.get("hostIds") if isinstance(scope, dict) else None
    if (isinstance(rows, list) and result.get("truncated") is False and result.get("totalCount") == len(rows)
            and all(isinstance(row, dict) for row in rows) and isinstance(hosts, list) and "local" in hosts
            and scope.get("omittedHostIds") == []):
        return rows
    return None


def _places(row: dict, kind: str) -> list[str] | None:
    """Every location an inventory row names. A row that names none Pod can read is None, so it makes the
    inventory unusable rather than silently dropping out of a proof."""
    from .listing import _location as named
    if kind == "worktrees":
        found = [row.get("path")]
        if "git" in row:
            found.append(row["git"].get("path") if isinstance(row["git"], dict) else None)
    else:
        found = [] if row.get("worktreePath") is None else [row["worktreePath"]]
        if row.get("worktreeId") is not None:
            found.append(named(row["worktreeId"]))
    if not found or not all(_readable(place) for place in found):
        return None
    return found


class _Host:
    """The repository's Git worktree registry and Orca's complete worktree and terminal inventories, each read at
    most once for one objective. A failed, partial, host-incomplete or unlocatable read stays unavailable, never an
    empty list."""

    def __init__(self, project: Path):
        self.project, self._reads = project, {}

    def registry(self) -> list[str] | None:
        if "registry" not in self._reads:
            from .cleanup import _worktrees
            try:
                rows = _worktrees(self.project)
                self._reads["registry"] = [row["worktree"] for row in rows if isinstance(row.get("worktree"), str)]
                if len(self._reads["registry"]) != len(rows):
                    self._reads["registry"] = None
            except PodError:
                self._reads["registry"] = None
        return self._reads["registry"]

    def inventory(self, kind: str) -> dict | None:
        if kind not in self._reads:
            from .orca import read_command
            self._reads[kind] = None
            try:
                result = read_command([kind.removesuffix("s"), "list", "--json"])["result"]
            except (PodError, KeyError, TypeError):
                return None
            rows = _complete(result, kind)
            places = [_places(row, kind) for row in rows] if rows is not None else [None]
            if None not in places:
                self._reads[kind] = {"rows": rows, "places": places,
                                     "hosts": sorted(str(host) for host in result["hostScope"]["hostIds"])}
        return self._reads[kind]

    def beneath(self, kind: str, path: str) -> list[dict] | None:
        """The rows of an inventory naming the path or anything beneath it; None while the inventory is unusable."""
        found = self.inventory(kind)
        if found is None:
            return None
        return [row for row, places in zip(found["rows"], found["places"])
                if any(_within(place, path) for place in places)]


def _exited(row: dict) -> bool:
    from .orca import EXIT_KINDS
    cause = row.get("exitCause")
    return (row.get("connected") is False and row.get("writable") is False and isinstance(cause, dict)
            and cause.get("kind") in EXIT_KINDS)


def _location(path: str, disposition: str, condition: str | None, reason: str | None, **evidence) -> dict:
    return {"path": path, "disposition": disposition, "condition": condition, "reason": reason, "evidence": evidence}


def _recorded_entries(state: dict) -> dict[str, set[str]]:
    """The Orca repository entries the record's native bindings name for each worktree path (`repo::path`)."""
    from .listing import _location as named
    entries: dict[str, set[str]] = {}
    for row in state.get("admissions", {}).values():
        binding = row.get("native_binding") if isinstance(row, dict) else None
        value = binding.get("worktreeId") if isinstance(binding, dict) else None
        place = named(value)
        if place is not None and isinstance(value, str) and "::" in value:
            entries.setdefault(os.path.normpath(place), set()).add(value.removeprefix("id:").split("::", 1)[0])
    return entries


def _identity(path: str) -> str | None:
    """The worktree identity Orca's exact show reports for an existing path, or None when it reports none."""
    from .orca import read_command
    try:
        worktree = read_command(["worktree", "show", "--worktree", "path:" + path, "--json"])["result"]["worktree"]
        identity = worktree.get("identity") if isinstance(worktree, dict) and worktree.get("path") == path else None
        key = identity.get("key") if isinstance(identity, dict) else None
    except (PodError, KeyError, TypeError):
        return None
    return key if _text(key, 256) else None


def _existing(path: str, repository: str, entries: set[str], host: _Host) -> dict:
    """A released path that exists: a worktree root of this repository that Orca resolves exactly, under the
    repository entry the record names, with every terminal Orca lists in it or beneath it proven exited (R112), and
    its Orca worktree identity for the decision (R113)."""
    from .gitio import read as git_read
    from .orca import read_command
    found = git_read(Path(path), ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"])
    if found is None:
        return _location(path, "unknown", "path_unverified", "Git could not read this path.", filesystem="directory")
    lines = found.stdout.splitlines() if found.returncode == 0 else []
    if (len(lines) != 2 or os.path.realpath(lines[0]) != path
            or hashlib.sha256(str(Path(lines[1]).resolve()).encode()).hexdigest() != repository):
        return _location(path, "foreign", "path_foreign", "The path exists but is not a worktree of this repository: "
                         "another repository, a plain directory or a reused path keeps its own protection.",
                         filesystem="directory")
    try:
        shown = read_command(["worktree", "show", "--worktree", "path:" + path, "--json"])
        listed = read_command(["terminal", "list", "--worktree", "path:" + path, "--json"])
        worktree, rows = shown["result"]["worktree"], _complete(listed["result"], "terminals")
        identity = worktree.get("identity") if isinstance(worktree, dict) else None
        key = identity.get("key") if isinstance(identity, dict) else None
        if (not isinstance(worktree, dict) or worktree.get("path") != path or rows is None
                or any(row.get("worktreePath") != path for row in rows)):
            raise PodError("orca_contract", "terminal list is partial or names another worktree")
        if not _text(key, 256):
            raise PodError("orca_contract", "Orca reports no identity for the worktree")
        entry = worktree["id"].split("::", 1)[0] if entries and isinstance(worktree.get("id"), str) else None
    except (PodError, KeyError, TypeError) as exc:
        return _location(path, "unknown", "path_unverified", "Orca could not list the terminals of this path ("
                         + (exc.code if isinstance(exc, PodError) else "orca_contract") + ").",
                         filesystem="directory", git="this_repository")
    if entries and entries != {entry}:
        return _location(path, "foreign", "path_foreign", "Orca now registers this path under another repository "
                         "entry than the one the record names: the path was reused.",
                         filesystem="directory", git="this_repository", orca="reused")
    beneath = host.beneath("terminals", path)
    if beneath is None:
        return _location(path, "unknown", "path_unverified", "Orca's terminal inventory is unreadable, partial, "
                         "misses an execution host or has a row without a readable location, so terminals beneath "
                         "this path stay unverified.", filesystem="directory", git="this_repository", orca="resolved")
    # Any row, from either list, that does not prove its terminal exited holds the path; the lists overlap, so
    # exited terminals are counted once by handle where they have one.
    listed = [*rows, *beneath]
    live = [row for row in listed if not _exited(row)]
    exited = {next((("key", row[key]) for key in ("handle", "ptyId", "incarnationId") if isinstance(row.get(key), str)),
                   ("row", index)) for index, row in enumerate(listed) if _exited(row)}
    row = _location(path, "existing", "path_in_use" if live else None,
                    "An Orca terminal that is not proven exited is in or beneath this path." if live else None,
                    filesystem="directory", git="this_repository", orca="resolved", exited_terminals=len(exited))
    row["worktree"] = key
    return row


def _absent(path: str, host: _Host) -> dict:
    """A released path proven gone on this host: absent from its filesystem with no link in its ancestry, from the
    repository's Git worktree registry, stale registrations included, and from Orca's complete worktree inventory;
    Orca's complete terminal inventory may name it only for terminals proven exited. The filesystem is read again
    after the other reads, so a path that appears meanwhile is never proven absent (R113)."""
    registry = host.registry()
    if registry is None:
        return _location(path, "unknown", "path_unverified", "Git's worktree registry of this repository could not "
                         "be read completely.", filesystem="absent")
    if any(_within(item, path) for item in registry):
        return _location(path, "unknown", "path_unverified", "Git still registers a worktree at or beneath this "
                         "path, though its directory is gone; resolve that stale registration through Git first.",
                         filesystem="absent", git="registered")
    worktrees, terminals = host.beneath("worktrees", path), host.beneath("terminals", path)
    if worktrees is None or terminals is None:
        return _location(path, "unknown", "path_unverified", "Orca's worktree or terminal inventory is unreadable, "
                         "partial, misses an execution host or has a row without a readable location.",
                         filesystem="absent", git="unregistered")
    if worktrees:
        return _location(path, "unknown", "path_unverified", "Orca still lists a worktree at or beneath this path.",
                         filesystem="absent", git="unregistered", orca="listed")
    if _filesystem(path) != "absent":
        return _location(path, "unknown", "path_unverified", "The path appeared, or its ancestry changed, while Pod "
                         "read Git and Orca; detect again.", filesystem="changed", git="unregistered")
    live = [row for row in terminals if not _exited(row)]
    return _location(path, "absent", "path_in_use" if live else None,
                     "An Orca terminal that is not proven exited names this path or a location beneath it." if live
                     else None, filesystem="absent", git="unregistered", orca="unlisted",
                     hosts=host.inventory("worktrees")["hosts"], exited_terminals=len(terminals) - len(live))


def _moved(located: list[dict]) -> bool:
    """Whether any released path no longer stands as detection found it, read again right before the write."""
    for row in located:
        shown = _filesystem(row["path"])
        if row["disposition"] == "absent" and shown != "absent":
            return True
        if row["disposition"] == "existing" and (shown != "directory" or _identity(row["path"]) != row["worktree"]):
            return True
    return False



_UNPROVEN = {"linked": "A link or file stands in this path or its ancestry, so where it leads stays unproven.",
             "inaccessible": "This host cannot read the path or one of its parent directories."}


def _locate(path: str, repository: str, host: _Host, entries: set[str]) -> dict:
    """One released path: existing, proven absent, foreign, or unknown with the reason absence stays unproven."""
    shown = _filesystem(path)
    if shown == "directory":
        return _existing(path, repository, entries, host)
    if shown == "absent":
        return _absent(path, host)
    return _location(path, "unknown", "path_unverified", _UNPROVEN[shown], filesystem=shown)


def _detect(project: Path, objective: str, release: list[str], state: dict | None, *, port,
            repository: str, host: _Host) -> dict:
    """Fresh facts for one objective, read-only; an unproven fact refuses it alone."""
    from .handoff import unfinished
    from .ledger import _native_assignment_settled, _run_references, map_of
    from .obligations import TERMINAL
    from .orca import run_show, terminal_identity, worker_rows
    verdict = {"objective": objective, "status": "refused", "condition": None,
               "facts": {"objective": objective, "owner": None, "runs": [], "release": list(release),
                         "locations": [], "retained": [], "unrecorded_locations": None, "records": []}}

    def fail(condition: str, message: str, action: str | None = None) -> dict:
        verdict.update(condition=condition, message=message,
                       next_action=action or "Resolve the named condition; nothing was written.")
        return verdict

    if state is None or not isinstance(state.get("checkpoint"), dict):
        return fail("objective_unknown", "No readable objective of that name is recorded in this repository.")
    if isinstance(state.get("continued_by"), dict):
        return fail("objective_continued", "A successor continues this objective; historical retirement does not apply to it separately.",
                    "Read the successor with pod status --objective " + state["continued_by"]["objective"] + ".")
    if isinstance(state.get("continues"), dict):
        # Retiring a successor would leave its predecessor's chain neither open nor closed (R110).
        return fail("objective_continues", "This objective continues objective " + state["continues"]["objective"]
                    + "; historical retirement never ends a successor that holds a claim.")
    if isinstance(state.get(FIELD), dict):
        entry = state[FIELD]
        retained, unrecorded = _reservations(state, entry["released"])
        verdict.update(status="retired", condition="objective_retired", facts={
            "objective": objective, "owner": entry["owner"], "runs": list(entry["runs"]),
            "release": list(entry["released"]),
            "locations": [dict(row) for row in entry["locations"]] if "locations" in entry else None,
            "retained": retained, "unrecorded_locations": unrecorded, "records": list(entry["preserved"])},
            message="The owner's historical retirement already covers this objective.", next_action=ACTION.format(objective))
        return verdict
    map_state = map_of(state)
    if map_state is not None and map_state.get("closure"):
        return fail("objective_closed", "The objective is closed; a closed objective needs no retirement.")
    claimants = _claimants(project, objective)
    if claimants is None:
        return fail("scope_unreadable", "A Pod record that could name this objective as a claimed successor is "
                    "unreadable.")
    if claimants:
        # An interrupted claim recorded the predecessor's link before this objective's own back-link.
        return fail("objective_continues", f"Objective {claimants[0]} records this objective as its successor; "
                    "historical retirement never ends a successor that holds a claim.")
    owner = state.get("owner")
    if not owner:
        return fail("owner_unrecorded", "The objective records no coordinator.")
    verdict["facts"]["owner"] = owner
    try:
        if unfinished(state) is not None:
            return fail("handoff_pending", "The objective has an unfinished owner handoff.")
        refs = sorted(_run_references(state))
        recorded, _ = _recorded(state)
        records = _governor_rows(project, objective)
    except PodError as exc:
        return fail("scope_unreadable", f"The objective's record or Governor journal is unreadable ({exc.code}).")
    for path in release:
        if path not in recorded:
            return fail("path_not_recorded", "A path to release is not one of the objective's recorded worktrees.",
                        "Name only worktrees pod status --list records for this objective.")
    retained, unrecorded = _reservations(state, release)
    if len(retained) > MAX_LISTED:
        return fail("scope_unreadable", f"More than {MAX_LISTED} recorded worktrees left held cannot be bound to "
                    "one decision.")
    verdict["facts"].update(retained=retained, unrecorded_locations=unrecorded)
    if not refs:
        return fail("scope_unreadable", "The objective records no Run whose work could be proven settled.")
    for key in sorted(state["admissions"]):
        row = state["admissions"][key]
        if row["state"] in ("closed", "deferred"):
            continue
        records.append({"kind": "admission", "id": key, "role": row["role"], "state": row["state"],
                        "dispatch": (row.get("native_binding") or {}).get("dispatchId"),
                        "reported": row["state"] == "bound"
                        and (row.get("report") or {}).get("status") == "validated_observation"})
    if len(records) > MAX_RECORDS:
        return fail("scope_unreadable", f"More than {MAX_RECORDS} preserved records cannot be bound to one decision.")
    verdict["facts"]["records"] = records
    previous = terminal_identity(owner)
    if previous["state"] == "live":
        return fail("owner_not_lost", "Orca reports the objective's coordinator live; retirement never takes "
                    "over live work.", "Continue in the coordinator's own terminal, or close it first.")
    if previous["state"] != "lost" and not previous.get("exited"):
        return fail("owner_not_lost", "Orca cannot prove the objective's coordinator gone ("
                    + (previous["code"] or previous["state"]) + ").")
    binding = ((state["checkpoint"].get("continuity") or {}).get("binding")) or {}
    runs, dispatches, runtime, settled = [], {}, None, 0
    for run in refs:
        try:
            shown = run_show(run)
            fleet = worker_rows(run)
        except PodError as exc:
            return fail("workers_unverified", f"A Run or its worker list could not be read ({exc.code}).")
        if shown["coordinator_handle"] not in (None, owner):
            return fail("generation_changed", "A recorded Run is now bound to another coordinator.")
        if binding.get("run") == run and binding.get("generation") != shown["consumer_generation"]:
            return fail("generation_changed", "A recorded Run's consumer generation changed.")
        if fleet.get("complete") is not True:
            return fail("workers_unverified", "A Run's worker list is incomplete.")
        runtime = runtime or fleet["runtime"]
        runs.append({"run": run, "generation": shown["consumer_generation"]})
        for row in fleet["workers"]:
            dispatch = row.get("dispatchId") if isinstance(row, dict) else None
            if not isinstance(dispatch, str) or not dispatch:
                return fail("workers_unverified", "A worker has no Dispatch identity.")
            dispatches[dispatch] = None
    verdict["facts"]["runs"] = runs
    # Every remaining fact is read before any refusal, so a refusal from here on keeps complete facts.
    activity = None
    from .operations import _receipt_from_request_show
    for key, row in state["admissions"].items():
        if row["state"] == "bound":
            dispatches[row["native_binding"]["dispatchId"]] = (key, row["native_binding"])
        elif activity is None and row["state"] in ("reserved", "unresolved") and isinstance(row.get("request_uuid"), str):
            # A request Orca still holds could yet start a worker; absent or completed ones are settled by
            # the complete worker lists above.
            try:
                found, _ = _receipt_from_request_show(port.request_show(row["request_uuid"]), row["request_uuid"])
            except PodError as exc:
                activity = ("workers_unverified", f"Admission {key[:12]}'s start request could not be read ({exc.code}).",
                            None)
                continue
            if found == "pending":
                activity = ("work_live", f"Admission {key[:12]}'s start request is still pending in Orca.",
                            "Let Orca settle the request, then detect again.")
    for dispatch in sorted(dispatches):
        if activity is not None:
            break
        try:
            shown = port.show_worker(dispatch)
        except PodError as exc:
            activity = ("workers_unverified", f"A recorded Dispatch could not be read ({exc.code}).", None)
            break
        result = shown.get("result") if isinstance(shown, dict) else None
        if dispatches[dispatch] is not None:
            key, bound = dispatches[dispatch]
            parts = [result.get(name) if isinstance(result, dict) else None for name in ("dispatch", "projection", "worker")]
            if not all(isinstance(part, dict) for part in parts) or (
                    parts[0].get("id"), parts[0].get("runId"), parts[0].get("taskId"), parts[1].get("id"),
                    parts[2].get("worktreeId")) != (bound["dispatchId"], bound["runId"], bound["taskId"],
                                                    bound["workerId"], bound["worktreeId"]):
                activity = ("workers_unverified", f"Admission {key[:12]} has a differing native identity.", None)
                break
        if not _native_assignment_settled(shown):
            activity = ("work_live", "A recorded Dispatch is live or unsettled; retirement waits for it.",
                        "Settle the work through Orca, then detect again.")
            break
        settled += 1
    entries = _recorded_entries(state)
    located = [_locate(path, repository, host, entries.get(path, set())) for path in release]
    verdict["locations"] = located
    verdict["facts"]["locations"] = [{"path": row["path"], "disposition": row["disposition"],
                                      **({"worktree": row["worktree"]} if "worktree" in row else {})}
                                     for row in located]
    if activity is not None:
        return fail(*activity)
    blocked = next((row for row in located if row["condition"] is not None), None)
    if blocked is not None:
        action = ("Close that terminal, or name only paths no terminal uses." if blocked["condition"] == "path_in_use"
                  else "Resolve what each location names, then detect again; nothing was written.")
        return fail(blocked["condition"], f"Path {blocked['path']}: {blocked['reason']}", action)
    obligations = (map_state or {}).get("obligations", [])
    verdict.update(status="eligible", condition="retirable",
                   unfinished=[ob["id"] for ob in obligations if ob["state"] not in TERMINAL][:MAX_LISTED],
                   native={"runtime": runtime or "unknown",
                           "owner": "exited" if previous.get("exited") else (previous["code"] or "lost"),
                           "runs": len(runs), "dispatches": settled,
                           "exited_terminals": sum(row["evidence"]["exited_terminals"] for row in located)},
                   message="The coordinator is gone, every recorded Dispatch is settled, every path to release is an "
                           "existing worktree of this repository or proven absent, and no live terminal uses them.")
    return verdict


def _requested(value: object) -> list[dict]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_OBJECTIVES:
        raise PodError("invalid_request", f"objectives is a list of 1 to {MAX_OBJECTIVES} entries")
    rows = []
    for item in value:
        row = exact(item, REQUEST, REQUEST, name="retirement_objective")
        bounded_text(row["objective"], name="objective", limit=256)
        if (not isinstance(row["release"], list) or not 0 < len(row["release"]) <= MAX_PATHS
                or not all(_path_text(path) for path in row["release"])
                or len(set(row["release"])) != len(row["release"])):
            raise PodError("invalid_request", f"release names 1 to {MAX_PATHS} distinct absolute normalized paths")
        rows.append({"objective": row["objective"], "release": list(row["release"])})
    if len({row["objective"] for row in rows}) != len(rows):
        raise PodError("invalid_request", "Each objective is named once")
    return rows


def _decision(value: object, requested: list[dict], repository: str) -> dict:
    decision = exact(value, DECISION, DECISION, name="retirement_decision")
    bounded_text(decision["instruction"], name="instruction", limit=1024)
    bounded_text(decision["reason"], name="reason", limit=1024)
    mismatch = PodError("retirement_decision_mismatch", "Only the owner's direct instruction for exactly the "
                        "requested objectives and paths permits retirement. Nothing was written.",
                        {"next_action": "detect again without a decision and confirm its facts with the owner"})
    if decision["provenance"] != "user_direct" or decision["repository"] != repository:
        raise mismatch
    blocks = decision["objectives"]
    if not isinstance(blocks, list) or len(blocks) != len(requested) or any(
            not isinstance(block, dict) or block.get("objective") != row["objective"]
            or block.get("release") != row["release"] for block, row in zip(blocks, requested)):
        raise mismatch
    return decision


def _block_valid(block: dict) -> bool:
    """One objective's part of the decision; a malformed one refuses only that objective."""
    return (set(block) == DECIDED and _facts_valid(block) and _texts(block["delivery"], MAX_DELIVERY, 512)
            and bool(block["delivery"]))


def _released(locations: list[dict]) -> str:
    return ", ".join(row["path"] + (" (absent)" if row["disposition"] == "absent" else " (existing worktree)")
                     for row in locations)


def _effects(row: dict) -> str:
    """What cleanup keeps holding for one objective after retirement, and the hold retirement ends (R113)."""
    facts, said = row["facts"], ""
    if facts["retained"]:
        said += (f" Objective {row['objective']} keeps holding its recorded worktree(s) it does not release: "
                 + ", ".join(facts["retained"]) + ".")
    if facts["unrecorded_locations"] == "hold_ends":
        said += (f" Objective {row['objective']}'s record does not list every location its work used, so while it "
                 "is open cleanup holds every task worktree of this repository for it; retirement ends that hold.")
    return said


def _question(eligible: list[dict], refused: list[dict], tally: dict) -> str:
    """One plain question naming every objective a decision on this request could bind, its paths and how each
    stands, and what cleanup holds for it afterwards (R113)."""
    names = [row["objective"] for row in eligible]
    standing = {row["path"]: row for item in eligible for row in item["facts"]["locations"]}
    question = (f"May Pod retire {'objective' if len(names) == 1 else 'objectives'} {', '.join(names)} as delivered "
                f"history whose coordinator is gone, releasing {_released([standing[key] for key in sorted(standing)])} "
                "for cleanup?")
    # Only a refusal that came after every fact was read, with every path an existing worktree or proven absent,
    # leaves facts a decision can bind (R112, R113); what that decision would end is said for it too.
    later = [row for row in refused if row["status"] == "refused" and row["facts"]["runs"]
             and row["facts"]["locations"]
             and all(item["disposition"] in RELEASABLE for item in row["facts"]["locations"])]
    never = [row for row in refused if row not in later]
    if any(item["disposition"] == "absent" for row in eligible + later for item in row["facts"]["locations"]):
        question += (" An absent path is gone from this host's filesystem, from the repository's Git worktree "
                     "registry and from Orca's complete worktree and terminal inventories; its missing directory "
                     "does not prove that work stopped, which the coordinator, Run and Dispatch checks prove.")
    question += "".join(_effects(row) for row in eligible)
    held = sum(tally.values())
    question += (f" Retirement does not close or complete {'it' if len(names) == 1 else 'them'}: {held} pending, "
                 "unknown, failed or unreported record(s) stay exactly as recorded, and the record will accept no "
                 "further writes.")
    if later:
        question += (" Not retirable now, but a decision that also names it retires it once its condition clears "
                     "and its facts still match: " + "; ".join(
                         f"{row['objective']} releasing {_released(row['facts']['locations'])} ({row['condition']}; "
                         f"{sum(counts(row['facts']['records']).values())} pending, unknown, failed or unreported "
                         "record(s) of it would stay as recorded)" for row in later) + "."
                     + "".join(_effects(row) for row in later))
    if never:
        question += (" Not retirable by a decision on these facts: " + "; ".join(
            f"{row['objective']} ({row['condition']})" for row in never) + ".")
    if refused:
        question += " Leave out of the decision any objective the owner does not want to end this way."
    return question


def historical_retirement(project: Path, objectives: object, *, decision: object | None = None,
                          port=None) -> dict:
    """Detect without a decision; retire with an exact one. Each objective succeeds or refuses alone."""
    from .github import repository_context
    from .ledger import _lock, _path, _read, _write_retirement, record_name
    from .operations import OrcaPort
    requested = _requested(objectives)
    context = repository_context(project, fresh=True)
    repository = context["repo_key"]
    if repository is None:
        raise PodError("retirement_scope_unavailable", "Historical retirement needs a Git repository",
                       {"next_action": "run it from the repository's canonical checkout"})
    if decision is not None:
        decision = _decision(decision, requested, repository)
    port = port or OrcaPort(project)
    paths = {row["objective"]: _path(project, row["objective"]) for row in requested}
    results = []
    with ExitStack() as stack:
        # Nothing is created for a name that has no record: a lock would make its directory.
        for path in sorted((path for path in paths.values() if path.exists()), key=str):
            stack.enter_context(_lock(path))
        for index, row in enumerate(requested):
            name, path = row["objective"], paths[row["objective"]]
            try:
                state = _read(path) if path.exists() else None
            except PodError as exc:
                verdict = {"objective": name, "status": "refused", "condition": "scope_unreadable",
                           "facts": {"objective": name, "owner": None, "runs": [], "release": row["release"],
                                     "locations": [], "retained": [], "unrecorded_locations": None,
                                     "records": []},
                           "message": f"The objective's record is unreadable or unsupported ({exc.code}).",
                           "next_action": "Historical retirement needs a readable current record."}
            else:
                # Each objective reads the host afresh: an earlier objective's inventory is never its proof.
                verdict = _detect(project, name, row["release"], state, port=port, repository=repository,
                                  host=_Host(project))
            verdict["record"] = record_name(project, context, name)
            if decision is None:
                results.append(verdict)
                continue
            block = decision["objectives"][index]
            facts = {key: block.get(key) for key in FACTS}
            if verdict["status"] == "retired":
                if state[FIELD]["decision"] == digest(decision):
                    results.append({**verdict, "status": "replayed",
                                    "message": "This decision's historical retirement is already recorded; "
                                               "nothing was written."})
                else:
                    results.append({**verdict, "status": "refused"})
                continue
            if verdict["status"] != "eligible":
                results.append(verdict)
                continue
            if not _block_valid(block) or facts != verdict["facts"]:
                # A decision made before R113 names no dispositions or reservations, so it never binds them (R113).
                changed = sorted(key for key in FACTS if facts[key] != verdict["facts"][key]) \
                    if _block_valid(block) else ["malformed"]
                results.append({**verdict, "status": "refused", "condition": "retirement_decision_mismatch",
                                "changed": changed,
                                "message": "The decision differs from fresh owner, Run, path, disposition, "
                                           "reservation or record facts. Nothing was written.",
                                "next_action": "detect again and confirm the changed facts with the owner"})
                continue
            if _moved(verdict["locations"]):
                results.append({**verdict, "status": "refused", "condition": "retirement_decision_mismatch",
                                "changed": ["locations"],
                                "message": "A released path changed, or could not be read again, after Pod read "
                                           "its facts. Nothing was written.",
                                "next_action": "detect again and confirm the changed facts with the owner"})
                continue
            entry = {"at": datetime.now(timezone.utc).isoformat(), "decision": digest(decision),
                     "instruction": decision["instruction"], "reason": decision["reason"],
                     "repository": repository, "objectives": [item["objective"] for item in requested],
                     "owner": facts["owner"], "runs": facts["runs"], "released": facts["release"],
                     "locations": facts["locations"], "preserved": facts["records"], "unfinished": verdict["unfinished"],
                     "delivery": list(block["delivery"]), "native": verdict["native"]}
            state[FIELD] = entry
            _write_retirement(path, state)
            results.append({**verdict, "status": "retired", "retirement": status_block(state),
                            "message": "Historical retirement recorded: the record is kept, not completed, and "
                                       "accepts no writes.",
                            "next_action": ACTION.format(name)})
    done = [row for row in results if row["status"] in ("retired", "replayed")]
    if decision is None:
        eligible = [row for row in results if row["status"] == "eligible"]
        tally = {"pending": 0, "unknown": 0, "failed": 0, "unreported": 0}
        for row in eligible:
            for key, value in counts(row["facts"]["records"]).items():
                tally[key] += value
        out = {"status": "retirement_available" if eligible else "retirement_unavailable",
               "repository": repository, "objectives": results}
        if eligible:
            out["question"] = _question(eligible, [row for row in results if row["status"] != "eligible"], tally)
            out["next_action"] = ("Ask the owner the question. Then rerun the request with only the objectives the "
                                  "owner's answer names, and a user_direct decision of exactly {provenance, "
                                  "instruction, repository, reason, objectives}, each of those objectives in order as "
                                  "{objective, owner, runs, release, locations, retained, unrecorded_locations, "
                                  "records, delivery} with these facts and its delivery references; a refused "
                                  "objective stays refused alone.")
        else:
            out["next_action"] = "Historical retirement fits no requested objective now; resolve each named condition."
        return out
    return {"status": "retired" if len(done) == len(results) else "partially_retired" if done else "refused",
            "repository": repository, "objectives": results}

"""Closure by evidence: delivered objectives always close, whatever happened to their coordinator (R144-R148).

One read-only delivery proof, one immutable end-of-life record written beside the objective's record (never inside
it), and one sweep that applies it to every open objective of a repository. The proof reuses R110's loss proof and
R112's Run, Dispatch and start-request checks unchanged. Proof never asks the owner, starts work or mutates Orca,
Git or a provider. After closure a separate exact bound-source effect may complete its issue; `context.json`,
`governor.json` and Pod history stay byte-identical, and a pending, UNKNOWN or
FAILED result is preserved as it reads, never stamped.
"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from .errors import PodError

SCHEMA = "pod-evidence-closure/v1"
FILE = "closure-evidence.json"
STATEMENT = "closed by evidence, coordinator gone"
BASIS = "evidence"
STATUSES = ("proved", "not_proved", "unverified", "not_applicable")
# Checks run cheapest first; the strongest class found decides the status.
_ORDER = ("not_applicable", "not_proved", "unverified")
MAX_HOPS = 8
MAX_MEMBERS = 16
MAX_PRS = 16
MAX_PRESERVED = 64
MAX_LISTED = 64
MAX_TEXT = 512
MAX_FILE = 256 * 1024
SWEEP_ACTION = "pod sweep closes it by evidence; no owner decision is needed"
LEAD_ACTION = ("A task for the project lead: resolve each named condition, then run pod sweep again; no owner "
               "decision is needed.")
RESULTS = ("closed", "already_closed", "not_proved", "unverified", "skipped")
_FIELDS = {"schema", "objective", "repository", "at", "statement", "basis", "caller", "relation", "tail", "pod",
           "record", "coordinator", "runs", "dispatches_settled", "source", "sources", "delivery", "chain",
           "preserved", "counts", "unfinished", "worktrees", "locations_complete"}
SUPPORTED = ("current", "older")


def _clip(value: object, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _text(value: object, limit: int = 1024) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- the stored record


def _presence(root: Path) -> str:
    """`present`, `absent`, or `unknown` when this host cannot inspect the objective's directory."""
    try:
        os.lstat(root / FILE)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def present(root: Path) -> bool:
    """Whether an evidence closure is recorded beside this objective's record. Only a recorded one reads as closed;
    an entry Pod cannot inspect is never proof of closure (and `refuse_closed` refuses writes on it)."""
    return _presence(root) == "present"


def load(root: Path) -> dict:
    """The stored closure, read bounded and without following a link; anything else raises."""
    try:
        fd = os.open(root / FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PodError("closure_evidence_unreadable", "The evidence closure is not a regular file")
            data = file.read(MAX_FILE + 1)
    except OSError as exc:
        raise PodError("closure_evidence_unreadable", "The evidence closure cannot be read") from exc
    try:
        value = json.loads(data.decode("utf-8")) if len(data) <= MAX_FILE else None
    except (ValueError, UnicodeError, RecursionError):
        value = None
    if (not isinstance(value, dict) or set(value) != _FIELDS or value["schema"] != SCHEMA
            or value["statement"] != STATEMENT or value["basis"] != BASIS
            or not all(_text(value[key]) for key in ("objective", "at", "caller"))
            or value["relation"] not in ("tail", "predecessor") or not isinstance(value["tail"], dict)
            or not isinstance(value["counts"], dict) or not isinstance(value["preserved"], list)
            or not isinstance(value["delivery"], list) or not isinstance(value["runs"], list)
            or not isinstance(value["sources"], list)
            or not isinstance(value["worktrees"], list) or type(value["locations_complete"]) is not bool):
        raise PodError("closure_evidence_unreadable", "The evidence closure is malformed")
    return value


def tail_closed(root: Path) -> bool:
    """Whether status reads this objective as closed by evidence: any entry but a continued predecessor's, which
    keeps reading continued with its entry beside it (C4, Amendment 1)."""
    if not present(root):
        return False
    try:
        return load(root)["relation"] != "predecessor"
    except PodError:
        return True  # A closure record that cannot be read still ends the objective; status says so.


def refuse_closed(root: Path) -> None:
    """Every write of an objective closed by evidence refuses, its own coordinator's included (R145, C4)."""
    presence = _presence(root)
    if presence == "absent":
        return
    if presence == "unknown":
        # Never write past a closure Pod cannot rule out; the code names the unreadable state, not a closure.
        raise PodError("unsafe_state", "Whether this objective is closed by evidence cannot be read; nothing was "
                       "written", {"next_action": "restore access to the objective's state directory, then retry"})
    try:
        at = load(root)["at"]
    except PodError:
        at = None
    raise PodError("objective_closed",
                   "This objective was closed by evidence, coordinator gone" + (f", at {at}" if at else "")
                   + "; its record accepts no writes",
                   {"detail": "objective_closed", "basis": BASIS, "closed_at": at,
                    "next_action": "Read it with pod status --objective NAME; nothing remains to record."})


def refuse_objective(project: Path, objective: str) -> None:
    from .ledger import objective_root
    refuse_closed(objective_root(project, objective))


def compact(value: dict) -> dict:
    """The list's compact block (R108): enough for cleanup to free what a closed objective held."""
    return {"statement": value["statement"], "basis": value["basis"], "at": value["at"],
            "relation": value["relation"], "tail": dict(value["tail"]),
            "runs": [row.get("run") for row in value["runs"] if isinstance(row, dict)][:MAX_LISTED],
            "delivery": [row.get("pr") for row in value["delivery"] if isinstance(row, dict)][:MAX_PRS],
            "counts": dict(value["counts"]), "worktrees": list(value["worktrees"]),
            "locations_complete": value["locations_complete"]}


def block(root: Path) -> dict | None:
    """Status's projection of the stored closure, or an explicit unreadable block; None when there is none."""
    if not present(root):
        return None
    try:
        value = load(root)
    except PodError as exc:
        return {"status": "unreadable", "code": exc.code}
    return {"status": "recorded", **{key: deepcopy(value[key]) for key in sorted(_FIELDS)}}


# --------------------------------------------------------------------------- reading records


def _record_class(raw: dict) -> str:
    """current, older (an earlier version of a current schema family), unknown (future or foreign) or malformed."""
    from .handoff import _older_schema
    from .installer import superseded_schemas
    from .ledger import _validate_context
    if superseded_schemas(raw):
        return "older" if _older_schema(raw) else "unknown"
    try:
        _validate_context(deepcopy(raw))
    except PodError:
        return "malformed"
    return "current"


def _documented(raw: dict, *, current: bool) -> dict:
    """The documented fields an older record is read through (C2.1), read the same way from every record."""
    from .ledger import ADMISSION_STATES

    def bad(what: str) -> PodError:
        return PodError("record_unreadable", f"The record's documented {what} is malformed")

    owner = raw.get("owner")
    if owner is not None and not _text(owner, 256):
        raise bad("owner")
    checkpoint = raw.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise bad("checkpoint")
    refs = checkpoint.get("native_refs", [])
    if not isinstance(refs, list) or len(refs) > 32:
        raise bad("Run references")
    runs = []
    for ref in refs:
        if not isinstance(ref, dict) or not _text(ref.get("runId"), 256):
            raise bad("Run references")
        runs.append(ref["runId"])
    admissions = raw.get("admissions", {})
    if not isinstance(admissions, dict) or len(admissions) > 256:
        raise bad("admissions")
    rows = {}
    for key, row in admissions.items():
        if not _text(key, 256) or not isinstance(row, dict) or row.get("state") not in ADMISSION_STATES \
                or not _text(row.get("run_id"), 256) or not isinstance(row.get("request_uuid"), (str, type(None))):
            raise bad("admissions")
        binding = row.get("native_binding")
        names = ("dispatchId", "runId", "taskId", "workerId", "worktreeId")
        if binding is not None and (not isinstance(binding, dict) or not all(_text(binding.get(name), 1024)
                                                                             for name in names)):
            if row["state"] == "bound":
                raise bad("admission identity")
            binding = None
        if row["state"] == "bound" and binding is None:
            raise bad("admission identity")
        report = row.get("report") if current else None
        rows[key] = {"state": row["state"], "run_id": row["run_id"], "request_uuid": row.get("request_uuid"),
                     "native_binding": {name: binding[name] for name in names} if binding else None,
                     "role": row.get("role") if current and isinstance(row.get("role"), str) else None,
                     # Only a current record's validated report is read; an older one counts as unreported.
                     "reported": bool(row["state"] == "bound" and isinstance(report, dict)
                                      and report.get("status") == "validated_observation")}
        runs.append(row["run_id"])
    issue = _record_issue(raw)
    worktree = checkpoint.get("worktree")
    return {"owner": owner, "objective": checkpoint.get("objective"), "runs": sorted(set(runs)),
            "admissions": rows, "issue": issue,
            "worktree": worktree.get("path") if isinstance(worktree, dict) and isinstance(worktree.get("path"), str)
            else None,
            "native_refs": [dict(ref) for ref in refs][:MAX_LISTED]}


def _record_issue(raw: dict) -> dict | None:
    """The documented issue binding alone, so a malformed work field cannot hide a recovery's membership."""
    checkpoint = raw.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise PodError("record_unreadable", "The record's documented checkpoint is malformed")
    source = checkpoint.get("objective_source")
    if source in (None, "direct_objective"):
        return None
    if isinstance(source, dict):
        from .github import parse_issue_locator
        try:
            issue = parse_issue_locator(source.get("locator"))
            if source.get("role") == "SEAL":
                issue["role"] = "SEAL"
            return issue
        except PodError as exc:
            raise PodError("record_unreadable", "The record's documented objective source is malformed") from exc
    raise PodError("record_unreadable", "The record's documented objective source is malformed")


def _read_record(root: Path) -> tuple[dict, bytes]:
    """The raw record and its exact bytes, bounded and without following links; never validated or converted."""
    from .history import _load
    raw, _, data = _load(root / "context.json", validate=False)
    if not isinstance(raw, dict):
        raise PodError("record_unreadable", "The record is not a JSON object")
    return raw, data


def _journal_bytes(root: Path) -> bytes | None:
    path = root / "governor.json"
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    from .governor import MAX_JOURNAL
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PodError("journal_unreadable", "The Governor journal is not a regular file")
            data = file.read(MAX_JOURNAL + 1)
    except OSError as exc:
        raise PodError("journal_unreadable", "The Governor journal cannot be read") from exc
    if len(data) > MAX_JOURNAL:
        raise PodError("journal_unreadable", "The Governor journal exceeds its bound")
    return data


ROOT_UNREADABLE = "state_root_unreadable"
BEYOND_BOUND = "beyond_scan_bound"
UNATTRIBUTED = "unattributed"


def _scan(project: Path, *, orphans: bool = False) -> tuple[list[dict], list[dict]]:
    """Every readable record of this repository, any schema, and what kept the scan from reading all of them.

    The second list names each problem as `{record, code}`: a record directory that could not be read, one whose
    record names no objective (`unattributed`), the state root itself (`state_root_unreadable`), or the directories
    past the scan's own bound (`beyond_scan_bound` with their `count`). `orphans` also names a directory holding
    Pod files without the record that names them, as R108's list does."""
    from .github import repository_context
    from .ledger import _record_exists, record_name, state_root
    from .listing import MAX_SCAN, _name
    context = repository_context(project)
    root = state_root(project)
    try:
        if root.is_symlink():
            return [], [{"record": None, "code": ROOT_UNREADABLE}]
        with os.scandir(root) as frontier:
            entries = sorted((entry for entry in frontier if not entry.name.startswith(".")),
                             key=lambda entry: entry.name)
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [{"record": None, "code": ROOT_UNREADABLE}]
    found = []
    unreadable = ([] if len(entries) <= MAX_SCAN
                  else [{"record": None, "code": BEYOND_BOUND, "count": len(entries) - MAX_SCAN}])
    for entry in entries[:MAX_SCAN]:
        try:
            if entry.is_symlink():
                unreadable.append({"record": entry.name, "code": "unsafe_state"})
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not _record_exists(Path(entry.path) / "context.json"):
                if orphans:
                    with os.scandir(entry.path) as contents:
                        if any(item.name != ".lock" for item in contents):
                            unreadable.append({"record": entry.name, "code": UNATTRIBUTED})
                continue
            raw, _ = _read_record(Path(entry.path))
        except PodError as exc:
            unreadable.append({"record": entry.name, "code": exc.code})
            continue
        except OSError:
            unreadable.append({"record": entry.name, "code": "unreadable"})
            continue
        name = _name(raw)
        if name is None:
            unreadable.append({"record": entry.name, "code": UNATTRIBUTED})
            continue
        if record_name(project, context, name) == entry.name:
            found.append({"objective": name, "root": Path(entry.path), "raw": raw})
    return found, unreadable


# --------------------------------------------------------------------------- the proof


class _Proof:
    def __init__(self, objective: str):
        self.conditions: list[dict] = []
        self.facts: dict = {"objective": objective}

    def add(self, status: str, code: str, message: str) -> None:
        row = {"code": code, "status": status, "message": message}
        if len(self.conditions) < MAX_LISTED and row not in self.conditions:
            self.conditions.append(row)

    def result(self) -> dict:
        found = {row["status"] for row in self.conditions}
        status = next((name for name in _ORDER if name in found), "proved")
        return {"status": status, "conditions": list(self.conditions), "facts": self.facts}


class _Remote:
    """The GitHub port, made only when a check reads GitHub."""

    def __init__(self, project: Path, port=None):
        self.project, self._port = project, port

    def get(self):
        if self._port is None:
            from .governor_effects import _port
            self._port = _port(self.project)
        return self._port


def _gone(identity: dict) -> bool:
    return identity["state"] == "lost" or bool(identity.get("exited"))


def _phase(root: Path, raw: dict) -> str:
    from .listing import _phase as listed
    return "closed" if present(root) and listed(raw) == "open" else listed(raw)


def _settled_record(project: Path, row: dict, *, frozen: frozenset[str] = frozenset()) -> bool:
    """Whether a recovery coordinator's own record is finished: closed, closed by evidence, ended by the owner's
    historical retirement, or continued along a chain that ended closed (R110). A certified frozen predecessor in
    this proof's complete chain has also ended authority; its native work is still checked. A record on a schema
    Pod does not know proves nothing finished, whatever its raw fields say."""
    from .succession import chain_state
    if present(row["root"]):
        return True
    if _record_class(row["raw"]) not in SUPPORTED:
        return False
    phase = _phase(row["root"], row["raw"])
    if phase in ("closed", "retired"):
        return True
    return phase == "continued" and (row["objective"] in frozen
                                     or chain_state(project, row["raw"])["state"] == "successor_closed")


def prove(project: Path, objective: str, *, port=None, remote=None, now: datetime | None = None,
          readback: bool = True) -> dict:
    """Whether this objective's delivery is proved (C2), read-only: never writes, locks or asks anyone.

    Preserved results never decide the status, so a reader that only asks whether delivery is proved passes
    `readback=False` and Pod's provider readback of pending rows waits for the closure write.

    Returns `{status, conditions, facts}`: `proved`, `not_proved` (a task for the project lead), `unverified`
    (something could not be read) or `not_applicable` (closed, continued, under historical retirement, live, or a
    schema Pod does not know). Every unmet condition found is listed, cheapest checks first.
    """
    from .ledger import _record_exists, objective_root
    from .operations import OrcaPort
    project = Path(project)
    proof = _Proof(objective)
    try:
        root = objective_root(project, objective)
        if not _record_exists(root / "context.json"):
            proof.add("not_applicable", "objective_unknown", "No record of that objective exists in this repository.")
            return proof.result()
        raw, data = _read_record(root)
    except PodError as exc:
        proof.add("unverified", "record_unreadable", f"The objective's record cannot be read ({exc.code}).")
        return proof.result()
    kind = _record_class(raw)
    if kind == "unknown":
        proof.add("not_applicable", "schema_unknown", "The record uses a schema this Pod does not know; it is never "
                  "closed by evidence.")
        return proof.result()
    if kind == "malformed":
        proof.add("unverified", "record_unreadable", "The record does not validate under its schema.")
        return proof.result()
    try:
        doc = _documented(raw, current=kind == "current")
        journal_data = _journal_bytes(root)
    except PodError as exc:
        proof.add("unverified", "record_unreadable", f"A documented field of the record is unreadable ({exc.code}).")
        return proof.result()
    from .listing import _facts
    located = _facts(raw)
    proof.facts.update(record={"class": kind, "context_sha256": _sha(data),
                               "governor_sha256": _sha(journal_data) if journal_data is not None else None},
                       worktrees=located["worktrees"][:MAX_LISTED], locations_complete=located["locations_complete"],
                       native_refs=doc["native_refs"])
    # 2. Phase: what already ended needs no proof.
    if present(root):
        proof.add("not_applicable", "objective_closed", "The objective is already closed by evidence.")
        return proof.result()
    phase = _phase(root, raw)
    if phase != "open":
        code = {"closed": "objective_closed", "retired": "objective_retired",
                "continued": "objective_continued"}[phase]
        proof.add("not_applicable", code, {"closed": "Its coordinator recorded its closure.",
                                           "retired": "The owner's historical retirement covers it.",
                                           "continued": "A successor continues it; its chain tail closes it."}[phase])
        return proof.result()
    try:
        _checks(project, objective, root, raw, kind, doc, proof, port=port or OrcaPort(project), remote=remote,
                now=now, readback=readback)
    except PodError as exc:
        # A fact the proof needs could not be read: nothing is closed on it (C2).
        proof.add("unverified", "record_unreadable", f"A fact the proof needs could not be read ({exc.code}).")
    return proof.result()


def _checks(project: Path, objective: str, root: Path, raw: dict, kind: str, doc: dict, proof: _Proof, *, port,
            remote, now: datetime | None, readback: bool) -> None:
    """Conditions 2 to 8 after the record is read, cheapest first; each unmet one is added to `proof`."""
    _claims(project, objective, raw, kind, proof)
    remote = remote if isinstance(remote, _Remote) else _Remote(project, remote)
    scan: dict = {}

    def records() -> tuple[list[dict], list[dict]]:
        if not scan:
            scan["found"], scan["unreadable"] = _scan(project)
        return scan["found"], scan["unreadable"]

    owner = _coordinator(doc, proof)
    if owner is False:
        return
    state = raw if kind == "current" else None
    members = _members(project, objective, raw, kind, doc, proof, records)
    if members is None:
        return  # An unreadable or over-bound membership cannot prove a partial delivery.
    # Only a whole, classified chain can exempt its frozen predecessors from each other's holder check. Every
    # predecessor's native work is still proved below; a distinct open recovery remains outside this exemption.
    frozen = frozenset(row["objective"] for row in members if row["relation"] == "predecessor"
                       and proof.facts["chain_complete"])
    runs, dispatches = _runs(project, doc, owner, proof, records, frozen=frozen)
    proof.facts.update(runs=runs, dispatches_settled=_work(doc, dispatches, proof, port=port))
    proof.facts["predecessor_runs"] = _predecessor_work(project, members, proof, records, port=port, frozen=frozen)
    _source(members, doc, state, proof, remote)
    _delivery(project, members, proof, remote)
    _preserved(project, root, doc, state, proof, remote, now=now, readback=readback)
    from .obligations import TERMINAL
    from .ledger import map_of
    obligations = ((map_of(state) or {}).get("obligations") or []) if state is not None else []
    proof.facts["unfinished"] = [ob["id"] for ob in obligations if ob.get("state") not in TERMINAL][:MAX_LISTED]


def _claims(project: Path, objective: str, raw: dict, kind: str, proof: _Proof) -> None:
    """R118's one predicate for an incomplete, unverified or ambiguous claim, unchanged."""
    if kind == "current":
        from .succession import incoming_claim, reserved_for
        if reserved_for(raw) is not None:
            proof.add("unverified", "claim_incomplete", "Its own claim of a predecessor is incomplete.")
            return
        found = incoming_claim(project, objective, raw, full=True)
        if found is None:
            return
        if "ambiguous" in found:
            proof.add("unverified", "claim_ambiguous", "More than one predecessor names it as successor.")
        elif found["predecessor"] is not None:
            proof.add("unverified", "claim_incomplete", f"An incomplete claim of {found['predecessor']} names it.")
        else:
            proof.add("unverified", "claim_unverified", "A Pod record that could name it as a claimed successor "
                      "is unreadable.")
        return
    from .retirement import _claimants
    claimants, unreadable = _claimants(project, objective)
    if claimants:
        proof.add("unverified", "claim_incomplete", f"Objective {claimants[0]} records it as its successor.")
    elif unreadable:
        proof.add("unverified", "claim_unverified", "A Pod record that could name it as a claimed successor is "
                  "unreadable.")


def _coordinator(doc: dict, proof: _Proof):
    """R110's loss proof. Returns the owner, None when unrecorded, or False when the coordinator is live."""
    from .orca import terminal_identity
    owner = doc["owner"]
    if not owner:
        proof.facts["coordinator"] = {"handle": None, "liveness": "unrecorded", "cause": None}
        proof.add("not_proved", "owner_unrecorded", "The objective records no coordinator.")
        return None
    identity = terminal_identity(owner)
    if identity["state"] == "live":
        proof.facts["coordinator"] = {"handle": owner, "liveness": "live", "cause": None}
        proof.add("not_applicable", "coordinator_live", "Orca reports its coordinator live; the coordinator records "
                  "closure itself.")
        return False
    if identity["state"] == "lost":
        proof.facts["coordinator"] = {"handle": owner, "liveness": "lost", "cause": _clip(identity.get("code"), 64)}
    elif identity.get("exited"):
        proof.facts["coordinator"] = {"handle": owner, "liveness": "exited", "cause": _clip(identity.get("cause"), 64)}
    else:
        proof.facts["coordinator"] = {"handle": owner, "liveness": "unknown",
                                      "cause": _clip(identity.get("code"), 64)}
        proof.add("unverified", "owner_unverified", "Orca cannot prove the coordinator gone ("
                  + str(identity.get("code") or identity["state"]) + ").")
    return owner


def _runs(project: Path, doc: dict, owner: str | None, proof: _Proof, records, *,
          whose: str | None = None, frozen: frozenset[str] = frozenset()) -> tuple[list[dict], dict | None]:
    """Each recorded Run of one record: who holds it now (R146 rebinding) and its complete worker list (R112).

    `whose` names a continued predecessor, whose conditions then say so; the objective's own conditions name no
    one. Returns the Run facts read and the worker Dispatches, or None for them when a list could not be read."""
    from .orca import run_show, terminal_identity, worker_rows
    from .retirement import fleet_dispatches

    def add(status: str, code: str, message: str) -> None:
        proof.add(status, code, message if whose is None else f"Predecessor objective {whose}: {message}")
    runs, dispatches, unread = [], {}, False
    if not doc["runs"]:
        add("not_proved", "runs_unrecorded", "The objective records no Run whose work could be proven settled.")
        return [], None
    held = []
    for run in doc["runs"][:32]:
        try:
            shown = run_show(run)
        except PodError as exc:
            runs.append({"run": run, "generation": None, "holder": None})
            add("unverified", "workers_unverified", f"A recorded Run could not be read ({exc.code}).")
            unread = True
            continue
        holder = shown["coordinator_handle"]
        runs.append({"run": run, "generation": shown["consumer_generation"], "holder": holder})
        if not shown.get("coordinator_known", False):
            # A reply that omits the coordinator proves nobody absent: only an explicit null is an unbound Run.
            runs[-1]["holder_known"] = False
            add("unverified", "workers_unverified", "Orca's reply for a recorded Run does not say who "
                "coordinates it, so a holder cannot be ruled out.")
        elif holder is not None and (holder != owner or whose is not None) \
                and not _gone(terminal_identity(holder)):
            # A live (or unproven) holder: its own records referencing this Run must all be finished.
            # Completed claims ended authority over every certified frozen predecessor in this chain. Other
            # objectives that terminal records on the Run must still be finished, even if its handle did not change.
            claimed_owner = holder == owner and whose is not None
            found, unreadable = records()
            mine = [row for row in found if row["raw"].get("owner") == holder and run in _record_runs(row["raw"])
                    and not (claimed_owner and row["objective"] == whose)]
            if ((not mine and not claimed_owner) or unreadable
                    or not all(_settled_record(project, row, frozen=frozen) for row in mine)):
                held.append(run)
            elif mine:
                runs[-1].update(rebound="rebound_settled",
                                settled_with=sorted(row["objective"] for row in mine)[:MAX_LISTED])
        try:
            fleet = worker_rows(run)
        except PodError as exc:
            add("unverified", "workers_unverified", f"A Run's worker list could not be read ({exc.code}).")
            unread = True
            continue
        found, refused = fleet_dispatches(fleet)
        if refused is not None:
            add("unverified", *refused)
            unread = True
            continue
        for dispatch in found:
            dispatches[dispatch] = None
    if held:
        add("not_proved", "run_held", "A live coordinator of an open objective holds a recorded Run; it settles "
            "that Run first.")
    return runs, None if unread else dispatches


def _record_runs(raw: dict) -> list[str]:
    from .listing import _facts
    return _facts(raw)["runs"]


def _work(doc: dict, dispatches: dict | None, proof: _Proof, *, port, whose: str | None = None) -> int:
    """R112's work proof, unchanged: no live or unsettled Dispatch and no start Orca still holds pending. Returns
    how many Dispatches read settled; `whose` names a continued predecessor in its condition."""
    from .retirement import work_activity
    if dispatches is None:
        return 0
    activity, settled = work_activity(doc["admissions"], dispatches, port=port)
    if activity is not None:
        code, message, _ = activity
        message = message.replace("retirement waits for it", "closure waits for it")
        proof.add("not_proved" if code == "work_live" else "unverified", code,
                  message if whose is None else f"Predecessor objective {whose}: {message}")
    return settled


def _member_work(project: Path, row: dict, proof: _Proof, records, *, port,
                frozen: frozenset[str] = frozenset()) -> dict:
    """One continued predecessor's own Runs and native work, read now with the objective's own checks (R110's
    holder rule with R146's rebinding, generation recorded and never blocking, complete worker lists, settled
    Dispatches, start requests): what its closure entry records. The claim ended its coordinator's authority over
    each certified predecessor; any distinct open recovery that owner holds on the Run still blocks closure."""
    runs, dispatches = _runs(project, row["doc"], row["doc"]["owner"], proof, records,
                            whose=row["objective"], frozen=frozen)
    return {"objective": row["objective"], "runs": runs,
            "dispatches_settled": _work(row["doc"], dispatches, proof, port=port, whose=row["objective"])}


def _predecessor_work(project: Path, members: list[dict] | None, proof: _Proof, records, *, port,
                     frozen: frozenset[str] = frozenset()) -> list[dict]:
    """Every record whose closure entry the tail's closure writes is proved fresh, not only the tail (R146): each
    continued predecessor's Runs and work, nearest first. A predecessor Run held by a live coordinator of an open
    objective holds the whole chain (`run_held`)."""
    return [_member_work(project, row, proof, records, port=port, frozen=frozen) for row in members or []
            if row["relation"] == "predecessor"]


def _chain_issues(objective: str, doc: dict, members: list[dict] | None) -> list[tuple[dict, str]]:
    """Every distinct issue source the delivery chain binds, with the member that binds it: the objective's own
    first, then each predecessor's along the chain (R146). A recovery member binds the issue of the member whose
    Run it shares."""
    rows = [(doc["issue"], objective)] + [(row["doc"]["issue"], row["objective"]) for row in members or []
                                          if row["relation"] != "objective"]
    found, seen = [], set()
    for issue, name in rows:
        if issue is not None and issue["locator"].casefold() not in seen:
            seen.add(issue["locator"].casefold())
            found.append((issue, name))
    return found


def _source(members: list[dict] | None, doc: dict, state: dict | None, proof: _Proof, remote: _Remote) -> None:
    """PoDs all ticked, or the issue closed as completed, for every issue source the delivery chain binds (R145,
    R146); a claim-only successor without its own binding is proved by its chain's. A standalone objective with no
    issue source anywhere in its chain is proved by its own recorded criteria and verified delivery record."""
    objective = proof.facts["objective"]
    issues = _chain_issues(objective, doc, members)
    if not issues:
        proved, bound_by = _direct_source(objective, state, members)
        proof.facts["source"] = {"locator": None, "repository": None, "number": None,
                                 "proof": "recorded_criteria" if proved else None, "state": None,
                                 "state_reason": None, "pod_lines": None, "body_sha256": None, "bound_by": bound_by}
        proof.facts["sources"] = []
        if not proved:
            proof.add("not_proved", "source_unproved", "A direct objective needs every criterion satisfied or "
                      "withdrawn, at least one satisfied, and a verified delivery record of its own or its "
                      "completed claimed chain; each claim-only successor needs its own criteria satisfied.")
        return
    for current in [state, *[member.get("raw") for member in members or [] if member.get("kind") == "current"]]:
        if any(ob.get("kind") == "criterion" and "renews" in ob and ob.get("state") not in ("satisfied", "withdrawn", "renewed")
               for ob in ((current or {}).get("checkpoint") or {}).get("obligations", [])):
            proof.add("not_proved", "renewal_pending", "Renewed criteria need fresh proof before evidence closure; existing issue ticks do not prove the replacement.")
    proof.facts["sources"] = []
    for issue, bound_by in issues:
        if issue.get("role") == "SEAL":
            from .lineage import Lineage
            bound = next((row["raw"] for row in members or [] if row["objective"] == bound_by), state)
            lineage = Lineage(((bound or {}).get("checkpoint") or {}).get("obligations", []))
            criteria = [row for row in lineage.rows.values() if row.get("kind") == "criterion"
                        and lineage.effective(row["id"]) == row["id"]]
            if not criteria or any(row.get("state") not in ("satisfied", "withdrawn") for row in criteria):
                proof.add("not_proved", "source_unproved", f"{bound_by}'s SEAL completion still needs every live criterion, including unmapped handoff and delivery criteria.")
        facts = {"locator": issue["locator"], "repository": issue["repository"], "number": issue["number"],
                 "proof": None, "state": None, "state_reason": None, "pod_lines": None, "body_sha256": None,
                 "bound_by": bound_by}
        proof.facts["sources"].append(facts)
        whose = "The source issue" if bound_by == objective else f"Source issue {issue['locator']} of {bound_by}"
        try:
            row = remote.get().issue_state(repository=issue["repository"], number=issue["number"])
        except PodError as exc:
            proof.add("unverified", "source_unreadable", f"{whose} could not be read on GitHub ({exc.code}).")
            continue
        from .github import pod_lines
        recognized = pod_lines(row["body"], seal=issue.get("role") == "SEAL")
        lines = [line for rows in recognized.values() for line in rows]
        ticked = sum(line["ticked"] for line in lines)
        facts.update(state=row["state"], state_reason=_clip(row.get("state_reason"), 64),
                     pod_lines={"ticked": ticked, "total": len(lines)}, body_sha256=_sha(row["body"].encode()))
        if lines and ticked == len(lines):
            facts["proof"] = "pod_ticks"
        elif row["state"] == "CLOSED" and row.get("state_reason") == "COMPLETED":
            facts["proof"] = "issue_completed"
        else:
            proof.add("not_proved", "source_unproved", f"{whose} has {ticked} of {len(lines)} PoD line(s) "
                      f"ticked and is {row['state'].lower()}" + (f" ({row.get('state_reason')})"
                                                                  if row.get("state_reason") else "") + ".")
    proof.facts["source"] = dict(proof.facts["sources"][0])


def _direct_source(objective: str, state: dict | None, members: list[dict] | None) -> tuple[bool, str]:
    """A direct source is a current record's satisfied criteria and R93 delivery. A claim-only successor with
    satisfied criteria can use its completed predecessor's source, walking only the certified succession chain;
    recovery peers supply no direct source. Delivery PRs are still proved separately over every member."""
    from .ledger import map_of
    from .governance import validate_delivery_record
    rows = [(objective, state)] + [(row["objective"], row["raw"] if row["kind"] == "current" else None)
                                  for row in members or [] if row["relation"] == "predecessor"]
    for name, current in rows:
        mapped = map_of(current) if current is not None else None
        from .lineage import Lineage
        lineage = Lineage((mapped or {}).get("obligations") or [])
        criteria = [ob for ob in lineage.rows.values() if ob.get("kind") == "criterion" and lineage.effective(ob["id"]) == ob["id"]]
        if (not criteria or not all(ob.get("state") in ("satisfied", "withdrawn") for ob in criteria)
                or not any(ob.get("state") == "satisfied" for ob in criteria)):
            return False, name
        delivery = (mapped or {}).get("delivery")
        if delivery is not None:
            try:
                validate_delivery_record(delivery)
            except PodError:
                return False, name
            return True, name
    return False, objective


def _lineage(project: Path, raw: dict) -> tuple[list[dict], tuple[str, str, str] | None]:
    """A chain tail's continued predecessors, nearest first, and why the walk could not reach the chain's head.

    Each step follows a completed claim (`continues` without a pending state) whose predecessor names this record
    back as its successor, at most eight steps (R146). Every predecessor is classified before it is used: a record
    on a schema Pod does not know, or one that does not validate under its own, ends the walk. Returns the rows read
    (`objective`, `root`, `raw`, `kind`, `doc`) and None when the walk reached a record that continues nothing, or
    `(status, code, message)` naming what kept the chain from being read whole: a bound crossed, a loop, a link the
    other side does not confirm, an incomplete claim, or a record that is unreadable or unsupported. Never certify
    the part of a chain it could read."""
    from .ledger import _record_exists, objective_root
    from .listing import _name
    found, current = [], raw
    seen = {_name(raw)}
    while True:
        back = current.get("continues")
        name = _name(current)
        if back is None:
            return found, None
        if not isinstance(back, dict) or not _text(back.get("objective"), 1024):
            return found, ("unverified", "chain_inconsistent", f"Objective {name} records a malformed claim of its "
                           "predecessor.")
        prior_name = back["objective"]
        if back.get("state") is not None:
            return found, ("unverified", "claim_incomplete", "Its own claim of a predecessor is incomplete."
                           if not found else f"The claim of {prior_name} by {name} is incomplete.")
        if prior_name in seen:
            return found, ("unverified", "chain_inconsistent", f"The succession chain loops back to {prior_name}.")
        if len(found) == MAX_HOPS:
            return found, ("not_proved", "scope_too_large", f"The succession chain continues past {MAX_HOPS} "
                           "predecessors; Pod cannot prove the delivery of all of it.")
        root = objective_root(project, prior_name)
        try:
            if not _record_exists(root / "context.json"):
                raise PodError("record_unreadable", "The linked record is missing")
            prior, _ = _read_record(root)
        except (PodError, OSError):
            return found, ("unverified", "record_unreadable", f"Linked objective {prior_name} cannot be read.")
        link = prior.get("continued_by")
        if _name(prior) != prior_name or not isinstance(link, dict) or link.get("objective") != name:
            return found, ("unverified", "chain_inconsistent", f"Linked objective {prior_name} does not name {name} "
                           "as its successor.")
        kind = _record_class(prior)
        if kind not in SUPPORTED:
            return found, (("unverified", "schema_unknown", f"Linked objective {prior_name} uses a schema this Pod "
                            "does not know; its chain is never closed by evidence.") if kind == "unknown"
                           else ("unverified", "record_unreadable", f"Linked objective {prior_name} does not "
                                 "validate under its schema."))
        try:
            doc = _documented(prior, current=kind == "current")
        except PodError:
            return found, ("unverified", "record_unreadable", f"A documented field of linked objective {prior_name} "
                           "is unreadable.")
        seen.add(prior_name)
        found.append({"objective": prior_name, "root": root, "raw": prior, "kind": kind, "doc": doc})
        current = prior


def _members(project: Path, objective: str, raw: dict, kind: str, doc: dict, proof: _Proof,
             records) -> list[dict] | None:
    """The delivery chain: the objective, its continued predecessors (R146's walk, at most eight) and, for every
    member, this repository's records that share one of that member's recorded Runs and bind that member's source
    issue: a recovery of a predecessor's Run belongs to the delivery as much as one of the objective's own. Every
    member is identified by its readable Run and source before its other documented fields are validated. An
    identifiable but unreadable member stays in the chain and holds proof, never disappearing from it."""
    from .ledger import objective_root
    members = {objective: {"objective": objective, "root": objective_root(project, objective), "raw": raw,
                           "kind": kind, "doc": doc, "relation": "objective"}}
    lineage, problem = _lineage(project, raw)
    for row in lineage:
        members[row["objective"]] = {**row, "relation": "predecessor"}
    proof.facts["chain_complete"] = problem is None
    if problem is not None:
        proof.add(*problem)
    # Each member's own Runs and issue, recovery members included, until no member adds another or the bound is
    # crossed; the bound then refuses below, never a part of the chain.
    pending, read, invalid = list(members.values()), {}, False
    while pending and len(members) <= MAX_MEMBERS:
        base = pending.pop(0)
        issue, shared = base["doc"]["issue"], set(base["doc"]["runs"])
        if issue is None or not shared:
            continue
        found, _ = records()
        for row in found:
            if row["objective"] in members:
                continue
            if row["objective"] not in read:
                try:
                    read[row["objective"]] = {"issue": _record_issue(row["raw"]),
                                              "runs": _record_runs(row["raw"])}
                except PodError:
                    read[row["objective"]] = None
            other = read[row["objective"]]
            if other is None:
                continue
            if (other["issue"] is not None and other["issue"]["locator"].casefold() == issue["locator"].casefold()
                    and set(other["runs"]) & shared):
                peer = _record_class(row["raw"])
                detail = None
                if peer == "unknown":
                    proof.add("unverified", "schema_unknown", f"Recovery objective {row['objective']}, which shares "
                              f"a Run and source with {base['objective']}, uses a schema this Pod does not know.")
                    invalid = True
                elif peer not in SUPPORTED:
                    detail = "does not validate under its schema."
                else:
                    try:
                        other = _documented(row["raw"], current=peer == "current")
                    except PodError:
                        detail = "has an unreadable documented field."
                if detail is not None:
                    proof.add("unverified", "record_unreadable", f"Recovery objective {row['objective']}, which "
                              f"shares a Run and source with {base['objective']}, {detail}")
                    invalid = True
                members[row["objective"]] = {**row, "kind": peer, "doc": other, "relation": "recovery"}
                pending.append(members[row["objective"]])
    rows = list(members.values())
    proof.facts["chain"] = [{"objective": row["objective"], "relation": row["relation"],
                             "phase": _phase(row["root"], row["raw"])} for row in rows][:MAX_MEMBERS]
    if len(rows) > MAX_MEMBERS:
        proof.add("not_proved", "scope_too_large", f"The delivery chain has more than {MAX_MEMBERS} objectives.")
        return None
    if invalid:
        proof.facts["chain_complete"] = False
        return None
    return rows


def _journal_prs(root: Path) -> list[int] | None:
    """The pull requests a Governor journal records, from its documented fields only; None when unreadable."""
    try:
        data = _journal_bytes(root)
        value = json.loads(data.decode("utf-8")) if data is not None else {"actions": []}
    except (PodError, ValueError, UnicodeError, RecursionError):
        return None
    actions = value.get("actions") if isinstance(value, dict) else None
    if not isinstance(actions, list):
        return None
    found = []
    for row in actions:
        if not isinstance(row, dict):
            continue
        action = row.get("action") if isinstance(row.get("action"), dict) else {}
        receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
        provider = receipt.get("provider") if isinstance(receipt.get("provider"), dict) else {}
        for value in ((action.get("pr") if action.get("kind") == "merge" else None), provider.get("pr")):
            if type(value) is int and value > 0:
                found.append(value)
    return found


def _delivery(project: Path, members: list[dict] | None, proof: _Proof, remote: _Remote) -> None:
    """Every delivery pull request any member records, each merged as GitHub reads it now (R145)."""
    proof.facts["delivery"] = []
    if members is None:
        return
    recorded: dict[int, str] = {}
    for row in members:
        numbers = _journal_prs(row["root"]) or []
        delivery = ((row["raw"].get("checkpoint") or {}).get("delivery") or {})
        provider = delivery.get("provider") if isinstance(delivery, dict) else None
        if isinstance(provider, dict) and type(provider.get("pr")) is int and provider["pr"] > 0:
            numbers.append(provider["pr"])
        for number in numbers:
            recorded.setdefault(number, row["objective"])
    if not recorded:
        if not proof.facts.get("chain_complete", True):
            return  # An unread member may record its PRs; never assert absence from a partial chain.
        if members and any(row["doc"]["issue"] is not None for row in members):
            _linked_delivery(project, members, proof, remote)
            return
        # A chain Pod could not read whole may record its pull requests in a member it could not read; that
        # member's own condition already holds the proof, so nothing is claimed unrecorded.
        if proof.facts.get("chain_complete", True):
            proof.add("not_proved", "delivery_unrecorded", "No delivery pull request is recorded for this objective "
                      "or its chain.")
        return
    if len(recorded) > MAX_PRS:
        proof.add("not_proved", "scope_too_large", f"The chain records more than {MAX_PRS} pull requests.")
        return
    from .github import repository_context
    repository = repository_context(project)["repository"]
    for number in sorted(recorded):
        if repository is None:
            proof.add("unverified", "pr_unreadable", "The repository has no GitHub identity to read its pull "
                      "requests from.")
            return
        try:
            row = remote.get().pr_view(repository=repository, number=number)
        except PodError as exc:
            proof.add("unverified", "pr_unreadable", f"Pull request #{number} could not be read ({exc.code}).")
            continue
        if row["state"] == "MERGED":
            proof.facts["delivery"].append({"pr": number, "url": _clip(row.get("url")),
                                            "merged_at": _clip(row.get("merged_at"), 64),
                                            "merge_commit": _clip(row.get("merge_commit"), 64),
                                            "recorded_by": recorded[number]})
        else:
            proof.add("not_proved", "pr_not_merged", f"Pull request #{number} is {row['state'].lower()}, not merged.")


def _linked_delivery(project: Path, members: list[dict], proof: _Proof, remote: _Remote) -> None:
    """Read linked proof only when no PR was recorded; never replace recorded-PR checks or create R93."""
    from .github import repository_context
    repository = repository_context(project)["repository"]
    seen_sources, linked = set(), {}
    for member in members:
        issue = member["doc"]["issue"]
        if issue is None or issue["locator"].casefold() in seen_sources:
            continue
        seen_sources.add(issue["locator"].casefold())
        governance = (member["raw"].get("checkpoint") or {}).get("governance") or {}
        target = governance.get("base_ref")
        prefix = "refs/remotes/origin/"
        if (repository is None or repository.casefold() != issue["repository"].casefold()
                or not isinstance(target, str) or not target.startswith(prefix) or not target[len(prefix):]):
            proof.add("unverified", "target_identity_unavailable",
                      f"{member['objective']} has no retained exact GitHub target identity; the project lead must restore genuine delivery facts.")
            continue
        try:
            rows = remote.get().closing_prs(repository=issue["repository"], number=issue["number"])
        except (PodError, AttributeError) as exc:
            code = getattr(exc, "code", "linked_read_unavailable")
            proof.add("not_proved" if code == "scope_too_large" else "unverified", code,
                      f"Linked delivery for {issue['locator']} could not be proved ({code}).")
            continue
        matching = [row for row in rows if row["base_repository"].casefold() == repository.casefold()
                    and row["base_branch"] == target[len(prefix):] and row["state"] == "MERGED"]
        if not matching:
            proof.add("not_proved", "delivery_unrecorded", f"No merged linked pull request targets {repository}/{target[len(prefix):]} for {issue['locator']}.")
        for row in matching:
            linked.setdefault(row["number"], {"pr": row["number"], "url": row["url"],
                "merged_at": row["merged_at"], "merge_commit": row["merge_commit"],
                "recorded_by": member["objective"], "proof": "github_link"})
    if len(linked) > MAX_PRS:
        proof.add("not_proved", "scope_too_large", f"Linked delivery across the chain exceeds {MAX_PRS} pull requests.")
    else:
        proof.facts["delivery"] = [linked[key] for key in sorted(linked)]


def _preserved(project: Path, root: Path, doc: dict, state: dict | None, proof: _Proof, remote: _Remote, *,
               now: datetime | None, readback: bool) -> None:
    """Pending, UNKNOWN and FAILED results kept as they read: reconciled only by Pod's read-only readback (R118's
    `_observe`), never stamped and never written back (C2.8)."""
    from .governor import _read_journal
    rows = []
    try:
        journal = _read_journal(root / "governor.json")
    except PodError:
        journal = None
        try:
            data = _journal_bytes(root)
        except PodError:
            data = None
        rows.append({"kind": "journal", "outcome": "UNKNOWN", "sha256": _sha(data) if data is not None else None})
    moment = now or datetime.now(timezone.utc)
    for row in (journal or {}).get("actions", []):
        if row["decision"] != "ALLOW" or row["outcome"] not in ("pending", "UNKNOWN", "FAILED"):
            continue
        item = {"kind": "governor", "id": row["record_id"], "action": row["action"]["kind"],
                "candidate": _clip(row.get("candidate_id") or row["action"]["candidate"], 256),
                "recorded": row["outcome"], "outcome": row["outcome"] if row["outcome"] == "FAILED" else "UNKNOWN",
                "reconciled": False, "evidence": None, "reason": None}
        if row["outcome"] != "FAILED" and not readback:
            item["reason"] = "read back when its closure is recorded"
        elif row["outcome"] != "FAILED":
            item["outcome"], item["reconciled"], item["evidence"], item["reason"] = _readback(
                project, row, journal, remote, moment)
        rows.append(item)
    for key in sorted(doc["admissions"]):
        admission = doc["admissions"][key]
        if admission["state"] in ("closed", "deferred") or admission["state"] == "bound" and admission["reported"]:
            continue
        rows.append({"kind": "admission", "id": key, "role": admission["role"], "state": admission["state"],
                     "dispatch": (admission["native_binding"] or {}).get("dispatchId"),
                     "outcome": "unreported" if admission["state"] == "bound" else "UNKNOWN"})
    proof.facts["preserved"] = rows[:MAX_PRESERVED]
    proof.facts["counts"] = counts(rows)
    if len(rows) > MAX_PRESERVED:
        proof.add("not_proved", "scope_too_large", f"More than {MAX_PRESERVED} preserved results cannot be recorded.")


def _readback(project: Path, row: dict, journal: dict, remote: _Remote,
              moment: datetime) -> tuple[str, bool, str | None, str | None]:
    """One unsettled row's read-only readback: (outcome, reconciled, evidence, reason)."""
    from .governor_effects import _observe
    kind = row["action"]["kind"]
    unit = journal["units"].get(row["action"]["unit"])
    if unit is None:
        return "UNKNOWN", False, None, "no delivery unit binds it, so nothing can read it back"
    if kind in ("release", "deploy"):
        return "UNKNOWN", False, None, f"a {kind} is performed by the project and has no readback"
    if kind == "merge" and not isinstance(row.get("carried_from"), dict):
        return "UNKNOWN", False, None, "a merge outcome is recorded by its coordinator; its pull request is checked"
    try:
        answer = _observe(remote.get(), row, unit, journal, project=project, now=moment)
    except PodError as exc:
        return "UNKNOWN", False, None, f"the provider did not answer ({exc.code})"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "UNKNOWN", False, None, "its readback identity cannot be read"
    if answer.get("outcome") in ("PASS", "FAILED"):
        return answer["outcome"], True, _clip(answer.get("detail")), None
    return "UNKNOWN", False, None, _clip(answer.get("hold") or "the provider did not settle it")


def counts(rows: list[dict]) -> dict:
    tally = {"reconciled": 0, "unknown": 0, "failed": 0, "unreported": 0}
    for row in rows:
        if row.get("reconciled"):
            tally["reconciled"] += 1
        if row["outcome"] == "UNKNOWN":
            tally["unknown"] += 1
        elif row["outcome"] == "FAILED":
            tally["failed"] += 1
        elif row["outcome"] == "unreported":
            tally["unreported"] += 1
    return tally


# --------------------------------------------------------------------------- a live coordinator's closure (C11)

SUPERSEDED = "superseded_by_merged_head"
MAX_PR_COMMITS = 250  # GitHub lists at most this many commits of a pull request; a full list may be truncated.


def closure_preservation(project: Path, objective: str, state: dict, journal: dict, *, remote=None,
                         now: datetime | None = None) -> dict | None:
    """Whether a delivered objective's closing write may preserve its unsettled Governor rows (C11, Amendment 3).

    Only a publication-derived validation row (workflow_dispatch derived from a publication) whose commit an
    earlier head of a merged delivery pull request carried, positively superseded by that pull request's different
    merged head, may be preserved, after a read-only readback that does not settle it. None (today's rule, with no
    provider read) when no row is unsettled, when any unsettled row is of another kind, or when finalization names
    anything but such validation readbacks before closure. Otherwise `{proof, preserved, settles, held}`: the
    delivery proof of C2.6 and C2.7 over the delivery chain, the rows to preserve UNKNOWN, the rows Pod's readback
    now settles (reconcile first), and the rows that keep holding the closure with why.
    """
    from .governor import finalization
    rows = [row for row in journal["actions"] if row["decision"] == "ALLOW" and row["outcome"] in ("pending", "UNKNOWN")]
    if not rows or any(row["action"]["kind"] != "workflow_dispatch" or not isinstance(row.get("derived_from"), str)
                       for row in rows):
        return None
    if any(name not in ("validation_readback", "closure") for name in finalization(state, journal)["remaining"]):
        return None
    proof = _Proof(objective)
    remote = remote if isinstance(remote, _Remote) else _Remote(project, remote)
    out = {"proof": None, "preserved": [], "settles": [], "held": []}
    try:
        doc = _documented(state, current=True)
        scan: dict = {}

        def records() -> tuple[list[dict], list[dict]]:
            if not scan:
                scan["found"], scan["unreadable"] = _scan(project)
            return scan["found"], scan["unreadable"]
        # The same source and pull-request proof as R145's, over the same chain (C2.6, C2.7).
        members = _members(project, objective, state, "current", doc, proof, records)
        _source(members, doc, state, proof, remote)
        _delivery(project, members, proof, remote)
    except PodError as exc:
        proof.add("unverified", "record_unreadable", f"A fact the proof needs could not be read ({exc.code}).")
    verdict = proof.result()
    out["proof"] = {"status": verdict["status"],
                    "conditions": [{"code": row["code"], "message": row["message"]} for row in verdict["conditions"]]}
    if verdict["status"] != "proved":
        return out
    from .github import repository_context
    repository = repository_context(project)["repository"]
    heads = {}
    for merged in proof.facts["delivery"]:
        try:
            read = remote.get().pr_commits(repository=repository, number=merged["pr"])
            heads[merged["pr"]] = read if not read["truncated"] else None
        except PodError:
            heads[merged["pr"]] = None
    moment = now or datetime.now(timezone.utc)
    for row in rows:
        commit = row.get("commit")
        superseded = next(((number, read["head"]) for number, read in heads.items()
                           if read is not None and commit and read["head"] != commit and commit in read["commits"]),
                          None)
        if superseded is None:
            reason = ("the merged delivery head itself" if any(read and read["head"] == commit for read in heads.values())
                      else "a merged delivery pull request's commits could not be read completely"
                      if None in heads.values() else "no merged delivery pull request carried its commit")
            out["held"].append({"record": row["record_id"], "reason": reason})
            continue
        outcome, settled, evidence, _ = _readback(project, row, journal, remote, moment)
        if settled:
            out["settles"].append({"record": row["record_id"], "outcome": outcome, "evidence": evidence})
            continue
        out["preserved"].append({"record": row["record_id"], "kind": "workflow_dispatch", "recorded": row["outcome"],
                                 "outcome": "UNKNOWN", "reason": SUPERSEDED, "pr": superseded[0],
                                 "merged_head": superseded[1]})
    return out


# --------------------------------------------------------------------------- the closure write


def _content(project: Path, objective: str, facts: dict, *, caller: str, at: str, relation: str,
             tail: dict) -> dict:
    from .bundle import running_identity
    from .github import repository_context
    identity = running_identity()
    return {"schema": SCHEMA, "objective": objective, "repository": repository_context(project)["repo_key"],
            "at": at, "statement": STATEMENT, "basis": BASIS, "caller": caller, "relation": relation,
            "tail": dict(tail), "pod": {"version": identity["version"], "bundle_digest": identity["bundle_digest"]},
            "record": dict(facts["record"]), "coordinator": dict(facts["coordinator"]),
            "runs": [dict(row) for row in facts["runs"]][:32],
            "dispatches_settled": facts["dispatches_settled"], "source": deepcopy(facts["source"]),
            "sources": deepcopy(facts["sources"])[:MAX_MEMBERS],
            "delivery": [dict(row) for row in facts["delivery"]][:MAX_PRS],
            "chain": [dict(row) for row in facts["chain"]][:MAX_MEMBERS],
            "preserved": deepcopy(facts["preserved"])[:MAX_PRESERVED], "counts": dict(facts["counts"]),
            "unfinished": list(facts["unfinished"])[:MAX_LISTED],
            "worktrees": list(facts["worktrees"])[:MAX_LISTED], "locations_complete": facts["locations_complete"]}


def _create(root: Path, content: dict) -> bool:
    """Write the closure once: a complete, synced file linked into place, so it is either whole or absent.
    Returns False when one already exists; nothing is ever rewritten or removed."""
    from .util import encoded
    data = encoded(content, compact=True)
    if len(data) > MAX_FILE:
        raise PodError("record_too_large", "The evidence closure exceeds its bound; nothing was written")
    temporary = root / f".{FILE}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, root / FILE, follow_symlinks=False)
        except FileExistsError:
            return False
    finally:
        os.unlink(temporary)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def _predecessor_content(project: Path, row: dict, tail: dict, work: dict, *, caller: str, at: str) -> dict:
    """A continued predecessor's own entry, naming the tail closure; its record stays frozen as R110 left it. The
    row was classified by R146's walk, so its record class is the one that walk read, never assumed, and `work` is
    the Run and Dispatch facts the proof read for it, never placeholders."""
    raw = row["raw"]
    try:
        _, data = _read_record(row["root"])
        journal = _journal_bytes(row["root"])
    except PodError:
        data, journal = b"", None
    from .listing import _facts
    located = _facts(raw)
    link = raw.get("continued_by") if isinstance(raw.get("continued_by"), dict) else {}
    admissions = row["doc"]["admissions"]
    preserved = [{"kind": "admission", "id": key, "role": item["role"], "state": item["state"],
                  "dispatch": (item["native_binding"] or {}).get("dispatchId"),
                  "outcome": "unreported" if item["state"] == "bound" else "UNKNOWN"}
                 for key, item in sorted(admissions.items())
                 if item["state"] not in ("closed", "deferred") and not (item["state"] == "bound" and item["reported"])]
    facts = {"record": {"class": row["kind"], "context_sha256": _sha(data),
                        "governor_sha256": _sha(journal) if journal is not None else None},
             "coordinator": {"handle": raw.get("owner") if isinstance(raw.get("owner"), str) else None,
                             "liveness": "claimed", "cause": None},
             "runs": [dict(item) for item in work["runs"]][:32], "dispatches_settled": work["dispatches_settled"],
             "source": deepcopy(tail["source"]), "sources": deepcopy(tail["sources"]),
             "delivery": deepcopy(tail["delivery"]),
             "chain": deepcopy(tail["chain"]), "preserved": preserved[:MAX_PRESERVED], "counts": counts(preserved),
             "unfinished": list(link.get("unfinished") or [])[:MAX_LISTED],
             "worktrees": located["worktrees"][:MAX_LISTED], "locations_complete": located["locations_complete"]}
    return _content(project, row["objective"], facts, caller=caller, at=at, relation="predecessor",
                    tail={"objective": tail["objective"], "at": tail["at"]})


def close_by_evidence(project: Path, objective: str, *, caller: str, port=None, remote=None,
                      now: datetime | None = None) -> dict:
    """Close one objective by evidence (C3): under its lock and its predecessors', proved again, written once.

    `closed` writes `closure-evidence.json` beside the record and one for each continued predecessor of the chain,
    naming those in `predecessors`. An existing closure writes nothing and returns `replayed`, unless an
    interruption left a predecessor the stored closure names without its entry: then the retry writes only those
    entries and returns `chain_completed`, naming them in `predecessors`. Anything else is the proof's own status
    with its conditions, and nothing is written.
    """
    from .ledger import _lock, _record_exists, objective_root
    from .util import bounded_text
    bounded_text(caller, name="caller", limit=64)
    project = Path(project)
    root = objective_root(project, objective)
    if not _record_exists(root / "context.json"):
        return {"objective": objective, "status": "not_applicable",
                "conditions": [{"code": "objective_unknown", "status": "not_applicable",
                                "message": "No record of that objective exists in this repository."}]}
    try:
        raw, _ = _read_record(root)
        chain, _ = _lineage(project, raw)
    except PodError:
        chain = []
    with ExitStack() as stack:
        for path in sorted({root / "context.json", *(row["root"] / "context.json" for row in chain)}, key=str):
            stack.enter_context(_lock(path))
        if present(root):
            return _closed_effects(project, objective, _replay(project, root, chain, caller=caller, port=port),
                                   chain, remote=remote)
        raw, _ = _read_record(root)
        if _phase(root, raw) == "closed" and (isinstance((raw.get("checkpoint") or {}).get("objective_source"), dict)
                or any(row["doc"]["issue"] is not None for row in chain)):
            return _closed_effects(project, objective, {"objective": objective, "status": "replayed",
                                   "closure": {"basis": "obligations"}}, chain, remote=remote)
        verdict = prove(project, objective, port=port, remote=remote, now=now)
        if verdict["status"] != "proved":
            return {"objective": objective, "status": verdict["status"], "conditions": verdict["conditions"]}
        certified = [row["objective"] for row in verdict["facts"]["chain"] if row["relation"] == "predecessor"]
        work = {row["objective"]: row for row in verdict["facts"].get("predecessor_runs") or []}
        if certified != [row["objective"] for row in chain] or set(work) != set(certified):
            # Every predecessor the proof certified, its Runs and work read, gets its entry under its own lock, or
            # nothing is written.
            return {"objective": objective, "status": "unverified", "conditions": [{
                "code": "chain_inconsistent", "status": "unverified",
                "message": "The succession chain changed while it was being closed; nothing was written."}]}
        at = (now or datetime.now(timezone.utc)).isoformat()
        content = _content(project, objective, verdict["facts"], caller=caller, at=at, relation="tail",
                           tail={"objective": objective, "at": at})
        if not _create(root, content):
            return _closed_effects(project, objective, _replay(project, root, chain, caller=caller, port=port),
                                   chain, remote=remote)
        written = _complete_chain(project, _missing(chain, content), content, work, caller=caller)
        return _closed_effects(project, objective, {"objective": objective, "status": "closed",
                               "closure": compact(content), "predecessors": written}, chain, remote=remote)


def _closed_effects(project: Path, objective: str, result: dict, chain: list[dict], *, remote=None) -> dict:
    if result["status"] not in ("closed", "replayed", "chain_completed"):
        return result
    from .ledger import objective_root
    result.update(close_source_issues(project, objective, remote=remote, locked=True,
                                     _roots={objective_root(project, objective), *[row["root"] for row in chain]}))
    return result


def _completed_sources(result: dict) -> dict:
    completed = {row["locator"].casefold(): row for row in result["source_issues"]
                 if row.get("locator") and row["outcome"] in ("closed", "already_completed")}
    for row in result["source_issues"]:
        # Several chain members can bind the same issue. A later member's exact completed readback settles
        # an earlier source-drift refusal as a no-op too; no proof, receipt or provider write is transferred.
        if row.get("reason") == "source_changed" and (row.get("locator") or "").casefold() in completed:
            observed = completed[row["locator"].casefold()]
            row.update(outcome="already_completed", state="CLOSED", state_reason="COMPLETED",
                       prior_condition=row.pop("reason"), observed_with=observed["objective"],
                       next_action="the bound source issue is completed")
    return result


def _missing(chain: list[dict], tail: dict) -> list[dict]:
    """The predecessors whose entry the tail closure still owes: those it names, that R146's walk classified as
    supported, without an entry yet; never one it does not name."""
    named = {row.get("objective") for row in (tail["chain"] if isinstance(tail["chain"], list) else [])
             if isinstance(row, dict) and row.get("relation") == "predecessor"}
    return [row for row in chain if row["objective"] in named and row["kind"] in SUPPORTED and not present(row["root"])]


def _complete_chain(project: Path, rows: list[dict], tail: dict, work: dict, *, caller: str) -> list[str]:
    """Write each owed predecessor entry with the Run and work facts read for it. Returns the names written."""
    written = []
    for row in rows:
        if _create(row["root"], _predecessor_content(project, row, tail, work[row["objective"]], caller=caller,
                                                     at=tail["at"])):
            written.append(row["objective"])
    return written


def _replay(project: Path, root: Path, chain: list[dict], *, caller: str, port=None) -> dict:
    """An existing closure: `replayed` with nothing written, or `chain_completed` naming the predecessor entries an
    interrupted closure left out, which this retry wrote. Existing files are never rewritten.

    Each owed entry's predecessor has its Runs and work proved fresh first, as the first closure did (R146); while
    any of them holds, nothing is written and the answer is that proof's status and conditions with the stored
    closure, a task for the project lead."""
    try:
        stored = load(root)
    except PodError as exc:
        return {"objective": root.name, "status": "replayed", "closure": {"status": "unreadable", "code": exc.code}}
    owed = _missing(chain, stored) if stored["relation"] == "tail" else []
    out = {"objective": stored["objective"], "status": "replayed", "closure": compact(stored)}
    if not owed:
        return out
    from .operations import OrcaPort
    proof = _Proof(stored["objective"])
    scan: dict = {}

    def records() -> tuple[list[dict], list[dict]]:
        if not scan:
            scan["found"], scan["unreadable"] = _scan(project)
        return scan["found"], scan["unreadable"]
    port = port or OrcaPort(project)
    try:
        work = {row["objective"]: _member_work(project, row, proof, records, port=port) for row in owed}
    except PodError as exc:
        proof.add("unverified", "record_unreadable", f"A fact the proof needs could not be read ({exc.code}).")
    verdict = proof.result()
    if verdict["status"] != "proved":
        return {**out, "status": verdict["status"], "conditions": verdict["conditions"]}
    completed = _complete_chain(project, owed, stored, work, caller=caller)
    if completed:
        out.update(status="chain_completed", predecessors=completed)
    return out


# --------------------------------------------------------------------------- status projection


def summary(value: dict | None) -> str:
    if not isinstance(value, dict) or value.get("status") == "unreadable":
        return "Closed by evidence, coordinator gone: its closure record is unreadable; its record accepts no writes."
    proof = (value.get("source") or {}).get("proof")
    merged = len(value.get("delivery") or [])
    named = {"pod_ticks": "every PoD ticked", "issue_completed": "the issue closed as completed",
             "recorded_criteria": "its recorded criteria"}.get(proof, "the recorded proof")
    held = value.get("counts") or {}
    kept = sum(held.get(key, 0) for key in ("unknown", "failed", "unreported"))
    text = (f"Closed by evidence, coordinator gone: delivered by {named} and {merged} merged pull request(s); Pod "
            "needs nothing more")
    if kept:
        text += f"; {kept} unknown, failed or unreported result(s) stay as recorded"
    return text + "."


def readiness_block(root: Path, raw: dict, value: dict | None) -> dict:
    from .readiness import CONTRACT
    at = value.get("at") if isinstance(value, dict) else None
    try:
        doc = _documented(raw, current=False)
    except PodError:
        doc = {"owner": raw.get("owner") if isinstance(raw.get("owner"), str) else None, "runs": []}
    return {"contract": CONTRACT, "state": "closed", "acts_next": "none", "coordinator_needed": False,
            "coordinator": {"handle": doc["owner"], "run": doc["runs"][0] if len(doc["runs"]) == 1 else None,
                            "runs": doc["runs"][:32], "generation": None, "runtime": None, "liveness": "not_read",
                            "code": None},
            "finalization": {"schema": "pod-finalization/v1", "remaining": [], "records": {}, "finished": True,
                             "next_action": "Finished: closed by evidence, coordinator gone; Pod needs nothing more."},
            "reason": {"basis": BASIS, "closed_at": at}, "summary": summary(value)}


def status_fields(project: Path, objective: str, root: Path) -> dict:
    """What status shows for an objective closed by evidence (C4), for any reader and any record schema: read-only,
    from the closure record and the record's documented fields. The record itself is never converted."""
    value = block(root)
    try:
        raw, _ = _read_record(root)
    except PodError as exc:
        raw = {}
        failure = exc.code
    else:
        failure = None
    checkpoint = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else {}
    try:
        doc = _documented(raw, current=_record_class(raw) == "current") if raw else None
    except PodError:
        doc = None
    settled = []
    for key, row in sorted(((doc or {}).get("admissions") or {}).items()):
        binding = row["native_binding"] or {}
        item = {"admission": key, "role": row["role"], "dispatch": binding.get("dispatchId"), "state": row["state"]}
        if row["state"] not in ("closed", "deferred"):
            item.update(settlement=BASIS, closed_at=(value or {}).get("at"))
        settled.append(item)
    out = {"status": "observed", "phase": "closed", "closure_basis": BASIS, "blocker": "objective_closed",
           "objective": objective, "evidence_closure": value,
           "source": checkpoint.get("objective_source", "direct_objective") if checkpoint else None,
           "selected_worktree": checkpoint.get("worktree"),
           "checkpoint_join": {"native_refs": (doc or {}).get("native_refs", []), "assignments": [],
                               "plan_revision": checkpoint.get("plan_revision"),
                               "candidate": checkpoint.get("candidate")},
           "assignments": {"active": [], "unverified": [], "settled": settled[:MAX_LISTED],
                           "preserved": list((value or {}).get("preserved") or [])},
           "retained_terminals": [], "worker_tabs": [], "native": None,
           "readiness": readiness_block(root, raw, value),
           "next_safe_action": "objective closed by evidence, coordinator gone; its record accepts no writes",
           "next_action": "objective closed by evidence, coordinator gone; nothing remains to record"}
    if failure is not None:
        out["record_error"] = failure
    return out


def older_predecessor_fields(project: Path, objective: str, root: Path) -> dict | None:
    """What status shows for an older continued predecessor whose chain tail closed by evidence (R146): read-only,
    through its own entry, R146's documented fields and its succession link, as a current predecessor reads:
    phase `continued`, its evidence entry and the settled chain. The record is never validated as current or
    converted. None unless the record is older and its entry is a readable predecessor entry: the ordinary reader
    then answers, and still refuses an older record without closure (R90)."""
    if not present(root):
        return None
    try:
        value = load(root)
        raw, _ = _read_record(root)
    except PodError:
        return None
    if value["relation"] != "predecessor" or _record_class(raw) != "older":
        return None
    from .readiness import CONTRACT
    from .succession import ACTION, chain_state
    from .term import clean
    out = status_fields(project, objective, root)
    link = raw.get("continued_by") if isinstance(raw.get("continued_by"), dict) else {}
    successor = link.get("objective") if _text(link.get("objective"), 1024) else None
    chain = (chain_state(project, raw) if successor is not None
             else {"state": "unknown", "objective": None, "reason": "its successor link is unreadable"})
    claimed_at = _clip(link.get("at"), 64)
    for item in out["assignments"]["settled"]:
        # The claim settled what its predecessor left open, as a current predecessor reads (R110).
        if item.pop("settlement", None) is not None:
            item.pop("closed_at", None)
            item.update(settlement="claim" if item["state"] == "bound" else "claim_no_dispatch", claimed_at=claimed_at)
    succession = {"continued_by": successor, "at": claimed_at, "state": chain["state"],
                  "chain_end": chain.get("objective")}
    if chain.get("reason"):
        succession["reason"] = chain["reason"]
    action = ACTION.format(successor) if successor is not None else (
        "Read it with pod status --list; its successor link is unreadable and this record accepts no writes.")
    named = clean(successor)[:64] if successor is not None else "(unreadable)"
    readiness = {"contract": CONTRACT, "state": "continued", "acts_next": "successor", "coordinator_needed": False,
                 "coordinator": out["readiness"]["coordinator"], "finalization": None,
                 "reason": {"successor": successor, "succession_state": chain["state"]},
                 "summary": f"Continued by successor objective {named} ({chain['state'].replace('_', ' ')}); read "
                            "that objective, as this record accepts no writes."}
    out.pop("closure_basis")  # Its tail closed by evidence; this record itself reads continued.
    out.update(phase="continued", blocker="objective_continued", record_class="older", succession=succession,
               readiness=readiness, next_safe_action=action, next_action=action)
    return out


def closed_older_state(project: Path, objective: str) -> dict | None:
    """The cleanup plan's read of an older record closed by evidence (R94, R146), before the validating reader.

    Only R146's documented fields are read, through this module's bounded raw read; the record is never validated
    as current, converted or written. Fields the plan uses that older records do not document (the governance
    target, a verified delivery, recovery placements, terminal bindings) read as absent, so every resource whose
    safety depends on them stays protected. A continuation link of any shape is kept, so a successor's locations
    stay protected. None when no evidence closure is recorded or the record is not an older one: the ordinary reader
    then answers, and still refuses an older open record."""
    from .ledger import objective_root
    root = objective_root(project, objective)
    if not present(root):
        return None
    try:
        raw, _ = _read_record(root)
    except PodError:
        return None
    if _record_class(raw) != "older":
        return None
    try:
        doc = _documented(raw, current=False)
    except PodError as exc:
        raise PodError("cleanup_unavailable", "A documented field of this older closed record is unreadable",
                       {"next_action": "read it with pod status --objective NAME; nothing was planned"}) from exc
    bound = raw["checkpoint"].get("worktree")
    worktree = None
    if isinstance(bound, dict) and isinstance(bound.get("path"), str):
        worktree = {"path": bound["path"]}
        if isinstance(bound.get("branch"), str):
            worktree["branch"] = bound["branch"]
    state = {"checkpoint": {"objective": objective, "worktree": worktree, "governance": {}, "delivery": None,
                            "closure": None},
             "admissions": {key: {"state": row["state"], "run_id": row["run_id"],
                                  "native_binding": row["native_binding"]} for key, row in doc["admissions"].items()}}
    if "continued_by" in raw:
        link = raw["continued_by"]
        state["continued_by"] = {"objective": link["objective"] if isinstance(link, dict) and _text(
            link.get("objective"), 1024) else "(an unreadable successor)"}
    return state


def render(result: dict) -> None:
    from .term import clean
    value = result.get("evidence_closure") or {}
    print(clean(result["readiness"]["summary"]))
    print(f"Pod status: {result['status']}; objective {clean(str(result.get('objective')))}")
    if value.get("status") == "recorded":
        tally = value["counts"]
        print(f"Closed by evidence at {clean(value['at'])} ({clean(value['caller'])}): "
              f"{clean(value['statement'])}; preserved {tally['reconciled']} reconciled, {tally['unknown']} unknown, "
              f"{tally['failed']} failed and {tally['unreported']} unreported result(s)")
        for row in value["delivery"]:
            print(f"  Delivery pull request #{row['pr']} merged ({clean(str(row.get('merge_commit') or 'unknown'))[:12]})")
        if value["relation"] == "predecessor":
            print(f"  Closed with its chain tail {clean(value['tail']['objective'])}")
        for path in value["worktrees"]:
            print(f"  Recorded worktree: {clean(path)}")
    print(f"Blocker: {result['blocker']}; next: {result['next_safe_action']}")


# --------------------------------------------------------------------------- sweep (R148)


def _source_closure(project: Path, objective: str, root: Path, raw: dict, before: bytes) -> bool:
    """Validate closure before source effects; an older ordinary closure permits observation alone."""
    kind = _record_class(raw)
    if kind not in SUPPORTED or raw.get("historical_retirement"):
        raise PodError("objective_not_closed", "Only an actual supported Pod closure permits this effect")
    if present(root):
        stored = load(root)
        from .github import repository_context
        if (stored["objective"] != objective or stored["repository"] != repository_context(project)["repo_key"]
                or not isinstance(stored["record"], dict) or stored["record"].get("context_sha256") != _sha(before)):
            raise PodError("objective_not_closed", "The evidence closure names another objective")
        if stored["relation"] == "predecessor":
            from .ledger import objective_root
            tail = load(objective_root(project, stored["tail"]["objective"]))
            if tail["relation"] != "tail" or not any(row.get("objective") == objective
                    and row.get("relation") == "predecessor" for row in tail["chain"]):
                raise PodError("objective_not_closed", "The evidence closure's certified tail is unavailable")
        return True
    if raw.get("continued_by") or not isinstance((raw.get("checkpoint") or {}).get("closure"), dict):
        raise PodError("objective_not_closed", "Only an actual ordinary or evidence closure permits this effect")
    if kind == "older":
        return False
    from .ledger import _read, map_of, validate_checkpoint_snapshot, closed_snapshot_context
    from .obligations import validate_closed_snapshot
    state = _read(root / "context.json")
    mapped = map_of(state)
    validate_checkpoint_snapshot(project, state)
    if not (mapped or {}).get("closure") or validate_closed_snapshot(mapped, closed_snapshot_context(project, objective, state)):
        raise PodError("closure_unverified", "A required recorded closure fact is unavailable")
    return True


def _source_failure(objective: str, reason: str) -> dict:
    outcome = "refused" if reason in ("objective_unknown", "objective_not_closed", "closure_changed", "chain_changed") else "unverified"
    return {"source_issues": [{"objective": objective, "locator": None, "outcome": outcome, "reason": reason,
            "next_action": "the project lead resolves the named closure or chain condition before source completion"}],
            "parents_open": []}


def close_source_issues(project: Path, objective: str, *, remote=None, locked: bool = False,
                       _roots: set[Path] | None = None) -> dict:
    """The shared closed-chain effect. Every writable issue is retained by a reciprocal chain member.

    Writers release their write lock first; this effect acquires every chain lock in path order and validates the
    accepted closure afresh. Evidence closure already holds those locks. Provider outcomes never undo the accepted
    Pod write, and context, history and Governor bytes stay identical.
    """
    from .ledger import _lock, _record_exists, objective_root
    root = objective_root(project, objective)
    if not _record_exists(root / "context.json"):
        return _source_failure(objective, "objective_unknown")
    try:
        raw, before = _read_record(root)
        chain, problem = _lineage(project, raw)
    except PodError as exc:
        return _source_failure(objective, exc.code)
    roots = {root, *[row["root"] for row in chain]}
    if not locked:
        with ExitStack() as stack:
            for path in sorted(roots, key=str):
                stack.enter_context(_lock(path / "context.json"))
            return close_source_issues(project, objective, remote=remote, locked=True, _roots=roots)
    if _roots != roots:
        return _source_failure(objective, "chain_changed")
    if problem is not None and present(root):
        return _source_failure(objective, problem[1])
    try:
        writable = _source_closure(project, objective, root, raw, before)
        closed_at = (load(root)["tail"].get("at") if present(root)
                     else (raw.get("checkpoint") or {}).get("closure", {}).get("at"))
        members = [(objective, root, raw, before)]
        # An ordinary tail's own closure authorizes its own source independently of a broken claimed chain.
        # A partial lineage never authorizes effects on any predecessor.
        for row in chain if problem is None else []:
            member, data = _read_record(row["root"])
            if member != row["raw"]:
                raise PodError("closure_changed", "A chain member changed during closure validation")
            members.append((row["objective"], row["root"], member, data))
    except PodError as exc:
        return _source_failure(objective, exc.code)
    snapshots = [(path, data) for _, path, _, data in members]
    out = {"source_issues": [], "parents_open": []}
    for name, path, record, data in members:
        effect = _close_source_issue(project, name, record, remote=remote, writable=writable, snapshots=snapshots,
                                     closed_at=closed_at)
        for key in out:
            out[key].extend(effect[key])
    if problem is not None:
        out["source_issues"].extend(_source_failure(objective, problem[1])["source_issues"])
    return _completed_sources(out)


def _reopened_after_closure(reader, source: dict, closed_at: str | None, effect: dict) -> bool:
    from .util import normalize_timestamp
    try:
        reopened_at = normalize_timestamp(reader.issue_reopened_at(
            repository=source["repository"], number=source["number"]),
            field="Issue reopen time", code="source_reopen_unverified")
        closure_at = normalize_timestamp(closed_at, field="Pod closure time", code="source_reopen_unverified")
    except (PodError, AttributeError) as exc:
        raise PodError("source_reopen_unverified", "The latest reopen and Pod closure times must be readable") from exc
    effect.update(reopened_at=reopened_at, pod_closed_at=closure_at)
    return datetime.fromisoformat(reopened_at.replace("Z", "+00:00")) > datetime.fromisoformat(
        closure_at.replace("Z", "+00:00"))


def _close_source_issue(project: Path, objective: str, raw: dict, *, remote, writable: bool,
                        snapshots: list[tuple[Path, bytes]], closed_at: str | None) -> dict:
    out = {"source_issues": [], "parents_open": []}
    effect = {"objective": objective, "locator": None, "outcome": "unverified"}
    try:
        source = (raw.get("checkpoint") or {}).get("objective_source")
        if source in (None, "direct_objective"):
            return out
        from .github import GhPort, validate_issue_binding, issue_recheck_live, pod_lines
        source = validate_issue_binding(source)
        effect["locator"] = source["locator"]
        reader = remote or GhPort(project)
        # Cached observations can rule out a send; only this effect's own fresh source check permits one.
        checks = (reader, reader._source()) if isinstance(reader, _SweepRemote) else (reader,)
        for check_reader in checks:
            checked, live = issue_recheck_live(project, source, port=check_reader)
            if live is None:
                raise PodError("source_unreadable", "The bound source observation is unavailable")
            # No-op for an already completed source; never change another closing reason.
            if live["state"] == "CLOSED":
                effect.update(outcome="already_completed" if checked.get("state_reason") == "COMPLETED" else "closed_other_reason",
                              state=live["state"], state_reason=checked.get("state_reason"))
                break
            if checked.get("state_reason") == "REOPENED" and _reopened_after_closure(check_reader, source, closed_at, effect):
                effect.update(outcome="reopened", reason="source_reopened", state="OPEN", state_reason="REOPENED")
                break
            if not writable:
                raise PodError("closure_unverified", "An older ordinary closure permits source observation, not a new mutation")
            from .pod_mapping import scope_hash
            from .util import digest
            retained = (raw.get("pod_ticks") or {}).get("facts") or {}
            checkbox_only = (checked.get("body_changed") and not checked.get("amendment_changed")
                             and retained.get("source") == digest(source)
                             and retained.get("mapping_scope_sha256") == scope_hash(live["body"], seal=source.get("role") == "SEAL"))
            if checked["status"] != "current" and not checkbox_only:
                raise PodError("source_changed", "The project lead must reconcile the bound source body and amendments")
            lines = pod_lines(live["body"], seal=source.get("role") == "SEAL")
            if not lines or any(len(rows) != 1 or not rows[0]["ticked"] for rows in lines.values()):
                raise PodError("source_unproved", "The bound source needs a nonempty unambiguous fully ticked PoD set")
        else:
            # The cooperative lock is the local fence; this second read also rejects an uncooperative raw edit.
            for path, before in snapshots:
                _, immediate = _read_record(path)
                if immediate != before:
                    raise PodError("closure_changed", "A closed-chain record changed during source readback; no issue write was sent")
            try:
                response = reader.issue_close(source=source)
            except PodError as exc:
                response = {"result": "unconfirmed", "reason": exc.code}
            effect["write"] = response
            try:
                readback, observed = issue_recheck_live(project, source, port=reader)
                effect.update(state=readback["state"], state_reason=readback.get("state_reason"))
                completed = readback["state"] == "CLOSED" and readback.get("state_reason") == "COMPLETED"
                effect["outcome"] = ("refused" if response["result"] == "refused" else "closed" if completed else "unknown")
                if readback.get("body_changed") or readback.get("amendment_changed"):
                    effect["source_changed"] = True
                if observed is not None:
                    effect["body_sha256"] = _sha(observed["body"].encode())
            except PodError as exc:
                effect.update(outcome="refused" if response["result"] == "refused" else "unknown", reason=exc.code)
        _source_parents(live["body"], source, reader, out["parents_open"])
    except (PodError, AttributeError) as exc:
        effect.update(outcome="refused" if getattr(exc, "code", None) in
                      ("objective_not_closed", "closure_changed", "source_changed", "source_unproved") else "unverified",
                      reason=getattr(exc, "code", "source_effect_unavailable"))
    effect["next_action"] = ("the bound source issue is completed" if effect["outcome"] in ("closed", "already_completed")
                             else "the project lead reconciles the source issue's explicit reopen after Pod's closure; Pod will not re-close it automatically"
                             if effect["outcome"] == "reopened"
                             else "the project lead checks the reopen timeline and recorded Pod closure evidence, then reconciles source completion through the project's supported process; Pod leaves the source unchanged while ordering is unverified"
                             if effect.get("reason") == "source_reopen_unverified"
                             else "the project lead resolves the named source condition, then runs pod sweep; no owner completion decision is needed")
    for parent in out["parents_open"]:
        parent["objective"] = objective
    out["source_issues"].append(effect)
    return out


def _source_parents(body: str, source: dict, remote, output: list) -> None:
    """References never authorize a parent mutation; its completion remains a named lead task."""
    import re
    from .github import parse_issue_locator, _document_marker, SEAL_TITLE, SEAL_FORMAT, EEL_TITLE, EEL_FORMAT
    links = list(dict.fromkeys(re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*", body)))
    if len(links) > MAX_PRS:
        output.append({"locator": source["locator"], "role": "references_unavailable", "party": "lead",
                       "code": "scope_too_large", "remaining": len(links) - MAX_PRS,
                       "next_action": "the project lead evaluates the remaining source references beyond the bounded parent read"})
    for link in links[:MAX_PRS]:
        if link.casefold() == source["locator"].casefold():
            continue
        issue = parse_issue_locator(link)
        cache = getattr(remote, "parent_tasks", None)
        key = link.casefold()
        if cache is not None and key in cache:
            if cache[key] is not None:
                output.append(deepcopy(cache[key]))
            continue
        facts = {"repository": issue["repository"], "number": issue["number"]}
        try:
            row = remote.issue(repository=issue["repository"], number=issue["number"])
            role = ("SEAL" if _document_marker(row.get("title"), row.get("body"), SEAL_TITLE, SEAL_FORMAT) else
                    "EEL" if _document_marker(row.get("title"), row.get("body"), EEL_TITLE, EEL_FORMAT) else None)
            if role is None or row.get("state") == "CLOSED" and row.get("stateReason") == "COMPLETED":
                if cache is not None:
                    cache[key] = None
                continue
            facts.update(read="observed", state=row.get("state"), state_reason=row.get("stateReason"),
                         body_sha256=_sha(row["body"].encode()), updated_at=row.get("updatedAt"))
        except (PodError, KeyError, AttributeError) as exc:
            role = "unavailable"
            facts.update(read="unavailable", code=getattr(exc, "code", "source_unreadable"))
        rule = {"EEL": "closes when its consuming PES work is delivered",
                "SEAL": "closes when every requirement's delivering PES is closed"}.get(role, "source completion rule unavailable")
        task = {"locator": link, "role": role, "party": "lead", "completion_rule": rule, "facts": facts,
                       "reason": "completion_rule_unavailable",
                       "next_action": "the project lead verifies this rule against all consumers or requirements: "
                                      + rule + "; the child objective alone cannot evaluate that rule"}
        if cache is not None:
            cache[key] = deepcopy(task)
        output.append(task)


class _SweepRemote:
    """One sweep's observations only; open source reads before mutations are always fresh.

    Full issue bodies have a small cache bound. Parent tasks retain only compact facts, bounded by the sweep's
    record and per-source reference limits. A mutation invalidates every observation of its issue.
    """

    def __init__(self, project: Path, remote, observations: dict):
        self.project, self.source_reader = project, remote
        self.remote = _Remote(project, remote)
        self.issues, self.states, self.parent_tasks = (observations[key] for key in ("issues", "states", "parents"))
        self.reopens = observations["reopens"]

    def _source(self):
        if self.source_reader is None:
            from .github import GhPort
            self.source_reader = GhPort(self.project)
        return self.source_reader

    def __getattr__(self, name):
        return getattr(self._source() if name == "amendment" else self.remote.get(), name)

    def _read(self, method: str, arguments: dict, cache: dict, *, closed_only: bool = False):
        key = (arguments["repository"].casefold(), arguments["number"])
        if key in cache:
            value = cache[key]
            if isinstance(value, PodError):
                raise value
            return deepcopy(value)
        try:
            reader = self._source() if method in ("issue", "issue_reopened_at") else self.remote.get()
            value = getattr(reader, method)(**arguments)
        except PodError as exc:
            if len(cache) < MAX_LISTED:
                cache[key] = exc
            raise
        if len(cache) < MAX_LISTED and (not closed_only or value.get("state") == "CLOSED"
                                      or value.get("stateReason") == "REOPENED"):
            cache[key] = deepcopy(value)
        return value

    def issue(self, **arguments):
        return self._read("issue", arguments, self.issues, closed_only=True)

    def issue_state(self, **arguments):
        return self._read("issue_state", arguments, self.states)

    def issue_reopened_at(self, **arguments):
        return self._read("issue_reopened_at", arguments, self.reopens)

    def issue_close(self, *, source):
        key = (source["repository"].casefold(), source["number"])
        self.issues.pop(key, None)
        self.states.pop(key, None)
        self.reopens.pop(key, None)
        self.parent_tasks.pop(source["locator"].casefold(), None)
        return self._source().issue_close(source=source)


def _effect_tasks(row: dict) -> bool:
    return any(effect.get("outcome") not in ("closed", "already_completed") and not effect.get("task_with")
               for effect in row.get("source_issues", [])) or bool(row.get("parents_open"))


def _deduplicate_tasks(rows: list[dict]) -> None:
    """Keep every source outcome, with one actionable task per issue locator in this sweep."""
    tasks = {}
    for row in rows:
        for effect in row.get("source_issues", []):
            if effect.get("outcome") in ("closed", "already_completed") or not effect.get("locator"):
                continue
            key = effect["locator"].casefold()
            if key in tasks:
                effect["task_with"] = tasks[key]["objective"]
            else:
                tasks[key] = effect
    for row in rows:
        parents = []
        for parent in row.get("parents_open", []):
            key = parent["locator"].casefold()
            if key in tasks:
                tasks[key].setdefault("also_referenced_by", []).append(row["objective"])
            else:
                tasks[key] = parent
                parents.append(parent)
        if "parents_open" in row:
            row["parents_open"] = parents
        row["party"] = "lead" if row["outcome"] in ("not_proved", "unverified") or _effect_tasks(row) else None


def _outcome(objective: str, verdict: dict) -> dict:
    """One objective's sweep row. `already_closed` means its own closure existed and nothing was written for it; a
    predecessor entry the sweep wrote is reported as that predecessor's own `closed` row (see `sweep`)."""
    status = verdict["status"]
    outcome = {"closed": "closed", "replayed": "already_closed", "chain_completed": "already_closed",
               "not_proved": "not_proved", "unverified": "unverified"}.get(status, "skipped")
    effects = {key: [row for row in verdict[key] if row.get("objective") == objective]
               for key in ("source_issues", "parents_open") if key in verdict}
    effects_held = _effect_tasks(effects)
    return {"objective": objective, "outcome": outcome,
            "conditions": [{"code": item["code"], "message": item["message"]}
                           for item in verdict.get("conditions", [])][:MAX_LISTED],
            "party": "lead" if outcome in ("not_proved", "unverified") or effects_held else None,
            **effects}


def _unavailable(target: Path, reason: str) -> dict:
    return {"path": str(target), "repository": None, "repo_key": None, "status": "unavailable", "reason": reason,
            "objectives": []}


def _sweep_scope(target: Path, names: list[str]) -> tuple[list[dict], list[str], list[dict]]:
    """The records one sweep visits: each named objective resolved directly from its own record, or else every
    record of the repository through the sweep's own bounded scan (never a presentation list). A named continued
    predecessor also resolves its already-closed reciprocal tail for source retries. Returns the rows
    (`objective`, `phase`), the named objectives that have no record, and what kept the scan from reading every
    record (empty when it read them all)."""
    from .ledger import _record_exists, objective_root
    if names:
        rows, missing = [], []
        for name in names:
            root = objective_root(target, name)
            if not _record_exists(root / "context.json"):
                missing.append(name)
                continue
            try:
                raw, _ = _read_record(root)
                phase = _phase(root, raw)
            except PodError:
                phase = "open"  # Its closure reports why it cannot be read.
            rows.append({"objective": name, "phase": phase})
            if phase == "continued" and _record_class(raw) in SUPPORTED:
                from .succession import chain_state
                successor = chain_state(target, raw)
                tail = successor.get("objective")
                if successor["state"] == "successor_closed" and tail is not None:
                    rows.append({"objective": tail, "phase": "closed"})
        return rows, missing, []
    found, problems = _scan(target, orphans=True)
    return [{"objective": row["objective"], "phase": _phase(row["root"], row["raw"])} for row in found], [], problems


def _scope_reason(problems: list[dict]) -> str:
    """Plain words for what kept a sweep from reading every record of a repository."""
    from .listing import MAX_SCAN
    beyond = sum(row.get("count", 0) for row in problems if row["code"] == BEYOND_BOUND)
    orphaned = [row["record"] for row in problems if row["code"] == UNATTRIBUTED]
    unread = [row for row in problems if row["code"] not in (BEYOND_BOUND, UNATTRIBUTED)]
    parts = []
    if any(row["code"] == ROOT_UNREADABLE for row in problems):
        return "the Pod state root cannot be enumerated; nothing can be proven absent"
    if beyond:
        parts.append(f"{beyond} record directory(ies) lie beyond the sweep's {MAX_SCAN}-directory scan bound")
    if unread:
        parts.append(f"{len(unread)} record(s) cannot be read ("
                     + ", ".join(f"{row['record'][:16]}: {row['code']}" for row in unread[:4])
                     + (", ..." if len(unread) > 4 else "") + ")")
    if orphaned:
        parts.append(f"{len(orphaned)} director(ies) hold Pod files without a record naming them ("
                     + ", ".join(name[:16] for name in orphaned[:4]) + (", ..." if len(orphaned) > 4 else "") + ")")
    return ("the sweep could not read every record of this repository: " + "; ".join(parts)
            + "; an objective among them may still be open")[:MAX_TEXT]


def sweep(project: Path, *, repositories: list[str] | None = None, objectives: list[str] | None = None,
          port=None, remote=None) -> dict:
    """Close every provably delivered open objective of each repository (C6), idempotently.

    Needs no Orca terminal, Run binding or live coordinator; starts nothing and mutates no Orca or Git state.
    It writes only `closure-evidence.json` Pod files, then retries the separate bound-source effect. Each
    repository's records are enumerated by the sweep's own bounded scan; named objectives are resolved directly. A
    repository it cannot read is `unavailable`; one whose records it could not all read is `incomplete`, with the
    reason, and the sweep never claims its objectives all closed. The sweep still ran.
    """
    from .github import repository_context
    from .ledger import objective_root
    targets = [Path(path).expanduser() for path in (repositories or [str(project)])]
    names = list(dict.fromkeys(objectives or []))
    if len(targets) > MAX_LISTED or len(names) > MAX_LISTED:
        raise PodError("sweep_scope_unavailable", f"A sweep names at most {MAX_LISTED} repositories and objectives")
    out = {"schema": "pod-cli/v5", "status": "swept", "repositories": [],
           "counts": {name: 0 for name in RESULTS}, "next_safe_action": None}
    observations = {key: {} for key in ("issues", "states", "parents", "reopens")}
    for target in targets:
        if not target.is_dir():
            out["repositories"].append(_unavailable(target, "the path is not a directory"))
            continue
        target = target.resolve()
        try:
            context = repository_context(target)
            if context["repo_key"] is None:
                out["repositories"].append(_unavailable(target, "the path is not a Git repository"))
                continue
            rows, missing, problems = _sweep_scope(target, names)
        except PodError as exc:
            out["repositories"].append(_unavailable(target, f"the repository could not be read ({exc.code})"))
            continue
        if any(row["code"] == ROOT_UNREADABLE for row in problems):
            out["repositories"].append(_unavailable(target, _scope_reason(problems)))
            continue
        # Successor tails first: closing a tail writes its continued predecessors' entries too.
        rows = sorted(rows, key=lambda row: (row["phase"] == "continued", row["objective"]))
        reader = _SweepRemote(target, remote, observations)
        done, results = set(), []
        for row in rows:
            if row["objective"] in done:
                continue
            done.add(row["objective"])
            verdict = close_by_evidence(target, row["objective"], caller="sweep", port=port, remote=reader)
            results.append(_outcome(row["objective"], verdict))
            # A predecessor entry this sweep wrote, with its tail or completing an interrupted closure, closed that
            # predecessor now: it is reported as its own `closed` row, never as an earlier closure replayed.
            for name in verdict.get("predecessors") or []:
                if name not in done:
                    done.add(name)
                    results.append(_outcome(name, {**verdict, "status": "closed"}))
            # An ordinary closed tail can have no binding itself. Keep each continued predecessor's source
            # outcome visible even when the caller selected only the tail, and never repeat its effect this sweep.
            for effect in verdict.get("source_issues", []):
                name = effect["objective"]
                if name not in done:
                    done.add(name)
                    results.append(_outcome(name, {**verdict, "status": "replayed" if present(
                        objective_root(target, name))
                        else "not_applicable"}))
        for name in missing:
            results.append(_outcome(name, {"status": "not_applicable", "conditions": [
                {"code": "objective_unknown", "message": "No record of that objective exists in this repository."}]}))
        for row in results:
            out["counts"][row["outcome"]] += 1
        out["repositories"].append({"path": str(target), "repository": context["repository"],
                                    "repo_key": context["repo_key"], "status": "incomplete" if problems else "swept",
                                    "reason": _scope_reason(problems) if problems else None,
                                    "objectives": results})
    _deduplicate_tasks([row for repository in out["repositories"] for row in repository["objectives"]])
    tally = out["counts"]
    statuses = {row["status"] for row in out["repositories"]}
    lead = tally["not_proved"] + tally["unverified"]
    if lead:
        text = (f"{lead} objective(s) are tasks for the project lead: resolve each named condition, then sweep "
                "again; no owner decision is needed")
    elif statuses <= {"swept"}:
        text = "every provably delivered objective is closed; nothing else needs the sweep"
    else:
        text = "every provably delivered objective the sweep could read is closed"
    if "unavailable" in statuses:
        text += "; an unavailable repository was not swept"
    if "incomplete" in statuses:
        text += ("; a repository's records could not all be read, so an objective there may still be open: "
                 "restore them as its reason names, then sweep again")
    effects_held = sum(_effect_tasks(row)
                       for repository in out["repositories"] for row in repository["objectives"])
    if effects_held:
        text = text.removesuffix("; nothing else needs the sweep")
        text += (f"; {effects_held} objective(s) still have source-issue or parent tasks for the project lead: "
                 "resolve the reported outcome and sweep again")
    out["next_safe_action"] = text
    return out


def render_sweep(result: dict) -> None:
    from .term import clean
    tally = result["counts"]
    print("Pod sweep: " + ", ".join(f"{tally[name]} {name.replace('_', ' ')}" for name in RESULTS))
    for repository in result["repositories"]:
        print(f"Repository {clean(repository['path'])}: {repository['status']}, "
              f"{len(repository['objectives'])} objective(s)"
              + (f"; {clean(repository['reason'])}" if repository["reason"] else ""))
        for row in repository["objectives"]:
            reason = "; ".join(f"{item['code']}: {clean(item['message'])}" for item in row["conditions"][:3])
            print(f"  {clean(row['objective'])[:64]}: {row['outcome'].replace('_', ' ')}"
                  + (f" ({reason})" if reason else "") + (" - a task for the project lead" if row["party"] else ""))
            for effect in row.get("source_issues", []):
                print(f"    Source {clean(str(effect.get('locator') or 'unavailable'))}: {clean(effect['outcome'])}"
                      + (f" ({clean(effect['reason'])})" if effect.get("reason") else ""))
            for parent in row.get("parents_open", []):
                print(f"    Parent {clean(parent['locator'])}: a task for the project lead")
    print(f"Next: {result['next_safe_action']}")

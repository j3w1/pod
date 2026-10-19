"""Closure by evidence: delivered objectives always close, whatever happened to their coordinator (R140-R144).

One read-only delivery proof, one immutable end-of-life record written beside the objective's record (never inside
it), and one sweep that applies it to every open objective of a repository. The proof reuses R110's loss proof and
R112's Run, Dispatch and start-request checks unchanged. Nothing here asks the owner, starts work or mutates Orca,
Git or a provider: `context.json`, `governor.json` and Pod history stay byte-identical, and a pending, UNKNOWN or
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
           "record", "coordinator", "runs", "dispatches_settled", "source", "delivery", "chain", "preserved",
           "counts", "unfinished", "worktrees", "locations_complete"}


def _clip(value: object, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _text(value: object, limit: int = 1024) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- the stored record


def present(root: Path) -> bool:
    """Whether an evidence closure is recorded beside this objective's record; an unreadable entry counts."""
    try:
        os.lstat(root / FILE)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # Never read an entry Pod cannot inspect as absent.
    return True


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
            or not isinstance(value["worktrees"], list) or type(value["locations_complete"]) is not bool):
        raise PodError("closure_evidence_unreadable", "The evidence closure is malformed")
    return value


def refuse_closed(root: Path) -> None:
    """Every write of an objective closed by evidence refuses, its own coordinator's included (R141, C4)."""
    if not present(root):
        return
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
    return {"status": "recorded", **{key: deepcopy(value[key]) for key in sorted(_FIELDS - {"schema"})}}


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
    source = checkpoint.get("objective_source")
    if source in (None, "direct_objective"):
        issue = None
    elif isinstance(source, dict):
        from .github import parse_issue_locator
        try:
            issue = parse_issue_locator(source.get("locator"))
        except PodError as exc:
            raise bad("objective source") from exc
    else:
        raise bad("objective source")
    worktree = checkpoint.get("worktree")
    return {"owner": owner, "objective": checkpoint.get("objective"), "runs": sorted(set(runs)),
            "admissions": rows, "issue": issue,
            "worktree": worktree.get("path") if isinstance(worktree, dict) and isinstance(worktree.get("path"), str)
            else None,
            "native_refs": [dict(ref) for ref in refs][:MAX_LISTED]}


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
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PodError("journal_unreadable", "The Governor journal is not a regular file")
            from .governor import MAX_JOURNAL
            data = file.read(MAX_JOURNAL + 1)
    except OSError as exc:
        raise PodError("journal_unreadable", "The Governor journal cannot be read") from exc
    from .governor import MAX_JOURNAL
    if len(data) > MAX_JOURNAL:
        raise PodError("journal_unreadable", "The Governor journal exceeds its bound")
    return data


def _scan(project: Path) -> tuple[list[dict], list[str]]:
    """Every readable record of this repository, any schema, and the record directories that could not be read."""
    from .github import repository_context
    from .ledger import _record_exists, record_name, state_root
    from .listing import MAX_SCAN, _name
    context = repository_context(project)
    root = state_root(project)
    try:
        if root.is_symlink():
            return [], [str(root)]
        with os.scandir(root) as frontier:
            entries = sorted((entry for entry in frontier if not entry.name.startswith(".")),
                             key=lambda entry: entry.name)
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [str(root)]
    found, unreadable = [], [] if len(entries) <= MAX_SCAN else ["(records beyond the scan bound)"]
    for entry in entries[:MAX_SCAN]:
        try:
            if entry.is_symlink():
                unreadable.append(entry.name)
                continue
            if not entry.is_dir(follow_symlinks=False) or not _record_exists(Path(entry.path) / "context.json"):
                continue
            raw, _ = _read_record(Path(entry.path))
        except (OSError, PodError):
            unreadable.append(entry.name)
            continue
        name = _name(raw)
        if name is None:
            unreadable.append(entry.name)
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
        if len(self.conditions) < MAX_LISTED:
            self.conditions.append({"code": code, "status": status, "message": message})

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


def _settled_record(project: Path, row: dict) -> bool:
    """Whether a recovery coordinator's own record is finished: closed, closed by evidence, under historical
    retirement, or continued along a chain that ended closed (R110)."""
    from .succession import chain_state
    if present(row["root"]):
        return True
    phase = _phase(row["root"], row["raw"])
    if phase in ("closed", "retired"):
        return True
    return phase == "continued" and chain_state(project, row["raw"])["state"] == "successor_closed"


def prove(project: Path, objective: str, *, port=None, remote=None, now: datetime | None = None) -> dict:
    """Whether this objective's delivery is proved (C2), read-only: never writes, locks or asks anyone.

    Returns `{status, conditions, facts}`: `proved`, `not_proved` (a task for the project lead), `unverified`
    (something could not be read) or `not_applicable` (closed, continued, under historical retirement, live, or a
    schema Pod does not know). Every unmet condition found is listed, cheapest checks first.
    """
    from .ledger import _record_exists, objective_root
    from .operations import OrcaPort
    project = Path(project)
    proof = _Proof(objective)
    root = objective_root(project, objective)
    try:
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
    _claims(project, objective, raw, kind, proof)
    port = port or OrcaPort(project)
    remote = remote if isinstance(remote, _Remote) else _Remote(project, remote)
    scan: dict = {}

    def records() -> tuple[list[dict], list[str]]:
        if not scan:
            scan["found"], scan["unreadable"] = _scan(project)
        return scan["found"], scan["unreadable"]

    owner = _coordinator(doc, proof)
    if owner is False:
        return proof.result()
    dispatches = _runs(project, doc, owner, proof, records)
    _work(doc, dispatches, proof, port=port)
    state = raw if kind == "current" else None
    _source(doc, state, proof, remote)
    members = _members(project, objective, raw, doc, proof, records)
    _delivery(project, members, proof, remote)
    _preserved(project, root, doc, state, proof, remote, now=now)
    from .obligations import TERMINAL
    from .ledger import map_of
    obligations = (map_of(state) or {}).get("obligations") or [] if state is not None else []
    proof.facts["unfinished"] = [ob["id"] for ob in obligations if ob.get("state") not in TERMINAL][:MAX_LISTED]
    return proof.result()


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


def _runs(project: Path, doc: dict, owner: str | None, proof: _Proof, records) -> dict | None:
    """Each recorded Run: who holds it now (R142 rebinding) and its complete worker list (R112)."""
    from .orca import run_show, terminal_identity, worker_rows
    from .retirement import fleet_dispatches
    runs, dispatches, unread = [], {}, False
    if not doc["runs"]:
        proof.facts.update(runs=[], dispatches_settled=0)
        proof.add("not_proved", "runs_unrecorded", "The objective records no Run whose work could be proven settled.")
        return None
    held = []
    for run in doc["runs"][:32]:
        try:
            shown = run_show(run)
        except PodError as exc:
            runs.append({"run": run, "generation": None, "holder": None})
            proof.add("unverified", "workers_unverified", f"A recorded Run could not be read ({exc.code}).")
            unread = True
            continue
        holder = shown["coordinator_handle"]
        runs.append({"run": run, "generation": shown["consumer_generation"], "holder": holder})
        if holder not in (None, owner) and not _gone(terminal_identity(holder)):
            # A live (or unproven) holder: its own records referencing this Run must all be finished.
            found, unreadable = records()
            mine = [row for row in found if row["raw"].get("owner") == holder and run in _record_runs(row["raw"])]
            if not mine or unreadable or not all(_settled_record(project, row) for row in mine):
                held.append(run)
            else:
                runs[-1].update(rebound="rebound_settled",
                                settled_with=sorted(row["objective"] for row in mine)[:MAX_LISTED])
        try:
            fleet = worker_rows(run)
        except PodError as exc:
            proof.add("unverified", "workers_unverified", f"A Run's worker list could not be read ({exc.code}).")
            unread = True
            continue
        found, refused = fleet_dispatches(fleet)
        if refused is not None:
            proof.add("unverified", *refused)
            unread = True
            continue
        for dispatch in found:
            dispatches[dispatch] = None
    if held:
        proof.add("not_proved", "run_held", "A live coordinator of an open objective holds a recorded Run; it "
                  "settles that Run first.")
    proof.facts.update(runs=runs, dispatches_settled=0)
    return None if unread else dispatches


def _record_runs(raw: dict) -> list[str]:
    from .listing import _facts
    return _facts(raw)["runs"]


def _work(doc: dict, dispatches: dict | None, proof: _Proof, *, port) -> None:
    """R112's work proof, unchanged: no live or unsettled Dispatch and no start Orca still holds pending."""
    from .retirement import work_activity
    if dispatches is None:
        return
    activity, settled = work_activity(doc["admissions"], dispatches, port=port)
    proof.facts["dispatches_settled"] = settled
    if activity is not None:
        code, message, _ = activity
        proof.add("not_proved" if code == "work_live" else "unverified", code,
                  message.replace("retirement waits for it", "closure waits for it"))


def _source(doc: dict, state: dict | None, proof: _Proof, remote: _Remote) -> None:
    """PoDs all ticked, or the issue closed as completed; for a direct objective its recorded criteria (R141)."""
    issue = doc["issue"]
    if issue is None:
        from .ledger import map_of
        from .governance import validate_delivery_record
        map_state = map_of(state) if state is not None else None
        criteria = [ob for ob in (map_state or {}).get("obligations") or [] if ob.get("kind") == "criterion"]
        delivery = (map_state or {}).get("delivery")
        try:
            delivered = delivery is not None and bool(validate_delivery_record(delivery))
        except PodError:
            delivered = False
        proved = (bool(criteria) and all(ob.get("state") in ("satisfied", "withdrawn") for ob in criteria)
                  and any(ob.get("state") == "satisfied" for ob in criteria) and delivered)
        proof.facts["source"] = {"locator": None, "repository": None, "number": None,
                                 "proof": "recorded_criteria" if proved else None, "state": None,
                                 "state_reason": None, "pod_lines": None, "body_sha256": None}
        if not proved:
            proof.add("not_proved", "source_unproved", "A direct objective needs every criterion satisfied or "
                      "withdrawn, at least one satisfied, and a verified delivery record.")
        return
    facts = {"locator": issue["locator"], "repository": issue["repository"], "number": issue["number"],
             "proof": None, "state": None, "state_reason": None, "pod_lines": None, "body_sha256": None}
    proof.facts["source"] = facts
    try:
        row = remote.get().issue_state(repository=issue["repository"], number=issue["number"])
    except PodError as exc:
        proof.add("unverified", "source_unreadable", f"The source issue could not be read on GitHub ({exc.code}).")
        return
    from .github import pod_lines
    lines = [line for rows in pod_lines(row["body"]).values() for line in rows]
    ticked = sum(line["ticked"] for line in lines)
    facts.update(state=row["state"], state_reason=_clip(row.get("state_reason"), 64),
                 pod_lines={"ticked": ticked, "total": len(lines)}, body_sha256=_sha(row["body"].encode()))
    if lines and ticked == len(lines):
        facts["proof"] = "pod_ticks"
    elif row["state"] == "CLOSED" and row.get("state_reason") == "COMPLETED":
        facts["proof"] = "issue_completed"
    else:
        proof.add("not_proved", "source_unproved", f"The source issue has {ticked} of {len(lines)} PoD line(s) "
                  f"ticked and is {row['state'].lower()}" + (f" ({row.get('state_reason')})"
                                                              if row.get("state_reason") else "") + ".")


def _links(raw: dict) -> list[str]:
    return [link["objective"] for link in (raw.get("continued_by"), raw.get("continues"))
            if isinstance(link, dict) and isinstance(link.get("objective"), str)]


def _members(project: Path, objective: str, raw: dict, doc: dict, proof: _Proof, records) -> list[dict] | None:
    """The delivery chain: the objective, its succession links both ways (at most eight hops) and this repository's
    records that share a recorded Run with it and bind the same source issue."""
    from .ledger import objective_root
    members = {objective: {"objective": objective, "root": objective_root(project, objective), "raw": raw,
                           "relation": "objective"}}
    frontier, depth = [objective], 0
    found, unreadable = None, None
    while frontier and depth < MAX_HOPS:
        depth += 1
        following = []
        for name in frontier:
            for link in _links(members[name]["raw"]):
                if link in members:
                    continue
                if found is None:
                    found, unreadable = records()
                row = next((item for item in found if item["objective"] == link), None)
                if row is None:
                    proof.add("unverified", "record_unreadable", f"Linked objective {link} cannot be read.")
                    continue
                relation = "successor" if (members[name]["raw"].get("continued_by") or {}).get("objective") == link \
                    else "predecessor"
                members[link] = {**row, "relation": relation}
                following.append(link)
        frontier = following
    issue = doc["issue"]
    if issue is not None and doc["runs"]:
        if found is None:
            found, unreadable = records()
        for row in found:
            if row["objective"] in members:
                continue
            try:
                other = _documented(row["raw"], current=False)
            except PodError:
                continue
            if (other["issue"] is not None and other["issue"]["locator"].casefold() == issue["locator"].casefold()
                    and set(other["runs"]) & set(doc["runs"])):
                members[row["objective"]] = {**row, "relation": "recovery"}
    rows = list(members.values())
    proof.facts["chain"] = [{"objective": row["objective"], "relation": row["relation"],
                             "phase": _phase(row["root"], row["raw"])} for row in rows][:MAX_MEMBERS]
    if len(rows) > MAX_MEMBERS:
        proof.add("not_proved", "scope_too_large", f"The delivery chain has more than {MAX_MEMBERS} objectives.")
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
    """Every delivery pull request any member records, each merged as GitHub reads it now (R141)."""
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
        proof.add("not_proved", "delivery_unrecorded", "No delivery pull request is recorded for this objective or "
                  "its chain.")
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


def _preserved(project: Path, root: Path, doc: dict, state: dict | None, proof: _Proof, remote: _Remote, *,
               now: datetime | None) -> None:
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
        if row["outcome"] != "FAILED":
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
    temporary = root / f".{FILE}.{os.getpid()}.{_sha(data)[:12]}.tmp"
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


def _predecessors(project: Path, raw: dict) -> list[dict]:
    """The continued predecessors of a chain tail, by completed links only, at most eight."""
    from .ledger import objective_root
    found, current, seen = [], raw, set()
    for _ in range(MAX_HOPS):
        back = current.get("continues")
        name = (current.get("checkpoint") or {}).get("objective")
        if not isinstance(back, dict) or not isinstance(back.get("objective"), str) or back.get("state") is not None:
            break
        if back["objective"] in seen:
            break
        root = objective_root(project, back["objective"])
        try:
            prior, _ = _read_record(root)
        except PodError:
            break
        if (prior.get("continued_by") or {}).get("objective") != name:
            break
        seen.add(back["objective"])
        found.append({"objective": back["objective"], "root": root, "raw": prior})
        current = prior
    return found


def _predecessor_content(project: Path, row: dict, tail: dict, *, caller: str, at: str) -> dict:
    """A continued predecessor's own entry, naming the tail closure; its record stays frozen as R110 left it."""
    raw = row["raw"]
    try:
        _, data = _read_record(row["root"])
        journal = _journal_bytes(row["root"])
    except PodError:
        data, journal = b"", None
    from .listing import _facts
    located = _facts(raw)
    link = raw.get("continued_by") or {}
    try:
        doc = _documented(raw, current=True)
        admissions = doc["admissions"]
    except PodError:
        admissions = {}
    preserved = [{"kind": "admission", "id": key, "role": item["role"], "state": item["state"],
                  "dispatch": (item["native_binding"] or {}).get("dispatchId"),
                  "outcome": "unreported" if item["state"] == "bound" else "UNKNOWN"}
                 for key, item in sorted(admissions.items())
                 if item["state"] not in ("closed", "deferred") and not (item["state"] == "bound" and item["reported"])]
    facts = {"record": {"class": "current", "context_sha256": _sha(data),
                        "governor_sha256": _sha(journal) if journal is not None else None},
             "coordinator": {"handle": raw.get("owner") if isinstance(raw.get("owner"), str) else None,
                             "liveness": "claimed", "cause": None},
             "runs": [{"run": run, "generation": None, "holder": None}
                      for run in (link.get("runs") if isinstance(link.get("runs"), list) else [])][:32],
             "dispatches_settled": 0, "source": deepcopy(tail["source"]), "delivery": deepcopy(tail["delivery"]),
             "chain": deepcopy(tail["chain"]), "preserved": preserved[:MAX_PRESERVED], "counts": counts(preserved),
             "unfinished": list(link.get("unfinished") or [])[:MAX_LISTED],
             "worktrees": located["worktrees"][:MAX_LISTED], "locations_complete": located["locations_complete"]}
    return _content(project, row["objective"], facts, caller=caller, at=at, relation="predecessor",
                    tail={"objective": tail["objective"], "at": tail["at"]})


def close_by_evidence(project: Path, objective: str, *, caller: str, port=None, remote=None,
                      now: datetime | None = None) -> dict:
    """Close one objective by evidence (C3): under its lock and its predecessors', proved again, written once.

    `closed` writes `closure-evidence.json` beside the record and one for each continued predecessor of the chain;
    `replayed` writes nothing (it only completes a predecessor entry an interruption left out); anything else is the
    proof's own status with its conditions, and nothing is written.
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
        chain = _predecessors(project, raw)
    except PodError:
        chain = []
    with ExitStack() as stack:
        for path in sorted({root / "context.json", *(row["root"] / "context.json" for row in chain)}, key=str):
            stack.enter_context(_lock(path))
        if present(root):
            return _replay(project, root, chain, caller=caller)
        verdict = prove(project, objective, port=port, remote=remote, now=now)
        if verdict["status"] != "proved":
            return {"objective": objective, "status": verdict["status"], "conditions": verdict["conditions"]}
        at = (now or datetime.now(timezone.utc)).isoformat()
        content = _content(project, objective, verdict["facts"], caller=caller, at=at, relation="tail",
                           tail={"objective": objective, "at": at})
        if not _create(root, content):
            return _replay(project, root, chain, caller=caller)
        written = _complete_chain(project, chain, content, caller=caller)
        return {"objective": objective, "status": "closed", "closure": compact(content), "predecessors": written}


def _complete_chain(project: Path, chain: list[dict], tail: dict, *, caller: str) -> list[str]:
    written = []
    for row in chain:
        if present(row["root"]):
            continue
        if _create(row["root"], _predecessor_content(project, row, tail, caller=caller, at=tail["at"])):
            written.append(row["objective"])
    return written


def _replay(project: Path, root: Path, chain: list[dict], *, caller: str) -> dict:
    try:
        stored = load(root)
    except PodError as exc:
        return {"objective": root.name, "status": "replayed", "closure": {"status": "unreadable", "code": exc.code}}
    completed = _complete_chain(project, chain, stored, caller=caller) if stored["relation"] == "tail" else []
    out = {"objective": stored["objective"], "status": "replayed", "closure": compact(stored)}
    if completed:
        out["completed"] = completed
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


# --------------------------------------------------------------------------- sweep (R144)


def _outcome(objective: str, record: str | None, verdict: dict) -> dict:
    status = verdict["status"]
    result = {"closed": "closed", "replayed": "already_closed", "not_proved": "not_proved",
              "unverified": "unverified"}.get(status, "skipped")
    row = {"objective": objective, "record": record, "result": result,
           "conditions": [dict(item) for item in verdict.get("conditions", [])][:MAX_LISTED]}
    if result in ("closed", "already_closed"):
        row["closure"] = verdict.get("closure")
        for key in ("predecessors", "completed"):
            if verdict.get(key):
                row[key] = list(verdict[key])
    if result in ("not_proved", "unverified"):
        row.update(party="lead", next_action=LEAD_ACTION)
    return row


def sweep(project: Path, *, repositories: list[str] | None = None, objectives: list[str] | None = None,
          port=None, remote=None) -> dict:
    """Close every provably delivered open objective of each repository (C6), idempotently.

    Needs no Orca terminal, Run binding or live coordinator; starts nothing and mutates no Orca, Git or provider
    state. It writes only `closure-evidence.json` files, and a second run writes nothing.
    """
    from .github import repository_context
    from .listing import objective_list
    targets = [Path(path).expanduser() for path in (repositories or [str(project)])]
    names = list(dict.fromkeys(objectives or []))
    if len(targets) > MAX_LISTED or len(names) > MAX_LISTED:
        raise PodError("sweep_scope_unavailable", f"A sweep names at most {MAX_LISTED} repositories and objectives")
    for target in targets:
        if not target.is_dir():
            raise PodError("sweep_scope_unavailable", "A swept repository path is not a directory",
                           {"repository": str(target)})
        if repository_context(target.resolve())["repo_key"] is None:
            raise PodError("sweep_scope_unavailable", "A swept path is not a Git repository",
                           {"repository": str(target)})
    out = {"schema": "pod-sweep/v1", "status": "swept", "repositories": [],
           "counts": {name: 0 for name in RESULTS}}
    for target in targets:
        target = target.resolve()
        listed = objective_list(target, repository=str(target))
        rows = listed["objectives"]
        if names:
            rows = [row for row in rows if row["objective"] in names]
        # Successor tails first: closing a tail writes its continued predecessors' entries too.
        rows = sorted(rows, key=lambda row: (row["phase"] == "continued", row["objective"]))
        done, results = set(), []
        for row in rows:
            if row["objective"] in done:
                continue
            done.add(row["objective"])
            verdict = close_by_evidence(target, row["objective"], caller="sweep", port=port, remote=remote)
            results.append(_outcome(row["objective"], row["record"], verdict))
        for name in names:
            if name not in done:
                results.append({"objective": name, "record": None, "result": "skipped",
                                "conditions": [{"code": "objective_unknown", "status": "not_applicable",
                                                "message": "No record of that objective is listed in this "
                                                           "repository."}]})
        for row in results:
            out["counts"][row["result"]] += 1
        out["repositories"].append({"repository": str(target), "repo_key": listed["scope"]["repo_key"],
                                    "list_status": listed["status"], "complete": listed["complete"],
                                    "unreadable_records": listed["unreadable_records"],
                                    "objectives": results})
    return out


def render_sweep(result: dict) -> None:
    from .term import clean
    tally = result["counts"]
    print("Pod sweep: " + ", ".join(f"{tally[name]} {name.replace('_', ' ')}" for name in RESULTS))
    for repository in result["repositories"]:
        print(f"Repository {clean(repository['repository'])}: {len(repository['objectives'])} objective(s)"
              + ("" if repository["complete"] else "; some records are unreadable"))
        for row in repository["objectives"]:
            reason = "; ".join(f"{item['code']}: {clean(item['message'])}" for item in row["conditions"][:3])
            print(f"  {clean(row['objective'])[:64]}: {row['result'].replace('_', ' ')}"
                  + (f" ({reason})" if reason else "") + (" — a task for the project lead" if row.get("party") else ""))
